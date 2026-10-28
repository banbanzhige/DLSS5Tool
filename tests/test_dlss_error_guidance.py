"""Reported Feature 18 failures reach recovery guidance without GPU work."""
import threading
from unittest import mock

import pytest

from dlss5tool import dlss_error_guidance as hints, gui, i18n, updater


@pytest.fixture(autouse=True)
def restore_language():
    language = i18n.get_language()
    i18n.set_language('zh_CN')
    yield
    i18n.set_language(language)


@pytest.mark.parametrize('error', [
    'RuntimeError: Feature 18 create failed.',
    'Feature 18 resize failed.\ntexture/staging allocation failed',
    'feature 18 CREATE FAILED.\n',
    'dlssnr_init failed (D3D12/gate). See dlss_run.log',
    'CreateFeature(18) -> 0xBAD0000B feature=00000000',
])
def test_generic_startup_failure_has_actionable_guidance(error):
    message = hints.runtime_guidance(error)
    assert '无法确定具体原因' in message
    assert '30系.zip' in message and '50系.zip' in message
    assert i18n.tr('mods.path_settings') in message
    assert i18n.tr('label.render_gpu') in message
    assert '导出诊断日志' in message
    assert updater.RELEASES_URL in message
    assert not hints.is_runtime_unsupported(error)


@pytest.mark.parametrize(('code', 'expected'), [
    ('0xBAD00001', 'FeatureNotSupported'),
    ('0xbad00002', 'PlatformError'),
    ('0xBAD0000C', 'OutOfDate'),
    ('0xbad0000d', 'OutOfGPUMemory'),
    ('0xBAD0000F', 'UnableToWriteToAppDataPath'),
])
def test_known_ngx_result_takes_priority_over_generic_wrapper(code, expected):
    message = hints.runtime_guidance('Feature 18 create failed.\nCreateFeature(18) -> ' + code)
    assert expected in message
    assert '无法确定具体原因' not in message
    assert hints.offers_runtime_download(code) == (code == '0xBAD00001')


@pytest.mark.parametrize('error', [None, '', 'encoder failed', 'Feature 18 ready',
    'CreateFeature(18) -> 0x00000001', 'Feature 180 create failed', '0xBAD000010'])
def test_unrelated_errors_and_success_are_not_startup_failures(error):
    assert hints.runtime_guidance(error) == ''
    assert not hints.offers_runtime_download(error)


def test_symbolic_unsupported_result_is_recognized():
    assert hints.is_runtime_unsupported('NVSDK_NGX_Result_FAIL_FeatureNotSupported')


@pytest.mark.parametrize('language', ['zh_CN', 'en_US'])
def test_guidance_is_localized_and_preserves_full_diagnostics_once(language):
    i18n.set_language(language)
    raw = 'Feature 18 create failed.\n' + ('native log detail\n' * 100) + '0xBAD0000B'
    display = hints.format_error(raw)
    assert display.startswith(hints.runtime_guidance(raw))
    assert raw in display
    assert hints.format_error(display) == display
    assert 'message.dlss_' not in display
    if language == 'en_US':
        assert 'could not initialize' in display
        assert 'Choose DLL' in display
        assert 'Export diagnostic log' in display


@pytest.mark.parametrize('open_download', [False, True])
def test_generic_error_dialog_opens_download_page_only_when_selected(open_download):
    app = gui.App.__new__(gui.App)
    app.root = mock.sentinel.root
    app._open_release_page = mock.Mock()
    with mock.patch.object(gui.messagebox, 'askyesno', return_value=open_download) as ask:
        assert app._show_dlss_error('Feature 18 create failed.')
    assert ask.call_args.args[0] == i18n.tr('dialog.dlss_failed')
    assert '30系.zip' in ask.call_args.args[1]
    assert ask.call_args.kwargs['parent'] is app.root
    if open_download:
        app._open_release_page.assert_called_once_with(updater.RELEASES_URL)
    else:
        app._open_release_page.assert_not_called()


def test_platform_error_keeps_specific_guidance_and_does_not_offer_download():
    app = gui.App.__new__(gui.App)
    app.root = mock.sentinel.root
    error = 'v2: 0xBAD00001\nlegacy: Feature 18 create failed.\n0xBAD00002'
    with mock.patch.object(gui.messagebox, 'showerror') as show, \
            mock.patch.object(gui.messagebox, 'askyesno') as ask:
        assert app._show_dlss_error(error)
    assert show.call_args.args[0] == i18n.tr('dialog.dlss_failed')
    assert 'PlatformError' in show.call_args.args[1]
    ask.assert_not_called()


def test_failed_session_logs_recovery_steps_and_raw_worker_error():
    app = gui.App.__new__(gui.App)
    app._live_lock = threading.RLock()
    app._live = None
    app._guidance_generation = 0
    app.logln = mock.Mock()
    raw = 'Feature 18 create failed.\nworker traceback: native failure'
    with mock.patch.object(gui, 'ProcessLive', side_effect=RuntimeError(raw)):
        assert app._ensure_live(640, 360, {'host_backend': 'auto'}) is None
    assert raw in app._live_error
    assert app._live_error.startswith(hints.runtime_guidance(raw))
    assert app._live_error in app.logln.call_args.args[0]


def test_shared_preview_reports_same_failure_once_even_during_modal_callbacks():
    app = gui.App.__new__(gui.App)
    app.set_status = mock.Mock()
    app.logln = mock.Mock()
    raw = 'Feature 18 create failed.'
    app._show_dlss_error = mock.Mock(side_effect=lambda error: app._shared_report_error(error))
    app._shared_report_error(raw)
    app._shared_report_error(raw)
    app._show_dlss_error.assert_called_once_with(raw)
    app.logln.assert_called_once()
    assert hints.runtime_guidance(raw) in app.logln.call_args.args[0]
    assert raw in app.logln.call_args.args[0]
    assert 'DLSS 增强引擎初始化失败' in app.set_status.call_args.args[0]


@pytest.mark.parametrize('notify', [False, True])
def test_export_failure_uses_guidance_and_respects_queue_notification_setting(notify):
    app = gui.App.__new__(gui.App)
    app._export_cancel_event = threading.Event()
    for name in ('_update_action_labels', '_update_host_control_states',
                 '_update_queue_action_states', 'set_status', '_show_dlss_error'):
        setattr(app, name, mock.Mock())
    app.pbar = {}
    app.eta_label = mock.Mock()
    raw = 'Feature 18 create failed.'
    app._end_export_ui(False, 'unused.png', notify=notify, error_message=raw)
    if notify:
        app._show_dlss_error.assert_called_once_with(raw)
    else:
        app._show_dlss_error.assert_not_called()
