"""Connected packet/container gate. Existing CPU FFmpeg remains the reference."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from fractions import Fraction

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.encode_acceptance_nvenc_contract import framemd5_file,sha256_file
from scripts.encoder_packet_gate import make_frames
from dlss5tool.nvenc_yuv import NativeYuvEncoder,YuvEncoderConfig,ReadyYuvFrame
from dlss5tool.cuda_session import CudaSession
from dlss5tool.packet_mux import PacketVideoWriter


def run(command,payload=None):
    result=subprocess.run(command,input=payload,capture_output=True,timeout=40,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace')[-2000:])
    return result.stdout


def probe_packets(probe,path,time_base):
    document=json.loads(run([probe,'-v','error','-select_streams','v:0','-show_packets','-show_streams',
        '-show_entries','packet=pts,dts,duration,flags:stream=time_base,codec_name,profile,pix_fmt,color_transfer,color_primaries,color_space',
        '-of','json',str(path)]))
    stream=document['streams'][0];scale=Fraction(stream['time_base'])/time_base
    packets=[]
    for packet in document['packets']:
        row={key:str(Fraction(packet[key])*scale) if key in packet else None for key in ('pts','dts','duration')}
        row['keyframe']='K' in packet['flags'];packets.append(row)
    return dict(stream=stream,packets=packets)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work',type=Path,required=True);parser.add_argument('--label',default='mux')
    parser.add_argument('--audio',choices=('none','short-aac','long-aac','pcm'),default='none')
    args=parser.parse_args();work=args.work.resolve();report_path=work/(args.label+'.json')
    if not (work/'TASK.md').is_file() or report_path.exists() or not args.label.replace('-','').isalnum():
        parser.error('Fresh registered work required')
    os.environ['TEMP']=os.environ['TMP']=str(work);os.environ['CUDA_CACHE_DISABLE']='1'
    import numpy as np
    import av
    from dlss5tool.video_export import find_ffmpeg,find_ffprobe,build_video_encoder_args,mux_source_audio
    ffmpeg=find_ffmpeg();ffprobe=find_ffprobe(ffmpeg)
    native=ROOT/'tmp/encode-integration-20260917/native-encoder/ring.dll'
    report=dict(scope=__doc__,audio=args.audio,av_version=av.__version__,library_versions=av.library_versions,cases=[],
        sources={name:sha256_file(ROOT/name) for name in ('dlss5tool/packet_mux.py','dlss5tool/cuda_session.py',
                 'scripts/gpu_export_mux_gate.py')},native_sha256=sha256_file(native))
    try:
        audio=None
        if args.audio!='none':
            audio=work/(args.label+('-audio.wav' if args.audio=='pcm' else '-audio.m4a'))
            run([ffmpeg,'-hide_banner','-loglevel','error','-n','-f','lavfi','-i','sine=frequency=997:sample_rate=48000',
                '-t','0.12' if args.audio=='short-aac' else '1.2','-c:a','pcm_s16le' if args.audio=='pcm' else 'aac',str(audio)])
        for codec,profile in enumerate(('sdr','pq','hlg')):
            config=YuvEncoderConfig(160,128,30000,1001,codec)
            frames=make_frames(np,config,24)
            packets=[]
            with CudaSession() as cuda:
                pointer=cuda.allocate(config.frame_bytes)
                with cuda.current(),NativeYuvEncoder(native,config) as encoder:
                    headers=encoder.headers
                    for index,frame in enumerate(frames):
                        cuda.upload(pointer,frame)
                        packets.extend(encoder.write(ReadyYuvFrame(pointer,config.frame_bytes,config.layout,index)))
                    packets.extend(encoder.finish())
            colors=[] if not codec else ['-color_range','tv','-color_primaries','bt2020','-color_trc',
                'smpte2084' if codec==1 else 'arib-std-b67','-colorspace','bt2020nc']
            for extension in ('mp4','mkv','mov'):
                label=f'{args.label}-{profile}.{extension}'
                ref=work/('ref-'+label);actual=work/label
                cmd=[ffmpeg,'-hide_banner','-loglevel','error','-n','-f','rawvideo','-pixel_format',config.layout,
                    '-video_size','160x128','-framerate','30000/1001',*colors,'-i','pipe:0','-an',
                    *build_video_encoder_args(bool(codec),True),*colors]
                if extension in ('mp4','mov'):
                    if codec:cmd+=['-tag:v','hvc1']
                    cmd+=['-movflags','+faststart']
                run(cmd+[str(ref)],b''.join(frame.tobytes() for frame in frames))
                if audio:
                    combined=work/('audio-ref-'+label)
                    reference_audio_mode=mux_source_audio(ffmpeg,str(ref),str(audio),str(combined))
                    ref=combined
                mux=PacketVideoWriter(actual,config,headers,audio_source=audio)
                try:
                    for packet in packets:mux.write(packet)
                    mux.finish(24)
                except BaseException:mux.abort();raise
                expected=framemd5_file(ffmpeg,ref);decoded=framemd5_file(ffmpeg,actual)
                et=probe_packets(ffprobe,ref,config.time_base);at=probe_packets(ffprobe,actual,config.time_base)
                row=dict(profile=profile,container=extension,expected=expected,actual=decoded,
                    reference_timing=et,actual_timing=at,reference_sha256=sha256_file(ref),actual_sha256=sha256_file(actual))
                row['equal']=expected['returncode']==decoded['returncode']==0 and expected['md5']==decoded['md5'] and expected['frames']==decoded['frames']==24
                row['timing_equal']=et['packets']==at['packets']
                row['audio_equal']=True
                if audio:
                    def audio_packets(path):
                        return json.loads(run([ffprobe,'-v','error','-select_streams','a',
                            '-show_packets','-show_data_hash','sha256','-show_entries',
                            'packet=pts,dts,duration,data_hash:stream=codec_name,time_base,sample_rate,channels',
                            '-of','json',str(path)]))
                    row['reference_audio']=audio_packets(ref);row['actual_audio']=audio_packets(actual)
                    row['audio_mode']=mux.audio_mode;row['reference_audio_mode']=reference_audio_mode
                    row['audio_equal']=row['reference_audio']==row['actual_audio'] and mux.audio_mode==reference_audio_mode
                report['cases'].append(row)
                print(json.dumps(dict(profile=profile,container=extension,equal=row['equal'],timing=row['timing_equal'],audio=row['audio_equal'])),flush=True)
        report['status']='passed' if all(r['equal'] and r['timing_equal'] and r['audio_equal'] for r in report['cases']) else 'failed'
    except BaseException as exc:
        report.update(status='failed',error=repr(exc),traceback=traceback.format_exc());traceback.print_exc()
    finally:
        with report_path.open('x',encoding='utf-8') as output:json.dump(report,output,indent=2)
        print('Report: '+str(report_path),flush=True)
    return int(report['status']!='passed')


if __name__=='__main__':raise SystemExit(main())
