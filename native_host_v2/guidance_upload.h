#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace guidance_upload {

// Preserve the host's existing conversion bit-for-bit, including its rounding,
// subnormal and overflow behavior. Hardware half conversion is not equivalent.
inline uint16_t FloatToHalf(float value)
{
    uint32_t bits = 0;
    memcpy(&bits, &value, sizeof(bits));
    const uint32_t sign = (bits >> 16) & 0x8000u;
    uint32_t mantissa = bits & 0x007FFFFFu;
    int exponent = static_cast<int>((bits >> 23) & 0xFFu) - 127 + 15;
    if (exponent <= 0)
    {
        if (exponent < -10)
            return static_cast<uint16_t>(sign);
        mantissa = (mantissa | 0x00800000u) >> (1 - exponent);
        return static_cast<uint16_t>(sign | ((mantissa + 0x00001000u) >> 13));
    }
    if (exponent >= 31)
        return static_cast<uint16_t>(sign | 0x7C00u);
    return static_cast<uint16_t>(sign | (static_cast<uint32_t>(exponent) << 10) |
                                 ((mantissa + 0x00001000u) >> 13));
}

// Destination is a mapped UPLOAD heap: write only, respect D3D12 RowPitch and
// leave padding untouched. Caller waits for the slot fence before reusing it.
inline void Motion(unsigned char *destination, size_t pitch, size_t width,
                   size_t height, const float *source)
{
    for (size_t row = 0; row < height; ++row)
    {
        auto *output = destination + row * pitch;
        if (source == nullptr)
            memset(output, 0, width * 4);
        else
        {
            // Volatile stores prevent read-modify-write optimizations on
            // write-combined upload memory. Never read back this destination.
            auto *half = reinterpret_cast<volatile uint16_t *>(output);
            const float *input = source + row * width * 2;
            for (size_t index = 0; index < width * 2; ++index)
                half[index] = FloatToHalf(input[index]);
        }
    }
}

inline void Depth(unsigned char *destination, size_t pitch, size_t width,
                  size_t height, const float *source)
{
    for (size_t row = 0; row < height; ++row)
    {
        auto *output = destination + row * pitch;
        if (source == nullptr)
            memset(output, 0, width * sizeof(float));
        else
            memcpy(output, source + row * width, width * sizeof(float));
    }
}

} // namespace guidance_upload
