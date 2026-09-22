from fractions import Fraction
import io
from pathlib import Path
import threading
import unittest
from unittest import mock

import numpy as np

from dlss5tool import frame_generation as fg


class TimingTests(unittest.TestCase):
    def test_cfr_decimal_timestamp_quantization(self):
        self.assertEqual(fg.validate_timestamps(['0', '0.041667', '0.083333'], Fraction(24)), 3)
        self.assertEqual(fg.validate_timestamps(['0', '.033367', '.066733'], Fraction(30000, 1001)), 3)

    def test_vfr_missing_repeated_or_offset_timestamps_rejected(self):
        for stamps in ([], ['0', '.04', '.1'], ['0', '0'], ['1', '1.041667'], ['0', 'N/A']):
            with self.subTest(stamps=stamps), self.assertRaises(ValueError):
                fg.validate_timestamps(stamps, Fraction(24))

    def test_exact_pipe_and_truncated_output(self):
        self.assertEqual(fg._read_exact(io.BytesIO(b'abcd'), 4), b'abcd')
        with self.assertRaises(RuntimeError):
            fg._read_exact(io.BytesIO(b'abc'), 4)

    def test_cut_and_still(self):
        black = np.zeros((180, 320, 4), np.uint8)
        white = np.full_like(black, 255)
        self.assertFalse(fg.is_cut(black, black))
        self.assertTrue(fg.is_cut(black, white))

    def test_cancel_is_explicit(self):
        event = threading.Event()
        fg.check_cancel(event)
        event.set()
        with self.assertRaises(fg.Cancelled):
            fg.check_cancel(event)

    def test_existing_output_rejected_before_gpu_or_filesystem_writes(self):
        path = Path(__file__)
        with self.assertRaisesRegex(ValueError, '不覆盖'):
            fg.export_video(path, path)

    def test_inspect_source_reports_counted_timestamp_progress(self):
        lines = ''.join(f'{i / 24:.6f}\n' for i in range(24)).encode()
        proc = mock.Mock()
        proc.stdout = io.BytesIO(lines)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        reports = []
        meta = {
            'r_frame_rate': '24/1', 'width': 64, 'height': 48,
            'frames': 24, 'nb_frames': '24',
        }
        with mock.patch.object(fg, 'find_ffmpeg', return_value='ffmpeg'), \
                mock.patch.object(fg, 'find_ffprobe', return_value='ffprobe'), \
                mock.patch.object(fg, 'probe_video_stream', return_value=meta), \
                mock.patch.object(fg.subprocess, 'Popen', return_value=proc):
            result = fg.inspect_source(
                'clip.mp4', threading.Event(),
                progress=lambda *args: reports.append(args),
            )
        self.assertEqual(result[4], 24)
        self.assertGreaterEqual(len(reports), 2)
        self.assertEqual(reports[0][2], (0, 24))
        self.assertEqual(reports[-1][2], (24, 24))
        self.assertTrue(all(item[2][0] <= 24 for item in reports))


