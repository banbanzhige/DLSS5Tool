#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <DirectXPackedVector.h>

#include <algorithm>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include <nvsdk_ngx.h>
#include "guidance_upload.h"
#include "tile_blend.h"
#include "host_queue.h"

namespace {

constexpr int kMaxSlots = host_queue::kMaxSlots;
IDXGIAdapter3 *g_budget_adapter = nullptr;
constexpr NVSDK_NGX_Feature kFeatureId = static_cast<NVSDK_NGX_Feature>(18);
constexpr unsigned int kNvidiaVendorId = 0x10DE;
constexpr unsigned int kAdapterSoftware = 1u;
constexpr unsigned int kAdapterD3D12Level11 = 2u;
constexpr unsigned int kAdapterD3D12Level12 = 4u;

struct DlssnrAdapterInfo
{
    unsigned int struct_size = sizeof(DlssnrAdapterInfo);
    unsigned int preference_index = 0;
    unsigned int vendor_id = 0;
    unsigned int device_id = 0;
    unsigned int subsys_id = 0;
    unsigned int revision = 0;
    unsigned int flags = 0;
    unsigned long long dedicated_video_memory = 0;
    long luid_high = 0;
    unsigned long luid_low = 0;
    wchar_t description[128] = {};
};

template <typename T>
void Release(T *&value)
{
    if (value != nullptr)
    {
        value->Release();
        value = nullptr;
    }
}

bool NgxSucceeded(NVSDK_NGX_Result result)
{
    return (static_cast<unsigned int>(result) & 0xFFF00000u) != 0xBAD00000u;
}

FILE *g_log = nullptr;

void Log(const char *format, ...)
{
    char line[2048] = {};
    va_list args;
    va_start(args, format);
    _vsnprintf_s(line, sizeof(line), _TRUNCATE, format, args);
    va_end(args);
    if (g_log != nullptr)
    {
        fputs(line, g_log);
        fputc('\n', g_log);
        fflush(g_log);
    }
    OutputDebugStringA(line);
    OutputDebugStringA("\n");
}

struct HostConfig
{
    bool zero_guidance_fast_path = true;
    bool persistent_buffers = true;
    bool merged_submission = true;
    bool auto_fallback = true;
    bool tiled_subrects = false;
    int in_flight = 2;
    int tile_width = 6000;
    int tile_height = 3000;
};

struct Options
{
    unsigned int preset = 1;
    unsigned int style = 0;
    float intensity = 1.0f;
    float local_tone = 1.0f;
    float local_struct = 1.0f;
    float skin_struct = 1.0f;
    unsigned int use_auto_mask = 0;
    unsigned int ui_correction = 0;
    int guidance_mode = 0;
    int depth_convention = 2;
    float motion_scale_x = 1.0f;
    float motion_scale_y = 1.0f;
};

enum class FrameFormat : int
{
    Rgba8 = 0,
    Rgba16Float = 1,
};

enum class ColorProfile : int
{
    Srgb = 0,
    ScRgb = 1,
    Hdr10Pq = 2,
    Hdr10Hlg = 3,
};

struct Staging
{
    ID3D12Resource *resource = nullptr;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint = {};
    UINT rows = 0;
    UINT64 row_size = 0;
    UINT64 total_size = 0;
    unsigned char *mapped = nullptr;
};

struct Slot
{
    ID3D12CommandAllocator *allocator = nullptr;
    ID3D12GraphicsCommandList *list = nullptr;
    ID3D12Resource *color = nullptr;
    ID3D12Resource *output = nullptr;
    ID3D12Resource *motion = nullptr;
    ID3D12Resource *depth = nullptr;
    Staging color_upload;
    Staging motion_upload;
    Staging depth_upload;
    Staging output_readback;
    UINT64 fence_value = 0;
    bool pending = false;
};

using PFN_InitExt = NVSDK_NGX_Result(NVSDK_CONV *)(
    unsigned long long, const wchar_t *, ID3D12Device *,
    NVSDK_NGX_Version, const NVSDK_NGX_Parameter *);
using PFN_CreateFeature = decltype(&NVSDK_NGX_D3D12_CreateFeature);
using PFN_EvaluateFeature = decltype(&NVSDK_NGX_D3D12_EvaluateFeature);
using PFN_ReleaseFeature = decltype(&NVSDK_NGX_D3D12_ReleaseFeature);

HostConfig g_config;
Options g_options;
bool g_needs_recreate = false;
bool g_initialized = false;
bool g_ready = false;
UINT g_width = 0;
UINT g_height = 0;
UINT g_feature_width = 0;
UINT g_feature_height = 0;
int g_slot_count = 1;
FrameFormat g_frame_format = FrameFormat::Rgba8;
ColorProfile g_color_profile = ColorProfile::Srgb;

ID3D12Device *g_device = nullptr;
ID3D12CommandQueue *g_queue = nullptr;
ID3D12Fence *g_fence = nullptr;
HANDLE g_fence_event = nullptr;
UINT64 g_fence_value = 0;
Slot g_slots[kMaxSlots];
ID3D12Resource *g_zero_motion = nullptr;
ID3D12Resource *g_zero_depth = nullptr;
bool g_adapter_explicit = false;
LUID g_requested_adapter_luid = {};
bool g_has_selected_adapter = false;
DlssnrAdapterInfo g_selected_adapter = {};

HMODULE g_runtime = nullptr;
PFN_InitExt g_init_ext = nullptr;
PFN_CreateFeature g_create_feature = nullptr;
PFN_EvaluateFeature g_evaluate_feature = nullptr;
PFN_ReleaseFeature g_release_feature = nullptr;
NVSDK_NGX_Parameter *g_params = nullptr;
NVSDK_NGX_Handle *g_feature = nullptr;

int g_pending_order[kMaxSlots] = {};
int g_pending_head = 0;
int g_pending_count = 0;
int g_enqueue_cursor = 0;

using PFN_GetModuleFileNameW = DWORD(WINAPI *)(HMODULE, LPWSTR, DWORD);
PFN_GetModuleFileNameW g_original_get_module_filename = nullptr;
HMODULE g_self_module = nullptr;
uintptr_t *g_iat_slot = nullptr;

DWORD WINAPI HookGetModuleFileNameW(HMODULE module, LPWSTR filename, DWORD size)
{
    if (module == g_self_module)
    {
        static constexpr wchar_t replacement[] = L"nvngx.dll";
        constexpr DWORD length = static_cast<DWORD>((sizeof(replacement) / sizeof(wchar_t)) - 1);
        if (size <= length)
        {
            if (filename != nullptr && size > 0)
            {
                const DWORD copy = size - 1;
                memcpy(filename, replacement, static_cast<size_t>(copy) * sizeof(wchar_t));
                filename[copy] = L'\0';
            }
            SetLastError(ERROR_INSUFFICIENT_BUFFER);
            return size;
        }
        memcpy(filename, replacement, sizeof(replacement));
        SetLastError(ERROR_SUCCESS);
        return length;
    }
    return g_original_get_module_filename != nullptr
        ? g_original_get_module_filename(module, filename, size)
        : 0;
}

bool InstallCallerHook(HMODULE module)
{
    if (module == nullptr)
        return false;
    if (g_iat_slot != nullptr)
        return true;

    auto *base = reinterpret_cast<unsigned char *>(module);
    auto *dos = reinterpret_cast<IMAGE_DOS_HEADER *>(base);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0)
        return false;
    auto *nt = reinterpret_cast<IMAGE_NT_HEADERS64 *>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC)
        return false;
    const auto &directory = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (directory.VirtualAddress == 0 || directory.Size == 0)
        return false;

    GetModuleHandleExW(
        GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(&HookGetModuleFileNameW), &g_self_module);

    auto *descriptor = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR *>(base + directory.VirtualAddress);
    for (; descriptor->Name != 0; ++descriptor)
    {
        auto *names = descriptor->OriginalFirstThunk != 0
            ? reinterpret_cast<IMAGE_THUNK_DATA64 *>(base + descriptor->OriginalFirstThunk)
            : reinterpret_cast<IMAGE_THUNK_DATA64 *>(base + descriptor->FirstThunk);
        auto *thunks = reinterpret_cast<IMAGE_THUNK_DATA64 *>(base + descriptor->FirstThunk);
        for (; names->u1.AddressOfData != 0; ++names, ++thunks)
        {
            if (IMAGE_SNAP_BY_ORDINAL64(names->u1.Ordinal))
                continue;
            auto *entry = reinterpret_cast<IMAGE_IMPORT_BY_NAME *>(base + names->u1.AddressOfData);
            if (strcmp(reinterpret_cast<const char *>(entry->Name), "GetModuleFileNameW") != 0)
                continue;

            auto *slot = reinterpret_cast<uintptr_t *>(&thunks->u1.Function);
            g_original_get_module_filename = reinterpret_cast<PFN_GetModuleFileNameW>(*slot);
            DWORD old_protect = 0;
            if (!VirtualProtect(slot, sizeof(*slot), PAGE_READWRITE, &old_protect))
                return false;
            *slot = reinterpret_cast<uintptr_t>(&HookGetModuleFileNameW);
            DWORD ignored = 0;
            VirtualProtect(slot, sizeof(*slot), old_protect, &ignored);
            FlushInstructionCache(GetCurrentProcess(), slot, sizeof(*slot));
            g_iat_slot = slot;
            Log("caller hook installed slot=%p", static_cast<void *>(slot));
            return true;
        }
    }
    return false;
}

