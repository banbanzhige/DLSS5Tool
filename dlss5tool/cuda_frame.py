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
    """One owned pitched RGBA8 SDR allocation with bounded explicit leases."""
    layout, color_profile = 'rgba8', 'srgb'

    def __init__(self, session, width, height, *, row_pitch=None):
        if any(type(value) is not int or not 0 < value <= 16384 for value in (width, height)):
            raise ValueError('Positive image dimensions up to 16384 required')
        pitch = width*4 if row_pitch is None else row_pitch
        if type(pitch) is not int or not width*4 <= pitch <= width*4+65536:
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
        if frame.dtype != np.uint8 or frame.shape != (self.height, self.width, 4):
            raise ValueError('Expected full-size SDR RGBA8')
        if not isinstance(timing, FrameTiming):
            raise TypeError('FrameTiming required')
        self.begin_write()
        # Host inputs are an explicit compatibility consumer, not the device
        # pipeline. Padding is retained for tests and future decoder surfaces.
        # A noncontiguous caller may need a temporary reshape in addition to
        # the pitched upload staging array.
        with memory_allocation(2*self.nbytes, stage='cuda_image_host_upload'):
            data = np.zeros((self.height, self.row_pitch), np.uint8)
            data[:, :self.width*4] = frame.reshape(self.height, self.width*4)
            self.session.upload(self._pointer, data)
            # Pageable HtoD can return after staging while DMA is still in
            # flight. The VSR consumer uses a nonblocking stream, so publish
            # only after the owner has explicitly completed the upload.
            self.session.synchronize()
        return self.complete(timing)

    def download(self, frame):
        frame._check()
        if frame._owner is not self:
            raise ValueError('Frame belongs to another allocation')
        # CudaSession.download temporarily owns both its ctypes readback and
        # returned bytes; extracting the owned ndarray also needs a snapshot.
        with memory_allocation(2*self.nbytes, stage='cuda_image_host_download'):
            data = self.session.download(frame.pointer, self.nbytes)
            return np.frombuffer(data, np.uint8).reshape(self.height, self.row_pitch)[:, :self.width*4].reshape(
                self.height, self.width, 4).copy()

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
