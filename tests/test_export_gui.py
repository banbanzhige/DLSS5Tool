from types import SimpleNamespace
from unittest import mock

from dlss5tool import gui, i18n
from dlss5tool.export_metrics import FrameRate


def progress_app():
    app = gui.App.__new__(gui.App)
    app.root = SimpleNamespace(update_idletasks=lambda: None)
    app.pbar = {}
    app.eta_label = mock.Mock()
    app._export_t0 = 1
    app._export_rate = FrameRate()
    app._update_active_queue_progress = mock.Mock()
    return app


def test_normalized_bar_is_never_counted_as_video_frames():
    app = progress_app()
    translate = lambda key, **values: i18n.tr_for('en_US', key, **values)
    with mock.patch.object(gui, 'tr', translate), mock.patch.object(gui.time, 'perf_counter') as clock:
        clock.return_value = 61
        app.set_progress(200, 1000, 'render', frame_counts=None)
        assert 'frames/s' not in app.eta_label.config.call_args.kwargs['text']
        app.set_progress(201, 1000, 'render', frame_counts=(1, 10000))
        clock.return_value = 64
        app.set_progress(300, 1000, 'encode', frame_counts=(31, 10000))
    text = app.eta_label.config.call_args.kwargs['text']
    assert '10.0 frames/s' in text
    assert '31/10000' in text
    assert app.pbar['value'] == 300 and app.pbar['maximum'] == 1000


def test_preflight_advises_long_sr_once_and_rechecks_new_resource_pressure():
    app = gui.App.__new__(gui.App)
    app.root = object()
    app.logln = mock.Mock()
    app._preview_cache_bytes = lambda: 1024**3
    app._confirm_super_resolution_export = mock.Mock(return_value=True)
    memory = dict(total_physical_bytes=32*1024**3, free_physical_bytes=20*1024**3,
                  free_commit_bytes=24*1024**3)
    plan = {'frames': 36000, 'fps': 30, 'settings': {}}
    with mock.patch.object(gui, 'system_memory', return_value=memory), \
            mock.patch.object(gui, 'query_gpu_memory', return_value={'free_bytes': 7*1024**3}) as gpu, \
            mock.patch.object(gui.messagebox, 'askyesno', return_value=True) as prompt:
        assert app._confirm_video_export_resources(1920, 1080, 2, False, True, plan)
        assert 'long_run' in app._last_export_preflight['warnings']
        assert app._confirm_video_export_resources(1920, 1080, 2, False, True, plan)
        assert prompt.call_count == 1
        gpu.return_value = {'free_bytes': 128*1024**2}
        prompt.return_value = False
        assert not app._confirm_video_export_resources(1920, 1080, 2, False, True, plan)
        assert prompt.call_count == 2


def test_cancelled_preflight_does_not_start_export_worker():
    app = gui.App.__new__(gui.App)
    app.root = object()
    app.logln = mock.Mock()
    app._preview_cache_bytes = lambda: 1024**3
    app._confirm_super_resolution_export = mock.Mock(return_value=False)
    with mock.patch.object(gui, 'assess_video_export') as assess:
        assert not app._confirm_video_export_resources(1920, 1080, 2, False, True, {})
    assess.assert_not_called()


def test_queue_checks_long_video_before_starting_jobs():
    app = gui.App.__new__(gui.App)
    app._gpu_admit = mock.Mock(return_value=True)
    app._queue_running = app._exporting = False
    app._queue_jobs = [SimpleNamespace(state='pending', media_kind='video',
        metadata={'width': 1920, 'height': 1080, 'frames': 36000, 'fps': 30},
        export_settings={'super_resolution_scale': 2}, settings={}, color_info={})]
    app._confirm_super_resolution_export = mock.Mock(return_value=False)
    app._prepare_queue_jobs_for_start = mock.Mock()
    with mock.patch.object(gui, 'query_gpu_memory', return_value=None):
        app.start_export_queue()
    assert app._confirm_super_resolution_export.call_args.kwargs['video_plan']['frames'] == 36000
    app._prepare_queue_jobs_for_start.assert_not_called()
