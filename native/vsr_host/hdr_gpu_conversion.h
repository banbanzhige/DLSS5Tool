// SPDX-License-Identifier: MIT
#pragma once
#include <d3d12.h>
#include <d3dcompiler.h>
#include <cstdint>
#include <cstring>
#include <string>

// Same wire semantics as the existing CPU helpers. Raw buffer stores avoid a
// typed UNORM UAV's additional quantization/rounding step. The caller owns the
// device, textures, queue/fence, thread and completion before Close()/reuse.
namespace vsr_hdr {
inline constexpr const char *shader = R"hlsl(
ByteAddressBuffer source : register(t0);
RWByteAddressBuffer destination : register(u0);
cbuffer Dimensions : register(b0) { uint width; uint height; uint sourcePitch; uint destinationPitch; };
float decodeHalf(uint halfValue) {
    uint sign = (halfValue & 0x8000u) << 16;
    int exponent = int((halfValue >> 10) & 31u);
    uint mantissa = halfValue & 1023u;
    uint bits;
    if (exponent == 0) {
        if (mantissa == 0) bits = sign;
        else {
            exponent = 1;
            [loop] while ((mantissa & 1024u) == 0) { mantissa <<= 1; --exponent; }
            bits = sign | (uint(exponent+112) << 23) | ((mantissa & 1023u) << 13);
        }
    } else if (exponent == 31) bits = sign | 0x7f800000u | (mantissa << 13);
    else bits = sign | (uint(exponent+112) << 23) | (mantissa << 13);
    return asfloat(bits);
}
uint encodeHalf(float value) {
    uint bits = asuint(value), sign = (bits >> 16) & 0x8000u;
    uint mantissa = bits & 0x7fffffu;
    int exponent = int((bits >> 23) & 255u)-127+15;
    if (exponent <= 0) {
        if (exponent < -10) return sign;
        mantissa = (mantissa | 0x800000u) >> (1-exponent);
        return sign | ((mantissa+0x1000u) >> 13);
    }
    if (exponent >= 31) return sign | 0x7c00u;
    return sign | (uint(exponent) << 10) | ((mantissa+0x1000u) >> 13);
}
uint quantize(uint bits) {
    precise float value = clamp(decodeHalf(bits), 0.0f, 1.0f);
    precise float scaled = value * 1023.0f;
    precise float rounded = scaled + 0.5f;
    return uint(rounded);
}
[numthreads(16,16,1)] void packHDR(uint3 id : SV_DispatchThreadID) {
    if (id.x >= width || id.y >= height) return;
    uint2 bits = source.Load2(id.y*sourcePitch+id.x*8);
    uint packed = quantize(bits.x & 65535u) | (quantize(bits.x >> 16) << 10)
                | (quantize(bits.y & 65535u) << 20) | (3u << 30);
    destination.Store(id.y*destinationPitch+id.x*4, packed);
}
[numthreads(16,16,1)] void unpackHDR(uint3 id : SV_DispatchThreadID) {
    if (id.x >= width || id.y >= height) return;
    uint pixel = source.Load(id.y*sourcePitch+id.x*4);
    precise float r = float(pixel & 1023u) / 1023.0f;
    precise float g = float((pixel >> 10) & 1023u) / 1023.0f;
    precise float b = float((pixel >> 20) & 1023u) / 1023.0f;
    destination.Store2(id.y*destinationPitch+id.x*8,
        uint2(encodeHalf(r) | (encodeHalf(g) << 16), encodeHalf(b) | (0x3c00u << 16)));
}
)hlsl";

class Conversion {
    std::string error_;
    ID3D12Resource *upload_ = nullptr, *input_ = nullptr, *packedInput_ = nullptr;
    ID3D12Resource *packedOutput_ = nullptr, *output_ = nullptr, *readback_ = nullptr;
    ID3D12RootSignature *root_ = nullptr;
    ID3D12PipelineState *pack_ = nullptr, *unpack_ = nullptr;
    unsigned char *uploadMap_ = nullptr, *readbackMap_ = nullptr;
    UINT iw_ = 0, ih_ = 0, ow_ = 0, oh_ = 0, inputPitch_ = 0, outputPitch_ = 0;
    UINT64 inputBytes_ = 0, outputBytes_ = 0, packedInputBytes_ = 0, packedOutputBytes_ = 0;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT inputFootprint_ = {}, outputFootprint_ = {};

