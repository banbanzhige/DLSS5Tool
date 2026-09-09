"""Bounded local CPU/GPU benchmark. Synthetic moving clip, never user settings.

--native measures frozen guidance + isolated DLSS + NVENC output. Without it,
measure guidance IPC/inference only. First frame is a separate warmup; identical
weights, dimensions, edge and RAFT iteration count must be used for comparisons.
"""
import argparse
import json
import multiprocessing
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
import guidance_client
from dlss_host_process import ProcessLive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', type=Path, required=True)
    parser.add_argument('--device', choices=['cpu', 'cuda', 'auto'], default='auto')
    parser.add_argument('--edge', type=int, default=720)
    parser.add_argument('--frames', type=int, default=6)
    parser.add_argument('--modes', nargs='+', type=int, choices=[0, 1, 2, 3], default=[1, 2, 3])
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.frames <= 600:
        parser.error('frames must be 1..600')
    args.output.mkdir(parents=True, exist_ok=False)
    width, height, fps = 1280, 720, 30
    bgr = cv2.resize(cv2.imread(str(ROOT / 'img/01.png')), (width, height))
    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
    report = []
    for mode in args.modes:
        if not mode and not args.native:
            continue
        settings = {'mods_directory': str(args.component.resolve().parent),
            'guidance_mode': mode, 'guidance_device': args.device, 'guidance_edge': args.edge,
            'guidance_depth_encoder': 'vitl', 'guidance_flow_direction': 'backward',
            'guidance_flow_weights': str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
            'guidance_depth_weights': str(ROOT / 'mods/models/depth_anything_v2_vitl.pth'),
            'host_backend': 'v2', 'host_auto_fallback': False, 'host_in_flight': 1,
            'style': 0, 'intensity': 1.0, 'ui_language': 'en_US'}
        samples, timings, metrics = [], [], []
        stop = threading.Event()
        def monitor():
            while not stop.is_set():
                try:
                    result = subprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                        capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                    if result.returncode == 0:
                        samples.append(int(result.stdout.splitlines()[0]))
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                stop.wait(1)
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        live = writer = None
        started = time.perf_counter()
        try:
            live = ProcessLive(width, height, settings) if args.native else guidance_client.GuidanceSession(settings, width, height)
            live.process(rgba, reset=True)
            warmup = time.perf_counter() - started
            if args.native:
                import imageio_ffmpeg
                writer = subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                    '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{width}x{height}', '-r', str(fps),
                    '-i', '-', '-an', '-c:v', 'h264_nvenc', '-preset', 'p4', '-pix_fmt', 'yuv420p',
                    str(args.output / f'mode-{mode}.mp4')], stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW)
            timed_start = time.perf_counter()
            for index in range(args.frames):
                matrix = np.float32([[1, 0, 12*np.sin((index+1)/15)], [0, 1, 8*np.sin((index+1)/23)]])
                frame = cv2.warpAffine(rgba, matrix, (width, height), borderMode=cv2.BORDER_REFLECT_101)
                tick = time.perf_counter()
                result = live.process(frame, reset=False)
                timings.append(time.perf_counter() - tick)
                if args.native:
                    assert result is not None and result.shape == frame.shape
                    writer.stdin.write(result.tobytes())
                    metrics.append(live.guidance_metrics)
                else:
                    mv, depth, reset = result
                    assert np.isfinite(mv).all() and np.isfinite(depth).all()
                    metrics.append(live.last_metrics)
                if (index+1) % 30 == 0:
                    print(f'mode={mode} {index+1}/{args.frames}', flush=True)
            if writer:
                writer.stdin.close()
                writer.stdin = None
                _, error = writer.communicate(timeout=60)
                if writer.returncode:
                    raise RuntimeError(error.decode(errors='replace'))
            elapsed = time.perf_counter() - timed_start
            info = live.guidance_info if args.native else live.info
            if mode:
                assert info['device'] == ('cpu' if args.device == 'cpu' else 'cuda'), info
            record = {'mode': mode, 'native_dlss_nvenc': args.native, 'device': info,
                'input': [width, height, fps], 'edge': args.edge, 'frames': args.frames,
                'warmup_seconds': warmup, 'timed_seconds': elapsed,
                'fps': args.frames / elapsed, 'mean_frame_seconds': statistics.mean(timings),
                'median_frame_seconds': statistics.median(timings),
                'guidance_peak_allocated_mib': max((m.get('peak_allocated_mib', 0) for m in metrics), default=0),
                'guidance_peak_reserved_mib': max((m.get('peak_reserved_mib', 0) for m in metrics), default=0),
                'whole_gpu_used_mib_samples': samples.copy(),
                'mean_flow_ms': statistics.mean(m.get('flow_ms', 0) for m in metrics),
                'mean_depth_ms': statistics.mean(m.get('depth_ms', 0) for m in metrics)}
            report.append(record)
            print(json.dumps(record), flush=True)
            (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        finally:
            if writer and writer.poll() is None:
                writer.kill()
                writer.communicate()
            if live:
                live.close()
            stop.set()
            thread.join(timeout=6)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
