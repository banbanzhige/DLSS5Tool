// Single-owner CUDA VSR bridge. The caller owns its CUDA context/stream and
// pitched device buffers; this library owns only its NGX feature and arrays.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <cuda.h>
#include <nvsdk_ngx.h>
#include <nvsdk_ngx_helpers_vsr.h>
#include <cstdint>
#include <cstdio>
#include <cstdarg>
#include <new>
#include <mutex>

namespace {
struct Session {
    CUcontext context = nullptr;
    CUstream stream = nullptr;
    CUarray input = nullptr, output = nullptr;
    CUtexObject texture = 0;
    CUsurfObject surface = 0;
    NVSDK_NGX_Parameter* parameters = nullptr;
    NVSDK_NGX_Handle* feature = nullptr;
    DWORD thread = GetCurrentThreadId();
    int width = 0, height = 0, scale = 0, quality = 4;
    FILE* log = nullptr;
    bool initialized = false;
};
Session* active = nullptr;
std::mutex api_mutex;

void log(Session* s, const char* format, ...) {
    if (!s || !s->log) return;
    va_list args;
    va_start(args, format);
    vfprintf(s->log, format, args);
    va_end(args);
    fputc('\n', s->log);
    fflush(s->log);
}

bool driver(Session* s, CUresult result, const char* operation) {
    if (result == CUDA_SUCCESS) return true;
    const char* error = nullptr;
    cuGetErrorName(result, &error);
    log(s, "%s -> CUDA %d %s", operation, int(result), error ? error : "unknown");
    return false;
}

bool ngx(Session* s, NVSDK_NGX_Result result, const char* operation) {
    log(s, "%s -> NGX 0x%08X", operation, unsigned(result));
    return !NVSDK_NGX_FAILED(result);
}

bool owned(Session* s) {
    CUcontext current = nullptr;
    return s && s == active && s->thread == GetCurrentThreadId() &&
        driver(s, cuCtxGetCurrent(&current), "get current context") && current == s->context;
}

bool release(Session* s) {
    if (!s) return true;
    bool ok = driver(s, cuStreamSynchronize(s->stream), "final stream synchronize");
    if (s->feature) ok = ngx(s, NVSDK_NGX_CUDA_ReleaseFeature(s->feature), "release feature") && ok;
    if (s->parameters) ok = ngx(s, NVSDK_NGX_CUDA_DestroyParameters(s->parameters), "destroy parameters") && ok;
    log(s, "begin NGX shutdown");
    if (s->initialized) ok = ngx(s, NVSDK_NGX_CUDA_Shutdown(), "NGX shutdown") && ok;
    if (s->texture) ok = driver(s, cuTexObjectDestroy(s->texture), "destroy texture") && ok;
    if (s->surface) ok = driver(s, cuSurfObjectDestroy(s->surface), "destroy surface") && ok;
    if (s->input) ok = driver(s, cuArrayDestroy(s->input), "destroy input array") && ok;
    if (s->output) ok = driver(s, cuArrayDestroy(s->output), "destroy output array") && ok;
    if (s->log) fclose(s->log);
    delete s;
    active = nullptr;
    return ok;
}

bool arrays(Session* s) {
    CUDA_ARRAY3D_DESCRIPTOR descriptor{};
    descriptor.Width = s->width;
    descriptor.Height = s->height;
    descriptor.Format = CU_AD_FORMAT_UNSIGNED_INT8;
    descriptor.NumChannels = 4;
    descriptor.Flags = CUDA_ARRAY3D_SURFACE_LDST;
    if (!driver(s, cuArray3DCreate(&s->input, &descriptor), "input array")) return false;
    descriptor.Width *= s->scale;
    descriptor.Height *= s->scale;
    if (!driver(s, cuArray3DCreate(&s->output, &descriptor), "output array")) return false;
    CUDA_RESOURCE_DESC resource{};
    resource.resType = CU_RESOURCE_TYPE_ARRAY;
    resource.res.array.hArray = s->input;
    CUDA_TEXTURE_DESC texture{};
    texture.addressMode[0] = texture.addressMode[1] = texture.addressMode[2] = CU_TR_ADDRESS_MODE_CLAMP;
    texture.filterMode = CU_TR_FILTER_MODE_LINEAR;
    texture.flags = CU_TRSF_NORMALIZED_COORDINATES;
    if (!driver(s, cuTexObjectCreate(&s->texture, &resource, &texture, nullptr), "input texture")) return false;
    resource.res.array.hArray = s->output;
    return driver(s, cuSurfObjectCreate(&s->surface, &resource), "output surface");
}
}

extern "C" __declspec(dllexport) unsigned vsr_cuda_abi() { return 1; }

