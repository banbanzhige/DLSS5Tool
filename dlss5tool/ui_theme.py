#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dark/light visual language for the DLSS5Tool studio UI."""

import ctypes
import os
import sys
import tkinter as tk
from tkinter import ttk, font as tkfont


THEMES = {
    "dark": {
        "name": "dark",
        "bg": "#0c1014",
        "surface": "#12181e",
        "panel": "#161d24",
        "elev": "#1c242d",
        "hover": "#24303a",
        "line": "#2a3540",
        "line2": "#3a4854",
        "text": "#e8eef3",
        "muted": "#8b97a3",
        "faint": "#62707c",
        "accent": "#7fe8e8",
        "accent_dim": "#4aa8b8",
        "fill": "#3d8eaa",
        "ok": "#7dcea0",
        "warn": "#e0b86a",
        "danger": "#d97878",
        "canvas": "#0a0d10",
        "canvas_drop": "#1a3348",
        "hud": "#d8d8d8",
        "overlay": "#e8eef3",
        "split": "#7fe8e8",
        "timeline_bg": "#10161c",
        "timeline_track": "#2a333b",
        "timeline_fill": "#3d7ea6",
        "timeline_thumb": "#e6e6e6",
        "timeline_rendered": "#78c7d5",
        "timeline_queued": "#557780",
        "transport_btn": "#1a232b",
        "transport_btn_active": "#24313a",
        "transport_btn_disabled": "#151c22",
        "transport_btn_border": "#2d3a44",
        "slider_trough_on": "#3d8eaa",
        "slider_trough_off": "#4a5560",
        "slider_knob": "#f2f7f9",
        "log_bg": "#0e1419",
        "log_fg": "#c5d0d8",
        "select_bg": "#2b3d48",
        "tree_bg": "#161d24",
        "tooltip_bg": "#1e2a33",
        "tooltip_fg": "#e8eef3",
        "primary_bg": "#3aa0b0",
        "primary_fg": "#071216",
        "primary_active": "#5ec9d4",
        "entry_bg": "#1a242c",
        "entry_fg": "#e8eef3",
        "failed": "#e08a86",
        "interrupted": "#e0b86a",
        "completed": "#7dcea0",
    },
    "light": {
        "name": "light",
        "bg": "#e7ecef",
        "surface": "#f3f6f8",
        "panel": "#f7f9fb",
        "elev": "#ffffff",
        "hover": "#e8eef2",
        "line": "#d5dee4",
        "line2": "#c3ced6",
        "text": "#1c252c",
        "muted": "#5b6a75",
        "faint": "#7b8a94",
        "accent": "#1b8f9a",
        "accent_dim": "#2f7d88",
        "fill": "#2d8494",
        "ok": "#2f8a58",
        "warn": "#b7812c",
        "danger": "#c05454",
        "canvas": "#dce4ea",
        "canvas_drop": "#cfe3e8",
        "hud": "#1c252c",
        "overlay": "#f4f8fb",
        "split": "#1b8f9a",
        "timeline_bg": "#eef2f5",
        "timeline_track": "#c5d0d7",
        "timeline_fill": "#2d8494",
        "timeline_thumb": "#1c252c",
        "timeline_rendered": "#3aa0b0",
        "timeline_queued": "#9bb7bf",
        "transport_btn": "#ffffff",
        "transport_btn_active": "#e8eef2",
        "transport_btn_disabled": "#e7ecef",
        "transport_btn_border": "#d5dee4",
        "slider_trough_on": "#2d8494",
        "slider_trough_off": "#d5dee4",
        "slider_knob": "#ffffff",
        "log_bg": "#f7f9fb",
        "log_fg": "#1c252c",
        "select_bg": "#d2eef1",
        "tree_bg": "#ffffff",
        "tooltip_bg": "#ffffff",
        "tooltip_fg": "#1c252c",
        "primary_bg": "#2aa0ad",
        "primary_fg": "#062024",
        "primary_active": "#3bb8c4",
        "entry_bg": "#ffffff",
        "entry_fg": "#1c252c",
        "failed": "#c05454",
        "interrupted": "#b7812c",
        "completed": "#2f8a58",
    },
}

