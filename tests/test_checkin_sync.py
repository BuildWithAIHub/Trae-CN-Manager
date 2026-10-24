"""Official-state synchronization and per-account sign-in controls, synthetic only."""
import copy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QTableWidgetSelectionRange

from trae_manager.service import Controller
from trae_manager.trae import API_ORIGIN, STATUS_PATH, TraeClient
from trae_manager.ui import MainWindow, cn_datetime, selected_keys
import test_ui_v2 as ui_fixture


class StatusClient:
    def __init__(self, root):
        self.root = root
        self.checked = False
        self.enabled = True
        self.failure = False
        self.claims = 0

    def discover(self):
        return {"global_home": str(self.root / "global"), "profiles": []}

    def usage(self, account):
        return {"remaining": 99, "plan": "Pro+", "message": "ok"}

    def checkin_status(self, account):
        if self.failure:
            raise OSError("synthetic-network-error")
        return {"state": "already" if self.checked else "pending" if self.enabled else "unavailable",
                "enabled": self.enabled, "checked_in": self.checked}

    def checkin(self, account):
        if self.checked:
            return {"state": "already", "message": "官方已签到"}
        self.claims += 1
        self.checked = True
        return {"state": "success", "message": "已确认签到"}


@pytest.fixture
def controller(tmp_path):
    client = StatusClient(tmp_path)
    value = Controller(tmp_path / "data", client=client, discovery=client.discover())
    value.setup_password("isolated-status-password")
    with value.vault.transaction() as doc:
        doc["settings"].update(auto_checkin=False, scheduled_enabled=False, usage_auto_refresh=False)
        doc["accounts"] = [{"id": "a", "label": "Alpha", "user_id": "a",
                            "profile_path": str(tmp_path / "no-profile"),
                            "credentials": {"login": {"token": "SYNTHETIC_STATUS_TOKEN"}},
                            "last_success_day": ""}]
    yield value
    value.close()


def test_external_official_checkin_is_synced_without_claim(controller):
    controller.refresh_usage()
    assert controller.state()["accounts"][0]["checkin_state"] == "pending"
    controller.client.checked = True  # The user signed in using the official TRAE client.
    controller.refresh_usage()
    account = controller.state()["accounts"][0]
    assert account["signed_today"] is True
    assert account["checkin_state"] == "already"
    assert account["remaining"] == 99
    assert "credentials" not in account
    assert "SYNTHETIC_STATUS_TOKEN" not in str(account)
    assert controller.checkin()[0]["state"] == "already"
    assert controller.client.claims == 0


def test_failed_refresh_preserves_confirmed_green_day(controller):
    controller.client.checked = True
    controller.refresh_usage()
    before = controller.state()["accounts"][0]
    controller.client.failure = True
    controller.refresh_usage()
    after = controller.state()["accounts"][0]
    assert after["signed_today"] is True
    assert after["checkin_state"] == "already"
    assert after["checkin_confirmed_at"] == before["checkin_confirmed_at"]
    assert after["checkin_error"]


def test_delayed_negative_status_cannot_undo_confirmed_day(controller):
    controller.client.checked = True
    controller.refresh_usage()
    controller.client.checked = False
    controller.refresh_usage()
    assert controller.state()["accounts"][0]["checkin_state"] == "already"


def test_next_beijing_day_expires_yesterdays_confirmed_state(controller):
    now = datetime(2026, 10, 24, 23, 59, tzinfo=timezone(timedelta(hours=8)))
    with patch("trae_manager.service.cn_now", return_value=now):
        controller.client.checked = True
        controller.refresh_usage()
        assert controller.state()["accounts"][0]["signed_today"]
    with patch("trae_manager.service.cn_now", return_value=now + timedelta(minutes=2)):
        account = controller.state()["accounts"][0]
        assert account["checkin_state"] == "unknown"
        assert account["signed_today"] is False
        controller.client.checked = False
        controller.refresh_usage()
        assert controller.state()["accounts"][0]["checkin_state"] == "pending"


@pytest.mark.parametrize("result", [
    {"state": "already"},
    {"state": "already", "checked_in": "yes", "enabled": True},
    {"state": "pending", "checked_in": True, "enabled": True},
    {"state": "unavailable", "checked_in": False, "enabled": True},
])
def test_malformed_official_state_does_not_mark_success(controller, result):
    controller.client.checkin_status = lambda _: result
    controller.refresh_usage()
    account = controller.state()["accounts"][0]
    assert account["checkin_state"] == "error"
    assert account["signed_today"] is False


def test_legacy_client_without_status_endpoint_keeps_usage_compatible(controller):
    controller.client.checkin_status = None
    controller.refresh_usage()
    account = controller.state()["accounts"][0]
    assert account["remaining"] == 99
    assert account["checkin_state"] == "unknown"


