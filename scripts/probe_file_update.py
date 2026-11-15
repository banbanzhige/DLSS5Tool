"""Smoke-test a real frozen helper against small synthetic release files."""
import argparse
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool import delta_update as delta, updater
from scripts.build_file_update import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helper', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--legacy-runtime', action='store_true',
                        help='Also verify a custom bundled runtime and mods runtime survive the frozen helper')
    parser.add_argument('--edition', choices=('lite', 'full'), default='lite')
    parser.add_argument('--readme-state', choices=('official', 'missing', 'empty', 'modified', 'crlf'),
                        default='official', help='Set local README, changelog, license and notice states')
    parser.add_argument('--change-documents', action='store_true',
                        help='Also change all official documents in the target release')
    args = parser.parse_args()
    output = args.output.absolute()
    delta.safe_path(output.parent, output.name)
    delta.safe_path(args.helper.absolute().parent, args.helper.name)
    output.mkdir()  # Never overwrite a previous probe.
    before, after = output / 'before', output / 'after'
    document_names = ['mods/README.md', 'README.md', 'CHANGELOG.md', 'LICENSE',
                      'THIRD_PARTY_NOTICES.md', '_internal/GUIDANCE_PARAMETERS.md']
    if args.edition == 'full':
        document_names.append('mods/ADDON-INSTALL.txt')
    for folder, content in ((before, b'old'), (after, b'new')):
        folder.mkdir()
        (folder / 'DLSS5Tool.exe').write_bytes(content)
        shutil.copyfile(args.helper, folder / delta.HELPER)
        (folder / '_internal').mkdir()
        (folder / '_internal/unchanged.dll').write_bytes(b'unchanged dependency')
        (folder / 'mods').mkdir()
        for name in document_names:
            (folder / name).write_bytes(b'updated instructions\n' if folder == after and args.change_documents
                                       else b'official instructions\n')
        if args.edition == 'full':
            (folder / 'mods/enhancement').mkdir()
            (folder / 'mods/enhancement/guidance_worker.exe').write_bytes(b'unchanged worker')
        if args.legacy_runtime:
            (folder / delta.LEGACY_RUNTIME).write_bytes(b'official unchanged runtime')
    package = build(before, after, output / 'payload', 'v9.0.0', 'v9.0.1', args.edition)
    install = output / 'installed'
    shutil.copytree(before, install)
    (install / 'dlss5_settings.json').write_bytes(b'keep settings')
    expected_documents = {}
    for name in document_names:
        document = install / name
        if args.readme_state == 'missing':
            document.unlink()
        elif args.readme_state != 'official':
            document.write_bytes({'empty': b'', 'modified': b'my notes',
                                  'crlf': b'official instructions\r\n'}[args.readme_state])
        expected_documents[name] = (b'updated instructions\n' if args.change_documents and args.readme_state == 'official'
                                    else document.read_bytes() if document.exists() else None)
    if args.legacy_runtime:
        (install / delta.LEGACY_RUNTIME).write_bytes(b'legacy custom runtime')
        (install / 'mods/nvngx_dlssnr.dll').write_bytes(b'custom mods runtime')
    data = package.read_bytes()
    record = delta.file_record(package)
    asset = updater.ReleaseAsset(package.name,
        f'https://github.com/{updater.GITHUB_REPOSITORY}/releases/download/v9.0.1/{package.name}',
        len(data), 'sha256:' + record['sha256'])
    delta.prepare(install, asset, 'v9.0.0', 'v9.0.1', args.edition, opener=lambda *a, **k: io.BytesIO(data))
    parent = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(2)'],
                              creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        result = subprocess.run([str(install / delta.TRANSACTION / delta.HELPER),
            '--root', str(install), '--parent-pid', str(parent.pid), '--quiet'],
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
    finally:
        parent.wait(timeout=10)
    state = delta.status(install)
    if result.returncode != 0 or not state or state['phase'] != 'complete':
        raise RuntimeError(f'Frozen helper failed: exit={result.returncode}, state={state}')
    checks = {
        'target': (install / 'DLSS5Tool.exe').read_bytes() == b'new',
        'settings': (install / 'dlss5_settings.json').read_bytes() == b'keep settings',
        'backup': (install / delta.TRANSACTION / 'backup/DLSS5Tool.exe').read_bytes() == b'old',
    }
    for name, expected in expected_documents.items():
        document = install / name
        checks[name] = (document.read_bytes() if document.exists() else None) == expected
        backup = install / delta.TRANSACTION / 'backup' / name
        if args.change_documents and args.readme_state == 'official':
            checks['backup/' + name] = backup.read_bytes() == b'official instructions\n'
        else:
            checks['no backup/' + name] = not backup.exists()
    if args.legacy_runtime:
        checks['legacy runtime'] = (install / delta.LEGACY_RUNTIME).read_bytes() == b'legacy custom runtime'
        checks['mods runtime'] = (install / 'mods/nvngx_dlssnr.dll').read_bytes() == b'custom mods runtime'
    if not all(checks.values()):
        raise RuntimeError('Frozen update verification failed: ' + ', '.join(k for k, ok in checks.items() if not ok))
    report = {'frozen_helper_sha256': delta.file_record(args.helper)['sha256'],
              'exit_code': result.returncode, 'state': delta.status(install),
              'settings_preserved': True, 'backup_verified': True,
              'edition': args.edition, 'readme_state': args.readme_state,
              'readme_preserved': True,
              'official_documents_changed': args.change_documents,
              'documents_verified': document_names,
              'legacy_runtime_preserved': args.legacy_runtime,
              'scope': 'Synthetic files; not full app/GPU/clean-machine acceptance'}
    delta.write_json(output / 'report.json', report)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
