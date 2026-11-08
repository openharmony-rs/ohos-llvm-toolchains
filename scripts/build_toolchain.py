#!/usr/bin/env python3
"""Build an OpenHarmony LLVM toolchain from the OpenHarmony LLVM fork's source.

For the toolchains in toolchains.json that have a `source` entry: the fork at a
pinned commit, plus this repository's patches from patches/<version>/.

Subcommands, normally run in this order:
  source <version> <dir>                       check out the fork and apply the patches
  host <version> <src> <work>                  clang, lld and the LLVM tools for a host
  target <version> <src> <work> <triple>...    compiler-rt and libc++ for OpenHarmony targets
  package <version> <src> <work> <outdir>      the release archive of a host, and SHA256SUMS

The output has the layout of the fork's prebuilt toolchains, reduced to what
cargo-ohos uses: clang, lld, the LLVM binutils, libclang, and per target the
compiler-rt runtimes (including the sanitizers) and the toolchain flavour of
libc++ (ABI namespace `__h`).

Hosts: linux-x86_64 and darwin-arm64 are built on themselves, windows-x86_64
is cross-compiled on Linux with llvm-mingw (`--mingw`), like the fork's own
Windows toolchains.

The target runtimes are built against the sysroot of an OpenHarmony SDK
(`--ohos-sdk-native`, default `$OHOS_SDK_NATIVE`), like the applications that
use them. They do not depend on the host, so they are built once, with the
linux-x86_64 host tools, and `package --host-install` combines them with the
host tools of another host.
"""

import argparse
import filecmp
import gzip
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "toolchains.json"
PATCHES_DIR = REPO_ROOT / "patches"

HOSTS = ["linux-x86_64", "darwin-arm64", "windows-x86_64"]
MACOS_DEPLOYMENT_TARGET = "11.0"
MINGW_TRIPLE = "x86_64-w64-mingw32"
# MinGW's import library for libclang.dll, of no use to MSVC programs. cargo-ohos
# would take lib/ for the directory of libclang.dll because of it.
WINDOWS_EXCLUDED = {"lib/libclang.dll.a"}

# The tools cargo-ohos resolves, plus what clang itself runs or ships.
DISTRIBUTION_COMPONENTS = [
    "clang",
    "clang-resource-headers",
    "lld",
    "libclang",
    "llvm-ar",
    "llvm-ranlib",
    "llvm-nm",
    "llvm-objcopy",
    "llvm-strip",
    "llvm-objdump",
    "llvm-readobj",
    "llvm-readelf",
    "llvm-symbolizer",
]

# clang triple -> extra compile flags for the runtimes. arm is the default
# (soft-float) multilib only, which is what `armv7-unknown-linux-ohos` links.
TARGETS = {
    "aarch64-linux-ohos": [],
    "arm-linux-ohos": ["-march=armv7-a", "-mfloat-abi=soft"],
    "x86_64-linux-ohos": [],
}


def load_entry(version: str) -> dict:
    manifest = json.loads(MANIFEST.read_text())
    entry = manifest["toolchains"].get(version)
    if entry is None or "source" not in entry:
        sys.exit(f"error: {version} is not a source-built toolchain in {MANIFEST.name}")
    return entry


def run(cmd: list, **kwargs):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def cmake(src: Path, build: Path, defines: dict, targets: list[str] | None = None):
    build.mkdir(parents=True, exist_ok=True)
    run(["cmake", "-G", "Ninja", "-S", src, "-B", build]
        + [f"-D{key}={value}" for key, value in defines.items()])
    run(["ninja", "-C", build] + (targets or []))


def native_host() -> str | None:
    return {
        ("Linux", "x86_64"): "linux-x86_64",
        ("Darwin", "arm64"): "darwin-arm64",
    }.get((platform.system(), platform.machine()))


def host_specific(rel: Path) -> bool:
    """Whether a path of an install belongs to its host (the programs and libclang)
    rather than to the OpenHarmony targets."""
    return rel.parts[0] == "bin" or (
        rel.parts[0] == "lib" and len(rel.parts) == 2 and rel.name.startswith("libclang"))


