#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FFmpeg-backed video export with NVENC and source-audio preservation."""

from collections import deque
import os
import shutil
import subprocess
import sys
import tempfile
import threading

import cv2
import numpy as np


_NVENC_CACHE = {}
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MP4_COPY_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "alac"}


def find_ffmpeg():
    """Return a usable FFmpeg executable, preferring a bundled/full build."""
    names = []
    configured = os.environ.get("FFMPEG_EXE")
    if configured:
        names.append(configured)

    bundle_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    names.extend([
        os.path.join(bundle_dir, "ffmpeg.exe"),
        os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "ffmpeg.exe"),
        shutil.which("ffmpeg"),
    ])

    # setup.bat installs this lightweight fallback. A full FFmpeg on PATH is still
    # preferred because it is more likely to include NVIDIA's NVENC encoder.
    try:
        import imageio_ffmpeg
        names.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass

    for name in names:
        if name and os.path.isfile(name):
            return os.path.abspath(name)
    raise RuntimeError(
        "未找到 FFmpeg。请运行 setup.bat，或安装 FFmpeg 并将 ffmpeg.exe 加入 PATH。"
    )


def has_h264_nvenc(ffmpeg):
    """Probe the actual NVENC device, not merely whether the encoder is listed."""
    key = os.path.normcase(os.path.abspath(ffmpeg))
    if key in _NVENC_CACHE:
        return _NVENC_CACHE[key]
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        # Current NVENC drivers reject dimensions below their minimum; 256x256
        # is still cheap to probe and works across supported RTX generations.
        "-i", "color=c=black:s=256x256:r=1", "-frames:v", "1", "-an",
        "-c:v", "h264_nvenc", "-f", "null", "-",
    ]
    try:
        probe = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, creationflags=_CREATE_NO_WINDOW,
        )
        available = probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        available = False
    _NVENC_CACHE[key] = available
    return available


def probe_audio_codecs(ffmpeg, source):
    """Return source audio codec names, [] for no audio, or None without ffprobe."""
    suffix = ".exe" if os.name == "nt" else ""
    sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + suffix)
    ffprobe = sibling if os.path.isfile(sibling) else shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0", source,
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=15, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [
        line.strip().lower()
        for line in result.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]


