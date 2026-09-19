# Encoder integration phase-one evidence

Main report: [ENCODER_INTEGRATION_20260917.md](../ENCODER_INTEGRATION_20260917.md).
`MANIFEST.json` records archived report/media/source SHA256 and sizes.
Only `interface-profiles.json` and `lifecycle.json` use the final CUDA-interface DLL;
earlier reports intentionally retain intermediate negative results and source hashes.
No GUI export backend was switched. This is not complete upgrade acceptance.

Continuations have separate immutable manifests; the original 70-file archive
is unchanged:

- [CFR packet timing](packet-timing/README.md): ABI2, 21 cases/453 frames.
- [SDR GPU conversion](sdr-conversion/README.md): fused conversion, 144 byte
  comparisons and six conversion→encoder cases/126 frames; 85 related tests.

Temporary duplicates and build intermediates were removed after verification.
Only one current encoder candidate, one SDR PTX and registrations remain (~150 KiB).
No deployed runtime replaced. Retention review: 2026-09-24.
