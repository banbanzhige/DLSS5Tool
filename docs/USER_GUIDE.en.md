# User guide

[Back to product overview](../README.en.md) · [简体中文](USER_GUIDE.md)

For installation and GPU selection, start with [Quick start](../README.en.md#quick-start). This guide covers source version v2.1.2 (release packages have not been uploaded).

- [Controls and shortcuts](#controls-and-shortcuts)
- [Optional optical flow](#optional-optical-flow)
- [Performance and compatibility](#performance-and-compatibility)
- [Output and quality](#output-and-quality)
- [FAQ](#faq)
- [Runtime selection and verification](#runtime-selection-and-verification)

Supported video extensions: `MP4 / M4V / MOV / MKV / AVI / WebM`.
Supported image extensions: `PNG / JPG / JPEG / WebP / BMP / TIF / TIFF`.
Decoding depends on the file contents and codec support.

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

## Optional optical flow

### Base package and enhancement component

**Ordinary enhancement and super resolution do not require models.** The lite package contains no Torch, model architectures, or weights. For guidance, download the full edition, or extract the matching add-on beside `DLSS5Tool.exe`. The archive already contains `mods`; do not create `mods/mods`.

```text
DLSS5Tool.exe
_internal/                  # Base application runtime
mods/
  nvngx_dlssnr.dll           # Optional replacement runtime
  enhancement/
    guidance_worker.exe
    enhancement.json
    _internal/              # Required bundled inference dependencies
    models/                 # Component default weights, if included
  models/                   # User weight overrides
```

A complete component carries its dependencies; end users do not separately install Python, PyTorch, or CUDA Toolkit. Supported GPU hardware and drivers are still required. The app never automatically downloads models, installs dependencies, or runs installers. Compatible `.pth` weights are replaceable; renaming an incompatible architecture does not make it compatible. See [component layout and discovery](../mods/README.md). Candidate full/add-on packages include RAFT weights, not depth weights.

### Activation and recovery

NVOFA needs no flow weights or iterations. Initialization failure is reported before trying RAFT (weights required). Runtime failure stops processing, with no mid-video algorithm switch. RAFT-Large remains the default; the last analysis mode is remembered and restored after a startup environment check. Depth inference is temporarily hidden. Launch the development app with `run.bat` at the repo root.

1. Under **Models → Analysis mode**, choose Flow only; select RAFT or NVOFA.
2. Wait for the environment check. The mode remains off during the check and activates only after it succeeds.
3. If the check fails, read the reason shown below the mode. Check **Settings → Models & add-ons**, correct the component path, weights, device, or precision, then select the mode again. Base enhancement remains available.

Both the analysis mode and tuning are saved. On restart, the previously enabled mode is checked and restored automatically; a missing environment or failed check turns it off and reports the reason. First use checks the default RAFT-Large mode before enabling it; a saved off mode remains off. Installing the full component does not require a separate Python, PyTorch, or CUDA Toolkit installation. Do not copy just the component EXE.

A successful check does not guarantee enough VRAM for every source. Auto/GPU does not silently fall back to CPU; CPU must be selected explicitly with compatible settings.

### Candidate defaults

| Setting | Default |
| --- | --- |
| Analysis mode | Flow only; enabled after a startup environment check |
| Flow model, analysis long edge, updates | RAFT-Large, 512 px, six updates; alternative NVOFA grid: 1×1 |
| Flow direction and precision | Current → previous frame, FP32 |
| Flow display range | 5 px/frame; visualization only |

Model alignment may slightly change the actual input dimensions. Larger inputs, more updates, or larger models do not guarantee better final images. Explicit saved settings are not overwritten by this table. See [parameter notes](../docs/guidance/GUIDANCE_PARAMETERS.md).

### Inspect and export maps

The Models player offers Original, Flow, and Compare. The bottom **Compare ▾** menu controls wipe/side-by-side layout, target, centering, and legend. Both sides share frame position, zoom, and pan; the current complete pair remains visible while the next is pending. Flow hue represents direction and brightness magnitude.

Export a complete MP4 or the current PNG at source dimensions/frame rate, without audio or UI overlays. PNG is an 8-bit visualization, not raw floating-point data. Preview selection does not change enhanced-video export content. Export can be cancelled and preserves existing destination files on failure/cancellation.

## Performance and compatibility

- Flow supports SDR and a separate SDR analysis copy of HDR input; rendering and encoding retain the high-precision original. Flow previews/exports are SDR visualizations, not HDR footage.
- Still images skip temporal flow without changing video preferences or blocking image tiling. Tiled video temporal guidance remains unsupported.
- Flow analysis supports long edges up to 2048. Above 1280 is experimental and requires an updated enhancement component; defaults are unchanged. High-resolution RAFT costs substantially more memory/time and is not an export-size limit.
- Flow is zero for a standalone image. Seeking and scene cuts reset relevant history.
- Frame caches share a budget of 8192 MiB by default. Cache does not survive a restart or speed up frames not yet processed. The RAM readout shows registered caches, not total process memory or VRAM.
- Parallel export uses up to four workers and can consume more memory without necessarily being faster. Reduce concurrency or try smaller media when resources are limited.
- Model inference does not silently reduce precision, swap models, or switch to CPU on an out-of-memory error.

## Output and quality

### Same-resolution enhancement and super resolution

- **Super resolution off:** neural rendering runs at the source size. A video output-resolution limit downscales after processing and does not reduce DLSS input dimensions.
- **2× / 4× super resolution:** RTX Video scales first, then DLSS 5 runs at the target resolution. Width and height both scale, so pixel count becomes 4× or 16× and memory requirements increase accordingly.
- During super-resolution playback, the application uses a lower-cost proxy. Pausing, stepping, or releasing the scrubber generates an exact target-resolution preview.
- Very large still images are tiled at 45 million pixels or above, or when a dimension exceeds 8192. When device capacity is known, 6/8 GiB GPUs tile earlier. Full GPU textures are still required: dimensions must not exceed 16384, and success also depends on VRAM and the runtime.
- Custom video output dimensions now allow up to 16384 per side. Actual dimensions must pass the encoder trial; not every size is guaranteed to encode.

### Formats, encoding, and temporal behavior

- **Images:** keep the source format by default. PNG and TIFF are written losslessly; JPEG and similar formats are re-encoded. Lossless file encoding does not mean the enhanced pixels equal the source.
- **Video:** MP4 by default, with MKV, MOV, and Match input options. Match input supports MP4/M4V, MKV, and MOV; AVI and WebM safely fall back to MP4.
- **Quality:** SDR defaults to H.264 and automatically uses HEVC when either final output dimension exceeds 4096, with quality profiles or a custom bitrate. Maximum quality is still lossy compression, not mathematically lossless video.
- **Temporal behavior:** Strict sequence keeps one continuous processing history. Visually lossless parallel segments can accelerate SDR video, but segment boundaries may differ slightly. High-precision HDR and super-resolution jobs use strict single-session processing.
- **Audio:** compatible source audio is copied directly. Incompatible MP4/MOV audio falls back to AAC.

### HDR and experimental controls

- High-precision HDR processing applies only to correctly tagged PQ/HLG video; static HDR images are not currently supported.
- Large HDR jobs may use the requested 2/3 in-flight frames when estimated free VRAM permits, otherwise 1. Strict single-session temporal order is unchanged; estimates are not allocation guarantees.
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

**Why is inference off after restarting or after selecting a mode?**

Startup remembers the last mode and checks it automatically. It is not active while the check runs; only a missing environment or failed check turns it off, while tuning is retained. See the status, Details, or log, then check **Settings → Models & add-ons**, fix weights, component paths, device, or precision, and retry. Do not copy only the component EXE.

**Flow slows rendering. Will a larger cache help?**

Per-frame models add processing time. A larger cache does not skip first-pass inference. Check the actual GPU, precision, and timing logs, then compare RAFT and NVOFA at identical settings. Model-size or parameter changes can affect quality; judge moving detail, ghosting, and flicker through continuous playback, not only still screenshots.

**How is 8K output from 4× super resolution encoded?**

4× processing of 1080p produces 7680×4320. When either final output dimension exceeds 4096, export automatically uses HEVC/H.265 while keeping SDR content SDR. Smaller SDR output retains H.264; HDR uses HEVC Main10. A trial encode validates the actual size and settings before export, with CPU fallback if GPU encoding is unavailable (potentially much slower at 8K). The log identifies the selected encoder. MP4, MKV and MOV support HEVC, but playback also requires HEVC support. If export still fails, lower the final output size and include the encoder error log when reporting it.

**How do updates work?**

The portable build checks GitHub Releases quietly at startup. You can also choose **More → Check for updates**. A prompt appears only for a newer release and asks before downloading. The download is the lite portable app; it does not include the full or add-on packages and never replaces the running application. Close the old copy, extract the new package into a new folder, then reinstall the matching add-on or use the full edition if you still need flow.

## Runtime selection and verification

Auto-detection prefers an existing custom path, then nvngx_dlssnr.dll in modules, then a unique recognized alternative, then bundled `_internal`. Multiple candidates are not guessed; select one manually or force bundled. No need to overwrite `_internal`. The modules directory defaults to mods beside the executable and can be changed; detected paths are shown. The RTX 30-series runtime is a community adaptation and is not an NVIDIA support commitment. GPU and driver combinations still require real-hardware verification.

<details>
<summary>Recorded runtime versions and SHA-256 values</summary>

- **RTX 30 series:** `310.8.SF-v2` (file version `310.8.SF.0`)
  - SHA-256: `6EB209E764F39872625DEBD6ABAF45E2BB6322F6F270F781F70C059AE30B3927`
- **Default RTX 40-series DLL:**
  - SHA-256: `CEB6432F6FBDF44D886014BCD47241932BF8B67439FEEF9BBDD0961436662650`
- **RTX 50 series:** NVIDIA-signed `310.8.0.0`
  - SHA-256: `E16BCF15E16E13F527491CDF7845B2FE6521A738D8F7C9C721866A8496E1FC8E`

For a replacement DLL, run the following from the application directory. To check the bundled runtime instead, change `mods` to `_internal`. These are recorded v2.1.1 values; use the release notes for the version you downloaded.

```powershell
Get-FileHash .\mods\nvngx_dlssnr.dll -Algorithm SHA256
```

A matching hash identifies the file only; it is not a licensing or compatibility guarantee.

</details>
