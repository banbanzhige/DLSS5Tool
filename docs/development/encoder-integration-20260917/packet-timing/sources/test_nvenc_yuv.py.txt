import unittest
import ctypes
from fractions import Fraction
from unittest.mock import Mock
from dlss5tool.nvenc_yuv import YuvEncoderConfig,config_from_contract,NativeYuvEncoder,ReadyYuvFrame,NativeEncoderError
from dlss5tool.encoding_contract import frame_encoding_contract
from dlss5tool.nvenc_yuv import _PacketInfo


class NativeYuvContractTests(unittest.TestCase):
    def test_layout_sizes_and_rate(self):
        c=frame_encoding_contract(320,180,30000/1001,320,180)
        cfg=config_from_contract(c,320,180,codec='h264')
        self.assertEqual((cfg.fps_num,cfg.fps_den),(30000,1001))
        self.assertEqual(cfg.frame_bytes,86400)
        meta=dict(color_transfer='arib-std-b67',color_primaries='bt2020',color_space='bt2020nc')
        cfg=config_from_contract(frame_encoding_contract(320,180,24,320,180,False,meta),320,180,codec='hevc')
        self.assertEqual((cfg.codec,cfg.layout,cfg.frame_bytes),(2,'p010le',172800))

    def test_no_silent_unsupported_color_or_bitrate_conversion(self):
        c=frame_encoding_contract(320,180,24,320,180)
        with self.assertRaises(ValueError):config_from_contract(c,320,180,codec='hevc')
        with self.assertRaises(ValueError):config_from_contract(c,320,180,codec='h264',rate_control='bitrate')
        meta=dict(color_transfer='smpte2084',color_primaries='bt709',color_space='bt709')
        with self.assertRaises(ValueError):config_from_contract(frame_encoding_contract(320,180,24,320,180,False,meta),320,180,codec='hevc')

    def test_invalid_geometry_and_fps(self):
        for w,h,fn,fd in ((321,180,24,1),(320,181,24,1),(320,180,0,1),(320,180,24,0),(99999,99999,24,1)):
            with self.assertRaises(ValueError):YuvEncoderConfig(w,h,fn,fd)

    def session(self):
        s=NativeYuvEncoder.__new__(NativeYuvEncoder)
        s.config=YuvEncoderConfig(320,180,24,1)
        s._check=Mock();s._feed=Mock(return_value=1);s._drain=Mock(return_value=[])
        s._flush=Mock(return_value=1);s._error=Mock(return_value=99)
        s._sequence=0;s._draining=False;s._failed=False;s._packet_count=0
        return s

    def test_wrong_sequence_bytes_layout_do_not_submit_or_advance(self):
        s=self.session()
        for frame in (ReadyYuvFrame(1,86400,'yuv420p',1),ReadyYuvFrame(1,10,'yuv420p',0),ReadyYuvFrame(1,86400,'nv12',0)):
            with self.assertRaises(ValueError):s.write(frame)
        s._feed.assert_not_called();self.assertEqual(s._sequence,0)

    def test_eos_is_idempotent_and_rejects_late_input(self):
        s=self.session();s.write(ReadyYuvFrame(1,86400,'yuv420p',0))
        self.assertEqual(s._sequence,1);s._packet_count=1;s.finish();self.assertEqual(s.finish(),[])
        s._flush.assert_called_once()
        with self.assertRaises(NativeEncoderError):s.write(ReadyYuvFrame(1,86400,'yuv420p',1))

    def test_failed_feed_is_not_silently_replaced(self):
        s=self.session();s._feed.return_value=0
        with self.assertRaises(NativeEncoderError):s.write(ReadyYuvFrame(1,86400,'yuv420p',0))
        self.assertTrue(s._failed);self.assertEqual(s._sequence,0)

    def test_eos_detects_missing_packets(self):
        s=self.session();s._sequence=2;s._packet_count=1
        with self.assertRaises(NativeEncoderError):s.finish()
        self.assertTrue(s._failed)

    def test_reject_wrapping_or_non_integer_frame_indices(self):
        s=self.session()
        for index in (False,0.0,2**32,-1):
            s._sequence=index
            with self.assertRaises(ValueError):s.write(ReadyYuvFrame(1,86400,'yuv420p',index))
        s._feed.assert_not_called()

    def packet_session(self, updates=None):
        s=self.session();s.config=YuvEncoderConfig(320,180,30000,1001)
        s._sequence=4;s._reorder_delay=3;s._buffer=ctypes.create_string_buffer(b'packet',64)
        values=dict(size=40,flags=1,pts=0,dts=-3,duration=1,picture_type=3,reserved=0)
        values.update(updates or {})
        calls=[]
        def pop(buffer,capacity,size,info,info_size):
            size._obj.value=0 if calls else 6
            for key,value in values.items():setattr(info._obj,key,value)
            calls.append(True)
            return 1
        s._pop=pop
        del s._drain  # exercise implementation instead of the write/finish mock
        return s

    def test_packet_abi_and_negative_dts(self):
        self.assertEqual(ctypes.sizeof(_PacketInfo),40)
        s=self.packet_session();packet,=s._drain()
        self.assertEqual((packet.data,packet.pts,packet.dts,packet.duration),(b'packet',0,-3,1))
        self.assertEqual(packet.time_base,Fraction(1001,30000));self.assertTrue(packet.is_keyframe)

    def test_packet_presentation_order_is_not_decode_order(self):
        s=self.packet_session(dict(pts=3,flags=0,picture_type=0));packet,=s._drain()
        self.assertEqual(packet.pts,3);self.assertEqual(packet.dts,-3)
        self.assertFalse(packet.is_keyframe)

    def test_reject_bad_native_metadata(self):
        for values in (dict(size=32),dict(pts=4),dict(pts=-1),dict(duration=0),
                       dict(dts=0),dict(flags=0),dict(picture_type=9),dict(reserved=1)):
            s=self.packet_session(values)
            with self.assertRaises(NativeEncoderError):s._drain()
            self.assertTrue(s._failed)


if __name__=='__main__':unittest.main()
