"""Small offline handoff UI for the AMD developer probe; separate from gui.App."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser

import amd_devtest as core


class DevTestWindow:
    def __init__(self, root, folder=None):
        self.root = root
        self.folder = Path(folder or core.package_root()).resolve()
        self.runtime = self.folder/"amd_backend"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.installer = None
        self.feedback = None
        self.last_run = None
        self.current_runner = None
        self.adapters = []
        self.photo = None
        self.status = tk.StringVar(value="尚未测试。不会自动加载 AMD 模组。")
        self.components = tk.StringVar()
        self.gpu = tk.StringVar(value="自动选择 AMD 独显")
        self.custom = tk.StringVar(value="使用内置测试图（推荐第一轮）")
        self.shared = tk.BooleanVar(value=True)
        self.high = tk.BooleanVar(value=False)
        self.share_image = tk.BooleanVar(value=False)
        self.license_consent = tk.BooleanVar(value=False)
        scale = max(1.0, float(root.tk.call("tk", "scaling")) / (96/72))
        window_w = min(round(900*scale), root.winfo_screenwidth()-60)
        window_h = min(round(730*scale), root.winfo_screenheight()-100)
        self.wrap = window_w-90
        self.root.title("DLSS5Tool · AMD 开发验证 " + core.VERSION)
        self.root.geometry(f"{window_w}x{window_h}")
        self.root.minsize(min(window_w, round(760*scale)), min(window_h, round(640*scale)))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.report_callback_exception = self.callback_error
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6f8")
        style.configure("TLabel", background="#f3f6f8", foreground="#1c252c", font=("Microsoft YaHei UI", 10))
        style.configure("TLabelframe", background="#f3f6f8")
        style.configure("TLabelframe.Label", background="#f3f6f8", foreground="#1c252c", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TCheckbutton", background="#f3f6f8", foreground="#1c252c", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(9, 7))
        style.configure("Primary.TButton", background="#087d87", foreground="white")
        style.map("Primary.TButton", background=[("active", "#06646d"), ("disabled", "#dce4e8")], foreground=[("disabled", "#596770")])
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Hint.TLabel", foreground="#536575")
        outer = ttk.Frame(root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="AMD 开发验证", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="独立测试包 · 不修改正式版 · 暂不提供视频导出或正式 AMD 开关", style="Hint.TLabel").pack(anchor="w", pady=(4, 12))

        setup = ttk.LabelFrame(outer, text="1  准备组件", padding=12)
        setup.pack(fill="x")
        ttk.Label(setup, text="把官方 dlssnr_on_amd_setup.exe 和你有权使用的 nvngx_dlssnr.dll 放入 amd_backend。",
                  wraplength=self.wrap).pack(anchor="w")
        row = ttk.Frame(setup); row.pack(fill="x", pady=8)
        self.folder_btn = ttk.Button(row, text="打开组件文件夹", command=lambda: self.open_folder(self.runtime))
        self.folder_btn.pack(side="left")
        ttk.Button(row, text="官方下载页", command=lambda: webbrowser.open(core.UPSTREAM_URL)).pack(side="left", padx=6)
        ttk.Button(row, text="查看上游许可", command=lambda: webbrowser.open(core.LICENSE_URL)).pack(side="left")
        self.install_btn = ttk.Button(row, text="运行官方安装器…", command=self.install)
        self.install_btn.pack(side="right")
        self.consent_check = ttk.Checkbutton(setup, text="我已阅读上游许可，确认本次是个人非商业测试，并同意运行自己提供的组件。",
                                            variable=self.license_consent)
        self.consent_check.pack(anchor="w")
        ttk.Label(setup, textvariable=self.components, style="Hint.TLabel", wraplength=self.wrap).pack(anchor="w", pady=(8, 0))

        controls = ttk.LabelFrame(outer, text="2  选择测试", padding=12)
        controls.pack(fill="x", pady=10)
        row = ttk.Frame(controls); row.pack(fill="x")
        ttk.Label(row, text="显卡").pack(side="left")
        self.gpu_box = ttk.Combobox(row, textvariable=self.gpu, state="readonly", values=["自动选择 AMD 独显"], width=42)
        self.gpu_box.pack(side="left", fill="x", expand=True, padx=8)
        self.inventory_btn = ttk.Button(row, text="检测环境", command=lambda: self.start(inventory=True))
        self.inventory_btn.pack(side="right")
        row = ttk.Frame(controls); row.pack(fill="x", pady=(8, 0))
        self.shared_check = ttk.Checkbutton(row, text="共享纹理（推荐）", variable=self.shared)
        self.shared_check.pack(side="left")
        self.high_check = ttk.Checkbutton(row, text="基础通过后附加 1080p（6 帧）", variable=self.high)
        self.high_check.pack(side="left", padx=12)
        row = ttk.Frame(controls); row.pack(fill="x", pady=(8, 0))
        self.photo_btn = ttk.Button(row, text="可选：选择测试图片…", command=self.choose_photo)
        self.photo_btn.pack(side="left")
        self.clear_btn = ttk.Button(row, text="使用内置图", command=self.clear_photo)
        self.clear_btn.pack(side="left", padx=6)
        ttk.Label(row, textvariable=self.custom, style="Hint.TLabel").pack(side="left")
        self.share_check = ttk.Checkbutton(controls, text="允许回传所选图片生成的对比预览（默认不勾选；不回传原文件）", variable=self.share_image)
        self.share_check.pack(anchor="w", pady=(7, 0))

        actions = ttk.Frame(outer); actions.pack(fill="x", pady=(0, 10))
        self.run_btn = ttk.Button(actions, text="开始 AMD 一轮测试", style="Primary.TButton", command=self.start)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(actions, text="取消", command=self.stop, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.recover_btn = ttk.Button(actions, text="恢复测试设置", command=self.recover)
        self.recover_btn.pack(side="right")
        self.export_btn = ttk.Button(actions, text="导出上次回传包", command=self.export_previous)
        self.export_btn.pack(side="right", padx=6)
        ttk.Label(outer, textvariable=self.status, wraplength=self.wrap).pack(anchor="w", pady=(0, 6))
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x")
        self.footer = ttk.Label(outer, text="回传只需 feedback-*.zip。无自动上传，不包含安装器、DLL、权重或内存转储。",
                                style="Hint.TLabel", wraplength=self.wrap)
        self.footer.pack(side="bottom", anchor="w", pady=(5, 0))
        self.log = tk.Text(outer, height=4, wrap="word", state="disabled", relief="flat", background="#e9eff3",
                           foreground="#273744", font=("Consolas", 10))
        self.log.pack(fill="both", expand=True, pady=(8, 5))
        self.mutable = [self.run_btn, self.inventory_btn, self.install_btn, self.photo_btn, self.clear_btn,
                        self.shared_check, self.high_check, self.share_check, self.recover_btn, self.export_btn, self.consent_check]
        self.refresh_components()
        self._poll_id = self.root.after(100, self.poll)

    def callback_error(self, _kind, value, _traceback):
        self.add_log("界面错误：" + core.sanitize(str(value), (self.folder,)))

    def add_log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", str(line) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_components(self):
        found = {name: (self.runtime/name).is_file() for name in core.COMPONENTS}
        if found["version.dll"] and found["dlssnr_on_amd_weights.bin"]:
            self.components.set("检测到模组和权重。文件存在 ≠ 已验证可用，请运行测试。")
        elif found["dlssnr_on_amd_setup.exe"]:
            self.components.set("已放入安装器；仍需按官方提示完成安装，生成 version.dll 和权重。")
        else:
            self.components.set("尚未放入安装器。也可直接放入由官方安装器生成的 version.dll 和权重。")

    def open_folder(self, path):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except OSError as error:
            messagebox.showerror("无法打开文件夹", str(error), parent=self.root)

    def choose_photo(self):
        name = filedialog.askopenfilename(parent=self.root, title="选择非敏感的 SDR 测试图片",
                                         filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if name:
            self.photo = name
            self.custom.set("已选图片（测试会缩放；默认不回传预览）")

    def clear_photo(self):
        self.photo = None
        self.custom.set("使用内置测试图（推荐第一轮）")

    def set_busy(self, busy):
        self.busy = busy
        for widget in self.mutable:
            widget.configure(state="disabled" if busy else "normal")
        self.gpu_box.configure(state="disabled" if busy else "readonly")
        self.cancel_btn.configure(state="normal" if busy and self.installer is None else "disabled")
        self.progress.start(12) if busy else self.progress.stop()

    def install(self):
        if not self.license_consent.get():
            messagebox.showinfo("需要确认", "请先阅读许可并勾选个人测试确认。", parent=self.root); return
        installer = self.runtime/"dlssnr_on_amd_setup.exe"
        if not installer.is_file():
            messagebox.showinfo("缺少安装器", "请从官方下载页取得安装器，并放入 amd_backend。", parent=self.root); return
        try:
            digest = core.file_hash(installer)
            known = core.KNOWN_INSTALLERS.get(digest)
            if not known:
                messagebox.showwarning("未核验的安装器", "这份安装器不是本开发包核验的 v0.2.15。\n"
                    "本测试器不会替你运行它。请在官方下载页自行确认来源，并手动安装后重新检测。", parent=self.root)
                return
            if not messagebox.askyesno("运行第三方安装器？", f"将运行你提供的官方 {known} 安装器。\n\n"
                    "它会在 amd_backend 中生成文件；本工具不修改它，也不自动回答它的提示。\n"
                    "请按官方提示操作，不要关闭系统安全保护或使用管理员权限绕过拦截。\n\n是否继续？", parent=self.root):
                return
            self.installer = subprocess.Popen([str(installer)], cwd=str(self.runtime), shell=False,
                                               creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            self.set_busy(True)
            self.status.set("安装器已打开，请在它的窗口中操作；关闭后回到这里测试。")
        except OSError as error:
            self.installer = None; self.set_busy(False)
            messagebox.showerror("安装器未能启动", str(error), parent=self.root)

    def start(self, inventory=False):
        if self.busy:
            return
        if not inventory:
            if not self.license_consent.get():
                messagebox.showinfo("需要确认", "请先阅读许可并勾选个人测试确认。", parent=self.root); return
            if not messagebox.askyesno("保存工作后再开始", "这是未在 A 卡上验证的开发测试。\n\n"
                    "GPU/驱动问题可能使屏幕闪黑、驱动重置，严重时需重启。进程隔离不能消除 GPU 风险。\n"
                    "请保存工作、关闭游戏和其他 GPU 任务。失败后不会自动重试。\n\n"
                    "将临时切换模组到同步测试设置并备份 INI，结束后恢复。\n是否开始？", parent=self.root):
                return
        self.cancel.clear(); self.set_busy(True)
        adapter = self.gpu_box.current()-1
        if adapter >= 0:
            adapter = self.adapters[adapter]["adapter"]
        snapshot = dict(adapter=adapter, inventory_only=inventory, shared=self.shared.get(),
                        custom=self.photo, include_images=self.share_image.get(), high_resolution=self.high.get())
        self.status.set("正在检查环境…" if inventory else "正在运行；请不要同时打开游戏或更换 DLL。")

        def worker():
            try:
                self.current_runner = core.Runner(self.folder, self.events.put, self.cancel)
                report, feedback = self.current_runner.run(**snapshot)
                self.events.put(("done", report, feedback, self.current_runner.run_dir))
            except Exception as error:
                self.events.put(("error", core.sanitize(error, (self.folder,))))
        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self.cancel.set()
        self.status.set("正在终止本次测试子进程并保存报告…")
        self.cancel_btn.configure(state="disabled")

    def recover(self):
        try:
            restored = core.restore_settings(self.runtime)
            messagebox.showinfo("恢复测试设置", "已恢复测试前 INI。" if restored else "没有需要恢复的备份。", parent=self.root)
        except OSError as error:
            messagebox.showerror("恢复失败", str(error), parent=self.root)

    def export_previous(self):
        try:
            self.feedback, self.last_run = core.export_previous(self.folder)
            self.add_log("已导出：" + str(self.feedback))
            self.open_folder(self.last_run)
        except OSError as error:
            messagebox.showerror("导出失败", str(error), parent=self.root)

    def poll(self):
        if self.installer and self.installer.poll() is not None:
            self.add_log("安装器已退出；请以实际生成的文件为准。")
            self.installer = None; self.set_busy(False); self.refresh_components()
            self.status.set("请确认组件状态，然后运行测试。")
        try:
            while True:
                item = self.events.get_nowait()
                if isinstance(item, tuple) and item[0] == "done":
                    _, report, self.feedback, self.last_run = item
                    self.adapters = report.get("adapters", [])
                    values = ["自动选择 AMD 独显"] + [f"{a['adapter']}: {a['name']} ({a['vram_mib']} MiB)" for a in self.adapters]
                    self.gpu_box.configure(values=values); self.gpu_box.current(0)
                    self.set_busy(False); self.refresh_components()
                    self.status.set(report["status_text"])
                    self.add_log("测试结束。点击“导出上次回传包”打开结果文件夹。")
                elif isinstance(item, tuple) and item[0] == "error":
                    self.set_busy(False); self.status.set("测试器错误；可导出上次中途报告。")
                    self.add_log(item[1])
                else:
                    self.add_log(item)
        except queue.Empty:
            pass
        self._poll_id = self.root.after(100, self.poll)

    def close(self):
        if self.installer is not None:
            messagebox.showinfo("请先关闭安装器", "安装器还在运行，请在它的窗口中退出。", parent=self.root); return
        if self.busy:
            self.stop()
            messagebox.showinfo("正在停止", "正在停止测试并恢复 INI；结束后可关闭窗口。", parent=self.root); return
        if self._poll_id:
            self.root.after_cancel(self._poll_id)
        self.root.destroy()


def launch(folder=None):
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        root.iconbitmap(str(core.resources_root()/"assets"/"app.ico"))
    except tk.TclError:
        pass
    DevTestWindow(root, folder)
    root.mainloop()
