// Candidate only: integer I420/P010 input over host or CUDA transport.
// CFR packet timing is expressed in frame ticks. No production backend switch.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>
#include <cstring>
#include <cstdio>
#include "nvEncodeAPI.h"

namespace {
NV_ENCODE_API_FUNCTION_LIST api{};
void* encoder = nullptr;
HMODULE nvenc_mod = nullptr;
const int kMax = 32;
struct Slot {
    NV_ENC_INPUT_PTR input = nullptr;
    NV_ENC_OUTPUT_PTR bitstream = nullptr;
    uint64_t device_pointer = 0;
    NV_ENC_REGISTERED_PTR registered = nullptr;
};
Slot slots[kMax];
int nslots = 0, free_q[kMax], pending_q[kMax], ready_q[kMax];
int nfree = 0, npending = 0, nready = 0;
unsigned width = 0, height = 0, host_bytes = 0;
unsigned codec_kind = 0;
NV_ENC_BUFFER_FORMAT input_format = NV_ENC_BUFFER_FORMAT_YV12;
int last_error = 0;
char info_json[512]{};
bool draining = false;
bool cuda_input = false, failed = false;
unsigned next_index = 0, device_pitch = 0;
unsigned reorder_delay = 0;
uint64_t output_index = 0;
struct PacketInfo {
    uint32_t size, flags; // flags bit 0: IDR/random-access packet
    int64_t pts, dts;
    uint64_t duration;
    uint32_t picture_type, reserved;
};
static_assert(sizeof(PacketInfo)==40,"Packet metadata ABI");
int fail_allocation_slot = -1;
HMODULE cuda_mod = nullptr;
using CuAlloc = int(__stdcall*)(uint64_t*, size_t);
using CuFree = int(__stdcall*)(uint64_t);
using CuCopy = int(__stdcall*)(uint64_t, uint64_t, size_t);
struct DriverCopy2D {
    size_t sx=0,sy=0; unsigned stype=2; const void* shost=nullptr;
    uint64_t sdevice=0;void* sarray=nullptr;size_t spitch=0;
    size_t dx=0,dy=0; unsigned dtype=2; void* dhost=nullptr;
    uint64_t ddevice=0;void* darray=nullptr;size_t dpitch=0;
    size_t width=0,height=0;
};
static_assert(sizeof(DriverCopy2D)==128,"CUDA_MEMCPY2D x64 ABI");
using CuCopy2D = int(__stdcall*)(const DriverCopy2D*);
CuAlloc cu_alloc = nullptr;
CuFree cu_free = nullptr;
CuCopy cu_copy = nullptr;
CuCopy2D cu_copy2d = nullptr;

bool ok(NVENCSTATUS s) { last_error = int(s); return s == NV_ENC_SUCCESS; }
void push(int* q, int& n, int v) { q[n++] = v; }
int pop(int* q, int& n) { int v = q[0]; for (int i = 1; i < n; ++i) q[i - 1] = q[i]; --n; return v; }

void copy_i420_to_yv12(const unsigned char* src, unsigned char* dst, unsigned pitch) {
    unsigned chroma_pitch = pitch / 2;
    const unsigned char* y = src;
    const unsigned char* u = src + width * height;
    const unsigned char* v = u + (width * height / 4);
    for (unsigned row = 0; row < height; ++row)
        std::memcpy(dst + row * pitch, y + row * width, width);
    unsigned char* dst_v = dst + pitch * height;
    unsigned char* dst_u = dst_v + chroma_pitch * (height / 2);
    for (unsigned row = 0; row < height / 2; ++row) {
        std::memcpy(dst_v + row * chroma_pitch, v + row * (width / 2), width / 2);
        std::memcpy(dst_u + row * chroma_pitch, u + row * (width / 2), width / 2);
    }
}

void destroy_slots() {
    for (int i = 0; i < nslots; ++i) {
        if (encoder && slots[i].bitstream) api.nvEncDestroyBitstreamBuffer(encoder, slots[i].bitstream);
        if (cuda_input) {
            if (encoder && slots[i].input) api.nvEncUnmapInputResource(encoder, slots[i].input);
            if (encoder && slots[i].registered) api.nvEncUnregisterResource(encoder, slots[i].registered);
            if (slots[i].device_pointer && cu_free) cu_free(slots[i].device_pointer);
        } else if (encoder && slots[i].input) api.nvEncDestroyInputBuffer(encoder, slots[i].input);
        slots[i] = {};
    }
    nslots = nfree = npending = nready = 0;
}
}

