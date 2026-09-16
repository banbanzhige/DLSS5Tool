#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shareable, one-click diagnostics for the packaged and source applications."""

import ctypes
from collections import Counter
from datetime import datetime
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import numpy as np

from dlss5tool.app_version import APP_VERSION
from dlss5tool import dlss_engine
from dlss5tool import mod_paths
from dlss5tool import super_resolution
from dlss5tool import updater


_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROBE_TIMEOUT_SECONDS = 45
_REPORT_SCHEMA = 3
_FEATURE_PROBE_TIMEOUT = 30
_EXPORT_HISTORY_LIMIT = 5
_EXPORT_OUTCOMES = {"success", "failed", "cancelled"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
_KNOWN_RUNTIMES = {
    "CEB6432F6FBDF44D886014BCD47241932BF8B67439FEEF9BBDD0961436662650":
        "RTX 40 项目默认核心 / 310.8.0.0",
    "6EB209E764F39872625DEBD6ABAF45E2BB6322F6F270F781F70C059AE30B3927":
        "RTX 30 兼容核心 / 310.8.SF-v2",
    "E16BCF15E16E13F527491CDF7845B2FE6521A738D8F7C9C721866A8496E1FC8E":
        "RTX 50 NVIDIA 签名核心 / 310.8.0.0",
}
_KNOWN_DLSSG = {
    "135EAF0733C1E37381A8C28ABCF7A862404A54132B81787C04E35D09EFC5E36F":
        "DLSSG 实验钉扎运行库 / 310.7.0.0",
}
_UI_LOG_MARKERS = (
    "dlss", "ngx", "d3d12", "主机", "后端", "失败", "错误", "异常",
    "hdr", "编码器", "性能", "色彩检测", "preview", "vsr", "超分",
    "插帧", "dlssg", "时间戳", "nvofa",
)
_MEDIA_PATH_PATTERN = re.compile(
    r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\r\n<>|\"]+?\."
    r"(?:mp4|avi|mov|mkv|m4v|webm|png|jpe?g|webp|bmp|tiff?)"
)


class _VSFixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]


def suggested_report_name(now=None):
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"DLSS5Tool-diagnostic-{stamp}.log"


def _redact(value):
    text = str(value or "")
    candidates = {
        os.path.expanduser("~"),
        os.environ.get("USERPROFILE", ""),
    }
    for candidate in sorted((item for item in candidates if item), key=len, reverse=True):
        text = re.sub(re.escape(os.path.abspath(candidate)), "%USERPROFILE%", text, flags=re.I)
    return text


def _redact_media_paths(value):
    def replacement(match):
        name = re.split(r"[\\/]", match.group(0))[-1]
        return "%MEDIA_FILE%\\" + name

    return _MEDIA_PATH_PATTERN.sub(replacement, str(value or ""))


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _windows_file_version(path):
    if os.name != "nt" or not os.path.isfile(path):
        return ""
    try:
        handle = ctypes.c_uint32(0)
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, ctypes.byref(handle))
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buffer):
            return ""
        value = ctypes.c_void_p()
        length = ctypes.c_uint32(0)
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(value), ctypes.byref(length),
        ):
            return ""
        info = ctypes.cast(value, ctypes.POINTER(_VSFixedFileInfo)).contents
        parts = (
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
            info.dwFileVersionLS & 0xFFFF,
        )
        return ".".join(str(part) for part in parts)
    except (AttributeError, OSError, ValueError):
        return ""


def describe_file(path):
    absolute = os.path.abspath(path)
    result = {
        "path": _redact(absolute),
        "exists": os.path.isfile(absolute),
    }
    if not result["exists"]:
        return result
    try:
        result.update({
            "bytes": os.path.getsize(absolute),
            "file_version": _windows_file_version(absolute) or "unknown",
            "sha256": _sha256(absolute),
        })
        name = os.path.basename(absolute).lower()
        if name == "nvngx_dlssnr.dll" or os.path.normcase(absolute) == os.path.normcase(dlss_engine.DLSSNR_DLL):
            result["runtime_profile"] = _KNOWN_RUNTIMES.get(
                result["sha256"], "未知/自定义运行时",
            )
        elif name == "nvngx_dlssg.dll":
            result["runtime_profile"] = _KNOWN_DLSSG.get(
                result["sha256"], "未知/自定义插帧运行库",
            )
    except OSError as exception:
        result["read_error"] = repr(exception)
    return result


def filter_ui_log(text, limit=200):
    selected = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        lowered = line.lower()
        if line and any(marker in lowered for marker in _UI_LOG_MARKERS):
            selected.append(_redact(_redact_media_paths(line)))
    return selected[-max(int(limit), 1):]


def export_history_path():
    overridden = os.environ.get("DLSS5TOOL_EXPORT_HISTORY_PATH")
    if overridden:
        return os.path.abspath(overridden)
    from dlss5tool.paths import state_path
    return str(state_path("dlss5_export_history.json"))


def _iso_timestamp(value=None):
    if value in (None, "", 0, 0.0):
        return datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        return datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, TypeError, ValueError):
        return datetime.now().astimezone().isoformat(timespec="seconds")


def _job_get(job, name, default=None):
    if isinstance(job, dict):
        return job.get(name, default)
    return getattr(job, name, default)


def planned_output_size(width, height, export_settings=None, kind="video"):
    from dlss5tool.super_resolution import normalize_scale, target_size
    try:
        width, height = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return 0, 0
    scale = normalize_scale((export_settings or {}).get("super_resolution_scale", 1))
    if scale > 1:
        return target_size(width, height, scale)
    return width, height


