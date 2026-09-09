import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import numpy as np

import parallel_export
import video_export as video


class EncoderSelectionTests(unittest.TestCase):
    def test_auto_codec_uses_both_final_dimensions(self):
        for size, codec in [
            ((3840, 2160), "h264"), ((4096, 4096), "h264"),
            ((4098, 2160), "hevc"), ((2160, 4098), "hevc"),
            ((7680, 4320), "hevc"), ((4320, 7680), "hevc"),
        ]:
            with self.subTest(size=size), mock.patch.object(video, "probe_video_encoder", return_value=None) as probe:
                self.assertEqual(video.select_video_encoder("ffmpeg", *size, 60), (codec, True))
                self.assertEqual(probe.call_args.args[1:4], (*size, 60.0))

    def test_hevc_sdr_preserves_eight_bit_policy_and_hdr_stays_main10(self):
        for gpu, encoder in [(True, "hevc_nvenc"), (False, "libx265")]:
            with self.subTest(gpu=gpu):
                args = video.build_video_encoder_args(False, gpu, codec="hevc")
                self.assertEqual(args[:2], ["-c:v", encoder])
                self.assertNotIn("main10", args)
                hdr = video.build_video_encoder_args(True, gpu, codec="h264")
                self.assertEqual(hdr[:2], ["-c:v", encoder])
                self.assertIn("main10", hdr)

    def test_gpu_failure_falls_back_to_same_codec_with_cpu_parameters(self):
        with mock.patch.object(video, "probe_video_encoder", side_effect=["unsupported size", None]) as probe:
            self.assertEqual(video.select_video_encoder(
                "ffmpeg", 7680, 4320, 24, nvenc_preset="p7",
                rate_control="bitrate", video_bitrate_mbps=42,
            ), ("hevc", False))
        gpu, cpu = [call.args[-1] for call in probe.call_args_list]
        self.assertEqual(gpu[1], "hevc_nvenc")
        self.assertEqual(cpu[1], "libx265")
        self.assertEqual(cpu[cpu.index("-preset") + 1], "slow")
        self.assertEqual(cpu[cpu.index("-b:v") + 1], "42M")
        self.assertNotIn("main10", cpu)

    def test_explicit_gpu_is_strict_for_parallel_segment_consistency(self):
        with mock.patch.object(video, "probe_video_encoder", return_value="session limit") as probe:
            with self.assertRaisesRegex(RuntimeError, "7680×4320.*HEVC"):
                video.select_video_encoder("ffmpeg", 7680, 4320, 24, use_nvenc=True)
        self.assertEqual(probe.call_count, 1)

    def test_cpu_only_and_hdr_selection(self):
        with mock.patch.object(video, "probe_video_encoder", return_value=None) as probe:
            self.assertEqual(video.select_video_encoder(
                "ffmpeg", 1920, 1080, 24, is_hdr=True, use_nvenc=False,
            ), ("hevc", False))
        self.assertTrue(probe.call_args.args[4])
        self.assertIn("main10", probe.call_args.args[-1])

    def test_all_failures_are_reported_before_touching_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "keep.mp4")
            with open(path, "wb") as handle:
                handle.write(b"original")
            with mock.patch.object(video, "probe_video_encoder", side_effect=["GPU failed", "CPU failed"]):
                with self.assertRaisesRegex(RuntimeError, "(?s)hevc_nvenc: GPU failed.*libx265: CPU failed"):
                    video.FFmpegVideoWriter(path, 7680, 4320, 30)
            self.assertEqual(os.listdir(directory), ["keep.mp4"])
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), b"original")

    def test_probe_uses_actual_size_rate_format_and_options(self):
        for hdr, pixel_format in [(False, "yuv420p"), (True, "p010le")]:
            with self.subTest(hdr=hdr), mock.patch.object(video.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, stderr=b"")
                args = video.build_video_encoder_args(hdr, True, codec="hevc")
                self.assertIsNone(video.probe_video_encoder("ffmpeg", 7680, 4320, 60.0, hdr, args))
                command = run.call_args.args[0]
                self.assertIn(f"color=c=black:s=7680x4320:r=60,format={pixel_format}", command)
                self.assertEqual(command[-3:], ["-f", "null", "-"])
                self.assertEqual(command[command.index("-c:v") + 1], "hevc_nvenc")
                self.assertEqual(run.call_args.kwargs["timeout"], 45)

    def test_probe_timeout_and_stderr_are_actionable(self):
        with mock.patch.object(video.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 45)):
            self.assertIn("超时", video.probe_video_encoder("ffmpeg", 7680, 4320, 24.0, False, []))
        with mock.patch.object(video.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stderr=b"unsupported width")):
            self.assertEqual(video.probe_video_encoder("ffmpeg", 7680, 4320, 24.0, False, []), "unsupported width")

    def test_writer_selects_after_scaling_and_padding(self):
        for width, height, output_size, expected in [
            (7680, 4320, (3840, 2160), (3840, 2160)),
            (4097, 2159, None, (4098, 2160)),
            (7680, 4320, (4097, 2159), (4096, 2158)),
        ]:
            with self.subTest(expected=expected), mock.patch.object(video, "select_video_encoder", side_effect=RuntimeError("stop")) as select:
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    video.FFmpegVideoWriter("test.mp4", width, height, 24, output_size=output_size)
                self.assertEqual(select.call_args.args[1:3], expected)

    def test_parallel_parent_pins_resolved_codec_and_device_for_all_workers(self):
        cap = mock.Mock()
        cap.get.side_effect = [4, 7680, 4320, 24]
        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(parallel_export.cv2, "VideoCapture", return_value=cap), \
                mock.patch.object(parallel_export, "find_ffmpeg", return_value="ffmpeg"), \
                mock.patch.object(parallel_export, "select_video_encoder", return_value=("hevc", False)) as select, \
                mock.patch.object(parallel_export.subprocess, "Popen", return_value=process) as launch, \
                mock.patch.object(parallel_export, "_read_json", return_value={"ok": True}), \
                mock.patch.object(parallel_export, "concat_video_segments"), \
                mock.patch.object(parallel_export, "mux_source_audio", return_value="none"):
            result = parallel_export.export_parallel("source.mp4", os.path.join(directory, "out.mp4"), {})
            self.assertEqual(select.call_args.args[1:4], (7680, 4320, 24))
            self.assertEqual(launch.call_count, 2)
            for call in launch.call_args_list:
                command = call.args[0]
                self.assertEqual(command[command.index("--codec") + 1], "hevc")
                self.assertEqual(command[command.index("--nvenc") + 1], "0")
            self.assertIn("libx265", result["encoder"])


