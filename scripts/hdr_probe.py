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

from dlss5tool.dlss_host_process import ProcessLive
from dlss5tool.super_resolution import ProcessSuperResolution
from dlss5tool.video_export import (
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
    parser.add_argument("--scale", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument('--flow', choices=('off', 'raft', 'nvofa'), default='off')
    parser.add_argument('--flow-edge', type=int, default=512)
    parser.add_argument('--mods-directory')
    parser.add_argument('--async-queue', action='store_true')
    args = parser.parse_args()

    source = os.path.abspath(args.input)
    output = os.path.abspath(args.output)
    if os.path.exists(output):
        raise ValueError('Use a new output path; existing files are not overwritten')
    os.makedirs(os.path.dirname(output), exist_ok=True)
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
        "color_primaries": info.get('color_primaries', 'bt2020'),
        "guidance_mode": 0 if args.flow == 'off' else 1,
        "guidance_flow_backend": 'raft' if args.flow == 'off' else args.flow,
        "guidance_flow_edge": args.flow_edge,
        "guidance_flow_fallback": False,
        "style": 1,
        "intensity": 1.0,
        "local_tone": 1.0,
        "local_struct": 1.0,
    }
    if args.mods_directory:
        settings['mods_directory'] = os.path.abspath(args.mods_directory)
    output_width = width * args.scale
    output_height = height * args.scale
    if output_width * output_height > 3840 * 2160:
        settings["host_in_flight"] = 1
    reader = None
    writer = None
    live = None
    sr_live = None
    started = time.perf_counter()
    count = 0
    completed = False
    try:
        reader = FFmpegHDRVideoReader(source, width, height, info, ffmpeg=ffmpeg)
        writer = FFmpegVideoWriter(
            output, output_width, output_height, fps,
            audio_source=None, hdr_metadata=info,
        )
        if args.scale > 1:
            sr_live = ProcessSuperResolution(width, height, args.scale, is_hdr=True)
        live = ProcessLive(output_width, output_height, settings)
        pending = 0
        if args.async_queue and not live.supports_async:
            raise RuntimeError('Requested async probe is not supported by this host')
        while count < max(int(args.frames), 1):
            frame = reader.read()
            if frame is None:
                break
            if sr_live is not None:
                frame = sr_live.process(frame)
            if args.async_queue:
                if pending >= live.max_in_flight:
                    writer.write(live.dequeue())
                    pending -= 1
                if not live.enqueue(frame, reset=(count == 0)):
                    raise RuntimeError('Async HDR submission failed')
                pending += 1
            else:
                result = live.process(frame, reset=(count == 0))
                if result is None:
                    raise RuntimeError(f"Feature 18 returned no frame at index {count}")
                writer.write(result)
            count += 1
        while pending:
            writer.write(live.dequeue())
            pending -= 1
        writer.finish()
        completed = True
        encoder = writer.encoder_name
        host = live.backend
        metrics = live.guidance_metrics
        guidance = live.guidance_info
        in_flight = live.max_in_flight
    finally:
        if reader is not None:
            reader.close()
        if live is not None:
            live.close()
        if sr_live is not None:
            sr_live.close()
        if writer is not None and not completed:
            writer.abort()
    elapsed = time.perf_counter() - started
    print(json.dumps({
        "ok": True,
        "frames": count,
        "fps": count / elapsed if elapsed else 0.0,
        "profile": info["profile"],
        "scale": args.scale,
        "output_size": [output_width, output_height],
        "encoder": encoder,
        "host": host,
        "async": args.async_queue,
        "in_flight": in_flight,
        "guidance": guidance,
        "guidance_metrics": metrics,
        "output": output,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
