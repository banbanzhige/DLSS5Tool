// Research only: isolated DLSS color input bridge; synchronous ownership.
#include "../native/host_v2/dlssnr_host_v2.cpp"

namespace {
ID3D12Resource* resident_color = nullptr;
ID3D12Resource* encoder_frame = nullptr;
HANDLE encoder_handle = nullptr;
}

extern "C" __declspec(dllexport) int probe_color_open(HANDLE handle, const LUID* luid) {
    if (!g_ready || resident_color || !luid || !handle || g_frame_format != FrameFormat::Rgba8) return 0;
    auto actual = g_device->GetAdapterLuid();
    if (actual.HighPart != luid->HighPart || actual.LowPart != luid->LowPart) return 0;
    if (FAILED(g_device->OpenSharedHandle(handle, IID_PPV_ARGS(&resident_color)))) return 0;
    auto desc = resident_color->GetDesc();
    if (desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER || desc.Width < g_slots[0].color_upload.total_size) {
        Release(resident_color); return 0;
    }
    return 1;
}

extern "C" __declspec(dllexport) int probe_color_process(const void* input, void* output, int shared, int reset, int readback) {
    if (!g_ready || g_options.guidance_mode || g_slot_count != 1 || (readback && !output)
        || (shared ? !resident_color : !input)) return 0;
    auto& slot = g_slots[0];
    if (shared) {
        if (!BeginCommands(slot)) return 0;
        auto src = Transition(resident_color, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
        auto dst = Transition(slot.color, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
        slot.list->ResourceBarrier(1, &src); slot.list->ResourceBarrier(1, &dst);
        D3D12_TEXTURE_COPY_LOCATION from{}, to{};
        from.pResource = resident_color; from.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        from.PlacedFootprint = slot.color_upload.footprint;
        to.pResource = slot.color; to.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        slot.list->CopyTextureRegion(&to, 0, 0, 0, &from, nullptr);
        src = Transition(resident_color, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
        dst = Transition(slot.color, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COMMON);
        slot.list->ResourceBarrier(1, &src); slot.list->ResourceBarrier(1, &dst);
        if (!SubmitCommands(slot, true)) return 0;
    } else if (!SubmitUploadImmediate(slot, slot.color, &slot.color_upload, input, FrameRowPitch())) return 0;
    if (!BeginCommands(slot)) return 0;
    SetEvaluationParameters(slot, reset != 0);
    if (!NgxSucceeded(SafeEvaluate(slot.list))) { AbortCommands(slot); return 0; }
    if (!SubmitCommands(slot, true)) return 0;
    return !readback || SubmitReadbackImmediate(slot, output);
}

extern "C" __declspec(dllexport) int probe_color_read(void* output) {
    return g_ready && output && SubmitReadbackImmediate(g_slots[0], output);
}

extern "C" __declspec(dllexport) void probe_color_close() {
    if (g_initialized) WaitFence(g_fence_value);
    Release(resident_color);
    Release(encoder_frame);
    if (encoder_handle) CloseHandle(encoder_handle);
    encoder_handle = nullptr;
}

extern "C" __declspec(dllexport) int probe_output_share(HANDLE* handle, UINT64* allocation, UINT* pitch, LUID* luid) {
    if (!g_ready || encoder_frame || g_frame_format != FrameFormat::Rgba8 || !handle || !allocation || !pitch || !luid) return 0;
    auto desc = BufferDescription(g_slots[0].output_readback.total_size);
    auto heap = HeapProperties(D3D12_HEAP_TYPE_DEFAULT);
    if (FAILED(g_device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_SHARED, &desc,
        D3D12_RESOURCE_STATE_COMMON, nullptr, IID_PPV_ARGS(&encoder_frame)))) return 0;
    if (FAILED(g_device->CreateSharedHandle(encoder_frame, nullptr, GENERIC_ALL, nullptr, &encoder_handle))) return 0;
    *handle = encoder_handle;
    *allocation = g_device->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
    *pitch = g_slots[0].output_readback.footprint.Footprint.RowPitch;
    *luid = g_device->GetAdapterLuid();
    return 1;
}

extern "C" __declspec(dllexport) int probe_output_prepare(const void* cpu_bounce) {
    if (!g_ready || !encoder_frame) return 0;
    auto& slot = g_slots[0];
    // Reference bounce reloads the very same output pixels. Does NOT feed them
    // back into DLSS or advance its history. Encoder has finished before reuse.
    if (cpu_bounce) CopyRowsToStaging(slot.color_upload, cpu_bounce, FrameRowPitch());
    if (!BeginCommands(slot)) return 0;
    auto dst = Transition(encoder_frame, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_DEST);
    slot.list->ResourceBarrier(1, &dst);
    if (cpu_bounce) {
        slot.list->CopyBufferRegion(encoder_frame, 0, slot.color_upload.resource, 0, slot.color_upload.total_size);
    } else {
        auto src = Transition(slot.output, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COPY_SOURCE);
        slot.list->ResourceBarrier(1, &src);
        D3D12_TEXTURE_COPY_LOCATION from{}, to{};
        from.pResource = slot.output; from.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        to.pResource = encoder_frame; to.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        to.PlacedFootprint = slot.output_readback.footprint;
        slot.list->CopyTextureRegion(&to, 0, 0, 0, &from, nullptr);
        src = Transition(slot.output, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COMMON);
        slot.list->ResourceBarrier(1, &src);
    }
    dst = Transition(encoder_frame, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COMMON);
    slot.list->ResourceBarrier(1, &dst);
    return SubmitCommands(slot, true);
}
