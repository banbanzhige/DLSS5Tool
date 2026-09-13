"""Read-only product-config A/B probe; warm host-only throughput, not export FPS."""
import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import queue
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = {
    'compat_fast_q3': ('compatibility', True, 3, True),
    'merged_fast_q1': ('merged', True, 1, True),
    'merged_fast_q2': ('merged', True, 2, True),
    'merged_fast_q3': ('merged', True, 3, True),
    'compat_upload_q3': ('compatibility', False, 3, True),
    'merged_fast_q2_old_effects': ('merged', True, 2, False),
}


def child(args, work):
    import cv2
    from dlss5tool import dlss_engine
    from dlss5tool.dlss_host_process import ProcessLive
    dlss_engine.LOG_PATH = str(work / (args.case + '.log'))
    cap = cv2.VideoCapture(args.video)
    frames = []
    for _ in range(16):
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA))
    cap.release()
    if not frames:
        raise RuntimeError('No input frames')
    h, w = frames[0].shape[:2]
    submission, fast, queue_depth, effects = CASES[args.case]
    settings = dict(guidance_mode=0, host_backend='v2', host_auto_fallback=False,
                    host_submission=submission, host_zero_fast_path=fast,
                    host_persistent_buffers=True, host_in_flight=queue_depth,
                    style=0, intensity=1.0, local_tone=1.0,
                    local_struct=1.0 if effects else 0.0,
                    use_auto_mask=effects, skin_struct=1.0 if effects else 0.5,
                    output_mix=1.0, output_view=0)
    factory = ProcessLive if args.proxy or args.pipeline else dlss_engine.Live
    started = time.perf_counter()
    live = factory(w, h, settings)
    setup = time.perf_counter() - started

    def run(count, hashes=False):
        pending = deque()
        digests = []
        def collect(result):
            if result is None:
                raise RuntimeError('Missing frame')
            pending.popleft()
            if hashes:
                digests.append(hashlib.sha256(memoryview(result).cast('B')).hexdigest())
        start = time.perf_counter()
        for i in range(count):
            rgba = frames[i % len(frames)]
            pending.append(i)
            if live.supports_async:
                if not live.enqueue(rgba, reset=(i == 0)):
                    raise RuntimeError('Enqueue rejected')
                if len(pending) >= live.max_in_flight:
                    collect(live.dequeue())
            else:
                collect(live.process(rgba, reset=(i == 0)))
        while pending:
            collect(live.dequeue())
        return time.perf_counter() - start, digests

    run(24)
    if args.pipeline:
        from dlss5tool.video_export import find_ffmpeg, build_video_encoder_args
        capture = cv2.VideoCapture(args.video)
        fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
        decode_queue = queue.Queue(maxsize=4)
        stop = threading.Event()
        def decode():
            try:
                for i in range(args.frames):
                    ok, bgr = capture.read()
                    if not ok:
                        break
                    while not stop.is_set():
                        try:
                            decode_queue.put((i, bgr), timeout=0.1)
                            break
                        except queue.Full:
                            pass
                    if stop.is_set():
                        return
            finally:
                capture.release()
            while not stop.is_set():
                try:
                    decode_queue.put(None, timeout=0.1)
                    return
                except queue.Full:
                    pass
        command = [find_ffmpeg(), '-hide_banner', '-loglevel', 'error',
                   '-f', 'rawvideo', '-pixel_format', 'bgr24', '-video_size', f'{w}x{h}',
                   '-framerate', str(fps), '-i', 'pipe:0', '-an',
                   '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p',
                   *build_video_encoder_args(False, True, 'p5', 'quality', 'high'),
                   '-f', 'null', '-']
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, bufsize=0,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def write(bgr):
            view = memoryview(bgr).cast('B')
            while view:
                count = os.write(proc.stdin.fileno(), view)
                if count <= 0:
                    raise RuntimeError('Encoder pipe closed')
                view = view[count:]
        started = time.perf_counter()
        producer = threading.Thread(target=decode, daemon=True)
        producer.start()
        submitted = done = 0
        previous_write = None
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                def consume(output):
                    nonlocal done, previous_write
                    if output is None:
                        raise RuntimeError('Missing frame')
                    bgr = cv2.cvtColor(output, cv2.COLOR_RGBA2BGR)
                    if previous_write is not None:
                        previous_write.result()
                    previous_write = pool.submit(write, bgr)
                    done += 1
                while True:
                    item = decode_queue.get(timeout=30)
                    if item is None:
                        break
                    i, bgr = item
                    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
                    submitted += 1
                    if live.supports_async:
                        if not live.enqueue(rgba, reset=(i == 0)):
                            raise RuntimeError('Enqueue rejected')
                        if submitted - done >= live.max_in_flight:
                            consume(live.dequeue())
                    else:
                        consume(live.process(rgba, reset=(i == 0)))
                while submitted > done:
                    consume(live.dequeue())
                if previous_write is not None:
                    previous_write.result()
            proc.stdin.close()
            error = proc.stderr.read().decode('utf-8', errors='replace')
            if proc.wait(timeout=30):
                raise RuntimeError(error)
            seconds = time.perf_counter() - started
            args.frames = done
        finally:
            stop.set()
            producer.join(timeout=2)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    else:
        seconds, _ = run(args.frames)
    _, digests = run(16, hashes=True)
    result = dict(case=args.case, proxy=args.proxy or args.pipeline, pipeline=args.pipeline,
                  size=[w, h], settings=settings,
                  setup_seconds=setup, frames=args.frames, seconds=seconds,
                  fps=args.frames/seconds, actual_queue=live.max_in_flight,
                  adapter=getattr(live, 'adapter_info', {}), hashes=digests)
    print(json.dumps(result), flush=True)
    if args.proxy or args.pipeline:
        live.close()
    # Native NGX shutdown can hang on some drivers; disposable process exit owns cleanup.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', required=True)
    parser.add_argument('--work-dir', required=True)
    parser.add_argument('--frames', type=int, default=160)
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--proxy', action='store_true')
    parser.add_argument('--pipeline', action='store_true', help='Real decode + proxy + NVENC to null; no video output')
    args = parser.parse_args()
    work = Path(args.work_dir).resolve()
    if not (work / 'TASK.md').is_file():
        raise RuntimeError('Create a hygiene TASK.md and check space before running')
    os.environ['TMP'] = os.environ['TEMP'] = str(work)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    if args.case:
        child(args, work)
        return
    results = []
    for round_no in range(2):
        cases = list(CASES)
        if args.pipeline:
            cases = ['compat_fast_q3', 'merged_fast_q2', 'compat_upload_q3']
        if round_no:
            cases.reverse()
        for case in cases:
            command = [sys.executable, '-B', str(Path(__file__).resolve()),
                       '--video', args.video, '--work-dir', str(work),
                       '--case', case, '--frames', str(args.frames)]
            if args.proxy:
                command.append('--proxy')
            if args.pipeline:
                command.append('--pipeline')
            completed = subprocess.run(command, capture_output=True, text=True, timeout=90)
            if completed.returncode:
                raise RuntimeError(completed.stdout + completed.stderr)
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            result['round'] = round_no
            results.append(result)
            print(json.dumps({k: result[k] for k in ('round', 'case', 'fps', 'actual_queue')}), flush=True)
            report = work / ('pipeline-report.json' if args.pipeline else
                             'proxy-report.json' if args.proxy else 'native-report.json')
            report.write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