void RestoreCallerHook()
{
    if (g_iat_slot != nullptr && g_original_get_module_filename != nullptr)
    {
        DWORD old_protect = 0;
        if (VirtualProtect(g_iat_slot, sizeof(*g_iat_slot), PAGE_READWRITE, &old_protect))
        {
            *g_iat_slot = reinterpret_cast<uintptr_t>(g_original_get_module_filename);
            DWORD ignored = 0;
            VirtualProtect(g_iat_slot, sizeof(*g_iat_slot), old_protect, &ignored);
            FlushInstructionCache(GetCurrentProcess(), g_iat_slot, sizeof(*g_iat_slot));
        }
    }
    g_iat_slot = nullptr;
    g_original_get_module_filename = nullptr;
    g_self_module = nullptr;
}

NVSDK_NGX_Result SafeInitExt(
    PFN_InitExt function, unsigned long long app_id, const wchar_t *path,
    ID3D12Device *device, NVSDK_NGX_Version version, const NVSDK_NGX_Parameter *params)
{
    __try
    {
        return function(app_id, path, device, version, params);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        Log("Init_Ext raised SEH 0x%08X", GetExceptionCode());
        return static_cast<NVSDK_NGX_Result>(0xBAD00000u);
    }
}

NVSDK_NGX_Result SafeCreate(
    ID3D12GraphicsCommandList *list, NVSDK_NGX_Parameter *params, NVSDK_NGX_Handle **handle)
{
    __try
    {
        return g_create_feature(list, kFeatureId, params, handle);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        Log("CreateFeature raised SEH 0x%08X", GetExceptionCode());
        return static_cast<NVSDK_NGX_Result>(0xBAD00000u);
    }
}

NVSDK_NGX_Result SafeEvaluate(ID3D12GraphicsCommandList *list)
{
    __try
    {
        return g_evaluate_feature(list, g_feature, g_params, nullptr);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        Log("EvaluateFeature raised SEH 0x%08X", GetExceptionCode());
        return static_cast<NVSDK_NGX_Result>(0xBAD00000u);
    }
}

void SafeReleaseFeature()
{
    if (g_feature == nullptr || g_release_feature == nullptr)
        return;
    __try
    {
        g_release_feature(g_feature);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        Log("ReleaseFeature raised SEH 0x%08X (ignored)", GetExceptionCode());
    }
    g_feature = nullptr;
}

D3D12_HEAP_PROPERTIES HeapProperties(D3D12_HEAP_TYPE type)
{
    D3D12_HEAP_PROPERTIES value = {};
    value.Type = type;
    value.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
    value.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
    value.CreationNodeMask = 1;
    value.VisibleNodeMask = 1;
    return value;
}

D3D12_RESOURCE_DESC BufferDescription(UINT64 size)
{
    D3D12_RESOURCE_DESC value = {};
    value.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    value.Alignment = 0;
    value.Width = size;
    value.Height = 1;
    value.DepthOrArraySize = 1;
    value.MipLevels = 1;
    value.Format = DXGI_FORMAT_UNKNOWN;
    value.SampleDesc.Count = 1;
    value.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    return value;
}

ID3D12Resource *CreateTexture(DXGI_FORMAT format, D3D12_RESOURCE_FLAGS flags)
{
    D3D12_RESOURCE_DESC description = {};
    description.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    description.Width = g_width;
    description.Height = g_height;
    description.DepthOrArraySize = 1;
    description.MipLevels = 1;
    description.Format = format;
    description.SampleDesc.Count = 1;
    description.Layout = D3D12_TEXTURE_LAYOUT_UNKNOWN;
    description.Flags = flags;
    const auto heap = HeapProperties(D3D12_HEAP_TYPE_DEFAULT);
    ID3D12Resource *resource = nullptr;
    const HRESULT result = g_device->CreateCommittedResource(
        &heap, D3D12_HEAP_FLAG_NONE, &description, D3D12_RESOURCE_STATE_COMMON,
        nullptr, __uuidof(ID3D12Resource), reinterpret_cast<void **>(&resource));
    if (FAILED(result))
        Log("texture create failed format=%u flags=%u hr=0x%08X", format, flags, result);
    return resource;
}

DXGI_FORMAT FrameDxgiFormat()
{
    return g_frame_format == FrameFormat::Rgba16Float
        ? DXGI_FORMAT_R16G16B16A16_FLOAT
        : DXGI_FORMAT_R8G8B8A8_UNORM;
}

size_t FrameRowPitch()
{
    return static_cast<size_t>(g_width) *
        (g_frame_format == FrameFormat::Rgba16Float ? 8u : 4u);
}

bool IsHdrProfile()
{
    return g_color_profile != ColorProfile::Srgb;
}

void ReleaseStaging(Staging &staging)
{
    if (staging.resource != nullptr && staging.mapped != nullptr)
        staging.resource->Unmap(0, nullptr);
    staging.mapped = nullptr;
    Release(staging.resource);
    staging = {};
}

bool CreateStaging(ID3D12Resource *texture, D3D12_HEAP_TYPE type, Staging &staging)
{
    ReleaseStaging(staging);
    const D3D12_RESOURCE_DESC description = texture->GetDesc();
    g_device->GetCopyableFootprints(
        &description, 0, 1, 0, &staging.footprint,
        &staging.rows, &staging.row_size, &staging.total_size);
    const auto heap = HeapProperties(type);
    const auto buffer = BufferDescription(staging.total_size);
    const D3D12_RESOURCE_STATES state = type == D3D12_HEAP_TYPE_UPLOAD
        ? D3D12_RESOURCE_STATE_GENERIC_READ
        : D3D12_RESOURCE_STATE_COPY_DEST;
    HRESULT result = g_device->CreateCommittedResource(
        &heap, D3D12_HEAP_FLAG_NONE, &buffer, state, nullptr,
        __uuidof(ID3D12Resource), reinterpret_cast<void **>(&staging.resource));
    if (FAILED(result) || staging.resource == nullptr)
    {
        Log("staging create failed type=%u hr=0x%08X", type, result);
        ReleaseStaging(staging);
        return false;
    }
    D3D12_RANGE range = type == D3D12_HEAP_TYPE_UPLOAD
        ? D3D12_RANGE{0, 0}
        : D3D12_RANGE{0, static_cast<SIZE_T>(staging.total_size)};
    result = staging.resource->Map(0, &range, reinterpret_cast<void **>(&staging.mapped));
    if (FAILED(result) || staging.mapped == nullptr)
    {
        Log("staging map failed type=%u hr=0x%08X", type, result);
        ReleaseStaging(staging);
        return false;
    }
    return true;
}

void CopyRowsToStaging(Staging &staging, const void *source, size_t source_pitch)
{
    const auto *input = static_cast<const unsigned char *>(source);
    auto *output = staging.mapped + staging.footprint.Offset;
    const size_t copy_size = static_cast<size_t>(staging.row_size);
    for (UINT row = 0; row < staging.rows; ++row)
        memcpy(output + static_cast<size_t>(row) * staging.footprint.Footprint.RowPitch,
               input + static_cast<size_t>(row) * source_pitch, copy_size);
}

