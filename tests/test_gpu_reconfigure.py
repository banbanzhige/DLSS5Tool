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


def test_remap_keeps_image_owner_identity_and_publishes_new_geometry(monkeypatch):
    original = mapping()
    image = SharedCudaImage(original, 8, 4, 32)
    registry.register(original)
    lease = image.complete(FrameTiming(0)); lease.close()
    original.suspend_reconfigure()
    replacement = mapping(handle=11, pointer=901)
    monkeypatch.setattr('dlss5tool.cuda_interop.SharedCudaBuffer', mock.Mock(return_value=replacement))
    descriptor = dict(producer_pid=123, handle=11, allocation=4096, size=4096, producer_luid=456, pitch=64)
    try:
        original.resume_reconfigure(descriptor, 12, 7, 'rgba8', 'srgb')
        assert (image.width, image.height, image.row_pitch) == (12, 7, 64)
        assert image._pointer == 901 and not image._suspended
        assert original._image_owners == 1 and original.handle == 11
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


def test_invalid_remap_preserves_image_owner_accounting(monkeypatch):
    original = mapping()
    image = SharedCudaImage(original, 8, 4, 32)
    original.suspend_reconfigure()
    replacement = mapping(handle=11, pointer=901)
    monkeypatch.setattr('dlss5tool.cuda_interop.SharedCudaBuffer', mock.Mock(return_value=replacement))
    descriptor = dict(producer_pid=123, handle=11, allocation=4096, size=4096, producer_luid=456, pitch=64)
    try:
        with pytest.raises(ValueError, match='color contract'):
            original.resume_reconfigure(descriptor, 12, 7, 'rgba8', 'invalid')
        assert original._image_owners == 1
        assert image._suspended
    finally:
        original._image_owners = 1
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
