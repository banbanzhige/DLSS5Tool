"""Torch-free checks for the isolated experimental interop harness."""
import ctypes
import unittest
from unittest.mock import Mock

from scripts.flow_interop_probe import ExternalMemoryDesc, BufferDesc, Interop


class InteropProbeTests(unittest.TestCase):
    def test_win64_driver_descriptor_abi(self):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest('Win64 probe only')
        self.assertEqual(ctypes.sizeof(ExternalMemoryDesc), 104)
        self.assertEqual(ExternalMemoryDesc.handle.offset, 8)
        self.assertEqual(ExternalMemoryDesc.size.offset, 24)
        self.assertEqual(ExternalMemoryDesc.flags.offset, 32)
        self.assertEqual(ctypes.sizeof(BufferDesc), 88)
        self.assertEqual(BufferDesc.size.offset, 8)

    def test_close_frees_mapping_before_import_and_host(self):
        bridge = Interop.__new__(Interop)
        bridge.torch, bridge.cuda, bridge.lib = Mock(), Mock(), Mock()
        bridge.pointer = ctypes.c_uint64(123)
        bridge.external = ctypes.c_void_p(456)
        events = []
        bridge.cuda.cuMemFree_v2.side_effect = lambda p: events.append(('mapping',p.value)) or 0
        bridge.cuda.cuDestroyExternalMemory.side_effect = lambda p: events.append(('external',p.value)) or 0
        bridge.lib.probe_close.side_effect = lambda: events.append(('host',None))
        bridge.close()
        self.assertEqual(events,[('mapping',123),('external',456),('host',None)])
        self.assertEqual(bridge.pointer.value,0)
        self.assertIsNone(bridge.external.value)
        bridge.close()
        self.assertEqual(events[-1],('host',None))
        self.assertEqual(bridge.cuda.cuMemFree_v2.call_count,1)

    def test_reject_cpu_or_wrong_size_before_copy(self):
        bridge = Interop.__new__(Interop)
        bridge.torch, bridge.cuda = Mock(), Mock()
        bridge.size=128
        flow=Mock(dtype=bridge.torch.float32,is_cuda=False)
        with self.assertRaises(ValueError):
            bridge.send(flow)
        flow.is_cuda=True
        flow.is_contiguous.return_value=True
        flow.numel.return_value=33
        with self.assertRaises(ValueError):
            bridge.send(flow)
        bridge.cuda.cuMemcpyDtoDAsync_v2.assert_not_called()


if __name__=='__main__': unittest.main()
