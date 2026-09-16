"""Bounded frame-generation acceptance harness.

Compares the existing CPU-bounce and shared-GPU transport while using the
production NVOFA motion-vector path.  This is an experiment harness only:
the candidate worker is never installed or selected by the application.
"""
import argparse, ctypes as C, hashlib, json, os, statistics, struct, subprocess, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.gpu_color_probe import bind, duplicate_handle
from scripts.flow_interop_probe import ExternalMemoryDesc, BufferDesc, check
from scripts.gpu_pipeline_probe import digest


def timeline_events(frame_count, multiplier, cuts=()):
    """Return complete output ordering (original and generated timestamps)."""
    if frame_count < 1 or multiplier not in (2, 3, 4):
        raise ValueError("invalid frame timeline")
    cuts = set(cuts); events = []
    for i in range(frame_count):
        if i:
            for slot in range(multiplier - 1):
                events.append({"kind": "hold" if i in cuts else "generated",
                               "source": i - 1, "slot": slot})
        events.append({"kind": "original", "source": i})
    for slot in range(multiplier - 1):
        events.append({"kind": "hold", "source": frame_count - 1, "slot": slot})
    return events


def nvofa_motion(flow, current, previous, padded_size):
    """Match frame_generation.motion_for: RGB, border padding, NVOFA output."""
    import cv2, numpy as np
    fw, fh = padded_size
    def pad(rgba):
        rgb = rgba[..., :3]
        return cv2.copyMakeBorder(rgb, 0, fh-rgb.shape[0], 0, fw-rgb.shape[1], cv2.BORDER_REPLICATE)
    return np.ascontiguousarray(flow.calculate(pad(current), pad(previous))[:current.shape[0], :current.shape[1]])


