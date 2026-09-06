// SPDX-License-Identifier: MIT
// RTX Video SDK calls are compiled against an SDK installed separately by the developer.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>

#include <nvsdk_ngx.h>
#include <nvsdk_ngx_defs_vsr.h>
#include <nvsdk_ngx_helpers_vsr.h>

#include <algorithm>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

#pragma comment(lib, "d3d12.lib")
#pragma comment(lib, "dxgi.lib")

namespace
{
ID3D12Device *g_device = nullptr;
ID3D12CommandQueue *g_queue = nullptr;
ID3D12CommandAllocator *g_allocator = nullptr;
ID3D12GraphicsCommandList *g_list = nullptr;
ID3D12Fence *g_fence = nullptr;
HANDLE g_fence_event = nullptr;
UINT64 g_fence_value = 0;

ID3D12Resource *g_input = nullptr;
ID3D12Resource *g_output = nullptr;
ID3D12Resource *g_upload = nullptr;
ID3D12Resource *g_readback = nullptr;
D3D12_PLACED_SUBRESOURCE_FOOTPRINT g_input_footprint = {};
D3D12_PLACED_SUBRESOURCE_FOOTPRINT g_output_footprint = {};
UINT64 g_input_staging_size = 0;
UINT64 g_output_staging_size = 0;
unsigned char *g_upload_map = nullptr;
unsigned char *g_readback_map = nullptr;

NVSDK_NGX_Parameter *g_parameters = nullptr;
NVSDK_NGX_Handle *g_feature = nullptr;

UINT g_input_width = 0;
UINT g_input_height = 0;
UINT g_output_width = 0;
UINT g_output_height = 0;
bool g_hdr = false;
unsigned int g_quality = 4;
FILE *g_log = nullptr;

template <typename T>
void Release(T *&value)
{
    if (value != nullptr)
    {
        value->Release();
        value = nullptr;
    }
}

void Log(const char *format, ...)
{
    if (g_log == nullptr)
        return;
    va_list args;
    va_start(args, format);
    vfprintf(g_log, format, args);
    va_end(args);
    fputc('\n', g_log);
    fflush(g_log);
}

D3D12_HEAP_PROPERTIES HeapProperties(D3D12_HEAP_TYPE type)
{
    D3D12_HEAP_PROPERTIES value = {};
    value.Type = type;
    value.CreationNodeMask = 1;
    value.VisibleNodeMask = 1;
    return value;
}

D3D12_RESOURCE_DESC BufferDescription(UINT64 size)
{
    D3D12_RESOURCE_DESC value = {};
    value.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    value.Width = size;
    value.Height = 1;
    value.DepthOrArraySize = 1;
    value.MipLevels = 1;
    value.SampleDesc.Count = 1;
    value.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    return value;
}

ID3D12Resource *CreateTexture(UINT width, UINT height, DXGI_FORMAT format, D3D12_RESOURCE_FLAGS flags)
{
    D3D12_RESOURCE_DESC description = {};
    description.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    description.Width = width;
    description.Height = height;
    description.DepthOrArraySize = 1;
    description.MipLevels = 1;
    description.Format = format;
    description.SampleDesc.Count = 1;
    description.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    description.Flags = flags;
    ID3D12Resource *resource = nullptr;
    const auto heap = HeapProperties(D3D12_HEAP_TYPE_DEFAULT);
    const HRESULT result = g_device->CreateCommittedResource(
        &heap, D3D12_HEAP_FLAG_NONE, &description, D3D12_RESOURCE_STATE_COMMON,
        nullptr, IID_PPV_ARGS(&resource));
    if (FAILED(result))
        Log("CreateTexture %ux%u format=%u failed hr=0x%08X", width, height, format, result);
    return resource;
}

bool CreateStaging(
    ID3D12Resource *texture, D3D12_HEAP_TYPE type, ID3D12Resource **resource,
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT *footprint, UINT64 *total_size, unsigned char **mapped)
{
    UINT rows = 0;
    UINT64 row_size = 0;
    const D3D12_RESOURCE_DESC texture_description = texture->GetDesc();
    g_device->GetCopyableFootprints(
        &texture_description, 0, 1, 0, footprint, &rows, &row_size, total_size);
    const auto heap = HeapProperties(type);
    const auto description = BufferDescription(*total_size);
    const auto state = type == D3D12_HEAP_TYPE_UPLOAD
        ? D3D12_RESOURCE_STATE_GENERIC_READ : D3D12_RESOURCE_STATE_COPY_DEST;
    HRESULT result = g_device->CreateCommittedResource(
        &heap, D3D12_HEAP_FLAG_NONE, &description, state, nullptr, IID_PPV_ARGS(resource));
    if (FAILED(result) || *resource == nullptr)
        return false;
    D3D12_RANGE range = type == D3D12_HEAP_TYPE_UPLOAD
        ? D3D12_RANGE{0, 0} : D3D12_RANGE{0, static_cast<SIZE_T>(*total_size)};
    result = (*resource)->Map(0, &range, reinterpret_cast<void **>(mapped));
    return SUCCEEDED(result) && *mapped != nullptr;
}

D3D12_RESOURCE_BARRIER Transition(
    ID3D12Resource *resource, D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after)
{
    D3D12_RESOURCE_BARRIER barrier = {};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = resource;
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    barrier.Transition.StateBefore = before;
    barrier.Transition.StateAfter = after;
    return barrier;
}

bool BeginCommands()
{
    if (FAILED(g_allocator->Reset()))
        return false;
    return SUCCEEDED(g_list->Reset(g_allocator, nullptr));
}

bool ExecuteAndWait()
{
    if (FAILED(g_list->Close()))
        return false;
    ID3D12CommandList *lists[] = {g_list};
    g_queue->ExecuteCommandLists(1, lists);
    const UINT64 value = ++g_fence_value;
    if (FAILED(g_queue->Signal(g_fence, value)))
        return false;
    if (g_fence->GetCompletedValue() < value)
    {
        ResetEvent(g_fence_event);
        if (FAILED(g_fence->SetEventOnCompletion(value, g_fence_event)))
            return false;
        if (WaitForSingleObject(g_fence_event, INFINITE) != WAIT_OBJECT_0)
            return false;
    }
    return true;
}

void RecordUpload()
{
    auto to_copy = Transition(g_input, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
    g_list->ResourceBarrier(1, &to_copy);
    D3D12_TEXTURE_COPY_LOCATION source = {};
    source.pResource = g_upload;
    source.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    source.PlacedFootprint = g_input_footprint;
    D3D12_TEXTURE_COPY_LOCATION destination = {};
    destination.pResource = g_input;
    destination.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    g_list->CopyTextureRegion(&destination, 0, 0, 0, &source, nullptr);
    auto to_common = Transition(g_input, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COMMON);
    g_list->ResourceBarrier(1, &to_common);
}

void RecordReadback()
{
    auto to_copy = Transition(g_output, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
    g_list->ResourceBarrier(1, &to_copy);
    D3D12_TEXTURE_COPY_LOCATION source = {};
    source.pResource = g_output;
    source.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    D3D12_TEXTURE_COPY_LOCATION destination = {};
    destination.pResource = g_readback;
    destination.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    destination.PlacedFootprint = g_output_footprint;
    g_list->CopyTextureRegion(&destination, 0, 0, 0, &source, nullptr);
    auto to_common = Transition(g_output, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
    g_list->ResourceBarrier(1, &to_common);
}

float HalfToFloat(uint16_t half)
{
    const uint32_t sign = static_cast<uint32_t>(half & 0x8000u) << 16;
    int exponent = static_cast<int>((half >> 10) & 0x1Fu);
    uint32_t mantissa = half & 0x03FFu;
    uint32_t bits = 0;
    if (exponent == 0)
    {
        if (mantissa == 0)
            bits = sign;
        else
        {
            exponent = 1;
            while ((mantissa & 0x0400u) == 0)
            {
                mantissa <<= 1;
                --exponent;
            }
            mantissa &= 0x03FFu;
            bits = sign | (static_cast<uint32_t>(exponent + 112) << 23) | (mantissa << 13);
        }
    }
    else if (exponent == 31)
        bits = sign | 0x7F800000u | (mantissa << 13);
    else
        bits = sign | (static_cast<uint32_t>(exponent + 112) << 23) | (mantissa << 13);
    float value = 0.0f;
    memcpy(&value, &bits, sizeof(value));
    return value;
}

uint16_t FloatToHalf(float value)
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
    return static_cast<uint16_t>(
        sign | (static_cast<uint32_t>(exponent) << 10) | ((mantissa + 0x00001000u) >> 13));
}

uint32_t Pack10(float r, float g, float b)
{
    const auto channel = [](float value) -> uint32_t {
        value = std::clamp(value, 0.0f, 1.0f);
        return static_cast<uint32_t>(value * 1023.0f + 0.5f);
    };
    return channel(r) | (channel(g) << 10) | (channel(b) << 20) | (3u << 30);
}

void CopyInput(const void *input)
{
    const size_t row_bytes = static_cast<size_t>(g_input_width) * 4;
    auto *destination = g_upload_map + g_input_footprint.Offset;
    if (!g_hdr)
    {
        const auto *source = static_cast<const unsigned char *>(input);
        for (UINT y = 0; y < g_input_height; ++y)
            memcpy(destination + static_cast<size_t>(y) * g_input_footprint.Footprint.RowPitch,
                   source + static_cast<size_t>(y) * row_bytes, row_bytes);
        return;
    }
    const auto *source = static_cast<const uint16_t *>(input);
    for (UINT y = 0; y < g_input_height; ++y)
    {
        auto *row = reinterpret_cast<uint32_t *>(
            destination + static_cast<size_t>(y) * g_input_footprint.Footprint.RowPitch);
        for (UINT x = 0; x < g_input_width; ++x)
        {
            const size_t index = (static_cast<size_t>(y) * g_input_width + x) * 4;
            row[x] = Pack10(HalfToFloat(source[index]), HalfToFloat(source[index + 1]),
                            HalfToFloat(source[index + 2]));
        }
    }
}

void CopyOutput(void *output)
{
    const auto *source = g_readback_map + g_output_footprint.Offset;
    const size_t row_bytes = static_cast<size_t>(g_output_width) * 4;
    if (!g_hdr)
    {
        auto *destination = static_cast<unsigned char *>(output);
        for (UINT y = 0; y < g_output_height; ++y)
            memcpy(destination + static_cast<size_t>(y) * row_bytes,
                   source + static_cast<size_t>(y) * g_output_footprint.Footprint.RowPitch, row_bytes);
        return;
    }
    auto *destination = static_cast<uint16_t *>(output);
    const uint16_t one = FloatToHalf(1.0f);
    for (UINT y = 0; y < g_output_height; ++y)
    {
        const auto *row = reinterpret_cast<const uint32_t *>(
            source + static_cast<size_t>(y) * g_output_footprint.Footprint.RowPitch);
        for (UINT x = 0; x < g_output_width; ++x)
        {
            const uint32_t pixel = row[x];
            const size_t index = (static_cast<size_t>(y) * g_output_width + x) * 4;
            destination[index] = FloatToHalf(static_cast<float>(pixel & 0x3FFu) / 1023.0f);
            destination[index + 1] = FloatToHalf(static_cast<float>((pixel >> 10) & 0x3FFu) / 1023.0f);
            destination[index + 2] = FloatToHalf(static_cast<float>((pixel >> 20) & 0x3FFu) / 1023.0f);
            destination[index + 3] = one;
        }
    }
}

bool CreateDevice()
{
    IDXGIFactory6 *factory = nullptr;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory))))
        return false;
    IDXGIAdapter1 *selected = nullptr;
    for (UINT index = 0;; ++index)
    {
        IDXGIAdapter1 *adapter = nullptr;
        HRESULT result = factory->EnumAdapterByGpuPreference(
            index, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, IID_PPV_ARGS(&adapter));
        if (result == DXGI_ERROR_NOT_FOUND)
            break;
        if (FAILED(result))
            continue;
        DXGI_ADAPTER_DESC1 description = {};
        adapter->GetDesc1(&description);
        if (description.VendorId == 0x10DE &&
            !(description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) &&
            SUCCEEDED(D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&g_device))))
        {
            selected = adapter;
            Log("adapter: %ls", description.Description);
            break;
        }
        adapter->Release();
    }
    Release(selected);
    Release(factory);
    if (g_device == nullptr)
        return false;

    D3D12_COMMAND_QUEUE_DESC queue_description = {};
    queue_description.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    if (FAILED(g_device->CreateCommandQueue(&queue_description, IID_PPV_ARGS(&g_queue))) ||
        FAILED(g_device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&g_allocator))) ||
        FAILED(g_device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, g_allocator, nullptr,
                                           IID_PPV_ARGS(&g_list))) ||
        FAILED(g_device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&g_fence))))
        return false;
    g_fence_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (g_fence_event == nullptr || FAILED(g_list->Close()))
        return false;
    return true;
}

