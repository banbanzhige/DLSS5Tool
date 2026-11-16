"""Build tiny portable fixtures and audit one native Velopack update at a time.

Tools/SDK must already be checksum-verified in a registered tmp task. This never
runs an installer, uploads releases, deletes an installation, or copies a GPU
environment. Archive a case report and clean that exact task install before the
next case. Run --stage once, then --case with the same --output directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool import delta_update as delta

PACKAGE_ID = 'DLSS5Tool.UpdateProbe'
APP = 'DLSS5Tool.exe'
VERSIONS = ('1.0.0', '1.0.1', '1.0.2')
CASES = ('lite-full', 'lite-delta', 'lite-missing-hop', 'full-full', 'full-delta',
         'full-wrong-channel', 'lite-corrupt-full', 'lite-broken-delta')


def run(command, log, *, env=None, expected_success=True):
    result = subprocess.run([str(p) for p in command], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=180)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout + '\n' + result.stderr, encoding='utf-8')
    if expected_success and result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}); see {log}')
    return result


def unpack(archive, destination):
    if destination.exists():
        raise RuntimeError('Review and safely clean the previous task install before another case')
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            path = delta.safe_path(destination, item.filename)
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, path.open('xb') as target:
                shutil.copyfileobj(source, target)


def package_inventory(path):
    """Audit reconstructed payload bytes independently of ZIP recompression."""
    inventory = {}
    with zipfile.ZipFile(path) as bundle:
        for item in bundle.infolist():
            if item.is_dir():
                continue
            if item.filename in inventory:
                raise RuntimeError('Duplicate package entry')
            with bundle.open(item) as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            inventory[item.filename] = {'size': item.file_size, 'sha256': digest}
    return inventory


def native_updater_idle(install):
    """Pinned Windows SDK holds this lock through its post-update callbacks."""
    import msvcrt
    try:
        with (install / 'packages/.velopack_lock').open('r+b') as handle:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--sdk', type=Path)
    parser.add_argument('--vpk', type=Path)
    parser.add_argument('--wrapped', action='store_true')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--stage', action='store_true')
    action.add_argument('--case', choices=CASES)
    args = parser.parse_args()
    output = args.output.absolute()
    if not output.is_relative_to(ROOT / 'tmp') or not output.parent.joinpath('TASK.md').is_file():
        parser.error('Use a new child of a registered repository tmp task')
    delta.safe_path(output.parent, output.name)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TEMP=str(output.parent), TMP=str(output.parent),
               DOTNET_CLI_HOME=str(output.parent / 'dotnet-cli'),
               DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1', DOTNET_CLI_TELEMETRY_OPTOUT='1')
    evidence = output / 'evidence'
    if args.stage:
        if not args.sdk or not args.vpk or output.exists():
            parser.error('Staging requires verified --sdk/--vpk and a NEW --output')
        free = shutil.disk_usage(output.parent).free
        if free < 512 * 1024**2 + 15 * 1024**3:
            parser.error('Need fixture peak + 15 GiB free space')
        output.mkdir()
        print('Building the tiny Python onedir app...', flush=True)
        run([sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm', '--onedir', '--console', '--noupx',
             '--name', 'DLSS5Tool', '--paths', args.sdk.absolute(), '--paths', ROOT,
             '--distpath', output / 'build/dist', '--workpath', output / 'build/work',
             '--specpath', output / 'build', '--exclude-module', 'numpy', '--exclude-module', 'tkinter',
             ROOT / 'scripts/velopack_probe_app.py'], evidence / 'build.log', env=env)
        stage = output / 'build/dist/DLSS5Tool'
        for edition in ('lite', 'full'):
            marker = {'schema': 1, 'provider': 'velopack-portable', 'package_id': PACKAGE_ID,
                      'edition': edition, 'channel': 'win-' + edition}
            (stage / '_internal/portable-layout.json').write_text(json.dumps(marker), encoding='utf-8')
            for version in VERSIONS:
                print(f'Packaging synthetic {edition} {version}...', flush=True)
                (stage / '_internal/probe-version.txt').write_text(version, encoding='utf-8')
                (stage / 'README.md').write_text('Probe docs ' + version, encoding='utf-8')
                run(['dotnet', args.vpk.absolute(), 'pack', '--packId', PACKAGE_ID,
                     '--packVersion', version, '--packDir', stage, '--mainExe', APP,
                     '--outputDir', output / ('release-' + edition), '--channel', 'win-' + edition,
                     '--runtime', 'win-x64', '--noInst', '--icon', ROOT / 'assets/app.ico'],
                    evidence / f'pack-{edition}-{version}.log', env=env)
                if version == VERSIONS[0]:
                    portable, = (output / ('release-' + edition)).glob('*Portable.zip')
                    shutil.copyfile(portable, output / f'seed-{edition}.zip')
            from scripts.package_managed_core import write_manifest
            from dlss5tool.managed_update import manifest_name
            package, = (output / ('release-' + edition)).glob('*1.0.2*full.nupkg')
            write_manifest(package, 'v1.0.2', edition, PACKAGE_ID,
                           package.parent / manifest_name('v1.0.2', edition))
        delta.write_json(output / 'staging.json', {'package_id': PACKAGE_ID, 'versions': VERSIONS,
                         'editions': ['lite', 'full'], 'sdk': str(args.sdk.absolute()),
                         'vpk': str(args.vpk.absolute()), 'scope': 'Synthetic fixtures only'})
        return

    if not (output / 'staging.json').is_file():
        parser.error('Run --stage first')
    case = args.case
    edition = case.split('-')[0]
    feed = output / ('release-' + edition)
    feed_data = json.loads((feed / f'releases.win-{edition}.json').read_bytes())
    assets = feed_data['Assets']
    base = next(a for a in assets if a['Type'] == 'Full' and a['Version'] == '1.0.0')
    target = next(a for a in assets if a['Type'] == 'Full' and a['Version'] == '1.0.2')
    install = output / 'installed'
    unpack(output / f'seed-{edition}.zip', install)
    (install / 'packages').mkdir(exist_ok=True)
    if case.endswith(('delta', 'missing-hop')):
        shutil.copyfile(feed / base['FileName'], install / 'packages' / base['FileName'])
    if case.endswith(('missing-hop', 'corrupt-full', 'broken-delta')):
        view = output / ('feed-' + case)
        view.mkdir()
        selected = assets if case.endswith('broken-delta') else [a for a in assets if a['Version'] in ('1.0.0', '1.0.2')]
        delta.write_json(view / f'releases.win-{edition}.json', {'Assets': selected})
        for asset in selected:
            shutil.copyfile(feed / asset['FileName'], view / asset['FileName'])
        for metadata in feed.glob('*.manifest.json'):
            shutil.copyfile(metadata, view / metadata.name)
        if case.endswith(('corrupt-full', 'broken-delta')):
            filename = target['FileName'] if case.endswith('corrupt-full') else next(
                a['FileName'] for a in assets if a['Type'] == 'Delta' and a['Version'] == '1.0.2')
            path = view / filename
            with path.open('r+b') as package:
                package.seek(100)
                byte = package.read(1)
                package.seek(100)
                package.write(bytes([byte[0] ^ 1]))
        feed = view
    elif case == 'full-wrong-channel':
        feed = output / 'release-lite'

    full_files = {'mods/enhancement/guidance_worker.exe': b'full worker fixture, never executed',
                  'mods/enhancement/enhancement.json': b'{"id":"dlss5-guidance","protocol":1}',
                  'mods/models/raft.pth': b'official model fixture, never loaded'} if edition == 'full' else {}
    for name, content in full_files.items():
        path = delta.safe_path(install, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    before_file = evidence / f'{case}-before.json'
    app = install / 'current' / APP
    common = [app, '--sandbox', output.parent, '--stable-data']
    if args.wrapped:
        common.append('--wrapped')
    run([*common, '--report', before_file, '--write-markers'], evidence / f'{case}-before.log', env=env)
    before = json.loads(before_file.read_bytes())
    negative = case.endswith(('wrong-channel', 'corrupt-full'))
    ready_file = evidence / f'{case}-ready.json'
    result = run([*common, '--report', ready_file, '--feed', feed, '--download'],
                 evidence / f'{case}-download.log', env=env, expected_success=not negative)
    if negative:
        if result.returncode == 0:
            raise RuntimeError('Invalid source unexpectedly accepted')
        expected_version = '1.0.0'
        ready = None
    else:
        ready = json.loads(ready_file.read_bytes())
        if ready['current_version'] != '1.0.0' or ready['pending_version'] != '1.0.2':
            raise RuntimeError('Download modified the running version or did not prepare target')
        # Starting again must not apply a prepared update before an explicit action.
        still_file = evidence / f'{case}-still-old.json'
        run([*common, '--report', still_file], evidence / f'{case}-still-old.log', env=env)
        if json.loads(still_file.read_bytes())['current_version'] != '1.0.0':
            raise RuntimeError('Pending update applied implicitly on startup')
        cached_path = install / 'packages' / target['FileName']
        cached = delta.file_record(cached_path)
        publisher = output / ('release-' + edition) / target['FileName']
        publisher_record = {'size': target['Size'], 'sha256': target['SHA256'].lower()}
        if delta.file_record(publisher) != publisher_record:
            raise RuntimeError('Publisher fixture changed after packaging')
        target_files = package_inventory(publisher)
        if package_inventory(cached_path) != target_files:
            raise RuntimeError('Reconstructed package content differs from publisher target')
        run([*common, '--report', evidence / f'{case}-apply.json', '--feed', feed, '--download', '--apply'],
            evidence / f'{case}-apply.log', env=env)
        for _ in range(150):
            try:
                complete = '<version>1.0.2</version>' in (install / 'current/sq.version').read_text()
            except OSError:
                complete = False
            if complete and native_updater_idle(install):
                break
            time.sleep(.1)
        else:
            raise RuntimeError('Native updater did not finish')
        expected_version = '1.0.2'
    after_file = evidence / f'{case}-after.json'
    run([*common, '--report', after_file], evidence / f'{case}-after.log', env=env)
    after = json.loads(after_file.read_bytes())
    if (after['current_version'] != expected_version or not after['portable']
            or after['data_root'] != str(install) or after['state_root'] != str(install)
            or not all(v['matches'] for v in after['markers'].values())):
        raise RuntimeError('Version or stable portable user-data verification failed')
    if any(delta.safe_path(install, name).read_bytes() != value for name, value in full_files.items()):
        raise RuntimeError('Full edition external component/model changed')
    report = {'case': case, 'edition': edition, 'from': before['current_version'],
              'to': after['current_version'], 'rejected_invalid_source': negative,
              'planned': None if ready is None else ready['update'],
              'target_package_files_verified': 0 if negative else len(target_files),
              'reconstructed_archive_matches_publisher': None if negative else cached == publisher_record,
              'reconstructed_record': None if negative else cached,
              'stable_user_files_verified': len(after['markers']),
              'full_component_files_verified': len(full_files), 'implicit_apply_disabled': True,
              'state_and_models_outside_current': True, 'install': str(install),
              'production_adapter': args.wrapped,
              'passed': True, 'scope': 'Real native SDK/Update.exe, synthetic files, no GPU/production app acceptance'}
    delta.write_json(evidence / f'{case}-report.json', report)
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
