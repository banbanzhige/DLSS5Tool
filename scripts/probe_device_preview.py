"""Validate the real GPU preview renderer, reference labels, demand and retirement."""
import argparse
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe, digest
    from scripts.s4_candidate import components_for
    from scripts.probe_resident_decode import process_record
    from dlss5tool import app_settings, dlss_engine, resident_sr
    from dlss5tool.resident_preview import render_preview
    from dlss5tool.frame_generation import resize_original, inspect_source, Cancelled
    from dlss5tool.image_sequence import open_capture
    from dlss5tool.video_export import FFmpegHDRVideoReader
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.memory_budget import memory_scope
    import cv2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--enhance', action='store_true')
    parser.add_argument('--multiplier', type=int, choices=(1,2,3,4), default=1)
    parser.add_argument('--demand-start', type=int, default=0)
    parser.add_argument('--cancel-after', type=int)
    args = parser.parse_args()
    task, source = safe(args.task, ROOT/'tmp'), safe(args.source, ROOT/'tmp')
    report_path = task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or report_path.exists() or not args.label.replace('-', '').isalnum():
        raise ValueError('Fresh registered evidence required')
    os.environ['DLSS5TOOL_SR_PREVIEW_LOG_ROOT'] = str(task/'preview-logs')
    cancel = threading.Event()
    watchdog = threading.Timer(150, cancel.set)
    watchdog.daemon = True; watchdog.start()
    inspection = inspect_source(source, cancel, allow_vfr=True)
    meta, rate, width, height, count = inspection
    if count > 96 or not 0 <= args.demand_start < count*args.multiplier:
        raise ValueError('Bounded source and valid output demand required')
    adapter = dlss_engine.available_render_adapters()[0]
    settings = app_settings.validate(dict(super_resolution_scale=2, frame_generation_multiplier=args.multiplier,
        nr_quality={'total':1 if args.enhance else 0}, hdr_mode=meta['is_hdr'], render_gpu=adapter['id'],
        guidance_mode=0, output_mix=1, output_view=0))
    expected_references = []
    reader = FFmpegHDRVideoReader(source, width, height, meta) if meta['is_hdr'] else open_capture(source)
    try:
        for index in range(count):
            pixels = reader.read()
            if not meta['is_hdr']:
                ok, bgr = pixels
                if not ok: raise ValueError('Reference decode ended early')
                pixels = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
            reference = resize_original(pixels, (width*2, height*2))
            expected_references.append(hashlib.sha256(memoryview(reference)).hexdigest())
    finally:
        reader.close() if hasattr(reader, 'close') else reader.release()
    report = dict(source_sha256=digest(source), settings=settings, frames=[], skipped=[],
                  expected_references=expected_references, demand_start=args.demand_start)
    owners = []
    original_spawn = resident_sr._spawn
    def spawn(*values):
        process = original_spawn(*values)
        owners.append(process.pid)
        return process
    def sink(sequence, pixels, reference, **options):
        expected = expected_references[sequence//args.multiplier]
        actual = hashlib.sha256(memoryview(reference)).hexdigest()
        report['frames'].append(dict(sequence=sequence, sha256=hashlib.sha256(memoryview(pixels)).hexdigest(),
                                    reference_equal=actual == expected, reference_sha256=actual))
        if actual != expected:
            raise ValueError('Preview original label differs from pre-SR source pixels')
        if args.cancel_after is not None and len(report['frames']) >= args.cancel_after:
            cancel.set()
    sink.skip = lambda sequence: report['skipped'].append(sequence)
    pool = SharedCacheBudget(limit_bytes=1024**3)
    started = time.perf_counter()
    try:
      with memory_scope(pool), mock.patch.object(resident_sr, '_spawn', spawn):
        report['result'] = render_preview(source, multiplier=args.multiplier, scale=2, settings=settings,
            cancel=cancel, frame_sink=sink, render_gate=lambda index: args.demand_start,
            source_inspector=lambda *values, **options: inspection, components=components_for(task))
        report['status'] = 'complete'
    except BaseException as error:
        import traceback
        traceback.print_exc()
        report.update(status='cancelled' if isinstance(error, Cancelled) else 'failed', error=str(error))
    finally:
        watchdog.cancel()
        report['elapsed_seconds'] = time.perf_counter()-started
        report['owners'] = [process_record(pid) for pid in owners]
        report['pool_after_close'] = pool.snapshot()
        pool.close()
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('status','elapsed_seconds','error','owners') if key in report}))
    expected_status = 'cancelled' if args.cancel_after is not None else 'complete'
    return 0 if report['status'] == expected_status and all(not row['present'] for row in report['owners']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
