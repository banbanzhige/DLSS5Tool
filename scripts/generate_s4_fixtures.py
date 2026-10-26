"""Small deterministic SDR/HDR acceptance media; no external/user input files."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe,digest
    from dlss5tool.video_export import find_ffmpeg
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',required=True,type=Path)
    parser.add_argument('--only', help='Generate one new named fixture without replacing earlier ones')
    args=parser.parse_args();task=safe(args.task,ROOT/'tmp')
    if not (task/'TASK.md').is_file() or shutil.disk_usage(task).free<16*1024**3:
        raise ValueError('Registered work and disk reserve required')
    cases=[('hlg-mild-audio',960,540,16,'arib-std-b67','mild','30000/1001',True),
           ('pq-cuts',960,540,16,'smpte2084','cut','30',False),
           ('pq-96',960,540,96,'smpte2084','testsrc2','30',False),
           ('pq4k-mild',1920,1080,8,'smpte2084','mild','30',False),
           ('hlg-mild-96',960,540,96,'arib-std-b67','mild','30000/1001',True)]
    if args.only:
        cases=[row for row in cases if row[0]==args.only]
        if not cases:raise ValueError('Unknown fixture')
    rows=[]
    for name,width,height,frames,transfer,pattern,rate,audio in cases:
        target=safe(task/(name+'.mp4'),task)
        if target.exists():raise FileExistsError(target)
        graph=f'testsrc2=size={width}x{height}:rate={rate}'
        if pattern=='mild':graph+=',lutyuv=y=val*0.4+64:u=(val-128)*0.1+128:v=(val-128)*0.1+128'
        if pattern=='cut':
            graph=(f'color=black:size={width}x{height}:rate={rate},trim=end_frame={frames//2},setpts=PTS-STARTPTS[a];'
                f'color=white:size={width}x{height}:rate={rate},trim=end_frame={frames-frames//2},setpts=PTS-STARTPTS[b];'
                '[a][b]concat=n=2:v=1:a=0')
        command=[find_ffmpeg(),'-hide_banner','-loglevel','error','-n','-f','lavfi','-i',graph]
        if audio:command+=['-f','lavfi','-i','sine=frequency=880:sample_rate=48000']
        command+=['-frames:v',str(frames),'-c:v','libx265','-preset','ultrafast','-threads','2',
            '-x265-params','pools=2:frame-threads=1:log-level=error:colorprim=bt2020:transfer='+transfer+':colormatrix=bt2020nc',
            '-pix_fmt','yuv420p10le','-color_primaries','bt2020','-colorspace','bt2020nc','-color_range','tv','-color_trc',transfer]
        command+=['-c:a','aac','-shortest'] if audio else ['-an']
        command.append(str(target))
        subprocess.run(command,check=True,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
        rows.append(dict(source=str(target),sha256=digest(target),bytes=target.stat().st_size,command=command))
    report=task/('fixtures-'+args.only+'.json' if args.only else 'fixtures.json')
    with report.open('x',encoding='utf-8') as stream:json.dump(rows,stream,indent=2)
    print(json.dumps(dict(files=len(rows),bytes=sum(row['bytes'] for row in rows))))


if __name__=='__main__':main()
