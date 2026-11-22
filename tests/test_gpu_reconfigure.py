"""Consumer barriers and generation changes without loading a GPU driver."""
import ctypes as C
from types import SimpleNamespace
from unittest import mock
import weakref

import pytest

from dlss5tool import gpu_reconfigure as registry
from dlss5tool.cuda_interop import SharedCudaBuffer
from dlss5tool.cuda_frame import SharedCudaImage, FrameTiming
from tests.test_device_consumers import Session


def mapping(handle=8, pointer=900, size=4096):
    result = SharedCudaBuffer.__new__(SharedCudaBuffer)
    result.session = Session()
    result.producer_pid, result.handle, result.size = 123, handle, size
    result.pointer, result.external = C.c_uint64(pointer), C.c_void_p(800)
    result._image_owners, result._failed = 0, False
    result._images = weakref.WeakSet()
    result._free, result._destroy = mock.Mock(return_value=0), mock.Mock(return_value=0)
    return result


def test_prepare_checks_all_consumers_before_suspending_any_mapping():
    first, second = mapping(), mapping(handle=9)
    a, b = SharedCudaImage(first, 8, 4, 32), SharedCudaImage(second, 8, 4, 32)
    registry.register(first); registry.register(second)
    owner = SimpleNamespace(_gpu_input_descriptor={'producer_pid': 123, 'handle': 8},
                            _gpu_output_descriptor={'producer_pid': 123, 'handle': 9})
    lease = b.complete(FrameTiming(0))
    try:
        with pytest.raises(RuntimeError, match='Finish'):
            registry.prepare(owner)
        first._free.assert_not_called()
        second._free.assert_not_called()
        assert not a._suspended
        lease.close()
        groups = registry.prepare(owner)
        assert len(groups) == 2 and a._suspended and b._suspended
        assert not first.pointer.value and not second.pointer.value
        with pytest.raises(RuntimeError, match='reconfigured'):
            a.begin_write()
    finally:
        lease.close(); a.close(); b.close()
        registry.unregister(first); registry.unregister(second)


def test_remap_keeps_image_owner_identity_and_publishes_new_geometry():
    original = mapping()
    image = SharedCudaImage(original, 8, 4, 32)
    original._caller_tag = 'mapping-state'
    image._caller_tag = 'image-state'
    registry.register(original)
    lease = image.complete(FrameTiming(0)); lease.close()
    original.suspend_reconfigure()
    original._import_external_memory = mock.Mock(return_value=(C.c_uint64(901), C.c_void_p(801)))
    descriptor = dict(producer_pid=123, handle=11, allocation=4096, size=4096, producer_luid=456, pitch=64)
    try:
        original.resume_reconfigure(descriptor, 12, 7, 'rgba8', 'srgb')
        assert (image.width, image.height, image.row_pitch) == (12, 7, 64)
        assert image._pointer == 901 and not image._suspended
        assert original._image_owners == 1 and original.handle == 11
        assert original._caller_tag == 'mapping-state' and image._caller_tag == 'image-state'
        assert (123, 8) not in registry._mappings and original in registry._mappings[(123, 11)]
        with pytest.raises(RuntimeError):
            _ = lease.pointer
        with image.complete(FrameTiming(1)) as new_lease:
            assert new_lease.pointer == 901
    finally:
        image.close(); original.close()


def test_native_or_remap_failure_retires_worker_and_leaves_images_suspended():
    shared = mapping()
    image = SharedCudaImage(shared, 8, 4, 32)
    registry.register(shared)
    session = SimpleNamespace(supports_gpu_reconfigure=True, request=mock.Mock(), close=mock.Mock())
    owner = SimpleNamespace(_gpu_output_descriptor={'producer_pid': 123, 'handle': 8}, _session=session)
    try:
        with pytest.raises(RuntimeError, match='native failed'):
            registry.reconfigure(owner, 12, 7, {}, mock.Mock(side_effect=RuntimeError('native failed')))
        session.close.assert_called_once()
        assert session._reconfigure_failed and image._suspended
    finally:
        image.close(); registry.unregister(shared)


def test_invalid_remap_preserves_image_owner_accounting():
    original = mapping()
    image = SharedCudaImage(original, 8, 4, 32)
    original.suspend_reconfigure()
    original._import_external_memory = mock.Mock(return_value=(C.c_uint64(901), C.c_void_p(801)))
    descriptor = dict(producer_pid=123, handle=11, allocation=4096, size=4096, producer_luid=456, pitch=64)
    try:
        with pytest.raises(ValueError, match='color contract'):
            original.resume_reconfigure(descriptor, 12, 7, 'rgba8', 'invalid')
        assert original._image_owners == 1
        assert image._suspended
    finally:
        image.close(); original.close()


