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


def test_free_context_pop_failure_does_not_re_free_released_pointer_on_retry():
    from dlss5tool.cuda_analysis import CudaAnalysis
    from dlss5tool.cuda_frame import CudaImage
    from dlss5tool.cuda_hdr_pack import CudaHdrPacking

    cuda = _fake_cuda_session()
    cuda._buffers = {301, 302, 303, 304}
    cuda._buffer_sizes = {301: 64, 302: 64, 303: 64, 304: 64}
    cuda._pop.side_effect = [700, 700, 700, 700, 0, 0, 0]

    analyzer = CudaAnalysis.__new__(CudaAnalysis)
    analyzer.session = cuda
    analyzer._closed = False
    analyzer._gray = analyzer._rgba = None
    analyzer._stream = C.c_void_p()
    analyzer._module = C.c_void_p()
    analyzer._buffers = [301]

    validator = CudaHdrPacking.__new__(CudaHdrPacking)
    validator.session = cuda
    validator._closed = False
    validator._stream = C.c_void_p()
    validator._module = C.c_void_p()
    validator._buffers = [302]
    validator._validation = None

    image = CudaImage.__new__(CudaImage)
    image.session = cuda
    image._pointer = 303
    image._pid, image._thread = __import__('os').getpid(), __import__('threading').get_ident()
    image._leases = 0
    image._closed = False

    with pytest.raises(RuntimeError, match='CUDA pop'):
        analyzer.close()
    with pytest.raises(RuntimeError, match='CUDA pop'):
        validator.close()
    with pytest.raises(RuntimeError, match='CUDA pop'):
        image.close()

    # Pointers 301/302/303 were freed before pop failed; only 304 remains on session.
    assert cuda._buffers == {304} and not analyzer._buffers and not validator._buffers
    assert image._closed and image._pointer == 0
    assert cuda._free.call_count == 3

    # Retries on owners do not re-free released pointers.
    analyzer.close()
    validator.close()
    image.close()
    assert analyzer._closed and validator._closed
    assert cuda._free.call_count == 3

    # Session close freeing remaining pointer 304 also survives a pop failure without re-freeing 304.
    with pytest.raises(RuntimeError, match='CUDA pop'):
        cuda.close()
    assert not cuda._buffers and not cuda._closed
    assert cuda._free.call_count == 4
    cuda._release.assert_not_called()

    cuda.close()
    assert cuda._closed
    assert cuda._free.call_count == 4
    cuda._release.assert_called_once()


