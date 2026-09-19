# CFR packet timing evidence · 2026-09-17

ABI2 adds access-unit PTS/DTS/duration, IDR and picture-type metadata. This is an
encoder boundary test, NOT a new muxer, audio sync acceptance, GPU RGB conversion,
real HDR fixture, or actual DLSS-G/FG 2x/3x/4x run. No production default changed.

- `input-color-tags.json`: 21/21 gates passed, 453 frames. SDR/PQ/HLG × short
  1/2/3-frame clips at 24 fps, 17 frames at 30000/1001, 32 at 60, 48 at 72/96.
  All decoded MD5, PTS/DTS/duration/IDR flags match FFmpeg. Display timestamps
  cover every submitted frame once. Empty EOS passes on all three codecs.
- `packet-timing.json`: initial negative control. All timings passed; HDR
  decoding differed because the raw-YUV reference omitted input color tags.
  Corrected reference supplies limited-range BT2020/PQ/HLG at input AND output,
  preserving the frame color properties used by production. Native unchanged.
  The initial script hash is retained in that report; the source snapshot is
  the final corrected script (initial version omitted input `*colors` and label).
- `lifecycle-abi2.json`: six allocation failures and nine cancellation cases
  clean up; three reopened sessions each return 120 packets and zero resources.
- Targeted unittest run: 78 passed; diff whitespace check passed. Not full suite.
- `MANIFEST.json`: reports, reference MP4s, source snapshots and hashes. Native
  elementary packet bytes have hashes and per-frame decoded MD5 in reports;
  only reference MP4s were written, no claim native MP4 muxing passed.

Timing rule is independently implemented for fixed-rate sequential input only:
PTS = NVENC output timestamp; DTS = decode ordinal minus configured B-frame delay.
No arbitrary VFR timestamps accepted. Relevant upstream contract:
[FFmpeg 7.1.1 timestamps](https://raw.githubusercontent.com/FFmpeg/FFmpeg/n7.1.1/libavcodec/nvenc.c).
No upstream implementation copied into product code.

Reproduce after rebuilding candidate:

```powershell
tmp/guidance-cuda-env/Scripts/python.exe -B scripts/encoder_packet_gate.py --work tmp/encode-integration-20260917/packet-timing --native-root tmp/encode-integration-20260917 --label fresh-label
```

Run GPU process under a parent 240-second timeout. Uses existing runtime/dependencies.
