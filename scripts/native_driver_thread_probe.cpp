// Diagnostic-only, bounded per-thread allocation reproduction in this process.
// The parent arms a kill-on-close job before sending the stdin startup gate.
// No CUDA/NGX, D3D calls, remote injection, DLL replacement or thread termination.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <psapi.h>
#include <cstdio>
#include <cstdlib>
#include <string>

namespace {
struct HeapSummaryInfo {
    DWORD cb;
    SIZE_T allocated, committed, reserved, maximum;
};
using HeapSummaryFn = BOOL (WINAPI*)(HANDLE, DWORD, HeapSummaryInfo*);
using TraceBegin = int (*)();
using TraceDump = int (*)(const wchar_t*);
using TraceEnd = int (*)();
DWORD WINAPI empty_thread(void*) { return 0; }

bool snapshot(unsigned count) {
    PROCESS_MEMORY_COUNTERS_EX memory{};
    memory.cb = sizeof(memory);
    HeapSummaryInfo heap{sizeof(HeapSummaryInfo)};
    const auto summary = reinterpret_cast<HeapSummaryFn>(
        GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "HeapSummary"));
    if (!GetProcessMemoryInfo(GetCurrentProcess(),
            reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&memory), sizeof(memory)) ||
        !summary || !summary(GetProcessHeap(), 0, &heap)) return false;
    std::printf("{\"kind\":\"sample\",\"pid\":%lu,\"threads_completed\":%u,"
                "\"private_bytes\":%llu,\"heap_allocated\":%llu,\"heap_committed\":%llu}\n",
                GetCurrentProcessId(), count, static_cast<unsigned long long>(memory.PrivateUsage),
                static_cast<unsigned long long>(heap.allocated),
                static_cast<unsigned long long>(heap.committed));
    std::fflush(stdout);
    return memory.PrivateUsage < 1024ull * 1024 * 1024;
}
}

int wmain(int argc, wchar_t** argv) {
    if (argc != 5) return 2;
    wchar_t* end = nullptr;
    const unsigned count = wcstoul(argv[2], &end, 10);
    if (!end || *end || count < 1000 || count > 100000) return 2;
    char gate = 0; DWORD received = 0;
    if (!ReadFile(GetStdHandle(STD_INPUT_HANDLE), &gate, 1, &received, nullptr) ||
        received != 1 || gate != 'G') return 3;
    // Use only a caller-verified absolute installed module, not DLL search paths.
    const auto target = LoadLibraryExW(argv[1], nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!target) {
        std::printf("{\"kind\":\"load_error\",\"error\":%lu}\n", GetLastError());
        return 4;
    }
    const bool tracing = std::wstring(argv[3]) != L"-";
    TraceDump dump = nullptr;
    TraceEnd finish = nullptr;
    int patches = 0;
    if (tracing) {
        const auto tracer = LoadLibraryExW(argv[3], nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        if (!tracer) return 5;
        const auto begin = reinterpret_cast<TraceBegin>(GetProcAddress(tracer, "trace_begin"));
        dump = reinterpret_cast<TraceDump>(GetProcAddress(tracer, "trace_dump"));
        finish = reinterpret_cast<TraceEnd>(GetProcAddress(tracer, "trace_end"));
        if (!begin || !dump || !finish) return 5;
        patches = begin();
        if (patches <= 0) return 6;
    }
    const std::wstring prefix(argv[4]);
    int result = 0;
    const auto began = GetTickCount64();
    if (!snapshot(0) || (dump && dump((prefix + L"-before.jsonl").c_str()))) result = 7;
    unsigned completed = 0;
    while (!result && completed < count) {
        if (GetTickCount64() - began > 90000) { result = 8; break; }
        HANDLE thread = CreateThread(nullptr, 0, empty_thread, nullptr, 0, nullptr);
        if (!thread) { result = 9; break; }
        const auto waited = WaitForSingleObject(thread, 10000);
        CloseHandle(thread);
        if (waited != WAIT_OBJECT_0) { result = 10; break; }
        ++completed;
        if (completed % 5000 == 0 && !snapshot(completed)) result = 11;
    }
    if (dump && dump((prefix + L"-after.jsonl").c_str()) && !result) result = 12;
    const int restored = finish ? finish() : 0;
    if (restored != patches && !result) result = 13;
    std::printf("{\"kind\":\"result\",\"completed\":%u,\"requested\":%u,"
                "\"trace_enabled\":%s,\"patches\":%d,\"restored\":%d,\"elapsed_ms\":%llu,\"error\":%d}\n",
                completed, count, tracing ? "true" : "false", patches, restored,
                GetTickCount64() - began, result);
    // Leave the pinned tracer and installed module to normal process teardown.
    return result;
}
