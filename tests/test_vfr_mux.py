"""End-to-end timestamp preservation through the ordinary FFmpeg writer."""
from fractions import Fraction
import subprocess

import numpy as np
import pytest

from dlss5tool.video_export import FFmpegVideoWriter, find_ffmpeg, find_ffprobe


def test_vfr_writer_preserves_frame_pts_and_duration(tmp_path):
    pytest.importorskip('av')
    if not find_ffprobe(find_ffmpeg()):
        pytest.skip('ffprobe is required to verify output timestamps')
    path = tmp_path / 'vfr.mp4'
    stamps = ['0', '0.05', '0.083333', '0.133333']
    writer = FFmpegVideoWriter(path, 128, 128, 30, use_nvenc=False,
                               frame_timestamps=stamps, timeline_end='0.166667')
    try:
        for index in range(len(stamps)):
            writer.write(np.full((128, 128, 3), index * 48, dtype=np.uint8))
        writer.finish()
    except BaseException:
        writer.abort()
        raise
    raw = subprocess.check_output([
        find_ffprobe(find_ffmpeg()), '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'csv=p=0',
        str(path),
    ], text=True)
    actual = [Fraction(line.strip().split(',')[0]) for line in raw.splitlines() if line.strip()]
    assert actual == [Fraction(value) for value in stamps]
    duration = subprocess.check_output([
        find_ffprobe(find_ffmpeg()), '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
        str(path),
    ], text=True)
    assert Fraction(duration.strip()) == Fraction('0.166667')
