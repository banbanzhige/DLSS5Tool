"""Compare production FFmpeg NVENC with a single-buffer native HQ VBR/CQ probe.

This isolates encoder policy and RGB/YUV conversion. It does not attach DLSS,
audio, HDR, B-frames, lookahead, or change application defaults.
"""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.encode_acceptance_fg import resolve_native_root
from scripts.gpu_color_probe import bind


FMT_ABGR, FMT_NV12, FMT_ARGB = 0, 1, 2
POLICY_LEGACY, POLICY_HQ = 0, 1


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def nv12_pitch(width):
    return (int(width) + 255) // 256 * 256


def pack_nv12(raw, width, height, pitch):
    import numpy as np
    expected = width * height * 3 // 2
    if len(raw) != expected:
        raise ValueError(f"NV12 size {len(raw)} != {expected}")
    y = np.frombuffer(raw[: width * height], np.uint8).reshape(height, width)
    uv = np.frombuffer(raw[width * height :], np.uint8).reshape(height // 2, width)
    packed = np.zeros((height + height // 2, pitch), np.uint8)
    packed[:height, :width] = y
    packed[height:, :width] = uv
    return packed


def framemd5_bytes(ffmpeg, payload, demuxer="h264"):
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", demuxer, "-i", "pipe:0",
         "-map", "0:v:0", "-f", "framemd5", "-"],
        input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    rows = [line.split(",")[-1].strip() for line in result.stdout.decode(errors="replace").splitlines()
            if line and not line.startswith("#")]
    return {"returncode": result.returncode, "frames": len(rows), "md5": rows,
            "stderr": result.stderr.decode(errors="replace")[-1000:]}


def framemd5_file(ffmpeg, path):
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-map", "0:v:0", "-f", "framemd5", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    rows = [line.split(",")[-1].strip() for line in result.stdout.decode(errors="replace").splitlines()
            if line and not line.startswith("#")]
    return {"returncode": result.returncode, "frames": len(rows), "md5": rows,
            "stderr": result.stderr.decode(errors="replace")[-1000:]}


def ffmpeg_nv12(ffmpeg, frames_bgr, width, height, fps):
    payload = b"".join(frame.tobytes() for frame in frames_bgr)
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
         "-pixel_format", "bgr24", "-video_size", f"{width}x{height}",
         "-framerate", f"{fps:.12g}", "-i", "pipe:0", "-vf", "format=nv12",
         "-f", "rawvideo", "pipe:1"],
        input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-2000:])
    frame_size = width * height * 3 // 2
    if len(result.stdout) != frame_size * len(frames_bgr):
        raise RuntimeError(f"NV12 bytes {len(result.stdout)} != {frame_size * len(frames_bgr)}")
    return [result.stdout[i * frame_size:(i + 1) * frame_size] for i in range(len(frames_bgr))]