def clang_major(src: Path) -> str:
    for line in (src / "cmake" / "Modules" / "LLVMVersion.cmake").read_text().splitlines():
        if "LLVM_VERSION_MAJOR" in line and "set(" in line:
            return line.split()[-1].rstrip(")")
    sys.exit("error: could not determine the LLVM major version")


def cmd_source(args):
    entry = load_entry(args.version)
    source = entry["source"]
    dest = Path(args.dir)
    if not (dest / ".git").is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q", dest])
        run(["git", "-C", dest, "remote", "add", "origin", source["repository"]])
    run(["git", "-C", dest, "fetch", "--depth", "1", "origin", source["commit"]])
    run(["git", "-C", dest, "checkout", "-q", "--force", "FETCH_HEAD"])
    run(["git", "-C", dest, "clean", "-q", "-fdx"])
    for patch in source.get("patches", []):
        run(["git", "-C", dest, "apply", "--verbose", (PATCHES_DIR / args.version / patch).resolve()])
    return 0


def mingw_defines(mingw: Path) -> dict:
    """Cross-compiling for Windows with llvm-mingw, linked statically so that the
    programs need nothing but the system's DLLs (the UCRT)."""
    bin_dir = mingw / "bin"
    return {
        "CMAKE_SYSTEM_NAME": "Windows",
        "CMAKE_SYSTEM_PROCESSOR": "x86_64",
        "CMAKE_C_COMPILER": bin_dir / f"{MINGW_TRIPLE}-clang",
        "CMAKE_CXX_COMPILER": bin_dir / f"{MINGW_TRIPLE}-clang++",
        "CMAKE_RC_COMPILER": bin_dir / f"{MINGW_TRIPLE}-windres",
        "CMAKE_AR": bin_dir / "llvm-ar",
        "CMAKE_RANLIB": bin_dir / "llvm-ranlib",
        "CMAKE_FIND_ROOT_PATH": mingw / MINGW_TRIPLE,
        "CMAKE_FIND_ROOT_PATH_MODE_PROGRAM": "NEVER",
        "CMAKE_FIND_ROOT_PATH_MODE_LIBRARY": "ONLY",
        "CMAKE_FIND_ROOT_PATH_MODE_INCLUDE": "ONLY",
        "CMAKE_FIND_ROOT_PATH_MODE_PACKAGE": "ONLY",
        "CMAKE_EXE_LINKER_FLAGS": "-static",
        "CMAKE_SHARED_LINKER_FLAGS": "-static",
        "CMAKE_MODULE_LINKER_FLAGS": "-static",
        "LLVM_HOST_TRIPLE": "x86_64-w64-windows-gnu",
        # llvm-mingw has no zlib.
        "LLVM_ENABLE_ZLIB": "OFF",
    }


