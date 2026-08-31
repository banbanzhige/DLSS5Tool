import os
import tempfile
import unittest

import numpy as np

import app_settings
from gui import (
    App, TimelineBar,
    _clamp_frame, _decode_plan, _first_image, _format_duration, _format_timecode,
    _is_image_path, _is_video_path, _play_target_frame, _read_image_bgr,
    _write_image_bgr, effective_slider,
)
from preview_audio import frame_to_ms, ms_to_frame
from video_export import compose_output_frame


class PlayerHelperTests(unittest.TestCase):
    def test_clamp_frame(self):
        self.assertEqual(_clamp_frame(-3, 10), 0)
        self.assertEqual(_clamp_frame(3.9, 10), 3)
        self.assertEqual(_clamp_frame(99, 10), 10)
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
                {"preview_prefetch": 48, "preview_cache": 160, "preview_scrub_ms": 20},
                path=path,
            )
            self.assertEqual(saved["preview_prefetch"], 48)
            self.assertEqual(saved["preview_cache"], 160)
            self.assertEqual(saved["preview_scrub_ms"], 20)
            loaded = app_settings.load(path)
            self.assertEqual(loaded["preview_prefetch"], 48)

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


class OutputViewTests(unittest.TestCase):
    def test_processed_uses_dlss_when_mix_is_one(self):
        orig = np.zeros((4, 8, 3), np.uint8)
        proc = np.full((4, 8, 3), 200, np.uint8)
        out = compose_output_frame(orig, proc, view=0, mix=1.0)
        np.testing.assert_array_equal(out, proc)

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


class WidgetSmokeTests(unittest.TestCase):
    def test_app_and_timeline_construct(self):
        import tkinter as tk

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dlss5_settings.json")
            old = os.environ.get("DLSS5TOOL_SETTINGS_PATH")
            os.environ["DLSS5TOOL_SETTINGS_PATH"] = path
            root = tk.Tk()
            root.withdraw()
            try:
                app = App(root)
                self.assertEqual(app._frame, 0)
                self.assertFalse(app._fullscreen)
                self.assertEqual(str(app.fs_btn.cget("text")), "全屏")
                self.assertTrue(hasattr(app, "import_btn"))
                self.assertTrue(hasattr(app, "clear_btn"))
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
                app.video = "dummy.mp4"
                app._update_action_labels()
                self.assertTrue(app.clear_btn.instate(["!disabled"]))
                app.clear_media()
                self.assertIsNone(app.video)
                self.assertTrue(app.clear_btn.instate(["disabled"]))
                self.assertTrue(app._preview_section.collapsed)
                self.assertTrue(app._export_section.collapsed)
                self.assertTrue(app._host_section.collapsed)
                packed = list(app.root.pack_slaves())
                self.assertEqual(
                    packed[-6:-1],
                    [
                        app._settings_frame, app._preview_section,
                        app._export_section, app._host_section,
                        app._export_row,
                    ],
                )
                self.assertIs(packed[-1], getattr(app.log, "frame", app.log))
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

    def test_closed_switch_sends_zero_and_remembers(self):
        import tkinter as tk

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dlss5_settings.json")
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
            os.environ["DLSS5TOOL_SETTINGS_PATH"] = path
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
                self.assertAlmostEqual(remembered["skin_struct"], 0.8)
                self.assertFalse(remembered["use_auto_mask"])
                self.assertEqual(str(app._settings["w_intensity"].cget("state")), "disabled")
                self.assertEqual(str(app._settings["w_local_tone"].cget("state")), "normal")
                self.assertEqual(str(app._settings["w_skin_struct"].cget("state")), "disabled")
                self.assertEqual(
                    str(app._settings["w_intensity"].cget("troughcolor")).lower(),
                    "#e6e6e6",
                )
                self.assertEqual(
                    str(app._settings["w_local_tone"].cget("troughcolor")).lower(),
                    "#5b8fad",
                )
                self.assertEqual(
                    str(app._settings["w_intensity_value"].cget("foreground")).lower(),
                    "#9a9a9a",
                )
                root.update_idletasks()
                intensity_w = app._settings["w_intensity"].master.winfo_width()
                mix_w = app._settings["w_outmix"].master.winfo_width()
                tone_w = app._settings["w_local_tone"].master.winfo_width()
                self.assertGreater(intensity_w, 0)
                self.assertEqual(intensity_w, mix_w)
                self.assertEqual(intensity_w, tone_w)

                app._settings["v_use_intensity"].set(True)
                app._settings["v_auto_mask"].set(True)
                app._update_dlss_control_states()
                live = app._collect_settings()
                self.assertAlmostEqual(live["intensity"], 0.9)
                self.assertAlmostEqual(live["skin_struct"], 0.8)
                self.assertEqual(live["use_auto_mask"], 1)
                self.assertEqual(str(app._settings["w_intensity"].cget("state")), "normal")
                self.assertEqual(str(app._settings["w_skin_struct"].cget("state")), "normal")
                self.assertEqual(
                    str(app._settings["w_intensity"].cget("troughcolor")).lower(),
                    "#5b8fad",
                )
                self.assertEqual(
                    str(app._settings["w_intensity_value"].cget("foreground")).lower(),
                    "#222222",
                )

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


if __name__ == "__main__":
    unittest.main()
