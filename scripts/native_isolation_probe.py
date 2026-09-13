"""Small-output diagnostic builds; one candidate at a time, read-only runtime baseline.

Subprocesses render real continuous frames and report hashes/means. No video files,
no GUI config writes, no model copies. Flow tests use existing CUDA source worker.
"""
import argparse
from collections import deque
from contextlib import ExitStack
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


def once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f'Unexpected source shape: {old[:80]!r}')
    return text.replace(old, new, 1)


def variant_source(variant):
    base = subprocess.check_output(['git', 'show', 'HEAD:native/host_v2/dlssnr_host_v2.cpp'],
                                   cwd=ROOT).decode('utf-8').replace('\r\n', '\n')
    current = (ROOT / 'native/host_v2/dlssnr_host_v2.cpp').read_text(encoding='utf-8')
    budget_start = current.index('    const int requested_slots = g_slot_count;')
    budget_end = current.index('    for (int index = 0; index < g_slot_count; ++index)', budget_start)
    budget = current[budget_start:budget_end]
    getter_start = current.index('extern "C" __declspec(dllexport) int dlssnr_queue_capacity()')
    getter_end = current.index('extern "C" __declspec(dllexport) void dlssnr_set_options(', getter_start)
    getter = current[getter_start:getter_end]
    edits = [('#include "tile_blend.h"\n', '#include "tile_blend.h"\n#include "host_queue.h"\n'),
             ('constexpr int kMaxSlots = 3;\n', 'constexpr int kMaxSlots = host_queue::kMaxSlots;\nIDXGIAdapter3 *g_budget_adapter = nullptr;\n'),
             ('    Release(g_device);\n', '    Release(g_device);\n    Release(g_budget_adapter);\n'),
             ('    adapter->Release();\n    if (FAILED(result) || g_device == nullptr)',
              '    adapter->QueryInterface(IID_PPV_ARGS(&g_budget_adapter));\n    adapter->Release();\n    if (FAILED(result) || g_device == nullptr)')]
    depth = current
    for old, new in edits:
        depth = once(depth, new, old)
    depth = once(depth, budget, '')
    depth = once(depth, getter, '')
    queue = base
    for old, new in edits:
        queue = once(queue, old, new)
    marker = '        : 1;\n    for (int index = 0; index < g_slot_count; ++index)'
    queue = once(queue, marker, '        : 1;\n' + budget + '    for (int index = 0; index < g_slot_count; ++index)')
    marker = 'extern "C" __declspec(dllexport) void dlssnr_set_options('
    queue = once(queue, marker, getter + marker)
    sources = {'baseline': base, 'depth': depth, 'queue': queue, 'combined': current}
    # Diagnostic control only: remove CPU/GPU in-flight overlap while preserving slot rotation.
    sources['wait'] = once(current, '    if (!SubmitCommands(slot, false))\n',
                          '    if (!SubmitCommands(slot, true))\n')
    return sources[variant]


