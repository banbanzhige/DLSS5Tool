#!/usr/bin/env python3
"""Bounded, CPU-only SDR streaming acceptance harness.

This deliberately exercises the production FFmpegVideoWriter contract without
claiming that the experimental CUDA-pointer NVENC probe is production-equivalent.
"""
import argparse, hashlib, json, os, platform, subprocess, time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def encode_once(source, output, frames, width, height, cancel_after=None):
    from dlss5tool.video_export import FFmpegVideoWriter
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError("cannot open source")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    writer = FFmpegVideoWriter(str(output), width, height, fps,
                               audio_source=None, use_nvenc=False,
                               rate_control="quality", quality_profile="high")
    pixels = hashlib.sha256(); count = 0
    started = time.perf_counter()
    try:
        while count < frames:
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            frame = np.ascontiguousarray(frame, dtype=np.uint8)
            pixels.update(frame.tobytes())
            writer.write(frame)
            count += 1
            if cancel_after is not None and count >= cancel_after:
                writer.abort()
                return {"cancelled": True, "frames_written": count,
                        "source_pixels_sha256": pixels.hexdigest(),
                        "elapsed_s": time.perf_counter() - started,
                        "output_exists": output.exists()}
        writer.finish()
    finally:
        cap.release()
    return {"cancelled": False, "frames_written": count,
            "source_pixels_sha256": pixels.hexdigest(),
            "elapsed_s": time.perf_counter() - started,
            "output_exists": output.exists(), "output_bytes": output.stat().st_size}


def decode_audit(ffmpeg, path):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-map", "0:v:0", "-f", "framemd5", "-"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rows = [x for x in result.stdout.decode(errors="replace").splitlines()
            if x and not x.startswith("#")]
    return {"returncode": result.returncode, "frames": len(rows),
            "first_frame_md5": rows[0].split(",")[-1].strip() if rows else None,
            "last_frame_md5": rows[-1].split(",")[-1].strip() if rows else None,
            "stderr": result.stderr.decode(errors="replace")[-1000:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--frames", type=int, default=180)
    a = ap.parse_args(); a.work = a.work.resolve(); a.source = a.source.resolve()
    if not (a.work / "TASK.md").is_file(): ap.error("registered --work/TASK.md required")
    if not (1 <= a.frames <= 360): ap.error("frames must be 1..360")
    os.environ["TEMP"] = os.environ["TMP"] = str(a.work)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    from dlss5tool.video_export import build_video_encoder_args, find_ffmpeg
    ffmpeg = find_ffmpeg()
    width, height = 360, 640
    out1, out2 = a.work / "sdr-run-1.mp4", a.work / "sdr-run-2.mp4"
    cancelled = a.work / "sdr-cancelled.mp4"
    report = {
        "status": "started", "scope": "CPU production SDR streaming only",
        "source": str(a.source), "source_sha256": sha256_file(a.source),
        "dimensions": [width, height], "requested_frames": a.frames,
        "python": platform.python_version(), "platform": platform.platform(),
        "ffmpeg": ffmpeg,
        "production_encoder_args": build_video_encoder_args(False, False, "p5", "quality", "high", 20.0),
        "experimental_nvenc_policy": "H264 P5 low-latency CONSTQP19, ABGR CUDA pointer; unsupported as production-equivalent",
        "constraints": {"gpu_used": False, "input_copied": False, "temporary_cap_mib": 64},
    }
    try:
        report["cancel"] = encode_once(a.source, cancelled, min(a.frames, 24), width, height, cancel_after=min(a.frames, 8))
        report["cancel"]["orphan_temp_files"] = [p.name for p in a.work.glob(".*sdr-cancelled*")]
        report["run1"] = encode_once(a.source, out1, a.frames, width, height)
        report["run2"] = encode_once(a.source, out2, a.frames, width, height)
        report["run1"]["encoded_sha256"] = sha256_file(out1)
        report["run2"]["encoded_sha256"] = sha256_file(out2)
        report["run1"]["decode"] = decode_audit(ffmpeg, out1)
        report["run2"]["decode"] = decode_audit(ffmpeg, out2)
        report["gates"] = {
            "cancel_removed_output": not report["cancel"]["output_exists"],
            "frame_count_run1": report["run1"]["decode"]["frames"] == a.frames,
            "frame_count_run2": report["run2"]["decode"]["frames"] == a.frames,
            "source_pixel_repeatable": report["run1"]["source_pixels_sha256"] == report["run2"]["source_pixels_sha256"],
            "encoded_stream_repeatable": report["run1"]["encoded_sha256"] == report["run2"]["encoded_sha256"],
        }
        report["status"] = "measurements_completed"
    except Exception as exc:
        report.update(status="failed", error=repr(exc))
    finally:
        (a.work / "sdr-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": report["status"], "report": str(a.work / "sdr-acceptance.json")}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if report["status"] == "measurements_completed" else 1)


if __name__ == "__main__": main()
