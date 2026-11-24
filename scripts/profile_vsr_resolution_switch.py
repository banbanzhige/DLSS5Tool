"""Bounded synthetic benchmark for VSR resolution switching and reconfiguration.

Measures current process-replacement overhead vs in-process VSR reconfiguration,
verifies bit-exact pixel outputs against cold-start baselines, and monitors
process identity and GPU VRAM across many distinct resolutions.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Synthetic manga-like mixed-resolution cases:
# 1) 2x upscaling to ~2560x1440 / 2540x1440 (SDR)
# 2) Scale switch 2x <-> 4x
# 3) HDR 1080p->4K GPU path + HDR smaller CPU path
MANGA_CASES = [
    (1280, 720, 2, False),
    (1280, 720, 2, False),
    (1270, 720, 2, False),
    (1270, 720, 2, False),
    (1280, 720, 2, False),
    (1280, 720, 2, False),
]

MIXED_FEATURE_CASES = [
    (1280, 720, 2, False),
    (1270, 720, 2, False),
    (640, 360, 4, False),
    (630, 360, 4, False),
    (1920, 1080, 2, True),   # GPU HDR conversion route (output 3840x2160)
    (1900, 1080, 2, True),   # GPU HDR conversion route (output 3800x2160)
    (640, 360, 2, True),     # CPU HDR conversion route (output 1280x720 < 1080p)
    (1280, 720, 2, False),   # Back to SDR
]

MULTI_SIZE_CASES = [
    (1280 - i * 8, 720 - (i % 4) * 6, 2, False) for i in range(16)
]


def synthetic_frame(width, height, is_hdr):
    image = np.empty((height, width, 4), np.uint8)
    image[..., 0] = (np.arange(width, dtype=np.uint16)[None, :] * 3) % 256
    image[..., 1] = (np.arange(height, dtype=np.uint16)[:, None] * 5) % 256
    image[..., 2] = ((np.arange(width, dtype=np.uint16)[None, :] + np.arange(height, dtype=np.uint16)[:, None]) * 2) % 256
    image[..., 3] = 255
    if is_hdr:
        return (image.astype(np.float32) / 255.0).astype(np.float16)
    return image


def query_vram():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.free,memory.total", "--format=csv,noheader,nounits"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        ).stdout.decode("utf-8", errors="replace").splitlines()[0]
        name, used, free, total = [part.strip() for part in out.split(",", 3)]
        return {
            "name": name,
            "used_mib": int(float(used)),
            "free_mib": int(float(free)),
            "total_mib": int(float(total)),
        }
    except Exception:
        return None


def sha256_array(array):
    return hashlib.sha256(memoryview(np.ascontiguousarray(array))).hexdigest()


def run_baseline(cases, host_library=None):
    from dlss5tool.super_resolution import ProcessSuperResolution

    session = None
    key = None
    rows = []
    vram_start = query_vram()
    try:
        for index, (w, h, scale, is_hdr) in enumerate(cases):
            new_key = (w, h, scale, is_hdr)
            switch_seconds = 0.0
            close_seconds = 0.0
            init_seconds = 0.0
            switched = new_key != key
            if switched:
                t0 = time.perf_counter()
                if session is not None:
                    session.close()
                    session = None
                close_seconds = time.perf_counter() - t0
                t1 = time.perf_counter()
                session = ProcessSuperResolution(w, h, scale, is_hdr=is_hdr, _host_library=host_library)
                init_seconds = time.perf_counter() - t1
                switch_seconds = time.perf_counter() - t0
                key = new_key
            frame = synthetic_frame(w, h, is_hdr)
            t2 = time.perf_counter()
            out = session.process(frame)
            process_seconds = time.perf_counter() - t2
            rows.append({
                "index": index,
                "width": w, "height": h, "scale": scale, "is_hdr": is_hdr,
                "switched": switched,
                "pid": session._process.pid if session._process else None,
                "hdr_conversion": session.adapter_info.get("hdr_conversion"),
                "close_seconds": round(close_seconds, 4),
                "init_seconds": round(init_seconds, 4),
                "switch_seconds": round(switch_seconds, 4),
                "process_seconds": round(process_seconds, 4),
                "sha256": sha256_array(out),
                "vram": query_vram(),
            })
    finally:
        if session is not None:
            session.close()
    return {"mode": "baseline", "vram_start": vram_start, "vram_end": query_vram(), "rows": rows}


def run_reconfigure(cases, host_library=None):
    from dlss5tool.super_resolution import ProcessSuperResolution

    session = None
    key = None
    rows = []
    vram_start = query_vram()
    try:
        for index, (w, h, scale, is_hdr) in enumerate(cases):
            new_key = (w, h, scale, is_hdr)
            switch_seconds = 0.0
            init_seconds = 0.0
            switched = new_key != key
            if session is None:
                t0 = time.perf_counter()
                session = ProcessSuperResolution(w, h, scale, is_hdr=is_hdr, _host_library=host_library)
                init_seconds = time.perf_counter() - t0
                switch_seconds = init_seconds
                key = new_key
            elif switched:
                t0 = time.perf_counter()
                session.reconfigure(w, h, scale, is_hdr=is_hdr)
                switch_seconds = time.perf_counter() - t0
                key = new_key
            frame = synthetic_frame(w, h, is_hdr)
            t2 = time.perf_counter()
            out = session.process(frame)
            process_seconds = time.perf_counter() - t2
            rows.append({
                "index": index,
                "width": w, "height": h, "scale": scale, "is_hdr": is_hdr,
                "switched": switched,
                "pid": session._process.pid if session._process else None,
                "supports_reconfigure": getattr(session, "supports_reconfigure", False),
                "hdr_conversion": session.adapter_info.get("hdr_conversion"),
                "init_seconds": round(init_seconds, 4),
                "switch_seconds": round(switch_seconds, 4),
                "process_seconds": round(process_seconds, 4),
                "sha256": sha256_array(out),
                "vram": query_vram(),
            })
    finally:
        if session is not None:
            session.close()
    return {"mode": "reconfigure", "vram_start": vram_start, "vram_end": query_vram(), "rows": rows}


def run_combined(cases, host_library=None, use_reconfigure=True):
    from dlss5tool import app_settings, dlss_engine
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.super_resolution import ProcessSuperResolution

    base_settings = dict(
        app_settings.DEFAULTS,
        host_backend="v2", host_auto_fallback=False, guidance_mode=0,
        dlss_runtime=str(ROOT / "runtime/nvngx_dlssnr.dll"),
        host_submission="compatibility", host_in_flight=1,
        host_persistent_buffers=True, host_zero_fast_path=True,
        style=0, intensity=0.5, local_tone=0.5, local_struct=0.5, skin_struct=0.5,
    )
    sr = None
    live = None
    sr_key = None
    nr_key = None
    rows = []
    vram_start = query_vram()
    try:
        for index, (w, h, scale, is_hdr) in enumerate(cases):
            new_sr_key = (w, h, scale, is_hdr)
            t_sr_sw = 0.0
            if sr is None:
                t0 = time.perf_counter()
                sr = ProcessSuperResolution(w, h, scale, is_hdr=is_hdr, _host_library=host_library)
                t_sr_sw = time.perf_counter() - t0
                sr_key = new_sr_key
            elif new_sr_key != sr_key:
                t0 = time.perf_counter()
                if use_reconfigure:
                    sr.reconfigure(w, h, scale, is_hdr=is_hdr)
                else:
                    sr.close()
                    sr = ProcessSuperResolution(w, h, scale, is_hdr=is_hdr, _host_library=host_library)
                t_sr_sw = time.perf_counter() - t0
                sr_key = new_sr_key

            frame = synthetic_frame(w, h, is_hdr)
            t1 = time.perf_counter()
            upscaled = sr.process(frame)
            t_sr_proc = time.perf_counter() - t1

            ow, oh = upscaled.shape[1], upscaled.shape[0]
            nr_settings = dict(
                base_settings,
                frame_format="rgba16f" if is_hdr else "rgba8",
                color_profile="hdr10_pq" if is_hdr else "srgb",
            )
            new_nr_key = (ow, oh, bool(is_hdr))
            t_nr_sw = 0.0
            if live is None:
                t2 = time.perf_counter()
                live = ProcessLive(ow, oh, nr_settings)
                t_nr_sw = time.perf_counter() - t2
                nr_key = new_nr_key
            elif new_nr_key != nr_key:
                t2 = time.perf_counter()
                live.resize(ow, oh, settings=nr_settings)
                t_nr_sw = time.perf_counter() - t2
                nr_key = new_nr_key

            t3 = time.perf_counter()
            enhanced = live.process(upscaled, reset=True)
            t_nr_proc = time.perf_counter() - t3

            rows.append({
                "index": index,
                "input_size": [w, h], "scale": scale, "is_hdr": bool(is_hdr),
                "output_size": [ow, oh],
                "vsr_pid": sr._process.pid if sr._process else None,
                "nr_pid": live._session._process.pid if live._session and live._session._process else None,
                "vsr_switch_seconds": round(t_sr_sw, 4),
                "vsr_process_seconds": round(t_sr_proc, 4),
                "nr_switch_seconds": round(t_nr_sw, 4),
                "nr_process_seconds": round(t_nr_proc, 4),
                "total_seconds": round(t_sr_sw + t_sr_proc + t_nr_sw + t_nr_proc, 4),
                "vsr_sha256": sha256_array(upscaled),
                "nr_sha256": sha256_array(enhanced),
            })
    finally:
        if live is not None:
            live.close()
        if sr is not None:
            sr.close()
        dlss_engine.close_adapter_probe()
    return {
        "mode": "combined-reconfigure" if use_reconfigure else "combined-baseline",
        "vram_start": vram_start, "vram_end": query_vram(), "rows": rows,
    }


def run_native_probe(cases, multi_cases, host_library):
    import ctypes
    dll_dir = os.add_dll_directory(str((ROOT / "runtime").resolve()))
    lib = ctypes.WinDLL(host_library)
    lib.vsr_init.argtypes = [ctypes.c_int] * 5 + [ctypes.c_wchar_p, ctypes.c_wchar_p]
    lib.vsr_init.restype = ctypes.c_int
    lib.vsr_resize.argtypes = [ctypes.c_int] * 5
    lib.vsr_resize.restype = ctypes.c_int
    lib.vsr_process.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.vsr_process.restype = ctypes.c_int
    lib.vsr_shutdown.argtypes = []
    lib.vsr_hdr_gpu_conversion.argtypes = []
    lib.vsr_hdr_gpu_conversion.restype = ctypes.c_int

    log_path = str((ROOT / "tmp/vsr-reconfigure-20261124/native-probe.log").resolve())
    vram_start = query_vram()
    w0, h0, s0, hdr0 = cases[0]
    rows = []
    multi_rows = []
    try:
        t0 = time.perf_counter()
        if lib.vsr_init(w0, h0, s0, 4, int(hdr0), str((ROOT / "runtime").resolve()), log_path) != 1:
            raise RuntimeError("vsr_init failed")
        init_seconds = time.perf_counter() - t0
        for index, (w, h, scale, is_hdr) in enumerate(cases):
            resize_seconds = 0.0
            if index > 0:
                ts = time.perf_counter()
                if lib.vsr_resize(w, h, scale, 4, int(is_hdr)) != 1:
                    raise RuntimeError(f"vsr_resize failed at index {index}")
                resize_seconds = time.perf_counter() - ts
            inp = synthetic_frame(w, h, is_hdr)
            out = np.empty((h * scale, w * scale, 4), dtype=inp.dtype)
            tp = time.perf_counter()
            if lib.vsr_process(inp.ctypes.data_as(ctypes.c_void_p), out.ctypes.data_as(ctypes.c_void_p)) != 1:
                raise RuntimeError(f"vsr_process failed at index {index}")
            process_seconds = time.perf_counter() - tp
            digest = sha256_array(out)
            conv = ("gpu-rgb10a2" if lib.vsr_hdr_gpu_conversion() == 1 else "cpu-rgb10a2") if is_hdr else "direct-rgba8"
            rows.append({
                "index": index, "width": w, "height": h, "scale": scale, "is_hdr": is_hdr,
                "hdr_conversion": conv,
                "resize_seconds": round(resize_seconds, 5),
                "process_seconds": round(process_seconds, 5),
                "sha256": digest,
                "vram": query_vram(),
            })
        for index, (w, h, scale, is_hdr) in enumerate(multi_cases):
            ts = time.perf_counter()
            if lib.vsr_resize(w, h, scale, 4, int(is_hdr)) != 1:
                raise RuntimeError(f"multi vsr_resize failed at index {index}")
            resize_seconds = time.perf_counter() - ts
            inp = synthetic_frame(w, h, is_hdr)
            out = np.empty((h * scale, w * scale, 4), dtype=inp.dtype)
            tp = time.perf_counter()
            if lib.vsr_process(inp.ctypes.data_as(ctypes.c_void_p), out.ctypes.data_as(ctypes.c_void_p)) != 1:
                raise RuntimeError(f"multi vsr_process failed at index {index}")
            process_seconds = time.perf_counter() - tp
            multi_rows.append({
                "index": index, "width": w, "height": h, "scale": scale,
                "resize_seconds": round(resize_seconds, 5),
                "process_seconds": round(process_seconds, 5),
                "vram": query_vram(),
            })
    finally:
        lib.vsr_shutdown()
        dll_dir.close()
    return {
        "mode": "native-probe",
        "init_seconds": round(init_seconds, 4),
        "vram_start": vram_start,
        "vram_end": query_vram(),
        "rows": rows,
        "multi_rows": multi_rows,
    }


def _valid_digest(value):
    return isinstance(value, str) and bool(value.strip())


def apply_reference_comparison(result, reference_rows):
    rows = result.get("rows") or []
    combined = result.get("mode") in ("combined-baseline", "combined-reconfigure")
    if not rows or not isinstance(reference_rows, list) or len(rows) != len(reference_rows):
        for row in rows:
            row["matches_baseline"] = False
        result["all_matched"] = False
        return result
    all_matched = True
    for row, ref in zip(rows, reference_rows):
        if not isinstance(ref, dict):
            matched = False
        elif combined:
            vsr_ref = ref.get("vsr_sha256")
            nr_ref = ref.get("nr_sha256")
            matched = (
                _valid_digest(vsr_ref)
                and _valid_digest(nr_ref)
                and row.get("vsr_sha256") == vsr_ref
                and row.get("nr_sha256") == nr_ref
            )
        else:
            sha_ref = ref.get("sha256")
            matched = _valid_digest(sha_ref) and row.get("sha256") == sha_ref
        row["matches_baseline"] = bool(matched)
        if not matched:
            all_matched = False
    result["all_matched"] = all_matched
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("baseline", "reconfigure", "combined-baseline", "combined-reconfigure", "native-probe"), required=True)
    parser.add_argument("--suite", choices=("manga", "mixed", "multi-size"), default="manga")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--host-library", default=None)
    parser.add_argument("--reference", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    suites = {"manga": MANGA_CASES, "mixed": MIXED_FEATURE_CASES, "multi-size": MULTI_SIZE_CASES}
    cases = suites[args.suite] * max(1, args.cycles)
    host_library = str(Path(args.host_library).resolve()) if args.host_library else None
    reference_rows = None
    if args.reference is not None:
        if not args.reference.is_file():
            raise FileNotFoundError(f"Reference file not found: {args.reference}")
        reference_rows = json.loads(args.reference.read_text(encoding="utf-8")).get("rows")
        if not isinstance(reference_rows, list):
            raise ValueError(f"Invalid reference file (missing rows): {args.reference}")

    if args.case == "baseline":
        result = run_baseline(cases, host_library=host_library)
    elif args.case == "reconfigure":
        result = run_reconfigure(cases, host_library=host_library)
    elif args.case == "combined-baseline":
        result = run_combined(cases, host_library=host_library, use_reconfigure=False)
    elif args.case == "native-probe":
        result = run_native_probe(cases, MULTI_SIZE_CASES, host_library or str((ROOT / "runtime/vsr_host.dll").resolve()))
    else:
        result = run_combined(cases, host_library=host_library, use_reconfigure=True)

    if reference_rows is not None:
        apply_reference_comparison(result, reference_rows)

    result["suite"] = args.suite
    result["cycles"] = args.cycles
    result["host_library"] = host_library or str(ROOT / "runtime/vsr_host.dll")
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    if reference_rows is not None and result.get("all_matched") is not True:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
