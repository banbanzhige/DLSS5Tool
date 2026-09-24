"""About window: version, repository, author links and support actions."""
import queue
import threading
import tkinter as tk
from tkinter import ttk
import webbrowser

from dlss5tool import ui_theme, updater
from dlss5tool.app_version import APP_VERSION
from dlss5tool.i18n import tr
from dlss5tool.image_sequence_dialog import _dialog_chrome, _place_over_parent
from dlss5tool.ui_theme import UI_FONT_BOLD
from dlss5tool.ui_widgets import ChromeButton


REPO_URL = f"https://github.com/{updater.GITHUB_REPOSITORY}"
AUTHOR_URL = "https://space.bilibili.com/3584419"
REPO_LABEL = "github.com/banbanzhige/DLSS5Tool"


def show_about(parent, on_check_updates, on_export_diagnostics, current_version=APP_VERSION):
    """Open one about window. A second click brings the existing one forward."""
    existing = getattr(parent, "_about_window", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.lift()
                existing.focus_set()
                return existing
        except tk.TclError:
            pass
    ui, dark = _dialog_chrome(parent)
    panel = ui["panel"]
    window = tk.Toplevel(parent)
    parent._about_window = window
    window.withdraw()
    window.title(tr("about.title"))
    window.configure(bg=panel)
    window.transient(parent)
    window.resizable(False, False)
    ui_theme.apply_app_icon(window)
    body = ttk.Frame(window, padding=20)
    body.pack(fill="both", expand=True)

    facts = ttk.Frame(body)
    facts.pack(anchor="w", fill="x")
    ttk.Label(facts, text=tr("about.current")).grid(row=0, column=0, sticky="w", padx=(0, 16), pady=2)
    ttk.Label(facts, text=current_version).grid(row=0, column=1, sticky="w", pady=2)
    ttk.Label(facts, text=tr("about.latest")).grid(row=1, column=0, sticky="w", padx=(0, 16), pady=2)
    latest = tk.StringVar(value=tr("about.latest_loading"))
    ttk.Label(facts, textvariable=latest).grid(row=1, column=1, sticky="w", pady=2)

    ttk.Label(body, text=tr("about.note"), wraplength=420, justify="left").pack(anchor="w", pady=(16, 0))

    links = ttk.Frame(body)
    links.pack(anchor="w", fill="x", pady=(14, 0))
    _link_row(links, 0, tr("about.repo"), REPO_LABEL, REPO_URL, ui, panel)
    _link_row(links, 1, tr("about.author"), tr("about.author_name"), AUTHOR_URL, ui, panel)

    ttk.Separator(body, orient="horizontal").pack(fill="x", pady=(18, 0))
    actions = ttk.Frame(body)
    actions.pack(fill="x", pady=(12, 0))
    progress_area = ttk.Frame(body)
    progress_label = ttk.Label(progress_area)
    progress_label.pack(anchor="w")
    progress = ttk.Progressbar(progress_area, mode="determinate", maximum=6)
    progress.pack(fill="x", pady=(6, 0))

    def close():
        window.destroy()

    def check():
        on_check_updates()

    close_button = ChromeButton(actions, text=tr("about.close"), command=close, ui=ui, variant="ghost")
    close_button.pack(side="right")
    ChromeButton(
        actions, text=tr("action.check_updates"), command=check, ui=ui, variant="outline",
    ).pack(side="right", padx=(0, 8))
    diagnostic_button = ChromeButton(
        actions, text=tr("about.export_diagnostics"), command=on_export_diagnostics,
        ui=ui, variant="outline",
    )
    diagnostic_button.pack(side="right", padx=(0, 8))
    window._diagnostics_progress_area = progress_area
    window._diagnostics_progress_label = progress_label
    window._diagnostics_progress = progress
    window._diagnostics_button = diagnostic_button
    initial_progress = getattr(parent, "_diagnostic_progress", None) or (0, 6, "collecting")
    set_diagnostics_progress(
        parent, bool(getattr(parent, "_diagnosing", False)), *initial_progress,
    )

    window.protocol("WM_DELETE_WINDOW", close)
    window.bind("<Escape>", lambda _event: close())
    window.update_idletasks()
    ui_theme.apply_native_titlebar(window, ui, dark)
    _place_over_parent(window, parent)
    window.deiconify()
    window.focus_set()
    _load_latest(window, latest, current_version)
    return window


def set_diagnostics_progress(parent, running, completed=0, total=6, stage="collecting"):
    """Reflect the current diagnostic job in an open About window."""
    window = getattr(parent, "_about_window", None)
    try:
        if window is None or not window.winfo_exists():
            return
    except tk.TclError:
        return
    area = getattr(window, "_diagnostics_progress_area", None)
    if area is None:
        return
    progress = window._diagnostics_progress
    button = window._diagnostics_button
    if running:
        window._diagnostics_progress_label.configure(text=tr(
            "about.diagnostics_progress",
            completed=completed, total=total,
            stage=tr(f"about.diagnostics_stage_{stage}"),
        ))
        progress.configure(maximum=total, value=completed)
        if not area.winfo_manager():
            area.pack(fill="x", pady=(12, 0), before=button.master)
        button.configure(state="disabled")
    else:
        area.pack_forget()
        button.configure(state="normal")


def _link_row(parent, row, caption, text, url, ui, panel):
    ttk.Label(parent, text=caption).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=3)
    link = tk.Label(
        parent, text=text, fg=ui.get("accent", "#147e87"), bg=panel,
        cursor="hand2", font=UI_FONT_BOLD, bd=0, highlightthickness=0,
    )
    link.grid(row=row, column=1, sticky="w", pady=3)
    link.bind("<Button-1>", lambda _event: _open(url))


def _open(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _load_latest(window, latest, current_version):
    result = queue.Queue()

    def worker():
        try:
            result.put(updater.fetch_latest_release())
        except Exception:
            result.put(None)

    def poll():
        try:
            if not window.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            release = result.get_nowait()
        except queue.Empty:
            window.after(50, poll)
            return
        if release is None:
            latest.set(tr("about.latest_failed"))
            return
        comparison = updater.compare_versions(release.tag, current_version)
        if comparison is None:
            text = tr("about.latest_raw", version=release.tag)
        elif comparison > 0:
            text = tr("about.latest_newer", version=release.tag)
        else:
            text = tr("about.latest_current", version=release.tag)
        latest.set(text)

    threading.Thread(target=worker, name="dlss5-about-version", daemon=True).start()
    window.after(50, poll)
