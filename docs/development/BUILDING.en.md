# Developer guide

[Product overview](../../README.en.md) · [简体中文](BUILDING.md)

For normal use, prefer the portable release. Development requires:

- Windows 10/11 x64, Python 3.10+ with Tkinter and the `py` launcher, and Git.
- Visual Studio 2022 Build Tools with “Desktop development with C++” and the Windows SDK to compile native hosts.
- A compatible NVIDIA GPU, driver, and NVIDIA runtimes you are authorized to use for real neural-rendering execution.

From the project root:

```powershell
# 1. Create .venv and install Python dependencies
.\setup.bat

# 2. Obtain the NVIDIA DLSS SDK, review and accept its license, then build the host
git clone --depth 1 https://github.com/NVIDIA/DLSS.git third_party/NVIDIA-DLSS
.\native\host_v2\build.bat

# 3. Place an authorized nvngx_dlssnr.dll in runtime/ and start the app
.\run.bat
```

For 2× / 4× super resolution, obtain RTX Video SDK 1.1 separately. Extract it to `third_party/RTX_Video_SDK` or point `NV_RTX_VIDEO_SDK` to its root, then run:

```powershell
.\native\vsr_host\build.bat
```

This places `vsr_host.dll` and the SDK's `nvngx_vsr.dll` in `runtime/`, with compiler intermediates in `build/native/`.

Application sources live in `dlss5tool/`; development settings, queue and logs live in `var/`.
The development entry point is `run.bat` at the repo root (it launches `gui.py`).
See the [directory and documentation index](../../docs/README.md).

## Tests and packaging

Unit tests do not require a GPU, NVIDIA SDK, or proprietary DLL. Real GPU, HDR, and super-resolution output still requires hardware testing.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

Before building the portable package, provide `dlssnr_host_v2.dll`, `nvngx_dlssnr.dll`, `vsr_host.dll`, `nvngx_vsr.dll`, the application icon, and the original RTX Video SDK license file. The release script installs build dependencies, runs tests, and creates the portable directory and ZIP:

```powershell
.\scripts\build_release.ps1
```

The inference component is built separately; rebuilding the base app does not update it. See the [component build instructions](../../mods/README.md#maintainer-build-not-end-user-setup) and [packaging index (Chinese)](../../docs/release/PACKAGING_INDEX.md).

See [CONTRIBUTING.md](../../CONTRIBUTING.md) for development conventions and [SECURITY.md](../../SECURITY.md) for security reports. Performance experiments and validation records belong in the [technical documentation](../../docs/README.md), not the product README.
