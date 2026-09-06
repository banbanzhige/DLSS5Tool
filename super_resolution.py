#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Crash-isolated RTX Video Super Resolution bridge and resource estimates."""

import ctypes
import multiprocessing
from multiprocessing import shared_memory
import os
import subprocess
import sys
import tempfile
import time
import uuid

import numpy as np


BASE = (
    os.path.abspath(getattr(sys, "_MEIPASS"))
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None)
    else os.path.dirname(os.path.abspath(__file__))
)
VSR_HOST_DLL = os.path.join(BASE, "vsr_host.dll")
VSR_RUNTIME_DLL = os.path.join(BASE, "nvngx_vsr.dll")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MIB = 1024 * 1024
_GIB = 1024 * _MIB
_GPU_QUERY_CACHE = None


class SuperResolutionError(RuntimeError):
    """RTX Video Super Resolution could not initialize or process a frame."""


def normalize_scale(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 1
    return value if value in (1, 2, 4) else 1


def target_size(width, height, scale):
    width, height = int(width), int(height)
    scale = normalize_scale(scale)
    if width <= 0 or height <= 0:
        return 0, 0
    return width * scale, height * scale


def operation_timeout(output_width, output_height, minimum=60.0):
    target_pixels = max(int(output_width), 0) * max(int(output_height), 0)
    resolution_timeout = min(
        600.0, 30.0 + target_pixels / float(1920 * 1080) * 15.0,
    )
    return max(float(minimum), resolution_timeout, 5.0)


def runtime_status():
    missing = []
    if not os.path.isfile(VSR_HOST_DLL):
        missing.append(os.path.basename(VSR_HOST_DLL))
    if not os.path.isfile(VSR_RUNTIME_DLL):
        missing.append(os.path.basename(VSR_RUNTIME_DLL))
    return {
        "available": not missing,
        "missing": missing,
        "host": VSR_HOST_DLL,
        "runtime": VSR_RUNTIME_DLL,
    }


def query_gpu_memory(cache_seconds=5.0):
    """Return total/free VRAM in bytes using the driver tool, or None."""
    global _GPU_QUERY_CACHE
    now = time.monotonic()
    if _GPU_QUERY_CACHE and now - _GPU_QUERY_CACHE[0] <= max(float(cache_seconds), 0.0):
        return dict(_GPU_QUERY_CACHE[1])
    try:
        result = subprocess.run(
            [
                "nvidia-smi", "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, creationflags=_CREATE_NO_WINDOW, check=False,
        )
        line = result.stdout.decode("utf-8", errors="replace").splitlines()[0]
        name, total_mib, free_mib = [part.strip() for part in line.split(",", 2)]
        info = {
            "name": name,
            "total_bytes": int(float(total_mib)) * _MIB,
            "free_bytes": int(float(free_mib)) * _MIB,
        }
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    _GPU_QUERY_CACHE = (now, info)
    return dict(info)


def estimate_resources(width, height, scale, is_hdr=False):
    """Estimate known buffers; model/driver workspaces remain runtime-dependent."""
    width, height = int(width), int(height)
    scale = normalize_scale(scale)
    output_width, output_height = target_size(width, height, scale)
    source_pixels = max(width * height, 0)
    output_pixels = max(output_width * output_height, 0)
    frame_bpp = 8 if is_hdr else 4
    in_flight = 1 if output_pixels > 3840 * 2160 else 2

    # VSR owns one packed RGB input/output texture. DLSS owns color/output per
    # slot plus one zero-motion and one zero-depth target-resolution texture.
    vsr_gpu = (source_pixels + output_pixels) * 4
    dlss_gpu = output_pixels * (2 * in_flight * frame_bpp + 8)
    known_gpu = vsr_gpu + dlss_gpu

    # Shared VSR input/output + its upload/readback + DLSS shared input/output,
    # plus two application-side target frames used for compose/write overlap.
    shared = (source_pixels + output_pixels) * frame_bpp
    staging = (source_pixels + output_pixels) * 4
    dlss_shared = output_pixels * frame_bpp * 2
    application_frames = output_pixels * frame_bpp * 2
    known_ram = shared + staging + dlss_shared + application_frames
    recommended_gpu = int(known_gpu * 1.35 + 1536 * _MIB)
    recommended_ram = int(known_ram * 1.20 + 512 * _MIB)
    return {
        "input_width": width,
        "input_height": height,
        "output_width": output_width,
        "output_height": output_height,
        "scale": scale,
        "is_hdr": bool(is_hdr),
        "in_flight": in_flight,
        "single_frame_bytes": output_pixels * frame_bpp,
        "known_gpu_bytes": known_gpu,
        "recommended_gpu_bytes": recommended_gpu,
        "known_ram_bytes": known_ram,
        "recommended_ram_bytes": recommended_ram,
    }


def classify_resource_risk(estimate, gpu_memory=None):
    gpu_memory = gpu_memory if gpu_memory is not None else query_gpu_memory()
    if estimate["output_width"] > 8192 or estimate["output_height"] > 8192:
        return "extreme"
    if not gpu_memory or gpu_memory.get("free_bytes", 0) <= 0:
        return "unknown"
    ratio = estimate["recommended_gpu_bytes"] / float(gpu_memory["free_bytes"])
    if ratio >= 0.90:
        return "high"
    if ratio >= 0.70:
        return "medium"
    if estimate["output_width"] * estimate["output_height"] > 3840 * 2160:
        return "medium"
    return "low"


def format_bytes(value):
    value = max(float(value), 0.0)
    if value >= _GIB:
        return f"{value / _GIB:.2f} GiB"
    return f"{value / _MIB:.0f} MiB"


def format_resource_hint(estimate, gpu_memory=None):
    gpu_memory = gpu_memory if gpu_memory is not None else query_gpu_memory()
    risk = classify_resource_risk(estimate, gpu_memory)
    labels = {
        "low": "较低", "medium": "中等", "high": "高",
        "extreme": "极高", "unknown": "未知",
    }
    text = (
        f"{estimate['scale']}× → {estimate['output_width']}×{estimate['output_height']}；"
        f"单帧 {format_bytes(estimate['single_frame_bytes'])}；"
        f"已知显存下限 {format_bytes(estimate['known_gpu_bytes'])}，"
        f"建议空闲 {format_bytes(estimate['recommended_gpu_bytes'])}；"
        f"系统内存约 {format_bytes(estimate['recommended_ram_bytes'])}；风险{labels[risk]}"
    )
    if gpu_memory:
        text += f"（当前空闲 {format_bytes(gpu_memory['free_bytes'])}）"
    return text


def _load_host():
    status = runtime_status()
    if not status["available"]:
        raise SuperResolutionError("缺少 RTX 视频超分组件：" + "、".join(status["missing"]))
    dll_directory = None
    if hasattr(os, "add_dll_directory"):
        dll_directory = os.add_dll_directory(BASE)
    try:
        library = ctypes.WinDLL(VSR_HOST_DLL)
    except OSError as exception:
        if dll_directory is not None:
            dll_directory.close()
        raise SuperResolutionError(f"无法加载 RTX 视频超分宿主：{exception}") from exception
    library.vsr_init.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_wchar_p, ctypes.c_wchar_p,
    ]
    library.vsr_init.restype = ctypes.c_int
    library.vsr_process.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.vsr_process.restype = ctypes.c_int
    library.vsr_shutdown.argtypes = []
    library.vsr_shutdown.restype = None
    library._dll_directory_handle = dll_directory
    return library


def _worker_main(connection, width, height, scale, is_hdr, input_name, output_name, log_path):
    input_memory = output_memory = None
    library = None
    try:
        dtype = np.float16 if is_hdr else np.uint8
        output_width, output_height = target_size(width, height, scale)
        input_memory = shared_memory.SharedMemory(name=input_name)
        output_memory = shared_memory.SharedMemory(name=output_name)
        input_frame = np.ndarray((height, width, 4), dtype=dtype, buffer=input_memory.buf)
        output_frame = np.ndarray(
            (output_height, output_width, 4), dtype=dtype, buffer=output_memory.buf,
        )
        library = _load_host()
        initialized = library.vsr_init(
            width, height, scale, 4, 1 if is_hdr else 0, BASE, log_path,
        )
        if not initialized:
            raise SuperResolutionError("RTX Video Super Resolution 初始化失败，请查看超分日志")
        connection.send({"ok": True, "operation": "initialize"})
        while True:
            message = connection.recv()
            operation = message.get("operation")
            if operation == "close":
                connection.send({"ok": True, "operation": "close"})
                break
            if operation != "process":
                raise ValueError(f"未知超分命令：{operation!r}")
            ok = library.vsr_process(
                input_frame.ctypes.data_as(ctypes.c_void_p),
                output_frame.ctypes.data_as(ctypes.c_void_p),
            )
            if not ok:
                raise SuperResolutionError("RTX Video Super Resolution 帧处理失败")
            connection.send({"ok": True, "operation": "process"})
    except EOFError:
        pass
    except BaseException as exception:
        try:
            connection.send({"ok": False, "error": f"{type(exception).__name__}: {exception}"})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if library is not None:
            try:
                library.vsr_shutdown()
            except Exception:
                pass
        for memory in (input_memory, output_memory):
            if memory is not None:
                try:
                    memory.close()
                except (BufferError, OSError):
                    pass
        try:
            connection.close()
        except OSError:
            pass


class ProcessSuperResolution:
    """One disposable VSR process with fixed source and target shared buffers."""

    def __init__(self, width, height, scale, is_hdr=False, timeout=60.0):
        self.width = int(width)
        self.height = int(height)
        self.scale = normalize_scale(scale)
        self.is_hdr = bool(is_hdr)
        if self.scale == 1:
            raise ValueError("超分会话只接受 2× 或 4×")
        self.output_width, self.output_height = target_size(width, height, scale)
        self.dtype = np.float16 if self.is_hdr else np.uint8
        self.timeout = operation_timeout(
            self.output_width, self.output_height, minimum=timeout,
        )
        self.log_path = os.path.join(
            tempfile.gettempdir(), f"dlss5tool-vsr-{uuid.uuid4().hex}.log",
        )
        self._closed = False
        item_size = np.dtype(self.dtype).itemsize
        input_bytes = self.width * self.height * 4 * item_size
        output_bytes = self.output_width * self.output_height * 4 * item_size
        self._input_memory = shared_memory.SharedMemory(create=True, size=input_bytes)
        self._output_memory = shared_memory.SharedMemory(create=True, size=output_bytes)
        self._input_frame = np.ndarray(
            (self.height, self.width, 4), dtype=self.dtype, buffer=self._input_memory.buf,
        )
        self._output_frame = np.ndarray(
            (self.output_height, self.output_width, 4), dtype=self.dtype,
            buffer=self._output_memory.buf,
        )
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self._connection = parent
        self._process = context.Process(
            target=_worker_main,
            args=(
                child, self.width, self.height, self.scale, self.is_hdr,
                self._input_memory.name, self._output_memory.name, self.log_path,
            ),
            name="rtx-vsr-host", daemon=True,
        )
        try:
            self._process.start()
            child.close()
            self._receive("initialize")
        except BaseException:
            self.close()
            raise

    def _log_tail(self):
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as handle:
                return handle.read()[-3000:].strip()
        except OSError:
            return ""

    def _receive(self, operation):
        if not self._connection.poll(self.timeout):
            if self._process is not None and not self._process.is_alive():
                detail = f"超分子进程异常退出（代码 {self._process.exitcode}）"
            else:
                detail = f"超分子进程在 {operation} 时超过 {self.timeout:.0f} 秒未响应"
            tail = self._log_tail()
            if tail:
                detail += "\n超分日志末尾：\n" + tail
            raise SuperResolutionError(detail)
        try:
            response = self._connection.recv()
        except (EOFError, OSError) as exception:
            raise SuperResolutionError(f"超分子进程在 {operation} 时断开：{exception}") from exception
        if not response.get("ok"):
            detail = response.get("error") or "超分子进程返回未知错误"
            tail = self._log_tail()
            if tail:
                detail += "\n超分日志末尾：\n" + tail
            raise SuperResolutionError(detail)
        return response

    def process(self, rgba):
        if self._closed:
            raise SuperResolutionError("超分会话已关闭")
        expected = (self.height, self.width, 4)
        if rgba.shape != expected or rgba.dtype != self.dtype:
            raise ValueError(
                f"超分输入必须是 {np.dtype(self.dtype).name} {expected}，实际为 {rgba.dtype} {rgba.shape}"
            )
        np.copyto(self._input_frame, rgba)
        self._connection.send({"operation": "process"})
        self._receive("process")
        return self._output_frame.copy()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._process is not None and self._process.is_alive():
                self._connection.send({"operation": "close"})
                if self._connection.poll(2.0):
                    self._connection.recv()
                self._process.join(timeout=3.0)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(timeout=2.0)
        except (BrokenPipeError, EOFError, OSError):
            pass
        try:
            self._connection.close()
        except (AttributeError, OSError):
            pass
        for memory in (getattr(self, "_input_memory", None), getattr(self, "_output_memory", None)):
            if memory is not None:
                try:
                    memory.close()
                except (BufferError, OSError):
                    pass
                try:
                    memory.unlink()
                except (FileNotFoundError, OSError):
                    pass
        try:
            os.remove(self.log_path)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
