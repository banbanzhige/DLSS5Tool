#pragma once
// Included inside host namespace after resource helpers; no CUDA dependency.
// One raw shared source per native slot, ordered D3D12 scratch conversion.
namespace gpu_flow {
ID3D12Resource *probe_sources[kMaxSlots] = {};
ID3D12Resource *probe_coordinates = nullptr;
Staging probe_motion;
ID3D12RootSignature *probe_root = nullptr;
ID3D12PipelineState *probe_pipeline = nullptr;
HANDLE probe_handles[kMaxSlots] = {};
UINT probe_fw = 0, probe_fh = 0;
UINT64 probe_size = 0;
int armed_slot = -1;
bool clear_frame = false;

const char *probe_shader = R"hlsl(
ByteAddressBuffer inputFlow : register(t0);
ByteAddressBuffer coordinates : register(t1);
RWByteAddressBuffer outputMotion : register(u0);
cbuffer Dimensions : register(b0) { uint fw; uint fh; uint w; uint h; uint pitch; uint clearFlow; float scaleX; float scaleY; };
uint halfBits(float value) {
    uint bits = asuint(value), sign = (bits >> 16) & 0x8000;
    uint mantissa = bits & 0x7fffff;
    int exponent = int((bits >> 23) & 255) - 127 + 15;
    if (exponent <= 0) {
        if (exponent < -10) return sign;
        mantissa = (mantissa | 0x800000) >> (1 - exponent);
        return sign | ((mantissa + 0x1000) >> 13);
    }
    if (exponent >= 31) return sign | 0x7c00;
    return sign | (uint(exponent) << 10) | ((mantissa + 0x1000) >> 13);
}
float readFlow(uint x, uint y, uint c) {
    return asfloat(inputFlow.Load(4 * (c * fw * fh + y * fw + x)));
}
[numthreads(16,16,1)] void main(uint3 id : SV_DispatchThreadID) {
    uint x = id.x, y = id.y;
    if (x >= w || y >= h) return;
    if (clearFlow != 0) { outputMotion.Store(y * pitch + 4*x, 0); return; }
    int ix = asint(coordinates.Load(x*8)), iy = asint(coordinates.Load((w+y)*8));
    precise float ax = asfloat(coordinates.Load(x*8+4)), ay = asfloat(coordinates.Load((w+y)*8+4));
    uint x0 = uint(clamp(ix, 0, int(fw)-1)), x1 = uint(clamp(ix+1, 0, int(fw)-1));
    uint y0 = uint(clamp(iy, 0, int(fh)-1)), y1 = uint(clamp(iy+1, 0, int(fh)-1));
    uint packed = 0;
    [unroll] for (uint c=0; c<2; ++c) {
        precise float a = readFlow(x0,y0,c) * (1.0f-ax) + readFlow(x1,y0,c) * ax;
        precise float b = readFlow(x0,y1,c) * (1.0f-ax) + readFlow(x1,y1,c) * ax;
        precise float value = (a * (1.0f-ay) + b * ay) * (c == 0 ? scaleX : scaleY);
        packed |= halfBits(value) << (16*c);
    }
    outputMotion.Store(y * pitch + 4*x, packed);
}
)hlsl";

