"""Serial cross-process VSR->shared VRAM->DLSS experiment, SDR only.

No changes to deployed DLLs. One producer and one consumer, with CPU fence waits
on both sides. This proves residency, not async or production IPC lifecycle.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
import multiprocessing
from multiprocessing import shared_memory
from pathlib import Path
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.gpu_pipeline_probe import digest


def bind(lib,name,args,result=C.c_int):
    fn=getattr(lib,name);fn.argtypes=args;fn.restype=result
    return fn


class Luid(C.Structure):
    _fields_=[('low',C.c_uint32),('high',C.c_int32)]


def vsr_worker(conn, work, w, h, scale, label, input_name, output_name):
    import numpy as np
    # Match ProcessSuperResolution's loader: ctypes uses secure default DLL
    # search flags; SetDllDirectory alone is not equivalent to AddDllDirectory.
    dll_directory = os.add_dll_directory(str(ROOT/'runtime'))
    input_memory=shared_memory.SharedMemory(name=input_name)
    output_memory=shared_memory.SharedMemory(name=output_name)
    input_frame=np.ndarray((h,w,4),np.uint8,buffer=input_memory.buf)
    output=np.ndarray((h*scale,w*scale,4),np.uint8,buffer=output_memory.buf)
    try:
        sr=C.WinDLL(str(Path(work)/'native-vsr/candidate.dll'))
        init=bind(sr,'vsr_init',[C.c_int]*5+[C.c_wchar_p,C.c_wchar_p])
        process=bind(sr,'probe_vsr_process',[C.c_void_p,C.c_void_p,C.c_int])
        share=bind(sr,'probe_vsr_share',[C.POINTER(C.c_void_p),C.POINTER(Luid)])
        if not init(w,h,scale,4,0,str(ROOT/'runtime'),str(Path(work)/(label+'-vsr.log'))):
            raise RuntimeError('VSR init failed')
        handle,luid=C.c_void_p(),Luid()
        if not share(C.byref(handle),C.byref(luid)):raise RuntimeError('VSR share failed')
        conn.send(dict(ok=True,handle=handle.value,pid=os.getpid(),luid=[luid.high,luid.low]))
        while True:
            request=conn.recv()
            if request is None:break
            shared=request
            if not process(input_frame.ctypes.data,None if shared else output.ctypes.data,int(shared)):
                raise RuntimeError('VSR process failed')
            conn.send(dict(ok=True))
        bind(sr,'probe_vsr_close_share',[],None)()
    except BaseException as error:
        conn.send(dict(ok=False,error=repr(error)))
    finally:
        conn.close()
        input_memory.close();output_memory.close()
        dll_directory.close()
        os._exit(0)


def duplicate_handle(pid,handle):
    kernel=C.WinDLL('kernel32',use_last_error=True)
    op=bind(kernel,'OpenProcess',[C.c_uint,C.c_int,C.c_uint],C.c_void_p)
    dup=bind(kernel,'DuplicateHandle',[C.c_void_p,C.c_void_p,C.c_void_p,C.POINTER(C.c_void_p),C.c_uint,C.c_int,C.c_uint])
    current=bind(kernel,'GetCurrentProcess',[],C.c_void_p)()
    close=bind(kernel,'CloseHandle',[C.c_void_p])
    process=op(0x40,0,pid)
    if not process:raise C.WinError(C.get_last_error())
    result=C.c_void_p()
    try:
        if not dup(process,handle,current,C.byref(result),0,0,2):raise C.WinError(C.get_last_error())
    finally:
        close(process)
    return result,lambda:close(result)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--label',required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--frames',type=int,default=24)
    p.add_argument('--start',type=int,default=0)
    p.add_argument('--rounds',type=int,default=3)
    p.add_argument('--scale',type=int,choices=(1,2),default=2)
    p.add_argument('--skip-no-readback',action='store_true',help='Isolate VSR input transport without output-readback scheduling change')
    p.add_argument('--baseline-only',action='store_true',help='Repeatability control without either residency change')
    a=p.parse_args();a.work=a.work.resolve()
    dest=a.work/(a.label+'.json')
    if (not (a.work/'TASK.md').is_file() or dest.exists() or not a.label.replace('-','').isalnum()
        or not 4<=a.frames<=64 or not 1<=a.rounds<=5):
        p.error('Registered task, fresh safe label and bounded parameters required')
    os.environ['TEMP']=os.environ['TMP']=str(a.work)
    import cv2
    import numpy as np
    from dlss5tool import dlss_engine as engine
    cap=cv2.VideoCapture(str(a.source))
    for _ in range(a.start):
        cap.grab()
    frames=[]
    for _ in range(a.frames):
        ok,bgr=cap.read()
        if not ok: raise RuntimeError('Source too short')
        frames.append(cv2.cvtColor(bgr,cv2.COLOR_BGR2RGBA))
    cap.release()
    h,w=frames[0].shape[:2]; ow,oh=w*a.scale,h*a.scale
    if ow*oh>3840*2160: raise ValueError('4K pixel budget exceeded')
    engine.HOST_DLL_V2=str(a.work/'native-color/candidate.dll')
    engine.LOG_PATH=str(a.work/(a.label+'-ngx.log'))
    settings=dict(host_backend='v2',host_auto_fallback=False,host_submission='compatibility',
                  host_in_flight=1,host_zero_fast_path=True,host_persistent_buffers=True,
                  guidance_mode=0,style=0,intensity=1.,local_tone=1.,local_struct=1.,skin_struct=1.,use_auto_mask=True)
    live=engine.Live(ow,oh,settings);lib=live._lib
    process=bind(lib,'probe_color_process',[C.c_void_p,C.c_void_p,C.c_int,C.c_int,C.c_int])
    read=bind(lib,'probe_color_read',[C.c_void_p])
    close=bind(lib,'probe_color_close',[],None)
    sr=conn=None
    shared_input=shared_output=None
    def receive():
        if not conn.poll(45):raise TimeoutError('VSR worker timed out; abort both GPU owners')
        value=conn.recv()
        if not value['ok']:raise RuntimeError(value['error'])
        return value
    report=dict(scope='SDR two processes/D3D12 devices; CPU baseline uses shared RAM (not pickled frames), GPU candidate uses shared VRAM; CPU fence waits retained; no encoding',
                input_size=[w,h],output_size=[ow,oh],settings=settings,cases=[],
                source=str(a.source),start=a.start,frames=a.frames,scale=a.scale,
                baseline_only=a.baseline_only,skip_no_readback=a.skip_no_readback,
                script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                host_sha256=hashlib.sha256(Path(engine.HOST_DLL_V2).read_bytes()).hexdigest())
    code=0
    try:
        if a.scale>1:
            shared_input=shared_memory.SharedMemory(create=True,size=w*h*4)
            shared_output=shared_memory.SharedMemory(create=True,size=ow*oh*4)
            input_view=np.ndarray((h,w,4),np.uint8,buffer=shared_input.buf)
            output_view=np.ndarray((oh,ow,4),np.uint8,buffer=shared_output.buf)
            context=multiprocessing.get_context('spawn')
            conn,child=context.Pipe()
            sr=context.Process(target=vsr_worker,args=(child,str(a.work),w,h,a.scale,a.label,
                shared_input.name,shared_output.name),daemon=True)
            sr.start();child.close()
            ready=receive();luid=Luid(ready['luid'][1],ready['luid'][0])
            handle,handle_close=duplicate_handle(ready['pid'],ready['handle'])
            try:
                if not bind(lib,'probe_color_open',[C.c_void_p,C.POINTER(Luid)])(handle,C.byref(luid)):
                    raise RuntimeError('Shared color import rejected')
            finally:handle_close()
            report['shared_adapter_luid']=ready['luid']
            report['producer_pid']=ready['pid'];report['consumer_pid']=os.getpid()
        upscaled=np.empty((oh,ow,4),np.uint8)
        output=np.empty_like(upscaled)
        reset_at={0,len(frames)//2}
        def run(path,audit=True):
            records=[]
            for i,frame in enumerate(frames):
                started=time.perf_counter()
                if sr:
                    shared=path!='baseline'
                    np.copyto(input_view,frame)
                    conn.send(shared)
                    receive()
                    if not shared:upscaled=output_view.copy()
                    source=None if shared else upscaled.ctypes.data
                else:
                    shared=False;source=frame.ctypes.data
                keep=path!='no-output-readback'
                if not process(source,output.ctypes.data,int(shared),int(i in reset_at),int(keep)):
                    raise RuntimeError('Color processing failed')
                elapsed=(time.perf_counter()-started)*1000
                # Audit unconditionally reads output, but outside the no-readback
                # timer. This is an upper-bound isolation, not a useful export.
                if not keep and audit and not read(output.ctypes.data):raise RuntimeError('Audit readback failed')
                records.append(dict(frame=i,ms=elapsed,output_sha256=digest(output) if audit else None,
                                    rgb_mean=output[...,:3].mean(axis=(0,1)).tolist() if audit else None))
            return dict(path=path,mean_ms=statistics.mean(r['ms'] for r in records),frames=records)
        run('baseline')
        reference=run('baseline');report['reference']=reference
        paths=('baseline','resident','no-output-readback') if sr else ('baseline','no-output-readback')
        if a.skip_no_readback:paths=tuple(x for x in paths if x!='no-output-readback')
        if a.baseline_only:paths=('baseline',)
        for repeat in range(a.rounds):
            for path in paths if repeat%2==0 else tuple(reversed(paths)):
                result=run(path);result['repeat']=repeat
                result['changed_frames']=[x['frame'] for x,y in zip(result['frames'],reference['frames']) if x['output_sha256']!=y['output_sha256']]
                report['cases'].append(result)
                print(json.dumps({k:v for k,v in result.items() if k!='frames'}),flush=True)
                if result['changed_frames']:raise RuntimeError('Output equality/repeatability gate failed')
        report['summary']={path:statistics.mean(r['mean_ms'] for r in report['cases'] if r['path']==path) for path in paths}
        report['status']='measurements_completed'
    except BaseException as error:
        report.update(status='failed',error=repr(error));code=1
        import traceback
        traceback.print_exc()
    finally:
        close()
        if sr:
            try:
                conn.send(None)
                sr.join(3)
            except (BrokenPipeError,EOFError,OSError):pass
            if sr.is_alive():sr.terminate();sr.join(3)
            conn.close()
        for memory in (shared_input,shared_output):
            if memory is not None:
                memory.close();memory.unlink()
        with dest.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
        print('Report: '+str(dest),flush=True)
    # Same crash-isolated shutdown policy as the app; OS reclaims NGX on exit.
    os._exit(code)


if __name__=='__main__':main()
