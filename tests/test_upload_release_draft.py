"""Exercise the upload entry point against a fake GitHub API; no network."""
import hashlib
import json
from pathlib import Path
import sys
import threading
from unittest import mock

import pytest

from scripts import upload_release_draft as upload


TAG = 'v3.1.2'
COMMIT = '1' * 40
NOTES_NAME = f'RELEASE_NOTES_{TAG}.md'
SUPPORT_NAMES = ('Join-ReleaseArchive.ps1', NOTES_NAME, 'SHA256SUMS.txt')


@pytest.fixture
def bundle(tmp_path):
    assets = tmp_path / 'assets'
    assets.mkdir()
    def record(name):
        data = name.encode('utf-8')
        (assets / name).write_bytes(data)
        return dict(name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest())
    lite = record(f'DLSS5Tool-{TAG}-win64.zip')
    editions = {'lite': {**lite, 'github_name': lite['name'], 'volumes': []}}
    for edition in ('full', 'addon'):
        editions[edition] = {'volumes': [record(f'DLSS5Tool-{TAG}-win64-{edition}.zip.{i:03}')
                                        for i in (1, 2)]}
    updates = [record(f'DLSS5Tool-v3.1.1-to-{TAG}-win64-{edition}.dlssupdate')
               for edition in ('lite', 'full')]
    package = dict(version=TAG, editions=editions, incremental_updates={'assets': updates})
    (assets / 'package-report.json').write_text(json.dumps(package), encoding='utf-8')
    notes = tmp_path / NOTES_NAME
    notes.write_text(f'# {TAG} Release Notes\n\nApproved user-facing download text.\n', encoding='utf-8')
    (assets / NOTES_NAME).write_bytes(notes.read_bytes())
    return assets, notes, package, tmp_path / 'upload-report.json'


class GitHub:
    def __init__(self, notes, existing=(), published=False):
        self.notes = notes
        self.assets = list(existing)
        self.exists = bool(existing) or published
        self.published = published
        self.uploaded = []
        self.created_body = None
        self.lock = threading.Lock()

    def release(self):
        return dict(id=123, tag_name=TAG, draft=not self.published, target_commitish=COMMIT,
                    html_url=f'https://github.com/example/project/releases/tag/{TAG}',
                    body=self.notes.read_text(encoding='utf-8'), assets=list(self.assets),
                    published_at='2026-10-28T00:00:00Z' if self.published else None)

    def command(self, *args):
        with self.lock:
            if args == ('git', 'rev-parse', 'HEAD'):
                return COMMIT + '\n'
            if args[:2] == ('gh', 'api'):
                if '/commits/' in args[2]:
                    return json.dumps({'sha': COMMIT})
                if args[2].endswith('/releases?per_page=100'):
                    return json.dumps([self.release()] if self.exists else [])
                if args[2].endswith('/releases/123'):
                    return json.dumps(self.release())
            if args[:3] == ('gh', 'release', 'create'):
                assert '--draft' in args
                self.exists = True
                self.created_body = Path(args[args.index('--notes-file') + 1]).read_text(encoding='utf-8')
                return ''
            if args[:3] == ('gh', 'release', 'upload'):
                path = Path(args[4])
                self.uploaded.append(path.name)
                self.assets.append(dict(id=len(self.assets) + 1, name=path.name,
                    size=path.stat().st_size, digest='sha256:' + upload.digest(path), state='uploaded'))
                return ''
            raise AssertionError(f'Unexpected external command: {args}')


def invoke(bundle, github):
    assets, notes, _, report = bundle
    arguments = ['upload_release_draft.py', '--repo', 'example/project', '--tag', TAG,
                 '--target', COMMIT, '--assets', str(assets), '--notes', str(notes), '--report', str(report)]
    with mock.patch.object(sys, 'argv', arguments), mock.patch.object(upload, 'command', side_effect=github.command):
        upload.main()
    return json.loads(report.read_text(encoding='utf-8'))


@pytest.mark.parametrize('support_files_present', [False, True])
def test_only_installation_payloads_and_both_updates_are_uploaded(bundle, support_files_present):
    assets, notes, package, _ = bundle
    if support_files_present:
        for name in ('Join-ReleaseArchive.ps1', 'SHA256SUMS.txt', 'README-UPLOAD.txt'):
            (assets / name).write_text('local support only', encoding='utf-8')
    github = GitHub(notes)
    report = invoke(bundle, github)
    assert len(github.uploaded) == 7
    assert set(github.uploaded) == set(upload.release_payloads(package))
    assert not set(SUPPORT_NAMES) & set(github.uploaded)
    assert sum(name.endswith('.dlssupdate') for name in github.uploaded) == 2
    assert github.created_body == notes.read_text(encoding='utf-8')
    assert report['status'] == 'complete' and report['notes_match']
    assert report['all_assets_sha256_and_size_verified'] and report['published'] is False


def test_existing_support_assets_are_preserved_and_verified_payloads_are_reused(bundle):
    _, notes, package, _ = bundle
    existing = [dict(name=name, size=record['size'], digest='sha256:' + record['sha256'], state='uploaded')
                for name, record in upload.release_payloads(package).items()]
    support = dict(name='SHA256SUMS.txt', size=999, digest='sha256:' + '0' * 64, state='uploaded')
    github = GitHub(notes, existing=[*existing, support])
    report = invoke(bundle, github)
    assert github.uploaded == [] and support in github.assets
    assert len(report['expected_assets']) == 7 and report['status'] == 'complete'


def test_corrupt_payload_still_fails_without_publishing_or_uploading(bundle):
    assets, notes, _, _ = bundle
    (assets / f'DLSS5Tool-{TAG}-win64.zip').write_bytes(b'changed')
    github = GitHub(notes)
    with pytest.raises(ValueError, match='Local asset mismatch'):
        invoke(bundle, github)
    assert not github.exists and not github.uploaded


def test_body_source_must_still_match_local_notes_copy(bundle):
    assets, notes, _, _ = bundle
    (assets / NOTES_NAME).write_text('different copy', encoding='utf-8')
    github = GitHub(notes)
    with pytest.raises(ValueError, match='differ from the source'):
        invoke(bundle, github)
    assert not github.exists and not github.uploaded


def test_published_release_is_not_modified(bundle):
    _, notes, _, _ = bundle
    github = GitHub(notes, published=True)
    with pytest.raises(ValueError, match='already published'):
        invoke(bundle, github)
    assert not github.uploaded and github.created_body is None
