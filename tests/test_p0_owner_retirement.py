"""Failed retirement must retain owners and still close independent resources."""
import threading
from unittest import mock

import pytest

from dlss5tool import gui, render_cache
from dlss5tool.dlss_host_process import ProcessLive, HostProcessError


@pytest.mark.parametrize('kind', ['nr', 'sr'])
def test_preview_failed_close_keeps_owner_for_retry(kind):
    app = gui.App.__new__(gui.App)
    app._live_lock = app._super_resolution_lock = threading.RLock()
    resource = mock.Mock()
    resource.close.side_effect = RuntimeError('worker still alive')
    app._live = app._super_resolution_live = resource
    app._super_resolution_key = key = (640, 360, 2, False)
    app._last_super_resolution_preview = object()
    app._cache_clear = mock.Mock()
    method = app._close_live if kind == 'nr' else app._close_super_resolution
    field = '_live' if kind == 'nr' else '_super_resolution_live'
    with mock.patch.object(gui, 'release_native_composer') as composer:
        with pytest.raises(RuntimeError, match='worker still alive'):
            method()
        assert getattr(app, field) is resource
        if kind == 'nr':
            app._cache_clear.assert_called_once_with()
            composer.assert_called_once_with()
        else:
            assert app._super_resolution_key == key
            assert app._last_super_resolution_preview is None
        resource.close.side_effect = None
        method()
    assert getattr(app, field) is None
    assert resource.close.call_count == 2


def replacement_owner():
    owner = ProcessLive.__new__(ProcessLive)
    owner._session = old = mock.Mock()
    owner._make_session = mock.Mock(return_value=mock.Mock())
    owner._sync_metadata = mock.Mock()
    old.close.side_effect = HostProcessError('old worker still alive')
    return owner, old


def test_failed_replaced_worker_is_retained_and_blocks_more_allocations():
    owner, old = replacement_owner()
    with pytest.raises(HostProcessError, match='old worker still alive'):
        owner._replace(640, 360, {'host_backend': 'v2'})
    replacement = owner._session
    assert owner._retiring_sessions == [old]
    with pytest.raises(HostProcessError, match='old worker still alive'):
        owner._replace(800, 448, {'host_backend': 'v2'})
    assert owner._make_session.call_count == 1
    assert owner._session is replacement
    old.close.side_effect = None
    owner.close()
    assert owner._session is None and owner._retiring_sessions == []
    replacement.close.assert_called_once_with()


def test_retiring_failure_does_not_skip_active_session_close():
    owner, old = replacement_owner()
    active = owner._session = mock.Mock()
    owner._retiring_sessions = [old]
    with pytest.raises(HostProcessError, match='old worker still alive'):
        owner.close()
    active.close.assert_called_once_with()
    assert owner._session is None and owner._retiring_sessions == [old]
    old.close.side_effect = None
    owner.close()
    assert owner._retiring_sessions == []


def test_partly_closed_proxy_cannot_allocate_a_new_session():
    owner, old = replacement_owner()
    owner._session = None
    owner._retiring_sessions = [old]
    old.close.side_effect = None
    with pytest.raises(HostProcessError, match='关闭'):
        owner._replace(800, 448, {'host_backend': 'v2'})
    owner._make_session.assert_not_called()
    owner.close()
    assert not owner._retiring_sessions


def test_cache_manager_closes_all_layers_and_keeps_only_failed_sessions():
    manager = render_cache.RenderCache(1024**2)
    bad, good, spatial = mock.Mock(), mock.Mock(), mock.Mock()
    bad.close.side_effect = RuntimeError('cache worker still alive')
    manager.sessions.update(bad=bad, good=good)
    manager.base = good
    manager.spatial = spatial
    with pytest.raises(RuntimeError, match='cache worker still alive'):
        manager.close()
    good.close.assert_called_once_with()
    spatial.close.assert_called_once_with()
    assert list(manager.sessions.values()) == [bad]
    assert manager.base is manager.spatial is None
    bad.close.side_effect = None
    manager.close()
    assert not manager.sessions


def preview_app():
    app = gui.App.__new__(gui.App)
    app._cancel_after = app._clear_preview_wait = mock.Mock()
    app._shared_config = lambda: {}
    app._ensure_shared_cache_pool = mock.Mock()
    return app


def test_failed_background_cache_retirement_remains_owned_until_retry():
    app = preview_app()
    manager = app._render_cache_manager = mock.Mock()
    manager.close.side_effect = RuntimeError('cache worker still alive')
    app._shared_retire()
    app._shared_retiring.join(3)
    assert not app._shared_retiring.is_alive()
    assert app._shared_retire_pending == [manager]
    assert app._shared_retire_error == 'cache worker still alive'
    with mock.patch('dlss5tool.shared_render_preview.RenderCache') as create:
        with pytest.raises(RuntimeError, match='cache worker still alive'):
            app._shared_manager()
        create.assert_not_called()
    manager.close.side_effect = None
    app._shared_retire()
    app._shared_retiring.join(3)
    assert app._shared_retire_pending == [] and app._shared_retire_error is None
    assert manager.close.call_count == 2