void Shutdown()
{
    if (g_queue != nullptr && g_fence != nullptr)
    {
        const UINT64 value = ++g_fence_value;
        if (SUCCEEDED(g_queue->Signal(g_fence, value)) && g_fence->GetCompletedValue() < value)
        {
            g_fence->SetEventOnCompletion(value, g_fence_event);
            WaitForSingleObject(g_fence_event, 10000);
        }
    }
    if (g_feature != nullptr)
    {
        NVSDK_NGX_D3D12_ReleaseFeature(g_feature);
        g_feature = nullptr;
    }
    if (g_parameters != nullptr)
    {
        NVSDK_NGX_D3D12_DestroyParameters(g_parameters);
        g_parameters = nullptr;
    }
    if (g_device != nullptr)
        NVSDK_NGX_D3D12_Shutdown1(g_device);

    if (g_upload != nullptr && g_upload_map != nullptr)
        g_upload->Unmap(0, nullptr);
    if (g_readback != nullptr && g_readback_map != nullptr)
        g_readback->Unmap(0, nullptr);
    g_upload_map = nullptr;
    g_readback_map = nullptr;
    Release(g_upload);
    Release(g_readback);
    Release(g_input);
    Release(g_output);
    Release(g_list);
    Release(g_allocator);
    Release(g_queue);
    Release(g_fence);
    Release(g_device);
    if (g_fence_event != nullptr)
    {
        CloseHandle(g_fence_event);
        g_fence_event = nullptr;
    }
    if (g_log != nullptr)
    {
        fclose(g_log);
        g_log = nullptr;
    }
    SetDllDirectoryW(nullptr);
    g_fence_value = 0;
}
}

