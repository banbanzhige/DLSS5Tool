"""Bounded first-pass profiling at unchanged user tuning; no persistent user writes.

Fresh session for each run, no warm-cache replays, no dropped frames. Default
source mode is production Models + native Live in one diagnostic process;
--backend frozen instead uses the deployed worker and isolated ProcessLive.
Stage wall timers do not synchronize CUDA and must not be read as kernel times.
"""
import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def summary(rows, field):
    values = [r[field] for r in rows if field in r]
    if not values:
        return {}
    return {'mean': statistics.mean(values), 'median': statistics.median(values),
            'p95': sorted(values)[min(len(values)-1, int(len(values)*.95))]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend', choices=['source', 'frozen'], default='source')
    parser.add_argument('--threads', type=int, default=0, help='0 retains library defaults; source backend only')
    parser.add_argument('--opencv-threads', type=int, default=0)
    parser.add_argument('--frames', type=int, default=165)
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--mode', type=int, choices=[1, 2, 3], default=3)
    parser.add_argument('--component', type=Path, help='Alternate complete enhancement directory; never modifies deployed worker')
    parser.add_argument('--encode', action='store_true')
    parser.add_argument('--numpy-flow-input', action='store_true', help='Experimental CPU preparation only; original RAFT forward unchanged')
    args = parser.parse_args()
    if not 1 <= args.frames <= 600 or not 0 <= args.threads <= 32 or not 0 <= args.opencv_threads <= 32:
        parser.error('Bound frames to 1..600 and thread counts to 0..32')
    if args.backend == 'frozen' and (args.threads or args.opencv_threads):
        parser.error('Thread overrides are diagnostic source-backend options')
    if args.backend == 'frozen' and args.numpy_flow_input:
        parser.error('Candidate patch is source-only; frozen backend runs deployed code')
    if not args.source.is_file():
        parser.error('Source does not exist')
    args.output.mkdir(parents=True, exist_ok=False)
    import cv2
    import numpy as np
    from dlss5tool import app_settings
    from dlss5tool import mod_paths
    from dlss5tool import dlss_engine
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.video_export import FFmpegVideoWriter

    settings = {**app_settings.load(), 'guidance_mode': args.mode,
                'guidance_cache_mb': 0, 'guidance_cache_pool': None,
                'host_backend': 'v2', 'host_auto_fallback': False}
    if args.component:
        if args.component.name != 'enhancement' or not (args.component / 'guidance_worker.exe').is_file():
            parser.error('Need a complete enhancement directory')
        settings['mods_directory'] = str(args.component.resolve().parent)
    # Freeze the selected baseline/candidate method even after source adoption.
    with args.source.open('rb') as handle:
        source_hash = hashlib.file_digest(handle, 'sha256').hexdigest()
    cap = cv2.VideoCapture(str(args.source))
    if not cap.isOpened():
        raise RuntimeError('Cannot decode source')
    w, h, fps = int(cap.get(3)), int(cap.get(4)), float(cap.get(5))
    if not w or not h or w*h > 4096*4096:
        raise ValueError('Probe input must have 1..4096*4096 pixels')
    model = live = writer = None
    samples, stage, hashes, raw_hashes = [], {}, [], []
    runtime = {}
    total_start = time.perf_counter()
    try:
        if args.backend == 'source':
            sys.path.insert(0, str(ROOT / 'tmp/dlss5standaloneV2/models'))
            import torch
            from dlss5tool.guidance_worker import Models
            if args.threads:
                torch.set_num_threads(args.threads)
            if args.opencv_threads:
                cv2.setNumThreads(args.opencv_threads)
            runtime = {'torch_threads': torch.get_num_threads(), 'opencv_threads': cv2.getNumThreads(),
                       'torch': torch.__version__, 'cuda': torch.version.cuda}
            tick = time.perf_counter()
            model = Models({**settings, **mod_paths.guidance_files(settings)})
            import types
            from scripts.flow_input_candidate import prepare_flow, original_flow_input
            model._flow_input = types.MethodType(prepare_flow if args.numpy_flow_input else original_flow_input, model)
            runtime['numpy_flow_input'] = args.numpy_flow_input
            runtime['model_load_ms'] = (time.perf_counter() - tick)*1000
            # Observe existing methods without introducing synchronization.
            for name in ('_flow_input', '_depth_input', '_infer_flow', '_infer_depth', '_finish_flow', '_finish_depth'):
                original = getattr(model, name)
                def measured(*a, _name=name, _original=original, **kw):
                    started = time.perf_counter()
                    try:
                        return _original(*a, **kw)
                    finally:
                        stage[_name + '_wall_ms'] = (time.perf_counter() - started)*1000
                setattr(model, name, measured)
            class Guidance:
                info = {'device': model.device, 'device_name': model.device_name, 'precision': model.precision}
                mv = np.empty((h, w, 2), np.float32)
                dp = np.empty((h, w), np.float32)
                @property
                def last_metrics(self):
                    return model.last_metrics
                def process(self, frame, reset=False, *, copy_outputs=False):
                    return model.process(frame, reset, (self.mv, self.dp))
                def close(self):
                    pass
            dlss_engine.LOG_PATH = str(args.output / 'ngx.log')
            live = dlss_engine.Live(w, h, settings)
            live._guidance = Guidance()
        else:
            live = ProcessLive(w, h, settings)
        runtime['setup_ms'] = (time.perf_counter() - total_start)*1000
        if args.encode:
            writer = FFmpegVideoWriter(str(args.output / 'result.mp4'), w, h, fps,
                nvenc_preset=settings['nvenc_preset'], rate_control=settings['rate_control'],
                quality_profile=settings['quality_profile'], video_bitrate_mbps=settings['video_bitrate_mbps'])
        for i in range(args.frames):
            row = {'index': i}
            tick = time.perf_counter()
            ok, bgr = cap.read()
            if not ok:
                break
            frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
            row['decode_convert_ms'] = (time.perf_counter()-tick)*1000
            stage.clear()
            tick = time.perf_counter()
            output = live.process(frame, reset=i == 0)
            row['process_ms'] = (time.perf_counter()-tick)*1000
            if output is None:
                raise RuntimeError('DLSS returned no frame')
            row.update(live.guidance_metrics)
            row.update(stage)
            tick = time.perf_counter()
            hashes.append(hashlib.sha256(memoryview(output)).hexdigest())
            if model is not None:
                raw_hashes.append([hashlib.sha256(memoryview(live._guidance.mv)).hexdigest(),
                                   hashlib.sha256(memoryview(live._guidance.dp)).hexdigest()])
            row['qa_hash_ms'] = (time.perf_counter()-tick)*1000
            if writer:
                tick = time.perf_counter()
                writer.write(cv2.cvtColor(output, cv2.COLOR_RGBA2BGR))
                row['encode_submit_ms'] = (time.perf_counter()-tick)*1000
            samples.append(row)
            if i % 30 == 0:
                print(args.output.name, i, 'process_ms', round(row['process_ms'], 2), flush=True)
        if not samples:
            raise RuntimeError('Empty clip')
        tick = time.perf_counter()
        if writer:
            writer.finish()
            writer = None
        runtime['encoder_drain_ms'] = (time.perf_counter()-tick)*1000
        runtime['wall_including_setup_decode_hash_encode_ms'] = (time.perf_counter()-total_start)*1000
        if args.encode:
            verify = cv2.VideoCapture(str(args.output / 'result.mp4'))
            decoded = 0
            try:
                while verify.read()[0]:
                    decoded += 1
            finally:
                verify.release()
            if decoded != len(samples):
                raise RuntimeError(f'Encoded output has {decoded} frames, expected {len(samples)}')
            runtime['verified_encoded_frames'] = decoded
        stable = samples[3:] or samples
        fields = ['process_ms', 'inference_ms', 'roundtrip_ms', 'flow_ms', 'depth_ms', 'decode_convert_ms',
                  'encode_submit_ms', *sorted(stage)]
        stats = {key: summary(stable, key) for key in fields}
        record = {'backend': args.backend, 'source_sha256': source_hash, 'dimensions': [w, h],
                  'frames': len(samples), 'settings': settings, 'runtime': runtime,
                  'first_frame_process_ms': samples[0]['process_ms'], 'stats_excluding_first3': stats,
                  'samples': samples, 'output_hashes': hashes, 'guidance_hashes': raw_hashes,
                  'model_calls': {name: sum(r.get(name, 0) for r in samples)
                                  for name in ('flow_model_calls', 'depth_model_calls')},
                  'timing_notes': 'Fresh session, cache disabled; no model warmup excluded from total. Stage wall times include enqueue/waits; dual GPU times overlap.'}
        if args.reference:
            reference = json.loads(args.reference.read_text(encoding='utf-8'))
            if reference['source_sha256'] != source_hash or reference['frames'] != len(samples):
                raise ValueError('Reference source/frame count mismatch')
            def comparable(config):
                return {k: v for k, v in config.items() if k != 'mods_directory'}
            if comparable(reference['settings']) != comparable(settings):
                raise ValueError('Reference tuning changed; not a controlled A/B')
            record['output_matches_reference'] = hashes == reference['output_hashes']
            record['guidance_matches_reference'] = raw_hashes == reference['guidance_hashes'] if raw_hashes and reference['guidance_hashes'] else None
        (args.output / 'report.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: value for key, value in record.items() if key not in ('samples', 'output_hashes', 'guidance_hashes', 'settings')}, ensure_ascii=False), flush=True)
    finally:
        cap.release()
        if live and args.backend == 'frozen':
            live.close()
        elif live:
            # This standalone diagnostic process owns NGX. Its shutdown has a
            # known hang in same-process Torch experiments; OS exit reclaims it.
            live.close_guidance()
        if model:
            model.close()
        if writer:
            writer.abort()
    if args.backend == 'source':
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
