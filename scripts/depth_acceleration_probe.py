"""Frozen guidance + ProcessLive + original-video decode + NVENC integration A/B.

Does not change app settings. Each profile writes a new silent test video; no
audio mux or GUI timings. DLSS hashes are taken before lossy encoding.
"""
import argparse
import hashlib
import json
import multiprocessing
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
from dlss_host_process import ProcessLive
import imageio_ffmpeg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=165)
    parser.add_argument('--execution-ab', action='store_true', help='Compare original serial/dual stream at fixed SDPA FP16')
    parser.add_argument('--execution-order', nargs='+', choices=['serial', 'raft_streams'],
                        default=['serial', 'raft_streams'])
    args = parser.parse_args()
    if args.component.name != 'enhancement' or not (args.component / 'guidance_worker.exe').is_file():
        parser.error('Need an enhancement component directory')
    args.output.mkdir(parents=True, exist_ok=False)
    report, reference_hashes = [], None
    cases = [(p, 'sdpa_fp16', p) for p in args.execution_order] if args.execution_ab else [
        (p, p, 'serial') for p in ('fp32', 'sdpa_fp16')]
    for label, profile, execution in cases:
        cap = cv2.VideoCapture(str(args.source))
        w, h, fps = int(cap.get(3)), int(cap.get(4)), cap.get(5)
        ok, first = cap.read()
        if not ok:
            raise ValueError('Cannot decode source')
        first = cv2.cvtColor(first, cv2.COLOR_BGR2RGBA)
        settings = {'guidance_mode': 3, 'guidance_device': 'cuda', 'guidance_edge': 720,
            'guidance_depth_encoder': 'vitl', 'guidance_depth_profile': profile,
            'guidance_execution': execution,
            'guidance_transport': 'shared_memory_v1', 'mods_directory': str(args.component.resolve().parent),
            'guidance_flow_weights': str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
            'guidance_depth_weights': str(ROOT / 'mods/models/depth_anything_v2_vitl.pth'),
            'host_backend': 'v2', 'host_auto_fallback': False, 'host_in_flight': 1,
            'style': 0, 'intensity': 1.0}
        live = writer = None
        tick = time.perf_counter()
        try:
            live = ProcessLive(w, h, settings)
            live.process(first, True)
            live.process(first, False)
            warmup = time.perf_counter() - tick
            assert live.guidance_info['depth_profile'] == profile
            if args.execution_ab:
                assert live.guidance_info['execution'] == execution
            video = args.output / f'{label}.mp4'
            writer = subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{w}x{h}', '-r', str(fps), '-i', '-', '-an',
                '-c:v', 'h264_nvenc', '-preset', 'p5', '-pix_fmt', 'yuv420p', str(video)],
                stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            timings, hashes, metrics = [], [], []
            started = time.perf_counter()
            for index in range(args.frames):
                ok, bgr = cap.read()
                if not ok:
                    break
                frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
                tick = time.perf_counter()
                output = live.process(frame, index == 0)
                timings.append((time.perf_counter() - tick) * 1000)
                if output is None:
                    raise RuntimeError('No DLSS output')
                hashes.append(hashlib.sha256(memoryview(output)).hexdigest())
                metrics.append(live.guidance_metrics)
                writer.stdin.write(memoryview(output))
                if (index + 1) % 30 == 0:
                    print(label, index + 1, flush=True)
            writer.stdin.close()
            writer.stdin = None
            _, error = writer.communicate(timeout=30)
            if writer.returncode:
                raise RuntimeError(error.decode(errors='replace'))
            elapsed = time.perf_counter() - started
            verify = cv2.VideoCapture(str(video))
            decoded = 0
            try:
                while verify.read()[0]:
                    decoded += 1
            finally:
                verify.release()
            assert decoded == len(hashes) == args.frames
            reference_hashes = reference_hashes or hashes
            record = {'profile': profile, 'dimensions': [w, h], 'frames': len(hashes),
                'execution': execution, 'timing_kind': metrics[-1].get('timing_kind'),
                'baseline_execution': cases[0][2],
                'warmup_seconds': warmup, 'decode_process_hash_encode_seconds': elapsed,
                'fps_including_decode_hash_encode': len(hashes) / elapsed,
                'mean_process_ms': statistics.mean(timings),
                'mean_flow_ms': statistics.mean(m['flow_ms'] for m in metrics),
                'mean_depth_ms': statistics.mean(m['depth_ms'] for m in metrics),
                'peak_allocated_mib': max(m['peak_allocated_mib'] for m in metrics),
                'device': live.guidance_info, 'output_matches_baseline': hashes == reference_hashes,
                'decoded_output_frames': decoded, 'sha256': hashes}
            if args.execution_ab and execution != 'serial':
                old_session = live._session
                live.update({'guidance_execution': 'serial'})
                assert live._session is not old_session and old_session._closed
                assert live.process(first, True) is not None
                assert live.guidance_info['execution'] == 'serial'
                record['execution_rollback_verified'] = True
            elif profile == 'sdpa_fp16' and not args.execution_ab:
                # Same ProcessLive object: reconfigure back to original precision.
                old_session = live._session
                live.update({'guidance_depth_profile': 'fp32'})
                assert live._session is not old_session and old_session._closed
                assert live.process(first, True) is not None
                assert live.guidance_info['depth_profile'] == 'fp32'
                record['rollback_verified'] = True
            report.append(record)
            (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps({k: v for k, v in record.items() if k != 'sha256'}), flush=True)
        finally:
            cap.release()
            if writer and writer.poll() is None:
                writer.kill()
                writer.communicate()
            if live:
                live.close()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
