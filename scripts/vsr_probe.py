#!/usr/bin/env python3
"""Run a disposable RTX Video Super Resolution SDR/HDR smoke probe."""

import argparse
import os
import sys

import numpy as np


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dlss5tool.super_resolution import ProcessSuperResolution, runtime_status
from dlss5tool.dlss_host_process import ProcessLive


def _gradient(width, height, is_hdr):
    dtype = np.float16 if is_hdr else np.uint8
    maximum = 1.0 if is_hdr else 255
    x = np.linspace(0, maximum, width, dtype=dtype)
    y = np.linspace(0, maximum, height, dtype=dtype)[:, None]
    frame = np.empty((height, width, 4), dtype=dtype)
    frame[..., 0] = x
    frame[..., 1] = y
    frame[..., 2] = maximum / 2
    frame[..., 3] = maximum
    return np.ascontiguousarray(frame)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--scale", type=int, choices=(2, 4), default=2)
    parser.add_argument("--hdr", action="store_true")
    parser.add_argument("--hlg", action="store_true")
    parser.add_argument("--with-dlss", action="store_true")
    args = parser.parse_args()
    status = runtime_status()
    if not status["available"]:
        raise SystemExit("missing: " + ", ".join(status["missing"]))
    is_hdr = bool(args.hdr or args.hlg)
    frame = _gradient(args.width, args.height, is_hdr)
    with ProcessSuperResolution(
        args.width, args.height, args.scale, is_hdr=is_hdr,
    ) as session:
        output = session.process(frame)
    expected = (args.height * args.scale, args.width * args.scale, 4)
    if output.shape != expected or output.dtype != frame.dtype:
        raise SystemExit(f"unexpected output {output.dtype} {output.shape}; expected {frame.dtype} {expected}")
    if args.with_dlss:
        settings = {
            "host_backend": "v2",
            "host_auto_fallback": False,
            "host_zero_fast_path": True,
            "host_persistent_buffers": True,
            "host_submission": "merged",
            "host_in_flight": 1,
            "style": 0,
            "intensity": 1.0,
            "local_tone": 1.0,
            "local_struct": 1.0,
            "skin_struct": 0.5,
            "use_auto_mask": False,
        }
        if is_hdr:
            settings.update({
                "frame_format": "rgba16f",
                "color_profile": "hdr10_hlg" if args.hlg else "hdr10_pq",
            })
        dlss = ProcessLive(expected[1], expected[0], settings)
        try:
            enhanced = dlss.process(output, reset=True)
        finally:
            dlss.close()
        if enhanced is None or enhanced.shape != expected or enhanced.dtype != frame.dtype:
            raise SystemExit("DLSS stage returned an invalid frame")
        output = enhanced
    print(
        f"ok {'HLG' if args.hlg else 'HDR10' if is_hdr else 'SDR'} {args.width}x{args.height} -> "
        f"{output.shape[1]}x{output.shape[0]} "
        f"{'-> DLSS5 ' if args.with_dlss else ''}mean={float(output[..., :3].mean()):.5f}"
    )


if __name__ == "__main__":
    main()
