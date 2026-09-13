#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validated, atomic persistence for DLSS5Tool UI/export settings."""

import json
import os
import re
import sys

from dlss5tool import i18n
from dlss5tool.host_queue import MAX_IN_FLIGHT
from dlss5tool.performance_profiles import profiles as validate_performance_profiles
from dlss5tool.guidance_parameters import parameters as guidance_parameters
from dlss5tool.guidance_public import normalize_public_settings


DLSS_SLIDER_MIN = 0.0
DLSS_STANDARD_MAX = 1.0
DLSS_SLIDER_MAX = 5.0
DLSS_SLIDER_STEP = 0.01


DEFAULTS = {
    **guidance_parameters({'guidance_edge': 512, 'guidance_flow_range': 5.0}),
    "preview_view": "compare",
    "preview_compare_layout": "wipe",
    "guidance_preview_view": "flow",
    "guidance_compare_target": "flow",
    "style": 0,
    "enable_5x": False,
    "intensity": 1.0,
    "use_intensity": True,
    "local_tone": 1.0,
    "use_local_tone": True,
    "local_struct": 1.0,
    "use_local_struct": True,
    "use_auto_mask": True,
    "skin_struct": 1.0,
    "output_view": 0,
    "output_mix": 1.0,
    "use_output_mix": True,
    "export_mode": "single",
    "parallel_workers": 4,
    "warmup_frames": 8,
    "decode_buffer": 4,
    "nvenc_preset": "p5",
    "output_container": "mp4",
    "output_resolution": "source",
    "super_resolution_scale": 1,
    "custom_output_width": 1920,
    "custom_output_height": 1080,
    "rate_control": "quality",
    "quality_profile": "high",
    "video_bitrate_mbps": 20.0,
    "hdr_mode": True,
    "host_backend": "auto",
    "render_gpu": "auto",
    "dlss_runtime": "",
    "mods_directory": "",
    "guidance_flow_weights": "",
    "guidance_depth_weights": "",
    "ui_modules_open": False,
    "guidance_mode": 1,
    "guidance_skip_still_flow": True,
    "guidance_flow_backend": "raft",
    "guidance_flow_grid": 1,
    "guidance_edge": 720,
    "guidance_flow_direction": "backward",
    "guidance_depth_encoder": "vitl",
    "guidance_device": "auto",
    "guidance_depth_profile": "sdpa_fp16",
    "guidance_execution": "raft_streams",
    "host_zero_fast_path": False,
    "host_persistent_buffers": True,
    "host_submission": "compatibility",
    "host_in_flight": 3,
    "host_auto_fallback": True,
    "host_mode_profiles": {},
    "ui_export_open": False,
    "ui_host_open": False,
    "ui_preview_open": False,
    "ui_theme": "light",
    "ui_language": i18n.DEFAULT_LANGUAGE,
    "inspector_width": 360,
    "preview_detached": False,
    "preview_window_geometry": "",
    "queue_output_dir": "",
    "preview_quality": "original",
    "preview_prefetch": 120,
    "preview_cache": 400,
    "preview_cache_mb": 8192,
    "preview_scrub_ms": 40,
}


def settings_path():
    overridden = os.environ.get("DLSS5TOOL_SETTINGS_PATH")
    if overridden:
        return os.path.abspath(overridden)
    from dlss5tool.paths import state_path
    return str(state_path("dlss5_settings.json"))


