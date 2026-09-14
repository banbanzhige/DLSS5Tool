"""Real Windows audio clock + Tk scheduler using a silent generated test clip.

No user media/settings are touched. Pixel readiness is controlled to test late
callbacks and buffering independently from GPU speed.
"""
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import tkinter as tk
    import numpy as np
    from unittest import mock
    from dlss5tool import preview_audio
    from dlss5tool.shared_render_preview import SharedRenderPreview
    from dlss5tool.video_export import find_ffmpeg
    task = ROOT/'tmp/preview-switches-20260914'
    source = task/'silent-clock.mp4'
    if not source.exists():
        subprocess.run([find_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-n',
            '-f', 'lavfi', '-i', 'color=c=black:s=160x128:r=30:d=5.5',
            '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '5.5',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(source)],
            check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    root = tk.Tk()
    root.withdraw()
    app = SharedRenderPreview()
    app.root, app.playing = root, True
    app._hold_original = app._buffering = False
    app._shared_clock = None
    app._shared_clock_index = app._shared_output_index = 0
    audio = preview_audio.PreviewAudio()
    app._audio = audio
    app._set_play_btn = lambda *_: None
    app.set_status = lambda *_: None
    errors, paints, starts = [], [], []
    app.logln = errors.append
    root.report_callback_exception = lambda *args: errors.append(str(args))
    real_play = audio.play
    def play(frame, fps):
        starts.append((frame, fps))
        return real_play(frame, fps)
    audio.play = play
    def pause():
        app.playing = False
        audio.pause()
        root.quit()
    app.pause = pause
    pixels = (np.zeros((128, 160, 4), np.uint8),)*2
    hole = {'enabled': True, 'started': None}
    def request(index):
        if hole['enabled'] and index >= 90:
            if hole['started'] is None:
                hole['started'] = time.perf_counter()
                root.after(400, lambda: hole.update(enabled=False))
            return None
        return pixels
    session = SimpleNamespace(multiplier=2, metadata={'output_rate': '60', 'source_frames': 165},
        request=request, peek=lambda _: pixels, buffered=lambda i, n: not(hole['enabled'] and i >= 90))
    app._shared_ready = lambda: session
    app._shared_display = lambda: None
    def paint(_, pair, index):
        if audio.mode() == 'playing':
            paints.append((index, audio.position_ms()))
    app._shared_paint = paint
    try:
        # Keep the extracted WAV inside this registered task, too.
        with mock.patch.object(preview_audio.tempfile, 'tempdir', str(task)):
            audio.prepare(str(source), 5.5)
            deadline = time.perf_counter()+15
            while not audio.ready and time.perf_counter() < deadline:
                root.update()
                time.sleep(.01)
        assert audio.has_audio, 'test requires the Windows audio backend'
        started = time.perf_counter()
        root.after(700, lambda: time.sleep(.25))  # deliberately late UI callback
        root.after(15000, lambda: (errors.append('timeout'), pause()))
        root.after(0, app._shared_tick)
        root.mainloop()
        elapsed = time.perf_counter()-started
        assert not errors, errors
        assert len(starts) == 2, starts  # initial start, then one true buffer resume
        assert starts[1][0] >= 90, starts
        assert app._shared_output_index == 329
        drift = max(abs(index/60-ms/1000) for index, ms in paints if ms is not None)
        assert drift < .08, drift
        assert 5.5 <= elapsed < 7, elapsed
        report = {'source_duration_seconds': 5.5, 'output_frames': 330, 'output_fps': 60,
                  'wall_seconds_with_buffer': elapsed, 'max_sampled_clock_difference_ms': drift*1000,
                  'audio_starts': starts, 'late_ui_stall_ms': 250, 'cache_hole_ms': 400,
                  'last_output_index': app._shared_output_index, 'errors': errors}
        (task/'clock-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report), flush=True)
    finally:
        audio.close()
        root.destroy()


if __name__ == '__main__':
    main()
