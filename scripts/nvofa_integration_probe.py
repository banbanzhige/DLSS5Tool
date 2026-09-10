"""Explicit frozen-component flow-only acceptance probe. Never patches Models.

New output directory; synthetic activation always runs. With --source, export
all frames through the production ProcessLive/GuidanceSession path, with the
same parameters for both backends. No fallback is permitted in an A/B probe.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--mods', type=Path, required=True)
    result.add_argument('--output', type=Path, required=True)
    result.add_argument('--source', type=Path)
    result.add_argument('--flow-weights', type=Path)
    result.add_argument('--backends', nargs='+', choices=('raft', 'nvofa'), default=['nvofa'])
    result.add_argument('--edge', type=int, default=512)
    result.add_argument('--grid', type=int, choices=(1, 2, 4), default=4)
    result.add_argument('--direction', choices=('backward', 'forward_negated'), default='backward')
    result.add_argument('--cache-mb', type=int, default=0)
    return result


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    import cv2
    import numpy as np
    from dlss5tool import app_settings, guidance_client
    from dlss5tool.dlss_host_process import ProcessLive
    args = parser().parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    worker = args.mods.resolve() / 'enhancement/guidance_worker.exe'
    settings = {**app_settings.DEFAULTS, 'guidance_mode': 1,
                'guidance_device': 'cuda', 'guidance_flow_fallback': False,
                'guidance_flow_edge': args.edge, 'guidance_flow_direction': args.direction,
                'guidance_flow_grid': args.grid,
                'guidance_execution': 'serial', 'guidance_cache_mb': args.cache_mb,
                'host_backend': 'v2', 'host_auto_fallback': False,
                'mods_directory': str(args.mods.resolve())}
    if args.flow_weights:
        settings['guidance_flow_weights'] = str(args.flow_weights.resolve())
    report = {'component': str(worker), 'worker_sha256': sha(worker), 'settings': settings,
              'source': str(args.source.resolve()) if args.source else None,
              'source_sha256': sha(args.source) if args.source else None,
              'runs': [], 'quality_review': 'pending human continuous playback'}
    for backend in args.backends:
        selected = {**settings, 'guidance_flow_backend': backend}
        started = time.perf_counter()
        ready = guidance_client.preflight(selected)
        if ready.get('flow_backend') != backend:
            raise RuntimeError('Probe backend mismatch')
        record = {'backend': backend, 'ready': ready, 'activation_s': time.perf_counter() - started}
        report['runs'].append(record)
        if args.source:
            cap = cv2.VideoCapture(str(args.source))
            if not cap.isOpened():
                raise RuntimeError('Cannot decode source')
            w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps, expected = cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if expected > 600:
                raise ValueError('Bounded acceptance probe: at most 600 frames')
            from dlss5tool.video_export import FFmpegVideoWriter
            target = args.output / (backend + '.mp4')
            writer = live = None
            count, durations, hashes, metrics = 0, [], [], []
            try:
                live = ProcessLive(w, h, selected)
                writer = FFmpegVideoWriter(str(target), w, h, fps, audio_source=None)
                wall = time.perf_counter()
                while True:
                    ok, bgr = cap.read()
                    if not ok:
                        break
                    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
                    tick = time.perf_counter()
                    output = live.process(rgba, reset=count == 0)
                    if output is None:
                        raise RuntimeError('Missing synchronous output')
                    durations.append((time.perf_counter() - tick) * 1000)
                    if live.guidance_info.get('flow_backend') != backend:
                        raise RuntimeError('Active processing backend mismatch')
                    metrics.append(dict(live.guidance_metrics))
                    hashes.append(hashlib.sha256(output.tobytes()).hexdigest())
                    writer.write(cv2.cvtColor(output, cv2.COLOR_RGBA2BGR))
                    count += 1
                if count != expected:
                    raise RuntimeError(f'Incomplete decode: {count}/{expected}')
                writer.finish()
                record.update(frames=count, wall_s=time.perf_counter() - wall,
                              process_p50_ms=float(np.median(durations)),
                              process_p95_ms=float(np.percentile(durations, 95)), frame_sha256=hashes,
                              width=w, height=h, source_fps=fps,
                              process_ms=durations, guidance_metrics=metrics,
                              output=str(target))
                # Explicit seek/reset after a complete sequence must give zero motion.
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, count // 2))
                ok, bgr = cap.read()
                if not ok:
                    raise RuntimeError('Seek decode failed')
                live.process(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA), reset=True)
            finally:
                if writer is not None:
                    writer.abort()
                if live is not None:
                    live.close()
                cap.release()
            verify = cv2.VideoCapture(str(target))
            decoded = 0
            while verify.read()[0]:
                decoded += 1
            verify.release()
            if decoded != count:
                raise RuntimeError('Encoded video incomplete')
            record['decoded_frames'] = decoded
        print(json.dumps({key: value for key, value in record.items()
                          if key not in ('frame_sha256', 'process_ms', 'guidance_metrics')}, ensure_ascii=False), flush=True)
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    main()
