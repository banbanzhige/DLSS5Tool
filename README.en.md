<h1 align="center">
  <img src="assets/readme-banner-v1.png" alt="DLSS5Tool — local neural rendering, 2× / 4× super resolution, real-time comparison, and batch export" width="1200">
</h1>

<p align="center">
  Reshape light and detail locally with DLSS 5, bringing a more lifelike look to videos and images.
</p>

<p align="center">
  <a href="README.md">简体中文</a> ·
  <strong>English</strong> ·
  <a href="https://github.com/banbanzhige/DLSS5Tool/releases/latest">Download portable release</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v2.3.2-0E7490?style=flat&amp;labelColor=475569" alt="Source version v2.3.2" height="20"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/platform-Windows_x64-0369A1?style=flat&amp;labelColor=475569" alt="Platform: Windows x64" height="20"></a>
  <a href="#2-select-the-runtime-for-your-gpu"><img src="https://img.shields.io/badge/GPU-NVIDIA_RTX-0E7490?style=flat&amp;labelColor=475569" alt="GPU: NVIDIA RTX; select the runtime for your GPU generation" height="20"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-0369A1?style=flat&amp;labelColor=475569" alt="Project source is licensed under MIT" height="20"></a>
</p>

## In-app preview

<p align="center">
  <a href="img/03.png"><img src="img/03.png" alt="DLSS5Tool showing the original/DLSS split comparison, processing controls, and preview cache status" width="760"></a>
</p>

<p align="center"><sub>Application screenshot from v2.1.1</sub></p>

## Features

DLSS5Tool uses **DLSS 5 Neural Rendering** to enhance local videos and images. No game engine integration is needed.

- **Image enhancement:** Default, Natural, and Cinema styles with strength, tone, structure, and skin-mask controls.
- **2× / 4× super resolution:** Upscale with RTX Video before enhancement, or process at the original size.
- **Frame interpolation:** Export at 2× / 3× / 4×. 3× / 4× are experimental and may show motion errors. Super resolution and interpolation each have a preview switch, off by default.
- **Interactive comparison:** Compare the true original with enhanced output using a draggable wipe, side-by-side views, zoom, frame stepping, fullscreen, or a detachable preview. Active upscale and interpolation multipliers are shown, and display interactions keep pre-caching continuous.
- **Batch export:** Mix images, videos, and image sequences with independent settings per item. Drag to select multiple items, use batch context menus and shortcuts, undo removal, and hover over long filenames for full paths. Export MP4 / MKV / MOV video and preserve compatible source audio.
- **Image sequences to video:** Import consecutively numbered, same-size images as one task, set the source frame rate, then enhance, upscale, or interpolate them into a video without audio. Supports SDR PNG/JPG; current source also supports PQ / HLG HDR PNG sequences that meet the input requirements.
- **HDR / GPU export:** High-precision HDR10 / HLG processing and 10-bit export. Eligible configurations use GPU color conversion and pass enhancement and interpolated frames directly from GPU memory to the encoder, reducing CPU transfers.
- **Export progress and diagnostics:** Track timestamp scanning before long-video exports. Failed or cancelled GPU exports preserve existing output files; Diagnostics show effective settings and recent export information.
- **Optical flow guidance:** Estimate inter-frame motion for temporal guidance, for more stable pictures and more accurate lighting.
- **DLSS render GPU:** Uses a high-performance NVIDIA GPU by default. If the display is on an iGPU, DLSS still runs on the NVIDIA GPU. Multi-GPU systems can pick a device in Settings.
- **File-level updates:** After installing a release that includes the update helper, **More → Check for updates** can download only changed files.

The interface supports Simplified Chinese / English and light / dark themes. Change the language under **More → Language**, then restart the app.

## Before and after

