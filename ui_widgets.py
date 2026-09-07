#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Custom-drawn studio widgets. Keep Tk/ttk chrome off the inspector."""

import tkinter as tk
from tkinter import ttk
from collections import OrderedDict
from PIL import Image, ImageDraw, ImageTk

from ui_theme import (
    HEIGHT_BUTTON, HEIGHT_PRIMARY, RADIUS_CONTROL, SLIDER_THUMB, SLIDER_TRACK,
    UI_FONT, UI_FONT_BOLD, UI_FONT_SMALL, UI_MONO, control_height, scale_px,
    install_combobox_behavior,
)
from ui_icons import icon_photo


def _clear_chrome_live(canvas):
    """Release PhotoImages whose canvas items were removed before repainting."""
    canvas._chrome_live = []


def round_rect(canvas, x1, y1, x2, y2, radius=6, fill="", outline="", width=1, tags=(),
               smooth=True):
    """Antialiased chrome only; text remains native, never raster-resized.

    Cache a small number of surfaces on each owning canvas/Tcl interpreter.
    This avoids supersampling on every hover or paint and bounds image memory.
    Pass smooth=False for rapidly changing fills so the cache cannot evict
    other chrome on the same canvas.
    """
    w, h = round(abs(x2 - x1)), round(abs(y2 - y1))
    if (
        not smooth or w < 2 or h < 2 or w * h > 400_000
        or not hasattr(canvas, "tk")
    ):
        return _native_round_rect(canvas, x1, y1, x2, y2, radius, fill, outline, width, tags)
    key = (w, h, radius, fill, outline, width)
    cache = getattr(canvas, "_chrome_surfaces", None)
    if cache is None:
        cache = canvas._chrome_surfaces = OrderedDict()
    photo = cache.get(key)
    if photo is None:
        scale, inset = 4, 2
        img = Image.new("RGBA", ((w + 4) * scale, (h + 4) * scale))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (inset * scale, inset * scale, (w + inset) * scale, (h + inset) * scale),
            radius=max(0, min(radius, w / 2, h / 2)) * scale,
            fill=fill or None, outline=outline or None,
            width=max(1, round(width * scale)),
        )
        img = img.resize((w + 4, h + 4), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(img, master=canvas)
        cache[key] = photo
        while len(cache) > 48:
            cache.popitem(last=False)
    else:
        cache.move_to_end(key)
    live = getattr(canvas, "_chrome_live", None)
    if live is None:
        live = canvas._chrome_live = []
    live.append(photo)
    return [canvas.create_image(min(x1, x2) - 2, min(y1, y2) - 2,
                                image=photo, anchor="nw", tags=tags)]


def _native_round_rect(canvas, x1, y1, x2, y2, radius=6, fill="", outline="", width=1, tags=()):
    """Fill with unstroked ovals/rects, stroke with arcs and lines — no seams."""
    x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    radius = max(0.0, min(float(radius), (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    items = []
    if fill:
        if radius <= 0.5:
            items.append(canvas.create_rectangle(
                x1, y1, x2, y2, fill=fill, outline="", tags=tags,
            ))
        else:
            items.append(canvas.create_rectangle(
                x1 + radius, y1, x2 - radius, y2, fill=fill, outline="", tags=tags,
            ))
            items.append(canvas.create_rectangle(
                x1, y1 + radius, x2, y2 - radius, fill=fill, outline="", tags=tags,
            ))
            items.append(canvas.create_oval(
                x1, y1, x1 + 2 * radius, y1 + 2 * radius, fill=fill, outline="", tags=tags,
            ))
            items.append(canvas.create_oval(
                x2 - 2 * radius, y1, x2, y1 + 2 * radius, fill=fill, outline="", tags=tags,
            ))
            items.append(canvas.create_oval(
                x1, y2 - 2 * radius, x1 + 2 * radius, y2, fill=fill, outline="", tags=tags,
            ))
            items.append(canvas.create_oval(
                x2 - 2 * radius, y2 - 2 * radius, x2, y2, fill=fill, outline="", tags=tags,
            ))
    if outline and width:
        if radius <= 0.5:
            items.append(canvas.create_rectangle(
                x1, y1, x2, y2, fill="", outline=outline, width=width, tags=tags,
            ))
        else:
            items.append(canvas.create_line(
                x1 + radius, y1, x2 - radius, y1, fill=outline, width=width, tags=tags,
            ))
            items.append(canvas.create_line(
                x1 + radius, y2, x2 - radius, y2, fill=outline, width=width, tags=tags,
            ))
            items.append(canvas.create_line(
                x1, y1 + radius, x1, y2 - radius, fill=outline, width=width, tags=tags,
            ))
            items.append(canvas.create_line(
                x2, y1 + radius, x2, y2 - radius, fill=outline, width=width, tags=tags,
            ))
            items.append(canvas.create_arc(
                x1, y1, x1 + 2 * radius, y1 + 2 * radius,
                start=90, extent=90, style="arc", outline=outline, width=width, tags=tags,
            ))
            items.append(canvas.create_arc(
                x2 - 2 * radius, y1, x2, y1 + 2 * radius,
                start=0, extent=90, style="arc", outline=outline, width=width, tags=tags,
            ))
            items.append(canvas.create_arc(
                x1, y2 - 2 * radius, x1 + 2 * radius, y2,
                start=180, extent=90, style="arc", outline=outline, width=width, tags=tags,
            ))
            items.append(canvas.create_arc(
                x2 - 2 * radius, y2 - 2 * radius, x2, y2,
                start=270, extent=90, style="arc", outline=outline, width=width, tags=tags,
            ))
    return items


def _round_rect(canvas, x1, y1, x2, y2, radius=6, **kwargs):
    return round_rect(
        canvas, x1, y1, x2, y2, radius=radius,
        fill=kwargs.get("fill", ""),
        outline=kwargs.get("outline", ""),
        width=kwargs.get("width", 1),
        tags=kwargs.get("tags", ()),
    )


def _clamp_frame(frame, last):
    try:
        frame = int(frame)
    except (TypeError, ValueError):
        frame = 0
    return max(0, min(frame, max(int(last), 0)))


class Tooltip:
    PALETTE = {"bg": "#1e2a33", "fg": "#e8eef3", "line": "#2a3540"}

    @classmethod
    def set_palette(cls, ui):
        cls.PALETTE = {
            "bg": ui.get("tooltip_bg", "#1e2a33"),
            "fg": ui.get("tooltip_fg", "#e8eef3"),
            "line": ui.get("line", "#2a3540"),
        }

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
            tip, text=self.text, justify="left",
            background=self.PALETTE["bg"], foreground=self.PALETTE["fg"],
            relief="flat", borderwidth=0, highlightthickness=1,
            highlightbackground=self.PALETTE["line"],
            font=UI_FONT_SMALL, wraplength=360, padx=8, pady=6,
        )
        label.pack()
        self.tip = tip

    def _hide(self, event=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class CollapsibleSection(ttk.Frame):
    def __init__(self, parent, title, collapsed=True, on_toggle=None, tooltip=None, ui=None):
        super().__init__(parent, style="Panel.TFrame")
        self._title = title
        self._collapsed = bool(collapsed)
        self._on_toggle = on_toggle
        self._ui = dict(ui or {})
        self._hover = False
        self._header = tk.Canvas(
            self, height=control_height(parent), highlightthickness=0, borderwidth=0,
            bg=self._ui.get("panel", "#161d24"), cursor="hand2",
        )
        self._header.pack(fill="x")
        self._header.bind("<Button-1>", lambda _event: self.toggle())
        self._header.bind("<Enter>", self._on_enter)
        self._header.bind("<Leave>", self._on_leave)
        self._header.bind("<Configure>", lambda _event: self._redraw_header())
        self._header.bind("<space>", lambda _event: self.toggle() or "break")
        self._header.bind("<Return>", lambda _event: self.toggle() or "break")
        self._header.configure(takefocus=1)
        if tooltip:
            Tooltip(self._header, tooltip)
        self.body = ttk.Frame(self, style="Panel.TFrame")
        if not self._collapsed:
            self.body.pack(fill="x", padx=(4, 0), pady=(0, 8))
        self._redraw_header()

    @property
    def collapsed(self):
        return self._collapsed

    def apply_theme(self, ui):
        self._ui = dict(ui or {})
        self._redraw_header()

    def _on_enter(self, _event=None):
        self._hover = True
        self._redraw_header()

    def _on_leave(self, _event=None):
        self._hover = False
        self._redraw_header()

    def _redraw_header(self):
        ui = self._ui
        bg = ui.get("hover" if self._hover else "panel", "#161d24")
        self._header.configure(bg=bg, height=control_height(self._header))
        self._header.delete("all")
        _clear_chrome_live(self._header)
        width = max(int(self._header.winfo_width()), 40)
        height = int(self._header.cget("height") or HEIGHT_BUTTON)
        chevron = "▸" if self._collapsed else "▾"
        self._header.create_text(
            8, height / 2, text=f"{chevron}  {self._title}",
            anchor="w", fill=ui.get("text", "#e8eef3"), font=UI_FONT,
        )
        if self._header.focus_get() is self._header:
            round_rect(
                self._header, 1, 1, width - 1, height - 1, RADIUS_CONTROL,
                fill="", outline=ui.get("accent_dim", "#4aa8b8"), width=1,
            )

    def toggle(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.body.pack_forget()
        else:
            self.body.pack(fill="x", padx=(4, 0), pady=(0, 8))
        self._redraw_header()
        if self._on_toggle:
            self._on_toggle()


class TimelineBar(tk.Canvas):
    def __init__(self, master, height=10, **kwargs):
        super().__init__(
            master, height=scale_px(master, height), bg="#10161c", highlightthickness=0,
            cursor="hand2", **kwargs,
        )
        self._colors = {
            "bg": "#10161c",
            "track": "#2a333b",
            "fill": "#3d7ea6",
            "thumb": "#e6e6e6",
            "rendered": "#78c7d5",
            "queued": "#557780",
        }
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

    def apply_theme(self, ui):
        self._colors = {
            "bg": ui.get("timeline_bg", "#10161c"),
            "track": ui.get("timeline_track", "#2a333b"),
            "fill": ui.get("timeline_fill", "#3d7ea6"),
            "thumb": ui.get("timeline_thumb", "#e6e6e6"),
            "rendered": ui.get("timeline_rendered", "#78c7d5"),
            "queued": ui.get("timeline_queued", "#557780"),
        }
        self.configure(bg=self._colors["bg"])
        self._redraw()

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
        pad = scale_px(self, 8)
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
        pad = scale_px(self, 8)
        y = h // 2
        x1 = w - pad
        span_width = max(x1 - pad, 1)
        colors = self._colors
        track_w = scale_px(self, 6)
        self.create_line(pad, y, x1, y, fill=colors["track"], width=track_w, capstyle="round")
        cache_y = max(y - 4, 2)
        for start, end in self._queued_ranges:
            self.create_line(
                self._x_from_value(start, pad, span_width), cache_y,
                self._x_from_value(end + 1, pad, span_width), cache_y,
                fill=colors["queued"], width=2,
            )
        for start, end in self._rendered_ranges:
            self.create_line(
                self._x_from_value(start, pad, span_width), cache_y,
                self._x_from_value(end + 1, pad, span_width), cache_y,
                fill=colors["rendered"], width=2,
            )
        x = pad + self._frac() * max(x1 - pad, 1)
        if self._max > self._min:
            self.create_line(pad, y, x, y, fill=colors["fill"], width=track_w, capstyle="round")
            r = max(4, scale_px(self, 6))
            self.create_oval(
                x - r, y - r, x + r, y + r,
                fill=colors["thumb"], outline=colors["bg"], width=2,
            )


def _hex_rgb(color):
    value = str(color or "").lstrip("#")
    if len(value) != 6:
        return (80, 160, 170)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


class ProgressRule(tk.Canvas):
    """Hairline window divider that thickens into a gradient fill while work runs."""

    def __init__(self, parent, ui=None):
        self._ui = dict(ui or {})
        self._value = 0.0
        self._maximum = 100.0
        self._photo = None
        self._draw_key = None
        super().__init__(
            parent, height=scale_px(parent, 7), highlightthickness=0, borderwidth=0,
            bg=self._ui.get("bg", "#0c1014"),
        )
        self.bind("<Configure>", lambda _event: self._redraw())
        self._redraw()

    def apply_theme(self, ui):
        self._ui = dict(ui or {})
        self._draw_key = None
        self.configure(bg=self._ui.get("bg", "#0c1014"))
        self._redraw()

    def __setitem__(self, key, value):
        if key == "value":
            self._value = max(0.0, float(value))
        elif key == "maximum":
            self._maximum = max(float(value), 1e-9)
        else:
            raise KeyError(key)
        self._redraw()

    def __getitem__(self, key):
        if key == "value":
            return self._value
        if key == "maximum":
            return self._maximum
        raise KeyError(key)

    def _ratio(self):
        if self._maximum <= 0:
            return 0.0
        return max(0.0, min(self._value / self._maximum, 1.0))

    def _fill_photo(self, width, height):
        start = _hex_rgb(self._ui.get("fill", "#2d8494"))
        end = _hex_rgb(self._ui.get("primary_active", "#5ec9d4"))
        tip = _hex_rgb(self._ui.get("accent", "#7fe8e8"))
        img = Image.new("RGBA", (width, height))
        pixels = img.load()
        glow = min(width, max(1, min(18, width // 6 or 1)))
        last = max(width - 1, 1)
        v_last = max(height - 1, 1)
        for x in range(width):
            t = x / last
            r = start[0] + (end[0] - start[0]) * t
            g = start[1] + (end[1] - start[1]) * t
            b = start[2] + (end[2] - start[2]) * t
            if x >= width - glow:
                head = (x - (width - glow)) / glow
                head *= head
                r += (tip[0] - r) * head * 0.7
                g += (tip[1] - g) * head * 0.7
                b += (tip[2] - b) * head * 0.7
            for y in range(height):
                sheen = 1.12 - 0.28 * (y / v_last)
                if y == 0:
                    sheen += 0.16
                elif y == height - 1:
                    sheen -= 0.08
                pixels[x, y] = (
                    max(0, min(255, int(r * sheen))),
                    max(0, min(255, int(g * sheen))),
                    max(0, min(255, int(b * sheen))),
                    255,
                )
        if height >= 3 and width >= 3:
            for y in (0, height - 1):
                pixels[width - 1, y] = (
                    pixels[width - 1, y][0],
                    pixels[width - 1, y][1],
                    pixels[width - 1, y][2],
                    0,
                )
        photo = ImageTk.PhotoImage(img, master=self)
        self._photo = photo
        return photo

    def _redraw(self):
        width = max(int(self.winfo_width()), 2)
        height = max(int(self.winfo_height()), 2)
        line = self._ui.get("line", "#2a3540")
        ratio = self._ratio()
        key = (width, height, round(ratio, 5), line, self._ui.get("fill"), self._ui.get("accent"))
        if key == self._draw_key:
            return
        self._draw_key = key
        self.delete("all")
        mid = height // 2
        self.create_line(0, mid, width, mid, fill=line, width=1)
        if ratio <= 0:
            self._photo = None
            return
        fill_w = max(1, int(round(width * ratio)))
        fill_h = max(4, scale_px(self, 4))
        y0 = max(0, mid - fill_h // 2)
        self.create_image(0, y0, image=self._fill_photo(fill_w, fill_h), anchor="nw")


class AccentSlider(tk.Canvas):
    """Scale-compatible inspector slider."""

    def __init__(
        self,
        master,
        from_=0.0,
        to=1.0,
        resolution=0.01,
        orient="horizontal",
        showvalue=False,
        variable=None,
        command=None,
        length=160,
        sliderlength=16,
        highlightthickness=0,
        troughcolor="#3d8eaa",
        background="#161d24",
        activebackground=None,
        highlightbackground=None,
        state="normal",
        **kwargs
    ):
        kwargs.pop("borderwidth", None)
        super().__init__(
            master, width=int(length), height=scale_px(master, 22),
            highlightthickness=0, borderwidth=0,
            bg=background or "#161d24", cursor="hand2",
        )
        self._from = float(from_)
        self._to = float(to)
        self._resolution = float(resolution) or 0.01
        self._state = state or "normal"
        self._troughcolor = troughcolor
        self._trackcolor = kwargs.pop("trackcolor", None) or "#2a333b"
        self._command = command
        self._variable = None
        self._syncing = False
        self._trace = None
        self._dragging = False
        self._hover = False
        self._items = None
        super().configure(takefocus=1, highlightthickness=0)
        if variable is not None:
            self._bind_variable(variable)
        self.bind("<Configure>", lambda _event: self._redraw(full=True))
        self.bind("<Button-1>", self._on_down)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_up)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<FocusIn>", lambda _event: self._redraw())
        self.bind("<FocusOut>", lambda _event: self._redraw())
        self.bind("<Left>", lambda _event: self._nudge(-1))
        self.bind("<Right>", lambda _event: self._nudge(1))
        self.bind("<Home>", lambda _event: self._jump(self._from))
        self.bind("<End>", lambda _event: self._jump(self._to))
        self._redraw(full=True)

    def _bind_variable(self, variable):
        if self._variable is not None and self._trace is not None:
            try:
                self._variable.trace_remove("write", self._trace)
            except tk.TclError:
                pass
        self._variable = variable
        if variable is not None:
            self._trace = variable.trace_add("write", self._on_var)
        else:
            self._trace = None

    def destroy(self):
        self._bind_variable(None)
        super().destroy()

    def _on_var(self, *_args):
        if not self._syncing:
            self._redraw()

    def _current(self):
        if self._variable is None:
            return self._from
        try:
            return float(self._variable.get())
        except (TypeError, ValueError, tk.TclError):
            return self._from

    def _clamp(self, value):
        low, high = (self._from, self._to) if self._from <= self._to else (self._to, self._from)
        value = max(low, min(high, float(value)))
        step = self._resolution
        if step > 0:
            value = round(value / step) * step
            value = max(low, min(high, value))
        return value

    def _value_from_x(self, x):
        pad = scale_px(self, 8)
        width = max(int(self.winfo_width()) - pad * 2, 1)
        frac = max(0.0, min(1.0, (float(x) - pad) / width))
        if self._to == self._from:
            return self._from
        return self._clamp(self._from + frac * (self._to - self._from))

    def _emit(self):
        if self._command is None:
            return
        try:
            self._command(str(self._current()))
        except TypeError:
            self._command()

    def _on_down(self, event):
        if self._state == "disabled":
            return
        self._dragging = True
        self.set(self._value_from_x(event.x))
        self._emit()

    def _on_drag(self, event):
        if not self._dragging or self._state == "disabled":
            return
        self.set(self._value_from_x(event.x))
        self._emit()

    def _on_up(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self.set(self._value_from_x(event.x))
        self._emit()

    def set(self, value):
        value = self._clamp(value)
        if self._variable is None:
            return
        try:
            current = float(self._variable.get())
        except (TypeError, ValueError, tk.TclError):
            current = None
        if current is not None and abs(current - value) < 1e-9:
            self._redraw()
            return
        self._syncing = True
        try:
            self._variable.set(value)
        finally:
            self._syncing = False
        self._redraw()

    def get(self):
        return self._current()

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "to":
            return self._to
        if key in {"from", "from_"}:
            return self._from
        if key == "resolution":
            return self._resolution
        if key == "troughcolor":
            return self._troughcolor
        if key == "background":
            return super().cget("bg")
        return super().cget(key)

    def config(self, cnf=None, **kwargs):
        if cnf:
            kwargs = dict(cnf, **kwargs)
        if "to" in kwargs:
            self._to = float(kwargs.pop("to"))
        if "from_" in kwargs:
            self._from = float(kwargs.pop("from_"))
        if "from" in kwargs:
            self._from = float(kwargs.pop("from"))
        if "resolution" in kwargs:
            self._resolution = float(kwargs.pop("resolution") or 0.01)
        if "state" in kwargs:
            self._state = kwargs.pop("state") or "normal"
            super().configure(cursor="" if self._state == "disabled" else "hand2")
        if "troughcolor" in kwargs:
            self._troughcolor = kwargs.pop("troughcolor")
        if "trackcolor" in kwargs:
            self._trackcolor = kwargs.pop("trackcolor")
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if "variable" in kwargs:
            self._bind_variable(kwargs.pop("variable"))
        bg = kwargs.pop("background", None)
        if bg is None:
            bg = kwargs.pop("bg", None)
        if bg is not None:
            super().configure(bg=bg)
        for ignored in (
            "activebackground", "highlightbackground", "highlightthickness",
            "sliderlength", "length", "orient", "showvalue", "borderwidth",
        ):
            kwargs.pop(ignored, None)
        if kwargs:
            super().configure(**kwargs)
        self._redraw(full=True)

    configure = config

    def _on_enter(self, _event=None):
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None):
        if not self._dragging:
            self._hover = False
            self._redraw()

    def _nudge(self, steps):
        if self._state == "disabled":
            return "break"
        self.set(self._current() + steps * self._resolution)
        self._emit()
        return "break"

    def _jump(self, value):
        if self._state == "disabled":
            return "break"
        self.set(value)
        self._emit()
        return "break"

    def _geometry(self):
        width = max(int(self.winfo_width()), 2)
        height = max(int(self.winfo_height()), 2)
        pad = scale_px(self, 8)
        y = height // 2
        span = max(width - pad * 2, 1)
        frac = 0.0
        if self._to != self._from:
            frac = (self._current() - self._from) / (self._to - self._from)
        frac = max(0.0, min(1.0, frac))
        x = pad + frac * span
        return pad, y, span, x

    def _redraw(self, full=False):
        pad, y, span, x = self._geometry()
        disabled = self._state == "disabled"
        track = self._trackcolor
        fill = track if disabled else self._troughcolor
        knob = "#d0d6db" if disabled else "#f7fafc"
        outline = "#6a7680" if disabled else "#1b242c"
        if self._hover and not disabled:
            outline = "#0f151a"
        radius = scale_px(self, SLIDER_THUMB) / 2.0
        track_w = scale_px(self, SLIDER_TRACK)
        if full or self._items is None:
            self.delete("all")
            track_id = self.create_line(
                pad, y, pad + span, y, fill=track, width=track_w, capstyle="round",
            )
            fill_id = self.create_line(
                pad, y, x, y, fill=fill, width=track_w, capstyle="round",
            )
            thumb_id = self.create_oval(
                x - radius, y - radius, x + radius, y + radius,
                fill=knob, outline=outline, width=1,
            )
            focus_id = self.create_oval(
                x - radius - 3, y - radius - 3, x + radius + 3, y + radius + 3,
                fill="", outline="", width=1, dash=(2, 2),
            )
            self._items = {
                "track": track_id, "fill": fill_id, "thumb": thumb_id, "focus": focus_id,
            }
        else:
            self.coords(self._items["track"], pad, y, pad + span, y)
            self.itemconfigure(self._items["track"], fill=track, width=track_w)
            self.coords(self._items["fill"], pad, y, x, y)
            self.itemconfigure(self._items["fill"], fill=fill, width=track_w)
            self.coords(
                self._items["thumb"],
                x - radius, y - radius, x + radius, y + radius,
            )
            self.itemconfigure(self._items["thumb"], fill=knob, outline=outline)
            self.coords(
                self._items["focus"],
                x - radius - 3, y - radius - 3, x + radius + 3, y + radius + 3,
            )
        focused = (self.focus_get() is self) and not disabled
        self.itemconfigure(
            self._items["focus"], outline=outline if focused else "",
        )

    def apply_theme(self, ui):
        self._trackcolor = ui.get("slider_trough_off", self._trackcolor)
        if self._state != "disabled":
            self._troughcolor = ui.get("slider_trough_on", self._troughcolor)
        super().configure(bg=ui.get("panel", super().cget("bg")))
        self._redraw(full=True)


class ChromeButton(tk.Canvas):
    """Shared button chrome: 32px default, 36px accent, 6px radius."""

    def __init__(
        self, parent, text="", command=None, ui=None, variant="default",
        width=None, takefocus=True, icon="", icon_only=False, primary=None,
    ):
        self._ui = dict(ui or {})
        self._text = text
        self._icon = icon or ""
        self._icon_only = bool(icon_only)
        self._icon_image = None
        self._command = command
        self._variant = variant
        self._state = "normal"
        self._hover = False
        self._pressed = False
        self._redrawing = False
        self._drawn_size = (0, 0)
        self._idle_redraw = None
        self._explicit_width = scale_px(parent, int(width)) if width else None
        if primary is None:
            primary = variant == "accent"
        height = control_height(parent, primary=bool(primary))
        super().__init__(
            parent, height=height, highlightthickness=0, borderwidth=0,
            bg=self._surface(), cursor="hand2",
        )
        if self._explicit_width:
            super().configure(width=self._explicit_width)
        super().configure(takefocus=1 if takefocus else 0)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", self._on_configure)
        self.bind("<Map>", self._on_map)
        self.bind("<Destroy>", self._on_destroy)
        self.bind("<space>", self._on_key)
        self.bind("<Return>", self._on_key)
        self.bind("<FocusIn>", lambda _event: self._redraw())
        self.bind("<FocusOut>", lambda _event: self._redraw())
        self._redraw()

    def _surface(self):
        if self._variant == "tool":
            return self._ui.get("timeline_bg", "#10161c")
        return self._ui.get("panel", "#161d24")

    def apply_theme(self, ui):
        self._ui = dict(ui or {})
        super().configure(bg=self._surface())
        self._redraw()

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "icon":
            return self._icon
        if key == "state":
            return self._state
        if key == "width":
            return int(super().cget("width"))
        return super().cget(key)

    def config(self, cnf=None, **kwargs):
        if cnf:
            kwargs = dict(cnf, **kwargs)
        if "text" in kwargs:
            self._text = kwargs.pop("text")
        if "icon" in kwargs:
            self._icon = kwargs.pop("icon") or ""
        if "icon_only" in kwargs:
            self._icon_only = bool(kwargs.pop("icon_only"))
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if "state" in kwargs:
            self._state = kwargs.pop("state") or "normal"
        if "variant" in kwargs:
            self._variant = kwargs.pop("variant") or "default"
            height = control_height(self, primary=self._variant == "accent")
            super().configure(height=height)
        if "primary" in kwargs:
            height = control_height(self, primary=bool(kwargs.pop("primary")))
            super().configure(height=height)
        if kwargs:
            super().configure(**kwargs)
        self._redraw()

    configure = config

    def state(self, states=None):
        if states is None:
            return [self._state]
        if "disabled" in states:
            self._state = "disabled"
        if "!disabled" in states:
            self._state = "normal"
        self._redraw()
        return [self._state]

    def instate(self, statespec):
        spec = list(statespec)
        disabled = self._state == "disabled"
        for item in spec:
            if item == "disabled" and not disabled:
                return False
            if item == "!disabled" and disabled:
                return False
        return True

    def _on_enter(self, _event=None):
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None):
        self._hover = False
        self._redraw()

    def _on_press(self, _event=None):
        if self._state == "disabled":
            return
        self._pressed = True
        self.focus_set()
        self._redraw()

    def _on_release(self, event=None):
        if self._state == "disabled":
            self._pressed = False
            self._redraw()
            return
        pressed = self._pressed
        self._pressed = False
        self._redraw()
        if not pressed or not self._command:
            return
        if event is not None:
            try:
                if (
                    event.x < 0 or event.y < 0
                    or event.x > self.winfo_width()
                    or event.y > self.winfo_height()
                ):
                    return
            except tk.TclError:
                pass
        self._command()

    def invoke(self):
        if self._state == "disabled":
            return
        if self._command:
            self._command()

    def _on_key(self, _event=None):
        if self._state == "disabled":
            return "break"
        self.invoke()
        return "break"

    def _on_map(self, _event=None):
        self._schedule_idle_redraw()

    def _on_destroy(self, _event=None):
        if self._idle_redraw is None:
            return
        try:
            self.after_cancel(self._idle_redraw)
        except tk.TclError:
            pass
        self._idle_redraw = None

    def _schedule_idle_redraw(self):
        if self._idle_redraw is not None:
            return
        try:
            self._idle_redraw = self.after_idle(self._idle_redraw_now)
        except tk.TclError:
            self._idle_redraw = None

    def _idle_redraw_now(self):
        self._idle_redraw = None
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._redraw()

    def _on_configure(self, event):
        if event.width < 8 or event.height < 8:
            return
        size = (int(event.width), int(event.height))
        if size == self._drawn_size:
            return
        if self._redrawing:
            self._schedule_idle_redraw()
            return
        self._redraw(size)

    def _palette(self):
        ui = self._ui
        disabled = self._state == "disabled"
        if self._variant == "accent":
            fill = ui.get("primary_bg", "#3aa0b0")
            fg = ui.get("primary_fg", "#071216")
            outline = ui.get("accent_dim", "#4aa8b8")
            if self._pressed:
                fill = ui.get("fill", fill)
            elif self._hover:
                fill = ui.get("primary_active", fill)
            if disabled:
                fill = ui.get("surface", fill)
                fg = ui.get("faint", fg)
                outline = ui.get("line", outline)
        elif self._variant == "outline":
            fill = ui.get("select_bg", ui.get("panel", "#161d24"))
            fg = ui.get("accent", "#7fe8e8")
            outline = ui.get("accent_dim", "#4aa8b8")
            if self._hover:
                fill = ui.get("hover", fill)
            if self._pressed:
                fill = ui.get("fill", fill)
                fg = ui.get("primary_fg", "#ffffff")
            if disabled:
                fg = ui.get("faint", fg)
                fill = ui.get("surface", fill)
                outline = ui.get("line", outline)
        elif self._variant == "danger":
            fill = ui.get("danger", "#c05454")
            fg = "#ffffff"
            outline = ui.get("danger", fill)
            if self._hover or self._pressed:
                fill = ui.get("failed", fill)
                outline = fill
            if disabled:
                fg = ui.get("faint", fg)
                fill = ui.get("surface", fill)
                outline = ui.get("line", outline)
        elif self._variant == "tool":
            fill = ui.get("transport_btn", "#1a232b")
            fg = ui.get("hud", ui.get("text", "#e8eef3"))
            outline = ui.get("transport_btn_border", ui.get("line", "#2a3540"))
            if self._hover:
                fill = ui.get("transport_btn_active", fill)
            if self._pressed:
                fill = ui.get("hover", fill)
            if disabled:
                fg = ui.get("faint", fg)
                fill = ui.get("transport_btn_disabled", ui.get("surface", fill))
        else:
            fill = ui.get("elev", "#1c242d")
            fg = ui.get("text", "#e8eef3")
            outline = ui.get("line", "#2a3540")
            if self._variant == "ghost":
                fill = ui.get("panel", fill)
            if self._hover:
                fill = ui.get("hover", fill)
            if self._pressed:
                fill = ui.get("select_bg", fill)
            if disabled:
                fg = ui.get("faint", fg)
                fill = ui.get("surface", fill)
        return fill, fg, outline

    def _redraw(self, size=None):
        if self._redrawing:
            self._schedule_idle_redraw()
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._redrawing = True
        try:
            self._redraw_body(size)
        finally:
            self._redrawing = False
        self._queue_stale_sync()

    def _queue_stale_sync(self):
        try:
            if not self.winfo_exists() or not self.winfo_ismapped():
                return
            actual = (int(self.winfo_width()), int(self.winfo_height()))
        except tk.TclError:
            return
        if actual[0] < 8 or actual[1] < 8:
            return
        if actual != self._drawn_size:
            self._schedule_idle_redraw()

    def _paint_size(self, needed, size=None):
        allocated_w = 0
        allocated_h = 0
        try:
            allocated_w = int(self.winfo_width())
            allocated_h = int(self.winfo_height())
        except tk.TclError:
            pass
        # Prefer the Configure event size. winfo_* can still be Tk's 10cm
        # canvas default, or the cell size can arrive before <Map>.
        if size is not None:
            event_w, event_h = int(size[0]), int(size[1])
            if event_w >= 8:
                allocated_w = event_w
            if event_h >= 8:
                allocated_h = event_h
        if allocated_w >= 8:
            width = allocated_w
        elif self._explicit_width:
            width = max(self._explicit_width, needed, 8)
        else:
            width = needed
            try:
                requested = int(super().cget("width") or 0)
            except (tk.TclError, TypeError, ValueError):
                requested = 0
            if requested != needed:
                super().configure(width=needed)
        if allocated_h >= 8:
            height = allocated_h
        else:
            height = int(super().cget("height") or HEIGHT_BUTTON)
        return max(int(width), 8), max(int(height), 8)

    def _redraw_body(self, size=None):
        self.delete("all")
        _clear_chrome_live(self)
        font = UI_FONT_BOLD if self._variant == "accent" else UI_FONT
        fill, fg, outline = self._palette()
        # Icon-only controls have enough room for an 18px glyph inside the
        # standard 36px desktop target.  Text buttons keep the quieter 16px
        # size so their label spacing and hierarchy stay balanced.
        icon_size = scale_px(self, 18 if self._icon_only else 16)
        self._icon_size = icon_size
        photo = icon_photo(self, self._icon, icon_size, fg) if self._icon else None
        self._icon_image = photo
        if photo and self._icon_only:
            needed = max(scale_px(self, 36), icon_size + 18)
        else:
            probe = self.create_text(-80, -80, text=self._text or " ", font=font)
            bbox = self.bbox(probe) or (0, 0, 24, 12)
            self.delete(probe)
            needed = int(bbox[2] - bbox[0] + 24)
            if photo:
                needed += icon_size + 8
        width, height = self._paint_size(needed, size)
        self._drawn_size = (width, height)
        round_rect(
            self, 1, 1, width - 1, height - 1, RADIUS_CONTROL,
            fill=fill, outline=outline, width=1,
        )
        if self._variant == "accent" and self._state != "disabled":
            highlight = self._ui.get("primary_active", fill)
            self.create_line(
                8, 2, width - 8, 2, fill=highlight, width=1,
            )
        if photo and self._icon_only:
            self.create_image(width / 2, height / 2, image=photo)
        elif photo:
            probe = self.create_text(-80, -80, text=self._text or " ", font=font)
            bbox = self.bbox(probe) or (0, 0, 24, 12)
            self.delete(probe)
            text_w = bbox[2] - bbox[0]
            group = icon_size + 8 + text_w
            x0 = max(10, (width - group) / 2)
            self.create_image(x0 + icon_size / 2, height / 2, image=photo)
            self.create_text(
                x0 + icon_size + 8, height / 2, text=self._text, fill=fg,
                font=font, anchor="w",
            )
        else:
            self.create_text(
                width / 2, height / 2, text=self._text, fill=fg, font=font,
            )
        if self.focus_get() is self and self._state != "disabled":
            round_rect(
                self, 3, 3, width - 3, height - 3, RADIUS_CONTROL - 1,
                fill="", outline=self._ui.get("accent_dim", "#4aa8b8"), width=1,
            )


class ChromeField(tk.Frame):
    """Rounded 32px field wrapping a ttk Entry, Spinbox or Combobox."""

    def __init__(self, parent, ui=None):
        self._ui = dict(ui or {})
        super().__init__(parent, bd=0, highlightthickness=0, bg=self._surface())
        self._height = control_height(parent)
        self._active_state = "normal"
        self._focused = False
        self._canvas = tk.Canvas(
            self, height=self._height, highlightthickness=0, bd=0, bg=self._surface(),
        )
        self._canvas.pack(fill="both", expand=True)
        self.inner = None
        self._window = None

    def _surface(self):
        return self._ui.get("panel", "#161d24")

    def _mount(self, inner, width=None):
        self.inner = inner
        min_width = int(width) if width else max(inner.winfo_reqwidth() + 16, 48)
        self._canvas.configure(width=min_width)
        self._window = self._canvas.create_window(
            8, self._height // 2, window=inner, anchor="w",
        )
        self._canvas.bind("<Configure>", self._layout)
        inner.bind("<FocusIn>", self._on_inner_focus, add="+")
        inner.bind("<FocusOut>", self._on_inner_blur, add="+")
        self._layout()

    def _on_inner_focus(self, _event=None):
        self._focused = True
        self._layout()

    def _on_inner_blur(self, _event=None):
        self._focused = False
        self._layout()

    def apply_theme(self, ui):
        self._ui = dict(ui or {})
        super().configure(bg=self._surface())
        self._canvas.configure(bg=self._surface())
        self._layout()

    def _layout(self, event=None):
        if self.inner is None or self._window is None:
            return
        width = max(int(self._canvas.winfo_width() or self.winfo_width() or 72), 48)
        self._canvas.delete("chrome")
        _clear_chrome_live(self._canvas)
        disabled = False
        try:
            disabled = str(self.inner.cget("state")) == "disabled"
        except tk.TclError:
            pass
        fill = self._ui.get("surface" if disabled else "entry_bg", "#1c242d")
        focused = self._focused and not disabled
        outline = self._ui.get("accent_dim" if focused else "line", "#2a3540")
        round_rect(
            self._canvas, 1, 1, width - 1, self._height - 1, RADIUS_CONTROL,
            fill=fill, outline=outline,
            width=1, tags=("chrome",),
        )
        self._canvas.coords(self._window, 8, self._height // 2)
        self._canvas.itemconfigure(
            self._window, width=max(width - 16, 40), height=self._height - 10,
        )
        try:
            self._canvas.tag_lower("chrome")
        except tk.TclError:
            pass

    def instate(self, spec):
        return self.inner.instate(spec)

    def get(self, *args):
        return self.inner.get(*args)

    def set(self, value):
        self.inner.set(value)

    def config(self, cnf=None, **kwargs):
        if isinstance(cnf, str) and not kwargs:
            return self.inner.config(cnf)
        if cnf:
            kwargs = dict(cnf, **kwargs)
        if "state" in kwargs and kwargs["state"] not in {None, "disabled"}:
            self._active_state = kwargs["state"]
        result = self.inner.config(**kwargs) if kwargs else self.inner.config()
        self._layout()
        return result

    configure = config

    def cget(self, key):
        return self.inner.cget(key)

    def bind(self, sequence=None, func=None, add=None):
        return self.inner.bind(sequence, func, add)

    def state(self, states=None):
        if states is None:
            try:
                return self.inner.state()
            except Exception:
                return [str(self.inner.cget("state"))]
        try:
            result = self.inner.state(states)
        except Exception:
            if "disabled" in states:
                self.inner.configure(state="disabled")
            if "!disabled" in states:
                self.inner.configure(state=self._active_state)
            result = [str(self.inner.cget("state"))]
        self._layout()
        return result


class ChromeSpinbox(ChromeField):
    """Rounded 32px numeric field wrapping ttk.Spinbox."""

    def __init__(self, parent, ui=None, **spin_kwargs):
        super().__init__(parent, ui=ui)
        spin_kwargs.setdefault("style", "SliderValue.TSpinbox")
        spin_kwargs.setdefault("font", UI_MONO)
        spin_kwargs.setdefault("justify", "right")
        self.spin = ttk.Spinbox(self._canvas, **spin_kwargs)
        self._mount(self.spin, width=max(84, self.spin.winfo_reqwidth() + 16))


class ChromeCombobox(ChromeField):
    """Rounded 32px combobox wrapping ttk.Combobox."""

    def __init__(self, parent, ui=None, **combo_kwargs):
        super().__init__(parent, ui=ui)
        combo_kwargs.setdefault("style", "Chrome.TCombobox")
        combo_kwargs.setdefault("font", UI_FONT)
        combo_kwargs.setdefault("exportselection", False)
        self._active_state = combo_kwargs.get("state", "readonly") or "readonly"
        self.combo = ttk.Combobox(self._canvas, **combo_kwargs)
        self._mount(self.combo)
        install_combobox_behavior(self)
        self.combo.bind("<<ComboboxSelected>>", self._commit_value, add="+")

    def bind(self, sequence=None, func=None, add=None):
        if sequence == "<<ComboboxSelected>>" and func is not None:
            add = "+"
        return super().bind(sequence, func, add)

    def _on_inner_focus(self, event=None):
        super()._on_inner_focus(event)
        self._commit_value()

    def _on_inner_blur(self, event=None):
        self._commit_value()
        super()._on_inner_blur(event)

    def _commit_value(self, event=None):
        try:
            self.combo.selection_clear()
        except tk.TclError:
            pass
        try:
            self.after_idle(self._clear_committed_selection)
        except tk.TclError:
            pass
        return None

    def _clear_committed_selection(self):
        try:
            self.combo.selection_clear()
        except tk.TclError:
            pass


class ChromeEntry(ChromeField):
    """Rounded 32px text field wrapping ttk.Entry."""

    def __init__(self, parent, ui=None, **entry_kwargs):
        super().__init__(parent, ui=ui)
        entry_kwargs.setdefault("style", "Chrome.TEntry")
        entry_kwargs.setdefault("font", UI_MONO)
        self.entry = ttk.Entry(self._canvas, **entry_kwargs)
        self._mount(self.entry)

    def set(self, value):
        self.entry.delete(0, "end")
        self.entry.insert(0, value)

    def insert(self, index, value):
        return self.entry.insert(index, value)

    def delete(self, first, last=None):
        return self.entry.delete(first, last)


class ChipGroup(tk.Canvas):
    """Compact pill choices matching the studio mockup."""

    def __init__(
        self, parent, variable, choices, command=None, ui=None, tone="panel",
        connected=False,
    ):
        self._ui = dict(ui or {})
        self._tone = tone
        self._connected = bool(connected)
        super().__init__(
            parent, height=control_height(parent), highlightthickness=0, borderwidth=0,
            bg=self._bg(), cursor="hand2",
        )
        self._variable = variable
        self._command = command
        self._choices = list(choices)
        self._hits = []
        self._hover = None
        self._trace = variable.trace_add("write", lambda *_args: self._redraw())
        super().configure(takefocus=1)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Configure>", lambda _event: self._redraw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda _event: self._set_hover(None))
        self.bind("<Left>", lambda _event: self._nudge(-1))
        self.bind("<Right>", lambda _event: self._nudge(1))
        self.bind("<FocusIn>", lambda _event: self._redraw())
        self.bind("<FocusOut>", lambda _event: self._redraw())
        self._redraw()

    def destroy(self):
        trace = self._trace
        self._trace = None
        if trace is not None:
            try:
                self._variable.trace_remove("write", trace)
            except tk.TclError:
                pass
        super().destroy()

    def _bg(self):
        if self._tone == "transport":
            return self._ui.get("timeline_bg", "#10161c")
        return self._ui.get("panel", "#161d24")

    def apply_theme(self, ui):
        self._ui = dict(ui or {})
        self.configure(bg=self._bg())
        self._redraw()

    def _set_hover(self, name):
        if self._hover == name:
            return
        self._hover = name
        self._redraw()

    def _on_motion(self, event):
        for name, x0, x1 in self._hits:
            if x0 <= event.x <= x1:
                self._set_hover(name)
                return
        self._set_hover(None)

    def _nudge(self, step):
        if not self._choices:
            return "break"
        try:
            current = self._variable.get()
        except tk.TclError:
            current = self._choices[0]
        index = self._choices.index(current) if current in self._choices else 0
        index = max(0, min(len(self._choices) - 1, index + step))
        name = self._choices[index]
        if name != current:
            self._variable.set(name)
            if self._command:
                self._command()
        return "break"

    def _on_click(self, event):
        self.focus_set()
        for name, x0, x1 in self._hits:
            if x0 <= event.x <= x1:
                if self._variable.get() != name:
                    self._variable.set(name)
                    if self._command:
                        self._command()
                return

    def _redraw(self):
        self.delete("all")
        _clear_chrome_live(self)
        ui = self._ui
        current = ""
        try:
            current = self._variable.get()
        except tk.TclError:
            pass
        font = UI_FONT
        height = control_height(self)
        sizes = []
        for name in self._choices:
            probe = self.create_text(0, -40, text=name, font=font)
            bbox = self.bbox(probe) or (0, 0, 24, 12)
            self.delete(probe)
            sizes.append((name, max(bbox[2] - bbox[0], 12) + 20))
        gap = 0 if self._connected else 8
        x = 1
        if self._connected and sizes:
            total = sum(width for _name, width in sizes) + 2
            round_rect(
                self, 1, 2, total, height - 2, RADIUS_CONTROL,
                fill=ui.get("elev", "#1c242d") if self._tone != "transport"
                else ui.get("timeline_bg", "#10161c"),
                outline=ui.get("line2" if self._tone == "transport" else "line", "#2a3540"),
                width=1,
            )
            x = 2
        self._hits = []
        for name, width in sizes:
            on = name == current
            hover = name == self._hover
            fill = ui.get("select_bg", "#1a2d34") if on else ui.get("elev", "#1c242d")
            if hover and not on:
                fill = ui.get("hover", fill)
            outline = ui.get("accent_dim", "#4aa8b8") if on else ui.get("line", "#2a3540")
            fg = ui.get("accent", "#7fe8e8") if on else ui.get("muted", "#8b97a3")
            if self._connected:
                if on:
                    round_rect(
                        self, x, 3, x + width, height - 3, max(RADIUS_CONTROL - 1, 3),
                        fill=ui.get("select_bg", fill), outline="", width=0,
                    )
            else:
                round_rect(
                    self, x, 2, x + width, height - 2, RADIUS_CONTROL,
                    fill=fill, outline=outline, width=1,
                )
            self.create_text(
                x + width / 2, height / 2, text=name, fill=fg, font=font,
            )
            self._hits.append((name, x, x + width))
            x += width + gap
        super().configure(width=max(x, 40), height=height)
        # Separate chips already show selection on the active pill; a group
        # focus ring around the gaps reads as a stray box after a click.
        if self._connected and self.focus_get() is self:
            round_rect(
                self, 1, 1, max(x - 1, 8), height - 1,
                RADIUS_CONTROL, fill="", outline=ui.get("accent_dim", "#4aa8b8"), width=1,
            )


class SegmentedBar(tk.Frame):
    def __init__(self, parent, variable, choices, command=None, ui=None):
        bg = (ui or {}).get("timeline_bg", "#10161c")
        super().__init__(parent, bg=bg, highlightthickness=0)
        self._group = ChipGroup(
            self, variable, choices, command=command, ui=ui, tone="transport",
            connected=True,
        )
        self._group.pack(side="left")

    def apply_theme(self, ui):
        self.configure(bg=ui.get("timeline_bg", "#10161c"))
        self._group.apply_theme(ui)


class CheckToggle(tk.Canvas):
    """Teal rounded checkbox used in the inspector."""

    def __init__(self, parent, text, variable, command=None, ui=None):
        self._ui = dict(ui or {})
        super().__init__(
            parent, height=control_height(parent), highlightthickness=0, borderwidth=0,
            bg=self._ui.get("panel", "#161d24"), cursor="hand2",
        )
        self._text = text
        self._variable = variable
        self._command = command
        self._state = "normal"
        self._hover = False
        self._trace = variable.trace_add("write", lambda *_args: self._redraw())
        super().configure(takefocus=1)
        self.bind("<Button-1>", self._toggle)
        self.bind("<Configure>", lambda _event: self._redraw())
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<space>", self._on_key)
        self.bind("<Return>", self._on_key)
        self.bind("<FocusIn>", lambda _event: self._redraw())
        self.bind("<FocusOut>", lambda _event: self._redraw())
        self._redraw()

    def destroy(self):
        trace = self._trace
        self._trace = None
        if trace is not None:
            try:
                self._variable.trace_remove("write", trace)
            except tk.TclError:
                pass
        super().destroy()

    def apply_theme(self, ui):
        self._ui = dict(ui or {})
        self.configure(bg=self._ui.get("panel", "#161d24"))
        self._redraw()

    def _checked(self):
        try:
            return bool(self._variable.get())
        except tk.TclError:
            return False

    def _set_hover(self, hover):
        self._hover = hover
        self._redraw()

    def _on_key(self, _event=None):
        self._toggle()
        return "break"

    def _toggle(self, _event=None):
        if self._state == "disabled":
            return
        self.focus_set()
        self._variable.set(not self._checked())
        if self._command:
            self._command()

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().cget(key)

    def config(self, cnf=None, **kwargs):
        if cnf:
            kwargs = dict(cnf, **kwargs)
        if "state" in kwargs:
            self._state = kwargs.pop("state") or "normal"
            super().configure(cursor="" if self._state == "disabled" else "hand2")
        if "text" in kwargs:
            self._text = kwargs.pop("text")
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if kwargs:
            super().configure(**kwargs)
        self._redraw()

    configure = config

    def state(self, states=None):
        if states is None:
            return [self._state]
        if "disabled" in states:
            self._state = "disabled"
        if "!disabled" in states:
            self._state = "normal"
        super().configure(cursor="" if self._state == "disabled" else "hand2")
        self._redraw()
        return [self._state]

    def _redraw(self):
        self.delete("all")
        _clear_chrome_live(self)
        ui = self._ui
        font = UI_FONT
        probe = self.create_text(0, -40, text=self._text, font=font)
        bbox = self.bbox(probe) or (0, 0, 48, 12)
        self.delete(probe)
        text_w = bbox[2] - bbox[0]
        height = control_height(self)
        super().configure(width=text_w + 32, height=height)
        cy = height / 2
        on = self._checked() and self._state != "disabled"
        box = ui.get("fill", "#3d8eaa") if on else ui.get("elev", "#1c242d")
        if self._hover and not on and self._state != "disabled":
            box = ui.get("hover", box)
        outline = ui.get("accent_dim", "#5fb3c8") if on else ui.get("line", "#2a3540")
        round_rect(self, 2, cy - 7, 16, cy + 7, 3, fill=box, outline=outline, width=1)
        if on:
            self.create_line(5, cy, 8, cy + 4, 13, cy - 4, fill="#e9fbff", width=2, capstyle="round")
        fg = ui.get("faint", "#62707c") if self._state == "disabled" else ui.get("text", "#e8eef3")
        self.create_text(22, cy, text=self._text, fill=fg, font=font, anchor="w")
        if self.focus_get() is self and self._state != "disabled":
            round_rect(
                self, 1, 1, text_w + 30, height - 1, RADIUS_CONTROL,
                fill="", outline=ui.get("accent_dim", "#4aa8b8"), width=1,
            )


class StudioNotebook(tk.Frame):
    """Underline tab strip + page host. ttk.Notebook-compatible subset."""

    def __init__(self, master, ui=None, **kwargs):
        self._ui = dict(ui or {})
        super().__init__(
            master, bg=self._ui.get("panel", "#161d24"), highlightthickness=0,
        )
        self._header = tk.Canvas(
            self, height=scale_px(self, 38), highlightthickness=0, borderwidth=0,
            bg=self._ui.get("panel", "#161d24"),
        )
        self._header.pack(fill="x")
        self._rule = tk.Frame(self, height=1, bg=self._ui.get("line", "#2a3540"))
        self._rule.pack(fill="x")
        self.content = tk.Frame(self, bg=self._ui.get("panel", "#161d24"))
        self.content.pack(fill="both", expand=True)
        self._tabs = []
        self._current = None
        self._header.bind("<Button-1>", self._on_click)
        self._header.bind("<Configure>", lambda _event: self._redraw())

    def apply_theme(self, ui):
        self._ui = dict(ui or {})
        bg = self._ui.get("panel", "#161d24")
        self.configure(bg=bg)
        self._header.configure(bg=bg)
        self.content.configure(bg=bg)
        self._rule.configure(bg=self._ui.get("line", "#2a3540"))
        for item in self._tabs:
            try:
                item["frame"].configure(style="Panel.TFrame")
            except tk.TclError:
                pass
        self._redraw()

    def add(self, child, text="", badge=""):
        self._tabs.append({"frame": child, "text": text, "badge": badge})
        if self._current is None:
            self.select(child)
        else:
            try:
                child.pack_forget()
            except tk.TclError:
                pass
        self._redraw()

    def set_badge(self, child, text=""):
        index = self._index(child)
        item = self._tabs[index]
        badge = "" if text is None else str(text)
        if item.get("badge") == badge:
            return
        item["badge"] = badge
        self._redraw()

    def _index(self, child):
        if isinstance(child, int):
            return child
        for index, item in enumerate(self._tabs):
            if item["frame"] is child:
                return index
        return 0

    def select(self, child):
        index = self._index(child)
        if not self._tabs:
            return
        index = max(0, min(index, len(self._tabs) - 1))
        target = self._tabs[index]["frame"]
        if self._current is target:
            self._redraw()
            return
        self._current = target
        for item in self._tabs:
            try:
                item["frame"].pack_forget()
            except tk.TclError:
                pass
        self._current.pack(in_=self.content, fill="both", expand=True)
        self._redraw()

    def tab(self, child, option=None, **kwargs):
        index = self._index(child)
        item = self._tabs[index]
        if "text" in kwargs:
            item["text"] = kwargs["text"]
            self._redraw()
            return None
        if option == "text":
            return item["text"]
        return item["text"]

    def _on_click(self, event):
        for item, x0, x1 in self._hit_map():
            if x0 <= event.x <= x1:
                self.select(item["frame"])
                return

    def _hit_map(self):
        width = max(int(self._header.winfo_width()), 1)
        count = max(len(self._tabs), 1)
        slot = width / count
        hits = []
        for index, item in enumerate(self._tabs):
            hits.append((item, index * slot, (index + 1) * slot))
        return hits

    def _redraw(self):
        self._header.delete("all")
        _clear_chrome_live(self._header)
        ui = self._ui
        bg = ui.get("panel", "#161d24")
        height = scale_px(self, 38)
        self._header.configure(bg=bg, height=height)
        for item, x0, x1 in self._hit_map():
            on = item["frame"] is self._current
            fg = ui.get("text", "#e8eef3") if on else ui.get("muted", "#8b97a3")
            font = UI_FONT_BOLD if on else UI_FONT
            cx = (x0 + x1) / 2
            title_y = height / 2 - 2
            badge = item.get("badge") or ""
            if badge:
                probe = self._header.create_text(0, -40, text=item["text"], font=font)
                title_box = self._header.bbox(probe) or (0, 0, 24, 12)
                self._header.delete(probe)
                title_w = title_box[2] - title_box[0]
                probe = self._header.create_text(0, -40, text=badge, font=UI_FONT_SMALL)
                badge_box = self._header.bbox(probe) or (0, 0, 12, 10)
                self._header.delete(probe)
                badge_w = badge_box[2] - badge_box[0] + 10
                gap = 6
                block = title_w + gap + badge_w
                title_x = cx - block / 2 + title_w / 2
                badge_x = cx + block / 2 - badge_w / 2
                self._header.create_text(title_x, title_y, text=item["text"], fill=fg, font=font)
                bx0, by0 = badge_x - badge_w / 2, title_y - 8
                round_rect(
                    self._header, bx0, by0, bx0 + badge_w, by0 + 16, 8,
                    fill=ui.get("select_bg", "#1a2d34"),
                    outline=ui.get("line", "#2a3540"), width=1,
                )
                self._header.create_text(
                    badge_x, title_y, text=badge, fill=ui.get("accent", "#7fe8e8"),
                    font=UI_FONT_SMALL,
                )
            else:
                self._header.create_text(cx, title_y, text=item["text"], fill=fg, font=font)
            if on:
                self._header.create_line(
                    x0 + 18, height - 2, x1 - 18, height - 2,
                    fill=ui.get("accent", "#7fe8e8"), width=2,
                )


class StatusPills(tk.Frame):
    def __init__(self, parent, ui=None):
        self._ui = dict(ui or {})
        super().__init__(parent, bg=self._ui.get("surface", "#12181e"))
        self._labels = []

    def apply_theme(self, ui):
        self._ui = dict(ui or {})
        self.configure(bg=self._ui.get("surface", "#12181e"))
        items = getattr(self, "_last_items", None)
        self._last_items = None
        if items:
            self.set_pills(items)

    def set_pills(self, items):
        items = list(items)
        if getattr(self, "_last_items", None) == items:
            return
        self._last_items = items
        for label in self._labels:
            label.destroy()
        self._labels = []
        ui = self._ui
        for text, kind in items:
            if kind == "ok":
                fg = ui.get("ok", "#7dcea0")
                bg = ui.get("pill_ok_bg", ui.get("elev", "#1a262e"))
                line = ui.get("ok", "#7dcea0")
            elif kind == "warn":
                fg = ui.get("warn", "#e0b86a")
                bg = ui.get("pill_warn_bg", ui.get("elev", "#1a262e"))
                line = ui.get("warn", "#e0b86a")
            else:
                fg = ui.get("muted", "#b7c6cf")
                bg = ui.get("pill_bg", ui.get("elev", "#1a262e"))
                line = ui.get("line", "#2c3b45")
            label = tk.Label(
                self, text=text, font=UI_FONT_SMALL,
                fg=fg, bg=bg, padx=7, pady=2,
                highlightthickness=1, highlightbackground=line, bd=0,
            )
            label.pack(side="left", padx=(0, 6))
            self._labels.append(label)
