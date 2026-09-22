"""About window links, latest-version line and the update button."""
from types import SimpleNamespace
from unittest import mock

import pytest


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _text(item):
    import tkinter as tk
    try:
        text = str(item.cget("text") or "")
    except tk.TclError:
        text = ""
    if text:
        return text
    try:
        variable = item.cget("textvariable")
    except tk.TclError:
        return ""
    if not variable:
        return ""
    try:
        return str(item.getvar(str(variable)))
    except (tk.TclError, TypeError):
        return ""


@pytest.mark.parametrize("tag,current,expected", [
    ("v9.0.0", "v2.3.1", "v9.0.0，可在此检查更新"),
    ("v2.3.1", "v2.3.1", "v2.3.1，已是最新"),
])
def test_about_shows_latest_version_and_checks_for_updates(tag, current, expected):
    import tkinter as tk
    from dlss5tool import about_dialog as about
    root = tk.Tk()
    root.withdraw()
    failures = []
    checked = []
    root.report_callback_exception = lambda *args: failures.append(args)

    def finish():
        texts = [_text(item) for item in _walk(root)]
        if expected not in texts:
            root.after(40, finish)
            return
        assert about.REPO_LABEL in texts
        assert "板板之歌" in "".join(texts)
        button = next(
            item for item in _walk(root)
            if callable(getattr(item, "invoke", None)) and _text(item) == about.tr("action.check_updates")
        )
        button.invoke()
        assert checked == [True]
        root.destroy()

    def timeout():
        failures.append("timeout")
        root.destroy()

    try:
        with mock.patch.object(
            about.updater, "fetch_latest_release", return_value=SimpleNamespace(tag=tag),
        ):
            window = about.show_about(root, lambda: checked.append(True), current_version=current)
            root.after(40, finish)
            root.after(4000, timeout)
            root.wait_window(window)
        assert not failures
        assert checked == [True]
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
