"""CPU-only unittest suite for ProcessSuperResolution reconfiguration, multi-GPU VRAM cap, GUI wiring, and profiler CLI."""
import ctypes
import json
from multiprocessing import shared_memory
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

from dlss5tool import super_resolution as sr


class FakeVsrLibrary:
    """In-memory stand-in for vsr_host.dll running inside a real spawned worker."""

    def __init__(self, *, supports_resize=True, fail_resize_sizes=()):
        self.supports_resize = supports_resize
        self.fail_resize_sizes = set(fail_resize_sizes)
        self.width = 0
        self.height = 0
        self.scale = 2
        self.is_hdr = False
        self.init_calls = 0
        self.resize_calls = 0
        if supports_resize:
            self.vsr_resize = self._vsr_resize

    def vsr_init(self, width, height, scale, quality, hdr, runtime_dir, log_path):
        self.init_calls += 1
        self.width, self.height, self.scale, self.is_hdr = int(width), int(height), int(scale), bool(hdr)
        return 1

    def vsr_init_adapter(self, width, height, scale, quality, hdr, runtime_dir, log_path, high, low):
        return self.vsr_init(width, height, scale, quality, hdr, runtime_dir, log_path)

    def vsr_adapter_luid(self, high_ptr, low_ptr):
        high_ptr._obj.value = 0
        low_ptr._obj.value = 42
        return 1

    def vsr_hdr_gpu_conversion(self):
        return 1 if self.is_hdr and (self.width * self.scale * self.height * self.scale) >= 64 else 0

    def _vsr_resize(self, width, height, scale, quality, hdr):
        self.resize_calls += 1
        if (int(width), int(height), int(scale), bool(hdr)) in self.fail_resize_sizes:
            return 0
        self.width, self.height, self.scale, self.is_hdr = int(width), int(height), int(scale), bool(hdr)
        return 1

    def vsr_process(self, input_ptr, output_ptr):
        dtype = np.float16 if self.is_hdr else np.uint8
        in_count = self.height * self.width * 4
        out_h, out_w = self.height * self.scale, self.width * self.scale
        out_count = out_h * out_w * 4
        ctype = ctypes.c_uint16 if self.is_hdr else ctypes.c_uint8
        src_raw = np.ctypeslib.as_array((ctype * in_count).from_address(int(input_ptr.value))).view(dtype)
        dst_raw = np.ctypeslib.as_array((ctype * out_count).from_address(int(output_ptr.value))).view(dtype)
        src = src_raw.reshape((self.height, self.width, 4))
        dst = dst_raw.reshape((out_h, out_w, 4))
        upscaled = np.repeat(np.repeat(src, self.scale, axis=0), self.scale, axis=1)
        if self.is_hdr:
            dst[...] = upscaled + np.float16(0.25)
        else:
            dst[...] = (upscaled.astype(np.uint16) + self.scale).astype(np.uint8)
        return 1

    def vsr_shutdown(self):
        return None


def fake_worker_with_resize(connection, width, height, scale, is_hdr, input_name, output_name, log_path, options=None):
    lib = FakeVsrLibrary(supports_resize=True)
    with mock.patch.object(sr, "_load_host", return_value=lib):
        sr._worker_main(connection, width, height, scale, is_hdr, input_name, output_name, log_path, options)


def fake_worker_legacy(connection, width, height, scale, is_hdr, input_name, output_name, log_path, options=None):
    lib = FakeVsrLibrary(supports_resize=False)
    with mock.patch.object(sr, "_load_host", return_value=lib):
        sr._worker_main(connection, width, height, scale, is_hdr, input_name, output_name, log_path, options)


def fake_worker_failing_resize(connection, width, height, scale, is_hdr, input_name, output_name, log_path, options=None):
    lib = FakeVsrLibrary(supports_resize=True, fail_resize_sizes={(10, 8, 2, False)})
    with mock.patch.object(sr, "_load_host", return_value=lib):
        sr._worker_main(connection, width, height, scale, is_hdr, input_name, output_name, log_path, options)


def fake_worker_pressure_on_resize(connection, width, height, scale, is_hdr, input_name, output_name, log_path, options=None):
    class PressureLib(FakeVsrLibrary):
        def _vsr_resize(self, w, h, s, q, hdr):
            err = MemoryError("simulated worker RAM pressure")
            err.details = {"stage": "vsr_resize", "required_bytes": 1234}
            raise err

    lib = PressureLib(supports_resize=True)
    with mock.patch.object(sr, "_load_host", return_value=lib):
        sr._worker_main(connection, width, height, scale, is_hdr, input_name, output_name, log_path, options)


def fake_worker_slow_reconfigure(connection, width, height, scale, is_hdr, input_name, output_name, log_path, options=None):
    class SlowResizeLib(FakeVsrLibrary):
        def _vsr_resize(self, w, h, s, q, hdr):
            time.sleep(0.15)
            return super()._vsr_resize(w, h, s, q, hdr)

    lib = SlowResizeLib(supports_resize=True)
    with mock.patch.object(sr, "_load_host", return_value=lib):
        sr._worker_main(connection, width, height, scale, is_hdr, input_name, output_name, log_path, options)


