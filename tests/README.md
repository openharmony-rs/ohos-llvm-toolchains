# Toolchain smoke tests

Each `.c` / `.cpp` file in this directory is one test. The validator
(`scripts/ohos_toolchain.py validate --full`) discovers them automatically
and, for every test:

- compiles and links it **statically and against the shared libc++** for the
  primary target (`aarch64-linux-ohos`) and, when `--device` is given, for
  the device target (default `x86_64-linux-ohos`, the Oniro emulator);
- runs the static aarch64 binary under qemu-user when available;
- runs the static *and* shared device binaries on the device/emulator via
  hdc — the shared one with the toolchain's own `libc++.so` pushed
  alongside, exercising the dynamic-loader path.

The two execution environments are complementary, not redundant: the Oniro
emulator provides a **real OHOS userland** (musl loader, system libs) but
only ships x86_64 images, while qemu-user is the only place **aarch64** —
the shipping target — actually executes (static binaries, no OHOS userland).
If an aarch64 OHOS emulator image becomes available, the qemu-user path
should be replaced by it.

## Conventions

- A test **must print a line containing `<filename-stem>-ok`** and exit 0 on
  success (e.g. `smoke-cxx23.cpp` prints `smoke-cxx23-ok`). Failures should
  exit non-zero.
- Extra compile flags go in a comment directive within the first ten lines:

  ```c
  // ohos-flags: -std=c++23
  ```

- `.c` files are compiled with `clang`, `.cpp` files with `clang++`.
- Keep tests small and targeted: each should pin one capability the pinned
  toolchain exists to provide (a language/library feature, a runtime
  behavior), so a failure names the regression.
