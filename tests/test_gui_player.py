import os
import queue
import tempfile
import threading
import unittest
from tkinter import messagebox

import numpy as np

import app_settings
from gui import (
    App, TimelineBar,
    _ExportCancelled,
    _clamp_frame, _decode_plan, _first_image, _format_duration, _format_timecode,
    _fit_preview_size, _frame_ranges, _realtime_preview_size,
    _is_image_path, _is_video_path, _play_target_frame, _read_image_bgr,
    _normalize_slider_input, _write_image_bgr, compose_preview_frame,
    effective_skin_settings, effective_slider,
)
from preview_audio import frame_to_ms, ms_to_frame
from video_export import compose_output_frame


class PlayerHelperTests(unittest.TestCase):
    def test_clamp_frame(self):
        self.assertEqual(_clamp_frame(-3, 10), 0)
        self.assertEqual(_clamp_frame(3.9, 10), 3)
        self.assertEqual(_clamp_frame(99, 10), 10)

    def test_queue_output_name_avoids_reserved_and_existing_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate = os.path.join(temp_dir, "clip_dlss.mp4")
            reserved = [candidate]
            second = App._unique_target_path(candidate, reserved)
            self.assertEqual(second, os.path.join(temp_dir, "clip_dlss_2.mp4"))
            open(second, "wb").close()
            third = App._unique_target_path(candidate, reserved)
            self.assertEqual(third, os.path.join(temp_dir, "clip_dlss_3.mp4"))
        self.assertEqual(_clamp_frame("nope", 10), 0)
        self.assertEqual(_clamp_frame(0, 0), 0)

    def test_format_timecode(self):
        self.assertEqual(_format_timecode(0, 24), "0:00.00")
        self.assertEqual(_format_timecode(12, 24), "0:00.50")
        self.assertEqual(_format_timecode(24, 24), "0:01.00")
        self.assertEqual(_format_timecode(24 * 60, 24), "1:00.00")

    def test_play_target_skips_ahead_and_stops_at_last(self):
        self.assertEqual(_play_target_frame(0, 0, 24, 242), 0)
        self.assertEqual(_play_target_frame(0, 1.0, 24, 242), 24)
        self.assertEqual(_play_target_frame(10, 0.2, 24, 242), 14)
        self.assertEqual(_play_target_frame(0, 30, 24, 242), 242)

    def test_decode_plan_prefers_sequential_reads(self):
        self.assertEqual(_decode_plan(None, 0), ("seek", 0))
        self.assertEqual(_decode_plan(12, 12), ("read", 0))
        self.assertEqual(_decode_plan(12, 15), ("skip", 3))
        self.assertEqual(_decode_plan(12, 40), ("seek", 0))
        self.assertEqual(_decode_plan(12, 5), ("seek", 0))

    def test_realtime_preview_size_downscales_4k_but_preserves_smaller_sources(self):
        self.assertEqual(_fit_preview_size(3840, 2160, 1920), (1920, 1080))
        self.assertEqual(_fit_preview_size(2160, 3840, 1920), (1080, 1920))
        self.assertEqual(_realtime_preview_size(3840, 2160, "auto"), (1920, 1080))
        self.assertEqual(_realtime_preview_size(2560, 1440, "auto"), (2560, 1440))
        self.assertEqual(_realtime_preview_size(3840, 2160, "1440p"), (2560, 1440))
        self.assertEqual(_realtime_preview_size(3840, 2160, "original"), (3840, 2160))

    def test_frame_ranges_compacts_non_contiguous_cache(self):
        self.assertEqual(_frame_ranges([]), [])
        self.assertEqual(_frame_ranges([5, 2, 3, 3, 8]), [(2, 3), (5, 5), (8, 8)])

    def test_first_image_does_not_evaluate_numpy_truth(self):
        arr = np.zeros((2, 2, 3), np.uint8)
        arr2 = np.ones((2, 2, 3), np.uint8)
        with self.assertRaises(ValueError):
            bool(arr or arr2)
        np.testing.assert_array_equal(_first_image(arr, arr2), arr)
        np.testing.assert_array_equal(_first_image(None, arr2), arr2)
        self.assertIsNone(_first_image(None, None))

    def test_format_duration(self):
        self.assertEqual(_format_duration(0), "0:00")
        self.assertEqual(_format_duration(65), "1:05")
        self.assertEqual(_format_duration(3661), "1:01:01")

    def test_audio_timestamp_roundtrip(self):
        self.assertEqual(frame_to_ms(0, 24), 0)
        self.assertEqual(frame_to_ms(24, 24), 1000)
        self.assertEqual(ms_to_frame(1000, 24, 242), 24)
        self.assertEqual(ms_to_frame(99999, 24, 242), 242)


