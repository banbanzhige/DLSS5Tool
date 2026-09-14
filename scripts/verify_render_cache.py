"""Small real-GPU regression: exact warm pixels and cached SR/FG export.

Run from the repository root with .venv/Scripts/python.exe -B.
No dependencies are downloaded; outputs stay in the registered task directory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dlss5tool.render_cache import RenderCache, encode_cached
from dlss5tool.frame_generation import export_video
from dlss5tool.video_export import probe_video_stream, find_ffmpeg


def run(args):
    root = Path(__file__).resolve().parents[1]
    source = root/'tmp/hdr-e2e'/f'source-{args.color}.mp4'
    task = root/args.task
    if not (task/'TASK.md').is_file():
        raise ValueError('Register the task directory with TASK.md first')
    destination = task/f'{args.color}-sr{args.scale}-fg{args.multiplier}{args.label}.mp4'
    if destination.exists():
        raise ValueError(f'Refusing overwrite: {destination}')
    config = {'super_resolution_scale': args.scale, 'frame_generation_multiplier': args.multiplier,
              'guidance_mode': 0, 'host_backend': 'v2', 'host_zero_fast_path': True,
              'dlss_runtime': '__bundled__', 'hdr_mode': True, 'intensity': .5,
              'output_mix': .7, 'output_view': 0, 'quality_profile': 'high', 'nvenc_preset': 'p5'}
    manager = RenderCache(1024*1024**2)
    cancel = threading.Event()
    timer = threading.Timer(180, cancel.set)
    timer.start()
    try:
        session = manager.session(source, config)
        started = time.perf_counter()
        metadata = session.wait_metadata(cancel)
        count = metadata['source_frames']*args.multiplier
        hashes = []
        for i in range(count):
            pair = session.wait(i, cancel)
            hashes.append(hashlib.sha256(pair[0].tobytes()+pair[1].tobytes()).hexdigest())
        cold = time.perf_counter()-started
        deadline = time.perf_counter()+30
        while not session.complete:
            if session.error:
                raise session.error
            if time.perf_counter() > deadline:
                raise TimeoutError('producer did not finish')
            time.sleep(.01)
        before = session.snapshot()['computed']
        base_before = manager.base.snapshot()['computed']
        for i in range(count):
            pair = session.request(i)
            assert pair is not None
            assert hashlib.sha256(pair[0].tobytes()+pair[1].tobytes()).hexdigest() == hashes[i]
        started = time.perf_counter()
        result = encode_cached(session, destination, config, cancel, lambda *args: None)
        warm = time.perf_counter()-started
        assert result['cache_hits'] == count, result
        assert result['new_frames'] == 0, result
        assert session.snapshot()['computed'] == before
        assert manager.base.snapshot()['computed'] == base_before
        if args.compare_fresh:
            fresh = []
            export_video(source, None, multiplier=args.multiplier, scale=args.scale, enhance=True,
                settings=config, cancel=cancel, frame_sink=lambda i, a, b:
                fresh.append(hashlib.sha256(a.tobytes()+b.tobytes()).hexdigest()))
            assert fresh == hashes, 'shared pipeline pixels differ from fresh sequential renderer'
        output = probe_video_stream(find_ffmpeg(), destination)
        report = {'source': str(source), 'output': str(destination), 'frames': count,
                  'config': config, 'cold_seconds': cold, 'warm_export_seconds': warm,
                  'cache_hits': result['cache_hits'], 'new_frames': result['new_frames'],
                  'base_computed': base_before, 'fresh_pixels_equal': bool(args.compare_fresh),
                  'pixel_hashes': hashes, 'probe': output}
        report_path = task/f'{destination.stem}.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in report.items() if k not in ('pixel_hashes', 'config')}, ensure_ascii=False), flush=True)
    finally:
        timer.cancel()
        manager.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--color', choices=['sdr', 'hdr10', 'hlg'], default='sdr')
    parser.add_argument('--scale', type=int, choices=[1, 2, 4], default=2)
    parser.add_argument('--multiplier', type=int, choices=[1, 2, 3, 4], default=2)
    parser.add_argument('--label', default='')
    parser.add_argument('--compare-fresh', action='store_true')
    parser.add_argument('--task', default='tmp/render-cache-20260914')
    run(parser.parse_args())
