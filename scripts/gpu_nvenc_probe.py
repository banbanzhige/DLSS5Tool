"""Real DLSS SDR -> shared D3D12 buffer -> CUDA import -> NVENC packets.

Compare to identical encoder fed identical DLSS pixels through a CPU bounce.
This tests transport with a pinned low-latency encoder, NOT production FFmpeg
color conversion, B frames, CQ/VBR, HDR, audio muxing or GUI cache behavior.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.gpu_color_probe import bind,Luid
from scripts.flow_interop_probe import ExternalMemoryDesc,BufferDesc,check
from scripts.gpu_pipeline_probe import digest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--label',required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--frames',type=int,default=24)
    p.add_argument('--start',type=int,default=0)
    p.add_argument('--rounds',type=int,default=3)
    a=p.parse_args();a.work=a.work.resolve();dest=a.work/(a.label+'.json')
    if (not (a.work/'TASK.md').is_file() or dest.exists() or not a.label.replace('-','').isalnum()
        or not 4<=a.frames<=64 or not 1<=a.rounds<=5):p.error('Registered task and bounded arguments required')
    os.environ['TEMP']=os.environ['TMP']=str(a.work)
    import torch
    import cv2
    import numpy as np
    from dlss5tool import dlss_engine as e
    from dlss5tool.video_export import find_ffmpeg
    cap=cv2.VideoCapture(str(a.source));frames=[]
    for _ in range(a.start):cap.grab()
    for _ in range(a.frames):
        ok,bgr=cap.read()
        if not ok:raise RuntimeError('Source too short')
        frames.append(cv2.cvtColor(bgr,cv2.COLOR_BGR2RGBA))
    cap.release()
    h,w=frames[0].shape[:2]
    if w*h>3840*2160:raise ValueError('4K pixel budget exceeded')
    e.HOST_DLL_V2=str(a.work/'native-color/candidate.dll');e.LOG_PATH=str(a.work/(a.label+'-ngx.log'))
    settings=dict(host_backend='v2',host_auto_fallback=False,host_submission='compatibility',host_in_flight=1,
        host_zero_fast_path=True,host_persistent_buffers=True,guidance_mode=0,
        style=0,intensity=1.,local_tone=1.,local_struct=1.,skin_struct=1.,use_auto_mask=True)
    live=e.Live(w,h,settings);lib=live._lib
    process=bind(lib,'probe_color_process',[C.c_void_p,C.c_void_p,C.c_int,C.c_int,C.c_int])
    read=bind(lib,'probe_color_read',[C.c_void_p])
    prepare=bind(lib,'probe_output_prepare',[C.c_void_p])
    close=bind(lib,'probe_color_close',[],None)
    share=bind(lib,'probe_output_share',[C.POINTER(C.c_void_p),C.POINTER(C.c_uint64),C.POINTER(C.c_uint),C.POINTER(Luid)])
    cuda=C.WinDLL('nvcuda.dll');torch.empty(0,device='cuda')
    context=C.c_void_p();device=C.c_int();cuda_luid=C.create_string_buffer(8);mask=C.c_uint()
    check(bind(cuda,'cuCtxGetCurrent',[C.POINTER(C.c_void_p)])(C.byref(context)),'current context')
    check(bind(cuda,'cuCtxGetDevice',[C.POINTER(C.c_int)])(C.byref(device)),'current device')
    check(bind(cuda,'cuDeviceGetLuid',[C.c_void_p,C.POINTER(C.c_uint),C.c_int])(cuda_luid,C.byref(mask),device),'CUDA LUID')
    import_memory=bind(cuda,'cuImportExternalMemory',[C.POINTER(C.c_void_p),C.POINTER(ExternalMemoryDesc)])
    map_buffer=bind(cuda,'cuExternalMemoryGetMappedBuffer',[C.POINTER(C.c_uint64),C.c_void_p,C.POINTER(BufferDesc)])
    free=bind(cuda,'cuMemFree_v2',[C.c_uint64]);destroy=bind(cuda,'cuDestroyExternalMemory',[C.c_void_p])
    encoder=C.WinDLL(str(a.work/'native-encoder/candidate.dll'))
    encopen=bind(encoder,'probe_encoder_open',[C.c_void_p,C.c_void_p,C.c_uint,C.c_uint,C.c_uint])
    encframe=bind(encoder,'probe_encoder_frame',[C.c_uint,C.c_void_p,C.c_uint,C.POINTER(C.c_uint)])
    encclose=bind(encoder,'probe_encoder_close',[],None);error=bind(encoder,'probe_encoder_error',[])
    external=C.c_void_p();pointer=C.c_uint64();report=dict(scope=__doc__,cases=[],source=str(a.source),
        dimensions=[w,h],frames=a.frames,start=a.start,settings=settings,
        encoder_policy='H264 P5 low-latency CONSTQP19 no B/no lookahead; transport experiment only',
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        host_sha256=hashlib.sha256(Path(e.HOST_DLL_V2).read_bytes()).hexdigest(),
        encoder_sha256=hashlib.sha256((a.work/'native-encoder/candidate.dll').read_bytes()).hexdigest())
    code=0
    try:
        handle,allocation,pitch,luid=C.c_void_p(),C.c_uint64(),C.c_uint(),Luid()
        if not share(C.byref(handle),C.byref(allocation),C.byref(pitch),C.byref(luid)):raise RuntimeError('Share output failed')
        if (luid.low,luid.high)!=(int.from_bytes(cuda_luid.raw[:4],'little'),int.from_bytes(cuda_luid.raw[4:],'little',signed=True)) or mask.value!=1:
            raise RuntimeError('Adapter mismatch; refuse import')
        desc=ExternalMemoryDesc();desc.type=5;desc.flags=1;desc.size=allocation.value;desc.handle.win32.handle=handle.value
        check(import_memory(C.byref(external),C.byref(desc)),'import D3D12 output')
        mapping=BufferDesc(size=pitch.value*h)
        check(map_buffer(C.byref(pointer),external,C.byref(mapping)),'map output CUDA pointer')
        output=np.empty_like(frames[0]);packet=C.create_string_buffer(8*1024*1024)
        def run(path,audit=False):
            if not encopen(context,C.c_void_p(pointer.value),w,h,pitch.value):raise RuntimeError('NVENC open error '+str(error()))
            samples=[];packets=[];checks=[]
            try:
                for i,frame in enumerate(frames):
                    started=time.perf_counter();bounce=path=='cpu-bounce'
                    if not process(frame.ctypes.data,output.ctypes.data,0,int(i in (0,len(frames)//2)),int(bounce)):
                        raise RuntimeError('DLSS failed')
                    if not prepare(output.ctypes.data if bounce else None):raise RuntimeError('Encoder prepare failed')
                    size=C.c_uint()
                    if not encframe(i,packet,len(packet),C.byref(size)):raise RuntimeError('NVENC frame error '+str(error()))
                    samples.append((time.perf_counter()-started)*1000)
                    packets.append(packet.raw[:size.value])
                    if sum(map(len,packets))>64*1024*1024:raise RuntimeError('Packet memory budget exceeded')
                    if audit:
                        if not bounce and not read(output.ctypes.data):raise RuntimeError('Audit readback failed')
                        checks.append(digest(output))
                encoded=b''.join(packets)
                return dict(path=path,mean_ms=statistics.mean(samples),samples_ms=samples,packet_sizes=list(map(len,packets)),
                            encoded_sha256=hashlib.sha256(encoded).hexdigest(),raw_checks=checks),encoded
            finally:encclose()
        run('cpu-bounce')
        reference,encoded=run('cpu-bounce',True);report['reference']=reference
        # Validate that the compressed output decodes to exactly the requested
        # count. Read only compressed bytes through stdin, no media files.
        ffmpeg=find_ffmpeg()
        decoded=subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','h264','-i','pipe:0',
            '-f','framemd5','-'],input=encoded,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        rows=[r for r in decoded.stdout.decode().splitlines() if r and not r.startswith('#')]
        report['decode_validation']=dict(returncode=decoded.returncode,frames=len(rows),stderr=decoded.stderr.decode(errors='replace'))
        if decoded.returncode or len(rows)!=len(frames):raise RuntimeError('Encoded stream decode/count validation failed')
        for repeat in range(a.rounds):
            for path in (('cpu-bounce','resident') if repeat%2==0 else ('resident','cpu-bounce')):
                result,_=run(path,True);result['repeat']=repeat
                result['raw_equal']=result['raw_checks']==reference['raw_checks']
                result['bitstream_equal']=result['encoded_sha256']==reference['encoded_sha256']
                report['cases'].append(result)
                print(json.dumps({k:v for k,v in result.items() if k not in ('samples_ms','raw_checks','packet_sizes')}),flush=True)
                if not result['raw_equal'] or not result['bitstream_equal']:raise RuntimeError('Transport equality/repeatability gate failed')
        report['summary']={path:statistics.mean(r['mean_ms'] for r in report['cases'] if r['path']==path) for path in ('cpu-bounce','resident')}
        report['status']='measurements_completed'
    except BaseException as exc:
        report.update(status='failed',error=repr(exc));code=1
        import traceback
        traceback.print_exc()
    finally:
        encclose()
        if pointer.value:check(free(pointer),'free mapping')
        if external.value:check(destroy(external),'destroy import')
        close()
        with dest.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
        print('Report: '+str(dest),flush=True)
    os._exit(code)


if __name__=='__main__':main()