class HevcSdrIntegrationTests(unittest.TestCase):
    def test_real_sdr_hevc_containers_remain_sdr_and_keep_dimensions(self):
        for container in ("mp4", "mkv", "mov"):
            with self.subTest(container=container), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "sdr." + container)
                writer = video.FFmpegVideoWriter(
                    path, 64, 64, 24, use_nvenc=False, codec="hevc", nvenc_preset="p1",
                )
                try:
                    writer.write(np.full((64, 64, 3), 96, np.uint8))
                    writer.finish()
                    ffprobe = video.find_ffprobe(writer.ffmpeg)
                    if not ffprobe:
                        self.skipTest("ffprobe is required for codec/tag assertions")
                    result = subprocess.run(
                        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", path],
                        capture_output=True, check=True, timeout=20,
                        creationflags=video._CREATE_NO_WINDOW,
                    )
                    stream = json.loads(result.stdout)["streams"][0]
                    self.assertEqual(stream["codec_name"], "hevc")
                    self.assertEqual((stream["width"], stream["height"]), (64, 64))
                    self.assertEqual(stream["pix_fmt"], "yuv420p")
                    self.assertNotIn(stream.get("color_transfer"), ("smpte2084", "arib-std-b67"))
                    if container != "mkv":
                        self.assertEqual(stream["codec_tag_string"], "hvc1")
                finally:
                    writer.abort()


if __name__ == "__main__":
    unittest.main()
