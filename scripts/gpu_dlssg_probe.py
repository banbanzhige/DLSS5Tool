"""Isolate SDR 2x DLSSG output residency through real NVENC.

Real decoded pictures, fixed ZERO motion to isolate transport; NOT an optical
flow/interpolation quality test. Input remains a CPU pipe in BOTH variants.
Only generated frames are encoded, reset outputs omitted in both variants.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import statistics
import struct
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.gpu_color_probe import bind,duplicate_handle
from scripts.flow_interop_probe import ExternalMemoryDesc,BufferDesc,check
from scripts.gpu_pipeline_probe import digest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--label',required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--frames',type=int,default=24)
    p.add_argument('--rounds',type=int,default=2)
    a=p.parse_args();a.work=a.work.resolve();dest=a.work/(a.label+'.json')
    if (not (a.work/'TASK.md').is_file() or dest.exists() or not a.label.replace('-','').isalnum()
        or not 4<=a.frames<=32 or not 1<=a.rounds<=3):p.error('Registered task, fresh label and bounded parameters required')
    os.environ['TEMP']=os.environ['TMP']=str(a.work)
    import torch
    import cv2
    import numpy as np
    from dlss5tool.frame_generation import NativeStream,_read_exact,runtime_files
    from dlss5tool.video_export import find_ffmpeg
    cap=cv2.VideoCapture(str(a.source));frames=[]
    for _ in range(a.frames):
        ok,bgr=cap.read()
        if not ok:raise RuntimeError('Source too short')
        frames.append(cv2.cvtColor(bgr,cv2.COLOR_BGR2RGBA))
    cap.release();h,w=frames[0].shape[:2]
    if w*h>1920*1080:raise ValueError('This DLSSG probe is bounded to 1080p')
    torch.empty(0,device='cuda');cuda=C.WinDLL('nvcuda.dll')
    context=C.c_void_p();device=C.c_int();luid=C.create_string_buffer(8);mask=C.c_uint()
    check(bind(cuda,'cuCtxGetCurrent',[C.POINTER(C.c_void_p)])(C.byref(context)),'context')
    check(bind(cuda,'cuCtxGetDevice',[C.POINTER(C.c_int)])(C.byref(device)),'device')
    check(bind(cuda,'cuDeviceGetLuid',[C.c_void_p,C.POINTER(C.c_uint),C.c_int])(luid,C.byref(mask),device),'luid')
    imp=bind(cuda,'cuImportExternalMemory',[C.POINTER(C.c_void_p),C.POINTER(ExternalMemoryDesc)])
    mapping=bind(cuda,'cuExternalMemoryGetMappedBuffer',[C.POINTER(C.c_uint64),C.c_void_p,C.POINTER(BufferDesc)])
    h2d=bind(cuda,'cuMemcpyHtoD_v2',[C.c_uint64,C.c_void_p,C.c_size_t])
    d2h=bind(cuda,'cuMemcpyDtoH_v2',[C.c_void_p,C.c_uint64,C.c_size_t])
    free=bind(cuda,'cuMemFree_v2',[C.c_uint64]);destroy=bind(cuda,'cuDestroyExternalMemory',[C.c_void_p])
    encoder=C.WinDLL(str(a.work/'native-encoder/candidate.dll'))
    encopen=bind(encoder,'probe_encoder_open',[C.c_void_p,C.c_void_p,C.c_uint,C.c_uint,C.c_uint])
    encframe=bind(encoder,'probe_encoder_frame',[C.c_uint,C.c_void_p,C.c_uint,C.POINTER(C.c_uint)])
    encclose=bind(encoder,'probe_encoder_close',[],None);error=bind(encoder,'probe_encoder_error',[])
    report=dict(scope=__doc__,source=str(a.source),dimensions=[w,h],frames=a.frames,cases=[],
                encoder_policy='H264 P5 low-latency CONSTQP19 no B/no lookahead',
                script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                worker_sha256=hashlib.sha256((a.work/'native-dlssg/candidate.exe').read_bytes()).hexdigest())
    runtime=runtime_files()[1]
    report['runtime_sha256']=hashlib.sha256(runtime.read_bytes()).hexdigest()
    def run(path,index):
        wire=NativeStream.__new__(NativeStream)
        wire.cancel=threading.Event()
        wire.log=(a.work/f'{a.label}-{index}.log').open('xb')
        wire.proc=subprocess.Popen([str(a.work/'native-dlssg/candidate.exe'),str(runtime.parent),str(a.work),
            str(w),str(h),'2','sdr'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=wire.log,
            bufsize=0,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        external=C.c_void_p();pointer=C.c_uint64()
        try:
            ready=wire.call(lambda:_read_exact(wire.proc.stdout,44),timeout=45)
            magic,low,high,handle,allocation,pitch,pid=struct.unpack('<IIIQQQQ',ready)
            if magic!=0x31474746 or pid!=wire.proc.pid or struct.pack('<II',low,high)!=luid.raw or mask.value!=1:
                raise RuntimeError('Untrusted/mismatched shared descriptor')
            copied,close_handle=duplicate_handle(pid,handle)
            desc=ExternalMemoryDesc();desc.type=5;desc.flags=1;desc.size=allocation;desc.handle.win32.handle=copied.value
            try:check(imp(C.byref(external),C.byref(desc)),'import output')
            finally:close_handle()
            check(mapping(C.byref(pointer),external,C.byref(BufferDesc(size=pitch*h))),'map output')
            if not encopen(context,C.c_void_p(pointer.value),w,h,pitch):raise RuntimeError('Encoder open '+str(error()))
            rows=np.zeros((h,pitch),np.uint8);motion=np.zeros((h,w,2),np.float32)
            packet=C.create_string_buffer(8*1024*1024);checks=[];packets=[];samples=[];validity=[]
            for i,frame in enumerate(frames):
                resident=path=='resident';reset=i in (0,len(frames)//2)
                def exchange():
                    for block in (struct.pack('<I',int(reset)|(4 if resident else 0)),memoryview(frame).cast('B'),memoryview(motion).cast('B')):
                        view=memoryview(block)
                        while view:
                            size=wire.proc.stdin.write(view)
                            if not size:raise RuntimeError('Input pipe closed')
                            view=view[size:]
                    wire.proc.stdin.flush()
                    valid=struct.unpack('<I',_read_exact(wire.proc.stdout,4))[0]
                    rgba=None if resident else np.frombuffer(_read_exact(wire.proc.stdout,w*h*4),np.uint8).reshape(h,w,4)
                    return valid,rgba
                started=time.perf_counter();valid,rgba=wire.call(exchange,timeout=30)
                validity.append(valid)
                if not reset:
                    if not valid:raise RuntimeError('DLSSG invalid on nonreset pair')
                    if not resident:
                        rows[:,:w*4]=rgba.reshape(h,w*4)
                        check(h2d(pointer,rows.ctypes.data,rows.nbytes),'baseline upload')
                    size=C.c_uint()
                    if not encframe(len(packets),packet,len(packet),C.byref(size)):raise RuntimeError('Encode '+str(error()))
                    samples.append((time.perf_counter()-started)*1000)
                    packets.append(packet.raw[:size.value])
                    if resident:check(d2h(rows.ctypes.data,pointer,rows.nbytes),'audit readback outside timer')
                    checks.append(digest(rows[:,:w*4]))
                else:samples.append((time.perf_counter()-started)*1000)
            encoded=b''.join(packets)
            return dict(path=path,mean_ms=statistics.mean(samples),samples_ms=samples,
                        raw_checks=checks,encoded_sha256=hashlib.sha256(encoded).hexdigest(),
                        packet_count=len(packets),validity=validity),encoded
        finally:
            encclose()
            if pointer.value:check(free(pointer),'unmap')
            if external.value:check(destroy(external),'destroy')
            wire.close()
    code=0
    try:
        reference,encoded=run('cpu-bounce',0);report['reference']=reference
        validation=subprocess.run([find_ffmpeg(),'-hide_banner','-loglevel','error','-f','h264','-i','pipe:0','-f','framemd5','-'],
            input=encoded,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        count=sum(bool(s) and not s.startswith('#') for s in validation.stdout.decode().splitlines())
        report['decode_validation']=dict(returncode=validation.returncode,frames=count,stderr=validation.stderr.decode(errors='replace'))
        if validation.returncode or count!=a.frames-2:raise RuntimeError('Decode/count failed')
        index=0
        for repeat in range(a.rounds):
            for path in (('cpu-bounce','resident') if repeat%2==0 else ('resident','cpu-bounce')):
                index+=1;result,_=run(path,index);result['repeat']=repeat
                result['raw_equal']=result['raw_checks']==reference['raw_checks']
                result['bitstream_equal']=result['encoded_sha256']==reference['encoded_sha256']
                result['validity_equal']=result['validity']==reference['validity']
                report['cases'].append(result)
                print(json.dumps({k:v for k,v in result.items() if k not in ('raw_checks','samples_ms','validity')}),flush=True)
                if not all(result[k] for k in ('raw_equal','bitstream_equal','validity_equal')):raise RuntimeError('Equality/repeatability gate failed')
        report['summary']={path:statistics.mean(r['mean_ms'] for r in report['cases'] if r['path']==path) for path in ('cpu-bounce','resident')}
        report['status']='measurements_completed'
    except BaseException as exc:
        report.update(status='failed',error=repr(exc));code=1
        import traceback
        traceback.print_exc()
    finally:
        with dest.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
        print('Report: '+str(dest),flush=True)
    os._exit(code)


if __name__=='__main__':main()
