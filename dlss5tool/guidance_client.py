"""Optional model worker. No torch imports, installations or downloads in the app."""
import json
from multiprocessing.connection import Listener
import os
import queue
import secrets
import subprocess
import threading
import uuid
import time

import numpy as np
from dlss5tool import mod_paths
from dlss5tool import i18n
from dlss5tool.guidance_transport import GuidanceBuffers, TRANSPORT
from dlss5tool.guidance_execution import execution_contract
from dlss5tool.guidance_parameters import ANALYSIS_KEYS, parameters, check_parameter_handshake
from dlss5tool.guidance_public import normalize_public_settings
from dlss5tool.guidance_flow import flow_backend, flow_grid, check_flow_handshake

KEYS = ("guidance_mode", "guidance_edge", "guidance_flow_direction",
        "guidance_depth_encoder", "guidance_device", "mods_directory",
        "guidance_flow_weights", "guidance_depth_weights", "guidance_transport", "guidance_depth_profile", "guidance_execution", "guidance_cache_mb", "guidance_cache_pool")


def contract(settings):
    settings = normalize_public_settings(settings)
    values = parameters(settings)
    backend = flow_backend(settings)
    return (backend, settings.get('guidance_flow_fallback', True), flow_grid(settings)) + tuple(
        settings.get(key) for key in KEYS) + tuple(
        values[key] for key in ANALYSIS_KEYS if not (backend == 'nvofa' and key == 'guidance_flow_updates'))


def validate(settings):
    settings = normalize_public_settings(settings)
    try:
        flow_backend(settings, strict=True)
        flow_grid(settings)
    except ValueError as exc:
        raise ValueError(i18n.tr_for(settings.get('ui_language'), str(exc))) from exc
    try:
        parameters(settings, strict=True)
    except ValueError as exc:
        raise ValueError(i18n.tr_for(settings.get('ui_language'), 'guidance.error.parameter', parameter=str(exc))) from exc
    mode = int(settings.get("guidance_mode", 0))
    if mode not in (0, 1, 2, 3):
        raise ValueError(i18n.tr_for(settings.get('ui_language'), 'guidance.error.mode'))
    if mode and (settings.get("frame_format") == "rgba16f" or settings.get("host_tiled_mode")):
        raise ValueError(i18n.tr_for(settings.get('ui_language'), 'guidance.error.format'))
    profile = settings.get('guidance_depth_profile', 'fp32')
    if mode and profile not in ('fp32', 'sdpa_fp16'):
        raise ValueError(i18n.tr_for(settings.get('ui_language'), 'guidance.error.depth_profile'))
    if mode in (2, 3) and profile == 'sdpa_fp16' and settings.get('guidance_device') == 'cpu':
        raise ValueError(i18n.tr_for(settings.get('ui_language'), 'guidance.error.depth_cuda'))
    if mode:
        try:
            execution_contract(settings, settings.get('guidance_device'))
        except ValueError as exc:
            raise ValueError(i18n.tr_for(settings.get('ui_language'), str(exc))) from exc
    files = mod_paths.guidance_files(settings) if mode else {}
    if mode in (1, 3) and flow_backend(settings) == 'nvofa' and 'nvofa' not in mod_paths.flow_backends(settings):
        raise ValueError(i18n.tr_for(settings.get('ui_language'), 'guidance.error.flow_backend_component'))
    return files


def preflight(settings, *, require_shared_cache=False):
    """Load only requested models and exercise kernels in an isolated worker.

    A small two-frame probe checks both depth and temporal flow, even without
    imported media. It does not certify VRAM capacity for a full-size render.
    No Torch import, downloads, or environment installation in the base app.
    """
    settings = normalize_public_settings(settings)
    validate(settings)
    if not int(settings.get('guidance_mode', 0)):
        return {}
    probe = {**settings, 'guidance_cache_mb': 0}
    session = GuidanceSession(probe, 128, 128)
    try:
        if require_shared_cache and session.info.get('cache_version') != 'raw_lru_v2_shared':
            raise RuntimeError(i18n.tr_for(settings.get('ui_language'), 'guidance.cache_unavailable'))
        frame = np.empty((128, 128, 4), dtype=np.uint8)
        frame[..., :3] = np.arange(128, dtype=np.uint8)[None, :, None]
        frame[..., 3] = 255
        session.process(frame, reset=True)
        session.process(np.roll(frame, 1, axis=1), reset=False)
        return dict(session.info)
    finally:
        session.close()