# Named fonts keep ttk and Canvas text on the same measured pixel grid. They
# are configured per Tcl interpreter, including galleries and test windows.
UI_FONT = "StudioBody"
UI_FONT_BOLD = "StudioStrong"
UI_FONT_SMALL = "StudioCaption"
UI_FONT_TITLE = "StudioTitle"
UI_MONO = "StudioMono"
RADIUS_CONTROL = 6
RADIUS_CONTAINER = 10
HEIGHT_BUTTON = 32
HEIGHT_PRIMARY = 36
SPACE_INSET = 16
SLIDER_TRACK = 4
SLIDER_THUMB = 12
INSPECTOR_MIN = 320
INSPECTOR_MAX = 480
INSPECTOR_DEFAULT = 360

# Neutral surfaces, not a blue-grey wash. Accent belongs to actions/selection.
THEMES["light"].update({
    "bg": "#e9ecf0", "surface": "#f6f7f9", "panel": "#fafbfc",
    "canvas": "#f0f2f5", "elev": "#ffffff", "hover": "#eef2f5",
    "line": "#dfe4ea", "line2": "#cbd3dc", "text": "#202b3a",
    "hud": "#202b3a", "muted": "#566477", "faint": "#657387",
    "accent": "#147e87", "accent_dim": "#45969e", "fill": "#268e97",
    "primary_bg": "#147e87", "primary_active": "#2496a0", "primary_fg": "#ffffff",
    "select_bg": "#e4f2f2", "timeline_bg": "#fafbfc", "timeline_track": "#d4dce4",
    "slider_trough_on": "#268e97", "slider_trough_off": "#d4dce4",
    "entry_bg": "#ffffff", "entry_fg": "#202b3a", "canvas_drop": "#e2efef",
    "empty_card": "#fafbfc", "empty_shadow": "#e5e9ef", "primary_highlight": "#20929a",
    "tree_bg": "#fafbfc",
    "pill_bg": "#ffffff", "pill_ok_bg": "#e7f4ec", "pill_warn_bg": "#f6eedc",
})
THEMES["dark"].update({
    "bg": "#101318", "surface": "#171b22", "panel": "#1a2028",
    "canvas": "#11161d", "elev": "#232b35", "hover": "#2b3542",
    "line": "#303b48", "line2": "#404e5d", "text": "#e6edf5",
    "hud": "#e6edf5", "muted": "#a1adbc", "faint": "#909eaf",
    "accent": "#83d9db", "accent_dim": "#477f88", "fill": "#56aeb6",
    "primary_bg": "#77ced2", "primary_active": "#98e0e2", "primary_fg": "#102529",
    "select_bg": "#273f49", "timeline_bg": "#171d25",
    "slider_trough_on": "#65bbc1", "slider_trough_off": "#394553",
    "empty_card": "#191f28", "empty_shadow": "#0e1319", "primary_highlight": "#95dbde",
    "pill_bg": "#232b35", "pill_ok_bg": "#1c2c24", "pill_warn_bg": "#2c2618",
})


def enable_dpi_awareness():
    """Opt out of startup bitmap stretching before Tk creates a window.

    Tk 8.6 custom geometry is system-DPI based; do not claim per-monitor-v2
    support without also implementing live relayout on WM_DPICHANGED.
    A manifest/host may already set awareness; that is left untouched.
    """
    if sys.platform != "win32":
        return False
    try:
        set_context = ctypes.windll.user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_int
        return bool(set_context(ctypes.c_void_p(-2)))
    except (AttributeError, OSError):
        try:
            return ctypes.windll.shcore.SetProcessDpiAwareness(1) == 0
        except (AttributeError, OSError):
            return False


