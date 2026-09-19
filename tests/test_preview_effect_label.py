"""Preview labels reflect enabled preview effects, without loading GPU/media."""
import unittest
from unittest.mock import Mock

from dlss5tool import i18n, ui_theme
from dlss5tool.preview_comparison import PreviewComparison
from dlss5tool.shared_render_preview import SharedRenderPreview


class PreviewLabelHarness(PreviewComparison, SharedRenderPreview):
    def __init__(self, sr_preview=False, fg_preview=False, scale=4, multiplier=2):
        self._export_settings = {
            'v_preview_super_resolution': Mock(get=Mock(return_value=sr_preview)),
            'v_preview_frame_generation': Mock(get=Mock(return_value=fg_preview)),
        }
        self._collect_export_settings = Mock(return_value={
            'super_resolution_scale': scale, 'frame_generation_multiplier': multiplier})
        self.canvas = Mock()
        self.canvas.create_text.return_value = 1
        self.canvas.create_rectangle.return_value = 2
        self.canvas.bbox.return_value = (100, 10, 290, 34)
        self.canvas.tk.call.return_value = 20
        from dlss5tool.gui import App
        self._canvas_shadow_text = App._canvas_shadow_text.__get__(self)
        self._ui_color = lambda name, default: ui_theme.THEMES['dark'].get(name, default)


class PreviewEffectLabelTests(unittest.TestCase):
    def setUp(self):
        language = i18n.get_language()
        self.addCleanup(i18n.set_language, language)
        i18n.set_language('zh_CN')

    def test_export_only_settings_do_not_appear(self):
        self.assertEqual(PreviewLabelHarness()._compare_label(), 'DLSS')

    def test_sr_and_fg_preview_combinations(self):
        for sr, fg, expected in (
            (True, False, 'DLSS · 超分 4×'),
            (False, True, 'DLSS · 插帧 2×'),
            (True, True, 'DLSS · 超分 4× · 插帧 2×'),
        ):
            self.assertEqual(PreviewLabelHarness(sr, fg)._compare_label(), expected)

    def test_disabled_multiplier_stays_hidden_even_if_checked(self):
        self.assertEqual(PreviewLabelHarness(True, True, 1, 1)._compare_label(), 'DLSS')

    def test_live_toggle_and_multiplier_update(self):
        preview = PreviewLabelHarness(True, True)
        preview._export_settings['v_preview_super_resolution'].get.return_value = False
        preview._collect_export_settings.return_value['frame_generation_multiplier'] = 4
        self.assertEqual(preview._compare_label(), 'DLSS · 插帧 4×')

    def test_images_match_image_rendering_not_video_checkbox(self):
        preview = PreviewLabelHarness(False, True)
        preview._is_image = True
        self.assertEqual(preview._compare_label(), 'DLSS · 超分 4×')

    def test_guidance_label_is_unchanged(self):
        preview = PreviewLabelHarness(True, True)
        preview._guidance_context = True
        preview.compare_target = Mock(get=Mock(return_value='flow'))
        self.assertEqual(preview._compare_label(), i18n.tr('view.flow'))
        preview._collect_export_settings.assert_not_called()

    def test_pending_keeps_effect_names_and_adds_status(self):
        preview = PreviewLabelHarness(True, True)
        preview._dlss_pending = True
        preview._draw_processed_preview_label(300, 10, 180)
        options = preview.canvas.create_text.call_args.kwargs
        self.assertEqual(options['text'], 'DLSS · 超分 4× · 插帧 2×…')
        self.assertEqual(options['anchor'], 'ne')
        self.assertEqual(options['width'], 180)
        self.assertEqual(options['font'], ui_theme.UI_FONT_SMALL)
        preview.canvas.create_rectangle.assert_not_called()
        self.assertEqual(preview.canvas.create_text.call_count, 2)  # original-style shadow and text

    def test_localization_and_theme_readability(self):
        preview = PreviewLabelHarness(True, True)
        i18n.set_language('en_US')
        self.assertEqual(preview._compare_label(), 'DLSS · Super Resolution 4× · Frame Generation 2×')
        for theme in ('light', 'dark'):
            preview._ui_color = lambda name, default: ui_theme.THEMES[theme].get(name, default)
            preview._draw_processed_preview_label(300, 10, 180)
            self.assertEqual(preview.canvas.create_text.call_args.kwargs['fill'], ui_theme.THEMES[theme]['overlay'])
            preview.canvas.create_rectangle.assert_not_called()


class PreviewBadgeCanvasTests(unittest.TestCase):
    def test_wipe_and_side_comparison_draw_effect_badge(self):
        import tkinter as tk
        import numpy as np
        from dlss5tool.gui import App
        root = tk.Tk()
        root.withdraw()
        try:
            ui_theme.configure_fonts(root)
            preview = PreviewLabelHarness(True, True)
            preview.canvas = tk.Canvas(root, width=640, height=240)
            preview._split_orig = np.zeros((180, 240, 3), np.uint8)
            preview._split_dlss = np.ones((180, 240, 3), np.uint8)
            preview._hold_original = False
            preview._dlss_pending = False
            preview.split_x = .5
            preview.view_var = Mock(get=Mock(return_value='compare'))
            preview.compare_layout = Mock(get=Mock(return_value='wipe'))
            preview._render_viewport_image = Mock(side_effect=lambda image, *a, **kw: (image, (10, 10, 240, 180)))
            preview._draw_navigator = Mock()
            preview._draw_pending_status = Mock()
            for layout in ('wipe', 'side'):
                preview.compare_layout.get.return_value = layout
                App._blit_split(preview, 640, 240)
                badge = preview.canvas.find_withtag('preview_effect_label')[-1]
                self.assertEqual(preview.canvas.itemcget(badge, 'text'), preview._compare_label())
                self.assertEqual(preview.canvas.find_withtag('preview_effect_badge'), ())
                self.assertEqual(bool(preview.canvas.find_withtag('split')), layout == 'wipe')
        finally:
            root.destroy()

    def test_badge_wraps_inside_narrow_canvas_in_both_languages(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            ui_theme.configure_fonts(root)
            preview = PreviewLabelHarness(True, True)
            preview.canvas = tk.Canvas(root, width=320, height=240)
            preview._dlss_pending = True
            language = i18n.get_language()
            try:
                for selected in ('zh_CN', 'en_US'):
                    for available in (140, 220):
                        i18n.set_language(selected)
                        preview.canvas.delete('all')
                        left = preview._canvas_shadow_text(10, 14, i18n.tr('view.original'), anchor='w', font=ui_theme.UI_FONT_SMALL)
                        item = preview._draw_processed_preview_label(290, 14, available)
                        x0, y0, x1, y1 = preview.canvas.bbox(item)
                        self.assertGreaterEqual(x0, 290 - available - 3)
                        self.assertLessEqual(x1, 293)
                        self.assertLessEqual(abs(y0 - preview.canvas.bbox(left)[1]), 1)
                        self.assertLess(y1, 230)
                        self.assertEqual(preview.canvas.find_withtag('preview_effect_badge'), ())
            finally:
                i18n.set_language(language)
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main()
