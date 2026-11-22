"""Install a real release delta with the OLD frozen helper in a fresh task copy.

Never modifies either release endpoint or removes the verification copy. Run one
edition at a time, archive the small report, then clean only that task copy.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dlss5tool import delta_update as delta, updater
from scripts.verify_update_overlay import verify


VERIFY_CASES = ('clean', 'used-install', 'damaged-executable')
EXECUTABLE = 'DLSS5Tool.exe'


def _verify_installed_files(install, expected, absent, *, runtime_only=False):
    count = 0
    for name, record in expected.items():
        if runtime_only and delta.is_document(name):
            continue
        if delta.file_record(delta.safe_path(install, name)) != record:
            raise ValueError(f'Installed file differs from expected bytes: {name}')
        count += 1
    for name in absent:
        if runtime_only and delta.is_document(name):
            continue
        if delta.safe_path(install, name).exists():
            raise ValueError(f'Unexpected installed file: {name}')
    return count


def verify_upgrade(before, after, asset_path, edition, output, *, resume_ready=False, case='clean'):
    if case not in VERIFY_CASES or (resume_ready and case != 'clean'):
        raise ValueError('Choose a verification case; only the clean case can resume a ready transaction')
    before, after, asset_path, output = (
        Path(path).absolute() for path in (before, after, asset_path, output))
    for path in (before, after, asset_path, output):
        delta.safe_path(path.parent, path.name)
        if path.resolve() != path:
            raise ValueError(f'Use a resolved absolute path without redirects: {path}')
    if ((output.exists() and not resume_ready)
            or any(output.is_relative_to(path) for path in (before, after))):
        raise ValueError('Use a NEW verification task directory outside both releases')
    if not output.parent.is_dir():
        raise ValueError('Register and create the parent task directory first')
    # Validate parent links too, before creating or copying any large files.
    delta.safe_path(output.parent, output.name)
    print('Verifying real payload and both release endpoints...', flush=True)
    overlay = verify(before, after, asset_path, edition)
    with zipfile.ZipFile(asset_path) as bundle:
        manifest = delta.validate_manifest(json.loads(bundle.read('manifest.json')),
                                           edition=edition)
    changed = delta.changes(manifest)
    expected_peak = (sum(record['size'] for record in manifest['before'].values())
                     + asset_path.stat().st_size
                     + sum(manifest['after'][name]['size'] for name in changed
                           if name in manifest['after'])
                     + sum(manifest['before'][name]['size'] for name in changed
                           if name in manifest['before'])
                     + max((record['size'] for record in manifest['after'].values()), default=0))
    free = shutil.disk_usage(output.parent).free
    print(json.dumps({'estimated_peak_bytes': expected_peak, 'free_bytes': free}), flush=True)
    if free < expected_peak + 15 * 1024**3:
        raise ValueError('Need verification peak + 15 GiB free space')
    install = output / 'installed'
    if resume_ready:
        delta.safe_path(output, 'installed')
        if (delta.status(install) or {}).get('phase') != 'ready':
            raise ValueError('Only an unchanged ready transaction can be resumed')
        if delta.read_json(install / delta.TRANSACTION / 'manifest.json') != manifest:
            raise ValueError('Prepared manifest differs from this real payload')
        delta.check_baseline(install, manifest)
    else:
        output.mkdir()
        print('Copying the verified old release (no hardlinks/junctions)...', flush=True)
        shutil.copytree(before, install)
    local_documents = {}
    if case == 'used-install':
        local_documents = {'mods/README.md': None, 'README.md': b'local verification notes\n'}
        for name, content in local_documents.items():
            if name not in manifest['before']:
                raise ValueError(f'Expected an official source document: {name}')
            path = delta.safe_path(install, name)
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
    # These are unmanaged user files; no private user input is used.
    user_files = {
        'dlss5_settings.json': b'{"verification":true}',
        'dlss5_queue.json': b'{"jobs":[]}',
        'mods/nvngx_dlssnr.dll': b'custom external runtime verification marker',
        'mods/custom-verification.txt': b'keep custom mods',
    }
    for name, content in user_files.items():
        if name in manifest['before'] or name in manifest['after']:
            raise ValueError('Verification marker conflicts with a managed file')
        path = delta.safe_path(install, name)
        if resume_ready:
            if path.read_bytes() != content:
                raise ValueError('Existing verification marker changed')
            continue
        if path.exists():
            raise ValueError('Old release unexpectedly contains user files')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    record = delta.file_record(asset_path)
    asset = updater.ReleaseAsset(asset_path.name,
        f'https://github.com/{updater.GITHUB_REPOSITORY}/releases/download/'
        f'{manifest["to"]}/{asset_path.name}', record['size'], 'sha256:' + record['sha256'])
    if resume_ready:
        if delta.file_record(install / delta.TRANSACTION / 'package.dlssupdate') != record:
            raise ValueError('Ready transaction contains different payload bytes')
    else:
        print('Preparing the real update; validating its SHA256...', flush=True)
        # Simulate an HTTP body with the actual local payload. Download logic still
        # checks exact digest and size; no asset hardlink needs to be trusted.
        delta.prepare(install, asset, manifest['from'], manifest['to'], edition,
                      opener=lambda *args, **kwargs: asset_path.open('rb'))
    helper = install / delta.TRANSACTION / delta.HELPER
    if delta.file_record(helper) != manifest['before'][delta.HELPER]:
        raise ValueError('Verification MUST execute the old installed frozen helper')
    helper_record = delta.file_record(helper)
    applied_changes = delta.installation_changes(install, manifest)
    if case == 'damaged-executable':
        executable = delta.safe_path(install, EXECUTABLE)
        # Change the copied binary after preparation; the verifier never runs it.
        with executable.open('ab') as handle:
            handle.write(b'\nrelease verification: damaged executable\n')
        damaged_record = delta.file_record(executable)
    child_env = dict(os.environ)
    child_env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    child_env.pop('PYTHONPATH', None)
    child_env.pop('PYTHONHOME', None)
    # Preserve the normal Windows TEMP/TMP. Relocating the bootloader's private
    # extraction directory changes the runtime/security conditions being tested.
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    parent = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(2)'],
                              creationflags=flags)
    print('Running the OLD frozen update helper...', flush=True)
    try:
        result = subprocess.run([str(helper), '--root', str(install),
                                 '--parent-pid', str(parent.pid), '--quiet'],
                                cwd=install, env=child_env, creationflags=flags, timeout=300)
    finally:
        parent.wait(timeout=10)
    state = delta.status(install)
    transaction = delta.transaction_path(install)
    if case == 'damaged-executable':
        expected_error = f'Installed file missing or modified; no files replaced: {EXECUTABLE}'
        if (result.returncode != 1 or not state or state.get('phase') != 'failed'
                or state.get('error') != expected_error):
            raise ValueError(f'Expected old helper to reject the damaged executable: exit={result.returncode}, state={state}')
        if any(delta.safe_path(transaction, name).exists() for name in ('backup', 'journal.json')):
            raise ValueError('Rejected update started an installation transaction')
        expected = {**manifest['before'], EXECUTABLE: damaged_record}
        outcome = {
            'source_files_verified_unchanged': _verify_installed_files(
                install, expected, manifest['after'].keys() - expected.keys()),
            'damaged_executable': damaged_record,
            'replacement_journal_created': False,
            'rollback_backup_created': False,
        }
    else:
        if result.returncode != 0 or not state or state.get('phase') != 'complete':
            raise ValueError(f'Old frozen helper failed: exit={result.returncode}, state={state}')
        runtime_count = _verify_installed_files(
            install, manifest['after'], manifest['before'].keys() - manifest['after'].keys(),
            runtime_only=True)
        backup_count = 0
        for name in applied_changes:
            if name in manifest['before']:
                backup = delta.safe_path(transaction, 'backup/' + name)
                if delta.file_record(backup) != manifest['before'][name]:
                    raise ValueError(f'Rollback backup differs from old release: {name}')
                backup_count += 1
        for name, content in local_documents.items():
            document = delta.safe_path(install, name)
            actual = document.read_bytes() if document.exists() else None
            if actual != content or delta.safe_path(transaction, 'backup/' + name).exists():
                raise ValueError(f'Local document was not left alone: {name}')
        outcome = {
            'managed_target_files_verified': runtime_count,
            'documents_excluded_from_installation_integrity': [n for n in manifest['after'] if delta.is_document(n)],
            'rollback_backup_files_verified': backup_count,
            'local_documents_preserved': {name: 'missing' if content is None else 'modified'
                                          for name, content in local_documents.items()},
        }
    for name, content in user_files.items():
        if delta.safe_path(install, name).read_bytes() != content:
            raise ValueError(f'User file was not preserved: {name}')
    report = {**overlay, **outcome, 'case': case, 'old_frozen_helper': helper_record,
              'exit_code': result.returncode, 'state': state, 'user_files_preserved': list(user_files),
              'verification_install': str(install), 'clean_machine_verified': False,
              'resumed_ready_transaction': resume_ready,
              'helper_temp': child_env.get('TEMP'),
              'scope': 'Real version-pair delta exercised by old frozen helper in an isolated copy'}
    delta.write_json(output / 'report.json', report)
    print(json.dumps(report), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--asset', type=Path, required=True)
    parser.add_argument('--edition', choices=('lite', 'full'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-external-output', action='store_true')
    parser.add_argument('--case', choices=VERIFY_CASES, default='clean',
                        help='Use official files, delete/edit local documents, or damage the staged install executable')
    parser.add_argument('--resume-ready', action='store_true',
                        help='Retry this probe only if a previous helper failed before applying any file')
    args = parser.parse_args()
    if not args.allow_external_output and not args.output.absolute().is_relative_to(ROOT / 'tmp'):
        parser.error('Use a registered tmp task or explicitly allow external output')
    verify_upgrade(args.before, args.after, args.asset, args.edition, args.output,
                   resume_ready=args.resume_ready, case=args.case)
