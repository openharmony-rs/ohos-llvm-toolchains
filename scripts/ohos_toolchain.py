#!/usr/bin/env python3
"""Validate and mirror the OpenHarmony LLVM toolchains pinned in toolchains.json.

Subcommands:
  validate <version> <host>   check one host archive pair against the manifest
  fetch <version> <outdir>    download + verify all archives of a version (mirroring)

Validation covers: archive checksums, toolchain layout, libc++ ABI namespaces
and version, an exported-symbol ABI snapshot (removals break shipped apps),
and with --full compile/link/run smoke tests against an OHOS sysroot — under
qemu-user (aarch64, static) and, with --device, on a real OpenHarmony system
(e.g. the Oniro emulator) via hdc, including dynamic loading of the
toolchain's own libc++.so.

Stdlib only; external tools used: tar, and hdc/qemu when requested.
"""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "toolchains.json"

TESTS_DIR = REPO_ROOT / "tests"
DEVICE_DIR = "/data/local/tmp/ohos-llvm-validate"


def discover_tests() -> list[tuple[Path, list[str]]]:
    """Each .c/.cpp file in tests/ is one test; an optional `ohos-flags:`
    comment directive in its first ten lines declares extra compile flags.
    See tests/README.md for the conventions."""
    tests = []
    for src in sorted(TESTS_DIR.iterdir()):
        if src.suffix not in (".c", ".cpp"):
            continue
        flags: list[str] = []
        for line in src.read_text().splitlines()[:10]:
            if "ohos-flags:" in line:
                flags = line.split("ohos-flags:", 1)[1].split()
                break
        tests.append((src, flags))
    return tests


def load_manifest():
    return json.loads(MANIFEST.read_text())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path):
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(3):
        try:
            print(f"== downloading {url}")
            with urllib.request.urlopen(url) as r, part.open("wb") as f:
                shutil.copyfileobj(r, f)
            part.rename(dest)
            return
        except OSError as e:
            part.unlink(missing_ok=True)
            if attempt == 2:
                raise
            print(f"   retrying after error: {e}")
            time.sleep(5)


def fetch_archive(work: Path, base: str, version: str, host_dir: str, entry: dict) -> Path:
    """Download (if needed), checksum-verify, and extract one archive."""
    dest = work / entry["file"]
    if not dest.is_file():
        download(f"{base}/{version}/{host_dir}/{entry['file']}", dest)
    got = sha256_file(dest)
    if got != entry["sha256"]:
        sys.exit(
            f"error: sha256 mismatch for {entry['file']}\n"
            f"  expected: {entry['sha256']}\n  got:      {got}"
        )
    print(f"== sha256 OK: {entry['file']}")
    top = subprocess.run(
        ["tar", "tzf", str(dest)], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0].split("/")[0]
    if not (work / top).is_dir():
        subprocess.run(["tar", "xzf", str(dest), "-C", str(work)], check=True)
    return work / top


class Checker:
    def __init__(self):
        self.failed = False

    def check(self, description: str, ok: bool, detail: str = ""):
        if ok:
            print(f"ok: {description}")
        else:
            print(f"FAIL: {description}")
            if detail:
                print(detail)
            self.failed = True

    def run(self, description: str, cmd: list, **kwargs) -> subprocess.CompletedProcess:
        p = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
        detail = (p.stdout + p.stderr).strip()
        self.check(description, p.returncode == 0, detail if p.returncode != 0 else "")
        return p


def host_is_runnable(host: str) -> bool:
    system, machine = platform.system(), platform.machine()
    return {
        ("Linux", "x86_64"): "linux-x86_64",
        ("Darwin", "arm64"): "darwin-arm64",
        ("Darwin", "x86_64"): "darwin-x86_64",
        ("Windows", "AMD64"): "windows-x86_64",
    }.get((system, machine)) == host


def tool(clang_dir: Path, host: str, name: str) -> Path:
    return clang_dir / "bin" / (name + (".exe" if host.startswith("windows") else ""))