extern "C" __declspec(dllexport) int ring_error() { return last_error; }
extern "C" __declspec(dllexport) unsigned ring_abi_version() { return 2; }
extern "C" __declspec(dllexport) const char* ring_info() { return info_json; }
extern "C" __declspec(dllexport) int ring_enable_cuda_input(int enabled) {
    if (encoder) return 0;
    cuda_input = enabled != 0;
    return 1;
}
extern "C" __declspec(dllexport) int ring_test_allocation_failure(int slot) {
    if (encoder || slot < -1 || slot >= kMax) return 0;
    fail_allocation_slot = slot;
    return 1;
}
extern "C" __declspec(dllexport) int ring_owned_buffers() {
    int owned=0;
    for (const auto& slot : slots) owned += (slot.input!=nullptr)+(slot.bitstream!=nullptr)
        +(slot.device_pointer!=0)+(slot.registered!=nullptr);
    return owned;
}

extern "C" __declspec(dllexport) void ring_close() {
    // Complete normal cancellation before releasing any registered input slots.
    if (encoder && (npending || nready)) {
        if (!draining) {
            NV_ENC_PIC_PARAMS eos{}; eos.version=NV_ENC_PIC_PARAMS_VER;
            eos.encodePicFlags=NV_ENC_PIC_FLAG_EOS;
            api.nvEncEncodePicture(encoder,&eos);
        }
        while (npending) push(ready_q,nready,pop(pending_q,npending));
        while (nready) {
            int id=pop(ready_q,nready);
            NV_ENC_LOCK_BITSTREAM lock{};lock.version=NV_ENC_LOCK_BITSTREAM_VER;
            lock.outputBitstream=slots[id].bitstream;
            if (api.nvEncLockBitstream(encoder,&lock)==NV_ENC_SUCCESS)
                api.nvEncUnlockBitstream(encoder,slots[id].bitstream);
        }
    }
    destroy_slots();
    if (encoder) { api.nvEncDestroyEncoder(encoder); encoder = nullptr; }
    if (nvenc_mod) { FreeLibrary(nvenc_mod); nvenc_mod = nullptr; }
    if (cuda_mod) FreeLibrary(cuda_mod);
    cuda_mod=nullptr;cu_alloc=nullptr;cu_free=nullptr;cu_copy=nullptr;cu_copy2d=nullptr;
    width = height = host_bytes = 0;
    draining = false;
    failed=false;next_index=0;cuda_input=false;device_pitch=0;
    reorder_delay=0;output_index=0;
    info_json[0] = 0;
}

