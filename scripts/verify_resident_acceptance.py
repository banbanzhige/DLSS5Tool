"""Bounded installed-runtime SDR/HDR export, audio/timeline and retirement test.

Uses production owners with synthetic task-local input. No dependency copying,
driver changes or user settings. All source/output bytes remain in the task.
"""
import argparse
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def media(path):
    import av
    av.logging.restore_default_callback()
    video_pts=[];video_dts=[];audio_pts=[];audio=hashlib.sha256();packets=0
    with av.open(str(path)) as container:
        stream=container.streams.video[0]
        size=(stream.width,stream.height);rate=Fraction(stream.average_rate)
        for packet in container.demux():
            if not packet.size:continue
            if packet.stream.type=='video' and packet.stream.index==stream.index:
                video_pts.append(Fraction(packet.pts)*packet.time_base)
                video_dts.append(Fraction(packet.dts)*packet.time_base)
            elif packet.stream.type=='audio':
                audio.update(bytes(packet));packets+=1
                audio_pts.append(str(Fraction(packet.pts)*packet.time_base))
    assert video_pts and sorted(video_pts)==[Fraction(i)/rate for i in range(len(video_pts))]
    assert all(b>a for a,b in zip(video_dts,video_dts[1:]))
    return dict(size=size,rate=str(rate),frames=len(video_pts),pts_exact=True,dts_monotonic=True,
        audio_packets=packets,audio_sha256=audio.hexdigest(),
        audio_pts_sha256=hashlib.sha256(json.dumps(audio_pts).encode()).hexdigest())


