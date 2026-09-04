#!/usr/bin/env python3
"""Hardware smoke test for the RGBA16F HDR10/HLG Feature-18 path."""

import argparse
import json
import os
import sys
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dlss_host_process import ProcessLive
from video_export import (
    FFmpegHDRVideoReader,
    FFmpegVideoWriter,
    find_ffmpeg,
    probe_video_stream,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--frames", type=int, default=8)
    args = parser.parse_args()

    source = os.path.abspath(args.input)
    output = os.path.abspath(args.output)
    ffmpeg = find_ffmpeg()
    info = probe_video_stream(ffmpeg, source)
    if not info.get("is_hdr"):
        raise RuntimeError("input is not tagged HDR10/PQ or HLG")
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    fps = float(info.get("fps") or 30.0)
    if width <= 0 or height <= 0:
        raise RuntimeError("ffprobe did not return a valid video size")

    settings = {
        "host_backend": "v2",
        "host_auto_fallback": False,
        "host_submission": "merged",
        "host_persistent_buffers": True,
        "host_in_flight": 2,
        "frame_format": "rgba16f",
        "color_profile": info["profile"],
        "style": 1,
        "intensity": 1.0,
        "local_tone": 1.0,
        "local_struct": 1.0,
    }
    reader = None
    writer = None
    live = None
    started = time.perf_counter()
    count = 0
    completed = False
    try:
        reader = FFmpegHDRVideoReader(source, width, height, info, ffmpeg=ffmpeg)
        writer = FFmpegVideoWriter(
            output, width, height, fps, audio_source=None, hdr_metadata=info,
        )
        live = ProcessLive(width, height, settings)
        while count < max(int(args.frames), 1):
            frame = reader.read()
            if frame is None:
                break
            result = live.process(frame, reset=(count == 0))
            if result is None:
                raise RuntimeError(f"Feature 18 returned no frame at index {count}")
            writer.write(result)
            count += 1
        writer.finish()
        completed = True
        encoder = writer.encoder_name
        host = live.backend
    finally:
        if reader is not None:
            reader.close()
        if live is not None:
            live.close()
        if writer is not None and not completed:
            writer.abort()
    elapsed = time.perf_counter() - started
    print(json.dumps({
        "ok": True,
        "frames": count,
        "fps": count / elapsed if elapsed else 0.0,
        "profile": info["profile"],
        "encoder": encoder,
        "host": host,
        "output": output,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
