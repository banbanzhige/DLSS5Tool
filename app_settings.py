#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validated, atomic persistence for DLSS5Tool UI/export settings."""

import json
import os
import sys


DLSS_SLIDER_MIN = 0.0
DLSS_STANDARD_MAX = 1.0
DLSS_SLIDER_MAX = 5.0
DLSS_SLIDER_STEP = 0.01


DEFAULTS = {
    "preview_view": "对比",
    "style": 0,
    "enable_5x": False,
    "intensity": 1.0,
    "use_intensity": False,
    "local_tone": 1.0,
    "use_local_tone": False,
    "local_struct": 1.0,
    "use_local_struct": False,
    "use_auto_mask": False,
    "skin_struct": 0.5,
    "output_view": 0,
    "output_mix": 1.0,
    "use_output_mix": False,
    "export_mode": "single",
    "parallel_workers": 2,
    "warmup_frames": 8,
    "decode_buffer": 4,
    "nvenc_preset": "p5",
    "output_resolution": "source",
    "custom_output_width": 1920,
    "custom_output_height": 1080,
    "rate_control": "quality",
    "quality_profile": "high",
    "video_bitrate_mbps": 20.0,
    "hdr_mode": True,
    "host_backend": "auto",
    "host_zero_fast_path": True,
    "host_persistent_buffers": True,
    "host_submission": "merged",
    "host_in_flight": 2,
    "host_auto_fallback": True,
    "ui_export_open": False,
    "ui_host_open": False,
    "ui_preview_open": False,
    "preview_quality": "auto",
    "preview_prefetch": 24,
    "preview_cache": 96,
    "preview_cache_mb": 2048,
    "preview_scrub_ms": 40,
}


def settings_path():
    overridden = os.environ.get("DLSS5TOOL_SETTINGS_PATH")
    if overridden:
        return os.path.abspath(overridden)
    base = (
        os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__))
    )
    return os.path.join(base, "dlss5_settings.json")


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


def validate(values):
    """Return a complete safe configuration, ignoring unknown/corrupt values."""
    source = values if isinstance(values, dict) else {}
    result = dict(DEFAULTS)
    if source.get("preview_view") in {"原图", "DLSS", "对比"}:
        result["preview_view"] = source["preview_view"]
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
    if source.get("output_resolution") in {
        "source", "2160p", "1440p", "1080p", "720p", "custom",
    }:
        result["output_resolution"] = source["output_resolution"]
    result["custom_output_width"] = _clamp_int(
        source.get("custom_output_width", result["custom_output_width"]), 2, 8192
    )
    result["custom_output_height"] = _clamp_int(
        source.get("custom_output_height", result["custom_output_height"]), 2, 8192
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
    if source.get("host_submission") in {"merged", "compatibility"}:
        result["host_submission"] = source["host_submission"]
    for name in (
        "use_intensity", "use_local_tone", "use_local_struct",
        "use_output_mix", "use_auto_mask",
        "hdr_mode",
        "host_zero_fast_path", "host_persistent_buffers", "host_auto_fallback",
        "ui_export_open", "ui_host_open", "ui_preview_open",
    ):
        result[name] = _as_bool(source.get(name, result[name]), result[name])
    result["host_in_flight"] = _clamp_int(
        source.get("host_in_flight", result["host_in_flight"]), 1, 3
    )
    if source.get("preview_quality") in {"auto", "1080p", "1440p", "original"}:
        result["preview_quality"] = source["preview_quality"]
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
    return result


def load(path=None):
    path = os.path.abspath(path or settings_path())
    try:
        with open(path, encoding="utf-8") as handle:
            return validate(json.load(handle))
    except (OSError, ValueError, TypeError):
        return dict(DEFAULTS)


def save(values, path=None):
    path = os.path.abspath(path or settings_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe = validate(values)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(safe, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)
    return safe
