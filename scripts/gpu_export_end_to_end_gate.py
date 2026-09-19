"""Final CPU-frame writer route: isolated GPU conversion/encode/mux vs FFmpeg."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.encode_acceptance_nvenc_contract import framemd5_file,sha256_file


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--label',required=True);parser.add_argument('--long',action='store_true')
    parser.add_argument('--automatic',action='store_true')
    args=parser.parse_args();work=args.work.resolve();dest=work/(args.label+'.json')
    if not (work/'TASK.md').is_file() or dest.exists() or not args.label.replace('-','').isalnum():parser.error('Fresh registered work required')
    os.environ['TEMP']=os.environ['TMP']=str(work);os.environ['CUDA_CACHE_DISABLE']='1'
    import numpy as np
    from dlss5tool.gpu_export_runtime import create_video_writer
    from dlss5tool.video_export import FFmpegVideoWriter,find_ffmpeg
    ffmpeg=find_ffmpeg();options=dict(native_library=str(ROOT/'tmp/encode-integration-20260917/native-encoder/ring.dll'),
        sdr_ptx=str(ROOT/'tmp/encode-integration-20260917/sdr-conversion/sdr-yuv.ptx'),hdr_ptx=str(work/'hdr/hdr-yuv-strict.ptx'))
    report=dict(scope=__doc__,argv=sys.argv,cases=[],note='No exclusive GPU reservation; local observed timings only.')
    try:
        for w,h,count in (((1920,1080,720),) if args.long else ((320,180,48),(1920,1080,96))):
            yy,xx=np.indices((h,w));bgr=np.stack((xx%256,yy%256,(xx+yy)%256),axis=-1).astype(np.uint8)
            rgba=np.concatenate((bgr[...,::-1].astype(np.float32)/255,np.ones((h,w,1),np.float32)),axis=-1).astype(np.float16)
            for profile in (('pq','hlg') if args.long else ('sdr','pq','hlg')):
                meta=None if profile=='sdr' else dict(color_transfer='smpte2084' if profile=='pq' else 'arib-std-b67',
                    color_primaries='bt2020',color_space='bt2020nc')
                frame=bgr if profile=='sdr' else rgba
                runs=[]
                for repeat in range(2):
                    for route in (('baseline','gpu') if repeat==0 else ('gpu','baseline')):
                        output=work/f'{args.label}-{w}-{profile}-{repeat}-{route}.mp4'
                        start=time.perf_counter()
                        if route=='baseline':writer=FFmpegVideoWriter(output,w,h,24,use_nvenc=True,hdr_metadata=meta)
                        else:writer=create_video_writer(output,w,h,24,use_nvenc=True,hdr_metadata=meta,
                            **({} if args.automatic else {'gpu_options':options}))
                        try:
                            for _ in range(count):writer.write(frame)
                            writer.finish()
                        except BaseException:writer.abort();raise
                        elapsed=time.perf_counter()-start
                        decoded=framemd5_file(ffmpeg,output)
                        runs.append(dict(route=route,repeat=repeat,seconds=elapsed,decode=decoded,sha256=sha256_file(output)))
                        print(json.dumps(dict(size=[w,h],profile=profile,route=route,seconds=round(elapsed,3))),flush=True)
                expected=runs[0]['decode']['md5']
                equal=all(r['decode']['returncode']==0 and r['decode']['frames']==count and r['decode']['md5']==expected for r in runs)
                report['cases'].append(dict(size=[w,h],profile=profile,frames=count,runs=runs,equal=equal))
        report['status']='passed' if all(c['equal'] for c in report['cases']) else 'failed'
    except BaseException as error:
        report.update(status='failed',error=repr(error),traceback=traceback.format_exc());traceback.print_exc()
    finally:
        with dest.open('x',encoding='utf-8') as output:json.dump(report,output,indent=2)
        print(f'{report["status"]}: {dest}',flush=True)
    return int(report['status']!='passed')


if __name__=='__main__':raise SystemExit(main())