// codec: 0 H264 SDR / 1 HEVC PQ / 2 HEVC HLG. Integer YUV already converted
// according to the production contract; never silently convert RGB or HDR here.
extern "C" __declspec(dllexport) int ring_open_config(void* context, unsigned w, unsigned h,
    unsigned fps_num, unsigned fps_den, unsigned cq, unsigned preset_index, unsigned codec,
    unsigned* out_pitch, unsigned* out_slots) {
    if (encoder || !context || !w || !h || (w & 1) || (h & 1) || uint64_t(w) * h > 3840ull * 2160
        || !fps_num || !fps_den || cq > 51 || preset_index < 1 || preset_index > 7 || codec > 2) {
        last_error = -4; return 0;
    }
    codec_kind = codec;
    input_format = codec ? NV_ENC_BUFFER_FORMAT_YUV420_10BIT : NV_ENC_BUFFER_FORMAT_YV12;
    device_pitch = ((codec ? w * 2 : w) + 255) & ~255u;
    if (cuda_input) {
        cuda_mod=LoadLibraryW(L"nvcuda.dll");
        if (cuda_mod) {
            cu_alloc=reinterpret_cast<CuAlloc>(GetProcAddress(cuda_mod,"cuMemAlloc_v2"));
            cu_free=reinterpret_cast<CuFree>(GetProcAddress(cuda_mod,"cuMemFree_v2"));
            cu_copy=reinterpret_cast<CuCopy>(GetProcAddress(cuda_mod,"cuMemcpyDtoD_v2"));
            cu_copy2d=reinterpret_cast<CuCopy2D>(GetProcAddress(cuda_mod,"cuMemcpy2D_v2"));
        }
        if (!cu_alloc || !cu_free || !cu_copy2d) {last_error=-10;ring_close();return 0;}
    }
    GUID presets[] = {NV_ENC_PRESET_P1_GUID,NV_ENC_PRESET_P2_GUID,NV_ENC_PRESET_P3_GUID,
        NV_ENC_PRESET_P4_GUID,NV_ENC_PRESET_P5_GUID,NV_ENC_PRESET_P6_GUID,NV_ENC_PRESET_P7_GUID};
    GUID codec_guid = codec ? NV_ENC_CODEC_HEVC_GUID : NV_ENC_CODEC_H264_GUID;
    GUID preset_guid = presets[preset_index - 1];
    nvenc_mod = LoadLibraryW(L"nvEncodeAPI64.dll");
    if (!nvenc_mod) { last_error = -1; ring_close(); return 0; }
    auto create = reinterpret_cast<NVENCSTATUS (NVENCAPI*)(NV_ENCODE_API_FUNCTION_LIST*)>(
        GetProcAddress(nvenc_mod, "NvEncodeAPICreateInstance"));
    if (!create) { last_error = -2; ring_close(); return 0; }
    api = {}; api.version = NV_ENCODE_API_FUNCTION_LIST_VER;
    if (!ok(create(&api))) { ring_close(); return 0; }
    NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS open{};
    open.version = NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER;
    open.deviceType = NV_ENC_DEVICE_TYPE_CUDA; open.device = context; open.apiVersion = NVENCAPI_VERSION;
    if (!ok(api.nvEncOpenEncodeSessionEx(&open, &encoder))) { ring_close(); return 0; }
    NV_ENC_PRESET_CONFIG preset{};
    preset.version = NV_ENC_PRESET_CONFIG_VER; preset.presetCfg.version = NV_ENC_CONFIG_VER;
    if (!ok(api.nvEncGetEncodePresetConfigEx(encoder, codec_guid, preset_guid,
        NV_ENC_TUNING_INFO_HIGH_QUALITY, &preset))) { ring_close(); return 0; }
    auto config = preset.presetCfg;
    // FFmpeg h264_nvenc default profile is main, not the preset's profile.
    config.profileGUID = codec ? NV_ENC_HEVC_PROFILE_MAIN10_GUID : NV_ENC_H264_PROFILE_MAIN_GUID;
    config.frameFieldMode = NV_ENC_PARAMS_FRAME_FIELD_MODE_FRAME;
    int interval = config.frameIntervalP < 1 ? 1 : config.frameIntervalP;
    reorder_delay = unsigned(interval - 1);
    int look = config.rcParams.enableLookahead ? int(config.rcParams.lookaheadDepth) : 0;
    int needed = 4;
    if (interval * 4 > needed) needed = interval * 4;
    if (look > 0) {
        int extra = look + interval + 1 + 4;
        if (extra > needed) needed = extra;
    }
    if (needed > kMax) { last_error = -5; ring_close(); return 0; }
    config.rcParams.rateControlMode = NV_ENC_PARAMS_RC_VBR;
    config.rcParams.multiPass = NV_ENC_MULTI_PASS_DISABLED;
    config.rcParams.enableInitialRCQP = 1;
    // FFmpeg AVCodecContext defaults: I factor -.8, B factor 1.25,
    // B offset 1.25; nvenc set_vbr rounds these from initial P=26.
    config.rcParams.initialRCQP.qpInterP = 26;
    config.rcParams.initialRCQP.qpIntra = 21;
    config.rcParams.initialRCQP.qpInterB = 34;
    config.rcParams.targetQuality = static_cast<uint8_t>(cq);
    config.rcParams.targetQualityLSB = 0;
    config.rcParams.averageBitRate = 0;
    config.rcParams.maxBitRate = 0;
    config.rcParams.vbvBufferSize = 0;
    if (!codec) {
    auto& h264 = config.encodeCodecConfig.h264Config;
    h264.sliceMode = 3; h264.sliceModeData = 1;
    h264.idrPeriod = config.gopLength;
    h264.chromaFormatIDC = 1;
    h264.outputPictureTimingSEI = 1;
    // Match FFmpeg's global-header container policy, with separate SPS/PPS.
    h264.disableSPSPPS = 1; h264.repeatSPSPPS = 0; h264.outputAUD = 0;
    auto& vui = h264.h264VUIParameters;
    vui.videoSignalTypePresentFlag = 0;
    vui.videoFormat = NV_ENC_VUI_VIDEO_FORMAT_UNSPECIFIED;
    vui.videoFullRangeFlag = 0;
    vui.colourDescriptionPresentFlag = 0;
    vui.colourPrimaries = NV_ENC_VUI_COLOR_PRIMARIES_UNSPECIFIED;
    vui.transferCharacteristics = NV_ENC_VUI_TRANSFER_CHARACTERISTIC_UNSPECIFIED;
    vui.colourMatrix = NV_ENC_VUI_MATRIX_COEFFS_UNSPECIFIED;
    } else {
        auto& hevc = config.encodeCodecConfig.hevcConfig;
        hevc.sliceMode = 3; hevc.sliceModeData = 1;
        hevc.idrPeriod = config.gopLength;
        hevc.chromaFormatIDC = 1;
        hevc.pixelBitDepthMinus8 = 2;
        hevc.outputPictureTimingSEI = 1;
        hevc.disableSPSPPS = 1; hevc.repeatSPSPPS = 0;
        hevc.outputAUD = 0;
        auto& vui = hevc.hevcVUIParameters;
        vui.videoSignalTypePresentFlag = 1;
        vui.videoFormat = NV_ENC_VUI_VIDEO_FORMAT_UNSPECIFIED;
        vui.videoFullRangeFlag = 0;
        vui.colourDescriptionPresentFlag = 1;
        vui.colourPrimaries = static_cast<NV_ENC_VUI_COLOR_PRIMARIES>(9);
        vui.transferCharacteristics = static_cast<NV_ENC_VUI_TRANSFER_CHARACTERISTIC>(codec == 1 ? 16 : 18);
        vui.colourMatrix = static_cast<NV_ENC_VUI_MATRIX_COEFFS>(9);
        hevc.numRefL0 = static_cast<NV_ENC_NUM_REF_FRAMES>(0);
        hevc.numRefL1 = static_cast<NV_ENC_NUM_REF_FRAMES>(0);
    }
    NV_ENC_INITIALIZE_PARAMS init{};
    init.version = NV_ENC_INITIALIZE_PARAMS_VER;
    init.encodeGUID = codec_guid; init.presetGUID = preset_guid;
    init.encodeWidth = init.darWidth = w; init.encodeHeight = init.darHeight = h;
    init.frameRateNum = fps_num; init.frameRateDen = fps_den;
    init.enablePTD = 1; init.enableEncodeAsync = 0; init.encodeConfig = &config;
    init.tuningInfo = NV_ENC_TUNING_INFO_HIGH_QUALITY;
    if (!ok(api.nvEncInitializeEncoder(encoder, &init))) { ring_close(); return 0; }
    width = w; height = h; host_bytes = codec ? w * h * 3 : w * h * 3 / 2;
    for (int i = 0; i < needed; ++i) {
        nslots = i + 1;
        if (cuda_input) {
            size_t bytes=size_t(device_pitch)*h*3/2;
            if (cu_alloc(&slots[i].device_pointer,bytes)) {last_error=-11;ring_close();return 0;}
            NV_ENC_REGISTER_RESOURCE reg{};reg.version=NV_ENC_REGISTER_RESOURCE_VER;
            reg.resourceType=NV_ENC_INPUT_RESOURCE_TYPE_CUDADEVICEPTR;
            reg.resourceToRegister=reinterpret_cast<void*>(slots[i].device_pointer);
            reg.width=w;reg.height=h;reg.pitch=device_pitch;
            reg.bufferFormat=input_format;reg.bufferUsage=NV_ENC_INPUT_IMAGE;
            if (!ok(api.nvEncRegisterResource(encoder,&reg))) {ring_close();return 0;}
            slots[i].registered=reg.registeredResource;
        } else {
        NV_ENC_CREATE_INPUT_BUFFER in{};
        in.version = NV_ENC_CREATE_INPUT_BUFFER_VER;
        in.width = w; in.height = h; in.bufferFmt = input_format;
        if (!ok(api.nvEncCreateInputBuffer(encoder, &in))) { ring_close(); return 0; }
        slots[i].input = in.inputBuffer;
        }
        if (i==fail_allocation_slot) {fail_allocation_slot=-1;last_error=-20;ring_close();return 0;}
        NV_ENC_CREATE_BITSTREAM_BUFFER buffer{}; buffer.version = NV_ENC_CREATE_BITSTREAM_BUFFER_VER;
        if (!ok(api.nvEncCreateBitstreamBuffer(encoder, &buffer))) { ring_close(); return 0; }
        slots[i].bitstream = buffer.bitstreamBuffer;
        push(free_q, nfree, i);
    }
    nslots = needed;
    std::snprintf(info_json, sizeof(info_json),
        "{\"frameIntervalP\":%d,\"gopLength\":%u,\"enableLookahead\":%u,\"lookaheadDepth\":%u,"
        "\"enableAQ\":%u,\"zeroReorderDelay\":%u,\"slots\":%d,\"hostBytes\":%u,\"targetQuality\":%u,"
        "\"codec\":%u,\"preset\":%u,\"input\":\"%s\",\"initialQP\":[21,26,34],\"globalHeaders\":true}",
        interval, config.gopLength, config.rcParams.enableLookahead, config.rcParams.lookaheadDepth,
        config.rcParams.enableAQ, config.rcParams.zeroReorderDelay, nslots, host_bytes, cq,
        codec, preset_index, cuda_input ? (codec ? "cuda-p010" : "cuda-yv12") : (codec ? "sysmem-p010" : "sysmem-yv12"));
    if (out_pitch) *out_pitch = codec ? w * 2 : w;
    if (out_slots) *out_slots = unsigned(nslots);
    return 1;
}

