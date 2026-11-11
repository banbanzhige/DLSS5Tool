"""Exercise queue recovery with real NR/VSR and child-only simulated pressure.

No real RAM pressure, GUI settings, drivers or media are changed. Queue dispatch,
recovery callbacks, production ownership and existing runtimes are real; GUI
widgets and the small export adapter are headless fixtures. This is not a full
video export or overnight memory plateau acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def save(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n').encode('utf-8'))


class Callbacks:
    def __init__(self):
        self.callbacks = {}; self.next_id = 0

    def after(self, delay, callback):
        self.next_id += 1
        self.callbacks[self.next_id] = (time.monotonic()+delay/1000, callback)
        return self.next_id

    def after_cancel(self, identity):
        self.callbacks.pop(identity, None)

    def tick(self):
        if not self.callbacks:
            time.sleep(.01); return
        identity = min(self.callbacks, key=lambda key:self.callbacks[key][0])
        deadline, callback = self.callbacks[identity]
        if time.monotonic() < deadline:
            time.sleep(min(.02, deadline-time.monotonic())); return
        del self.callbacks[identity]
        callback()


def workload(args):
    if sys.stdin.buffer.read(1) != b'G':
        raise RuntimeError('Missing supervisor job gate')
    work = args.task/args.label
    work.mkdir()
    os.environ.update(TEMP=str(work), TMP=str(work), DLSS5TOOL_DISABLE_UPDATE_CHECK='1',
        DLSS5TOOL_SETTINGS_PATH=str(work/'settings.json'), DLSS5TOOL_QUEUE_PATH=str(work/'queue.json'))
    from dlss5tool import paths
    paths.state_path = lambda name:work/name
    import numpy as np
    from PIL import Image
    from types import SimpleNamespace
    from dlss5tool import gui, dlss_host_process as host, super_resolution as sr
    from dlss5tool import export_queue, memory_budget as mb
    from scripts.profile_native_frame_memory import Live, vsr_worker
    from scripts.ngx_updater_lifecycle_probe import Processes
    processes = Processes()
    records = []
    gui.messagebox.showinfo = lambda *a,**k:None
    for kind in ('nr','vsr'):
        a = gui.App.__new__(gui.App)
        a.root = Callbacks()
        a._queue_running = True; a._queue_pause_requested = False
        a._queue_active_job_id = None; a._exporting = False
        a._live = a._super_resolution_live = None
        a._live_lock = threading.RLock(); a._super_resolution_lock = threading.RLock()
        a._cache_clear = lambda:None
        a._freeze_preview_cache = lambda **kwargs:None
        a._wait_play_dlss = lambda **kwargs:True
        a.queue_tree = SimpleNamespace(selection_set=lambda _:None, see=lambda _:None)
        events = []
        def log(message):
            events.append(dict(wall=time.time(), message=str(message)))
        for name in ('logln','set_status'):
            setattr(a, name, log)
        for name in ('_refresh_queue_tree','_update_queue_action_states',
                     '_update_action_labels','_update_host_control_states'):
            setattr(a, name, lambda:None)
        settings = dict(host_backend='v2', host_auto_fallback=False, guidance_mode=0,
            dlss_runtime=str(ROOT/'runtime/nvngx_dlssnr.dll'), host_submission='compatibility',
            host_in_flight=1, host_persistent_buffers=True, host_zero_fast_path=True,
            style=0, intensity=1.0)
        a._queue_jobs = [export_queue.ExportJob.create(str(work/f'{kind}-{name}.png'),
            str(work/f'{kind}-{name}-out.png'), settings, {}) for name in ('done','current','later')]
        a._queue_jobs[0].state = 'completed'
        done = Path(a._queue_jobs[0].output_path)
        done.write_bytes(b'previously completed; preserve exactly')
        done_hash = hashlib.sha256(done.read_bytes()).hexdigest()
        a._save_queue_state = lambda:export_queue.save(a._queue_jobs, str(work/f'{kind}-queue.json'))
        a._normalize_queue_output_path = lambda job:job.output_path
        width,height = (640,360) if kind=='nr' else (320,180)
        pixels = np.empty((height,width,4), np.uint8)
        pixels[...,0] = np.arange(width)[None,:] % 256
        pixels[...,1] = np.arange(height)[:,None] % 256
        pixels[...,2] = 96; pixels[...,3] = 255
        record = dict(kind=kind, attempts=[], simulated_pressure_only=True)
        def export(source, *, out_path, **kwargs):
            if source == a._queue_jobs[2].source_path:
                with open(out_path,'xb') as destination:destination.write(b'next item completed')
                return dict(success=True,cancelled=False,error='',frames=1,output_path=out_path)
            first = not record['attempts']
            os.environ['DLSS5TOOL_P0_INJECT_MEMORY_PRESSURE'] = '1' if first else '0'
            previous = sr._worker_main
            try:
                if kind=='nr':
                    a._live = host.ProcessLive(width,height,settings,_live_factory=Live if first else None)
                    owner = a._live; process = owner._session._process
                else:
                    if first:sr._worker_main = vsr_worker
                    a._super_resolution_live = sr.ProcessSuperResolution(width,height,2)
                    owner = a._super_resolution_live; process = owner._process
            finally:
                sr._worker_main = previous
                os.environ.pop('DLSS5TOOL_P0_INJECT_MEMORY_PRESSURE',None)
            row = next(r for r in processes.snapshot() if r['pid']==process.pid)
            identity = processes.identity(row)
            output = owner.process(pixels,reset=True) if kind=='nr' else owner.process(pixels)
            attempt = dict(pid=process.pid,created=identity['created'],
                output_sha256=hashlib.sha256(memoryview(output)).hexdigest())
            record['attempts'].append(attempt)
            if first:
                deadline = time.monotonic()+15
                while process.is_alive():
                    if time.monotonic()>deadline or (args.task/'cancel.flag').exists():
                        raise RuntimeError('Worker did not retire after simulated idle pressure')
                    time.sleep(.05)
                try:
                    owner.process(pixels,reset=True) if kind=='nr' else owner.process(pixels)
                except Exception as error:
                    attempt['pressure'] = mb.pressure_details(error)
                    assert attempt['pressure'] is not None
                    raise
                raise AssertionError('Retired worker did not report its structured error')
            assert attempt['output_sha256']==record['attempts'][0]['output_sha256']
            assert (attempt['pid'],attempt['created']) != (record['attempts'][0]['pid'],record['attempts'][0]['created'])
            Image.fromarray(output).save(out_path)
            return dict(success=True,cancelled=False,error='',frames=1,output_path=out_path)
        a._export_image_source = export
        try:
            a._run_next_queue_job()
            deadline = time.monotonic()+120
            while a._queue_running:
                if time.monotonic()>deadline or (args.task/'cancel.flag').exists():
                    raise RuntimeError('Queue recovery exceeded bounded observation')
                a.root.tick()
            assert len(record['attempts'])==2
            assert all(job.state=='completed' for job in a._queue_jobs)
            assert hashlib.sha256(done.read_bytes()).hexdigest()==done_hash
            record.update(passed=True, queue_states=[j.state for j in a._queue_jobs],
                completed_output_unchanged=True, events=events)
        finally:
            a._cleanup_video_export([('NR',a._close_live),('VSR',a._close_super_resolution)])
            host.dlss_engine.close_adapter_probe()
        records.append(record)
        save(work/'result.json', records)
        print(json.dumps(dict(kind=kind,passed=True,attempts=len(record['attempts']))),flush=True)
    pool = mb.current_pool(create=False)
    if pool is not None:pool.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True)
    parser.add_argument('--label',default='native')
    parser.add_argument('--worker',action='store_true')
    args=parser.parse_args()
    from scripts.verify_p0_stability import checked_path, Memory, run_manifest
    args.task=checked_path(args.task)
    if (not args.task.is_relative_to(ROOT/'tmp') or not (args.task/'TASK.md').is_file()
            or not re.fullmatch(r'[a-z0-9-]+',args.label)):
        parser.error('Registered repository tmp task required')
    if args.worker:
        workload(args);return
    from scripts.verify_native_worker_recycling import job_pids, Performance
    from scripts.ngx_updater_lifecycle_probe import Processes
    from dlss5tool.process_job import KillOnCloseJob
    def manifest():
        result=run_manifest(args.task)
        for name in ('dlss5tool/queue_memory_recovery.py','dlss5tool/deferred_error.py',
                     'dlss5tool/gpu_resources.py',
                     'dlss5tool/gpu_export_process.py','dlss5tool/resident_sr.py',
                     'dlss5tool/resident_sr_worker.py','locales/zh_CN.json','locales/en_US.json',
                     'scripts/verify_queue_memory_recovery.py'):
            value=(ROOT/name).read_bytes()
            result['files'].append(dict(file=name,bytes=len(value),sha256=hashlib.sha256(value).hexdigest()))
        return result
    before=manifest()
    save(args.task/(args.label+'-manifest-before.json'),before)
    start=time.monotonic();error=None;remaining=[]
    processes=Processes();memory=Memory(processes);performance=Performance();samples=[]
    environment=dict(os.environ,TEMP=str(args.task),TMP=str(args.task),PYTHONDONTWRITEBYTECODE='1')
    with (args.task/(args.label+'-console.log')).open('xb') as console:
        child=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),'--task',str(args.task),
            '--label',args.label,'--worker'],
            cwd=ROOT,env=environment,stdin=subprocess.PIPE,stdout=console,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        job=None;exit_seen=None
        try:
            job=KillOnCloseJob(child)
            child.stdin.write(b'G');child.stdin.flush();child.stdin.close()
            while True:
                members=set(job_pids(job))
                owned=[memory.read(processes.identity(r)) for r in processes.snapshot() if r['pid'] in members]
                private=sum(r.get('private_bytes',0) for r in owned)
                system=performance.read()
                samples.append(dict(wall=time.time(),processes=owned,private_bytes=private,system=system))
                if (private>8*1024**3 or min(system['free_commit_bytes'],system['free_physical_bytes'])<8*1024**3
                        or time.monotonic()-start>180):
                    error='Diagnostic memory/headroom or time limit';break
                if child.poll() is not None:
                    exit_seen=exit_seen or time.monotonic()
                    if not members or time.monotonic()-exit_seen>8:break
                time.sleep(.25)
        finally:
            if job is not None:
                remaining=job_pids(job);job.close()
            elif child.poll() is None:child.terminate()
            child.wait(timeout=20)
    after=manifest()
    save(args.task/(args.label+'-manifest-after.json'),after)
    passed=child.returncode==0 and not remaining and error is None and before==after
    save(args.task/(args.label+'-samples.json'),samples)
    result=dict(passed=passed,exit_code=child.returncode,remaining_before_supervisor_cleanup=remaining,
        error=error,source_and_runtime_unchanged=before==after,elapsed_seconds=time.monotonic()-start)
    save(args.task/(args.label+'-verification.json'),result)
    print(json.dumps(result),flush=True)
    raise SystemExit(0 if passed else 1)


if __name__=='__main__':main()