def test_partial_unmap_failure_retires_producer_and_invalidates_all_images():
    first, second = mapping(), mapping(handle=9)
    a, b = SharedCudaImage(first, 8, 4, 32), SharedCudaImage(second, 8, 4, 32)
    registry.register(first); registry.register(second)
    second._free.return_value = 1
    session = SimpleNamespace(close=mock.Mock())
    owner = SimpleNamespace(_session=session,
        _gpu_input_descriptor={'producer_pid': 123, 'handle': 8},
        _gpu_output_descriptor={'producer_pid': 123, 'handle': 9})
    try:
        with pytest.raises(RuntimeError, match='unmap'):
            registry.prepare(owner)
        session.close.assert_called_once()
        assert a._suspended and b._suspended and first._failed and second._failed
    finally:
        a.close(); b.close()
        second._free.return_value = 0
        first.close(); second.close()


def test_remap_cleanup_failure_preserves_original_error_and_retains_handle_for_close(monkeypatch):
    import dlss5tool.cuda_interop as interop
    original = mapping()
    image = SharedCudaImage(original, 8, 4, 32)
    original.suspend_reconfigure()
    original._destroy.side_effect = [700, 700, 0]

    def fake_import(out_external, _desc):
        out_external._obj.value = 0x1234
        return 0

    def fake_map(_out_pointer, _external, _desc):
        return 2

    def fake_dup(_proc, _handle, _cur, out_copied, *_rest):
        out_copied._obj.value = 0x5678
        return 1

    fake_fns = {
        'cuImportExternalMemory': fake_import,
        'cuExternalMemoryGetMappedBuffer': fake_map,
        'OpenProcess': lambda *_: 0x9999,
        'CloseHandle': mock.Mock(return_value=1),
        'DuplicateHandle': fake_dup,
        'GetCurrentProcess': lambda: 0x1111,
    }
    monkeypatch.setattr(interop, 'bind', lambda _lib, name, *_args: fake_fns[name])
    monkeypatch.setattr(interop.C, 'WinDLL', mock.Mock())
    descriptor = dict(producer_pid=123, handle=11, allocation=4096, size=4096, producer_luid=456, pitch=64)
    try:
        with pytest.raises(RuntimeError, match='CUDA map D3D12') as exc_info:
            original.resume_reconfigure(descriptor, 12, 7, 'rgba8', 'srgb')
        assert any('cuDestroyExternalMemory=700' in note for note in getattr(exc_info.value, '__notes__', ()))
        assert original._failed and original.external.value == 0x1234 and not original.pointer.value
        image.close()
        with pytest.raises(RuntimeError, match='cleanup failed'):
            original.close()
        assert original.external.value == 0x1234
        original.close()
        assert not original.external.value
    finally:
        if not image._closed:
            image.close()
        original._destroy.side_effect = None
        original._destroy.return_value = 0
        if original.external.value or original.pointer.value:
            original.close()


def _fake_cuda_session():
    import threading
    from dlss5tool.cuda_session import CudaSession
    cuda = CudaSession.__new__(CudaSession)
    cuda._thread = threading.get_ident()
    cuda._closed = False
    cuda.cuda = mock.Mock()
    cuda.device = C.c_int(0)
    cuda.context = C.c_void_p(0xABC)
    cuda.luid = 456
    cuda._push = mock.Mock(return_value=0)
    cuda._pop = mock.Mock(return_value=0)
    cuda._release = mock.Mock(return_value=0)
    cuda._free = mock.Mock(return_value=0)
    cuda._buffers = set()
    cuda._buffer_sizes = {}
    cuda._retained_external = []
    return cuda


def test_initial_import_cleanup_failure_is_retained_and_retried_by_cuda_session_and_gpu_writer(monkeypatch):
    import dlss5tool.cuda_interop as interop
    from dlss5tool.gpu_video_export import NativeGpuVideoWriter

    cuda = _fake_cuda_session()
    writer = NativeGpuVideoWriter.__new__(NativeGpuVideoWriter)
    writer._finished = writer._failed = False
    writer._cuda = cuda
    writer._interop = []
    writer._encoder = writer._converter = writer._upload = None
    writer._owns_cuda = True

    destroy = mock.Mock(side_effect=[700, 700, 0])

    def fake_import(out_external, _desc):
        out_external._obj.value = 0x1234
        return 0

    def fake_dup(_proc, _handle, _cur, out_copied, *_rest):
        out_copied._obj.value = 0x5678
        return 1

    fake_fns = {
        'cuMemFree_v2': mock.Mock(return_value=0),
        'cuDestroyExternalMemory': destroy,
        'cuImportExternalMemory': fake_import,
        'cuExternalMemoryGetMappedBuffer': lambda *_: 2,
        'OpenProcess': lambda *_: 0x9999,
        'CloseHandle': mock.Mock(return_value=1),
        'DuplicateHandle': fake_dup,
        'GetCurrentProcess': lambda: 0x1111,
    }
    monkeypatch.setattr(interop, 'bind', lambda _lib, name, *_args: fake_fns[name])
    monkeypatch.setattr(interop.C, 'WinDLL', mock.Mock())
    descriptor = dict(producer_pid=123, handle=11, allocation=4096, size=4096, producer_luid=456)

    with pytest.raises(RuntimeError, match='CUDA map D3D12') as exc_info:
        writer.import_shared_output(**descriptor)
    assert any('cuDestroyExternalMemory=700' in note for note in getattr(exc_info.value, '__notes__', ()))
    assert writer._interop == []
    assert len(cuda._retained_external) == 1
    assert cuda._retained_external[0][1].value == 0x1234

    with pytest.raises(RuntimeError, match='GPU export cleanup failed'):
        writer._close_gpu()
    assert not cuda._closed and writer._cuda is cuda
    cuda._release.assert_not_called()

    writer._close_gpu()
    assert cuda._closed and writer._cuda is None and not cuda._retained_external
    cuda._release.assert_called_once()
    assert destroy.call_count == 3