def cmd_host(args):
    entry = load_entry(args.version)
    src = Path(args.src).resolve()
    work = Path(args.work).resolve()
    native = native_host()
    host = args.host or native
    defines = {
        "CMAKE_BUILD_TYPE": "Release",
        "CMAKE_INSTALL_PREFIX": work / "install",
        "LLVM_ENABLE_PROJECTS": "clang;lld",
        "LLVM_TARGETS_TO_BUILD": "AArch64;ARM;X86",
        "LLVM_ENABLE_ASSERTIONS": "OFF",
        "LLVM_ENABLE_BINDINGS": "OFF",
        "LLVM_ENABLE_TERMINFO": "OFF",
        "LLVM_ENABLE_ZSTD": "OFF",
        "LLVM_ENABLE_LIBXML2": "OFF",
        "LLVM_INCLUDE_TESTS": "OFF",
        "LLVM_INCLUDE_BENCHMARKS": "OFF",
        "LLVM_INCLUDE_EXAMPLES": "OFF",
        # Linking clang, lld or libclang takes GBs; the CI runners have 7 to 16 GB.
        "LLVM_PARALLEL_LINK_JOBS": "2",
        "CLANG_DEFAULT_LINKER": "lld",
        # The fork's OpenHarmony driver features (TSan support, the libcxx-ohos
        # header directory, ...) are compiled in only with this.
        "OHOS_LLVM": "ON",
        "CLANG_VENDOR": "OHOS (openharmony-rs) ",
        "LLVM_FORCE_VC_REPOSITORY": entry["source"]["repository"],
        "LLVM_FORCE_VC_REVISION": entry["source"]["commit"],
        "LLVM_DISTRIBUTION_COMPONENTS": ";".join(DISTRIBUTION_COMPONENTS),
    }
    if host == "windows-x86_64":
        if not args.mingw:
            sys.exit("error: the windows-x86_64 host is cross-compiled: pass --mingw <llvm-mingw dir>")
        defines.update(mingw_defines(Path(args.mingw).resolve()))
    elif host == native == "linux-x86_64":
        defines["LLVM_STATIC_LINK_CXX_STDLIB"] = "ON"
    elif host == native == "darwin-arm64":
        defines["CMAKE_OSX_ARCHITECTURES"] = "arm64"
        defines["CMAKE_OSX_DEPLOYMENT_TARGET"] = MACOS_DEPLOYMENT_TARGET
    else:
        sys.exit(f"error: cannot build the {host} host tools on {native or 'this machine'}")
    if args.linker:
        defines["LLVM_USE_LINKER"] = args.linker
    if shutil.which("ccache"):
        defines["LLVM_CCACHE_BUILD"] = "ON"
    build = work / "build" / "host"
    cmake(src / "llvm", build, defines, ["distribution"])
    run(["ninja", "-C", build, "install-distribution"])
    return 0


def target_defines(src: Path, work: Path, triple: str, sysroot: Path) -> dict:
    """The CMake setup the fork's llvm-build/build.py uses for target libraries."""
    install = work / "install"
    platform = work / "cmake-platform"
    (platform / "Platform").mkdir(parents=True, exist_ok=True)
    # The llvm-build copy, unlike the SDK's, disables versioned sonames, which
    # OpenHarmony's loader does not resolve.
    shutil.copy(src / "llvm-build" / "OHOS.cmake", platform / "Platform" / "OHOS.cmake")
    # CMake 4 has its own Platform/OHOS.cmake, which is just Linux's and which it
    # prefers to ours, so its settings are applied again after the platform's.
    overrides = platform / "OHOSOverrides.cmake"
    overrides.write_text("set(CMAKE_PLATFORM_NO_VERSIONED_SONAME 1)\n"
                         "set(CMAKE_SHARED_LIBRARY_RUNTIME_C_FLAG \"\")\n"
                         "set(CMAKE_SHARED_LIBRARY_RUNTIME_CXX_FLAG \"\")\n")
    cflags = [
        f"--target={triple}",
        "-D__MUSL__",
        "-fstack-protector-strong",
        "-ffunction-sections",
        "-fdata-sections",
        "-funwind-tables",
        "-fno-omit-frame-pointer",
        f"-ffile-prefix-map={src}=.",
    ] + TARGETS[triple]
    ldflags = [
        "-fuse-ld=lld",
        "-Wl,--gc-sections",
        "-Wl,--build-id=sha1",
        "-Wl,-z,relro,-z,now",
        "--rtlib=compiler-rt",
        "-stdlib=libc++",
        f"-L{install / 'lib' / triple}",
    ]
    return {
        "CMAKE_BUILD_TYPE": "Release",
        "CMAKE_SYSTEM_NAME": "OHOS",
        "CMAKE_CROSSCOMPILING": "True",
        "CMAKE_MODULE_PATH": platform,
        "CMAKE_USER_MAKE_RULES_OVERRIDE": overrides,
        "CMAKE_SYSROOT": sysroot,
        "CMAKE_FIND_ROOT_PATH_MODE_PROGRAM": "NEVER",
        "CMAKE_C_COMPILER": install / "bin" / "clang",
        "CMAKE_CXX_COMPILER": install / "bin" / "clang++",
        "CMAKE_ASM_COMPILER": install / "bin" / "clang",
        "CMAKE_AR": install / "bin" / "llvm-ar",
        "CMAKE_RANLIB": install / "bin" / "llvm-ranlib",
        "CMAKE_NM": install / "bin" / "llvm-nm",
        "CMAKE_C_FLAGS": " ".join(cflags),
        "CMAKE_CXX_FLAGS": " ".join(cflags),
        "CMAKE_ASM_FLAGS": " ".join(cflags),
        "CMAKE_EXE_LINKER_FLAGS": " ".join(ldflags),
        "CMAKE_SHARED_LINKER_FLAGS": " ".join(ldflags),
        "CMAKE_MODULE_LINKER_FLAGS": " ".join(ldflags),
        "CMAKE_TRY_COMPILE_TARGET_TYPE": "STATIC_LIBRARY",
        "CMAKE_POSITION_INDEPENDENT_CODE": "ON",
        "OHOS": "1",
        "OHOS_LLVM": "ON",
        "LLVM_ENABLE_PER_TARGET_RUNTIME_DIR": "ON",
        "LLVM_DEFAULT_TARGET_TRIPLE": triple,
    }


