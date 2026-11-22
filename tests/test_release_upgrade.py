"""Small transaction fixtures for the real-release verification harness."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dlss5tool import delta_update as delta
from scripts.build_file_update import build
from scripts.verify_release_upgrade import verify_upgrade


def endpoints(tmp_path):
    before, after = tmp_path / 'before', tmp_path / 'after'
    for directory, content in ((before, b'old'), (after, b'new')):
        directory.mkdir()
        (directory / 'DLSS5Tool.exe').write_bytes(content)
        (directory / delta.HELPER).write_bytes(content + b'helper')
        (directory / '_internal').mkdir()
        (directory / '_internal/unchanged.dll').write_bytes(b'unchanged')
    asset = build(before, after, tmp_path / 'payload', 'v3.0.1', 'v3.0.2', 'lite')
    return before, after, asset


def simulated_helper(command, **kwargs):
    install = Path(command[command.index('--root') + 1])
    with delta.transaction_lock(install):
        delta.apply_transaction(install)
    return SimpleNamespace(returncode=0)


def test_real_endpoint_verifier_checks_old_helper_backup_and_user_files(tmp_path):
    before, after, asset = endpoints(tmp_path)
    with patch('scripts.verify_release_upgrade.subprocess.Popen', return_value=Mock(pid=123)), \
         patch('scripts.verify_release_upgrade.subprocess.run', side_effect=simulated_helper):
        report = verify_upgrade(before, after, asset, 'lite', tmp_path / 'probe')
    assert report['state']['phase'] == 'complete'
    assert report['old_frozen_helper'] == delta.file_record(before / delta.HELPER)
    assert report['managed_target_files_verified'] == 3
    assert report['rollback_backup_files_verified'] == 2
    assert len(report['user_files_preserved']) == 4
    assert not report['resumed_ready_transaction']


def test_only_ready_unchanged_probe_can_resume_after_bootloader_failure(tmp_path):
    before, after, asset = endpoints(tmp_path)
    output = tmp_path / 'probe'
    with patch('scripts.verify_release_upgrade.subprocess.Popen', return_value=Mock(pid=123)), \
         patch('scripts.verify_release_upgrade.subprocess.run', return_value=SimpleNamespace(returncode=-1)):
        with pytest.raises(ValueError, match='Old frozen helper failed'):
            verify_upgrade(before, after, asset, 'lite', output)
    assert delta.status(output / 'installed')['phase'] == 'ready'
    with patch('scripts.verify_release_upgrade.subprocess.Popen', return_value=Mock(pid=123)), \
         patch('scripts.verify_release_upgrade.subprocess.run', side_effect=simulated_helper):
        report = verify_upgrade(before, after, asset, 'lite', output, resume_ready=True)
    assert report['resumed_ready_transaction']
    with pytest.raises(ValueError, match='Only an unchanged ready transaction'):
        verify_upgrade(before, after, asset, 'lite', output, resume_ready=True)


def test_release_verifier_separates_documents_from_required_installation_integrity(tmp_path):
    before, after, _ = endpoints(tmp_path)
    for directory, revision in ((before, b'old'), (after, b'new')):
        (directory / 'README.md').write_bytes(revision + b' official readme')
        (directory / 'mods').mkdir()
        (directory / 'mods/README.md').write_bytes(revision + b' mod instructions')
    asset = build(before, after, tmp_path / 'document-payload', 'v3.0.1', 'v3.0.2', 'lite')
    output = tmp_path / 'used-install'
    with patch('scripts.verify_release_upgrade.subprocess.Popen', return_value=Mock(pid=123)), \
         patch('scripts.verify_release_upgrade.subprocess.run', side_effect=simulated_helper):
        report = verify_upgrade(before, after, asset, 'lite', output, case='used-install')
    assert report['state']['phase'] == 'complete'
    assert report['managed_target_files_verified'] == 3
    assert report['rollback_backup_files_verified'] == 2
    assert report['local_documents_preserved'] == {'mods/README.md': 'missing', 'README.md': 'modified'}
    assert (output / 'installed/README.md').read_bytes() == b'local verification notes\n'
    assert not (output / 'installed/mods/README.md').exists()


def test_release_verifier_requires_a_recorded_executable_rejection(tmp_path):
    before, after, asset = endpoints(tmp_path)

    def rejected_helper(command, **kwargs):
        with pytest.raises(delta.DeltaError, match=r'no files replaced: DLSS5Tool\.exe'):
            simulated_helper(command, **kwargs)
        return SimpleNamespace(returncode=1)

    with patch('scripts.verify_release_upgrade.subprocess.Popen', return_value=Mock(pid=123)), \
         patch('scripts.verify_release_upgrade.subprocess.run', side_effect=rejected_helper):
        report = verify_upgrade(before, after, asset, 'lite', tmp_path / 'damaged', case='damaged-executable')
    assert report['state']['phase'] == 'failed'
    assert report['source_files_verified_unchanged'] == 3
    assert not report['replacement_journal_created'] and not report['rollback_backup_created']
    assert (before / 'DLSS5Tool.exe').read_bytes() == b'old'

    with patch('scripts.verify_release_upgrade.subprocess.Popen', return_value=Mock(pid=123)), \
         patch('scripts.verify_release_upgrade.subprocess.run', return_value=SimpleNamespace(returncode=1)):
        with pytest.raises(ValueError, match='Expected old helper to reject the damaged executable'):
            verify_upgrade(before, after, asset, 'lite', tmp_path / 'unreported', case='damaged-executable')