class ProcessSuperResolutionReconfigureTests(unittest.TestCase):
    def test_reconfigure_reuses_worker_buffers_and_switches_scale_and_hdr(self):
        with mock.patch.object(sr, "_worker_main", fake_worker_with_resize), \
                mock.patch.object(sr, "query_gpu_memory", return_value={"free_bytes": 8 * sr._GIB}):
            session = sr.ProcessSuperResolution(8, 6, 2, is_hdr=False)
            try:
                self.assertTrue(session.supports_reconfigure)
                pid = session._process.pid
                names = (session._input_memory.name, session._output_memory.name)

                out0 = session.process(np.full((6, 8, 4), 10, np.uint8))
                self.assertEqual(out0.shape, (12, 16, 4))
                np.testing.assert_array_equal(out0, 12)

                # Shrink: reuses same worker PID and exact same SharedMemory segments.
                session.reconfigure(6, 4, 2, is_hdr=False)
                self.assertEqual(session._process.pid, pid)
                self.assertEqual((session._input_memory.name, session._output_memory.name), names)
                out1 = session.process(np.full((4, 6, 4), 20, np.uint8))
                self.assertEqual(out1.shape, (8, 12, 4))
                np.testing.assert_array_equal(out1, 22)

                # Expand to 4x HDR: still same worker PID, allocates larger buffers, unlinks old ones.
                session.reconfigure(10, 8, 4, is_hdr=True)
                self.assertEqual(session._process.pid, pid)
                self.assertNotEqual(session._input_memory.name, names[0])
                self.assertNotEqual(session._output_memory.name, names[1])
                self.assertEqual(session.adapter_info.get("hdr_conversion"), "gpu-rgb10a2")
                out2 = session.process(np.full((8, 10, 4), 0.5, np.float16))
                self.assertEqual(out2.shape, (32, 40, 4))
                np.testing.assert_allclose(out2, 0.75, atol=1e-3)
                for old_name in names:
                    with self.assertRaises(FileNotFoundError):
                        shared_memory.SharedMemory(name=old_name)

                # No-op reconfigure keeps same PID and generation.
                gen = session._generation
                session.reconfigure(10, 8, 4, is_hdr=True)
                self.assertEqual(session._process.pid, pid)
                self.assertEqual(session._generation, gen)

                # Switch back to small HDR (CPU conversion) and SDR.
                session.reconfigure(2, 2, 2, is_hdr=True)
                self.assertEqual(session._process.pid, pid)
                self.assertEqual(session.adapter_info.get("hdr_conversion"), "cpu-rgb10a2")
                session.reconfigure(8, 6, 2, is_hdr=False)
                self.assertEqual(session._process.pid, pid)
                self.assertEqual(session.adapter_info.get("hdr_conversion"), "direct-rgba8")
            finally:
                session.close()

    def test_legacy_host_without_vsr_resize_falls_back_to_replace(self):
        with mock.patch.object(sr, "_worker_main", fake_worker_legacy):
            session = sr.ProcessSuperResolution(8, 6, 2, is_hdr=False)
            try:
                self.assertFalse(session.supports_reconfigure)
                pid0 = session._process.pid
                session.reconfigure(6, 4, 2, is_hdr=False)
                self.assertNotEqual(session._process.pid, pid0)
                out = session.process(np.full((4, 6, 4), 5, np.uint8))
                self.assertEqual(out.shape, (8, 12, 4))
                np.testing.assert_array_equal(out, 7)
            finally:
                session.close()

    def test_native_resize_failure_retires_old_worker_and_recovers_cleanly(self):
        with mock.patch.object(sr, "_worker_main", fake_worker_failing_resize), \
                mock.patch.object(sr, "query_gpu_memory", return_value={"free_bytes": 8 * sr._GIB}):
            session = sr.ProcessSuperResolution(8, 6, 2, is_hdr=False)
            try:
                pid0 = session._process.pid
                old_names = (session._input_memory.name, session._output_memory.name)
                session.reconfigure(10, 8, 2, is_hdr=False)
                self.assertNotEqual(session._process.pid, pid0)
                self.assertFalse(session._closed)
                self.assertFalse(session._failed)
                out = session.process(np.full((8, 10, 4), 9, np.uint8))
                self.assertEqual(out.shape, (16, 20, 4))
                np.testing.assert_array_equal(out, 11)
                for old_name in old_names:
                    with self.assertRaises(FileNotFoundError):
                        shared_memory.SharedMemory(name=old_name)
            finally:
                session.close()

    def test_invalid_dimensions_and_memory_pressure_keep_live_session_intact(self):
        with mock.patch.object(sr, "_worker_main", fake_worker_with_resize), \
                mock.patch.object(sr, "query_gpu_memory", return_value={"free_bytes": 8 * sr._GIB}):
            session = sr.ProcessSuperResolution(8, 6, 2, is_hdr=False)
            try:
                pid = session._process.pid
                with self.assertRaises(sr.SuperResolutionError):
                    session.reconfigure(0, 6, 2)
                with self.assertRaises(sr.SuperResolutionError):
                    session.reconfigure(10000, 10000, 2)
                with self.assertRaises(ValueError):
                    session.reconfigure(8, 6, 3)
                with mock.patch("dlss5tool.memory_budget.system_memory", return_value={
                    "total_physical_bytes": 16 * sr._GIB,
                    "free_physical_bytes": 1,
                    "free_commit_bytes": 16 * sr._GIB,
                }):
                    with self.assertRaises(MemoryError):
                        session.reconfigure(16, 12, 2)
                self.assertEqual(session._process.pid, pid)
                self.assertEqual((session.width, session.height), (8, 6))
                out = session.process(np.full((6, 8, 4), 3, np.uint8))
                np.testing.assert_array_equal(out, 5)
            finally:
                session.close()

    def test_bucket_policy_reuses_aligned_buckets_and_recycles_at_cap(self):
        with mock.patch.object(sr, "_worker_main", fake_worker_with_resize), \
                mock.patch.object(sr, "query_gpu_memory", return_value={"free_bytes": 8 * sr._GIB}), \
                mock.patch.object(sr, "_VSR_MAX_CACHED_BUCKETS", 2):
            session = sr.ProcessSuperResolution(32, 32, 2, is_hdr=False)
            try:
                pid0 = session._process.pid
                # Same 16px-aligned bucket (32->2, 24->2): stays in 1 bucket, no recycle.
                session.reconfigure(24, 24, 2, is_hdr=False)
                self.assertEqual(session._process.pid, pid0)
                self.assertEqual(len(session._cached_buckets), 1)

                # Second distinct bucket (48->3): allowed up to cap=2.
                session.reconfigure(48, 32, 2, is_hdr=False)
                self.assertEqual(session._process.pid, pid0)
                self.assertEqual(len(session._cached_buckets), 2)

                # Revisiting bucket 1: still allowed, no recycle.
                session.reconfigure(32, 32, 2, is_hdr=False)
                self.assertEqual(session._process.pid, pid0)

                # Third distinct bucket (64->4): exceeds cap=2 -> recycles worker cleanly.
                session.reconfigure(64, 32, 2, is_hdr=False)
                self.assertNotEqual(session._process.pid, pid0)
                self.assertEqual(len(session._cached_buckets), 1)
            finally:
                session.close()

    def test_vram_workspace_cap_triggers_clean_recycle(self):
        with mock.patch.object(sr, "_worker_main", fake_worker_with_resize), \
                mock.patch.object(sr, "query_gpu_memory", return_value={"free_bytes": 500 * sr._MIB}), \
                mock.patch.object(sr, "_estimate_bucket_workspace_bytes", return_value=120 * sr._MIB):
            session = sr.ProcessSuperResolution(32, 32, 2, is_hdr=False)
            try:
                pid0 = session._process.pid
                # 500 MiB * 35% = 175 MiB cap; new bucket projects 240 MiB -> recycle.
                session.reconfigure(64, 32, 2, is_hdr=False)
                self.assertNotEqual(session._process.pid, pid0)
                self.assertEqual(session._cached_workspace_bytes, 120 * sr._MIB)
            finally:
                session.close()

    def test_memory_reuse_helper_shrinks_oversized_buffers(self):
        small = mock.Mock(size=16 * sr._MIB)
        huge = mock.Mock(size=256 * sr._MIB)
        self.assertTrue(sr._reusable_memory(small, 12 * sr._MIB))
        self.assertFalse(sr._reusable_memory(small, 20 * sr._MIB))
        self.assertTrue(sr._reusable_memory(huge, 128 * sr._MIB))
        self.assertFalse(sr._reusable_memory(huge, 16 * sr._MIB))

    def test_partial_candidate_failure_cleans_up_and_preserves_live_session(self):
        with mock.patch.object(sr, "_worker_main", fake_worker_with_resize), \
                mock.patch.object(sr, "query_gpu_memory", return_value={"free_bytes": 8 * sr._GIB}):
            session = sr.ProcessSuperResolution(8, 6, 2, is_hdr=False)
            real_shm = shared_memory.SharedMemory
            created = []

            def failing_second_shm(**kwargs):
                if len(created) == 1:
                    raise MemoryError("second candidate shm failed")
                mem = real_shm(**kwargs)
                created.append(mem)
                return mem

            try:
                pid = session._process.pid
                with mock.patch.object(sr.shared_memory, "SharedMemory", side_effect=failing_second_shm):
                    with self.assertRaises(MemoryError):
                        session.reconfigure(16, 12, 2, is_hdr=False)
                self.assertEqual(len(created), 1)
                with self.assertRaises(FileNotFoundError):
                    real_shm(name=created[0].name)
                self.assertEqual(session._process.pid, pid)
                out = session.process(np.full((6, 8, 4), 11, np.uint8))
                np.testing.assert_array_equal(out, 13)
            finally:
                session.close()

    def test_worker_memory_pressure_on_reconfigure_retires_without_retry(self):
        with mock.patch.object(sr, "_worker_main", fake_worker_pressure_on_resize), \
                mock.patch.object(sr, "query_gpu_memory", return_value={"free_bytes": 8 * sr._GIB}):
            session = sr.ProcessSuperResolution(8, 6, 2, is_hdr=False)
            try:
                with mock.patch.object(session, "_replace") as replace:
                    with self.assertRaises(sr.SuperResolutionError) as ctx:
                        session.reconfigure(16, 12, 2, is_hdr=False)
                    self.assertIsNotNone(ctx.exception.memory_pressure)
                    replace.assert_not_called()
                self.assertTrue(session._closed)
                self.assertIsNone(session._process)
            finally:
                session.close()

    def test_late_reconfigure_ack_after_failed_close_cannot_return_stale_pixels(self):
        """P2 regression: failed reconfigure + failed close must poison session and reject mismatched ACKs."""
        with mock.patch.object(sr, "_worker_main", fake_worker_slow_reconfigure), \
                mock.patch.object(sr, "query_gpu_memory", return_value={"free_bytes": 8 * sr._GIB}):
            session = sr.ProcessSuperResolution(8, 6, 2, is_hdr=False)
            try:
                first = session.process(np.full((6, 8, 4), 7, np.uint8))
                np.testing.assert_array_equal(first, 9)

                # Simulate reconfigure timeout followed by failed close (unkillable worker).
                real_close = session.close
                with mock.patch.object(
                    session, "_receive", side_effect=sr.SuperResolutionError("timeout waiting for reconfigure"),
                ), mock.patch.object(
                    session, "close", side_effect=sr.SuperResolutionError("worker did not retire"),
                ):
                    with self.assertRaises(sr.SuperResolutionError):
                        session.reconfigure(6, 4, 2, is_hdr=False)

                # Wait for worker to finish vsr_resize and enqueue its late reconfigure ACK on the pipe.
                time.sleep(0.25)
                self.assertTrue(session._failed)
                self.assertFalse(session._closed)

                # Session must refuse frame processing instead of reading the late reconfigure ACK.
                with self.assertRaises(sr.SuperResolutionError):
                    session.process(np.full((6, 8, 4), 50, np.uint8))

                # Even if _receive('process') were called directly, mismatched operation must be rejected.
                with self.assertRaises(sr.SuperResolutionError):
                    session._receive("process")
            finally:
                real_close()


