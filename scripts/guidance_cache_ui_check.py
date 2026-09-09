"""Capture only a disposable cache UI, using isolated settings and no media."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    p=argparse.ArgumentParser();p.add_argument('--language',default='zh_CN');p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    with tempfile.TemporaryDirectory() as directory:
        os.environ['DLSS5TOOL_SETTINGS_PATH']=str(Path(directory)/'settings.json')
        os.environ['DLSS5TOOL_QUEUE_PATH']=str(Path(directory)/'queue.json')
        os.environ['DLSS5TOOL_LANG']=args.language
        from dlss5tool import app_settings
        (Path(directory)/'settings.json').write_text(json.dumps({'ui_language':args.language,'preview_cache_mb':8192}),encoding='utf-8')
        from dlss5tool.gui import App,TkinterDnD
        from PIL import ImageGrab
        root=TkinterDnD.Tk();root.withdraw();app=App(root)
        errors=[];root.report_callback_exception=lambda *error:errors.append(str(error))
        root.geometry('1240x900+40+40')
        app.workspace_tabs.select(app._export_page)
        if app._preview_section.collapsed:app._preview_section.toggle()
        app._update_preview_memory_hint()
        assert app._collect_settings()['guidance_cache_pool']
        assert app._preview_cache_bytes()==8192*1048576
        ancestor=ctypes.windll.user32.GetAncestor
        ancestor.argtypes=[ctypes.c_void_p,ctypes.c_uint];ancestor.restype=ctypes.c_void_p
        themes=iter(['light','dark'])
        def advance():
            theme=next(themes,None)
            if theme is None:
                root.destroy();return
            app._apply_ui_theme(theme,persist=False)
            root.after(350,lambda:save(theme))
        def save(theme):
            hwnd=ancestor(root.winfo_id(),2) or root.winfo_id()
            ImageGrab.grab(window=hwnd).save(args.output/f'{theme}.png')
            root.after(30,advance)
        root.deiconify();root.after(200,advance);root.mainloop()
        if errors:raise RuntimeError(errors)
        app._shared_cache_pool.close()
        print('shared cache budget 8192 MiB; light/dark; no callbacks failed')


if __name__=='__main__':main()
