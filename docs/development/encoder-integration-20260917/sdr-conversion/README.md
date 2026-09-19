# SDR GPU conversion and encoder evidence · 2026-09-17

## Result and limits

Unscaled SDR conversion now has a fused integer CUDA candidate, including even-size
black padding. BGR24/RGB24/BGRA/RGBA with packed or padded row pitch is supported.
No default backend changed; no D3D texture producer or real FG was attached.

- `fused-layouts.json`: 18 fixtures × 4 channel layouts × 2 pitches = 144 byte-exact
  comparisons with production FFmpeg BGR24/pad/yuv420p conversion. Covers black,
  white, ramp, random, red row and checker at 64×48, 320×180 and odd 321×181.
- `contract-guard.json`: final source revision, six GPU conversion→D2D→NVENC
  cases, 126 frames. Production decode MD5 and packet timing agree in every case.
  User `9月1日.mp4` frames resized only to prepare fixtures; 320×180 and 321×181
  test P5/CQ19 and P5/CQ23 (24 frames each); 1080p tests CQ19/24 frames;
  4K tests CQ19/6 frames, **not long-duration or performance acceptance**.
- `sdr-cuda-encode.json`: first equivalent six-case success before adding the
  contract factory guard. Final gate above checks the guarded path instead.
- `box-fixed.json` and `box-floor.json`: negative arithmetic candidates, retained.
  `rgb-floor.json`: 18/18 successful Torch arithmetic prototypes. Final script
  keeps these named arithmetic variants; source snapshots are final versions.
- `ptx-build.json`: existing NVRTC source/compiler/PTX hashes and options.
  First compile could not locate NVRTC builtins; preloading the existing matching
  DLL fixed it. No runtime, environment or SDK files were copied/downloaded.
- Related unit suite: 85 passed; `git diff --check` passed. Not the full suite.

## What changed

`gpu_sdr_yuv.cu` is independently implemented integer matrix/averaging arithmetic:
one CUDA thread writes a 2×2 luma block plus U/V, with no full-frame intermediate.
Matching this unscaled contract requires truncated RGB block average before
chroma conversion and truncated limited-range output (not a rounded substitute).
Upstream dispatch/coefficients were used only as compatibility references:
[FFmpeg 7.1.1 unscaled dispatch](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.1/libswscale/swscale_unscaled.c),
[matrix setup](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.1/libswscale/utils.c).
No third-party source implementation was embedded in product code.

`CudaSdrConverter` loads explicit precompiled PTX into the caller's CUDA context.
It does not import Torch or NVRTC. It owns one contiguous I420 output allocation,
checks owner thread/context and input memory geometry, and synchronizes its
conversion stream before returning a `ReadyYuvFrame` lease. Feed that lease to
the encoder before the next conversion overwrites it. The caller must finish
producer work before conversion; this is not yet a D3D fence/IPC implementation.

`from_contract` rejects HDR, scaling and nondefault color/filter contracts before
loading CUDA. Failures are explicit; there is no silent format/quality fallback.

The combined test uploads source RGBA from CPU as a fixture. After that upload,
RGB→I420→encoder ring stays on the GPU; only compressed packets return to CPU.
It does **not** establish end-to-end zero-readback export, container/audio muxing,
GPU resizing or a speed gain. Output mixes/views, HDR P010, VSR and actual FG
production-source attachment remain separate work. Supported behavior is pinned
to the tested FFmpeg 7.1.1 path, not asserted for every FFmpeg build/platform.

## Reproduction

```powershell
.venv/Scripts/python.exe -B scripts/build_sdr_yuv_ptx.py --work tmp/encode-integration-20260917/sdr-conversion
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/sdr_yuv_conversion_probe.py --work tmp/encode-integration-20260917/sdr-conversion --label fresh-conversion --method fused
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/sdr_cuda_encode_gate.py --work tmp/encode-integration-20260917/sdr-conversion --native-root tmp/encode-integration-20260917 --label fresh-encode
```

The build requires a fresh PTX path; retain/reuse the verified existing PTX when
present. Run GPU children with 150/240-second parent timeouts respectively.
`MANIFEST.json` verifies reports, reference MP4s, PTX and source snapshots.