The images below use the same AI-generated source. The left side is untouched and the right side is the neural-rendered result. This illustrates changes to materials, lighting, and detail; results vary by source.
<table>
  <tr>
    <th width="50%">Image 1 · Original</th>
    <th width="50%">Image 2 · After neural rendering</th>
  </tr>
  <tr>
    <td align="center"><a href="img/01.png"><img src="img/01.png" alt="Original AI-generated image" width="100%"></a></td>
    <td align="center"><a href="img/02.png"><img src="img/02.png" alt="Image processed by DLSS5Tool" width="100%"></a></td>
  </tr>
</table>

## RAFT / NVOFA optical flow comparison

Enabling optical flow can improve overall video stability and lighting consistency, and reduce flicker, black patches, and flashing shadows. There are two backends: RAFT, which favors quality, and NVOFA, which favors speed.
Turn on the flow-map preview to see what the model inferred: **color encodes motion direction, brightness encodes displacement magnitude**.

<table>
  <tr>
    <th width="33%">07 · Original / DLSS wipe</th>
    <th width="33%">08 · RAFT flow</th>
    <th width="33%">09 · NVOFA flow</th>
  </tr>
  <tr>
    <td align="center"><a href="img/07.png"><img src="img/07.png" alt="Original and DLSS wipe comparison of the same scene" width="100%"></a></td>
    <td align="center"><a href="img/08.png"><img src="img/08.png" alt="RAFT flow with more coherent motion regions around the subject and hair" width="100%"></a></td>
    <td align="center"><a href="img/09.png"><img src="img/09.png" alt="NVOFA flow with more fragmented regions and local direction changes" width="100%"></a></td>
  </tr>
</table>

In these screenshots, RAFT shows more coherent subject outlines and large motion regions; NVOFA has more fragments and local direction changes in the background, hair, and face.

### Measurements

Test setup: RTX 4070 SUPER 12 GB / driver 616.64. Analysis long edge 512; RAFT-Large 6 updates / FP32; NVOFA SLOW / 1×1 grid / temporal hints off. Same DLSS v2 settings, source-resolution SDR, cache off, fallback forbidden. Each clip/backend ran three times with swapped order; values below are medians.

| Clip | RAFT total time (effective fps) | NVOFA total time (effective fps) | NVOFA time reduction |
| --- | ---: | ---: | ---: |
| Square 1440×1440 · 165 frames | 17.08 s (9.66) | 10.54 s (15.65) | 38.3% |
| Portrait 1088×1920 · 243 frames | 20.77 s (11.70) | 14.23 s (17.08) | 31.5% |

Steady flow-stage times (input preparation, readback, and size restore, excluding DLSS): square RAFT / NVOFA **31.21 / 6.19 ms**, portrait **23.21 / 5.29 ms**.

| Quality proxy ↓ (39 pairs per clip; 8-bit RGB levels) | Square RAFT | Square NVOFA | Portrait RAFT | Portrait NVOFA |
| --- | ---: | ---: | ---: | ---: |
| Flow warp MAE · shared valid region | 3.099 | 2.592 | 3.469 | 3.652 |
| DLSS enhancement temporal residual · fixed RAFT reference | 1.560 | 1.610 | 1.446 | 1.478 |
| DLSS enhancement temporal residual · fixed NVOFA reference | 1.516 | 1.424 | 1.394 | 1.391 |

**Conclusion: on these two local clips NVOFA was faster, and the image-quality gap was not large.**

See the [full methodology, run-to-run variation, no-flow baseline, and reproduction commands (Chinese)](docs/experiments/README_FLOW_BENCHMARK.md) and [public measurements with component hashes](docs/experiments/README_FLOW_BENCHMARK_20260910.json).

## Quick start

You need **Windows 10 / 11 x64, a compatible NVIDIA RTX GPU and driver**, and the [Microsoft Visual C++ Redistributable x64](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist). See step 2 for GPU-specific runtimes.

### 1. Download and fully extract the package

