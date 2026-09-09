# Development runtimes

Place authorized native runtime files here for source-mode execution:

- `dlssnr_host_v2.dll` (built by `native/host_v2/build.bat`)
- `nvngx_dlssnr.dll` (user-supplied)
- `vsr_host.dll` and `nvngx_vsr.dll` (built/staged by `native/vsr_host/build.bat`)
- `dlssnr_host.dll` (optional legacy host)

DLLs are ignored by Git. Compiler intermediates belong in `build/native/`.
Portable releases still load bundled runtimes from `_internal/`; user overrides
and enhancement components remain in `mods/`, not in this source-only directory.
