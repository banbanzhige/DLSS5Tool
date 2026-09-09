"""Separate frozen enhancement executable. Never imported by the base app."""
import argparse
import contextlib
import json
from multiprocessing.connection import Client
import os
from pathlib import Path
import sys
import threading
import time
from dlss5tool.guidance_transport import GuidanceBuffers, TRANSPORT
from dlss5tool.guidance_execution import execution_contract
from dlss5tool.guidance_parameters import parameters, analysis_parameters, analysis_size
from dlss5tool.guidance_cache import RawGuidanceCache, frame_digest, cache_budget_mib, CACHE_VERSION
from dlss5tool.guidance_inputs import prepare_flow, clear_flow_inputs


class ModelConfigurationError(RuntimeError):
    def __init__(self, key):
        self.key = key
        super().__init__(key)


def select_device(requested, cuda_available):
    if requested == 'cpu':
        return 'cpu'
    if requested not in ('auto', 'cuda'):
        raise ModelConfigurationError('guidance.error.device')
    if not cuda_available:
        raise ModelConfigurationError('guidance.error.cuda')
    return 'cuda'


def depth_profile(settings, device):
    requested = settings.get('guidance_depth_profile', 'fp32')
    if requested not in ('fp32', 'sdpa_fp16'):
        raise ModelConfigurationError('guidance.error.depth_profile')
    effective = requested if int(settings['guidance_mode']) in (2, 3) else 'fp32'
    if effective == 'sdpa_fp16' and device != 'cuda':
        raise ModelConfigurationError('guidance.error.depth_cuda')
    return effective


def watch_parent(pid):
    # Kill the model child even if the crash-isolated native parent is terminated.
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        os._exit(2)
    kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
    kernel.CloseHandle(handle)
    os._exit(0)


