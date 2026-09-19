"""Actual production export loop + NVOFA + DLSSG + GPU conversion/mux gate."""
import argparse
import json
import os
from pathlib import Path
import sys
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.gpu_export_mux_gate import run,probe_packets
from scripts.encode_acceptance_nvenc_contract import framemd5_file,sha256_file


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--label',required=True)
    parser.add_argument('--multiplier',type=int,choices=(1,2,3,4),default=2)
    parser.add_argument('--container',choices=('mp4','mkv','mov'),default='mp4')
    parser.add_argument('--cut',action='store_true')
    parser.add_argument('--hdr',choices=('pq','hlg'))
    parser.add_argument('--enhance',action='store_true')
    args=parser.parse_args();work=args.work.resolve();dest=work/(args.label+'.json')
    if not (work/'TASK.md').is_file() or dest.exists() or not args.label.replace('-','').isalnum():
        parser.error('Fresh registered task and label required')
    os.environ['TEMP']=os.environ['TMP']=str(work);os.environ['CUDA_CACHE_DISABLE']='1'
    from fractions import Fraction
    from dlss5tool.frame_generation import export_video
    from dlss5tool.video_export import find_ffmpeg,find_ffprobe
    ffmpeg=find_ffmpeg();probe=find_ffprobe(ffmpeg)
    report=dict(scope=__doc__,argv=sys.argv,sources={name:sha256_file(ROOT/name) for name in
        ('dlss5tool/frame_generation.py','dlss5tool/gpu_video_export.py','dlss5tool/packet_mux.py',
         'dlss5tool/cuda_interop.py','scripts/dlssg_video_worker.cpp','scripts/gpu_export_fg_gate.py')})
    try:
        source=work/(args.label+'-source.mp4')
        # Real video input with a deliberately short AAC track; 12 source frames.
        if args.hdr:
            fixture=ROOT/'tmp/hdr-e2e'/('source-hdr10.mp4' if args.hdr=='pq' else 'source-hlg.mp4')
            run([ffmpeg,'-hide_banner','-loglevel','error','-n','-i',str(fixture),'-f','lavfi','-i',
                'sine=frequency=997:sample_rate=48000:duration=0.12','-map','0:v:0','-map','1:a:0',
                '-vf','fps=24','-frames:v','12','-c:v','hevc_nvenc','-preset','p5','-profile:v','main10',
                '-pix_fmt','p010le','-color_primaries','bt2020','-colorspace','bt2020nc',
                '-color_trc','smpte2084' if args.hdr=='pq' else 'arib-std-b67','-c:a','aac',str(source)])
        elif args.cut:
            run([ffmpeg,'-hide_banner','-loglevel','error','-n','-f','lavfi','-i',
                'color=c=black:s=320x180:r=24:d=0.25','-f','lavfi','-i','color=c=white:s=320x180:r=24:d=0.25',
                '-f','lavfi','-i','sine=frequency=997:sample_rate=48000:duration=0.12',
                '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]','-map','2:a:0',
                '-frames:v','12','-c:v','libx264','-crf','16','-c:a','aac',str(source)])
        else:run([ffmpeg,'-hide_banner','-loglevel','error','-n','-i','F:/project/test/dlss5/测试素材/9月1日.mp4',
            '-f','lavfi','-i','sine=frequency=997:sample_rate=48000:duration=0.12',
            '-map','0:v:0','-map','1:a:0','-vf','scale=320:180,fps=24','-frames:v','12',
            '-c:v','libx264','-crf','16','-c:a','aac',str(source)])
        options=dict(native_library=str(ROOT/'tmp/encode-integration-20260917/native-encoder/ring.dll'),
            sdr_ptx=str(ROOT/'tmp/encode-integration-20260917/sdr-conversion/sdr-yuv.ptx'),
            worker=str(work/'gpu-export-worker.exe'))
        if args.hdr:options['hdr_ptx']=str(work/'hdr/hdr-yuv-strict.ptx')
        if args.enhance:options['host_library']=str(work/'native-host/candidate.dll')
        report['binaries']={key:sha256_file(path) for key,path in options.items()}
        results={};outputs={}
        for mode in ('baseline','gpu'):
            target=work/(args.label+'-'+mode+'.'+args.container)
            results[mode]=export_video(source,target,multiplier=args.multiplier,enhance=args.enhance,
                settings={'host_backend':'v2','host_persistent_buffers':True,'host_auto_fallback':False,
                          'guidance_mode':0,'output_mix':1,'intensity':.5},
                log_dir=work/(args.label+'-'+mode+'-logs'),
                gpu_export=options if mode=='gpu' else None)
            decoded=framemd5_file(ffmpeg,target)
            timing=probe_packets(probe,target,Fraction(1,24*args.multiplier))
            audio=json.loads(run([probe,'-v','error','-select_streams','a','-show_packets','-show_data_hash','sha256',
                '-show_entries','packet=pts,dts,duration,data_hash:stream=codec_name,time_base',
                '-of','json',str(target)]))
            outputs[mode]=dict(decode=decoded,timing=timing,audio=audio,sha256=sha256_file(target))
            print(f'{mode}: {decoded["frames"]} frames',flush=True)
        report.update(results=results,outputs=outputs)
        left,right=outputs['baseline'],outputs['gpu']
        report['decode_equal']=left['decode']['md5']==right['decode']['md5'] and left['decode']['frames']==right['decode']['frames']==12*args.multiplier
        report['timing_equal']=left['timing']['packets']==right['timing']['packets']
        report['audio_equal']=left['audio']==right['audio']
        counts=('real_frames','generated_frames','cut_holds','endpoint_holds')
        report['counts_equal']=all(results['baseline'][key]==results['gpu'][key] for key in counts)
        if args.cut and results['gpu']['cut_holds']!=args.multiplier-1:
            raise RuntimeError('Cut fixture did not exercise exactly one reset boundary')
        report['status']='passed' if all(report[key] for key in ('decode_equal','timing_equal','audio_equal','counts_equal')) else 'failed'
        print(json.dumps({key:value for key,value in report.items() if key.endswith('equal') or key=='status'}),flush=True)
    except BaseException as error:
        report.update(status='failed',error=repr(error),traceback=traceback.format_exc());traceback.print_exc()
    finally:
        with dest.open('x',encoding='utf-8') as output:json.dump(report,output,ensure_ascii=False,indent=2)
        print(f'Report: {dest}',flush=True)
    return int(report['status']!='passed')


if __name__=='__main__':raise SystemExit(main())
