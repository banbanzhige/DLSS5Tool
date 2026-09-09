"""Exercise and capture only the developer test app's own window using synthetic paths."""
import argparse
import ctypes
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from unittest import mock

from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dlss5tool.amd_devtest_ui import DevTestWindow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=float, default=1)
    args = parser.parse_args()
    errors = []
    captures = Path(__file__).resolve().parents[1]/"output"/"amd-devtest-ui"
    captures.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="amd-ui-test-") as temporary:
        root = tk.Tk()
        root.withdraw()
        root.tk.call("tk", "scaling", args.scale*96/72)
        app = DevTestWindow(root, temporary)
        root.report_callback_exception = lambda *err: errors.append(str(err))
        ancestor = ctypes.windll.user32.GetAncestor
        ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]; ancestor.restype = ctypes.c_void_p
        def check():
            try:
                for widget in (app.run_btn, app.install_btn, app.export_btn, app.share_check, app.log, app.footer):
                    assert widget.winfo_ismapped(), str(widget)
                    bottom = widget.winfo_rooty()+widget.winfo_height()
                    right = widget.winfo_rootx()+widget.winfo_width()
                    assert bottom <= root.winfo_rooty()+root.winfo_height(), str(widget)
                    assert right <= root.winfo_rootx()+root.winfo_width(), str(widget)
                hwnd = ancestor(root.winfo_id(), 2) or root.winfo_id()
                path = captures/f"initial-{args.scale:g}x.png"
                ImageGrab.grab(window=hwnd).save(path)
                print(path)
                with mock.patch('dlss5tool.amd_devtest_ui.messagebox.showinfo') as info:
                    app.install()
                    assert info.called, "installer must require consent"
                app.set_busy(True)
                assert str(app.run_btn.cget("state")) == "disabled"
                app.stop()
                assert app.cancel.is_set()
                app.set_busy(False)
            except Exception as error:
                errors.append(str(error))
            finally:
                app.close()
        root.deiconify()
        root.after(700, check)
        root.mainloop()
    print("UI errors:", errors)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
