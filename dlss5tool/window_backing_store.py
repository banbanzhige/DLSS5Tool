"""Retain painted client pixels for native restore paints, without an overlay.

Tk keeps its normal paint path. During restore, each invalid native region can
first reuse pixels from the last complete client frame instead of exposing an
uninitialized surface while the queued Tk draw is still pending.
"""
import ctypes as c
from ctypes import wintypes as w
import sys
import time
import tkinter as tk
from tkinter import ttk
import weakref

import numpy as np

from dlss5tool import ui_theme

MAX_FRAME_BYTES = 32 * 1024 * 1024
_LIVE = {}


def has_blank_blocks(pixels, exclusions=(), block=8):
    """Reject unfinished black UI rectangles; media may legitimately be black."""
    height, width = pixels.shape[:2]
    if height < block or width < block:
        return False
    # Every solid block contains a 2x2 square on this half-block grid. Scan
    # that small grid first, then verify only its candidate neighborhoods.
    # Avoid reducing three color channels over an entire 4K frame on the UI
    # thread when almost all pixels are normal, non-black UI colors.
    stride = max(1, block//2)
    samples = np.all(pixels[::stride, ::stride, :3] < 12, axis=2)
    for x1, y1, x2, y2 in exclusions:
        samples[max(0, (y1+stride-1)//stride):max(0, (y2+stride-1)//stride),
                max(0, (x1+stride-1)//stride):max(0, (x2+stride-1)//stride)] = False
    candidates = samples[:-1, :-1] & samples[1:, :-1] & samples[:-1, 1:] & samples[1:, 1:]
    while candidates.any():
        row, column = np.unravel_index(candidates.argmax(), candidates.shape)
        candidates[row, column] = False
        x = max(0, column*stride-stride+1)
        y = max(0, row*stride-stride+1)
        right, bottom = min(width, column*stride+block), min(height, row*stride+block)
        black = np.all(pixels[y:bottom, x:right, :3] < 12, axis=2)
        for x1, y1, x2, y2 in exclusions:
            black[max(0, y1-y):max(0, y2-y), max(0, x1-x):max(0, x2-x)] = False
        ph, pw = black.shape
        if ph < block or pw < block:
            continue
        rows = black[:, :pw-block+1].copy()
        for offset in range(1, block):
            rows &= black[:, offset:pw-block+1+offset]
        squares = rows[:ph-block+1].copy()
        for offset in range(1, block):
            squares &= rows[offset:ph-block+1+offset]
        if squares.any():
            return True
    return False


class _Header(c.Structure):
    _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG),
                ('planes', w.WORD), ('bits', w.WORD), ('compression', w.DWORD),
                ('image_size', w.DWORD), ('xppm', w.LONG), ('yppm', w.LONG),
                ('used', w.DWORD), ('important', w.DWORD)]


class _Bitmap:
    def __init__(self, api, width, height):
        self.api, self.width, self.height = api, width, height
        self.dc = self.bitmap = self.previous = None
        self.bits = c.c_void_p()
        try:
            self.dc = api.gdi.CreateCompatibleDC(None)
            header = _Header(c.sizeof(_Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            self.bitmap = api.gdi.CreateDIBSection(self.dc, c.byref(header), 0,
                                                  c.byref(self.bits), None, 0)
            if not self.dc or not self.bitmap or not self.bits.value:
                raise c.WinError(c.get_last_error())
            self.previous = api.gdi.SelectObject(self.dc, self.bitmap)
            if not self.previous or self.previous == c.c_void_p(-1).value:
                self.previous = None
                raise c.WinError(c.get_last_error())
        except Exception:
            self.close()
            raise

    def pixels(self):
        data = (c.c_ubyte * (self.width*self.height*4)).from_address(self.bits.value)
        return np.ctypeslib.as_array(data).reshape(self.height, self.width, 4)

    def close(self):
        if self.previous and self.dc:
            self.api.gdi.SelectObject(self.dc, self.previous)
        if self.bitmap:
            self.api.gdi.DeleteObject(self.bitmap)
        if self.dc:
            self.api.gdi.DeleteDC(self.dc)
        self.dc = self.bitmap = self.previous = None


class WindowBackingStore:
    TICK_MS = 60

    def __init__(self, window, *, exclusions=None):
        self._window = weakref.ref(window)
        self._exclusions = exclusions or (lambda: ())
        self.hwnd = None
        self.frame = None
        self.error = None
        self._hooks, self._widgets, self._brushes, self._bindings = {}, {}, {}, []
        self._after = None
        self._callback = None
        self._closed = self._capturing = self._minimized = self._replaying = False
        self._dirty = True
        self._last_dirty = time.monotonic()
        self._retry_after = 0.
        self._failed_captures = 0
        self._cached_exclusions = ()
        self._layout = {}
        self.captures = self.replays = self.rejected = 0
        self.capture_method = 'dc'
        self.last_capture_ms = 0.
        if sys.platform != 'win32':
            return
        try:
            self._load_api()
            self._attach_tree()
            for sequence, callback in (
                    ('<Map>', self._mapped), ('<Expose>', self._exposed),
                    ('<Configure>', self._configured), ('<Destroy>', self._destroyed),
                    ('<ButtonRelease>', self._changed), ('<KeyRelease>', self._changed),
                    ('<MouseWheel>', self._changed), ('<<ComboboxSelected>>', self._changed)):
                self._bindings.append((sequence, window.bind(sequence, callback, add='+')))
            self._after = window.after(self.TICK_MS, self._tick)
        except (AttributeError, OSError, tk.TclError) as error:
            self.error = str(error)
            self.close()

    def _load_api(self):
        self.user = c.WinDLL('user32', use_last_error=True)
        self.gdi = c.WinDLL('gdi32', use_last_error=True)
        self.comctl = c.WinDLL('comctl32', use_last_error=True)
        self._proc_type = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, c.c_size_t,
                                       c.c_ssize_t, c.c_size_t, c.c_size_t)
        specs = [
            (self.user, 'GetClientRect', [w.HWND, c.POINTER(w.RECT)], w.BOOL),
            (self.user, 'ClientToScreen', [w.HWND, c.POINTER(w.POINT)], w.BOOL),
            (self.user, 'IsIconic', [w.HWND], w.BOOL),
            (self.user, 'IsWindowVisible', [w.HWND], w.BOOL),
            (self.user, 'GetForegroundWindow', [], w.HWND),
            (self.user, 'GetDC', [w.HWND], w.HDC),
            (self.user, 'ReleaseDC', [w.HWND, w.HDC], c.c_int),
            (self.user, 'FillRect', [w.HDC, c.POINTER(w.RECT), w.HBRUSH], c.c_int),
            (self.gdi, 'CreateCompatibleDC', [w.HDC], w.HDC),
            (self.gdi, 'CreateDIBSection', [w.HDC, c.c_void_p, w.UINT, c.POINTER(c.c_void_p), w.HANDLE, w.DWORD], w.HBITMAP),
            (self.gdi, 'SelectObject', [w.HDC, c.c_void_p], c.c_void_p),
            (self.gdi, 'DeleteObject', [c.c_void_p], w.BOOL),
            (self.gdi, 'DeleteDC', [w.HDC], w.BOOL),
            (self.gdi, 'CreateSolidBrush', [w.DWORD], w.HBRUSH),
            (self.gdi, 'BitBlt', [w.HDC, c.c_int, c.c_int, c.c_int, c.c_int,
                                w.HDC, c.c_int, c.c_int, w.DWORD], w.BOOL),
            (self.gdi, 'GdiFlush', [], w.BOOL),
            (self.comctl, 'SetWindowSubclass', [w.HWND, self._proc_type, c.c_size_t, c.c_size_t], w.BOOL),
            (self.comctl, 'RemoveWindowSubclass', [w.HWND, self._proc_type, c.c_size_t], w.BOOL),
            (self.comctl, 'DefSubclassProc', [w.HWND, w.UINT, c.c_size_t, c.c_ssize_t], c.c_ssize_t),
        ]
        for dll, name, args, result in specs:
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
        owner, default = weakref.ref(self), self.comctl.DefSubclassProc

        def dispatch(hwnd, message, wp, lp, ident, data):
            instance = owner()
            if instance is None:
                return default(hwnd, message, wp, lp)
            return instance._wndproc(hwnd, message, wp, lp)
        self._callback = self._proc_type(dispatch)

    def _brush(self, widget):
        color = None
        try:
            color = widget.cget('background')
        except tk.TclError:
            style = ttk.Style(widget)
            color = style.lookup(widget.cget('style') or widget.winfo_class(), 'background')
        rgb = widget.winfo_rgb(color or '#f0f2f5')
        colorref = (rgb[0]//257) | (rgb[1]//257 << 8) | (rgb[2]//257 << 16)
        if colorref not in self._brushes:
            self._brushes[colorref] = self.gdi.CreateSolidBrush(colorref)
            if not self._brushes[colorref]:
                self._brushes.pop(colorref)
                raise c.WinError(c.get_last_error())
        return self._brushes[colorref]

    def _attach(self, hwnd, widget):
        if self._closed or not hwnd or hwnd in self._hooks:
            return
        brush = self._brush(widget)
        if not self.comctl.SetWindowSubclass(hwnd, self._callback, id(self), 0):
            raise c.WinError(c.get_last_error())
        self._hooks[hwnd] = brush
        self._widgets[hwnd] = weakref.ref(widget)
        _LIVE[hwnd, id(self)] = self

    def _attach_tree(self):
        window = self._window()
        if self._closed or window is None:
            return
        self.hwnd = ui_theme._window_hwnd(window)
        self._attach(self.hwnd, window)
        pending = [window]
        while pending:
            widget = pending.pop()
            if widget is not window and isinstance(widget, (tk.Tk, tk.Toplevel)):
                continue
            if widget.winfo_ismapped():
                self._attach(widget.winfo_id(), widget)
                pending.extend(widget.winfo_children())

    def _mapped(self, event):
        window = self._window()
        if self._closed or window is None:
            return
        if event.widget is window:
            self._attach_tree()
        elif isinstance(event.widget, tk.Misc) and event.widget.winfo_toplevel() is window:
            self._attach(event.widget.winfo_id(), event.widget)
        self._changed(event)

    def _exposed(self, event):
        self._dirty = True
        self._last_dirty = time.monotonic()

    def _configured(self, event):
        if event.widget is self._window() and self.frame is not None:
            rect = w.RECT()
            if (not self.hwnd or not self.user.GetClientRect(self.hwnd, c.byref(rect))
                    or (rect.right, rect.bottom) != (self.frame.width, self.frame.height)):
                self._drop_frame()
        self._changed(event)

    def _changed(self, event=None):
        self._dirty = True
        self._last_dirty = time.monotonic()

    def invalidate(self):
        self._replaying = False
        self._cached_exclusions = ()
        self._drop_frame()
        self._changed()

    def refresh_palette(self):
        self.invalidate()
        for hwnd, reference in list(self._widgets.items()):
            widget = reference()
            if widget is not None:
                try:
                    self._hooks[hwnd] = self._brush(widget)
                except tk.TclError:
                    pass

    def _tick(self):
        self._after = None
        window = self._window()
        if self._closed or window is None:
            return
        now = time.monotonic()
        if self._dirty and now >= self._retry_after and now-self._last_dirty >= .025:
            try:
                self.capture()
            except (OSError, tk.TclError, ValueError, MemoryError) as error:
                self.error = str(error)
                self._retry_after = time.monotonic()+1.
        self._after = window.after(self.TICK_MS if window.winfo_ismapped() else 250, self._tick)

    def _relative_rect(self, hwnd):
        rect, origin, root_origin = w.RECT(), w.POINT(), w.POINT()
        if (self.hwnd and self.user.GetClientRect(hwnd, c.byref(rect))
                and self.user.ClientToScreen(hwnd, c.byref(origin))
                and self.user.ClientToScreen(self.hwnd, c.byref(root_origin))):
            return origin.x-root_origin.x, origin.y-root_origin.y, rect.right, rect.bottom
        return None

    def capture(self, *, native=False):
        if (self._closed or not self.hwnd or self.user.IsIconic(self.hwnd)
                or not self.user.IsWindowVisible(self.hwnd)
                or self.user.GetForegroundWindow() != self.hwnd):
            return False
        rect = w.RECT()
        if not self.user.GetClientRect(self.hwnd, c.byref(rect)):
            return False
        width, height = rect.right, rect.bottom
        if width <= 0 or height <= 0 or width*height*4 > MAX_FRAME_BYTES:
            return False
        candidate = _Bitmap(self, width, height)
        dc = None
        began = time.perf_counter()
        self._capturing = True
        try:
            dc = self.user.GetDC(self.hwnd)
            success = dc and self.gdi.BitBlt(candidate.dc, 0, 0, width, height, dc, 0, 0, 0x00CC0020)
            self.gdi.GdiFlush()
            if not native:
                rectangles = []
                for widget in self._exclusions():
                    if widget.winfo_exists() and widget.winfo_ismapped():
                        relative = self._relative_rect(widget.winfo_id())
                        if relative is not None:
                            x, y, cw, ch = relative
                            rectangles.append((x, y, x+cw, y+ch))
                self._cached_exclusions = tuple(rectangles)
            if not success or has_blank_blocks(candidate.pixels(), self._cached_exclusions):
                self.rejected += 1
                self._failed_captures += 1
                self._retry_after = time.monotonic()+min(1., .06*2**min(self._failed_captures, 4))
                return False
            self._drop_frame()
            self.frame, candidate = candidate, None
            self._layout = {hwnd: self._relative_rect(hwnd) for hwnd in self._hooks}
            self.captures += 1
            self._failed_captures = 0
            self._retry_after = 0.
            self._dirty = False
            return True
        finally:
            self._capturing = False
            self.last_capture_ms = (time.perf_counter()-began)*1000
            if dc:
                self.user.ReleaseDC(self.hwnd, dc)
            if candidate is not None:
                candidate.close()

    def _blit(self, hwnd, dc=None):
        frame = self.frame
        if frame is None or not self.hwnd:
            return False
        root_rect, rect = w.RECT(), w.RECT()
        origin, root_origin = w.POINT(), w.POINT()
        if not (self.user.GetClientRect(self.hwnd, c.byref(root_rect))
                and (root_rect.right, root_rect.bottom) == (frame.width, frame.height)
                and self.user.GetClientRect(hwnd, c.byref(rect))
                and self.user.ClientToScreen(hwnd, c.byref(origin))
                and self.user.ClientToScreen(self.hwnd, c.byref(root_origin))):
            return False
        x, y = origin.x-root_origin.x, origin.y-root_origin.y
        if self._layout.get(hwnd) != (x, y, rect.right, rect.bottom):
            return False
        if x < 0 or y < 0 or x+rect.right > frame.width or y+rect.bottom > frame.height:
            return False
        owned_dc = None if dc else self.user.GetDC(hwnd)
        dc = dc or owned_dc
        try:
            if dc and self.gdi.BitBlt(dc, 0, 0, rect.right, rect.bottom, frame.dc, x, y, 0x00CC0020):
                self.replays += 1
                return True
            return False
        finally:
            if owned_dc:
                self.user.ReleaseDC(hwnd, owned_dc)

    def _wndproc(self, hwnd, message, wp, lp):
        result = None
        try:
            if message == 0x82:  # NCDESTROY; no Tcl calls from native callbacks.
                self.comctl.RemoveWindowSubclass(hwnd, self._callback, id(self))
                self._hooks.pop(hwnd, None)
                self._widgets.pop(hwnd, None)
                self._layout.pop(hwnd, None)
                _LIVE.pop((hwnd, id(self)), None)
                if hwnd == self.hwnd:
                    self.hwnd = None
                    self._drop_frame()
            elif not self._closed:
                if hwnd == self.hwnd and message == 0x112 and wp & 0xFFF0 == 0xF020:
                    # Refresh from the current painted pixels before minimizing.
                    # This path is entirely native and never enters Tcl.
                    self.capture(native=True)
                if hwnd == self.hwnd and message == 0x47:  # WINDOWPOSCHANGED
                    iconic = bool(self.user.IsIconic(hwnd))
                    if self._minimized and not iconic:
                        self._replaying = True
                        self._blit(hwnd)
                    self._minimized = iconic
                if message in (0x201, 0x100, 0x20A, 0x231):  # Edits or native resize.
                    self._replaying = False
                if not self._capturing:
                    if message in (0xF, 0x14, 0x18):
                        self._dirty = True
                        self._last_dirty = time.monotonic()
                    if self._replaying and message == 0x14 and wp:  # ERASEBKGND
                        if self._replaying and self._blit(hwnd, wp):
                            return 1
                        rect = w.RECT()
                        if self.user.GetClientRect(hwnd, c.byref(rect)):
                            self.user.FillRect(wp, c.byref(rect), self._hooks.get(hwnd))
                            return 1
                    if self._replaying and (message == 0xF or (message == 0x18 and wp)):
                        self._blit(hwnd)
            result = self.comctl.DefSubclassProc(hwnd, message, wp, lp)
            return result
        except Exception as error:
            self.error = str(error)
            return result if result is not None else self.comctl.DefSubclassProc(hwnd, message, wp, lp)

    def _drop_frame(self):
        if self.frame is not None:
            self.frame.close()
            self.frame = None
        self._layout.clear()

    def _destroyed(self, event):
        if event.widget is self._window():
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        window = self._window()
        if window is not None:
            if self._after is not None:
                try:
                    window.after_cancel(self._after)
                except tk.TclError:
                    pass
            for sequence, ident in self._bindings:
                try:
                    window.unbind(sequence, ident)
                except tk.TclError:
                    pass
        self._after = None
        self._bindings.clear()
        for hwnd in list(self._hooks):
            if self.comctl.RemoveWindowSubclass(hwnd, self._callback, id(self)):
                _LIVE.pop((hwnd, id(self)), None)
        self._hooks.clear()
        self._widgets.clear()
        self._drop_frame()
        for brush in self._brushes.values():
            self.gdi.DeleteObject(brush)
        self._brushes.clear()
