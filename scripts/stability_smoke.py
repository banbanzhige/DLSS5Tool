"""Opt-in stability smoke: real native/RAFT inference or an isolated QA window.

Uses synthetic media and temporary settings, never the user's saved session.
"""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', action='store_true')
    parser.add_argument('--show-ui', action='store_true')
    parser.add_argument('--theme', choices=('light', 'dark'), default='light')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    import tkinter as tk
    import numpy as np
    from dlss5tool import gui, app_settings

    with tempfile.TemporaryDirectory(prefix='dlss-stability-') as directory:
        os.environ['DLSS5TOOL_SETTINGS_PATH'] = str(Path(directory) / 'settings.json')
        os.environ['DLSS5TOOL_QUEUE_PATH'] = str(Path(directory) / 'queue.json')
        app_settings.save({'ui_language': 'zh_CN', 'guidance_mode': 0, 'preview_cache_mb': 256})
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
        root.withdraw()
        app = gui.App(root)
        errors, ticks = [], []
        root.report_callback_exception = lambda *error: errors.append(str(error))
        report = {}

        def tick():
            ticks.append(time.perf_counter())
            root.after(20, tick)

        def wait_for(predicate, timeout=90):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                root.update()
                if errors:
                    raise AssertionError(errors)
                if predicate():
                    return
                time.sleep(0.005)
            raise TimeoutError(str(getattr(app, '_live_error', '')))

        try:
            if args.gpu:
                app.video = 'stability-synthetic.png'
                app._source_kind = 'image'
                app._frame, app.nframes, app.fps = 0, 1, 24
                app._media_w, app._media_h = 640, 360
                app._video_color_info = {'is_hdr': False}
                app._image_bgr = np.zeros((360, 640, 3), np.uint8)
                app._image_bgr[..., 0] = np.arange(640, dtype=np.uint16)[None, :] % 256
                app._image_bgr[..., 1] = np.arange(360, dtype=np.uint16)[:, None] % 256
                root.after(20, tick)
                started = time.perf_counter()
                app.view_var.set('dlss')
                app.on_view_change()
                wait_for(lambda: app._cached_dlss(0) is not None)
                report['still_preview_seconds'] = round(time.perf_counter() - started, 3)
                wait_for(lambda: not app._play_dlss_busy)
                app.workspace_tabs.select(app._guidance_page)
                app._guidance_view = 'flow'
                app.preview_selector.set('flow')
                app._host_settings['v_guidance'].set(gui.tr('guidance.mode.1'))
                started = time.perf_counter()
                app._on_mod_settings_change()
                wait_for(lambda: app._module_reload_thread is None)
                assert not app._guidance_preflight_error, app._guidance_preflight_error
                assert app._guidance_result is not None
                report['warm_activation_seconds'] = round(time.perf_counter() - started, 3)
                session = app._live
                native_session = session._session
                app._guidance_preview_epoch += 1
                app._guidance_result = None
                app.display_view()
                wait_for(lambda: app._guidance_result is not None)
                assert app._live is session and app._live._session is native_session
                assert not app._guidance_result[-1], app._guidance_result[-1]
                report['warm_session_reused'] = True
                report['ui_ticks'] = len(ticks)
                report['max_ui_tick_gap_ms'] = round(max((b - a for a, b in zip(ticks, ticks[1:])), default=0) * 1000, 1)
                report['backend'] = session.backend
                report['model'] = session.guidance_info
                print(json.dumps(report, ensure_ascii=False, default=str), flush=True)

            if args.show_ui:
                app.video = None
                app.workspace_tabs.select(app._guidance_page)
                root.geometry('1200x820+40+40')
                root.title('DLSS5Tool — Stability QA')
                app._apply_ui_theme(args.theme, persist=False)
                for section in (app._guidance_flow_advanced, app._guidance_display_section):
                    if section.collapsed:
                        section.toggle()
                root.deiconify()
                root.lift()
                root.protocol('WM_DELETE_WINDOW', root.quit)
                root.after(180000, root.quit)
                root.mainloop()
            assert not errors, errors
            (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        finally:
            app.pause()
            app._wait_play_dlss(timeout=5)
            if app._module_reload_thread is not None:
                app._module_reload_thread.join(5)
            app._close_live()
            app._close_super_resolution()
            if getattr(app, '_shared_cache_pool', None):
                app._shared_cache_pool.close()
            for handle in root.tk.call('after', 'info'):
                root.after_cancel(handle)
            root.destroy()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
