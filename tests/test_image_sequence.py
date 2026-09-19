"""Sequence ingestion and existing rendering/queue contracts; no GPU required."""
from fractions import Fraction
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np
import pytest

from dlss5tool import image_sequence as seq


def picture(path, value=0, shape=(128, 128, 3), dtype=np.uint8):
    pixels = np.full(shape, value, dtype)
    ok, encoded = cv2.imencode(path.suffix, pixels)
    assert ok
    encoded.tofile(path)
    return path


@pytest.fixture
def sequence(tmp_path):
    files = [picture(tmp_path / f'画面_{i}.png', i) for i in range(8, 12)]
    value = seq.ImageSequence.scan(files[1], '24000/1001')
    return value, value.save(tmp_path / 'records')


def test_order_metadata_reader_and_descriptor_roundtrip(sequence):
    value, manifest = sequence
    assert [p.stem for p in value.files] == [f'画面_{i}' for i in range(8, 12)]
    assert seq.ImageSequence.load(manifest) == value
    assert value.save(Path(manifest).parent) == manifest
    cap = seq.open_capture(manifest)
    assert cap.get(cv2.CAP_PROP_FRAME_COUNT) == 4
    assert cap.get(cv2.CAP_PROP_FPS) == float(Fraction(24000, 1001))
    assert cap.read()[1][0, 0, 0] == 8
    assert cap.set(cv2.CAP_PROP_POS_FRAMES, 3)
    assert cap.read()[1][0, 0, 0] == 11
    assert cap.read() == (False, None)
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    assert cap.read()[0]
    cap.release()
    assert not cap.isOpened()
    assert cap.read() == (False, None)
    assert seq.audio_source(manifest) is None
    assert seq.output_source(manifest) == str(value.files[0])
    assert seq.source_bytes(manifest) == sum(p.stat().st_size for p in value.files)


@pytest.mark.parametrize('rate', ['0', '-1', 'nan', 'inf', '1/0', '241', '', None, '0.5'])
def test_invalid_rates(rate):
    with pytest.raises(ValueError):
        seq.parse_rate(rate)


@pytest.mark.parametrize('names', [('a1.png',), ('a1.png', 'a3.png'), ('a1.png', 'a01.png')])
def test_missing_duplicate_and_single_frames(tmp_path, names):
    for name in names:
        picture(tmp_path / name)
    with pytest.raises(ValueError):
        seq.ImageSequence.scan(tmp_path / names[0], 24)


def test_select_one_family_and_numeric_suffix(tmp_path):
    for name in ('shot_01_left.jpg', 'shot_02_left.jpg', 'shot_01_right.jpg', 'shot_02_left.png'):
        picture(tmp_path / name)
    assert [p.name for p in seq.discover(tmp_path / 'shot_02_left.jpg')] == [
        'shot_01_left.jpg', 'shot_02_left.jpg']


@pytest.mark.parametrize('shape,dtype', [((130, 128, 3), np.uint8),
    ((128, 128, 4), np.uint8), ((128, 128), np.uint8), ((128, 128, 3), np.uint16)])
def test_reject_size_alpha_gray_and_high_depth(tmp_path, shape, dtype):
    picture(tmp_path / 'a1.png')
    picture(tmp_path / 'a2.png', shape=shape, dtype=dtype)
    with pytest.raises(ValueError):
        seq.ImageSequence.scan(tmp_path / 'a1.png', 24)


def test_corrupt_and_changed_source(sequence):
    value, manifest = sequence
    value.files[1].write_bytes(b'invalid image')
    with pytest.raises(ValueError):
        seq.ImageSequence.scan(value.files[0], 24)
    with pytest.raises(ValueError):
        seq.ImageSequence.load(manifest).validate()
    cap = seq.open_capture(manifest)
    cap.set(cv2.CAP_PROP_POS_FRAMES, 1)
    with pytest.raises(ValueError):
        cap.read()


def test_cancellation_and_fps_identity(sequence):
    value, manifest = sequence
    def cancel():
        raise InterruptedError()
    with pytest.raises(InterruptedError):
        seq.ImageSequence.scan(value.files[0], 24, cancel)
    other = seq.ImageSequence.scan(value.files[0], 30).save(Path(manifest).parent)
    assert other != manifest
    from dlss5tool.render_cache import render_identity
    assert render_identity(other, {}) != render_identity(manifest, {})