def hdc(key: str, *args: str) -> subprocess.CompletedProcess:
    cmd = ["hdc"] + (["-t", key] if key else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def device_run(key: str, binary: Path, marker: str, lib: Path | None = None) -> tuple[bool, str]:
    """Push a binary (and optionally a shared library) to the device, run it,
    and report whether its output contains the marker."""
    name = binary.name
    hdc(key, "shell", f"mkdir -p {DEVICE_DIR}/lib")
    sent = hdc(key, "file", "send", str(binary), f"{DEVICE_DIR}/{name}")
    if sent.returncode != 0:
        return False, sent.stdout + sent.stderr
    if lib is not None:
        sent = hdc(key, "file", "send", str(lib), f"{DEVICE_DIR}/lib/{lib.name}")
        if sent.returncode != 0:
            return False, sent.stdout + sent.stderr
    # hdc shell does not propagate exit codes; echo it and parse.
    out = hdc(
        key, "shell",
        f"chmod +x {DEVICE_DIR}/{name}; "
        f"LD_LIBRARY_PATH={DEVICE_DIR}/lib {DEVICE_DIR}/{name}; echo exit:$?",
    )
    text = out.stdout + out.stderr
    return marker in text and "exit:0" in text, text.strip()


def cmd_validate(args):
    manifest = load_manifest()
    entry = manifest["toolchains"].get(args.version)
    if entry is None:
        sys.exit(f"error: version '{args.version}' not in toolchains.json")
    host = entry["hosts"].get(args.host)
    if host is None:
        print(f"SKIP: host '{args.host}' not defined for {args.version}")
        return 0

    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="ohos-llvm-validate."))
    work.mkdir(parents=True, exist_ok=True)
    print(f"== work dir: {work}")

    base = manifest["mirror_base"]
    clang_dir = fetch_archive(work, base, args.version, host["dir"], host["clang"])
    ndk_dir = fetch_archive(work, base, args.version, host["dir"], host["libcxx_ndk"])

    c = Checker()

    # Layout and metadata (host-independent).
    for target in entry["targets"]:
        c.check(
            f"target runtime dir lib/{target} with libc++.so",
            (clang_dir / "lib" / target / "libc++.so").is_file(),
        )

    v1 = "include/libcxx-ohos/include/c++/v1"
    ns = entry["abi_namespace"]
    for flavor, root, want in (("toolchain", clang_dir, ns["toolchain"]), ("ndk", ndk_dir, ns["ndk"])):
        site = root / v1 / "__config_site"
        text = site.read_text() if site.is_file() else ""
        c.check(
            f"{flavor} libc++ ABI namespace is {want}",
            f"#define _LIBCPP_ABI_NAMESPACE {want}\n" in text,
        )
    config = clang_dir / v1 / "__config"
    c.check(
        f"_LIBCPP_VERSION is {entry['libcpp_version']}",
        f"define _LIBCPP_VERSION {entry['libcpp_version']}\n" in config.read_text(),
    )

    runnable = host_is_runnable(args.host)
    if runnable:
        p = subprocess.run([tool(clang_dir, args.host, "clang"), "--version"], capture_output=True, text=True)
        c.check(
            f"clang --version reports {entry['llvm_version']}",
            f"clang version {entry['llvm_version']}" in p.stdout,
            p.stdout.strip(),
        )

    # ABI snapshot: the exported-symbol set of each libc++ flavor is part of
    # the contract; a removal between releases breaks already-shipped apps.
    nm = tool(clang_dir, args.host, "llvm-nm")
    if not runnable:
        nm = shutil.which("llvm-nm")
    abi_target = "aarch64-linux-ohos"
    abi_dir = REPO_ROOT / "abi" / args.version
    if nm:
        for flavor, root in (("toolchain", clang_dir), ("ndk", ndk_dir)):
            so = root / "lib" / abi_target / "libc++.so"
            p = subprocess.run([nm, "-D", "--defined-only", str(so)], capture_output=True, text=True, check=True)
            current = sorted({line.split()[2] for line in p.stdout.splitlines() if len(line.split()) == 3})
            snap = abi_dir / f"{abi_target}-{flavor}.syms"
            if args.write_abi:
                abi_dir.mkdir(parents=True, exist_ok=True)
                snap.write_text("\n".join(current) + "\n")
                print(f"wrote {snap}")
            elif snap.is_file():
                recorded = snap.read_text().split()
                removed = sorted(set(recorded) - set(current))
                added = sorted(set(current) - set(recorded))
                detail = "\n".join(
                    [f"  removed: {s}" for s in removed[:10]] + [f"  added:   {s}" for s in added[:10]]
                )
                c.check(f"{flavor} libc++.so export set matches abi snapshot", not removed and not added, detail)
            else:
                c.check(f"abi snapshot {snap} exists (run with --write-abi to create)", False)
    else:
        print("note: no runnable llvm-nm available, skipping ABI snapshot check")

    if args.full:
        run_full(args, entry, clang_dir, c, work)

    if c.failed:
        print(f"== validation FAILED for {args.version} {args.host} (work dir kept: {work})")
        return 1
    print(f"== validation OK for {args.version} {args.host}")
    return 0