class ImageIoTests(unittest.TestCase):
    def test_path_kind(self):
        self.assertTrue(_is_image_path("a.PNG"))
        self.assertTrue(_is_image_path("b.jpeg"))
        self.assertTrue(_is_video_path("c.mp4"))
        self.assertFalse(_is_image_path("c.mp4"))
        self.assertFalse(_is_video_path("a.png"))

    def test_write_and_read_png_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tiny.png")
            src = np.zeros((8, 12, 3), np.uint8)
            src[:] = (10, 20, 30)
            written = _write_image_bgr(path, src)
            self.assertTrue(os.path.isfile(written))
            loaded = _read_image_bgr(written)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.shape, src.shape)
            np.testing.assert_array_equal(loaded, src)

    def test_unique_image_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "shot.png")
            first = App._unique_output_path(src, ".png")
            self.assertTrue(first.endswith("_dlss.png"))
            open(first, "wb").close()
            second = App._unique_output_path(src, ".png")
            self.assertTrue(second.endswith("_dlss_2.png"))


class SettingsPanelPersistenceTests(unittest.TestCase):
    def test_ui_panel_flags_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dlss5_settings.json")
            saved = app_settings.save(
                {"ui_export_open": True, "ui_host_open": 1, "preview_view": "DLSS"},
                path=path,
            )
            self.assertTrue(saved["ui_export_open"])
            self.assertTrue(saved["ui_host_open"])
            loaded = app_settings.load(path)
            self.assertTrue(loaded["ui_export_open"])
            self.assertTrue(loaded["ui_host_open"])
            self.assertEqual(loaded["preview_view"], "DLSS")
            self.assertFalse(app_settings.validate({})["ui_preview_open"])

    def test_preview_cache_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dlss5_settings.json")
            saved = app_settings.save(
                {
                    "preview_quality": "1440p",
                    "preview_prefetch": 48,
                    "preview_cache": 160,
                    "preview_cache_mb": 4096,
                    "preview_scrub_ms": 20,
                },
                path=path,
            )
            self.assertEqual(saved["preview_quality"], "1440p")
            self.assertEqual(saved["preview_prefetch"], 48)
            self.assertEqual(saved["preview_cache"], 160)
            self.assertEqual(saved["preview_cache_mb"], 4096)
            self.assertEqual(saved["preview_scrub_ms"], 20)
            loaded = app_settings.load(path)
            self.assertEqual(loaded["preview_prefetch"], 48)
            self.assertEqual(loaded["preview_quality"], "1440p")
            self.assertEqual(loaded["preview_cache_mb"], 4096)
            self.assertEqual(app_settings.validate({"preview_quality": "bad"})["preview_quality"], "auto")

    def test_slider_toggles_default_off_and_roundtrip(self):
        loaded = app_settings.validate({"intensity": 0.9, "skin_struct": 0.8})
        self.assertFalse(loaded["use_intensity"])
        self.assertFalse(loaded["use_local_tone"])
        self.assertFalse(loaded["use_local_struct"])
        self.assertFalse(loaded["use_output_mix"])
        self.assertFalse(loaded["use_auto_mask"])
        self.assertEqual(loaded["intensity"], 0.9)
        self.assertEqual(loaded["skin_struct"], 0.8)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dlss5_settings.json")
            saved = app_settings.save(
                {
                    "use_intensity": True,
                    "intensity": 0.7,
                    "use_local_struct": 1,
                    "local_struct": 0.4,
                    "use_auto_mask": True,
                    "skin_struct": 0.8,
                },
                path=path,
            )
            self.assertTrue(saved["use_intensity"])
            self.assertEqual(saved["intensity"], 0.7)
            self.assertTrue(saved["use_local_struct"])
            self.assertTrue(saved["use_auto_mask"])
            loaded = app_settings.load(path)
            self.assertTrue(loaded["use_intensity"])
            self.assertEqual(loaded["intensity"], 0.7)
            self.assertTrue(loaded["use_auto_mask"])
            self.assertEqual(loaded["skin_struct"], 0.8)