extern "C" __declspec(dllexport) int vsr_cuda_create(
    void* context, void* stream, int width, int height, int scale, int quality,
    const wchar_t* runtime_directory, const wchar_t* log_path, void** result) {
    std::lock_guard<std::mutex> lock(api_mutex);
    if (!result) return 0;
    *result = nullptr;
    if (active || !context || width <= 0 || height <= 0 || (scale != 2 && scale != 4) ||
        width > 16384 / scale || height > 16384 / scale || quality < 1 || quality > 4 ||
        !runtime_directory || !*runtime_directory) return 0;
    Session* s = new(std::nothrow) Session;
    if (!s) return 0;
    active = s;
    s->context = static_cast<CUcontext>(context);
    s->stream = static_cast<CUstream>(stream);
    s->width = width; s->height = height; s->scale = scale; s->quality = quality;
    if (log_path) _wfopen_s(&s->log, log_path, L"w");
    log(s, "CUDA VSR input=%dx%d scale=%d quality=%d context=%p stream=%p",
        width, height, scale, quality, context, stream);
    if (!owned(s)) { release(s); return 0; }
    const wchar_t* search[] = {runtime_directory};
    NVSDK_NGX_FeatureCommonInfo info{};
    info.PathListInfo = {search, 1};
    if (!ngx(s, NVSDK_NGX_CUDA_Init(0, runtime_directory, &info), "NGX init")) {
        release(s); return 0;
    }
    s->initialized = true;
    int available = 0;
    size_t scratch = 0;
    if (!ngx(s, NVSDK_NGX_CUDA_GetCapabilityParameters(&s->parameters), "capabilities") ||
        !s->parameters ||
        !ngx(s, s->parameters->Get(NVSDK_NGX_Parameter_VSR_Available, &available), "VSR available") ||
        !available ||
        !ngx(s, NVSDK_NGX_CUDA_GetScratchBufferSize(NVSDK_NGX_Feature_VSR, s->parameters, &scratch), "scratch size") || scratch) {
        release(s); return 0;
    }
    NVSDK_NGX_CUDA_VSRCreate_Params create{};
    create.InCUContext = context;
    create.InCUStream = stream;
    if (!ngx(s, NGX_CUDA_CREATE_VSR(&s->feature, s->parameters, &create), "create VSR") ||
        !s->feature || !arrays(s)) { release(s); return 0; }
    *result = s;
    return 1;
}

extern "C" __declspec(dllexport) int vsr_cuda_process(
    void* handle, uint64_t input, size_t input_pitch, uint64_t output, size_t output_pitch) {
    std::lock_guard<std::mutex> lock(api_mutex);
    Session* s = static_cast<Session*>(handle);
    if (!owned(s) || !input || !output || input_pitch < size_t(s->width) * 4 ||
        output_pitch < size_t(s->width) * s->scale * 4) return 0;
    CUDA_MEMCPY2D copy{};
    copy.srcMemoryType = CU_MEMORYTYPE_DEVICE;
    copy.srcDevice = input;
    copy.srcPitch = input_pitch;
    copy.dstMemoryType = CU_MEMORYTYPE_ARRAY;
    copy.dstArray = s->input;
    copy.WidthInBytes = size_t(s->width) * 4;
    copy.Height = s->height;
    if (!driver(s, cuMemcpy2DAsync(&copy, s->stream), "device input to array")) return 0;
    NVSDK_NGX_CUDA_VSR_Eval_Params eval{};
    eval.pInput = &s->texture;
    eval.pOutput = &s->surface;
    eval.InputSubrectSize = {unsigned(s->width), unsigned(s->height)};
    eval.OutputSubrectSize = {unsigned(s->width * s->scale), unsigned(s->height * s->scale)};
    eval.QualityLevel = static_cast<NVSDK_NGX_VSR_QualityLevel>(s->quality);
    if (!ngx(s, NGX_CUDA_EVALUATE_VSR(s->feature, s->parameters, &eval), "evaluate VSR")) return 0;
    copy = {};
    copy.srcMemoryType = CU_MEMORYTYPE_ARRAY;
    copy.srcArray = s->output;
    copy.dstMemoryType = CU_MEMORYTYPE_DEVICE;
    copy.dstDevice = output;
    copy.dstPitch = output_pitch;
    copy.WidthInBytes = size_t(s->width) * s->scale * 4;
    copy.Height = s->height * s->scale;
    return driver(s, cuMemcpy2DAsync(&copy, s->stream), "array to device output") &&
        driver(s, cuStreamSynchronize(s->stream), "VSR completion");
}

extern "C" __declspec(dllexport) int vsr_cuda_destroy(void* handle) {
    std::lock_guard<std::mutex> lock(api_mutex);
    Session* s = static_cast<Session*>(handle);
    if (!owned(s)) return 0;
    return release(s) ? 1 : 0;
}
