"""Bounded isolated resident HDR decode comparison against the production reader."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from scripts.build_vsr_cuda import safe, digest
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--ptx', type=Path)
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    source = safe(args.source, task)
    path = task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or path.exists() or not args.label.replace('-', '').isalnum():
        raise ValueError('Fresh registered evidence required')
    if not args.worker:
        from dlss5tool.process_job import KillOnCloseJob
        from scripts.probe_resident_decode import process_record
        environment = dict(os.environ, PYTHONPATH=str(Path(sys.prefix)/'Lib/site-packages'),
                           PYTHONDONTWRITEBYTECODE='1', TEMP=str(task), TMP=str(task))
        process = subprocess.Popen([sys._base_executable, '-B', str(Path(__file__).resolve()),
            *sys.argv[1:], '--worker'], cwd=ROOT, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        job = KillOnCloseJob(process)
        try:
            stdout, stderr = process.communicate('GO\n', timeout=90)
            record = dict(returncode=process.returncode, stdout=stdout[-3000:], stderr=stderr[-4000:])
        except subprocess.TimeoutExpired:
            record = dict(returncode='timeout')
        finally:
            job.close()
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
            record['after_retirement'] = process_record(process.pid)
            (task/(args.label+'-process.json')).write_text(json.dumps(record, indent=2), encoding='utf-8')
        print(json.dumps(record))
        return 0 if record['returncode'] == 0 else 1
    if sys.stdin.readline().strip() != 'GO':
        raise RuntimeError('Native work requires a parent Job')
    import av
    from av.codec.hwaccel import HWAccel
    import numpy as np
    from fractions import Fraction
    from dlss5tool.cuda_session import CudaSession
    from dlss5tool.cuda_frame import FrameTiming
    from dlss5tool.cuda_p010 import CudaP010Converter
    from dlss5tool.video_export import FFmpegHDRVideoReader, probe_video_stream, find_ffmpeg
    av.logging.restore_default_callback()
    metadata = probe_video_stream(find_ffmpeg(), source)
    width, height = int(metadata['width']), int(metadata['height'])
    if width*height > 1920*1080:
        raise ValueError('Bounded input <=1080p required')
    report = dict(source_sha256=digest(source), accepted=False, cases=[])
    started = time.perf_counter()
    reference = FFmpegHDRVideoReader(source, width, height, metadata)
    try:
      with CudaSession() as cuda:
        with cuda.current():
            container = av.open(str(source), hwaccel=HWAccel('cuda', device=0,
                allow_software_fallback=False, is_hw_owned=True,
                options={'primary_ctx':'0', 'current_ctx':'1'}))
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 1
            iterator = container.decode(stream)
        try:
          with CudaP010Converter(cuda, safe(args.ptx, task) if args.ptx else task/'p010-rgba.ptx', width, height,
                                 color_profile=metadata['profile']) as converter:
            for index in range(16):
                with cuda.current(): frame = next(iterator, None)
                if frame is None:
                    break
                expected = reference.read()
                timing = FrameTiming(index, frame.pts, Fraction(frame.time_base), index == 0)
                with converter.convert(frame, timing) as lease:
                    actual = converter._output.download(lease)
                differences = actual.view(np.uint16) != expected.view(np.uint16)
                mismatch = np.argwhere(differences)
                report['cases'].append(dict(index=index, equal=not differences.any(),
                    mismatches=int(differences.sum()), input_format=frame.sw_format.name,
                    sha256=__import__('hashlib').sha256(memoryview(actual)).hexdigest(),
                    first=[dict(position=where.tolist(), actual=int(actual.view(np.uint16)[tuple(where)]),
                                expected=int(expected.view(np.uint16)[tuple(where)])) for where in mismatch[:4]]))
                frame = None
            report['accepted'] = bool(report['cases']) and all(row['equal'] for row in report['cases'])
        finally:
            with cuda.current():
                frame = None
                iterator.close(); container.close()
                iterator = container = stream = None
    finally:
        reference.close()
        report['elapsed_seconds'] = time.perf_counter()-started
        path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(dict(accepted=report['accepted'], mismatches=[row['mismatches'] for row in report['cases']])))
    return 0 if report['accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