def make_export_record(
    *, source_path="", output_path="", kind="video", color_info=None,
    export_settings=None, result=None, source_width=0, source_height=0,
    source_frames=0, source_fps=0, planned=None, elapsed=None, extra=None,
    at=None,
):
    """Compact, shareable export snapshot. Stores the output path only locally."""
    export_settings = dict(export_settings or {})
    color_info = dict(color_info or {})
    result = dict(result or {})
    if result.get("cancelled"):
        outcome = "cancelled"
    elif result.get("success"):
        outcome = "success"
    else:
        outcome = "failed"
    try:
        planned_width, planned_height = (
            (int(planned[0]), int(planned[1])) if planned else planned_output_size(
                source_width, source_height, export_settings, kind,
            )
        )
    except (TypeError, ValueError, IndexError):
        planned_width, planned_height = planned_output_size(
            source_width, source_height, export_settings, kind,
        )
    from dlss5tool.super_resolution import normalize_scale
    try:
        frames = int(result.get("frames") or 0)
    except (TypeError, ValueError):
        frames = 0
    try:
        fg = int(export_settings.get("frame_generation_multiplier") or 1)
    except (TypeError, ValueError):
        fg = 1
    if fg not in (1, 2, 3, 4):
        fg = 1
    try:
        elapsed_seconds = None if elapsed is None else round(float(elapsed), 2)
    except (TypeError, ValueError):
        elapsed_seconds = None
    record = {
        "at": at or _iso_timestamp(),
        "outcome": outcome,
        "error": _redact_media_paths(result.get("error") or ""),
        "source": {
            "name": os.path.basename(source_path or ""),
            "kind": "image" if kind == "image" else "video",
            "width": int(source_width or 0),
            "height": int(source_height or 0),
            "frames": int(source_frames or 0),
            "fps": source_fps or 0,
            "color": color_info.get("label") or "",
            "is_hdr": bool(color_info.get("is_hdr")),
        },
        "requested": {
            "super_resolution_scale": normalize_scale(export_settings.get("super_resolution_scale", 1)),
            "frame_generation_multiplier": fg,
            "output_resolution": str(export_settings.get("output_resolution") or "source"),
            "hdr_mode": bool(export_settings.get("hdr_mode", True)),
            "hdr_active": bool(export_settings.get("hdr_mode", True) and color_info.get("is_hdr")),
            "container": str(export_settings.get("resolved_container") or export_settings.get("output_container") or ""),
            "mode": str(export_settings.get("mode") or "single"),
        },
        "planned": {"width": planned_width, "height": planned_height},
        "result": {
            "frames": frames,
            "elapsed_seconds": elapsed_seconds,
            "encoder": result.get("encoder") or "",
            "host_backend": result.get("host_backend") or "",
        },
        "output_path": os.path.abspath(output_path) if output_path else "",
    }
    if extra:
        record["result"].update({key: extra[key] for key in extra if extra[key] not in (None, "")})
    return record


def snapshot_from_queue_job(job):
    export_settings = dict(_job_get(job, "export_settings") or {})
    metadata = dict(_job_get(job, "metadata") or {})
    color_info = dict(_job_get(job, "color_info") or {})
    state = str(_job_get(job, "state") or "failed")
    outcome = {
        "completed": "success", "failed": "failed",
        "cancelled": "cancelled", "interrupted": "failed",
    }.get(state, "failed")
    width = metadata.get("width") or 0
    height = metadata.get("height") or 0
    return make_export_record(
        source_path=_job_get(job, "source_path") or "",
        output_path=_job_get(job, "output_path") or "",
        kind=_job_get(job, "media_kind") or "video",
        color_info=color_info,
        export_settings=export_settings,
        result={
            "success": outcome == "success",
            "cancelled": outcome == "cancelled",
            "error": _job_get(job, "error") or "",
            "frames": _job_get(job, "progress_done") or metadata.get("frames") or 0,
        },
        source_width=width,
        source_height=height,
        source_frames=metadata.get("frames") or 0,
        source_fps=metadata.get("fps") or 0,
        at=_iso_timestamp(_job_get(job, "finished_at") or _job_get(job, "started_at")),
    )


def load_export_history(path=None):
    path = os.path.abspath(path or export_history_path())
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, TypeError, ValueError):
        return []
    rows = raw.get("exports", []) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    history = []
    for item in rows:
        if isinstance(item, dict) and item.get("outcome") in _EXPORT_OUTCOMES:
            history.append(item)
    return history


def append_export_history(record, path=None, limit=_EXPORT_HISTORY_LIMIT):
    if not isinstance(record, dict) or not record.get("outcome"):
        return []
    path = os.path.abspath(path or export_history_path())
    history = load_export_history(path)
    history.append(record)
    history = history[-max(int(limit), 1):]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {"version": 1, "exports": history}
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)
    return history


def inspect_output_file(path):
    """Read container size only. Never copies pixels into the report."""
    name = os.path.basename(path or "")
    if not path or not os.path.isfile(path):
        return {"exists": False, "name": name}
    info = {
        "exists": True,
        "name": name,
        "bytes": os.path.getsize(path),
    }
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXTENSIONS:
        try:
            from PIL import Image
            with Image.open(path) as image:
                info["width"], info["height"] = int(image.width), int(image.height)
        except Exception as exception:
            info["probe_error"] = str(exception).splitlines()[0][:200]
        return info
    try:
        from dlss5tool.video_export import find_ffmpeg, probe_video_stream
        probed = probe_video_stream(find_ffmpeg(), path)
        info.update({
            "width": int(probed.get("width") or 0),
            "height": int(probed.get("height") or 0),
            "fps": probed.get("fps") or 0,
            "frames": int(probed.get("frames") or 0),
            "pixel_format": probed.get("pixel_format") or probed.get("pix_fmt") or "",
            "color": probed.get("label") or "",
        })
    except Exception as exception:
        info["probe_error"] = str(exception).splitlines()[0][:200]
    return info


def export_hints(record):
    planned = record.get("planned") or {}
    output = record.get("output") or {}
    hints = []
    if record.get("outcome") == "cancelled":
        hints.append("导出已取消。")
    elif record.get("outcome") == "failed":
        error = str(record.get("error") or "").strip()
        hints.append("导出失败" + (": " + error.splitlines()[0] if error else "。"))
    planned_size = (int(planned.get("width") or 0), int(planned.get("height") or 0))
    actual_size = (int(output.get("width") or 0), int(output.get("height") or 0))
    scale = int((record.get("requested") or {}).get("super_resolution_scale") or 1)
    if output.get("exists") and planned_size[0] > 0 and actual_size[0] > 0 and planned_size != actual_size:
        hints.append(
            f"计划输出 {planned_size[0]}×{planned_size[1]}，实际文件 {actual_size[0]}×{actual_size[1]}"
        )
    elif scale > 1 and output.get("exists") and actual_size == (
        int((record.get("source") or {}).get("width") or 0),
        int((record.get("source") or {}).get("height") or 0),
    ) and actual_size[0] > 0:
        hints.append(f"选择了 {scale}× 超分，但输出仍是源尺寸 {actual_size[0]}×{actual_size[1]}")
    elif record.get("outcome") == "success" and not output.get("exists"):
        hints.append("记录为成功，但诊断时输出文件已不存在。")
    return hints


