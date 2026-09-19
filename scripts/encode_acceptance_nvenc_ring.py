"""HQ ring-buffer NVENC experiment. Does not change application defaults.

Compares production FFmpegVideoWriter, FFmpeg encoding of the same NV12, and a
native P5 HQ VBR/CQ ring that keeps preset B-frames/lookahead.
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

from scripts.encode_acceptance_nvenc_contract import (
    encode_ffmpeg, framemd5_bytes, framemd5_file, sha256_file,
)
from scripts.gpu_color_probe import bind


def surface_count(frame_interval_p, lookahead_depth):
    needed = max(4, int(frame_interval_p) * 4)
    if int(lookahead_depth) > 0:
        needed = max(needed, int(lookahead_depth) + int(frame_interval_p) + 1 + 4)
    return max(4, min(32, needed))


def ffmpeg_raw(ffmpeg, frames_bgr, width, height, fps, pix_fmt):
    payload = b"".join(frame.tobytes() for frame in frames_bgr)
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
         "-pixel_format", "bgr24", "-video_size", f"{width}x{height}",
         "-framerate", f"{fps:.12g}", "-i", "pipe:0", "-vf", f"format={pix_fmt}",
         "-f", "rawvideo", "pipe:1"],
        input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-2000:])
    frame_size = width * height * 3 // 2
    if len(result.stdout) != frame_size * len(frames_bgr):
        raise RuntimeError(f"{pix_fmt} bytes {len(result.stdout)} != {frame_size * len(frames_bgr)}")
    return [result.stdout[i * frame_size:(i + 1) * frame_size] for i in range(len(frames_bgr))]


def nv12_to_yuv420p(blob, width, height):
    y = blob[: width * height]
    uv = blob[width * height :]
    u = uv[0::2]
    v = uv[1::2]
    return y + u + v


def pack_yv12(blob, width, height, pitch):
    import numpy as np
    luma = width * height
    chroma = luma // 4
    y = np.frombuffer(blob[:luma], np.uint8).reshape(height, width)
    u = np.frombuffer(blob[luma:luma + chroma], np.uint8).reshape(height // 2, width // 2)
    v = np.frombuffer(blob[luma + chroma:], np.uint8).reshape(height // 2, width // 2)
    chroma_pitch = pitch // 2
    out = np.zeros(pitch * height + chroma_pitch * (height // 2) * 2, np.uint8)
    y_plane = out[:pitch * height].reshape(height, pitch)
    v_plane = out[pitch * height:pitch * height + chroma_pitch * (height // 2)].reshape(height // 2, chroma_pitch)
    u_plane = out[pitch * height + chroma_pitch * (height // 2):].reshape(height // 2, chroma_pitch)
    y_plane[:, :width] = y
    v_plane[:, :width // 2] = v
    u_plane[:, :width // 2] = u
    return out


def yuv420p_to_nv12(blob, width, height):
    luma = width * height
    chroma = luma // 4
    y = blob[:luma]
    u = blob[luma:luma + chroma]
    v = blob[luma + chroma:]
    uv = bytearray(chroma * 2)
    uv[0::2] = u
    uv[1::2] = v
    return y + bytes(uv)


def encode_ffmpeg_raw(ffmpeg, path, payloads, width, height, fps, pix_fmt):
    payload = b"".join(payloads)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pixel_format", pix_fmt,
        "-video_size", f"{width}x{height}", "-framerate", f"{fps:.12g}",
        "-i", "pipe:0", "-an",
        "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
        "-rc", "vbr", "-cq", "19", "-b:v", "0",
        "-movflags", "+faststart", str(path),
    ]
    started = time.perf_counter()
    result = subprocess.run(
        cmd, input=payload, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-2000:])
    return {
        "seconds": round(time.perf_counter() - started, 6),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def pop_all(pop, packet, capacity):
    packets = []
    size = C.c_uint()
    while True:
        if not pop(packet, capacity, C.byref(size)):
            raise RuntimeError("ring_pop failed")
        if not size.value:
            break
        packets.append(packet.raw[:size.value])
    return packets


def native_ring_encode(dll, context, packed, width, height, fps, fmt=2, *, codec=0, cq=19, preset=5, device_input=False):
    open_fn = bind(dll, "ring_open",
                   [C.c_void_p, C.c_uint, C.c_uint, C.c_uint, C.c_uint, C.c_uint, C.c_uint,
                    C.POINTER(C.c_uint), C.POINTER(C.c_uint)])
    feed = bind(dll, "ring_feed", [C.c_void_p, C.c_uint, C.c_uint])
    pop = bind(dll, "ring_pop", [C.c_void_p, C.c_uint, C.POINTER(C.c_uint)])
    flush = bind(dll, "ring_flush", [])
    close = bind(dll, "ring_close", [], None)
    error = bind(dll, "ring_error", [])
    info_fn = dll.ring_info
    info_fn.restype = C.c_char_p
    pitch = C.c_uint(); slots = C.c_uint()
    fps_num, fps_den = int(round(fps * 1000)), 1000
    started = time.perf_counter()
    if hasattr(dll,'ring_enable_cuda_input'):
        if not bind(dll,'ring_enable_cuda_input',[C.c_int])(int(device_input)):
            raise RuntimeError('ring_enable_cuda_input rejected')
    elif device_input:raise RuntimeError('No CUDA input support')
    if hasattr(dll,'ring_open_config'):
        configure=bind(dll,'ring_open_config',[C.c_void_p]+[C.c_uint]*7+[C.POINTER(C.c_uint),C.POINTER(C.c_uint)])
        opened=configure(context,width,height,fps_num,fps_den,cq,preset,codec,C.byref(pitch),C.byref(slots))
    else:
        if codec or preset!=5:raise RuntimeError('Old encoder does not support the requested contract')
        opened=open_fn(context,width,height,fps_num,fps_den,cq,fmt,C.byref(pitch),C.byref(slots))
    if not opened:
        raise RuntimeError(f"ring_open failed {error()}")
    info = {}
    try:
        raw = info_fn()
        if raw:
            info = json.loads(raw.decode("utf-8"))
        if not device_input and not packed[0].flags.c_contiguous:
            raise RuntimeError("packed frame is not contiguous")
        if device_input:
            feed=bind(dll,'ring_feed_device',[C.c_uint64,C.c_uint,C.c_uint])
        packet = C.create_string_buffer(8 * 1024 * 1024)
        header_size = C.c_uint()
        header = b''
        if hasattr(dll, 'ring_headers'):
            get_headers = bind(dll,'ring_headers',[C.c_void_p,C.c_uint,C.POINTER(C.c_uint)])
            if not get_headers(packet,len(packet),C.byref(header_size)):
                raise RuntimeError('ring_headers failed')
            header = packet.raw[:header_size.value]
        packets = []
        for index, frame in enumerate(packed):
            ptr=frame.data_ptr() if device_input else frame.ctypes.data
            size=frame.numel()*frame.element_size() if device_input else frame.nbytes
            if not feed(ptr, size, index):
                raise RuntimeError(f"ring_feed {index} error {error()}")
            packets.extend(pop_all(pop, packet, len(packet)))
        if not flush():
            raise RuntimeError(f"ring_flush error {error()}")
        packets.extend(pop_all(pop, packet, len(packet)))
    finally:
        close()
    encoded = header + b"".join(packets)
    return {
        "seconds": round(time.perf_counter() - started, 6),
        "packets": len(packets),
        "bytes": len(encoded),
        "global_header_bytes": len(header),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "preset": info,
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
    if any((args.work / name).exists() for name in ('ring-hq.json','ring-ffmpeg-bgr.mp4','ring-ffmpeg-nv12.mp4','ring-ffmpeg-yuv420p.mp4')):
        parser.error('Refusing to overwrite evidence; use a fresh registered work directory')
    os.environ["TEMP"] = os.environ["TMP"] = str(args.work)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    import cv2
    import numpy as np
    import torch
    from dlss5tool.video_export import build_video_encoder_args, find_ffmpeg

    ffmpeg = find_ffmpeg()
    dll_path = (args.native_root or args.work).resolve() / "native-encoder" / "ring.dll"
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

    dest = args.work / "ring-hq.json"
    report = {
        "scope": __doc__,
        "source": str(args.source),
        "dimensions": [args.width, args.height],
        "frames": args.frames,
        "fps": fps,
        "production_nvenc_args": build_video_encoder_args(False, True, "p5", "quality", "high", 20.0),
        "native_policy": "P5 HQ preset GOP/B + VBR CQ19 initialQP26; sysmem YV12 from production yuv420p",
        "cases": {},
        "argv": sys.argv,
        "script_sha256": sha256_file(Path(__file__)),
        "native_source_sha256": sha256_file(ROOT / 'scripts/gpu_nvenc_ring.cpp'),
        "native_binary_sha256": sha256_file(dll_path),
    }
    try:
        bgr_mp4 = args.work / "ring-ffmpeg-bgr.mp4"
        nv12_mp4 = args.work / "ring-ffmpeg-nv12.mp4"
        report["cases"]["ffmpeg_bgr"] = encode_ffmpeg(bgr_mp4, frames, fps, True)
        report["cases"]["ffmpeg_bgr"]["decode"] = framemd5_file(ffmpeg, bgr_mp4)

        raw_nv12 = ffmpeg_raw(ffmpeg, frames, args.width, args.height, fps, "nv12")
        raw_i420 = ffmpeg_raw(ffmpeg, frames, args.width, args.height, fps, "yuv420p")
        converted = [nv12_to_yuv420p(blob, args.width, args.height) for blob in raw_nv12]
        report["yuv_identity"] = {
            "nv12_to_i420_matches_format_yuv420p": converted == raw_i420,
            "mismatch_frames": sum(a != b for a, b in zip(converted, raw_i420)),
        }
        torch.empty(0, device="cuda")
        cuda = C.WinDLL("nvcuda.dll")
        context = C.c_void_p()
        if bind(cuda, "cuCtxGetCurrent", [C.POINTER(C.c_void_p)])(C.byref(context)):
            raise RuntimeError("no CUDA context")
        dll = C.WinDLL(str(dll_path))
        packed = [np.frombuffer(blob, np.uint8).copy() for blob in raw_i420]
        report["cases"]["ffmpeg_nv12"] = encode_ffmpeg_raw(
            ffmpeg, nv12_mp4, raw_nv12, args.width, args.height, fps, "nv12")
        report["cases"]["ffmpeg_nv12"]["decode"] = framemd5_file(ffmpeg, nv12_mp4)
        i420_mp4 = args.work / "ring-ffmpeg-yuv420p.mp4"
        report["cases"]["ffmpeg_yuv420p"] = encode_ffmpeg_raw(
            ffmpeg, i420_mp4, raw_i420, args.width, args.height, fps, "yuv420p")
        report["cases"]["ffmpeg_yuv420p"]["decode"] = framemd5_file(ffmpeg, i420_mp4)

        native = native_ring_encode(dll, context, packed, args.width, args.height, fps, fmt=2)
        encoded = native.pop("encoded")
        (args.work/'ring-native.h264').write_bytes(encoded)
        native["decode"] = framemd5_bytes(ffmpeg, encoded)
        report["cases"]["native_ring"] = native
        report["surface_formula"] = surface_count(
            native.get("preset", {}).get("frameIntervalP", 1),
            native.get("preset", {}).get("lookaheadDepth", 0),
        )
        bgr_md5 = report["cases"]["ffmpeg_bgr"]["decode"]["md5"]
        nv12_md5 = report["cases"]["ffmpeg_nv12"]["decode"]["md5"]
        i420_md5 = report["cases"]["ffmpeg_yuv420p"]["decode"]["md5"]
        ring_md5 = report["cases"]["native_ring"]["decode"]["md5"]
        report["gates"] = {
            "ffmpeg_bgr_used_gpu": report["cases"]["ffmpeg_bgr"]["uses_nvenc"] is True,
            "ffmpeg_bgr_frames": report["cases"]["ffmpeg_bgr"]["decode"]["frames"] == args.frames,
            "ffmpeg_nv12_frames": report["cases"]["ffmpeg_nv12"]["decode"]["frames"] == args.frames,
            "ffmpeg_yuv420p_frames": report["cases"]["ffmpeg_yuv420p"]["decode"]["frames"] == args.frames,
            "native_frames": report["cases"]["native_ring"]["decode"]["frames"] == args.frames,
            "yuv_planes_match": report["yuv_identity"]["nv12_to_i420_matches_format_yuv420p"],
            "ffmpeg_nv12_matches_bgr": nv12_md5 == bgr_md5,
            "ffmpeg_yuv420p_matches_bgr": i420_md5 == bgr_md5,
            "ffmpeg_nv12_matches_yuv420p": nv12_md5 == i420_md5,
            "native_matches_ffmpeg_nv12": ring_md5 == nv12_md5,
            "native_matches_ffmpeg_yuv420p": ring_md5 == i420_md5,
            "native_matches_ffmpeg_bgr": ring_md5 == bgr_md5,
        }
        report["status"] = "measurements_completed"
        if report["gates"]["native_matches_ffmpeg_bgr"]:
            report["promotion_gate"] = "pass"
        elif report["gates"]["native_matches_ffmpeg_nv12"]:
            report["promotion_gate"] = "encoder-ok-color-gap"
        else:
            report["promotion_gate"] = "reject"
    except Exception as exc:
        report.update(status="failed", error=repr(exc))
        import traceback
        report["traceback"] = traceback.format_exc()[-4000:]
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(dest),
                      "promotion_gate": report.get("promotion_gate"),
                      "gates": report.get("gates"),
                      "preset": report.get("cases", {}).get("native_ring", {}).get("preset")},
                     ensure_ascii=False), flush=True)
    raise SystemExit(0 if report.get("status") == "measurements_completed" else 1)


if __name__ == "__main__":
    main()
