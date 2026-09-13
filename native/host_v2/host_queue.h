#pragma once
#include <algorithm>
#include <cstdint>

namespace host_queue {
constexpr int kMaxSlots = 16;
constexpr uint64_t kMiB = 1024ull * 1024;

// Budget only the new frame slots; leave room for NGX/model/driver allocations.
// Unknown budget retains the historical ceiling instead of allocating 16 blindly.
inline int Limit(int requested, uint64_t per_slot, uint64_t available,
                 uint64_t reserve, bool known)
{
    requested = std::clamp(requested, 1, kMaxSlots);
    if (!known) return std::min(requested, 3);
    if (per_slot == 0 || available <= reserve) return 1;
    const uint64_t slots = ((available - reserve) / 4 * 3) / per_slot;
    return static_cast<int>(std::max<uint64_t>(1, std::min<uint64_t>(requested, slots)));
}
}
