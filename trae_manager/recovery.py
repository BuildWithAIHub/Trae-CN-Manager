"""Public-key recovery envelopes. The printed recovery code is never stored."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .security import Vault, atomic_write, file_lock, _parse, _open

AAD = b"TraeCNManager/recovery/v1"


def _raw(public):
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _wrapping_key(shared, salt):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=AAD).derive(shared)


class RecoveryManager:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.path = vault.directory / "recovery.json"

    @property
    def available(self):
        if not self.vault.initialized or not self.path.is_file():
            return False
        try:
            self._read()
            return True
        except (ValueError, OSError):
            return False

    def _read(self):
        try:
            if self.path.stat().st_size > 16384:
                raise ValueError()
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("format") != "trae-cn-manager-recovery" or doc.get("version") != 1:
                raise ValueError()
            for key, length in (("public_key", 32), ("ephemeral_key", 32), ("salt", 16), ("nonce", 12), ("ciphertext", 48)):
                raw = base64.b64decode(doc[key], validate=True)
                if len(raw) != length:
                    raise ValueError()
            return doc
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError("恢复文件损坏，请使用加密迁移备份，或解锁后重新生成恢复码") from exc

    def _envelope(self, public: bytes, secret: bytes):
        ephemeral = X25519PrivateKey.generate()
        salt, nonce = os.urandom(16), os.urandom(12)
        key = _wrapping_key(ephemeral.exchange(X25519PublicKey.from_public_bytes(public)), salt)
        cipher = AESGCM(key).encrypt(nonce, secret, AAD)
        doc = {"format": "trae-cn-manager-recovery", "version": 1,
               "public_key": base64.b64encode(public).decode(), "ephemeral_key": base64.b64encode(_raw(ephemeral.public_key())).decode(),
               "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
               "ciphertext": base64.b64encode(cipher).decode()}
        return json.dumps(doc, indent=2).encode()

    def _wrap(self, public: bytes):
        atomic_write(self.path, self._envelope(public, self.vault.key_for_background()))

    def rotation_files(self, new_key):
        if not self.path.exists():
            return {}
        doc = self._read()
        return {self.path: self._envelope(base64.b64decode(doc["public_key"]), new_key)}

    def create_code(self):
        with self.vault._mutex, file_lock(self.vault.lock_path):
            if not self.vault.unlocked:
                raise ValueError("请先解锁，再创建恢复码")
            self.vault._recover_rotation_locked()
            _open(_parse(self.vault.path.read_bytes()), self.vault.key_for_background())
            private = X25519PrivateKey.generate()
            self._wrap(_raw(private.public_key()))
            raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
            code = base64.b32encode(raw).decode().rstrip("=")
            return "TCN2-" + "-".join(code[i:i + 4] for i in range(0, len(code), 4))

    def rewrap(self):
        with self.vault._mutex, file_lock(self.vault.lock_path):
            if not self.path.exists():
                return
            _open(_parse(self.vault.path.read_bytes()), self.vault.key_for_background())
            doc = self._read()
            self._wrap(base64.b64decode(doc["public_key"]))

    def reset(self, code: str, new_password: str, extra_builder=None):
        if not isinstance(new_password, str) or len(new_password) < 8:
            raise ValueError("新密码至少需要 8 个字符")
        cleaned = re.sub(r"[\s-]", "", str(code).upper())
        if not cleaned.startswith("TCN2") or not re.fullmatch(r"[A-Z2-7]{52}", cleaned[4:]):
            raise ValueError("恢复码格式不正确，请输入 TCN2 开头的完整恢复码")
        try:
            raw = base64.b32decode(cleaned[4:] + "=" * ((8 - len(cleaned[4:]) % 8) % 8))
            private = X25519PrivateKey.from_private_bytes(raw)
            doc = self._read()
            public = base64.b64decode(doc["public_key"])
            if _raw(private.public_key()) != public:
                raise ValueError("恢复码与这个账号库不匹配，或已被新恢复码替换")
            salt = base64.b64decode(doc["salt"])
            key = _wrapping_key(private.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(doc["ephemeral_key"]))), salt)
            recovered = AESGCM(key).decrypt(base64.b64decode(doc["nonce"]), base64.b64decode(doc["ciphertext"]), AAD)
            def companions(new_key):
                if base64.b64decode(self._read()["public_key"]) != public:
                    raise ValueError("恢复码已经更新，请使用最新保存的恢复码")
                files = {self.path: self._envelope(public, new_key)}
                if extra_builder:
                    files.update(extra_builder(new_key))
                return files
            self.vault.reset_password_with_key(recovered, new_password, companion_builder=companions)
        except InvalidTag as exc:
            raise ValueError("恢复文件校验失败，请检查恢复码或使用迁移备份") from exc
