#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Crash-isolated, restartable process proxy for the DLSS host DLLs.

NGX core initialization is intentionally confined to a child process.  Replacing
that process allows the GUI to switch host DLLs without initializing NGX twice in
the long-lived GUI process.  Frames use shared memory; the Pipe only carries small
commands and acknowledgements.
"""

import multiprocessing
from multiprocessing import shared_memory
from dlss5tool.memory_budget import (require_memory_headroom, receive_worker_command,
    memory_allocation, current_pool, worker_with_memory_pool, record_memory_event)
import os
import tempfile
import traceback
import uuid

import numpy as np

from dlss5tool import dlss_engine
from dlss5tool import guidance_client
from dlss5tool import appearance
from dlss5tool.host_queue import configured_in_flight
from dlss5tool.deferred_error import ErrorSnapshot
from dlss5tool.guidance_visualization import guidance_images


_CREATE_TIMEOUT = 120.0
_COMMAND_TIMEOUT = 120.0
_STOP_TIMEOUT = 2.0


class HostProcessError(RuntimeError):
    """The isolated DLSS host failed, exited, or stopped responding."""


def _open_shared_memory(*, name=None, create=False, size=0, track=True):
    """Use Python 3.13's track flag when available, while retaining 3.8 support."""
    try:
        return shared_memory.SharedMemory(
            name=name, create=create, size=size, track=track,
        )
    except TypeError:  # Python < 3.13
        return shared_memory.SharedMemory(name=name, create=create, size=size)


def _error_payload(operation, exception, log_path=None):
    detail = str(exception) or repr(exception)
    pressure=getattr(exception,'details',None)
    if pressure is not None:pressure=dict(pressure,source_pid=os.getpid())
    if log_path:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                tail = handle.read().strip()[-2000:]
        except OSError:
            tail = ""
        if tail and tail not in detail:
            detail += "\nNGX 日志末尾：\n" + tail
    return {
        "ok": False,
        "operation": operation,
        "error": detail,
        "traceback": traceback.format_exc()[-4000:],
        "memory_pressure": pressure,
    }


def _metadata(live):
    return {
        "backend": live.backend,
        "adapter_info": dict(getattr(live, "adapter_info", {})),
        "max_in_flight": int(live.max_in_flight),
        "supports_async": bool(live.supports_async),
        "tiled": bool(getattr(live, "tiled", False)),
        "guidance_info": dict(getattr(live, 'guidance_info', {})),
        "guidance_metrics": dict(getattr(live, 'guidance_metrics', {})),
    }