def configure_fonts(root):
    existing = getattr(root, "_studio_fonts", None)
    if existing:
        return existing
    families = set(tkfont.families(root))
    family = next((name for name in (
        "Noto Sans SC", "Source Han Sans SC", "Microsoft YaHei UI", "Segoe UI",
    ) if name in families), "TkDefaultFont")
    mono = next((name for name in ("Cascadia Mono", "Consolas") if name in families), family)
    factor = max(1.0, min(float(root.winfo_fpixels("1i")) / 96.0, 3.0))
    specs = {
        UI_FONT: (family, 13, "normal"), UI_FONT_BOLD: (family, 13, "bold"),
        UI_FONT_SMALL: (family, 12, "normal"), UI_FONT_TITLE: (family, 21, "bold"),
        UI_MONO: (mono, 12, "normal"),
    }
    fonts = {}
    names = set(tkfont.names(root))
    for name, (face, size, weight) in specs.items():
        font = tkfont.Font(root=root, name=name, exists=name in names)
        font.configure(family=face, size=-round(size * factor), weight=weight)
        fonts[name] = font
    root._studio_fonts = fonts  # retain ownership; Font.__del__ deletes names
    root._studio_scale = factor
    return fonts


def control_height(widget, primary=False):
    font = tkfont.Font(root=widget, font=UI_FONT)
    return max(HEIGHT_PRIMARY if primary else HEIGHT_BUTTON, font.metrics("linespace") + 12)


def studio_scale(widget):
    try:
        root_getter = getattr(widget, "_root", None)
        root = root_getter() if root_getter is not None else widget.winfo_toplevel()
        return max(1.0, float(getattr(root, "_studio_scale", 1.0) or 1.0))
    except (AttributeError, tk.TclError, TypeError, ValueError):
        return 1.0


def scale_px(widget, px, minimum=1):
    return max(int(minimum), int(round(float(px) * studio_scale(widget))))


def normalize_theme_name(value):
    name = str(value or "").strip().lower()
    return "light" if name == "light" else "dark"


def tokens(name="dark"):
    return dict(THEMES[normalize_theme_name(name)])


def slider_colors(ui, enabled):
    fill = ui["slider_trough_on"] if enabled else ui["slider_trough_off"]
    return {
        "troughcolor": fill,
        "trackcolor": ui["slider_trough_off"],
        "background": ui["panel"],
        "activebackground": ui["hover"],
        "highlightbackground": ui["line"],
    }


def _colorref(hex_color):
    value = str(hex_color or "").lstrip("#")
    if len(value) != 6:
        return 0
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    return red | (green << 8) | (blue << 16)


APP_USER_MODEL_ID = "DLSS5Tool.App"
_USER32 = None


def claim_app_identity(app_id=APP_USER_MODEL_ID):
    """Give the process its own taskbar identity instead of python.exe."""
    if sys.platform != "win32":
        return False
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id))
        return True
    except (AttributeError, OSError):
        return False


def _user32():
    global _USER32
    if _USER32 is None:
        dll = ctypes.windll.user32
        dll.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        dll.GetAncestor.restype = ctypes.c_void_p
        dll.LoadImageW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        dll.LoadImageW.restype = ctypes.c_void_p
        dll.SendMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
        ]
        dll.SendMessageW.restype = ctypes.c_void_p
        dll.DestroyIcon.argtypes = [ctypes.c_void_p]
        dll.DestroyIcon.restype = ctypes.c_int
        dll.GetSystemMetrics.restype = ctypes.c_int
        try:
            dll.GetDpiForWindow.argtypes = [ctypes.c_void_p]
            dll.GetDpiForWindow.restype = ctypes.c_uint
        except AttributeError:
            pass
        _USER32 = dll
    return _USER32


def _window_hwnd(window):
    """Return the Win32 wrapper HWND. winfo_id() is an inner Tk child."""
    try:
        hwnd = window.winfo_id()
    except tk.TclError:
        return 0
    try:
        return _user32().GetAncestor(hwnd, 2) or hwnd
    except (AttributeError, OSError):
        return hwnd


