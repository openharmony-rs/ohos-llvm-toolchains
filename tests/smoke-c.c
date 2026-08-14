/* Baseline C: compiles, links against the OHOS sysroot, atomics work. */
#include <stdio.h>
#include <stdatomic.h>

_Atomic long counter;

int main(void) {
    atomic_fetch_add(&counter, 1);
    if (atomic_load(&counter) != 1) {
        return 1;
    }
    printf("smoke-c-ok\n");
    return 0;
}
