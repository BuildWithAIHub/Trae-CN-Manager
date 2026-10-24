"""Local TRAE CN discovery and the verified native account interfaces.

Account snapshots returned here contain credentials. They must only be passed to
the encrypted vault, never serialized to an ordinary export or application log.
Native formats were checked against installed 3.4.3 and 3.4.7 resources.
This module never changes the official client's authentication files.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:
    import winreg
except ImportError:  # Unit tests can run on another OS.
    winreg = None


API_ORIGIN = "https://api.trae.cn"
STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
USAGE_PATH = "/trae/api/v2/pay/ide_user_ent_usage"
AUTH_KEY = "iCubeAuthInfo://icube.cloudide"
DEVICE_KEY_PREFIX = "iCubeAuthInfo://icube-dc:"
DATA_SCHEMA = 1
VERIFIED_NATIVE_VERSIONS = {"3.4.3", "3.4.7"}
MAX_STATE_BYTES = 16 * 1024 * 1024
CN_TIME = timezone(timedelta(hours=8))
_NATIVE_HEADER = bytes((116, 99, 5, 16, 0, 0))
_PRIVATE_HEADER = bytes((18, 57, 32, 32, 2, 3))

# Published in the shipped byteCrypto.js, not user- or account-specific keys.
_AES_A = bytes((82,9,106,213,48,54,165,56,191,64,163,158,129,243,215,251,124,227,57,130,155,47,255,135,52,142,67,68,196,222,233,203,84,123,148,50,166,194,35,61,238,76,149,11,66,250,195,78,8,46,161,102,40,217,36,178,118,91,162,73,109,139,209,37))
_AES_B = bytes((31,221,168,51,136,7,199,49,177,18,16,89,39,128,236,95,96,81,127,169,25,181,74,13,45,229,122,159,147,201,156,239,160,224,59,77,174,42,245,176,200,235,187,60,131,83,153,97,23,43,4,126,186,119,214,38,225,105,20,99,85,33,12,125))
_PRIVATE_A = bytes((191,192,216,250,122,246,220,97,31,254,98,27,8,72,71,176,135,99,96,18,127,101,203,104,211,102,191,125,37,72,150,156,51,229,121,35,17,153,141,177,110,131,150,128,172,255,254,6,18,140,55,62,236,249,135,64,135,12,117,4,89,149,168,209))
_PRIVATE_B = bytes((246,204,26,232,232,70,129,109,223,146,169,242,23,241,105,145,50,196,165,42,254,120,3,54,244,207,209,85,53,6,138,106,175,148,31,204,186,186,165,182,87,142,49,10,39,110,26,154,86,56,173,125,18,64,198,225,99,99,83,82,191,134,76,170))
_EXPIRED_CODES = {20101, 20125, 20126, 20315, 20324, 20401, 20403}
_EXE_NAMES = ("Trae CN.exe", "TraeCode CN.exe", "TRAE CN.exe", "Trae.exe")
_PROFILE_NAMES = ("Trae CN", "TraeCode CN", "TRAE SOLO CN", "TRAE Work CN")
_OWNED_PROFILE_MARKER = ".trae-cn-manager-profile"
_MIGRATABLE_KEYS = {AUTH_KEY, "iCubeAuthInfo://usertag", "device_id"}
_DISCOVERY_LOCK = threading.RLock()
_PRODUCT_CACHE = {}
_SHORTCUT_CACHE = {}
_REGISTRY_CACHE = None
_PROCESS_CACHE = None
_RECENT_CACHE = {}
_COMPAT_CACHE = {}


def _stamp(path: Path):
    try:
        info = path.stat()
        return info.st_mtime_ns, info.st_size
    except OSError:
        return None


def _product_metadata(executable: Path) -> dict | None:
    """Cache only public installed-product metadata, invalidated on update."""
    executable = Path(executable)
    product = executable.parent / "resources" / "app" / "product.json"
    stamp = (_stamp(executable), _stamp(product))
    if not stamp[0] or not stamp[1] or executable.suffix.lower() != ".exe":
        return None
    cache_key = (_path_key(executable), stamp)
    with _DISCOVERY_LOCK:
        if cache_key in _PRODUCT_CACHE:
            return _PRODUCT_CACHE[cache_key]
    try:
        doc = _json_file(product)
        result = doc if doc.get("packageType") == "TRAE_CN" and str(doc.get("providerCode", "")).lower() == "cn" else None
    except TraeError:
        result = None
    with _DISCOVERY_LOCK:
        if len(_PRODUCT_CACHE) > 128:
            _PRODUCT_CACHE.clear()
        _PRODUCT_CACHE[cache_key] = result
    return result


def _native_profile(path: Path, *, trusted=False) -> bool:
    """Verify structure without opening account state or project contents."""
    try:
        if not (path / "User" / "globalStorage" / "storage.json").is_file():
            return False
        return trusted or (path / _OWNED_PROFILE_MARKER).is_file() or any((path / name).is_dir() for name in ("aha", "ahanet", "solo-lite", "conversation-tips"))
    except OSError:
        return False


def _user_data_argument(command: str) -> str | None:
    match = re.search(r'(?:^|\s)--user-data-dir(?:=|\s+)(?:"([^"]+)"|([^\s"]+))', command, re.I)
    return (match.group(1) or match.group(2)) if match else None


def _running_trae() -> list[dict]:
    """Query relevant process paths in-process; expose only a user-data flag."""
    global _PROCESS_CACHE
    now = time.monotonic()
    with _DISCOVERY_LOCK:
        if _PROCESS_CACHE and now - _PROCESS_CACHE[0] < 5:
            return _PROCESS_CACHE[1]
    result = []
    if os.name == "nt":
        from ctypes import wintypes
        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
        class UnicodeString(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        kernel.Process32FirstW.restype = kernel.Process32NextW.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot and snapshot != ctypes.c_void_p(-1).value:
            try:
                entry = Entry()
                entry.dwSize = ctypes.sizeof(entry)
                more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
                seen = set()
                while more:
                    if entry.szExeFile.casefold() in {n.casefold() for n in _EXE_NAMES}:
                        process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                        if process:
                            try:
                                image = ctypes.create_unicode_buffer(32768)
                                length = wintypes.DWORD(len(image))
                                if kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(length)):
                                    executable = Path(image.value)
                                    if _product_metadata(executable):
                                        profile = None
                                        try:
                                            query = ctypes.WinDLL("ntdll").NtQueryInformationProcess
                                            query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
                                            query.restype = wintypes.LONG
                                            buffer = ctypes.create_string_buffer(131072)
                                            returned = wintypes.ULONG()
                                            if query(process, 60, buffer, len(buffer), ctypes.byref(returned)) >= 0:
                                                value = UnicodeString.from_buffer(buffer)
                                                start, pointer = ctypes.addressof(buffer), value.Buffer or 0
                                                if value.Length % 2 == 0 and start <= pointer <= start + len(buffer) - value.Length:
                                                    profile = _user_data_argument(ctypes.wstring_at(pointer, value.Length // 2))
                                        except (OSError, ValueError, AttributeError):
                                            pass  # Older Windows: image path still supplies evidence.
                                        key = (_path_key(executable), _path_key(Path(profile)) if profile else "")
                                        if key not in seen:
                                            seen.add(key)
                                            result.append({"executable": str(executable.resolve()), "profile": str(Path(profile).resolve()) if profile else None, "source": "process"})
                            finally:
                                kernel.CloseHandle(process)
                    more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
            finally:
                kernel.CloseHandle(snapshot)
    with _DISCOVERY_LOCK:
        _PROCESS_CACHE = (now, result)
    return result


class TraeError(ValueError):
    """An intentionally credential-free error suitable for the UI."""


def _now() -> str:
    return datetime.now(CN_TIME).isoformat(timespec="seconds")


def _derive_native(seed: bytes, private: bool = False) -> tuple[bytes, bytes]:
    left, right = (_PRIVATE_A, _PRIVATE_B) if private else (_AES_A, _AES_B)
    material = bytes(a ^ b for a, b in zip(left, right))
    derived = hashlib.sha512(hashlib.sha512(seed).digest() + material).digest()
    return derived[:16], derived[16:32]


def decode_native_value(value: str) -> str:
    """Decode the native Base64/AES-CBC container, verifying its digest."""
    try:
        if not isinstance(value, str) or len(value) > MAX_STATE_BYTES:
            raise ValueError
        packed = base64.b64decode("".join(value.split()), validate=True)
        if len(packed) < 54 or packed[:6] not in (_NATIVE_HEADER, _PRIVATE_HEADER):
            raise ValueError
        ciphertext = packed[38:]
        if not ciphertext or len(ciphertext) % 16:
            raise ValueError
        key, iv = _derive_native(packed[6:38], packed[:6] == _PRIVATE_HEADER)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        if len(plain) < 64 or not hmac.compare_digest(plain[:64], hashlib.sha512(plain[64:]).digest()):
            raise ValueError
        return plain[64:].decode("utf-8")
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise TraeError("原生登录存储格式不受支持或已损坏，请在官方客户端重新登录。") from None


def encode_native_value(value: str) -> str:
    """Native-compatible codec for synthetic fixtures and interoperability.

    This function does not write client files. Application-owned secrets use the
    vault's stronger, user-scoped encryption instead of this legacy format.
    """
    raw = value.encode("utf-8")
    seed = os.urandom(32)
    key, iv = _derive_native(seed)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(hashlib.sha512(raw).digest() + raw) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(_NATIVE_HEADER + seed + encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def _json_file(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            raise TraeError("客户端状态文件过大，已停止读取。")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except TraeError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise TraeError("无法读取客户端状态，请确认所选资料目录正确且已完成官方登录。") from None


def _hidden_powershell(script: str) -> str:
    if os.name != "nt":
        return ""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, timeout=12, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _shortcut_target(path: Path) -> Path | None:
    path = Path(path)
    stamp = _stamp(path)
    if path.suffix.lower() != ".lnk" or not stamp:
        return None
    cache_key = (_path_key(path), stamp)
    with _DISCOVERY_LOCK:
        if cache_key in _SHORTCUT_CACHE:
            return _SHORTCUT_CACHE[cache_key]
    target = None
    # Common local Shell Links carry a Unicode LinkInfo target. Parse the public
    # shortcut format directly, avoiding PowerShell startup per shortcut.
    try:
        if stamp[1] > 1024 * 1024:
            return None
        raw = path.read_bytes()
        if len(raw) < 76 or raw[:4] != b"L\x00\x00\x00":
            raise ValueError()
        flags = struct.unpack_from("<I", raw, 20)[0]
        offset = 76
        if flags & 1:
            offset += 2 + struct.unpack_from("<H", raw, offset)[0]
        if flags & 2:
            total, header = struct.unpack_from("<II", raw, offset)
            if not 28 <= header <= total or offset + total > len(raw):
                raise ValueError()
            base_offset = struct.unpack_from("<I", raw, offset + 28)[0] if header >= 36 else 0
            if base_offset:
                start = offset + base_offset
                end = start
                while end + 1 < offset + total and raw[end:end + 2] != b"\x00\x00":
                    end += 2
                text = raw[start:end].decode("utf-16le")
            else:
                base_offset = struct.unpack_from("<I", raw, offset + 16)[0]
                start = offset + base_offset
                end = raw.index(0, start, offset + total)
                text = raw[start:end].decode("mbcs" if os.name == "nt" else "utf-8")
            if text:
                target = Path(text)
    except (OSError, ValueError, UnicodeError, IndexError, struct.error):
        pass
    if target is None:
        quoted = str(path).replace("'", "''")
        text = _hidden_powershell(
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
            f"$taskLink=(New-Object -ComObject WScript.Shell).CreateShortcut('{quoted}');"
            "[Console]::Write($taskLink.TargetPath)"
        )
        target = Path(text) if text else None
    with _DISCOVERY_LOCK:
        if len(_SHORTCUT_CACHE) > 128:
            _SHORTCUT_CACHE.clear()
        _SHORTCUT_CACHE[cache_key] = target
    return target


def _registry_candidates() -> tuple[list[Path], list[Path]]:
    global _REGISTRY_CACHE
    now = time.monotonic()
    with _DISCOVERY_LOCK:
        if _REGISTRY_CACHE and now - _REGISTRY_CACHE[0] < 60:
            cached_candidates, cached_desktops = _REGISTRY_CACHE[1]
            return list(cached_candidates), list(cached_desktops)
    candidates: list[Path] = []
    desktops: list[Path] = []
    if winreg is None:
        return candidates, desktops
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            desktops.append(Path(os.path.expandvars(winreg.QueryValueEx(key, "Desktop")[0])))
    except OSError:
        pass
    for base, name in ((winreg.HKEY_CURRENT_USER, "Programs"), (winreg.HKEY_LOCAL_MACHINE, "Common Programs")):
        try:
            with winreg.OpenKey(base, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                desktops.append(Path(os.path.expandvars(winreg.QueryValueEx(key, name)[0])))
        except OSError:
            pass
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (0, getattr(winreg, "KEY_WOW64_32KEY", 0), getattr(winreg, "KEY_WOW64_64KEY", 0)):
            try:
                with winreg.OpenKey(hive, uninstall, 0, winreg.KEY_READ | view) as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as sub:
                                name = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                                if not re.search(r"(?i)trae.*(?:cn|中国|国内)", name):
                                    continue
                                for field in ("InstallLocation", "DisplayIcon"):
                                    try:
                                        raw = str(winreg.QueryValueEx(sub, field)[0]).strip()
                                    except OSError:
                                        continue
                                    if field == "DisplayIcon":
                                        match = re.match(r'^"([^"]+)"|^(.+?\.exe)(?:,.*)?$', raw, re.I)
                                        if match:
                                            candidates.append(Path(match.group(1) or match.group(2)))
                                    elif raw:
                                        candidates.extend(Path(raw) / exe for exe in _EXE_NAMES)
                        except OSError:
                            continue
            except OSError:
                continue
    with _DISCOVERY_LOCK:
        _REGISTRY_CACHE = (now, (candidates, desktops))
    return list(candidates), list(desktops)


def _recent_projects(profiles: list[dict[str, str]]) -> list[str]:
    """Select only the recent-path record; never read account records here."""
    cache_key = tuple((_path_key(Path(p["path"])), _stamp(Path(p["path"]) / "User" / "globalStorage" / "state.vscdb")) for p in profiles)
    with _DISCOVERY_LOCK:
        cached = _RECENT_CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] < 30:
            return list(cached[1])
    result: list[str] = []
    seen: set[str] = set()
    for profile in profiles:
        database = Path(profile["path"]) / "User" / "globalStorage" / "state.vscdb"
        if not database.is_file():
            continue
        try:
            uri = database.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1) as connection:
                row = connection.execute("SELECT value FROM ItemTable WHERE key=?", ("history.recentlyOpenedPathsList",)).fetchone()
            if not row:
                continue
            data = json.loads(row[0])
            entries = data.get("entries", []) if isinstance(data, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                value = entry.get("folderUri")
                workspace = entry.get("workspace")
                if not value and isinstance(workspace, dict):
                    value = workspace.get("configPath")
                if not isinstance(value, str) or not value.startswith("file:"):
                    continue
                from urllib.parse import unquote, urlsplit
                parsed = urlsplit(value)
                local = unquote(parsed.path)
                if parsed.netloc:
                    local = "//" + parsed.netloc + local
                elif re.match(r"^/[A-Za-z]:", local):
                    local = local[1:]
                candidate = Path(local)
                if candidate.is_dir() and _path_key(candidate) not in seen:
                    seen.add(_path_key(candidate))
                    result.append(str(candidate.resolve()))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            continue
    result = result[:50]
    with _DISCOVERY_LOCK:
        if len(_RECENT_CACHE) > 32:
            _RECENT_CACHE.clear()
        _RECENT_CACHE[cache_key] = (time.monotonic(), result)
    return result


def _finite_number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _expiry_passed(login: dict[str, Any]) -> bool:
    value = login.get("expiredAt")
    if isinstance(value, str):
        try:
            expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry <= datetime.now(timezone.utc)
        except ValueError:
            pass
    return False


class _ApiError(Exception):
    def __init__(self, state: str, message: str):
        self.state, self.message = state, message
        super().__init__(message)


class TraeClient:
    def __init__(self, executable: Path | None = None):
        self.executable = Path(executable) if executable else None
        self.session = requests.Session()
        self._operation_lock = threading.RLock()
        self._launched_profiles: dict[str, subprocess.Popen] = {}
        self.version = ""
        if self.executable:
            try:
                product = _json_file(self.executable.parent / "resources" / "app" / "product.json")
                self.version = str(product.get("appVersion") or "")
            except TraeError:
                pass

    def _compatibility(self) -> dict[str, str]:
        if not self.executable:
            return {"state": "missing", "message": "未检测到官方客户端，请选择安装位置。"}
        root = self.executable.parent / "resources" / "app"
        cache_key = (_path_key(self.executable), _stamp(self.executable), _stamp(root / "product.json"), _stamp(root / "out" / "main.js"))
        with _DISCOVERY_LOCK:
            if cache_key in _COMPAT_CACHE:
                return dict(_COMPAT_CACHE[cache_key])
        try:
            product = _json_file(root / "product.json")
            main = root / "out" / "main.js"
            if main.stat().st_size > 32 * 1024 * 1024:
                raise OSError
            source = main.read_text(encoding="utf-8")
            markers = (STATUS_PATH, CLAIM_PATH, USAGE_PATH, "_requestCheckinCredits", "userStorage.js", "byteCrypto.js", "getDeviceKeyPair", "storeUserInfo")
            matching = all(marker in source for marker in markers)
            official = product.get("bootConfig", {}).get("ug", {}).get("trae", {}).get("normal") == API_ORIGIN
            baseline = (product.get("appVersion") in VERIFIED_NATIVE_VERSIONS and product.get("packageType") == "TRAE_CN" and product.get("providerCode") == "cn" and matching and official)
            if baseline:
                result = {"state": "baseline", "message": f"已识别 {product.get('appVersion')} 原生接口与存储结构基线；真实登录、签到资格和服务端结果仍以实际查询为准。"}
            else:
                result = {"state": "unverified", "message": "客户端版本或原生接口/存储结构已变化，等待适配验证；请先通过官方客户端核对结果。"}
        except (TraeError, OSError, UnicodeError, TypeError, AttributeError):
            result = {"state": "unverified", "message": "未能验证此客户端的原生接口与存储结构，请检查版本并等待适配。"}
        with _DISCOVERY_LOCK:
            if len(_COMPAT_CACHE) > 32:
                _COMPAT_CACHE.clear()
            _COMPAT_CACHE[cache_key] = result
        return dict(result)

    def discover(self) -> dict[str, Any]:
        started = time.monotonic()
        home = Path(os.environ.get("USERPROFILE", str(Path.home())))
        executable = None
        source = "missing"
        if self.executable and _product_metadata(self.executable):
            executable, source = self.executable.resolve(), "cache"
        running = _running_trae()
        if executable is None:
            if running:
                executable, source = Path(running[0]["executable"]), "process"
        if executable is None:
            candidates, desktops = _registry_candidates()
            for candidate in candidates:
                if _product_metadata(candidate):
                    executable, source = candidate.resolve(), "registry"
                    break
            if executable is None:
                desktops.extend((home / "Desktop", Path(r"E:\Users") / home.name / "Desktop", Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "Microsoft" / "Windows" / "Start Menu" / "Programs"))
                shortcuts = []
                seen_dirs = set()
                for desktop in desktops:
                    if not desktop.is_dir() or _path_key(desktop) in seen_dirs:
                        continue
                    seen_dirs.add(_path_key(desktop))
                    # Bounded Desktop/Programs and one product subfolder only.
                    shortcuts.extend(desktop.glob("*Trae*.lnk"))
                    for folder in desktop.glob("*Trae*"):
                        if folder.is_dir():
                            shortcuts.extend(folder.glob("*.lnk"))
                for shortcut in shortcuts[:32]:
                    target = _shortcut_target(shortcut)
                    if target and _product_metadata(target):
                        executable, source = target.resolve(), "shortcut"
                        break
            if executable is None:
                for root in (Path(r"E:\Trae CN"), Path(r"D:\Trae CN"), Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))) / "Programs" / "Trae CN", Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Trae CN"):
                    for name in _EXE_NAMES:
                        candidate = root / name
                        if _product_metadata(candidate):
                            executable, source = candidate.resolve(), "known"
                            break
                    if executable:
                        break
        self.executable = executable
        product = _product_metadata(executable) if executable else None
        version = str(product.get("appVersion") or "") if product else ""
        self.version = version
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        profiles: list[dict[str, str]] = []
        seen: set[str] = set()
        candidates = [(Path(row["profile"]), "process", True) for row in running if row.get("profile")]
        candidates.extend((appdata / name, "known", False) for name in _PROFILE_NAMES)
        for candidate, profile_source, trusted in candidates:
            if _native_profile(candidate, trusted=trusted):
                resolved = _path_key(candidate)
                if resolved not in seen:
                    seen.add(resolved)
                    profiles.append({"name": candidate.name, "path": str(candidate.resolve()), "source": profile_source, "confidence": "native"})
        if executable and not profiles:
            # A cached EXE may already be open with an isolated data directory.
            for row in _running_trae():
                if row.get("profile") and _native_profile(Path(row["profile"]), trusted=True):
                    candidate = Path(row["profile"]).resolve()
                    if _path_key(candidate) not in seen:
                        seen.add(_path_key(candidate))
                        profiles.append({"name": candidate.name, "path": str(candidate), "source": "process", "confidence": "native"})
        return {"executable": str(executable) if executable else None, "executable_source": source, "version": version, "profiles": profiles, "global_home": str((home / ".trae-cn").resolve()), "global_home_source": "known", "recent_projects": _recent_projects(profiles), "compatibility": self._compatibility(), "quick_complete": bool(executable and profiles), "quick_status": "found" if executable else "missing", "discovery_ms": round((time.monotonic() - started) * 1000, 2)}

    def inspect_profile(self, profile: Path) -> dict[str, Any]:
        profile = Path(profile).expanduser().resolve()
        state = _json_file(profile / "User" / "globalStorage" / "storage.json")
        encoded = state.get(AUTH_KEY)
        if not isinstance(encoded, str) or not encoded:
            raise TraeError("该资料目录尚未登录，请先在官方客户端完成手机号登录。")
        try:
            login = json.loads(decode_native_value(encoded))
        except TraeError:
            raise
        except (TypeError, ValueError):
            raise TraeError("客户端登录对象格式无效，请重新登录。") from None
        if not isinstance(login, dict) or not isinstance(login.get("userId"), (str, int)) or isinstance(login.get("userId"), bool) or not login.get("userId") or not isinstance(login.get("account"), dict) or not isinstance(login.get("token"), str) or not login["token"]:
            raise TraeError("客户端登录对象不完整，请重新登录。")
        account = login["account"]
        if account.get("scope") != "marscode":
            raise TraeError("当前登录属于其他账户体系，此版本仅支持 TRAE CN 个人账户。")
        host = str(login.get("host") or API_ORIGIN).lower().rstrip("/")
        if host not in (API_ORIGIN, "https://api.trae.com.cn"):
            raise TraeError("当前登录不属于已支持的国内版官方服务。")
        pairs: dict[str, dict[str, Any]] = {}
        for key, value in state.items():
            if not key.startswith(DEVICE_KEY_PREFIX) or not isinstance(value, str):
                continue
            device = key[len(DEVICE_KEY_PREFIX):]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device) or device == "0":
                continue
            try:
                pair = json.loads(decode_native_value(value))
                if isinstance(pair, dict) and pair.get("privateKeyPEM") and pair.get("publicKeyPEM"):
                    pairs[device] = pair
            except (TraeError, ValueError, TypeError):
                continue
        def valid_device(value: Any) -> str:
            return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) != "0" and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(value)) else ""
        login_device = valid_device(login.get("deviceId") or login.get("device_id"))
        legacy_device = valid_device(state.get("device_id"))
        # 3.4.3 moves device registration into AHA and removes legacy device_id.
        # A stale leftover must not override the sole native signing-key record.
        matching = next((value for value in (login_device, legacy_device) if value in pairs), "")
        if matching:
            device_id, device_source = matching, "native-keypair"
        elif len(pairs) == 1:
            device_id, device_source = next(iter(pairs)), "native-keypair"
        elif not pairs and login_device:
            device_id, device_source = login_device, "login-record"
        elif not pairs and legacy_device and state.get("has_device_id_updated_to_aha") not in (True, "true"):
            device_id, device_source = legacy_device, "legacy-state"
        else:
            device_id, device_source = "", "unknown"
        label = str(account.get("username") or account.get("nonPlainTextMobile") or f"TRAE {str(login['userId'])[-6:]}")[:100]
        # Retain only authentication-related native records inside the vault.
        # Do not include settings, chat history, recent paths or the whole state.
        storage_keys = {AUTH_KEY: encoded}
        for key in ("iCubeAuthInfo://usertag", "device_id"):
            if key in state:
                storage_keys[key] = state[key]
        for device in pairs:
            storage_keys[DEVICE_KEY_PREFIX + device] = state[DEVICE_KEY_PREFIX + device]
        return {"schema_version": DATA_SCHEMA, "login": login, "device_id": device_id, "device_id_source": device_source, "device_key_pairs": pairs, "storage_keys": storage_keys, "profile_path": str(profile), "label": label, "user_id": str(login["userId"]), "device_verified": False, "device_available": bool(device_id), "warning": "" if device_id else "未能唯一确定官方注册设备，请在官方客户端重新登录后重新采集。"}

    @staticmethod
    def _ensure_owned_profile(profile: Path) -> Path:
        profile = Path(profile).expanduser().resolve()
        home = Path(os.environ.get("USERPROFILE", str(Path.home())))
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        if any(_path_key(profile) == _path_key(appdata / name) for name in _PROFILE_NAMES):
            raise TraeError("不能将账号恢复到官方默认资料目录，请选择管理器的独立实例。")
        marker = profile / _OWNED_PROFILE_MARKER
        if profile.exists() and not profile.is_dir():
            raise TraeError("实例路径不是文件夹。")
        if profile.exists() and not marker.is_file():
            # An empty User directory is what a newly created manager instance
            # contains before the official application has ever been launched.
            entries = list(profile.iterdir())
            empty_user = len(entries) == 1 and entries[0].name == "User" and entries[0].is_dir() and not any(entries[0].iterdir())
            if entries and not empty_user:
                raise TraeError("目标目录包含既有资料且未由本管理器创建，已停止恢复。")
        profile.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            marker.write_text('{"schema_version":1}', encoding="utf-8")
        return profile

    def restore_account(self, account: dict[str, Any], profile: Path) -> dict[str, str]:
        """Restore native auth only to an empty or explicitly manager-owned dir.

        The caller chooses a manager instance. This never alters the source or
        replaces a whole profile. A migrated device may require official login.
        """
        snapshot = account.get("credentials", account) if isinstance(account, dict) else None
        login = snapshot.get("login") if isinstance(snapshot, dict) else None
        if not isinstance(login, dict) or not login.get("userId") or not isinstance(login.get("token"), str) or not login["token"] or not isinstance(login.get("account"), dict) or login["account"].get("scope") != "marscode":
            raise TraeError("账号备份不包含受支持的原生登录档案。")
        if str(login.get("host") or API_ORIGIN).lower().rstrip("/") not in (API_ORIGIN, "https://api.trae.com.cn"):
            raise TraeError("账号备份的服务地址不属于已支持的国内版官方服务。")
        with self._operation_lock:
            running = self._launched_profiles.get(_path_key(Path(profile)))
            if running is not None and running.poll() is None:
                raise TraeError("该独立实例仍在运行，请先保存工作并关闭实例后恢复。")
            profile = self._ensure_owned_profile(profile)
            directory = profile / "User" / "globalStorage"
            if not directory.resolve().is_relative_to(profile):
                raise TraeError("实例内部目录指向外部位置，已停止恢复以保护其他客户端资料。")
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "storage.json"
            if target.exists() and not target.resolve().is_relative_to(profile):
                raise TraeError("实例状态文件指向外部位置，已停止恢复。")
            state = _json_file(target) if target.is_file() else {}
            updates: dict[str, Any] = {AUTH_KEY: encode_native_value(json.dumps(login, ensure_ascii=False, separators=(",", ":")))}
            original = snapshot.get("storage_keys", {})
            if isinstance(original, dict):
                for key, value in original.items():
                    if not isinstance(key, str) or (key not in _MIGRATABLE_KEYS and not key.startswith(DEVICE_KEY_PREFIX)):
                        continue
                    if key == AUTH_KEY:
                        continue
                    if key == "device_id":
                        if isinstance(value, (str, int)) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(value)):
                            updates[key] = value
                    elif isinstance(value, str):
                        try:
                            decoded = json.loads(decode_native_value(value))
                        except (TraeError, ValueError, TypeError):
                            continue
                        if isinstance(decoded, dict):
                            if key == "iCubeAuthInfo://usertag":
                                user_id = str(login["userId"])
                                if user_id in decoded:
                                    previous: dict[str, Any] = {}
                                    if isinstance(state.get(key), str):
                                        try:
                                            parsed = json.loads(decode_native_value(state[key]))
                                            if isinstance(parsed, dict):
                                                previous = parsed
                                        except (TraeError, ValueError, TypeError):
                                            pass
                                    previous[user_id] = decoded[user_id]
                                    updates[key] = encode_native_value(json.dumps(previous, ensure_ascii=False))
                            elif decoded.get("privateKeyPEM") and decoded.get("publicKeyPEM"):
                                updates[key] = value
            pairs = snapshot.get("device_key_pairs", {})
            if isinstance(pairs, dict):
                for device, pair in pairs.items():
                    if isinstance(device, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device) and isinstance(pair, dict) and pair.get("privateKeyPEM") and pair.get("publicKeyPEM"):
                        updates[DEVICE_KEY_PREFIX + device] = encode_native_value(json.dumps(pair))
            # This is the same startup flag set by TRAE's own migration service.
            state.update(updates)
            state["iCubeNativeAppStartSetup"] = True
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=".manager-auth-", suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                temporary = None
            except (OSError, ValueError, TypeError):
                raise TraeError("恢复未完成，原登录存储已保留。请关闭该独立实例后重试。") from None
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
        message = "已恢复到独立实例。跨电脑或设备绑定变化可能需要在官方客户端重新登录；请启动后核对账户。"
        if _expiry_passed(login):
            message = "已恢复到独立实例，但备份登录已过期；请在官方客户端重新登录后重新采集。"
        return {"state": "restored", "profile_path": str(profile), "message": message}

    @staticmethod
    def _snapshot(account: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(account, dict):
            raise _ApiError("unavailable", "账号档案格式无效。")
        snapshot = account.get("credentials", account)
        if not isinstance(snapshot, dict):
            raise _ApiError("unavailable", "账号档案格式无效。")
        login = snapshot.get("login")
        if not isinstance(login, dict) or not isinstance(login.get("account"), dict) or not isinstance(login.get("token"), str) or not login["token"]:
            raise _ApiError("expired", "未保存有效登录，请在官方客户端登录并重新采集。")
        if login["account"].get("scope") != "marscode":
            raise _ApiError("unavailable", "此账户体系暂不支持签到或积分查询。")
        if _expiry_passed(login):
            raise _ApiError("expired", "登录已过期，请在官方客户端重新登录后重新采集。")
        return snapshot

    @staticmethod
    def _validate_business(payload: dict[str, Any]) -> None:
        metadata = payload.get("ResponseMetadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("Error"), dict):
            raw_code = metadata["Error"].get("Code")
            if isinstance(raw_code, (int, str)) and str(raw_code).isdigit() and int(raw_code) in _EXPIRED_CODES:
                raise _ApiError("expired", "登录已失效，请在官方客户端重新登录后重新采集。")
            raise _ApiError("error", "官方认证接口拒绝请求，请在官方客户端检查登录状态。")
        if "code" not in payload:
            return
        code = payload["code"]
        if isinstance(code, bool) or not isinstance(code, int):
            raise _ApiError("error", "官方接口返回了无法识别的业务状态。")
        if code in _EXPIRED_CODES:
            raise _ApiError("expired", "登录已失效，请在官方客户端重新登录后重新采集。")
        if code in (9074, 9095):
            raise _ApiError("unavailable", f"官方设备校验未通过（{code}），请在官方客户端查看签到结果。")
        if code != 0:
            raise _ApiError("error", f"官方接口拒绝请求（业务状态 {code}）。")

    def _post(self, path: str, snapshot: dict[str, Any], body: dict[str, Any], *, device: bool = False) -> dict[str, Any]:
        if path not in (STATUS_PATH, CLAIM_PATH, USAGE_PATH):
            raise _ApiError("error", "请求路径未获支持。")
        headers = {"Content-Type": "application/json", "Authorization": "Cloud-IDE-JWT " + snapshot["login"]["token"], "User-Agent": "TraeCNManager/1.0", "X-User-Region": "cn"}
        if self.version and re.fullmatch(r"[0-9A-Za-z.+_-]{1,64}", self.version):
            headers["x-app-version"] = self.version
        if device:
            identifier = snapshot.get("device_id")
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier) or identifier == "0":
                raise _ApiError("unavailable", "缺少可确认的官方注册设备，请先在官方客户端登录并重新采集。")
            headers["x-device-id"] = identifier
        try:
            response = self.session.post(API_ORIGIN + path, headers=headers, json=body, timeout=(5, 20), allow_redirects=False)
            if response.status_code == 401:
                raise _ApiError("expired", "登录已失效，请在官方客户端重新登录后重新采集。")
            if response.status_code == 403:
                raise _ApiError("unavailable", "官方服务拒绝访问，请在官方客户端检查账户状态。")
            if response.status_code != 200:
                raise _ApiError("error", f"官方服务请求失败（HTTP {int(response.status_code)}）。")
            payload = response.json()
            if not isinstance(payload, dict):
                raise _ApiError("error", "官方接口返回的数据格式无效。")
            self._validate_business(payload)
            if isinstance(payload.get("data"), dict):
                payload = payload["data"]
                self._validate_business(payload)
            return payload
        except _ApiError:
            raise
        except requests.Timeout:
            raise _ApiError("error", "请求超时，请稍后重试。") from None
        except requests.RequestException:
            raise _ApiError("error", "无法连接官方服务，请检查网络后重试。") from None
        except (ValueError, TypeError, OverflowError):
            raise _ApiError("error", "官方接口返回的数据无法解析。") from None

    def _status(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        data = self._post(STATUS_PATH, snapshot, {"req_source": 1}, device=True)
        if not isinstance(data.get("enable"), bool) or not isinstance(data.get("checked_in"), bool):
            raise _ApiError("error", "签到状态响应不完整，未执行领取。")
        return data

    def checkin_status(self, account: dict[str, Any]) -> dict[str, Any]:
        """Read today's official status without ever submitting a claim."""
        result: dict[str, Any] = {"state": "error", "message": "签到状态暂未更新。",
                                  "checked_at": _now(), "checked_in": None, "enabled": None}
        with self._operation_lock:
            try:
                snapshot = self._snapshot(account)
                status = self._status(snapshot)
                result.update(checked_in=status["checked_in"], enabled=status["enable"])
                if status["checked_in"]:
                    result.update(state="already", message="今日已签到。")
                elif status["enable"]:
                    result.update(state="pending", message="今日尚未签到。")
                else:
                    result.update(state="unavailable", message="官方当前未向此账号开放签到。")
            except _ApiError as error:
                result.update(state=error.state, message=error.message)
        return result

    def checkin(self, account: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"state": "error", "message": "签到尚未完成。", "checked_at": _now()}
        with self._operation_lock:
            try:
                snapshot = self._snapshot(account)
                before = self._status(snapshot)
                if before["checked_in"]:
                    return {**result, "state": "already", "message": "今日已签到。"}
                if not before["enable"]:
                    return {**result, "state": "unavailable", "message": "官方当前未向此账号开放签到。"}
                claimed = self._post(CLAIM_PATH, snapshot, {"req_source": 1}, device=True)
                after = self._status(snapshot)
                if not after["checked_in"]:
                    raise _ApiError("error", "领取后未确认已签到，请在官方客户端检查，暂未记为成功。")
                result.update(state="success", message="签到成功，已通过官方状态复查。")
                credits = _finite_number(claimed.get("credits"))
                if credits is not None and credits >= 0:
                    result["credits"] = credits
            except _ApiError as error:
                result.update(state=error.state, message=error.message)
        return result

    def usage(self, account: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"remaining": None, "limit": None, "used": None, "plan": "未知", "message": "尚未查询。", "updated_at": _now(), "unlimited": False}
        with self._operation_lock:
            try:
                snapshot = self._snapshot(account)
                data = self._post(USAGE_PATH, snapshot, {"require_usage": True, "req_source": 1})
                packs = data.get("user_entitlement_pack_list")
                if not isinstance(packs, list):
                    raise _ApiError("error", "官方未返回积分权益列表，余额未知。")
                total_limit = total_used = total_remaining = 0
                found = unlimited = incomplete = False
                types: set[int] = set()
                for pack in packs:
                    if not isinstance(pack, dict):
                        continue
                    base = pack.get("entitlement_base_info")
                    if not isinstance(base, dict):
                        continue
                    product_type = base.get("product_type")
                    if isinstance(product_type, int) and not isinstance(product_type, bool):
                        types.add(product_type)
                    quota = base.get("quota")
                    usage = pack.get("usage")
                    limit = _finite_number(quota.get("credits_limit")) if isinstance(quota, dict) else None
                    # The 3.4.3 native cnCreditsUsage Gqe uses
                    # a.usage?.credits_amount ?? 0: a newly granted credit pack
                    # legitimately has usage={} until its first consumption.
                    # This default is only applied after recognizing a numeric
                    # credit quota, never to an absent/unknown balance response.
                    if usage is None:
                        used = 0
                    elif isinstance(usage, dict):
                        amount = usage.get("credits_amount")
                        used = 0 if amount is None else _finite_number(amount)
                    else:
                        used = None
                    if limit is None or limit == 0:
                        continue
                    if limit < -1 or used is None or used < 0:
                        incomplete = True
                        continue
                    found = True
                    total_used += used
                    if limit == -1:
                        unlimited = True
                    elif limit > 0:
                        total_limit += limit
                        total_remaining += max(limit - used, 0)
                names = {0: "免费", 1: "Pro", 2: "积分包", 3: "活动权益", 4: "Pro+", 5: "Pro+ 积分包", 6: "Ultra", 7: "按量付费", 8: "Lite", 9: "邀请权益", 100: "CN Express"}
                labels = [names[k] for k in sorted(types) if k in names]
                result["plan"] = " / ".join(labels) if labels else "账户权益"
                if incomplete:
                    raise _ApiError("error", "部分积分权益数据缺失，未显示可能不准确的余额。")
                if not found:
                    raise _ApiError("unavailable", "官方尚未返回可计算的积分额度，余额未知。")
                result.update(used=total_used, limit=None if unlimited else total_limit, remaining=None if unlimited else total_remaining, unlimited=unlimited, message="不限量积分权益。" if unlimited else "积分余额已从官方接口更新。")
            except _ApiError as error:
                result.update(message=error.message, state=error.state)
        return result

    def launch(self, profile: Path, new_window: bool = True) -> subprocess.Popen:
        if not self.executable or not self.executable.is_file():
            self.discover()
        if not self.executable or not self.executable.is_file():
            raise TraeError("未找到 TRAE CN，请先在设置中选择官方客户端 EXE。")
        profile = Path(profile).expanduser().resolve()
        try:
            # Default/source profiles can be launched as-is. A genuinely new
            # profile receives only an ownership marker and an empty User dir.
            if not profile.exists() or not any(profile.iterdir()):
                self._ensure_owned_profile(profile)
            (profile / "User").mkdir(parents=True, exist_ok=True)
            command = [str(self.executable), "--user-data-dir", str(profile)]
            if new_window:
                command.append("--new-window")
            process = subprocess.Popen(command, shell=False, close_fds=True)
            self._launched_profiles[_path_key(profile)] = process
            return process
        except OSError:
            raise TraeError("无法启动官方客户端，请检查程序与资料目录权限。") from None

    def launch_account(self, account: dict[str, Any], new_window: bool = True) -> subprocess.Popen:
        """Open the original account profile without cloning or overwriting it."""
        snapshot = account.get("credentials", account) if isinstance(account, dict) else None
        source = snapshot.get("profile_path") if isinstance(snapshot, dict) else None
        if not isinstance(source, str) or not Path(source).is_dir():
            raise TraeError("此账号的原实例不存在，请先恢复到新的管理器实例。")
        native_login = snapshot.get("login")
        expected_user = snapshot.get("user_id") or (native_login.get("userId") if isinstance(native_login, dict) else None)
        if expected_user and (Path(source) / "User" / "globalStorage" / "storage.json").is_file():
            try:
                current = self.inspect_profile(Path(source))
            except TraeError:
                current = None  # Opening a logged-out instance permits official login.
            if current and str(current.get("user_id")) != str(expected_user):
                raise TraeError("原实例当前登录了另一个账号，请将所选账号恢复到独立实例后启动。")
        return self.launch(Path(source), new_window=new_window)
