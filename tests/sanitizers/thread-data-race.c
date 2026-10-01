/* ThreadSanitizer: the runtime loads, reports a data race, and the program
   still runs to completion afterwards. */
// ohos-expect: WARNING: ThreadSanitizer: data race
// ohos-expect: thread-data-race-done
#include <pthread.h>
#include <stdio.h>

static int counter;

static void *increment(void *arg) {
    counter++;
    return arg;
}

int main(void) {
    pthread_t a, b;
    pthread_create(&a, 0, increment, 0);
    pthread_create(&b, 0, increment, 0);
    pthread_join(a, 0);
    pthread_join(b, 0);
    printf("thread-data-race-done\n");
    return 0;
}
