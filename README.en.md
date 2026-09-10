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
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-v2.1.2-0E7490?style=flat&amp;labelColor=475569" alt="Source version v2.1.2" height="20"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/platform-Windows_x64-0369A1?style=flat&amp;labelColor=475569" alt="Platform: Windows x64" height="20"></a>
  <a href="#2-select-the-runtime-for-your-gpu"><img src="https://img.shields.io/badge/GPU-NVIDIA_RTX-0E7490?style=flat&amp;labelColor=475569" alt="GPU: NVIDIA RTX; select the runtime for your GPU generation" height="20"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-0369A1?style=flat&amp;labelColor=475569" alt="Project source is licensed under MIT" height="20"></a>
</p>

## In-app preview

<p align="center">
  <a href="img/03.png"><img src="img/03.png" alt="DLSS5Tool showing the original/DLSS split comparison, processing controls, and preview cache status" width="760"></a>
</p>

<p align="center"><sub>Application screenshot from v2.1.1</sub></p>

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

## Features

DLSS5Tool uses **DLSS 5 Neural Rendering** to enhance local videos and images. No game engine or user-supplied materials, normals, or depth data are needed. It is a post-processing tool for existing media, not a game plugin or frame-interpolation tool.

- **Image enhancement:** Default, Natural, and Cinema styles with strength, tone, structure, and skin-mask controls.
- **2× / 4× super resolution:** Upscale with RTX Video before enhancement, or process at the original size.
- **Interactive comparison:** Draggable wipe, side-by-side views, zoom, frame stepping, fullscreen, and a detachable preview.
- **Batch export:** Mix images and videos in one queue, with independent settings per item. Export MP4 / MKV / MOV video and preserve compatible source audio.
- **HDR video:** High-precision HDR10 / HLG processing and 10-bit export; see the HDR limitations below.
- **Optical flow guidance (optional):** Estimate inter-frame motion for temporal guidance. Results depend on the source. Depth reference currently has no observed effect in this release and depth inference is temporarily hidden.

The interface supports Simplified Chinese / English and light / dark themes. Change the language under **More → Language**, then restart the app.

The current source version is **v2.1.2; release packages have not been uploaded**: depth is temporarily hidden; the flow backend can be RAFT or NVOFA, with RAFT still the default. Launch with `run.bat` at the repo root. Download links below refer to published releases, not necessarily this version.

## Quick start

You need **Windows 10 / 11 x64, a compatible NVIDIA RTX GPU and driver**, and the [Microsoft Visual C++ Redistributable x64](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist). See step 2 for GPU-specific runtimes.

### 1. Download and fully extract the package

Get the [portable release](https://github.com/banbanzhige/DLSS5Tool/releases/latest), not GitHub's automatically generated `Source code` archives.

| Your needs | Package |
| --- | --- |
| Enhancement and 2× / 4× super resolution | **Lite (recommended)**: `DLSS5Tool-vVERSION-win64.zip` |
| Optical flow as well | **Full**: all volumes starting with `DLSS5Tool-vVERSION-win64-full.zip.001` |
| Add model features to an existing lite installation | **Inference add-on**: all volumes starting with `DLSS5Tool-vVERSION-win64-addon.zip.001` |

- Full already includes the add-on. Download every volume of the same version into one folder, then open `.001` in 7-Zip to extract.
- Close the app before extracting the add-on beside `DLSS5Tool.exe`; do not create `mods/mods`.
- **Fully extract into a writable folder before running.** Keep `DLSS5Tool.exe` beside `_internal`. No separate Python or FFmpeg installation is required.

### 2. Select the runtime for your GPU

| GPU | Setup |
| --- | --- |
| RTX 40 series | Use the bundled default runtime |
| RTX 30 series | Download `30系.zip` from the same release and place the DLL as shown below |
| RTX 50 series | Download `50系.zip` from the same release and place the DLL as shown below |

For RTX 30 / 50 series, close the app and place the matching `nvngx_dlssnr.dll` in the `mods` folder beside the executable:

```text
mods\nvngx_dlssnr.dll
```

Do not overwrite `_internal`. If you previously selected a custom DLL path, check that setting too. The RTX 30-series runtime is a community adaptation, not an NVIDIA support commitment; compatibility depends on the GPU and driver combination.

### 3. Import, compare, and export

1. Open `DLSS5Tool.exe`, then drop in an image or video, or click **Choose a file**.
2. Switch to **DLSS** or **Compare**, then choose a style and adjust strength in **Effects**. Press `3` for split comparison.
3. Choose the output format and quality in **Settings**. Enable 2× / 4× super resolution if you want to upscale.
4. Export the current item, or add items to the queue for batch processing.

Start with the default settings and a short clip or single image. See the [user guide](docs/USER_GUIDE.en.md) for detailed controls.

## Before you start

- **Results and speed vary by source and hardware.** Interactive comparison does not mean real-time model processing. High resolutions, 4× scaling, and optical flow increase processing time and VRAM use.
- **Optical flow is optional.** With Full or the add-on installed, select a mode under **Models**; it activates after an environment check. It starts off on every launch. Guidance currently supports only SDR, non-tiled processing; a single image has no inter-frame flow.
- **HDR metadata is not fully preserved.** Preview is tone-mapped to SDR. Export retains basic HDR10 / HLG color tags, but not Dolby Vision / HDR10+ dynamic metadata or some source HDR metadata. Static HDR images are not supported. See [output and quality](docs/USER_GUIDE.en.md#output-and-quality).

## FAQ

**The app will not start, or a DLL is missing.**

Fully extract the package, keep the EXE beside `_internal`, and install the x64 Visual C++ runtime. Do not mix application files from different versions.

**Preview is slow, or high-multiplier super resolution fails.**

Lower playback quality, try smaller media or 2× scaling, and temporarily disable optional optical flow. A larger cache cannot speed up frames that have not yet been processed.

**How do I update?**

Use **More → Check for updates**, or download the latest Release. In-app downloads contain the lite edition. Close the old app, extract the new version into a new folder, and configure the runtime for your GPU. For model features, use the matching Full edition or add-on.

**Still having trouble?**

Use **More → Diagnostics**, then open an [issue](https://github.com/banbanzhige/DLSS5Tool/issues) with the app version, GPU, driver, reproduction steps, and diagnostic log. Check logs for local paths and other private information before posting.

## Documentation

- [User guide](docs/USER_GUIDE.en.md): shortcuts, model settings, export details, and troubleshooting
- [Changelog](CHANGELOG.md)
- [Developer guide](docs/development/BUILDING.en.md) · [Contributing](CONTRIBUTING.md) · [Technical documentation index (Chinese)](docs/README.md)
- [Security reports](SECURITY.md)

## License

Project-owned source code is released under the [MIT License](LICENSE). `nvngx_dlssnr.dll`, NVIDIA SDKs, FFmpeg, and bundled Python dependencies remain subject to their respective upstream licenses and are not covered by this repository's MIT license. Candidate full/add-on packages include RAFT weights, not depth weights. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `mods/enhancement/licenses` in those packages.
