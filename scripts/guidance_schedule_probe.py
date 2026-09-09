"""Historical isolated RAFT final-only and two-CUDA-stream experiment.

The final-only variant was withdrawn from production after temporal jitter was
observed in real footage. Keep this script only to reproduce the experiment.

Same model weights, six RAFT updates, SDPA depth AMP FP16, same source frames.
CUDA streams only overlap the two different models, never concurrent calls to
the stateful RAFT instance. No GUI, IPC, DLL or encoding included in timings.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def raft_final_only(self,image1,image2,num_flow_updates=6):
    # Adapted from torchvision RAFT.forward (BSD-3-Clause). Keep recurrence
    # identical; only mask prediction and full-resolution outputs move outside
    # the loop, since neither feeds the next recurrent update.
    import torch
    from torchvision.models.optical_flow._utils import make_coords_grid,upsample_flow
    b,_,h,w=image1.shape
    if image2.shape != image1.shape or h%8 or w%8 or num_flow_updates<1:
        raise ValueError('Invalid RAFT input')
    fmap1,fmap2=torch.chunk(self.feature_encoder(torch.cat([image1,image2],dim=0)),chunks=2,dim=0)
    self.corr_block.build_pyramid(fmap1,fmap2)
    context_out=self.context_encoder(image1)
    hidden_size=self.update_block.hidden_state_size
    hidden,context=torch.split(context_out,[hidden_size,context_out.shape[1]-hidden_size],dim=1)
    hidden=torch.tanh(hidden)
    context=torch.nn.functional.relu(context)
    coords0=make_coords_grid(b,h//8,w//8).to(fmap1.device)
    coords1=make_coords_grid(b,h//8,w//8).to(fmap1.device)
    for _ in range(num_flow_updates):
        coords1=coords1.detach()
        corr=self.corr_block.index_pyramid(centroids_coords=coords1)
        hidden,delta=self.update_block(hidden,context,corr,coords1-coords0)
        coords1=coords1+delta
    mask=None if self.mask_predictor is None else self.mask_predictor(hidden)
    return [upsample_flow(flow=coords1-coords0,up_mask=mask)]


def parallel_process(self,rgba,reset,outputs=None):
    cv2,np,torch=self.cv2,self.np,self.torch
    tick=time.perf_counter()
    rgb=rgba[...,:3]; h,w=rgb.shape[:2]
    thumb=cv2.resize(rgb,(64,36)).astype(np.float32)/255.0
    cut=self.prev_thumb is not None and np.abs(thumb-self.prev_thumb).mean()>0.30
    reset=bool(reset or cut or self.prev is None)
    edge=max(128,min(1280,int(self.settings.get('guidance_edge',720))))
    scale=min(1.0,edge/max(w,h))
    fw,fh=max(128,round(w*scale/8)*8),max(128,round(h*scale/8)*8)
    small=cv2.resize(rgb,(fw,fh))
    mv,dp=outputs
    if reset: mv.fill(0)
    with torch.inference_mode():
        tensors=None
        if not reset:
            tensors=[torch.from_numpy(np.ascontiguousarray(x)).permute(2,0,1).float()[None]/255.0 for x in (self.prev,small)]
            prev,cur=self.transforms(*tensors)
        dw,dh=max(14,round(fw/14)*14),max(14,round(fh/14)*14)
        image=cv2.resize(small,(dw,dh)).astype(np.float32)/255.0
        image=(image-np.array([.485,.456,.406],np.float32))/np.array([.229,.224,.225],np.float32)
        depth_input=torch.from_numpy(image).permute(2,0,1)[None]
        # Weights were loaded on the default stream. Both consumers wait before
        # using them; each tensor is allocated and used on its own model stream.
        self.flow_stream.wait_stream(torch.cuda.current_stream())
        self.depth_stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(self.flow_stream):
            if not reset:
                flow=self.flow(cur.to(self.device),prev.to(self.device),num_flow_updates=6)[-1]
        with torch.cuda.stream(self.depth_stream):
            tensor=depth_input.to(self.device)
            with torch.autocast('cuda',dtype=torch.float16):
                prediction=self.depth(tensor)
            prediction=prediction[0].float()
        # Enqueue both networks BEFORE any blocking CPU read. No global sync
        # between models; the CPU copy on each stream waits for its producer.
        if not reset:
            with torch.cuda.stream(self.flow_stream):
                values=flow[0].permute(1,2,0).cpu().numpy()
            cv2.resize(values,(w,h),dst=mv,interpolation=cv2.INTER_LINEAR)
            mv[...,0]*=w/fw; mv[...,1]*=h/fh
        with torch.cuda.stream(self.depth_stream):
            values=prediction.cpu().numpy()
        if not np.isfinite(values).all(): raise RuntimeError('Nonfinite depth')
        low,high=map(float,np.percentile(values,[1,99]))
        if reset or self.depth_range is None:self.depth_range=(low,high)
        else:self.depth_range=tuple(.9*a+.1*b for a,b in zip(self.depth_range,(low,high)))
        low,high=self.depth_range
        values=np.clip((values-low)/max(high-low,1e-6),0,1)
        cv2.resize(values,(w,h),dst=dp,interpolation=cv2.INTER_LINEAR)
    self.prev,self.prev_thumb=small,thumb
    self.last_metrics={'inference_ms':(time.perf_counter()-tick)*1000}
    return mv,dp,reset


def run(args):
    sys.path.insert(0,str(ROOT/'tmp/dlss5standaloneV2/models'))
    import numpy as np
    import torch
    from guidance_worker import Models
    directory=args.output/f'{args.variant}-r{args.round}'
    directory.mkdir(parents=True,exist_ok=False)
    frames=np.load(ROOT/'output/attention-ab-20260908/inputs.npy',mmap_mode='r')
    settings={'guidance_mode':3,'guidance_device':'cuda','guidance_edge':720,'guidance_depth_encoder':'vitl',
        'guidance_depth_profile':'sdpa_fp16','guidance_flow_direction':'backward',
        'flow_weights':str(ROOT/'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
        'depth_weights':str(ROOT/'mods/models/depth_anything_v2_vitl.pth')}
    model=Models(settings)
    if 'final' in args.variant:model.flow.forward=types.MethodType(raft_final_only,model.flow)
    if 'streams' in args.variant:
        model.flow_stream=torch.cuda.Stream(); model.depth_stream=torch.cuda.Stream()
        model.process=types.MethodType(parallel_process,model)
    _,h,w,_=frames.shape
    mv=np.empty((h,w,2),np.float32);dp=np.empty((h,w),np.float32)
    reference_file=args.output/'reference.npy'
    baseline=args.variant=='baseline' and args.round==0
    reference=np.lib.format.open_memmap(reference_file,mode='w+',dtype=np.float32,shape=(12,h,w,3)) if baseline else np.load(reference_file,mmap_mode='r')
    timings=[]; errors=[];hashes=[]; peaks=[]; sample=0
    for segment in range(3):
        for offset in range(7):
            frame=np.array(frames[segment*11+offset])
            if offset==3:torch.cuda.reset_peak_memory_stats()
            tick=time.perf_counter()
            result=model.process(frame,offset==0,outputs=(mv,dp))
            elapsed=(time.perf_counter()-tick)*1000
            if offset<3:continue
            if not np.isfinite(mv).all() or not np.isfinite(dp).all():raise RuntimeError('Nonfinite output')
            timings.append(elapsed)
            peaks.append(torch.cuda.max_memory_allocated()/1048576)
            if baseline:
                reference[sample,:,:,:2]=mv;reference[sample,:,:,2]=dp
            flow_diff=np.abs(mv-reference[sample,:,:,:2]);depth_diff=np.abs(dp-reference[sample,:,:,2])
            errors.append({'flow_mae':float(flow_diff.mean()),'flow_max':float(flow_diff.max()),
                           'depth_mae':float(depth_diff.mean()),'depth_max':float(depth_diff.max())})
            digest=hashlib.sha256(memoryview(mv));digest.update(memoryview(dp));hashes.append(digest.hexdigest())
            sample+=1
        print(args.variant,args.round,segment+1,flush=True)
    if baseline:reference.flush()
    record={'variant':args.variant,'round':args.round,'frames':sample,'mean_ms':statistics.mean(timings),
        'median_ms':statistics.median(timings),'peak_allocated_mib':max(peaks),
        'flow_max':max(e['flow_max'] for e in errors),'depth_max':max(e['depth_max'] for e in errors),
        'frame_ms':timings,'errors':errors,'hashes':hashes}
    (directory/'result.json').write_text(json.dumps(record,indent=2))
    print(json.dumps({k:v for k,v in record.items() if k not in ('frame_ms','errors','hashes')}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--variant')
    parser.add_argument('--round',type=int,default=0)
    args=parser.parse_args()
    if args.variant:run(args);return
    args.output.mkdir(parents=True,exist_ok=False)
    report=[]
    for run_id in range(2):
        variants=['baseline','final','streams','final_streams']
        if run_id:variants.reverse()
        for variant in variants:
            subprocess.run([sys.executable,__file__,'--output',str(args.output),'--variant',variant,'--round',str(run_id)],
                           timeout=90,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
            record=json.loads((args.output/f'{variant}-r{run_id}'/'result.json').read_text())
            report.append(record)
            (args.output/'report.json').write_text(json.dumps(report,indent=2))


if __name__=='__main__':main()
