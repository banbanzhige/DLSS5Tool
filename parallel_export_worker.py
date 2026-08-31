#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Child process for one ordered segment of a parallel DLSS export."""

import argparse
import json
import os
import time
import traceback

import cv2

import dlss_engine
from video_export import FFmpegVideoWriter, compose_output_frame


def _write_json(path, payload, required=True):
    """Atomically publish worker state despite transient Defender/indexer locks."""
    temp = f"{path}.tmp.{os.getpid()}"
    last_error = None
    for attempt in range(12 if required else 3):
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
            os.replace(temp, path)
            return True
        except PermissionError as ex:
            last_error = ex
            time.sleep(min(0.02 * (2 ** attempt), 0.4))
        except OSError as ex:
            last_error = ex
            if not required:
                break
            time.sleep(min(0.02 * (2 ** attempt), 0.4))
    try:
        if os.path.isfile(temp):
            os.remove(temp)
    except OSError:
        pass
    if required:
        raise last_error or OSError("无法写入工作进程结果文件")
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--warmup", type=int, default=8)
    parser.add_argument("--settings", default="{}")
    parser.add_argument("--progress", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--nvenc", type=int, default=1)
    parser.add_argument("--nvenc-preset", default="p5")
    args = parser.parse_args()

    source = os.path.abspath(args.input)
    output = os.path.abspath(args.output)
    progress_path = os.path.abspath(args.progress)
    result_path = os.path.abspath(args.result)
    process_start = max(0, args.start - max(0, args.warmup))
    settings = json.loads(args.settings)
    view = int(settings.get("output_view", 0))
    mix = float(settings.get("output_mix", 1.0))
    writer = None
    started = time.perf_counter()
    payload = {
        "worker_id": args.worker_id,
        "start": args.start,
        "end": args.end,
        "process_start": process_start,
    }
    try:
        dlss_engine.LOG_PATH = os.path.splitext(result_path)[0] + ".ngx.log"
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            raise RuntimeError("无法打开输入视频")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.set(cv2.CAP_PROP_POS_FRAMES, process_start)
        actual = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
        if actual != process_start:
            raise RuntimeError(f"视频定位失败: 请求 {process_start}，实际 {actual}")

        writer = FFmpegVideoWriter(
            output, width, height, fps, audio_source=None,
            use_nvenc=bool(args.nvenc), nvenc_preset=args.nvenc_preset,
        )
        live = None
        pending = []
        index = process_start
        kept = 0
        dlss_seconds = 0.0

        def consume_one():
            nonlocal kept, dlss_seconds
            queued_index, queued_frame = pending.pop(0)
            wait_started = time.perf_counter()
            processed_rgba = live.dequeue()
            dlss_seconds += time.perf_counter() - wait_started
            if processed_rgba is None:
                raise RuntimeError(f"DLSS 在第 {queued_index} 帧异步回读失败")
            if queued_index >= args.start:
                processed = cv2.cvtColor(processed_rgba, cv2.COLOR_RGBA2BGR)
                writer.write(compose_output_frame(queued_frame, processed, view, mix))
                kept += 1
                if kept == 1 or kept % 4 == 0 or queued_index + 1 == args.end:
                    # Progress is advisory. A scanner locking this JSON must never
                    # cancel a successful GPU/encoder job.
                    _write_json(
                        progress_path,
                        {"done": kept, "total": args.end - args.start},
                        required=False,
                    )

        while index < args.end:
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f"视频在第 {index} 帧提前结束")
            rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
            if live is None:
                live = dlss_engine.Live(width, height, settings)
            dlss_started = time.perf_counter()
            if live.supports_async:
                if not live.enqueue(rgba, reset=(index == process_start)):
                    raise RuntimeError(f"DLSS 在第 {index} 帧异步提交失败")
                dlss_seconds += time.perf_counter() - dlss_started
                pending.append((index, frame))
                if len(pending) >= live.max_in_flight:
                    consume_one()
            else:
                processed_rgba = live.process(rgba, reset=(index == process_start))
                dlss_seconds += time.perf_counter() - dlss_started
                if processed_rgba is None:
                    raise RuntimeError(f"DLSS 在第 {index} 帧失败")
                pending.append((index, frame))
                processed = cv2.cvtColor(processed_rgba, cv2.COLOR_RGBA2BGR)
                if index >= args.start:
                    writer.write(compose_output_frame(frame, processed, view, mix))
                    kept += 1
                    if kept == 1 or kept % 4 == 0 or index + 1 == args.end:
                        _write_json(
                            progress_path,
                            {"done": kept, "total": args.end - args.start},
                            required=False,
                        )
                pending.pop()
            index += 1
        while pending:
            consume_one()
        cap.release()
        writer.finish()
        payload.update({
            "ok": True,
            "kept_frames": kept,
            "processed_frames": args.end - process_start,
            "dlss_seconds": dlss_seconds,
            "wall_seconds": time.perf_counter() - started,
            "encoder": writer.encoder_name,
            "host_backend": live.backend if live else "unknown",
            "in_flight": live.max_in_flight if live else 1,
        })
    except Exception as ex:
        if writer:
            writer.abort()
        payload.update({
            "ok": False,
            "error": repr(ex),
            "traceback": traceback.format_exc(),
            "wall_seconds": time.perf_counter() - started,
        })
    _write_json(result_path, payload, required=True)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
