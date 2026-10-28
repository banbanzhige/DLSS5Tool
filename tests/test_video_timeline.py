"""Source timing inspection must preserve local timing and the final frame."""
from fractions import Fraction as F
from unittest import mock

import pytest

from dlss5tool import video_timeline as timing


def inspect(stamps, **metadata):
    meta = dict(avg_frame_rate='30', start_time='0', duration=str(F(len(stamps), 30)))
    meta.update(metadata)
    closed = []
    def scan(*args, **kwargs):
        try:
            yield from stamps
            kwargs['duration_sink'](metadata.get('_last_duration'))
        finally:
            closed.append(True)
    with mock.patch.object(timing, 'probe_video_stream', return_value=meta), \
            mock.patch.object(timing, 'find_ffmpeg', return_value='ffmpeg'), \
            mock.patch.object(timing, 'iter_timestamps', side_effect=scan):
        result = timing.inspect_video_timeline('source.mp4')
    assert closed == [True]
    return result


def test_equal_total_duration_does_not_hide_variable_local_timing():
    stamps = [str(F(i, 15)) for i in range(30)]
    stamps += [str(2 + F(i, 45)) for i in range(90)]
    timeline = inspect(stamps)
    assert timeline.restore and timeline.frames == 120 and timeline.end == 4
    assert F(timeline.writer_options()['frame_timestamps'][30]) == 2


def test_nonzero_start_normalizes_video_but_keeps_source_origin_for_audio():
    timeline = inspect([str(1 + F(i, 30)) for i in range(3)], start_time='1')
    assert timeline.timestamps == ('0', '1/30', '1/15')
    assert not timeline.restore and timeline.end == F(1, 10)
    assert timeline.writer_options() == {'source_video_start': '1'}


def test_fractional_cfr_keeps_existing_encoder_route():
    rate = F(30000, 1001)
    timeline = inspect([str(i / rate) for i in range(100)],
                       avg_frame_rate=str(rate), duration=str(100 / rate))
    assert not timeline.restore and timeline.rate == rate


def test_short_final_frame_requires_restoring_end_time():
    timeline = inspect(['0', '1/30', '1/15'], duration='0.09')
    assert timeline.restore and timeline.end == F(9, 100)


def test_stream_ticks_preserve_exact_duration_over_rounded_metadata():
    timeline = inspect(['0', '1001/30000'], avg_frame_rate='30000/1001',
                       duration='0.066', time_base='1/30000', duration_ts=2002, start_pts=0)
    assert timeline.end == F(2002, 30000) and not timeline.restore


def test_missing_stream_duration_uses_actual_last_frame_duration():
    timeline = inspect(['0', '0.05', '0.15'], duration=None, _last_duration=F(1, 50))
    assert timeline.end == F(17, 100)


@pytest.mark.parametrize('stamps', [[], ['0', '0'], ['0', '-1'], ['0', 'N/A']])
def test_invalid_timing_is_rejected(stamps):
    with pytest.raises(ValueError, match='时间戳|可解码'):
        inspect(stamps)


def test_cancelled_scan_closes_decoder_and_preserves_decode_choice():
    closed = []
    def scan(source, check_cancel, **options):
        assert options['allow_cpu_fallback'] is True and callable(options['duration_sink'])
        try:
            yield '0'
            raise InterruptedError('cancelled')
        finally:
            closed.append(True)
    with mock.patch.object(timing, 'find_ffmpeg', return_value='ffmpeg'), \
            mock.patch.object(timing, 'probe_video_stream', return_value={'avg_frame_rate': '30'}), \
            mock.patch.object(timing, 'iter_timestamps', side_effect=scan):
        with pytest.raises(InterruptedError):
            timing.inspect_video_timeline('source.mp4', allow_cpu_fallback=True)
    assert closed == [True]
