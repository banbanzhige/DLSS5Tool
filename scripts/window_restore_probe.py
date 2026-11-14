"""Exercise an isolated GUI's restore lifecycle and optional settled snapshots.

For independent-process sampling of intermediate screen frames, use
trace_window_restore.py --mode current --screen instead.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import stat
import sys
import time

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))


def probe_output(parser, requested):
    path = Path(os.path.abspath(requested))
    for candidate in (path, *path.parents):
        if candidate.exists() or candidate.is_symlink():
            info = candidate.lstat()
            if candidate.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                parser.error(f'Reparse point in output path: {candidate}')
    path = path.resolve()
    temporary_root = (REPOSITORY / 'tmp').resolve()
    if not path.is_relative_to(temporary_root) or path == temporary_root:
        parser.error('Use a new output directory inside a registered tmp/<task>-<date>/ task')
    path.mkdir(parents=True, exist_ok=False)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=3, help='Restore cycles per theme')
    parser.add_argument('--capture', action='store_true', help='Save settled HWND snapshots')
    parser.add_argument('--no-backing-store', action='store_true', help='Baseline comparison only')
    parser.add_argument('--native-minimize', action='store_true')
    parser.add_argument('--maximized', action='store_true')
    parser.add_argument('--synthetic-video', action='store_true',
                        help='Verify original-video playback continues across restores without GPU inference')
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error('--cycles must be positive')
    if args.native_minimize and sys.platform != 'win32':
        parser.error('--native-minimize requires Windows')
    output = probe_output(parser, args.output)
    os.environ['DLSS5TOOL_SETTINGS_PATH'] = str(output / 'settings.json')
    os.environ['DLSS5TOOL_QUEUE_PATH'] = str(output / 'queue.json')
    os.environ['DLSS5TOOL_DISABLE_UPDATE_CHECK'] = '1'

    import tkinter as tk
    from PIL import ImageGrab
    from dlss5tool import app_settings, ui_theme
    from dlss5tool.gui import App, TkinterDnD

    ui_theme.enable_dpi_awareness()
    app_settings.save({'ui_theme': 'light', 'inspector_width': 380})
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    root.withdraw()
    errors, capture_errors, timings = [], [], []
    root.report_callback_exception = lambda *error: errors.append(str(error))
    App._start_render_gpu_scan = lambda self: None
    app = App(root)
    store = app._window_backing_store
    if args.no_backing_store:
        store.close()
    root.title(root.title() + ' [Restore probe]')
    root.geometry('1280x840+20+20')
    root.deiconify()
    if args.maximized:
        root.state('zoomed')
    playback = []
    case = [0]

    def load_synthetic_video():
        import cv2
        import numpy as np
        path = output / 'moving-black-background.avi'
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 30, (320, 180))
        if not writer.isOpened():
            raise RuntimeError('Synthetic video writer did not open')
        try:
            for frame in range(600):
                image = np.zeros((180, 320, 3), dtype=np.uint8)
                x = frame % 260
                image[50:110, x:x+60] = (40, 190, 240)
                writer.write(image)
        finally:
            writer.release()
        # Only this owned probe disables inference and audio preparation.
        app._schedule_preview_cache_resume = lambda *args: None
        app._audio.prepare = lambda *args, **kwargs: None
        app._audio.set_muted(True)
        app.view_var.set('original')
        if not app._load_media(str(path)):
            raise RuntimeError('Synthetic video did not load')
        app.play()

    def advance():
        if case[0] >= args.cycles * 2:
            finish()
            return
        app._apply_ui_theme('light' if case[0] < args.cycles else 'dark', persist=False)
        root.after(250, minimize)

    def minimize():
        root._probe_frame_before = app._frame
        if args.native_minimize:
            post = ctypes.windll.user32.PostMessageW
            post.argtypes, post.restype = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_int
            post(ui_theme._window_hwnd(root), 0x112, 0xF020, 0)
        else:
            root.iconify()
        root.after(120, restore)

    def restore():
        index = case[0]
        theme = 'light' if index < args.cycles else 'dark'
        started = time.perf_counter()
        root.deiconify()
        root.focus_force()
        root.after_idle(lambda: timings.append({
            'cycle': index, 'theme': theme,
            'idle_ms': round((time.perf_counter()-started)*1000, 2)}))
        case[0] += 1
        root.after(350, lambda: settled(index, theme))

    def settled(index, theme):
        if args.synthetic_video:
            playback.append({'cycle': index, 'before': root._probe_frame_before,
                             'after': app._frame, 'playing': app.playing})
        if args.capture:
            try:
                ImageGrab.grab(window=ui_theme._window_hwnd(root)).save(output / f'{theme}-{index}.png')
            except OSError as error:
                capture_errors.append(str(error))
        advance()

    def finish():
        report = {'tk': root.tk.call('package', 'require', 'Tk'),
                  'callback_errors': errors, 'capture_errors': capture_errors, 'idle': timings,
                  'playback': playback,
                  'backing': {'enabled': not args.no_backing_store, 'captures': store.captures,
                              'replays': store.replays, 'rejected': store.rejected,
                              'error': store.error, 'last_capture_ms': store.last_capture_ms},
                  'limitation': 'Idle readiness and settled HWND snapshots do not verify transient screen frames.'}
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
        app._on_close()

    if args.synthetic_video:
        root.after(100, load_synthetic_video)
    root.after(1000 if args.synthetic_video else 700, advance)
    root.mainloop()
    if errors or capture_errors or store.error:
        raise SystemExit('UI or native restore callbacks failed')
    if any(not item['playing'] or item['after'] <= item['before'] for item in playback):
        raise SystemExit('Playback failed to advance across restore')


if __name__ == '__main__':
    main()
