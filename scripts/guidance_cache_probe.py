"""Frozen production component and ProcessLive: full-clip cache A/B.

New output directory only. Never touches user settings/source. Five sequential
passes in one session, then a fresh cache-disabled control. Output hashes taken
before encoding (no encoding here); core timing excludes decode and hash.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cv2
import numpy as np
from dlss_host_process import ProcessLive


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--component',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--frames',type=int,default=243)
    p.add_argument('--execution',choices=['serial','raft_streams'],default='raft_streams')
    p.add_argument('--direction',choices=['backward','forward_negated'],default='forward_negated')
    p.add_argument('--shared',action='store_true')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    cap=cv2.VideoCapture(str(args.source))
    w,h=int(cap.get(3)),int(cap.get(4));n=min(args.frames,int(cap.get(7)))
    if not cap.isOpened() or not 1<=n<=600:raise ValueError('Need a bounded readable video')
    settings={'guidance_mode':3,'guidance_device':'cuda','guidance_edge':720,
        'guidance_depth_encoder':'vitl','guidance_depth_profile':'sdpa_fp16',
        'guidance_execution':args.execution,'guidance_flow_direction':args.direction,
        'guidance_cache_mb':2048,'mods_directory':str(args.component.resolve().parent),
        'guidance_flow_weights':str(ROOT/'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
        'guidance_depth_weights':str(ROOT/'mods/models/depth_anything_v2_vitl.pth'),
        'host_backend':'v2','host_auto_fallback':False,'host_in_flight':1,
        'host_submission':'compatibility','host_persistent_buffers':True,
        'style':0,'intensity':1.,'local_tone':0.,'local_struct':1.,'skin_struct':1.,'use_auto_mask':1}
    reports=[];live=None
    pool=None
    if args.shared:
        from shared_cache_budget import SharedCacheBudget
        pool=SharedCacheBudget(limit_bytes=1024*1048576)
        settings['guidance_cache_pool']=pool.name
        # Simulate an actually allocated GUI cache consumer, not a fixed reserve.
        frame_cache=np.zeros((32*1048576,),np.uint8)
        with pool.locked():pool.publish_locked(frame_cache.nbytes)
    try:
        live=ProcessLive(w,h,settings)
        for name,intensity in [('cold',1.),('warm_same',1.),('warm_changed',.55),('warm_repeat',.55),('uncached_changed',.55)]:
            if name=='uncached_changed':
                # New worker via settings contract, no cache and no old model state.
                live.update({'guidance_cache_mb':0,'guidance_cache_pool':None})
            live.update({'intensity':intensity})
            cap.set(cv2.CAP_PROP_POS_FRAMES,0)
            times=[];hashes=[];metrics=[]
            for i in range(n):
                ok,bgr=cap.read()
                if not ok:raise RuntimeError(f'Decode failed {i}')
                frame=cv2.cvtColor(bgr,cv2.COLOR_BGR2RGBA)
                tick=time.perf_counter();out=live.process(frame,reset=i==0)
                times.append((time.perf_counter()-tick)*1000)
                hashes.append(hashlib.sha256(memoryview(out)).hexdigest())
                metrics.append(live.guidance_metrics)
                if i%60==0:print(name,i,n,flush=True)
            info=live.guidance_info
            assert info.get('cache_version') in ('raw_lru_v1','raw_lru_v2_shared'),info
            calls=sum(m['depth_model_calls']+m['flow_model_calls'] for m in metrics)
            result={'pass':name,'frames':n,'core_fps_excluding_first':1000/np.mean(times[1:]),
                'first_frame_ms':times[0],'p95_ms_excluding_first':float(np.percentile(times[1:],95)),
                'model_calls':calls,'hits':sum(int(m['cache_flow_hit'])+int(m['cache_depth_hit']) for m in metrics),
                'cache_mib':metrics[-1]['cache_bytes']/1048576,'info':info,'hashes':hashes,'frame_metrics':metrics}
            if name=='warm_same':result['equal']=hashes==reports[0]['hashes']
            if name in ('warm_repeat','uncached_changed'):result['equal']=hashes==reports[2]['hashes']
            if name.startswith('warm'):assert calls==0,result
            reports.append(result)
            if pool:
                result['shared_pool']=pool.snapshot()
                assert result['shared_pool']['used_bytes']<=result['shared_pool']['limit_bytes']
            (args.output/'report.json').write_text(json.dumps(reports,indent=2),encoding='utf-8')
            print(json.dumps({k:v for k,v in result.items() if k not in ('hashes','frame_metrics')}),flush=True)
        if not all(r.get('equal',True) for r in reports):raise AssertionError('Native replay differs; see retained report')
        if pool:
            # New shared model, accumulate a few frames then shrink while idle.
            live.update({'guidance_cache_pool':pool.name})
            cap.set(cv2.CAP_PROP_POS_FRAMES,0)
            for i in range(min(12,n)):
                ok,bgr=cap.read();assert ok
                live.process(cv2.cvtColor(bgr,cv2.COLOR_BGR2RGBA),reset=i==0)
            before=pool.snapshot()
            pool.set_limit(frame_cache.nbytes)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and pool.snapshot()['used_bytes']>frame_cache.nbytes:
                time.sleep(.1)
            after=pool.snapshot()
            assert after['used_bytes']==frame_cache.nbytes,after
            live.close();live=None
            assert pool.snapshot()['used_bytes']==frame_cache.nbytes
            with pool.locked():pool.publish_locked(0)
            frame_cache=None
            assert pool.snapshot()['used_bytes']==0
            (args.output/'reclaim.json').write_text(json.dumps({'before':before,'after_shrink':after,
                'after_close':pool.snapshot()},indent=2),encoding='utf-8')
    finally:
        if live:live.close()
        if pool:pool.close()
        cap.release()


if __name__=='__main__':main()
