from pathlib import Path
import tempfile
import unittest

from trae_manager.security import Vault
from trae_manager.recovery import RecoveryManager


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Vault(Path(self.temp.name))
        self.vault.create("original-password", {"accounts": [{"synthetic": True}]})
        self.recovery = RecoveryManager(self.vault)

    def tearDown(self):
        self.vault.lock()
        self.temp.cleanup()

    def test_reset_retains_accounts_without_storing_code(self):
        code = self.recovery.create_code()
        self.assertNotIn(code, self.recovery.path.read_text())
        self.vault.lock()
        self.recovery.reset(code, "new-password")
        self.assertEqual(self.vault.snapshot()["accounts"], [{"synthetic": True}])
        self.vault.lock()
        self.vault.unlock("new-password")
        with self.assertRaises(ValueError):
            self.vault.unlock("original-password")

    def test_same_code_survives_normal_password_change(self):
        code = self.recovery.create_code()
        self.vault.change_password("original-password", "changed-password")
        self.recovery.rewrap()
        self.vault.lock()
        self.recovery.reset(code, "reset-password")
        self.assertTrue(self.vault.unlocked)

    def test_new_code_revokes_old_code(self):
        old = self.recovery.create_code()
        new = self.recovery.create_code()
        self.vault.lock()
        with self.assertRaises(ValueError):
            self.recovery.reset(old, "new-password")
        self.recovery.reset(new, "new-password")

    def test_tamper_and_missing_recovery(self):
        self.assertFalse(self.recovery.available)
        with self.assertRaises(ValueError):
            self.recovery.reset("bad", "new-password")
        self.recovery.create_code()
        self.assertTrue(self.recovery.available)
        self.recovery.path.write_text("{}")
        self.assertFalse(self.recovery.available)

    def test_rotation_failure_restores_password_and_recovery_together(self):
        from unittest.mock import patch
        from trae_manager.security import atomic_write
        code = self.recovery.create_code()
        failed = False
        def once(path, data):
            nonlocal failed
            if Path(path) == self.recovery.path and not failed:
                failed = True
                raise OSError("synthetic interruption")
            return atomic_write(path, data)
        with patch("trae_manager.security.atomic_write", side_effect=once):
            with self.assertRaises(RuntimeError):
                self.vault.change_password("original-password", "new-password", companion_builder=self.recovery.rotation_files)
        self.vault.lock()
        self.vault.unlock("original-password")
        self.vault.lock()
        self.recovery.reset(code, "recovered-password")
        self.assertEqual(self.vault.snapshot()["accounts"], [{"synthetic": True}])

    def test_restart_rolls_back_unfinished_rotation_journal(self):
        import base64
        import json
        original = self.vault.path.read_bytes()
        journal = self.vault.directory / ".rotation.journal"
        journal.write_text(json.dumps({"version": 1, "committed": False, "original": {"accounts.vault": base64.b64encode(original).decode()}}))
        self.vault.path.write_bytes(b"synthetic-interrupted-write")
        reopened = Vault(self.vault.directory)
        reopened.unlock("original-password")
        self.assertEqual(reopened.snapshot()["accounts"], [{"synthetic": True}])
        self.assertFalse(journal.exists())

    def test_stale_unlocked_instance_cannot_replace_new_recovery(self):
        stale = Vault(self.vault.directory)
        stale.unlock("original-password")
        recovery_stale = RecoveryManager(stale)
        code = self.recovery.create_code()
        self.vault.change_password("original-password", "changed-password", companion_builder=self.recovery.rotation_files)
        with self.assertRaises(ValueError):
            recovery_stale.create_code()
        self.vault.lock()
        self.recovery.reset(code, "recovered-password")
        self.assertTrue(self.vault.unlocked)


if __name__ == "__main__":
    unittest.main()