def test_overlapping_retirements_do_not_lose_failed_old_manager():
    app = preview_app()
    entered, release = threading.Event(), threading.Event()
    bad = app._render_cache_manager = mock.Mock()
    def fail_close():
        entered.set()
        assert release.wait(3)
        raise RuntimeError('old cache worker still alive')
    bad.close.side_effect = fail_close
    app._shared_retire()
    first = app._shared_retiring
    assert entered.wait(2)
    good = app._render_cache_manager = mock.Mock()
    try:
        app._shared_retire()
    finally:
        release.set()
    app._shared_retiring.join(3)
    first.join(3)
    good.close.assert_called_once_with()
    assert app._shared_retire_pending == [bad]
    bad.close.side_effect = None
    app._shared_retire()
    app._shared_retiring.join(3)
    assert app._shared_retire_pending == []


def test_retirement_thread_start_failure_is_owned_and_can_be_retried():
    app = preview_app()
    manager = app._render_cache_manager = mock.Mock()
    with mock.patch('dlss5tool.shared_render_preview.threading.Thread.start',
                    side_effect=RuntimeError('thread could not start')):
        app._shared_retire()
    assert app._shared_retire_pending == [manager]
    assert app._shared_retire_error == 'thread could not start'
    assert app._gpu_occupancy()[0].phase == 'failed'
    app._shared_retire()
    app._shared_retiring.join(3)
    assert not app._shared_retire_pending


def test_failed_retirement_does_not_report_cache_clear_complete():
    app = preview_app()
    app.video = None
    app._clear_preview_pending = app._preview_cache_frozen = True
    app._shared_retire_pending = [mock.Mock()]
    app._shared_retire_error = 'cache worker still alive'
    app._finish_clear_preview_cache = mock.Mock()
    app._update_clear_cache_control = app.set_status = app.logln = mock.Mock()
    app._poll_clear_preview_cache()
    app._finish_clear_preview_cache.assert_not_called()
    app.set_status.assert_called_with('cache worker still alive')
    assert not app._clear_preview_pending


def test_exit_waits_for_failed_cache_owner_and_allows_a_later_retry():
    app = preview_app()
    app._diagnosing = app._exporting = app._queue_running = app._update_downloading = False
    app._shared_closing = True
    app._shared_retire_pending = [mock.Mock()]
    app._shared_retire_error = 'cache worker still alive'
    app.root = mock.Mock()
    app.set_status = mock.Mock()
    app._on_close()
    app.root.destroy.assert_not_called()
    app.set_status.assert_called_once_with('cache worker still alive')
    assert not app._shared_closing


def test_cancel_cleanup_failure_still_finishes_export_ui():
    from dlss5tool import app_settings
    app = gui.App.__new__(gui.App)
    app._collect_settings = lambda: dict(app_settings.DEFAULTS)
    export = dict(app_settings.DEFAULTS, mode='single', frame_generation_multiplier=1)
    app._collect_export_settings = lambda: export
    app._ensure_shared_cache_pool = lambda: mock.Mock(name='fixture-pool')
    app._video_info = lambda *args: (24, 24, 320, 180)
    app._confirm_super_resolution_export = lambda *args, **kwargs: True
    app._live_lock = threading.RLock()
    live = mock.Mock(max_in_flight=1, supports_async=False, backend='v2', adapter_info={})
    app._ensure_live = mock.Mock(return_value=live)
    import numpy as np
    def frames(*args, **kwargs):
        yield 0, np.zeros((180, 320, 3), np.uint8)
        raise gui._ExportCancelled()
    app._iter_frames = frames
    live.process.side_effect = lambda rgba, **kwargs: rgba
    app._raise_if_export_cancelled = app.set_progress = mock.Mock()
    app.root = mock.Mock()
    app._close_live = mock.Mock(side_effect=RuntimeError('cancel close failed'))
    app._begin_export_ui = lambda text: setattr(app, '_export_t0', 0)
    app._end_export_ui = app._remember_export = app.logln = mock.Mock()
    writer = mock.Mock()
    with mock.patch.object(gui, 'FFmpegVideoWriter', return_value=writer):
        result = app._export_video_source('fixture.mp4', {}, {}, {},
                                          out_path='fixture-output.mp4', notify=False)
    writer.abort.assert_called_once_with()
    assert result['cancelled'] and 'cancel close failed' in result['error']
    assert any(call.kwargs.get('cancelled') for call in app._end_export_ui.call_args_list)
    assert len(app._pending_export_cleanup) == 1
