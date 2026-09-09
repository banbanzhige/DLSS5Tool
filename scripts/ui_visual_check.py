"""Capture this test application's windows only; never read the user's media/settings.

Run on Windows: python scripts/ui_visual_check.py --scale 1.5
Outputs are ignored under output/ui-polish/. Pillow 11.2+ required for HWND capture.
"""
import argparse
import ctypes
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tkinter as tk
from PIL import ImageGrab
import app_settings
import ui_theme
from gui import App, TkinterDnD


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=float, nargs="+", default=[1.0])
    args = parser.parse_args()
    ui_theme.enable_dpi_awareness()
    output = Path(__file__).resolve().parents[1] / "output" / "ui-polish"
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    for scale in args.scale:
        with tempfile.TemporaryDirectory() as temporary:
            os.environ["DLSS5TOOL_SETTINGS_PATH"] = str(Path(temporary) / "settings.json")
            os.environ["DLSS5TOOL_QUEUE_PATH"] = str(Path(temporary) / "queue.json")
            app_settings.save({"ui_theme": "light", "inspector_width": 380})
            root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
            root.withdraw()
            root.tk.call("tk", "scaling", scale * 96 / 72)
            app = App(root)
            root.geometry("1400x900+0+0")
            root.report_callback_exception = lambda *error: errors.append(str(error))
            ancestor = ctypes.windll.user32.GetAncestor
            ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            ancestor.restype = ctypes.c_void_p
            cases = iter((
                ("light", app._preview_page, "adjust"),
                ("light", app._guidance_page, "guidance"),
                ("light", app._export_page, "export"),
                ("light", app.queue_tab, "queue"),
                ("dark", app._preview_page, "adjust"),
                ("dark", app._guidance_page, "guidance"),
            ))

            def advance(current_scale=scale):
                try:
                    theme, page, name = next(cases)
                except StopIteration:
                    print("font:", root._studio_fonts[ui_theme.UI_FONT].actual())
                    print("scale:", current_scale)
                    print("callback_errors:", errors)
                    for handle in root.tk.splitlist(root.tk.call('after', 'info')):
                        root.after_cancel(handle)
                    root.destroy()
                    return
                app._apply_ui_theme(theme, persist=False)
                app.workspace_tabs.select(page)
                root.after(500, lambda: capture(theme, name, current_scale))

            def capture(theme, name, current_scale):
                hwnd = ancestor(root.winfo_id(), 2) or root.winfo_id()
                path = output / f"{theme}-{name}-{current_scale:g}x.png"
                ImageGrab.grab(window=hwnd).save(path)
                print(path)
                root.after(20, advance)

            root.deiconify()
            root.after(250, advance)
            root.mainloop()
    if errors:
        raise SystemExit("UI callback failures")


if __name__ == "__main__":
    main()
