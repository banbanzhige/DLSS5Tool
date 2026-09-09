"""Opt-in real component / hidden Tk readiness smoke; no user media or settings writes."""
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix='guidance-activation-') as directory:
        os.environ['DLSS5TOOL_SETTINGS_PATH'] = str(Path(directory) / 'settings.json')
        os.environ['DLSS5TOOL_QUEUE_PATH'] = str(Path(directory) / 'queue.json')
        import tkinter as tk
        import app_settings
        import gui
        app_settings.save({**app_settings.DEFAULTS, 'guidance_mode': 3})
        root = tk.Tk()
        root.withdraw()
        app = gui.App(root)
        errors = []
        root.report_callback_exception = lambda *error: errors.append(str(error[1]))
        records = []
        try:
            assert app._collect_host_settings()['guidance_mode'] == 0
            for mode in (1, 2, 3, 0):
                selected = app._host_settings['v_guidance']
                selected.set(gui.tr('guidance.mode.' + str(mode)))
                start = last = time.monotonic()
                app._on_mod_settings_change()
                callback_ms = (time.monotonic() - start) * 1000
                assert app._collect_host_settings()['guidance_mode'] == 0
                max_tick_gap = 0
                while app._module_reload_thread is not None:
                    root.update()
                    now = time.monotonic()
                    max_tick_gap = max(max_tick_gap, now - last)
                    last = now
                    if now - start > 300:
                        raise RuntimeError('activation timeout')
                    time.sleep(0.01)
                assert not errors, errors
                assert app._collect_host_settings()['guidance_mode'] == mode, app._guidance_preflight_error
                assert str(app._host_settings['w_guidance'].cget('state')) == 'readonly'
                record = dict(mode=mode, elapsed_seconds=round(time.monotonic() - start, 3),
                              callback_ms=round(callback_ms, 2), max_tick_gap_ms=round(max_tick_gap * 1000, 2),
                              status=app._host_settings['w_guidance_status'].cget('text'),
                              info=app._guidance_preflight_info)
                records.append(record)
                print(json.dumps(record, ensure_ascii=False), flush=True)
            assert max(r['max_tick_gap_ms'] for r in records) < 2000
            print('PASS: startup off, all three modes checked without media, off needs no models', flush=True)
        finally:
            while app._module_reload_thread is not None:
                root.update()
                time.sleep(0.02)
            app._on_close()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
