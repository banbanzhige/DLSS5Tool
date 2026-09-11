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
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir()  # Never overwrite a previous probe.
    before, after = output / 'before', output / 'after'
    for folder, content in ((before, b'old'), (after, b'new')):
        folder.mkdir()
        (folder / 'DLSS5Tool.exe').write_bytes(content)
        shutil.copyfile(args.helper, folder / delta.HELPER)
        (folder / '_internal').mkdir()
        (folder / '_internal/unchanged.dll').write_bytes(b'unchanged dependency')
    package = build(before, after, output / 'payload', 'v9.0.0', 'v9.0.1', 'lite')
    install = output / 'installed'
    shutil.copytree(before, install)
    (install / 'dlss5_settings.json').write_bytes(b'keep settings')
    data = package.read_bytes()
    record = delta.file_record(package)
    asset = updater.ReleaseAsset(package.name,
        f'https://github.com/{updater.GITHUB_REPOSITORY}/releases/download/v9.0.1/{package.name}',
        len(data), 'sha256:' + record['sha256'])
    delta.prepare(install, asset, 'v9.0.0', 'v9.0.1', 'lite', opener=lambda *a, **k: io.BytesIO(data))
    parent = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(2)'])
    try:
        result = subprocess.run([str(install / delta.TRANSACTION / delta.HELPER),
            '--root', str(install), '--parent-pid', str(parent.pid), '--quiet'],
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
    finally:
        parent.wait(timeout=10)
    assert result.returncode == 0, delta.status(install)
    assert delta.status(install)['phase'] == 'complete'
    assert (install / 'DLSS5Tool.exe').read_bytes() == b'new'
    assert (install / 'dlss5_settings.json').read_bytes() == b'keep settings'
    assert (install / delta.TRANSACTION / 'backup/DLSS5Tool.exe').read_bytes() == b'old'
    report = {'frozen_helper_sha256': delta.file_record(args.helper)['sha256'],
              'exit_code': result.returncode, 'state': delta.status(install),
              'settings_preserved': True, 'backup_verified': True,
              'scope': 'Synthetic files; not full app/GPU/clean-machine acceptance'}
    delta.write_json(output / 'report.json', report)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
