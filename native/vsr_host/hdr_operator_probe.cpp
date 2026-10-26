// SPDX-License-Identifier: MIT
// Standalone bounded probe. CPU helper bodies are frozen from the checkpoint
// by build_vsr_hdr.py into its registered task; never part of the shipped DLL.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <algorithm>
#include <cstdint>
#include <cstring>
#include "hdr_gpu_conversion.h"
#include "cpu_hdr_reference.h"

namespace {
ID3D12Device *device = nullptr;
ID3D12CommandQueue *queue = nullptr;
ID3D12CommandAllocator *allocator = nullptr;
ID3D12GraphicsCommandList *list = nullptr;
ID3D12Fence *fence = nullptr;
HANDLE eventHandle = nullptr;
UINT64 fenceValue = 0;
ID3D12Resource *input = nullptr, *output = nullptr, *packedReadback = nullptr, *packedUpload = nullptr;
unsigned char *readMap = nullptr, *uploadMap = nullptr;
UINT width = 0, height = 0;
vsr_hdr::Conversion conversion;
const char *failure = "";
template<class T> void Release(T *&value) { if (value) { value->Release(); value = nullptr; } }
void Transition(ID3D12Resource *resource,D3D12_RESOURCE_STATES before,D3D12_RESOURCE_STATES after) {
    D3D12_RESOURCE_BARRIER barrier = {}; barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = resource; barrier.Transition.StateBefore = before; barrier.Transition.StateAfter = after;
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES; list->ResourceBarrier(1,&barrier);
}
ID3D12Resource *Resource(bool texture,D3D12_HEAP_TYPE type,UINT64 bytes) {
    D3D12_RESOURCE_DESC desc = {}; desc.Dimension = texture ? D3D12_RESOURCE_DIMENSION_TEXTURE2D : D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = texture ? width : bytes; desc.Height = texture ? height : 1; desc.DepthOrArraySize = desc.MipLevels = 1;
    desc.SampleDesc.Count = 1; desc.Layout = texture ? D3D12_TEXTURE_LAYOUT_UNKNOWN : D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    desc.Format = texture ? DXGI_FORMAT_R10G10B10A2_UNORM : DXGI_FORMAT_UNKNOWN;
    D3D12_HEAP_PROPERTIES heap = {}; heap.Type = type; heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    const auto state = type == D3D12_HEAP_TYPE_READBACK ? D3D12_RESOURCE_STATE_COPY_DEST :
        type == D3D12_HEAP_TYPE_UPLOAD ? D3D12_RESOURCE_STATE_GENERIC_READ : D3D12_RESOURCE_STATE_COMMON;
    ID3D12Resource *resource = nullptr;
    if (FAILED(device->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_NONE,&desc,state,nullptr,IID_PPV_ARGS(&resource)))) return nullptr;
    return resource;
}
bool Complete() {
    if (FAILED(list->Close())) return false;
    ID3D12CommandList *commands[] = {list}; queue->ExecuteCommandLists(1,commands);
    const UINT64 value = ++fenceValue;
    if (FAILED(queue->Signal(fence,value))) return false;
    if (fence->GetCompletedValue() < value) {
        if (FAILED(fence->SetEventOnCompletion(value,eventHandle))) return false;
        if (WaitForSingleObject(eventHandle,15000) != WAIT_OBJECT_0) return false;
    }
    return true;
}
}

