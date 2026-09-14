// Isolated, public-NGX DLSSG probe. Reuse existing D3D12 allocation helpers;
// Never initialize Feature18 or install its caller hook. Optional pinned Ada
// research compatibility changes the isolated process image only, never disk.
#include "../native/host_v2/dlssnr_host_v2.cpp"
#include <nvsdk_ngx_helpers_dlssg.h>
#include <cmath>
#include <stdexcept>
#include "dlssg_ada_gate.h"
#include "rtxmfg_temporal/dlssg_provider_policy.cpp"
#include "rtxmfg_temporal/midpoint_fix.cpp"
#pragma comment(lib, "version.lib")

namespace probe {
void Check(bool ok, const char* step) {
    if (!ok) throw std::runtime_error(step);
}
void Ngx(NVSDK_NGX_Result value, const char* step) {
    printf("%s=0x%08X\n", step, unsigned(value)); fflush(stdout);
    Check(NgxSucceeded(value), step);
}
void Callback(const char* message, NVSDK_NGX_Logging_Level, NVSDK_NGX_Feature) {
    fprintf(stderr, "%s\n", message);
}
struct Texture {
    ID3D12Resource* resource = nullptr;
    Staging upload, readback;
    void Init(DXGI_FORMAT format, bool output = false) {
        resource = CreateTexture(format, output ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE);
        Check(resource != nullptr, "texture");
        Check(CreateStaging(resource, D3D12_HEAP_TYPE_UPLOAD, upload), "upload allocation");
        if (output) Check(CreateStaging(resource, D3D12_HEAP_TYPE_READBACK, readback), "readback allocation");
    }
    void State(ID3D12GraphicsCommandList* list, D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
        auto barrier = Transition(resource, before, after); list->ResourceBarrier(1, &barrier);
    }
    ~Texture() { ReleaseStaging(upload); ReleaseStaging(readback); Release(resource); }
};
struct Validity {
    ID3D12Resource *gpu = nullptr, *upload = nullptr, *readback = nullptr;
    void Init() {
        auto desc = BufferDescription(4);
        desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
        auto heap = HeapProperties(D3D12_HEAP_TYPE_DEFAULT);
        Check(SUCCEEDED(g_device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
            D3D12_RESOURCE_STATE_COMMON, nullptr, IID_PPV_ARGS(&gpu))), "validity gpu");
        desc.Flags = D3D12_RESOURCE_FLAG_NONE;
        heap = HeapProperties(D3D12_HEAP_TYPE_UPLOAD);
        Check(SUCCEEDED(g_device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
            D3D12_RESOURCE_STATE_GENERIC_READ, nullptr, IID_PPV_ARGS(&upload))), "validity upload");
        heap = HeapProperties(D3D12_HEAP_TYPE_READBACK);
        Check(SUCCEEDED(g_device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
            D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&readback))), "validity readback");
        void* data = nullptr; D3D12_RANGE empty = {0, 0};
        Check(SUCCEEDED(upload->Map(0, &empty, &data)), "validity map");
        memset(data, 0xcd, 4); upload->Unmap(0, nullptr);
    }
    void Before(ID3D12GraphicsCommandList* list, bool startGroup) {
        if (!startGroup) {
            auto b = Transition(gpu, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
            list->ResourceBarrier(1, &b); return;
        }
        auto b = Transition(gpu, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
        list->ResourceBarrier(1, &b); list->CopyBufferRegion(gpu, 0, upload, 0, 4);
        b = Transition(gpu, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        list->ResourceBarrier(1, &b);
    }
    void After(ID3D12GraphicsCommandList* list) {
        auto b = Transition(gpu, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->ResourceBarrier(1, &b); list->CopyBufferRegion(readback, 0, gpu, 0, 4);
        b = Transition(gpu, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
        list->ResourceBarrier(1, &b);
    }
    unsigned Read() {
        void* data = nullptr; D3D12_RANGE range = {0, 4};
        Check(SUCCEEDED(readback->Map(0, &range, &data)), "validity readback map");
        const unsigned value = *static_cast<unsigned char*>(data);
        D3D12_RANGE empty = {0, 0}; readback->Unmap(0, &empty); return value;
    }
    ~Validity() { Release(gpu); Release(upload); Release(readback); }
};
float hdrAmplitude = 3.f;
float Pixel(float x, float y, int channel, bool hdr) {
    const float v = .5f + .22f * std::sin(x * .041f + channel) + .18f * std::sin(y * .063f + x * .017f);
    return channel == 3 ? 1.f : v * (hdr ? hdrAmplitude : 1.f);
}
void Fill(std::vector<unsigned char>& bytes, double time, bool hdr) {
    for (UINT y = 0; y < g_height; ++y) for (UINT x = 0; x < g_width; ++x) for (int c = 0; c < 4; ++c) {
        const size_t i = (size_t(y) * g_width + x) * 4 + c;
        const float v = Pixel(float(x) - float(time) * 8.f, float(y), c, hdr);
        if (hdr) {
            const auto half = DirectX::PackedVector::XMConvertFloatToHalf(v);
            memcpy(bytes.data() + i * 2, &half, 2);
        } else bytes[i] = static_cast<unsigned char>(std::round(v * 255.f));
    }
}
float Read(const std::vector<unsigned char>& bytes, size_t i, bool hdr) {
    if (!hdr) return bytes[i] / 255.f;
    uint16_t half; memcpy(&half, bytes.data() + i * 2, 2);
    return DirectX::PackedVector::XMConvertHalfToFloat(half);
}
int Run(int argc, wchar_t** argv) {
    Check(argc == 9, "usage: probe runtime-dir log-dir width height multiplier sdr|hdr|hdr-coded stock|ada-compat ngx|process");
    g_width = _wtoi(argv[3]); g_height = _wtoi(argv[4]);
    const unsigned multiplier = _wtoi(argv[5]);
    Check(g_width >= 128 && g_height >= 128 && g_width <= 4096 && g_height <= 2160 && multiplier >= 2 && multiplier <= 6, "arguments");
    const bool coded = !wcscmp(argv[6], L"hdr-coded");
    const bool hdr = coded || !wcscmp(argv[6], L"hdr");
    hdrAmplitude = coded ? 1.f : 3.f;
    Check(hdr || !wcscmp(argv[6], L"sdr"), "color mode");
    const bool presetB = !wcscmp(argv[7], L"ada-preset-b");
    const bool compat = presetB || !wcscmp(argv[7], L"ada-compat");
    Check(compat || !wcscmp(argv[7], L"stock"), "compatibility mode");
    const bool processExit = !wcscmp(argv[8], L"process");
    Check(processExit || !wcscmp(argv[8], L"ngx"), "shutdown mode");
    g_frame_format = hdr ? FrameFormat::Rgba16Float : FrameFormat::Rgba8;
    g_log = stdout;
    Check(SetupD3D12(), "device setup");
    if (presetB) {
        Check(g_selected_adapter.vendor_id == 0x10de && g_selected_adapter.device_id == 0x2783, "preset device identity");
        const std::wstring path = std::wstring(argv[1]) + L"\\nvngx_dlssg.dll";
        const HMODULE provider = LoadLibraryExW(path.c_str(), nullptr, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        Check(dlssg_ada_gate::PresetB(provider), "preset B research callsite");
        printf("preset_b=isolated_callsite_before_init\n"); fflush(stdout);
    }
    const wchar_t* paths[] = {argv[1]};
    NVSDK_NGX_FeatureCommonInfo info = {};
    info.PathListInfo = {paths, 1};
    info.LoggingInfo = {Callback, NVSDK_NGX_LOGGING_LEVEL_VERBOSE, true};
    Ngx(NVSDK_NGX_D3D12_Init_with_ProjectID("3bfc79f4-8b20-4815-b317-e9da984d942f", NVSDK_NGX_ENGINE_TYPE_CUSTOM,
        "DLSS5Tool-DLSSG-Probe", argv[2], g_device, &info), "init");
    NVSDK_NGX_Parameter* params = nullptr;
    Ngx(NVSDK_NGX_D3D12_GetCapabilityParameters(&params), "capabilities");
    for (const char* name : {NVSDK_NGX_Parameter_FrameGeneration_Available, NVSDK_NGX_Parameter_FrameGeneration_FeatureInitResult,
            NVSDK_NGX_DLSSG_Parameter_MultiFrameCountMax}) {
        int v = -1; const auto r = params->Get(name, &v);
        printf("capability %s result=0x%08X value=%d\n", name, unsigned(r), v);
    }
    if (compat) {
        Check(dlssg_ada_gate::Apply(GetModuleHandleW(L"nvngx_dlssg.dll"), g_selected_adapter.vendor_id,
            g_selected_adapter.device_id), "pinned Ada compatibility validation");
        printf("ada_compatibility=process_image_only\n"); fflush(stdout);
        midpoint_fix::SetLogCallback([](const wchar_t* message) { fprintf(stderr, "%ls\n", message); });
        Check(midpoint_fix::ObserveD3D12Device(g_device), "temporal adapter identity");
        const bool corrected = midpoint_fix::PatchProvider(GetModuleHandleW(L"nvngx_dlssg.dll"), nullptr);
        printf("temporal_correction=%d failure=%u\n", corrected, midpoint_fix::FailureCode()); fflush(stdout);
        Check(corrected, "temporal correction");
    }
    std::array<Texture, 2> colors;
    std::array<Texture, 5> outputs;
    Texture motion, depth;
    Validity validity; validity.Init();
    for (auto& color : colors) color.Init(FrameDxgiFormat());
    for (unsigned i=0; i<multiplier-1; ++i) outputs[i].Init(FrameDxgiFormat(), true);
    motion.Init(DXGI_FORMAT_R16G16_FLOAT);
    depth.Init(DXGI_FORMAT_R32_FLOAT);
    NVSDK_NGX_Handle* feature = nullptr;
    NVSDK_NGX_DLSSG_Create_Params create = {g_width, g_height, unsigned(FrameDxgiFormat()), g_width, g_height, false};
    params->Set(NVSDK_NGX_DLSSG_Parameter_MultiFrameCount, multiplier - 1);
    // MultiFrameCountMax is an output capability, never an override knob.
    auto& slot = g_slots[0];
    Check(BeginCommands(slot), "begin create");
    Ngx(NGX_D3D12_CREATE_DLSSG(slot.list, 1, 1, &feature, params, &create), "create");
    Check(SubmitCommands(slot, false) && WaitFence(slot.fence_value, 15000), "create fence");
    Check(feature != nullptr, "feature handle");
    if (const HMODULE provider = GetModuleHandleW(L"nvngx_dlssg.dll")) {
        wchar_t loaded[32768] = {}; GetModuleFileNameW(provider, loaded, 32768);
        printf("loaded_provider=%ls\n", loaded);
    }
    // Exact screen-space translation, constant planar depth and static
    // orthographic camera. These are ground truth for this synthetic scene,
    // NOT claims that a real video's camera/depth can be replaced by constants.
    std::vector<uint16_t> mv(size_t(g_width) * g_height * 2, 0);
    for (size_t i = 0; i < mv.size(); i += 2) mv[i] = DirectX::PackedVector::XMConvertFloatToHalf(-8.f);
    std::vector<float> dp(size_t(g_width) * g_height, .5f);
    CopyRowsToStaging(motion.upload, mv.data(), size_t(g_width) * 4);
    CopyRowsToStaging(depth.upload, dp.data(), size_t(g_width) * 4);
    std::vector<unsigned char> input(size_t(g_height) * FrameRowPitch()), actual(input.size());
    std::vector<unsigned char> previous(input.size()), expected(input.size());
    NVSDK_NGX_D3D12_DLSSG_Eval_Params eval = {};
    eval.pDepth = depth.resource; eval.pMVecs = motion.resource;
    eval.pOutputDisableInterpolation = validity.gpu;
    NVSDK_NGX_DLSSG_Opt_Eval_Params opts = {};
    for (int i = 0; i < 4; ++i) {
        opts.cameraViewToClip[i][i] = opts.clipToCameraView[i][i] = opts.clipToLensClip[i][i] = 1;
        opts.clipToPrevClip[i][i] = opts.prevClipToClip[i][i] = 1;
    }
    opts.cameraUp[1] = opts.cameraRight[0] = opts.cameraFwd[2] = 1;
    opts.cameraNear = .1f; opts.cameraFar = 1000.f; opts.cameraFOV = 1.04719755f;
    opts.cameraAspectRatio = float(g_width) / g_height;
    opts.mvecScale[0] = 1.f / g_width; opts.mvecScale[1] = 1.f / g_height;
    opts.cameraMotionIncluded = true; opts.orthoProjection = true; opts.colorBuffersHDR = hdr;
    opts.motionVectorsInvalidValue = -65504.f; opts.multiFrameCount = multiplier - 1;
    opts.mvecsSubrectSize = opts.depthSubrectSize = opts.hudLessSubrectSize = {g_width, g_height};
    opts.backbufferSubrectSize = opts.outputInterpSubrectSize = {g_width, g_height};
    unsigned good = 0;
    unsigned temporalGood = 0;
    for (unsigned frame = 0; frame < 8; ++frame) {
        Texture& color = colors[frame % 2];
        eval.pBackbuffer = color.resource; eval.pHudless = color.resource;
        params->Set(NVSDK_NGX_DLSSG_Parameter_BackbufferFrameID, static_cast<unsigned long long>(frame));
        Fill(input, frame, hdr); CopyRowsToStaging(color.upload, input.data(), FrameRowPitch());
        for (unsigned sub = 1; sub < multiplier; ++sub) {
            Texture& output = outputs[sub - 1];
            eval.pOutputInterpFrame = output.resource;
            opts.multiFrameIndex = sub; opts.reset = frame == 0;
            Check(BeginCommands(slot), "begin evaluate");
            if (sub == 1) {
                RecordUpload(slot.list, color.upload, color.resource);
                RecordUpload(slot.list, motion.upload, motion.resource);
                RecordUpload(slot.list, depth.upload, depth.resource);
            }
            for (auto* t : {&color, &motion, &depth}) t->State(slot.list, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
            output.State(slot.list, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
            validity.Before(slot.list, sub == 1);
            Ngx(NGX_D3D12_EVALUATE_DLSSG(slot.list, feature, params, &eval, &opts), "evaluate");
            validity.After(slot.list);
            output.State(slot.list, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COMMON);
            for (auto* t : {&color, &motion, &depth}) t->State(slot.list, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
            RecordReadback(slot.list, output.resource, output.readback);
            Check(SubmitCommands(slot, false) && WaitFence(slot.fence_value, 15000), "evaluate fence");
            CopyRowsFromStaging(output.readback, actual.data(), FrameRowPitch());
            const unsigned disabled = validity.Read();
            printf("validity frame=%u sub=%u disable=%u\n", frame, sub, disabled); fflush(stdout);
            if (frame == 7) {
                Fill(previous, frame - 1, hdr); Fill(expected, frame - 1.0 + double(sub) / multiplier, hdr);
                double err = 0, prevErr = 0, nextErr = 0; size_t count = 0, nonfinite = 0;
                float peak = 0;
                for (UINT y = 16; y + 16 < g_height; ++y) for (UINT x = 64; x + 64 < g_width; ++x) for (int c = 0; c < 3; ++c) {
                    const size_t i = (size_t(y) * g_width + x) * 4 + c;
                    const float value = Read(actual, i, hdr);
                    if (!std::isfinite(value)) { ++nonfinite; continue; }
                    peak = std::max(peak, value);
                    err += std::abs(value - Read(expected, i, hdr));
                    prevErr += std::abs(value - Read(previous, i, hdr));
                    nextErr += std::abs(value - Read(input, i, hdr)); ++count;
                }
                // A marginal relative improvement must not hide clipping or
                // a gross color-contract error in the floating-point test.
                const bool pass = count > 0 && !nonfinite && disabled == 0 && err / count < .02
                    && err < prevErr && err < nextErr;
                // Fit temporal position independently of the requested index;
                // distinct buffers alone cannot prove evenly paced motion.
                double fitError = 1e100, fittedTime = -1;
                for (int step = 0; step <= 100; ++step) {
                    const double t = step / 100.0;
                    double e = 0;
                    for (UINT y = 32; y + 32 < g_height; y += 16)
                        for (UINT x = 64; x + 64 < g_width; x += 16)
                            for (int c = 0; c < 3; ++c) {
                                const size_t i = (size_t(y) * g_width + x) * 4 + c;
                                e += std::abs(Read(actual, i, hdr) - Pixel(float(x) - float(frame - 1 + t) * 8, float(y), c, hdr));
                            }
                    if (e < fitError) {fitError = e; fittedTime = t;}
                }
                printf("TEMPORAL sub=%u requested=%.6f fitted=%.6f drift=%.6f\n", sub,
                    double(sub)/multiplier, fittedTime, std::abs(fittedTime-double(sub)/multiplier));
                temporalGood += std::abs(fittedTime-double(sub)/multiplier) <= .05;
                printf("PIXELS sub=%u mae=%.9f previous_mae=%.9f next_mae=%.9f nonfinite=%zu peak=%.6f pixel_test_pass=%d\n",
                    sub, err / (count ? count : 1), prevErr / (count ? count : 1), nextErr / (count ? count : 1), nonfinite, peak, pass);
                good += pass;
            }
        }
    }
    printf("synthetic_pixel_probe_pass=%d\n", good == multiplier - 1); fflush(stdout);
    printf("synthetic_temporal_probe_pass=%d\n", temporalGood == multiplier - 1); fflush(stdout);
    Ngx(NVSDK_NGX_D3D12_ReleaseFeature(feature), "release");
    Ngx(NVSDK_NGX_D3D12_DestroyParameters(params), "destroy parameters");
    if (processExit) {
        printf("shutdown_policy=isolated_process_exit (not NGX shutdown validation)\n");
        fflush(nullptr);
        ExitProcess(good == multiplier - 1 && temporalGood == multiplier - 1 ? 0 : 3);
    }
    printf("shutdown_begin\n"); fflush(stdout);
    Ngx(NVSDK_NGX_D3D12_Shutdown1(g_device), "shutdown");
    return good == multiplier - 1 && temporalGood == multiplier - 1 ? 0 : 3;
}
}
int wmain(int argc, wchar_t** argv) {
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    try { return probe::Run(argc, argv); }
    catch (const std::exception& ex) { fprintf(stderr, "PROBE_FAILED: %s\n", ex.what()); return 2; }
}
