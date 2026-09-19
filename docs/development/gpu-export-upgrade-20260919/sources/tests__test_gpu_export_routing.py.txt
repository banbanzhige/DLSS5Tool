from pathlib import Path
from fractions import Fraction
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

import numpy as np

from dlss5tool.cuda_hdr_yuv import chroma_tables,CudaHdrConverter
from dlss5tool.encoding_contract import frame_encoding_contract
from dlss5tool.gpu_export_runtime import eligible,create_video_writer
from dlss5tool.gpu_export_process import ProcessGpuVideoWriter


class HdrContractTests(unittest.TestCase):
    def test_chroma_filters_preserve_dc_and_stay_inside_image(self):
        for size in (16,32,180,322,2160):
            for precision in (4096,16384):
                indices,weights=chroma_tables(size,precision)
                self.assertEqual(indices.shape,(size//2,8))
                self.assertTrue(np.all(weights.sum(axis=1)==precision))
                self.assertGreaterEqual(indices.min(),0);self.assertLess(indices.max(),size)

    def test_hdr_contract_rejects_scaling_sdr_wrong_matrix(self):
        for metadata in (None,dict(color_transfer='smpte2084',color_primaries='bt709',color_space='bt709')):
            contract=frame_encoding_contract(320,180,24,320,180,False,metadata)
            with self.assertRaises(ValueError):CudaHdrConverter.from_contract('unused',contract)
        metadata=dict(color_transfer='arib-std-b67',color_primaries='bt2020',color_space='bt2020nc')
        contract=frame_encoding_contract(320,180,24,640,360,True,metadata)
        with self.assertRaises(ValueError):CudaHdrConverter.from_contract('unused',contract)
        contract=frame_encoding_contract(320,180,24,320,180,False,metadata)
        with patch.object(CudaHdrConverter,'__init__',return_value=None) as initialize:
            CudaHdrConverter.from_contract('ptx',contract);initialize.assert_called_once_with('ptx',320,180)


class RoutingTests(unittest.TestCase):
    def test_unsupported_contracts_keep_original_encoder(self):
        for options in (dict(use_nvenc=False),dict(nvenc_preset='p7'),dict(rate_control='bitrate'),
                        dict(quality_profile='maximum'),dict(codec='hevc'),dict(output_size=(640,360))):
            self.assertFalse(eligible(320,180,**options))
            with patch('dlss5tool.video_export.FFmpegVideoWriter') as legacy:
                create_video_writer('out.mp4',320,180,24,**options)
                legacy.assert_called_once_with('out.mp4',320,180,24,**options)

    def test_missing_components_do_not_change_existing_export(self):
        with patch('dlss5tool.gpu_export_runtime.load_components',return_value=None), \
             patch('dlss5tool.video_export.FFmpegVideoWriter') as legacy:
            create_video_writer('out.mp4',320,180,24)
            legacy.assert_called_once()

    def test_cpu_origin_sdr_never_auto_selects_known_slower_path(self):
        with patch('dlss5tool.gpu_export_runtime.load_components') as load, \
             patch('dlss5tool.video_export.FFmpegVideoWriter') as legacy:
            create_video_writer('out.mp4',1920,1080,24)
            load.assert_not_called();legacy.assert_called_once()

    def test_native_capability_probe_can_select_original_software(self):
        with patch('dlss5tool.video_export.select_video_encoder',return_value=('h264',False)), \
             patch('dlss5tool.video_export.find_ffmpeg',return_value='ffmpeg'), \
             patch('dlss5tool.video_export.FFmpegVideoWriter') as legacy:
            create_video_writer('out.mp4',320,180,24,gpu_options={'sdr_ptx':'ptx'})
            legacy.assert_called_once()


class WorkerContainmentTests(unittest.TestCase):
    def session(self):
        s=ProcessGpuVideoWriter.__new__(ProcessGpuVideoWriter)
        s._cancel=None;s._timeout=0;s._closed=False;s._failed=False
        s._connection=Mock();s._connection.poll.return_value=False;s._proc=Mock()
        s._proc.is_alive.return_value=True;s._terminate=Mock()
        return s

    def test_timeout_terminates_owner(self):
        s=self.session()
        with self.assertRaisesRegex(RuntimeError,'timed out'):s._receive()
        s._terminate.assert_called_once()

    def test_disconnect_terminates_owner(self):
        s=self.session();s._connection.send.side_effect=BrokenPipeError()
        with self.assertRaisesRegex(RuntimeError,'connection'):s._request({})
        s._terminate.assert_called_once()

    def test_cancel_terminates_owner(self):
        from dlss5tool.frame_generation import Cancelled
        s=self.session();s._cancel=threading.Event();s._cancel.set()
        with self.assertRaises(Cancelled):s._receive()
        s._terminate.assert_called_once()