def _clamp_float(value, low=DLSS_SLIDER_MIN, high=DLSS_SLIDER_MAX):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def _clamp_int(value, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return low


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    return default


def _as_window_geometry(value):
    """Keep only a normal Tk ``WIDTHxHEIGHT+X+Y`` geometry string."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if re.fullmatch(r"\d{2,5}x\d{2,5}[+-]\d{1,6}[+-]\d{1,6}", value):
        return value
    return ""


def validate(values):
    """Return a complete safe configuration, ignoring unknown/corrupt values."""
    source = values if isinstance(values, dict) else {}
    result = dict(DEFAULTS)
    preview_view = {
        "原图": "original",
        "DLSS": "dlss",
        "对比": "compare",
    }.get(source.get("preview_view"), source.get("preview_view"))
    if preview_view in {"original", "dlss", "compare"}:
        result["preview_view"] = preview_view
    result["style"] = _clamp_int(source.get("style", result["style"]), 0, 2)
    result["enable_5x"] = _as_bool(
        source.get("enable_5x", result["enable_5x"]), result["enable_5x"]
    )
    slider_max = DLSS_SLIDER_MAX if result["enable_5x"] else DLSS_STANDARD_MAX
    for name in (
        "intensity", "local_tone", "local_struct", "skin_struct", "output_mix",
    ):
        result[name] = _clamp_float(source.get(name, result[name]), high=slider_max)
    result["output_view"] = _clamp_int(source.get("output_view", result["output_view"]), 0, 2)
    if source.get("export_mode") in {"single", "parallel"}:
        result["export_mode"] = source["export_mode"]
    result["parallel_workers"] = _clamp_int(
        source.get("parallel_workers", result["parallel_workers"]), 2, 4
    )
    result["warmup_frames"] = _clamp_int(
        source.get("warmup_frames", result["warmup_frames"]), 0, 120
    )
    result["decode_buffer"] = _clamp_int(
        source.get("decode_buffer", result["decode_buffer"]), 1, 8
    )
    preset = str(source.get("nvenc_preset", result["nvenc_preset"]))
    if preset in {f"p{i}" for i in range(1, 8)}:
        result["nvenc_preset"] = preset
    if source.get("output_container") in {"mp4", "mkv", "mov", "source"}:
        result["output_container"] = source["output_container"]
    if source.get("output_resolution") in {
        "source", "2160p", "1440p", "1080p", "720p", "custom",
    }:
        result["output_resolution"] = source["output_resolution"]
    super_resolution_scale = _clamp_int(
        source.get("super_resolution_scale", result["super_resolution_scale"]), 1, 4
    )
    result["super_resolution_scale"] = (
        super_resolution_scale if super_resolution_scale in {1, 2, 4} else 1
    )
    result["custom_output_width"] = _clamp_int(
        source.get("custom_output_width", result["custom_output_width"]), 2, 16384
    )
    result["custom_output_height"] = _clamp_int(
        source.get("custom_output_height", result["custom_output_height"]), 2, 16384
    )
    if source.get("rate_control") in {"quality", "bitrate"}:
        result["rate_control"] = source["rate_control"]
    if source.get("quality_profile") in {"maximum", "high", "balanced", "compact"}:
        result["quality_profile"] = source["quality_profile"]
    try:
        bitrate = float(source.get("video_bitrate_mbps", result["video_bitrate_mbps"]))
    except (TypeError, ValueError):
        bitrate = result["video_bitrate_mbps"]
    result["video_bitrate_mbps"] = max(0.5, min(500.0, bitrate))
    if source.get("host_backend") in {"auto", "v2", "legacy"}:
        result["host_backend"] = source["host_backend"]
    render_gpu = str(source.get("render_gpu", result["render_gpu"])).strip()
    if render_gpu == "auto" or re.fullmatch(
        r"dxgi:[0-9A-F]{4}:[0-9A-F]{4}:[0-9A-F]{8}:[0-9A-F]{8}:[0-9A-F]{16}"
        r"(?::(?:P[0-9A-F]{9}|C\d+))?",
        render_gpu,
        flags=re.I,
    ):
        result["render_gpu"] = render_gpu
    for name in ("dlss_runtime", "mods_directory",
                 "guidance_flow_weights", "guidance_depth_weights"):
        if isinstance(source.get(name), str):
            result[name] = source[name].strip()
    result["guidance_mode"] = _clamp_int(source.get("guidance_mode", result["guidance_mode"]), 0, 3)
    result["guidance_edge"] = _clamp_int(source.get("guidance_edge", result['guidance_edge']), 128, 1280)
    analysis_source = {**result, **source}
    # Preserve old shared-edge settings, without changing the worker protocol's
    # legacy defaults or overwriting independently saved model parameters.
    for key in ('guidance_flow_edge', 'guidance_depth_edge'):
        if key not in source and 'guidance_edge' in source:
            analysis_source[key] = result['guidance_edge']
    result.update(guidance_parameters(analysis_source))
    for name, choices in (("guidance_flow_direction", {"backward", "forward_negated"}),
                          ("guidance_flow_backend", {"raft", "nvofa"}),
                          ("guidance_depth_encoder", {"auto", "vits", "vitb", "vitl"}),
                          ("guidance_device", {"auto", "cpu", "cuda"}),
                          ("guidance_depth_profile", {"fp32", "sdpa_fp16"}),
                          ("guidance_execution", {"serial", "raft_final", "raft_streams"})):
        if source.get(name) in choices:
            result[name] = source[name]
    # The final-only RAFT adapter was withdrawn after real footage exposed
    # severe temporal jitter. Preserve old settings files but migrate that
    # retired profile to torchvision's original serial implementation.
    if result["guidance_execution"] == "raft_final":
        result["guidance_execution"] = "serial"
    try:
        grid = int(source.get("guidance_flow_grid", result["guidance_flow_grid"]))
    except (TypeError, ValueError):
        grid = result["guidance_flow_grid"]
    if grid in (1, 2, 4):
        result["guidance_flow_grid"] = grid
    if source.get("host_submission") in {"merged", "compatibility"}:
        result["host_submission"] = source["host_submission"]
    for name in (
        "use_intensity", "use_local_tone", "use_local_struct",
        "use_output_mix", "use_auto_mask",
        "hdr_mode",
        "guidance_skip_still_flow",
        "host_zero_fast_path", "host_persistent_buffers", "host_auto_fallback",
        "ui_export_open", "ui_host_open", "ui_preview_open", "ui_modules_open", "preview_detached",
    ):
        result[name] = _as_bool(source.get(name, result[name]), result[name])
    theme = str(source.get("ui_theme", result["ui_theme"])).strip().lower()
    result["ui_theme"] = "light" if theme == "light" else "dark"
    result["ui_language"] = i18n.normalize_language(
        source.get("ui_language", result["ui_language"])
    )
    result["inspector_width"] = _clamp_int(
        source.get("inspector_width", result["inspector_width"]), 320, 480,
    )
    result["preview_window_geometry"] = _as_window_geometry(
        source.get("preview_window_geometry", result["preview_window_geometry"])
    )
    queue_output_dir = source.get("queue_output_dir", result["queue_output_dir"])
    if isinstance(queue_output_dir, str):
        result["queue_output_dir"] = queue_output_dir.strip()
    result["host_in_flight"] = _clamp_int(
        source.get("host_in_flight", result["host_in_flight"]), 1, MAX_IN_FLIGHT
    )
    if source.get("preview_quality") in {"auto", "1080p", "1440p", "original"}:
        result["preview_quality"] = source["preview_quality"]
    if source.get('preview_compare_layout') in {'wipe', 'side'}:
        result['preview_compare_layout'] = source['preview_compare_layout']
    if source.get('guidance_preview_view') in {'original', 'depth', 'flow', 'compare'}:
        result['guidance_preview_view'] = source['guidance_preview_view']
    if source.get('guidance_compare_target') in {'depth', 'flow'}:
        result['guidance_compare_target'] = source['guidance_compare_target']
    result["preview_prefetch"] = _clamp_int(
        source.get("preview_prefetch", result["preview_prefetch"]), 4, 120
    )
    result["preview_cache"] = _clamp_int(
        source.get("preview_cache", result["preview_cache"]), 16, 400
    )
    result["preview_cache_mb"] = _clamp_int(
        source.get("preview_cache_mb", result["preview_cache_mb"]), 256, 32768
    )
    result["preview_scrub_ms"] = _clamp_int(
        source.get("preview_scrub_ms", result["preview_scrub_ms"]), 0, 400
    )
    result = normalize_public_settings(result)
    # With no explicit override, base rendering should skip zero-input uploads.
    # Keep a manual opt-out (and saved queue snapshots) intact.
    if "host_zero_fast_path" not in source:
        result["host_zero_fast_path"] = not result["guidance_mode"]
    result['host_mode_profiles'] = validate_performance_profiles(source.get('host_mode_profiles'), result)
    return result


def startup_settings(values):
    """Restore saved tuning and mode; the GUI checks readiness before activation."""
    return validate(values)


def load(path=None):
    path = os.path.abspath(path or settings_path())
    is_first_run = not os.path.isfile(path)
    try:
        with open(path, encoding="utf-8") as handle:
            return validate(json.load(handle))
    except (OSError, ValueError, TypeError):
        defaults = dict(DEFAULTS)
        if is_first_run:
            defaults["ui_language"] = i18n.system_language()
        return defaults


def save(values, path=None):
    path = os.path.abspath(path or settings_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe = validate(values)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(safe, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)
    return safe
