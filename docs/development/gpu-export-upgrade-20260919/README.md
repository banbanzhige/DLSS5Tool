# GPU export local upgrade evidence

[Report](../GPU_EXPORT_UPGRADE_20260919.md). This archive is separate from the previous
`gpu-export-integration-20260919` manifest, which remains unchanged.

Important reports: `hdr/gpu-bicubic.json`, `hdr/edge-cases.json`, actual HDR FG and
enhancement reports, `automatic-long-hdr.json`, `job-cancel.json`, `regression.json`,
`tkinter-recheck.json`. Older script/source hashes stay in each report. Source
snapshots are the final revision, not asserted to be the version of every earlier run.

`MANIFEST.json` records evidence/source/runtime-component hashes. Downloaded official
FFmpeg reference C files remain reference-only and are not copied into product or
source snapshots; their URLs and hashes are recorded for reconstruction.

Existing binaries are untouched. Additive registered components under runtime/gpu-export
enable only the tested large HDR automatic route. Redistribution review is pending;
this is not a release, universal GPU-only path or generic whole-export speed guarantee.