def test_queue_persistence_and_import_classification(sequence):
    from dlss5tool import export_queue, gui
    value, manifest = sequence
    job = export_queue.ExportJob.create(manifest, 'result.mp4', {}, {'frame_generation_multiplier': 2})
    restored = export_queue.ExportJob.from_dict(job.to_dict())
    assert restored.source_path == manifest
    assert restored.media_kind == 'video'  # temporal source; never a still-image job
    assert gui._is_video_path(manifest) and not gui._is_image_path(manifest)
    assert gui.App._video_info(manifest) == (4, float(value.rate), 128, 128)
    assert Path(gui.App._unique_output_path(manifest)).parent == value.files[0].parent


def test_inspect_and_render_real_sequence_without_video_decoder(sequence, tmp_path):
    from dlss5tool import frame_generation as fg, super_resolution
    from dlss5tool.video_export import probe_video_stream
    value, manifest = sequence
    with mock.patch.object(fg, 'find_ffmpeg', side_effect=AssertionError('not a video')):
        info = fg.inspect_source(manifest, threading.Event())
    assert info[1:] == (value.rate, 128, 128, 4)
    assert probe_video_stream(None, manifest)['is_hdr'] is False
    frames, metadata = [], []
    with mock.patch.object(super_resolution, 'query_gpu_memory', return_value=None):
        result = fg.export_video(manifest, None, multiplier=1, enhance=False,
            log_dir=tmp_path / 'render-log', frame_sink=lambda i, frame, ref, **kw: frames.append(frame.copy()),
            metadata_sink=metadata.append)
    assert len(frames) == result['real_frames'] == 4
    assert [int(frame[0, 0, 0]) for frame in frames] == [8, 9, 10, 11]
    assert result['output_rate'] == '24000/1001'
    assert metadata[-1]['source_frames'] == 4


def test_preview_decoder_seek(sequence):
    from dlss5tool.preview_decoder import PreviewDecoder
    value, manifest = sequence
    decoder = PreviewDecoder(capacity=2)
    try:
        for index in (3, 0, 2):
            deadline = time.monotonic() + 3
            frame = None
            while frame is None and time.monotonic() < deadline:
                frame = decoder.get(manifest, index, value.metadata)
                time.sleep(.005)
            assert frame is not None
            assert int(frame[0, 0, 0]) == index + 8
    finally:
        decoder.close()
        decoder._thread.join(timeout=2)


def test_cached_export_no_audio_and_reject_modified_inputs(sequence, tmp_path):
    from dlss5tool import render_cache as rc, gpu_export_runtime
    value, manifest = sequence
    metadata = dict(width=128, height=128, source_frames=4, hdr_metadata=None,
        output_rate=str(value.rate), source_rate=str(value.rate))
    session = mock.Mock(source=manifest, multiplier=1, settings={}, result={})
    session.wait_metadata.return_value = metadata
    session.metadata = metadata
    session.snapshot.return_value = {'computed': 4}
    session.condition = threading.RLock()
    session.cache = {}
    rgba = np.zeros((128, 128, 4), np.uint8)
    session.wait.return_value = (rgba, rgba)
    writer = mock.Mock()
    destination = tmp_path / 'out.mp4'
    def factory(temporary, *args, **kwargs):
        assert kwargs['audio_source'] is None
        Path(temporary).write_bytes(b'test bitstream')
        return writer
    with mock.patch.object(gpu_export_runtime, 'create_video_writer', side_effect=factory):
        result = rc.encode_cached(session, destination, {}, threading.Event(), lambda *a: None)
    assert writer.write.call_count == 4
    assert destination.exists()
    value.files[0].write_bytes(b'changed')
    with pytest.raises(ValueError):
        rc.encode_cached(session, tmp_path / 'other.mp4', {}, threading.Event(), lambda *a: None)


