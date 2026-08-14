// ohos-flags: -std=c++23
// This is expected to fail on the SDKs clang-15, but should compile with newer clang versions (tested 19+).
#include <expected>
#include <print>
#include <atomic>
#include <algorithm>
#include <ranges>

int main() {
    std::atomic<double> d{1.5};
    d.fetch_add(2.5);
    std::expected<int, int> e{7};
    auto v = {3, 1, 2};
    auto m = std::ranges::lower_bound(v, 2);
    if (d.load() != 4.0 || *e != 7 || *m != 2) {
        return 1;
    }
    std::println("smoke-cxx23-ok {} {} {}", d.load(), *e, *m);
    return 0;
}