def test_mixed_batch_reports_new_success_and_previously_signed(controller):
    original = controller.vault.snapshot()["accounts"][0]
    with controller.vault.transaction() as doc:
        doc["accounts"].append({**copy.deepcopy(original), "id": "b", "label": "Beta", "user_id": "b",
                                "last_success_day": cn_datetime().date().isoformat()})
        doc["accounts"].append({**copy.deepcopy(original), "id": "c", "label": "Gamma", "user_id": "",
                                "credentials": {}})
    result = controller.checkin()
    assert [r["state"] for r in result] == ["success", "already", "expired"]
    assert controller.client.claims == 1
    lines = MainWindow._checkin_result_lines(result)
    assert "本次成功 1" in lines[0] and "此前已签 1" in lines[0] and "未完成 1" in lines[0]
    assert "此前已签：Beta" in lines
    assert "本次成功：Alpha" in lines
    assert "未完成：Gamma" in lines


@pytest.mark.parametrize("body,expected", [
    ({"enable": True, "checked_in": True}, "already"),
    ({"enable": True, "checked_in": False}, "pending"),
    ({"enable": False, "checked_in": False}, "unavailable"),
    ({"checked_in": True}, "error"),
])
def test_native_status_is_readonly_and_schema_checked(body, expected):
    snapshot = {"device_id": "synthetic-device", "login": {"token": "SYNTHETIC", "userId": "a", "account": {"scope": "marscode"},
                "host": API_ORIGIN, "expiredAt": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}}
    client = TraeClient()
    response = Mock(status_code=200)
    response.json.return_value = body
    client.session = Mock()
    client.session.post.return_value = response
    result = client.checkin_status(snapshot)
    assert result["state"] == expected
    assert client.session.post.call_count == 1
    assert client.session.post.call_args.args[0] == API_ORIGIN + STATUS_PATH


class CheckinControlsTests(unittest.TestCase):
    setUpClass = classmethod(ui_fixture.DesktopV2Tests.setUpClass.__func__)
    setUp = ui_fixture.DesktopV2Tests.setUp
    tearDown = ui_fixture.DesktopV2Tests.tearDown
    wait_idle = ui_fixture.DesktopV2Tests.wait_idle

    def test_poll_updates_green_badge_and_disables_all_signin_entrypoints(self):
        table = self.window.accounts_table
        table.selectRow(0)
        self.client.checkin_status = lambda _: {"state": "already", "checked_in": True, "enabled": True}
        self.window._poll_usage()
        self.wait_idle()
        self.assertIs(self.window.accounts_table, table)
        self.assertEqual(len(selected_keys(table)), 1)
        self.assertEqual(table.item(0, 7).text(), "今日已签到")
        self.assertFalse(table.item(0, 7).icon().isNull())
        self.assertEqual(table.item(0, 7).foreground().color().name(), "#8de6b4")
        self.assertTrue(table.item(0, 7).font().bold())
        for control in [self.window.account_actions["checkin"], self.window.account_actions["check_all"],
                        self.window.overview_checkin_button, self.window.tray_checkin_action]:
            self.assertFalse(control.isEnabled())
        self.window.data["settings"]["theme"] = "light"
        self.window.apply_theme("light")
        self.window._paint_checkin_badges()
        self.assertEqual(table.item(0, 7).foreground().color().name(), "#247447")

    def test_multiselect_hint_and_mixed_selection_explain_skipping(self):
        today = cn_datetime().date().isoformat()
        with self.controller.vault.transaction() as doc:
            second = copy.deepcopy(doc["accounts"][0])
            second.update(id="second", user_id="second", label="Second", last_success_day=today)
            doc["accounts"].append(second)
        self.window.refresh()
        self.wait_idle()
        table = self.window.accounts_table
        table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, table.columnCount() - 1), True)
        self.assertTrue(self.window.account_actions["checkin"].isEnabled())
        self.assertEqual(self.window.account_actions["checkin"].text(), "签到（1）")
        labels = [label.text() for label in self.window.findChildren(QLabel)]
        self.assertTrue(any("按住 Ctrl" in text for text in labels))
        self.assertTrue(any("今日已签 1" in text and "已签账号会跳过" in text for text in labels))

    def test_midnight_redraw_keeps_table_and_reenables_next_day(self):
        now = datetime(2026, 10, 24, 23, 59, tzinfo=timezone(timedelta(hours=8)))
        self.window.data["accounts"][0]["last_success_day"] = now.date().isoformat()
        table = self.window.accounts_table
        table.selectRow(0)
        with patch("trae_manager.ui.cn_datetime", return_value=now):
            self.window._tick()
            self.assertFalse(self.window.account_actions["checkin"].isEnabled())
        with patch("trae_manager.ui.cn_datetime", return_value=now + timedelta(minutes=2)):
            self.window._tick()
            self.assertIs(table, self.window.accounts_table)
            self.assertEqual(table.item(0, 7).text(), "待确认")
            self.assertTrue(self.window.account_actions["checkin"].isEnabled())

    def test_summary_distinguishes_official_already_from_new_success(self):
        messages = []
        results = [{"label": "Alpha", "state": "success"}, {"label": "Beta", "state": "already"},
                   {"label": "Gamma", "state": "unavailable", "message": "官方未开放"}]
        self.window.message = lambda title, text: messages.append((title, text))
        self.controller.checkin = lambda _: results
        self.window.job = lambda text, operation, done, **kwargs: done(operation())
        self.window.checkin()
        self.assertEqual(messages[0][0], "签到结果")
        self.assertIn("此前已签：Beta", messages[0][1])
        self.assertIn("本次成功 1", messages[0][1])
        self.assertIn("暂未开放 1", messages[0][1])
