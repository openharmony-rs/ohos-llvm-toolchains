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

## Pinned toolchains

| Version | LLVM | Status | Hosts | OHOS targets |
|---|---|---|---|---|
| `19.1.4-79830f` | 19.1.4 (2026‑02‑23) | preview | linux‑x86_64, darwin‑arm64, darwin‑x86_64, windows‑x86_64 | aarch64, arm, x86_64 |

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

`cargo-ohos` integration (an external-toolchain mode building on its
inline-flags support) is planned; until then the env can be assembled
manually as above.


## Adding or bumping a toolchain version

1. Add the entry to `toolchains.json` (copy the sha256 values from the
   mirror's `.sha256` files — do not compute them from your own download
   alone).
2. Generate ABI snapshots: `scripts/ohos_toolchain.py validate <version>
   <host> --write-abi` on a matching host, and commit `abi/<version>/`.
3. Open a PR; CI validates every host.
4. After merge, run the *Mirror release* workflow for the new version.
5. Treat any ABI-snapshot diff or namespace change relative to the previous
   pinned version as a compatibility decision to make explicitly, not a CI
   nuisance — consumers ship these runtimes inside apps.

## License

The scripts and configuration in this repository are licensed under MIT OR Apache-2.0. 
The mirrored toolchain archives are built from the OpenHarmony LLVM fork of the LLVM
project and retain their own license (Apache-2.0 WITH LLVM-exception);
this repository redistributes them unmodified.
