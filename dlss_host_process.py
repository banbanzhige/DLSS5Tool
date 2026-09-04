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
import os
import tempfile
import traceback
import uuid

import numpy as np

import dlss_engine


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
    }


def _metadata(live):
    return {
        "backend": live.backend,
        "max_in_flight": int(live.max_in_flight),
        "supports_async": bool(live.supports_async),
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
            message = connection.recv()
        except (EOFError, OSError):
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
                })
            elif operation == "enqueue":
                accepted = live.enqueue(
                    input_frame, reset=bool(message.get("reset")),
                )
                connection.send({
                    "ok": True, "operation": operation,
                    "accepted": bool(accepted),
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
        self.width = int(width)
        self.height = int(height)
        self._closed = False
        self._process = None
        self._connection = None
        self._input_memory = None
        self._output_memory = None
        self._input_frame = None
        self._output_frame = None
        self.backend = "unknown"
        self.max_in_flight = 1
        self.supports_async = False
        self.dtype = dlss_engine.frame_dtype(settings)
        self.log_path = os.path.join(
            tempfile.gettempdir(), "dlss5tool-host-%s.log" % uuid.uuid4().hex,
        )

        frame_bytes = self.width * self.height * 4 * np.dtype(self.dtype).itemsize
        if frame_bytes <= 0:
            raise ValueError("DLSS 帧尺寸必须大于零")
        try:
            self._input_memory = _open_shared_memory(
                create=True, size=frame_bytes, track=True,
            )
            self._output_memory = _open_shared_memory(
                create=True, size=frame_bytes, track=True,
            )
            self._input_frame = np.ndarray(
                (self.height, self.width, 4), dtype=self.dtype,
                buffer=self._input_memory.buf,
            )
            self._output_frame = np.ndarray(
                (self.height, self.width, 4), dtype=self.dtype,
                buffer=self._output_memory.buf,
            )
            context = multiprocessing.get_context("spawn")
            parent_connection, child_connection = context.Pipe(duplex=True)
            self._connection = parent_connection
            self._process = context.Process(
                target=_host_worker_main,
                args=(
                    child_connection, self.width, self.height, dict(settings),
                    self._input_memory.name, self._output_memory.name,
                    self.log_path, live_factory,
                ),
                name="dlss-host-%s" % settings.get("host_backend", "auto"),
                daemon=True,
            )
            self._process.start()
            child_connection.close()
            response = self._receive(_CREATE_TIMEOUT, "initialize")
            self._check_response(response)
            self._apply_metadata(response)
        except BaseException:
            self.close()
            raise

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
        detail = response.get("error", "未知错误")
        trace = str(response.get("traceback", "")).strip()
        if trace:
            detail += "\n" + trace
        raise HostProcessError(detail)

    def _apply_metadata(self, response):
        if "backend" in response:
            self.backend = str(response["backend"])
            self.max_in_flight = max(1, int(response.get("max_in_flight", 1)))
            self.supports_async = bool(response.get("supports_async", False))

    def request(self, operation, timeout=_COMMAND_TIMEOUT, **payload):
        if self._closed:
            raise HostProcessError("DLSS 工作进程已关闭")
        if self._process is None or not self._process.is_alive():
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
        np.copyto(self._input_frame, rgba)

    def copy_output(self):
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
        if self._process is not None and not self._process.is_alive():
            self._process.join(timeout=0)
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:
                pass
            self._connection = None
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


class ProcessLive:
    """Drop-in Live-compatible proxy with transaction-like backend replacement."""

    def __init__(self, width, height, settings=None, _live_factory=None):
        self._w = int(width)
        self._h = int(height)
        self.settings = dict(settings or {})
        self.preference = str(self.settings.get("host_backend", "auto"))
        self._live_factory = _live_factory
        self._session = self._start_session(self._w, self._h, self.settings)
        self._sync_metadata()

    @staticmethod
    def _auto_candidates(settings):
        if not os.path.isfile(dlss_engine.HOST_DLL_V2):
            return ["legacy"]
        if dlss_engine.frame_format_id(settings) == 1:
            return ["v2"]
        candidates = ["v2"]
        if bool(settings.get("host_auto_fallback", True)):
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
            except Exception as exception:
                failures.append("%s: %s" % (backend, exception))
        raise HostProcessError(
            "无法启动 DLSS 后端：\n" + "\n\n".join(failures)
        )

    def _sync_metadata(self):
        self.backend = self._session.backend
        self.max_in_flight = self._session.max_in_flight
        self.supports_async = self._session.supports_async

    def _requires_replacement(self, new_preference):
        if new_preference == self.preference:
            return False
        if new_preference == "auto":
            desired = self._auto_candidates(self.settings)[0]
            return desired != self.backend
        return new_preference != self.backend

    def _replace(self, width, height, settings):
        # Build first so a failed backend switch leaves the current session usable.
        replacement = self._start_session(width, height, settings)
        previous = self._session
        self._session = replacement
        self._w, self._h = int(width), int(height)
        self.settings = dict(settings)
        self.preference = str(self.settings.get("host_backend", "auto"))
        self._sync_metadata()
        previous.close()

    def update(self, settings):
        updated = dict(self.settings)
        updated.update(settings or {})
        new_preference = str(updated.get("host_backend", self.preference))
        if (
            self._requires_replacement(new_preference)
            or dlss_engine.frame_contract(updated) != dlss_engine.frame_contract(self.settings)
        ):
            self._replace(self._w, self._h, updated)
            return
        worker_settings = dict(updated)
        worker_settings["host_backend"] = self.backend
        self._session.request("update", settings=worker_settings)
        self.settings = updated
        self.preference = new_preference
        self._sync_metadata()

    def resize(self, width, height, preset=None):
        updated = dict(self.settings)
        if preset is not None:
            updated["preset"] = int(preset)
        self._replace(int(width), int(height), updated)

    def process(self, rgba, reset=False):
        self._session.copy_input(rgba)
        response = self._session.request("process", reset=bool(reset))
        return self._session.copy_output() if response["has_output"] else None

    def enqueue(self, rgba, reset=False):
        if not self.supports_async:
            raise RuntimeError("当前主机设置不支持异步帧队列")
        self._session.copy_input(rgba)
        response = self._session.request("enqueue", reset=bool(reset))
        return bool(response["accepted"])

    def dequeue(self):
        if not self.supports_async:
            raise RuntimeError("当前主机设置不支持异步帧队列")
        response = self._session.request("dequeue")
        return self._session.copy_output() if response["has_output"] else None

    @property
    def pending(self):
        return int(self._session.request("pending")["pending"])

    def close(self):
        if getattr(self, "_session", None) is not None:
            self._session.close()
            self._session = None