void CopyRowsFromStaging(const Staging &staging, void *destination, size_t destination_pitch)
{
    const auto *input = staging.mapped + staging.footprint.Offset;
    auto *output = static_cast<unsigned char *>(destination);
    const size_t copy_size = static_cast<size_t>(staging.row_size);
    for (UINT row = 0; row < staging.rows; ++row)
        memcpy(output + static_cast<size_t>(row) * destination_pitch,
               input + static_cast<size_t>(row) * staging.footprint.Footprint.RowPitch, copy_size);
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

void RecordUpload(
    ID3D12GraphicsCommandList *list, const Staging &staging, ID3D12Resource *texture)
{
    auto to_copy = Transition(texture, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
    list->ResourceBarrier(1, &to_copy);
    D3D12_TEXTURE_COPY_LOCATION source = {};
    source.pResource = staging.resource;
    source.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    source.PlacedFootprint = staging.footprint;
    D3D12_TEXTURE_COPY_LOCATION destination = {};
    destination.pResource = texture;
    destination.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    destination.SubresourceIndex = 0;
    list->CopyTextureRegion(&destination, 0, 0, 0, &source, nullptr);
    auto to_common = Transition(texture, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COMMON);
    list->ResourceBarrier(1, &to_common);
}

void RecordReadback(
    ID3D12GraphicsCommandList *list, ID3D12Resource *texture, const Staging &staging,
    const D3D12_BOX *box = nullptr)
{
    auto to_copy = Transition(texture, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
    list->ResourceBarrier(1, &to_copy);
    D3D12_TEXTURE_COPY_LOCATION source = {};
    source.pResource = texture;
    source.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    source.SubresourceIndex = 0;
    D3D12_TEXTURE_COPY_LOCATION destination = {};
    destination.pResource = staging.resource;
    destination.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    destination.PlacedFootprint = staging.footprint;
    list->CopyTextureRegion(&destination, 0, 0, 0, &source, box);
    auto to_common = Transition(texture, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
    list->ResourceBarrier(1, &to_common);
}

bool WaitFence(UINT64 value, DWORD timeout = INFINITE)
{
    if (value == 0 || g_fence->GetCompletedValue() >= value)
        return true;
    ResetEvent(g_fence_event);
    if (FAILED(g_fence->SetEventOnCompletion(value, g_fence_event)))
        return false;
    return WaitForSingleObject(g_fence_event, timeout) == WAIT_OBJECT_0;
}

bool BeginCommands(Slot &slot)
{
    if (!WaitFence(slot.fence_value))
        return false;
    if (FAILED(slot.allocator->Reset()))
        return false;
    return SUCCEEDED(slot.list->Reset(slot.allocator, nullptr));
}

bool SubmitCommands(Slot &slot, bool wait)
{
    if (FAILED(slot.list->Close()))
        return false;
    ID3D12CommandList *lists[] = {slot.list};
    g_queue->ExecuteCommandLists(1, lists);
    const UINT64 value = ++g_fence_value;
    if (FAILED(g_queue->Signal(g_fence, value)))
        return false;
    slot.fence_value = value;
    return !wait || WaitFence(value);
}

void AbortCommands(Slot &slot)
{
    if (slot.list != nullptr)
        slot.list->Close();
}

void PrepareMotion(Staging &staging, const float *motion)
{
    const bool enabled = g_options.guidance_mode == 1 || g_options.guidance_mode == 3;
    guidance_upload::Motion(staging.mapped + staging.footprint.Offset,
        staging.footprint.Footprint.RowPitch, g_width, staging.rows,
        enabled ? motion : nullptr);
}

void PrepareDepth(Staging &staging, const float *depth)
{
    const bool enabled = g_options.guidance_mode == 2 || g_options.guidance_mode == 3;
    guidance_upload::Depth(staging.mapped + staging.footprint.Offset,
        staging.footprint.Footprint.RowPitch, g_width, staging.rows,
        enabled ? depth : nullptr);
}

bool UsesDepth()
{
    return !g_config.zero_guidance_fast_path &&
        (g_options.guidance_mode == 2 || g_options.guidance_mode == 3);
}

int ScalingRatioCallback(NVSDK_NGX_Parameter *parameters)
{
    parameters->Set("DLSSNR.ScalingRatio", 1.0f);
    return 1;
}

void SetCreateParameters()
{
    const int width = static_cast<int>(g_width);
    const int height = static_cast<int>(g_height);
    const int feature_width = static_cast<int>(g_feature_width);
    const int feature_height = static_cast<int>(g_feature_height);
    g_params->Set("DLSSNR.Width", feature_width);
    g_params->Set("DLSSNR.Height", feature_height);
    g_params->Set("DLSSNR.InputWidth", width);
    g_params->Set("DLSSNR.InputHeight", height);
    g_params->Set("DLSSNR.OutputWidth", width);
    g_params->Set("DLSSNR.OutputHeight", height);
    g_params->Set("DLSSNR.Output.Width", width);
    g_params->Set("DLSSNR.Output.Height", height);
    g_params->Set("DLSSNR.Upscaling", 0);
    g_params->Set("DLSSNR.Scale", 1.0f);
    g_params->Set("DLSSNR.ScalingRatio", 1.0f);
    g_params->Set(
        "DLSSNRComputeScalingRatioCallback",
        static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(&ScalingRatioCallback)));
    g_params->Set("DLSSNR.Hint.Render.Preset", g_options.preset);
    g_params->Set("Width", width);
    g_params->Set("Height", height);
    g_params->Set("PerfQualityValue", 2u);
    g_params->Set("CreationNodeMask", 1);
    g_params->Set("VisibilityNodeMask", 1);
    g_params->Set(
        "DLSS.Feature.Create.Flags",
        IsHdrProfile() ? static_cast<int>(NVSDK_NGX_DLSS_Feature_Flags_IsHDR) : 0);
}

void SetEvaluationParameters(
    Slot &slot, bool reset, UINT base_x = 0, UINT base_y = 0,
    UINT rect_width = 0, UINT rect_height = 0)
{
    ID3D12Resource *motion = g_config.zero_guidance_fast_path ? g_zero_motion : slot.motion;
    ID3D12Resource *depth = UsesDepth() ? slot.depth : g_zero_depth;
    g_params->Set("DLSSNR.Color", slot.color);
    g_params->Set("DLSSNR.Output", slot.output);
    g_params->Set("DLSSNR.MVec", motion);
    g_params->Set("DLSSNR.Depth", depth);

    struct RectKeys
    {
        const char *x;
        const char *y;
        const char *width;
        const char *height;
    };
    static constexpr RectKeys keys[] = {
        {"DLSSNR.ColorSubrectBaseX", "DLSSNR.ColorSubrectBaseY", "DLSSNR.ColorSubrectWidth", "DLSSNR.ColorSubrectHeight"},
        {"DLSSNR.OutputSubrectBaseX", "DLSSNR.OutputSubrectBaseY", "DLSSNR.OutputSubrectWidth", "DLSSNR.OutputSubrectHeight"},
        {"DLSSNR.MVecSubrectBaseX", "DLSSNR.MVecSubrectBaseY", "DLSSNR.MVecSubrectWidth", "DLSSNR.MVecSubrectHeight"},
        {"DLSSNR.DepthSubrectBaseX", "DLSSNR.DepthSubrectBaseY", "DLSSNR.DepthSubrectWidth", "DLSSNR.DepthSubrectHeight"},
    };
    if (rect_width == 0)
        rect_width = g_width;
    if (rect_height == 0)
        rect_height = g_height;
    for (const auto &item : keys)
    {
        g_params->Set(item.x, static_cast<int>(base_x));
        g_params->Set(item.y, static_cast<int>(base_y));
        g_params->Set(item.width, static_cast<int>(rect_width));
        g_params->Set(item.height, static_cast<int>(rect_height));
    }

    g_params->Set("DLSSNR.MVecScaleX", g_options.motion_scale_x);
    g_params->Set("DLSSNR.MVecScaleY", g_options.motion_scale_y);
    g_params->Set("DLSSNR.DepthInverted", g_options.depth_convention != 1 ? 1u : 0u);
    g_params->Set("DLSSNR.Indicator.Invert.X.Axis", 0u);
    g_params->Set("DLSSNR.Indicator.Invert.Y.Axis", 0u);
    g_params->Set("DLSSNR.Enabled", 1u);
    g_params->Set("DLSSNR.Reset", reset ? 1u : 0u);
    g_params->Set("DLSSNR.Style", g_options.style);
    g_params->Set("DLSSNR.Intensity", g_options.intensity);
    g_params->Set("DLSSNR.LocalToneStrength", g_options.local_tone);
    g_params->Set("DLSSNR.LocalStructureStrength", g_options.local_struct);
    g_params->Set("DLSSNR.SkinStructureStrength", g_options.skin_struct);
    g_params->Set("DLSSNR.UseAutoMask", g_options.use_auto_mask);
    g_params->Set("DLSSNR.UICorrection", g_options.ui_correction);
    g_params->Set("DLSS.Pre.Exposure", 1.0f);
    g_params->Set("DLSS.Exposure.Scale", 1.0f);
}

void ReleaseSlotResources(Slot &slot)
{
    ReleaseStaging(slot.color_upload);
    ReleaseStaging(slot.motion_upload);
    ReleaseStaging(slot.depth_upload);
    ReleaseStaging(slot.output_readback);
    Release(slot.color);
    Release(slot.output);
    Release(slot.motion);
    Release(slot.depth);
    slot.fence_value = 0;
    slot.pending = false;
}

void ResetPending()
{
    g_pending_head = 0;
    g_pending_count = 0;
    g_enqueue_cursor = 0;
    memset(g_pending_order, 0, sizeof(g_pending_order));
    for (auto &slot : g_slots)
        slot.pending = false;
}

bool WaitAll()
{
    bool ok = true;
    for (int index = 0; index < g_slot_count; ++index)
        ok = WaitFence(g_slots[index].fence_value) && ok;
    ResetPending();
    return ok;
}

bool InitializeZeroTextures()
{
    Slot &slot = g_slots[0];
    Staging motion_upload;
    Staging depth_upload;
    if ((g_zero_motion && !CreateStaging(g_zero_motion, D3D12_HEAP_TYPE_UPLOAD, motion_upload)) ||
        !CreateStaging(g_zero_depth, D3D12_HEAP_TYPE_UPLOAD, depth_upload))
    {
        ReleaseStaging(motion_upload);
        ReleaseStaging(depth_upload);
        return false;
    }
    if (g_zero_motion)
        memset(motion_upload.mapped, 0, static_cast<size_t>(motion_upload.total_size));
    memset(depth_upload.mapped, 0, static_cast<size_t>(depth_upload.total_size));
    const bool recorded = BeginCommands(slot);
    if (recorded)
    {
        if (g_zero_motion)
            RecordUpload(slot.list, motion_upload, g_zero_motion);
        RecordUpload(slot.list, depth_upload, g_zero_depth);
    }
    const bool ok = recorded && SubmitCommands(slot, true);
    ReleaseStaging(motion_upload);
    ReleaseStaging(depth_upload);
    return ok;
}

bool CreateFrameResources()
{
    g_slot_count = (!g_config.tiled_subrects && g_config.merged_submission && g_config.persistent_buffers)
        ? std::clamp(g_config.in_flight, 1, kMaxSlots)
        : 1;
    const int requested_slots = g_slot_count;
    if (g_slot_count > 1)
    {
        DXGI_QUERY_VIDEO_MEMORY_INFO memory = {};
        const bool known = g_budget_adapter && SUCCEEDED(g_budget_adapter->QueryVideoMemoryInfo(
            0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &memory));
        const uint64_t available = memory.Budget > memory.CurrentUsage ? memory.Budget - memory.CurrentUsage : 0;
        const uint64_t frame_bpp = g_frame_format == FrameFormat::Rgba16Float ? 8 : 4;
        // Row-pitch and resource alignment padding included conservatively.
        const uint64_t frame = ((uint64_t(g_width) * frame_bpp + 255) / 256 * 256 * g_height + 65535) / 65536 * 65536;
        const uint64_t map = ((uint64_t(g_width) * 4 + 255) / 256 * 256 * g_height + 65535) / 65536 * 65536;
        const uint64_t slot_gpu = 2 * frame + (g_config.zero_guidance_fast_path ? 0 : 2 * map);
        const uint64_t reserve = (1536 + (g_options.guidance_mode ? 1024ull : 0ull)) * host_queue::kMiB;
        g_slot_count = host_queue::Limit(g_slot_count, slot_gpu, available, reserve, known);
        MEMORYSTATUSEX ram = {};
        ram.dwLength = sizeof(ram);
        const bool ram_known = GlobalMemoryStatusEx(&ram) != FALSE;
        // Upload/readback staging plus the caller's retained original/output frames.
        g_slot_count = host_queue::Limit(g_slot_count, slot_gpu + 2 * frame,
            std::min(ram.ullAvailPhys, ram.ullAvailPageFile), 512 * host_queue::kMiB, ram_known);
        Log("queue budget: requested=%d selected=%d gpu_budget_known=%d available=%llu MiB slot=%llu MiB",
            requested_slots, g_slot_count, known, available / host_queue::kMiB, slot_gpu / host_queue::kMiB);
    }
    for (int index = 0; index < g_slot_count; ++index)
    {
        Slot &slot = g_slots[index];
        slot.color = CreateTexture(FrameDxgiFormat(), D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
        slot.output = CreateTexture(FrameDxgiFormat(), D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
        if (slot.color == nullptr || slot.output == nullptr)
            return false;
        if (!g_config.zero_guidance_fast_path)
        {
            slot.motion = CreateTexture(DXGI_FORMAT_R16G16_FLOAT, D3D12_RESOURCE_FLAG_NONE);
            slot.depth = CreateTexture(DXGI_FORMAT_R32_FLOAT, D3D12_RESOURCE_FLAG_NONE);
            if (slot.motion == nullptr || slot.depth == nullptr)
                return false;
        }
        if (g_config.persistent_buffers)
        {
            if (!CreateStaging(slot.color, D3D12_HEAP_TYPE_UPLOAD, slot.color_upload) ||
                !CreateStaging(slot.output, D3D12_HEAP_TYPE_READBACK, slot.output_readback))
                return false;
            if (!g_config.zero_guidance_fast_path &&
                (!CreateStaging(slot.motion, D3D12_HEAP_TYPE_UPLOAD, slot.motion_upload) ||
                 !CreateStaging(slot.depth, D3D12_HEAP_TYPE_UPLOAD, slot.depth_upload)))
                return false;
        }
    }
    if (g_config.zero_guidance_fast_path)
    {
        g_zero_motion = CreateTexture(DXGI_FORMAT_R16G16_FLOAT, D3D12_RESOURCE_FLAG_NONE);
        if (g_zero_motion == nullptr)
            return false;
    }
    // A valid, immutable zero texture remains bound when depth is inactive.
    // Keep slot depth resources for safe in-flight mode switches, but do not
    // clear/copy/upload their unused contents every frame.
    g_zero_depth = CreateTexture(DXGI_FORMAT_R32_FLOAT, D3D12_RESOURCE_FLAG_NONE);
    if (g_zero_depth == nullptr || !InitializeZeroTextures())
        return false;
    ResetPending();
    return true;
}

void ReleaseFeatureResources()
{
    WaitAll();
    SafeReleaseFeature();
    if (g_params != nullptr)
    {
        NVSDK_NGX_D3D12_DestroyParameters(g_params);
        g_params = nullptr;
    }
    for (auto &slot : g_slots)
        ReleaseSlotResources(slot);
    Release(g_zero_motion);
    Release(g_zero_depth);
    g_ready = false;
    g_feature_width = 0;
    g_feature_height = 0;
}

bool SubmitUploadImmediate(
    Slot &slot, ID3D12Resource *texture, Staging *persistent,
    const void *source, size_t source_pitch)
{
    Staging temporary;
    Staging *staging = persistent;
    if (staging == nullptr)
    {
        if (!CreateStaging(texture, D3D12_HEAP_TYPE_UPLOAD, temporary))
            return false;
        staging = &temporary;
    }
    CopyRowsToStaging(*staging, source, source_pitch);
    const bool recorded = BeginCommands(slot);
    if (recorded)
        RecordUpload(slot.list, *staging, texture);
    const bool ok = recorded && SubmitCommands(slot, true);
    ReleaseStaging(temporary);
    return ok;
}

bool SubmitMotionImmediate(Slot &slot, const float *motion)
{
    Staging temporary;
    Staging *staging = g_config.persistent_buffers ? &slot.motion_upload : &temporary;
    if (!g_config.persistent_buffers && !CreateStaging(slot.motion, D3D12_HEAP_TYPE_UPLOAD, temporary))
        return false;
    PrepareMotion(*staging, motion);
    const bool recorded = BeginCommands(slot);
    if (recorded)
        RecordUpload(slot.list, *staging, slot.motion);
    const bool ok = recorded && SubmitCommands(slot, true);
    ReleaseStaging(temporary);
    return ok;
}

bool SubmitDepthImmediate(Slot &slot, const float *depth)
{
    if (!UsesDepth())
        return true;
    Staging temporary;
    Staging *staging = g_config.persistent_buffers ? &slot.depth_upload : &temporary;
    if (!g_config.persistent_buffers && !CreateStaging(slot.depth, D3D12_HEAP_TYPE_UPLOAD, temporary))
        return false;
    PrepareDepth(*staging, depth);
    const bool recorded = BeginCommands(slot);
    if (recorded)
        RecordUpload(slot.list, *staging, slot.depth);
    const bool ok = recorded && SubmitCommands(slot, true);
    ReleaseStaging(temporary);
    return ok;
}

bool SubmitReadbackImmediate(Slot &slot, void *output)
{
    Staging temporary;
    Staging *staging = g_config.persistent_buffers ? &slot.output_readback : &temporary;
    if (!g_config.persistent_buffers && !CreateStaging(slot.output, D3D12_HEAP_TYPE_READBACK, temporary))
        return false;
    const bool recorded = BeginCommands(slot);
    if (recorded)
        RecordReadback(slot.list, slot.output, *staging);
    const bool ok = recorded && SubmitCommands(slot, true);
    if (ok)
        CopyRowsFromStaging(*staging, output, FrameRowPitch());
    ReleaseStaging(temporary);
    return ok;
}

bool ProcessCompatibility(
    const void *color, const float *motion, const float *depth, void *output, bool reset)
{
    Slot &slot = g_slots[0];
    if (!SubmitUploadImmediate(
            slot, slot.color, g_config.persistent_buffers ? &slot.color_upload : nullptr,
            color, FrameRowPitch()))
        return false;
    if (!g_config.zero_guidance_fast_path)
    {
        if (!SubmitMotionImmediate(slot, motion) || !SubmitDepthImmediate(slot, depth))
            return false;
    }
    if (!BeginCommands(slot))
        return false;
    SetEvaluationParameters(slot, reset);
    const NVSDK_NGX_Result evaluated = SafeEvaluate(slot.list);
    if (!NgxSucceeded(evaluated))
    {
        Log("EvaluateFeature compatibility -> 0x%08X", evaluated);
        AbortCommands(slot);
        return false;
    }
    if (!SubmitCommands(slot, true))
        return false;
    return SubmitReadbackImmediate(slot, output);
}

bool ProcessTiledSubrects(
    const void *color, const float *motion, const float *depth, void *output)
{
    Slot &slot = g_slots[0];
    if (!SubmitUploadImmediate(
            slot, slot.color, g_config.persistent_buffers ? &slot.color_upload : nullptr,
            color, FrameRowPitch()))
        return false;
    if (!g_config.zero_guidance_fast_path)
    {
        if (!SubmitMotionImmediate(slot, motion) || !SubmitDepthImmediate(slot, depth))
            return false;
    }

    // Read back each evaluated rect before an overlapping evaluation overwrites
    // it. The original color texture remains immutable throughout the frame.
    Staging temporary;
    struct Cleanup { Staging &s; ~Cleanup() { ReleaseStaging(s); } } cleanup{temporary};
    Staging *readback = g_config.persistent_buffers ? &slot.output_readback : &temporary;
    if (!g_config.persistent_buffers &&
        !CreateStaging(slot.output, D3D12_HEAP_TYPE_READBACK, temporary)) return false;
    try {
        const auto columns = tile_blend::Plan(g_width, g_feature_width);
        const auto rows = tile_blend::Plan(g_height, g_feature_height);
        tile_blend::Rows blend(g_width, g_feature_height);
        Log("Feature 18 overlap tiles: frame=%ux%u tile=%ux%u grid=%zux%zu context=128 blend=128",
            g_width, g_height, g_feature_width, g_feature_height, columns.size(), rows.size());
        const bool hdr = g_frame_format == FrameFormat::Rgba16Float;
        const size_t pixel_bytes = hdr ? 8 : 4;
        const size_t tile_pitch = size_t(g_feature_width) * pixel_bytes;
        std::vector<unsigned char> tile_data(tile_pitch * g_feature_height);
        for (size_t row = 0; row < rows.size(); ++row) {
          const auto &y = rows[row];
          for (const auto &x : columns) {
            if (!BeginCommands(slot))
                return false;
            // Each rectangle is an independent still-image region. Resetting prevents
            // temporal history from one spatial tile contaminating the next one.
            SetEvaluationParameters(slot, true, x.start, y.start, x.size, y.size);
            const NVSDK_NGX_Result evaluated = SafeEvaluate(slot.list);
            if (!NgxSucceeded(evaluated))
            {
                Log("EvaluateFeature tiled rect=%u,%u %ux%u -> 0x%08X",
                    x.start, y.start, x.size, y.size, evaluated);
                AbortCommands(slot);
                return false;
            }
            const D3D12_BOX box{x.start, y.start, 0, x.start + x.size, y.start + y.size, 1};
            RecordReadback(slot.list, slot.output, *readback, &box);
            if (!SubmitCommands(slot, true))
                return false;
            // Mapped READBACK memory can be very slow for scalar channel reads.
            // Bulk-copy once into cacheable RAM before the arithmetic loop.
            for (unsigned py = 0; py < y.size; ++py)
                memcpy(tile_data.data() + size_t(py) * tile_pitch,
                    readback->mapped + readback->footprint.Offset +
                    size_t(py) * readback->footprint.Footprint.RowPitch, size_t(x.size) * pixel_bytes);
            blend.Add(x, y, [&](unsigned px, unsigned py, unsigned c) {
                const auto *data = tile_data.data() + size_t(py) * tile_pitch;
                return hdr ? DirectX::PackedVector::XMConvertHalfToFloat(
                    reinterpret_cast<const uint16_t *>(data)[size_t(px) * 4 + c])
                    : float(data[size_t(px) * 4 + c]);
            });
          }
          blend.Flush(row + 1 < rows.size() ? rows[row + 1].begin : g_height,
            [&](unsigned x, unsigned y, unsigned c, float value) {
                const size_t index = (size_t(y) * g_width + x) * 4 + c;
                if (hdr)
                    static_cast<uint16_t *>(output)[index] = DirectX::PackedVector::XMConvertFloatToHalf(value);
                else
                    static_cast<unsigned char *>(output)[index] =
                        static_cast<unsigned char>(std::clamp(value + 0.5f, 0.0f, 255.0f));
            });
        }
        return true;
    } catch (const std::exception &error) {
        Log("Tile blending failed: %s", error.what());
        return false;
    }
}

bool ProcessMergedTransient(
    const void *color, const float *motion, const float *depth, void *output, bool reset)
{
    Slot &slot = g_slots[0];
    Staging color_upload;
    Staging motion_upload;
    Staging depth_upload;
    Staging readback;
    bool ok = CreateStaging(slot.color, D3D12_HEAP_TYPE_UPLOAD, color_upload) &&
              CreateStaging(slot.output, D3D12_HEAP_TYPE_READBACK, readback);
    if (ok && !g_config.zero_guidance_fast_path)
        ok = CreateStaging(slot.motion, D3D12_HEAP_TYPE_UPLOAD, motion_upload) &&
             (!UsesDepth() || CreateStaging(slot.depth, D3D12_HEAP_TYPE_UPLOAD, depth_upload));
    if (!ok)
        goto cleanup;

    CopyRowsToStaging(color_upload, color, FrameRowPitch());
    if (!g_config.zero_guidance_fast_path)
    {
        PrepareMotion(motion_upload, motion);
        if (UsesDepth())
            PrepareDepth(depth_upload, depth);
    }
    if (!BeginCommands(slot))
    {
        ok = false;
        goto cleanup;
    }
    RecordUpload(slot.list, color_upload, slot.color);
    if (!g_config.zero_guidance_fast_path)
    {
        RecordUpload(slot.list, motion_upload, slot.motion);
        if (UsesDepth())
            RecordUpload(slot.list, depth_upload, slot.depth);
    }
    SetEvaluationParameters(slot, reset);
    {
        const NVSDK_NGX_Result evaluated = SafeEvaluate(slot.list);
        if (!NgxSucceeded(evaluated))
        {
            Log("EvaluateFeature merged transient -> 0x%08X", evaluated);
            AbortCommands(slot);
            ok = false;
            goto cleanup;
        }
    }
    RecordReadback(slot.list, slot.output, readback);
    ok = SubmitCommands(slot, true);
    if (ok)
        CopyRowsFromStaging(readback, output, FrameRowPitch());

cleanup:
    ReleaseStaging(color_upload);
    ReleaseStaging(motion_upload);
    ReleaseStaging(depth_upload);
    ReleaseStaging(readback);
    return ok;
}

bool EnqueueFrame(const void *color, const float *motion, const float *depth, bool reset)
{
    if (!g_ready || g_config.tiled_subrects || !g_config.merged_submission || !g_config.persistent_buffers ||
        color == nullptr || g_pending_count >= g_slot_count)
        return false;
    const int index = g_enqueue_cursor;
    Slot &slot = g_slots[index];
    if (slot.pending || !WaitFence(slot.fence_value))
        return false;

    CopyRowsToStaging(slot.color_upload, color, FrameRowPitch());
    if (!g_config.zero_guidance_fast_path)
    {
        PrepareMotion(slot.motion_upload, motion);
        if (UsesDepth())
            PrepareDepth(slot.depth_upload, depth);
    }
    if (!BeginCommands(slot))
        return false;
    RecordUpload(slot.list, slot.color_upload, slot.color);
    if (!g_config.zero_guidance_fast_path)
    {
        RecordUpload(slot.list, slot.motion_upload, slot.motion);
        if (UsesDepth())
            RecordUpload(slot.list, slot.depth_upload, slot.depth);
    }
    SetEvaluationParameters(slot, reset);
    const NVSDK_NGX_Result evaluated = SafeEvaluate(slot.list);
    if (!NgxSucceeded(evaluated))
    {
        Log("EvaluateFeature merged slot=%d -> 0x%08X", index, evaluated);
        AbortCommands(slot);
        return false;
    }
    RecordReadback(slot.list, slot.output, slot.output_readback);
    if (!SubmitCommands(slot, false))
        return false;

    slot.pending = true;
    const int tail = (g_pending_head + g_pending_count) % kMaxSlots;
    g_pending_order[tail] = index;
    ++g_pending_count;
    g_enqueue_cursor = (g_enqueue_cursor + 1) % g_slot_count;
    return true;
}

bool DequeueFrame(void *output)
{
    if (output == nullptr || g_pending_count <= 0)
        return false;
    const int index = g_pending_order[g_pending_head];
    Slot &slot = g_slots[index];
    if (!slot.pending || !WaitFence(slot.fence_value))
        return false;
    CopyRowsFromStaging(slot.output_readback, output, FrameRowPitch());
    slot.pending = false;
    g_pending_head = (g_pending_head + 1) % kMaxSlots;
    --g_pending_count;
    return true;
}

bool CreateFeatureResources(UINT width, UINT height, int preset)
{
    if (!g_initialized || width == 0 || height == 0)
        return false;
    ReleaseFeatureResources();
    g_width = width;
    g_height = height;
    g_feature_width = g_config.tiled_subrects
        ? std::min(width, static_cast<UINT>(std::max(g_config.tile_width, 1)))
        : width;
    g_feature_height = g_config.tiled_subrects
        ? std::min(height, static_cast<UINT>(std::max(g_config.tile_height, 1)))
        : height;
    g_options.preset = static_cast<unsigned int>(std::max(preset, 0));
    if (!CreateFrameResources())
    {
        Log("texture/staging allocation failed");
        ReleaseFeatureResources();
        return false;
    }
    NVSDK_NGX_Result allocated = NVSDK_NGX_D3D12_AllocateParameters(&g_params);
    Log("AllocateParameters -> 0x%08X params=%p", allocated, static_cast<void *>(g_params));
    if (!NgxSucceeded(allocated) || g_params == nullptr)
    {
        ReleaseFeatureResources();
        return false;
    }
    SetCreateParameters();
    Slot &slot = g_slots[0];
    if (!BeginCommands(slot))
    {
        ReleaseFeatureResources();
        return false;
    }
    const NVSDK_NGX_Result created = SafeCreate(slot.list, g_params, &g_feature);
    Log("CreateFeature(18) -> 0x%08X feature=%p", created, static_cast<void *>(g_feature));
    if (!NgxSucceeded(created) || g_feature == nullptr)
    {
        AbortCommands(slot);
        ReleaseFeatureResources();
        return false;
    }
    if (!SubmitCommands(slot, true))
    {
        ReleaseFeatureResources();
        return false;
    }
    g_ready = true;
    g_needs_recreate = false;
    ResetPending();
    Log("Feature 18 ready: merged=%d persistent=%d zero_fast=%d in_flight=%d tiled=%d core=%ux%u",
        g_config.merged_submission, g_config.persistent_buffers,
        g_config.zero_guidance_fast_path, g_slot_count,
        g_config.tiled_subrects, g_feature_width, g_feature_height);
    return true;
}

void ReleaseDeviceObjects()
{
    for (auto &slot : g_slots)
    {
        Release(slot.list);
        Release(slot.allocator);
    }
    if (g_fence_event != nullptr)
    {
        CloseHandle(g_fence_event);
        g_fence_event = nullptr;
    }
    Release(g_fence);
    Release(g_queue);
    Release(g_device);
    Release(g_budget_adapter);
    g_has_selected_adapter = false;
    g_selected_adapter = {};
    g_fence_value = 0;
}

bool SameLuid(const LUID &left, const LUID &right)
{
    return left.HighPart == right.HighPart && left.LowPart == right.LowPart;
}

std::vector<DlssnrAdapterInfo> EnumerateAdapterInfos()
{
    std::vector<DlssnrAdapterInfo> infos;
    IDXGIFactory1 *base_factory = nullptr;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&base_factory))))
        return infos;
    IDXGIFactory6 *preference_factory = nullptr;
    base_factory->QueryInterface(IID_PPV_ARGS(&preference_factory));
    for (UINT index = 0;; ++index)
    {
        IDXGIAdapter1 *adapter = nullptr;
        const HRESULT enumerated = preference_factory != nullptr
            ? preference_factory->EnumAdapterByGpuPreference(
                index, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, IID_PPV_ARGS(&adapter))
            : base_factory->EnumAdapters1(index, &adapter);
        if (enumerated == DXGI_ERROR_NOT_FOUND)
            break;
        if (FAILED(enumerated) || adapter == nullptr)
            continue;
        DXGI_ADAPTER_DESC1 description = {};
        if (SUCCEEDED(adapter->GetDesc1(&description)))
        {
            DlssnrAdapterInfo info = {};
            info.struct_size = sizeof(info);
            info.preference_index = index;
            info.vendor_id = description.VendorId;
            info.device_id = description.DeviceId;
            info.subsys_id = description.SubSysId;
            info.revision = description.Revision;
            info.dedicated_video_memory = description.DedicatedVideoMemory;
            info.luid_high = description.AdapterLuid.HighPart;
            info.luid_low = description.AdapterLuid.LowPart;
            if (description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)
                info.flags |= kAdapterSoftware;
            if (SUCCEEDED(D3D12CreateDevice(
                    adapter, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device), nullptr)))
                info.flags |= kAdapterD3D12Level11;
            if (SUCCEEDED(D3D12CreateDevice(
                    adapter, D3D_FEATURE_LEVEL_12_0, __uuidof(ID3D12Device), nullptr)))
                info.flags |= kAdapterD3D12Level12;
            wcsncpy_s(info.description, description.Description, _TRUNCATE);
            infos.push_back(info);
        }
        adapter->Release();
    }
    Release(preference_factory);
    Release(base_factory);
    return infos;
}

