"""Runtime preparation/tests retire previews and keep all Tk calls on the UI thread."""
import os
import threading
import time
import tkinter as tk
from unittest import mock

import pytest

from dlss5tool import gui, dlss_runtime
from tests import test_gui_module_reload as reload_fixture


@pytest.fixture
def session():
    fixture = reload_fixture.ModuleReloadTests('runTest')
    fixture.setUp()
    fixture.app._host_settings = {'w_runtime_status': mock.Mock()}
    return fixture


def test_frame_test_runs_off_tk_and_closes_old_session_first(session):
    app = session.app
    entered, release = threading.Event(), threading.Event()
    app._live = mock.Mock()
    old_live = app._live
    def test(settings):
        assert threading.current_thread() is not threading.main_thread()
        old_live.close.assert_called_once()
        entered.set()
        release.wait(3)
        return {'adapter': {'name': 'RTX Fixture'}, 'path': 'selected.dll', 'backend': 'v2'}
    with mock.patch.object(dlss_runtime, 'test_runtime', side_effect=test):
        app._test_runtime()
        try:
            assert entered.wait(1)
            assert app._switching_backend
            app.root.tick()
            assert app._switching_backend
        finally:
            release.set()
            session.wait_reload()
    assert not app._switching_backend
    assert 'RTX Fixture' in app._runtime_test_text
    assert 'selected.dll' in app.logln.call_args.args[0]


def test_failed_test_preserves_selection_and_allows_retry(session):
    app = session.app
    settings = {'dlss_runtime': '__rtx50__'}
    app._collect_host_settings = lambda: dict(settings)
    with mock.patch.object(dlss_runtime, 'ensure_runtime', return_value={'profile': 'rtx50'}), \
            mock.patch.object(dlss_runtime, 'test_runtime', side_effect=RuntimeError('incompatible DLL')):
        app._test_runtime()
        session.wait_reload()
    assert settings['dlss_runtime'] == '__rtx50__'
    assert 'incompatible DLL' in app._runtime_test_text
    assert app._last_module_settings is None
    assert not app._switching_backend


def test_download_progress_uses_mailbox_and_can_cancel_on_close(session):
    app = session.app
    app._collect_host_settings = lambda: {'dlss_runtime': '__rtx30__'}
    entered = threading.Event()
    def ensure(settings, *, adapter, progress, cancel):
        assert threading.current_thread() is not threading.main_thread()
        progress(25, 100)
        entered.set()
        assert cancel.wait(2)
        raise RuntimeError('cancelled')
    with mock.patch.object(dlss_runtime, 'ensure_runtime', side_effect=ensure):
        app._on_mod_settings_change()
        assert entered.wait(1)
        app.root.tick()
        assert '25%' in app._runtime_test_text
        app._on_close()
        assert app._runtime_cancel.is_set()
        app._on_close = mock.Mock()
        session.wait_reload()
        app._on_close.assert_called_once()


@pytest.mark.parametrize('flag', ['_exporting', '_queue_running', '_switching_backend', '_diagnosing'])
def test_test_button_cannot_conflict_with_an_existing_task(session, flag):
    setattr(session.app, flag, True)
    with mock.patch.object(dlss_runtime, 'test_runtime') as test:
        session.app._test_runtime()
        test.assert_not_called()
    assert session.app._module_reload_thread is None


def test_real_controls_persist_series_selection_and_restore_auto(tmp_path):
    env = {'DLSS5TOOL_SETTINGS_PATH': str(tmp_path / 'settings.json'),
           'DLSS5TOOL_QUEUE_PATH': str(tmp_path / 'queue.json')}
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(gui.App, '_start_render_gpu_scan'), \
            mock.patch.object(dlss_runtime, 'selected_adapter', return_value={}):
        gui.app_settings.save({'guidance_mode': 0})
        root = tk.Tk()
        root.withdraw()
        app = gui.App(root)
        errors = []
        root.report_callback_exception = lambda *value: errors.append(value)
        def finish():
            deadline = time.monotonic() + 3
            while app._module_reload_thread is not None and time.monotonic() < deadline:
                root.update()
                time.sleep(.005)
            assert app._module_reload_thread is None
        try:
            entered, release = threading.Event(), threading.Event()
            def ensure(*args, **kwargs):
                entered.set()
                release.wait(3)
                return {'profile': 'rtx30'}
            tested = {'adapter': {'name': 'RTX Fixture'}, 'path': 'fixture.dll', 'backend': 'v2'}
            with mock.patch.object(dlss_runtime, 'ensure_runtime', side_effect=ensure), \
                    mock.patch.object(dlss_runtime, 'test_runtime', return_value=tested):
                app._host_settings['v_runtime'].set(dlss_runtime.selection_label('__rtx30__'))
                app._test_runtime()
                assert entered.wait(1)
                assert str(app._host_settings['w_runtime'].cget('state')) == 'disabled'
                assert str(app._host_settings['w_runtime_test'].cget('state')) == 'disabled'
                release.set()
                finish()
            assert app._collect_host_settings()['dlss_runtime'] == '__rtx30__'
            assert str(app._host_settings['w_runtime'].cget('state')) == 'readonly'
            assert str(app._host_settings['w_runtime_test'].cget('state')) == 'normal'
            assert 'RTX Fixture' in app._host_settings['w_runtime_status'].cget('text')
            app._host_settings['v_runtime'].set(dlss_runtime.selection_label(''))
            app._on_mod_settings_change()
            finish()
            assert app._collect_host_settings()['dlss_runtime'] == ''
            assert not errors
        finally:
            app._on_close()


def test_realtime_prepares_matching_gpu_runtime_off_main_thread():
    app = gui.App.__new__(gui.App)
    app._gpu_conflict = lambda operation: None
    workspace = app._realtime_workspace = mock.Mock()
    workspace.session_summary.return_value = ('idle', False)
    workspace.collect.return_value = {'gpu_index': 2}
    workspace._adapters = [{'index': 2, 'vendor': 0x10DE, 'name': 'RTX 5090'}]
    app._collect_host_settings = lambda: {'dlss_runtime': ''}
    app._on_mod_settings_change = mock.Mock()
    with mock.patch.object(gui.mod_paths, 'runtime_info', return_value={
            'path': 'matching.dll', 'profile': 'rtx50', 'needs_download': True}), \
            mock.patch.object(dlss_runtime, 'install') as install:
        assert not app._can_start_realtime()
    assert app._runtime_prepare_adapter['name'] == 'RTX 5090'
    app._on_mod_settings_change.assert_called_once()
    install.assert_not_called()
    workspace.client.start.assert_not_called()
