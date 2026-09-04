import os
import tempfile
import unittest

import cv2
import numpy as np

import app_settings
from gui import _estimate_output_size_mb, _fit_output_box, _resolve_output_size
from parallel_export import _encoding_worker_args
from video_export import FFmpegVideoWriter, build_video_encoder_args


class OutputSizeTests(unittest.TestCase):
    def test_named_resolution_preserves_aspect_ratio_and_never_upscales(self):
        self.assertEqual(_resolve_output_size(3840, 2160, "1080p"), (1920, 1080))
        self.assertEqual(_resolve_output_size(2160, 3840, "1080p"), (1080, 1920))
        self.assertEqual(_resolve_output_size(1280, 720, "2160p"), (1280, 720))
        self.assertEqual(_resolve_output_size(1919, 1079, "source"), (1919, 1079))

    def test_custom_resolution_is_a_bounding_box(self):
        self.assertEqual(_fit_output_box(3840, 1600, 1920, 1080), (1920, 800))
        self.assertEqual(_resolve_output_size(3840, 2160, "custom", 1000, 1000), (1000, 562))
        self.assertEqual(_resolve_output_size(640, 480, "custom", 1920, 1080), (640, 480))

    def test_target_bitrate_size_estimate_includes_audio_allowance(self):
        self.assertAlmostEqual(_estimate_output_size_mb(60, 8.0), 61.92, places=2)
        self.assertEqual(_estimate_output_size_mb("bad", 8.0), 0.0)


class EncoderArgumentTests(unittest.TestCase):
    def test_default_quality_preserves_current_nvenc_policy(self):
        args = build_video_encoder_args(False, True)
        self.assertEqual(args[:2], ["-c:v", "h264_nvenc"])
        self.assertIn("p5", args)
        self.assertEqual(args[args.index("-cq") + 1], "19")
        self.assertEqual(args[args.index("-b:v") + 1], "0")

    def test_target_bitrate_removes_constant_quality_and_sets_vbr_limits(self):
        args = build_video_encoder_args(
            False, True, rate_control="bitrate", video_bitrate_mbps=12.0,
        )
        self.assertNotIn("-cq", args)
        self.assertEqual(args[args.index("-b:v") + 1], "12M")
        self.assertEqual(args[args.index("-maxrate") + 1], "18M")
        self.assertEqual(args[args.index("-bufsize") + 1], "24M")

    def test_hdr_software_path_uses_main10_and_speed_mapping(self):
        args = build_video_encoder_args(
            True, False, nvenc_preset="p7", quality_profile="balanced",
        )
        self.assertEqual(args[:2], ["-c:v", "libx265"])
        self.assertEqual(args[args.index("-profile:v") + 1], "main10")
        self.assertEqual(args[args.index("-preset") + 1], "slow")
        self.assertEqual(args[args.index("-crf") + 1], "22")

    def test_parallel_worker_receives_the_same_encoding_policy(self):
        args = _encoding_worker_args(
            "p7", "bitrate", "compact", 42.5, (1920, 1080),
        )
        self.assertEqual(args[args.index("--nvenc-preset") + 1], "p7")
        self.assertEqual(args[args.index("--rate-control") + 1], "bitrate")
        self.assertEqual(args[args.index("--quality-profile") + 1], "compact")
        self.assertEqual(args[args.index("--video-bitrate-mbps") + 1], "42.5")
        self.assertEqual(args[args.index("--output-width") + 1], "1920")
        self.assertEqual(args[args.index("--output-height") + 1], "1080")


class EncoderIntegrationTests(unittest.TestCase):
    def test_software_writer_applies_target_output_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "scaled.mp4")
            writer = FFmpegVideoWriter(
                output, 64, 36, 24, use_nvenc=False,
                rate_control="bitrate", video_bitrate_mbps=2.0,
                output_size=(32, 18),
            )
            try:
                for level in range(6):
                    writer.write(np.full((36, 64, 3), level * 30, np.uint8))
                writer.finish()
            except Exception:
                writer.abort()
                raise
            capture = cv2.VideoCapture(output)
            try:
                self.assertTrue(capture.isOpened())
                self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), 32)
                self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)), 18)
            finally:
                capture.release()

    def test_hdr_software_writer_scales_and_keeps_main10_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "scaled-hdr.mp4")
            writer = FFmpegVideoWriter(
                output, 64, 36, 24, use_nvenc=False,
                hdr_metadata={
                    "color_transfer": "smpte2084",
                    "color_primaries": "bt2020",
                    "color_space": "bt2020nc",
                },
                quality_profile="high", output_size=(32, 18),
            )
            frame = np.full((36, 64, 4), 0.25, np.float16)
            frame[..., 3] = 1.0
            try:
                for _ in range(4):
                    writer.write(frame)
                writer.finish()
            except Exception:
                writer.abort()
                raise
            capture = cv2.VideoCapture(output)
            try:
                self.assertTrue(capture.isOpened())
                self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), 32)
                self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)), 18)
            finally:
                capture.release()


class EncodingSettingsTests(unittest.TestCase):
    def test_new_export_settings_validate_and_roundtrip(self):
        raw_values = {
            "output_resolution": "custom",
            "custom_output_width": 2560,
            "custom_output_height": 1440,
            "rate_control": "bitrate",
            "quality_profile": "compact",
            "video_bitrate_mbps": 42.5,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "settings.json")
            app_settings.save(raw_values, path)
            values = app_settings.load(path)
        self.assertEqual(values["output_resolution"], "custom")
        self.assertEqual(values["custom_output_width"], 2560)
        self.assertEqual(values["custom_output_height"], 1440)
        self.assertEqual(values["rate_control"], "bitrate")
        self.assertEqual(values["quality_profile"], "compact")
        self.assertEqual(values["video_bitrate_mbps"], 42.5)

    def test_invalid_values_fall_back_or_are_clamped(self):
        values = app_settings.validate({
            "output_resolution": "8k",
            "rate_control": "size",
            "quality_profile": "lossless",
            "video_bitrate_mbps": "bad",
            "custom_output_width": 1,
            "custom_output_height": 99999,
        })
        self.assertEqual(values["output_resolution"], "source")
        self.assertEqual(values["rate_control"], "quality")
        self.assertEqual(values["quality_profile"], "high")
        self.assertEqual(values["video_bitrate_mbps"], 20.0)
        self.assertEqual(values["custom_output_width"], 2)
        self.assertEqual(values["custom_output_height"], 8192)


if __name__ == "__main__":
    unittest.main()