def _host_worker_main(
    connection, width, height, settings, input_name, output_name, log_path,
    live_factory=None,
):
    """Entry point for one disposable NGX process."""
    input_memory = None
    output_memory = None
    live = None
    try:
        # A non-owning process must not ask resource_tracker to unlink the parent's
        # buffers when this deliberately short-lived worker exits.
        input_memory = _open_shared_memory(name=input_name, track=False)
        output_memory = _open_shared_memory(name=output_name, track=False)
        dtype = dlss_engine.frame_dtype(settings)
        input_frame = np.ndarray(
            (height, width, 4), dtype=dtype, buffer=input_memory.buf,
        )
        output_frame = np.ndarray(
            (height, width, 4), dtype=dtype, buffer=output_memory.buf,
        )
        dlss_engine.LOG_PATH = log_path
        if settings.get('_gpu_export_host'):
            # Explicit candidate affects only this disposable worker, never the
            # GUI/global default backend or other workers.
            dlss_engine.HOST_DLL_V2=os.path.abspath(settings['_gpu_export_host'])
        factory = live_factory or dlss_engine.Live
        live = factory(width, height, settings)
        connection.send({"ok": True, "operation": "ready", **_metadata(live)})
    except BaseException as exception:
        try:
            connection.send(_error_payload("initialize", exception, log_path))
        except (BrokenPipeError, EOFError, OSError):
            pass
        return

    while True:
        try:
            message = receive_worker_command(connection, input_frame.nbytes,
                                             allocation_bytes=input_frame.nbytes)
        except (EOFError, OSError):
            break
        except MemoryError as exception:
            try:
                connection.send(_error_payload('memory', exception, log_path))
            except (BrokenPipeError, EOFError, OSError):
                pass
            break
        operation = message.get("operation")
        try:
            if operation == "stop":
                connection.send({"ok": True, "operation": operation})
                break
            if operation == "update":
                live.update(message["settings"])
                connection.send({
                    "ok": True, "operation": operation, **_metadata(live),
                })
            elif operation == "process":
                result = live.process(input_frame, reset=bool(message.get("reset")))
                if result is not None:
                    np.copyto(output_frame, result)
                connection.send({
                    "ok": True, "operation": operation,
                    "has_output": result is not None,
                    **_metadata(live),
                })
            elif operation == 'gpu_output_create':
                connection.send({'ok':True,'operation':operation,**live.gpu_output_create()})
            elif operation == 'process_gpu_output':
                has_output=live.process(input_frame,reset=bool(message.get('reset')),gpu_output=True)
                connection.send({'ok':True,'operation':operation,'has_output':bool(has_output),**_metadata(live)})
            elif operation == "enqueue":
                accepted = live.enqueue(
                    input_frame, reset=bool(message.get("reset")),
                )
                connection.send({
                    "ok": True, "operation": operation,
                    "accepted": bool(accepted),
                    **_metadata(live),
                })
            elif operation == "dequeue":
                result = live.dequeue()
                if result is not None:
                    np.copyto(output_frame, result)
                connection.send({
                    "ok": True, "operation": operation,
                    "has_output": result is not None,
                })
            elif operation == "pending":
                connection.send({
                    "ok": True, "operation": operation,
                    "pending": int(live.pending),
                })
            elif operation == 'guidance_preview':
                if live.pending:
                    raise ValueError('Drain DLSS frames before guidance preview')
                mode = int(live.settings.get('guidance_mode', 0))
                if not mode:
                    raise ValueError('Guidance preview requires enabled guidance')
                # Use the existing component/cache, without evaluating DLSS.
                motion, depth, reset = live.guidance_preview(
                    input_frame, bool(message.get('reset')),
                    final=bool(message.get('display')),
                )
                images = guidance_images(motion, depth, mode, live.settings) if message.get('display') else {}
                connection.send({'ok': True, 'operation': operation, 'images': images,
                                 'reset': bool(reset), **_metadata(live)})
            else:
                raise ValueError("未知 DLSS 工作进程命令: %r" % operation)
        except BaseException as exception:
            try:
                connection.send(_error_payload(operation, exception, log_path))
            except (BrokenPipeError, EOFError, OSError):
                break

    # Do not call dlssnr_shutdown here.  Some driver/runtime combinations hang or
    # crash on shutdown after evaluation.  Exiting this disposable process lets
    # Windows reclaim its D3D12/NGX resources without a second in-process init.
    if hasattr(live, "close_guidance"):
        live.close_guidance()
    del live
    del input_frame
    del output_frame
    try:
        connection.close()
    except OSError:
        pass
    if input_memory is not None:
        input_memory.close()
    if output_memory is not None:
        output_memory.close()


