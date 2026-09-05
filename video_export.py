#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FFmpeg-backed video export with NVENC and source-audio preservation."""

from collections import deque
from fractions import Fraction
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

import cv2
import numpy as np


_NVENC_CACHE = {}
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
OUTPUT_CONTAINER_EXTENSIONS = {"mp4": ".mp4", "mkv": ".mkv", "mov": ".mov"}
_SOURCE_CONTAINER_MAP = {
    ".mp4": "mp4", ".m4v": "mp4", ".mkv": "mkv", ".mov": "mov",
}
_ISO_BMFF_COPY_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "alac"}
_HDR_TRANSFERS = {"smpte2084": "hdr10_pq", "arib-std-b67": "hdr10_hlg"}
_MAX_OUTPUT_MIX = 5.0
_QUALITY_PROFILE_VALUES = {
    "maximum": {"nvenc": 16, "software": 16},
    "high": {"nvenc": 19, "software": 18},
    "balanced": {"nvenc": 23, "software": 22},
    "compact": {"nvenc": 27, "software": 26},
}
_SOFTWARE_PRESETS = {
    "p1": "ultrafast",
    "p2": "superfast",
    "p3": "veryfast",
    "p4": "faster",
    "p5": "fast",
    "p6": "medium",
    "p7": "slow",
}


def resolve_output_container(source_path, preference="mp4"):
    """Resolve an explicit or follow-source choice to mp4, mkv, or mov."""
    preference = str(preference or "mp4").strip().lower()
    if preference in OUTPUT_CONTAINER_EXTENSIONS:
        return preference
    if preference == "source":
        source_ext = os.path.splitext(str(source_path or ""))[1].lower()
        return _SOURCE_CONTAINER_MAP.get(source_ext, "mp4")
    return "mp4"


def output_container_extension(container):
    return OUTPUT_CONTAINER_EXTENSIONS.get(str(container).lower(), ".mp4")


def output_container_from_path(path):
    ext = os.path.splitext(str(path or ""))[1].lower()
    return {value: key for key, value in OUTPUT_CONTAINER_EXTENSIONS.items()}.get(
        ext, "mp4"
    )


def _clamp_video_bitrate(value):
    try:
        return max(0.5, min(500.0, float(value)))
    except (TypeError, ValueError):
        return 20.0


def _bitrate_arg(value):
    return f"{float(value):.3f}".rstrip("0").rstrip(".") + "M"


def build_video_encoder_args(
    is_hdr, uses_nvenc, nvenc_preset="p5", rate_control="quality",
    quality_profile="high", video_bitrate_mbps=20.0,
):
    """Build one validated encoder policy for SDR/HDR and GPU/CPU paths."""
    is_hdr = bool(is_hdr)
    uses_nvenc = bool(uses_nvenc)
    nvenc_preset = nvenc_preset if nvenc_preset in _SOFTWARE_PRESETS else "p5"
    rate_control = rate_control if rate_control in {"quality", "bitrate"} else "quality"
    quality_profile = (
        quality_profile if quality_profile in _QUALITY_PROFILE_VALUES else "high"
    )
    bitrate = _clamp_video_bitrate(video_bitrate_mbps)

    if uses_nvenc:
        args = [
            "-c:v", "hevc_nvenc" if is_hdr else "h264_nvenc",
        ]
        if is_hdr:
            args.extend(["-profile:v", "main10"])
        args.extend(["-preset", nvenc_preset, "-tune", "hq"])
        if rate_control == "quality":
            quality = _QUALITY_PROFILE_VALUES[quality_profile]["nvenc"]
            args.extend(["-rc", "vbr", "-cq", str(quality), "-b:v", "0"])
        else:
            args.extend([
                "-rc", "vbr",
                "-b:v", _bitrate_arg(bitrate),
                "-maxrate", _bitrate_arg(bitrate * 1.5),
                "-bufsize", _bitrate_arg(bitrate * 2.0),
            ])
        return args

    args = [
        "-c:v", "libx265" if is_hdr else "libx264",
        "-preset", _SOFTWARE_PRESETS[nvenc_preset],
    ]
    if is_hdr:
        args[2:2] = ["-profile:v", "main10"]
    if rate_control == "quality":
        quality = _QUALITY_PROFILE_VALUES[quality_profile]["software"]
        args.extend(["-crf", str(quality)])
    else:
        args.extend([
            "-b:v", _bitrate_arg(bitrate),
            "-maxrate", _bitrate_arg(bitrate * 1.5),
            "-bufsize", _bitrate_arg(bitrate * 2.0),
        ])
    return args


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


