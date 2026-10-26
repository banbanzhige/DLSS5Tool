"""CUDA frame leases, VSR handoff and explicit adapter contracts without GPU."""
from contextlib import nullcontext
import ctypes as C
from fractions import Fraction
import os
from types import SimpleNamespace
import threading
from unittest import mock

import numpy as np
import pytest

from dlss5tool.cuda_frame import CudaImage, FrameTiming
from dlss5tool.cuda_session import CudaSession
from dlss5tool.cuda_vsr import CudaVSR
from dlss5tool import super_resolution as sr
from dlss5tool import cuda_vsr_runtime as runtime


class FakeCuda:
    def __init__(self, luid=123, context=456):
        self.luid, self.context = luid, C.c_void_p(context)
        self.buffers, self.next = {}, 100
        self.synchronize = mock.Mock()
    def _check(self): pass
    def current(self): return nullcontext()
    def allocate(self, size):
        self.next += 1
        self.buffers[self.next] = bytearray(size)
        return self.next
    def free(self, pointer): del self.buffers[pointer]
    def upload(self, pointer, data): self.buffers[pointer][:] = data.tobytes()
    def download(self, pointer, size): return bytes(self.buffers[pointer][:size])


@pytest.mark.parametrize('profile', ['hdr10_pq', 'hdr10_hlg', 'scrgb'])
def test_hdr_frame_keeps_half_bits_color_timing_and_pitched_lease(profile):
    cuda = FakeCuda()
    values = np.arange(4*8*4, dtype=np.uint16).reshape(4, 8, 4)
    timing = FrameTiming(9, 27, Fraction(1, 90), True)
    with CudaImage(cuda, 8, 4, row_pitch=80, layout='rgba16f', color_profile=profile) as image:
        with image.upload(values.view(np.float16), timing) as frame:
            assert frame.layout == 'rgba16f' and frame.color_profile == profile
            assert frame.timing is timing and frame.nbytes == 320
            np.testing.assert_array_equal(image.download(frame).view(np.uint16), values)
            with pytest.raises(RuntimeError, match='held'): image.begin_write()
        with pytest.raises(RuntimeError, match='expired'): _ = frame.pointer
    assert not cuda.buffers


@pytest.mark.parametrize('arguments', [dict(layout='rgba8', color_profile='hdr10_pq'),
    dict(layout='rgba16f', color_profile='srgb'), dict(layout='rgba16f', color_profile='hdr10_pq', row_pitch=65)])
def test_hdr_color_or_pitch_mismatch_rejected_before_allocation(arguments):
    cuda = FakeCuda()
    with pytest.raises(ValueError): CudaImage(cuda, 8, 4, **arguments)
    assert not cuda.buffers


def test_hdr_vsr_cannot_load_without_packing_contract():
    with mock.patch('dlss5tool.cuda_vsr.C.WinDLL') as load:
        with pytest.raises(ValueError, match='packing PTX'):
            CudaVSR(FakeCuda(), 8, 4, 2, library_path='unused', cudart_directory='unused',
                    runtime_directory='unused', log_path='unused', color_profile='hdr10_pq')
    load.assert_not_called()


@pytest.mark.parametrize('values', [dict(sequence=True), dict(sequence=-1), dict(sequence=0, pts=2),
    dict(sequence=0, time_base=Fraction(1, 30)), dict(sequence=0, pts=0, time_base=0.1),
    dict(sequence=0, pts=0, time_base=Fraction(-1, 30)), dict(sequence=0, reset=1)])
def test_frame_timing_rejects_ambiguous_metadata(values):
    with pytest.raises(ValueError): FrameTiming(**values)


def test_pitched_image_preserves_pixels_and_timing_and_blocks_reuse():
    cuda = FakeCuda()
    pixels = np.arange(8*4*4, dtype=np.uint8).reshape(4, 8, 4)
    timing = FrameTiming(7, 21, Fraction(1, 90), True)
    with CudaImage(cuda, 8, 4, row_pitch=48) as image:
        with image.upload(pixels, timing) as frame:
            assert (frame.width, frame.height, frame.row_pitch, frame.layout, frame.color_profile) == (8, 4, 48, 'rgba8', 'srgb')
            assert frame.adapter_luid == 123 and frame.context == 456 and frame.process_id == os.getpid()
            assert frame.stream == 0 and frame.timing is timing
            cuda.synchronize.assert_called_once()
            np.testing.assert_array_equal(image.download(frame), pixels)
            assert not any(cuda.buffers[frame.pointer][32:48])
            with pytest.raises(RuntimeError, match='held'): image.upload(pixels, timing)
            with pytest.raises(RuntimeError, match='held'): image.close()
        with pytest.raises(RuntimeError, match='expired'): _ = frame.pointer
        with image.upload(pixels, FrameTiming(8)) as fresh:
            assert fresh.timing.sequence == 8
    assert not cuda.buffers


