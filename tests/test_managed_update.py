"""Managed update trust, persisted readiness, and opt-in UI boundaries."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
import zipfile

from dlss5tool import delta_update as delta, managed_update as managed, updater
from dlss5tool.managed_update_ui import ManagedUpdateUI
from dlss5tool.portable_layout import PortableLayout
from scripts.package_managed_core import write_manifest


class ManagedUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.layout = PortableLayout(self.root, self.root / 'current', 'DLSS5Tool', 'lite', 'win-lite')
        self.tag = 'v3.1.5'
        self.cache = self.root / 'packages'
        self.cache.mkdir()
        self.package = self.cache / 'DLSS5Tool-3.1.5-win-lite-full.nupkg'
        with zipfile.ZipFile(self.package, 'w') as bundle:
            bundle.writestr('lib/app/DLSS5Tool.exe', b'new executable fixture')
            bundle.writestr('lib/app/_internal/runtime.dll', b'new runtime fixture')
            bundle.writestr('lib/app/README.md', b'new docs')
            bundle.writestr('lib/app/sq.version', b'version metadata')
        metadata = self.root / managed.STATE_DIRECTORY
        metadata.mkdir()
        self.manifest_path = metadata / managed.manifest_name(self.tag, 'lite')
        self.manifest = write_manifest(self.package, self.tag, 'lite', 'DLSS5Tool', self.manifest_path)
        self.release = self.make_release()
        record = delta.file_record(self.package)
        self.target = SimpleNamespace(PackageId='DLSS5Tool', Version='3.1.5', Size=record['size'],
                                      SHA256=record['sha256'].upper(), FileName=self.package.name)
        self.update = SimpleNamespace(TargetFullRelease=self.target, IsDowngrade=False,
                                      DeltasToTarget=[SimpleNamespace(FileName='older-to-newer-delta.nupkg')])
        self.manager = mock.Mock()
        self.manager.get_app_id.return_value = 'DLSS5Tool'
        self.manager.get_is_portable.return_value = True
        self.manager.get_current_version.return_value = '1.0.0'
        self.manager.check_for_updates.return_value = self.update
        self.manager.get_update_pending_restart.return_value = self.target
        self.sdk = mock.Mock()
        self.sdk.UpdateManager.return_value = self.manager
        patch = mock.patch.object(managed.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * 1024**3))
        patch.start()
        self.addCleanup(patch.stop)

    def make_release(self):
        assets = []
        for path in (self.package, self.manifest_path):
            record = delta.file_record(path)
            assets.append(updater.ReleaseAsset(path.name,
                f'https://github.com/{updater.GITHUB_REPOSITORY}/releases/download/{self.tag}/{path.name}',
                record['size'], 'sha256:' + record['sha256']))
        return updater.ReleaseInfo(self.tag, updater.RELEASES_URL, '', tuple(assets))

    def session(self):
        return managed.Session(self.release, layout=self.layout, sdk=self.sdk)

    def rewrite_manifest(self, value):
        self.manifest_path.write_text(json.dumps(value), encoding='utf-8')
        self.release = self.make_release()

    def test_prepare_cross_version_pins_target_and_restores_ready_after_restart(self):
        session = self.session()
        ready = session.prepare()
        self.assertEqual(ready['target'], self.tag)
        self.assertEqual(ready['source_version'], '1.0.0')
        self.assertEqual(ready['phase'], 'ready')
        self.sdk.UpdateOptions.assert_called_once_with(False, 10, 'win-lite')
        self.manager.wait_exit_then_apply_updates.assert_not_called()
        restarted = self.session()
        self.assertIs(restarted.ready(), self.target)
        restarted.apply_on_exit(restart=True)
        self.manager.wait_exit_then_apply_updates.assert_called_once_with(self.target, silent=True, restart=True)

    def test_target_version_channel_identity_digest_drift_refused_before_download(self):
        for attribute, bad in (('Version', '3.1.6'), ('PackageId', 'OtherApp'),
                               ('SHA256', '0' * 64), ('FileName', 'another.nupkg'), ('Size', 1)):
            with self.subTest(attribute=attribute):
                before = getattr(self.target, attribute)
                setattr(self.target, attribute, bad)
                try:
                    with self.assertRaisesRegex(updater.UpdateError, 'target changed'):
                        self.session().prepare()
                    self.manager.download_updates.assert_not_called()
                finally:
                    setattr(self.target, attribute, before)

    def test_missing_or_foreign_official_digest_is_refused(self):
        for asset in (updater.ReleaseAsset(self.manifest_path.name, 'https://github.com/other/repo/file', 100, 'sha256:'+'a'*64),
                      updater.ReleaseAsset(self.manifest_path.name, self.release.assets[1].download_url, 100)):
            with self.subTest(asset=asset):
                self.release = updater.ReleaseInfo(self.tag, updater.RELEASES_URL, '', (asset,))
                with self.assertRaisesRegex(updater.UpdateError, 'trusted'):
                    self.session()

    def test_full_channel_requires_compatible_external_component(self):
        full = PortableLayout(self.root, self.root/'current', 'DLSS5Tool', 'full', 'win-full')
        self.layout = full
        self.manifest_path = self.manifest_path.parent / managed.manifest_name(self.tag, 'full')
        write_manifest(self.package, self.tag, 'full', 'DLSS5Tool', self.manifest_path)
        self.release = self.make_release()
        component = self.root/'mods/enhancement/enhancement.json'
        component.parent.mkdir(parents=True)
        component.write_text('{"id":"dlss5-guidance","protocol":2}')
        with self.assertRaisesRegex(updater.UpdateError, 'external full component'):
            self.session().prepare()
        self.manager.download_updates.assert_not_called()
        component.write_text('{"id":"dlss5-guidance","protocol":1}')
        self.session().prepare()
        self.sdk.UpdateOptions.assert_called_with(False, 10, 'win-full')
        self.assertEqual(json.loads(component.read_text())['protocol'], 1)

    def test_missing_space_leaves_native_cache_untouched(self):
        with mock.patch.object(managed.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(updater.UpdateError, 'space'):
                self.session().prepare()
        self.manager.download_updates.assert_not_called()

    def test_cancel_after_sdk_download_never_persists_or_schedules_install(self):
        canceled = [False]
        self.manager.download_updates.side_effect = lambda *a, **k: canceled.__setitem__(0, True)
        with self.assertRaises(updater.DownloadCancelled):
            self.session().prepare(cancelled=lambda: canceled[0])
        self.assertFalse((self.manifest_path.parent/'ready.json').exists())
        self.manager.wait_exit_then_apply_updates.assert_not_called()

    def test_downloaded_package_content_is_verified_even_when_container_recompressed(self):
        original = self.package.read_bytes()
        def recompress(*args, **kwargs):
            import io
            with zipfile.ZipFile(io.BytesIO(original)) as before, \
                    zipfile.ZipFile(self.package, 'w', compression=zipfile.ZIP_DEFLATED) as after:
                for name in before.namelist():
                    after.writestr(name, before.read(name))
        self.manager.download_updates.side_effect = recompress
        session = self.session()
        session.prepare()
        self.assertNotEqual(delta.file_record(self.package), self.manifest['full_record'])
        self.assertIs(session.ready(), self.target)

    def test_changed_downloaded_payload_is_refused(self):
        def corrupt(*args, **kwargs):
            with zipfile.ZipFile(self.package, 'w') as bundle:
                bundle.writestr('lib/app/DLSS5Tool.exe', b'tampered')
        self.manager.download_updates.side_effect = corrupt
        with self.assertRaisesRegex(updater.UpdateError, 'target files failed'):
            self.session().prepare()
        self.manager.wait_exit_then_apply_updates.assert_not_called()

    def test_ready_state_is_not_trusted_to_override_official_file_inventory(self):
        session = self.session()
        session.prepare()
        with zipfile.ZipFile(self.package, 'w') as bundle:
            bundle.writestr('lib/app/DLSS5Tool.exe', b'local tamper')
        ready_path = self.manifest_path.parent/'ready.json'
        ready = delta.read_json(ready_path)
        ready['cached_record'] = delta.file_record(self.package)
        delta.write_json(ready_path, ready)
        with self.assertRaisesRegex(updater.UpdateError, 'content changed'):
            session.apply_on_exit(restart=True)
        self.manager.wait_exit_then_apply_updates.assert_not_called()

    def test_manifest_cannot_request_models_or_user_data_in_core(self):
        for path in ('lib/app/mods/models/model.pth', 'lib/app/dlss5_settings.json', '../escape'):
            with self.subTest(path=path):
                value = {**self.manifest, 'files': {**self.manifest['files'], path: self.manifest['files']['lib/app/README.md']}}
                self.rewrite_manifest(value)
                with self.assertRaises(updater.UpdateError):
                    self.session().prepare()
                self.manager.download_updates.assert_not_called()

    def test_downgrade_and_implicit_startup_install_disabled(self):
        self.manager.get_current_version.return_value = '4.0.0'
        with self.assertRaises(updater.UpdateError):
            self.session()
        with mock.patch.object(managed, 'current_layout', return_value=self.layout), \
             mock.patch.object(managed.importlib, 'import_module', return_value=self.sdk):
            managed.startup()
        self.sdk.App.return_value.set_auto_apply_on_startup.assert_called_once_with(False)

    def test_legacy_startup_does_not_import_or_require_sdk(self):
        with mock.patch.object(managed, 'current_layout', return_value=None), \
             mock.patch.object(managed.importlib, 'import_module') as import_sdk:
            managed.startup()
        import_sdk.assert_not_called()


class ManagedUpdateInstallUiTests(unittest.TestCase):
    def setUp(self):
        self.app = ManagedUpdateUI()
        self.app._exporting = self.app._queue_running = self.app._diagnosing = self.app._switching_backend = False
        self.app._on_close = mock.Mock()
        self.app._managed_update_session = mock.Mock()
        self.app.root = mock.Mock()

    @staticmethod
    def immediate_thread(*args, **kwargs):
        thread = mock.Mock()
        thread.start.side_effect = kwargs['target']
        return thread

    def test_busy_or_declined_install_never_closes_or_schedules(self):
        for busy in (True, False):
            with self.subTest(busy=busy):
                self.app._exporting = busy
                with mock.patch('dlss5tool.managed_update_ui.messagebox.showinfo'), \
                     mock.patch('dlss5tool.managed_update_ui.messagebox.askyesno', return_value=False):
                    self.app._offer_managed_install()
                self.app._on_close.assert_not_called()
                self.app._managed_update_session.apply_on_exit.assert_not_called()

    def test_confirmation_only_requests_close_native_apply_waits_for_cleanup(self):
        with mock.patch('dlss5tool.managed_update_ui.messagebox.askyesno', return_value=True):
            self.app._offer_managed_install()
        self.app._on_close.assert_called_once()
        self.app._managed_update_session.apply_on_exit.assert_not_called()
        with mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread):
            self.assertFalse(self.app._schedule_managed_apply_after_cleanup())
        self.app.root.after.call_args.args[1]()
        self.app._managed_update_session.apply_on_exit.assert_called_once_with(restart=True)
        self.assertEqual(self.app._on_close.call_count, 2)
        self.assertTrue(self.app._schedule_managed_apply_after_cleanup())

    def test_apply_schedule_failure_keeps_window_open(self):
        self.app._managed_apply_on_close = True
        self.app._managed_update_session.apply_on_exit.side_effect = OSError('blocked')
        with mock.patch('dlss5tool.managed_update_ui.messagebox.showerror'), \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread):
            self.assertFalse(self.app._schedule_managed_apply_after_cleanup())
            self.app.root.after.call_args.args[1]()
        self.assertFalse(self.app._managed_apply_on_close)
        self.app._on_close.assert_not_called()
