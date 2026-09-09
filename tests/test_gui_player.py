import os
import queue
import tempfile
import threading
import time
import unittest
from tkinter import filedialog, messagebox, ttk

import numpy as np

import app_settings
import diagnostics
import updater
from app_version import APP_VERSION
import ui_icons
import ui_theme
from ui_widgets import (
    AccentSlider, CheckToggle, ChipGroup, ChromeButton, ChromeCombobox,
    ProgressRule, StudioNotebook,
)
import gui
from gui import (
    App, TimelineBar,
    PREVIEW_BACKGROUND_TICK_MS, PREVIEW_INTERACTION_IDLE_MS, PREVIEW_WORKER_POLL_MS,
    _ExportCancelled,
    _clamp_frame, _decode_plan, _first_image, _format_duration, _format_timecode,
    _fit_preview_size, _frame_ranges, _large_image_host_settings, _realtime_preview_size,
    _is_image_path, _is_video_path, _play_target_frame, _read_image_bgr,
    _dlss_runtime_guidance, _is_dlss_runtime_unsupported,
    _clamp_window_geometry, _normalize_slider_input, _preview_viewport,
    _preview_control_layout, _write_image_bgr, compose_preview_frame,
    _studio_window_layout,
    effective_skin_settings, effective_slider,
)
from preview_audio import frame_to_ms, ms_to_frame
from video_export import compose_output_frame


