"""Real Tk/GPU latency regression, with deliberately blocked timestamp scan."""
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    task = ROOT/'tmp/preview-feedback-20260914'
    os.environ['DLSS5TOOL_SETTINGS_PATH'] = str(task/'ui-settings.json')
    os.environ['DLSS5TOOL_QUEUE_PATH'] = str(task/'ui-queue.json')
    import tkinter as tk
    from unittest import mock
    from dlss5tool import app_settings, gui, render_cache
    config = {**app_settings.DEFAULTS, 'guidance_mode': 0, 'host_backend': 'v2',
              'host_zero_fast_path': True, 'dlss_runtime': '__bundled__',
              'super_resolution_scale': 2, 'frame_generation_multiplier': 2,
              'preview_cache_mb': 1024, 'intensity': .5, 'output_mix': 1}
    config.update(preview_super_resolution=True, preview_frame_generation=True)
    root = tk.Tk()
    root.withdraw()
    errors, paints, source_paints = [], [], []
    root.report_callback_exception = lambda *args: errors.append(str(args))
    with mock.patch.object(app_settings, 'load', return_value=config):
        app = gui.App(root)
    original_fit, original_paint = app._draw_fit, app._shared_paint
    def fit(*args, **kwargs):
        if kwargs.get('badge') == gui.tr('status.shared_source'):
            source_paints.append(time.perf_counter())
        return original_fit(*args, **kwargs)
    def paint(session, pair, index):
        paints.append((time.perf_counter(), session.key, index))
        return original_paint(session, pair, index)
    app._draw_fit, app._shared_paint = fit, paint
    release = threading.Event()
    scan_started = threading.Event()
    actual_inspect = render_cache.inspect_source
    scans = []
    def slow_inspect(source, cancel):
        scans.append(str(source))
        scan_started.set()
        while not release.wait(.02):
            render_cache.check_cancel(cancel)
        return actual_inspect(source, cancel)
    def pump(predicate, timeout=100):
        deadline = time.perf_counter()+timeout
        while not predicate():
            root.update()
            if errors:
                raise AssertionError(errors)
            if getattr(app, '_shared_last_error', None):
                raise AssertionError(app._shared_last_error)
            if time.perf_counter() > deadline:
                raise TimeoutError('preview did not respond')
            time.sleep(.005)
    def current_ready():
        session = getattr(app, '_shared_session', None)
        return (session and session.key == render_cache.render_identity(app.video, app._shared_config())
                and session.peek(app._shared_output_index) is not None)
    try:
        with mock.patch.object(render_cache, 'inspect_source', side_effect=slow_inspect):
            started = time.perf_counter()
            assert app._load_video(str(ROOT/'tmp/hdr-e2e/source-sdr.mp4'))
            imported = time.perf_counter()
            pump(lambda: bool(source_paints), timeout=2)
            assert not release.is_set()
            first_delay = source_paints[0]-imported
            assert first_delay < .5, first_delay
            pump(scan_started.is_set, timeout=5)
            assert not paints, 'full result must not exist before validation'
            release.set()
            pump(current_ready)
            app.play()
            pump(lambda: not app.playing)
            app.jump_frame(12)
            pump(current_ready)
            spatial = app._shared_manager().spatial
            base = app._shared_manager().base
            spatial_before, base_before = spatial.new_frames, base.new_frames
            before = len(paints)
            changed = time.perf_counter()
            app._settings['v_outmix'].set(.3)
            app.on_output_settings_change()
            assert len(paints) > before, 'mix must repaint within the parameter callback'
            mix_delay = paints[-1][0]-changed
            assert mix_delay < .1, mix_delay
            pump(current_ready)
            assert app._shared_manager().base is base
            assert base.new_frames == base_before
            assert spatial.new_frames == spatial_before
            count = len(paints)
            for value in (.1, .2, .3, .4, .6, .8):
                app._settings['v_intensity'].set(value)
                app.on_settings_change()
                root.update()
            assert len(paints) == count, 'stale strength must not repaint as the new result'
            pump(current_ready)
            assert app._shared_manager().spatial is spatial
            assert spatial.new_frames == spatial_before
            assert app._shared_manager().base.settings['intensity'] == .8
            assert len(scans) == 1, scans
            app.jump_frame(0)
            app.play()
            pump(lambda: not app.playing)
            reports = []
            export = app._export_frame_generated_video
            def capture_export(*args):
                result = export(*args)
                reports.append(result)
                return result
            app._export_frame_generated_video = capture_export
            output = task/'ui-export.mp4'
            assert not output.exists()
            result = app._export_video_source(app.video, app._collect_settings(),
                app._collect_export_settings(), app._video_color_info, out_path=str(output), notify=False)
            assert result['success'], result
            assert reports[-1]['cache_hits'] == 48 and reports[-1]['new_frames'] == 0, reports[-1]
            report = {'first_source_after_import_ms': round(first_delay*1000, 2),
                      'first_source_from_load_call_ms': round((source_paints[0]-started)*1000, 2),
                      'mix_feedback_ms': round(mix_delay*1000, 2),
                      'timestamp_scans': len(scans), 'strength_preserves_spatial': True,
                      'mix_preserves_enhancement': True, 'latest_strength': .8,
                      'stale_effect_paints': 0, 'export_cache_hits': reports[-1]['cache_hits'],
                      'export_new_frames': reports[-1]['new_frames'], 'tk_errors': errors}
            (task/'ui-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps(report), flush=True)
    finally:
        release.set()
        app._shared_closing = True
        app.pause()
        app._shared_retire()
        thread = getattr(app, '_shared_retiring', None)
        if thread:
            deadline = time.perf_counter()+140
            while thread.is_alive() and time.perf_counter() < deadline:
                root.update()
                time.sleep(.02)
        app._on_close()


if __name__ == '__main__':
    main()