extern "C" __declspec(dllexport) void hdr_probe_close() {
    conversion.Close();
    if (packedReadback && readMap) packedReadback->Unmap(0,nullptr);
    if (packedUpload && uploadMap) packedUpload->Unmap(0,nullptr);
    readMap = uploadMap = nullptr;
    Release(packedReadback); Release(packedUpload); Release(input); Release(output);
    Release(list); Release(allocator); Release(queue); Release(fence); Release(device);
    if (eventHandle) { CloseHandle(eventHandle); eventHandle = nullptr; }
    fenceValue = 0;
}
extern "C" __declspec(dllexport) const char *hdr_probe_error() { return failure; }
extern "C" __declspec(dllexport) int hdr_probe_init(UINT w,UINT h,int32_t high,uint32_t low) {
    hdr_probe_close(); width = w; height = h;
    if (!w || !h || UINT64(w)*h > 3840ull*2160) return 0;
    failure = "device creation";
    IDXGIFactory6 *factory = nullptr;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return 0;
    const LUID luid{low,high}; IDXGIAdapter1 *adapter = nullptr;
    HRESULT hr = factory->EnumAdapterByLuid(luid,IID_PPV_ARGS(&adapter));
    if (SUCCEEDED(hr)) hr = D3D12CreateDevice(adapter,D3D_FEATURE_LEVEL_12_0,IID_PPV_ARGS(&device));
    Release(adapter); Release(factory);
    if (FAILED(hr)) return 0;
    D3D12_COMMAND_QUEUE_DESC queueDesc = {};
    if (FAILED(device->CreateCommandQueue(&queueDesc,IID_PPV_ARGS(&queue))) ||
        FAILED(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,IID_PPV_ARGS(&allocator))) ||
        FAILED(device->CreateCommandList(0,D3D12_COMMAND_LIST_TYPE_DIRECT,allocator,nullptr,IID_PPV_ARGS(&list))) ||
        FAILED(device->CreateFence(0,D3D12_FENCE_FLAG_NONE,IID_PPV_ARGS(&fence)))) return 0;
    eventHandle = CreateEventW(nullptr,FALSE,FALSE,nullptr);
    if (!eventHandle || FAILED(list->Close())) return 0;
    input = Resource(true,D3D12_HEAP_TYPE_DEFAULT,0); output = Resource(true,D3D12_HEAP_TYPE_DEFAULT,0);
    if (!input || !output) return 0;
    if (!conversion.Initialize(device,input,output)) { failure = conversion.Error(); return 0; }
    failure = "probe staging allocation/map";
    packedReadback = Resource(false,D3D12_HEAP_TYPE_READBACK,conversion.PackedInputBytes());
    packedUpload = Resource(false,D3D12_HEAP_TYPE_UPLOAD,conversion.PackedInputBytes());
    if (!packedReadback || !packedUpload) return 0;
    D3D12_RANGE read = {0,SIZE_T(conversion.PackedInputBytes())}, none = {0,0};
    if (FAILED(packedReadback->Map(0,&read,reinterpret_cast<void **>(&readMap))) ||
        FAILED(packedUpload->Map(0,&none,reinterpret_cast<void **>(&uploadMap)))) return 0;
    failure = ""; return 1;
}
extern "C" __declspec(dllexport) int hdr_probe_run(int unpackOnly,const void *source,void *result,void *packedResult) {
    if (!device || !source || !result || (!unpackOnly && !packedResult)) return 0;
    if (unpackOnly) {
        for (UINT y=0;y<height;++y) memcpy(uploadMap+UINT64(y)*conversion.InputFootprint().Footprint.RowPitch,
            static_cast<const unsigned char *>(source)+UINT64(y)*width*4,UINT64(width)*4);
    } else conversion.CopyInput(source);
    if (FAILED(allocator->Reset()) || FAILED(list->Reset(allocator,nullptr))) return 0;
    D3D12_TEXTURE_COPY_LOCATION src = {}, dst = {};
    if (unpackOnly) {
        Transition(output,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_COPY_DEST);
        src.pResource = packedUpload; src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; src.PlacedFootprint = conversion.InputFootprint();
        dst.pResource = output; dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        list->CopyTextureRegion(&dst,0,0,0,&src,nullptr);
        Transition(output,D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_COMMON);
    } else {
        conversion.RecordUpload(list,input);
        Transition(input,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_COPY_SOURCE);
        Transition(output,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_COPY_DEST);
        src.pResource = input; src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        dst.pResource = output; dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        list->CopyTextureRegion(&dst,0,0,0,&src,nullptr);
        Transition(input,D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_COMMON);
        Transition(output,D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_COMMON);
        Transition(conversion.PackedInput(),D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(packedReadback,0,conversion.PackedInput(),0,conversion.PackedInputBytes());
        Transition(conversion.PackedInput(),D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_COMMON);
    }
    conversion.RecordReadback(list,output);
    if (!Complete()) return 0;
    conversion.CopyOutput(result);
    if (!unpackOnly) for (UINT y=0;y<height;++y) memcpy(static_cast<unsigned char *>(packedResult)+UINT64(y)*width*4,
        readMap+UINT64(y)*conversion.InputFootprint().Footprint.RowPitch,UINT64(width)*4);
    return 1;
}
extern "C" __declspec(dllexport) void hdr_reference_pack(const uint16_t *input,uint32_t *output,UINT64 count) {
    for (UINT64 i=0;i<count;++i) output[i] = Pack10(HalfToFloat(input[i*4]),HalfToFloat(input[i*4+1]),HalfToFloat(input[i*4+2]));
}
extern "C" __declspec(dllexport) void hdr_reference_unpack(const uint32_t *input,uint16_t *output,UINT64 count) {
    for (UINT64 i=0;i<count;++i) {
        output[i*4] = FloatToHalf(float(input[i]&1023u)/1023.0f);
        output[i*4+1] = FloatToHalf(float((input[i]>>10)&1023u)/1023.0f);
        output[i*4+2] = FloatToHalf(float((input[i]>>20)&1023u)/1023.0f);
        output[i*4+3] = FloatToHalf(1.0f);
    }
}
