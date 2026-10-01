/* AddressSanitizer: the runtime loads and reports a heap overflow. */
// ohos-expect: AddressSanitizer: heap-buffer-overflow
#include <stdlib.h>

int main(void) {
    volatile int index = 4;
    int *values = malloc(4 * sizeof(int));
    values[index] = 1;
    free(values);
    return 0;
}
