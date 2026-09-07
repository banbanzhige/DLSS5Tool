#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py — 简约 DLSS5 实时预览 + 导出 (test4)

功能：导入视频或图片 → 实时预览(原图/DLSS/对比) → 调风格/强度/本地色调整/本地结构
      → 逐帧实时看出效果 → 导出 DLSS 视频或图片。

零引导（Feature 18 神经渲染忽略光流/深度），无需 torch/模型，只需 NVIDIA 显卡。
运行： python gui.py
"""
import ctypes
import math
import os
import multiprocessing
import queue
import re
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk, filedialog, messagebox, scrolledtext

import cv2
import numpy as np

import app_settings
from app_version import APP_VERSION
import diagnostics
import dlss_engine
import export_queue as export_queue_state
import updater
from dlss_host_process import ProcessLive
from parallel_export import export_parallel
from preview_audio import PreviewAudio, ms_to_frame
from super_resolution import (
    ProcessSuperResolution, classify_resource_risk, estimate_resources,
    format_bytes, format_resource_hint, normalize_scale, query_gpu_memory,
    runtime_status as super_resolution_runtime_status, target_size as super_resolution_target_size,
)
from video_export import (
    FFmpegHDRVideoReader, FFmpegVideoWriter, compose_hdr_frame,
    compose_output_frame, find_ffmpeg, output_container_extension,
    probe_video_stream, resolve_output_container, tone_map_hdr_preview,
)
import ui_theme
from ui_widgets import (
    AccentSlider, CheckToggle, ChipGroup, ChromeButton, ChromeCombobox,
    ChromeEntry, ChromeSpinbox, CollapsibleSection, ProgressRule, SegmentedBar,
    StatusPills, StudioNotebook, TimelineBar, Tooltip, round_rect,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

VIEWS = ["原图", "DLSS", "对比"]
STYLE_CHOICES = {"默认": 0, "自然": 1, "电影": 2}
OUTVIEW_CHOICES = {"处理": 0, "差异×10": 1, "左右对比": 2}
STYLE_NAMES = {value: name for name, value in STYLE_CHOICES.items()}
OUTVIEW_NAMES = {value: name for name, value in OUTVIEW_CHOICES.items()}
EXPORT_MODE_CHOICES = {"严格时序（单会话）": "single", "视觉无损（并行分段）": "parallel"}
EXPORT_MODE_NAMES = {value: name for name, value in EXPORT_MODE_CHOICES.items()}
NVENC_PRESET_CHOICES = {
    "p1 最快": "p1", "p3 快速": "p3", "p5 较慢（推荐）": "p5", "p7 最慢": "p7",
}
NVENC_PRESET_NAMES = {value: name for name, value in NVENC_PRESET_CHOICES.items()}
OUTPUT_CONTAINER_CHOICES = {
    "MP4（推荐）": "mp4",
    "MKV": "mkv",
    "MOV": "mov",
    "跟随输入": "source",
}
OUTPUT_CONTAINER_NAMES = {
    value: name for name, value in OUTPUT_CONTAINER_CHOICES.items()
}
OUTPUT_CONTAINER_LABELS = {"mp4": "MP4", "mkv": "MKV", "mov": "MOV"}
OUTPUT_RESOLUTION_CHOICES = {
    "跟随源视频（推荐）": "source",
    "2160p": "2160p",
    "1440p": "1440p",
    "1080p": "1080p",
    "720p": "720p",
    "自定义上限": "custom",
}
OUTPUT_RESOLUTION_NAMES = {
    value: name for name, value in OUTPUT_RESOLUTION_CHOICES.items()
}
OUTPUT_RESOLUTION_MAX_EDGES = {
    "2160p": 3840, "1440p": 2560, "1080p": 1920, "720p": 1280,
}
SUPER_RESOLUTION_CHOICES = {"关闭": 1, "2×": 2, "4×": 4}
SUPER_RESOLUTION_NAMES = {value: name for name, value in SUPER_RESOLUTION_CHOICES.items()}
RATE_CONTROL_CHOICES = {
    "按画质（推荐）": "quality",
    "目标码率": "bitrate",
}
RATE_CONTROL_NAMES = {value: name for name, value in RATE_CONTROL_CHOICES.items()}
QUALITY_PROFILE_CHOICES = {
    "极高质量": "maximum",
    "高质量（推荐）": "high",
    "均衡": "balanced",
    "小体积": "compact",
}
QUALITY_PROFILE_NAMES = {value: name for name, value in QUALITY_PROFILE_CHOICES.items()}
HOST_BACKEND_CHOICES = {
    "自动（优先 v2）": "auto", "v2 优化主机": "v2", "旧版兼容主机": "legacy",
}
HOST_BACKEND_NAMES = {value: name for name, value in HOST_BACKEND_CHOICES.items()}
HOST_SUBMISSION_CHOICES = {"合并提交（快速）": "merged", "兼容提交（保守）": "compatibility"}
HOST_SUBMISSION_NAMES = {value: name for name, value in HOST_SUBMISSION_CHOICES.items()}
PREVIEW_QUALITY_CHOICES = {
    "自动（推荐）": "auto",
    "1080p": "1080p",
    "1440p": "1440p",
    "原始分辨率": "original",
}
PREVIEW_QUALITY_NAMES = {value: name for name, value in PREVIEW_QUALITY_CHOICES.items()}
PREVIEW_MAX_EDGES = {"1080p": 1920, "1440p": 2560}
PREVIEW_QUEUE_SIZE = 3
PREVIEW_BUFFER_SECONDS = 1.0
PREVIEW_BACKGROUND_TICK_MS = 16
PREVIEW_INTERACTION_IDLE_MS = 180
PREVIEW_WORKER_POLL_MS = 20
LARGE_IMAGE_TILE_THRESHOLD_PIXELS = 45_000_000
LARGE_IMAGE_TILE_WIDTH = 6000
LARGE_IMAGE_TILE_HEIGHT = 3000
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_FILETYPES = [
    ("媒体", "*.mp4 *.avi *.mov *.mkv *.m4v *.webm *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
    ("视频", "*.mp4 *.avi *.mov *.mkv *.m4v *.webm"),
    ("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
    ("所有文件", "*.*"),
]
QUEUE_STATE_NAMES = {
    "pending": "等待",
    "running": "处理中",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "被中断",
}
QUEUE_STARTABLE_STATES = {"pending", "cancelled", "interrupted"}
IMAGE_ENCODE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
CANVAS_BG = "#161616"
CANVAS_DROP_BG = "#24405C"
HUD_FILL = "#d8d8d8"
SPLIT_LINE = "#7FE8E8"
TIMELINE_BG = "#1a1a1a"
TIMELINE_TRACK = "#3a3a3a"
TIMELINE_FILL = "#3d7ea6"
TIMELINE_THUMB = "#e6e6e6"
TIMELINE_RENDERED = "#78c7d5"
TIMELINE_QUEUED = "#557780"
SCALE_ENABLED = {
    "troughcolor": "#5b8fad",
    "background": "#f4f4f4",
    "activebackground": "#ffffff",
    "highlightbackground": "#c8c8c8",
}
APP_TITLE = f"DLSS5Tool {APP_VERSION}"
APP_CREDIT = "B站：板板之歌"
SCALE_DISABLED = {
    "troughcolor": "#e6e6e6",
    "background": "#d0d0d0",
    "activebackground": "#d0d0d0",
    "highlightbackground": "#ececec",
}
SCALE_VALUE_ON = "#222222"
SCALE_VALUE_OFF = "#9a9a9a"
CANVAS_RESIZE_MS = 30
PREVIEW_ZOOM_MIN = 0.25
PREVIEW_ZOOM_MAX = 8.0
PREVIEW_ZOOM_STEP = 1.25
NAVIGATOR_MAX_WIDTH = 180
NAVIGATOR_MAX_HEIGHT = 120
NAVIGATOR_MARGIN = 12
VK_MENU = 0x12
SPLIT_HIT_PX = 18
_INPUT_WIDGETS = {
    "Entry", "TEntry", "Text", "Combobox", "TCombobox",
    "Spinbox", "TSpinbox", "Treeview",
}
_SPACE_PASSTHROUGH = _INPUT_WIDGETS | {
    "Button", "TButton", "Checkbutton", "TCheckbutton",
    "Radiobutton", "TRadiobutton",
}

_WINDOW_GEOMETRY_RE = re.compile(
    r"^(?P<width>\d+)x(?P<height>\d+)(?P<x>[+-]\d+)(?P<y>[+-]\d+)$"
)
_FRAME_STREAM_END = object()


class _ExportCancelled(Exception):
    """Internal control-flow signal for a user-requested video export stop."""


def _clamp_frame(frame, last):
    try:
        frame = int(frame)
    except (TypeError, ValueError):
        frame = 0
    return max(0, min(frame, max(int(last), 0)))


def _lerp_hex(start, end, amount):
    amount = max(0.0, min(1.0, float(amount)))
    def channels(value):
        text = str(value or "").lstrip("#")
        if len(text) != 6:
            return (0, 0, 0)
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    r0, g0, b0 = channels(start)
    r1, g1, b1 = channels(end)
    return "#{:02x}{:02x}{:02x}".format(
        int(r0 + (r1 - r0) * amount),
        int(g0 + (g1 - g0) * amount),
        int(b0 + (b1 - b0) * amount),
    )


def _format_timecode(frame, fps):
    fps = float(fps) if fps else 30.0
    if fps <= 0:
        fps = 30.0
    total = max(int(frame), 0) / fps
    minutes = int(total // 60)
    seconds = total - minutes * 60
    return f"{minutes}:{seconds:05.2f}"


def _virtual_screen_bounds(root=None):
    """Return the usable virtual desktop rectangle, including secondary displays."""
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            x = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
            y = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
            width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
            height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
            if width > 0 and height > 0:
                return x, y, width, height
        except Exception:
            pass
    if root is not None:
        try:
            return (
                int(root.winfo_vrootx()), int(root.winfo_vrooty()),
                int(root.winfo_vrootwidth()), int(root.winfo_vrootheight()),
            )
        except Exception:
            pass
    return 0, 0, 1920, 1080


def _primary_work_area(root=None):
    """Return the primary monitor work area, excluding the taskbar when possible."""
    if sys.platform == "win32":
        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        try:
            if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0,
            ):
                return (
                    int(rect.left), int(rect.top),
                    max(int(rect.right - rect.left), 1),
                    max(int(rect.bottom - rect.top), 1),
                )
        except (AttributeError, OSError):
            pass
    if root is not None:
        try:
            return 0, 0, int(root.winfo_screenwidth()), int(root.winfo_screenheight())
        except Exception:
            pass
    return 0, 0, 1280, 720


def _studio_window_layout(scale, work_area):
    """Choose a DPI-aware initial size that remains inside the work area."""
    left, top, work_width, work_height = (int(value) for value in work_area)
    scale = max(1.0, min(float(scale or 1.0), 3.0))
    margin = max(16, round(24 * scale))
    max_width = max(min(work_width, 640), work_width - margin * 2)
    max_height = max(min(work_height, 480), work_height - margin * 2)
    width = min(round(1280 * scale), max_width)
    height = min(round(840 * scale), max_height)
    x = left + max((work_width - width) // 2, 0)
    y = top + max((work_height - height) // 2, 0)
    minimum = (
        min(round(980 * scale), width),
        min(round(680 * scale), height),
    )
    return f"{width}x{height}{x:+d}{y:+d}", minimum


def _clamp_window_geometry(value, bounds, fallback=(1100, 700, 48, 48)):
    """Clamp a saved Tk geometry so a detached preview remains fully reachable."""
    bx, by, bw, bh = map(int, bounds)
    fw, fh, fx, fy = map(int, fallback)
    match = _WINDOW_GEOMETRY_RE.fullmatch(str(value or "").strip())
    if match:
        width = int(match.group("width"))
        height = int(match.group("height"))
        x = int(match.group("x"))
        y = int(match.group("y"))
    else:
        width, height, x, y = fw, fh, fx, fy
    width = min(max(width, 320), max(bw, 1))
    height = min(max(height, 240), max(bh, 1))
    x = max(bx, min(x, bx + max(bw - width, 0)))
    y = max(by, min(y, by + max(bh - height, 0)))
    return f"{width}x{height}{x:+d}{y:+d}"


def _preview_control_layout(width):
    """Choose a player-toolbar layout that never dictates a wide preview window."""
    width = max(int(width), 0)
    if width >= 805:
        return "wide"
    if width >= 420:
        return "stacked"
    return "compact"


def _play_target_frame(start_frame, elapsed, fps, last):
    fps = float(fps) if fps else 30.0
    if fps <= 0:
        fps = 30.0
    target = int(start_frame + elapsed * fps)
    return _clamp_frame(target, last)


def _format_duration(seconds):
    try:
        seconds = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        seconds = 0
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def _alt_is_down():
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_MENU) & 0x8000)
    except Exception:
        return False


def _is_video_path(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS


def _is_image_path(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def _read_image_bgr(path):
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _write_image_bgr(path, bgr):
    ext = os.path.splitext(path)[1].lower() or ".png"
    if ext not in IMAGE_ENCODE_EXTS:
        ext = ".png"
        path = os.path.splitext(path)[0] + ext
    ok, buf = cv2.imencode(ext, bgr)
    if not ok:
        ext = ".png"
        path = os.path.splitext(path)[0] + ext
        ok, buf = cv2.imencode(ext, bgr)
    if not ok:
        raise RuntimeError("无法编码图片")
    buf.tofile(path)
    return path


def _first_image(*images):
    """Return the first argument that is not None. Numpy arrays must not be used with `or`."""
    for image in images:
        if image is not None:
            return image
    return None


def _decode_plan(cap_next, frame, small_gap=8):
    """How to reach `frame` from the decoder's next index: read, skip N, or seek."""
    if cap_next is None:
        return "seek", 0
    try:
        frame = int(frame)
        cap_next = int(cap_next)
    except (TypeError, ValueError):
        return "seek", 0
    if frame == cap_next:
        return "read", 0
    if frame > cap_next and (frame - cap_next) <= small_gap:
        return "skip", frame - cap_next
    return "seek", 0


def _frame_ranges(frames):
    """Compress frame indexes into inclusive ranges for timeline drawing."""
    ordered = sorted({int(frame) for frame in frames})
    if not ordered:
        return []
    ranges = []
    start = previous = ordered[0]
    for frame in ordered[1:]:
        if frame == previous + 1:
            previous = frame
            continue
        ranges.append((start, previous))
        start = previous = frame
    ranges.append((start, previous))
    return ranges


def _fit_preview_size(width, height, max_edge):
    """Return an even, aspect-preserving size capped by its longest edge."""
    try:
        width, height, max_edge = int(width), int(height), int(max_edge)
    except (TypeError, ValueError):
        return 0, 0
    if width <= 0 or height <= 0:
        return 0, 0
    if max_edge <= 0 or max(width, height) <= max_edge:
        return width, height
    scale = max_edge / float(max(width, height))
    scaled_w = max(2, int(width * scale))
    scaled_h = max(2, int(height * scale))
    scaled_w -= scaled_w % 2
    scaled_h -= scaled_h % 2
    return max(scaled_w, 2), max(scaled_h, 2)


def _realtime_preview_size(width, height, quality="auto"):
    """Choose the DLSS processing size used during realtime playback."""
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return 0, 0
    quality = quality if quality in PREVIEW_QUALITY_NAMES else "auto"
    if quality == "original":
        return width, height
    if quality == "auto":
        # Preserve sources up to 1440p; 4K-class sources use a 1080p proxy.
        max_edge = 1920 if max(width, height) > 2560 else max(width, height)
    else:
        max_edge = PREVIEW_MAX_EDGES[quality]
    return _fit_preview_size(width, height, max_edge)


def _preview_viewport(width, height, canvas_width, canvas_height,
                      zoom=1.0, center_x=0.5, center_y=0.5):
    """Return source crop, canvas destination, clamped center and display scale."""
    try:
        width, height = int(width), int(height)
        canvas_width, canvas_height = int(canvas_width), int(canvas_height)
        zoom = float(zoom)
        center_x, center_y = float(center_x), float(center_y)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (zoom, center_x, center_y)):
        return None
    if min(width, height, canvas_width, canvas_height) <= 0:
        return None
    zoom = max(PREVIEW_ZOOM_MIN, min(zoom, PREVIEW_ZOOM_MAX))
    scale = min(canvas_width / width, canvas_height / height) * zoom

    def axis(source_size, viewport_size, center):
        drawn = source_size * scale
        if drawn <= viewport_size + 1e-6:
            offset = (viewport_size - drawn) / 2.0
            return 0.0, float(source_size), offset, drawn, 0.5
        visible = viewport_size / scale
        center_px = max(visible / 2.0, min(center * source_size, source_size - visible / 2.0))
        start = center_px - visible / 2.0
        return start, start + visible, 0.0, float(viewport_size), center_px / source_size

    x0, x1, dx, dw, center_x = axis(width, canvas_width, center_x)
    y0, y1, dy, dh, center_y = axis(height, canvas_height, center_y)
    return (
        (x0, y0, x1, y1),
        (dx, dy, dw, dh),
        (center_x, center_y),
        scale,
    )


def _large_image_host_settings(width, height, settings):
    """Use Feature 18 subrects when one full-frame feature exceeds safe limits."""
    result = dict(settings or {})
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return result
    if width <= 0 or height <= 0 or width * height < LARGE_IMAGE_TILE_THRESHOLD_PIXELS:
        return result
    result.update({
        "host_backend": "v2",
        "host_auto_fallback": False,
        "host_zero_fast_path": True,
        "host_in_flight": 1,
        "host_tiled_mode": True,
        "host_tile_width": min(width, LARGE_IMAGE_TILE_WIDTH),
        "host_tile_height": min(height, LARGE_IMAGE_TILE_HEIGHT),
    })
    return result


def _fit_output_box(width, height, max_width, max_height):
    """Fit a source inside an output box without upscaling or changing aspect ratio."""
    try:
        width, height = int(width), int(height)
        max_width, max_height = int(max_width), int(max_height)
    except (TypeError, ValueError):
        return 0, 0
    if min(width, height, max_width, max_height) <= 0:
        return 0, 0
    scale = min(1.0, max_width / float(width), max_height / float(height))
    if scale >= 1.0:
        return width, height
    output_width = max(2, int(width * scale))
    output_height = max(2, int(height * scale))
    output_width -= output_width % 2
    output_height -= output_height % 2
    return max(output_width, 2), max(output_height, 2)


