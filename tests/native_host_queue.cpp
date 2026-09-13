#include "../native/host_v2/host_queue.h"
#include <cassert>
int main()
{
    using host_queue::Limit;
    constexpr uint64_t mib = host_queue::kMiB;
    assert(Limit(16, 32*mib, 12*1024*mib, 1536*mib, true) == 16);
    assert(Limit(999, 32*mib, 12*1024*mib, 1536*mib, true) == 16);
    assert(Limit(16, 512*mib, 4096*mib, 1536*mib, true) == 3);
    assert(Limit(16, 512*mib, 1024*mib, 1536*mib, true) == 1);
    assert(Limit(16, 32*mib, 0, 0, false) == 3);
    assert(Limit(2, 32*mib, 0, 0, false) == 2);
    assert(Limit(0, 32*mib, 4096*mib, 0, true) == 1);
    assert(Limit(16, 0, 4096*mib, 0, true) == 1);
}