class EffectiveSliderTests(unittest.TestCase):
    def test_closed_is_zero_open_keeps_value(self):
        self.assertEqual(effective_slider(False, 0.85), 0.0)
        self.assertEqual(effective_slider(True, 0.85), 0.85)
        self.assertEqual(effective_slider(False, "nope"), 0.0)
        self.assertEqual(effective_slider(True, "0.4"), 0.4)

    def test_zero_skin_strength_does_not_enable_auto_mask(self):
        self.assertEqual(effective_skin_settings(False, 0.8), (0, 0.0))
        self.assertEqual(effective_skin_settings(False, 0.0), (0, 0.0))
        self.assertEqual(effective_skin_settings(True, 0.0), (0, 0.0))
        self.assertEqual(effective_skin_settings(True, 0.8), (1, 0.8))

    def test_slider_input_requires_5x_permission_above_one_hundred_percent(self):
        self.assertEqual(_normalize_slider_input("0.73"), 0.73)
        self.assertEqual(_normalize_slider_input("75%"), 0.75)
        self.assertEqual(_normalize_slider_input("0.734"), 0.73)
        self.assertEqual(_normalize_slider_input("150%"), 1.0)
        self.assertEqual(_normalize_slider_input("500%"), 1.0)
        self.assertEqual(_normalize_slider_input("150%", max_value=5.0), 1.5)
        self.assertEqual(_normalize_slider_input("500%", max_value=5.0), 5.0)
        self.assertEqual(_normalize_slider_input("600%", max_value=5.0), 5.0)
        self.assertEqual(_normalize_slider_input("bad", fallback=0.8), 0.8)

    def test_persisted_slider_values_require_5x_permission(self):
        standard = app_settings.validate(
            {
                "intensity": 0.75,
                "local_tone": 1.0,
                "local_struct": 1.25,
                "skin_struct": 2.0,
                "output_mix": 6.0,
            }
        )
        self.assertFalse(standard["enable_5x"])
        self.assertEqual(standard["intensity"], 0.75)
        self.assertEqual(standard["local_tone"], 1.0)
        self.assertEqual(standard["local_struct"], 1.0)
        self.assertEqual(standard["skin_struct"], 1.0)
        self.assertEqual(standard["output_mix"], 1.0)

        experimental = app_settings.validate({
            "enable_5x": True,
            "local_struct": 1.25,
            "skin_struct": 2.0,
            "output_mix": 6.0,
        })
        self.assertTrue(experimental["enable_5x"])
        self.assertEqual(experimental["local_struct"], 1.25)
        self.assertEqual(experimental["skin_struct"], 2.0)
        self.assertEqual(experimental["output_mix"], 5.0)


class OutputViewTests(unittest.TestCase):
    def test_processed_uses_dlss_when_mix_is_one(self):
        orig = np.zeros((4, 8, 3), np.uint8)
        proc = np.full((4, 8, 3), 200, np.uint8)
        out = compose_output_frame(orig, proc, view=0, mix=1.0)
        np.testing.assert_array_equal(out, proc)

    def test_output_mix_above_one_amplifies_the_processed_residual(self):
        orig = np.full((2, 3, 3), 100, np.uint8)
        proc = np.full((2, 3, 3), 120, np.uint8)
        out = compose_output_frame(orig, proc, view=0, mix=5.0)
        np.testing.assert_array_equal(out, np.full_like(proc, 200))

    def test_diff_x10_is_midgray_when_identical(self):
        img = np.full((4, 8, 3), 40, np.uint8)
        out = compose_output_frame(img, img, view=1, mix=1.0)
        self.assertTrue(np.all((out == 127) | (out == 128)))

    def test_lr_compare_keeps_original_on_the_left(self):
        orig = np.zeros((4, 8, 3), np.uint8)
        proc = np.full((4, 8, 3), 255, np.uint8)
        out = compose_output_frame(orig, proc, view=2, mix=1.0)
        np.testing.assert_array_equal(out[:, :3], orig[:, :3])
        np.testing.assert_array_equal(out[:, 5:], proc[:, 5:])

    def test_preview_applies_output_mix_for_processed_view(self):
        orig = np.zeros((4, 8, 3), np.uint8)
        proc = np.full((4, 8, 3), 200, np.uint8)
        out = compose_preview_frame(orig, proc, output_view=0, output_mix=0.25)
        np.testing.assert_array_equal(out, np.full_like(proc, 50))

    def test_preview_ignores_mix_for_export_only_views(self):
        orig = np.zeros((4, 8, 3), np.uint8)
        proc = np.full((4, 8, 3), 200, np.uint8)
        out = compose_preview_frame(orig, proc, output_view=1, output_mix=0.25)
        np.testing.assert_array_equal(out, proc)

    def test_preview_resizes_original_to_proxy_before_mixing(self):
        orig = np.zeros((8, 16, 3), np.uint8)
        proc = np.full((4, 8, 3), 200, np.uint8)
        out = compose_preview_frame(orig, proc, output_view=0, output_mix=0.25)
        self.assertEqual(out.shape, proc.shape)
        np.testing.assert_array_equal(out, np.full_like(proc, 50))


