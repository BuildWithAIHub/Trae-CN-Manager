"""Compact account/MCP workflows, with isolated data and no service execution."""
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
from trae_manager.ui import MainWindow, cn_datetime, selected_keys
import test_ui_v2 as fixture


class CompactWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.env_patch = patch.dict(os.environ, {"APPDATA": str(self.path / "appdata")})
        self.env_patch.start()
        self.client = fixture.LocalClient(self.path)
        self.controller = Controller(self.path / "data", client=self.client, discovery=self.client.discover())
        self.controller.ui_test_mode = True
        self.controller.location_state = lambda: {"scan_consent": False, "notice": None}
        self.controller.memory_snapshots = lambda: []
        self.controller.setup_password("isolated-ui-password")
        today = cn_datetime().strftime("%Y-%m-%d")
        accounts = []
        for identifier, name, day in [("a", "Alpha", today), ("b", "Beta", ""), ("c", "Gamma", "")]:
            accounts.append({"id": identifier, "label": name, "user_id": identifier if identifier != "c" else "",
                             "profile_path": str(self.path / ("profile-" + identifier)),
                             "credentials": {"login": {"token": "synthetic-only"}}, "plan": "Pro", "remaining": 100,
                             "usage_state": "ok", "last_success_day": day, "status": "待登录" if identifier == "c" else "正常",
                             "notes": "测试备注", "usage_updated_at": "2026-10-24T10:00:00+08:00"})
        with self.controller.vault.transaction() as doc:
            doc["accounts"] = accounts
            doc["settings"].update(auto_checkin=False, usage_auto_refresh=False)
        mcp = self.controller.configs.mcp_path()
        self.controller.configs.upsert_mcp(mcp, "alpha", {"command": "never-executed"})
        self.controller.configs.upsert_mcp(mcp, "beta", {"command": "never-executed"})
        self.controller.configs.set_mcp_enabled(mcp, "beta", False)
        self.controller.configs.upsert_mcp(mcp, "gamma", {"url": "https://mcp.example.invalid/api?token=DO-NOT-RENDER", "headers": {"Authorization": "DO-NOT-RENDER"}})
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
        self.env_patch.stop()
        self.temp.cleanup()

    def wait_idle(self):
        deadline = time.monotonic() + 15
        while self.window._busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertFalse(self.window._busy)

    def row_for(self, table, key):
        return next(row for row in range(table.rowCount()) if table.item(row, 0).data(Qt.ItemDataRole.UserRole) == key)

    def test_account_actions_are_hidden_until_selection_and_menus_preserve_all_actions(self):
        self.assertTrue(self.window.account_detail.isHidden())
        actions = self.window.account_actions
        for name in ("launch", "checkin", "refresh", "sync", "edit", "remove", "save_memory", "sync_memory"):
            self.assertFalse(actions[name].isEnabled())
        self.assertTrue(actions["check_all"].isEnabled())
        self.assertTrue(actions["refresh_all"].isEnabled())
        self.window.accounts_table.selectRow(self.row_for(self.window.accounts_table, "b"))
        self.assertFalse(self.window.account_detail.isHidden())
        self.assertTrue(actions["edit"].isEnabled())
        self.assertTrue(actions["sync_memory"].isEnabled())

    def test_account_filters_count_and_clear_selection_when_hidden(self):
        table = self.window.accounts_table
        table.selectRow(self.row_for(table, "a"))
        self.window.account_status_filter.setCurrentIndex(2)
        self.assertEqual(selected_keys(table), [])
        self.assertIn("共 3 个账号", self.window.accounts_count_label.text())
        self.assertIn("已签到 1", self.window.accounts_count_label.text())
        self.assertIn("显示 2", self.window.accounts_count_label.text())
        self.window.account_status_filter.setCurrentIndex(3)
        shown = [table.item(row, 0).text() for row in range(table.rowCount()) if not table.isRowHidden(row)]
        self.assertEqual(shown, ["Gamma"])

    def test_usage_poll_preserves_search_filter_selection_and_dirty_settings(self):
        self.window.account_search.setText("Beta")
        self.window.account_status_filter.setCurrentIndex(2)
        table = self.window.accounts_table
        table.selectRow(self.row_for(table, "b"))
        settings = self.window.setting_repo
        settings.setText("unsaved/repository")
        self.window._poll_usage()
        self.wait_idle()
        self.assertIs(table, self.window.accounts_table)
        self.assertEqual(self.window.account_search.text(), "Beta")
        self.assertEqual(self.window.account_status_filter.currentIndex(), 2)
        self.assertEqual(selected_keys(table), ["b"])
        self.assertEqual(settings.text(), "unsaved/repository")
        self.assertIn("显示 1", self.window.accounts_count_label.text())

    def test_selected_and_all_account_actions_have_distinct_targets(self):
        calls = []
        self.window.job = lambda _text, fn, *args, **kwargs: fn()
        self.controller.checkin = lambda ids: calls.append(ids) or []
        self.window.account_actions["checkin"].click()
        self.assertEqual(calls, [])
        self.window.accounts_table.selectRow(self.row_for(self.window.accounts_table, "b"))
        self.window.account_actions["checkin"].click()
        self.window.account_actions["check_all"].trigger()
        self.assertEqual(calls, [["b"], None])

    def test_mcp_filters_and_selection_actions_do_not_render_credentials(self):
        table = self.window.mcp_table
        self.assertTrue(self.window.mcp_detail.isHidden())
        self.assertFalse(self.window.mcp_actions["edit"].isEnabled())
        self.assertFalse(self.window.mcp_actions["delete_selected"].isEnabled())
        table.selectRow(self.row_for(table, "alpha"))
        self.assertTrue(self.window.mcp_actions["edit"].isEnabled())
        self.window.mcp_status_filter.setCurrentIndex(2)
        self.assertEqual(selected_keys(table), [])
        self.assertIn("启用 2", self.window.mcp_count_label.text())
        self.assertIn("显示 1", self.window.mcp_count_label.text())
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                self.assertNotIn("DO-NOT-RENDER", table.item(row, column).text())
        self.assertFalse(any("DO-NOT-RENDER" in widget.text() for widget in self.window.findChildren(QLabel)))

    def test_mcp_delete_all_counts_full_scope_and_requires_second_confirmation(self):
        self.window.mcp_search.setText("alpha")
        prompts, jobs = [], []
        responses = iter([True, False])
        self.window.confirm = lambda title, text: prompts.append((title, text)) or next(responses)
        self.window.job = lambda *args, **kwargs: jobs.append(args)
        self.window.batch_mcp(None, all_items=True)
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("3" in text for _, text in prompts))
        self.assertEqual(jobs, [])

    def test_mcp_multiple_selection_enables_bulk_but_not_single_service_actions(self):
        table = self.window.mcp_table
        table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 2), True)
        self.assertFalse(self.window.mcp_actions["edit"].isEnabled())
        self.assertFalse(self.window.mcp_actions["delete"].isEnabled())
        self.assertTrue(self.window.mcp_actions["delete_selected"].isEnabled())

    def test_select_all_under_a_filter_never_operates_on_hidden_rows(self):
        self.window.mcp_search.setText("alpha")
        self.window.mcp_table.selectAll()
        self.assertEqual(selected_keys(self.window.mcp_table), ["alpha"])
        deleted = []
        self.window.confirm = lambda *args: True
        self.controller.configs.delete_mcp_servers = lambda _path, names: deleted.append(names)
        self.window.job = lambda _text, fn, *args, **kwargs: fn()
        self.window.batch_mcp(None)
        self.assertEqual(deleted, [["alpha"]])
        self.window.account_search.setText("Beta")
        self.window.accounts_table.selectAll()
        self.assertEqual(self.window._account_selection(), ["b"])


if __name__ == "__main__":
    unittest.main()