extern "C" __declspec(dllexport) int ring_open(void* context, unsigned w, unsigned h,
    unsigned fps_num, unsigned fps_den, unsigned cq, unsigned fmt,
    unsigned* out_pitch, unsigned* out_slots) {
    // Old probe ABI's fmt was ignored, including legacy callers passing 2.
    (void)fmt;
    return ring_open_config(context,w,h,fps_num,fps_den,cq,5,0,out_pitch,out_slots);
}

static int drain_success(NVENCSTATUS status) {
    if (status == NV_ENC_ERR_NEED_MORE_INPUT) { last_error = int(status); return 1; }
    if (!ok(status)) {failed=true;return 0;}
    while (npending) push(ready_q, nready, pop(pending_q, npending));
    return 1;
}

static int submit_slot(int id,unsigned index,unsigned pitch) {
    NV_ENC_PIC_PARAMS pic{}; pic.version = NV_ENC_PIC_PARAMS_VER;
    pic.inputBuffer = slots[id].input;
    pic.bufferFmt = input_format;
    pic.inputWidth = width; pic.inputHeight = height; pic.inputPitch = pitch;
    pic.outputBitstream = slots[id].bitstream;
    pic.pictureStruct = NV_ENC_PIC_STRUCT_FRAME;
    if (codec_kind) {
        pic.codecPicParams.hevcPicParams.sliceMode = 3;
        pic.codecPicParams.hevcPicParams.sliceModeData = 1;
    } else {
        pic.codecPicParams.h264PicParams.sliceMode = 3;
        pic.codecPicParams.h264PicParams.sliceModeData = 1;
    }
    pic.frameIdx = index; pic.inputTimeStamp = index; pic.inputDuration = 1;
    NVENCSTATUS status = api.nvEncEncodePicture(encoder, &pic);
    push(pending_q, npending, id);
    ++next_index;
    return drain_success(status);
}

