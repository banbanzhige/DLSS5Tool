"""Bounded, Job-contained PyAV resident NVDEC lifecycle experiment.

No process names are killed. Only the gated child owned by this invocation is
retired; reports distinguish normal native teardown from forced retirement.
"""
import argparse
import ctypes as C
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def process_record(pid):
    """Toolhelp snapshot also catches exited-but-retained OS process records."""
    from ctypes import wintypes as W
    class Entry(C.Structure):
        _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                    ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                    ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                    ('name', W.WCHAR*260)]
    kernel = C.WinDLL('kernel32', use_last_error=True)
    from dlss5tool.cuda_session import bind
    snapshot = bind(kernel, 'CreateToolhelp32Snapshot', [W.DWORD, W.DWORD], W.HANDLE)(2, 0)
    if snapshot == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        first = bind(kernel, 'Process32FirstW', [W.HANDLE, C.POINTER(Entry)])
        next_entry = bind(kernel, 'Process32NextW', [W.HANDLE, C.POINTER(Entry)])
        entry = Entry(); entry.size = C.sizeof(entry)
        valid = first(snapshot, C.byref(entry))
        while valid:
            if entry.pid == pid:
                return dict(present=True, pid=pid, threads=entry.threads,
                            parent=entry.parent, name=entry.name)
            valid = next_entry(snapshot, C.byref(entry))
        return dict(present=False, pid=pid)
    finally:
        bind(kernel, 'CloseHandle', [W.HANDLE])(snapshot)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--owner', choices=('decoder-first', 'borrow-current'), default='decoder-first')
    parser.add_argument('--python-logging', action='store_true')
    parser.add_argument('--device', type=int, default=0)
    parser.add_argument('--ptx', type=Path, help='Also compare resident GPU RGBA with production planar BGR')
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    from scripts.build_vsr_cuda import safe, digest
    task = safe(args.task, ROOT/'tmp')
    if not (task/'TASK.md').is_file() or not args.label.replace('-', '').isalnum():
        raise ValueError('Registered task and safe fresh label required')
    path = task/f'{args.label}.json'
    events = task/f'{args.label}-events.jsonl'
    source = task/'decode-320x180.mp4'
    if not args.worker:
        if path.exists() or events.exists():
            raise ValueError('Refusing evidence overwrite')
        if not source.exists():
            from dlss5tool.video_export import find_ffmpeg
            subprocess.run([find_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-n',
                '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30', '-frames:v', '12',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2',
                '-pix_fmt', 'yuv420p', str(source)], check=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW)
        from dlss5tool.process_job import KillOnCloseJob
        environment = dict(os.environ, TEMP=str(task), TMP=str(task),
            PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(Path(sys.prefix)/'Lib/site-packages'))
        process = job = None
        started = time.perf_counter()
        report = dict(owner=args.owner, python_logging=args.python_logging,
                      source_sha256=digest(source), parent_pid=os.getpid())
        try:
            process = subprocess.Popen([sys._base_executable, '-B', str(Path(__file__).resolve()),
                *sys.argv[1:], '--worker'], cwd=ROOT, env=environment, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW)
            job = KillOnCloseJob(process)
            stdout, stderr = process.communicate('GO\n', timeout=25)
            report.update(returncode=process.returncode, stdout=stdout[-3000:], stderr=stderr[-5000:])
        except subprocess.TimeoutExpired as error:
            report.update(returncode='timeout', stdout=str(error.stdout)[-3000:],
                          stderr=str(error.stderr)[-5000:])
        finally:
            if job:
                job.close()
            if process and process.poll() is None:
                process.kill(); process.wait(timeout=5)
            report.update(elapsed_seconds=time.perf_counter()-started,
                worker_pid=process.pid, process_after_retirement=process_record(process.pid))
            path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
        return 0 if report['returncode'] == 0 and not report['process_after_retirement']['present'] else 1
    if sys.stdin.readline().strip() != 'GO':
        raise RuntimeError('Native work requires parent Job assignment')
    def mark(stage, **data):
        with events.open('a', encoding='utf-8') as out:
            out.write(json.dumps(dict(stage=stage, pid=os.getpid(), **data))+'\n')
        print(stage, flush=True)
    import faulthandler
    faulthandler.dump_traceback_later(15)
    import av
    if not args.python_logging:
        av.logging.restore_default_callback()
    from av.codec.hwaccel import HWAccel
    from dlss5tool.cuda_session import CudaSession, bind, check
    from contextlib import ExitStack
    mark('av_loaded', version=av.__version__, libraries=av.library_versions)
    container = iterator = stream = frame = plane = capsule = None
    cuda = None
    converter = None
    try:
        with ExitStack() as stack:
            if args.owner == 'borrow-current':
                cuda = CudaSession(args.device)
                stack.enter_context(cuda.current())
            options = {'primary_ctx': '0', 'current_ctx': '1'} if cuda else None
            container = av.open(str(source), hwaccel=HWAccel('cuda', device=args.device,
                options=options, is_hw_owned=True, allow_software_fallback=False))
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 1
            iterator = container.decode(stream)
            mark('open')
            for index, frame in enumerate(iterator):
                assert stream.codec_context.is_hwaccel and frame.format.name == 'cuda'
                if cuda is None:
                    cuda = CudaSession(args.device)
                    stack.enter_context(cuda.current())
                if args.ptx and converter is None:
                    from dlss5tool.cuda_nv12 import CudaNv12Converter
                    converter = stack.enter_context(CudaNv12Converter(cuda, args.ptx, frame.width, frame.height))
                cuda.synchronize()
                for plane in frame.planes:
                    assert plane.__dlpack_device__() == (2, args.device)
                    capsule = plane.__dlpack__()
                    capsule = plane = None
                if converter:
                    from dlss5tool.cuda_frame import FrameTiming
                    import numpy as np
                    import hashlib
                    expected = frame.reformat(format='yuv420p').to_ndarray(format='bgr24')[..., ::-1]
                    with converter.convert(frame, FrameTiming(index, frame.pts, frame.time_base)) as result:
                        actual = converter._output.download(result)
                    assert np.array_equal(actual[..., :3], expected) and np.all(actual[..., 3] == 255)
                    mark('rgba_equal', index=index, sha256=hashlib.sha256(actual.tobytes()).hexdigest())
                mark('frame', index=index, pts=frame.pts, time_base=str(frame.time_base),
                     software_format=frame.sw_format.name, color_range=frame.color_range,
                     colorspace=frame.colorspace)
                frame = None
                if index == 7:
                    break
            mark('decode_complete')
            iterator.close(); iterator = None
            frame = plane = capsule = None
            mark('iterator_closed')
            container.close(); container = None
            mark('container_closed')
            stream = None
            gc.collect()
            mark('all_av_refs_released')
        mark('context_popped')
        cuda.close(); cuda = None
        mark('cuda_closed')
        gc.collect()
        mark('complete')
    finally:
        faulthandler.cancel_dump_traceback_later()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
