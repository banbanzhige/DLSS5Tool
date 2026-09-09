"""Opt-in CUDA/DLSS check: shared buffers, three pending native slots, resets."""
import argparse
import json
import multiprocessing
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np

from dlss5tool.dlss_host_process import ProcessLive
from dlss5tool.guidance_client import GuidanceSession
from dlss5tool.guidance_transport import TRANSPORT
from dlss5tool.guidance_execution import execution_contract


def native_factory(width, height, settings):
    from dlss5tool import dlss_engine
    if settings.get('_probe_host_dll'):
        dlss_engine.HOST_DLL_V2 = settings['_probe_host_dll']
    return dlss_engine.Live(width, height, settings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--depth-profile', choices=['fp32', 'sdpa_fp16'], default='fp32')
    parser.add_argument('--execution', choices=['serial', 'raft_streams'], default='serial')
    parser.add_argument('--host-dll', type=Path)
    args = parser.parse_args()
    if args.component.name != 'enhancement' or not (args.component / 'guidance_worker.exe').is_file():
        parser.error('Component must be an existing enhancement directory containing guidance_worker.exe')
    args.output.mkdir(parents=True, exist_ok=False)
    image = cv2.cvtColor(cv2.resize(cv2.imread(str(ROOT / 'img/01.png')), (192, 192)), cv2.COLOR_BGR2RGBA)
    frames = [np.roll(image, index, axis=1) for index in range(7)]
    report = []
    for mode in (1, 2, 3):
        settings = {'guidance_mode': mode, 'guidance_transport': TRANSPORT,
            '_probe_host_dll': str(args.host_dll.resolve()) if args.host_dll else None,
            'guidance_depth_profile': args.depth_profile,
            'guidance_execution': args.execution,
            'mods_directory': str(args.component.resolve().parent),
            'guidance_device': 'cuda', 'guidance_edge': 192, 'guidance_depth_encoder': 'vitl',
            'guidance_flow_weights': str(ROOT / 'mods/models/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
            'guidance_depth_weights': str(ROOT / 'mods/models/depth_anything_v2_vitl.pth'),
            'host_backend': 'v2', 'host_auto_fallback': False,
            'host_in_flight': 3, 'host_persistent_buffers': True, 'host_submission': 'merged'}
        session = GuidanceSession(settings, 192, 192)
        try:
            assert session.info['depth_profile'] == (args.depth_profile if mode in (2, 3) else 'fp32')
            for key, value in execution_contract(settings, 'cuda').items():
                assert session.info[key] == value, session.info
            session.process(frames[0], True)
            mv, dp, _ = session.process(frames[1])
            assert bool(np.any(mv)) == (mode in (1, 3))
            assert bool(np.any(dp)) == (mode in (2, 3))
            reset_mv, _, reset = session.process(frames[2], True)
            assert reset and not np.any(reset_mv)
        finally:
            session.close()
        results = []
        for queued in (False, True):
            live = ProcessLive(192, 192, settings, _live_factory=native_factory)
            output = []
            try:
                assert live.supports_async and live.max_in_flight == 3
                pending = 0
                for index, frame in enumerate(frames):
                    reset = index in (0, 4)
                    if queued:
                        assert live.enqueue(frame, reset=reset)
                        pending += 1
                        if pending == 3:
                            output.append(live.dequeue())
                            pending -= 1
                    else:
                        output.append(live.process(frame, reset=reset))
                while pending:
                    output.append(live.dequeue())
                    pending -= 1
                assert live.guidance_info['transport'] == TRANSPORT
                assert all(frame is not None for frame in output)
                results.append(output)
            finally:
                live.close()
        for serial, queued in zip(*results):
            np.testing.assert_array_equal(serial, queued)
        report.append({'mode': mode, 'transport': TRANSPORT, 'frames': len(frames),
                       'depth_profile_requested': args.depth_profile,
                       'execution_requested': args.execution,
                       'three_slot_output_equal': True, 'reset_and_inactive_zero': True})
        (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report[-1]), flush=True)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
