"""Real frozen-component execution regression matrix; no user settings writes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
from dlss5tool.guidance_client import GuidanceSession
from dlss5tool.guidance_execution import execution_contract


def trial(component, execution, case):
    mode, direction, precision, size = case
    settings = {'mods_directory': str(component.resolve().parent), 'guidance_mode': mode,
        'guidance_execution': execution, 'guidance_device': 'cuda', 'guidance_depth_encoder': 'vitl',
        'guidance_edge': size, 'guidance_depth_profile': precision, 'guidance_flow_direction': direction,
        'guidance_flow_weights': str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
        'guidance_depth_weights': str(ROOT / 'mods/models/depth_anything_v2_vitl.pth'), 'ui_language': 'en_US'}
    source = cv2.cvtColor(cv2.resize(cv2.imread(str(ROOT / 'img/01.png')), (size, size)), cv2.COLOR_BGR2RGBA)
    frames = [source, np.roll(source, 2, axis=1), np.roll(source, 3, axis=1),
              np.zeros_like(source), np.roll(source, 4, axis=1), np.roll(source, 5, axis=1)]
    session = GuidanceSession(settings, size, size)
    output = []
    try:
        for index, frame in enumerate(frames):
            before = frame.copy()
            mv, dp, reset = session.process(frame, index in (0, 2))
            np.testing.assert_array_equal(frame, before)
            if reset or mode == 2:
                assert not np.any(mv)
            if mode == 1:
                assert not np.any(dp)
            output.append((mv, dp, reset))
        assert output[0][2] and output[2][2] and output[3][2] and output[4][2]
        info, metrics = dict(session.info), dict(session.last_metrics)
    finally:
        session.close()
    return output, info, metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', type=Path, required=True)
    parser.add_argument('--old-component', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for component in (args.component, args.old_component):
        if component.name != 'enhancement' or not (component / 'guidance_worker.exe').is_file():
            parser.error('Need an enhancement directory')
    args.output.mkdir(parents=True, exist_ok=False)
    cases = [(3, 'backward', 'sdpa_fp16', 192), (3, 'forward_negated', 'sdpa_fp16', 192),
             (3, 'backward', 'fp32', 192), (1, 'forward_negated', 'fp32', 192),
             (2, 'backward', 'fp32', 192), (3, 'backward', 'sdpa_fp16', 256)]
    report = []
    for case in cases:
        reference = None
        profiles = ['serial', 'raft_streams']
        for profile in profiles:
            output, info, metrics = trial(args.component, profile, case)
            expected = execution_contract({'guidance_mode': case[0], 'guidance_execution': profile}, 'cuda')
            for key, value in expected.items():
                assert info[key] == value, info
            reference = reference or output
            for actual, baseline in zip(output, reference):
                for a, b in zip(actual, baseline):
                    np.testing.assert_array_equal(a, b)
            record = {'case': case, 'requested_execution': profile, 'actual': expected,
                'equal_to_serial': True, 'explicit_reset_and_cut': True, 'immutable_input': True,
                'timing_kind': metrics['timing_kind'],
                'hashes': [hashlib.sha256(mv.tobytes() + dp.tobytes() + bytes([reset])).hexdigest()
                           for mv, dp, reset in output]}
            report.append(record)
            (args.output / 'report.json').write_text(json.dumps(report, indent=2))
            print(case, profile, 'PASS', flush=True)
        if case == cases[0]:
            old, _, _ = trial(args.old_component, 'serial', case)
            for actual, baseline in zip(old, reference):
                for a, b in zip(actual, baseline):
                    np.testing.assert_array_equal(a, b)
            print('previous frozen serial output preserved: PASS', flush=True)
            report.append({'old_serial_preserved': True})
    (args.output / 'report.json').write_text(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
