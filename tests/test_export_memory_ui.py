"""User-selected retries wait for retirement and preserve media and outputs."""
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from dlss5tool import export_memory_ui as actions, gui, i18n


class Root:
    def __init__(self):
        self.callbacks = {}
        self.identity = 0

    def after(self, delay, callback):
        self.identity += 1
        self.callbacks[self.identity] = callback
        return self.identity

    def after_idle(self, callback):
        return self.after(0, callback)

    def update_idletasks(self):
        pass

    def after_cancel(self, identity):
        self.callbacks.pop(identity, None)

    def tick(self):
        identity = next(iter(self.callbacks))
        self.callbacks.pop(identity)()


@pytest.fixture
def app(tmp_path):
    app = gui.App.__new__(gui.App)
    app.root = Root()
    app.video = str(tmp_path/'source.png')
    app._exporting = app._queue_running = app._clear_preview_pending = False
    app._queue_cleanup_pending = app._preview_cache_frozen = False
    app._export_cancel_event = threading.Event()
    app._gpu_conflict = mock.Mock(return_value=None)
    app._gpu_admit = mock.Mock(return_value=True)
    for name in ('pause', '_update_clear_cache_control', 'set_status', 'logln',
                 '_update_action_labels', '_update_host_control_states', '_update_queue_action_states',
                 '_save_queue_state', '_refresh_queue_tree', 'start_export_queue'):
        setattr(app, name, mock.Mock())
    app.pbar = {}
    app.eta_label = mock.Mock()
    app._show_dlss_error = mock.Mock(return_value=False)
    app.retire_results = [True]
    app.order = []
    def retire():
        app.order.append('retire')
        result = app.retire_results.pop(0) if len(app.retire_results)>1 else app.retire_results[0]
        if isinstance(result, Exception):
            raise result
        if result:
            app._queue_cleanup_pending = False
        return result
    def clear():
        app.order.append('clear')
        app._clear_preview_pending = False
    app._retire_queue_memory_owners = mock.Mock(side_effect=retire)
    app._finish_clear_preview_cache = mock.Mock(side_effect=clear)
    app._export_image_source = mock.Mock(side_effect=lambda *a, **k: app.order.append('export'))
    app._export_video_source = mock.Mock()
    app._unique_target_path = mock.Mock(return_value=str(tmp_path/'retry.png'))
    return app


def pressure():
    return dict(required_bytes=14*1024**2, failure='reclaim_timeout', reason='commit',
                reserved_processing_bytes=0, reclaim_acknowledged=False)


def request(app, kind='image'):
    return actions.ExportRetry(kind, app.video, app.video+'.out', dict(output_mix=.8),
                               dict(mode='single'), dict(is_hdr=True))


def test_failure_actions_are_deferred_and_settings_do_not_retry(app, monkeypatch):
    show = mock.Mock(return_value='settings')
    monkeypatch.setattr(actions, 'show_memory_actions', show)
    app._open_preview_cache_settings = mock.Mock()
    assert app._offer_export_memory_actions(pressure(), request(app))
    show.assert_not_called()
    app.root.tick()
    app._open_preview_cache_settings.assert_called_once()
    app._retire_queue_memory_owners.assert_not_called()
    app._export_image_source.assert_not_called()


@pytest.mark.parametrize('changed', ['source', 'export', 'queue', 'closing'])
def test_deferred_dialog_is_discarded_if_context_changes(app, monkeypatch, changed):
    show = mock.Mock()
    monkeypatch.setattr(actions, 'show_memory_actions', show)
    app._offer_export_memory_actions(pressure(), request(app))
    if changed == 'source':
        app.video += 'new'
    else:
        flag = {'export': '_exporting', 'queue': '_queue_running', 'closing': '_render_gpu_scan_closing'}[changed]
        setattr(app, flag, True)
    app.root.tick()
    show.assert_not_called()


