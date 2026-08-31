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
import os
import multiprocessing
import queue
import sys
import threading
import time
import traceback
from collections import deque
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk, filedialog, messagebox, scrolledtext

import cv2
import numpy as np

import app_settings
from app_version import APP_VERSION
import dlss_engine
from dlss_host_process import ProcessLive
from parallel_export import export_parallel
from preview_audio import PreviewAudio, ms_to_frame
from video_export import FFmpegVideoWriter, compose_output_frame

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
    "p1 最快": "p1", "p3 快速": "p3", "p5 高质量": "p5", "p7 最慢": "p7",
}
NVENC_PRESET_NAMES = {value: name for name, value in NVENC_PRESET_CHOICES.items()}
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
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_FILETYPES = [
    ("媒体", "*.mp4 *.avi *.mov *.mkv *.m4v *.webm *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
    ("视频", "*.mp4 *.avi *.mov *.mkv *.m4v *.webm"),
    ("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"),
    ("所有文件", "*.*"),
]
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
SCALE_DISABLED = {
    "troughcolor": "#e6e6e6",
    "background": "#d0d0d0",
    "activebackground": "#d0d0d0",
    "highlightbackground": "#ececec",
}
SCALE_VALUE_ON = "#222222"
SCALE_VALUE_OFF = "#9a9a9a"
CANVAS_RESIZE_MS = 30
VK_MENU = 0x12
SPLIT_HIT_PX = 18
_INPUT_WIDGETS = {
    "Entry", "TEntry", "Text", "Combobox", "TCombobox",
    "Spinbox", "TSpinbox",
}
_SPACE_PASSTHROUGH = _INPUT_WIDGETS | {
    "Button", "TButton", "Checkbutton", "TCheckbutton",
    "Radiobutton", "TRadiobutton",
}
_FRAME_STREAM_END = object()


class _ExportCancelled(Exception):
    """Internal control-flow signal for a user-requested video export stop."""


def _clamp_frame(frame, last):
    try:
        frame = int(frame)
    except (TypeError, ValueError):
        frame = 0
    return max(0, min(frame, max(int(last), 0)))


def _format_timecode(frame, fps):
    fps = float(fps) if fps else 30.0
    if fps <= 0:
        fps = 30.0
    total = max(int(frame), 0) / fps
    minutes = int(total // 60)
    seconds = total - minutes * 60
    return f"{minutes}:{seconds:05.2f}"


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


class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, event=None):
        if not self.text or self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            tip, text=self.text, justify="left", background="#ffffe8",
            foreground="#222", relief="solid", borderwidth=1,
            font=("Microsoft YaHei", 9), wraplength=380, padx=7, pady=5,
        )
        label.pack()
        self.tip = tip

    def _hide(self, event=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class CollapsibleSection(ttk.Frame):
    def __init__(self, parent, title, collapsed=True, on_toggle=None, tooltip=None):
        super().__init__(parent)
        self._title = title
        self._collapsed = bool(collapsed)
        self._on_toggle = on_toggle
        header = ttk.Frame(self)
        header.pack(fill="x")
        self._btn = ttk.Button(
            header, text=self._header_text(), style="Toolbutton",
            command=self.toggle,
        )
        self._btn.pack(side="left")
        if tooltip:
            Tooltip(self._btn, tooltip)
        self.body = ttk.Frame(self)
        if not self._collapsed:
            self.body.pack(fill="x", padx=(12, 0), pady=(0, 4))

    @property
    def collapsed(self):
        return self._collapsed

    def _header_text(self):
        return ("▸  " if self._collapsed else "▾  ") + self._title

    def toggle(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.body.pack_forget()
        else:
            self.body.pack(fill="x", padx=(12, 0), pady=(0, 4))
        self._btn.config(text=self._header_text())
        if self._on_toggle:
            self._on_toggle()


class TimelineBar(tk.Canvas):
    def __init__(self, master, height=18, **kwargs):
        super().__init__(
            master, height=height, bg=TIMELINE_BG, highlightthickness=0,
            cursor="hand2", **kwargs,
        )
        self._min = 0
        self._max = 0
        self._value = 0
        self._dragging = False
        self._rendered_ranges = []
        self._queued_ranges = []
        self.on_seek = None
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._on_down)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_up)

    def set_range(self, minimum, maximum):
        self._min = int(minimum)
        self._max = max(int(maximum), self._min)
        self._value = max(self._min, min(self._value, self._max))
        self._redraw()

    def set(self, value):
        value = _clamp_frame(value, self._max)
        if value == self._value:
            return
        self._value = value
        self._redraw()

    def get(self):
        return self._value

    def set_cache_ranges(self, rendered=(), queued=()):
        rendered = list(rendered)
        queued = list(queued)
        if rendered == self._rendered_ranges and queued == self._queued_ranges:
            return
        self._rendered_ranges = rendered
        self._queued_ranges = queued
        self._redraw()

    def _frac(self):
        span = self._max - self._min
        if span <= 0:
            return 0.0
        return (self._value - self._min) / span

    def _value_from_x(self, x):
        pad = 8
        width = max(self.winfo_width() - pad * 2, 1)
        frac = max(0.0, min(1.0, (x - pad) / width))
        span = self._max - self._min
        return int(round(self._min + frac * span))

    def _x_from_value(self, value, pad, width):
        span = self._max - self._min
        if span <= 0:
            return pad
        frac = (max(self._min, min(int(value), self._max)) - self._min) / span
        return pad + frac * width

    def _emit(self, phase):
        if self.on_seek:
            self.on_seek(self._value, phase)

    def _on_down(self, event):
        self._dragging = True
        self.set(self._value_from_x(event.x))
        self._emit("start")

    def _on_drag(self, event):
        if not self._dragging:
            return
        self.set(self._value_from_x(event.x))
        self._emit("move")

    def _on_up(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self.set(self._value_from_x(event.x))
        self._emit("end")

    def _redraw(self):
        self.delete("all")
        w = max(self.winfo_width(), 2)
        h = max(self.winfo_height(), 2)
        pad = 8
        y = h // 2
        x1 = w - pad
        span_width = max(x1 - pad, 1)
        self.create_line(pad, y, x1, y, fill=TIMELINE_TRACK, width=4, capstyle="round")
        cache_y = max(y - 5, 2)
        for start, end in self._queued_ranges:
            self.create_line(
                self._x_from_value(start, pad, span_width), cache_y,
                self._x_from_value(end + 1, pad, span_width), cache_y,
                fill=TIMELINE_QUEUED, width=2,
            )
        for start, end in self._rendered_ranges:
            self.create_line(
                self._x_from_value(start, pad, span_width), cache_y,
                self._x_from_value(end + 1, pad, span_width), cache_y,
                fill=TIMELINE_RENDERED, width=2,
            )
        x = pad + self._frac() * max(x1 - pad, 1)
        if self._max > self._min:
            self.create_line(pad, y, x, y, fill=TIMELINE_FILL, width=4, capstyle="round")
        r = 6
        self.create_oval(x - r, y - r, x + r, y + r, fill=TIMELINE_THUMB, outline="#111", width=1)


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_TITLE} — 实时预览 + 导出")
        root.geometry("1000x820")
        try:
            ttk.Style().configure("Toolbutton", padding=(8, 2))
        except Exception:
            pass
        self._saved_settings = app_settings.load()
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
        self._live = None
        self._live_cache = None
        self._last_dlss_frame = -1
        self._live_debounce = None
        self._output_preview_after = None
        self._scrub_after = None
        self._resize_after = None
        self._play_after = None
        self._preview_decode_after = None
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
        self._media_w = 0
        self._media_h = 0
        self._active_preview_size = None
        self._fullscreen = False
        self._fs_hidden = []
        self._fs_geom = None
        self.view_var = tk.StringVar(value=self._saved_settings["preview_view"])

        # ---- preview canvas ----
        self.canvas = tk.Canvas(root, bg=CANVAS_BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Button-1>", self.on_canvas_press)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<Motion>", self.on_canvas_hover)
        self.canvas.bind("<Double-Button-1>", self.on_canvas_double)
        self.canvas.bind("<Leave>", lambda e: self.canvas.config(cursor=""))
        self.canvas.bind("<MouseWheel>", self._on_wheel_step)

        # ---- transport: timeline + playback chrome ----
        transport = ttk.Frame(root)
        self.transport = transport
        transport.pack(fill="x", padx=8, pady=(0, 4))
        self.timeline = TimelineBar(transport)
        self.timeline.pack(fill="x", pady=(0, 4))
        self.timeline.on_seek = self._on_timeline_seek
        self.timeline.bind("<MouseWheel>", self._on_wheel_step)
        Tooltip(self.timeline, "浅青：当前设置下已渲染；灰青：已解码并等待渲染。")

        ctrl = ttk.Frame(transport)
        ctrl.pack(fill="x")
        self.prev_btn = ttk.Button(ctrl, text="⟨", width=3, command=lambda: self.step_frame(-1))
        self.prev_btn.pack(side="left")
        Tooltip(self.prev_btn, "上一帧（←）")
        self.play_btn = ttk.Button(ctrl, text="▶ 播放", width=8, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.play_btn, "播放 / 暂停（空格）。播完停在最后一帧。")
        self.next_btn = ttk.Button(ctrl, text="⟩", width=3, command=lambda: self.step_frame(1))
        self.next_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.next_btn, "下一帧（→）")
        self.mute_btn = ttk.Button(ctrl, text="音", width=3, command=self.toggle_mute)
        self.mute_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.mute_btn, "预览播放原视频声音。点击静音/取消静音。")

        self.time_label = ttk.Label(ctrl, text="0:00.00 / 0:00.00", width=18, anchor="w")
        self.time_label.pack(side="left", padx=(10, 6))
        ttk.Label(ctrl, text="帧").pack(side="left")
        self.fentry = tk.Entry(ctrl, width=6)
        self.fentry.pack(side="left", padx=3)
        self.fentry.insert(0, "0")
        self.fentry.bind("<Return>", self.on_frame_entry)
        self.fentry.bind("<FocusOut>", lambda e: self.sync_frame_entry())
        self.ftotal = ttk.Label(ctrl, text="/ 0")
        self.ftotal.pack(side="left")

        self.fs_btn = ttk.Button(ctrl, text="全屏", width=6, command=self.toggle_fullscreen)
        self.fs_btn.pack(side="right")
        Tooltip(self.fs_btn, "全屏预览（F11 或双击画面，Esc 退出）")
        view_bar = ttk.Frame(ctrl)
        view_bar.pack(side="right", padx=(0, 8))
        for name in VIEWS:
            ttk.Radiobutton(
                view_bar, text=name, value=name, variable=self.view_var,
                style="Toolbutton", command=self.on_view_change,
            ).pack(side="left", padx=1)
        Tooltip(
            view_bar,
            "1 原图  ·  2 DLSS  ·  3 对比。对比模式可单击定位或横向拖动分界线；按住 Alt 查看纯原图。",
        )

        # ---- DLSS settings ----
        sf = ttk.LabelFrame(root, text="DLSS 设置")
        self._settings_frame = sf
        sf.pack(fill="x", padx=8, pady=4)
        self._settings = self._build_settings(sf)
        Tooltip(
            sf,
            "每个滑条的开关关闭时按 0 处理，开启后使用记忆的数值。\n"
            "皮肤蒙版关闭时皮肤结构按 0；开启后使用记忆的皮肤结构，并用自动蒙版。",
        )

        self._preview_section = CollapsibleSection(
            root, "预览性能",
            collapsed=not self._saved_settings.get("ui_preview_open", False),
            on_toggle=self._on_panels_toggle,
            tooltip=(
                "播放质量只影响实时播放；暂停、逐帧和拖动松手后仍生成原始分辨率精确帧。\n"
                "缓存窗口越大越占内存。"
            ),
        )
        self._preview_section.pack(fill="x", padx=8, pady=2)
        self._preview_settings = self._build_preview_settings(self._preview_section.body)
        self._preview_runtime_settings = self._collect_preview_settings()
        self.root.after_idle(self._update_preview_memory_hint)

        self._export_section = CollapsibleSection(
            root, "导出性能",
            collapsed=not self._saved_settings.get("ui_export_open", False),
            on_toggle=self._on_panels_toggle,
            tooltip="案例实测：2进程 / 预热8帧最快。并行模式保持视觉质量，但不保证逐像素时序一致。",
        )
        self._export_section.pack(fill="x", padx=8, pady=2)
        self._export_settings = self._build_export_settings(self._export_section.body)

        self._host_section = CollapsibleSection(
            root, "高级主机优化",
            collapsed=not self._saved_settings.get("ui_host_open", False),
            on_toggle=self._on_panels_toggle,
            tooltip="后端在隔离进程中热切换，无需重启 GUI。导出期间为保持时序会锁定这些选项。",
        )
        self._host_section.pack(fill="x", padx=8, pady=2)
        self._host_settings = self._build_host_settings(self._host_section.body)

        # ---- import / export action, just above the log ----
        e = ttk.Frame(root)
        e.pack(fill="x", padx=8, pady=(6, 2))
        self._export_row = e
        actions = ttk.Frame(e)
        actions.pack(fill="x")
        self.import_btn = ttk.Button(actions, text="导入", command=self.import_media)
        self.import_btn.pack(side="left")
        self.clear_btn = ttk.Button(actions, text="清空", command=self.clear_media)
        self.clear_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.clear_btn, "卸下当前视频/图片，释放解码、音轨和 DLSS 主机占用。")
        self.export_btn = ttk.Button(actions, text="导出 DLSS", command=self.export_dlss)
        self.export_btn.pack(side="left", padx=(6, 0))
        self.cancel_export_btn = ttk.Button(
            actions, text="取消导出", command=self.cancel_export,
        )
        self.cancel_export_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.cancel_export_btn, "停止当前视频导出，并清理本次未完成的输出文件。")
        self.pbar = ttk.Progressbar(e, maximum=100)
        self.pbar.pack(fill="x", pady=(4, 0))
        self.eta_label = ttk.Label(e, text="", anchor="w")
        self.eta_label.pack(fill="x", pady=(2, 0))

        self.log = scrolledtext.ScrolledText(root, height=4, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=False, padx=8, pady=4)

        self._bind_player_keys()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_drag_and_drop()
        self._update_action_labels()
        self.root.after_idle(self._draw_empty)
        root.minsize(880, 680)

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
            busy = bool(self._exporting)
            cancel_requested = self._export_cancel_event.is_set()
            self._set_ttk_enabled(self.import_btn, not busy)
            self._set_ttk_enabled(self.clear_btn, has and not busy)
            self._set_ttk_enabled(self.export_btn, has and not busy)
            self._set_ttk_enabled(
                self.cancel_export_btn,
                busy and not self._is_image and not cancel_requested,
            )
            self.cancel_export_btn.config(text="取消中…" if cancel_requested else "取消导出")
            if self._is_image:
                self.export_btn.config(text="导出 DLSS 图片")
            else:
                self.export_btn.config(text="导出 DLSS 视频")
        except Exception:
            pass

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
        return image if ok else None

    # ---------- preview ----------
    def _build_settings(self, parent):
        d = {}
        saved = self._saved_settings
        d['v_style'] = tk.StringVar(value=STYLE_NAMES.get(saved['style'], "默认"))
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
        body = ttk.Frame(parent)
        body.pack(fill="x", padx=4, pady=2)

        top = ttk.Frame(body)
        top.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(top, text="风格").pack(side="left")
        style_cb = ttk.Combobox(
            top, textvariable=d['v_style'], values=list(STYLE_CHOICES),
            state="readonly", width=10,
        )
        style_cb.pack(side="left", padx=(6, 20))
        ttk.Label(top, text="输出视图").pack(side="left")
        outview_cb = ttk.Combobox(
            top, textvariable=d['v_outview'], values=list(OUTVIEW_CHOICES),
            state="readonly", width=10,
        )
        outview_cb.pack(side="left", padx=(6, 0))
        Tooltip(
            outview_cb,
            "导出视图只影响导出构图；上方预览始终使用「处理」构图。\n"
            "处理：DLSS/对比预览和导出都会按输出混合与原图融合。\n"
            "差异×10：仅导出，把 DLSS 与原图的差值放大 10 倍，灰色=几乎没改，亮/暗=改动大。\n"
            "左右对比：仅导出，左半原图、右半 DLSS，中间用白线分隔。",
        )
        style_cb.bind("<<ComboboxSelected>>", lambda e: self.on_settings_change())
        outview_cb.bind("<<ComboboxSelected>>", lambda e: self.on_output_settings_change())

        ttk.Separator(body, orient="horizontal").pack(fill="x", padx=8, pady=(2, 2))

        grid = ttk.Frame(body)
        grid.pack(fill="x")
        for col in range(3):
            grid.columnconfigure(col, weight=1, uniform="dlss_slider")
        _, d['w_intensity'], d['w_intensity_value'] = self._add_toggle_slider(
            grid, 0, 0, "强度", d['v_intensity'], d['v_use_intensity'],
            "关闭时按 0 处理。开启后使用记忆的强度。",
        )
        d['w_use_output_mix'], d['w_outmix'], d['w_outmix_value'] = self._add_toggle_slider(
            grid, 0, 1, "输出混合", d['v_outmix'], d['v_use_output_mix'],
            "仅「处理」有效。0=原图，1=完整 DLSS；DLSS/对比预览与导出同步生效。\n"
            "关闭时按 0（原图），开启后使用记忆的混合比例。",
            on_change=self.on_output_settings_change,
        )
        _, d['w_local_tone'], d['w_local_tone_value'] = self._add_toggle_slider(
            grid, 1, 0, "本地色调", d['v_local_tone'], d['v_use_local_tone'],
            "关闭时按 0 处理。开启后使用记忆的本地色调。",
        )
        _, d['w_local_struct'], d['w_local_struct_value'] = self._add_toggle_slider(
            grid, 1, 1, "本地结构", d['v_local_struct'], d['v_use_local_struct'],
            "关闭时按 0 处理。开启后使用记忆的本地结构。",
        )
        _, d['w_skin_struct'], d['w_skin_struct_value'] = self._add_toggle_slider(
            grid, 1, 2, "皮肤蒙版", d['v_skin_struct'], d['v_auto_mask'],
            "关闭时皮肤结构按 0 处理。开启后使用记忆的皮肤结构，并用自动蒙版保护皮肤纹理。",
        )
        self._settings = d
        self._update_dlss_control_states()
        return d

    def _add_toggle_slider(
        self, parent, row, column, text, value_var, enabled_var,
        tooltip=None, on_change=None,
    ):
        on_change = on_change or self.on_settings_change
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=column, sticky="nsew", padx=16, pady=(4, 8))
        header = ttk.Frame(cell)
        header.pack(fill="x")
        checkbox = ttk.Checkbutton(
            header, text=text, variable=enabled_var,
            command=on_change,
        )
        checkbox.pack(side="left")
        value_label = ttk.Label(header, width=4, anchor="e", foreground=SCALE_VALUE_OFF)
        value_label.pack(side="right")

        def refresh_label(*_args):
            try:
                value_label.config(text=f"{float(value_var.get()):.2f}")
            except (TypeError, ValueError, tk.TclError):
                pass

        refresh_label()
        value_var.trace_add("write", refresh_label)
        scale = tk.Scale(
            cell, from_=0, to=1, resolution=0.05, orient="horizontal",
            showvalue=False, variable=value_var, length=160,
            sliderlength=16, highlightthickness=0, **SCALE_DISABLED,
        )
        scale.pack(fill="x")
        scale.config(command=lambda e: on_change())
        if tooltip:
            Tooltip(checkbox, tooltip)
            Tooltip(scale, tooltip)
            Tooltip(value_label, tooltip)
        return checkbox, scale, value_label

    def _set_slider_enabled(self, scale, value_label, enabled):
        colors = SCALE_ENABLED if enabled else SCALE_DISABLED
        scale.config(state="normal" if enabled else "disabled", **colors)
        if value_label is not None:
            value_label.config(foreground=SCALE_VALUE_ON if enabled else SCALE_VALUE_OFF)

    def _update_dlss_control_states(self):
        if not hasattr(self, "_settings"):
            return
        d = self._settings
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
        ttk.Label(parent, text="播放质量:").grid(row=0, column=0, sticky="e", padx=(6, 2), pady=4)
        quality = ttk.Combobox(
            parent, textvariable=d['v_quality'], values=list(PREVIEW_QUALITY_CHOICES),
            state="readonly", width=14,
        )
        quality.grid(row=0, column=1, sticky="w", padx=(0, 10))
        Tooltip(quality, "自动模式会把 4K 级素材降到 1080p 实时处理；暂停后恢复原始分辨率。")
        ttk.Label(parent, text="缓存预算:").grid(row=0, column=2, sticky="e", padx=(4, 2))
        cache_mb = ttk.Spinbox(
            parent, from_=256, to=32768, increment=256,
            textvariable=d['v_cache_mb'], width=7,
        )
        cache_mb.grid(row=0, column=3, sticky="w")
        ttk.Label(parent, text="MiB").grid(row=0, column=4, sticky="w", padx=(2, 10))
        ttk.Label(parent, text="启动缓冲:").grid(row=0, column=5, sticky="e", padx=(4, 2))
        ttk.Label(parent, text=f"{PREVIEW_BUFFER_SECONDS:.1f} 秒").grid(
            row=0, column=6, sticky="w", padx=(0, 10),
        )
        ttk.Label(parent, text="拖动后生成:").grid(row=0, column=7, sticky="e", padx=(4, 2))
        scrub = ttk.Spinbox(parent, from_=0, to=400, textvariable=d['v_scrub_ms'], width=6)
        scrub.grid(row=0, column=8, sticky="w")
        Tooltip(scrub, "停止拖动或跳转后等待这段时间，再生成精确预览并从当前帧向后预渲染。")
        ttk.Label(parent, text="ms").grid(row=0, column=9, sticky="w", padx=(2, 8))
        d.update({
            'w_quality': quality, 'w_cache_mb': cache_mb, 'w_scrub_ms': scrub,
        })
        cache_hint = ttk.Label(parent, text="", foreground="#666666")
        cache_hint.grid(row=1, column=0, columnspan=10, sticky="w", padx=(6, 8), pady=(0, 2))
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
                value=NVENC_PRESET_NAMES.get(saved['nvenc_preset'], "p5 高质量")
            ),
        }
        ttk.Label(parent, text="模式:").grid(row=0, column=0, sticky="e", padx=(6, 2), pady=4)
        mode = ttk.Combobox(
            parent, textvariable=d['v_mode'], values=list(EXPORT_MODE_CHOICES),
            state="readonly", width=20,
        )
        mode.grid(row=0, column=1, padx=(0, 10))
        ttk.Label(parent, text="并行进程:").grid(row=0, column=2, sticky="e", padx=(4, 2))
        workers = ttk.Spinbox(parent, from_=2, to=4, textvariable=d['v_workers'], width=5)
        workers.grid(row=0, column=3, padx=(0, 10))
        ttk.Label(parent, text="预热帧:").grid(row=0, column=4, sticky="e", padx=(4, 2))
        warmup = ttk.Spinbox(parent, from_=0, to=120, textvariable=d['v_warmup'], width=6)
        warmup.grid(row=0, column=5, padx=(0, 10))
        ttk.Label(parent, text="解码缓存:").grid(row=0, column=6, sticky="e", padx=(4, 2))
        decode = ttk.Spinbox(parent, from_=1, to=8, textvariable=d['v_decode_buffer'], width=5)
        decode.grid(row=0, column=7, padx=(0, 10))
        ttk.Label(parent, text="NVENC:").grid(row=1, column=0, sticky="e", padx=(6, 2), pady=4)
        preset = ttk.Combobox(
            parent, textvariable=d['v_nvenc_preset'], values=list(NVENC_PRESET_CHOICES),
            state="readonly", width=12,
        )
        preset.grid(row=1, column=1, sticky="w", padx=(0, 10))
        d.update({
            'w_workers': workers,
            'w_warmup': warmup,
            'w_decode_buffer': decode,
        })
        mode.bind("<<ComboboxSelected>>", lambda e: self._on_export_settings_change())
        preset.bind("<<ComboboxSelected>>", lambda e: self._schedule_settings_save())
        for widget in (workers, warmup, decode):
            widget.config(command=self._schedule_settings_save)
            widget.bind("<FocusOut>", lambda e: self._schedule_settings_save())
            widget.bind("<Return>", lambda e: self._schedule_settings_save())
        self.root.after_idle(self._update_export_control_states)
        return d

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
        ttk.Label(parent, text="后端:").grid(row=0, column=0, sticky="e", padx=(6, 2), pady=4)
        backend = ttk.Combobox(
            parent, textvariable=d['v_backend'], values=list(HOST_BACKEND_CHOICES),
            state="readonly", width=17,
        )
        backend.grid(row=0, column=1, sticky="w", padx=(0, 10))
        ttk.Label(parent, text="提交方式:").grid(row=0, column=2, sticky="e", padx=(4, 2))
        submission = ttk.Combobox(
            parent, textvariable=d['v_submission'], values=list(HOST_SUBMISSION_CHOICES),
            state="readonly", width=17,
        )
        submission.grid(row=0, column=3, sticky="w", padx=(0, 10))
        zero_fast = ttk.Checkbutton(
            parent, text="零引导快路径", variable=d['v_zero_fast'],
            command=self._on_host_settings_change,
        )
        zero_fast.grid(row=0, column=4, sticky="w", padx=(4, 8))
        persistent = ttk.Checkbutton(
            parent, text="持久上传/回读缓冲", variable=d['v_persistent'],
            command=self._on_host_settings_change,
        )
        persistent.grid(row=0, column=5, sticky="w", padx=(4, 8))
        ttk.Label(parent, text="GPU 队列帧:").grid(row=1, column=0, sticky="e", padx=(6, 2), pady=4)
        in_flight = ttk.Spinbox(
            parent, from_=1, to=3, textvariable=d['v_in_flight'], width=5,
            command=self._on_host_settings_change,
        )
        in_flight.grid(row=1, column=1, sticky="w", padx=(0, 10))
        fallback = ttk.Checkbutton(
            parent, text="优化路径失败时自动回退", variable=d['v_fallback'],
            command=self._on_host_settings_change,
        )
        fallback.grid(row=1, column=2, columnspan=2, sticky="w", padx=(4, 8))
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
        if self._exporting or self._switching_backend:
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
        if self._exporting:
            self.set_status("正在导出，完成后才能切换主机后端")
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
        d = self._settings
        return {
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
            'use_auto_mask': 1 if remembered['use_auto_mask'] else 0,
            'skin_struct': effective_slider(
                remembered['use_auto_mask'], remembered['skin_struct']
            ),
            'output_view': OUTVIEW_CHOICES.get(d['v_outview'].get(), 0),
            'output_mix': effective_slider(
                remembered['use_output_mix'], remembered['output_mix']
            ),
        }
        if hasattr(self, "_host_settings"):
            result.update(self._collect_host_settings())
        return result

    def _collect_export_settings(self):
        d = self._export_settings
        def integer(variable, default):
            try:
                return int(variable.get())
            except (ValueError, tk.TclError):
                return default
        return {
            'mode': EXPORT_MODE_CHOICES.get(d['v_mode'].get(), 'single'),
            'workers': max(2, min(4, integer(d['v_workers'], 2))),
            'warmup': max(0, min(120, integer(d['v_warmup'], 8))),
            'decode_buffer': max(1, min(8, integer(d['v_decode_buffer'], 4))),
            'nvenc_preset': NVENC_PRESET_CHOICES.get(d['v_nvenc_preset'].get(), 'p5'),
        }

    def _update_export_control_states(self):
        if not hasattr(self, "_export_settings"):
            return
        state = "normal" if self._collect_export_settings()['mode'] == 'parallel' else "disabled"
        self._export_settings['w_workers'].config(state=state)
        self._export_settings['w_warmup'].config(state=state)
        self._export_settings['w_decode_buffer'].config(state="normal")

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
            "ui_export_open": bool(
                getattr(self, "_export_section", None) and not self._export_section.collapsed
            ),
            "ui_host_open": bool(
                getattr(self, "_host_section", None) and not self._host_section.collapsed
            ),
            "ui_preview_open": bool(
                getattr(self, "_preview_section", None) and not self._preview_section.collapsed
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
        if self._exporting:
            messagebox.showinfo(
                "正在导出", "请先点击“取消导出”，待取消完成后再关闭程序。",
            )
            return
        self._cancel_after("_settings_save_after")
        self._cancel_after("_live_debounce")
        self._cancel_after("_output_preview_after")
        self._cancel_after("_scrub_after")
        self._cancel_after("_resize_after")
        self._save_settings_now()
        self.pause()
        self._wait_play_dlss()
        self._audio.close()
        if getattr(self, "_cap", None):
            self._cap.release()
        self._image_bgr = None
        self._source_kind = None
        self._close_live()
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
        )

    def _settings_hash(self):
        return self._hash_settings_dict(self._collect_settings())

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
                    if self._live:
                        self._live.resize(w, h, int(settings.get('preset', 1)))
                    else:
                        self._live = ProcessLive(w, h, settings)
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
            self.set_status(f"实时预览 · 原始分辨率 {width}×{height}")
        else:
            self.set_status(f"实时预览 {width}×{height} · 暂停后恢复原始分辨率")

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
        if target_size is not None:
            cached = self._cached_dlss_sk(frame, sk, target_size)
            if cached is not None:
                return cached
        fr = source_bgr if source_bgr is not None else self._read_frame(frame)
        if fr is None:
            return None
        source_h, source_w = fr.shape[:2]
        if target_size is None:
            target_size = (source_w, source_h)
        try:
            requested_w, requested_h = map(int, target_size)
        except (TypeError, ValueError):
            requested_w, requested_h = source_w, source_h
        if requested_w <= 0 or requested_h <= 0:
            requested_w, requested_h = source_w, source_h
        target_w, target_h = _fit_preview_size(
            source_w, source_h, max(requested_w, requested_h)
        )
        target_w = min(target_w, requested_w)
        target_h = min(target_h, requested_h)
        cached = self._cached_dlss_sk(frame, sk, (target_w, target_h))
        if cached is not None:
            return cached
        if (target_w, target_h) != (source_w, source_h):
            fr = cv2.resize(fr, (target_w, target_h), interpolation=cv2.INTER_AREA)
        h, w = fr.shape[:2]
        rgb = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
        rgba = np.dstack([rgb, np.full((h, w), 255, np.uint8)])
        with self._live_lock:
            live = self._ensure_live(w, h, settings)
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
            return self._read_frame(frame)
        if view == "DLSS":
            original = self._read_frame(frame)
            if original is None:
                return None
            processed = self._live_dlss_image(frame, source_bgr=original)
            settings = self._collect_settings()
            return compose_preview_frame(
                original, processed,
                settings['output_view'], settings['output_mix'],
            )
        return None

    def _cached_dlss(self, frame, target_size=None):
        target_size = target_size or self._source_size()
        return self._cached_dlss_sk(frame, self._settings_hash(), target_size)

    def _cached_dlss_sk(self, frame, sk, target_size=None):
        target_size = target_size or self._source_size()
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

    def _cache_clear(self):
        with self._cache_lock:
            self._dlss_frame_cache.clear()
            self._source_frame_cache.clear()
            self._queued_preview_frames.clear()
            self._dlss_cache_bytes = 0
            self._source_cache_bytes = 0
            self._live_cache = None
            self._last_shown_dlss = None
            self._preview_processed_frames = 0
            self._preview_process_t0 = None

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
        self.canvas.delete("all")
        self.canvas.create_text(
            cw // 2, ch // 2,
            text="导入",
            fill="#b0b0b0", font=("Microsoft YaHei", 13),
        )

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
                cw // 2, ch // 2, text=msg, fill="#888888", font=("Microsoft YaHei", 11),
            )
            return
        self._draw_fit(img, cw, ch, badge=badge)

    def _canvas_shadow_text(self, x, y, text, fill=HUD_FILL, shadow="#000000", **kwargs):
        self.canvas.create_text(
            x + 1, y + 1, text=text, fill=shadow, **kwargs,
        )
        return self.canvas.create_text(
            x, y, text=text, fill=fill, **kwargs,
        )

    def _draw_fit(self, img, cw, ch, badge=None):
        ih, iw = img.shape[:2]
        scale = min(cw / iw, ch / ih)
        nw, nh = max(int(iw * scale), 1), max(int(ih * scale), 1)
        nimg = cv2.resize(img, (nw, nh))
        from PIL import Image, ImageTk
        self._pilimg = Image.fromarray(cv2.cvtColor(nimg, cv2.COLOR_BGR2RGB))
        self._photo = ImageTk.PhotoImage(self._pilimg)
        ox, oy = (cw - nw) // 2, (ch - nh) // 2
        self._video_geom = (ox, oy, nw, nh)
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)
        if badge:
            self._canvas_shadow_text(
                ox + 10, oy + 14, badge, fill=HUD_FILL, anchor="w",
                font=("Microsoft YaHei", 9),
            )
        if self._dlss_pending:
            self.canvas.create_text(
                cw - 10, ch - 10, text="DLSS…", fill="#888888", anchor="se",
                font=("Microsoft YaHei", 9),
            )

    def _draw_split(self, frame, cw, ch, fast=False):
        size = (cw, ch)
        need = (
            getattr(self, "_split_frame", -1) != frame
            or getattr(self, "_split_size", None) != size
            or getattr(self, "_split_orig", None) is None
        )
        if need:
            orig = self._read_frame(frame)
            if orig is None:
                self.canvas.delete("all")
                self.canvas.create_text(
                    cw // 2, ch // 2, text=f"帧 {frame} 读取失败", fill="#888888",
                    font=("Microsoft YaHei", 11),
                )
                return
            ih, iw = orig.shape[:2]
            scale = min(cw / iw, ch / ih)
            nw, nh = max(int(iw * scale), 1), max(int(ih * scale), 1)
            self._split_nw, self._split_nh = nw, nh
            self._split_orig = cv2.resize(orig, (nw, nh))
            self._split_frame = frame
            self._split_size = size
            if fast:
                self._split_dlss = None
            else:
                dlss = self._live_dlss_image(frame, source_bgr=orig)
                if dlss is None:
                    self._split_dlss = None
                    self._draw_fit(orig, cw, ch, badge="DLSS 生成失败")
                    return
                settings = self._collect_settings()
                dlss = compose_preview_frame(
                    orig, dlss,
                    settings['output_view'], settings['output_mix'],
                )
                self._split_dlss = cv2.resize(dlss, (nw, nh))
        elif not fast and self._split_dlss is None:
            orig = self._read_frame(frame)
            dlss = self._live_dlss_image(frame, source_bgr=orig) if orig is not None else None
            if dlss is None:
                self._draw_fit(self._split_orig, cw, ch, badge="DLSS 生成失败")
                return
            settings = self._collect_settings()
            dlss = compose_preview_frame(
                orig, dlss,
                settings['output_view'], settings['output_mix'],
            )
            self._split_dlss = cv2.resize(dlss, (self._split_nw, self._split_nh))
        self._blit_split(cw, ch)

    def _blit_split(self, cw, ch):
        nw, nh = self._split_nw, self._split_nh
        ox, oy = (cw - nw) // 2, (ch - nh) // 2
        self._video_geom = (ox, oy, nw, nh)
        self._drag_nw = nw
        self._drag_offsetx = ox
        composed = self._split_orig
        show_divider = self.view_var.get() == "对比" and not self._hold_original
        if show_divider:
            composed = self._split_orig.copy()
            sx = int(self.split_x * nw)
            if self._split_dlss is not None:
                composed[:, sx:] = self._split_dlss[:, sx:]
        from PIL import Image, ImageTk
        self._pilimg = Image.fromarray(cv2.cvtColor(composed, cv2.COLOR_BGR2RGB))
        self._photo = ImageTk.PhotoImage(self._pilimg)
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)
        if show_divider:
            sx_abs = ox + int(self.split_x * nw)
            self.canvas.create_line(
                sx_abs, oy, sx_abs, oy + nh, fill=SPLIT_LINE, width=2, tags=("split",),
            )
            handle_y = oy + nh // 2
            self.canvas.create_oval(
                sx_abs - 5, handle_y - 5, sx_abs + 5, handle_y + 5,
                fill=SPLIT_LINE, outline=CANVAS_BG, width=1, tags=("split",),
            )
            self._canvas_shadow_text(
                ox + 10, oy + 14, "原图", fill=HUD_FILL, anchor="w",
                font=("Microsoft YaHei", 9),
            )
            self._canvas_shadow_text(
                ox + nw - 10, oy + 14,
                "DLSS 生成中…" if self._dlss_pending else "DLSS",
                fill=HUD_FILL, anchor="e",
                font=("Microsoft YaHei", 9),
            )
        elif self._hold_original:
            self._canvas_shadow_text(
                ox + 10, oy + 14, "原图（按住 Alt）", fill=HUD_FILL, anchor="w",
                font=("Microsoft YaHei", 9),
            )
        if self._dlss_pending:
            self.canvas.create_text(
                cw - 10, ch - 10, text="DLSS…", fill="#888888", anchor="se",
                font=("Microsoft YaHei", 9),
            )

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
            self.display_view(quality="full")

    def on_canvas_press(self, event):
        if not self.video or self._exporting:
            return
        try:
            self.root.focus_set()
        except Exception:
            pass
        if self._hold_original and not _alt_is_down():
            self._set_hold_original(False)
        shift = bool(event.state & 0x0001)
        if self.view_var.get() == "对比" and (self._near_split(event.x) or shift):
            self._drag_split = True
            self._split_moved = False
            self._canvas_press = ("split", event.x, event.y)
            self.canvas.config(cursor="sb_h_double_arrow")
            self._update_split_from_event(event)
            return
        self._drag_split = False
        kind = "compare" if self.view_var.get() == "对比" else "click"
        self._canvas_press = (kind, event.x, event.y)

    def on_canvas_drag(self, event):
        if not self._drag_split:
            press = self._canvas_press
            if not press or press[0] != "compare":
                return
            dx, dy = event.x - press[1], event.y - press[2]
            if abs(dx) <= 6 or abs(dx) < abs(dy):
                return
            self._drag_split = True
            self.canvas.config(cursor="sb_h_double_arrow")
        self._split_moved = True
        self._update_split_from_event(event)

    def on_canvas_release(self, event):
        if self._drag_split:
            self._drag_split = False
            self._canvas_press = None
            self.on_canvas_hover(event)
            return
        press = self._canvas_press
        self._canvas_press = None
        if not press or press[0] not in ("click", "compare") or not self.video or self._exporting:
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
        if self.video and self._near_split(event.x):
            self.canvas.config(cursor="sb_h_double_arrow")
        else:
            self.canvas.config(cursor="")

    def on_canvas_double(self, event):
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
        try:
            self.root.focus_set()
        except Exception:
            pass
        if self._fullscreen:
            self._exit_fullscreen()
            return
        if self._exporting:
            return
        self._enter_fullscreen()

    def _set_fs_btn(self, fullscreen):
        try:
            self.fs_btn.config(text="退出" if fullscreen else "全屏")
        except Exception:
            pass

    def _enter_fullscreen(self):
        if self._fullscreen:
            return
        self._fullscreen = True
        self._fs_geom = self.root.geometry()
        self._fs_hidden = []
        for widget in (
            self._settings_frame, self._preview_section,
            self._export_section, self._host_section,
            self._export_row, self.log,
        ):
            try:
                info = widget.pack_info()
            except Exception:
                continue
            self._fs_hidden.append((widget, info))
            widget.pack_forget()
        try:
            self.canvas.pack_configure(padx=0, pady=0)
            self.transport.pack_configure(padx=12, pady=(0, 10))
        except Exception:
            pass
        try:
            self.root.attributes("-fullscreen", True)
        except Exception:
            self.root.state("zoomed")
        self._set_fs_btn(True)
        self.root.after_idle(lambda: self.display_view(quality="full"))

    def _exit_fullscreen(self):
        if not self._fullscreen:
            return
        self._fullscreen = False
        try:
            self.root.attributes("-fullscreen", False)
        except Exception:
            pass
        if self._fs_geom:
            try:
                self.root.geometry(self._fs_geom)
            except Exception:
                pass
        try:
            self.canvas.pack_configure(padx=8, pady=4)
            self.transport.pack_configure(padx=8, pady=(0, 4))
        except Exception:
            pass
        for widget, info in self._fs_hidden:
            try:
                widget.pack(**info)
            except Exception:
                pass
        self._fs_hidden = []
        self._set_fs_btn(False)
        self.root.after_idle(lambda: self.display_view(quality="full"))

    def _on_canvas_configure(self, event):
        if event.widget is not self.canvas:
            return
        self._cancel_after("_resize_after")
        self._resize_after = self.root.after(CANVAS_RESIZE_MS, self._apply_canvas_resize)

    def _apply_canvas_resize(self):
        self._resize_after = None
        self._split_size = None
        if self.playing:
            self._present_play_frame(self._frame)
        elif self.video:
            quality = "fast" if self._scrub_after else "full"
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
        delay = max(0, int(self._preview_scrub_ms()))
        self._scrub_after = self.root.after(delay, self._apply_full_preview)

    def _apply_full_preview(self):
        self._scrub_after = None
        if self.playing or not self.video or self._exporting:
            return
        self._display_precise_preview()
        if self._start_paused_prerender():
            self._update_preview_timeline_and_status(force=True)

    def _display_precise_preview(self):
        source_size = self._source_size()
        wants_dlss = self.view_var.get() in ("DLSS", "对比") and not self._hold_original
        if wants_dlss:
            # Playback keeps a canvas-sized split image; invalidate it so compare
            # mode uses the full-resolution cache (or generates it) after pausing.
            self._split_frame = -1
            self._split_dlss = None
        if wants_dlss and self._cached_dlss(self._frame, source_size) is None:
            self.set_status("正在生成原始分辨率精确预览…")
        self.display_view(quality="full")
        if wants_dlss and self._cached_dlss(self._frame, source_size) is not None:
            width, height = source_size
            self.set_status(f"精确预览 · {width}×{height}")

    def _on_timeline_seek(self, frame, phase):
        if not self.video or self._exporting:
            return
        try:
            self.root.focus_set()
        except Exception:
            pass
        if self.playing:
            self.pause()
        if phase in ("start", "move"):
            self._stop_paused_prerender()
            self._goto_frame(frame, quality="fast")
            return
        self._goto_frame(frame, quality="fast")

    def _on_wheel_step(self, event):
        if not self.video or self._exporting:
            return
        delta = -1 if getattr(event, "delta", 0) < 0 else 1
        self.step_frame(delta)
        return "break"

    def step_frame(self, delta):
        if not self.video or self._exporting:
            return
        self.pause()
        self._goto_frame(self._frame + int(delta), quality="full")
        self._schedule_full_preview()
        try:
            self.root.focus_set()
        except Exception:
            pass

    def skip_seconds(self, seconds):
        if not self.video or self._exporting:
            return
        self.pause()
        frames = int(round(float(seconds) * max(self.fps, 1.0)))
        self._goto_frame(self._frame + frames, quality="full")
        self._schedule_full_preview()

    def jump_frame(self, frame):
        if not self.video or self._exporting:
            return
        self.pause()
        if frame < 0:
            frame = self._last_frame_index()
        self._goto_frame(frame, quality="full")
        self._schedule_full_preview()

    def on_frame_entry(self, event=None):
        txt = self.fentry.get().strip()
        try:
            f = int(float(txt))
        except ValueError:
            self.sync_frame_entry()
            return
        self.pause()
        self._goto_frame(f, quality="full")
        self._schedule_full_preview()

    def sync_frame_entry(self):
        try:
            if self.root.focus_get() is self.fentry:
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
            self._active_preview_size = None
            self.set_status("")
            if self.playing and self._buffering:
                self._resume_play_clock(self._frame)
        elif self.playing:
            if self._preview_frame_queue is None or self._prefetch_stop.is_set():
                self._start_strict_preview_buffering()
        self._split_size = None
        self.display_view(quality="full")
        if not self.playing and self.view_var.get() in ("DLSS", "对比"):
            self._schedule_full_preview()

    def on_settings_change(self, event=None):
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
        current_source = self._source_cache_get(self._frame)
        self._wait_play_dlss(timeout=2.0)
        if self._live:
            try:
                with self._live_lock:
                    self._live.update(self._collect_settings())
            except Exception as ex:
                self.logln("[DLSS 参数] " + str(ex))
        self._cache_clear()
        try:
            self.timeline.set_cache_ranges([], [])
        except Exception:
            pass
        if current_source is not None:
            self._source_cache_store(self._frame, current_source)
        self._split_frame = -1
        self._split_dlss = None
        if self.video and not self._is_image:
            self._stop_paused_prerender()
            if self.playing and self.view_var.get() in ("DLSS", "对比"):
                self._start_strict_preview_buffering()
        if self.playing:
            return
        if self.view_var.get() in ("DLSS", "对比"):
            self.display_view(quality="full")
            self._schedule_full_preview()

    def _input_widget_focused(self, extra=()):
        w = self.root.focus_get()
        if w is None:
            return False
        try:
            cls = w.winfo_class()
        except Exception:
            return False
        return cls in _INPUT_WIDGETS or cls in extra

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
        self.root.bind_all("<KeyPress>", self._on_modifier_poll, add="+")
        self.root.bind_all("<KeyRelease>", self._on_modifier_poll, add="+")
        self.root.bind_all("<F11>", self._on_fullscreen_key)
        self.root.bind_all("<Escape>", self._on_escape_key)
        self.root.bind("<FocusOut>", self._on_root_focus_out)

    def _on_step_key(self, delta):
        if self._input_widget_focused() or self._exporting:
            return None
        self.step_frame(delta)
        return "break"

    def _on_skip_key(self, sign):
        if self._input_widget_focused() or self._exporting:
            return None
        self.skip_seconds(sign)
        return "break"

    def _on_jump_key(self, frame):
        if self._input_widget_focused() or self._exporting:
            return None
        self.jump_frame(frame)
        return "break"

    def _on_view_hotkey(self, view):
        if self._input_widget_focused() or self._exporting:
            return None
        if self.view_var.get() != view:
            self.view_var.set(view)
            self.on_view_change()
        return "break"

    def _on_fullscreen_key(self, event=None):
        if self._input_widget_focused():
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
        if event is not None and event.widget is not self.root:
            return
        if self._hold_original and not _alt_is_down():
            self._hold_original = False
            if self.video and not self._exporting and not self.playing:
                self.display_view(quality="full")

    def toggle_play(self):
        try:
            self.root.focus_set()
        except Exception:
            pass
        self._set_hold_original(False)
        if self.playing:
            self.pause()
        else:
            self.play()

    def on_space(self, event=None):
        if self._input_widget_focused(extra=_SPACE_PASSTHROUGH):
            return None
        self.toggle_play()
        return "break"

    def _set_play_btn(self, playing):
        try:
            if playing and self._buffering:
                text = "停止等待"
            else:
                text = "⏸ 暂停" if playing else "▶ 播放"
            self.play_btn.config(text=text)
        except Exception:
            pass

    def toggle_mute(self):
        try:
            self.root.focus_set()
        except Exception:
            pass
        self._audio.set_muted(not self._audio.muted)
        try:
            self.mute_btn.config(text="静" if self._audio.muted else "音")
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
        self._start_prefetch()
        self._queue_preview_frame(self._frame, source)
        self._enter_preview_buffering(self._frame)
        self._schedule_preview_decode(0)
        return True

    def _preview_session_active(self):
        return bool(
            (self.playing or self._pre_rendering)
            and self.video and not self._exporting and not self._is_image
            and self.view_var.get() in ("DLSS", "对比")
        )

    def _stop_paused_prerender(self):
        self._pre_rendering = False
        self._cancel_after("_preview_decode_after")
        self._prefetch_stop.set()
        self._preview_frame_queue = None
        with self._cache_lock:
            self._queued_preview_frames.clear()

    def _start_paused_prerender(self):
        if self.playing or not self.video or self._exporting or self._is_image:
            return False
        if self.view_var.get() not in ("DLSS", "对比") or self._hold_original:
            return False
        self._stop_paused_prerender()
        self._pre_rendering = True
        self._active_preview_size = self._playback_preview_size()
        source = self._source_cache_get(self._frame)
        if source is None:
            source = self._read_frame(self._frame)
        if source is None:
            self._pre_rendering = False
            return False
        self._source_cache_store(self._frame, source)
        self._start_prefetch()
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
            self._preview_decode_after = self.root.after(delay, self._preview_decode_tick)

    def _preview_decode_tick(self):
        self._preview_decode_after = None
        if not self._preview_session_active():
            return
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
            self._update_preview_timeline_and_status()
            self._schedule_preview_decode(20)
            return
        if self._pre_rendering and not self.playing:
            self._pre_rendering = False
            self._prefetch_stop.set()
            self._preview_frame_queue = None
            self._update_preview_timeline_and_status(force=True)
            return
        self._update_preview_timeline_and_status(force=True)
        self._schedule_preview_decode(20)

    def _update_preview_timeline_and_status(self, force=False):
        if not self.video or self.view_var.get() not in ("DLSS", "对比"):
            return
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
        now = time.perf_counter()
        if not force and now - self._preview_status_at < 0.2:
            return
        self._preview_status_at = now
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
        ih, iw = orig.shape[:2]
        scale = min(cw / iw, ch / ih)
        nw, nh = max(int(iw * scale), 1), max(int(ih * scale), 1)
        self._split_nw, self._split_nh = nw, nh
        self._split_orig = cv2.resize(orig, (nw, nh))
        if dlss is None and pending:
            dlss = self._pending_preview_image(orig)
        self._split_dlss = None if dlss is None else cv2.resize(dlss, (nw, nh))
        self._split_frame = self._frame
        self._split_size = (cw, ch)
        self._dlss_pending = bool(pending or self._split_dlss is None)
        self._blit_split(cw, ch)

    def _start_prefetch(self):
        if not self._preview_session_active():
            return
        if self.view_var.get() == "原图":
            return
        self._prefetch_stop.set()
        self._prefetch_gen += 1
        gen = self._prefetch_gen
        stop = threading.Event()
        self._prefetch_stop = stop
        self._preview_worker_error = None
        settings = self._collect_settings()
        preview_size = self._playback_preview_size()
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
            bgr = cv2.cvtColor(output[..., :3], cv2.COLOR_RGB2BGR)
            self._cache_store(frame, sk, bgr)
            with self._cache_lock:
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
                if (target_w, target_h) != (source_w, source_h):
                    bgr = cv2.resize(
                        bgr, (target_w, target_h), interpolation=cv2.INTER_AREA,
                    )
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                rgba = np.dstack([
                    rgb, np.full((target_h, target_w), 255, np.uint8),
                ])
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
                self._last_dlss_frame = frame
        except Exception as ex:
            self._preview_worker_error = str(ex)
        finally:
            try:
                keep_results = gen == self._prefetch_gen
                while pending:
                    receive_one(store=keep_results)
            except Exception as ex:
                self._preview_worker_error = str(ex)
            if gen == self._prefetch_gen:
                self._play_dlss_busy = False

    def _wait_play_dlss(self, timeout=5.0):
        self._cancel_after("_preview_decode_after")
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
        path = filedialog.askopenfilename(filetypes=VIDEO_FILETYPES)
        if path:
            self._load_media(path)

    def _setup_drag_and_drop(self):
        """Register both the window and video-facing widgets as file drop targets."""
        if DND_FILES is None:
            self.logln("[拖拽] tkinterdnd2 未安装；仍可点击“导入”。运行 setup.bat 可启用拖拽。")
            return
        try:
            for widget in (self.root, self.import_btn, self.canvas):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
                widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
                widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as ex:
            self.logln("[拖拽] 初始化失败，仍可点击导入: " + str(ex))

    def _on_drop_enter(self, event):
        if not self._exporting:
            self.canvas.config(bg=CANVAS_DROP_BG)
            self.set_status("松开鼠标以导入")
        return getattr(event, "action", None)

    def _on_drop_leave(self, event):
        self.canvas.config(bg=CANVAS_BG)
        if not self._exporting:
            self.set_status("就绪")
        return getattr(event, "action", None)

    def _on_drop(self, event):
        self.canvas.config(bg=CANVAS_BG)
        if self._exporting:
            messagebox.showinfo("忙", "正在导出，请结束后再导入。")
            return getattr(event, "action", None)
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except Exception:
            paths = [str(getattr(event, "data", "")).strip("{} \"")]
        if len(paths) != 1:
            messagebox.showwarning("拖拽导入", "一次只能导入一个文件。")
        elif paths[0]:
            self._load_media(paths[0])
        return getattr(event, "action", None)

    def _begin_source_load(self):
        self.pause()
        self._wait_play_dlss()
        self._audio.close()
        self._cache_clear()
        self._last_dlss_frame = -1
        self._play_orig = None
        self._split_frame = -1
        self._split_orig = None
        self._split_dlss = None
        self._cancel_after("_scrub_after")
        if getattr(self, "_cap", None):
            self._cap.release()
        self._cap = None
        self._cap_next = None
        self._image_bgr = None
        self._source_kind = None
        self._media_w = 0
        self._media_h = 0
        self._active_preview_size = None
        self._preview_decode_next = None
        self._buffering = False
        self._buffer_started_at = None
        self.video = None
        self.nframes = 0
        self.fps = 30.0
        self._frame = 0
        try:
            self.timeline.set_cache_ranges([], [])
        except Exception:
            pass

    def clear_media(self):
        if self._exporting:
            messagebox.showinfo("忙", "正在导出，请结束后再清空。")
            return
        if not self.video and self._live is None:
            return
        self._begin_source_load()
        self._close_live()
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
        self.root.title(f"{APP_TITLE} — 实时预览 + 导出")
        self._update_action_labels()
        self._draw_empty()
        self.set_status("就绪")
        self.logln("已清空导入，解码/音轨/DLSS 主机已释放")

    def _load_media(self, path):
        if self._exporting:
            messagebox.showinfo("忙", "正在导出，请稍候。")
            return False
        path = os.path.abspath(os.path.normpath(path))
        if not os.path.isfile(path):
            messagebox.showerror("导入失败", "找不到拖入的文件：\n" + path)
            self.set_status("导入失败：文件不存在")
            return False
        if _is_image_path(path):
            return self._load_image(path)
        if _is_video_path(path):
            return self._load_video(path)
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
        self.root.title(f"{APP_TITLE} — {os.path.basename(path)}")
        self.timeline.set_range(0, 0)
        self.timeline.set(0)
        self._sync_transport_labels()
        self._update_action_labels()
        try:
            self.display_view(quality="full")
        except Exception as ex:
            self.logln(f"[preview] {ex}")
        self.logln(f"已导入图片: {path}  ({w}×{h})")
        self.set_status(f"图片 · {w}×{h}")
        return True

    def _load_video(self, path):
        """Validate and load a video from either the file dialog or drag-and-drop."""
        if self._exporting:
            messagebox.showinfo("忙", "正在导出，请稍候。")
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

        # Keep a same-size worker for temporal continuity.  A resolution change is
        # handled by replacing only the isolated NGX process in _ensure_live().
        self._begin_source_load()
        self.video = path
        self._cap = new_cap
        self._cap_next = 0
        self._source_kind = "video"
        self._media_w, self._media_h = w, h
        self.nframes, self.fps = n, fps
        self._update_preview_memory_hint()
        last = max(n - 1, 0)
        self._frame = 0
        self.root.title(f"{APP_TITLE} — {os.path.basename(self.video)}")
        self.timeline.set_range(0, last)
        self.timeline.set(0)
        self._sync_transport_labels()
        self._update_action_labels()
        try:
            self.display_view(quality="full")
        except Exception as ex:
            self.logln(f"[preview] {ex}")
        self.logln(f"已导入: {self.video}  ({n} 帧)")
        duration = n / max(float(fps) or 30.0, 1.0)
        self._audio.prepare(path, duration, callback=self._audio_ready_cb)
        self._schedule_full_preview()
        if not self._hinted_keys:
            self.set_status("空格播放/暂停 · ← → 逐帧 · Alt 对照原图 · F11 全屏")
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
        self._wait_play_dlss()
        if self._fullscreen:
            self._exit_fullscreen()
        self._export_cancel_event.clear()
        self._exporting = True
        self._update_action_labels()
        self._update_host_control_states()
        self._export_t0 = time.perf_counter()
        self._export_ema_fps = None
        self.set_status(status_text)

    def _end_export_ui(
        self, success, out_path, done_label="完成", done_message=None, cancelled=False,
        completed_items=1,
    ):
        self._exporting = False
        self._export_cancel_event.clear()
        self._update_action_labels()
        self._update_host_control_states()
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
            messagebox.showinfo("导出", done_message or ("已导出:\n" + out_path))
        else:
            self.pbar["value"] = 0
            try:
                self.eta_label.config(text="")
            except Exception:
                pass
            self.set_status("导出失败，请查看日志")
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
        if not self._exporting or self._is_image or self._export_cancel_event.is_set():
            return
        self._export_cancel_event.set()
        self._update_action_labels()
        self.set_status("正在取消导出…")
        self.logln("[导出] 用户请求取消，正在停止导出流水线…")

    def _raise_if_export_cancelled(self):
        if self._export_cancel_event.is_set():
            raise _ExportCancelled()

    def _export_image(self):
        settings = self._collect_settings()
        self._save_settings_now()
        orig = self._image_bgr
        if orig is None:
            messagebox.showwarning("提示", "请先导入图片")
            return
        ext = os.path.splitext(self.video)[1].lower() or ".png"
        default_out = os.path.splitext(self.video)[0] + "_dlss" + ext
        out_path = self._unique_output_path(self.video, ext)
        if out_path != default_out:
            self.logln("[导出] 目标文件已存在，自动改名为: " + os.path.basename(out_path))
        self._begin_export_ui("正在导出图片…")
        success = False
        started_at = self._export_t0
        try:
            processed = self._live_dlss_image(0, source_bgr=orig, settings=settings)
            if processed is None:
                raise RuntimeError("DLSS 处理失败")
            view = settings["output_view"]
            mix = float(settings["output_mix"])
            composed = compose_output_frame(orig, processed, view, mix)
            out_path = _write_image_bgr(out_path, composed)
            success = True
            elapsed = time.perf_counter() - started_at
            h, w = composed.shape[:2]
            self.logln(f"[导出] 图片 {w}×{h}；用时 {elapsed:.2f} 秒")
            self.set_progress(1, 1, "完成")
        except Exception as ex:
            traceback.print_exc()
            self.logln("导出错误: " + str(ex))
        self._end_export_ui(
            success, out_path, "完成",
            "已导出图片:\n" + out_path if success else None,
        )

    # ---------- export ----------
    def export_dlss(self):
        if not self.video:
            messagebox.showwarning("提示", "请先导入视频或图片")
            return
        if self._exporting or (self.thread and self.thread.is_alive()):
            messagebox.showinfo("忙", "上一个任务还没结束")
            return
        if self._is_image:
            self._export_image()
            return
        settings = self._collect_settings()
        export_settings = self._collect_export_settings()
        self._save_settings_now()
        n, fps, w, h = self._video_info(self.video)
        view = settings['output_view']; mix = float(settings['output_mix'])
        live = None; writer = None
        default_out_path = os.path.splitext(self.video)[0] + "_dlss.mp4"
        out_path = self._unique_output_path(self.video)
        if out_path != default_out_path:
            self.logln("[导出] 目标文件已存在，自动改名为: " + os.path.basename(out_path))
        self._begin_export_ui("正在流水线导出（CPU 解码 + GPU DLSS/NVENC）...")
        success = False
        cancelled = False
        started_at = self._export_t0
        dlss_seconds = 0.0
        exported_frames = 0
        try:
            if export_settings['mode'] == 'parallel':
                self.logln(
                    f"[导出] 视觉无损并行模式: {export_settings['workers']} 进程，"
                    f"预热 {export_settings['warmup']} 帧"
                )
                result = export_parallel(
                    self.video, out_path, settings,
                    workers=export_settings['workers'],
                    warmup=export_settings['warmup'],
                    nvenc_preset=export_settings['nvenc_preset'],
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
                    out_path, w, h, fps, audio_source=self.video,
                    nvenc_preset=export_settings['nvenc_preset'],
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

                    for i, fr in self._iter_frames(decode_buffer):
                        self._raise_if_export_cancelled()
                        hh, ww = fr.shape[:2]
                        rgba = cv2.cvtColor(fr, cv2.COLOR_BGR2RGBA)
                        if live is None:
                            live = self._ensure_live(ww, hh)
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
            self.logln("导出错误: " + str(ex))
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
        )

    def _iter_frames(self, buffer_size=4):
        """Decode ahead on a worker so CPU decode overlaps the ordered DLSS stage."""
        frame_queue = queue.Queue(maxsize=max(int(buffer_size), 1))
        stop_event = threading.Event()
        decode_errors = []
        video_path = self.video

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
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    # Required for ProcessLive's spawn worker in a PyInstaller build.
    multiprocessing.freeze_support()
    if "--parallel-worker" in sys.argv:
        sys.argv.remove("--parallel-worker")
        from parallel_export_worker import main as parallel_worker_main
        parallel_worker_main()
    else:
        main()