def _system_icon_size(hwnd, small):
    base = 16 if small else 32
    user32 = _user32()
    try:
        dpi = int(user32.GetDpiForWindow(hwnd)) if hwnd else 0
        if dpi > 0:
            return max(base, int(round(base * dpi / 96.0)))
    except (AttributeError, OSError):
        pass
    try:
        metric = 49 if small else 11
        return max(base, int(user32.GetSystemMetrics(metric)))
    except (AttributeError, OSError):
        return base


def _load_icon_file(path, size):
    return _user32().LoadImageW(
        None, os.path.abspath(path), 1, int(size), int(size), 0x0010,
    )


def _apply_win32_icons(window, ico_path):
    if sys.platform != "win32" or not os.path.isfile(ico_path):
        return False
    try:
        hwnd = _window_hwnd(window)
    except (AttributeError, OSError, tk.TclError):
        return False
    if not hwnd:
        return False
    user32 = _user32()
    try:
        owner = window._root()
    except (AttributeError, tk.TclError):
        owner = window
    if owner is not window:
        shared = list(getattr(owner, "_app_icon_handles", ()))
        if shared:
            small = shared[0]
            big = shared[-1]
            user32.SendMessageW(hwnd, 0x0080, 0, small)
            user32.SendMessageW(hwnd, 0x0080, 1, big)
            window._app_icon_handles = []
            window._app_icon_borrowed_handles = shared
            _bind_app_icon_cleanup(window)
            return True
    small_size = _system_icon_size(hwnd, True)
    big_size = _system_icon_size(hwnd, False)
    # Taskbar paints ICON_BIG smaller than SM_CXICON. Feed a larger tight
    # frame so the shell downscale stays sharp and the face fills the plate.
    if big_size <= 32:
        load_big = 48
    elif big_size <= 48:
        load_big = 64
    else:
        load_big = big_size
    small = _load_icon_file(ico_path, small_size)
    big = _load_icon_file(ico_path, load_big)
    if not small and small_size != 16:
        small = _load_icon_file(ico_path, 16)
    if not big:
        big = _load_icon_file(ico_path, big_size) or _load_icon_file(ico_path, 32)
    if not small and not big:
        return False
    if small:
        user32.SendMessageW(hwnd, 0x0080, 0, small)
    if big:
        user32.SendMessageW(hwnd, 0x0080, 1, big)
    previous = getattr(window, "_app_icon_handles", [])
    window._app_icon_handles = [handle for handle in (small, big) if handle]
    for handle in previous:
        if handle and handle not in window._app_icon_handles:
            try:
                user32.DestroyIcon(handle)
            except (AttributeError, OSError):
                pass
    _bind_app_icon_cleanup(window)
    return True


def release_app_icon(window):
    """Release Win32 icon handles owned by a window before it is destroyed."""
    handles = list(getattr(window, "_app_icon_handles", ()))
    borrowed = list(getattr(window, "_app_icon_borrowed_handles", ()))
    window._app_icon_handles = []
    window._app_icon_borrowed_handles = []
    if sys.platform != "win32" or not (handles or borrowed):
        return
    try:
        user32 = _user32()
        hwnd = _window_hwnd(window)
        if hwnd:
            user32.SendMessageW(hwnd, 0x0080, 0, 0)
            user32.SendMessageW(hwnd, 0x0080, 1, 0)
        for handle in handles:
            if handle:
                user32.DestroyIcon(handle)
    except (AttributeError, OSError, tk.TclError):
        pass


def _bind_app_icon_cleanup(window):
    if getattr(window, "_app_icon_cleanup_bound", False):
        return

    def cleanup(event):
        if getattr(event, "widget", None) is window:
            release_app_icon(window)

    try:
        window.bind("<Destroy>", cleanup, add="+")
        window._app_icon_cleanup_bound = True
    except (AttributeError, tk.TclError):
        pass


