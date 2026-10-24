from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trae_manager.service import Controller
from trae_manager.platforms import due_today, task_xml
from trae_manager.security import encrypt_package


class FakeClient:
    def __init__(self, root):
        self.root = root
        self.claims = 0

    def discover(self):
        return {"executable": "synthetic.exe", "version": "3.4.3", "profiles":
                [{"path": str(self.root / "official"), "name": "test"}],
                "global_home": str(self.root / "global"), "compatibility": {"state": "baseline"}}

    def inspect_profile(self, path):
        return {"login": {"token": "synthetic-token"}, "user_id": "synthetic-user", "label": "test", "profile_path": str(path)}

    def checkin(self, account):
        self.claims += 1
        return {"state": "success", "message": "已签到"}

    def usage(self, account):
        return {"remaining": 123, "plan": "test", "message": "ok"}

    def launch(self, profile):
        return None

    def restore_account(self, account, profile):
        self.restored = (account, profile)
        profile.mkdir(parents=True, exist_ok=True)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = FakeClient(self.root)
        self.controller = Controller(self.root / "data", client=self.client)
        self.controller.setup_password("test-password-2026")

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def test_detect_dedup_and_summaries_hide_credentials(self):
        self.controller.detect_accounts()
        self.controller.detect_accounts()
        state = self.controller.state()
        self.assertEqual(len(state["accounts"]), 1)
        self.assertNotIn("credentials", state["accounts"][0])
        self.assertNotIn("synthetic-token", str(state))

    def test_daily_idempotency_and_locked_access(self):
        self.controller.detect_accounts()
        self.controller.checkin()
        self.controller.checkin()
        self.assertEqual(self.client.claims, 1)
        self.controller.lock()
        self.assertEqual(self.controller.state()["accounts"], [])
        with self.assertRaises(ValueError):
            self.controller.checkin()

    def test_migration_merges_without_restoring_arbitrary_paths(self):
        self.controller.detect_accounts()
        skill = self.root / "global" / "skills" / "sample" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: sample\ndescription: test\n---\nhello", encoding="utf-8")
        backup = self.root / "backup.tcmbackup"
        self.controller.backup_export(backup, "backup-password-2026")
        self.assertNotIn(b"synthetic-token", backup.read_bytes())
        self.assertNotIn(b"description", backup.read_bytes())
        other = Controller(self.root / "second", client=FakeClient(self.root / "other"))
        try:
            other.setup_password("other-password")
            result = other.backup_import(backup, "backup-password-2026")
            self.assertEqual(result["added"], 1)
            account = other.vault.snapshot()["accounts"][0]
            self.assertTrue(account["needs_restore"])
            self.assertTrue(Path(account["profile_path"]).is_relative_to(other.data_dir))
            self.assertFalse(other.vault.snapshot()["settings"]["scheduled_enabled"])
            restored = other.restore_backup_configs()
            self.assertEqual(restored["restored"], 1)
        finally:
            other.close()

    def test_malicious_migration_path_is_rejected(self):
        package = {"package": "trae-cn-manager-backup", "schema_version": 1,
                   "vault": {"accounts": []}, "resources": [{"scope": "global", "owner": "", "name": "skills/../../escape", "content": "eA=="}]}
        backup = self.root / "attack.tcmbackup"
        backup.write_bytes(encrypt_package(package, "backup-password"))
        with self.assertRaises(ValueError):
            self.controller.backup_import(backup, "backup-password")

    def test_exports_cannot_overwrite_vault_or_background_key(self):
        for name in ("accounts.vault", "background.key", "accounts.vault.previous"):
            with self.assertRaises(ValueError):
                self.controller.export_logs(self.controller.data_dir / name)
            with self.assertRaises(ValueError):
                self.controller.backup_export(self.controller.data_dir / name, "backup-password")

    def test_detect_fills_pending_account_instead_of_duplicating(self):
        self.controller.create_account("pending")
        self.controller.detect_accounts()
        accounts = self.controller.state()["accounts"]
        # Official account was detected first; merge a pending matching path directly.
        with self.controller.vault.transaction() as doc:
            doc["accounts"] = [a for a in doc["accounts"] if a["label"] == "pending"]
            pending = doc["accounts"][0]
            pending["user_id"] = ""
            pending["credentials"] = {}
            pending["profile_path"] = str(self.root / "official")
        self.controller.detect_accounts()
        self.assertEqual(len(self.controller.state()["accounts"]), 1)
        self.assertEqual(self.controller.state()["accounts"][0]["label"], "pending")
        self.assertEqual(self.controller.state()["accounts"][0]["user_id"], "synthetic-user")

    def test_launch_saved_account_uses_separate_profile_after_native_switch(self):
        self.controller.detect_accounts()
        identifier = self.controller.state()["accounts"][0]["id"]
        with patch.object(self.client, "inspect_profile", return_value={"user_id": "different-native-user"}):
            self.controller.launch_account(identifier)
        selected = self.controller.state()["accounts"][0]
        self.assertTrue(Path(selected["profile_path"]).is_relative_to(self.controller.data_dir / "instances"))
        self.assertNotEqual(selected["profile_path"], str(self.root / "official"))

    def test_setting_task_failure_rolls_back(self):
        with patch("trae_manager.service.configure_schedule", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError):
                self.controller.save_settings({"scheduled_enabled": True})
        self.assertFalse(self.controller.state()["settings"]["scheduled_enabled"])
        self.assertFalse((self.controller.data_dir / "background.key").exists())

    def test_config_history_can_restore_deleted_skill(self):
        item = self.controller.configs.save_item("skills", "test-skill", "---\nname: test-skill\ndescription: test\n---\nhello")
        self.controller.configs.remove_item("skills", item)
        history = self.controller.config_history()
        self.assertEqual(len(history), 1)
        self.controller.restore_config_backup(history[0]["path"])
        self.assertTrue(item.exists())

    def test_config_history_cannot_restore_outside_managed_scope(self):
        import json
        folder = self.controller.data_dir / "config-backups"
        folder.mkdir()
        backup = folder / "forged-backup"
        backup.write_text("test")
        backup.with_name(backup.name + ".restore.json").write_text(json.dumps({"original_path": str(self.root / "unmanaged.txt")}))
        with self.assertRaises(ValueError):
            self.controller.restore_config_backup(backup)

    def test_setting_vault_commit_failure_restores_os_changes(self):
        from contextlib import contextmanager
        @contextmanager
        def fail_commit():
            raise OSError("synthetic disk error")
            yield
        with patch.object(self.controller.vault, "transaction", fail_commit), \
             patch("trae_manager.service.windows_protect", return_value=b"synthetic-protected-key"), \
             patch("trae_manager.service.configure_schedule", return_value={"backend": "startup", "message": "test"}) as schedule, \
             patch("trae_manager.service.configure_startup") as startup:
            with self.assertRaises(OSError):
                self.controller.save_settings({"scheduled_enabled": True, "start_at_login": True})
        self.assertEqual(schedule.call_count, 2)
        self.assertEqual([call.args[1] for call in schedule.call_args_list], [True, False])
        self.assertEqual([call.args[1] for call in startup.call_args_list], [True, False])
        self.assertFalse((self.controller.data_dir / "background.key").exists())
        self.assertFalse(self.controller.state()["settings"]["scheduled_enabled"])

    def test_background_retries_transient_key_rotation(self):
        with self.controller.vault.transaction() as doc:
            doc["settings"].update(scheduled_enabled=True, schedule_backend="startup")
        key = self.controller.vault.key_for_background()
        (self.controller.data_dir / "background.key").write_bytes(b"synthetic-placeholder")
        self.controller.lock()
        with patch("trae_manager.service.windows_protect", side_effect=[RuntimeError("synthetic rotation gap"), key]) as decrypt, \
             patch.object(self.controller._stop, "wait", side_effect=[False, True]):
            self.assertEqual(self.controller.run_background(), 0)
        self.assertEqual(decrypt.call_count, 2)
        import json
        status = json.loads((self.controller.data_dir / "background-status.json").read_text())
        self.assertEqual(status["state"], "running")

    def test_time_and_catchup_xml(self):
        tz = timezone(timedelta(hours=8))
        self.assertFalse(due_today("10:30", datetime(2026, 10, 23, 10, 29, tzinfo=tz)))
        self.assertTrue(due_today("10:30", datetime(2026, 10, 23, 10, 31, tzinfo=tz)))
        xml = task_xml(self.root, "17:21")
        self.assertIn("17:21:00+08:00", xml)
        self.assertIn("StartWhenAvailable>true", xml)
        self.assertIn("LogonTrigger", xml)
        self.assertIn("InteractiveToken", xml)


if __name__ == "__main__":
    unittest.main()
