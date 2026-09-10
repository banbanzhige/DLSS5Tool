import os
from pathlib import Path
import tempfile
import threading
import unittest
from tests.depth_fixture import depth_test_case

import cv2
import numpy as np

from dlss5tool.guidance_export import export_guidance, GuidanceExportCancelled


class FakeSession:
    def __init__(self, settings, width, height):
        self.settings = settings
        self.calls = []
        self.closed = False

    def process(self, rgba, reset=False):
        self.calls.append((rgba.copy(), reset))
        motion = np.zeros((*rgba.shape[:2], 2), np.float32)
        motion[..., 0] = 0 if reset else 32
        depth = np.full(rgba.shape[:2], 0.5, np.float32)
        return motion, depth, reset

    def close(self):
        self.closed = True


@depth_test_case
class GuidanceExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / 'source.avi'
        video = cv2.VideoWriter(str(self.source), cv2.VideoWriter_fourcc(*'MJPG'), 24, (32, 24))
        self.assertTrue(video.isOpened())
        for index in range(4):
            video.write(np.full((24, 32, 3), 30 * index, np.uint8))
        video.release()
        self.sessions = []
        self.settings = {'guidance_mode': 3, 'guidance_edge': 128, 'guidance_cache_pool': 'do-not-persist'}

    def session(self, *args):
        result = FakeSession(*args)
        self.sessions.append(result)
        return result

    def test_png_is_clean_visualization_at_source_size(self):
        output = self.directory / '深度.png'
        count = export_guidance(self.source, output, self.settings, 'depth', frame=2, session_factory=self.session)
        self.assertEqual(count, 1)
        image = cv2.imdecode(np.fromfile(output, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.shape, (24, 32, 3))
        self.assertTrue(np.all(image == 128))
        self.assertEqual([reset for _, reset in self.sessions[0].calls], [True, False])
        self.assertNotIn('guidance_cache_pool', self.sessions[0].settings)
        self.assertTrue(self.sessions[0].closed)

    def test_flow_frame_uses_preceding_source_frame(self):
        output = self.directory / 'flow.png'
        export_guidance(self.source, output, self.settings, 'flow', frame=2, session_factory=self.session)
        calls = self.sessions[0].calls
        self.assertLess(float(calls[0][0][..., 0].mean()), float(calls[1][0][..., 0].mean()))
        image = cv2.imread(str(output))
        self.assertTrue(np.all(image[..., 2] == 255))
        self.assertTrue(np.all(image[..., :2] == 0))

    def test_custom_analysis_and_display_settings_reach_export(self):
        settings = {**self.settings, 'guidance_flow_edge': 128, 'guidance_depth_edge': 256,
                    'guidance_flow_updates': 12, 'guidance_depth_smoothing': 0.5,
                    'guidance_depth_palette': 'turbo', 'guidance_depth_invert': True}
        output = self.directory / 'custom.png'
        still = np.zeros((512, 512, 3), np.uint8)
        export_guidance(self.source, output, settings, 'depth', frame=0, still=still,
                        session_factory=self.session)
        session = self.sessions[0]
        self.assertEqual(session.calls[0][0].shape, (256, 256, 4))
        self.assertEqual(session.settings['guidance_flow_updates'], 12)
        self.assertEqual(session.settings['guidance_depth_smoothing'], 0.5)
        image = cv2.imread(str(output))
        expected = cv2.applyColorMap(np.full((512, 512), 127, np.uint8), cv2.COLORMAP_TURBO)
        np.testing.assert_array_equal(image, expected)

    def test_first_frame_flow_and_still_are_zero(self):
        output = self.directory / 'first.png'
        export_guidance(self.source, output, self.settings, 'flow', frame=0, session_factory=self.session)
        self.assertFalse(cv2.imread(str(output)).any())
        export_guidance(self.source, output, self.settings, 'flow', frame=0,
                        still=np.zeros((13, 17, 3), np.uint8), session_factory=self.session)
        self.assertEqual(cv2.imread(str(output)).shape, (13, 17, 3))

    def test_cancellation_preserves_existing_file_and_cleans_temporary(self):
        output = self.directory / 'existing.png'
        output.write_bytes(b'original-output')
        cancelled = threading.Event()
        def cancel_after_frame(done, total):
            cancelled.set()
        with self.assertRaises(GuidanceExportCancelled):
            export_guidance(self.source, output, self.settings, 'depth', frame=0,
                            session_factory=self.session, cancel=cancelled, progress=cancel_after_frame)
        self.assertEqual(output.read_bytes(), b'original-output')
        self.assertEqual(list(self.directory.glob('.guidance-export-*')), [])
        self.assertTrue(self.sessions[0].closed)

    def test_bad_frame_fails_without_overwriting(self):
        output = self.directory / 'existing.png'
        output.write_bytes(b'keep')
        with self.assertRaises(RuntimeError):
            export_guidance(self.source, output, self.settings, 'depth', frame=99, session_factory=self.session)
        self.assertEqual(output.read_bytes(), b'keep')

    def test_source_cannot_be_overwritten(self):
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            export_guidance(self.source, self.source, self.settings, 'depth', frame=0, session_factory=self.session)
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(self.sessions, [])

    def test_whole_video_is_sequential_and_excludes_audio(self):
        writers = []
        class Writer:
            def __init__(self, path, width, height, fps, audio_source=None):
                self.path, self.size, self.fps, self.audio = path, (width, height), fps, audio_source
                self.frames = []
                writers.append(self)
            def write(self, frame):
                self.frames.append(frame.copy())
            def finish(self):
                Path(self.path).write_bytes(b'encoded')
            def abort(self):
                pass
        output = self.directory / 'depth.mp4'
        count = export_guidance(self.source, output, self.settings, 'depth', session_factory=self.session, writer_factory=Writer)
        self.assertEqual(count, 4)
        self.assertEqual(writers[0].size, (32, 24))
        self.assertEqual(writers[0].fps, 24)
        self.assertIsNone(writers[0].audio)
        self.assertEqual(len(writers[0].frames), 4)
        self.assertEqual([reset for _, reset in self.sessions[0].calls], [True, False, False, False])
        self.assertEqual(output.read_bytes(), b'encoded')

    def test_real_ffmpeg_output_has_source_dimensions_rate_and_frame_count(self):
        from dlss5tool.video_export import FFmpegVideoWriter
        output = self.directory / 'encoded.mp4'
        def writer(*args, **kwargs):
            return FFmpegVideoWriter(*args, use_nvenc=False, **kwargs)
        count = export_guidance(self.source, output, self.settings, 'depth',
                                session_factory=self.session, writer_factory=writer)
        self.assertEqual(count, 4)
        capture = cv2.VideoCapture(str(output))
        try:
            self.assertTrue(capture.isOpened())
            self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_COUNT)), 4)
            self.assertAlmostEqual(capture.get(cv2.CAP_PROP_FPS), 24, places=2)
            ok, image = capture.read()
            self.assertTrue(ok)
            self.assertEqual(image.shape, (24, 32, 3))
        finally:
            capture.release()


if __name__ == '__main__':
    unittest.main()