@pytest.mark.parametrize('scale,multiplier', [(1, 1), (2, 1), (1, 2), (2, 2)])
def test_gui_export_routes_sequences_through_shared_renderer(sequence, tmp_path, scale, multiplier):
    from dlss5tool import gui
    value, manifest = sequence
    app = gui.App.__new__(gui.App)
    app._collect_settings = lambda: {'output_view': 0, 'output_mix': 1}
    config = dict(mode='parallel', output_container='mp4', output_resolution='source',
        custom_output_width=128, custom_output_height=128, super_resolution_scale=scale,
        frame_generation_multiplier=multiplier, hdr_mode=False, rate_control='quality',
        quality_profile='high', nvenc_preset='p5', video_bitrate_mbps=20)
    app._collect_export_settings = lambda: config.copy()
    app._ensure_shared_cache_pool = lambda: SimpleNamespace(name='test')
    app._confirm_super_resolution_export = lambda *a, **kw: True
    for name in ('_wait_play_dlss', '_close_super_resolution', '_close_live', 'logln',
                 '_end_export_ui', '_remember_export'):
        setattr(app, name, mock.Mock())
    app._begin_export_ui = lambda _: setattr(app, '_export_t0', time.perf_counter())
    app._export_frame_generated_video = mock.Mock(return_value={
        'real_frames': 4, 'generated_frames': 3 * (multiplier - 1),
        'output_rate': str(value.rate * multiplier)})
    result = app._export_video_source(manifest, {}, config, value.metadata,
        out_path=str(tmp_path / 'output.mp4'), notify=False)
    assert result['success']
    passed = app._export_frame_generated_video.call_args.args[3]
    assert passed['mode'] == 'single'
    assert passed['frame_generation_multiplier'] == multiplier
    assert passed['super_resolution_scale'] == scale


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_import_dialog_validates_and_saves_on_worker(sequence, tmp_path, theme):
    import tkinter as tk
    from tkinter import ttk
    from dlss5tool import image_sequence_dialog as dialog, ui_theme
    value, _ = sequence
    root = tk.Tk()
    root.withdraw()
    ui_theme.apply_ttk(root, ui_theme.THEMES[theme])
    failures = []
    root.report_callback_exception = lambda *args: failures.append(args)
    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from walk(child)
    def submit():
        widgets = list(walk(root))
        entry = next(w for w in widgets if isinstance(w, ttk.Entry))
        entry.delete(0, 'end')
        entry.insert(0, '30')
        next(w for w in widgets if isinstance(w, ttk.Button)
             and w.cget('text') == dialog.tr('sequence.confirm')).invoke()
    def timeout():
        failures.append('dialog timeout')
        for child in root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
    root.after(50, submit)
    timer = root.after(5000, timeout)
    try:
        with mock.patch.object(dialog.paths, 'state_path', return_value=tmp_path / 'dialog-records'):
            manifest = dialog.ask_sequence(root, str(value.files[1]))
        assert not failures
        assert seq.ImageSequence.load(manifest).rate == 30
    finally:
        root.after_cancel(timer)
        root.destroy()


def test_cancel_import_keeps_records_unwritten(sequence, tmp_path):
    import tkinter as tk
    from tkinter import ttk
    from dlss5tool import image_sequence_dialog as dialog
    value, _ = sequence
    root = tk.Tk()
    root.withdraw()
    records = tmp_path / 'cancelled-records'
    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from walk(child)
    def cancel():
        for widget in walk(root):
            if isinstance(widget, ttk.Button) and widget.cget('text') == dialog.tr('sequence.cancel'):
                widget.invoke()
                return
    root.after(50, cancel)
    try:
        with mock.patch.object(dialog.paths, 'state_path', return_value=records):
            assert dialog.ask_sequence(root, str(value.files[0])) is None
        assert not records.exists()
    finally:
        root.destroy()


@pytest.mark.parametrize('selected,manifest', [('frame_0001.png', 'clip.dlssseq'),
    ('frame_0001.png', None), ('', None)])
def test_queue_sequence_entry_adds_job_without_replacing_preview(selected, manifest):
    from dlss5tool import gui, image_sequence_dialog
    app = gui.App.__new__(gui.App)
    app.root = mock.Mock()
    app.video = 'current-preview.mp4'
    app._exporting = app._queue_running = app._diagnosing = app._switching_backend = False
    app._freeze_preview_cache = mock.Mock()
    app._schedule_preview_cache_resume = mock.Mock()
    app._add_paths_to_queue = mock.Mock()
    app._load_media = mock.Mock()
    with mock.patch.object(gui.filedialog, 'askopenfilename', return_value=selected), \
            mock.patch.object(image_sequence_dialog, 'ask_sequence', return_value=manifest) as ask:
        app.add_queue_image_sequence()
    if selected and manifest:
        app._add_paths_to_queue.assert_called_once_with([manifest])
    else:
        app._add_paths_to_queue.assert_not_called()
    if not selected:
        ask.assert_not_called()
    app._load_media.assert_not_called()
    assert app.video == 'current-preview.mp4'
    app._schedule_preview_cache_resume.assert_called_once()


