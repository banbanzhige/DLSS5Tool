"""Fixed-resolution, single-in-flight guidance buffers (no Torch dependency).

The authenticated pipe remains the ownership barrier: the client writes input
before a request; the worker writes outputs before its acknowledgement. The
client must finish consuming borrowed outputs before sending the next request.
"""
from multiprocessing import shared_memory

import numpy as np


TRANSPORT = 'shared_memory_v1'


class GuidanceBuffers:
    def __init__(self, width, height, descriptor=None):
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ValueError('Invalid guidance buffer dimensions')
        self.memory = None
        self.rgba = self.motion = self.depth = None
        self.owner = descriptor is None
        pixels = width * height
        size = pixels * 16  # RGBA8 + float32 XY motion + float32 depth
        if descriptor is not None:
            if (not isinstance(descriptor, dict) or descriptor.get('transport') != TRANSPORT
                    or descriptor.get('size') != size or not isinstance(descriptor.get('name'), str)):
                raise ValueError('Invalid guidance shared-memory descriptor')
        try:
            kwargs = {'create': True, 'size': size} if self.owner else {'name': descriptor['name']}
            try:
                self.memory = shared_memory.SharedMemory(**kwargs, track=self.owner)
            except TypeError:  # Python < 3.13 (Windows has kernel-owned lifetime)
                self.memory = shared_memory.SharedMemory(**kwargs)
            if self.memory.size < size:
                raise ValueError('Guidance shared-memory block is too small')
            self.rgba = np.ndarray((height, width, 4), np.uint8, buffer=self.memory.buf)
            self.motion = np.ndarray((height, width, 2), np.float32,
                                     buffer=self.memory.buf, offset=pixels * 4)
            self.depth = np.ndarray((height, width), np.float32,
                                    buffer=self.memory.buf, offset=pixels * 12)
            self.descriptor = {'transport': TRANSPORT, 'name': self.memory.name, 'size': size}
        except BaseException:
            self.close()
            raise

    def close(self):
        self.rgba = self.motion = self.depth = None
        if self.memory is not None:
            memory, self.memory = self.memory, None
            memory.close()
            if self.owner:
                try:
                    memory.unlink()
                except FileNotFoundError:
                    pass
