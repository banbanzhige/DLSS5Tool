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

    def test_startup_error_never_opens_modal_dialog_for_headless_workers(self):
        import io
        with mock.patch('dlss5tool.update_helper.notify') as notify:
            for flag in managed.HEADLESS_FLAGS:
                with self.subTest(flag=flag):
                    stderr = io.StringIO()
                    with mock.patch.object(managed.sys, 'stderr', stderr):
                        managed.report_startup_error(RuntimeError('layout broken'), [flag, '1'])
                    notify.assert_not_called()
                    self.assertIn('layout broken', stderr.getvalue())
            with mock.patch.object(managed.sys, 'stderr', None):
                managed.report_startup_error(RuntimeError('layout broken'), ['--adapter-probe'])
            notify.assert_not_called()
            managed.report_startup_error(RuntimeError('layout broken'), [])
            notify.assert_called_once_with('layout broken', error=True)

    def test_space_margin_scales_by_edition(self):
        with mock.patch.object(managed.shutil, 'disk_usage',
                               return_value=SimpleNamespace(free=2 * 1024**3)):
            self.session().prepare()
        with mock.patch.object(managed.shutil, 'disk_usage',
                               return_value=SimpleNamespace(free=512 * 1024**2)):
            with self.assertRaisesRegex(updater.UpdateError, 'space'):
                self.session().prepare()
        full = PortableLayout(self.root, self.root / 'current', 'DLSS5Tool', 'full', 'win-full')
        self.layout = full
        self.manifest_path = self.manifest_path.parent / managed.manifest_name(self.tag, 'full')
        write_manifest(self.package, self.tag, 'full', 'DLSS5Tool', self.manifest_path)
        self.release = self.make_release()
        component = self.root / 'mods/enhancement/enhancement.json'
        component.parent.mkdir(parents=True, exist_ok=True)
        component.write_text('{"id":"dlss5-guidance","protocol":1}')
        with mock.patch.object(managed.shutil, 'disk_usage',
                               return_value=SimpleNamespace(free=int(1.5 * 1024**3))):
            with self.assertRaisesRegex(updater.UpdateError, 'space'):
                self.session().prepare()
        with mock.patch.object(managed.shutil, 'disk_usage',
                               return_value=SimpleNamespace(free=3 * 1024**3)):
            self.session().prepare()

    def test_has_prepared_update_matches_persisted_ready_marker(self):
        self.assertFalse(managed.has_prepared_update(self.layout, self.release))
        self.session().prepare()
        self.assertTrue(managed.has_prepared_update(self.layout, self.release))
        other_release = updater.ReleaseInfo('v9.9.9', updater.RELEASES_URL, '', ())
        self.assertFalse(managed.has_prepared_update(self.layout, other_release))
        other_layout = PortableLayout(self.root, self.root / 'current', 'DLSS5Tool', 'full', 'win-full')
        self.assertFalse(managed.has_prepared_update(other_layout, self.release))

    def test_ready_rechecks_component_protocol_release_asset_and_cached_space(self):
        # Cached space excludes download double-copy overhead, while keeping unpack/rollback + margin.
        self.assertLess(
            managed.required_free_bytes(500 * 1024**2, 100 * 1024**2, 'lite', cached=True),
            managed.required_free_bytes(500 * 1024**2, 100 * 1024**2, 'lite', cached=False),
        )
        full = PortableLayout(self.root, self.root / 'current', 'DLSS5Tool', 'full', 'win-full')
        self.layout = full
        self.manifest_path = self.manifest_path.parent / managed.manifest_name(self.tag, 'full')
        write_manifest(self.package, self.tag, 'full', 'DLSS5Tool', self.manifest_path)
        self.release = self.make_release()
        component = self.root / 'mods/enhancement/enhancement.json'
        component.parent.mkdir(parents=True, exist_ok=True)
        component.write_text('{"id":"dlss5-guidance","protocol":1}')
        self.session().prepare()

        # 1. External full component protocol drift or removal must block ready/apply.
        component.write_text('{"id":"dlss5-guidance","protocol":2}')
        with self.assertRaisesRegex(updater.UpdateError, 'external full component'):
            self.session().apply_on_exit(restart=True)
        component.unlink()
        with self.assertRaisesRegex(updater.UpdateError, 'external full component'):
            self.session().apply_on_exit(restart=True)
        component.write_text('{"id":"dlss5-guidance","protocol":1}')

        # 2. Removing the full package asset from the official Release must block ready/apply.
        manifest_only = updater.ReleaseInfo(self.tag, updater.RELEASES_URL, '', (self.release.assets[1],))
        with self.assertRaisesRegex(updater.UpdateError, 'trusted'):
            managed.Session(manifest_only, layout=self.layout, sdk=self.sdk).apply_on_exit(restart=True)

        # 3. Insufficient disk space for unpack/rollback + edition margin must block ready/apply.
        with mock.patch.object(managed.shutil, 'disk_usage',
                               return_value=SimpleNamespace(free=int(1.5 * 1024**3))):
            with self.assertRaisesRegex(updater.UpdateError, 'space'):
                self.session().apply_on_exit(restart=True)

        self.manager.wait_exit_then_apply_updates.assert_not_called()
        # Once prerequisites hold again, cached ready succeeds and can apply.
        self.session().apply_on_exit(restart=True)
        self.manager.wait_exit_then_apply_updates.assert_called_once_with(self.target, silent=True, restart=True)

    def test_prepare_space_budget_guarantees_immediate_ready_after_cache_write(self):
        # Compressed PyInstaller archives expand well beyond their .nupkg size.
        with zipfile.ZipFile(self.package, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr('lib/app/DLSS5Tool.exe', b'\x00' * (2 * 1024 * 1024))
            bundle.writestr('lib/app/sq.version', b'version metadata')
        self.manifest_path.unlink()
        self.manifest = write_manifest(self.package, self.tag, 'lite', 'DLSS5Tool', self.manifest_path)
        self.release = self.make_release()
        record = delta.file_record(self.package)
        self.target.Size = record['size']
        self.target.SHA256 = record['sha256'].upper()
        unpacked = sum(item['size'] for item in self.manifest['files'].values())
        self.assertGreater(unpacked, record['size'] * 10)

        for edition in ('lite', 'full'):
            prepare_need = managed.required_free_bytes(record['size'], unpacked, edition, cached=False)
            ready_need = managed.required_free_bytes(record['size'], unpacked, edition, cached=True)
            self.assertGreaterEqual(prepare_need - record['size'], ready_need)

        available = [managed.required_free_bytes(record['size'], unpacked, 'lite', cached=False)]

        def write_cache(*_args, **_kwargs):
            available[0] -= record['size'] + self.manifest_path.stat().st_size

        self.manager.download_updates.side_effect = write_cache
        with mock.patch.object(managed.shutil, 'disk_usage',
                               side_effect=lambda *_: SimpleNamespace(free=available[0])):
            session = self.session()
            session.prepare()
            self.assertIs(session.ready(), self.target)


class ManagedUpdateInstallUiTests(unittest.TestCase):
    def setUp(self):
        import threading
        self.app = ManagedUpdateUI()
        self.app._exporting = self.app._queue_running = self.app._diagnosing = self.app._switching_backend = False
        self.app._update_downloading = False
        self.app._update_cancel_event = threading.Event()
        self.app._update_action_labels = mock.Mock()
        self.app._delta_fallback = mock.Mock()
        self.app.logln = mock.Mock()
        self.app.set_status = mock.Mock()
        self.app._on_close = mock.Mock()
        self.app._finalize_window_close = mock.Mock()
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

    def test_confirmation_verifies_before_close_and_native_apply_waits_for_cleanup(self):
        with mock.patch('dlss5tool.managed_update_ui.messagebox.askyesno', return_value=True), \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread):
            self.app._offer_managed_install()
            self.app.root.after.call_args.args[1]()
        self.app._managed_update_session.ready.assert_called_once()
        self.app._on_close.assert_called_once()
        self.app._managed_update_session.apply_on_exit.assert_not_called()
        with mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread):
            self.assertFalse(self.app._schedule_managed_apply_after_cleanup())
        self.app.root.after.call_args.args[1]()
        self.app._managed_update_session.apply_on_exit.assert_called_once_with(restart=True)
        self.app._finalize_window_close.assert_called_once()
        self.assertTrue(self.app._schedule_managed_apply_after_cleanup())

    def test_preclose_verify_failure_keeps_intact_window_open_and_clears_session(self):
        self.app._managed_update_session.ready.side_effect = updater.UpdateError('cache changed')
        with mock.patch('dlss5tool.managed_update_ui.messagebox.askyesno', return_value=True), \
             mock.patch('dlss5tool.managed_update_ui.messagebox.showerror') as showerror, \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread):
            self.app._offer_managed_install()
            self.app.root.after.call_args.args[1]()
        showerror.assert_called_once()
        self.assertIsNone(self.app._managed_update_session)
        self.assertFalse(getattr(self.app, '_managed_apply_on_close', False))
        self.app._on_close.assert_not_called()
        self.app._finalize_window_close.assert_not_called()

    def test_postcleanup_apply_failure_reports_and_finalizes_close(self):
        self.app._managed_apply_on_close = True
        self.app._managed_update_session.apply_on_exit.side_effect = OSError('blocked')
        with mock.patch('dlss5tool.managed_update_ui.messagebox.showerror') as showerror, \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread):
            self.assertFalse(self.app._schedule_managed_apply_after_cleanup())
            self.app.root.after.call_args.args[1]()
        showerror.assert_called_once()
        self.assertFalse(self.app._managed_apply_on_close)
        self.assertFalse(self.app._managed_schedule_started)
        self.app._finalize_window_close.assert_called_once()

    def test_aborted_on_close_clears_managed_apply_flag(self):
        from dlss5tool.gui import App
        app = App.__new__(App)
        app._managed_apply_on_close = True
        app._managed_schedule_started = False
        app._gpu_release_thread = None
        app._module_reload_thread = None
        app._diagnosing = True
        app._exporting = app._queue_running = False
        app._cancel_export_memory_retry = mock.Mock()
        app._gpu_thread_alive = mock.Mock(return_value=False)
        app._cancel_after = mock.Mock()
        with mock.patch('dlss5tool.gui.messagebox.showinfo'):
            app._on_close()
        self.assertFalse(app._managed_apply_on_close)

    def test_cancel_download_message_is_not_overwritten_by_later_progress(self):
        layout = PortableLayout(Path('C:/app'), Path('C:/app/current'), 'DLSS5Tool', 'lite', 'win-lite')
        release = updater.ReleaseInfo('v3.2.0', updater.RELEASES_URL, '', ())
        fake_window = mock.Mock()
        fake_window.winfo_exists.return_value = True
        fake_label = mock.Mock()
        fake_bar = mock.Mock()
        buttons = []

        def fake_button(*args, **kwargs):
            btn = mock.Mock()
            buttons.append(kwargs)
            return btn

        def fake_prepare(progress=None, cancelled=None):
            progress(10)
            buttons[0]['command']()
            progress(50)
            raise updater.DownloadCancelled('cancelled')

        fake_session = mock.Mock()
        fake_session.prepare.side_effect = fake_prepare
        with mock.patch('dlss5tool.managed_update_ui.messagebox.askyesno', return_value=True), \
             mock.patch('dlss5tool.managed_update_ui.tk.Toplevel', return_value=fake_window), \
             mock.patch('dlss5tool.managed_update_ui.ttk.Frame', return_value=mock.Mock()), \
             mock.patch('dlss5tool.managed_update_ui.ttk.Label', return_value=fake_label), \
             mock.patch('dlss5tool.managed_update_ui.ttk.Progressbar', return_value=fake_bar), \
             mock.patch('dlss5tool.managed_update_ui.ttk.Button', side_effect=fake_button), \
             mock.patch('dlss5tool.managed_update_ui.managed_update.Session', return_value=fake_session), \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread):
            self.app._prompt_managed_download(release, layout)
            self.app.root.after.call_args.args[1]()
        texts = [call.kwargs['text'] for call in fake_label.config.call_args_list if 'text' in call.kwargs]
        self.assertTrue(texts)
        from dlss5tool.i18n import tr
        self.assertEqual(texts[-1], tr('update.managed_cancel_wait'))

    def test_try_managed_update_resumes_persisted_ready_or_falls_back_to_download(self):
        layout = PortableLayout(Path('C:/app'), Path('C:/app/current'), 'DLSS5Tool', 'lite', 'win-lite')
        release = updater.ReleaseInfo('v3.2.0', updater.RELEASES_URL, '', ())
        self.app._managed_update_session = None
        resumed = mock.Mock()
        with mock.patch('dlss5tool.managed_update_ui.managed_update.current_layout', return_value=layout), \
             mock.patch('dlss5tool.managed_update_ui.managed_update.has_prepared_update', return_value=True), \
             mock.patch('dlss5tool.managed_update_ui.managed_update.Session', return_value=resumed), \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread), \
             mock.patch.object(self.app, '_offer_managed_install') as offer, \
             mock.patch.object(self.app, '_prompt_managed_download') as prompt:
            self.assertTrue(self.app._try_managed_update(release))
            self.app.root.after.call_args.args[1]()
        resumed.ready.assert_called_once()
        self.assertIs(self.app._managed_update_session, resumed)
        offer.assert_called_once()
        prompt.assert_not_called()

        stale = mock.Mock()
        stale.ready.side_effect = updater.UpdateError('stale cache')
        with mock.patch('dlss5tool.managed_update_ui.managed_update.current_layout', return_value=layout), \
             mock.patch('dlss5tool.managed_update_ui.managed_update.has_prepared_update', return_value=True), \
             mock.patch('dlss5tool.managed_update_ui.managed_update.Session', return_value=stale), \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread), \
             mock.patch.object(self.app, '_offer_managed_install') as offer, \
             mock.patch.object(self.app, '_prompt_managed_download') as prompt:
            self.assertTrue(self.app._try_managed_update(release))
            self.app.root.after.call_args.args[1]()
        offer.assert_not_called()
        prompt.assert_called_once_with(release, layout)

    def test_closing_or_scheduling_blocks_export_queue_and_gpu_admission(self):
        from dlss5tool.gui import App
        for flag in ('_app_closing', '_managed_schedule_started'):
            with self.subTest(flag=flag):
                app = App.__new__(App)
                app._app_closing = (flag == '_app_closing')
                app._managed_schedule_started = (flag == '_managed_schedule_started')
                app._switching_backend = False
                app._queue_running = False
                app._exporting = False
                app._diagnosing = False
                app._queue_jobs = [mock.Mock(state='pending')]
                app.root = mock.Mock()
                app._gpu_admit_spy = mock.Mock(side_effect=AssertionError('should block before admission'))
                with mock.patch('tkinter.messagebox.showinfo') as info:
                    self.assertFalse(app._gpu_admit('queue'))
                    self.assertFalse(app._gpu_admit('export'))
                    info.assert_not_called()
                occupants = app._gpu_occupancy()
                self.assertTrue(any(item.owner == 'release' and item.phase == 'releasing' for item in occupants))
                app._gpu_admit = app._gpu_admit_spy
                app.start_export_queue()
                app.export_dlss()
                app.export_diagnostics()
                app._gpu_admit_spy.assert_not_called()

    def test_retry_install_succeeds_after_aborted_close_from_adapter_probe_error(self):
        from dlss5tool.gui import App
        app = App.__new__(App)
        app.root = mock.Mock()
        app._exporting = app._queue_running = app._diagnosing = app._switching_backend = False
        app._update_downloading = False
        app._module_reload_thread = None
        app._gpu_release_thread = None
        app._app_closing = False
        app._managed_verifying = False
        app._managed_schedule_started = False
        app._managed_apply_on_close = True
        app._managed_update_session = mock.Mock()
        app._managed_update_session.release = updater.ReleaseInfo('v3.2.0', updater.RELEASES_URL, '', ())
        app._cancel_export_memory_retry = mock.Mock()
        app._gpu_thread_alive = mock.Mock(return_value=False)
        app._cancel_after = mock.Mock()
        app._cleanup_video_export = mock.Mock()
        app.set_status = mock.Mock()

        with mock.patch('dlss5tool.gui.dlss_engine.close_adapter_probe', side_effect=RuntimeError('probe busy')):
            app._on_close()
        self.assertTrue(app._render_gpu_scan_closing)
        self.assertFalse(app._app_closing)
        self.assertFalse(app._managed_apply_on_close)

        # Retrying installation in the same window must not be dropped by _render_gpu_scan_closing.
        with mock.patch.object(app, '_on_close') as retry_close, \
             mock.patch('dlss5tool.managed_update_ui.messagebox.askyesno', return_value=True), \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread):
            app._offer_managed_install()
            app.root.after.call_args.args[1]()
        self.assertTrue(app._managed_apply_on_close)
        retry_close.assert_called_once()

    def test_resume_verify_updates_status_when_user_defers_or_falls_back(self):
        from dlss5tool.i18n import tr
        layout = PortableLayout(Path('C:/app'), Path('C:/app/current'), 'DLSS5Tool', 'lite', 'win-lite')
        release = updater.ReleaseInfo('v3.2.0', updater.RELEASES_URL, '', ())
        self.app._managed_update_session = None
        resumed = mock.Mock(release=release)
        with mock.patch('dlss5tool.managed_update_ui.managed_update.current_layout', return_value=layout), \
             mock.patch('dlss5tool.managed_update_ui.managed_update.has_prepared_update', return_value=True), \
             mock.patch('dlss5tool.managed_update_ui.managed_update.Session', return_value=resumed), \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread), \
             mock.patch('dlss5tool.managed_update_ui.messagebox.askyesno', return_value=False):
            self.app._try_managed_update(release)
            self.app.root.after.call_args.args[1]()
        self.assertEqual(self.app.set_status.call_args_list[-1].args[0],
                         tr('update.managed_ready', version='v3.2.0'))

        self.app.set_status.reset_mock()
        stale = mock.Mock()
        stale.ready.side_effect = updater.UpdateError('stale cache')
        with mock.patch('dlss5tool.managed_update_ui.managed_update.current_layout', return_value=layout), \
             mock.patch('dlss5tool.managed_update_ui.managed_update.has_prepared_update', return_value=True), \
             mock.patch('dlss5tool.managed_update_ui.managed_update.Session', return_value=stale), \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', side_effect=self.immediate_thread), \
             mock.patch('dlss5tool.managed_update_ui.messagebox.askyesno', return_value=False):
            self.app._try_managed_update(release)
            self.app.root.after.call_args.args[1]()
        self.assertEqual(self.app.set_status.call_args_list[-1].args[0], '')

    def test_thread_start_failure_does_not_wedge_close_or_verify_state(self):
        broken_thread = mock.Mock()
        broken_thread.start.side_effect = RuntimeError('cannot start thread')

        # 1. Scheduling thread startup failure must clear flags, report error, and finalize close.
        self.app._managed_apply_on_close = True
        with mock.patch('dlss5tool.managed_update_ui.messagebox.showerror') as showerror, \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', return_value=broken_thread):
            self.assertFalse(self.app._schedule_managed_apply_after_cleanup())
        showerror.assert_called_once()
        self.assertFalse(self.app._managed_schedule_started)
        self.assertFalse(self.app._managed_apply_on_close)
        self.app._finalize_window_close.assert_called_once()
        self.app.root.after.assert_not_called()

        # 2. Pre-close verify thread startup failure must reset _managed_verifying and keep session.
        self.app._app_closing = False
        session = self.app._managed_update_session
        with mock.patch('dlss5tool.managed_update_ui.messagebox.showerror') as showerror, \
             mock.patch('dlss5tool.managed_update_ui.threading.Thread', return_value=broken_thread):
            self.app._begin_managed_install()
        showerror.assert_called_once()
        self.assertFalse(self.app._managed_verifying)
        self.assertIs(self.app._managed_update_session, session)
        self.app._on_close.assert_not_called()

        # 3. Resume verify thread startup failure must reset _managed_verifying and fall back.
        layout = PortableLayout(Path('C:/app'), Path('C:/app/current'), 'DLSS5Tool', 'lite', 'win-lite')
        release = updater.ReleaseInfo('v3.2.0', updater.RELEASES_URL, '', ())
        with mock.patch('dlss5tool.managed_update_ui.threading.Thread', return_value=broken_thread), \
             mock.patch.object(self.app, '_prompt_managed_download') as prompt:
            self.app._resume_managed_update(release, layout)
        self.assertFalse(self.app._managed_verifying)
        self.assertEqual(self.app.set_status.call_args_list[-1].args[0], '')
        prompt.assert_called_once_with(release, layout)
