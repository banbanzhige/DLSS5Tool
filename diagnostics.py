#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shareable, one-click diagnostics for the packaged and source applications."""

import ctypes
from datetime import datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import traceback

import numpy as np

from app_version import APP_VERSION
import dlss_engine


_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROBE_TIMEOUT_SECONDS = 45
_REPORT_SCHEMA = 1
_KNOWN_RUNTIMES = {
    "CEB6432F6FBDF44D886014BCD47241932BF8B67439FEEF9BBDD0961436662650":
        "RTX 40 项目默认核心 / 310.8.0.0",
    "6EB209E764F39872625DEBD6ABAF45E2BB6322F6F270F781F70C059AE30B3927":
        "RTX 30 兼容核心 / 310.8.SF-v2",
    "E16BCF15E16E13F527491CDF7845B2FE6521A738D8F7C9C721866A8496E1FC8E":
        "RTX 50 NVIDIA 签名核心 / 310.8.0.0",
}
_UI_LOG_MARKERS = (
    "dlss", "ngx", "d3d12", "主机", "后端", "失败", "错误", "异常",
    "hdr", "编码器", "性能", "色彩检测", "preview",
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
        if os.path.normcase(absolute) == os.path.normcase(dlss_engine.DLSSNR_DLL):
            result["runtime_profile"] = _KNOWN_RUNTIMES.get(
                result["sha256"], "未知/自定义运行时",
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
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui.py")
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
    if "load nvngx_dlssnr.dll failed" in combined:
        hints.append("运行时加载失败；检查实际替换路径、文件完整性和安全软件。")
    if "missing runtime exports" in combined:
        hints.append("DLL 缺少必需导出，可能拿错或损坏。")
    if "caller/static initialization failed" in combined:
        hints.append("NGX 静态初始化或调用者检查失败；核对驱动、GPU 与 DLL 代际。")
    if re.search(r"(?:Init_with_ProjectID|runtime Init_Ext).*0xBAD", combined, re.I):
        hints.append("NGX 初始化返回 BAD 错误；通常与驱动、默认适配器或硬件支持有关。")
    if re.search(r"CreateFeature\(18\).*0xBAD00001", combined, re.I):
        hints.append("Feature 18 返回 FeatureNotSupported；优先核对 DLL 与 RTX 代际是否匹配。")
    if "Feature 18 ready" in combined and probe.get("ok"):
        hints.append("Feature 18 初始化和单帧处理通过。")
    if probe.get("ok") and not hints:
        hints.append("宿主初始化和单帧处理通过。")
    if not hints and not probe.get("ok"):
        hints.append("未匹配已知错误，请结合异常与原生日志继续分析。")
    return hints


def _format_mapping(mapping, indent=""):
    return json.dumps(mapping, ensure_ascii=False, indent=2).replace("\n", "\n" + indent)


def _render_report(context, files, gpu, probes):
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
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

    lines.extend(["", "[运行文件]"])
    for label, details in files.items():
        lines.append(f"{label}:")
        for key, value in details.items():
            lines.append(f"  {key}: {value}")

    lines.extend(["", "[当前上下文]"])
    clean_context = dict(context or {})
    ui_log = clean_context.pop("ui_log", "")
    lines.append(_format_mapping(clean_context))

    for probe in probes:
        backend = probe.get("backend_requested", "unknown")
        outcome = "SKIP" if probe.get("skipped") else "PASS" if probe.get("ok") else "FAIL"
        lines.extend(["", f"[宿主探针: {backend}]", f"结果: {outcome}"])
        for key in (
            "backend_actual", "elapsed_seconds", "wall_seconds", "output_shape",
            "output_sha256", "error_type", "error", "process_returncode",
            "timed_out", "launch_error", "traceback", "worker_stdout", "worker_stderr",
        ):
            if key in probe and probe[key] not in (None, ""):
                lines.append(f"{key}: {probe[key]}")
        lines.append("判断:")
        for hint in _probe_hints(probe):
            lines.append("- " + hint)
        lines.append("原生 NGX 日志:")
        lines.append(probe.get("native_log") or "（未生成）")

    lines.extend(["", "[界面关键日志]"])
    filtered = filter_ui_log(ui_log)
    lines.extend(filtered or ["（无）"])
    lines.extend([
        "",
        "[隐私说明]",
        "用户主目录已替换为 %USERPROFILE%，媒体路径已缩减为 %MEDIA_FILE%\\文件名；"
        "报告不包含视频画面或队列内容。",
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
    files = {
        "nvngx_dlssnr.dll": describe_file(dlss_engine.DLSSNR_DLL),
        "dlssnr_host_v2.dll": describe_file(dlss_engine.HOST_DLL_V2),
        "dlssnr_host.dll": describe_file(dlss_engine.HOST_DLL_LEGACY),
    }
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
    report = _render_report(context or {}, files, gpu, probes)
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
    try:
        settings = _read_json(settings_path)
        settings.update({
            "host_backend": backend,
            "host_auto_fallback": False,
            "frame_format": "rgba8",
            "color_profile": "srgb",
        })
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
    payload["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    try:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        return 3
    return 0 if payload.get("ok") else 1