extern "C" __declspec(dllexport) int ring_feed(const void* host_i420, unsigned nbytes, unsigned index) {
    if (!encoder || cuda_input || failed || draining || !host_i420 || nbytes != host_bytes || nfree < 1 || index!=next_index) { last_error = -4; return 0; }
    int id = pop(free_q, nfree);
    NV_ENC_LOCK_INPUT_BUFFER lock{}; lock.version = NV_ENC_LOCK_INPUT_BUFFER_VER;
    lock.inputBuffer = slots[id].input;
    if (!ok(api.nvEncLockInputBuffer(encoder, &lock))) { push(free_q, nfree, id); failed=true;return 0; }
    if (codec_kind) {
        for (unsigned row = 0; row < height + height / 2; ++row)
            std::memcpy(static_cast<unsigned char*>(lock.bufferDataPtr) + size_t(row) * lock.pitch,
                static_cast<const unsigned char*>(host_i420) + size_t(row) * width * 2, size_t(width) * 2);
    } else copy_i420_to_yv12(static_cast<const unsigned char*>(host_i420),static_cast<unsigned char*>(lock.bufferDataPtr),lock.pitch);
    if (!ok(api.nvEncUnlockInputBuffer(encoder,slots[id].input))) {push(free_q,nfree,id);failed=true;return 0;}
    return submit_slot(id,index,lock.pitch);
}