def build_compiler_rt(src: Path, work: Path, triple: str, sysroot: Path, sanitizers: bool):
    defines = target_defines(src, work, triple, sysroot)
    defines.update({
        "CMAKE_INSTALL_PREFIX": work / "install" / "lib" / "clang" / clang_major(src),
        "COMPILER_RT_STANDALONE_BUILD": "ON",
        "LLVM_CMAKE_DIR": src / "llvm" / "cmake" / "modules",
        "COMPILER_RT_DEFAULT_TARGET_TRIPLE": triple,
        "COMPILER_RT_USE_BUILTINS_LIBRARY": "ON",
        "COMPILER_RT_EXCLUDE_ATOMIC_BUILTIN": "OFF",
        "COMPILER_RT_INCLUDE_TESTS": "OFF",
        "COMPILER_RT_BUILD_BUILTINS": "OFF" if sanitizers else "ON",
        "COMPILER_RT_BUILD_CRT": "OFF" if sanitizers else "ON",
        "COMPILER_RT_BUILD_SANITIZERS": "ON" if sanitizers else "OFF",
        "COMPILER_RT_SANITIZERS_TO_BUILD": "asan;tsan;ubsan_minimal",
        "SANITIZER_CXX_ABI": "libcxxabi",
        # gwp_asan needs <sigchain.h>, which only OpenHarmony's platform sysroot has.
        "COMPILER_RT_BUILD_GWP_ASAN": "OFF",
        "COMPILER_RT_BUILD_LIBFUZZER": "OFF",
        "COMPILER_RT_BUILD_MEMPROF": "OFF",
        "COMPILER_RT_BUILD_ORC": "OFF",
        "COMPILER_RT_BUILD_PROFILE": "OFF",
        "COMPILER_RT_BUILD_XRAY": "OFF",
        "COMPILER_RT_BUILD_CTX_PROFILE": "OFF",
    })
    stage = "sanitizers" if sanitizers else "builtins"
    build = work / "build" / f"compiler-rt-{stage}-{triple}"
    cmake(src / "compiler-rt", build, defines)
    run(["ninja", "-C", build, "install"])


