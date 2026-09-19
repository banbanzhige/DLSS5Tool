import ctypes as C
import unittest
from unittest.mock import Mock,patch

from dlss5tool.cuda_sdr_yuv import CudaSdrConverter
from dlss5tool.encoding_contract import frame_encoding_contract


class CudaSdrBoundaryTests(unittest.TestCase):
    def session(self):
        s=CudaSdrConverter.__new__(CudaSdrConverter)
        s.width=321;s.height=181;s.output_width=322;s.output_height=182
        s.nbytes=322*182*3//2;s._closed=False;s._failed=False
        s._check_owner=Mock();s._pointer=C.c_uint64(1234)
        s._stream=C.c_void_p(1);s._function=C.c_void_p(2)
        s._launch=Mock(return_value=0);s._sync=Mock(return_value=0)
        return s

    def test_reject_invalid_geometry_without_loading_cuda(self):
        for w,h in ((0,1),(-1,2),(True,2),(3841,2160),(1920,1.5)):
            with self.assertRaises(ValueError):CudaSdrConverter('unused',w,h)

    def test_contract_factory_rejects_resize_hdr_before_cuda_loading(self):
        resized=frame_encoding_contract(320,180,30,640,360,True)
        hdr=frame_encoding_contract(320,180,30,320,180,False,
            dict(color_transfer='smpte2084',color_primaries='bt2020',color_space='bt2020nc'))
        for contract in (resized,hdr):
            with self.assertRaises(ValueError):CudaSdrConverter.from_contract('unused',contract)
        contract=frame_encoding_contract(321,181,30,322,182)
        with patch.object(CudaSdrConverter,'__init__',return_value=None) as init:
            CudaSdrConverter.from_contract('unused',contract)
            init.assert_called_once_with('unused',321,181)

    def test_returns_padded_i420_lease(self):
        s=self.session();frame=s.convert(1,321*181*4,'rgba',5)
        self.assertEqual((frame.pointer,frame.nbytes,frame.layout,frame.sequence),
                         (1234,322*182*3//2,'yuv420p',5))
        s._launch.assert_called_once();s._sync.assert_called_once()

    def test_reject_hdr_and_invalid_memory_contracts(self):
        s=self.session()
        for pointer,nbytes,layout,sequence,pitch in ((0,1000000,'rgba',0,None),
            (1,10,'rgba',0,None),(1,1000000,'rgba16f',0,None),
            (1,1000000,'rgba',0,321*3),(1,1000000,'rgba',False,None),
            (1,1000000,'rgba',2**32,None),(1,1000000,'rgba',0,1.5)):
            with self.assertRaises(ValueError):s.convert(pointer,nbytes,layout,sequence,pitch)
        s._launch.assert_not_called()

    def test_pitch_padding_is_supported(self):
        s=self.session();s.convert(1,(321*4+17)*181,'rgba',0,321*4+17)
        s._launch.assert_called_once()

    def test_failed_kernel_poison_session(self):
        s=self.session();s._launch.return_value=700
        with self.assertRaises(RuntimeError):s.convert(1,321*181*4,'rgba',0)
        self.assertTrue(s._failed)
        with self.assertRaises(RuntimeError):s.convert(1,321*181*4,'rgba',0)
        self.assertEqual(s._launch.call_count,1)

    def test_closed_session_rejects_input(self):
        s=self.session();s._closed=True
        with self.assertRaises(RuntimeError):s.convert(1,321*181*4,'rgba',0)
        s._launch.assert_not_called()


if __name__=='__main__':unittest.main()
