"""Real Tk queue events, menu dispatch and bounded layout. No GPU or user files."""
import os
from types import SimpleNamespace
from unittest import mock
import tkinter as tk
from tkinter import ttk

import pytest

from dlss5tool import gui, ui_theme, export_queue, i18n
from dlss5tool.queue_list import QueueList, bounded_columns, elide


@pytest.fixture(scope='module')
def tk_root():
    # One Tcl interpreter, fresh queue widgets per test; avoid global Tk churn.
    root = tk.Tk()
    yield root
    root.destroy()


@pytest.fixture
def queue_ui(tk_root):
    root = tk_root
    root.geometry('440x300+0+0')
    app = gui.App.__new__(gui.App)
    app.root = root
    app._ui = ui_theme.THEMES['light']
    ui_theme.apply_ttk(root, app._ui)
    app._queue_running = app._exporting = app._diagnosing = app._switching_backend = False
    app._queue_jobs = [export_queue.ExportJob.create(f'long_filename_{i:03d}_abcdefghijklmnopqrst.png',
        f'output_{i}.png', {}, {}, media_kind='image') for i in range(40)]
    tree = app.queue_tree = ttk.Treeview(root, columns=('state', 'source', 'info', 'settings', 'output', 'progress'),
        displaycolumns=('state', 'source', 'progress'), show='headings', selectmode='extended')
    for name in ('state', 'source', 'progress'):
        tree.heading(name, text=name)
    tree.pack(fill='both', expand=True)
    app._save_queue_state = mock.Mock()
    app._update_queue_action_states = mock.Mock()
    app._on_queue_tree_select = mock.Mock()
    app.load_selected_queue_job = mock.Mock()
    app.apply_current_settings_to_queue = mock.Mock()
    app.add_queue_files = app.add_queue_folder = app.add_queue_image_sequence = mock.Mock()
    controller = app._queue_list = QueueList(app)
    for job in app._queue_jobs:
        app._update_queue_job_row(job)
    errors = []
    root.report_callback_exception = lambda *error: errors.append(error)
    root.update()
    tree.focus_force()
    root.update()
    try:
        yield app, controller, root
        assert not errors
    finally:
        for child in root.winfo_children():
            child.destroy()
        root.update_idletasks()


