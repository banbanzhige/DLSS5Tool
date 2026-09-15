"""Real production Live + separate source guidance worker; bounded CPU/GPU A/B.

No media outputs, no settings writes, no NGX shutdown after Evaluate. Use a new
report label and a registered task directory; runtime defaults to candidate.dll.
"""
import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--label',required=True)
    p.add_argument('--frames',type=int,default=24)
    p.add_argument('--edge',type=int,default=512)
    p.add_argument('--queue',type=int,default=3)
    p.add_argument('--submission',choices=['merged','compatibility'],default='merged')
    p.add_argument('--cache-mb',type=int,default=0)
    p.add_argument('--direction',choices=['backward','forward_negated'],default='backward')
    p.add_argument('--host',type=Path)
    p.add_argument('--allow-fallback',action='store_true')
    p.add_argument('--preview-first',action='store_true')
    p.add_argument('--frozen-worker',action='store_true',help='Compatibility test with the currently installed old component')
    a=p.parse_args(); a.work=a.work.resolve()
    report_path=a.work/(a.label+'.json')
    if (not (a.work/'TASK.md').is_file() or report_path.exists()
            or not a.label.replace('-','').isalnum() or not 4<=a.frames<=64):
        p.error('Registered task, fresh safe label, frames 4..64 required')
    os.environ['TEMP']=os.environ['TMP']=str(a.work)
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    os.environ['DLSS5TOOL_GUIDANCE_PYTHON']=str(ROOT/'tmp/guidance-cuda-env/Scripts/python.exe')
    if a.frozen_worker:
        os.environ.pop('DLSS5TOOL_GUIDANCE_PYTHON',None)
    import cv2
    import numpy as np
    from dlss5tool import dlss_engine as e
    e.HOST_DLL_V2=str((a.host or a.work/'candidate.dll').resolve())
    e.LOG_PATH=str(a.work/(a.label+'-ngx.log'))
    cap=cv2.VideoCapture(str(a.source))
    for _ in range(80):
        if not cap.grab(): raise RuntimeError('Source too short')
    frames=[]
    for _ in range(a.frames):
        ok,bgr=cap.read()
        if not ok: raise RuntimeError('Source too short')
        frames.append(cv2.cvtColor(bgr,cv2.COLOR_BGR2RGBA))
    cap.release()
    h,w=frames[0].shape[:2]
    settings=dict(host_backend='v2',host_auto_fallback=False,host_zero_fast_path=False,
                  host_persistent_buffers=True,host_submission=a.submission,host_in_flight=a.queue,
                  guidance_mode=1,guidance_device='cuda',guidance_flow_backend='raft',
                  guidance_flow_edge=a.edge,guidance_flow_updates=6,guidance_cache_mb=a.cache_mb,
                  guidance_flow_direction=a.direction,guidance_gpu_transport='off',
                  style=0,intensity=1.,local_tone=1.,local_struct=1.,use_auto_mask=True,skin_struct=1.)
    live=e.Live(w,h,settings)
    report=dict(settings=settings,dimensions=[w,h],frames=a.frames,source=str(a.source),
                host_sha256=hashlib.sha256(Path(e.HOST_DLL_V2).read_bytes()).hexdigest(),
                cases=[],source_worker=not a.frozen_worker,base_imported_torch='torch' in sys.modules)
    def run(collect=False):
        pending=deque(); outputs=[]; metrics=[]
        start=time.perf_counter()
        for i,frame in enumerate(frames):
            reset=i in (0,len(frames)//2)
            if live.supports_async:
                if not live.enqueue(frame,reset=reset): raise RuntimeError('Queue rejected')
                pending.append(i)
                if len(pending)>=live.max_in_flight:
                    out=live.dequeue(); pending.popleft()
                    if out is None: raise RuntimeError('Dequeue failed')
                    if collect: outputs.append(out.copy())
            else:
                out=live.process(frame,reset=reset)
                if out is None: raise RuntimeError('Process failed')
                if collect: outputs.append(out.copy())
            metrics.append(dict(live.guidance_metrics))
        while pending:
            out=live.dequeue(); pending.popleft()
            if out is None: raise RuntimeError('Tail failed')
            if collect: outputs.append(out.copy())
        return outputs,(time.perf_counter()-start)*1000/len(frames),metrics
    try:
        reference=None
        for index,transport in enumerate(('off','auto','auto','off')):
            live.update({'guidance_gpu_transport':transport})
            if a.preview_first:
                # CPU visualization must remain available before and after GPU use.
                motion,depth,_=live.guidance_preview(frames[0],reset=True)
                if motion is None or not np.isfinite(motion).all(): raise RuntimeError('Preview failed')
            run()  # warm inference/component and shader, not counted
            outputs,_,metrics=run(True)
            if reference is None: reference=outputs
            max_diff=max(int(np.abs(x.astype(np.int16)-y.astype(np.int16)).max()) for x,y in zip(reference,outputs))
            changed=sum(int(np.count_nonzero(x!=y)) for x,y in zip(reference,outputs))
            _,ms,timed_metrics=run()
            info=live.guidance_info
            case=dict(transport=transport,mean_ms=ms,fps=1000/ms,max_diff=max_diff,changed=changed,
                      info=info,actual_queue=live.max_in_flight,
                      gpu_frames=sum(bool(m.get('gpu_flow')) for m in metrics),
                      cache_hits=sum(bool(m.get('cache_flow_hit')) for m in timed_metrics),
                      worker_pid=live._guidance._process.pid,host_pid=os.getpid())
            report['cases'].append(case)
            print(json.dumps(case,ensure_ascii=False),flush=True)
            if max_diff: raise RuntimeError('CPU/GPU output mismatch')
            if transport=='auto' and not a.allow_fallback and not info.get('gpu_flow_transport'):
                raise RuntimeError('GPU flow unexpectedly fell back: '+str(info))
        report['status']='completed'
    except BaseException as error:
        report.update(status='failed',error=repr(error))
        raise
    finally:
        live.close_guidance()
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    # Existing ProcessLive intentionally exits its disposable host instead of
    # calling the known-hanging NGX Shutdown1 after Evaluate.
    os._exit(0)

if __name__=='__main__': main()