def compose_output_frame(original, processed, view=0, mix=1.0):
    """Create the selected export view from original/processed BGR frames."""
    width = original.shape[1]
    if view == 1:
        pf = processed.astype(np.float32)
        of = original.astype(np.float32)
        return (np.clip(0.5 + (pf - of) / 255.0 * 10.0, 0, 1) * 255).astype(np.uint8)
    if view == 2:
        frame = processed.copy()
        frame[:, :width // 2] = original[:, :width // 2]
        if width > 1:
            frame[:, max(width // 2 - 1, 0)] = [255, 255, 255]
        return frame
    if mix >= 1.0:
        return processed
    if mix <= 0.0:
        return original
    return cv2.addWeighted(original, 1.0 - mix, processed, mix, 0.0)


def mux_source_audio(ffmpeg, video_path, audio_source, output_path):
    """Atomically attach source audio to an encoded video and return the audio mode."""
    video_path = os.path.abspath(video_path)
    audio_source = os.path.abspath(audio_source)
    output_path = os.path.abspath(output_path)
    audio_codecs = probe_audio_codecs(ffmpeg, audio_source)
    if audio_codecs == []:
        os.replace(video_path, output_path)
        return "源视频无音轨"

    output_dir = os.path.dirname(output_path) or os.getcwd()
    prefix = "." + os.path.basename(output_path) + "."
    mux_temp = tempfile.NamedTemporaryFile(
        prefix=prefix, suffix=".mux.tmp.mp4", dir=output_dir, delete=False
    )
    mux_path = mux_temp.name
    mux_temp.close()
    common = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", video_path, "-i", audio_source,
        "-map", "0:v:0", "-map", "1:a?",
        "-c:v", "copy", "-movflags", "+faststart",
    ]
    try:
        if audio_codecs and all(c in _MP4_COPY_AUDIO_CODECS for c in audio_codecs):
            result = subprocess.run(
                common + ["-c:a", "copy", mux_path],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, creationflags=_CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                os.replace(mux_path, output_path)
                return "原音轨直通"

        fallback = subprocess.run(
            common + ["-c:a", "aac", "-b:a", "192k", mux_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=_CREATE_NO_WINDOW,
        )
        if fallback.returncode != 0:
            detail = fallback.stderr.decode("utf-8", errors="replace")[-4000:]
            raise RuntimeError("FFmpeg 音频封装失败：\n" + detail)
        os.replace(mux_path, output_path)
        return "AAC 192 kbps（兼容转换）"
    finally:
        if os.path.isfile(mux_path):
            try:
                os.remove(mux_path)
            except OSError:
                pass


def concat_video_segments(ffmpeg, segments, output_path):
    """Losslessly concatenate compatible MP4 video-only segments."""
    if not segments:
        raise ValueError("没有可拼接的视频分段")
    output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(output_path) or os.getcwd()
    listing = tempfile.NamedTemporaryFile(
        mode="w", prefix=".dlss-concat-", suffix=".txt", dir=output_dir,
        delete=False, encoding="utf-8", newline="\n",
    )
    try:
        with listing:
            for segment in segments:
                normalized = os.path.abspath(segment).replace("\\", "/")
                escaped = normalized.replace("'", "'\\''")
                listing.write(f"file '{escaped}'\n")
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", listing.name,
                "-map", "0:v:0", "-c:v", "copy", "-an", output_path,
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=_CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[-4000:]
            raise RuntimeError("FFmpeg 分段拼接失败：\n" + detail)
    finally:
        try:
            os.remove(listing.name)
        except OSError:
            pass


def _tail_text(lines):
    text = "".join(lines).strip()
    return text[-4000:] if text else "FFmpeg 未提供错误详情"


class FFmpegVideoWriter:
    """Stream BGR24 frames to FFmpeg, then attach audio from the source file."""

    def __init__(
        self, output_path, width, height, fps, audio_source=None,
        use_nvenc=None, nvenc_preset="p5",
    ):
        self.output_path = os.path.abspath(output_path)
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps)
        self.audio_source = os.path.abspath(audio_source) if audio_source else None
        self.ffmpeg = find_ffmpeg()
        self.uses_nvenc = has_h264_nvenc(self.ffmpeg) if use_nvenc is None else bool(use_nvenc)
        self.nvenc_preset = nvenc_preset if nvenc_preset in {f"p{i}" for i in range(1, 8)} else "p5"
        self.encoder_name = "NVIDIA NVENC (GPU)" if self.uses_nvenc else "libx264 (CPU 回退)"
        self._stderr = deque(maxlen=100)
        self._frames = 0
        self._finished = False

        output_dir = os.path.dirname(self.output_path) or os.getcwd()
        prefix = "." + os.path.basename(self.output_path) + "."
        temp = tempfile.NamedTemporaryFile(
            prefix=prefix, suffix=".video.tmp.mp4", dir=output_dir, delete=False
        )
        self._temp_path = temp.name
        temp.close()

        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "rawvideo", "-pixel_format", "bgr24",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", f"{self.fps:.12g}", "-i", "pipe:0", "-an",
            # H.264 4:2:0 needs even dimensions; padding affects only unusual odd-sized input.
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
        ]
        if self.uses_nvenc:
            cmd.extend([
                "-c:v", "h264_nvenc", "-preset", self.nvenc_preset, "-tune", "hq",
                "-rc", "vbr", "-cq", "19", "-b:v", "0",
            ])
        else:
            cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "18"])
        cmd.extend(["-movflags", "+faststart", self._temp_path])

        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, bufsize=0, creationflags=_CREATE_NO_WINDOW,
            )
        except Exception:
            self._remove_temp()
            raise
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self):
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                self._stderr.append(raw.decode("utf-8", errors="replace"))
        except Exception:
            pass

    def write(self, frame):
        if self._finished:
            raise RuntimeError("不能向已经结束的导出任务写入帧")
        if frame.shape != (self.height, self.width, 3) or frame.dtype != np.uint8:
            raise ValueError(
                f"帧格式应为 {self.width}x{self.height} BGR uint8，实际为 {frame.shape}/{frame.dtype}"
            )
        try:
            # Pass NumPy's buffer directly to the pipe instead of allocating a full
            # frame-sized Python bytes object for every frame.
            view = memoryview(np.ascontiguousarray(frame)).cast("B")
            fd = self._proc.stdin.fileno()
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise BrokenPipeError("FFmpeg pipe accepted zero bytes")
                view = view[written:]
            self._frames += 1
        except (BrokenPipeError, OSError) as ex:
            self._wait_encoder()
            raise RuntimeError("FFmpeg 编码中断：\n" + _tail_text(self._stderr)) from ex

    def _wait_encoder(self):
        if self._proc.stdin and not self._proc.stdin.closed:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        code = self._proc.wait()
        self._stderr_thread.join(timeout=2)
        return code

    def finish(self):
        if self._finished:
            return
        self._finished = True
        if self._frames == 0:
            self.abort()
            raise RuntimeError("没有可导出的视频帧")
        code = self._wait_encoder()
        if code != 0:
            error = _tail_text(self._stderr)
            self._remove_temp()
            raise RuntimeError("FFmpeg 视频编码失败：\n" + error)

        try:
            if self.audio_source:
                self.audio_mode = mux_source_audio(
                    self.ffmpeg, self._temp_path, self.audio_source, self.output_path
                )
            else:
                os.replace(self._temp_path, self.output_path)
                self.audio_mode = "无音频源"
                self._temp_path = None
        finally:
            self._remove_temp()

    def abort(self):
        if hasattr(self, "_proc") and self._proc.poll() is None:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
            except OSError:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        self._remove_temp()

    def _remove_temp(self):
        path = getattr(self, "_temp_path", None)
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
        self._temp_path = None