@pytest.mark.parametrize('field', ['_pid', '_thread'])
def test_frame_pointer_rejects_other_process_or_thread_before_driver_access(field):
    cuda = FakeCuda()
    with CudaImage(cuda, 8, 4) as image:
        with image.upload(np.zeros((4, 8, 4), np.uint8), FrameTiming(0)) as frame:
            original = getattr(image, field)
            setattr(image, field, -1)
            try:
                with pytest.raises(RuntimeError, match='process/thread'): _ = frame.pointer
            finally:
                setattr(image, field, original)


def make_vsr(cuda):
    vsr = CudaVSR.__new__(CudaVSR)
    vsr.session, vsr.width, vsr.height, vsr.scale = cuda, 8, 4, 2
    vsr._pid, vsr._thread = os.getpid(), threading.get_ident()
    vsr._closed, vsr._failed = False, False
    vsr._output = CudaImage(cuda, 16, 8)
    vsr._handle, vsr._stream = C.c_void_p(1), C.c_void_p(2)
    vsr._dll_directory = None
    vsr._process = mock.Mock(return_value=1)
    vsr._destroy, vsr._stream_destroy = mock.Mock(return_value=1), mock.Mock(return_value=0)
    vsr._stream_sync = mock.Mock(return_value=0)
    return vsr


def test_vsr_preserves_timeline_and_refuses_output_reuse_until_consumer_releases():
    cuda = FakeCuda()
    vsr = make_vsr(cuda)
    try:
        with CudaImage(cuda, 8, 4) as source:
            with source.upload(np.zeros((4, 8, 4), np.uint8), FrameTiming(3, 6, Fraction(1, 60), True)) as frame:
                output = vsr.process_device(frame)
                assert output.timing is frame.timing and output.stream == 2
                assert (output.width, output.height, output.adapter_luid) == (16, 8, cuda.luid)
                with pytest.raises(RuntimeError, match='held'): vsr.process_device(frame)
                with pytest.raises(RuntimeError, match='held'): vsr.close()
                assert vsr._process.call_count == 1
                output.close()
                with vsr.process_device(frame): pass
                assert vsr._process.call_count == 2
    finally:
        vsr.close()
    assert not cuda.buffers


@pytest.mark.parametrize('other', [FakeCuda(999, 456), FakeCuda(123, 999), FakeCuda(123, 456)])
def test_vsr_rejects_other_gpu_context_or_owner_before_native_call(other):
    cuda = FakeCuda()
    vsr = make_vsr(cuda)
    try:
        with CudaImage(other, 8, 4) as source:
            with source.upload(np.zeros((4, 8, 4), np.uint8), FrameTiming(0)) as frame:
                with pytest.raises(ValueError, match='same physical GPU'): vsr.process_device(frame)
        vsr._process.assert_not_called()
    finally:
        vsr.close()


def test_vsr_native_error_poisoning_prevents_reuse_of_partial_output():
    cuda = FakeCuda()
    vsr = make_vsr(cuda)
    vsr._process.return_value = 0
    try:
        with CudaImage(cuda, 8, 4) as source:
            with source.upload(np.zeros((4, 8, 4), np.uint8), FrameTiming(0)) as frame:
                with pytest.raises(RuntimeError, match='evaluation'): vsr.process_device(frame)
                with pytest.raises(RuntimeError, match='failed'): vsr.process_device(frame)
        assert vsr._process.call_count == 1 and not vsr._output._ready
    finally:
        vsr.close()


def test_failed_completion_retains_output_owner_until_worker_can_retire():
    cuda = FakeCuda()
    vsr = make_vsr(cuda)
    vsr._stream_sync.return_value = 700
    with pytest.raises(RuntimeError, match='final completion'): vsr.close()
    assert not vsr._closed and vsr._output._pointer in cuda.buffers
    vsr._destroy.assert_not_called()
    vsr._stream_sync.return_value = 0
    vsr.close()


@pytest.mark.parametrize('arguments', [dict(width=0, height=8, scale=2),
    dict(width=8, height=8, scale=3), dict(width=8, height=8, scale=2, quality=3)])
