#pragma once
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <stdexcept>
#include <vector>

// Independent overlap-add implementation. Context is discarded BEFORE blending;
// coordinates refer to the original frame, not a resized per-tile canvas.
namespace tile_blend {
struct Region {
    unsigned start, size, begin, end;
    std::vector<float> weights;
};

inline std::vector<Region> Plan(unsigned length, unsigned requested,
                                unsigned context = 128, unsigned overlap = 128)
{
    if (!length || !requested) throw std::invalid_argument("Empty tile axis");
    const unsigned size = std::min(length, requested);
    context = std::min(context, size / 8);
    overlap = std::max(1u, std::min(overlap, size / 4));
    const unsigned stride = std::max(1u, size - 2 * context - overlap);
    std::vector<Region> result;
    for (unsigned start = 0;;) {
        result.push_back({start, size, start == 0 ? 0 : start + context,
                          start + size == length ? length : start + size - context, {}});
        if (start + size == length) break;
        start = std::min(start + stride, length - size);
    }
    std::vector<float> sums(length, 0.0f);
    for (size_t i = 0; i < result.size(); ++i) {
        auto &r = result[i];
        r.weights.resize(r.end - r.begin);
        for (unsigned p = r.begin; p < r.end; ++p) {
            float weight = 1.0f;
            auto ramp = [](float t) { return 0.5f - 0.5f * std::cos(3.14159265358979323846f * t); };
            if (i && p < result[i - 1].end)
                weight *= ramp(float(p - r.begin + 1) / float(result[i - 1].end - r.begin + 1));
            if (i + 1 < result.size() && p >= result[i + 1].begin)
                weight *= ramp(float(r.end - p) / float(r.end - result[i + 1].begin + 1));
            r.weights[p - r.begin] = weight;
            sums[p] += weight;
        }
    }
    for (auto &r : result)
        for (unsigned p = r.begin; p < r.end; ++p)
            r.weights[p - r.begin] /= sums[p];
    return result;
}

// Only one tile-height strip is accumulated, not a full-frame FP32 image.
// Flush rows before advancing the vertical grid; modulo storage then reuses them.
class Rows {
    unsigned width_, height_, next_ = 0;
    std::vector<float> values_;
public:
    Rows(unsigned width, unsigned tile_height)
        : width_(width), height_(tile_height), values_(size_t(width) * tile_height * 4, 0.0f) {}
    template<class Read> void Add(const Region &x, const Region &y, Read read) {
        for (unsigned py = y.begin; py < y.end; ++py) {
            float *row = values_.data() + size_t(py % height_) * width_ * 4;
            const float wy = y.weights[py - y.begin];
            for (unsigned px = x.begin; px < x.end; ++px) {
                const float weight = wy * x.weights[px - x.begin];
                for (unsigned c = 0; c < 4; ++c)
                    row[size_t(px) * 4 + c] += read(px - x.start, py - y.start, c) * weight;
            }
        }
    }
    template<class Write> void Flush(unsigned end, Write write) {
        for (; next_ < end; ++next_) {
            float *row = values_.data() + size_t(next_ % height_) * width_ * 4;
            for (unsigned x = 0; x < width_; ++x)
                for (unsigned c = 0; c < 4; ++c)
                    write(x, next_, c, row[size_t(x) * 4 + c]);
            std::fill(row, row + size_t(width_) * 4, 0.0f);
        }
    }
};
} // namespace tile_blend