def check_depth_handshake(settings, ready, language):
    profile = settings.get('guidance_depth_profile', 'fp32') if int(settings.get('guidance_mode', 0)) in (2, 3) else 'fp32'
    if profile == 'sdpa_fp16':
        if (ready.get('depth_profile') != profile or ready.get('depth_attention') != 'sdpa'
                or ready.get('depth_precision') != 'float16_amp'):
            raise RuntimeError(i18n.tr_for(language, 'guidance.error.depth_acceleration'))
    elif (ready.get('depth_profile', 'fp32') != 'fp32'
          or ready.get('depth_precision', 'float32') != 'float32'
          or ready.get('depth_attention', 'original') != 'original'):
        raise RuntimeError(i18n.tr_for(language, 'guidance.error.depth_profile'))


def check_execution_handshake(settings, ready, language):
    expected = execution_contract(settings, ready.get('device'))
    accelerated = expected['execution'] != 'serial'
    for key, value in expected.items():
        # Old v1 components only implement the original serial path. Missing
        # acknowledgement is acceptable solely for that legacy configuration.
        if ready.get(key, None if accelerated else value) != value:
            raise RuntimeError(i18n.tr_for(language, 'guidance.error.execution_component'))


class GuidanceSession:
    def __init__(self, settings, width, height):
        settings = normalize_public_settings(settings)
        backend = flow_backend(settings, strict=True)
        try:
            self._initialize(settings, width, height)
        except (RuntimeError, FileNotFoundError, OSError) as error:
            if (backend != 'nvofa' or settings['guidance_mode'] != 1
                    or not settings.get('guidance_flow_fallback', True)):
                raise
            # Only during initialization. process() never changes backend.
            fallback = {**settings, 'guidance_flow_backend': 'raft'}
            try:
                self._initialize(fallback, width, height)
            except Exception as fallback_error:
                raise RuntimeError(f'NVOFA: {error}\nRAFT: {fallback_error}') from fallback_error
            self.info.update(flow_backend='raft', flow_requested_backend='nvofa',
                             flow_fallback_reason=str(error))

    def _initialize(self, settings, width, height):
        settings = normalize_public_settings(settings)
        self._process = self._connection = self._listener = None
        self._buffers = None
        self._sequence = 0
        self.transport = 'pipe'
        self.width, self.height = width, height
        self.language = settings.get('ui_language') or i18n.get_language()
        self.info = {}
        self.last_metrics = {}
        files = validate(settings)
        worker_settings = dict(settings)
        if int(settings.get('guidance_mode', 0)) in (2, 3):
            worker_settings['guidance_depth_encoder'] = mod_paths.depth_encoder(settings)
        token = secrets.token_bytes(32)
        # AF_PIPE is local-only and avoids firewall/network requirements.
        address = r"\\.\pipe\dlss5-guidance-" + uuid.uuid4().hex
        self._listener = Listener(address, family="AF_PIPE", authkey=token)
        listener = self._listener
        result = queue.Queue()
        def accept():
            try:
                result.put(listener.accept())
            except Exception as exc:
                result.put(exc)
        threading.Thread(target=accept, daemon=True).start()
        env = dict(os.environ)
        # Never leak PyInstaller's Python import paths into a different Python.
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        try:
            requested_transport = settings.get('guidance_transport', 'auto')
            if requested_transport not in ('auto', 'pipe', TRANSPORT):
                raise ValueError('Invalid guidance transport')
            if requested_transport != 'pipe':
                self._buffers = GuidanceBuffers(width, height)
            self._process = subprocess.Popen([files["worker"],
                "--address", address, "--token", token.hex(), "--parent", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            connection = result.get(timeout=20)
            if isinstance(connection, Exception):
                raise connection
            self._connection = connection
            self._listener.close()
            self._listener = None
            self._send({**worker_settings, **files, "protocol": mod_paths.GUIDANCE_PROTOCOL, "width": width, "height": height,
                        "shared_memory": self._buffers.descriptor if self._buffers else None,
                        "mods_root": str(mod_paths.mods_root(settings))})
            ready = self._reply()
            if ready.get('protocol') != mod_paths.GUIDANCE_PROTOCOL:
                raise RuntimeError(i18n.tr_for(self.language, 'guidance.error.component_invalid'))
            expected = 'cpu' if settings.get('guidance_device') == 'cpu' else 'cuda'
            if ready.get('device') != expected:
                raise RuntimeError(i18n.tr_for(self.language, 'guidance.error.cuda'))
            check_depth_handshake(settings, ready, self.language)
            check_execution_handshake(settings, ready, self.language)
            try:
                check_flow_handshake(settings, ready)
                check_parameter_handshake(settings, ready)
            except ValueError as exc:
                raise RuntimeError(i18n.tr_for(self.language, str(exc))) from exc
            if settings.get('guidance_cache_pool') and ready.get('cache_version') != 'raw_lru_v2_shared':
                raise RuntimeError(i18n.tr_for(self.language,'guidance.cache_unavailable'))
            self.transport = ready.get('transport', 'pipe')
            if self.transport not in ('pipe', TRANSPORT) or (self.transport == TRANSPORT and self._buffers is None):
                raise RuntimeError('Invalid guidance transport acknowledgement')
            if requested_transport == TRANSPORT and self.transport != TRANSPORT:
                raise RuntimeError('Guidance component does not support shared memory')
            if self.transport == 'pipe' and self._buffers is not None:
                self._buffers.close()
                self._buffers = None
            self.info = {key: ready.get(key) for key in ('device', 'device_name', 'precision', 'load_ms',
                'depth_profile', 'depth_precision', 'depth_attention', 'execution', 'raft_output', 'schedule',
                'cache_version', 'cache_limit_bytes', 'analysis_parameters')}
            self.info['transport'] = self.transport
            self.info.update({key: ready.get(key) for key in ('flow_grid', 'flow_quality', 'flow_temporal_hints')})
            self.info['flow_backend'] = ready.get('flow_backend', 'raft')
        except Exception as exc:
            self.close()
            detail = str(exc) or i18n.tr_for(self.language, 'guidance.error.connection')
            raise RuntimeError(i18n.tr_for(self.language, 'guidance.error.load', error=detail)) from exc

    def _send(self, value):
        self._connection.send_bytes(json.dumps(value).encode("utf-8"))

    def _reply(self):
        if not self._connection.poll(90):
            raise TimeoutError(i18n.tr_for(self.language, 'guidance.error.timeout'))
        value = json.loads(self._connection.recv_bytes(65536))
        if not value.get("ok"):
            key = value.get('error_key')
            if key in ('guidance.error.cuda', 'guidance.error.component_invalid', 'guidance.error.oom', 'guidance.error.device',
                       'guidance.error.depth_profile', 'guidance.error.depth_cuda', 'guidance.error.depth_acceleration',
                       'guidance.error.depth_nonfinite', 'guidance.error.execution',
                       'guidance.error.streams_cuda', 'guidance.error.execution_component',
                       'guidance.error.nvofa', 'guidance.error.nvofa_cuda', 'guidance.error.nvofa_mixed',
                       'guidance.error.flow_backend', 'guidance.error.flow_backend_component',
                       'guidance.error.flow_grid', 'guidance.error.flow_grid_component'):
                raise RuntimeError(i18n.tr_for(self.language, key))
            raise RuntimeError(i18n.tr_for(self.language, 'guidance.error.worker', error=value.get('error', '')))
        return value

    def process(self, rgba, reset=False, *, copy_outputs=True):
        """Return independent arrays by default.

        Native callers may borrow shared outputs with copy_outputs=False. These
        views expire at the next process()/close(); native enqueue must have
        copied them into its own upload slot before returning.
        """
        if rgba.dtype != np.uint8 or rgba.shape != (self.height, self.width, 4):
            raise ValueError(i18n.tr_for(self.language, 'guidance.error.input'))
        try:
            started = time.perf_counter()
            self._sequence += 1
            if self._buffers is not None:
                np.copyto(self._buffers.rgba, rgba)
            self._send({"reset": bool(reset), "sequence": self._sequence})
            if self._buffers is None:
                self._connection.send_bytes(np.ascontiguousarray(rgba).tobytes())
            response = self._reply()
            if self._buffers is not None:
                if response.get('sequence') != self._sequence:
                    raise RuntimeError('Guidance frame acknowledgement mismatch')
                mv, dp = self._buffers.motion, self._buffers.depth
                if copy_outputs:
                    mv, dp = mv.copy(), dp.copy()
            else:
                mv = np.frombuffer(self._connection.recv_bytes(self.width * self.height * 8), np.float32).reshape(self.height, self.width, 2).copy()
                dp = np.frombuffer(self._connection.recv_bytes(self.width * self.height * 4), np.float32).reshape(self.height, self.width).copy()
            if not np.isfinite(mv).all() or not np.isfinite(dp).all():
                raise ValueError(i18n.tr_for(self.language, 'guidance.error.nonfinite'))
            self.last_metrics = {**response.get('metrics', {}), 'roundtrip_ms': (time.perf_counter() - started) * 1000}
            return mv, dp, bool(response.get("reset"))
        except Exception:
            self.close()
            raise

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
            self._process = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._buffers is not None:
            self._buffers.close()
            self._buffers = None
