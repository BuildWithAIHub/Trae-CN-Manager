"""Dialog presentation and real modal interactions, without native/user data."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QDialog as NativeDialog, QDialogButtonBox,
                               QLabel, QLineEdit, QMessageBox as NativeMessageBox,
                               QPushButton, QTextEdit, QVBoxLayout)

from trae_manager.ui import (MainWindow, PasswordDialog, QDialog, QFileDialog,
                            QInputDialog, QMessageBox, TextEditor, prepare_application)


@pytest.fixture
def app():
    value = QApplication.instance() or QApplication([])
    prepare_application(value)
    return value


@pytest.fixture
def owner(app):
    value = QDialog()
    value.setWindowTitle("测试工作台")
    MainWindow.apply_theme(value, "dark")
    yield value
    value.close()
    value.deleteLater()
    app.processEvents()


def modal_action(app, action):
    errors = []
    def run():
        dialog = app.activeModalWidget()
        try:
            assert dialog is not None
            action(dialog)
        except Exception as exc:
            errors.append(exc)
            if dialog:
                dialog.reject()
    QTimer.singleShot(20, run)
    return errors


def assert_chrome(dialog):
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert dialog.property("traeThemedDialog")
    assert dialog._trae_title_bar.height() == 44
    assert dialog._trae_title_bar.findChild(QLabel, "titleBarName").text() == dialog.windowTitle()
    assert dialog.layout().contentsMargins().top() >= 56
    assert dialog._trae_title_bar.findChild(QPushButton, "windowMinimize") is None


def test_editor_caption_and_content_use_the_same_theme(app, owner):
    dialog = TextEditor("新建规则", "示例", "# 简单规则", parent=owner)
    dialog.show()
    app.processEvents()
    assert_chrome(dialog)
    assert dialog.editor.toPlainText() == "# 简单规则"
    assert dialog.name.geometry().top() >= 56
    dialog.setWindowTitle("编辑规则")
    assert dialog._trae_title_bar.findChild(QLabel, "titleBarName").text() == "编辑规则"
    assert "#121918" in app.styleSheet()
    MainWindow.apply_theme(owner, "light")
    assert "#f4f7f3" in app.styleSheet()
    QTest.mouseClick(dialog._trae_title_bar.findChild(QPushButton, "windowClose"), Qt.MouseButton.LeftButton)
    assert dialog.result() == NativeDialog.DialogCode.Rejected
    assert not dialog.isVisible()


def test_password_dialog_keeps_validation_and_esc(app, owner):
    dialog = PasswordDialog("迁移密码", "设置密码", parent=owner)
    errors = modal_action(app, lambda current: (assert_chrome(current), QTest.keyClick(current, Qt.Key.Key_Escape)))
    assert dialog.exec() == NativeDialog.DialogCode.Rejected
    assert not errors


def test_input_text_keeps_password_mode_and_enter(app, owner):
    def accept(dialog):
        assert isinstance(dialog, QInputDialog)
        assert_chrome(dialog)
        assert dialog.textEchoMode() == QLineEdit.EchoMode.Password
        dialog.setTextValue("a-test-password")
        entry = dialog.findChild(QLineEdit)
        entry.setFocus()
        QTest.keyClick(entry, Qt.Key.Key_Return)
    errors = modal_action(app, accept)
    text, accepted = QInputDialog.getText(owner, "验证密码", "输入密码", QLineEdit.EchoMode.Password)
    assert not errors
    assert accepted and text == "a-test-password"


def test_input_item_preserves_selection_and_cancel(app, owner):
    def cancel(dialog):
        assert_chrome(dialog)
        assert dialog.textValue() == "来源二"
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    errors = modal_action(app, cancel)
    text, accepted = QInputDialog.getItem(owner, "选择来源", "请选择", ["来源一", "来源二"], 1, False)
    assert not errors
    assert not accepted and text == "来源二"


def test_question_default_no_and_enter_never_approve(app, owner):
    def answer(dialog):
        assert_chrome(dialog)
        assert dialog.defaultButton() is dialog.button(QMessageBox.StandardButton.No)
        assert dialog.button(QMessageBox.StandardButton.No).text() == "取消"
        QTest.keyClick(dialog, Qt.Key.Key_Return)
    errors = modal_action(app, answer)
    result = QMessageBox.question(owner, "删除全部", "删除所选内容？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
    assert not errors
    assert result == QMessageBox.StandardButton.No


def test_confirmation_close_is_cancel(app, owner):
    def close(dialog):
        assert_chrome(dialog)
        QTest.mouseClick(dialog._trae_title_bar.findChild(QPushButton, "windowClose"), Qt.MouseButton.LeftButton)
    errors = modal_action(app, close)
    result = QMessageBox.question(owner, "操作确认", "继续？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
    assert not errors
    assert result != QMessageBox.StandardButton.Yes


def test_long_information_can_expand_scrollable_details(app, owner):
    message = "操作结果\n" + "这是详细信息。\n" * 90
    def inspect(dialog):
        assert_chrome(dialog)
        assert dialog.detailedText() == message
        details = next(control for control in dialog.buttons() if dialog.standardButton(control) == QMessageBox.StandardButton.NoButton)
        QTest.mouseClick(details, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert any(editor.isVisible() and editor.isReadOnly() for editor in dialog.findChildren(QTextEdit))
        assert_chrome(dialog)
        dialog.button(QMessageBox.StandardButton.Ok).click()
    errors = modal_action(app, inspect)
    assert QMessageBox.information(owner, "操作结果", message) == QMessageBox.StandardButton.Ok
    assert not errors


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_file_chooser_is_themed_chinese_and_selects_file(app, owner, tmp_path, theme):
    MainWindow.apply_theme(owner, theme)
    path = tmp_path / "配置.json"
    path.write_text("{}", encoding="utf-8")
    def choose(dialog):
        assert isinstance(dialog, QFileDialog)
        assert_chrome(dialog)
        assert dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
        assert dialog.labelText(QFileDialog.DialogLabel.FileName) == "文件名"
        assert dialog.labelText(QFileDialog.DialogLabel.Reject) == "取消"
        dialog.selectFile(str(path))
        dialog.accept()
    errors = modal_action(app, choose)
    selected, selected_filter = QFileDialog.getOpenFileName(owner, "导入配置", str(tmp_path), "JSON (*.json)")
    assert not errors
    assert Path(selected) == path
    assert selected_filter == "JSON (*.json)"


@pytest.mark.parametrize("close_method", ["escape", "caption"])
def test_directory_chooser_cancel_returns_empty(app, owner, tmp_path, close_method):
    def close(dialog):
        assert_chrome(dialog)
        if close_method == "escape":
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
        else:
            QTest.mouseClick(dialog._trae_title_bar.findChild(QPushButton, "windowClose"), Qt.MouseButton.LeftButton)
    errors = modal_action(app, close)
    assert QFileDialog.getExistingDirectory(owner, "选择目录", str(tmp_path)) == ""
    assert not errors


def test_qt_internal_dialogs_receive_chrome_without_replacing_layout(app, owner):
    # Qt's own overwrite prompt is not an instance of our wrapper class.
    dialog = NativeMessageBox(owner)
    dialog.setWindowTitle("覆盖文件")
    dialog.setText("是否替换？")
    dialog.setStandardButtons(NativeMessageBox.StandardButton.Yes | NativeMessageBox.StandardButton.No)
    original = dialog.layout()
    dialog.show()
    app.processEvents()
    assert_chrome(dialog)
    assert dialog.layout() is original
    dialog.close()


def test_generic_dialog_has_one_caption_after_repeated_show(app, owner):
    dialog = QDialog(owner)
    dialog.setWindowTitle("恢复备份")
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("预览内容"))
    dialog.show()
    app.processEvents()
    margins = layout.contentsMargins().top()
    dialog.hide()
    dialog.show()
    app.processEvents()
    assert_chrome(dialog)
    assert layout.contentsMargins().top() == margins
    assert len([item for item in dialog.findChildren(QLabel) if item.objectName() == "titleBarName"]) == 1
    dialog.close()
