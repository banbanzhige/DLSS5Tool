"""Process-local, single-slot CUDA image ownership and completed-frame leases.

All GPU consumers must finish before releasing a lease. A pointer is never an
IPC descriptor. The owner rejects reuse/free while any consumer holds a frame.
"""
from dataclasses import dataclass
from fractions import Fraction
import os
import threading

import numpy as np

from dlss5tool.memory_budget import memory_allocation


@dataclass(frozen=True)
class FrameTiming:
    sequence: int
    pts: int | None = None
    time_base: Fraction | None = None
    reset: bool = False

    def __post_init__(self):
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError('Nonnegative frame sequence required')
        if (self.pts is None) != (self.time_base is None):
            raise ValueError('PTS and time base must be supplied together')
        if self.pts is not None and (type(self.pts) is not int or
                not isinstance(self.time_base, Fraction) or self.time_base <= 0):
            raise ValueError('Integer PTS and positive rational time base required')
        if type(self.reset) is not bool:
            raise ValueError('Boolean reset required')


class ReadyCudaFrame:
    """Borrowed immutable image description, valid until its explicit close."""
    def __init__(self, owner, timing):
        if not isinstance(timing, FrameTiming):
            raise TypeError('FrameTiming required')
        owner._check()
        if not owner._ready:
            raise RuntimeError('CUDA image has not completed production')
        self._owner, self.timing = owner, timing
        self._generation, self._released = owner._generation, False
        owner._leases += 1

    def _check(self):
        self._owner._check()
        if self._released or self._generation != self._owner._generation:
            raise RuntimeError('CUDA frame lease expired')

    @property
    def pointer(self):
        self._check()
        return self._owner._pointer

    @property
    def width(self): return self._owner.width
    @property
    def height(self): return self._owner.height
    @property
    def row_pitch(self): return self._owner.row_pitch
    @property
    def nbytes(self): return self._owner.nbytes
    @property
    def layout(self): return self._owner.layout
    @property
    def color_profile(self): return self._owner.color_profile
    @property
    def adapter_luid(self): return self._owner.session.luid
    @property
    def context(self): return self._owner.session.context.value
    @property
    def stream(self): return self._owner._producer_stream
    @property
    def process_id(self): return self._owner._pid
    @property
    def session(self): return self._owner.session

    def close(self):
        if self._released:
            return
        self._check()
        self._owner._leases -= 1
        self._released = True

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *_): self.close()


