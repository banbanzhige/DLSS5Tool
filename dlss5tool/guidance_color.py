"""Fixed, frame-independent HDR analysis proxy; never modifies render pixels.

The enhancement worker retains its RGBA8 protocol. Only motion analysis sees
this SDR copy; native rendering and encoding retain the original RGBA16F.
Rows are bounded so a large HDR frame does not require whole-frame FP32 copies.
"""
from functools import lru_cache
import numpy as np

from dlss5tool.video_export import _pq_eotf, _hlg_eotf

HDR_PROFILES = ('hdr10_pq', 'hdr10_hlg', 'scrgb')
_BT2020_TO_709 = np.array([
    [1.660491, -0.587641, -0.072850],
    [-0.124550, 1.132900, -0.008349],
    [-0.018151, -0.100579, 1.118730],
], dtype=np.float32)


def _hdr_analysis_format(frame, settings):
    profile = settings.get('color_profile')
    if profile not in HDR_PROFILES:
        raise ValueError('HDR guidance requires an explicit PQ, HLG or scRGB profile')
    if frame.dtype not in (np.float16, np.float32) or frame.ndim != 3 or frame.shape[2] != 4:
        raise ValueError('HDR guidance requires RGBA16F/32F')
    primaries = settings.get('color_primaries', 'bt2020')
    if profile != 'scrgb' and primaries not in ('bt2020', 'bt709'):
        raise ValueError('HDR guidance supports BT.2020 or BT.709 primaries')
    return profile, primaries


def validate_analysis_frame(frame, settings):
    """Retain input checks when a cache consumer reuses upstream scene cuts.

    Checking bounded rows avoids tone mapping or allocating a full RGBA8 proxy.
    Alpha is ignored, as in analysis_rgba8's existing RGB validation contract.
    """
    if settings.get('frame_format', 'rgba8') != 'rgba16f':
        if frame.dtype != np.uint8:
            raise ValueError('SDR guidance requires RGBA8')
        return
    _hdr_analysis_format(frame, settings)
    for top in range(0, frame.shape[0], 128):
        if not np.isfinite(frame[top:top + 128, :, :3]).all():
            raise ValueError('HDR guidance input contains non-finite pixels')


@lru_cache(maxsize=2)
def _half_hdr_linear_table(profile):
    """Evaluate the unchanged EOTF once for every half encoding, not per pixel.

    Only finite RGB indices are consumed. Keep NumPy's resulting dtype (HLG
    currently yields float64); downcasting would change the analysis proxy.
    """
    values = np.arange(65536, dtype=np.uint16).view(np.float16).astype(np.float32)
    values[~np.isfinite(values)] = 0
    values = np.clip(values, 0, 1)
    if profile == 'hdr10_pq':
        result = _pq_eotf(values) * 100.0
    elif profile == 'hdr10_hlg':
        result = _hlg_eotf(values) * 12.0
    else:
        raise ValueError('Half EOTF table requires PQ or HLG')
    result.flags.writeable = False
    return result


def analysis_rgba8(frame, settings):
    if settings.get('frame_format', 'rgba8') != 'rgba16f':
        if frame.dtype != np.uint8:
            raise ValueError('SDR guidance requires RGBA8')
        return frame
    profile, primaries = _hdr_analysis_format(frame, settings)
    result = np.empty(frame.shape, dtype=np.uint8)
    for top in range(0, frame.shape[0], 128):
        block = frame[top:top + 128, :, :3]
        if frame.dtype == np.float16 and frame.flags.c_contiguous and profile != 'scrgb':
            bits = block.view(np.uint16)
            if np.any((bits & 0x7c00) == 0x7c00):
                raise ValueError('HDR guidance input contains non-finite pixels')
            linear = _half_hdr_linear_table(profile)[bits]
        else:
            rgb = block.astype(np.float32)
            if not np.isfinite(rgb).all():
                raise ValueError('HDR guidance input contains non-finite pixels')
            if profile == 'hdr10_pq':
                linear = _pq_eotf(np.clip(rgb, 0, 1)) * 100.0
            elif profile == 'hdr10_hlg':
                linear = _hlg_eotf(np.clip(rgb, 0, 1)) * 12.0
            else:
                linear = rgb  # linear BT.709; signed/over-white render values stay intact
        if profile != 'scrgb' and primaries == 'bt2020':
            linear = np.einsum('...c,dc->...d', linear, _BT2020_TO_709)
        linear = np.maximum(linear, 0)
        # Fixed Reinhard curve preserves highlight gradation without per-frame
        # auto exposure, so identical pixels keep identical analysis values.
        mapped = linear / (1.0 + linear)
        srgb = np.where(mapped <= 0.0031308, mapped * 12.92,
                        1.055 * np.power(mapped, 1.0 / 2.4) - 0.055)
        result[top:top + 128, :, :3] = (np.clip(srgb, 0, 1) * 255 + 0.5).astype(np.uint8)
    result[..., 3] = 255
    return result


class HDRAnalysisReader:
    """Decode an HDR video to fixed SDR BGR analysis copies, never for export color."""
    def __init__(self, source, color_info, *, start_frame=0, allow_cpu_fallback=False):
        from dlss5tool.video_export import FFmpegHDRVideoReader
        self.settings = {'frame_format': 'rgba16f', 'color_profile': color_info['profile'],
                         'color_primaries': color_info.get('color_primaries', 'bt2020')}
        self.reader = FFmpegHDRVideoReader(source, int(color_info['width']), int(color_info['height']),
                                          color_info, start_frame=start_frame,
                                          **({'allow_cpu_fallback': True} if allow_cpu_fallback else {}))

    def read(self):
        frame = self.reader.read()
        if frame is None:
            return None
        return np.ascontiguousarray(analysis_rgba8(frame, self.settings)[..., 2::-1])

    def close(self):
        self.reader.close()
