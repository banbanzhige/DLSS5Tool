"""Isolated residency candidates. Not imported by the application.

All inputs to ResidentInputs must be immutable, session-local prepared tensors.
Synchronous transfer and one calling stream are intentional. This is not a
cross-stream/cross-process transport or an approved production cache.
"""


class ResidentInputs:
    """Keep just the last frame pair, keyed by strong object identity."""

    def __init__(self, device):
        self.device = device
        self.entries = []
        self.uploads = 0
        self.hits = 0

    def pair(self, inputs):
        result, fresh = [], []
        for source in inputs:
            cached = next((gpu for cpu, gpu in self.entries + fresh if cpu is source), None)
            if cached is None:
                cached = source.to(self.device)
                self.uploads += 1
            else:
                self.hits += 1
            result.append(cached)
            if not any(cpu is source for cpu, _ in fresh):
                fresh.append((source, cached))
        self.entries = fresh
        return tuple(result)

    def clear(self):
        self.entries.clear()


def hdr_transfer(value, profile, encode=False):
    """Direct Torch candidate; numerical identity is NOT assumed."""
    import torch
    value = value.float().clamp_min(0)
    if profile == 'hdr10_pq':
        m1, m2 = 2610.0 / 16384.0, 2523.0 / 32.0
        c1, c2, c3 = 3424.0 / 4096.0, 2413.0 / 128.0, 2392.0 / 128.0
        if encode:
            value = value ** m1
            return ((c1 + c2 * value) / (1.0 + c3 * value).clamp_min(1e-7)) ** m2
        value = value ** (1.0 / m2)
        return ((value - c1).clamp_min(0) / (c2 - c3 * value).clamp_min(1e-7)) ** (1.0 / m1)
    if profile != 'hdr10_hlg':
        raise ValueError('PQ/HLG only')
    import math
    a = 0.17883277
    b = 1 - 4 * a
    c = 0.5 - a * math.log(4 * a)
    if encode:
        return torch.where(value <= 1 / 12, torch.sqrt(3 * value),
                           a * torch.log((12 * value - b).clamp_min(1e-7)) + c)
    return torch.where(value <= .5, value * value / 3, (torch.exp((value - c) / a) + b) / 12)


def compose_hdr(original, processed, mix, profile):
    import torch
    mix = max(0.0, min(5.0, float(mix)))
    if mix == 0:
        return original
    if mix == 1:
        return processed
    a, b = original.float(), processed.float()
    al = hdr_transfer(a[..., :3], profile)
    bl = hdr_transfer(b[..., :3], profile)
    rgb = hdr_transfer((al + (bl - al) * mix).clamp_min(0), profile, encode=True).clamp(0, 1)
    alpha = (a[..., 3:4] + (b[..., 3:4] - a[..., 3:4]) * mix).clamp(0, 1)
    return torch.cat((rgb, alpha), dim=-1).half()


def analysis_hdr(frame, profile, primaries='bt2020'):
    import torch
    linear = hdr_transfer(frame[..., :3].float().clamp(0, 1), profile)
    linear = linear * (100.0 if profile == 'hdr10_pq' else 12.0)
    if primaries == 'bt2020':
        # Explicit arithmetic, no TF32 matrix multiply.
        r, g, b = linear.unbind(-1)
        linear = torch.stack((1.660491*r - .587641*g - .072850*b,
                              -.124550*r + 1.132900*g - .008349*b,
                              -.018151*r - .100579*g + 1.118730*b), dim=-1)
    elif primaries != 'bt709':
        raise ValueError('BT.709/2020 only')
    linear = linear.clamp_min(0)
    mapped = linear / (1 + linear)
    srgb = torch.where(mapped <= .0031308, mapped * 12.92,
                       1.055 * mapped ** (1 / 2.4) - .055)
    rgba = torch.empty(frame.shape, device=frame.device, dtype=torch.uint8)
    rgba[..., :3] = (srgb.clamp(0, 1) * 255 + .5).to(torch.uint8)
    rgba[..., 3] = 255
    return rgba


def depth_finish(prediction, size, prior=None, reset=False, low=1, high=99, smoothing=.9):
    """Naive all-GPU candidate, intentionally audited against NumPy/OpenCV."""
    import torch
    import torch.nn.functional as functional
    bounds = torch.quantile(prediction.flatten().double(),
                            torch.tensor([low / 100, high / 100], device=prediction.device, dtype=torch.float64))
    if prior is not None and not reset:
        bounds = smoothing * prior + (.1 if smoothing == .9 else 1-smoothing) * bounds
    # Production NumPy uses FP32 array arithmetic with scalar bounds.
    values = ((prediction - bounds[0].float()) / (bounds[1]-bounds[0]).clamp_min(1e-6).float()).clamp(0, 1)
    out = functional.interpolate(values[None, None], size=(size[1], size[0]),
                                 mode='bilinear', align_corners=False)[0, 0]
    return out, bounds


def opencv_float_resize(value, size):
    """Mirror the previously validated double->float coordinate construction.

Experimental Torch gather implementation, no change to production interpolation.
Not presumed equivalent to all OpenCV SIMD kernels; compare every output bit.
"""
    import torch
    import numpy as np
    height, width = value.shape
    def axis(src, dst, horizontal):
        coord = ((np.arange(dst, dtype=np.float64)+.5)*(src/dst)-.5).astype(np.float32)
        base = np.floor(coord).astype(np.int64)
        fraction = coord-base.astype(np.float32)
        if horizontal:
            fraction[(base<0)|(base>=src-1)] = 0
        return (torch.from_numpy(np.clip(base,0,src-1)).to(value.device),
                torch.from_numpy(np.clip(base+1,0,src-1)).to(value.device),
                torch.from_numpy(fraction).to(value.device))
    x0,x1,ax = axis(width,size[0],True)
    y0,y1,ay = axis(height,size[1],False)
    rows = value[:,x0]*(1-ax) + value[:,x1]*ax
    return rows[y0]*(1-ay[:,None]) + rows[y1]*ay[:,None]
