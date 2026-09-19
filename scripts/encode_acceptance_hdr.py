"""Bounded HDR acceptance harness (CPU preparation; no DLSS implementation).

This deliberately uses the registered tiny HDR fixtures when no inventory is
provided.  It never calls the shared GPU probes and never labels a fixture as
an original camera/source asset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tmp" / "hdr-e2e"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_candidates():
    """Load the research candidate module without requiring a scripts package."""
    import importlib.util
    path = ROOT / "scripts" / "gpu_pipeline_candidates.py"
    spec = importlib.util.spec_from_file_location("_gpu_pipeline_candidates", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def child(cmd: list[str], timeout: int = 30, stdin=None) -> dict:
    started = time.perf_counter()
    p = subprocess.run(cmd, stdin=stdin, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout,
                       env=os.environ.copy(),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"command": cmd, "returncode": p.returncode,
            "seconds": round(time.perf_counter() - started, 6),
            "stdout": p.stdout, "stderr": p.stderr}


def probe(ffprobe: str, source: Path) -> dict:
    r = child([ffprobe, "-v", "error", "-of", "json", "-show_streams",
               "-show_format", str(source)])
    if r["returncode"]:
        raise RuntimeError(r["stderr"].decode(errors="replace"))
    return json.loads(r["stdout"])


def frame_audit(ffmpeg: str, source: Path, profile: str, frames: int) -> dict:
    # rgba64le avoids an 8-bit path; only a small bounded prefix is streamed.
    r = child([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-i", str(source), "-frames:v", str(frames), "-an",
               "-vf", "format=rgba64le", "-f", "rawvideo", "pipe:1"])
    streams = probe(os.environ.get("FFPROBE", "ffprobe"), source).get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    w, h = int(video.get("width", 0)), int(video.get("height", 0))
    expected = w * h * 8 * frames
    got = len(r["stdout"])
    # Compare the isolated Torch candidate with the production NumPy functions
    # on decoded PQ/HLG fixtures. This is CPU arithmetic, not a DLSS/NVENC run.
    numeric = {"available": False}
    if r["returncode"] == 0 and w and h and got == expected:
        try:
            import numpy as np
            import torch
            from dlss5tool.guidance_color import analysis_rgba8
            from dlss5tool.video_export import compose_hdr_frame
            candidates = load_candidates()
            arr = np.frombuffer(r["stdout"], dtype="<u2").reshape(frames, h, w, 4)
            rgba16 = (arr.astype(np.float32) / 65535.0).astype(np.float16)
            settings = {"frame_format": "rgba16f", "color_profile": profile,
                        "color_primaries": "bt2020"}
            production = np.stack([analysis_rgba8(frame, settings) for frame in rgba16])
            x = torch.from_numpy(arr.astype(np.float32) / 65535.0).half()
            cand = candidates.analysis_hdr(x, profile).numpy()
            delta = np.abs(cand.astype(np.int16) - production.astype(np.int16))
            mixed_prod = compose_hdr_frame(rgba16[0], rgba16[min(1, frames - 1)], 0, 0.7, profile)
            mixed_cand = candidates.compose_hdr(x[0], x[min(1, frames - 1)], 0.7, profile)
            mix_delta = (mixed_cand.float() - torch.from_numpy(np.asarray(mixed_prod, np.float32))).abs()
            numeric = {
                "available": True,
                "reference": "production analysis_rgba8 / compose_hdr_frame",
                "candidate_device": str(x.device),
                "candidate_shape": list(cand.shape),
                "analysis_mismatch_channels": int(np.count_nonzero(delta)),
                "analysis_max_abs_code_error": int(delta.max()),
                "analysis_sha256": hashlib.sha256(cand.tobytes()).hexdigest(),
                "production_analysis_sha256": hashlib.sha256(production.tobytes()).hexdigest(),
                "mix_max_abs": float(mix_delta.max()),
                "promotion_gate": "reject" if int(delta.max()) or float(mix_delta.max()) else "pass",
            }
        except Exception as exc:  # optional CPU deps must not hide decode evidence
            numeric = {"available": False, "error": repr(exc)}
    return {"profile": profile, "width": w, "height": h, "requested_frames": frames,
            "decoded_bytes": got, "expected_bytes": expected,
            "decode_returncode": r["returncode"], "numeric": numeric,
            "stderr": r["stderr"].decode(errors="replace")[-2000:]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--source-dir", type=Path)
    ap.add_argument("--inventory", type=Path)
    ap.add_argument("--frames", type=int, default=2)
    a = ap.parse_args(); a.work = a.work.resolve(); a.work.mkdir(parents=True, exist_ok=True)
    if not (a.work / "TASK.md").is_file() or not 1 <= a.frames <= 4:
        ap.error("registered work directory and 1..4 bounded frames required")
    ffmpeg = os.environ.get("FFMPEG", "ffmpeg"); ffprobe = os.environ.get("FFPROBE", "ffprobe")
    sources = {}
    if a.inventory and a.inventory.is_file():
        inv = json.loads(a.inventory.read_text(encoding="utf-8"))
        for row in inv.get("files", inv if isinstance(inv, list) else []):
            p = Path(row.get("path", "")); profile = row.get("profile")
            if profile in ("hdr10_pq", "hdr10_hlg") and p.is_file(): sources[profile] = p
    if a.source_dir:
        for p in a.source_dir.iterdir():
            if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".mkv"):
                try:
                    meta = probe(ffprobe, p); vs = next(s for s in meta.get("streams", []) if s.get("codec_type") == "video")
                    tr = vs.get("color_transfer", ""); profile = "hdr10_pq" if tr == "smpte2084" else "hdr10_hlg" if tr == "arib-std-b67" else None
                    if profile and profile not in sources: sources[profile] = p
                except Exception: pass
    fixture_used = not sources
    if fixture_used: sources = {"hdr10_pq": FIXTURES / "source-hdr10.mp4", "hdr10_hlg": FIXTURES / "source-hlg.mp4"}
    report = {"scope": "HDR acceptance CPU preparation; DLSS GPU path not executed",
              "fixture_used": fixture_used, "sources": {}, "blocked": [
                  "真实生产 decode→DLSS→混合→Main10 GPU/NVENC 未执行，等待 root GPU slot 与共享编译",
                  "候选算子未达到逐位数值门槛时不得 promotion；现有 8-bit ABGR H264 probe 不构成 HDR 通过",
                  "音频时间线/metadata 仅在源含音频时可检查，不能由无音频 fixture 证明"]}
    for profile, src in sources.items():
        if not src.is_file(): report["sources"][profile] = {"missing": str(src)}; continue
        meta = probe(ffprobe, src); vs = next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), {})
        report["sources"][profile] = {"path": str(src), "fixture": fixture_used,
            "metadata": {k: vs.get(k) for k in ("codec_name", "pix_fmt", "color_space", "color_primaries", "color_transfer", "color_range")},
            "audio_streams": sum(s.get("codec_type") == "audio" for s in meta.get("streams", [])),
            "frame_audit": frame_audit(ffmpeg, src, profile, a.frames)}
    dest = a.work / "hdr-acceptance.json"; dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(dest), "fixture_used": fixture_used, "profiles": list(sources)}, ensure_ascii=False), flush=True)


if __name__ == "__main__": main()
