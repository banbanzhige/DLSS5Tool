"""Synchronous resident BT.2020 limited P010 to coded PQ/HLG half frames.

The kernel preserves production zscale's left/center bilinear chroma, matrix,
16-bit rounding and half quantization. Other color/chroma contracts stay outside
this route. Decoder capsules and their context checks are shared with SDR.
"""
from dlss5tool.cuda_nv12 import CudaNv12Converter


class CudaP010Converter(CudaNv12Converter):
    software_format, dtype_bits, color_layout = 'p010le', 16, 'rgba16f'
    kernel_name = b'p010_to_rgba'

    def __init__(self, session, ptx_path, width, height, *, color_profile):
        if color_profile not in ('hdr10_pq', 'hdr10_hlg'):
            raise ValueError('P010 converter requires an explicit PQ/HLG color profile')
        if (width, height) not in ((960, 540), (1920, 1080)):
            raise ValueError('P010 exact conversion currently verifies 960x540 and 1920x1080 inputs')
        self.color_profile = color_profile
        super().__init__(session, ptx_path, width, height, color_profile=color_profile)

    def _coefficients(self, frame):
        if (frame.colorspace != 9 or frame.color_range != 1 or frame.color_primaries != 9
                or frame.color_trc != (16 if self.color_profile == 'hdr10_pq' else 18)):
            raise ValueError('P010 frame color tags differ from the selected HDR contract')
        return ()
