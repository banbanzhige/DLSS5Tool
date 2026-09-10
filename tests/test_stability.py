"""Model-switch, still-preview and reported RTX 4060 regressions (no GPU needed)."""
import queue
import threading
import unittest
from unittest import mock

import numpy as np

from dlss5tool import diagnostics, gui
from dlss5tool.preview_comparison import guidance_input_pair
from tests import test_gui_module_reload as reload_tests


class SwitchStabilityTests(unittest.TestCase):
    setUp = reload_tests.ModuleReloadTests.setUp
    wait_reload = reload_tests.ModuleReloadTests.wait_reload

    def test_paused_analysis_refreshes_after_switch(self):
        app = self.app
        app._guidance_context = True
        app.playing = False
        app.view_var = mock.Mock(get=lambda: 'original')
        app.display_view = mock.Mock()
        app._schedule_full_preview = mock.Mock()
        gui.App._resume_preview_cache(app)
        app.display_view.assert_called_once_with()
        app._schedule_full_preview.assert_not_called()

    def test_warm_activation_reuses_real_session_without_disposable_probe(self):
        app = self.app
        app._guidance_context = True
        app._guidance_preview_epoch = 0
        app._frame = 2
        app._source_kind = 'video'
        app._video_color_info = {}
        app._collect_settings = lambda: {'guidance_mode': 1}
        app._collect_host_settings = app._collect_settings
        app._host_settings = {'v_guidance': mock.Mock()}
        app._guidance_preview_key = lambda: ('new-generation', app._guidance_generation)
        live = mock.Mock()
        image = np.zeros((4, 8, 3), np.uint8)

        def warm(request):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            app._live = live
            return {'info': {'device': 'cuda'},
                    'preview': (app.video, 2, 0, image, {'flow': image}, False)}

        with mock.patch.object(gui.guidance_client, 'preflight') as probe:
            app._warm_guidance_preview = mock.Mock(side_effect=warm)
            app._on_mod_settings_change()
            self.wait_reload()
            probe.assert_not_called()
        self.assertIs(app._live, live)
        self.assertEqual(app._guidance_result[0], app._guidance_preview_key())
        self.assertFalse(app._switching_backend)

    def test_failed_warm_session_closes_and_does_not_commit_mode(self):
        app = self.app
        app._guidance_context = True
        app._guidance_preview_epoch = 0
        app._frame = 0
        app._source_kind = 'video'
        app._video_color_info = {}
        app._collect_settings = lambda: {'guidance_mode': 1}
        app._collect_host_settings = app._collect_settings
        app._host_settings = {'v_guidance': mock.Mock()}
        app._warm_guidance_preview = mock.Mock(side_effect=RuntimeError('0xBAD00002'))
        app._close_live = mock.Mock()
        app._on_mod_settings_change()
        self.wait_reload()
        self.assertEqual(app._close_live.call_count, 2)
        app._host_settings['v_guidance'].set.assert_called_once_with(gui.tr('guidance.mode.0'))
        self.assertIn('0xBAD00002', app._guidance_preflight_error)