def apply_native_titlebar(window, ui, dark):
    """Keep the native Windows frame; only tint caption/chrome."""
    if sys.platform != "win32":
        return
    try:
        hwnd = _window_hwnd(window)
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        set_attribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        set_attribute.restype = ctypes.c_long
    except (AttributeError, OSError, tk.TclError):
        return
    if not hwnd:
        return
    value = ctypes.c_int(1 if dark else 0)
    for attribute in (20, 19):
        try:
            set_attribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value),
            )
        except Exception:
            pass
    try:
        caption = ctypes.c_int(_colorref(ui.get("bg", "#0c1014")))
        set_attribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
        border = ctypes.c_int(_colorref(ui.get("line", "#2a3540")))
        set_attribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
        text = ctypes.c_int(_colorref(ui["text"]))
        set_attribute(hwnd, 36, ctypes.byref(text), ctypes.sizeof(text))
    except Exception:
        pass
    ico_path, _png_path = app_icon_paths()
    _apply_win32_icons(window, ico_path)


def bundle_dir():
    from dlss5tool.paths import resource_root
    return str(resource_root())


def app_icon_paths():
    root = bundle_dir()
    return (
        os.path.join(root, "assets", "app.ico"),
        os.path.join(root, "assets", "app.png"),
    )


def apply_app_icon(window, default=False):
    """Attach the split-comparison Shiba mark. Missing files are ignored."""
    ico_path, png_path = app_icon_paths()
    applied = False
    # Win32 receives exact system-sized icons through WM_SETICON below. Calling
    # Tk's iconbitmap as well duplicates HICON ownership for each Toplevel and
    # leaves GDI objects behind after repeated detach/dock cycles.
    if os.path.isfile(ico_path) and sys.platform != "win32":
        try:
            window.iconbitmap(ico_path)
            applied = True
        except tk.TclError:
            pass
        if default:
            try:
                window.iconbitmap(default=ico_path)
                applied = True
            except tk.TclError:
                pass
    # A 256px PhotoImage is what Tk downscales into the 16px caption slot.
    # On Windows the ICO + WM_SETICON path stays sharp; keep PNG for elsewhere.
    use_photo = os.path.isfile(png_path) and (
        sys.platform != "win32" or not os.path.isfile(ico_path)
    )
    if use_photo:
        try:
            photo = tk.PhotoImage(master=window, file=png_path)
            window.iconphoto(bool(default), photo)
            photos = getattr(window, "_app_icon_photos", [])
            photos.append(photo)
            window._app_icon_photos = photos
            applied = True
        except tk.TclError:
            pass
    if sys.platform == "win32" and os.path.isfile(ico_path):
        def apply_native(_event=None):
            return _apply_win32_icons(window, ico_path)

        try:
            window.update_idletasks()
        except tk.TclError:
            pass
        if apply_native():
            applied = True
        else:
            try:
                window.after_idle(apply_native)
            except tk.TclError:
                pass
    return applied


_COMBOBOX_WHEEL_SEQUENCES = (
    "<MouseWheel>",
    "<Button-4>",
    "<Button-5>",
    "<Option-MouseWheel>",
    "<Shift-MouseWheel>",
    "<Control-MouseWheel>",
)


def _clear_readonly_combobox_selection(event):
    """Drop leftover entry selection so a closed combo does not stay inverted."""
    widget = getattr(event, "widget", None)
    if widget is None or isinstance(widget, str):
        return

    def clear():
        try:
            if str(widget.cget("state")) != "readonly":
                return
            widget.selection_clear()
        except (tk.TclError, AttributeError):
            pass

    try:
        widget.after_idle(clear)
    except (tk.TclError, AttributeError):
        clear()


def install_combobox_behavior(root):
    """Readonly combos commit on blur and ignore wheel value changes.

    ttk.Combobox otherwise keeps `selection range 0 end` after a pick (gray
    inverted text when unfocused) and cycles values on MouseWheel, which
    collides with inspector scrolling.
    """
    for sequence in _COMBOBOX_WHEEL_SEQUENCES:
        try:
            root.unbind_class("TCombobox", sequence)
        except tk.TclError:
            pass
    for sequence in ("<<ComboboxSelected>>", "<FocusOut>", "<FocusIn>"):
        try:
            root.bind_class("TCombobox", sequence, _clear_readonly_combobox_selection)
        except tk.TclError:
            pass


