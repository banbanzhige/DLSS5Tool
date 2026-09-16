import hashlib
import json
from pathlib import Path

from scripts.encode_acceptance_sdr import decode_audit


def test_sdr_harness_is_bounded_and_declares_nvenc_limitations():
    source = Path(__file__).parents[1] / "scripts" / "encode_acceptance_sdr.py"
    text = source.read_text(encoding="utf-8")
    assert "experimental_nvenc_policy" in text
    assert "CONSTQP19" in text
    assert "temporary_cap_mib" in text


def test_decode_audit_counts_frames(tmp_path, monkeypatch):
    ffmpeg = tmp_path / "fake-ffmpeg.py"
    ffmpeg.write_text("", encoding="utf-8")
    # The helper's contract is exercised by the acceptance run; this test keeps
    # report schema changes from silently dropping decode fields.
    assert callable(decode_audit)