class CudaImage:
    """One owned pitched RGBA allocation with bounded explicit leases."""
    layout, color_profile = 'rgba8', 'srgb'

    def __init__(self, session, width, height, *, row_pitch=None,
                 layout='rgba8', color_profile='srgb'):
        if any(type(value) is not int or not 0 < value <= 16384 for value in (width, height)):
            raise ValueError('Positive image dimensions up to 16384 required')
        if ((layout == 'rgba8' and color_profile != 'srgb') or
                (layout == 'rgba16f' and color_profile not in ('hdr10_pq', 'hdr10_hlg', 'scrgb')) or
                (layout == 'gray8' and color_profile != 'analysis') or
                layout not in ('rgba8', 'rgba16f', 'gray8')):
            raise ValueError('Explicit RGBA layout and compatible color profile required')
        self.layout, self.color_profile = layout, color_profile
        self.dtype = np.dtype(np.float16 if layout == 'rgba16f' else np.uint8)
        self.channels = 1 if layout == 'gray8' else 4
        self.pixel_bytes = self.channels*self.dtype.itemsize
        minimum = width*self.pixel_bytes
        pitch = minimum if row_pitch is None else row_pitch
        if type(pitch) is not int or not minimum <= pitch <= minimum+65536 or pitch % self.dtype.itemsize:
            raise ValueError('Invalid RGBA row pitch')
        session._check()
        self.session, self.width, self.height = session, width, height
        self.row_pitch, self.nbytes = pitch, pitch*height
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._pointer, self._leases, self._generation = 0, 0, 0
        self._ready, self._closed = False, False
        self._producer_stream = 0
        with memory_allocation(self.nbytes, stage='cuda_image_allocate') as ticket:
            self._pointer = session.allocate(self.nbytes)
            if ticket: ticket.consume(self.nbytes)

    def _check(self):
        if self._closed or os.getpid() != self._pid or threading.get_ident() != self._thread:
            raise RuntimeError('CUDA image closed or accessed outside its owning process/thread')
        self.session._check()

    def begin_write(self):
        self._check()
        if self._leases:
            raise RuntimeError('CUDA image is still held by a consumer')
        self._ready = False
        self._generation += 1
        return self._pointer

    def complete(self, timing, *, stream=0):
        self._check()
        if self._leases:
            raise RuntimeError('CUDA image already leased')
        if type(stream) is not int or stream < 0:
            raise ValueError('CUDA producer stream handle required')
        self._producer_stream = stream
        self._ready = True
        return ReadyCudaFrame(self, timing)

    def upload(self, frame, timing):
        if frame.dtype != self.dtype or frame.shape != (self.height, self.width, self.channels):
            raise ValueError('Expected full-size ' + self.layout)
        if not isinstance(timing, FrameTiming):
            raise TypeError('FrameTiming required')
        self.begin_write()
        # Host inputs are an explicit compatibility consumer, not the device
        # pipeline. Padding is retained for tests and future decoder surfaces.
        # A noncontiguous caller may need a temporary reshape in addition to
        # the pitched upload staging array.
        with memory_allocation(2*self.nbytes, stage='cuda_image_host_upload'):
            data = np.zeros((self.height, self.row_pitch), np.uint8)
            data[:, :self.width*self.pixel_bytes] = np.ascontiguousarray(frame).view(np.uint8).reshape(
                self.height, self.width*self.pixel_bytes)
            self.session.upload(self._pointer, data)
            # Pageable HtoD can return after staging while DMA is still in
            # flight. The VSR consumer uses a nonblocking stream, so publish
            # only after the owner has explicitly completed the upload.
            self.session.synchronize()
        return self.complete(timing)

    def copy_from(self, frame, timing=None):
        """Clone a completed same-owner image without a CPU staging frame."""
        import ctypes as C
        from dlss5tool.cuda_session import bind, check
        from dlss5tool.nvofa import Copy2D
        if not isinstance(frame, ReadyCudaFrame):
            raise TypeError('Completed CUDA frame lease required')
        frame._check()
        if (frame.session is not self.session or (frame.width, frame.height, frame.layout, frame.color_profile)
                != (self.width, self.height, self.layout, self.color_profile)):
            raise ValueError('CUDA copy owner/geometry/color mismatch')
        self.begin_write()
        descriptor = Copy2D(stype=2, sdevice=frame.pointer, spitch=frame.row_pitch,
            dtype=2, ddevice=self._pointer, dpitch=self.row_pitch,
            width=self.width*self.pixel_bytes, height=self.height)
        with self.session.current():
            check(bind(self.session.cuda, 'cuMemcpy2D_v2', [C.POINTER(Copy2D)])(
                C.byref(descriptor)), 'CUDA frame clone')
        self.session.synchronize()
        return self.complete(timing or frame.timing)

    def download(self, frame):
        frame._check()
        if frame._owner is not self:
            raise ValueError('Frame belongs to another allocation')
        # CudaSession.download temporarily owns both its ctypes readback and
        # returned bytes; extracting the owned ndarray also needs a snapshot.
        with memory_allocation(2*self.nbytes, stage='cuda_image_host_download'):
            data = self.session.download(frame.pointer, self.nbytes)
            rows = np.frombuffer(data, np.uint8).reshape(self.height, self.row_pitch)
            return rows[:, :self.width*self.pixel_bytes].copy().view(self.dtype).reshape(
                self.height, self.width, self.channels)

    def close(self):
        if self._closed:
            return
        self._check()
        if self._leases:
            raise RuntimeError('Cannot free a CUDA image held by a consumer')
        self.session.free(self._pointer)
        self._pointer = 0
        self._closed = True

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class SharedCudaImage(CudaImage):
    """Lease owner for a mapped D3D buffer; mapping outlives this image owner."""
    def __init__(self, mapping, width, height, row_pitch, *, layout='rgba8', color_profile='srgb'):
        if (layout, color_profile) not in (('rgba8', 'srgb'), ('rgba16f', 'hdr10_pq'), ('rgba16f', 'hdr10_hlg')):
            raise ValueError('Unsupported shared image color contract')
        dtype = np.dtype(np.float16 if layout == 'rgba16f' else np.uint8)
        if (any(type(value) is not int or not 0 < value <= 16384 for value in (width, height))
                or type(row_pitch) is not int or row_pitch < width*4*dtype.itemsize
                or row_pitch % dtype.itemsize or row_pitch*height > mapping.size or not mapping.pointer.value):
            raise ValueError('Shared image exceeds its pitched mapping')
        mapping.session._check()
        self._mapping, self.session = mapping, mapping.session
        self.width, self.height, self.row_pitch, self.nbytes = width, height, row_pitch, row_pitch*height
        self.layout, self.color_profile, self.dtype = layout, color_profile, dtype
        self.channels = 4
        self.pixel_bytes = 4*dtype.itemsize
        self._pointer = mapping.pointer.value
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._leases, self._generation, self._producer_stream = 0, 0, 0
        self._ready, self._closed = False, False
        mapping._image_owners += 1

    def _check(self):
        super()._check()
        if self._mapping.pointer.value != self._pointer:
            raise RuntimeError('Shared CUDA image mapping expired')

    def close(self):
        if self._closed:
            return
        self._check()
        if self._leases:
            raise RuntimeError('Cannot release shared image held by a consumer')
        self._mapping._image_owners -= 1
        self._closed, self._pointer = True, 0