def find_ffprobe(ffmpeg=None):
    """Return ffprobe when available; HDR probing has an FFmpeg-only fallback."""
    configured = os.environ.get("FFPROBE_EXE")
    suffix = ".exe" if os.name == "nt" else ""
    sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + suffix) if ffmpeg else None
    for candidate in (configured, sibling, shutil.which("ffprobe")):
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _rate_value(value, default=30.0):
    try:
        rate = float(Fraction(str(value)))
        return rate if rate > 0 else float(default)
    except (TypeError, ValueError, ZeroDivisionError):
        return float(default)


def classify_color_info(values=None):
    """Normalize ffprobe fields and name the NGX input color contract."""
    source = dict(values or {})
    transfer = str(source.get("color_transfer") or "unknown").lower()
    profile = _HDR_TRANSFERS.get(transfer, "srgb")
    is_hdr = profile != "srgb"
    unspecified = {"", "unknown", "unspecified", "reserved", "none", "n/a"}
    primaries = str(source.get("color_primaries") or "").lower()
    matrix = str(source.get("color_space") or "").lower()
    if is_hdr:
        source["color_primaries"] = "bt2020" if primaries in unspecified else primaries
        source["color_space"] = "bt2020nc" if matrix in unspecified else matrix
    else:
        source["color_primaries"] = "bt709" if primaries in unspecified else primaries
        source["color_space"] = "bt709" if matrix in unspecified else matrix
    source["color_transfer"] = transfer
    source["color_range"] = str(source.get("color_range") or "tv").lower()
    source["pixel_format"] = str(source.get("pix_fmt") or source.get("pixel_format") or "unknown")
    source["profile"] = profile
    source["is_hdr"] = is_hdr
    source["label"] = (
        "HDR10 / PQ" if profile == "hdr10_pq" else
        "HDR / HLG" if profile == "hdr10_hlg" else "SDR / sRGB"
    )
    return source


def probe_video_stream(ffmpeg, source):
    """Read dimensions, timing, and color metadata without decoding a frame."""
    source = os.path.abspath(source)
    ffprobe = find_ffprobe(ffmpeg)
    if ffprobe:
        try:
            result = subprocess.run(
                [
                    ffprobe, "-v", "error", "-select_streams", "v:0",
                    "-show_entries",
                    "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration,pix_fmt,color_space,color_primaries,color_transfer,color_range",
                    "-of", "json", source,
                ],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=20, creationflags=_CREATE_NO_WINDOW,
            )
            payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
            streams = payload.get("streams") or []
            if result.returncode == 0 and streams:
                stream = dict(streams[0])
                stream["fps"] = _rate_value(
                    stream.get("avg_frame_rate") or stream.get("r_frame_rate")
                )
                try:
                    stream["frames"] = int(stream.get("nb_frames") or 0)
                except (TypeError, ValueError):
                    stream["frames"] = 0
                return classify_color_info(stream)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            pass

    # imageio-ffmpeg bundles ffmpeg but not ffprobe. Its input banner still
    # carries the transfer/primaries/matrix tuple needed to select HDR safely.
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", source],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=20, creationflags=_CREATE_NO_WINDOW,
        )
        banner = result.stderr.decode("utf-8", errors="replace").lower()
    except (OSError, subprocess.SubprocessError):
        banner = ""
    transfer = next((name for name in _HDR_TRANSFERS if name in banner), "unknown")
    primaries = "bt2020" if "bt2020" in banner else "bt709" if "bt709" in banner else "unknown"
    matrix = "bt2020nc" if "bt2020nc" in banner else "bt709" if "bt709" in banner else "unknown"
    pix_fmt = "unknown"
    match = re.search(
        r"video:.*?\b(p010[a-z0-9_]*|(?:yuv|gbr)[a-z0-9_]*10[a-z0-9_]*)\b",
        banner,
    )
    if match:
        pix_fmt = match.group(1)
    return classify_color_info({
        "color_transfer": transfer,
        "color_primaries": primaries,
        "color_space": matrix,
        "pix_fmt": pix_fmt,
    })


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