class MultiGpuMemorySelectionTests(unittest.TestCase):
    def setUp(self):
        sr._GPU_QUERY_CACHE = None
        sr._GPU_QUERY_LIST_CACHE = None

    def tearDown(self):
        sr._GPU_QUERY_CACHE = None
        sr._GPU_QUERY_LIST_CACHE = None

    def test_workspace_cache_limit_distinguishes_zero_free_vram_from_query_failure(self):
        self.assertEqual(sr._workspace_cache_limit_bytes(None), sr._VSR_MAX_CACHED_WORKSPACE_BYTES)
        self.assertEqual(sr._workspace_cache_limit_bytes({}), sr._VSR_MAX_CACHED_WORKSPACE_BYTES)
        self.assertEqual(sr._workspace_cache_limit_bytes({"free_bytes": 0}), 0)
        self.assertEqual(
            sr._workspace_cache_limit_bytes({"free_bytes": 128 * sr._MIB}),
            int(128 * sr._MIB * 0.35),
        )

    def test_multi_gpu_query_selects_active_adapter_and_recycles_on_low_second_gpu(self):
        multi_output = (
            "GPU-11111111-1111-1111-1111-111111111111, 00000000:01:00.0, NVIDIA GeForce RTX 4090, 24564, 8000\n"
            "GPU-22222222-2222-2222-2222-222222222222, 00000000:02:00.0, NVIDIA GeForce RTX 4090, 24564, 128\n"
        ).encode("utf-8")
        completed = mock.Mock(stdout=multi_output)

        with mock.patch.object(sr.subprocess, "run", return_value=completed):
            # Default (no adapter_info) returns first valid GPU for backward compatibility.
            gpu0 = sr.query_gpu_memory(cache_seconds=0)
            self.assertEqual(gpu0["free_bytes"], 8000 * sr._MIB)

            # Matching by UUID selects the exact card even when model names are identical.
            gpu1_uuid = sr.query_gpu_memory(
                cache_seconds=5.0,
                adapter_info={"uuid": "GPU-22222222-2222-2222-2222-222222222222"},
            )
            self.assertEqual(gpu1_uuid["free_bytes"], 128 * sr._MIB)

            # Matching second GPU by CUDA-style 4-digit PCI bus ID picks GPU 1 (128 MiB).
            gpu1_pci = sr.query_gpu_memory(cache_seconds=5.0, adapter_info={"pci_bus_id": "0000:02:00.0"})
            self.assertEqual(gpu1_pci["free_bytes"], 128 * sr._MIB)

            # When PCI/UUID is missing and CUDA order differs from NVML order:
            # - default query (conservative_fallback=False) returns None rather than guessing another card;
            # - opt-in conservative_fallback=True (used by VSR recycling) takes the lowest valid free VRAM (128 MiB).
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=5.0, adapter_info={"cuda_index": 0}))
            reordered = sr.query_gpu_memory(
                cache_seconds=5.0, adapter_info={"cuda_index": 0}, conservative_fallback=True,
            )
            self.assertEqual(reordered["free_bytes"], 128 * sr._MIB)

            # Matching auto-selected LUID via available_render_adapters resolves PCI bus ID -> 128 MiB.
            fake_adapters = [
                {"luid_high": 0, "luid_low": 100, "pci_bus_id": "0000:01:00.0", "cuda_index": 1, "name": "NVIDIA GeForce RTX 4090"},
                {"luid_high": 0, "luid_low": 200, "pci_bus_id": "0000:02:00.0", "cuda_index": 0, "name": "NVIDIA GeForce RTX 4090"},
            ]
            with mock.patch("dlss5tool.dlss_engine.available_render_adapters", return_value=fake_adapters):
                gpu1_luid = sr.query_gpu_memory(cache_seconds=5.0, adapter_info={"luid_high": 0, "luid_low": 200})
            self.assertEqual(gpu1_luid["free_bytes"], 128 * sr._MIB)

            # Verify ProcessSuperResolution recycles when running on GPU 1 (128 MiB free) or with ambiguous cuda_index.
            session = sr.ProcessSuperResolution.__new__(sr.ProcessSuperResolution)
            session._cached_buckets = {sr._vsr_bucket_key(1280, 720, 2, False)}
            session._cached_workspace_bytes = sr._estimate_bucket_workspace_bytes(1280, 720, 2)
            session.adapter_info = {"pci_bus_id": "0000:02:00.0"}
            self.assertTrue(session._should_recycle_for_bucket(1248, 720, 2, False))
            session.adapter_info = {"cuda_index": 0}
            self.assertTrue(session._should_recycle_for_bucket(1248, 720, 2, False))

            # Same session confirmed on GPU 0 (8000 MiB free) via PCI bus ID reuses.
            session.adapter_info = {"pci_bus_id": "0000:01:00.0"}
            self.assertFalse(session._should_recycle_for_bucket(1248, 720, 2, False))

    def test_non_target_gpu_na_preserves_target_gpu_vram_reading(self):
        na_output = (
            "GPU-11111111-1111-1111-1111-111111111111, 00000000:01:00.0, NVIDIA GeForce RTX 4090, [N/A], [N/A]\n"
            "GPU-22222222-2222-2222-2222-222222222222, 00000000:02:00.0, NVIDIA GeForce RTX 4070, 12282, 128\n"
        ).encode("utf-8")
        completed = mock.Mock(stdout=na_output)

        with mock.patch.object(sr.subprocess, "run", return_value=completed):
            # Target GPU 1 keeps its valid 128 MiB reading despite GPU 0 returning [N/A].
            target = sr.query_gpu_memory(cache_seconds=0, adapter_info={"pci_bus_id": "0000:02:00.0"})
            self.assertIsNotNone(target)
            self.assertEqual(target["free_bytes"], 128 * sr._MIB)

            # Recycle decision uses GPU 1's 128 MiB rather than falling back to 1.5 GiB.
            session = sr.ProcessSuperResolution.__new__(sr.ProcessSuperResolution)
            session.adapter_info = {"pci_bus_id": "0000:02:00.0"}
            session._cached_buckets = {sr._vsr_bucket_key(1280, 720, 2, False)}
            session._cached_workspace_bytes = sr._estimate_bucket_workspace_bytes(1280, 720, 2)
            self.assertTrue(session._should_recycle_for_bucket(1248, 720, 2, False))

            # Target GPU 0 itself returned [N/A]: returns None instead of borrowing GPU 1.
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=5.0, adapter_info={"pci_bus_id": "0000:01:00.0"}))

            # Default query inspects GPU 0 only: returns None rather than letting GPU 1's 128 MiB block GPU 0 exports.
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=5.0))
            self.assertIsNone(sr.cached_gpu_memory())

        # Partial reading (total=N/A, free=128 MiB): never pollutes global cache with total_bytes=None,
        # never breaks _large_image_host_settings, and still lets explicit adapter VSR query see 128 MiB.
        from dlss5tool import gui
        sr._GPU_QUERY_CACHE = None
        sr._GPU_QUERY_LIST_CACHE = None
        partial = mock.Mock(stdout=b"GPU-1, 00000000:01:00.0, RTX 4070, N/A, 128\n")
        with mock.patch.object(sr.subprocess, "run", return_value=partial):
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=0))
            self.assertIsNone(sr.cached_gpu_memory())
            explicit = sr.query_gpu_memory(
                cache_seconds=0, adapter_info={"pci_bus_id": "0000:01:00.0"}, conservative_fallback=True,
            )
            self.assertEqual(explicit["free_bytes"], 128 * sr._MIB)
            self.assertNotIn("total_bytes", explicit)
        # Even if cached_gpu_memory somehow contained total_bytes=None, _large_image_host_settings handles it cleanly.
        with mock.patch.object(gui, "cached_gpu_memory", return_value={"total_bytes": None, "free_bytes": 128 * sr._MIB}):
            planned = gui._large_image_host_settings(2560, 1440, {})
            self.assertIsInstance(planned, dict)

        # All GPUs returning N/A cleanly returns None.
        sr._GPU_QUERY_CACHE = None
        sr._GPU_QUERY_LIST_CACHE = None
        all_na = mock.Mock(stdout=b"GPU-1, 00000000:01:00.0, RTX 4090, N/A, N/A\n")
        with mock.patch.object(sr.subprocess, "run", return_value=all_na):
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=0))
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=0, adapter_info={"pci_bus_id": "0000:01:00.0"}))

    def test_dead_vsr_without_frame_attrs_reports_worker_error_before_buffer_check(self):
        owner = sr.ProcessSuperResolution.__new__(sr.ProcessSuperResolution)
        owner._closed, owner.width, owner.height, owner.dtype = False, 4, 4, np.uint8
        owner._process = mock.Mock()
        owner._process.is_alive.return_value = False
        owner._connection = mock.Mock()
        owner._connection.recv.return_value = {"ok": False, "error": "MemoryError: reserve exhausted"}
        owner._log_tail = lambda: ""
        owner.timeout = 60
        with self.assertRaisesRegex(sr.SuperResolutionError, "reserve exhausted"):
            owner.process(np.zeros((4, 4, 4), np.uint8))
        owner._connection.poll.assert_called_once_with(0)
        owner._connection.send.assert_not_called()

    def test_frame_generation_preflight_queries_selected_gpu_and_ignores_other_low_vram_gpu(self):
        from fractions import Fraction
        from dlss5tool import frame_generation as fg

        na_on_a_low_on_b = (
            "GPU-aaaa, 00000000:01:00.0, NVIDIA GeForce RTX 4090, N/A, N/A\n"
            "GPU-bbbb, 00000000:02:00.0, NVIDIA GeForce RTX 4070, 12282, 128\n"
        ).encode("utf-8")
        adapters = [
            {"id": "gpu-a", "pci_bus_id": "0000:01:00.0", "name": "NVIDIA GeForce RTX 4090"},
            {"id": "gpu-b", "pci_bus_id": "0000:02:00.0", "name": "NVIDIA GeForce RTX 4070"},
        ]
        meta = {"is_hdr": False, "profile": "srgb", "color_primaries": "bt709"}
        sentinel = RuntimeError("reached_render_prep")
        inspector = lambda *_args, **_kwargs: (meta, Fraction(30, 1), 640, 360, 10)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp4"
            src.write_bytes(b"x")
            out = Path(tmp) / "out.mp4"
            with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=na_on_a_low_on_b)), \
                    mock.patch("dlss5tool.dlss_engine.available_render_adapters", return_value=adapters), \
                    mock.patch.object(fg, "source_bytes", return_value=1024), \
                    mock.patch.object(fg.shutil, "disk_usage", return_value=mock.Mock(free=100 * sr._GIB)), \
                    mock.patch("dlss5tool.memory_budget.require_memory_headroom", side_effect=sentinel):
                # Selected GPU A (N/A) for SR/NR must NOT be blocked by GPU B's 128 MiB -> reaches render prep.
                with self.assertRaisesRegex(RuntimeError, "reached_render_prep"):
                    fg.export_video(
                        src, out, multiplier=1, scale=2, settings={"render_gpu": "gpu-a"},
                        log_dir=Path(tmp) / "log-a-sr", source_inspector=inspector,
                    )
                with self.assertRaisesRegex(RuntimeError, "reached_render_prep"):
                    fg.export_video(
                        src, out, multiplier=1, scale=1, enhance=True, settings={"render_gpu": "gpu-a", "intensity": 1.0},
                        log_dir=Path(tmp) / "log-a-nr", source_inspector=inspector,
                    )
                # Default auto (GPU 0 = A = N/A) also must NOT be blocked by GPU B's 128 MiB.
                with self.assertRaisesRegex(RuntimeError, "reached_render_prep"):
                    fg.export_video(
                        src, out, multiplier=1, scale=2, settings={"render_gpu": "auto"},
                        log_dir=Path(tmp) / "log-auto", source_inspector=inspector,
                    )
                # Selected GPU B (128 MiB) when SR or NR actually executes IS blocked by insufficient VRAM.
                with self.assertRaisesRegex(RuntimeError, "显存余量不足") as blocked_sr:
                    fg.export_video(
                        src, out, multiplier=1, scale=2, settings={"render_gpu": "gpu-b"},
                        log_dir=Path(tmp) / "log-b-sr", source_inspector=inspector,
                    )
                self.assertIsInstance(blocked_sr.exception.__cause__, ValueError)
                with self.assertRaisesRegex(RuntimeError, "显存余量不足") as blocked_nr:
                    fg.export_video(
                        src, out, multiplier=1, scale=1, enhance=True, settings={"render_gpu": "gpu-b", "intensity": 1.0},
                        log_dir=Path(tmp) / "log-b-nr", source_inspector=inspector,
                    )
                self.assertIsInstance(blocked_nr.exception.__cause__, ValueError)

    def test_pure_frame_generation_preflight_ignores_unrelated_render_gpu_and_checks_active_stages(self):
        from fractions import Fraction
        from dlss5tool import frame_generation as fg

        adapters = [
            {"id": "gpu-a", "pci_bus_id": "0000:01:00.0", "cuda_index": 0, "name": "NVIDIA GeForce RTX 4090"},
            {"id": "gpu-b", "pci_bus_id": "0000:02:00.0", "cuda_index": 1, "name": "NVIDIA GeForce RTX 4070"},
        ]
        a_ok_b_low = (
            "GPU-aaaa, 00000000:01:00.0, NVIDIA GeForce RTX 4090, 24564, 8192\n"
            "GPU-bbbb, 00000000:02:00.0, NVIDIA GeForce RTX 4070, 12282, 128\n"
        ).encode("utf-8")
        b_low_a_ok_reordered = (
            "GPU-bbbb, 00000000:02:00.0, NVIDIA GeForce RTX 4070, 12282, 128\n"
            "GPU-aaaa, 00000000:01:00.0, NVIDIA GeForce RTX 4090, 24564, 8192\n"
        ).encode("utf-8")
        a_low_b_ok = (
            "GPU-aaaa, 00000000:01:00.0, NVIDIA GeForce RTX 4090, 24564, 128\n"
            "GPU-bbbb, 00000000:02:00.0, NVIDIA GeForce RTX 4070, 12282, 8192\n"
        ).encode("utf-8")
        meta = {"is_hdr": False, "profile": "srgb", "color_primaries": "bt709"}
        sentinel = RuntimeError("reached_render_prep")
        inspector = lambda *_args, **_kwargs: (meta, Fraction(30, 1), 640, 360, 10)

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp4"
            src.write_bytes(b"x")
            out = Path(tmp) / "out.mp4"
            with mock.patch("dlss5tool.dlss_engine.available_render_adapters", return_value=adapters), \
                    mock.patch.object(fg, "source_bytes", return_value=1024), \
                    mock.patch.object(fg.shutil, "disk_usage", return_value=mock.Mock(free=100 * sr._GIB)), \
                    mock.patch("dlss5tool.memory_budget.require_memory_headroom", side_effect=sentinel):
                # 1) Pure FG (multiplier=2, scale=1, enhance=False) must NOT be blocked by enhancement render_gpu=gpu-b (128 MiB),
                # regardless of whether nvidia-smi lists A first or B first.
                for idx, stdout in enumerate((a_ok_b_low, b_low_a_ok_reordered)):
                    with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=stdout)):
                        with self.assertRaisesRegex(RuntimeError, "reached_render_prep"):
                            fg.export_video(
                                src, out, multiplier=2, scale=1, enhance=False,
                                settings={"render_gpu": "gpu-b"},
                                log_dir=Path(tmp) / f"pure-fg-{idx}", source_inspector=inspector,
                            )

                # 2) FG + bypassed enhancement (output_mix=0 -> creates_nr=False) also ignores gpu-b.
                with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=a_ok_b_low)):
                    with self.assertRaisesRegex(RuntimeError, "reached_render_prep"):
                        fg.export_video(
                            src, out, multiplier=2, scale=1, enhance=True,
                            settings={"render_gpu": "gpu-b", "output_mix": 0},
                            log_dir=Path(tmp) / "fg-bypass", source_inspector=inspector,
                        )
                    # 3) Combined FG + active NR on gpu-b (128 MiB) IS blocked.
                    with self.assertRaisesRegex(RuntimeError, "显存余量不足") as blocked_fg_nr:
                        fg.export_video(
                            src, out, multiplier=2, scale=1, enhance=True,
                            settings={"render_gpu": "gpu-b", "intensity": 1.0},
                            log_dir=Path(tmp) / "fg-nr-low-b", source_inspector=inspector,
                        )
                    self.assertIsInstance(blocked_fg_nr.exception.__cause__, ValueError)
                    # 4) Combined FG + SR on gpu-b (128 MiB) IS blocked.
                    with self.assertRaisesRegex(RuntimeError, "显存余量不足") as blocked_fg_sr:
                        fg.export_video(
                            src, out, multiplier=2, scale=2, enhance=False,
                            settings={"render_gpu": "gpu-b"},
                            log_dir=Path(tmp) / "fg-sr-low-b", source_inspector=inspector,
                        )
                    self.assertIsInstance(blocked_fg_sr.exception.__cause__, ValueError)

                # 5) When FG's own GPU A has only 128 MiB and gpu-b has 8 GiB:
                # - Pure SR on gpu-b succeeds (reaches render prep),
                # - Pure FG and combined FG+SR are blocked because FG runs on GPU A.
                with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=a_low_b_ok)):
                    with self.assertRaisesRegex(RuntimeError, "reached_render_prep"):
                        fg.export_video(
                            src, out, multiplier=1, scale=2, enhance=False,
                            settings={"render_gpu": "gpu-b"},
                            log_dir=Path(tmp) / "sr-ok-b-low-a", source_inspector=inspector,
                        )
                    with self.assertRaisesRegex(RuntimeError, "显存余量不足") as blocked_fg_a:
                        fg.export_video(
                            src, out, multiplier=2, scale=1, enhance=False,
                            settings={"render_gpu": "gpu-b"},
                            log_dir=Path(tmp) / "pure-fg-low-a", source_inspector=inspector,
                        )
                    self.assertIsInstance(blocked_fg_a.exception.__cause__, ValueError)
                    with self.assertRaisesRegex(RuntimeError, "显存余量不足") as blocked_combo_a:
                        fg.export_video(
                            src, out, multiplier=2, scale=2, enhance=False,
                            settings={"render_gpu": "gpu-b"},
                            log_dir=Path(tmp) / "combo-low-a", source_inspector=inspector,
                        )
                    self.assertIsInstance(blocked_combo_a.exception.__cause__, ValueError)

    def test_strict_gpu_query_never_borrows_other_card_on_single_row_or_same_model_name(self):
        from fractions import Fraction
        from dlss5tool import frame_generation as fg

        gpu_a = {
            "id": "gpu-a",
            "uuid": "GPU-aaaa",
            "pci_bus_id": "0000:01:00.0",
            "name": "NVIDIA GeForce RTX 4090",
        }
        only_gpu_b = (
            "GPU-bbbb, 00000000:02:00.0, NVIDIA GeForce RTX 4090, 24564, 128\n"
        ).encode("utf-8")
        multi_same_model_b = (
            "GPU-cccc, 00000000:03:00.0, NVIDIA GeForce RTX 4080, 16384, 8192\n"
            "GPU-bbbb, 00000000:02:00.0, NVIDIA GeForce RTX 4090, 24564, 128\n"
        ).encode("utf-8")
        conflicting_uuid_same_pci = (
            "GPU-bbbb, 00000000:01:00.0, NVIDIA GeForce RTX 4090, 24564, 128\n"
        ).encode("utf-8")
        na_pci = (
            "[N/A], [N/A], NVIDIA GeForce RTX 4090, 24564, 128\n"
        ).encode("utf-8")

        # 1) Single-row nvidia-smi containing only GPU B must NOT satisfy strict query for GPU A.
        with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=only_gpu_b)):
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=0, adapter_info=gpu_a))
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=0, adapter_info={"id": "gpu-a"}))
            # Opt-in conservative fallback (VSR cache recycling) still receives the 128 MiB reading.
            self.assertEqual(
                sr.query_gpu_memory(cache_seconds=0, adapter_info=gpu_a, conservative_fallback=True)["free_bytes"],
                128 * sr._MIB,
            )

        # 2) Multi-row nvidia-smi where UUID and PCI both fail must NOT fall back to matching same-model GPU B.
        with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=multi_same_model_b)):
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=0, adapter_info=gpu_a))
            self.assertEqual(
                sr.query_gpu_memory(cache_seconds=0, adapter_info=gpu_a, conservative_fallback=True)["free_bytes"],
                128 * sr._MIB,
            )

        # 3) Conflicting UUID on same PCI slot and placeholder [N/A] PCI strings never falsely match.
        with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=conflicting_uuid_same_pci)):
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=0, adapter_info=gpu_a))
        self.assertEqual(sr._normalize_pci_bus_id("[N/A]"), "")
        self.assertEqual(sr._normalize_pci_bus_id("N/A"), "")
        with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=na_pci)):
            self.assertIsNone(sr.query_gpu_memory(cache_seconds=0, adapter_info={"pci_bus_id": "[N/A]"}))

        # 4) End-to-end export_video preflight treats both unconfirmed cases as unknown VRAM (does not block).
        meta = {"is_hdr": False, "profile": "srgb", "color_primaries": "bt709"}
        sentinel = RuntimeError("reached_render_prep")
        inspector = lambda *_args, **_kwargs: (meta, Fraction(30, 1), 640, 360, 10)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp4"
            src.write_bytes(b"x")
            out = Path(tmp) / "out.mp4"
            with mock.patch("dlss5tool.dlss_engine.available_render_adapters", return_value=[gpu_a]), \
                    mock.patch.object(fg, "source_bytes", return_value=1024), \
                    mock.patch.object(fg.shutil, "disk_usage", return_value=mock.Mock(free=100 * sr._GIB)), \
                    mock.patch("dlss5tool.memory_budget.require_memory_headroom", side_effect=sentinel):
                for label, stdout in (("only-b", only_gpu_b), ("same-model-b", multi_same_model_b)):
                    with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=stdout)):
                        with self.assertRaisesRegex(RuntimeError, "reached_render_prep"):
                            fg.export_video(
                                src, out, multiplier=1, scale=2, settings={"render_gpu": "gpu-a"},
                                log_dir=Path(tmp) / f"log-{label}", source_inspector=inspector,
                            )

    def test_auto_super_resolution_preflight_skips_d3d12_11_0_adapter_like_vsr_host(self):
        from fractions import Fraction
        from dlss5tool import frame_generation as fg

        adapters = [
            {
                "id": "gpu-11",
                "pci_bus_id": "0000:01:00.0",
                "d3d12_feature_level": "11_0",
                "name": "NVIDIA GeForce GTX 750",
            },
            {
                "id": "gpu-12",
                "pci_bus_id": "0000:02:00.0",
                "d3d12_feature_level": "12_0",
                "name": "NVIDIA GeForce RTX 4070 SUPER",
            },
        ]
        legacy_low_rtx_ok = (
            "GPU-1111, 00000000:01:00.0, NVIDIA GeForce GTX 750, 2048, 128\n"
            "GPU-2222, 00000000:02:00.0, NVIDIA GeForce RTX 4070 SUPER, 12282, 8192\n"
        ).encode("utf-8")
        legacy_ok_rtx_low = (
            "GPU-1111, 00000000:01:00.0, NVIDIA GeForce GTX 750, 2048, 2048\n"
            "GPU-2222, 00000000:02:00.0, NVIDIA GeForce RTX 4070 SUPER, 12282, 128\n"
        ).encode("utf-8")
        meta = {"is_hdr": False, "profile": "srgb", "color_primaries": "bt709"}
        sentinel = RuntimeError("reached_render_prep")
        inspector = lambda *_args, **_kwargs: (meta, Fraction(30, 1), 640, 360, 10)

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp4"
            src.write_bytes(b"x")
            out = Path(tmp) / "out.mp4"
            with mock.patch("dlss5tool.dlss_engine.available_render_adapters", return_value=adapters), \
                    mock.patch.object(fg, "source_bytes", return_value=1024), \
                    mock.patch.object(fg.shutil, "disk_usage", return_value=mock.Mock(free=100 * sr._GIB)), \
                    mock.patch("dlss5tool.memory_budget.require_memory_headroom", side_effect=sentinel):
                # Auto VSR-only skips the 11_0 card (128 MiB) and checks the 12_0 card (8 GiB) -> reaches render prep.
                with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=legacy_low_rtx_ok)):
                    with self.assertRaisesRegex(RuntimeError, "reached_render_prep"):
                        fg.export_video(
                            src, out, multiplier=1, scale=2, enhance=False,
                            settings={"render_gpu": "auto"},
                            log_dir=Path(tmp) / "auto-vsr-skip-11", source_inspector=inspector,
                        )
                    # Auto NR (which accepts 11_0) still checks the 11_0 card (128 MiB) -> blocked.
                    with self.assertRaisesRegex(RuntimeError, "显存余量不足") as blocked_nr:
                        fg.export_video(
                            src, out, multiplier=1, scale=1, enhance=True,
                            settings={"render_gpu": "auto", "intensity": 1.0},
                            log_dir=Path(tmp) / "auto-nr-11", source_inspector=inspector,
                        )
                    self.assertIsInstance(blocked_nr.exception.__cause__, ValueError)

                # When the 12_0 card itself has only 128 MiB, auto VSR IS blocked.
                with mock.patch.object(sr.subprocess, "run", return_value=mock.Mock(stdout=legacy_ok_rtx_low)):
                    with self.assertRaisesRegex(RuntimeError, "显存余量不足") as blocked_vsr:
                        fg.export_video(
                            src, out, multiplier=1, scale=2, enhance=False,
                            settings={"render_gpu": "auto"},
                            log_dir=Path(tmp) / "auto-vsr-low-12", source_inspector=inspector,
                        )
                    self.assertIsInstance(blocked_vsr.exception.__cause__, ValueError)


