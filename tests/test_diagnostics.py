import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import numpy as np

from dlss5tool import diagnostics


class DiagnosticHelpersTests(unittest.TestCase):
    def test_suggested_name_is_timestamped(self):
        self.assertEqual(
            diagnostics.suggested_report_name(datetime(2026, 9, 5, 8, 7, 6)),
            "DLSS5Tool-diagnostic-20260905-080706.log",
        )

    def test_file_description_hashes_unknown_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "nvngx_dlssnr.dll")
            with open(path, "wb") as handle:
                handle.write(b"diagnostic fixture")
            with mock.patch.object(diagnostics.dlss_engine, "DLSSNR_DLL", path):
                result = diagnostics.describe_file(path)
            self.assertTrue(result["exists"])
            self.assertEqual(result["bytes"], len(b"diagnostic fixture"))
            self.assertEqual(result["runtime_profile"], "未知/自定义运行时")
            self.assertEqual(len(result["sha256"]), 64)

    def test_dlssg_runtime_profile_uses_pinned_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "nvngx_dlssg.dll")
            with open(path, "wb") as handle:
                handle.write(b"dlssg fixture")
            result = diagnostics.describe_file(path)
        self.assertEqual(result["runtime_profile"], "未知/自定义插帧运行库")
        known = next(iter(diagnostics._KNOWN_DLSSG))
        with mock.patch.object(diagnostics, "_sha256", return_value=known):
            with tempfile.TemporaryDirectory() as temp_dir:
                path = os.path.join(temp_dir, "nvngx_dlssg.dll")
                with open(path, "wb") as handle:
                    handle.write(b"dlssg fixture")
                labeled = diagnostics.describe_file(path)
        self.assertIn("DLSSG", labeled["runtime_profile"])

    def test_ui_log_keeps_only_support_lines_and_redacts_home(self):
        home = os.path.expanduser("~")
        lines = diagnostics.filter_ui_log(
            "普通提示\n[DLSS] failed at " + os.path.join(home, "clip.mp4") + "\n[性能] 60 fps"
        )
        self.assertEqual(len(lines), 2)
        self.assertIn("%MEDIA_FILE%", lines[0])
        self.assertIn("clip.mp4", lines[0])
        self.assertNotIn(home, lines[0])
        self.assertNotIn("普通提示", "\n".join(lines))

    def test_probe_hints_classify_feature_gate_and_success(self):
        failed = diagnostics._probe_hints({
            "ok": False,
            "native_log": "CreateFeature(18) -> 0xBAD00001",
        })
        self.assertTrue(any("FeatureNotSupported" in line for line in failed))
        self.assertTrue(any(diagnostics.updater.RELEASES_URL in line for line in failed))
        init_failed = diagnostics._probe_hints({
            "ok": False,
            "native_log": "Init_with_ProjectID -> 0xBAD00001",
        })
        self.assertTrue(any("_internal\\nvngx_dlssnr.dll" in line for line in init_failed))
        self.assertTrue(any("高性能（NVIDIA GPU）" in line for line in init_failed))
        passed = diagnostics._probe_hints({"ok": True, "native_log": "EvaluateFeature -> 1"})
        self.assertEqual(passed, ["宿主初始化和单帧处理通过。"])
        skipped = diagnostics._probe_hints({"skipped": True})
        self.assertIn("已跳过", skipped[0])