def _read_exact(pipe, size):
    data = bytearray(size); view = memoryview(data); offset = 0
    while offset < size:
        n = pipe.readinto(view[offset:])
        if not n: raise RuntimeError("worker output truncated")
        offset += n
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work', type=Path, required=True); p.add_argument('--label', required=True)
    p.add_argument('--source', type=Path, required=True); p.add_argument('--frames', type=int, default=8)
    p.add_argument('--rounds', type=int, default=1); p.add_argument('--multiplier', type=int, choices=(2,3,4), default=2)
    p.add_argument('--hdr', action='store_true')
    a = p.parse_args(); a.work = a.work.resolve(); dest = a.work/(a.label+'.json')
    if not (a.work/'TASK.md').is_file() or dest.exists() or not a.label.replace('-','').isalnum(): p.error('registered fresh label required')
    if not 4 <= a.frames <= 16 or not 1 <= a.rounds <= 2: p.error('bounded sample required')
    os.environ['TEMP'] = os.environ['TMP'] = str(a.work)
    import cv2, numpy as np, torch
    from dlss5tool.frame_generation import runtime_files
    cap = cv2.VideoCapture(str(a.source)); frames = []
    for _ in range(a.frames):
        ok, bgr = cap.read()
        if not ok: raise RuntimeError('source too short')
        frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA))
    cap.release(); h, w = frames[0].shape[:2]
    if w*h > 1440*1440: raise ValueError('bounded to 1440-square')
    torch.empty(0, device='cuda')
    from dlss5tool.nvofa import OpticalFlow, align_size
    fw, fh = align_size(w, h); flow = OpticalFlow(fw, fh, grid=1)
    cuda = C.WinDLL('nvcuda.dll'); context=C.c_void_p(); device=C.c_int(); luid=C.create_string_buffer(8); mask=C.c_uint()
    check(bind(cuda,'cuCtxGetCurrent',[C.POINTER(C.c_void_p)])(C.byref(context)), 'context')
    check(bind(cuda,'cuCtxGetDevice',[C.POINTER(C.c_int)])(C.byref(device)), 'device')
    check(bind(cuda,'cuDeviceGetLuid',[C.c_void_p,C.POINTER(C.c_uint),C.c_int])(luid,C.byref(mask),device), 'luid')
    imp=bind(cuda,'cuImportExternalMemory',[C.POINTER(C.c_void_p),C.POINTER(ExternalMemoryDesc)]); mapping=bind(cuda,'cuExternalMemoryGetMappedBuffer',[C.POINTER(C.c_uint64),C.c_void_p,C.POINTER(BufferDesc)])
    h2d=bind(cuda,'cuMemcpyHtoD_v2',[C.c_uint64,C.c_void_p,C.c_size_t]); d2h=bind(cuda,'cuMemcpyDtoH_v2',[C.c_void_p,C.c_uint64,C.c_size_t]); free=bind(cuda,'cuMemFree_v2',[C.c_uint64]); destroy=bind(cuda,'cuDestroyExternalMemory',[C.c_void_p])
    encoder=C.WinDLL(str(a.work/'native-encoder/candidate.dll')); encopen=bind(encoder,'probe_encoder_open',[C.c_void_p,C.c_void_p,C.c_uint,C.c_uint,C.c_uint]); encframe=bind(encoder,'probe_encoder_frame',[C.c_uint,C.c_void_p,C.c_uint,C.POINTER(C.c_uint)]); encclose=bind(encoder,'probe_encoder_close',[],None); error=bind(encoder,'probe_encoder_error',[])
    runtime=runtime_files()[1]; worker=a.work/'native-dlssg/candidate.exe'
    report={'scope':__doc__, 'source':str(a.source), 'dimensions':[w,h], 'frames':a.frames, 'multiplier':a.multiplier, 'hdr':a.hdr, 'motion':'NVOFA grid=1 production algorithm', 'timeline':timeline_events(a.frames,a.multiplier,(a.frames//2,)), 'cases':[], 'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'worker_sha256':hashlib.sha256(worker.read_bytes()).hexdigest(), 'runtime_sha256':hashlib.sha256(runtime.read_bytes()).hexdigest()}
    def run(path, index):
        from dlss5tool.frame_generation import NativeStream
        wire=NativeStream.__new__(NativeStream); wire.cancel=threading.Event(); wire.log=(a.work/f'{a.label}-{index}.log').open('xb'); wire.proc=subprocess.Popen([str(worker),str(runtime.parent),str(a.work),str(w),str(h),str(a.multiplier),'hdr-coded' if a.hdr else 'sdr'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=wire.log,bufsize=0,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        external=C.c_void_p(); pointer=C.c_uint64(); rows=np.zeros((h,w*4),np.uint8); packet=C.create_string_buffer(8*1024*1024); encoded=[]; raw=[]; validity=[]; samples=[]; originals=[]; previous=None; cuts={a.frames//2}
        try:
            ready=wire.call(lambda:_read_exact(wire.proc.stdout,44),timeout=45); magic,low,high,handle,allocation,pitch,pid=struct.unpack('<IIIQQQQ',ready)
            if magic!=0x31474746 or pid!=wire.proc.pid or struct.pack('<II',low,high)!=luid.raw or mask.value!=1: raise RuntimeError('shared descriptor mismatch')
            copied, close_handle=duplicate_handle(pid,handle); desc=ExternalMemoryDesc(); desc.type=5; desc.flags=1; desc.size=allocation; desc.handle.win32.handle=copied.value
            try: check(imp(C.byref(external),C.byref(desc)),'import output')
            finally: close_handle()
            check(mapping(C.byref(pointer),external,C.byref(BufferDesc(size=pitch*h))),'map output')
            if not encopen(context,C.c_void_p(pointer.value),w,h,pitch): raise RuntimeError('encoder open '+str(error()))
            motion=np.zeros((h,w,2),np.float32); events=[]
            for i, frame in enumerate(frames):
                reset=i==0 or i in cuts
                if previous is not None and not reset: motion=nvofa_motion(flow,frame,previous,(fw,fh))
                started=time.perf_counter()
                blocks=(struct.pack('<I',int(reset)),memoryview(frame).cast('B'),memoryview(motion).cast('B'))
                def exchange():
                    for block in blocks:
                        view=memoryview(block)
                        while view:
                            n=wire.proc.stdin.write(view)
                            if not n: raise RuntimeError('worker input closed')
                            view=view[n:]
                    wire.proc.stdin.flush(); out=[]; valid=[]
                    for _ in range(a.multiplier-1): valid.append(struct.unpack('<I',_read_exact(wire.proc.stdout,4))[0]==1); out.append(np.frombuffer(_read_exact(wire.proc.stdout,w*h*4),np.uint8).reshape(h,w,4))
                    return valid,out
                valid, outs=wire.call(exchange,timeout=30); validity.extend(valid)
                if previous is not None:
                    for slot, out in enumerate(outs):
                        if reset: out=previous
                        if path=='cpu-bounce': rows[:,:w*4]=out.reshape(h,w*4); check(h2d(pointer,rows.ctypes.data,rows.nbytes),'upload')
                        size=C.c_uint();
                        if not encframe(len(encoded),packet,len(packet),C.byref(size)): raise RuntimeError('encode '+str(error()))
                        encoded.append(packet.raw[:size.value]); raw.append(digest(out)); events.append({'kind':'hold' if reset else 'generated','source':i-1,'slot':slot})
                originals.append(digest(frame)); events.append({'kind':'original','source':i}); previous=frame
                samples.append((time.perf_counter()-started)*1000)
            for _ in range(a.multiplier-1): events.append({'kind':'hold','source':a.frames-1,'slot':_})
            return {'path':path,'mean_ms':statistics.mean(samples),'samples_ms':samples,'raw_generated':raw,'originals':originals,'validity':validity,'timeline':events,'encoded_sha256':hashlib.sha256(b''.join(encoded)).hexdigest(),'packet_count':len(encoded)}, b''.join(encoded)
        finally:
            encclose()
            if pointer.value: check(free(pointer),'free')
            if external.value: check(destroy(external),'destroy')
            wire.close()
    try:
        reference, encoded = run('cpu-bounce',0); report['reference']=reference
        for repeat in range(a.rounds):
            for path in ('resident','cpu-bounce') if repeat%2 else ('cpu-bounce','resident'):
                result,_=run(path, repeat*2+(1 if path=='cpu-bounce' else 2)); result['repeat']=repeat
                for key, refkey in (('raw_equal','raw_generated'),('original_equal','originals'),('timeline_equal','timeline'),('validity_equal','validity')): result[key]=result[refkey]==reference[refkey]
                result['bitstream_equal']=result['encoded_sha256']==reference['encoded_sha256']; report['cases'].append(result)
                if not all(result[k] for k in ('raw_equal','original_equal','timeline_equal','validity_equal','bitstream_equal')): raise RuntimeError('repeatability/equality gate failed')
        report['summary']={p:statistics.mean(r['mean_ms'] for r in report['cases'] if r['path']==p) for p in ('cpu-bounce','resident')}; report['status']='measurements_completed'
    except BaseException as exc: report.update(status='failed',error=repr(exc))
    finally: dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Report: '+str(dest),flush=True)
    raise SystemExit(0 if report.get('status')=='measurements_completed' else 1)

if __name__=='__main__': main()
