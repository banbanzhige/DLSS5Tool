#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Coordinate quality-warmed, multi-process DLSS segment export."""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

import cv2

from video_export import (
    concat_video_segments,
    find_ffmpeg,
    has_h264_nvenc,
    mux_source_audio,
)


_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _read_json(path):
    for attempt in range(3):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except PermissionError:
            time.sleep(0.02 * (attempt + 1))
        except (OSError, ValueError):
            return None
    return None


def _unique_diagnostic_dir(output_dir):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(output_dir, f"dlss-failure-{stamp}")
    candidate = base
    index = 2
    while os.path.exists(candidate):
        candidate = f"{base}-{index}"
        index += 1
    os.makedirs(candidate)
    return candidate


def _preserve_diagnostics(temp_dir, output_dir):
    diagnostic_dir = _unique_diagnostic_dir(output_dir)
    for name in os.listdir(temp_dir):
        if name.endswith((".json", ".log", ".txt")):
            try:
                shutil.copy2(os.path.join(temp_dir, name), os.path.join(diagnostic_dir, name))
            except OSError:
                pass
    return diagnostic_dir


def export_parallel(
    source, output, settings, workers=2, warmup=8, nvenc_preset="p5",
    progress=None,
):
    """Export contiguous segments concurrently and losslessly concatenate them."""
    source = os.path.abspath(source)
    output = os.path.abspath(output)
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError("无法打开输入视频")
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    workers = max(2, min(int(workers), 4, frame_count))
    warmup = max(0, min(int(warmup), 120))
    chunk = math.ceil(frame_count / workers)
    output_dir = os.path.dirname(output) or os.getcwd()
    temp_dir = tempfile.mkdtemp(prefix=".dlss-parallel-", dir=output_dir)
    ffmpeg = find_ffmpeg()
    use_nvenc = has_h264_nvenc(ffmpeg)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parallel_export_worker.py")
    processes = []
    jobs = []
    stderr_handles = []
    started = time.perf_counter()
    try:
        for worker_id in range(workers):
            start = worker_id * chunk
            end = min(frame_count, start + chunk)
            if start >= end:
                continue
            segment = os.path.join(temp_dir, f"segment_{worker_id:02d}.mp4")
            progress_path = os.path.join(temp_dir, f"progress_{worker_id:02d}.json")
            result_path = os.path.join(temp_dir, f"result_{worker_id:02d}.json")
            stderr_path = os.path.join(temp_dir, f"stderr_{worker_id:02d}.txt")
            cmd = [
                sys.executable, "-B", script,
                "--input", source,
                "--output", segment,
                "--start", str(start),
                "--end", str(end),
                "--warmup", str(warmup),
                "--settings", json.dumps(settings, ensure_ascii=False),
                "--progress", progress_path,
                "--result", result_path,
                "--worker-id", str(worker_id),
                "--nvenc", "1" if use_nvenc else "0",
                "--nvenc-preset", nvenc_preset,
            ]
            stderr_handle = open(stderr_path, "wb")
            stderr_handles.append(stderr_handle)
            process = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=stderr_handle, creationflags=_CREATE_NO_WINDOW,
            )
            processes.append(process)
            jobs.append({
                "segment": segment,
                "progress": progress_path,
                "result": result_path,
                "stderr": stderr_path,
                "frames": end - start,
                "worker_id": worker_id,
            })

        while any(process.poll() is None for process in processes):
            done = 0
            for job in jobs:
                state = _read_json(job["progress"])
                done += int(state.get("done", 0)) if state else 0
            if progress:
                progress(done, frame_count, f"并行导出 {workers} 进程")
            time.sleep(0.08)

        codes = [process.wait() for process in processes]
        for handle in stderr_handles:
            handle.close()
        stderr_handles.clear()
        results = [_read_json(job["result"]) for job in jobs]
        if any(code != 0 for code in codes) or any(not item or not item.get("ok") for item in results):
            diagnostic_dir = _preserve_diagnostics(temp_dir, output_dir)
            details = []
            for job, code, item in zip(jobs, codes, results):
                if code == 0 and item and item.get("ok"):
                    continue
                prefix = f"worker {job['worker_id']} (exit={code})"
                if item:
                    error = item.get("error", "未知错误")
                    trace = str(item.get("traceback", "")).strip()[-2500:]
                    details.append(f"{prefix}: {error}" + (f"\n{trace}" if trace else ""))
                else:
                    try:
                        with open(job["stderr"], encoding="utf-8", errors="replace") as handle:
                            stderr = handle.read().strip()[-2500:]
                    except OSError:
                        stderr = "未生成结果 JSON"
                    details.append(f"{prefix}: {stderr or '未生成结果 JSON'}")
            raise RuntimeError(
                "并行 DLSS 子进程失败：\n" + "\n\n".join(details) +
                "\n诊断文件已保留在：" + diagnostic_dir
            )
        if progress:
            progress(frame_count, frame_count, "正在拼接视频和音频")

        joined = os.path.join(temp_dir, "joined.mp4")
        concat_video_segments(ffmpeg, [job["segment"] for job in jobs], joined)
        audio_mode = mux_source_audio(ffmpeg, joined, source, output)
        elapsed = time.perf_counter() - started
        return {
            "frames": frame_count,
            "seconds": elapsed,
            "fps": frame_count / elapsed,
            "workers": workers,
            "warmup": warmup,
            "audio_mode": audio_mode,
            "encoder": "NVIDIA NVENC (GPU)" if use_nvenc else "libx264 (CPU 回退)",
            "worker_results": results,
            "host_backends": sorted({
                item.get("host_backend", "unknown") for item in results if item
            }),
            "in_flight": max(
                [int(item.get("in_flight", 1)) for item in results if item] or [1]
            ),
        }
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        for handle in stderr_handles:
            try:
                handle.close()
            except OSError:
                pass
        # Only remove the exact temporary directory created above, inside output_dir.
        if os.path.commonpath([os.path.abspath(temp_dir), os.path.abspath(output_dir)]) == os.path.abspath(output_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