def run_full(args, entry, clang_dir: Path, c: Checker, work: Path):
    if not host_is_runnable(args.host):
        c.check(f"--full requires the {args.host} archive to match this machine", False)
        return
    sdk_native = args.ohos_sdk_native
    if not sdk_native:
        c.check("--full requires OHOS_SDK_NATIVE (or --ohos-sdk-native)", False)
        return
    sysroot = Path(sdk_native) / "sysroot"
    tests = discover_tests()
    c.check(f"tests/ contains tests ({len(tests)} found)", bool(tests))

    def compiler_for(src: Path) -> Path:
        return tool(clang_dir, args.host, "clang++" if src.suffix == ".cpp" else "clang")

    def build(src: Path, flags: list[str], triple: str, static: bool) -> Path | None:
        kind = "static" if static else "shared"
        out = work / f"{src.stem}-{triple}-{kind}"
        cmd = [
            str(compiler_for(src)), f"--target={triple}", f"--sysroot={sysroot}",
            "-fuse-ld=lld", *flags, *(["-static"] if static else []),
            str(src), "-o", str(out),
        ]
        p = c.run(f"{src.name}: compile+link ({triple}, {kind})", cmd)
        return out if p.returncode == 0 else None

    qemu = shutil.which("qemu-aarch64-static") or shutil.which("qemu-aarch64")
    if not qemu:
        print("note: qemu-aarch64 not available, skipping qemu execution tests")

    device = args.device
    if device is not None:
        if not hdc(device, "shell", "echo hdc-ok").stdout.strip().endswith("hdc-ok"):
            c.check(f"hdc reaches device '{device or '<default>'}'", False)
            device = None
    else:
        print("note: no --device, skipping on-device execution tests")

    try:
        for src, flags in tests:
            marker = f"{src.stem}-ok"
            # Primary target: aarch64. Static binaries run under qemu-user.
            a64_static = build(src, flags, "aarch64-linux-ohos", static=True)
            a64_shared = build(src, flags, "aarch64-linux-ohos", static=False)
            if qemu and a64_static:
                p = subprocess.run([qemu, str(a64_static)], capture_output=True, text=True)
                c.check(
                    f"{src.name}: static aarch64 binary runs under qemu",
                    p.returncode == 0 and marker in p.stdout,
                    (p.stdout + p.stderr).strip(),
                )
            # Device / emulator: real OpenHarmony userland. The shared run is
            # the only place the dynamic-loader path (toolchain libc++.so
            # bundled, correct flavor) gets tested.
            if device is None:
                continue
            triple = args.device_target
            if triple == "aarch64-linux-ohos":
                dev_static, dev_shared = a64_static, a64_shared
            else:
                dev_static = build(src, flags, triple, static=True)
                dev_shared = build(src, flags, triple, static=False)
            if dev_static:
                ok, out = device_run(device, dev_static, marker)
                c.check(f"{src.name}: static binary runs on device", ok, out)
            if dev_shared:
                lib = clang_dir / "lib" / triple / "libc++.so" if src.suffix == ".cpp" else None
                ok, out = device_run(device, dev_shared, marker, lib)
                c.check(f"{src.name}: shared binary runs on device", ok, out)
    finally:
        if device is not None:
            hdc(device, "shell", f"rm -rf {DEVICE_DIR}")


def cmd_fetch(args):
    manifest = load_manifest()
    entry = manifest["toolchains"].get(args.version)
    if entry is None:
        sys.exit(f"error: version '{args.version}' not in toolchains.json")
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    sums = []
    for host in entry["hosts"].values():
        for pkg in ("clang", "libcxx_ndk"):
            e = host[pkg]
            dest = out / e["file"]
            if not dest.is_file():
                download(f"{manifest['mirror_base']}/{args.version}/{host['dir']}/{e['file']}", dest)
            got = sha256_file(dest)
            if got != e["sha256"]:
                sys.exit(f"error: sha256 mismatch for {e['file']} (got {got})")
            print(f"== sha256 OK: {e['file']}")
            sums.append(f"{e['sha256']}  {e['file']}")
    (out / "SHA256SUMS").write_text("\n".join(sorted(sums)) + "\n")
    print(f"== all archives for {args.version} verified in {out}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="validate one host archive pair")
    v.add_argument("version")
    v.add_argument("host")
    v.add_argument("--full", action="store_true", help="compile/link/run smoke tests against an OHOS sysroot")
    v.add_argument("--write-abi", action="store_true", help="(re)generate abi/<version>/*.syms instead of diffing")
    v.add_argument("--work", help="reuse DIR for downloads/extraction (default: mktemp)")
    v.add_argument("--device", nargs="?", const="", default=None, metavar="CONNECT_KEY",
                   help="also run smoke tests on a device/emulator via hdc "
                        "(optional hdc connect-key; bare flag = sole connected target)")
    v.add_argument("--device-target", default="x86_64-linux-ohos",
                   help="clang triple for on-device tests (default: x86_64-linux-ohos, the emulator)")
    v.add_argument("--ohos-sdk-native", default=None, help="SDK native dir (default: $OHOS_SDK_NATIVE)")
    v.set_defaults(func=cmd_validate)

    f = sub.add_parser("fetch", help="download + verify all archives of a version")
    f.add_argument("version")
    f.add_argument("outdir")
    f.set_defaults(func=cmd_fetch)

    args = parser.parse_args()
    if getattr(args, "ohos_sdk_native", None) is None and args.command == "validate":
        import os
        args.ohos_sdk_native = os.environ.get("OHOS_SDK_NATIVE")
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