def event_at(app, index, **kwargs):
    row = app._queue_jobs[index].job_id
    app.queue_tree.see(row)
    app.root.update()
    x, y, w, h = app.queue_tree.bbox(row)
    return SimpleNamespace(x=x + 12, y=y + h // 2, x_root=x + 12, y_root=y + h // 2, state=0, **kwargs)


def selected_indices(app):
    selection = app.queue_tree.selection()
    return [i for i, j in enumerate(app._queue_jobs) if j.job_id in selection]


@pytest.mark.parametrize('width', [50, 240, 360, 640, 1000])
@pytest.mark.parametrize('preferred', [None, (9999, 9999), (-1, -1), (120, 90)])
def test_bounded_columns_never_overflow(width, preferred):
    result = bounded_columns(width, (70, 100, 70), preferred)
    assert sum(result) == width
    assert min(result) > 0
    if width >= 240:
        assert all(actual >= minimum for actual, minimum in zip(result, (70, 100, 70)))


def test_ellipsis_keeps_extension_and_never_exceeds_width():
    assert elide('a.png', 10, len, True) == 'a.png'
    assert elide('very_long_filename.png', 12, len, True) == 'very_lo….png'
    assert len(elide('whatever.png', 3, len, True)) <= 3
    assert elide('file', 0, len) == ''


def test_click_ctrl_shift_drag_and_blank_drag(queue_ui):
    app, c, root = queue_ui
    c.press(event_at(app, 0)); c.release()
    e = event_at(app, 2); e.state = 4
    c.press(e); c.release()
    assert selected_indices(app) == [0, 2]
    e = event_at(app, 4); e.state = 1
    c.press(e); c.release()
    assert selected_indices(app) == [2, 3, 4]
    c.press(event_at(app, 1))
    c.motion(event_at(app, 5)); c.release()
    assert selected_indices(app) == [1, 2, 3, 4, 5]
    # Empty space below a short list starts a range select, never a reorder.
    for job in app._queue_jobs[4:]:
        app.queue_tree.delete(job.job_id)
    app._queue_jobs = app._queue_jobs[:4]
    app.queue_tree.yview_moveto(0); root.update()
    e = SimpleNamespace(x=30, y=270, state=0)
    c.press(e)
    c.motion(event_at(app, 1)); c.release()
    assert selected_indices(app) == [1, 2, 3]


def test_autoscroll_extends_selection_and_stops_on_release(queue_ui):
    app, c, root = queue_ui
    c.press(event_at(app, 0))
    c.motion(SimpleNamespace(x=30, y=350))
    c._cancel_timer('scroll_timer')
    c._autoscroll()
    assert app.queue_tree.yview()[0] > 0
    assert len(selected_indices(app)) > 1
    c.release()
    assert c.scroll_timer is None and c.drag is None


def test_header_drag_is_bounded_and_does_not_select(queue_ui):
    app, c, root = queue_ui
    app._queue_jobs[0].source_path = os.path.abspath('very_long_filename_' * 20 + '.png')
    app.queue_tree.selection_set(app._queue_jobs[0].job_id)
    for column in ('#1', '#2'):
        c.resize = (column, 100, c.widths)
        c.motion(SimpleNamespace(x=10000, y=10))
        root.update()
        assert sum(c.widths) == app.queue_tree.winfo_width() - 4
        assert all(x >= y for x, y in zip(c.widths, c.minimums))
        c.release()
    assert selected_indices(app) == [0]
    c.reset_columns()
    assert c.preferred is None
    text = app.queue_tree.set(app._queue_jobs[0].job_id, 'source')
    assert text.endswith('.png') and '…' in text
    assert c.font.measure(text) <= c.widths[1] - 12


def test_real_mouse_bindings_select_and_resize_without_native_overflow(queue_ui):
    app, c, root = queue_ui
    tree = app.queue_tree
    a, b = event_at(app, 0), event_at(app, 3)
    tree.event_generate('<ButtonPress-1>', x=a.x, y=a.y)
    tree.event_generate('<B1-Motion>', x=b.x, y=b.y)
    tree.event_generate('<ButtonRelease-1>', x=b.x, y=b.y)
    root.update()
    assert selected_indices(app) == [0, 1, 2, 3]
    boundary = next((x for x in range(c.widths[0] - 4, c.widths[0] + 5)
                     if tree.identify_region(x, 10) == 'separator'), None)
    assert boundary is not None
    tree.event_generate('<ButtonPress-1>', x=boundary, y=10)
    tree.event_generate('<B1-Motion>', x=2000, y=10)
    root.update()
    tree.event_generate('<ButtonRelease-1>', x=2000, y=10)
    root.update()
    assert sum(int(tree.column(name, 'width')) for name in ('state', 'source', 'progress')) <= tree.winfo_width()
    assert selected_indices(app) == [0, 1, 2, 3]


def test_context_retains_multiselection_and_selects_new_row(queue_ui):
    app, c, root = queue_ui
    app.queue_tree.selection_set([j.job_id for j in app._queue_jobs[:3]])
    with mock.patch.object(c, '_popup'):
        c.context(event_at(app, 1))
        assert selected_indices(app) == [0, 1, 2]
        c.context(event_at(app, 4))
        assert selected_indices(app) == [4]
    assert c.menu_actions(heading=True)[0][0] == 'reset'


def test_menu_enablement_and_busy_dispatch(queue_ui):
    app, c, root = queue_ui
    app.queue_tree.selection_set([j.job_id for j in app._queue_jobs[:3]])
    app._queue_jobs[0].state = 'completed'
    app._queue_jobs[1].state = 'failed'
    actions = {key: enabled for key, _, _, enabled in c.menu_actions()}
    assert not actions['preview'] and not actions['location'] and not actions['apply']
    assert actions['retry'] and actions['remove']
    for flag in ('_queue_running', '_exporting', '_diagnosing', '_switching_backend'):
        setattr(app, flag, True)
        actions = {key: enabled for key, _, _, enabled in c.menu_actions()}
        assert not actions['remove'] and not actions['retry']
        assert actions['copy']
        c.command('remove'); c.command('apply'); c.command('retry')
        assert len(app._queue_jobs) == 40
        assert app._queue_jobs[1].state == 'failed'
        setattr(app, flag, False)
    c.command('retry')
    assert app._queue_jobs[1].state == 'pending'
    assert app._queue_jobs[0].state == 'completed'


def test_keyboard_remove_undo_and_input_focus_isolation(queue_ui, tmp_path):
    app, c, root = queue_ui
    source = tmp_path / 'source.png'; source.write_bytes(b'original')
    output = tmp_path / 'output.png'; output.write_bytes(b'result')
    app._queue_jobs[0].source_path = str(source)
    app._queue_jobs[0].output_path = str(output)
    original_ids = [j.job_id for j in app._queue_jobs]
    app.queue_tree.event_generate('<Control-a>'); root.update()
    assert len(app.queue_tree.selection()) == 40
    app.queue_tree.event_generate('<Delete>'); root.update()
    assert not app._queue_jobs
    assert source.read_bytes() == b'original' and output.read_bytes() == b'result'
    app.queue_tree.event_generate('<Control-z>'); root.update()
    assert [j.job_id for j in app._queue_jobs] == original_ids
    app.queue_tree.event_generate('<Escape>'); root.update()
    assert not app.queue_tree.selection()
    app.queue_tree.selection_set(original_ids[0])
    entry = ttk.Entry(root); entry.pack(before=app.queue_tree); entry.insert(0, 'abc')
    root.update()
    entry.focus_force(); root.update()
    assert root.focus_get() is entry
    entry.event_generate('<Control-a>'); entry.event_generate('<Delete>'); root.update()
    assert len(app._queue_jobs) == 40


def test_shift_arrows_enter_and_keyboard_context(queue_ui):
    app, c, root = queue_ui
    c.press(event_at(app, 0)); c.release()
    app.queue_tree.event_generate('<Shift-Down>'); root.update()
    assert selected_indices(app) == [0, 1]
    app.queue_tree.event_generate('<Return>'); root.update()
    app.load_selected_queue_job.assert_not_called()
    c.press(event_at(app, 1)); c.release()
    app.queue_tree.event_generate('<Return>'); root.update()
    app.load_selected_queue_job.assert_called_once()
    with mock.patch.object(c, '_popup') as popup:
        app.queue_tree.event_generate('<Shift-F10>'); root.update()
    popup.assert_called_once()


def test_copy_multiple_paths_and_undo_does_not_duplicate_readded_source(queue_ui):
    app, c, root = queue_ui
    jobs = list(app._queue_jobs)
    app.queue_tree.selection_set([j.job_id for j in jobs[1:3]])
    c.command('copy')
    assert root.clipboard_get() == '\n'.join(j.source_path for j in jobs[1:3])
    c.command('remove')
    replacement = export_queue.ExportJob.create(jobs[1].source_path, 'new.png', {}, {})
    app._queue_jobs.append(replacement)
    c.command('undo')
    assert len(app._queue_jobs) == 40
    assert len({j.source_path for j in app._queue_jobs}) == 40
    assert jobs[2] in app._queue_jobs


def test_actual_menu_and_tooltip_cleanup(queue_ui):
    app, c, root = queue_ui
    app.queue_tree.selection_set([j.job_id for j in app._queue_jobs[:2]])
    with mock.patch.object(tk.Menu, 'tk_popup'):
        c.context(event_at(app, 1))
    assert c.menu.entrycget(0, 'state') == 'disabled'  # multiple preview targets
    row = app._queue_jobs[0].job_id
    c.hover_row = row
    c._show_tip(row, 10, 20)
    text = c.tip.winfo_children()[0].cget('text')
    assert app._queue_jobs[0].source_path in text
    c.blur()
    assert c.tip is None and c.hover_timer is None
    with mock.patch.object(c, '_show_tip') as show:
        c.hover(event_at(app, 1))
        c.hide_tip()
        assert c.hover_timer is None
        show.assert_not_called()


@pytest.mark.parametrize('theme,language,width', [('light', 'zh_CN', 320), ('dark', 'zh_CN', 360),
    ('light', 'en_US', 420), ('dark', 'en_US', 640)])
def test_fonts_and_column_bounds_across_themes(queue_ui, theme, language, width):
    app, c, root = queue_ui
    previous = i18n.get_language()
    try:
        i18n.set_language(language)
        app._ui = ui_theme.THEMES[theme]
        ui_theme.apply_ttk(root, app._ui)
        root.geometry(f'{width}x300'); root.update()
        c.layout()
        assert sum(c.widths) == app.queue_tree.winfo_width() - 4
        assert all(x >= y for x, y in zip(c.widths, c.minimums))
    finally:
        i18n.set_language(previous)
