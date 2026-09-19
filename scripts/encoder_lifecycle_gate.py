"""Native failure injection / cancellation / reuse gate. Small same-GPU test."""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.gpu_color_probe import bind


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',type=Path,required=True)
    a=p.parse_args();a.work=a.work.resolve();dest=a.work/'lifecycle.json'
    if not (a.work/'TASK.md').is_file() or dest.exists():p.error('Fresh registered task required')
    os.environ['TEMP']=os.environ['TMP']=str(a.work)
    import torch
    from dlss5tool.nvenc_yuv import NativeYuvEncoder,YuvEncoderConfig,ReadyYuvFrame,NativeEncoderError
    torch.empty(0,device='cuda');ctx=C.c_void_p()
    if bind(C.WinDLL('nvcuda.dll'),'cuCtxGetCurrent',[C.POINTER(C.c_void_p)])(C.byref(ctx)):raise RuntimeError('No context')
    path=a.work/'native-encoder/ring.dll';dll=C.WinDLL(str(path));u=C.c_uint
    configure=bind(dll,'ring_open_config',[C.c_void_p]+[u]*7+[C.POINTER(u),C.POINTER(u)])
    mode=bind(dll,'ring_enable_cuda_input',[C.c_int]);fault=bind(dll,'ring_test_allocation_failure',[C.c_int])
    owned=bind(dll,'ring_owned_buffers',[]);close=bind(dll,'ring_close',[],None)
    r=dict(scope=__doc__,native_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),failures=[],cancel=[],status='running')
    try:
        for gpu in (False,True):
            for slot in (0,3,15):
                mode(int(gpu));fault(slot);pitch,count=u(),u()
                opened=configure(ctx,320,180,24,1,19,5,0,C.byref(pitch),C.byref(count))
                row=dict(gpu=gpu,failed_slot=slot,opened=bool(opened),owned_after_failure=owned());r['failures'].append(row)
                if opened or owned():raise RuntimeError('Partial allocation leaked')
                close()
        fault(-1)
        for codec in (0,1,2):
            cfg=YuvEncoderConfig(320,180,24,1,codec)
            frame=torch.zeros(cfg.frame_bytes,dtype=torch.uint8,device='cuda');torch.cuda.synchronize()
            for count in (0,3,17):
                session=NativeYuvEncoder(path,cfg)
                try:
                    try:NativeYuvEncoder(path,cfg)
                    except NativeEncoderError:pass
                    else:raise RuntimeError('Second session incorrectly acquired same DLL')
                    for index in range(count):session.write(ReadyYuvFrame(frame.data_ptr(),cfg.frame_bytes,cfg.layout,index))
                finally:session.close()
                session.close()
                row=dict(codec=codec,submitted=count,owned_after_cancel=owned());r['cancel'].append(row)
                if owned():raise RuntimeError('Cancellation leaked native buffers')
            packets=[]
            with NativeYuvEncoder(path,cfg) as session:
                for index in range(120):packets.extend(session.write(ReadyYuvFrame(frame.data_ptr(),cfg.frame_bytes,cfg.layout,index)))
                packets.extend(session.finish())
            r.setdefault('reopen',[]).append(dict(codec=codec,submitted=120,packets=len(packets),owned=owned()))
            if len(packets)!=120 or owned():raise RuntimeError('Reuse/flush lost frames or buffers')
        r['status']='passed'
    except BaseException as exc:
        r.update(status='failed',error=repr(exc))
        import traceback
        traceback.print_exc()
    finally:
        with dest.open('x',encoding='utf-8') as out:json.dump(r,out,indent=2)
        print(json.dumps(r),flush=True)
    raise SystemExit(0 if r['status']=='passed' else 1)


if __name__=='__main__':main()
