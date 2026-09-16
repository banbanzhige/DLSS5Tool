"""Bounded FFmpeg NVDEC/NVENC capability and decoded-byte equivalence tests.

This does NOT integrate hardware frames into DLSS. No encoded media written:
FFmpeg still runs NVENC, then discards compressed packets through the null muxer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dlss5tool.video_export import find_ffmpeg,probe_video_stream,build_video_encoder_args


def run(command):
    started=time.perf_counter()
    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return dict(command=command,seconds=time.perf_counter()-started,returncode=result.returncode,
                stdout=result.stdout.decode('utf-8',errors='replace'),stderr=result.stderr.decode('utf-8',errors='replace'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--label',required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--frames',type=int,default=96)
    p.add_argument('--rounds',type=int,default=3)
    a=p.parse_args();a.work=a.work.resolve()
    dest=a.work/(a.label+'.json')
    if (not (a.work/'TASK.md').is_file() or dest.exists() or not a.label.replace('-','').isalnum()
        or not 8<=a.frames<=120 or not 1<=a.rounds<=5):p.error('Registered task and bounded arguments required')
    ffmpeg=find_ffmpeg();meta=probe_video_stream(ffmpeg,str(a.source))
    fmt='p010le' if meta['is_hdr'] else 'nv12'
    prefix=[ffmpeg,'-hide_banner','-nostdin','-loglevel','error']
    def input_args(gpu):
        return (['-hwaccel','cuda','-hwaccel_output_format','cuda'] if gpu else [])+['-i',str(a.source)]
    report=dict(scope='FFmpeg-only codec path, not DLSS export',source=str(a.source),metadata=meta,
                requested_frames=a.frames,ffmpeg_version=run([ffmpeg,'-version'])['stdout'].splitlines()[0],
                script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),cases=[])
    try:
        hashes={}
        for gpu in (False,True):
            filters=('hwdownload,' if gpu else '')+'format='+fmt
            result=run(prefix+input_args(gpu)+['-frames:v',str(a.frames),'-an','-vf',filters,'-f','framemd5','-'])
            rows=[line.split(',')[-1].strip() for line in result['stdout'].splitlines() if line and not line.startswith('#')]
            hashes['gpu' if gpu else 'cpu']=rows
            report['cases'].append(dict(phase='decoded_byte_audit',gpu=gpu,decoded_frames=len(rows),**result))
        report['decoded_identical']=bool(hashes['cpu']) and hashes['cpu']==hashes['gpu']
        report['decoded_frames']=len(hashes['cpu'])
        args=build_video_encoder_args(meta['is_hdr'],True,nvenc_preset='p5',quality_profile='high',codec='hevc' if meta['is_hdr'] else 'h264')
        for repeat in range(a.rounds):
            for gpu in ((False,True) if repeat%2==0 else (True,False)):
                result=run(prefix+input_args(gpu)+['-frames:v',str(a.frames),'-an',*args,
                    '-progress','pipe:1','-nostats','-f','null','-'])
                frame_lines=[line for line in result['stdout'].splitlines() if line.startswith('frame=')]
                count=int(frame_lines[-1].split('=')[1]) if frame_lines else 0
                record=dict(phase='decode_encode',gpu=gpu,repeat=repeat,encoded_frames=count,**result)
                report['cases'].append(record)
                print(json.dumps({k:v for k,v in record.items() if k not in ('stdout','stderr','command')}),flush=True)
        report['summary']={str(gpu):statistics.mean(r['seconds'] for r in report['cases']
            if r['phase']=='decode_encode' and r['gpu']==gpu) for gpu in (False,True)}
        report['status']='measurements_completed'
        report['full_requested_count']=all(r.get('encoded_frames',a.frames)==a.frames for r in report['cases'])
        report['errors']=[r['stderr'] for r in report['cases'] if r['returncode']]
        if report['errors'] or not report['full_requested_count'] or not report['decoded_identical']:
            report['status']='validation_failed'
    except BaseException as error:
        report.update(status='failed',error=repr(error))
        raise
    finally:
        with dest.open('x',encoding='utf-8') as out:json.dump(report,out,ensure_ascii=False,indent=2)
        print('Report: '+str(dest),flush=True)


if __name__=='__main__':main()
