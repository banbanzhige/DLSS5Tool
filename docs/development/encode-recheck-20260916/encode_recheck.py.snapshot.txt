"""Targeted retest with immutable old evidence, small samples and exact scope."""
import argparse
import ast
import ctypes as C
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(cmd, data=None):
    result=subprocess.run(cmd,input=data,capture_output=True,timeout=45,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace')[-2000:])
    return result.stdout


def compare(a,b):
    import numpy as np
    delta=b.astype(np.float64)-a.astype(np.float64)
    mse=float(np.mean(delta*delta))
    return dict(equal=a.tobytes()==b.tobytes(),changed=int(np.count_nonzero(a!=b)),
        values=a.size,max_abs=float(np.abs(delta).max()),mae=float(np.abs(delta).mean()),
        bias=float(delta.mean()),psnr_db=10*math.log10(255**2/mse) if mse and a.dtype==np.uint8 and b.dtype==np.uint8 else None,
        psnr_scope='8-bit decoded values only; no HDR code-value PSNR')


def endpoint(args,r):
    import numpy as np
    from dlss5tool import video_export as v
    source=subprocess.check_output(['git','show','HEAD:dlss5tool/video_export.py'],cwd=ROOT).decode('utf-8')
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='compose_hdr_frame')
    ns=dict(vars(v));exec(compile(ast.Module(body=[node],type_ignores=[]),'HEAD:video_export.py','exec'),ns)
    old=ns['compose_hdr_frame'];rng=np.random.default_rng(20260916);checks=[]
    for dtype in (np.float16,np.float32):
        for contiguous in (True,False):
            a=rng.random((13,18,4),dtype=np.float32).astype(dtype);b=rng.random(a.shape,dtype=np.float32).astype(dtype)
            if not contiguous:a=a[:,::2];b=b[:,::2]
            for view in (0,1,2):
                for mix in (-1,0,.7,1,2,5,6):
                    for profile in ('hdr10_pq','hdr10_hlg'):
                        checks.append(dict(dtype=str(np.dtype(dtype)),contiguous=contiguous,view=view,mix=mix,profile=profile,
                            **compare(old(a,b,view,mix,profile),v.compose_hdr_frame(a,b,view,mix,profile))))
    a=np.full((1080,1920,4),.25,np.float16);b=np.full_like(a,.75)
    timing=[]
    for mix in (0,1):
        old(a,b,mix=mix);v.compose_hdr_frame(a,b,mix=mix)
        for repeat in range(6):
            for label,fn in ((('old',old),('fast',v.compose_hdr_frame)) if repeat%2==0 else (('fast',v.compose_hdr_frame),('old',old))):
                start=time.perf_counter();out=fn(a,b,mix=mix)
                timing.append(dict(mix=mix,path=label,ms=(time.perf_counter()-start)*1000))
    r.update(scope='HDR CPU endpoint only; exact comparisons against HEAD, no export/GPU speed claim',
        baseline_sha256=hashlib.sha256(source.encode()).hexdigest(),checks=checks,timing=timing,
        gate=all(c['equal'] for c in checks),summary={str(m):{p:statistics.median(t['ms'] for t in timing if t['mix']==m and t['path']==p)
         for p in ('old','fast')} for m in (0,1)})