Get the [portable release](https://github.com/banbanzhige/DLSS5Tool/releases/latest), not GitHub's automatically generated `Source code` archives.

| Your needs | Package |
| --- | --- |
| Enhancement, 2× / 4× super resolution, and frame interpolation | **Lite (recommended)**: `DLSS5Tool-vVERSION-win64.zip` |
| Optical flow as well | **Full**: all volumes starting with `DLSS5Tool-vVERSION-win64-full.zip.001` |
| Add model features to an existing lite installation | **Inference add-on**: all volumes starting with `DLSS5Tool-vVERSION-win64-addon.zip.001` |

- Full already includes the add-on. Download every volume of the same version into one folder, then open `.001` in 7-Zip to extract.
- Close the app before extracting the add-on beside `DLSS5Tool.exe`; do not create `mods/mods`.
- **Fully extract into a writable folder before running.** Keep `DLSS5Tool.exe` beside `_internal`. No separate Python or FFmpeg installation is required.

### 2. Select the runtime for your GPU

Confirm your GPU model first, then download the matching runtime from [Releases](https://github.com/banbanzhige/DLSS5Tool/releases/tag/zip).

| GPU | Setup |
| --- | --- |
| RTX 40 series | Use the bundled default runtime |
| RTX 30 series | Download `30系.zip` from the same Release and place the DLL as shown below |
| RTX 50 series | Download `50系.zip` from the same Release and place the DLL as shown below |

Download and extract the matching [nvngx_dlssnr.dll](https://github.com/banbanzhige/DLSS5Tool/releases/tag/zip). Close the app first, then place `nvngx_dlssnr.dll` from the matching archive into the `mods` folder beside the executable:

```text
mods\nvngx_dlssnr.dll
```

Use `mods` instead of overwriting the bundled DLL in `_internal`: changing managed files can prevent incremental updates. If you previously set a custom DLL path, check that setting too. The RTX 30-series runtime is a community adaptation, not an NVIDIA support commitment; compatibility depends on the GPU and driver combination.

### 3. Import, compare, and export

1. Open `DLSS5Tool.exe`, then drop in an image or video, or click **Choose a file**.
2. Switch to **DLSS** or **Compare**, then choose a style and adjust strength in **Effects**. Press `3` for split comparison.
3. Choose the output format and quality in the **Effects** export area or **Settings**. Enable super resolution or frame interpolation if you need them. The preview switches next to those options are off by default and do not change the export.
4. Export the current item, or add items to the queue for batch processing.

Start with the default settings and a short clip or single image. See the [user guide](docs/USER_GUIDE.en.md) for detailed controls.

### 4. Turn an image sequence into video (optional)

Choose **Queue → Add image sequence…** and select any frame, such as `frame_0001.png`. The app checks images in the same folder for matching prefixes, suffixes, and extensions, consecutive numbering, and identical dimensions, then adds the sequence as one queue item.

HDR image-sequence support is in current source but not in the existing local candidate package. Check Releases for the packages actually available to download.

- **Set the source frame rate:** Use the actual source rate, then enable upscaling or interpolation as needed. The output video has no audio.
- **Standard sequences:** Select **SDR / sRGB** for 8-bit, three-channel RGB PNG/JPG images.
- **HDR sequences (current source):** Use full-range BT.2020, 16-bit, three-channel RGB PNG images already encoded as PQ or HLG, and select the matching **Input image color**. Enable HDR high-precision processing to retain HDR on export. A 16-bit image is not necessarily HDR; linear EXR, HDR TIFF, grayscale, and HDR sequences with alpha are unsupported.

Re-import if you move or modify the source images. See the [image-sequence guide](docs/USER_GUIDE.en.md#image-sequences) for full requirements.

## Before you start

- **Results and speed vary by source and hardware.** Interactive comparison does not mean real-time model processing. High resolutions, 4× scaling, frame interpolation, and optical flow increase processing time and VRAM use.
- **Frame interpolation:** Export at 2× / 3× / 4×. 3× / 4× are experimental and may show motion errors. Super-resolution and interpolation previews are off by default and do not change the export selection.
- **GPU export is conditional:** GPU color conversion and direct transfer depend on hardware, components, resolution, and encoding settings. Ineligible configurations keep the existing encoding path; no fixed speedup is guaranteed. Check export logs and Diagnostics for the actual route.
- **Optical flow is optional.** With Full or the add-on installed, select a mode under **Models**. The app remembers your selection and restores it after a startup environment check; a failed check turns it off and reports the reason. First use checks RAFT-Large before enabling it; a saved off mode remains off. On Lite without the add-on, a failed first check that leaves flow off is expected. On the same NVIDIA GPU, RAFT flow can connect directly to DLSS. Supports SDR and separate HDR analysis copies, but not tiled temporal guidance. Still images skip flow without blocking tiling or changing video preferences.
- **DLSS render GPU:** Uses NVIDIA GPUs in high-performance order by default. If the display is on an iGPU, DLSS still runs on the discrete NVIDIA GPU. Multi-GPU systems can pick a device in Settings.
- **HDR export and preview.** Export writes HDR10 / HLG color tags (BT.2020, PQ/HLG, limited range) and 10-bit HEVC. Preview is tone-mapped to SDR. Dolby Vision / HDR10+ dynamic metadata is not copied. Single HDR still images are not supported; HDR image-sequence requirements are listed above. See [output and quality](docs/USER_GUIDE.en.md#output-and-quality).

## FAQ

**The app will not start, or a DLL is missing.**

Fully extract the package, keep the EXE beside `_internal`, and install the x64 Visual C++ runtime. Do not mix application files from different versions.

If you are running a `Source code` package, installing Python dependencies does not provide the native DLLs. Follow the [developer guide](docs/development/BUILDING.en.md). `missing dlssnr_host.dll` can also mean the v2 host was not found and the app tried the legacy host; it does not necessarily mean you need the legacy DLL.

**Preview is slow, or high-multiplier super resolution fails.**

Lower playback quality, try smaller media or 2× scaling, and temporarily disable optional optical flow. A larger cache cannot speed up frames that have not yet been processed.

**How do I update?**

Use **More → Check for updates**, or download the latest Release. Incremental packages support only the previous official release: **v2.3.1 targets v2.3.0 → v2.3.1 for both Lite and Full**. Earlier versions have no direct delta to this release. Users with official v2.2.2 / v2.3.0 inference components can reuse them with the new Lite package; see the [upgrade guide](docs/release/UPGRADE_v2.3.1.md). Do not replace only the EXE.

When a matching payload exists, the updater downloads changed files for the installed edition, verifies them, then asks again before exiting for the standalone helper to install it. Without a matching payload, Lite offers its complete archive; Full directs you to the release page for Full or Lite plus the matching add-on. Versions without the update helper must be upgraded by fully extracting the new release into a new folder. Incremental updates require the matching assets on the release page. See the [update guide](docs/USER_GUIDE.en.md).

**Still having trouble?**

Use **More → Diagnostics**, then open an [issue](https://github.com/banbanzhige/DLSS5Tool/issues) with the app version, GPU, driver, reproduction steps, and diagnostic log. Check logs for local paths and other private information before posting.

## Documentation

- [User guide](docs/USER_GUIDE.en.md): shortcuts, model settings, export details, and troubleshooting
- [Changelog](CHANGELOG.md)
- [Developer guide](docs/development/BUILDING.en.md) · [Contributing](CONTRIBUTING.md) · [Technical documentation index (Chinese)](docs/README.md)
- [Security reports](SECURITY.md)

## License

Project-owned source code is released under the [MIT License](LICENSE). `nvngx_dlssnr.dll`, NVIDIA SDKs, FFmpeg, and bundled Python dependencies remain subject to their respective upstream licenses and are not covered by this repository's MIT license. Full and add-on packages include RAFT weights only. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `mods/enhancement/licenses` in those packages.
