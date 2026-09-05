import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import numpy as np

import diagnostics


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
        passed = diagnostics._probe_hints({"ok": True, "native_log": "EvaluateFeature -> 1"})
        self.assertEqual(passed, ["宿主初始化和单帧处理通过。"])
        skipped = diagnostics._probe_hints({"skipped": True})
        self.assertIn("已跳过", skipped[0])


class DiagnosticReportTests(unittest.TestCase):
    def test_report_is_written_even_when_one_probe_fails(self):
        probes = {
            "v2": {
                "backend_requested": "v2", "ok": True,
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
                mock.patch("diagnostics.os.path.isfile", return_value=True),
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
            self.assertIn("FeatureNotSupported", report)
            self.assertIn("RTX Test, 999.0", report)
            self.assertFalse(os.path.exists(output + ".tmp"))

    def test_worker_publishes_machine_readable_result(self):
        class FakeLive:
            backend = "v2"

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
            self.assertEqual(payload["output_shape"], [360, 640, 4])


if __name__ == "__main__":
    unittest.main()