def test_unsupported_vsr_geometry_or_quality_rejected_before_loading_dll(arguments):
    with mock.patch('dlss5tool.cuda_vsr.C.WinDLL') as load:
        with pytest.raises((ValueError, sr.SuperResolutionError)):
            CudaVSR(FakeCuda(), **arguments, library_path='unused', cudart_directory='unused',
                    runtime_directory='unused', log_path='unused')
        load.assert_not_called()


def test_session_free_cannot_release_unowned_pointer_and_keeps_failed_owner():
    session = CudaSession.__new__(CudaSession)
    session._closed, session._thread = False, threading.get_ident()
    session._buffers, session._buffer_sizes = {123}, {123: 64}
    session.current = lambda: nullcontext()
    session._free = mock.Mock(return_value=700)
    with pytest.raises(ValueError): session.free(999)
    session._free.assert_not_called()
    with pytest.raises(RuntimeError): session.free(123)
    assert session._buffers == {123}
    session._free.return_value = 0
    session.free(123)
    assert not session._buffers and not session._buffer_sizes


RECORD = dict(id='fixture-gpu', name='Fixture RTX', luid_high=-1, luid_low=12)


def adapter_library(actual=(-1, 12), query_ok=True):
    def query(high, low):
        high._obj.value, low._obj.value = actual
        return int(query_ok)
    return SimpleNamespace(vsr_init=mock.Mock(return_value=1),
        vsr_init_adapter=mock.Mock(return_value=1), vsr_adapter_luid=mock.Mock(side_effect=query))


def test_explicit_vsr_selection_uses_requested_luid_and_verifies_actual_identity():
    library = adapter_library()
    with mock.patch('dlss5tool.dlss_engine.available_render_adapters', return_value=[RECORD]):
        result = sr._initialize_host(library, 8, 8, 2, False, 'unused', RECORD['id'])
    assert result['id'] == RECORD['id'] and result['verified']
    assert library.vsr_init_adapter.call_args.args[-2:] == (-1, 12)
    library.vsr_init.assert_not_called()


def test_vsr_auto_reports_actual_identity_without_another_probe():
    library = adapter_library()
    with mock.patch('dlss5tool.dlss_engine.available_render_adapters') as probe:
        result = sr._initialize_host(library, 8, 8, 2, False, 'unused')
    assert result['verified'] and result['auto_selection']
    assert (result['luid_high'], result['luid_low']) == (-1, 12)
    library.vsr_init_adapter.assert_not_called()
    probe.assert_not_called()


@pytest.mark.parametrize('library', [adapter_library(actual=(0, 99)), adapter_library(query_ok=False)])
def test_actual_vsr_gpu_mismatch_stops_and_invalidates_inventory(library):
    with mock.patch('dlss5tool.dlss_engine.invalidate_render_adapters') as invalidate:
        with pytest.raises(sr.SuperResolutionError, match='不一致'):
            sr._initialize_host(library, 8, 8, 2, False, 'unused', RECORD['id'], RECORD)
    invalidate.assert_called_once()


def test_missing_vsr_gpu_or_legacy_manual_host_cannot_silently_select_auto():
    library = adapter_library()
    with mock.patch('dlss5tool.dlss_engine.available_render_adapters', return_value=[]):
        with pytest.raises(sr.SuperResolutionError, match='不可用'):
            sr._initialize_host(library, 8, 8, 2, False, 'unused', 'missing')
    library.vsr_init.assert_not_called()
    library.vsr_init_adapter.assert_not_called()
    legacy = SimpleNamespace(vsr_init=mock.Mock(return_value=1))
    with pytest.raises(sr.SuperResolutionError, match='更新'):
        sr._initialize_host(legacy, 8, 8, 2, False, 'unused', RECORD['id'])
    legacy.vsr_init.assert_not_called()
    assert not sr._initialize_host(legacy, 8, 8, 2, False, 'unused')['verified']


def test_preview_gpu_change_retires_vsr_before_reuse():
    from dlss5tool import gui
    app = gui.App.__new__(gui.App)
    app._host_settings = {'render_gpu_selected_id': 'first'}
    app._super_resolution_lock = threading.RLock()
    app._super_resolution_live = app._super_resolution_key = None
    owners = [mock.Mock(), mock.Mock()]
    with mock.patch.object(gui, 'ProcessSuperResolution', side_effect=owners) as create:
        assert app._ensure_super_resolution(8, 8, 2) is owners[0]
        assert app._ensure_super_resolution(8, 8, 2) is owners[0]
        app._host_settings['render_gpu_selected_id'] = 'second'
        assert app._ensure_super_resolution(8, 8, 2) is owners[1]
        assert create.call_args.kwargs['render_gpu'] == 'second'
    owners[0].close.assert_called_once()
    app._close_super_resolution()


