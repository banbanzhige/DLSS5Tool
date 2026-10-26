"""Verify an imported video fills the idle NR preview through its last frame.

The registered task owns isolated settings, audio and worker logs. No video or
frame images are generated. This deliberately uses the existing 8 GiB RAM
budget, so the supplied source must fit that budget at the selected quality.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    task = args.task.absolute()
    if task != task.resolve() or not task.is_relative_to(ROOT / 'tmp'):
        raise ValueError('Use a registered repository tmp task without links')
    if any(p.is_symlink() or p.is_junction() for p in (task, *task.parents)):
        raise ValueError('Linked task paths are not supported')
    if not (task / 'TASK.md').is_file() or (task / 'autofill.json').exists():
        raise ValueError('Use a registered task without previous probe evidence')
    source = args.source.resolve(strict=True)
    settings = json.loads((ROOT / 'var/dlss5_settings.json').read_text(encoding='utf-8'))
    settings.update(super_resolution_scale=2, frame_generation_multiplier=2,
                    preview_super_resolution=False, preview_frame_generation=False,
                    preview_auto_prerender=True, preview_cache_mb=8192,
                    preview_detached=False, guidance_mode=0, workbench_mode='local')
    with (task / 'settings.json').open('x', encoding='utf-8') as handle:
        json.dump(settings, handle)
    for key, name in (('SETTINGS', 'settings.json'), ('QUEUE', 'queue.json'),
                      ('EXPORT_HISTORY', 'history.json')):
        os.environ[f'DLSS5TOOL_{key}_PATH'] = str(task / name)
    os.environ['TEMP'] = os.environ['TMP'] = str(task)

    import tkinter as tk
    from dlss5tool import gui

    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *exc: errors.append(''.join(traceback.format_exception(*exc)))
    app = gui.App(root)
    report = {'passed': False, 'play_pressed': False}
    started = time.perf_counter()
    first_ready = None
    peak_cache = 0
    pressure_samples = 0
    last_report = 0
    try:
        assert app._load_video(str(source))
        width, height = app._playback_preview_size()
        theoretical = (app._media_w * app._media_h + width * height) * 3 * app.nframes
        assert theoretical < app._preview_cache_bytes(), 'source does not fit the 8 GiB task budget'
        completed = None
        while time.perf_counter() - started < args.timeout:
            root.update()
            if errors:
                raise RuntimeError(errors)
            assert not app.playing and app._frame == 0
            if app._cached_dlss(0) is not None and not app._dlss_pending and first_ready is None:
                first_ready = time.perf_counter() - started
            snapshot = app._shared_cache_pool.snapshot()
            peak_cache = max(peak_cache, snapshot['used_bytes'])
            pressure_samples += bool(app._shared_cache_pool.under_pressure)
            ready = len(app._dlss_frame_cache)
            if ready == app.nframes and len(app._source_frame_cache) == app.nframes:
                completed = time.perf_counter() - started
                break
            if time.perf_counter() - last_report >= 10:
                print(json.dumps({'elapsed_seconds': round(time.perf_counter() - started, 2),
                    'rendered_frames': ready, 'total_frames': app.nframes,
                    'cache_MiB': round(snapshot['used_bytes'] / 1024**2, 2),
                    'prerender_active': app._pre_rendering,
                    'memory_pressure': app._shared_cache_pool.under_pressure}), flush=True)
                last_report = time.perf_counter()
            time.sleep(.01)
        if completed is None:
            raise TimeoutError(str(app.eta_label.cget('text')))
        deadline = time.perf_counter() + 1
        while time.perf_counter() < deadline:
            root.update()
            time.sleep(.01)
        assert all(app._cached_dlss(i, (width, height)) is not None for i in range(app.nframes))
        assert not app.playing and app._frame == 0 and not app._dlss_pending
        assert app._preview_wait_key is None
        assert app._pre_rendering and app._preview_decode_after is not None
        status = str(app.eta_label.cget('text'))
        assert f'{app.nframes}/{app.nframes}' in status and 'fps' not in status
        assert peak_cache <= app._preview_cache_bytes()
        report.update(passed=True, source_size=list(app._source_size()), frames=app.nframes,
            fps=app.fps, preview_size=[width, height], first_ready_seconds=first_ready,
            all_frames_ready_seconds=completed, last_frame_cached=True,
            peak_cache_MiB=peak_cache / 1024**2, pressure_samples=pressure_samples,
            auto_monitor_active=True, status=status, callback_errors=errors)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    except Exception:
        report['error'] = traceback.format_exc()
        raise
    finally:
        app._freeze_preview_cache(resume_ms=None)
        worker = getattr(app, '_play_dlss_thread', None)
        if worker is not None:
            worker.join(timeout=5)
        report['worker_retired'] = worker is None or not worker.is_alive()
        app._on_close()
        (task / 'autofill.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