bool OpenAdapterByLuid(const LUID &luid, IDXGIAdapter1 **result)
{
    if (result == nullptr)
        return false;
    *result = nullptr;
    IDXGIFactory1 *base_factory = nullptr;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&base_factory))))
        return false;
    IDXGIFactory4 *luid_factory = nullptr;
    base_factory->QueryInterface(IID_PPV_ARGS(&luid_factory));
    if (luid_factory != nullptr)
        luid_factory->EnumAdapterByLuid(luid, IID_PPV_ARGS(result));
    if (*result == nullptr)
    {
        for (UINT index = 0;; ++index)
        {
            IDXGIAdapter1 *adapter = nullptr;
            const HRESULT enumerated = base_factory->EnumAdapters1(index, &adapter);
            if (enumerated == DXGI_ERROR_NOT_FOUND)
                break;
            if (FAILED(enumerated) || adapter == nullptr)
                continue;
            DXGI_ADAPTER_DESC1 description = {};
            adapter->GetDesc1(&description);
            if (SameLuid(description.AdapterLuid, luid))
            {
                *result = adapter;
                break;
            }
            adapter->Release();
        }
    }
    Release(luid_factory);
    Release(base_factory);
    return *result != nullptr;
}

bool SetupD3D12()
{
    g_has_selected_adapter = false;
    g_selected_adapter = {};
    const std::vector<DlssnrAdapterInfo> adapters = EnumerateAdapterInfos();
    const DlssnrAdapterInfo *selected = nullptr;
    for (const auto &adapter : adapters)
    {
        Log("adapter[%u]: %ls vendor=%04X device=%04X luid=%08lX:%08lX vram=%llu MiB flags=0x%X",
            adapter.preference_index, adapter.description, adapter.vendor_id, adapter.device_id,
            adapter.luid_high, adapter.luid_low,
            adapter.dedicated_video_memory / (1024ull * 1024ull), adapter.flags);
        const LUID luid = {adapter.luid_low, adapter.luid_high};
        const bool requested = !g_adapter_explicit || SameLuid(luid, g_requested_adapter_luid);
        const bool usable = adapter.vendor_id == kNvidiaVendorId &&
            !(adapter.flags & kAdapterSoftware) && (adapter.flags & kAdapterD3D12Level11);
        if (selected == nullptr && requested && usable)
            selected = &adapter;
    }
    if (selected == nullptr)
    {
        if (g_adapter_explicit)
            Log("requested NVIDIA adapter is unavailable or does not support D3D12");
        else
            Log("no compatible NVIDIA D3D12 adapter found");
        return false;
    }
    const LUID selected_luid = {selected->luid_low, selected->luid_high};
    IDXGIAdapter1 *adapter = nullptr;
    if (!OpenAdapterByLuid(selected_luid, &adapter))
    {
        Log("selected adapter disappeared before D3D12 device creation");
        return false;
    }
    HRESULT result = D3D12CreateDevice(
        adapter, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&g_device));
    D3D_FEATURE_LEVEL feature_level = D3D_FEATURE_LEVEL_12_0;
    if (FAILED(result))
    {
        feature_level = D3D_FEATURE_LEVEL_11_0;
        result = D3D12CreateDevice(
            adapter, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&g_device));
    }
    adapter->QueryInterface(IID_PPV_ARGS(&g_budget_adapter));
    adapter->Release();
    if (FAILED(result) || g_device == nullptr)
    {
        Log("D3D12 device creation failed for selected NVIDIA adapter: 0x%08X", result);
        return false;
    }
    g_selected_adapter = *selected;
    g_has_selected_adapter = true;
    Log("selected adapter: %ls policy=%s luid=%08lX:%08lX feature_level=0x%X",
        selected->description, g_adapter_explicit ? "explicit" : "auto-nvidia",
        selected->luid_high, selected->luid_low, feature_level);

    D3D12_COMMAND_QUEUE_DESC queue_description = {};
    queue_description.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    if (FAILED(g_device->CreateCommandQueue(
            &queue_description, __uuidof(ID3D12CommandQueue), reinterpret_cast<void **>(&g_queue))))
        return false;
    for (auto &slot : g_slots)
    {
        if (FAILED(g_device->CreateCommandAllocator(
                D3D12_COMMAND_LIST_TYPE_DIRECT, __uuidof(ID3D12CommandAllocator),
                reinterpret_cast<void **>(&slot.allocator))))
            return false;
        if (FAILED(g_device->CreateCommandList(
                0, D3D12_COMMAND_LIST_TYPE_DIRECT, slot.allocator, nullptr,
                __uuidof(ID3D12GraphicsCommandList), reinterpret_cast<void **>(&slot.list))))
            return false;
        slot.list->Close();
    }
    if (FAILED(g_device->CreateFence(
            0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence), reinterpret_cast<void **>(&g_fence))))
        return false;
    g_fence_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    return g_fence_event != nullptr;
}

