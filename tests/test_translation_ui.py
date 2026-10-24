"""Translation consent and display behavior using synthetic purposes only."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
import threading
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QLineEdit

from trae_manager.ui import selected_keys
import test_ui_v2 as fixture


class TranslationUITests(unittest.TestCase):
    setUpClass = fixture.DesktopV2Tests.__dict__["setUpClass"]
    tearDown = fixture.DesktopV2Tests.tearDown
    wait_idle = fixture.DesktopV2Tests.wait_idle

    def setUp(self):
        fixture.DesktopV2Tests.setUp(self)
        self.skill = self.controller.configs.save_item("skills", "report-helper",
            "---\nname: report-helper\ndescription: Check interfaces and generate a report.\n---\n\nPrivate body is never translated.\n")
        self.rule = self.controller.configs.save_item("rules", "review.md", "# 规则\n\n检查接口并生成报告。\n")
        self.window.refresh()
        self.wait_idle()
        self.calls = []
        self.settings = {"provider": "mymemory", "endpoint": "", "has_api_key": False, "consent": False}
        self.controller.translation_settings = lambda: dict(self.settings)

        def configure(provider, **kwargs):
            self.calls.append(("configure", provider, kwargs))
            self.settings.update(provider=provider, **{k: v for k, v in kwargs.items() if k in ("endpoint", "consent")})
            return dict(self.settings)

        def translate(text, target):
            self.calls.append(("translate", text, target))
            return {"text": "检查接口并生成报告。" if target == "zh-CN" else "Check interfaces and generate a report.",
                    "target": target, "source": "en" if target == "zh-CN" else "zh-CN", "provider": "mymemory", "cached": False}

        self.controller.configure_translation = configure
        self.controller.translate_description = translate

    def select(self, kind, path):
        table = getattr(self.window, kind + "_table")
        row = next(i for i in range(table.rowCount()) if table.item(i, 0).data(Qt.ItemDataRole.UserRole) == str(path))
        table.selectRow(row)
        return table

    def test_reject_consent_does_not_send_or_change_purpose(self):
        table = self.select("skills", self.skill)
        self.window.confirm = lambda *args: False
        self.window.translate_purpose("skills", "zh-CN")
        self.wait_idle()
        self.assertEqual(self.calls, [])
        self.assertEqual(table.item(table.currentRow(), 2).text(), "Check interfaces and generate a report.")

    def test_translation_updates_selected_purpose_without_writing_file_or_rebuilding(self):
        table = self.select("skills", self.skill)
        original = self.skill.read_bytes()
        prompts = []
        self.window.confirm = lambda title, text: prompts.append(text) or True
        self.window.translate_purpose("skills", "zh-CN")
        self.wait_idle()
        self.assertIn("MyMemory", prompts[0])
        self.assertIn("保存文本", prompts[0])
        self.assertEqual([x[0] for x in self.calls], ["configure", "translate"])
        self.assertEqual(self.calls[-1][1], "Check interfaces and generate a report.")
        self.assertNotIn("Private body", str(self.calls))
        self.assertIs(self.window.skills_table, table)
        self.assertEqual(selected_keys(table), [str(self.skill)])
        self.assertEqual(table.item(table.currentRow(), 2).text(), "检查接口并生成报告。")
        self.assertEqual(self.skill.read_bytes(), original)
        self.window.restore_purpose("skills")
        self.assertEqual(table.item(table.currentRow(), 2).text(), "Check interfaces and generate a report.")

    def test_reverse_direction_and_matching_language_skip_permission_prompt(self):
        table = self.select("rules", self.rule)
        self.settings["consent"] = True
        self.window.confirm = lambda *args: self.fail("Stored consent should not prompt again")
        self.window.translate_purpose("rules", "en")
        self.wait_idle()
        self.assertEqual(self.calls[-1][2], "en")
        self.assertIn("Check interfaces", table.item(table.currentRow(), 2).text())
        self.settings["consent"] = False
        self.window.translate_purpose("rules", "zh-CN")
        self.wait_idle()
        self.assertEqual(self.calls[-1][2], "zh-CN")

    def test_new_source_invalidates_old_translation_and_lock_clears_views(self):
        self.select("skills", self.skill)
        self.settings["consent"] = True
        self.window.translate_purpose("skills", "zh-CN")
        self.wait_idle()
        item = self.window.skills_entries[0]
        changed = {**item, "summary": "Different purpose."}
        self.assertEqual(self.window.display_purpose("skills", changed), "Different purpose.")
        self.window.lock()
        self.assertEqual(self.window._purpose_translations, {})
        self.assertEqual(self.window._purpose_views, {})

    def test_settings_do_not_reveal_key_and_save_explicit_revoke(self):
        config = {"provider": "libre", "endpoint": "http://localhost:5000/translate", "has_api_key": True, "consent": True}
        observed = []

        def accept():
            dialog = next(widget for widget in self.app.topLevelWidgets() if isinstance(widget, QDialog) and widget.windowTitle() == "翻译设置")
            secret = next(widget for widget in dialog.findChildren(QLineEdit) if widget.echoMode() == QLineEdit.EchoMode.Password)
            observed.append(secret.text())
            consent = next(widget for widget in dialog.findChildren(QCheckBox) if "允许" in widget.text())
            consent.setChecked(False)
            dialog.accept()

        QTimer.singleShot(0, accept)
        self.window._show_translation_settings(config)
        self.wait_idle()
        self.assertEqual(observed, [""])
        self.assertFalse(self.calls[-1][2]["consent"])
        self.assertIsNone(self.calls[-1][2]["api_key"])

    def add_second_skill(self):
        second = self.controller.configs.save_item("skills", "writer-helper",
            "---\nname: writer-helper\ndescription: Write project documentation.\n---\n\nPrivate second body.\n")
        self.window.refresh()
        self.wait_idle()
        return second

    def test_total_translation_button_covers_hidden_rows_and_preserves_files_selection(self):
        second = self.add_second_skill()
        original = {path: path.read_bytes() for path in (self.skill, second)}
        self.window.skills_search.setText("report-helper")
        table = self.select("skills", self.skill)
        prompts, messages = [], []
        self.window.confirm = lambda title, text: prompts.append(text) or True
        self.window.message = lambda title, text: messages.append(text)
        control = self.window.skills_translate_all
        self.assertEqual(control.text(), "全部翻译")
        self.assertIn("全部译为中文", [a.text() for a in control.menu().actions()])
        self.window.translate_all_purposes("skills", "zh-CN")
        self.wait_idle()
        self.assertIn("2 项", prompts[0])
        self.assertIn("隐藏", prompts[0])
        self.assertIn("可能保存", prompts[0])
        sent = [call[1] for call in self.calls if call[0] == "translate"]
        self.assertEqual(set(sent), {"Check interfaces and generate a report.", "Write project documentation."})
        self.assertNotIn("Private", str(sent))
        self.assertEqual(self.window.skills_search.text(), "report-helper")
        self.assertEqual(selected_keys(table), [str(self.skill)])
        self.assertIs(table, self.window.skills_table)
        self.assertTrue(all(path.read_bytes() == content for path, content in original.items()))
        self.assertIn("未处理 0", messages[0])
        self.window.restore_all_purposes("skills")
        self.assertEqual(table.item(table.currentRow(), 2).text(), "Check interfaces and generate a report.")

    def test_total_translation_always_discloses_scope_even_after_single_item_consent(self):
        self.add_second_skill()
        self.settings["consent"] = True
        prompts = []
        self.window.confirm = lambda title, text: prompts.append(text) or False
        self.window.translate_all_purposes("skills", "zh-CN")
        self.wait_idle()
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.calls, [])
        self.assertIsNone(getattr(self.window, "_translation_batch_dialog", None))

    def test_cancel_batch_retains_first_result_and_reports_unprocessed_items(self):
        self.add_second_skill()
        self.settings["consent"] = True
        self.window.confirm = lambda *args: True
        messages = []
        self.window.message = lambda title, text: messages.append(text)
        translate = self.controller.translate_description

        def cancel_after_first(text, target):
            self.window._translation_batch_cancel.set()
            return translate(text, target)

        self.controller.translate_description = cancel_after_first
        self.window.translate_all_purposes("skills", "zh-CN")
        self.wait_idle()
        self.assertEqual(len([call for call in self.calls if call[0] == "translate"]), 1)
        self.assertIn("未处理 1", messages[0])
        self.assertIn("已取消", messages[0])
        self.assertEqual(len(self.window._purpose_translations), 1)
        self.assertIsNone(self.window._translation_batch_dialog)

    def test_progress_cancel_stays_usable_while_main_shell_is_disabled(self):
        self.add_second_skill()
        self.settings["consent"] = True
        self.window.confirm = lambda *args: True
        self.window.message = lambda *args: None
        release = threading.Event()
        entered = threading.Event()
        observed = []
        translate = self.controller.translate_description

        def slow_request(text, target):
            entered.set()
            release.wait(4)
            return translate(text, target)

        self.controller.translate_description = slow_request

        def cancel_progress():
            dialog = getattr(self.window, "_translation_batch_dialog", None)
            if dialog is None or not entered.is_set():
                QTimer.singleShot(10, cancel_progress)
                return
            try:
                from PySide6.QtWidgets import QPushButton
                button = next(button for button in dialog.findChildren(QPushButton) if button.text() == "取消翻译")
                observed.append((not self.window.shell.isEnabled(), button.isEnabled(), dialog.isVisible()))
                button.click()
                observed.append((dialog.isVisible(), self.window._translation_batch_cancel.is_set()))
            finally:
                release.set()

        QTimer.singleShot(10, cancel_progress)
        self.window.translate_all_purposes("skills", "zh-CN")
        try:
            self.wait_idle()
        finally:
            release.set()
        self.assertEqual(observed, [(True, True, True), (True, True)])
        self.assertEqual(len([call for call in self.calls if call[0] == "translate"]), 1)
        self.assertIsNone(self.window._translation_batch_dialog)


if __name__ == "__main__":
    unittest.main()
