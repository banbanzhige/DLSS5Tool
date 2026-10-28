"""Inspect exact source timing before an ordinary video export starts.

Pixels still use the selected decoder and encoder. Only streams that differ
from the encoder's constant frame cadence need packet timestamp restoration.
"""
from dataclasses import dataclass
from fractions import Fraction
import time

from dlss5tool.gpu_decode import iter_timestamps
from dlss5tool.video_export import find_ffmpeg, probe_video_stream


@dataclass(frozen=True)
class VideoTimeline:
    timestamps: tuple
    end: Fraction
    start: Fraction
    rate: Fraction
    restore: bool

    @property
    def frames(self):
        return len(self.timestamps)

    def writer_options(self):
        options = {'source_video_start': str(self.start)}
        if self.restore:
            options.update(frame_timestamps=self.timestamps, timeline_end=str(self.end))
        return options


def _fraction(value, default=None):
    try:
        return Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def inspect_video_timeline(source, fps=None, *, check_cancel=None, progress=None,
                           allow_cpu_fallback=False):
    """Normalize decoded PTS while retaining the original first-frame position.

    Cancellation and decoder choice match the pixel path. Invalid or missing
    timing fails before encoding; an average frame rate cannot replace it.
    """
    check_cancel = check_cancel or (lambda: None)
    check_cancel()
    metadata = probe_video_stream(find_ffmpeg(), source)
    rate = next((value for value in (
        _fraction(metadata.get('avg_frame_rate')), _fraction(metadata.get('r_frame_rate')),
        _fraction(fps),
    ) if value is not None and value > 0), None)
    if rate is None:
        raise ValueError('无法确定视频的准确帧率')
    stamps = []
    first = previous = None
    reported = 0.0
    last_duration = [None]
    scan = iter_timestamps(source, check_cancel, **(
        {'allow_cpu_fallback': True} if allow_cpu_fallback else {}),
        duration_sink=lambda value: last_duration.__setitem__(0, value))
    try:
        for field in scan:
            value = _fraction(field)
            if value is None or (previous is not None and value <= previous):
                raise ValueError('视频时间戳缺失、重复或倒退，无法保留原时间线')
            if first is None:
                first = value
            stamps.append(str(value - first))
            previous = value
            now = time.monotonic()
            if progress and (len(stamps) == 1 or now - reported >= .2):
                progress(len(stamps), metadata.get('frames') or 0)
                reported = now
    finally:
        scan.close()
    check_cancel()
    if not stamps:
        raise ValueError('视频没有可解码画面')
    # Stream duration is relative to stream.start_time, not to container zero.
    time_base = _fraction(metadata.get('time_base'))
    duration_ticks = _fraction(metadata.get('duration_ts'))
    start_ticks = _fraction(metadata.get('start_pts'))
    duration = (duration_ticks * time_base if time_base is not None and duration_ticks is not None
                else _fraction(metadata.get('duration')))
    stream_start = (start_ticks * time_base if time_base is not None and start_ticks is not None
                    else _fraction(metadata.get('start_time'), first))
    end = stream_start + duration - first if duration is not None else Fraction(0)
    last = Fraction(stamps[-1])
    if end <= last:
        end = last + (last_duration[0] or (last - Fraction(stamps[-2]) if len(stamps) > 1 else 1 / rate))
    tolerance = Fraction(2, 1_000_000)
    restore = (any(abs(Fraction(stamp) - index / rate) > tolerance
                   for index, stamp in enumerate(stamps))
               or abs(end - len(stamps) / rate) > tolerance)
    if progress:
        progress(len(stamps), len(stamps))
    return VideoTimeline(tuple(stamps), end, first, rate, restore)
