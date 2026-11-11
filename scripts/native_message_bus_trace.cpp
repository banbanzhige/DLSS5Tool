// Bounded observational IAT tracing in an explicitly owned diagnostic process.
// No remote injection, code patching, payload capture or return-value changes.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <psapi.h>
#include <intrin.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cwchar>

namespace {
constexpr unsigned Capacity = 128, Depth = 16, PathLength = 512, PatchCapacity = 256;
enum Kind { Thread = 1, PipeWait, PipeOpen, Read, Write, Pause, Wait,
            ProcessOpen, TokenOpen, TokenInfo, ServerPid, Close };
struct Entry {
    unsigned kind; DWORD error; uint64_t value, result, calls, total_ticks, max_ticks;
    void* caller; void* frames[Depth]; USHORT depth; wchar_t path[PathLength];
};
struct Patch { void** slot; void* before; void* after; unsigned kind; HMODULE module; };
Entry entries[Capacity]{};
Patch patches[PatchCapacity]{};
unsigned count = 0, patch_count = 0;
uint64_t dropped = 0;
SRWLOCK lock = SRWLOCK_INIT;
volatile LONG enabled = 0;
thread_local bool inside = false;
LARGE_INTEGER frequency{};
HMODULE own_module = nullptr;
decltype(&CreateThread) real_thread = nullptr;
decltype(&WaitNamedPipeW) real_pipe_wait = nullptr;
decltype(&CreateFileW) real_open = nullptr;
decltype(&ReadFile) real_read = nullptr;
decltype(&WriteFile) real_write = nullptr;
decltype(&Sleep) real_sleep = nullptr;
decltype(&WaitForSingleObject) real_wait = nullptr;
decltype(&OpenProcess) real_process_open = nullptr;
decltype(&OpenProcessToken) real_token_open = nullptr;
decltype(&GetTokenInformation) real_token_info = nullptr;
decltype(&GetNamedPipeServerProcessId) real_server_pid = nullptr;
decltype(&CloseHandle) real_close = nullptr;
void* volatile last_pipe = nullptr;

uint64_t ticks() { LARGE_INTEGER value{}; QueryPerformanceCounter(&value); return value.QuadPart; }
uint64_t begin_call() {
    const auto error = GetLastError(); const auto begin = ticks(); SetLastError(error); return begin;
}
bool pipe_path(const wchar_t* path) { return path && _wcsnicmp(path, L"\\\\.\\pipe\\", 9) == 0; }

__declspec(noinline) void record(unsigned kind, void* caller, uint64_t value, uint64_t result,
                                DWORD error, uint64_t elapsed, const wchar_t* path = nullptr) {
    if (inside || !InterlockedCompareExchange(&enabled, 0, 0)) return;
    inside = true;
    AcquireSRWLockExclusive(&lock);
    unsigned index = 0;
    for (; index < count; ++index) {
        auto& row = entries[index];
        if (row.kind == kind && row.caller == caller && row.value == value && row.result == result &&
            row.error == error && ((!path && !row.path[0]) ||
                (path && std::wcsncmp(path, row.path, PathLength - 1) == 0))) break;
    }
    if (index == count) {
        if (count == Capacity) { ++dropped; ReleaseSRWLockExclusive(&lock); inside = false; return; }
        auto& row = entries[count++];
        row.kind = kind; row.caller = caller; row.value = value; row.result = result; row.error = error;
        if (path) wcsncpy_s(row.path, path, _TRUNCATE);
        row.depth = CaptureStackBackTrace(2, Depth, row.frames, nullptr);
    }
    auto& row = entries[index];
    ++row.calls; row.total_ticks += elapsed;
    if (elapsed > row.max_ticks) row.max_ticks = elapsed;
    ReleaseSRWLockExclusive(&lock);
    inside = false;
}

HANDLE WINAPI observe_thread(LPSECURITY_ATTRIBUTES security, SIZE_T stack_size,
        LPTHREAD_START_ROUTINE start, LPVOID argument, DWORD flags, LPDWORD id) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const auto result = real_thread(security, stack_size, start, argument, flags, id);
    const auto error = GetLastError();
    record(Thread, caller, reinterpret_cast<uintptr_t>(start), result ? 1 : 0,
           result ? 0 : error, ticks() - begin);
    SetLastError(error); return result;
}
BOOL WINAPI observe_pipe_wait(LPCWSTR name, DWORD timeout) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const auto result = real_pipe_wait(name, timeout); const auto error = GetLastError();
    record(PipeWait, caller, timeout, result, result ? 0 : error, ticks() - begin, name);
    SetLastError(error); return result;
}
HANDLE WINAPI observe_open(LPCWSTR name, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES security,
                          DWORD disposition, DWORD attributes, HANDLE template_file) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const auto result = real_open(name, access, share, security, disposition, attributes, template_file);
    const auto error = GetLastError();
    if (pipe_path(name) && result != INVALID_HANDLE_VALUE)
        InterlockedExchangePointer(&last_pipe, result);
    if (pipe_path(name)) record(PipeOpen, caller, access, result != INVALID_HANDLE_VALUE,
                              result != INVALID_HANDLE_VALUE ? 0 : error, ticks() - begin, name);
    SetLastError(error); return result;
}
BOOL WINAPI observe_read(HANDLE file, LPVOID data, DWORD size, LPDWORD read, LPOVERLAPPED overlapped) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const auto result = real_read(file, data, size, read, overlapped); const auto error = GetLastError();
    record(Read, caller, size, result, result ? 0 : error, ticks() - begin);
    SetLastError(error); return result;
}
BOOL WINAPI observe_write(HANDLE file, LPCVOID data, DWORD size, LPDWORD written, LPOVERLAPPED overlapped) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const auto result = real_write(file, data, size, written, overlapped); const auto error = GetLastError();
    record(Write, caller, size, result, result ? 0 : error, ticks() - begin);
    SetLastError(error); return result;
}
void WINAPI observe_sleep(DWORD milliseconds) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    real_sleep(milliseconds); const auto error = GetLastError();
    record(Pause, caller, milliseconds, 0, 0, ticks() - begin);
    SetLastError(error);
}
DWORD WINAPI observe_wait(HANDLE object, DWORD timeout) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const auto result = real_wait(object, timeout); const auto error = GetLastError();
    record(Wait, caller, timeout, result, result == WAIT_FAILED ? error : 0, ticks() - begin);
    SetLastError(error); return result;
}
HANDLE WINAPI observe_process_open(DWORD access, BOOL inherit, DWORD pid) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const auto result = real_process_open(access, inherit, pid); const auto error = GetLastError();
    record(ProcessOpen, caller, (uint64_t(pid) << 32) | access, result ? 1 : 0,
           result ? 0 : error, ticks() - begin);
    SetLastError(error); return result;
}
BOOL WINAPI observe_token_open(HANDLE process, DWORD access, PHANDLE token) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const auto result = real_token_open(process, access, token); const auto error = GetLastError();
    record(TokenOpen, caller, access, result, result ? 0 : error, ticks() - begin);
    SetLastError(error); return result;
}
BOOL WINAPI observe_token_info(HANDLE token, TOKEN_INFORMATION_CLASS kind, LPVOID data, DWORD size, PDWORD required) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const auto result = real_token_info(token, kind, data, size, required); const auto error = GetLastError();
    record(TokenInfo, caller, kind, result, result ? 0 : error, ticks() - begin);
    SetLastError(error); return result;
}
BOOL WINAPI observe_server_pid(HANDLE pipe, PULONG pid) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const auto result = real_server_pid(pipe, pid); const auto error = GetLastError();
    record(ServerPid, caller, result && pid ? *pid : 0, result, result ? 0 : error, ticks() - begin);
    SetLastError(error); return result;
}
BOOL WINAPI observe_close(HANDLE handle) {
    auto caller = _ReturnAddress(); const auto begin = begin_call();
    const bool is_pipe = handle && InterlockedCompareExchangePointer(&last_pipe, nullptr, handle) == handle;
    const auto result = real_close(handle); const auto error = GetLastError();
    record(Close, caller, is_pipe ? 1 : 0, result, result ? 0 : error, ticks() - begin);
    SetLastError(error); return result;
}
struct Api { const char* name; void* original; void* wrapper; unsigned kind; };

