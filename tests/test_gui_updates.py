"""Update UX contracts without opening windows or using GitHub."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from dlss5tool import gui, updater, delta_update as delta


class UpdateGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / delta.HELPER).touch()
        self.app = gui.App.__new__(gui.App)
        self.app.logln = mock.Mock()
        self.app._collect_host_settings = lambda: {}
        self.app._collect_settings = mock.Mock(side_effect=AssertionError('Update checks must not allocate preview caches'))
        self.app._start_update_download = mock.Mock()
        self.app._start_delta_download = mock.Mock()
        self.app._open_release_page = mock.Mock()
        self.app._exporting = self.app._queue_running = self.app._diagnosing = False
        self.app._switching_backend = False
        self.app._update_action_labels = mock.Mock()
        self.app._on_close = mock.Mock()
        self.app.root = mock.Mock()
        self.release = updater.ReleaseInfo('v99.0.0', updater.RELEASES_URL, '', (
            updater.ReleaseAsset('DLSS5Tool-v99.0.0-win64.zip', 'https://github.com/a/lite.zip', 10),))
        patch = mock.patch.object(gui.paths, 'app_root', return_value=self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def test_full_install_never_silently_downloads_lite(self):
        (self.root / 'mods/enhancement').mkdir(parents=True)
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=True) as ask:
            self.app._prompt_for_update(self.release)
        self.app._start_update_download.assert_not_called()
        self.app._open_release_page.assert_called_once()
        self.assertIn('完整', ask.call_args.args[1])

    def test_lite_falls_back_to_portable_download(self):
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=True):
            self.app._prompt_for_update(self.release)
        self.app._start_update_download.assert_called_once()

    def test_delta_requires_download_confirmation(self):
        asset = updater.ReleaseAsset('delta', 'https://github.com/a/delta', 10)
        with (mock.patch.object(gui.delta_update, 'select_asset', return_value=asset),
              mock.patch.object(gui.sys, 'frozen', True, create=True),
              mock.patch.object(gui.messagebox, 'askyesno', return_value=False)):
            self.app._prompt_for_update(self.release)
        self.app._start_delta_download.assert_not_called()
        self.app._start_update_download.assert_not_called()

    def test_confirmed_delta_routes_to_file_update(self):
        asset = updater.ReleaseAsset('delta', 'https://github.com/a/delta', 10)
        with (mock.patch.object(gui.delta_update, 'select_asset', return_value=asset),
              mock.patch.object(gui.sys, 'frozen', True, create=True),
              mock.patch.object(gui.messagebox, 'askyesno', return_value=True)):
            self.app._prompt_for_update(self.release)
        self.app._start_delta_download.assert_called_once_with(self.release, asset, 'lite')

    def test_export_blocks_install_and_does_not_launch_helper(self):
        self.app._exporting = True
        with (mock.patch.object(gui.messagebox, 'showinfo'),
              mock.patch.object(gui.update_helper, 'launch') as launch):
            self.app._offer_delta_install(self.release)
        launch.assert_not_called()
        self.app._on_close.assert_not_called()

    def test_install_decline_keeps_program_running(self):
        with (mock.patch.object(gui.messagebox, 'askyesno', return_value=False),
              mock.patch.object(gui.update_helper, 'launch') as launch):
            self.app._offer_delta_install(self.release)
        launch.assert_not_called()
        self.app._on_close.assert_not_called()

    def test_only_close_after_helper_acknowledges_waiting(self):
        process = mock.Mock()
        process.poll.return_value = None
        with (mock.patch.object(gui.messagebox, 'askyesno', return_value=True),
              mock.patch.object(gui.update_helper, 'launch', return_value=process),
              mock.patch.object(gui.delta_update, 'status', return_value={'phase': 'waiting'})):
            self.app._offer_delta_install(self.release)
            self.app._on_close.assert_not_called()
            callback = self.app.root.after.call_args.args[1]
            callback()
        self.app._on_close.assert_called_once()

    def test_helper_launch_failure_leaves_program_open_and_offers_recovery(self):
        with (mock.patch.object(gui.messagebox, 'askyesno', return_value=True),
              mock.patch.object(gui.update_helper, 'launch', side_effect=OSError('blocked'))):
            self.app._offer_delta_install(self.release)
        self.app._on_close.assert_not_called()
        self.app._open_release_page.assert_called_once()