bool Record(Slot &slot, int index, bool reset) {
    auto *probe_source = probe_sources[index];
    auto src = Transition(probe_source, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    auto dst = Transition(probe_motion.resource, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    slot.list->ResourceBarrier(1, &src);
    slot.list->ResourceBarrier(1, &dst);
    slot.list->SetPipelineState(probe_pipeline);
    slot.list->SetComputeRootSignature(probe_root);
    slot.list->SetComputeRootShaderResourceView(0, probe_source->GetGPUVirtualAddress());
    slot.list->SetComputeRootUnorderedAccessView(1, probe_motion.resource->GetGPUVirtualAddress());
    UINT constants[] = {probe_fw, probe_fh, g_width, g_height, probe_motion.footprint.Footprint.RowPitch, reset ? 1u : 0u, 0, 0};
    float scale_x = static_cast<float>(double(g_width)/probe_fw);
    float scale_y = static_cast<float>(double(g_height)/probe_fh);
    memcpy(constants+6,&scale_x,4); memcpy(constants+7,&scale_y,4);
    slot.list->SetComputeRoot32BitConstants(2, 8, constants, 0);
    slot.list->SetComputeRootShaderResourceView(3, probe_coordinates->GetGPUVirtualAddress());
    slot.list->Dispatch((g_width+15)/16, (g_height+15)/16, 1);
    dst = Transition(probe_motion.resource, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
    slot.list->ResourceBarrier(1, &dst);
    RecordUpload(slot.list, probe_motion, slot.motion);
    dst = Transition(probe_motion.resource, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
    src = Transition(probe_source, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COMMON);
    slot.list->ResourceBarrier(1, &dst);
    slot.list->ResourceBarrier(1, &src);
    return true;
}

void Close() {
    if (g_initialized) WaitFence(g_fence_value);
    Release(probe_pipeline);
    Release(probe_root);
    ReleaseStaging(probe_motion);
    for (auto &source : probe_sources) Release(source);
    Release(probe_coordinates);
    for (auto &handle : probe_handles) { if (handle) CloseHandle(handle); handle = nullptr; }
    armed_slot = -1;
}

bool Create(UINT fw, UINT fh) {
    if (!g_ready || g_pending_count || fw < 128 || fh < 128 || fw > 2048 || fh > 2048 || g_config.zero_guidance_fast_path || !g_config.persistent_buffers || g_config.tiled_subrects || g_options.guidance_mode != 1) return 0;
    Close();
    probe_fw = fw; probe_fh = fh;
    auto heap = HeapProperties(D3D12_HEAP_TYPE_DEFAULT);
    auto desc = BufferDescription(UINT64(fw)*fh*8);
    probe_size = g_device->GetResourceAllocationInfo(0,1,&desc).SizeInBytes;
    for (int i=0; i<g_slot_count; ++i) {
        if (FAILED(g_device->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_SHARED,&desc,D3D12_RESOURCE_STATE_COMMON,
            nullptr,IID_PPV_ARGS(&probe_sources[i])))) return false;
        if (FAILED(g_device->CreateSharedHandle(probe_sources[i],nullptr,GENERIC_ALL,nullptr,&probe_handles[i]))) return false;
    }
    auto tex = g_slots[0].motion->GetDesc();
    g_device->GetCopyableFootprints(&tex,0,1,0,&probe_motion.footprint,&probe_motion.rows,&probe_motion.row_size,&probe_motion.total_size);
    desc = BufferDescription(probe_motion.total_size);
    desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    if (FAILED(g_device->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_NONE,&desc,D3D12_RESOURCE_STATE_COMMON,
        nullptr,IID_PPV_ARGS(&probe_motion.resource)))) return 0;
    // Immutable O(width+height) coordinate table calculated once, in the same
    // double -> float order as OpenCV. No per-frame CPU flow processing.
    auto coordinate_heap = HeapProperties(D3D12_HEAP_TYPE_UPLOAD);
    auto coordinate_desc = BufferDescription(UINT64(g_width+g_height)*8);
    if (FAILED(g_device->CreateCommittedResource(&coordinate_heap,D3D12_HEAP_FLAG_NONE,&coordinate_desc,
        D3D12_RESOURCE_STATE_GENERIC_READ,nullptr,IID_PPV_ARGS(&probe_coordinates)))) return 0;
    unsigned char *mapping = nullptr;
    D3D12_RANGE empty = {};
    if (FAILED(probe_coordinates->Map(0,&empty,reinterpret_cast<void**>(&mapping)))) return 0;
    for (UINT i=0; i<g_width+g_height; ++i) {
        bool horizontal = i<g_width;
        UINT pos = horizontal ? i : i-g_width;
        UINT src = horizontal ? fw : fh, dst = horizontal ? g_width : g_height;
        float coordinate = static_cast<float>((pos+0.5)*(double(src)/dst)-0.5);
        int base = static_cast<int>(std::floor(coordinate));
        float fraction = coordinate-base;
        if (horizontal && (base<0 || base>=int(src)-1)) {
            base = std::max(0,std::min(base,int(src)-1)); fraction=0;
        }
        memcpy(mapping+8*i,&base,4); memcpy(mapping+8*i+4,&fraction,4);
    }
    probe_coordinates->Unmap(0,nullptr);
    D3D12_ROOT_PARAMETER parameters[4] = {};
    parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_SRV;
    parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    parameters[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    parameters[2].Constants.Num32BitValues = 8;
    parameters[3].ParameterType = D3D12_ROOT_PARAMETER_TYPE_SRV;
    parameters[3].Descriptor.ShaderRegister = 1;
    D3D12_ROOT_SIGNATURE_DESC root = {};
    root.NumParameters = 4; root.pParameters = parameters;
    ID3DBlob *blob = nullptr, *error = nullptr;
    HRESULT hr = D3D12SerializeRootSignature(&root,D3D_ROOT_SIGNATURE_VERSION_1,&blob,&error);
    if (error) { Log("probe root: %s", (char*)error->GetBufferPointer()); Release(error); }
    if (FAILED(hr)) { Release(blob); return 0; }
    hr = g_device->CreateRootSignature(0,blob->GetBufferPointer(),blob->GetBufferSize(),IID_PPV_ARGS(&probe_root));
    Release(blob);
    if (FAILED(hr)) return 0;
    hr = D3DCompile(probe_shader,strlen(probe_shader),"flow_interop",nullptr,nullptr,"main","cs_5_0",
        D3DCOMPILE_OPTIMIZATION_LEVEL3 | D3DCOMPILE_IEEE_STRICTNESS,0,&blob,&error);
    if (error) { Log("probe shader: %s", (char*)error->GetBufferPointer()); Release(error); }
    if (FAILED(hr)) { Release(blob); return 0; }
    D3D12_COMPUTE_PIPELINE_STATE_DESC pipeline = {};
    pipeline.pRootSignature = probe_root;
    pipeline.CS = {blob->GetBufferPointer(),blob->GetBufferSize()};
    hr = g_device->CreateComputePipelineState(&pipeline,IID_PPV_ARGS(&probe_pipeline));
    Release(blob);
    if (FAILED(hr)) return 0;
    return 1;
}


} // namespace gpu_flow
