"""Real RAFT/Feature18 A/B with source worker; reuses the current CUDA Python.

No EXE/environment copies. Run in guidance-cuda-env; --output must be new.
Tests full-v1 versus compact shared/pipe outputs and old/new native output hashes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import cv2
    import numpy as np
    from dlss5tool import dlss_engine as engine, guidance_client
    from dlss5tool.guidance_transport import FLOW_ONLY

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old', type=Path)
    parser.add_argument('--new', type=Path)
    parser.add_argument('--dll', type=Path)
    parser.add_argument('--layout', choices=['full_v1', FLOW_ONLY], default=FLOW_ONLY)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    image = cv2.imread(str(ROOT / 'img/01.png'))
    if image is None:
        raise RuntimeError('Missing probe image')
    rgba = cv2.cvtColor(cv2.resize(image, (256, 256)), cv2.COLOR_BGR2RGBA)
    frames = [np.roll(rgba, n * 2, axis=1) for n in range(6)]
    settings = {'guidance_mode': 1, 'guidance_device': 'cuda', 'guidance_flow_edge': 128,
                'guidance_depth_edge': 512, 'guidance_cache_mb': 0,
                'guidance_output_layout': args.layout, 'host_backend': 'v2',
                'host_auto_fallback': False, 'host_submission': 'merged',
                'host_in_flight': 3, 'style': 0, 'intensity': 1.0}
    files = {'worker': sys.executable, 'flow_weights': str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth')}
    real_popen = subprocess.Popen

    def launch(command, **kwargs):
        return real_popen([sys.executable, '-B', '-m', 'dlss5tool.guidance_worker', *command[1:]],
                          cwd=ROOT, **kwargs)

    def digest(arrays):
        result = hashlib.sha256()
        for array in arrays:
            result.update(memoryview(array))
        return result.hexdigest()

    with mock.patch.object(guidance_client, 'validate', return_value=files), \
            mock.patch.object(guidance_client.subprocess, 'Popen', side_effect=launch):
        if args.dll:
            engine.HOST_DLL_V2 = str(args.dll.resolve())
            engine.LOG_PATH = str(args.output / 'ngx.log')
            live = engine.Live(256, 256, settings)
            hashes, metrics = [], []
            try:
                for index, frame in enumerate(frames):
                    output = live.process(frame, reset=index in (0, 4))
                    if output is None:
                        raise RuntimeError('Native processing failed')
                    hashes.append(digest([output]))
                    metrics.append(live.guidance_metrics)
                report = {'hashes': hashes, 'info': live.guidance_info, 'metrics': metrics}
            finally:
                # As in native_upload_probe, let the isolated process reclaim
                # NGX; explicit shutdown can block on some installed runtimes.
                live.close_guidance()
        else:
            report = []
            references = None
            for layout, transport in (('full_v1', 'auto'), (FLOW_ONLY, 'auto'), (FLOW_ONLY, 'pipe')):
                session = guidance_client.GuidanceSession({**settings, 'guidance_output_layout': layout,
                                                          'guidance_transport': transport}, 256, 256)
                hashes, milliseconds, metrics = [], [], []
                try:
                    for index, frame in enumerate(frames):
                        start = time.perf_counter()
                        motion, depth, reset = session.process(frame, index in (0, 4),
                                                               copy_outputs=False, allow_missing_depth=True)
                        milliseconds.append((time.perf_counter() - start) * 1000)
                        hashes.append(digest([motion]))
                        metrics.append(dict(session.last_metrics))
                        assert (depth is None) == (layout == FLOW_ONLY)
                        assert session.last_metrics['depth_model_calls'] == 0
                    if references is None:
                        references = hashes
                    assert references == hashes, 'RAFT motion changed'
                    report.append({'layout': layout, 'info': session.info, 'motion_equal': True,
                                   'frame_ms': milliseconds, 'metrics': metrics})
                finally:
                    session.close()
    (args.output / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'native': bool(args.dll),
                      'frames': len(frames), 'checks_passed': True}), flush=True)
    if not args.dll and args.old and args.new:
        results = []
        for label, dll, layout in (('old', args.old, 'full_v1'), ('new', args.new, FLOW_ONLY)):
            destination = args.output / label
            subprocess.run([sys.executable, '-B', __file__, '--dll', str(dll.resolve()), '--layout', layout,
                            '--output', str(destination)], check=True, timeout=120,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            results.append(json.loads((destination / 'result.json').read_text()))
        equal = results[0]['hashes'] == results[1]['hashes']
        (args.output / 'native-equality.json').write_text(json.dumps({'equal': equal}), encoding='utf-8')
        if not equal:
            raise RuntimeError('End-to-end RAFT/native output mismatch')


if __name__ == '__main__':
    main()