class PreviewCacheTests(unittest.TestCase):
    def _app(self):
        app = App.__new__(App)
        app._dlss_frame_cache = {}
        app._source_frame_cache = {}
        app._queued_preview_frames = set()
        app._cache_lock = threading.RLock()
        app._dlss_cache_bytes = 0
        app._source_cache_bytes = 0
        app._live_cache = None
        app._last_shown_dlss = None
        app._preview_processed_frames = 0
        app._preview_process_t0 = None
        app._frame = 10
        app._media_w, app._media_h = 8, 4
        app._active_preview_size = (4, 2)
        app.fps = 24
        app._preview_prefetch = lambda: 24
        app._preview_cache_max = lambda: 96
        app._preview_cache_bytes = lambda: 1024 * 1024
        app._settings_hash = lambda: ("settings",)
        return app

    def test_cache_separates_proxy_and_exact_frames(self):
        app = self._app()
        sk = app._settings_hash()
        proxy = np.zeros((2, 4, 3), np.uint8)
        exact = np.ones((4, 8, 3), np.uint8)
        app._cache_store(10, sk, proxy)
        app._cache_store(10, sk, exact)
        self.assertIs(app._cached_dlss_sk(10, sk, (4, 2)), proxy)
        self.assertIs(app._cached_dlss_sk(10, sk, (8, 4)), exact)

    def test_live_preview_processes_the_requested_proxy_size(self):
        app = self._app()
        app._live_lock = threading.RLock()
        app._last_dlss_frame = -1
        app._collect_settings = lambda: {}
        app._hash_settings_dict = lambda settings: app._settings_hash()
        seen = []

        class FakeLive:
            def process(self, rgba, reset=False):
                seen.append((rgba.shape, reset))
                return rgba.copy()

        app._ensure_live = lambda width, height, settings: FakeLive()
        source = np.zeros((4, 8, 3), np.uint8)
        result = app._live_dlss_image(10, source_bgr=source, target_size=(4, 2))
        self.assertEqual(result.shape, (2, 4, 3))
        self.assertEqual(seen, [((2, 4, 4), 1)])

    def test_combined_source_and_dlss_cache_respects_ram_budget(self):
        app = self._app()
        app._preview_cache_bytes = lambda: 150
        source = np.zeros((4, 8, 3), np.uint8)
        proxy = np.zeros((2, 4, 3), np.uint8)
        app._source_cache_store(10, source)
        app._cache_store(10, app._settings_hash(), proxy)
        app._source_cache_store(11, source)
        self.assertLessEqual(app._source_cache_bytes + app._dlss_cache_bytes, 150)

    def test_large_ram_budget_extends_prerender_beyond_startup_buffer(self):
        app = self._app()
        app._media_w, app._media_h = 3840, 2160
        app._active_preview_size = (1920, 1080)
        app._preview_cache_bytes = lambda: 8 * 1024**3
        startup_frames = app._buffer_target_frames()
        capacity = app._cache_capacity_frames()
        self.assertEqual(startup_frames, 24)
        self.assertGreater(capacity, startup_frames)
        self.assertEqual(app._prerender_target_frames(), capacity - 3)

    def test_shadow_text_draws_offset_shadow_then_foreground(self):
        app = App.__new__(App)
        calls = []

        class FakeCanvas:
            def create_text(self, x, y, **kwargs):
                calls.append((x, y, kwargs))
                return len(calls)

        app.canvas = FakeCanvas()
        result = app._canvas_shadow_text(
            20, 30, "DLSS", fill="#ffffff", anchor="w",
        )
        self.assertEqual(result, 2)
        self.assertEqual(calls[0][:2], (21, 31))
        self.assertEqual(calls[0][2]["fill"], "#000000")
        self.assertEqual(calls[1][:2], (20, 30))
        self.assertEqual(calls[1][2]["fill"], "#ffffff")
        self.assertEqual([call[2]["text"] for call in calls], ["DLSS", "DLSS"])

    def test_pending_preview_is_visibly_not_the_original(self):
        original = np.full((4, 8, 3), (40, 120, 220), np.uint8)
        pending = App._pending_preview_image(original)
        self.assertEqual(pending.shape, original.shape)
        self.assertFalse(np.array_equal(pending, original))
        self.assertLess(float(pending.mean()), float(original.mean()) * 0.4)


