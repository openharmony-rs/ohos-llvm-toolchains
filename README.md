# ohos-llvm-toolchains

Pinned, validated, and mirrored [OpenHarmony LLVM
fork](https://gitcode.com/openharmony/third_party_llvm-project) prebuilt
toolchains, for cross-compiling to OpenHarmony with a clang newer than the
one in the released SDKs (15.0.4).

The OpenHarmony project publishes full cross-toolchain archives (host clang +
OHOS-target libc++/libunwind/compiler-rt) as plain downloads on
`repo.huaweicloud.com/openharmony/compiler/clang/`. Newer-than-SDK versions
(16.0.0, 18.1.8, 19.1.4/19.1.7 as of mid-2026) exist only as **previews**:
they are not referenced by any official release, and nothing guarantees the
archives stay on the mirror. This repository:

- **pins** specific toolchain builds by URL + sha256 in
  [`toolchains.json`](toolchains.json),
- **validates** them in CI (checksums, layout, libc++ ABI namespace, an
  exported-symbol ABI snapshot, and compile/link/run smoke tests against a
  real OHOS sysroot — executed both under qemu-user and on a booted
  OpenHarmony system, the [Oniro emulator](https://github.com/eclipse-oniro4openharmony/device_board_oniro)),
- **mirrors** them into GitHub releases of this repository, so consumers
  don't depend on the upstream mirror's retention policy.

Where no prebuilt is good enough, it also **builds** a toolchain from the fork's
source, see [Toolchains built from source](#toolchains-built-from-source).

## Pinned toolchains

| Version | LLVM | Status | Hosts | OHOS targets |
|---|---|---|---|---|
| `19.1.4-79830f` | 19.1.4 (2026‑02‑23) | preview | linux‑x86_64, darwin‑arm64, darwin‑x86_64, windows‑x86_64 | aarch64, arm, x86_64 |
| `21.1.8-7fda98` | 21.1.8 (2026‑09‑17) | built from source, patched | linux‑x86_64 | aarch64, arm, x86_64 |

Archives per host: `clang_*` (the toolchain: host clang/lld/llvm-* plus
OHOS-target runtimes and full libc++ headers) and `libcxx-ndk_*` (see below).

## The two libc++ flavors

Each toolchain release carries **one libc++ build in two ABI-namespace
configurations** (verified for 19.1.4: identical `_LIBCPP_VERSION`, identical
exported-symbol sets modulo namespace):

| | ABI namespace | headers | library |
|---|---|---|---|
| toolchain (`clang_*` archive) | `__h` | full tree, found automatically by the bundled clang | `lib/<triple>/libc++.so` |
| ndk (`libcxx-ndk_*` archive) | `__n1` | **only** `__config`/`__config_site` — an overlay over the toolchain headers | `lib/<triple>/libc++.so` |

Rules that follow:

- **Bundle the flavor whose headers you compiled against.** Mixing them fails
  at load time with missing-symbol errors (namespaces are distinct symbols).
- The ndk flavor's `__n1` matches the namespace of the SDK-15 era
  `libc++_shared.so`, for continuity with existing app-ecosystem binaries —
  but it is **not** a bit-perfect superset of the old ABI: vs the SDK 15
  runtime, 19.1.4's `__n1` library drops ~24 symbols (tag objects such as
  `std::adopt_lock` that became inline constexpr, and removed internal
  instantiations) while adding ~50. Old binaries referencing a dropped symbol
  would fail against it.
- The distinct namespaces (`__1` upstream, `__n1` ndk, `__h` toolchain) mean
  different-flavor runtimes can coexist in one process without ODR
  collisions; C++ types still must not cross a flavor boundary.

## Using a toolchain

Unpack both archives of a release. The toolchain is SDK-shaped (`bin/`,
`lib/<triple>/`, bundled libc++ headers found automatically by its clang).
Cross-compile with:

```
<toolchain>/bin/clang++ --target=aarch64-linux-ohos --sysroot=$OHOS_SDK_NATIVE/sysroot ...
```

Notes:

1. **Fold `--target`/`--sysroot` into the `CC`/`CXX` value** when driving
   build systems that test commandline flags (e.g. autoconf).
   On Linux / macOS a compiler wrapper like `aarch64-unknown-linux-ohos-clang` 
   is also a good solution to ensure the compiler is configured correctly.
2. The sysroot still comes from a regular OpenHarmony SDK; only the
   `llvm/` part is replaced by this toolchain.

[cargo-ohos](https://github.com/openharmony-rs/cargo-ohos) downloads,
verifies and uses a release of this repository with
`--download-prebuilt <version>` (e.g. `--download-prebuilt=21`).


## Toolchains built from source

Entries with a `source` object in `toolchains.json` are not mirrored but built
by [`scripts/build_toolchain.py`](scripts/build_toolchain.py) from the fork at
the pinned commit. The reason is the sanitizers: no published prebuilt has
AddressSanitizer and ThreadSanitizer runtimes that work on OpenHarmony (19.1.4's
reference Android's `__errno` and place ASan's allocator outside a 39-bit
address space; the fixes, and the ThreadSanitizer report fixes of 2026-08-24,
are only on the fork's branches).

The build is reduced to what [cargo-ohos](https://github.com/openharmony-rs/cargo-ohos)
uses, in the layout of the fork's prebuilts:

- clang, lld, the LLVM binutils (`llvm-ar`, `llvm-objcopy`, `llvm-readelf`,
  ...) and `libclang`, for a **linux-x86_64** host only, built in a
  `manylinux_2_28` container so that they need glibc 2.28 at most;
- per target (`aarch64`, `arm` with the default soft-float multilib only,
  `x86_64`): compiler-rt (builtins, crt objects, ASan, TSan, UBSan) and the
  toolchain flavour of libc++ (`__h`), built against the sysroot of the
  OpenHarmony 6.0 (API 20) SDK.

Not included, unlike the fork's prebuilts: the ndk libc++ flavour, lldb,
clang-tools-extra, OpenMP, the other `arm` multilibs, and gwp_asan (which
needs OpenHarmony's platform sysroot). There is no LTO or PGO, so the
compiler itself is somewhat slower.

### Patches

`patches/<version>/` holds the changes applied on top of the fork, listed
under `source.patches`. They are **not part of the OpenHarmony LLVM fork**:
each says so in its message, the release notes list them, and the archive
carries them in `llvm/patches/` next to a `BUILD_INFO.json` naming the fork
commit. A patch belongs upstream; it stays here only until the fork has it.

- `21.1.8-7fda98/0001`: ThreadSanitizer restores all of its signal-handler
  state when a handler `longjmp`s out. Without it, the first report on a
  device without SHA-512 instructions corrupts the shadow stack (OpenSSL's
  CPU feature probe in the reporting path does exactly that) and the process
  crashes after the report.

### Building one locally

```sh
V=21.1.8-7fda98
scripts/build_toolchain.py source "$V" build/src
scripts/build_toolchain.py host "$V" build/src build/work --linker lld
OHOS_SDK_NATIVE=<sdk>/native scripts/build_toolchain.py target "$V" build/src build/work \
  aarch64-linux-ohos arm-linux-ohos x86_64-linux-ohos
scripts/build_toolchain.py package "$V" build/src build/work dist
scripts/ohos_toolchain.py validate "$V" linux-x86_64 --archive dist/clang_*.tar.gz --full --device
```

The host stage takes about 3 CPU-hours (8 minutes on 32 cores), the target
stage a few CPU-minutes. CI builds and validates every source-built version on
pull requests that touch the build (`build.yml`), and again in the *Mirror
release* workflow before publishing.

## Adding or bumping a toolchain version

1. Add the entry to `toolchains.json` (copy the sha256 values from the
   mirror's `.sha256` files — do not compute them from your own download
   alone). For a toolchain built from source, give the fork commit and
   patches under `source` instead, and only the archive name per host.
2. Generate ABI snapshots: `scripts/ohos_toolchain.py validate <version>
   <host> --write-abi` on a matching host, and commit `abi/<version>/`.
3. Open a PR; CI validates every host.
4. After merge, run the *Mirror release* workflow for the new version.
5. Treat any ABI-snapshot diff or namespace change relative to the previous
   pinned version as a compatibility decision to make explicitly, not a CI
   nuisance — consumers ship these runtimes inside apps.

## License

The scripts and configuration in this repository are licensed under MIT OR Apache-2.0. 
The toolchain archives are built from the OpenHarmony LLVM fork of the LLVM
project and retain their own license (Apache-2.0 WITH LLVM-exception).
Mirrored archives are redistributed unmodified; the ones built from source
here contain the patches in `patches/`.