std::wstring DirectoryOf(const wchar_t *path)
{
    std::wstring result = path != nullptr ? path : L"";
    const size_t slash = result.find_last_of(L"\\/");
    if (slash == std::wstring::npos)
        return L".";
    result.resize(slash + 1);
    return result;
}

void ShutdownInternal()
{
    ReleaseFeatureResources();
    if (g_initialized && g_device != nullptr)
    {
        __try
        {
            NVSDK_NGX_D3D12_Shutdown1(g_device);
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
            Log("NGX shutdown raised SEH 0x%08X (ignored)", GetExceptionCode());
        }
    }
    g_initialized = false;
    RestoreCallerHook();
    ReleaseDeviceObjects();
    if (g_runtime != nullptr)
    {
        FreeLibrary(g_runtime);
        g_runtime = nullptr;
    }
    g_init_ext = nullptr;
    g_create_feature = nullptr;
    g_evaluate_feature = nullptr;
    g_release_feature = nullptr;
    g_width = 0;
    g_height = 0;
    g_needs_recreate = false;
    if (g_log != nullptr)
    {
        fclose(g_log);
        g_log = nullptr;
    }
}

} // namespace

extern "C" __declspec(dllexport) int dlssnr_enumerate_adapters(
    DlssnrAdapterInfo *items, int capacity)
{
    const std::vector<DlssnrAdapterInfo> adapters = EnumerateAdapterInfos();
    if (items != nullptr && capacity > 0)
    {
        if (items[0].struct_size != sizeof(DlssnrAdapterInfo))
            return -1;
        const int count = std::min(capacity, static_cast<int>(adapters.size()));
        for (int index = 0; index < count; ++index)
            items[index] = adapters[index];
    }
    return static_cast<int>(adapters.size());
}