def build_libcxx(src: Path, work: Path, triple: str, sysroot: Path):
    install = work / "install"
    headers = install / "include" / "libcxx-ohos" / "include" / "c++" / "v1"
    defines = target_defines(src, work, triple, sysroot)
    defines.update({
        "CMAKE_INSTALL_PREFIX": install,
        "LLVM_ENABLE_RUNTIMES": "libunwind;libcxxabi;libcxx",
        "LIBUNWIND_USE_COMPILER_RT": "ON",
        "LIBUNWIND_ENABLE_SHARED": "OFF",
        "LIBCXXABI_USE_COMPILER_RT": "ON",
        "LIBCXXABI_USE_LLVM_UNWINDER": "ON",
        "LIBCXXABI_ENABLE_STATIC_UNWINDER": "ON",
        "LIBCXXABI_STATICALLY_LINK_UNWINDER_IN_STATIC_LIBRARY": "OFF",
        "LIBCXXABI_HAS_CXA_THREAD_ATEXIT_IMPL": "OFF",
        "LIBCXXABI_ENABLE_SHARED": "OFF",
        "LIBCXXABI_INSTALL_INCLUDE_DIR": headers,
        "LIBCXX_USE_COMPILER_RT": "ON",
        "LIBCXX_HAS_MUSL_LIBC": "ON",
        "LIBCXX_ENABLE_ABI_LINKER_SCRIPT": "OFF",
        "LIBCXX_ENABLE_STATIC_ABI_LIBRARY": "ON",
        "LIBCXX_ABI_NAMESPACE": "__h",
        "LIBCXX_INSTALL_INCLUDE_DIR": headers,
        "LIBCXX_INSTALL_INCLUDE_TARGET_DIR": headers,
        "LIBCXX_INCLUDE_BENCHMARKS": "OFF",
        "LIBCXX_INCLUDE_TESTS": "OFF",
        "LIBCXXABI_INCLUDE_TESTS": "OFF",
        "LIBUNWIND_INCLUDE_TESTS": "OFF",
    })
    build = work / "build" / f"runtimes-{triple}"
    cmake(src / "runtimes", build, defines)
    run(["ninja", "-C", build, "install"])


def cmd_target(args):
    load_entry(args.version)
    src = Path(args.src).resolve()
    work = Path(args.work).resolve()
    if not args.ohos_sdk_native:
        sys.exit("error: no OpenHarmony SDK: pass --ohos-sdk-native or set OHOS_SDK_NATIVE")
    sysroot = Path(args.ohos_sdk_native).resolve() / "sysroot"
    for triple in args.triples:
        if triple not in TARGETS:
            sys.exit(f"error: unsupported target {triple}; expected one of {', '.join(TARGETS)}")
        # The builtins have to exist before anything links, and the sanitizer
        # runtimes link against libc++abi.
        build_compiler_rt(src, work, triple, sysroot, sanitizers=False)
        build_libcxx(src, work, triple, sysroot)
        build_compiler_rt(src, work, triple, sysroot, sanitizers=True)
    return 0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def package_files(host: str, install: Path, host_install: Path) -> dict[Path, Path]:
    """The archive's files, relative path -> source: the host-specific ones from
    the host's install, the rest from the install the target runtimes went into."""
    files = {}
    for path in sorted(host_install.rglob("*")):
        rel = path.relative_to(host_install)
        if host_specific(rel):
            if not (host == "windows-x86_64" and rel.as_posix() in WINDOWS_EXCLUDED):
                files[rel] = path
            continue
        if host_install == install:
            continue
        other = install / rel
        same = other.is_dir() if path.is_dir() else other.is_file() and filecmp.cmp(path, other, shallow=False)
        if not same:
            sys.exit(f"error: {rel} of the {host} host install is not in {install}, or differs")
    for path in sorted(install.rglob("*")):
        rel = path.relative_to(install)
        if host_specific(rel):
            continue
        if path.is_symlink():
            sys.exit(f"error: {rel} is a symbolic link: the target runtimes need unversioned "
                     "sonames, and Windows cannot unpack symbolic links")
        files[rel] = path
    return files