class Models:
    def __init__(self, settings):
        import cv2
        import numpy as np
        import torch
        self.cv2, self.np, self.torch = cv2, np, torch
        self.settings = {**settings, **parameters(settings, strict=True)}
        self.analysis_parameters = analysis_parameters(self.settings)
        self.mode = int(settings["guidance_mode"])
        requested = settings.get("guidance_device", "auto")
        self.device = select_device(requested, torch.cuda.is_available() if requested != 'cpu' else False)
        self.device_name = torch.cuda.get_device_name() if self.device == 'cuda' else 'CPU'
        try:
            self.execution_info = execution_contract(settings, self.device)
        except ValueError as exc:
            raise ModelConfigurationError(str(exc)) from exc
        self.flow_stream = self.depth_stream = None
        self._closed = False
        self._failed = False
        self._process_lock = threading.Lock()
        clear_flow_inputs(self)
        self.depth_profile = depth_profile(settings, self.device)
        self.depth_precision = 'float16_amp' if self.depth_profile == 'sdpa_fp16' else 'float32'
        self.depth_attention = 'sdpa' if self.depth_profile == 'sdpa_fp16' else 'original'
        self.precision = 'depth=AMP FP16 (SDPA); flow=FP32' if self.depth_profile == 'sdpa_fp16' else 'float32'
        self.last_metrics = {}
        pool = None
        if settings.get('guidance_cache_pool'):
            from dlss5tool.shared_cache_budget import SharedCacheBudget
            pool = SharedCacheBudget(settings['guidance_cache_pool'])
        self.raw_cache = RawGuidanceCache(cache_budget_mib(settings) * 1048576, pool=pool)
        self.prev_digest = None
        if self.device == 'cuda':
            torch.cuda.reset_peak_memory_stats()
        if self.device == "cpu":
            torch.set_num_threads(min(4, os.cpu_count() or 1))
        self.prev = self.prev_thumb = None
        self.depth_range = None
        self.flow = self.depth = None
        if self.mode in (1, 3):
            from torchvision.models.optical_flow import Raft_Large_Weights, raft_large
            self.flow = raft_large(weights=None).eval()
            self.flow.load_state_dict(torch.load(settings["flow_weights"], map_location="cpu", weights_only=True))
            self.flow.to(self.device)
            self.transforms = Raft_Large_Weights.DEFAULT.transforms()
        if self.mode in (2, 3):
            # Architecture is packaged by the maintainer; only weights are external.
            from depth_anything_v2.dpt import DepthAnythingV2
            configs = {"vits": (64, [48, 96, 192, 384]), "vitb": (128, [96, 192, 384, 768]),
                       "vitl": (256, [256, 512, 1024, 1024])}
            encoder = settings.get("guidance_depth_encoder", "vitl")
            features, channels = configs[encoder]
            self.depth = DepthAnythingV2(encoder=encoder, features=features, out_channels=channels)
            self.depth.load_state_dict(torch.load(settings["depth_weights"], map_location="cpu", weights_only=True))
            self.depth.to(self.device).eval()
            if self.depth_profile == 'sdpa_fp16':
                from dlss5tool.depth_acceleration import enable_sdpa
                try:
                    enable_sdpa(self.depth, encoder)
                except (ImportError, KeyError, ValueError, AttributeError) as exc:
                    raise ModelConfigurationError('guidance.error.depth_acceleration') from exc
        if self.execution_info['schedule'] == 'dual_stream':
            self.flow_stream = torch.cuda.Stream()
            self.depth_stream = torch.cuda.Stream()
            # Immutable parameters were initialized on the default stream.
            # Keep them alive until both streams finish during close().
            self.flow_stream.wait_stream(torch.cuda.current_stream())
            self.depth_stream.wait_stream(torch.cuda.current_stream())
            self._flow_events = (torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True))
            self._depth_events = (torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True))

    def _flow_input(self, small):
        return prepare_flow(self, small)

    def _depth_input(self, small, fw, fh):
        cv2, np, torch = self.cv2, self.np, self.torch
        dw, dh = max(14, round(fw / 14) * 14), max(14, round(fh / 14) * 14)
        image = cv2.resize(small, (dw, dh)).astype(np.float32) / 255.0
        image = (image - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        return torch.from_numpy(image).permute(2, 0, 1)[None]

    def _infer_flow(self, inputs):
        first, second = inputs
        return self.flow(first.to(self.device), second.to(self.device),
                         num_flow_updates=self.settings.get('guidance_flow_updates', 6))[-1]

    def _infer_depth(self, tensor):
        torch = self.torch
        tensor = tensor.to(self.device)
        with torch.autocast('cuda', dtype=torch.float16) if self.depth_profile == 'sdpa_fp16' else contextlib.nullcontext():
            prediction = self.depth(tensor)
        return prediction[0].float()

    def _finish_flow(self, flow, mv, w, h, fw, fh):
        cv2, np = self.cv2, self.np
        values = flow if isinstance(flow, np.ndarray) else flow[0].permute(1, 2, 0).cpu().numpy()
        if not np.isfinite(values).all():
            raise ModelConfigurationError('guidance.error.nonfinite')
        if self._flow_key is not None and not self._flow_hit:
            self.raw_cache.put(self._flow_key, values)
        if self.settings.get('guidance_flow_direction', 'backward') == 'forward_negated':
            values = -values
        cv2.resize(values, (w, h), dst=mv, interpolation=cv2.INTER_LINEAR)
        mv[..., 0] *= w / fw
        mv[..., 1] *= h / fh

    def _finish_depth(self, prediction, dp, reset, w, h):
        cv2, np = self.cv2, self.np
        values = prediction if isinstance(prediction, np.ndarray) else prediction.cpu().numpy()
        if not np.isfinite(values).all():
            raise ModelConfigurationError('guidance.error.depth_nonfinite')
        if self._depth_key is not None and not self._depth_hit:
            self.raw_cache.put(self._depth_key, values)
        low, high = map(float, np.percentile(values, [self.settings.get('guidance_depth_low', 1),
                                                    self.settings.get('guidance_depth_high', 99)]))
        weight = self.settings.get('guidance_depth_smoothing', 0.9)
        bounds = (low, high) if reset or self.depth_range is None else tuple(
            weight * a + (0.1 if weight == 0.9 else 1 - weight) * b
            for a, b in zip(self.depth_range, (low, high)))
        low, high = bounds
        values = np.clip((values - low) / max(high - low, 1e-6), 0, 1)
        cv2.resize(values, (w, h), dst=dp, interpolation=cv2.INTER_LINEAR)
        return bounds

    def process(self, rgba, reset, outputs=None):
        # RAFT keeps a mutable correlation pyramid: never allow two frames in
        # flight in one Models instance, even though different models overlap.
        if not self._process_lock.acquire(blocking=False):
            raise RuntimeError('Concurrent guidance frames are not supported')
        try:
            if self._closed or self._failed:
                raise RuntimeError('Guidance models require a new session')
            return self._process_frame(rgba, reset, outputs)
        except Exception:
            self._failed = True
            clear_flow_inputs(self)
            if hasattr(self, 'raw_cache'):
                self.raw_cache.clear()
            self._cached_flow = self._cached_depth = None
            raise
        finally:
            self._process_lock.release()

    def _process_frame(self, rgba, reset, outputs=None):
        started = time.perf_counter()
        self.raw_cache.trim()
        cv2, np, torch = self.cv2, self.np, self.torch
        rgb = rgba[..., :3]
        h, w = rgb.shape[:2]
        thumb = cv2.resize(rgb, (64, 36)).astype(np.float32) / 255.0
        # Conservative cut heuristic; explicit seek/segment resets always win.
        cut = self.prev_thumb is not None and np.abs(thumb - self.prev_thumb).mean() > 0.30
        reset = bool(reset or cut or self.prev is None)
        params = parameters(self.settings, strict=True)
        fw, fh = analysis_size(w, h, params['guidance_flow_edge'])
        dw, dh = analysis_size(w, h, params['guidance_depth_edge'])
        small = cv2.resize(rgb, (fw, fh))
        # Both branches resize from the original input, never from each other.
        depth_small = small if (dw, dh) == (fw, fh) else cv2.resize(rgb, (dw, dh))
        if self.prev is not None and self.prev.shape != small.shape:
            reset = True
        # Session fixes all model/config identity. Hash actual input, including
        # dimensions/dtype; frame-number/path reuse cannot return stale guidance.
        # A shared allowance can temporarily reach zero. Still track the previous
        # input identity, or a later grant could cache (None,current) for wrong pairs.
        current_digest = frame_digest(rgba) if self.raw_cache.limit_bytes or self.raw_cache.pool else None
        self._depth_key = ('depth', current_digest) if current_digest is not None and self.depth is not None else None
        self._flow_key = ('flow', self.prev_digest, current_digest) if current_digest is not None and not reset and self.flow is not None else None
        self._cached_depth = self.raw_cache.get(self._depth_key) if self._depth_key is not None else None
        self._cached_flow = self.raw_cache.get(self._flow_key) if self._flow_key is not None else None
        self._depth_hit = self._cached_depth is not None
        self._flow_hit = self._cached_flow is not None
        if outputs is None:
            mv = np.zeros((h, w, 2), np.float32)
            dp = np.zeros((h, w), np.float32)
        else:
            mv, dp = outputs
            # Reused buffers must never leak a prior frame on reset/inactive mode.
            if self.flow is None or reset:
                mv.fill(0)
            if self.depth is None:
                dp.fill(0)
        flow_ms = depth_ms = 0.0
        bounds = self.depth_range
        dual = self.flow_stream is not None and not (self._depth_hit and (reset or self._flow_hit))
        with torch.inference_mode():
            if dual:
                # Prepare both inputs first, launch both models BEFORE any .cpu()
                # wait. Tensor allocation, consumption and readback stay on each
                # tensor's creation stream; no cross-stream reuse of activations.
                flow_inputs = self._flow_input(small) if not reset and not self._flow_hit else None
                depth_input = self._depth_input(depth_small, dw, dh) if not self._depth_hit else None
                with torch.cuda.stream(self.flow_stream):
                    self._flow_events[0].record()
                    flow = self._cached_flow if self._flow_hit else self._infer_flow(flow_inputs) if flow_inputs is not None else None
                    self._flow_events[1].record()
                with torch.cuda.stream(self.depth_stream):
                    self._depth_events[0].record()
                    prediction = self._cached_depth if self._depth_hit else self._infer_depth(depth_input)
                    self._depth_events[1].record()
                with torch.cuda.stream(self.flow_stream):
                    if flow is not None:
                        self._finish_flow(flow, mv, w, h, fw, fh)
                    self._flow_events[1].synchronize()
                with torch.cuda.stream(self.depth_stream):
                    bounds = self._finish_depth(prediction, dp, reset, w, h)
                # GPU event durations exclude CPU postprocessing and OVERLAP.
                # Explicit labels keep them from masquerading as serial timings.
                flow_ms = self._flow_events[0].elapsed_time(self._flow_events[1]) if flow is not None else 0.0
                depth_ms = self._depth_events[0].elapsed_time(self._depth_events[1])
            else:
                flow_ms, depth_ms, bounds = self._serial(small, mv, dp, reset, w, h, fw, fh,
                                                       depth_small, dw, dh)
        self.last_metrics = {'inference_ms': (time.perf_counter() - started) * 1000,
            'flow_ms': flow_ms, 'depth_ms': depth_ms,
            'flow_size': [fw, fh] if self.flow is not None else None,
            'depth_size': [max(14, round(dw / 14) * 14), max(14, round(dh / 14) * 14)] if self.depth is not None else None,
            'flow_updates': params['guidance_flow_updates'] if self.flow is not None else 0,
            'timing_kind': 'overlapping_gpu_events' if dual else 'serial_wall_with_postprocess',
            'schedule': self.execution_info['schedule'],
            **self.raw_cache.metrics(),
            'cache_flow_hit': self._flow_hit, 'cache_depth_hit': self._depth_hit,
            'flow_model_calls': int(self.flow is not None and not reset and not self._flow_hit),
            'depth_model_calls': int(self.depth is not None and not self._depth_hit),
            'peak_allocated_mib': torch.cuda.max_memory_allocated() / 1048576 if self.device == 'cuda' else 0,
            'peak_reserved_mib': torch.cuda.max_memory_reserved() / 1048576 if self.device == 'cuda' else 0}
        self.prev, self.prev_thumb, self.depth_range = small, thumb, bounds
        self.prev_digest = current_digest
        self._cached_flow = self._cached_depth = None
        return mv, dp, reset

    def _serial(self, small, mv, dp, reset, w, h, fw, fh, depth_small=None, dw=None, dh=None):
        bounds = self.depth_range
        with self.torch.inference_mode():
            flow_start = time.perf_counter()
            if self.flow is not None and not reset:
                flow = self._cached_flow if self._flow_hit else self._infer_flow(self._flow_input(small))
                self._finish_flow(flow, mv, w, h, fw, fh)
            flow_ms = (time.perf_counter() - flow_start) * 1000
            depth_start = time.perf_counter()
            if self.depth is not None:
                prediction = self._cached_depth if self._depth_hit else self._infer_depth(self._depth_input(
                    small if depth_small is None else depth_small, dw or fw, dh or fh))
                bounds = self._finish_depth(prediction, dp, reset, w, h)
            depth_ms = (time.perf_counter() - depth_start) * 1000
        return flow_ms, depth_ms, bounds

    def close(self):
        if self._closed:
            return
        self._closed = True
        # Exception paths may still have kernels using the persistent weights.
        # Drain side streams before dropping any model/activation references.
        for stream in (self.flow_stream, self.depth_stream):
            if stream is not None:
                try:
                    stream.synchronize()
                except RuntimeError:
                    pass  # Preserve the original failure; the worker exits.
        self.prev = self.prev_thumb = self.depth_range = None
        self.prev_digest = self._cached_flow = self._cached_depth = None
        clear_flow_inputs(self)
        if hasattr(self, 'raw_cache'):
            self.raw_cache.close()
        self.flow = self.depth = None


def serve(conn, settings, model_factory=Models):
    """Serve either the original v1 pipe or negotiated shared-memory transport."""
    def reply(value):
        conn.send_bytes(json.dumps(value).encode('utf-8'))
    if settings.get('protocol') != 1:
        raise ModelConfigurationError('guidance.error.component_invalid')
    buffers = models = None
    try:
        w, h = int(settings['width']), int(settings['height'])
        if settings.get('shared_memory') is not None:
            buffers = GuidanceBuffers(w, h, settings['shared_memory'])
        load_start = time.perf_counter()
        models = model_factory(settings)
        reply({'ok': True, 'device': models.device, 'device_name': models.device_name,
               **({'analysis_parameters': models.analysis_parameters} if hasattr(models, 'analysis_parameters') else {}),
               'precision': getattr(models, 'precision', 'float32'),
               'depth_profile': getattr(models, 'depth_profile', 'fp32'),
               'depth_precision': getattr(models, 'depth_precision', 'float32'),
               'depth_attention': getattr(models, 'depth_attention', 'original'),
               'cache_version': CACHE_VERSION if hasattr(models, 'raw_cache') else None,
               'cache_limit_bytes': models.raw_cache.limit_bytes if hasattr(models, 'raw_cache') else 0,
               **getattr(models, 'execution_info', execution_contract(settings, getattr(models, 'device', None))),
               'load_ms': (time.perf_counter() - load_start) * 1000,
               'protocol': 1, 'transport': TRANSPORT if buffers else 'pipe'})
        sequence = 0
        while True:
            # Reclaim after a reduced GUI budget even while preview is paused.
            if hasattr(models,'raw_cache'):
                models.raw_cache.trim()
                if not conn.poll(0.25):continue
            request = json.loads(conn.recv_bytes(65536))
            if buffers is not None:
                sequence += 1
                if request.get('sequence') != sequence:
                    raise ValueError('Guidance frame sequence mismatch')
                frame = buffers.rgba
                mv, dp, reset = models.process(frame, request.get('reset', False),
                                                outputs=(buffers.motion, buffers.depth))
            else:
                frame = models.np.frombuffer(conn.recv_bytes(w * h * 4), models.np.uint8).reshape(h, w, 4)
                mv, dp, reset = models.process(frame, request.get('reset', False))
            reply({'ok': True, 'reset': reset, 'metrics': models.last_metrics,
                   'sequence': request.get('sequence')})
            if buffers is None:
                conn.send_bytes(mv.tobytes())
                conn.send_bytes(dp.tobytes())
    finally:
        # Drop ndarray aliases before unmapping, including on inference failure.
        frame = mv = dp = None
        if models is not None and hasattr(models, 'close'):
            models.close()
        if buffers is not None:
            buffers.close()


def main():
    if os.name == 'nt' and not getattr(sys, 'frozen', False):
        # Reset only this external Python's inherited private DLL search path.
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        kernel.SetDllDirectoryW(None)
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--parent", type=int, required=True)
    args = parser.parse_args()
    threading.Thread(target=watch_parent, args=(args.parent,), daemon=True).start()
    conn = Client(args.address, family="AF_PIPE", authkey=bytes.fromhex(args.token))
    def reply(value):
        conn.send_bytes(json.dumps(value).encode("utf-8"))
    try:
        settings = json.loads(conn.recv_bytes(65536))
        serve(conn, settings)
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        try:
            torch_module = sys.modules.get('torch')
            key = ('guidance.error.oom' if torch_module and isinstance(exc, torch_module.OutOfMemoryError) else
                   exc.key if isinstance(exc, ModelConfigurationError) else None)
            reply({"ok": False, "error": str(exc)[:4000],
                   "error_key": key})
        except (EOFError, BrokenPipeError):
            pass
    finally:
        conn.close()


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    main()