def test_upper_cleanups_keep_cuda_session_open_until_failed_mapping_retries_succeed():
    from dlss5tool.device_cached import DeviceEnhancement
    from dlss5tool.gpu_video_export import NativeGpuVideoWriter

    # 1. DeviceEnhancement keeps CUDA session open across a failed mapping close.
    cuda = _fake_cuda_session()
    shared = mapping()
    shared.session = cuda
    shared._destroy.side_effect = [700, 0]
    enhancement = DeviceEnhancement.__new__(DeviceEnhancement)
    enhancement._closed = enhancement._failed = False
    enhancement.nr = enhancement.output = enhancement.input = None
    enhancement.mappings = [shared]
    enhancement.cuda = cuda

    with pytest.raises(RuntimeError, match='Cached enhancement cleanup failed'):
        enhancement.close()
    assert not enhancement._closed and not cuda._closed and shared.external.value == 800
    cuda._release.assert_not_called()

    enhancement.close()
    assert enhancement._closed and cuda._closed and not shared.external.value
    cuda._release.assert_called_once()

    # 2. NativeGpuVideoWriter retains failed mappings and its CUDA session for retry.
    cuda2 = _fake_cuda_session()
    shared2 = mapping(handle=12, pointer=902)
    shared2.session = cuda2
    shared2._destroy.side_effect = [700, 0]
    writer = NativeGpuVideoWriter.__new__(NativeGpuVideoWriter)
    writer._cuda = cuda2
    writer._interop = [shared2]
    writer._encoder = writer._converter = writer._upload = None
    writer._owns_cuda = True

    with pytest.raises(RuntimeError, match='GPU export cleanup failed'):
        writer._close_gpu()
    assert writer._interop == [shared2] and writer._cuda is cuda2 and not cuda2._closed
    cuda2._release.assert_not_called()

    writer._close_gpu()
    assert writer._interop == [] and writer._cuda is None and cuda2._closed
    cuda2._release.assert_called_once()


def test_device_fg_close_retries_analysis_and_hdr_pack_buffer_failures():
    from dlss5tool.cuda_analysis import CudaAnalysis
    from dlss5tool.cuda_hdr_pack import CudaHdrPacking
    from dlss5tool.device_cached import DeviceFrameGeneration

    cuda = _fake_cuda_session()
    cuda._buffers = {101, 102, 201, 202}
    cuda._buffer_sizes = {101: 16, 102: 16, 201: 16, 202: 16}
    fail_once = {102, 202}

    def flaky_free(pointer):
        if pointer in fail_once:
            fail_once.remove(pointer)
            return 700
        return 0

    cuda._free.side_effect = flaky_free

    analyzer = CudaAnalysis.__new__(CudaAnalysis)
    analyzer.session = cuda
    analyzer._closed = False
    analyzer._gray = analyzer._rgba = None
    analyzer._stream = C.c_void_p()
    analyzer._module = C.c_void_p()
    analyzer._buffers = [101, 102]

    validator = CudaHdrPacking.__new__(CudaHdrPacking)
    validator.session = cuda
    validator._closed = False
    validator._stream = C.c_void_p()
    validator._module = C.c_void_p()
    validator._buffers = [201, 202]
    validator._validation = None

    fg = DeviceFrameGeneration.__new__(DeviceFrameGeneration)
    fg._closed = fg._failed = False
    fg.previous_gray = fg.native = fg.output = fg.mapping = fg.flow = None
    fg.previous_image = fg.input = None
    fg.analyzer = analyzer
    fg.validator = validator
    fg.cuda = cuda

    with pytest.raises(RuntimeError, match='Cached FG cleanup failed'):
        fg.close()
    assert not fg._closed and not cuda._closed
    assert analyzer._buffers == [102] and validator._buffers == [202]
    assert cuda._buffers == {102, 202}
    cuda._release.assert_not_called()

    fg.close()
    assert fg._closed and cuda._closed
    assert analyzer._closed and validator._closed
    assert not analyzer._buffers and not validator._buffers and not cuda._buffers
    cuda._release.assert_called_once()
