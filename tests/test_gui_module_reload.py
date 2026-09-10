"""Regression tests for inference-setting changes during an active preview."""
import queue
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from dlss5tool.gui import App


class MainThreadRoot:
    def __init__(self):
        self.callbacks = {}
        self.sequence = 0

    def after(self, delay, callback):
        assert threading.current_thread() is threading.main_thread(), 'Tk from worker'
        self.sequence += 1
        self.callbacks[self.sequence] = callback
        return self.sequence

    def after_cancel(self, handle):
        self.callbacks.pop(handle, None)

    def tick(self):
        callbacks = list(self.callbacks.values())
        self.callbacks.clear()
        for callback in callbacks:
            callback()


class ModuleReloadTests(unittest.TestCase):
    def setUp(self):
        self.app = app = App.__new__(App)
        app.root = MainThreadRoot()
        app._exporting = app._queue_running = app._switching_backend = app._diagnosing = False
        app._module_reload_thread = app._module_reload_after = None
        app._close_after_module_reload = False
        app._guidance_generation = 0
        app._guidance_events = queue.SimpleQueue()
        app._guidance_events_after = None
        app._live_lock = threading.RLock()
        app._cache_lock = threading.RLock()
        app._prefetch_stop = threading.Event()
        app._prefetch_gen = 1
        app._queued_preview_frames = {0}
        app._preview_cache_frozen = False
        app._preview_cache_resume_after = None
        app._play_dlss_thread = None
        app._play_dlss_busy = False
        app._live = None
        app.video = 'fixture.mp4'
        app._last_module_settings = {'guidance_depth_profile': 'fp32'}
        app._collect_host_settings = lambda: {'guidance_depth_profile': 'sdpa_fp16'}
        for name in ('pause', '_cache_clear', '_schedule_settings_save', '_refresh_status_chips',
                     '_update_host_control_states', '_update_action_labels',
                     '_update_queue_action_states', 'set_status', 'logln'):
            setattr(app, name, mock.Mock())
        app._resume_preview_cache = mock.Mock()

    def wait_reload(self):
        deadline = time.monotonic() + 3
        while self.app._module_reload_thread is not None and time.monotonic() < deadline:
            self.app.root.tick()
            time.sleep(0.005)
        self.assertIsNone(self.app._module_reload_thread)

    def test_notifications_do_not_call_tk_and_drop_retired_generation(self):
        app = self.app
        info = {'device': 'cuda', 'precision': 'float32'}
        thread = threading.Thread(target=lambda: app._guidance_started(info, 0))
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        info['precision'] = 'mutated'
        app._poll_guidance_events()
        self.assertEqual(app._last_guidance_info['precision'], 'float32')
        app.logln.reset_mock()
        app._guidance_generation = 1
        app._guidance_started({'device': 'cuda', 'precision': 'old'}, 0)
        app._guidance_started({'device': 'cuda', 'precision': 'float16_amp'}, 1)
        app._poll_guidance_events()
        self.assertEqual(app._last_guidance_info['precision'], 'float16_amp')
        app.logln.assert_called_once()

    def test_switch_returns_while_preview_and_engine_close_are_blocked(self):
        app = self.app
        acquired, release = threading.Event(), threading.Event()
        closing, close_release = threading.Event(), threading.Event()

        def close():
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            closing.set()
            close_release.wait(3)

        app._live = mock.Mock(close=close)

        def preview():
            with app._live_lock:
                acquired.set()
                release.wait(3)
                app._guidance_started({'device': 'cuda', 'precision': 'fp32'}, 0)

        previous = threading.Thread(target=preview, daemon=True)
        app._play_dlss_thread = previous
        previous.start()
        self.assertTrue(acquired.wait(1))
        try:
            started = time.monotonic()
            app._on_mod_settings_change()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(app._switching_backend)
            self.assertTrue(app._preview_cache_frozen)
            self.assertTrue(app._prefetch_stop.is_set())
            self.assertEqual(app._prefetch_gen, 2)
            self.assertIs(app._play_dlss_thread, previous)
            self.assertFalse(closing.is_set())
            reload_thread = app._module_reload_thread
            app._on_mod_settings_change()  # duplicate callback cannot start a new session
            self.assertIs(app._module_reload_thread, reload_thread)
            app._schedule_preview_cache_resume(0)
            self.assertIsNone(app._preview_cache_resume_after)
            App._resume_preview_cache(app)
            self.assertTrue(app._preview_cache_frozen)
            app._refresh_dlss()  # a pending slider callback must not update the old engine
            app._live.update.assert_not_called()
            self.assertIsNone(app._live_dlss_image(0))
            release.set()
            self.assertTrue(closing.wait(1))
            ticks = []
            app.root.after(0, lambda: ticks.append('responsive'))
            app.root.tick()
            self.assertEqual(ticks, ['responsive'])
            self.assertTrue(app._switching_backend)
        finally:
            release.set()
            close_release.set()
            previous.join(1)
            self.wait_reload()
        self.assertFalse(app._switching_backend)
        self.assertIsNone(app._live)
        self.assertIsNone(app._play_dlss_thread)
        app.root.tick()
        app._resume_preview_cache.assert_called_once()
        app._poll_guidance_events()
        app.logln.assert_not_called()  # the retiring FP32 session must not be reported as new

    def test_wait_timeout_preserves_running_worker_reference(self):
        app = self.app
        release = threading.Event()
        previous = threading.Thread(target=lambda: release.wait(3), daemon=True)
        app._play_dlss_thread = previous
        previous.start()
        try:
            self.assertFalse(app._wait_play_dlss(timeout=0))
            self.assertIs(app._play_dlss_thread, previous)
            self.assertTrue(app._play_dlss_busy)
        finally:
            release.set()
            previous.join(1)
        self.assertTrue(app._wait_play_dlss(timeout=0))
        self.assertIsNone(app._play_dlss_thread)

    def test_reload_failure_unlocks_controls_without_resuming(self):
        app = self.app
        app._close_live = mock.Mock(side_effect=RuntimeError('fixture failure'))
        app._on_mod_settings_change()
        self.wait_reload()
        self.assertFalse(app._switching_backend)
        self.assertIsNone(app._last_module_settings)
        self.assertTrue(app._preview_cache_frozen)
        self.assertIn('fixture failure', app.logln.call_args.args[0])
        app._resume_preview_cache.assert_not_called()

    def test_notification_created_during_retirement_is_discarded(self):
        app = self.app
        app._module_reload_thread = mock.Mock()
        app._guidance_started({'device': 'cuda', 'precision': 'old'}, 0)
        app._poll_guidance_events()
        app.logln.assert_not_called()
        app._guidance_started({'device': 'cuda', 'precision': 'late'}, 0)
        result = queue.SimpleQueue()
        result.put(None)
        app._poll_module_reload(result)
        app._poll_guidance_events()
        app.logln.assert_not_called()

    def test_close_request_is_deferred_until_reload_finishes(self):
        app = self.app
        app._module_reload_thread = mock.Mock()
        App._on_close(app)
        self.assertTrue(app._close_after_module_reload)
        app._on_close = mock.Mock()
        result = queue.SimpleQueue()
        result.put(None)
        app._poll_module_reload(result)
        app._on_close.assert_called_once()
        app._resume_preview_cache.assert_not_called()

    def test_busy_shortcuts_cannot_start_conflicting_operations(self):
        app = self.app
        app._switching_backend = True
        for action in (app.play, app.export_dlss, app.start_export_queue,
                       app.import_media, app.clear_media):
            action()
        self.assertFalse(app._load_media('does-not-exist.mp4'))

    def test_real_tk_loop_keeps_ticking_during_reload(self):
        import tkinter as tk

        app = self.app
        root = tk.Tk()
        root.withdraw()
        app.root = root
        acquired, release = threading.Event(), threading.Event()
        ticks = []
        errors = []
        root.report_callback_exception = lambda *error: errors.append(error)

        def preview():
            with app._live_lock:
                acquired.set()
                release.wait(2)
                app._guidance_started({'device': 'cuda', 'precision': 'fp32'}, 0)

        previous = threading.Thread(target=preview, daemon=True)
        app._play_dlss_thread = previous
        previous.start()
        self.assertTrue(acquired.wait(1))

        def change():
            app._on_mod_settings_change()
            root.after(10, lambda: ticks.append('responsive'))
            root.after(30, release.set)

        try:
            root.after(0, change)
            root.after(250, root.quit)
            root.mainloop()
            self.assertEqual(errors, [])
            self.assertEqual(ticks, ['responsive'])
            self.assertFalse(app._switching_backend)
            self.assertIsNone(app._play_dlss_thread)
        finally:
            release.set()
            previous.join(1)
            if app._module_reload_thread:
                app._module_reload_thread.join(1)
            for handle in root.tk.call('after', 'info'):
                root.after_cancel(handle)
            root.destroy()

    def test_real_controls_restore_after_switching_both_directions(self):
        import tkinter as tk
        from dlss5tool import gui

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            'DLSS5TOOL_SETTINGS_PATH': os.path.join(directory, 'settings.json'),
            'DLSS5TOOL_QUEUE_PATH': os.path.join(directory, 'queue.json'),
        }):
            gui.app_settings.save({'guidance_mode': 0})
            root = tk.Tk()
            root.withdraw()
            app = App(root)
            errors = []
            root.report_callback_exception = lambda *error: errors.append(error)
            try:
                for profile in ('fp32', 'sdpa_fp16'):
                    app._last_module_settings = app._collect_host_settings()
                    app._host_settings['v_depth_profile'].set(gui.tr('guidance.option.' + profile))
                    release, closing = threading.Event(), threading.Event()

                    def close():
                        closing.set()
                        release.wait(3)

                    app._live = mock.Mock(close=close)
                    try:
                        app._on_mod_settings_change()
                        self.assertTrue(closing.wait(1))
                        self.assertTrue(app._switching_backend)
                        self.assertEqual(str(app._host_settings['w_guidance'].cget('state')), 'disabled')
                        self.assertTrue(app.import_btn.instate(['disabled']))
                        root.update()
                    finally:
                        release.set()
                    deadline = time.monotonic() + 3
                    while app._module_reload_thread is not None and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.005)
                    self.assertIsNone(app._module_reload_thread)
                    self.assertFalse(app._switching_backend)
                    self.assertEqual(str(app._host_settings['w_guidance'].cget('state')), 'readonly')
                    self.assertFalse(app.import_btn.instate(['disabled']))
                    self.assertEqual(app._collect_host_settings()['guidance_depth_profile'], profile)
                self.assertEqual(errors, [])
            finally:
                if app._module_reload_thread:
                    app._module_reload_thread.join(3)
                for handle in root.tk.call('after', 'info'):
                    root.after_cancel(handle)
                root.destroy()


if __name__ == '__main__':
    unittest.main()
