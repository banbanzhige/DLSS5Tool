#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rasterize a small Lucide (ISC) icon subset with Pillow. No Cairo required.

Lucide icons: https://lucide.dev  Copyright (c) 2026 Lucide Icons and Contributors.
Feather-derived glyphs retain the MIT notice in THIRD_PARTY_NOTICES.md.
"""

import math
import re
from collections import OrderedDict
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw, ImageTk


# 24×24 Lucide inner markup. stroke="currentColor" stroke-width="2"
# stroke-linecap="round" stroke-linejoin="round" fill="none".
LUCIDE = {
    "prev": '<path d="m15 18-6-6 6-6"/>',
    "next": '<path d="m9 18 6-6-6-6"/>',
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "pause": (
        '<rect x="14" y="4" width="4" height="16" rx="1"/>'
        '<rect x="6" y="4" width="4" height="16" rx="1"/>'
    ),
    "stop": '<rect width="18" height="18" x="3" y="3" rx="2"/>',
    "minus": '<path d="M5 12h14"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "fit": (
        '<path d="M4 14h6v6"/><path d="M20 10h-6V4"/>'
        '<path d="m14 10 7-7"/><path d="m3 21 7-7"/>'
    ),
    "fullscreen": (
        '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/>'
        '<path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>'
    ),
    "fullscreen-exit": (
        '<path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/>'
        '<path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M21 16h-3a2 2 0 0 0-2 2v3"/>'
    ),
    "detach": (
        '<rect width="20" height="14" x="2" y="4" rx="2"/>'
        '<rect width="8" height="6" x="13" y="13" rx="1"/>'
    ),
    "dock": (
        '<rect width="18" height="18" x="3" y="3" rx="2"/>'
        '<path d="M3 9h18"/>'
    ),
    "volume": (
        '<path d="M11 5 6 9H2v6h4l5 4z"/>'
        '<path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>'
        '<path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>'
    ),
    "volume-off": (
        '<path d="M11 5 6 9H2v6h4l5 4z"/>'
        '<line x1="22" x2="16" y1="9" y2="15"/>'
        '<line x1="16" x2="22" y1="9" y2="15"/>'
    ),
    "file-plus": (
        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/>'
        '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>'
        '<path d="M9 15h6"/><path d="M12 12v6"/>'
    ),
    "folder-plus": (
        '<path d="M12 10v6"/><path d="M9 13h6"/>'
        '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9'
        'L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/>'
    ),
    "folder-open": (
        '<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2A2 2 0 0 0 12.21 6H18a2 2 0 0 1 2 2v2"/>'
    ),
    "trash": (
        '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>'
        '<path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>'
        '<line x1="10" x2="10" y1="11" y2="17"/>'
        '<line x1="14" x2="14" y1="11" y2="17"/>'
    ),
    "more": (
        '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>'
        '<circle cx="5" cy="12" r="1"/>'
    ),
    "clear": (
        '<path d="M11 12H3"/><path d="M16 6H3"/><path d="M16 18H3"/>'
        '<path d="m19 10-4 4"/><path d="m15 10 4 4"/>'
    ),
    "cancel": (
        '<circle cx="12" cy="12" r="10"/>'
        '<path d="m15 9-6 6"/><path d="m9 9 6 6"/>'
    ),
    "retry": (
        '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>'
        '<path d="M3 3v5h5"/>'
    ),
    "clear-done": (
        '<path d="M13 5h8"/><path d="M13 12h8"/><path d="M13 19h8"/>'
        '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/>'
    ),
    "up": '<path d="m18 15-6-6-6 6"/>',
    "down": '<path d="m6 9 6 6 6-6"/>',
}

_FILLED = {"play", "pause", "stop"}
_TOKEN = re.compile(
    r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?"
)
_CACHE = OrderedDict()
_CACHE_LIMIT = 128


def _rgba(color):
    text = str(color or "#000000").lstrip("#")
    if len(text) != 6:
        return (0, 0, 0, 255)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), 255)


def _nums(d):
    tokens = _TOKEN.findall(d.replace(",", " "))
    out, cmd, args = [], None, []
    arity = {
        "M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0,
    }
    for token in tokens:
        if token.isalpha():
            if cmd:
                out.append((cmd, args))
            cmd, args = token, []
        else:
            args.append(float(token))
            if cmd and len(args) >= arity.get(cmd.upper(), 99):
                out.append((cmd, args))
                cmd = {"M": "L", "m": "l"}.get(cmd, cmd)
                args = []
    if cmd:
        out.append((cmd, args))
    return out


def _arc_points(x1, y1, rx, ry, phi_deg, large, sweep, x2, y2):
    rx, ry = abs(rx) or 1e-6, abs(ry) or 1e-6
    phi = math.radians(phi_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_p * dx + sin_p * dy
    y1p = -sin_p * dx + cos_p * dy
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        scale = math.sqrt(lam)
        rx, ry = rx * scale, ry * scale
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / max(den, 1e-12)))
    if int(large) == int(sweep):
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = coef * -ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2.0

    def ang(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        nrm = math.hypot(ux, uy) * math.hypot(vx, vy)
        value = math.acos(max(-1.0, min(1.0, dot / nrm))) if nrm else 0.0
        return -value if ux * vy - uy * vx < 0 else value

    theta1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = ang(
        (x1p - cxp) / rx, (y1p - cyp) / ry,
        (-x1p - cxp) / rx, (-y1p - cyp) / ry,
    )
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    steps = max(4, int(abs(delta) / (math.pi / 8)))
    pts = []
    for i in range(steps + 1):
        t = theta1 + delta * i / steps
        x = cos_p * rx * math.cos(t) - sin_p * ry * math.sin(t) + cx
        y = sin_p * rx * math.cos(t) + cos_p * ry * math.sin(t) + cy
        pts.append((x, y))
    return pts


def _path_points(d):
    x = y = mx = my = 0.0
    cx = cy = qx = qy = None
    parts, current = [], []

    def flush():
        if current:
            parts.append(list(current))
            current.clear()

    def add(px, py):
        current.append((px, py))

    for cmd, raw in _nums(d):
        rel = cmd.islower()
        op, a = cmd.upper(), list(raw)
        if op == "M":
            flush()
            while len(a) >= 2:
                nx, ny = a.pop(0), a.pop(0)
                if rel:
                    nx, ny = x + nx, y + ny
                x, y = nx, ny
                mx, my = x, y
                add(x, y)
                op = "L"
        elif op == "L":
            while len(a) >= 2:
                nx, ny = a.pop(0), a.pop(0)
                if rel:
                    nx, ny = x + nx, y + ny
                x, y = nx, ny
                add(x, y)
            cx = cy = qx = qy = None
        elif op == "H":
            while a:
                nx = x + a.pop(0) if rel else a.pop(0)
                x = nx
                add(x, y)
            cx = cy = qx = qy = None
        elif op == "V":
            while a:
                ny = y + a.pop(0) if rel else a.pop(0)
                y = ny
                add(x, y)
            cx = cy = qx = qy = None
        elif op == "C":
            while len(a) >= 6:
                x1, y1, x2, y2, nx, ny = (a.pop(0) for _ in range(6))
                if rel:
                    x1, y1, x2, y2, nx, ny = (
                        x + x1, y + y1, x + x2, y + y2, x + nx, y + ny,
                    )
                for t in (0.25, 0.5, 0.75, 1.0):
                    u = 1 - t
                    px = u**3 * x + 3 * u**2 * t * x1 + 3 * u * t**2 * x2 + t**3 * nx
                    py = u**3 * y + 3 * u**2 * t * y1 + 3 * u * t**2 * y2 + t**3 * ny
                    add(px, py)
                cx, cy, x, y = x2, y2, nx, ny
                qx = qy = None
        elif op == "S":
            while len(a) >= 4:
                x2, y2, nx, ny = (a.pop(0) for _ in range(4))
                if rel:
                    x2, y2, nx, ny = x + x2, y + y2, x + nx, y + ny
                x1 = 2 * x - cx if cx is not None else x
                y1 = 2 * y - cy if cy is not None else y
                for t in (0.25, 0.5, 0.75, 1.0):
                    u = 1 - t
                    px = u**3 * x + 3 * u**2 * t * x1 + 3 * u * t**2 * x2 + t**3 * nx
                    py = u**3 * y + 3 * u**2 * t * y1 + 3 * u * t**2 * y2 + t**3 * ny
                    add(px, py)
                cx, cy, x, y = x2, y2, nx, ny
        elif op in {"Q", "T"}:
            while len(a) >= (2 if op == "T" else 4):
                if op == "T":
                    nx, ny = a.pop(0), a.pop(0)
                    if rel:
                        nx, ny = x + nx, y + ny
                    x1 = 2 * x - qx if qx is not None else x
                    y1 = 2 * y - qy if qy is not None else y
                else:
                    x1, y1, nx, ny = (a.pop(0) for _ in range(4))
                    if rel:
                        x1, y1, nx, ny = x + x1, y + y1, x + nx, y + ny
                for t in (0.25, 0.5, 0.75, 1.0):
                    u = 1 - t
                    px = u * u * x + 2 * u * t * x1 + t * t * nx
                    py = u * u * y + 2 * u * t * y1 + t * t * ny
                    add(px, py)
                qx, qy, x, y = x1, y1, nx, ny
                cx = cy = None
        elif op == "A":
            while len(a) >= 7:
                rx, ry, phi, large, sweep, nx, ny = (a.pop(0) for _ in range(7))
                if rel:
                    nx, ny = x + nx, y + ny
                for px, py in _arc_points(x, y, rx, ry, phi, large, sweep, nx, ny)[1:]:
                    add(px, py)
                x, y = nx, ny
                cx = cy = qx = qy = None
        elif op == "Z":
            add(mx, my)
            x, y = mx, my
            flush()
    flush()
    return parts


def _scale_pts(points, k, pad):
    return [(pad + x * k, pad + y * k) for x, y in points]


def _stroke(draw, pts, color, width):
    if len(pts) < 2:
        return
    draw.line(pts, fill=color, width=width, joint="curve")
    r = width / 2.0
    for x, y in (pts[0], pts[-1]):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)


def _draw_svg(draw, markup, k, pad, color, stroke, filled=False):
    wrapped = f'<g>{markup}</g>'
    root = ET.fromstring(wrapped)
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag == "path":
            for part in _path_points(node.get("d") or ""):
                pts = _scale_pts(part, k, pad)
                if filled and len(pts) >= 3:
                    draw.polygon(pts, fill=color)
                else:
                    _stroke(draw, pts, color, stroke)
        elif tag == "polygon":
            raw = [float(v) for v in (node.get("points") or "").replace(",", " ").split()]
            pts = _scale_pts(list(zip(raw[0::2], raw[1::2])), k, pad)
            if filled:
                draw.polygon(pts, fill=color)
            else:
                _stroke(draw, pts + pts[:1], color, stroke)
        elif tag == "polyline":
            raw = [float(v) for v in (node.get("points") or "").replace(",", " ").split()]
            _stroke(draw, _scale_pts(list(zip(raw[0::2], raw[1::2])), k, pad), color, stroke)
        elif tag == "line":
            pts = _scale_pts([
                (float(node.get("x1", 0)), float(node.get("y1", 0))),
                (float(node.get("x2", 0)), float(node.get("y2", 0))),
            ], k, pad)
            _stroke(draw, pts, color, stroke)
        elif tag == "circle":
            cx, cy, r = float(node.get("cx", 0)), float(node.get("cy", 0)), float(node.get("r", 0))
            x0, y0, x1, y1 = (
                pad + (cx - r) * k, pad + (cy - r) * k,
                pad + (cx + r) * k, pad + (cy + r) * k,
            )
            if r <= 2:
                draw.ellipse((x0, y0, x1, y1), fill=color)
            else:
                draw.ellipse((x0, y0, x1, y1), outline=color, width=stroke)
        elif tag == "rect":
            x, y = float(node.get("x", 0)), float(node.get("y", 0))
            w, h = float(node.get("width", 0)), float(node.get("height", 0))
            rr = float(node.get("rx") or node.get("ry") or 0)
            box = (pad + x * k, pad + y * k, pad + (x + w) * k, pad + (y + h) * k)
            if filled:
                draw.rounded_rectangle(box, radius=rr * k, fill=color)
            else:
                draw.rounded_rectangle(box, radius=rr * k, outline=color, width=stroke)


def icon_photo(master, name, size, color):
    """Return a PhotoImage for a Lucide icon, cached per Tcl interpreter."""
    if not name or size < 8:
        return None
    interp = getattr(getattr(master, "tk", None), "interpaddr", lambda: 0)()
    key = (interp, name, int(size), str(color))
    photo = _CACHE.get(key)
    if photo is not None:
        _CACHE.move_to_end(key)
        return photo
    markup = LUCIDE.get(name)
    if not markup:
        return None
    scale = 4
    pad = 2 * scale
    canvas = int(size) * scale + pad * 2
    k = (int(size) * scale) / 24.0
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_svg(
        draw, markup, k, pad, _rgba(color),
        stroke=max(2, round(2 * k)),
        filled=name in _FILLED,
    )
    img = img.resize((int(size) + 4, int(size) + 4), Image.Resampling.LANCZOS)
    photo = ImageTk.PhotoImage(img, master=master)
    _CACHE[key] = photo
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)
    return photo
