#!/usr/bin/env python3
"""Isolated raw-frame A/B probe for the legacy and v2 Feature 18 hosts."""

import argparse
import hashlib
import json
import os
import sys
import time
from collections import deque

import cv2


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import dlss_engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--backend", choices=("legacy", "v2"), required=True)
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--submission", choices=("merged", "compatibility"), default="merged")
    parser.add_argument("--persistent", type=int, choices=(0, 1), default=1)
    parser.add_argument("--zero-fast", type=int, choices=(0, 1), default=1)
    parser.add_argument("--in-flight", type=int, choices=(1, 2, 3), default=2)
    args = parser.parse_args()

    capture = cv2.VideoCapture(os.path.abspath(args.video))
    frames = []
    while len(frames) < max(args.frames, 1):
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA))
    capture.release()
    if not frames:
        raise RuntimeError("video yielded no frames")

    height, width = frames[0].shape[:2]
    settings = {
        "style": 0,
        "intensity": 1.0,
        "local_tone": 1.0,
        "local_struct": 1.0,
        "host_backend": args.backend,
        "host_submission": args.submission,
        "host_persistent_buffers": bool(args.persistent),
        "host_zero_fast_path": bool(args.zero_fast),
        "host_in_flight": args.in_flight,
        "host_auto_fallback": False,
    }
    initialized = time.perf_counter()
    live = dlss_engine.Live(width, height, settings)
    setup_seconds = time.perf_counter() - initialized
    hashes = []
    pending = deque()
    started = time.perf_counter()

    def collect(output):
        if output is None:
            raise RuntimeError("DLSS returned no output")
        pending.popleft()
        hashes.append(hashlib.sha256(memoryview(output).cast("B")).hexdigest())

    for index, rgba in enumerate(frames):
        if live.supports_async:
            if not live.enqueue(rgba, reset=(index == 0)):
                raise RuntimeError(f"enqueue failed at frame {index}")
            pending.append(index)
            if len(pending) >= live.max_in_flight:
                collect(live.dequeue())
        else:
            pending.append(index)
            collect(live.process(rgba, reset=(index == 0)))
    while pending:
        collect(live.dequeue())

    elapsed = time.perf_counter() - started
    combined = hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()
    print(json.dumps({
        "backend": live.backend,
        "frames": len(hashes),
        "setup_seconds": setup_seconds,
        "process_seconds": elapsed,
        "fps": len(hashes) / elapsed,
        "in_flight": live.max_in_flight,
        "combined_sha256": combined,
        "frame_sha256": hashes,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
