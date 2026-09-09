#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Explicit, dependency-free localization for DLSS5Tool."""

from __future__ import annotations

import json
import locale
import os
import sys
from functools import lru_cache


DEFAULT_LANGUAGE = "zh_CN"
SUPPORTED_LANGUAGES = ("zh_CN", "en_US")
LANGUAGE_NAMES = {"zh_CN": "简体中文", "en_US": "English"}

_language = DEFAULT_LANGUAGE


def normalize_language(value):
    text = str(value or "").strip().replace("-", "_").lower()
    if text.startswith("en"):
        return "en_US"
    if text.startswith(("zh", "cmn")):
        return "zh_CN"
    return DEFAULT_LANGUAGE


def system_language():
    override = os.environ.get("DLSS5TOOL_LANG")
    if override:
        return normalize_language(override)
    try:
        return normalize_language(locale.getlocale()[0])
    except (TypeError, ValueError):
        return DEFAULT_LANGUAGE


def set_language(value):
    global _language
    _language = normalize_language(value)
    return _language


def get_language():
    return _language


def _resource_root():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


@lru_cache(maxsize=None)
def _catalog(language):
    path = os.path.join(_resource_root(), "locales", f"{language}.json")
    try:
        with open(path, encoding="utf-8") as handle:
            values = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return values if isinstance(values, dict) else {}


def tr(key, **values):
    """Translate a stable key and safely interpolate named values."""
    return tr_for(_language, key, **values)


def tr_for(language_code, key, /, **values):
    """Explicit language for background workers; never mutates global UI state."""
    fallback = _catalog(DEFAULT_LANGUAGE).get(key, key)
    template = _catalog(normalize_language(language_code) if language_code else _language).get(key, fallback)
    if not isinstance(template, str):
        template = str(template)
    if not values:
        return template
    try:
        return template.format(**values)
    except (KeyError, ValueError, IndexError):
        return fallback


def catalog_keys(language):
    return set(_catalog(normalize_language(language)))


def clear_cache():
    _catalog.cache_clear()
