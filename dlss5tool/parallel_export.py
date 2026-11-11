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
import threading
import time

import cv2

from dlss5tool.image_sequence import open_capture
from dlss5tool.gpu_decode import decode_options
from dlss5tool.video_timeline import inspect_video_timeline
from dlss5tool.vfr_mux import retime_encoded_video
from dlss5tool.process_job import create_process_job

from dlss5tool.video_export import (
    concat_video_segments,
    find_ffmpeg,
    resolve_encoder_size,
    select_video_encoder,
    video_encoder_name,
    mux_source_audio,
)


_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_pending_retirements = []
_retirement_lock = threading.Lock()


def _retire_process_trees(trees):
    pending, errors = [], []
    for process, lifetime in trees:
        failed = False
        if lifetime is not None:
            try:
                lifetime.close()
            except OSError as error:
                errors.append(str(error))
                failed = True
        if process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(str(error))
                failed = True
        if failed:
            # Retain both the job and its borrowed Popen handle for retry.
            pending.append((process, lifetime))
    return pending, errors


def _retry_pending_retirements():
    with _retirement_lock:
        pending, errors = _retire_process_trees(_pending_retirements)
        _pending_retirements[:] = pending
    if errors:
        raise RuntimeError('前次并行导出进程树尚未回收：' + '; '.join(errors))


def _worker_launch():
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--parallel-worker'], None
    environment = os.environ.copy()
    executable = sys.executable
    if os.name == 'nt':
        # The venv redirector can spawn Python before it is assigned to the job.
        # Launch the interpreter directly, retaining the venv's import paths.
        executable = getattr(sys, '_base_executable', sys.executable)
        if sys.prefix != sys.base_prefix:
            environment['__PYVENV_LAUNCHER__'] = sys.executable
    return [executable, '-B', '-m', 'dlss5tool.parallel_export_worker'], environment


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


def _encoding_worker_args(
    nvenc_preset="p5", rate_control="quality", quality_profile="high",
    video_bitrate_mbps=20.0, output_size=None, codec="auto",
):
    output_width, output_height = output_size or (0, 0)
    return [
        "--codec", codec,
        "--nvenc-preset", str(nvenc_preset),
        "--rate-control", str(rate_control),
        "--quality-profile", str(quality_profile),
        "--video-bitrate-mbps", str(video_bitrate_mbps),
        "--output-width", str(output_width),
        "--output-height", str(output_height),
    ]


def export_parallel(
    source, output, settings, workers=2, warmup=8, nvenc_preset="p5",
    rate_control="quality", quality_profile="high", video_bitrate_mbps=20.0,
    output_size=None, progress=None, timeline=None,
):
    """Export contiguous segments concurrently and losslessly concatenate them."""
    _retry_pending_retirements()
    source = os.path.abspath(source)
    output = os.path.abspath(output)
    cap = open_capture(source, **decode_options(settings))
    if not cap.isOpened():
        raise RuntimeError("无法打开输入视频")
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()
    timeline = timeline or inspect_video_timeline(source, fps, **decode_options(settings))
    frame_count, fps = timeline.frames, float(timeline.rate)
    workers = max(2, min(int(workers), 4, frame_count))
    warmup = max(0, min(int(warmup), 120))
    chunk = math.ceil(frame_count / workers)
    output_dir = os.path.dirname(output) or os.getcwd()
    ffmpeg = find_ffmpeg()
    output_width, output_height = resolve_encoder_size(width, height, output_size)
    codec, use_nvenc = select_video_encoder(
        ffmpeg, output_width, output_height, fps,
        nvenc_preset=nvenc_preset, rate_control=rate_control,
        quality_profile=quality_profile, video_bitrate_mbps=video_bitrate_mbps,
    )
    temp_dir = tempfile.mkdtemp(prefix=".dlss-parallel-", dir=output_dir)
    from dlss5tool.paths import project_root
    processes = []
    lifetime_jobs = []
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
            cmd, environment = _worker_launch()
            cmd += [
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
                '--fps', f'{fps:.12g}',
            ]
            cmd += _encoding_worker_args(
                nvenc_preset, rate_control, quality_profile,
                video_bitrate_mbps, output_size, codec=codec,
            )
            if timeline.restore:
                cmd.append('--disable-b-frames')
            stderr_handle = open(stderr_path, "wb")
            stderr_handles.append(stderr_handle)
            start_gate = os.path.join(temp_dir, f"worker-{worker_id}.start")
            cmd += ['--start-gate', start_gate]
            process = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=stderr_handle, creationflags=_CREATE_NO_WINDOW,
                env=environment,
                cwd=None if getattr(sys, "frozen", False) else str(project_root()),
            )
            processes.append(process)
            lifetime_jobs.append(create_process_job(process, nvidia_compat=True))
            # A fresh task-private gate is published only after job assignment.
            with open(start_gate, 'x', encoding='ascii') as gate:
                gate.write('start')
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
        for job, result in zip(jobs, results):
            if result.get('kept_frames') != job['frames']:
                raise RuntimeError('并行分段帧数与原时间线不一致')
        if progress:
            progress(frame_count, frame_count, "正在拼接视频和音频")

        joined = os.path.join(temp_dir, "joined.mp4")
        concat_video_segments(ffmpeg, [job["segment"] for job in jobs], joined)
        if timeline.restore:
            retimed = os.path.join(temp_dir, 'retimed.mp4')
            retime_encoded_video(joined, retimed, timeline.timestamps, timeline.end)
            joined = retimed
        audio_mode = mux_source_audio(ffmpeg, joined, source, output,
                                      source_video_start=str(timeline.start))
        elapsed = time.perf_counter() - started
        return {
            "frames": frame_count,
            "seconds": elapsed,
            "fps": frame_count / elapsed,
            "workers": workers,
            "warmup": warmup,
            "audio_mode": audio_mode,
            "encoder": video_encoder_name(codec, use_nvenc),
            "worker_results": results,
            "host_backends": sorted({
                item.get("host_backend", "unknown") for item in results if item
            }),
            "render_gpus": sorted({
                item.get("adapter_info", {}).get("name", "unknown")
                for item in results if item
            }),
            "in_flight": max(
                [int(item.get("in_flight", 1)) for item in results if item] or [1]
            ),
        }
    finally:
        trees = [(process, lifetime_jobs[index] if index < len(lifetime_jobs) else None)
                 for index, process in enumerate(processes)]
        pending, cleanup_errors = _retire_process_trees(trees)
        with _retirement_lock:
            _pending_retirements.extend(pending)
        for handle in stderr_handles:
            try:
                handle.close()
            except OSError:
                pass
        # Only remove the exact temporary directory created above, inside output_dir.
        if cleanup_errors:
            message = '并行导出进程树回收失败：' + '; '.join(cleanup_errors) + '\n诊断目录：' + temp_dir
            error = sys.exc_info()[1]
            if error is None:
                raise RuntimeError(message)
            if hasattr(error, 'add_note'):
                error.add_note(message)
        elif os.path.commonpath([os.path.abspath(temp_dir), os.path.abspath(output_dir)]) == os.path.abspath(output_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
