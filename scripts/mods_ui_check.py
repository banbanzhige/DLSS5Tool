"""Capture only our disposable test app, without reading user settings/media."""
import ctypes
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tkinter as tk
from PIL import ImageGrab
import app_settings
import ui_theme


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--language', choices=['zh_CN', 'en_US'], default='zh_CN')
    parser.add_argument('--component-build', choices=['cuda', 'cpu'])
    parser.add_argument('--output', type=Path, default=ROOT / 'output/mods-ui')
    parser.add_argument('--depth-profile', choices=['fp32', 'sdpa_fp16'], default='fp32')
    parser.add_argument('--execution', choices=['serial', 'raft_streams'], default='serial')
    args = parser.parse_args()
    os.environ['DLSS5TOOL_LANG'] = args.language
    from gui import App, tr, TkinterDnD
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        os.environ['DLSS5TOOL_SETTINGS_PATH'] = str(Path(directory) / 'settings.json')
        os.environ['DLSS5TOOL_QUEUE_PATH'] = str(Path(directory) / 'queue.json')
        import mod_paths
        # Isolate discovery now that the development workspace contains real mods.
        mod_paths.mods_root = lambda settings=None: Path(directory) / 'mods'
        mod_paths.search_roots = lambda settings=None: [Path(directory) / 'mods']
        mod_paths.enhancement_path = lambda settings=None: Path(directory) / 'mods/enhancement'
        if args.component_build:
            fixture = Path(directory) / 'mods/enhancement'
            (fixture / '_internal').mkdir(parents=True)
            (fixture / 'guidance_worker.exe').touch()  # UI fixture, never executed
            (fixture / '_internal/python313.dll').touch()
            (fixture / 'enhancement.json').write_text(json.dumps({
                'id': 'dlss5-guidance', 'protocol': 1,
                'architectures': ['raft_large', 'depth_anything_v2'], 'build': args.component_build}))
        app_settings.save({'ui_preview_open': False, 'ui_export_open': False, 'ui_host_open': False,
                           'guidance_depth_profile': args.depth_profile, 'guidance_execution': args.execution})
        ui_theme.enable_dpi_awareness()
        root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        root.withdraw()
        app = App(root)
        root.geometry('1400x1000+0+0')
        app.workspace_tabs.select(app._guidance_page)
        errors = []
        root.report_callback_exception = lambda *error: errors.append(str(error))
        cases = iter([(theme, page) for theme in ('light', 'dark') for page in ('adjust', 'guidance', 'summary', 'missing', 'paths', 'inference')])
        ancestor = ctypes.windll.user32.GetAncestor
        ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        ancestor.restype = ctypes.c_void_p
        def advance():
            try:
                theme, page = next(cases)
            except StopIteration:
                root.destroy()
                print('callback errors:', errors)
                return
            app._apply_ui_theme(theme, persist=False)
            mode = 1 if page == 'missing' else 3
            app._host_settings['v_guidance'].set(tr('guidance.mode.' + str(mode)))
            app._on_mod_settings_change()
            app._refresh_mods()
            app.workspace_tabs.select(app._preview_page if page == 'adjust' else app._guidance_page)
            want_collapsed = page in ('adjust', 'guidance', 'inference')
            if app._modules_section.collapsed != want_collapsed:
                app._modules_section.toggle()
            for section, wanted in ((app._module_editor, page in ('paths', 'guidance')), (app._guidance_advanced, page == 'inference')):
                if section.collapsed == wanted:
                    section.toggle()
            root.after(200, lambda: capture(theme, page))
        def capture(theme, page):
            app._guidance_canvas.yview_moveto(1.0 if page in ('inference', 'paths', 'summary') else 0.0)
            root.after(300, lambda: save_capture(theme, page))
        def save_capture(theme, page):
            hwnd = ancestor(root.winfo_id(), 2) or root.winfo_id()
            path = output / f'{args.language}-{theme}-{page}.png'
            ImageGrab.grab(window=hwnd).save(path)
            print(path, flush=True)
            root.after(20, advance)
        root.deiconify()
        root.after(200, advance)
        root.mainloop()
        if errors:
            raise RuntimeError(errors)


if __name__ == '__main__':
    main()
