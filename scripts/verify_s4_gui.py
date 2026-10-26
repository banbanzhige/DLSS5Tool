"""Exercise installed S4 through the real GUI export method with an inert UI pump."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe,digest
    from scripts.verify_resident_sr import decoded_md5,timeline
    from dlss5tool import app_settings,gui,dlss_engine
    from dlss5tool.render_cache import RenderCache
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.memory_budget import prefer_pool
    from dlss5tool.resident_hdr_runtime import load_components
    from dlss5tool.resident_sr_runtime import load_components as load_s3
    from dlss5tool.process_job import process_record
    from dlss5tool.video_export import find_ffmpeg
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',required=True,type=Path)
    parser.add_argument('--label',required=True)
    parser.add_argument('--consumers',action='store_true')
    args=parser.parse_args()
    task=safe(args.task,ROOT/'tmp')
    report_path=task/(args.label+'.json');output=task/(args.label+'.mp4')
    if not (task/'TASK.md').is_file() or report_path.exists() or output.exists():
        raise ValueError('Fresh registered evidence required')
    os.environ['DLSS5TOOL_SR_LOG_ROOT']=str(task/'logs')
    assert load_components() is not None and load_s3() is not None
    source=task/('hlg-mild-96.mp4' if args.consumers else 'pq-96.mp4')
    adapter=dlss_engine.available_render_adapters()[0]
    multiplier=2 if args.consumers else 1
    settings=app_settings.validate(dict(super_resolution_scale=2,frame_generation_multiplier=multiplier,
        nr_quality={'total':1 if args.consumers else 0},hdr_mode=True,render_gpu=adapter['id']))
    pool=SharedCacheBudget(limit_bytes=1024**3);prefer_pool(pool)
    manager=RenderCache(1024**3,pool_name=pool.name)
    app=gui.App.__new__(gui.App)
    app.root=SimpleNamespace(update=lambda:time.sleep(.001))
    app._shared_manager=lambda:manager
    app._export_cancel_event=threading.Event()
    progress,logs=[],[]
    app.set_progress=lambda value,total,message,**_:progress.append(value/total)
    app.logln=logs.append
    started=time.perf_counter()
    report=dict(accepted=False,scope=__doc__,source_sha256=digest(source))
    try:
        result=app._export_frame_generated_video(str(source),str(output),settings,{})
        assert result['route']=='resident-cuda-hdr-sr' and result['real_frames']==96
        assert progress and progress[-1]==1 and all(b>=a for a,b in zip(progress,progress[1:]))
        assert app._last_export_profile['encoded_frames']==(96*multiplier,96*multiplier)
        assert not process_record(result['worker_pid'])['present']
        md5=decoded_md5(find_ffmpeg(),output)
        rate='60000/1001' if args.consumers else '30'
        info=timeline(output,96*multiplier,[1920,1080],rate)
        if args.consumers:
            original=timeline(source,96,[960,540],'30000/1001')
            for key in ('audio_packets','audio_sha256','audio_pts_sha256'):assert original[key]==info[key]
            assert result['experimental'] and result['warning']
        else:
            assert md5==decoded_md5(find_ffmpeg(),task/'control-pq-96-sr.mp4')
        report.update(accepted=True,result=result,gui_profile=app._last_export_profile,
            progress=progress,logs=logs,decoded_md5=md5,timeline=info,output_sha256=digest(output))
    finally:
        manager.close()
        report['pool_after_close']=pool.snapshot();pool.close()
        report['elapsed_seconds']=time.perf_counter()-started
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(accepted=True,elapsed_seconds=report['elapsed_seconds'],route=result['route'],
        encoded_frames=app._last_export_profile['encoded_frames'],pool_after_close=report['pool_after_close'])))


if __name__=='__main__':main()
