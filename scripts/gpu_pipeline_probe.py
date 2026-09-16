"""Bounded research A/B; never imports candidates into the production app.

Run with the existing CUDA Python and -B. Register TASK.md first. Reports are
exclusive-create. GPU timings synchronize; no hidden asynchronous claims.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(array):
    import numpy as np
    return hashlib.sha256(memoryview(np.ascontiguousarray(array))).hexdigest()


def difference(a, b):
    import numpy as np
    delta = b.astype(np.float64)-a.astype(np.float64)
    return dict(changed=int(np.count_nonzero(a != b)), values=a.size,
                bitwise_equal=digest(a) == digest(b), max_abs=float(np.max(np.abs(delta))),
                mean_abs=float(np.mean(np.abs(delta))), mean_bias=float(delta.mean()))


def time_call(fn, torch, rounds=8):
    fn()
    torch.cuda.synchronize()
    samples = []
    for _ in range(rounds):
        start = time.perf_counter()
        fn()
        torch.cuda.synchronize()
        samples.append((time.perf_counter()-start)*1000)
    return dict(median_ms=statistics.median(samples), mean_ms=statistics.mean(samples), samples_ms=samples)


def raft(a, report, torch):
    import cv2
    import numpy as np
    from dlss5tool import dlss_engine as engine
    from dlss5tool.guidance_worker import Models
    from dlss5tool.guidance_inputs import clear_flow_inputs
    from scripts.gpu_pipeline_candidates import ResidentInputs
    cap = cv2.VideoCapture(str(a.source))
    try:
        for _ in range(a.start):
            if not cap.grab():
                raise ValueError('Source too short')
        frames = []
        for _ in range(a.frames):
            ok, bgr = cap.read()
            if not ok:
                raise ValueError('Source too short')
            frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA))
    finally:
        cap.release()
    h, w = frames[0].shape[:2]
    settings = dict(host_backend='v2', host_auto_fallback=False, host_submission='compatibility',
                    host_in_flight=1, host_zero_fast_path=False, host_persistent_buffers=True,
                    guidance_mode=0, style=0, intensity=1., local_tone=1., local_struct=1.,
                    skin_struct=1., use_auto_mask=True)
    engine.LOG_PATH = str(a.work/(a.label+'-ngx.log'))
    live = engine.Live(w, h, settings)
    engine._set_options(live._lib, {**settings, 'guidance_mode': 1})
    model = Models(dict(guidance_mode=1, guidance_device='cuda', guidance_flow_edge=a.edge,
                        guidance_flow_updates=6, guidance_cache_mb=a.cache_mb,
                        guidance_flow_direction=a.direction,
                        flow_weights=str(ROOT/'tmp/dlss5standaloneV2/torch_home/hub/checkpoints/raft_large_C_T_SKHT_V2-ff5fadd5.pth')))
    original = model._infer_flow
    resident = ResidentInputs('cuda')
    def candidate(inputs):
        return original(resident.pair(inputs))
    output = np.empty_like(frames[0])
    reset_at = {0, len(frames)//2}
    def run(path):
        model._infer_flow = original if path == 'baseline' else candidate
        model.prev = model.prev_thumb = model.prev_digest = None
        clear_flow_inputs(model)
        resident.clear()
        uploads = resident.uploads
        records = []
        for i, frame in enumerate(frames):
            started = time.perf_counter()
            motion, depth, reset = model.process(frame, i in reset_at)
            if not live._lib.dlssnr_process(frame.ctypes.data, motion.ctypes.data, None,
                                           output.ctypes.data, int(reset)):
                raise RuntimeError('DLSS failed')
            elapsed = (time.perf_counter()-started)*1000
            records.append(dict(frame=i, reset=reset, ms=elapsed, flow_sha256=digest(motion),
                                output_sha256=digest(output), rgb_mean=output[..., :3].mean(axis=(0, 1)).tolist(),
                                cache_hit=model.last_metrics['cache_flow_hit']))
        return dict(path=path, frames=records, mean_ms=statistics.mean(x['ms'] for x in records),
                    steady_mean_ms=statistics.mean(x['ms'] for x in records if not x['reset']),
                    candidate_uploads=resident.uploads-uploads)
    report.update(scope='same-process real RAFT + CPU flow transport + real DLSS; no decode/encode/IPC',
                  dimensions=[w,h], direction=a.direction, cache_mb=a.cache_mb, reset_frames=sorted(reset_at),
                  cases=[], opencv=cv2.__version__, adapter=live.adapter_info,
                  host_sha256=hashlib.sha256(Path(engine.HOST_DLL_V2).read_bytes()).hexdigest())
    try:
        run('baseline')
        baseline = run('baseline')
        report['reference'] = baseline
        for repeat in range(a.rounds):
            for path in (('baseline', 'resident') if repeat % 2 == 0 else ('resident', 'baseline')):
                result = run(path)
                result['repeat'] = repeat
                result['flow_changed_frames'] = [x['frame'] for x,y in zip(result['frames'], baseline['frames']) if x['flow_sha256'] != y['flow_sha256']]
                result['output_changed_frames'] = [x['frame'] for x,y in zip(result['frames'], baseline['frames']) if x['output_sha256'] != y['output_sha256']]
                report['cases'].append(result)
                print(json.dumps({k:v for k,v in result.items() if k!='frames'}), flush=True)
                if result['flow_changed_frames'] or result['output_changed_frames']:
                    raise RuntimeError('Baseline repeatability/candidate equality gate failed; stop promotion')
        report['summary'] = {path: statistics.mean(x['mean_ms'] for x in report['cases'] if x['path']==path)
                             for path in ('baseline','resident')}
    finally:
        resident.clear()
        model.close()
        live.close_guidance()
        # Caller exits this disposable process; no known-hanging NGX Shutdown.


def post(a, report, torch):
    import numpy as np
    from dlss5tool.video_export import compose_hdr_frame
    from dlss5tool.guidance_color import analysis_rgba8
    from scripts.gpu_pipeline_candidates import compose_hdr, analysis_hdr
    rng = np.random.default_rng(715)
    original = rng.random((a.height, a.width, 4), dtype=np.float32).astype(np.float16)
    processed = np.clip(original.astype(np.float32)*.95+.035, 0, 1).astype(np.float16)
    original[...,3] = processed[...,3] = 1
    gt, pt = torch.from_numpy(original).cuda(), torch.from_numpy(processed).cuda()
    report.update(scope='synthetic full-size HDR operation isolation; GPU-resident vs CPU vs CPU-bounce; not end-to-end', cases=[])
    for profile in ('hdr10_pq','hdr10_hlg'):
        settings = dict(frame_format='rgba16f',color_profile=profile,color_primaries='bt2020')
        operations = [
            ('analysis', lambda: analysis_rgba8(original, settings), lambda: analysis_hdr(gt, profile)),
            ('mix-0.7', lambda: compose_hdr_frame(original, processed, mix=.7, profile=profile),
             lambda: compose_hdr(gt, pt, .7, profile)),
            ('mix-1', lambda: compose_hdr_frame(original, processed, mix=1, profile=profile),
             lambda: compose_hdr(gt, pt, 1, profile))]
        for name,cpu,gpu in operations:
            expected, actual = cpu(), gpu().cpu().numpy()
            record = dict(profile=profile, operation=name, quality=difference(expected, actual),
                          cpu=time_call(cpu, torch, 5), resident_gpu=time_call(gpu, torch, 5))
            if name == 'mix-0.7':
                record['cpu_to_gpu_to_cpu'] = time_call(lambda: compose_hdr(torch.from_numpy(original).cuda(),
                    torch.from_numpy(processed).cuda(), .7, profile).cpu().numpy(), torch, 5)
            report['cases'].append(record)
            print(json.dumps(record),flush=True)
    report['promotion_gate'] = 'pass' if all(x['quality']['bitwise_equal'] for x in report['cases']) else 'rejected_numerical_differences'


def depth(a, report, torch):
    import numpy as np
    import cv2
    from scripts.gpu_pipeline_candidates import depth_finish,opencv_float_resize
    from dlss5tool.guidance_worker import Models
    sys.path.insert(0, str(ROOT/'tmp/dlss5standaloneV2/models'))
    model = Models(dict(guidance_mode=2, guidance_device='cuda', guidance_depth_encoder='vits',
                        guidance_depth_edge=518, guidance_cache_mb=0,
                        depth_weights=str(ROOT/'tmp/realtime-routes-20260908/depth_anything_v2_vits.pth')))
    cap = cv2.VideoCapture(str(a.source))
    report.update(scope='real depth model raw predictions; isolate CPU vs GPU percentiles/temporal normalization/resize', cases=[])
    prior = None
    try:
        for _ in range(a.start):
            cap.grab()
        for i in range(min(a.frames, 12)):
            ok, bgr = cap.read()
            if not ok:
                raise ValueError('Source too short')
            h,w = bgr.shape[:2]
            rgb=cv2.cvtColor(bgr,cv2.COLOR_BGR2RGB)
            fw,fh = 518, max(14,round(518*h/w/14)*14)
            small=cv2.resize(rgb,(fw,fh))
            with torch.inference_mode():
                prediction=model._infer_depth(model._depth_input(small,fw,fh))
            model._depth_key=None
            dp=np.empty((h,w),np.float32)
            reset=i in (0,6)
            def cpu():
                return model._finish_depth(prediction,dp,reset,w,h)
            bounds=cpu()
            actual,gpu_bounds=depth_finish(prediction,(w,h),prior,reset)
            raw = prediction.cpu().numpy()
            normalized = np.clip((raw-bounds[0])/max(bounds[1]-bounds[0],1e-6),0,1)
            # Isolate resize from percentile/normalization differences.
            exact_coords = opencv_float_resize(torch.from_numpy(normalized).cuda(),(w,h))
            record=dict(frame=i,reset=reset,quality=difference(dp,actual.cpu().numpy()),
                        coordinate_candidate_quality=difference(dp,exact_coords.cpu().numpy()),
                        cpu_bounds=list(bounds),gpu_bounds=gpu_bounds.cpu().tolist(),
                        cpu=time_call(cpu,torch,3),resident_gpu=time_call(
                            lambda: depth_finish(prediction,(w,h),prior,reset),torch,3))
            report['cases'].append(record)
            model.depth_range=bounds
            prior=gpu_bounds
            print(json.dumps(record),flush=True)
        report['promotion_gate']='pass' if all(x['quality']['bitwise_equal'] for x in report['cases']) else 'rejected_numerical_differences'
    finally:
        cap.release()
        model.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--label',required=True)
    p.add_argument('--case',choices=('raft','post','depth'),required=True)
    p.add_argument('--source',type=Path,default=ROOT/'tmp/vsr-input.mp4')
    p.add_argument('--frames',type=int,default=24)
    p.add_argument('--start',type=int,default=0)
    p.add_argument('--rounds',type=int,default=3)
    p.add_argument('--edge',type=int,default=512)
    p.add_argument('--cache-mb',type=int,default=0)
    p.add_argument('--direction',choices=('backward','forward_negated'),default='backward')
    p.add_argument('--width',type=int,default=1920)
    p.add_argument('--height',type=int,default=1080)
    a=p.parse_args(); a.work=a.work.resolve()
    if (not (a.work/'TASK.md').is_file() or not a.label.replace('-','').isalnum()
        or not 4<=a.frames<=64 or not 1<=a.rounds<=6 or not 128<=a.edge<=720
        or not 0<=a.cache_mb<=256 or not 128<=a.width<=3840 or not 128<=a.height<=2160):
        p.error('Registered task, safe label and bounded parameters required')
    path=a.work/(a.label+'.json')
    if path.exists():
        p.error('Refusing to overwrite report')
    os.environ['TEMP']=os.environ['TMP']=str(a.work)
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    import torch
    torch.cuda.init()
    report=dict(case=a.case,args={k:str(v) if isinstance(v,Path) else v for k,v in vars(a).items()},
                torch=torch.__version__,cuda=torch.version.cuda,gpu=torch.cuda.get_device_name(),
                source_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                candidate_hash=hashlib.sha256((ROOT/'scripts/gpu_pipeline_candidates.py').read_bytes()).hexdigest())
    code=0
    try:
        globals()[a.case](a,report,torch)
        report['status']='measurements_completed'
    except BaseException as error:
        report.update(status='failed',error=repr(error)); code=1
        import traceback
        traceback.print_exc()
    finally:
        report.update(peak_allocated_mib=torch.cuda.max_memory_allocated()/1048576,
                      peak_reserved_mib=torch.cuda.max_memory_reserved()/1048576)
        with path.open('x',encoding='utf-8') as out:
            json.dump(report,out,ensure_ascii=False,indent=2)
        print('Report: '+str(path),flush=True)
    os._exit(code)


if __name__=='__main__':
    main()
