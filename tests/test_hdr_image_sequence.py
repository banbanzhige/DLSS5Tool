"""HDR sequence contracts and production preview/render paths; no GPU needed."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from unittest import mock

import cv2
import numpy as np
import pytest

from dlss5tool.image_sequence import ImageSequence, open_capture
from dlss5tool.video_export import FFmpegHDRVideoReader, tone_map_hdr_preview


def write_png(path, pixels):
    ok, encoded = cv2.imencode('.png', pixels)
    assert ok
    encoded.tofile(path)


@pytest.fixture(params=['hdr10_pq', 'hdr10_hlg'])
def hdr_sequence(request, tmp_path):
    # >256 distinct values and unequal channels reveal both 8-bit quantization
    # and RGB/BGR swaps. Only transfer-coded synthetic fixtures, not camera HDR.
    ramp = np.arange(128 * 128, dtype=np.uint16).reshape(128, 128) * 3
    pixels = np.stack((ramp, ramp // 2 + 4096, ramp // 3 + 8192), axis=-1)
    for i in range(3):
        write_png(tmp_path / f'高光_{i:04}.png', pixels + i * 33)
    sequence = ImageSequence.scan(tmp_path / '高光_0000.png', '24000/1001', color_profile=request.param)
    return sequence, sequence.save(tmp_path / 'records')


def test_manifest_metadata_and_color_identity(hdr_sequence):
    sequence, manifest = hdr_sequence
    assert ImageSequence.load(manifest) == sequence
    assert json.loads(Path(manifest).read_text(encoding='utf-8'))['version'] == 2
    info = sequence.metadata
    assert info['is_hdr'] and info['profile'] == sequence.color_profile
    assert info['color_primaries'] == 'bt2020'
    assert info['color_space'] == 'bt2020nc' and info['color_range'] == 'pc'
    assert info['pixel_format'] == 'rgb48le'
    assert info['color_transfer'] == ('smpte2084' if sequence.color_profile == 'hdr10_pq' else 'arib-std-b67')
    other = replace(sequence, color_profile='hdr10_hlg' if sequence.color_profile == 'hdr10_pq' else 'hdr10_pq')
    other_manifest = other.save(Path(manifest).parent)
    from dlss5tool.render_cache import render_identity
    assert render_identity(manifest, {}) != render_identity(other_manifest, {})


def test_reader_preserves_precision_seek_eof_and_close(hdr_sequence):
    sequence, manifest = hdr_sequence
    with mock.patch('dlss5tool.video_export.find_ffmpeg', side_effect=AssertionError('not a video')), \
            mock.patch('dlss5tool.video_export.subprocess',
                SimpleNamespace(Popen=mock.Mock(side_effect=AssertionError('no decoder process')))):
        reader = FFmpegHDRVideoReader(manifest, 128, 128, sequence.metadata, start_frame=1)
        for i in (1, 2):
            rgba = reader.read()
            assert rgba.dtype == np.float16 and rgba.flags.c_contiguous
            np.testing.assert_array_equal(rgba[..., :3],
                (sequence.read(i)[..., ::-1].astype(np.float32) / 65535).astype(np.float16))
            assert np.all(rgba[..., 3] == 1)
            assert len(np.unique(rgba[..., 0])) > 256
        assert reader.read() is None
        reader.close()
        reader.close()
        assert reader.read() is None
        past_end = FFmpegHDRVideoReader(manifest, 128, 128, sequence.metadata, start_frame=10)
        assert past_end.read() is None
        past_end.close()


def test_preview_tonemaps_uint16_without_modifying_input(hdr_sequence):
    sequence, manifest = hdr_sequence
    cap = open_capture(manifest)
    cap.set(cv2.CAP_PROP_POS_FRAMES, 2)
    ok, bgr = cap.read()
    assert ok and bgr.dtype == np.uint16
    original = bgr.copy()
    actual = tone_map_hdr_preview(bgr, sequence.metadata)
    expected = tone_map_hdr_preview(bgr.astype(np.float32) / 65535, sequence.metadata)
    assert actual.dtype == np.uint8
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(original, bgr)
    assert actual.min() < 200 and actual.max() > actual.min()
    cap.release()


def test_analysis_proxy_uses_hdr_reader_and_never_changes_source(hdr_sequence):
    from dlss5tool.guidance_color import HDRAnalysisReader, analysis_rgba8
    sequence, manifest = hdr_sequence
    reader = HDRAnalysisReader(manifest, sequence.metadata, start_frame=1)
    raw = FFmpegHDRVideoReader(manifest, 128, 128, sequence.metadata, start_frame=1)
    try:
        rgba = raw.read()
        expected = analysis_rgba8(rgba, {'frame_format': 'rgba16f',
            'color_profile': sequence.color_profile, 'color_primaries': 'bt2020'})
        np.testing.assert_array_equal(reader.read(), expected[..., 2::-1])
    finally:
        raw.close()
        reader.close()


@pytest.mark.parametrize('hdr_enabled', [True, False])
def test_production_renderer_precision_and_explicit_sdr_conversion(hdr_sequence, tmp_path, hdr_enabled):
    from dlss5tool import frame_generation as fg, super_resolution
    sequence, manifest = hdr_sequence
    frames, metadata = [], []
    with mock.patch.object(super_resolution, 'query_gpu_memory', return_value=None):
        result = fg.export_video(manifest, None, multiplier=1, enhance=False,
            settings={'hdr_mode': hdr_enabled}, log_dir=tmp_path / 'render',
            frame_sink=lambda i, frame, ref, **kw: frames.append((frame.copy(), ref.copy())),
            metadata_sink=metadata.append)
    assert result['real_frames'] == 3 and result['status'] == 'complete'
    assert bool(metadata[-1]['hdr_metadata']) == hdr_enabled
    reader = FFmpegHDRVideoReader(manifest, 128, 128, sequence.metadata)
    try:
        for frame, reference in frames:
            rgba = reader.read()
            expected = rgba if hdr_enabled else cv2.cvtColor(
                tone_map_hdr_preview(rgba[..., 2::-1], sequence.metadata), cv2.COLOR_BGR2RGBA)
            np.testing.assert_array_equal(frame, expected)
            np.testing.assert_array_equal(reference, expected)
    finally:
        reader.close()


def test_async_preview_seek(hdr_sequence):
    from dlss5tool.preview_decoder import PreviewDecoder
    sequence, manifest = hdr_sequence
    decoder = PreviewDecoder(capacity=2)
    try:
        for index in (2, 0, 1):
            deadline = time.monotonic() + 3
            frame = None
            while frame is None and time.monotonic() < deadline:
                frame = decoder.get(manifest, index, sequence.metadata)
                time.sleep(.005)
            assert frame is not None
            np.testing.assert_array_equal(frame, tone_map_hdr_preview(sequence.read(index), sequence.metadata))
    finally:
        decoder.close()
        decoder._thread.join(timeout=2)


def test_changed_source_and_wrong_color_contract_rejected(hdr_sequence):
    sequence, manifest = hdr_sequence
    for changes in ({'profile': 'srgb', 'color_transfer': 'iec61966-2-1'},
                    {'color_range': 'tv'}, {'color_primaries': 'bt709'}):
        with pytest.raises(ValueError):
            FFmpegHDRVideoReader(manifest, 128, 128, {**sequence.metadata, **changes})
    with pytest.raises(ValueError):
        FFmpegHDRVideoReader(manifest, 130, 128, sequence.metadata)
    reader = FFmpegHDRVideoReader(manifest, 128, 128, sequence.metadata)
    try:
        sequence.files[0].write_bytes(b'changed input')
        with pytest.raises(ValueError):
            reader.read()
    finally:
        reader.close()


@pytest.mark.parametrize('profile', ['linear', 'scrgb', 'auto', None])
def test_unknown_profile_rejected(tmp_path, profile):
    with pytest.raises(ValueError):
        ImageSequence.scan(tmp_path / 'frame1.png', 24, color_profile=profile)


@pytest.mark.parametrize('shape,dtype', [((128, 128, 3), np.uint8),
    ((128, 128, 4), np.uint16), ((128, 128), np.uint16), ((130, 128, 3), np.uint16)])
def test_hdr_mixed_depth_alpha_gray_and_size_rejected(tmp_path, shape, dtype):
    write_png(tmp_path / 'frame1.png', np.zeros((128, 128, 3), np.uint16))
    write_png(tmp_path / 'frame2.png', np.zeros(shape, dtype))
    with pytest.raises(ValueError):
        ImageSequence.scan(tmp_path / 'frame1.png', 24, color_profile='hdr10_pq')


@pytest.mark.parametrize('change', [{'version': 3}, {'version': True}, {'color_profile': 'linear'},
                                  {'color_profile': None}])
def test_invalid_hdr_manifest_rejected(hdr_sequence, change):
    _, manifest = hdr_sequence
    path = Path(manifest)
    data = json.loads(path.read_text(encoding='utf-8'))
    data.update(change)
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError):
        ImageSequence.load(path)


def test_queue_preserves_hdr_contract(hdr_sequence):
    from dlss5tool import gui, export_queue
    sequence, manifest = hdr_sequence
    app = gui.App.__new__(gui.App)
    app.logln = mock.Mock()
    with mock.patch.object(gui, 'find_ffmpeg', side_effect=AssertionError('sequence metadata needs no ffmpeg')):
        metadata, color = app._probe_queue_video(manifest)
    assert color == sequence.metadata
    job = export_queue.ExportJob.create(manifest, 'result.mp4', {}, {'hdr_mode': True})
    job.color_info = color
    job.metadata = metadata
    restored = export_queue.ExportJob.from_dict(job.to_dict())
    assert restored.color_info == color and restored.export_settings['hdr_mode']
    with mock.patch.object(gui, 'probe_video_stream', side_effect=ValueError('invalid record')):
        with pytest.raises(ValueError, match='invalid record'):
            app._probe_queue_video(manifest)


def test_render_cancellation_closes_hdr_reader(hdr_sequence, tmp_path):
    from dlss5tool import frame_generation as fg, super_resolution
    sequence, manifest = hdr_sequence
    cancel = threading.Event()
    reader = FFmpegHDRVideoReader(manifest, 128, 128, sequence.metadata)
    def consume(*args, **kwargs):
        cancel.set()
    with mock.patch.object(super_resolution, 'query_gpu_memory', return_value=None), \
            mock.patch.object(fg, 'FFmpegHDRVideoReader', return_value=reader):
        with pytest.raises(fg.Cancelled):
            fg.export_video(manifest, None, multiplier=1, enhance=False, cancel=cancel,
                log_dir=tmp_path / 'cancel-render', frame_sink=consume)
    assert reader.read() is None
    report = json.loads((tmp_path / 'cancel-render/result.json').read_text(encoding='utf-8'))
    assert report['status'] == 'cancelled' and report['cleanup_errors'] == []


@pytest.mark.parametrize('theme,language', [('light', 'zh_CN'), ('dark', 'en_US')])
def test_import_dialog_recovers_from_sdr_error_and_imports_hdr(hdr_sequence, tmp_path, theme, language):
    import tkinter as tk
    from tkinter import ttk
    from dlss5tool import image_sequence_dialog as dialog, ui_theme, i18n
    sequence, _ = hdr_sequence
    previous_language = i18n.get_language()
    i18n.set_language(language)
    root = tk.Tk()
    root.withdraw()
    ui_theme.apply_ttk(root, ui_theme.THEMES[theme])
    failures, retried = [], []
    root.report_callback_exception = lambda *args: failures.append(args)
    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from walk(child)
    def controls():
        widgets = list(walk(root))
        def shown(widget):
            node = widget
            top = widget.winfo_toplevel()
            while node is not None and node is not top:
                parent = node.master
                if node.winfo_manager():
                    node = parent
                    continue
                if parent is not None and parent.winfo_class() == 'Canvas':
                    node = parent
                    continue
                return False
            return True
        colors = [widget for widget in widgets if isinstance(widget, ttk.Combobox) and shown(widget)]
        submits = []
        for widget in widgets:
            if not callable(getattr(widget, 'invoke', None)):
                continue
            try:
                if (widget.cget('text') == dialog.tr('sequence.confirm')
                        and str(widget.cget('state')) == 'normal'):
                    submits.append(widget)
            except tk.TclError:
                continue
        if not colors or not submits:
            return None
        return colors[0], submits[0]
    def state_path(name):
        if name == 'image-sequences':
            return tmp_path / 'dialog-records'
        return tmp_path / name
    def start():
        found = controls()
        if found is None:
            root.after(30, start)
            return
        color, submit = found
        assert color.get() == ''
        assert list(color.cget('values')) == [
            dialog.tr('sequence.color_' + profile)
            for profile in dialog.COLOR_PROFILES if profile != 'srgb']
        submit.invoke()
        assert str(submit.cget('state')) == 'normal'
        color.current(0 if sequence.color_profile == 'hdr10_pq' else 1)
        retried.append(True)
        submit.invoke()
    def timeout():
        failures.append('dialog timeout')
        for child in root.winfo_children():
            child.destroy()
    root.after(50, start)
    timer = root.after(5000, timeout)
    try:
        with mock.patch.object(dialog.paths, 'state_path', side_effect=state_path):
            manifests = dialog.ask_sequence(root, str(sequence.files[0]))
        assert not failures and retried
        assert ImageSequence.load(manifests[0]).color_profile == sequence.color_profile
    finally:
        root.after_cancel(timer)
        root.destroy()
        i18n.set_language(previous_language)
