"""Tk installer regression tests. No payload is installed and no shortcut is made."""
from pathlib import Path
from types import SimpleNamespace
import json
import threading
import time
from unittest.mock import Mock

import pytest

from installer import setup


@pytest.fixture
def application(tmp_path):
    tk = pytest.importorskip("tkinter")

    def forbidden_executor(*_):
        raise AssertionError("This GUI test must not execute a real installation")

    try:
        app = setup.InstallerGUI(tmp_path / "application", False, show=False,
                                 executor=forbidden_executor)
    except tk.TclError as error:
        pytest.skip("Tk display is unavailable: " + str(error))
    yield app
    app.busy = False
    app.request_close()


def pump_until(application, predicate, seconds=3):
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        application.root.update()
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "The simulated GUI operation did not complete"


def test_custom_header_and_drag_keep_window_controls(application):
    app = application
    assert app.root.overrideredirect()
    assert app.chrome.bar.cget("bg") == setup.COLORS["bar"]
    assert app.chrome.close_button.cget("text") == "×"
    initial_x, initial_y = app.root.winfo_x(), app.root.winfo_y()
    app.chrome.start_drag(SimpleNamespace(x_root=initial_x + 20, y_root=initial_y + 20))
    app.chrome.drag(SimpleNamespace(x_root=initial_x + 55, y_root=initial_y + 45))
    assert f"+{initial_x + 35}+{initial_y + 25}" in app.root.geometry()
    app.chrome.drag(SimpleNamespace(x_root=-100, y_root=-100))
    assert "+0+0" in app.root.geometry()


def test_busy_install_cannot_close_from_buttons_escape_or_alt_f4(application):
    app = application
    release = threading.Event()

    def fake_executor(destination, shortcut, progress):
        progress(20)
        release.wait(2)
        raise OSError("simulated file lock")

    app.executor = fake_executor
    app.message = Mock()
    app.entry.focus_set = Mock()
    try:
        app.begin()
        assert app.busy
        assert all(str(control.cget("state")) == "disabled" for control in app.controls)
        assert app.request_close() is False
        assert app.chrome._close_event() == "break"
        assert app.root.winfo_exists()
        assert "等待完成" in app.status.get()
    finally:
        release.set()
    pump_until(app, lambda: not app.busy)
    assert app.root.winfo_exists()
    assert all(str(control.cget("state")) == "normal" for control in app.controls)
    assert app.message.call_args.args[0] == "安装未完成"
    assert "simulated file lock" in app.message.call_args.args[1]
    app.entry.focus_set.assert_called()


def test_blank_path_is_rejected_before_spawning_worker(application):
    app = application
    app.executor = Mock()
    app.message = Mock()
    app.entry.focus_set = Mock()
    app.selected.set('  ""  ')
    app.begin()
    assert not app.busy
    app.executor.assert_not_called()
    app.message.assert_called_once()
    app.entry.focus_set.assert_called_once()


def test_simulated_success_updates_progress_and_closes_without_launch(application, tmp_path):
    app = application
    observed = []
    app.launch_value.set(False)

    def fake_executor(destination, shortcut, progress):
        progress(65)
        return {"installed": True, "directory": str(tmp_path / "synthetic")}

    app.executor = fake_executor
    app.message = lambda title, text: observed.append((title, app.progress["value"]))
    app.begin()
    pump_until(app, lambda: app.closed)
    assert observed == [("安装完成", 100)]


def test_custom_dialog_restores_parent_modal_grab(application):
    app = application
    outer = setup.ThemedDialog(app.root, "父提示")
    outer.show()
    inner = setup.ThemedDialog(outer.window, "子提示")
    inner.show()
    try:
        assert inner.window.overrideredirect()
        assert inner.window.grab_current() is inner.window
        inner.close()
        assert outer.window.grab_current() is outer.window
    finally:
        inner.close()
        outer.close()


def test_folder_browser_lists_only_immediate_directories(application, tmp_path):
    folder = tmp_path / "visible"
    (folder / "nested-must-not-be-listed").mkdir(parents=True)
    (tmp_path / "plain-file.txt").write_text("temporary", encoding="utf-8")
    browser = setup.FolderBrowser(application.root, tmp_path)
    try:
        pump_until(application, lambda: browser.current == tmp_path)
        entries = browser.listbox.get(0, "end")
        assert entries == ("visible",)
        assert browser.dialog.window.overrideredirect()
        browser.accept()
        assert browser.result == tmp_path
        assert browser.dialog.closed
    finally:
        browser.close()


def test_unreadable_folder_never_accepts_an_unverified_path(application, tmp_path, monkeypatch):
    def denied(_):
        raise PermissionError("simulated directory permission")

    monkeypatch.setattr(setup.os, "scandir", denied)
    browser = setup.FolderBrowser(application.root, tmp_path)
    try:
        pump_until(application, lambda: "无法读取" in browser.status.get())
        assert str(browser.choose.cget("state")) == "disabled"
        browser.accept()
        assert browser.result is None
        assert not browser.dialog.closed
    finally:
        browser.close()


def test_silent_report_does_not_construct_gui(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    destination = tmp_path / "application"
    executor = Mock(return_value={"installed": True, "directory": str(destination)})
    monkeypatch.setattr(setup, "execute", executor)
    monkeypatch.setattr(setup, "gui", lambda *_: pytest.fail("silent mode must not construct Tk windows"))
    monkeypatch.setattr(setup.sys, "argv", ["setup", "--silent", "--no-shortcut", "--install-dir", str(destination), "--report", str(report)])
    assert setup.main() == 0
    executor.assert_called_once_with(destination, False)
    assert json.loads(report.read_text(encoding="utf-8"))["installed"] is True
