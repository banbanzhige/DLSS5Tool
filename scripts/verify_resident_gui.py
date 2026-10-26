"""Run the real GUI export method with an inert UI pump and installed components.

This verifies routing, background execution, progress and output. It is not a
visual widget/layout test, and does not create or alter a user GUI session.
"""
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
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True)
    args=parser.parse_args()
    from scripts.build_vsr_cuda import safe,digest
    task=safe(args.task,ROOT/'tmp')
    report_path=task/'gui-entry.json';output=task/'gui-entry.mp4'
    if not (task/'TASK.md').is_file() or report_path.exists() or output.exists():
        raise ValueError('Fresh registered task required')
    os.environ['TEMP']=os.environ['TMP']=str(task)
    os.environ['DLSS5TOOL_SR_LOG_ROOT']=str(task/'logs')
    from dlss5tool import app_settings,gui,dlss_engine
    from dlss5tool.render_cache import RenderCache
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.memory_budget import prefer_pool
    from dlss5tool.resident_sr_runtime import load_components
    from scripts.verify_resident_sr import decoded_md5,timeline
    from dlss5tool.video_export import find_ffmpeg
    from dlss5tool.process_job import process_record
    assert load_components() is not None
    adapter=dlss_engine.available_render_adapters()[0]
    settings=app_settings.validate(dict(super_resolution_scale=2,frame_generation_multiplier=1,
        nr_quality={'total':0},hdr_mode=False,render_gpu=adapter['id']))
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
    try:
        result=app._export_frame_generated_video(str(task/'synthetic-1920x1080-96.mp4'),str(output),settings,{})
        elapsed=time.perf_counter()-started
        assert result['route']=='resident-cuda-sr' and result['real_frames']==96
        assert progress and progress[-1]==1 and all(b>=a for a,b in zip(progress,progress[1:]))
        assert app._last_export_profile['encoded_frames']==(96,96)
        assert not process_record(result['worker_pid'])['present']
        evidence=json.loads((task/'verification-summary.json').read_text(encoding='utf-8'))
        md5=decoded_md5(find_ffmpeg(),output)
        assert md5==evidence['pairs'][0]['decoded_md5']
        info=timeline(output,96,[3840,2160],'30')
        report=dict(accepted=True,scope=__doc__,elapsed_seconds=elapsed,result=result,
            gui_profile=app._last_export_profile,progress=progress,logs=logs,
            decoded_md5=md5,timeline=info,output_sha256=digest(output))
    finally:
        manager.close()
        snapshot=pool.snapshot();pool.close()
    report['pool_after_close']=snapshot
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(accepted=True,elapsed_seconds=elapsed,route=result['route'],
        progress_monotonic=True,decoded_md5=md5,pool_after_close=snapshot)))


if __name__=='__main__':main()
