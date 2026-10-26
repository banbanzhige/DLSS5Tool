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
    parser.add_argument('--color-profile', choices=('srgb', 'hdr10_pq', 'hdr10_hlg'), default='srgb')
    parser.add_argument('--enhance', action='store_true')
    parser.add_argument('--multiplier', type=int, choices=(1, 2, 3, 4), default=1)
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
        frame_generation_multiplier=args.multiplier, nr_quality={'total':1 if args.enhance else 0}, output_view=0,
        hdr_mode=args.color_profile != 'srgb', output_mix=1, nvenc_preset='p5', rate_control='quality', quality_profile=args.quality))
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
        pure_settings = dict(settings, frame_generation_multiplier=1, nr_quality={'total':0})
        if args.color_profile != 'srgb':
            from dlss5tool.resident_hdr import make_plan as make_hdr_plan
            plan = make_hdr_plan(inspection, pure_settings, adapter)
            if plan and plan['color_profile'] != args.color_profile:
                raise ValueError('Requested HDR color profile differs from source')
        else:
            plan = make_plan(inspection, pure_settings, adapter)
        if plan is None:
            raise ValueError('Not a supported resident SR contract: '+repr(inspection))
        plan.update(multiplier=args.multiplier, nr=args.enhance, consumer_settings=settings)
        components = load_components()
        if not components:
            raise RuntimeError('Verified encoder components missing')
        if args.color_profile != 'srgb':
            from dlss5tool.cuda_vsr_runtime import load_components as load_vsr
            pair = load_vsr()
            components.update(p010_ptx=str(task/'p010-rgba-v2.ptx'), hdr_pack_ptx=str(task/'vsr-hdr-v2.ptx'),
                hdr_vsr_library=str(task/'build/vsr_cuda.dll'), cudart_directory=pair['cudart_directory'],
                runtime_directory=pair['runtime_directory'])
            keys = ('p010_ptx', 'hdr_pack_ptx', 'hdr_vsr_library', 'native_library', 'sdr_ptx', 'hdr_ptx')
        else:
            local_ptx = task/'nv12-rgba.ptx'
            components['nv12_ptx'] = str(safe(local_ptx if local_ptx.exists() else ROOT/'runtime/resident-sr/nv12-rgba.ptx'))
            keys = ('nv12_ptx', 'native_library', 'sdr_ptx')
        if args.enhance:
            components['device_host'] = str(task/'consumer-build/enhancement.dll')
            keys += ('device_host',)
        if args.multiplier > 1:
            components['device_worker'] = str(task/'consumer-build/frame-worker.exe')
            keys += ('device_worker',)
        components['analysis_ptx'] = str(task/'analysis-v2.ptx')
        components['analysis_tables'] = str(task/'analysis-tables.npz')
        keys += ('analysis_ptx', 'analysis_tables')
        components['component_hashes'] = {key:digest(Path(components[key])) for key in keys}
        components['cuda_vsr_manifest'] = digest(ROOT/'runtime/vsr-cuda/manifest.json')
        from dlss5tool.analysis_quantization import cpu_fingerprint
        components['analysis_cpu'] = cpu_fingerprint()
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