class PreviewQueueTests(unittest.TestCase):
    def test_scrub_delay_triggers_precise_preview_and_paused_prerender(self):
        app = App.__new__(App)
        callbacks = []
        events = []

        class FakeRoot:
            def after(self, delay, callback):
                callbacks.append((delay, callback))
                return "after-id"

        app.root = FakeRoot()
        app._scrub_after = None
        app._preview_scrub_ms = lambda: 75
        app._cancel_after = lambda name: events.append(("cancel", name))
        app.playing = False
        app.video = "video.mp4"
        app._exporting = False
        app._display_precise_preview = lambda: events.append(("precise", None))
        app._start_paused_prerender = lambda: events.append(("prerender", None)) or True
        app._update_preview_timeline_and_status = (
            lambda force=False: events.append(("status", force))
        )

        app._schedule_full_preview()
        self.assertEqual(callbacks[0][0], 75)
        self.assertIs(callbacks[0][1].__self__, app)
        callbacks[0][1]()
        self.assertEqual(
            events,
            [
                ("cancel", "_scrub_after"),
                ("precise", None),
                ("prerender", None),
                ("status", True),
            ],
        )

    def test_paused_prerender_can_start_without_playback(self):
        app = App.__new__(App)
        calls = []
        source = np.zeros((4, 8, 3), np.uint8)

        class FakeVar:
            def get(self):
                return "DLSS"

        app.playing = False
        app.video = "video.mp4"
        app._exporting = False
        app._source_kind = "video"
        app.view_var = FakeVar()
        app._hold_original = False
        app._frame = 12
        app._pre_rendering = False
        app._stop_paused_prerender = lambda: calls.append("stop")
        app._playback_preview_size = lambda: (4, 2)
        app._source_cache_get = lambda frame: source
        app._source_cache_store = lambda frame, bgr: calls.append(("store", frame))
        app._start_prefetch = lambda: calls.append("worker")
        app._queue_preview_frame = lambda frame, bgr: calls.append(("queue", frame))
        app._schedule_preview_decode = lambda delay=1: calls.append(("decode", delay))

        self.assertTrue(app._start_paused_prerender())
        self.assertTrue(app._pre_rendering)
        self.assertEqual(app._active_preview_size, (4, 2))
        self.assertEqual(
            calls,
            ["stop", ("store", 12), "worker", ("queue", 12), ("decode", 0)],
        )

    def test_preview_worker_consumes_frames_without_a_second_decoder(self):
        app = App.__new__(App)
        app._prefetch_gen = 7
        app._play_dlss_busy = True
        app._frame = 10
        app._live_lock = threading.RLock()
        app._last_dlss_frame = -1
        app._live_error = None
        app._cache_lock = threading.RLock()
        app._queued_preview_frames = {10}
        app._hash_settings_dict = lambda settings: ("settings",)
        app._cached_dlss_sk = lambda frame, settings, size: None
        stop = threading.Event()
        processed = []

        class FakeLive:
            supports_async = False
            max_in_flight = 1

            def process(self, rgba, reset=False):
                stop.set()
                return rgba.copy()

        app._ensure_live = lambda width, height, settings: FakeLive()
        app._cache_store = lambda frame, settings, bgr: processed.append((frame, bgr.shape))
        frames = queue.Queue(maxsize=3)
        source = np.zeros((4, 8, 3), np.uint8)
        frames.put_nowait((10, source))
        app._prefetch_job({}, (8, 4), frames, stop, 7)
        self.assertEqual(len(processed), 1)
        self.assertEqual(processed[0], (10, (4, 8, 3)))
        self.assertFalse(app._play_dlss_busy)

    def test_preview_worker_uses_async_in_flight_order(self):
        app = App.__new__(App)
        app._prefetch_gen = 3
        app._play_dlss_busy = True
        app._frame = 20
        app._live_lock = threading.RLock()
        app._last_dlss_frame = -1
        app._live_error = None
        app._cache_lock = threading.RLock()
        app._queued_preview_frames = {20, 21, 22}
        app._hash_settings_dict = lambda settings: ("settings",)
        app._cached_dlss_sk = lambda frame, settings, size: None
        stop = threading.Event()
        submitted = []
        outputs = queue.Queue()
        stored = []

        class FakeAsyncLive:
            supports_async = True
            max_in_flight = 2

            def enqueue(self, rgba, reset=False):
                submitted.append(reset)
                outputs.put(rgba.copy())
                if len(submitted) == 3:
                    stop.set()
                return True

            def dequeue(self):
                return outputs.get_nowait()

        live = FakeAsyncLive()
        app._ensure_live = lambda width, height, settings: live
        app._cache_store = lambda frame, settings, bgr: stored.append(frame)
        frames = queue.Queue(maxsize=3)
        source = np.zeros((4, 8, 3), np.uint8)
        for frame in (20, 21, 22):
            frames.put_nowait((frame, source))
        app._prefetch_job({}, (8, 4), frames, stop, 3)
        self.assertEqual(stored, [20, 21, 22])
        self.assertEqual(submitted, [True, False, False])