void patch_module(HMODULE module, const Api* apis, unsigned api_count) {
    if (!module || module == own_module) return;
    auto base = reinterpret_cast<unsigned char*>(module);
    auto dos = reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return;
    auto nt = reinterpret_cast<IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return;
    auto directory = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!directory.VirtualAddress || directory.VirtualAddress + directory.Size > nt->OptionalHeader.SizeOfImage) return;
    auto descriptor = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR*>(base + directory.VirtualAddress);
    for (; descriptor->Name && descriptor->FirstThunk; ++descriptor) {
        if (!descriptor->OriginalFirstThunk) continue;
        auto names = reinterpret_cast<IMAGE_THUNK_DATA64*>(base + descriptor->OriginalFirstThunk);
        auto targets = reinterpret_cast<IMAGE_THUNK_DATA64*>(base + descriptor->FirstThunk);
        for (; names->u1.AddressOfData; ++names, ++targets) {
            if (IMAGE_SNAP_BY_ORDINAL64(names->u1.Ordinal)) continue;
            auto imported = reinterpret_cast<IMAGE_IMPORT_BY_NAME*>(base + names->u1.AddressOfData);
            for (unsigned i = 0; i < api_count; ++i) {
                const auto& api = apis[i];
                auto slot = reinterpret_cast<void**>(&targets->u1.Function);
                // Never bypass an existing third-party interceptor.
                if (std::strcmp(reinterpret_cast<char*>(imported->Name), api.name) ||
                    *slot != api.original || patch_count == PatchCapacity) continue;
                DWORD old = 0;
                if (!VirtualProtect(slot, sizeof(void*), PAGE_READWRITE, &old)) continue;
                auto previous = InterlockedCompareExchangePointer(slot, api.wrapper, api.original);
                DWORD ignored; VirtualProtect(slot, sizeof(void*), old, &ignored);
                if (previous == api.original) patches[patch_count++] = {slot, api.original, api.wrapper, api.kind, module};
            }
        }
    }
}
bool write_line(HANDLE file, const char* text) {
    DWORD written = 0; const auto size = static_cast<DWORD>(std::strlen(text));
    return WriteFile(file, text, size, &written, nullptr) && written == size;
}
void escape(const wchar_t* source, char* destination, unsigned capacity) {
    char converted[4096]{};
    WideCharToMultiByte(CP_UTF8, 0, source, -1, converted, sizeof(converted), nullptr, nullptr);
    unsigned offset = 0;
    for (const unsigned char* p = reinterpret_cast<unsigned char*>(converted); *p && offset + 8 < capacity; ++p) {
        if (*p == '"' || *p == '\\') destination[offset++] = '\\';
        if (*p < 32) offset += std::snprintf(destination + offset, capacity - offset, "\\u%04x", *p);
        else destination[offset++] = static_cast<char>(*p);
    }
    destination[offset] = 0;
}
}

