#!/usr/bin/env python3
"""Offline, disposable AMD/FSR developer test runner (not a production backend).

Never extracts or patches the user's installer/mod. The native child uses real
public FSR calls; the same run without that mod is the reference, not the input.
No network requests, automatic installation, memory dumps, or telemetry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile

import numpy as np
from PIL import Image, ImageDraw


VERSION = "amd-dev1"
UPSTREAM_URL = "https://github.com/danielblnc/DLSS-NR-on-AMD/releases"
LICENSE_URL = "https://github.com/danielblnc/DLSS-NR-on-AMD/blob/master/LICENSE"
KNOWN_INSTALLERS = {
    "66b910da005e000070256b55c41dd5d0e1ae1ea6a8c3a9d0b76edc1749438092": "v0.2.15",
}
COMPONENTS = ("dlssnr_on_amd_setup.exe", "version.dll", "dlssnr_on_amd_weights.bin", "nvngx_dlssnr.dll")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STATUS_LABELS = {
    "PREFLIGHT_ONLY": "仅完成环境检查；尚未测试 AMD 增强",
    "MISSING_COMPONENTS": "缺少测试组件；请按说明准备后再运行",
    "NO_AMD_GPU": "未选择 AMD 显卡；没有加载第三方模组",
    "BASELINE_ONLY": "仅通过原生/FSR 基线；这不代表 AMD NR 可用",
    "CANCELLED": "测试已取消，已保存中途报告",
    "FAILED": "测试未完成，请回传诊断包",
    "INCONCLUSIVE": "未确认增强，不能据此启用 AMD 后端",
    "ENHANCEMENT_OBSERVED": "观察到 AMD 增强差异，仍需人工检查画质和时序",
}


def package_root():
    from dlss5tool.paths import app_root
    return app_root()


def resources_root():
    from dlss5tool.paths import resource_root
    return resource_root()


def native_path():
    if getattr(sys, "frozen", False):
        return resources_root() / "native" / "amd_probe.exe"
    return package_root() / "build" / "amd_probe" / "amd_probe.exe"


def fsr_path():
    if getattr(sys, "frozen", False):
        return resources_root() / "native" / "amd_fidelityfx_dx12.dll"
    return package_root() / "third_party" / "FidelityFX-1.1.4" / "PrebuiltSignedDLL" / "amd_fidelityfx_dx12.dll"


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def describe_file(path):
    path = Path(path)
    if not path.is_file():
        return {"name": path.name, "present": False}
    return {"name": path.name, "present": True, "bytes": path.stat().st_size, "sha256": file_hash(path)}


def write_json(path, payload):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def sanitize(text, roots=()):
    """Redact paths/identity from opt-in feedback. Keep module basenames only."""
    value = str(text)
    for root in sorted([str(p) for p in roots if p], key=len, reverse=True):
        value = re.sub(re.escape(root), "<test-folder>", value, flags=re.I)
        value = re.sub(re.escape(root.replace("\\", "/")), "<test-folder>", value, flags=re.I)
    # Also handle paths printed by the third-party module from other locations.
    value = re.sub(r"[A-Za-z]:[\\/][^\r\n\"<>|]*", "<local-path>", value)
    value = re.sub(r"\\\\[^\s\r\n]+", "<network-path>", value)
    for name in (os.environ.get("USERNAME"), os.environ.get("COMPUTERNAME")):
        if name and len(name) > 2:
            value = re.sub(r"\b" + re.escape(name) + r"\b", "<redacted>", value, flags=re.I)
    return value


def read_events(path):
    events = []
    try:
        # Bound untrusted diagnostic input; invalid or partial lines are ignored.
        text = Path(path).read_bytes()[:4 * 1024 * 1024].decode("utf-8", "replace")
    except OSError:
        return events
    for line in text.splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                events.append(item)
        except ValueError:
            pass
    return events


class Cancelled(RuntimeError):
    pass


class ChildJob:
    """Windows job owns only our native probe, including on controller exit."""
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
                        ("min_ws", c.c_size_t), ("max_ws", c.c_size_t), ("process_limit", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ("ro", "wo", "oo", "rt", "wt", "ot")]

        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", c.c_size_t),
                        ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

        k = c.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]; k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]; k.SetInformationJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]; k.AssignProcessToJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]; k.CloseHandle.restype = w.BOOL
        h = k.CreateJobObjectW(None, None)
        info = Extended(); info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not h or not k.SetInformationJobObject(h, 9, c.byref(info), c.sizeof(info)) or not k.AssignProcessToJobObject(h, w.HANDLE(process._handle)):
            if h:
                k.CloseHandle(h)
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("无法建立隔离进程作业；已停止测试，不继续加载第三方模组")
        self.handle, self.kernel = h, k

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_child(command, directory, cancel, progress, timeout=120):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    seen = 0
    with (directory / "console.txt").open("wb") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                                   cwd=str(directory), shell=False)
        job = ChildJob(process)
        try:
            while process.poll() is None:
                if cancel.is_set():
                    process.kill(); process.wait(timeout=5)
                    raise Cancelled("用户取消")
                events = read_events(directory / "native.jsonl")
                for event in events[seen:]:
                    if "stage" in event:
                        progress("阶段：" + str(event["stage"]))
                    if "frame_done" in event and event["frame_done"] % 6 == 0:
                        progress(f"已处理 {event['frame_done'] + 1} 帧")
                seen = len(events)
                if time.monotonic() - started > timeout:
                    timed_out = True
                    process.kill(); process.wait(timeout=5)
                    break
                cancel.wait(0.1)
            return {"exit_code": process.returncode, "timeout": timed_out,
                    "wall_seconds": round(time.monotonic() - started, 3),
                    "events": read_events(directory / "native.jsonl")}
        finally:
            job.close()


def make_scene(width, height, custom=None):
    if custom:
        with Image.open(custom) as image:
            image = image.convert("RGB")
            image.thumbnail((width, height), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (width, height), (35, 35, 35))
            canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
            return np.array(canvas)
    # Procedural test data, not a photograph and not a neural-image-quality proof.
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    rng = np.random.default_rng(613)
    noise = rng.normal(0, 4, (height, width)).astype(np.float32)
    colors = np.stack((35 + 145*x/width, 28 + 120*y/height, 42 + 80*(1-x/width)), axis=-1)
    colors += noise[..., None]
    image = Image.fromarray(np.uint8(np.clip(colors, 0, 255)))
    draw = ImageDraw.Draw(image)
    for n, color in enumerate(((198, 132, 99), (103, 142, 159), (164, 160, 84))):
        cx, cy, r = int(width*(0.2+n*0.3)), int(height*0.47), int(height*0.22)
        draw.ellipse((cx-r+7, cy-r+14, cx+r+7, cy+r+14), fill=(20, 23, 29))
        for radius in range(r, 0, -1):
            light = 0.45 + 0.55*np.sqrt(max(0, 1-(radius/r)**2))
            shade = tuple(int(c*light) for c in color)
            draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), fill=shade)
    for i in range(10):
        draw.line((width//12, height*3//4+i*3, width*11//12, height*3//4+i*3), fill=(160+i*6, 150+i*6, 135+i*6), width=1)
    return np.array(image)


def make_sequence(path, width, height, frames, custom=None):
    scene = make_scene(width, height, custom)
    frame_hashes = []
    # Distinct alternating top color markers detect gross stale/repeated frames.
    with Path(path).open("wb") as handle:
        for index in range(frames):
            picture = np.roll(scene, max(0, index-3)*2, axis=1).copy()
            if index >= frames // 2:
                picture = 255 - picture
            marker = (220, 35, 35) if index % 2 == 0 else (30, 215, 40)
            picture[:max(8, height//12), :] = marker
            rgba = np.ones((height, width, 4), dtype="<f2")
            rgba[..., :3] = picture / 255.0
            handle.write(rgba.tobytes())
            frame_hashes.append(hashlib.sha256(rgba.tobytes()).hexdigest())
    return frame_hashes


def load_frame(path, width, height):
    path = Path(path)
    if not path.is_file() or path.stat().st_size != width*height*8:
        raise ValueError("输出帧缺失或大小不符：" + path.name)
    return np.fromfile(path, dtype="<f2").reshape(height, width, 4).astype(np.float32)


def metric(reference, candidate):
    if reference.shape != candidate.shape or not np.isfinite(candidate).all() or not np.isfinite(reference).all():
        return {"valid": False}
    diff = np.abs(candidate[..., :3] - reference[..., :3])
    return {"valid": True, "mae": float(diff.mean()), "p99_abs": float(np.quantile(diff, .99)),
            "changed_fraction": float((diff.max(axis=-1) > 1/1024).mean()),
            "rgb_min": float(candidate[..., :3].min()), "rgb_max": float(candidate[..., :3].max())}


def marker_ok(frame, index):
    band = frame[:max(8, frame.shape[0]//12), :, :3].mean(axis=(0, 1))
    return bool(band[0] > band[1]+.15) if index % 2 == 0 else bool(band[1] > band[0]+.15)


def compare_cases(reference_dir, candidate_dir, width, height, frames, image_dir):
    results = []
    image_dir = Path(image_dir); image_dir.mkdir(parents=True, exist_ok=True)
    saved = {0, 1, frames//2-1, frames//2, frames-1}
    for index in range(frames):
        name = f"frame-{index:03d}.rgba16f"
        a = load_frame(Path(reference_dir)/name, width, height)
        b = load_frame(Path(candidate_dir)/name, width, height)
        stats = metric(a, b); stats.update(frame=index, marker_ok=marker_ok(b, index))
        # Exclude the marker from change evidence; it tests order, not NR quality.
        body = metric(a[max(8, height//12):], b[max(8, height//12):])
        stats["body_mae"] = body.get("mae", 0)
        results.append(stats)
        if index in saved and stats["valid"]:
            preview = np.concatenate((np.clip(a[..., :3], 0, 1), np.clip(b[..., :3], 0, 1),
                                      np.clip(.5 + (b[..., :3]-a[..., :3])*10, 0, 1)), axis=1)
            img = Image.fromarray(np.uint8(preview*255))
            img.thumbnail((1600, 600), Image.Resampling.LANCZOS)
            img.save(image_dir / f"frame-{index:03d}-fsr_nr_diff.png")
    return results


def verdict(comparison, mod_log, child_ok):
    if not child_ok:
        return "FAILED"
    if not comparison or not all(s.get("valid") and s.get("marker_ok") for s in comparison):
        return "INCONCLUSIVE"
    # An FSR result alone is never an NR success. Logs are supporting evidence,
    # not an authenticated API; even this positive result remains provisional.
    job_seen = bool(re.search(r"network job\s+\d+\s+done", mod_log, re.I))
    bad = bool(re.search(r"GPU wait timeout|GPU errors|setup failed|FAULT:|CRASH:|unsupported colour|invalid kernel", mod_log, re.I))
    differences = [s["body_mae"] for s in comparison[3:]]
    changed = bool(differences and np.median(differences) > 1/2048)
    bounded = all(s.get("rgb_min", -99) >= -.05 and s.get("rgb_max", 99) <= 1.1 for s in comparison)
    return "ENHANCEMENT_OBSERVED" if job_seen and not bad and changed and bounded else "INCONCLUSIVE"


def mod_log_snapshot(runtime):
    snapshot = {}
    for path in Path(runtime).glob("*dlssnr*.log"):
        if path.is_file():
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(max(0, size-512))
                tail_hash = hashlib.sha256(handle.read(512)).hexdigest()
            snapshot[path.name] = (size, path.stat().st_mtime_ns, tail_hash)
    return snapshot


def new_mod_logs(runtime, before):
    logs = {}
    for path in Path(runtime).glob("*dlssnr*.log"):
        if not path.is_file():
            continue
        size, stamp = path.stat().st_size, path.stat().st_mtime_ns
        old_size, old_stamp, old_hash = before.get(path.name, (0, 0, ""))
        if (size, stamp) == (old_size, old_stamp):
            continue
        # Some builds truncate the file on each process start. Retain at most
        # 2 MiB; baseline never loads the mod, so only this NR case writes it.
        with path.open("rb") as handle:
            start = 0
            if size > old_size > 0:
                handle.seek(max(0, old_size-512))
                if hashlib.sha256(handle.read(min(old_size, 512))).hexdigest() == old_hash:
                    start = old_size  # an append, not this case's entire log
            handle.seek(max(start, size - 2*1024*1024))
            logs[path.name] = handle.read(2*1024*1024).decode("utf-8", "replace")
    return logs


def save_mod_logs(runtime, before, directory, roots=()):
    logs = new_mod_logs(runtime, before)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    text = "\n".join(f"--- {name} ---\n{content}" for name, content in logs.items())
    if text:
        (directory/"mod-log.txt").write_text(sanitize(text, roots), encoding="utf-8")
    return text


def validate_baseline(directory, width, height, frames):
    for index in range(frames):
        frame = load_frame(Path(directory)/f"frame-{index:03d}.rgba16f", width, height)
        if not np.isfinite(frame).all() or not marker_ok(frame, index):
            raise RuntimeError("无模组 FSR 基线已存在无效像素或帧标记错位，不能继续对照 NR")
        if frame[..., :3].min() < -.05 or frame[..., :3].max() > 1.1:
            raise RuntimeError("无模组 FSR 基线范围异常，不能继续对照 NR")


class RuntimeSettings:
    """Temporary public INI settings in our dedicated runtime dir; restore exactly."""
    def __init__(self, runtime, shared):
        self.path = Path(runtime)/"dlssnr_on_amd.ini"
        self.backup = self.path.with_name("dlssnr_on_amd.ini.before-devtest")
        self.shared = shared
        self.original = None

    def __enter__(self):
        if self.backup.exists():
            raise RuntimeError("发现上次未恢复的 INI 备份，请先用“恢复测试设置”恢复后再测")
        self.original = self.path.read_bytes() if self.path.exists() else None
        if self.original is not None:
            with self.backup.open("xb") as out:
                out.write(self.original)
        else:
            # Empty backup is still a recoverable file, and restoration is safe.
            with self.backup.open("xb") as out:
                out.write(b"")
        config = ("[DlssNrOnAmd]\nEnabled=1\nLocalTone=0\nLocalStructure=1\n"
                  "SkinStructure=-1\nUseAutoMask=1\nToneChannels=0\nScale=0.03125\n"
                  "Temporal=1\nTonemap=0\nUseFsrInputs=1\nUseDepth=0\n"
                  f"Interop={int(self.shared)}\nInline=1\nHipDevice=-1\n")
        try:
            self.path.write_text(config, encoding="utf-8")
        except Exception:
            restore_settings(self.path.parent)
            raise
        return config

    def __exit__(self, *_):
        restore_settings(self.path.parent)


def restore_settings(runtime):
    target = Path(runtime)/"dlssnr_on_amd.ini"
    backup = target.with_name("dlssnr_on_amd.ini.before-devtest")
    if not backup.exists():
        return False
    # Recoverable rename replaces only our INI. If no INI existed before, the
    # restored empty file asks the mod to use its normal defaults.
    os.replace(backup, target)
    return True


def trusted_files():
    if not getattr(sys, "frozen", False):
        return  # developer builds have no packaged manifest
    manifest = json.loads((resources_root()/"native-manifest.json").read_text(encoding="utf-8"))
    for path in (native_path(), fsr_path()):
        if file_hash(path) != manifest.get(path.name):
            raise RuntimeError("内置测试组件校验失败，请重新完整解压开发包：" + path.name)


class Runner:
    def __init__(self, root=None, progress=None, cancel=None):
        self.root = Path(root or package_root()).resolve()
        self.runtime = self.root/"amd_backend"
        self.progress = progress or (lambda line: print(line, flush=True))
        self.cancel = cancel or threading.Event()
        self.run_dir = None

    def command(self, mode, out, adapter=-1, shared=True, stream=None, width=640, height=360, frames=24):
        cmd = [str(native_path()), "--mode", mode, "--output", str(out),
               "--runtime", str(self.runtime), "--fsr", str(fsr_path()),
               "--adapter", str(adapter), "--shared", str(int(shared))]
        if stream:
            cmd += ["--input", str(stream), "--width", str(width), "--height", str(height), "--frames", str(frames)]
        return cmd

    def run(self, adapter=-1, baseline_only=False, inventory_only=False, shared=True,
            custom=None, include_images=False, high_resolution=False):
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.run_dir = self.root/"results"/run_id
        self.run_dir.mkdir(parents=True)
        report = {"schema": 1, "version": VERSION, "run_id": run_id, "status": "PREFLIGHT_ONLY",
                  "requested_test": "inventory" if inventory_only else "baseline" if baseline_only else "nr",
                  "os": {"system": platform.system(), "version": platform.version()},
                  "image_source": "user-selected" if custom else "procedural",
                  "images_in_feedback": bool(include_images or not custom),
                  "production_ready": False, "cases": [], "notes": []}
        report["limits"] = ["SDR only", "FSR NativeAA reference, not direct NGX parity", "zero motion/depth input",
                            "short sequence only", "no GPU TDR recovery guarantee", "no binary changes to upstream"]
        write_json(self.run_dir/"report.json", report)
        try:
            trusted_files()
            self.runtime.mkdir(exist_ok=True)
            report["components"] = [describe_file(self.runtime/name) for name in COMPONENTS]
            report["components"] += [describe_file(native_path()), describe_file(fsr_path())]
            self.progress("1/4 检查显卡和运行环境（不加载 AMD 模组）")
            out = self.run_dir/"inventory"
            inventory = run_child(self.command("inventory", out), out, self.cancel, self.progress, 20)
            report["cases"].append({"name": "inventory", **inventory})
            if inventory["exit_code"] != 0:
                raise RuntimeError("原生环境检查失败")
            adapters = [e for e in inventory["events"] if "adapter" in e]
            report["adapters"] = adapters
            if inventory_only:
                return self.finish(report)
            amd = [a for a in adapters if a.get("vendor") == 0x1002 and not a.get("software")]
            selected = next((a for a in adapters if a["adapter"] == adapter), None)
            if adapter < 0 and amd:
                selected = max(amd, key=lambda a: a.get("vram_mib", 0))
            if selected is None or (not baseline_only and selected.get("vendor") != 0x1002):
                report["status"] = "NO_AMD_GPU"
                return self.finish(report)
            adapter = selected["adapter"]
            report["selected_adapter"] = adapter
            if not baseline_only and (not (self.runtime/"version.dll").is_file() or not (self.runtime/"dlssnr_on_amd_weights.bin").is_file()):
                report["status"] = "MISSING_COMPONENTS"
                report["notes"].append("请先运行官方安装器，生成 version.dll 和权重文件；仅放入 EXE 不等于完成安装。")
                return self.finish(report)
            shapes = [(640, 360, 24)]
            if high_resolution:
                shapes.append((1920, 1080, 6))
            combined_results = []
            for width, height, frames in shapes:
                group = self.run_dir/f"{width}x{height}"
                group.mkdir()
                stream = group/"input.rgba16f"
                report.setdefault("sequences", []).append({"width": width, "height": height, "frames": frames,
                    "input_sha256": make_sequence(stream, width, height, frames, custom), "cut_frame": frames//2,
                    "shared": shared, "reset": [0, frames//2]})
                modes = ("copy", "fsr") if baseline_only else ("copy", "fsr", "nr")
                for mode in modes:
                    if self.cancel.is_set():
                        raise Cancelled("用户取消")
                    self.progress({"copy": "2/4 验证 GPU 上传与回读", "fsr": "3/4 生成无模组 FSR 基线", "nr": "4/4 测试 AMD NR"}[mode] + f" · {width}×{height}")
                    out = group/mode
                    before = mod_log_snapshot(self.runtime)
                    if mode == "nr":
                        try:
                            with RuntimeSettings(self.runtime, shared) as config:
                                report["test_ini"] = config
                                write_json(self.run_dir/"report.json", report)
                                result = run_child(self.command(mode, out, adapter, shared, stream, width, height, frames),
                                                   out, self.cancel, self.progress)
                        finally:
                            # Collect even if cancellation or controller-side errors
                            # prevented the normal per-case completion path.
                            mod_text = save_mod_logs(self.runtime, before, out, (self.root,))
                    else:
                        result = run_child(self.command(mode, out, adapter, shared, stream, width, height, frames),
                                           out, self.cancel, self.progress)
                    report["cases"].append({"name": f"{width}x{height}/{mode}", **result})
                    completed = any(e.get("completed") and e.get("frames") == frames for e in result["events"])
                    if result["exit_code"] != 0 or result["timeout"] or not completed:
                        raise RuntimeError(f"{mode} 子进程未正常完成；不自动反复重试 GPU 测试")
                    if mode == "copy":
                        copy_hash = hashlib.sha256()
                        for i in range(frames):
                            copy_hash.update((out/f"frame-{i:03d}.rgba16f").read_bytes())
                        if copy_hash.hexdigest() != file_hash(stream):
                            raise RuntimeError("原生 GPU 上传/回读与输入不一致")
                    if mode == "fsr":
                        validate_baseline(out, width, height, frames)
                    if mode == "nr":
                        comparison = compare_cases(group/"fsr", group/"nr", width, height, frames, group/"previews")
                        this_verdict = verdict(comparison, mod_text, True)
                        combined_results.append(this_verdict)
                        report.setdefault("comparisons", []).append({"size": [width, height], "status": this_verdict, "frames": comparison})
                        if this_verdict != "ENHANCEMENT_OBSERVED":
                            report["notes"].append("低分辨率未确认有效增强，不继续更高负载测试。")
                    write_json(self.run_dir/"report.json", report)
                if combined_results and combined_results[-1] != "ENHANCEMENT_OBSERVED":
                    break
            report["status"] = "BASELINE_ONLY" if baseline_only else (
                "ENHANCEMENT_OBSERVED" if combined_results and all(s == "ENHANCEMENT_OBSERVED" for s in combined_results) else "INCONCLUSIVE")
        except Cancelled:
            report["status"] = "CANCELLED"
        except Exception as error:
            report["status"] = "FAILED"
            report["error"] = sanitize(error, (self.root,))
        return self.finish(report)

    def finish(self, report):
        # Also collect partial worker output after timeout/crash/cancel.
        report["status_text"] = STATUS_LABELS[report["status"]]
        report["test_finished"] = True
        # Sanitize strings structurally, never run path regexes across serialized
        # JSON: Windows path escapes can otherwise corrupt the JSON syntax.
        safe = sanitize_payload(report, (self.root,))
        write_json(self.run_dir/"report.json", safe)
        (self.run_dir/"SUMMARY.txt").write_text(summary_text(safe), encoding="utf-8")
        feedback = make_feedback(self.run_dir, bool(report["images_in_feedback"]), (self.root,))
        self.progress(safe["status_text"])
        self.progress("回传文件：" + str(feedback))
        return safe, feedback


def sanitize_payload(value, roots=()):
    if isinstance(value, str):
        return sanitize(value, roots)
    if isinstance(value, dict):
        return {k: sanitize_payload(v, roots) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(v, roots) for v in value]
    return value


def summary_text(report):
    lines = ["DLSS5Tool AMD 开发测试报告 " + VERSION, report.get("status_text", "尚未完成"), "",
             "本报告不代表正式 AMD 支持、画质一致或视频稳定性认证。",
             "对比图左：无模组 FSR；中：加载模组；右：相对 FSR 的差异×10。",
             "帧编号相同才能比较；色条仅检查粗略错帧，不能证明没有拖影。", ""]
    if report.get("error"):
        lines += ["错误：" + report["error"]]
    for a in report.get("adapters", []):
        lines += [f"GPU {a['adapter']}: {a['name']} / {a.get('vram_mib')} MiB / driver {a.get('driver')}"]
    for c in report.get("cases", []):
        lines += [f"{c['name']}: exit={c.get('exit_code')} timeout={c.get('timeout')} time={c.get('wall_seconds')}s"]
    lines += ["", "请手动回传同目录 feedback-*.zip；不会自动上传任何文件。",
              "若系统曾卡死或重启：再次打开测试器，仅导出上一次结果，不要立即重试。"]
    return "\n".join(lines) + "\n"


def make_feedback(run_dir, include_images=False, roots=()):
    """Strict allowlist: no DLLs, EXEs, weights, input streams, or memory dumps."""
    run_dir = Path(run_dir).resolve()
    target = run_dir/f"feedback-{run_dir.name}.zip"
    allowed_text = {"report.json", "SUMMARY.txt", "native.jsonl", "console.txt", "mod-log.txt"}
    temporary = target.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(run_dir):
                continue
            relative = path.relative_to(run_dir).as_posix()
            if path.name in allowed_text and path.stat().st_size <= 4*1024*1024:
                text = path.read_text(encoding="utf-8", errors="replace")
                if path.suffix == ".json":
                    try:
                        text = json.dumps(sanitize_payload(json.loads(text), roots), ensure_ascii=False, indent=2)
                    except ValueError:
                        text = "{\"error\":\"partial report; consult native log\"}"
                else:
                    text = sanitize(text, roots)
                bundle.writestr(relative, text)
            elif include_images and path.suffix == ".png" and path.parent.name == "previews" and path.stat().st_size <= 8*1024*1024:
                bundle.write(path, relative)
    os.replace(temporary, target)
    return target


def export_previous(root):
    """Prefer the last actual test over a later harmless inventory check."""
    root = Path(root).resolve()
    results = root/"results"
    runs = sorted((p for p in results.iterdir() if p.is_dir() and not p.is_symlink()),
                  key=lambda p: p.name, reverse=True) if results.exists() else []
    if not runs:
        raise FileNotFoundError("暂无测试结果")
    choices = []
    for path in runs:
        try:
            report = json.loads((path/"report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
        choices.append((path, report))
    selected = next(((p, r) for p, r in choices if r.get("requested_test") == "nr"), choices[0])
    path, report = selected
    return make_feedback(path, bool(report.get("images_in_feedback", False)), (root,)), path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--adapter", type=int, default=-1)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--shared", type=int, choices=[0, 1], default=1)
    args = parser.parse_args()
    if args.inventory or args.baseline_only:
        report, feedback = Runner(args.root).run(args.adapter, baseline_only=args.baseline_only,
                                               inventory_only=args.inventory, shared=bool(args.shared))
        print(feedback)
        return 0 if report["status"] in {"PREFLIGHT_ONLY", "BASELINE_ONLY"} else 2
    from dlss5tool.amd_devtest_ui import launch
    launch(args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