def _resolve_output_size(
    width, height, resolution="source", custom_width=1920, custom_height=1080,
):
    """Resolve a named output limit to the actual aspect-preserving frame size."""
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return 0, 0
    if width <= 0 or height <= 0:
        return 0, 0
    if resolution == "source":
        return width, height
    if resolution == "custom":
        return _fit_output_box(width, height, custom_width, custom_height)
    max_edge = OUTPUT_RESOLUTION_MAX_EDGES.get(resolution)
    if max_edge is None:
        return width, height
    if width >= height:
        return _fit_output_box(width, height, max_edge, max_edge * 9 // 16)
    return _fit_output_box(width, height, max_edge * 9 // 16, max_edge)


def _estimate_output_size_mb(duration_seconds, video_bitrate_mbps, audio_mbps=0.256):
    """Estimate decimal megabytes for target-bitrate mode, including typical audio."""
    try:
        duration = max(0.0, float(duration_seconds))
        video_rate = max(0.0, float(video_bitrate_mbps))
        audio_rate = max(0.0, float(audio_mbps))
    except (TypeError, ValueError):
        return 0.0
    return duration * (video_rate + audio_rate) / 8.0


def _postprocess_and_write(writer, original, processed, view, mix):
    """CPU post-processing + FFmpeg write stage, run on one ordered worker thread."""
    writer.write(compose_output_frame(original, processed, view, mix))


def effective_slider(enabled, value):
    """Closed switch yields 0; open switch yields the remembered slider value."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    return value if enabled else 0.0


def effective_skin_settings(enabled, value):
    """A zero skin strength is a true no-op, including the auto-mask flag."""
    strength = effective_slider(enabled, value)
    return (1 if strength > 0.0 else 0), strength


def _normalize_slider_input(
    value, fallback=0.0, max_value=app_settings.DLSS_STANDARD_MAX,
):
    """Parse decimal or percentage input and snap it to the supported slider range."""
    try:
        text = str(value).strip()
        if text.endswith("%"):
            parsed = float(text[:-1].strip()) / 100.0
        else:
            parsed = float(text)
        if not math.isfinite(parsed):
            raise ValueError
    except (TypeError, ValueError):
        try:
            parsed = float(fallback)
        except (TypeError, ValueError):
            parsed = app_settings.DLSS_SLIDER_MIN
    try:
        max_value = float(max_value)
        if not math.isfinite(max_value):
            raise ValueError
    except (TypeError, ValueError):
        max_value = app_settings.DLSS_STANDARD_MAX
    max_value = max(
        app_settings.DLSS_STANDARD_MAX,
        min(app_settings.DLSS_SLIDER_MAX, max_value),
    )
    parsed = max(
        app_settings.DLSS_SLIDER_MIN,
        min(max_value, parsed),
    )
    steps = round(
        (parsed - app_settings.DLSS_SLIDER_MIN) / app_settings.DLSS_SLIDER_STEP
    )
    return round(
        app_settings.DLSS_SLIDER_MIN + steps * app_settings.DLSS_SLIDER_STEP,
        2,
    )


def compose_preview_frame(original, processed, output_view=0, output_mix=1.0):
    """Apply the export mix to preview without previewing export-only view layouts."""
    if original is None or processed is None:
        return processed
    if original.shape[:2] != processed.shape[:2]:
        ph, pw = processed.shape[:2]
        shrinking = original.shape[0] > ph or original.shape[1] > pw
        interpolation = cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR
        original = cv2.resize(original, (pw, ph), interpolation=interpolation)
    mix = float(output_mix) if int(output_view) == 0 else 1.0
    return compose_output_frame(original, processed, view=0, mix=mix)



class App:
    def __init__(self, root):
        self.root = root
        self._saved_settings = app_settings.load()
        self._ui_theme_name = ui_theme.normalize_theme_name(
            self._saved_settings.get("ui_theme", "dark")
        )
        self._ui = ui_theme.tokens(self._ui_theme_name)
        self._log_open = True
        self._drop_hover = False
        self._empty_btn_hover = 0.0
        self._empty_btn_target = 0.0
        self._empty_btn_after = None
        self._empty_import_geom = None
        Tooltip.set_palette(self._ui)
        ui_theme.apply_ttk(root, self._ui)
        root.title(f"{APP_TITLE} — {APP_CREDIT}")
        ui_theme.apply_app_icon(root, default=True)
        ui_scale = getattr(root, "_studio_scale", 1.0)
        initial_geometry, minimum_size = _studio_window_layout(
            ui_scale, _primary_work_area(root),
        )
        root.geometry(initial_geometry)
        root.minsize(*minimum_size)
        self._settings_save_after = None
        self.video = None
        self.nframes = 0
        self.fps = 30.0
        self.thread = None
        self.split_x = 0.5
        self.playing = False
        self._frame = 0
        self._exporting = False
        self._export_cancel_event = threading.Event()
        self._switching_backend = False
        self._diagnosing = False
        self._diagnostic_thread = None
        self._update_checking = False
        self._update_downloading = False
        self._update_progress_percent = None
        self._update_thread = None
        self._update_cancel_event = threading.Event()
        self._live = None
        self._live_cache = None
        self._super_resolution_live = None
        self._super_resolution_key = None
        self._super_resolution_lock = threading.RLock()
        self._last_super_resolution_preview = None
        self._confirmed_super_resolution_plans = set()
        self._last_dlss_frame = -1
        self._live_debounce = None
        self._output_preview_after = None
        self._scrub_after = None
        self._resize_after = None
        self._play_after = None
        self._preview_decode_after = None
        self._preview_cache_resume_after = None
        self._preview_cache_frozen = False
        self._preview_zoom = 1.0
        self._preview_pan_x = 0.5
        self._preview_pan_y = 0.5
        self._viewport_crop_norm = (0.0, 0.0, 1.0, 1.0)
        self._viewport_scale = 1.0
        self._viewport_source_size = (1, 1)
        self._navigator_geom = None
        self._navigator_source = None
        self._navigator_thumb = None
        self._navigator_thumb_size = None
        self._pan_moved = False
        self._last_viewport_image = None
        self._hold_original = False
        self._drag_split = False
        self._split_moved = False
        self._canvas_press = None
        self._dlss_pending = False
        self._hinted_keys = False
        self._play_anchor_time = 0.0
        self._play_anchor_frame = 0
        self._video_geom = None
        self._split_orig = None
        self._split_dlss = None
        self._cap_next = None
        self._live_lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._play_dlss_busy = False
        self._play_dlss_thread = None
        self._preview_frame_queue = None
        self._preview_worker_error = None
        self._last_shown_dlss = None
        self._presented_preview_key = None
        self._dlss_frame_cache = {}
        self._dlss_cache_bytes = 0
        self._source_frame_cache = {}
        self._source_cache_bytes = 0
        self._queued_preview_frames = set()
        self._preview_decode_next = None
        self._buffering = False
        self._buffer_started_at = None
        self._pre_rendering = False
        self._preview_processed_frames = 0
        self._preview_process_t0 = None
        self._preview_status_at = 0.0
        self._prefetch_stop = threading.Event()
        self._prefetch_stop.set()
        self._prefetch_gen = 0
        self._export_t0 = None
        self._export_ema_fps = None
        self._audio = PreviewAudio()
        self._image_bgr = None
        self._source_kind = None
        self._video_color_info = None
        self._media_w = 0
        self._media_h = 0
        self._active_preview_size = None
        self._fullscreen = False
        self._fs_hidden = []
        self._fs_geom = None
        self._fs_window = None
        self._fs_used_zoomed = False
        self._preview_detached = False
        self._detached_preview_window = None
        self._detached_preview_pane = None
        self._detached_preview_placeholder = None
        self._detached_preview_geometry = self._saved_settings.get(
            "preview_window_geometry", ""
        )
        self._queue_jobs = export_queue_state.load()
        self._queue_running = False
        self._queue_pause_requested = False
        self._queue_active_job_id = None
        self._queue_last_summary = None
        self.view_var = tk.StringVar(value=self._saved_settings["preview_view"])
        self._theme_widgets = []

        # Studio: left monitor + fixed 360px inspector (matches the mockup).
        self._studio = ttk.Frame(root, style="Workspace.TFrame")
        self._studio.pack(fill="both", expand=True)
        self._inspector_width = max(
            ui_theme.INSPECTOR_MIN,
            min(ui_theme.INSPECTOR_MAX, int(self._saved_settings.get("inspector_width", 360))),
        )
        self._inspector = ttk.Frame(
            self._studio, style="Panel.TFrame", width=round(self._inspector_width * ui_scale),
        )
        self._inspector.pack(side="right", fill="y")
        self._inspector.pack_propagate(False)
        self._inspector_grip = tk.Frame(
            self._inspector, width=4, cursor="sb_h_double_arrow",
            bg=self._ui.get("line", "#2a3540"), bd=0, highlightthickness=0,
        )
        self._inspector_grip.pack(side="left", fill="y")
        self._inspector_grip.bind("<Button-1>", self._on_inspector_grip_start)
        self._inspector_grip.bind("<B1-Motion>", self._on_inspector_grip_drag)
        self._inspector_grip.bind("<ButtonRelease-1>", self._on_inspector_grip_end)
        self._preview_host = ttk.Frame(self._studio, style="Stage.TFrame")
        self._preview_host.pack(side="left", fill="both", expand=True)

        self._docked_preview_pane = self._create_preview_pane(
            self._preview_host, detached=False,
        )
        self._activate_preview_pane(self._docked_preview_pane)

        self.workspace_tabs = StudioNotebook(self._inspector, ui=self._ui)
        self.workspace_tabs.pack(fill="both", expand=True)
        self._theme_widgets.append(self.workspace_tabs)
        self._preview_page = ttk.Frame(self.workspace_tabs.content, style="Panel.TFrame")
        self._export_page = ttk.Frame(self.workspace_tabs.content, style="Panel.TFrame")
        self.queue_tab = ttk.Frame(self.workspace_tabs.content, style="Panel.TFrame")
        self.workspace_tabs.add(self._preview_page, text="调参")
        self.workspace_tabs.add(self._export_page, text="导出")
        self.workspace_tabs.add(self.queue_tab, text="队列", badge="0")

        self._preview_page.grid_rowconfigure(0, weight=1)
        self._preview_page.grid_rowconfigure(1, weight=0)
        self._preview_page.grid_columnconfigure(0, weight=1)
        self._preview_canvas = tk.Canvas(
            self._preview_page, background=self._ui["panel"],
            highlightthickness=0, borderwidth=0,
        )
        self._preview_canvas.grid(row=0, column=0, sticky="nsew")
        self._preview_scrollbar = ttk.Scrollbar(
            self._preview_page, orient="vertical", command=self._preview_canvas.yview,
        )
        self._preview_scrollbar.grid(row=0, column=1, sticky="ns")
        self._preview_scrollbar.grid_remove()
        self._preview_scrollbar_visible = False
        self._preview_canvas.configure(yscrollcommand=self._preview_scrollbar.set)
        self.preview_tab = ttk.Frame(self._preview_canvas, style="Panel.TFrame")
        self._preview_window = self._preview_canvas.create_window(
            (0, 0), window=self.preview_tab, anchor="nw",
        )
        self.preview_tab.bind("<Configure>", self._sync_preview_scrollregion)
        self._preview_canvas.bind("<Configure>", self._resize_preview_content)
        self.root.bind_all("<MouseWheel>", self._on_workspace_mousewheel, add="+")

        sf = ttk.Frame(self.preview_tab, style="Panel.TFrame")
        self._settings_frame = sf
        sf.pack(fill="x", padx=16, pady=(12, 8))
        self._settings = self._build_settings(sf)
        Tooltip(
            sf,
            "每个滑条的开关关闭时按 0 处理，开启后使用记忆的数值。\n"
            "皮肤蒙版数值为 0 时等同关闭；大于 0 时才启用自动蒙版。",
        )

        self._export_quick = ttk.Frame(self.preview_tab, style="Panel.TFrame")
        self._export_quick.pack(fill="x", padx=16, pady=(2, 6))

        e = ttk.Frame(self._preview_page, style="Panel.TFrame")
        e.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(8, 16))
        self._export_row = e
        actions = ttk.Frame(e, style="Panel.TFrame")
        actions.pack(fill="x")
        for column in range(3):
            actions.columnconfigure(column, weight=1, uniform="actions")
        self.import_btn = ChromeButton(
            actions, text="导入", variant="ghost", command=self.import_media,
            ui=self._ui, width=88,
        )
        self.import_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._theme_widgets.append(self.import_btn)
        self.clear_btn = ChromeButton(
            actions, text="清空", variant="ghost", command=self.clear_media,
            ui=self._ui, width=88,
        )
        self.clear_btn.grid(row=0, column=1, sticky="ew", padx=4)
        self._theme_widgets.append(self.clear_btn)
        Tooltip(self.clear_btn, "卸下当前视频/图片，释放解码、音轨和 DLSS 主机占用。")
        self.add_queue_btn = ChromeButton(
            actions, text="加入队列", variant="default", command=self.add_current_to_queue,
            ui=self._ui, width=108,
        )
        self.add_queue_btn.grid(row=0, column=2, sticky="ew", padx=(4, 0))
        self._theme_widgets.append(self.add_queue_btn)
        Tooltip(self.add_queue_btn, "使用当前处理与导出参数，把当前视频或图片加入导出队列。")
        run_bar = ttk.Frame(e, style="Panel.TFrame")
        run_bar.pack(fill="x", pady=(8, 0))
        for column in range(2):
            run_bar.columnconfigure(column, weight=1, uniform="export_run")
        self._export_run_bar = run_bar
        self.export_btn = ChromeButton(
            run_bar, text="导出 DLSS", variant="accent", command=self.export_dlss, ui=self._ui,
        )
        self.export_btn.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._theme_widgets.append(self.export_btn)
        self.cancel_export_btn = ChromeButton(
            run_bar, text="取消导出", variant="danger", command=self.cancel_export,
            ui=self._ui, icon="cancel", primary=True,
        )
        self._theme_widgets.append(self.cancel_export_btn)
        Tooltip(self.cancel_export_btn, "停止当前视频导出，并清理本次未完成的输出文件。")
        self.cancel_export_btn.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.cancel_export_btn.grid_remove()
        self._export_run_layout = "export"

        self._export_page.grid_rowconfigure(0, weight=1)
        self._export_page.grid_columnconfigure(0, weight=1)
        self._export_canvas = tk.Canvas(
            self._export_page, background=self._ui["panel"],
            highlightthickness=0, borderwidth=0,
        )
        self._export_canvas.grid(row=0, column=0, sticky="nsew")
        self._export_scrollbar = ttk.Scrollbar(
            self._export_page, orient="vertical", command=self._export_canvas.yview,
        )
        self._export_scrollbar.grid(row=0, column=1, sticky="ns")
        self._export_scrollbar.grid_remove()
        self._export_scrollbar_visible = False
        self._export_canvas.configure(yscrollcommand=self._export_scrollbar.set)
        export_inner = ttk.Frame(self._export_canvas, style="Panel.TFrame")
        self._export_window = self._export_canvas.create_window(
            (0, 0), window=export_inner, anchor="nw",
        )
        export_inner.bind("<Configure>", self._sync_export_scrollregion)
        self._export_canvas.bind("<Configure>", self._resize_export_content)
        self._preview_section = CollapsibleSection(
            export_inner, "预览性能",
            collapsed=not self._saved_settings.get("ui_preview_open", False),
            on_toggle=self._on_panels_toggle,
            tooltip=(
                "播放质量只影响实时播放；暂停、逐帧和拖动松手后仍生成原始分辨率精确帧。\n"
                "缓存窗口越大越占内存。"
            ),
            ui=self._ui,
        )
        self._theme_widgets.append(self._preview_section)
        self._preview_section.pack(fill="x", padx=8, pady=(8, 2))
        self._preview_settings = self._build_preview_settings(self._preview_section.body)
        self._preview_runtime_settings = self._collect_preview_settings()
        self.root.after_idle(self._update_preview_memory_hint)

        self._export_section = CollapsibleSection(
            export_inner, "导出设置",
            collapsed=not self._saved_settings.get("ui_export_open", False),
            on_toggle=self._on_panels_toggle,
            tooltip=(
                "设置输出分辨率、编码质量或目标码率，以及导出性能。"
                "并行模式保持视觉质量，但不保证逐像素时序一致。"
            ),
            ui=self._ui,
        )
        self._theme_widgets.append(self._export_section)
        self._export_section.pack(fill="x", padx=8, pady=2)
        self._export_settings = self._build_export_settings(self._export_section.body)

        self._host_section = CollapsibleSection(
            export_inner, "高级主机优化",
            collapsed=not self._saved_settings.get("ui_host_open", False),
            on_toggle=self._on_panels_toggle,
            tooltip="后端在隔离进程中热切换，无需重启 GUI。导出期间为保持时序会锁定这些选项。",
            ui=self._ui,
        )
        self._theme_widgets.append(self._host_section)
        self._host_section.pack(fill="x", padx=8, pady=2)
        self._host_settings = self._build_host_settings(self._host_section.body)

        self._build_queue_tab(self.queue_tab)
        self._build_export_quick(self._export_quick)
        self._build_status_bar(root)
        self._build_progress_rule(root)

        self.log = scrolledtext.ScrolledText(
            root, height=5, state="disabled",
            font=ui_theme.UI_MONO,
            bg=self._ui["log_bg"], fg=self._ui["log_fg"],
            insertbackground=self._ui["text"],
            relief="flat", borderwidth=0, highlightthickness=0,
        )
        self._sync_log_panel()

        self._bind_player_keys()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_drag_and_drop()
        self._apply_ui_theme(self._ui_theme_name, persist=False)
        self.root.after_idle(
            lambda: ui_theme.apply_native_titlebar(
                self.root, self._ui, self._ui_theme_name == "dark",
            )
        )
        self._update_action_labels()
        self._refresh_queue_tree()
        self._save_queue_state()
        self._refresh_status_chips()
        self.root.after_idle(self._draw_empty)
        if self._saved_settings.get("preview_detached", False):
            self.root.after_idle(self.detach_preview)
        if getattr(sys, "frozen", False) and not os.environ.get(
            "DLSS5TOOL_DISABLE_UPDATE_CHECK"
        ):
            # Start exactly one non-blocking check during application startup.
            # Up-to-date, newer local builds, and network failures stay silent.
            self.check_for_updates(manual=False)

    def _create_preview_pane(self, parent, detached=False):
        """Build one visual player surface backed by the shared App state."""
        theme_widgets = []
        canvas = tk.Canvas(
            parent, bg=self._ui_color("canvas", CANVAS_BG), highlightthickness=0,
        )
        canvas.pack(fill="both", expand=True)
        canvas.bind("<Configure>", self._on_canvas_configure)
        canvas.bind("<Button-1>", self.on_canvas_press)
        canvas.bind("<B1-Motion>", self.on_canvas_drag)
        canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        canvas.bind("<Motion>", self.on_canvas_hover)
        canvas.bind("<Double-Button-1>", self.on_canvas_double)
        canvas.bind("<Leave>", self._on_canvas_leave)
        canvas.bind("<MouseWheel>", self._on_canvas_wheel)

        transport = ttk.Frame(parent, style="Transport.TFrame")
        transport.pack(fill="x", padx=10, pady=(0, 10))
        timeline = TimelineBar(transport, height=10)
        timeline.apply_theme(self._ui)
        timeline.pack(fill="x", padx=10, pady=(8, 6))
        timeline.on_seek = self._on_timeline_seek
        timeline.bind("<MouseWheel>", self._on_wheel_step)
        Tooltip(timeline, "浅青：当前设置下已渲染；灰青：已解码并等待渲染。")

        ctrl = ttk.Frame(transport, style="Transport.TFrame")
        ctrl.pack(fill="x", padx=8, pady=(0, 8))
        left = ttk.Frame(ctrl, style="Transport.TFrame")
        left.pack(side="left")
        playback_bar = ttk.Frame(left, style="Transport.TFrame")
        playback_bar.pack(side="left")
        prev_btn = ChromeButton(
            playback_bar, text="上一帧", icon="prev", icon_only=True,
            variant="tool", width=36, command=lambda: self.step_frame(-1), ui=self._ui,
        )
        prev_btn.pack(side="left")
        Tooltip(prev_btn, "上一帧（←）")
        play_btn = ChromeButton(
            playback_bar, text="播放", icon="play", icon_only=True,
            variant="tool", width=36, command=self.toggle_play, ui=self._ui,
        )
        play_btn.pack(side="left", padx=(4, 0))
        Tooltip(play_btn, "播放 / 暂停（空格）。播完停在最后一帧。")
        next_btn = ChromeButton(
            playback_bar, text="下一帧", icon="next", icon_only=True,
            variant="tool", width=36, command=lambda: self.step_frame(1), ui=self._ui,
        )
        next_btn.pack(side="left", padx=(4, 0))
        Tooltip(next_btn, "下一帧（→）")
        mute_btn = ChromeButton(
            playback_bar, text="声音", icon="volume", icon_only=True,
            variant="tool", width=36, command=self.toggle_mute, ui=self._ui,
        )
        mute_btn.pack(side="left", padx=(4, 0))
        Tooltip(mute_btn, "预览播放原视频声音。点击静音/取消静音。")

        position_bar = ttk.Frame(left, style="Transport.TFrame")
        position_bar.pack(side="left")
        time_label = ttk.Label(
            position_bar, text="0:00.00 / 0:00.00", width=18, anchor="w",
            style="Transport.TLabel", font=ui_theme.UI_MONO,
        )
        time_label.pack(side="left", padx=(10, 6))
        ttk.Label(position_bar, text="帧", style="Transport.TLabel").pack(side="left")
        fentry = ChromeEntry(
            position_bar, ui=self._ui, width=6, justify="right",
        )
        fentry.pack(side="left", padx=3)
        fentry.insert(0, "0")
        fentry.bind("<Return>", self.on_frame_entry)
        fentry.bind("<FocusOut>", lambda _event: self.sync_frame_entry())
        self._theme_widgets.append(fentry)
        theme_widgets.append(fentry)
        ftotal = ttk.Label(
            position_bar, text="/ 0", style="Transport.TLabel",
            font=ui_theme.UI_MONO,
        )
        ftotal.pack(side="left")

        right = ttk.Frame(ctrl, style="Transport.TFrame")
        right.pack(side="right")
        view_bar = SegmentedBar(
            right, self.view_var, VIEWS, command=self.on_view_change, ui=self._ui,
        )
        view_bar.pack(side="left", padx=(0, 8))
        self._theme_widgets.append(view_bar)
        theme_widgets.append(view_bar)
        Tooltip(
            view_bar,
            "1 原图  ·  2 DLSS  ·  3 对比。滚轮缩放；放大后拖动画面；"
            "对比模式拖动分界线；按住 Alt 查看纯原图。",
        )
        zoom_bar = ttk.Frame(right, style="Transport.TFrame")
        zoom_bar.pack(side="left", padx=(0, 8))
        zoom_out_btn = ChromeButton(
            zoom_bar, text="缩小", icon="minus", icon_only=True,
            variant="tool", width=36, command=lambda: self._step_zoom(-1), ui=self._ui,
        )
        zoom_out_btn.pack(side="left")
        zoom_reset_btn = ChromeButton(
            zoom_bar, text="适应", icon="fit", icon_only=True,
            variant="tool", width=36, command=self.reset_preview_zoom, ui=self._ui,
        )
        zoom_reset_btn.pack(side="left", padx=2)
        zoom_in_btn = ChromeButton(
            zoom_bar, text="放大", icon="plus", icon_only=True,
            variant="tool", width=36, command=lambda: self._step_zoom(1), ui=self._ui,
        )
        zoom_in_btn.pack(side="left")
        Tooltip(zoom_out_btn, "缩小预览（-）")
        Tooltip(zoom_reset_btn, "恢复适应窗口（0）")
        Tooltip(zoom_in_btn, "放大预览（+）")
        detach_btn = ChromeButton(
            right,
            text="停靠" if detached else "分离",
            icon="dock" if detached else "detach",
            icon_only=True, variant="tool", width=36,
            command=self.toggle_detached_preview, ui=self._ui,
        )
        detach_btn.pack(side="left", padx=(0, 4))
        Tooltip(
            detach_btn,
            "将预览停靠回主窗口" if detached else "在可自由缩放的独立窗口中预览",
        )
        fs_btn = ChromeButton(
            right, text="全屏", icon="fullscreen", icon_only=True,
            variant="tool", width=36, command=self.toggle_fullscreen, ui=self._ui,
        )
        fs_btn.pack(side="left")
        Tooltip(fs_btn, "全屏预览（F11 或双击画面，Esc 退出）")

        layout_state = {"mode": "wide"}
        ctrl.bind(
            "<Configure>",
            lambda event: self._layout_preview_controls(
                event.width,
                left, right,
                playback_bar, position_bar,
                view_bar, zoom_bar, detach_btn, fs_btn,
                layout_state,
            ),
            add="+",
        )

        for widget in (
            prev_btn, play_btn, next_btn, mute_btn,
            zoom_out_btn, zoom_reset_btn, zoom_in_btn, detach_btn, fs_btn,
        ):
            self._theme_widgets.append(widget)
            theme_widgets.append(widget)

        return {
            "canvas": canvas,
            "transport": transport,
            "timeline": timeline,
            "prev_btn": prev_btn,
            "play_btn": play_btn,
            "next_btn": next_btn,
            "mute_btn": mute_btn,
            "time_label": time_label,
            "fentry": fentry,
            "ftotal": ftotal,
            "zoom_out_btn": zoom_out_btn,
            "zoom_reset_btn": zoom_reset_btn,
            "zoom_in_btn": zoom_in_btn,
            "detach_btn": detach_btn,
            "fs_btn": fs_btn,
            "theme_widgets": tuple(theme_widgets),
        }

    @staticmethod
    def _layout_preview_controls(
        width,
        left, right,
        playback_bar, position_bar,
        view_bar, zoom_bar, detach_btn, fs_btn,
        state,
    ):
        mode = _preview_control_layout(width)
        if state.get("mode") == mode:
            return
        state["mode"] = mode
        for widget in (
            left, right,
            playback_bar, position_bar,
            view_bar, zoom_bar, detach_btn, fs_btn,
        ):
            widget.pack_forget()

        if mode == "wide":
            left.pack(side="left")
            right.pack(side="right")
        else:
            left.pack(side="top", fill="x", anchor="w")
            right.pack(side="top", fill="x", anchor="w", pady=(4, 0))

        if mode == "compact":
            playback_bar.pack(side="top", anchor="w")
            position_bar.pack(side="top", anchor="w", pady=(3, 0))
            view_bar.pack(side="top", anchor="w", pady=(0, 3))
        else:
            playback_bar.pack(side="left")
            position_bar.pack(side="left")
            view_bar.pack(side="left", padx=(0, 8))

        zoom_bar.pack(side="left", padx=(0, 8))
        detach_btn.pack(side="left", padx=(0, 4))
        fs_btn.pack(side="left")

    def _activate_preview_pane(self, pane):
        for name, widget in pane.items():
            if name == "theme_widgets":
                continue
            setattr(self, name, widget)

    def _ui_color(self, key, fallback=""):
        ui = getattr(self, "_ui", None) or {}
        return ui.get(key, fallback)

    def _on_inspector_grip_start(self, event):
        self._inspector_drag = (event.x_root, self._inspector_width)

    def _on_inspector_grip_drag(self, event):
        start = getattr(self, "_inspector_drag", None)
        if not start:
            return
        scale = getattr(self.root, "_studio_scale", 1.0)
        width = start[1] + (start[0] - event.x_root) / scale
        width = max(ui_theme.INSPECTOR_MIN, min(ui_theme.INSPECTOR_MAX, int(width)))
        self._inspector_width = width
        self._inspector.configure(width=round(width * scale))

    def _on_inspector_grip_end(self, _event=None):
        self._inspector_drag = None
        self._schedule_settings_save()

    def _popup_more(self):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=str(self.log_btn.cget("text")), command=self.toggle_log_panel)
        menu.add_command(
            label=str(self.update_btn.cget("text")),
            command=lambda: self.check_for_updates(manual=True),
        )
        menu.add_command(
            label=str(self.diagnostic_btn.cget("text")), command=self.export_diagnostics,
        )
        menu.add_separator()
        menu.add_command(label=str(self.theme_btn.cget("text")), command=self.toggle_ui_theme)
        try:
            menu.tk_popup(
                self.more_btn.winfo_rootx(),
                self.more_btn.winfo_rooty() + self.more_btn.winfo_height(),
            )
        finally:
            menu.grab_release()

    def _build_status_bar(self, parent):
        bar = ttk.Frame(parent, style="Status.TFrame")
        self._status_bar = bar
        bar.pack(fill="x")
        dot = ui_theme.scale_px(parent, 10)
        self._status_dot = tk.Canvas(
            bar, width=dot, height=dot, highlightthickness=0, bg=self._ui["surface"],
        )
        self._status_dot.pack(side="left", padx=(12, 0), pady=8)
        self._status_host = ttk.Label(bar, text="宿主已就绪", style="Status.TLabel")
        self._status_host.pack(side="left", padx=(6, 8), pady=6)
        self._status_chips = StatusPills(bar, ui=self._ui)
        self._status_chips.pack(side="left", padx=(8, 8))
        self._theme_widgets.append(self._status_chips)
        self._status_chip_labels = []
        self.eta_label = ttk.Label(bar, text="", style="Status.TLabel", anchor="w")
        self.eta_label.pack(side="left", fill="x", expand=True)
        utility = ttk.Frame(bar, style="Status.TFrame")
        utility.pack(side="right", padx=8, pady=4)
        self.theme_btn = ttk.Button(
            utility, text="浅色皮肤", width=8, command=self.toggle_ui_theme,
        )
        self.diagnostic_btn = ttk.Button(
            utility, text="一键诊断", width=10, command=self.export_diagnostics,
        )
        self.update_btn = ttk.Button(
            utility, text="检查更新", width=10,
            command=lambda: self.check_for_updates(manual=True),
        )
        self.log_btn = ttk.Button(
            utility, text="日志", width=6, command=self.toggle_log_panel,
        )
        self._status_metric = ttk.Label(utility, text="等待导入", style="Status.TLabel")
        self._status_metric.pack(side="left", padx=(0, 10))
        self.more_btn = ChromeButton(
            utility, text="更多", variant="ghost", width=36,
            icon="more", icon_only=True, command=self._popup_more, ui=self._ui,
        )
        self.more_btn.pack(side="right")
        self._theme_widgets.append(self.more_btn)
        Tooltip(self.more_btn, "日志、检查更新、一键诊断和皮肤。")

    def _build_progress_rule(self, parent):
        rule = ProgressRule(parent, ui=self._ui)
        self._progress_rule = rule
        self.pbar = rule
        self._theme_widgets.append(rule)
        rule.pack(fill="x", before=self._status_bar)

    def toggle_ui_theme(self):
        self._apply_ui_theme(
            "light" if self._ui_theme_name != "light" else "dark",
        )

    def toggle_log_panel(self):
        self._log_open = not self._log_open
        self._sync_log_panel()

    def _sync_log_panel(self):
        if self._log_open:
            options = {"fill": "x", "padx": 0, "pady": 0}
            if getattr(self, "_progress_rule", None) is not None:
                options["after"] = self._progress_rule
            elif getattr(self, "_status_bar", None) is not None:
                options["before"] = self._status_bar
            self.log.pack(**options)
            self.log_btn.configure(text="收起日志")
        else:
            self.log.pack_forget()
            self.log_btn.configure(text="日志")

    def _restack_bottom_chrome(self):
        try:
            self._status_bar.pack(fill="x")
        except Exception:
            pass
        try:
            self._progress_rule.pack(fill="x", before=self._status_bar)
        except Exception:
            pass
        if hasattr(self, "log"):
            self._sync_log_panel()

    def _apply_ui_theme(self, name=None, persist=True):
        if name is not None:
            self._ui_theme_name = ui_theme.normalize_theme_name(name)
        self._ui = ui_theme.tokens(self._ui_theme_name)
        Tooltip.set_palette(self._ui)
        ui_theme.apply_ttk(self.root, self._ui)
        themed_windows = [self.root]
        if self._detached_preview_window is not None:
            themed_windows.append(self._detached_preview_window)
        for window in themed_windows:
            try:
                window.update_idletasks()
                ui_theme.apply_native_titlebar(
                    window, self._ui, self._ui_theme_name == "dark",
                )
            except Exception:
                pass
        try:
            self._inspector_grip.configure(bg=self._ui.get("line", "#2a3540"))
        except Exception:
            pass
        try:
            self.theme_btn.configure(
                text="暗色皮肤" if self._ui_theme_name == "light" else "浅色皮肤",
            )
        except Exception:
            pass
        try:
            self.log.configure(
                bg=self._ui["log_bg"], fg=self._ui["log_fg"],
                insertbackground=self._ui["text"],
            )
        except Exception:
            pass
        try:
            self._preview_canvas.configure(bg=self._ui["panel"])
        except Exception:
            pass
        try:
            self._export_canvas.configure(bg=self._ui["panel"])
        except Exception:
            pass
        panes = [self._docked_preview_pane]
        if self._detached_preview_pane is not None:
            panes.append(self._detached_preview_pane)
        for pane in panes:
            try:
                pane["canvas"].configure(bg=self._ui["canvas"])
            except Exception:
                pass
            try:
                pane["timeline"].apply_theme(self._ui)
            except Exception:
                pass
            try:
                apply_fentry = getattr(pane["fentry"], "apply_theme", None)
                if apply_fentry:
                    apply_fentry(self._ui)
            except Exception:
                pass
        try:
            self.queue_tree.tag_configure("failed", foreground=self._ui["failed"])
            self.queue_tree.tag_configure("interrupted", foreground=self._ui["interrupted"])
            self.queue_tree.tag_configure("completed", foreground=self._ui["completed"])
        except Exception:
            pass
        try:
            self._queue_tree_shell.configure(bg=self._ui.get("line", "#2a3540"))
        except Exception:
            pass
        for widget in list(getattr(self, "_theme_widgets", ())):
            apply = getattr(widget, "apply_theme", None)
            if apply is None:
                continue
            try:
                apply(self._ui)
            except Exception:
                pass
        if hasattr(self, "_settings"):
            self._update_dlss_control_states()
        if hasattr(self, "_export_settings"):
            self._update_export_control_states()
        self._refresh_status_chips()
        if persist:
            self._schedule_settings_save()
        if not self.video:
            try:
                self._draw_empty()
            except Exception:
                pass
        else:
            self._refresh_preview_surface()

    def _refresh_status_chips(self):
        if not hasattr(self, "_status_chips"):
            return
        for label in getattr(self, "_status_chip_labels", ()):
            try:
                label.destroy()
            except Exception:
                pass
        self._status_chip_labels = []
        host = "等待导入"
        if self._exporting:
            host = "正在导出"
        elif self._queue_running:
            host = "队列处理中"
        elif self.video:
            host = "预渲染就绪" if not self.playing else "播放中"
        else:
            host = "宿主已就绪"
        try:
            self._status_host.configure(text=host)
        except Exception:
            pass
        try:
            self._status_dot.delete("all")
            color = self._ui.get("ok", "#7dcea0")
            size = ui_theme.scale_px(self._status_dot, 10)
            pad = max(1, size // 5)
            self._status_dot.configure(
                bg=self._ui.get("surface", "#12181e"), width=size, height=size,
            )
            self._status_dot.create_oval(
                pad, pad, size - pad, size - pad, fill=color, outline=color,
            )
        except Exception:
            pass
        pills = [("v2", "ok"), ("零引导", "")]
        color = getattr(self, "_video_color_info", None) or {}
        if color.get("label"):
            pills.append((color.get("label"), "warn" if color.get("is_hdr") else ""))
        elif self.video:
            pills.append(("SDR · sRGB", ""))
        if self._media_w and self._media_h:
            pills.append((f"{self._media_w}×{self._media_h}", ""))
        if self.video and self.nframes:
            pills.append((f"{self.nframes} 帧", ""))
        settings = getattr(self, "_preview_runtime_settings", None)
        if settings and self.video:
            pills.append((f"缓存 {settings.get('preview_cache_mb', 0)} MiB", "ok"))
        try:
            self._status_chips.set_pills(pills)
        except Exception:
            pass
        try:
            self._status_metric.configure(text="" if self.video else "等待导入")
        except Exception:
            pass

    @staticmethod
    def _timeline_snapshot(timeline):
        return {
            "minimum": timeline._min,
            "maximum": timeline._max,
            "value": timeline.get(),
            "rendered": list(timeline._rendered_ranges),
            "queued": list(timeline._queued_ranges),
        }

    def _sync_preview_chrome(self, timeline_state):
        self.timeline.set_range(timeline_state["minimum"], timeline_state["maximum"])
        self.timeline.set_cache_ranges(
            timeline_state["rendered"], timeline_state["queued"],
        )
        self.timeline.set(timeline_state["value"])
        self._sync_transport_labels()
        self._set_play_btn(self.playing)
        try:
            self.mute_btn.config(
                text="静音" if self._audio.muted else "声音",
                icon="volume-off" if self._audio.muted else "volume",
            )
        except Exception:
            pass
        self._update_zoom_controls()
        self._set_detach_btn(self._preview_detached)
        self._set_fs_btn(self._fullscreen)

    def _set_detach_btn(self, detached):
        try:
            self.detach_btn.config(
                text="停靠" if detached else "分离",
                icon="dock" if detached else "detach",
            )
        except Exception:
            pass

    def _preview_host_window(self):
        window = self._detached_preview_window
        try:
            if window is not None and window.winfo_exists():
                return window
        except Exception:
            pass
        return self.root

    def _focus_preview_host(self):
        target = self._preview_host_window()
        try:
            target.focus_set()
        except Exception:
            pass

    def _sync_window_titles(self):
        filename = os.path.basename(self.video) if self.video else None
        main_suffix = filename or APP_CREDIT
        self.root.title(f"{APP_TITLE} — {main_suffix}")
        window = self._detached_preview_window
        if window is not None:
            try:
                preview_suffix = f"预览 — {filename}" if filename else "独立预览"
                window.title(f"{APP_TITLE} — {preview_suffix}")
            except Exception:
                pass

    def _detached_geometry_for_save(self):
        if self._fullscreen and self._fs_window is self._detached_preview_window:
            return self._fs_geom or self._detached_preview_geometry
        window = self._detached_preview_window
        if window is not None:
            try:
                return window.geometry()
            except Exception:
                pass
        return self._detached_preview_geometry

    def _detached_window_geometry(self):
        try:
            self.root.update_idletasks()
            fallback = (
                1100, 700,
                self.root.winfo_rootx() + 40,
                self.root.winfo_rooty() + 40,
            )
        except Exception:
            fallback = (1100, 700, 48, 48)
        return _clamp_window_geometry(
            self._detached_preview_geometry,
            _virtual_screen_bounds(self.root),
            fallback=fallback,
        )

    def _show_detached_placeholder(self):
        if self._detached_preview_placeholder is None:
            placeholder = ttk.Frame(self.root, padding=(10, 6), style="Status.TFrame")
            ttk.Label(
                placeholder,
                text="预览已在独立窗口中打开",
                style="Status.TLabel",
            ).pack(side="left")
            dock_btn = ChromeButton(
                placeholder, text="停靠回来", command=self.dock_preview,
                variant="ghost", ui=self._ui, width=88,
            )
            dock_btn.pack(side="left", padx=(10, 0))
            self._theme_widgets.append(dock_btn)
            self._detached_preview_placeholder = placeholder
        self._preview_host.pack_forget()
        self._inspector.pack_configure(side="right", fill="both", expand=True)
        self._inspector.pack_propagate(True)
        self._detached_preview_placeholder.pack(
            fill="x", before=self._studio,
        )

    def _hide_detached_placeholder(self):
        if self._detached_preview_placeholder is not None:
            self._detached_preview_placeholder.pack_forget()

    def _on_detached_configure(self, event=None):
        window = self._detached_preview_window
        if (
            window is None or event is None or event.widget is not window
            or self._fullscreen
        ):
            return
        try:
            self._detached_preview_geometry = window.geometry()
        except Exception:
            return
        self._schedule_settings_save()

    def _register_preview_drop_target(self, widget):
        if DND_FILES is None:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as ex:
            self.logln("[拖拽] 独立预览注册失败，仍可点击导入: " + str(ex))

    def toggle_detached_preview(self):
        if self._detached_preview_window is None:
            self.detach_preview()
        else:
            self.dock_preview()

    def detach_preview(self):
        window = self._detached_preview_window
        if window is not None:
            try:
                window.deiconify()
                window.lift()
                window.focus_set()
            except Exception:
                pass
            return
        if self._fullscreen:
            self._exit_fullscreen()
        timeline_state = self._timeline_snapshot(self.timeline)
        window = tk.Toplevel(self.root)
        window.withdraw()
        ui_theme.apply_app_icon(window)
        window.resizable(True, True)
        bounds = _virtual_screen_bounds(self.root)
        window.minsize(min(320, bounds[2]), min(240, bounds[3]))
        window.geometry(self._detached_window_geometry())
        window.protocol("WM_DELETE_WINDOW", self.dock_preview)
        window.bind("<Configure>", self._on_detached_configure)
        window.bind("<FocusOut>", self._on_root_focus_out)
        pane = self._create_preview_pane(window, detached=True)
        ui_theme.apply_native_titlebar(
            window, self._ui, self._ui_theme_name == "dark",
        )

        self._docked_preview_pane["canvas"].pack_forget()
        self._docked_preview_pane["transport"].pack_forget()
        self._preview_host.pack_forget()
        self._detached_preview_window = window
        self._detached_preview_pane = pane
        self._preview_detached = True
        self._activate_preview_pane(pane)
        self._sync_preview_chrome(timeline_state)
        self._show_detached_placeholder()
        self._register_preview_drop_target(self.canvas)
        self._sync_window_titles()
        self._schedule_settings_save()

        try:
            if self.root.state() != "withdrawn":
                window.deiconify()
                window.lift()
                window.focus_set()
        except Exception:
            window.deiconify()
        self._refresh_preview_surface()

    def dock_preview(self):
        window = self._detached_preview_window
        if window is None:
            return
        if self._fullscreen:
            self._exit_fullscreen()
        try:
            self._detached_preview_geometry = window.geometry()
        except Exception:
            pass
        timeline_state = self._timeline_snapshot(self.timeline)
        self._preview_detached = False
        self._activate_preview_pane(self._docked_preview_pane)
        self._sync_preview_chrome(timeline_state)
        detached_pane = self._detached_preview_pane
        self._detached_preview_window = None
        self._detached_preview_pane = None
        if detached_pane is not None:
            stale_ids = {
                id(widget) for widget in detached_pane.get("theme_widgets", ())
            }
            self._theme_widgets = [
                widget for widget in self._theme_widgets if id(widget) not in stale_ids
            ]
        try:
            ui_theme.release_app_icon(window)
            window.destroy()
        except Exception:
            pass
        self._hide_detached_placeholder()
        self._inspector.pack_configure(side="right", fill="y", expand=False)
        self._inspector.pack_propagate(False)
        self._preview_host.pack(side="left", fill="both", expand=True, before=self._inspector)
        self.canvas.pack(fill="both", expand=True)
        self.transport.pack(fill="x", padx=10, pady=(0, 10))
        self._sync_window_titles()
        self._schedule_settings_save()
        self._focus_preview_host()
        self._refresh_preview_surface()

    def _resize_preview_content(self, event=None):
        """Keep the settings content flush with the viewport width."""
        width = max(getattr(event, "width", self._preview_canvas.winfo_width()), 1)
        self._preview_canvas.itemconfigure(self._preview_window, width=width)
        self.root.after_idle(self._sync_preview_scrollregion)

    def _sync_preview_scrollregion(self, event=None):
        """Update the scroll range and only show the bar when content overflows."""
        if not hasattr(self, "_preview_canvas"):
            return
        self._preview_canvas.update_idletasks()
        content_height = self.preview_tab.winfo_reqheight()
        viewport_height = self._preview_canvas.winfo_height()
        self._preview_canvas.configure(
            scrollregion=(0, 0, self._preview_canvas.winfo_width(), content_height),
        )
        needs_scrollbar = content_height > viewport_height + 2
        if needs_scrollbar == self._preview_scrollbar_visible:
            return
        self._preview_scrollbar_visible = needs_scrollbar
        if needs_scrollbar:
            self._preview_scrollbar.grid()
        else:
            self._preview_canvas.yview_moveto(0.0)
            self._preview_scrollbar.grid_remove()

    def _resize_export_content(self, event=None):
        width = max(getattr(event, "width", self._export_canvas.winfo_width()), 1)
        self._export_canvas.itemconfigure(self._export_window, width=width)
        self.root.after_idle(self._sync_export_scrollregion)

    def _sync_export_scrollregion(self, event=None):
        if not hasattr(self, "_export_canvas"):
            return
        inner = self._export_section.master
        self._export_canvas.update_idletasks()
        content_height = inner.winfo_reqheight()
        viewport_height = self._export_canvas.winfo_height()
        self._export_canvas.configure(
            scrollregion=(0, 0, self._export_canvas.winfo_width(), content_height),
        )
        needs_scrollbar = content_height > viewport_height + 2
        if needs_scrollbar == getattr(self, "_export_scrollbar_visible", False):
            return
        self._export_scrollbar_visible = needs_scrollbar
        if needs_scrollbar:
            self._export_scrollbar.grid()
        else:
            self._export_canvas.yview_moveto(0.0)
            self._export_scrollbar.grid_remove()

    def _on_workspace_mousewheel(self, event):
        """Scroll inspector pages when the pointer is over them."""
        widget = getattr(event, "widget", None)
        target = None
        cursor = widget
        while cursor is not None:
            if cursor is self._preview_page and getattr(self, "_preview_scrollbar_visible", False):
                target = self._preview_canvas
                break
            if cursor is self._export_page and getattr(self, "_export_scrollbar_visible", False):
                target = self._export_canvas
                break
            cursor = getattr(cursor, "master", None)
        if target is None:
            return None
        delta = getattr(event, "delta", 0)
        if not delta:
            return None
        target.yview_scroll(-3 if delta > 0 else 3, "units")
        return "break"

    # ---------- batch export queue ----------
    def _chrome_button(
        self, parent, text, command, variant="default", width=None,
        icon="", icon_only=False, primary=None,
    ):
        button = ChromeButton(
            parent, text=text, command=command, variant=variant, ui=self._ui,
            width=width, icon=icon, icon_only=icon_only, primary=primary,
        )
        self._theme_widgets.append(button)
        return button

    def _chrome_combo(self, parent, variable, values, **kwargs):
        kwargs.setdefault("state", "readonly")
        widget = ChromeCombobox(
            parent, ui=self._ui, textvariable=variable, values=values, **kwargs,
        )
        self._theme_widgets.append(widget)
        return widget

    def _chrome_spin(self, parent, **kwargs):
        widget = ChromeSpinbox(parent, ui=self._ui, **kwargs)
        self._theme_widgets.append(widget)
        return widget

    def _build_queue_tab(self, parent):
        toolbar = ttk.Frame(parent, style="Panel.TFrame")
        toolbar.pack(fill="x", padx=12, pady=(10, 6))
        self.queue_add_files_btn = self._chrome_button(
            toolbar, "添加文件", self.add_queue_files, width=36,
            icon="file-plus", icon_only=True,
        )
        self.queue_add_files_btn.pack(side="left")
        Tooltip(self.queue_add_files_btn, "添加文件")
        self.queue_add_folder_btn = self._chrome_button(
            toolbar, "添加文件夹", self.add_queue_folder, width=36,
            icon="folder-plus", icon_only=True,
        )
        self.queue_add_folder_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.queue_add_folder_btn, "添加文件夹")
        self.queue_remove_btn = self._chrome_button(
            toolbar, "移除", self.remove_selected_queue_jobs, variant="ghost", width=36,
            icon="trash", icon_only=True,
        )
        self.queue_remove_btn.pack(side="left", padx=(8, 0))
        Tooltip(self.queue_remove_btn, "移除所选任务")
        self.queue_clear_btn = self._chrome_button(
            toolbar, "清空队列", self.clear_queue_jobs, variant="ghost", width=36,
            icon="clear", icon_only=True,
        )
        self.queue_clear_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.queue_clear_btn, "清空队列")
        self.queue_retry_btn = self._chrome_button(
            toolbar, "重试", self.retry_selected_queue_jobs, variant="ghost",
            width=36, icon="retry", icon_only=True,
        )
        self.queue_retry_btn.pack(side="left", padx=(8, 0))
        Tooltip(self.queue_retry_btn, "重试所选失败任务")
        self.queue_clear_done_btn = self._chrome_button(
            toolbar, "清理已完成", self.clear_completed_queue_jobs, variant="ghost",
            width=36, icon="clear-done", icon_only=True,
        )
        self.queue_clear_done_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.queue_clear_done_btn, "清理已完成任务")
        self.queue_move_down_btn = self._chrome_button(
            toolbar, "下移", lambda: self.move_selected_queue_job(1), variant="ghost",
            width=36, icon="down", icon_only=True,
        )
        self.queue_move_down_btn.pack(side="right")
        Tooltip(self.queue_move_down_btn, "下移所选任务")
        self.queue_move_up_btn = self._chrome_button(
            toolbar, "上移", lambda: self.move_selected_queue_job(-1), variant="ghost",
            width=36, icon="up", icon_only=True,
        )
        self.queue_move_up_btn.pack(side="right", padx=(0, 4))
        Tooltip(self.queue_move_up_btn, "上移所选任务")

        output_row = ttk.Frame(parent, style="Panel.TFrame")
        output_row.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(output_row, text="输出目录").pack(side="left")
        self.queue_output_dir_var = tk.StringVar(
            value=self._saved_settings.get("queue_output_dir", "")
        )
        self.queue_output_entry = ChromeEntry(
            output_row, ui=self._ui, textvariable=self.queue_output_dir_var,
        )
        self.queue_output_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self._theme_widgets.append(self.queue_output_entry)
        self.queue_output_entry.bind("<FocusOut>", self._on_queue_output_dir_change)
        self.queue_output_entry.bind("<Return>", self._on_queue_output_dir_change)
        self.queue_output_browse_btn = self._chrome_button(
            output_row, "浏览…", self.choose_queue_output_dir, variant="ghost",
            width=36, icon="folder-open", icon_only=True,
        )
        self.queue_output_browse_btn.pack(side="left")
        Tooltip(self.queue_output_browse_btn, "选择输出目录")
        Tooltip(
            self.queue_output_entry,
            "仅影响之后添加的任务。留空时输出到各源媒体所在目录；队列会自动避免覆盖已有文件。",
        )

        tree_frame = ttk.Frame(parent, style="Panel.TFrame")
        tree_frame.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self._queue_tree_shell = tk.Frame(
            tree_frame, bg=self._ui.get("line", "#2a3540"), highlightthickness=0, bd=0,
        )
        self._queue_tree_shell.grid(row=0, column=0, sticky="nsew")
        columns = ("state", "source", "info", "settings", "output", "progress")
        self.queue_tree = ttk.Treeview(
            self._queue_tree_shell, columns=columns, show="headings", height=8,
            selectmode="extended", displaycolumns=("state", "source", "progress"),
        )
        headings = {
            "state": "状态", "source": "文件", "info": "素材信息",
            "settings": "参数", "output": "输出", "progress": "进度",
        }
        widths = {
            "state": 76, "source": 220, "info": 155,
            "settings": 160, "output": 180, "progress": 120,
        }
        for name in columns:
            self.queue_tree.heading(name, text=headings[name])
            self.queue_tree.column(
                name, width=widths[name], minwidth=60,
                stretch=name in {"source", "output"}, anchor="w",
            )
        self.queue_tree.pack(fill="both", expand=True, padx=1, pady=1)
        queue_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=queue_y.set)
        queue_y.grid(row=0, column=1, sticky="ns")
        def fit_queue_columns(event):
            width = max(event.width, 1)
            self.queue_tree.column("state", width=48, minwidth=40, stretch=False)
            self.queue_tree.column("progress", width=64, minwidth=48, stretch=False)
            self.queue_tree.column("source", width=max(60, width - 114), minwidth=60, stretch=True)
        self.queue_tree.bind("<Configure>", fit_queue_columns, add="+")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.queue_tree.tag_configure("failed", foreground=self._ui_color("failed", "#a12622"))
        self.queue_tree.tag_configure("interrupted", foreground=self._ui_color("interrupted", "#8a5a00"))
        self.queue_tree.tag_configure("completed", foreground=self._ui_color("completed", "#226b32"))
        self.queue_tree.bind("<<TreeviewSelect>>", self._on_queue_tree_select)
        self.queue_tree.bind("<Double-Button-1>", lambda event: self.load_selected_queue_job())

        details_row = ttk.Frame(parent, style="Panel.TFrame")
        details_row.pack(fill="x", padx=12, pady=(0, 6))
        self.queue_details = ttk.Label(
            details_row, text="选择任务查看详情。",
            anchor="w", justify="left", wraplength=280, style="Hint.TLabel",
        )
        self.queue_details.pack(side="top", fill="x", pady=(4, 8))
        details_row.bind(
            "<Configure>",
            lambda event: self.queue_details.config(wraplength=max(event.width - 12, 160)),
        )

        footer = ttk.Frame(parent, style="Panel.TFrame")
        footer.pack(fill="x", padx=12, pady=(0, 10))
        actions = ttk.Frame(footer, style="Panel.TFrame")
        actions.pack(fill="x")
        for column in range(2):
            actions.columnconfigure(column, weight=1, uniform="queue_actions")
        self.queue_load_btn = self._chrome_button(
            actions, "载入预览", self.load_selected_queue_job, variant="ghost",
        )
        self.queue_load_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 6))
        self.queue_apply_settings_btn = self._chrome_button(
            actions, "应用参数", self.apply_current_settings_to_queue, variant="ghost",
        )
        self.queue_apply_settings_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 6))
        Tooltip(self.queue_apply_settings_btn, "把当前调参和导出设置应用到所选任务")
        run_bar = ttk.Frame(actions, style="Panel.TFrame")
        run_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        for column in range(2):
            run_bar.columnconfigure(column, weight=1, uniform="queue_run")
        self._queue_run_bar = run_bar
        self.queue_start_btn = self._chrome_button(
            run_bar, "开始队列", self.start_export_queue, variant="accent",
            icon="play",
        )
        self.queue_pause_btn = self._chrome_button(
            run_bar, "暂停", self.pause_export_queue_after_current,
            variant="outline", icon="pause", primary=True,
        )
        Tooltip(self.queue_pause_btn, "当前项完成后暂停队列")
        self.queue_cancel_btn = self._chrome_button(
            run_bar, "取消", self.cancel_current_queue_job,
            variant="danger", icon="cancel", primary=True,
        )
        Tooltip(self.queue_cancel_btn, "取消当前任务并暂停队列，未完成文件会清理")
        self.queue_pause_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.queue_cancel_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.queue_pause_btn.grid_remove()
        self.queue_cancel_btn.grid_remove()
        self.queue_start_btn.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._queue_details_tip = Tooltip(self.queue_details, "")
        self._queue_run_layout = False

    def _save_queue_state(self):
        try:
            export_queue_state.save(self._queue_jobs)
        except Exception as ex:
            if hasattr(self, "log"):
                self.logln("[队列] 保存失败: " + str(ex))

    def _queue_job(self, job_id):
        return next((job for job in self._queue_jobs if job.job_id == job_id), None)

    def _selected_queue_jobs(self):
        if not hasattr(self, "queue_tree"):
            return []
        selected = set(self.queue_tree.selection())
        return [job for job in self._queue_jobs if job.job_id in selected]

    @staticmethod
    def _queue_info_text(job):
        meta = job.metadata or {}
        try:
            width = int(meta.get("width", 0) or 0)
            height = int(meta.get("height", 0) or 0)
            fps = float(meta.get("fps", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            width, height, fps = 0, 0, 0.0
        size = f"{width}×{height}" if width and height else "尺寸未知"
        if job.media_kind == "image":
            return f"图片 · {size} · SDR"
        color = (job.color_info or {}).get("label", "待检测")
        return f"视频 · {size} · {fps:g} fps · {color}" if fps else f"视频 · {size} · {color}"

    @staticmethod
    def _queue_settings_text(job):
        settings = job.settings or {}
        export = job.export_settings or {}
        style = STYLE_NAMES.get(settings.get("style"), "默认")
        scale = normalize_scale(
            export.get("super_resolution_scale", settings.get("super_resolution_scale", 1))
        )
        scale_note = f" · RTX超分{scale}×" if scale > 1 else ""
        if job.media_kind == "image":
            image_format = os.path.splitext(job.output_path)[1].upper().lstrip(".") or "PNG"
            return f"{style} · {image_format}{scale_note}"
        if export.get("rate_control") == "bitrate":
            try:
                bitrate = float(export.get("video_bitrate_mbps", 20))
            except (TypeError, ValueError, OverflowError):
                bitrate = 20.0
            quality = f"{bitrate:g} Mbps"
        else:
            quality = QUALITY_PROFILE_NAMES.get(export.get("quality_profile"), "高质量（推荐）")
        mode = "严格" if export.get("mode", "single") == "single" else "并行"
        container = resolve_output_container(
            job.source_path, export.get("output_container", "mp4")
        )
        hdr_active = bool(export.get("hdr_mode") and (job.color_info or {}).get("is_hdr"))
        codec = "HEVC10" if hdr_active else "H.264"
        return (
            f"{style} · {OUTPUT_CONTAINER_LABELS[container]}/{codec} · "
            f"{quality} · {mode}{scale_note}"
        )

    @staticmethod
    def _queue_details_summary(job):
        settings = job.settings or {}
        export = job.export_settings or {}
        style = STYLE_NAMES.get(settings.get("style"), "默认")
        scale = normalize_scale(
            export.get("super_resolution_scale", settings.get("super_resolution_scale", 1))
        )
        bits = [style]
        if job.media_kind == "image":
            image_format = os.path.splitext(job.output_path or job.source_path)[1].upper().lstrip(".") or "PNG"
            bits.extend((image_format, "原尺寸"))
        else:
            if export.get("rate_control") == "bitrate":
                try:
                    bitrate = float(export.get("video_bitrate_mbps", 20))
                except (TypeError, ValueError, OverflowError):
                    bitrate = 20.0
                bits.append(f"{bitrate:g} Mbps")
            else:
                bits.append(QUALITY_PROFILE_NAMES.get(export.get("quality_profile"), "均衡"))
            bits.append("严格单会话" if export.get("mode", "single") == "single" else "并行分段")
        if scale > 1:
            bits.append(f"{scale}×超分")
        return " · ".join(bits)

    @staticmethod
    def _queue_progress_text(job):
        if job.state == "completed":
            return "100%"
        if job.state in {"failed", "cancelled", "interrupted"}:
            if job.progress_total > 0:
                pct = 100.0 * min(job.progress_done, job.progress_total) / job.progress_total
                return f"{pct:.0f}% · 可重试"
            return "可重试"
        if job.progress_total > 0:
            pct = 100.0 * min(job.progress_done, job.progress_total) / job.progress_total
            return f"{pct:.0f}% · {job.progress_done}/{job.progress_total}"
        return "—"

    def _queue_row_values(self, job):
        return (
            QUEUE_STATE_NAMES.get(job.state, job.state),
            os.path.basename(job.source_path),
            self._queue_info_text(job),
            self._queue_settings_text(job),
            os.path.basename(job.output_path),
            self._queue_progress_text(job),
        )

    def _update_queue_job_row(self, job):
        if not hasattr(self, "queue_tree"):
            return
        values = self._queue_row_values(job)
        tags = (job.state,) if job.state in {"failed", "interrupted", "completed"} else ()
        if self.queue_tree.exists(job.job_id):
            self.queue_tree.item(job.job_id, values=values, tags=tags)
        else:
            self.queue_tree.insert("", "end", iid=job.job_id, values=values, tags=tags)

    def _refresh_queue_tree(self, keep_selection=True):
        if not hasattr(self, "queue_tree"):
            return
        selected = set(self.queue_tree.selection()) if keep_selection else set()
        current_ids = set(self.queue_tree.get_children(""))
        job_ids = {job.job_id for job in self._queue_jobs}
        for removed in current_ids - job_ids:
            self.queue_tree.delete(removed)
        for index, job in enumerate(self._queue_jobs):
            self._update_queue_job_row(job)
            self.queue_tree.move(job.job_id, "", index)
        restored = [job.job_id for job in self._queue_jobs if job.job_id in selected]
        if restored:
            self.queue_tree.selection_set(restored)
        failed = sum(job.state in {"failed", "interrupted"} for job in self._queue_jobs)
        badge = str(len(self._queue_jobs))
        if failed:
            badge = f"{len(self._queue_jobs)}!"
        try:
            self.workspace_tabs.tab(self.queue_tab, text="队列")
            self.workspace_tabs.set_badge(self.queue_tab, badge)
        except Exception:
            pass
        self._update_queue_action_states()
        self._on_queue_tree_select()

    def _update_queue_action_states(self):
        if not hasattr(self, "queue_start_btn"):
            return
        selected = self._selected_queue_jobs()
        editable = not self._queue_running and not self._exporting and not self._diagnosing
        retryable = any(
            job.state in {"failed", "cancelled", "interrupted"} for job in selected
        )
        completed = any(job.state == "completed" for job in self._queue_jobs)
        for widget in (
            self.queue_add_files_btn, self.queue_add_folder_btn,
            self.queue_output_entry, self.queue_output_browse_btn,
        ):
            self._set_ttk_enabled(widget, editable)
        self._set_ttk_enabled(self.queue_remove_btn, editable and bool(selected))
        self._set_ttk_enabled(
            self.queue_clear_btn, editable and bool(self._queue_jobs),
        )
        self._set_ttk_enabled(self.queue_retry_btn, editable and retryable)
        self._set_ttk_enabled(self.queue_clear_done_btn, editable and completed)
        self._set_ttk_enabled(self.queue_move_up_btn, editable and len(selected) == 1)
        self._set_ttk_enabled(self.queue_move_down_btn, editable and len(selected) == 1)
        self._set_ttk_enabled(self.queue_load_btn, editable and len(selected) == 1)
        self._set_ttk_enabled(
            self.queue_apply_settings_btn,
            editable and bool(selected) and all(job.state != "completed" for job in selected),
        )
        self._layout_queue_run_controls(self._queue_running)
        startable = any(job.state in QUEUE_STARTABLE_STATES for job in self._queue_jobs)
        self._set_ttk_enabled(self.queue_start_btn, editable and startable)
        self._set_ttk_enabled(self.queue_pause_btn, self._queue_running)
        active_job = self._queue_job(self._queue_active_job_id)
        self._set_ttk_enabled(
            self.queue_cancel_btn,
            self._queue_running
            and active_job is not None
            and active_job.media_kind == "video"
            and self._exporting,
        )
        resume = startable and self._queue_last_summary == "paused"
        self.queue_start_btn.config(
            text="继续队列" if resume else "开始队列",
            icon="play",
        )
        self.queue_pause_btn.config(
            text="将暂停" if self._queue_pause_requested else "暂停",
        )

    def _layout_queue_run_controls(self, running):
        running = bool(running)
        if running:
            self.queue_start_btn.grid_remove()
            self.queue_pause_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
            self.queue_cancel_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
            try:
                self.queue_pause_btn.lift()
                self.queue_cancel_btn.lift()
            except tk.TclError:
                pass
        else:
            self.queue_pause_btn.grid_remove()
            self.queue_cancel_btn.grid_remove()
            self.queue_start_btn.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._queue_run_layout = running

    def _on_queue_tree_select(self, event=None):
        selected = self._selected_queue_jobs()
        if not selected:
            text = (
                "添加文件、文件夹或拖入素材。"
                if not self._queue_jobs else
                "选择任务查看详情。"
            )
        elif len(selected) > 1:
            text = f"已选择 {len(selected)} 个任务。"
        else:
            job = selected[0]
            src = os.path.basename(job.source_path)
            out = os.path.basename(job.output_path or "")
            text = f"{src}  →  {out}\n{self._queue_details_summary(job)}"
            if job.error:
                text += "\n错误：" + job.error
            tip = getattr(self, "_queue_details_tip", None)
            if tip is not None:
                tip.text = f"输入：{job.source_path}\n输出：{job.output_path or ''}"
        if not selected or len(selected) > 1:
            tip = getattr(self, "_queue_details_tip", None)
            if tip is not None:
                tip.text = ""
        if hasattr(self, "queue_details"):
            self.queue_details.config(text=text)
        self._update_queue_action_states()

    def _on_queue_output_dir_change(self, event=None):
        value = self.queue_output_dir_var.get().strip()
        if value:
            value = os.path.abspath(os.path.normpath(value))
            self.queue_output_dir_var.set(value)
        self._schedule_settings_save()
        return "break" if event is not None and getattr(event, "keysym", "") == "Return" else None

    def choose_queue_output_dir(self):
        initial = self.queue_output_dir_var.get().strip() or os.getcwd()
        path = filedialog.askdirectory(initialdir=initial)
        if path:
            self.queue_output_dir_var.set(os.path.abspath(os.path.normpath(path)))
            self._schedule_settings_save()

    @staticmethod
    def _unique_target_path(candidate, reserved=()):
        candidate = os.path.abspath(candidate)
        reserved = {os.path.normcase(os.path.abspath(path)) for path in reserved}
        stem, ext = os.path.splitext(candidate)
        index = 1
        result = candidate
        while os.path.exists(result) or os.path.normcase(result) in reserved:
            index += 1
            result = f"{stem}_{index}{ext}"
        return result

    def _new_queue_output_path(
        self, source_path, media_kind=None, export_settings=None, exclude_job_id=None,
    ):
        output_dir = self.queue_output_dir_var.get().strip()
        if not output_dir:
            output_dir = os.path.dirname(source_path)
        output_dir = os.path.abspath(os.path.normpath(output_dir))
        media_kind = media_kind or ("image" if _is_image_path(source_path) else "video")
        stem = os.path.splitext(os.path.basename(source_path))[0] + "_dlss"
        if media_kind == "image":
            extension = os.path.splitext(source_path)[1].lower()
            if extension not in IMAGE_ENCODE_EXTS:
                extension = ".png"
        else:
            export_settings = export_settings or self._collect_export_settings()
            container = resolve_output_container(
                source_path, export_settings.get("output_container", "mp4")
            )
            extension = output_container_extension(container)
        candidate = os.path.join(output_dir, stem + extension)
        reserved = [
            job.output_path for job in self._queue_jobs if job.job_id != exclude_job_id
        ]
        return self._unique_target_path(candidate, reserved)

    def _normalize_queue_output_path(self, job):
        if job.media_kind == "image":
            expected_ext = os.path.splitext(job.source_path)[1].lower()
            if expected_ext not in IMAGE_ENCODE_EXTS:
                expected_ext = ".png"
        else:
            container = resolve_output_container(
                job.source_path,
                (job.export_settings or {}).get("output_container", "mp4"),
            )
            expected_ext = output_container_extension(container)
        if os.path.splitext(job.output_path)[1].lower() == expected_ext:
            return job.output_path
        candidate = os.path.splitext(job.output_path)[0] + expected_ext
        return self._unique_target_path(
            candidate,
            reserved=[
                item.output_path for item in self._queue_jobs
                if item.job_id != job.job_id
            ],
        )

    def _probe_queue_video(self, path):
        cap = cv2.VideoCapture(path)
        try:
            if not cap.isOpened():
                raise RuntimeError("无法打开视频，请检查文件是否损坏或编码是否受支持。")
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if width <= 0 or height <= 0:
                raise RuntimeError("无法读取视频尺寸。")
        finally:
            cap.release()
        try:
            color_info = probe_video_stream(find_ffmpeg(), path)
        except Exception as ex:
            color_info = {"is_hdr": False, "profile": "srgb", "label": "SDR / sRGB"}
            self.logln(f"[队列色彩检测] {os.path.basename(path)} 按 SDR 处理: {ex}")
        metadata = {
            "frames": frames, "fps": fps, "width": width, "height": height,
            "duration": frames / max(fps, 1.0),
        }
        return metadata, color_info

    def _probe_queue_media(self, path):
        if _is_image_path(path):
            image = _read_image_bgr(path)
            if image is None or image.size == 0:
                raise RuntimeError("无法读取图片，请检查文件是否损坏。")
            height, width = image.shape[:2]
            return {
                "frames": 1, "fps": 0.0, "width": width, "height": height,
                "duration": 0.0,
            }, {"is_hdr": False, "profile": "srgb", "label": "SDR 图片"}
        return self._probe_queue_video(path)

    def add_queue_files(self):
        if self._queue_running or self._exporting:
            return
        paths = filedialog.askopenfilenames(filetypes=VIDEO_FILETYPES)
        if paths:
            self._add_paths_to_queue(paths)

    def add_queue_folder(self):
        if self._queue_running or self._exporting:
            return
        folder = filedialog.askdirectory()
        if not folder:
            return
        paths = []
        for current, _dirs, files in os.walk(folder):
            for name in sorted(files):
                path = os.path.join(current, name)
                if _is_video_path(path) or _is_image_path(path):
                    paths.append(path)
        self._add_paths_to_queue(paths)

    def add_current_to_queue(self):
        if not self.video:
            messagebox.showwarning("加入队列", "请先导入一个视频或图片。")
            return
        self._add_paths_to_queue([self.video])

    def _add_paths_to_queue(self, paths, switch_tab=True):
        if self._queue_running or self._exporting:
            messagebox.showinfo("队列忙", "请先暂停或完成当前队列。")
            return 0
        normalized = []
        for raw in paths:
            path = os.path.abspath(os.path.normpath(str(raw)))
            if os.path.isfile(path) and (_is_video_path(path) or _is_image_path(path)):
                normalized.append(path)
        if not normalized:
            messagebox.showwarning("添加到队列", "没有找到受支持的视频或图片文件。")
            return 0
        existing = {os.path.normcase(job.source_path) for job in self._queue_jobs}
        settings = self._collect_settings()
        export_settings = self._collect_export_settings()
        added = 0
        duplicates = 0
        invalid = 0
        for path in normalized:
            key = os.path.normcase(path)
            if key in existing:
                duplicates += 1
                continue
            media_kind = "image" if _is_image_path(path) else "video"
            output = self._new_queue_output_path(path, media_kind, export_settings)
            try:
                if os.path.normcase(path) == os.path.normcase(self.video or ""):
                    metadata = {
                        "frames": self.nframes, "fps": self.fps,
                        "width": self._media_w, "height": self._media_h,
                        "duration": (
                            self.nframes / max(float(self.fps), 1.0)
                            if media_kind == "video" else 0.0
                        ),
                    }
                    color_info = dict(self._video_color_info or {})
                    if media_kind == "image":
                        color_info = {"is_hdr": False, "profile": "srgb", "label": "SDR 图片"}
                else:
                    metadata, color_info = self._probe_queue_media(path)
                effective_export = dict(export_settings)
                if (
                    media_kind == "video"
                    and effective_export.get("hdr_mode")
                    and color_info.get("is_hdr")
                ):
                    effective_export["mode"] = "single"
                if normalize_scale(effective_export.get("super_resolution_scale", 1)) > 1:
                    effective_export["mode"] = "single"
                job = export_queue_state.ExportJob.create(
                    path, output, settings, effective_export, metadata, color_info,
                    media_kind=media_kind,
                )
            except Exception as ex:
                job = export_queue_state.ExportJob.create(
                    path, output, settings, export_settings, media_kind=media_kind,
                )
                job.state = "failed"
                job.error = str(ex)
                invalid += 1
            self._queue_jobs.append(job)
            existing.add(key)
            added += 1
            self.root.update_idletasks()
        self._save_queue_state()
        self._refresh_queue_tree(keep_selection=False)
        if self._queue_jobs:
            last = self._queue_jobs[-1]
            self.queue_tree.selection_set(last.job_id)
            self.queue_tree.see(last.job_id)
        if switch_tab:
            self.workspace_tabs.select(self.queue_tab)
        note = f"已添加 {added} 个媒体文件"
        if duplicates:
            note += f"，跳过 {duplicates} 个重复项"
        if invalid:
            note += f"，{invalid} 个需要修复或重试"
        self.logln("[队列] " + note)
        return added

    def remove_selected_queue_jobs(self):
        if self._queue_running or self._exporting:
            return
        selected = {job.job_id for job in self._selected_queue_jobs()}
        if not selected:
            return
        self._queue_jobs = [job for job in self._queue_jobs if job.job_id not in selected]
        self._save_queue_state()
        self._refresh_queue_tree(keep_selection=False)

    def clear_completed_queue_jobs(self):
        if self._queue_running or self._exporting:
            return
        self._queue_jobs = [job for job in self._queue_jobs if job.state != "completed"]
        self._save_queue_state()
        self._refresh_queue_tree(keep_selection=False)

    def clear_queue_jobs(self):
        if self._queue_running or self._exporting:
            return
        if not self._queue_jobs:
            return
        count = len(self._queue_jobs)
        if not messagebox.askyesno("清空队列", f"移除全部 {count} 个任务？"):
            return
        self._queue_jobs = []
        self._save_queue_state()
        self._refresh_queue_tree(keep_selection=False)

    def retry_selected_queue_jobs(self):
        if self._queue_running or self._exporting:
            return
        changed = False
        for job in self._selected_queue_jobs():
            if job.state in {"failed", "cancelled", "interrupted"}:
                job.reset_for_retry()
                changed = True
        if changed:
            self._queue_last_summary = None
            self._save_queue_state()
            self._refresh_queue_tree()

    def move_selected_queue_job(self, direction):
        if self._queue_running or self._exporting:
            return
        selected = self._selected_queue_jobs()
        if len(selected) != 1:
            return
        job = selected[0]
        index = self._queue_jobs.index(job)
        target = max(0, min(index + int(direction), len(self._queue_jobs) - 1))
        if target == index:
            return
        self._queue_jobs.pop(index)
        self._queue_jobs.insert(target, job)
        self._save_queue_state()
        self._refresh_queue_tree()
        self.queue_tree.see(job.job_id)

    def apply_current_settings_to_queue(self):
        if self._queue_running or self._exporting:
            return
        settings = self._collect_settings()
        export_settings = self._collect_export_settings()
        changed = False
        for job in self._selected_queue_jobs():
            if job.state == "completed":
                continue
            job.settings = dict(settings)
            job.export_settings = dict(export_settings)
            if (
                job.media_kind == "video"
                and job.export_settings.get("hdr_mode")
                and (job.color_info or {}).get("is_hdr")
            ):
                job.export_settings["mode"] = "single"
            if normalize_scale(job.export_settings.get("super_resolution_scale", 1)) > 1:
                job.export_settings["mode"] = "single"
            job.output_path = self._new_queue_output_path(
                job.source_path, job.media_kind, job.export_settings,
                exclude_job_id=job.job_id,
            )
            changed = True
        if changed:
            self._save_queue_state()
            self._refresh_queue_tree()

    def load_selected_queue_job(self):
        selected = self._selected_queue_jobs()
        if len(selected) != 1 or self._queue_running or self._exporting:
            return
        job = selected[0]
        if self._load_media(job.source_path):
            self.workspace_tabs.select(self._preview_page)

    def _prepare_queue_jobs_for_start(self):
        changed = False
        for job in self._queue_jobs:
            if job.state in {"cancelled", "interrupted"}:
                job.reset_for_retry()
                changed = True
        if changed:
            self._save_queue_state()
            self._refresh_queue_tree()
        return changed

    def start_export_queue(self):
        if self._queue_running or self._exporting:
            return
        if not any(job.state in QUEUE_STARTABLE_STATES for job in self._queue_jobs):
            self.logln("[队列] 没有等待处理的任务")
            return
        plans = []
        risk_order = {"low": 0, "unknown": 1, "medium": 2, "high": 3, "extreme": 4}
        gpu_memory = query_gpu_memory(cache_seconds=0)
        for job in self._queue_jobs:
            if job.state not in QUEUE_STARTABLE_STATES:
                continue
            export = job.export_settings or {}
            settings = job.settings or {}
            scale = normalize_scale(
                export.get("super_resolution_scale", settings.get("super_resolution_scale", 1))
            )
            if scale == 1:
                continue
            metadata = job.metadata or {}
            try:
                width = int(metadata.get("width", 0) or 0)
                height = int(metadata.get("height", 0) or 0)
            except (TypeError, ValueError):
                continue
            if width <= 0 or height <= 0:
                continue
            is_hdr = bool(
                job.media_kind == "video" and export.get("hdr_mode")
                and (job.color_info or {}).get("is_hdr")
            )
            estimate = estimate_resources(width, height, scale, is_hdr=is_hdr)
            risk = classify_resource_risk(estimate, gpu_memory)
            plans.append((risk_order.get(risk, 1), width, height, scale, is_hdr))
        if plans:
            _rank, width, height, scale, is_hdr = max(plans)
            if not self._confirm_super_resolution_export(
                width, height, scale, is_hdr=is_hdr, notify=True,
            ):
                return
            for _rank, plan_width, plan_height, plan_scale, plan_hdr in plans:
                self._confirmed_super_resolution_plans.add(
                    (plan_width, plan_height, plan_scale, plan_hdr)
                )
        self._prepare_queue_jobs_for_start()
        self.pause()
        self._queue_running = True
        self._queue_pause_requested = False
        self._queue_last_summary = None
        self.workspace_tabs.select(self.queue_tab)
        self.logln("[队列] 开始串行处理媒体任务")
        self._update_action_labels()
        self._update_host_control_states()
        self._update_queue_action_states()
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        self.root.after_idle(self._run_next_queue_job)

    def _run_next_queue_job(self):
        if not self._queue_running:
            return
        if self._queue_pause_requested:
            self._finish_queue_run(paused=True)
            return
        job = next((item for item in self._queue_jobs if item.state == "pending"), None)
        if job is None:
            self._finish_queue_run(paused=False)
            return
        self._queue_active_job_id = job.job_id
        job.state = "running"
        job.error = ""
        job.progress_done = 0
        try:
            job.progress_total = max(int(job.metadata.get("frames", 0) or 0), 0)
        except (TypeError, ValueError, OverflowError):
            job.progress_total = 0
        job.progress_label = "准备导出"
        job.started_at = time.time()
        job.finished_at = 0.0
        try:
            job.output_path = self._normalize_queue_output_path(job)
            output_dir = os.path.dirname(job.output_path) or os.getcwd()
            os.makedirs(output_dir, exist_ok=True)
            if os.path.exists(job.output_path):
                job.output_path = self._unique_target_path(
                    job.output_path,
                    reserved=[
                        item.output_path for item in self._queue_jobs
                        if item.job_id != job.job_id
                    ],
                )
            self._save_queue_state()
            self._refresh_queue_tree()
            self.queue_tree.selection_set(job.job_id)
            self.queue_tree.see(job.job_id)
            self.logln(f"[队列] 开始: {job.source_path}")
            if job.media_kind == "image":
                image_settings = dict(job.settings)
                image_settings['super_resolution_scale'] = normalize_scale(
                    (job.export_settings or {}).get(
                        'super_resolution_scale', image_settings.get('super_resolution_scale', 1)
                    )
                )
                result = self._export_image_source(
                    job.source_path,
                    settings=image_settings,
                    out_path=job.output_path,
                    notify=False,
                )
            else:
                result = self._export_video_source(
                    job.source_path,
                    settings=dict(job.settings),
                    export_settings=dict(job.export_settings),
                    color_info=dict(job.color_info),
                    out_path=job.output_path,
                    notify=False,
                )
            job.output_path = result.get("output_path") or job.output_path
            if result["success"]:
                job.state = "completed"
                job.progress_done = max(job.progress_total, int(result.get("frames", 0)))
                job.progress_total = max(job.progress_done, job.progress_total)
                job.progress_label = "完成"
            elif result["cancelled"]:
                job.state = "cancelled"
                job.error = "用户取消了当前任务。"
            else:
                job.state = "failed"
                job.error = result.get("error") or "导出未完成，请查看日志。"
        except Exception as ex:
            traceback.print_exc()
            job.state = "failed"
            job.error = str(ex)
            self.logln(f"[队列] {os.path.basename(job.source_path)} 失败: {ex}")
        job.finished_at = time.time()
        self._queue_active_job_id = None
        self._save_queue_state()
        self._refresh_queue_tree()
        if self._queue_pause_requested:
            self._finish_queue_run(paused=True)
        else:
            self.root.after(20, self._run_next_queue_job)

    def _finish_queue_run(self, paused=False):
        self._queue_running = False
        self._queue_active_job_id = None
        self._queue_pause_requested = False
        self._queue_last_summary = "paused" if paused else "complete"
        self._update_action_labels()
        self._update_host_control_states()
        self._refresh_queue_tree()
        completed = sum(job.state == "completed" for job in self._queue_jobs)
        failed = sum(job.state in {"failed", "interrupted"} for job in self._queue_jobs)
        cancelled = sum(job.state == "cancelled" for job in self._queue_jobs)
        if paused:
            message = "队列已暂停，可稍后继续。"
        else:
            message = f"队列处理结束：完成 {completed}，失败 {failed}，取消 {cancelled}。"
        self.logln("[队列] " + message)
        if not paused:
            messagebox.showinfo("导出队列", message)

    def pause_export_queue_after_current(self):
        if not self._queue_running:
            return
        self._queue_pause_requested = True
        self._update_queue_action_states()

    def cancel_current_queue_job(self):
        if not self._queue_running or self._queue_active_job_id is None:
            return
        self._queue_pause_requested = True
        self.cancel_export()
        self._update_queue_action_states()

    def _update_active_queue_progress(self, done, total, label=""):
        job = self._queue_job(self._queue_active_job_id)
        if job is None:
            return
        job.progress_done = max(int(done), 0)
        job.progress_total = max(int(total), 0)
        job.progress_label = str(label or "")
        self._update_queue_job_row(job)

    # ---------- helpers ----------
    def set_status(self, msg):
        if not msg or msg == "就绪":
            msg = ""
        try:
            self.eta_label.config(text=msg)
            self.root.update_idletasks()
        except Exception:
            pass

    def logln(self, msg):
        try:
            self.log.config(state="normal")
            self.log.insert("end", msg + "\n"); self.log.see("end")
            self.log.config(state="disabled")
        except Exception:
            pass

    def set_progress(self, i, total, extra=""):
        try:
            if not total:
                self.root.update_idletasks()
                return
            if hasattr(self, "pbar"):
                self.pbar["maximum"] = total
                self.pbar["value"] = i
            elapsed = 0.0
            if self._export_t0:
                elapsed = max(0.0, time.perf_counter() - self._export_t0)
            inst_fps = (i / elapsed) if elapsed >= 0.25 and i > 0 else 0.0
            if inst_fps > 0:
                if self._export_ema_fps is None:
                    self._export_ema_fps = inst_fps
                else:
                    self._export_ema_fps = 0.85 * self._export_ema_fps + 0.15 * inst_fps
            fps = self._export_ema_fps or inst_fps
            remain = ((total - i) / fps) if fps > 0 and i < total else 0.0
            pct = (100.0 * i / total) if total else 0.0
            stats = f"{fps:.1f} fps    已用 {_format_duration(elapsed)}    剩余 {_format_duration(remain)}"
            if fps > 0 and i < total:
                done_at = time.localtime(time.time() + remain)
                stats += time.strftime("    完成 %H:%M:%S", done_at)
            elif i >= total and elapsed > 0:
                stats = f"{fps:.1f} fps    用时 {_format_duration(elapsed)}    已完成"
            label = (extra or "").strip()
            if label:
                stats = (
                    f"{label}  {i}/{total} ({pct:.0f}%)    {stats}"
                    if stats else
                    f"{label}  {i}/{total} ({pct:.0f}%)"
                )
            self._update_active_queue_progress(i, total, label)
            try:
                self.eta_label.config(text=stats)
            except Exception:
                pass
            self.root.update_idletasks()
        except Exception:
            pass

    @property
    def _is_image(self):
        return self._source_kind == "image" and self._image_bgr is not None

    def _set_ttk_enabled(self, widget, enabled):
        try:
            widget.state(["!disabled"] if enabled else ["disabled"])
        except Exception:
            try:
                widget.config(state="normal" if enabled else "disabled")
            except Exception:
                pass

    def _update_action_labels(self):
        try:
            has = bool(self.video)
            busy = bool(self._exporting or self._queue_running or self._diagnosing)
            cancel_requested = self._export_cancel_event.is_set()
            active_job = self._queue_job(self._queue_active_job_id)
            cancellable_export = (
                active_job.media_kind == "video" if active_job is not None
                else not self._is_image
            )
            self._layout_export_run_controls(self._exporting and cancellable_export)
            self._set_ttk_enabled(self.import_btn, not busy)
            self._set_ttk_enabled(self.clear_btn, has and not busy)
            self._set_ttk_enabled(self.export_btn, has and not busy)
            self._set_ttk_enabled(
                self.add_queue_btn, has and not self._is_image and not busy,
            )
            self._set_ttk_enabled(
                self.cancel_export_btn,
                self._exporting
                and cancellable_export
                and not cancel_requested,
            )
            self.cancel_export_btn.config(text="取消中…" if cancel_requested else "取消导出")
            self._set_ttk_enabled(
                self.diagnostic_btn,
                not self._exporting
                and not self._queue_running
                and not self._switching_backend
                and not self._diagnosing,
            )
            self.diagnostic_btn.config(text="诊断中…" if self._diagnosing else "一键诊断")
            update_busy = self._update_checking or self._update_downloading
            self._set_ttk_enabled(self.update_btn, not update_busy)
            if self._update_downloading:
                percent = self._update_progress_percent
                update_text = f"下载 {percent}%" if percent is not None else "下载更新…"
            elif self._update_checking:
                update_text = "检查中…"
            else:
                update_text = "检查更新"
            self.update_btn.config(text=update_text)
            if not has:
                self.export_btn.config(text="导出 DLSS")
            elif self._is_image:
                self.export_btn.config(text="导出 DLSS 图片")
            else:
                self.export_btn.config(text="导出 DLSS 视频")
            self._update_zoom_controls()
            self._refresh_status_chips()
        except Exception:
            pass

    def _layout_export_run_controls(self, show_cancel):
        show_cancel = bool(show_cancel)
        if show_cancel:
            self.export_btn.grid_remove()
            self.cancel_export_btn.grid(row=0, column=0, columnspan=2, sticky="ew")
            try:
                self.cancel_export_btn.lift()
            except tk.TclError:
                pass
        else:
            self.cancel_export_btn.grid_remove()
            self.export_btn.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._export_run_layout = "cancel" if show_cancel else "export"

    def _read_frame(self, frame):
        if self._is_image:
            return None if self._image_bgr is None else self._image_bgr.copy()
        cap = getattr(self, "_cap", None)
        if cap is None:
            return None
        try:
            frame = int(frame)
        except (TypeError, ValueError):
            return None
        plan, skip = _decode_plan(getattr(self, "_cap_next", None), frame)
        if plan == "skip":
            for _ in range(skip):
                ok, _discarded = cap.read()
                if not ok:
                    plan = "seek"
                    break
            if plan == "skip":
                self._cap_next = frame
                plan = "read"
        if plan == "seek":
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
            self._cap_next = frame
        ok, image = cap.read()
        self._cap_next = frame + 1 if ok else None
        if not ok:
            return None
        return tone_map_hdr_preview(image, self._video_color_info)

    # ---------- preview ----------
    def _build_settings(self, parent):
        d = {}
        self._slider_committers = []
        saved = self._saved_settings
        d['v_style'] = tk.StringVar(value=STYLE_NAMES.get(saved['style'], "默认"))
        d['v_enable_5x'] = tk.BooleanVar(value=saved.get('enable_5x', False))
        d['v_intensity'] = tk.DoubleVar(value=saved['intensity'])
        d['v_use_intensity'] = tk.BooleanVar(value=saved['use_intensity'])
        d['v_local_tone'] = tk.DoubleVar(value=saved['local_tone'])
        d['v_use_local_tone'] = tk.BooleanVar(value=saved['use_local_tone'])
        d['v_local_struct'] = tk.DoubleVar(value=saved['local_struct'])
        d['v_use_local_struct'] = tk.BooleanVar(value=saved['use_local_struct'])
        d['v_auto_mask'] = tk.BooleanVar(value=saved['use_auto_mask'])
        d['v_skin_struct'] = tk.DoubleVar(value=saved['skin_struct'])
        d['v_outview'] = tk.StringVar(value=OUTVIEW_NAMES.get(saved['output_view'], "处理"))
        d['v_outmix'] = tk.DoubleVar(value=saved['output_mix'])
        d['v_use_output_mix'] = tk.BooleanVar(value=saved['use_output_mix'])
        body = ttk.Frame(parent, style="Panel.TFrame")
        body.pack(fill="x", padx=0, pady=2)

        ttk.Label(body, text="风格", style="Kicker.TLabel").pack(
            fill="x", pady=(0, 6),
        )
        style_chips = ChipGroup(
            body, d['v_style'], list(STYLE_CHOICES),
            command=self.on_settings_change, ui=self._ui,
        )
        style_chips.pack(anchor="w", pady=(0, 12))
        self._theme_widgets.append(style_chips)

        slider_max = (
            app_settings.DLSS_SLIDER_MAX
            if d['v_enable_5x'].get() else app_settings.DLSS_STANDARD_MAX
        )
        sliders = ttk.Frame(body, style="Panel.TFrame")
        sliders.pack(fill="x")
        _, d['w_intensity'], d['w_intensity_value'] = self._add_toggle_slider(
            sliders, 0, 0, "强度", d['v_intensity'], d['v_use_intensity'],
            "关闭时按 0 处理。开启后使用记忆的强度。", slider_max=slider_max,
        )
        d['w_use_output_mix'], d['w_outmix'], d['w_outmix_value'] = self._add_toggle_slider(
            sliders, 1, 0, "输出混合", d['v_outmix'], d['v_use_output_mix'],
            "仅「处理」有效。0=原图，1=完整 DLSS，超过 1 会放大处理残差；"
            "DLSS/对比预览与导出同步生效。\n"
            "关闭时按 0（原图），开启后使用记忆的混合比例。",
            on_change=self.on_output_settings_change,
            slider_max=slider_max,
        )
        _, d['w_local_tone'], d['w_local_tone_value'] = self._add_toggle_slider(
            sliders, 2, 0, "本地色调", d['v_local_tone'], d['v_use_local_tone'],
            "关闭时按 0 处理。开启后使用记忆的本地色调。", slider_max=slider_max,
        )
        _, d['w_local_struct'], d['w_local_struct_value'] = self._add_toggle_slider(
            sliders, 3, 0, "本地结构", d['v_local_struct'], d['v_use_local_struct'],
            "关闭时按 0 处理。开启后使用记忆的本地结构。", slider_max=slider_max,
        )
        _, d['w_skin_struct'], d['w_skin_struct_value'] = self._add_toggle_slider(
            sliders, 4, 0, "皮肤蒙版", d['v_skin_struct'], d['v_auto_mask'],
            "关闭时皮肤结构按 0 处理。开启且数值大于 0 时使用自动蒙版保护皮肤纹理；"
            "数值为 0 时等同关闭。",
            slider_max=slider_max,
        )

        enable_5x = CheckToggle(
            body, "允许 5× 实验范围", d['v_enable_5x'],
            command=self._on_5x_toggle, ui=self._ui,
        )
        enable_5x.pack(anchor="w", pady=(8, 0))
        self._theme_widgets.append(enable_5x)
        d['w_enable_5x'] = enable_5x
        Tooltip(
            enable_5x,
            "默认关闭：全部强度参数和输出混合限制在 0%–100%。"
            "开启后允许输入 0%–500%；高于 100% 可能产生饱和、伪影或过度处理。",
        )
        range_hint = ttk.Label(
            body, text="", style="Hint.TLabel", wraplength=320, justify="left",
        )
        d['w_range_hint'] = range_hint

        ttk.Label(body, text="输出预览", style="Kicker.TLabel").pack(
            fill="x", pady=(4, 6),
        )
        outview_chips = ChipGroup(
            body, d['v_outview'], list(OUTVIEW_CHOICES),
            command=self.on_output_settings_change, ui=self._ui,
        )
        outview_chips.pack(anchor="w")
        self._theme_widgets.append(outview_chips)
        Tooltip(
            outview_chips,
            "导出视图只影响导出构图；上方预览始终使用「处理」构图。\n"
            "处理：DLSS/对比预览和导出都会按输出混合与原图融合。\n"
            "差异×10：仅导出，把 DLSS 与原图的差值放大 10 倍，灰色=几乎没改，亮/暗=改动大。\n"
            "左右对比：仅导出，左半原图、右半 DLSS，中间用白线分隔。",
        )
        self._settings = d
        self._update_dlss_control_states()
        return d

    def _add_toggle_slider(
        self, parent, row, column, text, value_var, enabled_var,
        tooltip=None, on_change=None, slider_max=app_settings.DLSS_STANDARD_MAX,
    ):
        on_change = on_change or self.on_settings_change
        cell = ttk.Frame(parent, style="Panel.TFrame")
        cell.pack(fill="x", pady=(0, 10))
        header = ttk.Frame(cell, style="Panel.TFrame")
        header.pack(fill="x")
        checkbox = CheckToggle(
            header, text, enabled_var, command=on_change, ui=self._ui,
        )
        checkbox.pack(side="left")
        self._theme_widgets.append(checkbox)
        value_text = tk.StringVar()

        def refresh_input(*_args):
            try:
                value_text.set(f"{float(value_var.get()):.2f}")
            except (TypeError, ValueError, tk.TclError):
                pass

        refresh_input()
        value_var.trace_add("write", refresh_input)

        def commit_input(_event=None, notify=True):
            try:
                fallback = float(value_var.get())
            except (TypeError, ValueError, tk.TclError):
                fallback = app_settings.DLSS_SLIDER_MIN
            value = _normalize_slider_input(
                value_text.get(), fallback, max_value=self._dlss_slider_limit(),
            )
            value_var.set(value)
            value_text.set(f"{value:.2f}")
            if notify:
                on_change()

        value_input = ChromeSpinbox(
            header, ui=self._ui,
            from_=app_settings.DLSS_SLIDER_MIN,
            to=slider_max,
            increment=app_settings.DLSS_SLIDER_STEP,
            format="%.2f",
            textvariable=value_text,
            width=6,
            command=commit_input,
        )
        value_input.pack(side="right")
        self._theme_widgets.append(value_input)
        value_input.bind("<Return>", commit_input)
        value_input.bind("<KP_Enter>", commit_input)
        value_input.bind("<FocusOut>", commit_input)
        self._slider_committers.append(lambda: commit_input(notify=False))

        colors = ui_theme.slider_colors(self._ui, False)
        colors["background"] = self._ui_color("panel", "#161d24")
        scale = AccentSlider(
            cell,
            from_=app_settings.DLSS_SLIDER_MIN,
            to=slider_max,
            resolution=app_settings.DLSS_SLIDER_STEP,
            variable=value_var, length=220,
            **colors,
        )
        scale.pack(fill="x")
        scale.config(command=lambda e: on_change())
        if tooltip:
            Tooltip(checkbox, tooltip)
            value_help = (
                tooltip
                + "\n可拖动或直接输入数值，也支持 75% 等百分比格式。"
                + "\n默认最高 1.00（100%）；开启「允许 5× 实验范围」后最高 5.00（500%）。"
            )
            Tooltip(scale, value_help)
            Tooltip(value_input, value_help)
        return checkbox, scale, value_input

    def _dlss_slider_limit(self):
        settings = getattr(self, "_settings", None) or {}
        enabled_var = settings.get('v_enable_5x')
        try:
            enabled = bool(enabled_var.get()) if enabled_var is not None else False
        except tk.TclError:
            enabled = False
        return (
            app_settings.DLSS_SLIDER_MAX
            if enabled else app_settings.DLSS_STANDARD_MAX
        )

    def _on_5x_toggle(self):
        d = self._settings
        limit = self._dlss_slider_limit()
        if limit <= app_settings.DLSS_STANDARD_MAX:
            for key in (
                'v_intensity', 'v_local_tone', 'v_local_struct',
                'v_skin_struct', 'v_outmix',
            ):
                variable = d[key]
                try:
                    value = float(variable.get())
                except (TypeError, ValueError, tk.TclError):
                    value = app_settings.DLSS_SLIDER_MIN
                variable.set(_normalize_slider_input(value, value, max_value=limit))
        self.on_settings_change()

    def _set_slider_enabled(self, scale, value_input, enabled):
        colors = ui_theme.slider_colors(self._ui, enabled)
        scale.config(state="normal" if enabled else "disabled", **colors)
        if value_input is not None:
            value_input.config(state="normal" if enabled else "disabled")

    def _update_dlss_control_states(self):
        if not hasattr(self, "_settings"):
            return
        d = self._settings
        limit = self._dlss_slider_limit()
        for scale_key, input_key in (
            ('w_intensity', 'w_intensity_value'),
            ('w_local_tone', 'w_local_tone_value'),
            ('w_local_struct', 'w_local_struct_value'),
            ('w_skin_struct', 'w_skin_struct_value'),
            ('w_outmix', 'w_outmix_value'),
        ):
            d[scale_key].config(to=limit)
            d[input_key].config(to=limit)
        hint = d['w_range_hint']
        if limit > app_settings.DLSS_STANDARD_MAX:
            hint.config(text="已开启 0%–500%，超过 100% 可能过曝。")
            if not hint.winfo_manager():
                hint.pack(fill="x", pady=(2, 8), after=d['w_enable_5x'])
        else:
            hint.config(text="")
            hint.pack_forget()
        self._set_slider_enabled(
            d['w_intensity'], d['w_intensity_value'], d['v_use_intensity'].get(),
        )
        self._set_slider_enabled(
            d['w_local_tone'], d['w_local_tone_value'], d['v_use_local_tone'].get(),
        )
        self._set_slider_enabled(
            d['w_local_struct'], d['w_local_struct_value'], d['v_use_local_struct'].get(),
        )
        mix_view = d['v_outview'].get() == "处理"
        self._set_ttk_enabled(d['w_use_output_mix'], mix_view)
        self._set_slider_enabled(
            d['w_outmix'], d['w_outmix_value'],
            mix_view and d['v_use_output_mix'].get(),
        )
        self._set_slider_enabled(
            d['w_skin_struct'], d['w_skin_struct_value'], d['v_auto_mask'].get(),
        )

    def _build_preview_settings(self, parent):
        saved = self._saved_settings
        d = {
            'v_quality': tk.StringVar(
                value=PREVIEW_QUALITY_NAMES.get(saved.get('preview_quality', 'auto'), "自动（推荐）")
            ),
            'v_prefetch': tk.IntVar(value=saved.get('preview_prefetch', 24)),
            'v_cache': tk.IntVar(value=saved.get('preview_cache', 96)),
            'v_cache_mb': tk.IntVar(value=saved.get('preview_cache_mb', 2048)),
            'v_scrub_ms': tk.IntVar(value=saved.get('preview_scrub_ms', 40)),
        }

        parent.grid_columnconfigure(0, weight=1)
        preview_group = ttk.Frame(parent, style="Panel.TFrame")
        ttk.Label(preview_group, text="播放与缓存", style="Kicker.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6),
        )
        preview_group.grid(row=0, column=0, sticky="ew", padx=(6, 8), pady=(2, 4))
        preview_group.grid_columnconfigure(1, weight=1)

        quality = self._chrome_combo(
            preview_group, d['v_quality'], list(PREVIEW_QUALITY_CHOICES),
        )
        ttk.Label(preview_group, text="播放质量").grid(row=1, column=0, sticky="w", pady=3)
        quality.grid(row=1, column=1, sticky="ew", pady=3)
        Tooltip(quality, "自动模式会把 4K 级素材降到 1080p 实时处理；暂停后恢复原始分辨率。")

        cache_row = ttk.Frame(preview_group, style="Panel.TFrame")
        cache_mb = self._chrome_spin(
            cache_row, from_=256, to=32768, increment=256,
            textvariable=d['v_cache_mb'], width=7,
        )
        cache_mb.pack(side="left")
        ttk.Label(cache_row, text="MiB", font=ui_theme.UI_MONO).pack(
            side="left", padx=(6, 0),
        )
        ttk.Label(preview_group, text="缓存预算").grid(row=2, column=0, sticky="w", pady=3)
        cache_row.grid(row=2, column=1, sticky="w", pady=3)

        ttk.Label(preview_group, text="启动缓冲").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Label(
            preview_group, text=f"{PREVIEW_BUFFER_SECONDS:.1f} 秒",
            font=ui_theme.UI_MONO,
        ).grid(row=3, column=1, sticky="w", pady=3)

        scrub_row = ttk.Frame(preview_group, style="Panel.TFrame")
        scrub = self._chrome_spin(
            scrub_row, from_=0, to=400, textvariable=d['v_scrub_ms'], width=6,
        )
        scrub.pack(side="left")
        ttk.Label(scrub_row, text="ms", font=ui_theme.UI_MONO).pack(
            side="left", padx=(6, 0),
        )
        ttk.Label(preview_group, text="拖动后生成").grid(row=4, column=0, sticky="w", pady=3)
        scrub_row.grid(row=4, column=1, sticky="w", pady=3)
        Tooltip(scrub, "停止拖动或跳转后等待这段时间，再生成精确预览并从当前帧向后预渲染。")
        d.update({
            'w_quality': quality, 'w_cache_mb': cache_mb, 'w_scrub_ms': scrub,
        })
        cache_hint = ttk.Label(
            preview_group, text="", style="Hint.TLabel", justify="left", wraplength=320,
        )
        cache_hint.grid(row=5, column=0, columnspan=2, sticky="w", pady=(7, 0))
        d['w_cache_hint'] = cache_hint
        quality.bind("<<ComboboxSelected>>", self._on_preview_settings_change)
        for widget in (cache_mb, scrub):
            widget.config(command=self._on_preview_settings_change)
            widget.bind("<FocusOut>", lambda e: self._on_preview_settings_change())
            widget.bind("<Return>", lambda e: self._on_preview_settings_change())
        return d

    def _collect_preview_settings(self):
        d = getattr(self, "_preview_settings", None) or {}

        def integer(name, default, low, high):
            try:
                return max(low, min(high, int(d[name].get())))
            except (KeyError, ValueError, tk.TclError, AttributeError):
                return default
        return {
            'preview_quality': PREVIEW_QUALITY_CHOICES.get(
                d.get('v_quality').get() if d.get('v_quality') else "", 'auto'
            ),
            'preview_prefetch': integer('v_prefetch', 24, 4, 120),
            'preview_cache': integer('v_cache', 96, 16, 400),
            'preview_cache_mb': integer('v_cache_mb', 2048, 256, 32768),
            'preview_scrub_ms': integer('v_scrub_ms', 40, 0, 400),
        }

    def _preview_prefetch(self):
        settings = getattr(self, '_preview_runtime_settings', None)
        return (settings or self._collect_preview_settings())['preview_prefetch']

    def _preview_quality(self):
        settings = getattr(self, '_preview_runtime_settings', None)
        return (settings or self._collect_preview_settings())['preview_quality']

    def _preview_cache_max(self):
        settings = getattr(self, '_preview_runtime_settings', None)
        return (settings or self._collect_preview_settings())['preview_cache']

    def _preview_cache_bytes(self):
        settings = getattr(self, '_preview_runtime_settings', None)
        mib = (settings or self._collect_preview_settings())['preview_cache_mb']
        return int(mib) * 1024 * 1024

    def _preview_scrub_ms(self):
        settings = getattr(self, '_preview_runtime_settings', None)
        return (settings or self._collect_preview_settings())['preview_scrub_ms']

    def _on_preview_settings_change(self, event=None):
        self._preview_runtime_settings = self._collect_preview_settings()
        self._update_preview_memory_hint()
        with self._cache_lock:
            self._evict_preview_cache_locked()
        try:
            self.timeline.set_cache_ranges([], [])
        except Exception:
            pass
        self._schedule_settings_save()
        if self.video and not self._exporting and not self._is_image:
            self._stop_paused_prerender()
            if self.playing and self.view_var.get() in ("DLSS", "对比"):
                self._start_strict_preview_buffering()
                self._present_play_frame(self._frame)
            elif self.view_var.get() in ("DLSS", "对比"):
                self._schedule_full_preview()

    def _update_preview_memory_hint(self):
        settings = getattr(self, "_preview_runtime_settings", None) or self._collect_preview_settings()
        budget_mib = settings['preview_cache_mb']
        source_w, source_h = self._source_size()
        if source_w <= 0 or source_h <= 0:
            text = f"RAM 预算 {budget_mib} MiB；按原图帧 + DLSS 帧合计管理"
        else:
            preview_w, preview_h = _realtime_preview_size(
                source_w, source_h, settings['preview_quality'],
            )
            pair_bytes = max((source_w * source_h + preview_w * preview_h) * 3, 1)
            frames = max(int(budget_mib * 1024 * 1024 // pair_bytes), 1)
            seconds = frames / max(float(self.fps), 1.0)
            text = (
                f"后台最多约 {frames} 个原图+DLSS帧（{seconds:.1f} 秒）；"
                f"播放启动仍按 {PREVIEW_BUFFER_SECONDS:.1f} 秒；"
                f"当前处理尺寸 {preview_w}×{preview_h}"
            )
        label = (getattr(self, "_preview_settings", None) or {}).get('w_cache_hint')
        if label is not None:
            try:
                label.config(text=text)
            except Exception:
                pass

    def _build_export_settings(self, parent):
        saved = self._saved_settings
        d = {
            'v_mode': tk.StringVar(value=EXPORT_MODE_NAMES[saved['export_mode']]),
            'v_workers': tk.IntVar(value=saved['parallel_workers']),
            'v_warmup': tk.IntVar(value=saved['warmup_frames']),
            'v_decode_buffer': tk.IntVar(value=saved['decode_buffer']),
            'v_nvenc_preset': tk.StringVar(
                value=NVENC_PRESET_NAMES.get(saved['nvenc_preset'], "p5 较慢（推荐）")
            ),
            'v_output_container': tk.StringVar(value=OUTPUT_CONTAINER_NAMES.get(
                saved.get('output_container', 'mp4'), "MP4（推荐）"
            )),
            'v_output_resolution': tk.StringVar(value=OUTPUT_RESOLUTION_NAMES.get(
                saved.get('output_resolution', 'source'), "跟随源视频（推荐）"
            )),
            'v_super_resolution': tk.StringVar(value=SUPER_RESOLUTION_NAMES.get(
                normalize_scale(saved.get('super_resolution_scale', 1)), "关闭"
            )),
            'v_custom_width': tk.IntVar(value=saved.get('custom_output_width', 1920)),
            'v_custom_height': tk.IntVar(value=saved.get('custom_output_height', 1080)),
            'v_rate_control': tk.StringVar(value=RATE_CONTROL_NAMES.get(
                saved.get('rate_control', 'quality'), "按画质（推荐）"
            )),
            'v_quality_profile': tk.StringVar(value=QUALITY_PROFILE_NAMES.get(
                saved.get('quality_profile', 'high'), "高质量（推荐）"
            )),
            'v_video_bitrate': tk.DoubleVar(value=saved.get('video_bitrate_mbps', 20.0)),
            'v_hdr': tk.BooleanVar(value=saved.get('hdr_mode', True)),
        }

        # Keep controls in compact, equal-width field groups.  The previous flat
        # eight-column grid let wide comboboxes and short spinboxes pull every row
        # onto a different visual rhythm as the window width or DPI changed.
        parent.grid_columnconfigure(0, weight=1)
        output_group = ttk.Frame(parent, style="Panel.TFrame")
        output_group.grid(row=0, column=0, sticky="ew", padx=(6, 8), pady=(2, 4))
        ttk.Label(output_group, text="输出与编码", style="Kicker.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6),
        )
        performance_group = ttk.Frame(parent, style="Panel.TFrame")
        performance_group.grid(row=1, column=0, sticky="ew", padx=(6, 8), pady=(0, 4))
        ttk.Label(performance_group, text="性能参数", style="Kicker.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6),
        )
        output_group.grid_columnconfigure(1, weight=1)
        performance_group.grid_columnconfigure(1, weight=1)

        container = self._chrome_combo(
            output_group, d['v_output_container'], list(OUTPUT_CONTAINER_CHOICES),
        )
        ttk.Label(output_group, text="输出容器").grid(row=1, column=0, sticky="w", pady=3)
        container.grid(row=1, column=1, sticky="ew", pady=3)

        resolution = self._chrome_combo(
            output_group, d['v_output_resolution'], list(OUTPUT_RESOLUTION_CHOICES),
        )
        ttk.Label(output_group, text="输出分辨率").grid(row=2, column=0, sticky="w", pady=3)
        resolution.grid(row=2, column=1, sticky="ew", pady=3)

        custom_label = ttk.Label(output_group, text="自定义上限")
        custom_label.grid(row=3, column=0, sticky="w", pady=3)
        custom_frame = ttk.Frame(output_group, style="Panel.TFrame")
        custom_frame.grid(row=3, column=1, sticky="w", pady=3)
        custom_width = self._chrome_spin(
            custom_frame, from_=2, to=8192, increment=2,
            textvariable=d['v_custom_width'], width=6,
        )
        custom_width.pack(side="left")
        ttk.Label(custom_frame, text="×").pack(side="left", padx=4)
        custom_height = self._chrome_spin(
            custom_frame, from_=2, to=8192, increment=2,
            textvariable=d['v_custom_height'], width=6,
        )
        custom_height.pack(side="left")

        rate_control = self._chrome_combo(
            output_group, d['v_rate_control'], list(RATE_CONTROL_CHOICES),
        )
        ttk.Label(output_group, text="码率控制").grid(row=4, column=0, sticky="w", pady=3)
        rate_control.grid(row=4, column=1, sticky="ew", pady=3)

        quality_label = ttk.Label(output_group, text="编码质量")
        quality_label.grid(row=5, column=0, sticky="w", pady=3)
        quality = self._chrome_combo(
            output_group, d['v_quality_profile'], list(QUALITY_PROFILE_CHOICES),
        )
        quality.grid(row=5, column=1, sticky="ew", pady=3)

        bitrate_label = ttk.Label(output_group, text="目标码率")
        bitrate_label.grid(row=6, column=0, sticky="w", pady=3)
        bitrate = self._chrome_spin(
            output_group, from_=0.5, to=500.0, increment=0.5,
            textvariable=d['v_video_bitrate'], width=8,
        )
        bitrate.grid(row=6, column=1, sticky="w", pady=3)

        preset = self._chrome_combo(
            output_group, d['v_nvenc_preset'], list(NVENC_PRESET_CHOICES),
        )
        ttk.Label(output_group, text="编码速度").grid(row=7, column=0, sticky="w", pady=3)
        preset.grid(row=7, column=1, sticky="ew", pady=3)

        super_resolution = self._chrome_combo(
            output_group, d['v_super_resolution'], list(SUPER_RESOLUTION_CHOICES),
        )
        ttk.Label(output_group, text="AI 超分").grid(row=8, column=0, sticky="w", pady=3)
        super_resolution.grid(row=8, column=1, sticky="ew", pady=3)

        hdr = CheckToggle(
            output_group, "HDR10 / HLG 高精度处理", d['v_hdr'],
            command=self._on_export_settings_change, ui=self._ui,
        )
        hdr.grid(row=9, column=0, columnspan=2, sticky="w", pady=(6, 2))
        self._theme_widgets.append(hdr)

        hint = ttk.Label(
            output_group,
            text="PQ/HLG 自动 HEVC Main10。",
            style="Hint.TLabel", wraplength=320, justify="left",
        )
        hint.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        Tooltip(
            hdr,
            "导入 PQ/HLG 后使用 RGBA16F 进入 Feature 18，导出 HEVC Main10。"
            "关闭时先 tone-map 到 SDR 再按 H.264 处理。",
        )

        mode = self._chrome_combo(
            performance_group, d['v_mode'], list(EXPORT_MODE_CHOICES),
        )
        ttk.Label(performance_group, text="导出模式").grid(row=1, column=0, sticky="w", pady=3)
        mode.grid(row=1, column=1, sticky="ew", pady=3)

        workers = self._chrome_spin(
            performance_group, from_=2, to=4, textvariable=d['v_workers'], width=7,
        )
        ttk.Label(performance_group, text="并行进程").grid(row=2, column=0, sticky="w", pady=3)
        workers.grid(row=2, column=1, sticky="w", pady=3)

        warmup = self._chrome_spin(
            performance_group, from_=0, to=120, textvariable=d['v_warmup'], width=7,
        )
        ttk.Label(performance_group, text="预热帧").grid(row=3, column=0, sticky="w", pady=3)
        warmup.grid(row=3, column=1, sticky="w", pady=3)

        decode = self._chrome_spin(
            performance_group, from_=1, to=8, textvariable=d['v_decode_buffer'], width=7,
        )
        ttk.Label(performance_group, text="解码缓存").grid(row=4, column=0, sticky="w", pady=3)
        decode.grid(row=4, column=1, sticky="w", pady=3)
        d.update({
            'w_output_container': container,
            'w_mode': mode,
            'w_workers': workers,
            'w_warmup': warmup,
            'w_decode_buffer': decode,
            'w_nvenc_preset': preset,
            'w_output_resolution': resolution,
            'w_super_resolution': super_resolution,
            'w_custom_label': custom_label,
            'w_custom_frame': custom_frame,
            'w_custom_width': custom_width,
            'w_custom_height': custom_height,
            'w_rate_control': rate_control,
            'w_quality_label': quality_label,
            'w_quality_profile': quality,
            'w_bitrate_label': bitrate_label,
            'w_video_bitrate': bitrate,
            'w_hdr': hdr,
            'w_hdr_hint': hint,
        })
        mode.bind("<<ComboboxSelected>>", lambda e: self._on_export_settings_change())
        for widget in (container, resolution, rate_control, quality, preset):
            widget.bind("<<ComboboxSelected>>", lambda e: self._on_export_settings_change())
        super_resolution.bind(
            "<<ComboboxSelected>>", lambda e: self._on_super_resolution_change()
        )
        for widget in (workers, warmup, decode, custom_width, custom_height, bitrate):
            widget.config(command=self._on_export_settings_change)
            widget.bind("<FocusOut>", lambda e: self._on_export_settings_change())
            widget.bind("<Return>", lambda e: self._on_export_settings_change())
        Tooltip(
            container,
            "视频可输出 MP4、MKV 或 MOV。跟随输入仅跟随这三类容器；"
            "M4V 视为 MP4，AVI/WebM 会安全回退到 MP4。图片始终保持源格式。",
        )
        Tooltip(
            resolution,
            "只控制视频输出尺寸，并保持原宽高比；不会放大低分辨率素材。"
            "DLSS 仍以源分辨率处理，因此缩小输出不会减少神经渲染耗时。",
        )
        Tooltip(
            super_resolution,
            "固定先用 RTX Video Super Resolution 放大，再以目标分辨率运行 DLSS 5。"
            "支持 SDR 与 10-bit HDR；高分辨率会显著增加显存、内存和处理时间。",
        )
        Tooltip(
            rate_control,
            "按画质会稳定压缩质量但文件大小浮动；目标码率便于控制体积，复杂画面可能波动。",
        )
        Tooltip(preset, "越慢通常压缩效率越高；它不等同于清晰度或目标码率。")
        self.root.after_idle(self._update_export_control_states)
        return d

    def _build_export_quick(self, parent):
        d = self._export_settings
        ttk.Label(parent, text="这次导出", style="Kicker.TLabel").pack(anchor="w", pady=(4, 6))
        fields = (
            ("容器", d["v_output_container"], list(OUTPUT_CONTAINER_CHOICES),
             "w_output_container", lambda _event: self._on_export_settings_change()),
            ("尺寸", d["v_output_resolution"], list(OUTPUT_RESOLUTION_CHOICES),
             "w_output_resolution", lambda _event: self._on_export_settings_change()),
            ("超分", d["v_super_resolution"], list(SUPER_RESOLUTION_CHOICES),
             "w_super_resolution", lambda _event: self._on_super_resolution_change()),
        )
        self._export_quick_fields = {}
        for label, variable, values, key, command in fields:
            row = ttk.Frame(parent, style="Panel.TFrame")
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=4, style="Panel.TLabel").pack(side="left")
            combo = self._chrome_combo(row, variable, values)
            combo.pack(side="left", fill="x", expand=True)
            combo.bind("<<ComboboxSelected>>", command)
            self._export_quick_fields[key] = combo
        self._export_summary = ttk.Label(
            parent, text="", style="Hint.TLabel", wraplength=320, justify="left",
        )
        self._export_summary.pack(fill="x", pady=(6, 0))

    def _set_grid_visible(self, widget, visible):
        try:
            if visible:
                widget.grid()
            else:
                widget.grid_remove()
        except tk.TclError:
            pass

    def _build_host_settings(self, parent):
        saved = self._saved_settings
        d = {
            'v_backend': tk.StringVar(value=HOST_BACKEND_NAMES[saved['host_backend']]),
            'v_submission': tk.StringVar(
                value=HOST_SUBMISSION_NAMES[saved['host_submission']]
            ),
            'v_zero_fast': tk.BooleanVar(value=saved['host_zero_fast_path']),
            'v_persistent': tk.BooleanVar(value=saved['host_persistent_buffers']),
            'v_in_flight': tk.IntVar(value=saved['host_in_flight']),
            'v_fallback': tk.BooleanVar(value=saved['host_auto_fallback']),
        }

        parent.grid_columnconfigure(0, weight=1)
        host_wrap = ttk.Frame(parent, style="Panel.TFrame")
        host_wrap.grid(row=0, column=0, sticky="ew", padx=(6, 8), pady=(2, 4))
        ttk.Label(host_wrap, text="主机与提交", style="Kicker.TLabel").pack(
            anchor="w", pady=(0, 6),
        )
        host_group = ttk.Frame(host_wrap, style="Panel.TFrame")
        host_group.pack(fill="x")
        host_group.grid_columnconfigure(1, weight=1)

        backend = self._chrome_combo(
            host_group, d['v_backend'], list(HOST_BACKEND_CHOICES),
        )
        ttk.Label(host_group, text="后端").grid(row=0, column=0, sticky="w", pady=3)
        backend.grid(row=0, column=1, sticky="ew", pady=3)

        submission = self._chrome_combo(
            host_group, d['v_submission'], list(HOST_SUBMISSION_CHOICES),
        )
        ttk.Label(host_group, text="提交方式").grid(row=1, column=0, sticky="w", pady=3)
        submission.grid(row=1, column=1, sticky="ew", pady=3)

        in_flight = self._chrome_spin(
            host_group, from_=1, to=3, textvariable=d['v_in_flight'], width=7,
            command=self._on_host_settings_change,
        )
        ttk.Label(host_group, text="GPU 队列帧").grid(row=2, column=0, sticky="w", pady=3)
        in_flight.grid(row=2, column=1, sticky="w", pady=3)

        zero_fast = CheckToggle(
            host_group, "零引导快路径", d['v_zero_fast'],
            command=self._on_host_settings_change, ui=self._ui,
        )
        zero_fast.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 2))
        persistent = CheckToggle(
            host_group, "持久上传/回读缓冲", d['v_persistent'],
            command=self._on_host_settings_change, ui=self._ui,
        )
        persistent.grid(row=4, column=0, columnspan=2, sticky="w", pady=2)
        fallback = CheckToggle(
            host_group, "优化路径失败时自动回退", d['v_fallback'],
            command=self._on_host_settings_change, ui=self._ui,
        )
        fallback.grid(row=5, column=0, columnspan=2, sticky="w", pady=2)
        self._theme_widgets.extend((zero_fast, persistent, fallback))
        d.update({
            'w_backend': backend,
            'w_submission': submission,
            'w_zero_fast': zero_fast,
            'w_persistent': persistent,
            'w_in_flight': in_flight,
            'w_fallback': fallback,
        })
        backend.bind("<<ComboboxSelected>>", lambda e: self._on_host_settings_change())
        submission.bind("<<ComboboxSelected>>", lambda e: self._on_host_settings_change())
        in_flight.bind("<FocusOut>", lambda e: self._on_host_settings_change())
        in_flight.bind("<Return>", lambda e: self._on_host_settings_change())
        self.root.after_idle(self._update_host_control_states)
        return d

    def _collect_host_settings(self):
        d = self._host_settings
        try:
            in_flight = int(d['v_in_flight'].get())
        except (ValueError, tk.TclError):
            in_flight = 2
        return {
            'host_backend': HOST_BACKEND_CHOICES.get(d['v_backend'].get(), 'auto'),
            'host_submission': HOST_SUBMISSION_CHOICES.get(
                d['v_submission'].get(), 'merged'
            ),
            'host_zero_fast_path': bool(d['v_zero_fast'].get()),
            'host_persistent_buffers': bool(d['v_persistent'].get()),
            'host_in_flight': max(1, min(3, in_flight)),
            'host_auto_fallback': bool(d['v_fallback'].get()),
        }

    def _update_host_control_states(self):
        if not hasattr(self, "_host_settings"):
            return
        if self._exporting or self._queue_running or self._switching_backend or self._diagnosing:
            for name in (
                'w_backend', 'w_submission', 'w_zero_fast', 'w_persistent',
                'w_in_flight', 'w_fallback',
            ):
                self._host_settings[name].config(state="disabled")
            return
        host = self._collect_host_settings()
        self._host_settings['w_backend'].config(state="readonly")
        v2_enabled = host['host_backend'] != 'legacy'
        self._host_settings['w_submission'].config(
            state="readonly" if v2_enabled else "disabled"
        )
        for name in ('w_zero_fast', 'w_persistent', 'w_fallback'):
            self._host_settings[name].config(
                state="normal" if v2_enabled else "disabled"
            )
        queue_enabled = (
            v2_enabled and host['host_submission'] == 'merged'
            and host['host_persistent_buffers']
        )
        self._host_settings['w_in_flight'].config(
            state="normal" if queue_enabled else "disabled"
        )

    def _on_host_settings_change(self):
        if self._switching_backend:
            return
        if self._exporting or self._queue_running:
            self.set_status("正在处理队列，完成或暂停后才能切换主机后端")
            return
        self._update_host_control_states()
        self._cache_clear()
        try:
            self.timeline.set_cache_ranges([], [])
        except Exception:
            pass
        self._last_dlss_frame = -1
        self._split_frame = -1
        settings = self._collect_settings()
        if self._live:
            old_preference = self._live.preference
            old_backend = self._live.backend
            backend_changed = settings['host_backend'] != old_preference
            if backend_changed:
                self.pause()
                self._wait_play_dlss()
                self._switching_backend = True
                self._update_host_control_states()
                self.root.config(cursor="wait")
                self.set_status("正在切换 DLSS 主机后端...")
                self.root.update_idletasks()
            try:
                with self._live_lock:
                    self._live.update(settings)
            except Exception as ex:
                if backend_changed:
                    self._switching_backend = False
                    self.root.config(cursor="")
                    self._host_settings['v_backend'].set(
                        HOST_BACKEND_NAMES.get(old_preference, HOST_BACKEND_NAMES['auto'])
                    )
                    self._update_host_control_states()
                self.logln("[DLSS 后端] 设置应用失败，继续使用原后端：" + str(ex))
                if backend_changed:
                    self.set_status(f"后端切换失败；仍使用 {self._live.backend}")
                else:
                    self.set_status(f"主机设置应用失败；仍使用 {self._live.backend}")
                self._schedule_settings_save()
                if backend_changed:
                    messagebox.showerror(
                        "后端切换失败",
                        "新后端初始化失败，程序仍在使用原后端。\n\n" + str(ex),
                    )
                return
            if backend_changed:
                self._switching_backend = False
                self.root.config(cursor="")
                self._update_host_control_states()
                if self._live.backend != old_backend:
                    self.logln(
                        f"[DLSS 后端] 已热切换到 {self._live.backend}（GUI 无需重启）"
                    )
                    self.set_status(f"已切换到 {self._live.backend} 后端")
                else:
                    self.logln(
                        f"[DLSS 后端] 选择已更新；继续使用 {self._live.backend}"
                    )
                    self.set_status(f"后端设置已应用；当前 {self._live.backend}")
            else:
                self.set_status(f"主机设置已应用；当前后端 {self._live.backend}")
            if self.video and self.view_var.get() in ("DLSS", "对比"):
                self.root.after_idle(lambda: self.display_view(quality="full"))
        else:
            self.set_status("主机设置已保存；将在首次 DLSS 预览/导出时应用")
        self._schedule_settings_save()

    def _remembered_dlss(self):
        for commit in getattr(self, "_slider_committers", ()):
            commit()
        d = self._settings
        return {
            'enable_5x': bool(d['v_enable_5x'].get()),
            'intensity': float(d['v_intensity'].get()),
            'use_intensity': bool(d['v_use_intensity'].get()),
            'local_tone': float(d['v_local_tone'].get()),
            'use_local_tone': bool(d['v_use_local_tone'].get()),
            'local_struct': float(d['v_local_struct'].get()),
            'use_local_struct': bool(d['v_use_local_struct'].get()),
            'use_auto_mask': bool(d['v_auto_mask'].get()),
            'skin_struct': float(d['v_skin_struct'].get()),
            'output_mix': float(d['v_outmix'].get()),
            'use_output_mix': bool(d['v_use_output_mix'].get()),
        }

    def _collect_settings(self):
        d = self._settings
        remembered = self._remembered_dlss()
        use_auto_mask, skin_struct = effective_skin_settings(
            remembered['use_auto_mask'], remembered['skin_struct']
        )
        result = {
            'style': STYLE_CHOICES.get(d['v_style'].get(), 0),
            'intensity': effective_slider(
                remembered['use_intensity'], remembered['intensity']
            ),
            'local_tone': effective_slider(
                remembered['use_local_tone'], remembered['local_tone']
            ),
            'local_struct': effective_slider(
                remembered['use_local_struct'], remembered['local_struct']
            ),
            'use_auto_mask': use_auto_mask,
            'skin_struct': skin_struct,
            'output_view': OUTVIEW_CHOICES.get(d['v_outview'].get(), 0),
            'output_mix': effective_slider(
                remembered['use_output_mix'], remembered['output_mix']
            ),
        }
        if hasattr(self, "_host_settings"):
            result.update(self._collect_host_settings())
        if hasattr(self, "_export_settings"):
            result['super_resolution_scale'] = self._collect_export_settings()[
                'super_resolution_scale'
            ]
        return result

    def _collect_export_settings(self):
        d = self._export_settings
        def integer(variable, default):
            try:
                return int(variable.get())
            except (ValueError, tk.TclError):
                return default
        def number(variable, default):
            try:
                return float(variable.get())
            except (ValueError, tk.TclError):
                return default
        return {
            'mode': EXPORT_MODE_CHOICES.get(d['v_mode'].get(), 'single'),
            'workers': max(2, min(4, integer(d['v_workers'], 2))),
            'warmup': max(0, min(120, integer(d['v_warmup'], 8))),
            'decode_buffer': max(1, min(8, integer(d['v_decode_buffer'], 4))),
            'nvenc_preset': NVENC_PRESET_CHOICES.get(d['v_nvenc_preset'].get(), 'p5'),
            'output_container': OUTPUT_CONTAINER_CHOICES.get(
                d['v_output_container'].get(), 'mp4'
            ),
            'output_resolution': OUTPUT_RESOLUTION_CHOICES.get(
                d['v_output_resolution'].get(), 'source'
            ),
            'super_resolution_scale': SUPER_RESOLUTION_CHOICES.get(
                d['v_super_resolution'].get(), 1
            ),
            'custom_output_width': max(2, min(8192, integer(d['v_custom_width'], 1920))),
            'custom_output_height': max(2, min(8192, integer(d['v_custom_height'], 1080))),
            'rate_control': RATE_CONTROL_CHOICES.get(
                d['v_rate_control'].get(), 'quality'
            ),
            'quality_profile': QUALITY_PROFILE_CHOICES.get(
                d['v_quality_profile'].get(), 'high'
            ),
            'video_bitrate_mbps': max(
                0.5, min(500.0, number(d['v_video_bitrate'], 20.0))
            ),
            'hdr_mode': bool(d['v_hdr'].get()),
        }

    def _update_export_control_states(self):
        if not hasattr(self, "_export_settings"):
            return
        export = self._collect_export_settings()
        super_resolution_scale = normalize_scale(export['super_resolution_scale'])
        super_resolution_enabled = super_resolution_scale > 1
        color = getattr(self, "_video_color_info", None) or {}
        effective_hdr = bool(export['hdr_mode'] and color.get('is_hdr'))
        video_controls_enabled = not self._is_image
        if (effective_hdr or super_resolution_enabled) and export['mode'] == 'parallel':
            self._export_settings['v_mode'].set(EXPORT_MODE_NAMES['single'])
            export['mode'] = 'single'
        state = (
            "normal"
            if video_controls_enabled and export['mode'] == 'parallel'
            and not effective_hdr and not super_resolution_enabled
            else "disabled"
        )
        self._export_settings['w_workers'].config(state=state)
        self._export_settings['w_warmup'].config(state=state)
        self._export_settings['w_decode_buffer'].config(
            state="normal" if video_controls_enabled else "disabled"
        )
        self._export_settings['w_mode'].config(
            state="disabled" if self._is_image or effective_hdr or super_resolution_enabled else "readonly"
        )
        self._export_settings['w_super_resolution'].config(state="readonly")
        self._export_settings['w_output_container'].config(
            state="readonly" if video_controls_enabled else "disabled"
        )
        self._export_settings['w_nvenc_preset'].config(
            state="readonly" if video_controls_enabled else "disabled"
        )
        self._set_ttk_enabled(self._export_settings['w_hdr'], video_controls_enabled)
        self._export_settings['w_output_resolution'].config(
            state="readonly" if video_controls_enabled and not super_resolution_enabled else "disabled"
        )
        self._export_settings['w_rate_control'].config(
            state="readonly" if video_controls_enabled else "disabled"
        )
        custom_enabled = (
            video_controls_enabled and not super_resolution_enabled
            and export['output_resolution'] == 'custom'
        )
        quality_enabled = video_controls_enabled and export['rate_control'] == 'quality'
        bitrate_enabled = video_controls_enabled and export['rate_control'] == 'bitrate'
        for key in ('w_custom_label', 'w_custom_width', 'w_custom_height'):
            self._set_ttk_enabled(self._export_settings[key], custom_enabled)
        self._set_grid_visible(self._export_settings['w_custom_label'], custom_enabled)
        self._set_grid_visible(self._export_settings['w_custom_frame'], custom_enabled)
        self._set_ttk_enabled(self._export_settings['w_quality_label'], quality_enabled)
        self._export_settings['w_quality_profile'].config(
            state="readonly" if quality_enabled else "disabled"
        )
        self._set_grid_visible(self._export_settings['w_quality_label'], quality_enabled)
        self._set_grid_visible(self._export_settings['w_quality_profile'], quality_enabled)
        self._set_ttk_enabled(self._export_settings['w_bitrate_label'], bitrate_enabled)
        self._set_ttk_enabled(self._export_settings['w_video_bitrate'], bitrate_enabled)
        self._set_grid_visible(self._export_settings['w_bitrate_label'], bitrate_enabled)
        self._set_grid_visible(self._export_settings['w_video_bitrate'], bitrate_enabled)
        parts = []
        source_width, source_height = self._source_size()
        if self._is_image:
            image_ext = os.path.splitext(self.video or "")[1].upper().lstrip(".") or "PNG"
            image_width, image_height = super_resolution_target_size(
                source_width, source_height, super_resolution_scale,
            )
            parts.append(image_ext)
            if image_width > 0 and image_height > 0:
                parts.append(f"{image_width}×{image_height}")
        else:
            container = resolve_output_container(
                self.video or "", export.get("output_container", "mp4"),
            )
            parts.append(OUTPUT_CONTAINER_LABELS.get(container, "MP4"))
            if color.get("is_hdr") and export["hdr_mode"]:
                parts.append("HEVC Main10")
            else:
                parts.append("H.264")
            if super_resolution_enabled:
                output_width, output_height = super_resolution_target_size(
                    source_width, source_height, super_resolution_scale,
                )
            else:
                output_width, output_height = _resolve_output_size(
                    source_width, source_height, export["output_resolution"],
                    export["custom_output_width"], export["custom_output_height"],
                )
            if output_width > 0 and output_height > 0:
                parts.append(f"{output_width}×{output_height}")
            if export["rate_control"] == "quality":
                parts.append(QUALITY_PROFILE_NAMES.get(export["quality_profile"], "均衡"))
            else:
                parts.append(f"{export['video_bitrate_mbps']:g} Mbps")
            if super_resolution_enabled:
                parts.append(f"{super_resolution_scale}×超分")
        if not self.video:
            parts = ["PQ/HLG 自动 HEVC Main10"]
        elif color.get("is_hdr") and not export["hdr_mode"]:
            parts.append("将 tone-map 到 SDR")
        if super_resolution_enabled:
            status = super_resolution_runtime_status()
            if not status["available"]:
                parts.append("缺超分组件")
        self._export_settings["w_hdr_hint"].config(text=" · ".join(parts))
        if hasattr(self, "_export_summary"):
            if export.get("rate_control") == "bitrate":
                current = f"{export.get('video_bitrate_mbps', 20):g} Mbps"
            else:
                current = QUALITY_PROFILE_NAMES.get(
                    export.get("quality_profile"), "均衡",
                )
            preset_name = NVENC_PRESET_NAMES.get(
                export.get("nvenc_preset"), "p7 最慢",
            )
            try:
                self._export_summary.config(text=f"{current} · {preset_name}")
            except Exception:
                pass
        for key, widget in getattr(self, "_export_quick_fields", {}).items():
            main = self._export_settings.get(key)
            if main is None:
                continue
            try:
                widget.config(state=str(main.cget("state")))
            except Exception:
                pass

    def _on_super_resolution_change(self):
        scale = self._super_resolution_scale()
        width, height = self._source_size()
        color = getattr(self, '_video_color_info', None) or {}
        export = self._collect_export_settings()
        is_hdr = bool(export['hdr_mode'] and color.get('is_hdr'))
        if width > 0 and height > 0 and not self._confirm_super_resolution_export(
            width, height, scale, is_hdr=is_hdr, notify=True,
        ):
            self._export_settings['v_super_resolution'].set(SUPER_RESOLUTION_NAMES[1])
            scale = 1
        if self.playing:
            self.pause()
        self._freeze_preview_cache()
        self._cache_clear()
        self._last_dlss_frame = -1
        self._split_frame = -1
        self._split_dlss = None
        self._update_export_control_states()
        self._schedule_settings_save()
        if self.video and self.view_var.get() in ("DLSS", "对比"):
            self.display_view(quality="fast")
            self._schedule_preview_cache_resume()

    def _on_export_settings_change(self):
        self._update_export_control_states()
        self._schedule_settings_save()

    def _collect_persisted_settings(self):
        d = self._settings
        export = self._collect_export_settings()
        return {
            "preview_view": self.view_var.get(),
            "style": STYLE_CHOICES.get(d['v_style'].get(), 0),
            **self._remembered_dlss(),
            "output_view": OUTVIEW_CHOICES.get(d['v_outview'].get(), 0),
            "export_mode": export['mode'],
            "parallel_workers": export['workers'],
            "warmup_frames": export['warmup'],
            "decode_buffer": export['decode_buffer'],
            "nvenc_preset": export['nvenc_preset'],
            "output_container": export['output_container'],
            "output_resolution": export['output_resolution'],
            "super_resolution_scale": export['super_resolution_scale'],
            "custom_output_width": export['custom_output_width'],
            "custom_output_height": export['custom_output_height'],
            "rate_control": export['rate_control'],
            "quality_profile": export['quality_profile'],
            "video_bitrate_mbps": export['video_bitrate_mbps'],
            "hdr_mode": export['hdr_mode'],
            "ui_export_open": bool(
                getattr(self, "_export_section", None) and not self._export_section.collapsed
            ),
            "ui_host_open": bool(
                getattr(self, "_host_section", None) and not self._host_section.collapsed
            ),
            "ui_preview_open": bool(
                getattr(self, "_preview_section", None) and not self._preview_section.collapsed
            ),
            "ui_theme": self._ui_theme_name,
            "inspector_width": int(getattr(self, "_inspector_width", 360)),
            "preview_detached": bool(self._detached_preview_window),
            "preview_window_geometry": self._detached_geometry_for_save(),
            "queue_output_dir": (
                self.queue_output_dir_var.get().strip()
                if hasattr(self, "queue_output_dir_var") else ""
            ),
            **self._collect_preview_settings(),
            **self._collect_host_settings(),
        }

    def _schedule_settings_save(self, event=None):
        if self._settings_save_after:
            self.root.after_cancel(self._settings_save_after)
        self._settings_save_after = self.root.after(300, self._save_settings_now)

    def _save_settings_now(self):
        self._settings_save_after = None
        try:
            self._saved_settings = app_settings.save(self._collect_persisted_settings())
        except Exception as ex:
            self.logln("[设置] 保存失败: " + str(ex))

    def _on_panels_toggle(self):
        try:
            self.root.focus_set()
        except Exception:
            pass
        if hasattr(self, "_preview_canvas"):
            self.root.after_idle(self._sync_preview_scrollregion)
        if hasattr(self, "_export_canvas"):
            self.root.after_idle(self._sync_export_scrollregion)
        self._schedule_settings_save()

    def _cancel_after(self, name):
        handle = getattr(self, name, None)
        if handle is not None:
            try:
                self.root.after_cancel(handle)
            except Exception:
                pass
            setattr(self, name, None)

    def _on_close(self):
        if self._diagnosing:
            messagebox.showinfo("诊断中", "请等待诊断报告生成后再关闭程序。")
            return
        if self._exporting or self._queue_running:
            messagebox.showinfo(
                "正在导出",
                "请先点击“当前项后暂停”；如需立即停止，再点击“取消当前项”，待任务结束后关闭程序。",
            )
            return
        if self._update_downloading:
            if not messagebox.askyesno(
                "正在下载更新",
                "更新包仍在下载。是否取消下载并退出？\n\n已下载的临时文件会自动清理。",
            ):
                return
            self._update_cancel_event.set()
        self._cancel_after("_settings_save_after")
        self._cancel_after("_live_debounce")
        self._cancel_after("_output_preview_after")
        self._cancel_after("_scrub_after")
        self._cancel_after("_resize_after")
        self._cancel_after("_preview_cache_resume_after")
        self._save_settings_now()
        self._save_queue_state()
        self.pause()
        self._wait_play_dlss()
        self._audio.close()
        if getattr(self, "_cap", None):
            self._cap.release()
        self._image_bgr = None
        self._source_kind = None
        self._video_color_info = None
        self._close_live()
        self._close_super_resolution()
        if self._detached_preview_window is not None:
            ui_theme.release_app_icon(self._detached_preview_window)
        ui_theme.release_app_icon(self.root)
        self.root.destroy()

    def _parallel_progress(self, done, total, label):
        self.set_progress(done, total, label)
        self.root.update()
        self._raise_if_export_cancelled()

    def _hash_settings_dict(self, s):
        return (
            s['style'], s['intensity'], s['local_tone'], s['local_struct'],
            s['use_auto_mask'], s['skin_struct'],
            s.get('host_backend'), s.get('host_submission'),
            s.get('host_zero_fast_path'), s.get('host_persistent_buffers'),
            s.get('host_in_flight'),
            normalize_scale(s.get('super_resolution_scale', 1)),
        )

    def _settings_hash(self):
        return self._hash_settings_dict(self._collect_settings())

    def _super_resolution_scale(self, settings=None):
        if settings is not None and 'super_resolution_scale' in settings:
            return normalize_scale(settings['super_resolution_scale'])
        if hasattr(self, '_export_settings'):
            return normalize_scale(
                self._collect_export_settings()['super_resolution_scale']
            )
        return 1

    def _precise_preview_size(self):
        width, height = self._source_size()
        return super_resolution_target_size(
            width, height, self._super_resolution_scale(),
        )

    def _ensure_super_resolution(self, width, height, scale, is_hdr=False):
        scale = normalize_scale(scale)
        if scale == 1:
            return None
        key = (int(width), int(height), scale, bool(is_hdr))
        with self._super_resolution_lock:
            if self._super_resolution_live is not None and self._super_resolution_key != key:
                self._close_super_resolution()
            if self._super_resolution_live is None:
                self._super_resolution_live = ProcessSuperResolution(
                    width, height, scale, is_hdr=is_hdr,
                )
                self._super_resolution_key = key
            return self._super_resolution_live

    def _close_super_resolution(self):
        with self._super_resolution_lock:
            if self._super_resolution_live is not None:
                try:
                    self._super_resolution_live.close()
                except Exception:
                    pass
            self._super_resolution_live = None
            self._super_resolution_key = None
            self._last_super_resolution_preview = None

    def _upscale_rgba(self, rgba, scale, is_hdr=False, session=None):
        scale = normalize_scale(scale)
        if scale == 1:
            return rgba
        height, width = rgba.shape[:2]
        live = session or self._ensure_super_resolution(
            width, height, scale, is_hdr=is_hdr,
        )
        if live is None:
            raise RuntimeError("RTX 视频超分会话不可用")
        return live.process(np.ascontiguousarray(rgba))

    def _preview_composition_source(self, frame, original, processed):
        if original is None or processed is None or original.shape[:2] == processed.shape[:2]:
            return original
        cached = self._last_super_resolution_preview
        if cached is not None:
            cached_frame, cached_image = cached
            if int(cached_frame) == int(frame) and cached_image.shape[:2] == processed.shape[:2]:
                return cached_image
        return cv2.resize(
            original, (processed.shape[1], processed.shape[0]), interpolation=cv2.INTER_LANCZOS4,
        )

    def _ensure_live(self, w, h, settings=None):
        """Reuse one isolated host process for preview + strict single-session export.

        A size/backend change replaces only that disposable process, so NGX is never
        initialized twice in the long-lived GUI process.
        """
        settings = settings or self._collect_settings()
        with self._live_lock:
            try:
                need = (self._live is None) or (getattr(self, "_live_w", -1) != w) or (getattr(self, "_live_h", -1) != h)
                if need:
                    if settings.get("host_tiled_mode") and threading.current_thread() is threading.main_thread():
                        self.logln(
                            "[DLSS 大图] 使用 Feature 18 分区处理："
                            f"{w}×{h}；子区域 "
                            f"{settings.get('host_tile_width')}×{settings.get('host_tile_height')}"
                        )
                    if self._live:
                        self._live.resize(
                            w, h, int(settings.get('preset', 1)), settings=settings,
                        )
                    else:
                        self._live = ProcessLive(w, h, settings)
                    self._live.update(settings)
                    self._live_w, self._live_h = w, h
                    self._last_dlss_frame = -1
                else:
                    self._live.update(settings)
                return self._live
            except Exception as ex:
                if threading.current_thread() is threading.main_thread():
                    self.logln("[DLSS] " + str(ex))
                else:
                    self._live_error = str(ex)
                return None

    def _close_live(self):
        with self._live_lock:
            if self._live:
                try:
                    self._live.close()
                except Exception:
                    pass
                self._live = None
        self._cache_clear()
        self._last_dlss_frame = -1

    def _source_size(self, source_bgr=None):
        if source_bgr is not None:
            height, width = source_bgr.shape[:2]
            return int(width), int(height)
        return int(self._media_w), int(self._media_h)

    def _playback_preview_size(self, source_bgr=None):
        width, height = self._source_size(source_bgr)
        return _realtime_preview_size(width, height, self._preview_quality())

    def _set_realtime_preview_status(self, size):
        width, height = size or (0, 0)
        source_size = self._source_size()
        if width <= 0 or height <= 0:
            return
        if (width, height) == source_size:
            suffix = " · 暂停后生成超分精确帧" if self._super_resolution_scale() > 1 else ""
            self.set_status(f"实时预览 · 原始分辨率 {width}×{height}{suffix}")
        else:
            suffix = "并生成超分精确帧" if self._super_resolution_scale() > 1 else "恢复原始分辨率"
            self.set_status(f"实时预览 {width}×{height} · 暂停后{suffix}")

    @staticmethod
    def _cache_key(frame, size):
        try:
            width, height = size
            return int(frame), int(width), int(height)
        except (TypeError, ValueError):
            return None

    def _live_dlss_image(self, frame, source_bgr=None, settings=None, target_size=None):
        settings = settings or self._collect_settings()
        sk = self._hash_settings_dict(settings)
        source_size = self._source_size(source_bgr)
        sr_scale = self._super_resolution_scale(settings)
        if target_size is None:
            target_size = (
                super_resolution_target_size(*source_size, sr_scale)
                if sr_scale > 1 and not self.playing else source_size
            )
        cached = self._cached_dlss_sk(frame, sk, target_size)
        if cached is not None:
            return cached
        fr = source_bgr if source_bgr is not None else self._read_frame(frame)
        if fr is None:
            return None
        source_h, source_w = fr.shape[:2]
        try:
            requested_w, requested_h = map(int, target_size)
        except (TypeError, ValueError):
            requested_w, requested_h = source_w, source_h
        if requested_w <= 0 or requested_h <= 0:
            requested_w, requested_h = source_w, source_h
        sr_target = super_resolution_target_size(source_w, source_h, sr_scale)
        use_super_resolution = (
            sr_scale > 1 and not self.playing
            and (requested_w, requested_h) == sr_target
        )
        if use_super_resolution:
            target_w, target_h = sr_target
        else:
            target_w, target_h = _fit_preview_size(
                source_w, source_h, max(requested_w, requested_h)
            )
            target_w = min(target_w, requested_w)
            target_h = min(target_h, requested_h)
        cached = self._cached_dlss_sk(frame, sk, (target_w, target_h))
        if cached is not None:
            return cached
        if use_super_resolution:
            rgba_source = cv2.cvtColor(fr, cv2.COLOR_BGR2RGBA)
            try:
                rgba = self._upscale_rgba(rgba_source, sr_scale, is_hdr=False)
            except Exception as ex:
                self._live_error = str(ex)
                self.logln("[RTX 超分预览] " + str(ex))
                return None
            fr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
            self._last_super_resolution_preview = (int(frame), fr)
        elif (target_w, target_h) != (source_w, source_h):
            fr = cv2.resize(fr, (target_w, target_h), interpolation=cv2.INTER_AREA)
            rgba = cv2.cvtColor(fr, cv2.COLOR_BGR2RGBA)
        else:
            rgba = cv2.cvtColor(fr, cv2.COLOR_BGR2RGBA)
        h, w = rgba.shape[:2]
        live_settings = (
            _large_image_host_settings(w, h, settings)
            if getattr(self, "_source_kind", None) == "image" else settings
        )
        with self._live_lock:
            live = self._ensure_live(w, h, live_settings)
            if live is None:
                return None
            reset = 0 if frame == self._last_dlss_frame + 1 else 1
            o = live.process(rgba, reset=reset)
            self._last_dlss_frame = frame
        if o is None:
            self._live_cache = None
            return None
        bgr = cv2.cvtColor(o[..., :3], cv2.COLOR_RGB2BGR)
        self._cache_store(frame, sk, bgr)
        return bgr

    def load_view_img(self, view, frame):
        if view == "原图":
            original = self._source_cache_get(frame)
            return original if original is not None else self._read_frame(frame)
        if view == "DLSS":
            original = self._source_cache_get(frame)
            if original is None:
                original = self._read_frame(frame)
            if original is None:
                return None
            processed = self._live_dlss_image(frame, source_bgr=original)
            settings = self._collect_settings()
            original = self._preview_composition_source(frame, original, processed)
            return compose_preview_frame(
                original, processed,
                settings['output_view'], settings['output_mix'],
            )
        return None

    def _cached_dlss(self, frame, target_size=None):
        target_size = target_size or self._precise_preview_size()
        return self._cached_dlss_sk(frame, self._settings_hash(), target_size)

    def _cached_dlss_sk(self, frame, sk, target_size=None):
        target_size = target_size or self._precise_preview_size()
        key = self._cache_key(frame, target_size)
        if key is None:
            return None
        with self._cache_lock:
            item = self._dlss_frame_cache.get(key)
            if item is not None and item[0] == sk:
                return item[1]
            cache = self._live_cache
            if cache and cache[0] == key and cache[1] == sk:
                return cache[2]
        return None

    def _cache_store(self, frame, sk, bgr):
        try:
            frame = int(frame)
        except (TypeError, ValueError):
            return
        height, width = bgr.shape[:2]
        key = self._cache_key(frame, (width, height))
        with self._cache_lock:
            previous = self._dlss_frame_cache.get(key)
            if previous is not None:
                self._dlss_cache_bytes -= previous[1].nbytes
            self._dlss_frame_cache[key] = (sk, bgr)
            self._dlss_cache_bytes += bgr.nbytes
            self._live_cache = (key, sk, bgr)
            self._last_shown_dlss = (key, bgr)
            self._preview_processed_frames += 1
            if self._preview_process_t0 is None:
                self._preview_process_t0 = time.perf_counter()
            self._evict_preview_cache_locked()

    def _source_cache_store(self, frame, bgr):
        frame = int(frame)
        with self._cache_lock:
            previous = self._source_frame_cache.get(frame)
            if previous is not None:
                self._source_cache_bytes -= previous.nbytes
            self._source_frame_cache[frame] = bgr
            self._source_cache_bytes += bgr.nbytes
            self._evict_preview_cache_locked()

    def _source_cache_get(self, frame):
        with self._cache_lock:
            return self._source_frame_cache.get(int(frame))

    def _buffer_target_frames(self):
        capacity = self._cache_capacity_frames()
        desired = max(int(round(max(float(self.fps), 1.0) * PREVIEW_BUFFER_SECONDS)), 1)
        return max(1, min(desired, capacity))

    def _cache_capacity_frames(self):
        source_w, source_h = self._source_size()
        preview_w, preview_h = self._active_preview_size or self._playback_preview_size()
        pair_bytes = max((source_w * source_h + preview_w * preview_h) * 3, 1)
        return max(int(self._preview_cache_bytes() // pair_bytes), 1)

    def _prerender_target_frames(self):
        capacity = self._cache_capacity_frames()
        reserve = PREVIEW_QUEUE_SIZE if capacity > PREVIEW_QUEUE_SIZE else 0
        return max(self._buffer_target_frames(), capacity - reserve)

    def _evict_preview_cache_locked(self):
        budget = max(self._preview_cache_bytes(), 1)
        playhead = int(self._frame)
        protected_end = playhead + self._prerender_target_frames() - 1

        def total_bytes():
            return self._dlss_cache_bytes + self._source_cache_bytes

        while total_bytes() > budget:
            candidates = []
            for key, item in self._dlss_frame_cache.items():
                frame = key[0]
                protected = playhead <= frame <= protected_end
                candidates.append((protected, -abs(frame - playhead), "dlss", key, item[1].nbytes))
            for frame, image in self._source_frame_cache.items():
                protected = playhead <= frame <= protected_end
                candidates.append((protected, -abs(frame - playhead), "source", frame, image.nbytes))
            if not candidates:
                break
            _protected, _distance, kind, key, size = min(candidates)
            if kind == "dlss":
                self._dlss_frame_cache.pop(key, None)
                self._dlss_cache_bytes -= size
            else:
                self._source_frame_cache.pop(key, None)
                self._source_cache_bytes -= size

    def _cache_clear(self, keep_source=False):
        """Invalidate processed frames; only parameter edits can retain decoded sources."""
        with self._cache_lock:
            self._dlss_frame_cache.clear()
            if not keep_source:
                self._source_frame_cache.clear()
                self._source_cache_bytes = 0
            self._queued_preview_frames.clear()
            self._dlss_cache_bytes = 0
            self._live_cache = None
            self._last_shown_dlss = None
            self._presented_preview_key = None
            self._preview_processed_frames = 0
            self._preview_process_t0 = None
            self._last_super_resolution_preview = None

    def _canvas_size(self):
        return (
            max(self.canvas.winfo_width() or 780, 200),
            max(self.canvas.winfo_height() or 400, 150),
        )

    def _last_frame_index(self):
        if not self.video or self.nframes <= 0:
            return 0
        return max(self.nframes - 1, 0)

    def _draw_empty(self, cw=None, ch=None):
        if cw is None or ch is None:
            cw, ch = self._canvas_size()
        self._video_geom = None
        self._navigator_geom = None
        self.canvas.delete("all")
        self.canvas._chrome_live = []
        ui = self._ui
        scale = getattr(self.root, "_studio_scale", 1.0)
        compact = cw < 460 * scale or ch < 400 * scale
        cx, cy = cw // 2, ch // 2
        card_w, card_h = min(440 * scale, cw - 32), min(318 * scale, ch - 32)
        left, top = cx - card_w / 2, cy - card_h / 2
        if not compact:
            round_rect(self.canvas, left, top + 3, left + card_w, top + card_h + 3,
                       16, fill=ui["empty_shadow"], tags="empty")
            round_rect(self.canvas, left, top, left + card_w, top + card_h, 16,
                       fill=ui["empty_card"], outline=ui["accent_dim"] if self._drop_hover else ui["line"],
                       tags="empty")
            iy = cy - 92 * scale
            round_rect(self.canvas, cx - 25 * scale, iy - 25 * scale,
                       cx + 25 * scale, iy + 25 * scale, 12,
                       fill=ui["select_bg"], tags="empty")
            # A small line icon, not a font-dependent Unicode arrow/emoji.
            self.canvas.create_line(cx, iy - 10 * scale, cx, iy + 7 * scale,
                                    fill=ui["accent"], width=2, tags="empty")
            self.canvas.create_line(cx - 6 * scale, iy + scale, cx, iy + 7 * scale,
                                    cx + 6 * scale, iy + scale, fill=ui["accent"], width=2, tags="empty")
            self.canvas.create_line(cx - 12 * scale, iy + 9 * scale, cx - 12 * scale, iy + 14 * scale,
                                    cx + 12 * scale, iy + 14 * scale, cx + 12 * scale, iy + 9 * scale,
                                    fill=ui["accent"], width=2, tags="empty")
        title_y = cy - (24 if not compact else 30) * scale
        self.canvas.create_text(
            cx, title_y, text="松开以导入素材" if self._drop_hover else "拖入视频或图片",
            fill=ui["hud"], font=ui_theme.UI_FONT_TITLE,
            width=max(cw - 48, 100),
            tags="empty",
        )
        if not compact:
            self.canvas.create_text(
            cx, cy + 108 * scale, text="MP4 · MOV · MKV · WebM · PNG · JPEG · WebP · TIFF",
            fill=ui["faint"], font=ui_theme.UI_FONT_SMALL,
            tags="empty",
            )
        bw, bh = min(184 * scale, cw - 48), ui_theme.control_height(self.root, primary=True)
        bx1, by1 = cx - bw // 2, cy + (20 if not compact else 16) * scale
        self._empty_import_geom = (bx1, by1, bx1 + bw, by1 + bh)
        self._paint_empty_button()

    def display_view(self, quality="full"):
        if getattr(self, "_exporting", False):
            return
        if self.playing:
            self._present_play_frame(self._frame)
            return
        cw, ch = self._canvas_size()
        if not self.video:
            self._draw_empty(cw, ch)
            return
        frame = self._frame
        view = self.view_var.get()
        if self._hold_original:
            view = "原图"
        fast = quality == "fast" and view in ("DLSS", "对比") and self._cached_dlss(frame) is None
        self._dlss_pending = bool(fast)
        if view == "对比":
            self._draw_split(frame, cw, ch, fast=fast)
            return
        if fast:
            img = self._read_frame(frame)
            badge = "预览原图 · 松手生成 DLSS"
        else:
            img = self.load_view_img(view, frame)
            badge = "原图（按住 Alt）" if self._hold_original and self.view_var.get() != "原图" else None
        if img is None:
            self.canvas.delete("all")
            msg = f"{view}：帧 {frame} 读取失败" if view == "原图" else f"DLSS：帧 {frame} 生成失败"
            self.canvas.create_text(
                cw // 2, ch // 2, text=msg, fill=self._ui_color("muted", "#888888"),
                font=ui_theme.UI_FONT,
            )
            return
        self._draw_fit(img, cw, ch, badge=badge)

    def _canvas_shadow_text(self, x, y, text, fill=None, shadow="#000000", **kwargs):
        if fill is None:
            fill = self._ui_color("overlay", HUD_FILL)
        self.canvas.create_text(
            x + 1, y + 1, text=text, fill=shadow, **kwargs,
        )
        return self.canvas.create_text(
            x, y, text=text, fill=fill, **kwargs,
        )

    def _render_viewport_image(self, img, cw, ch, track=True):
        ih, iw = img.shape[:2]
        layout = _preview_viewport(
            iw, ih, cw, ch,
            self._preview_zoom, self._preview_pan_x, self._preview_pan_y,
        )
        if layout is None:
            return None, (0, 0, 0, 0)
        (x0, y0, x1, y1), (dx, dy, dw, dh), center, scale = layout
        ix0 = max(0, min(int(math.floor(x0)), iw - 1))
        iy0 = max(0, min(int(math.floor(y0)), ih - 1))
        ix1 = max(ix0 + 1, min(int(math.ceil(x1)), iw))
        iy1 = max(iy0 + 1, min(int(math.ceil(y1)), ih))
        crop = img[iy0:iy1, ix0:ix1]
        nw, nh = max(int(round(dw)), 1), max(int(round(dh)), 1)
        interpolation = (
            cv2.INTER_AREA
            if nw < crop.shape[1] or nh < crop.shape[0]
            else cv2.INTER_LINEAR
        )
        rendered = cv2.resize(crop, (nw, nh), interpolation=interpolation)
        ox, oy = int(round(dx)), int(round(dy))
        if track:
            self._preview_pan_x, self._preview_pan_y = center
            self._viewport_crop_norm = (
                x0 / iw, y0 / ih, x1 / iw, y1 / ih,
            )
            self._viewport_scale = scale
            self._viewport_source_size = (iw, ih)
            self._video_geom = (ox, oy, nw, nh)
        return rendered, (ox, oy, nw, nh)

    def _draw_navigator(self, img, cw, ch):
        crop = getattr(self, "_viewport_crop_norm", (0.0, 0.0, 1.0, 1.0))
        if (
            self._preview_zoom <= 1.0 + 1e-6
            or (crop[0] <= 1e-6 and crop[1] <= 1e-6
                and crop[2] >= 1.0 - 1e-6 and crop[3] >= 1.0 - 1e-6)
        ):
            self._navigator_geom = None
            self._nav_photo = None
            self._navigator_source = None
            self._navigator_thumb = None
            self._navigator_thumb_size = None
            return
        ih, iw = img.shape[:2]
        max_width = min(NAVIGATOR_MAX_WIDTH, max(int(cw * 0.22), 64))
        max_height = min(NAVIGATOR_MAX_HEIGHT, max(int(ch * 0.22), 48))
        scale = min(max_width / iw, max_height / ih, 1.0)
        nw, nh = max(int(round(iw * scale)), 1), max(int(round(ih * scale)), 1)
        if self._navigator_source is img and self._navigator_thumb_size == (nw, nh):
            thumb = self._navigator_thumb
        else:
            thumb = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
            self._navigator_source = img
            self._navigator_thumb = thumb
            self._navigator_thumb_size = (nw, nh)
        from PIL import Image, ImageTk
        self._nav_pilimg = Image.fromarray(cv2.cvtColor(thumb, cv2.COLOR_BGR2RGB))
        self._nav_photo = ImageTk.PhotoImage(self._nav_pilimg)
        ox = max(cw - nw - NAVIGATOR_MARGIN, 0)
        oy = max(ch - nh - NAVIGATOR_MARGIN, 0)
        self._navigator_geom = (ox, oy, nw, nh)
        self.canvas.create_rectangle(
            ox - 3, oy - 3, ox + nw + 3, oy + nh + 3,
            outline="#050505", width=3, tags=("navigator",),
        )
        self.canvas.create_image(
            ox, oy, anchor="nw", image=self._nav_photo, tags=("navigator",),
        )
        vx0 = ox + crop[0] * nw
        vy0 = oy + crop[1] * nh
        vx1 = ox + crop[2] * nw
        vy1 = oy + crop[3] * nh
        self.canvas.create_rectangle(
            vx0, vy0, vx1, vy1,
            outline="#0a0a0a", width=4, tags=("navigator", "navigator_view"),
        )
        self.canvas.create_rectangle(
            vx0, vy0, vx1, vy1,
            outline=self._ui_color("split", SPLIT_LINE), width=2, tags=("navigator", "navigator_view"),
        )

    def _draw_pending_status(self, cw, ch):
        if not self._dlss_pending:
            return
        nav = getattr(self, "_navigator_geom", None)
        y = nav[1] - 8 if nav is not None else ch - 10
        self.canvas.create_text(
            cw - 10, y, text="DLSS…", fill=self._ui_color("muted", "#aaaaaa"), anchor="se",
            font=ui_theme.UI_FONT_SMALL,
        )

    def _draw_fit(self, img, cw, ch, badge=None):
        self._last_viewport_image = img if abs(self._preview_zoom - 1.0) > 1e-6 else None
        nimg, (ox, oy, nw, nh) = self._render_viewport_image(img, cw, ch)
        if nimg is None:
            return
        from PIL import Image, ImageTk
        self._pilimg = Image.fromarray(cv2.cvtColor(nimg, cv2.COLOR_BGR2RGB))
        self._photo = ImageTk.PhotoImage(self._pilimg)
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)
        if badge:
            self._canvas_shadow_text(
                ox + 10, oy + 14, badge, fill=self._ui_color("hud", HUD_FILL), anchor="w",
                font=ui_theme.UI_FONT_SMALL,
            )
        self._draw_navigator(img, cw, ch)
        self._draw_pending_status(cw, ch)

    def _draw_split(self, frame, cw, ch, fast=False):
        need = (
            getattr(self, "_split_frame", -1) != frame
            or getattr(self, "_split_orig", None) is None
        )
        if need:
            orig = self._source_cache_get(frame)
            if orig is None:
                orig = self._read_frame(frame)
            if orig is None:
                self.canvas.delete("all")
                self.canvas.create_text(
                    cw // 2, ch // 2, text=f"帧 {frame} 读取失败",
                    fill=self._ui_color("muted", "#888888"),
                    font=ui_theme.UI_FONT,
                )
                return
            self._split_orig = orig
            self._split_frame = frame
            self._split_size = (cw, ch)
            if fast:
                self._split_dlss = None
            else:
                dlss = self._live_dlss_image(frame, source_bgr=orig)
                if dlss is None:
                    self._split_dlss = None
                    self._draw_fit(orig, cw, ch, badge="DLSS 生成失败")
                    return
                settings = self._collect_settings()
                orig = self._preview_composition_source(frame, orig, dlss)
                dlss = compose_preview_frame(
                    orig, dlss,
                    settings['output_view'], settings['output_mix'],
                )
                self._split_dlss = dlss
        elif not fast and self._split_dlss is None:
            orig = self._source_cache_get(frame)
            if orig is None:
                orig = self._read_frame(frame)
            dlss = self._live_dlss_image(frame, source_bgr=orig) if orig is not None else None
            if dlss is None:
                self._draw_fit(self._split_orig, cw, ch, badge="DLSS 生成失败")
                return
            settings = self._collect_settings()
            orig = self._preview_composition_source(frame, orig, dlss)
            dlss = compose_preview_frame(
                orig, dlss,
                settings['output_view'], settings['output_mix'],
            )
            self._split_dlss = dlss
        self._blit_split(cw, ch)

    def _blit_split(self, cw, ch):
        original, (ox, oy, nw, nh) = self._render_viewport_image(
            self._split_orig, cw, ch,
        )
        if original is None:
            return
        processed = None
        if self._split_dlss is not None:
            processed, processed_geom = self._render_viewport_image(
                self._split_dlss, cw, ch, track=False,
            )
            if processed_geom != (ox, oy, nw, nh):
                processed = cv2.resize(processed, (nw, nh), interpolation=cv2.INTER_LINEAR)
        self._split_nw, self._split_nh = nw, nh
        self._drag_nw = nw
        self._drag_offsetx = ox
        composed = original
        show_divider = self.view_var.get() == "对比" and not self._hold_original
        if show_divider:
            composed = original.copy()
            sx = int(self.split_x * nw)
            if processed is not None:
                composed[:, sx:] = processed[:, sx:]
        from PIL import Image, ImageTk
        self._pilimg = Image.fromarray(cv2.cvtColor(composed, cv2.COLOR_BGR2RGB))
        self._photo = ImageTk.PhotoImage(self._pilimg)
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)
        if show_divider:
            sx_abs = ox + int(self.split_x * nw)
            self.canvas.create_line(
                sx_abs, oy, sx_abs, oy + nh, fill=self._ui_color("split", SPLIT_LINE), width=2, tags=("split",),
            )
            handle_y = oy + nh // 2
            self.canvas.create_oval(
                sx_abs - 5, handle_y - 5, sx_abs + 5, handle_y + 5,
                fill=self._ui_color("split", SPLIT_LINE),
                outline=self._ui_color("canvas", CANVAS_BG),
                width=1, tags=("split",),
            )
            self._canvas_shadow_text(
                ox + 10, oy + 14, "原图", anchor="w",
                font=ui_theme.UI_FONT_SMALL,
            )
            self._canvas_shadow_text(
                ox + nw - 10, oy + 14,
                "DLSS 生成中…" if self._dlss_pending else "DLSS",
                anchor="e",
                font=ui_theme.UI_FONT_SMALL,
            )
        elif self._hold_original:
            self._canvas_shadow_text(
                ox + 10, oy + 14, "原图（按住 Alt）", anchor="w",
                font=ui_theme.UI_FONT_SMALL,
            )
        navigator_image = self._split_dlss if self._split_dlss is not None else self._split_orig
        self._draw_navigator(navigator_image, cw, ch)
        self._draw_pending_status(cw, ch)

    def _split_x_abs(self):
        geom = getattr(self, "_video_geom", None)
        if not geom:
            return None
        ox, _oy, nw, _nh = geom
        return ox + int(self.split_x * nw)

    def _near_split(self, x):
        if self.view_var.get() != "对比" or self._hold_original:
            return False
        sx = self._split_x_abs()
        if sx is None:
            return False
        return abs(x - sx) <= SPLIT_HIT_PX

    def _update_split_from_event(self, event):
        geom = getattr(self, "_video_geom", None)
        if not geom:
            return
        ox, _oy, nw, _nh = geom
        self.split_x = max(0.0, min(1.0, (event.x - ox) / max(nw, 1)))
        cw, ch = self._canvas_size()
        if getattr(self, "_split_orig", None) is not None:
            self._blit_split(cw, ch)
        else:
            self.display_view(
                quality="fast" if self._preview_cache_frozen else "full"
            )

    def _point_in_navigator(self, x, y):
        geom = getattr(self, "_navigator_geom", None)
        if geom is None:
            return False
        ox, oy, nw, nh = geom
        return ox - 3 <= x <= ox + nw + 3 and oy - 3 <= y <= oy + nh + 3

    def _point_in_video(self, x, y):
        geom = getattr(self, "_video_geom", None)
        if geom is None:
            return False
        ox, oy, nw, nh = geom
        return ox <= x <= ox + nw and oy <= y <= oy + nh

    def _refresh_viewport_display(self):
        if not self.video or self._exporting:
            return
        cw, ch = self._canvas_size()
        if (
            self.view_var.get() == "对比"
            and getattr(self, "_split_orig", None) is not None
            and getattr(self, "_split_frame", -1) == self._frame
        ):
            self._blit_split(cw, ch)
            return
        image = getattr(self, "_last_viewport_image", None)
        if image is not None:
            self._draw_fit(image, cw, ch)
        else:
            self.display_view(
                quality="fast" if self._preview_cache_frozen else "full"
            )

    def _update_pan_from_navigator(self, event):
        geom = getattr(self, "_navigator_geom", None)
        if geom is None:
            return
        ox, oy, nw, nh = geom
        self._preview_pan_x = max(0.0, min(1.0, (event.x - ox) / max(nw, 1)))
        self._preview_pan_y = max(0.0, min(1.0, (event.y - oy) / max(nh, 1)))
        self._refresh_viewport_display()

    def _empty_button_hit(self, x, y):
        geom = getattr(self, "_empty_import_geom", None)
        if not geom or self.video:
            return False
        x0, y0, x1, y1 = geom
        return x0 <= x <= x1 and y0 <= y <= y1

    def _cancel_empty_btn_anim(self):
        after_id = getattr(self, "_empty_btn_after", None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
            self._empty_btn_after = None

    def _set_empty_btn_hover(self, hover):
        target = 1.0 if hover else 0.0
        if abs(getattr(self, "_empty_btn_target", 0.0) - target) < 1e-6 and (
            self._empty_btn_after is None
        ):
            return
        self._empty_btn_target = target
        self._tick_empty_btn_anim()

    def _tick_empty_btn_anim(self):
        self._cancel_empty_btn_anim()
        current = float(getattr(self, "_empty_btn_hover", 0.0) or 0.0)
        target = float(getattr(self, "_empty_btn_target", 0.0) or 0.0)
        nxt = current + (target - current) * 0.28
        if abs(nxt - target) < 0.02:
            nxt = target
        self._empty_btn_hover = nxt
        if not self.video:
            self._paint_empty_button()
        if nxt != target:
            self._empty_btn_after = self.root.after(16, self._tick_empty_btn_anim)

    def _paint_empty_button(self):
        geom = getattr(self, "_empty_import_geom", None)
        if geom is None:
            return
        try:
            self.canvas.delete("empty_pick")
        except tk.TclError:
            return
        ui = self._ui
        hover = float(getattr(self, "_empty_btn_hover", 0.0) or 0.0)
        x0, y0, x1, y1 = geom
        lift = 2 * hover
        bx1, by1 = x0, y0 - lift
        bx2, by2 = x1, y1 - lift
        fill = _lerp_hex(ui["primary_bg"], ui.get("primary_active", ui["primary_bg"]), hover)
        round_rect(
            self.canvas, bx1, by1, bx2, by2, ui_theme.RADIUS_CONTROL,
            fill=fill, outline=ui.get("primary_highlight", ui["accent_dim"]), width=1,
            tags=("empty", "empty_pick"), smooth=False,
        )
        if hover > 0.15:
            self.canvas.create_line(
                bx1 + 10, by1 + 2, bx2 - 10, by1 + 2,
                fill=ui.get("primary_highlight", fill), width=1,
                tags=("empty", "empty_pick"),
            )
        self.canvas.create_text(
            (bx1 + bx2) / 2, (by1 + by2) / 2, text="选择文件",
            fill=ui["primary_fg"], font=ui_theme.UI_FONT_BOLD,
            tags=("empty", "empty_pick"),
        )

    def on_canvas_press(self, event):
        if not self.video or self._exporting:
            if (
                not self.video
                and not self._exporting
                and not self._queue_running
                and not self._diagnosing
                and self._empty_button_hit(event.x, event.y)
            ):
                self.import_media()
            return
        self._focus_preview_host()
        if self._hold_original and not _alt_is_down():
            self._set_hold_original(False)
        if self._point_in_navigator(event.x, event.y):
            self.pause()
            self._freeze_preview_cache(resume_ms=None)
            self._drag_split = False
            self._canvas_press = ("navigator", event.x, event.y)
            self.canvas.config(cursor="hand2")
            self._update_pan_from_navigator(event)
            return
        shift = bool(event.state & 0x0001)
        if self.view_var.get() == "对比" and (self._near_split(event.x) or shift):
            self.pause()
            self._freeze_preview_cache(resume_ms=None)
            self._drag_split = True
            self._split_moved = False
            self._canvas_press = ("split", event.x, event.y)
            self.canvas.config(cursor="sb_h_double_arrow")
            self._update_split_from_event(event)
            return
        self._drag_split = False
        if self._preview_zoom > 1.0 and self._point_in_video(event.x, event.y):
            self.pause()
            self._freeze_preview_cache(resume_ms=None)
            self._pan_moved = False
            self._canvas_press = (
                "pan", event.x, event.y,
                self._preview_pan_x, self._preview_pan_y,
                self._viewport_scale, self._viewport_source_size,
            )
            self.canvas.config(cursor="fleur")
            return
        kind = "compare" if self.view_var.get() == "对比" else "click"
        self._canvas_press = (kind, event.x, event.y)

    def on_canvas_drag(self, event):
        press = self._canvas_press
        if press and press[0] == "navigator":
            self._update_pan_from_navigator(event)
            return
        if press and press[0] == "pan":
            dx, dy = event.x - press[1], event.y - press[2]
            if abs(dx) > 3 or abs(dy) > 3:
                self._pan_moved = True
            scale = max(float(press[5]), 1e-9)
            source_w, source_h = press[6]
            self._preview_pan_x = press[3] - dx / (scale * max(source_w, 1))
            self._preview_pan_y = press[4] - dy / (scale * max(source_h, 1))
            self._refresh_viewport_display()
            self.canvas.config(cursor="fleur")
            return
        if not self._drag_split:
            if not press or press[0] != "compare":
                return
            dx, dy = event.x - press[1], event.y - press[2]
            if abs(dx) <= 6 or abs(dx) < abs(dy):
                return
            self.pause()
            self._freeze_preview_cache(resume_ms=None)
            self._drag_split = True
            self.canvas.config(cursor="sb_h_double_arrow")
        self._split_moved = True
        self._update_split_from_event(event)

    def on_canvas_release(self, event):
        if self._drag_split:
            self._drag_split = False
            self._canvas_press = None
            self.on_canvas_hover(event)
            self._schedule_preview_cache_resume()
            return
        press = self._canvas_press
        self._canvas_press = None
        if not press or not self.video or self._exporting:
            return
        if press[0] == "navigator":
            self.on_canvas_hover(event)
            self._schedule_preview_cache_resume()
            return
        if press[0] == "pan":
            moved = self._pan_moved
            self._pan_moved = False
            if not moved and self.view_var.get() == "对比":
                self._update_split_from_event(event)
            elif not moved:
                self.toggle_play()
            if not self.playing:
                self._schedule_preview_cache_resume()
            self.on_canvas_hover(event)
            return
        if press[0] not in ("click", "compare"):
            return
        if abs(event.x - press[1]) > 6 or abs(event.y - press[2]) > 6:
            return
        if press[0] == "compare":
            self._update_split_from_event(event)
            self.on_canvas_hover(event)
            return
        self.toggle_play()

    def on_canvas_hover(self, event):
        if self._drag_split:
            self.canvas.config(cursor="sb_h_double_arrow")
            return
        if not self.video:
            hit = self._empty_button_hit(event.x, event.y)
            self.canvas.config(cursor="hand2" if hit else "")
            self._set_empty_btn_hover(hit)
            return
        if self._point_in_navigator(event.x, event.y):
            self.canvas.config(cursor="hand2")
        elif self._near_split(event.x):
            self.canvas.config(cursor="sb_h_double_arrow")
        elif self._preview_zoom > 1.0 and self._point_in_video(event.x, event.y):
            self.canvas.config(cursor="fleur")
        else:
            self.canvas.config(cursor="")

    def _on_canvas_leave(self, event=None):
        try:
            self.canvas.config(cursor="")
        except tk.TclError:
            pass
        if not self.video:
            self._set_empty_btn_hover(False)

    def on_canvas_double(self, event):
        if self._point_in_navigator(event.x, event.y):
            return "break"
        if self.video and self.view_var.get() == "对比" and self._near_split(event.x):
            self.split_x = 0.5
            cw, ch = self._canvas_size()
            if getattr(self, "_split_orig", None) is not None:
                self._blit_split(cw, ch)
            else:
                self.display_view(quality="full")
            return "break"
        if self.video:
            self.toggle_fullscreen()
        return "break"

    def toggle_fullscreen(self):
        self._focus_preview_host()
        if self._fullscreen:
            self._exit_fullscreen()
            return
        if self._exporting or self._queue_running:
            return
        self._enter_fullscreen()

    def _set_fs_btn(self, fullscreen):
        try:
            self.fs_btn.config(
                text="退出" if fullscreen else "全屏",
                icon="fullscreen-exit" if fullscreen else "fullscreen",
            )
        except Exception:
            pass

    def _enter_fullscreen(self):
        if self._fullscreen:
            return
        target = self._preview_host_window()
        self._fullscreen = True
        self._fs_window = target
        self._fs_used_zoomed = False
        self._fs_geom = target.geometry()
        self._fs_hidden = []
        if target is self.root:
            for widget in (
                self._status_bar, self.log, self._progress_rule,
                self._detached_preview_placeholder,
            ):
                if widget is None:
                    continue
                try:
                    info = widget.pack_info()
                except Exception:
                    continue
                self._fs_hidden.append((widget, info))
                widget.pack_forget()
            try:
                info = self._inspector.pack_info()
                self._fs_hidden.append((self._inspector, info))
                self._inspector.pack_forget()
            except Exception:
                pass
        try:
            self.canvas.pack_configure(padx=0, pady=0)
            self.transport.pack_configure(padx=0, pady=0)
        except Exception:
            pass
        try:
            target.attributes("-fullscreen", True)
        except Exception:
            target.state("zoomed")
            self._fs_used_zoomed = True
        self._set_fs_btn(True)
        self._refresh_preview_surface()

    def _exit_fullscreen(self):
        if not self._fullscreen:
            return
        self._fullscreen = False
        target = self._fs_window or self._preview_host_window()
        try:
            target.attributes("-fullscreen", False)
        except Exception:
            pass
        if self._fs_used_zoomed:
            try:
                target.state("normal")
            except Exception:
                pass
        if self._fs_geom:
            try:
                target.geometry(self._fs_geom)
            except Exception:
                pass
        try:
            self.canvas.pack_configure(padx=0, pady=0)
            self.transport.pack_configure(padx=0, pady=0)
        except Exception:
            pass
        for widget, info in self._fs_hidden:
            try:
                widget.pack(**info)
            except Exception:
                pass
        self._fs_hidden = []
        self._restack_bottom_chrome()
        if target is self._detached_preview_window and self._fs_geom:
            self._detached_preview_geometry = self._fs_geom
        self._fs_window = None
        self._fs_used_zoomed = False
        self._set_fs_btn(False)
        self._schedule_settings_save()
        self._refresh_preview_surface()

    def _refresh_preview_surface(self):
        """Redraw the active pane with the same freeze/fast/resume policy as resize."""
        if self.video and not self.playing:
            self._freeze_preview_cache()
        self._cancel_after("_resize_after")
        self._resize_after = self.root.after(CANVAS_RESIZE_MS, self._apply_canvas_resize)

    def _on_canvas_configure(self, event):
        if event.widget is not self.canvas:
            return
        self._refresh_preview_surface()

    def _apply_canvas_resize(self):
        self._resize_after = None
        self._split_size = None
        if self.playing:
            self._present_play_frame(self._frame)
        elif self.video:
            quality = (
                "fast"
                if self._scrub_after or self._preview_cache_frozen
                else "full"
            )
            self.display_view(quality=quality)
        else:
            self._draw_empty()

    # ---------- frame / view ----------
    def _sync_transport_labels(self):
        last = self._last_frame_index()
        try:
            self.time_label.config(
                text=f"{_format_timecode(self._frame, self.fps)} / {_format_timecode(last, self.fps)}"
            )
        except Exception:
            pass
        try:
            self.ftotal.config(text=f"/ {last}")
        except Exception:
            pass
        self.sync_frame_entry()

    def _goto_frame(self, frame, quality="full"):
        last = self._last_frame_index()
        frame = _clamp_frame(frame, last)
        self._frame = frame
        if getattr(self, "timeline", None) is not None and self.timeline.get() != frame:
            self.timeline.set(frame)
        self._sync_transport_labels()
        if (
            quality == "full"
            and not self.playing
            and self.view_var.get() in ("DLSS", "对比")
            and not self._hold_original
        ):
            self._display_precise_preview()
        else:
            self.display_view(quality=quality)
        if (
            quality == "fast"
            and self.view_var.get() in ("DLSS", "对比")
            and not self._hold_original
            and not self.playing
        ):
            self._schedule_full_preview()
        else:
            self._cancel_after("_scrub_after")

    def _schedule_full_preview(self):
        self._cancel_after("_scrub_after")
        if getattr(self, "_preview_cache_frozen", False):
            # Interaction owns the resume timer. Restarting it here would let a
            # paused timeline drag refill the cache while the pointer is down.
            return
        delay = max(0, int(self._preview_scrub_ms()))
        self._scrub_after = self.root.after(delay, self._apply_full_preview)

    def _apply_full_preview(self):
        self._scrub_after = None
        if (
            getattr(self, "_preview_cache_frozen", False)
            or self.playing or not self.video or self._exporting
        ):
            return
        wants_dlss = self.view_var.get() in ("DLSS", "对比") and not self._hold_original
        precise_size = self._precise_preview_size()
        if (
            wants_dlss and not self._is_image
            and self._cached_dlss(self._frame, precise_size) is None
        ):
            # Exact paused previews used to run synchronously here and could lock
            # Tk during a GPU evaluation. Queue that current frame on the worker;
            # the UI keeps showing the inexpensive source preview meanwhile.
            self.display_view(quality="fast")
            if self._start_paused_prerender(target_size=precise_size):
                self._update_preview_timeline_and_status(force=True)
            return
        self._display_precise_preview()
        if self._start_paused_prerender():
            self._update_preview_timeline_and_status(force=True)

    def _display_precise_preview(self):
        source_size = self._source_size()
        precise_size = self._precise_preview_size()
        sr_scale = self._super_resolution_scale()
        wants_dlss = self.view_var.get() in ("DLSS", "对比") and not self._hold_original
        if wants_dlss:
            # Playback keeps a canvas-sized split image; invalidate it so compare
            # mode uses the full-resolution cache (or generates it) after pausing.
            self._split_frame = -1
            self._split_dlss = None
        if wants_dlss and self._cached_dlss(self._frame, precise_size) is None:
            if sr_scale > 1:
                self.set_status(f"正在生成 {sr_scale}× 超分精确预览…")
            else:
                self.set_status("正在生成原始分辨率精确预览…")
        self.display_view(quality="full")
        if wants_dlss and self._cached_dlss(self._frame, precise_size) is not None:
            width, height = precise_size
            prefix = f"{sr_scale}× 超分" if sr_scale > 1 else "精确预览"
            self.set_status(f"{prefix} · {width}×{height}")

    def _on_timeline_seek(self, frame, phase):
        if not self.video or self._exporting:
            return
        self._focus_preview_host()
        if self.playing:
            self.pause()
        if phase in ("start", "move"):
            self._freeze_preview_cache(resume_ms=None)
            self._goto_frame(frame, quality="fast")
            return
        self._goto_frame(frame, quality="fast")
        self._schedule_preview_cache_resume()

    def _update_zoom_controls(self):
        zoom = max(PREVIEW_ZOOM_MIN, min(self._preview_zoom, PREVIEW_ZOOM_MAX))
        fitted = abs(zoom - 1.0) < 0.005
        text = "适应" if fitted else f"{int(round(zoom * 100))}%"
        try:
            self.zoom_reset_btn.config(
                text=text,
                icon="fit" if fitted else "",
                icon_only=fitted,
            )
            enabled = bool(self.video) and not self._exporting
            self._set_ttk_enabled(
                self.zoom_out_btn, enabled and zoom > PREVIEW_ZOOM_MIN + 1e-6,
            )
            self._set_ttk_enabled(self.zoom_reset_btn, enabled)
            self._set_ttk_enabled(
                self.zoom_in_btn, enabled and zoom < PREVIEW_ZOOM_MAX - 1e-6,
            )
        except Exception:
            pass

    def _set_preview_zoom(self, zoom, anchor=None):
        if not self.video or self._exporting:
            return False
        try:
            zoom = float(zoom)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(zoom):
            return False
        zoom = max(PREVIEW_ZOOM_MIN, min(zoom, PREVIEW_ZOOM_MAX))
        self.pause()
        self._freeze_preview_cache()
        old_zoom = self._preview_zoom
        if anchor is not None:
            geom = getattr(self, "_video_geom", None)
            crop = getattr(self, "_viewport_crop_norm", None)
            source_size = getattr(self, "_viewport_source_size", None)
            if geom is not None and crop is not None and source_size is not None:
                ox, oy, nw, nh = geom
                rel_x = max(0.0, min(1.0, (anchor[0] - ox) / max(nw, 1)))
                rel_y = max(0.0, min(1.0, (anchor[1] - oy) / max(nh, 1)))
                source_x = crop[0] + rel_x * (crop[2] - crop[0])
                source_y = crop[1] + rel_y * (crop[3] - crop[1])
                source_w, source_h = source_size
                cw, ch = self._canvas_size()
                fit_scale = min(cw / max(source_w, 1), ch / max(source_h, 1))
                new_scale = max(fit_scale * zoom, 1e-9)

                def anchored_center(source, canvas, source_at_pointer, pointer):
                    if source * new_scale <= canvas + 1e-6:
                        return 0.5
                    visible = canvas / new_scale
                    pointer = max(0.0, min(float(pointer), float(canvas)))
                    start = source_at_pointer * source - pointer / new_scale
                    return (start + visible / 2.0) / source

                self._preview_pan_x = anchored_center(
                    source_w, cw, source_x, anchor[0],
                )
                self._preview_pan_y = anchored_center(
                    source_h, ch, source_y, anchor[1],
                )
        self._preview_zoom = zoom
        self._update_zoom_controls()
        if abs(zoom - old_zoom) > 1e-9 or anchor is None:
            self._refresh_viewport_display()
        return True

    def _step_zoom(self, direction):
        factor = PREVIEW_ZOOM_STEP if int(direction) > 0 else 1.0 / PREVIEW_ZOOM_STEP
        self._set_preview_zoom(self._preview_zoom * factor)

    def reset_preview_zoom(self):
        self._preview_pan_x = 0.5
        self._preview_pan_y = 0.5
        self._set_preview_zoom(1.0)

    def _on_canvas_wheel(self, event):
        delta = getattr(event, "delta", 0)
        if not self.video or self._exporting or not delta:
            return "break"
        notches = max(1, min(abs(int(delta)) // 120 or 1, 4))
        factor = PREVIEW_ZOOM_STEP ** notches
        zoom = self._preview_zoom * (factor if delta > 0 else 1.0 / factor)
        self._set_preview_zoom(zoom, anchor=(event.x, event.y))
        return "break"

    def _on_wheel_step(self, event):
        if not self.video or self._exporting:
            return
        wheel = getattr(event, "delta", 0)
        if not wheel:
            return "break"
        delta = -1 if wheel < 0 else 1
        self.step_frame(delta)
        return "break"

    def step_frame(self, delta):
        if not self.video or self._exporting:
            return
        self.pause()
        self._freeze_preview_cache()
        self._goto_frame(self._frame + int(delta), quality="fast")
        self._focus_preview_host()

    def skip_seconds(self, seconds):
        if not self.video or self._exporting:
            return
        self.pause()
        self._freeze_preview_cache()
        frames = int(round(float(seconds) * max(self.fps, 1.0)))
        self._goto_frame(self._frame + frames, quality="fast")

    def jump_frame(self, frame):
        if not self.video or self._exporting:
            return
        self.pause()
        self._freeze_preview_cache()
        if frame < 0:
            frame = self._last_frame_index()
        self._goto_frame(frame, quality="fast")

    def on_frame_entry(self, event=None):
        txt = self.fentry.get().strip()
        try:
            f = int(float(txt))
        except ValueError:
            self.sync_frame_entry()
            return
        self.pause()
        self._freeze_preview_cache()
        self._goto_frame(f, quality="fast")

    def sync_frame_entry(self):
        try:
            focus = self.root.focus_get()
            entry = self.fentry
            if focus is entry or focus is getattr(entry, "inner", None) or focus is getattr(entry, "entry", None):
                return
            txt = str(int(self._frame))
            if self.fentry.get().strip() != txt:
                self.fentry.delete(0, "end")
                self.fentry.insert(0, txt)
        except Exception:
            pass

    def on_view_change(self):
        self._schedule_settings_save()
        if not self.video:
            self._draw_empty()
            return
        if self.view_var.get() == "原图":
            self._stop_paused_prerender()
            self._cancel_after("_preview_cache_resume_after")
            self._preview_cache_frozen = False
            self._active_preview_size = None
            self.set_status("")
            if self.playing and self._buffering:
                self._resume_play_clock(self._frame)
        elif self.playing:
            if self._preview_frame_queue is None or self._prefetch_stop.is_set():
                self._start_strict_preview_buffering()
        else:
            self._freeze_preview_cache(resume_ms=None)
            self._schedule_preview_cache_resume()
        self._split_size = None
        self.display_view(
            quality="fast" if self._preview_cache_frozen else "full"
        )

    def on_settings_change(self, event=None):
        if self.playing:
            self.pause()
        self._freeze_preview_cache(resume_ms=None)
        self._update_dlss_control_states()
        self._schedule_settings_save()
        self._cancel_after("_live_debounce")
        self._live_debounce = self.root.after(60, self._refresh_dlss)

    def on_output_settings_change(self, event=None):
        """Refresh the composed preview while keeping the expensive DLSS cache intact."""
        self._update_dlss_control_states()
        self._schedule_settings_save()
        self._cancel_after("_output_preview_after")
        self._output_preview_after = self.root.after(16, self._refresh_output_preview)

    def _refresh_output_preview(self):
        self._output_preview_after = None
        self._split_frame = -1
        self._split_dlss = None
        if not self.video or self.view_var.get() not in ("DLSS", "对比"):
            return
        if self.playing:
            self._present_play_frame(self._frame)
        else:
            self.display_view(quality="full")

    def _refresh_dlss(self):
        self._live_debounce = None
        thread = getattr(self, "_play_dlss_thread", None)
        if thread is not None and thread.is_alive():
            self._live_debounce = self.root.after(
                PREVIEW_WORKER_POLL_MS, self._refresh_dlss,
            )
            return
        self._play_dlss_thread = None
        self._play_dlss_busy = False
        if self._live:
            try:
                with self._live_lock:
                    self._live.update(self._collect_settings())
            except Exception as ex:
                self.logln("[DLSS 参数] " + str(ex))
        # DLSS parameters do not change decoded video pixels. Preserve these
        # within the same shared RAM budget instead of seeking/decoding again.
        self._cache_clear(keep_source=True)
        try:
            self.timeline.set_cache_ranges([], [])
        except Exception:
            pass
        self._split_frame = -1
        self._split_dlss = None
        if self.view_var.get() in ("DLSS", "对比"):
            self.display_view(quality="fast")
        self._schedule_preview_cache_resume()

    def _input_widget_focused(self, extra=()):
        w = self.root.focus_get()
        if w is None:
            return False
        try:
            cls = w.winfo_class()
        except Exception:
            return False
        return cls in _INPUT_WIDGETS or cls in extra

    def _preview_tab_selected(self):
        window = self._detached_preview_window
        if window is None:
            return True
        focused = self.root.focus_get()
        if focused is None:
            return False
        try:
            return focused.winfo_toplevel() is window
        except Exception:
            return False

    def _bind_player_keys(self):
        self.root.bind_all("<space>", self.on_space)
        self.root.bind_all("<Left>", lambda e: self._on_step_key(-1))
        self.root.bind_all("<Right>", lambda e: self._on_step_key(1))
        self.root.bind_all("<Shift-Left>", lambda e: self._on_skip_key(-1))
        self.root.bind_all("<Shift-Right>", lambda e: self._on_skip_key(1))
        self.root.bind_all("<Home>", lambda e: self._on_jump_key(0))
        self.root.bind_all("<End>", lambda e: self._on_jump_key(-1))
        self.root.bind_all("<Key-1>", lambda e: self._on_view_hotkey("原图"))
        self.root.bind_all("<Key-2>", lambda e: self._on_view_hotkey("DLSS"))
        self.root.bind_all("<Key-3>", lambda e: self._on_view_hotkey("对比"))
        self.root.bind_all("<Key-0>", self._on_zoom_reset_key)
        self.root.bind_all("<KeyPress-plus>", lambda e: self._on_zoom_key(1))
        self.root.bind_all("<KeyPress-equal>", lambda e: self._on_zoom_key(1))
        self.root.bind_all("<KeyPress-minus>", lambda e: self._on_zoom_key(-1))
        self.root.bind_all("<KP_Add>", lambda e: self._on_zoom_key(1))
        self.root.bind_all("<KP_Subtract>", lambda e: self._on_zoom_key(-1))
        self.root.bind_all("<KeyPress>", self._on_modifier_poll, add="+")
        self.root.bind_all("<KeyRelease>", self._on_modifier_poll, add="+")
        self.root.bind_all("<F11>", self._on_fullscreen_key)
        self.root.bind_all("<Escape>", self._on_escape_key)
        self.root.bind("<FocusOut>", self._on_root_focus_out)

    def _on_step_key(self, delta):
        if not self._preview_tab_selected() or self._input_widget_focused() or self._exporting:
            return None
        self.step_frame(delta)
        return "break"

    def _on_skip_key(self, sign):
        if not self._preview_tab_selected() or self._input_widget_focused() or self._exporting:
            return None
        self.skip_seconds(sign)
        return "break"

    def _on_jump_key(self, frame):
        if not self._preview_tab_selected() or self._input_widget_focused() or self._exporting:
            return None
        self.jump_frame(frame)
        return "break"

    def _on_view_hotkey(self, view):
        if not self._preview_tab_selected() or self._input_widget_focused() or self._exporting:
            return None
        if self.view_var.get() != view:
            self.view_var.set(view)
            self.on_view_change()
        return "break"

    def _on_zoom_key(self, direction):
        if not self._preview_tab_selected() or self._input_widget_focused() or self._exporting:
            return None
        self._step_zoom(direction)
        return "break"

    def _on_zoom_reset_key(self, event=None):
        if not self._preview_tab_selected() or self._input_widget_focused() or self._exporting:
            return None
        self.reset_preview_zoom()
        return "break"

    def _on_fullscreen_key(self, event=None):
        if not self._preview_tab_selected() or self._input_widget_focused():
            return None
        self.toggle_fullscreen()
        return "break"

    def _on_escape_key(self, event=None):
        if self._fullscreen:
            self._exit_fullscreen()
            return "break"
        return None

    def _set_hold_original(self, down):
        down = bool(
            down
            and self._preview_tab_selected()
            and self.video
            and not self._exporting
            and not self._input_widget_focused()
        )
        if down == self._hold_original:
            return False
        self._hold_original = down
        return True

    def _refresh_after_hold_change(self):
        if self.playing:
            self._present_play_frame(self._frame)
        elif self.video and not self._exporting:
            self.display_view(quality="fast" if self._hold_original else "full")

    def _on_modifier_poll(self, event=None):
        if event is None:
            return None
        key = getattr(event, "keysym", "")
        etype = getattr(event, "type", "")
        etype_s = str(etype)
        etype_name = getattr(etype, "name", "")
        is_press = etype in (2, "2") or etype_s in ("2", "KeyPress") or etype_name == "KeyPress"
        is_release = etype in (3, "3") or etype_s in ("3", "KeyRelease") or etype_name == "KeyRelease"
        if key in ("Alt_L", "Alt_R"):
            if self._set_hold_original(is_press and not is_release):
                self._refresh_after_hold_change()
            return None
        if self._hold_original and (is_release or not _alt_is_down()):
            if self._set_hold_original(False):
                self._refresh_after_hold_change()
        return None

    def _on_root_focus_out(self, event=None):
        if event is not None and event.widget not in (
            self.root, self._detached_preview_window,
        ):
            return
        if self._hold_original and not _alt_is_down():
            self._hold_original = False
            if self.video and not self._exporting and not self.playing:
                self.display_view(quality="full")

    def toggle_play(self):
        self._focus_preview_host()
        self._set_hold_original(False)
        if self.playing:
            self.pause()
        else:
            self.play()

    def on_space(self, event=None):
        if not self._preview_tab_selected():
            return None
        if self._input_widget_focused(extra=_SPACE_PASSTHROUGH):
            return None
        self.toggle_play()
        return "break"

    def _set_play_btn(self, playing):
        try:
            if playing and self._buffering:
                self.play_btn.config(text="停止等待", icon="stop")
            elif playing:
                self.play_btn.config(text="暂停", icon="pause")
            else:
                self.play_btn.config(text="播放", icon="play")
        except Exception:
            pass

    def toggle_mute(self):
        self._focus_preview_host()
        self._audio.set_muted(not self._audio.muted)
        try:
            muted = self._audio.muted
            self.mute_btn.config(
                text="静音" if muted else "声音",
                icon="volume-off" if muted else "volume",
            )
        except Exception:
            pass
        if self.playing and not self._buffering and not self._audio.muted:
            self._audio.play(self._frame, self.fps)

    def play(self):
        if not self.video:
            messagebox.showwarning("提示", "请先导入视频或图片")
            return
        if self._is_image or self.nframes <= 1:
            self.display_view(quality="full")
            return
        last = self._last_frame_index()
        if self._frame >= last:
            self._frame = 0
            self.timeline.set(0)
            self._sync_transport_labels()
        self._set_hold_original(False)
        self._cancel_after("_preview_cache_resume_after")
        self._preview_cache_frozen = False
        view = self.view_var.get()
        preview_size = self._playback_preview_size()
        self.playing = True
        self._active_preview_size = preview_size
        self._cancel_after("_play_after")
        self._cancel_after("_preview_decode_after")
        self._cancel_after("_scrub_after")
        if view in ("DLSS", "对比"):
            if not self._start_strict_preview_buffering():
                self.playing = False
                self._set_play_btn(False)
                return
            self._present_play_frame(self._frame)
        else:
            self._resume_play_clock(self._frame)
        self._play_tick()

    def _enter_preview_buffering(self, frame):
        frame = _clamp_frame(frame, self._last_frame_index())
        self._frame = frame
        if not self._buffering:
            self._buffer_started_at = time.perf_counter()
        self._buffering = True
        self._audio.pause()
        self._set_play_btn(True)

    def _resume_play_clock(self, frame):
        self._buffering = False
        self._buffer_started_at = None
        self._play_anchor_time = time.perf_counter()
        self._play_anchor_frame = int(frame)
        self._set_play_btn(True)
        self._audio.play(frame, self.fps)

    def _start_strict_preview_buffering(self):
        self._pre_rendering = False
        self._active_preview_size = self._playback_preview_size()
        source = self._source_cache_get(self._frame)
        if source is None:
            source = self._read_frame(self._frame)
        if source is None:
            return False
        self._source_cache_store(self._frame, source)
        self._preview_decode_next = self._frame + 1
        if self._start_prefetch() is False:
            self._enter_preview_buffering(self._frame)
            self._schedule_preview_cache_resume(PREVIEW_WORKER_POLL_MS)
            return True
        self._queue_preview_frame(self._frame, source)
        self._enter_preview_buffering(self._frame)
        self._schedule_preview_decode(0)
        return True

    def _preview_session_active(self):
        return bool(
            (self.playing or self._pre_rendering)
            and not getattr(self, "_preview_cache_frozen", False)
            and self.video and not self._exporting and not self._is_image
            and self.view_var.get() in ("DLSS", "对比")
        )

    def _schedule_preview_cache_resume(self, delay=PREVIEW_INTERACTION_IDLE_MS):
        self._cancel_after("_preview_cache_resume_after")
        root = getattr(self, "root", None)
        if root is None or self._exporting:
            self._preview_cache_frozen = False
            return
        self._preview_cache_resume_after = root.after(
            max(int(delay), 0), self._resume_preview_cache,
        )

    def _freeze_preview_cache(self, resume_ms=PREVIEW_INTERACTION_IDLE_MS):
        """Stop background decode/render work without blocking the Tk event loop."""
        held_without_timer = (
            getattr(self, "_preview_cache_frozen", False)
            and getattr(self, "_preview_cache_resume_after", None) is None
        )
        self._cancel_after("_preview_cache_resume_after")
        self._cancel_after("_preview_decode_after")
        self._cancel_after("_scrub_after")
        self._pre_rendering = False
        self._preview_cache_frozen = True
        stop = getattr(self, "_prefetch_stop", None)
        if stop is not None:
            stop.set()
        # Invalidating the generation prevents a retiring worker from writing old
        # frames into a new source's cache after the UI has already switched.
        self._preview_frame_queue = None
        lock = getattr(self, "_cache_lock", None)
        if lock is None:
            self._prefetch_gen = getattr(self, "_prefetch_gen", 0) + 1
        else:
            with lock:
                self._prefetch_gen = getattr(self, "_prefetch_gen", 0) + 1
                self._queued_preview_frames.clear()
        if resume_ms is None or held_without_timer:
            return
        if getattr(self, "root", None) is not None:
            self._schedule_preview_cache_resume(resume_ms)

    def _resume_preview_cache(self):
        self._preview_cache_resume_after = None
        thread = getattr(self, "_play_dlss_thread", None)
        if thread is not None and thread.is_alive():
            self._preview_cache_resume_after = self.root.after(
                PREVIEW_WORKER_POLL_MS, self._resume_preview_cache,
            )
            return
        self._play_dlss_thread = None
        self._play_dlss_busy = False
        self._preview_cache_frozen = False
        if self._exporting:
            return
        if not self.video:
            self._close_live()
            return
        if self.playing:
            if (
                self.view_var.get() in ("DLSS", "对比")
                and self._preview_frame_queue is None
            ):
                self._start_strict_preview_buffering()
            return
        if self.view_var.get() in ("DLSS", "对比") and not self._hold_original:
            self._schedule_full_preview()

    def _stop_paused_prerender(self):
        self._pre_rendering = False
        self._cancel_after("_preview_decode_after")
        self._prefetch_stop.set()
        self._preview_frame_queue = None
        with self._cache_lock:
            self._queued_preview_frames.clear()

    def _start_paused_prerender(self, target_size=None):
        if (
            getattr(self, "_preview_cache_frozen", False)
            or self.playing or not self.video or self._exporting or self._is_image
        ):
            return False
        if self.view_var.get() not in ("DLSS", "对比") or self._hold_original:
            return False
        self._stop_paused_prerender()
        self._pre_rendering = True
        self._active_preview_size = target_size or self._playback_preview_size()
        source = self._source_cache_get(self._frame)
        if source is None:
            source = self._read_frame(self._frame)
        if source is None:
            self._pre_rendering = False
            return False
        self._source_cache_store(self._frame, source)
        started = (
            self._start_prefetch(preview_size=target_size)
            if target_size is not None else self._start_prefetch()
        )
        if started is False:
            self._pre_rendering = False
            self._schedule_preview_cache_resume(PREVIEW_WORKER_POLL_MS)
            return False
        self._queue_preview_frame(self._frame, source)
        self._schedule_preview_decode(0)
        return True

    def _buffered_frame_count(self, start):
        start = int(start)
        last = self._last_frame_index()
        target_size = self._active_preview_size or self._playback_preview_size()
        sk = self._settings_hash()
        count = 0
        for frame in range(start, min(last, start + self._buffer_target_frames() - 1) + 1):
            if (
                self._source_cache_get(frame) is None
                or self._cached_dlss_sk(frame, sk, target_size) is None
            ):
                break
            count += 1
        return count

    def _buffer_is_ready(self):
        available = self._buffered_frame_count(self._frame)
        remaining = self._last_frame_index() - self._frame + 1
        required = min(self._buffer_target_frames(), max(remaining, 1))
        return available >= required, available, required

    def _schedule_preview_decode(self, delay=1):
        self._cancel_after("_preview_decode_after")
        if self._preview_session_active():
            if self._pre_rendering and not self.playing:
                delay = max(int(delay), PREVIEW_BACKGROUND_TICK_MS)
            self._preview_decode_after = self.root.after(delay, self._preview_decode_tick)

    def _preview_decode_tick(self):
        self._preview_decode_after = None
        if not self._preview_session_active():
            return
        # Presentation must precede every early return in the fill-ahead loop,
        # including queue-full retries. A large cache must not delay this frame.
        self._present_current_cached_preview()
        self._update_preview_timeline_and_status()
        target_end = min(
            self._last_frame_index(),
            self._frame + self._prerender_target_frames() - 1,
        )
        target_size = self._active_preview_size or self._playback_preview_size()
        sk = self._settings_hash()
        waiting_on_worker = False
        for next_frame in range(self._frame, target_end + 1):
            processed_ready = self._cached_dlss_sk(next_frame, sk, target_size) is not None
            source = self._source_cache_get(next_frame)
            if processed_ready and source is not None:
                continue
            with self._cache_lock:
                if not processed_ready and next_frame in self._queued_preview_frames:
                    waiting_on_worker = True
                    continue
            if source is None:
                source = self._read_frame(next_frame)
                if source is None:
                    self._schedule_preview_decode(20)
                    return
                self._source_cache_store(next_frame, source)
                if processed_ready:
                    self._schedule_preview_decode(1)
                    return
            self._queue_preview_frame(next_frame, source)
            self._schedule_preview_decode(1)
            return
        if waiting_on_worker:
            self._schedule_preview_decode(20)
            return
        if self._pre_rendering and not self.playing:
            self._pre_rendering = False
            self._prefetch_stop.set()
            self._preview_frame_queue = None
            # The worker may have completed this frame during the scan above.
            self._present_current_cached_preview()
            self._update_preview_timeline_and_status(force=True)
            return
        self._update_preview_timeline_and_status(force=True)
        self._schedule_preview_decode(20)

    def _present_current_cached_preview(self):
        if (
            getattr(self, "_preview_cache_frozen", False)
            or self.playing or not self.video or self._exporting
            or self._hold_original or self.view_var.get() not in ("DLSS", "对比")
        ):
            return False
        # Full display uses the precise size. A ready playback proxy alone must
        # not cause synchronous full-resolution inference on the UI thread.
        target_size = self._precise_preview_size()
        sk = self._settings_hash()
        if self._cached_dlss_sk(self._frame, sk, target_size) is None:
            return False
        key = (self._frame, target_size, sk, self.view_var.get())
        if (
            getattr(self, "_presented_preview_key", None) == key
            and not getattr(self, "_dlss_pending", False)
        ):
            return False
        self.display_view(quality="full")
        self._presented_preview_key = key
        return True

    def _update_preview_timeline_and_status(self, force=False):
        if not self.video or self.view_var.get() not in ("DLSS", "对比"):
            return
        if getattr(self, "_preview_cache_frozen", False) and not force:
            return
        now = time.perf_counter()
        if not force and now - self._preview_status_at < 0.2:
            return
        self._preview_status_at = now
        size = self._active_preview_size or self._playback_preview_size()
        sk = self._settings_hash()
        with self._cache_lock:
            rendered_frames = {
                key[0] for key, item in self._dlss_frame_cache.items()
                if key[1:] == size and item[0] == sk
            }
            queued_frames = set(self._source_frame_cache) - rendered_frames
            used_bytes = self._dlss_cache_bytes + self._source_cache_bytes
        self.timeline.set_cache_ranges(
            _frame_ranges(rendered_frames), _frame_ranges(queued_frames),
        )
        elapsed = (
            now - self._preview_process_t0
            if self._preview_process_t0 is not None else 0.0
        )
        rate = self._preview_processed_frames / elapsed if elapsed > 0.05 else 0.0
        used_mib = used_bytes / (1024 * 1024)
        target_end = min(
            self._last_frame_index(),
            self._frame + self._prerender_target_frames() - 1,
        )
        target_total = max(target_end - self._frame + 1, 1)
        rendered_ahead = sum(
            1 for frame in rendered_frames if self._frame <= frame <= target_end
        )
        if self._buffering:
            _ready, available, required = self._buffer_is_ready()
            text = f"正在渲染第 {self._frame} 帧 · 启动缓冲 {available}/{required} 帧"
        elif self._pre_rendering and not self.playing:
            text = f"后台预渲染 · 前向缓存 {rendered_ahead}/{target_total} 帧"
        elif not self.playing:
            text = f"预渲染就绪 · 前向缓存 {rendered_ahead}/{target_total} 帧"
        else:
            text = f"严格同步预览 · 已渲染 {len(rendered_frames)} 帧"
        if rate > 0:
            text += f" · {rate:.1f} fps"
            if rate + 0.5 < max(float(self.fps), 1.0):
                text += f"（低于视频 {self.fps:.1f} fps，将间歇等待）"
        text += f" · RAM {used_mib:.0f}/{self._preview_cache_bytes() / (1024 * 1024):.0f} MiB"
        try:
            self.eta_label.config(text=text)
        except Exception:
            pass

    def _play_clock_frame(self):
        if self._buffering:
            return self._frame
        last = self._last_frame_index()
        fps = max(float(self.fps) or 30.0, 1.0)
        if self._audio.has_audio and not self._audio.muted:
            ms = self._audio.position_ms()
            if ms is not None:
                return ms_to_frame(ms, fps, last)
        elapsed = time.perf_counter() - self._play_anchor_time
        return _play_target_frame(self._play_anchor_frame, elapsed, fps, last)

    def _play_should_stop(self, target, last):
        if target < last:
            return False
        elapsed = time.perf_counter() - self._play_anchor_time
        fps = max(float(self.fps) or 30.0, 1.0)
        if elapsed * fps >= max(last - self._play_anchor_frame, 0):
            return True
        if self._audio.has_audio and not self._audio.muted:
            length = self._audio.length_ms()
            ms = self._audio.position_ms()
            if length and ms is not None and ms >= max(length - 40, 0):
                return True
        return False

    def _play_tick(self):
        if not self.playing:
            return
        worker_error = self._preview_worker_error
        if worker_error:
            self._preview_worker_error = None
            self.logln("[预览] DLSS 缓存失败: " + str(worker_error))
        if self._hold_original and not _alt_is_down():
            self._set_hold_original(False)
            try:
                self._present_play_frame(self._frame)
            except Exception as ex:
                self.logln("[播放] " + str(ex))
        last = self._last_frame_index()
        fps = max(float(self.fps) or 30.0, 1.0)
        delay = max(8, min(int(1000 / fps), 33))
        try:
            self._update_preview_timeline_and_status()
            if self._buffering:
                ready, _available, _required = self._buffer_is_ready()
                if ready:
                    self._resume_play_clock(self._frame)
                self._present_play_frame(self._frame)
                self._play_after = self.root.after(delay, self._play_tick)
                return
            target = self._play_clock_frame()
            if self._play_should_stop(target, last):
                self._present_play_frame(last)
                self.pause()
                self.set_status("播放结束")
                return
            if target != self._frame or self._hold_original:
                view = "原图" if self._hold_original else self.view_var.get()
                if view in ("DLSS", "对比"):
                    target_size = self._active_preview_size or self._playback_preview_size()
                    exact = _first_image(
                        self._cached_dlss(target, target_size),
                        self._cached_dlss(target),
                    )
                    if exact is None:
                        self._enter_preview_buffering(target)
                self._present_play_frame(target)
        except Exception as ex:
            self.logln("[播放] " + str(ex))
        self._play_after = self.root.after(delay, self._play_tick)

    def _present_play_frame(self, frame, orig=None):
        if not self.video:
            return
        last = self._last_frame_index()
        frame = _clamp_frame(frame, last)
        self._frame = frame
        if getattr(self, "timeline", None) is not None and self.timeline.get() != frame:
            self.timeline.set(frame)
        self._sync_transport_labels()
        cached_orig = getattr(self, "_play_orig", None)
        if orig is None and cached_orig is not None and cached_orig[0] == frame:
            orig = cached_orig[1]
        if orig is None and self.playing:
            orig = self._source_cache_get(frame)
        if orig is None:
            orig = self._read_frame(frame)
        if orig is None:
            return
        self._play_orig = (frame, orig)
        cw, ch = self._canvas_size()
        view = "原图" if self._hold_original else self.view_var.get()
        if view == "原图" or self._hold_original:
            badge = "原图（按住 Alt）" if self._hold_original and self.view_var.get() != "原图" else None
            self._dlss_pending = False
            self._draw_fit(orig, cw, ch, badge=badge)
            return
        preview_size = self._active_preview_size or self._playback_preview_size(orig)
        self._queue_preview_frame(frame, orig)
        cached = _first_image(
            self._cached_dlss(frame, preview_size),
            self._cached_dlss(frame),
        )
        exact_frame = cached is not None
        settings = self._collect_settings()
        preview = compose_preview_frame(
            orig, cached,
            settings['output_view'], settings['output_mix'],
        )
        if view == "对比":
            self._blit_play_split(orig, preview, cw, ch, pending=not exact_frame)
            return
        img = preview if preview is not None else self._pending_preview_image(orig)
        self._dlss_pending = not exact_frame
        badge = "正在渲染当前帧…" if not exact_frame else None
        self._draw_fit(img, cw, ch, badge=badge)

    @staticmethod
    def _pending_preview_image(orig):
        gray = cv2.cvtColor(orig, cv2.COLOR_BGR2GRAY)
        muted = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        return cv2.addWeighted(muted, 0.28, np.zeros_like(muted), 0.72, 0.0)

    def _blit_play_split(self, orig, dlss, cw, ch, pending=False):
        self._split_orig = orig
        if dlss is None and pending:
            dlss = self._pending_preview_image(orig)
        self._split_dlss = dlss
        self._split_frame = self._frame
        self._split_size = (cw, ch)
        self._dlss_pending = bool(pending or self._split_dlss is None)
        self._blit_split(cw, ch)

    def _start_prefetch(self, preview_size=None):
        if not self._preview_session_active():
            return False
        if self.view_var.get() == "原图":
            return False
        previous = getattr(self, "_play_dlss_thread", None)
        if previous is not None and previous.is_alive():
            return False
        self._play_dlss_thread = None
        self._prefetch_stop.set()
        self._prefetch_gen += 1
        gen = self._prefetch_gen
        stop = threading.Event()
        self._prefetch_stop = stop
        self._preview_worker_error = None
        settings = self._collect_settings()
        preview_size = preview_size or self._playback_preview_size()
        self._active_preview_size = preview_size
        frame_queue = queue.Queue(
            maxsize=max(self._buffer_target_frames() + PREVIEW_QUEUE_SIZE, 4)
        )
        self._preview_frame_queue = frame_queue
        with self._cache_lock:
            self._queued_preview_frames.clear()
        thread = threading.Thread(
            target=self._prefetch_job,
            args=(settings, preview_size, frame_queue, stop, gen),
            daemon=True, name="dlss-prefetch",
        )
        self._play_dlss_thread = thread
        self._play_dlss_busy = True
        thread.start()
        return True

    def _queue_preview_frame(self, frame, bgr):
        if (
            not self._preview_session_active() or bgr is None
        ):
            return False
        frame_queue = self._preview_frame_queue
        if frame_queue is None or self._prefetch_stop.is_set():
            return False
        frame = int(frame)
        with self._cache_lock:
            if frame in self._queued_preview_frames:
                return True
            self._queued_preview_frames.add(frame)
        try:
            frame_queue.put_nowait((frame, bgr))
            return True
        except queue.Full:
            with self._cache_lock:
                self._queued_preview_frames.discard(frame)
            return False

    def _prefetch_job(self, settings, preview_size, frame_queue, stop, gen):
        sk = self._hash_settings_dict(settings)
        pending = deque()
        live = None
        last_submitted = -2

        def store_output(frame, output):
            if output is None:
                raise RuntimeError(f"DLSS 预览第 {frame} 帧失败")
            if gen != self._prefetch_gen:
                return
            bgr = cv2.cvtColor(output[..., :3], cv2.COLOR_RGB2BGR)
            with self._cache_lock:
                if gen != self._prefetch_gen:
                    return
                self._cache_store(frame, sk, bgr)
                self._queued_preview_frames.discard(frame)

        def receive_one(store=True):
            frame = pending.popleft()
            with self._live_lock:
                output = live.dequeue()
            if store:
                store_output(frame, output)

        try:
            while (
                not stop.is_set()
                and gen == self._prefetch_gen
            ):
                try:
                    frame, bgr = frame_queue.get(timeout=0.02)
                except queue.Empty:
                    if pending:
                        receive_one()
                    continue
                if stop.is_set() or gen != self._prefetch_gen:
                    break
                if self._cached_dlss_sk(frame, sk, preview_size) is not None:
                    with self._cache_lock:
                        self._queued_preview_frames.discard(frame)
                    continue
                source_h, source_w = bgr.shape[:2]
                target_w, target_h = map(int, preview_size)
                sr_scale = self._super_resolution_scale(settings)
                sr_target = super_resolution_target_size(
                    source_w, source_h, sr_scale,
                )
                use_super_resolution = (
                    sr_scale > 1 and (target_w, target_h) == sr_target
                )
                if use_super_resolution:
                    rgba_source = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
                    rgba = self._upscale_rgba(rgba_source, sr_scale, is_hdr=False)
                    target_h, target_w = rgba.shape[:2]
                    if gen == self._prefetch_gen:
                        enhanced_source = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
                        self._last_super_resolution_preview = (frame, enhanced_source)
                elif (target_w, target_h) != (source_w, source_h):
                    bgr = cv2.resize(
                        bgr, (target_w, target_h), interpolation=cv2.INTER_AREA,
                    )
                    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
                else:
                    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
                if live is None:
                    with self._live_lock:
                        live = self._ensure_live(target_w, target_h, settings)
                        if live is None:
                            raise RuntimeError(getattr(self, "_live_error", "DLSS 主机不可用"))
                reset = frame != last_submitted + 1
                if live.supports_async:
                    while len(pending) >= max(int(live.max_in_flight), 1):
                        receive_one()
                    with self._live_lock:
                        accepted = live.enqueue(rgba, reset=reset)
                    if not accepted:
                        raise RuntimeError(f"DLSS 异步提交第 {frame} 帧失败")
                    pending.append(frame)
                else:
                    with self._live_lock:
                        output = live.process(rgba, reset=reset)
                    store_output(frame, output)
                last_submitted = frame
                if gen == self._prefetch_gen:
                    self._last_dlss_frame = frame
        except Exception as ex:
            if gen == self._prefetch_gen:
                self._preview_worker_error = str(ex)
        finally:
            try:
                keep_results = gen == self._prefetch_gen
                while pending:
                    receive_one(store=keep_results)
            except Exception as ex:
                if gen == self._prefetch_gen:
                    self._preview_worker_error = str(ex)
            if gen == self._prefetch_gen:
                self._play_dlss_busy = False

    def _wait_play_dlss(self, timeout=5.0):
        self._cancel_after("_preview_decode_after")
        self._cancel_after("_preview_cache_resume_after")
        self._cancel_after("_scrub_after")
        self._prefetch_stop.set()
        self._pre_rendering = False
        self._preview_frame_queue = None
        with self._cache_lock:
            self._queued_preview_frames.clear()
        thread = getattr(self, "_play_dlss_thread", None)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._play_dlss_busy = False
        self._play_dlss_thread = None

    def pause(self):
        was_playing = self.playing
        self.playing = False
        self._buffering = False
        self._buffer_started_at = None
        self._pre_rendering = False
        self._active_preview_size = None
        self._set_play_btn(False)
        self._cancel_after("_play_after")
        self._cancel_after("_preview_decode_after")
        self._audio.pause()
        if was_playing and self.video and not self._exporting and not self._is_image:
            self._prefetch_stop.set()
            self._preview_frame_queue = None
            with self._cache_lock:
                self._queued_preview_frames.clear()
            if self.view_var.get() in ("DLSS", "对比") and not self._hold_original:
                self._schedule_full_preview()

    # ---------- import ----------
    def import_video(self):
        self.import_media()

    def import_media(self):
        if self._diagnosing:
            messagebox.showinfo("诊断中", "请等待诊断报告生成后再导入素材。")
            return
        self._freeze_preview_cache(resume_ms=None)
        path = filedialog.askopenfilename(filetypes=VIDEO_FILETYPES)
        if path:
            self._load_media(path)
        else:
            self._schedule_preview_cache_resume()

    # ---------- application updates ----------
    @staticmethod
    def _open_release_page(url=updater.RELEASES_URL):
        try:
            webbrowser.open(url or updater.RELEASES_URL)
            return True
        except Exception:
            return False

    @staticmethod
    def _open_download_directory(path):
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if os.name == "nt":
                os.startfile(directory)
            else:
                webbrowser.open("file://" + directory)
            return True
        except Exception:
            return False

    def check_for_updates(self, manual=True):
        """Check GitHub without blocking Tk; startup failures stay unobtrusive."""
        if self._update_checking or self._update_downloading:
            if manual:
                messagebox.showinfo("检查更新", "更新检查或下载已经在进行中。")
            return
        self._update_checking = True
        self._update_thread = None
        self._update_action_labels()
        if manual:
            self.logln(f"[更新] 正在检查 GitHub Releases；当前版本 {APP_VERSION}")
        result_queue = queue.Queue(maxsize=1)

        def worker():
            try:
                result_queue.put((updater.fetch_latest_release(), None))
            except BaseException as exception:
                result_queue.put((None, str(exception) or repr(exception)))

        def poll_result():
            try:
                release, error = result_queue.get_nowait()
            except queue.Empty:
                self.root.after(100, poll_result)
                return
            self._update_checking = False
            self._update_thread = None
            self._update_action_labels()
            self._handle_update_check_result(release, error, manual)

        self._update_thread = threading.Thread(
            target=worker, name="dlss5-update-check", daemon=True,
        )
        self._update_thread.start()
        self.root.after(100, poll_result)

    def _handle_update_check_result(self, release, error, manual):
        """Keep automatic startup checks silent unless an update exists."""
        if error:
            if manual:
                self.logln("[更新] 检查失败: " + error)
                messagebox.showwarning("检查更新失败", error)
            return
        comparison = updater.compare_versions(release.tag, APP_VERSION)
        if comparison is None:
            if manual:
                messagebox.showwarning(
                    "检查更新失败", f"无法比较版本号：{APP_VERSION} / {release.tag}"
                )
            return
        if comparison <= 0:
            if manual:
                self.logln(f"[更新] GitHub 最新正式版本为 {release.tag}")
                messagebox.showinfo(
                    "已是最新版本",
                    f"当前版本：{APP_VERSION}\nGitHub 最新正式版本：{release.tag}",
                )
            return
        self._prompt_for_update(release)

    def _prompt_for_update(self, release):
        asset = updater.select_portable_asset(release)
        notes = release.body.strip()
        if len(notes) > 900:
            notes = notes[:897].rstrip() + "…"
        notes_text = f"\n\n更新说明：\n{notes}" if notes else ""
        if asset is None:
            self.logln(f"[更新] 发现 {release.tag}，但未找到完整 win64 便携包")
            if messagebox.askyesno(
                "发现新版本",
                f"当前版本：{APP_VERSION}\n最新版本：{release.tag}{notes_text}\n\n"
                "此 Release 没有可识别的完整 win64 便携包。是否打开发布页？",
            ):
                self._open_release_page(release.page_url)
            return
        size_text = updater.format_size(asset.size) if asset.size else "大小未知"
        self.logln(f"[更新] 发现新版本 {release.tag}：{asset.name}（{size_text}）")
        if messagebox.askyesno(
            "发现新版本",
            f"当前版本：{APP_VERSION}\n最新版本：{release.tag}{notes_text}\n\n"
            f"是否将 {asset.name}（{size_text}）自动下载到“下载”文件夹？\n"
            "下载完成后请关闭程序、完整解压，再运行新版本。",
        ):
            self._start_update_download(release, asset)

    def _start_update_download(self, release, asset):
        try:
            directory = updater.default_download_directory()
            os.makedirs(directory, exist_ok=True)
            destination = updater.unique_download_path(directory, asset.name)
        except OSError as ex:
            messagebox.showerror("无法下载更新", f"无法使用下载文件夹：\n{ex}")
            return
        self._update_downloading = True
        self._update_progress_percent = 0 if asset.size else None
        self._update_cancel_event.clear()
        self._update_action_labels()
        self.logln(f"[更新] 开始下载到: {destination}")
        events = queue.Queue()

        def progress(downloaded, total):
            events.put(("progress", downloaded, total))

        def worker():
            try:
                path = updater.download_asset(
                    asset,
                    destination,
                    progress=progress,
                    cancelled=self._update_cancel_event.is_set,
                )
                events.put(("complete", path, None))
            except BaseException as exception:
                events.put(("complete", None, str(exception) or repr(exception)))

        def poll_events():
            completed = None
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                if event[0] == "progress":
                    _kind, downloaded, total = event
                    if total:
                        self._update_progress_percent = min(
                            100, int(downloaded * 100 / total)
                        )
                    self._update_action_labels()
                else:
                    completed = event
            if completed is None:
                self.root.after(100, poll_events)
                return
            _kind, path, error = completed
            self._update_downloading = False
            self._update_progress_percent = None
            self._update_thread = None
            self._update_action_labels()
            if error:
                if self._update_cancel_event.is_set():
                    self.logln("[更新] 下载已取消，临时文件已清理")
                else:
                    self.logln("[更新] 下载失败: " + error)
                    messagebox.showerror("更新下载失败", error)
                return
            self.logln(f"[更新] {release.tag} 已下载并校验完成: {path}")
            if messagebox.askyesno(
                "更新下载完成",
                f"新版本已下载并校验完成：\n{path}\n\n"
                "本程序是免安装版，不会在运行中覆盖自身。"
                "请关闭程序后完整解压。\n\n是否打开所在文件夹？",
            ):
                self._open_download_directory(path)

        self._update_thread = threading.Thread(
            target=worker, name="dlss5-update-download", daemon=True,
        )
        self._update_thread.start()
        self.root.after(100, poll_events)

    def export_diagnostics(self):
        """Create a shareable support log without requiring imported media."""
        if self._diagnosing:
            return
        if self._exporting or self._queue_running or self._switching_backend:
            messagebox.showinfo("忙", "请等待当前导出、队列或主机切换完成后再诊断。")
            return
        initial_dir = ""
        if self.video:
            initial_dir = os.path.dirname(os.path.abspath(self.video))
        if not initial_dir or not os.path.isdir(initial_dir):
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            initial_dir = desktop if os.path.isdir(desktop) else os.getcwd()
        output_path = filedialog.asksaveasfilename(
            title="保存 DLSS5Tool 诊断日志",
            initialdir=initial_dir,
            initialfile=diagnostics.suggested_report_name(),
            defaultextension=".log",
            filetypes=[("诊断日志", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not output_path:
            return
        try:
            ui_log = self.log.get("1.0", "end")
        except Exception:
            ui_log = ""
        active_host = None
        if self._live is not None:
            active_host = {
                "backend": getattr(self._live, "backend", "unknown"),
                "preference": getattr(self._live, "preference", "unknown"),
                "max_in_flight": getattr(self._live, "max_in_flight", 1),
                "supports_async": bool(getattr(self._live, "supports_async", False)),
            }
        media = None
        if self.video:
            media = {
                "name": os.path.basename(self.video),
                "kind": self._source_kind,
                "width": self._media_w,
                "height": self._media_h,
                "frames": self.nframes,
                "fps": self.fps,
                "color": dict(self._video_color_info or {}),
            }
        context = {
            "settings": self._collect_settings(),
            "export_settings": self._collect_export_settings(),
            "active_host": active_host,
            "media": media,
            "ui_log": ui_log,
        }
        self._diagnosing = True
        self.root.config(cursor="wait")
        self._update_action_labels()
        self._update_host_control_states()
        self._update_queue_action_states()
        self.set_status("正在诊断 GPU、DLL 与 Feature 18 主机…")
        self.logln("[诊断] 开始生成一键诊断报告…")

        def finish(result, error):
            self._diagnosing = False
            self._diagnostic_thread = None
            self.root.config(cursor="")
            self._update_action_labels()
            self._update_host_control_states()
            self._update_queue_action_states()
            if error:
                self.set_status("诊断报告导出失败")
                self.logln("[诊断] 导出失败: " + error)
                messagebox.showerror(
                    "诊断失败",
                    "无法保存诊断报告：\n" + error + "\n\n请换一个可写目录后重试。",
                )
                return
            path = result["path"]
            passed = int(result.get("passed", 0))
            total = int(result.get("total", 0))
            self.set_status(f"诊断完成 · 宿主探针 {passed}/{total} 通过")
            self.logln(f"[诊断] 已导出: {path}；宿主探针 {passed}/{total} 通过")
            messagebox.showinfo(
                "诊断完成",
                f"诊断日志已保存：\n{path}\n\n宿主探针 {passed}/{total} 通过。"
                "无论通过或失败，都可以把这个日志直接发给维护者。",
            )

        result_queue = queue.Queue(maxsize=1)

        def worker():
            result = None
            error = None
            try:
                result = diagnostics.write_diagnostic_report(output_path, context)
            except BaseException as exception:
                error = str(exception) or repr(exception)
            result_queue.put((result, error))

        def poll_result():
            try:
                result, error = result_queue.get_nowait()
            except queue.Empty:
                self.root.after(100, poll_result)
                return
            finish(result, error)

        self._diagnostic_thread = threading.Thread(
            target=worker, name="dlss5-diagnostic", daemon=True,
        )
        self._diagnostic_thread.start()
        self.root.after(100, poll_result)

    def _setup_drag_and_drop(self):
        """Register both the window and video-facing widgets as file drop targets."""
        if DND_FILES is None:
            self.logln("[拖拽] tkinterdnd2 未安装；仍可点击“导入”。运行 setup.bat 可启用拖拽。")
            return
        try:
            for widget in (
                self.root, self.import_btn, self.canvas,
                self.queue_tab, self.queue_tree, self._inspector,
                self._preview_host,
            ):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
                widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
                widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as ex:
            self.logln("[拖拽] 初始化失败，仍可点击导入: " + str(ex))

    def _on_drop_enter(self, event):
        if not self._exporting and not self._queue_running and not self._diagnosing:
            self._freeze_preview_cache(resume_ms=None)
            self._drop_hover = True
            self.canvas.config(bg=self._ui_color("canvas_drop", CANVAS_DROP_BG))
            if not self.video:
                self._draw_empty()
            self.set_status("松开鼠标以导入；多个媒体文件会加入队列")
        return getattr(event, "action", None)

    def _on_drop_leave(self, event):
        self._drop_hover = False
        self.canvas.config(bg=self._ui_color("canvas", CANVAS_BG))
        if not self.video:
            self._draw_empty()
        if not self._exporting and not self._queue_running and not self._diagnosing:
            self.set_status("就绪")
            self._schedule_preview_cache_resume()
        return getattr(event, "action", None)

    def _on_drop(self, event):
        self._drop_hover = False
        self.canvas.config(bg=self._ui_color("canvas", CANVAS_BG))
        if self._exporting or self._queue_running or self._diagnosing:
            message = (
                "正在生成诊断报告，请完成后再导入。"
                if self._diagnosing else
                "正在处理队列，请暂停或结束后再导入。"
            )
            messagebox.showinfo("忙", message)
            return getattr(event, "action", None)
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except Exception:
            paths = [str(getattr(event, "data", "")).strip("{} \"")]
        queue_target = getattr(event, "widget", None) in {self.queue_tab, self.queue_tree}
        contains_folder = any(os.path.isdir(path) for path in paths)
        if queue_target or len(paths) != 1 or contains_folder:
            expanded = []
            for path in paths:
                if os.path.isdir(path):
                    for current, _dirs, files in os.walk(path):
                        expanded.extend(
                            os.path.join(current, name)
                            for name in sorted(files)
                            if _is_video_path(name) or _is_image_path(name)
                        )
                elif path:
                    expanded.append(path)
            self._add_paths_to_queue(expanded)
        elif paths[0]:
            self._load_media(paths[0])
        return getattr(event, "action", None)

    def _begin_source_load(self):
        self.pause()
        self._freeze_preview_cache(resume_ms=None)
        self._audio.close()
        self._cache_clear()
        self._last_dlss_frame = -1
        self._play_orig = None
        self._split_frame = -1
        self._split_orig = None
        self._split_dlss = None
        self._preview_zoom = 1.0
        self._preview_pan_x = 0.5
        self._preview_pan_y = 0.5
        self._viewport_crop_norm = (0.0, 0.0, 1.0, 1.0)
        self._viewport_scale = 1.0
        self._viewport_source_size = (1, 1)
        self._navigator_geom = None
        self._navigator_source = None
        self._navigator_thumb = None
        self._navigator_thumb_size = None
        self._last_viewport_image = None
        self._cancel_after("_scrub_after")
        if getattr(self, "_cap", None):
            self._cap.release()
        self._cap = None
        self._cap_next = None
        self._image_bgr = None
        self._source_kind = None
        self._video_color_info = None
        self._media_w = 0
        self._media_h = 0
        self._active_preview_size = None
        self._preview_decode_next = None
        self._buffering = False
        self._buffer_started_at = None
        self.video = None
        self._update_zoom_controls()
        self.nframes = 0
        self.fps = 30.0
        self._frame = 0
        try:
            self.timeline.set_cache_ranges([], [])
        except Exception:
            pass

    def clear_media(self):
        if self._exporting or self._queue_running or self._diagnosing:
            message = (
                "正在生成诊断报告，请完成后再清空。"
                if self._diagnosing else
                "正在处理队列，请暂停或结束后再清空。"
            )
            messagebox.showinfo("忙", message)
            return
        if not self.video and self._live is None:
            return
        self._begin_source_load()
        thread = getattr(self, "_play_dlss_thread", None)
        if thread is None or not thread.is_alive():
            self._play_dlss_thread = None
            self._close_live()
        else:
            self._schedule_preview_cache_resume(PREVIEW_WORKER_POLL_MS)
        self._photo = None
        self._pilimg = None
        self._hold_original = False
        try:
            self.timeline.set_range(0, 0)
            self.timeline.set(0)
            self._sync_transport_labels()
            self.pbar["value"] = 0
            self.eta_label.config(text="")
        except Exception:
            pass
        self._sync_window_titles()
        self._update_action_labels()
        self._update_export_control_states()
        self._draw_empty()
        self.set_status("就绪")
        self.logln("已清空导入，解码/音轨/DLSS 主机已释放")

    def _load_media(self, path):
        if self._exporting or self._queue_running or self._diagnosing:
            message = (
                "正在生成诊断报告，请完成后再导入。"
                if self._diagnosing else
                "正在处理队列，请暂停或结束后再导入。"
            )
            messagebox.showinfo("忙", message)
            return False
        path = os.path.abspath(os.path.normpath(path))
        if not os.path.isfile(path):
            messagebox.showerror("导入失败", "找不到拖入的文件：\n" + path)
            self.set_status("导入失败：文件不存在")
            return False
        if _is_image_path(path) or _is_video_path(path):
            # Freeze the current source before potentially slow media probing so
            # queued cache ticks cannot run ahead of the import/drop interaction.
            self._freeze_preview_cache(resume_ms=None)
            loaded = self._load_image(path) if _is_image_path(path) else self._load_video(path)
            if not loaded:
                self._schedule_preview_cache_resume()
            return loaded
        messagebox.showerror(
            "不支持的格式",
            "请选择视频（MP4/AVI/MOV/MKV/M4V/WebM）或图片（PNG/JPG/WEBP/BMP/TIFF）。",
        )
        self.set_status("导入失败：不支持的格式")
        return False

    def _load_image(self, path):
        img = _read_image_bgr(path)
        if img is None or img.size == 0:
            messagebox.showerror("导入失败", "无法读取该图片，请检查文件是否损坏。")
            self.set_status("导入失败：无法读取图片")
            return False
        h, w = img.shape[:2]
        if w <= 0 or h <= 0:
            messagebox.showerror("导入失败", "无法读取图片尺寸。")
            self.set_status("导入失败：无法读取图片")
            return False
        self._begin_source_load()
        self.video = path
        self._image_bgr = img
        self._source_kind = "image"
        self._media_w, self._media_h = w, h
        self.nframes, self.fps = 1, 1.0
        self._frame = 0
        self._sync_window_titles()
        self.timeline.set_range(0, 0)
        self.timeline.set(0)
        self._sync_transport_labels()
        self._update_action_labels()
        self._update_export_control_states()
        try:
            self.display_view(quality="fast")
        except Exception as ex:
            self.logln(f"[preview] {ex}")
        self.logln(f"已导入图片: {path}  ({w}×{h})")
        self.set_status(f"图片 · {w}×{h}")
        self._schedule_preview_cache_resume()
        return True

    def _load_video(self, path):
        """Validate and load a video from either the file dialog or drag-and-drop."""
        if self._exporting or self._queue_running:
            messagebox.showinfo("忙", "正在处理队列，请暂停或结束后再导入。")
            return False
        path = os.path.abspath(os.path.normpath(path))
        if not os.path.isfile(path):
            messagebox.showerror("导入失败", "找不到拖入的文件：\n" + path)
            self.set_status("导入失败：文件不存在")
            return False
        if not _is_video_path(path):
            messagebox.showerror(
                "不支持的格式",
                "请选择 MP4、AVI、MOV、MKV、M4V 或 WebM 视频文件。",
            )
            self.set_status("导入失败：不支持的格式")
            return False

        new_cap = cv2.VideoCapture(path)
        if not new_cap.isOpened():
            new_cap.release()
            messagebox.showerror("导入失败", "无法打开该视频，请检查文件是否损坏或编码是否受支持。")
            self.set_status("导入失败：无法打开视频")
            return False
        n = int(new_cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        fps = new_cap.get(cv2.CAP_PROP_FPS) or 30.0
        w = int(new_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(new_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if w <= 0 or h <= 0:
            new_cap.release()
            messagebox.showerror("导入失败", "无法读取视频尺寸，请检查视频编码。")
            self.set_status("导入失败：无法读取视频")
            return False

        try:
            color_info = probe_video_stream(find_ffmpeg(), path)
        except Exception as ex:
            color_info = {"is_hdr": False, "profile": "srgb", "label": "SDR / sRGB"}
            self.logln("[色彩检测] 无法读取视频色彩元数据，按 SDR 处理：" + str(ex))

        # Keep a same-size worker for temporal continuity.  A resolution change is
        # handled by replacing only the isolated NGX process in _ensure_live().
        self._begin_source_load()
        self.video = path
        self._cap = new_cap
        self._cap_next = 0
        self._source_kind = "video"
        self._video_color_info = color_info
        self._media_w, self._media_h = w, h
        self.nframes, self.fps = n, fps
        self._update_preview_memory_hint()
        last = max(n - 1, 0)
        self._frame = 0
        self._sync_window_titles()
        self.timeline.set_range(0, last)
        self.timeline.set(0)
        self._sync_transport_labels()
        self._update_action_labels()
        self._update_export_control_states()
        try:
            self.display_view(quality="fast")
        except Exception as ex:
            self.logln(f"[preview] {ex}")
        self.logln(
            f"已导入: {self.video}  ({n} 帧)；色彩 {color_info.get('label', '未知')} "
            f"[{color_info.get('pixel_format', 'unknown')}, "
            f"{color_info.get('color_primaries', 'unknown')}/"
            f"{color_info.get('color_transfer', 'unknown')}]"
        )
        duration = n / max(float(fps) or 30.0, 1.0)
        self._audio.prepare(path, duration, callback=self._audio_ready_cb)
        self._schedule_preview_cache_resume()
        if not self._hinted_keys:
            self.set_status("滚轮缩放 · 拖动平移 · 0 适应窗口 · ← → 逐帧 · F11 全屏")
            self._hinted_keys = True
        else:
            self.set_status(f"{n} 帧 · {fps:.0f} fps · {w}×{h}")
        return True

    def _audio_ready_cb(self, ok, message, generation):
        try:
            self.root.after(
                0, lambda o=ok, m=message, g=generation: self._on_audio_ready(o, m, g)
            )
        except Exception:
            pass

    def _on_audio_ready(self, ok, message, generation):
        if generation != self._audio.current_generation():
            return
        if not ok:
            self.logln("[音频] 预览音轨准备失败: " + (message or "未知错误"))
            return
        if message != "ok":
            return
        self.logln("[音频] 预览播放将使用原视频音轨")
        if self.playing and not self._buffering and not self._audio.muted:
            self._audio.play(self._frame, self.fps)

    @staticmethod
    def _video_info(path):
        cap = cv2.VideoCapture(path)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        return n, fps, w, h

    @staticmethod
    def _unique_output_path(source_path, ext=".mp4"):
        """Choose a new output name without overwriting an earlier export."""
        if not ext:
            ext = ".mp4"
        if not ext.startswith("."):
            ext = "." + ext
        stem = os.path.splitext(source_path)[0] + "_dlss"
        candidate = stem + ext
        index = 2
        while os.path.exists(candidate):
            candidate = f"{stem}_{index}{ext}"
            index += 1
        return candidate

    def _begin_export_ui(self, status_text):
        self.pause()
        self._cancel_after("_preview_cache_resume_after")
        self._preview_cache_frozen = False
        self._wait_play_dlss()
        if self._fullscreen:
            self._exit_fullscreen()
        self._export_cancel_event.clear()
        self._exporting = True
        self._update_action_labels()
        self._update_host_control_states()
        self._update_queue_action_states()
        self._export_t0 = time.perf_counter()
        self._export_ema_fps = None
        self.set_status(status_text)

    def _end_export_ui(
        self, success, out_path, done_label="完成", done_message=None, cancelled=False,
        completed_items=1, notify=True,
    ):
        self._exporting = False
        self._export_cancel_event.clear()
        self._update_action_labels()
        self._update_host_control_states()
        self._update_queue_action_states()
        if cancelled:
            removed = self._remove_partial_export(out_path)
            self.pbar["value"] = 0
            message = (
                "导出已取消，未完成文件已清理"
                if removed else
                "导出已取消，但未完成文件无法删除，请手动清理"
            )
            self.set_status(message)
            self.logln("[导出] " + message)
        elif success:
            completed_items = max(int(completed_items), 1)
            self.set_progress(completed_items, completed_items, done_label)
            self.logln("已导出: " + out_path)
            if notify:
                messagebox.showinfo("导出", done_message or ("已导出:\n" + out_path))
            try:
                self.pbar["value"] = 0
            except Exception:
                pass
        else:
            self.pbar["value"] = 0
            try:
                self.eta_label.config(text="")
            except Exception:
                pass
            self.set_status("导出失败，请查看日志")
            if notify:
                messagebox.showerror("导出失败", "导出未完成，请查看下方日志。")

    @staticmethod
    def _remove_partial_export(out_path):
        if not out_path or not os.path.exists(out_path):
            return True
        try:
            os.remove(out_path)
            return True
        except OSError:
            return False

    def cancel_export(self):
        active_job = self._queue_job(self._queue_active_job_id)
        if (
            not self._exporting
            or (active_job is not None and active_job.media_kind == "image")
            or (active_job is None and self._is_image)
            or self._export_cancel_event.is_set()
        ):
            return
        self._export_cancel_event.set()
        self._update_action_labels()
        self._update_queue_action_states()
        self.set_status("正在取消导出…")
        self.logln("[导出] 用户请求取消，正在停止导出流水线…")

    def _raise_if_export_cancelled(self):
        if self._export_cancel_event.is_set():
            raise _ExportCancelled()

    def _confirm_super_resolution_export(self, width, height, scale, is_hdr=False, notify=True):
        scale = normalize_scale(scale)
        if scale == 1:
            return True
        status = super_resolution_runtime_status()
        if not status['available']:
            message = (
                "RTX 视频超分组件不完整：" + "、".join(status['missing']) +
                "。\n\n请重新构建或安装包含 RTX Video SDK 运行时的版本。"
            )
            self.logln("[RTX 超分] " + message.replace("\n", " "))
            if notify:
                messagebox.showerror("RTX 超分不可用", message)
            return False
        resource_estimate = estimate_resources(width, height, scale, is_hdr=is_hdr)
        gpu_memory = query_gpu_memory(cache_seconds=0)
        risk = classify_resource_risk(resource_estimate, gpu_memory)
        plan_key = (int(width), int(height), scale, bool(is_hdr))
        self.logln("[RTX 超分资源] " + format_resource_hint(resource_estimate, gpu_memory))
        if (
            not notify or risk not in {'medium', 'high', 'extreme'}
            or plan_key in self._confirmed_super_resolution_plans
        ):
            return True
        warning = (
            f"即将执行 {scale}× RTX 视频超分 → DLSS 5\n\n"
            f"目标尺寸：{resource_estimate['output_width']}×{resource_estimate['output_height']}\n"
            f"单帧：{format_bytes(resource_estimate['single_frame_bytes'])}\n"
            f"已知显存下限：{format_bytes(resource_estimate['known_gpu_bytes'])}\n"
            f"建议空闲显存：{format_bytes(resource_estimate['recommended_gpu_bytes'])}\n"
            f"预计系统内存：{format_bytes(resource_estimate['recommended_ram_bytes'])}"
        )
        if gpu_memory:
            warning += f"\n当前 GPU 空闲：{format_bytes(gpu_memory['free_bytes'])}"
        if resource_estimate['output_width'] > 8192 or resource_estimate['output_height'] > 8192:
            warning += (
                "\n\n目标有一边超过 8192。工具不会限制，但 RTX VSR 或视频编码器可能拒绝该尺寸。"
            )
        warning += "\n\n初始化或分配失败只会终止当前处理会话。是否继续？"
        confirmed = messagebox.askyesno("高资源超分确认", warning, icon="warning")
        if confirmed:
            self._confirmed_super_resolution_plans.add(plan_key)
        return confirmed

    def _export_image(self):
        settings = self._collect_settings()
        self._save_settings_now()
        if self._image_bgr is None:
            messagebox.showwarning("提示", "请先导入图片")
            return
        return self._export_image_source(self.video, settings, notify=True)

    def _process_still_image(self, source_bgr, settings):
        """Process one standalone image without consulting or polluting preview caches."""
        height, width = source_bgr.shape[:2]
        rgba = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2RGBA)
        scale = self._super_resolution_scale(settings)
        if scale > 1:
            rgba = self._upscale_rgba(rgba, scale, is_hdr=False)
            source_bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
            self._last_super_resolution_preview = (0, source_bgr)
            height, width = source_bgr.shape[:2]
        with self._live_lock:
            live = self._ensure_live(
                width, height,
                _large_image_host_settings(width, height, settings),
            )
            if live is None:
                return None
            processed_rgba = live.process(rgba, reset=True)
            self._last_dlss_frame = -1
        if processed_rgba is None:
            return None
        return cv2.cvtColor(processed_rgba[..., :3], cv2.COLOR_RGB2BGR)

    def _export_image_source(self, source_path, settings, out_path=None, notify=True):
        """Export one immutable SDR image request for the preview UI or mixed queue."""
        source_path = os.path.abspath(os.path.normpath(source_path))
        settings = {**self._collect_settings(), **dict(settings or {})}
        orig = _read_image_bgr(source_path)
        if orig is None or orig.size == 0:
            error = "无法读取图片，请检查输入文件。"
            self.logln("导出错误: " + error)
            if notify:
                messagebox.showerror("导出失败", error)
            return {
                "success": False, "cancelled": False, "error": error,
                "output_path": out_path or "", "frames": 0,
            }
        scale = self._super_resolution_scale(settings)
        if not self._confirm_super_resolution_export(
            orig.shape[1], orig.shape[0], scale, is_hdr=False, notify=notify,
        ):
            return {
                "success": False, "cancelled": True, "error": "用户取消超分导出",
                "output_path": out_path or "", "frames": 0,
            }
        ext = os.path.splitext(source_path)[1].lower()
        if ext not in IMAGE_ENCODE_EXTS:
            ext = ".png"
        default_out = os.path.splitext(source_path)[0] + "_dlss" + ext
        out_path = out_path or self._unique_output_path(source_path, ext)
        if not notify and os.path.exists(out_path):
            out_path = self._unique_target_path(out_path)
        if out_path != default_out and notify:
            self.logln("[导出] 目标文件已存在，自动改名为: " + os.path.basename(out_path))
        self._begin_export_ui("正在导出图片…")
        success = False
        error_message = ""
        started_at = self._export_t0
        try:
            processed = self._process_still_image(orig, settings)
            if processed is None:
                raise RuntimeError("DLSS 处理失败")
            view = settings["output_view"]
            mix = float(settings["output_mix"])
            orig = self._preview_composition_source(0, orig, processed)
            composed = compose_output_frame(orig, processed, view, mix)
            out_path = _write_image_bgr(out_path, composed)
            success = True
            elapsed = time.perf_counter() - started_at
            h, w = composed.shape[:2]
            self.logln(f"[导出] 图片 {w}×{h}；用时 {elapsed:.2f} 秒")
            self.set_progress(1, 1, "完成")
        except Exception as ex:
            traceback.print_exc()
            error_message = str(ex)
            self.logln("导出错误: " + error_message)
        self._end_export_ui(
            success, out_path, "完成",
            "已导出图片:\n" + out_path if success else None,
            notify=notify,
        )
        return {
            "success": success, "cancelled": False, "error": error_message,
            "output_path": out_path, "frames": 1 if success else 0,
        }

    # ---------- export ----------
    def _export_sdr_upscaled_video(
        self, source_path, color_info, out_path, total_frames, fps, width, height,
        settings, export_settings, view, mix,
    ):
        """Run fixed VSR -> Feature 18 -> encode with one bounded target frame."""
        scale = normalize_scale(export_settings.get('super_resolution_scale', 1))
        output_width, output_height = super_resolution_target_size(width, height, scale)
        dlss_settings = dict(settings)
        if output_width * output_height > 3840 * 2160:
            dlss_settings['host_in_flight'] = 1
        writer = None
        sr_live = None
        live = None
        completed = False
        written = 0
        super_resolution_seconds = 0.0
        dlss_seconds = 0.0
        memory_before = query_gpu_memory(cache_seconds=0)
        try:
            writer = FFmpegVideoWriter(
                out_path, output_width, output_height, fps, audio_source=source_path,
                nvenc_preset=export_settings['nvenc_preset'],
                rate_control=export_settings['rate_control'],
                quality_profile=export_settings['quality_profile'],
                video_bitrate_mbps=export_settings['video_bitrate_mbps'],
                output_size=None,
            )
            sr_live = ProcessSuperResolution(width, height, scale, is_hdr=False)
            live = ProcessLive(output_width, output_height, dlss_settings)
            memory_after = query_gpu_memory(cache_seconds=0)
            if memory_before and memory_after:
                measured = max(
                    memory_before['free_bytes'] - memory_after['free_bytes'], 0,
                )
                self.logln(f"[RTX 超分资源] 初始化后显存占用增加约 {format_bytes(measured)}")
            self.logln(
                f"[RTX 超分] SDR {width}×{height} → {output_width}×{output_height} → "
                "DLSS 5"
            )
            self.logln(
                f"[DLSS 主机] {live.backend}；目标分辨率队列 {live.max_in_flight} 帧"
            )
            decode_buffer = 1
            for index, frame in self._iter_frames(
                decode_buffer,
                tone_map_hdr=bool((color_info or {}).get('is_hdr')),
                source_path=source_path, color_info=color_info,
            ):
                self._raise_if_export_cancelled()
                rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
                started = time.perf_counter()
                upscaled_rgba = sr_live.process(rgba)
                super_resolution_seconds += time.perf_counter() - started
                upscaled_bgr = cv2.cvtColor(upscaled_rgba, cv2.COLOR_RGBA2BGR)
                started = time.perf_counter()
                processed_rgba = live.process(upscaled_rgba, reset=(index == 0))
                dlss_seconds += time.perf_counter() - started
                if processed_rgba is None:
                    raise RuntimeError(f"DLSS 处理第 {index} 帧失败")
                processed_bgr = cv2.cvtColor(processed_rgba, cv2.COLOR_RGBA2BGR)
                writer.write(compose_output_frame(
                    upscaled_bgr, processed_bgr, view=view, mix=mix,
                ))
                written = index + 1
                if written == 1 or written % 2 == 0 or written >= total_frames:
                    self.set_progress(written, max(total_frames, written), "超分 → DLSS 导出")
                    self.root.update()
            self._raise_if_export_cancelled()
            writer.finish()
            completed = True
            return {
                "frames": written,
                "super_resolution_seconds": super_resolution_seconds,
                "dlss_seconds": dlss_seconds,
                "encoder": writer.encoder_name,
                "audio_mode": writer.audio_mode,
                "host_backend": live.backend,
                "in_flight": live.max_in_flight,
            }
        finally:
            if live is not None:
                live.close()
            if sr_live is not None:
                sr_live.close()
            if writer is not None and not completed:
                writer.abort()

    def _export_hdr_video(
        self, source_path, color_info, out_path, total_frames, fps, width, height,
        settings, export_settings, view, mix,
    ):
        """Run the strict PQ/HLG RGBA16F path in its own disposable host."""
        color_info = dict(color_info or {})
        if not color_info.get("is_hdr"):
            raise RuntimeError("HDR 导出请求与源视频色彩元数据不一致")
        scale = normalize_scale(export_settings.get('super_resolution_scale', 1))
        process_width, process_height = super_resolution_target_size(width, height, scale)
        hdr_settings = {
            **settings,
            "frame_format": "rgba16f",
            "color_profile": color_info["profile"],
            "host_backend": "v2",
            "host_auto_fallback": False,
        }
        if process_width * process_height > 3840 * 2160:
            hdr_settings['host_in_flight'] = 1
        reader = None
        writer = None
        live = None
        sr_live = None
        completed = False
        dlss_seconds = 0.0
        super_resolution_seconds = 0.0
        written = 0
        try:
            ffmpeg = find_ffmpeg()
            reader = FFmpegHDRVideoReader(
                source_path, width, height, color_info, ffmpeg=ffmpeg,
            )
            writer = FFmpegVideoWriter(
                out_path, process_width, process_height, fps, audio_source=source_path,
                nvenc_preset=export_settings['nvenc_preset'], hdr_metadata=color_info,
                rate_control=export_settings['rate_control'],
                quality_profile=export_settings['quality_profile'],
                video_bitrate_mbps=export_settings['video_bitrate_mbps'],
                output_size=None if scale > 1 else export_settings.get('output_size'),
            )
            if scale > 1:
                sr_live = ProcessSuperResolution(width, height, scale, is_hdr=True)
            live = ProcessLive(process_width, process_height, hdr_settings)
            self.logln(
                f"[HDR] {color_info.get('label')} → "
                + (f"10-bit RTX VSR {scale}× → " if scale > 1 else "")
                + "RGBA16F Feature 18 → "
                f"{writer.encoder_name}"
            )
            self.logln(
                f"[DLSS 主机] {live.backend}；HDR GPU 队列 {live.max_in_flight} 帧"
            )
            pending = deque()
            index = 0

            def consume_one():
                nonlocal dlss_seconds, written
                frame_index, original = pending.popleft()
                wait_started = time.perf_counter()
                processed = live.dequeue()
                dlss_seconds += time.perf_counter() - wait_started
                if processed is None:
                    raise RuntimeError(f"HDR DLSS 异步回读第 {frame_index} 帧失败")
                writer.write(compose_hdr_frame(
                    original, processed, view=view, mix=mix,
                    profile=color_info["profile"],
                ))
                written += 1
                if written == 1 or written % 4 == 0 or written >= total_frames:
                    self.set_progress(written, max(total_frames, written), "HDR 严格导出")
                    self.root.update()
                    self._raise_if_export_cancelled()

            while True:
                self._raise_if_export_cancelled()
                frame = reader.read()
                if frame is None:
                    break
                if sr_live is not None:
                    sr_started = time.perf_counter()
                    frame = sr_live.process(frame)
                    super_resolution_seconds += time.perf_counter() - sr_started
                started = time.perf_counter()
                if live.supports_async and sr_live is None:
                    if not live.enqueue(frame, reset=(index == 0)):
                        raise RuntimeError(f"HDR DLSS 异步提交第 {index} 帧失败")
                    dlss_seconds += time.perf_counter() - started
                    pending.append((index, frame))
                    if len(pending) >= live.max_in_flight:
                        consume_one()
                else:
                    processed = live.process(frame, reset=(index == 0))
                    dlss_seconds += time.perf_counter() - started
                    if processed is None:
                        raise RuntimeError(f"HDR DLSS 处理第 {index} 帧失败")
                    writer.write(compose_hdr_frame(
                        frame, processed, view=view, mix=mix,
                        profile=color_info["profile"],
                    ))
                    written += 1
                index += 1
            while pending:
                consume_one()
            self._raise_if_export_cancelled()
            writer.finish()
            completed = True
            return {
                "frames": written,
                "dlss_seconds": dlss_seconds,
                "super_resolution_seconds": super_resolution_seconds,
                "encoder": writer.encoder_name,
                "audio_mode": writer.audio_mode,
                "host_backend": live.backend,
                "in_flight": live.max_in_flight,
            }
        finally:
            if reader is not None:
                reader.close()
            if live is not None:
                live.close()
            if sr_live is not None:
                sr_live.close()
            if writer is not None and not completed:
                writer.abort()

    def export_dlss(self):
        if not self.video:
            messagebox.showwarning("提示", "请先导入视频或图片")
            return
        if self._queue_running or self._exporting or (self.thread and self.thread.is_alive()):
            messagebox.showinfo("忙", "上一个任务还没结束")
            return
        if self._is_image:
            self._export_image()
            return
        settings = self._collect_settings()
        export_settings = self._collect_export_settings()
        self._save_settings_now()
        return self._export_video_source(
            self.video, settings, export_settings,
            dict(self._video_color_info or {}), notify=True,
        )

    def _export_video_source(
        self, source_path, settings, export_settings, color_info,
        out_path=None, notify=True,
    ):
        """Export one immutable video request for either the preview UI or queue."""
        source_path = os.path.abspath(os.path.normpath(source_path))
        settings = {**self._collect_settings(), **dict(settings or {})}
        export_settings = {
            **self._collect_export_settings(), **dict(export_settings or {}),
        }
        color_info = dict(color_info or {})
        resolved_container = resolve_output_container(
            source_path, export_settings.get("output_container", "mp4")
        )
        export_settings["resolved_container"] = resolved_container
        output_extension = output_container_extension(resolved_container)
        n, fps, w, h = self._video_info(source_path)
        if w <= 0 or h <= 0:
            error = "无法读取视频尺寸，请检查输入文件。"
            self.logln("导出错误: " + error)
            if notify:
                messagebox.showerror("导出失败", error)
            return {
                "success": False, "cancelled": False, "error": error,
                "output_path": out_path or "", "frames": 0,
            }
        super_resolution_scale = normalize_scale(
            export_settings.get('super_resolution_scale', 1)
        )
        settings['super_resolution_scale'] = super_resolution_scale
        if super_resolution_scale > 1:
            output_width, output_height = super_resolution_target_size(
                w, h, super_resolution_scale,
            )
        else:
            output_width, output_height = _resolve_output_size(
                w, h, export_settings['output_resolution'],
                export_settings['custom_output_width'], export_settings['custom_output_height'],
            )
        if output_width <= 0 or output_height <= 0:
            output_width, output_height = w, h
        export_settings['output_size'] = (
            (output_width, output_height)
            if super_resolution_scale == 1 and (output_width, output_height) != (w, h)
            else None
        )
        hdr_active = bool(export_settings['hdr_mode'] and color_info.get('is_hdr'))
        if not self._confirm_super_resolution_export(
            w, h, super_resolution_scale, is_hdr=hdr_active, notify=notify,
        ):
            return {
                "success": False, "cancelled": True, "error": "用户取消超分导出",
                "output_path": out_path or "", "frames": 0,
            }
        if super_resolution_scale > 1:
            self._wait_play_dlss(timeout=3.0)
            self._close_super_resolution()
            self._close_live()
        view = settings['output_view']; mix = float(settings['output_mix'])
        live = None; writer = None
        default_out_path = os.path.splitext(source_path)[0] + "_dlss" + output_extension
        out_path = out_path or self._unique_output_path(source_path, output_extension)
        if not notify and os.path.exists(out_path):
            out_path = self._unique_target_path(out_path)
        if out_path != default_out_path and notify:
            self.logln("[导出] 目标文件已存在，自动改名为: " + os.path.basename(out_path))
        pipeline_name = (
            "RTX超分 + GPU DLSS/NVENC" if super_resolution_scale > 1
            else "CPU 解码 + GPU DLSS/NVENC"
        )
        self._begin_export_ui(f"正在流水线导出（{pipeline_name}）...")
        success = False
        cancelled = False
        started_at = self._export_t0
        dlss_seconds = 0.0
        exported_frames = 0
        if hdr_active or super_resolution_scale > 1:
            export_settings['mode'] = 'single'
        error_message = ""
        try:
            if export_settings['rate_control'] == 'quality':
                encoding_note = QUALITY_PROFILE_NAMES.get(
                    export_settings['quality_profile'], "高质量（推荐）"
                )
                encoding_note = f"按画质 {encoding_note}"
            else:
                encoding_note = f"目标码率 {export_settings['video_bitrate_mbps']:g} Mbps"
                if export_settings['mode'] == 'parallel':
                    encoding_note += "（并行分段近似）"
            self.logln(
                f"[导出] {OUTPUT_CONTAINER_LABELS[resolved_container]} · "
                f"输出 {output_width}×{output_height}；{encoding_note}；"
                f"编码速度 {export_settings['nvenc_preset']}"
            )
            if hdr_active:
                result = self._export_hdr_video(
                    source_path, color_info, out_path, n, fps, w, h,
                    settings, export_settings, view, mix,
                )
                exported_frames = result['frames']
                dlss_seconds = result['dlss_seconds']
                super_resolution_seconds = result.get('super_resolution_seconds', 0.0)
                success = True
                self.logln(f"[导出] 编码器: {result['encoder']}")
                self.logln(
                    f"[DLSS 主机] {result['host_backend']}；"
                    f"HDR 队列 {result['in_flight']} 帧"
                )
                self.logln(f"[导出] 音频: {result['audio_mode']}")
                elapsed = time.perf_counter() - started_at
                throughput = exported_frames / elapsed if elapsed > 0 else 0.0
                self.logln(
                    f"[性能] HDR {exported_frames} 帧 / {elapsed:.1f} 秒 = "
                    f"{throughput:.2f} fps；"
                    + (
                        f"超分 {super_resolution_seconds:.1f} 秒；"
                        if super_resolution_scale > 1 else ""
                    )
                    + f"DLSS {dlss_seconds:.1f} 秒"
                )
            elif super_resolution_scale > 1:
                result = self._export_sdr_upscaled_video(
                    source_path, color_info, out_path, n, fps, w, h,
                    settings, export_settings, view, mix,
                )
                exported_frames = result['frames']
                dlss_seconds = result['dlss_seconds']
                success = True
                self.logln(f"[导出] 编码器: {result['encoder']}")
                self.logln(f"[导出] 音频: {result['audio_mode']}")
                elapsed = time.perf_counter() - started_at
                throughput = exported_frames / elapsed if elapsed > 0 else 0.0
                self.logln(
                    f"[性能] 超分+DLSS {exported_frames} 帧 / {elapsed:.1f} 秒 = "
                    f"{throughput:.2f} fps；超分 {result['super_resolution_seconds']:.1f} 秒；"
                    f"DLSS {dlss_seconds:.1f} 秒"
                )
            elif export_settings['mode'] == 'parallel':
                self.logln(
                    f"[导出] 视觉无损并行模式: {export_settings['workers']} 进程，"
                    f"预热 {export_settings['warmup']} 帧"
                )
                result = export_parallel(
                    source_path, out_path, settings,
                    workers=export_settings['workers'],
                    warmup=export_settings['warmup'],
                    nvenc_preset=export_settings['nvenc_preset'],
                    rate_control=export_settings['rate_control'],
                    quality_profile=export_settings['quality_profile'],
                    video_bitrate_mbps=export_settings['video_bitrate_mbps'],
                    output_size=export_settings['output_size'],
                    progress=self._parallel_progress,
                )
                exported_frames = result['frames']
                success = True
                self.logln(f"[导出] 编码器: {result['encoder']}")
                self.logln(
                    f"[DLSS 主机] {', '.join(result['host_backends'])}；"
                    f"队列 {result['in_flight']} 帧/进程"
                )
                self.logln(f"[导出] 音频: {result['audio_mode']}")
                self.logln(
                    f"[性能] {result['frames']} 帧 / {result['seconds']:.1f} 秒 = "
                    f"{result['fps']:.2f} fps；{result['workers']} 个 DLSS 进程"
                )
            else:
                writer = FFmpegVideoWriter(
                    out_path, w, h, fps, audio_source=source_path,
                    nvenc_preset=export_settings['nvenc_preset'],
                    rate_control=export_settings['rate_control'],
                    quality_profile=export_settings['quality_profile'],
                    video_bitrate_mbps=export_settings['video_bitrate_mbps'],
                    output_size=export_settings['output_size'],
                )
                self.logln(f"[导出] 编码器: {writer.encoder_name}；完成后保留原视频音轨")
                decode_buffer = export_settings['decode_buffer']
                if w * h >= 3840 * 2160:
                    decode_buffer = min(decode_buffer, 2)
                self.logln(
                    f"[导出] 加速流水线: 解码预读 {decode_buffer} 帧 + "
                    "顺序 DLSS + 后处理/编码线程"
                )
                pending_write = None
                pending_dlss = deque()
                last_ui_update = 0.0
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="dlss-export-write") as write_pool:
                    def consume_processed(processed_rgba):
                        nonlocal pending_write, exported_frames, last_ui_update
                        frame_index, original_frame = pending_dlss.popleft()
                        if processed_rgba is None:
                            raise RuntimeError(f"DLSS 处理第 {frame_index} 帧失败")
                        processed_bgr = cv2.cvtColor(processed_rgba, cv2.COLOR_RGBA2BGR)
                        if pending_write is not None:
                            pending_write.result()
                        pending_write = write_pool.submit(
                            _postprocess_and_write,
                            writer, original_frame, processed_bgr, view, mix,
                        )
                        exported_frames = frame_index + 1
                        now = time.perf_counter()
                        if now - last_ui_update >= 0.1 or exported_frames >= n:
                            self.set_progress(exported_frames, n, "流水线导出")
                            self.root.update()
                            self._raise_if_export_cancelled()
                            last_ui_update = now

                    for i, fr in self._iter_frames(
                        decode_buffer, tone_map_hdr=bool(color_info.get('is_hdr')),
                        source_path=source_path, color_info=color_info,
                    ):
                        self._raise_if_export_cancelled()
                        hh, ww = fr.shape[:2]
                        rgba = cv2.cvtColor(fr, cv2.COLOR_BGR2RGBA)
                        if live is None:
                            live = self._ensure_live(ww, hh, settings)
                            if live is None:
                                raise RuntimeError("DLSS 引擎初始化失败")
                            live.update(settings)
                            self.logln(
                                f"[DLSS 主机] {live.backend}；"
                                f"GPU 队列 {live.max_in_flight} 帧"
                            )

                        dlss_started = time.perf_counter()
                        if live.supports_async:
                            if not live.enqueue(rgba, reset=(i == 0)):
                                raise RuntimeError(f"DLSS 异步提交第 {i} 帧失败")
                            dlss_seconds += time.perf_counter() - dlss_started
                            pending_dlss.append((i, fr))
                            if len(pending_dlss) >= live.max_in_flight:
                                wait_started = time.perf_counter()
                                consume_processed(live.dequeue())
                                dlss_seconds += time.perf_counter() - wait_started
                        else:
                            o = live.process(rgba, reset=(i == 0))
                            dlss_seconds += time.perf_counter() - dlss_started
                            pending_dlss.append((i, fr))
                            consume_processed(o)
                    while pending_dlss:
                        self._raise_if_export_cancelled()
                        wait_started = time.perf_counter()
                        consume_processed(live.dequeue())
                        dlss_seconds += time.perf_counter() - wait_started
                    if pending_write is not None:
                        pending_write.result()
                self._raise_if_export_cancelled()
                writer.finish()
                success = True
                self.logln(f"[导出] 音频: {writer.audio_mode}")
                elapsed = time.perf_counter() - started_at
                throughput = exported_frames / elapsed if elapsed > 0 else 0.0
                self.logln(
                    f"[性能] {exported_frames} 帧 / {elapsed:.1f} 秒 = {throughput:.2f} fps；"
                    f"DLSS 串行耗时 {dlss_seconds:.1f} 秒"
                )
        except _ExportCancelled:
            cancelled = True
        except Exception as ex:
            traceback.print_exc()
            error_message = str(ex)
            self.logln("导出错误: " + error_message)
        finally:
            if writer and not success:
                writer.abort()
            if cancelled and live is not None:
                # An async single-session export may still own queued frames. Reusing
                # that host would return stale output on the next preview/export.
                self._close_live()
        self._end_export_ui(
            success, out_path, "完成",
            "已导出（含原音轨）:\n" + out_path if success else None,
            cancelled=cancelled,
            completed_items=exported_frames,
            notify=notify,
        )
        return {
            "success": success,
            "cancelled": cancelled,
            "error": error_message,
            "output_path": out_path,
            "frames": exported_frames,
        }

    def _iter_frames(
        self, buffer_size=4, tone_map_hdr=False, source_path=None, color_info=None,
    ):
        """Decode ahead on a worker so CPU decode overlaps the ordered DLSS stage."""
        frame_queue = queue.Queue(maxsize=max(int(buffer_size), 1))
        stop_event = threading.Event()
        decode_errors = []
        video_path = source_path or self.video
        color_info = color_info if color_info is not None else self._video_color_info

        def put_with_stop(item):
            while not stop_event.is_set():
                try:
                    frame_queue.put(item, timeout=0.1)
                    return True
                except queue.Full:
                    pass
            return False

        def decode_worker():
            cap = cv2.VideoCapture(video_path)
            try:
                if not cap.isOpened():
                    raise RuntimeError("无法打开视频进行导出解码")
                index = 0
                while not stop_event.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        break
                    if tone_map_hdr:
                        frame = tone_map_hdr_preview(frame, color_info)
                    if not put_with_stop((index, frame)):
                        return
                    index += 1
            except Exception as ex:
                decode_errors.append(ex)
            finally:
                cap.release()
                put_with_stop(_FRAME_STREAM_END)

        decode_thread = threading.Thread(
            target=decode_worker, name="dlss-export-decode", daemon=True
        )
        decode_thread.start()
        try:
            while True:
                item = frame_queue.get()
                if item is _FRAME_STREAM_END:
                    if decode_errors:
                        raise decode_errors[0]
                    break
                yield item
        finally:
            stop_event.set()
            decode_thread.join(timeout=2.0)


def main():
    if "--vsr-selftest" in sys.argv:
        try:
            source = np.zeros((180, 320, 4), np.uint8)
            source[..., 0] = np.linspace(0, 255, 320, dtype=np.uint8)
            source[..., 1] = np.linspace(0, 255, 180, dtype=np.uint8)[:, None]
            source[..., 2:] = 255
            with ProcessSuperResolution(320, 180, 2, is_hdr=False) as sr:
                upscaled = sr.process(source)
            settings = {
                'host_backend': 'v2', 'host_auto_fallback': False,
                'host_submission': 'merged', 'host_persistent_buffers': True,
                'host_zero_fast_path': True, 'host_in_flight': 1,
                'style': 0, 'intensity': 1.0, 'local_tone': 1.0,
                'local_struct': 1.0, 'skin_struct': 0.5, 'use_auto_mask': False,
            }
            live = ProcessLive(640, 360, settings)
            try:
                enhanced = live.process(upscaled, reset=True)
            finally:
                live.close()
            result = (
                f"VSR_DLSS_OK {enhanced.dtype} {enhanced.shape}"
                if enhanced is not None else "VSR_DLSS_FAIL no output"
            )
        except Exception as exception:
            result = "VSR_DLSS_FAIL " + repr(exception)[:1000]
        try:
            outdir = os.path.dirname(os.path.abspath(sys.argv[0]))
            with open(os.path.join(outdir, "_vsr_selftest.txt"), "w", encoding="utf-8") as handle:
                handle.write(result)
        except Exception:
            pass
        return
    if "--selftest" in sys.argv:
        # headless DLSS sanity check (writes a result file; used to verify the frozen exe
        # can load the host/runtime DLLs from _MEIPASS and actually run Feature 18).
        import dlss_engine
        try:
            f = np.full((360, 640, 3), 90, np.uint8)
            cv2.circle(f, (320, 180), 80, (255, 0, 0), -1)
            rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
            rgba = np.dstack([rgb, np.full((360, 640), 255, np.uint8)])
            live = dlss_engine.Live(640, 360, {'style': 1, 'intensity': 0.9})
            o = live.process(rgba, reset=True)
            # This is a one-shot process. Some NGX runtime/driver combinations hang
            # in dlssnr_shutdown after a successful evaluation, so let process exit
            # reclaim the D3D12 resources after the result file is written.
            ok = "DLSS_OK " + (str(o.shape) if o is not None else "None")
        except Exception as e:
            ok = "DLSS_FAIL " + repr(e)[:300]
        try:
            outdir = os.path.dirname(os.path.abspath(sys.argv[0]))
            with open(os.path.join(outdir, "_selftest.txt"), "w", encoding="utf-8") as fh:
                fh.write(ok)
        except Exception:
            pass
        return
    ui_theme.claim_app_identity()
    ui_theme.enable_dpi_awareness()
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    # Required for ProcessLive's spawn worker in a PyInstaller build.
    multiprocessing.freeze_support()
    if "--diagnostic-worker" in sys.argv:
        worker_index = sys.argv.index("--diagnostic-worker")
        raise SystemExit(diagnostics.diagnostic_worker_main(
            sys.argv[worker_index + 1:worker_index + 5]
        ))
    if "--parallel-worker" in sys.argv:
        sys.argv.remove("--parallel-worker")
        from parallel_export_worker import main as parallel_worker_main
        parallel_worker_main()
    else:
        main()
