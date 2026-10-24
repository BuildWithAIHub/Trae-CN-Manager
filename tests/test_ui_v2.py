"""Offscreen regressions for the new desktop flows (temporary data only)."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QTableWidgetSelectionRange

from trae_manager.service import Controller
from trae_manager.ui import MainWindow, SkillResourcesDialog


class LocalClient:
    def __init__(self, root):
        self.root = root
        self.balance = 125

    def discover(self):
        return {"executable": str(self.root / "Trae.exe"), "version": "3.4.3",
                "global_home": str(self.root / "global"),
                "profiles": [{"path": str(self.root / "profile"), "name": "test"}],
                "compatibility": {"state": "baseline"}}

    def inspect_profile(self, path):
        return {"login": {"token": "test-only"}, "user_id": "test-user",
                "label": "test", "profile_path": str(path)}

    def usage(self, account):
        self.balance -= 1
        return {"remaining": self.balance, "plan": "Pro", "message": "ok"}


class DesktopV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.appdata_patch = patch.dict(os.environ, {"APPDATA": str(self.path / "appdata")})
        self.appdata_patch.start()
        self.client = LocalClient(self.path)
        self.controller = Controller(self.path / "data", client=self.client,
                                     discovery=self.client.discover())
        self.controller.ui_test_mode = True
        self.controller.location_state = lambda: {"scan_consent": False, "notice": None}
        self.controller.memory_snapshots = lambda: []
        self.controller.setup_password("test-password-2026")
        self.controller.detect_accounts()
        with self.controller.vault.transaction() as doc:
            doc["settings"].update(auto_checkin=False, usage_auto_refresh=False)
        self.window = MainWindow(self.controller)
        self.wait_idle()

    def tearDown(self):
        self.window.idle_timer.stop()
        self.window.usage_timer.stop()
        self.wait_idle()
        self.window.tray.hide()
        self.window._quitting = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.controller.close()
        self.appdata_patch.stop()
        self.temp.cleanup()

    def wait_idle(self):
        deadline = time.monotonic() + 10
        while self.window._busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertFalse(self.window._busy, "GUI worker did not finish")

    def test_all_pages_render_and_titlebar_is_themed(self):
        self.assertTrue(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(self.window.pages.count(), 8)
        self.assertEqual(self.window.title_bar.height(), 48)
        for page in range(8):
            for item in self.window.pages.widget(page).findChildren(QLabel):
                self.assertNotIn("此页配置暂时无法读取", item.text())
        self.window.apply_theme("light")
        self.assertIn("#titleBar", self.window.styleSheet())

    def test_automatic_usage_refresh_preserves_dirty_settings_and_selection(self):
        self.window.nav.setCurrentRow(6)
        field = self.window.setting_repo
        field.setText("unsaved/repository")
        table = self.window.accounts_table
        table.selectRow(0)
        activity = self.window._last_activity
        self.window._poll_usage()
        self.wait_idle()
        self.assertIs(field, self.window.setting_repo)
        self.assertEqual(field.text(), "unsaved/repository")
        self.assertEqual(len(table.selectionModel().selectedRows()), 1)
        self.assertEqual(table.item(0, 3).text(), str(self.client.balance))
        self.assertEqual(activity, self.window._last_activity)

    def test_lock_clears_cached_configuration_and_account_summaries(self):
        self.window.lock()
        self.assertEqual(self.window._view_cache, {})
        self.assertEqual(self.window.data["accounts"], [])
        self.assertEqual(self.window.pages.count(), 0)
        self.assertFalse(self.controller.is_unlocked)

    def test_poll_failure_keeps_last_balance_and_marks_it_stale(self):
        before = self.window.accounts_table.item(0, 3).text()

        def unavailable(_):
            raise OSError("offline")

        self.client.usage = unavailable
        self.window._poll_usage()
        self.wait_idle()
        self.assertEqual(self.window.accounts_table.item(0, 3).text(), before)
        self.assertIn("保留原值", self.window.accounts_table.item(0, 4).text())

    def test_acknowledged_partial_scan_does_not_start_another_full_scan(self):
        calls = []
        self.controller.scan_locations = lambda: calls.append("scan")
        self.window._view_cache["location"] = {"scan_consent": True,
                                               "needs_scan": True,
                                               "auto_scan_requested": False,
                                               "notice": None}
        self.window._location_startup()
        self.assertEqual(calls, [])

    def test_skill_file_tree_includes_nested_and_disabled_resources(self):
        configs = self.controller.configs
        skill = configs.save_item("skills", "sample", "---\nname: sample\ndescription: test\n---\n# Demo\n")
        configs.save_skill_file(skill, "assets/example.txt", "example")
        configs.set_skill_entry_enabled(skill, "assets/example.txt", False)
        dialog = SkillResourcesDialog(self.window, {"name": "sample", "path": str(skill)})
        dialog.load_tree()
        self.wait_idle()
        found = []

        def visit(item):
            value = item.data(0, Qt.ItemDataRole.UserRole)
            found.append(value)
            for index in range(item.childCount()):
                visit(item.child(index))

        for index in range(dialog.tree.topLevelItemCount()):
            visit(dialog.tree.topLevelItem(index))
        resource = next(item for item in found if item["relative"] == "assets/example.txt")
        self.assertFalse(resource["enabled"])
        self.assertTrue(any(item["relative"] == "SKILL.md" for item in found))
        dialog.deleteLater()

    def add_skill_fixtures(self):
        for name in ("beta", "alpha", "gamma"):
            self.controller.configs.save_item("skills", name, f"---\nname: {name}\ndescription: {name} tools\n---\n# Demo\n")
        path = self.path / "global" / "skills" / "beta" / "SKILL.md"
        self.controller.configs.set_items_enabled("skills", [path], False)
        self.window.refresh()
        self.wait_idle()

    def test_skill_filters_show_counts_and_clear_hidden_selection(self):
        self.add_skill_fixtures()
        table = self.window.skills_table
        text = self.window.skills_count_label.text()
        self.assertIn("共 3 项", text)
        self.assertIn("启用 2", text)
        self.assertIn("停用 1", text)
        self.assertEqual(table.item(0, 0).text(), "alpha")
        table.selectRow(0)
        self.window.skills_status_filter.setCurrentIndex(2)
        self.assertIn("显示 1", self.window.skills_count_label.text())
        self.assertEqual(len(table.selectionModel().selectedRows()), 0)
        visible = [table.item(row, 0).text() for row in range(table.rowCount()) if not table.isRowHidden(row)]
        self.assertEqual(visible, ["beta"])
        self.window.skills_status_filter.setCurrentIndex(0)
        self.window.skills_search.setText("gamma")
        visible = [table.item(row, 0).text() for row in range(table.rowCount()) if not table.isRowHidden(row)]
        self.assertEqual(visible, ["gamma"])

    def test_selection_actions_require_a_single_item_and_bulk_accepts_multiple(self):
        self.add_skill_fixtures()
        actions = self.window.item_actions["skills"]
        self.assertFalse(actions["edit"].isEnabled())
        self.assertFalse(actions["files"].isEnabled())
        self.assertFalse(actions["delete_selected"].isEnabled())
        table = self.window.skills_table
        table.selectRow(0)
        self.assertTrue(actions["edit"].isEnabled())
        self.assertTrue(actions["files"].isEnabled())
        table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 2), True)
        self.assertFalse(actions["edit"].isEnabled())
        self.assertTrue(actions["delete_selected"].isEnabled())

    def test_delete_without_selection_never_falls_back_to_all(self):
        self.add_skill_fixtures()
        jobs, prompts, messages = [], [], []
        self.window.job = lambda *args, **kwargs: jobs.append(args)
        self.window.confirm = lambda *args: prompts.append(args) or True
        self.window.message = lambda *args: messages.append(args)
        self.window.batch_items("skills", None)
        self.assertEqual(jobs, [])
        self.assertEqual(prompts, [])
        self.assertEqual(len(messages), 1)

    def test_delete_all_requires_two_confirmations_with_total_even_when_filtered(self):
        self.add_skill_fixtures()
        self.window.skills_search.setText("alpha")
        jobs, prompts = [], []
        responses = iter([True, False])
        self.window.job = lambda *args, **kwargs: jobs.append(args)

        def confirm(title, message):
            prompts.append((title, message))
            return next(responses)

        self.window.confirm = confirm
        self.window.batch_items("skills", None, all_items=True)
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("3" in message for _, message in prompts))
        self.assertEqual(jobs, [])

    def test_quick_location_success_does_not_request_full_disk_scan(self):
        calls = []
        self.window._view_cache["location"] = {"quick_complete": False, "quick_status": "found", "scan_consent": None,
                                               "notice": {"id": "found-test", "kind": "found", "message": "已确认位置"}}
        self.window.message = lambda *args: calls.append("message")
        self.window.confirm = lambda *args: self.fail("quick discovery must not request deep scan")
        self.controller.acknowledge_location = lambda value: calls.append(value)
        self.window._location_startup()
        self.wait_idle()
        self.assertEqual(calls, ["message", "found-test"])


if __name__ == "__main__":
    unittest.main()
