"""Small synthetic end-to-end release update tests; no runtime dependencies."""
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import zipfile

from dlss5tool import delta_update as delta, updater
from scripts import build_file_update as builder


class Response(io.BytesIO):
    headers = {}


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.before = self.root / 'before'
        self.after = self.root / 'after'
        self.install = self.root / 'installed'
        self.before.mkdir()
        self.after.mkdir()
        files = {
            'DLSS5Tool.exe': b'old exe', 'DLSS5Update.exe': b'old helper',
            '_internal/unchanged.dll': b'large dependency reused',
            '_internal/nvngx_dlssnr.dll': b'bundled GPU runtime',
            '_internal/obsolete.dll': b'remove me',
            '_internal/change.dll': b'old dll', 'mods/README.md': b'instructions',
        }
        for name, data in files.items():
            self.put(self.before, name, data)
            self.put(self.after, name, data)
        self.put(self.after, 'DLSS5Tool.exe', b'new exe')
        self.put(self.after, 'DLSS5Update.exe', b'new helper')
        self.put(self.after, '_internal/change.dll', b'new dll')
        self.put(self.after, '_internal/added.dll', b'new file')
        (self.after / '_internal/obsolete.dll').unlink()
        shutil.copytree(self.before, self.install)
        self.put(self.install, 'settings.json', b'user settings')
        self.put(self.install, 'export_queue.json', b'user queue')
        self.put(self.install, 'mods/nvngx_dlssnr.dll', b'user GPU runtime')
        self.put(self.install, 'mods/models/custom.pth', b'user model')
        self.package = builder.build(self.before, self.after, self.root / 'output',
                                     'v3.0.0', 'v3.0.1', 'lite')

    @staticmethod
    def put(root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def asset(self, data=None):
        data = self.package.read_bytes() if data is None else data
        return updater.ReleaseAsset(self.package.name,
            f'https://github.com/{updater.GITHUB_REPOSITORY}/releases/download/v3.0.1/{self.package.name}',
            len(data), 'sha256:' + hashlib.sha256(data).hexdigest())

    def prepare(self, data=None, **kwargs):
        data = self.package.read_bytes() if data is None else data
        return delta.prepare(self.install, self.asset(data), 'v3.0.0', 'v3.0.1', 'lite',
                             opener=lambda *a, **k: Response(data), **kwargs)

    def manifest(self):
        with zipfile.ZipFile(self.package) as z:
            return json.loads(z.read('manifest.json'))

    def rewrite(self, manifest=None, extra=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(self.package) as original, zipfile.ZipFile(stream, 'w') as output:
            for name in original.namelist():
                data = json.dumps(manifest).encode() if name == 'manifest.json' and manifest else original.read(name)
                output.writestr(name, data)
            if extra:
                output.writestr(*extra)
        return stream.getvalue()

    def assert_baseline(self):
        for name, expected in builder.inventory(self.before, 'lite').items():
            self.assertEqual(delta.file_record(self.install / name), expected, name)
        self.assertFalse((self.install / '_internal/added.dll').exists())

    def test_end_to_end_only_changes_and_preserves_user_data(self):
        with zipfile.ZipFile(self.package) as z:
            self.assertNotIn('payload/_internal/unchanged.dll', z.namelist())
            self.assertNotIn('payload/_internal/obsolete.dll', z.namelist())
        self.prepare()
        self.assert_baseline()
        with delta.transaction_lock(self.install):
            delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'complete')
        for name, record in builder.inventory(self.after, 'lite').items():
            self.assertEqual(delta.file_record(self.install / name), record)
        self.assertFalse((self.install / '_internal/obsolete.dll').exists())
        self.assertEqual((self.install / 'settings.json').read_bytes(), b'user settings')
        self.assertEqual((self.install / 'mods/nvngx_dlssnr.dll').read_bytes(), b'user GPU runtime')
        self.assertEqual((self.install / 'mods/models/custom.pth').read_bytes(), b'user model')
        self.assertEqual((self.install / delta.TRANSACTION / 'backup/DLSS5Tool.exe').read_bytes(), b'old exe')

    def test_local_package_uses_official_digest_and_existing_install_flow(self):
        with mock.patch.object(updater, 'download_asset', side_effect=AssertionError('network download')):
            delta.prepare(self.install, self.asset(), 'v3.0.0', 'v3.0.1', 'lite',
                          local_file=self.package)
        self.assertEqual(delta.status(self.install)['phase'], 'ready')
        self.assertEqual((self.install / delta.TRANSACTION / 'package.dlssupdate').read_bytes(),
                         self.package.read_bytes())
        with delta.transaction_lock(self.install):
            delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'complete')
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'new exe')

    def test_local_package_rejects_changed_bytes_before_staging(self):
        changed = self.root / self.package.name
        changed.write_bytes(self.package.read_bytes()[:-1] + b'!')
        with self.assertRaisesRegex(delta.DeltaError, 'SHA-256'):
            delta.prepare(self.install, self.asset(), 'v3.0.0', 'v3.0.1', 'lite',
                          local_file=changed)
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'old exe')
        self.assertNotEqual(delta.status(self.install)['phase'], 'ready')

    def test_modified_unchanged_dependency_aborts(self):
        self.put(self.install, '_internal/unchanged.dll', b'custom')
        with self.assertRaisesRegex(delta.DeltaError, 'modified'):
            self.prepare()
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'old exe')

    def test_unchanged_readme_missing_or_modified_allows_lite_and_full_update(self):
        # Issue #14: the document is in both inventories, but not in payload.
        states = {'missing': None, 'empty': b'', 'edited': b'my notes',
                  'crlf': b'instructions\r\n'}
        for folder in (self.before, self.after):
            self.put(folder, 'mods/README.md', b'instructions\n')
        for edition in ('lite', 'full'):
            if edition == 'full':
                for folder in (self.before, self.after):
                    self.put(folder, 'mods/enhancement/guidance_worker.exe', b'worker')
            package = builder.build(self.before, self.after, self.root / f'docs-{edition}',
                                    'v3.0.0', 'v3.0.1', edition)
            self.package = package
            for state, content in states.items():
                with self.subTest(edition=edition, state=state):
                    install = self.root / f'{edition}-{state}'
                    shutil.copytree(self.before, install)
                    readme = install / 'mods/README.md'
                    if content is None:
                        readme.unlink()
                    else:
                        readme.write_bytes(content)
                    manifest = delta.prepare(install, self.asset(), 'v3.0.0', 'v3.0.1',
                                             edition, local_file=package)
                    self.assertNotIn('mods/README.md', delta.changes(manifest))
                    with delta.transaction_lock(install):
                        delta.apply_transaction(install)
                    self.assertEqual(delta.status(install)['phase'], 'complete')
                    self.assertEqual((install / 'DLSS5Tool.exe').read_bytes(), b'new exe')
                    self.assertEqual(readme.read_bytes() if readme.exists() else None, content)
                    self.assertFalse((install / delta.TRANSACTION / 'backup/mods/README.md').exists())

    def test_readme_removed_after_prepare_is_preserved(self):
        self.prepare()
        (self.install / 'mods/README.md').unlink()
        delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'complete')
        self.assertFalse((self.install / 'mods/README.md').exists())

    def test_readme_exception_does_not_allow_directories_or_hardlinks(self):
        path = self.install / 'mods/README.md'
        path.unlink()
        path.mkdir()
        with self.assertRaises(delta.DeltaError):
            delta.check_baseline(self.install, self.manifest())
        path.rmdir()
        os.link(self.install / 'settings.json', path)
        with self.assertRaisesRegex(delta.DeltaError, 'Hard-linked'):
            delta.check_baseline(self.install, self.manifest())

    def test_documents_are_not_baseline_requirements_and_dependencies_stay_strict(self):
        manifest = self.manifest()
        manifest['edition'] = 'full'
        worker = 'mods/enhancement/guidance_worker.exe'
        self.put(self.install, worker, b'worker')
        manifest['before'][worker] = manifest['after'][worker] = delta.file_record(self.install / worker)
        document_record = manifest['before']['mods/README.md']
        for name in ('README.md', 'README.en.md', 'CHANGELOG.md', 'mods/README.md',
                     'mods/enhancement/README.md', '_internal/GUIDANCE_PARAMETERS.md',
                     'licenses/README.md', 'THIRD_PARTY_NOTICES.md', 'LICENSE',
                     '_internal/realtime/licenses/DLSS5Tool-GPLv3.txt', 'mods/ADDON-INSTALL.txt'):
            with self.subTest(document=name):
                manifest['before'][name] = manifest['after'][name] = document_record
                self.put(self.install, name, b'my notes')
                self.assertNotIn(name, delta.check_baseline(self.install, manifest))
        for name in ('_internal/av.dist-info/entry_points.txt', '_internal/settings.txt',
                     '_internal/README.dll', 'licenses/License.json',
                     'mods/enhancement/enhancement.json', 'mods/models/official.pth'):
            with self.subTest(dependency=name):
                strict = {**manifest, 'before': dict(manifest['before']),
                          'after': dict(manifest['after'])}
                strict['before'][name] = strict['after'][name] = document_record
                self.put(self.install, name, b'changed dependency')
                with self.assertRaisesRegex(delta.DeltaError, name):
                    delta.check_baseline(self.install, strict)

    def test_official_readme_can_still_be_updated_and_backed_up(self):
        self.put(self.after, 'mods/README.md', b'new official instructions')
        self.package = builder.build(self.before, self.after, self.root / 'new-document',
                                     'v3.0.0', 'v3.0.1', 'lite')
        self.prepare()
        delta.apply_transaction(self.install)
        self.assertEqual((self.install / 'mods/README.md').read_bytes(), b'new official instructions')
        self.assertEqual((self.install / delta.TRANSACTION / 'backup/mods/README.md').read_bytes(),
                         b'instructions')

    def test_modified_readme_does_not_block_changed_or_removed_official_document(self):
        self.put(self.install, 'mods/README.md', b'my notes')
        for operation in ('replace', 'remove'):
            with self.subTest(operation=operation):
                manifest = self.manifest()
                if operation == 'replace':
                    manifest['after']['mods/README.md'] = manifest['after']['DLSS5Tool.exe']
                else:
                    del manifest['after']['mods/README.md']
                delta.check_baseline(self.install, manifest)
                self.assertNotIn('mods/README.md', delta.installation_changes(self.install, manifest))

    def test_readme_changed_during_apply_does_not_roll_back_application(self):
        self.put(self.install, 'mods/README.md', b'my notes')
        self.prepare()
        original = delta._replace_copy

        def edit_document(source, destination, scratch):
            original(source, destination, scratch)
            if destination.name == 'DLSS5Tool.exe' and 'payload' in str(source):
                self.put(self.install, 'mods/README.md', b'edited during update')

        with mock.patch.object(delta, '_replace_copy', side_effect=edit_document):
            delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'complete')
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'new exe')
        self.assertEqual((self.install / 'mods/README.md').read_bytes(), b'edited during update')

    def test_unchanged_local_document_is_never_hashed_for_install_verification(self):
        readme = self.install / 'mods/README.md'
        original = delta.file_record

        def reject_document_check(path):
            if Path(path) == readme:
                raise AssertionError('Local README entered mandatory integrity verification')
            return original(path)

        with mock.patch.object(delta, 'file_record', side_effect=reject_document_check):
            self.prepare()
            delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'complete')

    def test_changed_and_removed_documents_preserve_local_states_end_to_end(self):
        self.put(self.after, 'mods/README.md', b'new official instructions')
        self.put(self.before, 'licenses/obsolete-LICENSE.txt', b'old license')
        self.put(self.after, 'licenses/new-LICENSE.txt', b'new license')
        self.package = builder.build(self.before, self.after, self.root / 'changed-documents',
                                     'v3.0.0', 'v3.0.1', 'lite')
        for state, content in {'missing': None, 'empty': b'', 'edited': b'my notes',
                               'crlf': b'instructions\r\n'}.items():
            with self.subTest(state=state):
                install = self.root / f'changed-docs-{state}'
                shutil.copytree(self.before, install)
                readme = install / 'mods/README.md'
                if content is None:
                    readme.unlink()
                else:
                    readme.write_bytes(content)
                self.put(install, 'licenses/obsolete-LICENSE.txt', b'my old license notes')
                self.put(install, 'licenses/new-LICENSE.txt', b'my colliding document')
                delta.prepare(install, self.asset(), 'v3.0.0', 'v3.0.1', 'lite', local_file=self.package)
                delta.apply_transaction(install)
                self.assertEqual(delta.status(install)['phase'], 'complete')
                self.assertEqual((install / 'DLSS5Tool.exe').read_bytes(), b'new exe')
                self.assertEqual(readme.read_bytes() if readme.exists() else None, content)
                self.assertEqual((install / 'licenses/obsolete-LICENSE.txt').read_bytes(), b'my old license notes')
                self.assertEqual((install / 'licenses/new-LICENSE.txt').read_bytes(), b'my colliding document')

    def test_corrupt_document_payload_still_fails_before_program_replacement(self):
        self.put(self.after, 'mods/README.md', b'new official instructions')
        self.package = builder.build(self.before, self.after, self.root / 'document-payload',
                                     'v3.0.0', 'v3.0.1', 'lite')
        self.put(self.install, 'mods/README.md', b'my notes')
        self.prepare()
        self.put(self.install / delta.TRANSACTION, 'payload/mods/README.md', b'tampered')
        with self.assertRaisesRegex(delta.DeltaError, 'Staged payload modified'):
            delta.apply_transaction(self.install)
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'old exe')

    def test_document_classification_never_exempts_dependency_metadata(self):
        for name in ('_internal/pkg.dist-info/entry_points.txt', '_internal/pkg.dist-info/top_level.txt',
                     '_internal/config.txt', '_internal/licenses/README.dll',
                     '_internal/licenses/AUTHORS.py', 'licenses/LICENSE.json',
                     'mods/models/LICENSE.pth', 'mods/enhancement/README.exe'):
            with self.subTest(name=name):
                self.assertFalse(delta.is_document(name))

    def test_changed_document_read_or_write_failure_does_not_stop_core_update(self):
        self.put(self.after, 'mods/README.md', b'new official instructions')
        self.package = builder.build(self.before, self.after, self.root / 'locked-document',
                                     'v3.0.0', 'v3.0.1', 'lite')
        template = self.install
        for failure in ('read', 'write'):
            with self.subTest(failure=failure):
                self.install = self.root / f'locked-document-{failure}'
                shutil.copytree(template, self.install)
                self.prepare()
                record, replace = delta.file_record, delta._replace_copy

                def locked_read(path):
                    if Path(path) == self.install / 'mods/README.md':
                        raise PermissionError('document opened exclusively')
                    return record(path)

                def locked_write(source, destination, scratch):
                    if destination == self.install / 'mods/README.md':
                        raise PermissionError('document is read-only')
                    return replace(source, destination, scratch)

                with mock.patch.object(delta, 'file_record', side_effect=locked_read if failure == 'read' else record), \
                     mock.patch.object(delta, '_replace_copy', side_effect=locked_write if failure == 'write' else replace):
                    delta.apply_transaction(self.install)
                self.assertEqual(delta.status(self.install)['phase'], 'complete')
                self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'new exe')
                self.assertEqual((self.install / 'mods/README.md').read_bytes(), b'instructions')

    def test_document_edited_between_selection_and_backup_is_preserved(self):
        self.put(self.after, 'mods/README.md', b'new official instructions')
        self.package = builder.build(self.before, self.after, self.root / 'document-race',
                                     'v3.0.0', 'v3.0.1', 'lite')
        self.prepare()
        original = delta.installation_changes

        def select_then_edit(root, manifest):
            selected = original(root, manifest)
            self.put(self.install, 'mods/README.md', b'notes edited after selection')
            return selected

        with mock.patch.object(delta, 'installation_changes', side_effect=select_then_edit):
            delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'complete')
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'new exe')
        self.assertEqual((self.install / 'mods/README.md').read_bytes(), b'notes edited after selection')

    def test_changed_document_recovers_after_process_interruption(self):
        self.put(self.after, 'mods/README.md', b'new official instructions')
        self.package = builder.build(self.before, self.after, self.root / 'document-recovery',
                                     'v3.0.0', 'v3.0.1', 'lite')
        self.prepare()
        original = delta._replace_copy

        def interrupted(source, destination, scratch):
            original(source, destination, scratch)
            if destination == self.install / 'mods/README.md':
                raise KeyboardInterrupt('process died after document write')

        with mock.patch.object(delta, '_replace_copy', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'applying')
        delta.apply_transaction(self.install, recover=True)
        self.assertEqual(delta.status(self.install)['phase'], 'rolled_back')
        self.assert_baseline()

    def test_locked_document_during_rollback_does_not_block_runtime_recovery(self):
        self.put(self.before, 'README.md', b'old readme')
        self.put(self.after, 'README.md', b'new readme')
        self.put(self.install, 'README.md', b'old readme')
        self.package = builder.build(self.before, self.after, self.root / 'locked-rollback-doc',
                                     'v3.0.0', 'v3.0.1', 'lite')
        template = self.install
        for failure in ('read', 'write'):
            with self.subTest(failure=failure):
                self.install = self.root / f'locked-rollback-{failure}'
                shutil.copytree(template, self.install)
                self.prepare()
                original_replace = delta._replace_copy
                original_record = delta.file_record
                in_rollback = [False]
                hit_doc_failure = [False]

                def fail_after_doc(source, destination, scratch):
                    if not in_rollback[0] and destination == self.install / '_internal/change.dll':
                        in_rollback[0] = True
                        raise PermissionError('runtime locked')
                    if in_rollback[0] and failure == 'write' and destination == self.install / 'README.md':
                        hit_doc_failure[0] = True
                        raise PermissionError('document locked during rollback')
                    return original_replace(source, destination, scratch)

                def fail_doc_read(path):
                    if in_rollback[0] and failure == 'read' and Path(path) == self.install / 'README.md':
                        hit_doc_failure[0] = True
                        raise PermissionError('document unreadable during rollback')
                    return original_record(path)

                with mock.patch.object(delta, '_replace_copy', side_effect=fail_after_doc), \
                     mock.patch.object(delta, 'file_record', side_effect=fail_doc_read):
                    with self.assertRaises(PermissionError):
                        delta.apply_transaction(self.install)
                self.assertTrue(hit_doc_failure[0])
                self.assertEqual(delta.status(self.install)['phase'], 'rolled_back')
                self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'old exe')
                self.assertEqual((self.install / '_internal/change.dll').read_bytes(), b'old dll')

    def test_legacy_internal_runtime_is_preserved_with_or_without_mods(self):
        for has_mods in (True, False):
            with self.subTest(has_mods=has_mods):
                if not has_mods:
                    delta.discard_transaction(self.install)
                    shutil.rmtree(self.install)
                    shutil.copytree(self.before, self.install)
                custom = b'custom legacy runtime with a different size'
                self.put(self.install, delta.LEGACY_RUNTIME, custom)
                manifest = self.prepare()
                self.assertNotIn(delta.LEGACY_RUNTIME, delta.changes(manifest))
                with delta.transaction_lock(self.install):
                    delta.apply_transaction(self.install)
                self.assertEqual(delta.status(self.install)['phase'], 'complete')
                self.assertEqual((self.install / delta.LEGACY_RUNTIME).read_bytes(), custom)
                for name, record in manifest['after'].items():
                    if name != delta.LEGACY_RUNTIME:
                        self.assertEqual(delta.file_record(self.install / name), record)
                self.assertFalse((self.install / delta.TRANSACTION / 'backup' / delta.LEGACY_RUNTIME).exists())
                if has_mods:
                    self.assertEqual((self.install / 'mods/nvngx_dlssnr.dll').read_bytes(), b'user GPU runtime')

    def test_legacy_runtime_exception_does_not_weaken_other_files(self):
        self.put(self.install, delta.LEGACY_RUNTIME, b'custom runtime')
        self.put(self.install, '_internal/unchanged.dll', b'custom dependency')
        with self.assertRaisesRegex(delta.DeltaError, 'unchanged.dll'):
            self.prepare()

    def test_missing_empty_or_directory_runtime_is_not_tolerated(self):
        runtime = self.install / delta.LEGACY_RUNTIME
        runtime.unlink()
        for kind in ('missing', 'empty', 'directory'):
            with self.subTest(kind=kind):
                if kind == 'empty':
                    runtime.touch()
                elif kind == 'directory':
                    runtime.unlink()
                    runtime.mkdir()
                with self.assertRaisesRegex(delta.DeltaError, 'nvngx_dlssnr.dll'):
                    delta.check_baseline(self.install, self.manifest())

    def test_custom_runtime_cannot_be_replaced_or_removed_by_update(self):
        self.put(self.install, delta.LEGACY_RUNTIME, b'custom runtime')
        for operation in ('replace', 'remove'):
            with self.subTest(operation=operation):
                manifest = self.manifest()
                if operation == 'replace':
                    manifest['after'][delta.LEGACY_RUNTIME] = manifest['after']['DLSS5Tool.exe']
                else:
                    del manifest['after'][delta.LEGACY_RUNTIME]
                with self.assertRaisesRegex(delta.DeltaError, 'nvngx_dlssnr.dll'):
                    delta.check_baseline(self.install, manifest)

    def test_official_runtime_can_still_be_updated(self):
        self.put(self.after, delta.LEGACY_RUNTIME, b'new official runtime')
        self.package = builder.build(self.before, self.after, self.root / 'runtime-output',
                                     'v3.0.0', 'v3.0.1', 'lite')
        self.prepare()
        delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'complete')
        self.assertEqual((self.install / delta.LEGACY_RUNTIME).read_bytes(), b'new official runtime')

    def test_legacy_runtime_hardlink_is_rejected(self):
        (self.install / delta.LEGACY_RUNTIME).unlink()
        os.link(self.install / 'mods/nvngx_dlssnr.dll', self.install / delta.LEGACY_RUNTIME)
        with self.assertRaisesRegex(delta.DeltaError, 'Hard-linked'):
            self.prepare()

    def test_runtime_changed_to_custom_after_prepare_is_preserved(self):
        self.prepare()
        self.put(self.install, delta.LEGACY_RUNTIME, b'custom after download')
        delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'complete')
        self.assertEqual((self.install / delta.LEGACY_RUNTIME).read_bytes(), b'custom after download')

    def test_missing_runtime_after_prepare_aborts_before_replacement(self):
        self.prepare()
        (self.install / delta.LEGACY_RUNTIME).unlink()
        with self.assertRaisesRegex(delta.DeltaError, 'nvngx_dlssnr.dll'):
            delta.apply_transaction(self.install)
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'old exe')

    def test_legacy_runtime_survives_rollback(self):
        custom = b'custom legacy runtime'
        self.put(self.install, delta.LEGACY_RUNTIME, custom)
        self.prepare()
        original = delta._replace_copy

        def fail_payload(source, destination, scratch):
            if destination.name == 'change.dll' and 'payload' in str(source):
                raise PermissionError('simulated failure')
            return original(source, destination, scratch)

        with mock.patch.object(delta, '_replace_copy', side_effect=fail_payload):
            with self.assertRaises(PermissionError):
                delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'rolled_back')
        self.assertEqual((self.install / delta.LEGACY_RUNTIME).read_bytes(), custom)
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'old exe')

    def test_runtime_changed_during_apply_is_detected(self):
        self.put(self.install, delta.LEGACY_RUNTIME, b'custom before apply')
        self.prepare()
        original = delta._replace_copy

        def change_runtime(source, destination, scratch):
            original(source, destination, scratch)
            if destination.name == 'DLSS5Tool.exe' and 'payload' in str(source):
                self.put(self.install, delta.LEGACY_RUNTIME, b'changed during apply')

        with mock.patch.object(delta, '_replace_copy', side_effect=change_runtime):
            with self.assertRaisesRegex(delta.DeltaError, 'Installed file verification failed'):
                delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'rolled_back')
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'old exe')
        self.assertEqual((self.install / delta.LEGACY_RUNTIME).read_bytes(), b'changed during apply')

    def test_new_file_collision_is_not_overwritten(self):
        self.put(self.install, '_internal/added.dll', b'local file')
        with self.assertRaisesRegex(delta.DeltaError, 'conflicts'):
            self.prepare()
        self.assertEqual((self.install / '_internal/added.dll').read_bytes(), b'local file')

    def test_missing_managed_file_aborts(self):
        (self.install / '_internal/unchanged.dll').unlink()
        with self.assertRaises(delta.DeltaError):
            self.prepare()

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
    def test_real_windows_file_lock_rolls_back_other_files(self):
        self.prepare()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self.install / '_internal/change.dll'),
                                    0x80000000, 3, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            with self.assertRaises(PermissionError):
                delta.apply_transaction(self.install)
        finally:
            kernel.CloseHandle(handle)
        self.assertEqual(delta.status(self.install)['phase'], 'rolled_back')
        self.assert_baseline()

    def test_baseline_changed_after_preparation_is_preserved(self):
        self.prepare()
        self.put(self.install, '_internal/change.dll', b'customized after download')
        with self.assertRaises(delta.DeltaError):
            delta.apply_transaction(self.install)
        self.assertEqual((self.install / '_internal/change.dll').read_bytes(), b'customized after download')
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'old exe')

    def test_corrupt_state_fails_closed(self):
        self.prepare()
        delta.write_json(self.install / delta.TRANSACTION / 'state.json', [])
        with self.assertRaises(delta.DeltaError):
            delta.status(self.install)
        with self.assertRaises(delta.DeltaError):
            delta.discard_transaction(self.install)

    def test_archive_duplicate_and_bad_payload_hash_are_rejected(self):
        manifest = self.manifest()
        manifest['after']['DLSS5Tool.exe']['sha256'] = '0' * 64
        with self.assertRaisesRegex(delta.DeltaError, 'hash mismatch'):
            self.prepare(self.rewrite(manifest=manifest))
        delta.discard_transaction(self.install)
        with self.assertWarns(UserWarning):
            data = self.rewrite(extra=('payload/DLSS5Tool.exe', b'bad'))
        with self.assertRaisesRegex(delta.DeltaError, 'Duplicate'):
            self.prepare(data)
        self.assert_baseline()

    def test_locked_destination_rolls_back_all_changes(self):
        self.prepare()
        original = delta._replace_copy
        failed = False

        def fail_once(source, destination, scratch):
            nonlocal failed
            if destination.name == 'change.dll' and not failed:
                failed = True
                raise PermissionError('simulated locked DLL')
            return original(source, destination, scratch)

        with mock.patch.object(delta, '_replace_copy', side_effect=fail_once):
            with self.assertRaises(PermissionError):
                delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'rolled_back')
        self.assert_baseline()

    def test_power_loss_journal_recovers(self):
        self.prepare()
        original = delta._replace_copy

        def interrupted(source, destination, scratch):
            original(source, destination, scratch)
            if destination.name == 'added.dll':
                raise KeyboardInterrupt('simulated abrupt process death')

        with mock.patch.object(delta, '_replace_copy', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'applying')
        with self.assertRaises(delta.DeltaError):
            delta.discard_transaction(self.install)
        delta.apply_transaction(self.install, recover=True)
        self.assert_baseline()

    def test_interrupted_wait_can_be_recovered_without_replacing_files(self):
        self.prepare()
        delta.set_state(self.install, 'waiting')
        delta.apply_transaction(self.install, recover=True)
        self.assertEqual(delta.status(self.install)['phase'], 'ready')
        self.assert_baseline()

    def test_rollback_failure_keeps_backups_for_retry(self):
        self.prepare()
        original = delta._replace_copy
        def fail_after_first(source, destination, scratch):
            if destination.name == 'DLSS5Tool.exe' and 'payload' in str(source):
                return original(source, destination, scratch)
            raise PermissionError('locked')
        with mock.patch.object(delta, '_replace_copy', side_effect=fail_after_first):
            with self.assertRaisesRegex(delta.DeltaError, 'Rollback incomplete'):
                delta.apply_transaction(self.install)
        self.assertEqual(delta.status(self.install)['phase'], 'recovery_needed')
        delta.apply_transaction(self.install, recover=True)
        self.assert_baseline()

    def test_stage_tampering_and_post_download_baseline_change_abort(self):
        self.prepare()
        self.put(self.install / delta.TRANSACTION, 'payload/DLSS5Tool.exe', b'tampered')
        with self.assertRaises(delta.DeltaError):
            delta.apply_transaction(self.install)
        self.assert_baseline()

    def test_cancel_before_apply_leaves_program_unchanged(self):
        with self.assertRaises(updater.DownloadCancelled):
            self.prepare(cancelled=lambda: True)
        self.assert_baseline()
        delta.discard_transaction(self.install)
        self.assertFalse(delta.transaction_path(self.install).exists())

    def test_insufficient_disk_space_does_not_create_transaction(self):
        with mock.patch.object(delta.shutil, 'disk_usage', return_value=mock.Mock(free=0)):
            with self.assertRaisesRegex(delta.DeltaError, 'space'):
                self.prepare()
        self.assertFalse(delta.transaction_path(self.install).exists())

    def test_malicious_paths_and_case_aliases(self):
        for path in ('../escape', 'C:/escape', '_internal/a:stream', '_internal/CON.txt',
                     '_internal/a.', '_internal/a\\b', '/_internal/a', 'settings.json',
                     'mods/nvngx_dlssnr.dll', 'mods/models/model.pth'):
            with self.subTest(path=path):
                manifest = self.manifest()
                manifest['after'][path] = manifest['after']['DLSS5Tool.exe']
                with self.assertRaises(delta.DeltaError):
                    delta.validate_manifest(manifest)
        manifest = self.manifest()
        manifest['after']['dlss5tool.exe'] = manifest['after']['DLSS5Tool.exe']
        with self.assertRaises(delta.DeltaError):
            delta.validate_manifest(manifest)

    def test_version_edition_and_negative_size_rejected(self):
        for field, value in [('from', 'v9.0.0'), ('to', 'v2.0.0'), ('edition', 'unknown'), ('schema', 99)]:
            manifest = self.manifest()
            manifest[field] = value
            with self.assertRaises(delta.DeltaError):
                delta.validate_manifest(manifest, 'v3.0.0', 'v3.0.1', 'lite')
        manifest = self.manifest()
        manifest['after']['DLSS5Tool.exe']['size'] = -1
        with self.assertRaises(delta.DeltaError):
            delta.validate_manifest(manifest)

    def test_extra_archive_member_rejected_without_extraction(self):
        with self.assertRaises(delta.DeltaError):
            self.prepare(self.rewrite(extra=('../escape', b'bad')))
        self.assert_baseline()
        self.assertFalse((self.root / 'escape').exists())

    def test_symlink_or_junction_and_hardlink_are_rejected(self):
        link = self.install / '_internal/linked.dll'
        os.link(self.before / '_internal/unchanged.dll', link)
        with self.assertRaisesRegex(delta.DeltaError, 'Hard-linked'):
            delta.safe_path(self.install, '_internal/linked.dll')
        with self.assertRaises(delta.DeltaError):
            delta.safe_path(self.install, '../outside')

    def test_selection_requires_exact_pair_edition_repo_and_digest(self):
        good = self.asset()
        release = updater.ReleaseInfo('v3.0.1', updater.RELEASES_URL, '', (good,))
        self.assertEqual(delta.select_asset(release, 'v3.0.0', 'lite'), good)
        self.assertIsNone(delta.select_asset(release, 'v2.0.0', 'lite'))
        self.assertIsNone(delta.select_asset(release, 'v3.0.0', 'full'))
        self.assertIsNone(updater.select_portable_asset(release))
        for asset in (updater.ReleaseAsset(good.name, good.download_url, good.size),
                      updater.ReleaseAsset(good.name, good.download_url.replace('banbanzhige', 'other'), good.size, good.digest)):
            bad = updater.ReleaseInfo(release.tag, release.page_url, '', (asset,))
            self.assertIsNone(delta.select_asset(bad, 'v3.0.0', 'lite'))

    def test_full_addon_update_reuses_models_and_updates_worker(self):
        for folder in (self.before, self.after, self.install):
            self.put(folder, 'mods/enhancement/guidance_worker.exe', b'worker old')
            self.put(folder, 'mods/models/official.pth', b'unchanged weights')
        self.put(self.after, 'mods/enhancement/guidance_worker.exe', b'worker new')
        self.package = builder.build(self.before, self.after, self.root / 'full-output',
                                     'v3.0.0', 'v3.0.1', 'full')
        self.assertEqual(delta.installed_edition(self.install), 'full')
        with zipfile.ZipFile(self.package) as z:
            self.assertNotIn('payload/mods/models/official.pth', z.namelist())
        data = self.package.read_bytes()
        delta.prepare(self.install, self.asset(), 'v3.0.0', 'v3.0.1', 'full',
                      opener=lambda *a, **k: Response(data))
        delta.apply_transaction(self.install)
        self.assertEqual((self.install / 'mods/enhancement/guidance_worker.exe').read_bytes(), b'worker new')
        self.assertEqual((self.install / 'mods/models/custom.pth').read_bytes(), b'user model')

    def test_full_addon_update_preserves_legacy_internal_runtime(self):
        self.put(self.install, delta.LEGACY_RUNTIME, b'legacy full runtime')
        self.test_full_addon_update_reuses_models_and_updates_worker()
        self.assertEqual(delta.status(self.install)['phase'], 'complete')
        self.assertEqual((self.install / delta.LEGACY_RUNTIME).read_bytes(), b'legacy full runtime')
        self.assertEqual((self.install / 'mods/nvngx_dlssnr.dll').read_bytes(), b'user GPU runtime')

    def test_external_mods_require_manual_update(self):
        with self.assertRaisesRegex(delta.DeltaError, 'Custom mods'):
            delta.installed_edition(self.install, {'mods_directory': str(self.root / 'external')})

    def test_digest_mismatch(self):
        data = self.package.read_bytes()
        asset = self.asset(data)
        with self.assertRaises(updater.UpdateError):
            delta.prepare(self.install, asset, 'v3.0.0', 'v3.0.1', 'lite',
                          opener=lambda *a, **k: Response(b'x' * len(data)))
        self.assert_baseline()


if __name__ == '__main__':
    unittest.main()
