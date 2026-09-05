import json
import os
import tempfile
import unittest

import export_queue


class ExportJobTests(unittest.TestCase):
    def test_create_copies_mutable_settings(self):
        settings = {"style": 2, "nested": {"value": 1}}
        export_settings = {"mode": "parallel"}
        job = export_queue.ExportJob.create(
            "source.mp4", "output.mp4", settings, export_settings,
        )
        settings["nested"]["value"] = 9
        export_settings["mode"] = "single"
        self.assertEqual(job.settings["nested"]["value"], 1)
        self.assertEqual(job.export_settings["mode"], "parallel")
        self.assertEqual(job.media_kind, "video")

    def test_media_kind_is_inferred_for_images(self):
        job = export_queue.ExportJob.create(
            "source.webp", "output.webp", {"style": 1}, {},
        )
        self.assertEqual(job.media_kind, "image")

    def test_retry_resets_runtime_state_but_keeps_snapshot(self):
        job = export_queue.ExportJob.create(
            "source.mp4", "output.mp4", {"style": 1}, {"mode": "single"},
            metadata={"frames": 24},
        )
        job.state = "failed"
        job.progress_done = 12
        job.error = "failed"
        job.reset_for_retry()
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.progress_done, 0)
        self.assertEqual(job.progress_total, 24)
        self.assertEqual(job.error, "")
        self.assertEqual(job.settings["style"], 1)


class ExportQueuePersistenceTests(unittest.TestCase):
    def test_roundtrip_and_running_job_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "queue.json")
            job = export_queue.ExportJob.create(
                os.path.join(temp_dir, "source.mp4"),
                os.path.join(temp_dir, "output.mp4"),
                {"style": 2}, {"mode": "single"},
                metadata={"frames": 100},
                color_info={"is_hdr": True, "label": "HDR10 / PQ"},
            )
            job.state = "running"
            job.progress_done = 25
            export_queue.save([job], path)
            loaded = export_queue.load(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].state, "interrupted")
        self.assertIn("可重试", loaded[0].error)
        self.assertEqual(loaded[0].progress_done, 25)
        self.assertTrue(loaded[0].color_info["is_hdr"])

    def test_invalid_rows_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "queue.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"jobs": [None, {}, {"source_path": "x.mp4"}]}, handle)
            loaded = export_queue.load(path)
        self.assertEqual(loaded, [])

    def test_legacy_image_job_infers_kind_when_field_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "queue.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "jobs": [{
                        "source_path": os.path.join(temp_dir, "source.png"),
                        "output_path": os.path.join(temp_dir, "output.png"),
                        "settings": {},
                        "export_settings": {},
                    }],
                }, handle)
            loaded = export_queue.load(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].media_kind, "image")


if __name__ == "__main__":
    unittest.main()