def test_clear_and_retry_waits_for_old_owner_then_restarts_snapshot(app, monkeypatch):
    monkeypatch.setattr(actions, 'show_memory_actions', lambda *a: 'retry')
    app.retire_results = [False, True]
    job = request(app)
    app._offer_export_memory_actions(pressure(), job)
    app.root.tick()  # user selects retry
    assert app._clear_preview_pending and app._queue_cleanup_pending
    app.root.tick()  # old owner is still alive
    app._finish_clear_preview_cache.assert_not_called()
    app._export_image_source.assert_not_called()
    app.root.tick()  # all old owners have retired; clear cache now
    app._export_image_source.assert_not_called()
    app.root.tick()  # replacement starts after the cleanup callback returned
    app._export_image_source.assert_called_once_with(job.source_path, dict(job.settings), job.output_path, notify=True)
    assert app.order == ['retire', 'retire', 'clear', 'export']
    assert not app._clear_preview_pending and not app.root.callbacks


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_retry_preserves_existing_output_and_original_settings(app, kind):
    job = request(app, kind)
    Path(job.output_path).write_bytes(b'preserve this file')
    app._run_export_memory_retry(job, app.video)
    app._unique_target_path.assert_called_once_with(job.output_path)
    target = app._export_image_source if kind == 'image' else app._export_video_source
    assert target.call_args.args[0] == job.source_path
    assert target.call_args.args[1] == job.settings
    assert target.call_args.args[-1] == app._unique_target_path.return_value
    assert Path(job.output_path).read_bytes() == b'preserve this file'


@pytest.mark.parametrize('failure', ['busy', 'error', 'timeout'])
def test_cleanup_failure_or_wait_never_starts_replacement(app, monkeypatch, failure):
    popup = mock.Mock()
    monkeypatch.setattr(actions.messagebox, 'showerror', popup)
    app._start_export_memory_retry(request(app), app.video)
    app.retire_results = [RuntimeError('owner still alive') if failure == 'error' else False]
    if failure == 'timeout':
        job, source, started = app._export_memory_retry
        app._export_memory_retry = (job, source, started-121)
    app.root.tick()
    app._export_image_source.assert_not_called()
    app._finish_clear_preview_cache.assert_not_called()
    assert app._queue_cleanup_pending
    assert popup.call_count == int(failure != 'busy')


def test_changed_source_cannot_run_scheduled_retry(app):
    app._start_export_memory_retry(request(app), app.video)
    app.video += 'new'
    app.root.tick()
    app._retire_queue_memory_owners.assert_not_called()
    assert app._export_memory_retry is None
    app._export_image_source.assert_not_called()


def test_exit_or_source_load_cancels_retry_after_cleanup_finished(app):
    app._start_export_memory_retry(request(app), app.video)
    app.root.tick()
    assert app.root.callbacks
    app._cancel_export_memory_retry()
    assert not app.root.callbacks
    app._export_image_source.assert_not_called()


def test_retry_invalidates_inflight_guidance_and_releases_held_results(app):
    app._guidance_preview_epoch = 4
    app._guidance_result = app._guidance_ready = app._guidance_presented = object()
    app._guidance_display_signature = object()
    app._start_export_memory_retry(request(app), app.video)
    assert app._guidance_preview_epoch == 5
    assert app._guidance_result is app._guidance_ready is app._guidance_presented is None
    assert app._guidance_display_signature is None


def test_retry_respects_realtime_ownership(app):
    app._gpu_conflict.return_value = SimpleNamespace(owner='realtime')
    app._gpu_admit.return_value = False
    app._start_export_memory_retry(request(app), app.video)
    assert not app._clear_preview_pending and not app.root.callbacks
    app._retire_queue_memory_owners.assert_not_called()


def test_settings_reveals_existing_cache_limit_without_changing_it(app):
    app.workspace_tabs = mock.Mock()
    app._export_page = object()
    app._preview_section = SimpleNamespace(collapsed=True, toggle=mock.Mock())
    widget = mock.Mock()
    widget.winfo_rooty.return_value = 350
    app._preview_settings = {'w_cache_mb': widget}
    app._export_inner = mock.Mock()
    app._export_inner.winfo_rooty.return_value = 100
    app._export_inner.winfo_height.return_value = 1000
    app._export_canvas = mock.Mock()
    app._open_preview_cache_settings()
    app.root.tick()
    app.workspace_tabs.select.assert_called_once_with(app._export_page)
    app._preview_section.toggle.assert_called_once()
    app._export_canvas.yview_moveto.assert_called_once_with(.17)
    widget.focus_set.assert_called_once()
    widget.set.assert_not_called()


@pytest.mark.parametrize('notify', [False, True])
def test_export_end_only_offers_actions_for_visible_memory_failure(app, monkeypatch, notify):
    popup = mock.Mock()
    monkeypatch.setattr(gui.messagebox, 'showerror', popup)
    app._offer_export_memory_actions = mock.Mock()
    job = request(app)
    app._end_export_ui(False, job.output_path, notify=notify, memory_pressure=pressure(), retry_request=job)
    assert app._offer_export_memory_actions.call_count == int(notify)
    popup.assert_not_called()
    app._show_dlss_error.assert_not_called()


