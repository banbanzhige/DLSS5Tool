"""Disposable native inspector screenshots; never opens user media/settings."""
import ctypes
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    language = sys.argv[1] if len(sys.argv) > 1 else 'zh_CN'
    os.environ['DLSS5TOOL_LANG'] = language
    from PIL import ImageGrab
    import tkinter as tk
    from dlss5tool import app_settings
    from dlss5tool import ui_theme
    with tempfile.TemporaryDirectory() as temporary:
        os.environ['DLSS5TOOL_SETTINGS_PATH'] = str(Path(temporary) / 'settings.json')
        os.environ['DLSS5TOOL_QUEUE_PATH'] = str(Path(temporary) / 'queue.json')
        app_settings.save({'guidance_mode': 3, 'guidance_device': 'cuda', 'inspector_width': 360})
        from dlss5tool.gui import App, TkinterDnD
        ui_theme.enable_dpi_awareness()
        root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        root.withdraw()
        app = App(root)
        root.geometry('1200x880+0+0')
        app.workspace_tabs.select(app._guidance_page)
        errors = []
        root.report_callback_exception = lambda *e: errors.append(str(e))
        output = ROOT / 'output/guidance-parameters-ui'
        output.mkdir(parents=True, exist_ok=True)
        cases = iter((theme, width, page) for theme in ('light', 'dark')
                     for width in (320, 480) for page in ('main', 'display', 'settings', 'device', 'modules'))
        ancestor = ctypes.windll.user32.GetAncestor
        ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        ancestor.restype = ctypes.c_void_p

        def advance():
            try:
                theme, width, page = next(cases)
            except StopIteration:
                for handle in root.tk.splitlist(root.tk.call('after', 'info')):
                    root.after_cancel(handle)
                root.destroy()
                return
            app._apply_ui_theme(theme, persist=False)
            app._inspector.configure(width=width)
            app.workspace_tabs.select(app._guidance_page if page in ('main', 'display') else app._export_page)
            for section in (app._guidance_display_section, app._guidance_depth_advanced):
                if section.collapsed != (page != 'display'):
                    section.toggle()
            for section in (app._export_section, app._preview_section, app._host_section,
                            app._guidance_advanced, app._modules_section, app._module_editor):
                opened = ((page == 'device' and section is app._guidance_advanced) or
                          (page == 'modules' and section is app._modules_section))
                if section.collapsed == opened:
                    section.toggle()
            root.after(200, lambda: position(theme, width, page))

        def position(theme, width, page):
            if page in ('main', 'display'):
                app._guidance_canvas.yview_moveto(0 if page == 'main' else 0.55)
            else:
                app._export_canvas.yview_moveto(0 if page == 'settings' else 0.35)
            root.after(200, lambda: capture(theme, width, page))

        def capture(theme, width, page):
            hwnd = ancestor(root.winfo_id(), 2) or root.winfo_id()
            path = output / f'{language}-{theme}-{width}-{page}.png'
            ImageGrab.grab(window=hwnd).save(path)
            print(path, flush=True)
            root.after(30, advance)

        root.deiconify()
        root.after(200, advance)
        root.mainloop()
        if errors:
            raise RuntimeError(errors)


if __name__ == '__main__':
    main()
