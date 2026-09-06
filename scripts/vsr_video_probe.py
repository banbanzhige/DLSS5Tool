#!/usr/bin/env python3
"""Hardware smoke test for the SDR RTX VSR -> Feature 18 -> encoder path."""

import argparse
import json
import os
import sys
import time

import cv2


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dlss_host_process import ProcessLive
from super_resolution import ProcessSuperResolution
from video_export import FFmpegVideoWriter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--scale", type=int, choices=(2, 4), default=2)
    args = parser.parse_args()

    source = os.path.abspath(args.input)
    output = os.path.abspath(args.output)
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        raise RuntimeError("input could not be opened")
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS)) or 30.0
    output_width, output_height = width * args.scale, height * args.scale
    settings = {
        "host_backend": "v2",
        "host_auto_fallback": False,
        "host_submission": "merged",
        "host_persistent_buffers": True,
        "host_zero_fast_path": True,
        "host_in_flight": 1,
        "style": 0,
        "intensity": 1.0,
        "local_tone": 1.0,
        "local_struct": 1.0,
        "skin_struct": 0.5,
        "use_auto_mask": False,
    }
    sr = live = writer = None
    completed = False
    count = 0
    started = time.perf_counter()
    try:
        sr = ProcessSuperResolution(width, height, args.scale, is_hdr=False)
        live = ProcessLive(output_width, output_height, settings)
        writer = FFmpegVideoWriter(output, output_width, output_height, fps)
        while count < max(int(args.frames), 1):
            ok, frame = capture.read()
            if not ok:
                break
            rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
            upscaled = sr.process(rgba)
            enhanced = live.process(upscaled, reset=(count == 0))
            if enhanced is None:
                raise RuntimeError(f"Feature 18 returned no frame at index {count}")
            writer.write(cv2.cvtColor(enhanced, cv2.COLOR_RGBA2BGR))
            count += 1
        writer.finish()
        completed = True
    finally:
        capture.release()
        if live is not None:
            live.close()
        if sr is not None:
            sr.close()
        if writer is not None and not completed:
            writer.abort()
    elapsed = time.perf_counter() - started
    print(json.dumps({
        "ok": True,
        "frames": count,
        "fps": count / elapsed if elapsed else 0.0,
        "scale": args.scale,
        "output_size": [output_width, output_height],
        "output": output,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
