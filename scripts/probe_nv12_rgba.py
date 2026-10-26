"""Compare an independent integer NV12 conversion with production PyAV planar BGR.

An exploratory model is not acceptance. Reports retain every mismatch. CPU
references use the same two reformats as GPUVideoCapture, including tags.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import ExitStack
from fractions import Fraction

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def coefficients(matrix, full):
    # BT.601 / BT.709 definitions; calculate rather than embed upstream code.
    kr, kb = {1: (.2126, .0722), 2: (.299, .114), 5: (.299, .114),
              6: (.299, .114)}[matrix]
    kg = 1-kr-kb
    factors = (2*(1-kr), 2*(1-kb), -2*kb*(1-kb)/kg, -2*kr*(1-kr)/kg)
    scaled = [round(value*255/224*65536) for value in factors]
    if full:
        scaled = [int(value*224/255) for value in scaled]
    cy = 65536 if full else 65536*255//219
    return [(cy+4)//8, *[(value+4)//8 for value in scaled], 0 if full else 16*8]


def model(y, u, v, matrix, full):
    import numpy as np
    cy, rv, bu, gu, gv, offset = coefficients(matrix, full)
    lum = ((y.astype(np.int32)*8-offset)*cy) >> 16
    uu = u.astype(np.int32).repeat(2, axis=0).repeat(2, axis=1)*8-1024
    vv = v.astype(np.int32).repeat(2, axis=0).repeat(2, axis=1)*8-1024
    result = np.stack((lum+((vv*rv)>>16),
                       lum+((uu*gu)>>16)+((vv*gv)>>16),
                       lum+((uu*bu)>>16)), axis=-1)
    return result.clip(0, 255).astype(np.uint8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--ptx', type=Path)
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    from scripts.build_vsr_cuda import safe
    task = safe(args.task, ROOT/'tmp')
    path = task/f'{args.label}.json'
    if not (task/'TASK.md').is_file() or not args.label.replace('-', '').isalnum() or path.exists():
        raise ValueError('Registered work and fresh safe label required')
    if args.ptx and not args.worker:
        from dlss5tool.process_job import KillOnCloseJob
        from scripts.probe_resident_decode import process_record
        environment = dict(os.environ, TEMP=str(task), TMP=str(task), PYTHONDONTWRITEBYTECODE='1',
            PYTHONPATH=str(Path(sys.prefix)/'Lib/site-packages'))
        process = subprocess.Popen([sys._base_executable, '-B', str(Path(__file__).resolve()),
            *sys.argv[1:], '--worker'], cwd=ROOT, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        job = KillOnCloseJob(process)
        try:
            stdout, stderr = process.communicate('GO\n', timeout=40)
            result = dict(returncode=process.returncode, stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired:
            result = dict(returncode='timeout')
        finally:
            job.close()
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
        result.update(pid=process.pid, process_after_retirement=process_record(process.pid))
        (task/f'{args.label}-process.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result))
        return 0 if result['returncode'] == 0 and not result['process_after_retirement']['present'] else 1
    if args.worker and sys.stdin.readline().strip() != 'GO':
        raise RuntimeError('Parent Job assignment required')
    import av
    import numpy as np
    av.logging.restore_default_callback()
    report = dict(pyav=av.__version__, libraries=av.library_versions, cases=[])
    for width, height in ((64, 48), (320, 180), (960, 540), (1920, 1080)):
        rng = np.random.default_rng(20261026)
        fixtures = dict(random=[rng.integers(0, 256, shape, np.uint8) for shape in
            ((height, width), (height//2, width//2), (height//2, width//2))])
        with ExitStack() as stack:
            if args.ptx:
                from dlss5tool.cuda_session import CudaSession
                from dlss5tool.cuda_nv12 import CudaNv12Converter, conversion_coefficients
                from dlss5tool.cuda_frame import FrameTiming
                cuda = stack.enter_context(CudaSession())
                converter = stack.enter_context(CudaNv12Converter(cuda, args.ptx, width, height))
                pitch = width+32
                buffer = cuda.allocate(pitch*height*3//2)
            for matrix in (2, 1, 5, 6):
              for color_range in (0, 1, 2):
                for fixture, (y, u, v) in fixtures.items():
                    packed = np.concatenate([plane.reshape(-1) for plane in (y, u, v)]).reshape(height*3//2, width)
                    frame = av.VideoFrame.from_ndarray(packed, format='yuv420p')
                    frame.colorspace, frame.color_range = matrix, color_range
                    expected = frame.to_ndarray(format='bgr24')[..., ::-1]
                    actual = model(y, u, v, matrix, color_range == 2)
                    if args.ptx:
                        nv12 = np.full((height*3//2, pitch), 237, np.uint8)
                        nv12[:height, :width] = y
                        nv12[height:, :width:2] = u
                        nv12[height:, 1:width:2] = v
                        cuda.upload(buffer, nv12); cuda.synchronize()
                        with cuda.current(), converter._produce([buffer, buffer+pitch*height], [pitch, pitch],
                                conversion_coefficients(matrix, color_range), FrameTiming(0, 0, Fraction(1,30))) as lease:
                            rgba = converter._output.download(lease)
                            assert np.all(rgba[..., 3] == 255)
                            actual = rgba[..., :3]
                    delta = actual.astype(np.int16)-expected.astype(np.int16)
                    indices = np.argwhere(delta)
                    row = dict(size=[width, height], matrix=matrix, range=color_range,
                        fixture=fixture, coefficients=coefficients(matrix, color_range == 2),
                        equal=not len(indices), mismatches=len(indices), max_abs=int(np.abs(delta).max()),
                        actual_sha256=hashlib.sha256(actual.tobytes()).hexdigest(),
                        reference_sha256=hashlib.sha256(expected.tobytes()).hexdigest(),
                        first=[dict(y=int(i), x=int(j), c=int(k), actual=int(actual[i,j,k]),
                                    expected=int(expected[i,j,k])) for i,j,k in indices[:5]])
                    report['cases'].append(row)
        print(json.dumps(dict(size=[width, height], rows=[(r['matrix'], r['range'], r['mismatches'], r['max_abs'])
                         for r in report['cases'] if r['size']==[width,height]])), flush=True)
    report['accepted'] = all(row['equal'] for row in report['cases'])
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
