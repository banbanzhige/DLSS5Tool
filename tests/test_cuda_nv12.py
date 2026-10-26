"""NVDEC surface/capsule ownership and same-context encoder handoff boundaries."""
from contextlib import nullcontext
import ctypes as C
from fractions import Fraction
from types import SimpleNamespace as NS
from unittest import mock

import pytest

from dlss5tool.cuda_nv12 import CudaNv12Converter, _Tensor, _Device, _Dtype
from dlss5tool.cuda_frame import CudaImage, FrameTiming
from dlss5tool.gpu_video_export import NativeGpuVideoWriter


@pytest.mark.parametrize('profile', ['hdr10_pq', 'hdr10_hlg'])
def test_hdr_encoder_accepts_only_matching_completed_half_color_lease(profile):
    cuda = session()
    writer = NativeGpuVideoWriter.__new__(NativeGpuVideoWriter)
    writer._cuda, writer.adapter_luid = cuda, cuda.luid
    writer.is_hdr, writer.color_profile = True, profile
    writer.width, writer.height, writer._frames = 8, 4, 0
    writer.write_device = mock.Mock()
    with CudaImage(cuda, 8, 4, row_pitch=80, layout='rgba16f', color_profile=profile) as image:
        with image.complete(FrameTiming(0)) as frame:
            writer.write_cuda_frame(frame)
            writer.write_device.assert_called_once_with(frame.pointer, frame.nbytes, 'rgba16f', cuda.luid, 80, strict_hdr=True)
            writer.color_profile = 'hdr10_hlg' if profile == 'hdr10_pq' else 'hdr10_pq'
            with pytest.raises(ValueError, match='format'): writer.write_cuda_frame(frame)


def session():
    cuda = mock.Mock(luid=123,context=C.c_void_p(456),device=C.c_int(0))
    cuda.current.side_effect = nullcontext
    cuda.allocate.return_value = 100
    return cuda


def surface(cuda, *, wrong_context=False, wrong_device=False, wrong_stride=False):
    converter = CudaNv12Converter.__new__(CudaNv12Converter)
    converter.session,converter.width,converter.height = cuda,64,48
    def context(out, attribute, address):
        C.cast(out,C.POINTER(C.c_void_p))[0] = 999 if wrong_context else cuda.context.value
        return 0
    converter._pointer_context = mock.Mock(side_effect=context)
    planes=[]
    for index,(shape,strides) in enumerate((((48,64),(96,1)),((24,32,2),(96,2,1)))):
        if wrong_stride: strides=(97,*strides[1:])
        tensor=_Tensor(1000+index*10000,_Device(2,0),len(shape),_Dtype(1,8,1),
            (C.c_int64*len(shape))(*shape),(C.c_int64*len(shape))(*strides),0)
        make=C.pythonapi.PyCapsule_New
        make.argtypes,make.restype=[C.c_void_p,C.c_char_p,C.c_void_p],C.py_object
        plane=NS(line_size=96,tensor=tensor)
        plane.__dlpack_device__=lambda: (2,1 if wrong_device else 0)
        plane.__dlpack__=lambda t=tensor: make(C.addressof(t),b'dltensor',None)
        planes.append(plane)
    return converter,NS(format=NS(name='cuda'),sw_format=NS(name='nv12'),width=64,height=48,planes=planes)


def test_decoder_capsules_retain_both_pitched_planes_in_same_owner():
    converter,frame=surface(session())
    capsules,addresses,pitches=converter._planes(frame)
    assert len(capsules)==2 and addresses==[1000,11000] and pitches==[96,96]


@pytest.mark.parametrize('option', ['wrong_context','wrong_device','wrong_stride'])
def test_foreign_or_malformed_nvdec_planes_rejected(option):
    converter,frame=surface(session(),**{option:True})
    with pytest.raises(ValueError): converter._planes(frame)


def test_conversion_waits_before_export_and_releases_capsules_after_kernel():
    cuda=session(); order=[]
    converter=CudaNv12Converter.__new__(CudaNv12Converter)
    converter.session=cuda;converter._closed=converter._failed=False
    cuda.synchronize.side_effect=lambda:order.append('producer-complete')
    capsules=[object(),object()]
    converter._planes=mock.Mock(side_effect=lambda _: (order.append('export') or capsules,[1,2],[64,64]))
    converter._produce=mock.Mock(side_effect=lambda *_: order.append('kernel-complete') or 'lease')
    assert converter.convert(NS(colorspace=2,color_range=1),FrameTiming(0))=='lease'
    assert order==['producer-complete','export','kernel-complete'] and capsules==[]


def test_encoder_requires_exact_owner_and_sequence_and_preserves_source_lease():
    cuda=session()
    writer=NativeGpuVideoWriter.__new__(NativeGpuVideoWriter)
    writer._cuda=cuda;writer.is_hdr=False;writer.adapter_luid=123
    writer.width,writer.height,writer._frames=64,48,0
    writer.write_device=mock.Mock()
    with CudaImage(cuda,64,48) as image:
        with image.complete(FrameTiming(0,0,Fraction(1,30))) as frame:
            writer.write_cuda_frame(frame)
            assert frame.pointer==100 and image._leases==1
            writer.write_device.assert_called_once_with(100,64*48*4,'rgba',123,64*4)
            writer._frames=1
            with pytest.raises(ValueError,match='sequence'):writer.write_cuda_frame(frame)
            writer._frames=0;writer._cuda=session()
            with pytest.raises(ValueError,match='owner'):writer.write_cuda_frame(frame)


def test_borrowed_encoder_closes_resources_without_releasing_context():
    writer=NativeGpuVideoWriter.__new__(NativeGpuVideoWriter)
    writer._cuda=cuda=session();writer._owns_cuda=False;writer._upload=None;writer._interop=[]
    writer._encoder,writer._converter=mock.Mock(),mock.Mock()
    writer._close_gpu()
    cuda.close.assert_not_called();cuda.free.assert_not_called()
