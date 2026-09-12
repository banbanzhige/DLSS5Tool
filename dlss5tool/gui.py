#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py — 简约 DLSS5 实时预览 + 导出 (test4)

功能：导入视频或图片 → 实时预览(原图/DLSS/对比) → 调风格/强度/本地色调整/本地结构
      → 逐帧实时看出效果 → 导出 DLSS 视频或图片。

默认零引导，无需 torch/模型；进阶设置可选用户提供的深度/光流模块。
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

from dlss5tool import app_settings
from dlss5tool import i18n
from dlss5tool.app_version import APP_VERSION
from dlss5tool import diagnostics
from dlss5tool import dlss_engine
from dlss5tool import guidance_client
from dlss5tool.guidance_public import depth_enabled, public_mode
from dlss5tool.preview_comparison import PreviewComparison
from dlss5tool.guidance_export_ui import GuidanceExportUI
from dlss5tool.shared_cache_budget import SharedCacheBudget
from dlss5tool import mod_paths
from dlss5tool import export_queue as export_queue_state
from dlss5tool import updater
from dlss5tool import delta_update
from dlss5tool import update_helper
from dlss5tool import paths
from dlss5tool.dlss_host_process import ProcessLive
from dlss5tool.parallel_export import export_parallel
from dlss5tool.preview_audio import PreviewAudio, ms_to_frame
from dlss5tool.super_resolution import (
    ProcessSuperResolution, classify_resource_risk, estimate_resources,
    format_bytes, format_resource_hint, normalize_scale, query_gpu_memory,
    cached_gpu_memory, select_in_flight, MAX_TEXTURE_DIMENSION,
    runtime_status as super_resolution_runtime_status, target_size as super_resolution_target_size,
    validate_dimensions as validate_super_resolution_dimensions, SuperResolutionError,
)
from dlss5tool.video_export import (
    FFmpegHDRVideoReader, FFmpegVideoWriter, compose_hdr_frame,
    compose_output_frame, find_ffmpeg, output_container_extension,
    probe_video_stream, resolve_output_container, tone_map_hdr_preview,
)
from dlss5tool import ui_theme
from dlss5tool.ui_widgets import (
    AccentSlider, CheckToggle, ChipGroup, ChromeButton, ChromeCombobox,
    ChromeEntry, ChromeSpinbox, CollapsibleSection, ProgressRule, SegmentedBar,
    StatusPills, StudioNotebook, TimelineBar, Tooltip, round_rect,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

_STARTUP_SETTINGS = app_settings.load()
i18n.set_language(
    os.environ.get("DLSS5TOOL_LANG") or _STARTUP_SETTINGS.get("ui_language")
)
tr = i18n.tr

VIEWS = {
    "original": tr("view.original"),
    "dlss": tr("view.dlss"),
    "compare": tr("view.compare"),
}
STYLE_CHOICES = {
    tr("style.default"): 0,
    tr("style.natural"): 1,
    tr("style.cinema"): 2,
}
OUTVIEW_CHOICES = {
    tr("output_view.processed"): 0,
    tr("output_view.difference"): 1,
    tr("output_view.side_by_side"): 2,
}
STYLE_NAMES = {value: name for name, value in STYLE_CHOICES.items()}
OUTVIEW_NAMES = {value: name for name, value in OUTVIEW_CHOICES.items()}
EXPORT_MODE_CHOICES = {
    tr("export_mode.single"): "single",
    tr("export_mode.parallel"): "parallel",
}
EXPORT_MODE_NAMES = {value: name for name, value in EXPORT_MODE_CHOICES.items()}
NVENC_PRESET_CHOICES = {
    tr("preset.p1"): "p1", tr("preset.p3"): "p3",
    tr("preset.p5"): "p5", tr("preset.p7"): "p7",
}
NVENC_PRESET_NAMES = {value: name for name, value in NVENC_PRESET_CHOICES.items()}
OUTPUT_CONTAINER_CHOICES = {
    tr("container.mp4"): "mp4",
    "MKV": "mkv",
    "MOV": "mov",
    tr("container.source"): "source",
}
OUTPUT_CONTAINER_NAMES = {
    value: name for name, value in OUTPUT_CONTAINER_CHOICES.items()
}
OUTPUT_CONTAINER_LABELS = {"mp4": "MP4", "mkv": "MKV", "mov": "MOV"}
OUTPUT_RESOLUTION_CHOICES = {
    tr("resolution.source"): "source",
    "2160p": "2160p",
    "1440p": "1440p",
    "1080p": "1080p",
    "720p": "720p",
    tr("resolution.custom"): "custom",
}
OUTPUT_RESOLUTION_NAMES = {
    value: name for name, value in OUTPUT_RESOLUTION_CHOICES.items()
}
OUTPUT_RESOLUTION_MAX_EDGES = {
    "2160p": 3840, "1440p": 2560, "1080p": 1920, "720p": 1280,
}
SUPER_RESOLUTION_CHOICES = {tr("common.off"): 1, "2×": 2, "4×": 4}
SUPER_RESOLUTION_NAMES = {value: name for name, value in SUPER_RESOLUTION_CHOICES.items()}
RATE_CONTROL_CHOICES = {
    tr("rate.quality"): "quality",
    tr("rate.bitrate"): "bitrate",
}
RATE_CONTROL_NAMES = {value: name for name, value in RATE_CONTROL_CHOICES.items()}
QUALITY_PROFILE_CHOICES = {
    tr("quality.maximum"): "maximum",
    tr("quality.high"): "high",
    tr("quality.balanced"): "balanced",
    tr("quality.compact"): "compact",
}
QUALITY_PROFILE_NAMES = {value: name for name, value in QUALITY_PROFILE_CHOICES.items()}
HOST_BACKEND_CHOICES = {
    tr("backend.auto"): "auto", tr("backend.v2"): "v2",
    tr("backend.legacy"): "legacy",
}
HOST_BACKEND_NAMES = {value: name for name, value in HOST_BACKEND_CHOICES.items()}
HOST_SUBMISSION_CHOICES = {
    tr("submission.merged"): "merged",
    tr("submission.compatibility"): "compatibility",
}
HOST_SUBMISSION_NAMES = {value: name for name, value in HOST_SUBMISSION_CHOICES.items()}
PREVIEW_QUALITY_CHOICES = {
    tr("common.auto_recommended"): "auto",
    "1080p": "1080p",
    "1440p": "1440p",
    tr("preview_quality.original"): "original",
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
    (tr("common.media"), "*.mp4 *.avi *.mov *.mkv *.m4v *.webm *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
    (tr("common.video"), "*.mp4 *.avi *.mov *.mkv *.m4v *.webm"),
    (tr("common.image"), "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
    (tr("common.all_files"), "*.*"),
]
QUEUE_STATE_NAMES = {
    "pending": tr("queue.pending"),
    "running": tr("queue.running"),
    "completed": tr("queue.completed"),
    "failed": tr("queue.failed"),
    "cancelled": tr("queue.cancelled"),
    "interrupted": tr("queue.interrupted"),
}
QUEUE_STARTABLE_STATES = {"pending", "cancelled", "interrupted"}
IMAGE_ENCODE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
CANVAS_BG = "#161616"
CANVAS_DROP_BG = "#24405C"


def _is_dlss_runtime_unsupported(error):
    """Recognize NGX's FeatureNotSupported result without hiding raw diagnostics."""
    text = str(error or "")
    return bool(
        re.search(r"0xBAD00001", text, re.IGNORECASE)
        or re.search(r"FeatureNotSupported", text, re.IGNORECASE)
    )


def _dlss_runtime_guidance(error):
    if re.search(r"0xBAD00002\b", str(error), re.I):
        return tr("message.dlss_platform_error")
    if not _is_dlss_runtime_unsupported(error):
        return ""
    return tr(
        "message.dlss_runtime_unsupported",
        releases_url=updater.RELEASES_URL,
    )
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
APP_CREDIT = tr("app.credit")
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


def _preview_control_layout(width, language=None):
    """Choose a player-toolbar layout that never dictates a wide preview window."""
    width = max(int(width), 0)
    language = i18n.normalize_language(language or i18n.get_language())
    # Four localized guidance-view labels need more room than the original
    # three-item DLSS selector, plus the cache-clear tool.
    wide_min = 1000
    stacked_min = 560 if language == "en_US" else 460
    if width >= wide_min:
        return "wide"
    if width >= stacked_min:
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
        raise RuntimeError(tr("message.image_encode_failed"))
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
    """Plan a still-image request without mutating the video preferences.

    Static images have no temporal flow. Total VRAM (not fluctuating free RAM)
    keeps tile topology stable across repeated previews of the same image.
    """
    result = dict(settings or {})
    mode = int(result.get('guidance_mode', 0))
    if mode in (1, 3):
        result['guidance_mode'] = 0 if mode == 1 else 2
        result['_still_flow_skipped'] = True
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return result
    if width <= 0 or height <= 0:
        return result
    memory = cached_gpu_memory() or {}
    total_gib = memory.get('total_bytes', 0) / 1024 ** 3
    tile_width, tile_height = LARGE_IMAGE_TILE_WIDTH, LARGE_IMAGE_TILE_HEIGHT
    threshold = LARGE_IMAGE_TILE_THRESHOLD_PIXELS
    if 0 < total_gib <= 6:
        tile_width, tile_height, threshold = 3000, 1500, 12_000_000
    elif 0 < total_gib <= 8:
        tile_width, tile_height, threshold = 4000, 2000, 24_000_000
    if not (result.get('host_tiled_mode') or width * height >= threshold or max(width, height) > 8192):
        return result
    result.update({
        "host_backend": "v2",
        "host_auto_fallback": False,
        "host_zero_fast_path": True,
        "host_in_flight": 1,
        "host_tiled_mode": True,
        "host_tile_width": min(width, tile_width),
        "host_tile_height": min(height, tile_height),
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



class App(PreviewComparison, GuidanceExportUI):
    def __init__(self, root):
        self.root = root
        self._saved_settings = app_settings.startup_settings(app_settings.load())
        self._startup_guidance_mode = 0
        self._ui_language = i18n.get_language()
        self._preferred_ui_language = self._saved_settings.get(
            "ui_language", self._ui_language,
        )
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
        self._clear_preview_pending = False
        self._frame = 0
        self._exporting = False
        self._export_cancel_event = threading.Event()
        self._switching_backend = False
        self._render_gpu_scan_thread = None
        self._render_gpu_scan_after = None
        self._render_gpu_scan_queue = queue.SimpleQueue()
        self._module_reload_thread = None
        self._module_reload_after = None
        self._close_after_module_reload = False
        self._guidance_events = queue.SimpleQueue()
        self._guidance_generation = 0
        self._guidance_events_after = None
        self._diagnosing = False
        self._diagnostic_thread = None
        self._update_checking = False
        self._update_downloading = False
        self._update_progress_percent = None
        self._update_thread = None
        self._update_cancel_event = threading.Event()
        self._live = None
        self._shared_cache_pool = None
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
        self._init_comparison()
        self._theme_widgets = []

        # Studio: left monitor + fixed 360px inspector (matches the mockup).
        self._studio = ttk.Frame(root, style="Workspace.TFrame")
        self._studio.pack(fill="both", expand=True)
        language_min_width = 420 if self._ui_language == "en_US" else ui_theme.INSPECTOR_MIN
        self._inspector_width = max(
            language_min_width,
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

        self.workspace_tabs = StudioNotebook(self._inspector, ui=self._ui, command=self._on_workspace_selected)
        self.workspace_tabs.pack(fill="both", expand=True)
        self._theme_widgets.append(self.workspace_tabs)
        self._preview_page = ttk.Frame(self.workspace_tabs.content, style="Panel.TFrame")
        self._guidance_page = ttk.Frame(self.workspace_tabs.content, style="Panel.TFrame")
        self._export_page = ttk.Frame(self.workspace_tabs.content, style="Panel.TFrame")
        self.queue_tab = ttk.Frame(self.workspace_tabs.content, style="Panel.TFrame")
        self.workspace_tabs.add(self._preview_page, text=tr("tab.adjust"))
        self.workspace_tabs.add(self._guidance_page, text=tr("tab.guidance"))
        self.workspace_tabs.add(self._export_page, text=tr("tab.export"))
        self.workspace_tabs.add(self.queue_tab, text=tr("tab.queue"), badge="0")

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
            tr("tooltip.settings"),
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
            actions, text=tr("action.import"), variant="ghost", command=self.import_media,
            ui=self._ui, width=88,
        )
        self.import_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._theme_widgets.append(self.import_btn)
        self.clear_btn = ChromeButton(
            actions, text=tr("action.clear"), variant="ghost", command=self.clear_media,
            ui=self._ui, width=88,
        )
        self.clear_btn.grid(row=0, column=1, sticky="ew", padx=4)
        self._theme_widgets.append(self.clear_btn)
        Tooltip(self.clear_btn, tr("tooltip.clear_media"))
        self.add_queue_btn = ChromeButton(
            actions, text=tr("action.add_queue"), variant="default", command=self.add_current_to_queue,
            ui=self._ui, width=108,
        )
        self.add_queue_btn.grid(row=0, column=2, sticky="ew", padx=(4, 0))
        self._theme_widgets.append(self.add_queue_btn)
        Tooltip(self.add_queue_btn, tr("tooltip.add_queue"))
        run_bar = ttk.Frame(e, style="Panel.TFrame")
        run_bar.pack(fill="x", pady=(8, 0))
        for column in range(2):
            run_bar.columnconfigure(column, weight=1, uniform="export_run")
        self._export_run_bar = run_bar
        self.export_btn = ChromeButton(
            run_bar, text=tr("action.export_dlss"), variant="accent", command=self.export_dlss, ui=self._ui,
        )
        self.export_btn.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._theme_widgets.append(self.export_btn)
        self.cancel_export_btn = ChromeButton(
            run_bar, text=tr("action.cancel_export"), variant="danger", command=self.cancel_export,
            ui=self._ui, icon="cancel", primary=True,
        )
        self._theme_widgets.append(self.cancel_export_btn)
        Tooltip(self.cancel_export_btn, tr("tooltip.cancel_export"))
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
        self._export_inner = export_inner  # Settings page; retain legacy export identifiers.
        self._export_window = self._export_canvas.create_window(
            (0, 0), window=export_inner, anchor="nw",
        )
        export_inner.bind("<Configure>", self._sync_export_scrollregion)
        self._export_canvas.bind("<Configure>", self._resize_export_content)
        self._preview_section = CollapsibleSection(
            export_inner, tr("section.preview_performance"),
            collapsed=not self._saved_settings.get("ui_preview_open", False),
            on_toggle=self._on_panels_toggle,
            tooltip=(
                tr("tooltip.preview_performance")
            ),
            ui=self._ui,
        )
        self._theme_widgets.append(self._preview_section)
        self._preview_section.pack(fill="x", padx=16, pady=(10, 0))
        self._preview_settings = self._build_preview_settings(self._preview_section.body)
        self._preview_runtime_settings = self._collect_preview_settings()
        self.root.after_idle(self._update_preview_memory_hint)

        self._export_section = CollapsibleSection(
            export_inner, tr("section.export_settings"),
            collapsed=not self._saved_settings.get("ui_export_open", False),
            on_toggle=self._on_panels_toggle,
            tooltip=(
                tr("tooltip.export_settings")
            ),
            ui=self._ui,
        )
        self._theme_widgets.append(self._export_section)
        self._export_section.pack(fill="x", padx=16, pady=(12, 0), before=self._preview_section)
        self._export_settings = self._build_export_settings(self._export_section.body)

        self._host_section = CollapsibleSection(
            export_inner, tr("section.advanced_host"),
            collapsed=not self._saved_settings.get("ui_host_open", False),
            on_toggle=self._on_panels_toggle,
            tooltip=tr("tooltip.advanced_host"),
            ui=self._ui,
        )
        self._theme_widgets.append(self._host_section)
        self._host_section.pack(fill="x", padx=16, pady=(10, 0))
        self._host_settings = self._build_host_settings(self._host_section.body)
        self._guidance_page.rowconfigure(0, weight=1)
        self._guidance_page.columnconfigure(0, weight=1)
        self._guidance_canvas = tk.Canvas(
            self._guidance_page, background=self._ui['panel'],
            highlightthickness=0, borderwidth=0,
        )
        self._guidance_canvas.grid(row=0, column=0, sticky='nsew')
        self._guidance_scrollbar = ttk.Scrollbar(
            self._guidance_page, orient='vertical', command=self._guidance_canvas.yview,
        )
        self._guidance_scrollbar.grid(row=0, column=1, sticky='ns')
        self._guidance_scrollbar.grid_remove()
        self._guidance_scrollbar_visible = False
        self._guidance_canvas.configure(yscrollcommand=self._guidance_scrollbar.set)
        self._guidance_inner = ttk.Frame(self._guidance_canvas, style='Panel.TFrame')
        self._guidance_window = self._guidance_canvas.create_window(
            (0, 0), window=self._guidance_inner, anchor='nw',
        )
        self._guidance_inner.bind('<Configure>', self._sync_guidance_scrollregion)
        self._guidance_canvas.bind('<Configure>', self._resize_guidance_content)
        self._build_guidance_settings(self._guidance_inner)
        self._modules_section = CollapsibleSection(
            self._export_inner, tr('mods.section'), collapsed=not self._saved_settings.get('ui_modules_open', False),
            on_toggle=self._on_panels_toggle, ui=self._ui,
        )
        self._theme_widgets.append(self._modules_section)
        self._modules_section.pack(fill='x', padx=16, pady=(10, 0))
        self._build_module_settings(self._modules_section.body)
        self._build_guidance_export()

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
        ui_theme.install_ttk_scrolledtext_scrollbar(self.log)
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
        self._poll_guidance_events()
        self._sync_comparison_controls()
        if self._saved_settings.get("preview_detached", False):
            self.root.after_idle(self.detach_preview)
        if getattr(sys, "frozen", False) and not os.environ.get(
            "DLSS5TOOL_DISABLE_UPDATE_CHECK"
        ):
            # Start exactly one non-blocking check during application startup.
            # Up-to-date, newer local builds, and network failures stay silent.
            self.check_for_updates(manual=False)

        self._restore_guidance_mode()

    def _restore_guidance_mode(self):
        """Check the remembered mode asynchronously before allowing inference."""
        self._startup_guidance_mode = self._saved_settings.get('guidance_mode', 0)
        if self._startup_guidance_mode:
            # Widget construction already captured these settings. Force the
            # normal checked activation path even though nothing was edited.
            self._last_module_settings = None
            self._on_mod_settings_change()

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
        Tooltip(timeline, tr("tooltip.timeline"))

        ctrl = ttk.Frame(transport, style="Transport.TFrame")
        ctrl.pack(fill="x", padx=8, pady=(0, 8))
        left = ttk.Frame(ctrl, style="Transport.TFrame")
        left.pack(side="left")
        playback_bar = ttk.Frame(left, style="Transport.TFrame")
        playback_bar.pack(side="left")
        prev_btn = ChromeButton(
            playback_bar, text=tr("action.previous_frame"), icon="prev", icon_only=True,
            variant="tool", width=36, command=lambda: self.step_frame(-1), ui=self._ui,
        )
        prev_btn.pack(side="left")
        Tooltip(prev_btn, tr("tooltip.previous_frame"))
        play_btn = ChromeButton(
            playback_bar, text=tr("action.play"), icon="play", icon_only=True,
            variant="tool", width=36, command=self.toggle_play, ui=self._ui,
        )
        play_btn.pack(side="left", padx=(4, 0))
        Tooltip(play_btn, tr("tooltip.play"))
        next_btn = ChromeButton(
            playback_bar, text=tr("action.next_frame"), icon="next", icon_only=True,
            variant="tool", width=36, command=lambda: self.step_frame(1), ui=self._ui,
        )
        next_btn.pack(side="left", padx=(4, 0))
        Tooltip(next_btn, tr("tooltip.next_frame"))
        mute_btn = ChromeButton(
            playback_bar, text=tr("action.audio"), icon="volume", icon_only=True,
            variant="tool", width=36, command=self.toggle_mute, ui=self._ui,
        )
        mute_btn.pack(side="left", padx=(4, 0))
        Tooltip(mute_btn, tr("tooltip.audio"))

        position_bar = ttk.Frame(left, style="Transport.TFrame")
        position_bar.pack(side="left")
        time_label = ttk.Label(
            position_bar, text="0:00.00 / 0:00.00", width=18, anchor="w",
            style="Transport.TLabel", font=ui_theme.UI_MONO,
        )
        time_label.pack(side="left", padx=(10, 6))
        ttk.Label(position_bar, text=tr("label.frame"), style="Transport.TLabel").pack(side="left")
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
            right, self.preview_selector, VIEWS, command=self._on_preview_selection, ui=self._ui,
        )
        view_bar.pack(side="left", padx=(0, 8))
        self._install_comparison_menu(view_bar)
        self._theme_widgets.append(view_bar)
        theme_widgets.append(view_bar)
        Tooltip(
            view_bar,
            tr("tooltip.views"),
        )
        zoom_bar = ttk.Frame(right, style="Transport.TFrame")
        zoom_bar.pack(side="left", padx=(0, 8))
        zoom_out_btn = ChromeButton(
            zoom_bar, text=tr("action.zoom_out"), icon="minus", icon_only=True,
            variant="tool", width=36, command=lambda: self._step_zoom(-1), ui=self._ui,
        )
        zoom_out_btn.pack(side="left")
        zoom_reset_btn = ChromeButton(
            zoom_bar, text=tr("action.fit"), icon="fit", icon_only=True,
            variant="tool", width=36, command=self.reset_preview_zoom, ui=self._ui,
        )
        zoom_reset_btn.pack(side="left", padx=2)
        zoom_in_btn = ChromeButton(
            zoom_bar, text=tr("action.zoom_in"), icon="plus", icon_only=True,
            variant="tool", width=36, command=lambda: self._step_zoom(1), ui=self._ui,
        )
        zoom_in_btn.pack(side="left")
        Tooltip(zoom_out_btn, tr("tooltip.zoom_out"))
        Tooltip(zoom_reset_btn, tr("tooltip.fit"))
        Tooltip(zoom_in_btn, tr("tooltip.zoom_in"))
        clear_cache_btn = ChromeButton(
            zoom_bar, text=tr("action.clear_preview_cache"), icon="retry", icon_only=True,
            variant="tool", width=36, command=self.clear_preview_cache, ui=self._ui,
        )
        clear_cache_btn.pack(side="left", padx=(4, 0))
        Tooltip(clear_cache_btn, tr("tooltip.clear_preview_cache"))
        detach_btn = ChromeButton(
            right,
            text=tr("action.dock") if detached else tr("action.detach"),
            icon="dock" if detached else "detach",
            icon_only=True, variant="tool", width=36,
            command=self.toggle_detached_preview, ui=self._ui,
        )
        detach_btn.pack(side="left", padx=(0, 4))
        Tooltip(
            detach_btn,
            tr("tooltip.dock") if detached else tr("tooltip.detach"),
        )
        fs_btn = ChromeButton(
            right, text=tr("action.fullscreen"), icon="fullscreen", icon_only=True,
            variant="tool", width=36, command=self.toggle_fullscreen, ui=self._ui,
        )
        fs_btn.pack(side="left")
        Tooltip(fs_btn, tr("tooltip.fullscreen"))

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
            zoom_out_btn, zoom_reset_btn, zoom_in_btn, clear_cache_btn, detach_btn, fs_btn,
        ):
            self._theme_widgets.append(widget)
            theme_widgets.append(widget)

        return {
            "canvas": canvas,
            "view_bar": view_bar,
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
            "clear_cache_btn": clear_cache_btn,
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
        language_menu = tk.Menu(menu, tearoff=0)
        selected_language = tk.StringVar(value=self._preferred_ui_language)
        for code in i18n.SUPPORTED_LANGUAGES:
            language_menu.add_radiobutton(
                label=tr(f"language.{code}"), value=code,
                variable=selected_language,
                command=lambda value=code: self._select_ui_language(value),
            )
        menu.add_cascade(label=tr("language.menu"), menu=language_menu)
        try:
            menu.tk_popup(
                self.more_btn.winfo_rootx(),
                self.more_btn.winfo_rooty() + self.more_btn.winfo_height(),
            )
        finally:
            menu.grab_release()

    def _select_ui_language(self, language):
        language = i18n.normalize_language(language)
        if language == self._preferred_ui_language:
            return
        previous = self._preferred_ui_language
        self._preferred_ui_language = language
        values = self._collect_persisted_settings()
        try:
            self._saved_settings = app_settings.save(values)
        except Exception as ex:
            self._preferred_ui_language = previous
            self.logln(tr("log.settings_save_failed", error=ex))
            return
        messagebox.showinfo(
            tr("language.saved_title"),
            tr("language.saved_message", language=language),
        )

    def _build_status_bar(self, parent):
        bar = ttk.Frame(parent, style="Status.TFrame")
        self._status_bar = bar
        bar.pack(fill="x")
        dot = ui_theme.scale_px(parent, 10)
        self._status_dot = tk.Canvas(
            bar, width=dot, height=dot, highlightthickness=0, bg=self._ui["surface"],
        )
        self._status_dot.pack(side="left", padx=(12, 0), pady=8)
        self._status_host = ttk.Label(bar, text=tr("status.host_ready"), style="Status.TLabel")
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
            utility, text=tr("action.light_theme"), width=12, command=self.toggle_ui_theme,
        )
        self.diagnostic_btn = ttk.Button(
            utility, text=tr("action.diagnostics"), width=16, command=self.export_diagnostics,
        )
        self.update_btn = ttk.Button(
            utility, text=tr("action.check_updates"), width=16,
            command=lambda: self.check_for_updates(manual=True),
        )
        self.log_btn = ttk.Button(
            utility, text=tr("action.log"), width=8, command=self.toggle_log_panel,
        )
        self._status_metric = ttk.Label(utility, text=tr("status.waiting_import"), style="Status.TLabel")
        self._status_metric.pack(side="left", padx=(0, 10))
        self.more_btn = ChromeButton(
            utility, text=tr("action.more"), variant="ghost", width=36,
            icon="more", icon_only=True, command=self._popup_more, ui=self._ui,
        )
        self.more_btn.pack(side="right")
        self._theme_widgets.append(self.more_btn)
        Tooltip(self.more_btn, tr("tooltip.more"))

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
            self.log_btn.configure(text=tr("action.hide_log"))
        else:
            self.log.pack_forget()
            self.log_btn.configure(text=tr("action.log"))

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
                text=(tr("action.dark_theme") if self._ui_theme_name == "light"
                      else tr("action.light_theme")),
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
            self._guidance_canvas.configure(bg=self._ui["panel"])
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
        host = tr("status.waiting_import")
        if self._exporting:
            host = tr("status.exporting")
        elif self._queue_running:
            host = tr("status.queue_processing")
        elif self.video:
            host = tr("status.prerender_ready") if not self.playing else tr("status.playing")
        else:
            host = tr("status.host_ready")
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
        selected_mode = self._collect_host_settings().get('guidance_mode', 0) if hasattr(self, '_host_settings') else 0
        mode_label = tr('guidance.mode.' + str(selected_mode)) if selected_mode else tr('status.zero_guidance')
        pills = [("v2", "ok"), (mode_label, "warn" if selected_mode else "")]
        color = getattr(self, "_video_color_info", None) or {}
        if color.get("label"):
            pills.append((color.get("label"), "warn" if color.get("is_hdr") else ""))
        elif self.video:
            pills.append(("SDR · sRGB", ""))
        if self._media_w and self._media_h:
            pills.append((f"{self._media_w}×{self._media_h}", ""))
        if self.video and self.nframes:
            pills.append((tr("status.frames_count", frames=self.nframes), ""))
        settings = getattr(self, "_preview_runtime_settings", None)
        if settings and self.video:
            pills.append((tr(
                "status.cache_mib", value=settings.get('preview_cache_mb', 0)
            ), "ok"))
        try:
            self._status_chips.set_pills(pills)
        except Exception:
            pass
        try:
            self._status_metric.configure(
                text="" if self.video else tr("status.waiting_import")
            )
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
        self._sync_comparison_controls()
        self.timeline.set_range(timeline_state["minimum"], timeline_state["maximum"])
        self.timeline.set_cache_ranges(
            timeline_state["rendered"], timeline_state["queued"],
        )
        self.timeline.set(timeline_state["value"])
        self._sync_transport_labels()
        self._set_play_btn(self.playing)
        try:
            self.mute_btn.config(
                text=tr("action.muted") if self._audio.muted else tr("action.audio"),
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
                text=tr("action.dock") if detached else tr("action.detach"),
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
                if filename:
                    preview_suffix = (
                        f"Preview — {filename}" if self._ui_language == "en_US"
                        else f"预览 — {filename}"
                    )
                else:
                    preview_suffix = (
                        "Detached preview" if self._ui_language == "en_US" else "独立预览"
                    )
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
                text=tr("status.detached_preview"),
                style="Status.TLabel",
            ).pack(side="left")
            dock_btn = ChromeButton(
                placeholder, text=tr("action.dock_back"), command=self.dock_preview,
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
            self.logln(tr("log.detached_drop_failed", error=ex))

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

    def _resize_guidance_content(self, event=None):
        width = max(getattr(event, 'width', self._guidance_canvas.winfo_width()), 1)
        self._guidance_canvas.itemconfigure(self._guidance_window, width=width)
        self.root.after_idle(self._sync_guidance_scrollregion)

    def _sync_guidance_scrollregion(self, event=None):
        content_height = self._guidance_inner.winfo_reqheight()
        self._guidance_canvas.configure(
            scrollregion=(0, 0, self._guidance_canvas.winfo_width(), content_height),
        )
        needs_scrollbar = content_height > self._guidance_canvas.winfo_height() + 2
        if needs_scrollbar == self._guidance_scrollbar_visible:
            return
        self._guidance_scrollbar_visible = needs_scrollbar
        if needs_scrollbar:
            self._guidance_scrollbar.grid()
        else:
            self._guidance_canvas.yview_moveto(0.0)
            self._guidance_scrollbar.grid_remove()

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
            if cursor is getattr(self, '_guidance_page', None) and getattr(self, '_guidance_scrollbar_visible', False):
                target = self._guidance_canvas
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
            toolbar, tr("action.add_files"), self.add_queue_files, width=36,
            icon="file-plus", icon_only=True,
        )
        self.queue_add_files_btn.pack(side="left")
        Tooltip(self.queue_add_files_btn, tr("action.add_files"))
        self.queue_add_folder_btn = self._chrome_button(
            toolbar, tr("action.add_folder"), self.add_queue_folder, width=36,
            icon="folder-plus", icon_only=True,
        )
        self.queue_add_folder_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.queue_add_folder_btn, tr("action.add_folder"))
        self.queue_remove_btn = self._chrome_button(
            toolbar, tr("action.remove"), self.remove_selected_queue_jobs, variant="ghost", width=36,
            icon="trash", icon_only=True,
        )
        self.queue_remove_btn.pack(side="left", padx=(8, 0))
        Tooltip(self.queue_remove_btn, tr("tooltip.remove_jobs"))
        self.queue_clear_btn = self._chrome_button(
            toolbar, tr("action.clear_queue"), self.clear_queue_jobs, variant="ghost", width=36,
            icon="clear", icon_only=True,
        )
        self.queue_clear_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.queue_clear_btn, tr("action.clear_queue"))
        self.queue_retry_btn = self._chrome_button(
            toolbar, tr("action.retry"), self.retry_selected_queue_jobs, variant="ghost",
            width=36, icon="retry", icon_only=True,
        )
        self.queue_retry_btn.pack(side="left", padx=(8, 0))
        Tooltip(self.queue_retry_btn, tr("action.retry"))
        self.queue_clear_done_btn = self._chrome_button(
            toolbar, tr("action.clear_completed"), self.clear_completed_queue_jobs, variant="ghost",
            width=36, icon="clear-done", icon_only=True,
        )
        self.queue_clear_done_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.queue_clear_done_btn, tr("action.clear_completed"))
        self.queue_move_down_btn = self._chrome_button(
            toolbar, tr("action.move_down"), lambda: self.move_selected_queue_job(1), variant="ghost",
            width=36, icon="down", icon_only=True,
        )
        self.queue_move_down_btn.pack(side="right")
        Tooltip(self.queue_move_down_btn, tr("action.move_down"))
        self.queue_move_up_btn = self._chrome_button(
            toolbar, tr("action.move_up"), lambda: self.move_selected_queue_job(-1), variant="ghost",
            width=36, icon="up", icon_only=True,
        )
        self.queue_move_up_btn.pack(side="right", padx=(0, 4))
        Tooltip(self.queue_move_up_btn, tr("action.move_up"))

        output_row = ttk.Frame(parent, style="Panel.TFrame")
        output_row.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(output_row, text=tr("label.output_folder")).pack(side="left")
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
            output_row, tr("action.browse"), self.choose_queue_output_dir, variant="ghost",
            width=36, icon="folder-open", icon_only=True,
        )
        self.queue_output_browse_btn.pack(side="left")
        Tooltip(self.queue_output_browse_btn, tr("tooltip.choose_output"))
        Tooltip(
            self.queue_output_entry,
            tr("tooltip.queue_output"),
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
            "state": tr("label.status"), "source": tr("label.file"),
            "info": tr("label.media_info"), "settings": tr("common.settings"),
            "output": tr("common.output"), "progress": tr("label.progress"),
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
            state_width = 64 if self._ui_language == "en_US" else 48
            progress_width = 82 if self._ui_language == "en_US" else 64
            self.queue_tree.column("state", width=state_width, minwidth=40, stretch=False)
            self.queue_tree.column("progress", width=progress_width, minwidth=48, stretch=False)
            self.queue_tree.column(
                "source", width=max(60, width - state_width - progress_width - 2),
                minwidth=60, stretch=True,
            )
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
            details_row, text=tr("status.select_job"),
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
            actions, tr("action.load_preview"), self.load_selected_queue_job, variant="ghost",
        )
        self.queue_load_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 6))
        self.queue_apply_settings_btn = self._chrome_button(
            actions, tr("action.apply_settings"), self.apply_current_settings_to_queue, variant="ghost",
        )
        self.queue_apply_settings_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 6))
        Tooltip(self.queue_apply_settings_btn, tr("tooltip.apply_settings"))
        run_bar = ttk.Frame(actions, style="Panel.TFrame")
        run_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        for column in range(2):
            run_bar.columnconfigure(column, weight=1, uniform="queue_run")
        self._queue_run_bar = run_bar
        self.queue_start_btn = self._chrome_button(
            run_bar, tr("action.start_queue"), self.start_export_queue, variant="accent",
            icon="play",
        )
        self.queue_pause_btn = self._chrome_button(
            run_bar, tr("action.pause"), self.pause_export_queue_after_current,
            variant="outline", icon="pause", primary=True,
        )
        Tooltip(self.queue_pause_btn, tr("tooltip.pause_queue"))
        self.queue_cancel_btn = self._chrome_button(
            run_bar, tr("action.cancel"), self.cancel_current_queue_job,
            variant="danger", icon="cancel", primary=True,
        )
        Tooltip(self.queue_cancel_btn, tr("tooltip.cancel_queue"))
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
        size = f"{width}×{height}" if width and height else tr("common.unknown_dimensions")
        if job.media_kind == "image":
            return tr("queue.image_info", size=size)
        color = (job.color_info or {}).get("label", tr("common.not_checked"))
        return (
            tr("queue.video_info_fps", size=size, fps=fps, color=color)
            if fps else tr("queue.video_info", size=size, color=color)
        )

    @staticmethod
    def _queue_settings_text(job):
        settings = job.settings or {}
        export = job.export_settings or {}
        style = STYLE_NAMES.get(settings.get("style"), tr("style.default"))
        scale = normalize_scale(
            export.get("super_resolution_scale", settings.get("super_resolution_scale", 1))
        )
        scale_note = tr("queue.scale_note", scale=scale) if scale > 1 else ""
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
            quality = QUALITY_PROFILE_NAMES.get(export.get("quality_profile"), tr("quality.high"))
        mode = tr("queue.mode_strict") if export.get("mode", "single") == "single" else tr("queue.mode_parallel")
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
        style = STYLE_NAMES.get(settings.get("style"), tr("style.default"))
        scale = normalize_scale(
            export.get("super_resolution_scale", settings.get("super_resolution_scale", 1))
        )
        bits = [style]
        if job.media_kind == "image":
            image_format = os.path.splitext(job.output_path or job.source_path)[1].upper().lstrip(".") or "PNG"
            bits.extend((image_format, tr("common.original_size")))
        else:
            if export.get("rate_control") == "bitrate":
                try:
                    bitrate = float(export.get("video_bitrate_mbps", 20))
                except (TypeError, ValueError, OverflowError):
                    bitrate = 20.0
                bits.append(f"{bitrate:g} Mbps")
            else:
                bits.append(QUALITY_PROFILE_NAMES.get(export.get("quality_profile"), tr("quality.balanced")))
            bits.append(
                tr("queue.mode_single") if export.get("mode", "single") == "single"
                else tr("queue.mode_segments")
            )
        if scale > 1:
            bits.append(tr("queue.upscale", scale=scale))
        return " · ".join(bits)

    @staticmethod
    def _queue_progress_text(job):
        if job.state == "completed":
            return "100%"
        if job.state in {"failed", "cancelled", "interrupted"}:
            if job.progress_total > 0:
                pct = 100.0 * min(job.progress_done, job.progress_total) / job.progress_total
                return tr("queue.retry_percent", percent=pct)
            return tr("queue.retry_available")
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
            self.workspace_tabs.tab(self.queue_tab, text=tr("tab.queue"))
            self.workspace_tabs.set_badge(self.queue_tab, badge)
        except Exception:
            pass
        self._update_queue_action_states()
        self._on_queue_tree_select()

    def _update_queue_action_states(self):
        if not hasattr(self, "queue_start_btn"):
            return
        selected = self._selected_queue_jobs()
        editable = not (self._queue_running or self._exporting or self._diagnosing
                        or getattr(self, '_switching_backend', False))
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
            text=tr("action.resume_queue") if resume else tr("action.start_queue"),
            icon="play",
        )
        self.queue_pause_btn.config(
            text=tr("action.pause_pending") if self._queue_pause_requested else tr("action.pause"),
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
                tr("status.add_media_prompt")
                if not self._queue_jobs else
                tr("status.select_job")
            )
        elif len(selected) > 1:
            text = tr("status.selected_jobs", count=len(selected))
        else:
            job = selected[0]
            src = os.path.basename(job.source_path)
            out = os.path.basename(job.output_path or "")
            text = f"{src}  →  {out}\n{self._queue_details_summary(job)}"
            if job.error:
                text += tr("queue.error", error=job.error)
            tip = getattr(self, "_queue_details_tip", None)
            if tip is not None:
                tip.text = tr(
                    "queue.paths", source=job.source_path,
                    output=job.output_path or "",
                )
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
                raise RuntimeError(tr("message.video_unreadable"))
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if width <= 0 or height <= 0:
                raise RuntimeError(tr("message.video_dimensions_failed"))
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
                raise RuntimeError(tr("message.image_unreadable"))
            height, width = image.shape[:2]
            return {
                "frames": 1, "fps": 0.0, "width": width, "height": height,
                "duration": 0.0,
            }, {"is_hdr": False, "profile": "srgb", "label": tr("queue.sdr_image")}
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
            messagebox.showwarning(tr("dialog.add_queue"), tr("message.import_queue_first"))
            return
        self._add_paths_to_queue([self.video])

    def _add_paths_to_queue(self, paths, switch_tab=True):
        if self._queue_running or self._exporting:
            messagebox.showinfo(tr("dialog.queue_busy"), tr("message.queue_busy"))
            return 0
        normalized = []
        for raw in paths:
            path = os.path.abspath(os.path.normpath(str(raw)))
            if os.path.isfile(path) and (_is_video_path(path) or _is_image_path(path)):
                normalized.append(path)
        if not normalized:
            messagebox.showwarning(tr("dialog.add_to_queue"), tr("message.no_supported_media"))
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
                        color_info = {
                            "is_hdr": False, "profile": "srgb",
                            "label": tr("queue.sdr_image"),
                        }
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
        note = tr("status.added_media", added=added)
        if duplicates:
            note += tr("status.skipped_duplicates", count=duplicates)
        if invalid:
            note += tr("status.invalid_media", count=invalid)
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
        if not messagebox.askyesno(
            tr("dialog.clear_queue"), tr("message.remove_all_jobs", count=count)
        ):
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
        if getattr(self, '_switching_backend', False):
            return
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
        job.progress_label = tr("status.preparing_export")
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
                job.progress_label = tr("common.completed")
            elif result["cancelled"]:
                job.state = "cancelled"
                job.error = tr("message.job_cancelled")
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
            message = tr("status.queue_paused")
        else:
            message = tr(
                "status.queue_finished", completed=completed,
                failed=failed, cancelled=cancelled,
            )
        self.logln("[队列] " + message)
        if not paused:
            messagebox.showinfo(tr("dialog.export_queue"), message)

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
        self._update_guidance_export_controls()
        try:
            has = bool(self.video)
            busy = bool(self._exporting or self._queue_running or self._diagnosing
                        or self._switching_backend)
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
            self.cancel_export_btn.config(
                text=tr("status.cancelling") if cancel_requested else tr("action.cancel_export")
            )
            self._set_ttk_enabled(
                self.diagnostic_btn,
                not self._exporting
                and not self._queue_running
                and not self._switching_backend
                and not self._diagnosing,
            )
            self.diagnostic_btn.config(
                text=tr("status.diagnosing") if self._diagnosing else tr("action.diagnostics")
            )
            update_busy = self._update_checking or self._update_downloading
            self._set_ttk_enabled(self.update_btn, not update_busy)
            if self._update_downloading:
                percent = self._update_progress_percent
                update_text = (
                    tr("status.download_percent", percent=percent)
                    if percent is not None else tr("status.downloading_update")
                )
            elif self._update_checking:
                update_text = tr("status.checking")
            else:
                update_text = tr("action.check_updates")
            self.update_btn.config(text=update_text)
            if not has:
                self.export_btn.config(text=tr("action.export_dlss"))
            elif self._is_image:
                self.export_btn.config(text=tr("action.export_image"))
            else:
                self.export_btn.config(text=tr("action.export_video"))
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
        d['v_style'] = tk.StringVar(value=STYLE_NAMES.get(saved['style'], tr("style.default")))
        d['v_enable_5x'] = tk.BooleanVar(value=saved.get('enable_5x', False))
        d['v_intensity'] = tk.DoubleVar(value=saved['intensity'])
        d['v_use_intensity'] = tk.BooleanVar(value=saved['use_intensity'])
        d['v_local_tone'] = tk.DoubleVar(value=saved['local_tone'])
        d['v_use_local_tone'] = tk.BooleanVar(value=saved['use_local_tone'])
        d['v_local_struct'] = tk.DoubleVar(value=saved['local_struct'])
        d['v_use_local_struct'] = tk.BooleanVar(value=saved['use_local_struct'])
        d['v_auto_mask'] = tk.BooleanVar(value=saved['use_auto_mask'])
        d['v_skin_struct'] = tk.DoubleVar(value=saved['skin_struct'])
        d['v_outview'] = tk.StringVar(
            value=OUTVIEW_NAMES.get(saved['output_view'], tr("output_view.processed"))
        )
        d['v_outmix'] = tk.DoubleVar(value=saved['output_mix'])
        d['v_use_output_mix'] = tk.BooleanVar(value=saved['use_output_mix'])
        body = ttk.Frame(parent, style="Panel.TFrame")
        body.pack(fill="x", padx=0, pady=2)

        ttk.Label(body, text=tr("label.style"), style="Kicker.TLabel").pack(
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
            sliders, 0, 0, tr("label.strength"), d['v_intensity'], d['v_use_intensity'],
            tr("tooltip.strength"), slider_max=slider_max,
        )
        d['w_use_output_mix'], d['w_outmix'], d['w_outmix_value'] = self._add_toggle_slider(
            sliders, 1, 0, tr("label.output_mix"), d['v_outmix'], d['v_use_output_mix'],
            tr("tooltip.output_mix"),
            on_change=self.on_output_settings_change,
            slider_max=slider_max,
        )
        _, d['w_local_tone'], d['w_local_tone_value'] = self._add_toggle_slider(
            sliders, 2, 0, tr("label.local_tone"), d['v_local_tone'], d['v_use_local_tone'],
            tr("tooltip.local_tone"), slider_max=slider_max,
        )
        _, d['w_local_struct'], d['w_local_struct_value'] = self._add_toggle_slider(
            sliders, 3, 0, tr("label.local_structure"), d['v_local_struct'], d['v_use_local_struct'],
            tr("tooltip.local_structure"), slider_max=slider_max,
        )
        _, d['w_skin_struct'], d['w_skin_struct_value'] = self._add_toggle_slider(
            sliders, 4, 0, tr("label.skin_mask"), d['v_skin_struct'], d['v_auto_mask'],
            tr("tooltip.skin_mask"),
            slider_max=slider_max,
        )

        enable_5x = CheckToggle(
            body, tr("label.experimental_range"), d['v_enable_5x'],
            command=self._on_5x_toggle, ui=self._ui,
        )
        enable_5x.pack(anchor="w", pady=(8, 0))
        self._theme_widgets.append(enable_5x)
        d['w_enable_5x'] = enable_5x
        Tooltip(
            enable_5x,
            tr("tooltip.experimental_range"),
        )
        range_hint = ttk.Label(
            body, text="", style="Hint.TLabel", wraplength=320, justify="left",
        )
        d['w_range_hint'] = range_hint

        d['v_guidance'] = tk.StringVar(value=tr('guidance.mode.' + str(saved.get('guidance_mode', 0))))

        ttk.Label(body, text=tr("label.output_preview"), style="Kicker.TLabel").pack(
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
            tr("tooltip.output_view"),
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
                + tr("tooltip.slider_input")
                + tr("tooltip.slider_range")
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
            hint.config(text=tr("status.experimental_range"))
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
        mix_view = d['v_outview'].get() == OUTVIEW_NAMES[0]
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
                value=PREVIEW_QUALITY_NAMES.get(
                    saved.get('preview_quality', 'auto'), tr("common.auto_recommended")
                )
            ),
            'v_prefetch': tk.IntVar(value=saved.get('preview_prefetch', 24)),
            'v_cache': tk.IntVar(value=saved.get('preview_cache', 96)),
            'v_cache_mb': tk.IntVar(value=saved.get('preview_cache_mb', 2048)),
            'v_scrub_ms': tk.IntVar(value=saved.get('preview_scrub_ms', 40)),
        }

        parent.grid_columnconfigure(0, weight=1)
        preview_group = ttk.Frame(parent, style="Panel.TFrame")
        ttk.Label(preview_group, text=tr("section.playback_cache"), style="Kicker.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6),
        )
        preview_group.grid(row=0, column=0, sticky="ew", padx=(6, 8), pady=(2, 4))
        preview_group.grid_columnconfigure(1, weight=1)

        quality = self._chrome_combo(
            preview_group, d['v_quality'], list(PREVIEW_QUALITY_CHOICES),
        )
        ttk.Label(preview_group, text=tr("label.playback_quality")).grid(row=1, column=0, sticky="w", pady=3)
        quality.grid(row=1, column=1, sticky="ew", pady=3)
        Tooltip(quality, tr("tooltip.playback_quality"))

        cache_row = ttk.Frame(preview_group, style="Panel.TFrame")
        cache_mb = self._chrome_spin(
            cache_row, from_=256, to=32768, increment=256,
            textvariable=d['v_cache_mb'], width=7,
        )
        cache_mb.pack(side="left")
        ttk.Label(cache_row, text="MiB", font=ui_theme.UI_MONO).pack(
            side="left", padx=(6, 0),
        )
        ttk.Label(preview_group, text=tr("label.cache_budget")).grid(row=2, column=0, sticky="w", pady=3)
        cache_row.grid(row=2, column=1, sticky="w", pady=3)

        ttk.Label(preview_group, text=tr("label.startup_buffer")).grid(row=3, column=0, sticky="w", pady=3)
        ttk.Label(
            preview_group, text=tr("value.seconds", value=f"{PREVIEW_BUFFER_SECONDS:.1f}"),
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
        ttk.Label(preview_group, text=tr("label.render_after_scrub")).grid(row=4, column=0, sticky="w", pady=3)
        scrub_row.grid(row=4, column=1, sticky="w", pady=3)
        Tooltip(scrub, tr("tooltip.render_after_scrub"))
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

    def _ensure_shared_cache_pool(self):
        with self._cache_lock:
            if getattr(self, '_shared_cache_pool', None) is None:
                self._shared_cache_pool = SharedCacheBudget(limit_bytes=self._preview_cache_bytes())
                with self._shared_cache_pool.locked():
                    self._publish_frame_cache_locked()
            return self._shared_cache_pool

    def _available_frame_cache_bytes(self):
        pool = getattr(self, '_shared_cache_pool', None)
        if pool is None:return self._preview_cache_bytes()
        with pool.locked():return pool.allowance_locked(respect_demand=True)

    def _publish_frame_cache_locked(self):
        pool = getattr(self, '_shared_cache_pool', None)
        if pool:
            # Caller holds both the local cache lock and pool accounting lock.
            # Pending minimum demand only borrows space when needed, never a split.
            used = self._dlss_cache_bytes + self._source_cache_bytes
            demand = getattr(self, '_frame_cache_pending_bytes', 0)
            if demand > pool.limit_bytes:demand = 0;self._frame_cache_pending_bytes = 0
            pool.publish_locked(used, max(used, demand))

    def _preview_scrub_ms(self):
        settings = getattr(self, '_preview_runtime_settings', None)
        return (settings or self._collect_preview_settings())['preview_scrub_ms']

    def _on_preview_settings_change(self, event=None):
        self._preview_runtime_settings = self._collect_preview_settings()
        if getattr(self, '_shared_cache_pool', None):
            self._shared_cache_pool.set_limit(self._preview_cache_bytes())
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
            if self.playing and self.view_var.get() in ("dlss", "compare"):
                self._start_strict_preview_buffering()
                self._present_play_frame(self._frame)
            elif self.view_var.get() in ("dlss", "compare"):
                self._schedule_full_preview()

    def _update_preview_memory_hint(self):
        settings = getattr(self, "_preview_runtime_settings", None) or self._collect_preview_settings()
        budget_mib = settings['preview_cache_mb']
        source_w, source_h = self._source_size()
        if source_w <= 0 or source_h <= 0:
            text = tr("hint.cache_budget", budget=budget_mib)
        else:
            preview_w, preview_h = _realtime_preview_size(
                source_w, source_h, settings['preview_quality'],
            )
            pair_bytes = max((source_w * source_h + preview_w * preview_h) * 3, 1)
            frames = max(int(self._available_frame_cache_bytes() // pair_bytes), 1)
            seconds = frames / max(float(self.fps), 1.0)
            text = tr(
                "hint.cache_estimate", frames=frames, seconds=seconds,
                startup=PREVIEW_BUFFER_SECONDS, width=preview_w, height=preview_h,
            )
        shared_hint = tr('guidance.cache_budget', budget=budget_mib)
        text = text + '\n' + shared_hint if source_w > 0 and source_h > 0 else shared_hint
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
                value=NVENC_PRESET_NAMES.get(saved['nvenc_preset'], tr("preset.p5"))
            ),
            'v_output_container': tk.StringVar(value=OUTPUT_CONTAINER_NAMES.get(
                saved.get('output_container', 'mp4'), tr("container.mp4")
            )),
            'v_output_resolution': tk.StringVar(value=OUTPUT_RESOLUTION_NAMES.get(
                saved.get('output_resolution', 'source'), tr("resolution.source")
            )),
            'v_super_resolution': tk.StringVar(value=SUPER_RESOLUTION_NAMES.get(
                normalize_scale(saved.get('super_resolution_scale', 1)), tr("common.off")
            )),
            'v_custom_width': tk.IntVar(value=saved.get('custom_output_width', 1920)),
            'v_custom_height': tk.IntVar(value=saved.get('custom_output_height', 1080)),
            'v_rate_control': tk.StringVar(value=RATE_CONTROL_NAMES.get(
                saved.get('rate_control', 'quality'), tr("rate.quality")
            )),
            'v_quality_profile': tk.StringVar(value=QUALITY_PROFILE_NAMES.get(
                saved.get('quality_profile', 'high'), tr("quality.high")
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
        ttk.Label(output_group, text=tr("section.output_encoding"), style="Kicker.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6),
        )
        performance_group = ttk.Frame(parent, style="Panel.TFrame")
        performance_group.grid(row=1, column=0, sticky="ew", padx=(6, 8), pady=(0, 4))
        ttk.Label(performance_group, text=tr("section.performance"), style="Kicker.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6),
        )
        output_group.grid_columnconfigure(1, weight=1)
        performance_group.grid_columnconfigure(1, weight=1)

        container = self._chrome_combo(
            output_group, d['v_output_container'], list(OUTPUT_CONTAINER_CHOICES),
        )
        ttk.Label(output_group, text=tr("label.output_container")).grid(row=1, column=0, sticky="w", pady=3)
        container.grid(row=1, column=1, sticky="ew", pady=3)

        resolution = self._chrome_combo(
            output_group, d['v_output_resolution'], list(OUTPUT_RESOLUTION_CHOICES),
        )
        ttk.Label(output_group, text=tr("label.output_resolution")).grid(row=2, column=0, sticky="w", pady=3)
        resolution.grid(row=2, column=1, sticky="ew", pady=3)

        custom_label = ttk.Label(output_group, text=tr("resolution.custom"))
        custom_label.grid(row=3, column=0, sticky="w", pady=3)
        custom_frame = ttk.Frame(output_group, style="Panel.TFrame")
        custom_frame.grid(row=3, column=1, sticky="w", pady=3)
        custom_width = self._chrome_spin(
            custom_frame, from_=2, to=MAX_TEXTURE_DIMENSION, increment=2,
            textvariable=d['v_custom_width'], width=6,
        )
        custom_width.pack(side="left")
        ttk.Label(custom_frame, text="×").pack(side="left", padx=4)
        custom_height = self._chrome_spin(
            custom_frame, from_=2, to=MAX_TEXTURE_DIMENSION, increment=2,
            textvariable=d['v_custom_height'], width=6,
        )
        custom_height.pack(side="left")

        rate_control = self._chrome_combo(
            output_group, d['v_rate_control'], list(RATE_CONTROL_CHOICES),
        )
        ttk.Label(output_group, text=tr("label.rate_control")).grid(row=4, column=0, sticky="w", pady=3)
        rate_control.grid(row=4, column=1, sticky="ew", pady=3)

        quality_label = ttk.Label(output_group, text=tr("label.encoding_quality"))
        quality_label.grid(row=5, column=0, sticky="w", pady=3)
        quality = self._chrome_combo(
            output_group, d['v_quality_profile'], list(QUALITY_PROFILE_CHOICES),
        )
        quality.grid(row=5, column=1, sticky="ew", pady=3)

        bitrate_label = ttk.Label(output_group, text=tr("label.target_bitrate"))
        bitrate_label.grid(row=6, column=0, sticky="w", pady=3)
        bitrate = self._chrome_spin(
            output_group, from_=0.5, to=500.0, increment=0.5,
            textvariable=d['v_video_bitrate'], width=8,
        )
        bitrate.grid(row=6, column=1, sticky="w", pady=3)

        preset = self._chrome_combo(
            output_group, d['v_nvenc_preset'], list(NVENC_PRESET_CHOICES),
        )
        ttk.Label(output_group, text=tr("label.encoding_speed")).grid(row=7, column=0, sticky="w", pady=3)
        preset.grid(row=7, column=1, sticky="ew", pady=3)

        super_resolution = self._chrome_combo(
            output_group, d['v_super_resolution'], list(SUPER_RESOLUTION_CHOICES),
        )
        ttk.Label(output_group, text=tr("label.ai_upscale")).grid(row=8, column=0, sticky="w", pady=3)
        super_resolution.grid(row=8, column=1, sticky="ew", pady=3)

        hdr = CheckToggle(
            output_group, tr("label.hdr_precision"), d['v_hdr'],
            command=self._on_export_settings_change, ui=self._ui,
        )
        hdr.grid(row=9, column=0, columnspan=2, sticky="w", pady=(6, 2))
        self._theme_widgets.append(hdr)

        hint = ttk.Label(
            output_group,
            text=tr("hint.hdr_main10"),
            style="Hint.TLabel", wraplength=320, justify="left",
        )
        hint.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        Tooltip(
            hdr,
            tr("tooltip.hdr_precision"),
        )

        mode = self._chrome_combo(
            performance_group, d['v_mode'], list(EXPORT_MODE_CHOICES),
        )
        ttk.Label(performance_group, text=tr("label.export_mode")).grid(row=1, column=0, sticky="w", pady=3)
        mode.grid(row=1, column=1, sticky="ew", pady=3)

        workers = self._chrome_spin(
            performance_group, from_=2, to=4, textvariable=d['v_workers'], width=7,
        )
        ttk.Label(performance_group, text=tr("label.parallel_workers")).grid(row=2, column=0, sticky="w", pady=3)
        workers.grid(row=2, column=1, sticky="w", pady=3)

        warmup = self._chrome_spin(
            performance_group, from_=0, to=120, textvariable=d['v_warmup'], width=7,
        )
        ttk.Label(performance_group, text=tr("label.warmup_frames")).grid(row=3, column=0, sticky="w", pady=3)
        warmup.grid(row=3, column=1, sticky="w", pady=3)

        decode = self._chrome_spin(
            performance_group, from_=1, to=8, textvariable=d['v_decode_buffer'], width=7,
        )
        ttk.Label(performance_group, text=tr("label.decode_buffer")).grid(row=4, column=0, sticky="w", pady=3)
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
            tr("tooltip.container"),
        )
        Tooltip(
            resolution,
            tr("tooltip.resolution"),
        )
        Tooltip(
            super_resolution,
            tr("tooltip.super_resolution"),
        )
        Tooltip(
            rate_control,
            tr("tooltip.rate_control"),
        )
        Tooltip(preset, tr("tooltip.preset"))
        self.root.after_idle(self._update_export_control_states)
        return d

    def _build_export_quick(self, parent):
        d = self._export_settings
        ttk.Label(parent, text=tr("section.current_export"), style="Kicker.TLabel").pack(anchor="w", pady=(4, 6))
        fields = (
            (tr("label.container"), d["v_output_container"], list(OUTPUT_CONTAINER_CHOICES),
             "w_output_container", lambda _event: self._on_export_settings_change()),
            (tr("label.size"), d["v_output_resolution"], list(OUTPUT_RESOLUTION_CHOICES),
             "w_output_resolution", lambda _event: self._on_export_settings_change()),
            (tr("label.upscale"), d["v_super_resolution"], list(SUPER_RESOLUTION_CHOICES),
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
        render_gpu_id = saved.get('render_gpu', dlss_engine.RENDER_GPU_AUTO)
        d = {
            'v_backend': tk.StringVar(value=HOST_BACKEND_NAMES[saved['host_backend']]),
            'v_render_gpu': tk.StringVar(value=(
                tr('gpu.auto_nvidia') if render_gpu_id == dlss_engine.RENDER_GPU_AUTO
                else tr('gpu.detecting_saved')
            )),
            'render_gpu_selected_id': render_gpu_id,
            'render_gpu_choices': {
                tr('gpu.auto_nvidia'): dlss_engine.RENDER_GPU_AUTO,
            },
            'v_submission': tk.StringVar(
                value=HOST_SUBMISSION_NAMES[saved['host_submission']]
            ),
            'v_zero_fast': tk.BooleanVar(value=saved['host_zero_fast_path']),
            'v_persistent': tk.BooleanVar(value=saved['host_persistent_buffers']),
            'v_in_flight': tk.IntVar(value=saved['host_in_flight']),
            'v_fallback': tk.BooleanVar(value=saved['host_auto_fallback']),
            'v_runtime': tk.StringVar(value=tr('mods.bundled') if saved.get('dlss_runtime') == '__bundled__' else (saved.get('dlss_runtime', '') or tr('mods.auto'))),
            'v_guidance': self._settings['v_guidance'],
            'v_guidance_edge': tk.IntVar(value=saved.get('guidance_edge', 720)),
            'v_flow_direction': tk.StringVar(value=tr('guidance.option.' + saved.get('guidance_flow_direction', 'backward'))),
            'v_depth_encoder': tk.StringVar(value=tr('guidance.option.' + saved.get('guidance_depth_encoder', 'auto'))),
            'v_guidance_device': tk.StringVar(value=tr('guidance.option.' + saved.get('guidance_device', 'auto'))),
            'v_depth_profile': tk.StringVar(value=tr('guidance.option.' + saved.get('guidance_depth_profile', 'fp32'))),
            'v_guidance_execution': tk.StringVar(value=tr('guidance.option.' + saved.get('guidance_execution', 'serial'))),
        }

        parent.grid_columnconfigure(0, weight=1)
        host_wrap = ttk.Frame(parent, style="Panel.TFrame")
        host_wrap.grid(row=0, column=0, sticky="ew", padx=(6, 8), pady=(2, 4))
        ttk.Label(host_wrap, text=tr("section.host_submission"), style="Kicker.TLabel").pack(
            anchor="w", pady=(0, 6),
        )
        host_group = ttk.Frame(host_wrap, style="Panel.TFrame")
        host_group.pack(fill="x")
        host_group.grid_columnconfigure(1, weight=1)

        backend = self._chrome_combo(
            host_group, d['v_backend'], list(HOST_BACKEND_CHOICES),
        )
        ttk.Label(host_group, text=tr("label.backend")).grid(row=0, column=0, sticky="w", pady=3)
        backend.grid(row=0, column=1, sticky="ew", pady=3)

        render_gpu_wrap = ttk.Frame(host_group, style="Panel.TFrame")
        render_gpu_wrap.grid(row=1, column=1, sticky="ew", pady=(3, 0))
        render_gpu_wrap.columnconfigure(0, weight=1)
        render_gpu = self._chrome_combo(
            render_gpu_wrap, d['v_render_gpu'], [tr('gpu.auto_nvidia')],
        )
        render_gpu.grid(row=0, column=0, sticky="ew")
        render_gpu_refresh = ttk.Button(
            render_gpu_wrap, text=tr('gpu.refresh'), width=7,
            command=self._start_render_gpu_scan,
        )
        render_gpu_refresh.grid(row=0, column=1, sticky="e", padx=(4, 0))
        ttk.Label(host_group, text=tr("label.render_gpu")).grid(
            row=1, column=0, sticky="w", pady=(3, 0),
        )
        render_gpu_status = ttk.Label(
            host_group, text=tr('gpu.status.loading'), style='Hint.TLabel',
            wraplength=260, justify='left',
        )
        render_gpu_status.grid(row=2, column=1, sticky='ew', pady=(1, 3))

        submission = self._chrome_combo(
            host_group, d['v_submission'], list(HOST_SUBMISSION_CHOICES),
        )
        ttk.Label(host_group, text=tr("label.submission")).grid(row=3, column=0, sticky="w", pady=3)
        submission.grid(row=3, column=1, sticky="ew", pady=3)

        in_flight = self._chrome_spin(
            host_group, from_=1, to=3, textvariable=d['v_in_flight'], width=7,
            command=self._on_host_settings_change,
        )
        ttk.Label(host_group, text=tr("label.gpu_queue_frames")).grid(row=4, column=0, sticky="w", pady=3)
        in_flight.grid(row=4, column=1, sticky="w", pady=3)

        zero_fast = CheckToggle(
            host_group, tr("label.zero_guidance_fast"), d['v_zero_fast'],
            command=self._on_host_settings_change, ui=self._ui,
        )
        zero_fast.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 2))
        persistent = CheckToggle(
            host_group, tr("label.persistent_buffers"), d['v_persistent'],
            command=self._on_host_settings_change, ui=self._ui,
        )
        persistent.grid(row=6, column=0, columnspan=2, sticky="w", pady=2)
        fallback = CheckToggle(
            host_group, tr("label.auto_fallback"), d['v_fallback'],
            command=self._on_host_settings_change, ui=self._ui,
        )
        fallback.grid(row=7, column=0, columnspan=2, sticky="w", pady=2)
        self._theme_widgets.extend((zero_fast, persistent, fallback))
        d.update({
            'w_backend': backend,
            'w_render_gpu': render_gpu,
            'w_render_gpu_refresh': render_gpu_refresh,
            'w_render_gpu_status': render_gpu_status,
            'w_submission': submission,
            'w_zero_fast': zero_fast,
            'w_persistent': persistent,
            'w_in_flight': in_flight,
            'w_fallback': fallback,
        })
        backend.bind("<<ComboboxSelected>>", lambda e: self._on_host_settings_change())
        render_gpu.bind("<<ComboboxSelected>>", self._on_render_gpu_selected)
        submission.bind("<<ComboboxSelected>>", lambda e: self._on_host_settings_change())
        in_flight.bind("<FocusOut>", lambda e: self._on_host_settings_change())
        in_flight.bind("<Return>", lambda e: self._on_host_settings_change())
        self.root.after_idle(self._start_render_gpu_scan)
        self.root.after_idle(self._update_host_control_states)
        return d

    def _set_render_gpu_display(self, adapter_id):
        d = self._host_settings
        adapter_id = str(adapter_id or dlss_engine.RENDER_GPU_AUTO)
        label = next((name for name, value in d.get('render_gpu_choices', {}).items()
                      if value == adapter_id), None)
        if label is None:
            label = tr('gpu.saved_unavailable')
            d.setdefault('render_gpu_choices', {})[label] = adapter_id
            values = list(d['render_gpu_choices'])
            d['w_render_gpu'].config(values=values)
        d['render_gpu_selected_id'] = adapter_id
        d['v_render_gpu'].set(label)

    def _on_render_gpu_selected(self, _event=None):
        d = self._host_settings
        selected = d.get('render_gpu_choices', {}).get(d['v_render_gpu'].get())
        if selected is None or selected == d.get('render_gpu_selected_id'):
            return
        d['render_gpu_selected_id'] = selected
        self._on_host_settings_change()

    def _start_render_gpu_scan(self):
        if not hasattr(self, '_host_settings'):
            return
        if self._render_gpu_scan_thread is not None and self._render_gpu_scan_thread.is_alive():
            return
        d = self._host_settings
        d['w_render_gpu_status'].config(text=tr('gpu.status.loading'))
        d['w_render_gpu'].config(state='disabled')
        d['w_render_gpu_refresh'].config(state='disabled')
        generation = time.monotonic_ns()
        self._render_gpu_scan_generation = generation

        def worker():
            try:
                result = dlss_engine.available_render_adapters()
                error = ''
            except Exception as exc:
                result = []
                error = str(exc)
            self._render_gpu_scan_queue.put((generation, result, error))

        self._render_gpu_scan_thread = threading.Thread(
            target=worker, name='dlss-gpu-scan', daemon=True,
        )
        self._render_gpu_scan_thread.start()
        self._poll_render_gpu_scan()

    def _poll_render_gpu_scan(self):
        self._render_gpu_scan_after = None
        try:
            generation, adapters, error = self._render_gpu_scan_queue.get_nowait()
        except queue.Empty:
            thread = self._render_gpu_scan_thread
            if thread is not None and thread.is_alive():
                self._render_gpu_scan_after = self.root.after(50, self._poll_render_gpu_scan)
            return
        if generation != getattr(self, '_render_gpu_scan_generation', None):
            return
        self._render_gpu_scan_thread = None
        d = self._host_settings
        choices = {tr('gpu.auto_nvidia'): dlss_engine.RENDER_GPU_AUTO}
        labels = {}
        for adapter in adapters:
            memory = int(adapter.get('dedicated_video_memory', 0))
            base = adapter.get('name') or 'NVIDIA GPU'
            if memory:
                base += ' · ' + format_bytes(memory)
            count = labels.get(base, 0) + 1
            labels[base] = count
            label = base if count == 1 else f'{base} · #{count}'
            choices[label] = adapter['id']
        selected = d.get('render_gpu_selected_id', dlss_engine.RENDER_GPU_AUTO)
        if selected not in choices.values():
            choices[tr('gpu.saved_unavailable')] = selected
        d['render_gpu_choices'] = choices
        d['w_render_gpu'].config(values=list(choices))
        self._set_render_gpu_display(selected)
        if error:
            status = tr('gpu.status.failed', error=error)
        elif not adapters:
            status = tr('gpu.status.none')
        elif selected not in {adapter['id'] for adapter in adapters} and selected != dlss_engine.RENDER_GPU_AUTO:
            status = tr('gpu.saved_unavailable')
        else:
            status = tr('gpu.status.ready', count=len(adapters))
        d['render_gpu_scan_status'] = status
        d['w_render_gpu_status'].config(text=status)
        self._update_host_control_states()

    def _build_guidance_settings(self, parent):
        from dlss5tool.guidance_settings_ui import build_guidance_settings
        build_guidance_settings(self, parent)

    def _build_module_settings(self, parent):
        d, saved = self._host_settings, self._saved_settings
        group = ttk.Frame(parent, style='Panel.TFrame')
        group.pack(fill='x', padx=(6, 8), pady=4)
        group.columnconfigure(0, weight=1)
        setup_hint = ttk.Label(group, text=tr('mods.setup_hint'), style='Hint.TLabel', wraplength=300)
        setup_hint.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        group.bind('<Configure>', lambda e: setup_hint.configure(wraplength=max(160, e.width - 8)), add='+')
        summaries = ttk.Frame(group, style='Panel.TFrame')
        summaries.grid(row=1, column=0, sticky='ew')
        summaries.columnconfigure(1, weight=1)
        d['module_summaries'] = {}
        for row, name in enumerate(('runtime', 'component', 'flow', 'depth') if depth_enabled() else ('runtime', 'component', 'flow')):
            ttk.Label(summaries, text=tr('mods.summary.' + name)).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=3)
            label = ttk.Label(summaries, text='', style='Hint.TLabel')
            label.grid(row=row, column=1, sticky='w', pady=3)
            d['module_summaries'][name] = label
        actions = ttk.Frame(group, style='Panel.TFrame')
        actions.grid(row=2, column=0, sticky='ew', pady=(8, 4))
        ttk.Button(actions, text=tr('mods.open'), command=self._open_mods).pack(side='left')
        ttk.Button(actions, text=tr('mods.refresh'), command=self._refresh_mods).pack(side='left', padx=4)
        ttk.Button(actions, text=tr('mods.details'), command=self._show_module_details).pack(side='left')
        # Paths are a first-level section, independent of add-on status.
        editor = CollapsibleSection(
            self._export_inner, tr('mods.path_settings'), collapsed=True, ui=self._ui,
        )
        editor.pack(fill='x', padx=16, pady=(10, 8))
        self._theme_widgets.append(editor)
        self._module_editor = editor
        editor.body.columnconfigure(0, weight=1)
        runtime_combo = self._chrome_combo(editor.body, d['v_runtime'], [tr('mods.auto'), tr('mods.bundled')] + mod_paths.runtime_choices(saved))
        ttk.Label(editor.body, text=tr('mods.runtime')).grid(row=0, column=0, sticky='w', pady=(0, 4))
        runtime_combo.grid(row=1, column=0, sticky='ew')
        runtime_combo.bind('<<ComboboxSelected>>', lambda e: self._on_mod_settings_change())
        buttons = ttk.Frame(editor.body, style='Panel.TFrame')
        buttons.grid(row=2, column=0, sticky='ew', pady=5)
        runtime_button = ttk.Button(buttons, text=tr('mods.choose_runtime'), command=self._choose_runtime)
        runtime_button.pack(side='left')
        paths = ttk.Frame(editor.body, style='Panel.TFrame')
        paths.grid(row=3, column=0, sticky='ew')
        paths.columnconfigure(0, weight=1)
        self._module_path_defaults = {
            'guidance_flow_weights': 'models/raft_large_C_T_SKHT_V2-ff5fadd5.pth',
            'guidance_depth_weights': 'models/depth_anything_v2_{encoder}.pth',
            'mods_directory': 'mods',
        }
        path_vars, path_controls, path_entries = {}, [], {}
        for row, (key, default) in enumerate(self._module_path_defaults.items()):
            variable = tk.StringVar()
            variable.set(saved.get(key, '') or default)
            path_vars[key] = variable
            if key == 'guidance_depth_weights' and not depth_enabled():
                continue
            ttk.Label(paths, text=tr('mods.path.' + key)).grid(row=row * 2, column=0, columnspan=2, sticky='w', pady=(6, 3))
            entry = ChromeEntry(paths, ui=self._ui, textvariable=variable, width=18)
            self._theme_widgets.append(entry)
            path_entries[key] = entry
            entry.grid(row=row * 2 + 1, column=0, sticky='ew', padx=(0, 4))
            entry.bind('<Return>', lambda e: self._on_mod_settings_change())
            entry.bind('<FocusOut>', lambda e: self._on_mod_settings_change())
            browse = ttk.Button(paths, text=tr('common.browse'), width=7, command=lambda name=key: self._choose_module_path(name))
            browse.grid(row=row * 2 + 1, column=1, sticky='e')
            path_controls.extend((entry, browse))
        reset_button = ttk.Button(editor.body, text=tr('mods.reset_paths'), command=self._reset_module_paths)
        reset_button.grid(row=4, column=0, sticky='w', pady=6)
        path_controls.append(reset_button)
        footer = ttk.Frame(group, style='Panel.TFrame')
        footer.grid(row=3, column=0, sticky='ew', pady=(4, 0))
        footer.columnconfigure(0, weight=1)
        hint = ttk.Label(footer, text='', style='Hint.TLabel')
        hint.grid(row=0, column=0, sticky='w')
        d['w_mod_setup_hint'] = setup_hint
        d.update(w_runtime=runtime_combo, w_mod_hint=hint,
                 w_runtime_button=runtime_button, path_vars=path_vars, path_controls=path_controls, path_entries=path_entries)
        self._last_module_settings = self._collect_host_settings()

    def _open_module_settings(self):
        self.workspace_tabs.select(self._export_page)
        if self._modules_section.collapsed:
            self._modules_section.toggle()
        self.root.after_idle(lambda: self._export_canvas.yview_moveto(1.0))

    def _show_module_editor(self, name):
        self.workspace_tabs.select(self._export_page)
        if self._module_editor.collapsed:
            self._module_editor.toggle()
        key = {'component': 'mods_directory', 'flow': 'guidance_flow_weights', 'depth': 'guidance_depth_weights'}.get(name)
        widget = self._host_settings['path_entries'][key] if key else self._host_settings['w_runtime']
        def reveal():
            self.root.update_idletasks()
            inner = self._export_inner
            y = widget.winfo_rooty() - inner.winfo_rooty()
            self._export_canvas.yview_moveto(max(0.0, (y - 80) / max(inner.winfo_height(), 1)))
            widget.focus_set()
        self.root.after_idle(reveal)

    def _show_module_details(self):
        text = getattr(self, '_module_details_text', tr('mods.paths_hint'))
        if getattr(self, '_last_guidance_info', None):
            info = self._last_guidance_info
            text += '\n\n' + tr('guidance.last_device', device=info.get('device_name') or info['device'])
            if info.get('execution') in ('serial', 'raft_streams'):
                text += '\n' + tr('guidance.execution_confirmed', execution=tr('guidance.option.' + info['execution']))
        messagebox.showinfo(tr('mods.details'), text, parent=self.root)

    def _guidance_started(self, info, generation=None):
        # Even Tk.after() can wait for the Tk thread. This callback runs while
        # holding _live_lock, so it must not make ANY Tk calls.
        if generation is None:
            generation = self._guidance_generation
        self._guidance_events.put((generation, dict(info)))

    def _poll_guidance_events(self):
        self._guidance_events_after = None
        for _ in range(64):
            try:
                generation, info = self._guidance_events.get_nowait()
            except queue.Empty:
                break
            if generation != self._guidance_generation or self._module_reload_thread is not None:
                continue
            self._last_guidance_info = info
            if info.get('flow_backend'):
                grid = info.get('flow_grid')
                self.logln('flow_backend=' + info['flow_backend']
                           + (f' grid={grid}' if grid else ''))
            if info.get('flow_fallback_reason'):
                warning = tr('guidance.flow_fallback', reason=info['flow_fallback_reason'])
                self.logln(warning)
                self.set_status(warning)
            if info.get('analysis_parameters'):
                self.logln(tr('guidance.parameters_confirmed', parameters=info['analysis_parameters']))
            self.logln(tr('guidance.running', device=info.get('device_name') or info['device'],
                          precision=info.get('precision') or 'float32'))
            if info.get('execution') in ('serial', 'raft_streams'):
                self.logln(tr('guidance.execution_confirmed', execution=tr('guidance.option.' + info['execution'])))
            if info.get('cache_version') in ('raw_lru_v1', 'raw_lru_v2_shared'):
                self.logln(tr('guidance.cache_ready', budget=int(info.get('cache_limit_bytes', 0)) // 1048576))
            elif 'cache_version' in info:
                self.logln(tr('guidance.cache_unavailable'))
        self._guidance_events_after = self.root.after(100, self._poll_guidance_events)

    def _choose_module_path(self, key):
        if self._exporting or self._queue_running or self._diagnosing:
            return
        settings = self._collect_host_settings()
        options = dict(title=tr('mods.path.' + key), initialdir=str(mod_paths.mods_root(settings)))
        if key == 'mods_directory':
            path = filedialog.askdirectory(**options)
        else:
            extension = '*.pth'
            path = filedialog.askopenfilename(**options, filetypes=[('Module', extension), ('All', '*.*')])
        if path:
            self._host_settings['path_vars'][key].set(path)
            self._on_mod_settings_change()

    def _reset_module_paths(self):
        if self._exporting or self._queue_running or self._diagnosing:
            return
        for key, default in self._module_path_defaults.items():
            self._host_settings['path_vars'][key].set(default)
        self._host_settings['v_runtime'].set(tr('mods.auto'))
        self._host_settings['v_depth_encoder'].set(tr('guidance.option.auto'))
        self._refresh_mods()

    def _open_mods(self):
        try:
            directory = mod_paths.mods_root(self._collect_host_settings())
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))
        except OSError as exc:
            messagebox.showerror(tr('mods.title'), str(exc))

    def _choose_runtime(self):
        if self._exporting or self._queue_running:
            return
        path = filedialog.askopenfilename(title=tr('mods.choose_runtime'), initialdir=str(mod_paths.mods_root(self._collect_host_settings())), filetypes=[('DLSS runtime', '*.dll')])
        if path:
            self._host_settings['v_runtime'].set(path)
            self._on_mod_settings_change()

    def _refresh_mods(self):
        choices = [tr('mods.auto'), tr('mods.bundled')] + mod_paths.runtime_choices(self._collect_host_settings())
        current = self._host_settings['v_runtime'].get()
        if current not in choices:
            choices.append(current)
        self._host_settings['w_runtime'].config(values=choices)
        if not (self._exporting or self._queue_running or self._switching_backend or self._diagnosing):
            self._last_module_settings = None  # refresh also reloads a replaced file
            self._on_mod_settings_change()
        else:
            self._update_host_control_states()

    def _on_mod_settings_change(self):
        if self._exporting or self._queue_running or self._switching_backend or self._diagnosing:
            return
        settings = self._collect_host_settings()
        if settings == getattr(self, '_last_module_settings', None):
            return
        from dlss5tool.guidance_parameters import DISPLAY_KEYS
        previous_settings = getattr(self, '_last_module_settings', None)
        if previous_settings is not None and (
                {k: v for k, v in settings.items() if k not in DISPLAY_KEYS} ==
                {k: v for k, v in previous_settings.items() if k not in DISPLAY_KEYS}):
            self._last_module_settings = settings
            self.pause()
            self._guidance_preview_epoch += 1
            self._guidance_result = self._guidance_ready = self._guidance_presented = None
            self._guidance_display_signature = None
            self._schedule_settings_save()
            if self.video and self._guidance_context:
                self.display_view()
            return
        self._last_module_settings = settings
        self._guidance_preflight_error = ''
        self._guidance_preflight_info = None
        self._module_reload_error = ''
        self._module_pending_settings = settings
        # If media is already open in the analysis workspace, validate using the
        # real preview session and retain it. A disposable probe would load the
        # same weights a second time immediately afterwards.
        warm_preview = None
        if (settings.get('guidance_mode') and self.video
                and getattr(self, '_guidance_context', False)
                and not (self._video_color_info or {}).get('is_hdr')):
            warm_preview = (self.video, self._frame,
                            self._image_bgr.copy() if self._is_image else None,
                            self._collect_settings(), self._guidance_preview_epoch)
        # The requested mode is not active until the worker has loaded the
        # selected weights and successfully processed a frame pair. A remembered
        # startup choice stays persisted while this check is still pending.
        if settings.get('guidance_mode'):
            self._host_settings['v_guidance'].set(tr('guidance.mode.0'))
        self.pause()
        self._freeze_preview_cache(resume_ms=None)
        self._cancel_after('_live_debounce')
        self._cancel_after('_output_preview_after')
        self._guidance_generation += 1
        self._last_guidance_info = None
        self._cache_clear()
        self._last_dlss_frame = -1
        self._split_frame = -1
        self._split_dlss = None
        self._schedule_settings_save()
        self._refresh_status_chips()
        self._switching_backend = True
        self._update_host_control_states()
        self._update_action_labels()
        self._update_queue_action_states()
        self.set_status(tr('guidance.checking' if settings.get('guidance_mode') else 'guidance.switching'))
        if self.video and hasattr(self, 'canvas'):
            self._draw_work_status(tr('guidance.switching'))
        previous = self._play_dlss_thread
        result = queue.SimpleQueue()

        def retire():
            try:
                # Keep the old worker referenced until it really exits; its
                # in-flight IPC/GPU work has its own timeout. Never join on Tk.
                if previous is not None:
                    previous.join()
                self._close_live()
                if settings.get('guidance_mode'):
                    if warm_preview is not None:
                        result.put(self._warm_guidance_preview(warm_preview))
                        return
                    info = guidance_client.preflight(settings, require_shared_cache=True)
                    result.put({'info': info})
                    return
            except Exception as exc:
                if warm_preview is not None:
                    self._close_live()
                result.put(str(exc))
            else:
                result.put(None)

        self._module_reload_thread = threading.Thread(
            target=retire, daemon=True, name='dlss-module-reload',
        )
        try:
            self._module_reload_thread.start()
        except Exception as exc:
            result.put(str(exc))
        self._poll_module_reload(result)

    def _warm_guidance_preview(self, request):
        from dlss5tool.preview_comparison import guidance_input_pair
        source, frame, still, settings, epoch = request
        current, previous = guidance_input_pair(source, frame, still, settings)
        # First/reset frames have no motion. Still exercise temporal kernels so
        # activation retains the same two-frame guarantee as the small probe.
        with self._live_lock:
            live = self._ensure_live(current.shape[1], current.shape[0], settings=settings)
            if live is None:
                raise RuntimeError(self._live_error)
            if previous is None:
                live.guidance_preview(current, np.roll(current, 1, axis=1))
            images, reset = live.guidance_preview(current, previous)
            info = live.guidance_info
            if info.get('cache_version') != 'raw_lru_v2_shared':
                raise RuntimeError(tr('guidance.cache_unavailable'))
            images['_metrics'] = dict(live.guidance_metrics)
            self._last_dlss_frame = -1
        return {'info': info, 'preview': (source, frame, epoch,
                cv2.cvtColor(current, cv2.COLOR_RGBA2BGR), images, reset)}

    def _poll_module_reload(self, result):
        self._module_reload_after = None
        try:
            error = result.get_nowait()
        except queue.Empty:
            self._module_reload_after = self.root.after(
                PREVIEW_WORKER_POLL_MS, lambda: self._poll_module_reload(result),
            )
            return
        self._module_reload_thread = None
        pending = getattr(self, '_module_pending_settings', None)
        self._module_pending_settings = None
        self._startup_guidance_mode = 0
        warmed = None
        if isinstance(error, dict):
            warmed = error.get('preview')
            self._guidance_preflight_info = error['info']
            if error['info'].get('flow_fallback_reason'):
                pending['guidance_flow_backend'] = 'raft'
                self._host_settings['v_flow_backend'].set(tr('guidance.option.raft'))
                self.logln(tr('guidance.flow_fallback', reason=error['info']['flow_fallback_reason']))
            self._guidance_edit_mode = 0
            self._host_settings['v_guidance'].set(tr('guidance.mode.' + str(pending['guidance_mode'])))
            error = None
        elif error is not None and pending and pending.get('guidance_mode'):
            self._guidance_preflight_error = tr('guidance.check_failed', error=error)
            self._guidance_edit_mode = pending['guidance_mode']
        self._module_reload_error = str(error) if error is not None else ''
        # A retiring worker may have entered _ensure_live after the initial
        # invalidation. It is now gone: invalidate any last queued notification.
        self._guidance_generation += 1
        if warmed is not None:
            source, frame, epoch, original, images, reset = warmed
            if (source == self.video and frame == self._frame
                    and epoch == self._guidance_preview_epoch and self._guidance_context):
                self._guidance_result = (self._guidance_preview_key(), original, images, reset, '')
            self._last_guidance_info = self._guidance_preflight_info
            if self._live is not None:
                generation = self._guidance_generation
                self._live._on_guidance_ready = lambda info: self._guidance_started(info, generation)
        previous = self._play_dlss_thread
        if previous is None or not previous.is_alive():
            self._play_dlss_thread = None
            self._play_dlss_busy = False
        self._switching_backend = False
        self._update_host_control_states()
        self._update_action_labels()
        self._update_queue_action_states()
        self._schedule_settings_save()
        if self._close_after_module_reload:
            self._close_after_module_reload = False
            self._on_close()
            return
        if error is not None:
            self._last_module_settings = None  # allow an explicit retry
            self.set_status(tr('guidance.status.failed'))
            self.logln(tr('guidance.switch_failed', error=error))
            if hasattr(self, 'canvas'):
                self._draw_work_status(tr('guidance.status.failed'))
            if pending and pending.get('guidance_mode'):
                # Failed activation leaves base rendering available. Preserve
                # attempted parameters so the user can fix paths and retry.
                self._schedule_preview_cache_resume(0)
            return
        self.set_status(tr('guidance.checked' if pending and pending.get('guidance_mode') else 'guidance.changed'))
        self._schedule_preview_cache_resume(0)

    def _collect_host_settings(self):
        d = self._host_settings
        try:
            in_flight = int(d['v_in_flight'].get())
        except (ValueError, tk.TclError):
            in_flight = 2
        try:
            edge = max(128, min(1280, int(d['v_guidance_edge'].get())))
        except (KeyError, ValueError, tk.TclError):
            edge = 720
        # get defaults keeps older embedded/test callers compatible.
        def value(name, default):
            return d[name].get() if name in d else default
        def option(name, choices, default):
            current = value(name, default)
            return next((choice for choice in choices if current in (choice, tr('guidance.option.' + choice))), default)
        mode = public_mode(next((i for i in range(4) if value('v_guidance', '') == tr('guidance.mode.' + str(i))), 0))
        runtime = value('v_runtime', '')
        paths = {key: (variable.get().strip() if variable.get().strip() != self._module_path_defaults[key] else '')
                 for key, variable in d.get('path_vars', {}).items()}
        from dlss5tool.guidance_parameters import parameters
        analysis = parameters({'guidance_edge': edge})
        for key, variable in d.get('analysis_vars', {}).items():
            try:
                analysis[key] = parameters({key: variable.get()}, strict=True)[key]
            except ValueError:
                analysis[key] = d['analysis_valid'][key]
        analysis['guidance_depth_palette'] = option('v_depth_palette', ('gray', 'turbo'), 'gray')
        analysis['guidance_depth_invert'] = option('v_depth_invert', ('normal', 'inverted'), 'normal') == 'inverted'
        return {
            **analysis,
            'dlss_runtime': '' if runtime == tr('mods.auto') else ('__bundled__' if runtime == tr('mods.bundled') else runtime),
            'guidance_mode': mode,
            'guidance_flow_backend': option('v_flow_backend', ('raft', 'nvofa'), 'raft'),
            'guidance_flow_grid': next((n for n in (4, 2, 1)
                                        if value('v_flow_grid', '') == tr('guidance.option.grid_' + str(n))), 4),
            'guidance_edge': edge,
            'guidance_flow_direction': option('v_flow_direction', ('backward', 'forward_negated'), 'backward'),
            'guidance_depth_encoder': option('v_depth_encoder', ('auto', 'vits', 'vitb', 'vitl'), 'auto'),
            'guidance_device': option('v_guidance_device', ('auto', 'cuda', 'cpu'), 'auto'),
            'guidance_depth_profile': option('v_depth_profile', ('fp32', 'sdpa_fp16'), 'fp32'),
            'guidance_execution': option('v_guidance_execution', ('serial', 'raft_streams'), 'serial'),
            **paths,
            'host_backend': HOST_BACKEND_CHOICES.get(d['v_backend'].get(), 'auto'),
            'render_gpu': d.get('render_gpu_selected_id', dlss_engine.RENDER_GPU_AUTO),
            'host_submission': HOST_SUBMISSION_CHOICES.get(
                d['v_submission'].get(), 'merged'
            ),
            'host_zero_fast_path': bool(d['v_zero_fast'].get()) and not mode,
            'host_persistent_buffers': bool(d['v_persistent'].get()),
            'host_in_flight': max(1, min(3, in_flight)),
            'host_auto_fallback': bool(d['v_fallback'].get()),
        }

    def _update_module_summary(self, host):
        d = self._host_settings
        if 'w_mod_setup_hint' in d:
            custom = bool(host.get('mods_directory'))
            d['w_mod_setup_hint'].config(text=tr('mods.custom_hint' if custom else 'mods.setup_hint'))
        # File-presence summary only. It never imports torch or starts inference.
        candidates = mod_paths.guidance_candidates({**host, 'guidance_mode': 3 if depth_enabled() else 1})
        present = {key: os.path.isfile(path) for key, path in candidates.items()}
        component_error = ''
        try:
            mod_paths.enhancement_info(host)
        except (FileNotFoundError, ValueError) as exc:
            present['worker'] = False
            component_error = str(exc)
        runtime = mod_paths.runtime_info(host)
        found, missing = tr('mods.found'), tr('mods.missing')
        summary = {
            'runtime': tr('mods.runtime.' + ('bundled' if runtime['source'] == 'bundled' else 'external')) if os.path.isfile(runtime['path']) else missing,
            'component': found if present['worker'] else (tr('mods.incompatible') if os.path.isfile(candidates['worker']) else missing),
            'flow': tr('guidance.no_flow_weights') if host.get('guidance_flow_backend') == 'nvofa' else found if present.get('flow_weights') else missing,
            'depth': found if present.get('depth_weights') else missing,
        }
        for key, label in d.get('module_summaries', {}).items():
            if key == 'component' and present['worker']:
                build = mod_paths.component_build(host)
                if build != 'unknown':
                    summary[key] = tr('mods.build.' + build)
            label.config(text=summary[key])
        mode = host['guidance_mode']
        required = ['worker'] + (['flow_weights'] if mode in (1, 3) and host.get('guidance_flow_backend', 'raft') == 'raft' else []) + (['depth_weights'] if mode in (2, 3) else [])
        missing_required = [key for key in required if not present[key]] if mode else []
        status = tr('mods.status.off') if not mode else (tr('mods.status.missing') if missing_required else tr('mods.status.ready'))
        if runtime['ambiguous']:
            status = tr('mods.status.ambiguous')
        elif runtime['fallback']:
            status = tr('mods.status.fallback')
        d['w_mod_hint'].config(text=status)
        pending = getattr(self, '_module_pending_settings', None)
        if pending and pending.get('guidance_mode'):
            status = tr('guidance.checking')
        elif getattr(self, '_guidance_preflight_error', ''):
            status = self._guidance_preflight_error
        elif mode and getattr(self, '_guidance_preflight_info', None):
            reason = self._guidance_preflight_info.get('flow_fallback_reason')
            status = tr('guidance.flow_fallback', reason=reason) if reason else tr('guidance.checked')
        if getattr(self, '_module_reload_error', ''):
            status = self._module_reload_error
        # Keep the page geometry stable; detailed errors belong in Details.
        short_status = tr('guidance.status.off')
        if pending:
            short_status = tr('guidance.status.loading')
        elif getattr(self, '_guidance_preflight_error', '') or getattr(self, '_module_reload_error', ''):
            short_status = tr('guidance.status.failed')
        elif mode:
            short_status = tr('guidance.status.checked' if getattr(self, '_guidance_preflight_info', None)
                              else 'guidance.status.unchecked')
        if getattr(self, '_is_image', False) and mode == 1 and not pending:
            short_status = tr('guidance.status.still')
            status = tr('guidance.still_hint')
        d['w_guidance_status'].config(text=short_status)
        if 'guidance_status_tooltip' in d:
            d['guidance_status_tooltip'].text = status
        lines = [tr('mods.paths_hint'), '', tr('mods.detected_runtime', path=runtime['path'])]
        if component_error:
            lines.append(component_error)
        if runtime['ambiguous']:
            lines.append(tr('mods.ambiguous_runtime'))
        if runtime['fallback']:
            lines.append(tr('mods.path_fallback'))
        for key, path in candidates.items():
            lines.append(tr('mods.file_detail', label=tr('mods.file.' + key), status=found if present[key] else missing, path=path))
        lines.extend(['', status, tr('guidance.off_hint') if not mode else tr('guidance.ready_hint')])
        self._module_details_text = '\n'.join(lines)

    def _update_host_control_states(self):
        self._update_clear_cache_control()
        if not hasattr(self, "_host_settings"):
            return
        if 'w_mod_hint' in self._host_settings:
            host = self._collect_host_settings()
            busy = self._exporting or self._queue_running or self._switching_backend or self._diagnosing
            for key in ('w_guidance', 'w_runtime'):
                self._host_settings[key].config(state='disabled' if busy else 'readonly')
            self._host_settings['w_runtime_button'].config(state='disabled' if busy else 'normal')
            for widget in self._host_settings.get('path_controls', []):
                widget.config(state='disabled' if busy else 'normal')
            # A rejected activation is still OFF, but its configuration must
            # remain editable (e.g. select FP32/serial before selecting CPU).
            mode = host['guidance_mode'] or getattr(self, '_guidance_edit_mode', 0)
            enabled = {
                'mode': True, 'device': bool(mode), 'edge': bool(mode),
                'flow': mode in (1, 3), 'flow_backend': True, 'depth': mode in (2, 3),
                'profile': mode in (2, 3), 'execution': mode == 3,
                'palette': mode in (2, 3), 'invert': mode in (2, 3),
            }
            for key, widget in self._host_settings['guidance_controls'].items():
                if key.startswith('guidance_flow_'):
                    enabled[key] = mode in (1, 3)
                elif key.startswith('guidance_depth_'):
                    enabled[key] = mode in (2, 3)
                if key == 'guidance_flow_updates' and host.get('guidance_flow_backend') == 'nvofa':
                    enabled[key] = False
                if key == 'flow_grid':
                    enabled[key] = mode in (1, 3) and host.get('guidance_flow_backend') == 'nvofa'
                state = 'readonly' if isinstance(widget, ChromeCombobox) else 'normal'
                widget.config(state=state if enabled[key] and not busy else 'disabled')
            from dlss5tool.guidance_settings_ui import sync_flow_backend_controls
            sync_flow_backend_controls(self)
            if hasattr(self, '_guidance_canvas'):
                self.root.after_idle(self._sync_guidance_scrollregion)
            self._update_module_summary(host)
            self._update_guidance_export_controls()
        if self._exporting or self._queue_running or self._switching_backend or self._diagnosing:
            for name in (
                'w_backend', 'w_render_gpu', 'w_render_gpu_refresh', 'w_submission',
                'w_zero_fast', 'w_persistent', 'w_in_flight', 'w_fallback',
            ):
                self._host_settings[name].config(state="disabled")
            return
        host = self._collect_host_settings()
        self._host_settings['w_backend'].config(state="readonly")
        v2_enabled = host['host_backend'] != 'legacy'
        scanning = (
            self._render_gpu_scan_thread is not None
            and self._render_gpu_scan_thread.is_alive()
        )
        self._host_settings['w_render_gpu'].config(
            state="readonly" if v2_enabled and not scanning else "disabled"
        )
        self._host_settings['w_render_gpu_refresh'].config(
            state="normal" if v2_enabled and not scanning else "disabled"
        )
        if not v2_enabled:
            self._host_settings['w_render_gpu_status'].config(
                text=tr('gpu.status.legacy')
            )
        elif not scanning:
            adapter_name = (
                getattr(getattr(self, '_live', None), 'adapter_info', {}).get('name')
                if getattr(self, '_live', None) is not None
                and getattr(self._live, 'backend', None) == 'v2'
                else ''
            )
            self._host_settings['w_render_gpu_status'].config(text=(
                tr('gpu.status.active', name=adapter_name) if adapter_name
                else self._host_settings.get('render_gpu_scan_status', tr('gpu.status.loading'))
            ))
        self._host_settings['w_submission'].config(
            state="readonly" if v2_enabled else "disabled"
        )
        for name in ('w_zero_fast', 'w_persistent', 'w_fallback'):
            self._host_settings[name].config(
                state="normal" if v2_enabled and not (name == 'w_zero_fast' and host.get('guidance_mode')) else "disabled"
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
            self.set_status(tr("status.wait_queue_backend"))
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
            old_render_gpu = self._live.settings.get(
                'render_gpu', dlss_engine.RENDER_GPU_AUTO,
            )
            backend_changed = settings['host_backend'] != old_preference
            adapter_changed = settings.get(
                'render_gpu', dlss_engine.RENDER_GPU_AUTO,
            ) != old_render_gpu
            session_changed = backend_changed or adapter_changed
            if session_changed:
                self.pause()
                self._wait_play_dlss()
                self._switching_backend = True
                self._update_host_control_states()
                self.root.config(cursor="wait")
                self.set_status(tr(
                    "status.switching_render_gpu" if adapter_changed
                    else "status.switching_backend"
                ))
                self.root.update_idletasks()
            try:
                with self._live_lock:
                    self._live.update(settings)
            except Exception as ex:
                if session_changed:
                    self._switching_backend = False
                    self.root.config(cursor="")
                    if backend_changed:
                        self._host_settings['v_backend'].set(
                            HOST_BACKEND_NAMES.get(old_preference, HOST_BACKEND_NAMES['auto'])
                        )
                    if adapter_changed:
                        self._set_render_gpu_display(old_render_gpu)
                    self._update_host_control_states()
                self.logln("[DLSS 主机] 设置应用失败，继续使用原会话：" + str(ex))
                if adapter_changed:
                    self.set_status(tr("status.host_apply_failed", backend=self._live.backend))
                elif backend_changed:
                    self.set_status(tr("status.backend_switch_failed", backend=self._live.backend))
                else:
                    self.set_status(tr("status.host_apply_failed", backend=self._live.backend))
                self._schedule_settings_save()
                if session_changed:
                    messagebox.showerror(
                        tr("dialog.render_gpu_switch_failed" if adapter_changed
                           else "dialog.backend_switch_failed"),
                        tr("message.render_gpu_switch_failed" if adapter_changed
                           else "message.backend_switch_failed", error=ex),
                    )
                return
            if session_changed:
                self._switching_backend = False
                self.root.config(cursor="")
                self._update_host_control_states()
                if adapter_changed:
                    adapter = self._live.adapter_info
                    name = adapter.get('name') or tr('gpu.auto_nvidia')
                    self._host_settings['w_render_gpu_status'].config(
                        text=tr('gpu.status.active', name=name)
                    )
                    self.logln(f"[DLSS GPU] 已切换到 {name}")
                    self.set_status(tr("status.host_applied", backend=self._live.backend))
                elif self._live.backend != old_backend:
                    self.logln(
                        f"[DLSS 后端] 已热切换到 {self._live.backend}（GUI 无需重启）"
                    )
                    self.set_status(tr("status.backend_switched", backend=self._live.backend))
                else:
                    self.logln(
                        f"[DLSS 后端] 选择已更新；继续使用 {self._live.backend}"
                    )
                    self.set_status(tr("status.backend_applied", backend=self._live.backend))
            else:
                self.set_status(tr("status.host_applied", backend=self._live.backend))
            if self.video and self.view_var.get() in ("dlss", "compare"):
                self.root.after_idle(lambda: self.display_view(quality="full"))
        else:
            self.set_status(tr("status.host_saved"))
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
        result['ui_language'] = getattr(self, '_ui_language', i18n.get_language())
        result['guidance_cache_pool'] = self._ensure_shared_cache_pool().name
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
            'custom_output_width': max(2, min(MAX_TEXTURE_DIMENSION, integer(d['v_custom_width'], 1920))),
            'custom_output_height': max(2, min(MAX_TEXTURE_DIMENSION, integer(d['v_custom_height'], 1080))),
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
                parts.append(QUALITY_PROFILE_NAMES.get(export["quality_profile"], tr("quality.balanced")))
            else:
                parts.append(f"{export['video_bitrate_mbps']:g} Mbps")
            if super_resolution_enabled:
                parts.append(tr("queue.upscale", scale=super_resolution_scale))
        if not self.video:
            parts = [tr("hint.hdr_main10").rstrip("。").rstrip(".")]
        elif color.get("is_hdr") and not export["hdr_mode"]:
            parts.append(tr("hint.tonemap_sdr"))
        if self._is_image:
            parts.append(tr('guidance.still_hint'))
        elif color.get('is_hdr') and export['hdr_mode']:
            parts.append(tr('guidance.hdr_hint'))
        if super_resolution_enabled:
            status = super_resolution_runtime_status()
            if not status["available"]:
                parts.append(tr("hint.vsr_missing"))
        self._export_settings["w_hdr_hint"].config(text=" · ".join(parts))
        if hasattr(self, "_export_summary"):
            if export.get("rate_control") == "bitrate":
                current = f"{export.get('video_bitrate_mbps', 20):g} Mbps"
            else:
                current = QUALITY_PROFILE_NAMES.get(
                    export.get("quality_profile"), tr("quality.balanced"),
                )
            preset_name = NVENC_PRESET_NAMES.get(
                export.get("nvenc_preset"), tr("preset.p7"),
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
        if self.video and self.view_var.get() in ("dlss", "compare"):
            self.display_view(quality="fast")
            self._schedule_preview_cache_resume()

    def _on_export_settings_change(self):
        self._update_export_control_states()
        self._schedule_settings_save()

    def _collect_persisted_settings(self):
        d = self._settings
        export = self._collect_export_settings()
        host = self._collect_host_settings()
        if getattr(self, '_startup_guidance_mode', 0):
            host['guidance_mode'] = self._startup_guidance_mode
        return {
            "preview_view": self._normal_preview_view if self._guidance_context else self.view_var.get(),
            "preview_compare_layout": self.compare_layout.get(),
            "guidance_preview_view": self._guidance_view,
            "guidance_compare_target": self.compare_target.get(),
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
            "ui_modules_open": bool(getattr(self, '_modules_section', None) and not self._modules_section.collapsed),
            "ui_preview_open": bool(
                getattr(self, "_preview_section", None) and not self._preview_section.collapsed
            ),
            "ui_theme": self._ui_theme_name,
            "ui_language": getattr(
                self, "_preferred_ui_language", i18n.get_language()
            ),
            "inspector_width": int(getattr(self, "_inspector_width", 360)),
            "preview_detached": bool(self._detached_preview_window),
            "preview_window_geometry": self._detached_geometry_for_save(),
            "queue_output_dir": (
                self.queue_output_dir_var.get().strip()
                if hasattr(self, "queue_output_dir_var") else ""
            ),
            **self._collect_preview_settings(),
            **host,
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
            self.logln(tr("log.settings_save_failed", error=ex))

    def _on_panels_toggle(self):
        try:
            self.root.focus_set()
        except Exception:
            pass
        if hasattr(self, "_preview_canvas"):
            self.root.after_idle(self._sync_preview_scrollregion)
        if hasattr(self, "_export_canvas"):
            self.root.after_idle(self._sync_export_scrollregion)
        if hasattr(self, '_guidance_canvas'):
            self.root.after_idle(self._sync_guidance_scrollregion)
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
        if getattr(self, '_module_reload_thread', None) is not None:
            self._close_after_module_reload = True
            self.set_status(tr('guidance.closing'))
            return
        if self._diagnosing:
            messagebox.showinfo(
                tr("dialog.diagnosing"), tr("message.wait_diagnostics_close")
            )
            return
        if self._exporting or self._queue_running:
            messagebox.showinfo(
                tr("dialog.exporting"), tr("message.wait_export_close"),
            )
            return
        if self._update_downloading:
            if not messagebox.askyesno(
                tr("dialog.downloading_update"), tr("message.cancel_download_exit"),
            ):
                return
            self._update_cancel_event.set()
        self._cancel_after("_settings_save_after")
        self._cancel_after("_live_debounce")
        self._cancel_after("_output_preview_after")
        self._cancel_after("_scrub_after")
        self._cancel_after("_resize_after")
        self._cancel_after("_preview_cache_resume_after")
        self._cancel_after('_guidance_events_after')
        self._cancel_after('_clear_preview_after')
        self._cancel_after('_module_reload_after')
        self._cancel_after('_guidance_preview_after')
        self._cancel_after('_render_gpu_scan_after')
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
        if getattr(self, '_shared_cache_pool', None):
            self._shared_cache_pool.close()
            self._shared_cache_pool = None
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
            s.get('host_backend'), s.get('render_gpu'), s.get('host_submission'),
            s.get('host_zero_fast_path'), s.get('host_persistent_buffers'),
            s.get('host_in_flight'),
            s.get('dlss_runtime'), guidance_client.contract(s),
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
                        generation = self._guidance_generation
                        self._live = ProcessLive(w, h, settings, _on_guidance_ready=
                            lambda info: self._guidance_started(info, generation))
                    self._live.update(settings)
                    self._live_w, self._live_h = w, h
                    self._last_dlss_frame = -1
                    adapter_name = self._live.adapter_info.get('name')
                    if adapter_name and threading.current_thread() is threading.main_thread():
                        self._host_settings['w_render_gpu_status'].config(
                            text=tr('gpu.status.active', name=adapter_name)
                        )
                        self.logln(f"[DLSS GPU] {adapter_name}")
                else:
                    self._live.update(settings)
                return self._live
            except Exception as ex:
                raw_error = str(ex)
                guidance = _dlss_runtime_guidance(raw_error)
                display_error = (
                    raw_error + "\n\n" + guidance
                    if guidance and guidance not in raw_error else raw_error
                )
                self._live_error = display_error
                if threading.current_thread() is threading.main_thread():
                    self.logln("[DLSS] " + display_error)
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
            suffix = tr("status.precise_upscale_suffix") if self._super_resolution_scale() > 1 else ""
            self.set_status(tr("status.realtime_original", width=width, height=height, suffix=suffix))
        else:
            suffix = (
                tr("status.precise_upscale_action") if self._super_resolution_scale() > 1
                else tr("status.restore_original_action")
            )
            self.set_status(tr("status.realtime_proxy", width=width, height=height, suffix=suffix))

    @staticmethod
    def _cache_key(frame, size):
        try:
            width, height = size
            return int(frame), int(width), int(height)
        except (TypeError, ValueError):
            return None

    def _live_dlss_image(self, frame, source_bgr=None, settings=None, target_size=None):
        if getattr(self, '_module_reload_thread', None) is not None:
            return None
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
        if view == "original":
            original = self._source_cache_get(frame)
            return original if original is not None else self._read_frame(frame)
        if view == "dlss":
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
            pool = getattr(self, '_shared_cache_pool', None)
            if pool:
                with pool.locked():
                    return self._cache_store_accounted(frame, sk, bgr, key)
            return self._cache_store_accounted(frame, sk, bgr, key)

    def _cache_store_accounted(self, frame, sk, bgr, key):
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
            pool = getattr(self, '_shared_cache_pool', None)
            if pool:
                with pool.locked():return self._source_cache_store_accounted(frame,bgr)
            return self._source_cache_store_accounted(frame,bgr)

    def _source_cache_store_accounted(self,frame,bgr):
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
        return max(int(self._available_frame_cache_bytes() // pair_bytes), 1)

    def _prerender_target_frames(self):
        capacity = self._cache_capacity_frames()
        reserve = PREVIEW_QUEUE_SIZE if capacity > PREVIEW_QUEUE_SIZE else 0
        return max(self._buffer_target_frames(), capacity - reserve)

    def _evict_preview_cache_locked(self):
        pool = getattr(self, '_shared_cache_pool', None)
        if pool:
            with pool.locked():
                self._evict_preview_cache_accounted(pool.allowance_locked(respect_demand=True))
                self._publish_frame_cache_locked()
        else:self._evict_preview_cache_accounted(self._preview_cache_bytes())

    def _evict_preview_cache_accounted(self, budget):
        budget = max(budget, 0)
        playhead = int(self._frame)
        protected_end = playhead + self._prerender_target_frames() - 1

        def total_bytes():
            return self._dlss_cache_bytes + self._source_cache_bytes

        while total_bytes() > budget:
            # No fixed raw/frame split. A denied frame can request enough space
            # for the next source/result pair; idle worker releases LRU entries.
            source_w, source_h = self._source_size()
            pw, ph = self._active_preview_size or self._playback_preview_size()
            pair_bytes = (source_w*source_h+pw*ph)*3
            self._frame_cache_pending_bytes = pair_bytes if pair_bytes <= self._preview_cache_bytes() else 0
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
                if self._live_cache and self._live_cache[0] == key:self._live_cache = None
                if self._last_shown_dlss and self._last_shown_dlss[0] == key:self._last_shown_dlss = None
            else:
                self._source_frame_cache.pop(key, None)
                self._source_cache_bytes -= size
        if total_bytes() >= getattr(self,'_frame_cache_pending_bytes',0):
            self._frame_cache_pending_bytes = 0

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
            self._frame_cache_pending_bytes = 0
            pool = getattr(self,'_shared_cache_pool',None)
            if pool:
                with pool.locked():self._publish_frame_cache_locked()

    def _can_clear_preview_cache(self):
        return bool(getattr(self, 'video', None)) and not any(getattr(self, name, False) for name in (
            '_exporting', '_queue_running', '_diagnosing', '_switching_backend',
            '_module_reload_thread', '_clear_preview_pending',
        ))

    def _update_clear_cache_control(self):
        button = getattr(self, 'clear_cache_btn', None)
        if button is not None:
            self._set_ttk_enabled(button, self._can_clear_preview_cache())

    def clear_preview_cache(self):
        """Retire in-flight work, then regenerate without changing media or settings."""
        if not self._can_clear_preview_cache():
            return
        self.pause()
        self._freeze_preview_cache(resume_ms=None)
        self._cancel_after('_live_debounce')
        self._cancel_after('_output_preview_after')
        self._clear_preview_pending = True
        self._guidance_preview_epoch += 1
        self._guidance_result = self._guidance_ready = self._guidance_presented = None
        self._guidance_display_signature = None
        self._update_clear_cache_control()
        self.set_status(tr('status.clearing_preview_cache'))
        self._clear_preview_after = self.root.after(0, self._poll_clear_preview_cache)

    def _poll_clear_preview_cache(self):
        self._clear_preview_after = None
        if not getattr(self, '_clear_preview_pending', False):
            return
        thread = getattr(self, '_play_dlss_thread', None)
        if ((thread is not None and thread.is_alive()) or any(
                getattr(self, name, False) for name in
                ('_module_reload_thread', '_exporting', '_queue_running', '_switching_backend', '_diagnosing'))):
            self._clear_preview_after = self.root.after(
                PREVIEW_WORKER_POLL_MS, self._poll_clear_preview_cache,
            )
            return
        self._play_dlss_thread = None
        self._play_dlss_busy = False
        self._preview_cache_frozen = False
        self._finish_clear_preview_cache()
        if self.video:
            self.display_view(quality="fast")
            if not getattr(self, '_guidance_context', False):
                self._schedule_full_preview()

    def _finish_clear_preview_cache(self):
        # Called only after the worker has retired: it can no longer refill the
        # frame caches or overwrite the temporal reset marker below.
        pool = getattr(self, '_shared_cache_pool', None)
        if pool:
            pool.invalidate_guidance()
        self._cache_clear()
        self._last_dlss_frame = -1
        self._play_orig = None
        self._split_frame = -1
        self._split_orig = self._split_dlss = None
        self._active_preview_size = None
        self._clear_preview_pending = False
        self.timeline.set_cache_ranges([], [])
        self._update_clear_cache_control()
        message = tr('status.preview_cache_cleared')
        self.logln(message)
        self.set_status(message)

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
            cx, title_y,
            text=(tr("status.release_to_import") if self._drop_hover
                  else tr("status.drop_media")),
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
        if getattr(self, "_exporting", False) or getattr(self, '_clear_preview_pending', False):
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
        if self._guidance_context:
            self._display_guidance(frame)
            return
        if self._hold_original:
            view = "original"
        missing = view in ("dlss", "compare") and self._cached_dlss(frame) is None
        # Full-resolution still images must use the same asynchronous queue as
        # videos. Never start an IPC/GPU wait while painting the Tk canvas.
        fast = missing and (quality == "fast" or self._is_image)
        if missing and self._is_image and quality != "fast" and not self._pre_rendering:
            self._schedule_full_preview()
        self._dlss_pending = bool(fast)
        if view == "compare":
            self._draw_split(frame, cw, ch, fast=fast)
            return
        if fast:
            img = self._read_frame(frame)
            badge = tr("status.previewing_original")
        else:
            img = self.load_view_img(view, frame)
            badge = tr("status.original_held") if self._hold_original and self.view_var.get() != "original" else None
        if img is None:
            self.canvas.delete("all")
            view_name = VIEWS.get(view, view)
            msg = (
                tr("status.view_read_failed", view=view_name, frame=frame)
                if view == "original"
                else tr("status.dlss_frame_failed", frame=frame)
            )
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

    def _draw_work_status(self, text):
        """A fixed overlay, independent of the layout and bottom status bar."""
        self.canvas.delete('work_status')
        if not text:
            return
        cw, ch = self._canvas_size()
        item = self.canvas.create_text(
            cw - 16, ch - 16, text=text, anchor='se', width=max(100, cw - 48),
            fill=self._ui_color('text', '#eeeeee'), font=ui_theme.UI_FONT_SMALL,
            tags='work_status',
        )
        bounds = self.canvas.bbox(item)
        if bounds:
            x0, y0, x1, y1 = bounds
            background = self.canvas.create_rectangle(
                x0 - 8, y0 - 5, x1 + 8, y1 + 5,
                fill=self._ui_color('panel', '#161d24'), outline='', tags='work_status',
            )
            self.canvas.tag_lower(background, item)

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
                    cw // 2, ch // 2,
                    text=tr("status.frame_read_failed", frame=frame),
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
        if getattr(self, 'compare_layout', None) is not None and self.compare_layout.get() == 'side' and not self._hold_original:
            self._blit_side_by_side(cw, ch)
            return
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
        show_divider = self._active_compare() and not self._hold_original
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
                ox + 10, oy + 14, tr("view.original"), anchor="w",
                font=ui_theme.UI_FONT_SMALL,
            )
            self._canvas_shadow_text(
                ox + nw - 10, oy + 14,
                tr("status.generating_dlss") if self._dlss_pending else self._compare_label(),
                anchor="e",
                font=ui_theme.UI_FONT_SMALL,
            )
        elif self._hold_original:
            self._canvas_shadow_text(
                ox + 10, oy + 14, tr("status.original_held"), anchor="w",
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
        if not self._wipe_compare() or self._hold_original:
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
        if self._active_compare() and getattr(self, 'compare_layout', None) is not None and self.compare_layout.get() == 'side':
            slot = max(1, (self._canvas_size()[0] - 12) // 2)
            if x >= slot + 12:
                x -= slot + 12
        return ox <= x <= ox + nw and oy <= y <= oy + nh

    def _refresh_viewport_display(self):
        if not self.video or self._exporting:
            return
        cw, ch = self._canvas_size()
        if getattr(self, '_guidance_context', False):
            self._display_guidance(self._frame)
            return
        if (
            self.view_var.get() == "compare"
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
            (bx1 + bx2) / 2, (by1 + by2) / 2, text=tr("status.choose_file"),
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
        if self._wipe_compare() and (self._near_split(event.x) or shift):
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
        kind = "compare" if self._wipe_compare() else "click"
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
            if not moved and self._wipe_compare():
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
        if self.video and self._wipe_compare() and self._near_split(event.x):
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
                text=tr("action.exit_fullscreen") if fullscreen else tr("action.fullscreen"),
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
            and self.view_var.get() in ("dlss", "compare")
            and not self._hold_original
        ):
            self._display_precise_preview()
        else:
            self.display_view(quality=quality)
        if (
            quality == "fast"
            and self.view_var.get() in ("dlss", "compare")
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
        wants_dlss = self.view_var.get() in ("dlss", "compare") and not self._hold_original
        precise_size = self._precise_preview_size()
        if (
            wants_dlss
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
        if not self._is_image and self._start_paused_prerender():
            self._update_preview_timeline_and_status(force=True)

    def _display_precise_preview(self):
        source_size = self._source_size()
        precise_size = self._precise_preview_size()
        sr_scale = self._super_resolution_scale()
        wants_dlss = self.view_var.get() in ("dlss", "compare") and not self._hold_original
        if wants_dlss:
            # Playback keeps a canvas-sized split image; invalidate it so compare
            # mode uses the full-resolution cache (or generates it) after pausing.
            self._split_frame = -1
            self._split_dlss = None
        if wants_dlss and self._cached_dlss(self._frame, precise_size) is None:
            if sr_scale > 1:
                self.set_status(tr("status.generating_upscale", scale=sr_scale))
            else:
                self.set_status(tr("status.generating_original"))
        self.display_view(quality="full")
        if wants_dlss and self._cached_dlss(self._frame, precise_size) is not None:
            width, height = precise_size
            key = "status.precise_upscale" if sr_scale > 1 else "status.precise_preview"
            self.set_status(tr(key, scale=sr_scale, width=width, height=height))

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
        self._update_clear_cache_control()
        zoom = max(PREVIEW_ZOOM_MIN, min(self._preview_zoom, PREVIEW_ZOOM_MAX))
        fitted = abs(zoom - 1.0) < 0.005
        text = tr("action.fit") if fitted else f"{int(round(zoom * 100))}%"
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
        side = self._active_compare() and self.compare_layout.get() == 'side'
        if anchor is not None and side:
            slot = max(1, (self._canvas_size()[0] - 12) // 2)
            if anchor[0] >= slot + 12:
                anchor = (anchor[0] - slot - 12, anchor[1])
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
                if side:
                    cw = max(1, (cw - 12) // 2)
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
        if hasattr(self, 'preview_selector'):
            if not self._guidance_context:
                self.preview_selector.set(self.view_var.get())
            self._sync_comparison_controls()
        self._schedule_settings_save()
        if not self.video:
            self._draw_empty()
            return
        if self.view_var.get() == "original":
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
        if not self.video or self.view_var.get() not in ("dlss", "compare"):
            return
        if self.playing:
            self._present_play_frame(self._frame)
        else:
            self.display_view(quality="full")

    def _refresh_dlss(self):
        self._live_debounce = None
        if getattr(self, '_module_reload_thread', None) is not None:
            return
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
        if self.view_var.get() in ("dlss", "compare"):
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
        self.root.bind_all("<Key-1>", lambda e: self._on_view_hotkey("original"))
        self.root.bind_all("<Key-2>", lambda e: self._on_view_hotkey("dlss"))
        self.root.bind_all("<Key-3>", lambda e: self._on_view_hotkey("compare"))
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
        if self._guidance_context:
            from dlss5tool.guidance_public import public_targets
            self.preview_selector.set({'dlss': public_targets()[0]}.get(view, view))
            self._on_preview_selection()
            return 'break'
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
                self.play_btn.config(text=tr("action.stop_waiting"), icon="stop")
            elif playing:
                self.play_btn.config(text=tr("action.pause"), icon="pause")
            else:
                self.play_btn.config(text=tr("action.play"), icon="play")
        except Exception:
            pass

    def toggle_mute(self):
        self._focus_preview_host()
        self._audio.set_muted(not self._audio.muted)
        try:
            muted = self._audio.muted
            self.mute_btn.config(
                text=tr("action.muted") if muted else tr("action.audio"),
                icon="volume-off" if muted else "volume",
            )
        except Exception:
            pass
        if self.playing and not self._buffering and not self._audio.muted:
            self._audio.play(self._frame, self.fps)

    def play(self):
        if getattr(self, '_switching_backend', False) or getattr(self, '_clear_preview_pending', False):
            return
        if not self.video:
            messagebox.showwarning(tr("dialog.hint"), tr("message.import_first"))
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
        if self._guidance_context and self._guidance_view != 'original':
            self._guidance_display_time = None
            self._audio.pause()
            self._set_play_btn(True)
            self._play_tick()
            return
        if view in ("dlss", "compare"):
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
            and getattr(self, '_module_reload_thread', None) is None
            and not getattr(self, '_clear_preview_pending', False)
            and not getattr(self, "_preview_cache_frozen", False)
            and self.video and not self._exporting
            and self.view_var.get() in ("dlss", "compare")
        )

    def _schedule_preview_cache_resume(self, delay=PREVIEW_INTERACTION_IDLE_MS):
        self._cancel_after("_preview_cache_resume_after")
        if getattr(self, '_module_reload_thread', None) is not None:
            return
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
        if (getattr(self, '_module_reload_thread', None) is not None
                or getattr(self, '_clear_preview_pending', False)):
            return
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
        if getattr(self, '_guidance_context', False):
            self.display_view()
            return
        if hasattr(self, 'canvas'):
            self._draw_work_status('')
        if self.playing:
            if (
                self.view_var.get() in ("dlss", "compare")
                and self._preview_frame_queue is None
            ):
                self._start_strict_preview_buffering()
            return
        if self.view_var.get() in ("dlss", "compare") and not self._hold_original:
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
            or self.playing or not self.video or self._exporting
        ):
            return False
        if self.view_var.get() not in ("dlss", "compare") or self._hold_original:
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
        if self._handle_preview_worker_error():
            return
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
            or self._hold_original or self.view_var.get() not in ("dlss", "compare")
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
        if not self.video or self.view_var.get() not in ("dlss", "compare"):
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
            self._evict_preview_cache_locked()
            rendered_frames = {
                key[0] for key, item in self._dlss_frame_cache.items()
                if key[1:] == size and item[0] == sk
            }
            queued_frames = set(self._source_frame_cache) - rendered_frames
            used_bytes = self._dlss_cache_bytes + self._source_cache_bytes
        pool = getattr(self,'_shared_cache_pool',None)
        if pool:used_bytes = pool.snapshot()['used_bytes']
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
            text = tr(
                "status.buffering", frame=self._frame,
                available=available, required=required,
            )
        elif self._pre_rendering and not self.playing:
            text = tr("status.prerendering", rendered=rendered_ahead, total=target_total)
        elif not self.playing:
            text = tr("status.prerendered", rendered=rendered_ahead, total=target_total)
        else:
            text = tr("status.strict_preview", frames=len(rendered_frames))
        if rate > 0:
            text += f" · {rate:.1f} fps"
            if rate + 0.5 < max(float(self.fps), 1.0):
                text += tr("status.slower_than_video", fps=self.fps)
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

    def _handle_preview_worker_error(self):
        error = getattr(self, '_preview_worker_error', None)
        if not error:
            return False
        self._preview_worker_error = None
        self.pause()
        self._freeze_preview_cache(resume_ms=None)
        self._dlss_pending = False
        self.logln('[预览] DLSS 缓存失败: ' + str(error))
        self.set_status(tr('status.preview_failed'))
        if hasattr(self, 'canvas'):
            self._draw_work_status(tr('status.preview_failed'))
        return True

    def _play_tick(self):
        if not self.playing:
            return
        if getattr(self, '_guidance_context', False) and self._guidance_view != 'original':
            self._guidance_play_tick()
            return
        if self._handle_preview_worker_error():
            return
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
                self.set_status(tr("status.playback_finished"))
                return
            if target != self._frame or self._hold_original:
                view = "original" if self._hold_original else self.view_var.get()
                if view in ("dlss", "compare"):
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
        if getattr(self, '_guidance_context', False):
            self._display_guidance(frame, orig)
            return
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
        view = "original" if self._hold_original else self.view_var.get()
        if view == "original" or self._hold_original:
            badge = tr("status.original_held") if self._hold_original and self.view_var.get() != "original" else None
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
        if view == "compare":
            self._blit_play_split(orig, preview, cw, ch, pending=not exact_frame)
            return
        img = preview if preview is not None else self._pending_preview_image(orig)
        self._dlss_pending = not exact_frame
        badge = tr("status.rendering_current") if not exact_frame else None
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
        if self.view_var.get() == "original":
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
                if stop.is_set() or gen != self._prefetch_gen:
                    break
                if live is None:
                    with self._live_lock:
                        live_settings = (_large_image_host_settings(target_w, target_h, settings)
                                         if getattr(self, '_source_kind', None) == 'image' else settings)
                        live = self._ensure_live(target_w, target_h, live_settings)
                        if live is None:
                            raise RuntimeError(getattr(self, "_live_error", "DLSS 主机不可用"))
                if stop.is_set() or gen != self._prefetch_gen:
                    break
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
        if thread is not None and thread.is_alive():
            self._play_dlss_busy = True
            return False
        self._play_dlss_busy = False
        self._play_dlss_thread = None
        return True

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
            if self.view_var.get() in ("dlss", "compare") and not self._hold_original:
                self._schedule_full_preview()

    # ---------- import ----------
    def import_video(self):
        self.import_media()

    def import_media(self):
        if getattr(self, '_switching_backend', False):
            return
        if self._diagnosing:
            messagebox.showinfo(
                tr("dialog.diagnosing"), tr("message.wait_diagnostics_import")
            )
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
                messagebox.showinfo(
                    tr("dialog.check_updates"), tr("message.update_in_progress")
                )
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
                messagebox.showwarning(tr("dialog.update_failed"), error)
            return
        comparison = updater.compare_versions(release.tag, APP_VERSION)
        if comparison is None:
            if manual:
                messagebox.showwarning(
                    tr("dialog.update_failed"),
                    tr("message.version_compare_failed", current=APP_VERSION, latest=release.tag),
                )
            return
        if comparison <= 0:
            if manual:
                self.logln(f"[更新] GitHub 最新正式版本为 {release.tag}")
                messagebox.showinfo(
                    tr("dialog.up_to_date"),
                    tr("message.up_to_date", current=APP_VERSION, latest=release.tag),
                )
            return
        self._prompt_for_update(release)

    def _prompt_for_update(self, release):
        try:
            edition = delta_update.installed_edition(paths.app_root(), self._collect_host_settings())
            delta_asset = delta_update.select_asset(release, APP_VERSION, edition)
            if (delta_asset is not None and getattr(sys, 'frozen', False)
                    and os.name == 'nt' and (paths.app_root() / delta_update.HELPER).is_file()):
                if messagebox.askyesno(
                    tr('dialog.new_version'),
                    tr('update.delta_offer', current=APP_VERSION, latest=release.tag,
                       edition=tr('update.edition_' + edition), size=updater.format_size(delta_asset.size)),
                ):
                    self._start_delta_download(release, delta_asset, edition)
                return
            if edition == 'full':
                self._delta_fallback(release, tr('update.no_delta'))
                return
        except Exception as error:
            self._delta_fallback(release, str(error))
            return
        asset = updater.select_portable_asset(release)
        notes = release.body.strip()
        if len(notes) > 900:
            notes = notes[:897].rstrip() + "…"
        notes_text = tr("message.release_notes", notes=notes) if notes else ""
        if asset is None:
            self.logln(f"[更新] 发现 {release.tag}，但未找到完整 win64 便携包")
            if messagebox.askyesno(
                tr("dialog.new_version"),
                tr(
                    "message.update_no_asset", current=APP_VERSION,
                    latest=release.tag, notes=notes_text,
                ),
            ):
                self._open_release_page(release.page_url)
            return
        size_text = updater.format_size(asset.size) if asset.size else tr("common.unknown_size")
        self.logln(f"[更新] 发现新版本 {release.tag}：{asset.name}（{size_text}）")
        if messagebox.askyesno(
            tr("dialog.new_version"),
            tr(
                "message.update_download", current=APP_VERSION,
                latest=release.tag, notes=notes_text,
                asset=asset.name, size=size_text,
            ),
        ):
            self._start_update_download(release, asset)

    def _delta_fallback(self, release, error):
        self.logln('[更新] ' + str(error))
        if messagebox.askyesno(tr('dialog.new_version'), tr('update.fallback', error=error)):
            self._open_release_page(release.page_url)

    def _start_delta_download(self, release, asset, edition):
        root = paths.app_root()
        try:
            if delta_update.transaction_path(root).exists():
                state = delta_update.status(root) or {}
                if state.get('phase') == 'ready' and state.get('target') == release.tag:
                    self._offer_delta_install(release)
                    return
                if not messagebox.askyesno(tr('dialog.new_version'), tr('update.discard', path=str(delta_update.transaction_path(root)))):
                    return
                delta_update.discard_transaction(root)
        except Exception as error:
            self._delta_fallback(release, str(error))
            return
        self._update_downloading = True
        self._update_progress_percent = 0
        self._update_cancel_event.clear()
        self._update_action_labels()
        self.logln(tr('update.preparing'))
        events = queue.Queue()

        def worker():
            try:
                delta_update.prepare(
                    root, asset, APP_VERSION, release.tag, edition,
                    progress=lambda done, total: events.put(('progress', done, total)),
                    cancelled=self._update_cancel_event.is_set,
                )
                events.put(('complete', None, None))
            except Exception as error:
                events.put(('complete', str(error), None))

        def poll():
            complete = None
            while True:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                if event[0] == 'progress':
                    self._update_progress_percent = min(100, int(event[1] * 100 / event[2]))
                    self._update_action_labels()
                    if event[1] == event[2]:
                        self.logln(tr('update.verifying'))
                else:
                    complete = event
            if complete is None:
                self.root.after(100, poll)
                return
            self._update_downloading = False
            self._update_progress_percent = None
            self._update_thread = None
            self._update_action_labels()
            if complete[1]:
                if self._update_cancel_event.is_set():
                    self.logln(tr('update.cancelled'))
                else:
                    self._delta_fallback(release, complete[1])
                return
            self._offer_delta_install(release)

        self._update_thread = threading.Thread(target=worker, name='dlss5-file-update', daemon=True)
        self._update_thread.start()
        self.root.after(100, poll)

    def _offer_delta_install(self, release):
        if (self._exporting or self._queue_running or self._diagnosing or self._switching_backend
                or getattr(self, '_module_reload_thread', None) is not None):
            messagebox.showinfo(tr('dialog.busy'), tr('update.install_busy'))
            return
        if not messagebox.askyesno(tr('dialog.download_complete'), tr('update.install')):
            return
        try:
            process = update_helper.launch(paths.app_root(), os.getpid(), i18n.get_language())
        except Exception as error:
            self._delta_fallback(release, str(error))
            return
        # Do not exit before the helper has acknowledged startup. Keep Tk responsive.
        self._update_downloading = True
        self._update_action_labels()
        deadline = time.monotonic() + 30

        def await_helper():
            try:
                state = delta_update.status(paths.app_root()) or {}
                if state.get('phase') == 'waiting' and process.poll() is None:
                    self._update_downloading = False
                    self._update_action_labels()
                    self._on_close()
                    return
                if process.poll() is not None or time.monotonic() > deadline:
                    raise delta_update.DeltaError(tr('update.helper_failed'))
            except Exception as error:
                self._update_downloading = False
                self._update_action_labels()
                self._delta_fallback(release, str(error))
                return
            self.root.after(100, await_helper)

        self.root.after(100, await_helper)

    def _start_update_download(self, release, asset):
        try:
            directory = updater.default_download_directory()
            os.makedirs(directory, exist_ok=True)
            destination = updater.unique_download_path(directory, asset.name)
        except OSError as ex:
            messagebox.showerror(
                tr("dialog.download_failed"), tr("message.download_folder_failed", error=ex)
            )
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
                    messagebox.showerror(tr("dialog.download_failed"), error)
                return
            self.logln(f"[更新] {release.tag} 已下载并校验完成: {path}")
            if messagebox.askyesno(
                tr("dialog.download_complete"),
                tr("message.download_complete", path=path),
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
            messagebox.showinfo(tr("dialog.busy"), tr("message.wait_diagnostics"))
            return
        initial_dir = ""
        if self.video:
            initial_dir = os.path.dirname(os.path.abspath(self.video))
        if not initial_dir or not os.path.isdir(initial_dir):
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            initial_dir = desktop if os.path.isdir(desktop) else os.getcwd()
        output_path = filedialog.asksaveasfilename(
            title=tr("dialog.save_diagnostics"),
            initialdir=initial_dir,
            initialfile=diagnostics.suggested_report_name(),
            defaultextension=".log",
            filetypes=[
                (tr("filetype.diagnostic"), "*.log"),
                (tr("filetype.text"), "*.txt"),
                (tr("common.all_files"), "*.*"),
            ],
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
        self.set_status(tr("status.diagnosing_host"))
        self.logln("[诊断] 开始生成一键诊断报告…")

        def finish(result, error):
            self._diagnosing = False
            self._diagnostic_thread = None
            self.root.config(cursor="")
            self._update_action_labels()
            self._update_host_control_states()
            self._update_queue_action_states()
            if error:
                self.set_status(tr("status.diagnostics_export_failed"))
                self.logln("[诊断] 导出失败: " + error)
                messagebox.showerror(
                    tr("dialog.diagnostics_failed"),
                    tr("message.diagnostics_save_failed", error=error),
                )
                return
            path = result["path"]
            passed = int(result.get("passed", 0))
            total = int(result.get("total", 0))
            self.set_status(tr("status.diagnostics_complete", passed=passed, total=total))
            self.logln(f"[诊断] 已导出: {path}；宿主探针 {passed}/{total} 通过")
            messagebox.showinfo(
                tr("dialog.diagnostics_complete"),
                tr("message.diagnostics_complete", path=path, passed=passed, total=total),
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
            self.logln(tr("log.dnd_unavailable"))
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
            self.logln(tr("log.dnd_init_failed", error=ex))

    def _on_drop_enter(self, event):
        if not self._exporting and not self._queue_running and not self._diagnosing:
            self._freeze_preview_cache(resume_ms=None)
            self._drop_hover = True
            self.canvas.config(bg=self._ui_color("canvas_drop", CANVAS_DROP_BG))
            if not self.video:
                self._draw_empty()
            self.set_status(tr("status.release_multi_import"))
        return getattr(event, "action", None)

    def _on_drop_leave(self, event):
        self._drop_hover = False
        self.canvas.config(bg=self._ui_color("canvas", CANVAS_BG))
        if not self.video:
            self._draw_empty()
        if not self._exporting and not self._queue_running and not self._diagnosing:
            self.set_status(tr("status.ready"))
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
            messagebox.showinfo(tr("dialog.busy"), message)
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
        self._clear_preview_pending = False
        self._guidance_preview_epoch = getattr(self, '_guidance_preview_epoch', 0) + 1
        self._guidance_result = None
        self._guidance_ready = self._guidance_presented = None
        self._guidance_display_signature = None
        self.pause()
        self._cancel_after('_clear_preview_after')
        self._freeze_preview_cache(resume_ms=None)
        if getattr(self,'_shared_cache_pool',None):
            self._shared_cache_pool.invalidate_guidance()
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
        if getattr(self, '_switching_backend', False):
            return
        if self._exporting or self._queue_running or self._diagnosing:
            message = (
                tr("message.wait_diagnostics_clear")
                if self._diagnosing else
                tr("message.wait_queue_clear")
            )
            messagebox.showinfo(tr("dialog.busy"), message)
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
        self.set_status(tr("status.ready"))
        self.logln(tr("status.cleared"))

    def _load_media(self, path):
        if getattr(self, '_switching_backend', False):
            return False
        if self._exporting or self._queue_running or self._diagnosing:
            message = (
                tr("message.wait_diagnostics_import")
                if self._diagnosing else
                tr("message.wait_queue_import")
            )
            messagebox.showinfo(tr("dialog.busy"), message)
            return False
        path = os.path.abspath(os.path.normpath(path))
        if not os.path.isfile(path):
            messagebox.showerror(
                tr("dialog.import_failed"), tr("message.file_missing", path=path)
            )
            self.set_status(tr("status.import_missing"))
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
            tr("dialog.unsupported_format"), tr("message.media_formats"),
        )
        self.set_status(tr("status.import_unsupported"))
        return False

    def _load_image(self, path):
        img = _read_image_bgr(path)
        if img is None or img.size == 0:
            messagebox.showerror(
                tr("dialog.import_failed"), tr("message.image_unreadable")
            )
            self.set_status(tr("status.import_image_failed"))
            return False
        h, w = img.shape[:2]
        if w <= 0 or h <= 0:
            messagebox.showerror(
                tr("dialog.import_failed"), tr("message.image_size_unreadable")
            )
            self.set_status(tr("status.import_image_failed"))
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
        self.logln(tr("status.imported_image", path=f"{path}  ({w}×{h})"))
        self.set_status(tr("status.image_summary", width=w, height=h))
        self._schedule_preview_cache_resume()
        return True

    def _load_video(self, path):
        """Validate and load a video from either the file dialog or drag-and-drop."""
        if self._exporting or self._queue_running:
            messagebox.showinfo(tr("dialog.busy"), tr("message.wait_queue_import"))
            return False
        path = os.path.abspath(os.path.normpath(path))
        if not os.path.isfile(path):
            messagebox.showerror(
                tr("dialog.import_failed"), tr("message.file_missing", path=path)
            )
            self.set_status(tr("status.import_missing"))
            return False
        if not _is_video_path(path):
            messagebox.showerror(
                tr("dialog.unsupported_format"), tr("message.video_formats"),
            )
            self.set_status(tr("status.import_unsupported"))
            return False

        new_cap = cv2.VideoCapture(path)
        if not new_cap.isOpened():
            new_cap.release()
            messagebox.showerror(
                tr("dialog.import_failed"), tr("message.video_unreadable")
            )
            self.set_status(tr("status.import_video_open_failed"))
            return False
        n = int(new_cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        fps = new_cap.get(cv2.CAP_PROP_FPS) or 30.0
        w = int(new_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(new_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if w <= 0 or h <= 0:
            new_cap.release()
            messagebox.showerror(
                tr("dialog.import_failed"), tr("message.video_size_unreadable")
            )
            self.set_status(tr("status.import_video_size_failed"))
            return False

        try:
            color_info = probe_video_stream(find_ffmpeg(), path)
        except Exception as ex:
            color_info = {"is_hdr": False, "profile": "srgb", "label": "SDR / sRGB"}
            self.logln(tr("log.color_fallback", error=ex))

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
        self.logln(tr(
            "log.imported_video", path=self.video, frames=n,
            color=color_info.get('label', tr("common.unknown")),
            pixel_format=color_info.get('pixel_format', 'unknown'),
            primaries=color_info.get('color_primaries', 'unknown'),
            transfer=color_info.get('color_transfer', 'unknown'),
        ))
        duration = n / max(float(fps) or 30.0, 1.0)
        self._audio.prepare(path, duration, callback=self._audio_ready_cb)
        self._schedule_preview_cache_resume()
        if not self._hinted_keys:
            self.set_status(tr("status.preview_shortcuts"))
            self._hinted_keys = True
        else:
            self.set_status(tr("status.video_summary", frames=n, fps=fps, width=w, height=h))
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
            self.logln(tr(
                "log.audio_prepare_failed",
                error=message or tr("common.unknown_error"),
            ))
            return
        if message != "ok":
            return
        self.logln(tr("log.audio_ready"))
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
        self, success, out_path, done_label=None, done_message=None, cancelled=False,
        completed_items=1, notify=True, error_message="",
    ):
        done_label = done_label or tr("common.completed")
        self._exporting = False
        self._export_cancel_event.clear()
        self._update_action_labels()
        self._update_host_control_states()
        self._update_queue_action_states()
        if cancelled:
            removed = self._remove_partial_export(out_path)
            self.pbar["value"] = 0
            message = (
                tr("status.export_cancelled_clean")
                if removed else
                tr("status.export_cancelled_dirty")
            )
            self.set_status(message)
            self.logln(tr("log.export_message", message=message))
        elif success:
            completed_items = max(int(completed_items), 1)
            self.set_progress(completed_items, completed_items, done_label)
            self.logln(tr("status.exported", path=out_path))
            if notify:
                messagebox.showinfo(
                    tr("tab.export"),
                    done_message or tr("status.exported", path=out_path),
                )
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
            self.set_status(tr("status.export_failed_log"))
            if notify:
                guidance = _dlss_runtime_guidance(error_message)
                if guidance and _is_dlss_runtime_unsupported(error_message):
                    if messagebox.askyesno(
                        tr("dialog.dlss_runtime_unsupported"),
                        guidance + tr("message.open_releases_prompt"),
                        icon="error",
                    ):
                        self._open_release_page(updater.RELEASES_URL)
                else:
                    messagebox.showerror(
                        tr("dialog.export_failed"), guidance or tr("message.export_incomplete")
                    )

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
        self.set_status(tr("status.cancelling_export"))
        self.logln(tr("log.export_cancel_requested"))

    def _raise_if_export_cancelled(self):
        if self._export_cancel_event.is_set():
            raise _ExportCancelled()

    def _confirm_super_resolution_export(self, width, height, scale, is_hdr=False, notify=True):
        scale = normalize_scale(scale)
        if scale == 1:
            return True
        try:
            validate_super_resolution_dimensions(width, height, scale)
        except SuperResolutionError as error:
            self.logln(str(error))
            if notify:
                messagebox.showerror(tr('dialog.rtx_unavailable'), str(error))
            return False
        status = super_resolution_runtime_status()
        if not status['available']:
            message = tr(
                "message.vsr_components_missing",
                missing=", ".join(status['missing']),
            )
            self.logln("[RTX 超分] " + message.replace("\n", " "))
            if notify:
                messagebox.showerror(tr("dialog.rtx_unavailable"), message)
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
        warning = tr(
            "message.super_resolution_plan",
            scale=scale,
            width=resource_estimate['output_width'],
            height=resource_estimate['output_height'],
            frame_memory=format_bytes(resource_estimate['single_frame_bytes']),
            gpu_minimum=format_bytes(resource_estimate['known_gpu_bytes']),
            gpu_recommended=format_bytes(resource_estimate['recommended_gpu_bytes']),
            ram_recommended=format_bytes(resource_estimate['recommended_ram_bytes']),
        )
        if gpu_memory:
            warning += tr(
                "message.gpu_free", value=format_bytes(gpu_memory['free_bytes'])
            )
        if resource_estimate['output_width'] > 8192 or resource_estimate['output_height'] > 8192:
            warning += tr("message.over_8192")
        warning += tr("message.super_resolution_continue")
        confirmed = messagebox.askyesno(
            tr("dialog.high_resource"), warning, icon="warning"
        )
        if confirmed:
            self._confirmed_super_resolution_plans.add(plan_key)
        return confirmed

    def _export_image(self):
        settings = self._collect_settings()
        self._save_settings_now()
        if self._image_bgr is None:
            messagebox.showwarning(
                tr("dialog.hint"), tr("message.import_image_first")
            )
            return
        return self._export_image_source(self.video, settings, notify=True)

    def _process_still_image(self, source_bgr, settings):
        """Process one standalone image without consulting or polluting preview caches."""
        height, width = source_bgr.shape[:2]
        if int(settings.get('guidance_mode', 0)) in (1, 3):
            self.logln(tr('guidance.still_hint'))
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
                raise RuntimeError(
                    getattr(self, "_live_error", tr("message.dlss_host_unavailable"))
                )
            processed_rgba = live.process(rgba, reset=True)
            self._last_dlss_frame = -1
        if processed_rgba is None:
            return None
        return cv2.cvtColor(processed_rgba[..., :3], cv2.COLOR_RGB2BGR)

    def _export_image_source(self, source_path, settings, out_path=None, notify=True):
        """Export one immutable SDR image request for the preview UI or mixed queue."""
        source_path = os.path.abspath(os.path.normpath(source_path))
        settings = {**self._collect_settings(), **dict(settings or {})}
        settings['guidance_cache_pool'] = self._ensure_shared_cache_pool().name
        orig = _read_image_bgr(source_path)
        if orig is None or orig.size == 0:
            error = tr("message.input_image_unreadable")
            self.logln(tr("log.export_error", error=error))
            if notify:
                messagebox.showerror(tr("dialog.export_failed"), error)
            return {
                "success": False, "cancelled": False, "error": error,
                "output_path": out_path or "", "frames": 0,
            }
        scale = self._super_resolution_scale(settings)
        if not self._confirm_super_resolution_export(
            orig.shape[1], orig.shape[0], scale, is_hdr=False, notify=notify,
        ):
            return {
                "success": False, "cancelled": True,
                "error": tr("message.super_resolution_cancelled"),
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
            self.logln(tr("log.target_renamed", name=os.path.basename(out_path)))
        self._begin_export_ui(tr("status.exporting_image"))
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
            self.logln(tr("log.image_exported", width=w, height=h, seconds=elapsed))
            self.set_progress(1, 1, tr("common.completed"))
        except Exception as ex:
            traceback.print_exc()
            error_message = str(ex)
            self.logln(tr("log.export_error", error=error_message))
        self._end_export_ui(
            success, out_path, tr("common.completed"),
            tr("status.exported_image", path=out_path) if success else None,
            error_message=error_message,
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
        # This path processes synchronously; extra in-flight textures cannot
        # improve throughput until VSR/DLSS scheduling is pipelined.
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
            self.logln(f"[导出] 编码器: {writer.encoder_name}")
            sr_live = ProcessSuperResolution(width, height, scale, is_hdr=False)
            live = ProcessLive(output_width, output_height, dlss_settings, _on_guidance_ready=self._guidance_started)
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
                f"[DLSS 主机] {live.backend}；目标分辨率队列 {live.max_in_flight} 帧；"
                f"GPU {live.adapter_info.get('name', 'unknown')}"
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
                    self.set_progress(
                        written, max(total_frames, written),
                        tr("status.upscale_dlss_export"),
                    )
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
            "color_primaries": color_info.get('color_primaries', 'bt2020'),
            "host_backend": "v2",
            "host_auto_fallback": False,
        }
        hdr_settings['host_in_flight'] = select_in_flight(
            width, height, scale, settings.get('host_in_flight', 2), is_hdr=True,
            guidance=bool(settings.get('guidance_mode')),
            gpu_memory=query_gpu_memory(),
        )
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
            live = ProcessLive(process_width, process_height, hdr_settings, _on_guidance_ready=self._guidance_started)
            if settings.get('guidance_mode'):
                self.logln(tr('guidance.hdr_hint'))
            self.logln(
                f"[HDR] {color_info.get('label')} → "
                + (f"10-bit RTX VSR {scale}× → " if scale > 1 else "")
                + "RGBA16F Feature 18 → "
                f"{writer.encoder_name}"
            )
            self.logln(
                f"[DLSS 主机] {live.backend}；HDR GPU 队列 {live.max_in_flight} 帧；"
                f"GPU {live.adapter_info.get('name', 'unknown')}"
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
        if getattr(self, '_switching_backend', False):
            return
        if not self.video:
            messagebox.showwarning(tr("dialog.hint"), tr("message.import_first"))
            return
        if self._queue_running or self._exporting or (self.thread and self.thread.is_alive()):
            messagebox.showinfo(tr("dialog.busy"), tr("message.previous_running"))
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
        settings['guidance_cache_pool'] = self._ensure_shared_cache_pool().name
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
            error = tr("message.input_video_dimensions_failed")
            self.logln(tr("log.export_error", error=error))
            if notify:
                messagebox.showerror(tr("dialog.export_failed"), error)
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
            self.logln(tr("log.target_renamed", name=os.path.basename(out_path)))
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
                    f"队列 {result['in_flight']} 帧/进程；"
                    f"GPU {', '.join(result.get('render_gpus', ['unknown']))}"
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
                            self.set_progress(
                                exported_frames, n, tr("status.pipeline_export")
                            )
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
                                raise RuntimeError(
                                    getattr(
                                        self, "_live_error",
                                        tr("message.dlss_host_unavailable"),
                                    )
                                )
                            live.update(settings)
                            self.logln(
                                f"[DLSS 主机] {live.backend}；"
                                f"GPU 队列 {live.max_in_flight} 帧；"
                                f"GPU {live.adapter_info.get('name', 'unknown')}"
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
            self.logln(tr("log.export_error", error=error_message))
        finally:
            if writer and not success:
                writer.abort()
            if cancelled and live is not None:
                # An async single-session export may still own queued frames. Reusing
                # that host would return stale output on the next preview/export.
                self._close_live()
        self._end_export_ui(
            success, out_path, tr("common.completed"),
            tr("status.exported_audio", path=out_path) if success else None,
            cancelled=cancelled,
            completed_items=exported_frames,
            error_message=error_message,
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
                    raise RuntimeError(tr("message.export_decode_failed"))
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
        from dlss5tool import dlss_engine
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


def cli():
    # Required for ProcessLive's spawn worker in a PyInstaller build.
    multiprocessing.freeze_support()
    if "--diagnostic-worker" in sys.argv:
        worker_index = sys.argv.index("--diagnostic-worker")
        raise SystemExit(diagnostics.diagnostic_worker_main(
            sys.argv[worker_index + 1:worker_index + 5]
        ))
    if "--parallel-worker" in sys.argv:
        sys.argv.remove("--parallel-worker")
        from dlss5tool.parallel_export_worker import main as parallel_worker_main
        parallel_worker_main()
    else:
        main()


if __name__ == "__main__":
    cli()
