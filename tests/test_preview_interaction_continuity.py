"""Display gestures preserve cache producers; pixel changes still refresh them.

No windows, GPU runtime, media files or temporary output are required.
"""
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from dlss5tool.gui import App


class PreviewContinuityTests(unittest.TestCase):
    def make_app(self, playing=False):
        app = App.__new__(App)
        app.video = 'preview.mp4'
        app._source_kind = 'video'
        app.playing = playing
        app._exporting = app._queue_running = app._diagnosing = False
        app._hold_original = app._guidance_context = False
        app._preview_cache_frozen = False
        app._preview_cache_resume_after = None
        app._preview_decode_after = 'decode-timer'
        app._scrub_after = None
        app._pre_rendering = True
        app._prefetch_gen = 7
        app._prefetch_stop = threading.Event()
        app._cache_lock = threading.RLock()
        app._live_lock = threading.RLock()
        app._queued_preview_frames = {0, 1}
        app._preview_frame_queue = queue.Queue()
        app._preview_zoom = 1.
        app._preview_pan_x = app._preview_pan_y = .5
        app._viewport_scale = 1.
        app._viewport_source_size = (8, 4)
        app._drag_split = False
        app._canvas_press = None
        app._frame = 0
        app._buffering = False
        app._active_preview_size = (8, 4)
        app._shared_clock = 123.
        app._audio = Mock()
        app._background_preview_decoder = Mock()
        app.canvas = Mock()
        app.root = Mock()
        app.view_var = Mock(get=Mock(return_value='compare'))
        app.compare_layout = Mock(get=Mock(return_value='wipe'))
        app._source_size = lambda: (8, 4)
        app._canvas_size = lambda: (200, 100)
        for name in ('_focus_preview_host', '_update_zoom_controls', '_update_split_from_event',
                     '_refresh_viewport_display', '_update_pan_from_navigator', 'on_canvas_hover',
                     '_set_play_btn', '_schedule_full_preview', '_schedule_settings_save',
                     '_schedule_preview_cache_resume', '_cancel_after', 'display_view',
                     '_shared_feedback', '_shared_display', 'toggle_play', 'set_status',
                     '_start_strict_preview_buffering'):
            setattr(app, name, Mock())
        app._point_in_navigator = Mock(return_value=False)
        app._near_split = Mock(return_value=False)
        app._point_in_video = Mock(return_value=True)
        app._ui_color = lambda name, default: default
        return app

    def assert_preserved(self, app, original_queue, playing):
        self.assertEqual(app.playing, playing)
        self.assertTrue(app._pre_rendering)
        self.assertFalse(app._preview_cache_frozen)
        self.assertFalse(app._prefetch_stop.is_set())
        self.assertEqual(app._prefetch_gen, 7)
        self.assertEqual(app._queued_preview_frames, {0, 1})
        self.assertIs(app._preview_frame_queue, original_queue)
        self.assertEqual(app._preview_decode_after, 'decode-timer')
        self.assertEqual(app._active_preview_size, (8, 4))
        self.assertEqual(app._shared_clock, 123.)
        app._audio.pause.assert_not_called()
        app._background_preview_decoder.invalidate.assert_not_called()
        app._schedule_preview_cache_resume.assert_not_called()

    def gesture(self, app, kind):
        event = SimpleNamespace(x=10, y=10, state=0, action='copy')
        drag = SimpleNamespace(x=40, y=12, state=0)
        if kind in ('split', 'shift_split', 'compare_drag', 'pan', 'navigator'):
            app._near_split.return_value = kind == 'split'
            event.state = 1 if kind == 'shift_split' else 0
            app._preview_zoom = 2. if kind == 'pan' else 1.
            app._point_in_navigator.return_value = kind == 'navigator'
            app.on_canvas_press(event)
            app.on_canvas_drag(drag)
            app.on_canvas_release(drag)
        elif kind == 'zoom':
            app._set_preview_zoom(2.)
        elif kind == 'same_zoom':
            app._set_preview_zoom(1.)
        elif kind == 'reset_zoom':
            app.reset_preview_zoom()
        elif kind == 'resize':
            for _ in range(3):
                app._on_canvas_configure(SimpleNamespace(widget=app.canvas))
            app._present_play_frame = Mock()
            app._apply_canvas_resize()
            if not app.playing:
                app.display_view.assert_called_with(quality='fast')
        elif kind == 'view':
            for view in ('dlss', 'compare', 'dlss'):
                app.view_var.get.return_value = view
                app.on_view_change()
        elif kind == 'drop_hover':
            app._on_drop_enter(event)
            app._on_drop_leave(event)

    def test_display_gestures_preserve_paused_and_playing_producers(self):
        for playing in (False, True):
            for kind in ('split', 'shift_split', 'compare_drag', 'pan', 'navigator',
                         'zoom', 'same_zoom', 'reset_zoom', 'resize', 'view', 'drop_hover'):
                with self.subTest(playing=playing, gesture=kind):
                    app = self.make_app(playing)
                    original_queue = app._preview_frame_queue
                    self.gesture(app, kind)
                    self.assert_preserved(app, original_queue, playing)
                    app._schedule_full_preview.assert_not_called()
                    app.toggle_play.assert_not_called()

    def test_gesture_release_cannot_resume_an_unrelated_held_freeze(self):
        for kind in ('split', 'navigator', 'pan', 'drop_hover', 'view'):
            app = self.make_app()
            app._preview_cache_frozen = True
            self.gesture(app, kind)
            self.assertTrue(app._preview_cache_frozen)
            app._schedule_preview_cache_resume.assert_not_called()
            app._schedule_full_preview.assert_not_called()

    def test_original_to_processed_starts_missing_producer(self):
        app = self.make_app()
        app.view_var.get.return_value = 'original'
        app.on_view_change()
        self.assertFalse(app._pre_rendering)
        self.assertTrue(app._prefetch_stop.is_set())
        app.view_var.get.return_value = 'dlss'
        app.on_view_change()
        app._schedule_full_preview.assert_called_once()
        self.assertFalse(app._preview_cache_frozen)

    def test_view_change_preserves_pending_render_timer(self):
        app = self.make_app()
        app._pre_rendering = False
        app._scrub_after = 'pending-render'
        app.on_view_change()
        app._schedule_full_preview.assert_not_called()
        app._cancel_after.assert_not_called()

    def test_viewport_cache_miss_uses_nonblocking_display(self):
        app = self.make_app()
        app.view_var.get.return_value = 'dlss'
        App._refresh_viewport_display(app)
        app.display_view.assert_called_once_with(quality='fast')

    def export_app(self, sr=False, fg=False):
        app = self.make_app(playing=True)
        values = dict(super_resolution_scale=2, frame_generation_multiplier=2,
                      hdr_mode=True, output_resolution='source', custom_output_width=8,
                      custom_output_height=4, output_container='mp4', video_bitrate_mbps=20)
        app._export_settings = {
            'v_preview_super_resolution': Mock(get=Mock(return_value=sr)),
            'v_preview_frame_generation': Mock(get=Mock(return_value=fg)),
        }
        app._collect_export_settings = lambda: dict(values)
        app._last_export_preview_key = app._export_preview_key()
        # Widget painting is omitted; retain its production snapshot contract.
        app._update_export_control_states = lambda: setattr(
            app, '_last_export_preview_key', app._export_preview_key())
        app._on_effect_preview_change = Mock()
        return app, values

    def test_export_only_settings_and_duplicate_events_never_stop_preview(self):
        for sr, fg in ((False, False), (True, False), (False, True), (True, True)):
            app, values = self.export_app(sr, fg)
            original_queue = app._preview_frame_queue
            for key, value in (('output_container', 'mkv'), ('video_bitrate_mbps', 30),
                               ('quality_profile', 'high'), ('nvenc_preset', 'p7'),
                               ('mode', 'single'), ('parallel_workers', 4),
                               ('decode_buffer', 8), ('warmup_frames', 10)):
                values[key] = value
                app._on_export_settings_change()
                app._on_export_settings_change()  # FocusOut/Return without changes
                self.assert_preserved(app, original_queue, True)
            app._shared_feedback.assert_not_called()
            app._on_effect_preview_change.assert_not_called()

    def test_disabled_fg_multiplier_and_legacy_hdr_size_are_export_only(self):
        app, values = self.export_app()
        for key, value in (('frame_generation_multiplier', 4), ('hdr_mode', False),
                           ('output_resolution', 'custom'), ('custom_output_width', 16)):
            values[key] = value
            app._on_export_settings_change()
        self.assertTrue(app.playing)
        app._shared_feedback.assert_not_called()
        app._on_effect_preview_change.assert_not_called()

    def test_shared_display_gestures_preserve_session_and_playback_clock(self):
        for playing in (False, True):
            app, _ = self.export_app(sr=True, fg=True)
            app.playing = playing
            session = app._shared_session = object()
            original_queue = app._preview_frame_queue
            for kind in ('split', 'pan', 'navigator', 'zoom', 'resize', 'view', 'drop_hover'):
                self.gesture(app, kind)
                self.assert_preserved(app, original_queue, playing)
                self.assertIs(app._shared_session, session)
            app._shared_feedback.assert_not_called()

    def test_widget_state_refresh_records_key_for_real_export_callback(self):
        app, values = self.export_app(fg=True)
        values.update(super_resolution_scale=1, mode='single', rate_control='quality',
                      quality_profile='high')
        for name in ('workers', 'warmup', 'decode_buffer', 'mode', 'super_resolution',
                     'frame_generation', 'output_container', 'nvenc_preset', 'hdr',
                     'output_resolution', 'rate_control', 'custom_label', 'custom_width',
                     'custom_height', 'custom_frame', 'quality_label', 'quality_profile',
                     'bitrate_label', 'video_bitrate', 'hdr_hint'):
            app._export_settings['w_' + name] = Mock()
        app._set_grid_visible = Mock()
        app._update_export_control_states = App._update_export_control_states.__get__(app)
        app._update_export_control_states()
        self.assertEqual(app._last_export_preview_key, app._export_preview_key())
        values['output_container'] = 'mkv'
        app._on_export_settings_change()
        self.assertTrue(app.playing)
        values['frame_generation_multiplier'] = 4
        app._on_export_settings_change()
        self.assertFalse(app.playing)
        app._shared_feedback.assert_called_once()

    def test_equivalent_output_size_does_not_restart_shared_preview(self):
        app, values = self.export_app(fg=True)
        # Output boxes preserve aspect ratio and never upscale.
        values.update(output_resolution='custom', custom_output_width=16, custom_output_height=8)
        app._on_export_settings_change()
        self.assertTrue(app.playing)
        app._shared_feedback.assert_not_called()

    def test_export_and_queue_guard_prevents_preview_session_mutation(self):
        for busy in ('_exporting', '_queue_running'):
            app, values = self.export_app(fg=True)
            setattr(app, busy, True)
            values['frame_generation_multiplier'] = 4
            app._on_export_settings_change()
            self.assertTrue(app.playing)
            app._shared_feedback.assert_not_called()
            app._on_effect_preview_change.assert_not_called()

    def test_active_shared_pixel_changes_refresh_once(self):
        for key, value in (('frame_generation_multiplier', 4), ('hdr_mode', False),
                           ('output_resolution', 'custom')):
            app, values = self.export_app(fg=True)
            values['custom_output_width'] = 4
            values['custom_output_height'] = 2
            values[key] = value
            app._on_export_settings_change()
            self.assertFalse(app.playing)
            app._shared_feedback.assert_called_once()
            app._on_export_settings_change()
            app._shared_feedback.assert_called_once()

    def test_fg_multiplier_transitions_enter_and_leave_shared_pipeline(self):
        for old, new in ((1, 2), (2, 1)):
            app, values = self.export_app(fg=True)
            values['frame_generation_multiplier'] = old
            app._last_export_preview_key = app._export_preview_key()
            values['frame_generation_multiplier'] = new
            app._on_export_settings_change()
            app._on_effect_preview_change.assert_called_once()

    def test_effect_toggle_syncs_export_key_before_next_export_event(self):
        app, values = self.export_app()
        app._export_settings['v_preview_frame_generation'].get.return_value = True
        app._cache_clear = Mock()
        # Call the real toggle handler (not the spy installed in export_app).
        App._on_effect_preview_change(app)
        self.assertEqual(app._last_export_preview_key, app._export_preview_key())
        app._shared_feedback.reset_mock()
        app._on_export_settings_change()
        app._shared_feedback.assert_not_called()
        app._on_effect_preview_change.assert_not_called()

    def test_inflight_worker_output_survives_display_gestures(self):
        app = self.make_app()
        started, release, complete = threading.Event(), threading.Event(), threading.Event()
        app._hash_settings_dict = lambda _: ('settings',)
        app._super_resolution_scale = lambda _: 1
        app._cached_dlss_sk = lambda *_: None
        stored = []

        def process(rgba, **kwargs):
            started.set()
            if not release.wait(3):
                raise AssertionError('test failed to release renderer')
            return rgba

        def store(frame, sk, pixels):
            stored.append(frame)
            if len(stored) == 2:
                complete.set()

        app._ensure_live = lambda *_: SimpleNamespace(supports_async=False, process=process)
        app._cache_store = store
        for frame in (0, 1):
            app._preview_frame_queue.put((frame, np.zeros((4, 8, 3), np.uint8)))
        worker = threading.Thread(target=app._prefetch_job, args=(
            {}, (8, 4), app._preview_frame_queue, app._prefetch_stop, app._prefetch_gen))
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            for kind in ('split', 'pan', 'zoom', 'resize', 'view'):
                self.gesture(app, kind)
            release.set()
            self.assertTrue(complete.wait(3), getattr(app, '_preview_worker_error', 'output discarded'))
            self.assertEqual(stored, [0, 1])
        finally:
            release.set()
            app._prefetch_stop.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