class StillPreviewTests(unittest.TestCase):
    def test_paused_worker_failure_stops_buffering_and_remains_visible(self):
        app = gui.App.__new__(gui.App)
        app._preview_worker_error = 'CreateFeature(18) -> 0xBAD00002'
        for name in ('pause', '_freeze_preview_cache', 'logln', 'set_status', '_draw_work_status'):
            setattr(app, name, mock.Mock())
        app.canvas = mock.Mock()
        app._preview_session_active = mock.Mock()
        app._preview_decode_tick()
        app._preview_session_active.assert_not_called()
        app._freeze_preview_cache.assert_called_once_with(resume_ms=None)
        app._draw_work_status.assert_called_once_with(gui.tr('status.preview_failed'))
        self.assertIn('0xBAD00002', app.logln.call_args.args[0])
        self.assertIsNone(app._preview_worker_error)

    def test_still_full_preview_queues_instead_of_waiting_on_tk(self):
        app = gui.App.__new__(gui.App)
        app._source_kind = 'image'
        app._image_bgr = np.zeros((1, 1, 3), np.uint8)
        app.playing = app._exporting = app._hold_original = False
        app.video = 'image.png'
        app.view_var = mock.Mock(get=lambda: 'dlss')
        app._frame = 0
        app._precise_preview_size = lambda: (1920, 1080)
        app._cached_dlss = lambda *args: None
        app.display_view = mock.Mock()
        app._display_precise_preview = mock.Mock()
        app._start_paused_prerender = mock.Mock(return_value=True)
        app._update_preview_timeline_and_status = mock.Mock()
        app._apply_full_preview()
        app._display_precise_preview.assert_not_called()
        app.display_view.assert_called_once_with(quality='fast')
        app._start_paused_prerender.assert_called_once_with(target_size=(1920, 1080))
        app._pre_rendering = True
        self.assertTrue(app._preview_session_active())
        app._cached_dlss = lambda *args: np.zeros((1, 1, 3), np.uint8)
        app._start_paused_prerender.reset_mock()
        app._apply_full_preview()
        app._display_precise_preview.assert_called_once_with()
        app._start_paused_prerender.assert_not_called()

    def test_still_pair_is_bounded_and_has_no_false_motion(self):
        still = np.zeros((600, 1200, 3), np.uint8)
        with mock.patch('dlss5tool.preview_comparison.cv2.VideoCapture') as capture:
            current, previous = guidance_input_pair('image.png', 0, still,
                                                    {'guidance_mode': 1, 'guidance_flow_edge': 512})
        capture.assert_not_called()
        self.assertEqual(current.shape, (256, 512, 4))
        self.assertIsNone(previous)

    def test_warm_still_exercises_temporal_kernels_then_resets(self):
        app = gui.App.__new__(gui.App)
        app._live_lock = threading.RLock()
        live = mock.Mock()
        live.guidance_preview.return_value = ({'flow': np.zeros((4, 8, 3), np.uint8)}, True)
        live.guidance_info = {'cache_version': 'raw_lru_v2_shared'}
        live.guidance_metrics = {}
        app._ensure_live = mock.Mock(return_value=live)
        image = np.zeros((4, 8, 3), np.uint8)
        result = app._warm_guidance_preview(('image.png', 0, image, {'guidance_mode': 1}, 3))
        self.assertEqual(live.guidance_preview.call_count, 2)
        self.assertIsNotNone(live.guidance_preview.call_args_list[0].args[1])
        self.assertIsNone(live.guidance_preview.call_args_list[1].args[1])
        self.assertTrue(result['preview'][-1])
        live.close.assert_not_called()


class PlatformErrorTests(unittest.TestCase):
    def test_reported_4060_error_is_platform_not_unknown_or_unsupported(self):
        text = '\n'.join(diagnostics._probe_hints({'ok': False, 'native_log':
            'Init_Ext -> 0x00000001\nAllocateParameters -> 0x00000001\n'
            'CreateFeature(18) -> 0xBAD00002 feature=0000000000000000'}))
        self.assertIn('PlatformError', text)
        self.assertIn('尚未处理输入图像', text)
        self.assertNotIn('未匹配已知错误', text)
        self.assertNotIn('显存不足，请', text)
        self.assertIn('PlatformError', gui._dlss_runtime_guidance('0xBAD00002'))
        self.assertFalse(gui._is_dlss_runtime_unsupported('0xBAD00002'))

    def test_error_codes_are_case_insensitive_and_distinct(self):
        for code, expected in (('0xbad0000c', 'OutOfDate'), ('0xBAD0000D', 'OutOfGPUMemory'),
                               ('0xBAD0000F', 'UnableToWriteToAppDataPath')):
            text = '\n'.join(diagnostics._probe_hints({'ok': False, 'native_log': code}))
            self.assertIn(expected, text)
            self.assertNotIn('PlatformError', text)


if __name__ == '__main__':
    unittest.main()
