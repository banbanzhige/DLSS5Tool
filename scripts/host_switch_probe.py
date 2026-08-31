#!/usr/bin/env python3
"""One-frame hardware smoke test for isolated legacy -> v2 hot switching."""

import hashlib
import json
import os
import sys

import cv2
import numpy as np


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dlss_host_process import ProcessLive


def _digest(frame):
    return hashlib.sha256(memoryview(frame).cast("B")).hexdigest()


def main():
    bgr = np.full((360, 640, 3), 90, np.uint8)
    cv2.circle(bgr, (320, 180), 80, (255, 0, 0), -1)
    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
    settings = {
        "style": 1,
        "intensity": 0.9,
        "local_tone": 1.0,
        "local_struct": 1.0,
        "host_backend": "legacy",
        "host_auto_fallback": False,
        "host_submission": "merged",
        "host_zero_fast_path": True,
        "host_persistent_buffers": True,
        "host_in_flight": 2,
    }
    live = ProcessLive(640, 360, settings)
    try:
        legacy = live.process(rgba, reset=True)
        before = {
            "backend": live.backend,
            "shape": list(legacy.shape) if legacy is not None else None,
            "sha256": _digest(legacy) if legacy is not None else None,
        }
        live.update({**settings, "host_backend": "v2"})
        optimized = live.process(rgba, reset=True)
        queued = []
        if live.supports_async:
            if not live.enqueue(rgba, reset=True) or not live.enqueue(rgba):
                raise RuntimeError("v2 async enqueue failed")
            for _ in range(2):
                output = live.dequeue()
                if output is None:
                    raise RuntimeError("v2 async dequeue failed")
                queued.append(_digest(output))
        after = {
            "backend": live.backend,
            "shape": list(optimized.shape) if optimized is not None else None,
            "sha256": _digest(optimized) if optimized is not None else None,
            "in_flight": live.max_in_flight,
            "async_sha256": queued,
        }
        live.update({**settings, "host_backend": "legacy"})
        legacy_again = live.process(rgba, reset=True)
        restored = {
            "backend": live.backend,
            "shape": list(legacy_again.shape) if legacy_again is not None else None,
            "sha256": _digest(legacy_again) if legacy_again is not None else None,
        }
        print(json.dumps({
            "ok": True, "before": before, "after": after,
            "restored": restored,
        }))
        return 0
    finally:
        live.close()


if __name__ == "__main__":
    raise SystemExit(main())
