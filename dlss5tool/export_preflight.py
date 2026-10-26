"""Advisory export risks; duration is not a VRAM requirement or OOM forecast."""
import math

from dlss5tool import appearance
from dlss5tool.memory_budget import memory_headroom_bytes
from dlss5tool.host_queue import configured_in_flight
from dlss5tool.super_resolution import estimate_resources, normalize_scale

LONG_VIDEO_SECONDS = 10*60


def assess_video_export(width, height, frames, fps, settings, *, memory=None,
                        gpu_memory=None, cache_bytes=0):
    scale = normalize_scale(settings.get('super_resolution_scale', 1))
    hdr = bool(settings.get('_source_is_hdr') and settings.get('hdr_mode', True))
    enhancement = appearance.enhancement_bypass(settings) is None
    slots = 1 if scale > 1 or settings.get('frame_generation_multiplier', 1) > 1 else configured_in_flight(settings)
    estimate = estimate_resources(width, height, scale, hdr, in_flight=slots,
        use_super_resolution=scale > 1, use_enhancement=enhancement,
        decode_buffer=1 if hdr else settings.get('decode_buffer',4),
        frame_generation_multiplier=settings.get('frame_generation_multiplier',1))
    try:
        rate = float(fps)
        duration = max(0, int(frames))/rate if math.isfinite(rate) and rate > 0 else None
    except (ValueError, TypeError, OverflowError):
        duration = None
    warnings = []
    headroom = memory_headroom_bytes(memory=memory) if memory else None
    if headroom is not None and estimate['recommended_ram_bytes'] > headroom:
        warnings.append('ram_peak')
    free_gpu = (gpu_memory or {}).get('free_bytes', 0)
    # These buffers omit opaque model/driver allocations: this is advice,
    # never a claim that an otherwise low-risk workload must finish.
    if free_gpu and estimate['recommended_gpu_bytes'] > free_gpu:
        warnings.append('vram_peak')
    elif free_gpu and estimate['recommended_gpu_bytes'] >= free_gpu*.7:
        warnings.append('vram_margin')
    if scale > 1 and estimate['output_width']*estimate['output_height'] > 3840*2160:
        warnings.append('large_output')
    if scale > 1 and duration is not None and duration >= LONG_VIDEO_SECONDS:
        warnings.append('long_run')
    if scale > 1 and (not memory or not free_gpu):
        warnings.append('unknown_resources')
    return {'warnings': warnings, 'duration_seconds': duration,
            'risk': 'high' if {'ram_peak', 'vram_peak'} & set(warnings) else
                    'medium' if warnings else 'low',
            'estimate': estimate, 'ram_headroom_bytes': headroom,
            'free_gpu_bytes': free_gpu or None, 'cache_budget_bytes': max(0, int(cache_bytes)),
            'limits': 'advisory; no duration-based VRAM or failure-time prediction'}
