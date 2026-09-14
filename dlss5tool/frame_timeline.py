"""Exact, backend-independent frame-generation timeline and plan contracts.

This module does not enable frame generation in the GUI. A native backend must
pass the quality/compatibility gates before any plan is executable. No fallback,
automatic quality reduction or interpretation of an average FPS as VFR timing.
"""
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Iterator, Optional, Tuple


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')
    return value


def _time(value, name):
    if not isinstance(value, (int, Fraction)) or isinstance(value, bool):
        raise ValueError(f'{name} requires integer ticks/Fraction, not float')
    return Fraction(value)


@dataclass(frozen=True)
class ColorContract:
    pixel_format: str
    transfer: str
    primaries: str
    range: str = 'full'

    def __post_init__(self):
        if (self.pixel_format, self.transfer, self.primaries, self.range) not in {
            ('rgba8', 'srgb', 'bt709', 'full'),
            ('rgba16f', 'pq', 'bt2020', 'full'),
            ('rgba16f', 'hlg', 'bt2020', 'full'),
        }:
            raise ValueError('Unvalidated frame-generation color contract')


@dataclass(frozen=True)
class ProcessingPlan:
    source_size: Tuple[int, int]
    spatial_scale: int
    multiplier: int
    color: ColorContract
    final_size: Optional[Tuple[int, int]] = None
    source_rate: Optional[Fraction] = None
    enhance: bool = True

    def __post_init__(self):
        if not isinstance(self.source_size, tuple) or len(self.source_size) != 2:
            raise ValueError('source_size must be an immutable width/height pair')
        for value in self.source_size:
            _integer(value, 'source dimension', 1)
        _integer(self.spatial_scale, 'spatial scale', 1)
        _integer(self.multiplier, 'frame multiplier', 1)
        if self.spatial_scale not in (1, 2, 4) or self.multiplier not in (1, 2, 3, 4, 6):
            raise ValueError('Unsupported scale or multiplier; no automatic fallback')
        if not isinstance(self.color, ColorContract) or type(self.enhance) is not bool:
            raise ValueError('Invalid plan contract')
        if self.final_size is not None:
            if not isinstance(self.final_size, tuple) or len(self.final_size) != 2:
                raise ValueError('final_size must be an immutable width/height pair')
            for value in self.final_size:
                _integer(value, 'final dimension', 1)
        if self.source_rate is not None:
            rate = _time(self.source_rate, 'source_rate')
            if rate <= 0:
                raise ValueError('source_rate must be positive')
            object.__setattr__(self, 'source_rate', rate)

    @property
    def upscaled_size(self):
        return tuple(value * self.spatial_scale for value in self.source_size)

    @property
    def output_size(self):
        return self.final_size or self.upscaled_size

    @property
    def output_rate(self):
        return None if self.source_rate is None else self.source_rate * self.multiplier

    @property
    def stages(self):
        stages = ['decode_timestamped']
        if self.spatial_scale != 1:
            stages.append('super_resolve_real_frames')
        if self.enhance:
            stages.append('enhance_real_frames')
        stages.append('compose_real_frame_strength')
        if self.output_size != self.upscaled_size:
            stages.append('explicit_final_resize')
        if self.multiplier != 1:
            stages.extend(('analyze_final_frame_pair', 'dlssg_frame_group'))
        return tuple(stages) + ('timeline', 'comparison_ui_or_encode')

    def require_backend(self, *, sizes, multipliers, colors, video_inputs_verified):
        """Accept only explicit, quality-verified backend capabilities.

        A provider's advertised MultiFrameCountMax is not certification. The
        caller supplies tested contracts, not merely settings or GPU model names.
        """
        if self.multiplier == 1:
            return
        if video_inputs_verified is not True:
            raise ValueError('DLSSG video input contract has not passed validation')
        if self.output_size not in sizes:
            raise ValueError('Final dimensions not validated; refusing resolution reduction')
        if self.multiplier not in multipliers:
            raise ValueError('Requested multiplier not validated; refusing fallback')
        if self.color not in colors:
            raise ValueError('Color contract not validated; refusing SDR/bit-depth fallback')


@dataclass(frozen=True)
class SourceFrame:
    index: int
    pts: Fraction
    duration: Fraction
    scene: int = 0
    epoch: int = 0

    def __post_init__(self):
        for name in ('index', 'scene', 'epoch'):
            _integer(getattr(self, name), name)
        object.__setattr__(self, 'pts', _time(self.pts, 'pts'))
        object.__setattr__(self, 'duration', _time(self.duration, 'duration'))
        if self.duration <= 0:
            raise ValueError('Frame duration must be positive')


