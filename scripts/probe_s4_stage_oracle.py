"""Serial CPU-frame oracle for 4K SR/NR/FG when concurrent legacy VRAM is insufficient."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe, digest
    from dlss5tool import app_settings, dlss_engine
    from dlss5tool.video_export import FFmpegHDRVideoReader
    from dlss5tool.frame_generation import inspect_source, scene_thumbnail, is_cut, NativeStream, check_cancel
    from dlss5tool.guidance_color import analysis_rgba8
    from dlss5tool.super_resolution import ProcessSuperResolution
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.nvofa import OpticalFlow, align_size
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.memory_budget import memory_scope
    from dlss5tool.process_job import process_record
    import cv2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True)
    parser.add_argument('--label',required=True)
    args = parser.parse_args()
    task = safe(args.task,ROOT/'tmp')
    target = task/(args.label+'.json')
    directory = safe(task/(args.label+'-frames'),task)
    if not (task/'TASK.md').is_file() or target.exists() or directory.exists():
        raise ValueError('Fresh registered evidence required')
    source = task/'pq4k-mild.mp4'
    if shutil.disk_usage(task).free < 17*1024**3:
        raise ValueError('1.1 GiB rebuildable frame budget and 15 GiB reserve required')
    directory.mkdir()
    cancel = threading.Event()
    watchdog = threading.Timer(150,cancel.set)
    watchdog.daemon=True;watchdog.start()
    adapter = dlss_engine.available_render_adapters()[0]
    meta,rate,w,h,count = inspect_source(source,cancel,allow_vfr=True)
    assert (w,h,count)==(1920,1080,8)
    config = app_settings.validate(dict(super_resolution_scale=2,frame_generation_multiplier=2,
        nr_quality={'total':1},hdr_mode=True,render_gpu=adapter['id'],host_backend='v2',
        host_in_flight=1,host_submission='merged',host_persistent_buffers=True,host_auto_fallback=False,
        output_mix=1,output_view=0,guidance_mode=0))
    config.update(frame_format='rgba16f',color_profile=meta['profile'],color_primaries=meta['color_primaries'])
    report = dict(accepted=False,scope=__doc__,source_sha256=digest(source),settings=config,
                  cuts=[],frame_hashes=[],sr_hashes=[],nr_hashes=[],owners=[])
    pool = SharedCacheBudget(limit_bytes=256*1024**2)
    started = time.perf_counter()
    sr=reader=nr=native=flow=None
    owners=[]
    def hashed(array):return hashlib.sha256(memoryview(array)).hexdigest()
    try:
      with memory_scope(pool):
        # Preserve each model's continuous history. Only the *stages* are
        # serialized, with exact full-precision snapshots between them.
        sr=ProcessSuperResolution(w,h,2,is_hdr=True,render_gpu=adapter['id'])
        owners.append(sr._process.pid)
        reader=FFmpegHDRVideoReader(source,w,h,meta)
        previous_scene=None
        for index in range(count):
            check_cancel(cancel)
            pixels=sr.process(reader.read())
            scene=scene_thumbnail(analysis_rgba8(pixels,config))
            if previous_scene is not None and is_cut(previous_scene,scene):report['cuts'].append(index)
            previous_scene=scene
            report['sr_hashes'].append(hashed(pixels))
            np.save(directory/f'sr-{index}.npy',pixels,allow_pickle=False)
        pixels=None
        reader.close();reader=None
        sr.close();sr=None
        nr=ProcessLive(w*2,h*2,config)
        owners.append(nr._session._process.pid)
        for index in range(count):
            check_cancel(cancel)
            pixels=np.load(directory/f'sr-{index}.npy',allow_pickle=False)
            enhanced=nr.process(pixels,reset=index==0 or index in report['cuts'])
            report['nr_hashes'].append(hashed(enhanced))
            np.save(directory/f'nr-{index}.npy',enhanced,allow_pickle=False)
        pixels=enhanced=None
        nr.close();nr=None
        fw,fh=align_size(w*2,h*2)
        flow=OpticalFlow(fw,fh,grid=1)
        native=NativeStream(w*2,h*2,2,True,directory,cancel)
        owners.append(native.proc.pid)
        previous=previous_proxy=None
        for index in range(count):
            check_cancel(cancel)
            pixels=np.load(directory/f'nr-{index}.npy',allow_pickle=False)
            proxy=analysis_rgba8(pixels,config)
            cut=index in report['cuts']
            if previous is None or cut:
                motion=np.zeros((h*2,w*2,2),np.float32)
            else:
                a=cv2.copyMakeBorder(proxy[...,:3],0,fh-h*2,0,fw-w*2,cv2.BORDER_REPLICATE)
                b=cv2.copyMakeBorder(previous_proxy[...,:3],0,fh-h*2,0,fw-w*2,cv2.BORDER_REPLICATE)
                motion=np.ascontiguousarray(flow.calculate(a,b)[:h*2,:w*2])
            frames,valid=native.process(pixels,motion,reset=previous is None or cut)
            if previous is not None:
                if not valid and not cut:raise ValueError('Legacy oracle FG invalid')
                report['frame_hashes'].extend(hashed(previous if cut else frame) for frame in frames)
            report['frame_hashes'].append(hashed(pixels))
            previous=pixels
            previous_proxy=proxy
        report['frame_hashes'].append(hashed(previous))
        report['accepted']=len(report['frame_hashes'])==count*2
    except BaseException as error:
        import traceback
        traceback.print_exc()
        report['error']=type(error).__name__+': '+str(error)
    finally:
        watchdog.cancel()
        for item in (native,flow,nr,reader,sr):
            if item:item.close()
        report['pool_after_close']=pool.snapshot()
        pool.close()
        report['owners']=[process_record(pid) for pid in owners]
        report['elapsed_seconds']=time.perf_counter()-started
        report['accepted']=report['accepted'] and all(not row['present'] for row in report['owners'])
        target.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('accepted','elapsed_seconds','error','owners') if key in report}))
    return 0 if report['accepted'] else 1


if __name__=='__main__':
    raise SystemExit(main())
