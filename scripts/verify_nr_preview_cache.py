"""Verify real NR preview presentation with SR/FG enabled only for export.

Uses a hidden Tk window and isolated settings in an existing registered task.
The supplied video is read only; no video output or frame images are saved.
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
    parser.add_argument('--timeout', type=float, default=45)
    args = parser.parse_args()
    task = args.task.absolute()
    if task != task.resolve() or not task.is_relative_to(ROOT / 'tmp'):
        raise ValueError('Use a task inside the repository tmp directory without links')
    if any(p.is_symlink() or p.is_junction() for p in (task, *task.parents)):
        raise ValueError('Linked task paths are not supported')
    if not (task / 'TASK.md').is_file() or (task / 'nr-preview.json').exists():
        raise ValueError('Use a registered task without previous probe evidence')
    source = args.source.resolve(strict=True)
    config = json.loads((ROOT / 'var/dlss5_settings.json').read_text(encoding='utf-8'))
    config.update(super_resolution_scale=2, frame_generation_multiplier=2,
                  preview_super_resolution=False, preview_frame_generation=False,
                  preview_auto_prerender=False, preview_cache_mb=256,
                  preview_detached=False, guidance_mode=0, workbench_mode='local')
    settings_path = task / 'settings.json'
    with settings_path.open('x', encoding='utf-8') as handle:
        json.dump(config, handle)
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
    started = time.perf_counter()
    report = {'passed': False}

    def pump_ready():
        while time.perf_counter() - started < args.timeout:
            root.update()
            if errors:
                raise RuntimeError(errors)
            if app._cached_dlss(app._frame) is not None and not app._dlss_pending:
                return
            time.sleep(.005)
        raise TimeoutError(str(app.eta_label.cget('text')))

    try:
        assert app._load_video(str(source))
        pump_ready()
        first_ready = time.perf_counter() - started
        pixels = app._cached_dlss(0)
        before = app._preview_processed_frames
        assert before == 1 and not app._uses_shared_render()
        assert app._preview_wait_key is None
        assert app._collect_export_settings()['super_resolution_scale'] == 2
        app._export_settings['v_super_resolution'].set(gui.SUPER_RESOLUTION_NAMES[4])
        app._on_super_resolution_change()
        app.display_view(quality='full')
        pump_ready()
        assert app._cached_dlss(0) is pixels
        assert app._preview_processed_frames == before
        report.update(passed=True, source_size=list(app._source_size()), frames=app.nframes,
                      fps=app.fps, preview_shape=list(pixels.shape),
                      first_presentation_seconds=first_ready,
                      export_scale_change_preserved_pixels=True,
                      preview_wait_cleared=app._preview_wait_key is None,
                      processed_frames=before, callback_errors=errors)
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
        (task / 'nr-preview.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
