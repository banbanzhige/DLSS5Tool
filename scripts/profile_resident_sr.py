"""Bounded end-to-end resident SR export using production owners and audio mux."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import threading
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--scale', type=int, choices=(2, 4), default=2)
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--cancel-after', type=int)
    parser.add_argument('--sample-gpu', action='store_true')
    parser.add_argument('--quality', choices=('high','balanced'), default='high')
    args = parser.parse_args()
    from scripts.build_vsr_cuda import safe, digest
    task = safe(args.task, ROOT/'tmp')
    source = safe(args.source, task)
    report_path, output = task/f'{args.label}.json', task/f'{args.label}.mp4'
    if not (task/'TASK.md').is_file() or not args.label.replace('-', '').isalnum() or report_path.exists() or output.exists():
        raise ValueError('Fresh registered task and safe label required')
    from dlss5tool import app_settings, dlss_engine
    from dlss5tool.frame_generation import inspect_source
    from dlss5tool.gpu_export_runtime import load_components
    from dlss5tool.resident_sr import make_plan, export_resident
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.memory_budget import memory_scope
    from scripts.probe_resident_decode import process_record
    cancel = threading.Event()
    watchdog = threading.Timer(150, cancel.set)
    watchdog.daemon = True; watchdog.start()
    settings = app_settings.validate(dict(super_resolution_scale=args.scale,
        frame_generation_multiplier=1, nr_quality={'total':0}, output_view=0,
        hdr_mode=False, output_mix=1, nvenc_preset='p5', rate_control='quality', quality_profile=args.quality))
    report = dict(source_sha256=digest(source), settings=settings)
    adapter = dlss_engine.available_render_adapters()[0]
    pool = SharedCacheBudget(limit_bytes=1024**3)
    owner = []
    gpu_monitor = gpu_log = None
    if args.sample_gpu:
        gpu_log = (task/f'{args.label}-gpu.csv').open('xb')
        gpu_monitor = subprocess.Popen(['nvidia-smi',
            '--query-gpu=timestamp,utilization.gpu,utilization.memory,utilization.encoder,utilization.decoder,memory.used,power.draw',
            '--format=csv,noheader,nounits', '--loop-ms=200'], stdout=gpu_log, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
    started = time.perf_counter()
    try:
      with memory_scope(pool):
        inspection = inspect_source(source, cancel, allow_vfr=True)
        if inspection[-1] > 1800:
            raise ValueError('Probe limited to 1800 frames')
        plan = make_plan(inspection, settings, adapter)
        if plan is None:
            raise ValueError('Not a supported resident SR contract: '+repr(inspection))
        components = load_components()
        if not components:
            raise RuntimeError('Verified encoder components missing')
        components['nv12_ptx'] = str(safe(task/'nv12-rgba.ptx', task))
        components['component_hashes'] = {key:digest(Path(components[key])) for key in ('nv12_ptx', 'native_library', 'sdr_ptx')}
        components['cuda_vsr_manifest'] = digest(ROOT/'runtime/vsr-cuda/manifest.json')
        def on_frames(done, total):
            if args.cancel_after is not None and done >= args.cancel_after:
                report['cancel_requested_seconds'] = time.perf_counter()-started
                cancel.set()
        from dlss5tool import resident_sr
        original_spawn = resident_sr._spawn
        def tracked_spawn(*values):
            process = original_spawn(*values)
            owner.append(process.pid)
            return process
        with mock.patch.object(resident_sr, '_spawn', tracked_spawn):
            result = export_resident(source, output, plan, components, cancel=cancel,
                pool_name=pool.name, audit=args.audit, log_root=task/'logs', frame_progress=on_frames)
        report.update(status='complete', result=result, output_sha256=digest(output),
                      worker_after_retirement=process_record(result['worker_pid']))
    except BaseException as error:
        import traceback
        traceback.print_exc()
        from dlss5tool.frame_generation import Cancelled
        report.update(status='cancelled' if isinstance(error, Cancelled) else 'failed',
                      error=type(error).__name__+': '+str(error))
    finally:
        watchdog.cancel()
        if gpu_monitor:
            gpu_monitor.terminate(); gpu_monitor.wait(timeout=5); gpu_log.close()
        report['elapsed_seconds'] = time.perf_counter()-started
        if owner:
            report['worker_after_retirement'] = process_record(owner[-1])
        report['output_exists'] = output.exists()
        report['transaction_directories_remaining'] = [p.name for p in task.glob('.sr-*')]
        report['pool_after_retirement'] = pool.snapshot()
        pool.close()
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('status','elapsed_seconds','error','worker_after_retirement') if k in report}))
    if report.get('result'):
        print(json.dumps({k:v for k,v in report['result'].items() if k not in ('frame_hashes','adapter','performance')}))
    expected = 'cancelled' if args.cancel_after else 'complete'
    return 0 if (report['status'] == expected and not report['worker_after_retirement']['present']
                 and not report['transaction_directories_remaining']
                 and (not args.cancel_after or not report['output_exists'])) else 1


if __name__ == '__main__':
    raise SystemExit(main())
