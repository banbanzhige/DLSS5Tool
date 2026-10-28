"""Bounded synthetic probe of export timing/audio, without model inference.

Run from the repository root with the existing development Python environment.
The caller provides a new task-local work directory; no existing files are
replaced or deleted. A delayed write simulates inference latency, not its pixels.
"""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import wave

import av
import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dlss5tool.image_sequence import open_capture
from dlss5tool.video_export import FFmpegVideoWriter, find_ffmpeg, find_ffprobe
from dlss5tool.video_timeline import inspect_video_timeline


def new_workspace(value):
    repo = Path(__file__).resolve().parents[1]
    path = Path(value).absolute()
    # Refuse links before resolving, including Windows directory junctions.
    for parent in (path, *path.parents):
        try:
            attributes = getattr(parent.lstat(), 'st_file_attributes', 0)
        except FileNotFoundError:
            continue
        if parent.is_symlink() or attributes & 0x400:
            raise ValueError(f'Linked workspace component: {parent}')
    path = path.resolve()
    path.relative_to(repo / 'tmp')
    if path.exists():
        raise FileExistsError(path)
    # All fixtures plus decoded snapshots stay below 100 MiB.
    if shutil.disk_usage(repo).free < 15 * 1024**3 + 100 * 1024**2:
        raise ValueError('Probe needs its estimated peak plus 15 GiB of free space')
    path.mkdir(parents=True)
    return path


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def snapshot(path):
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        rate = str(stream.average_rate)
        frames = list(container.decode(stream))
        times = [Fraction(frame.pts) * frame.time_base for frame in frames]
        levels = [float(frame.to_ndarray(format='gray').mean()) for frame in frames]
        marker = next((index for index, level in enumerate(levels) if level > 150), None)
        end = stream.duration * stream.time_base if stream.duration is not None else None
        video_start = times[0]
    audio_pts, audio_data = [], hashlib.sha256()
    with av.open(str(path)) as container:
        streams = container.streams.audio
        for packet in container.demux(*streams) if streams else ():
            if packet.dts is not None:
                audio_pts.append(str(Fraction(packet.pts) * packet.time_base))
                audio_data.update(bytes(packet))
    probe = subprocess.check_output([
        find_ffprobe(find_ffmpeg()), '-v', 'error', '-show_entries',
        'stream=index,codec_type,start_time,duration:format=start_time,duration',
        '-of', 'json', str(path),
    ], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return dict(frames=len(times), average_rate=rate, video_start=str(video_start),
                video_duration=str(end) if end is not None else None,
                marker_time=str(times[marker]) if marker is not None else None,
                pts=[str(value) for value in times], audio_first_pts=audio_pts[0] if audio_pts else None,
                audio_pts_sha256=hashlib.sha256(json.dumps(audio_pts).encode()).hexdigest(),
                audio_payload_sha256=audio_data.hexdigest(), probe=json.loads(probe))


def make_video(path, stamps=None):
    options = dict(frame_timestamps=stamps, timeline_end='4') if stamps else {}
    writer = FFmpegVideoWriter(path, 128, 128, 30, use_nvenc=False, **options)
    try:
        for index in range(120):
            # CFR event is at 2 s; VFR event is also at 2 s, after 30 frames.
            marker = 30 if stamps else 60
            writer.write(np.full((128, 128, 3), 230 if index >= marker else 20, np.uint8))
        writer.finish()
    except BaseException:
        writer.abort()
        raise


def export_frames(source, target, *, delay=0, preserve=True):
    # Match ordinary GUI export: decoded frames + capture's average FPS + source audio.
    capture = open_capture(source, allow_cpu_fallback=True)
    writer = None
    started = time.perf_counter()
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        options = {}
        if preserve:
            timeline = inspect_video_timeline(source, fps, allow_cpu_fallback=True)
            options = timeline.writer_options()
            fps = float(timeline.rate)
        writer = FFmpegVideoWriter(target, 128, 128, fps, audio_source=source,
                                   use_nvenc=False, **options)
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if delay:
                time.sleep(delay)
            writer.write(frame)
        writer.finish()
        return dict(seconds=round(time.perf_counter() - started, 4),
                    decode_backend=capture.backend, audio_mode=writer.audio_mode)
    except BaseException:
        if writer:
            writer.abort()
        raise
    finally:
        capture.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True)
    parser.add_argument('--require-sync', action='store_true', help='Fail if any case changes relative A/V timing')
    args = parser.parse_args()
    work = new_workspace(args.work)
    ffmpeg = find_ffmpeg()
    sample_rate = 48000
    samples = np.zeros(4 * sample_rate, np.float64)
    start = 2 * sample_rate
    pulse = np.arange(sample_rate // 10) / sample_rate
    samples[start:start + len(pulse)] = np.sin(2*np.pi*880*pulse) * .5
    wav = work / 'pulse.wav'
    with wave.open(str(wav), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes((samples * 32767).astype('<i2').tobytes())
    cfr, vfr = work / 'cfr-video.mp4', work / 'vfr-video.mp4'
    make_video(cfr)
    stamps = [str(Fraction(index, 15)) for index in range(30)]
    stamps += [str(Fraction(2) + Fraction(index, 45)) for index in range(90)]
    make_video(vfr, stamps)
    sources = {}
    for name, video, video_offset, audio_offset in (
        ('cfr', cfr, '0', '0'), ('vfr', vfr, '0', '0'),
        ('audio-late', cfr, '0', '0.75'),
        ('video-late', cfr, '1', '0'),
        ('both-offset', cfr, '1', '1.75'),
    ):
        source = work / f'{name}-source.mp4'
        run(ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-n',
            '-copyts', '-itsoffset', video_offset, '-i', video,
            '-itsoffset', audio_offset, '-i', wav,
            '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', source)
        sources[name] = source
    results = {'scope': 'Synthetic media, real capture/writer/mux, no NR or guidance model.',
               'ffmpeg': str(ffmpeg), 'pyav': av.__version__,
               'delay_seconds_per_frame': .04, 'cases': {}}
    for name, source in sources.items():
        target = work / f'{name}-export.mp4'
        execution = export_frames(source, target)
        results['cases'][name] = dict(source=snapshot(source), output=snapshot(target), **execution)
    for name, source, delay, preserve in (
        ('cfr-slow', sources['cfr'], .04, True),
        ('vfr-retimed', sources['vfr'], 0, True),
    ):
        target = work / f'{name}-export.mp4'
        execution = export_frames(source, target, delay=delay, preserve=preserve)
        results['cases'][name] = dict(source=snapshot(source), output=snapshot(target), **execution)
    for case in results['cases'].values():
        source, output = case['source'], case['output']
        if source['frames'] != output['frames'] or output['frames'] != 120:
            raise AssertionError('Missing frames invalidate the timing comparison')
        case['audio_payload_equal'] = source['audio_payload_sha256'] == output['audio_payload_sha256']
        if not case['audio_payload_equal']:
            raise AssertionError('Changed audio payload invalidates the offset comparison')
        relative_source = Fraction(source['marker_time']) - Fraction(source['audio_first_pts'])
        relative_output = Fraction(output['marker_time']) - Fraction(output['audio_first_pts'])
        case['marker_audio_relative_shift_seconds'] = float(relative_output - relative_source)
        source_origin = Fraction(source['video_start'])
        output_origin = Fraction(output['video_start'])
        case['max_normalized_video_pts_error_seconds'] = float(max(abs(
            Fraction(b) - output_origin - (Fraction(a) - source_origin)
        ) for a, b in zip(source['pts'], output['pts'])))
    cases = results['cases']
    if cases['cfr']['output']['pts'] != cases['cfr-slow']['output']['pts']:
        raise AssertionError('Write latency changed the control timeline')
    if abs(cases['vfr-retimed']['marker_audio_relative_shift_seconds']) > 1e-6:
        raise AssertionError('Source-PTS control did not preserve marker/audio alignment')
    if args.require_sync:
        for name, case in cases.items():
            if abs(case['marker_audio_relative_shift_seconds']) > 1e-6:
                raise AssertionError(f'A/V offset changed: {name}')
            if case['max_normalized_video_pts_error_seconds'] > 1e-6:
                raise AssertionError(f'Source frame PTS changed: {name}')
    report = work / 'summary.json'
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for name, case in results['cases'].items():
        source, output = case['source'], case['output']
        print(json.dumps(dict(case=name, seconds=case['seconds'],
            source_marker=source['marker_time'], output_marker=output['marker_time'],
            source_video_start=source['video_start'], output_video_start=output['video_start'],
            source_audio_start=source['audio_first_pts'], output_audio_start=output['audio_first_pts'],
            frames=output['frames'], video_duration=output['video_duration'],
            marker_audio_relative_shift_seconds=case['marker_audio_relative_shift_seconds'],
            max_normalized_video_pts_error_seconds=case['max_normalized_video_pts_error_seconds'],
            audio_payload_equal=case['audio_payload_equal'],
            audio_pts_equal=source['audio_pts_sha256']==output['audio_pts_sha256']), ensure_ascii=False))
    print(f'Report: {report}')


if __name__ == '__main__':
    main()
