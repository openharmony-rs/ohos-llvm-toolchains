# Fallback: building OHOS runtimes from upstream llvm-project

The primary path of this repository is consuming the OpenHarmony fork's
prebuilt toolchains. This recipe is the documented fallback for cases the
fork's prebuilts don't cover:

- a host platform missing from a preview release (e.g. Windows for 19.1.4),
- exact LLVM-major matching against rustc for cross-language LTO
  (the fork skipped LLVM 20; rustc 1.88 is LLVM 20),
- a preview being pulled from the mirror,
- needing a stock upstream compiler for any other reason.

It builds libc++ / libc++abi / libunwind / compiler-rt builtins for
`aarch64-linux-ohos` from an upstream llvm-project release, against the OHOS
SDK sysroot. The compiler itself stays a stock upstream release binary.

Note: upstream libc++ uses ABI namespace `__1`, distinct from both the SDK's
`__n1` and the fork-19 toolchain's `__h` — so it cannot ODR-collide with
either, but binaries built this way need *this* `libc++.so.1` bundled.

```sh
LLVM=$PWD/llvm-release            # extracted upstream LLVM release (host)
SRC=$PWD/llvm-project-20.1.8.src  # matching llvm-project source tarball
SYSROOT=$OHOS_SDK_NATIVE/sysroot
DEST=$PWD/ohos-runtimes

cmake -S "$SRC/runtimes" -B build-rt \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER="$LLVM/bin/clang" \
  -DCMAKE_CXX_COMPILER="$LLVM/bin/clang++" \
  -DCMAKE_ASM_COMPILER="$LLVM/bin/clang" \
  -DCMAKE_AR="$LLVM/bin/llvm-ar" -DCMAKE_RANLIB="$LLVM/bin/llvm-ranlib" \
  -DCMAKE_C_COMPILER_TARGET=aarch64-linux-ohos \
  -DCMAKE_CXX_COMPILER_TARGET=aarch64-linux-ohos \
  -DCMAKE_ASM_COMPILER_TARGET=aarch64-linux-ohos \
  -DCMAKE_SYSROOT="$SYSROOT" \
  -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64 \
  -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
  -DLLVM_ENABLE_RUNTIMES="compiler-rt;libunwind;libcxxabi;libcxx" \
  -DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=ON \
  -DLIBCXX_ENABLE_SHARED=ON -DLIBCXXABI_ENABLE_SHARED=OFF \
  -DLIBUNWIND_ENABLE_SHARED=OFF \
  -DLIBCXX_STATICALLY_LINK_ABI_IN_SHARED_LIBRARY=ON \
  -DLIBCXX_CXX_ABI=libcxxabi -DLIBCXXABI_USE_LLVM_UNWINDER=ON \
  -DLIBCXX_HAS_MUSL_LIBC=ON \
  -DLIBCXX_INCLUDE_BENCHMARKS=OFF -DLIBCXX_INCLUDE_TESTS=OFF \
  -DLIBCXX_USE_COMPILER_RT=ON -DLIBCXXABI_USE_COMPILER_RT=ON \
  -DLIBUNWIND_USE_COMPILER_RT=ON \
  -DLIBCXX_HAS_ATOMIC_LIB=OFF -DLIBCXX_HAS_GCC_LIB=OFF \
  -DLIBCXX_HAS_GCC_S_LIB=OFF \
  -DLIBCXXABI_HAS_CXA_THREAD_ATEXIT_IMPL=OFF \
  -DCOMPILER_RT_BUILD_BUILTINS=ON -DCOMPILER_RT_BUILD_SANITIZERS=OFF \
  -DCOMPILER_RT_BUILD_XRAY=OFF -DCOMPILER_RT_BUILD_LIBFUZZER=OFF \
  -DCOMPILER_RT_BUILD_PROFILE=OFF -DCOMPILER_RT_BUILD_MEMPROF=OFF \
  -DCOMPILER_RT_BUILD_ORC=OFF -DCOMPILER_RT_BUILD_CTX_PROFILE=OFF \
  -DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON \
  -DCMAKE_INSTALL_PREFIX="$DEST"
cmake --build build-rt -j"$(nproc)"
cmake --install build-rt
```

Why the unusual flags (all discovered the hard way; every one is needed):

- `CMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY` lets configure run without
  a linkable sysroot setup — but it makes cmake's library-exists probes
  false-positive, which is why the next group pins their answers:
- `LIBCXX_HAS_ATOMIC_LIB/GCC_LIB/GCC_S_LIB=OFF`: without these the shared
  libc++ links phantom `-latomic` / `-lgcc`.
- `*_USE_COMPILER_RT=ON`: otherwise the runtimes try to link libgcc.
- `LIBCXXABI_HAS_CXA_THREAD_ATEXIT_IMPL=OFF`: glibc extension the OHOS musl
  does not export; libc++abi falls back to its own implementation.
- `LLVM_ENABLE_PER_TARGET_RUNTIME_DIR=ON`: clang ≥ 16 drivers look for
  builtins in `lib/<triple>/libclang_rt.builtins.a`; the legacy
  `lib/linux/*-aarch64` layout will not be found.
- `LIBCXX_STATICALLY_LINK_ABI_IN_SHARED_LIBRARY=ON`: one self-contained
  `libc++.so.1` to bundle.

Consume with (flags folded into the compiler value, see README):

```
--target=aarch64-linux-ohos --sysroot=$SYSROOT
-resource-dir=$DEST/lib/clang-resource-mirror   # or clang's own + -L below
-nostdinc++ -isystem$DEST/include/c++/v1
-L$DEST/lib
```

and bundle `$DEST/lib/libc++.so.1` with the app. When packaging, dereference
symlinks (`libc++.so.1` is a symlink to `libc++.so.1.0` in the install tree).