extern "C" __declspec(dllexport) int message_trace_begin() {
    if (patch_count) return -1;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                      reinterpret_cast<LPCWSTR>(&message_trace_begin), &own_module);
    QueryPerformanceFrequency(&frequency);
    real_thread = &CreateThread; real_pipe_wait = &WaitNamedPipeW; real_open = &CreateFileW;
    real_read = &ReadFile; real_write = &WriteFile; real_sleep = &Sleep; real_wait = &WaitForSingleObject;
    real_process_open = &OpenProcess; real_token_open = &OpenProcessToken; real_token_info = &GetTokenInformation;
    real_server_pid = &GetNamedPipeServerProcessId; real_close = &CloseHandle;
    const Api apis[] = {
        {"CreateThread", reinterpret_cast<void*>(real_thread), reinterpret_cast<void*>(&observe_thread), Thread},
        {"WaitNamedPipeW", reinterpret_cast<void*>(real_pipe_wait), reinterpret_cast<void*>(&observe_pipe_wait), PipeWait},
        {"CreateFileW", reinterpret_cast<void*>(real_open), reinterpret_cast<void*>(&observe_open), PipeOpen},
        {"ReadFile", reinterpret_cast<void*>(real_read), reinterpret_cast<void*>(&observe_read), Read},
        {"WriteFile", reinterpret_cast<void*>(real_write), reinterpret_cast<void*>(&observe_write), Write},
        {"Sleep", reinterpret_cast<void*>(real_sleep), reinterpret_cast<void*>(&observe_sleep), Pause},
        {"WaitForSingleObject", reinterpret_cast<void*>(real_wait), reinterpret_cast<void*>(&observe_wait), Wait},
        {"OpenProcess", reinterpret_cast<void*>(real_process_open), reinterpret_cast<void*>(&observe_process_open), ProcessOpen},
        {"OpenProcessToken", reinterpret_cast<void*>(real_token_open), reinterpret_cast<void*>(&observe_token_open), TokenOpen},
        {"GetTokenInformation", reinterpret_cast<void*>(real_token_info), reinterpret_cast<void*>(&observe_token_info), TokenInfo},
        {"GetNamedPipeServerProcessId", reinterpret_cast<void*>(real_server_pid), reinterpret_cast<void*>(&observe_server_pid), ServerPid},
        {"CloseHandle", reinterpret_cast<void*>(real_close), reinterpret_cast<void*>(&observe_close), Close}
    };
    const wchar_t* names[] = {L"NvMessageBus.dll", L"nvdiagclt64.dll", L"nvcuda64.dll", L"message_trace_fixture.dll"};
    inside = true;
    for (auto name : names) patch_module(GetModuleHandleW(name), apis, static_cast<unsigned>(sizeof(apis) / sizeof(apis[0])));
    InterlockedExchange(&enabled, 1); inside = false;
    return static_cast<int>(patch_count);
}

