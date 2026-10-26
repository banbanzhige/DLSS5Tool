"""Pre-start selection of the verified resident HDR pure-SR contract."""
from fractions import Fraction
import os


GEOMETRIES = ((960, 540, 2), (1920, 1080, 2))


def eligible_settings(settings):
    from dlss5tool import appearance
    return (settings.get('hdr_mode') is True and settings.get('super_resolution_scale') == 2
        and settings.get('frame_generation_multiplier', 1) == 1
        and settings.get('output_view', 0) == 0 and not settings.get('output_size')
        and appearance.enhancement_bypass(settings) == 'source'
        and settings.get('nvenc_preset', 'p5') == 'p5'
        and settings.get('rate_control', 'quality') == 'quality'
        and settings.get('quality_profile', 'high') in ('high', 'balanced')
        and settings.get('codec', 'auto') in ('auto', 'hevc'))


def make_plan(inspection, settings, adapter):
    from dlss5tool.gpu_decode import BACKEND
    meta, rate, width, height, count = inspection
    rotations = [meta.get('tags', {}).get('rotate', 0),
                 *[item.get('rotation', 0) for item in meta.get('side_data_list', [])]]
    try:
        rotated = any(float(value) % 360 for value in rotations)
    except (ValueError, TypeError):
        return None
    if (not eligible_settings(settings) or not meta.get('is_hdr') or meta.get('_is_vfr')
            or (width, height, settings['super_resolution_scale']) not in GEOMETRIES
            or meta.get('profile') not in ('hdr10_pq', 'hdr10_hlg')
            or meta.get('pixel_format') not in ('yuv420p10le', 'p010le')
            or meta.get('codec_name') != 'hevc' or meta.get('chroma_location') != 'left'
            or meta.get('color_space') != 'bt2020nc' or meta.get('color_primaries') != 'bt2020'
            or meta.get('color_range') != 'tv' or rotated
            or meta.get('field_order') not in (None, 'unknown', 'progressive')
            or meta.get('decode_backend') not in (None, BACKEND)
            or type(adapter.get('cuda_index')) is not int or not 0 < Fraction(rate) <= 240
            or type(count) is not int or count <= 0):
        return None
    return dict(width=width, height=height, scale=2, rate=str(rate), frames=count,
                quality=settings.get('quality_profile', 'high'), adapter=dict(adapter),
                color_profile=meta['profile'], hdr_metadata={key:meta[key] for key in (
                    'is_hdr', 'profile', 'color_transfer', 'color_primaries', 'color_space',
                    'color_range', 'pixel_format', 'width', 'height') if key in meta})


def try_export(manager, source, output, settings, *, cancel, progress, frame_progress=None):
    from dlss5tool.device_sr_plan import eligible_settings as device_settings, make_plan as device_plan
    if os.name != 'nt' or not device_settings(settings):
        return None
    from dlss5tool.image_sequence import is_sequence
    if is_sequence(source):
        return None
    from dlss5tool.resident_hdr_runtime import load_components
    components = load_components()
    if components is None or manager.has_complete_spatial(source, settings):
        return None
    from dlss5tool.dlss_engine import available_render_adapters
    adapters = available_render_adapters(cancel=cancel)
    requested = settings.get('render_gpu', 'auto')
    adapter = next((item for item in adapters if item['id'] == requested), None) if requested != 'auto' else next(iter(adapters), None)
    if adapter is None:
        raise RuntimeError('Selected render GPU is unavailable')
    if [adapter['vendor_id'], adapter['device_id']] not in components['verified_devices']:
        return None
    inspection = manager.inspect(source, cancel,
        progress=lambda message, fraction=0, counts=None: progress(message, min(1, max(0, fraction or 0))*.05),
        allow_cpu_fallback=settings.get('decode_cpu_fallback') is True)
    plan = device_plan(inspection, settings, adapter)
    if plan is None or plan['frames'] < 96:
        return None
    from dlss5tool.video_export import find_ffmpeg, select_video_encoder
    _, nvenc = select_video_encoder(find_ffmpeg(), plan['width']*plan['scale'], plan['height']*plan['scale'],
        float(Fraction(plan['rate'])*plan['multiplier']), bool(plan.get('hdr_metadata')), True, 'p5', 'quality', plan['quality'],
        settings.get('video_bitrate_mbps', 20), codec='hevc' if plan.get('hdr_metadata') else 'h264')
    if not nvenc:
        return None
    manager.close()
    if frame_progress:
        frame_progress(0, plan['frames']*plan['multiplier'])
    from dlss5tool.resident_sr import export_resident
    result = export_resident(source, output, plan, components, cancel=cancel,
        progress=lambda message, fraction: progress(message, .05+.9*fraction),
        frame_progress=frame_progress, pool_name=manager.pool_name)
    from dlss5tool.i18n import tr
    progress(tr('status.shared_export', hit=0, done=plan['frames'], total=plan['frames']), 1)
    return result
