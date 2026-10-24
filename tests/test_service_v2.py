from pathlib import Path
import tempfile
import unittest

from trae_manager.service import Controller


class Client:
    def __init__(self, root):
        self.root = root
        self.failure = False
        self.reads = 0

    def discover(self):
        return {"executable": "synthetic.exe", "version": "3.4.3", "global_home": str(self.root / "global"),
                "profiles": [{"name": "test", "path": str(self.root / "profile")}], "compatibility": {"state": "baseline"}}

    def inspect_profile(self, path):
        return {"login": {"token": "synthetic-only"}, "user_id": "test-user", "label": "test", "profile_path": str(path)}

    def usage(self, snapshot):
        self.reads += 1
        return {"remaining": None, "state": "error", "message": "test network failure"} if self.failure else {"remaining": 321.5, "plan": "Pro+", "message": "ok"}

    def checkin(self, snapshot):
        return {"state": "success", "message": "synthetic success"}


class V2ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = Client(self.root)
        self.controller = Controller(self.root / "data", client=self.client)
        self.controller.setup_password("original-password")
        self.controller.detect_accounts()
        self.account_id = self.controller.state()["accounts"][0]["id"]

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def test_usage_failure_preserves_last_balance_and_plan(self):
        before = self.controller.state()["accounts"][0]
        self.client.failure = True
        self.controller.refresh_usage()
        after = self.controller.state()["accounts"][0]
        self.assertEqual(after["remaining"], 321.5)
        self.assertEqual(after["plan"], "Pro+")
        self.assertEqual(after["usage_state"], "error")
        self.assertEqual(after["usage_updated_at"], before["usage_updated_at"])

    def test_signin_triggers_usage_update(self):
        before = self.client.reads
        self.controller.checkin()
        self.assertGreater(self.client.reads, before)

    def test_existing_v1_vault_recovers_without_losing_accounts(self):
        code = self.controller.create_recovery_code()
        self.controller.lock()
        self.controller.recover_password(code, "new-password")
        self.assertEqual(self.controller.state()["accounts"][0]["id"], self.account_id)
        self.assertTrue(self.controller.recovery_available)

    def test_memory_snapshot_is_encrypted_and_merge_is_explicit(self):
        memory = self.root / "global" / "memory" / "user_profile.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("# Profile\n\nSynthetic personal preference.\n", encoding="utf-8")
        self.controller.save_memory_snapshot(self.account_id)
        self.assertNotIn(b"Synthetic personal preference", self.controller.vault.path.read_bytes())
        memory.write_text("# Profile\n\nExisting preference.\n", encoding="utf-8")
        preview = self.controller.preview_memory_sync(self.account_id, self.account_id)
        self.assertEqual(preview["scope"], "local-shared")
        self.assertNotIn("Synthetic personal", memory.read_text())
        self.controller.sync_memory(self.account_id, self.account_id)
        self.assertIn("Synthetic personal preference", memory.read_text())
        self.assertIn("Existing preference", memory.read_text())
        self.assertTrue(self.controller.vault.snapshot().get("memory_history"))

    def test_polling_settings_are_bounded(self):
        self.controller.save_settings({"usage_refresh_seconds": 1, "usage_auto_refresh": False})
        settings = self.controller.state()["settings"]
        self.assertEqual(settings["usage_refresh_seconds"], 5)
        self.assertFalse(settings["usage_auto_refresh"])

    def test_removing_account_removes_its_private_memory_snapshot(self):
        self.controller.save_memory_snapshot(self.account_id)
        self.controller.remove_account(self.account_id)
        self.assertNotIn(self.account_id, self.controller.vault.snapshot().get("memory_snapshots", {}))

    def test_migration_carries_native_memory_without_silent_write(self):
        memory = self.root / "global" / "memory" / "user_profile.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("# Profile\n\nImported synthetic preference.\n", encoding="utf-8")
        self.controller.save_memory_snapshot(self.account_id)
        backup = self.root / "test.tcmbackup"
        self.controller.backup_export(backup, "backup-password")
        second = Controller(self.root / "second-data", client=Client(self.root / "other"))
        try:
            second.setup_password("second-password")
            second.backup_import(backup, "backup-password")
            target = self.root / "other" / "global" / "memory" / "user_profile.md"
            self.assertFalse(target.exists())
            self.assertEqual(second.memory_snapshots()[0]["file_count"], 1)
            second.merge_imported_memory()
            self.assertIn("Imported synthetic preference", target.read_text())
        finally:
            second.close()