def test_partially_closed_shared_mapping_blocks_new_images_leases_rebind_and_copy():
    shared = mapping(handle=21, pointer=950)
    peer = mapping(handle=21, pointer=952)
    owner = SimpleNamespace(_gpu_input_descriptor={'producer_pid': 123, 'handle': 21},
                            _gpu_output_descriptor=None)
    registry.register(shared)
    registry.register(peer)
    try:
        shared._free.return_value = 700
        shared._destroy.return_value = 0

        with pytest.raises(RuntimeError, match='cleanup failed'):
            shared.close()
        assert shared._closing and shared._failed
        assert shared.pointer.value == 950 and not shared.external.value
        # Still holds a native handle: must stay registered so producer cannot release exports or resize.
        assert shared in registry._mappings.get((123, 21), ())
        with pytest.raises(RuntimeError, match='Retire shared GPU images and mappings'):
            registry.require_retired(owner)
        with pytest.raises(RuntimeError, match='unavailable'):
            registry.prepare(owner)

        with pytest.raises(RuntimeError, match='closed or failed'):
            SharedCudaImage(shared, 8, 4, 32)
        with pytest.raises(RuntimeError, match='unavailable'):
            shared.check_reconfigure()
        with pytest.raises(RuntimeError, match='closing'):
            shared.resume_reconfigure(
                dict(producer_pid=123, handle=22, allocation=4096, size=4096, producer_luid=456, pitch=32),
                8, 4, 'rgba8', 'srgb',
            )

        # Also verify the symmetric case: pointer freed but external destroy failed keeps registration.
        peer._free.return_value = 0
        peer._destroy.return_value = 700
        with pytest.raises(RuntimeError, match='cleanup failed'):
            peer.close()
        assert not peer.pointer.value and peer.external.value == 800
        assert peer in registry._mappings.get((123, 21), ())
        with pytest.raises(RuntimeError, match='Retire shared GPU images and mappings'):
            registry.require_retired(owner)

        # Also prove an existing SharedCudaImage rejects leases/rebind if its mapping degrades.
        active = mapping(handle=23, pointer=951)
        image = SharedCudaImage(active, 8, 4, 32)
        active._closing = True
        active.external = C.c_void_p()
        with pytest.raises(RuntimeError, match='closed or failed'):
            image.begin_write()
        with pytest.raises(RuntimeError, match='closed or failed'):
            image.complete(FrameTiming(0))
        with pytest.raises(RuntimeError, match='closed or failed'):
            image.rebind(8, 4, 32)
        image.close()

        # Retry close frees remaining handles and unregisters only once both handles are gone.
        shared._free.return_value = 0
        shared.close()
        assert not shared.pointer.value and not shared.external.value
        assert shared not in registry._mappings.get((123, 21), ())
        # Peer still holds external handle, so release_gpu_resources remains blocked.
        with pytest.raises(RuntimeError, match='Retire shared GPU images and mappings'):
            registry.require_retired(owner)

        peer._destroy.return_value = 0
        peer.close()
        assert not peer.pointer.value and not peer.external.value
        assert (123, 21) not in registry._mappings
        registry.require_retired(owner)
    finally:
        registry.unregister(shared)
        registry.unregister(peer)


def test_wrong_thread_close_does_not_poison_or_unregister_active_shared_mapping():
    import threading

    cuda = _fake_cuda_session()
    shared = mapping(handle=25, pointer=955)
    shared.session = cuda
    shared._closing = False
    registry.register(shared)
    worker_errors = []

    def close_from_other_thread():
        try:
            shared.close()
        except Exception as error:
            worker_errors.append(error)

    try:
        worker = threading.Thread(target=close_from_other_thread)
        worker.start()
        worker.join()
        assert len(worker_errors) == 1
        assert 'wrong thread' in str(worker_errors[0])
        shared._free.assert_not_called()
        shared._destroy.assert_not_called()
        assert not shared._closing and not shared._failed
        assert shared.pointer.value == 955 and shared.external.value == 800
        assert shared in registry._mappings.get((123, 25), ())

        # Owner thread can still use and cleanly close the mapping.
        shared.check_reconfigure()
        image = SharedCudaImage(shared, 8, 4, 32)
        with image.complete(FrameTiming(0)) as frame:
            assert frame.pointer == 955
        image.close()
        shared.close()
        assert not shared.pointer.value and not shared.external.value
        assert (123, 25) not in registry._mappings
    finally:
        registry.unregister(shared)


