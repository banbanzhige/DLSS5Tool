"""Device-frame lifetime, native handoff and temporal failure boundaries."""
from contextlib import nullcontext
import ctypes as C
from fractions import Fraction
from types import SimpleNamespace as NS
from unittest import mock

import numpy as np
import pytest

from dlss5tool.cuda_frame import CudaImage, SharedCudaImage, FrameTiming
from dlss5tool.cuda_interop import SharedCudaBuffer
from dlss5tool.device_consumers import DeviceConsumers
from dlss5tool.dlss_host_process import ProcessLive, HostProcessError
from dlss5tool.frame_generation import NativeStream
from dlss5tool.nvofa import OpticalFlow


class Session:
    def __init__(self, context=123):
        self.context,self.luid=C.c_void_p(context),456
        self.buffers={};self.next=100
        self.cuda=NS(cuMemcpy2D_v2=mock.Mock(side_effect=self._copy))
        self.synchronize=mock.Mock()
    def _check(self):pass
    def current(self):return nullcontext()
    def allocate(self,size):
        self.next+=1;self.buffers[self.next]=bytearray(size);return self.next
    def free(self,pointer):del self.buffers[pointer]
    def upload(self,pointer,array):self.buffers[pointer][:]=array.tobytes()
    def download(self,pointer,size):return bytes(self.buffers[pointer][:size])
    def _copy(self,argument):
        value=argument._obj
        for row in range(value.height):
            self.buffers[value.ddevice][row*value.dpitch:row*value.dpitch+value.width]=self.buffers[value.sdevice][
                row*value.spitch:row*value.spitch+value.width]
        return 0


@pytest.mark.parametrize('layout,profile,channels,dtype', [('rgba8','srgb',4,np.uint8),
    ('rgba16f','hdr10_hlg',4,np.float16),('gray8','analysis',1,np.uint8)])
def test_device_clone_keeps_pitched_pixels_timing_and_blocks_reuse(layout,profile,channels,dtype):
    session=Session();pixels=np.arange(8*4*channels,dtype=dtype).reshape(4,8,channels)
    timing=FrameTiming(5,15,Fraction(1,90),True)
    with CudaImage(session,8,4,layout=layout,color_profile=profile,row_pitch=8*channels*np.dtype(dtype).itemsize+16) as source, \
         CudaImage(session,8,4,layout=layout,color_profile=profile) as target:
        with source.upload(pixels,timing) as frame,target.copy_from(frame) as output:
            np.testing.assert_array_equal(target.download(output),pixels)
            assert output.timing is timing
            with pytest.raises(RuntimeError,match='held'):target.copy_from(frame)
        assert session.cuda.cuMemcpy2D_v2.call_count==1
    assert not session.buffers


def test_shared_mapping_cannot_close_until_image_and_all_consumers_release():
    session=Session()
    mapping=SharedCudaBuffer.__new__(SharedCudaBuffer)
    mapping.session,mapping.size=session,4*8*4
    mapping.pointer,mapping.external=C.c_uint64(999),C.c_void_p(888)
    mapping._image_owners=0
    mapping._free,mapping._destroy=mock.Mock(return_value=0),mock.Mock(return_value=0)
    image=SharedCudaImage(mapping,8,4,32)
    frame=image.complete(FrameTiming(0))
    with pytest.raises(RuntimeError,match='image owners'):mapping.close()
    with pytest.raises(RuntimeError,match='held'):image.close()
    frame.close();image.close();mapping.close()
    assert mapping._image_owners==0 and not mapping.pointer.value
    with pytest.raises(RuntimeError):_ = frame.pointer


def test_foreign_gray_context_rejected_before_optical_flow_driver_call():
    flow=OpticalFlow.__new__(OpticalFlow)
    flow._closed=False;flow.w,flow.h=8,4;flow.context=C.c_void_p(123)
    flow._current=mock.Mock()
    with CudaImage(Session(999),8,4,layout='gray8',color_profile='analysis') as image:
        with image.complete(FrameTiming(0)) as frame:
            with pytest.raises(ValueError,match='context'):flow.calculate_cuda_gray(frame,frame)
    flow._current.assert_not_called()


def _unmanaged_gpu_live(descriptor_field):
    live=ProcessLive.__new__(ProcessLive)
    live._w,live._h=8,4
    live.settings={'host_backend':'v2','preset':1,'intensity':1.0,'frame_format':'rgba8','color_profile':'srgb'}
    live.backend=live.preference='v2'
    live.adapter_info={}
    live.runtime_path=''
    live._live_factory=None
    live._session=mock.Mock(backend='v2',adapter_info={},runtime_path='',max_in_flight=1,
                            supports_async=False,tiled=False,_validate_configuration=False,reason=None)
    setattr(live,descriptor_field,{'handle':9})
    return live


@pytest.mark.parametrize('descriptor_field',['_gpu_input_descriptor','_gpu_output_descriptor'])
@pytest.mark.parametrize('operation',[lambda live:live.update({'preset':2}),lambda live:live.resize(16,8)])
def test_mapped_enhancement_owner_requires_retirement_before_reconfiguration(descriptor_field,operation):
    live=_unmanaged_gpu_live(descriptor_field)
    with pytest.raises(HostProcessError,match='Retire'):operation(live)
    live._session.request.assert_not_called()


@pytest.mark.parametrize('descriptor_field',['_gpu_input_descriptor','_gpu_output_descriptor'])
def test_unmanaged_gpu_owner_still_allows_appearance_only_update(descriptor_field):
    live=_unmanaged_gpu_live(descriptor_field)
    live.update({'intensity':0.5})
    live._session.request.assert_called_once()
    assert live._session.request.call_args.args[0]=='update'
    assert live.settings['intensity']==0.5


def test_failed_consumer_cannot_advance_temporal_history_again():
    consumer=DeviceConsumers.__new__(DeviceConsumers)
    consumer._closed=consumer._failed=consumer._finished=False
    consumer._feed=mock.Mock(side_effect=RuntimeError('consumer rejected output'))
    with pytest.raises(RuntimeError,match='rejected'):consumer.feed(None)
    with pytest.raises(RuntimeError,match='cannot resume'):consumer.feed(None)
    consumer._feed.assert_called_once()


def test_gpu_fg_rejects_wrong_sequence_before_copying_or_sending():
    stream=NativeStream.__new__(NativeStream)
    stream._cuda_input=mock.Mock();stream._source_frames=3
    stream.shape=(4,8,4);stream.dtype=np.uint8;stream._cuda_color='srgb'
    with CudaImage(Session(),8,4) as image:
        with image.complete(FrameTiming(0)) as frame:
            with pytest.raises(ValueError,match='sequence'):
                stream.process_cuda_frame(frame,np.zeros((4,8,2),np.float32),False,mock.Mock())
    stream._cuda_input.copy_from.assert_not_called()