class _HostSession:
    """One child process plus its fixed-resolution shared frame buffers."""

    def __init__(self, width, height, settings, live_factory=None):
        settings = guidance_client.normalize_public_settings(settings)
        # Fail incompatible guidance before allocating two full-resolution frames
        # or spawning NGX. Keep injected test hosts independent of model files.
        if live_factory is None:
            guidance_client.validate(settings)
        self.width = int(width)
        self.height = int(height)
        from dlss5tool.super_resolution import validate_dimensions
        validate_dimensions(self.width, self.height, 1)
        self._closed = False
        self._process = None
        self._connection = None
        self._child_connection = None
        self._input_memory = None
        self._output_memory = None
        self._input_frame = None
        self._output_frame = None
        self.backend = "unknown"
        self.adapter_info = {}
        self.max_in_flight = 1
        self.supports_async = False
        self.tiled = False
        self.dtype = dlss_engine.frame_dtype(settings)
        self.log_path = os.path.join(
            tempfile.gettempdir(), "dlss5tool-host-%s.log" % uuid.uuid4().hex,
        )

        frame_bytes = self.width * self.height * 4 * np.dtype(self.dtype).itemsize
        if frame_bytes <= 0:
            raise ValueError("DLSS 帧尺寸必须大于零")
        # Two IPC frames, native upload/readback per slot and caller output/
        # composition frames. Pure NR/HDR also needs admission without VSR.
        slots = configured_in_flight(settings)
        # Only this owner's IPC and per-slot native staging are allocated here.
        # Caller composition/output copies are admitted at their own stage.
        pool=current_pool()
        with memory_allocation(frame_bytes * (2 + 2 * slots),pool=pool,
                               stage='nr_initialize') as ticket:
            try:
                self._initialize_resources(settings, live_factory, frame_bytes, pool, ticket)
            except BaseException as error:
                try:self.close()
                except BaseException as cleanup:
                    if ticket:ticket.defer_until_retired(self)
                    if hasattr(error,'add_note'):error.add_note('NR cleanup: '+str(cleanup))
                raise

    def _initialize_resources(self, settings, live_factory, frame_bytes, pool, ticket):
        self._input_memory = _open_shared_memory(
            create=True, size=frame_bytes, track=True,
        )
        self._output_memory = _open_shared_memory(
            create=True, size=frame_bytes, track=True,
        )
        if ticket:ticket.consume(frame_bytes*2)
        self._input_frame = np.ndarray(
            (self.height, self.width, 4), dtype=self.dtype,
            buffer=self._input_memory.buf,
        )
        self._output_frame = np.ndarray(
            (self.height, self.width, 4), dtype=self.dtype,
            buffer=self._output_memory.buf,
        )
        context = multiprocessing.get_context("spawn")
        self._connection, self._child_connection = context.Pipe(duplex=True)
        worker_settings=dict(settings)
        if pool:worker_settings['guidance_cache_pool']=pool.name
        arguments=(
                self._child_connection, self.width, self.height, worker_settings,
                self._input_memory.name, self._output_memory.name,
                self.log_path, live_factory,
            )
        self._process = context.Process(
            target=worker_with_memory_pool if pool else _host_worker_main,
            args=(_host_worker_main,pool.name,arguments) if pool else arguments,
            name="dlss-host-%s" % settings.get("host_backend", "auto"),
            daemon=True,
        )
        self._process.start()
        self._child_connection.close()
        self._child_connection = None
        response = self._receive(_CREATE_TIMEOUT, "initialize")
        self._check_response(response)
        self._apply_metadata(response)

    def _worker_description(self):
        if self._process is None:
            return "未启动"
        return "PID %s, exit=%s" % (self._process.pid, self._process.exitcode)

    def _receive(self, timeout, operation):
        if self._connection is None:
            raise HostProcessError("DLSS 工作进程连接已关闭")
        try:
            if self._connection.poll(timeout):
                return self._connection.recv()
        except (BrokenPipeError, EOFError, OSError) as exception:
            raise HostProcessError(
                "DLSS 工作进程在 %s 时断开（%s）：%s"
                % (operation, self._worker_description(), exception)
            ) from exception
        if self._process is not None and not self._process.is_alive():
            raise HostProcessError(
                "DLSS 工作进程在 %s 时异常退出（%s）"
                % (operation, self._worker_description())
            )
        raise HostProcessError(
            "DLSS 工作进程在 %s 时超过 %.0f 秒未响应"
            % (operation, timeout)
        )

    @staticmethod
    def _check_response(response):
        if response.get("ok"):
            return
        if response.get('memory_pressure'):
            record_memory_event('worker_rejected',**response['memory_pressure'])
        detail = response.get("error", "未知错误")
        trace = str(response.get("traceback", "")).strip()
        if trace:
            detail += "\n" + trace
        raise HostProcessError(detail)

    def _apply_metadata(self, response):
        if "adapter_info" in response:
            self.adapter_info = dict(response["adapter_info"])
        if 'guidance_info' in response:
            self.guidance_info = dict(response['guidance_info'])
        if 'guidance_metrics' in response:
            self.guidance_metrics = dict(response['guidance_metrics'])
        if "backend" in response:
            self.backend = str(response["backend"])
            self.max_in_flight = max(1, int(response.get("max_in_flight", 1)))
            self.supports_async = bool(response.get("supports_async", False))
            self.tiled = bool(response.get("tiled", False))

    def request(self, operation, timeout=_COMMAND_TIMEOUT, **payload):
        if self._closed:
            raise HostProcessError("DLSS 工作进程已关闭")
        if self._process is None or not self._process.is_alive():
            # An idle host may have retired under memory pressure. Preserve its
            # reported reason instead of replacing it with a generic dead PID.
            if self._connection is not None and self._connection.poll(0):
                response = self._receive(0, operation)
                self._check_response(response)
            raise HostProcessError(
                "DLSS 工作进程不可用（%s）" % self._worker_description()
            )
        try:
            self._connection.send({"operation": operation, **payload})
        except (BrokenPipeError, EOFError, OSError) as exception:
            raise HostProcessError(
                "无法向 DLSS 工作进程发送 %s：%s" % (operation, exception)
            ) from exception
        response = self._receive(timeout, operation)
        self._check_response(response)
        self._apply_metadata(response)
        return response

    def copy_input(self, rgba):
        if rgba.dtype != self.dtype or rgba.shape != self._input_frame.shape:
            raise ValueError(
                "RGBA frame must be %s with shape %s, got %s/%s"
                % (np.dtype(self.dtype).name, self._input_frame.shape, rgba.shape, rgba.dtype)
            )
        require_memory_headroom(rgba.nbytes,stage='nr_input')
        np.copyto(self._input_frame, rgba)

    def copy_output(self):
        with memory_allocation(self._output_frame.nbytes,stage='nr_output'):
            return self._output_frame.copy()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._process is not None and self._process.is_alive():
            try:
                self._connection.send({"operation": "stop"})
                if self._connection.poll(_STOP_TIMEOUT):
                    self._connection.recv()
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._process.join(timeout=_STOP_TIMEOUT)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=_STOP_TIMEOUT)
            if self._process.is_alive() and hasattr(self._process, "kill"):
                self._process.kill()
                self._process.join(timeout=_STOP_TIMEOUT)
            if self._process.is_alive():
                self._closed = False
                raise HostProcessError('DLSS 工作进程尚未退出，暂不释放共享帧，请重试关闭')
        if self._process is not None and self._process.pid is not None:
            self._process.join(timeout=0)
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:
                pass
            self._connection = None
        if self._child_connection is not None:
            try:
                self._child_connection.close()
            except OSError:
                pass
            self._child_connection = None
        self._input_frame = None
        self._output_frame = None
        for memory in (self._input_memory, self._output_memory):
            if memory is None:
                continue
            try:
                memory.close()
            except (BufferError, OSError):
                pass
            try:
                memory.unlink()
            except (FileNotFoundError, OSError):
                pass
        self._input_memory = None
        self._output_memory = None
        if self._process is not None and not self._process.is_alive():
            try:
                self._process.close()
            except (OSError, ValueError):
                pass
        self._process = None
        try:
            os.remove(self.log_path)
        except OSError:
            pass


