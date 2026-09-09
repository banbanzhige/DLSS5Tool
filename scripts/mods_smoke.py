"""Opt-in real model / native integration test using temporary user-module layout."""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
import numpy as np
import guidance_client
import mod_paths


def native_factory(width, height, settings):
    import dlss_engine
    settings = {**settings, 'mods_directory': settings['_smoke_mods']}
    return dlss_engine.Live(width, height, settings)


def frozen_probe(executable, root, settings, expected=True):
    config, result, log = root / 'smoke-settings.json', root / 'smoke-result.json', root / 'smoke-ngx.log'
    config.write_text(json.dumps(settings), encoding='utf-8')
    process = subprocess.run([str(executable), '--diagnostic-worker', str(result), str(log), str(config), 'v2'],
                             check=False, timeout=90, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert process.returncode == (0 if expected else 1), process.returncode
    payload = json.loads(result.read_text(encoding='utf-8'))
    assert payload['ok'] == expected, payload
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--component', type=Path, required=True, help='Frozen enhancement folder')
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--language', choices=['zh_CN', 'en_US'], default='zh_CN')
    parser.add_argument('--frozen-exe', type=Path)
    parser.add_argument('--modes', nargs='+', type=int, default=[1, 2, 3])
    parser.add_argument('--device', choices=['auto', 'cuda', 'cpu'], default='cpu')
    args = parser.parse_args()
    # The actual child executable must not depend on a system Python on PATH.
    os.environ['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    report = []
    (ROOT / 'output').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mods-smoke-', dir=ROOT / 'output') as directory:
        root = Path(directory)
        frozen = None
        if args.frozen_exe:
            package = root / 'package'
            shutil.copytree(args.frozen_exe.resolve().parent, package)
            frozen = package / args.frozen_exe.name
            root = package / 'mods'
            baseline = {'guidance_mode': 0, 'mods_directory': str(root), 'dlss_runtime': '__bundled__', 'ui_language': 'en_US'}
            frozen_probe(frozen, root, baseline)
            rejected = frozen_probe(frozen, root, {**baseline, 'guidance_mode': 1}, expected=False)
            assert 'component is missing' in rejected['error'], rejected
            report.append({'base_only_frozen_ok': True, 'missing_component_rejected': True})
            print(json.dumps(report[-1]), flush=True)
            # Exercise the user runtime override without changing the original.
            os.link(package / '_internal/nvngx_dlssnr.dll', root / 'nvngx_dlssnr.dll')
        (root / 'models').mkdir()
        shutil.copytree(args.component.resolve(), root / 'enhancement', copy_function=os.link)
        for name, source in (
            ('raft_large_C_T_SKHT_V2-ff5fadd5.pth', args.reference / 'torch_home/hub/checkpoints/raft_large_C_T_SKHT_V2-ff5fadd5.pth'),
            ('depth_anything_v2_vitl.pth', args.reference / 'models/checkpoints/depth_anything_v2_vitl.pth'),
        ):
            os.link(source.resolve(), root / 'models' / name)
        # No architecture source or Python environment is copied into user mods.
        (root / 'models/raft_large_C_T_SKHT_V2-ff5fadd5.pth').rename(root / 'models/custom-flow.pth')
        (root / 'models/depth_anything_v2_vitl.pth').rename(root / 'models/custom-depth.pth')
        original_mods = mod_paths.mods_root
        mod_paths.mods_root = lambda settings=None: root
        try:
            source = cv2.resize(cv2.imread(str(ROOT / 'img/01.png')), (192, 192))
            first = cv2.cvtColor(source, cv2.COLOR_BGR2RGBA)
            second = cv2.warpAffine(first, np.float32([[1, 0, 3], [0, 1, 1]]), (192, 192), borderMode=cv2.BORDER_REFLECT_101)
            for mode in args.modes:
                settings = {'guidance_mode': mode,
                            'guidance_edge': 192, 'guidance_device': args.device, 'guidance_depth_encoder': 'vitl',
                            'host_backend': 'v2', 'host_auto_fallback': False, '_smoke_mods': str(root),
                            'mods_directory': str(root),
                            'guidance_flow_weights': 'models/custom-flow.pth',
                            'guidance_depth_weights': 'models/custom-depth.pth',
                            'ui_language': args.language}
                session = guidance_client.GuidanceSession(settings, 192, 192)
                try:
                    mv0, dp0, reset0 = session.process(first, True)
                    mv1, dp1, reset1 = session.process(second, False)
                    mv2, dp2, reset2 = session.process(second, True)
                    assert reset0 and reset2 and not reset1
                    assert not np.any(mv0) and not np.any(mv2)
                    assert bool(np.any(mv1)) == (mode in (1, 3))
                    assert bool(np.any(dp1)) == (mode in (2, 3))
                    record = {'mode': mode, 'mean_abs_flow': float(np.abs(mv1).mean()),
                              'depth_range': [float(dp1.min()), float(dp1.max())], 'explicit_reset': True,
                              'frozen_component': True, 'system_python_on_path': False}
                    record['actual_device'] = session.info
                finally:
                    session.close()
                if args.native:
                    from dlss_host_process import ProcessLive
                    live = ProcessLive(192, 192, settings, _live_factory=native_factory)
                    try:
                        output = live.process(first, reset=True)
                        assert output is not None and output.shape == first.shape
                        if live.supports_async:
                            assert live.enqueue(second)
                            assert live.dequeue() is not None
                        record['native_output_mae'] = float(np.abs(output[..., :3].astype(float) - first[..., :3]).mean())
                    finally:
                        live.close()
                if frozen:
                    frozen_probe(frozen, root, settings)
                    record['frozen_ok'] = True
                report.append(record)
                (ROOT / 'output/mods-smoke.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
                print(json.dumps(record), flush=True)
            wrong = {**settings, 'guidance_mode': 2, 'guidance_depth_weights': 'models/custom-flow.pth'}
            try:
                invalid = guidance_client.GuidanceSession(wrong, 192, 192)
            except RuntimeError as exc:
                assert 'state_dict' in str(exc) and 'DepthAnythingV2' in str(exc), str(exc)
                report.append({'incompatible_weights_rejected': True})
            else:
                invalid.close()
                raise AssertionError('RAFT weights must not load into Depth Anything')
        finally:
            mod_paths.mods_root = original_mods
    (ROOT / 'output/mods-smoke.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