def test_export_video_finally_retries_transient_cleanup_failure_and_keeps_first_error(tmp_path, monkeypatch):
    import json
    from dlss5tool import frame_generation as fg
    from dlss5tool.device_cached import DeviceEnhancement, DeviceFrameGeneration
    from dlss5tool.cuda_analysis import CudaAnalysis

    fg_cuda = _fake_cuda_session()
    fg_cuda._buffers = {401, 402}
    fg_cuda._buffer_sizes = {401: 16, 402: 16}
    fail_once = {402}

    def flaky_free(pointer):
        if pointer in fail_once:
            fail_once.remove(pointer)
            return 700
        return 0

    fg_cuda._free.side_effect = flaky_free
    analyzer = CudaAnalysis.__new__(CudaAnalysis)
    analyzer.session = fg_cuda
    analyzer._closed = False
    analyzer._gray = analyzer._rgba = None
    analyzer._stream = C.c_void_p()
    analyzer._module = C.c_void_p()
    analyzer._buffers = [401, 402]

    native = DeviceFrameGeneration.__new__(DeviceFrameGeneration)
    native._closed = native._failed = False
    native.previous_gray = native.native = native.output = native.mapping = native.flow = None
    native.validator = native.previous_image = native.input = None
    native.analyzer = analyzer
    native.cuda = fg_cuda
    native.luid, native.runtime_hash = 456, 'fixture-hash'

    nr_cuda = _fake_cuda_session()
    nr_mapping = mapping(handle=31, pointer=960)
    nr_mapping.session = nr_cuda
    nr_mapping._destroy.side_effect = [700, 0]
    nr = DeviceEnhancement.__new__(DeviceEnhancement)
    nr._closed = nr._failed = False
    nr.nr = nr.output = nr.input = None
    nr.mappings = [nr_mapping]
    nr.cuda = nr_cuda
    nr.adapter_info = None

    class FakeInputSession:
        metadata = {'scene_cuts': ()}

        def wait_metadata(self, _cancel):
            return {
                'source_metadata': {'is_hdr': False, 'profile': 'srgb', 'color_primaries': 'bt709'},
                'source_rate': '24',
                'width': 1920,
                'height': 1080,
                'source_frames': 1,
            }

        def wait(self, _index, _cancel):
            raise RuntimeError('simulated render stop')

    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'stub')
    monkeypatch.setenv('DLSS5TOOL_FG_LOG_ROOT', str(tmp_path / 'logs'))
    monkeypatch.setattr('dlss5tool.super_resolution.query_gpu_memory', lambda **_: None)
    monkeypatch.setattr('dlss5tool.memory_budget.require_memory_headroom', lambda *a, **k: None)
    monkeypatch.setattr('dlss5tool.resident_hdr_runtime.load_components', lambda: {'verified_devices': [[1, 2]]})
    monkeypatch.setattr('dlss5tool.device_cached.adapter_for', lambda *_: {'cuda_index': 0})
    monkeypatch.setattr('dlss5tool.device_cached.DeviceFrameGeneration', lambda *a, **k: native)
    monkeypatch.setattr('dlss5tool.device_cached.DeviceEnhancement', lambda *a, **k: nr)

    with pytest.raises(RuntimeError, match='simulated render stop'):
        fg.export_video(source, None, multiplier=2, scale=1, enhance=True,
                        settings={'color_profile': 'srgb', '_render_stage': 'base'},
                        frame_sink=lambda *a, **k: None, metadata_sink=lambda *a, **k: None,
                        input_session=FakeInputSession())

    assert native._closed and fg_cuda._closed and not fg_cuda._buffers
    assert nr._closed and nr_cuda._closed and not nr_mapping.external.value
    fg_cuda._release.assert_called_once()
    nr_cuda._release.assert_called_once()
    logs = list((tmp_path / 'logs').rglob('result.json'))
    assert len(logs) == 1
    report = json.loads(logs[0].read_text(encoding='utf-8'))
    assert len(report['cleanup_errors']) == 2
    assert 'Cached FG cleanup failed' in report['cleanup_errors'][0]
    assert 'recovered on retry 1' in report['cleanup_errors'][0]
    assert 'Cached enhancement cleanup failed' in report['cleanup_errors'][1]
    assert 'recovered on retry 1' in report['cleanup_errors'][1]


def test_device_cached_rollback_retries_and_preserves_original_error():
    from dlss5tool.device_cached import _rollback_close

    owner = mock.Mock()
    owner.close.side_effect = [RuntimeError('first close fail'), None]
    original = RuntimeError('init failure')
    _rollback_close(owner, original)
    assert owner.close.call_count == 2
    assert any('first close fail (recovered on retry 1)' in note
               for note in getattr(original, '__notes__', ()))

    stuck = mock.Mock()
    stuck.close.side_effect = [RuntimeError('first fail'), RuntimeError('second fail'), RuntimeError('final fail')]
    original2 = RuntimeError('init failure 2')
    _rollback_close(stuck, original2)
    assert stuck.close.call_count == 3
    assert any('first fail (final attempt 3: final fail)' in note
               for note in getattr(original2, '__notes__', ()))
