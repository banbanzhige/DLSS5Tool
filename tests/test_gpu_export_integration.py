import ctypes
from dataclasses import replace
from fractions import Fraction
import io
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

import numpy as np

from dlss5tool.frame_generation import NativeStream,Cancelled
from dlss5tool.nvenc_yuv import EncodedYuvPacket,YuvEncoderConfig
from dlss5tool.packet_mux import PacketVideoWriter
from dlss5tool.gpu_video_export import NativeGpuVideoWriter


class DeviceProtocolTests(unittest.TestCase):
    def stream(self,flags=(1,1,1)):
        stream=NativeStream.__new__(NativeStream)
        stream.gpu_mode=True;stream.cancel=threading.Event()
        stream.shape=(2,2,4);stream.dtype=np.uint8;stream.multiplier=len(flags)+1
        stream.proc=Mock();stream.proc.poll.return_value=None
        stream.proc.stdin=io.BytesIO();stream.proc.stdout=io.BytesIO(b''.join(struct.pack('<I',v) for v in flags))
        stream.call=lambda callback:callback()
        return stream

    def test_each_subframe_consumed_on_owner_before_ack(self):
        s=self.stream();owner=threading.get_ident();acks=[]
        def consume(index,valid):
            self.assertEqual(threading.get_ident(),owner);self.assertTrue(valid)
            acks.append(s.proc.stdin.getvalue().count(struct.pack('<I',0xA11CE001)))
        s.process_device(np.zeros(s.shape,np.uint8),np.zeros((2,2,2),np.float32),False,consume)
        self.assertEqual(acks,[0,1,2])
        self.assertEqual(s.proc.stdin.getvalue().count(struct.pack('<I',0xA11CE001)),3)

    def test_callback_failure_kills_worker_without_reuse_ack(self):
        s=self.stream()
        with self.assertRaisesRegex(RuntimeError,'consumer failed'):
            s.process_device(np.zeros(s.shape,np.uint8),np.zeros((2,2,2),np.float32),False,
                Mock(side_effect=RuntimeError('consumer failed')))
        s.proc.kill.assert_called_once()
        self.assertNotIn(struct.pack('<I',0xA11CE001),s.proc.stdin.getvalue())

    def test_cancel_between_ready_and_consume(self):
        s=self.stream();s.cancel.set();consume=Mock()
        with self.assertRaises(Cancelled):
            s.process_device(np.zeros(s.shape,np.uint8),np.zeros((2,2,2),np.float32),False,consume)
        consume.assert_not_called();s.proc.kill.assert_called_once()

    def test_bad_validity_flag_is_not_accepted(self):
        s=self.stream((9,))
        with self.assertRaisesRegex(RuntimeError,'validity'):
            s.process_device(np.zeros(s.shape,np.uint8),np.zeros((2,2,2),np.float32),False,Mock())
        s.proc.kill.assert_called_once()


class PacketBoundaryTests(unittest.TestCase):
    def test_order_validation_before_import_or_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'out.mp4'
            writer=PacketVideoWriter(path,YuvEncoderConfig(320,180,24,1),b'headers')
            base=EncodedYuvPacket(b'data',0,-3,1,Fraction(1,24),True,3)
            for packet in (replace(base,pts=1),replace(base,is_keyframe=False),
                           replace(base,duration=0),replace(base,time_base=Fraction(1,30))):
                with self.assertRaises(ValueError):writer.write(packet)
            self.assertFalse(path.exists());writer.abort();writer.abort()

    def test_empty_timeline_cannot_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'out.mp4'
            writer=PacketVideoWriter(path,YuvEncoderConfig(320,180,24,1),b'headers')
            with self.assertRaises(ValueError):writer.finish(0)
            self.assertFalse(path.exists())

    def test_no_overwrite_on_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'out.mp4';path.touch()
            with self.assertRaises(FileExistsError):
                PacketVideoWriter(path,YuvEncoderConfig(320,180,24,1),b'headers')

    def test_shared_resource_freed_before_cuda_session(self):
        s=NativeGpuVideoWriter.__new__(NativeGpuVideoWriter);order=[]
        s._cuda=Mock();s._cuda.current.return_value.__enter__=Mock();s._cuda.current.return_value.__exit__=Mock()
        s._cuda.close.side_effect=lambda:order.append('context')
        shared=Mock();shared.close.side_effect=lambda:order.append('shared');s._interop=[shared]
        s._encoder=Mock();s._encoder.close.side_effect=lambda:order.append('encoder')
        s._converter=Mock();s._converter.close.side_effect=lambda:order.append('converter')
        s._close_gpu()
        self.assertEqual(order,['shared','encoder','converter','context'])

    def test_audio_publish_does_not_overwrite_competing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'out.mp4';video=Path(directory)/'candidate.mp4';video.touch()
            writer=PacketVideoWriter(target,YuvEncoderConfig(320,180,24,1),b'headers',audio_source='audio')
            writer._container=Mock();writer._temp_path=str(video)
            writer._count=writer._next_display=1
            def mux(*_):
                target.touch()  # A competing writer creates final destination while muxing.
                return 'audio'
            with patch('dlss5tool.video_export.find_ffmpeg',return_value='ffmpeg'), \
                 patch('dlss5tool.video_export.mux_source_audio',side_effect=mux), \
                 patch('dlss5tool.packet_mux.os.rename',side_effect=FileExistsError('race')):
                with self.assertRaises(FileExistsError):writer.finish(1)
            self.assertTrue(target.exists());self.assertFalse(video.exists())
            self.assertEqual(list(Path(directory).iterdir()),[target])


if __name__=='__main__':unittest.main()