    template<class T> static void Release(T *&value) {
        if (value) { value->Release(); value = nullptr; }
    }
    static ID3D12Resource *Buffer(ID3D12Device *device, UINT64 bytes,
        D3D12_HEAP_TYPE type, D3D12_RESOURCE_STATES state, D3D12_RESOURCE_FLAGS flags = D3D12_RESOURCE_FLAG_NONE) {
        D3D12_HEAP_PROPERTIES heap = {}; heap.Type = type; heap.CreationNodeMask = heap.VisibleNodeMask = 1;
        D3D12_RESOURCE_DESC desc = {}; desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        desc.Width = bytes; desc.Height = 1; desc.DepthOrArraySize = desc.MipLevels = 1;
        desc.SampleDesc.Count = 1; desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR; desc.Flags = flags;
        ID3D12Resource *result = nullptr;
        if (FAILED(device->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_NONE,&desc,state,nullptr,IID_PPV_ARGS(&result)))) return nullptr;
        return result;
    }
    static void Transition(ID3D12GraphicsCommandList *list, ID3D12Resource *resource,
        D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
        D3D12_RESOURCE_BARRIER barrier = {}; barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = resource; barrier.Transition.StateBefore = before; barrier.Transition.StateAfter = after;
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        list->ResourceBarrier(1,&barrier);
    }
    bool Pipeline(ID3D12Device *device, const char *entry, ID3D12PipelineState **target) {
        ID3DBlob *blob = nullptr, *error = nullptr;
        HRESULT hr = D3DCompile(shader,strlen(shader),"vsr_hdr",nullptr,nullptr,entry,"cs_5_0",
            D3DCOMPILE_OPTIMIZATION_LEVEL3 | D3DCOMPILE_IEEE_STRICTNESS,0,&blob,&error);
        if (FAILED(hr) && error) error_.assign(static_cast<const char *>(error->GetBufferPointer()),error->GetBufferSize());
        Release(error);
        if (FAILED(hr)) { Release(blob); return false; }
        D3D12_COMPUTE_PIPELINE_STATE_DESC desc = {}; desc.pRootSignature = root_;
        desc.CS = {blob->GetBufferPointer(),blob->GetBufferSize()};
        hr = device->CreateComputePipelineState(&desc,IID_PPV_ARGS(target));
        Release(blob); return SUCCEEDED(hr);
    }
    void Dispatch(ID3D12GraphicsCommandList *list, bool pack) {
        list->SetComputeRootSignature(root_); list->SetPipelineState(pack ? pack_ : unpack_);
        list->SetComputeRootShaderResourceView(0,(pack ? input_ : packedOutput_)->GetGPUVirtualAddress());
        list->SetComputeRootUnorderedAccessView(1,(pack ? packedInput_ : output_)->GetGPUVirtualAddress());
        UINT constants[] = {pack ? iw_ : ow_, pack ? ih_ : oh_,
            pack ? inputPitch_ : outputFootprint_.Footprint.RowPitch,
            pack ? inputFootprint_.Footprint.RowPitch : outputPitch_};
        list->SetComputeRoot32BitConstants(2,4,constants,0);
        list->Dispatch((constants[0]+15)/16,(constants[1]+15)/16,1);
    }
