"""One bounded functional smoke; no quality comparisons or repeated benchmarks."""
import argparse
from collections import deque
import json
import os
from pathlib import Path
import sys
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    if not (work / 'TASK.md').is_file():
        parser.error('Register task first')
    os.environ.update(TEMP=str(work), TMP=str(work), PYTHONDONTWRITEBYTECODE='1',
                      DLSS5TOOL_SETTINGS_PATH=str(work / 'settings.json'),
                      DLSS5TOOL_QUEUE_PATH=str(work / 'queue.json'),
                      DLSS5TOOL_DISABLE_UPDATE_CHECK='1',
                      DLSS5TOOL_GUIDANCE_PYTHON=str(ROOT / 'tmp/guidance-cuda-env/Scripts/python.exe'))
    import cv2
    import tkinter as tk
    from dlss5tool import app_settings, gui, guidance_client
    from dlss5tool.preview_decoder import PreviewDecoder
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.video_export import FFmpegVideoWriter
    source = 'F:/project/test/dlss5/测试素材/260429广寒宫98s.mp4'
    report = {}
    app_settings.save({'guidance_mode': 0, 'preview_cache_mb': 256})
    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *error: errors.append(str(error))
    app = gui.App(root)
    def wait_reload():
        deadline = time.monotonic() + 8
        while app._module_reload_thread is not None and time.monotonic() < deadline:
            root.update()
            time.sleep(.005)
        assert app._module_reload_thread is None, 'mode reload timeout'
        assert not errors, errors
    try:
        h = app._host_settings
        h['v_in_flight'].set(8)
        app._on_host_settings_change()
        with mock.patch.object(guidance_client, 'preflight', return_value={'device': 'cuda'}):
            h['v_guidance'].set(gui.tr('guidance.mode.1'))
            app._on_mod_settings_change()
            wait_reload()
            assert not h['v_zero_fast'].get()
            h['v_in_flight'].set(16)
            app._on_host_settings_change()
            h['v_guidance'].set(gui.tr('guidance.mode.0'))
            app._on_mod_settings_change()
            wait_reload()
            assert h['v_zero_fast'].get()
            assert h['v_in_flight'].get() == 8, 'zero profile not restored'
            h['v_guidance'].set(gui.tr('guidance.mode.1'))
            app._on_mod_settings_change()
            wait_reload()
            assert h['v_in_flight'].get() == 16, 'flow profile not restored'
        app._save_settings_now()
        saved = app_settings.load()
        assert saved['host_mode_profiles']['zero']['host_in_flight'] == 8
        assert saved['host_mode_profiles']['guidance']['host_in_flight'] == 16
        report['ui_start_toggle_profiles_save'] = 'passed'
    finally:
        app._on_close()

    decoder = PreviewDecoder()
    try:
        for frame in (0, 1, 12):
            deadline = time.monotonic() + 8
            image = None
            while image is None and time.monotonic() < deadline:
                image = decoder.get(source, frame, {'is_hdr': False})
                time.sleep(.005)
            assert image is not None, f'background decode {frame} timed out'
        decoder.invalidate()
        report['background_decode_and_seek'] = 'passed'
    finally:
        decoder.close()
        decoder._thread.join(3)
        assert not decoder._thread.is_alive(), 'decoder did not stop'

    cap = cv2.VideoCapture(source)
    frames = []
    for _ in range(20):
        ok, bgr = cap.read()
        assert ok
        frames.append(cv2.cvtColor(cv2.resize(bgr, (360, 640)), cv2.COLOR_BGR2RGBA))
    cap.release()
    for mode, depth in ((0, 1), (1, 16)):
        settings = dict(guidance_mode=mode, host_backend='v2', host_auto_fallback=False,
                        host_submission='compatibility' if mode == 0 else 'merged',
                        host_in_flight=depth, host_persistent_buffers=True,
                        host_zero_fast_path=mode == 0, guidance_flow_edge=128,
                        guidance_flow_updates=6, guidance_cache_mb=0,
                        style=0, intensity=1.0)
        live = ProcessLive(360, 640, settings)
        output = work / f'mode-{mode}.mp4'
        writer = FFmpegVideoWriter(str(output), 360, 640, 30)
        pending = deque()
        written = 0
        try:
            assert live.max_in_flight == depth, (mode, live.max_in_flight)
            def consume(result):
                nonlocal written
                assert result is not None
                pending.popleft()
                writer.write(cv2.cvtColor(result, cv2.COLOR_RGBA2BGR))
                written += 1
            for index, rgba in enumerate(frames):
                pending.append(index)
                if live.supports_async:
                    assert live.enqueue(rgba, reset=index == 0)
                    if len(pending) >= live.max_in_flight:
                        consume(live.dequeue())
                else:
                    consume(live.process(rgba, reset=index == 0))
            while pending:
                consume(live.dequeue())
            writer.finish()
            if mode:
                assert live.guidance_info['transport'] == 'shared_memory_flow_v2'
            check = cv2.VideoCapture(str(output))
            decoded = 0
            while check.read()[0]:
                decoded += 1
            check.release()
            assert written == decoded == 20, (written, decoded)
            report[f'mode_{mode}_render_export'] = dict(frames=decoded, actual_queue=live.max_in_flight,
                transport=live.guidance_info.get('transport'), encoder=writer.encoder_name)
        finally:
            writer.abort()
            live.close()
    (work / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