class WidgetSmokeTests(unittest.TestCase):
    def test_app_and_timeline_construct(self):
        import tkinter as tk

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dlss5_settings.json")
            queue_path = os.path.join(tmp, "dlss5_queue.json")
            old = os.environ.get("DLSS5TOOL_SETTINGS_PATH")
            old_queue = os.environ.get("DLSS5TOOL_QUEUE_PATH")
            os.environ["DLSS5TOOL_SETTINGS_PATH"] = path
            os.environ["DLSS5TOOL_QUEUE_PATH"] = queue_path
            root = tk.Tk()
            root.withdraw()
            try:
                app = App(root)
                self.assertEqual(app._frame, 0)
                self.assertFalse(app._fullscreen)
                self.assertEqual(str(app.fs_btn.cget("text")), "全屏")
                self.assertTrue(hasattr(app, "import_btn"))
                self.assertTrue(hasattr(app, "clear_btn"))
                self.assertTrue(hasattr(app, "cancel_export_btn"))
                self.assertTrue(hasattr(app, "workspace_tabs"))
                self.assertTrue(hasattr(app, "queue_tree"))
                self.assertTrue(hasattr(app, "queue_start_btn"))
                self.assertEqual(str(app.cancel_export_btn.cget("text")), "取消导出")
                self.assertTrue(app.cancel_export_btn.instate(["disabled"]))
                self.assertTrue(hasattr(app, "eta_label"))
                self.assertEqual(str(app.eta_label.cget("text")), "")
                self.assertFalse(hasattr(app, "status") and app.status is not app.eta_label)
                app.set_status("就绪")
                self.assertEqual(str(app.eta_label.cget("text")), "")
                app.set_status("正在准备 DLSS 预览…")
                self.assertEqual(str(app.eta_label.cget("text")), "正在准备 DLSS 预览…")
                app.set_status("就绪")
                self.assertEqual(str(app.eta_label.cget("text")), "")
                self.assertTrue(app.clear_btn.instate(["disabled"]))
                export = app._collect_export_settings()
                self.assertEqual(export["output_resolution"], "source")
                self.assertEqual(export["rate_control"], "quality")
                self.assertEqual(export["quality_profile"], "high")
                app._export_settings["v_rate_control"].set("目标码率")
                app._export_settings["v_output_resolution"].set("自定义上限")
                app._on_export_settings_change()
                self.assertTrue(app._export_settings["w_quality_profile"].instate(["disabled"]))
                self.assertTrue(app._export_settings["w_video_bitrate"].instate(["!disabled"]))
                self.assertTrue(app._export_settings["w_custom_width"].instate(["!disabled"]))
                app.video = "dummy.mp4"
                app._update_action_labels()
                self.assertTrue(app.clear_btn.instate(["!disabled"]))
                self.assertTrue(app.cancel_export_btn.instate(["disabled"]))
                app._exporting = True
                app._update_action_labels()
                self.assertTrue(app.export_btn.instate(["disabled"]))
                self.assertTrue(app.cancel_export_btn.instate(["!disabled"]))
                app.cancel_export()
                self.assertTrue(app._export_cancel_event.is_set())
                self.assertEqual(str(app.cancel_export_btn.cget("text")), "取消中…")
                self.assertTrue(app.cancel_export_btn.instate(["disabled"]))
                self.assertEqual(str(app.eta_label.cget("text")), "正在取消导出…")
                with self.assertRaises(_ExportCancelled):
                    app._raise_if_export_cancelled()
                partial = os.path.join(tmp, "partial.mp4")
                open(partial, "wb").close()
                app._end_export_ui(False, partial, cancelled=True)
                self.assertFalse(os.path.exists(partial))
                self.assertFalse(app._export_cancel_event.is_set())
                self.assertEqual(
                    str(app.eta_label.cget("text")),
                    "导出已取消，未完成文件已清理",
                )
                app.view_var.set("对比")
                app._hold_original = False
                app._split_nw, app._split_nh = 100, 50
                app._split_orig = np.zeros((50, 100, 3), np.uint8)
                app._split_dlss = None
                app._dlss_pending = True
                app._blit_split(120, 70)
                self.assertTrue(app.canvas.find_withtag("split"))

                updates = []
                app._update_split_from_event = lambda event: updates.append((event.x, event.y))
                event = type("Event", (), {"x": 10, "y": 20, "state": 0})()
                app.on_canvas_press(event)
                self.assertFalse(app._drag_split)
                drag = type("Event", (), {"x": 30, "y": 21, "state": 0})()
                app.on_canvas_drag(drag)
                self.assertTrue(app._drag_split)
                self.assertEqual(updates, [(30, 21)])
                app.on_canvas_release(drag)
                click = type("Event", (), {"x": 15, "y": 20, "state": 0})()
                app.on_canvas_press(click)
                app.on_canvas_release(click)
                self.assertEqual(updates[-1], (15, 20))
                app.clear_media()
                self.assertIsNone(app.video)
                self.assertTrue(app.clear_btn.instate(["disabled"]))
                self.assertTrue(app._preview_section.collapsed)
                self.assertEqual(app._preview_settings["v_quality"].get(), "自动（推荐）")
                self.assertTrue(app._export_section.collapsed)
                self.assertTrue(app._host_section.collapsed)
                packed = list(app.root.pack_slaves())
                self.assertIn(app.workspace_tabs, packed)
                preview_packed = list(app.preview_tab.pack_slaves())
                self.assertEqual(
                    preview_packed,
                    [
                        app._settings_frame, app._preview_section,
                        app._export_section, app._host_section,
                        app._export_row,
                    ],
                )
                self.assertIs(packed[-1], getattr(app.log, "frame", app.log))
                self.assertEqual(app.queue_tree.get_children(""), ())
                self.assertTrue(app.queue_start_btn.instate(["disabled"]))
                source = os.path.join(tmp, "queued.mp4")
                open(source, "wb").close()
                app._probe_queue_video = lambda _path: (
                    {"frames": 48, "fps": 24.0, "width": 1920, "height": 1080},
                    {"is_hdr": False, "label": "SDR / sRGB"},
                )
                self.assertEqual(app._add_paths_to_queue([source], switch_tab=False), 1)
                self.assertEqual(app._add_paths_to_queue([source], switch_tab=False), 0)
                self.assertEqual(len(app._queue_jobs), 1)
                queued = app._queue_jobs[0]
                self.assertEqual(queued.state, "pending")
                self.assertEqual(queued.progress_total, 0)
                self.assertEqual(queued.metadata["frames"], 48)
                self.assertIn(queued.job_id, app.queue_tree.get_children(""))
                self.assertTrue(app.queue_start_btn.instate(["!disabled"]))
                queued.state = "failed"
                queued.error = "test failure"
                app._refresh_queue_tree()
                app.queue_tree.selection_set(queued.job_id)
                app.retry_selected_queue_jobs()
                self.assertEqual(queued.state, "pending")
                self.assertEqual(queued.progress_total, 48)
                app.queue_tree.selection_set(queued.job_id)
                app.remove_selected_queue_jobs()
                self.assertEqual(app._queue_jobs, [])

                hdr_source = os.path.join(tmp, "queued-hdr.mp4")
                open(hdr_source, "wb").close()
                app._export_settings["v_mode"].set("视觉无损（并行分段）")
                app._probe_queue_video = lambda _path: (
                    {"frames": 24, "fps": 24.0, "width": 1280, "height": 720},
                    {"is_hdr": True, "label": "HDR10 / PQ", "profile": "pq"},
                )
                self.assertEqual(
                    app._add_paths_to_queue([hdr_source], switch_tab=False), 1,
                )
                self.assertEqual(app._queue_jobs[0].export_settings["mode"], "single")
                app._queue_jobs = []
                app._save_queue_state()
                app._refresh_queue_tree(keep_selection=False)
                app._export_settings["v_mode"].set("严格时序（单会话）")
                app._probe_queue_video = lambda _path: (
                    {"frames": 48, "fps": 24.0, "width": 1920, "height": 1080},
                    {"is_hdr": False, "label": "SDR / sRGB"},
                )

                second = os.path.join(tmp, "queued-second.mp4")
                open(second, "wb").close()
                self.assertEqual(
                    app._add_paths_to_queue([source, second], switch_tab=False), 2,
                )
                callbacks = []
                original_after = app.root.after
                original_showinfo = messagebox.showinfo
                app.root.after = lambda _delay, callback: callbacks.append(callback)
                messagebox.showinfo = lambda *args, **kwargs: None
                app._export_video_source = lambda path, **kwargs: {
                    "success": path == source,
                    "cancelled": False,
                    "error": "simulated failure" if path == second else "",
                    "output_path": kwargs["out_path"],
                    "frames": 48,
                }
                try:
                    app._queue_running = True
                    app._run_next_queue_job()
                    self.assertEqual(app._queue_jobs[0].state, "completed")
                    self.assertEqual(len(callbacks), 1)
                    callbacks.pop(0)()
                    self.assertEqual(app._queue_jobs[1].state, "failed")
                    self.assertEqual(app._queue_jobs[1].error, "simulated failure")
                    callbacks.pop(0)()
                    self.assertFalse(app._queue_running)
                finally:
                    app.root.after = original_after
                    messagebox.showinfo = original_showinfo
                app._queue_jobs = []
                app._save_queue_state()
                app._refresh_queue_tree(keep_selection=False)
                app.timeline.set_range(0, 242)
                app.timeline.set(12)
                self.assertEqual(app.timeline.get(), 12)
                self.assertEqual(_format_timecode(app.timeline.get(), 24), "0:00.50")
                app._export_section.toggle()
                self.assertFalse(app._export_section.collapsed)
                bar = TimelineBar(root)
                bar.set_range(0, 10)
                bar.set(10)
                self.assertEqual(bar.get(), 10)
                bar.set(-3)
                self.assertEqual(bar.get(), 0)
                app._cancel_after("_settings_save_after")
                root.update_idletasks()
            finally:
                root.destroy()
                if old is None:
                    os.environ.pop("DLSS5TOOL_SETTINGS_PATH", None)
                else:
                    os.environ["DLSS5TOOL_SETTINGS_PATH"] = old
                if old_queue is None:
                    os.environ.pop("DLSS5TOOL_QUEUE_PATH", None)
                else:
                    os.environ["DLSS5TOOL_QUEUE_PATH"] = old_queue

    def test_closed_switch_sends_zero_and_remembers(self):
        import tkinter as tk

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dlss5_settings.json")
            queue_path = os.path.join(tmp, "dlss5_queue.json")
            app_settings.save(
                {
                    "intensity": 0.9,
                    "use_intensity": False,
                    "local_tone": 0.6,
                    "use_local_tone": True,
                    "local_struct": 0.85,
                    "use_local_struct": False,
                    "output_mix": 1.0,
                    "use_output_mix": False,
                    "use_auto_mask": False,
                    "skin_struct": 0.8,
                },
                path=path,
            )
            old = os.environ.get("DLSS5TOOL_SETTINGS_PATH")
            old_queue = os.environ.get("DLSS5TOOL_QUEUE_PATH")
            os.environ["DLSS5TOOL_SETTINGS_PATH"] = path
            os.environ["DLSS5TOOL_QUEUE_PATH"] = queue_path
            root = tk.Tk()
            root.withdraw()
            try:
                app = App(root)
                live = app._collect_settings()
                self.assertEqual(live["intensity"], 0.0)
                self.assertAlmostEqual(live["local_tone"], 0.6)
                self.assertEqual(live["local_struct"], 0.0)
                self.assertEqual(live["output_mix"], 0.0)
                self.assertEqual(live["skin_struct"], 0.0)
                self.assertEqual(live["use_auto_mask"], 0)
                remembered = app._collect_persisted_settings()
                self.assertAlmostEqual(remembered["intensity"], 0.9)
                self.assertFalse(remembered["use_intensity"])
                self.assertFalse(remembered["enable_5x"])
                self.assertAlmostEqual(remembered["skin_struct"], 0.8)
                self.assertFalse(remembered["use_auto_mask"])
                self.assertEqual(str(app._settings["w_intensity"].cget("state")), "disabled")
                self.assertEqual(str(app._settings["w_local_tone"].cget("state")), "normal")
                self.assertEqual(str(app._settings["w_skin_struct"].cget("state")), "disabled")
                self.assertEqual(float(app._settings["w_intensity"].cget("to")), 1.0)
                self.assertEqual(float(app._settings["w_intensity"].cget("resolution")), 0.01)
                self.assertTrue(app._settings["w_intensity_value"].instate(["disabled"]))
                self.assertEqual(
                    str(app._settings["w_intensity"].cget("troughcolor")).lower(),
                    "#e6e6e6",
                )
                self.assertEqual(
                    str(app._settings["w_local_tone"].cget("troughcolor")).lower(),
                    "#5b8fad",
                )
                root.update_idletasks()
                intensity_w = app._settings["w_intensity"].master.winfo_width()
                mix_w = app._settings["w_outmix"].master.winfo_width()
                tone_w = app._settings["w_local_tone"].master.winfo_width()
                self.assertGreater(intensity_w, 0)
                self.assertEqual(intensity_w, mix_w)
                self.assertEqual(intensity_w, tone_w)

                app._settings["v_enable_5x"].set(True)
                app._on_5x_toggle()
                self.assertEqual(float(app._settings["w_intensity"].cget("to")), 5.0)
                self.assertIn("0%–500%", str(app._settings["w_range_hint"].cget("text")))
                app._settings["v_intensity"].set(4.0)
                app._settings["v_enable_5x"].set(False)
                app._on_5x_toggle()
                self.assertEqual(float(app._settings["w_intensity"].cget("to")), 1.0)
                self.assertEqual(float(app._settings["v_intensity"].get()), 1.0)
                app._settings["v_intensity"].set(0.9)

                app._settings["v_use_intensity"].set(True)
                app._settings["v_auto_mask"].set(True)
                app._update_dlss_control_states()
                live = app._collect_settings()
                self.assertAlmostEqual(live["intensity"], 0.9)
                self.assertAlmostEqual(live["skin_struct"], 0.8)
                self.assertEqual(live["use_auto_mask"], 1)

                app._settings["v_skin_struct"].set(0.0)
                skin_zero_on = app._collect_settings()
                app._settings["v_auto_mask"].set(False)
                skin_zero_off = app._collect_settings()
                self.assertEqual(skin_zero_on["use_auto_mask"], 0)
                self.assertEqual(skin_zero_on["skin_struct"], 0.0)
                self.assertEqual(
                    app._hash_settings_dict(skin_zero_on),
                    app._hash_settings_dict(skin_zero_off),
                )

                app._settings["v_auto_mask"].set(True)
                app._settings["v_skin_struct"].set(0.8)
                self.assertEqual(str(app._settings["w_intensity"].cget("state")), "normal")
                self.assertEqual(str(app._settings["w_skin_struct"].cget("state")), "normal")
                self.assertTrue(app._settings["w_intensity_value"].instate(["!disabled"]))
                self.assertEqual(
                    str(app._settings["w_intensity"].cget("troughcolor")).lower(),
                    "#5b8fad",
                )
                app._settings["w_intensity_value"].set("75%")
                live = app._collect_settings()
                self.assertAlmostEqual(live["intensity"], 0.75)
                self.assertEqual(app._settings["w_intensity_value"].get(), "0.75")

                app._settings["v_outview"].set("差异×10")
                app._update_dlss_control_states()
                self.assertEqual(str(app._settings["w_outmix"].cget("state")), "disabled")
                app._cancel_after("_settings_save_after")
                root.update_idletasks()
            finally:
                root.destroy()
                if old is None:
                    os.environ.pop("DLSS5TOOL_SETTINGS_PATH", None)
                else:
                    os.environ["DLSS5TOOL_SETTINGS_PATH"] = old
                if old_queue is None:
                    os.environ.pop("DLSS5TOOL_QUEUE_PATH", None)
                else:
                    os.environ["DLSS5TOOL_QUEUE_PATH"] = old_queue


if __name__ == "__main__":
    unittest.main()