def has_hevc_main10_nvenc(ffmpeg):
    """Probe the real 10-bit HEVC path used by HDR export."""
    key = (os.path.normcase(os.path.abspath(ffmpeg)), "hevc-main10")
    if key in _NVENC_CACHE:
        return _NVENC_CACHE[key]
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=c=black:s=256x256:r=1", "-frames:v", "1", "-an",
        "-vf", "format=p010le", "-c:v", "hevc_nvenc", "-profile:v", "main10",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=20, creationflags=_CREATE_NO_WINDOW,
        )
        available = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        available = False
    _NVENC_CACHE[key] = available
    return available


def _pq_eotf(encoded):
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 32.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 128.0
    c3 = 2392.0 / 128.0
    value = np.maximum(np.asarray(encoded, dtype=np.float32), 0.0) ** (1.0 / m2)
    return (np.maximum(value - c1, 0.0) / np.maximum(c2 - c3 * value, 1e-7)) ** (1.0 / m1)


def _pq_oetf(linear):
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 32.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 128.0
    c3 = 2392.0 / 128.0
    value = np.maximum(np.asarray(linear, dtype=np.float32), 0.0) ** m1
    return ((c1 + c2 * value) / np.maximum(1.0 + c3 * value, 1e-7)) ** m2


def _hlg_eotf(encoded):
    a = 0.17883277
    b = 1.0 - 4.0 * a
    c = 0.5 - a * np.log(4.0 * a)
    value = np.maximum(np.asarray(encoded, dtype=np.float32), 0.0)
    return np.where(value <= 0.5, value * value / 3.0, (np.exp((value - c) / a) + b) / 12.0)


def _hlg_oetf(linear):
    a = 0.17883277
    b = 1.0 - 4.0 * a
    c = 0.5 - a * np.log(4.0 * a)
    value = np.maximum(np.asarray(linear, dtype=np.float32), 0.0)
    return np.where(
        value <= (1.0 / 12.0), np.sqrt(3.0 * value),
        a * np.log(np.maximum(12.0 * value - b, 1e-7)) + c,
    )