class GuiSuperResolutionReconfigureTests(unittest.TestCase):
    def test_gui_ensure_super_resolution_reuses_same_gpu_and_replaces_on_gpu_change(self):
        from dlss5tool import gui

        app = gui.App.__new__(gui.App)
        app._host_settings = {"render_gpu_selected_id": "gpu-a"}
        app._super_resolution_lock = threading.RLock()
        app._super_resolution_live = app._super_resolution_key = None

        first = mock.Mock(_closed=False, _failed=False)
        second = mock.Mock(_closed=False, _failed=False)
        with mock.patch.object(gui, "ProcessSuperResolution", side_effect=[first, second]) as create:
            self.assertIs(app._ensure_super_resolution(1280, 720, 2, is_hdr=False), first)
            self.assertIs(app._ensure_super_resolution(1270, 720, 2, is_hdr=False), first)
            first.reconfigure.assert_called_once_with(1270, 720, 2, is_hdr=False)
            self.assertIs(app._ensure_super_resolution(640, 360, 4, is_hdr=True), first)
            self.assertEqual(first.reconfigure.call_count, 2)
            self.assertEqual(create.call_count, 1)

            # Validation error keeping session open must NOT destroy the healthy session.
            first.reconfigure.side_effect = ValueError("bad size")
            with self.assertRaises(ValueError):
                app._ensure_super_resolution(1, 1, 2, is_hdr=False)
            first.close.assert_not_called()
            self.assertIs(app._super_resolution_live, first)

            # GPU switch closes old session and spawns a new one on gpu-b.
            first.reconfigure.side_effect = None
            app._host_settings["render_gpu_selected_id"] = "gpu-b"
            self.assertIs(app._ensure_super_resolution(640, 360, 4, is_hdr=True), second)
            first.close.assert_called_once()
            self.assertEqual(create.call_count, 2)
            self.assertEqual(create.call_args.kwargs["render_gpu"], "gpu-b")

            # Fatal reconfigure leaving session closed clears app._super_resolution_live.
            def fatal_reconfigure(*_args, **_kwargs):
                second._closed = True
                raise sr.SuperResolutionError("fatal")

            second.reconfigure.side_effect = fatal_reconfigure
            with self.assertRaises(sr.SuperResolutionError):
                app._ensure_super_resolution(800, 600, 2, is_hdr=False)
            self.assertIsNone(app._super_resolution_live)
            self.assertIsNone(app._super_resolution_key)

    def test_gui_replace_ram_pressure_discards_stale_session_and_recovers_on_2x(self):
        """P1 regression: 2x -> 4x replace fails RAM admission, then switching back to 2x recovers cleanly."""
        from dlss5tool import gui

        app = gui.App.__new__(gui.App)
        app._host_settings = {"render_gpu_selected_id": "auto"}
        app._super_resolution_lock = threading.RLock()
        app._super_resolution_live = app._super_resolution_key = None

        with mock.patch.object(sr, "_worker_main", fake_worker_with_resize), \
                mock.patch.object(sr, "query_gpu_memory", return_value={"free_bytes": 8 * sr._GIB}), \
                mock.patch.object(sr, "_VSR_MAX_CACHED_BUCKETS", 1):
            try:
                live_2x = app._ensure_super_resolution(8, 6, 2, is_hdr=False)
                pid_first = live_2x._process.pid
                np.testing.assert_array_equal(live_2x.process(np.full((6, 8, 4), 4, np.uint8)), 6)

                # Switch 2x -> 4x forces _replace (bucket cap=1), which closes old worker then hits RAM pressure.
                with mock.patch("dlss5tool.memory_budget.system_memory", return_value={
                    "total_physical_bytes": 16 * sr._GIB,
                    "free_physical_bytes": 1,
                    "free_commit_bytes": 16 * sr._GIB,
                }):
                    with self.assertRaises(MemoryError):
                        app._ensure_super_resolution(8, 6, 4, is_hdr=False)

                self.assertTrue(live_2x._closed)
                self.assertIsNone(app._super_resolution_live)
                self.assertIsNone(app._super_resolution_key)

                # After RAM recovers, switching back to 2x rebuilds cleanly without AttributeError.
                recovered = app._ensure_super_resolution(8, 6, 2, is_hdr=False)
                self.assertIsNot(recovered, live_2x)
                self.assertNotEqual(recovered._process.pid, pid_first)
                np.testing.assert_array_equal(recovered.process(np.full((6, 8, 4), 4, np.uint8)), 6)
            finally:
                app._close_super_resolution()


