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


def test_display_name_and_output_duration():
    group = seq.SequenceGroup((
        Path('镜头0050_dlss.png'), Path('镜头0120_dlss.png'),
    ), '', 'sdr', 3840, 2160, False)
    assert group.display_name() == '镜头0050–0120_dlss'
    assert seq.format_duration(71, 24) == '2.96 秒'
    assert seq.format_duration(4, '24') == '0.17 秒'
    assert seq.format_duration(1440, 24) == '1:00.00'
    assert '0.08 秒' in group.detail('24')
    blocked = seq.SequenceGroup((Path('a1.png'), Path('a3.png')), '缺号', '', 0, 0, False)
    assert blocked.detail('24') == '缺号'


def test_scan_files_keeps_a_selected_range(tmp_path):
    files = [picture(tmp_path / f'a{i}.png', i) for i in range(1, 6)]
    value = seq.ImageSequence.scan_files(files[1:3], 24)
    assert [path.name for path in value.files] == ['a2.png', 'a3.png']
    with pytest.raises(ValueError):
        seq.ImageSequence.scan_files([files[0], files[2]], 24)


def test_propose_expands_one_frame_and_lists_other_runs(tmp_path):
    for index in range(1, 5):
        picture(tmp_path / f'a{index}.png', index)
    for index in range(1, 3):
        picture(tmp_path / f'b{index}.png', index)
    picture(tmp_path / 'note.png')
    single = seq.propose(tmp_path / 'a2.png')
    assert [path.name for path in single[0].files] == [f'a{index}.png' for index in range(1, 5)]
    assert single[0].importable and single[0].kind == 'sdr' and not single[0].recommended
    assert [path.name for path in single[1].files] == ['b1.png', 'b2.png']
    assert single[1].recommended and single[1].importable
    ranged = seq.propose([tmp_path / 'a2.png', tmp_path / 'a3.png'])
    assert [path.name for path in ranged[0].files] == ['a2.png', 'a3.png']
    assert ranged[0].importable and not ranged[0].recommended
    assert [path.name for path in ranged[1].files] == ['b1.png', 'b2.png']
    seeds = seq.propose([tmp_path / 'a1.png', tmp_path / 'b2.png'])
    assert [not group.recommended and group.importable for group in seeds] == [True, True]
    assert [path.name for path in seeds[0].files] == [f'a{index}.png' for index in range(1, 5)]
    assert [path.name for path in seeds[1].files] == ['b1.png', 'b2.png']


def test_propose_reports_gaps_without_blocking_other_runs(tmp_path):
    for name in ('a1.png', 'a2.png', 'a4.png', 'b1.png', 'b2.png'):
        picture(tmp_path / name)
    with pytest.raises(ValueError):
        seq.discover(tmp_path / 'a1.png')
    ranged = seq.propose([tmp_path / 'a1.png', tmp_path / 'a2.png'])
    assert ranged[0].importable
    assert [path.name for path in ranged[0].files] == ['a1.png', 'a2.png']
    seeded = seq.propose(tmp_path / 'a1.png')
    assert not seeded[0].importable and seeded[0].reason
    assert any(group.recommended and [path.name for path in group.files] == ['b1.png', 'b2.png']
               for group in seeded)
    gap = seq.propose([tmp_path / 'a1.png', tmp_path / 'a4.png'])
    assert not gap[0].importable and 'a4.png' in gap[0].reason
    assert any(group.recommended for group in gap)


def test_propose_classifies_hdr_and_rejects_incompatible_frames(tmp_path):
    picture(tmp_path / 'h1.png', dtype=np.uint16)
    picture(tmp_path / 'h2.png', dtype=np.uint16)
    hdr = seq.propose(tmp_path / 'h1.png')
    assert hdr[0].kind == 'hdr' and hdr[0].importable and hdr[0].width == 128
    picture(tmp_path / 'bad1.png')
    picture(tmp_path / 'bad2.png', shape=(8, 8, 4))
    blocked = seq.propose([tmp_path / 'bad1.png', tmp_path / 'bad2.png'])[0]
    assert not blocked.importable and 'bad2.png' in blocked.reason
    for index, shape in enumerate(((8, 8, 3), (9, 8, 3)), start=1):
        picture(tmp_path / f's{index}.png', shape=shape)
    mismatch = seq.propose([tmp_path / 's1.png', tmp_path / 's2.png'])[0]
    assert not mismatch.importable and 's2.png' in mismatch.reason


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