def test_non_memory_failure_keeps_existing_error_guidance(app, monkeypatch):
    popup = mock.Mock()
    monkeypatch.setattr(gui.messagebox, 'showerror', popup)
    app._end_export_ui(False, 'unused', error_message='bad input')
    app._show_dlss_error.assert_called_once_with('bad input')
    popup.assert_called_once()


@pytest.mark.parametrize('loaded_preview', [False, True])
def test_queue_retry_works_without_preview_and_resets_only_failed_item(app, loaded_preview):
    from dlss5tool.export_queue import ExportJob
    job = ExportJob.create(app.video, app.video+'.out', {}, {})
    job.state = 'failed'
    completed = ExportJob.create(app.video, app.video+'.done', {}, {})
    completed.state = 'completed'
    app._queue_job = lambda identity: job if identity == job.job_id else None
    if not loaded_preview:
        app.video = None
    retry = actions.ExportRetry('queue', job.source_path, job.output_path, job_id=job.job_id)
    assert app._memory_actions_available(retry)
    app._start_export_memory_retry(retry, app.video)
    app.root.tick()  # retire and clear
    app.root.tick()  # resume queue through its existing entry point
    assert job.state == 'pending' and completed.state == 'completed'
    app.start_export_queue.assert_called_once()
    app._export_image_source.assert_not_called()


def test_removed_queue_item_cannot_be_retried_from_stale_dialog(app):
    retry = actions.ExportRetry('queue', app.video, app.video+'.out', job_id='deleted')
    app._queue_job = lambda identity: None
    app._start_export_memory_retry(retry, app.video)
    assert not app.root.callbacks
    app._retire_queue_memory_owners.assert_not_called()


@pytest.mark.parametrize('language', i18n.SUPPORTED_LANGUAGES)
def test_action_translations_exist_in_both_languages(language):
    import ast
    tree = ast.parse(Path(actions.__file__).read_text(encoding='utf-8'))
    keys = {node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == 'tr' and node.args
        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)}
    keys.update('memory.export.'+value for value in ('physical', 'commit', 'reserve', 'busy', 'reclaim_timeout', 'idle'))
    assert not keys-i18n.catalog_keys(language)


def _check_real_dialog(language, selected):
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.withdraw()  # Exercise real widgets without opening the main app.
    old_language = i18n.get_language()
    errors = []
    clicked = []
    i18n.set_language(language)
    key = {'retry': 'memory.export.retry', 'settings': 'memory.export.settings', 'close': 'common.close'}[selected]
    def choose():
        try:
            window = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))
            window.update_idletasks()
            assert window.winfo_reqwidth() < window.winfo_screenwidth()
            assert window.winfo_reqheight() < window.winfo_screenheight()
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            button = next(child for child in descendants(window) if isinstance(child, ttk.Button)
                          and str(child.cget('text')) == i18n.tr(key))
            clicked.append(True)
            button.invoke()
        except Exception as error:
            errors.append(error)
            for child in root.winfo_children():
                child.destroy()
    root.after(30, choose)
    watchdog = root.after(2500, lambda: [child.destroy() for child in root.winfo_children()])
    try:
        result = actions.show_memory_actions(root, pressure(), 'sample-with-a-long-file-name-示例.mp4')
        assert not errors, errors
        assert clicked and result == selected
    finally:
        root.after_cancel(watchdog)
        root.destroy()
        i18n.set_language(old_language)


@pytest.mark.parametrize('language', i18n.SUPPORTED_LANGUAGES)
@pytest.mark.parametrize('selected', ['retry', 'settings', 'close'])
def test_real_tk_dialog_routes_each_action_and_fits_screen(language, selected):
    # Match the repository's Tk acceptance approach: each Tcl/Tk runtime gets
    # a fresh process, so another test's interpreter cannot affect startup.
    import subprocess
    import sys
    code = ('import sys; from tests.test_export_memory_ui import _check_real_dialog; '
            '_check_real_dialog(*sys.argv[1:])')
    result = subprocess.run([sys.executable, '-B', '-c', code, language, selected],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        encoding='utf-8', timeout=15,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stdout+result.stderr