def encode_ffmpeg(path, frames, fps, use_nvenc):
    from dlss5tool.video_export import FFmpegVideoWriter
    height, width = frames[0].shape[:2]
    started = time.perf_counter()
    writer = FFmpegVideoWriter(str(path), width, height, fps, audio_source=None,
                               use_nvenc=use_nvenc, rate_control="quality",
                               quality_profile="high")
    try:
        for frame in frames:
            writer.write(frame)
        writer.finish()
    except Exception:
        writer.abort()
        raise
    return {
        "seconds": round(time.perf_counter() - started, 6),
        "uses_nvenc": writer.uses_nvenc,
        "encoder_name": writer.encoder_name,
        "codec": writer.codec,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def native_encode(encoder, context, pointer, frames, width, height, pitch, fmt, policy, fps):
    open_ex = bind(encoder, "probe_encoder_open_ex",
                   [C.c_void_p, C.c_void_p, C.c_uint, C.c_uint, C.c_uint,
                    C.c_uint, C.c_uint, C.c_uint, C.c_uint, C.c_uint])
    frame_fn = bind(encoder, "probe_encoder_frame",
                    [C.c_uint, C.c_void_p, C.c_uint, C.POINTER(C.c_uint)])
    flush = bind(encoder, "probe_encoder_flush",
                 [C.c_void_p, C.c_uint, C.POINTER(C.c_uint)])
    close = bind(encoder, "probe_encoder_close", [], None)
    error = bind(encoder, "probe_encoder_error", [])
    fps_num, fps_den = int(round(fps * 1000)), 1000
    packet = C.create_string_buffer(8 * 1024 * 1024)
    packets = []
    started = time.perf_counter()
    if not open_ex(context, C.c_void_p(pointer.value), width, height, pitch,
                   fmt, policy, fps_num, fps_den, 19):
        raise RuntimeError(f"NVENC open_ex failed {error()}")
    try:
        h2d = bind(C.WinDLL("nvcuda.dll"), "cuMemcpyHtoD_v2",
                   [C.c_uint64, C.c_void_p, C.c_size_t])
        for index, frame in enumerate(frames):
            status = h2d(pointer.value, frame.ctypes.data, frame.nbytes)
            if status:
                raise RuntimeError(f"cuMemcpyHtoD {status}")
            size = C.c_uint()
            if not frame_fn(index, packet, len(packet), C.byref(size)):
                raise RuntimeError(f"NVENC frame {index} error {error()}")
            if size.value:
                packets.append(packet.raw[:size.value])
        size = C.c_uint()
        if not flush(packet, len(packet), C.byref(size)):
            raise RuntimeError(f"NVENC flush error {error()}")
        if size.value:
            packets.append(packet.raw[:size.value])
    finally:
        close()
    encoded = b"".join(packets)
    return {
        "seconds": round(time.perf_counter() - started, 6),
        "packets": len(packets),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "encoded": encoded,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--width", type=int, default=360)
    parser.add_argument("--height", type=int, default=640)
    args = parser.parse_args()
    args.work = args.work.resolve()
    if not (args.work / "TASK.md").is_file():
        parser.error("registered --work/TASK.md required")
    if not 8 <= args.frames <= 48 or args.width * args.height > 360 * 640:
        parser.error("bounded 8..48 frames at <=360x640")
    os.environ["TEMP"] = os.environ["TMP"] = str(args.work)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    import cv2
    import numpy as np
    import torch
    from dlss5tool.video_export import build_video_encoder_args, find_ffmpeg

    native = resolve_native_root(args.work, args.native_root)
    ffmpeg = find_ffmpeg()
    cap = cv2.VideoCapture(str(args.source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames = []
    while len(frames) < args.frames:
        ok, bgr = cap.read()
        if not ok:
            break
        frames.append(np.ascontiguousarray(
            cv2.resize(bgr, (args.width, args.height), interpolation=cv2.INTER_AREA), np.uint8))
    cap.release()
    if len(frames) != args.frames:
        raise RuntimeError("source too short")

    report = {
        "scope": __doc__,
        "source": str(args.source),
        "native_root": str(native),
        "dimensions": [args.width, args.height],
        "frames": args.frames,
        "fps": fps,
        "production_nvenc_args": build_video_encoder_args(
            False, True, "p5", "quality", "high", 20.0),
        "native_policy": "H264 P5 HQ VBR CQ19, no B/no lookahead, single CUDA pointer",
        "limitations": [
            "Does not match FFmpeg HQ lookahead or B-frames; those need an input ring.",
            "ABGR/ARGB use NVENC's own RGB conversion, not FFmpeg format=yuv420p.",
            "NV12 path uses FFmpeg's conversion then native encode.",
        ],
        "cases": {},
    }
    dest = args.work / "nvenc-contract.json"
    try:
        gpu_mp4 = args.work / "contract-ffmpeg-nvenc.mp4"
        cpu_mp4 = args.work / "contract-ffmpeg-x264.mp4"
        report["cases"]["ffmpeg_nvenc"] = encode_ffmpeg(gpu_mp4, frames, fps, True)
        report["cases"]["ffmpeg_nvenc"]["decode"] = framemd5_file(ffmpeg, gpu_mp4)
        report["cases"]["ffmpeg_x264"] = encode_ffmpeg(cpu_mp4, frames, fps, False)
        report["cases"]["ffmpeg_x264"]["decode"] = framemd5_file(ffmpeg, cpu_mp4)

        torch.empty(0, device="cuda")
        cuda = C.WinDLL("nvcuda.dll")
        context = C.c_void_p()
        check = bind(cuda, "cuCtxGetCurrent", [C.POINTER(C.c_void_p)])
        if check(C.byref(context)):
            raise RuntimeError("no CUDA context")
        alloc = bind(cuda, "cuMemAlloc_v2", [C.POINTER(C.c_uint64), C.c_size_t])
        free = bind(cuda, "cuMemFree_v2", [C.c_uint64])
        encoder = C.WinDLL(str(native / "native-encoder" / "candidate.dll"))
        pitch = nv12_pitch(args.width)
        nv12_size = pitch * (args.height + args.height // 2)
        bgra_size = args.width * args.height * 4
        pointer = C.c_uint64()
        if alloc(C.byref(pointer), max(nv12_size, bgra_size)):
            raise RuntimeError("cuMemAlloc failed")
        try:
            nv12_frames = [np.ascontiguousarray(pack_nv12(blob, args.width, args.height, pitch))
                           for blob in ffmpeg_nv12(ffmpeg, frames, args.width, args.height, fps)]
            bgra = [np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)) for frame in frames]
            native_nv12 = native_encode(encoder, context, pointer, nv12_frames,
                                        args.width, args.height, pitch, FMT_NV12, POLICY_HQ, fps)
            encoded_nv12 = native_nv12.pop("encoded")
            native_nv12["decode"] = framemd5_bytes(ffmpeg, encoded_nv12)
            report["cases"]["native_hq_nv12"] = native_nv12
            native_bgra = native_encode(encoder, context, pointer, bgra,
                                        args.width, args.height, args.width * 4, FMT_ABGR, POLICY_HQ, fps)
            encoded_bgra = native_bgra.pop("encoded")
            native_bgra["decode"] = framemd5_bytes(ffmpeg, encoded_bgra)
            report["cases"]["native_hq_abgr"] = native_bgra
        finally:
            if pointer.value:
                free(pointer)

        ffmpeg_md5 = report["cases"]["ffmpeg_nvenc"]["decode"]["md5"]
        report["gates"] = {
            "ffmpeg_nvenc_used_gpu": report["cases"]["ffmpeg_nvenc"]["uses_nvenc"] is True,
            "ffmpeg_nvenc_frame_count": report["cases"]["ffmpeg_nvenc"]["decode"]["frames"] == args.frames,
            "native_nv12_frame_count": report["cases"]["native_hq_nv12"]["decode"]["frames"] == args.frames,
            "native_abgr_frame_count": report["cases"]["native_hq_abgr"]["decode"]["frames"] == args.frames,
            "native_nv12_matches_ffmpeg_nvenc": report["cases"]["native_hq_nv12"]["decode"]["md5"] == ffmpeg_md5,
            "native_abgr_matches_ffmpeg_nvenc": report["cases"]["native_hq_abgr"]["decode"]["md5"] == ffmpeg_md5,
            "native_nv12_matches_abgr": (
                report["cases"]["native_hq_nv12"]["decode"]["md5"]
                == report["cases"]["native_hq_abgr"]["decode"]["md5"]
            ),
        }
        report["status"] = "measurements_completed"
        report["promotion_gate"] = (
            "pass" if report["gates"]["native_nv12_matches_ffmpeg_nvenc"] else "reject"
        )
    except Exception as exc:
        report.update(status="failed", error=repr(exc))
        import traceback
        report["traceback"] = traceback.format_exc()[-4000:]
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(dest),
                      "promotion_gate": report.get("promotion_gate"),
                      "gates": report.get("gates")}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if report.get("status") == "measurements_completed" else 1)


if __name__ == "__main__":
    main()
