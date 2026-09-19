import hashlib
from pathlib import Path
import subprocess
import io
import tarfile
from unittest import mock

import pytest

from scripts.prepare_gpu_export_materials import fetch, extract_notices


def test_fetch_validates_before_publishing(tmp_path):
    data = b'verified source'
    target = tmp_path / 'source.tar.gz'
    def download(command, **kwargs):
        assert '--retry-all-errors' in command
        Path(command[command.index('--output') + 1]).write_bytes(data)
    with mock.patch('scripts.prepare_gpu_export_materials.subprocess.run', side_effect=download):
        result = fetch('https://example.invalid/source', target, hashlib.sha256(data).hexdigest())
    assert result['sha256'] == hashlib.sha256(data).hexdigest()
    assert target.read_bytes() == data
    assert not list(tmp_path.glob('*.partial'))


@pytest.mark.parametrize('failure', ['transport', 'digest'])
def test_failed_fetch_never_publishes_partial(tmp_path, failure):
    target = tmp_path / 'source.tar.gz'
    unrelated = tmp_path / 'source.tar.gz.previous.partial'
    unrelated.write_bytes(b'preserve')
    def download(command, **kwargs):
        Path(command[command.index('--output') + 1]).write_bytes(b'bad')
        if failure == 'transport':
            raise subprocess.CalledProcessError(35, command)
    with mock.patch('scripts.prepare_gpu_export_materials.subprocess.run', side_effect=download):
        with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
            fetch('https://example.invalid/source', target, '0' * 64)
    assert not target.exists()
    assert list(tmp_path.glob('*.partial')) == [unrelated]
    assert unrelated.read_bytes() == b'preserve'


def test_header_only_sdk_preserves_embedded_license(tmp_path):
    source = tmp_path / 'nv-codec-headers.tar.gz'
    notice = b'/* Copyright Example\nPermission is hereby granted under these terms.\n*/\n'
    with tarfile.open(source, 'w:gz') as archive:
        payload = notice + b'int unrelated_code;'
        member = tarfile.TarInfo('sdk/include/api.h')
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    target = tmp_path / 'notices'
    records = extract_notices(source, target)
    assert len(records) == 1
    assert (target / records[0]['file']).read_bytes() == notice
