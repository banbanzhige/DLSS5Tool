<h1 align="center">
  <img src="assets/readme-banner-v1.png" alt="DLSS5Tool — local neural rendering, 2× / 4× super resolution, real-time comparison, and batch export" width="1200">
</h1>

<p align="center">
  Local image and video processing · Realtime window enhancement, in one workbench for adjustment and comparison.
</p>

<p align="center">
  <a href="README.md">简体中文</a> ·
  <strong>English</strong> ·
  <a href="https://github.com/banbanzhige/DLSS5Tool/releases/latest">Download portable release</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/source-v3.1.1-0E7490?style=flat&amp;labelColor=475569" alt="Source version v3.1.1" height="20"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/platform-Windows_x64-0369A1?style=flat&amp;labelColor=475569" alt="Platform: Windows x64" height="20"></a>
  <a href="#2-select-the-runtime-for-your-gpu"><img src="https://img.shields.io/badge/GPU-NVIDIA_RTX-0E7490?style=flat&amp;labelColor=475569" alt="GPU: NVIDIA RTX; select the runtime for your GPU generation" height="20"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPLv3-0369A1?style=flat&amp;labelColor=475569" alt="Combined source licensed under GPLv3" height="20"></a>
</p>

This README describes the **v3.1.1** draft, fixing audio/video synchronization for affected exports and improving DLSS initialization failure guidance. Sustained memory growth remains unresolved in some environments. Check the actual Release assets for public availability. See the [v3.1.1 release notes](docs/release/RELEASE_NOTES_v3.1.1.md) and the [redistribution restrictions](#license).

## Features

DLSS5Tool brings **local media processing and realtime window enhancement** into one workbench. No game engine integration or capture card is needed.

- **Local images and videos:** DLSS 5 neural rendering, original/enhanced wipe or side-by-side comparison, frame stepping, batch export, and image sequences to video.
- **Local upscaling and interpolation:** RTX Video 2× / 4× upscaling; export video with 2× / 3× / 4× interpolation (3× / 4× are experimental).
- **Realtime window enhancement:** Select a running window, combine enhancement, scaling and interpolation steps, save presets, and protect fixed regions. Supports windowed and full-screen output.
- **Smoother adjustment:** Copy supported parameter groups, restore defaults, or undo changes, with pending, updating and applied feedback. Local and realtime settings are saved independently; not every parameter is automatically synchronized.
- **Optional optical flow:** Lite includes local NVOFA hardware flow. Install Full or the inference add-on only when you need RAFT.
- **Output and resource management:** Local HDR10 / HLG 10-bit output and conditional GPU export, with coordinated GPU resource use and shared diagnostics across modes.

Switch modes using the paired-arrow icon in the title bar, `Ctrl+Tab`, or **More → Work mode**. Both modes support Simplified Chinese / English and light / dark themes. Change **More → Language**, then restart the app.

## Current version interface

### Local media · Compare and adjust

<p align="center">
  <a href="img/v3.0.0-本地.png"><img src="img/v3.0.0-本地.png" alt="v3.0.0 local media: original/enhanced wipe, timeline, and picture controls (Chinese UI)" width="960"></a>
</p>

### Realtime enhancement · Source, preset and output

<p align="center">
  <a href="img/v.3.0.0-实时.png"><img src="img/v.3.0.0-实时.png" alt="v3.0.0 realtime idle configuration: source window, preset, output mode and picture controls (Chinese UI)" width="960"></a>
</p>

The realtime screenshot shows idle configuration, not active enhancement or a frame-rate demonstration. Click either screenshot for the original image. Both modes support light and dark themes.

<details>
<summary>View the older introduction video and before/after example</summary>

[▶ Watch the introduction on Bilibili](https://www.bilibili.com/video/BV1yhaN6uEWB) (older-version demonstration; use the v3.0.0 screenshots and guides for current controls).

### Before and after

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

</details>

## RAFT / NVOFA optical flow comparison

Optical flow provides motion estimates for video enhancement and may improve temporal stability, depending on the source. Try the NVOFA backend included in Lite first; install the inference add-on to compare RAFT. On the two historical test clips below, NVOFA was faster and the quality gap was small. This is not a performance guarantee for v3.0.0 or other workloads.

<details>
<summary>Expand flow screenshots and historical measurements (2026-09-10)</summary>

Turn on the flow-map preview to see the motion estimates: **color encodes direction, brightness encodes displacement magnitude**.

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

These measurements were taken on 2026-09-10 in the specified setup; performance may differ with other media or settings: RTX 4070 SUPER 12 GB / driver 616.64. Analysis long edge 512; RAFT-Large 6 updates / FP32; NVOFA SLOW / 1×1 grid / temporal hints off. Same DLSS v2 settings, source-resolution SDR, cache off, fallback forbidden. Each clip/backend ran three times with swapped order; values below are medians.

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

</details>

## Quick start

You need **Windows 10 / 11 x64, a compatible NVIDIA RTX GPU and driver**, and the [Microsoft Visual C++ Redistributable x64](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist). See step 2 for GPU-specific runtimes.

### 1. Download and fully extract the package

Get the [portable release](https://github.com/banbanzhige/DLSS5Tool/releases/latest), not GitHub's automatically generated `Source code` archives.

| Your needs | Package |
| --- | --- |
| Local enhancement/upscaling/interpolation, realtime window enhancement, and local NVOFA hardware flow | **Lite (recommended)**: `DLSS5Tool-vVERSION-win64.zip` |
| Local RAFT flow inference components and weights as well | **Full**: all volumes starting with `DLSS5Tool-vVERSION-win64-full.zip.001` |
| Add RAFT to Lite of the same version | **Inference add-on**: all volumes starting with `DLSS5Tool-vVERSION-win64-addon.zip.001` |

- Full already includes the add-on. Download every volume of the same version into one folder, then open `.001` in 7-Zip to extract.
- Close the app before extracting the add-on beside `DLSS5Tool.exe`; do not create `mods/mods`.
- **Fully extract into a writable folder before running.** Keep `DLSS5Tool.exe` beside `_internal`. No separate Python or FFmpeg installation is required.

### 2. Select the runtime for your GPU

Confirm your GPU model first. If you need a replacement runtime, use the separate [zip download page](https://github.com/banbanzhige/DLSS5Tool/releases/tag/zip):

| GPU | Setup |
| --- | --- |
| RTX 40 series | Use the bundled default runtime |
| RTX 30 series | Download `30系.zip` from the zip page and place the DLL as shown below |
| RTX 50 series | Download `50系.zip` from the zip page and place the DLL as shown below |

Download and extract the matching [nvngx_dlssnr.dll](https://github.com/banbanzhige/DLSS5Tool/releases/tag/zip). Close the app first, then place `nvngx_dlssnr.dll` from the matching archive into the `mods` folder beside the executable:

```text
mods\nvngx_dlssnr.dll
```

Use `mods` instead of overwriting the bundled DLL in `_internal`: changing managed files can prevent incremental updates. If you previously set a custom DLL path, check that setting too. The RTX 30-series runtime is a community adaptation, not an NVIDIA support commitment; compatibility depends on the GPU and driver combination.

### 3. Local media: import → compare → export

1. Open `DLSS5Tool.exe`, then drop in an image or video, or click **Choose a file**.
2. Switch to **DLSS** or **Compare**, then choose a style and adjust strength in **Effects**. Press `3` for split comparison.
3. Choose the output format and quality in the **Effects** export area or **Settings**. Enable super resolution or frame interpolation if you need them. The preview switches next to those options are off by default and do not change the export.
4. Export the current item, or add items to the queue for batch processing.

Start with the default settings and a short clip or single image. See the [user guide](docs/USER_GUIDE.en.md) for detailed controls.

### 4. Realtime: select a window → choose a preset → start/stop

1. Open the window you want to enhance, then use the title-bar arrows or `Ctrl+Tab` to switch to **Realtime enhancement**.
2. Select the source window, choose and load a built-in preset. For a first run, try the detail-enhancement preset with windowed output.
3. Start enhancement and wait for the actual output before adjusting settings. Follow any prompts to apply changes. The source snapshot is for identifying the window and placing protected regions, not a live enhanced preview.
4. Stop the session with **Stop enhancement**; the default emergency stop shortcut is `Ctrl+Alt+F10`. Check that it is available before using full-screen output.

Switching back to local media does not automatically stop the realtime session; Stop remains available in the bottom status bar. See the [realtime guide (Chinese)](docs/REALTIME_USER_GUIDE.md) for controls and limitations. Target-game, HDR and multi-display behavior still needs validation in the intended environment.

### 5. Turn an image sequence into video (optional)

Under **Queue → Add image sequence…**, select one frame, such as `frame_0001.png`, to include its full consecutively numbered sequence. Select multiple frames from the same sequence to import only the selected consecutive range. Other complete sequences in the folder are listed for you to opt in. Frames must have matching prefixes, suffixes, extensions, and dimensions.

- **Set the source frame rate:** Use the actual source rate, then enable upscaling or interpolation as needed. The output video has no audio.
- **Standard sequences:** Select **SDR / sRGB** for 8-bit, three-channel RGB PNG/JPG images.
- **HDR sequences:** Use full-range BT.2020, 16-bit, three-channel RGB PNG images already encoded as PQ or HLG, and select the matching **Input image color**. Enable HDR high-precision processing to retain HDR on export. A 16-bit image is not necessarily HDR; linear EXR, HDR TIFF, grayscale, and HDR sequences with alpha are unsupported.

Re-import if you move or modify the source images. See the [image-sequence guide](docs/USER_GUIDE.en.md#image-sequences) for full requirements.

## Before you start

- **Results and speed vary by source and hardware.** Interactive comparison does not mean real-time model processing. High resolutions, 4× scaling, frame interpolation, and optical flow increase processing time and VRAM use.
- **Frame interpolation:** Export at 2× / 3× / 4×. 3× / 4× are experimental and may show motion errors. Super-resolution and interpolation previews are off by default and do not change the export selection.
- **GPU export is conditional:** GPU color conversion and direct transfer depend on hardware, components, resolution, and encoding settings. Ineligible configurations keep the existing encoding path; no fixed speedup is guaranteed. Check export logs and Diagnostics for the actual route.
- **Local optical flow is off by default.** Enable it under **Models** when needed. NVOFA uses the components bundled with Lite and still requires compatible hardware and drivers. RAFT requires Full or the matching add-on. The app remembers the selection and restores it after a successful environment check; failures report the reason. Flow supports SDR video and separate HDR analysis copies, but not tiled temporal guidance. Still images skip flow without changing video preferences.
- **Shared controls do not mean everything stays in sync.** Copy only the groups explicitly supported by the UI and confirm the overwrite scope. Switching modes does not automatically overwrite settings, realtime drafts or queued jobs. Rendering and resource release still take time; follow pending, updating and session-status feedback.
- **DLSS render GPU:** Uses NVIDIA GPUs in high-performance order by default. If the display is on an iGPU, DLSS still runs on the discrete NVIDIA GPU. Multi-GPU systems can pick a device in Settings.
- **Local HDR export and preview.** Export writes HDR10 / HLG color tags (BT.2020, PQ/HLG, limited range) and 10-bit HEVC. Local preview is tone-mapped to SDR. Dolby Vision / HDR10+ dynamic metadata is not copied. Single HDR still images are not supported; HDR image-sequence requirements are listed above. See [output and quality](docs/USER_GUIDE.en.md#output-and-quality).

## FAQ

**The app will not start, or a DLL is missing.**

Fully extract the package, keep the EXE beside `_internal`, and install the x64 Visual C++ runtime. Do not mix application files from different versions.

If you are running a `Source code` package, installing Python dependencies does not provide the native DLLs. Follow the [developer guide](docs/development/BUILDING.en.md). `missing dlssnr_host.dll` can also mean the v2 host was not found and the app tried the legacy host; it does not necessarily mean you need the legacy DLL.

**Preview is slow, or high-multiplier super resolution fails.**

Lower playback quality, try smaller media or 2× scaling, and temporarily disable optional optical flow. A larger cache cannot speed up frames that have not yet been processed.

**How do I update?**

Use **About → Check for updates**, or download a Release. This build prepares lite/full incremental updates from v3.1.0 to v3.1.1; they become available through Releases and the updater only after official publication. Earlier versions should use complete archives; updates are not automatically chained across versions.

Choose `lite` for Lite, or `full` when inference components are installed. Do not replace only the EXE or mix old components into v3.1.1. The [historical component-reuse guide](docs/release/UPGRADE_v2.3.1.md) applies only to the versions it lists.

When a matching payload exists, the updater downloads changed files for the installed edition, verifies them, then asks again before exiting for the standalone helper to install it. Without a matching payload, Lite offers its complete archive; Full directs you to the release page for Full or Lite plus the matching add-on. Versions without the update helper must be upgraded by fully extracting the new release into a new folder. Incremental updates require the matching assets on the release page. See the [update guide](docs/USER_GUIDE.en.md).

If in-app downloading fails but your browser can download the official `.dlssupdate`, select it under **About → Install local update package**. Its source version and edition must match the installation. Import still requires GitHub API access to verify the official SHA-256, followed by another confirmation before installation. Do not extract the update or overwrite files manually. This entry point requires a newer app with the update helper; older clients without it need a complete-archive upgrade first.

**Still having trouble?**

Use **About → Export diagnostics**, then open an [issue](https://github.com/banbanzhige/DLSS5Tool/issues) with the app version, GPU, driver, reproduction steps, and diagnostic log. Check logs for local paths and other private information before posting.

## Documentation

- [User guide](docs/USER_GUIDE.en.md): shortcuts, model settings, export details, and troubleshooting
- [Realtime guide (Chinese)](docs/REALTIME_USER_GUIDE.md): source windows, presets, protected regions, and stopping a session
- [Changelog](CHANGELOG.md)
- [Developer guide](docs/development/BUILDING.en.md) · [Contributing](CONTRIBUTING.md) · [Technical documentation index (Chinese)](docs/README.md)
- [Security reports](SECURITY.md)

## License

The combined source incorporating Magpie / Veyra realtime modules uses [GPLv3](LICENSE). The [original MIT notice](licenses/DLSS5Tool-original-MIT.txt) remains in force for previously MIT-licensed code and releases. Vendor SDKs, runtimes and other dependencies retain their own terms. The realtime host is for local validation: redistribution of the GPL/proprietary NVIDIA combination remains under review, not approved by this license change. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [realtime provenance](native/realtime/NOTICE.md).
