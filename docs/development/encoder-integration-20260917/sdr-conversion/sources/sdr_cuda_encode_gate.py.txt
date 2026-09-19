"""SDR GPU RGB->I420->NVENC gate; excludes D3D producer, scaling, audio and mux."""
import argparse
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dlss5tool.cuda_sdr_yuv import CudaSdrConverter
from dlss5tool.nvenc_yuv import NativeYuvEncoder,config_from_contract
from scripts.encode_acceptance_nvenc_contract import framemd5_bytes,framemd5_file,sha256_file
from scripts.encoder_packet_gate import probe_packets


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--native-root',type=Path,required=True)
    parser.add_argument('--label',default='sdr-cuda-encode')
    parser.add_argument('--source',type=Path,default=Path('F:/project/test/dlss5/测试素材/9月1日.mp4'))
    args=parser.parse_args();work=args.work.resolve();report_path=work/(args.label+'.json')
    if not args.label.replace('-','').isalnum() or not (work/'TASK.md').is_file() or report_path.exists():parser.error('Fresh registered work required')
    os.environ['TEMP']=os.environ['TMP']=str(work);os.environ['CUDA_CACHE_DISABLE']='1'
    import cv2
    import torch
    from dlss5tool.video_export import FFmpegVideoWriter,find_ffmpeg,find_ffprobe
    ffmpeg=find_ffmpeg();probe=find_ffprobe(ffmpeg)
    dll=args.native_root.resolve()/'native-encoder/ring.dll';ptx=work/'sdr-yuv.ptx'
    report=dict(scope=__doc__,argv=sys.argv,source=str(args.source),cases=[],
        native_sha256=sha256_file(dll),ptx_sha256=sha256_file(ptx),
        source_sha256={name:sha256_file(ROOT/name) for name in ('scripts/gpu_sdr_yuv.cu',
            'scripts/sdr_cuda_encode_gate.py','dlss5tool/cuda_sdr_yuv.py','dlss5tool/nvenc_yuv.py')})
    try:
        torch.empty(0,device='cuda')
        for w,h,count,qualities in ((320,180,24,('high','balanced')),
            (321,181,24,('high','balanced')),(1920,1080,24,('high',)),(3840,2160,6,('high',))):
            frames=[];cap=cv2.VideoCapture(str(args.source));fps=cap.get(cv2.CAP_PROP_FPS)
            try:
                for _ in range(count):
                    ok,frame=cap.read()
                    if not ok:raise RuntimeError('Source too short')
                    frames.append(cv2.resize(frame,(w,h),interpolation=cv2.INTER_AREA))
            finally:cap.release()
            for quality in qualities:
                label=f'{args.label}-{w}x{h}-{quality}'
                output=work/(label+'.mp4')
                if output.exists():raise FileExistsError(output)
                writer=FFmpegVideoWriter(str(output),w,h,fps,use_nvenc=True,quality_profile=quality)
                try:
                    if not writer.uses_nvenc:raise RuntimeError('Reference unexpectedly selected software encoder')
                    for frame in frames:writer.write(frame)
                    writer.finish()
                except BaseException:writer.abort();raise
                config=config_from_contract(writer.frame_contract,(w+1)//2*2,(h+1)//2*2,
                    codec='h264',cq=19 if quality=='high' else 23)
                expected_decode=framemd5_file(ffmpeg,output)
                reference_timing=probe_packets(probe,output,config.time_base)
                packets=[]
                with CudaSdrConverter.from_contract(ptx,writer.frame_contract) as converter,NativeYuvEncoder(dll,config) as encoder:
                    headers=encoder.headers
                    for index,frame in enumerate(frames):
                        rgba=cv2.cvtColor(frame,cv2.COLOR_BGR2RGBA)
                        gpu=torch.from_numpy(rgba).cuda();torch.cuda.synchronize()
                        lease=converter.convert(gpu.data_ptr(),gpu.numel(),'rgba',index)
                        # No host readback/upload of RGB or YUV between converter
                        # and encoder. Encoder's synchronous D2D owns a ring slot
                        # before the converter output is reused next iteration.
                        packets.extend(encoder.write(lease))
                    packets.extend(encoder.finish())
                encoded=headers+b''.join(packet.data for packet in packets)
                actual_decode=framemd5_bytes(ffmpeg,encoded)
                timing=[dict(pts=str(p.pts),dts=str(p.dts),duration=str(p.duration),keyframe=p.is_keyframe) for p in packets]
                row=dict(label=label,size=[w,h],frames=count,fps=fps,quality=quality,
                    input_sha256=hashlib.sha256(b''.join(f.tobytes() for f in frames)).hexdigest(),
                    reference_sha256=sha256_file(output),native_sha256=hashlib.sha256(encoded).hexdigest(),
                    expected_decode=expected_decode,actual_decode=actual_decode,
                    reference_timing=reference_timing,native_timing=timing,
                    decode_equal=actual_decode['returncode']==expected_decode['returncode']==0
                        and actual_decode['frames']==expected_decode['frames']==count
                        and actual_decode['md5']==expected_decode['md5'],
                    timing_equal=timing==reference_timing['packets'])
                row['gate']=row['decode_equal'] and row['timing_equal']
                report['cases'].append(row)
                print(json.dumps(dict(label=label,frames=count,gate=row['gate'],decode=row['decode_equal'],timing=row['timing_equal'])),flush=True)
        report['status']='passed' if all(row['gate'] for row in report['cases']) else 'failed'
    except BaseException as exc:
        report.update(status='failed',error=repr(exc),traceback=traceback.format_exc());traceback.print_exc()
    finally:
        with report_path.open('x',encoding='utf-8') as output:json.dump(report,output,ensure_ascii=False,indent=2)
        print(f'Report: {report_path}',flush=True)
    return int(report['status']!='passed')


if __name__=='__main__':raise SystemExit(main())
