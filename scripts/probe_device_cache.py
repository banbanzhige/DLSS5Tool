"""Real GPU cache ownership, pause, FG switch, NR seed and seek/replay checks."""
import argparse
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
    from dlss5tool import app_settings, dlss_engine, resident_sr, resident_hdr_runtime
    from dlss5tool.render_cache import RenderCache
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.memory_budget import memory_scope
    from dlss5tool.process_job import process_record
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--installed', action='store_true')
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    target = task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or target.exists() or not args.label.replace('-', '').isalnum():
        raise ValueError('Fresh registered evidence required')
    os.environ['DLSS5TOOL_SR_PREVIEW_LOG_ROOT'] = str(task/'preview-logs')
    os.environ['DLSS5TOOL_FG_LOG_ROOT'] = str(task/'native-logs')
    cancel = threading.Event()
    watchdog = threading.Timer(150, cancel.set)
    watchdog.daemon = True
    watchdog.start()
    source = task/'hlg-mild-audio.mp4'
    adapter = dlss_engine.available_render_adapters()[0]
    settings = app_settings.validate(dict(super_resolution_scale=2, frame_generation_multiplier=1,
        nr_quality={'total':1}, hdr_mode=True, render_gpu=adapter['id'], guidance_mode=0,
        output_mix=1, output_view=0))
    pool = SharedCacheBudget(limit_bytes=1024**3)
    manager = RenderCache(1024**3, pool_name=pool.name)
    owners = []
    original_spawn = resident_sr._spawn
    def spawn(*values):
        process = original_spawn(*values)
        owners.append(process.pid)
        return process
    def pixel_hash(frame):
        return hashlib.sha256(memoryview(frame)).hexdigest()
    report = dict(accepted=False, source_sha256=digest(source), owners=[])
    start = time.perf_counter()
    patch = mock.patch.object(resident_hdr_runtime, 'load_components', lambda: components_for(task))
    if args.installed:
        from contextlib import nullcontext
        patch = nullcontext()
    try:
      with memory_scope(pool), patch, mock.patch.object(resident_sr, '_spawn', spawn):
        current = manager.session(source, settings)
        current.set_prefetch(False)
        for index in range(3):
            original = current.wait(index, cancel)
        assert getattr(current, 'device_preview', False)
        base, base_thread, owner_count = manager.base, current.thread, len(owners)
        current.suspend()
        time.sleep(.4)  # drain at most one in-flight frame before checking pause
        paused = current.snapshot()['computed']
        time.sleep(.5)
        assert current.snapshot()['computed'] == paused
        report['pause'] = dict(computed=paused, stable_seconds=.5)
        raw_sr_hash = pixel_hash(original.mix_source)
        generated = manager.session(source, dict(settings, frame_generation_multiplier=2))
        generated.set_prefetch(False)
        pair = generated.wait(5, cancel)
        report['fg_switch'] = dict(base_owner_preserved=True, nr_owner_preserved=True,
            frame5_sha256=pixel_hash(pair[0]), base_pid=owners[-1])
        report['fg_switch'].update(base_same=manager.base is base, thread_same=base.thread is base_thread,
                                   owners_before=owner_count, owners_after=len(owners))
        assert manager.base is base and base.thread is base_thread and len(owners) == owner_count
        composition = manager.session(source, app_settings.validate(dict(settings, nr_quality={'total':.75})))
        composition.set_prefetch(False)
        composition.wait(2, cancel)
        assert manager.base is base
        report['composition_switch'] = dict(raw_nr_owner_preserved=True)
        changed = manager.session(source, app_settings.validate(dict(settings, intensity=.75)))
        changed.set_prefetch(False)
        assert manager.spatial is not None
        seed = manager.spatial.peek(2)
        assert seed is not None and pixel_hash(seed[0]) == raw_sr_hash
        changed.wait(2, cancel)
        report['nr_switch'] = dict(raw_sr_seed_equal=True, seed_sha256=raw_sr_hash)
        manager.close()
        # A small bounded cache must replay temporal history on a backwards seek.
        manager = RenderCache(256*1024**2, pool_name=pool.name)
        current = manager.session(source, dict(settings, nr_quality={'total':0}))
        current.set_prefetch(False)
        first = pixel_hash(current.wait(0, cancel)[0])
        for index in range(1,13):
            current.wait(index, cancel)
        assert current.peek(0) is None
        replayed = pixel_hash(current.wait(0, cancel)[0])
        assert replayed == first
        report['seek'] = dict(first_sha256=first, replay_sha256=replayed, pixels_equal=True)
        # Changing output geometry retires the old CUDA owner before reuse.
        previous = current
        manager.session(source, dict(settings, super_resolution_scale=4, nr_quality={'total':0}))
        assert previous.closed
        report['geometry_switch'] = dict(previous_owner_closed=True)
        report['accepted'] = True
    except BaseException as error:
        import traceback
        traceback.print_exc()
        report['error'] = type(error).__name__+': '+str(error)
    finally:
        watchdog.cancel()
        manager.close()
        report['pool_after_close'] = pool.snapshot()
        pool.close()
        report['owners'] = [process_record(pid) for pid in owners]
        report['elapsed_seconds'] = time.perf_counter()-start
        report['accepted'] = report['accepted'] and all(not row['present'] for row in report['owners'])
        assert report['pool_after_close']['reserved_processing_bytes'] == 0
        target.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    return 0 if report['accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