def redact_export_record(record):
    payload = json.loads(json.dumps(record, ensure_ascii=False, default=str))
    payload.pop("output_path", None)
    if payload.get("error"):
        payload["error"] = _redact(_redact_media_paths(payload["error"]))
    output = dict(payload.get("output") or {})
    if output.get("name"):
        output["name"] = os.path.basename(str(output["name"]))
    payload["output"] = output
    source = dict(payload.get("source") or {})
    if source.get("name"):
        source["name"] = os.path.basename(str(source["name"]))
    payload["source"] = source
    return payload


def _export_identity(record):
    source = (record.get("source") or {}).get("name") or ""
    output = os.path.basename(record.get("output_path") or (record.get("output") or {}).get("name") or "")
    return record.get("outcome"), source, output


def collect_recent_exports(extra=None, queue_jobs=None, inspect=True, limit=_EXPORT_HISTORY_LIMIT):
    combined = []
    seen = set()
    for item in list(load_export_history()) + list(extra or []):
        if not isinstance(item, dict):
            continue
        key = _export_identity(item)
        if key in seen:
            continue
        seen.add(key)
        combined.append(dict(item))
    for job in queue_jobs or []:
        state = str(_job_get(job, "state") or "")
        if state not in {"completed", "failed", "cancelled", "interrupted"}:
            continue
        snapshot = snapshot_from_queue_job(job)
        key = _export_identity(snapshot)
        if key in seen:
            continue
        seen.add(key)
        combined.append(snapshot)
    combined.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    selected = combined[:max(int(limit), 1)]
    for record in selected:
        path = record.get("output_path") or ""
        record["output"] = inspect_output_file(path) if inspect else {
            "exists": bool(path and os.path.isfile(path)),
            "name": os.path.basename(path),
        }
        record["hints"] = export_hints(record)
    return [redact_export_record(record) for record in selected]


def _format_export_records(records):
    if not records:
        return ["（无。直接导出或队列完成后才会记录；未要求用户再导一次。）"]
    lines = []
    for index, record in enumerate(records, start=1):
        source = record.get("source") or {}
        requested = record.get("requested") or {}
        planned = record.get("planned") or {}
        output = record.get("output") or {}
        result = record.get("result") or {}
        scale = int(requested.get("super_resolution_scale") or 1)
        fg = int(requested.get("frame_generation_multiplier") or 1)
        lines.append(
            f"{index}. {record.get('at') or ''}  {record.get('outcome') or ''}"
        )
        lines.append(
            "   源: {name}  {width}×{height}  {frames}f  {fps}  {color}".format(
                name=source.get("name") or "（未命名）",
                width=source.get("width") or 0,
                height=source.get("height") or 0,
                frames=source.get("frames") or 0,
                fps=source.get("fps") or 0,
                color=source.get("color") or "",
            )
        )
        lines.append(
            "   计划: 超分 {scale}× → {width}×{height} · 插帧 {fg} · HDR{hdr} · {container}".format(
                scale=scale,
                width=planned.get("width") or 0,
                height=planned.get("height") or 0,
                fg="关闭" if fg <= 1 else f"{fg}×",
                hdr="开" if requested.get("hdr_active") else "关",
                container=requested.get("container") or "mp4",
            )
        )
        if output.get("exists"):
            size = ""
            if output.get("width") and output.get("height"):
                size = f"  {output['width']}×{output['height']}"
            bytes_text = f"  {output['bytes']} bytes" if output.get("bytes") else ""
            lines.append(
                f"   输出: %MEDIA_FILE%\\{output.get('name') or ''}  存在{bytes_text}{size}"
            )
        else:
            name = output.get("name") or ""
            lines.append("   输出: " + (f"%MEDIA_FILE%\\{name}  不存在" if name else "（无文件）"))
        detail = []
        if result.get("frames"):
            detail.append(f"{result['frames']} 帧")
        if result.get("elapsed_seconds") is not None:
            detail.append(f"{result['elapsed_seconds']}s")
        if result.get("encoder"):
            detail.append(str(result["encoder"]))
        if result.get("host_backend"):
            detail.append("主机 " + str(result["host_backend"]))
        if detail:
            lines.append("   结果: " + " · ".join(detail))
        if record.get("error"):
            lines.append("   错误: " + str(record["error"]).splitlines()[0])
        for hint in record.get("hints") or []:
            lines.append("   判断: " + hint)
    return lines


def _command_output(command, timeout=15):
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        return {
            "returncode": result.returncode,
            "stdout": _redact(stdout),
            "stderr": _redact(stderr),
        }
    except (OSError, subprocess.SubprocessError) as exception:
        return {"error": repr(exception)}


