"""Chinese desktop interface for Trae CN Manager.

No account credentials are rendered in the interface. Long operations execute in
one worker at a time, keeping the event loop and the encrypted vault lifecycle
predictable. Importing this module has no side effects.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import (QEvent, QLibraryInfo, QObject, QRunnable, QSize, QThreadPool, QTime,
                            QTimer, QTranslator, Qt, QUrl, Signal, Slot)
from PySide6.QtGui import (QAction, QColor, QCloseEvent, QDesktopServices,
                           QFont, QFontDatabase, QIcon, QPainter, QPainterPath, QPalette,
                           QPen, QPixmap)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox,
    QDialogButtonBox, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QSizeGrip, QStackedWidget, QStyle, QStyleOptionButton, QStyleOptionComboBox, QStyleOptionSpinBox,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QTextEdit, QTimeEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtWidgets import (QDialog as _QtDialog, QFileDialog as _QtFileDialog,
                               QInputDialog as _QtInputDialog, QMessageBox as _QtMessageBox)


from .descriptions import summary_text
from .translation_ui import TranslationUIMixin

APP_NAME = "Trae CN 管家"
NAV_ITEMS = ["概览", "账号管理", "MCP 服务", "Skills", "Rules", "加密备份", "设置与诊断", "操作日志"]


def cn_datetime():
    return datetime.now(timezone(timedelta(hours=8)))


def display_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is not None:
            return moment.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        pass
    return text_value(value)


def prepare_application(app: QApplication):
    """Use consistent controls, including in Windows offscreen self-tests."""
    app.setStyle("Fusion")
    app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    if not hasattr(app, "_trae_chinese_translator"):
        translator = QTranslator(app)
        if translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            app.installTranslator(translator)
        app._trae_chinese_translator = translator
    if not hasattr(app, "_trae_dialog_chrome"):
        app._trae_dialog_chrome = DialogChromeManager(app)
        app.installEventFilter(app._trae_dialog_chrome)
    if sys.platform == "win32" and not QFontDatabase.families():
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
            candidate = fonts / name
            if candidate.is_file():
                QFontDatabase.addApplicationFont(str(candidate))
    app.setFont(QFont("Microsoft YaHei UI", 10))


class CheckBox(QCheckBox):
    """An explicit check mark remains legible with both stylesheet themes."""
    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isChecked():
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#163e29"), 2.2, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        mark = QPainterPath()
        mark.moveTo(rect.left() + rect.width() * .24, rect.top() + rect.height() * .51)
        mark.lineTo(rect.left() + rect.width() * .43, rect.top() + rect.height() * .70)
        mark.lineTo(rect.left() + rect.width() * .78, rect.top() + rect.height() * .30)
        painter.drawPath(mark)
        painter.end()


def paint_arrow(widget, painter, rect, up=False):
    if rect.isEmpty():
        return
    painter.fillRect(rect.adjusted(1, 1, -1, -1), widget.palette().brush(QPalette.ColorRole.Base))
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(widget.palette().color(QPalette.ColorRole.Text), 1.6,
                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    x, y = rect.center().x(), rect.center().y()
    direction = -1 if up else 1
    path = QPainterPath()
    path.moveTo(x - 3, y - direction)
    path.lineTo(x, y + direction * 2)
    path.lineTo(x + 3, y - direction)
    painter.drawPath(path)


def paint_spin_arrows(widget):
    option = QStyleOptionSpinBox()
    widget.initStyleOption(option)
    painter = QPainter(widget)
    for control, up in [(QStyle.SubControl.SC_SpinBoxUp, True), (QStyle.SubControl.SC_SpinBoxDown, False)]:
        rect = widget.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, control, widget)
        paint_arrow(widget, painter, rect, up)
    painter.end()


class TimeEdit(QTimeEdit):
    def paintEvent(self, event):
        super().paintEvent(event)
        paint_spin_arrows(self)


class SpinBox(QSpinBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        paint_spin_arrows(self)


class ComboBox(QComboBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        rect = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                          QStyle.SubControl.SC_ComboBoxArrow, self)
        painter = QPainter(self)
        paint_arrow(self, painter, rect)
        painter.end()


def app_icon() -> QIcon:
    from .brand import icon
    return icon()


def label(text: str, style: str = "", wrap: bool = False) -> QLabel:
    result = QLabel(text)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(wrap)
    if style:
        result.setObjectName(style)
    return result


def button(text: str, fn: Callable | None = None, primary: bool = False) -> QPushButton:
    result = QPushButton(text)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    result.setMinimumHeight(35)
    if primary:
        result.setProperty("primary", True)
    if fn:
        result.clicked.connect(lambda _checked=False: fn())
    return result


def text_value(value: Any, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def make_table(headers: list[str], rows: list[list[Any]], keys: list[Any] | None = None) -> QTableWidget:
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(43)
    table.setShowGrid(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            item = QTableWidgetItem(text_value(value))
            item.setToolTip(text_value(value))
            if c == 0:
                item.setData(Qt.ItemDataRole.UserRole, keys[r] if keys else r)
            table.setItem(r, c, item)
    table.resizeColumnsToContents()
    for c in range(len(headers)):
        table.setColumnWidth(c, min(330, max(115, table.columnWidth(c))))
    table.setMinimumHeight(160)
    return table


def selected_keys(table: QTableWidget) -> list[Any]:
    return [table.item(row.row(), 0).data(Qt.ItemDataRole.UserRole)
            for row in table.selectionModel().selectedRows()
            if not table.isRowHidden(row.row()) and table.item(row.row(), 0) is not None]


def scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(widget)
    return area


def panel() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(12)
    return frame, layout


def _ensure_dialog_chrome(dialog):
    """Keep Qt's own layouts intact while reserving a themed caption area."""
    dialog.setProperty("traeThemedDialog", True)
    if not dialog.windowFlags() & Qt.WindowType.FramelessWindowHint:
        dialog.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    # Some Qt widget constructors polish through a temporary Python wrapper.
    # Recover the existing child widgets instead of relying on wrapper state.
    bar = dialog.findChild(TitleBar, "titleBar", Qt.FindChildOption.FindDirectChildrenOnly)
    if bar is None:
        bar = TitleBar(dialog, compact=True)
    bar.window_ref = dialog
    dialog._trae_title_bar = bar
    grip = dialog.findChild(QSizeGrip, "dialogSizeGrip", Qt.FindChildOption.FindDirectChildrenOnly)
    if grip is None:
        grip = QSizeGrip(dialog)
        grip.setObjectName("dialogSizeGrip")
        grip.setFixedSize(16, 16)
    dialog._trae_size_grip = grip
    layout = dialog.layout()
    if layout is not None:
        base = layout.property("traeChromeBaseMargins")
        if base is None:
            margins = layout.contentsMargins()
            base = [margins.left(), margins.top(), margins.right(), margins.bottom()]
            layout.setProperty("traeChromeBaseMargins", base)
        left, top, right, bottom = base
        desired = (max(20, left), max(12, top) + 44, max(20, right), max(18, bottom))
        margins = layout.contentsMargins()
        if (margins.left(), margins.top(), margins.right(), margins.bottom()) != desired:
            layout.setContentsMargins(*desired)
    dialog._trae_title_bar.setGeometry(1, 1, max(1, dialog.width() - 2), 44)
    dialog._trae_title_bar.raise_()
    grip = dialog._trae_size_grip
    grip.move(max(0, dialog.width() - grip.width() - 1), max(0, dialog.height() - grip.height() - 1))
    resizable = dialog.minimumWidth() < dialog.maximumWidth() or dialog.minimumHeight() < dialog.maximumHeight()
    grip.setVisible(resizable)
    grip.raise_()


class DialogChromeManager(QObject):
    """Also cover Qt-created prompts such as save-file overwrite confirmation."""
    def eventFilter(self, watched, event):
        if isinstance(watched, _QtDialog):
            kind = event.type()
            if kind in (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.LayoutRequest, QEvent.Type.Resize):
                if watched.layout() is not None:
                    was_visible = watched.isVisible()
                    newly_framed = not watched.property("traeThemedDialog")
                    _ensure_dialog_chrome(watched)
                    if kind == QEvent.Type.Show and newly_framed and was_visible and not watched.isVisible():
                        QTimer.singleShot(0, watched.show)
        return super().eventFilter(watched, event)


class _DialogChromeMixin:
    def exec(self):
        _ensure_dialog_chrome(self)
        return super().exec()

    def show(self):
        _ensure_dialog_chrome(self)
        return super().show()

    def open(self, *args):
        _ensure_dialog_chrome(self)
        return super().open(*args)


class QDialog(_DialogChromeMixin, _QtDialog):
    pass


class QInputDialog(_DialogChromeMixin, _QtInputDialog):
    @classmethod
    def getText(cls, parent, title, message, echo=QLineEdit.EchoMode.Normal,
                text="", flags=Qt.WindowType.Widget, inputMethodHints=Qt.InputMethodHint.ImhNone):
        dialog = cls(parent, flags)
        dialog.setWindowTitle(title)
        dialog.setLabelText(message)
        dialog.setTextEchoMode(echo)
        dialog.setTextValue(text)
        dialog.setInputMethodHints(inputMethodHints)
        dialog.setOkButtonText("确定")
        dialog.setCancelButtonText("取消")
        dialog.setMinimumWidth(440)
        accepted = dialog.exec() == _QtDialog.DialogCode.Accepted
        return dialog.textValue(), accepted

    @classmethod
    def getItem(cls, parent, title, message, items, current=0, editable=True,
                flags=Qt.WindowType.Widget, inputMethodHints=Qt.InputMethodHint.ImhNone):
        dialog = cls(parent, flags)
        dialog.setWindowTitle(title)
        dialog.setLabelText(message)
        dialog.setComboBoxItems(items)
        dialog.setComboBoxEditable(editable)
        if items and 0 <= current < len(items):
            dialog.setTextValue(items[current])
        dialog.setInputMethodHints(inputMethodHints)
        dialog.setOkButtonText("确定")
        dialog.setCancelButtonText("取消")
        dialog.setMinimumWidth(440)
        accepted = dialog.exec() == _QtDialog.DialogCode.Accepted
        return dialog.textValue(), accepted


class QFileDialog(_DialogChromeMixin, _QtFileDialog):
    @classmethod
    def _choose(cls, parent, caption, directory, name_filter, mode, accept,
                selected_filter="", options=_QtFileDialog.Option(0)):
        dialog = cls(parent, caption, directory, name_filter)
        dialog.setOptions(options | _QtFileDialog.Option.DontUseNativeDialog)
        dialog.setFileMode(mode)
        dialog.setAcceptMode(accept)
        dialog.setLabelText(_QtFileDialog.DialogLabel.Accept, "保存" if accept == _QtFileDialog.AcceptMode.AcceptSave else "选择")
        dialog.setLabelText(_QtFileDialog.DialogLabel.Reject, "取消")
        dialog.setLabelText(_QtFileDialog.DialogLabel.LookIn, "位置")
        dialog.setLabelText(_QtFileDialog.DialogLabel.FileName, "文件名")
        dialog.setLabelText(_QtFileDialog.DialogLabel.FileType, "文件类型")
        if selected_filter:
            dialog.selectNameFilter(selected_filter)
        dialog.resize(860, 610)
        if dialog.exec() == _QtDialog.DialogCode.Accepted:
            selected = dialog.selectedFiles()
            return selected[0] if selected else "", dialog.selectedNameFilter()
        return "", ""

    @classmethod
    def getOpenFileName(cls, parent=None, caption="", directory="", filter="", selectedFilter="", options=_QtFileDialog.Option(0)):
        return cls._choose(parent, caption, directory, filter, _QtFileDialog.FileMode.ExistingFile,
                           _QtFileDialog.AcceptMode.AcceptOpen, selectedFilter, options)

    @classmethod
    def getSaveFileName(cls, parent=None, caption="", directory="", filter="", selectedFilter="", options=_QtFileDialog.Option(0)):
        return cls._choose(parent, caption, directory, filter, _QtFileDialog.FileMode.AnyFile,
                           _QtFileDialog.AcceptMode.AcceptSave, selectedFilter, options)

    @classmethod
    def getExistingDirectory(cls, parent=None, caption="", directory="", options=_QtFileDialog.Option.ShowDirsOnly):
        return cls._choose(parent, caption, directory, "", _QtFileDialog.FileMode.Directory,
                           _QtFileDialog.AcceptMode.AcceptOpen, options=options)[0]


class QMessageBox(_DialogChromeMixin, _QtMessageBox):
    @classmethod
    def _standard(cls, parent, title, message, icon, buttons, default):
        dialog = cls(parent)
        dialog.setWindowTitle(title)
        dialog.setIcon(icon)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        text = str(message)
        if len(text) > 650 or text.count("\n") > 12:
            dialog.setText(text.splitlines()[0][:180] or title)
            dialog.setInformativeText("展开详细内容查看完整信息。")
            dialog.setDetailedText(text)
            for control in dialog.buttons():
                if "Details" in control.text() or "详细" in control.text():
                    control.setText("详细内容")
        else:
            dialog.setText(text)
        dialog.setStandardButtons(buttons)
        if default != _QtMessageBox.StandardButton.NoButton:
            dialog.setDefaultButton(default)
        translations = {_QtMessageBox.StandardButton.Ok: "确定", _QtMessageBox.StandardButton.Cancel: "取消",
                        _QtMessageBox.StandardButton.Yes: "确定", _QtMessageBox.StandardButton.No: "取消"}
        for value, caption in translations.items():
            control = dialog.button(value)
            if control:
                control.setText(caption)
        dialog.exec()
        clicked = dialog.clickedButton()
        return dialog.standardButton(clicked) if clicked else _QtMessageBox.StandardButton.NoButton

    @classmethod
    def information(cls, parent, title, text, buttons=_QtMessageBox.StandardButton.Ok, defaultButton=_QtMessageBox.StandardButton.NoButton):
        return cls._standard(parent, title, text, _QtMessageBox.Icon.Information, buttons, defaultButton)

    @classmethod
    def warning(cls, parent, title, text, buttons=_QtMessageBox.StandardButton.Ok, defaultButton=_QtMessageBox.StandardButton.NoButton):
        return cls._standard(parent, title, text, _QtMessageBox.Icon.Warning, buttons, defaultButton)

    @classmethod
    def critical(cls, parent, title, text, buttons=_QtMessageBox.StandardButton.Ok, defaultButton=_QtMessageBox.StandardButton.NoButton):
        return cls._standard(parent, title, text, _QtMessageBox.Icon.Critical, buttons, defaultButton)

    @classmethod
    def question(cls, parent, title, text, buttons=_QtMessageBox.StandardButton.Yes | _QtMessageBox.StandardButton.No,
                 defaultButton=_QtMessageBox.StandardButton.NoButton):
        return cls._standard(parent, title, text, _QtMessageBox.Icon.Question, buttons, defaultButton)


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn: Callable):
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            self.signals.finished.emit(self.fn())
        except Exception as exc:
            # Controller errors are meant to be safe, human-readable messages.
            # Do not expose traceback, request headers, or live response bodies.
            import re
            message = str(exc)[:1200] or "操作未完成，请查看诊断结果后重试。"
            message = re.sub(r"(?i)(bearer\s+)[^\s,;\"]+", r"\1[已隐藏]", message)
            message = re.sub(r'(?i)((?:access_?token|refresh_?token|authorization|cookie|password|secret)[\"\s:=]+)[^\s,;}]+',
                             r"\1[已隐藏]", message)
            self.signals.failed.emit(message)
        finally:
            self.fn = None


