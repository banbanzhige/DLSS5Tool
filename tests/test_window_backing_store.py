"""Retained restore pixels, bounded resources and unchanged Tk content."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import time
import tkinter as tk
import unittest
from unittest import mock

import numpy as np

from dlss5tool import gui, ui_theme
from dlss5tool.ui_widgets import ChromeButton
from dlss5tool import window_backing_store as backing


class BlankFrameTests(unittest.TestCase):
    def test_black_media_is_allowed_but_unpainted_ui_is_rejected(self):
        pixels = np.full((80, 120, 4), 240, dtype=np.uint8)
        pixels[5:75, 5:80, :3] = 0
        self.assertTrue(backing.has_blank_blocks(pixels))
        self.assertFalse(backing.has_blank_blocks(pixels, ((5, 5, 80, 75),)))
        pixels[20:28, 95:103, :3] = 0
        self.assertTrue(backing.has_blank_blocks(pixels, ((5, 5, 80, 75),)))

    def test_small_black_glyphs_are_not_unpainted_rectangles(self):
        pixels = np.full((32, 48, 4), 30, dtype=np.uint8)
        pixels[2:28, 5:8, :3] = 0
        pixels[20:23, 2:25, :3] = 0
        self.assertFalse(backing.has_blank_blocks(pixels))

    def test_grid_scan_matches_a_dense_reference_at_edges_and_offsets(self):
        random = np.random.default_rng(5)
        for _ in range(120):
            pixels = random.integers(20, 255, (32, 40, 4), dtype=np.uint8)
            side = int(random.integers(5, 12))
            x, y = int(random.integers(0, 41-side)), int(random.integers(0, 33-side))
            pixels[y:y+side, x:x+side, :3] = 0
            excluded = ((0, 0, 7, 13),) if random.integers(0, 2) else ()
            black = np.all(pixels[:, :, :3] < 12, axis=2)
            for x1, y1, x2, y2 in excluded:
                black[y1:y2, x1:x2] = False
            expected = np.lib.stride_tricks.sliding_window_view(black, (8, 8)).all(axis=(-1, -2)).any()
            self.assertEqual(backing.has_blank_blocks(pixels, excluded), bool(expected), (x, y, side))


@unittest.skipUnless(os.name == 'nt', 'Native Windows backing store')
class NativeBackingTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.geometry('320x180+40+40')
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.canvas = tk.Canvas(self.root, background='#fafbfc', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.item = self.canvas.create_rectangle(0, 0, 100, 100, fill='#aa2277', outline='')
        self.root.deiconify()
        self.root.attributes('-topmost', True)
        self.root.lift()
        self.root.focus_force()
        self.root.update()
        self.store = backing.WindowBackingStore(self.root)
        self.root.update()

    def tearDown(self):
        self.store.close()
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def capture(self):
        self.root.focus_force()
        deadline = time.monotonic()+1.
        while True:
            self.root.update()
            if self.store.capture():
                return
            if time.monotonic() >= deadline:
                self.skipTest('A foreground native drawing surface is required')
            time.sleep(.01)

    def test_cached_pixels_replay_without_rebuilding_canvas_items(self):
        self.capture()
        pixel = self.store.frame.pixels()[25, 25, :3][::-1]
        self.assertEqual(tuple(pixel), (170, 34, 119))
        scene = self.canvas.find_all()
        self.root.iconify()
        self.root.update()
        cached = self.store.frame
        self.assertFalse(self.store.capture())
        self.assertIs(self.store.frame, cached)
        self.root.deiconify()
        self.root.update()
        self.assertEqual(self.canvas.find_all(), scene)
        self.assertGreater(self.store.replays, 0)
        self.assertIsNone(self.store.error)

    def test_native_minimize_capture_does_not_enter_tcl(self):
        self.capture()
        count = self.store.captures
        self.store._exclusions = mock.Mock(side_effect=AssertionError('Tcl from native callback'))
        with mock.patch.object(self.store.comctl, 'DefSubclassProc', return_value=0), \
                mock.patch.object(self.root, 'update', side_effect=AssertionError('nested update')), \
                mock.patch.object(self.root, 'update_idletasks', side_effect=AssertionError('nested paint')):
            self.assertEqual(self.store._wndproc(self.store.hwnd, 0x112, 0xF020, 0), 0)
        self.store._exclusions.assert_not_called()
        self.assertEqual(self.store.captures, count+1)
        self.assertIsNone(self.store.error)

    def test_resize_and_palette_changes_never_reuse_an_incompatible_frame(self):
        self.capture()
        old = self.store.frame
        self.store.invalidate()
        self.assertIsNone(self.store.frame)
        self.assertIsNone(old.bitmap)
        dark = ui_theme.tokens('dark')
        self.root.configure(bg=dark['bg'])
        self.canvas.configure(bg=dark['panel'])
        self.store.refresh_palette()
        self.root.geometry('400x220+40+40')
        self.root.update()
        self.capture()
        self.assertEqual(self.store.frame.width, self.root.winfo_width())
        self.assertEqual(tuple(self.store.frame.pixels()[150, 250, :3][::-1]), (26, 32, 40))

    def test_memory_cap_prevents_another_allocation_and_keeps_the_verified_frame(self):
        self.capture()
        old = self.store.frame
        self.assertLessEqual(old.width*old.height*4, backing.MAX_FRAME_BYTES)
        with mock.patch.object(backing, 'MAX_FRAME_BYTES', 1), \
                mock.patch.object(backing, '_Bitmap', side_effect=AssertionError('over budget')):
            self.assertFalse(self.store.capture())
        self.assertIs(self.store.frame, old)

    def test_native_popdowns_and_repeated_close_release_callbacks_and_bitmaps(self):
        self.capture()
        self.store._mapped(SimpleNamespace(widget='.native.popdown'))
        old = self.store.frame
        ident = id(self.store)
        self.store.close()
        self.store.close()
        self.root.update()
        self.assertIsNone(old.bitmap)
        self.assertIsNone(old.dc)
        self.assertIsNone(self.store._after)
        self.assertFalse(self.store._hooks)
        self.assertFalse(self.store._brushes)
        self.assertFalse(any(key[1] == ident for key in backing._LIVE))

    def test_repeated_captures_do_not_accumulate_gdi_objects(self):
        self.capture()
        import ctypes as c
        kernel = c.WinDLL('kernel32')
        kernel.GetCurrentProcess.restype = c.c_void_p
        count = self.store.user.GetGuiResources
        count.argtypes, count.restype = [c.c_void_p, c.c_uint], c.c_uint
        handle = kernel.GetCurrentProcess()
        before = count(handle, 0)
        for _ in range(10):
            self.assertTrue(self.store.capture())
        self.assertLessEqual(count(handle, 0), before+1)
        self.store.close()
        self.assertLess(count(handle, 0), before)


class RetainedButtonTests(unittest.TestCase):
    def test_remap_keeps_the_scene_and_real_resize_updates_it(self):
        root = tk.Tk()
        root.withdraw()
        ui_theme.apply_ttk(root, ui_theme.tokens('light'))
        button = ChromeButton(root, text='Restore', width=180)
        button.pack()
        root.deiconify()
        root.update()
        try:
            scene = button.find_all()
            with mock.patch.object(button, '_redraw_body', wraps=button._redraw_body) as draw:
                root.iconify()
                root.update()
                root.deiconify()
                root.update()
                draw.assert_not_called()
                self.assertEqual(button.find_all(), scene)
                button.configure(width=240)
                root.update()
                self.assertEqual(button._drawn_size, (button.winfo_width(), button.winfo_height()))
        finally:
            root.destroy()


class InspectorRestoreTests(unittest.TestCase):
    def test_resize_bursts_settle_scrollbars_without_nested_paint_or_state_reset(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {
                'DLSS5TOOL_SETTINGS_PATH': str(Path(temporary) / 'settings.json'),
                'DLSS5TOOL_QUEUE_PATH': str(Path(temporary) / 'queue.json')}):
            root = tk.Tk()
            root.withdraw()
            errors = []
            root.report_callback_exception = lambda *error: errors.append(error)
            app = gui.App(root)
            try:
                root.deiconify()
                root.update()
                app._frame = 37
                canvas, tabs, jobs = app.canvas, app.workspace_tabs, app._queue_jobs
                for theme in ('light', 'dark'):
                    app._apply_ui_theme(theme, persist=False)
                    for page, viewport, inner in (
                            ('preview', app._preview_canvas, app.preview_tab),
                            ('guidance', app._guidance_canvas, app._guidance_inner),
                            ('export', app._export_canvas, app._export_section.master)):
                        app.workspace_tabs.select(getattr(app, '_preview_page' if page == 'preview' else f'_{page}_page'))
                        root.update()
                        with mock.patch.object(viewport, 'update_idletasks',
                                               side_effect=AssertionError('nested layout paint')):
                            for _ in range(20):
                                getattr(app, f'_sync_{page}_scrollregion')(SimpleNamespace(widget=inner))
                            self.assertEqual(len(app._scrollregion_after), 1)
                            root.update()
                        self.assertFalse(app._scrollregion_after)
                        region = tuple(map(float, viewport.cget('scrollregion').split()))
                        self.assertEqual(region[3], inner.winfo_reqheight())
                        root.iconify()
                        root.update()
                        root.deiconify()
                        root.update()
                        self.assertIs(app.canvas, canvas)
                        self.assertIs(app.workspace_tabs, tabs)
                        self.assertIs(app._queue_jobs, jobs)
                        self.assertEqual(app._frame, 37)
                self.assertEqual(errors, [])
            finally:
                app._on_close()
