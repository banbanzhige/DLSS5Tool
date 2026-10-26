"""Characterize resident HDR decoding against the exact production zscale chain.

Exploratory CPU models are evidence, not an enabled replacement. No tolerance
is accepted and mismatches are recorded in both 16-bit codes and half bits.
"""
import argparse
import itertools
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def model(y, uv, horizontal, vertical, order, normalize):
    height, width = y.shape
    def floating(values, offset, divisor):
        values = values.astype(np.float32)
        if normalize == 'subtract':
            return (values-np.float32(offset))*np.float32(1/divisor)
        return (values.astype(np.float64)*float(np.float32(1/divisor))
                + float(np.float32(-offset/divisor))).astype(np.float32)
    y = floating(y, 64, 876)
    uv = uv.astype(np.float32) if normalize.startswith('word') else floating(uv, 512, 896)
    def resize(values, axis, shift):
        count = (width if axis == 1 else height)
        coordinates = (np.arange(count, dtype=np.float32)+np.float32(shift))/2
        lo = np.floor(coordinates).astype(int)
        fraction = coordinates-lo
        upper = np.clip(lo+1, 0, values.shape[axis]-1)
        lower = np.clip(lo, 0, values.shape[axis]-1)
        a, b = np.take(values, lower, axis=axis), np.take(values, upper, axis=axis)
        fraction = fraction.reshape((1, -1, 1) if axis == 1 else (-1, 1, 1)).astype(np.float32)
        return (a*(1-fraction)+b*fraction).astype(np.float32)
    operations = ((1, horizontal), (0, vertical)) if order == 'xy' else ((0, vertical), (1, horizontal))
    for index, (axis, shift) in enumerate(operations):
        uv = resize(uv, axis, shift)
        if normalize == 'word' or normalize == 'word-first' and index == 0:
            uv = np.floor(uv+.5).clip(0, 1023)
        if normalize == 'word-first' and index == 0:
            uv = floating(uv, 512, 896)
    if normalize == 'word':
        uv = floating(uv, 512, 896)
    kr, kb = .2627, .0593
    matrix = np.array([[1, 0, 2*(1-kr)],
                       [1, -2*kb*(1-kb)/(1-kr-kb), -2*kr*(1-kr)/(1-kr-kb)],
                       [1, 2*(1-kb), 0]], np.float32)
    result = []
    for row in matrix:
        value = y*row[0]
        for channel in range(2):
            value = (uv[..., channel].astype(np.float64)*float(row[channel+1])
                     + value.astype(np.float64)).astype(np.float32)
        result.append(np.rint(np.clip(value, 0, 1)*65535).astype(np.uint16))
    result.append(np.full((height, width), 65535, np.uint16))
    return np.stack(result, axis=-1)


def main():
    from scripts.build_vsr_cuda import safe, digest
    from dlss5tool.video_export import find_ffmpeg, _zscale_decode_filter
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--width', type=int, default=64)
    parser.add_argument('--height', type=int, default=32)
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    report = task/(args.label+'.json')
    if not (task/'TASK.md').is_file() or report.exists() or not args.label.replace('-', '').isalnum():
        raise ValueError('Fresh registered evidence required')
    color = dict(color_transfer='smpte2084', color_primaries='bt2020',
                 color_space='bt2020nc', color_range='tv')
    width, height = args.width, args.height
    if width % 8 or height % 2 or min(width, height) < 16 or width*height > 1920*1080:
        raise ValueError('Bounded even geometry required')
    rng = np.random.default_rng(20261026)
    y = rng.integers(0, 1024, (height, width), np.uint16)
    uv = rng.integers(0, 1024, (height//2, width//2, 2), np.uint16)
    raw = (y<<6).tobytes()+(uv<<6).tobytes()
    source_reference = None
    if args.source:
        from dlss5tool.video_export import probe_video_stream
        source = safe(args.source, task)
        metadata = probe_video_stream(find_ffmpeg(), source)
        width, height = int(metadata['width']), int(metadata['height'])
        start = [find_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-hwaccel', 'cuda',
                 '-hwaccel_output_format', 'cuda', '-i', str(source), '-frames:v', '1', '-an', '-vf']
        extract = subprocess.run([*start, 'hwdownload,format=p010le', '-pix_fmt', 'p010le',
                                  '-f', 'rawvideo', 'pipe:1'], capture_output=True, check=True, timeout=20)
        raw = extract.stdout
        y = np.frombuffer(raw[:width*height*2], np.uint16).reshape(height, width)>>6
        uv = np.frombuffer(raw[width*height*2:], np.uint16).reshape(height//2, width//2, 2)>>6
        source_reference = subprocess.run([*start, 'hwdownload,format=p010le,'+_zscale_decode_filter(color),
            '-pix_fmt', 'rgba64le', '-f', 'rawvideo', 'pipe:1'], capture_output=True, check=True, timeout=20).stdout
    command = [find_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo',
               '-pixel_format', 'p010le', '-video_size', f'{width}x{height}',
               '-color_range', 'tv', '-colorspace', 'bt2020nc', '-i', 'pipe:0',
               '-frames:v', '1', '-an', '-vf', _zscale_decode_filter(color),
               '-pix_fmt', 'rgba64le', '-f', 'rawvideo', 'pipe:1']
    output = subprocess.run(command, input=raw, capture_output=True, check=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    reference = np.frombuffer(source_reference or output.stdout, np.uint16).reshape(height, width, 4)
    half_reference = (reference.astype(np.float32)/65535).astype(np.float16).view(np.uint16)
    cases = []
    for horizontal, vertical, order, normalize in itertools.product(
            (0, -.5), (0, -.5), ('xy', 'yx'), ('subtract', 'fma', 'word', 'word-first')):
        candidate = model(y, uv, horizontal, vertical, order, normalize)
        half = (candidate.astype(np.float32)/65535).astype(np.float16).view(np.uint16)
        cases.append(dict(horizontal=horizontal, vertical=vertical, order=order, normalize=normalize,
                          code_mismatches=int(np.count_nonzero(candidate != reference)),
                          half_mismatches=int(np.count_nonzero(half != half_reference)),
                          max_code_error=int(np.abs(candidate.astype(int)-reference.astype(int)).max())))
    cases.sort(key=lambda row: row['half_mismatches'])
    report.write_text(json.dumps(dict(exploratory=True, automatic_route_enabled=False,
        source_sha256=digest(Path(__file__)), command=command, cases=cases,
        raw_vs_tagged_source_codes_equal=source_reference is None or output.stdout == source_reference), indent=2), encoding='utf-8')
    print(json.dumps(dict(best=cases[:4], raw_vs_source_equal=source_reference is None or output.stdout == source_reference)))


if __name__ == '__main__':
    main()
