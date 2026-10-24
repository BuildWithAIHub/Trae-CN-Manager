import json
from pathlib import Path
import tempfile
import threading
import unittest

from trae_manager.security import Vault, encrypt_package, decrypt_package, windows_protect


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_wrong_password_tamper_and_no_plaintext(self):
        value = {"token": "synthetic-super-secret-value"}
        raw = encrypt_package(value, "test-password-2026")
        self.assertNotIn(value["token"].encode(), raw)
        self.assertEqual(decrypt_package(raw, "test-password-2026"), value)
        with self.assertRaises(ValueError):
            decrypt_package(raw, "wrong-password")
        envelope = json.loads(raw)
        import base64
        modified = bytearray(base64.b64decode(envelope["ciphertext"]))
        modified[0] ^= 1
        envelope["ciphertext"] = base64.b64encode(modified).decode()
        with self.assertRaises(ValueError):
            decrypt_package(json.dumps(envelope).encode(), "test-password-2026")

    def test_locked_vault_and_password_rotation(self):
        vault = Vault(self.root)
        vault.create("old-password", {"counter": 0})
        with vault.transaction() as doc:
            doc["counter"] = 1
        self.assertTrue(vault.path.with_suffix(".vault.previous").exists())
        vault.change_password("old-password", "new-password")
        self.assertFalse(vault.path.with_suffix(".vault.previous").exists())
        vault.lock()
        with self.assertRaises(ValueError):
            vault.snapshot()
        with self.assertRaises(ValueError):
            vault.unlock("old-password")
        vault.unlock("new-password")
        self.assertEqual(vault.snapshot()["counter"], 1)

    def test_independent_instances_do_not_lose_updates(self):
        one = Vault(self.root)
        one.create("same-password", {"counter": 0})
        two = Vault(self.root)
        two.unlock("same-password")
        def increment(vault):
            for _ in range(4):
                with vault.transaction() as doc:
                    doc["counter"] += 1
        a = threading.Thread(target=increment, args=(one,))
        b = threading.Thread(target=increment, args=(two,))
        a.start(); b.start(); a.join(); b.join()
        self.assertEqual(one.snapshot()["counter"], 8)

    def test_malicious_kdf_and_short_password_rejected(self):
        with self.assertRaises(ValueError):
            encrypt_package({}, "short")
        envelope = json.loads(encrypt_package({}, "long-password"))
        envelope["iterations"] = 999999999999
        with self.assertRaises(ValueError):
            decrypt_package(json.dumps(envelope).encode(), "long-password")

    def test_windows_key_protection(self):
        import os
        if os.name != "nt":
            self.skipTest("Windows only")
        original = b"synthetic" * 4
        sealed = windows_protect(original)
        self.assertNotEqual(sealed, original)
        self.assertEqual(windows_protect(sealed, decrypt=True), original)

    def test_envelope_size_boundary_rejected_before_writing(self):
        from unittest.mock import patch
        with patch("trae_manager.security.MAX_BYTES", 20000):
            with self.assertRaises(ValueError):
                encrypt_package({"data": "x" * 15000}, "test-password")


if __name__ == "__main__":
    unittest.main()