def _worker_command(result_path, native_log_path, settings_path, backend):
    arguments = [
        "--diagnostic-worker", result_path, native_log_path, settings_path, backend,
    ]
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments], os.path.dirname(os.path.abspath(sys.executable))
    from dlss5tool.paths import project_root
    script = str(project_root() / "gui.py")
    return [sys.executable, "-B", script, *arguments], os.path.dirname(script)


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _probe_backend(temp_dir, settings_path, backend):
    result_path = os.path.join(temp_dir, f"{backend}-result.json")
    native_log_path = os.path.join(temp_dir, f"{backend}-native.log")
    command, cwd = _worker_command(result_path, native_log_path, settings_path, backend)
    started = time.perf_counter()
    process = None
    timed_out = False
    launch_error = ""
    try:
        process = subprocess.run(
            command, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_PROBE_TIMEOUT_SECONDS, creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as exception:
        launch_error = repr(exception)
    result = _read_json(result_path)
    result.update({
        "backend_requested": backend,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "native_log": _redact(_read_text(native_log_path).strip()),
        "timed_out": timed_out,
    })
    if process is not None:
        result["process_returncode"] = process.returncode
        stdout = process.stdout.decode("utf-8", errors="replace").strip()
        stderr = process.stderr.decode("utf-8", errors="replace").strip()
        if stdout:
            result["worker_stdout"] = _redact(stdout[-4000:])
        if stderr:
            result["worker_stderr"] = _redact(stderr[-4000:])
    if launch_error:
        result["launch_error"] = launch_error
    if not result.get("ok") and not any(
        result.get(name) for name in ("error", "launch_error", "timed_out")
    ):
        result["error"] = "诊断子进程未返回结果"
    return result


def _probe_hints(probe):
    if probe.get("skipped"):
        return ["该宿主文件未随当前版本提供，已跳过，不影响其它宿主的诊断结果。"]
    log = str(probe.get("native_log") or "")
    error = str(probe.get("error") or "")
    combined = log + "\n" + error
    hints = []
    if probe.get("timed_out"):
        hints.append("宿主超过 45 秒未响应；检查驱动、GPU 占用或安全软件拦截。")
    if "D3D12 setup failed" in combined:
        hints.append("D3D12 设备创建失败；检查系统、驱动、远程桌面和高性能 GPU 选择。")
    if "no compatible NVIDIA D3D12 adapter found" in combined:
        hints.append("宿主没有找到可用的 NVIDIA D3D12 适配器；核对显卡是否启用及 NVIDIA 驱动。")
    if "requested NVIDIA adapter is unavailable" in combined:
        hints.append("手动选择的 DLSS 渲染 GPU 当前不可用；刷新 GPU 列表并重新选择。")
    if "load nvngx_dlssnr.dll failed" in combined:
        hints.append("运行时加载失败；检查实际替换路径、文件完整性和安全软件。")
    if "missing runtime exports" in combined:
        hints.append("DLL 缺少必需导出，可能拿错或损坏。")
    if re.search(r"0xBAD00002\b", combined, re.I):
        hints.append(
            "NGX 返回 0xBAD00002（PlatformError）：底层图形 API、系统或依赖发生错误；"
            "它不等于 FeatureNotSupported，也不能单凭此码判定显存不足。"
        )
        if re.search(r"CreateFeature\(18\).*0xBAD00002\b", combined, re.I):
            hints.append("失败阶段：创建 Feature 18，尚未处理输入图像；更换图片不能验证或解决该初始化错误。")
        hints.append(
            "请保留完整报告中的显卡/驱动、实际 DLL 路径、版本与 SHA256，以及 v2/legacy 各自的日志。"
            "先核对完整解压的可信安装包、运行库选择和 NVIDIA 驱动；不要据此安装光流组件或盲目替换 DLL。"
        )
    for code, name, advice in (
        ('0xBAD0000C', 'OutOfDate', '驱动或功能运行库版本过旧，请核对两者版本。'),
        ('0xBAD0000D', 'OutOfGPUMemory', 'GPU 显存不足，请关闭其它 GPU 任务并降低处理分辨率。'),
        ('0xBAD0000F', 'UnableToWriteToAppDataPath', 'NGX 数据目录不可写，请核对目录权限。'),
    ):
        if re.search(code + r'\b', combined, re.I):
            hints.append(f'NGX 返回 {code}（{name}）：{advice}')
    if "caller/static initialization failed" in combined:
        hints.append("NGX 静态初始化或调用者检查失败；核对驱动、GPU 与 DLL 代际。")
    if re.search(r"0xBAD00001", combined, re.I):
        hints.append(
            "NGX 返回 0xBAD00001（FeatureNotSupported）；通常是 DLL 与 RTX "
            "代际不匹配、驱动过旧或程序使用了核显。先在 Windows“设置 → 系统 → "
            "显示 → 图形”中将 DLSS5Tool.exe 设为“高性能（NVIDIA GPU）”并重启；"
            "再从 Releases 列表下载 30/40/50 系对应运行库，关闭程序后只替换 "
            f"mods\\nvngx_dlssnr.dll 后刷新检测（无外置库时使用 _internal\\nvngx_dlssnr.dll）：{updater.RELEASES_URL}"
        )
    elif re.search(r"(?:Init_with_ProjectID|runtime Init_Ext).*0xBAD", combined, re.I):
        hints.append("NGX 初始化返回 BAD 错误；通常与驱动、默认适配器或硬件支持有关。")
    if "Feature 18 ready" in combined and probe.get("ok"):
        hints.append("Feature 18 初始化和单帧处理通过。")
    if probe.get("ok") and not hints:
        hints.append("宿主初始化和单帧处理通过。")
    if not hints and not probe.get("ok"):
        hints.append("未匹配已知错误，请结合异常与原生日志继续分析。")
    return hints


def _format_mapping(mapping, indent=""):
    return json.dumps(mapping, ensure_ascii=False, indent=2).replace("\n", "\n" + indent)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _describe_adapters():
    try:
        records = dlss_engine.available_render_adapters()
    except Exception as exception:
        return [f"无法枚举适配器: {type(exception).__name__}: {exception}"]
    if not records:
        return ["未检测到可用于 DLSS 的 NVIDIA D3D12 适配器。"]
    lines = []
    nvidia_names = []
    for index, record in enumerate(records):
        name = str(record.get("name") or "?")
        vendor = record.get("vendor_id")
        luid = "{:08X}:{:08X}".format(
            _safe_int(record.get("luid_high")), _safe_int(record.get("luid_low")),
        )
        lines.append(
            f"{index}: {name}  vendor={vendor}  luid={luid}  cuda={record.get('cuda_index')}"
        )
        if vendor == 4318 or "NVIDIA" in name.upper():
            nvidia_names.append(name)
        if vendor == 32902 or "Intel" in name:
            lines.append(f"判断: 存在核显 {name}；确认 DLSS 跑在 NVIDIA 上。")
    duplicates = [name for name, count in Counter(nvidia_names).items() if count > 1]
    if duplicates:
        lines.append(
            "判断: 检测到重复的 NVIDIA 适配器（"
            + "、".join(duplicates)
            + "），GPU 光流直连可能因 LUID 不一致回退。"
        )
    return lines


def _describe_edition(settings):
    from dlss5tool import mod_paths
    settings = dict(settings or {})
    raft_path = None
    worker_path = None
    try:
        probe_settings = {
            **settings,
            "guidance_mode": max(_safe_int(settings.get("guidance_mode"), 0), 1),
            "guidance_flow_backend": "raft",
        }
        candidates = mod_paths.guidance_candidates(probe_settings)
        raft_path = candidates.get("flow_weights")
        worker_path = candidates.get("worker")
    except Exception:
        pass
    raft_ok = bool(raft_path and os.path.isfile(raft_path))
    worker_ok = bool(worker_path and os.path.isfile(worker_path))
    build = "unknown"
    try:
        build = mod_paths.component_build(settings)
    except Exception:
        pass
    if raft_ok and worker_ok:
        edition = "完整版（含 RAFT 推理组件）"
    elif raft_ok:
        edition = "有 RAFT 权重，但推理 worker 不完整"
    else:
        edition = "轻量版（未发现 RAFT 权重）"
    torch_state = "未检测"
    try:
        import torch
        torch_state = "已安装 " + str(getattr(torch, "__version__", "?"))
        if torch.cuda.is_available():
            torch_state += f"；CUDA {getattr(getattr(torch, 'version', None), 'cuda', '?')}"
            try:
                torch_state += "；设备 " + torch.cuda.get_device_name(0)
            except Exception:
                pass
        else:
            torch_state += "；CUDA 不可用"
    except Exception as exception:
        torch_state = "不能 import：" + type(exception).__name__
    lines = [
        f"形态: {edition}",
        f"推理组件: {'有' if worker_ok else '无'}",
        f"RAFT 权重: {'有' if raft_ok else '无'}",
        f"组件构建: {build}",
        f"Torch: {torch_state}",
    ]
    if _safe_int(settings.get("guidance_mode")) and not raft_ok and settings.get("guidance_flow_backend", "raft") == "raft":
        lines.append("判断: 已开光流但没有 RAFT 权重，完整版/附加包未装或路径不对。")
    return lines


def _describe_toolchain():
    from dlss5tool.video_export import (
        find_ffmpeg, find_ffprobe, has_h264_nvenc, has_hevc_main10_nvenc,
    )
    lines = []
    ffmpeg = ffprobe = None
    try:
        ffmpeg = find_ffmpeg()
        lines.append("ffmpeg: " + _redact(ffmpeg))
    except Exception as exception:
        lines.append("ffmpeg: 未找到（" + str(exception).splitlines()[0] + "）")
    if ffmpeg:
        version = _command_output([ffmpeg, "-version"], timeout=8)
        first = (version.get("stdout") or "").splitlines()[:1]
        if first:
            lines.append(first[0])
        try:
            ffprobe = find_ffprobe(ffmpeg)
        except Exception:
            ffprobe = None
        lines.append("ffprobe: " + (_redact(ffprobe) if ffprobe else "未找到（插帧时间戳扫描需要）"))
        try:
            lines.append("h264_nvenc: " + ("可用" if has_h264_nvenc(ffmpeg) else "不可用"))
        except Exception as exception:
            lines.append("h264_nvenc: 探测失败 " + type(exception).__name__)
        try:
            lines.append("hevc_main10_nvenc: " + ("可用" if has_hevc_main10_nvenc(ffmpeg) else "不可用"))
        except Exception as exception:
            lines.append("hevc_main10_nvenc: 探测失败 " + type(exception).__name__)
    try:
        usage = shutil.disk_usage(os.getcwd())
        lines.append(
            f"工作盘剩余: {usage.free / 1024**3:.1f} GiB / 共 {usage.total / 1024**3:.1f} GiB"
        )
    except OSError as exception:
        lines.append("工作盘: " + repr(exception))
    return lines


def _effective_config(context):
    settings = dict((context or {}).get("settings") or {})
    export = dict((context or {}).get("export_settings") or {})
    media = dict((context or {}).get("media") or {})
    color = dict(media.get("color") or {})
    scale = _safe_int(export.get("super_resolution_scale") or settings.get("super_resolution_scale"), 1)
    if scale not in (1, 2, 4):
        scale = 1
    fg = _safe_int(export.get("frame_generation_multiplier"), 1)
    if fg not in (1, 2, 3, 4):
        fg = 1
    preview_sr = bool((context or {}).get("preview_super_resolution"))
    preview_fg = bool((context or {}).get("preview_frame_generation"))
    hdr_mode = bool(export.get("hdr_mode", True))
    is_hdr = bool(color.get("is_hdr"))
    width = _safe_int(media.get("width"))
    height = _safe_int(media.get("height"))
    fps = media.get("fps") or 0
    lines = []
    if scale > 1:
        lines.append(
            f"超分 {scale}×"
            + (f" → {width * scale}×{height * scale}" if width and height else "")
            + ("，预览开" if preview_sr else "，预览关（预览保持源分辨率，导出会放大）")
        )
    else:
        lines.append("超分关闭")
    if fg > 1:
        out_fps = f"{float(fps) * fg:g}fps" if fps else ""
        lines.append(
            f"插帧 {fg}×"
            + (f"，输出约 {out_fps}" if out_fps else "")
            + ("，预览开" if preview_fg else "，预览关")
        )
    else:
        lines.append("插帧关闭")
    if hdr_mode and is_hdr:
        lines.append("有效 HDR：开（" + str(color.get("label") or "HDR") + "）")
    elif hdr_mode and not is_hdr:
        lines.append("hdr_mode 开，但素材是 SDR，有效 HDR 关")
    else:
        lines.append("HDR 处理关")
    intensity = settings.get("intensity")
    try:
        if float(intensity) > 1:
            lines.append(f"强度 {intensity}（实验 5× 范围）")
    except (TypeError, ValueError):
        pass
    host = (context or {}).get("active_host") or {}
    lines.append(
        "宿主: "
        + str(settings.get("host_backend") or "auto")
        + " / "
        + str(settings.get("host_submission") or "")
        + f" / 设置队列 {settings.get('host_in_flight')}"
        + (
            f" / 实际 {host.get('backend')} in_flight={host.get('max_in_flight')}"
            if host else " / 当前无活动会话"
        )
    )
    mode = _safe_int(settings.get("guidance_mode"))
    if mode:
        lines.append(
            "引导: 模式 "
            + str(mode)
            + " / "
            + str(settings.get("guidance_flow_backend") or "raft")
        )
    return lines or ["（无）"]


def _sample_timestamps(source, limit=90, timeout=15):
    from dlss5tool.frame_generation import validate_timestamps
    from dlss5tool.video_export import find_ffmpeg, find_ffprobe, probe_video_stream
    ffmpeg = find_ffmpeg()
    probe = find_ffprobe(ffmpeg)
    if not probe:
        return ["时间戳抽检: 没有 ffprobe，已跳过"]
    meta = probe_video_stream(ffmpeg, source)
    try:
        rate = Fraction(str(meta.get("r_frame_rate") or meta.get("avg_frame_rate") or "0"))
        if rate <= 0:
            raise ValueError
    except (TypeError, ValueError, ZeroDivisionError):
        return ["时间戳抽检: 无法读取帧率"]
    proc = subprocess.Popen(
        [
            probe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=best_effort_timestamp_time",
            "-of", "csv=p=0", str(source),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )
    stamps = []
    deadline = time.monotonic() + timeout
    try:
        for raw in proc.stdout:
            if time.monotonic() > deadline or len(stamps) >= limit:
                break
            field = raw.decode("utf-8", "replace").strip().split(",")[0]
            if field:
                stamps.append(field)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
    if len(stamps) < 2:
        return [f"时间戳抽检: 只读到 {len(stamps)} 帧，不足判断"]
    try:
        validate_timestamps(stamps, rate)
        return [f"时间戳抽检: 前 {len(stamps)} 帧通过恒定帧率检查（{rate} fps）"]
    except ValueError as exception:
        return ["时间戳抽检失败: " + str(exception).splitlines()[0]]


def _precheck_media(context, media_path=None):
    media = dict((context or {}).get("media") or {})
    if not media:
        return ["当前没有导入素材。"]
    export = dict((context or {}).get("export_settings") or {})
    color = dict(media.get("color") or {})
    width = _safe_int(media.get("width"))
    height = _safe_int(media.get("height"))
    frames = _safe_int(media.get("frames") or color.get("frames") or color.get("nb_frames"))
    fps = media.get("fps") or color.get("fps") or 0
    try:
        fps = float(fps)
    except (TypeError, ValueError):
        fps = 0.0
    fg = _safe_int(export.get("frame_generation_multiplier"), 1)
    scale = _safe_int(export.get("super_resolution_scale"), 1)
    lines = [
        f"素材: {media.get('name') or ''}  {width}×{height}  {frames}f  {fps:g}fps  "
        + str(color.get("label") or ""),
    ]
    if frames >= 10000 and fg > 1:
        minutes = frames / max(fps, 1.0) / 60.0
        lines.append(
            f"判断: 约 {minutes:.0f} 分钟 / {frames} 帧，插帧导出前会整片扫描时间戳，期间进度可能很慢。"
        )
    r_rate = str(color.get("r_frame_rate") or "")
    avg_rate = str(color.get("avg_frame_rate") or "")
    if r_rate and avg_rate and r_rate != avg_rate and avg_rate not in {"0/0", "N/A", "unknown"}:
        lines.append(
            f"判断: r_frame_rate={r_rate} 与 avg_frame_rate={avg_rate} 不一致，插帧入口可能拒绝变帧率。"
        )
    if media_path and os.path.isfile(media_path):
        try:
            lines.extend(_sample_timestamps(media_path))
        except Exception as exception:
            lines.append("时间戳抽检异常: " + type(exception).__name__ + ": " + str(exception).splitlines()[0])
        try:
            size = os.path.getsize(media_path)
            need = size * max(fg, 1) * max(scale, 1) ** 2 * 3 + 15 * 1024 ** 3
            free = shutil.disk_usage(os.path.dirname(os.path.abspath(media_path)) or ".").free
            lines.append(
                f"插帧磁盘预估: 约需 {need / 1024**3:.1f} GiB，素材所在盘剩余 {free / 1024**3:.1f} GiB"
            )
            if free < need:
                lines.append("判断: 剩余空间低于插帧入口门槛（源×倍率×3 + 15 GiB）。")
        except OSError:
            pass
    return lines


def _describe_gpu_flow(context):
    from dlss5tool.gpu_flow import eligible
    settings = dict((context or {}).get("settings") or {})
    info = dict((context or {}).get("guidance_info") or {})
    lines = [
        "按当前设置会尝试直连: " + ("是" if eligible(settings) else "否"),
    ]
    if info.get("gpu_flow_transport"):
        lines.append("当前会话: 已启用 " + str(info.get("gpu_flow_transport")))
    elif info.get("gpu_flow_fallback_reason"):
        lines.append("当前会话已回退: " + str(info.get("gpu_flow_fallback_reason")))
    elif info.get("gpu_flow_attempted"):
        lines.append("当前会话尝试过直连，未记录原因")
    elif info.get("gpu_flow_capability"):
        lines.append("worker 能力: " + str(info.get("gpu_flow_capability")))
    elif not (context or {}).get("active_host"):
        lines.append("当前无活动宿主，未实际协商 GPU 光流")
    return lines


def _describe_preview(preview):
    if not preview:
        return ["（无预览状态）"]
    lines = [
        "视图: " + str(preview.get("view") or ""),
        "共享管线: " + ("开" if preview.get("shared_render") else "关"),
        "全画质已出帧: " + ("是" if preview.get("shared_ready") else "否"),
        "等待中: " + ("是" if preview.get("dlss_pending") else "否"),
    ]
    if preview.get("hold_original"):
        lines.append("按住原图: 是")
    source = preview.get("source_size") or []
    shown = preview.get("preview_size") or []
    if len(source) == 2 and source[0]:
        lines.append(f"源尺寸: {source[0]}×{source[1]}")
    if len(shown) == 2 and shown[0]:
        lines.append(f"预览处理尺寸: {shown[0]}×{shown[1]}")
    if preview.get("shared_render") and not preview.get("shared_ready"):
        lines.append("判断: 已进入超分/插帧预览，全画质帧尚未就绪。")
    return lines


def _probe_vsr():
    started = time.perf_counter()
    status = super_resolution.runtime_status()
    if not status["available"]:
        return {
            "ok": False,
            "skipped": True,
            "error": "缺少 " + "、".join(status["missing"]),
            "hints": ["未找到 VSR 组件，已跳过超分探针。"],
            "elapsed_seconds": 0,
        }
    try:
        frame = np.zeros((180, 320, 4), np.uint8)
        frame[..., 0] = 32
        frame[..., 1] = 64
        frame[..., 2] = 96
        frame[..., 3] = 255
        with super_resolution.ProcessSuperResolution(320, 180, 2, timeout=20) as session:
            output = session.process(frame)
        height, width = output.shape[:2]
        ok = (width, height) == (640, 360)
        return {
            "ok": ok,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "output_shape": list(output.shape),
            "hints": (
                ["VSR 2× 320×180 → 640×360 通过。"]
                if ok else
                [f"VSR 输出尺寸异常：{width}×{height}，期望 640×360。"]
            ),
        }
    except Exception as exception:
        return {
            "ok": False,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(exception).__name__,
            "error": _redact(str(exception)[:800]),
            "hints": ["VSR 探针失败：超分初始化或处理未通过。"],
        }


def _probe_dlssg():
    from dlss5tool.frame_generation import NativeStream, PINNED_RUNTIME, runtime_files
    worker, runtime = runtime_files()
    missing = [path.name for path in (worker, runtime) if not path.is_file()]
    if missing:
        return {
            "ok": False,
            "skipped": True,
            "error": "缺少 " + "、".join(missing),
            "hints": ["未找到插帧组件，已跳过握手探针。"],
        }
    log_dir = Path(tempfile.mkdtemp(prefix="dlss5-dlssg-probe-"))
    cancel = threading.Event()
    timer = threading.Timer(_FEATURE_PROBE_TIMEOUT, cancel.set)
    stream = None
    started = time.perf_counter()
    try:
        timer.start()
        stream = NativeStream(320, 180, 2, False, log_dir, cancel)
        pinned = stream.runtime_hash == PINNED_RUNTIME
        return {
            "ok": True,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "luid": stream.luid,
            "runtime_pinned": pinned,
            "hints": [
                "插帧工作进程握手成功。",
                "运行库哈希与 3×/4× 钉扎一致。"
                if pinned else
                "运行库哈希与 3×/4× 钉扎不一致；2× 仍可尝试。",
            ],
        }
    except Exception as exception:
        log = ""
        try:
            log = (log_dir / "native.log").read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            pass
        return {
            "ok": False,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(exception).__name__,
            "error": _redact(str(exception)[:800]),
            "native_log": _redact(log),
            "hints": ["插帧组件未能完成握手。"],
        }
    finally:
        timer.cancel()
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        shutil.rmtree(log_dir, ignore_errors=True)


def _format_probe_block(title, probe):
    probe = probe or {}
    outcome = "SKIP" if probe.get("skipped") else "PASS" if probe.get("ok") else "FAIL"
    lines = ["", f"[{title}]", f"结果: {outcome}"]
    for key in (
        "elapsed_seconds", "output_shape", "luid", "runtime_pinned",
        "error_type", "error",
    ):
        if probe.get(key) not in (None, ""):
            lines.append(f"{key}: {probe[key]}")
    if probe.get("hints"):
        lines.append("判断:")
        for hint in probe["hints"]:
            lines.append("- " + hint)
    if probe.get("native_log"):
        lines.append("原生日志:")
        lines.append(str(probe["native_log"]))
    return lines


def _render_report(context, files, gpu, probes, exports=None, extras=None):
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    extras = extras or {}
    lines = [
        "DLSS5Tool 一键诊断报告",
        "=" * 72,
        f"报告格式: {_REPORT_SCHEMA}",
        f"生成时间: {generated}",
        f"应用版本: {APP_VERSION}",
        f"运行模式: {'PyInstaller 便携版' if getattr(sys, 'frozen', False) else 'Python 源码'}",
        f"系统: {platform.platform()}",
        f"系统版本: {platform.version()}",
        f"架构: {platform.machine()}",
        f"Python: {platform.python_version()}",
        f"程序路径: {_redact(sys.executable)}",
        f"工作目录: {_redact(os.getcwd())}",
        f"会话: {os.environ.get('SESSIONNAME', 'unknown')}",
        "",
        "[GPU / 驱动]",
    ]
    if gpu.get("stdout"):
        lines.extend(gpu["stdout"].splitlines())
    elif gpu.get("error"):
        lines.append("nvidia-smi 无法运行: " + gpu["error"])
    else:
        lines.append("nvidia-smi 未返回 GPU 信息")
        if gpu.get("stderr"):
            lines.append(gpu["stderr"])

    lines.extend(["", "[适配器]"])
    lines.extend(extras.get("adapters") or ["（无）"])

    lines.extend(["", "[运行文件]"])
    for label, details in files.items():
        lines.append(f"{label}:")
        for key, value in details.items():
            lines.append(f"  {key}: {value}")

    lines.extend(["", "[发行与推理]"])
    lines.extend(extras.get("edition") or ["（无）"])
    lines.extend(["", "[工具链]"])
    lines.extend(extras.get("toolchain") or ["（无）"])
    lines.extend(["", "[有效配置]"])
    lines.extend(extras.get("effective") or ["（无）"])
    lines.extend(["", "[当前素材预检]"])
    lines.extend(extras.get("media_check") or ["（无）"])
    lines.extend(["", "[当前预览]"])
    lines.extend(extras.get("preview") or ["（无）"])
    lines.extend(["", "[GPU 光流]"])
    lines.extend(extras.get("gpu_flow") or ["（无）"])

    lines.extend(["", "[当前上下文]"])
    clean_context = dict(context or {})
    ui_log = clean_context.pop("ui_log", "")
    clean_context.pop("queue_jobs", None)
    clean_context.pop("recent_exports", None)
    clean_context.pop("media_path", None)
    clean_context.pop("preview", None)
    clean_context.pop("guidance_info", None)
    lines.append(_format_mapping(clean_context))

    lines.extend(["", "[最近导出]"])
    lines.extend(_format_export_records(exports or []))

    for probe in probes:
        backend = probe.get("backend_requested", "unknown")
        outcome = "SKIP" if probe.get("skipped") else "PASS" if probe.get("ok") else "FAIL"
        lines.extend(["", f"[宿主探针: {backend}]", f"结果: {outcome}"])
        for key in (
            "backend_actual", "elapsed_seconds", "wall_seconds", "output_shape",
            "output_sha256", "adapter_info", "error_type", "error", "process_returncode",
            "timed_out", "launch_error", "traceback", "worker_stdout", "worker_stderr",
        ):
            if key in probe and probe[key] not in (None, ""):
                lines.append(f"{key}: {probe[key]}")
        lines.append("判断:")
        for hint in _probe_hints(probe):
            lines.append("- " + hint)
        lines.append("原生 NGX 日志:")
        lines.append(probe.get("native_log") or "（未生成）")

    lines.extend(_format_probe_block("超分探针", extras.get("vsr")))
    lines.extend(_format_probe_block("插帧探针", extras.get("dlssg")))

    lines.extend(["", "[界面关键日志]"])
    filtered = filter_ui_log(ui_log)
    lines.extend(filtered or ["（无）"])
    lines.extend([
        "",
        "[隐私说明]",
        "用户主目录已替换为 %USERPROFILE%，媒体路径已缩减为 %MEDIA_FILE%\\文件名；"
        "报告不包含视频画面。最近导出只含文件名、计划/实际尺寸和结果状态。",
        "",
    ])
    return "\n".join(lines)


def write_diagnostic_report(output_path, context=None):
    """Run disposable backend probes and atomically write one shareable log."""
    output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(output_path) or os.getcwd()
    if not os.path.isdir(output_dir):
        raise FileNotFoundError("诊断报告目录不存在: " + output_dir)
    settings = dict((context or {}).get("settings") or {})
    try:
        selected_runtime = mod_paths.runtime_path(settings)
    except FileNotFoundError:
        selected_runtime = settings.get('dlss_runtime') or dlss_engine.DLSSNR_DLL
    files = {
        "nvngx_dlssnr.dll": describe_file(selected_runtime),
        "dlssnr_host_v2.dll": describe_file(dlss_engine.HOST_DLL_V2),
        "dlssnr_host.dll": describe_file(dlss_engine.HOST_DLL_LEGACY),
        "nvngx_vsr.dll": describe_file(super_resolution.VSR_RUNTIME_DLL),
        "vsr_host.dll": describe_file(super_resolution.VSR_HOST_DLL),
    }
    from dlss5tool.frame_generation import runtime_files
    dlssg_worker, dlssg_runtime = runtime_files()
    files["dlssg_video_worker.exe"] = describe_file(str(dlssg_worker))
    files["nvngx_dlssg.dll"] = describe_file(str(dlssg_runtime))
    context = dict(context or {})
    media_path = context.get("media_path")
    extras = {}
    for key, factory in (
        ("adapters", _describe_adapters),
        ("edition", lambda: _describe_edition(settings)),
        ("toolchain", _describe_toolchain),
        ("effective", lambda: _effective_config(context)),
        ("media_check", lambda: _precheck_media(context, media_path)),
        ("preview", lambda: _describe_preview(context.get("preview"))),
        ("gpu_flow", lambda: _describe_gpu_flow(context)),
        ("vsr", _probe_vsr),
        ("dlssg", _probe_dlssg),
    ):
        try:
            extras[key] = factory()
        except Exception as exception:
            extras[key] = (
                {"ok": False, "error": _redact(str(exception)[:800]),
                 "hints": [key + " 收集失败: " + type(exception).__name__]}
                if key in {"vsr", "dlssg"} else
                [key + " 收集失败: " + type(exception).__name__ + ": " + str(exception).splitlines()[0]]
            )
    gpu = _command_output([
        "nvidia-smi",
        "--query-gpu=name,driver_version,pci.bus_id,memory.total",
        "--format=csv,noheader",
    ])
    probes = []
    with tempfile.TemporaryDirectory(prefix="dlss5tool-diagnostic-") as temp_dir:
        settings_path = os.path.join(temp_dir, "settings.json")
        with open(settings_path, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
        for backend, host_path in (
            ("v2", dlss_engine.HOST_DLL_V2),
            ("legacy", dlss_engine.HOST_DLL_LEGACY),
        ):
            if os.path.isfile(host_path):
                probes.append(_probe_backend(temp_dir, settings_path, backend))
            else:
                probes.append({
                    "backend_requested": backend,
                    "ok": False,
                    "skipped": True,
                    "error": "宿主 DLL 不存在，已跳过",
                    "native_log": "",
                    "timed_out": False,
                })
    try:
        exports = collect_recent_exports(
            extra=context.get("recent_exports"),
            queue_jobs=context.get("queue_jobs"),
        )
    except Exception:
        exports = []
    report = _render_report(context, files, gpu, probes, exports, extras)
    temp_path = output_path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(report)
        os.replace(temp_path, output_path)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return {
        "path": output_path,
        "passed": sum(bool(probe.get("ok")) for probe in probes),
        "total": sum(not probe.get("skipped") for probe in probes),
    }


def diagnostic_worker_main(arguments):
    """Run one host in a disposable process and publish JSON before process exit."""
    if len(arguments) != 4:
        return 2
    result_path, native_log_path, settings_path, backend = arguments
    started = time.perf_counter()
    payload = {"backend_requested": backend, "ok": False}
    live = None
    try:
        settings = _read_json(settings_path)
        settings.update({
            "host_backend": backend,
            "host_auto_fallback": False,
            "frame_format": "rgba8",
            "color_profile": "srgb",
        })
        if backend == "legacy":
            # Legacy has no adapter-selection ABI; probe its historical auto path
            # independently instead of misreporting an explicit-v2 choice as a failure.
            settings["render_gpu"] = "auto"
        dlss_engine.LOG_PATH = os.path.abspath(native_log_path)
        frame = np.zeros((360, 640, 4), dtype=np.uint8)
        frame[..., 0] = np.arange(640, dtype=np.uint16)[None, :] % 256
        frame[..., 1] = np.arange(360, dtype=np.uint16)[:, None] % 256
        frame[..., 2] = 96
        frame[..., 3] = 255
        live = dlss_engine.Live(640, 360, settings)
        output = live.process(frame, reset=True)
        if output is None:
            raise RuntimeError("Feature 18 单帧处理没有返回输出")
        payload.update({
            "ok": True,
            "backend_actual": live.backend,
            "adapter_info": dict(getattr(live, "adapter_info", {})),
            "output_shape": list(output.shape),
            "output_sha256": hashlib.sha256(memoryview(output).cast("B")).hexdigest().upper(),
        })
        # This worker is disposable. Avoid dlssnr_shutdown hangs seen on some
        # driver/runtime combinations and let process exit reclaim the D3D12 state.
    except BaseException as exception:
        payload.update({
            "error_type": type(exception).__name__,
            "error": _redact(str(exception) or repr(exception)),
            "traceback": _redact(traceback.format_exc()[-6000:]),
        })
    finally:
        # Close optional external models explicitly; still avoid native NGX
        # shutdown. This also prevents a model child outliving diagnostic output.
        if live is not None and hasattr(live, 'close_guidance'):
            live.close_guidance()
    payload["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    try:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        return 3
    return 0 if payload.get("ok") else 1
