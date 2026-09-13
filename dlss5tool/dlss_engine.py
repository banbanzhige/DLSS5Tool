#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dlss_engine.py — Feature 18 host wrapper. Guidance is optional and disabled by default.
"""
import ctypes
import os
import sys
import numpy as np
from dlss5tool import mod_paths
from dlss5tool import guidance_client
from dlss5tool.guidance_public import normalize_public_settings
from dlss5tool import i18n
from dlss5tool import paths
from dlss5tool.host_queue import clamp_in_flight, LEGACY_MAX_IN_FLIGHT
from dlss5tool.guidance_color import analysis_rgba8

BASE = str(paths.runtime_root())
HOST_DLL_LEGACY = os.path.join(BASE, "dlssnr_host.dll")
HOST_DLL_V2 = os.path.join(BASE, "dlssnr_host_v2.dll")
# Kept for callers that imported the old constant.
HOST_DLL = HOST_DLL_LEGACY
DLSSNR_DLL = mod_paths.runtime_path()
LOG_PATH = (os.path.join(BASE, "dlss_run.log") if getattr(sys, "frozen", False)
            else str(paths.state_path("dlss_run.log")))

_libraries = {}

FRAME_FORMAT_RGBA8 = "rgba8"
FRAME_FORMAT_RGBA16F = "rgba16f"
COLOR_PROFILES = {"srgb": 0, "scrgb": 1, "hdr10_pq": 2, "hdr10_hlg": 3}
DEFAULT_TILE_WIDTH = 6000
DEFAULT_TILE_HEIGHT = 3000
RENDER_GPU_AUTO = "auto"
_NVIDIA_VENDOR_ID = 0x10DE
_ADAPTER_SOFTWARE = 1
_ADAPTER_D3D12_LEVEL_11 = 2


class _NativeAdapterInfo(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("preference_index", ctypes.c_uint32),
        ("vendor_id", ctypes.c_uint32),
        ("device_id", ctypes.c_uint32),
        ("subsys_id", ctypes.c_uint32),
        ("revision", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("dedicated_video_memory", ctypes.c_uint64),
        ("luid_high", ctypes.c_int32),
        ("luid_low", ctypes.c_uint32),
        ("description", ctypes.c_wchar * 128),
    ]


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
    if hasattr(lib, "dlssnr_configure_tiling"):
        lib.dlssnr_configure_tiling.argtypes = [ctypes.c_int] * 3
        lib.dlssnr_configure_tiling.restype = None
    if hasattr(lib, "dlssnr_queue_capacity"):
        lib.dlssnr_queue_capacity.argtypes = []
        lib.dlssnr_queue_capacity.restype = ctypes.c_int
    if hasattr(lib, "dlssnr_configure_format"):
        lib.dlssnr_configure_format.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.dlssnr_configure_format.restype = None
    if hasattr(lib, "dlssnr_enumerate_adapters"):
        lib.dlssnr_enumerate_adapters.argtypes = [
            ctypes.POINTER(_NativeAdapterInfo), ctypes.c_int,
        ]
        lib.dlssnr_enumerate_adapters.restype = ctypes.c_int
    if hasattr(lib, "dlssnr_select_adapter"):
        lib.dlssnr_select_adapter.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        lib.dlssnr_select_adapter.restype = None
        lib.dlssnr_select_adapter_auto.argtypes = []
        lib.dlssnr_select_adapter_auto.restype = None
        lib.dlssnr_selected_adapter.argtypes = [ctypes.POINTER(_NativeAdapterInfo)]
        lib.dlssnr_selected_adapter.restype = ctypes.c_int
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


def _cuda_adapter_luids():
    """Map physical CUDA devices to DXGI LUIDs without importing CUDA toolkits."""
    if os.name != "nt":
        return {}
    try:
        cuda = ctypes.WinDLL("nvcuda.dll")
        cuda.cuInit.argtypes = [ctypes.c_uint]
        cuda.cuInit.restype = ctypes.c_int
        if cuda.cuInit(0) != 0:
            return {}
        count_fn = getattr(cuda, "cuDeviceGetCount_v2", None) or cuda.cuDeviceGetCount
        count_fn.argtypes = [ctypes.POINTER(ctypes.c_int)]
        count_fn.restype = ctypes.c_int
        count = ctypes.c_int()
        if count_fn(ctypes.byref(count)) != 0 or count.value < 0 or count.value > 64:
            return {}
        cuda.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        cuda.cuDeviceGet.restype = ctypes.c_int
        cuda.cuDeviceGetLuid.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.c_int,
        ]
        cuda.cuDeviceGetLuid.restype = ctypes.c_int
        bus_fn = getattr(cuda, "cuDeviceGetPCIBusId", None)
        if bus_fn is not None:
            bus_fn.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
            bus_fn.restype = ctypes.c_int
        result = {}
        for ordinal in range(count.value):
            device = ctypes.c_int()
            luid = (ctypes.c_ubyte * 8)()
            node_mask = ctypes.c_uint()
            if cuda.cuDeviceGet(ctypes.byref(device), ordinal) != 0:
                continue
            if cuda.cuDeviceGetLuid(luid, ctypes.byref(node_mask), device.value) != 0:
                continue
            luid_low = int.from_bytes(bytes(luid[:4]), "little", signed=False)
            luid_high = int.from_bytes(bytes(luid[4:]), "little", signed=True)
            pci_bus_id = ""
            if bus_fn is not None:
                bus = ctypes.create_string_buffer(32)
                if bus_fn(bus, len(bus), device.value) == 0:
                    pci_bus_id = bus.value.decode("ascii", errors="replace")
            result[(luid_high, luid_low)] = {
                "cuda_index": ordinal,
                "pci_bus_id": pci_bus_id,
            }
        return result
    except (AttributeError, OSError, TypeError, ValueError):
        return {}


def _adapter_records(native_infos, cuda_luids=None):
    """Return stable, persistable NVIDIA D3D12 choices from native DXGI data."""
    records = []
    seen = set()
    cuda_luids = dict(cuda_luids or {})
    for native in native_infos:
        if (
            int(native.vendor_id) != _NVIDIA_VENDOR_ID
            or int(native.flags) & _ADAPTER_SOFTWARE
            or not int(native.flags) & _ADAPTER_D3D12_LEVEL_11
        ):
            continue
        base = "dxgi:%04X:%04X:%08X:%08X:%016X" % (
            int(native.vendor_id), int(native.device_id), int(native.subsys_id),
            int(native.revision), int(native.dedicated_video_memory),
        )
        luid = (int(native.luid_high), int(native.luid_low))
        cuda = cuda_luids.get(luid)
        if cuda_luids and cuda is None:
            continue
        pci_token = "".join(
            character for character in str((cuda or {}).get("pci_bus_id", "")).upper()
            if character in "0123456789ABCDEF"
        )
        suffix = (
            ":P" + pci_token if len(pci_token) == 9
            else ":C%d" % cuda["cuda_index"] if cuda else ""
        )
        adapter_id = base + suffix
        # Hybrid/partitioned Windows drivers can publish several DXGI LUIDs for
        # one physical adapter. CUDA's driver LUID identifies physical NVIDIA
        # devices; when unavailable, retain the first high-performance view.
        if adapter_id in seen:
            continue
        seen.add(adapter_id)
        records.append({
            "id": adapter_id,
            "name": str(native.description).strip() or "NVIDIA GPU",
            "preference_index": int(native.preference_index),
            "vendor_id": int(native.vendor_id),
            "device_id": int(native.device_id),
            "subsys_id": int(native.subsys_id),
            "revision": int(native.revision),
            "dedicated_video_memory": int(native.dedicated_video_memory),
            "luid_high": int(native.luid_high),
            "luid_low": int(native.luid_low),
            "d3d12_feature_level": "12_0" if int(native.flags) & 4 else "11_0",
            "cuda_index": cuda["cuda_index"] if cuda else None,
            "pci_bus_id": cuda["pci_bus_id"] if cuda else "",
        })
    return records


def _native_adapter_infos(lib):
    if not hasattr(lib, "dlssnr_enumerate_adapters"):
        return []
    count = int(lib.dlssnr_enumerate_adapters(None, 0))
    if count < 0 or count > 64:
        raise RuntimeError("DLSS 宿主返回了无效的显卡数量")
    if not count:
        return []
    items = (_NativeAdapterInfo * count)()
    for item in items:
        item.struct_size = ctypes.sizeof(_NativeAdapterInfo)
    returned = int(lib.dlssnr_enumerate_adapters(items, count))
    if returned < 0:
        raise RuntimeError("DLSS 宿主无法枚举图形适配器")
    return list(items[:min(returned, count)])


def available_render_adapters():
    """List selectable NVIDIA D3D12 adapters without initializing NGX."""
    lib, _ = _load_backend("v2")
    if not hasattr(lib, "dlssnr_enumerate_adapters"):
        raise RuntimeError("当前 v2 宿主不支持显卡选择，请更新 dlssnr_host_v2.dll")
    return _adapter_records(_native_adapter_infos(lib), _cuda_adapter_luids())


def _configure_render_adapter(lib, settings, backend):
    requested = str((settings or {}).get("render_gpu", RENDER_GPU_AUTO))
    if backend != "v2":
        if requested != RENDER_GPU_AUTO:
            raise RuntimeError("手动选择 DLSS 渲染 GPU 需要 v2 宿主")
        return [], {}
    if not hasattr(lib, "dlssnr_select_adapter"):
        if requested != RENDER_GPU_AUTO:
            raise RuntimeError("当前 v2 宿主不支持手动选择显卡，请更新宿主 DLL")
        return [], {}
    records = _adapter_records(_native_adapter_infos(lib), _cuda_adapter_luids())
    if not records:
        raise RuntimeError("未检测到可用于 DLSS 的 NVIDIA D3D12 GPU")
    if requested == RENDER_GPU_AUTO:
        lib.dlssnr_select_adapter_auto()
        return records, records[0]
    selected = next((record for record in records if record["id"] == requested), None)
    if selected is None:
        names = "、".join(record["name"] for record in records) or "无"
        raise RuntimeError("此前选择的 DLSS 渲染 GPU 当前不可用；已检测到：" + names)
    lib.dlssnr_select_adapter(selected["luid_high"], selected["luid_low"])
    return records, selected


def _selected_render_adapter(lib, records, expected):
    if not hasattr(lib, "dlssnr_selected_adapter"):
        return {}
    native = _NativeAdapterInfo()
    native.struct_size = ctypes.sizeof(_NativeAdapterInfo)
    if not lib.dlssnr_selected_adapter(ctypes.byref(native)):
        return {}
    matched = next((record for record in records if (
        record["luid_high"] == int(native.luid_high)
        and record["luid_low"] == int(native.luid_low)
    )), None)
    if matched is not None:
        return dict(matched)
    result = dict(expected or {})
    result.update({
        "name": str(native.description).strip() or result.get("name", "NVIDIA GPU"),
        "vendor_id": int(native.vendor_id),
        "device_id": int(native.device_id),
        "dedicated_video_memory": int(native.dedicated_video_memory),
        "luid_high": int(native.luid_high),
        "luid_low": int(native.luid_low),
    })
    return result


def is_render_adapter_error(error):
    text = str(error or "")
    return any(marker in text for marker in (
        "未检测到可用于 DLSS 的 NVIDIA D3D12 GPU",
        "此前选择的 DLSS 渲染 GPU 当前不可用",
        "no compatible NVIDIA D3D12 adapter found",
        "requested NVIDIA adapter is unavailable",
    ))


def _host_config(settings):
    s = settings or {}
    return (
        bool(s.get("host_zero_fast_path", True)) and not int(s.get("guidance_mode", 0)),
        bool(s.get("host_persistent_buffers", True)),
        str(s.get("host_submission", "merged")) == "merged",
        clamp_in_flight(s.get("host_in_flight", 2)),
        bool(s.get("host_auto_fallback", True)),
        bool(s.get("host_tiled_mode", False)),
        max(64, min(8192, int(s.get("host_tile_width", DEFAULT_TILE_WIDTH)))),
        max(64, min(8192, int(s.get("host_tile_height", DEFAULT_TILE_HEIGHT)))),
    )


def _configure_host(lib, settings):
    if hasattr(lib, "dlssnr_configure"):
        zero_fast, persistent, merged, in_flight, fallback, *_ = _host_config(settings)
        if not hasattr(lib, "dlssnr_queue_capacity"):
            in_flight = min(in_flight, LEGACY_MAX_IN_FLIGHT)
        lib.dlssnr_configure(
            int(zero_fast), int(persistent), int(merged),
            in_flight, int(fallback),
        )
    tiled = bool((settings or {}).get("host_tiled_mode", False))
    if tiled and not hasattr(lib, "dlssnr_configure_tiling"):
        raise RuntimeError("当前 DLSS 主机不支持大图分区处理，请重新编译 v2 主机")
    if hasattr(lib, "dlssnr_configure_tiling"):
        lib.dlssnr_configure_tiling(
            int(tiled),
            max(64, min(8192, int((settings or {}).get("host_tile_width", DEFAULT_TILE_WIDTH)))),
            max(64, min(8192, int((settings or {}).get("host_tile_height", DEFAULT_TILE_HEIGHT)))),
        )
    format_id, profile_id = frame_contract(settings)
    if format_id == 1:
        if not hasattr(lib, "dlssnr_configure_format"):
            raise RuntimeError("当前主机不支持 RGBA16F/HDR，请重新编译 v2 主机")
        lib.dlssnr_configure_format(format_id, profile_id)
    elif hasattr(lib, "dlssnr_configure_format"):
        lib.dlssnr_configure_format(format_id, profile_id)


# Common appearance keys; optional guidance is configured separately below:
#   style (int), intensity/local_tone/local_struct (0..5), use_auto_mask,
#   skin_struct (effective with auto mask), output_view (0/1/2), output_mix (0..5).
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
        int(s.get('guidance_mode', 0)),
        int(s.get('depth_convention', 2)),
        float(s.get('motion_scale_x', 1.0)),
        float(s.get('motion_scale_y', 1.0)))


def _read_log_tail(path, limit=800):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()[-max(int(limit), 0):]
    except OSError:
        return ""


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
        self.settings = normalize_public_settings(settings)
        guidance_client.validate(self.settings)
        self._guidance = None
        self._reset_next = True
        self.adapter_info = {}
        self.runtime_path = mod_paths.runtime_path(self.settings)
        self._preference = str(self.settings.get("host_backend", "auto"))
        self._lib, self.backend = _load(self.settings)
        self._open_with_fallback()
        self._allocate_buffers()

    def _allocate_buffers(self):
        """Allocate the large zero-guidance/output buffers once per resolution."""
        self._mv = np.zeros((self._h, self._w, 2), np.float32)
        self._dp = None if getattr(self, 'supports_optional_depth', False) else np.zeros((self._h, self._w), np.float32)
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
        adapters, expected_adapter = _configure_render_adapter(
            self._lib, s, self.backend,
        )
        if not self._lib.dlssnr_init(self._w, self._h, int(s.get('preset', 1)), self.runtime_path, LOG_PATH):
            log = _read_log_tail(LOG_PATH)
            detail = "dlssnr_init failed (D3D12/gate). See dlss_run.log"
            if log:
                detail += "\nNGX 日志末尾：\n" + log
            raise RuntimeError(detail)
        if not self._lib.dlssnr_create_feature(self._w, self._h, int(s.get('preset', 1))):
            log = _read_log_tail(LOG_PATH)
            raise RuntimeError("Feature 18 create failed.\n" + log[-800:])
        self.adapter_info = _selected_render_adapter(
            self._lib, adapters, expected_adapter,
        )
        self._config = _host_config(s)
        self._refresh_capabilities()

    def _open_with_fallback(self):
        try:
            self._open()
        except Exception as error:
            allow = bool(self.settings.get("host_auto_fallback", True))
            if (
                self._preference != "auto" or self.backend != "v2" or not allow
                or self.settings.get("render_gpu", RENDER_GPU_AUTO) != RENDER_GPU_AUTO
                or is_render_adapter_error(error)
            ):
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
        requested = clamp_in_flight(self.settings.get("host_in_flight", 2))
        self.tiled = bool(capabilities & 4)
        self.supports_optional_depth = bool(capabilities & 8)
        capacity = (max(1, int(self._lib.dlssnr_queue_capacity()))
                    if hasattr(self._lib, "dlssnr_queue_capacity") else LEGACY_MAX_IN_FLIGHT)
        self.max_in_flight = min(requested, capacity) if capabilities & 2 and not self.tiled else 1
        self.supports_async = self.max_in_flight > 1

    def update(self, settings):
        updated = normalize_public_settings({**self.settings, **settings})
        guidance_client.validate(updated)
        runtime_selection_changed = any(updated.get(key, '') != self.settings.get(key, '')
                                        for key in ('dlss_runtime', 'mods_directory'))
        if runtime_selection_changed and mod_paths.runtime_path(updated) != self.runtime_path:
            raise RuntimeError(i18n.tr_for(updated.get('ui_language'), 'guidance.error.runtime_switch'))
        if updated.get("render_gpu", RENDER_GPU_AUTO) != self.settings.get(
            "render_gpu", RENDER_GPU_AUTO,
        ):
            raise RuntimeError("进程内 Live 不支持切换 DLSS 渲染 GPU；请新建会话或使用 ProcessLive。")
        changed_guidance = guidance_client.contract(updated) != guidance_client.contract(self.settings)
        if changed_guidance and self.pending:
            raise RuntimeError(i18n.tr_for(updated.get('ui_language'), 'guidance.error.pending'))
        if changed_guidance:
            self.close_guidance()
            self._allocate_buffers()
            self._reset_next = True
        old_preset = self.settings.get('preset')
        old_config = getattr(self, "_config", _host_config(self.settings))
        old_contract = frame_contract(self.settings)
        self.settings.update(updated)
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
            log = _read_log_tail(LOG_PATH)
            raise RuntimeError("Feature 18 resize failed.\n" + log[-800:])
        self._w, self._h = w, h
        self._config = _host_config(self.settings)
        self._refresh_capabilities()
        self._allocate_buffers()
        self.close_guidance()
        self._reset_next = True

    def close_guidance(self):
        if self._guidance is not None:
            # Discard borrowed shared-memory views before the session unmaps.
            self._mv = np.zeros((self._h, self._w, 2), np.float32)
            self._dp = None if getattr(self, 'supports_optional_depth', False) else np.zeros((self._h, self._w), np.float32)
            self._guidance.close()
            self._guidance = None

    @property
    def guidance_info(self):
        return dict(self._guidance.info) if self._guidance is not None else {}

    @property
    def guidance_metrics(self):
        return dict(self._guidance.last_metrics) if self._guidance is not None else {}

    def _prepare_guidance(self, rgba, reset):
        reset = bool(reset or self._reset_next)
        if int(self.settings.get('guidance_mode', 0)):
            if self._guidance is None:
                self._guidance = guidance_client.GuidanceSession(self.settings, self._w, self._h)
            try:
                proxy = analysis_rgba8(rgba, self.settings)
                self._mv, self._dp, reset = self._guidance.process(
                    proxy, reset, copy_outputs=False,
                    allow_missing_depth=getattr(self, 'supports_optional_depth', False))
            except Exception:
                self.close_guidance()
                self._reset_next = True
                raise
        return reset

    def _depth_pointer(self):
        if self._dp is None:
            if getattr(self, 'supports_optional_depth', False):
                return None
            # Older native libraries keep their non-null input contract.
            self._dp = np.zeros((self._h, self._w), np.float32)
        return self._dp.ctypes.data_as(ctypes.c_void_p)

    def guidance_preview(self, rgba, reset=False, final=True):
        """Evaluate guidance without running DLSS; returned views expire next call."""
        if rgba.dtype != frame_dtype(self.settings) or rgba.shape != (self._h, self._w, 4):
            raise ValueError("Guidance preview requires same-size input matching the frame contract")
        if not rgba.flags.c_contiguous:
            rgba = np.ascontiguousarray(rgba)
        reset = self._prepare_guidance(rgba, reset)
        # This inspection advances guidance history but not DLSS; make the next
        # production call explicitly reset its own temporal state.
        self._reset_next = bool(final)
        return self._mv, self._dp, reset

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
        reset = self._prepare_guidance(rgba, reset)
        ok = self._lib.dlssnr_process(
            rgba.ctypes.data_as(ctypes.c_void_p),
            self._mv.ctypes.data_as(ctypes.c_void_p),
            self._depth_pointer(),
            self._output.ctypes.data_as(ctypes.c_void_p),
            1 if reset else 0)
        self._reset_next = not bool(ok)
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
        # Do not advance the model's previous-frame state for a rejected queue.
        if self.pending >= self.max_in_flight:
            return False
        reset = self._prepare_guidance(rgba, reset)
        accepted = bool(self._lib.dlssnr_enqueue(
            rgba.ctypes.data_as(ctypes.c_void_p),
            self._mv.ctypes.data_as(ctypes.c_void_p),
            self._depth_pointer(),
            1 if reset else 0,
        ))
        self._reset_next = not accepted
        return accepted

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
        self.close_guidance()
        try:
            self._lib.dlssnr_shutdown()
        except Exception:
            pass


def run_dlss(rgba_frames, settings=None, reset=True, progress=None):
    """Batch-generate DLSS with optional user-provided guidance. Returns RGBA list."""
    settings = settings or {}
    h, w = rgba_frames[0].shape[:2]
    live = Live(w, h, settings)
    out = []
    try:
        for i, rgba in enumerate(rgba_frames):
            processed = live.process(rgba, reset=(reset and i == 0))
            if processed is None:
                raise RuntimeError("DLSS processing failed at frame %d" % i)
            if progress:
                progress(i, len(rgba_frames), "ok")
            out.append(processed.copy())
    finally:
        live.close_guidance()
    view = settings.get('output_view', 0)
    mix = float(settings.get('output_mix', 1.0))
    if view != 0 or mix < 1.0:
        out = _apply_output_view(out, rgba_frames, view, mix, w, h)
    return out