class PlayerHelperTests(unittest.TestCase):
    def test_impossible_upscale_plan_rejected_before_gpu_query(self):
        from unittest import mock
        app = App.__new__(App)
        app.logln = mock.Mock()
        with mock.patch.object(gui, 'query_gpu_memory') as query, mock.patch.object(
            gui.messagebox, 'showerror'
        ) as error:
            self.assertFalse(app._confirm_super_resolution_export(11637,5120,2))
            error.assert_called_once()
            query.assert_not_called()
            error.reset_mock()
            self.assertFalse(app._confirm_super_resolution_export(4608,4608,4,notify=False))
            error.assert_not_called()

    def test_dlss_feature_not_supported_has_actionable_release_guidance(self):
        error = "Init_with_ProjectID -> 0xBAD00001\ncaller/static initialization failed"
        self.assertTrue(_is_dlss_runtime_unsupported(error))
        guidance = _dlss_runtime_guidance(error)
        self.assertIn("FeatureNotSupported", guidance)
        self.assertIn("30系.zip", guidance)
        self.assertIn("_internal\\nvngx_dlssnr.dll", guidance)
        self.assertIn("高性能（NVIDIA GPU）", guidance)
        self.assertIn("更多 → 一键诊断", guidance)
        self.assertIn(updater.RELEASES_URL, guidance)
        self.assertFalse(_is_dlss_runtime_unsupported("0xBAD00002"))

    def test_studio_window_layout_stays_inside_work_area(self):
        geometry, minimum = _studio_window_layout(1.5, (0, 0, 1920, 1040))
        self.assertRegex(geometry, r"^1848x968\+36\+36$")
        self.assertLessEqual(minimum[0], 1848)
        self.assertLessEqual(minimum[1], 968)

        geometry, minimum = _studio_window_layout(2.0, (0, 0, 800, 600))
        self.assertEqual(geometry, "704x504+48+48")
        self.assertEqual(minimum, (704, 504))

    def test_variable_traces_are_removed_when_custom_widgets_are_destroyed(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        errors = []
        root.report_callback_exception = lambda *error: errors.append(error)
        try:
            text = tk.StringVar(value="A")
            checked = tk.BooleanVar(value=False)
            number = tk.DoubleVar(value=0.0)
            widgets = (
                ChipGroup(root, text, ["A", "B"], ui=ui_theme.tokens("dark")),
                CheckToggle(root, "开关", checked, ui=ui_theme.tokens("dark")),
                AccentSlider(root, variable=number),
            )
            for widget in widgets:
                widget.destroy()
            text.set("B")
            checked.set(True)
            number.set(1.0)
            root.update_idletasks()
            self.assertEqual(errors, [])
            self.assertEqual(text.trace_info(), [])
            self.assertEqual(checked.trace_info(), [])
            self.assertEqual(number.trace_info(), [])
        finally:
            root.destroy()

    def test_chip_group_separates_stable_values_from_localized_labels(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            selected = tk.StringVar(value="original")
            widget = ChipGroup(
                root,
                selected,
                {"original": "Original", "compare": "Compare"},
                ui=ui_theme.tokens("dark"),
            )
            widget.pack()
            root.update_idletasks()
            self.assertEqual(selected.get(), "original")
            self.assertEqual(widget._nudge(1), "break")
            self.assertEqual(selected.get(), "compare")
        finally:
            root.destroy()

    def test_rounded_chrome_live_images_remain_bounded_across_redraws(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            variable = tk.StringVar(value="A")
            widget = ChipGroup(
                root, variable, ["A", "B", "C"], ui=ui_theme.tokens("dark"),
            )
            widget.pack()
            root.update_idletasks()
            for index in range(100):
                variable.set(("A", "B", "C")[index % 3])
            self.assertLessEqual(len(widget._chrome_surfaces), 48)
            self.assertLessEqual(len(widget._chrome_live), 4)
        finally:
            root.destroy()

    def test_progress_rule_stays_idle_until_value(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            bar = ProgressRule(root, ui=ui_theme.tokens("light"))
            bar.pack(fill="x")
            root.update_idletasks()
            self.assertEqual(bar["value"], 0)
            self.assertEqual(bar._ratio(), 0.0)
            bar["maximum"] = 10
            bar["value"] = 4
            self.assertEqual(bar["value"], 4)
            self.assertAlmostEqual(bar._ratio(), 0.4)
            bar["value"] = 0
            self.assertEqual(bar._ratio(), 0.0)
        finally:
            root.destroy()

    def test_chrome_button_centers_when_hidden_tab_is_shown(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            ui_theme.configure_fonts(root)
            ui = ui_theme.tokens("light")
            notebook = StudioNotebook(root, ui=ui)
            notebook.pack(fill="both", expand=True)
            first = tk.Frame(notebook.content)
            queue = tk.Frame(notebook.content)
            notebook.add(first, text="调参")
            notebook.add(queue, text="队列")
            row = tk.Frame(queue)
            row.pack(fill="x", padx=12)
            for column in range(2):
                row.columnconfigure(column, weight=1, uniform="queue_actions")
            load = ChromeButton(row, text="载入预览", variant="ghost", ui=ui)
            apply = ChromeButton(row, text="应用参数", variant="ghost", ui=ui)
            load.grid(row=0, column=0, sticky="ew", padx=(0, 4))
            apply.grid(row=0, column=1, sticky="ew", padx=(4, 0))
            start = ChromeButton(
                row, text="开始队列", variant="accent", ui=ui, icon="play",
            )
            start.grid(row=1, column=0, columnspan=2, sticky="ew")
            load.state(["disabled"])
            apply.state(["disabled"])
            start.state(["disabled"])
            start.config(text="开始队列", icon="play")
            root.geometry("400x240+0+0")
            root.deiconify()
            root.update()
            notebook.select(queue)
            root.update()
            self.assertGreater(load.winfo_width(), 80)
            self.assertGreater(apply.winfo_width(), 80)
            self.assertGreater(start.winfo_width(), load.winfo_width())
            for button, label in (
                (load, "载入预览"),
                (apply, "应用参数"),
            ):
                width = button.winfo_width()
                self.assertEqual(button._drawn_size[0], width, label)
                texts = [
                    item for item in button.find_all() if button.type(item) == "text"
                ]
                self.assertTrue(texts, label)
                x = button.coords(texts[-1])[0]
                self.assertAlmostEqual(x, width / 2, delta=2, msg=label)
            self.assertEqual(start._drawn_size[0], start.winfo_width())
        finally:
            root.destroy()

    def test_icon_only_buttons_use_larger_optical_size(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            ui_theme.configure_fonts(root)
            ui = ui_theme.tokens("light")
            icon_only = ChromeButton(
                root, text="播放", icon="play", icon_only=True, ui=ui,
            )
            with_text = ChromeButton(root, text="播放", icon="play", ui=ui)
            self.assertEqual(icon_only._icon_size, ui_theme.scale_px(root, 18))
            self.assertEqual(with_text._icon_size, ui_theme.scale_px(root, 16))
        finally:
            root.destroy()

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

    def test_detached_window_geometry_stays_on_virtual_desktop(self):
        bounds = (-1920, 0, 3840, 1080)
        self.assertEqual(
            _clamp_window_geometry("1100x700-1800+900", bounds),
            "1100x700-1800+380",
        )
        self.assertEqual(
            _clamp_window_geometry("bad", (0, 0, 1920, 1080)),
            "1100x700+48+48",
        )
        self.assertEqual(
            _clamp_window_geometry("100x100+0+0", (0, 0, 1920, 1080)),
            "320x240+0+0",
        )

    def test_preview_controls_wrap_at_narrow_widths(self):
        self.assertEqual(_preview_control_layout(900, "zh_CN"), "stacked")
        self.assertEqual(_preview_control_layout(960, "zh_CN"), "wide")
        self.assertEqual(_preview_control_layout(959, "zh_CN"), "stacked")
        self.assertEqual(_preview_control_layout(420, "zh_CN"), "stacked")
        self.assertEqual(_preview_control_layout(419, "zh_CN"), "compact")
        self.assertEqual(_preview_control_layout(959, "en_US"), "stacked")
        self.assertEqual(_preview_control_layout(960, "en_US"), "wide")
        self.assertEqual(_preview_control_layout(519, "en_US"), "compact")

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

    def test_fit_and_fullscreen_icons_are_distinct(self):
        self.assertNotEqual(ui_icons.LUCIDE["fit"], ui_icons.LUCIDE["fullscreen"])
        for name in ("retry", "clear-done", "up", "down", "cancel"):
            self.assertIn(name, ui_icons.LUCIDE)

    def test_realtime_preview_size_downscales_4k_but_preserves_smaller_sources(self):
        self.assertEqual(_fit_preview_size(3840, 2160, 1920), (1920, 1080))
        self.assertEqual(_fit_preview_size(2160, 3840, 1920), (1080, 1920))
        self.assertEqual(_realtime_preview_size(3840, 2160, "auto"), (1920, 1080))
        self.assertEqual(_realtime_preview_size(2560, 1440, "auto"), (2560, 1440))
        self.assertEqual(_realtime_preview_size(3840, 2160, "1440p"), (2560, 1440))
        self.assertEqual(_realtime_preview_size(3840, 2160, "original"), (3840, 2160))

    def test_preview_viewport_fits_and_crops_before_zooming(self):
        crop, dest, center, scale = _preview_viewport(1920, 1080, 1000, 800)
        self.assertEqual(crop, (0.0, 0.0, 1920.0, 1080.0))
        self.assertAlmostEqual(dest[0], 0.0)
        self.assertAlmostEqual(dest[1], 118.75)
        self.assertAlmostEqual(dest[2], 1000.0)
        self.assertAlmostEqual(dest[3], 562.5)
        self.assertEqual(center, (0.5, 0.5))
        self.assertAlmostEqual(scale, 1000 / 1920)

        crop, dest, center, scale = _preview_viewport(
            1920, 1080, 1000, 800, zoom=2.0,
        )
        self.assertEqual(dest, (0.0, 0.0, 1000.0, 800.0))
        self.assertAlmostEqual(crop[0], 480.0)
        self.assertAlmostEqual(crop[1], 156.0)
        self.assertAlmostEqual(crop[2], 1440.0)
        self.assertAlmostEqual(crop[3], 924.0)
        self.assertEqual(center, (0.5, 0.5))
        self.assertAlmostEqual(scale, 1000 / 1920 * 2)

    def test_preview_viewport_clamps_pan_and_allows_zooming_out(self):
        crop, _dest, center, _scale = _preview_viewport(
            1920, 1080, 1000, 800, zoom=2.0, center_x=-2, center_y=-2,
        )
        self.assertAlmostEqual(crop[0], 0.0)
        self.assertAlmostEqual(crop[1], 0.0)
        self.assertAlmostEqual(center[0], 0.25)
        self.assertAlmostEqual(center[1], 768 / 2 / 1080)

        crop, dest, center, _scale = _preview_viewport(
            1920, 1080, 1000, 800, zoom=0.5, center_x=0, center_y=1,
        )
        self.assertEqual(crop, (0.0, 0.0, 1920.0, 1080.0))
        self.assertAlmostEqual(dest[0], 250.0)
        self.assertAlmostEqual(dest[1], 259.375)
        self.assertEqual(center, (0.5, 0.5))
        self.assertIsNone(_preview_viewport(0, 1080, 1000, 800))
        self.assertIsNone(_preview_viewport(1920, 1080, 1000, 800, zoom=float("nan")))

    def test_canvas_wheel_zooms_around_pointer_but_timeline_wheel_steps(self):
        app = App.__new__(App)
        app.video = "dummy.mp4"
        app._exporting = False
        app._preview_zoom = 1.0
        app._preview_pan_x = 0.5
        app._preview_pan_y = 0.5
        app._video_geom = (0, 119, 1000, 562)
        app._viewport_crop_norm = (0.0, 0.0, 1.0, 1.0)
        app._viewport_source_size = (1920, 1080)
        app._canvas_size = lambda: (1000, 800)
        app.pause = lambda: None
        app._update_zoom_controls = lambda: None
        app._refresh_viewport_display = lambda: None
        event = type("Event", (), {"x": 750, "y": 400, "delta": 120})()
        self.assertEqual(app._on_canvas_wheel(event), "break")
        self.assertAlmostEqual(app._preview_zoom, 1.25)
        layout = _preview_viewport(
            1920, 1080, 1000, 800, app._preview_zoom,
            app._preview_pan_x, app._preview_pan_y,
        )
        crop, _dest, _center, _scale = layout
        source_x = crop[0] + 0.75 * (crop[2] - crop[0])
        self.assertAlmostEqual(source_x / 1920, 0.75)

        steps = []
        app.step_frame = lambda delta: steps.append(delta)
        self.assertEqual(app._on_wheel_step(event), "break")
        self.assertEqual(steps, [1])

    def test_large_still_images_use_v2_feature_subrects(self):
        settings = {"host_backend": "legacy", "host_in_flight": 3}
        self.assertEqual(
            _large_image_host_settings(7680, 4320, settings), settings,
        )
        tiled = _large_image_host_settings(11637, 5120, settings)
        self.assertEqual(tiled["host_backend"], "v2")
        self.assertEqual(tiled["host_in_flight"], 1)
        self.assertTrue(tiled["host_tiled_mode"])
        self.assertEqual(
            (tiled["host_tile_width"], tiled["host_tile_height"]),
            (6000, 3000),
        )

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


def _iter_widgets(root):
    stack = [root]
    seen = set()
    while stack:
        widget = stack.pop()
        key = str(widget)
        if key in seen:
            continue
        seen.add(key)
        yield widget
        try:
            stack.extend(widget.winfo_children())
        except Exception:
            pass


class ChromeComboboxTests(unittest.TestCase):
    def _open(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        ui = ui_theme.tokens("light")
        ui_theme.apply_ttk(root, ui)
        choice = tk.StringVar(value="MP4（推荐）")
        values = ["MP4（推荐）", "MKV", "MOV"]
        combo = ChromeCombobox(
            root, ui=ui, textvariable=choice, values=values, state="readonly",
        )
        combo.pack()
        other = tk.Entry(root)
        other.pack()
        root.update_idletasks()
        return root, choice, combo, other

    def test_mousewheel_does_not_change_value(self):
        root, choice, combo, _other = self._open()
        try:
            combo.combo.focus_set()
            root.update()
            self.assertEqual(str(root.bind_class("TCombobox", "<MouseWheel>")), "")
            combo.combo.event_generate("<MouseWheel>", delta=-120)
            combo.combo.event_generate("<MouseWheel>", delta=120)
            root.update()
            self.assertEqual(choice.get(), "MP4（推荐）")
            self.assertEqual(combo.combo.current(), 0)
        finally:
            root.destroy()

    def test_focus_out_clears_inverted_selection(self):
        root, choice, combo, other = self._open()
        try:
            combo.combo.selection_range(0, "end")
            self.assertTrue(combo.combo.selection_present())
            combo.combo.event_generate("<FocusOut>")
            other.focus_set()
            root.update()
            self.assertFalse(combo.combo.selection_present())
            self.assertEqual(choice.get(), "MP4（推荐）")
        finally:
            root.destroy()

    def test_selecting_an_item_commits_without_leaving_selection(self):
        root, choice, combo, _other = self._open()
        try:
            combo.combo.current(1)
            combo.combo.selection_range(0, "end")
            combo.combo.event_generate("<<ComboboxSelected>>")
            root.update()
            self.assertEqual(choice.get(), "MKV")
            self.assertFalse(combo.combo.selection_present())
        finally:
            root.destroy()

    def test_readonly_select_colors_match_the_field(self):
        from tkinter import ttk

        root, _choice, _combo, _other = self._open()
        try:
            style = ttk.Style(root)
            ui = ui_theme.tokens("light")
            self.assertEqual(
                style.lookup("Chrome.TCombobox", "selectbackground", ("readonly",)),
                ui["entry_bg"],
            )
            self.assertEqual(
                style.lookup("Chrome.TCombobox", "selectforeground", ("readonly",)),
                ui["entry_fg"],
            )
        finally:
            root.destroy()


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
    def test_release_defaults_match_recommended_profile(self):
        defaults = app_settings.validate({})
        expected = {
            "preview_view": "original",
            "style": 0,
            "enable_5x": False,
            "intensity": 1.0,
            "use_intensity": True,
            "local_tone": 1.0,
            "use_local_tone": True,
            "local_struct": 1.0,
            "use_local_struct": True,
            "use_auto_mask": True,
            "skin_struct": 1.0,
            "output_view": 0,
            "output_mix": 1.0,
            "use_output_mix": True,
            "preview_quality": "original",
            "preview_cache_mb": 8192,
            "output_container": "mp4",
            "output_resolution": "source",
            "rate_control": "quality",
            "quality_profile": "high",
            "video_bitrate_mbps": 20.0,
            "nvenc_preset": "p5",
            "hdr_mode": True,
            "export_mode": "single",
            "parallel_workers": 4,
            "warmup_frames": 8,
            "decode_buffer": 4,
            "host_backend": "auto",
            "host_submission": "compatibility",
            "host_in_flight": 3,
            "host_zero_fast_path": True,
            "host_persistent_buffers": True,
            "host_auto_fallback": True,
            "ui_preview_open": True,
            "ui_export_open": True,
            "ui_host_open": True,
            "ui_theme": "dark",
            "preview_detached": False,
            "preview_window_geometry": "",
        }
        for name, value in expected.items():
            self.assertEqual(defaults[name], value, name)

    def test_ui_panel_flags_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dlss5_settings.json")
            saved = app_settings.save(
                {
                    "ui_export_open": True,
                    "ui_host_open": 1,
                    "preview_view": "DLSS",
                    "preview_detached": True,
                    "preview_window_geometry": "1280x720-1200+80",
                },
                path=path,
            )
            self.assertTrue(saved["ui_export_open"])
            self.assertTrue(saved["ui_host_open"])
            self.assertTrue(saved["preview_detached"])
            self.assertEqual(saved["preview_window_geometry"], "1280x720-1200+80")
            loaded = app_settings.load(path)
            self.assertTrue(loaded["ui_export_open"])
            self.assertTrue(loaded["ui_host_open"])
            self.assertEqual(loaded["preview_view"], "dlss")
            self.assertTrue(loaded["preview_detached"])
            self.assertEqual(loaded["preview_window_geometry"], "1280x720-1200+80")
            self.assertTrue(app_settings.validate({})["ui_preview_open"])
            self.assertEqual(app_settings.validate({})["ui_theme"], "dark")
            self.assertEqual(app_settings.validate({"ui_theme": "LIGHT"})["ui_theme"], "light")
            self.assertEqual(ui_theme.normalize_theme_name("Light"), "light")
            self.assertNotEqual(
                ui_theme.tokens("light")["panel"], ui_theme.tokens("dark")["panel"],
            )
            self.assertNotEqual(
                ui_theme.tokens("light")["canvas"], ui_theme.tokens("dark")["canvas"],
            )
            self.assertEqual(
                app_settings.validate({"preview_window_geometry": "off screen"})[
                    "preview_window_geometry"
                ],
                "",
            )

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
            self.assertEqual(
                app_settings.validate({"preview_quality": "bad"})["preview_quality"],
                "original",
            )

    def test_slider_toggles_default_on_and_roundtrip(self):
        loaded = app_settings.validate({"intensity": 0.9, "skin_struct": 0.8})
        self.assertTrue(loaded["use_intensity"])
        self.assertTrue(loaded["use_local_tone"])
        self.assertTrue(loaded["use_local_struct"])
        self.assertTrue(loaded["use_output_mix"])
        self.assertTrue(loaded["use_auto_mask"])
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
    def test_interaction_freeze_invalidates_cache_without_joining_worker(self):
        app = App.__new__(App)
        cancelled = []

        class NoJoinThread:
            def join(self, *_args, **_kwargs):
                raise AssertionError("interaction freeze must not join the worker")

        app._cancel_after = cancelled.append
        app._pre_rendering = True
        app._preview_cache_frozen = False
        app._prefetch_stop = threading.Event()
        app._prefetch_gen = 4
        app._preview_frame_queue = object()
        app._cache_lock = threading.RLock()
        app._queued_preview_frames = {1, 2, 3}
        app._play_dlss_thread = NoJoinThread()

        app._freeze_preview_cache(resume_ms=None)

        self.assertTrue(app._preview_cache_frozen)
        self.assertTrue(app._prefetch_stop.is_set())
        self.assertEqual(app._prefetch_gen, 5)
        self.assertIsNone(app._preview_frame_queue)
        self.assertEqual(app._queued_preview_frames, set())
        self.assertIn("_preview_decode_after", cancelled)
        self.assertIn("_scrub_after", cancelled)
        self.assertIn("_preview_cache_resume_after", cancelled)

    def test_idle_freeze_does_not_resume_a_held_interaction(self):
        app = App.__new__(App)
        scheduled = []

        class FakeRoot:
            def after(self, delay, callback):
                scheduled.append(delay)
                return "resume-id"

        def cancel(name):
            if name == "_preview_cache_resume_after":
                app._preview_cache_resume_after = None

        app.root = FakeRoot()
        app._cancel_after = cancel
        app._pre_rendering = False
        app._prefetch_stop = threading.Event()
        app._prefetch_gen = 1
        app._preview_frame_queue = None
        app._cache_lock = threading.RLock()
        app._queued_preview_frames = set()
        app._preview_cache_frozen = False
        app._preview_cache_resume_after = None
        app._play_dlss_thread = None
        app._exporting = False
        app.video = "video.mp4"

        app._freeze_preview_cache(resume_ms=None)
        app._freeze_preview_cache()
        self.assertEqual(scheduled, [])
        self.assertTrue(app._preview_cache_frozen)
        app._schedule_preview_cache_resume()
        self.assertEqual(scheduled, [PREVIEW_INTERACTION_IDLE_MS])

    def test_full_preview_stays_idle_while_cache_is_frozen(self):
        app = App.__new__(App)
        events = []

        class FakeRoot:
            def after(self, delay, callback):
                events.append(("after", delay))
                return "after-id"

        app.root = FakeRoot()
        app._scrub_after = None
        app._preview_cache_frozen = True
        app._cancel_after = lambda name: events.append(("cancel", name))
        app._schedule_preview_cache_resume = lambda *a, **k: events.append("resume")
        app._preview_scrub_ms = lambda: 75

        app._schedule_full_preview()
        self.assertEqual(events, [("cancel", "_scrub_after")])

    def test_timeline_seek_keeps_cache_frozen_until_release(self):
        app = App.__new__(App)
        events = []
        app.video = "video.mp4"
        app._exporting = False
        app.playing = False
        app._focus_preview_host = lambda: None
        app.pause = lambda: events.append("pause")
        app._freeze_preview_cache = (
            lambda resume_ms=PREVIEW_INTERACTION_IDLE_MS: events.append(("freeze", resume_ms))
        )
        app._goto_frame = (
            lambda frame, quality="full": events.append(("goto", frame, quality))
        )
        app._schedule_preview_cache_resume = (
            lambda delay=PREVIEW_INTERACTION_IDLE_MS: events.append(("resume", delay))
        )

        for detached in (False, True):
            events.clear()
            app._preview_detached = detached
            app._on_timeline_seek(8, "start")
            app._on_timeline_seek(12, "move")
            app._on_timeline_seek(15, "end")
            self.assertEqual(
                events,
                [
                    ("freeze", None),
                    ("goto", 8, "fast"),
                    ("freeze", None),
                    ("goto", 12, "fast"),
                    ("goto", 15, "fast"),
                    ("resume", PREVIEW_INTERACTION_IDLE_MS),
                ],
                msg=f"detached={detached}",
            )

    def test_canvas_configure_only_freezes_the_active_pane(self):
        app = App.__new__(App)
        frozen = []
        scheduled = []
        docked = object()
        detached = object()
        app.canvas = docked
        app.video = "video.mp4"
        app.playing = False
        app._resize_after = None
        app._freeze_preview_cache = lambda *a, **k: frozen.append("freeze")
        app._cancel_after = lambda name: scheduled.append(("cancel", name))

        class FakeRoot:
            def after(self, delay, callback):
                scheduled.append(("after", delay, callback))
                return "resize-id"

        app.root = FakeRoot()
        app._on_canvas_configure(type("Event", (), {"widget": detached})())
        self.assertEqual(frozen, [])
        self.assertEqual(scheduled, [])
        app._on_canvas_configure(type("Event", (), {"widget": docked})())
        self.assertEqual(frozen, ["freeze"])
        self.assertEqual(scheduled[0], ("cancel", "_resize_after"))
        self.assertEqual(scheduled[1][0], "after")
        self.assertEqual(scheduled[1][2], app._apply_canvas_resize)

    def test_resume_waits_for_live_worker_without_joining(self):
        app = App.__new__(App)
        scheduled = []
        joined = []

        class FakeRoot:
            def after(self, delay, callback):
                scheduled.append((delay, callback))
                return "resume-id"

        class LiveThread:
            def is_alive(self):
                return True

            def join(self, *_args, **_kwargs):
                joined.append(True)

        app.root = FakeRoot()
        app._preview_cache_resume_after = None
        app._play_dlss_thread = LiveThread()
        app._play_dlss_busy = True
        app._preview_cache_frozen = True
        app.video = "video.mp4"
        app._exporting = False

        app._resume_preview_cache()
        self.assertEqual(joined, [])
        self.assertTrue(app._preview_cache_frozen)
        self.assertIs(app._play_dlss_thread.__class__, LiveThread)
        self.assertEqual(scheduled[-1][0], PREVIEW_WORKER_POLL_MS)
        self.assertEqual(scheduled[-1][1], app._resume_preview_cache)

    def test_resume_closes_live_after_worker_exits_when_source_cleared(self):
        app = App.__new__(App)
        closed = []

        class DeadThread:
            def is_alive(self):
                return False

        app._preview_cache_resume_after = "resume-id"
        app._play_dlss_thread = DeadThread()
        app._play_dlss_busy = True
        app._preview_cache_frozen = True
        app.video = None
        app._exporting = False
        app._close_live = lambda: closed.append("live")

        app._resume_preview_cache()
        self.assertEqual(closed, ["live"])
        self.assertFalse(app._preview_cache_frozen)
        self.assertIsNone(app._play_dlss_thread)
        self.assertFalse(app._play_dlss_busy)

    def test_begin_source_load_freezes_without_joining_worker(self):
        app = App.__new__(App)
        events = []

        class NoJoinThread:
            def is_alive(self):
                return True

            def join(self, *_args, **_kwargs):
                raise AssertionError("source load must not join the worker")

        app.pause = lambda: events.append("pause")
        app._freeze_preview_cache = (
            lambda resume_ms=PREVIEW_INTERACTION_IDLE_MS: events.append(("freeze", resume_ms))
        )
        app._audio = type("Audio", (), {"close": lambda self: events.append("audio")})()
        app._cache_clear = lambda: events.append("clear")
        app._cancel_after = lambda name: events.append(("cancel", name))
        app._update_zoom_controls = lambda: None
        app.timeline = type("Bar", (), {"set_cache_ranges": lambda self, *_a: None})()
        app._play_dlss_thread = NoJoinThread()
        app._cap = None

        app._begin_source_load()
        self.assertEqual(events[0], "pause")
        self.assertIn(("freeze", None), events)
        self.assertNotIn("join", events)
        self.assertIsNone(app.video)

    def test_canvas_pan_click_resumes_cache_in_compare_view(self):
        app = App.__new__(App)
        events = []
        app._drag_split = False
        app._canvas_press = ("pan", 0, 0, 0.5, 0.5, 1.0, (8, 4))
        app._pan_moved = False
        app.video = "video.mp4"
        app._exporting = False
        app.playing = False
        app.view_var = type("FakeVar", (), {"get": lambda _self: "compare"})()
        app._update_split_from_event = lambda _event: events.append("split")
        app.toggle_play = lambda: events.append("play")
        app._schedule_preview_cache_resume = lambda *a, **k: events.append("resume")
        app.on_canvas_hover = lambda _event: events.append("hover")

        app.on_canvas_release(type("Event", (), {"x": 0, "y": 0})())
        self.assertEqual(events, ["split", "resume", "hover"])
        self.assertIsNone(app._canvas_press)

    def test_preview_timeline_status_skips_work_while_frozen(self):
        app = App.__new__(App)
        app.video = "video.mp4"
        app.view_var = type("FakeVar", (), {"get": lambda _self: "dlss"})()
        app._preview_cache_frozen = True
        app._preview_status_at = 0.0
        app.timeline = type(
            "Bar",
            (),
            {"set_cache_ranges": lambda *a, **k: self.fail("frozen status must not scan cache")},
        )()
        app._update_preview_timeline_and_status()

    def test_paused_background_decode_yields_between_frames(self):
        app = App.__new__(App)
        scheduled = []

        class FakeRoot:
            def after(self, delay, callback):
                scheduled.append((delay, callback))
                return "decode-id"

        app.root = FakeRoot()
        app._preview_decode_after = None
        app._cancel_after = lambda _name: None
        app._preview_session_active = lambda: True
        app._preview_decode_tick = lambda: None
        app._pre_rendering = True
        app.playing = False

        app._schedule_preview_decode(0)
        self.assertEqual(scheduled[-1][0], PREVIEW_BACKGROUND_TICK_MS)

        app.playing = True
        app._schedule_preview_decode(1)
        self.assertEqual(scheduled[-1][0], 1)

    def test_scrub_delay_queues_exact_preview_without_blocking_ui(self):
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
        app._source_kind = "video"
        app._hold_original = False
        app._frame = 12
        app.view_var = type("FakeVar", (), {"get": lambda _self: "dlss"})()
        app._precise_preview_size = lambda: (1920, 1080)
        app._cached_dlss = lambda frame, size: None
        app.display_view = lambda quality="full": events.append(("display", quality))
        app._display_precise_preview = lambda: events.append(("precise", None))
        app._start_paused_prerender = (
            lambda target_size=None: events.append(("prerender", target_size)) or True
        )
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
                ("display", "fast"),
                ("prerender", (1920, 1080)),
                ("status", True),
            ],
        )

    def test_paused_prerender_can_start_without_playback(self):
        app = App.__new__(App)
        calls = []
        source = np.zeros((4, 8, 3), np.uint8)

        class FakeVar:
            def get(self):
                return "dlss"

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
    def test_automatic_update_check_only_prompts_for_a_newer_release(self):
        logs = []
        prompted = []
        fake_app = type("FakeApp", (), {})()
        fake_app.logln = logs.append
        fake_app._prompt_for_update = prompted.append
        current = updater.ReleaseInfo(APP_VERSION, updater.RELEASES_URL, "", ())

        App._handle_update_check_result(fake_app, None, "offline", manual=False)
        App._handle_update_check_result(fake_app, current, None, manual=False)
        App._handle_update_check_result(
            fake_app,
            updater.ReleaseInfo("v99.0.0", updater.RELEASES_URL, "", ()),
            None,
            manual=False,
        )

        self.assertEqual(logs, [])
        self.assertEqual([release.tag for release in prompted], ["v99.0.0"])

    def test_one_click_diagnostic_exports_in_background(self):
        import tkinter as tk

        with tempfile.TemporaryDirectory() as tmp:
            settings_path = os.path.join(tmp, "dlss5_settings.json")
            queue_path = os.path.join(tmp, "dlss5_queue.json")
            report_path = os.path.join(tmp, "diagnostic.log")
            old_settings = os.environ.get("DLSS5TOOL_SETTINGS_PATH")
            old_queue = os.environ.get("DLSS5TOOL_QUEUE_PATH")
            os.environ["DLSS5TOOL_SETTINGS_PATH"] = settings_path
            os.environ["DLSS5TOOL_QUEUE_PATH"] = queue_path
            root = tk.Tk()
            root.withdraw()
            app = None
            original_dialog = filedialog.asksaveasfilename
            original_writer = diagnostics.write_diagnostic_report
            original_showinfo = messagebox.showinfo
            original_showerror = messagebox.showerror
            filedialog.asksaveasfilename = lambda **_kwargs: report_path

            def fake_writer(path, context):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("diagnostic ok")
                self.assertIn("settings", context)
                return {"path": path, "passed": 2, "total": 2}

            diagnostics.write_diagnostic_report = fake_writer
            messagebox.showinfo = lambda *args, **kwargs: None
            messagebox.showerror = lambda *args, **kwargs: None
            try:
                app = App(root)
                app.export_diagnostics()
                self.assertTrue(app._diagnosing)
                self.assertEqual(str(app.diagnostic_btn.cget("text")), "诊断中…")
                deadline = time.monotonic() + 3.0
                while app._diagnosing and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(app._diagnosing)
                self.assertTrue(os.path.isfile(report_path))
                self.assertEqual(str(app.diagnostic_btn.cget("text")), "一键诊断")
                self.assertIn("宿主探针 2/2 通过", app.eta_label.cget("text"))
            finally:
                filedialog.asksaveasfilename = original_dialog
                diagnostics.write_diagnostic_report = original_writer
                messagebox.showinfo = original_showinfo
                messagebox.showerror = original_showerror
                if app is not None:
                    for name in (
                        "_settings_save_after", "_live_debounce",
                        "_output_preview_after", "_scrub_after", "_resize_after",
                        "_play_after", "_preview_decode_after",
                        "_preview_cache_resume_after",
                    ):
                        app._cancel_after(name)
                    app._prefetch_stop.set()
                for after_id in root.tk.call("after", "info"):
                    root.after_cancel(after_id)
                root.destroy()
                if old_settings is None:
                    os.environ.pop("DLSS5TOOL_SETTINGS_PATH", None)
                else:
                    os.environ["DLSS5TOOL_SETTINGS_PATH"] = old_settings
                if old_queue is None:
                    os.environ.pop("DLSS5TOOL_QUEUE_PATH", None)
                else:
                    os.environ["DLSS5TOOL_QUEUE_PATH"] = old_queue

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
            callback_errors = []
            root.report_callback_exception = lambda *error: callback_errors.append(error)
            try:
                app = App(root)
                self.assertEqual(app._frame, 0)
                self.assertFalse(app._fullscreen)
                self.assertEqual(str(app.fs_btn.cget("text")), "全屏")
                self.assertEqual(str(app.fs_btn.cget("icon")), "fullscreen")
                self.assertEqual(str(app.detach_btn.cget("text")), "分离")
                self.assertEqual(str(app.play_btn.cget("icon")), "play")
                app._draw_empty(640, 400)
                empty_text = " ".join(
                    str(app.canvas.itemcget(item, "text"))
                    for item in app.canvas.find_withtag("empty")
                    if app.canvas.type(item) == "text"
                )
                self.assertIn("拖入视频或图片", empty_text)
                self.assertIn("选择文件", empty_text)
                self.assertNotIn("预览工作台", empty_text)
                self.assertNotIn("开始你的画质创作", empty_text)
                self.assertNotIn("也可以点击导入", empty_text)
                imports = []
                app.import_media = lambda: imports.append("import")
                x0, y0, x1, y1 = app._empty_import_geom
                app.on_canvas_press(type("Event", (), {
                    "x": (x0 + x1) / 2, "y": (y0 + y1) / 2, "state": 0,
                })())
                self.assertEqual(imports, ["import"])
                imports.clear()
                app.on_canvas_press(type("Event", (), {"x": 12, "y": 12, "state": 0})())
                self.assertEqual(imports, [])
                self.assertEqual(str(app.export_btn.cget("text")), "导出 DLSS")
                self.assertFalse(hasattr(app, "queue_more_btn"))
                self.assertEqual(str(app.queue_retry_btn.cget("text")), "重试")
                self.assertEqual(str(app.queue_retry_btn.cget("icon")), "retry")
                self.assertEqual(str(app.queue_clear_done_btn.cget("icon")), "clear-done")
                self.assertEqual(str(app.queue_move_up_btn.cget("icon")), "up")
                self.assertEqual(str(app.queue_move_down_btn.cget("icon")), "down")
                self.assertTrue(app._log_open)
                self.assertEqual(str(app.log_btn.cget("text")), "收起日志")
                self.assertEqual(str(app.log.winfo_manager()), "pack")
                self.assertEqual(str(app.zoom_reset_btn.cget("text")), "适应")
                self.assertEqual(str(app.zoom_reset_btn.cget("icon")), "fit")
                self.assertNotEqual(
                    str(app.zoom_reset_btn.cget("icon")),
                    str(app.fs_btn.cget("icon")),
                )
                app._layout_queue_run_controls(True)
                pause_info = app.queue_pause_btn.grid_info()
                cancel_info = app.queue_cancel_btn.grid_info()
                self.assertEqual(int(pause_info["column"]), 0)
                self.assertEqual(int(cancel_info["column"]), 1)
                self.assertEqual(str(pause_info["sticky"]), "ew")
                self.assertEqual(str(cancel_info["sticky"]), "ew")
                self.assertEqual(str(app.queue_start_btn.winfo_manager()), "")
                self.assertEqual(str(app.queue_pause_btn.winfo_manager()), "grid")
                self.assertEqual(str(app.queue_cancel_btn.winfo_manager()), "grid")
                self.assertNotEqual(
                    app.queue_start_btn.winfo_manager(),
                    app.queue_pause_btn.winfo_manager(),
                )
                app._layout_queue_run_controls(False)
                self.assertEqual(str(app.queue_start_btn.winfo_manager()), "grid")
                self.assertEqual(str(app.queue_pause_btn.winfo_manager()), "")
                self.assertTrue(app.zoom_in_btn.instate(["disabled"]))
                self.assertTrue(app.zoom_out_btn.instate(["disabled"]))
                docked_canvas = app.canvas
                docked_timeline = app.timeline
                self.assertFalse(app._preview_detached)
                self.assertIs(app.canvas, app._docked_preview_pane["canvas"])
                self.assertEqual(app.timeline.on_seek, app._on_timeline_seek)
                app.timeline.set_range(0, 20)
                app.timeline.set(7)
                app.timeline.set_cache_ranges([(1, 4)], [(5, 8)])
                theme_widget_count = len(app._theme_widgets)
                app.detach_preview()
                root.update_idletasks()
                self.assertTrue(app._preview_detached)
                self.assertIsNotNone(app._detached_preview_window)
                self.assertIsNot(app.canvas, docked_canvas)
                self.assertIs(app.canvas.winfo_toplevel(), app._detached_preview_window)
                self.assertIs(app.canvas, app._detached_preview_pane["canvas"])
                self.assertAlmostEqual(
                    ui_theme.studio_scale(app.canvas),
                    getattr(root, "_studio_scale", 1.0),
                )
                self.assertEqual(app.timeline.on_seek, app._on_timeline_seek)
                self.assertEqual(str(app.detach_btn.cget("text")), "停靠")
                self.assertEqual(app.timeline.get(), 7)
                self.assertEqual(app.timeline._rendered_ranges, [(1, 4)])
                detached_settings = app._collect_persisted_settings()
                self.assertTrue(detached_settings["preview_detached"])
                self.assertRegex(
                    detached_settings["preview_window_geometry"],
                    r"^\d+x\d+[+-]\d+[+-]\d+$",
                )
                app.dock_preview()
                root.update_idletasks()
                self.assertFalse(app._preview_detached)
                self.assertIsNone(app._detached_preview_window)
                self.assertIs(app.canvas, docked_canvas)
                self.assertIs(app.timeline, docked_timeline)
                self.assertEqual(app.timeline.get(), 7)
                self.assertEqual(str(app.detach_btn.cget("text")), "分离")
                self.assertFalse(app._collect_persisted_settings()["preview_detached"])
                self.assertEqual(len(app._theme_widgets), theme_widget_count + 1)
                app.view_var.set("dlss")
                root.update_idletasks()
                self.assertEqual(callback_errors, [])
                stable_theme_widget_count = len(app._theme_widgets)
                app.detach_preview()
                root.update_idletasks()
                app.dock_preview()
                root.update_idletasks()
                self.assertEqual(len(app._theme_widgets), stable_theme_widget_count)
                self.assertEqual(callback_errors, [])
                self.assertTrue(hasattr(app, "import_btn"))
                self.assertTrue(hasattr(app, "clear_btn"))
                self.assertTrue(hasattr(app, "cancel_export_btn"))
                self.assertTrue(hasattr(app, "diagnostic_btn"))
                self.assertEqual(str(app.diagnostic_btn.cget("text")), "一键诊断")
                self.assertTrue(app.diagnostic_btn.instate(["!disabled"]))
                self.assertTrue(hasattr(app, "update_btn"))
                self.assertEqual(str(app.update_btn.cget("text")), "检查更新")
                self.assertTrue(app.update_btn.instate(["!disabled"]))
                self.assertIs(app.update_btn.master, app.diagnostic_btn.master)
                self.assertEqual(
                    int(app.update_btn.cget("width")),
                    int(app.diagnostic_btn.cget("width")),
                )
                app._update_checking = True
                app._update_action_labels()
                self.assertEqual(str(app.update_btn.cget("text")), "检查中…")
                self.assertTrue(app.update_btn.instate(["disabled"]))
                app._update_checking = False
                app._diagnosing = True
                app._update_action_labels()
                self.assertEqual(str(app.diagnostic_btn.cget("text")), "诊断中…")
                self.assertTrue(app.diagnostic_btn.instate(["disabled"]))
                self.assertTrue(app.import_btn.instate(["disabled"]))
                app._diagnosing = False
                app._update_action_labels()
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
                self.assertEqual(export["output_container"], "mp4")
                self.assertEqual(export["output_resolution"], "source")
                self.assertEqual(export["super_resolution_scale"], 1)
                self.assertEqual(export["rate_control"], "quality")
                self.assertEqual(export["quality_profile"], "high")
                self.assertEqual(export["workers"], 4)
                self.assertEqual(export["nvenc_preset"], "p5")
                app._export_settings["v_rate_control"].set("目标码率")
                app._export_settings["v_output_resolution"].set("自定义上限")
                app._on_export_settings_change()
                self.assertTrue(app._export_settings["w_quality_profile"].instate(["disabled"]))
                self.assertTrue(app._export_settings["w_video_bitrate"].instate(["!disabled"]))
                self.assertTrue(app._export_settings["w_custom_width"].instate(["!disabled"]))
                app.video = "dummy.png"
                app._source_kind = "image"
                app._image_bgr = np.zeros((4, 8, 3), np.uint8)
                app._update_export_control_states()
                app._update_action_labels()
                self.assertTrue(app.zoom_in_btn.instate(["!disabled"]))
                self.assertTrue(app.zoom_out_btn.instate(["!disabled"]))
                self.assertTrue(app._export_settings["w_output_container"].instate(["disabled"]))
                self.assertTrue(app._export_settings["w_nvenc_preset"].instate(["disabled"]))
                self.assertTrue(app._export_settings["w_mode"].instate(["disabled"]))
                app._source_kind = None
                app._image_bgr = None
                app.video = "dummy.mp4"
                app._export_settings["v_mode"].set("视觉无损（并行分段）")
                app._export_settings["v_super_resolution"].set("2×")
                app._update_export_control_states()
                export = app._collect_export_settings()
                self.assertEqual(export["super_resolution_scale"], 2)
                self.assertEqual(export["mode"], "single")
                self.assertTrue(app._export_settings["w_output_resolution"].instate(["disabled"]))
                app._export_settings["v_super_resolution"].set("关闭")
                app._update_export_control_states()
                app._update_action_labels()
                self.assertTrue(app.clear_btn.instate(["!disabled"]))
                self.assertTrue(app.cancel_export_btn.instate(["disabled"]))
                app._exporting = True
                app._update_action_labels()
                self.assertTrue(app.export_btn.instate(["disabled"]))
                self.assertTrue(app.cancel_export_btn.instate(["!disabled"]))
                self.assertEqual(str(app.export_btn.winfo_manager()), "")
                self.assertEqual(str(app.cancel_export_btn.winfo_manager()), "grid")
                self.assertEqual(int(app.cancel_export_btn.grid_info()["columnspan"]), 2)
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
                self.assertEqual(str(app.export_btn.winfo_manager()), "grid")
                self.assertEqual(str(app.cancel_export_btn.winfo_manager()), "")
                self.assertEqual(str(app._progress_rule.winfo_manager()), "pack")
                app.view_var.set("compare")
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
                app._preview_zoom = 2.0
                app._blit_split(120, 70)
                self.assertTrue(app.canvas.find_withtag("navigator"))
                self.assertIsNotNone(app._navigator_geom)
                app._preview_zoom = 1.0
                app.clear_media()
                self.assertIsNone(app.video)
                self.assertTrue(app.clear_btn.instate(["disabled"]))
                self.assertFalse(app._preview_section.collapsed)
                self.assertEqual(app._preview_settings["v_quality"].get(), "原始分辨率")
                self.assertFalse(app._export_section.collapsed)
                self.assertFalse(app._host_section.collapsed)
                packed = list(app.root.pack_slaves())
                self.assertIn(app._studio, packed)
                self.assertIn(app._progress_rule, packed)
                self.assertIn(app.log.frame, packed)
                self.assertIsInstance(app.log.vbar, ttk.Scrollbar)
                self.assertEqual(app.log.vbar.winfo_class(), "TScrollbar")
                self.assertIn(app._status_bar, packed)
                self.assertLess(packed.index(app._studio), packed.index(app._progress_rule))
                self.assertLess(packed.index(app._progress_rule), packed.index(app.log.frame))
                self.assertLess(packed.index(app.log.frame), packed.index(app._status_bar))
                self.assertEqual(str(app.pbar.winfo_manager()), "pack")
                self.assertIs(app.pbar, app._progress_rule)
                self.assertIsInstance(app.pbar, ProgressRule)
                self.assertFalse(hasattr(app, "queue_progress"))
                self.assertFalse(hasattr(app, "queue_status_label"))
                self.assertEqual(str(app.export_btn.winfo_manager()), "grid")
                self.assertEqual(str(app.cancel_export_btn.winfo_manager()), "")
                self.assertIn(app.workspace_tabs, app._inspector.pack_slaves())
                preview_packed = list(app.preview_tab.pack_slaves())
                self.assertIn(app._settings_frame, preview_packed)
                export_packed = list(app._export_section.master.pack_slaves())
                self.assertEqual(
                    export_packed,
                    [app._export_section, app._preview_section, app._guidance_advanced,
                     app._host_section, app._modules_section, app._module_editor],
                )
                self.assertTrue(app._modules_section.collapsed)
                self.assertTrue(app._module_editor.collapsed)
                self.assertFalse(hasattr(app, '_module_advanced'))
                self.assertIs(app._module_editor.master, app._export_inner)
                self.assertIs(app._modules_section.master, app._export_inner)
                self.assertIs(app._guidance_advanced.master, app._export_inner)
                self.assertTrue(app._guidance_advanced.collapsed)
                self.assertNotIn('w_guidance_hint', app._settings)
                self.assertEqual(len(app._host_settings['module_summaries']), 4)
                self.assertIn('component', app._host_settings['module_summaries'])
                self.assertNotIn('guidance_python', app._host_settings['path_vars'])
                self.assertNotIn('guidance_depth_code', app._host_settings['path_vars'])
                self.assertNotIn('\n', app._host_settings['w_mod_hint'].cget('text'))
                for field, code in (('v_guidance_device', 'cuda'), ('v_flow_direction', 'forward_negated'), ('v_depth_encoder', 'vitb')):
                    app._host_settings[field].set(gui.tr('guidance.option.' + code))
                app._host_settings['v_depth_profile'].set(gui.tr('guidance.option.fp32'))
                app._host_settings['v_guidance_execution'].set(gui.tr('guidance.option.serial'))
                collected = app._collect_host_settings()
                self.assertEqual(collected['guidance_device'], 'cuda')
                self.assertEqual(collected['guidance_flow_direction'], 'forward_negated')
                self.assertEqual(collected['guidance_depth_encoder'], 'vitb')
                self.assertEqual(collected['guidance_depth_profile'], 'fp32')
                previous_hash = app._settings_hash()
                app._host_settings['v_depth_profile'].set(gui.tr('guidance.option.sdpa_fp16'))
                self.assertEqual(app._collect_persisted_settings()['guidance_depth_profile'], 'sdpa_fp16')
                self.assertNotEqual(app._settings_hash(), previous_hash)
                app._host_settings['v_depth_profile'].set(gui.tr('guidance.option.fp32'))
                previous_hash = app._settings_hash()
                app._host_settings['v_guidance_execution'].set(gui.tr('guidance.option.raft_streams'))
                self.assertEqual(app._collect_persisted_settings()['guidance_execution'], 'raft_streams')
                self.assertNotEqual(app._settings_hash(), previous_hash)
                app._host_settings['v_guidance_execution'].set(gui.tr('guidance.option.serial'))
                app._host_settings['v_guidance_device'].set(gui.tr('guidance.option.auto'))
                app._host_settings['v_flow_direction'].set(gui.tr('guidance.option.backward'))
                app._host_settings['v_depth_encoder'].set(gui.tr('guidance.option.vitl'))
                self.assertIs(app._settings['v_guidance'], app._host_settings['v_guidance'])
                self.assertNotIn(app._settings['w_guidance'], list(_iter_widgets(app._settings_frame)))
                self.assertIn(app._settings['w_guidance'], list(_iter_widgets(app._guidance_page)))
                self.assertNotIn(app._host_settings['w_runtime'], list(_iter_widgets(app._host_section)))
                self.assertNotIn(app._host_settings['w_runtime'], list(_iter_widgets(app._modules_section)))
                self.assertIn(app._host_settings['w_runtime'], list(_iter_widgets(app._module_editor)))
                self.assertEqual(app._collect_host_settings()['dlss_runtime'], '')
                app._host_settings['v_runtime'].set(gui.tr('mods.bundled'))
                self.assertEqual(app._collect_host_settings()['dlss_runtime'], '__bundled__')
                app._host_settings['v_runtime'].set(gui.tr('mods.auto'))
                path_vars = app._host_settings['path_vars']
                path_vars['guidance_flow_weights'].set('custom-flow.pth')
                self.assertEqual(app._collect_persisted_settings()['guidance_flow_weights'], 'custom-flow.pth')
                app._reset_module_paths()
                deadline = time.monotonic() + 3
                while app._module_reload_thread is not None and time.monotonic() < deadline:
                    app.root.update()
                    time.sleep(0.005)
                self.assertIsNone(app._module_reload_thread)
                self.assertEqual(app._collect_host_settings()['guidance_depth_encoder'], 'auto')
                self.assertEqual(app._collect_persisted_settings()['guidance_flow_weights'], '')
                self.assertEqual(path_vars['guidance_flow_weights'].get(), app._module_path_defaults['guidance_flow_weights'])
                combos = [
                    widget for widget in _iter_widgets(app.root)
                    if isinstance(widget, ChromeCombobox)
                ]
                self.assertGreaterEqual(len(combos), 8)
                self.assertEqual(str(app.root.bind_class("TCombobox", "<MouseWheel>")), "")
                snapshot = [combo.get() for combo in combos]
                for combo in combos:
                    combo.combo.event_generate("<MouseWheel>", delta=-120)
                app.root.update()
                self.assertEqual([combo.get() for combo in combos], snapshot)
                sample = combos[0]
                sample.combo.selection_range(0, "end")
                sample.combo.event_generate("<FocusOut>")
                app.root.update()
                self.assertFalse(sample.combo.selection_present())
                self.assertEqual(str(app.workspace_tabs.tab(0, "text")), "画面效果")
                self.assertEqual(app._ui_theme_name, "dark")
                app.toggle_ui_theme()
                self.assertEqual(app._ui_theme_name, "light")
                self.assertEqual(app._collect_persisted_settings()["ui_theme"], "light")
                self.assertEqual(
                    str(app.canvas.cget("bg")).lower(),
                    ui_theme.tokens("light")["canvas"].lower(),
                )
                self.assertNotEqual(
                    ui_theme.tokens("light")["canvas"],
                    ui_theme.tokens("dark")["canvas"],
                )
                app.toggle_ui_theme()
                self.assertEqual(app._ui_theme_name, "dark")
                self.assertEqual(
                    str(app.canvas.cget("bg")).lower(),
                    ui_theme.tokens("dark")["canvas"].lower(),
                )
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
                app._export_settings["v_output_container"].set("MKV")
                app._probe_queue_video = lambda _path: (
                    {"frames": 24, "fps": 24.0, "width": 1280, "height": 720},
                    {"is_hdr": True, "label": "HDR10 / PQ", "profile": "pq"},
                )
                self.assertEqual(
                    app._add_paths_to_queue([hdr_source], switch_tab=False), 1,
                )
                self.assertEqual(app._queue_jobs[0].export_settings["mode"], "single")
                self.assertEqual(app._queue_jobs[0].export_settings["output_container"], "mkv")
                self.assertTrue(app._queue_jobs[0].output_path.endswith(".mkv"))
                app._queue_jobs = []
                app._save_queue_state()
                app._refresh_queue_tree(keep_selection=False)
                app._export_settings["v_mode"].set("严格时序（单会话）")
                app._export_settings["v_output_container"].set("MP4（推荐）")
                app._probe_queue_video = lambda _path: (
                    {"frames": 48, "fps": 24.0, "width": 1920, "height": 1080},
                    {"is_hdr": False, "label": "SDR / sRGB"},
                )

                second = os.path.join(tmp, "queued-second.mp4")
                open(second, "wb").close()
                image_source = os.path.join(tmp, "queued-image.png")
                _write_image_bgr(image_source, np.zeros((12, 16, 3), np.uint8))
                image_output = os.path.join(tmp, "standalone-image.png")
                original_process_image = app._process_still_image
                app._process_still_image = lambda image, settings: image.copy()
                try:
                    image_result = app._export_image_source(
                        image_source, app._collect_settings(),
                        out_path=image_output, notify=False,
                    )
                finally:
                    app._process_still_image = original_process_image
                self.assertTrue(image_result["success"])
                self.assertEqual(_read_image_bgr(image_output).shape, (12, 16, 3))
                self.assertEqual(
                    app._add_paths_to_queue(
                        [source, image_source, second], switch_tab=False,
                    ),
                    3,
                )
                self.assertEqual(app._queue_jobs[1].media_kind, "image")
                self.assertTrue(app._queue_jobs[1].output_path.endswith(".png"))
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
                app._export_image_source = lambda path, **kwargs: {
                    "success": True,
                    "cancelled": False,
                    "error": "",
                    "output_path": kwargs["out_path"],
                    "frames": 1,
                }
                try:
                    app._queue_running = True
                    app._run_next_queue_job()
                    self.assertEqual(app._queue_jobs[0].state, "completed")
                    self.assertEqual(len(callbacks), 1)
                    callbacks.pop(0)()
                    self.assertEqual(app._queue_jobs[1].state, "completed")
                    callbacks.pop(0)()
                    self.assertEqual(app._queue_jobs[2].state, "failed")
                    self.assertEqual(app._queue_jobs[2].error, "simulated failure")
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
                self.assertTrue(app._export_section.collapsed)
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
                for after_id in root.tk.call("after", "info"):
                    root.after_cancel(after_id)
                root.destroy()
                if old is None:
                    os.environ.pop("DLSS5TOOL_SETTINGS_PATH", None)
                else:
                    os.environ["DLSS5TOOL_SETTINGS_PATH"] = old
                if old_queue is None:
                    os.environ.pop("DLSS5TOOL_QUEUE_PATH", None)
                else:
                    os.environ["DLSS5TOOL_QUEUE_PATH"] = old_queue

    def test_queue_resume_after_stop_and_stable_export_chrome(self):
        import tkinter as tk

        with tempfile.TemporaryDirectory() as tmp:
            settings_path = os.path.join(tmp, "dlss5_settings.json")
            queue_path = os.path.join(tmp, "dlss5_queue.json")
            old = os.environ.get("DLSS5TOOL_SETTINGS_PATH")
            old_queue = os.environ.get("DLSS5TOOL_QUEUE_PATH")
            os.environ["DLSS5TOOL_SETTINGS_PATH"] = settings_path
            os.environ["DLSS5TOOL_QUEUE_PATH"] = queue_path
            root = tk.Tk()
            root.withdraw()
            try:
                app = App(root)
                source = os.path.join(tmp, "clip.mp4")
                open(source, "wb").close()
                app._probe_queue_video = lambda _path: (
                    {"frames": 24, "fps": 24.0, "width": 1280, "height": 720},
                    {"is_hdr": False, "label": "SDR / sRGB"},
                )
                self.assertEqual(app._add_paths_to_queue([source], switch_tab=False), 1)
                job = app._queue_jobs[0]
                job.state = "cancelled"
                job.error = "用户取消了当前任务。"
                job.progress_done = 4
                job.progress_total = 24
                job.export_settings["super_resolution_scale"] = 2
                app._queue_last_summary = "paused"
                app._refresh_queue_tree()
                self.assertTrue(app.queue_start_btn.instate(["!disabled"]))
                self.assertEqual(str(app.queue_start_btn.cget("text")), "继续队列")
                app._confirm_super_resolution_export = lambda *args, **kwargs: False
                app.start_export_queue()
                self.assertEqual(job.state, "cancelled")
                self.assertEqual(job.error, "用户取消了当前任务。")
                self.assertEqual(job.progress_done, 4)
                self.assertFalse(app._queue_running)

                job.export_settings["super_resolution_scale"] = 1
                scheduled = []
                app.root.after_idle = lambda callback: scheduled.append(callback)
                app.start_export_queue()
                self.assertEqual(job.state, "pending")
                self.assertTrue(app._queue_running)
                self.assertEqual(scheduled, [app._run_next_queue_job])
                app._queue_running = False
                app._queue_last_summary = "paused"
                app._update_queue_action_states()

                job.state = "failed"
                job.error = "simulated failure"
                app._queue_last_summary = None
                app._refresh_queue_tree()
                self.assertTrue(app.queue_start_btn.instate(["disabled"]))
                self.assertEqual(job.state, "failed")
                app._prepare_queue_jobs_for_start()
                self.assertEqual(job.state, "failed")

                job.state = "running"
                app._queue_running = True
                app._queue_active_job_id = job.job_id
                app._exporting = True
                app._source_kind = "video"
                app._update_queue_action_states()
                app.cancel_current_queue_job()
                self.assertTrue(app._queue_pause_requested)
                self.assertTrue(app._export_cancel_event.is_set())

                app._exporting = False
                app._queue_running = False
                app._export_cancel_event.clear()
                app._update_action_labels()
                self.assertEqual(str(app.export_btn.winfo_manager()), "grid")
                self.assertEqual(str(app.cancel_export_btn.winfo_manager()), "")
                app._layout_export_run_controls(True)
                self.assertEqual(str(app.export_btn.winfo_manager()), "")
                self.assertEqual(str(app.cancel_export_btn.winfo_manager()), "grid")
                self.assertEqual(int(app.cancel_export_btn.grid_info()["columnspan"]), 2)
                app._layout_export_run_controls(False)
                self.assertEqual(str(app.export_btn.winfo_manager()), "grid")

                self.assertEqual(str(app._progress_rule.winfo_manager()), "pack")
                app.set_progress(6, 24, "导出")
                self.assertEqual(int(float(app.pbar["value"])), 6)
                self.assertEqual(int(float(app.pbar["maximum"])), 24)
                self.assertEqual(str(app._progress_rule.winfo_manager()), "pack")
                app.toggle_log_panel()
                self.assertEqual(str(app.log.frame.winfo_manager()), "")
                packed = list(app.root.pack_slaves())
                self.assertEqual(packed[-2:], [app._progress_rule, app._status_bar])
                app._cancel_after("_settings_save_after")
            finally:
                for after_id in root.tk.call("after", "info"):
                    root.after_cancel(after_id)
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
                dark = ui_theme.tokens("dark")
                self.assertEqual(
                    str(app._settings["w_intensity"].cget("troughcolor")).lower(),
                    dark["slider_trough_off"].lower(),
                )
                self.assertEqual(
                    str(app._settings["w_local_tone"].cget("troughcolor")).lower(),
                    dark["slider_trough_on"].lower(),
                )
                self.assertEqual(
                    str(app._settings["w_local_tone"]._trackcolor).lower(),
                    dark["slider_trough_off"].lower(),
                )
                self.assertNotEqual(
                    str(app._settings["w_local_tone"]._trackcolor).lower(),
                    str(app._settings["w_local_tone"].cget("troughcolor")).lower(),
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
                    dark["slider_trough_on"].lower(),
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
                for after_id in root.tk.call("after", "info"):
                    root.after_cancel(after_id)
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