extern "C" __declspec(dllexport) void dlssnr_select_adapter(
    long luid_high, unsigned long luid_low)
{
    g_adapter_explicit = true;
    g_requested_adapter_luid.HighPart = luid_high;
    g_requested_adapter_luid.LowPart = luid_low;
}

extern "C" __declspec(dllexport) void dlssnr_select_adapter_auto()
{
    g_adapter_explicit = false;
    g_requested_adapter_luid = {};
}

extern "C" __declspec(dllexport) int dlssnr_selected_adapter(
    DlssnrAdapterInfo *info)
{
    if (!g_has_selected_adapter || info == nullptr ||
        info->struct_size != sizeof(DlssnrAdapterInfo))
        return 0;
    *info = g_selected_adapter;
    return 1;
}

extern "C" __declspec(dllexport) void dlssnr_configure(
    int zero_guidance_fast_path, int persistent_buffers, int merged_submission,
    int in_flight, int auto_fallback)
{
    HostConfig next = g_config;
    next.zero_guidance_fast_path = zero_guidance_fast_path != 0;
    next.persistent_buffers = persistent_buffers != 0;
    next.merged_submission = merged_submission != 0;
    next.in_flight = std::clamp(in_flight, 1, kMaxSlots);
    next.auto_fallback = auto_fallback != 0;
    const bool changed =
        next.zero_guidance_fast_path != g_config.zero_guidance_fast_path ||
        next.persistent_buffers != g_config.persistent_buffers ||
        next.merged_submission != g_config.merged_submission ||
        next.auto_fallback != g_config.auto_fallback ||
        next.in_flight != g_config.in_flight;
    if (changed && g_ready)
        g_needs_recreate = true;
    g_config = next;
}