class _BypassSession:
    """Owned synchronous result, without a host, guidance or IPC buffers."""
    backend = 'bypass'
    adapter_info = {}
    max_in_flight = 1
    supports_async = False
    tiled = False

    def __init__(self, width, height, settings, reason):
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError('Frame dimensions must be positive')
        self.shape = (int(height), int(width), 4)
        self.settings = dict(settings)
        self.reason = reason
        self._closed = False
        self._input = None

    def copy_input(self, rgba):
        if self._closed:
            raise HostProcessError('DLSS session is closed')
        if rgba.shape != self.shape or rgba.dtype != dlss_engine.frame_dtype(self.settings):
            raise ValueError('Input does not match the frame contract')
        self._input = rgba

    def copy_output(self):
        try:
            return appearance.bypass_frame(self._input, self.reason)
        finally:
            self._input = None

    def process(self, rgba, reset=False):
        self.copy_input(rgba)
        return self.copy_output()

    def close_guidance(self):
        pass  # No guidance resources were created.

    def request(self, operation, **payload):
        if self._closed:
            raise HostProcessError('DLSS session is closed')
        if operation == 'process':
            return {'has_output': True}
        if operation == 'pending':
            return {'pending': 0}
        if operation == 'update':
            self.settings = dict(payload['settings'])
            return {}
        raise HostProcessError('Zero-effect bypass has no native GPU output')

    def close(self):
        self._closed = True
        self._input = None


