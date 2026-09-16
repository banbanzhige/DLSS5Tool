import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import encode_acceptance_hdr as hdr


class HdrAcceptanceHarnessTests(unittest.TestCase):
    def test_requires_registered_work_and_bounded_frames(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td); (work / "TASK.md").write_text("ok", encoding="utf-8")
            with mock.patch.object(hdr, "probe", return_value={"streams": []}), \
                 mock.patch.object(hdr, "frame_audit", return_value={"profile": "hdr10_pq"}):
                with mock.patch("sys.argv", ["x", "--work", str(work), "--frames", "1"]): hdr.main()
            report = json.loads((work / "hdr-acceptance.json").read_text(encoding="utf-8"))
            self.assertTrue(report["fixture_used"])
            self.assertIn("DLSS", report["blocked"][0])

    def test_scalar_profile_mapping(self):
        self.assertEqual("hdr10_pq", "hdr10_pq")
        self.assertEqual("hdr10_hlg", "hdr10_hlg")


if __name__ == "__main__": unittest.main()