def install_spinbox_behavior(root):
    """Reserve the wheel for page scrolling, even when a number has focus.

    Remove only the native class wheel actions: returning 'break' on the
    widget would also swallow the inspector's bind_all scrolling handler.
    Click/keyboard increment actions and text editing remain native.
    """
    for sequence in _COMBOBOX_WHEEL_SEQUENCES:
        try:
            root.unbind_class("TSpinbox", sequence)
        except tk.TclError:
            pass


def apply_ttk(root, ui):
    """Paint clam-based ttk widgets with the active palette."""
    configure_fonts(root)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=ui["bg"])
    try:
        root.option_add("*Font", UI_FONT)
        root.option_add("*TCombobox*Listbox.font", UI_FONT)
        root.option_add("*TCombobox*Listbox.background", ui["elev"])
        root.option_add("*TCombobox*Listbox.foreground", ui["text"])
        root.option_add("*TCombobox*Listbox.selectBackground", ui["select_bg"])
        root.option_add("*TCombobox*Listbox.selectForeground", ui["text"])
    except tk.TclError:
        pass

    style.configure(
        ".",
        background=ui["bg"],
        foreground=ui["text"],
        bordercolor=ui["line"],
        darkcolor=ui["line"],
        lightcolor=ui["line"],
        troughcolor=ui["elev"],
        focuscolor=ui["accent_dim"],
        font=UI_FONT,
    )
    for name in ("TFrame", "TPanedwindow"):
        style.configure(name, background=ui["panel"])
    style.configure("Workspace.TFrame", background=ui["bg"])
    style.configure("Panel.TFrame", background=ui["panel"])
    style.configure("Stage.TFrame", background=ui["canvas"])
    style.configure("Status.TFrame", background=ui["surface"])
    style.configure("TLabel", background=ui["panel"], foreground=ui["text"])
    style.configure("Panel.TLabel", background=ui["panel"], foreground=ui["text"])
    style.configure("Status.TLabel", background=ui["surface"], foreground=ui["muted"])
    style.configure(
        "Kicker.TLabel",
        background=ui["panel"],
        foreground=ui["faint"],
        font=UI_FONT_SMALL,
    )
    style.configure(
        "Hint.TLabel",
        background=ui["panel"],
        foreground=ui["faint"],
        font=UI_FONT_SMALL,
    )
    style.configure(
        "TButton",
        background=ui["elev"],
        foreground=ui["text"],
        bordercolor=ui["line"],
        darkcolor=ui["elev"],
        lightcolor=ui["elev"],
        padding=(10, 6),
        borderwidth=1,
        relief="flat",
    )
    style.map(
        "TButton",
        background=[("active", ui["hover"]), ("disabled", ui["surface"])],
        foreground=[("disabled", ui["faint"])],
        bordercolor=[("disabled", ui["line"])],
    )
    style.configure(
        "Accent.TButton",
        background=ui["primary_bg"],
        foreground=ui["primary_fg"],
        bordercolor=ui["accent_dim"],
        darkcolor=ui["primary_bg"],
        lightcolor=ui["primary_bg"],
        padding=(10, 6),
        font=UI_FONT_BOLD,
    )
    style.map(
        "Accent.TButton",
        background=[
            ("active", ui["primary_active"]),
            ("disabled", ui["fill"]),
        ],
        foreground=[("disabled", ui["panel"])],
    )
    style.configure(
        "Ghost.TButton",
        background=ui["panel"],
        foreground=ui["text"],
        bordercolor=ui["line"],
        padding=(9, 4),
    )
    style.map(
        "Ghost.TButton",
        background=[("active", ui["hover"])],
        foreground=[("active", ui["text"]), ("disabled", ui["faint"])],
    )
    style.configure(
        "Chip.TButton",
        background=ui["elev"],
        foreground=ui["muted"],
        bordercolor=ui["line"],
        padding=(9, 3),
        relief="flat",
    )
    style.map("Chip.TButton", background=[("active", ui["hover"])])
    style.configure(
        "ChipOn.TButton",
        background=ui["select_bg"],
        foreground=ui["accent"],
        bordercolor=ui["accent_dim"],
        padding=(9, 3),
        relief="flat",
    )
    style.configure(
        "Segment.TButton",
        background=ui["timeline_bg"],
        foreground=ui["muted"],
        bordercolor=ui["line2"],
        padding=(8, 3),
        relief="flat",
    )
    style.configure(
        "SegmentOn.TButton",
        background=ui["select_bg"],
        foreground=ui["accent"],
        bordercolor=ui["accent_dim"],
        padding=(8, 3),
        relief="flat",
    )
    style.configure(
        "Toolbutton",
        background=ui["panel"],
        foreground=ui["text"],
        padding=(8, 4),
        relief="flat",
        borderwidth=0,
    )
    style.map("Toolbutton", background=[("active", ui["hover"])])
    style.configure(
        "TCheckbutton",
        background=ui["panel"],
        foreground=ui["text"],
        indicatorcolor=ui["elev"],
        indicatorrelief="flat",
        padding=2,
    )
    style.map(
        "TCheckbutton",
        background=[("active", ui["panel"])],
        indicatorcolor=[
            ("selected", ui["fill"]),
            ("!selected", ui["elev"]),
            ("disabled", ui["surface"]),
        ],
        foreground=[("disabled", ui["faint"])],
    )
    style.configure(
        "TRadiobutton",
        background=ui["timeline_bg"],
        foreground=ui["text"],
        padding=2,
    )
    style.configure(
        "TLabelframe",
        background=ui["panel"],
        foreground=ui["muted"],
        bordercolor=ui["panel"],
        relief="flat",
        padding=4,
    )
    style.configure(
        "TLabelframe.Label",
        background=ui["panel"],
        foreground=ui["muted"],
    )
    style.configure(
        "TNotebook",
        background=ui["panel"],
        borderwidth=0,
        tabmargins=(6, 6, 6, 0),
    )
    style.configure(
        "TNotebook.Tab",
        background=ui["elev"],
        foreground=ui["muted"],
        padding=(12, 6),
        borderwidth=0,
        lightcolor=ui["panel"],
        darkcolor=ui["panel"],
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", ui["panel"]), ("active", ui["hover"])],
        foreground=[("selected", ui["text"])],
        lightcolor=[("selected", ui["panel"])],
    )
    field = dict(
        fieldbackground=ui["entry_bg"],
        background=ui["entry_bg"],
        foreground=ui["entry_fg"],
        insertcolor=ui["text"],
        bordercolor=ui["line"],
        lightcolor=ui["line"],
        darkcolor=ui["line"],
        arrowcolor=ui["muted"],
        padding=5,
        borderwidth=1,
        relief="flat",
    )
    style.configure("TEntry", **field)
    style.configure("TSpinbox", **field)
    style.configure("SliderValue.TSpinbox", padding=(0, 0), borderwidth=0,
                    relief="flat", bordercolor=ui["entry_bg"],
                    lightcolor=ui["entry_bg"], darkcolor=ui["entry_bg"])
    style.map(
        "SliderValue.TSpinbox",
        foreground=[("disabled", ui["faint"]), ("!disabled", ui["text"])],
        fieldbackground=[("disabled", ui["surface"])],
    )
    chrome_field = dict(field)
    chrome_field.update(padding=(0, 0), borderwidth=0, relief="flat",
                        bordercolor=ui["entry_bg"], lightcolor=ui["entry_bg"],
                        darkcolor=ui["entry_bg"])
    style.configure("Chrome.TEntry", **chrome_field)
    combo_field = dict(chrome_field)
    combo_field.update(
        focuscolor=ui["entry_bg"],
        selectbackground=ui["entry_bg"],
        selectforeground=ui["entry_fg"],
    )
    style.configure("Chrome.TCombobox", **combo_field)
    combo_map = dict(
        fieldbackground=[("readonly", ui["entry_bg"]), ("disabled", ui["surface"])],
        foreground=[("disabled", ui["faint"])],
        background=[("readonly", ui["entry_bg"])],
        arrowcolor=[("disabled", ui["faint"])],
        selectbackground=[
            ("readonly", ui["entry_bg"]),
            ("!focus", ui["entry_bg"]),
            ("disabled", ui["surface"]),
        ],
        selectforeground=[
            ("readonly", ui["entry_fg"]),
            ("!focus", ui["entry_fg"]),
            ("disabled", ui["faint"]),
        ],
    )
    style.map("Chrome.TCombobox", **combo_map)
    style.map(
        "Chrome.TEntry",
        fieldbackground=[("disabled", ui["surface"])],
        foreground=[("disabled", ui["faint"])],
    )
    field.update(
        focuscolor=ui["entry_bg"],
        selectbackground=ui["entry_bg"],
        selectforeground=ui["entry_fg"],
    )
    style.configure("TCombobox", **field)
    style.map("TCombobox", **combo_map)
    style.configure(
        "Horizontal.TProgressbar",
        background=ui["fill"],
        troughcolor=ui["elev"],
        bordercolor=ui["line"],
        lightcolor=ui["fill"],
        darkcolor=ui["fill"],
    )
    style.configure(
        "TScrollbar",
        background=ui["elev"],
        troughcolor=ui["surface"],
        bordercolor=ui["line"],
        arrowcolor=ui["muted"],
        relief="flat",
    )
    style.map("TScrollbar", background=[("active", ui["hover"])])
    style.configure(
        "Treeview",
        background=ui["tree_bg"],
        fieldbackground=ui["tree_bg"],
        foreground=ui["text"],
        bordercolor=ui["line"],
        rowheight=control_height(root) + 4,
        borderwidth=0,
    )
    style.configure(
        "Treeview.Heading",
        background=ui["elev"],
        foreground=ui["muted"],
        bordercolor=ui["line"],
        relief="flat",
        padding=(8, 8),
        font=UI_FONT_SMALL,
    )
    style.map(
        "Treeview",
        background=[("selected", ui["select_bg"])],
        foreground=[("selected", ui["text"])],
    )
    style.map("Treeview.Heading", background=[("active", ui["hover"])])
    style.configure("TSeparator", background=ui["line"])
    style.configure(
        "Transport.TFrame",
        background=ui["timeline_bg"],
    )
    style.configure(
        "Transport.TLabel",
        background=ui["timeline_bg"],
        foreground=ui["text"],
    )
    style.configure(
        "Transport.TButton",
        background=ui["transport_btn"],
        foreground=ui["hud"],
        bordercolor=ui["transport_btn_border"],
        darkcolor=ui["transport_btn"],
        lightcolor=ui["transport_btn"],
        padding=(8, 3),
    )
    style.map(
        "Transport.TButton",
        background=[
            ("active", ui["transport_btn_active"]),
            ("disabled", ui["transport_btn_disabled"]),
        ],
        foreground=[("disabled", ui["faint"])],
    )
    install_combobox_behavior(root)
    install_spinbox_behavior(root)
    return style


def install_ttk_scrolledtext_scrollbar(scrolled_text):
    """Replace ScrolledText's classic scrollbar with the themed ttk variant."""
    current = getattr(scrolled_text, "vbar", None)
    if isinstance(current, ttk.Scrollbar):
        return current

    frame = scrolled_text.frame
    if current is not None:
        current.pack_forget()
        current.destroy()

    scrollbar = ttk.Scrollbar(
        frame, orient="vertical", command=scrolled_text.yview,
    )
    scrollbar.pack(side="right", fill="y", before=scrolled_text._w)
    scrolled_text.configure(yscrollcommand=scrollbar.set)
    scrolled_text.vbar = scrollbar
    return scrollbar
