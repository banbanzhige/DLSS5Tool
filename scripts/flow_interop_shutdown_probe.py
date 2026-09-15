"""No-Torch/no-CUDA/no-interop shutdown control in an isolated process."""
import argparse
import ctypes as C
import json
import os
from pathlib import Path
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--host',type=Path,required=True)
    p.add_argument('--label',required=True)
    a=p.parse_args()
    a.work=a.work.resolve()
    if not (a.work/'TASK.md').is_file() or not a.label.replace('-','').isalnum():
        p.error('Register task and use an alphanumeric/dash label')
    result=a.work/(a.label+'.json')
    if result.exists():
        p.error('Refusing to overwrite report')
    os.environ['TMP']=os.environ['TEMP']=str(a.work)
    import numpy as np
    from dlss5tool import dlss_engine as e
    e.HOST_DLL_V2=str(a.host.resolve())
    e.LOG_PATH=str(a.work/(a.label+'-ngx.log'))
    live=e.Live(512,512,dict(host_backend='v2',host_zero_fast_path=False,host_auto_fallback=False,
                           guidance_mode=0,host_submission='merged',host_in_flight=1))
    report=dict(host=str(a.host.resolve()),torch_imported='torch' in sys.modules,
                interop=False,stage='process')
    def save(): result.write_text(json.dumps(report,indent=2),encoding='utf-8')
    def timeout():
        report.update(status='shutdown_timeout',stage='NGX shutdown watchdog timeout')
        save()
        print('No-Torch control shutdown timed out',flush=True)
        os._exit(3)
    rgba=np.zeros((512,512,4),np.uint8)
    rgba[...,3]=255
    for i in range(3):
        if live.process(rgba,reset=i==0) is None: raise RuntimeError('Render failed')
    report['stage']='shutdown'
    save()
    watchdog=threading.Timer(20,timeout)
    watchdog.daemon=True
    watchdog.start()
    print('No Torch, no external memory; closing DLSS',flush=True)
    if hasattr(live._lib,'probe_shutdown_diagnostic'):
        live._lib.probe_shutdown_diagnostic.argtypes=[]
        live._lib.probe_shutdown_diagnostic.restype=None
        live._lib.probe_shutdown_diagnostic()
    else:
        live.close()
    watchdog.cancel()
    report.update(stage='closed',status='completed')
    save()
    print(json.dumps(report),flush=True)

if __name__=='__main__': main()
