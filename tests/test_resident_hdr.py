"""Pre-frame HDR/consumer routing must preserve unsupported source contracts."""
from fractions import Fraction
from unittest import mock

import pytest

from dlss5tool import app_settings
from dlss5tool.device_sr_plan import make_plan
from dlss5tool.cuda_p010 import CudaP010Converter

ADAPTER=dict(id='gpu',cuda_index=0,luid_high=0,luid_low=123,vendor_id=4318,device_id=10115)
META=dict(is_hdr=True,profile='hdr10_pq',pixel_format='yuv420p10le',codec_name='hevc',
          color_space='bt2020nc',color_primaries='bt2020',color_range='tv',chroma_location='left')


def settings(**change):
    return app_settings.validate(dict(super_resolution_scale=2,frame_generation_multiplier=2,
        nr_quality={'total':1},hdr_mode=True,guidance_mode=0,output_mix=1,output_view=0)|change)


def test_combined_consumer_plan_keeps_rational_output_rate_and_original_settings():
    config=settings()
    plan=make_plan((META,Fraction(30000,1001),960,540,96),config,ADAPTER)
    assert plan['rate']=='30000/1001' and plan['multiplier']==2 and plan['nr']
    assert plan['consumer_settings']==config and plan['color_profile']=='hdr10_pq'


@pytest.mark.parametrize('change',[dict(_is_vfr=True),dict(chroma_location='center'),dict(color_range='pc'),
    dict(pixel_format='yuv420p12le'),dict(profile='scrgb'),dict(codec_name='av1'),
    dict(color_space='bt2020c'),dict(tags={'rotate':'90'}),dict(field_order='tt')])
def test_unverified_hdr_source_stays_outside_the_device_route(change):
    assert make_plan((META|change,Fraction(30),960,540,96),settings(),ADAPTER) is None


@pytest.mark.parametrize('change',[dict(output_mix=.5),dict(nr_quality={'total':.75}),
    dict(guidance_mode=1),dict(output_view=2),dict(super_resolution_scale=4)])
def test_unverified_composition_or_hdr_geometry_keeps_existing_pipeline(change):
    assert make_plan((META,Fraction(30),960,540,96),settings(**change),ADAPTER) is None


def test_second_cuda_device_does_not_enter_default_adapter_fg_worker():
    assert make_plan((META,Fraction(30),960,540,96),settings(),ADAPTER|{'cuda_index':1}) is None


def test_small_hdr_geometry_is_rejected_before_cuda_loading_or_allocating():
    session=mock.Mock()
    with pytest.raises(ValueError,match='exact conversion'):
        CudaP010Converter(session,'unused',480,270,color_profile='hdr10_hlg')
    session.allocate.assert_not_called()
