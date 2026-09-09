"""Opt-in real GPU/Tk smoke test; isolated settings, no source/output overwrites."""
import argparse
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--execution', action='store_true', help='Switch execution instead of depth precision')
    parser.add_argument('--component', type=Path)
    args = parser.parse_args()
    if not Path(args.source).is_file():
        parser.error('source must be an existing video')
    finished = threading.Event()

    def watchdog():
        if not finished.wait(150):
            print('FAIL: probe exceeded 150 seconds', flush=True)
            os._exit(2)  # model workers watch this test process, not the user app

    threading.Thread(target=watchdog, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='dlss-module-reload-') as directory:
        os.environ['DLSS5TOOL_SETTINGS_PATH'] = str(Path(directory) / 'settings.json')
        os.environ['DLSS5TOOL_QUEUE_PATH'] = str(Path(directory) / 'queue.json')
        import tkinter as tk
        import app_settings
        app_settings.save({**app_settings.DEFAULTS, 'preview_view': 'compare',
                           'mods_directory': str(args.component.resolve().parent) if args.component else '',
                           'guidance_flow_weights': str(Path(__file__).resolve().parents[1] / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
                           'guidance_depth_weights': str(Path(__file__).resolve().parents[1] / 'mods/models/depth_anything_v2_vitl.pth'),
                           'guidance_mode': 3, 'guidance_depth_encoder': 'vitl',
                           'guidance_depth_profile': 'fp32', 'guidance_edge': 720,
                           'preview_cache_mb': 256, 'preview_detached': False})
        import gui
        root = tk.Tk()
        root.withdraw()
        app = gui.App(root)
        # The audio track is unrelated to this smoke test.
        app._audio.prepare = lambda *a, **kw: None
        errors = []
        variable_key = 'v_guidance_execution' if args.execution else 'v_depth_profile'
        info_key = 'execution' if args.execution else 'depth_profile'
        accelerated = 'raft_streams' if args.execution else 'sdpa_fp16'
        original = 'serial' if args.execution else 'fp32'
        if args.execution:
            app._host_settings['v_depth_profile'].set(gui.tr('guidance.option.sdpa_fp16'))
        root.report_callback_exception = lambda *error: errors.append(str(error[1]))
        started = time.monotonic()
        last_tick = started
        max_gap = 0.0
        stage = 0
        switch_times = []

        def tick():
            nonlocal last_tick, max_gap, stage
            now = time.monotonic()
            max_gap = max(max_gap, now - last_tick)
            last_tick = now
            info = getattr(app, '_last_guidance_info', None) or {}
            thread = app._play_dlss_thread
            if errors or app._preview_worker_error or now - started > 120:
                errors.append(app._preview_worker_error or 'callback error / 120s timeout')
                root.quit()
                return
            profile = None
            if stage == 0 and thread is not None and thread.is_alive():
                # Deliberately switch while the first FP32 frame/model loads.
                profile = accelerated
            elif stage == 1 and info.get(info_key) == accelerated:
                print('READY accelerated:', info, flush=True)
                profile = original
            elif stage == 2 and info.get(info_key) == original:
                print('READY restored:', info, flush=True)
                root.quit()
                return
            if profile:
                app._host_settings[variable_key].set(gui.tr('guidance.option.' + profile))
                before = time.monotonic()
                app._on_mod_settings_change()
                elapsed = time.monotonic() - before
                switch_times.append(elapsed)
                print('SWITCH', profile, 'callback_seconds', round(elapsed, 3), flush=True)
                stage += 1
            root.after(20, tick)

        try:
            if not app._load_media(args.source):
                raise RuntimeError('source load failed')
            # The viewport may have a minimal size when withdrawn; precise
            # paused preview still uses the original video dimensions.
            last_tick = time.monotonic()
            root.after(20, tick)
            root.mainloop()
            print('RESULT', {'stage': stage, 'max_tick_gap_seconds': round(max_gap, 3),
                             'switch_seconds': switch_times, 'errors': errors}, flush=True)
            if stage != 2 or errors or max_gap > 2 or any(t > 0.5 for t in switch_times):
                raise RuntimeError('module reload smoke test failed')
        finally:
            app._freeze_preview_cache(resume_ms=None)
            # Keep the Tk loop pumping while the reload worker finishes.
            while app._module_reload_thread is not None:
                root.update()
                time.sleep(0.02)
            app._on_close()
            finished.set()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