class VsrBenchmarkScriptTests(unittest.TestCase):
    def test_reference_mismatch_returns_nonzero_exit_code(self):
        from scripts import profile_vsr_resolution_switch as profiler

        with tempfile.TemporaryDirectory() as tmp:
            ref_path = Path(tmp) / "ref.json"
            ref_path.write_text(json.dumps({
                "rows": [{"index": 0, "sha256": "expected_hash"}],
            }), encoding="utf-8")

            fake_mismatch = {"mode": "reconfigure", "rows": [{"index": 0, "sha256": "wrong_hash"}]}
            with mock.patch.object(profiler, "run_reconfigure", return_value=fake_mismatch):
                code = profiler.main(["--case", "reconfigure", "--suite", "manga", "--reference", str(ref_path)])
            self.assertEqual(code, 1)
            self.assertFalse(fake_mismatch["all_matched"])
            self.assertFalse(fake_mismatch["rows"][0]["matches_baseline"])

            fake_match = {"mode": "reconfigure", "rows": [{"index": 0, "sha256": "expected_hash"}]}
            with mock.patch.object(profiler, "run_reconfigure", return_value=fake_match):
                code = profiler.main(["--case", "reconfigure", "--suite", "manga", "--reference", str(ref_path)])
            self.assertEqual(code, 0)
            self.assertTrue(fake_match["all_matched"])

    def test_combined_and_native_probe_reject_missing_fields_empty_and_extra_reference_rows(self):
        from scripts import profile_vsr_resolution_switch as profiler

        with tempfile.TemporaryDirectory() as tmp:
            # 1) Combined mode given a VSR-only reference (no vsr_sha256 / nr_sha256) must fail.
            vsr_only_ref = Path(tmp) / "vsr_only.json"
            vsr_only_ref.write_text(json.dumps({"rows": [{"index": 0, "sha256": "aaa"}]}), encoding="utf-8")
            combined_out = {
                "mode": "combined-reconfigure",
                "rows": [{"index": 0, "vsr_sha256": "aaa", "nr_sha256": "bbb"}],
            }
            with mock.patch.object(profiler, "run_combined", return_value=combined_out):
                self.assertEqual(
                    profiler.main(["--case", "combined-reconfigure", "--suite", "manga", "--reference", str(vsr_only_ref)]),
                    1,
                )
            self.assertFalse(combined_out["all_matched"])

            # 2) Combined mode given a reference missing nr_sha256 must fail.
            missing_nr_ref = Path(tmp) / "missing_nr.json"
            missing_nr_ref.write_text(json.dumps({"rows": [{"index": 0, "vsr_sha256": "aaa"}]}), encoding="utf-8")
            combined_out2 = {
                "mode": "combined-reconfigure",
                "rows": [{"index": 0, "vsr_sha256": "aaa", "nr_sha256": "bbb"}],
            }
            with mock.patch.object(profiler, "run_combined", return_value=combined_out2):
                self.assertEqual(
                    profiler.main(["--case", "combined-reconfigure", "--suite", "manga", "--reference", str(missing_nr_ref)]),
                    1,
                )
            self.assertFalse(combined_out2["all_matched"])

            # 3) Combined mode given matching vsr_sha256 AND nr_sha256 passes with exit code 0.
            good_combined_ref = Path(tmp) / "good_combined.json"
            good_combined_ref.write_text(
                json.dumps({"rows": [{"index": 0, "vsr_sha256": "aaa", "nr_sha256": "bbb"}]}),
                encoding="utf-8",
            )
            combined_out3 = {
                "mode": "combined-reconfigure",
                "rows": [{"index": 0, "vsr_sha256": "aaa", "nr_sha256": "bbb"}],
            }
            with mock.patch.object(profiler, "run_combined", return_value=combined_out3):
                self.assertEqual(
                    profiler.main(["--case", "combined-reconfigure", "--suite", "manga", "--reference", str(good_combined_ref)]),
                    0,
                )
            self.assertTrue(combined_out3["all_matched"])

            # 4) native-probe given empty reference or extra reference rows must fail with exit code 1.
            empty_ref = Path(tmp) / "empty.json"
            empty_ref.write_text(json.dumps({"rows": []}), encoding="utf-8")
            extra_ref = Path(tmp) / "extra.json"
            extra_ref.write_text(
                json.dumps({"rows": [{"index": 0, "sha256": "aaa"}, {"index": 1, "sha256": "bbb"}]}),
                encoding="utf-8",
            )
            exact_ref = Path(tmp) / "exact.json"
            exact_ref.write_text(json.dumps({"rows": [{"index": 0, "sha256": "aaa"}]}), encoding="utf-8")

            for ref_file, expected_code in ((empty_ref, 1), (extra_ref, 1), (exact_ref, 0)):
                native_out = {"mode": "native-probe", "rows": [{"index": 0, "sha256": "aaa"}], "multi_rows": []}
                with mock.patch.object(profiler, "run_native_probe", return_value=native_out):
                    self.assertEqual(
                        profiler.main(["--case", "native-probe", "--suite", "manga", "--reference", str(ref_file)]),
                        expected_code,
                    )
                self.assertEqual(native_out["all_matched"], expected_code == 0)

    def test_combined_runner_updates_nr_format_for_hdr_cases(self):
        from scripts import profile_vsr_resolution_switch as profiler
        from dlss5tool import dlss_engine, dlss_host_process

        nr_calls = []

        class FakeSR:
            def __init__(self, w, h, scale, is_hdr=False, _host_library=None):
                self._process = mock.Mock(pid=101)
                self.w, self.h, self.scale, self.is_hdr = w, h, scale, is_hdr

            def reconfigure(self, w, h, scale, is_hdr=False):
                self.w, self.h, self.scale, self.is_hdr = w, h, scale, is_hdr

            def process(self, frame):
                dtype = np.float16 if self.is_hdr else np.uint8
                return np.zeros((self.h * self.scale, self.w * self.scale, 4), dtype=dtype)

            def close(self):
                return None

        class FakeLive:
            def __init__(self, w, h, settings):
                self._session = mock.Mock(_process=mock.Mock(pid=202))
                nr_calls.append(("init", w, h, settings["frame_format"], settings["color_profile"]))

            def resize(self, w, h, settings=None):
                nr_calls.append(("resize", w, h, settings["frame_format"], settings["color_profile"]))

            def process(self, frame, reset=True):
                expected = np.float16 if nr_calls[-1][3] == "rgba16f" else np.uint8
                if frame.dtype != expected:
                    raise AssertionError(f"dtype mismatch: {frame.dtype} != {expected}")
                return frame.copy()

            def close(self):
                return None

        with mock.patch.object(sr, "ProcessSuperResolution", FakeSR), \
                mock.patch.object(dlss_host_process, "ProcessLive", FakeLive), \
                mock.patch.object(dlss_engine, "close_adapter_probe"), \
                mock.patch.object(profiler, "query_vram", return_value=None):
            result = profiler.run_combined([(8, 6, 2, False), (8, 6, 2, True), (8, 6, 2, False)], use_reconfigure=True)

        self.assertEqual(nr_calls, [
            ("init", 16, 12, "rgba8", "srgb"),
            ("resize", 16, 12, "rgba16f", "hdr10_pq"),
            ("resize", 16, 12, "rgba8", "srgb"),
        ])
        self.assertEqual(len(result["rows"]), 3)


if __name__ == "__main__":
    unittest.main()
