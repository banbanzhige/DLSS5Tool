"""Exact frame/color boundary shared by export and isolated GPU consumers.

This module does not select a new backend. A GPU path must reproduce these
conversion bytes, not silently substitute NVENC's RGB conversion or NV12 filter.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class FrameEncodingContract:
    width: int
    height: int
    fps: float
    wire_format: str
    encoder_format: str
    filter_chain: str
    input_color_args: tuple
    output_color_args: tuple

    def input_args(self):
        return [
            '-f', 'rawvideo', '-pixel_format', self.wire_format,
            '-video_size', f'{self.width}x{self.height}',
            '-framerate', f'{self.fps:.12g}', *self.input_color_args,
            '-i', 'pipe:0', '-an', '-vf', self.filter_chain,
        ]


def frame_encoding_contract(width, height, fps, output_width, output_height,
                            resize_output=False, hdr_metadata=None):
    if width <= 0 or height <= 0 or output_width <= 0 or output_height <= 0:
        raise ValueError('Encoding dimensions must be positive')
    if not math.isfinite(float(fps)) or fps <= 0:
        raise ValueError('Encoding frame rate must be finite and positive')
    if hdr_metadata:
        transfer = hdr_metadata['color_transfer']
        primaries = hdr_metadata['color_primaries']
        matrix = hdr_metadata['color_space']
        geometry = (f'zscale=w={output_width}:h={output_height}:filter=lanczos:'
                    if resize_output else 'pad=ceil(iw/2)*2:ceil(ih/2)*2,zscale=')
        conversion = (f'{geometry}matrixin=gbr:matrix={matrix}:'
                      f'transferin={transfer}:transfer={transfer}:primariesin={primaries}:'
                      f'primaries={primaries}:rangein=full:range=limited,format=p010le')
        return FrameEncodingContract(width, height, fps, 'rgba64le', 'p010le', conversion,
            ('-color_range','pc','-color_primaries',primaries,'-color_trc',transfer),
            ('-color_range','tv','-color_primaries',primaries,'-color_trc',transfer,'-colorspace',matrix))
    conversion = (f'scale={output_width}:{output_height}:flags=lanczos,format=yuv420p'
                  if resize_output else 'pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p')
    return FrameEncodingContract(width, height, fps, 'bgr24', 'yuv420p', conversion, (), ())
