"""Authenticated vaults. No plaintext account snapshots are written to disk."""
from __future__ import annotations

import base64
import contextlib
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

FORMAT = "trae-cn-manager-vault"
ITERATIONS = 600_000
MAX_BYTES = 64 * 1024 * 1024
AAD = b"TraeCNManager/vault/v1"


def atomic_write(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 90):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() > deadline:
                    raise RuntimeError("另一个操作尚未完成，请稍后再试")
                time.sleep(0.1)
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=iterations).derive(password.encode("utf-8"))


def _parse(raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise ValueError("加密文件过大")
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError()
        if doc.get("format") != FORMAT or doc.get("version") != 1:
            raise ValueError()
        if doc.get("kdf") != "PBKDF2-SHA256" or doc.get("cipher") != "AES-256-GCM":
            raise ValueError()
        if not 100_000 <= doc["iterations"] <= 2_000_000:
            raise ValueError()
        for field in ("salt", "nonce", "ciphertext"):
            doc[field] = base64.b64decode(doc[field], validate=True)
        if len(doc["salt"]) != 16 or len(doc["nonce"]) != 12:
            raise ValueError()
        return doc
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("不是受支持的加密备份，或文件已经损坏") from exc


def _seal(payload: dict, key: bytes, salt: bytes, iterations: int) -> bytes:
    nonce = os.urandom(12)
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(plaintext) > MAX_BYTES - 4096:
        raise ValueError("备份内容过大，请减少资源文件")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, AAD)
    sealed = json.dumps({"format": FORMAT, "version": 1, "kdf": "PBKDF2-SHA256",
                       "iterations": iterations, "cipher": "AES-256-GCM",
                       "salt": base64.b64encode(salt).decode(),
                       "nonce": base64.b64encode(nonce).decode(),
                       "ciphertext": base64.b64encode(ciphertext).decode()},
                      ensure_ascii=False).encode("utf-8")
    if len(sealed) > MAX_BYTES:
        raise ValueError("加密结果超过 64 MB，请减少备份资源")
    return sealed


def _open(doc: dict, key: bytes) -> dict:
    try:
        value = json.loads(AESGCM(key).decrypt(doc["nonce"], doc["ciphertext"], AAD))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("密码错误，或加密文件完整性校验失败") from exc


def encrypt_package(payload: dict, password: str) -> bytes:
    salt = os.urandom(16)
    return _seal(payload, _derive(password, salt, ITERATIONS), salt, ITERATIONS)


def decrypt_package(raw: bytes, password: str) -> dict:
    doc = _parse(raw)
    return _open(doc, _derive(password, doc["salt"], doc["iterations"]))