def sequence_state(tmp_path):
    def state_path(name):
        if name == 'image-sequences':
            return tmp_path / 'dialog-records'
        return tmp_path / name
    return state_path


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def action_buttons(widgets, text, enabled=False):
    found = []
    for item in widgets:
        if not callable(getattr(item, 'invoke', None)):
            continue
        try:
            if item.cget('text') != text:
                continue
            if enabled and str(item.cget('state')) != 'normal':
                continue
        except tk.TclError:
            continue
        found.append(item)
    return found


def widget_shown(widget):
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
    def submit():
        widgets = list(walk(root))
        confirms = action_buttons(widgets, dialog.tr('sequence.confirm'), enabled=True)
        entries = [item for item in widgets if isinstance(item, ttk.Entry)]
        if not confirms or not entries:
            root.after(30, submit)
            return
        assert not any(isinstance(item, ttk.Combobox) and widget_shown(item) for item in widgets)
        labels = [str(item.cget('text')) for item in widgets if isinstance(item, ttk.Label)]
        assert dialog.tr('sequence.section_import') not in labels
        assert any('0.17 秒' in text for text in labels)
        assert all('输出时长' not in text for text in labels)
        assert any(isinstance(item, ttk.Label) and ('帧' in str(item.cget('text')) or 'frames' in str(item.cget('text')))
                   for item in widgets)
        canvases = [item for item in widgets if isinstance(item, tk.Canvas)]
        assert canvases and int(float(canvases[0].cget('height'))) > 20
        entry = entries[0]
        assert entry.get() == '24'
        entry.delete(0, 'end')
        entry.insert(0, '30')
        confirms[0].invoke()
    def timeout():
        failures.append('dialog timeout')
        for child in root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
    root.after(50, submit)
    timer = root.after(5000, timeout)
    try:
        with mock.patch.object(dialog.paths, 'state_path', side_effect=sequence_state(tmp_path)):
            manifests = dialog.ask_sequence(root, str(value.files[1]))
        assert not failures
        assert seq.ImageSequence.load(manifests[0]).rate == 30
        assert len(seq.ImageSequence.load(manifests[0]).files) == 4
        assert (tmp_path / 'image-sequence-rate.txt').read_text(encoding='utf-8') == '30'
    finally:
        root.after_cancel(timer)
        root.destroy()


def test_dialog_imports_a_checked_recommendation(sequence, tmp_path):
    import tkinter as tk
    from tkinter import ttk
    from dlss5tool import image_sequence_dialog as dialog
    value, _ = sequence
    folder = value.files[0].parent
    picture(folder / 'b1.png', 1)
    picture(folder / 'b2.png', 2)
    root = tk.Tk()
    root.withdraw()
    failures = []
    root.report_callback_exception = lambda *args: failures.append(args)
    def submit():
        widgets = list(walk(root))
        from dlss5tool.ui_widgets import CheckToggle
        checks = [item for item in widgets if isinstance(item, CheckToggle)]
        confirms = action_buttons(widgets, dialog.tr('sequence.confirm'), enabled=True)
        if len(checks) < 2 or not confirms:
            root.after(30, submit)
            return
        assert checks[0].instate(['selected'])
        assert not checks[1].instate(['selected'])
        checks[1].invoke()
        confirms[0].invoke()
    def timeout():
        failures.append('dialog timeout')
        for child in root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
    root.after(50, submit)
    timer = root.after(5000, timeout)
    try:
        with mock.patch.object(dialog.paths, 'state_path', side_effect=sequence_state(tmp_path)):
            manifests = dialog.ask_sequence(root, str(value.files[0]))
        assert not failures
        loaded = [seq.ImageSequence.load(path) for path in manifests]
        assert [path.stem for path in loaded[0].files] == [f'画面_{index}' for index in range(8, 12)]
        assert [path.name for path in loaded[1].files] == ['b1.png', 'b2.png']
        assert {item.color_profile for item in loaded} == {'srgb'}
    finally:
        root.after_cancel(timer)
        root.destroy()