def main(args):
    from scripts.verify_p0_stability import checked_path,run_manifest
    from scripts.windows_process_context import current_context
    from dlss5tool import app_settings,dlss_engine,resident_sr,paths
    from dlss5tool.frame_generation import inspect_source,Cancelled
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.memory_budget import memory_scope
    from dlss5tool.process_job import process_record
    task=checked_path(args.task);source=checked_path(args.source)
    if (not task.is_relative_to(ROOT/'tmp') or not source.is_relative_to(task)
            or not (task/'TASK.md').is_file()):raise ValueError('Registered task with synthetic source required')
    work=checked_path(task/('resident-'+args.label));work.mkdir(exist_ok=False)
    os.environ.update(TEMP=str(work),TMP=str(work),DLSS5TOOL_SETTINGS_PATH=str(work/'settings.json'),
        DLSS5TOOL_DISABLE_UPDATE_CHECK='1')
    paths.state_path=lambda name:work/name
    source_info=media(source)
    assert source_info['frames']<=1800 and source_info['size']==(1920,1080)
    cancel=threading.Event();watchdog=threading.Timer(180,cancel.set);watchdog.daemon=True
    progress=[];owned=[];started=time.monotonic();cancel_at=None
    settings=app_settings.validate(dict(app_settings.DEFAULTS,super_resolution_scale=2,
        frame_generation_multiplier=1,nr_quality={'total':1 if args.enhance else 0},
        output_view=0,output_mix=1,hdr_mode=args.hdr,nvenc_preset='p5',rate_control='quality',
        quality_profile='high',host_backend='v2',host_auto_fallback=False,guidance_mode=0))
    before=run_manifest(task)
    for name in ('dlss5tool/resident_sr.py','dlss5tool/resident_sr_worker.py',
                 'dlss5tool/cuda_vsr.py','dlss5tool/cuda_session.py','scripts/verify_resident_acceptance.py'):
        path=ROOT/name
        before['files'].append(dict(file=name,sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    (work/'manifest-before.json').write_text(json.dumps(before,indent=2)+'\n')
    result=dict(status='failed',source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                parent_context=current_context(),source_media=source_info)
    pool=SharedCacheBudget(limit_bytes=1024**3)
    original_spawn=resident_sr._spawn
    def spawn(*values):
        child=original_spawn(*values);owned.append(child.pid);return child
    def on_frames(done,total):
        nonlocal cancel_at
        progress.append(dict(wall=time.time(),done=done,total=total))
        if args.cancel_at and done>=args.cancel_at and cancel_at is None:
            cancel_at=time.monotonic();cancel.set()
    output=work/'output.mp4'
    watchdog.start()
    try:
      with memory_scope(pool):
        inspection=inspect_source(source,cancel,allow_vfr=True)
        adapter=dlss_engine.available_render_adapters(cancel=cancel)[0]
        pure=dict(settings,nr_quality={'total':0})
        if args.enhance:
            from dlss5tool.device_sr_plan import make_plan
            from dlss5tool.resident_hdr_runtime import load_components
        elif args.hdr:
            from dlss5tool.resident_hdr import make_plan
            from dlss5tool.resident_hdr_runtime import load_components
        else:
            from dlss5tool.resident_sr import make_plan
            from dlss5tool.resident_sr_runtime import load_components
        plan=make_plan(inspection,settings if args.enhance else pure,adapter);components=load_components()
        assert plan is not None and components is not None
        plan.update(nr=args.enhance,multiplier=1,consumer_settings=settings)
        with mock.patch.object(resident_sr,'_spawn',spawn):
            report=resident_sr.export_resident(source,output,plan,components,cancel=cancel,
                frame_progress=on_frames,pool_name=pool.name,audit=args.audit,log_root=work/'logs')
        result.update(status='complete',result=report,output_media=media(output),
            output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),output_bytes=output.stat().st_size)
        info=result['output_media']
        assert info['size']==(3840,2160) and info['frames']==source_info['frames'] and info['rate']==source_info['rate']
        for key in ('audio_packets','audio_sha256','audio_pts_sha256'):assert info[key]==source_info[key],key
        from dlss5tool.video_export import find_ffmpeg,probe_video_stream
        metadata=probe_video_stream(find_ffmpeg(),output)
        assert metadata['is_hdr']==args.hdr,metadata
        result['output_metadata']=metadata
    except Cancelled:
        result['status']='cancelled'
    except BaseException:
        import traceback
        result['error']=traceback.format_exc()
        result['status']='failed'
    finally:
        watchdog.cancel()
        result.update(elapsed_seconds=time.monotonic()-started,progress=progress,
            output_exists=output.exists(),cancel_to_retirement_seconds=None if cancel_at is None else time.monotonic()-cancel_at,
            workers_after_retirement=[process_record(pid) for pid in owned],
            transaction_directories_remaining=[p.name for p in work.glob('.sr-*')],pool_after_retirement=pool.snapshot())
        pool.close()
        current={r['file']:r['sha256'] for r in run_manifest(task)['files']}
        result['source_runtime_unchanged']=all(hashlib.sha256((ROOT/r['file']).read_bytes()).hexdigest()==r['sha256']
            for r in before['files'] if not Path(r['file']).is_absolute())
        result['source_runtime_unchanged'] &= all(current[r['file']]==r['sha256'] for r in before['files'] if r['file'] in current)
        expected='cancelled' if args.cancel_at else 'complete'
        result['passed']=(result['status']==expected and result['source_runtime_unchanged']
            and not any(r['present'] for r in result['workers_after_retirement'])
            and not result['transaction_directories_remaining']
            and result['pool_after_retirement']['reserved_processing_bytes']==0
            and (not args.cancel_at or not result['output_exists']))
        (work/'verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:result.get(k) for k in ('status','passed','elapsed_seconds','cancel_to_retirement_seconds',
        'error','workers_after_retirement','output_bytes')}),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--label',required=True)
    parser.add_argument('--hdr',action='store_true')
    parser.add_argument('--enhance',action='store_true')
    parser.add_argument('--audit',action='store_true')
    parser.add_argument('--cancel-at',type=int)
    args=parser.parse_args()
    if not args.label.replace('-','').isalnum():parser.error('Safe label required')
    raise SystemExit(main(args))
