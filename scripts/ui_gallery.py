#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Component gallery for v2.0.0 chrome states. Run: python scripts/ui_gallery.py"""

import os
import sys
import tkinter as tk
from tkinter import ttk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ui_theme
from ui_widgets import (
    AccentSlider, CheckToggle, ChipGroup, ChromeButton, ChromeCombobox,
    ChromeSpinbox,
)


def section(parent, title, ui):
    frame = ttk.Frame(parent, style="Panel.TFrame", padding=16)
    frame.pack(fill="x", pady=(0, 12))
    ttk.Label(frame, text=title, style="Kicker.TLabel").pack(anchor="w", pady=(0, 8))
    return frame


def main():
    ui_theme.claim_app_identity()
    ui_theme.enable_dpi_awareness()
    root = tk.Tk()
    root.title("DLSS5Tool chrome gallery")
    ui_theme.apply_app_icon(root, default=True)
    root.geometry("720x820")
    theme_name = tk.StringVar(value="dark")
    ui = ui_theme.tokens("dark")
    ui_theme.apply_ttk(root, ui)
    widgets = []

    def refresh(_event=None):
        nonlocal ui
        ui = ui_theme.tokens(theme_name.get())
        ui_theme.apply_ttk(root, ui)
        ui_theme.apply_native_titlebar(root, ui, theme_name.get() == "dark")
        for widget in widgets:
            apply = getattr(widget, "apply_theme", None)
            if apply:
                apply(ui)

    ttk.Radiobutton(
        root, text="暗色", value="dark", variable=theme_name, command=refresh,
    ).pack(anchor="w", padx=16, pady=(16, 0))
    ttk.Radiobutton(
        root, text="浅色", value="light", variable=theme_name, command=refresh,
    ).pack(anchor="w", padx=16, pady=(0, 8))

    body = ttk.Frame(root, style="Panel.TFrame")
    body.pack(fill="both", expand=True, padx=16, pady=8)

    row = section(body, "按钮 · 默认 / 悬停 / 按下 / 禁用 / 焦点", ui)
    for variant, label in (
        ("default", "默认"), ("ghost", "幽灵"), ("accent", "主要"), ("tool", "工具"),
    ):
        btn = ChromeButton(row, text=label, variant=variant, ui=ui, width=96)
        btn.pack(side="left", padx=(0, 8))
        widgets.append(btn)
    disabled = ChromeButton(row, text="禁用", variant="default", ui=ui, width=96)
    disabled.state(["disabled"])
    disabled.pack(side="left")
    widgets.append(disabled)

    row = section(body, "图标按钮 · Lucide", ui)
    for name in (
        "play", "pause", "volume", "fit", "detach", "fullscreen", "file-plus",
        "more", "retry", "clear-done", "up", "down", "cancel",
    ):
        btn = ChromeButton(row, text=name, icon=name, icon_only=True, variant="tool", ui=ui, width=36)
        btn.pack(side="left", padx=(0, 6))
        widgets.append(btn)

    row = section(body, "芯片与勾选", ui)
    style = tk.StringVar(value="默认")
    chips = ChipGroup(row, style, ["默认", "自然", "电影"], ui=ui)
    chips.pack(anchor="w")
    widgets.append(chips)
    flag = tk.BooleanVar(value=True)
    check = CheckToggle(row, "强度", flag, ui=ui)
    check.pack(anchor="w", pady=(8, 0))
    widgets.append(check)

    row = section(body, "滑条 · 未选区暗灰，已选区青色", ui)
    value = tk.DoubleVar(value=0.62)
    slider = AccentSlider(
        row, from_=0, to=1, resolution=0.01, variable=value,
        **ui_theme.slider_colors(ui, True),
    )
    slider.pack(fill="x")
    widgets.append(slider)

    row = section(body, "数值输入", ui)
    number = tk.StringVar(value="0.84")
    spin = ChromeSpinbox(row, ui=ui, from_=0, to=1, increment=0.01, textvariable=number)
    spin.pack(anchor="w")
    widgets.append(spin)
    choice = tk.StringVar(value="MP4（推荐）")
    combo = ChromeCombobox(
        row, ui=ui, textvariable=choice, values=["MP4（推荐）", "MKV", "MOV"],
        state="readonly",
    )
    combo.pack(anchor="w", fill="x", pady=(8, 0))
    widgets.append(combo)

    ttk.Label(
        body,
        text="Tab 移动焦点 · Space 激活 · 方向键调整滑条",
        style="Hint.TLabel",
    ).pack(anchor="w", pady=8)
    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