@pytest.mark.parametrize('busy', ['_exporting', '_queue_running', '_diagnosing', '_switching_backend'])
def test_queue_sequence_entry_ignores_busy_state(busy):
    from dlss5tool import gui
    app = gui.App.__new__(gui.App)
    app._exporting = app._queue_running = app._diagnosing = app._switching_backend = False
    setattr(app, busy, True)
    with mock.patch.object(gui.filedialog, 'askopenfilename') as choose:
        app.add_queue_image_sequence()
    choose.assert_not_called()


def test_real_sequence_added_as_one_job_and_loaded_on_request(sequence, tmp_path):
    from dlss5tool import gui, export_queue
    value, manifest = sequence
    app = gui.App.__new__(gui.App)
    app._queue_running = app._exporting = False
    app.video = 'current-preview.mp4'
    app._queue_jobs = []
    app._collect_settings = lambda: {'style': 0}
    app._collect_export_settings = lambda: {'mode': 'parallel', 'super_resolution_scale': 1,
        'frame_generation_multiplier': 1, 'output_container': 'mp4'}
    app.queue_output_dir_var = mock.Mock()
    app.queue_output_dir_var.get.return_value = ''
    app.root = mock.Mock()
    app.queue_tree = mock.Mock()
    app.workspace_tabs = mock.Mock()
    app.queue_tab = object()
    app._preview_page = object()
    app._save_queue_state = mock.Mock()
    app._refresh_queue_tree = mock.Mock()
    app.logln = mock.Mock()
    assert app._add_paths_to_queue([manifest]) == 1
    assert len(app._queue_jobs) == 1
    job = app._queue_jobs[0]
    assert job.state == 'pending' and job.media_kind == 'video'
    assert job.metadata['frames'] == 4
    assert job.metadata['fps'] == float(value.rate)
    assert job.export_settings['mode'] == 'single'
    assert Path(job.output_path).parent == value.files[0].parent
    app.workspace_tabs.select.assert_called_with(app.queue_tab)
    app.queue_tree.selection_set.assert_called_with(job.job_id)
    assert app.video == 'current-preview.mp4'
    saved = export_queue.ExportJob.from_dict(job.to_dict())
    assert seq.ImageSequence.load(saved.source_path).rate == value.rate
    assert app._add_paths_to_queue([manifest]) == 0
    app._selected_queue_jobs = lambda: [job]
    app._load_media = mock.Mock(return_value=True)
    app.load_selected_queue_job()
    app._load_media.assert_called_once_with(manifest)
    app.workspace_tabs.select.assert_called_with(app._preview_page)


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('language,width', [('zh_CN', 360), ('en_US', 420)])
def test_queue_import_buttons_fit_the_sidebar(theme, language, width):
    import tkinter as tk
    from tkinter import ttk
    from dlss5tool import gui, ui_theme, i18n
    previous_language = i18n.get_language()
    root = tk.Tk()
    root.withdraw()
    try:
        i18n.set_language(language)
        app = gui.App.__new__(gui.App)
        app.root = root
        app._ui = ui_theme.THEMES[theme]
        app._ui_language = language
        app._saved_settings = {}
        app._theme_widgets = []
        ui_theme.apply_ttk(root, app._ui)
        parent = ttk.Frame(root)
        parent.place(x=0, y=0, width=width, height=700)
        app._build_queue_tab(parent)
        root.update_idletasks()
        imports = app.queue_add_sequence_btn.master
        assert imports.winfo_width() == width - 24
        for button in (app.queue_add_files_btn, app.queue_add_folder_btn, app.queue_add_sequence_btn):
            assert button.winfo_width() > 100
            assert button.winfo_x() + button.winfo_width() <= imports.winfo_width()
            assert not button._icon_only
            assert button in app._theme_widgets
        assert app.queue_add_sequence_btn.winfo_y() >= app.queue_add_files_btn.winfo_height()
    finally:
        i18n.set_language(previous_language)
        root.destroy()
