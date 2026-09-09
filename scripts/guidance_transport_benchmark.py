"""A/B transport only: identical decoded frames, models, FP32 and dimensions.

New output directory required. Input decoding, model load, warmup and hashing
are outside the timed process call. Alternating run order reduces order bias;
this is not an export/GUI benchmark and other GPU users can affect results.
"""
import argparse
import hashlib
import json
import multiprocessing
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np

from dlss5tool.dlss_host_process import ProcessLive
from dlss5tool.guidance_client import GuidanceSession
from dlss5tool.guidance_transport import TRANSPORT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=16)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--modes', type=int, choices=[1, 2, 3], nargs='+', default=[3])
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--legacy-component', type=Path,
                        help='Optional old frozen component for the pipe baseline')
    args = parser.parse_args()
    for component in (args.component, args.legacy_component):
        if component is not None and (component.name != 'enhancement' or not (component / 'guidance_worker.exe').is_file()):
            parser.error('Component must be an existing enhancement directory containing guidance_worker.exe')
    if not 4 <= args.frames <= 120 or not 1 <= args.rounds <= 5:
        parser.error('frames must be 4..120 and rounds 1..5')
    args.output.mkdir(parents=True, exist_ok=False)
    frames = []
    cap = cv2.VideoCapture(str(args.source))
    try:
        for _ in range(args.frames + 3):
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError('Source has too few decodable frames')
            frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA))
    finally:
        cap.release()
    h, w = frames[0].shape[:2]
    records, references = [], {}
    for mode in args.modes:
        for run in range(args.rounds):
            transports = ['pipe', TRANSPORT] if run % 2 == 0 else [TRANSPORT, 'pipe']
            for transport in transports:
                component = args.legacy_component if transport == 'pipe' and args.legacy_component else args.component
                settings = {'mods_directory': str(component.resolve().parent),
                    'guidance_mode': mode, 'guidance_transport': transport,
                    'guidance_device': 'cuda', 'guidance_edge': 720,
                    'guidance_depth_encoder': 'vitl', 'guidance_flow_direction': 'backward',
                    'guidance_flow_weights': str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
                    'guidance_depth_weights': str(ROOT / 'mods/models/depth_anything_v2_vitl.pth'),
                    'host_backend': 'v2', 'host_auto_fallback': False, 'host_in_flight': 1,
                    'style': 0, 'intensity': 1.0, 'ui_language': 'en_US'}
                start = time.perf_counter()
                live = ProcessLive(w, h, settings) if args.native else GuidanceSession(settings, w, h)
                timings, metrics, hashes = [], [], []
                try:
                    for index, frame in enumerate(frames):
                        # Include an explicit mid-stream reset in the correctness check.
                        reset = index in (0, len(frames) // 2)
                        tick = time.perf_counter()
                        result = live.process(frame, reset=reset) if args.native else live.process(frame, reset, copy_outputs=False)
                        elapsed = time.perf_counter() - tick
                        if index == 2:
                            warmup = time.perf_counter() - start
                        if index < 3:
                            continue
                        timings.append(elapsed * 1000)
                        metrics.append(dict(live.guidance_metrics if args.native else live.last_metrics))
                        digest = hashlib.sha256()
                        if args.native:
                            digest.update(memoryview(result))
                        else:
                            digest.update(memoryview(result[0]))
                            digest.update(memoryview(result[1]))
                            digest.update(bytes([result[2]]))
                        hashes.append(digest.hexdigest())
                    info = live.guidance_info if args.native else live.info
                    assert info['transport'] == transport, info
                    reference = references.setdefault(mode, hashes)
                    record = {'mode': mode, 'round': run, 'transport': transport,
                        'component': str(component.resolve()), 'native': args.native,
                        'source': str(args.source.resolve()), 'dimensions': [w, h],
                        'frames': len(timings), 'warmup_seconds': warmup,
                        'mean_ms': statistics.mean(timings), 'median_ms': statistics.median(timings),
                        'fps_process_only': 1000 / statistics.mean(timings),
                        'mean_flow_ms': statistics.mean(m['flow_ms'] for m in metrics),
                        'mean_depth_ms': statistics.mean(m['depth_ms'] for m in metrics),
                        'mean_roundtrip_ms': statistics.mean(m['roundtrip_ms'] for m in metrics),
                        'mean_outside_worker_ms': statistics.mean(m['roundtrip_ms'] - m['inference_ms'] for m in metrics),
                        'identical_to_first_run': hashes == reference,
                        'device': info, 'frame_ms': timings, 'sha256': hashes}
                    records.append(record)
                    (args.output / 'report.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
                    print(json.dumps({k: v for k, v in record.items() if k not in ('frame_ms', 'sha256')}), flush=True)
                    if hashes != reference:
                        raise RuntimeError('Output mismatch; see per-frame hashes')
                finally:
                    result = None
                    live.close()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
