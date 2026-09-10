"""Local packaged EXE / add-on installation verification; no user settings/media.

Extracts lite + add-on into a fresh diagnostic directory, checks every installed
file against full, then tests real frozen app and inference worker on synthetic
inputs. Child PATH contains only Windows System32, not the developer Python.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool.app_version import APP_VERSION
from scripts.package_editions import inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packages', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to(ROOT / 'output'):
        parser.error('Use a NEW directory in workspace output')
    output.mkdir(parents=True)
    packages = args.packages.resolve()
    report = json.loads((packages / 'package-report.json').read_text(encoding='utf-8'))
    if report['version'] != APP_VERSION:
        raise ValueError('Package version mismatch')
    upgrade = output / 'lite-plus-addon'
    upgrade.mkdir()
    for kind in ('lite', 'addon'):
        print('Extracting:', kind, flush=True)
        with zipfile.ZipFile(packages / report['editions'][kind]['name']) as zip:
            for item in zip.infolist():
                if not (upgrade / item.filename).resolve().is_relative_to(upgrade):
                    raise ValueError('Unsafe ZIP path')
            zip.extractall(upgrade)
    expected = json.loads((packages / 'verification/full-files.json').read_text(encoding='utf-8'))
    if inventory(upgrade) != expected:
        raise RuntimeError('Extracted lite + add-on does not equal full package')
    print('Exact install overlay verified.', flush=True)

    from dlss5tool import app_settings
    from dlss5tool import guidance_client
    from dlss5tool import mod_paths
    import numpy as np
    os.environ['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    child_env = dict(os.environ)
    child_env.pop('PYTHONPATH', None)
    child_env.pop('PYTHONHOME', None)
    records = {'version': APP_VERSION, 'overlay_exact': True, 'frozen_app': [], 'addon_modes': []}

    def frozen(kind, folder, mode, expected_ok):
        config = output / f'{kind}-{mode}-settings.json'
        result = output / f'{kind}-{mode}-result.json'
        log = output / f'{kind}-{mode}-ngx.log'
        config.write_text(json.dumps({**app_settings.DEFAULTS, 'guidance_mode': mode,
                                     'dlss_runtime': '__bundled__', 'ui_language': 'en_US'}), encoding='utf-8')
        proc = subprocess.run([str(folder / 'DLSS5Tool.exe'), '--diagnostic-worker', str(result),
                               str(log), str(config), 'v2'], env=child_env, cwd=folder,
                              timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        payload = json.loads(result.read_text(encoding='utf-8'))
        if payload['ok'] != expected_ok or proc.returncode != (0 if expected_ok else 1):
            raise RuntimeError((kind, mode, payload))
        if not expected_ok and 'component' not in payload.get('error', '').lower():
            raise RuntimeError('Missing-component test failed for unrelated reason')
        records['frozen_app'].append({'edition': kind, 'mode': mode, **payload})
        print('Frozen', kind, mode, payload['ok'], flush=True)
        return payload

    frozen('lite', packages / f'DLSS5Tool-{APP_VERSION}-win64-lite', 0, True)
    frozen('lite', packages / f'DLSS5Tool-{APP_VERSION}-win64-lite', 1, False)
    full = frozen('full', packages / f'DLSS5Tool-{APP_VERSION}-win64-full', 3, True)
    upgraded = frozen('upgraded', upgrade, 3, True)
    if full['output_sha256'] != upgraded['output_sha256']:
        raise RuntimeError('Full and add-on installation produce different output')
    rgba = np.empty((128, 128, 4), np.uint8)
    rgba[..., :3] = np.arange(128, dtype=np.uint8)[None, :, None]
    rgba[..., 3] = 255
    from dlss5tool.guidance_public import public_mode
    for requested_mode in (1, 2, 3):
        mode = public_mode(requested_mode)
        settings = {**app_settings.DEFAULTS, 'mods_directory': str(upgrade / 'mods'),
                    'guidance_mode': mode, 'guidance_cache_mb': 0}
        if mode == 0:
            if guidance_client.preflight(settings):
                raise RuntimeError('Retired depth-only mode must remain off')
            records['addon_modes'].append({'requested_mode': requested_mode, 'mode': 0})
            continue
        files = guidance_client.validate(settings)
        for key in ('worker', 'flow_weights', 'depth_weights'):
            if key in files and not Path(files[key]).resolve().is_relative_to(upgrade):
                raise RuntimeError(f'Developer fallback leaked into package validation: {key}')
        session = guidance_client.GuidanceSession(settings, 128, 128)
        try:
            first = session.process(rgba, True)
            second = session.process(np.roll(rgba, 1, axis=1), False)
            if not first[2] or second[2] or np.any(first[0]):
                raise RuntimeError('Unexpected temporal reset')
            if bool(np.any(second[0])) != (mode in (1, 3)):
                raise RuntimeError('Flow mode did not produce expected output')
            if bool(np.any(second[1])) != (mode in (2, 3)):
                raise RuntimeError('Depth mode did not produce expected output')
            records['addon_modes'].append({'mode': mode, 'info': session.info,
                                            'flow_nonzero': bool(np.any(second[0])),
                                            'depth_nonzero': bool(np.any(second[1]))})
        finally:
            session.close()
        print('Installed add-on mode', mode, 'passed', flush=True)
    records['full_equals_upgraded_output'] = True
    records['clean_machine_verified'] = False
    (output / 'report.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
    print('PASS: packaged base, full and lite+addon; this is not a clean-machine certification.', flush=True)


if __name__ == '__main__':
    main()