extern "C" __declspec(dllexport) int ring_feed_device(uint64_t pointer,unsigned nbytes,unsigned index) {
    if (!encoder || !cuda_input || failed || draining || !pointer || nbytes!=host_bytes || nfree<1 || index!=next_index) {last_error=-4;return 0;}
    int id=pop(free_q,nfree);
    bool copied=true;
    auto copy_rows=[&](uint64_t dst,uint64_t src,unsigned rows,unsigned row_bytes,unsigned dst_pitch) {
        if (!copied) return;
        DriverCopy2D copy;
        copy.sdevice=src;copy.spitch=row_bytes;
        copy.ddevice=dst;copy.dpitch=dst_pitch;
        copy.width=row_bytes;copy.height=rows;
        copied=cu_copy2d(&copy)==0;
    };
    if (codec_kind) copy_rows(slots[id].device_pointer,pointer,height+height/2,width*2,device_pitch);
    else {
        copy_rows(slots[id].device_pointer,pointer,height,width,device_pitch);
        uint64_t ybytes=uint64_t(width)*height;
        uint64_t dst_chroma=slots[id].device_pointer+uint64_t(device_pitch)*height;
        copy_rows(dst_chroma,pointer+ybytes+ybytes/4,height/2,width/2,device_pitch/2);
        copy_rows(dst_chroma+uint64_t(device_pitch/2)*(height/2),pointer+ybytes,height/2,width/2,device_pitch/2);
    }
    if (!copied) {push(free_q,nfree,id);failed=true;last_error=-12;return 0;}
    NV_ENC_MAP_INPUT_RESOURCE mapped{};mapped.version=NV_ENC_MAP_INPUT_RESOURCE_VER;
    mapped.registeredResource=slots[id].registered;
    if (!ok(api.nvEncMapInputResource(encoder,&mapped))) {push(free_q,nfree,id);failed=true;return 0;}
    slots[id].input=mapped.mappedResource;
    return submit_slot(id,index,device_pitch);
}