def compose_hdr_frame(original, processed, view=0, mix=1.0, profile="hdr10_pq"):
    """Compose RGBA16F while blending PQ/HLG in linear-light space."""
    original_f = np.asarray(original, dtype=np.float32)
    processed_f = np.asarray(processed, dtype=np.float32)
    width = original_f.shape[1]
    if int(view) == 1:
        result = np.clip(0.5 + (processed_f - original_f) * 10.0, 0.0, 1.0)
        result[..., 3] = 1.0
        return result.astype(np.float16)
    if int(view) == 2:
        result = processed_f.copy()
        result[:, :width // 2] = original_f[:, :width // 2]
        if width > 1:
            result[:, max(width // 2 - 1, 0), :3] = 1.0
        return result.astype(np.float16)
    mix = max(0.0, min(_MAX_OUTPUT_MIX, float(mix)))
    if mix <= 0.0:
        return np.ascontiguousarray(original, dtype=np.float16)
    if mix == 1.0:
        return np.ascontiguousarray(processed, dtype=np.float16)
    decode = _pq_eotf if profile == "hdr10_pq" else _hlg_eotf
    encode = _pq_oetf if profile == "hdr10_pq" else _hlg_oetf
    original_linear = decode(original_f[..., :3])
    processed_linear = decode(processed_f[..., :3])
    linear = np.maximum(
        original_linear + (processed_linear - original_linear) * mix,
        0.0,
    )
    result = np.empty_like(original_f)
    result[..., :3] = np.clip(encode(linear), 0.0, 1.0)
    result[..., 3] = np.clip(
        original_f[..., 3] + (processed_f[..., 3] - original_f[..., 3]) * mix,
        0.0, 1.0,
    )
    return result.astype(np.float16)


def tone_map_hdr_preview(frame_bgr, color_info):
    """Create an SDR preview only; the export path retains the HDR signal."""
    if frame_bgr is None or not (color_info or {}).get("is_hdr"):
        return frame_bgr
    rgb = frame_bgr[..., ::-1].astype(np.float32) / 255.0
    linear = _pq_eotf(rgb) * 100.0 if color_info.get("profile") == "hdr10_pq" else _hlg_eotf(rgb) * 12.0
    if str(color_info.get("color_primaries", "")).startswith("bt2020"):
        matrix = np.array([
            [1.660491, -0.587641, -0.072850],
            [-0.124550, 1.132900, -0.008349],
            [-0.018151, -0.100579, 1.118730],
        ], dtype=np.float32)
        linear = np.einsum("...c,dc->...d", linear, matrix)
    linear = np.maximum(linear, 0.0)
    mapped = np.clip((linear * (2.51 * linear + 0.03)) /
                     np.maximum(linear * (2.43 * linear + 0.59) + 0.14, 1e-7), 0.0, 1.0)
    srgb = np.where(mapped <= 0.0031308, mapped * 12.92,
                    1.055 * np.power(mapped, 1.0 / 2.4) - 0.055)
    return np.ascontiguousarray((np.clip(srgb, 0.0, 1.0)[..., ::-1] * 255.0 + 0.5).astype(np.uint8))


def _zscale_decode_filter(color_info):
    transfer = color_info.get("color_transfer") or "smpte2084"
    primaries = color_info.get("color_primaries") or "bt2020"
    matrix = color_info.get("color_space") or "bt2020nc"
    source_range = "full" if color_info.get("color_range") in {"pc", "jpeg", "full"} else "limited"
    return (
        f"zscale=matrixin={matrix}:matrix={matrix}:transferin={transfer}:transfer={transfer}:"
        f"primariesin={primaries}:primaries={primaries}:rangein={source_range}:range=full,"
        # Let swscale perform the YUV-to-RGB matrix conversion.  zscale rejects
        # RGB matrix coefficients on YUV output negotiation in FFmpeg 7.x.
        "format=gbrp16le,format=rgba64le"
    )


class FFmpegHDRVideoReader:
    """Decode PQ/HLG frames as normalized transfer-coded RGBA16F."""

    def __init__(self, source, width, height, color_info, ffmpeg=None):
        self.source = os.path.abspath(source)
        self.width = int(width)
        self.height = int(height)
        self.color_info = classify_color_info(color_info)
        if not self.color_info["is_hdr"]:
            raise ValueError("HDR reader requires a PQ or HLG source")
        self.ffmpeg = ffmpeg or find_ffmpeg()
        self._frame_bytes = self.width * self.height * 8
        self._stderr = deque(maxlen=100)
        self._proc = subprocess.Popen(
            [
                self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-i", self.source,
                "-map", "0:v:0", "-an", "-sn", "-dn", "-fps_mode", "passthrough",
                "-vf", _zscale_decode_filter(self.color_info),
                "-f", "rawvideo", "-pix_fmt", "rgba64le", "pipe:1",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, creationflags=_CREATE_NO_WINDOW,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self):
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                self._stderr.append(raw.decode("utf-8", errors="replace"))
        except Exception:
            pass

    def read(self):
        data = bytearray(self._frame_bytes)
        view = memoryview(data)
        received = 0
        while received < self._frame_bytes:
            count = self._proc.stdout.readinto(view[received:])
            if not count:
                break
            received += count
        if received == 0:
            code = self._proc.wait()
            self._stderr_thread.join(timeout=2)
            if code != 0:
                raise RuntimeError("FFmpeg HDR 解码失败：\n" + _tail_text(self._stderr))
            return None
        if received != self._frame_bytes:
            raise RuntimeError(f"HDR 视频帧被截断：收到 {received}/{self._frame_bytes} 字节")
        rgba16 = np.frombuffer(data, dtype="<u2").reshape(self.height, self.width, 4)
        return (rgba16.astype(np.float32) / 65535.0).astype(np.float16)

    def close(self):
        if getattr(self, "_proc", None) is None:
            return
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        self._stderr_thread.join(timeout=2)
        self._proc = None


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
    mix = max(0.0, min(_MAX_OUTPUT_MIX, float(mix)))
    if mix == 1.0:
        return processed
    if mix <= 0.0:
        return original
    return cv2.addWeighted(original, 1.0 - mix, processed, mix, 0.0)


def mux_source_audio(ffmpeg, video_path, audio_source, output_path):
    """Atomically attach source audio to an encoded video and return the audio mode."""
    video_path = os.path.abspath(video_path)
    audio_source = os.path.abspath(audio_source)
    output_path = os.path.abspath(output_path)
    output_ext = os.path.splitext(output_path)[1].lower()
    if output_ext not in OUTPUT_CONTAINER_EXTENSIONS.values():
        raise ValueError("视频输出容器仅支持 MP4、MKV 或 MOV")
    audio_codecs = probe_audio_codecs(ffmpeg, audio_source)
    output_dir = os.path.dirname(output_path) or os.getcwd()
    container = output_container_from_path(output_path)
    container_ext = output_container_extension(container)
    prefix = "." + os.path.basename(output_path) + "."
    mux_temp = tempfile.NamedTemporaryFile(
        prefix=prefix, suffix=f".mux.tmp{container_ext}", dir=output_dir, delete=False
    )
    mux_path = mux_temp.name
    mux_temp.close()
    common = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", video_path]
    if audio_codecs == []:
        common.extend(["-map", "0:v:0", "-an"])
    else:
        common.extend(["-i", audio_source, "-map", "0:v:0", "-map", "1:a?"])
    common.extend(["-c:v", "copy"])
    if container in {"mp4", "mov"}:
        common.extend(["-movflags", "+faststart"])
    try:
        if audio_codecs == []:
            result = subprocess.run(
                common + [mux_path],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, creationflags=_CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace")[-4000:]
                raise RuntimeError("FFmpeg 视频封装失败：\n" + detail)
            os.replace(mux_path, output_path)
            return "源视频无音轨"

        copy_audio = (
            container == "mkv"
            or (
                audio_codecs is not None
                and all(codec in _ISO_BMFF_COPY_AUDIO_CODECS for codec in audio_codecs)
            )
        )
        if copy_audio:
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
    """Stream SDR BGR8 or HDR RGBA16F frames to FFmpeg, then attach audio."""

    def __init__(
        self, output_path, width, height, fps, audio_source=None,
        use_nvenc=None, nvenc_preset="p5", hdr_metadata=None,
        rate_control="quality", quality_profile="high",
        video_bitrate_mbps=20.0, output_size=None,
    ):
        self.output_path = os.path.abspath(output_path)
        output_ext = os.path.splitext(self.output_path)[1].lower()
        if output_ext not in OUTPUT_CONTAINER_EXTENSIONS.values():
            raise ValueError("视频输出容器仅支持 MP4、MKV 或 MOV")
        self.output_container = output_container_from_path(self.output_path)
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps)
        self.audio_source = os.path.abspath(audio_source) if audio_source else None
        self.ffmpeg = find_ffmpeg()
        self.hdr_metadata = classify_color_info(hdr_metadata) if hdr_metadata else None
        self.is_hdr = bool(self.hdr_metadata and self.hdr_metadata["is_hdr"])
        if use_nvenc is None:
            self.uses_nvenc = (
                has_hevc_main10_nvenc(self.ffmpeg) if self.is_hdr
                else has_h264_nvenc(self.ffmpeg)
            )
        else:
            self.uses_nvenc = bool(use_nvenc)
        self.nvenc_preset = nvenc_preset if nvenc_preset in {f"p{i}" for i in range(1, 8)} else "p5"
        self.rate_control = rate_control if rate_control in {"quality", "bitrate"} else "quality"
        self.quality_profile = (
            quality_profile if quality_profile in _QUALITY_PROFILE_VALUES else "high"
        )
        self.video_bitrate_mbps = _clamp_video_bitrate(video_bitrate_mbps)
        self.output_width = self.width + self.width % 2
        self.output_height = self.height + self.height % 2
        self._resize_output = False
        if output_size is not None:
            try:
                output_width, output_height = (int(value) for value in output_size)
            except (TypeError, ValueError):
                output_width, output_height = self.width, self.height
            output_width = max(2, output_width - output_width % 2)
            output_height = max(2, output_height - output_height % 2)
            self.output_width, self.output_height = output_width, output_height
            self._resize_output = (output_width, output_height) != (self.width, self.height)
        self.encoder_name = (
            "HEVC Main10 NVENC（HDR）" if self.is_hdr and self.uses_nvenc else
            "libx265 Main10（HDR CPU 回退）" if self.is_hdr else
            "NVIDIA NVENC (GPU)" if self.uses_nvenc else "libx264 (CPU 回退)"
        )
        self._stderr = deque(maxlen=100)
        self._frames = 0
        self._finished = False

        output_dir = os.path.dirname(self.output_path) or os.getcwd()
        prefix = "." + os.path.basename(self.output_path) + "."
        temp = tempfile.NamedTemporaryFile(
            prefix=prefix,
            suffix=f".video.tmp{output_container_extension(self.output_container)}",
            dir=output_dir, delete=False,
        )
        self._temp_path = temp.name
        temp.close()

        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y"]
        if self.is_hdr:
            transfer = self.hdr_metadata["color_transfer"]
            primaries = self.hdr_metadata["color_primaries"]
            matrix = self.hdr_metadata["color_space"]
            geometry = (
                f"zscale=w={self.output_width}:h={self.output_height}:filter=lanczos:"
                if self._resize_output else
                "pad=ceil(iw/2)*2:ceil(ih/2)*2,zscale="
            )
            cmd.extend([
                "-f", "rawvideo", "-pixel_format", "rgba64le",
                "-video_size", f"{self.width}x{self.height}",
                "-framerate", f"{self.fps:.12g}",
                "-color_range", "pc", "-color_primaries", primaries,
                "-color_trc", transfer, "-i", "pipe:0", "-an",
                "-vf",
                f"{geometry}matrixin=gbr:matrix={matrix}:"
                f"transferin={transfer}:transfer={transfer}:primariesin={primaries}:"
                f"primaries={primaries}:rangein=full:range=limited,format=p010le",
            ])
            cmd.extend(build_video_encoder_args(
                True, self.uses_nvenc, self.nvenc_preset, self.rate_control,
                self.quality_profile, self.video_bitrate_mbps,
            ))
            cmd.extend([
                "-color_range", "tv", "-color_primaries", primaries,
                "-color_trc", transfer, "-colorspace", matrix,
            ])
            if self.output_container in {"mp4", "mov"}:
                cmd.extend(["-tag:v", "hvc1"])
        else:
            output_filter = (
                f"scale={self.output_width}:{self.output_height}:flags=lanczos,format=yuv420p"
                if self._resize_output else
                "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p"
            )
            cmd.extend([
                "-f", "rawvideo", "-pixel_format", "bgr24",
                "-video_size", f"{self.width}x{self.height}",
                "-framerate", f"{self.fps:.12g}", "-i", "pipe:0", "-an",
                # H.264 4:2:0 needs even dimensions; padding affects only unusual odd-sized input.
                "-vf", output_filter,
            ])
            cmd.extend(build_video_encoder_args(
                False, self.uses_nvenc, self.nvenc_preset, self.rate_control,
                self.quality_profile, self.video_bitrate_mbps,
            ))
        if self.output_container in {"mp4", "mov"}:
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(self._temp_path)

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
        if self.is_hdr:
            if frame.shape != (self.height, self.width, 4) or frame.dtype not in (np.float16, np.float32):
                raise ValueError(
                    f"HDR 帧应为 {self.width}x{self.height} RGBA float16/32，实际为 {frame.shape}/{frame.dtype}"
                )
            frame = np.rint(
                np.clip(np.asarray(frame, dtype=np.float32), 0.0, 1.0) * 65535.0
            ).astype("<u2")
        elif frame.shape != (self.height, self.width, 3) or frame.dtype != np.uint8:
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
        try:
            self._proc.stderr.close()
        except (AttributeError, OSError):
            pass
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
        if hasattr(self, "_stderr_thread"):
            self._stderr_thread.join(timeout=2)
        try:
            self._proc.stderr.close()
        except (AttributeError, OSError):
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