@dataclass(frozen=True)
class OutputSample:
    pts: Fraction
    duration: Fraction
    kind: str
    left_index: int
    right_index: Optional[int]
    sub_index: int
    multiplier: int
    scene: int
    epoch: int


def frame_group(left: SourceFrame, right: Optional[SourceFrame], multiplier: int):
    """Own [left.pts, right.pts), with no extra ownership at segment seams."""
    _integer(multiplier, 'multiplier', 1)
    if multiplier not in (1, 2, 3, 4, 6):
        raise ValueError('Unsupported frame multiplier')
    if right is not None:
        if right.epoch != left.epoch:
            raise ValueError('Stale/mixed epochs must not enter a frame pair')
        if right.index != left.index + 1 or right.pts <= left.pts:
            raise ValueError('Frame pair must be consecutive and ordered')
        if left.pts + left.duration != right.pts:
            raise ValueError('Timing gap/overlap must be explicitly resolved, not hidden')
    step = left.duration / multiplier
    cut = right is not None and right.scene != left.scene
    return tuple(OutputSample(
        pts=left.pts + k * step, duration=step,
        kind=('real' if k == 0 else 'endpoint_hold' if right is None else
              'cut_hold' if cut else 'generated'),
        left_index=left.index,
        right_index=right.index if right is not None else None,
        sub_index=k, multiplier=multiplier, scene=left.scene, epoch=left.epoch,
    ) for k in range(multiplier))


def iter_timeline(frames: Iterable[SourceFrame], multiplier: int, *, owned=None, final_segment=False) -> Iterator[OutputSample]:
    """Streaming VFR/CFR scheduler; retains at most two frame descriptors.

    An owned half-open PTS interval trims segment warmup/right overlap. The
    caller must provide the right lookahead frame; it is never fabricated.
    """
    _integer(multiplier, 'multiplier', 1)
    if multiplier not in (1, 2, 3, 4, 6):
        raise ValueError('Unsupported frame multiplier')
    if owned is not None:
        start, end = (_time(value, 'owned interval') for value in owned)
        if end <= start:
            raise ValueError('Empty/reversed owned interval')
    iterator = iter(frames)
    left = next(iterator, None)
    while left is not None:
        right = next(iterator, None)
        if (owned is not None and not final_segment and right is None
                and left.pts < end and left.pts + left.duration > start):
            raise ValueError('Non-final segment is missing its right lookahead frame')
        for sample in frame_group(left, right, multiplier):
            if owned is not None and sample.pts < start < sample.pts + sample.duration:
                raise ValueError('Owned interval starts inside a sample duration')
            if owned is None or start <= sample.pts < end:
                if owned is not None and sample.pts + sample.duration > end:
                    raise ValueError('Owned interval cuts through a sample duration')
                yield sample
        left = right


def require_frame_group(samples, generated_count, valid, *, epoch):
    """Reject incomplete/stale/native-invalid groups before writing any pixels."""
    samples = tuple(samples)
    if not samples or any(sample.epoch != epoch for sample in samples):
        raise ValueError('Empty or stale frame group')
    first = samples[0]
    if (first.kind != 'real' or len(samples) != first.multiplier
            or any(s.multiplier != first.multiplier or s.left_index != first.left_index
                   or s.right_index != first.right_index or s.sub_index != i
                   or s.duration != first.duration or s.pts != first.pts + i * first.duration
                   or s.scene != first.scene
                   or s.kind not in ('real', 'generated', 'endpoint_hold', 'cut_hold')
                   or (i > 0 and s.kind == 'real') for i, s in enumerate(samples))):
        raise ValueError('Malformed or incomplete frame group')
    if len({s.kind for s in samples[1:]}) > 1:
        raise ValueError('A frame group cannot mix generated and held subframes')
    if any((s.kind == 'endpoint_hold' and s.right_index is not None)
           or (s.kind in ('generated', 'cut_hold') and s.right_index is None) for s in samples):
        raise ValueError('Frame kind does not match pair endpoints')
    expected = sum(sample.kind == 'generated' for sample in samples)
    _integer(generated_count, 'generated count')
    if generated_count != expected:
        raise ValueError('Native frame count does not match timeline')
    if expected and valid is not True:
        raise ValueError('Native interpolation invalid; no repeated-frame fallback')