class ProcessLive:
    """Drop-in Live-compatible proxy with transaction-like backend replacement."""

    def __init__(self, width, height, settings=None, _live_factory=None, _on_guidance_ready=None):
        self._on_guidance_ready = _on_guidance_ready
        self._notified_guidance_session = None
        self._w = int(width)
        self._h = int(height)
        self.settings = guidance_client.normalize_public_settings(settings)
        self.preference = str(self.settings.get("host_backend", "auto"))
        self._live_factory = _live_factory
        self._reset_next = True
        self._retiring_sessions = []
        self._session = self._make_session(self._w, self._h, self.settings)
        self._sync_metadata()

    def _make_session(self, width, height, settings):
        reason = appearance.enhancement_bypass(settings)
        if reason is not None:
            return _BypassSession(width, height, settings, reason)
        return self._start_session(width, height, settings)

    @property
    def bypass_reason(self):
        return getattr(self._session, 'reason', None)

    @property
    def enhancement_bypassed(self):
        return self.bypass_reason is not None

    @staticmethod
    def _auto_candidates(settings):
        if not os.path.isfile(dlss_engine.HOST_DLL_V2):
            return ["legacy"]
        if dlss_engine.frame_format_id(settings) == 1:
            return ["v2"]
        candidates = ["v2"]
        if (
            bool(settings.get("host_auto_fallback", True))
            and settings.get("render_gpu", dlss_engine.RENDER_GPU_AUTO)
                == dlss_engine.RENDER_GPU_AUTO
        ):
            candidates.append("legacy")
        return candidates

    def _start_session(self, width, height, settings):
        preference = str(settings.get("host_backend", "auto"))
        candidates = (
            self._auto_candidates(settings)
            if preference == "auto" else [preference]
        )
        failures = []
        for backend in candidates:
            worker_settings = dict(settings)
            # Make fallback process-safe: each worker loads exactly one host DLL.
            worker_settings["host_backend"] = backend
            try:
                return _HostSession(
                    width, height, worker_settings,
                    live_factory=self._live_factory,
                )
            except MemoryError:
                # RAM exhaustion must not trigger another backend allocation.
                raise
            except Exception as exception:
                failures.append("%s: %s" % (backend, exception))
                if backend == "v2" and dlss_engine.is_render_adapter_error(exception):
                    break
        raise HostProcessError(
            "无法启动 DLSS 后端：\n" + "\n\n".join(failures)
        )

    def _sync_metadata(self):
        self.backend = self._session.backend
        self.adapter_info = dict(getattr(self._session, "adapter_info", {}))
        self.max_in_flight = self._session.max_in_flight
        self.supports_async = self._session.supports_async
        self.tiled = self._session.tiled

    def _requires_replacement(self, new_preference):
        if new_preference == self.preference:
            return False
        if new_preference == "auto":
            desired = self._auto_candidates(self.settings)[0]
            return desired != self.backend
        return new_preference != self.backend

    def _retry_retiring(self):
        pending = getattr(self, '_retiring_sessions', [])
        self._retiring_sessions = []
        failure = None
        for session in pending:
            try:
                session.close()
            except Exception as error:
                self._retiring_sessions.append(session)
                if failure is None:
                    failure = ErrorSnapshot(error)
        if failure is not None:
            raise failure.exception()

    def _replace(self, width, height, settings):
        if self._session is None:
            raise HostProcessError('DLSS 会话正在关闭，请先重试关闭')
        self._retry_retiring()
        # Build first so a failed backend switch leaves the current session usable.
        replacement = self._make_session(width, height, settings)
        previous = self._session
        self._session = replacement
        self._w, self._h = int(width), int(height)
        self.settings = guidance_client.normalize_public_settings(settings)
        self.preference = str(self.settings.get("host_backend", "auto"))
        self._sync_metadata()
        self._reset_next = True
        self._retiring_sessions.append(previous)
        self._retry_retiring()

    def update(self, settings):
        updated = dict(self.settings)
        updated.update(settings or {})
        updated = guidance_client.normalize_public_settings(updated)
        new_preference = str(updated.get("host_backend", self.preference))
        bypass_changed = self.bypass_reason != appearance.enhancement_bypass(updated)
        if bypass_changed and self.pending:
            raise HostProcessError('Drain pending frames before changing enhancement bypass')
        if (
            bypass_changed or self._requires_replacement(new_preference)
            or dlss_engine.frame_contract(updated) != dlss_engine.frame_contract(self.settings)
            or guidance_client.contract(updated) != guidance_client.contract(self.settings)
            or updated.get("dlss_runtime", "") != self.settings.get("dlss_runtime", "")
            or updated.get("render_gpu", dlss_engine.RENDER_GPU_AUTO)
                != self.settings.get("render_gpu", dlss_engine.RENDER_GPU_AUTO)
        ):
            self._replace(self._w, self._h, updated)
            return
        worker_settings = dict(updated)
        worker_settings["host_backend"] = self.backend
        self._session.request("update", settings=worker_settings)
        self.settings = updated
        self.preference = new_preference
        self._sync_metadata()

    def resize(self, width, height, preset=None, settings=None):
        updated = dict(self.settings)
        updated.update(settings or {})
        updated = guidance_client.normalize_public_settings(updated)
        if preset is not None:
            updated["preset"] = int(preset)
        self._replace(int(width), int(height), updated)

    def process(self, rgba, reset=False):
        self._session.copy_input(rgba)
        response = self._session.request("process", reset=bool(reset or self._reset_next))
        self._reset_next = not response['has_output']
        self._notify_guidance_ready()
        return self._session.copy_output() if response["has_output"] else None

    def gpu_output_create(self):
        return self._session.request('gpu_output_create')['descriptor']

    def process_device(self,rgba,reset=False):
        if self.enhancement_bypassed:
            raise HostProcessError('Zero-effect bypass has no native GPU output')
        self._session.copy_input(rgba)
        response=self._session.request('process_gpu_output',reset=bool(reset or self._reset_next))
        self._reset_next = not response['has_output']
        self._notify_guidance_ready()
        if not response['has_output']:raise HostProcessError('DLSS GPU output failed')

    def guidance_preview(self, rgba, previous=None):
        """Read an exact adjacent-frame pair; return owned, bounded display arrays.

        The caller serializes access with normal preview/export and forces the
        next DLSS frame to reset, because this advances the component history.
        """
        if max(rgba.shape[:2]) > 1280:
            raise ValueError('Guidance preview exceeds inference limit')
        # Explicit guidance inspection is separate from invisible enhancement
        # work. Do not return fabricated zero flow for a bypassed output session.
        temporary = self.enhancement_bypassed
        session = self._start_session(self._w, self._h, self.settings) if temporary else self._session
        try:
            if previous is not None:
                session.copy_input(previous)
                session.request('guidance_preview', reset=True, display=False)
            session.copy_input(rgba)
            response = session.request('guidance_preview', reset=previous is None, display=True)
            if temporary:
                info = dict(getattr(session, 'guidance_info', {}))
                if info and self._on_guidance_ready:
                    self._on_guidance_ready(info)
            else:
                self._notify_guidance_ready()
            return response['images'], response['reset']
        finally:
            self._reset_next = True
            if temporary:
                session.close()

    def enqueue(self, rgba, reset=False):
        if not self.supports_async:
            raise RuntimeError("当前主机设置不支持异步帧队列")
        self._session.copy_input(rgba)
        response = self._session.request("enqueue", reset=bool(reset or self._reset_next))
        if response['accepted']:
            self._reset_next = False
        self._notify_guidance_ready()
        return bool(response["accepted"])

    def dequeue(self):
        if not self.supports_async:
            raise RuntimeError("当前主机设置不支持异步帧队列")
        response = self._session.request("dequeue")
        return self._session.copy_output() if response["has_output"] else None

    @property
    def pending(self):
        return int(self._session.request("pending")["pending"])

    @property
    def guidance_info(self):
        return dict(getattr(self._session, 'guidance_info', {})) if self._session else {}

    @property
    def guidance_metrics(self):
        return dict(getattr(self._session, 'guidance_metrics', {})) if self._session else {}

    def _notify_guidance_ready(self):
        info = self.guidance_info
        if info and self._notified_guidance_session is not self._session:
            self._notified_guidance_session = self._session
            if self._on_guidance_ready:
                self._on_guidance_ready(info)

    def close(self):
        failure = None
        try:
            self._retry_retiring()
        except Exception as error:
            failure = ErrorSnapshot(error)
        if getattr(self, "_session", None) is not None:
            try:
                self._session.close()
            except Exception as error:
                if failure is None:
                    failure = ErrorSnapshot(error)
            else:
                self._session = None
        if failure is not None:
            raise failure.exception()
