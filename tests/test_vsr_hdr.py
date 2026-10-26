"""HDR buffer admission, conversion diagnostics and exact analysis decoding."""
import ctypes as C
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

from dlss5tool import guidance_color as color, super_resolution as sr


def test_gpu_hdr_admission_includes_both_pitched_host_staging_buffers():
    w,h,scale=1919,1080,2
    estimate=sr.estimate_resources(w,h,scale,True,in_flight=1,use_enhancement=False)
    shared=(w*h+w*h*scale*scale)*8
    staged=((w*8+255)//256*256)*h+((w*scale*8+255)//256*256)*(h*scale)
    assert estimate['vsr_ram_bytes']==shared+staged
    assert estimate['known_gpu_bytes'] >= (w*h+w*h*scale*scale)*16


def test_hdr_extra_staging_pressure_rejects_before_worker_or_shared_memory():
    from dlss5tool.memory_budget import reserve_bytes
    pixels=1920*1080+3840*2160
    state=dict(total_physical_bytes=16*1024**3,free_commit_bytes=16*1024**3)
    state['free_physical_bytes']=reserve_bytes(state)+pixels*12+1
    with mock.patch('dlss5tool.memory_budget.system_memory',return_value=state), \
         mock.patch.object(sr.shared_memory,'SharedMemory') as memory, \
         mock.patch.object(sr.multiprocessing,'get_context') as context:
        with pytest.raises(MemoryError):sr.ProcessSuperResolution(1920,1080,2,is_hdr=True)
    memory.assert_not_called();context.assert_not_called()


@pytest.mark.parametrize('gpu',[0,1])
def test_hdr_reports_actual_native_conversion_route_without_changing_init(gpu):
    def identity(high,low):
        high._obj.value=0;low._obj.value=123
        return 1
    library=SimpleNamespace(vsr_init=mock.Mock(return_value=1),vsr_init_adapter=mock.Mock(),
        vsr_adapter_luid=identity,vsr_hdr_gpu_conversion=mock.Mock(return_value=gpu))
    result=sr._initialize_host(library,1920,1080,2,True,'log')
    assert result['hdr_conversion']==('gpu-rgb10a2' if gpu else 'cpu-rgb10a2')
    library.vsr_init.assert_called_once_with(1920,1080,2,4,1,sr.BASE,'log')


@pytest.mark.parametrize('profile',['hdr10_pq','hdr10_hlg'])
def test_half_eotf_table_matches_original_for_every_finite_encoding(profile):
    bits=np.arange(65536,dtype=np.uint16)
    values=bits.view(np.float16).astype(np.float32)
    valid=np.isfinite(values)
    values=np.clip(values[valid],0,1)
    expected=(color._pq_eotf(values)*100.0 if profile=='hdr10_pq' else color._hlg_eotf(values)*12.0)
    table=color._half_hdr_linear_table(profile)
    np.testing.assert_array_equal(table[bits[valid]],expected)
    assert table.dtype==expected.dtype and not table.flags.writeable


@pytest.mark.parametrize('profile',['hdr10_pq','hdr10_hlg'])
@pytest.mark.parametrize('dtype',[np.float16,np.float32])
@pytest.mark.parametrize('strided',[False,True])
def test_proxy_preserves_legacy_math_range_clamp_and_layout(profile,dtype,strided):
    rng=np.random.default_rng(20261026)
    frame=rng.uniform(-.1,1.2,(130,258,4)).astype(dtype)
    frame[...,3]=np.nan  # Analysis ignores alpha.
    if strided:frame=frame[:,::-1]
    settings=dict(frame_format='rgba16f',color_profile=profile,color_primaries='bt2020')
    # Force the unchanged float32 input route to serve as the exact oracle.
    expected=color.analysis_rgba8(frame.astype(np.float32),settings)
    actual=color.analysis_rgba8(frame,settings)
    np.testing.assert_array_equal(actual,expected)


@pytest.mark.parametrize('profile',['hdr10_pq','hdr10_hlg'])
def test_nonfinite_half_rgb_rejected_before_lookup(profile):
    frame=np.zeros((1,1,4),np.float16);frame[0,0,1]=np.inf
    with mock.patch.object(color,'_half_hdr_linear_table') as table:
        with pytest.raises(ValueError,match='non-finite'):
            color.analysis_rgba8(frame,dict(frame_format='rgba16f',color_profile=profile))
    table.assert_not_called()