extern "C" __declspec(dllexport) int vsr_init(
    int input_width, int input_height, int scale, int quality, int hdr,
    const wchar_t *runtime_directory, const wchar_t *log_path)
{
    Shutdown();
    if (log_path != nullptr)
        _wfopen_s(&g_log, log_path, L"w");
    if (input_width <= 0 || input_height <= 0 || (scale != 2 && scale != 4))
        return 0;
    g_input_width = static_cast<UINT>(input_width);
    g_input_height = static_cast<UINT>(input_height);
    g_output_width = g_input_width * static_cast<UINT>(scale);
    g_output_height = g_input_height * static_cast<UINT>(scale);
    if (g_output_width / static_cast<UINT>(scale) != g_input_width ||
        g_output_height / static_cast<UINT>(scale) != g_input_height)
        return 0;
    g_hdr = hdr != 0;
    g_quality = static_cast<unsigned int>(std::clamp(quality, 1, 4));
    if (runtime_directory != nullptr && *runtime_directory != L'\0')
        SetDllDirectoryW(runtime_directory);
    Log("VSR init input=%ux%u output=%ux%u hdr=%d quality=%u", g_input_width,
        g_input_height, g_output_width, g_output_height, g_hdr ? 1 : 0, g_quality);

    if (!CreateDevice())
    {
        Log("D3D12 device creation failed");
        Shutdown();
        return 0;
    }
    const DXGI_FORMAT format = g_hdr ? DXGI_FORMAT_R10G10B10A2_UNORM : DXGI_FORMAT_R8G8B8A8_UNORM;
    g_input = CreateTexture(g_input_width, g_input_height, format, D3D12_RESOURCE_FLAG_NONE);
    g_output = CreateTexture(g_output_width, g_output_height, format,
                             D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    if (g_input == nullptr || g_output == nullptr ||
        !CreateStaging(g_input, D3D12_HEAP_TYPE_UPLOAD, &g_upload, &g_input_footprint,
                       &g_input_staging_size, &g_upload_map) ||
        !CreateStaging(g_output, D3D12_HEAP_TYPE_READBACK, &g_readback, &g_output_footprint,
                       &g_output_staging_size, &g_readback_map))
    {
        Log("texture/staging allocation failed");
        Shutdown();
        return 0;
    }

    const wchar_t *application_path =
        runtime_directory != nullptr && *runtime_directory != L'\0' ? runtime_directory : L".";
    NVSDK_NGX_Result result = NVSDK_NGX_D3D12_Init(0, application_path, g_device);
    Log("NGX init -> 0x%08X", result);
    if (NVSDK_NGX_FAILED(result))
    {
        Shutdown();
        return 0;
    }
    result = NVSDK_NGX_D3D12_GetCapabilityParameters(&g_parameters);
    if (NVSDK_NGX_FAILED(result) || g_parameters == nullptr)
    {
        Log("GetCapabilityParameters -> 0x%08X", result);
        Shutdown();
        return 0;
    }
    int available = 0;
    result = g_parameters->Get(NVSDK_NGX_Parameter_VSR_Available, &available);
    if (NVSDK_NGX_FAILED(result) || !available)
    {
        Log("VSR unavailable result=0x%08X available=%d", result, available);
        Shutdown();
        return 0;
    }
    if (!BeginCommands())
    {
        Shutdown();
        return 0;
    }
    NVSDK_NGX_Feature_Create_Params create_parameters = {};
    result = NGX_D3D12_CREATE_VSR_EXT(
        g_list, 1, 1, &g_feature, g_parameters, &create_parameters);
    Log("Create VSR -> 0x%08X feature=%p", result, static_cast<void *>(g_feature));
    if (NVSDK_NGX_FAILED(result) || g_feature == nullptr || !ExecuteAndWait())
    {
        Shutdown();
        return 0;
    }
    return 1;
}

extern "C" __declspec(dllexport) int vsr_process(const void *input_rgba, void *output_rgba)
{
    if (g_feature == nullptr || input_rgba == nullptr || output_rgba == nullptr)
        return 0;
    CopyInput(input_rgba);
    if (!BeginCommands())
        return 0;
    RecordUpload();
    NVSDK_NGX_D3D12_VSR_Eval_Params parameters = {};
    parameters.pInput = g_input;
    parameters.pOutput = g_output;
    parameters.InputSubrectSize = {g_input_width, g_input_height};
    parameters.OutputSubrectSize = {g_output_width, g_output_height};
    parameters.QualityLevel = static_cast<NVSDK_NGX_VSR_QualityLevel>(g_quality);
    const NVSDK_NGX_Result result = NGX_D3D12_EVALUATE_VSR_EXT(
        g_list, g_feature, g_parameters, &parameters);
    if (NVSDK_NGX_FAILED(result))
    {
        Log("Evaluate VSR -> 0x%08X", result);
        g_list->Close();
        return 0;
    }
    RecordReadback();
    if (!ExecuteAndWait())
        return 0;
    CopyOutput(output_rgba);
    return 1;
}

extern "C" __declspec(dllexport) void vsr_shutdown()
{
    Shutdown();
}
