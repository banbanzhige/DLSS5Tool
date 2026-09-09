"""Capture disposable comparison UI with synthetic maps, no models/user media."""
import argparse
import ctypes
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--language', default='zh_CN', choices=('zh_CN', 'en_US'))
    args = parser.parse_args()
    os.environ['DLSS5TOOL_LANG'] = args.language
    import cv2
    import numpy as np
    import tkinter as tk
    from PIL import ImageGrab
    import app_settings
    import ui_theme
    from guidance_visualization import guidance_images
    with tempfile.TemporaryDirectory() as temporary:
        os.environ['DLSS5TOOL_SETTINGS_PATH'] = str(Path(temporary) / 'settings.json')
        os.environ['DLSS5TOOL_QUEUE_PATH'] = str(Path(temporary) / 'queue.json')
        app_settings.save({'guidance_mode': 3, 'ui_preview_open': False, 'ui_host_open': False,
                           'ui_export_open': False, 'guidance_preview_view': 'original'})
        from gui import App, TkinterDnD
        ui_theme.enable_dpi_awareness()
        root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        root.withdraw()
        app = App(root)
        root.geometry('1280x850+0+0')
        errors = []
        root.report_callback_exception = lambda *e: errors.append(str(e))
        y, x = np.mgrid[:480, :320]
        source = np.stack((x % 255, y % 255, (x + y) % 255), axis=-1).astype(np.uint8)
        cv2.rectangle(source, (70, 110), (250, 370), (100, 200, 30), -1)
        depth = ((x + y) / 800).astype(np.float32)
        motion = np.stack(((x - 160) / 8, (y - 240) / 12), axis=-1).astype(np.float32)
        maps = guidance_images(motion, depth, 3)
        app.video = 'synthetic-preview.png'
        app._source_kind = 'image'
        app._image_bgr = source
        app._media_w, app._media_h = 320, 480
        app.nframes, app.fps = 1, 24
        app._video_color_info = {'is_hdr': False}
        app._read_frame = lambda frame: source
        app._request_guidance_preview = lambda key: None
        app._live_dlss_image = lambda *a, **kw: cv2.convertScaleAbs(source, alpha=1.05, beta=5)
        app._update_action_labels()
        output = ROOT / 'output' / 'comparison-ui'
        output.mkdir(parents=True, exist_ok=True)
        cases = iter((theme, context, layout) for theme in ('light', 'dark')
                     for context in ('dlss', 'depth', 'flow') for layout in ('wipe', 'side'))
        ancestor = ctypes.windll.user32.GetAncestor
        ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        ancestor.restype = ctypes.c_void_p
        popup = None

        def advance():
            nonlocal popup
            if popup is not None:
                popup.unpost()
                popup.destroy()
                popup = None
            try:
                theme, context, layout = next(cases)
            except StopIteration:
                for handle in root.tk.splitlist(root.tk.call('after', 'info')):
                    root.after_cancel(handle)
                root.destroy()
                return
            app._apply_ui_theme(theme, persist=False)
            app.workspace_tabs.select(app._preview_page if context == 'dlss' else app._guidance_page)
            app.compare_layout.set(layout)
            app.preview_selector.set('compare')
            app.compare_target.set('flow' if context in ('flow', 'menu') else 'depth')
            app._guidance_result = (app._guidance_preview_key(), source, maps, False, '')
            app._on_preview_selection()
            if not app._module_editor.collapsed:
                app._module_editor.toggle()
            root.after(600, lambda: capture(theme, context, layout))

        def capture(theme, context, layout):
            nonlocal popup
            if context == 'menu':
                popup = app._build_comparison_menu(app.view_bar)
                popup.update_idletasks()
                # Windows native menus run a nested message loop while posted.
                # Schedule capture/dismissal before posting, not after it returns.
                root.after(200, lambda: save(theme, context, layout))
                root.after(450, popup.unpost)
                popup.post(app.view_bar.winfo_rootx(), app.view_bar.winfo_rooty() - popup.winfo_reqheight())
            else:
                root.after(200, lambda: save(theme, context, layout))

        def save(theme, context, layout):
            path = output / f'{args.language}-{theme}-{context}-{layout}.png'
            hwnd = ancestor(root.winfo_id(), 2) or root.winfo_id()
            ImageGrab.grab(window=hwnd).save(path)
            print(path, flush=True)
            root.after(50, advance)

        root.deiconify()
        root.after(500, advance)
        root.mainloop()
        print('callback errors:', errors)
        if errors:
            raise RuntimeError(errors)


if __name__ == '__main__':
    main()
