"""Isolated route/caching experiments; never imported by production.

Original RAFT, all six outputs, no final-only patch. Native timings include
guidance and DLSS but exclude decode, hashing, comparison and video encoding.
Only continuous full runs can provide full-clip throughput (not GUI playback).
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ASSETS = ROOT / 'tmp/realtime-routes-20260908'
VARIANTS = {
    'large720': ('vitl', 720, 'original'),
    'large512': ('vitl', 512, 'original'),
    'large384': ('vitl', 384, 'original'),
    'small512': ('vits', 512, 'original'),
    'small384': ('vits', 384, 'original'),
    'small384_amp': ('vits', 384, 'amp'),
    'large720_amp': ('vitl', 720, 'amp'),
    'large720_reuse': ('vitl', 720, 'reuse'),
    'small384_nvof': ('vits', 384, 'nvof'),
    'small512_nvof': ('vits', 512, 'nvof'),
    'large720_repeat': ('vitl', 720, 'original'),
    'small256_nvof': ('vits', 256, 'nvof'),
}


def digest(array):
    return hashlib.sha256(memoryview(array)).hexdigest()


def install_flow_route(model, route):
    torch, np = model.torch, model.np
    if route == 'amp':
        original = model._infer_flow
        def infer(self, inputs):
            with torch.autocast('cuda', dtype=torch.float16):
                return original(inputs).float()
        model._infer_flow = types.MethodType(infer, model)
    elif route == 'reuse':
        # Cache normalized GPU image and feature of the previous input. Compare
        # source object identity, so seek/cut/changed sequence cannot reuse it.
        state = {}
        original_encoder = model.flow.feature_encoder.forward
        def prepare(self, small):
            previous = state.get('current') if state.get('source') is self.prev else None
            if previous is None:
                state.pop('feature', None)
                previous = torch.from_numpy(np.ascontiguousarray(self.prev)).permute(2,0,1).float()[None] / 255.0
                previous = (previous * 2 - 1).to(self.device)
            current = torch.from_numpy(np.ascontiguousarray(small)).permute(2,0,1).float()[None] / 255.0
            current = (current * 2 - 1).to(self.device)
            state.update(current=current, source=small)
            return (previous, current) if self.settings['guidance_flow_direction'] == 'forward_negated' else (current, previous)
        def encode(images):
            forward = model.settings['guidance_flow_direction'] == 'forward_negated'
            current = images[1:] if forward else images[:1]
            previous = images[:1] if forward else images[1:]
            prev_feature = state.get('feature')
            if prev_feature is None:
                prev_feature = original_encoder(previous)
            current_feature = original_encoder(current)
            state['feature'] = current_feature
            return torch.cat((prev_feature, current_feature) if forward else (current_feature, prev_feature))
        # The GPU cache is created on the default stream, so this experimental
        # route uses serial scheduling. Do not cross streams without events.
        model.flow_stream = model.depth_stream = None
        model._flow_input = types.MethodType(prepare, model)
        model.flow.feature_encoder.forward = encode
    elif route == 'nvof':
        from scripts.nvof_probe import OpticalFlow
        model._nvof = None
        def prepare(self, small):
            return (self.prev, small) if self.settings['guidance_flow_direction'] == 'forward_negated' else (small, self.prev)
        def infer(self, inputs):
            first, second = inputs
            h, w = first.shape[:2]
            if self._nvof is None:
                self._nvof = OpticalFlow(w, h)
            # CPU tensor is intentional: isolated SDK baseline includes both
            # uploads and readback. Full-resolution pixel units, not grid units.
            values = self._nvof.calculate(first, second)
            return torch.from_numpy(values).permute(2,0,1)[None]
        model._flow_input = types.MethodType(prepare, model)
        model._infer_flow = types.MethodType(infer, model)


class RawCache:
    """Experimental in-memory low-resolution RAW predictions, not normalized depth.

    Per-model instance fixes all model/config identities. Content hashes identify
    frames and ordered pairs. Production needs bounded storage and a versioned
    persistent namespace; this deliberately is not wired to the application.
    """
    def __init__(self, model, indexed=False):
        self.depth, self.flow = {}, {}
        self.percentiles = {}
        self.hits = self.misses = 0
        self.enabled = True
        self.current = self.previous = None
        self.namespace = tuple(sorted((k,repr(v)) for k,v in model.settings.items()
            if k.startswith('guidance_') or k in ('flow_weights','depth_weights')))
        original_process = model.process
        original_depth, original_flow = model._infer_depth, model._infer_flow
        original_depth_input, original_flow_input = model._depth_input, model._flow_input
        def process(frame, reset, outputs=None):
            namespace = tuple(sorted((k,repr(v)) for k,v in model.settings.items()
                if k.startswith('guidance_') or k in ('flow_weights','depth_weights')))
            if namespace != self.namespace:
                raise RuntimeError('Experimental raw cache requires a new model/cache for changed guidance settings')
            token=getattr(model,'cache_frame_token',None) if indexed else None
            self.current = token if token is not None else digest(frame)
            result = original_process(frame, reset, outputs)
            self.previous = self.current
            return result
        def depth(tensor):
            key = self.current
            if self.enabled and key in self.depth:
                self.hits += 1
                return model.torch.from_numpy(self.depth[key])
            self.misses += 1
            result = original_depth(tensor).cpu()
            if self.enabled: self.depth[key] = result.numpy().copy()
            return result
        def flow(inputs):
            key = (self.previous, self.current)
            if self.enabled and key in self.flow:
                self.hits += 1
                return model.torch.from_numpy(self.flow[key])
            self.misses += 1
            result = original_flow(inputs).cpu()
            if self.enabled: self.flow[key] = result.numpy().copy()
            return result
        model.process, model._infer_depth, model._infer_flow = process, depth, flow
        def depth_input(*args):
            return None if self.enabled and self.current in self.depth else original_depth_input(*args)
        def flow_input(*args):
            return None if self.enabled and (self.previous,self.current) in self.flow else original_flow_input(*args)
        model._depth_input,model._flow_input=depth_input,flow_input
        if indexed:
            def finish_depth(prediction,dp,reset,w,h):
                np,cv2=model.np,model.cv2
                values=prediction.cpu().numpy()
                if not np.isfinite(values).all(): raise RuntimeError('Nonfinite cached depth')
                key=self.current
                if self.enabled and key in self.percentiles:
                    bounds=self.percentiles[key]
                else:
                    bounds=tuple(map(float,np.percentile(values,[1,99])))
                    if self.enabled: self.percentiles[key]=bounds
                bounds=bounds if reset or model.depth_range is None else tuple(.9*a+.1*b for a,b in zip(model.depth_range,bounds))
                low,high=bounds
                values=np.clip((values-low)/max(high-low,1e-6),0,1)
                cv2.resize(values,(w,h),dst=dp,interpolation=cv2.INTER_LINEAR)
                return bounds
            model._finish_depth=finish_depth
        # CPU cache lookup is a separate path, not overlap GPU event timings.
        model.flow_stream = model.depth_stream = None

    @property
    def bytes(self):
        return sum(x.nbytes for x in (*self.flow.values(), *self.depth.values()))


def run(args):
    sys.path.insert(0, str(ROOT / 'tmp/dlss5standaloneV2/models'))
    import cv2
    import numpy as np
    import torch
    import dlss_engine
    from guidance_worker import Models
    if args.threads:
        torch.set_num_threads(args.threads)
        cv2.setNumThreads(args.threads)
    encoder, edge, route = VARIANTS[args.variant]
    directory = args.output / args.variant
    directory.mkdir(parents=True, exist_ok=False)
    cap = cv2.VideoCapture(str(args.source))
    if not cap.isOpened():
        raise RuntimeError('Cannot open source')
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if args.full and n>600: raise ValueError('This bounded probe accepts full clips of at most 600 frames')
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    windows = [(0, n)] if args.full else [(x, min(x + 12, n)) for x in (0, max(0,n//2-6), max(0,n-12))]
    indices = [i for start,end in windows for i in range(start,end)]
    settings = {'guidance_mode': 3, 'guidance_device': 'cuda', 'guidance_edge': edge,
        'guidance_cache_mb': 0,  # Keep historical experiments independent of production cache.
        'guidance_depth_encoder': encoder, 'guidance_depth_profile': 'sdpa_fp16',
        'guidance_flow_direction': args.direction, 'guidance_execution': 'raft_streams',
        'flow_weights': str(ROOT/'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
        'depth_weights': str(ASSETS/'depth_anything_v2_vits.pth' if encoder == 'vits' else ROOT/'mods/models/depth_anything_v2_vitl.pth'),
        'guidance_flow_weights': str(ROOT/'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
        'guidance_depth_weights': str(ASSETS/'depth_anything_v2_vits.pth' if encoder == 'vits' else ROOT/'mods/models/depth_anything_v2_vitl.pth'),
        'mods_directory': str(ROOT/'mods'), 'host_backend':'v2', 'host_auto_fallback':False,
        'host_in_flight':1, 'host_persistent_buffers':True, 'host_submission':args.submission,
        'style':0, 'intensity':1.0, 'local_tone':0., 'local_struct':1., 'skin_struct':1., 'use_auto_mask':1,
        'ui_correction':0}
    # Do not allocate a RAFT model when hardware optical flow replaces it.
    model = Models({**settings, 'guidance_mode': 2} if route == 'nvof' else settings)
    if route == 'nvof':
        model.flow = True
    install_flow_route(model, route)
    cache = RawCache(model,indexed=args.cache_indexed) if args.cache else None
    source_stat=args.source.stat()
    source_identity=(str(args.source.resolve()),source_stat.st_size,source_stat.st_mtime_ns)
    if args.cache_indexed:
        with args.source.open('rb') as handle: source_identity=(*source_identity,hashlib.file_digest(handle,'sha256').hexdigest())
    class Guidance:
        info = {'device':'cuda','device_name':model.device_name,'precision':model.precision}
        mv = np.empty((h,w,2),np.float32)
        dp = np.empty((h,w),np.float32)
        @property
        def last_metrics(self): return model.last_metrics
        def process(self, frame, reset=False, *, copy_outputs=False):
            return model.process(frame, reset, (self.mv,self.dp))
        def close(self): pass
    dlss_engine.LOG_PATH = str(directory/'ngx.log')
    live = dlss_engine.Live(w,h,settings)
    local = Guidance()
    live._guidance = local
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ok, bgr = cap.read()
    if not ok: raise RuntimeError('Empty source')
    frame = cv2.cvtColor(bgr,cv2.COLOR_BGR2RGBA)
    tick = time.perf_counter()
    for i in range(4): live.process(frame, reset=i==0)
    warmup = time.perf_counter()-tick
    if cache:
        cache.depth.clear(); cache.flow.clear();cache.percentiles.clear()
    reference_path = args.output/'reference.npy'
    is_reference = args.variant == 'large720' and not reference_path.exists() and not args.no_reference
    reference = np.lib.format.open_memmap(reference_path,mode='w+',dtype=np.uint8,shape=(len(indices),h,w,4)) if is_reference else (np.load(reference_path,mmap_mode='r') if reference_path.exists() else None)
    raw_path=args.output/'guidance_sampled8.npy'
    raw_shape=(len(indices),(h+7)//8,(w+7)//8,3)
    raw_reference=np.lib.format.open_memmap(raw_path,mode='w+',dtype=np.float32,shape=raw_shape) if is_reference else (np.load(raw_path,mmap_mode='r') if raw_path.exists() else None)
    replay_ref = None
    passes = [('cold',1.),('warm_same',1.),('warm_changed',.55),('warm_changed_repeat',.55),('uncached_changed',.55)] if cache else [('normal',1.)]
    records=[]
    for pass_name,intensity in passes:
        if args.cache_indexed:
            current_stat=args.source.stat()
            if (current_stat.st_size,current_stat.st_mtime_ns) != (source_stat.st_size,source_stat.st_mtime_ns):
                raise RuntimeError('Source changed during indexed cache experiment')
        live.update({'intensity':intensity})
        if cache: cache.enabled = pass_name != 'uncached_changed'
        before_hits = cache.hits if cache else 0
        before_misses = cache.misses if cache else 0
        times=[]; samples=[]; sample=0
        previous_output=None; previous_source=None
        if pass_name in ('cold','warm_changed'):
            replay_ref = []
        writer = None
        if args.video and pass_name in ('normal','warm_same','warm_changed'):
            writer=cv2.VideoWriter(str(directory/(pass_name+'.mp4')),cv2.VideoWriter_fourcc(*'mp4v'),fps,(w,h))
            if not writer.isOpened(): raise RuntimeError('Cannot open comparison video')
        total_start = time.perf_counter()
        for start,end in windows:
            cap.set(cv2.CAP_PROP_POS_FRAMES,start)
            previous_output=previous_source=None
            for index in range(start,end):
                ok,bgr=cap.read()
                if not ok: raise RuntimeError(f'Decode failure at {index}')
                frame=cv2.cvtColor(bgr,cv2.COLOR_BGR2RGBA)
                if args.cache_indexed: model.cache_frame_token=(source_identity,index)
                tick=time.perf_counter()
                result=live.process(frame,reset=index==start)
                elapsed=(time.perf_counter()-tick)*1000
                if result is None: raise RuntimeError('Missing DLSS output')
                if args.speed_only:
                    times.append(elapsed)
                    samples.append({'source_frame':index,'process_ms':elapsed,'guidance_ms':model.last_metrics['inference_ms']})
                    sample+=1
                    if sample%60==0: print(args.variant,pass_name,sample,len(indices),flush=True)
                    continue
                output=result.copy()
                # Explicitly include every frame for full-clip throughput;
                # sampled runs exclude three warmup frames per window.
                if args.full or index-start>=3: times.append(elapsed)
                if not np.isfinite(local.mv).all() or not np.isfinite(local.dp).all():
                    raise RuntimeError('Nonfinite guidance')
                comparison=reference[sample] if reference is not None else output
                if is_reference and pass_name in ('normal','cold'):
                    reference[sample]=output
                    comparison=output
                delta=np.abs(output[...,:3].astype(np.int16)-comparison[...,:3].astype(np.int16))
                item={'source_frame':index,'process_ms':elapsed,'rgb_mae':float(delta.mean()),'rgb_max':int(delta.max()),
                    'output_sha256':digest(output),'flow_sha256':digest(local.mv),'depth_sha256':digest(local.dp),
                    'guidance_ms':model.last_metrics['inference_ms']}
                if raw_reference is not None:
                    if is_reference and pass_name in ('normal','cold'):
                        raw_reference[sample,:,:,:2]=local.mv[::8,::8]
                        raw_reference[sample,:,:,2]=local.dp[::8,::8]
                    flow_difference=local.mv[::8,::8]-raw_reference[sample,:,:,:2]
                    item['flow_sampled8_epe_vs_baseline']=float(np.linalg.norm(flow_difference,axis=-1).mean())
                    item['depth_sampled8_mae_vs_baseline']=float(np.abs(local.dp[::8,::8]-raw_reference[sample,:,:,2]).mean())
                if replay_ref is not None:
                    if pass_name in ('cold','warm_changed'): replay_ref.append(item['output_sha256'])
                    else: item['replay_exact']=item['output_sha256']==replay_ref[sample]
                if previous_output is not None:
                    residual=output[...,:3].astype(np.float32)-frame[...,:3]
                    old_residual=previous_output[...,:3].astype(np.float32)-previous_source[...,:3]
                    # Diagnostic only, no ground-truth motion: not a perceptual score.
                    item['unwarped_residual_change']=float(np.abs(residual-old_residual).mean())
                previous_output,previous_source=output,frame
                samples.append(item)
                if writer: writer.write(cv2.cvtColor(output,cv2.COLOR_RGBA2BGR))
                if index in (start+6,end-1) and pass_name in ('normal','warm_changed'):
                    cv2.imwrite(str(directory/f'{pass_name}-{index:03}.png'),cv2.cvtColor(output,cv2.COLOR_RGBA2BGRA))
                sample+=1
                if sample%30==0: print(args.variant,pass_name,sample,len(indices),flush=True)
        if writer: writer.release()
        record={'pass':pass_name,'frames':sample,'timed_frames':len(times),
            'core_fps':1000/float(np.mean(times)),'mean_ms':float(np.mean(times)),
            'p50_ms':float(np.percentile(times,50)),'p95_ms':float(np.percentile(times,95)),
            'p99_ms':float(np.percentile(times,99)),'max_ms':max(times),
            'over_33ms':sum(t>1000/30 for t in times),
            'loop_with_decode_hash_compare_encode_seconds':time.perf_counter()-total_start,
            'peak_allocated_mib':torch.cuda.max_memory_allocated()/1048576,
            'cache_hits':cache.hits-before_hits if cache else 0,'cache_misses':cache.misses-before_misses if cache else 0,
            'cache_mib':cache.bytes/1048576 if cache else 0,
            'rgb_mae':float(np.mean([x.get('rgb_mae',0) for x in samples])) if not args.speed_only and reference is not None else None,
            'rgb_max':max(x.get('rgb_max',0) for x in samples) if not args.speed_only and reference is not None else None,
            'flow_sampled8_epe_vs_baseline':float(np.mean([x.get('flow_sampled8_epe_vs_baseline',0) for x in samples])) if raw_reference is not None else None,
            'depth_sampled8_mae_vs_baseline':float(np.mean([x.get('depth_sampled8_mae_vs_baseline',0) for x in samples])) if raw_reference is not None else None,
            'all_replay_exact':all(x.get('replay_exact',False) for x in samples) if pass_name in ('warm_same','warm_changed_repeat','uncached_changed') else None,
            'samples':samples}
        records.append(record)
        if args.speed_only:
            record['decode_process_fps']=sample/record['loop_with_decode_hash_compare_encode_seconds']
        report={'source':str(args.source),'dimensions':[w,h],'source_frames':n,'source_fps':fps,
            'full':args.full,'variant':args.variant,'settings':settings,'route':route,
            'actual_schedule':'dual_stream' if model.flow_stream is not None else 'serial',
            'warmup_seconds':warmup,'torch':torch.__version__,'device':model.device_name,'passes':records}
        report.update(torch_threads=torch.get_num_threads(),opencv_threads=cv2.getNumThreads(),speed_only=args.speed_only)
        report.update(cache_indexed=args.cache_indexed,source_identity=source_identity)
        report.update(rgb_reference_available=reference is not None,raw_reference_available=raw_reference is not None)
        report['source_stat']={'size':args.source.stat().st_size,'mtime_ns':args.source.stat().st_mtime_ns}
        (directory/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in record.items() if k!='samples'}),flush=True)
    cap.release()
    # Isolated process exit owns NGX cleanup; avoids historical shutdown hangs.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--variant',choices=VARIANTS)
    parser.add_argument('--variants',nargs='+',choices=VARIANTS,default=list(VARIANTS)[:8])
    parser.add_argument('--direction',choices=['backward','forward_negated'],default='forward_negated')
    parser.add_argument('--full',action='store_true')
    parser.add_argument('--cache',action='store_true')
    parser.add_argument('--video',action='store_true')
    parser.add_argument('--no-reference',action='store_true',help='No full-resolution baseline array, useful for full-clip cache tests')
    parser.add_argument('--speed-only',action='store_true',help='Full decode/process only; no output comparisons, hashes or encoding')
    parser.add_argument('--threads',type=int,default=0,help='Optional explicit torch/OpenCV CPU thread cap, zero keeps defaults')
    parser.add_argument('--submission',choices=['compatibility','merged'],default='compatibility')
    parser.add_argument('--cache-indexed',action='store_true',help='Use verified source identity/frame index, cache depth percentiles as well')
    args=parser.parse_args()
    if args.cache_indexed and not args.cache: parser.error('--cache-indexed requires --cache')
    if args.speed_only:
        if args.cache or args.video or not args.full: parser.error('--speed-only requires --full and no --cache/--video')
        args.no_reference=True
    if args.variant:
        run(args);return
    args.output.mkdir(parents=True,exist_ok=False)
    results=[]
    for variant in args.variants:
        gpu=subprocess.run(['nvidia-smi','--query-gpu=name,utilization.gpu,memory.used,memory.total','--format=csv,noheader'],
            capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=10)
        (args.output/(variant+'-gpu-before.txt')).write_text(gpu.stdout+gpu.stderr,encoding='utf-8')
        command=[sys.executable,__file__,'--source',str(args.source),'--output',str(args.output),
            '--variant',variant,'--direction',args.direction]
        command += ['--threads',str(args.threads),'--submission',args.submission]
        command += [flag for flag,value in (('--full',args.full),('--cache',args.cache),('--video',args.video),('--no-reference',args.no_reference),('--speed-only',args.speed_only),('--cache-indexed',args.cache_indexed)) if value]
        try:
            completed=subprocess.run(command,timeout=600,creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
            (args.output/(variant+'.log')).write_text(completed.stdout,encoding='utf-8')
            result_file=args.output/variant/'result.json'
            if completed.returncode or not result_file.exists():
                raise RuntimeError(completed.stdout[-3000:])
            result=json.loads(result_file.read_text(encoding='utf-8'))
            compact={k:v for k,v in result.items() if k!='passes'}
            compact['passes']=[{k:v for k,v in p.items() if k!='samples'} for p in result['passes']]
            results.append(compact)
            print(json.dumps({'variant':variant,'passes':compact['passes']},ensure_ascii=True),flush=True)
        except Exception as error:
            results.append({'variant':variant,'error':str(error)})
            print(variant,str(error),flush=True)
        (args.output/'report.json').write_text(json.dumps(results,indent=2),encoding='utf-8')


if __name__=='__main__': main()
