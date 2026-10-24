"""Optional translation of one selected purpose, without editing native files."""
from __future__ import annotations

import hashlib
import re
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLineEdit, QMenu, QProgressBar, QVBoxLayout

from .descriptions import summary_text
from .translation import prepare_text
from .translation_batch import translate_purposes


class _BatchProgress(QObject):
    changed = Signal(int, int, str)


class TranslationUIMixin:
    @staticmethod
    def _original_purpose(item):
        if item.get("error"):
            return "内容有误，请编辑检查"
        return item.get("summary") or summary_text(item.get("description", ""))

    def _purpose_key(self, kind, item):
        source = self._original_purpose(item)
        return kind, str(item["path"]), hashlib.sha256(source.encode("utf-8")).hexdigest()

    def display_purpose(self, kind, item):
        entry = self.__dict__.get("_purpose_translations", {}).get(self._purpose_key(kind, item))
        return summary_text(entry["text"]) if entry else self._original_purpose(item)

    def _translation_menu(self, kind, owner):
        menu = QMenu(owner)
        for caption, target in (("译为中文", "zh-CN"), ("译为英文", "en")):
            action = menu.addAction(caption)
            action.triggered.connect(lambda _checked=False, lang=target: self.translate_purpose(kind, lang))
        original = menu.addAction("显示原文")
        original.triggered.connect(lambda: self.restore_purpose(kind))
        menu.addSeparator()
        settings = menu.addAction("翻译设置…")
        settings.triggered.connect(self.translation_settings_dialog)
        return menu

    def _update_purpose_view(self, kind):
        """Patch the existing table so translation retains selection and filters."""
        view = self.__dict__.get("_purpose_views", {}).get(kind)
        if not view:
            return
        table, lookup, changed = view
        try:
            table.rowCount()
        except RuntimeError:
            self._purpose_views.pop(kind, None)
            return
        sorting = table.isSortingEnabled()
        table.setSortingEnabled(False)
        try:
            for row in range(table.rowCount()):
                key = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                item = lookup.get(key)
                if item:
                    cell = table.item(row, 2)
                    cell.setText(self.display_purpose(kind, item))
                    entry = self.__dict__.get("_purpose_translations", {}).get(self._purpose_key(kind, item))
                    cell.setToolTip(entry["text"] if entry else "")
        finally:
            table.setSortingEnabled(sorting)
        changed()

    def restore_purpose(self, kind):
        item = self._selected_item(kind)
        if item:
            self.__dict__.setdefault("_purpose_translations", {}).pop(self._purpose_key(kind, item), None)
            self._update_purpose_view(kind)
            self.status_label.setText("已显示原文")

    def _batch_translation_menu(self, kind, owner):
        menu = QMenu(owner)
        for caption, target in (("全部译为中文", "zh-CN"), ("全部译为英文", "en")):
            menu.addAction(caption).triggered.connect(
                lambda _checked=False, language=target: self.translate_all_purposes(kind, language))
        menu.addSeparator()
        menu.addAction("全部显示原文").triggered.connect(lambda: self.restore_all_purposes(kind))
        return menu

    def restore_all_purposes(self, kind):
        translations = self.__dict__.setdefault("_purpose_translations", {})
        for item in getattr(self, kind + "_entries", []):
            translations.pop(self._purpose_key(kind, item), None)
        self._update_purpose_view(kind)
        self.status_label.setText("当前范围已全部显示原文")

    def translate_all_purposes(self, kind, target):
        if self._busy or not self.controller.is_unlocked:
            return
        entries = [{"key": self._purpose_key(kind, item), "name": item["name"],
                    "text": self._original_purpose(item), "invalid": bool(item.get("error"))}
                   for item in getattr(self, kind + "_entries", [])]
        if not entries:
            self.message("暂无条目", "当前配置范围没有可翻译的说明。")
            return
        unique = set()
        for entry in entries:
            if entry["invalid"]:
                continue
            try:
                text, source, language = prepare_text(entry["text"], target)
                if source != language:
                    unique.add(text)
            except ValueError:
                pass
        characters = sum(len(text) for text in unique)

        def configured(settings):
            direction = "中文" if target == "zh-CN" else "英文"
            scope = "当前配置范围的全部" + (" Skills" if kind == "skills" else " Rules")
            text = (f"将{scope} {len(entries)} 项用途译为{direction}，包括被筛选隐藏的条目。\n"
                    f"最多发送 {len(unique)} 段、约 {characters} 字符；已有译文缓存会复用，原文件不会改写。")
            if unique:
                if settings.get("provider", "mymemory") == "mymemory":
                    text += "\n\n将逐段发送至 MyMemory，服务可能保存这些文字。免费额度每日 5,000 字符，额度不足会停止并保留已完成结果。"
                else:
                    text += "\n\n将逐段发送至你配置的翻译服务：\n" + settings.get("endpoint", "")
                text += "\n不会发送文件、路径或账号信息。"
            else:
                text += "\n\n已有目标语言的用途在本机显示，不会联网。"
            text += "\n\n是否继续？可随时取消，正在进行的一次请求会先结束。"
            if not self.confirm("全部翻译", text):
                self.status_label.setText("已取消全部翻译")
                return
            self._run_purpose_batch(kind, target, entries, settings, bool(unique))

        self.job("正在读取翻译设置…", self.controller.translation_settings, configured, refresh=False)

    def _run_purpose_batch(self, kind, target, entries, settings, needs_online):
        from . import ui
        cancel = threading.Event()

        class ProgressDialog(ui.QDialog):
            finished_batch = False

            def reject(dialog_self):
                if dialog_self.finished_batch:
                    super().reject()
                    return
                cancel.set()
                cancel_button.setEnabled(False)
                caption.setText("正在取消，等待当前请求结束…")

        dialog = ProgressDialog(self)
        dialog.setWindowTitle("全部翻译")
        dialog.setMinimumWidth(480)
        layout = QVBoxLayout(dialog)
        caption = ui.label("准备逐项翻译…", "muted", True)
        layout.addWidget(caption)
        bar = QProgressBar()
        bar.setRange(0, len(entries))
        layout.addWidget(bar)
        cancel_button = ui.button("取消翻译", dialog.reject)
        layout.addWidget(cancel_button)
        bridge = _BatchProgress(dialog)
        closed = False

        def progress(current, total, name):
            if not closed:
                bar.setValue(current)
                if not cancel.is_set():
                    caption.setText(f"已处理 {current} / {total} · {name}")

        bridge.changed.connect(progress, Qt.ConnectionType.QueuedConnection)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._translation_batch_dialog = dialog
        self._translation_batch_cancel = cancel
        dialog.show()

        def operation():
            if needs_online and not settings.get("consent") and not cancel.is_set():
                self.controller.configure_translation(settings.get("provider", "mymemory"),
                                                      endpoint=settings.get("endpoint", ""), consent=True)
            return translate_purposes(self.controller, entries, target, cancel, bridge.changed.emit)

        def close():
            nonlocal closed
            closed = True
            bridge.changed.disconnect(progress)
            dialog.finished_batch = True
            dialog.accept()
            dialog.deleteLater()
            self._translation_batch_dialog = None
            self._translation_batch_cancel = None

        def done(result):
            close()
            if self.controller.is_unlocked and not result.get("invalidated"):
                translations = self.__dict__.setdefault("_purpose_translations", {})
                for entry in result["items"]:
                    if entry.get("result"):
                        translations[entry["key"]] = entry["result"]
                self._update_purpose_view(kind)
            counts = result["counts"]
            text = (f"翻译 {counts['translated']} · 复用缓存 {counts['cached']} · 已是目标语言 {counts['unchanged']}"
                    f"\n跳过 {counts['skipped']} · 失败 {counts['failed']} · 未处理 {result['remaining']}")
            if result["stopped"]:
                text += "\n\n" + result["stopped"] + "；已完成的译文已保留。"
            failures = [entry["name"] for entry in result["items"] if entry["state"] == "failed"]
            if failures:
                text += "\n未完成：" + "、".join(failures[:15])
            self.message("翻译结果", text)

        def failed(message):
            close()
            self.message("翻译未完成", message)

        self.job("正在翻译全部用途…", operation, done, refresh=False, failure=failed)

    def translate_purpose(self, kind, target):
        if self._busy:
            return
        item = self._selected_item(kind)
        if not item or item.get("error"):
            return
        source = self._original_purpose(item)
        if not source:
            self.message("暂无用途", "先编辑内容补充一句用途，再进行翻译。")
            return
        key = self._purpose_key(kind, item)

        def translated(result):
            if not self.controller.is_unlocked:
                return
            self.__dict__.setdefault("_purpose_translations", {})[key] = result
            self._update_purpose_view(kind)
            self.status_label.setText("已显示中文译文" if target == "zh-CN" else "已显示英文译文")

        def run():
            self.job("正在翻译用途…", lambda: self.controller.translate_description(source, target),
                     translated, refresh=False)

        def configured(settings):
            if settings.get("consent"):
                run()
                return
            if settings.get("provider", "mymemory") == "mymemory":
                disclosure = "将当前这一句用途发送至 MyMemory 翻译，服务可能保存文本。不会发送文件或账号。\n\n是否同意并继续？可随时在「翻译设置」撤销。"
            else:
                disclosure = ("将当前这一句用途发送至你配置的翻译服务：\n" + settings.get("endpoint", "")
                              + "\n\n是否同意并继续？不会发送文件或账号。")
            if not self.confirm("使用在线翻译", disclosure):
                self.status_label.setText("已取消翻译，保留原文")
                return

            def allow():
                self.controller.configure_translation(settings.get("provider", "mymemory"),
                                                      endpoint=settings.get("endpoint", ""), consent=True)
                return self.controller.translate_description(source, target)
            self.job("正在翻译用途…", allow, translated, refresh=False)

        # The backend returns the original locally for an already matching language.
        # Avoid an online permission prompt when no request will be sent.
        is_chinese = bool(re.search(r"[\u3400-\u9fff]", source))
        if (target == "zh-CN") == is_chinese:
            run()
        else:
            self.job("正在读取翻译设置…", self.controller.translation_settings, configured, refresh=False)

    def translation_settings_dialog(self):
        if self._busy:
            return
        self.job("正在读取翻译设置…", self.controller.translation_settings,
                 self._show_translation_settings, refresh=False)

    def _show_translation_settings(self, settings):
        from . import ui
        dialog = ui.QDialog(self)
        dialog.setWindowTitle("翻译设置")
        dialog.setMinimumWidth(540)
        layout = QVBoxLayout(dialog)
        layout.addWidget(ui.label("可翻译单项或当前范围全部用途；每次请求只发一段，原文件保留原文。", "muted", True))
        form = QFormLayout()
        provider = ui.ComboBox()
        provider.addItem("免费在线 · MyMemory", "mymemory")
        provider.addItem("自定义 · LibreTranslate", "libre")
        provider.setCurrentIndex(max(0, provider.findData(settings.get("provider", "mymemory"))))
        endpoint = QLineEdit(settings.get("endpoint", "") if settings.get("provider") == "libre" else "")
        endpoint.setPlaceholderText("https://你的服务地址 或 http://localhost:5000")
        api_key = QLineEdit()
        api_key.setEchoMode(QLineEdit.EchoMode.Password)
        api_key.setPlaceholderText("已保存密钥，留空保留" if settings.get("has_api_key") else "选填，密钥加密保存")
        clear_key = ui.CheckBox("清除已保存的密钥")
        consent = ui.CheckBox("允许向此服务发送所选用途")
        consent.setChecked(bool(settings.get("consent")))
        note = ui.label("", "muted", True)
        form.addRow("翻译服务", provider)
        form.addRow("服务地址", endpoint)
        form.addRow("API 密钥", api_key)
        form.addRow("", clear_key)
        form.addRow("", consent)
        layout.addLayout(form)
        layout.addWidget(note)

        def provider_changed(*_):
            custom = provider.currentData() == "libre"
            endpoint.setEnabled(custom)
            api_key.setEnabled(custom)
            clear_key.setEnabled(custom)
            note.setText("MyMemory 可能保存提交的文字。免费额度每日 5,000 字符，超额后可次日重试。"
                         if not custom else "使用你信任的 LibreTranslate 服务。公网地址需要 HTTPS；本机可用 HTTP。")

        def changed(*_):
            consent.setChecked(False)
            provider_changed()

        provider.currentIndexChanged.connect(changed)
        endpoint.textEdited.connect(lambda *_: consent.setChecked(False))
        provider_changed()
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(ui.button("取消", dialog.reject))
        buttons.addWidget(ui.button("保存", dialog.accept, True))
        layout.addLayout(buttons)
        accepted = dialog.exec() == ui.QDialog.DialogCode.Accepted
        chosen = provider.currentData()
        address = endpoint.text().strip() if chosen == "libre" else ""
        secret = api_key.text() or None
        permitted, clear = consent.isChecked(), clear_key.isChecked()
        api_key.clear()
        dialog.deleteLater()
        if accepted:
            def save():
                return self.controller.configure_translation(chosen, endpoint=address, api_key=secret,
                                                             consent=permitted, clear_key=clear)
            def saved(_):
                self.__dict__.setdefault("_purpose_translations", {}).clear()
                for kind in ("skills", "rules"):
                    self._update_purpose_view(kind)
                self.status_label.setText("翻译设置已保存")
            self.job("正在保存翻译设置…", save, saved, refresh=False)
