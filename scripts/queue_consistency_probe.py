"""Isolated queue/depth A/B; exact pixels, repeatability and segment-history controls.

Uses existing DLLs and optional source RAFT worker. Never writes product settings.
One full reference window on disk; all other runs compare against it in-place.
"""
import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def child(args):
    import cv2
    import numpy as np
    from dlss5tool import dlss_engine as engine, guidance_client
    config = json.loads(args.config)
    directory = args.work / config['name']
    directory.mkdir(exist_ok=False)
    engine.HOST_DLL_V2 = config['dll']
    engine.LOG_PATH = str(directory / 'ngx.log')
    capture = cv2.VideoCapture(str(args.source))
    # Sequentially decode a bounded real window, no seek ambiguity.
    images = []
    for index in range(args.frames):
        ok, bgr = capture.read()
        if not ok:
            raise RuntimeError('Source is too short')
        images.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA))
    capture.release()
    height, width = images[0].shape[:2]
    settings = dict(host_backend='v2', host_auto_fallback=False,
                    host_submission=config['submission'], host_in_flight=config['queue'],
                    host_persistent_buffers=True, host_zero_fast_path=not args.flow,
                    guidance_mode=1 if args.flow else 0, guidance_device='cuda',
                    guidance_output_layout=config.get('layout', 'flow_only_v2'),
                    guidance_flow_edge=512, guidance_flow_updates=6, guidance_cache_mb=config.get('cache_mb', 0),
                    guidance_flow_backend=config.get('backend', 'raft'), guidance_flow_grid=1,
                    style=0, intensity=1.0, local_tone=1.0, local_struct=1.0,
                    use_auto_mask=True, skin_struct=1.0)
    # Source worker allows testing compact layout without copying/freeze-building Torch.
    real_popen = subprocess.Popen
    def launch(command, **kwargs):
        return real_popen([sys.executable, '-B', '-m', 'dlss5tool.guidance_worker', *command[1:]],
                          cwd=ROOT, **kwargs)
    from contextlib import ExitStack
    with ExitStack() as stack:
        if args.flow:
            files = {'worker': sys.executable,
                     'flow_weights': str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth')}
            stack.enter_context(mock.patch.object(guidance_client, 'validate', return_value=files))
            stack.enter_context(mock.patch.object(guidance_client.subprocess, 'Popen', side_effect=launch))
        started = time.perf_counter()
        live = engine.Live(width, height, settings)
        setup = time.perf_counter() - started
        reference_path = args.work / 'reference.npy'
        baseline = config.get('reference', False)
        if baseline:
            reference = np.lib.format.open_memmap(reference_path, mode='w+', dtype=np.uint8,
                                                 shape=(args.frames, height, width, 4))
        else:
            reference = np.load(reference_path, mmap_mode='r')
        hashes, differences, metrics = [], [], []
        pending = deque()
        start = config.get('start', 0)
        compare_from = config.get('compare_from', start)
        restart_at = config.get('restart_at', -1)
        def collect(output, index):
            if output is None:
                raise RuntimeError('Missing output')
            hashes.append({'frame': index, 'sha256': hashlib.sha256(memoryview(output)).hexdigest()})
            if baseline:
                reference[index] = output
            elif index >= compare_from:
                delta = output[..., :3].astype(np.int16) - reference[index, ..., :3].astype(np.int16)
                absolute = np.abs(delta)
                differences.append({'frame': index, 'mae': float(absolute.mean()),
                                    'max': int(absolute.max()), 'changed_fraction': float((absolute != 0).mean()),
                                    'mean_rgb_shift': delta.mean(axis=(0, 1)).tolist()})
                if index in (compare_from, args.frames - 1):
                    cv2.imwrite(str(directory / f'frame-{index}.png'), cv2.cvtColor(output, cv2.COLOR_RGBA2BGR))
                    visual = np.minimum(absolute.astype(np.int32) * 10, 255).astype(np.uint8)
                    cv2.imwrite(str(directory / f'diff-{index}-x10.png'), cv2.cvtColor(visual, cv2.COLOR_RGB2BGR))
        quality_start = time.perf_counter()
        for index in range(start, args.frames):
            # Explicit same-process reset emulates a segment boundary without changing process.
            if index == restart_at:
                while pending:
                    collect(live.dequeue(), pending.popleft())
            reset = index == start or index == restart_at
            if live.supports_async:
                if not live.enqueue(images[index], reset=reset):
                    raise RuntimeError('Unexpected full queue')
                pending.append(index)
                if len(pending) >= live.max_in_flight:
                    collect(live.dequeue(), pending.popleft())
            else:
                collect(live.process(images[index], reset=reset), index)
            if args.flow:
                metrics.append(dict(live.guidance_metrics))
        while pending:
            collect(live.dequeue(), pending.popleft())
        quality_seconds = time.perf_counter() - quality_start
        if baseline:
            reference.flush()
        del reference
        # Independent warmed throughput, no hashes, pixel metrics or encoding in timed phase.
        def speed(count):
            outstanding = 0
            tick = time.perf_counter()
            for index in range(count):
                frame = images[index % len(images)]
                if live.supports_async:
                    if not live.enqueue(frame, reset=index == 0):
                        raise RuntimeError('Unexpected full speed queue')
                    outstanding += 1
                    if outstanding >= live.max_in_flight:
                        if live.dequeue() is None:
                            raise RuntimeError('Missing speed output')
                        outstanding -= 1
                elif live.process(frame, reset=index == 0) is None:
                    raise RuntimeError('Missing speed output')
            while outstanding:
                if live.dequeue() is None:
                    raise RuntimeError('Missing tail')
                outstanding -= 1
            return time.perf_counter() - tick
        speed(16)
        count = 48 if args.flow else 160
        seconds = speed(count)
        result = dict(config=config, dimensions=[width, height], actual_queue=live.max_in_flight,
                      setup_seconds=setup, quality_seconds_including_analysis=quality_seconds,
                      timed_frames=count, seconds=seconds, fps_process_only=count/seconds,
                      hashes=hashes, differences=differences, metrics=metrics,
                      last_speed_metrics=dict(live.guidance_metrics),
                      guidance_info=live.guidance_info)
        live.close_guidance()
        (directory / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps({'name':config['name'], 'fps':count/seconds, 'queue':live.max_in_flight,
                          'max_mae':max((d['mae'] for d in differences), default=0),
                          'max_diff':max((d['max'] for d in differences), default=0)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--old', type=Path)
    parser.add_argument('--new', type=Path)
    parser.add_argument('--frames', type=int, default=40)
    parser.add_argument('--flow', action='store_true')
    parser.add_argument('--config')
    parser.add_argument('--repeat-check', action='store_true', help='Extra independent repeatability controls; reuse reference')
    parser.add_argument('--alternatives', action='store_true', help='Flow-only compact transfer, NVOFA and warm-cache controls')
    parser.add_argument('--old-controls', action='store_true', help='Original DLL repeatability controls; reuse reference')
    args = parser.parse_args()
    args.work = args.work.resolve()
    if not (args.work / 'TASK.md').is_file():
        parser.error('Register TASK.md and check storage budget first')
    os.environ['TMP'] = os.environ['TEMP'] = str(args.work)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    if args.config:
        child(args)
        return
    if not 32 <= args.frames <= 64:
        parser.error('frames must be 32..64')
    old, new = str(args.old.resolve()), str(args.new.resolve())
    cases = [('old_compat', old, 'compatibility', 1, 'full_v1'),
             ('new_compat', new, 'compatibility', 1, 'flow_only_v2'),
             ('new_merged1', new, 'merged', 1, 'flow_only_v2'),
             ('new_merged2', new, 'merged', 2, 'flow_only_v2'),
             ('new_merged4', new, 'merged', 4, 'flow_only_v2'),
             ('new_merged8', new, 'merged', 8, 'flow_only_v2'),
             ('new_merged16', new, 'merged', 16, 'flow_only_v2')]
    configurations = []
    for repeat in range(2):
        for name, dll, submission, depth, layout in (cases if repeat == 0 else list(reversed(cases))):
            configurations.append(dict(name=f'{name}-r{repeat}', dll=dll, submission=submission,
                                       queue=depth, layout=layout, reference=not configurations))
    mid = args.frames // 2
    for name, start, restart in [('fresh_segment', mid, -1), ('warmup8_segment', mid-8, -1),
                                 ('same_process_reset', 0, mid)]:
        configurations.append(dict(name=name, dll=new, submission='compatibility', queue=1,
                                   layout='flow_only_v2', start=start, compare_from=mid, restart_at=restart))
    if args.repeat_check:
        configurations = []
        controls = [('compat', 'compatibility', 1), ('merged2', 'merged', 2), ('merged8', 'merged', 8)]
        for repeat in range(4):
            for name, submission, depth in (controls if repeat % 2 == 0 else list(reversed(controls))):
                configurations.append(dict(name=f'repeat-{name}-{repeat}', dll=new,
                                           submission=submission, queue=depth, layout='flow_only_v2'))
    if args.alternatives:
        configurations = []
        variants = [('full', 'full_v1', 'raft', 0), ('compact', 'flow_only_v2', 'raft', 0),
                    ('nvofa', 'flow_only_v2', 'nvofa', 0), ('warm_cache', 'flow_only_v2', 'raft', 512)]
        for repeat in range(2):
            for name, layout, backend, cache in (variants if repeat == 0 else list(reversed(variants))):
                configurations.append(dict(name=f'alt-{name}-{repeat}', dll=new, submission='compatibility',
                                           queue=1, layout=layout, backend=backend, cache_mb=cache))
    if args.old_controls:
        configurations = []
        for repeat in range(4):
            pairs = [('compat', 'compatibility', 1), ('merged2', 'merged', 2)]
            for name, submission, depth in (pairs if repeat % 2 == 0 else list(reversed(pairs))):
                configurations.append(dict(name=f'original-{name}-{repeat}', dll=old,
                                           submission=submission, queue=depth, layout='full_v1'))
    report = []
    for config in configurations:
        command = [sys.executable, '-B', __file__, '--source', str(args.source.resolve()),
                   '--work', str(args.work), '--frames', str(args.frames), '--config', json.dumps(config)]
        if args.flow:
            command.append('--flow')
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        print(result.stdout.strip(), flush=True)
        record = json.loads((args.work / config['name'] / 'result.json').read_text(encoding='utf-8'))
        report.append(record)
        report_name = ('original-report.json' if args.old_controls else
                       'alternatives-report.json' if args.alternatives else
                       'repeat-report.json' if args.repeat_check else 'report.json')
        (args.work / report_name).write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