class DiagnosticReportTests(unittest.TestCase):
    def test_report_is_written_even_when_one_probe_fails(self):
        probes = {
            "v2": {
                "backend_requested": "v2", "ok": True,
                "adapter_info": {"name": "RTX Test", "device_id": 1234},
                "native_log": "Feature 18 ready", "timed_out": False,
            },
            "legacy": {
                "backend_requested": "legacy", "ok": False,
                "native_log": "CreateFeature(18) -> 0xBAD00001",
                "error": "gate", "timed_out": False,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "report.log")
            with (
                mock.patch.object(diagnostics, "describe_file", return_value={"exists": True}),
                mock.patch.object(
                    diagnostics, "_command_output",
                    return_value={"returncode": 0, "stdout": "RTX Test, 999.0"},
                ),
                mock.patch.object(
                    diagnostics, "_probe_backend",
                    side_effect=lambda _temp, _settings, backend: probes[backend],
                ),
                mock.patch.object(diagnostics, "collect_recent_exports", return_value=[]),
                mock.patch.object(diagnostics, "_probe_vsr", return_value={"ok": True, "hints": ["VSR ok"]}),
                mock.patch.object(diagnostics, "_probe_dlssg", return_value={"ok": True, "hints": ["FG ok"]}),
                mock.patch.object(diagnostics, "_describe_adapters", return_value=["adapter0"]),
                mock.patch.object(diagnostics, "_describe_edition", return_value=["形态: 测试"]),
                mock.patch.object(diagnostics, "_describe_toolchain", return_value=["ffmpeg: test"]),
                mock.patch.object(diagnostics, "_precheck_media", return_value=["素材预检"]),
                mock.patch.object(diagnostics, "_describe_gpu_flow", return_value=["直连: 否"]),
                mock.patch.object(diagnostics, "_describe_preview", return_value=["视图: compare"]),
                mock.patch('dlss5tool.diagnostics.os.path.isfile', return_value=True),
            ):
                result = diagnostics.write_diagnostic_report(
                    output,
                    {"settings": {"host_backend": "auto"}, "ui_log": "[DLSS] gate"},
                )
            self.assertEqual(result["passed"], 1)
            self.assertEqual(result["total"], 2)
            with open(output, encoding="utf-8") as handle:
                report = handle.read()
            self.assertIn("[宿主探针: v2]", report)
            self.assertIn("[宿主探针: legacy]", report)
            self.assertIn(f"应用版本: {diagnostics.APP_VERSION}", report)
            self.assertIn("FeatureNotSupported", report)
            self.assertIn("RTX Test, 999.0", report)
            self.assertIn("adapter_info: {'name': 'RTX Test'", report)
            self.assertIn("[最近导出]", report)
            self.assertIn("[超分探针]", report)
            self.assertIn("[插帧探针]", report)
            self.assertIn("[有效配置]", report)
            self.assertIn("[当前素材预检]", report)
            self.assertIn("[发行与推理]", report)
            self.assertIn("[工具链]", report)
            self.assertIn("[适配器]", report)
            self.assertIn("[GPU 光流]", report)
            self.assertIn("[当前预览]", report)
            self.assertIn("报告格式: 3", report)
            self.assertIn("dlssg_video_worker.exe:", report)
            self.assertIn("nvngx_dlssg.dll:", report)
            self.assertFalse(os.path.exists(output + ".tmp"))

    def test_worker_publishes_machine_readable_result(self):
        class FakeLive:
            backend = "v2"
            adapter_info = {"name": "RTX Worker"}

            def __init__(self, width, height, settings):
                self.width = width
                self.height = height
                self.settings = settings

            def process(self, frame, reset=False):
                return np.clip(frame.astype(np.uint16) + 1, 0, 255).astype(np.uint8)

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = os.path.join(temp_dir, "result.json")
            native_log = os.path.join(temp_dir, "native.log")
            settings_path = os.path.join(temp_dir, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump({"host_backend": "auto"}, handle)
            with mock.patch.object(diagnostics.dlss_engine, "Live", FakeLive):
                code = diagnostics.diagnostic_worker_main([
                    result_path, native_log, settings_path, "v2",
                ])
            self.assertEqual(code, 0)
            with open(result_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["backend_actual"], "v2")
            self.assertEqual(payload["adapter_info"]["name"], "RTX Worker")
            self.assertEqual(payload["output_shape"], [360, 640, 4])


class ExportHistoryTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self._history = os.path.join(self._temp.name, "history.json")
        self._env = mock.patch.dict(os.environ, {"DLSS5TOOL_EXPORT_HISTORY_PATH": self._history})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._temp.cleanup()

    def test_planned_size_follows_super_resolution(self):
        self.assertEqual(
            diagnostics.planned_output_size(854, 480, {"super_resolution_scale": 4}),
            (3416, 1920),
        )
        self.assertEqual(
            diagnostics.planned_output_size(854, 480, {"super_resolution_scale": 1}),
            (854, 480),
        )

    def test_history_roundtrip_and_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "history.json")
            for index in range(7):
                diagnostics.append_export_history(
                    diagnostics.make_export_record(
                        source_path=f"clip{index}.mp4",
                        output_path=os.path.join(temp_dir, f"out{index}.mp4"),
                        export_settings={"super_resolution_scale": 2},
                        result={"success": True, "frames": 10},
                        source_width=320, source_height=180, planned=(640, 360),
                        at=f"2026-09-16T12:00:0{index}+08:00",
                    ),
                    path=path, limit=5,
                )
            history = diagnostics.load_export_history(path)
        self.assertEqual(len(history), 5)
        self.assertEqual(history[0]["source"]["name"], "clip2.mp4")
        self.assertEqual(history[-1]["source"]["name"], "clip6.mp4")

    def test_report_redacts_output_path_and_flags_size_mismatch(self):
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_video = os.path.join(temp_dir, "clip_dlss.mp4")
            with open(output_video, "wb") as handle:
                handle.write(b"not a real video")
            record = diagnostics.make_export_record(
                source_path=os.path.join(home, "videos", "clip.mp4"),
                output_path=output_video,
                export_settings={"super_resolution_scale": 4},
                result={"success": True, "frames": 361, "error": os.path.join(home, "failed.mp4")},
                source_width=854, source_height=480, source_frames=361,
                planned=(3416, 1920),
            )
            with mock.patch.object(
                diagnostics, "inspect_output_file",
                return_value={"exists": True, "name": "clip_dlss.mp4", "bytes": 12, "width": 854, "height": 480},
            ):
                rows = diagnostics.collect_recent_exports(extra=[record], inspect=True)
            self.assertEqual(len(rows), 1)
            self.assertNotIn("output_path", rows[0])
            self.assertNotIn(home, json.dumps(rows[0]))
            self.assertTrue(any("3416×1920" in hint and "854×480" in hint for hint in rows[0]["hints"]))
            text = "\n".join(diagnostics._format_export_records(rows))
            self.assertIn("%MEDIA_FILE%\\clip_dlss.mp4", text)
            self.assertIn("判断:", text)

    def test_image_output_probe_uses_header_size(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "still_dlss.png")
            Image.new("RGB", (128, 64), "red").save(path)
            info = diagnostics.inspect_output_file(path)
        self.assertTrue(info["exists"])
        self.assertEqual((info["width"], info["height"]), (128, 64))

    def test_queue_job_snapshot_does_not_duplicate_history(self):
        record = diagnostics.make_export_record(
            source_path="a.mp4", output_path="a_dlss.mp4",
            export_settings={"super_resolution_scale": 2},
            result={"success": True, "frames": 24},
            source_width=100, source_height=50, planned=(200, 100),
            at="2026-09-16T10:00:00+08:00",
        )
        job = {
            "source_path": "a.mp4", "output_path": "a_dlss.mp4",
            "state": "completed", "export_settings": {"super_resolution_scale": 2},
            "metadata": {"width": 100, "height": 50, "frames": 24},
            "finished_at": 1,
        }
        rows = diagnostics.collect_recent_exports(
            extra=[record], queue_jobs=[job], inspect=False,
        )
        self.assertEqual(len(rows), 1)


class DiagnosticSummaryTests(unittest.TestCase):
    def test_effective_config_flags_preview_off_and_sdr_hdr_mode(self):
        lines = diagnostics._effective_config({
            "settings": {
                "host_backend": "auto", "host_submission": "compatibility",
                "host_in_flight": 6, "guidance_mode": 1,
                "guidance_flow_backend": "raft", "intensity": 5.0,
            },
            "export_settings": {
                "super_resolution_scale": 4, "frame_generation_multiplier": 2,
                "hdr_mode": True,
            },
            "preview_super_resolution": False,
            "preview_frame_generation": False,
            "media": {
                "width": 854, "height": 480, "fps": 24,
                "color": {"is_hdr": False, "label": "SDR / sRGB"},
            },
        })
        text = "\n".join(lines)
        self.assertIn("超分 4×", text)
        self.assertIn("预览关", text)
        self.assertIn("插帧 2×", text)
        self.assertIn("有效 HDR 关", text)
        self.assertIn("强度 5.0", text)

    def test_media_precheck_warns_on_long_frame_generation_source(self):
        lines = diagnostics._precheck_media({
            "media": {
                "name": "clip_dlss.mp4", "width": 1920, "height": 1080,
                "frames": 28538, "fps": 60,
                "color": {
                    "label": "SDR / sRGB", "r_frame_rate": "60/1",
                    "avg_frame_rate": "60/1", "is_hdr": False,
                },
            },
            "export_settings": {"frame_generation_multiplier": 2},
        })
        text = "\n".join(lines)
        self.assertIn("28538", text)
        self.assertIn("整片扫描时间戳", text)

    def test_gpu_flow_reports_fallback_reason(self):
        lines = diagnostics._describe_gpu_flow({
            "settings": {
                "guidance_gpu_transport": "auto", "guidance_mode": 1,
                "guidance_device": "auto", "guidance_flow_backend": "raft",
                "host_persistent_buffers": True, "host_tiled_mode": False,
            },
            "guidance_info": {"gpu_flow_fallback_reason": "LUID mismatch"},
            "active_host": {"backend": "v2"},
        })
        text = "\n".join(lines)
        self.assertIn("会尝试直连: 是", text)
        self.assertIn("LUID mismatch", text)


if __name__ == "__main__":
    unittest.main()