extern "C" __declspec(dllexport) void dlssnr_configure_tiling(
    int enabled, int tile_width, int tile_height)
{
    const bool next_enabled = enabled != 0;
    const int next_width = std::clamp(tile_width, 64, 8192);
    const int next_height = std::clamp(tile_height, 64, 8192);
    const bool changed =
        next_enabled != g_config.tiled_subrects ||
        next_width != g_config.tile_width ||
        next_height != g_config.tile_height;
    if (changed && g_ready)
        g_needs_recreate = true;
    g_config.tiled_subrects = next_enabled;
    g_config.tile_width = next_width;
    g_config.tile_height = next_height;
}

extern "C" __declspec(dllexport) void dlssnr_configure_format(
    int frame_format, int color_profile)
{
    const FrameFormat next_format = frame_format == static_cast<int>(FrameFormat::Rgba16Float)
        ? FrameFormat::Rgba16Float
        : FrameFormat::Rgba8;
    const ColorProfile next_profile = static_cast<ColorProfile>(
        std::clamp(color_profile, static_cast<int>(ColorProfile::Srgb),
            static_cast<int>(ColorProfile::Hdr10Hlg)));
    if ((next_format != g_frame_format || next_profile != g_color_profile) && g_ready)
        g_needs_recreate = true;
    g_frame_format = next_format;
    g_color_profile = next_profile;
}