def test_dialog_remembers_rate_and_keeps_sdr_when_hdr_is_chosen(tmp_path):
    import tkinter as tk
    from tkinter import ttk
    from dlss5tool import image_sequence_dialog as dialog
    for index in range(1, 3):
        picture(tmp_path / f'a{index}.png', index)
        picture(tmp_path / f'h{index}.png', index, dtype=np.uint16)
    (tmp_path / 'image-sequence-rate.txt').write_text('25', encoding='utf-8')
    root = tk.Tk()
    root.withdraw()
    failures = []
    root.report_callback_exception = lambda *args: failures.append(args)
    def submit(step=[0]):
        widgets = list(walk(root))
        confirms = action_buttons(widgets, dialog.tr('sequence.confirm'), enabled=True)
        entries = [item for item in widgets if isinstance(item, ttk.Entry)]
        colors = [item for item in widgets if isinstance(item, ttk.Combobox) and widget_shown(item)]
        if not confirms or not entries or not colors:
            root.after(30, submit)
            return
        if step[0] == 0:
            assert entries[0].get() == '25'
            step[0] = 1
            confirms[0].invoke()
            assert str(confirms[0].cget('state')) == 'normal'
            root.after(30, submit)
            return
        colors[0].current(0)
        confirms[0].invoke()
    def timeout():
        failures.append('dialog timeout')
        for child in root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
    root.after(50, submit)
    timer = root.after(5000, timeout)
    try:
        with mock.patch.object(dialog.paths, 'state_path', side_effect=sequence_state(tmp_path)):
            manifests = dialog.ask_sequence(root, [tmp_path / 'a1.png', tmp_path / 'h1.png'])
        assert not failures
        loaded = {seq.ImageSequence.load(path).color_profile: seq.ImageSequence.load(path) for path in manifests}
        assert set(loaded) == {'srgb', 'hdr10_pq'}
        assert [path.name for path in loaded['srgb'].files] == ['a1.png', 'a2.png']
        assert [path.name for path in loaded['hdr10_pq'].files] == ['h1.png', 'h2.png']
        assert (tmp_path / 'image-sequence-rate.txt').read_text(encoding='utf-8') == '25'
    finally:
        root.after_cancel(timer)
        root.destroy()


def test_cancel_import_keeps_records_unwritten(sequence, tmp_path):
    import tkinter as tk
    from dlss5tool import image_sequence_dialog as dialog
    value, _ = sequence
    root = tk.Tk()
    root.withdraw()
    def cancel():
        buttons = action_buttons(list(walk(root)), dialog.tr('sequence.cancel'))
        if buttons:
            buttons[0].invoke()
    root.after(50, cancel)
    try:
        with mock.patch.object(dialog.paths, 'state_path', side_effect=sequence_state(tmp_path)):
            assert dialog.ask_sequence(root, str(value.files[0])) == []
        assert not (tmp_path / 'dialog-records').exists()
        assert not (tmp_path / 'image-sequence-rate.txt').exists()
    finally:
        root.destroy()


@pytest.mark.parametrize('selected,manifests', [
    (('frame_0001.png',), ['clip.dlssseq']),
    (('frame_0001.png',), []),
    ((), []),
])
def test_queue_sequence_entry_adds_job_without_replacing_preview(selected, manifests):
    from dlss5tool import gui, image_sequence_dialog
    app = gui.App.__new__(gui.App)
    app.root = mock.Mock()
    app.video = 'current-preview.mp4'
    app._exporting = app._queue_running = app._diagnosing = app._switching_backend = False
    app._freeze_preview_cache = mock.Mock()
    app._schedule_preview_cache_resume = mock.Mock()
    app._add_paths_to_queue = mock.Mock()
    app._load_media = mock.Mock()
    with mock.patch.object(gui.filedialog, 'askopenfilenames', return_value=selected), \
            mock.patch.object(image_sequence_dialog, 'ask_sequence', return_value=manifests) as ask:
        app.add_queue_image_sequence()
    if selected and manifests:
        app._add_paths_to_queue.assert_called_once_with(list(manifests))
    else:
        app._add_paths_to_queue.assert_not_called()
    if selected:
        ask.assert_called_once_with(app.root, selected)
    else:
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
    with mock.patch.object(gui.filedialog, 'askopenfilenames') as choose:
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
