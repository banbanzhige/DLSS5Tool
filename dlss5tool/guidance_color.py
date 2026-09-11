"""Fixed, frame-independent HDR analysis proxy; never modifies render pixels.

The enhancement worker retains its RGBA8 protocol. Only motion analysis sees
this SDR copy; native rendering and encoding retain the original RGBA16F.
Rows are bounded so a large HDR frame does not require whole-frame FP32 copies.
"""
import numpy as np

from dlss5tool.video_export import _pq_eotf, _hlg_eotf

HDR_PROFILES = ('hdr10_pq', 'hdr10_hlg', 'scrgb')
_BT2020_TO_709 = np.array([
    [1.660491, -0.587641, -0.072850],
    [-0.124550, 1.132900, -0.008349],
    [-0.018151, -0.100579, 1.118730],
], dtype=np.float32)


def analysis_rgba8(frame, settings):
    if settings.get('frame_format', 'rgba8') != 'rgba16f':
        if frame.dtype != np.uint8:
            raise ValueError('SDR guidance requires RGBA8')
        return frame
    profile = settings.get('color_profile')
    if profile not in HDR_PROFILES:
        raise ValueError('HDR guidance requires an explicit PQ, HLG or scRGB profile')
    if frame.dtype not in (np.float16, np.float32) or frame.ndim != 3 or frame.shape[2] != 4:
        raise ValueError('HDR guidance requires RGBA16F/32F')
    primaries = settings.get('color_primaries', 'bt2020')
    if profile != 'scrgb' and primaries not in ('bt2020', 'bt709'):
        raise ValueError('HDR guidance supports BT.2020 or BT.709 primaries')
    result = np.empty(frame.shape, dtype=np.uint8)
    for top in range(0, frame.shape[0], 128):
        rgb = frame[top:top + 128, :, :3].astype(np.float32)
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
    def __init__(self, source, color_info, *, start_frame=0):
        from dlss5tool.video_export import FFmpegHDRVideoReader
        self.settings = {'frame_format': 'rgba16f', 'color_profile': color_info['profile'],
                         'color_primaries': color_info.get('color_primaries', 'bt2020')}
        self.reader = FFmpegHDRVideoReader(source, int(color_info['width']), int(color_info['height']),
                                          color_info, start_frame=start_frame)

    def read(self):
        frame = self.reader.read()
        if frame is None:
            return None
        return np.ascontiguousarray(analysis_rgba8(frame, self.settings)[..., 2::-1])

    def close(self):
        self.reader.close()
