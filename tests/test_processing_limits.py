"""Relaxed capability boundaries without relaxing color/texture safety."""
import unittest
from unittest import mock

import numpy as np

from dlss5tool import app_settings, guidance_client, dlss_engine, super_resolution
from dlss5tool.guidance_color import analysis_rgba8
from dlss5tool.guidance_parameters import parameters
from dlss5tool.gui import _large_image_host_settings
from dlss5tool.preview_comparison import guidance_input_pair


class ColorProxyTests(unittest.TestCase):
    def test_sdr_is_identity(self):
        frame = np.zeros((8, 9, 4), np.uint8)
        self.assertIs(analysis_rgba8(frame, {}), frame)

    def test_hdr_profiles_preserve_original_and_have_monotonic_proxy(self):
        for profile in ('hdr10_pq', 'hdr10_hlg', 'scrgb'):
            with self.subTest(profile=profile):
                source = np.ones((260, 256, 4), np.float16)
                source[..., :3] = np.linspace(0, 1, 256, dtype=np.float16)[None, :, None]
                if profile == 'scrgb':
                    source[..., :3] = source[..., :3] * 12 - 1
                before = source.copy()
                settings = {'frame_format': 'rgba16f', 'color_profile': profile}
                proxy = analysis_rgba8(source, settings)
                np.testing.assert_array_equal(source, before)
                self.assertEqual(proxy.dtype, np.uint8)
                self.assertEqual(proxy.shape, source.shape)
                self.assertTrue(proxy.flags.c_contiguous)
                self.assertTrue((np.diff(proxy[0, :, 0].astype(int)) >= 0).all())
                self.assertGreater(len(np.unique(proxy[..., 0])), 70)
                np.testing.assert_array_equal(proxy[..., 3], 255)
                np.testing.assert_array_equal(proxy, analysis_rgba8(source, settings))
                # No per-frame auto exposure or dependence on row chunk size.
                np.testing.assert_array_equal(proxy[:2], analysis_rgba8(source[:2], settings))

    def test_bad_profile_and_nonfinite_fail_explicitly(self):
        frame = np.ones((2, 4, 4), np.float16)
        with self.assertRaises(ValueError):
            analysis_rgba8(frame, {'frame_format': 'rgba16f'})
        frame[0, 0, 0] = np.nan
        with self.assertRaises(ValueError):
            analysis_rgba8(frame, {'frame_format': 'rgba16f', 'color_profile': 'scrgb'})

    def test_hdr_validation_accepts_explicit_profile_but_never_tiled(self):
        for profile in ('hdr10_pq', 'hdr10_hlg', 'scrgb'):
            settings = {'guidance_mode': 1, 'frame_format': 'rgba16f', 'color_profile': profile}
            with mock.patch.object(guidance_client.mod_paths, 'guidance_files', return_value={}):
                guidance_client.validate(settings)
                with self.assertRaises(ValueError):
                    guidance_client.validate({**settings, 'host_tiled_mode': True})

    def test_proxy_reaches_worker_native_receives_original_for_sync_and_async(self):
        for asynchronous in (False, True):
            live = dlss_engine.Live.__new__(dlss_engine.Live)
            live.settings = {'guidance_mode': 1, 'frame_format': 'rgba16f', 'color_profile': 'hdr10_pq'}
            live._w, live._h = 8, 6
            live._guidance = None
            live._reset_next = True
            live._lib = mock.Mock()
            live._lib.dlssnr_process.return_value = 1
            live._lib.dlssnr_enqueue.return_value = 1
            live._lib.dlssnr_pending.return_value = 0
            live.supports_async, live.max_in_flight = True, 2
            live._allocate_buffers()
            frame = np.full((6, 8, 4), 0.7, np.float16)
            before = frame.copy()
            session = mock.Mock()
            session.process.return_value = (live._mv, live._dp, True)
            with mock.patch.object(guidance_client, 'GuidanceSession', return_value=session):
                live.enqueue(frame, reset=True) if asynchronous else live.process(frame, reset=True)
            proxy = session.process.call_args.args[0]
            self.assertEqual(proxy.dtype, np.uint8)
            native = live._lib.dlssnr_enqueue if asynchronous else live._lib.dlssnr_process
            self.assertEqual(native.call_args.args[0].value, frame.ctypes.data)
            np.testing.assert_array_equal(frame, before)

    def test_color_change_invalidates_guidance_contract(self):
        base = {'guidance_mode': 1, 'frame_format': 'rgba16f', 'color_profile': 'hdr10_pq'}
        self.assertNotEqual(guidance_client.contract(base), guidance_client.contract({**base, 'color_primaries': 'bt709'}))

    def test_hdr_preview_decodes_real_adjacent_pair_and_closes_reader(self):
        from dlss5tool import preview_comparison
        reader = mock.Mock()
        reader.read.side_effect = [np.full((24, 32, 3), 10, np.uint8), np.full((24, 32, 3), 20, np.uint8)]
        info = {'is_hdr': True, 'profile': 'hdr10_pq'}
        with mock.patch.object(preview_comparison, 'HDRAnalysisReader', return_value=reader) as factory:
            current, previous = guidance_input_pair('hdr.mp4', 7, None, {}, info)
        factory.assert_called_once_with('hdr.mp4', info, start_frame=6)
        self.assertEqual(int(previous[0, 0, 0]), 10)
        self.assertEqual(int(current[0, 0, 0]), 20)
        reader.close.assert_called_once()

    def test_hdr_decoder_uses_exact_frame_trim_not_fps_seek(self):
        from dlss5tool import video_export
        with mock.patch.object(video_export.subprocess, 'Popen') as process, \
                mock.patch.object(video_export.threading, 'Thread'):
            reader = video_export.FFmpegHDRVideoReader('test.mp4', 32, 24,
                {'color_transfer': 'smpte2084'}, ffmpeg='ffmpeg', start_frame=7)
            command = process.call_args.args[0]
            self.assertTrue(command[command.index('-vf') + 1].startswith('trim=start_frame=7,'))
            self.assertNotIn('-ss', command)

    def test_hdr_export_uses_proxy_and_releases_reader(self):
        import tempfile
        from pathlib import Path
        from dlss5tool import guidance_export
        from tests.test_guidance_export import FakeSession
        capture, reader = mock.Mock(), mock.Mock()
        capture.isOpened.return_value = True
        capture.get.side_effect = [3, 24.0]
        reader.read.side_effect = [np.zeros((24, 32, 3), np.uint8), np.full((24, 32, 3), 80, np.uint8)]
        info = {'is_hdr': True, 'profile': 'hdr10_pq', 'width': 32, 'height': 24}
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(guidance_export.cv2, 'VideoCapture', return_value=capture), \
                mock.patch.object(guidance_export, 'HDRAnalysisReader', return_value=reader) as factory:
            destination = Path(temporary) / 'flow.png'
            count = guidance_export.export_guidance(Path(temporary) / 'hdr.mp4', destination,
                {'guidance_mode': 1}, 'flow', frame=2, color_info=info, session_factory=FakeSession)
            self.assertEqual(count, 1)
            self.assertTrue(destination.is_file())
            self.assertEqual(factory.call_args.kwargs['start_frame'], 1)
        reader.close.assert_called_once()
        capture.release.assert_called_once()


