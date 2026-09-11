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

    def test_modified_unchanged_dependency_aborts(self):
        self.put(self.install, '_internal/unchanged.dll', b'custom')
        with self.assertRaisesRegex(delta.DeltaError, 'modified'):
            self.prepare()
        self.assertEqual((self.install / 'DLSS5Tool.exe').read_bytes(), b'old exe')

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
