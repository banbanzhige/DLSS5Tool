#pragma once
// Research-only, single-threaded pre-Evaluate adapter for the exact local
// 4070 SUPER / DLSSG 310.7 binary. No on-disk change or generic DLL injection.
// Branch pattern and validation adapted from:
// dashdogy/RTX40MFG-Unlock, 33b41835dc39c5d8ab1ef93efb2449be31139c09,
// source/native/ngx_mfg_gate.h. License applies to those portions:
//
// MIT License
// Copyright (c) 2026 Michael Robles
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
// THE SOFTWARE.

#include <bcrypt.h>
#include <array>
#include <vector>
#pragma comment(lib, "bcrypt.lib")

namespace dlssg_ada_gate {
// Optional research preset override is applied before NGX initialization. It
// replaces exactly one known callsite, not the driver or global DRS settings.
inline bool ExactFile(HMODULE module) {
    wchar_t path[32768] = {};
    const DWORD size = GetModuleFileNameW(module, path, 32768);
    if (!size || size >= 32768) return false;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    BCRYPT_ALG_HANDLE algorithm = nullptr; BCRYPT_HASH_HANDLE hash = nullptr;
    std::array<unsigned char, 32> digest{};
    bool ok = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) == 0;
    if (ok) ok = BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) == 0;
    unsigned char buffer[65536]; DWORD bytes = 0;
    while (ok) {
        if (!ReadFile(file, buffer, sizeof(buffer), &bytes, nullptr)) {ok = false; break;}
        if (!bytes) break;
        ok = BCryptHashData(hash, buffer, bytes, 0) == 0;
    }
    if (ok) ok = BCryptFinishHash(hash, digest.data(), DWORD(digest.size()), 0) == 0;
    if (hash) BCryptDestroyHash(hash);
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    CloseHandle(file);
    constexpr unsigned char expected[] = {0x13,0x5e,0xaf,0x07,0x33,0xc1,0xe3,0x73,0x81,0xa8,0xc2,0x8a,0xbc,0xf7,0xa8,0x62,
        0x40,0x4a,0x54,0x13,0x2b,0x81,0x78,0x7c,0x04,0xe3,0x5d,0x09,0xef,0xc5,0xe3,0x6f};
    return ok && !memcmp(digest.data(), expected, sizeof(expected));
}
inline bool ExecutableImage(const void* address, size_t bytes, HMODULE module) {
    MEMORY_BASIC_INFORMATION info{};
    return VirtualQuery(address, &info, sizeof(info)) == sizeof(info)
        && info.AllocationBase == module && info.State == MEM_COMMIT && info.Type == MEM_IMAGE
        && info.Protect == PAGE_EXECUTE_READ
        && uintptr_t(address) >= uintptr_t(info.BaseAddress)
        && bytes <= info.RegionSize - (uintptr_t(address) - uintptr_t(info.BaseAddress));
}
inline bool PresetB(HMODULE module) {
    if (!module || !ExactFile(module)) return false;
    auto* base = reinterpret_cast<unsigned char*>(module);
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0 || dos->e_lfanew > 0x100000) return false;
    auto* nt = reinterpret_cast<IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC
        || nt->FileHeader.NumberOfSections > 96) return false;
    const size_t imageSize = nt->OptionalHeader.SizeOfImage;
    constexpr unsigned char prefix[] = {0xb9,0xf1,0x1d,0xe4,0x10,0xe8};
    constexpr unsigned char suffix[] = {0x84,0xc0,0x0f,0x84};
    constexpr unsigned char prologue[] = {0x48,0x89,0x5c,0x24,0x10,0x89,0x4c,0x24,0x08,0x57,0x48,0x83,0xec,0x20,0x48,0x8b,0xfa,0x8b,0xd9};
    unsigned char* match = nullptr;
    auto* section = IMAGE_FIRST_SECTION(nt);
    for (unsigned i=0; i<nt->FileHeader.NumberOfSections; ++i, ++section) {
        if (!(section->Characteristics & IMAGE_SCN_MEM_EXECUTE) || section->VirtualAddress >= imageSize) continue;
        const size_t bytes = std::min(imageSize-section->VirtualAddress, size_t(std::max(section->Misc.VirtualSize,section->SizeOfRawData)));
        auto* start = base + section->VirtualAddress;
        if (!ExecutableImage(start, bytes, module)) return false;
        for (size_t j=0; j+14<=bytes; ++j) {
            if (memcmp(start+j,prefix,sizeof(prefix)) || memcmp(start+j+10,suffix,sizeof(suffix))) continue;
            if (match) return false;
            match=start+j;
        }
    }
    if (!match) return false;
    int32_t rel; memcpy(&rel,match+6,4);
    const ptrdiff_t target=match-base+10+rel;
    if (target<0 || size_t(target)+sizeof(prologue)>imageSize
        || !ExecutableImage(base+target,sizeof(prologue),module)
        || memcmp(base+target,prologue,sizeof(prologue))) return false;
    // call bool(uint32_t id, uint32_t* output): RDX is the caller-owned output.
    constexpr unsigned char replacement[]={0xc7,0x02,0x02,0,0,0,0xb0,0x01,0x90,0x90};
    DWORD before=0;
    if (!VirtualProtect(match,sizeof(replacement),PAGE_EXECUTE_WRITECOPY,&before)) return false;
    memcpy(match,replacement,sizeof(replacement));
    DWORD ignored=0;
    return VirtualProtect(match,sizeof(replacement),before,&ignored)
        && FlushInstructionCache(GetCurrentProcess(),match,sizeof(replacement))
        && ExecutableImage(match,sizeof(replacement),module);
}
inline bool Apply(HMODULE module, unsigned vendor, unsigned device) {
    if (!module || vendor != 0x10de || device != 0x2783 || !ExactFile(module)) return false;
    if (!GetProcAddress(module, "NVSDK_NGX_D3D12_PopulateDeviceParameters_Impl")
        || GetProcAddress(module, "NVSDK_NGX_DirectSR_Create")) return false;
    auto* base = reinterpret_cast<unsigned char*>(module);
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0 || dos->e_lfanew > 0x100000) return false;
    auto* nt = reinterpret_cast<IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC
        || nt->FileHeader.NumberOfSections > 96) return false;
    const size_t imageSize = nt->OptionalHeader.SizeOfImage;
    constexpr unsigned char pattern[] = {0x84,0xd2,0x0f,0x84,0x03,0x01,0x00,0x00,0xbe,0x05,0x00,0x00,0x00};
    unsigned char* match = nullptr;
    auto* section = IMAGE_FIRST_SECTION(nt);
    for (unsigned i = 0; i < nt->FileHeader.NumberOfSections; ++i, ++section) {
        if (!(section->Characteristics & IMAGE_SCN_MEM_EXECUTE) || section->VirtualAddress >= imageSize) continue;
        const size_t bytes = std::min(imageSize - section->VirtualAddress,
            size_t(std::max(section->Misc.VirtualSize, section->SizeOfRawData)));
        auto* begin = base + section->VirtualAddress;
        if (!ExecutableImage(begin, bytes, module)) return false;
        for (size_t j = 0; j + sizeof(pattern) <= bytes; ++j) {
            if (memcmp(begin + j, pattern, sizeof(pattern))) continue;
            if (match) return false; // reject ambiguity
            match = begin + j;
        }
    }
    if (!match || (uintptr_t(match + 2) & 1)) return false;
    int32_t displacement; memcpy(&displacement, match + 4, 4);
    const ptrdiff_t target = match - base + 8 + displacement;
    constexpr unsigned char countOne[] = {0x41,0x83,0xf8,0x01};
    if (target < 0 || size_t(target) + sizeof(countOne) > imageSize
        || !ExecutableImage(base + target, sizeof(countOne), module)
        || memcmp(base + target, countOne, sizeof(countOne))) return false;
    DWORD64 imageBase = 0;
    auto* function = RtlLookupFunctionEntry(reinterpret_cast<DWORD64>(match), &imageBase, nullptr);
    if (!function || imageBase != reinterpret_cast<DWORD64>(module)
        || function->BeginAddress > size_t(match-base) || function->EndAddress <= size_t(target) + sizeof(countOne)
        || function->EndAddress > imageSize) return false;
    DWORD before = 0;
    if (!VirtualProtect(match + 2, 2, PAGE_EXECUTE_WRITECOPY, &before)) return false;
    const short original = short(0x840f), replacement = short(0x04eb);
    const bool changed = InterlockedCompareExchange16(reinterpret_cast<volatile short*>(match + 2), replacement, original) == original;
    DWORD ignored = 0;
    const bool restored = VirtualProtect(match + 2, 2, before, &ignored) != 0;
    const bool flushed = FlushInstructionCache(GetCurrentProcess(), match + 2, 2) != 0;
    return changed && restored && flushed && ExecutableImage(match, sizeof(pattern), module);
}
}
