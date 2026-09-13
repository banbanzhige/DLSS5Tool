"""Native upload A/B without model inference. Fresh process per DLL/case.

Checks real Feature18 outputs, inactive/null maps, padding, persistent/transient
and compatibility submissions and three-slot ownership. Never replaces DLLs.
"""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(args):
    import cv2
    import numpy as np
    from dlss5tool import dlss_engine as engine
    directory = args.output
    directory.mkdir(parents=True, exist_ok=False)
    engine.HOST_DLL_V2 = str(args.dll.resolve())
    engine.LOG_PATH = str(directory / 'ngx.log')
    settings = {'host_backend': 'v2', 'host_auto_fallback': False,
        'host_submission': 'compatibility' if args.case == 'compatibility' else 'merged',
        'host_persistent_buffers': args.case != 'transient',
        'host_zero_fast_path': args.case == 'zero_fast', 'host_in_flight': 3 if args.case in ('async', 'flow_async') else 1,
        'guidance_mode': 0, 'style': 0, 'intensity': 1.0}
    w, h = (641, 359) if args.case == 'odd' else (1440, 1440)
    cap = cv2.VideoCapture(str(args.source))
    frames = []
    try:
        for _ in range(16):
            ok, frame = cap.read()
            if not ok: raise RuntimeError('Short input')
            frames.append(cv2.cvtColor(cv2.resize(frame, (w, h)), cv2.COLOR_BGR2RGBA))
    finally:
        cap.release()
    live = engine.Live(w, h, settings)
    lib = live._lib
    mv = np.empty((h,w,2), np.float32)
    dp = np.empty((h,w), np.float32)
    output = np.empty_like(frames[0])
    hashes, times, pending = [], [], 0
    modes = [3,3,3,1,2,0,3,3,3,1,2,3,3,3,3,3]
    def collect():
        if not lib.dlssnr_dequeue(ctypes.c_void_p(output.ctypes.data)):
            raise RuntimeError('dequeue failed')
        hashes.append(hashlib.sha256(memoryview(output)).hexdigest())
    started = time.perf_counter()
    for index, rgba in enumerate(frames):
        mode = 0 if args.case.startswith('zero') else 1 if args.case.startswith('flow') else modes[index]
        engine._set_options(lib, {**settings, 'guidance_mode': mode})
        mv[...,0] = (index - 6) * .125
        mv[...,1] = (index + 1) * -.1875
        dp[:] = np.linspace(0, 1, w, dtype=np.float32)[None,:]
        # Same buffers are overwritten every frame, including while 3 slots run.
        ptr_mv = None if index == 6 else ctypes.c_void_p(mv.ctypes.data)
        ptr_dp = None if index == 7 else ctypes.c_void_p(dp.ctypes.data)
        tick = time.perf_counter()
        if args.case in ('async', 'flow_async'):
            if not lib.dlssnr_enqueue(ctypes.c_void_p(rgba.ctypes.data), ptr_mv, ptr_dp, int(index in (0,8))):
                raise RuntimeError('enqueue failed')
            pending += 1
            if pending >= 3:
                collect(); pending -= 1
        else:
            if not lib.dlssnr_process(ctypes.c_void_p(rgba.ctypes.data), ptr_mv, ptr_dp,
                                     ctypes.c_void_p(output.ctypes.data), int(index in (0,8))):
                raise RuntimeError('process failed')
            hashes.append(hashlib.sha256(memoryview(output)).hexdigest())
        times.append((time.perf_counter()-tick)*1000)
    while pending:
        collect(); pending -= 1
    result = {'case':args.case, 'dll':str(args.dll.resolve()),'dimensions':[w,h],
        'total_seconds_including_hash':time.perf_counter()-started, 'frame_ms_including_hash':times,
        'hashes':hashes}
    (directory/'result.json').write_text(json.dumps(result,indent=2))
    # Exit reclaims NGX; production also isolates DLL lifetime by process.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old',type=Path)
    parser.add_argument('--new',type=Path)
    parser.add_argument('--dll',type=Path)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--case')
    args=parser.parse_args()
    if args.dll:
        run(args); return
    args.output.mkdir(parents=True,exist_ok=False)
    report=[]
    for case in ('zero_fast','zero_upload','flow','flow_async','mixed','async','odd','transient','compatibility'):
        results=[]
        for label,dll in (('old',args.old),('new',args.new)):
            directory=args.output/f'{case}-{label}'
            subprocess.run([sys.executable,__file__,'--dll',str(dll.resolve()),'--source',str(args.source),
                '--output',str(directory),'--case',case],check=True,timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW)
            results.append(json.loads((directory/'result.json').read_text()))
        record={'case':case,'equal':results[0]['hashes']==results[1]['hashes'],
                'old_seconds':results[0]['total_seconds_including_hash'],
                'new_seconds':results[1]['total_seconds_including_hash']}
        report.append(record)
        (args.output/'report.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(record),flush=True)
        if not record['equal']: raise RuntimeError('Native output mismatch')


if __name__=='__main__': main()