class NativeProtocolTests(unittest.TestCase):
    def session(self):
        stream = fg.NativeStream.__new__(fg.NativeStream)
        stream.shape = (2, 2, 4)
        stream.dtype = np.float16
        stream.bytes = 32
        stream.multiplier = 2
        stream.call = mock.Mock()
        return stream

    def test_hdr_overwhite_not_silently_clipped(self):
        stream = self.session()
        for value in (-.1, 1.1, float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                stream.process(np.full((2, 2, 4), value, np.float16), np.zeros((2, 2, 2), np.float32), False)
        stream.call.assert_not_called()

    def test_motion_and_dimensions_validated(self):
        stream = self.session()
        with self.assertRaises(ValueError):
            stream.process(np.zeros((3, 2, 4), np.float16), np.zeros((2, 2, 2), np.float32), False)
        with self.assertRaises(ValueError):
            stream.process(np.zeros((2, 2, 4), np.float16), np.full((2, 2, 2), np.nan, np.float32), False)

    def test_gui_uses_inline_export_control(self):
        import ast
        tree = ast.parse((Path(__file__).parents[1] / 'dlss5tool/gui.py').read_text(encoding='utf-8'))
        methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertNotIn('open_frame_generation_lab', methods)
        self.assertIn('v_frame_generation', ast.unparse(methods['_build_export_quick']))
        self.assertIn('_export_frame_generated_video', ast.unparse(methods['_export_video_source']))


class InlineControlTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from dlss5tool import gui, ui_theme, app_settings
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = gui.App.__new__(gui.App)
        self.app.root = self.root
        self.app._ui = ui_theme.THEMES['light']
        ui_theme.apply_ttk(self.root, self.app._ui)
        self.app._saved_settings = dict(app_settings.DEFAULTS)
        self.app._theme_widgets = []
        self.app._update_export_control_states = mock.Mock()
        self.app._on_export_settings_change = mock.Mock()
        self.app._on_super_resolution_change = mock.Mock()
        from tkinter import ttk
        self.settings_parent = ttk.Frame(self.root)
        self.app._export_settings = self.app._build_export_settings(self.settings_parent)
        self.quick = ttk.Frame(self.root)
        self.quick.pack()
        self.app._build_export_quick(self.quick)
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_inline_row_after_upscale_and_shared_selection(self):
        fields = self.app._export_quick_fields
        self.assertEqual(list(fields), ['w_output_container', 'w_output_resolution', 'w_super_resolution', 'w_frame_generation'])
        d = self.app._export_settings
        d['v_frame_generation'].set('4×')
        self.assertEqual(self.app._collect_export_settings()['frame_generation_multiplier'], 4)
        self.assertEqual(fields['w_frame_generation'].get(), '4×')
        self.assertEqual(d['w_frame_generation'].get(), '4×')

    def test_light_and_dark_layout(self):
        from dlss5tool import ui_theme
        self.root.attributes('-alpha', 0)
        self.root.deiconify()
        self.quick.pack(fill='x', expand=True)
        for name in ('dark', 'light'):
            ui_theme.apply_ttk(self.root, ui_theme.THEMES[name])
            self.root.geometry('430x310')
            self.root.update()
            fields = self.app._export_quick_fields
            self.assertEqual(fields['w_frame_generation'].winfo_width(), fields['w_super_resolution'].winfo_width())
            for effect, toggle in self.app._effect_preview_controls.items():
                toggle.apply_theme(ui_theme.THEMES[name])
                self.root.update_idletasks()
                self.assertIn(toggle, self.app._theme_widgets)
                combo = fields['w_'+effect]
                self.assertGreaterEqual(toggle.winfo_x(), combo.winfo_x()+combo.winfo_width())
                self.assertLessEqual(toggle.winfo_x()+toggle.winfo_width(), toggle.master.winfo_width())

    def test_inline_preview_toggles_are_independent_and_do_not_change_export(self):
        controls = self.app._effect_preview_controls
        self.assertEqual(set(controls), {'super_resolution', 'frame_generation'})
        d = self.app._export_settings
        self.assertTrue(d['v_preview_super_resolution'].get())
        self.assertTrue(d['v_preview_frame_generation'].get())
        d['v_frame_generation'].set('4×')
        d['v_preview_frame_generation'].set(True)
        self.assertEqual(self.app._preview_effect_settings()['frame_generation_multiplier'], 4)
        d['v_preview_frame_generation'].set(False)
        self.assertEqual(self.app._preview_effect_settings()['frame_generation_multiplier'], 1)
        self.assertEqual(self.app._collect_export_settings()['frame_generation_multiplier'], 4)
        for effect, control in controls.items():
            combo = self.app._export_quick_fields['w_'+effect]
            self.assertIs(control.master, combo.master)
            self.assertGreater(control.winfo_reqwidth(), 25)

    def test_settings_validation_and_queue_snapshot(self):
        from dlss5tool import app_settings, export_queue
        for value in (1, 2, 3, 4):
            self.assertEqual(app_settings.validate({'frame_generation_multiplier': value})['frame_generation_multiplier'], value)
        for value in (0, 5, '4', True, 2.5):
            self.assertEqual(app_settings.validate({'frame_generation_multiplier': value})['frame_generation_multiplier'], 1)
        export = {'frame_generation_multiplier': 3}
        job = export_queue.ExportJob.create('video.mp4', 'out.mp4', {}, export)
        export['frame_generation_multiplier'] = 4
        restored = export_queue.ExportJob.from_dict(job.to_dict())
        self.assertEqual(restored.export_settings['frame_generation_multiplier'], 3)

    def test_main_export_worker_passes_all_settings_and_cancellation(self):
        from dlss5tool import gui
        from dlss5tool import render_cache
        self.app._export_cancel_event = threading.Event()
        self.app.set_progress = mock.Mock()
        export = {'frame_generation_multiplier': 3, 'super_resolution_scale': 2,
                  'output_size': (256, 144), 'quality_profile': 'maximum', 'hdr_mode': False}
        manager = mock.Mock()
        self.app._shared_manager = lambda: manager
        with mock.patch.object(render_cache, 'encode_cached', return_value={'real_frames': 24}) as run:
            result = self.app._export_frame_generated_video('a.mp4', 'b.mp4', {'output_mix': .7}, export)
        self.assertEqual(result['real_frames'], 24)
        config = run.call_args.args[2]
        self.assertEqual(config['frame_generation_multiplier'], 3)
        self.assertEqual(config['super_resolution_scale'], 2)
        self.assertEqual(config['output_mix'], .7)
        self.assertEqual(config['output_size'], (256, 144))
        self.assertIs(run.call_args.args[0], manager.session.return_value)
        self.assertIs(run.call_args.args[3], self.app._export_cancel_event)
        with mock.patch.object(render_cache, 'encode_cached', side_effect=fg.Cancelled('cancel')):
            with self.assertRaises(gui._ExportCancelled):
                self.app._export_frame_generated_video('a.mp4', 'b.mp4', {}, export)

    def test_export_request_routes_to_fg_for_single_and_queue(self):
        import types
        from dlss5tool import gui
        app = self.app
        app._collect_settings = lambda: {'output_view': 0, 'output_mix': .5}
        app._ensure_shared_cache_pool = lambda: types.SimpleNamespace(name='cache')
        app._video_info = lambda _: (24, 24, 320, 180)
        app._confirm_super_resolution_export = lambda *args, **kwargs: True
        app._wait_play_dlss = mock.Mock()
        app._close_super_resolution = mock.Mock()
        app._close_live = mock.Mock()
        app.logln = mock.Mock()
        app._begin_export_ui = lambda _: setattr(app, '_export_t0', 0)
        app._end_export_ui = mock.Mock()
        app._export_frame_generated_video = mock.Mock(return_value={'real_frames': 24, 'output_rate': '72', 'generated_frames': 46})
        app._export_settings['v_frame_generation'].set('3×')
        for notify in (True, False):
            result = app._export_video_source('not-existing-source.mp4', {}, {'frame_generation_multiplier': 3}, {}, out_path='not-existing-result.mp4', notify=notify)
            self.assertTrue(result['success'])
            self.assertEqual(result['frames'], 24)
        self.assertEqual(app._export_frame_generated_video.call_count, 2)
        app._export_frame_generated_video.reset_mock()
        with mock.patch.object(gui, 'FFmpegVideoWriter', side_effect=RuntimeError('legacy route')), \
                mock.patch.object(gui.traceback, 'print_exc'):
            legacy = app._export_video_source('not-existing-source.mp4', {}, {}, {}, out_path='not-existing-result.mp4', notify=False)
        self.assertFalse(legacy['success'])
        app._export_frame_generated_video.assert_not_called()


if __name__ == '__main__':
    unittest.main()