public:
    Conversion() = default;
    Conversion(const Conversion &) = delete;
    Conversion &operator=(const Conversion &) = delete;
    ~Conversion() { Close(); }
    bool Initialize(ID3D12Device *device, ID3D12Resource *inputTexture, ID3D12Resource *outputTexture) {
        Close();
        error_.clear();
        const auto in = inputTexture->GetDesc(), out = outputTexture->GetDesc();
        if (!in.Width || !in.Height || !out.Width || !out.Height || out.Width*out.Height > 3840ull*2160 ||
            in.Format != DXGI_FORMAT_R10G10B10A2_UNORM || out.Format != DXGI_FORMAT_R10G10B10A2_UNORM) return false;
        iw_ = UINT(in.Width); ih_ = in.Height; ow_ = UINT(out.Width); oh_ = out.Height;
        inputPitch_ = (iw_*8+255)&~255u; outputPitch_ = (ow_*8+255)&~255u;
        inputBytes_ = UINT64(inputPitch_)*ih_; outputBytes_ = UINT64(outputPitch_)*oh_;
        UINT rows; UINT64 rowBytes;
        device->GetCopyableFootprints(&in,0,1,0,&inputFootprint_,&rows,&rowBytes,&packedInputBytes_);
        device->GetCopyableFootprints(&out,0,1,0,&outputFootprint_,&rows,&rowBytes,&packedOutputBytes_);
        D3D12_ROOT_PARAMETER parameters[3] = {};
        parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_SRV;
        parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
        parameters[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
        parameters[2].Constants.Num32BitValues = 4;
        D3D12_ROOT_SIGNATURE_DESC root = {}; root.NumParameters = 3; root.pParameters = parameters;
        ID3DBlob *blob = nullptr, *error = nullptr;
        HRESULT hr = D3D12SerializeRootSignature(&root,D3D_ROOT_SIGNATURE_VERSION_1,&blob,&error);
        Release(error);
        if (FAILED(hr)) { Release(blob); Close(); return false; }
        hr = device->CreateRootSignature(0,blob->GetBufferPointer(),blob->GetBufferSize(),IID_PPV_ARGS(&root_));
        Release(blob);
        if (FAILED(hr) || !Pipeline(device,"packHDR",&pack_) || !Pipeline(device,"unpackHDR",&unpack_)) { Close(); return false; }
        upload_ = Buffer(device,inputBytes_,D3D12_HEAP_TYPE_UPLOAD,D3D12_RESOURCE_STATE_GENERIC_READ);
        input_ = Buffer(device,inputBytes_,D3D12_HEAP_TYPE_DEFAULT,D3D12_RESOURCE_STATE_COMMON);
        packedInput_ = Buffer(device,packedInputBytes_,D3D12_HEAP_TYPE_DEFAULT,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
        packedOutput_ = Buffer(device,packedOutputBytes_,D3D12_HEAP_TYPE_DEFAULT,D3D12_RESOURCE_STATE_COMMON);
        output_ = Buffer(device,outputBytes_,D3D12_HEAP_TYPE_DEFAULT,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
        readback_ = Buffer(device,outputBytes_,D3D12_HEAP_TYPE_READBACK,D3D12_RESOURCE_STATE_COPY_DEST);
        if (!upload_ || !input_ || !packedInput_ || !packedOutput_ || !output_ || !readback_) { Close(); return false; }
        D3D12_RANGE none = {0,0}, read = {0,SIZE_T(outputBytes_)};
        if (FAILED(upload_->Map(0,&none,reinterpret_cast<void **>(&uploadMap_))) ||
            FAILED(readback_->Map(0,&read,reinterpret_cast<void **>(&readbackMap_)))) { Close(); return false; }
        return true;
    }
    void CopyInput(const void *source) {
        for (UINT y=0;y<ih_;++y) memcpy(uploadMap_+UINT64(y)*inputPitch_,
            static_cast<const unsigned char *>(source)+UINT64(y)*iw_*8,UINT64(iw_)*8);
    }
    void RecordUpload(ID3D12GraphicsCommandList *list, ID3D12Resource *inputTexture) {
        Transition(list,input_,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_COPY_DEST);
        list->CopyBufferRegion(input_,0,upload_,0,inputBytes_);
        Transition(list,input_,D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        Transition(list,packedInput_,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        Dispatch(list,true);
        Transition(list,packedInput_,D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_COPY_SOURCE);
        Transition(list,inputTexture,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_COPY_DEST);
        D3D12_TEXTURE_COPY_LOCATION source = {}, target = {};
        source.pResource = packedInput_; source.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; source.PlacedFootprint = inputFootprint_;
        target.pResource = inputTexture; target.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        list->CopyTextureRegion(&target,0,0,0,&source,nullptr);
        Transition(list,inputTexture,D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_COMMON);
        Transition(list,packedInput_,D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_COMMON);
        Transition(list,input_,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_COMMON);
    }
    void RecordReadback(ID3D12GraphicsCommandList *list, ID3D12Resource *outputTexture) {
        Transition(list,outputTexture,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_COPY_SOURCE);
        Transition(list,packedOutput_,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_COPY_DEST);
        D3D12_TEXTURE_COPY_LOCATION source = {}, target = {};
        source.pResource = outputTexture; source.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        target.pResource = packedOutput_; target.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; target.PlacedFootprint = outputFootprint_;
        list->CopyTextureRegion(&target,0,0,0,&source,nullptr);
        Transition(list,packedOutput_,D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        Transition(list,output_,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        Dispatch(list,false);
        Transition(list,output_,D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(readback_,0,output_,0,outputBytes_);
        Transition(list,output_,D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_COMMON);
        Transition(list,packedOutput_,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_COMMON);
        Transition(list,outputTexture,D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_COMMON);
    }
    void CopyOutput(void *destination) const {
        for (UINT y=0;y<oh_;++y) memcpy(static_cast<unsigned char *>(destination)+UINT64(y)*ow_*8,
            readbackMap_+UINT64(y)*outputPitch_,UINT64(ow_)*8);
    }
    ID3D12Resource *PackedInput() const { return packedInput_; }
    const char *Error() const { return error_.c_str(); }
    UINT64 PackedInputBytes() const { return packedInputBytes_; }
    const D3D12_PLACED_SUBRESOURCE_FOOTPRINT &InputFootprint() const { return inputFootprint_; }
    void Close() {
        if (upload_ && uploadMap_) upload_->Unmap(0,nullptr);
        if (readback_ && readbackMap_) readback_->Unmap(0,nullptr);
        uploadMap_ = readbackMap_ = nullptr;
        Release(upload_); Release(input_); Release(packedInput_); Release(packedOutput_); Release(output_); Release(readback_);
        Release(pack_); Release(unpack_); Release(root_);
    }
};
} // namespace vsr_hdr
