// Included only by the explicitly compiled GPU_PIPELINE_PROBE worker.
namespace gpu_pipeline_probe {
ID3D12Resource* shared = nullptr;
HANDLE handle = nullptr;
D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};

void Init(FILE* wire, ID3D12Resource* output) {
    auto texture = output->GetDesc();
    UINT64 size = 0;
    g_device->GetCopyableFootprints(&texture, 0, 1, 0, &footprint, nullptr, nullptr, &size);
    auto buffer = BufferDescription(size);
    auto heap = HeapProperties(D3D12_HEAP_TYPE_DEFAULT);
    probe::Check(SUCCEEDED(g_device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_SHARED,
        &buffer, D3D12_RESOURCE_STATE_COMMON, nullptr, IID_PPV_ARGS(&shared))), "share output");
    probe::Check(SUCCEEDED(g_device->CreateSharedHandle(shared, nullptr, GENERIC_ALL, nullptr, &handle)), "share handle");
    const uint64_t message[] = {reinterpret_cast<uint64_t>(handle),
        g_device->GetResourceAllocationInfo(0,1,&buffer).SizeInBytes,
        footprint.Footprint.RowPitch, GetCurrentProcessId()};
    probe::Check(fwrite(message,1,sizeof(message),wire)==sizeof(message),"share descriptor pipe");
    fflush(wire);
}

void Record(ID3D12GraphicsCommandList* list, ID3D12Resource* output) {
    auto from = Transition(output,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_COPY_SOURCE);
    auto to = Transition(shared,D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_COPY_DEST);
    list->ResourceBarrier(1,&from); list->ResourceBarrier(1,&to);
    D3D12_TEXTURE_COPY_LOCATION src{},dst{};
    src.pResource=output;src.Type=D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    dst.pResource=shared;dst.Type=D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;dst.PlacedFootprint=footprint;
    list->CopyTextureRegion(&dst,0,0,0,&src,nullptr);
    from=Transition(output,D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_COMMON);
    to=Transition(shared,D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_COMMON);
    list->ResourceBarrier(1,&from);list->ResourceBarrier(1,&to);
}
}
