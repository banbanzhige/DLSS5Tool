"""Exercise the real Tk player with SR/FG; isolated settings, no user-state writes."""
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    task = ROOT/'tmp/render-cache-20260914'
    os.environ['DLSS5TOOL_SETTINGS_PATH'] = str(task/'ui-settings.json')
    os.environ['DLSS5TOOL_QUEUE_PATH'] = str(task/'ui-queue.json')
    import tkinter as tk
    from unittest import mock
    from dlss5tool import app_settings, gui
    config = {**app_settings.DEFAULTS, 'guidance_mode': 0, 'host_backend': 'v2',
              'host_zero_fast_path': True, 'dlss_runtime': '__bundled__',
              'super_resolution_scale': 2, 'frame_generation_multiplier': 2,
              'preview_cache_mb': 256, 'intensity': .5}
    config.update(preview_super_resolution=True, preview_frame_generation=True)
    root = tk.Tk()
    root.withdraw()
    errors, paints = [], []
    root.report_callback_exception = lambda *args: errors.append(str(args))
    with mock.patch.object(app_settings, 'load', return_value=config):
        app = gui.App(root)
    original_paint = app._shared_paint
    def paint(session, pair, index):
        paints.append(index)
        return original_paint(session, pair, index)
    app._shared_paint = paint
    def pump(predicate, seconds=100):
        deadline = time.perf_counter()+seconds
        while not predicate():
            root.update()
            if errors:
                raise AssertionError(errors)
            if getattr(app, '_shared_last_error', None):
                raise AssertionError(app._shared_last_error)
            if time.perf_counter() > deadline:
                raise TimeoutError('Tk preview operation timed out')
            time.sleep(.005)
    try:
        assert app._load_video(str(ROOT/'tmp/hdr-e2e/source-sdr.mp4'))
        pump(lambda: bool(paints))
        assert app._shared_session.settings['super_resolution_scale'] == 2
        assert app._shared_session.metadata['width'] == 640
        app.step_frame(1)
        pump(lambda: paints[-1] == 1)
        assert app._frame == 0, 'step must expose generated subframe, not next source'
        app.jump_frame(5)
        pump(lambda: paints[-1] == 10)
        app.play()
        playback_start = len(paints)
        pump(lambda: not app.playing)
        playback = paints[playback_start:]
        assert playback and playback[-1] == 47 and playback == sorted(playback), playback
        # The seek intentionally left unrendered frames 2..9. Preview the whole
        # clip before asserting a completely warm (rather than partial) export.
        app.jump_frame(0)
        app.play()
        pump(lambda: not app.playing)
        session = app._shared_session
        app._export_settings['v_video_bitrate'].set('40')
        app._on_export_settings_change()
        pump(lambda: getattr(app, '_shared_session', None) is session)
        assert app._shared_ready() is session, 'encoding-only change lost cache'
        export_reports = []
        original_export = app._export_frame_generated_video
        def export(*args):
            report = original_export(*args)
            export_reports.append(report)
            return report
        app._export_frame_generated_video = export
        output = task/'ui-export-verified.mp4'
        assert not output.exists(), 'verification output already exists'
        result = app._export_video_source(app.video, app._collect_settings(),
            app._collect_export_settings(), app._video_color_info, out_path=str(output), notify=False)
        assert result['success'], result
        assert export_reports[-1]['cache_hits'] == 48, export_reports[-1]
        assert export_reports[-1]['new_frames'] == 0, export_reports[-1]
        base = app._shared_manager().base
        before = base.new_frames
        app._export_settings['v_frame_generation'].set('3×')
        app._on_export_settings_change()
        pump(lambda: getattr(app, '_shared_session', None) is not session)
        assert app._shared_manager().base is base
        app.jump_frame(0)
        pump(lambda: app._shared_session.multiplier == 3 and paints[-1] == 0)
        assert base.new_frames == before, 'multiplier change recomputed SR/NR'
        cached = app._shared_session
        app.clear_preview_cache()
        pump(lambda: not app._clear_preview_pending)
        assert cached.closed
        pump(lambda: getattr(app, '_shared_session', None) is not None and not app._shared_session.closed)
        report = {'status': 'passed', 'painted_output_indices': paints,
                  'step_generated_frame': True, 'seek': True, 'clock_ordered_playback': True,
                  'encoding_preserves_cache': True, 'multiplier_preserves_upstream': True,
                  'clear_retires_worker': True, 'tk_callback_errors': errors}
        report['main_export_cache_hits'] = export_reports[-1]['cache_hits']
        report['main_export_new_frames'] = export_reports[-1]['new_frames']
        (task/'ui-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in report.items() if k != 'painted_output_indices'}), flush=True)
    finally:
        app._shared_closing = True
        app.pause()
        app._shared_retire()
        pending = getattr(app, '_shared_retiring', None)
        if pending:
            pump(lambda: not pending.is_alive(), seconds=140)
        app._on_close()


if __name__ == '__main__':
    main()