extern "C" __declspec(dllexport) int message_trace_dump(const wchar_t* path) {
    if (!patch_count) return -1;
    inside = true;
    auto file = CreateFileW(path, GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) { inside = false; return -2; }
    HMODULE modules[1024]{}; DWORD needed = 0;
    bool ok = EnumProcessModules(GetCurrentProcess(), modules, sizeof(modules), &needed) && needed <= sizeof(modules);
    char text[70000]{};
    if (ok) for (unsigned i = 0; i < needed / sizeof(HMODULE); ++i) {
        wchar_t name[32768]{}; char escaped[65536]{}; MODULEINFO info{};
        if (!GetModuleFileNameW(modules[i], name, sizeof(name) / sizeof(name[0])) ||
            !GetModuleInformation(GetCurrentProcess(), modules[i], &info, sizeof(info))) { ok = false; break; }
        escape(name, escaped, sizeof(escaped));
        std::snprintf(text, sizeof(text), "{\"kind\":\"module\",\"base\":\"0x%llx\",\"size\":%lu,\"path\":\"%s\"}\n",
                      reinterpret_cast<uintptr_t>(info.lpBaseOfDll), info.SizeOfImage, escaped);
        ok = write_line(file, text) && ok;
    }
    AcquireSRWLockExclusive(&lock);
    std::snprintf(text, sizeof(text), "{\"kind\":\"summary\",\"pid\":%lu,\"patches\":%u,\"entries\":%u,\"dropped\":%llu}\n",
                  GetCurrentProcessId(), patch_count, count, dropped);
    ok = write_line(file, text) && ok;
    for (unsigned i = 0; i < patch_count; ++i) {
        auto& patch = patches[i];
        std::snprintf(text, sizeof(text), "{\"kind\":\"patch\",\"api\":%u,\"module\":\"0x%llx\",\"slot\":\"0x%llx\"}\n",
                      patch.kind, reinterpret_cast<uintptr_t>(patch.module), reinterpret_cast<uintptr_t>(patch.slot));
        ok = write_line(file, text) && ok;
    }
    for (unsigned i = 0; i < count; ++i) {
        auto& row = entries[i]; char escaped[8192]{}; escape(row.path, escaped, sizeof(escaped));
        auto length = std::snprintf(text, sizeof(text), "{\"kind\":\"api\",\"api\":%u,\"caller\":\"0x%llx\","
            "\"value\":%llu,\"result\":%llu,\"error\":%lu,\"calls\":%llu,\"total_us\":%.3f,\"max_us\":%.3f,"
            "\"path\":\"%s\",\"frames\":[", row.kind, reinterpret_cast<uintptr_t>(row.caller), row.value, row.result,
            row.error, row.calls, row.total_ticks * 1e6 / frequency.QuadPart, row.max_ticks * 1e6 / frequency.QuadPart, escaped);
        for (unsigned f = 0; f < row.depth; ++f)
            length += std::snprintf(text + length, sizeof(text) - length, "%s\"0x%llx\"", f ? "," : "", reinterpret_cast<uintptr_t>(row.frames[f]));
        std::snprintf(text + length, sizeof(text) - length, "]}\n");
        ok = write_line(file, text) && ok;
    }
    ReleaseSRWLockExclusive(&lock);
    CloseHandle(file); inside = false;
    return ok ? 0 : -3;
}

extern "C" __declspec(dllexport) int message_trace_end() {
    InterlockedExchange(&enabled, 0); inside = true;
    unsigned restored = 0;
    for (unsigned i = patch_count; i > 0; --i) {
        auto& patch = patches[i - 1]; DWORD old = 0;
        if (!VirtualProtect(patch.slot, sizeof(void*), PAGE_READWRITE, &old)) continue;
        if (InterlockedCompareExchangePointer(patch.slot, patch.before, patch.after) == patch.after) ++restored;
        DWORD ignored; VirtualProtect(patch.slot, sizeof(void*), old, &ignored);
    }
    inside = false;
    return static_cast<int>(restored);
}
