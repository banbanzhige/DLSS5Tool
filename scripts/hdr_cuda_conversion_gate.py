"""Exact HDR GPU conversion gate against the production FFmpeg filter chain."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.hdr_conversion_contract_probe import convert
from scripts.encode_acceptance_nvenc_contract import sha256_file
from dlss5tool.cuda_session import CudaSession
from dlss5tool.cuda_hdr_yuv import CudaHdrConverter


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--label',required=True);parser.add_argument('--ptx',default='hdr-yuv-strict.ptx')
    parser.add_argument('--edge-cases',action='store_true')
    args=parser.parse_args();work=args.work.resolve();dest=work/(args.label+'.json')
    if not (work/'TASK.md').is_file() or dest.exists() or not args.label.replace('-','').isalnum():parser.error('Fresh registered work required')
    os.environ['TEMP']=os.environ['TMP']=str(work);os.environ['CUDA_CACHE_DISABLE']='1'
    import numpy as np
    from dlss5tool.video_export import find_ffmpeg,FFmpegHDRVideoReader,probe_video_stream
    ffmpeg=find_ffmpeg();ptx=work/args.ptx
    report=dict(scope=__doc__,argv=sys.argv,ptx_sha256=sha256_file(ptx),cases=[],
        source_sha256={name:sha256_file(ROOT/name) for name in ('scripts/gpu_hdr_yuv.cu','dlss5tool/cuda_hdr_yuv.py','scripts/hdr_cuda_conversion_gate.py')})
    try:
        with CudaSession() as cuda:
            for w,h in (((17,17),(66,130),(3840,2160)) if args.edge_cases else ((32,16),(320,180),(321,181),(1920,1080))):
                rng=np.random.default_rng(20260919)
                fixtures=dict(random=rng.random((h,w,4)).astype(np.float16),
                    white=np.ones((h,w,4),np.float16),ramp=np.broadcast_to(np.linspace(0,1,w,dtype=np.float16)[None,:,None],(h,w,4)).copy())
                if (w,h)==(320,180):
                    for profile,file in (('pq','source-hdr10.mp4'),('hlg','source-hlg.mp4')):
                        source=ROOT/'tmp/hdr-e2e'/file;meta=probe_video_stream(ffmpeg,str(source))
                        reader=FFmpegHDRVideoReader(source,w,h,meta)
                        try:
                            for index in range(3):fixtures[f'{profile}-{index}']=reader.read()
                        finally:reader.close()
                with cuda.current(),CudaHdrConverter(ptx,w,h) as converter:
                    pointer=cuda.allocate(w*h*16)
                    for name,frame in fixtures.items():
                        transfer='arib-std-b67' if name.startswith('hlg') else 'smpte2084'
                        expected,_=convert(ffmpeg,frame,transfer=transfer)
                        for dtype,layout in ((np.float16,'rgba16f'),(np.float32,'rgba32f')):
                            source=frame.astype(dtype);cuda.upload(pointer,source)
                            lease=converter.convert(pointer,source.nbytes,layout,0)
                            actual=cuda.download(lease.pointer,lease.nbytes)
                            delta=(np.frombuffer(actual,'<u2').astype(np.int32)-np.frombuffer(expected,'<u2').astype(np.int32))//64
                            row=dict(size=[w,h],fixture=name,layout=layout,equal=actual==expected,
                                mismatches=int(np.count_nonzero(delta)),max_abs=int(np.max(np.abs(delta))),
                                expected_sha256=hashlib.sha256(expected).hexdigest(),actual_sha256=hashlib.sha256(actual).hexdigest(),
                                first=np.flatnonzero(delta)[:10].tolist())
                            report['cases'].append(row);print(json.dumps(row),flush=True)
                    if args.edge_cases:
                        source=rng.uniform(-.1,1.1,(h,w,4)).astype(np.float32)
                        source[0,0,:]=[np.nan,np.inf,-np.inf,.5]
                        expected,_=convert(ffmpeg,source)
                        cuda.upload(pointer,source)
                        lease=converter.convert(pointer,source.nbytes,'rgba32f',0)
                        actual=cuda.download(lease.pointer,lease.nbytes)
                        row=dict(size=[w,h],fixture='full-float32-clipping',equal=actual==expected)
                        report['cases'].append(row)
                        try:converter.convert(pointer,source.nbytes,'rgba32f',0,strict=True)
                        except ValueError:row['invalid_rejected']=True
                        else:raise RuntimeError('Invalid HDR accepted by strict FG gate')
            report['status']='passed' if all(row['equal'] for row in report['cases']) else 'failed'
    except BaseException as error:
        report.update(status='failed',error=repr(error),traceback=traceback.format_exc());traceback.print_exc()
    finally:
        with dest.open('x',encoding='utf-8') as output:json.dump(report,output,indent=2)
        print('Report: '+str(dest),flush=True)
    return int(report['status']!='passed')


if __name__=='__main__':raise SystemExit(main())
