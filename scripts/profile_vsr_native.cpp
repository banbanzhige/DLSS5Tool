// Research instrumentation only. Compile separately; never replace runtime DLLs.
// Reuse the existing probe's explicit read-only NGX runtime search path.
#include "gpu_vsr_probe.cpp"
#include <chrono>

namespace {
ID3D12QueryHeap* profile_queries = nullptr;
ID3D12Resource* profile_results = nullptr;
UINT64* profile_map = nullptr;
UINT64 profile_frequency = 0;
using ProfileClock = std::chrono::steady_clock;
double Milliseconds(ProfileClock::time_point a, ProfileClock::time_point b) {
    return std::chrono::duration<double, std::milli>(b - a).count();
}
}

extern "C" __declspec(dllexport) int probe_profile_init() {
    if (!g_device || profile_queries) return 0;
    D3D12_QUERY_HEAP_DESC desc{};
    desc.Type = D3D12_QUERY_HEAP_TYPE_TIMESTAMP;
    desc.Count = 4;
    if (FAILED(g_device->CreateQueryHeap(&desc, IID_PPV_ARGS(&profile_queries)))) return 0;
    auto heap = HeapProperties(D3D12_HEAP_TYPE_READBACK);
    auto buffer = BufferDescription(4 * sizeof(UINT64));
    if (FAILED(g_device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &buffer,
        D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&profile_results)))) return 0;
    D3D12_RANGE range{0, 4 * sizeof(UINT64)};
    if (FAILED(profile_results->Map(0, &range, reinterpret_cast<void**>(&profile_map)))) return 0;
    return SUCCEEDED(g_queue->GetTimestampFrequency(&profile_frequency)) && profile_frequency;
}

extern "C" __declspec(dllexport) int probe_profile_frame(
    const void* input, void* output, double* timings) {
    if (!g_feature || !profile_queries || !input || !timings) return 0;
    const auto start = ProfileClock::now();
    CopyInput(input);
    const auto packed = ProfileClock::now();
    if (!BeginCommands()) return 0;
    g_list->EndQuery(profile_queries, D3D12_QUERY_TYPE_TIMESTAMP, 0);
    RecordUpload();
    g_list->EndQuery(profile_queries, D3D12_QUERY_TYPE_TIMESTAMP, 1);
    NVSDK_NGX_D3D12_VSR_Eval_Params params{};
    params.pInput = g_input; params.pOutput = g_output;
    params.InputSubrectSize = {g_input_width, g_input_height};
    params.OutputSubrectSize = {g_output_width, g_output_height};
    params.QualityLevel = static_cast<NVSDK_NGX_VSR_QualityLevel>(g_quality);
    const auto result = NGX_D3D12_EVALUATE_VSR_EXT(g_list, g_feature, g_parameters, &params);
    if (NVSDK_NGX_FAILED(result)) { g_list->Close(); return 0; }
    g_list->EndQuery(profile_queries, D3D12_QUERY_TYPE_TIMESTAMP, 2);
    if (output) RecordReadback();
    g_list->EndQuery(profile_queries, D3D12_QUERY_TYPE_TIMESTAMP, 3);
    g_list->ResolveQueryData(profile_queries, D3D12_QUERY_TYPE_TIMESTAMP, 0, 4, profile_results, 0);
    const auto recorded = ProfileClock::now();
    if (!ExecuteAndWait()) return 0;
    const auto waited = ProfileClock::now();
    if (output) CopyOutput(output);
    const auto copied = ProfileClock::now();
    timings[0] = Milliseconds(start, packed);
    timings[1] = Milliseconds(packed, recorded);
    timings[2] = Milliseconds(recorded, waited);
    timings[3] = Milliseconds(waited, copied);
    const double tick_ms = 1000.0 / static_cast<double>(profile_frequency);
    for (int i = 0; i != 3; ++i)
        timings[4 + i] = static_cast<double>(profile_map[i + 1] - profile_map[i]) * tick_ms;
    timings[7] = static_cast<double>(profile_map[3] - profile_map[0]) * tick_ms;
    return 1;
}

extern "C" __declspec(dllexport) void probe_profile_release() {
    if (profile_results && profile_map) profile_results->Unmap(0, nullptr);
    profile_map = nullptr;
    Release(profile_results);
    Release(profile_queries);
}
