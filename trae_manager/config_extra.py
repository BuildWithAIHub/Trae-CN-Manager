"""Granular configuration operations for version 0.2.

All entry points are explicit user actions. Downloads are data-only and resource
disabling moves content outside TRAE's skill discovery tree.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import functools
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
import uuid

import requests


def _operation(method):
    @functools.wraps(method)
    def invoke(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (OSError, requests.RequestException, subprocess.SubprocessError) as exc:
            raise RuntimeError("配置文件或网络操作失败，请检查权限、路径和连接。") from exc
    return invoke


def _relative(value: str) -> Path:
    from .configs import _name
    if not isinstance(value, str) or not value:
        raise ValueError("请选择技能内的相对文件或目录。")
    parts = value.replace("\\", "/").split("/")
    for part in parts:
        _name(part)
    return Path(*parts)


def _is_link(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, "st_file_attributes", 0) & 0x400)


def _unlink_link(path: Path) -> None:
    """Remove only a link itself, never its target directory."""
    if not _is_link(path):
        raise ValueError("目标不是可移除的目录链接。")
    if os.name == "nt" and not path.is_symlink():
        os.rmdir(path)  # Junction: RemoveDirectory removes the reparse point.
    else:
        path.unlink()


def _remove_exact(path: Path) -> None:
    # Callers supply previously validated, exact transaction paths.
    if _is_link(path):
        _unlink_link(path)
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


class ConfigExtras:
    def _skill_context(self, skill_path: Path) -> tuple[Path, Path, bool]:
        return self._owned("skills", Path(skill_path))

    @staticmethod
    def _skill_mirror(root: Path, name: str) -> Path:
        return root.parent / ".trae-manager-disabled-skill-entries" / name

    def _entry_context(self, skill_path: Path, relative: str) -> tuple[Path, Path, Path, bool]:
        root, skill, enabled = self._skill_context(skill_path)
        relative_path = _relative(relative)
        active = skill / relative_path
        mirror = self._skill_mirror(root, skill.name) / relative_path
        for target, container in ((active, skill), (mirror, self._skill_mirror(root, skill.name))):
            if not target.resolve().is_relative_to(container.resolve()):
                raise ValueError("技能资源路径越界。")
            for parent in (target, *target.parents):
                if parent == container.parent:
                    break
                # The registered root may be our native junction to a custom
                # skills directory. Links below that boundary are never owned.
                if parent != root and _is_link(parent):
                    raise ValueError("技能资源不能经过符号链接或目录联接。")
        return skill, active, mirror, enabled

    def _skill_entry_destination(self, skill_path: Path, relative: str, active: Path, mirror: Path) -> Path:
        """Keep newly created children in a disabled resource directory."""
        if active.exists() and mirror.exists():
            raise ValueError("启用与停用区有同名资源，未覆盖。")
        if mirror.exists():
            return mirror
        root, skill, _ = self._skill_context(skill_path)
        disabled_root = self._skill_mirror(root, skill.name)
        for ancestor in _relative(relative).parents:
            if str(ancestor) != "." and (disabled_root / ancestor).is_dir() and not (skill / ancestor).exists():
                return mirror
        return active

    @contextmanager
    def _config_transaction(self, paths):
        """Snapshot exact prevalidated paths and roll back a failed batch."""
        unique = []
        for candidate in sorted({Path(p).absolute() for p in paths}, key=lambda p: len(p.parts)):
            if not any(candidate.is_relative_to(parent) for parent in unique):
                unique.append(candidate)
        with tempfile.TemporaryDirectory(prefix="trae-config-transaction-") as temporary:
            copies = []
            for index, path in enumerate(unique):
                if _is_link(path):
                    raise ValueError("事务目标不能是符号链接或目录联接。")
                snapshot = Path(temporary) / str(index)
                if path.is_dir():
                    self._check_tree(path)
                    shutil.copytree(path, snapshot)
                elif path.exists():
                    shutil.copy2(path, snapshot)
                copies.append((path, snapshot))
            try:
                yield
            except Exception:
                rollback_failed = False
                for path, snapshot in reversed(copies):
                    try:
                        _remove_exact(path)
                        if snapshot.exists():
                            path.parent.mkdir(parents=True, exist_ok=True)
                            if snapshot.is_dir():
                                shutil.copytree(snapshot, path)
                            else:
                                shutil.copy2(snapshot, path)
                    except OSError:
                        rollback_failed = True
                if rollback_failed:
                    raise RuntimeError("批量操作失败，部分恢复被系统阻止；请检查配置备份后恢复。") from None
                raise

    @_operation
    def list_skill_tree(self, skill_path: Path) -> list[dict]:
        root, skill, whole_enabled = self._skill_context(skill_path)
        mirror = self._skill_mirror(root, skill.name)
        result = {}
        for active, container in ((False, mirror), (True, skill)):
            if not container.is_dir():
                continue
            self._check_tree(container)
            for entry in sorted(container.rglob("*")):
                relative = entry.relative_to(container).as_posix()
                if relative in result and not entry.is_dir():
                    raise ValueError("技能启用与停用区存在同名资源，请先解决冲突。")
                result[relative] = {"relative": relative, "path": str(entry), "is_dir": entry.is_dir(),
                                    "enabled": bool(whole_enabled and active), "size": 0 if entry.is_dir() else entry.stat().st_size}
        return [result[key] for key in sorted(result, key=lambda s: (s.count("/"), s.casefold()))]

    @_operation
    def read_skill_file(self, skill_path: Path, relative: str) -> str:
        _, active, mirror, _ = self._entry_context(skill_path, relative)
        path = active if active.is_file() else mirror
        text = self.read_item(path)
        if "\x00" in text:
            raise ValueError("该资源是二进制文件，不能用文本编辑器打开。")
        return text

    @_operation
    def save_skill_file(self, skill_path: Path, relative: str, text: str) -> Path:
        from .configs import MAX_ITEM_BYTES, _fingerprint
        _, active, mirror, _ = self._entry_context(skill_path, relative)
        if not isinstance(text, str) or "\x00" in text or len(text.encode("utf-8")) > MAX_ITEM_BYTES:
            raise ValueError("只支持不超过 2 MB 的 UTF-8 文本资源。")
        if relative.replace("\\", "/") == "SKILL.md":
            metadata = self._frontmatter(text)
            if not isinstance(metadata.get("name"), str) or not metadata["name"].strip() or not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
                raise ValueError("SKILL.md 必须有有效 name 和 description。")
        path = self._skill_entry_destination(skill_path, relative, active, mirror)
        if path.is_dir():
            raise ValueError("目标是目录，不能保存为文件。")
        if path.exists():
            # Validate without resetting the optimistic edit snapshot from the
            # user's original read. A concurrent TRAE edit must be detected.
            try:
                existing = path.read_bytes()
                existing_text = existing.decode("utf-8-sig")
            except UnicodeError as exc:
                raise ValueError("该资源是二进制文件，不能用文本编辑器覆盖。") from exc
            if len(existing) > MAX_ITEM_BYTES or "\x00" in existing_text:
                raise ValueError("该资源是二进制文件或超过 2 MB，不能用文本编辑器覆盖。")
        with self._lock:
            canonical = path.resolve()
            raw = text.encode("utf-8")
            self._write(canonical, raw, self._item_snapshots.get(canonical, _fingerprint(canonical)))
            self._item_snapshots[canonical] = hashlib.sha256(raw).hexdigest()
        return path

    @_operation
    def create_skill_directory(self, skill_path: Path, relative: str) -> Path:
        _, active, mirror, _ = self._entry_context(skill_path, relative)
        if active.exists() or mirror.exists():
            raise ValueError("同名资源已存在。")
        destination = self._skill_entry_destination(skill_path, relative, active, mirror)
        destination.mkdir(parents=True, exist_ok=False)
        return destination

    @staticmethod
    def _preflight_merge(source: Path, destination: Path) -> None:
        if destination.exists():
            if not source.is_dir() or not destination.is_dir():
                raise ValueError("目标已有同名资源，未覆盖。")
            for entry in source.rglob("*"):
                target = destination / entry.relative_to(source)
                if target.exists() and not (entry.is_dir() and target.is_dir()):
                    raise ValueError("目标目录包含同名资源，未覆盖。")

    @_operation
    def set_skill_entry_enabled(self, skill_path: Path, relative: str, enabled: bool) -> Path:
        if not isinstance(enabled, bool):
            raise ValueError("启用状态必须是布尔值。")
        skill, active, mirror, whole_enabled = self._entry_context(skill_path, relative)
        if relative.replace("\\", "/") == "SKILL.md":
            return self.set_items_enabled("skills", [skill_path], enabled)[0]
        if not whole_enabled:
            raise ValueError("请先启用整个技能，再调整单个资源。")
        source, destination = (mirror, active) if enabled else (active, mirror)
        if not source.exists():
            if destination.exists():
                return destination
            raise ValueError("技能资源不存在。")
        self._check_tree(source) if source.is_dir() else None
        self._preflight_merge(source, destination)
        with self._lock, self._config_transaction([source, destination]):
            self._backup(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                shutil.copytree(source, destination, dirs_exist_ok=True)
                _remove_exact(source)
            else:
                source.rename(destination)
        return destination

    @_operation
    def delete_skill_entry(self, skill_path: Path, relative: str) -> Path:
        if relative.replace("\\", "/") == "SKILL.md":
            return self.delete_items("skills", [skill_path])[0]
        _, active, mirror, _ = self._entry_context(skill_path, relative)
        if not active.exists() and not mirror.exists():
            raise ValueError("技能资源不存在。")
        sources = [path for path in (active, mirror) if path.exists()]
        with self._lock, self._config_transaction(sources):
            backups = [self._backup(path) for path in sources]
            for path in sources:
                _remove_exact(path)
        return backups[0]

    def _batch_items(self, kind: str, paths) -> list[tuple[Path, Path, bool]]:
        if kind not in self._roots:
            raise ValueError("类型必须是 skills 或 rules。")
        if isinstance(paths, (str, Path)):
            raise ValueError("批量操作应传入文件列表。")
        try:
            paths = list(paths)
        except TypeError as exc:
            raise ValueError("批量操作应传入文件列表。") from exc
        items, seen = [], set()
        for path in paths:
            if not isinstance(path, (str, Path)):
                raise ValueError("条目路径必须是字符串或 Path。")
            root, item, enabled = self._owned(kind, Path(path))
            if not item.exists():
                raise ValueError("批量操作中有条目已经不存在。")
            key = str(item.resolve()).casefold()
            if key not in seen:
                seen.add(key)
                items.append((root, item, enabled))
        return items

    @_operation
    def set_items_enabled(self, kind: str, paths, enabled: bool) -> list[Path]:
        if not isinstance(enabled, bool):
            raise ValueError("启用状态必须是布尔值。")
        with self._lock:
            items = self._batch_items(kind, paths)
            changes, results = [], []
            for root, source, current in items:
                if current == enabled:
                    results.append(source / "SKILL.md" if kind == "skills" else source)
                    continue
                original = root if current else self._disabled_root(root, kind)
                target_root = root if enabled else self._disabled_root(root, kind)
                target = target_root / source.relative_to(original)
                if target.exists():
                    raise ValueError("批量操作存在目标冲突，没有更改任何条目。")
                changes.append((source, target))
                results.append(target / "SKILL.md" if kind == "skills" else target)
            with self._config_transaction([p for pair in changes for p in pair]):
                for source, _ in changes:
                    self.toggle_item(kind, source)
            return results

    @_operation
    def delete_items(self, kind: str, paths) -> list[Path]:
        with self._lock:
            items = self._batch_items(kind, paths)
            mirrors = [self._skill_mirror(root, item.name) for root, item, _ in items] if kind == "skills" else []
            with self._config_transaction([item for _, item, _ in items] + mirrors):
                backups = [self.remove_item(kind, item) for _, item, _ in items]
                for mirror in mirrors:
                    if mirror.exists():
                        self._backup(mirror)
                        _remove_exact(mirror)
            return backups

    @_operation
    def set_mcp_servers_enabled(self, path: Path, names, enabled: bool) -> None:
        if isinstance(names, str) or not isinstance(enabled, bool):
            raise ValueError("请提供 MCP 名称列表和布尔启用状态。")
        with self._lock:
            document = self.read_mcp(path)
            active = document["mcpServers"]
            disabled = document.setdefault("_trae_manager", {}).setdefault("disabledMcpServers", {})
            names = self._mcp_selection(names, active, disabled)
            if any(name not in active and name not in disabled for name in names):
                raise ValueError("批量操作中有 MCP 不存在，未保存任何更改。")
            for name in names:
                config = active.pop(name, disabled.pop(name, None))
                config.pop("disabled", None)
                (active if enabled else disabled)[name] = config
            self.save_mcp(path, document)

    @_operation
    def delete_mcp_servers(self, path: Path, names) -> None:
        if isinstance(names, str):
            raise ValueError("请提供 MCP 名称列表。")
        with self._lock:
            document = self.read_mcp(path)
            active = document["mcpServers"]
            disabled = document.get("_trae_manager", {}).get("disabledMcpServers", {})
            names = self._mcp_selection(names, active, disabled)
            if any(name not in active and name not in disabled for name in names):
                raise ValueError("批量操作中有 MCP 不存在，未保存任何更改。")
            for name in names:
                active.pop(name, None)
                disabled.pop(name, None)
            self.save_mcp(path, document)

    @staticmethod
    def _mcp_selection(names, active: dict, disabled: dict) -> list[str]:
        from .configs import _mcp_name
        if names is None:
            return [*active, *disabled]
        if isinstance(names, str):
            raise ValueError("请提供 MCP 名称列表。")
        try:
            values = list(names)
        except TypeError as exc:
            raise ValueError("请提供 MCP 名称列表。") from exc
        for name in values:
            _mcp_name(name)
        return list(dict.fromkeys(values))

    @staticmethod
    def _remote_data(url: str, limit: int) -> tuple[bytes, str]:
        """GET public HTTPS data; validate every redirect and bound wall time."""
        from .configs import ConfigManager
        if not isinstance(url, str):
            raise ValueError("请提供公开 HTTPS 文件地址。")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("配置下载只支持没有内嵌凭据的公开 HTTPS 地址。")
        if parsed.hostname == "github.com":
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 5 and parts[2] == "blob":
                url = "https://raw.githubusercontent.com/" + "/".join(parts[:2] + parts[3:])
        session = requests.Session()
        session.trust_env = False
        deadline = time.monotonic() + 30
        try:
            for _ in range(6):
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("配置下载只支持没有内嵌凭据的公开 HTTPS 地址。")
                try:
                    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
                except OSError as exc:
                    raise RuntimeError("无法解析下载地址。") from exc
                if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                    raise ValueError("下载地址指向非公开网络，已拒绝。")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("配置下载超时。")
                with session.get(url, stream=True, timeout=(min(5, remaining), min(10, remaining)), allow_redirects=False, headers={"User-Agent": "TraeCNManager/0.2"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status_code != 200:
                        raise RuntimeError("无法下载配置文件，请核对公开链接。")
                    if "text/event-stream" in response.headers.get("Content-Type", ""):
                        raise ValueError("该地址是 MCP 服务流；请提供配置 JSON 文件下载链接。")
                    declared = response.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > limit:
                        raise ValueError("下载文件超过允许的大小。")
                    raw = bytearray()
                    for chunk in ConfigManager._bounded_stream(response, deadline):
                        raw.extend(chunk)
                        if len(raw) > limit:
                            raise ValueError("下载文件超过允许的大小。")
                    return bytes(raw), url
            raise ValueError("配置下载跳转过多。")
        finally:
            session.close()

    @_operation
    def import_mcp_url(self, path: Path, url: str, name: str | None = None) -> int:
        from .configs import parse_jsonc, MAX_CONFIG_BYTES
        parsed = urlsplit(url)
        if parsed.scheme in {"trae-cn", "trae"}:
            route = (parsed.netloc + parsed.path).rstrip("/")
            if not route.endswith("mcp-import"):
                raise ValueError("只支持 TRAE 的 mcp-import 数据链接。")
            values = parse_qs(parsed.query)
            encoded = values.get("config", [""])[0]
            if not encoded or len(encoded) > MAX_CONFIG_BYTES * 2:
                raise ValueError("该安装链接没有完整配置；请提供含 config 的链接或 JSON 文件。")
            try:
                if encoded.lstrip().startswith("{"):
                    text = encoded
                else:
                    encoded = encoded.replace(" ", "+")
                    encoded += "=" * (-len(encoded) % 4)
                    text = base64.b64decode(encoded, altchars=b"-_", validate=True).decode("utf-8-sig")
            except (ValueError, UnicodeError) as exc:
                raise ValueError("TRAE 安装链接中的配置编码无效。") from exc
            name = name or values.get("name", [None])[0]
        else:
            raw, _ = self._remote_data(url, MAX_CONFIG_BYTES)
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError as exc:
                raise ValueError("远程配置不是 UTF-8 JSON 文件。") from exc
        if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError("配置超过 5 MB 限制。")
        document = parse_jsonc(text)
        if "mcpServers" not in document and "servers" not in document:
            if not name:
                raise ValueError("单个 MCP 配置需要明确名称；请填写名称或使用 mcpServers 根对象。")
            document = {"mcpServers": {name: document}}
        return self.import_mcp(path, document)

    @_operation
    def import_skill_url(self, url: str, project: Path | None = None) -> list[Path]:
        from .configs import MAX_ARCHIVE_BYTES, MAX_ITEM_BYTES, _name
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if parsed.scheme == "https" and parsed.hostname == "github.com" and (len(parts) == 2 or (len(parts) >= 3 and parts[2] in {"tree", "archive"})):
            self.last_skill_import_message = "已从 GitHub 导入所选范围内的技能。"
            return self.import_github_skill(url, project)
        raw, final_url = self._remote_data(url, MAX_ARCHIVE_BYTES)
        with tempfile.TemporaryDirectory(prefix="trae-remote-skill-") as temporary:
            stage = Path(temporary)
            if raw.startswith(b"PK") or urlsplit(final_url).path.lower().endswith(".zip"):
                archive = stage / "import.zip"
                archive.write_bytes(raw)
                result = self.import_skill(archive, project)
                self.last_skill_import_message = "已导入 ZIP 中的技能与资源。"
                return result
            if len(raw) > MAX_ITEM_BYTES:
                raise ValueError("远程 SKILL.md 超过 2 MB 限制。")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError as exc:
                raise ValueError("地址应提供 UTF-8 SKILL.md 或 ZIP 文件。") from exc
            if "\x00" in text:
                raise ValueError("远程文件是二进制数据。")
            metadata = self._frontmatter(text)
            if not isinstance(metadata.get("name"), str):
                raise ValueError("SKILL.md 缺少合法 name。")
            folder = stage / _name(metadata["name"])
            folder.mkdir()
            (folder / "SKILL.md").write_bytes(text.encode("utf-8"))
            result = self.import_skill(folder, project)
            self.last_skill_import_message = "只导入了 SKILL.md 主文件；引用的脚本和资源未下载，完整技能请优先使用 ZIP。"
            return result

    @_operation
    def list_mcp_files(self, folder: Path, max_depth: int = 4, max_count: int = 50) -> list[Path]:
        if not 0 <= max_depth <= 10 or not 1 <= max_count <= 200:
            raise ValueError("搜索深度或数量限制无效。")
        folder = Path(folder).resolve()
        if not folder.is_dir():
            raise ValueError("请选择配置文件夹。")
        names = {"mcp.json", ".mcp.json", "mcp_config.json", "claude_desktop_config.json"}
        pending, found = [(folder, 0)], []
        while pending:
            current, depth = pending.pop(0)
            for entry in sorted(current.iterdir()):
                if _is_link(entry):
                    continue
                if entry.is_file() and entry.name.lower() in names:
                    found.append(entry)
                    if len(found) >= max_count:
                        return found
                elif entry.is_dir() and depth < max_depth:
                    pending.append((entry, depth + 1))
        return found

    @staticmethod
    def _make_directory_link(link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            # Native Win32 junction creation without shell command interpolation.
            import ctypes
            import struct
            link.mkdir()
            substitute = "\\??\\" + str(target.resolve())
            printed = str(target.resolve())
            substitute_raw, print_raw = substitute.encode("utf-16-le"), printed.encode("utf-16-le")
            paths = substitute_raw + b"\0\0" + print_raw + b"\0\0"
            data = struct.pack("<HHHH", 0, len(substitute_raw), len(substitute_raw) + 2, len(print_raw)) + paths
            buffer = struct.pack("<IHH", 0xA0000003, len(data), 0) + data
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            kernel.CreateFileW.restype = ctypes.c_void_p
            handle = kernel.CreateFileW(str(link), 0x40000000, 0, None, 3, 0x00200000 | 0x02000000, None)
            if handle == ctypes.c_void_p(-1).value:
                link.rmdir()
                raise OSError(ctypes.get_last_error(), "无法创建目录联接")
            returned = ctypes.c_uint32()
            kernel.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            try:
                if not kernel.DeviceIoControl(handle, 0x000900A4, buffer, len(buffer), None, 0, ctypes.byref(returned), None):
                    raise OSError(ctypes.get_last_error(), "无法写入目录联接")
            except Exception:
                kernel.CloseHandle(handle)
                link.rmdir()
                raise
            else:
                kernel.CloseHandle(handle)
        else:
            link.symlink_to(target.resolve(), target_is_directory=True)

    def _copy_verified(self, source: Path, target: Path, created: list[Path] | None = None) -> list[Path]:
        self._check_tree(source)
        created = [] if created is None else created
        if not target.exists():
            target.mkdir(parents=True, exist_ok=False)
            created.append(target)
        for entry in sorted(source.rglob("*"), key=lambda p: (len(p.parts), str(p))):
            destination = target / entry.relative_to(source)
            if destination.exists():
                if entry.is_dir() and destination.is_dir():
                    continue
                raise ValueError("目标技能目录存在同名文件，未覆盖。")
            if entry.is_dir():
                destination.mkdir()
                created.append(destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Record before copy2: a failed write may leave a partial file.
                created.append(destination)
                shutil.copy2(entry, destination)
                if hashlib.sha256(entry.read_bytes()).digest() != hashlib.sha256(destination.read_bytes()).digest():
                    raise RuntimeError("技能迁移复制校验失败。")
        return created

    @_operation
    def configure_skills_root(self, destination: Path | None = None) -> dict:
        with self._lock:
            native = self.global_home / "skills"
            marker = self.global_home / ".trae-manager-skills-root.json"
            requested = Path(destination).expanduser().absolute() if destination is not None else native
            native_is_link = _is_link(native)
            current = native.resolve()
            if requested.resolve() == current and destination is not None:
                return {"root": str(current), "native_path": str(native), "message": "TRAE 已通过当前目录加载技能，无需迁移。"}
            if destination is None and not native_is_link:
                native.mkdir(parents=True, exist_ok=True)
                return {"root": str(native), "native_path": str(native), "message": "正在使用默认技能目录。"}
            old_marker = marker.read_bytes() if marker.exists() else None
            if native_is_link:
                try:
                    metadata = json.loads(old_marker or b"{}")
                    owned = isinstance(metadata, dict) and metadata.get("destination") == str(current)
                except (ValueError, TypeError):
                    owned = False
                if not owned:
                    raise ValueError("原生技能目录是已有目录联接，管理器不会删除它；可继续使用当前目标。")
            if destination is not None:
                if _is_link(requested):
                    raise ValueError("请选择实际存储目录，目标不能是已有目录联接。")
                requested = requested.resolve()
                if requested.is_relative_to(current) or current.is_relative_to(requested) or requested == native:
                    raise ValueError("新技能目录不能与原目录互相包含。")
                if requested.exists():
                    if not requested.is_dir():
                        raise ValueError("技能存储位置必须是目录。")
                    self._check_tree(requested)
            holding = native.with_name(".trae-manager-skills-original-" + uuid.uuid4().hex)
            actual_target = requested if destination is not None else native.with_name(".trae-manager-skills-default-" + uuid.uuid4().hex)
            created: list[Path] = []
            detached = False
            original_exists = native.exists()
            backup = None
            try:
                if original_exists:
                    # Preflight every collision before copying a single file.
                    for entry in current.rglob("*"):
                        target = actual_target / entry.relative_to(current)
                        if target.exists() and not (entry.is_dir() and target.is_dir()):
                            raise ValueError("目标目录含同名文件，原技能目录保持不变。")
                    backup = self._backup(current)
                    self._copy_verified(current, actual_target, created)
                else:
                    if not actual_target.exists():
                        actual_target.mkdir(parents=True, exist_ok=False)
                        created.append(actual_target)
                if native_is_link:
                    _unlink_link(native)
                    detached = True
                elif original_exists:
                    native.rename(holding)
                    detached = True
                else:
                    detached = True
                if destination is None:
                    actual_target.rename(native)
                    marker.unlink(missing_ok=True)
                else:
                    self._make_directory_link(native, actual_target)
                    self._write(marker, json.dumps({"version": 1, "destination": str(actual_target)}, ensure_ascii=False).encode("utf-8"), hashlib.sha256(old_marker).hexdigest() if old_marker is not None else None)
            except Exception:
                rollback_failed = False
                try:
                    if detached:
                        if native.exists() or _is_link(native):
                            _remove_exact(native)
                        if holding.exists():
                            holding.rename(native)
                        elif native_is_link:
                            self._make_directory_link(native, current)
                    if old_marker is not None:
                        if not marker.exists() or marker.read_bytes() != old_marker:
                            marker.parent.mkdir(parents=True, exist_ok=True)
                            marker.write_bytes(old_marker)
                    elif marker.exists():
                        marker.unlink()
                except (OSError, RuntimeError):
                    rollback_failed = True
                for path in reversed(created):
                    try:
                        if path.is_file():
                            path.unlink(missing_ok=True)
                        elif path.is_dir():
                            path.rmdir()
                    except OSError:
                        rollback_failed = True
                if rollback_failed:
                    raise RuntimeError("迁移失败且系统阻止了部分恢复，请从配置备份恢复；没有删除原自定义存储目录。") from None
                raise
            # Configuration and native loading are committed. This is only
            # cleanup of the old normal directory; a verified backup exists.
            cleanup_pending = False
            if holding.exists():
                try:
                    _remove_exact(holding)
                except OSError:
                    cleanup_pending = True
            self._roots["skills"].add(native)
            return {"root": str(native.resolve()), "native_path": str(native),
                    "backup": str(backup) if backup else "", "cleanup_pending": cleanup_pending,
                    "message": "技能存储已迁移并通过原生目录加载；原自定义目录不会被递归删除。"}
