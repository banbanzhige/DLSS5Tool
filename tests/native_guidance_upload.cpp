#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <random>
#include <stdexcept>
#include <vector>
#include "../native/host_v2/guidance_upload.h"

// Independent copy of the old host conversion, deliberately not calling the
// production helper: catches changed rounding/special-value behavior.
uint16_t OriginalHalf(float value)
{
    uint32_t bits; memcpy(&bits, &value, 4);
    const uint32_t sign = (bits >> 16) & 0x8000u;
    uint32_t mantissa = bits & 0x007fffffu;
    int exponent = int((bits >> 23) & 255u) - 112;
    if (exponent <= 0) {
        if (exponent < -10) return uint16_t(sign);
        mantissa = (mantissa | 0x00800000u) >> (1 - exponent);
        return uint16_t(sign | ((mantissa + 0x1000u) >> 13));
    }
    if (exponent >= 31) return uint16_t(sign | 0x7c00u);
    return uint16_t(sign | (uint32_t(exponent) << 10) | ((mantissa + 0x1000u) >> 13));
}

void OriginalMotion(unsigned char *out, size_t pitch, size_t w, size_t h, const float *src)
{
    std::vector<uint16_t> half(w * h * 2, 0);
    if (src) for (size_t i = 0; i < half.size(); ++i) half[i] = OriginalHalf(src[i]);
    for (size_t y = 0; y < h; ++y) memcpy(out + y * pitch, half.data() + y * w * 2, w * 4);
}
void OriginalDepth(unsigned char *out, size_t pitch, size_t w, size_t h, const float *src)
{
    std::vector<float> depth(w * h, 0);
    if (src) memcpy(depth.data(), src, depth.size() * 4);
    for (size_t y = 0; y < h; ++y) memcpy(out + y * pitch, depth.data() + y * w, w * 4);
}

void Require(bool value) { if (!value) throw std::runtime_error("upload regression"); }
void Check()
{
    std::mt19937 random(771);
    const uint32_t special[] = {0,0x80000000,1,0x007fffff,0x00800000,0x33800000,
        0x387fffff,0x38800000,0x477fe000,0x47800000,0x7f800000,0xff800000,0x7fc00000,0xffffffff};
    for (size_t width : {size_t(1),size_t(63),size_t(64),size_t(65),size_t(1440),size_t(1919)}) {
        const size_t height = 9, pitch = ((width * 4 + 255) / 256) * 256, offset = 512;
        std::vector<float> input(width * height * 2);
        for (size_t i = 0; i < input.size(); ++i) {
            uint32_t bits = i < std::size(special) ? special[i] : random();
            memcpy(&input[i], &bits, 4);
        }
        for (bool null : {false, true}) {
            std::vector<unsigned char> a(offset + pitch * height + 512, 0xcd), b(a);
            auto source = null ? nullptr : input.data();
            OriginalMotion(a.data() + offset, pitch, width, height, source);
            guidance_upload::Motion(b.data() + offset, pitch, width, height, source);
            Require(a == b); // Includes untouched padding and surrounding canaries.
            OriginalDepth(a.data() + offset, pitch, width, height, source);
            guidance_upload::Depth(b.data() + offset, pitch, width, height, source);
            Require(a == b);
        }
    }
    for (size_t i = 0; i < 1000000; ++i) {
        uint32_t bits = random(); float value; memcpy(&value, &bits, 4);
        Require(OriginalHalf(value) == guidance_upload::FloatToHalf(value));
    }
    puts("correctness: padding/offset/null/specials/1000000-random-floats PASS");
}

int main()
{
    try {
        Check();
        IDXGIFactory4 *factory = nullptr; ID3D12Device *device = nullptr;
        Require(SUCCEEDED(CreateDXGIFactory1(IID_PPV_ARGS(&factory))));
        for (UINT index = 0; !device; ++index) {
            IDXGIAdapter1 *adapter = nullptr;
            if (factory->EnumAdapters1(index, &adapter) == DXGI_ERROR_NOT_FOUND) break;
            DXGI_ADAPTER_DESC1 desc{}; adapter->GetDesc1(&desc);
            if (desc.VendorId == 0x10de && !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE))
                D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device));
            adapter->Release();
        }
        factory->Release(); Require(device != nullptr);
        const size_t w = 1440, h = 1440, pitch = ((w * 4 + 255) / 256) * 256;
        D3D12_HEAP_PROPERTIES heap{}; heap.Type = D3D12_HEAP_TYPE_UPLOAD;
        D3D12_RESOURCE_DESC desc{}; desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        desc.Width = pitch * h; desc.Height = 1; desc.DepthOrArraySize = 1;
        desc.MipLevels = 1; desc.SampleDesc.Count = 1; desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        ID3D12Resource *resource = nullptr;
        Require(SUCCEEDED(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
            D3D12_RESOURCE_STATE_GENERIC_READ, nullptr, IID_PPV_ARGS(&resource))));
        unsigned char *mapped = nullptr; D3D12_RANGE read{0,0};
        Require(SUCCEEDED(resource->Map(0, &read, reinterpret_cast<void **>(&mapped))));
        std::vector<float> input(w * h * 2);
        for (size_t i = 0; i < input.size(); ++i) input[i] = float(int(i % 2048) - 1024) / 31;
        using Fn = void (*)(unsigned char *, size_t, size_t, size_t, const float *);
        const Fn functions[] = {OriginalMotion, guidance_upload::Motion, OriginalDepth, guidance_upload::Depth};
        const char *names[] = {"old_motion", "direct_motion", "old_depth", "direct_depth"};
        // Alternate order, real mapped upload heap; never benchmark CPU reads.
        for (int round = 0; round < 2; ++round) for (int order = 0; order < 4; ++order) {
            int i = round ? 3 - order : order;
            for (int j = 0; j < 4; ++j) functions[i](mapped, pitch, w, h, input.data());
            auto start = std::chrono::steady_clock::now();
            for (int j = 0; j < 50; ++j) functions[i](mapped, pitch, w, h, input.data());
            const double ms = std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-start).count()/50;
            printf("{\"round\":%d,\"method\":\"%s\",\"mean_ms\":%.6f}\n",round,names[i],ms);
        }
        resource->Unmap(0,nullptr); resource->Release(); device->Release();
        return 0;
    } catch (const std::exception &error) { fprintf(stderr,"%s\n",error.what()); return 1; }
}
