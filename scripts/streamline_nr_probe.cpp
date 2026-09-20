// Capability-only research probe. No NR Options ABI, evaluation or runtime patches.
// Optional OTA is off; process-local policy blocks updater child processes.
// Driver initialization may still write caches/logs; this is not a pure read-only API.
// Build against unmodified official Streamline v2.12.0 common headers.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <wintrust.h>
#include <softpub.h>
#include <tlhelp32.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <cstdio>
#include <cassert>
#include <cstring>
#include <filesystem>
#include <stdexcept>
#include <sl.h>

constexpr sl::Feature kNR = 1004; // Official v2.14.1 sl_core_types.h and 2.13 plugin metadata.
void Require(bool ok, const char* what) { if (!ok) throw std::runtime_error(what); }
void Result(const char* name, sl::Result r) {
    std::printf("RESULT %s=%d\n", name, static_cast<int>(r));
}
void Log(sl::LogType, const char* msg) { std::printf("SL %s\n", msg ? msg : ""); }
void Verify(const std::filesystem::path& path) {
    WINTRUST_FILE_INFO file{sizeof(file)};
    file.pcwszFilePath = path.c_str();
    WINTRUST_DATA data{sizeof(data)};
    data.dwUIChoice = WTD_UI_NONE;
    data.fdwRevocationChecks = WTD_REVOKE_NONE;
    data.dwUnionChoice = WTD_CHOICE_FILE;
    data.pFile = &file;
    data.dwStateAction = WTD_STATEACTION_VERIFY;
    data.dwProvFlags = WTD_CACHE_ONLY_URL_RETRIEVAL;
    GUID action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    const LONG result = WinVerifyTrust(nullptr, &action, &data);
    data.dwStateAction = WTD_STATEACTION_CLOSE;
    WinVerifyTrust(nullptr, &action, &data);
    std::printf("SIGNATURE %ls=0x%08lx\n", path.c_str(), result);
    Require(result == ERROR_SUCCESS, "Authenticode check failed");
}
template<class T> T* Export(HMODULE m, const char* name) {
    auto p = reinterpret_cast<T*>(GetProcAddress(m, name));
    Require(p != nullptr, name);
    return p;
}
void Modules(const char* phase) {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, GetCurrentProcessId());
    if (snapshot == INVALID_HANDLE_VALUE) return;
    MODULEENTRY32W entry{sizeof(entry)};
    if (Module32FirstW(snapshot, &entry)) do {
        if (wcsncmp(entry.szModule, L"sl.", 3) == 0 || wcsstr(entry.szModule, L"nvngx"))
            std::printf("MODULE %s %ls\n", phase, entry.szExePath);
    } while (Module32NextW(snapshot, &entry));
    CloseHandle(snapshot);
}
int wmain(int argc, wchar_t** argv) {
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    setvbuf(stdout, nullptr, _IONBF, 0);
    try {
        PROCESS_MITIGATION_CHILD_PROCESS_POLICY childPolicy{};
        childPolicy.NoChildProcessCreation = 1;
        Require(SetProcessMitigationPolicy(ProcessChildProcessPolicy, &childPolicy, sizeof(childPolicy)),
            "block child processes");
        std::puts("CONFIG child-process-creation=blocked");
        Require(argc == 4, "usage: probe plugins-directory model-directory logs-directory");
        const auto plugins = std::filesystem::absolute(argv[1]);
        const auto model = std::filesystem::absolute(argv[2]);
        const auto logs = std::filesystem::absolute(argv[3]);
        Require(std::filesystem::is_directory(logs), "logs directory must exist");
        Require(std::filesystem::is_regular_file(model / L"nvngx_dlssnr.dll"), "model missing");
        for (const wchar_t* name : {L"sl.interposer.dll", L"sl.common.dll", L"sl.dlss_nr.dll"})
            Verify(plugins / name);
        Require(SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS), "DLL search policy");
        const auto library = LoadLibraryExW((plugins / L"sl.interposer.dll").c_str(), nullptr,
            LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        Require(library != nullptr, "load interposer");
        auto init = Export<PFun_slInit>(library, "slInit");
        auto shutdown = Export<PFun_slShutdown>(library, "slShutdown");
        auto requirements = Export<PFun_slGetFeatureRequirements>(library, "slGetFeatureRequirements");
        auto supported = Export<PFun_slIsFeatureSupported>(library, "slIsFeatureSupported");
        auto loaded = Export<PFun_slIsFeatureLoaded>(library, "slIsFeatureLoaded");
        auto version = Export<PFun_slGetFeatureVersion>(library, "slGetFeatureVersion");
        auto setDevice = Export<PFun_slSetD3DDevice>(library, "slSetD3DDevice");
        auto getFunction = Export<PFun_slGetFeatureFunction>(library, "slGetFeatureFunction");
        const wchar_t* search[] = {plugins.c_str(), model.c_str()};
        sl::Preferences pref;
        pref.featuresToLoad = &kNR;
        pref.numFeaturesToLoad = 1;
        pref.pathsToPlugins = search;
        pref.numPathsToPlugins = 2;
        pref.pathToLogsAndData = logs.c_str();
        pref.logLevel = sl::LogLevel::eVerbose;
        pref.logMessageCallback = Log;
        pref.engine = sl::EngineType::eCustom;
        pref.engineVersion = "DLSS5Tool-NR-Probe";
        pref.projectId = "7c134ab9-9677-4af5-a2b2-bca943350861";
        pref.renderAPI = sl::RenderAPI::eD3D12;
        pref.flags = sl::PreferenceFlags::eDisableCLStateTracking |
            sl::PreferenceFlags::eUseManualHooking | sl::PreferenceFlags::eUseFrameBasedResourceTagging;
        std::printf("CONFIG sdk=%d.%d.%d flags=%llu model=%ls\n",
            SL_VERSION_MAJOR, SL_VERSION_MINOR, SL_VERSION_PATCH,
            static_cast<unsigned long long>(pref.flags), model.c_str());
        auto r = init(pref, sl::kSDKVersion);
        Result("slInit", r);
        Modules("after-init");
        if (r != sl::Result::eOk) return 2;
        bool isLoaded = false;
        Result("loaded-before", loaded(kNR, isLoaded));
        std::printf("LOADED before=%d\n", isLoaded);
        sl::FeatureRequirements req;
        r = requirements(kNR, req);
        Result("requirements", r);
        if (r == sl::Result::eOk) {
            std::printf("REQUIREMENTS flags=%u viewports=%u driver=%s required-driver=%s tag-count=%u\n",
                static_cast<unsigned>(req.flags), req.maxNumViewports,
                req.driverVersionDetected.toStr().c_str(), req.driverVersionRequired.toStr().c_str(), req.numRequiredTags);
            for (unsigned i = 0; req.requiredTags && i < req.numRequiredTags && i < 128; ++i)
                std::printf("REQUIREMENTS tag[%u]=%u\n", i, req.requiredTags[i]);
        }
        IDXGIFactory1* factory = nullptr;
        Require(SUCCEEDED(CreateDXGIFactory1(IID_PPV_ARGS(&factory))), "DXGI factory");
        IDXGIAdapter1* selected = nullptr;
        DXGI_ADAPTER_DESC1 desc{};
        for (UINT i = 0; ; ++i) {
            IDXGIAdapter1* adapter = nullptr;
            if (factory->EnumAdapters1(i, &adapter) == DXGI_ERROR_NOT_FOUND) break;
            Require(adapter != nullptr, "enumerate adapter");
            adapter->GetDesc1(&desc);
            if (desc.VendorId == 0x10de && !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) {
                selected = adapter; break;
            }
            adapter->Release();
        }
        Require(selected != nullptr, "NVIDIA adapter missing");
        std::printf("ADAPTER %ls vendor=%x device=%x\n", desc.Description, desc.VendorId, desc.DeviceId);
        sl::AdapterInfo info;
        info.deviceLUID = reinterpret_cast<uint8_t*>(&desc.AdapterLuid);
        info.deviceLUIDSizeInBytes = sizeof(desc.AdapterLuid);
        Result("supported-before", supported(kNR, info));
        ID3D12Device* device = nullptr;
        Require(SUCCEEDED(D3D12CreateDevice(selected, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device))), "D3D12 device");
        r = setDevice(device);
        Result("set-device", r);
        Modules("after-device");
        Result("supported-after", supported(kNR, info));
        isLoaded = false;
        Result("loaded-after", loaded(kNR, isLoaded));
        std::printf("LOADED after=%d\n", isLoaded);
        sl::FeatureVersion ver;
        Result("feature-version", version(kNR, ver));
        std::printf("VERSION sl=%s ngx=%s\n", ver.versionSL.toStr().c_str(), ver.versionNGX.toStr().c_str());
        void* function = nullptr;
        r = getFunction(kNR, "slDLSSNRSetOptions", function);
        Result("find-options", r);
        if (r == sl::Result::eOk && function) {
            HMODULE owner = nullptr;
            GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                reinterpret_cast<LPCWSTR>(function), &owner);
            wchar_t filename[32768]{};
            GetModuleFileNameW(owner, filename, 32768);
            std::printf("OPTIONS_OWNER %ls\n", filename);
        }
        // Deliberately do not call options, allocate NR resources or evaluate.
        std::puts("NO_INFERENCE no NR options or evaluation called");
        std::puts("SHUTDOWN begin");
        Result("shutdown", shutdown());
        device->Release(); selected->Release(); factory->Release();
        std::puts("DONE");
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "FAIL %s win32=%lu\n", error.what(), GetLastError());
        return 1;
    }
}
