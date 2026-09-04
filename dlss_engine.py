#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dlss_engine.py — ctypes wrapper for the test4 DLSS5 Feature 18 host DLL (zero-guidance).

The Feature 18 ("neural render") ignores depth/flow guidance in this config, so this
engine always feeds ZERO guidance. It only needs the colour frames from the video.
"""
import ctypes
import os
import sys
import numpy as np

# resolve the bundle dir: PyInstaller (frozen) -> _MEIPASS, else the script's own dir
if getattr(sys, "frozen", False):
    BASE = sys._MEIPASS
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
HOST_DLL_LEGACY = os.path.join(BASE, "dlssnr_host.dll")
HOST_DLL_V2 = os.path.join(BASE, "dlssnr_host_v2.dll")
# Kept for callers that imported the old constant.
HOST_DLL = HOST_DLL_LEGACY
DLSSNR_DLL = os.path.join(BASE, "nvngx_dlssnr.dll")
LOG_PATH = os.path.join(BASE, "dlss_run.log")

_libraries = {}

FRAME_FORMAT_RGBA8 = "rgba8"
FRAME_FORMAT_RGBA16F = "rgba16f"
COLOR_PROFILES = {"srgb": 0, "scrgb": 1, "hdr10_pq": 2, "hdr10_hlg": 3}


def frame_dtype(settings=None):
    return np.float16 if (settings or {}).get("frame_format") == FRAME_FORMAT_RGBA16F else np.uint8


def frame_format_id(settings=None):
    return 1 if frame_dtype(settings) == np.float16 else 0


def color_profile_id(settings=None):
    return COLOR_PROFILES.get(str((settings or {}).get("color_profile", "srgb")), 0)


def frame_contract(settings=None):
    return frame_format_id(settings), color_profile_id(settings)


def _bind_library(lib):
    if getattr(lib, "_dlss5tool_bound", False):
        return lib
    lib.dlssnr_init.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_wchar_p, ctypes.c_wchar_p,
    ]
    lib.dlssnr_init.restype = ctypes.c_int
    lib.dlssnr_create_feature.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.dlssnr_create_feature.restype = ctypes.c_int
    lib.dlssnr_process.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_int,
    ]
    lib.dlssnr_process.restype = ctypes.c_int
    lib.dlssnr_set_options.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.c_float,
        ctypes.c_float, ctypes.c_float, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.c_float,
    ]
    lib.dlssnr_set_options.restype = None
    lib.dlssnr_shutdown.argtypes = []
    lib.dlssnr_shutdown.restype = None
    lib.dlssnr_resize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.dlssnr_resize.restype = ctypes.c_int
    if hasattr(lib, "dlssnr_configure"):
        lib.dlssnr_configure.argtypes = [ctypes.c_int] * 5
        lib.dlssnr_configure.restype = None
        lib.dlssnr_capabilities.argtypes = []
        lib.dlssnr_capabilities.restype = ctypes.c_int
        lib.dlssnr_enqueue.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
        ]
        lib.dlssnr_enqueue.restype = ctypes.c_int
        lib.dlssnr_dequeue.argtypes = [ctypes.c_void_p]
        lib.dlssnr_dequeue.restype = ctypes.c_int
        lib.dlssnr_pending.argtypes = []
        lib.dlssnr_pending.restype = ctypes.c_int
    if hasattr(lib, "dlssnr_configure_format"):
        lib.dlssnr_configure_format.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.dlssnr_configure_format.restype = None
    lib._dlss5tool_bound = True
    return lib


def _load_backend(name):
    path = HOST_DLL_V2 if name == "v2" else HOST_DLL_LEGACY
    if not os.path.exists(path):
        raise FileNotFoundError("missing %s" % path)
    key = os.path.normcase(os.path.abspath(path))
    if key not in _libraries:
        _libraries[key] = _bind_library(ctypes.CDLL(path))
    return _libraries[key], name


def _load(settings=None, forced=None):
    preference = forced or str((settings or {}).get("host_backend", "auto"))
    if preference == "auto":
        preference = "v2" if os.path.exists(HOST_DLL_V2) else "legacy"
    if frame_format_id(settings) == 1 and preference != "v2":
        raise RuntimeError("HDR/scRGB RGBA16F 处理需要 v2 主机")
    if preference not in {"v2", "legacy"}:
        preference = "legacy"
    return _load_backend(preference)


def _host_config(settings):
    s = settings or {}
    return (
        bool(s.get("host_zero_fast_path", True)),
        bool(s.get("host_persistent_buffers", True)),
        str(s.get("host_submission", "merged")) == "merged",
        max(1, min(3, int(s.get("host_in_flight", 2)))),
        bool(s.get("host_auto_fallback", True)),
    )


def _configure_host(lib, settings):
    if hasattr(lib, "dlssnr_configure"):
        zero_fast, persistent, merged, in_flight, fallback = _host_config(settings)
        lib.dlssnr_configure(
            int(zero_fast), int(persistent), int(merged),
            in_flight, int(fallback),
        )
    format_id, profile_id = frame_contract(settings)
    if format_id == 1:
        if not hasattr(lib, "dlssnr_configure_format"):
            raise RuntimeError("当前主机不支持 RGBA16F/HDR，请重新编译 v2 主机")
        lib.dlssnr_configure_format(format_id, profile_id)
    elif hasattr(lib, "dlssnr_configure_format"):
        lib.dlssnr_configure_format(format_id, profile_id)


# settings dict keys that actually change the output on this Feature 18 config:
#   style (int), intensity/local_tone/local_struct (0..1), use_auto_mask,
#   skin_struct (effective with auto mask), output_view (0/1/2), output_mix (0..1).
def _set_options(lib, s):
    lib.dlssnr_set_options(
        int(s.get('preset', 1)),          # preset: inert at same-res, keep 1
        int(s.get('style', 0)),
        float(s.get('intensity', 1.0)),
        float(s.get('local_tone', 1.0)),
        float(s.get('local_struct', 1.0)),
        float(s.get('skin_struct', 0.5)),
        int(s.get('use_auto_mask', 0)),
        int(s.get('ui_correction', 0)),    # inert
        0,                                 # guidance_mode ALWAYS 0 (off) — NR ignores guidance
        int(s.get('depth_convention', 2)), # inert (depth ignored)
        float(s.get('motion_scale_x', 1.0)),
        float(s.get('motion_scale_y', 1.0)))


def _apply_output_view(processed, color, view, mix, w, h):
    """Post-process the DLSS RGBA8 output per Output View (0=Processed,1=DiffX10,2=L/R Compare)."""
    out = []
    for pr, co in zip(processed, color):
        cof = co[..., :3].astype(np.float32) / 255.0
        prf = pr[..., :3].astype(np.float32) / 255.0
        if view == 1:      # Difference x10
            r = np.clip(0.5 + (prf - cof) * 10.0, 0, 1)
        elif view == 2:    # Left / Right compare
            r = prf.copy()
            r[:, :w // 2] = cof[:, :w // 2]
            if w % 2 == 1:
                r[:, w // 2] = 1.0
        else:              # Processed, blended by mix
            r = cof + (prf - cof) * mix
        res = np.dstack([r, np.ones((h, w), np.float32)])
        out.append((res * 255.0).clip(0, 255).astype(np.uint8))
    return out


class Live:
    """Persistent single-frame DLSS session for realtime preview. init+create the feature
    once, then process() per frame. Style/intensity/local_* apply at the next process;
    changing 'preset' recreates the feature. close() releases the D3D12 device."""
    def __init__(self, w, h, settings=None):
        self._w, self._h = w, h
        self.settings = dict(settings or {})
        self._preference = str(self.settings.get("host_backend", "auto"))
        self._lib, self.backend = _load(self.settings)
        self._open_with_fallback()
        self._allocate_buffers()

    def _allocate_buffers(self):
        """Allocate the large zero-guidance/output buffers once per resolution."""
        self._mv = np.zeros((self._h, self._w, 2), np.float32)
        self._dp = np.zeros((self._h, self._w), np.float32)
        self._output = np.empty(
            (self._h, self._w, 4), frame_dtype(self.settings),
        )

    def _open(self):
        s = self.settings
        _configure_host(self._lib, s)
        _set_options(self._lib, s)          # push preset before create
        try:
            self._lib.dlssnr_shutdown()
        except Exception:
            pass
        if not self._lib.dlssnr_init(self._w, self._h, int(s.get('preset', 1)), DLSSNR_DLL, LOG_PATH):
            raise RuntimeError("dlssnr_init failed (D3D12/gate). See dlss_run.log")
        if not self._lib.dlssnr_create_feature(self._w, self._h, int(s.get('preset', 1))):
            log = open(LOG_PATH).read() if os.path.exists(LOG_PATH) else ""
            raise RuntimeError("Feature 18 create failed.\n" + log[-800:])
        self._config = _host_config(s)
        self._refresh_capabilities()

    def _open_with_fallback(self):
        try:
            self._open()
        except Exception:
            allow = bool(self.settings.get("host_auto_fallback", True))
            if self._preference != "auto" or self.backend != "v2" or not allow:
                raise
            try:
                self._lib.dlssnr_shutdown()
            except Exception:
                pass
            self._lib, self.backend = _load_backend("legacy")
            self._open()

    def _refresh_capabilities(self):
        capabilities = (
            int(self._lib.dlssnr_capabilities())
            if hasattr(self._lib, "dlssnr_capabilities") else 0
        )
        requested = max(1, min(3, int(self.settings.get("host_in_flight", 2))))
        self.max_in_flight = requested if capabilities & 2 else 1
        self.supports_async = self.max_in_flight > 1

    def update(self, settings):
        old_preset = self.settings.get('preset')
        old_config = getattr(self, "_config", _host_config(self.settings))
        old_contract = frame_contract(self.settings)
        self.settings.update(settings)
        requested_backend = str(self.settings.get("host_backend", self._preference))
        if requested_backend not in {"auto", self.backend}:
            raise RuntimeError(
                "进程内 Live 不支持切换主机后端；请新建会话或使用 ProcessLive。"
            )
        new_config = _host_config(self.settings)
        _configure_host(self._lib, self.settings)
        if frame_contract(self.settings) != old_contract:
            self.resize(self._w, self._h, int(self.settings.get('preset', 1)))
        elif self.settings.get('preset') != old_preset:
            self.resize(self._w, self._h, int(self.settings.get('preset', 1)))
        elif new_config != old_config and self.backend == "v2":
            self.resize(self._w, self._h, int(self.settings.get('preset', 1)))
        self._config = new_config
        self._refresh_capabilities()

    def resize(self, w, h, preset=None):
        """Re-create the Feature 18 for a new frame size WITHOUT re-running the NGX core
        init (which is one-time per process and crashes if re-initialized)."""
        if preset is None:
            preset = int(self.settings.get('preset', 1))
        self.settings['preset'] = preset
        _set_options(self._lib, self.settings)
        if not self._lib.dlssnr_resize(w, h, preset):
            log = open(LOG_PATH).read() if os.path.exists(LOG_PATH) else ""
            raise RuntimeError("Feature 18 resize failed.\n" + log[-800:])
        self._w, self._h = w, h
        self._config = _host_config(self.settings)
        self._refresh_capabilities()
        self._allocate_buffers()

    def process(self, rgba, reset=False):
        _set_options(self._lib, self.settings)
        h, w = rgba.shape[:2]
        expected_dtype = frame_dtype(self.settings)
        if rgba.dtype != expected_dtype or rgba.shape != (self._h, self._w, 4):
            raise ValueError(
                "RGBA frame must be %s with shape (%d, %d, 4), got %s/%s"
                % (np.dtype(expected_dtype).name, self._h, self._w, rgba.shape, rgba.dtype))
        if not rgba.flags.c_contiguous:
            rgba = np.ascontiguousarray(rgba)
        ok = self._lib.dlssnr_process(
            rgba.ctypes.data_as(ctypes.c_void_p),
            self._mv.ctypes.data_as(ctypes.c_void_p),
            self._dp.ctypes.data_as(ctypes.c_void_p),
            self._output.ctypes.data_as(ctypes.c_void_p),
            1 if reset else 0)
        return self._output if ok else None

    def enqueue(self, rgba, reset=False):
        """Queue one ordered frame on v2 without waiting for its readback."""
        if not self.supports_async:
            raise RuntimeError("当前主机设置不支持异步帧队列")
        _set_options(self._lib, self.settings)
        if rgba.dtype != frame_dtype(self.settings) or rgba.shape != (self._h, self._w, 4):
            raise ValueError("RGBA frame shape/dtype mismatch")
        if not rgba.flags.c_contiguous:
            rgba = np.ascontiguousarray(rgba)
        return bool(self._lib.dlssnr_enqueue(
            rgba.ctypes.data_as(ctypes.c_void_p),
            self._mv.ctypes.data_as(ctypes.c_void_p),
            self._dp.ctypes.data_as(ctypes.c_void_p),
            1 if reset else 0,
        ))

    def dequeue(self):
        """Wait for and return the oldest v2 frame in submission order."""
        if not self.supports_async:
            raise RuntimeError("当前主机设置不支持异步帧队列")
        ok = self._lib.dlssnr_dequeue(
            self._output.ctypes.data_as(ctypes.c_void_p)
        )
        return self._output if ok else None

    @property
    def pending(self):
        if not hasattr(self._lib, "dlssnr_pending"):
            return 0
        return int(self._lib.dlssnr_pending())

    def close(self):
        try:
            self._lib.dlssnr_shutdown()
        except Exception:
            pass


def run_dlss(rgba_frames, settings=None, reset=True, progress=None):
    """Batch-generate DLSS for a list of HxWx4 rgba frames (zero guidance). Returns HxWx4 list."""
    settings = settings or {}
    h, w = rgba_frames[0].shape[:2]
    live = Live(w, h, settings)
    out = []
    for i, rgba in enumerate(rgba_frames):
        processed = live.process(rgba, reset=(reset and i == 0))
        if processed is None:
            raise RuntimeError("DLSS processing failed at frame %d" % i)
        if progress:
            progress(i, len(rgba_frames), "ok")
        out.append(processed.copy())
    view = settings.get('output_view', 0)
    mix = float(settings.get('output_mix', 1.0))
    if view != 0 or mix < 1.0:
        out = _apply_output_view(out, rgba_frames, view, mix, w, h)
    return out
