<h1 align="center">
  <img src="assets/readme-banner-v1.png" alt="DLSS5Tool — local neural rendering, 2× / 4× super resolution, real-time comparison, and batch export" width="1200">
</h1>

<p align="center">
  Enhance videos and images locally with real-time comparison and batch export.
</p>

<p align="center">
  <a href="README.md">简体中文</a> ·
  <strong>English</strong> ·
  <a href="https://github.com/banbanzhige/DLSS5Tool/releases/latest">Download portable release</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v2.2.0-0E7490?style=flat&amp;labelColor=475569" alt="Current documentation version v2.2.0" height="20"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/platform-Windows_x64-0369A1?style=flat&amp;labelColor=475569" alt="Platform: Windows x64" height="20"></a>
  <a href="#2-select-the-runtime-for-your-gpu"><img src="https://img.shields.io/badge/GPU-NVIDIA_RTX-0E7490?style=flat&amp;labelColor=475569" alt="GPU: NVIDIA RTX; select the runtime for your GPU generation" height="20"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-0369A1?style=flat&amp;labelColor=475569" alt="Project source is licensed under MIT" height="20"></a>
</p>

<details>
<summary>Contents</summary>

- [Screenshot and comparison](#screenshot-and-comparison)
- [Features](#features)
- [Quick start](#quick-start)
- [Controls and shortcuts](#controls-and-shortcuts)
- [Output and quality](#output-and-quality)
- [FAQ](#faq)
- [Run from source](#run-from-source)
- [License](#license)

</details>

## Screenshot and comparison

<p align="center">
  <a href="img/3.png"><img src="img/3.png" alt="DLSS5Tool showing the original/DLSS split comparison, processing controls, and preview cache status" width="760"></a>
</p>

<p align="center"><sub>Application screenshot from v2.0.0</sub></p>

The images below use the same AI-generated source. The left side is untouched and the right side is the neural-rendered result. This illustrates changes to materials, lighting, and detail; results vary by source. Click an image to view it at full size.

<table>
  <tr>
    <th width="50%">Original</th>
    <th width="50%">After neural rendering</th>
  </tr>
  <tr>
    <td align="center"><a href="img/01.png"><img src="img/01.png" alt="Original AI-generated image" width="100%"></a></td>
    <td align="center"><a href="img/02.png"><img src="img/02.png" alt="Image processed by DLSS5Tool" width="100%"></a></td>
  </tr>
</table>

## Features

DLSS5Tool uses **DLSS 5 Neural Rendering** to enhance existing images. It can optionally apply 2× or 4× RTX Video super resolution first. Inference runs locally and does not require a game engine, material data, normals, depth data, PyTorch, or an online service. The portable package includes its Python dependencies and FFmpeg.

> This is a neural post-processing tool for existing media. It is not in-game DLSS super resolution or frame generation. Quality and performance depend on the source, settings, GPU, driver, and runtime. RTX 30- and 50-series users must select the matching runtime described below.

- **Image enhancement:** Default, Natural, and Cinema styles with strength, local tone, local structure, output mix, and skin-mask controls.
- **Optional super resolution:** 2× / 4× RTX Video scaling followed by DLSS 5 enhancement; disabled means same-resolution enhancement.
- **Interactive preview:** Original, DLSS, and draggable split comparison; 25%–800% zoom, panning, navigator, frame stepping, fullscreen, and a detachable preview window.
- **HDR video:** Detects HDR10/PQ and HLG, uses a high-precision path, exports HEVC Main10, and preserves basic color tags.
- **Flexible export:** MP4, MKV, or MOV video; output-resolution limits; quality profiles or a custom bitrate; source audio passthrough when compatible.
- **Mixed batch queue:** Images and videos can be mixed, reordered, retried, and restored after restart. Each job keeps its own settings snapshot.
- **Desktop workspace:** Light and dark themes, resizable inspector, log, diagnostics, and update checks. Inference runs in an isolated process.
- **Languages:** Simplified Chinese and English. Change the language under **More → Language**; restart DLSS5Tool to apply it.

Supported video extensions: `MP4 / M4V / MOV / MKV / AVI / WebM`.
Supported image extensions: `PNG / JPG / JPEG / WebP / BMP / TIF / TIFF`.
Actual decoding support still depends on the file contents and available codecs.

## Quick start

### 1. Download and fully extract the package

Open the [latest release](https://github.com/banbanzhige/DLSS5Tool/releases/latest) and download `DLSS5Tool-vVERSION-win64.zip`. Do not download GitHub's automatically generated “Source code” archives.

Requirements:

- Windows 10 or 11 x64.
- A compatible NVIDIA RTX GPU and driver.
- The runtime matching your GPU generation.
- [Microsoft Visual C++ Redistributable x64](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist).

Extract the complete archive into a writable folder. The portable package does not require a separate Python or FFmpeg installation. Keep `DLSS5Tool.exe` beside its `_internal` directory; do not copy the EXE by itself or launch it from inside the ZIP.

### 2. Select the runtime for your GPU

| GPU | Runtime |
| --- | --- |
| RTX 40 series | Use the default runtime included in the main portable package. |
| RTX 30 series | Download `30系.zip` from the same release and replace the DLL below. |
| RTX 50 series | Download `50系.zip` from the same release and replace the DLL below. |

Close the application, then place the matching runtime here for RTX 30- or 50-series GPUs:

```text
mods\nvngx_dlssnr.dll
```

Auto-detection prefers an existing custom path, then nvngx_dlssnr.dll in modules, then a unique recognized alternative, then bundled `_internal`. Multiple candidates are not guessed; select one manually or force bundled. No need to overwrite `_internal`. The modules directory defaults to mods beside the executable and can be changed; detected paths are shown. The RTX 30-series runtime is a community adaptation and is not an NVIDIA support commitment. GPU and driver combinations still require real-hardware verification.

Depth/flow settings have a dedicated **Models** tab alongside Effects, Settings and Queue; Effects no longer duplicates guidance settings. Guidance is **off by default; ordinary use needs no PyTorch or models**. Extract the add-on beside the app: the archive includes `mods`, inference dependencies and architecture. No path setup or Python installation; depth size is detected automatically. Compatible `.pth` weights remain external and replaceable. Settings → Models & add-ons shows status and common actions; Replace DLL / models is a top-level section, collapsed by default without nested disclosure. FP16 and dual stream are under Settings → Performance & device. Inactive controls keep their values. See [mods instructions](mods/README.md). No models download and no installers run automatically. Guidance supports SDR, non-tiled processing; CPU inference may be slow.

The Models player offers Original, Depth, Flow and Compare, with comparisons against the original. Both DLSS and guidance comparisons support a draggable wipe or synchronized side-by-side display. Guidance images reuse the current component on demand and never change export content. Flow hue indicates direction and brightness indicates magnitude; depth is normalized relative depth, not metric distance.

Use the bottom **Compare ▾** menu for layout, comparison target, centering and the on-demand legend. Guidance playback holds the complete current image pair until the next pair is ready, updating the frame number and both images together.

The fixed Models export area saves a complete MP4 video or the current PNG frame at source dimensions and frame rate, without audio or UI overlays. PNG contains an 8-bit visualization, not raw floating-point data. Export supports cancellation and preserves existing destination files on failure or cancellation.

Analysis controls include RAFT iterations, independent flow/depth sizes, depth range stability and percentiles. Map display options affect map previews/exports only, not enhanced video. See [parameter notes](GUIDANCE_PARAMETERS.md).

<details>
<summary>Recorded runtime versions and SHA-256 values</summary>

- **RTX 30 series:** `310.8.SF-v2` (file version `310.8.SF.0`)
  - SHA-256: `6EB209E764F39872625DEBD6ABAF45E2BB6322F6F270F781F70C059AE30B3927`
- **Default RTX 40-series DLL:**
  - SHA-256: `CEB6432F6FBDF44D886014BCD47241932BF8B67439FEEF9BBDD0961436662650`
- **RTX 50 series:** NVIDIA-signed `310.8.0.0`
  - SHA-256: `E16BCF15E16E13F527491CDF7845B2FE6521A738D8F7C9C721866A8496E1FC8E`

Verify the extracted runtime from the application directory:

```powershell
Get-FileHash .\_internal\nvngx_dlssnr.dll -Algorithm SHA256
```

A matching hash identifies the file only; it is not a licensing or compatibility guarantee.

</details>

### 3. Import, compare, and export

1. Start `DLSS5Tool.exe`, then drop media onto the left side or click **Choose a file**.
2. Switch to **DLSS** or **Compare** and adjust the effect on the **Adjust** tab. The first launch opens the Original view; press `3` for split comparison.
3. Configure output on the **Export** tab. Safe first-run defaults are original resolution, super resolution off, Balanced quality, Strict single session, and MP4 for video.
4. Export the current item, or add multiple items to the queue and process them together.

Existing saved settings remain in effect and are not replaced by first-run defaults.

## Controls and shortcuts

| Action | Keyboard / mouse |
| --- | --- |
| Original / DLSS / split comparison | `1` / `2` / `3` |
| Temporarily show the original | Hold `Alt` |
| Move or reset the comparison divider | Drag it; double-click it to reset to 50% |
| Play / pause | `Space` |
| Previous / next frame | `←` / `→`, or use the wheel over the timeline |
| Seek backward / forward one second | `Shift` + `←` / `→` |
| First / last frame | `Home` / `End` |
| Zoom | Wheel over the canvas, or `+` / `-` |
| Fit to window | `0` |
| Pan | Drag a zoomed image or use the navigator |
| Enter / exit fullscreen | `F11` or double-click away from the divider; `Esc` exits |

Use **Detach** on the transport bar to move the preview and playback controls into a separate window. Closing that window or choosing **Dock** returns it to the main window. Frame position, playback state, and comparison position stay synchronized.

Each queued job stores the settings active when it was added. Later adjustments do not automatically modify existing jobs; select jobs and choose **Apply settings** when needed. Retry, clear-completed, move, pause, cancel, and resume operations are supported. This is not frame-level resumable export within a video.

## Output and quality

### Same-resolution enhancement and super resolution

- **Super resolution off:** neural rendering runs at the source size. A video output-resolution limit downscales after processing and does not reduce DLSS input dimensions.
- **2× / 4× super resolution:** RTX Video scales first, then DLSS 5 runs at the target resolution. Width and height both scale, so pixel count becomes 4× or 16× and memory requirements increase accordingly.
- During super-resolution playback, the application uses a lower-cost proxy. Pausing, stepping, or releasing the scrubber generates an exact target-resolution preview.
- Very large still images can be tiled automatically while preserving target dimensions. Success is still limited by VRAM, texture-size limits, and the runtime.

### Formats, encoding, and temporal behavior

- **Images:** keep the source format by default. PNG and TIFF are written losslessly; JPEG and similar formats are re-encoded. Lossless file encoding does not mean the enhanced pixels equal the source.
- **Video:** MP4 by default, with MKV, MOV, and Match input options. Match input supports MP4/M4V, MKV, and MOV; AVI and WebM safely fall back to MP4.
- **Quality:** SDR defaults to H.264 and automatically uses HEVC when either final output dimension exceeds 4096, with quality profiles or a custom bitrate. Maximum quality is still lossy compression, not mathematically lossless video.
- **Temporal behavior:** Strict sequence keeps one continuous processing history. Visually lossless parallel segments can accelerate SDR video, but segment boundaries may differ slightly. High-precision HDR and super-resolution jobs use strict single-session processing.
- **Audio:** compatible source audio is copied directly. Incompatible MP4/MOV audio falls back to AAC.

### HDR and experimental controls

- High-precision HDR processing applies only to correctly tagged PQ/HLG video; static HDR images are not currently supported.
- HDR export uses HEVC Main10, 10-bit 4:2:0, and preserves basic HDR10/HLG color tags. Dolby Vision, HDR10+ dynamic metadata, and source mastering-display/MaxCLL SEI are not copied.
- The UI tone-maps HDR previews to SDR and should not be used to judge final HDR brightness. Disabling high-precision processing tone-maps HDR video to SDR before export.
- The experimental 5× control range is disabled by default. Values up to 500% can cause clipping, artifacts, or over-processing, and some runtimes may clamp them internally.

## FAQ

**The application does not start or reports a missing DLL.**

Fully extract the archive, keep `_internal` beside the EXE, install the x64 Visual C++ runtime, and do not mix files from different DLSS5Tool releases.

**DLSS initialization fails, including after replacing the runtime.**

Check the GPU generation, DLL path, and hash. Then use **More → Diagnostics**; no media needs to be imported first. When reporting an issue, include the application version, GPU, driver, reproduction steps, and diagnostic `.log`. Review local paths and other private data before posting a log publicly.

**Preview is slow or high-multiplier super resolution fails.**

Lower Playback quality under Preview performance and adjust the cache budget to available memory (the first-run default is `8192 MiB`). Validate high-resolution or 4× jobs with smaller media or 2× first, and review the resource warning. Preview zoom does not change export dimensions.

**How is 8K output from 4× super resolution encoded?**

4× processing of 1080p produces 7680×4320. When either final output dimension exceeds 4096, export automatically uses HEVC/H.265 while keeping SDR content SDR. Smaller SDR output retains H.264; HDR uses HEVC Main10. A trial encode validates the actual size and settings before export, with CPU fallback if GPU encoding is unavailable (potentially much slower at 8K). The log identifies the selected encoder. MP4, MKV and MOV support HEVC, but playback also requires HEVC support. If export still fails, lower the final output size and include the encoder error log when reporting it.

**How do updates work?**

The portable build checks GitHub Releases quietly at startup. You can also choose **More → Check for updates**. A prompt appears only for a newer release and asks before downloading. The running application is never replaced automatically; close it and fully extract the new package into a new folder.

## Run from source

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
.\native_host_v2\build.bat

# 3. Place an authorized nvngx_dlssnr.dll in the project root and start the app
.\run.bat
```

For 2× / 4× super resolution, obtain RTX Video SDK 1.1 separately. Extract it to `third_party/RTX_Video_SDK` or point `NV_RTX_VIDEO_SDK` to its root, then run:

```powershell
.\native_vsr_host\build.bat
```

This builds `vsr_host.dll` and copies `nvngx_vsr.dll` from the SDK into the project root.

### Tests and packaging

Unit tests do not require a GPU, NVIDIA SDK, or proprietary DLL. Real GPU, HDR, and super-resolution output still requires hardware testing.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

Before building the portable package, provide `dlssnr_host_v2.dll`, `nvngx_dlssnr.dll`, `vsr_host.dll`, `nvngx_vsr.dll`, the application icon, and the original RTX Video SDK license file. The release script installs build dependencies, runs tests, and creates the portable directory and ZIP:

```powershell
.\build_release.ps1
```

The current release is written to `dist/DLSS5Tool-v2.2.0/` and `dist/DLSS5Tool-v2.2.0-win64.zip`.

The main portable package excludes AMD developer tools, experiment scripts/reports, test sources, and experiment outputs. These remain in the source repository; AMD testing has a separate build entry point. Runtime assets and user documents are collected explicitly, and a pre-archive check rejects development material. The enhancement component is also packaged separately; the base package includes only the instructions in `mods`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development conventions and [SECURITY.md](SECURITY.md) for security reports. The source repository excludes NVIDIA SDK files, runtime DLLs, user settings, private test media, and other generated artifacts.

## License

Project-owned source code is released under the [MIT License](LICENSE). `nvngx_dlssnr.dll`, NVIDIA SDKs, FFmpeg, and bundled Python dependencies remain subject to their respective upstream licenses and are not covered by this repository's MIT license. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
