"""Bounded GPU decode acceptance; CPU decoding is used only as a test reference."""
import argparse
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--label', default='gpu-decode')
    parser.add_argument('--case', choices=('all', 'high10'), default='all')
    args = parser.parse_args()
    if not args.label.replace('-', '').isalnum():
        parser.error('Plain label required')
    task = args.task.absolute()
    if task.resolve() != task or not task.is_relative_to(ROOT / 'tmp') or not (task / 'TASK.md').is_file():
        parser.error('Registered task inside repository tmp required')
    if any(path.is_symlink() or path.is_junction() for path in (task, *task.parents)):
        parser.error('Reparse point in task path')
    report_path = task / (args.label + '.json')
    if report_path.exists():
        parser.error('Refusing to overwrite an existing report')
    import av
    import cv2
    import numpy as np
    from dlss5tool.gpu_decode import GPUDecodeError, GPUVideoCapture
    from dlss5tool.frame_generation import inspect_source
    from dlss5tool.video_export import find_ffmpeg, probe_video_stream, FFmpegHDRVideoReader, _zscale_decode_filter
    ffmpeg = find_ffmpeg()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    def run(command):
        return subprocess.run(command, check=True, capture_output=True, timeout=60, creationflags=flags)
    prefix = [ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-n']
    report = {'pyav': av.__version__, 'ffmpeg': ffmpeg, 'cases': [],
              'scope': 'short synthetic clips; CPU reference is test-only; no long-video stability claim'}
    try:
        for name, encoder, pix_fmt, extra in (
            ('h264', 'libx264', 'yuv420p', []),
            ('hevc', 'libx265', 'yuv420p', ['-x265-params', 'pools=1:frame-threads=1']),
            ('av1', 'libaom-av1', 'yuv420p', ['-cpu-used', '8', '-row-mt', '1']),
            ('pq', 'libx265', 'yuv420p10le', ['-x265-params', 'pools=1:frame-threads=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc',
                '-color_primaries', 'bt2020', '-color_trc', 'smpte2084', '-colorspace', 'bt2020nc']),
            ('hlg', 'libx265', 'yuv420p10le', ['-x265-params', 'pools=1:frame-threads=1:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc',
                '-color_primaries', 'bt2020', '-color_trc', 'arib-std-b67', '-colorspace', 'bt2020nc']),
            ('vfr', 'libx264', 'yuv420p', ['-vf', r'select=not(eq(mod(n\,5)\,1))', '-fps_mode', 'vfr']),
            ('h264-high10', 'libx264', 'yuv420p10le', []),
        ):
            if args.case == 'high10' and name != 'h264-high10':
                continue
            source = task / (args.label + '-' + name + '.mp4')
            run(prefix + ['-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30',
                '-frames:v', '24', '-c:v', encoder, '-threads', '1', '-pix_fmt', pix_fmt, *extra, str(source)])
            started = time.perf_counter()
            if name == 'h264-high10':
                from dlss5tool.image_sequence import open_capture
                strict_supported = True
                strict_error = None
                try:
                    reader = open_capture(source)
                    reader.release()
                except GPUDecodeError as error:
                    strict_supported, strict_error = False, str(error)
                reader = open_capture(source, allow_cpu_fallback=True)
                reference = cv2.VideoCapture(str(source))
                count = 0
                try:
                    while True:
                        ok, baseline = reference.read()
                        decoded, frame = reader.read()
                        assert ok == decoded
                        if not ok:
                            break
                        assert np.array_equal(frame, baseline), (name, count)
                        count += 1
                    for index in (0, 10, 3, 23, 1):
                        reader.set(cv2.CAP_PROP_POS_FRAMES, index)
                        reference.set(cv2.CAP_PROP_POS_FRAMES, index)
                        ok, baseline = reference.read()
                        decoded, frame = reader.read()
                        assert ok and decoded and np.array_equal(frame, baseline)
                    backend = reader.backend
                finally:
                    reader.release()
                    reference.release()
                assert count == 24
                report['cases'].append({'name': name, 'gpu_supported': strict_supported,
                    'strict_error': strict_error, 'fallback_enabled_backend': backend,
                    'fallback_frames': count, 'identical_bgr': True,
                    'exact_seek_indices': [0, 10, 3, 23, 1]})
                print(json.dumps(report['cases'][-1], ensure_ascii=False), flush=True)
                continue
            metadata = probe_video_stream(ffmpeg, source)
            if name in ('pq', 'hlg'):
                raw = run(prefix + ['-threads', '1', '-i', str(source), '-an',
                    '-vf', _zscale_decode_filter(metadata), '-f', 'rawvideo', '-pix_fmt', 'rgba64le', 'pipe:1']).stdout
                expected = np.frombuffer(raw, '<u2').reshape(-1, 180, 320, 4)
                reader = FFmpegHDRVideoReader(source, 320, 180, metadata)
                count = 0
                digest = hashlib.sha256()
                try:
                    for frame in expected:
                        image = reader.read()
                        baseline = (frame.astype(np.float32) / 65535.0).astype(np.float16)
                        assert image is not None and np.array_equal(image, baseline), (name, count)
                        digest.update(image.tobytes())
                        count += 1
                    assert reader.read() is None
                finally:
                    reader.close()
                report['cases'].append({'name': name, 'frames': count, 'identical_rgba16f': True,
                    'sha256': digest.hexdigest(), 'seconds_with_reference': time.perf_counter() - started})
            else:
                reference = cv2.VideoCapture(str(source))
                expected = []
                try:
                    while True:
                        ok, frame = reference.read()
                        if not ok:
                            break
                        expected.append(frame)
                finally:
                    reference.release()
                reader = GPUVideoCapture(source)
                count = 0
                digest = hashlib.sha256()
                try:
                    for image in expected:
                        ok, frame = reader.read()
                        assert ok and np.array_equal(frame, image), (name, count)
                        digest.update(frame.tobytes())
                        count += 1
                    assert reader.read() == (False, None)
                    for index in (0, 10, 3, 23, 1):
                        assert reader.set(cv2.CAP_PROP_POS_FRAMES, index)
                        ok, frame = reader.read()
                        assert ok and np.array_equal(frame, expected[index]), (name, 'seek', index)
                finally:
                    reader.release()
                meta, rate, width, height, inspected = inspect_source(source, threading.Event(), allow_vfr=True)
                assert inspected == count == 24
                if name == 'vfr':
                    assert meta.get('_is_vfr') and len(meta['_source_pts']) == count
                report['cases'].append({'name': name, 'frames': count, 'identical_bgr': True,
                    'exact_seek_indices': [0, 10, 3, 23, 1], 'inspected_frames': inspected,
                    'vfr': bool(meta.get('_is_vfr')), 'sha256': digest.hexdigest(),
                    'seconds_with_reference': time.perf_counter() - started})
            print(json.dumps(report['cases'][-1], ensure_ascii=False), flush=True)
        report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Report: ' + str(report_path), flush=True)


if __name__ == '__main__':
    main()
