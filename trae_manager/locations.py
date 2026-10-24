"""Consent-based, metadata-only discovery of local TRAE CN locations."""
from __future__ import annotations

from collections import deque
import ctypes
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from .security import atomic_write
from .trae import TraeClient, _shortcut_target, _product_metadata, _native_profile, _running_trae


_EXES = {name.casefold() for name in ("Trae CN.exe", "TraeCode CN.exe", "TRAE CN.exe", "Trae.exe")}
_CACHE_NAMES = ("Cache", "GPUCache", "CachedData", "Code Cache", "CachedExtensions", "Crashpad")
_PROTECTED = {name.casefold() for name in ("Windows", "$Recycle.Bin", "System Volume Information", "Recovery", "Config.Msi", "$WINDOWS.~BT", "Documents and Settings")}
_CONTENT_SKIP = {"node_modules", ".git", ".svn", ".hg", ".updates", "work", "outputs", "temp", "tmp"}


def _noise(path: Path | str) -> bool:
    return any(part.casefold() in _CONTENT_SKIP or part.casefold().startswith("pytest-") for part in Path(path).parts)


def _input_path(value) -> Path:
    text = str(value).strip().strip('"').strip("'")
    return Path(os.path.expandvars(text)).expanduser().resolve()


def _canonical(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _exists_accessible(path: Path) -> tuple[bool, bool]:
    try:
        if not path.exists():
            return False, False
        if path.is_dir():
            with os.scandir(path) as iterator:
                next(iterator, None)
        else:
            path.stat()
        return True, True
    except OSError:
        # An inaccessible parent can make even exists() raise; represent the
        # location as needing permission rather than silently discarding it.
        return True, False


def _product(executable: Path) -> dict[str, str] | None:
    """Read installed program metadata only, never user/project configuration."""
    try:
        value = _product_metadata(executable)
        if not value:
            return None
        return {"version": str(value.get("appVersion") or ""), "product_type": "TRAE_CN"}
    except (OSError, ValueError, UnicodeError):
        return None


def _local_drives() -> list[Path]:
    if os.name != "nt":
        return []
    kernel = ctypes.windll.kernel32
    mask = kernel.GetLogicalDrives()
    drives = []
    for index in range(26):
        if mask & (1 << index):
            root = f"{chr(65 + index)}:\\"
            if kernel.GetDriveTypeW(ctypes.c_wchar_p(root)) in (2, 3):
                drives.append(Path(root))
    return drives


def _cancelled(cancel) -> bool:
    if cancel is None:
        return False
    if hasattr(cancel, "is_set"):
        return bool(cancel.is_set())
    return bool(cancel() if callable(cancel) else cancel)


class LocationManager:
    """Persist paths and decisions, with no credentials or project file content."""
    def __init__(self, state_dir: Path, client: TraeClient, *, max_directories: int = 200_000, max_seconds: float = 120):
        self.state_dir = Path(state_dir).resolve()
        self.path = self.state_dir / "locations.json"
        self.client = client
        self.max_directories = max(1, int(max_directories))
        self.max_seconds = max(0.01, float(max_seconds))
        self._lock = threading.RLock()

    def _load(self) -> dict:
        default = {"schema_version": 1, "scan_consent": False, "scan_decided": False, "manual": {}, "cached": [], "tracked": [], "selected": {}, "acknowledged": [], "last_scan": {"status": "never"}, "notice_generation": 0}
        fallback = json.loads(json.dumps(default))
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 2 * 1024 * 1024:
                return default
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("schema_version") == 1:
                for key in ("manual", "selected", "last_scan"):
                    if isinstance(saved.get(key), dict):
                        default[key] = saved[key]
                for key in ("cached", "acknowledged", "tracked"):
                    if isinstance(saved.get(key), list):
                        default[key] = saved[key]
                default["scan_consent"] = saved.get("scan_consent") is True
                default["scan_decided"] = saved.get("scan_decided") is True or default["scan_consent"]
                default["notice_generation"] = max(0, int(saved.get("notice_generation", 0)))
                for key in ("last_signature", "last_notice", "last_auto_scan_key"):
                    if isinstance(saved.get(key), str):
                        default[key] = saved[key]
        except (OSError, ValueError, TypeError, UnicodeError):
            return fallback
        return default

    def _save(self, state: dict) -> None:
        raw = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            if not self.path.is_symlink() and self.path.read_bytes() == raw:
                return
        except OSError:
            pass
        atomic_write(self.path, raw)

    def consent(self, allowed: bool) -> None:
        with self._lock:
            state = self._load()
            state["scan_consent"] = bool(allowed)
            state["scan_decided"] = True
            self._save(state)

    def acknowledge(self, notice_id: str) -> None:
        with self._lock:
            state = self._load()
            if notice_id != state.get("last_notice"):
                raise ValueError("路径提示已经变化，请确认当前检测结果。")
            known = state["acknowledged"]
            if notice_id not in known:
                known.append(notice_id)
            state["acknowledged"] = known[-100:]
            self._save(state)

    @staticmethod
    def _location(kind: str, path: Path | str) -> dict:
        resolved = Path(path).expanduser().resolve()
        exists, accessible = _exists_accessible(resolved)
        return {"kind": kind, "path": str(resolved), "exists": exists, "accessible": accessible}

    @staticmethod
    def _add_related(records: list[dict], kind: str, path: Path | str, source="known", confidence="native") -> None:
        path = Path(path)
        records.append({"kind": kind, "path": str(path), "source": source, "confidence": confidence})
        if kind == "profile":
            for name in _CACHE_NAMES:
                if (path / name).is_dir():
                    records.append({"kind": "cache", "path": str(path / name), "source": source, "confidence": confidence})
            if (path / "extensions").is_dir():
                records.append({"kind": "extensions", "path": str(path / "extensions"), "source": source, "confidence": confidence})
        elif kind == "global_home" and (path / "extensions").is_dir():
            records.append({"kind": "extensions", "path": str(path / "extensions"), "source": source, "confidence": confidence})

    def _known(self, state: dict) -> tuple[dict, list[dict]]:
        records = []
        selected, manual = state["selected"], state["manual"]
        def confirmed(kind, path):
            return any(isinstance(row, dict) and row.get("kind") == kind and row.get("path") == path and row.get("confidence") == "confirmed" for row in state["cached"])
        # Only previous active selections are trusted. Old broad scans cached
        # fixtures/backups too; never adopt all cached paths as active scopes.
        preferred_exe = manual.get("executable") or selected.get("executable")
        if preferred_exe and _product(Path(preferred_exe)) and (manual.get("executable") or confirmed("executable", preferred_exe) or not _noise(preferred_exe)):
            self._add_related(records, "executable", preferred_exe, "manual" if manual.get("executable") else "cache", "confirmed")
            self.client.executable = Path(preferred_exe)
        preferred_profiles = [manual["profile"]] if manual.get("profile") else selected.get("profiles", [])
        for path in preferred_profiles:
            if not isinstance(path, str):
                continue
            explicit = path == manual.get("profile")
            if (explicit or confirmed("profile", path) or not _noise(path)) and _native_profile(Path(path), trusted=explicit):
                self._add_related(records, "profile", path, "manual" if explicit else "cache", "confirmed" if explicit else "native")
        preferred_home = manual.get("global_home") or selected.get("global_home")
        if preferred_home and Path(preferred_home).is_dir() and (manual.get("global_home") or confirmed("global_home", preferred_home) or not _noise(preferred_home)):
            self._add_related(records, "global_home", preferred_home, "manual" if manual.get("global_home") else "cache", "confirmed")
        if manual.get("cache"):
            self._add_related(records, "cache", manual["cache"], "manual", "confirmed")
        ready = all(any(row["kind"] == kind for row in records) for kind in ("executable", "profile", "global_home"))
        if ready:
            discovery = {"recent_projects": [], "compatibility": {}, "global_home": preferred_home}
            for running in _running_trae():
                if running.get("profile") and _native_profile(Path(running["profile"]), trusted=True):
                    self._add_related(records, "profile", running["profile"], "process")
        else:
            discovery = self.client.discover()
            if discovery.get("executable"):
                self._add_related(records, "executable", discovery["executable"], discovery.get("executable_source", "known"))
            for row in discovery.get("profiles", []):
                path = row.get("path") if isinstance(row, dict) else row
                source = row.get("source", "known") if isinstance(row, dict) else "known"
                if path and (_native_profile(Path(path), trusted=source == "process") or path == manual.get("profile")):
                    self._add_related(records, "profile", path, source)
            if discovery.get("global_home"):
                self._add_related(records, "global_home", discovery["global_home"], discovery.get("global_home_source", "known"))
        # Inspect known manager profiles only by structure; do not open storage.
        instances = self.state_dir / "instances"
        if instances.is_dir():
            try:
                for child in instances.iterdir():
                    if child.is_dir() and not child.is_symlink() and _native_profile(child):
                        self._add_related(records, "profile", child, "manager", "confirmed")
            except OSError:
                pass
        return discovery, records

    def _render(self, state: dict, discovery: dict, records: list[dict], *, lost=False) -> dict:
        rows: list[dict] = []
        seen = set()
        for record in records:
            try:
                row = self._location(record["kind"], record["path"])
                row.update(source=record.get("source", "scan"), confidence=record.get("confidence", "native"))
            except (OSError, ValueError, TypeError):
                continue
            key = (row["kind"], _canonical(row["path"]))
            if key not in seen:
                seen.add(key)
                if row["kind"] == "executable" and row["exists"]:
                    metadata = _product(Path(row["path"]))
                    row["valid"] = metadata is not None
                    if metadata:
                        row.update(metadata)
                rows.append(row)
        valid_exes = [r for r in rows if r["kind"] == "executable" and r.get("valid") and r["accessible"]]
        preferred = state["manual"].get("executable") or state["selected"].get("executable")
        executable = next((r for r in valid_exes if preferred and _canonical(r["path"]) == _canonical(preferred)), None)
        executable = executable or (valid_exes[0] if valid_exes else None)
        if executable:
            self.client.executable = Path(executable["path"])
            self.client.version = executable.get("version", "")
        else:
            self.client.executable = None
            self.client.version = ""
        profiles = [{"name": Path(row["path"]).name, "path": row["path"], "source": row["source"], "confidence": row["confidence"]} for row in rows if row["kind"] == "profile" and row["exists"] and row["accessible"]]
        global_preferred = state["manual"].get("global_home") or state["selected"].get("global_home")
        global_rows = [row for row in rows if row["kind"] == "global_home" and row["exists"] and row["accessible"]]
        global_row = next((r for r in global_rows if global_preferred and _canonical(r["path"]) == _canonical(global_preferred)), None)
        active_global = global_row or (global_rows[0] if global_rows else {})
        global_home = active_global.get("path", discovery.get("global_home", ""))
        selected_paths = {("executable", executable["path"])} if executable else set()
        selected_paths.update(("profile", p["path"]) for p in profiles)
        if active_global:
            selected_paths.add(("global_home", active_global["path"]))
        if state["manual"].get("cache"):
            selected_paths.add(("cache", str(Path(state["manual"]["cache"]).resolve())))
        for row in rows:
            row["selected"] = (row["kind"], row["path"]) in selected_paths
        quick_complete = bool(executable and profiles)
        signature_data = {"locations": sorted((r["kind"], _canonical(r["path"]), r["exists"], r["accessible"]) for r in rows if r["selected"]), "scan_status": state["last_scan"].get("status"), "scan_denied": state["last_scan"].get("denied_count", 0), "scan_decided": state["scan_decided"], "scan_consent": state["scan_consent"]}
        signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        old_signature = state.get("last_signature")
        if old_signature and old_signature != signature:
            state["notice_generation"] += 1
        notice_id = "locations-" + signature[:16] + "-" + str(state["notice_generation"])
        notice = None
        if notice_id not in state["acknowledged"]:
            inaccessible = sum(r["exists"] and not r["accessible"] for r in rows)
            kind = "moved" if old_signature and old_signature != signature else "found"
            message = f"已检测 {len(rows)} 个本地位置，请确认客户端、资料、缓存与全局配置路径。"
            if kind == "moved":
                message = "安装位置或资料路径已变化，请重新确认检测结果。"
            if inaccessible:
                message += f"其中 {inaccessible} 个位置无法访问，可手动指定有权限的路径。"
            if not executable:
                kind = "missing"
                message = "未找到可访问的 TRAE CN 程序。可授权全盘查找，或右键桌面 TRAE 快捷方式→属性→打开文件所在的位置，选择对应 EXE；资料和缓存目录可在设置中分别指定。"
            if state["last_scan"].get("partial") and not quick_complete:
                kind = "permission" if state["last_scan"].get("denied_count") else "partial"
                message += " 本次扫描未覆盖全部目录（取消、达到限制或访问被拒绝），请查看扫描结果；可在设置中缩小范围重试或手动指定位置。"
            if not executable:
                message += "快速查找仅检查常见位置；深度全盘查找需要另行明确授权。"
            notice = {"id": notice_id, "kind": kind, "message": message}
        state["last_signature"] = signature
        state["last_notice"] = notice_id
        state["cached"] = [{k: row[k] for k in ("kind", "path", "source", "confidence", "selected")} for row in rows]
        state["tracked"] = []  # Legacy full-scan results are not active selections.
        state["selected"] = {"executable": executable["path"] if executable else "", "global_home": active_global.get("path", ""), "profiles": [p["path"] for p in profiles]}
        self._save(state)
        compatibility = discovery.get("compatibility", {})
        if executable and hasattr(self.client, "_compatibility"):
            compatibility = self.client._compatibility()
        return {**discovery, "executable": executable["path"] if executable else None, "version": executable.get("version", "") if executable else "", "profiles": profiles, "global_home": global_home, "compatibility": compatibility, "needs_scan": not quick_complete, "quick_complete": quick_complete, "quick_status": "found" if executable else "missing", "scan_consent": state["scan_consent"] if state["scan_decided"] else None, "scan_decided": state["scan_decided"], "notice": notice, "locations": rows, "signature": signature, "scan": state["last_scan"]}

    @staticmethod
    def _missing_key(state: dict) -> tuple[list[str], str]:
        selected = state.get("selected", {})
        critical = [selected.get("executable"), selected.get("global_home"), *selected.get("profiles", [])]
        critical += [path for key, path in state["manual"].items() if key in ("executable", "profile", "global_home", "cache")]
        missing = sorted(set(str(path) for path in critical if path and not Path(path).exists()))
        return missing, hashlib.sha256("\n".join(missing).encode()).hexdigest() if missing else ""

    def inspect(self) -> dict:
        with self._lock:
            started = time.monotonic()
            state = self._load()
            missing, lost_key = self._missing_key(state)
            discovery, records = self._known(state)
            result = self._render(state, discovery, records, lost=bool(missing))
            # Migration triggers quick discovery again. Deep scans are always
            # a separate explicit user action, even after past scan consent.
            result["auto_scan_requested"] = False
            result["discovery_ms"] = round((time.monotonic() - started) * 1000, 2)
            return result

    def set_manual(self, executable=None, profile=None, global_home=None, cache=None) -> dict:
        updates = {"executable": executable, "profile": profile, "global_home": global_home, "cache": cache}
        with self._lock:
            state = self._load()
            for kind, value in updates.items():
                if value is None:
                    continue
                if str(value) == "":
                    state["manual"].pop(kind, None)
                    continue
                path = _input_path(value)
                if kind == "executable":
                    if path.is_dir():
                        path = next((p for p in (path / name for name in _EXES) if _product(p)), path)
                    if path.suffix.lower() == ".lnk":
                        target = _shortcut_target(path)
                        if target:
                            path = target.resolve()
                    if _product(path) is None:
                        raise ValueError("请选择具有国内版 TRAE_CN 产品标识的官方客户端 EXE。")
                elif kind == "profile":
                    if path.name.casefold() == "storage.json":
                        path = path.parent
                    if path.name.casefold() == "globalstorage" and path.parent.name.casefold() == "user":
                        path = path.parent
                    if path.name.casefold() == "user":
                        path = path.parent
                    if not _native_profile(path, trusted=True):
                        raise ValueError("资料目录缺少 User/globalStorage/storage.json。可选择资料根目录、User 文件夹或对应 storage.json 文件。")
                elif not path.is_dir():
                    raise ValueError("手动指定的位置必须是已存在的文件夹。")
                elif kind == "global_home" and not any((path / name).is_dir() for name in ("skills", "user_rules", "memory", "extensions")):
                    raise ValueError("全局目录应包含 skills、user_rules、memory 等 TRAE 配置文件夹；通常为用户目录下的 .trae-cn。")
                state["manual"][kind] = str(path)
            self._save(state)
            discovery, records = self._known(state)
            return self._render(state, discovery, records)

    def scan(self, roots=None, progress=None, cancel=None) -> dict:
        with self._lock:
            state = self._load()
            if not state["scan_consent"]:
                raise PermissionError("尚未授权全盘查找，请先明确同意扫描本地盘上的目录与程序元数据。")
            _, lost_key = self._missing_key(state)
            state["last_auto_scan_key"] = lost_key
            discovery, records = self._known(state)
            requested = _local_drives() if roots is None else ([roots] if isinstance(roots, (str, Path)) else list(roots))
            scan_confidence = "confirmed" if roots is not None else "native"
            queue = deque()
            for root in requested:
                path = Path(root).expanduser().resolve()
                if str(path).startswith("\\\\"):
                    continue  # Default and explicit scans cover local disks only.
                queue.append(path)
            visited: set[str] = set()
            denied: list[str] = []
            unverified_profiles: list[str] = []
            known_profiles = {_canonical(row["path"]) for row in records if row.get("kind") == "profile"}
            denied_count = skipped = directories = 0
            reasons: list[str] = []
            started = time.monotonic()
            status = "complete"
            def report(current=""):
                if progress:
                    try:
                        progress({"directories": directories, "found": len(records), "denied": denied_count, "skipped": skipped, "current_path": str(current)})
                    except Exception:
                        pass  # A closed UI must not corrupt a completed metadata scan.
            if not queue:
                status = "partial"
                reasons.append("没有可访问的本地固定盘或可移动盘扫描根目录")
            while queue:
                if _cancelled(cancel):
                    status = "cancelled"
                    reasons.append("用户取消")
                    break
                if directories >= self.max_directories or time.monotonic() - started >= self.max_seconds:
                    status = "partial"
                    reasons.append("达到目录数或时长限制，可缩小扫描根目录后继续查找")
                    break
                directory = queue.popleft()
                try:
                    key = _canonical(directory)
                    if key in visited:
                        continue
                    visited.add(key)
                    with os.scandir(directory) as iterator:
                        entries = list(iterator)
                    directories += 1
                except OSError:
                    denied_count += 1
                    if len(denied) < 100:
                        denied.append(str(directory))
                    continue
                native_profile = False
                try:
                    native_profile = (directory / "User" / "globalStorage" / "storage.json").is_file()
                    if native_profile:
                        # VS Code and other forks use this exact storage.json
                        # layout too. It alone is not proof of a TRAE profile.
                        # Recognize names/manager marker/previously confirmed
                        # paths without opening credentials. Unknown names are
                        # suggestions for manual confirmation, never active MCP
                        # or account scopes.
                        if _native_profile(directory) or key in known_profiles:
                            self._add_related(records, "profile", directory, "scan", scan_confidence)
                        elif len(unverified_profiles) < 100:
                            unverified_profiles.append(str(directory))
                    if directory.name.casefold() == ".trae-cn" and any((directory / name).is_dir() for name in ("skills", "user_rules", "memory", "extensions")):
                        self._add_related(records, "global_home", directory, "scan", scan_confidence)
                except OSError:
                    denied_count += 1
                    if len(denied) < 100:
                        denied.append(str(directory))
                installation = False
                for entry in entries:
                    try:
                        if entry.name.casefold() in _EXES and entry.is_file(follow_symlinks=False):
                            candidate = Path(entry.path)
                            if _product(candidate):
                                self._add_related(records, "executable", candidate, "scan", scan_confidence)
                                installation = True
                    except OSError:
                        denied_count += 1
                        if len(denied) < 100:
                            denied.append(entry.path)
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                        name = entry.name.casefold()
                        if entry.is_symlink() or attributes & 0x400 or name in _PROTECTED or name in _CONTENT_SKIP or name.startswith("pytest-") or (installation and name in ("resources", "locales")) or (native_profile and name in {n.casefold() for n in _CACHE_NAMES}):
                            skipped += 1
                            continue
                        queue.append(Path(entry.path))
                    except OSError:
                        denied_count += 1
                        if len(denied) < 100:
                            denied.append(entry.path)
                if directories % 100 == 0:
                    report(directory)
            if denied_count and status == "complete":
                status = "partial"
                reasons.append("部分目录没有读取目录列表的权限，未声称扫描完整")
            state["last_scan"] = {"status": status, "partial": status != "complete", "directories": directories, "denied_count": denied_count, "denied": denied, "unverified_profiles": unverified_profiles, "skipped": skipped, "reasons": reasons, "roots": [str(Path(p)) for p in requested], "elapsed_seconds": round(time.monotonic() - started, 2)}
            result = self._render(state, discovery, records)
            result["auto_scan_requested"] = False
            report()
            return result
