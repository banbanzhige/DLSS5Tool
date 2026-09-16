// Research only: reuse VSR implementation without touching the deployed host.
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <nvsdk_ngx.h>
static NVSDK_NGX_Result ProbeInit(unsigned long long app, const wchar_t* path, ID3D12Device* device) {
    // The probe DLL is deliberately not next to nvngx_vsr.dll. The application
    // data path is NOT a feature search path; declare the read-only runtime.
    const wchar_t* paths[] = {path};
    NVSDK_NGX_FeatureCommonInfo info{};
    info.PathListInfo = {paths, 1};
    return NVSDK_NGX_D3D12_Init(app, path, device, &info);
}
#define NVSDK_NGX_D3D12_Init ProbeInit
#include "../native/vsr_host/vsr_host.cpp"
#undef NVSDK_NGX_D3D12_Init

namespace {
ID3D12Resource* resident_output = nullptr;
HANDLE resident_handle = nullptr;

void RecordResidentOutput() {
    auto source = Transition(g_output, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
    auto target = Transition(resident_output, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
    g_list->ResourceBarrier(1, &source);
    g_list->ResourceBarrier(1, &target);
    D3D12_TEXTURE_COPY_LOCATION src{}, dst{};
    src.pResource = g_output; src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    dst.pResource = resident_output; dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    dst.PlacedFootprint = g_output_footprint;
    g_list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    source = Transition(g_output, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
    target = Transition(resident_output, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COMMON);
    g_list->ResourceBarrier(1, &source);
    g_list->ResourceBarrier(1, &target);
}
}

extern "C" __declspec(dllexport) int probe_vsr_share(HANDLE* handle, LUID* luid) {
    if (!g_device || !g_output || g_hdr || resident_output || !handle || !luid) return 0;
    auto heap = HeapProperties(D3D12_HEAP_TYPE_DEFAULT);
    auto desc = BufferDescription(g_output_staging_size);
    if (FAILED(g_device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_SHARED, &desc,
        D3D12_RESOURCE_STATE_COMMON, nullptr, IID_PPV_ARGS(&resident_output)))) return 0;
    if (FAILED(g_device->CreateSharedHandle(resident_output, nullptr, GENERIC_ALL, nullptr, &resident_handle))) return 0;
    *handle = resident_handle; *luid = g_device->GetAdapterLuid();
    return 1;
}

extern "C" __declspec(dllexport) int probe_vsr_process(const void* input, void* output, int shared) {
    if (!g_feature || !input || (!output && !shared) || (shared && !resident_output)) return 0;
    CopyInput(input);
    if (!BeginCommands()) return 0;
    RecordUpload();
    NVSDK_NGX_D3D12_VSR_Eval_Params p{};
    p.pInput = g_input; p.pOutput = g_output;
    p.InputSubrectSize = {g_input_width, g_input_height};
    p.OutputSubrectSize = {g_output_width, g_output_height};
    p.QualityLevel = static_cast<NVSDK_NGX_VSR_QualityLevel>(g_quality);
    auto result = NGX_D3D12_EVALUATE_VSR_EXT(g_list, g_feature, g_parameters, &p);
    if (NVSDK_NGX_FAILED(result)) { g_list->Close(); return 0; }
    if (shared) RecordResidentOutput();
    if (output) RecordReadback();
    if (!ExecuteAndWait()) return 0;
    if (output) CopyOutput(output);
    return 1;
}

extern "C" __declspec(dllexport) void probe_vsr_close_share() {
    // Serial harness has completed both producer and consumer before this call.
    Release(resident_output);
    if (resident_handle) CloseHandle(resident_handle);
    resident_handle = nullptr;
}
