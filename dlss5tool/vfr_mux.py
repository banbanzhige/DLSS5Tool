"""Restore source frame presentation times after the existing FFmpeg encoder.

Only used without frame generation. The intermediate video has one access unit
per source frame and no B frames, so packet order is presentation order.
"""
from fractions import Fraction


def validate_timeline(timestamps, end):
    values = tuple(Fraction(str(value)) for value in timestamps)
    if not values or values[0] != 0 or any(b <= a for a, b in zip(values, values[1:])):
        raise ValueError('源视频时间戳必须从零开始且严格递增')
    end = Fraction(str(end))
    if end <= values[-1]:
        raise ValueError('源视频结束时间早于末帧')
    ticks = tuple(round(value * 1_000_000) for value in values)
    end_tick = round(end * 1_000_000)
    if any(b <= a for a, b in zip(ticks, ticks[1:])) or end_tick <= ticks[-1]:
        raise ValueError('源视频时间戳精度不足以封装')
    return ticks, end_tick


def retime_encoded_video(source, target, timestamps, end, *, cancel=None):
    """Copy encoded packets to a new container with the original PTS/DTS."""
    try:
        import av
    except ImportError as exc:
        raise RuntimeError('变帧率超分导出需要 PyAV 18.1.0') from exc

    ticks, end_tick = validate_timeline(timestamps, end)
    options = {'movflags': '+faststart'} if str(target).lower().endswith(('.mp4', '.mov')) else {}
    with av.open(str(source), mode='r') as encoded, av.open(str(target), mode='w', options=options) as output:
        video = encoded.streams.video[0]
        stream = output.add_stream_from_template(video, opaque=True)
        stream.time_base = Fraction(1, 1_000_000)
        if video.codec_context.name == 'hevc' and str(target).lower().endswith(('.mp4', '.mov')):
            stream.codec_context.codec_tag = 'hvc1'
        count = 0
        for packet in encoded.demux(video):
            if cancel is not None and cancel.is_set():
                from dlss5tool.frame_generation import Cancelled
                raise Cancelled('已取消变帧率封装')
            if packet.dts is None:
                continue
            if count >= len(ticks) or packet.pts != packet.dts:
                raise ValueError('变帧率封装需要每帧一个无 B 帧的编码包')
            packet.pts = packet.dts = ticks[count]
            packet.duration = (ticks[count + 1] if count + 1 < len(ticks) else end_tick) - ticks[count]
            packet.time_base = Fraction(1, 1_000_000)
            packet.stream = stream
            output.mux(packet)
            count += 1
        if count != len(ticks):
            raise ValueError(f'编码帧数与原时间戳不一致：{count}/{len(ticks)}')