def cmd_package(args):
    entry = load_entry(args.version)
    host = args.host
    if host not in entry["hosts"]:
        sys.exit(f"error: {MANIFEST.name} has no {host} host for {args.version}")
    src = Path(args.src).resolve()
    install = Path(args.work).resolve() / "install"
    host_install = Path(args.host_install).resolve() if args.host_install else install
    out = Path(args.outdir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    patches = entry["source"].get("patches", [])
    extra = {
        "LICENSE.TXT": (src / "LICENSE.TXT").read_bytes(),
        "BUILD_INFO.json": (json.dumps({
            "version": args.version,
            "llvm_version": entry["llvm_version"],
            "host": host,
            "source": entry["source"],
            "built_by": "https://github.com/openharmony-rs/ohos-llvm-toolchains",
            "note": "Built from the OpenHarmony LLVM fork's source with the patches in patches/, "
                    "which are not part of the fork.",
        }, indent=2) + "\n").encode(),
    }
    for patch in patches:
        extra[f"patches/{patch}"] = (PATCHES_DIR / args.version / patch).read_bytes()

    # Reproducible: fixed order, owner and timestamps.
    mtime = int(subprocess.run(["git", "-C", src, "log", "-1", "--format=%ct"],
                               capture_output=True, text=True, check=True).stdout)

    def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = mtime
        return info

    # Windows cannot create symbolic links without privileges, so there the
    # aliases (clang++, ld.lld, ...) become hard links, after all the files.
    hard_links = []
    archive = out / entry["hosts"][host]["clang"]["file"]
    with open(archive, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=mtime) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        tar.add(install, arcname="llvm", recursive=False, filter=normalize)
        for rel, path in sorted(package_files(host, install, host_install).items()):
            arcname = f"llvm/{rel.as_posix()}"
            if host == "windows-x86_64" and path.is_symlink():
                info = normalize(tarfile.TarInfo(arcname))
                info.type = tarfile.LNKTYPE
                info.linkname = f"llvm/{path.resolve().relative_to(host_install).as_posix()}"
                info.mode = 0o755
                hard_links.append(info)
            else:
                tar.add(path, arcname=arcname, recursive=False, filter=normalize)
        for info in hard_links:
            tar.addfile(info)
        for name, data in sorted(extra.items()):
            info = normalize(tarfile.TarInfo(f"llvm/{name}"))
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    print(f"== wrote {archive} ({archive.stat().st_size >> 20} MiB)")

    archives = sorted(out / h["clang"]["file"] for h in entry["hosts"].values()
                      if (out / h["clang"]["file"]).is_file())
    (out / "SHA256SUMS").write_text("".join(f"{sha256_file(a)}  {a.name}\n" for a in archives))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("source", help="check out the fork and apply the patches")
    s.add_argument("version")
    s.add_argument("dir")
    s.set_defaults(func=cmd_source)

    h = sub.add_parser("host", help="build clang, lld and the LLVM tools")
    h.add_argument("version")
    h.add_argument("src")
    h.add_argument("work")
    h.add_argument("--host", choices=HOSTS, help="the host to build for (default: this machine)")
    h.add_argument("--mingw", help="llvm-mingw toolchain to cross-compile the windows-x86_64 host with")
    h.add_argument("--linker", help="LLVM_USE_LINKER for the host build, e.g. lld or gold "
                   "(default: the compiler's)")
    h.set_defaults(func=cmd_host)

    t = sub.add_parser("target", help="build compiler-rt and libc++ for OpenHarmony targets")
    t.add_argument("version")
    t.add_argument("src")
    t.add_argument("work")
    t.add_argument("triples", nargs="+", metavar="triple")
    t.add_argument("--ohos-sdk-native", default=os.environ.get("OHOS_SDK_NATIVE"),
                   help="SDK native dir whose sysroot the runtimes build against (default: $OHOS_SDK_NATIVE)")
    t.set_defaults(func=cmd_target)

    p = sub.add_parser("package", help="write the release archive and SHA256SUMS")
    p.add_argument("version")
    p.add_argument("src")
    p.add_argument("work")
    p.add_argument("outdir")
    p.add_argument("--host", choices=HOSTS, default="linux-x86_64", help="the host of the archive")
    p.add_argument("--host-install", help="the install of `host --host <host>`, if it is not "
                   "the one in <work> that the target runtimes went into")
    p.set_defaults(func=cmd_package)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
