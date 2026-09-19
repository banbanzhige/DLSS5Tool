# Encoding retest evidence

Evidence from the targeted 2026-09-16 rerun, not production deployment approval.
Reports and small encoded samples are retained; hashes recorded in MANIFEST.json.
Full conclusions: ../ENCODE_RECHECK_20260916.md.

The initial hdr.json used an 8-bit peak (255) for a generic PSNR helper even on
normalized half-float differences. Its HDR PSNR fields must not be interpreted.
hdr-final.json is the corrected rerun: no PSNR is assigned to HDR code-value
differences; equality, absolute error and channel counts are the relevant gates.
The initial report and script snapshot are retained instead of being overwritten.

## Verification and cleanup

- 24 tests passed using `.venv/Scripts/python.exe -B -m unittest
  tests.test_encode_recheck tests.test_hdr_pipeline tests.test_encode_acceptance_fg
  tests.test_encode_acceptance_contract tests.test_encode_acceptance_nvenc_ring -q`.
- `git diff --check` passed. All three bounded retest child processes completed;
  no production binaries/defaults were changed. Existing unrelated edits preserved.
- Archive manifest records 38 evidence/snapshot files, 5,448,062 bytes. It covers
  the actual data files, not this subsequently updated human-readable README.
- Archived media includes SDR production/corrected native/wrong-layout controls,
  complete 32-frame interpolation baseline/candidate MP4s, and PQ/HLG Main10.
- After matching each original/archive SHA256 and obtaining an exclusive read
  handle to confirm no output writer still held each file, removed only 28 duplicate
  files from `F:\project\DLSS5Tool\tmp\encode-recheck-20260916` (5,314,784 bytes).
  Root and entries checked for reparse points; no recursive root deletion used.
- Observed F free bytes: 102,095,863,808 before; 102,101,237,760 after. Difference
  5,373,952 bytes. Other system activity may affect whole-disk free-space changes.
- Only TASK.md remains in the task directory (about 2 KiB after closeout update),
  retained as task ownership/cleanup audit until 2026-09-23 review. No environment,
  prior task backup or original media was removed. Evidence is recoverable from
  this archive; regenerated samples may need a new registered directory/label.
