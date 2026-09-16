// Isolated SDR CUDA-pointer NVENC consumer. Synchronous P5 low-latency probe;
// never changes the application's encoder policy. Header supplied separately.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>
#include <cstring>
#include "nvEncodeAPI.h"

namespace {
NV_ENCODE_API_FUNCTION_LIST api{};
void* encoder = nullptr;
NV_ENC_REGISTERED_PTR resource = nullptr;
NV_ENC_OUTPUT_PTR bitstream = nullptr;
HMODULE module = nullptr;
unsigned width = 0, height = 0, stride = 0;
int last_error = 0;
bool ok(NVENCSTATUS result) { last_error = int(result); return result == NV_ENC_SUCCESS; }
}

extern "C" __declspec(dllexport) int probe_encoder_error() { return last_error; }

extern "C" __declspec(dllexport) void probe_encoder_close() {
    if (encoder) {
        if (bitstream) api.nvEncDestroyBitstreamBuffer(encoder, bitstream);
        if (resource) api.nvEncUnregisterResource(encoder, resource);
        api.nvEncDestroyEncoder(encoder);
    }
    encoder = nullptr; resource = nullptr; bitstream = nullptr;
    if (module) FreeLibrary(module);
    module = nullptr;
}

extern "C" __declspec(dllexport) int probe_encoder_open(void* context, void* pointer, unsigned w, unsigned h, unsigned pitch) {
    if (encoder || !context || !pointer || !w || !h || pitch < w*4) return 0;
    module = LoadLibraryW(L"nvEncodeAPI64.dll");
    if (!module) { last_error = -1; return 0; }
    auto create = reinterpret_cast<NVENCSTATUS (NVENCAPI *)(NV_ENCODE_API_FUNCTION_LIST*)>(GetProcAddress(module, "NvEncodeAPICreateInstance"));
    if (!create) { last_error = -2; return 0; }
    api = {}; api.version = NV_ENCODE_API_FUNCTION_LIST_VER;
    if (!ok(create(&api))) return 0;
    NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS open{};
    open.version = NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER;
    open.deviceType = NV_ENC_DEVICE_TYPE_CUDA; open.device = context; open.apiVersion = NVENCAPI_VERSION;
    if (!ok(api.nvEncOpenEncodeSessionEx(&open, &encoder))) return 0;
    NV_ENC_PRESET_CONFIG preset{};
    preset.version = NV_ENC_PRESET_CONFIG_VER; preset.presetCfg.version = NV_ENC_CONFIG_VER;
    if (!ok(api.nvEncGetEncodePresetConfigEx(encoder, NV_ENC_CODEC_H264_GUID, NV_ENC_PRESET_P5_GUID,
        NV_ENC_TUNING_INFO_LOW_LATENCY, &preset))) return 0;
    auto config = preset.presetCfg;
    config.frameIntervalP = 1; config.gopLength = 120;
    config.rcParams.rateControlMode = NV_ENC_PARAMS_RC_CONSTQP;
    config.rcParams.constQP = {19,19,19};
    config.rcParams.enableLookahead = 0; config.rcParams.lookaheadDepth = 0;
    config.rcParams.zeroReorderDelay = 1;
    NV_ENC_INITIALIZE_PARAMS init{};
    init.version = NV_ENC_INITIALIZE_PARAMS_VER;
    init.encodeGUID = NV_ENC_CODEC_H264_GUID; init.presetGUID = NV_ENC_PRESET_P5_GUID;
    init.encodeWidth = init.darWidth = w; init.encodeHeight = init.darHeight = h;
    init.frameRateNum = 30; init.frameRateDen = 1;
    init.enablePTD = 1; init.enableEncodeAsync = 0; init.encodeConfig = &config;
    init.tuningInfo = NV_ENC_TUNING_INFO_LOW_LATENCY;
    if (!ok(api.nvEncInitializeEncoder(encoder, &init))) return 0;
    NV_ENC_REGISTER_RESOURCE reg{};
    reg.version = NV_ENC_REGISTER_RESOURCE_VER;
    reg.resourceType = NV_ENC_INPUT_RESOURCE_TYPE_CUDADEVICEPTR;
    reg.resourceToRegister = pointer; reg.width = w; reg.height = h; reg.pitch = pitch;
    reg.bufferFormat = NV_ENC_BUFFER_FORMAT_ABGR; reg.bufferUsage = NV_ENC_INPUT_IMAGE;
    if (!ok(api.nvEncRegisterResource(encoder, &reg))) return 0;
    resource = reg.registeredResource;
    NV_ENC_CREATE_BITSTREAM_BUFFER buffer{}; buffer.version = NV_ENC_CREATE_BITSTREAM_BUFFER_VER;
    if (!ok(api.nvEncCreateBitstreamBuffer(encoder, &buffer))) return 0;
    bitstream = buffer.bitstreamBuffer; width = w; height = h; stride = pitch;
    return 1;
}

extern "C" __declspec(dllexport) int probe_encoder_frame(unsigned index, void* bytes, unsigned capacity, unsigned* size) {
    if (!encoder || !resource || !bytes || !size) return 0;
    NV_ENC_MAP_INPUT_RESOURCE mapped{}; mapped.version = NV_ENC_MAP_INPUT_RESOURCE_VER;
    mapped.registeredResource = resource;
    if (!ok(api.nvEncMapInputResource(encoder, &mapped))) return 0;
    NV_ENC_PIC_PARAMS pic{}; pic.version = NV_ENC_PIC_PARAMS_VER;
    pic.inputBuffer = mapped.mappedResource; pic.bufferFmt = mapped.mappedBufferFmt;
    pic.inputWidth = width; pic.inputHeight = height; pic.inputPitch = stride;
    pic.outputBitstream = bitstream; pic.pictureStruct = NV_ENC_PIC_STRUCT_FRAME;
    pic.frameIdx = index; pic.inputTimeStamp = index; pic.inputDuration = 1;
    if (index == 0) pic.encodePicFlags = NV_ENC_PIC_FLAG_FORCEIDR | NV_ENC_PIC_FLAG_OUTPUT_SPSPPS;
    bool result = ok(api.nvEncEncodePicture(encoder, &pic));
    if (result) {
        NV_ENC_LOCK_BITSTREAM lock{}; lock.version = NV_ENC_LOCK_BITSTREAM_VER;
        lock.outputBitstream = bitstream; lock.doNotWait = 0;
        result = ok(api.nvEncLockBitstream(encoder, &lock));
        if (result) {
            *size = lock.bitstreamSizeInBytes;
            if (*size <= capacity) memcpy(bytes, lock.bitstreamBufferPtr, *size);
            else { result = false; last_error = -3; }
            api.nvEncUnlockBitstream(encoder, bitstream);
        }
    }
    api.nvEncUnmapInputResource(encoder, mapped.mappedResource);
    return result;
}
