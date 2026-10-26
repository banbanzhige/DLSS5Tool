"""Archive exact-pixel, timeline/audio, lifecycle and throughput S3 acceptance."""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def timeline(path, count, size, rate):
    import av
    av.logging.restore_default_callback()
    video_pts, video_dts, audio_pts = [], [], []
    audio = hashlib.sha256()
    audio_count = 0
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        assert (stream.width, stream.height) == tuple(size)
        assert Fraction(stream.average_rate) == Fraction(rate)
        for packet in container.demux():
            if not packet.size:
                continue
            if packet.stream.type == 'video' and packet.stream.index == stream.index:
                video_pts.append(Fraction(packet.pts)*packet.time_base)
                video_dts.append(Fraction(packet.dts)*packet.time_base)
            elif packet.stream.type == 'audio':
                audio.update(bytes(packet)); audio_count += 1
                audio_pts.append(str(Fraction(packet.pts)*packet.time_base))
    assert len(video_pts) == count
    assert sorted(video_pts) == [Fraction(index)/Fraction(rate) for index in range(count)]
    assert all(right > left for left,right in zip(video_dts,video_dts[1:]))
    return dict(frames=count, rate=rate, size=size, pts_exact=True, dts_monotonic=True,
        audio_packets=audio_count, audio_sha256=audio.hexdigest(),
        audio_pts_sha256=hashlib.sha256(json.dumps(audio_pts).encode()).hexdigest())


def decoded_md5(ffmpeg, path):
    result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-threads', '2',
        '-i', str(path), '-map', '0:v:0', '-f', 'md5', '-'], check=True, capture_output=True,
        text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.stdout.startswith('MD5=')
    return result.stdout.strip()[4:]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',required=True,type=Path)
    args=parser.parse_args()
    from scripts.build_vsr_cuda import safe,digest
    from dlss5tool.video_export import find_ffmpeg
    import av
    task=safe(args.task,ROOT/'tmp')
    target=task/'verification-summary.json'
    if not (task/'TASK.md').is_file() or target.exists():raise ValueError('Fresh registered task required')
    def read(name):return json.loads((task/(name+'.json')).read_text(encoding='utf-8'))
    report=dict(schema=1,accepted=False,pyav=av.__version__,av_libraries=av.library_versions,pairs=[])
    for control,candidate in [('control4k-sr','resident4k-audit'),
                              ('control1080-full709-sr','resident1080-full709-audit'),
                              ('control4x-balanced-sr','resident4x-balanced-audit')]:
        old,new=read(control),read(candidate)
        result=new['result']
        assert old['status']==new['status']=='complete'
        assert old['source_sha256']==new['source_sha256']
        assert old['frame_hashes']==result['frame_hashes'] and len(old['frame_hashes'])==result['real_frames']==96
        old_md5=decoded_md5(find_ffmpeg(),task/(control+'.mp4'))
        new_md5=decoded_md5(find_ffmpeg(),task/(candidate+'.mp4'))
        assert old_md5==new_md5
        info=timeline(task/(candidate+'.mp4'),96,[result['width'],result['height']],result['output_rate'])
        old_info=timeline(task/(control+'.mp4'),96,[result['width'],result['height']],result['output_rate'])
        assert info==old_info
        report['pairs'].append(dict(control=control,candidate=candidate,
            input_pixels_equal=True,decoded_md5=new_md5,timeline=info))
    for name in ('resident4k-speed2','resident4k-long'):
        data=read(name); result=data['result']
        assert data['status']=='complete' and not data['worker_after_retirement']['present']
        assert not result['audit'] and not result['frame_hashes']
        assert data['pool_after_retirement']['reserved_processing_bytes']==0
        report[name]=dict(elapsed_seconds=data['elapsed_seconds'],result=result,
            timeline=timeline(task/(name+'.mp4'),result['real_frames'],[result['width'],result['height']],result['output_rate']))
    long=read('resident4k-long')
    original=timeline(task/'long-audio-1920x1080-1800.mp4',1800,[1920,1080],'30')
    for key in ('audio_packets','audio_sha256','audio_pts_sha256'):
        assert original[key]==report['resident4k-long']['timeline'][key]
    cancel=read('resident-cancel')
    assert cancel['status']=='cancelled' and not cancel['output_exists'] and not cancel['worker_after_retirement']['present']
    assert not cancel['transaction_directories_remaining'] and cancel['pool_after_retirement']['reserved_processing_bytes']==0
    report['cancellation_seconds']=cancel['elapsed_seconds']-cancel['cancel_requested_seconds']
    assert report['cancellation_seconds']<5
    assert read('nv12-gpu-fixtures')['accepted']
    assert all(row['stage']!='failed' for row in [json.loads(line) for line in
        (task/'nv12-resident-pixels-events.jsonl').read_text().splitlines()])
    baseline=read('control4k-speed-sr')['elapsed_seconds']
    speed=read('resident4k-speed2')['elapsed_seconds']
    assert speed<baseline
    report['performance']=dict(control_seconds=baseline,resident_seconds=speed,
        time_reduction=1-speed/baseline,throughput_ratio=baseline/speed)
    report['adapter']=long['result']['adapter']
    report['kernel_build']=read('nv12-rgba-build')
    assert report['kernel_build']['ptx_sha256']==digest(task/'nv12-rgba.ptx')
    assert report['kernel_build']['source_sha256']==digest(ROOT/'scripts/gpu_nv12_rgba.cu')
    report['accepted']=True
    target.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(dict(accepted=True,performance=report['performance'],
        long_frames=1800,cancellation_seconds=report['cancellation_seconds'])))


if __name__=='__main__':main()