class Vault:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "accounts.vault"
        self.lock_path = self.directory / ".vault.lock"
        self._key: bytearray | None = None
        self._mutex = threading.RLock()
        self._recover_rotation()

    def _recover_rotation(self):
        journal = self.directory / ".rotation.journal"
        if not journal.exists():
            return
        with self._mutex, file_lock(self.lock_path):
            self._recover_rotation_locked()

    def _recover_rotation_locked(self):
        journal = self.directory / ".rotation.journal"
        if not journal.exists():
            return
        try:
            if journal.is_symlink() or journal.stat().st_size > MAX_BYTES * 2:
                raise ValueError()
            record = json.loads(journal.read_bytes())
            if not isinstance(record, dict) or record.get("version") != 1:
                raise ValueError()
            if not record.get("committed"):
                original = record["original"]
                if not isinstance(original, dict) or "accounts.vault" not in original:
                    raise ValueError()
                for name, encoded in original.items():
                    if name not in {"accounts.vault", "recovery.json", "background.key"}:
                        raise ValueError()
                    target = self.directory / name
                    if target.is_symlink():
                        raise ValueError()
                    if encoded is None:
                        target.unlink(missing_ok=True)
                    else:
                        atomic_write(target, base64.b64decode(encoded, validate=True))
            journal.unlink()
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise RuntimeError("密码更新恢复尚未完成，请保留数据目录并检查磁盘权限后重试") from exc

    @property
    def initialized(self):
        return self.path.exists()

    @property
    def unlocked(self):
        return self._key is not None

    def create(self, password: str, initial: dict):
        with self._mutex, file_lock(self.lock_path):
            if self.path.exists():
                raise ValueError("账号库已存在，请解锁")
            salt = os.urandom(16)
            key = _derive(password, salt, ITERATIONS)
            atomic_write(self.path, _seal(initial, key, salt, ITERATIONS))
            self._key = bytearray(key)

    def unlock(self, password: str):
        with self._mutex, file_lock(self.lock_path):
            doc = _parse(self.path.read_bytes())
            key = _derive(password, doc["salt"], doc["iterations"])
            _open(doc, key)
            self._key = bytearray(key)

    def unlock_key(self, key: bytes):
        with self._mutex, file_lock(self.lock_path):
            _open(_parse(self.path.read_bytes()), key)
            self._key = bytearray(key)

    def lock(self):
        with self._mutex:
            if self._key is not None:
                for i in range(len(self._key)):
                    self._key[i] = 0
            self._key = None

    def key_for_background(self) -> bytes:
        if not self.unlocked:
            raise ValueError("请先解锁")
        return bytes(self._key)

    def snapshot(self):
        with self._mutex, file_lock(self.lock_path):
            if self._key is None:
                raise ValueError("请先解锁账号库")
            return _open(_parse(self.path.read_bytes()), bytes(self._key))

    @contextlib.contextmanager
    def transaction(self):
        with self._mutex, file_lock(self.lock_path):
            if self._key is None:
                raise ValueError("请先解锁账号库")
            doc = _parse(self.path.read_bytes())
            payload = _open(doc, bytes(self._key))
            before = copy.deepcopy(payload)
            yield payload
            if before != payload:
                previous = self.path.read_bytes()
                atomic_write(self.path.with_suffix(".vault.previous"), previous)
                atomic_write(self.path, _seal(payload, bytes(self._key), doc["salt"], doc["iterations"]))

    def _rotate(self, new, *, old=None, recovered_key=None, companion_builder=None):
        with self._mutex, file_lock(self.lock_path):
            self._recover_rotation_locked()
            doc = _parse(self.path.read_bytes())
            original_key = recovered_key if recovered_key is not None else _derive(old, doc["salt"], doc["iterations"])
            payload = _open(doc, original_key)
            salt = os.urandom(16)
            key = _derive(new, salt, ITERATIONS)
            targets = {self.path: _seal(payload, key, salt, ITERATIONS)}
            if companion_builder:
                for path, raw in companion_builder(key).items():
                    path = Path(path)
                    if path.parent.resolve() != self.directory.resolve() or path.name not in {"recovery.json", "background.key"} or path.is_symlink():
                        raise ValueError("密码更新包含不受支持的伴随文件")
                    targets[path] = raw
            original = {path.name: base64.b64encode(path.read_bytes()).decode() if path.exists() else None for path in targets}
            journal = self.directory / ".rotation.journal"
            record = {"version": 1, "committed": False, "original": original}
            atomic_write(journal, json.dumps(record).encode())
            try:
                for path, raw in targets.items():
                    atomic_write(path, raw)
                # A previous-password snapshot must not survive a successful change.
                self.path.with_suffix(".vault.previous").unlink(missing_ok=True)
                record["committed"] = True
                atomic_write(journal, json.dumps(record).encode())
            except Exception as exc:
                try:
                    self._recover_rotation_locked()
                    self._key = bytearray(original_key)
                except Exception as rollback:
                    self.lock()
                    raise RuntimeError("密码更新中断，恢复记录已保留；重新打开软件会继续恢复原状态") from rollback
                raise RuntimeError("密码更新未完成，原密码、恢复文件和后台密钥已恢复") from exc
            self._key = bytearray(key)
            journal.unlink(missing_ok=True)

    def change_password(self, old: str, new: str, *, companion_builder=None):
        self._rotate(new, old=old, companion_builder=companion_builder)

    def reset_password_with_key(self, recovered_key: bytes, new: str, *, companion_builder=None):
        """Reset with a verified recovery key and commit companion envelopes together."""
        self._rotate(new, recovered_key=recovered_key, companion_builder=companion_builder)


def windows_protect(value: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("后台凭据保护需要 Windows")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    source_buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source = Blob(len(value), source_buffer)
    target = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    func = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    func.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    func.restype = wintypes.BOOL
    if not func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError("Windows 凭据保护失败（错误码 %s），请重新启用后台签到" % ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(target.pbData)
