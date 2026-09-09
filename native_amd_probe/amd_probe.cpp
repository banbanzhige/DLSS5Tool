// DLSS5Tool AMD developer probe. Own D3D12 application using the public FSR API.
// No hooks, binary patches, private entrypoints, or upstream code extraction.
// The user's unmodified mod may hook this application's ordinary FSR dispatches.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

// The SDK header declares provider functions dllexport. We are a dynamic API
// consumer, not a provider: suppress that annotation on the declarations only.
#pragma push_macro("__declspec")
#define __declspec(x)
#include <ffx_api/ffx_api.h>
#pragma pop_macro("__declspec")
#include <ffx_api/ffx_upscale.h>
#include <ffx_api/dx12/ffx_api_dx12.h>

using Microsoft::WRL::ComPtr;
namespace fs = std::filesystem;
static FILE* journal = nullptr;
static const char* stage = "starting";
static ComPtr<ID3D12Device> device;

static std::string Utf8(const wchar_t* value) {
    const int n = WideCharToMultiByte(CP_UTF8, 0, value, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return "";
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, value, -1, s.data(), n, nullptr, nullptr);
    s.pop_back();
    return s;
}
static std::string Json(const std::string& value) {
    std::string s = "\"";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { s += '\\'; s += c; }
        else if (c < 32) { char b[7]; sprintf_s(b, "\\u%04x", c); s += b; }
        else s += c;
    }
    return s + "\"";
}
static void Event(const std::string& line) {
    if (journal) { fprintf(journal, "%s\n", line.c_str()); fflush(journal); }
    printf("%s\n", line.c_str()); fflush(stdout);
}
static void Stage(const char* next) { stage = next; Event("{\"stage\":" + Json(stage) + "}"); }
static void Check(HRESULT hr, const char* action) {
    if (SUCCEEDED(hr)) return;
    char buf[256];
    sprintf_s(buf, "%s: HRESULT=0x%08lX device_removed=0x%08lX", action,
        static_cast<unsigned long>(hr), device ? static_cast<unsigned long>(device->GetDeviceRemovedReason()) : 0);
    throw std::runtime_error(buf);
}
static void FfxCheck(ffxReturnCode_t code, const char* action) {
    if (code != FFX_API_RETURN_OK) throw std::runtime_error(std::string(action) + ": FSR code=" + std::to_string(code));
}
static LONG WINAPI Crash(EXCEPTION_POINTERS* ep) {
    // No memory/stack dump or absolute addresses/paths: module basename and RVA
    // let a remote developer identify a failing build without personal data.
    HMODULE module = nullptr;
    char modulePath[MAX_PATH] = "unknown";
    unsigned long long offset = 0;
    if (GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCSTR>(ep->ExceptionRecord->ExceptionAddress), &module)) {
        GetModuleFileNameA(module, modulePath, MAX_PATH);
        offset = reinterpret_cast<uintptr_t>(ep->ExceptionRecord->ExceptionAddress) - reinterpret_cast<uintptr_t>(module);
    }
    const char* name = strrchr(modulePath, '\\'); name = name ? name + 1 : modulePath;
    if (journal) {
        fprintf(journal, "{\"crash\":true,\"exception\":%lu,\"stage\":\"%s\",\"module\":\"%s\",\"rva\":%llu}\n",
            ep->ExceptionRecord->ExceptionCode, stage, name, offset);
        fflush(journal);
    }
    TerminateProcess(GetCurrentProcess(), 90);
    return EXCEPTION_EXECUTE_HANDLER;
}
static void Pump() {
    MSG msg;
    while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) { TranslateMessage(&msg); DispatchMessageW(&msg); }
}
static void StartupWait(int milliseconds) {
    const auto until = GetTickCount64() + milliseconds;
    while (GetTickCount64() < until) { Pump(); Sleep(20); }
}
static LRESULT CALLBACK WindowProc(HWND hwnd, UINT msg, WPARAM w, LPARAM l) {
    return DefWindowProcW(hwnd, msg, w, l);
}
struct Options {
    std::wstring mode = L"inventory";
    fs::path runtime, fsr, input, output;
    UINT width = 640, height = 360, frames = 24;
    int adapter = -1;
    bool shared = true, debug = false;
};
static Options Parse(int argc, wchar_t** argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::wstring key = argv[i];
        if (key == L"--debug") { o.debug = true; continue; }
        if (i + 1 >= argc) throw std::runtime_error("missing option value");
        std::wstring v = argv[++i];
        if (key == L"--mode") o.mode = v;
        else if (key == L"--runtime") o.runtime = fs::absolute(v);
        else if (key == L"--fsr") o.fsr = fs::absolute(v);
        else if (key == L"--input") o.input = fs::absolute(v);
        else if (key == L"--output") o.output = fs::absolute(v);
        else if (key == L"--width") o.width = std::stoul(v);
        else if (key == L"--height") o.height = std::stoul(v);
        else if (key == L"--frames") o.frames = std::stoul(v);
        else if (key == L"--adapter") o.adapter = std::stoi(v);
        else if (key == L"--shared") o.shared = v == L"1";
        else throw std::runtime_error("unknown option");
    }
    if (o.mode != L"inventory" && o.mode != L"copy" && o.mode != L"fsr" && o.mode != L"nr")
        throw std::runtime_error("invalid mode");
    if (o.width < 64 || o.height < 64 || o.width > 1920 || o.height > 1080 || o.frames < 1 || o.frames > 36)
        throw std::runtime_error("developer probe is limited to 64..1920 x 64..1080, 1..36 frames");
    if (o.output.empty()) throw std::runtime_error("--output is required");
    return o;
}
static std::vector<ComPtr<IDXGIAdapter1>> Inventory() {
    ComPtr<IDXGIFactory4> factory;
    Check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "CreateDXGIFactory1");
    std::vector<ComPtr<IDXGIAdapter1>> adapters;
    for (UINT i = 0;; ++i) {
        ComPtr<IDXGIAdapter1> adapter;
        HRESULT hr = factory->EnumAdapters1(i, &adapter);
        if (hr == DXGI_ERROR_NOT_FOUND) break;
        Check(hr, "EnumAdapters1");
        DXGI_ADAPTER_DESC1 d{}; Check(adapter->GetDesc1(&d), "GetDesc1");
        LARGE_INTEGER version{};
        adapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &version);
        std::string driver = std::to_string(HIWORD(version.HighPart)) + "." + std::to_string(LOWORD(version.HighPart)) + "." +
            std::to_string(HIWORD(version.LowPart)) + "." + std::to_string(LOWORD(version.LowPart));
        Event("{\"adapter\":" + std::to_string(i) + ",\"name\":" + Json(Utf8(d.Description)) +
            ",\"vendor\":" + std::to_string(d.VendorId) + ",\"device_id\":" + std::to_string(d.DeviceId) +
            ",\"vram_mib\":" + std::to_string(d.DedicatedVideoMemory / (1024 * 1024)) +
            ",\"software\":" + (d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE ? "true" : "false") + ",\"driver\":" + Json(driver) + "}");
        adapters.push_back(adapter);
    }
    return adapters;
}
struct Texture {
    ComPtr<ID3D12Resource> resource, upload, readback;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};
    UINT rows = 0;
    UINT64 rowBytes = 0, totalBytes = 0;
    D3D12_RESOURCE_STATES state = D3D12_RESOURCE_STATE_COMMON;
};
static ComPtr<ID3D12Resource> Buffer(UINT64 size, D3D12_HEAP_TYPE type) {
    D3D12_HEAP_PROPERTIES heap{}; heap.Type = type; heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    D3D12_RESOURCE_DESC desc{}; desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; desc.Width = size;
    desc.Height = desc.DepthOrArraySize = desc.MipLevels = 1; desc.SampleDesc.Count = 1; desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ComPtr<ID3D12Resource> r;
    Check(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
        type == D3D12_HEAP_TYPE_UPLOAD ? D3D12_RESOURCE_STATE_GENERIC_READ : D3D12_RESOURCE_STATE_COPY_DEST,
        nullptr, IID_PPV_ARGS(&r)), "Create staging buffer");
    return r;
}
static Texture MakeTexture(UINT w, UINT h, DXGI_FORMAT format, bool shared, const wchar_t* name, bool output = false) {
    Texture t;
    D3D12_HEAP_PROPERTIES heap{}; heap.Type = D3D12_HEAP_TYPE_DEFAULT; heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    D3D12_RESOURCE_DESC desc{}; desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D; desc.Width = w; desc.Height = h;
    desc.DepthOrArraySize = desc.MipLevels = 1; desc.Format = format; desc.SampleDesc.Count = 1;
    desc.Flags = output ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE;
    Check(device->CreateCommittedResource(&heap, shared ? D3D12_HEAP_FLAG_SHARED : D3D12_HEAP_FLAG_NONE,
        &desc, t.state, nullptr, IID_PPV_ARGS(&t.resource)), "Create texture");
    t.resource->SetName(name);
    device->GetCopyableFootprints(&desc, 0, 1, 0, &t.footprint, &t.rows, &t.rowBytes, &t.totalBytes);
    t.upload = Buffer(t.totalBytes, D3D12_HEAP_TYPE_UPLOAD);
    t.readback = Buffer(t.totalBytes, D3D12_HEAP_TYPE_READBACK);
    return t;
}
static void Transition(ID3D12GraphicsCommandList* list, ID3D12Resource* r, D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
    if (before == after) return;
    D3D12_RESOURCE_BARRIER b{}; b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = r; b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b.Transition.StateBefore = before; b.Transition.StateAfter = after;
    list->ResourceBarrier(1, &b);
}
static void Transition(ID3D12GraphicsCommandList* list, Texture& t, D3D12_RESOURCE_STATES after) {
    Transition(list, t.resource.Get(), t.state, after); t.state = after;
}
static void Upload(ID3D12GraphicsCommandList* list, Texture& t, const void* packed) {
    void* mapped = nullptr; D3D12_RANGE empty{0, 0};
    Check(t.upload->Map(0, &empty, &mapped), "Upload Map");
    for (UINT y = 0; y < t.rows; ++y)
        memcpy(static_cast<char*>(mapped) + t.footprint.Offset + size_t(y) * t.footprint.Footprint.RowPitch,
            static_cast<const char*>(packed) + size_t(y) * t.rowBytes, size_t(t.rowBytes));
    t.upload->Unmap(0, nullptr);
    Transition(list, t, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_TEXTURE_COPY_LOCATION src{}; src.pResource = t.upload.Get(); src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; src.PlacedFootprint = t.footprint;
    D3D12_TEXTURE_COPY_LOCATION dst{}; dst.pResource = t.resource.Get(); dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    Transition(list, t, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
}
static void CopyReadback(ID3D12GraphicsCommandList* list, Texture& t) {
    const auto previous = t.state; Transition(list, t, D3D12_RESOURCE_STATE_COPY_SOURCE);
    D3D12_TEXTURE_COPY_LOCATION src{}; src.pResource = t.resource.Get(); src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    D3D12_TEXTURE_COPY_LOCATION dst{}; dst.pResource = t.readback.Get(); dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; dst.PlacedFootprint = t.footprint;
    list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    Transition(list, t, previous);
}
static void Save(Texture& t, const fs::path& file) {
    void* mapped = nullptr; D3D12_RANGE range{0, SIZE_T(t.totalBytes)};
    Check(t.readback->Map(0, &range, &mapped), "Readback Map");
    std::ofstream out(file, std::ios::binary);
    for (UINT y = 0; y < t.rows; ++y)
        out.write(static_cast<const char*>(mapped) + t.footprint.Offset + size_t(y) * t.footprint.Footprint.RowPitch, t.rowBytes);
    out.close(); D3D12_RANGE empty{0, 0}; t.readback->Unmap(0, &empty);
    if (!out) throw std::runtime_error("cannot write frame output");
}
static void Message(uint32_t type, const wchar_t* value) {
    Event("{\"fsr_message_type\":" + std::to_string(type) + ",\"message\":" + Json(Utf8(value)) + "}");
}
struct Host {
    // Keep all objects alive until the disposable process exits. In particular,
    // never release resources after an upstream GPU synchronization timeout.
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList> list;
    ComPtr<ID3D12Fence> fence;
    HANDLE event = nullptr;
    UINT64 fenceValue = 0;
    ComPtr<IDXGISwapChain3> swapchain;
    HWND window = nullptr;
    ComPtr<ID3D12DescriptorHeap> rtvHeap, srvHeap;
    ComPtr<ID3D12RootSignature> rootSignature;
    ComPtr<ID3D12PipelineState> pipeline;
    void Begin() { Check(allocator->Reset(), "Allocator Reset"); Check(list->Reset(allocator.Get(), nullptr), "List Reset"); }
    void Wait() {
        Check(queue->Signal(fence.Get(), ++fenceValue), "Queue Signal");
        if (fence->GetCompletedValue() < fenceValue) {
            Check(fence->SetEventOnCompletion(fenceValue, event), "Fence SetEvent");
            if (WaitForSingleObject(event, 15000) != WAIT_OBJECT_0) throw std::runtime_error("GPU fence timeout; aborting without resource teardown");
        }
        Check(device->GetDeviceRemovedReason(), "Device state");
    }
    void Submit() { Check(list->Close(), "List Close"); ID3D12CommandList* lists[] = {list.Get()}; queue->ExecuteCommandLists(1, lists); Wait(); }
    void Init(IDXGIAdapter1* adapter, UINT width, UINT height) {
        Check(D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device)), "D3D12CreateDevice");
        D3D12_COMMAND_QUEUE_DESC q{}; q.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
        Check(device->CreateCommandQueue(&q, IID_PPV_ARGS(&queue)), "CreateCommandQueue");
        Check(device->CreateCommandAllocator(q.Type, IID_PPV_ARGS(&allocator)), "CreateCommandAllocator");
        Check(device->CreateCommandList(0, q.Type, allocator.Get(), nullptr, IID_PPV_ARGS(&list)), "CreateCommandList");
        Check(list->Close(), "Initial List Close");
        Check(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)), "CreateFence");
        event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!event) throw std::runtime_error("CreateEvent failed");
        WNDCLASSW wc{}; wc.lpfnWndProc = WindowProc; wc.hInstance = GetModuleHandleW(nullptr); wc.lpszClassName = L"DLSS5ToolAmdProbe";
        RegisterClassW(&wc);
        window = CreateWindowExW(0, wc.lpszClassName, L"DLSS5Tool AMD probe (offscreen)", WS_OVERLAPPEDWINDOW,
            0, 0, width, height, nullptr, nullptr, wc.hInstance, nullptr);
        if (!window) throw std::runtime_error("CreateWindow failed");
        ComPtr<IDXGIFactory4> factory; Check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "Create factory");
        DXGI_SWAP_CHAIN_DESC1 sd{}; sd.Width = width; sd.Height = height; sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        sd.SampleDesc.Count = 1; sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT; sd.BufferCount = 2; sd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
        ComPtr<IDXGISwapChain1> sc;
        Check(factory->CreateSwapChainForHwnd(queue.Get(), window, &sd, nullptr, nullptr, &sc), "CreateSwapChainForHwnd");
        Check(sc.As(&swapchain), "Query swapchain");
        factory->MakeWindowAssociation(window, DXGI_MWA_NO_ALT_ENTER);
        D3D12_DESCRIPTOR_HEAP_DESC heap{}; heap.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV; heap.NumDescriptors = 1;
        Check(device->CreateDescriptorHeap(&heap, IID_PPV_ARGS(&rtvHeap)), "Create RTV heap");
        heap.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV; heap.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
        Check(device->CreateDescriptorHeap(&heap, IID_PPV_ARGS(&srvHeap)), "Create SRV heap");
    }
    void SetupDraw(Texture& output) {
        D3D12_DESCRIPTOR_RANGE range{}; range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV; range.NumDescriptors = 1;
        D3D12_ROOT_PARAMETER param{}; param.ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        param.DescriptorTable.NumDescriptorRanges = 1; param.DescriptorTable.pDescriptorRanges = &range; param.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
        D3D12_STATIC_SAMPLER_DESC sampler{}; sampler.Filter = D3D12_FILTER_MIN_MAG_MIP_POINT;
        sampler.AddressU = sampler.AddressV = sampler.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
        sampler.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL; sampler.MaxLOD = D3D12_FLOAT32_MAX;
        D3D12_ROOT_SIGNATURE_DESC rd{}; rd.NumParameters = 1; rd.pParameters = &param; rd.NumStaticSamplers = 1; rd.pStaticSamplers = &sampler;
        ComPtr<ID3DBlob> sig, err;
        Check(D3D12SerializeRootSignature(&rd, D3D_ROOT_SIGNATURE_VERSION_1, &sig, &err), "Serialize root signature");
        Check(device->CreateRootSignature(0, sig->GetBufferPointer(), sig->GetBufferSize(), IID_PPV_ARGS(&rootSignature)), "Create root signature");
        const char* shader = "Texture2D<float4> tex:register(t0); SamplerState s:register(s0);"
            "struct V{float4 p:SV_Position;float2 uv:TEXCOORD0;};"
            "V vs(uint i:SV_VertexID){V o;float2 u=float2((i<<1)&2,i&2);o.uv=u;o.p=float4(u*float2(2,-2)+float2(-1,1),0,1);return o;}"
            "float4 ps(V i):SV_Target{return float4(saturate(tex.SampleLevel(s,i.uv,0).rgb),1);}";
        ComPtr<ID3DBlob> vs, ps;
        Check(D3DCompile(shader, strlen(shader), nullptr, nullptr, nullptr, "vs", "vs_5_0", 0, 0, &vs, &err), "Compile VS");
        Check(D3DCompile(shader, strlen(shader), nullptr, nullptr, nullptr, "ps", "ps_5_0", 0, 0, &ps, &err), "Compile PS");
        D3D12_GRAPHICS_PIPELINE_STATE_DESC p{}; p.pRootSignature = rootSignature.Get();
        p.VS = {vs->GetBufferPointer(), vs->GetBufferSize()}; p.PS = {ps->GetBufferPointer(), ps->GetBufferSize()};
        p.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
        p.SampleMask = UINT_MAX; p.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID; p.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
        p.RasterizerState.DepthClipEnable = TRUE; p.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
        p.NumRenderTargets = 1; p.RTVFormats[0] = DXGI_FORMAT_R8G8B8A8_UNORM; p.SampleDesc.Count = 1;
        Check(device->CreateGraphicsPipelineState(&p, IID_PPV_ARGS(&pipeline)), "Create draw pipeline");
        D3D12_SHADER_RESOURCE_VIEW_DESC srv{}; srv.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; srv.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
        srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING; srv.Texture2D.MipLevels = 1;
        device->CreateShaderResourceView(output.resource.Get(), &srv, srvHeap->GetCPUDescriptorHandleForHeapStart());
    }
    ComPtr<ID3D12Resource> Draw(Texture& output, UINT width, UINT height) {
        ComPtr<ID3D12Resource> back;
        Check(swapchain->GetBuffer(swapchain->GetCurrentBackBufferIndex(), IID_PPV_ARGS(&back)), "Get backbuffer");
        device->CreateRenderTargetView(back.Get(), nullptr, rtvHeap->GetCPUDescriptorHandleForHeapStart());
        Begin(); Transition(list.Get(), output, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
        Transition(list.Get(), back.Get(), D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_RENDER_TARGET);
        auto rtv = rtvHeap->GetCPUDescriptorHandleForHeapStart(); list->OMSetRenderTargets(1, &rtv, FALSE, nullptr);
        D3D12_VIEWPORT vp{0, 0, float(width), float(height), 0, 1}; D3D12_RECT rect{0, 0, LONG(width), LONG(height)};
        list->RSSetViewports(1, &vp); list->RSSetScissorRects(1, &rect);
        list->SetPipelineState(pipeline.Get()); list->SetGraphicsRootSignature(rootSignature.Get());
        ID3D12DescriptorHeap* heaps[] = {srvHeap.Get()}; list->SetDescriptorHeaps(1, heaps);
        list->SetGraphicsRootDescriptorTable(0, srvHeap->GetGPUDescriptorHandleForHeapStart());
        list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST); list->DrawInstanced(3, 1, 0, 0);
        Transition(list.Get(), output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        Transition(list.Get(), back.Get(), D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_PRESENT);
        Submit(); Check(swapchain->Present(0, 0), "Present"); Pump(); Wait();
        return back;
    }
};
static void Run(const Options& o) {
    Stage("inventory"); auto adapters = Inventory();
    if (o.mode == L"inventory") { Event("{\"completed\":true,\"kind\":\"inventory\"}"); return; }
    int chosen = o.adapter;
    if (chosen < 0) {
        for (size_t i = 0; i < adapters.size(); ++i) {
            DXGI_ADAPTER_DESC1 desc{}; adapters[i]->GetDesc1(&desc);
            if (desc.VendorId == 0x1002 && !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) { chosen = int(i); break; }
        }
    }
    if (chosen < 0 || chosen >= int(adapters.size())) throw std::runtime_error("no selected AMD adapter; inventory only on this machine");
    DXGI_ADAPTER_DESC1 selected{}; adapters[chosen]->GetDesc1(&selected);
    if (o.mode == L"nr" && selected.VendorId != 0x1002) throw std::runtime_error("NR probe requires AMD; refusing to load third-party mod on this adapter");
    Event("{\"selected_adapter\":" + std::to_string(chosen) + ",\"mode\":" + Json(Utf8(o.mode.c_str())) +
        ",\"shared\":" + (o.shared ? "true" : "false") + ",\"width\":" + std::to_string(o.width) + ",\"height\":" + std::to_string(o.height) + "}");
    const auto frameBytes = UINT64(o.width) * o.height * 8;
    if (!fs::is_regular_file(o.input) || fs::file_size(o.input) != frameBytes * o.frames)
        throw std::runtime_error("input stream size mismatch");
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (o.debug) {
        ComPtr<ID3D12Debug> dbg;
        if (SUCCEEDED(D3D12GetDebugInterface(IID_PPV_ARGS(&dbg)))) dbg->EnableDebugLayer();
        else Event("{\"warning\":\"D3D12 debug layer unavailable\"}");
    }
    PfnFfxCreateContext create = nullptr; PfnFfxDispatch dispatch = nullptr; PfnFfxQuery query = nullptr;
    if (o.mode != L"copy") {
        Stage("load_fsr");
        auto mod = LoadLibraryExW(o.fsr.c_str(), nullptr, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        if (!mod) throw std::runtime_error("FSR DLL load failed, Win32=" + std::to_string(GetLastError()));
        create = reinterpret_cast<PfnFfxCreateContext>(GetProcAddress(mod, "ffxCreateContext"));
        dispatch = reinterpret_cast<PfnFfxDispatch>(GetProcAddress(mod, "ffxDispatch"));
        query = reinterpret_cast<PfnFfxQuery>(GetProcAddress(mod, "ffxQuery"));
        if (!create || !dispatch || !query) throw std::runtime_error("missing public FSR API exports");
    }
    if (o.mode == L"nr") {
        Stage("load_user_mod");
        const auto path = o.runtime / "version.dll";
        auto mod = LoadLibraryExW(path.c_str(), nullptr, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        if (!mod) throw std::runtime_error("version.dll load failed (or dependency such as amdhip64_7.dll missing), Win32=" + std::to_string(GetLastError()));
        // No public readiness API exists. Log this fixed warm-up, then test
        // actual output; timing alone is never accepted as proof of readiness.
        Event("{\"startup_wait_ms\":1500}"); StartupWait(1500);
    }
    Stage("create_device_swapchain");
    auto host = new Host(); host->Init(adapters[chosen].Get(), o.width, o.height);
    Stage("create_resources");
    auto color = new Texture(MakeTexture(o.width, o.height, DXGI_FORMAT_R16G16B16A16_FLOAT, o.shared, L"Probe color"));
    auto output = new Texture(MakeTexture(o.width, o.height, DXGI_FORMAT_R16G16B16A16_FLOAT, o.shared, L"Probe output", true));
    auto motion = new Texture(MakeTexture(o.width, o.height, DXGI_FORMAT_R16G16_FLOAT, o.shared, L"Probe zero motion"));
    auto depth = new Texture(MakeTexture(o.width, o.height, DXGI_FORMAT_R32_FLOAT, o.shared, L"Probe zero depth"));
    auto exposure = new Texture(MakeTexture(1, 1, DXGI_FORMAT_R32_FLOAT, o.shared, L"Probe exposure"));
    std::vector<char> zeros(size_t(frameBytes), 0); float expo = 1.0f;
    host->Begin(); Upload(host->list.Get(), *output, zeros.data());
    Upload(host->list.Get(), *motion, zeros.data()); Upload(host->list.Get(), *depth, zeros.data()); Upload(host->list.Get(), *exposure, &expo);
    Transition(host->list.Get(), *output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS); host->Submit();
    host->SetupDraw(*output);
    for (int i = 0; i < 3; ++i) { host->Draw(*output, o.width, o.height); StartupWait(100); }
    ffxContext context = nullptr;
    ffxCreateBackendDX12Desc backend{}; backend.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12; backend.device = device.Get();
    ffxCreateContextDescUpscale desc{}; desc.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    desc.header.pNext = &backend.header;
    desc.flags = FFX_UPSCALE_ENABLE_DEPTH_INVERTED | FFX_UPSCALE_ENABLE_DISPLAY_RESOLUTION_MOTION_VECTORS | FFX_UPSCALE_ENABLE_NON_LINEAR_COLORSPACE;
    if (o.debug) desc.flags |= FFX_UPSCALE_ENABLE_DEBUG_CHECKING;
    desc.maxRenderSize = desc.maxUpscaleSize = {o.width, o.height}; desc.fpMessage = Message;
    ffxOverrideVersion overrideVersion{};
    if (create) {
        Stage("create_fsr_context");
        uint64_t count = 0;
        ffxQueryDescGetVersions versions{}; versions.header.type = FFX_API_QUERY_DESC_TYPE_GET_VERSIONS;
        versions.createDescType = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE; versions.device = device.Get(); versions.outputCount = &count;
        FfxCheck(query(nullptr, &versions.header), "Query version count");
        if (count == 0 || count > 32) throw std::runtime_error("unexpected FSR provider count");
        std::vector<uint64_t> ids(count); std::vector<const char*> names(count);
        versions.versionIds = ids.data(); versions.versionNames = names.data();
        FfxCheck(query(nullptr, &versions.header), "Query versions");
        bool matched = false;
        for (size_t i = 0; i < ids.size(); ++i) {
            Event("{\"fsr_provider\":" + Json(names[i] ? names[i] : "unknown") + "}");
            if (!matched && names[i] && strstr(names[i], "3.1")) {
                overrideVersion.header.type = FFX_API_DESC_TYPE_OVERRIDE_VERSION;
                overrideVersion.header.pNext = desc.header.pNext; overrideVersion.versionId = ids[i];
                desc.header.pNext = &overrideVersion.header; matched = true;
            }
        }
        if (!matched) throw std::runtime_error("FSR 3.1 provider missing; no implicit driver override allowed");
        FfxCheck(create(&context, &desc.header, nullptr), "Create FSR context");
    }
    Stage("processing");
    std::ifstream input(o.input, std::ios::binary); std::vector<char> frame(size_t(frameBytes), 0);
    for (UINT index = 0; index < o.frames; ++index) {
        auto start = std::chrono::steady_clock::now();
        Event("{\"frame_start\":" + std::to_string(index) + "}");
        input.read(frame.data(), frame.size()); if (!input) throw std::runtime_error("input frame read failed");
        host->Begin(); Upload(host->list.Get(), *color, frame.data()); host->Submit();
        host->Begin();
        if (dispatch) {
            ffxDispatchDescUpscale d{}; d.header.type = FFX_API_DISPATCH_DESC_TYPE_UPSCALE; d.commandList = host->list.Get();
            d.color = ffxApiGetResourceDX12(color->resource.Get()); d.motionVectors = ffxApiGetResourceDX12(motion->resource.Get());
            d.depth = ffxApiGetResourceDX12(depth->resource.Get()); d.exposure = ffxApiGetResourceDX12(exposure->resource.Get());
            d.output = ffxApiGetResourceDX12(output->resource.Get(), FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
            d.renderSize = d.upscaleSize = {o.width, o.height}; d.motionVectorScale = {1, 1};
            d.frameTimeDelta = 1000.0f / 30; d.preExposure = 1; d.reset = index == 0 || index == o.frames / 2;
            d.cameraNear = 0.1f; d.cameraFar = 1000; d.cameraFovAngleVertical = 1.0f; d.viewSpaceToMetersFactor = 1;
            d.flags = FFX_UPSCALE_FLAG_NON_LINEAR_COLOR_SRGB;
            FfxCheck(dispatch(&context, &d.header), "FSR dispatch");
        } else {
            Transition(host->list.Get(), *color, D3D12_RESOURCE_STATE_COPY_SOURCE);
            Transition(host->list.Get(), *output, D3D12_RESOURCE_STATE_COPY_DEST);
            host->list->CopyResource(output->resource.Get(), color->resource.Get());
            Transition(host->list.Get(), *color, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
            Transition(host->list.Get(), *output, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        }
        host->Submit();
        auto back = host->Draw(*output, o.width, o.height);
        host->Begin(); CopyReadback(host->list.Get(), *output); host->Submit();
        char name[40]; sprintf_s(name, "frame-%03u.rgba16f", index); Save(*output, o.output / name);
        double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
        Event("{\"frame_done\":" + std::to_string(index) + ",\"wall_ms\":" + std::to_string(ms) + "}");
    }
    Stage("completed");
    // completion means this public FSR application finished, NOT that NR ran.
    // The controller compares a no-mod baseline and requires runtime evidence.
    Event("{\"completed\":true,\"frames\":" + std::to_string(o.frames) + ",\"nr_verified\":false}");
}
int wmain(int argc, wchar_t** argv) {
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    SetUnhandledExceptionFilter(Crash);
    int code = 0;
    try {
        const auto options = Parse(argc, argv);
        fs::create_directories(options.output);
        _wfopen_s(&journal, (options.output / "native.jsonl").c_str(), L"wb");
        if (!journal) throw std::runtime_error("cannot open journal");
        Event("{\"probe_version\":\"amd-dev1\",\"fsr_sdk\":\"1.1.4\",\"public_api_only\":true}");
        Run(options);
    } catch (const std::exception& e) {
        Event("{\"failed\":true,\"stage\":" + Json(stage) + ",\"error\":" + Json(e.what()) + "}"); code = 2;
    }
    if (journal) { fflush(journal); fclose(journal); journal = nullptr; }
    fflush(stdout); fflush(stderr);
    // All host and mod resources are process-lifetime. Skip third-party detach
    // and free no D3D12 resource that its asynchronous worker might still use.
    TerminateProcess(GetCurrentProcess(), code);
    return code;
}
