#!/usr/bin/env python3
"""Isolated Feature 18 guidance sensitivity probe; never changes app defaults.

Each case runs in a fresh process. Generated sequences have analytically known
current-to-previous flow, so a missing optical-flow model is not a confounder.
Synthetic depth tests input sensitivity, NOT depth accuracy or visual quality.
"""
import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(array):
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare(args):
    dest = args.output.resolve()
    dest.mkdir(parents=True, exist_ok=False)
    source = cv2.imread(str(args.image))
    if source is None:
        raise ValueError(f"Cannot read {args.image}")
    w, h = args.width, args.height
    source = cv2.resize(source, (w, h), interpolation=cv2.INTER_AREA)
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    frames, flows, masks = [], [], []
    prev_matrix = None
    for i in range(args.frames):
        # Rotation/zoom make inverse flow different from simply negating forward
        # flow; translation supplies a substantial nonzero motion signal.
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), i * 0.35, 1 + i * 0.006)
        matrix[:, 2] += (i * 2.5, i * 0.6)
        affine = np.vstack((matrix, [0, 0, 1]))
        image = cv2.warpAffine(source, matrix, (w, h), borderMode=cv2.BORDER_REFLECT_101)
        flow = np.zeros((h, w, 2), np.float32)
        mask = np.zeros((h, w), bool)
        if prev_matrix is not None:
            backward = prev_matrix @ np.linalg.inv(affine)
            px = backward[0, 0] * xx + backward[0, 1] * yy + backward[0, 2]
            py = backward[1, 0] * xx + backward[1, 1] * yy + backward[1, 2]
            flow = np.stack((px - xx, py - yy), -1).astype(np.float32)
            # Exclude padded borders when measuring temporal residuals.
            mask = (px > 24) & (px < w - 25) & (py > 24) & (py < h - 25)
            mask &= (xx > 24) & (xx < w - 25) & (yy > 24) & (yy < h - 25)
            inverse = np.linalg.inv(affine)
            sx = inverse[0, 0] * xx + inverse[0, 1] * yy + inverse[0, 2]
            sy = inverse[1, 0] * xx + inverse[1, 1] * yy + inverse[1, 2]
            mask &= (sx > 2) & (sx < w - 3) & (sy > 2) & (sy < h - 3)
        frames.append(cv2.cvtColor(image, cv2.COLOR_BGR2RGBA))
        flows.append(flow)
        masks.append(mask)
        prev_matrix = affine
    # A spatially varying bounded probe, intentionally not called real depth.
    depth = np.repeat(((xx / max(w - 1, 1) + yy / max(h - 1, 1)) * 0.5)[None], args.frames, axis=0)
    frames, flows = np.array(frames), np.array(flows)
    np.savez_compressed(dest / "inputs.npz", frames=frames, flow=flows,
                        depth=depth.astype(np.float32), masks=np.array(masks))
    cv2.imwrite(str(dest / "source-preview.jpg"), source)
    writer = cv2.VideoWriter(str(dest / "input.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 12, (w, h))
    if not writer.isOpened():
        raise RuntimeError("Cannot create preview video")
    for frame in frames:
        writer.write(cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR))
    writer.release()
    save_json(dest / "input.json", {"source": str(args.image.resolve()), "width": w,
        "height": h, "frames": args.frames, "input_sha256": digest(frames),
        "flow_abs_mean": float(np.abs(flows[1:]).mean()), "flow_abs_max": float(np.abs(flows).max()),
        "description": "Synthetic affine motion of a source image; exact backward optical flow. Depth is synthetic.",
        "flow_validity": "Border-reflected areas are not a true scene motion field; exclude borders in metrics."})
    print(dest, flush=True)


CASES = {
    "zero_fast": (0, "zero", "zero"),
    "zero": (0, "zero", "zero"),
    "repeat": (0, "zero", "zero"),
    "flow_zero": (1, "zero", "zero"),
    "flow_exact": (1, "exact", "zero"),
    "flow_wrong": (1, "wrong", "zero"),
    "flow_extreme": (1, "extreme", "zero"),
    "raft_original": (1, "raft_original", "zero"),
    "raft_corrected": (1, "raft_corrected", "zero"),
    "depth": (2, "zero", "gradient"),
    "depth_inverted": (2, "zero", "inverse"),
    "both": (3, "exact", "gradient"),
    "reset_each": (0, "zero", "zero"),
    "style_control": (0, "zero", "zero"),
}


def run_case(args):
    import dlss_engine as engine
    dest = args.output.resolve()
    case_dir = dest / f"{args.backend}-{args.case}"
    case_dir.mkdir(exist_ok=False)
    data = np.load(dest / "inputs.npz")
    frames, flows, depths = data["frames"], data["flow"], data["depth"]
    h, w = frames.shape[1:3]
    mode, flow_kind, depth_kind = CASES[args.case]
    raft_flows = None
    if flow_kind.startswith("raft_"):
        with np.load(dest / "raft.npz") as raft:
            raft_flows = raft[flow_kind]
        if raft_flows.shape != flows.shape or not np.isfinite(raft_flows).all():
            raise ValueError("RAFT cache shape/finite-value mismatch")
    settings = {"host_zero_fast_path": args.case == "zero_fast", "host_persistent_buffers": True,
                "host_submission": args.submission, "host_in_flight": 1,
                "host_auto_fallback": False}
    lib, backend = engine._load_backend(args.backend)
    engine._configure_host(lib, settings)
    style = 1 if args.case == "style_control" else 0
    lib.dlssnr_set_options(1, style, 1.0, 1.0, 1.0, 0.5, 0, 0, mode, 2, 1.0, 1.0)
    started = time.perf_counter()
    print(f"{backend}/{args.case}: initializing", flush=True)
    if not lib.dlssnr_init(w, h, 1, engine.DLSSNR_DLL, str(case_dir / "ngx.log")):
        raise RuntimeError("NGX init failed; inspect case log")
    results, times, input_hashes = [], [], []
    completed = False
    try:
        print(f"{backend}/{args.case}: creating feature", flush=True)
        if not lib.dlssnr_create_feature(w, h, 1):
            raise RuntimeError("Feature creation failed; inspect case log")
        setup = time.perf_counter() - started
        print(f"{backend}/{args.case}: processing {len(frames)} frames", flush=True)
        for i, rgba in enumerate(frames):
            motion = np.zeros((h, w, 2), np.float32)
            depth = np.zeros((h, w), np.float32)
            if flow_kind == "exact":
                motion[:] = flows[i]
            elif flow_kind == "wrong":
                motion[:] = -flows[i]
            elif flow_kind == "extreme":
                motion[:] = (64.0, -32.0) if i else (0.0, 0.0)
            elif raft_flows is not None:
                motion[:] = raft_flows[i]
            if depth_kind == "gradient":
                depth[:] = depths[i]
            elif depth_kind == "inverse":
                depth[:] = 1.0 - depths[i]
            out = np.empty_like(rgba)
            reset = i == 0 or args.case == "reset_each"
            begin = time.perf_counter()
            ok = lib.dlssnr_process(rgba.ctypes.data_as(ctypes.c_void_p),
                motion.ctypes.data_as(ctypes.c_void_p), depth.ctypes.data_as(ctypes.c_void_p),
                out.ctypes.data_as(ctypes.c_void_p), int(reset))
            times.append(time.perf_counter() - begin)
            if not ok:
                raise RuntimeError(f"Frame {i} failed")
            print(f"{backend}/{args.case}: frame {i + 1}/{len(frames)} ({times[-1]:.3f}s)", flush=True)
            results.append(out)
            input_hashes.append({"motion": digest(motion), "depth": digest(depth), "reset": reset})
        # Persist completed renders BEFORE teardown, so native shutdown failures
        # cannot destroy otherwise valid A/B evidence.
        results = np.array(results)
        np.save(case_dir / "frames.npy", results)
        save_json(case_dir / "result.json", {"case": args.case, "backend": backend, "mode": mode,
            "zero_fast_path": settings["host_zero_fast_path"], "submission": args.submission,
            "frame_count": len(results), "setup_seconds": setup,
            "frame_seconds": times, "fps_excluding_first": (len(times) - 1) / sum(times[1:]),
            "frame_sha256": [digest(x) for x in results], "combined_sha256": digest(results),
            "guidance_inputs": input_hashes,
            "host_sha256": hashlib.sha256(Path(engine.HOST_DLL_V2 if backend == "v2" else engine.HOST_DLL_LEGACY).read_bytes()).hexdigest(),
            "runtime_sha256": hashlib.sha256(Path(engine.DLSSNR_DLL).read_bytes()).hexdigest()})
        print(f"{backend}/{args.case}: {digest(results)}; shutting down", flush=True)
        completed = True
    finally:
        if completed and backend == "v2":
            # This probe is one feature per child process. Explicit native v2
            # shutdown hung after all frames completed on the test machine.
            # Leave its diagnosis separate; OS process cleanup releases handles.
            save_json(case_dir / "shutdown.json", {"ok": True, "method": "process_exit",
                "native_shutdown_tested": False, "reason": "Avoid independently observed native teardown hang"})
            os._exit(0)
        lib.dlssnr_shutdown()
    save_json(case_dir / "shutdown.json", {"ok": True, "method": "native"})


def suite(args):
    for backend in args.backends:
        for case in args.cases:
            if (args.output / f"{backend}-{case}").exists():
                raise FileExistsError(f"Refusing to overwrite {backend}-{case}; use a new experiment directory")
    for backend in args.backends:
        for case in args.cases:
            cmd = [sys.executable, str(Path(__file__).resolve()), "case", "--output", str(args.output),
                   "--backend", backend, "--case", case, "--submission", args.submission]
            try:
                subprocess.run(cmd, check=True, timeout=args.timeout)
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
                save_json(args.output / f"{backend}-{case}" / "failure.json",
                          {"error": str(error), "timeout": args.timeout})
                print(f"{backend}/{case}: FAILED {error}", flush=True)
                if not args.keep_going:
                    raise


def prepare_raft(args):
    import torch
    import torchvision
    from torchvision.models.optical_flow import Raft_Large_Weights, raft_large
    dest = args.output.resolve()
    if (dest / "raft.npz").exists():
        raise FileExistsError("Refusing to overwrite RAFT cache")
    torch.set_num_threads(args.threads)
    with np.load(dest / "inputs.npz") as data:
        frames, exact = data["frames"], data["flow"]
    h, w = frames.shape[1:3]
    scale = min(1.0, args.edge / min(h, w))
    fw = max(8, round(w * scale / 8) * 8)
    fh = max(8, round(h * scale / 8) * 8)
    model = raft_large(weights=None).eval()
    model.load_state_dict(torch.load(args.weights, map_location="cpu", weights_only=True))
    transforms = Raft_Large_Weights.DEFAULT.transforms()
    original = np.zeros_like(exact)
    corrected = np.zeros_like(exact)
    times = []
    for i in range(1, len(frames)):
        tensors = []
        for frame in frames[i - 1:i + 1]:
            rgb = cv2.resize(frame[..., :3], (fw, fh)) if (fw, fh) != (w, h) else frame[..., :3]
            tensors.append(torch.from_numpy(np.ascontiguousarray(rgb)).permute(2, 0, 1).float().unsqueeze(0) / 255.0)
        previous, current = transforms(*tensors)
        start = time.perf_counter()
        with torch.inference_mode():
            forward = model(previous, current, num_flow_updates=6)[-1]
            backward = model(current, previous, num_flow_updates=6)[-1]
        times.append(time.perf_counter() - start)
        for target, tensor in ((original, -forward), (corrected, backward)):
            flow = tensor[0].permute(1, 2, 0).numpy()
            if (fw, fh) != (w, h):
                flow = cv2.resize(flow, (w, h), interpolation=cv2.INTER_LINEAR)
                flow[..., 0] *= w / fw
                flow[..., 1] *= h / fh
            target[i] = flow
        print(f"RAFT pair {i}/{len(frames)-1}: {times[-1]:.2f}s (two directions)", flush=True)
    np.savez_compressed(dest / "raft.npz", raft_original=original, raft_corrected=corrected)
    save_json(dest / "raft.json", {"torch": torch.__version__, "torchvision": torchvision.__version__,
        "device": "cpu", "threads": args.threads, "model": "raft_large", "updates": 6,
        "weights": str(args.weights.resolve()), "weights_sha256": hashlib.sha256(args.weights.read_bytes()).hexdigest(),
        "fed_width": fw, "fed_height": fh, "pair_seconds_two_directions": times,
        "note": "Same bundled weights and 6 iterations as fork; CPU float32, not its CUDA AMP. Input is synthetic motion."})


def differences(reference, test):
    d = np.abs(test[..., :3].astype(np.int16) - reference[..., :3].astype(np.int16))
    return {"exact_equal": bool(np.array_equal(reference, test)), "rgb_mae_255": float(d.mean()),
            "rgb_max_255": int(d.max()), "changed_rgb_fraction": float(np.count_nonzero(d) / d.size)}


def temporal_residual(frames, source, flow, masks):
    # Warp the enhancement residual, not raw output, to reduce source-motion bias.
    h, w = frames.shape[1:3]
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    residual = frames[..., :3].astype(np.float32) - source[..., :3].astype(np.float32)
    values = []
    for i in range(1, len(frames)):
        previous = cv2.remap(residual[i - 1], xx + flow[i, ..., 0], yy + flow[i, ..., 1], cv2.INTER_LINEAR)
        values.append(float(np.abs(residual[i] - previous)[masks[i]].mean()))
    return {"per_frame": values, "mean_255": float(np.mean(values)),
            "caveat": "Diagnostic only; lower residual variation is not proof of better perceptual quality."}


def summarize(args):
    dest = args.output.resolve()
    data = np.load(dest / "inputs.npz")
    summary = {"input": json.loads((dest / "input.json").read_text(encoding="utf-8")), "backends": {}}
    for backend in args.backends:
        base = np.load(dest / f"{backend}-zero" / "frames.npy")
        cases = {}
        for case_dir in sorted(dest.glob(f"{backend}-*")):
            result_path = case_dir / "result.json"
            if not result_path.exists():
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            frames = np.load(case_dir / "frames.npy")
            result["vs_zero"] = differences(base, frames)
            result["vs_input"] = differences(data["frames"], frames)
            result["temporal_residual"] = temporal_residual(frames, data["frames"], data["flow"], data["masks"])
            result["shutdown"] = json.loads((case_dir / "shutdown.json").read_text()) if (case_dir / "shutdown.json").exists() else None
            result["process_failure"] = json.loads((case_dir / "failure.json").read_text()) if (case_dir / "failure.json").exists() else None
            cases[result["case"]] = result
        summary["backends"][backend] = cases
    save_json(dest / "summary.json", summary)
    print(json.dumps({b: {c: r["vs_zero"] for c, r in cases.items()} for b, cases in summary["backends"].items()}, indent=2))


def preview(args):
    import imageio_ffmpeg
    dest = args.output.resolve()
    target = dest / "comparison.mp4"
    if target.exists():
        raise FileExistsError(target)
    with np.load(dest / "inputs.npz") as data:
        source = data["frames"]
    choices = [("Input (synthetic camera motion)", source)]
    for label, case in (("Zero guidance", "zero"), ("RAFT fork: -forward", "raft_original"),
                        ("RAFT corrected: backward", "raft_corrected")):
        choices.append((label, np.load(dest / f"{args.backend}-{case}" / "frames.npy")))
    h, w = source.shape[1:3]
    out_w, out_h = w * 2, (h + 32) * 2
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{out_w}x{out_h}", "-r", "12", "-i", "-", "-an", "-c:v", "libx264",
        "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        for cycle in range(3):
            for i in range(len(source)):
                tiles = []
                for label, frames in choices:
                    tile = np.zeros((h + 32, w, 3), np.uint8)
                    tile[32:] = cv2.cvtColor(frames[i], cv2.COLOR_RGBA2BGR)
                    cv2.putText(tile, label, (8, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 255), 1, cv2.LINE_AA)
                    tiles.append(tile)
                composite = np.vstack((np.hstack(tiles[:2]), np.hstack(tiles[2:])))
                process.stdin.write(composite.tobytes())
                if cycle == 0 and i == len(source) // 2:
                    cv2.imwrite(str(dest / "comparison.png"), composite)
        process.stdin.close()
        if process.wait(timeout=60):
            raise RuntimeError("Preview encoding failed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    print(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--image", type=Path, required=True)
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=360)
    p.add_argument("--frames", type=int, default=16)
    p.set_defaults(func=prepare)
    p = sub.add_parser("case")
    p.add_argument("--backend", choices=["v2", "legacy"], required=True)
    p.add_argument("--case", choices=CASES, required=True)
    p.add_argument("--submission", choices=["merged", "compatibility"], default="merged")
    p.set_defaults(func=run_case)
    p = sub.add_parser("suite")
    p.add_argument("--backends", nargs="+", choices=["v2", "legacy"], default=["v2", "legacy"])
    p.add_argument("--cases", nargs="+", choices=CASES, default=[c for c in CASES if not c.startswith("raft_")])
    p.add_argument("--submission", choices=["merged", "compatibility"], default="merged")
    p.add_argument("--timeout", type=float, default=90)
    p.add_argument("--keep-going", action="store_true")
    p.set_defaults(func=suite)
    p = sub.add_parser("raft")
    p.add_argument("--weights", type=Path, default=ROOT / "tmp/dlss5standaloneV2/torch_home/hub/checkpoints/raft_large_C_T_SKHT_V2-ff5fadd5.pth")
    p.add_argument("--edge", type=int, default=720)
    p.add_argument("--threads", type=int, default=4)
    p.set_defaults(func=prepare_raft)
    p = sub.add_parser("summarize")
    p.add_argument("--backends", nargs="+", default=["v2", "legacy"])
    p.set_defaults(func=summarize)
    p = sub.add_parser("preview")
    p.add_argument("--backend", choices=["v2", "legacy"], default="v2")
    p.set_defaults(func=preview)
    for p in sub.choices.values():
        p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare" and (args.frames < 2 or args.width < 128 or args.height < 128):
        parser.error("Need at least 2 frames and dimensions >=128")
    args.func(args)


if __name__ == "__main__":
    main()
