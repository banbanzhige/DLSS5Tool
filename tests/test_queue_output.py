"""Queue output folders, saved job snapshots and actual export dispatch. No GPU."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import tkinter as tk

import pytest

from dlss5tool import export_queue, gui, i18n, ui_theme


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@pytest.fixture
def queue_app(tmp_path):
    app = gui.App.__new__(gui.App)
    app.root = object()
    app._queue_running = app._exporting = app._diagnosing = app._switching_backend = False
    app._queue_pause_requested = False
    app._queue_active_job_id = app._queue_last_summary = None
    app._queue_jobs = [
        export_queue.ExportJob.create(
            str(tmp_path / "inputs" / name), str(tmp_path / "old" / output),
            {"style": index, "nested": {"intensity": 0.5}},
            {"mode": "single", "output_container": "mp4"},
            metadata={"frames": 24, "width": 640, "height": 360, "fps": 24},
        )
        for index, (name, output) in enumerate([
            ("one.png", "custom_one.png"), ("two.mp4", "custom_two.mp4"),
            ("three.png", "completed.png"),
        ])
    ]
    app._queue_jobs[1].state = "failed"
    app._queue_jobs[1].error = "previous failure"
    app._queue_jobs[1].progress_done = 12
    app._queue_jobs[2].state = "completed"
    app.queue_tree = mock.Mock()
    app.queue_tree.selection.return_value = [job.job_id for job in app._queue_jobs]
    app.queue_output_dir_var = Value(str(tmp_path / "default"))
    app._save_queue_state = mock.Mock(
        side_effect=lambda: export_queue.save(app._queue_jobs, str(tmp_path / "queue.json")),
    )
    app._refresh_queue_tree = mock.Mock()
    app._schedule_settings_save = mock.Mock()
    app.logln = mock.Mock()
    app._validate_adjustment_draft = mock.Mock(return_value=True)
    app._collect_settings = mock.Mock(return_value={"style": 7})
    app._collect_export_settings = mock.Mock(return_value={"output_container": "mp4"})
    return app


def snapshots(app):
    return [job.to_dict() for job in app._queue_jobs]


def test_change_output_preserves_settings_states_names_and_existing_files(queue_app, tmp_path):
    app = queue_app
    destination = tmp_path / "chosen"
    destination.mkdir()
    original = Path(app._queue_jobs[0].output_path)
    original.parent.mkdir()
    original.write_bytes(b"keep existing export")
    before = snapshots(app)
    default = app.queue_output_dir_var.get()
    with mock.patch.object(gui.filedialog, "askdirectory", return_value=str(destination)):
        app.change_selected_queue_output_dir()
    for index, job in enumerate(app._queue_jobs):
        expected = before[index]
        if index < 2:
            expected["output_path"] = str(destination / Path(expected["output_path"]).name)
        assert job.to_dict() == expected
    assert app.queue_output_dir_var.get() == default
    assert original.read_bytes() == b"keep existing export"
    assert list(destination.iterdir()) == []  # Only records change; no files move or appear.
    assert snapshots(app) == [job.to_dict() for job in export_queue.load(str(tmp_path / "queue.json"))]
    app._save_queue_state.assert_called_once()
    app._refresh_queue_tree.assert_called_once()
    app._collect_settings.assert_not_called()
    app._collect_export_settings.assert_not_called()
    app._validate_adjustment_draft.assert_not_called()


def test_batch_change_avoids_disk_queue_and_selected_job_collisions(queue_app, tmp_path):
    app = queue_app
    destination = tmp_path / "chosen"
    destination.mkdir()
    existing = destination / "clip.mp4"
    existing.write_bytes(b"keep")
    app._queue_jobs = [
        export_queue.ExportJob.create(str(tmp_path / f"source{i}.mp4"), str(path), {}, {})
        for i, path in enumerate([
            tmp_path / "a" / "clip.mp4", tmp_path / "b" / "clip.mp4",
            destination / "clip_2.mp4", destination / "clip_4.mp4",
        ])
    ]
    app._queue_jobs[3].state = "completed"
    app.queue_tree.selection.return_value = [job.job_id for job in app._queue_jobs]
    with mock.patch.object(gui.filedialog, "askdirectory", return_value=str(destination)):
        app.change_selected_queue_output_dir()
        outputs = [job.output_path for job in app._queue_jobs]
        assert [Path(path).name for path in outputs] == ["clip_3.mp4", "clip_5.mp4", "clip_2.mp4", "clip_4.mp4"]
        app.change_selected_queue_output_dir()  # Selecting the same folder is a no-op.
    assert [job.output_path for job in app._queue_jobs] == outputs
    assert existing.read_bytes() == b"keep"
    app._save_queue_state.assert_called_once()


def test_cancel_and_completed_only_selection_leave_queue_untouched(queue_app):
    app = queue_app
    before = snapshots(app)
    with mock.patch.object(gui.filedialog, "askdirectory", return_value="") as choose:
        app.change_selected_queue_output_dir()
        choose.assert_called_once()
        choose.reset_mock()
        app.queue_tree.selection.return_value = [app._queue_jobs[2].job_id]
        app.change_selected_queue_output_dir()
        app.queue_tree.selection.return_value = []
        app.change_selected_queue_output_dir()
        choose.assert_not_called()
    assert snapshots(app) == before
    app._save_queue_state.assert_not_called()


@pytest.mark.parametrize("flag", ["_queue_running", "_exporting", "_diagnosing", "_switching_backend"])
def test_busy_queue_cannot_change_output(queue_app, flag):
    setattr(queue_app, flag, True)
    with mock.patch.object(gui.filedialog, "askdirectory") as choose:
        queue_app.change_selected_queue_output_dir()
        queue_app.choose_queue_output_dir()
    choose.assert_not_called()
    queue_app._save_queue_state.assert_not_called()


def test_dialog_rechecks_busy_state_and_original_tasks(queue_app, tmp_path):
    app = queue_app
    before = snapshots(app)

    def start_while_choosing(**_kwargs):
        app._queue_running = True
        return str(tmp_path / "chosen")

    with mock.patch.object(gui.filedialog, "askdirectory", side_effect=start_while_choosing):
        app.change_selected_queue_output_dir()
    assert snapshots(app) == before
    app._save_queue_state.assert_not_called()
    app._queue_running = False

    def finish_or_remove_while_choosing(**_kwargs):
        app._queue_jobs[0].state = "completed"
        app._queue_jobs.pop(1)
        return str(tmp_path / "chosen")

    with mock.patch.object(gui.filedialog, "askdirectory", side_effect=finish_or_remove_while_choosing):
        app.change_selected_queue_output_dir()
    assert app._queue_jobs[0].output_path == before[0]["output_path"]
    app._save_queue_state.assert_not_called()


@pytest.mark.parametrize("route", ["typed", "picker"])
@pytest.mark.parametrize("kind,extension", [("image", ".png"), ("video", ".mp4")])
def test_default_output_changes_only_future_jobs(queue_app, tmp_path, route, kind, extension):
    app = queue_app
    before = snapshots(app)
    destination = str(tmp_path / "new-default")
    if route == "typed":
        app.queue_output_dir_var.set(destination)
        app._on_queue_output_dir_change()
    else:
        with mock.patch.object(gui.filedialog, "askdirectory", return_value=destination):
            app.choose_queue_output_dir()
    assert snapshots(app) == before
    target = app._new_queue_output_path(str(tmp_path / ("later" + extension)), kind, {"output_container": "mp4"})
    assert Path(target).parent == Path(destination)
    app._schedule_settings_save.assert_called_once()
    app._save_queue_state.assert_not_called()


def test_apply_parameters_keeps_each_job_folder_and_filename(queue_app):
    app = queue_app
    completed = app._queue_jobs[2].to_dict()
    original_paths = [Path(job.output_path) for job in app._queue_jobs]
    app._collect_export_settings.return_value = {"output_container": "mkv"}
    app.queue_tree.selection.return_value = [job.job_id for job in app._queue_jobs[:2]]
    app.apply_current_settings_to_queue()
    assert Path(app._queue_jobs[0].output_path) == original_paths[0]
    assert Path(app._queue_jobs[1].output_path) == original_paths[1].with_suffix(".mkv")
    assert all(job.settings == {"style": 7} for job in app._queue_jobs[:2])
    assert app._queue_jobs[2].to_dict() == completed


@pytest.mark.parametrize("index", [0, 1])
def test_changed_output_survives_restore_retry_and_export_dispatch(queue_app, tmp_path, index):
    app = queue_app
    destination = tmp_path / "chosen"
    with mock.patch.object(gui.filedialog, "askdirectory", return_value=str(destination)):
        app.change_selected_queue_output_dir()
    job = export_queue.load(str(tmp_path / "queue.json"))[index]
    app._queue_jobs = [job]
    job.reset_for_retry()
    app._queue_running = True
    app._queue_pause_requested = False
    app.root = SimpleNamespace(after=mock.Mock())

    def export(source, **kwargs):
        output = Path(kwargs["out_path"])
        assert output.parent == destination
        output.write_bytes(b"simulated encoded media")
        return {"success": True, "cancelled": False, "output_path": str(output), "frames": 1}

    app._export_image_source = app._export_video_source = mock.Mock(side_effect=export)
    app._run_next_queue_job()
    assert job.state == "completed"
    assert Path(job.output_path).read_bytes() == b"simulated encoded media"
    assert export_queue.load(str(tmp_path / "queue.json"))[0].output_path == job.output_path


@pytest.fixture(scope="module")
def queue_root():
    root = tk.Tk()
    yield root
    root.destroy()


@pytest.mark.parametrize("language,theme,width,restore_custom", [
    ("zh_CN", "light", 320, False), ("en_US", "light", 420, False),
    ("zh_CN", "dark", 360, True), ("en_US", "dark", 420, True),
])
def test_queue_panel_integrates_output_picker_and_context_menu(
    queue_app, queue_root, tmp_path, language, theme, width, restore_custom,
):
    app = queue_app
    previous_language = i18n.get_language()
    root = queue_root
    try:
        i18n.set_language(language)
        root.geometry(f"{width}x620+0+0")
        app.root = root
        app._ui = ui_theme.tokens(theme)
        saved_directory = str(tmp_path / ("saved_" * 16) / "输出") if restore_custom else ""
        app._saved_settings = {"queue_output_dir": saved_directory}
        app._theme_widgets = []
        ui_theme.configure_fonts(root)
        ui_theme.apply_ttk(root, app._ui)
        panel = gui.ttk.Frame(root)
        panel.pack(fill="both", expand=True)
        app._build_queue_tab(panel)
        app._refresh_queue_tree = lambda **kwargs: gui.App._refresh_queue_tree(app, **kwargs)
        app._refresh_queue_tree()
        root.update()
        assert not app.queue_tree.selection()
        for widget in (app.queue_selection_label, app.queue_details, app._queue_details_row):
            assert not widget.winfo_ismapped()
        idle_tree_height = app.queue_tree.winfo_height()
        start_position = app.queue_start_btn.winfo_rooty()
        source_label = i18n.tr("queue.output_source_dir")
        assert app.queue_output_label.cget("text") == i18n.tr("queue.default_output_dir")
        assert app.queue_output_dir_var.get() == saved_directory
        assert app.queue_output_choice_var.get() == (saved_directory or source_label)
        assert app.queue_output_combo.instate(["readonly"])
        assert (saved_directory or source_label) in app._queue_output_tip.text
        assert not hasattr(app, "queue_output_entry")
        assert not hasattr(app, "queue_output_browse_btn")
        assert app.queue_output_combo.winfo_rooty() + app.queue_output_combo.winfo_height() <= app.queue_add_files_btn.winfo_rooty()
        assert app.queue_output_label.winfo_rootx() == app.queue_add_files_btn.winfo_rootx()
        assert app.queue_output_combo.winfo_rootx() + app.queue_output_combo.winfo_width() == app.queue_add_folder_btn.winfo_rootx() + app.queue_add_folder_btn.winfo_width()
        assert app.queue_add_sequence_btn.winfo_rooty() + app.queue_add_sequence_btn.winfo_height() < app.queue_remove_btn.winfo_rooty()

        def choose_last_option():
            app.queue_output_combo.combo.current(len(app.queue_output_combo.cget("values")) - 1)
            app.queue_output_combo.combo.event_generate("<<ComboboxSelected>>")
            root.update()

        existing_jobs = snapshots(app)
        custom_directory = str(tmp_path / ("chosen_" * 16) / "导出")
        with mock.patch.object(gui.filedialog, "askdirectory", return_value=custom_directory):
            choose_last_option()
        assert app.queue_output_dir_var.get() == custom_directory
        assert app.queue_output_choice_var.get() == custom_directory
        assert custom_directory in app._queue_output_tip.text
        app._schedule_settings_save.assert_called_once()
        assert snapshots(app) == existing_jobs

        with mock.patch.object(gui.filedialog, "askdirectory", return_value=""):
            choose_last_option()
        assert app.queue_output_dir_var.get() == custom_directory
        assert app.queue_output_choice_var.get() == custom_directory
        assert app._schedule_settings_save.call_count == 1
        app._queue_running = True
        app.queue_output_choice_var.set(source_label)
        app._on_queue_output_choice()
        assert app.queue_output_dir_var.get() == custom_directory
        assert app.queue_output_choice_var.get() == custom_directory
        app._queue_running = False
        app.queue_output_combo.combo.current(0)
        app.queue_output_combo.combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert app.queue_output_dir_var.get() == ""
        assert app.queue_output_choice_var.get() == source_label
        assert app._schedule_settings_save.call_count == 2
        source = tmp_path / "inputs" / "later.png"
        assert Path(app._new_queue_output_path(str(source), "image")).parent == source.parent
        assert snapshots(app) == existing_jobs
        app.queue_tree.selection_set([job.job_id for job in app._queue_jobs])
        root.update()
        for widget in (app.queue_selection_label, app.queue_details, app._queue_details_row):
            assert widget.winfo_viewable()
        assert app.queue_selection_label.cget("text") == i18n.tr("queue.selection_count", selected=3, total=3)
        assert app.queue_details.cget("text") == i18n.tr("status.selected_jobs", count=3)
        assert app.queue_tree.winfo_height() < idle_tree_height
        assert app.queue_start_btn.winfo_rooty() == start_position
        assert not hasattr(app, "queue_change_output_btn")
        assert not hasattr(app, "queue_output_path_entry")
        actions = {key: enabled for key, _, _, enabled in app._queue_list.menu_actions()}
        assert actions["output_dir"]
        assert app._queue_details_tip.text == ""
        app.queue_tree.selection_set(app._queue_jobs[0].job_id)
        root.update()
        assert app._queue_jobs[0].source_path in app._queue_details_tip.text
        assert app._queue_jobs[0].output_path in app._queue_details_tip.text
        assert app.queue_tree.winfo_height() > 40
        for widget in (app.queue_output_combo, app.queue_start_btn):
            assert widget.winfo_viewable() and widget.winfo_width() > 50
            assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + width
            assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + 620
        app._queue_details_tip._show()
        assert app._queue_details_tip.tip is not None
        app.queue_tree.selection_remove(app.queue_tree.selection())
        root.update()
        assert app._queue_details_tip.tip is None
        assert app._queue_details_tip.text == ""
        assert app.queue_details.cget("text") == ""
        assert app.queue_selection_label.cget("text") == ""
        for widget in (app.queue_selection_label, app.queue_details, app._queue_details_row):
            assert not widget.winfo_ismapped()
        assert app.queue_tree.winfo_height() == idle_tree_height
        assert app.queue_start_btn.winfo_rooty() == start_position
        app.queue_tree.selection_set(app._queue_jobs[0].job_id)
        root.update()
        with mock.patch.object(gui.filedialog, "askdirectory", return_value=str(tmp_path / "chosen")):
            app._queue_list.command("output_dir")
        assert app._queue_jobs[0].output_path in app._queue_details_tip.text
        assert Path(app._queue_jobs[0].output_path).parent == tmp_path / "chosen"
        app.queue_tree.selection_set(app._queue_jobs[2].job_id)
        root.update()
        actions = {key: enabled for key, _, _, enabled in app._queue_list.menu_actions()}
        assert not actions["output_dir"]
        app._queue_running = True
        app._update_queue_action_states()
        assert app.queue_output_combo.instate(["disabled"])
        app._queue_running = False
        app._update_queue_action_states()
        assert app.queue_output_combo.instate(["!disabled", "readonly"])

        removed_jobs = list(app._queue_jobs)
        app.queue_tree.selection_set([job.job_id for job in removed_jobs])
        app.remove_selected_queue_jobs()
        root.update()
        assert not app._queue_jobs
        assert not app.queue_tree.selection()
        assert not app.queue_selection_label.winfo_ismapped()
        assert not app.queue_details.winfo_ismapped()
        assert app._queue_details_row.winfo_viewable()
        assert app.queue_undo_btn.winfo_viewable()
        assert app._queue_undo_label.cget("text") == i18n.tr("queue.removed_notice", count=3)
        assert app.queue_start_btn.winfo_rooty() == start_position
        app.queue_undo_btn.invoke()
        root.update()
        assert app._queue_jobs == removed_jobs
        assert set(app.queue_tree.selection()) == {job.job_id for job in removed_jobs}
        assert app.queue_selection_label.winfo_viewable()
        assert app.queue_details.winfo_viewable()
        assert not app._queue_undo_row.winfo_ismapped()
        app.queue_tree.selection_remove(app.queue_tree.selection())
        root.update()
        assert not app._queue_details_row.winfo_ismapped()
        assert app.queue_tree.winfo_height() == idle_tree_height
    finally:
        if hasattr(app, "_queue_list"):
            app._queue_list.destroy()
        for child in root.winfo_children():
            child.destroy()
        i18n.set_language(previous_language)
