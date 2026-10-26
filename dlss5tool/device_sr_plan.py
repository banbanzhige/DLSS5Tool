"""Settings/source selection shared by isolated GPU export and preview owners."""
from dlss5tool import appearance


def eligible_settings(settings):
    bypass = appearance.enhancement_bypass(settings)
    neutral = bypass is not None or (not appearance.active(settings)
        and settings.get('output_mix', 1) == 1 and settings.get('guidance_mode', 0) == 0)
    if bypass is None and (settings.get('host_backend','auto') not in ('auto','v2')
            or settings.get('host_tiled_mode') or settings.get('host_submission','merged')!='merged'
            or not settings.get('host_persistent_buffers',True)):
        return False
    return (neutral and settings.get('super_resolution_scale') in (2, 4)
        and settings.get('frame_generation_multiplier', 1) in (1, 2, 3, 4)
        and settings.get('output_view', 0) == 0 and not settings.get('output_size')
        and settings.get('nvenc_preset', 'p5') == 'p5'
        and settings.get('rate_control', 'quality') == 'quality'
        and settings.get('quality_profile', 'high') in ('high', 'balanced'))


def make_plan(inspection, settings, adapter):
    if not eligible_settings(settings):
        return None
    meta = inspection[0]
    if bool(meta.get('is_hdr')) != bool(settings.get('hdr_mode')):
        return None
    hdr = bool(meta.get('is_hdr'))
    if settings.get('codec', 'auto') not in (('auto', 'hevc') if hdr else ('auto', 'h264')):
        return None
    pure = dict(settings, frame_generation_multiplier=1, nr_quality={'total':0})
    if hdr:
        from dlss5tool.resident_hdr import make_plan as make_hdr_plan
        plan = make_hdr_plan(inspection, pure, adapter)
    else:
        from dlss5tool.resident_sr import make_plan as make_sdr_plan
        plan = make_sdr_plan(inspection, pure, adapter)
    if plan is None or not 1920*1080 <= plan['width']*plan['height']*plan['scale']**2 <= 3840*2160:
        return None
    # The FG D3D worker currently selects the first renderer; verify the CUDA
    # owner before startup, and verify the actual LUID again on its handshake.
    if settings.get('frame_generation_multiplier', 1) > 1 and adapter.get('cuda_index') != 0:
        return None
    return dict(plan, color_profile=meta['profile'] if hdr else 'srgb',
                multiplier=settings.get('frame_generation_multiplier', 1),
                nr=appearance.enhancement_required(settings), consumer_settings=dict(settings))
