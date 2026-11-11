"""Bounded direct CUDA VSR teardown with per-phase evidence in an owned Job.

No driver replacement, global settings or cross-process GPU pointers. A timeout
remains a failed natural-close result even when the supervisor retires its Job.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def save(path,data):path.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')


def worker(args,work):
    if sys.stdin.buffer.read(1)!=b'G':raise RuntimeError('Missing owned Job start gate')
    os.environ.update(TEMP=str(work),TMP=str(work),DLSS5TOOL_DISABLE_UPDATE_CHECK='1')
    from dlss5tool import paths
    paths.state_path=lambda name:work/name
    from scripts.windows_process_context import current_context
    from scripts.diagnose_composer_lifetime import memory_snapshot
    from scripts.profile_thread_activity import load_counter,read_counter
    counter=load_counter()
    def event(kind,**value):
        row=dict(event=kind,wall=time.time(),pid=os.getpid(),**value)
        with (work/'events.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(row)+'\n')
    event('started',context=current_context())
    try:
        import numpy as np
        from dlss5tool.cuda_session import CudaSession
        from dlss5tool.cuda_frame import CudaImage,FrameTiming
        from dlss5tool.cuda_vsr import CudaVSR
        event('cuda-create-begin')
        cuda=CudaSession()
        event('cuda-created',luid=cuda.luid)
        source=CudaImage(cuda,320,180)
        vsr=CudaVSR.from_runtime(cuda,320,180,2,log_path=work/'native.log')
        event('vsr-created')
        actual_sync,actual_destroy=vsr._stream_sync,vsr._destroy
        def sync(stream):
            event('stream-sync-begin')
            code=actual_sync(stream)
            event('stream-sync-end',code=code)
            return code
        def destroy(handle):
            event('native-destroy-begin')
            code=actual_destroy(handle)
            event('native-destroy-end',code=code)
            return code
        vsr._stream_sync,vsr._destroy=sync,destroy
        pixels=np.zeros((180,320,4),np.uint8);pixels[...,3]=255
        hashes=[]
        for frame in range(3):
            pixels[...,0]=frame*16
            with source.upload(pixels,FrameTiming(frame,reset=frame==0)) as ready:
                with vsr.process_device(ready) as output:
                    actual=vsr._output.download(output)
                    hashes.append(hashlib.sha256(actual.tobytes()).hexdigest())
            event('frame',frame=frame+1)
        event('before-close',memory=memory_snapshot(),
              thread_activity=read_counter(counter) if counter else None)
        started=time.monotonic()
        event('vsr-close-begin')
        vsr.close()
        event('vsr-close-end',seconds=time.monotonic()-started)
        source.close()
        event('source-closed')
        event('cuda-close-begin')
        cuda.close()
        event('cuda-close-end')
        save(work/'worker-result.json',dict(status='passed',frame_hashes=hashes,
            close_seconds=time.monotonic()-started,all_native_close_calls_returned=True))
    except BaseException:
        event('failed',traceback=traceback.format_exc())
        raise


def main(args):
    from scripts.verify_p0_stability import checked_path,Memory,run_manifest
    from scripts.ngx_updater_lifecycle_probe import Processes
    from scripts.verify_native_worker_recycling import job_pids
    from dlss5tool.process_job import KillOnCloseJob
    from dlss5tool.memory_budget import system_memory
    from dlss5tool.cuda_vsr_runtime import load_components
    task=checked_path(args.task)
    if not task.is_relative_to(ROOT/'tmp') or not (task/'TASK.md').is_file():
        raise ValueError('Use a registered task')
    work=checked_path(task/('cuda-close-'+args.label))
    if args.worker:return worker(args,work)
    work.mkdir(exist_ok=False)
    pair=load_components()
    assert pair is not None
    def manifest():
        record=run_manifest(task)
        for name in ('scripts/verify_cuda_vsr_close.py','dlss5tool/cuda_session.py',
                     'dlss5tool/cuda_vsr.py','dlss5tool/cuda_frame.py','native/vsr_cuda/vsr_cuda.cpp'):
            path=ROOT/name
            record['files'].append(dict(file=name,sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        for key in ('native_library','cudart'):
            path=Path(pair[key])
            record['files'].append(dict(file=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        return record
    before=manifest();save(work/'manifest-before.json',before)
    child=job=None;error=None;compatibility=None;samples=[];remaining=[]
    began=time.monotonic()
    try:
        environment=dict(os.environ,TEMP=str(work),TMP=str(work),PYTHONDONTWRITEBYTECODE='1')
        with (work/'console.log').open('xb') as console:
            command=[sys.executable,'-B',str(Path(__file__).resolve()),'--task',str(task),
                     '--label',args.label,'--timeout',str(args.timeout),'--worker']
            child=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=console,
                stderr=subprocess.STDOUT,cwd=ROOT,env=environment,creationflags=subprocess.CREATE_NO_WINDOW)
            job=KillOnCloseJob(child,nvidia_compat=not args.control_off)
            compatibility=job.token_compatibility
            processes=Processes();memory=Memory(processes)
            child.stdin.write(b'G');child.stdin.close()
            while child.poll() is None:
                members=set(job_pids(job))
                owned=[memory.read(processes.identity(r)) for r in processes.snapshot() if r['pid'] in members]
                system=system_memory()
                samples.append(dict(wall=time.time(),processes=owned,system=system))
                if time.monotonic()-began>args.timeout:
                    error='natural_close_timeout';break
                if (sum(r.get('private_bytes',0) for r in owned)>8*1024**3 or not system
                        or min(system['free_commit_bytes'],system['free_physical_bytes'])<8*1024**3):
                    error='diagnostic_memory_or_headroom_limit';break
                time.sleep(.25)
    except BaseException:
        error=traceback.format_exc()
    finally:
        if child and child.stdin and not child.stdin.closed:child.stdin.close()
        if job:
            remaining=job_pids(job);job.close()
        if child:
            if child.poll() is None:child.wait(timeout=15)
            child.wait(timeout=15)
    after=manifest();save(work/'manifest-after.json',after)
    event_path=work/'events.jsonl'
    events=([json.loads(s) for s in event_path.read_text(encoding='utf-8').splitlines()]
            if event_path.exists() else [])
    result=dict(processing_and_natural_close_passed=not error and child.returncode==0 and before==after,
        error=error,returncode=child.returncode,elapsed_seconds=time.monotonic()-began,
        compatibility=compatibility,control_off=args.control_off,source_runtime_unchanged=before==after,
        remaining_before_supervisor_retirement=remaining,supervisor_recovery=bool(remaining),
        last_phase=events[-1]['event'] if events else None,frames=sum(r['event']=='frame' for r in events))
    save(work/'samples.json',samples);save(work/'verification.json',result)
    print(json.dumps(result),flush=True)
    return 0 if result['processing_and_natural_close_passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True)
    parser.add_argument('--label',required=True)
    parser.add_argument('--timeout',type=int,default=60,choices=range(30,121))
    parser.add_argument('--control-off',action='store_true')
    parser.add_argument('--worker',action='store_true')
    args=parser.parse_args()
    if not args.label.replace('-','').isalnum():parser.error('Safe label required')
    raise SystemExit(main(args))