class LimitPolicyTests(unittest.TestCase):
    def test_still_flow_skip_preserves_preferences_and_needs_no_component(self):
        settings = {'guidance_mode': 1, 'guidance_flow_edge': 512}
        with mock.patch('dlss5tool.gui.cached_gpu_memory', return_value=None):
            for size in ((512, 512), (11637, 5120)):
                result = _large_image_host_settings(*size, settings)
                self.assertEqual(result['guidance_mode'], 0)
                self.assertTrue(result['_still_flow_skipped'])
                with mock.patch.object(guidance_client.mod_paths, 'guidance_files', side_effect=AssertionError):
                    guidance_client.validate(result)
        self.assertEqual(settings['guidance_mode'], 1)

    def test_low_vram_and_long_thin_stills_tile_early(self):
        with mock.patch('dlss5tool.gui.cached_gpu_memory', return_value={'total_bytes': 6 * 1024 ** 3}):
            result = _large_image_host_settings(5000, 3000, {})
            self.assertTrue(result['host_tiled_mode'])
            self.assertEqual((result['host_tile_width'], result['host_tile_height']), (3000, 1500))
        with mock.patch('dlss5tool.gui.cached_gpu_memory', return_value=None):
            self.assertTrue(_large_image_host_settings(12000, 1000, {})['host_tiled_mode'])

    def test_custom_output_above_8k_persists_but_stays_within_texture_limit(self):
        saved = app_settings.validate({'custom_output_width': 12000, 'custom_output_height': 99999})
        self.assertEqual((saved['custom_output_width'], saved['custom_output_height']), (12000, 16384))
        self.assertEqual(parameters({'guidance_flow_edge': 2048}, strict=True)['guidance_flow_edge'], 2048)
        with self.assertRaises(ValueError):
            parameters({'guidance_flow_edge': 2049}, strict=True)
        with self.assertRaises(super_resolution.SuperResolutionError):
            super_resolution.validate_dimensions(16385, 512, 1)

    def test_large_hdr_queue_is_bounded_by_preference_and_available_memory(self):
        select = super_resolution.select_in_flight
        self.assertEqual(select(3840, 2160, 2, 3, is_hdr=True), 1)
        self.assertEqual(select(3840, 2160, 2, 3, is_hdr=True, gpu_memory={'free_bytes': 1024 ** 3}), 1)
        self.assertEqual(select(3840, 2160, 2, 3, is_hdr=True, gpu_memory={'free_bytes': 32 * 1024 ** 3}), 3)
        self.assertEqual(select(3840, 2160, 2, 1, is_hdr=True, gpu_memory={'free_bytes': 32 * 1024 ** 3}), 1)


if __name__ == '__main__':
    unittest.main()