def run_child(args):
    import cv2
    import numpy as np
    from dlss5tool import dlss_engine as engine, guidance_client
    config = json.loads(args.config)
    engine.HOST_DLL_V2 = config['dll']
    engine.LOG_PATH = str(args.work / (config['name'] + '.log'))
    cap = cv2.VideoCapture(str(args.source))
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    settings = dict(host_backend='v2', host_auto_fallback=False, host_submission=config['submission'],
                    host_in_flight=config['queue'], host_persistent_buffers=True,
                    host_zero_fast_path=not args.flow, guidance_mode=int(args.flow),
                    guidance_device='cuda', guidance_flow_edge=512, guidance_flow_updates=6,
                    guidance_cache_mb=0, guidance_output_layout=config.get('layout', 'flow_only_v2'),
                    style=0, intensity=1.0, local_tone=1.0, local_struct=1.0,
                    use_auto_mask=True, skin_struct=1.0)
    real_popen = subprocess.Popen
    def source_worker(command, **kwargs):
        return real_popen([sys.executable, '-B', '-m', 'dlss5tool.guidance_worker', *command[1:]], cwd=ROOT, **kwargs)
    with ExitStack() as stack:
        if args.flow:
            files = dict(worker=sys.executable, flow_weights=str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'))
            stack.enter_context(mock.patch.object(guidance_client, 'validate', return_value=files))
            stack.enter_context(mock.patch.object(guidance_client.subprocess, 'Popen', side_effect=source_worker))
        live = engine.Live(width, height, settings)
        hashes, means, pending, input_hashes = [], [], deque(), []
        def collect(output):
            if output is None:
                raise RuntimeError('No output')
            index = pending.popleft()
            hashes.append(hashlib.sha256(memoryview(output)).hexdigest())
            means.append(output[..., :3].mean(axis=(0, 1)).tolist())
        start = time.perf_counter()
        for index in range(args.frames):
            ok, bgr = cap.read()
            if not ok:
                raise RuntimeError('Video ended early')
            rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
            input_hashes.append(hashlib.sha256(memoryview(rgba)).hexdigest())
            pending.append(index)
            if live.supports_async:
                if not live.enqueue(rgba, reset=index == 0):
                    raise RuntimeError('Queue rejected')
                if len(pending) >= live.max_in_flight:
                    collect(live.dequeue())
            else:
                collect(live.process(rgba, reset=index == 0))
        while pending:
            collect(live.dequeue())
        elapsed = time.perf_counter() - start
        result = dict(config=config, frames=args.frames, size=[width, height], actual_queue=live.max_in_flight,
                      hashes=hashes, mean_rgb=means, input_hashes=input_hashes,
                      seconds_including_decode_hash_means=elapsed,
                      guidance_info=live.guidance_info, last_metrics=live.guidance_metrics)
        cap.release()
        live.close_guidance()
    (args.work / (config['name'] + '.json')).write_text(json.dumps(result, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--variant', choices=['runtime', 'baseline', 'depth', 'queue', 'combined', 'wait'], default='runtime')
    parser.add_argument('--repeats', type=int, default=6)
    parser.add_argument('--frames', type=int, default=96)
    parser.add_argument('--queues', nargs='+', type=int, default=[1, 2])
    parser.add_argument('--flow', action='store_true')
    parser.add_argument('--compat-only', action='store_true')
    parser.add_argument('--label', default='', help='Separate reference/test label, e.g. second-video')
    parser.add_argument('--config')
    args = parser.parse_args()
    args.work, args.source = args.work.resolve(), args.source.resolve()
    if not (args.work / 'TASK.md').is_file():
        parser.error('Register task and storage budget first')
    os.environ['TEMP'] = os.environ['TMP'] = str(args.work)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    if args.config:
        run_child(args)
        return
    candidate = args.work / 'build/candidate.dll'
    if args.variant != 'runtime':
        generated = args.work / 'variant.cpp'
        source = variant_source(args.variant)
        generated.write_text(source, encoding='utf-8')
        subprocess.run(['cmd', '/c', str(ROOT / 'scripts/build_isolated_host.bat'), str(generated), str(candidate.parent)],
                       cwd=ROOT, check=True, timeout=60)
        (args.work / (args.variant + '-source.sha256')).write_text(hashlib.sha256(source.encode()).hexdigest())
    dll = ROOT / 'runtime/dlssnr_host_v2.dll' if args.variant == 'runtime' else candidate
    dll_hash = hashlib.sha256(dll.read_bytes()).hexdigest()
    suffix = ('-' + args.label) if args.label else ''
    tag = args.variant + ('-flow' if args.flow else '-zero') + suffix
    reference_path = args.work / (('reference-flow' if args.flow else 'reference-zero') + suffix + '.json')
    records = []
    for repeat in range(args.repeats):
        cases = [('compat', 'compatibility', 1)] + [('merged'+str(q), 'merged', q) for q in args.queues]
        if args.compat_only:
            cases = cases[:1]
        if repeat % 2:
            cases.reverse()
        for label, submission, depth in cases:
            config = dict(name=f'{tag}-{label}-r{repeat}', dll=str(dll), dll_sha256=dll_hash,
                          submission=submission, queue=depth)
            destination = args.work / (config['name'] + '.json')
            if destination.exists():
                raise RuntimeError('Refusing to overwrite prior run')
            command = [sys.executable, '-B', __file__, '--work', str(args.work), '--source', str(args.source),
                       '--frames', str(args.frames), '--config', json.dumps(config)]
            if args.flow:
                command.append('--flow')
            subprocess.run(command, check=True, timeout=180)
            result = json.loads(destination.read_text())
            if not reference_path.exists():
                if args.variant != 'runtime' or submission != 'compatibility':
                    raise RuntimeError('First reference must be runtime compatibility')
                reference_path.write_text(json.dumps(result), encoding='utf-8')
            reference = json.loads(reference_path.read_text())
            if reference['frames'] != result['frames']:
                raise RuntimeError('Reference frame count mismatch')
            if reference.get('input_hashes') and reference['input_hashes'] != result['input_hashes']:
                raise RuntimeError('Decoded input changed; output comparison is invalid')
            changed = [i for i, (a, b) in enumerate(zip(reference['hashes'], result['hashes'])) if a != b]
            record = dict(name=config['name'], actual_queue=result['actual_queue'], changed_frames=len(changed),
                          first_changed=changed[:1], max_mean_rgb_shift=max(abs(a-b) for aa,bb in zip(reference['mean_rgb'],result['mean_rgb']) for a,b in zip(aa,bb)),
                          dll_sha256=dll_hash)
            records.append(record)
            (args.work / (tag + '-summary.json')).write_text(json.dumps(records, indent=2))
            print(json.dumps(record), flush=True)


if __name__ == '__main__':
    main()