class PasswordDialog(QDialog):
    def __init__(self, title: str, explanation: str, confirm: bool = True, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.addWidget(label(explanation, wrap=True))
        form = QFormLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("至少 8 个字符")
        form.addRow("密码", self.password)
        self.repeat = QLineEdit()
        self.repeat.setEchoMode(QLineEdit.EchoMode.Password)
        if confirm:
            form.addRow("确认密码", self.repeat)
        self.confirm = confirm
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def validate(self):
        if len(self.password.text()) < 8:
            QMessageBox.warning(self, "密码过短", "请使用至少 8 个字符的密码。")
            return
        if self.confirm and self.password.text() != self.repeat.text():
            QMessageBox.warning(self, "密码不一致", "两次输入的密码需要一致。")
            return
        self.accept()


class TextEditor(QDialog):
    def __init__(self, title: str, name: str, content: str, json_mode=False, parent=None, allow_subdirs=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 620)
        self.json_mode = json_mode
        self.allow_subdirs = allow_subdirs
        layout = QVBoxLayout(self)
        layout.addWidget(label("服务名称" if json_mode else "名称"))
        self.name = QLineEdit(name)
        self.name.setPlaceholderText("例如 browser-tools" if json_mode else "使用文件名称，不包含路径分隔符")
        layout.addWidget(self.name)
        layout.addWidget(label("MCP 服务配置（JSON 对象）" if json_mode else "Markdown 内容", "muted"))
        self.editor = QPlainTextEdit(content)
        self.editor.setFont(QFont("Consolas", 11))
        self.editor.setTabStopDistance(32)
        layout.addWidget(self.editor, 1)
        if json_mode:
            layout.addWidget(label("支持 stdio、SSE 和 HTTP；密钥仅写入所选范围的官方配置文件。", "muted", True))
        else:
            layout.addWidget(label("自动备份原文件；必要时重新加载 TRAE。", "muted", True))
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        controls.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        controls.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        controls.accepted.connect(self.validate)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)

    def validate(self):
        name = self.name.text().strip()
        parts = name.replace("\\", "/").split("/")
        if (not name or "\x00" in name or any(part in {"", ".", ".."} for part in parts)
                or (not self.allow_subdirs and len(parts) > 1)):
            QMessageBox.warning(self, "名称无效", "请输入有效名称，不要包含目录分隔符。")
            return
        if self.json_mode:
            try:
                value = json.loads(self.editor.toPlainText())
                if not isinstance(value, dict):
                    raise ValueError("需要 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                QMessageBox.warning(self, "JSON 格式错误", str(exc))
                return
        self.accept()


class TitleBar(QFrame):
    """A themed title bar with native Windows move and resize gestures."""
    def __init__(self, window, compact=False):
        super().__init__(window)
        self.window_ref = window
        self.compact = compact
        self.setObjectName("titleBar")
        self.setFixedHeight(44 if compact else 48)
        row = QHBoxLayout(self)
        row.setContentsMargins(17, 0, 8, 0)
        row.setSpacing(7)
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(23, 23))
        row.addWidget(icon)
        name = label(window.windowTitle() if compact else APP_NAME)
        name.setObjectName("titleBarName")
        if compact:
            name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            name.setToolTip(window.windowTitle())
            window.windowTitleChanged.connect(name.setText)
            window.windowTitleChanged.connect(name.setToolTip)
        row.addWidget(name, 1 if compact else 0)
        if not compact:
            row.addWidget(label("  本地账号与配置工作台", "muted"))
        if not compact:
            row.addStretch()
        controls = [
                ("—", "最小化", lambda: self.window_ref.showMinimized(), "windowMinimize"),
                ("□", "最大化 / 还原", self.toggle_maximized, "windowMaximize"),
                ("×", "关闭" if compact else "关闭至托盘", lambda: self.window_ref.close(), "windowClose")]
        for text, tip, handler, name in (controls[-1:] if compact else controls):
            control = button(text, handler)
            control.setObjectName(name)
            control.setToolTip(tip)
            control.setFixedSize(38, 32)
            row.addWidget(control)

    def toggle_maximized(self):
        if self.window_ref.isMaximized():
            self.window_ref.showNormal()
        else:
            self.window_ref.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window_ref.windowHandle()
            if handle and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.compact:
            self.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class SkillResourcesDialog(QDialog):
    """Manage files inside a skill without blocking on tree or text reads."""
    def __init__(self, owner, skill):
        super().__init__(owner)
        self.owner = owner
        self.skill = Path(skill["path"])
        self.setWindowTitle("Skill 文件与文件夹 · " + skill["name"])
        self.resize(860, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(label(str(self.skill), "muted", True))
        layout.addWidget(label("选择文件或文件夹可单独启停、编辑或删除；停用资源会移出 TRAE 的技能发现目录。整个 Skill 的启停在主列表操作。", "muted", True))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["文件 / 文件夹", "状态", "大小"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemDoubleClicked.connect(lambda item, _: self.edit_file())
        layout.addWidget(self.tree, 1)
        row = QHBoxLayout()
        row.addWidget(button("编辑文件", self.edit_file, True))
        row.addWidget(button("新建文件", lambda: self.edit_file(new=True)))
        row.addWidget(button("新建文件夹", self.create_directory))
        row.addWidget(button("启用所选", lambda: self.change_entries(True)))
        row.addWidget(button("停用所选", lambda: self.change_entries(False)))
        row.addWidget(button("删除所选", lambda: self.change_entries(None)))
        layout.addLayout(row)
        layout.addWidget(button("完成", self.accept))

    def reject(self):
        if not self.owner._busy:
            super().reject()

    def accept(self):
        if not self.owner._busy:
            super().accept()

    def selected_entries(self):
        # Operating a folder subsumes its selected children.
        values = [item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree.selectedItems()]
        return [value for value in values if value and not any(value["relative"].startswith(other["relative"] + "/") for other in values if other and other is not value)]

    def load_tree(self):
        def ready(entries):
            self.tree.clear()
            nodes = {}
            for entry in entries:
                relative = entry["relative"]
                parent = nodes.get(relative.rpartition("/")[0])
                item = QTreeWidgetItem([relative.rsplit("/", 1)[-1],
                                        "启用" if entry.get("enabled") else "停用", "" if entry.get("is_dir") else str(entry.get("size", 0)) + " B"])
                item.setData(0, Qt.ItemDataRole.UserRole, entry)
                if parent:
                    parent.addChild(item)
                else:
                    self.tree.addTopLevelItem(item)
                nodes[relative] = item
            self.tree.expandToDepth(1)
        self.owner.job("正在读取 Skill 文件树…", lambda: self.owner.controller.configs.list_skill_tree(self.skill), ready, refresh=False)

    def edit_file(self, new=False):
        entries = self.selected_entries()
        if not new and (len(entries) != 1 or entries[0].get("is_dir")):
            self.owner.message("选择文件", "请选择一个文本文件进行编辑。")
            return
        relative = ""
        if entries:
            relative = entries[0]["relative"]
            if new:
                relative = (relative + "/" if entries[0].get("is_dir") else relative.rpartition("/")[0] + "/").lstrip("/")

        def editor(content):
            dialog = TextEditor("新建 Skill 文件" if new else "编辑 Skill 文件", relative, content, parent=self, allow_subdirs=True)
            dialog.name.setPlaceholderText("技能内相对路径，例如 scripts/helper.py")
            if not new:
                dialog.name.setReadOnly(True)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                name, text = dialog.name.text().strip(), dialog.editor.toPlainText()
                self.owner.job("正在备份并保存 Skill 文件…", lambda: self.owner.controller.configs.save_skill_file(self.skill, name, text), lambda _: self.load_tree(), refresh=False)

        if new:
            editor("")
        else:
            self.owner.job("正在读取 Skill 文本…", lambda: self.owner.controller.configs.read_skill_file(self.skill, relative), editor, refresh=False)

    def create_directory(self):
        relative, ok = QInputDialog.getText(self, "新建 Skill 文件夹", "相对于当前 Skill 的目录，例如 assets/icons")
        if ok and relative.strip():
            self.owner.job("正在创建 Skill 文件夹…", lambda: self.owner.controller.configs.create_skill_directory(self.skill, relative.strip()), lambda _: self.load_tree(), refresh=False)

    def change_entries(self, enabled):
        entries = self.selected_entries()
        if not entries:
            self.owner.message("请选择资源", "在文件树中选择一个或多个文件 / 文件夹。")
            return
        action = "删除" if enabled is None else ("启用" if enabled else "停用")
        if not self.owner.confirm(action + " Skill 资源", f"{action}所选 {len(entries)} 项及其文件夹内容？变更前会保存备份。SKILL.md 的启停会作用于整个技能。"):
            return
        configs = self.owner.controller.configs

        def operation():
            for entry in entries:
                if enabled is None:
                    configs.delete_skill_entry(self.skill, entry["relative"])
                else:
                    configs.set_skill_entry_enabled(self.skill, entry["relative"], enabled)

        def ready(_):
            # Moving SKILL.md moves the whole skill root.
            if any(entry["relative"] == "SKILL.md" for entry in entries):
                self.accept()
            else:
                self.load_tree()

        self.owner.job("正在" + action + " Skill 资源…", operation, ready, refresh=False)


class MainWindow(TranslationUIMixin, QMainWindow):
    def __init__(self, controller):
        super().__init__()
        prepare_application(QApplication.instance())
        self.controller = controller
        self.data: dict = {"accounts": [], "settings": {}, "discovery": {}, "logs": []}
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._worker: Worker | None = None
        self._busy = False
        self._quitting = False
        self._refreshing = False
        self._auto_check_done = False
        self._last_activity = time.monotonic()
        self._scopes: dict[str, str] = {}
        self._page_index = 0
        self._account_search = ""
        self._schedule_day = ""
        self._view_cache: dict = {}
        self._location_flow = False
        self._location_checked = False
        self._next_usage_poll = 0.0
        self._quiet_usage_running = False
        self._item_filters = {"skills": {"text": "", "status": "all"}, "rules": {"text": "", "status": "all"}}
        self.item_actions = {}
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(1230, 810)
        self.setMinimumSize(1000, 690)
        self.root = QStackedWidget()
        frame = QWidget()
        frame.setObjectName("windowFrame")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        self.title_bar = TitleBar(self)
        outer.addWidget(self.title_bar)
        outer.addWidget(self.root, 1)
        self.setCentralWidget(frame)
        self.lock_screen = self._build_lock_screen()
        self.root.addWidget(self.lock_screen)
        self.shell = self._build_shell()
        self.root.addWidget(self.shell)
        self.apply_theme("dark")
        self._build_tray()
        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(10_000)
        self.idle_timer.timeout.connect(self._tick)
        self.idle_timer.start()
        self.usage_timer = QTimer(self)
        self.usage_timer.setInterval(1000)
        self.usage_timer.timeout.connect(self._usage_tick)
        self.usage_timer.start()
        QApplication.instance().installEventFilter(self)
        if controller.is_unlocked:
            self.refresh()
        else:
            self.root.setCurrentWidget(self.lock_screen)

    def _build_lock_screen(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addStretch()
        row = QHBoxLayout()
        row.addStretch()
        card, layout = panel()
        card.setFixedWidth(490)
        layout.setContentsMargins(38, 34, 38, 34)
        layout.addWidget(label("TRAE  /  CN", "brand"))
        layout.addSpacing(7)
        layout.addWidget(label(APP_NAME, "hero"))
        self.unlock_caption = label("解锁你的账号与配置工作台", "muted", True)
        layout.addWidget(self.unlock_caption)
        layout.addSpacing(18)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("输入解锁密码")
        self.password_input.setMinimumHeight(45)
        layout.addWidget(self.password_input)
        self.password_repeat = QLineEdit()
        self.password_repeat.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_repeat.setPlaceholderText("再次输入密码，至少 8 个字符")
        self.password_repeat.setMinimumHeight(45)
        layout.addWidget(self.password_repeat)
        self.unlock_error = label("", "error", True)
        layout.addWidget(self.unlock_error)
        self.unlock_button = button("解锁", self.unlock, True)
        self.unlock_button.setMinimumHeight(44)
        layout.addWidget(self.unlock_button)
        self.password_input.returnPressed.connect(self.unlock)
        self.password_repeat.returnPressed.connect(self.unlock)
        layout.addSpacing(12)
        layout.addWidget(button("忘记解锁密码 / 使用恢复码", self.recover_password))
        layout.addWidget(button("打开管理器数据目录", self.open_data_directory))
        layout.addWidget(label("登录状态保存在本机加密保险库。设置中可生成恢复码，忘记密码后凭恢复码重设；请把恢复码另行妥善保存。", "muted", True))
        row.addWidget(card)
        row.addStretch()
        outer.addLayout(row)
        outer.addStretch()
        outer.addWidget(label("本地管理工具  ·  非 TRAE 官方产品", "muted"), alignment=Qt.AlignmentFlag.AlignCenter)
        outer.addSpacing(20)
        self._update_unlock_mode()
        return page

    def _update_unlock_mode(self):
        initialized = bool(self.controller.initialized)
        self.password_repeat.setVisible(not initialized)
        self.unlock_button.setText("解锁工作台" if initialized else "创建加密保险库")
        self.unlock_caption.setText("解锁你的账号与配置工作台" if initialized else "首次使用：设置一个本机解锁密码")

    def _build_shell(self):
        shell = QWidget()
        layout = QHBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(206)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 27, 16, 20)
        side.addWidget(label("TRAE CN", "brand"))
        side.addWidget(label("管家  /  本地工作台", "muted"))
        side.addSpacing(22)
        self.nav = QListWidget()
        self.nav.setObjectName("navigation")
        for title in NAV_ITEMS:
            item = QListWidgetItem(title)
            item.setSizeHint(QSize(160, 43))
            self.nav.addItem(item)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._navigate)
        side.addWidget(self.nav, 1)
        side.addWidget(label("账号加密保存\n全局共享 · 实例独立", "muted"))
        side.addSpacing(12)
        side.addWidget(button("锁定保险库", self.lock))
        layout.addWidget(sidebar)
        main = QWidget()
        body = QVBoxLayout(main)
        body.setContentsMargins(25, 20, 25, 12)
        body.setSpacing(14)
        top = QHBoxLayout()
        self.page_title = label("概览", "pageTitle")
        self.version_label = label("正在读取 TRAE CN…", "muted")
        top.addWidget(self.page_title)
        top.addStretch()
        top.addWidget(self.version_label)
        top.addSpacing(10)
        top.addWidget(button("刷新", self.refresh))
        body.addLayout(top)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        status = QHBoxLayout()
        self.status_label = label("本地保险库已锁定", "muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(135)
        self.progress.setMaximumHeight(6)
        self.progress.hide()
        status.addWidget(self.status_label, 1)
        status.addWidget(self.progress)
        body.addLayout(status)
        layout.addWidget(main, 1)
        return shell

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        self.tray_show = QAction("打开管家", self)
        self.tray_show.triggered.connect(self.show_from_tray)
        menu.addAction(self.tray_show)
        action = QAction("全部账号签到", self)
        self.tray_checkin_action = action
        action.triggered.connect(lambda: self._tray_checkin())
        menu.addAction(action)
        action = QAction("锁定", self)
        action.triggered.connect(self.lock)
        menu.addAction(action)
        menu.addSeparator()
        action = QAction("退出管家", self)
        action.triggered.connect(self.quit_app)
        menu.addAction(action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_from_tray()
                                 if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _tray_checkin(self):
        if not self.controller.is_unlocked:
            self.show_from_tray()
            QMessageBox.information(self, "请先解锁", "解锁保险库后即可手动签到。已启用的 Windows 计划任务独立运行。")
            return
        self.checkin()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress,
                            QEvent.Type.Wheel, QEvent.Type.MouseMove):
            self._last_activity = time.monotonic()
        if (event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
                and isinstance(obj, QWidget) and obj.window() is self
                and not self.isMaximized()):
            position = self.mapFromGlobal(event.globalPosition().toPoint())
            edges = Qt.Edge(0)
            if position.x() <= 6:
                edges |= Qt.Edge.LeftEdge
            elif position.x() >= self.width() - 7:
                edges |= Qt.Edge.RightEdge
            if position.y() <= 6:
                edges |= Qt.Edge.TopEdge
            elif position.y() >= self.height() - 7:
                edges |= Qt.Edge.BottomEdge
            handle = self.windowHandle()
            if edges and handle and handle.startSystemResize(edges):
                return True
        return super().eventFilter(obj, event)

    def _tick(self):
        if not self.controller.is_unlocked or self._busy:
            return
        today = cn_datetime().date().isoformat()
        if getattr(self, "_checkin_display_day", "") != today:
            self._checkin_display_day = today
            self._patch_account_balances()
        if QApplication.activeModalWidget() is not None:
            return
        minutes = int(self.data.get("settings", {}).get("lock_minutes", 15) or 0)
        if minutes and time.monotonic() - self._last_activity >= minutes * 60:
            self.lock()

    def apply_theme(self, theme: str):
        dark = theme != "light"
        c = {"bg": "#121918", "side": "#171f1d", "card": "#1b2622", "input": "#17211d",
             "fg": "#edf6f0", "muted": "#9eb0a6", "border": "#304138", "hover": "#2a3b31",
             "accent": "#8de6b4", "alt": "#1e2a24", "selection": "#315643"} if dark else {
             "bg": "#f4f7f3", "side": "#edf2eb", "card": "#ffffff", "input": "#fafcf9",
             "fg": "#1d3327", "muted": "#63766a", "border": "#d5dfd4", "hover": "#e7eee4",
             "accent": "#247447", "alt": "#f4f8f1", "selection": "#cee6d5"}
        palette = QPalette()
        for role, key in [(QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.WindowText, "fg"),
                          (QPalette.ColorRole.Base, "input"), (QPalette.ColorRole.Text, "fg"),
                          (QPalette.ColorRole.Button, "card"), (QPalette.ColorRole.ButtonText, "fg"),
                          (QPalette.ColorRole.AlternateBase, "alt"), (QPalette.ColorRole.Highlight, "selection"),
                          (QPalette.ColorRole.HighlightedText, "fg"), (QPalette.ColorRole.Light, "muted"),
                          (QPalette.ColorRole.Mid, "border"), (QPalette.ColorRole.Dark, "border")]:
            palette.setColor(role, QColor(c[key]))
        QApplication.instance().setPalette(palette)
        self.setStyleSheet("""
            QWidget { font-family: 'Microsoft YaHei UI','Segoe UI'; font-size: 13px;
                color: %(fg)s; background: %(bg)s; }
            QLabel { background: transparent; }
            QFrame#sidebar { background: %(side)s; border-right: 1px solid %(border)s; }
            QWidget#windowFrame { border: 1px solid %(border)s; }
            QDialog[traeThemedDialog="true"] { border: 1px solid %(border)s; }
            QFrame#titleBar { background: %(side)s; border-bottom: 1px solid %(border)s; }
            QLabel#titleBarName { font-size: 14px; font-weight: 650; }
            QPushButton#windowMinimize, QPushButton#windowMaximize, QPushButton#windowClose {
                background: transparent; border: none; font-family: 'Segoe UI'; font-size: 19px; padding: 0; }
            QPushButton#windowMinimize:hover, QPushButton#windowMaximize:hover { background: %(hover)s; }
            QPushButton#windowClose:hover { background: #a63c43; color: white; }
            QFrame#card { background: %(card)s; border: 1px solid %(border)s; border-radius: 12px; }
            QLabel#brand { color: %(accent)s; font-size: 20px; font-weight: 700; letter-spacing: 2px; }
            QLabel#hero { font-size: 30px; font-weight: 700; }
            QLabel#pageTitle { font-size: 25px; font-weight: 700; }
            QLabel#sectionTitle { font-size: 16px; font-weight: 650; }
            QLabel#stat { font-size: 29px; font-weight: 700; color: %(accent)s; }
            QLabel#muted { color: %(muted)s; }
            QLabel#error { color: #e38a85; }
            QListWidget#navigation { background: transparent; border: none; outline: none; }
            QListWidget#navigation::item { padding: 8px 12px; border-radius: 7px; margin: 3px 0; }
            QListWidget#navigation::item:selected { background: %(selection)s; color: %(fg)s; font-weight: 650; }
            QListWidget#navigation::item:hover { background: %(hover)s; }
            QPushButton { background: %(card)s; border: 1px solid %(border)s; border-radius: 7px;
                padding: 6px 13px; }
            QPushButton:hover { background: %(hover)s; border-color: %(accent)s; }
            QPushButton:pressed { background: %(selection)s; }
            QPushButton:disabled { color: %(muted)s; }
            QPushButton[primary="true"] { background: #a4edbd; color: #133023; border-color: #a4edbd; font-weight: 650; }
            QPushButton[primary="true"]:hover { background: #baf5ce; }
            QPushButton[primary="true"]:disabled { background: %(card)s; color: %(muted)s; border-color: %(border)s; }
            QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QTimeEdit { background: %(input)s;
                border: 1px solid %(border)s; border-radius: 6px; padding: 7px 9px; selection-background-color: %(selection)s; }
            QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: %(accent)s; }
            QComboBox::drop-down { border: none; width: 24px; }
            QComboBox QAbstractItemView { background: %(card)s; selection-background-color: %(selection)s; }
            QTableWidget, QTreeWidget { background: %(card)s; alternate-background-color: %(alt)s; border: 1px solid %(border)s;
                border-radius: 8px; selection-background-color: %(selection)s; selection-color: %(fg)s; }
            QHeaderView::section { background: %(side)s; padding: 11px; border: none; border-bottom: 1px solid %(border)s;
                color: %(muted)s; font-weight: 500; }
            QTableWidget::item { padding: 5px 9px; border: none; }
            QScrollArea, QStackedWidget { border: none; }
            QScrollBar:vertical { background: transparent; width: 10px; }
            QScrollBar::handle:vertical { background: %(border)s; min-height: 25px; border-radius: 4px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QProgressBar { border: none; background: %(side)s; border-radius: 3px; }
            QProgressBar::chunk { background: %(accent)s; border-radius: 3px; }
            QCheckBox { background: transparent; spacing: 9px; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid %(muted)s;
                border-radius: 3px; background: %(input)s; }
            QCheckBox::indicator:checked { background: #a4edbd; border-color: #a4edbd; }
            QMenu { background: %(card)s; border: 1px solid %(border)s; }
            QMenu::item { padding: 8px 25px; }
            QMenu::item:selected { background: %(selection)s; }
            QToolTip { background: %(card)s; color: %(fg)s; border: 1px solid %(border)s; }
        """ % c)
        QApplication.instance().setStyleSheet(self.styleSheet())

    def unlock(self):
        if self._busy:
            return
        password = self.password_input.text()
        setup = not self.controller.initialized
        self.unlock_error.clear()
        if len(password) < 8:
            self.unlock_error.setText("请输入至少 8 个字符的密码。")
            return
        if setup and password != self.password_repeat.text():
            self.unlock_error.setText("两次输入的密码不一致。")
            return
        self.password_input.clear()
        self.password_repeat.clear()
        self.unlock_button.setEnabled(False)

        def ready(_):
            self.unlock_button.setEnabled(True)
            self._last_activity = time.monotonic()
            self.refresh()

        def failed(message):
            self.unlock_button.setEnabled(True)
            self.unlock_error.setText(message)
            self.password_input.setFocus()
            self._update_unlock_mode()

        fn = (lambda: self.controller.setup_password(password)) if setup else (lambda: self.controller.unlock(password))
        self.job("正在解锁保险库…", fn, ready, refresh=False, failure=failed)

    def lock(self):
        if self._busy:
            QMessageBox.information(self, "操作进行中", "请等待当前操作结束后再锁定。")
            return
        if not self.controller.is_unlocked:
            return
        try:
            self.controller.lock()
        except Exception:
            QMessageBox.warning(self, "锁定未完成", "请等待后台操作结束后重试。")
            return
        self.data = {"accounts": [], "settings": {}, "discovery": {}, "logs": []}
        self.mcp_entries = []
        self.skills_entries = []
        self.rules_entries = []
        self.current_mcp_path = None
        self._account_search = ""
        self._account_filter = 0
        self._mcp_search = ""
        self._mcp_filter = 0
        getattr(self, "account_actions", {}).clear()
        getattr(self, "mcp_actions", {}).clear()
        self._scopes.clear()
        self._item_filters = {"skills": {"text": "", "status": "all"}, "rules": {"text": "", "status": "all"}}
        self.item_actions.clear()
        self.__dict__.get("_purpose_translations", {}).clear()
        self.__dict__.get("_purpose_views", {}).clear()
        self._view_cache.clear()
        self._location_checked = False
        self._location_flow = False
        self._next_usage_poll = 0
        for dialog in self.findChildren(QDialog):
            for field in dialog.findChildren(QLineEdit):
                field.clear()
            for editor in dialog.findChildren(QPlainTextEdit):
                editor.clear()
            dialog.reject()
            dialog.deleteLater()
        self._clear_pages()
        self._auto_check_done = False
        self.password_input.clear()
        self.password_repeat.clear()
        self.unlock_error.clear()
        self._update_unlock_mode()
        self.root.setCurrentWidget(self.lock_screen)
        self.status_label.setText("本地保险库已锁定")
        self.password_input.setFocus()

    def job(self, caption: str, fn: Callable, done: Callable | None = None,
            refresh=True, failure: Callable | None = None, quiet=False):
        if self._busy:
            self.status_label.setText("请等待当前操作结束…")
            return
        self._busy = True
        if not quiet:
            self.shell.setEnabled(False)
        self.status_label.setText(caption)
        self.progress.show()
        worker = Worker(fn)
        self._worker = worker

        def finished(value):
            self._busy = False
            self._worker = None
            self.shell.setEnabled(True)
            self.progress.hide()
            self.status_label.setText("操作完成  ·  " + cn_datetime().strftime("%H:%M:%S"))
            if done:
                done(value)
            if refresh and self.controller.is_unlocked:
                self.refresh()

        def failed(message):
            self._busy = False
            self._worker = None
            self.shell.setEnabled(True)
            self.progress.hide()
            self.status_label.setText("操作未完成，请重试或查看诊断。")
            if failure:
                failure(message)
            else:
                QMessageBox.warning(self, "操作未完成", message)

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.pool.start(worker)

    def open_data_directory(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.controller.data_dir)))

    def create_recovery_code(self):
        if not self.confirm("生成保险库恢复码", "恢复码可在忘记密码后重设密码并访问全部账号。生成新码会使旧码失效，请另存到安全位置。继续？"):
            return

        def ready(code):
            dialog = QDialog(self)
            dialog.setWindowTitle("保存恢复码")
            dialog.setMinimumWidth(520)
            layout = QVBoxLayout(dialog)
            layout.addWidget(label("请立即另行保存。管理器只保存加密恢复信息，不能再次显示这份恢复码。", "muted", True))
            field = QPlainTextEdit(str(code))
            field.setReadOnly(True)
            field.setMaximumHeight(95)
            layout.addWidget(field)
            controls = QHBoxLayout()
            controls.addWidget(button("复制恢复码", lambda: QApplication.clipboard().setText(field.toPlainText())))
            controls.addWidget(button("我已保存", dialog.accept, True))
            layout.addLayout(controls)
            dialog.exec()
            field.clear()
            dialog.deleteLater()

        self.job("正在创建恢复信息…", self.controller.create_recovery_code, ready, refresh=False)

    def recover_password(self):
        if self._busy:
            return
        if not getattr(self.controller, "recovery_available", False):
            self.message("尚未设置恢复码", "当前保险库没有恢复码。请使用原解锁密码，或从迁移备份导入；数据目录按钮可帮助你找到原始保险库与备份。管理器无法直接找回原密码。")
            return
        code, accepted = QInputDialog.getText(self, "使用恢复码", "请输入创建保险库恢复码时保存的完整内容", QLineEdit.EchoMode.Password)
        if not accepted or not code.strip():
            return
        dialog = PasswordDialog("重新设置解锁密码", "恢复码验证成功后，将用新密码重新加密原保险库。", True, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        password = dialog.password.text()
        dialog.password.clear()
        dialog.repeat.clear()

        def ready(_):
            self._last_activity = time.monotonic()
            self.refresh()
            self.message("密码已重设", "保险库已使用新密码加密。请在设置中检查恢复码状态。")

        self.job("正在验证恢复码并重新加密…", lambda: self.controller.recover_password(code.strip(), password), ready, refresh=False)

    def _location_startup(self):
        if not self.controller.is_unlocked:
            return
        if self._busy or QApplication.activeModalWidget() is not None:
            QTimer.singleShot(600, self._location_startup)
            return
        state = self._view_cache.get("location", {})
        if not state:
            return
        if state.get("quick_complete") or state.get("quick_status") == "found":
            self._show_location_notice(state)
        elif state.get("notice"):
            self.choose_location_resolution(state)

    def quick_locate(self):
        """Probe known installation sources; deep scans require an explicit choice."""
        def ready(state):
            self._view_cache["location"] = state
            if state.get("quick_complete") or state.get("quick_status") == "found":
                notice = state.get("notice")
                if notice:
                    self._show_location_notice(state, refresh=True)
                else:
                    self.message("TRAE 位置已确认", "程序：" + text_value(state.get("executable")) + "\n\n可以在「设置与诊断」查看和修改程序、资料与缓存位置。")
                    self.refresh()
            else:
                self.choose_location_resolution(state)

        self.job("正在通过进程、快捷方式和已知位置快速定位 TRAE…", self.controller.location_state, ready, refresh=False)

    def choose_location_resolution(self, state=None):
        state = state or self._view_cache.get("location", {})
        self._location_flow = True
        dialog = QDialog(self)
        dialog.setWindowTitle("快速找到 TRAE")
        dialog.setMinimumWidth(610)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("尚未确认完整的 TRAE 程序与资料位置", "sectionTitle"))
        layout.addWidget(label("最快的方法：先打开 TRAE，在任务管理器找到 TRAE 进程，右键「打开文件所在的位置」；或右键桌面 TRAE 快捷方式 → 属性 → 复制「目标」。无需判断安装在哪个盘。", "muted", True))
        path = QLineEdit()
        path.setPlaceholderText("粘贴程序 .exe、快捷方式 .lnk 或 TRAE 安装文件夹的完整路径")
        path.setMinimumHeight(40)
        layout.addWidget(path)
        chosen = {"path": "", "action": "later"}

        def apply_path(value):
            if value.strip():
                chosen.update(path=value.strip(), action="manual")
                dialog.accept()

        def browse_file():
            value, _ = QFileDialog.getOpenFileName(dialog, "选择 TRAE 程序或快捷方式", "", "TRAE 程序 / 快捷方式 (*.exe *.lnk);;所有文件 (*)")
            if value:
                apply_path(value)

        def browse_folder():
            value = QFileDialog.getExistingDirectory(dialog, "选择 TRAE 安装文件夹")
            if value:
                apply_path(value)

        row = QHBoxLayout()
        row.addWidget(button("浏览程序 / 快捷方式…", browse_file))
        row.addWidget(button("选择安装文件夹…", browse_folder))
        row.addWidget(button("验证粘贴的位置", lambda: apply_path(path.text()), True))
        layout.addLayout(row)
        layout.addWidget(label("想手动指定资料、缓存或全局配置，可进入设置；只有选择深度扫描时才会请求全盘读取授权。", "muted", True))
        more = QHBoxLayout()

        def choose(action):
            chosen["action"] = action
            dialog.accept()

        more.addWidget(button("深度扫描所有磁盘…", lambda: choose("deep")))
        more.addWidget(button("打开手动位置设置", lambda: choose("settings")))
        more.addStretch()
        more.addWidget(button("稍后处理", dialog.reject))
        layout.addLayout(more)
        path.returnPressed.connect(lambda: apply_path(path.text()))
        dialog.exec()
        self._location_flow = False
        action = chosen["action"]
        if action == "manual":
            value = os.path.expandvars(chosen["path"].strip().strip('"'))

            def saved(result):
                self._view_cache["location"] = result
                self.message("TRAE 位置已保存", "已验证你选择的程序位置。\n\n资料、缓存或全局配置可以在设置中继续补充。")
                notice = result.get("notice") or {}
                if notice.get("id"):
                    self.job("正在记住位置确认…", lambda: self.controller.acknowledge_location(notice["id"]))
                else:
                    self.refresh()

            self.job("正在验证所选 TRAE 位置…", lambda: self.controller.set_locations(executable=value), saved, refresh=False)
        elif action == "deep":
            self.request_location_scan()
        else:
            if action == "settings":
                self.nav.setCurrentRow(6)
                self._show_location_settings()
            notice = state.get("notice") or {}
            if notice.get("id"):
                self.job("正在记住位置提示…", lambda: self.controller.acknowledge_location(notice["id"]), refresh=False)

    def _show_location_settings(self):
        self.nav.setCurrentRow(6)
        page = self.pages.widget(6)
        target = getattr(self, "location_card", None)
        if isinstance(page, QScrollArea) and target:
            page.ensureWidgetVisible(target)
        field = getattr(self, "location_inputs", {}).get("executable")
        if field:
            field.setFocus()

    def _show_location_notice(self, state, refresh=False):
        notice = state.get("notice") or {}
        if notice.get("id"):
            titles = {"found": "TRAE 位置已找到", "moved": "TRAE 位置已更新", "permission": "部分目录需要访问权限", "missing": "请手动指定 TRAE 位置", "partial": "TRAE 程序已找到，部分目录待确认"}
            self.message(titles.get(notice.get("kind"), "TRAE 位置检测结果"), notice.get("message", "位置检测已完成。") + "\n\n点击确定后，此状态不会再次提示；位置迁移或状态变化后会重新通知。可在设置页修改路径。")
            self.job("正在记住位置检测结果…", lambda: self.controller.acknowledge_location(notice["id"]), refresh=refresh)

    def scan_locations(self):
        if self._busy:
            return
        self._location_flow = True
        dialog = QDialog(self)
        dialog.setWindowTitle("正在扫描本机 TRAE 位置")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("正在读取可访问磁盘的目录。扫描速度取决于磁盘数量与文件数量；无权读取的目录会记录在结果中。", wrap=True))
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(progress)
        cancel = button("取消扫描", self.controller.cancel_location_scan)
        layout.addWidget(cancel)
        dialog.rejected.connect(self.controller.cancel_location_scan)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.show()

        def ready(_):
            dialog.accept()
            dialog.deleteLater()
            self._location_flow = False
            self._location_checked = False
            self.refresh()

        def failed(message):
            dialog.reject()
            dialog.deleteLater()
            self._location_flow = False
            self.message("位置扫描未完成", message + "\n可在设置中手动填写位置。")

        self.job("正在扫描本机目录…", self.controller.scan_locations, ready, refresh=False, failure=failed)

    def request_location_scan(self):
        state = self._view_cache.get("location", {})
        if state.get("scan_consent") is not True:
            if not self.confirm("授权目录扫描", "允许读取本机各磁盘目录名称以定位 TRAE 程序、配置和缓存？可取消扫描，无访问权限的目录会跳过。"):
                return
            self.job("正在保存扫描授权…", lambda: self.controller.allow_location_scan(True), lambda _: self.scan_locations(), refresh=False)
        else:
            self.scan_locations()

    def save_locations(self):
        paths = {key: os.path.expandvars(field.text().strip().strip('"')) for key, field in self.location_inputs.items()}
        self.job("正在验证 TRAE 位置…", lambda: self.controller.set_locations(**paths),
                 lambda _: self.message("位置已保存", "路径已验证；后续按这些位置管理 TRAE。"))

    def location_guide(self):
        self.message("一分钟找到 TRAE 位置", "最快：先运行 TRAE → 打开任务管理器 → 找到 TRAE 进程 → 右键「打开文件所在的位置」，复制地址或选择其中的 TRAE 程序。\n\n也可右键桌面 TRAE 快捷方式 → 属性 → 复制「目标」。本工具支持粘贴 .exe、.lnk 或安装文件夹，含引号也可以。无需猜测是否位于 C 盘。\n\n用户资料：支持选择资料根目录、其中的 User 文件夹，或 User/globalStorage/storage.json，管理器会自动识别根目录。\n\n全局配置包含 skills、user_rules、memory 等；缓存通常跟随用户资料目录。位置检测结果可作为参考，搬迁过的目录请以实际位置为准。\n\n选择「浏览」直接定位，或粘贴完整路径（支持 %APPDATA% 这类环境变量）。仅当以上方式无法找到时，再选择深度扫描所有磁盘。")

    def _load_view(self, scopes):
        """Read disk and vault state entirely outside the GUI event loop."""
        location = self.controller.location_state() if hasattr(self.controller, "location_state") else {}
        value = self.controller.state()
        cache = {"projects": self.controller.projects(), "items": {}, "location": location}
        for kind in ("skills", "rules"):
            scope = scopes.get(kind, "global")
            project = scope[8:] if scope.startswith("project:") else None
            try:
                cache["items"][kind] = self.controller.configs.list_items(kind, project=project)
            except Exception:
                cache["items"][kind] = []
                cache[kind + "_error"] = True
        scope = scopes.get("mcp", "global")
        if scope.startswith("profile:"):
            path = self.controller.configs.mcp_path(profile=scope[8:])
        else:
            path = self.controller.configs.mcp_path(project=scope[8:] if scope.startswith("project:") else None)
        cache["mcp_path"] = path
        cache["skills_root"] = str((Path(value.get("discovery", {}).get("global_home") or self.controller.global_home) / "skills").resolve())
        try:
            cache["mcp"] = self.controller.configs.list_mcp(path)
        except Exception:
            cache["mcp"] = []
            cache["mcp_error"] = True
        if hasattr(self.controller, "memory_snapshots"):
            cache["memory"] = self.controller.memory_snapshots()
        return value, cache

    def refresh(self):
        if self._busy or not self.controller.is_unlocked:
            return

        def ready(result):
            value, self._view_cache = result
            self.data = value
            self.root.setCurrentWidget(self.shell)
            self.apply_theme(value.get("settings", {}).get("theme", "dark"))
            self._render_pages()
            if not self._location_checked and not getattr(self.controller, "ui_test_mode", False):
                self._location_checked = True
                QTimer.singleShot(50, self._location_startup)
            if not self._auto_check_done:
                self._auto_check_done = True
                if self.data.get("accounts") and self.data.get("settings", {}).get("auto_checkin", True):
                    QTimer.singleShot(200, self._startup_checkin)

        scopes = dict(self._scopes)
        self.job("正在读取本地状态…", lambda: self._load_view(scopes), ready, refresh=False)

    def _startup_checkin(self):
        if self._busy or self._location_flow or QApplication.activeModalWidget() is not None:
            QTimer.singleShot(1000, self._startup_checkin)
            return
        if self.controller.is_unlocked:
            self.checkin(quiet=True)

    def _usage_tick(self):
        settings = self.data.get("settings", {})
        if (not self.controller.is_unlocked or self._busy or self._quiet_usage_running
                or not self.data.get("accounts") or self._location_flow
                or QApplication.activeModalWidget() is not None
                or not settings.get("usage_auto_refresh", True)):
            return
        if time.monotonic() >= self._next_usage_poll:
            self._poll_usage()

    def _poll_usage(self):
        """Refresh only balance cells. Editors, selections and forms stay intact."""
        if self._busy or not self.controller.is_unlocked:
            return
        self._quiet_usage_running = True
        seconds = max(5, min(300, int(self.data.get("settings", {}).get("usage_refresh_seconds", 15))))
        self._next_usage_poll = time.monotonic() + seconds

        def operation():
            self.controller.refresh_usage(None)
            return self.controller.state().get("accounts", [])

        def ready(accounts):
            self._quiet_usage_running = False
            self.data["accounts"] = accounts
            self._patch_account_balances()

        def failed(_):
            self._quiet_usage_running = False
            self.status_label.setText("积分刷新暂未完成，保留上次结果；稍后自动重试。")

        self.job("正在同步官方积分…", operation, ready, refresh=False, failure=failed, quiet=True)

    def _patch_account_balances(self):
        table = getattr(self, "accounts_table", None)
        if table is None:
            return
        accounts = {a["id"]: a for a in self.data.get("accounts", [])}
        sorting = table.isSortingEnabled()
        table.setSortingEnabled(False)
        for row in range(table.rowCount()):
            key = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            account = accounts.get(key)
            if account:
                for column, value in enumerate(self._account_rows([account])[0]):
                    item = table.item(row, column)
                    item.setText(text_value(value))
                    item.setToolTip(text_value(value))
        table.setSortingEnabled(sorting)
        self._paint_checkin_badges()
        if hasattr(self, "_filter_accounts_view"):
            self._filter_accounts_view()
        self._refresh_checkin_controls()

    def _clear_pages(self):
        while self.pages.count():
            old = self.pages.widget(0)
            for table in old.findChildren(QTableWidget):
                table.clearContents()
            self.pages.removeWidget(old)
            old.deleteLater()

    def _render_pages(self):
        current = max(0, self.nav.currentRow())
        self._clear_pages()
        factories = [self._overview_page, self._accounts_page, self._mcp_page,
                     lambda: self._items_page("skills"), lambda: self._items_page("rules"),
                     self._backup_page, self._settings_page, self._logs_page]
        for factory in factories:
            try:
                self.pages.addWidget(factory())
            except Exception:
                error = QWidget()
                layout = QVBoxLayout(error)
                layout.addWidget(label("此页配置暂时无法读取，请运行环境诊断后重试。", "error", True))
                layout.addWidget(button("重试", self.refresh))
                layout.addStretch()
                self.pages.addWidget(error)
        self.pages.setCurrentIndex(current)
        self.page_title.setText(NAV_ITEMS[current])
        discovery = self.data.get("discovery", {})
        self.version_label.setText("TRAE CN  " + text_value(discovery.get("version"), "未检测到"))

    def _navigate(self, index):
        if index < 0:
            return
        self._page_index = index
        if hasattr(self, "pages") and index < self.pages.count():
            self.pages.setCurrentIndex(index)
            self.page_title.setText(NAV_ITEMS[index])

    def _content(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 5, 0)
        layout.setSpacing(17)
        return page, layout

    def _overview_page(self):
        page, layout = self._content()
        hero, hl = panel()
        hl.addWidget(label("把账号、签到与配置放在一起", "sectionTitle"))
        hl.addWidget(label("手机号登录交给官方客户端；这里管理独立实例、积分签到与本机配置。", "muted", True))
        actions = QHBoxLayout()
        self.overview_checkin_button = button("全部账号签到", self.checkin, True)
        self.overview_checkin_button.setEnabled(any(self._checkin_can_attempt(a) for a in self.data.get("accounts", [])))
        actions.addWidget(self.overview_checkin_button)
        actions.addWidget(button("检测本机账号", self.detect_accounts))
        actions.addWidget(button("新建独立账号", self.create_account))
        actions.addStretch()
        hl.addLayout(actions)
        layout.addWidget(hero)
        accounts = self.data.get("accounts", [])
        checked = sum(1 for a in accounts if cn_datetime().strftime("%Y-%m-%d") == str(a.get("last_success_day", "")))
        settings = self.data.get("settings", {})
        stats = QHBoxLayout()
        entries = [(str(len(accounts)), "已管理账号"), (str(checked), "今日已签到"),
                   (settings.get("schedule_time", "09:00") if settings.get("scheduled_enabled") else "未开启", "每日后台签到"),
                   (str(settings.get("lock_minutes", 15)) + " 分钟" if settings.get("lock_minutes", 15) else "手动", "闲置锁定")]
        for number, title in entries:
            card, cl = panel()
            value_label = label(str(number), "stat")
            if title == "今日已签到":
                self.overview_checkin_count = value_label
            cl.addWidget(value_label)
            cl.addWidget(label(title, "muted"))
            stats.addWidget(card, 1)
        layout.addLayout(stats)
        status, sl = panel()
        sl.addWidget(label("本机与兼容性", "sectionTitle"))
        discovery = self.data.get("discovery", {})
        compatibility = discovery.get("compatibility")
        if isinstance(compatibility, dict):
            compatibility = compatibility.get("message") or compatibility.get("state")
        form = QFormLayout()
        for title, value in [("TRAE 程序", discovery.get("executable")),
                             ("当前版本", discovery.get("version")),
                             ("兼容性", compatibility),
                             ("全局配置目录", discovery.get("global_home"))]:
            item = label(text_value(value), wrap=True)
            item.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(title, item)
        sl.addLayout(form)
        locate = QHBoxLayout()
        locate.addWidget(button("快速定位 TRAE", self.quick_locate, True))
        locate.addWidget(button("手动选择位置…", self.choose_location_resolution))
        locate.addStretch()
        sl.addLayout(locate)
        sl.addWidget(label("TRAE 更新后先检测版本与配置结构；接口或配置结构不兼容时会停止相应操作并给出诊断。", "muted", True))
        layout.addWidget(status)
        plan, pl = panel()
        pl.addWidget(label("关机错过签到如何处理", "sectionTitle"))
        pl.addWidget(label("Windows 计划任务支持错过后尽快运行。电脑完全关机时无法执行；再次开机并登录 Windows 后，按当天签到规则补跑。平台不支持的历史日期无法补签。", "muted", True))
        layout.addWidget(plan)
        layout.addStretch()
        return scroll(page)

    def _account_rows(self, accounts):
        return [[a.get("label"), a.get("user_id"), a.get("plan"), "不限额" if a.get("unlimited") else a.get("remaining"),
                 ("同步暂不可用 · 保留原值" if a.get("usage_state") not in (None, "", "ok") else a.get("status")),
                 display_time(a.get("usage_updated_at")), a.get("notes"), self._checkin_badge(a)] for a in accounts]

    @staticmethod
    def _account_checkin_state(account):
        today = cn_datetime().date().isoformat()
        if account.get("last_success_day") == today:
            return "already"
        if not account.get("user_id"):
            return "expired"
        state = account.get("checkin_state") if account.get("checkin_day") == today else "unknown"
        return state if state in {"pending", "unavailable", "expired", "error"} else "unknown"

    @classmethod
    def _checkin_can_attempt(cls, account):
        return cls._account_checkin_state(account) not in {"already", "unavailable", "expired"}

    @classmethod
    def _checkin_badge(cls, account):
        return {"already": "今日已签到", "pending": "待签到", "unavailable": "暂未开放",
                "expired": "需登录", "error": "待确认", "unknown": "待确认"}[cls._account_checkin_state(account)]

    def _paint_checkin_badges(self):
        table = getattr(self, "accounts_table", None)
        if table is None or table.columnCount() < 8:
            return
        accounts = {a["id"]: a for a in self.data.get("accounts", [])}
        dark = self.data.get("settings", {}).get("theme") != "light"
        for row in range(table.rowCount()):
            key = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            account = accounts.get(key, {})
            item = table.item(row, 7)
            state = self._account_checkin_state(account)
            item.setText(self._checkin_badge(account))
            color = ("#8de6b4" if dark else "#247447") if state == "already" else (
                ("#e8c787" if dark else "#8b6624") if state == "pending" else ("#9eb0a6" if dark else "#63766a"))
            item.setForeground(QColor(color))
            # Draw the tick: Windows font fallback can lack the Unicode glyph.
            if state == "already":
                pix = QPixmap(32, 32)
                pix.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pix)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(QPen(QColor(color), 3.5, Qt.PenStyle.SolidLine,
                                    Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                path = QPainterPath()
                path.moveTo(6, 16)
                path.lineTo(13, 23)
                path.lineTo(26, 9)
                painter.drawPath(path)
                painter.end()
                item.setIcon(QIcon(pix))
            else:
                item.setIcon(QIcon())
            font = QFont(table.font())
            font.setBold(state == "already")
            item.setFont(font)
            confirmation = account.get("checkin_confirmed_at") or account.get("last_checkin")
            tip = self._checkin_badge(account)
            if state == "already" and confirmation:
                tip += "\n最近确认：" + display_time(confirmation)
            if account.get("checkin_error"):
                tip += "\n最近查询未完成，保留已确认结果。" if state == "already" else "\n状态暂未更新，可刷新或重试签到。"
            item.setToolTip(tip)

    def _refresh_checkin_controls(self):
        accounts = self.data.get("accounts", [])
        pending = sum(self._checkin_can_attempt(a) for a in accounts)
        checked = sum(self._account_checkin_state(a) == "already" for a in accounts)
        enabled = bool(pending)
        for control in (getattr(self, "overview_checkin_button", None),
                        getattr(self, "tray_checkin_action", None),
                        getattr(self, "account_actions", {}).get("check_all")):
            if control is not None:
                control.setEnabled(enabled)
                control.setToolTip("所有账号今日已签到。" if accounts and checked == len(accounts)
                                   else f"可尝试 {pending} 个账号；已签到账号会跳过。")
        stat = getattr(self, "overview_checkin_count", None)
        if stat is not None:
            stat.setText(str(checked))

    def _accounts_page(self):
        page, layout = self._content()
        toolbar = QHBoxLayout()
        search = QLineEdit(self._account_search)
        search.setPlaceholderText("搜索账号或备注")
        search.setClearButtonEnabled(True)
        self.account_search = search
        toolbar.addWidget(search, 1)
        state_filter = ComboBox()
        state_filter.addItems(["全部状态", "今日已签到", "待签到 / 待确认", "需要登录"])
        state_filter.setCurrentIndex(getattr(self, "_account_filter", 0))
        self.account_status_filter = state_filter
        toolbar.addWidget(state_filter)
        add = button("添加账号", primary=True)
        add_menu = QMenu(add)
        add_menu.addAction("检测本机账号", self.detect_accounts)
        add_menu.addAction("新建独立账号…", self.create_account)
        add.setMenu(add_menu)
        toolbar.addWidget(add)
        more = button("更多")
        more_menu = QMenu(more)
        sync_action = more_menu.addAction("同步所选登录", lambda: self.account_action("sync"))
        edit_action = more_menu.addAction("编辑名称和备注…", self.edit_account)
        remove_action = more_menu.addAction("移除所选账号…", self.remove_accounts)
        memory_menu = more_menu.addMenu("记忆")
        save_memory_action = memory_menu.addAction("保存记忆快照…", self.save_memory_snapshot)
        sync_memory_action = memory_menu.addAction("同步记忆…", self.sync_account_memory)
        memory_menu.addAction("查看和恢复历史…", self.restore_memory_history)
        more_menu.addSeparator()
        check_all = more_menu.addAction("全部账号签到", self.checkin)
        refresh_all = more_menu.addAction("全部刷新积分", self.refresh_usage)
        more.setMenu(more_menu)
        toolbar.addWidget(more)
        layout.addLayout(toolbar)
        accounts = self.data.get("accounts", [])
        count_label = label("", "muted")
        self.accounts_count_label = count_label
        layout.addWidget(count_label)
        table = make_table(["名称", "账号 ID", "套餐", "积分 / 额度", "状态", "积分更新时间", "备注", "签到"],
                           self._account_rows(accounts), [a["id"] for a in accounts])
        self.accounts_table = table
        table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # IDs and notes remain searchable; show them only in the selected detail.
        table.setColumnHidden(1, True)
        table.setColumnHidden(6, True)
        table.horizontalHeader().moveSection(7, 4)
        table.setColumnWidth(7, 150)
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self._paint_checkin_badges()
        layout.addWidget(table, 1)
        detail, detail_layout = panel()
        detail_title = label("", "sectionTitle")
        detail_info = label("", "muted", True)
        detail_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(detail_title)
        detail_layout.addWidget(detail_info)
        actions = QHBoxLayout()
        launch = button("打开实例", lambda: self.account_action("launch"))
        check_selected = button("签到", lambda: self.checkin(self._account_selection()), True)
        refresh_selected = button("刷新积分", lambda: self.refresh_usage(self._account_selection()))
        for control in (launch, check_selected, refresh_selected):
            actions.addWidget(control)
        actions.addStretch()
        detail_layout.addLayout(actions)
        layout.addWidget(detail)
        self.account_detail = detail
        self.account_actions = {"launch": launch, "checkin": check_selected, "refresh": refresh_selected,
                                "sync": sync_action, "edit": edit_action, "remove": remove_action,
                                "save_memory": save_memory_action, "sync_memory": sync_memory_action,
                                "check_all": check_all, "refresh_all": refresh_all}
        check_all.setEnabled(bool(accounts))
        refresh_all.setEnabled(bool(accounts))

        def update_selection():
            ids = selected_keys(table)
            single, any_selected = len(ids) == 1, bool(ids)
            for control in (launch, check_selected, refresh_selected, sync_action, remove_action):
                control.setEnabled(any_selected)
            selected_accounts = [a for a in self.data.get("accounts", []) if a["id"] in ids]
            signed_count = sum(self._account_checkin_state(a) == "already" for a in selected_accounts)
            pending_count = sum(self._checkin_can_attempt(a) for a in selected_accounts)
            check_selected.setEnabled(bool(pending_count))
            check_selected.setText("今日已签到" if any_selected and signed_count == len(ids) else (
                f"签到（{pending_count}）" if len(ids) > 1 else "签到"))
            check_selected.setToolTip("已确认今日签到，明天可再次签到。" if signed_count == len(ids) and any_selected
                                      else "先查询官方状态，已签到的账号自动跳过。")
            for action in (edit_action, save_memory_action, sync_memory_action):
                action.setEnabled(single)
            detail.setVisible(any_selected)
            if single:
                account = next((a for a in self.data.get("accounts", []) if a["id"] == ids[0]), {})
                detail_title.setText(account.get("label", "账号"))
                info = "账号 ID：" + text_value(account.get("user_id"), "尚未登录")
                if account.get("notes"):
                    info += "\n备注：" + str(account["notes"])[:180]
                info += "\n签到：" + self._checkin_badge(account)
                detail_info.setText(info)
            elif any_selected:
                detail_title.setText(f"已选择 {len(ids)} 个账号")
                detail_info.setText(f"待签 / 待确认 {pending_count} 个 · 今日已签 {signed_count} 个。已签账号会跳过。")
            if any_selected:
                detail_info.setMinimumHeight(max(
                    detail_info.fontMetrics().lineSpacing() * (detail_info.text().count("\n") + 1) + 4,
                    detail_info.heightForWidth(max(120, detail_info.width()))))
            self._refresh_checkin_controls()

        def filter_rows(*_):
            text = search.text().casefold()
            self._account_search = search.text()
            self._account_filter = state_filter.currentIndex()
            current_accounts = {a["id"]: a for a in self.data.get("accounts", [])}
            today = cn_datetime().strftime("%Y-%m-%d")
            checked = sum(self._account_checkin_state(a) == "already" for a in current_accounts.values())
            visible = 0
            hidden_selection = False
            for r in range(table.rowCount()):
                account = current_accounts.get(table.item(r, 0).data(Qt.ItemDataRole.UserRole), {})
                signed = self._account_checkin_state(account) == "already"
                needs_login = (self._account_checkin_state(account) == "expired" or account.get("usage_state") == "expired"
                               or any(word in str(account.get("status", "")) for word in ("待登录", "登录失效", "重新登录")))
                matches_state = (self._account_filter == 0 or self._account_filter == 1 and signed
                                 or self._account_filter == 2 and not signed or self._account_filter == 3 and needs_login)
                content = " ".join(table.item(r, c).text() for c in range(table.columnCount())).casefold()
                shown = text in content and matches_state
                hidden_selection |= not shown and table.item(r, 0).isSelected()
                table.setRowHidden(r, not shown)
                visible += int(shown)
            if hidden_selection:
                table.clearSelection()
            states = [self._account_checkin_state(a) for a in current_accounts.values()]
            counts = [f"共 {len(current_accounts)} 个账号", f"已签到 {checked}"]
            for keys, caption in [(("pending",), "待签到"), (("unknown", "error"), "待确认"), (("expired",), "需登录"), (("unavailable",), "未开放")]:
                amount = sum(s in keys for s in states)
                if amount:
                    counts.append(f"{caption} {amount}")
            count_label.setText(" · ".join(counts + [f"显示 {visible}"]))
            update_selection()

        self._filter_accounts_view = filter_rows
        table.itemSelectionChanged.connect(update_selection)
        search.textChanged.connect(filter_rows)
        state_filter.currentIndexChanged.connect(filter_rows)
        filter_rows()
        layout.addWidget(label("按住 Ctrl 点击可多选；Shift 可连续选择。同步登录和记忆在「更多」中。", "muted", True))
        if not accounts:
            layout.addWidget(label("还没有账号，请点击「添加账号」。", "muted", True))
        return page

    def _account_selection(self):
        ids = selected_keys(self.accounts_table)
        if not ids:
            QMessageBox.information(self, "请选择账号", "请在表格中选中一个或多个账号。")
        return ids

    def _one_account(self):
        ids = self._account_selection()
        if not ids:
            return None
        if len(ids) > 1:
            QMessageBox.information(self, "请选择一个账号", "此操作每次只处理一个账号。")
            return None
        return next((a for a in self.data.get("accounts", []) if a["id"] == ids[0]), None)

    def detect_accounts(self):
        self.job("正在检测本机登录账号…", self.controller.detect_accounts,
                 lambda result: self.message("检测完成", "本机账号已更新。未找到时，请先在 TRAE 登录。"))

    def create_account(self):
        name, ok = QInputDialog.getText(self, "添加独立账号", "账号名称（可在登录后修改）")
        if not ok or not name.strip():
            return
        self.job("正在创建独立实例并打开官方登录…", lambda: self.controller.create_account(name.strip()),
                 lambda result: self.message("独立实例已准备", "请在 TRAE 完成登录，再选中账号，从「更多」同步登录。"))

    def account_action(self, action):
        ids = self._account_selection()
        if not ids:
            return
        if action == "launch":
            self._launch_with_memory(ids)
            return
        fn = self.controller.sync_account
        self.job("正在打开所选 TRAE 实例…" if action == "launch" else "正在同步所选登录状态…",
                 lambda: [fn(account_id) for account_id in ids])

    def save_memory_snapshot(self):
        account = self._one_account()
        if not account:
            return
        if not self.confirm("保存账号记忆快照", f"把本机当前的 TRAE 共享记忆保存为「{account.get('label', '')}」的加密快照？\n\nTRAE 原生记忆目录由本机各账号共享；快照标签用于管理器保存版本，不代表官方已按账号隔离。"):
            return
        self.job("正在加密保存记忆快照…", lambda: self.controller.save_memory_snapshot(account["id"]),
                 lambda _: self.message("记忆快照已保存", "可在打开实例时选择，或从「更多 → 记忆」同步。"))

    def _choose_memory_source(self, title, allow_none=True):
        snapshots = [snapshot for snapshot in self._view_cache.get("memory", []) if snapshot.get("captured_at")]
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(570)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("选择要合并的账号记忆快照。TRAE 原生记忆由本机共享，合并后其他已打开实例也可能读到；不复制登录信息和聊天数据库。", "muted", True))
        source = ComboBox()
        if allow_none:
            source.addItem("不同步记忆，直接打开", None)
        for snapshot in snapshots:
            source.addItem(f"{snapshot.get('label', snapshot.get('account_id', '账号'))} · {snapshot.get('file_count', 0)} 个文件 · {display_time(snapshot.get('captured_at'))}", snapshot.get("account_id"))
        layout.addWidget(source)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        controls.button(QDialogButtonBox.StandardButton.Ok).setText("继续")
        controls.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        controls.accepted.connect(dialog.accept)
        controls.rejected.connect(dialog.reject)
        layout.addWidget(controls)
        if not snapshots:
            layout.addWidget(label("还没有账号记忆快照。先选择账号并保存当前共享记忆。", "muted", True))
        if dialog.exec() != QDialog.DialogCode.Accepted or source.count() == 0:
            return False, None
        return True, source.currentData()

    def _launch_with_memory(self, ids):
        accepted, source = self._choose_memory_source("打开实例：选择记忆同步方式")
        if not accepted:
            return
        if source is None:
            self.job("正在打开所选 TRAE 实例…", lambda: [self.controller.launch_account(i) for i in ids])
            return
        self._preview_and_sync_memory(source, ids, launch=True)

    def sync_account_memory(self):
        account = self._one_account()
        if not account:
            return
        accepted, source = self._choose_memory_source("同步账号记忆", allow_none=False)
        if accepted and source:
            self._preview_and_sync_memory(source, [account["id"]], launch=False)

    def _preview_and_sync_memory(self, source, ids, launch=False):
        def ready(previews):
            summaries = [self._memory_summary(result) for result in previews]
            text = "\n\n".join(summaries)
            if not self.confirm("确认合并记忆", text[:14000] + "\n\n将把所选快照合并到本机共享记忆目录，现有内容按后端冲突规则保留并备份。其他 TRAE 实例也可能受影响。确认后继续" + ("并打开实例？" if launch else "？")):
                return

            def operation():
                result = []
                for account_id in ids:
                    result.append(self.controller.sync_memory(source, account_id))
                    if launch:
                        self.controller.launch_account(account_id)
                return result

            self.job("正在合并共享记忆并保留备份…", operation,
                     None if launch else lambda _: self.message("记忆已合并", "所选快照已合并，原文件备份已保留。"))

        self.job("正在预览记忆变更…", lambda: [self.controller.preview_memory_sync(source, i) for i in ids], ready, refresh=False)

    @staticmethod
    def _memory_summary(result):
        lines = [f"来源：{result.get('source_count', 0)} 个记忆文件；本机：{result.get('target_count', 0)} 个。",
                 f"预计新增 {result.get('add_count', 0)} 个文件，合并更新 {result.get('changed_count', 0)} 个，跳过 {result.get('duplicates', 0)} 个重复段落。"]
        for row in result.get("details", []):
            action = {"add": "新增", "change": "合并更新", "unchanged": "保持"}.get(row.get("action"), "检查")
            lines.append(f"{action}  {row.get('relative', '')}")
        return "\n".join(lines)

    def restore_memory_history(self):
        def ready(records):
            if not records:
                self.message("暂无记忆历史", "每次同步记忆前会自动保存共享记忆的加密历史版本。")
                return
            titles = [f"{display_time(row.get('saved_at'))} · {row.get('file_count', 0)} 个文件" for row in records]
            item, ok = QInputDialog.getItem(self, "恢复记忆历史", "选择共享记忆历史版本", titles, len(titles) - 1, False)
            if not ok:
                return
            record = records[titles.index(item)]
            if self.confirm("恢复本机共享记忆", "将共享记忆恢复到所选历史版本，恢复后新增加的记忆文件可能被移除。当前状态会先保存为加密历史。其他已打开的 TRAE 实例也可能受影响。确认恢复？"):
                self.job("正在保存当前状态并恢复记忆…", lambda: self.controller.restore_memory_history(record["index"]),
                         lambda _: self.message("记忆历史已恢复", "已恢复所选版本；恢复前的内容仍保存在加密历史中。"))

        self.job("正在读取加密记忆历史…", self.controller.memory_history, ready, refresh=False)

    def edit_account(self):
        account = self._one_account()
        if not account:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑账号")
        dialog.setMinimumWidth(440)
        layout = QFormLayout(dialog)
        name = QLineEdit(account.get("label", ""))
        notes = QPlainTextEdit(account.get("notes", ""))
        notes.setMaximumHeight(140)
        layout.addRow("名称", name)
        layout.addRow("备注", notes)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and name.text().strip():
            self.job("正在保存账号…", lambda: self.controller.edit_account(account["id"], name.text().strip(), notes.toPlainText()))

    def remove_accounts(self):
        ids = self._account_selection()
        if not ids or not self.confirm("移除账号", f"从管理器移除所选的 {len(ids)} 个账号？此操作不会注销官方账号。"):
            return
        self.job("正在移除账号…", lambda: [self.controller.remove_account(i) for i in ids])

    def checkin(self, ids=None, quiet=False):
        if ids == []:
            return
        if not self.controller.is_unlocked:
            return
        if ids is None and not self.data.get("accounts"):
            self.message("尚无账号", "请先检测或添加账号，并完成官方登录。")
            return

        def finished(results):
            lines = self._checkin_result_lines(results)
            if quiet:
                summary = lines[0] if lines else "启动签到完成；请在操作日志查看结果。"
                self.status_label.setText(summary)
                if self.tray.isVisible() and lines:
                    self.tray.showMessage(APP_NAME, summary, QSystemTrayIcon.MessageIcon.Information, 4500)
            else:
                self.message("签到结果", "\n".join(lines) if lines else "已执行签到，请在操作日志查看每个账号的结果。")

        self.job("正在逐个账号签到…", lambda: self.controller.checkin(ids), finished)

    @staticmethod
    def _checkin_result_lines(results):
        """A batch is successful only for accounts actually confirmed today."""
        if not isinstance(results, list):
            return ["未取得签到结果，请刷新后查看。"]
        groups = {"success": [], "already": [], "unavailable": [], "failed": []}
        for result in results:
            if not isinstance(result, dict):
                groups["failed"].append("账号")
                continue
            state = result.get("state")
            key = state if state in groups and state != "failed" else "failed"
            groups[key].append(str(result.get("label") or "账号"))
        captions = {"success": "本次成功", "already": "此前已签", "unavailable": "暂未开放", "failed": "未完成"}
        summary = "签到完成：" + " · ".join(f"{captions[key]} {len(groups[key])}" for key in groups)
        lines = [summary]
        for key in groups:
            if groups[key]:
                lines.append(captions[key] + "：" + "、".join(groups[key]))
        for result in results:
            if isinstance(result, dict) and result.get("state") not in {"success", "already"}:
                lines.append(str(result.get("label") or "账号") + "：" + text_value(result.get("message"), "请刷新或同步登录后重试。"))
        return lines

    def refresh_usage(self, ids=None):
        if ids == []:
            return
        self.job("正在读取官方额度信息…", lambda: self.controller.refresh_usage(ids),
                 lambda result: self.message("额度已刷新", "\n".join(self._result_lines(result)) or "额度读取完成。"))

    @staticmethod
    def _result_lines(results):
        if not isinstance(results, list):
            return [text_value(results)]
        lines = []
        for result in results:
            if isinstance(result, dict):
                name = result.get("label") or result.get("account") or result.get("id") or "账号"
                message = result.get("message") or result.get("detail") or result.get("status") or result.get("result")
                lines.append(f"{name}：{text_value(message, '已处理')}")
            else:
                lines.append(str(result))
        return lines

    def _project_paths(self):
        value = self._view_cache.get("projects", [])
        return [str(v.get("path", v.get("directory", ""))) if isinstance(v, dict) else str(v) for v in value]

    def _scope_controls(self, kind, include_profiles=False):
        row = QHBoxLayout()
        row.addWidget(label("配置范围", "muted"))
        combo = ComboBox()
        combo.setMinimumWidth(330)
        combo.addItem("官方默认实例" if include_profiles else "全局共享配置", "global")
        if include_profiles:
            for account in self.data.get("accounts", []):
                if account.get("profile_path"):
                    combo.addItem("实例 · " + account.get("label", account["id"]), "profile:" + str(account["profile_path"]))
        for path in self._project_paths():
            if path:
                combo.addItem("项目 · " + path, "project:" + path)
        saved = self._scopes.get(kind, "global")
        index = combo.findData(saved)
        combo.setCurrentIndex(max(0, index))
        self._scopes[kind] = combo.currentData()

        def changed(_):
            self._scopes[kind] = combo.currentData()
            self.refresh()

        combo.currentIndexChanged.connect(changed)
        row.addWidget(combo, 1)
        row.addWidget(button("添加项目", self.add_project))
        if str(combo.currentData()).startswith("project:"):
            row.addWidget(button("移除项目", lambda: self.remove_project(str(combo.currentData())[8:])))
        return row

    def _scope_project(self, kind):
        scope = self._scopes.get(kind, "global")
        return scope[8:] if scope.startswith("project:") else None

    def _mcp_path(self):
        scope = self._scopes.get("mcp", "global")
        if scope.startswith("profile:"):
            return self.controller.configs.mcp_path(profile=scope[8:])
        return self.controller.configs.mcp_path(project=self._scope_project("mcp"))

    def add_project(self):
        path = QFileDialog.getExistingDirectory(self, "选择项目目录")
        if path:
            self.job("正在添加项目…", lambda: self.controller.add_project(path))

    def remove_project(self, path):
        if self.confirm("移除项目", "从管理器列表移除此项目？项目内的配置文件将保留。"):
            self.job("正在移除项目…", lambda: self.controller.remove_project(path))

    def _mcp_page(self):
        page, layout = self._content()
        layout.addLayout(self._scope_controls("mcp", True))
        path = self._view_cache.get("mcp_path") or self._mcp_path()
        self.current_mcp_path = path
        rows = self._view_cache.get("mcp", [])
        self.mcp_entries = rows
        toolbar = QHBoxLayout()
        search = QLineEdit(getattr(self, "_mcp_search", ""))
        search.setPlaceholderText("搜索服务名称或类型")
        search.setClearButtonEnabled(True)
        self.mcp_search = search
        toolbar.addWidget(search, 1)
        state_filter = ComboBox()
        state_filter.addItems(["全部状态", "已启用", "已停用"])
        state_filter.setCurrentIndex(getattr(self, "_mcp_filter", 0))
        self.mcp_status_filter = state_filter
        toolbar.addWidget(state_filter)
        toolbar.addWidget(button("添加服务", lambda: self.edit_mcp(False), True))
        import_button = button("导入")
        import_menu = QMenu(import_button)
        import_menu.addAction("从 JSON 文件…", self.import_mcp)
        import_menu.addAction("从网址…", self.import_mcp_url)
        import_menu.addAction("查找文件夹中的配置…", self.import_mcp_folder)
        import_button.setMenu(import_menu)
        toolbar.addWidget(import_button)
        more_button = button("更多")
        more_menu = QMenu(more_button)
        delete_action = more_menu.addAction("删除所选服务…", self.delete_mcp)
        more_menu.addSeparator()
        more_menu.addAction("导出当前配置…", self.export_mcp)
        more_menu.addAction("复制到其他范围…", self.copy_mcp)
        more_menu.addAction("打开配置目录", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent))))
        bulk_menu = more_menu.addMenu("批量管理")
        selected_actions = []
        for text, enabled in [("启用所选", True), ("停用所选", False), ("删除所选…", None)]:
            action = bulk_menu.addAction(text)
            action.triggered.connect(lambda _checked=False, value=enabled: self.batch_mcp(value))
            selected_actions.append(action)
        bulk_menu.addSeparator()
        for text, enabled in [("启用", True), ("停用", False), ("删除", None)]:
            action = bulk_menu.addAction(f"{text}当前范围全部（{len(rows)} 项）…")
            action.triggered.connect(lambda _checked=False, value=enabled: self.batch_mcp(value, all_items=True))
            action.setEnabled(bool(rows))
        more_button.setMenu(more_menu)
        toolbar.addWidget(more_button)
        layout.addLayout(toolbar)
        count_label = label("", "muted")
        self.mcp_count_label = count_label
        layout.addWidget(count_label)
        table = make_table(["服务名称", "状态", "类型"],
                           [[r["name"], "启用" if r.get("enabled", True) else "停用", r.get("transport")] for r in rows],
                           [r["name"] for r in rows])
        self.mcp_table = table
        table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.SortOrder.AscendingOrder)
        layout.addWidget(table, 1)
        detail, detail_layout = panel()
        detail_title = label("", "sectionTitle")
        detail_info = label("", "muted", True)
        detail_path = label(str(path), "muted", True)
        detail_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(detail_title)
        detail_layout.addWidget(detail_info)
        detail_layout.addWidget(detail_path)
        controls = QHBoxLayout()
        edit = button("编辑配置", lambda: self.edit_mcp(True))
        toggle = button("启用 / 停用", self.toggle_mcp)
        probe = button("检测连接", self.probe_mcp)
        for control in (edit, toggle, probe):
            controls.addWidget(control)
        controls.addStretch()
        detail_layout.addLayout(controls)
        self.mcp_detail = detail
        self.mcp_actions = {"edit": edit, "toggle": toggle, "probe": probe, "delete": delete_action,
                            "enable_selected": selected_actions[0], "disable_selected": selected_actions[1],
                            "delete_selected": selected_actions[2]}
        layout.addWidget(detail)

        def update_selection():
            names = selected_keys(table)
            single, has_selection = len(names) == 1, bool(names)
            for control in (edit, toggle, probe, delete_action):
                control.setEnabled(single)
            for action in selected_actions:
                action.setEnabled(has_selection)
            detail.setVisible(single)
            if single:
                row = next((entry for entry in rows if entry["name"] == names[0]), {})
                detail_title.setText(row.get("name", "MCP"))
                detail_info.setText(f"{text_value(row.get('transport'))} · {'已启用' if row.get('enabled', True) else '已停用'}；编辑可查看完整配置。")

        def filter_rows(*_):
            text = search.text().casefold()
            self._mcp_search = search.text()
            self._mcp_filter = state_filter.currentIndex()
            entries = {entry["name"]: entry for entry in rows}
            visible, hidden_selection = 0, False
            for index in range(table.rowCount()):
                row = entries.get(table.item(index, 0).data(Qt.ItemDataRole.UserRole), {})
                enabled = row.get("enabled", True)
                matches_state = self._mcp_filter == 0 or self._mcp_filter == 1 and enabled or self._mcp_filter == 2 and not enabled
                content = (str(row.get("name", "")) + " " + str(row.get("transport", ""))).casefold()
                shown = text in content and matches_state
                hidden_selection |= not shown and table.item(index, 0).isSelected()
                table.setRowHidden(index, not shown)
                visible += int(shown)
            if hidden_selection:
                table.clearSelection()
            enabled_count = sum(row.get("enabled", True) for row in rows)
            count_label.setText(f"共 {len(rows)} 项 · 启用 {enabled_count} · 停用 {len(rows) - enabled_count} · 显示 {visible}")
            update_selection()

        table.itemSelectionChanged.connect(update_selection)
        table.cellDoubleClicked.connect(lambda *_: self.edit_mcp(True))
        search.textChanged.connect(filter_rows)
        state_filter.currentIndexChanged.connect(filter_rows)
        filter_rows()
        if self._view_cache.get("mcp_error"):
            layout.addWidget(label("配置读取失败，请检查位置或权限后刷新。", "error", True))
        layout.addWidget(label("选中服务后操作；导入和批量管理在顶部菜单中。", "muted", True))
        return page

    @staticmethod
    def _safe_url(value):
        from urllib.parse import urlsplit, urlunsplit
        try:
            parts = urlsplit(str(value))
            host = parts.hostname or ""
            if parts.port:
                host += f":{parts.port}"
            return urlunsplit((parts.scheme, host, parts.path, "", ""))
        except (ValueError, TypeError):
            return "地址格式有误"

    def _selected_mcp(self):
        names = selected_keys(self.mcp_table)
        if len(names) != 1:
            self.message("选择一个 MCP", "请在列表中选择一个服务。")
            return None
        return next((row for row in self.mcp_entries if row["name"] == names[0]), None)

    def edit_mcp(self, existing):
        row = self._selected_mcp() if existing else None
        if existing and not row:
            return
        initial = row.get("config", {}) if row else {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/your/project"]}
        dialog = TextEditor("编辑 MCP 服务" if existing else "添加 MCP 服务", row["name"] if row else "", json.dumps(initial, ensure_ascii=False, indent=2), True, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.name.text().strip()
        config = json.loads(dialog.editor.toPlainText())
        path = self.current_mcp_path
        if row and name != row["name"]:
            self.message("保留服务名称", "编辑已有服务时请保留名称。若需改名，请新建后删除原服务。")
            return
        self.job("正在保存 MCP 配置…", lambda: self.controller.configs.upsert_mcp(path, name, config))

    def toggle_mcp(self):
        row = self._selected_mcp()
        if row:
            path = self.current_mcp_path
            self.job("正在切换 MCP 状态…", lambda: self.controller.configs.set_mcp_enabled(path, row["name"], not row.get("enabled", True)))

    def delete_mcp(self):
        row = self._selected_mcp()
        if row and self.confirm("删除 MCP", f"删除服务「{row['name']}」？原配置将保留备份。"):
            path = self.current_mcp_path
            self.job("正在删除 MCP…", lambda: self.controller.configs.delete_mcp(path, row["name"]))

    def probe_mcp(self):
        row = self._selected_mcp()
        if not row:
            return
        if row.get("config", {}).get("command") and not self.confirm("连接检测", "连接检测将运行该 MCP 配置中的命令。确认此服务来源可信并继续？"):
            return
        self.job("正在检测 MCP 连接…", lambda: self.controller.configs.probe_mcp(row["config"]),
                 lambda result: self.message("连接检测", self._mcp_probe_summary(result)), refresh=False)

    @staticmethod
    def _mcp_probe_summary(result):
        if result.get("ok") or result.get("success"):
            count = result.get("tool_count", 0)
            return f"连接成功，发现 {count} 个工具。"
        return "连接未成功。请检查启动依赖、权限或网络后重试。"

    def import_mcp(self):
        source, _ = QFileDialog.getOpenFileName(self, "导入 MCP 配置", "", "JSON 配置 (*.json);;所有文件 (*)")
        if not source:
            return
        if not self.confirm("导入 MCP 配置", "将所选 JSON 合并到当前范围？现有配置将自动备份；重名服务会停止导入并保留原配置。"):
            return
        path = self.current_mcp_path
        self.job("正在导入 MCP…", lambda: self.controller.configs.import_mcp(path, Path(source)))

    def import_mcp_url(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("从网址导入 MCP")
        dialog.setMinimumWidth(610)
        form = QFormLayout(dialog)
        url = QLineEdit()
        url.setPlaceholderText("HTTPS JSON 链接，或包含配置的 trae-cn://mcp-import 链接")
        name = QLineEdit()
        name.setPlaceholderText("单个 MCP 配置必填；mcpServers 文件可留空")
        form.addRow("配置地址", url)
        form.addRow("服务名称", name)
        form.addRow(label("只下载配置数据；不会运行 MCP 命令。导入会备份原配置，重名不会覆盖。", "muted", True))
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        controls.accepted.connect(dialog.accept)
        controls.rejected.connect(dialog.reject)
        form.addRow(controls)
        if dialog.exec() == QDialog.DialogCode.Accepted and url.text().strip():
            address, service_name = url.text().strip(), name.text().strip() or None
            path = self.current_mcp_path
            self.job("正在下载并验证 MCP 配置…", lambda: self.controller.configs.import_mcp_url(path, address, service_name))

    def import_mcp_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择要扫描的 MCP 配置文件夹")
        if not folder:
            return

        def choose(paths):
            if not paths:
                self.message("未找到 MCP 配置", "在所选文件夹四层以内没有找到常见 MCP JSON 配置。可使用「导入 JSON」直接选择文件。")
                return
            item, ok = QInputDialog.getItem(self, "选择配置文件", "从扫描结果中选择要导入的文件", [str(p) for p in paths], 0, False)
            if ok and self.confirm("导入扫描到的配置", f"将以下文件合并到当前范围？原文件会备份；同名服务不会覆盖。\n{item}"):
                path = self.current_mcp_path
                self.job("正在导入 MCP 配置…", lambda: self.controller.configs.import_mcp(path, Path(item)))

        self.job("正在扫描所选 MCP 配置文件夹…", lambda: self.controller.configs.list_mcp_files(Path(folder)), choose, refresh=False)

    def batch_mcp(self, enabled, *, all_items=False):
        names = [r["name"] for r in self.mcp_entries] if all_items else selected_keys(self.mcp_table)
        if not names:
            self.message("请先选择 MCP", "在列表中选择要操作的服务；不会自动操作全部服务。")
            return
        action = "删除" if enabled is None else ("启用" if enabled else "停用")
        if not self.confirm(action + ("全部" if all_items else "所选") + " MCP 服务", f"{action}{'当前范围全部' if all_items else '所选'} {len(names)} 个 MCP 服务？变更前会保留备份。"):
            return
        if all_items and enabled is None and not self.confirm("再次确认删除全部 MCP", f"将删除当前范围全部 {len(names)} 个 MCP 服务。确认继续？"):
            return
        path = self.current_mcp_path
        fn = (lambda: self.controller.configs.delete_mcp_servers(path, names)) if enabled is None else (lambda: self.controller.configs.set_mcp_servers_enabled(path, names, enabled))
        self.job("正在批量" + action + " MCP…", fn)

    def export_mcp(self):
        destination, _ = QFileDialog.getSaveFileName(self, "导出 MCP 配置", "mcp.json", "JSON 配置 (*.json)")
        if not destination:
            return
        choice = QMessageBox(self)
        choice.setWindowTitle("导出 MCP 配置")
        choice.setText("选择导出内容。脱敏导出将移除密钥与参数值，适合分享；保留凭据的 JSON 可直接迁移，但不会加密。")
        redacted = choice.addButton("脱敏导出", QMessageBox.ButtonRole.AcceptRole)
        complete = choice.addButton("保留凭据", QMessageBox.ButtonRole.ActionRole)
        choice.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        choice.setDefaultButton(redacted)
        choice.exec()
        if choice.clickedButton() not in (redacted, complete):
            return
        path = self.current_mcp_path
        include_secrets = choice.clickedButton() == complete
        self.job("正在导出 MCP…", lambda: self.controller.configs.export_mcp(path, self.controller.safe_export_path(destination), include_secrets=include_secrets),
                 lambda _: self.message("已导出", destination), refresh=False)

    def copy_mcp(self):
        source = Path(self.current_mcp_path)
        targets = {"官方默认实例": self.controller.configs.mcp_path()}
        for account in self.data.get("accounts", []):
            if account.get("profile_path"):
                targets["实例 · " + account.get("label", account["id"]) + " · " + str(account["id"])[:8]] = self.controller.configs.mcp_path(profile=account["profile_path"])
        for project in self._project_paths():
            targets["项目 · " + project] = self.controller.configs.mcp_path(project=project)
        targets = {title: path for title, path in targets.items() if Path(path).resolve() != source.resolve()}
        if not targets:
            self.message("暂无其他配置范围", "先添加另一个账号实例或项目，即可复制 MCP 配置。")
            return
        title, ok = QInputDialog.getItem(self, "复制 MCP 配置", "选择目标范围", list(targets), 0, False)
        if not ok:
            return
        target = targets[title]
        if not self.confirm("复制 MCP 配置", f"把当前 MCP 服务合并到「{title}」？目标配置将自动备份；重名服务会停止复制。\n\n目标：{target}"):
            return
        self.job("正在复制 MCP 配置…", lambda: self.controller.configs.import_mcp(target, self.controller.configs.read_mcp(source)))

    def _items_page(self, kind):
        skill = kind == "skills"
        title = "Skill" if skill else "Rule"
        page, layout = self._content()
        layout.addLayout(self._scope_controls(kind))
        project = self._scope_project(kind)
        items = self._view_cache.get("items", {}).get(kind, [])
        self.__dict__[kind + "_entries"] = items
        toolbar = QHBoxLayout()
        saved_filter = self._item_filters[kind]
        search = QLineEdit(saved_filter["text"])
        search.setClearButtonEnabled(True)
        search.setPlaceholderText("搜索名称或用途")
        search.setMinimumWidth(150)
        toolbar.addWidget(search, 1)
        status = ComboBox()
        status.addItem("全部状态", "all")
        status.addItem("仅启用", "enabled")
        status.addItem("仅停用", "disabled")
        status.setCurrentIndex(max(0, status.findData(saved_filter["status"])))
        toolbar.addWidget(status)
        toolbar.addWidget(button("新建 " + title, lambda: self.edit_item(kind, False), True))
        if skill:
            imports = button("导入")
            imports_menu = QMenu(imports)
            for name, fn in [("从本地文件夹…", self.import_skill_directory),
                             ("从 ZIP 压缩包…", self.import_skill_zip),
                             ("从网址 / GitHub…", self.import_skill_github)]:
                action = imports_menu.addAction(name)
                action.triggered.connect(lambda _checked=False, handler=fn: handler())
            imports.setMenu(imports_menu)
            toolbar.addWidget(imports)
        else:
            toolbar.addWidget(button("导入…", self.import_rule))
        translate_all = button("全部翻译")
        translate_all.setMenu(self._batch_translation_menu(kind, translate_all))
        translate_all.setEnabled(bool(items))
        self.__dict__[kind + "_translate_all"] = translate_all
        toolbar.addWidget(translate_all)
        more = button("更多")
        more_menu = QMenu(more)
        actions = {}

        def add_action(menu, key, name, fn):
            action = menu.addAction(name)
            action.triggered.connect(lambda _checked=False: fn())
            actions[key] = action
            return action

        add_action(more_menu, "edit", "编辑内容…", lambda: self.edit_item(kind, True))
        if skill:
            add_action(more_menu, "files", "管理文件与文件夹…", self.skill_resources)
        add_action(more_menu, "toggle", "启用 / 停用", lambda: self.toggle_item(kind))
        add_action(more_menu, "export", "导出所选…", lambda: self.export_item(kind))
        translation_settings = more_menu.addAction("翻译设置…")
        translation_settings.triggered.connect(self.translation_settings_dialog)
        more_menu.addSeparator()
        bulk = more_menu.addMenu("批量管理")
        add_action(bulk, "enable_selected", "启用所选", lambda: self.batch_items(kind, True))
        add_action(bulk, "disable_selected", "停用所选", lambda: self.batch_items(kind, False))
        add_action(bulk, "delete_selected", "删除所选…", lambda: self.batch_items(kind, None))
        bulk.addSeparator()
        add_action(bulk, "enable_all", f"启用当前范围全部（{len(items)} 项）…", lambda: self.batch_items(kind, True, all_items=True))
        add_action(bulk, "disable_all", f"停用当前范围全部（{len(items)} 项）…", lambda: self.batch_items(kind, False, all_items=True))
        add_action(bulk, "delete_all", f"删除当前范围全部（{len(items)} 项）…", lambda: self.batch_items(kind, None, all_items=True))
        if skill and project is None:
            more_menu.addSeparator()
            add_action(more_menu, "storage", "设置 Skills 存储目录…", self.change_skills_root)
        more.setMenu(more_menu)
        toolbar.addWidget(more)
        layout.addLayout(toolbar)
        count = label("", "muted")
        layout.addWidget(count)
        table = make_table(["名称", "状态", "用途"],
                           [[r["name"], "启用" if r.get("enabled", True) else "停用",
                             self.display_purpose(kind, r)] for r in items],
                           [str(r["path"]) for r in items])
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.__dict__[kind + "_table"] = table
        self.__dict__[kind + "_search"] = search
        self.__dict__[kind + "_status_filter"] = status
        self.__dict__[kind + "_count_label"] = count
        self.item_actions[kind] = actions
        table.setSortingEnabled(True)
        table.sortItems(saved_filter.get("sort_column", 0), Qt.SortOrder(saved_filter.get("sort_order", 0)))

        def remember_sort(column, order):
            saved_filter.update(sort_column=column, sort_order=order.value)

        table.horizontalHeader().sortIndicatorChanged.connect(remember_sort)
        table.itemDoubleClicked.connect(lambda *_: self.edit_item(kind, True))
        layout.addWidget(table, 1)
        details, detail_layout = panel()
        detail_layout.setContentsMargins(16, 12, 16, 12)
        detail_title = label("", "sectionTitle")
        detail_description = label("", "muted", True)
        detail_path = label("", "muted", True)
        detail_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(detail_title)
        purpose_row = QHBoxLayout()
        purpose_row.addWidget(detail_description, 1)
        translate = button("翻译")
        translate.setMenu(self._translation_menu(kind, translate))
        purpose_row.addWidget(translate)
        detail_layout.addLayout(purpose_row)
        detail_layout.addWidget(detail_path)
        selection_controls = QHBoxLayout()
        selection_controls.addWidget(button("编辑内容", lambda: self.edit_item(kind, True)))
        toggle = button("启用 / 停用", lambda: self.toggle_item(kind))
        selection_controls.addWidget(toggle)
        if skill:
            selection_controls.addWidget(button("文件与文件夹…", self.skill_resources))
        selection_controls.addStretch()
        detail_layout.addLayout(selection_controls)
        layout.addWidget(details)
        lookup = {str(row["path"]): row for row in items}
        enabled_count = sum(bool(row.get("enabled", True)) for row in items)

        def selection_changed():
            paths = selected_keys(table)
            single = len(paths) == 1
            for key in ("edit", "toggle", "export", "files"):
                if key in actions:
                    actions[key].setEnabled(single)
            for key in ("enable_selected", "disable_selected", "delete_selected"):
                actions[key].setEnabled(bool(paths))
            for key in ("enable_all", "disable_all", "delete_all"):
                actions[key].setEnabled(bool(items))
            details.setVisible(single)
            details.setEnabled(single)
            if single:
                item = lookup[paths[0]]
                detail_title.setText(item["name"])
                detail_description.setText(self.display_purpose(kind, item))
                translate.setEnabled(bool(self._original_purpose(item)) and not item.get("error"))
                detail_path.setText(str(item["path"]))
                toggle.setText("停用" if item.get("enabled", True) else "启用")
            visible = sum(not table.isRowHidden(row) for row in range(table.rowCount()))
            count.setText(f"共 {len(items)} 项  ·  启用 {enabled_count}  ·  停用 {len(items) - enabled_count}  ·  显示 {visible}" + (f"  ·  已选 {len(paths)}" if paths else ""))

        def filter_items(*_):
            text, state_filter = search.text().strip().casefold(), status.currentData()
            saved_filter.update(text=search.text(), status=state_filter)
            table.clearSelection()
            for row in range(table.rowCount()):
                key = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                item = lookup[key]
                content = (" ".join(str(item.get(field, "")) for field in ("name", "description", "summary", "path", "error"))
                           + " " + self.display_purpose(kind, item)).casefold()
                enabled = bool(item.get("enabled", True))
                matches_status = state_filter == "all" or (state_filter == "enabled" and enabled) or (state_filter == "disabled" and not enabled)
                table.setRowHidden(row, text not in content or not matches_status)
            selection_changed()

        table.itemSelectionChanged.connect(selection_changed)
        self.__dict__.setdefault("_purpose_views", {})[kind] = (table, lookup, selection_changed)
        search.textChanged.connect(filter_items)
        status.currentIndexChanged.connect(filter_items)
        filter_items()
        if skill:
            layout.addWidget(label("选中查看，双击编辑。全局技能供所有账号使用。", "muted", True))
        else:
            layout.addWidget(label("全局规则供所有账号使用；项目规则仅用于当前项目。", "muted", True))
        if self._view_cache.get(kind + "_error"):
            layout.addWidget(label("当前目录读取失败，请检查位置和权限后刷新。", "error", True))
        return page

    def skill_resources(self):
        skill = self._selected_item("skills")
        if not skill:
            return
        dialog = SkillResourcesDialog(self, skill)
        dialog.finished.connect(lambda _: self.refresh())
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.show()
        dialog.load_tree()

    def batch_items(self, kind, enabled, *, all_items=False):
        paths = ([str(r["path"]) for r in self.__dict__[kind + "_entries"]] if all_items
                 else selected_keys(self.__dict__[kind + "_table"]))
        if not paths:
            self.message("请先选择条目", "在列表中选择要操作的条目；不会自动操作全部内容。")
            return
        action = "删除" if enabled is None else ("启用" if enabled else "停用")
        scope = "当前配置范围全部" if all_items else "所选"
        if not self.confirm(action + ("全部 " if all_items else "所选 ") + kind, f"{action}{scope} {len(paths)} 个条目及其资源？操作前会保存备份。" + ("\n\n该操作包括被搜索或状态筛选隐藏的条目。" if all_items else "")):
            return
        if all_items and enabled is None and not self.confirm("再次确认删除全部", f"即将删除当前配置范围全部 {len(paths)} 个条目及其文件夹内容。确认继续？"):
            return
        locations = [Path(path) for path in paths]
        operation = (lambda: self.controller.configs.delete_items(kind, locations)) if enabled is None else (lambda: self.controller.configs.set_items_enabled(kind, locations, enabled))
        self.job("正在批量" + action + "条目…", operation)

    def change_skills_root(self):
        choice, ok = QInputDialog.getItem(self, "Skills 存储位置", "选择存储方式", ["选择自定义目录", "恢复 TRAE 默认目录"], 0, False)
        if not ok:
            return
        destination = None
        if choice == "选择自定义目录":
            destination = QFileDialog.getExistingDirectory(self, "选择全局 Skills 存储目录")
            if not destination:
                return
        text = destination or "TRAE 全局配置目录下的 skills"
        if not self.confirm("迁移全局 Skills", f"将现有全局 Skills 备份并迁移至：\n{text}\n\n自定义目录会通过目录联接供 TRAE 从默认位置读取。所有账号共享此设置；同名冲突会停止迁移。请先关闭 TRAE 后继续。"):
            return
        self.job("正在备份、迁移 Skills 存储并验证加载路径…", lambda: self.controller.set_skills_root(destination),
                 lambda result: self.message("Skills 位置已更新", self._format_result(result)))

    def _selected_item(self, kind):
        paths = selected_keys(self.__dict__[kind + "_table"])
        if len(paths) != 1:
            self.message("选择一个条目", "请在列表中选择一个条目。")
            return None
        return next((r for r in self.__dict__[kind + "_entries"] if str(r["path"]) == paths[0]), None)

    def edit_item(self, kind, existing):
        row = self._selected_item(kind) if existing else None
        if existing and not row:
            return
        if row:
            self.job("正在读取配置内容…", lambda: self.controller.configs.read_item(row["path"]),
                     lambda content: self._edit_item_text(kind, row, str(content)), refresh=False)
            return
        else:
            content = "---\nname: my-skill\ndescription: 说明这个 Skill 的用途与使用时机\n---\n\n# 使用说明\n\n在这里编写具体步骤。\n" if kind == "skills" else "# 项目规则\n\n在这里编写工作约定。\n"
        self._edit_item_text(kind, None, content)

    def _edit_item_text(self, kind, row, content):
        name = row["name"] if row else ""
        noun = "Skill" if kind == "skills" else "规则"
        dialog = TextEditor(("编辑" if row else "新建") + noun, name, str(content), False, self, allow_subdirs=kind == "rules")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_name = dialog.name.text().strip()
        if row and new_name != name:
            self.message("保留名称", "编辑已有条目时请保留名称。若需改名，请新建后删除原条目。")
            return
        project = self._scope_project(kind)
        text = dialog.editor.toPlainText()
        if row and kind == "skills":
            operation = lambda: self.controller.configs.save_skill_file(Path(row["path"]), "SKILL.md", text)
        elif row and not row.get("enabled", True) and not hasattr(self.controller.configs, "save_existing_item"):
            self.message("先启用再编辑", "此规则目前停用。请先启用后编辑；编辑完成后可再停用。")
            return
        elif row and hasattr(self.controller.configs, "save_existing_item"):
            operation = lambda: self.controller.configs.save_existing_item(kind, Path(row["path"]), text)
        else:
            operation = lambda: self.controller.configs.save_item(kind, new_name, text, project=project)
        self.job("正在保存内容…", operation)

    def toggle_item(self, kind):
        row = self._selected_item(kind)
        if row:
            self.job("正在切换启用状态…", lambda: self.controller.configs.toggle_item(kind, row["path"]))

    def remove_item(self, kind):
        row = self._selected_item(kind)
        if row and self.confirm("删除条目", f"删除「{row['name']}」及其内容？此操作会按配置备份规则保留备份。"):
            self.job("正在删除条目…", lambda: self.controller.configs.remove_item(kind, row["path"]))

    def import_skill_directory(self):
        source = QFileDialog.getExistingDirectory(self, "选择包含 SKILL.md 的目录")
        if source:
            self._import_skill(source)

    def import_rule(self):
        source, _ = QFileDialog.getOpenFileName(self, "导入规则文件", "", "Markdown 规则 (*.md)")
        if not source:
            return
        project = self._scope_project("rules")

        def operation():
            configs = self.controller.configs
            name = Path(source).name
            if any(row["name"].casefold() == name.casefold() for row in configs.list_items("rules", project=project)):
                raise ValueError("当前范围已有同名规则；请编辑原规则，或为导入文件改名后重试。")
            text = configs.read_item(Path(source))
            return configs.save_item("rules", name, text, project=project)

        self.job("正在校验并导入规则…", operation)

    def import_skill_zip(self):
        source, _ = QFileDialog.getOpenFileName(self, "导入 Skill ZIP", "", "ZIP 压缩包 (*.zip)")
        if source:
            self._import_skill(source)

    def _import_skill(self, source):
        project = self._scope_project("skills")
        self.job("正在校验并导入 Skill…", lambda: self.controller.configs.import_skill(Path(source), project=project))

    def import_skill_github(self):
        url, ok = QInputDialog.getText(self, "从网址导入 Skill", "HTTPS 公开 GitHub 仓库 / 技能目录、ZIP 或 SKILL.md 地址")
        if ok and url.strip():
            project = self._scope_project("skills")
            self.job("正在下载并校验 Skill…", lambda: self.controller.configs.import_skill_url(url.strip(), project=project),
                     lambda _: self.message("导入完成", "已加入列表。单个 Markdown 链接仅导入主文件。"))

    def export_item(self, kind):
        row = self._selected_item(kind)
        if not row:
            return
        is_skill = kind == "skills"
        filename = Path(row["name"]).name + ".zip" if is_skill else Path(row["name"]).name
        if not is_skill and not filename.lower().endswith(".md"):
            filename += ".md"
        destination, _ = QFileDialog.getSaveFileName(self, "导出 Skill" if is_skill else "导出 Rule",
                                                    filename,
                                                    "ZIP 压缩包 (*.zip)" if is_skill else "Markdown (*.md)")
        if not destination:
            return

        def export_file():
            self.controller.configs.export_item(Path(row["path"]), self.controller.safe_export_path(destination))

        self.job("正在导出条目…", export_file, lambda _: self.message("已导出", destination), refresh=False)

    def _backup_page(self):
        page, layout = self._content()
        card, cl = panel()
        cl.addWidget(label("把账号带到下一台电脑", "sectionTitle"))
        cl.addWidget(label("使用独立迁移密码导出加密备份。迁移密码无需与本机解锁密码一致；请至少使用 8 个字符。", "muted", True))
        cl.addWidget(label("备份包含敏感登录状态和管理配置，请只导入可信来源的备份。导入后可再次检测本机安装与账号登录状态。", "muted", True))
        actions = QHBoxLayout()
        actions.addWidget(button("导出加密迁移备份", self.export_backup, True))
        actions.addWidget(button("导入加密迁移备份", self.import_backup))
        actions.addStretch()
        cl.addLayout(actions)
        cl.addWidget(button("恢复已导入备份的配置", self.restore_backup_configs))
        cl.addWidget(button("合并迁移备份中的原生记忆", self.merge_imported_memory))
        cl.addWidget(button("本机配置变更历史", self.config_history))
        cl.addWidget(label("导入首先合并账号；随后可单独恢复全局与独立实例配置。已有文件会跳过，项目配置需要指定本机目标目录。", "muted", True))
        layout.addWidget(card)
        info, il = panel()
        il.addWidget(label("本机保险库与后台签到", "sectionTitle"))
        il.addWidget(label("保险库使用解锁密码加密。启用后台计划任务时，后台所需解密材料由当前 Windows 用户的系统加密保护，用于管理器关闭后的定时签到。迁移备份始终使用你设置的迁移密码。", "muted", True))
        il.addWidget(label("修改本机解锁密码不会更改已导出的备份密码。忘记解锁密码时，可使用已知迁移密码的备份恢复到新的保险库。", "muted", True))
        layout.addWidget(info)
        layout.addStretch()
        return scroll(page)

    def merge_imported_memory(self):
        if not self.confirm("合并迁移记忆", "将备份中的原生记忆合并到本机共享记忆，保留现有内容。其他已打开的 TRAE 窗口也可能读取到。合并前会保存加密历史，是否继续？"):
            return
        self.job("正在合并迁移记忆…", self.controller.merge_imported_memory,
                 lambda result: self.message("记忆迁移", result.get("message", self._format_result(result))))

    def export_backup(self):
        destination, _ = QFileDialog.getSaveFileName(self, "导出加密备份", "Trae-CN-Backup-" + cn_datetime().strftime("%Y%m%d") + ".tcmbackup", "加密备份 (*.tcmbackup)")
        if not destination:
            return
        dialog = PasswordDialog("设置迁移密码", "请为此备份设置一个独立密码。忘记密码将无法恢复此备份。", True, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        password = dialog.password.text()
        dialog.password.clear()
        dialog.repeat.clear()
        self.job("正在加密并导出备份…", lambda: self.controller.backup_export(destination, password),
                 lambda _: self.message("备份已导出", destination), refresh=False)

    def import_backup(self):
        source, _ = QFileDialog.getOpenFileName(self, "选择加密迁移备份", "", "加密备份 (*.tcmbackup);;所有文件 (*)")
        if not source:
            return
        dialog = PasswordDialog("解密迁移备份", "输入导出此备份时设置的迁移密码。导入将合并账号，不覆盖相同 ID 的账号。", False, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if not self.confirm("导入备份", "确认导入该来源可信的备份并合并账号与配置？建议先导出当前备份。"):
            return
        password = dialog.password.text()
        dialog.password.clear()
        self.job("正在验证并导入加密备份…", lambda: self.controller.backup_import(source, password),
                 lambda result: self.message("导入完成", self._format_result(result) if result else "账号已合并。请检查实例位置并同步登录状态。"))

    def restore_backup_configs(self):
        if not self.confirm("恢复迁移配置", "请先关闭目标 TRAE 实例，再恢复迁移配置。只恢复不存在的文件；已有文件与尚未指定目标的项目会跳过。是否继续？"):
            return
        projects = self.controller.pending_projects()
        mapping = {}
        if projects:
            dialog = QDialog(self)
            dialog.setWindowTitle("为迁移项目选择本机目录")
            dialog.resize(760, min(650, 220 + len(projects) * 72))
            layout = QVBoxLayout(dialog)
            layout.addWidget(label("为需要恢复的项目选择本机目录；留空的项目将跳过。配置会写入目标项目的 .trae 目录。", "muted", True))
            content = QWidget()
            fields = QFormLayout(content)
            edits = {}
            for project in projects:
                owner = project["owner"]
                row = QHBoxLayout()
                edit = QLineEdit()
                edit.setPlaceholderText("选择本机项目目录，留空跳过")
                row.addWidget(edit, 1)

                def browse(target=edit):
                    chosen = QFileDialog.getExistingDirectory(dialog, "选择目标项目目录")
                    if chosen:
                        target.setText(chosen)

                row.addWidget(button("浏览…", browse))
                name = label(str(project.get("label") or owner), wrap=True)
                name.setMaximumWidth(210)
                fields.addRow(name, row)
                edits[owner] = edit
            layout.addWidget(scroll(content), 1)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("恢复所选配置")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            mapping = {owner: edit.text().strip() for owner, edit in edits.items() if edit.text().strip()}
            if any(not Path(path).is_dir() for path in mapping.values()):
                self.message("目标目录无效", "请为需要恢复的项目选择已存在的文件夹。")
                return
        self.job("正在恢复迁移配置…", lambda: self.controller.restore_backup_configs(mapping),
                 lambda result: self.message("配置恢复结果", result.get("message", self._format_result(result))))

    def config_history(self):
        def show_history(records):
            dialog = QDialog(self)
            dialog.setWindowTitle("本机配置变更历史")
            dialog.resize(920, 540)
            layout = QVBoxLayout(dialog)
            layout.addWidget(label("选择一份配置备份即可回滚。恢复前会自动备份当前内容。", "muted", True))
            table = make_table(["备份时间", "名称", "原配置位置"],
                               [[display_time(r.get("time")), r.get("name"), r.get("original_path")] for r in records],
                               [r["path"] for r in records])
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            layout.addWidget(table, 1)

            def restore():
                paths = selected_keys(table)
                if len(paths) != 1:
                    self.message("选择一份备份", "请先在历史列表中选择一份配置备份。")
                    return
                record = next(r for r in records if r["path"] == paths[0])
                if not self.confirm("恢复本机配置", "请保存工作并关闭相关 TRAE 实例。恢复会替换原配置，当前内容将先自动备份。\n\n目标：" + str(record.get("original_path", ""))):
                    return
                dialog.accept()
                self.job("正在备份当前配置并恢复历史…", lambda: self.controller.restore_config_backup(paths[0]),
                         lambda result: self.message("配置已恢复", result.get("message", "恢复完成。")))

            controls = QHBoxLayout()
            controls.addStretch()
            controls.addWidget(button("恢复所选配置", restore, True))
            controls.addWidget(button("关闭", dialog.reject))
            layout.addLayout(controls)
            if not records:
                layout.addWidget(label("尚无配置变更备份。修改、启停或删除配置后，备份会出现在这里。", "muted", True))
            dialog.exec()
        self.job("正在读取配置备份历史…", self.controller.config_history, show_history, refresh=False)

    def _settings_page(self):
        page, layout = self._content()
        settings = self.data.get("settings", {})
        card, cl = panel()
        cl.addWidget(label("签到与启动", "sectionTitle"))
        self.setting_auto = CheckBox("每次解锁后自动签到（当天已完成的账号会跳过）")
        self.setting_auto.setChecked(settings.get("auto_checkin", True))
        cl.addWidget(self.setting_auto)
        self.setting_scheduled = CheckBox("启用后台每日签到（任务计划 / 当前用户启动项）")
        self.setting_scheduled.setChecked(settings.get("scheduled_enabled", False))
        cl.addWidget(self.setting_scheduled)
        time_row = QHBoxLayout()
        time_row.addWidget(label("北京时间"))
        self.setting_time = TimeEdit()
        self.setting_time.setDisplayFormat("HH:mm")
        initial_time = QTime.fromString(settings.get("schedule_time", "09:00"), "HH:mm")
        self.setting_time.setTime(initial_time if initial_time.isValid() else QTime(9, 0))
        time_row.addWidget(self.setting_time)
        time_row.addStretch()
        cl.addLayout(time_row)
        self.setting_start = CheckBox("登录 Windows 时启动管家")
        self.setting_start.setChecked(settings.get("start_at_login", False))
        cl.addWidget(self.setting_start)
        cl.addWidget(label("关机期间无法执行任务；重新开机并登录 Windows 后补跑当天签到。若系统拒绝创建计划任务，会改用当前用户开机启动的独立后台进程，管家窗口退出后仍可运行。后台密钥由当前 Windows 用户加密保护。", "muted", True))
        backend = {"task": "Windows 计划任务", "startup": "当前用户启动项 + 独立后台进程", "off": "未启用"}.get(settings.get("schedule_backend", "off"), "待检查")
        cl.addWidget(label("当前后台方式：" + backend, "muted", True))
        layout.addWidget(card)
        usage, ul = panel()
        ul.addWidget(label("积分与套餐同步", "sectionTitle"))
        self.setting_usage_auto = CheckBox("自动刷新所有账号的官方积分、签到额度和套餐信息")
        self.setting_usage_auto.setChecked(settings.get("usage_auto_refresh", True))
        ul.addWidget(self.setting_usage_auto)
        interval = QHBoxLayout()
        interval.addWidget(label("刷新间隔"))
        self.setting_usage_seconds = SpinBox()
        self.setting_usage_seconds.setRange(5, 300)
        self.setting_usage_seconds.setSuffix(" 秒")
        self.setting_usage_seconds.setValue(int(settings.get("usage_refresh_seconds", 15)))
        interval.addWidget(self.setting_usage_seconds)
        interval.addStretch()
        ul.addLayout(interval)
        ul.addWidget(label("默认每 15 秒读取官方结果；签到完成后也会更新。断网或登录过期时保留旧值并标记，成功时间显示在账号页。平台结算延迟会影响到账速度。", "muted", True))
        layout.addWidget(usage)
        appearance, al = panel()
        al.addWidget(label("外观、安全与版本", "sectionTitle"))
        form = QFormLayout()
        self.setting_theme = ComboBox()
        self.setting_theme.addItem("深色", "dark")
        self.setting_theme.addItem("浅色", "light")
        self.setting_theme.setCurrentIndex(1 if settings.get("theme") == "light" else 0)
        form.addRow("界面主题", self.setting_theme)
        self.setting_lock = SpinBox()
        self.setting_lock.setRange(0, 240)
        self.setting_lock.setSuffix(" 分钟")
        self.setting_lock.setSpecialValueText("不自动锁定")
        self.setting_lock.setValue(int(settings.get("lock_minutes", 15) or 0))
        form.addRow("闲置自动锁定", self.setting_lock)
        self.setting_repo = QLineEdit(settings.get("github_repository", ""))
        self.setting_repo.setPlaceholderText("owner/repository，上传 GitHub 后填写")
        form.addRow("本项目 GitHub 仓库", self.setting_repo)
        al.addLayout(form)
        controls = QHBoxLayout()
        controls.addWidget(button("保存设置", self.save_settings, True))
        controls.addWidget(button("修改解锁密码", self.change_password))
        controls.addWidget(button("生成 / 更换恢复码", self.create_recovery_code))
        controls.addWidget(button("检查项目更新", self.check_updates))
        controls.addStretch()
        al.addLayout(controls)
        al.addWidget(label("恢复码状态：" + ("已创建" if getattr(self.controller, "recovery_available", False) else "尚未创建，建议现在生成") + "。恢复码需另行保存；数据目录含加密账号库、配置备份和诊断资料。", "muted", True))
        al.addWidget(button("打开管理器数据目录", self.open_data_directory))
        layout.addWidget(appearance)
        locations, ll = panel()
        self.location_card = locations
        ll.addWidget(label("TRAE 程序、配置与缓存位置", "sectionTitle"))
        state = self._view_cache.get("location", {})
        discovery = self.data.get("discovery", {})
        fields = QFormLayout()
        self.location_inputs = {}
        profiles = discovery.get("profiles") or []
        first_profile = profiles[0] if profiles else {}
        profile_default = first_profile.get("path", "") if isinstance(first_profile, dict) else str(first_profile)
        cache_default = next((row.get("path", "") for row in state.get("locations", []) if row.get("kind") == "cache" and row.get("exists") and row.get("accessible")), "")
        for key, title, initial in [
                ("executable", "TRAE 程序 / 安装目录", discovery.get("executable", "")),
                ("profile", "默认用户数据目录", discovery.get("profile") or profile_default),
                ("global_home", "全局配置目录", discovery.get("global_home", "")),
                ("cache", "缓存目录", discovery.get("cache") or cache_default)]:
            field = QLineEdit(str(initial or ""))
            field.setPlaceholderText("粘贴 .exe / .lnk / 安装目录" if key == "executable" else ("资料根目录、User 目录或 storage.json" if key == "profile" else "快速检测后填充，也可粘贴完整路径"))
            self.location_inputs[key] = field
            row = QHBoxLayout()
            row.addWidget(field, 1)

            def browse(k=key, control=field, as_file=False):
                if k == "executable" and as_file:
                    selected, _ = QFileDialog.getOpenFileName(self, "选择 TRAE 程序或快捷方式", control.text(), "TRAE 程序 / 快捷方式 (*.exe *.lnk);;所有文件 (*)")
                elif k == "profile" and as_file:
                    selected, _ = QFileDialog.getOpenFileName(self, "选择 TRAE storage.json", control.text(), "TRAE 登录资料 (storage.json);;JSON 文件 (*.json)")
                else:
                    selected = QFileDialog.getExistingDirectory(self, "选择安装或资料目录", control.text())
                if selected:
                    control.setText(selected)

            browse_button = button("浏览…")
            if key in ("executable", "profile"):
                menu = QMenu(browse_button)
                file_action = menu.addAction("程序 / 快捷方式文件…" if key == "executable" else "storage.json 文件…")
                file_action.triggered.connect(lambda _checked=False, handler=browse: handler(as_file=True))
                folder_action = menu.addAction("安装文件夹…" if key == "executable" else "资料根目录 / User 文件夹…")
                folder_action.triggered.connect(lambda _checked=False, handler=browse: handler(as_file=False))
                browse_button.setMenu(menu)
            else:
                browse_button.clicked.connect(lambda _checked=False, handler=browse: handler())
            row.addWidget(browse_button)
            fields.addRow(title, row)
        ll.addLayout(fields)
        location_controls = QHBoxLayout()
        location_controls.addWidget(button("验证并保存位置", self.save_locations, True))
        location_controls.addWidget(button("快速定位", self.quick_locate))
        location_controls.addWidget(button("深度扫描所有磁盘…", self.request_location_scan))
        location_controls.addWidget(button("如何快速找到位置", self.location_guide))
        location_controls.addStretch()
        ll.addLayout(location_controls)
        ll.addWidget(label("优先从运行进程、快捷方式和已知位置快速定位，支持任意磁盘；无需先全盘扫描。深度扫描为可选操作，需单独授权。", "muted", True))
        consent_text = "已允许" if state.get("scan_consent") is True else ("未允许" if state.get("scan_consent") is False else "尚未选择")
        ll.addWidget(label("扫描授权：" + consent_text + "  ·  全局 Skills 实际存储：" + self._view_cache.get("skills_root", "未找到"), "muted", True))
        ll.addWidget(button("迁移 / 恢复默认 Skills 存储目录", self.change_skills_root))
        layout.insertWidget(0, locations)
        diagnostics, dl = panel()
        dl.addWidget(label("兼容性与环境诊断", "sectionTitle"))
        dl.addWidget(label("检查 TRAE 安装、配置结构、计划任务、工具环境和本机权限。TRAE 更新后建议先运行一次。", "muted", True))
        dl.addWidget(button("运行环境诊断", self.run_diagnostics))
        layout.addWidget(diagnostics)
        layout.addStretch()
        return scroll(page)

    def save_settings(self):
        settings = dict(self.data.get("settings", {}))
        settings.update(auto_checkin=self.setting_auto.isChecked(),
                        scheduled_enabled=self.setting_scheduled.isChecked(),
                        schedule_time=self.setting_time.time().toString("HH:mm"),
                        start_at_login=self.setting_start.isChecked(),
                        theme=self.setting_theme.currentData(),
                        lock_minutes=self.setting_lock.value(),
                        usage_auto_refresh=self.setting_usage_auto.isChecked(),
                        usage_refresh_seconds=self.setting_usage_seconds.value(),
                        github_repository=self.setting_repo.text().strip())
        self.job("正在保存设置并更新 Windows 任务…", lambda: self.controller.save_settings(settings),
                 lambda _: self.message("设置已保存", "签到时间与启动设置已更新。可通过环境诊断检查计划任务状态。"))

    def change_password(self):
        old, ok = QInputDialog.getText(self, "修改解锁密码", "当前解锁密码", QLineEdit.EchoMode.Password)
        if not ok:
            return
        dialog = PasswordDialog("设置新解锁密码", "输入新密码。现有迁移备份仍使用各自的迁移密码。", True, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new = dialog.password.text()
        dialog.password.clear()
        dialog.repeat.clear()
        self.job("正在重新加密保险库…", lambda: self.controller.change_password(old, new),
                 lambda _: self.message("密码已更改", "下次解锁请使用新密码。"), refresh=False)

    def run_diagnostics(self):
        def done(results):
            dialog = QDialog(self)
            dialog.setWindowTitle("环境诊断")
            dialog.resize(870, 530)
            layout = QVBoxLayout(dialog)
            rows = [[r.get("name"), r.get("status"), r.get("detail")] for r in results]
            layout.addWidget(make_table(["检查项", "状态", "详情"], rows), 1)
            layout.addWidget(button("关闭", dialog.accept))
            dialog.exec()
        self.job("正在诊断环境与兼容性…", self.controller.diagnostics, done, refresh=False)

    def check_updates(self):
        def done(result):
            message = result.get("message", "更新检查已完成。")
            url = result.get("url")
            if url and str(url).startswith("https://github.com/"):
                if self.confirm("项目更新", message + "\n\n打开 GitHub 发布页面？"):
                    QDesktopServices.openUrl(QUrl(url))
            else:
                self.message("项目更新", message)
        self.job("正在检查项目发布版本…", self.controller.check_updates, done, refresh=False)

    def _logs_page(self):
        page, layout = self._content()
        actions = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("搜索日志")
        search.setClearButtonEnabled(True)
        actions.addWidget(search, 1)
        actions.addWidget(button("导出日志", self.export_logs))
        layout.addLayout(actions)
        rows = []
        for record in self.data.get("logs", []):
            if isinstance(record, dict):
                rows.append([record.get("time") or record.get("timestamp") or record.get("date"),
                             record.get("level") or record.get("status", "INFO"), record.get("event") or record.get("action"),
                             record.get("message") or record.get("detail")])
            else:
                rows.append(["", "INFO", "", str(record)])
        table = make_table(["时间", "级别", "操作", "详情"], rows)
        layout.addWidget(table, 1)

        def filter_rows(text):
            for r in range(table.rowCount()):
                value = " ".join(table.item(r, c).text() for c in range(4))
                table.setRowHidden(r, text.lower() not in value.lower())
        search.textChanged.connect(filter_rows)
        layout.addWidget(label("日志用于核对签到、迁移、配置变更与兼容性问题。登录密钥不应出现在日志中。", "muted", True))
        return page

    def export_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出操作日志", "Trae-CN-logs.json", "JSON 日志 (*.json)")
        if path:
            self.job("正在导出日志…", lambda: self.controller.export_logs(path),
                     lambda _: self.message("日志已导出", path), refresh=False)

    @staticmethod
    def _format_result(result):
        if result is None:
            return "操作完成。"
        if isinstance(result, dict):
            return "\n".join(f"{key}：{text_value(value)}" for key, value in result.items()
                             if key.lower() not in {"token", "access_token", "refresh_token", "cookie", "headers"})
        return text_value(result)

    def message(self, title, text):
        QMessageBox.information(self, title, str(text))

    def confirm(self, title, text):
        result = QMessageBox.question(self, title, str(text),
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        return result == QMessageBox.StandardButton.Yes

    def closeEvent(self, event: QCloseEvent):
        if self._quitting:
            event.accept()
            return
        if self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage(APP_NAME, "管家已驻留托盘。双击图标打开，右键可完全退出。", QSystemTrayIcon.MessageIcon.Information, 3500)
        elif self._busy:
            event.ignore()
            QMessageBox.information(self, "操作进行中", "请等待当前操作完成后退出。")
        else:
            self.quit_app()
            event.accept()

    def quit_app(self):
        if self._busy:
            self.show_from_tray()
            self.message("操作进行中", "请等待当前操作结束后退出，避免中断配置或保险库写入。")
            return
        self._quitting = True
        self.idle_timer.stop()
        self.usage_timer.stop()
        if self.controller.is_unlocked:
            self.controller.lock()
        self.tray.hide()
        QApplication.instance().quit()


def run(controller) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("TraeCNManager")
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(controller)
    window.show()
    return app.exec()