def test_frame_admission_rejects_before_device_allocation_under_pressure():
    cuda = FakeCuda()
    with mock.patch('dlss5tool.memory_budget.system_memory', return_value=dict(
            total_physical_bytes=16*1024**3, free_physical_bytes=1, free_commit_bytes=16*1024**3)):
        with pytest.raises(MemoryError): CudaImage(cuda, 8, 4)
    assert not cuda.buffers


def test_host_readback_admission_includes_staging_and_owned_snapshot():
    from dlss5tool import memory_budget as memory
    cuda = FakeCuda()
    with CudaImage(cuda, 8, 4) as image:
        with image.upload(np.zeros((4, 8, 4), np.uint8), FrameTiming(0)) as frame:
            state = dict(total_physical_bytes=16*1024**3, free_commit_bytes=16*1024**3,
                         free_physical_bytes=0)
            state['free_physical_bytes'] = memory.reserve_bytes(state)+image.nbytes+1
            with mock.patch.object(memory, 'system_memory', return_value=state), \
                    mock.patch.object(cuda, 'download') as download:
                with pytest.raises(MemoryError): image.download(frame)
            download.assert_not_called()


def test_host_upload_completion_failure_does_not_publish_a_frame():
    cuda = FakeCuda()
    cuda.synchronize.side_effect = RuntimeError('injected upload completion failure')
    with CudaImage(cuda, 8, 4) as image:
        with pytest.raises(RuntimeError, match='upload completion'):
            image.upload(np.zeros((4, 8, 4), np.uint8), FrameTiming(0))
        assert not image._ready and image._leases == 0


def test_mismatched_image_layout_is_rejected_before_native_processing():
    cuda = FakeCuda()
    vsr = make_vsr(cuda)
    try:
        with CudaImage(cuda, 4, 4) as image:
            with image.upload(np.zeros((4, 4, 4), np.uint8), FrameTiming(0)) as frame:
                with pytest.raises(ValueError, match='geometry/color/layout'): vsr.process_device(frame)
        vsr._process.assert_not_called()
    finally:
        vsr.close()


def runtime_fixture(tmp_path):
    import json
    provider = tmp_path/'nvngx_vsr.dll'
    provider.write_bytes(b'verified-provider-fixture')
    root = tmp_path/'vsr-cuda'
    root.mkdir()
    for name in runtime.FILES.values(): (root/name).write_bytes(name.encode())
    record = dict(schema=1, enabled=True, abi=1, quality=4, formats=['rgba8-srgb'], scales=[2, 4],
        max_output_pixels=3840*2160, provider_sha256=runtime.sha256(provider),
        files={name: runtime.sha256(root/name) for name in runtime.FILES.values()})
    (root/'manifest.json').write_text(json.dumps(record))
    return root, provider, record


def test_runtime_discovery_validates_components_without_loading_gpu(tmp_path):
    root, provider, _ = runtime_fixture(tmp_path)
    with mock.patch('ctypes.WinDLL') as load:
        components = runtime.load_components(root, runtime_root=tmp_path)
    assert components['native_library'] == str(root/'vsr_cuda.dll')
    assert components['runtime_directory'] == str(tmp_path)
    load.assert_not_called()


@pytest.mark.parametrize('changed', ['provider', 'bridge', 'contract'])
def test_runtime_rejects_unverified_provider_binary_and_quality_contract(tmp_path, changed):
    import json
    root, provider, record = runtime_fixture(tmp_path)
    if changed == 'provider': provider.write_bytes(b'changed')
    elif changed == 'bridge': (root/'vsr_cuda.dll').write_bytes(b'changed')
    else:
        record['quality'] = 3
        (root/'manifest.json').write_text(json.dumps(record))
    with pytest.raises(RuntimeError): runtime.load_components(root, runtime_root=tmp_path)


@pytest.mark.parametrize('shape', [(True, 128, 2), (0, 128, 2), (1920, 1080, 3), (3840, 2160, 2)])
def test_verified_factory_rejects_unaccepted_plans_before_discovery(shape):
    with mock.patch.object(runtime, 'load_components') as discover:
        with pytest.raises(ValueError): CudaVSR.from_runtime(FakeCuda(), *shape, log_path='unused')
    discover.assert_not_called()
