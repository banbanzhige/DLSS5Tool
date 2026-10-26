"""Real-GPU byte comparison of SDR packing against the committed implementation."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--baseline-ref', default='HEAD', help='Local commit to compare against')
    args = parser.parse_args()
    task = args.task.absolute()
    if (task != task.resolve() or not task.is_relative_to(ROOT / 'tmp')
            or any(p.is_symlink() or p.is_junction() for p in (task, *task.parents))):
        raise ValueError('Registered repository tmp task without links required')
    if not (task / 'TASK.md').is_file() or (task / 'composition-oracle.json').exists():
        raise ValueError('Fresh evidence path in a registered task required')
    import cv2
    import numpy as np
    from dlss5tool import appearance, residual_composition as rc
    from dlss5tool.image_sequence import open_capture

    committed = subprocess.run(['git', 'rev-parse', '--verify', '--end-of-options',
                                args.baseline_ref + '^{commit}'], cwd=ROOT, check=True,
                                capture_output=True, text=True).stdout.strip()
    source_code = subprocess.run(['git', 'show', f'{committed}:dlss5tool/residual_composition.py'],
        cwd=ROOT, check=True, capture_output=True, text=True, encoding='utf-8').stdout
    names = {'_native_inputs', '_native_compose_frame'}
    nodes = [node for node in ast.parse(source_code).body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    namespace = dict(rc.__dict__)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<committed SDR packing>', 'exec'), namespace)
    baseline = namespace['_native_compose_frame']
    config = json.loads((ROOT / 'var/dlss5_settings.json').read_text(encoding='utf-8'))
    values = appearance.preflight(config)
    capture = open_capture(str(args.source.resolve(strict=True)))
    try:
        ok, bgr = capture.read()
        assert ok
    finally:
        capture.release()
    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
    rgba[..., 3] = (np.arange(rgba.shape[1]) % 256).astype(np.uint8)
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    cases = [('rgba', rgba, 'rgb'), ('rgb', rgb, 'rgb'), ('bgr', bgr, 'bgr'),
             ('strided-rgba', rgba[:, ::2], 'rgb')]
    bridge = rc._native_bridge(rc._composition_adapter(config))
    assert bridge is not None
    results = []
    try:
        for label, original, order in cases:
            processed = original.copy()
            processed[..., :3] += 17
            reference = baseline(original, processed, values, order=order, bridge=bridge)
            timings = {'before': [], 'after': []}
            for phase in ('before', 'after', 'after', 'before'):
                function = baseline if phase == 'before' else rc._native_compose_frame
                for _ in range(6):
                    started = time.perf_counter()
                    actual = function(original, processed, values, order=order, bridge=bridge)
                    timings[phase].append((time.perf_counter()-started)*1000)
                    assert np.array_equal(actual, reference), label
            results.append({'case': label, 'shape': list(original.shape), 'byte_equal': True,
                'sha256': hashlib.sha256(memoryview(np.ascontiguousarray(reference))).hexdigest(),
                'before_mean_ms': statistics.mean(timings['before'][2:]),
                'after_mean_ms': statistics.mean(timings['after'][2:])})
    finally:
        rc.release_native_composer()
    report = {'passed': True, 'baseline_commit': committed, 'cases': results}
    (task / 'composition-oracle.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