extern "C" __declspec(dllexport) int dlssnr_capabilities()
{
    int result = 1; // v2 host
    result |= 8; // inactive depth accepts null and reuses an immutable zero texture
    if (!g_config.tiled_subrects && g_config.merged_submission && g_config.persistent_buffers)
        result |= 2; // asynchronous enqueue/dequeue
    if (g_config.tiled_subrects)
        result |= 4; // Feature 18 subrect tiling
    return result;
}

extern "C" __declspec(dllexport) int dlssnr_queue_capacity()
{
    // Report allocated slots, never just the configured/requested upper bound.
    return g_ready ? g_slot_count : 1;
}

extern "C" __declspec(dllexport) void dlssnr_set_options(
    int preset, int style, float intensity, float local_tone,
    float local_struct, float skin_struct, int use_auto_mask,
    int ui_correction, int guidance_mode, int depth_convention,
    float motion_scale_x, float motion_scale_y)
{
    g_options.preset = static_cast<unsigned int>(std::max(preset, 0));
    g_options.style = static_cast<unsigned int>(std::max(style, 0));
    g_options.intensity = intensity;
    g_options.local_tone = local_tone;
    g_options.local_struct = local_struct;
    g_options.skin_struct = skin_struct;
    g_options.use_auto_mask = use_auto_mask != 0 ? 1u : 0u;
    g_options.ui_correction = ui_correction != 0 ? 1u : 0u;
    g_options.guidance_mode = std::clamp(guidance_mode, 0, 3);
    g_options.depth_convention = depth_convention;
    g_options.motion_scale_x = motion_scale_x;
    g_options.motion_scale_y = motion_scale_y;
}

extern "C" __declspec(dllexport) int dlssnr_init(
    int width, int height, int preset, const wchar_t *runtime_path, const wchar_t *log_path)
{
    ShutdownInternal();
    if (log_path != nullptr)
        _wfopen_s(&g_log, log_path, L"w");
    Log("=== dlssnr_host_v2 init w=%d h=%d preset=%d ===", width, height, preset);
    Log("frame contract: format=%s profile=%d hdr=%d",
        g_frame_format == FrameFormat::Rgba16Float ? "RGBA16F" : "RGBA8",
        static_cast<int>(g_color_profile), IsHdrProfile());
    if (!SetupD3D12())
    {
        Log("D3D12 setup failed");
        ShutdownInternal();
        return 0;
    }
    g_runtime = LoadLibraryW(runtime_path);
    if (g_runtime == nullptr)
    {
        Log("load nvngx_dlssnr.dll failed %u", GetLastError());
        ShutdownInternal();
        return 0;
    }
    g_init_ext = reinterpret_cast<PFN_InitExt>(GetProcAddress(g_runtime, "NVSDK_NGX_D3D12_Init_Ext"));
    g_create_feature = reinterpret_cast<PFN_CreateFeature>(GetProcAddress(g_runtime, "NVSDK_NGX_D3D12_CreateFeature"));
    g_evaluate_feature = reinterpret_cast<PFN_EvaluateFeature>(GetProcAddress(g_runtime, "NVSDK_NGX_D3D12_EvaluateFeature"));
    g_release_feature = reinterpret_cast<PFN_ReleaseFeature>(GetProcAddress(g_runtime, "NVSDK_NGX_D3D12_ReleaseFeature"));
    if (g_init_ext == nullptr || g_create_feature == nullptr ||
        g_evaluate_feature == nullptr || g_release_feature == nullptr)
    {
        Log("missing runtime exports");
        ShutdownInternal();
        return 0;
    }

    const std::wstring directory = DirectoryOf(runtime_path);
    NVSDK_NGX_Result initialized = NVSDK_NGX_D3D12_Init_with_ProjectID(
        "7c134ab9-9677-4af5-a2b2-bca943350861", NVSDK_NGX_ENGINE_TYPE_CUSTOM,
        "Resolve-DLSS5-1", directory.c_str(), g_device, nullptr, NVSDK_NGX_Version_API);
    Log("Init_with_ProjectID -> 0x%08X", initialized);
    if (!NgxSucceeded(initialized) || !InstallCallerHook(g_runtime))
    {
        Log("caller/static initialization failed");
        ShutdownInternal();
        return 0;
    }
    initialized = SafeInitExt(
        g_init_ext, 0x0876232Cull, directory.c_str(), g_device,
        NVSDK_NGX_Version_API, nullptr);
    Log("runtime Init_Ext -> 0x%08X", initialized);
    if (!NgxSucceeded(initialized))
    {
        ShutdownInternal();
        return 0;
    }
    g_initialized = true;
    g_width = static_cast<UINT>(std::max(width, 0));
    g_height = static_cast<UINT>(std::max(height, 0));
    g_options.preset = static_cast<unsigned int>(std::max(preset, 0));
    return 1;
}

extern "C" __declspec(dllexport) int dlssnr_create_feature(int width, int height, int preset)
{
    Log("=== create Feature 18 v2 w=%d h=%d preset=%d ===", width, height, preset);
    return CreateFeatureResources(
        static_cast<UINT>(std::max(width, 0)),
        static_cast<UINT>(std::max(height, 0)), preset) ? 1 : 0;
}

extern "C" __declspec(dllexport) int dlssnr_resize(int width, int height, int preset)
{
    Log("=== resize Feature 18 v2 w=%d h=%d preset=%d ===", width, height, preset);
    return CreateFeatureResources(
        static_cast<UINT>(std::max(width, 0)),
        static_cast<UINT>(std::max(height, 0)), preset) ? 1 : 0;
}

extern "C" __declspec(dllexport) int dlssnr_enqueue(
    const void *color_rgba8, const void *motion_float2, const void *depth_float, int reset)
{
    if (g_needs_recreate && !CreateFeatureResources(g_width, g_height, static_cast<int>(g_options.preset)))
        return 0;
    return EnqueueFrame(
        color_rgba8, static_cast<const float *>(motion_float2),
        static_cast<const float *>(depth_float), reset != 0) ? 1 : 0;
}

extern "C" __declspec(dllexport) int dlssnr_dequeue(void *output_rgba8)
{
    return DequeueFrame(output_rgba8) ? 1 : 0;
}

extern "C" __declspec(dllexport) int dlssnr_pending()
{
    return g_pending_count;
}

extern "C" __declspec(dllexport) int dlssnr_process(
    const void *color_rgba8, const void *motion_float2, const void *depth_float,
    void *output_rgba8, int reset)
{
    if (!g_ready || color_rgba8 == nullptr || output_rgba8 == nullptr)
        return 0;
    if (g_needs_recreate && !CreateFeatureResources(g_width, g_height, static_cast<int>(g_options.preset)))
        return 0;

    bool ok = false;
    if (g_config.tiled_subrects)
        ok = ProcessTiledSubrects(
            color_rgba8, static_cast<const float *>(motion_float2),
            static_cast<const float *>(depth_float), output_rgba8);
    else if (g_config.merged_submission && g_config.persistent_buffers)
        ok = EnqueueFrame(
                 color_rgba8, static_cast<const float *>(motion_float2),
                 static_cast<const float *>(depth_float), reset != 0) &&
             DequeueFrame(output_rgba8);
    else if (g_config.merged_submission)
        ok = ProcessMergedTransient(
            color_rgba8, static_cast<const float *>(motion_float2),
            static_cast<const float *>(depth_float), output_rgba8, reset != 0);
    else
        ok = ProcessCompatibility(
            color_rgba8, static_cast<const float *>(motion_float2),
            static_cast<const float *>(depth_float), output_rgba8, reset != 0);

    if (!ok && !g_config.tiled_subrects && g_config.auto_fallback &&
        g_config.merged_submission && g_pending_count == 0)
    {
        Log("merged path failed; retrying current frame with compatibility submission");
        ok = ProcessCompatibility(
            color_rgba8, static_cast<const float *>(motion_float2),
            static_cast<const float *>(depth_float), output_rgba8, true);
    }
    return ok ? 1 : 0;
}

extern "C" __declspec(dllexport) void dlssnr_shutdown()
{
    ShutdownInternal();
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
        DisableThreadLibraryCalls(instance);
    return TRUE;
}
