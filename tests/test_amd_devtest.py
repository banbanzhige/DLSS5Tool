import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

import numpy as np

import amd_devtest as amd


class AmdDeveloperTests(unittest.TestCase):
    def test_sequence_is_deterministic_distinct_and_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"a.raw"
            first = amd.make_sequence(path, 128, 72, 12)
            second = amd.make_sequence(path, 128, 72, 12)
            self.assertEqual(first, second)
            self.assertEqual(path.stat().st_size, 128*72*8*12)
            self.assertGreater(len(set(first)), 8)
            frames = np.fromfile(path, dtype="<f2").reshape(12, 72, 128, 4)
            for index, frame in enumerate(frames):
                self.assertTrue(amd.marker_ok(frame, index))
                self.assertFalse(amd.marker_ok(frame, index+1))

    def test_passthrough_is_never_nr_success(self):
        comparison = [{"valid": True, "marker_ok": True, "body_mae": 0, "rgb_min": 0, "rgb_max": 1} for _ in range(10)]
        self.assertEqual(amd.verdict(comparison, "network job 1 done in 31 ms", True), "INCONCLUSIVE")

    def test_output_difference_alone_is_not_nr_success(self):
        comparison = [{"valid": True, "marker_ok": True, "body_mae": .1, "rgb_min": 0, "rgb_max": 1} for _ in range(10)]
        self.assertEqual(amd.verdict(comparison, "FSR context ready", True), "INCONCLUSIVE")
        self.assertEqual(amd.verdict(comparison, "network job 5 done in 31 ms", True), "ENHANCEMENT_OBSERVED")
        self.assertEqual(amd.verdict(comparison, "network job 5 done in 31 ms\nGPU wait timeouts", True), "INCONCLUSIVE")
        comparison[-1]["marker_ok"] = False
        self.assertEqual(amd.verdict(comparison, "network job 5 done in 31 ms", True), "INCONCLUSIVE")

    def test_invalid_float_frame_fails_metric(self):
        a = np.zeros((10, 10, 4), np.float32)
        b = a.copy(); b[0, 0, 0] = np.nan
        self.assertFalse(amd.metric(a, b)["valid"])

    def test_ini_restores_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"dlssnr_on_amd.ini"
            old = b"[DlssNrOnAmd]\r\nEnabled=0\r\nUnknown=99\r\n"
            path.write_bytes(old)
            with self.assertRaises(RuntimeError):
                with amd.RuntimeSettings(tmp, True):
                    self.assertIn(b"Inline=1", path.read_bytes())
                    self.assertTrue(path.with_name("dlssnr_on_amd.ini.before-devtest").exists())
                    raise RuntimeError("simulated crash")
            self.assertEqual(path.read_bytes(), old)
            self.assertFalse(path.with_name("dlssnr_on_amd.ini.before-devtest").exists())

    def test_pending_ini_backup_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp)/"dlssnr_on_amd.ini.before-devtest"
            backup.write_bytes(b"keep")
            with self.assertRaises(RuntimeError):
                with amd.RuntimeSettings(tmp, True):
                    pass
            self.assertEqual(backup.read_bytes(), b"keep")
            self.assertTrue(amd.restore_settings(tmp))
            self.assertEqual((Path(tmp)/"dlssnr_on_amd.ini").read_bytes(), b"keep")

    def test_feedback_never_contains_binaries_input_or_unapproved_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"report.json").write_text(json.dumps({"path": "C:\\Users\\Alice\\clip.png"}), encoding="utf-8")
            for name in ("version.dll", "test.exe", "weights.bin", "input.rgba16f", "private.txt"):
                (root/name).write_bytes(b"SECRET")
            previews = root/"previews"; previews.mkdir(); (previews/"a.png").write_bytes(b"IMAGE")
            archive = amd.make_feedback(root, False)
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(z.namelist(), ["report.json"])
                value = json.loads(z.read("report.json"))
                self.assertNotIn("Alice", value["path"])
            archive = amd.make_feedback(root, True)
            with zipfile.ZipFile(archive) as z:
                self.assertIn("previews/a.png", z.namelist())
                self.assertNotIn("input.rgba16f", z.namelist())

    def test_malformed_partial_events_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"native.jsonl"
            path.write_text('{"stage":"processing"}\npartial\n[]\n{"frame_done":3}\n', encoding="utf-8")
            self.assertEqual(amd.read_events(path), [{"stage":"processing"}, {"frame_done":3}])

    def test_no_amd_never_starts_nr_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = {"adapter": 0, "name": "RTX", "vendor": 0x10de, "vram_mib": 12000, "software": False}
            inventory = {"exit_code": 0, "timeout": False, "wall_seconds": .1, "events": [event]}
            with mock.patch.object(amd, "run_child", return_value=inventory) as child:
                report, feedback = amd.Runner(tmp, lambda _: None).run()
            self.assertEqual(child.call_count, 1)
            self.assertEqual(report["status"], "NO_AMD_GPU")
            self.assertFalse(report["production_ready"])
            self.assertTrue(feedback.is_file())

    def test_installer_presence_is_not_backend_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)/"amd_backend"; runtime.mkdir()
            (runtime/"dlssnr_on_amd_setup.exe").write_bytes(b"MZ")
            event = {"adapter": 0, "name": "RX", "vendor": 0x1002, "vram_mib": 16000, "software": False}
            inventory = {"exit_code": 0, "timeout": False, "wall_seconds": .1, "events": [event]}
            with mock.patch.object(amd, "run_child", return_value=inventory) as child:
                report, feedback = amd.Runner(tmp, lambda _: None).run()
            self.assertEqual(child.call_count, 1)
            self.assertEqual(report["status"], "MISSING_COMPONENTS")
            self.assertTrue(feedback.is_file())

    def test_cancel_creates_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(amd, "run_child", side_effect=amd.Cancelled()):
                report, feedback = amd.Runner(tmp, lambda _: None).run()
            self.assertEqual(report["status"], "CANCELLED")
            self.assertTrue(feedback.is_file())

    def test_failure_creates_feedback_with_partial_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fail(command, directory, *_args):
                Path(directory).mkdir(parents=True)
                (Path(directory)/"native.jsonl").write_text('{"stage":"create_device"}\n', encoding="utf-8")
                raise RuntimeError("failed at C:\\Users\\Alice\\secret")
            with mock.patch.object(amd, "run_child", side_effect=fail):
                report, feedback = amd.Runner(tmp, lambda _: None).run()
            self.assertEqual(report["status"], "FAILED")
            self.assertNotIn("Alice", report["error"])
            with zipfile.ZipFile(feedback) as z:
                self.assertIn("inventory/native.jsonl", z.namelist())

    def test_unchanged_old_mod_log_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp)/"dlssnr_on_amd.log"
            log.write_text("network job 100 done in 31 ms", encoding="utf-8")
            old = amd.mod_log_snapshot(tmp)
            self.assertEqual(amd.new_mod_logs(tmp, old), {})
            log.write_text("FAULT: new failure", encoding="utf-8")
            self.assertIn("FAULT", amd.new_mod_logs(tmp, old)[log.name])

    def test_appended_log_does_not_reuse_old_success_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp)/"dlssnr_on_amd.log"
            log.write_text("network job 100 done in 31 ms\n", encoding="utf-8")
            old = amd.mod_log_snapshot(tmp)
            with log.open("a", encoding="utf-8") as out:
                out.write("new startup without any job\n")
            new = amd.new_mod_logs(tmp, old)[log.name]
            self.assertNotIn("network job", new)
            self.assertIn("new startup", new)

    def test_baseline_rejects_stale_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = np.ones((72, 128, 4), dtype="<f2")
            frame[..., 0] = .8; frame[..., 1] = .1
            for i in range(2):
                frame.tofile(Path(tmp)/f"frame-{i:03d}.rgba16f")
            with self.assertRaises(RuntimeError):
                amd.validate_baseline(tmp, 128, 72, 2)

    def test_feedback_prefers_last_nr_test_not_later_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, mode in (("001", "nr"), ("002", "inventory")):
                folder = root/"results"/name; folder.mkdir(parents=True)
                amd.write_json(folder/"report.json", {"requested_test": mode, "images_in_feedback": False})
            feedback, folder = amd.export_previous(root)
            self.assertEqual(folder.name, "001")
            self.assertTrue(feedback.is_file())


if __name__ == "__main__":
    unittest.main()
