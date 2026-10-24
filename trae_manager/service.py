"""Application services. UI receives summaries; credential material stays in the vault."""
from __future__ import annotations

import base64
import copy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import uuid

from . import __version__
from .configs import ConfigManager
from .platforms import (cn_now, due_today, validate_time, configure_schedule,
                        configure_startup, check_release, task_name, start_background, query_scheduler)
from .security import (Vault, atomic_write, encrypt_package, decrypt_package,
                       windows_protect, file_lock)
from .trae import TraeClient
from .recovery import RecoveryManager
from .locations import LocationManager
from .memory import MemoryManager
from .translation_service import TranslationServiceMixin

DEFAULT_SETTINGS = {"schedule_time": "09:00", "scheduled_enabled": False,
                    "auto_checkin": True, "start_at_login": False, "theme": "dark",
                    "github_repository": "BuildWithAIHub/Trae-CN-Manager", "lock_minutes": 15, "schedule_backend": "off",
                    "usage_auto_refresh": True, "usage_refresh_seconds": 15, "skills_root": ""}


def _clean(message) -> str:
    value = str(message)
    value = re.sub(r"(?i)(Cloud-IDE-JWT|Bearer)\s+[^\s\"']+", r"\1 [已隐藏]", value)
    value = re.sub(r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[令牌已隐藏]", value)
    value = re.sub(r"(?i)((?:token|password|secret|authorization|api[_-]?key)\s*[=:]\s*)[^\s,;]+", r"\1[已隐藏]", value)
    return value[:1500]


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


class _AuditedConfigs:
    _changes = {"save_mcp", "upsert_mcp", "set_mcp_enabled", "delete_mcp", "import_mcp",
                "save_item", "toggle_item", "remove_item", "import_skill", "import_github_skill", "restore_backup",
                "save_skill_file", "create_skill_directory", "set_skill_entry_enabled", "delete_skill_entry",
                "set_items_enabled", "delete_items", "set_mcp_servers_enabled", "delete_mcp_servers",
                "import_mcp_url", "import_skill_url", "configure_skills_root"}
    _changes.add("save_existing_item")

    def __init__(self, manager, controller):
        self._manager = manager
        self._controller = controller

    def __getattr__(self, name):
        value = getattr(self._manager, name)
        if name not in self._changes:
            return value
        def operation(*args, **kwargs):
            if not self._controller.is_unlocked:
                raise ValueError("请先解锁账号库")
            try:
                result = value(*args, **kwargs)
            except Exception:
                with self._controller.vault.transaction() as doc:
                    self._controller._record(doc, "配置变更", name + " 未完成，原配置或备份已保留", "error")
                raise
            with self._controller.vault.transaction() as doc:
                self._controller._record(doc, "配置变更", name + " 已完成（配置值不写入日志）")
            return result
        return operation


class Controller(TranslationServiceMixin):
    def __init__(self, data_dir: Path, *, client=None, discovery=None):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.vault = Vault(self.data_dir)
        self.recovery = RecoveryManager(self.vault)
        self.client = client or TraeClient()
        self.discovery = discovery or self.client.discover()
        self.locations = LocationManager(self.data_dir, self.client)
        self._scan_cancel = threading.Event()
        self.global_home = Path(self.discovery.get("global_home") or Path.home() / ".trae-cn")
        self.configs = _AuditedConfigs(ConfigManager(self.global_home, self.data_dir / "config-backups"), self)
        self.memory = MemoryManager(self.global_home)
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._scheduler = None
        self._last_attempt = None

    @property
    def initialized(self):
        return self.vault.initialized

    @property
    def is_unlocked(self):
        return self.vault.unlocked

    @property
    def recovery_available(self):
        return self.recovery.available

    def create_recovery_code(self):
        with self._mutex:
            code = self.recovery.create_code()
            with self.vault.transaction() as doc:
                self._record(doc, "密码恢复", "已生成新的恢复码，旧恢复码失效；恢复码不写入日志")
            return code

    def recover_password(self, code, new_password):
        with self._mutex:
            def background(new_key):
                return {self.data_dir / "background.key": windows_protect(new_key)} if (self.data_dir / "background.key").exists() else {}
            self.recovery.reset(code, new_password, extra_builder=background)
            with self.vault.transaction() as doc:
                self._record(doc, "密码恢复", "已使用恢复码重设密码，账号和配置记录已保留")
            self._start_scheduler()

    def _bind_locations(self, discovery):
        self.discovery = discovery
        home = Path(discovery.get("global_home") or self.global_home)
        if home.resolve() != self.global_home.resolve():
            self.global_home = home
            self.configs = _AuditedConfigs(ConfigManager(home, self.data_dir / "config-backups"), self)
            self.memory = MemoryManager(home)
        return discovery

    def location_state(self):
        return self._bind_locations(self.locations.inspect())

    def allow_location_scan(self, allowed):
        self.locations.consent(bool(allowed))

    def acknowledge_location(self, notice_id):
        self.locations.acknowledge(notice_id)

    def scan_locations(self):
        self._scan_cancel.clear()
        return self._bind_locations(self.locations.scan(cancel=self._scan_cancel))

    def cancel_location_scan(self):
        self._scan_cancel.set()

    def set_locations(self, executable=None, profile=None, global_home=None, cache=None):
        return self._bind_locations(self.locations.set_manual(executable=executable, profile=profile, global_home=global_home, cache=cache))

    def set_skills_root(self, path_or_none):
        if not self.is_unlocked:
            raise ValueError("请先解锁")
        result = self.configs.configure_skills_root(Path(path_or_none) if path_or_none else None)
        with self.vault.transaction() as doc:
            doc["settings"]["skills_root"] = str(result["root"])
            self._record(doc, "Skills 存储", "已设置并验证 Skills 原生读取位置")
        return result

    def setup_password(self, password: str):
        initial = {"schema_version": 1, "accounts": [], "projects": [],
                   "settings": copy.deepcopy(DEFAULT_SETTINGS), "logs": [], "pending_resources": []}
        self.vault.create(password, initial)
        self._start_scheduler()

    def unlock(self, password: str):
        self.vault.unlock(password)
        self._start_scheduler()

    def lock(self):
        self.vault.lock()

    def close(self):
        self._stop.set()
        self.vault.lock()

    def _start_scheduler(self):
        # Windows Task Scheduler runs a separate password-protected worker even
        # while this UI is locked. Avoid holding credentials in a UI timer thread.
        if os.name == "nt":
            return
        if self._scheduler and self._scheduler.is_alive():
            return
        self._stop.clear()
        self._scheduler = threading.Thread(target=self._schedule_loop, daemon=True, name="checkin-clock")
        self._scheduler.start()

    def _schedule_loop(self):
        while not self._stop.wait(30):
            try:
                if not self.is_unlocked:
                    continue
                doc = self.vault.snapshot()
                settings = doc.get("settings", {})
                now = cn_now()
                if (settings.get("scheduled_enabled") and due_today(settings.get("schedule_time", "09:00"), now)
                    and (self._last_attempt is None or now - self._last_attempt >= timedelta(minutes=15))):
                    self._last_attempt = now
                    self.checkin(None)
            except Exception:
                # Failure details are recorded by checkin without credential material.
                pass

    @staticmethod
    def _record(doc, action, message, status="info"):
        doc.setdefault("logs", []).append({"time": cn_now().isoformat(timespec="seconds"),
                                           "action": action, "status": status, "message": _clean(message)})
        doc["logs"] = doc["logs"][-1000:]

    def state(self):
        if not self.is_unlocked:
            return {"accounts": [], "discovery": self.discovery, "settings": DEFAULT_SETTINGS.copy(), "logs": []}
        doc = self.vault.snapshot()
        accounts = []
        today = cn_now().date().isoformat()
        for account in doc.get("accounts", []):
            value = {key: account.get(key, "") for key in
                ("id", "label", "user_id", "profile_path", "remaining", "plan", "status", "last_checkin", "last_success_day", "notes", "usage_updated_at", "usage_state", "usage_error", "usage_attempted_at", "unlimited",
                 "checkin_state", "checkin_day", "checkin_checked_at", "checkin_confirmed_at", "checkin_enabled", "checkin_error")}
            value["signed_today"] = account.get("last_success_day") == today
            if value["signed_today"]:
                value["checkin_state"] = "already"
            elif account.get("checkin_day") != today:
                value.update(checkin_state="unknown", checkin_enabled=None, checkin_error="")
            accounts.append(value)
        return {"accounts": accounts, "discovery": self.discovery,
                "settings": {**DEFAULT_SETTINGS, **doc.get("settings", {})}, "logs": doc.get("logs", [])}

    def _account(self, doc, identifier):
        for account in doc.get("accounts", []):
            if account["id"] == identifier:
                return account
        raise ValueError("账号不存在，请刷新列表")

    def detect_accounts(self):
        if not self.is_unlocked:
            raise ValueError("请先解锁")
        found = self.discovery if self.discovery.get("locations") else self.client.discover()
        self.discovery = found
        profiles = list(found.get("profiles", []))
        for path in (self.data_dir / "instances").glob("*"):
            if path.is_dir():
                profiles.append({"name": path.name, "path": str(path)})
        results = []
        seen = set()
        for profile in profiles:
            profile_path = Path(profile["path"] if isinstance(profile, dict) else profile).resolve()
            canonical = str(profile_path).lower()
            if canonical in seen:
                continue
            seen.add(canonical)
            try:
                snapshot = self.client.inspect_profile(profile_path)
            except (ValueError, RuntimeError, OSError):
                continue
            with self.vault.transaction() as doc:
                existing = next((a for a in doc["accounts"] if a.get("user_id") == snapshot.get("user_id")
                                 and a.get("user_id")), None)
                if existing is None:
                    existing = next((a for a in doc["accounts"] if not a.get("user_id") and
                                     str(Path(a["profile_path"]).resolve()).lower() == canonical), None)
                if existing:
                    existing["credentials"] = snapshot
                    existing["profile_path"] = str(profile_path)
                    existing["user_id"] = str(snapshot.get("user_id", ""))
                    existing["status"] = "已同步登录状态"
                    results.append({"id": existing["id"], "label": existing["label"], "state": "updated"})
                else:
                    account = self._from_snapshot(snapshot, profile_path)
                    doc["accounts"].append(account)
                    results.append({"id": account["id"], "label": account["label"], "state": "added"})
        with self.vault.transaction() as doc:
            self._record(doc, "检测账号", f"检测完成，新增或同步 {len(results)} 个账号")
        if self.is_unlocked and results:
            self.refresh_usage([r["id"] for r in results])
        return results

    @staticmethod
    def _from_snapshot(snapshot, profile_path, label=None):
        return {"id": uuid.uuid4().hex, "label": label or snapshot.get("label") or "TRAE 账号",
                "user_id": str(snapshot.get("user_id", "")), "profile_path": str(profile_path),
                "credentials": snapshot, "notes": "", "remaining": None, "plan": "待查询",
                "status": "已导入", "last_checkin": "", "last_success_day": ""}

    def create_account(self, label: str):
        label = str(label).strip()
        if not label or len(label) > 80:
            raise ValueError("请填写 1～80 字的账号名称")
        identifier = uuid.uuid4().hex
        profile = self.data_dir / "instances" / identifier
        profile.mkdir(parents=True, exist_ok=False)
        atomic_write(profile / ".trae-cn-manager-profile", b"1")
        account = self._from_snapshot({}, profile, label)
        account.update(id=identifier, status="等待官方登录")
        self.client.launch(profile)
        with self.vault.transaction() as doc:
            doc["accounts"].append(account)
            self._record(doc, "添加账号", "已启动独立 TRAE 实例；完成官方登录后点击同步登录")
        return {"id": identifier, "label": label, "profile_path": str(profile)}

    def sync_account(self, identifier):
        with self.vault.transaction() as doc:
            account = self._account(doc, identifier)
            snapshot = self.client.inspect_profile(Path(account["profile_path"]))
            duplicates = [a for a in doc["accounts"] if a["id"] != identifier and a.get("user_id")
                          and a["user_id"] == snapshot.get("user_id")]
            if duplicates:
                raise ValueError("这个 TRAE 用户已在账号库中，请使用已有账号")
            if str(account.get("user_id", "")) != str(snapshot.get("user_id", "")):
                account.update(last_success_day="", remaining=None, usage_updated_at="", checkin_day="",
                               checkin_state="unknown", checkin_enabled=None, checkin_error="")
            account.update(credentials=snapshot, user_id=str(snapshot.get("user_id", "")), status="已同步登录状态")
            self._record(doc, "同步登录", "已更新账号登录状态")
        self.refresh_usage([identifier])

    def launch_account(self, identifier):
        with self._mutex:
            account = self._account(self.vault.snapshot(), identifier)
            profile = Path(account["profile_path"])
            # A default TRAE profile may now contain a different phone account.
            # Keep that profile intact and open the saved account independently.
            if account.get("credentials", {}).get("login") and not account.get("needs_restore"):
                try:
                    current = self.client.inspect_profile(profile)
                    matches = str(current.get("user_id")) == str(account.get("user_id"))
                except (ValueError, RuntimeError, OSError):
                    matches = False
                if not matches:
                    profile = self.data_dir / "instances" / uuid.uuid4().hex
                    self.client.restore_account(account["credentials"], profile)
                    with self.vault.transaction() as doc:
                        target = self._account(doc, identifier)
                        target["profile_path"] = str(profile)
                        target["needs_restore"] = False
            if account.get("needs_restore"):
                if not _inside(profile, self.data_dir / "instances"):
                    raise ValueError("迁移账号的资料目录不在管理器目录中")
                self.client.restore_account(account["credentials"], profile)
                with self.vault.transaction() as doc:
                    self._account(doc, identifier)["needs_restore"] = False
            self.client.launch(profile)

    def edit_account(self, identifier, label, notes=""):
        if not str(label).strip() or len(str(label)) > 80:
            raise ValueError("账号名称需要 1～80 字")
        with self._mutex, self.vault.transaction() as doc:
            account = self._account(doc, identifier)
            account.update(label=str(label).strip(), notes=str(notes)[:4000])
            self._record(doc, "编辑账号", "账号名称及备注已更新")

    def remove_account(self, identifier):
        with self._mutex, self.vault.transaction() as doc:
            self._account(doc, identifier)
            doc["accounts"] = [a for a in doc["accounts"] if a["id"] != identifier]
            doc.get("memory_snapshots", {}).pop(identifier, None)
            doc["memory_history"] = [h for h in doc.get("memory_history", []) if h.get("target_id") != identifier]
            self._record(doc, "移除账号", "已从加密库移除账号；TRAE 本身及其资料目录保留")

    def checkin(self, ids=None):
        results = []
        today = cn_now().date().isoformat()
        with self._mutex, file_lock(self.data_dir / ".network.lock", timeout=120):
            doc = self.vault.snapshot()
            targets = [a["id"] for a in doc.get("accounts", []) if ids is None or a["id"] in ids]
            for identifier in targets:
                today = cn_now().date().isoformat()
                account = self._account(self.vault.snapshot(), identifier)
                if account.get("last_success_day") == today:
                    result = {"state": "already", "message": "今日已签到，已跳过。"}
                elif not account.get("credentials", {}).get("login"):
                    result = {"state": "expired", "message": "请在官方实例登录后同步登录状态"}
                else:
                    try:
                        # Re-read official state where available to pick up normal client renewal.
                        profile = Path(account["profile_path"])
                        if profile.exists() and not account.get("needs_restore"):
                            snapshot = self.client.inspect_profile(profile)
                            if str(snapshot.get("user_id")) == str(account.get("user_id")):
                                account["credentials"] = snapshot
                        result = self.client.checkin(account["credentials"])
                    except Exception:
                        result = {"state": "error", "message": "无法完成签到，请检查网络或同步官方登录状态"}
                with self.vault.transaction() as update:
                    target = self._account(update, identifier)
                    if account.get("credentials"):
                        target["credentials"] = account["credentials"]
                    message = _clean(result.get("message", result.get("state", "未知结果")))
                    target["status"] = message
                    target["last_checkin"] = cn_now().isoformat(timespec="seconds")
                    if result.get("state") in ("success", "already"):
                        target["last_success_day"] = today
                        target.update(checkin_state="already", checkin_day=today,
                                      checkin_checked_at=target["last_checkin"], checkin_confirmed_at=target["last_checkin"], checkin_error="")
                    elif target.get("last_success_day") != today:
                        state = result.get("state") if result.get("state") in {"expired", "unavailable"} else "error"
                        target.update(checkin_state=state, checkin_day=today,
                                      checkin_checked_at=target["last_checkin"], checkin_error=message)
                    self._record(update, "签到", message, result.get("state", "error"))
                results.append({"id": identifier, "label": account["label"], **result})
        if self.is_unlocked and results:
            self.refresh_usage([r["id"] for r in results])
        return results

    def refresh_usage(self, ids=None):
        results = []
        with self._mutex:
            doc = self.vault.snapshot()
            for account in doc.get("accounts", []):
                if ids is not None and account["id"] not in ids:
                    continue
                if not account.get("credentials", {}).get("login"):
                    result = {"remaining": None, "plan": "待登录", "message": "请先同步官方登录"}
                else:
                    try:
                        profile = Path(account["profile_path"])
                        if profile.exists() and not account.get("needs_restore"):
                            snapshot = self.client.inspect_profile(profile)
                            if str(snapshot.get("user_id")) == str(account.get("user_id")):
                                account["credentials"] = snapshot
                        result = self.client.usage(account["credentials"])
                    except Exception:
                        result = {"state": "error", "message": "用量查询暂时失败，请检查网络或官方登录状态"}
                with self.vault.transaction() as update:
                    target = self._account(update, account["id"])
                    ok = result.get("state") not in ("error", "expired", "unavailable") and (result.get("remaining") is not None or result.get("unlimited"))
                    target["usage_attempted_at"] = cn_now().isoformat(timespec="seconds")
                    old_state = target.get("usage_state")
                    target["usage_state"] = "ok" if ok else result.get("state", "unavailable")
                    target["usage_error"] = "" if ok else _clean(result.get("message", "未取得新用量，保留上次结果"))
                    if account.get("credentials"):
                        target["credentials"] = account["credentials"]
                    if ok:
                        target["remaining"] = result.get("remaining")
                        target["unlimited"] = bool(result.get("unlimited"))
                        target["plan"] = result.get("plan", target.get("plan", "未知"))
                        target["usage"] = result
                        target["usage_updated_at"] = result.get("updated_at") or target["usage_attempted_at"]
                    if old_state != target["usage_state"]:
                        self._record(update, "用量查询", "积分同步已恢复" if ok else target["usage_error"])
                # The status endpoint is read-only. Checking it alongside
                # usage catches sign-ins performed in the official client.
                checkin = self._read_checkin_status(account)
                if checkin is not None:
                    self._store_checkin_status(account["id"], checkin)
                results.append({"id": account["id"], **result})
        return results

    def _read_checkin_status(self, account):
        method = getattr(self.client, "checkin_status", None)
        if not callable(method):
            # Older adapters/test clients must not be mistaken for an official
            # negative response, nor undo a previously confirmed successful day.
            return None
        if not account.get("credentials", {}).get("login"):
            return {"state": "expired", "message": "请先同步官方登录。"}
        try:
            result = method(account["credentials"])
            if not isinstance(result, dict):
                raise ValueError("invalid status response")
            if result.get("state") not in {"already", "pending", "unavailable", "error", "expired"}:
                raise ValueError("unknown status response")
            state = result.get("state")
            if state in {"already", "pending", "unavailable"}:
                if not isinstance(result.get("checked_in"), bool) or not isinstance(result.get("enabled"), bool):
                    raise ValueError("unconfirmed check-in status")
                if state == "already" and result["checked_in"] is not True:
                    raise ValueError("unconfirmed check-in")
                if state == "pending" and (result["checked_in"] or not result["enabled"]):
                    raise ValueError("inconsistent check-in status")
                if state == "unavailable" and (result["checked_in"] or result["enabled"]):
                    raise ValueError("inconsistent availability")
            return result
        except Exception:
            return {"state": "error", "message": "签到状态暂未更新，请稍后刷新。"}

    def _store_checkin_status(self, identifier, result):
        today = cn_now().date().isoformat()
        with self.vault.transaction() as doc:
            account = self._account(doc, identifier)
            before = account.get("checkin_state")
            state = result.get("state", "error")
            account.update(checkin_day=today, checkin_checked_at=cn_now().isoformat(timespec="seconds"))
            if state in {"already", "pending", "unavailable"}:
                account["checkin_enabled"] = result.get("enabled")
                account["checkin_error"] = ""
                if state == "already":
                    account["last_success_day"] = today
                    account["checkin_confirmed_at"] = account["checkin_checked_at"]
                # Daily sign-in is irreversible. A delayed negative response
                # must not re-enable an account already confirmed today.
                account["checkin_state"] = "already" if account.get("last_success_day") == today else state
                if account["checkin_state"] == "already" and before != "already":
                    self._record(doc, "同步签到", "官方已确认今日签到，已自动同步", "already")
            else:
                account["checkin_state"] = "already" if account.get("last_success_day") == today else state
                account["checkin_error"] = _clean(result.get("message", "签到状态暂未更新"))
            return account["checkin_state"]

    def projects(self):
        return self.vault.snapshot().get("projects", [])

    def add_project(self, path):
        project = Path(path).resolve()
        if not project.is_dir():
            raise ValueError("请选择已存在的项目文件夹")
        with self.vault.transaction() as doc:
            known = {str(Path(p).resolve()).lower() for p in doc["projects"]}
            if str(project).lower() not in known:
                doc["projects"].append(str(project))

    def remove_project(self, path):
        with self.vault.transaction() as doc:
            doc["projects"] = [p for p in doc["projects"] if p != str(path)]

    def save_settings(self, changes):
        current = {**DEFAULT_SETTINGS, **self.vault.snapshot().get("settings", {})}
        settings = {**current, **{k: v for k, v in changes.items() if k in DEFAULT_SETTINGS and k not in ("schedule_backend", "skills_root")}}
        validate_time(settings["schedule_time"])
        if settings["theme"] not in ("dark", "light"):
            raise ValueError("不支持的主题")
        settings["lock_minutes"] = max(0, min(1440, int(settings["lock_minutes"])))
        settings["usage_refresh_seconds"] = max(5, min(300, int(settings["usage_refresh_seconds"])))
        for key in ("scheduled_enabled", "auto_checkin", "start_at_login", "usage_auto_refresh"):
            settings[key] = bool(settings[key])
        key_path = self.data_dir / "background.key"
        old_key = key_path.read_bytes() if key_path.exists() else None
        scheduled_changed = (settings["scheduled_enabled"] or settings["scheduled_enabled"] != current["scheduled_enabled"] or
                             settings["schedule_time"] != current["schedule_time"])
        startup_changed = settings["start_at_login"] != current["start_at_login"]
        try:
            if scheduled_changed:
                if settings["scheduled_enabled"]:
                    atomic_write(key_path, windows_protect(self.vault.key_for_background()))
                outcome = configure_schedule(self.data_dir, settings["scheduled_enabled"], settings["schedule_time"], start=False)
                settings["schedule_backend"] = outcome["backend"]
                if not settings["scheduled_enabled"]:
                    key_path.unlink(missing_ok=True)
            if startup_changed:
                configure_startup(self.data_dir, settings["start_at_login"])
            with self.vault.transaction() as doc:
                doc["settings"] = settings
                self._record(doc, "设置", "设置已保存；北京时间签到时间 " + settings["schedule_time"])
                if scheduled_changed:
                    self._record(doc, "后台方式", outcome.get("message", settings["schedule_backend"]))
        except Exception as original:
            failures = []
            try:
                if old_key is not None:
                    atomic_write(key_path, old_key)
                else:
                    key_path.unlink(missing_ok=True)
            except OSError:
                failures.append("后台密钥恢复失败")
            if scheduled_changed:
                try:
                    configure_schedule(self.data_dir, current["scheduled_enabled"], current["schedule_time"], start=False)
                except Exception:
                    failures.append("后台注册状态恢复失败")
            if startup_changed:
                try:
                    configure_startup(self.data_dir, current["start_at_login"])
                except Exception:
                    failures.append("界面开机启动状态恢复失败")
            if failures:
                raise RuntimeError("设置未完成；" + "；".join(failures) + "，请在诊断中检查并重新保存设置。原因：" + _clean(original)) from original
            raise
        if settings["scheduled_enabled"] and settings["schedule_backend"] == "startup":
            try:
                start_background(self.data_dir)
            except Exception as exc:
                with self.vault.transaction() as update:
                    self._record(update, "后台启动", "开机启动项已保存，但本次后台进程未启动；重新开启管家后再保存设置", "error")
                raise RuntimeError("设置已保存，但本次后台进程未启动；请重新保存设置后重试") from exc
        self._last_attempt = None

    def change_password(self, old, new):
        if self.recovery.path.exists() and not self.recovery.available:
            raise ValueError("恢复文件已损坏，请先重新生成恢复码，再修改密码")
        background_enabled = self.vault.snapshot().get("settings", {}).get("scheduled_enabled")
        def companions(new_key):
            files = self.recovery.rotation_files(new_key)
            if background_enabled:
                files[self.data_dir / "background.key"] = windows_protect(new_key)
            return files
        self.vault.change_password(old, new, companion_builder=companions)

    def memory_snapshots(self):
        doc = self.vault.snapshot()
        snapshots = doc.get("memory_snapshots", {})
        return [{"account_id": account["id"], "label": account["label"],
                 "captured_at": snapshots.get(account["id"], {}).get("captured_at", ""),
                 "file_count": len(snapshots.get(account["id"], {}).get("files", []))}
                for account in doc.get("accounts", [])]

    def save_memory_snapshot(self, identifier):
        with self._mutex:
            snapshot = self.memory.snapshot()
            with self.vault.transaction() as doc:
                self._account(doc, identifier)
                doc.setdefault("memory_snapshots", {})[identifier] = snapshot
                self._record(doc, "保存记忆", "已将本机共享记忆保存为选定账号的加密快照")
            return {"message": "当前本机共享记忆已保存为该账号快照", "file_count": len(snapshot["files"])}

    def preview_memory_sync(self, source_id, target_id):
        doc = self.vault.snapshot()
        self._account(doc, target_id)
        self._account(doc, source_id)
        source = doc.get("memory_snapshots", {}).get(source_id)
        if source is None:
            raise ValueError("来源账号还没有记忆快照，请先保存当前记忆到该账号")
        return {**self.memory.preview_merge(source), "scope": "local-shared",
                "message": "将合并到本机共享原生记忆，其他已打开的 TRAE 窗口也可能读取到，不会删除目标原有记忆"}

    def sync_memory(self, source_id, target_id):
        with self._mutex:
            doc = self.vault.snapshot()
            self._account(doc, source_id)
            self._account(doc, target_id)
            source = doc.get("memory_snapshots", {}).get(source_id)
            if source is None:
                raise ValueError("来源账号没有保存的记忆快照")
            current = self.memory.snapshot()
            with self.vault.transaction() as update:
                history = update.setdefault("memory_history", [])
                history.append({"target_id": target_id, "snapshot": current, "saved_at": cn_now().isoformat()})
                update["memory_history"] = history[-10:]
                while len(update["memory_history"]) > 1 and len(json.dumps(update["memory_history"]).encode()) > 8 * 1024 * 1024:
                    update["memory_history"].pop(0)
            result = self.memory.merge_snapshot(source)
            with self.vault.transaction() as update:
                self._record(update, "同步记忆", "已合并所选账号快照到本机共享记忆；合并前版本保存在加密库")
            return result

    def memory_history(self):
        doc = self.vault.snapshot()
        return [{"index": i, "saved_at": item.get("saved_at", ""), "file_count": len(item["snapshot"].get("files", []))}
                for i, item in enumerate(doc.get("memory_history", []))]

    def restore_memory_history(self, index):
        with self._mutex:
            doc = self.vault.snapshot()
            try:
                snapshot = doc.get("memory_history", [])[int(index)]["snapshot"]
            except (ValueError, IndexError, TypeError, KeyError):
                raise ValueError("记忆备份已变化，请刷新后选择") from None
            current = self.memory.snapshot()
            with self.vault.transaction() as update:
                history = update.setdefault("memory_history", [])
                history.append({"snapshot": current, "saved_at": cn_now().isoformat()})
                update["memory_history"] = history[-10:]
                while len(update["memory_history"]) > 1 and len(json.dumps(update["memory_history"]).encode()) > 8 * 1024 * 1024:
                    update["memory_history"].pop(0)
            result = self.memory.restore_snapshot(snapshot)
            with self.vault.transaction() as update:
                self._record(update, "恢复记忆", "已恢复本机共享记忆；恢复前版本已保存到加密历史")
            return result

    def _collect_resources(self, doc):
        resources = []
        total = 0
        roots = [("global", "", self.global_home)]
        for account in doc["accounts"]:
            profile = Path(account["profile_path"])
            roots.append(("profile", account["id"], profile / "User"))
        for index, project in enumerate(doc.get("projects", [])):
            roots.append(("project", str(index), Path(project) / ".trae"))
        for scope, owner, root in roots:
            candidates = []
            if scope in ("project", "profile"):
                candidates.append(root / "mcp.json")
            if scope != "profile":
                for name in ("skills", "user_rules" if scope == "global" else "rules",
                             ".trae-manager-disabled-skills", ".trae-manager-disabled-rules", ".trae-manager-disabled-skill-entries"):
                    folder = root / name
                    if folder.is_dir() and not folder.is_symlink():
                        candidates.extend(folder.rglob("*"))
            for path in candidates:
                boundary = root / "skills" if scope == "global" and path.is_relative_to(root / "skills") else root
                if path.is_symlink() or not path.is_file() or not _inside(path, boundary):
                    continue
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("单个配置资源超过 4 MB，请单独备份")
                raw = path.read_bytes()
                total += len(raw)
                if total > 24 * 1024 * 1024 or len(resources) >= 5000:
                    raise ValueError("配置资源超过迁移备份限制（24 MB / 5000 文件），请分开导出 Skills")
                resources.append({"scope": scope, "owner": owner, "name": path.relative_to(root).as_posix(),
                                  "content": base64.b64encode(raw).decode()})
        return resources

    def backup_export(self, path, password):
        doc = self.vault.snapshot()
        package = {"package": "trae-cn-manager-backup", "schema_version": 1, "app_version": __version__,
                   "created_at": cn_now().isoformat(), "vault": doc, "resources": self._collect_resources(doc),
                   "native_memory": self.memory.snapshot()}
        destination = self.safe_export_path(path)
        atomic_write(destination, encrypt_package(package, password))
        with self.vault.transaction() as update:
            self._record(update, "导出备份", f"已加密导出 {len(doc['accounts'])} 个账号及 {len(package['resources'])} 个配置文件")

    def backup_import(self, path, password):
        source = Path(path)
        if source.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("备份文件过大")
        package = decrypt_package(source.read_bytes(), password)
        if package.get("package") != "trae-cn-manager-backup" or package.get("schema_version") != 1:
            raise ValueError("不是受支持的迁移备份")
        incoming = package.get("vault", {}).get("accounts", [])
        if not isinstance(incoming, list) or len(incoming) > 1000:
            raise ValueError("备份账号列表不合法")
        resources = package.get("resources", [])
        self._validate_resources(resources)
        native_memory = package.get("native_memory")
        if native_memory is not None:
            self.memory._validate_snapshot(native_memory)
        added = 0
        mapping = {}
        with self.vault.transaction() as doc:
            for account in incoming:
                if not isinstance(account, dict) or not isinstance(account.get("credentials"), dict):
                    raise ValueError("备份账号内容不合法")
                existing = next((a for a in doc["accounts"] if a.get("user_id") and
                                 a["user_id"] == account.get("user_id")), None)
                if existing:
                    mapping[str(account.get("id"))] = existing["id"]
                    continue
                new = copy.deepcopy(account)
                new["id"] = uuid.uuid4().hex
                mapping[str(account.get("id"))] = new["id"]
                new["profile_path"] = str(self.data_dir / "instances" / new["id"])
                new["needs_restore"] = bool(new["credentials"].get("login"))
                new["credentials"]["profile_path"] = new["profile_path"]
                new["status"] = "已迁移；启动时恢复，必要时重新官方登录"
                new["label"] = str(new.get("label") or "迁移账号")[:80]
                doc["accounts"].append(new)
                added += 1
            for resource in resources:
                if resource["scope"] == "profile":
                    resource["owner"] = mapping.get(resource["owner"], "")
            snapshots = package.get("vault", {}).get("memory_snapshots", {})
            if isinstance(snapshots, dict):
                for old_id, snapshot in snapshots.items():
                    new_id = mapping.get(str(old_id))
                    if new_id and new_id not in doc.setdefault("memory_snapshots", {}):
                        self.memory._validate_snapshot(snapshot)
                        doc["memory_snapshots"][new_id] = snapshot
            doc["pending_resources"] = resources
            if native_memory is not None:
                doc["pending_native_memory"] = native_memory
            doc["pending_project_labels"] = {str(i): Path(p).name for i, p in enumerate(package.get("vault", {}).get("projects", [])) if isinstance(p, str)}
            self._record(doc, "导入备份", f"已合并 {added} 个账号；{len(resources)} 个配置文件待手动恢复；本机后台设置保持原值")
        return {"added": added, "resources": len(resources), "message": "账号已合并，配置请点击恢复迁移配置；跨设备登录可能需要重新验证"}

    def pending_projects(self):
        doc = self.vault.snapshot()
        owners = sorted({r["owner"] for r in doc.get("pending_resources", []) if r["scope"] == "project"})
        return [{"owner": owner, "label": doc.get("pending_project_labels", {}).get(owner, "项目 " + owner)} for owner in owners]

    def merge_imported_memory(self):
        with self._mutex:
            doc = self.vault.snapshot()
            pending = doc.get("pending_native_memory")
            if pending is None:
                raise ValueError("当前没有待合并的迁移记忆")
            current = self.memory.snapshot()
            with self.vault.transaction() as update:
                history = update.setdefault("memory_history", [])
                history.append({"snapshot": current, "saved_at": cn_now().isoformat()})
                update["memory_history"] = history[-10:]
            result = self.memory.merge_snapshot(pending)
            with self.vault.transaction() as update:
                update.pop("pending_native_memory", None)
                self._record(update, "迁移记忆", "已合并备份中的本机原生记忆，原内容已留存加密历史")
            return result

    def safe_export_path(self, path):
        destination = Path(path).resolve()
        protected = [self.data_dir, self.global_home]
        protected.extend(Path(p["path"]) for p in self.discovery.get("profiles", []) if isinstance(p, dict) and p.get("path"))
        if self.is_unlocked:
            protected.extend(Path(p) / ".trae" for p in self.projects())
        if any(_inside(destination, root) for root in protected):
            raise ValueError("导出文件请保存在管理器数据目录和 TRAE 配置目录以外，避免覆盖账号库或原配置")
        return destination

    def config_history(self):
        rows = []
        directory = self.data_dir / "config-backups"
        for metadata_path in sorted(directory.glob("*.restore.json"), reverse=True)[:200]:
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                backup = metadata_path.with_name(metadata_path.name.removesuffix(".restore.json"))
                if backup.exists() and _inside(backup, directory):
                    rows.append({"path": str(backup), "original_path": metadata.get("original_path", ""),
                                 "time": metadata.get("created_utc", ""), "name": backup.name})
            except (ValueError, OSError, TypeError):
                continue
        return rows

    def restore_config_backup(self, path):
        backup = Path(path).resolve()
        if not _inside(backup, self.data_dir / "config-backups"):
            raise ValueError("请选择本机配置历史中的备份")
        metadata_path = backup.with_name(backup.name + ".restore.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        original = Path(metadata["original_path"]).resolve()
        allowed = [self.global_home / name for name in
                   ("skills", "user_rules", ".trae-manager-disabled-skills", ".trae-manager-disabled-rules", ".trae-manager-disabled-skill-entries")]
        exact = []
        doc = self.vault.snapshot()
        profiles = [Path(a["profile_path"]) for a in doc["accounts"]]
        profiles.extend(Path(p["path"]) for p in self.discovery.get("profiles", []) if isinstance(p, dict) and p.get("path"))
        exact.extend(profile / "User" / "mcp.json" for profile in profiles)
        for project in doc.get("projects", []):
            root = Path(project) / ".trae"
            exact.append(root / "mcp.json")
            allowed.extend(root / name for name in ("skills", "rules", ".trae-manager-disabled-skills", ".trae-manager-disabled-rules", ".trae-manager-disabled-skill-entries"))
        if not (any(_inside(original, root) for root in allowed) or original in [p.resolve() for p in exact]):
            raise ValueError("备份原始位置已不在受管理的配置范围内，请先添加对应项目或实例")
        with self._mutex:
            target = self.configs.restore_backup(backup, overwrite=True)
            with self.vault.transaction() as update:
                self._record(update, "回滚配置", "已恢复配置历史；恢复前的现有版本也已备份")
            return {"message": "配置已恢复，当前配置也已留存备份", "path": str(target)}

    @staticmethod
    def _validate_resources(resources):
        if not isinstance(resources, list) or len(resources) > 5000:
            raise ValueError("配置资源列表不合法")
        total = 0
        for item in resources:
            if not isinstance(item, dict) or item.get("scope") not in ("global", "profile", "project"):
                raise ValueError("配置作用域不合法")
            relative = PurePosixPath(str(item.get("name", "")))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or any(":" in p or "\\" in p for p in relative.parts):
                raise ValueError("配置备份包含不安全的文件路径")
            for part in relative.parts:
                if (part.endswith((".", " ")) or re.search(r'[<>:"|?*\x00-\x1f]', part)
                    or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)):
                    raise ValueError("配置备份包含 Windows 不安全文件名")
            allowed = ("skills", "rules", "user_rules", "mcp.json", ".trae-manager-disabled-skills", ".trae-manager-disabled-rules", ".trae-manager-disabled-skill-entries")
            if relative.parts[0] not in allowed or (relative.parts[0] == "mcp.json" and len(relative.parts) != 1):
                raise ValueError("配置备份含非配置文件")
            try:
                raw = base64.b64decode(item["content"], validate=True)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError("配置备份编码损坏") from exc
            total += len(raw)
            if len(raw) > 4 * 1024 * 1024 or total > 24 * 1024 * 1024:
                raise ValueError("配置备份超过安全大小限制")

    def restore_backup_configs(self, project_mapping=None):
        """Apply only known configuration scopes; project destinations must be user chosen."""
        doc = self.vault.snapshot()
        resources = doc.get("pending_resources", [])
        self._validate_resources(resources)
        restored = 0
        skipped = 0
        for item in resources:
            if item["scope"] == "global":
                root = self.global_home
            elif item["scope"] == "profile":
                try:
                    account = self._account(doc, item["owner"])
                except ValueError:
                    skipped += 1
                    continue
                profile = Path(account["profile_path"])
                if not _inside(profile, self.data_dir / "instances"):
                    skipped += 1
                    continue
                root = profile / "User"
            else:
                chosen = (project_mapping or {}).get(item["owner"])
                if not chosen:
                    skipped += 1
                    continue
                root = Path(chosen).resolve() / ".trae"
            target = root.joinpath(*PurePosixPath(item["name"]).parts)
            boundary = root / "skills" if item["scope"] == "global" and PurePosixPath(item["name"]).parts[0] == "skills" else root
            if not _inside(target, boundary) or target.is_symlink():
                raise ValueError("恢复目标包含链接或越界路径")
            if target.exists():
                # Keep existing user content, including secrets and newer versions.
                skipped += 1
                continue
            atomic_write(target, base64.b64decode(item["content"]))
            restored += 1
        with self.vault.transaction() as update:
            self._record(update, "恢复配置", f"恢复 {restored} 个文件，跳过 {skipped} 个冲突或未映射文件")
        return {"restored": restored, "skipped": skipped,
                "message": f"恢复 {restored} 个文件；跳过 {skipped} 个已存在或未指定项目目标的文件"}

    def diagnostics(self):
        discovery = self.client.discover()
        compatibility = discovery.get("compatibility", {})
        items = [{"name": "TRAE CN", "status": "正常" if discovery.get("executable") else "缺失",
                  "detail": str(discovery.get("executable") or "在设置/诊断中检查安装位置")},
                 {"name": "版本兼容性", "status": str(compatibility.get("state", "待验证")) if isinstance(compatibility, dict) else "待验证",
                  "detail": str(compatibility.get("message", "")) if isinstance(compatibility, dict) else str(compatibility)},
                 {"name": "数据目录", "status": "正常", "detail": str(self.data_dir)},
                 {"name": "全局配置", "status": "正常" if self.global_home.exists() else "未创建", "detail": str(self.global_home)},
                 {"name": "后台任务", "status": "已启用" if self.vault.snapshot()["settings"].get("scheduled_enabled") else "未启用",
                  "detail": query_scheduler(self.data_dir).get("message", task_name(self.data_dir)) + "；关机时不能签到，开机登录后补签"}]
        for tool in ("node", "npm", "npx", "git", "python"):
            path = shutil.which(tool)
            items.append({"name": tool, "status": "已安装" if path else "未找到", "detail": path or "部分 MCP/Skill 可能需要此依赖"})
        return items

    def check_updates(self):
        return check_release(self.vault.snapshot()["settings"].get("github_repository", ""), __version__)

    def export_logs(self, path):
        logs = self.vault.snapshot().get("logs", [])
        atomic_write(self.safe_export_path(path), json.dumps(logs, ensure_ascii=False, indent=2).encode("utf-8"))

    def run_scheduled(self):
        key_file = self.data_dir / "background.key"
        if not key_file.exists() or not self.initialized:
            return 0
        self.vault.unlock_key(windows_protect(key_file.read_bytes(), decrypt=True))
        try:
            settings = self.vault.snapshot().get("settings", {})
            # A logon before today's chosen time still catches up; it never claims missed past days.
            if not settings.get("scheduled_enabled"):
                return 0
            results = self.checkin(None)
            return 1 if any(r.get("state") == "error" for r in results) else 0
        finally:
            self.lock()

    def run_background(self):
        """Least-privilege fallback for PCs that deny task registration."""
        guard = file_lock(self.data_dir / ".background-runner.lock", timeout=1)
        try:
            guard.__enter__()
        except RuntimeError:
            # A second fallback worker exits instead of duplicating requests.
            return 0
        try:
            first = True
            attempted = None
            while not self._stop.is_set():
                key_file = self.data_dir / "background.key"
                if not key_file.exists() or not self.initialized:
                    return 0
                try:
                    self.vault.unlock_key(windows_protect(key_file.read_bytes(), decrypt=True))
                    doc = self.vault.snapshot()
                    settings = doc.get("settings", {})
                    if not settings.get("scheduled_enabled") or settings.get("schedule_backend") != "startup":
                        return 0
                    now = cn_now()
                    pending = any(a.get("last_success_day") != now.date().isoformat() for a in doc.get("accounts", []))
                    if (pending and (first or due_today(settings.get("schedule_time", "09:00"), now))
                        and (attempted is None or now - attempted >= timedelta(minutes=15))):
                        attempted = now
                        self.checkin(None)
                    first = False
                    atomic_write(self.data_dir / "background-status.json", json.dumps({"state": "running", "updated_at": cn_now().isoformat()}).encode())
                except (OSError, RuntimeError, ValueError):
                    # Rotation can briefly replace the vault before its DPAPI key.
                    # Retry after re-reading both; never leave an enabled worker dead.
                    try:
                        atomic_write(self.data_dir / "background-status.json", json.dumps({"state": "retrying", "updated_at": cn_now().isoformat(), "message": "后台暂未完成，将重试；如持续失败请解锁后重新保存设置"}, ensure_ascii=False).encode("utf-8"))
                    except OSError:
                        pass
                finally:
                    self.vault.lock()
                if self._stop.wait(30):
                    return 0
            return 0
        finally:
            guard.__exit__(None, None, None)
