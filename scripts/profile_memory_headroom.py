"""Bounded owned-process RAM peak calibration; no downloads or real pressure."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
CASES={
    'nr-sdr-1080':(1920,1080,1,False,True,0),
    'nr-hdr-2160':(3840,2160,1,True,True,0),
    'sr-sdr-2x':(1920,1080,2,False,False,0),
    'sr-sdr-4x':(960,540,4,False,False,0),
    'sr-hdr-2x':(960,540,2,True,False,0),
    'flow-nr':(640,360,1,False,True,1),
    'fg-sdr':(640,360,1,False,False,0),
}


def save(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')


def validate(task):
    task=task.absolute()
    if task.resolve()!=task or not task.is_relative_to(ROOT/'tmp'):
        raise ValueError('Task must resolve inside repository tmp')
    for item in (task,*task.parents):
        if item.lstat().st_file_attributes&0x400:raise ValueError('Reparse point')
    if not (task/'TASK.md').is_file() or shutil.disk_usage(task).free<16*1024**3:
        raise ValueError('Registered task and 16 GiB free required')
    return task


def workload(args,task):
    prefix=task/f'{args.label}-{args.case}'
    def phase(value):save(prefix.with_suffix('.phase.json'),dict(phase=value,time=time.time()))
    deadline=time.monotonic()+10
    while not prefix.with_suffix('.start').exists():
        if time.monotonic()>deadline:raise TimeoutError('Supervisor did not assign job')
        time.sleep(.02)
    import numpy as np
    from dlss5tool import app_settings
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.super_resolution import ProcessSuperResolution,estimate_resources
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.memory_budget import prefer_pool,memory_events
    width,height,scale,hdr,enhance,flow=CASES[args.case]
    settings=app_settings.validate(dict(host_backend='v2',host_auto_fallback=False,
        host_in_flight=1,dlss_runtime='__bundled__',guidance_mode=flow,
        guidance_edge=384,guidance_flow_edge=384,guidance_flow_updates=6,
        guidance_skip_still_flow=False,super_resolution_scale=scale,
        output_mix=1,frame_generation_multiplier=2 if args.case=='fg-sdr' else 1))
    if hdr:settings.update(frame_format='rgba16f',color_profile='hdr10_pq',color_primaries='bt2020')
    pool=SharedCacheBudget(limit_bytes=256*1024**2);prefer_pool(pool)
    settings['guidance_cache_pool']=pool.name
    estimate=estimate_resources(width,height,scale,hdr,in_flight=1,
        use_super_resolution=scale>1,use_enhancement=enhance,
        decode_buffer=1 if args.case=='fg-sdr' else 0,
        frame_generation_multiplier=2 if args.case=='fg-sdr' else 1)
    phase('ready');time.sleep(.15)
    owner=None
    result=dict(case=args.case,width=width,height=height,scale=scale,hdr=hdr,
                estimate=estimate,settings=settings,frames=0,hashes=[])
    try:
        phase('initialization')
        if args.case=='fg-sdr':
            from dlss5tool import frame_generation,video_export
            source=task/'calibration-640x360.mp4'
            if not source.exists():
                subprocess.run([video_export.find_ffmpeg(),'-v','error','-n','-f','lavfi',
                    '-i','testsrc2=size=640x360:rate=24','-frames:v','8','-c:v','libx264',
                    '-preset','ultrafast','-pix_fmt','yuv420p',str(source)],check=True,timeout=30,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            def sink(index,pixels,reference,**kw):
                phase('processing')
                result['hashes'].append(hashlib.sha256(memoryview(pixels)).hexdigest())
                result['frames']+=1
            frame_generation.export_video(source,None,multiplier=2,scale=1,enhance=False,
                settings=settings,frame_sink=sink,log_dir=task/f'{args.label}-fg-log')
        else:
            pixels=np.zeros((height,width,4),np.float16 if hdr else np.uint8)
            pixels[...,3]=1 if hdr else 255
            owner=ProcessSuperResolution(width,height,scale,is_hdr=hdr) if scale>1 else ProcessLive(width,height,settings)
            phase('processing')
            for index in range(8):
                pixels[...,0]=(index+1)/16 if hdr else index*12
                output=owner.process(pixels) if scale>1 else owner.process(pixels,reset=index==0)
                assert output.shape==(height*scale,width*scale,4) and output.dtype==pixels.dtype
                result['hashes'].append(hashlib.sha256(memoryview(output)).hexdigest())
                result['frames']+=1
                time.sleep(.025)
                del output
        result['status']='passed'
    finally:
        phase('cleanup')
        if owner is not None:owner.close()
        result['ledger_after_close']=pool.snapshot()
        result['memory_events']=memory_events()
        pool.close()
        save(prefix.with_suffix('.result.json'),result)
        phase('complete')


def supervise(args,task):
    from scripts.ngx_updater_lifecycle_probe import Processes
    from scripts.verify_p0_stability import Memory
    from dlss5tool.process_job import KillOnCloseJob
    from dlss5tool.memory_budget import fresh_system_memory
    processes=Processes();reader=Memory(processes)
    results=[]
    for case in args.cases:
        prefix=task/f'{args.label}-{case}'
        if prefix.with_suffix('.result.json').exists() or prefix.with_suffix('.samples.json').exists():
            raise ValueError('Refusing overwrite: '+str(prefix))
        environment=dict(os.environ,TEMP=str(task),TMP=str(task),PYTHONDONTWRITEBYTECODE='1')
        with prefix.with_suffix('.stdout.txt').open('wb') as output:
            child=subprocess.Popen([sys.executable,'-B',__file__,'--task',str(task),'--label',args.label,
                '--child','--case',case],cwd=ROOT,env=environment,stdout=output,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            job=KillOnCloseJob(child)
            known={child.pid:processes.identity(dict(pid=child.pid,parent=os.getpid(),name='calibration'))}
            samples=[];started=time.monotonic();failure=None
            try:
                prefix.with_suffix('.start').write_text('assigned',encoding='ascii')
                while child.poll() is None:
                    rows=processes.snapshot()
                    for _ in range(4):
                        for row in rows:
                            if row['parent'] in known and row['pid'] not in known:
                                known[row['pid']]=processes.identity(row)
                    current=[]
                    for row in rows:
                        identity=known.get(row['pid'])
                        if identity and processes.identity(row).get('created')==identity.get('created'):
                            current.append(reader.read(identity))
                    private=sum(r.get('private_bytes',0) for r in current)
                    working=sum(r.get('working_set',0) for r in current)
                    try:phase=json.loads(prefix.with_suffix('.phase.json').read_text(encoding='utf-8'))['phase']
                    except (OSError,ValueError):phase='startup'
                    snapshot=fresh_system_memory()
                    samples.append(dict(seconds=round(time.monotonic()-started,3),phase=phase,
                        private_bytes=private,working_set_bytes=working,system=snapshot,processes=current))
                    if time.monotonic()-started>90 or private>8*1024**3 or (snapshot and
                        min(snapshot['free_physical_bytes'],snapshot['free_commit_bytes'])<4*1024**3):
                        failure='bounded calibration safety stop';break
                    time.sleep(.05)
            finally:
                job.close()  # only this owned child's job and descendants
                child.wait(timeout=10)
            save(prefix.with_suffix('.samples.json'),samples)
            try:result=json.loads(prefix.with_suffix('.result.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):result=dict(case=case,status='failed')
            ready=[s['private_bytes'] for s in samples if s['phase']=='ready']
            peak=max((s['private_bytes'] for s in samples),default=0)
            baseline=min(ready) if ready else min((s['private_bytes'] for s in samples if s['private_bytes']),default=0)
            known_bytes=result.get('estimate',{}).get('known_ram_bytes',0)
            result.update(exit_code=child.returncode,safety_failure=failure,peak_tree_private_bytes=peak,
                baseline_tree_private_bytes=baseline,peak_tree_working_set_bytes=max((s['working_set_bytes'] for s in samples),default=0),
                unmodelled_private_delta_bytes=max(0,peak-baseline-known_bytes),sample_count=len(samples),
                phase_peaks={phase:max(s['private_bytes'] for s in samples if s['phase']==phase)
                             for phase in sorted({s['phase'] for s in samples})})
            save(prefix.with_suffix('.summary.json'),result)
            results.append(result)
            print(json.dumps({k:result.get(k) for k in ('case','status','exit_code','peak_tree_private_bytes',
                'unmodelled_private_delta_bytes','safety_failure')}),flush=True)
            if child.returncode or failure:break
    save(task/f'{args.label}-calibration.json',dict(schema=1,results=results,
        scope='one local Windows machine; 50 ms sampling; 8 frames; no long-term plateau claim'))
    return 0 if len(results)==len(args.cases) and all(r['status']=='passed' and r['exit_code']==0 for r in results) else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',required=True,type=Path)
    parser.add_argument('--label',required=True)
    parser.add_argument('--child',action='store_true')
    parser.add_argument('--case',choices=CASES)
    parser.add_argument('--cases',nargs='+',choices=CASES,default=list(CASES))
    parser.add_argument('--evaluate',help='Evaluate a completed calibration label without another GPU run')
    args=parser.parse_args()
    if not args.label.replace('-','').isalnum():raise ValueError('Plain label required')
    task=validate(args.task)
    if args.evaluate:
        if not args.evaluate.replace('-','').isalnum():raise ValueError('Plain calibration label required')
        from dlss5tool import memory_budget as mb
        calibration=json.loads((task/f'{args.evaluate}-calibration.json').read_text(encoding='utf-8'))
        if not all(r['status']=='passed' and not r.get('exit_code') for r in calibration['results']):
            raise ValueError('Successful calibration required')
        gib=1024**3;required=256*1024**2
        unmodelled=max(r['unmodelled_private_delta_bytes'] for r in calibration['results'])
        candidates=[dict(cap_gib=cap,local_peak_cushion_bytes=cap*gib-unmodelled,
            covers_sample_plus_one_gib=cap*gib>=unmodelled+gib) for cap in (2,4,8,16)]
        recommended=min(r['cap_gib'] for r in candidates if r['covers_sample_plus_one_gib'])
        rows=[]
        for total in (8,16,32,64,128):
            old=max(2,total/8)*gib
            new=mb.reserve_bytes(dict(total_physical_bytes=total*gib))
            rows.append(dict(total_gib=total,previous_reserve_gib=old/gib,new_reserve_gib=new/gib,
                required_256_mib_minimum_free_gib=(new+required)/gib))
        matrix=[]
        for total in (8,16,32,64,128):
            for free in (0,.25,.5,1,2,2.25,3,4,4.25,6,8,16):
                if free>total:continue
                for commit in (.125,1,2,2.25,4,6,16):
                    memory=dict(total_physical_bytes=total*gib,free_physical_bytes=int(free*gib),
                                free_commit_bytes=int(commit*gib))
                    new=mb.memory_details(required,memory=memory)
                    admitted=new['available_bytes']>=required
                    assert admitted==(min(memory['free_physical_bytes'],memory['free_commit_bytes'])>=
                                      mb.reserve_bytes(memory)+required)
                    matrix.append(dict(total_gib=total,free_gib=free,commit_gib=commit,
                        previous_admitted=min(free,commit)*gib>=max(2,total/8)*gib+required,
                        admitted=admitted,reason=new['reason']))
        destination=task/f'{args.label}-policy-evaluation.json'
        if destination.exists():raise ValueError('Refusing overwrite')
        report=dict(calibration_label=args.evaluate,maximum_unmodelled_private_delta_bytes=unmodelled,
            candidates=candidates,recommended_cap_gib=recommended,policy=mb.memory_policy(),
            reserves=rows,matrix=matrix,
            scope='Cap selection from one local sampled workload set; minimum reserve retained. '
                  'Synthetic RAM capacities are not actual multi-machine OOM tests.')
        save(destination,report)
        print(json.dumps(dict(cap_gib=recommended,matrix_cases=len(matrix),reserves=rows)),flush=True)
        return 0
    if args.child:workload(args,task);return 0
    return supervise(args,task)


if __name__=='__main__':raise SystemExit(main())
