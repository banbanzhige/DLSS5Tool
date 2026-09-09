"""Real frozen-worker parameter/default parity checks, with synthetic motion."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
from dlss5tool import guidance_client
from dlss5tool.guidance_parameters import analysis_parameters


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--component', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    args = parser.parse_args()
    base = {'guidance_mode': 3, 'guidance_device': 'cuda', 'guidance_depth_encoder': 'vitl',
            'guidance_edge': 256, 'guidance_cache_mb': 64,
            'guidance_flow_weights': str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
            'guidance_depth_weights': str(ROOT / 'mods/models/depth_anything_v2_vitl.pth')}
    # Component discovery uses <mods_directory>/enhancement.
    source = cv2.resize(cv2.imread(str(ROOT / 'img/01.png')), (256, 256))
    first = cv2.cvtColor(source, cv2.COLOR_BGR2RGBA)
    frames = [cv2.warpAffine(first, np.float32([[1, 0, i * 2], [0, 1, i]]), (256, 256),
                            borderMode=cv2.BORDER_REFLECT_101) for i in range(3)]
    report = {}

    def run(label, component, changes):
        settings = {**base, 'mods_directory': str(component.parent.resolve()), **changes}
        outputs = []
        with_session = guidance_client.GuidanceSession(settings, 256, 256)
        try:
            for index, frame in enumerate(frames):
                output = with_session.process(frame, index == 0)
                assert np.isfinite(output[0]).all() and np.isfinite(output[1]).all()
                outputs.append(output)
            metrics = dict(with_session.last_metrics)
            if label != 'baseline':
                assert with_session.info['analysis_parameters'] == analysis_parameters(settings)
            # A replay must preserve output exactly and hit raw predictions.
            for index, frame in enumerate(frames):
                again = with_session.process(frame, index == 0)
                np.testing.assert_array_equal(again[0], outputs[index][0])
                np.testing.assert_array_equal(again[1], outputs[index][1])
            report[label] = {'parameters': with_session.info.get('analysis_parameters'),
                             'metrics': metrics, 'replay': with_session.last_metrics}
            print(label, json.dumps(report[label]), flush=True)
        finally:
            with_session.close()
        return outputs

    original = run('baseline', args.baseline, {})
    current = run('defaults', args.component, {})
    for before, after in zip(original, current):
        np.testing.assert_array_equal(before[0], after[0])
        np.testing.assert_array_equal(before[1], after[1])
    changed = run('custom', args.component, {'guidance_flow_edge': 192, 'guidance_depth_edge': 256,
            'guidance_flow_updates': 12, 'guidance_depth_smoothing': 0.5,
            'guidance_depth_low': 5, 'guidance_depth_high': 95})
    assert not np.array_equal(changed[-1][0], current[-1][0])
    assert not np.array_equal(changed[-1][1], current[-1][1])
    run('streams', args.component, {'guidance_flow_edge': 192, 'guidance_depth_edge': 256,
                                   'guidance_flow_updates': 4, 'guidance_execution': 'raft_streams'})
    report['default_bitwise_equal'] = True
    output = ROOT / 'output/guidance-parameters-probe.json'
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(output, flush=True)


if __name__ == '__main__':
    main()
