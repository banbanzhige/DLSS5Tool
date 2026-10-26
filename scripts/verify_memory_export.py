"""Small real encoder/cancellation gate for memory reservation integration."""
import argparse
import json
from pathlib import Path
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',type=Path,required=True)
    parser.add_argument('--label',required=True)
    args=parser.parse_args()
    from scripts.profile_memory_headroom import validate,save
    task=validate(args.task)
    if not args.label.replace('-','').isalnum():raise ValueError('Plain label required')
    destination=task/(args.label+'.json')
    if destination.exists():raise ValueError('Refusing overwrite')
    import numpy as np
    from dlss5tool.gpu_export_runtime import create_video_writer,load_components
    from dlss5tool.gpu_export_process import ProcessGpuVideoWriter
    from dlss5tool.video_export import FFmpegVideoWriter,find_ffmpeg
    from dlss5tool.frame_generation import Cancelled
    from dlss5tool.memory_budget import prefer_pool
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from scripts.encode_acceptance_nvenc_contract import framemd5_file
    components=load_components()
    if not components:raise RuntimeError('Existing GPU encoder components required')
    owner=SharedCacheBudget(limit_bytes=256*1024**2);prefer_pool(owner)
    report=dict(cases=[],scope='8 frames per profile; SDR/PQ/HLG; existing components, no builds')
    try:
        width,height=1920,1080
        bgr=np.zeros((height,width,3),np.uint8)
        bgr[...,0]=np.arange(width,dtype=np.uint16)[None,:]%256
        bgr[...,1]=np.arange(height,dtype=np.uint16)[:,None]%256
        bgr[...,2]=96
        rgba=np.empty((height,width,4),np.float16)
        rgba[...,:3]=bgr[...,::-1].astype(np.float32)/255
        rgba[...,3]=1
        for profile in ('sdr','pq','hlg'):
            metadata=None if profile=='sdr' else dict(color_primaries='bt2020',color_space='bt2020nc',
                color_transfer='smpte2084' if profile=='pq' else 'arib-std-b67')
            frame=bgr if profile=='sdr' else rgba
            runs=[]
            for route in ('baseline','isolated'):
                output=task/f'{args.label}-{profile}-{route}.mp4'
                if output.exists():raise ValueError('Refusing overwrite')
                options=dict(use_nvenc=True,hdr_metadata=metadata)
                writer=(FFmpegVideoWriter(output,width,height,24,**options) if route=='baseline' else
                        create_video_writer(output,width,height,24,gpu_options=components,**options))
                try:
                    if route=='isolated':assert isinstance(writer,ProcessGpuVideoWriter)
                    for _ in range(8):writer.write(frame)
                    writer.finish()
                except BaseException:writer.abort();raise
                decoded=framemd5_file(find_ffmpeg(),output)
                assert decoded['returncode']==0 and decoded['frames']==8
                runs.append(dict(route=route,encoder=writer.encoder_name,decode=decoded))
            assert runs[0]['decode']['md5']==runs[1]['decode']['md5']
            report['cases'].append(dict(profile=profile,status='passed',runs=runs))
            print(profile+': baseline/isolated decoded frames equal',flush=True)
        cancel=threading.Event();cancel.set()
        output=task/f'{args.label}-cancelled.mp4'
        try:
            create_video_writer(output,width,height,24,cancel=cancel,gpu_options=components,use_nvenc=True)
        except Cancelled:pass
        else:raise AssertionError('Constructor ignored cancellation')
        assert not output.exists()
        report['cancel_before_creation']='passed'
        report['ledger_after']=owner.snapshot()
        assert not report['ledger_after']['reserved_processing_bytes']
        assert not report['ledger_after']['waiting_processing_bytes']
        report['status']='passed'
    finally:
        owner.close()
        save(destination,report)
    return 0


if __name__=='__main__':raise SystemExit(main())