def contract(args,r):
    import cv2
    import numpy as np
    import torch
    from scripts.gpu_color_probe import bind
    from scripts.encode_acceptance_nvenc_contract import native_encode,pack_nv12,nv12_pitch,encode_ffmpeg,FMT_ARGB,FMT_ABGR,FMT_NV12,POLICY_HQ
    from scripts.encode_acceptance_nvenc_ring import ffmpeg_raw,native_ring_encode,encode_ffmpeg_raw
    from dlss5tool.video_export import find_ffmpeg
    ffmpeg=find_ffmpeg();w,h=360,640;cap=cv2.VideoCapture(str(args.source));fps=cap.get(cv2.CAP_PROP_FPS)
    frames=[]
    try:
        for _ in range(24):
            ok,bgr=cap.read()
            if not ok:raise RuntimeError('source too short')
            frames.append(np.ascontiguousarray(cv2.resize(bgr,(w,h),interpolation=cv2.INTER_AREA)))
    finally:cap.release()
    def decode(data,raw=False):
        prefix=['-f','h264'] if raw else []
        output=command([ffmpeg,'-hide_banner','-loglevel','error',*prefix,'-i','pipe:0' if raw else str(data),
            '-map','0:v:0','-f','rawvideo','-pix_fmt','rgb24','-'],data if raw else None)
        if len(output)!=24*w*h*3:raise RuntimeError('decoded count mismatch')
        return np.frombuffer(output,np.uint8).reshape(24,h,w,3)
    r.update(scope='24 frames 360x640 encoder-contract retest; no DLSS; native HQ single-slot/ring vs production FFmpeg',
             frames=24,dimensions=[w,h],fps=fps,cases={},source_pixels_sha256=hashlib.sha256(b''.join(f.tobytes() for f in frames)).hexdigest())
    production=args.work/'production.mp4';repeat=args.work/'production-repeat.mp4'
    encode_ffmpeg(production,frames,fps,True);encode_ffmpeg(repeat,frames,fps,True)
    reference=decode(production);reference_repeat=decode(repeat)
    r['baseline_repeat']=compare(reference,reference_repeat)
    if not r['baseline_repeat']['equal']:raise RuntimeError('Production baseline is not repeatable')
    raw_nv12=ffmpeg_raw(ffmpeg,frames,w,h,fps,'nv12')
    raw_i420=ffmpeg_raw(ffmpeg,frames,w,h,fps,'yuv420p')
    i420_file=args.work/'ffmpeg-i420.mp4'
    encode_ffmpeg_raw(ffmpeg,i420_file,raw_i420,w,h,fps,'yuv420p')
    r['ffmpeg_i420_vs_production']=compare(reference,decode(i420_file))
    torch.empty(0,device='cuda');cuda=C.WinDLL('nvcuda.dll');ctx=C.c_void_p();ptr=C.c_uint64()
    if bind(cuda,'cuCtxGetCurrent',[C.POINTER(C.c_void_p)])(C.byref(ctx)):raise RuntimeError('no CUDA context')
    pitch=nv12_pitch(w);size=max(w*h*4,pitch*(h+h//2))
    if bind(cuda,'cuMemAlloc_v2',[C.POINTER(C.c_uint64),C.c_size_t])(C.byref(ptr),size):raise RuntimeError('alloc failed')
    dllpath=ROOT/'tmp/encode-acceptance-20260916/native-encoder/candidate.dll';dll=C.WinDLL(str(dllpath))
    r['native_binary_sha256']=sha(dllpath)
    rgba=[np.ascontiguousarray(cv2.cvtColor(f,cv2.COLOR_BGR2RGBA)) for f in frames]
    bgra=[np.ascontiguousarray(cv2.cvtColor(f,cv2.COLOR_BGR2BGRA)) for f in frames]
    decoded={}
    cases=[('correct-bgra-argb',bgra,w*4,FMT_ARGB),('correct-rgba-abgr',rgba,w*4,FMT_ABGR),
           ('wrong-bgra-abgr-control',bgra,w*4,FMT_ABGR),
           ('nv12',[pack_nv12(x,w,h,pitch) for x in raw_nv12],pitch,FMT_NV12)]
    try:
        for name,inputs,row_pitch,fmt in cases:
            result=native_encode(dll,ctx,ptr,inputs,w,h,row_pitch,fmt,POLICY_HQ,fps)
            encoded=result.pop('encoded');(args.work/(name+'.h264')).write_bytes(encoded)
            decoded[name]=decode(encoded,True)
            r['cases'][name]={**result,'vs_production':compare(reference,decoded[name])}
        r['correct_channel_layouts_match']=compare(decoded['correct-bgra-argb'],decoded['correct-rgba-abgr'])
        r['wrong_layout_vs_correct']=compare(decoded['correct-bgra-argb'],decoded['wrong-bgra-abgr-control'])
        ringpath=ROOT/'tmp/nvenc-hq-ring-20260916/native-encoder/ring.dll'
        r['ring_binary_sha256']=sha(ringpath)
        ring=C.WinDLL(str(ringpath));result=native_ring_encode(ring,ctx,[np.frombuffer(x,np.uint8).copy() for x in raw_i420],w,h,fps)
        encoded=result.pop('encoded');(args.work/'ring.h264').write_bytes(encoded)
        r['ring']={**result,'vs_production':compare(reference,decode(encoded,True))}
    finally:bind(cuda,'cuMemFree_v2',[C.c_uint64])(ptr)
    r['promotion']='not production-equivalent' if not all(c['vs_production']['equal'] for n,c in r['cases'].items() if not n.startswith('wrong')) else 'transport/encoder checks only'


def hdr(args,r):
    import numpy as np
    import torch
    from dlss5tool.video_export import find_ffmpeg,find_ffprobe,probe_video_stream,FFmpegHDRVideoReader,FFmpegVideoWriter,compose_hdr_frame
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.guidance_color import analysis_rgba8
    from scripts.gpu_pipeline_candidates import analysis_hdr,compose_hdr
    ffmpeg=find_ffmpeg();r.update(scope='actual production HDR decode -> DLSS -> CPU mix .7 -> HEVC Main10, synthetic fixtures; separate GPU arithmetic audit',cases=[])
    for name in ('hdr10','hlg'):
        source=ROOT/('tmp/hdr-e2e/source-'+name+'.mp4');meta=probe_video_stream(ffmpeg,str(source))
        w,h=meta['width'],meta['height'];fps=meta['fps'];settings=dict(host_backend='v2',host_auto_fallback=False,
            host_submission='compatibility',host_in_flight=1,host_persistent_buffers=True,host_zero_fast_path=True,
            guidance_mode=0,frame_format='rgba16f',color_profile=meta['profile'],color_primaries=meta['color_primaries'],
            style=0,intensity=1.,local_tone=1.,local_struct=1.,skin_struct=1.,use_auto_mask=True)
        case=dict(profile=meta['profile'],fixture=str(source),source_sha256=sha(source),input_metadata=meta,frames=[],output_files=[])
        r['cases'].append(case)
        live=ProcessLive(w,h,settings);reader=None;writer=None
        try:
            for repeat in range(2):
                output=args.work/f'{args.label+"-" if args.label else ""}{name}-production-{repeat}.mp4'
                if output.exists():raise FileExistsError('Refusing to overwrite encoded HDR evidence')
                reader=FFmpegHDRVideoReader(source,w,h,meta,ffmpeg)
                writer=FFmpegVideoWriter(str(output),w,h,fps,audio_source=None,use_nvenc=True,hdr_metadata=meta)
                frame_checks=[]
                for i in range(24):
                    frame=reader.read()
                    if frame is None:raise RuntimeError('HDR fixture too short')
                    processed=live.process(frame,reset=i==0)
                    if processed is None or not np.isfinite(processed).all():raise RuntimeError('Invalid HDR DLSS output')
                    mixed=compose_hdr_frame(frame,processed,mix=.7,profile=meta['profile'])
                    writer.write(mixed)
                    item=dict(index=i,processed_sha256=hashlib.sha256(processed.tobytes()).hexdigest(),
                        mixed_sha256=hashlib.sha256(mixed.tobytes()).hexdigest())
                    if repeat==0:
                        gt=torch.from_numpy(frame).cuda();pt=torch.from_numpy(processed).cuda()
                        item['gpu_analysis']=compare(analysis_rgba8(frame,settings),analysis_hdr(gt,meta['profile']).cpu().numpy())
                        item['gpu_mix']=compare(mixed,compose_hdr(gt,pt,.7,meta['profile']).cpu().numpy())
                    frame_checks.append(item)
                reader.close();reader=None;writer.finish();writer=None
                if repeat==0:case['frames']=frame_checks
                else:case['repeat_raw_equal']=all(x['processed_sha256']==y['processed_sha256'] and x['mixed_sha256']==y['mixed_sha256'] for x,y in zip(case['frames'],frame_checks))
                data=json.loads(command([find_ffprobe(ffmpeg),'-v','error','-count_frames','-show_streams','-of','json',str(output)]))
                stream=next(s for s in data['streams'] if s['codec_type']=='video')
                case['output_files'].append(dict(file=output.name,sha256=sha(output),bytes=output.stat().st_size,
                    metadata={k:stream.get(k) for k in ('codec_name','profile','pix_fmt','color_space','color_transfer','color_primaries','color_range','nb_read_frames','duration','avg_frame_rate')}))
                if stream['codec_name']!='hevc' or stream['pix_fmt']!='yuv420p10le' or int(stream['nb_read_frames'])!=24 or stream['color_transfer']!=meta['color_transfer']:
                    raise RuntimeError('HDR output format/count mismatch')
            case['encoded_repeat_equal']=case['output_files'][0]['sha256']==case['output_files'][1]['sha256']
            case['gpu_analysis_equal']=all(f['gpu_analysis']['equal'] for f in case['frames'])
            case['gpu_mix_equal']=all(f['gpu_mix']['equal'] for f in case['frames'])
            if not case['repeat_raw_equal']:raise RuntimeError('HDR baseline temporal repeatability failed')
        finally:
            if reader:reader.close()
            if writer:writer.abort()
            live.close()
    r['promotion']='baseline supported; GPU arithmetic/10bit shared encoder NOT accepted'


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',type=Path,required=True)
    p.add_argument('--case',choices=('endpoint','contract','hdr'),required=True)
    p.add_argument('--label',help='Fresh report label; old evidence is never overwritten')
    p.add_argument('--source',type=Path,default=Path('F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4'))
    args=p.parse_args();args.work=args.work.resolve()
    if not (args.work/'TASK.md').is_file():p.error('Registered work required')
    label=args.label or args.case
    if not label.replace('-','').isalnum():p.error('Safe label required')
    output=args.work/(label+'.json')
    if output.exists():p.error('Refusing overwrite')
    os.environ['TEMP']=os.environ['TMP']=str(args.work);os.environ['PYTHONDONTWRITEBYTECODE']='1'
    report=dict(case=args.case,argv=sys.argv,script_sha256=sha(__file__),
        source_files={name:sha(ROOT/name) for name in ('dlss5tool/video_export.py','scripts/gpu_pipeline_candidates.py',
            'scripts/encode_acceptance_nvenc_contract.py','scripts/gpu_nvenc_probe.cpp','scripts/gpu_nvenc_ring.cpp')},
        git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    code=0
    try:
        globals()[args.case](args,report);report['status']='completed'
    except BaseException as exc:
        report.update(status='failed',error=repr(exc));code=1
        import traceback
        report['traceback']=traceback.format_exc();traceback.print_exc()
    finally:
        with output.open('x',encoding='utf-8') as handle:json.dump(report,handle,ensure_ascii=False,indent=2)
        print(json.dumps({k:report[k] for k in ('case','status','error','gate','summary','promotion') if k in report}),flush=True)
    raise SystemExit(code)


if __name__=='__main__':main()