static int pop_packet(void* bytes, unsigned capacity, unsigned* size, PacketInfo* metadata) {
    if (!encoder || failed || !bytes || !size) return 0;
    *size = 0;
    if (metadata) *metadata = {};
    // Match FFmpeg's default output delay; draining every successful submission
    // early changes the driver's available frame window, even without lookahead.
    if (!nready || (!draining && nready + npending < nslots - 1)) { *size = 0; return 1; }
    int id = pop(ready_q, nready);
    NV_ENC_LOCK_BITSTREAM lock{}; lock.version = NV_ENC_LOCK_BITSTREAM_VER;
    lock.outputBitstream = slots[id].bitstream; lock.doNotWait = 0;
    if (!ok(api.nvEncLockBitstream(encoder, &lock))) { push(ready_q, nready, id); failed=true;return 0; }
    *size = lock.bitstreamSizeInBytes;
    bool copied = *size <= capacity;
    if (copied && *size) std::memcpy(bytes, lock.bitstreamBufferPtr, *size);
    if (metadata && copied) {
        metadata->size=sizeof(PacketInfo);
        metadata->flags=lock.pictureType==NV_ENC_PIC_TYPE_IDR ? 1u : 0u;
        metadata->pts=int64_t(lock.outputTimeStamp);
        // Only CFR input starting at zero is accepted by feed. With one tick
        // per frame the decoder timeline starts before presentation by the
        // configured B-frame delay, including streams shorter than that delay.
        metadata->dts=int64_t(output_index)-reorder_delay;
        metadata->duration=lock.outputDuration;
        metadata->picture_type=uint32_t(lock.pictureType);
    }
    if (!ok(api.nvEncUnlockBitstream(encoder, slots[id].bitstream))) {failed=true;return 0;}
    if (cuda_input && slots[id].input) {
        if (!ok(api.nvEncUnmapInputResource(encoder,slots[id].input))) {failed=true;return 0;}
        slots[id].input=nullptr;
    }
    if (!copied) {failed=true;last_error=-3;}
    else ++output_index;
    push(free_q, nfree, id);
    return copied ? 1 : 0;
}

extern "C" __declspec(dllexport) int ring_pop(void* bytes, unsigned capacity, unsigned* size) {
    return pop_packet(bytes,capacity,size,nullptr);
}

extern "C" __declspec(dllexport) int ring_pop_packet(void* bytes, unsigned capacity,
    unsigned* size, PacketInfo* metadata, unsigned metadata_size) {
    if (!metadata || metadata_size!=sizeof(PacketInfo)) {last_error=-4;return 0;}
    return pop_packet(bytes,capacity,size,metadata);
}

extern "C" __declspec(dllexport) int ring_flush() {
    if (!encoder || failed) return 0;
    if (draining) return 1;
    draining = true;
    NV_ENC_PIC_PARAMS pic{}; pic.version = NV_ENC_PIC_PARAMS_VER;
    pic.encodePicFlags = NV_ENC_PIC_FLAG_EOS;
    return drain_success(api.nvEncEncodePicture(encoder, &pic));
}

extern "C" __declspec(dllexport) int ring_headers(void* bytes, unsigned capacity, unsigned* size) {
    if (!encoder || !bytes || !size || !capacity) return 0;
    NV_ENC_SEQUENCE_PARAM_PAYLOAD payload{};
    payload.version = NV_ENC_SEQUENCE_PARAM_PAYLOAD_VER;
    payload.inBufferSize = capacity;
    payload.spsppsBuffer = bytes;
    payload.outSPSPPSPayloadSize = size;
    return ok(api.nvEncGetSequenceParams(encoder, &payload));
}
