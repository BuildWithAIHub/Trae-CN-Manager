"""TRAE configuration editing with backups and explicit, bounded MCP probes.

Nothing in this module runs a server while reading, importing, or saving a
configuration. ``probe_mcp`` is the sole execution entry point.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import io
import itertools
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests
import yaml

from .config_extra import ConfigExtras, _is_link, _remove_exact
from .descriptions import summary_text


MAX_CONFIG_BYTES = 5 * 1024 * 1024
MAX_ARCHIVE_BYTES = 30 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 3000
MAX_ITEM_BYTES = 2 * 1024 * 1024
PROBE_TIMEOUT = 8.0
_META = "_trae_manager"
_DISABLED = "disabledMcpServers"


def _business_errors(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (OSError, zipfile.BadZipFile, requests.RequestException) as exc:
            raise RuntimeError("文件或网络操作失败，请检查权限、文件占用和网络。") from exc
    return wrapped


def parse_jsonc(text: str) -> dict:
    """Parse JSON comments/trailing commas without changing string contents."""
    if not isinstance(text, str):
        raise ValueError("配置必须是 JSON 文本。")
    result: list[str] = []
    i, quoted, escaped = 0, False, False
    while i < len(text):
        char = text[i]
        if quoted:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            i += 1
        elif char == '"':
            quoted = True
            result.append(char)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i + 2)
            i = len(text) if end < 0 else end
            result.append(" ")
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError("JSON 注释没有闭合。")
            result.append(" " + "\n" * text[i:end].count("\n"))
            i = end + 2
        else:
            result.append(char)
            i += 1
    text = "".join(result).lstrip("\ufeff")
    result, quoted, escaped = [], False, False
    for i, char in enumerate(text):
        if not quoted and char == ",":
            following = i + 1
            while following < len(text) and text[following].isspace():
                following += 1
            if following < len(text) and text[following] in "}]":
                continue
        result.append(char)
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True

    def pairs(items: list[tuple[str, Any]]) -> dict:
        value: dict = {}
        for key, item in items:
            if key in value:
                raise ValueError("JSON 中有重复字段，请先消除重复。")
            value[key] = item
        return value

    try:
        value = json.loads("".join(result), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("不支持 NaN/Infinity。")))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("JSON 配置无效，请检查括号、引号和字段格式。") from exc
    if not isinstance(value, dict):
        raise ValueError("配置根节点必须是对象。")
    return value


def _name(value: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 120
            or any(c in value for c in '\\/:*?"<>|\x00\r\n')
            or value in {".", ".."} or value.endswith((".", " "))):
        raise ValueError("名称无效：请使用不含路径符号的简短名称。")
    if value.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("名称不能使用 Windows 保留设备名。")
    return value


def _mcp_name(value: str) -> str:
    # A server label is a JSON key, not a Windows file name.
    if not isinstance(value, str) or not value.strip() or len(value) > 240 or any(ord(char) < 32 for char in value):
        raise ValueError("MCP 名称必须是非空且不含控制字符的简短文本。")
    return value


def _server(config: dict) -> str:
    if not isinstance(config, dict):
        raise ValueError("每个 MCP Server 配置必须是对象。")
    command, url = config.get("command"), config.get("url")
    if bool(command) == bool(url):
        raise ValueError("MCP 配置应包含 command 或 url 中的一项。")
    transport = config.get("type") or config.get("transport") or ("http" if url else "stdio")
    if transport not in {"stdio", "http", "sse", "streamable-http", "streamableHttp"}:
        raise ValueError("MCP transport 不受支持。")
    if command:
        if transport != "stdio" or not isinstance(command, str) or any(c in command for c in "\x00\r\n"):
            raise ValueError("stdio 的 command 必须是单个可执行命令或程序路径。")
        args = config.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) and "\x00" not in arg for arg in args):
            raise ValueError("MCP args 必须是字符串数组。")
    else:
        if not isinstance(url, str) or transport == "stdio":
            raise ValueError("HTTP MCP 必须配置有效 URL。")
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError
            parsed.port
        except ValueError as exc:
            raise ValueError("MCP URL 必须是有效 HTTP(S) 地址，凭据请放入 headers。") from exc
    for field in ("env", "headers"):
        value = config.get(field, {})
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) and "\x00" not in k + v for k, v in value.items()):
            raise ValueError(f"MCP {field} 必须是字符串键值对象。")
    if "disabled" in config and not isinstance(config["disabled"], bool):
        raise ValueError("MCP disabled 必须是布尔值。")
    return str(transport)


def _validate(document: dict) -> None:
    if not isinstance(document, dict) or not isinstance(document.get("mcpServers", {}), dict):
        raise ValueError("配置必须包含对象格式的 mcpServers。")
    meta = document.get(_META, {})
    if not isinstance(meta, dict) or not isinstance(meta.get(_DISABLED, {}), dict):
        raise ValueError("管理器禁用配置无效。")
    active, disabled = document.get("mcpServers", {}), meta.get(_DISABLED, {})
    if set(active) & set(disabled):
        raise ValueError("同一个 MCP 名称不能同时存在于启用和禁用列表。")
    for name, config in [*active.items(), *disabled.items()]:
        _mcp_name(name)
        _server(config)
    try:
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("配置包含无法保存的 JSON 数据。") from exc
    if len(encoded) > MAX_CONFIG_BYTES:
        raise ValueError("配置超过 5 MB 限制。")


def _fingerprint(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


class ConfigManager(ConfigExtras):
    def __init__(self, global_home: Path, backup_dir: Path):
        self.global_home = Path(global_home).expanduser().resolve()
        self.backup_dir = Path(backup_dir).expanduser().resolve()
        self._snapshots: dict[Path, str | None] = {}
        self._item_snapshots: dict[Path, str | None] = {}
        self._roots: dict[str, set[Path]] = {"skills": {self.global_home / "skills"}, "rules": {self.global_home / "user_rules"}}
        self._lock = threading.RLock()

    def mcp_path(self, profile: Path | None = None, project: Path | None = None) -> Path:
        if project is not None:
            return Path(project).expanduser().resolve() / ".trae" / "mcp.json"
        if profile is not None:
            return Path(profile).expanduser().resolve() / "User" / "mcp.json"
        roaming = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return roaming / "Trae CN" / "User" / "mcp.json"

    @_business_errors
    def read_mcp(self, path: Path) -> dict:
        path = Path(path).expanduser().resolve()
        with self._lock:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                self._snapshots[path] = None
                return {"mcpServers": {}}
            except OSError as exc:
                raise RuntimeError("无法读取 MCP 配置文件。") from exc
            if len(raw) > MAX_CONFIG_BYTES:
                raise ValueError("配置超过 5 MB 限制。")
            try:
                document = parse_jsonc(raw.decode("utf-8-sig"))
            except UnicodeError as exc:
                raise ValueError("MCP 配置必须使用 UTF-8 编码。") from exc
            _validate(document)
            document.setdefault("mcpServers", {})
            self._snapshots[path] = hashlib.sha256(raw).hexdigest()
            return document

    def _backup(self, source: Path) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        prefix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "_" + uuid.uuid4().hex[:8]
        destination = self.backup_dir / (prefix + "_" + source.name)
        if source.is_dir():
            self._check_tree(source)
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        metadata = {"original_path": str(source), "created_utc": datetime.now(timezone.utc).isoformat(), "is_directory": source.is_dir()}
        destination.with_name(destination.name + ".restore.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return destination

    def _write(self, path: Path, raw: bytes, expected: str | None, *, check: bool = True) -> None:
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if check and _fingerprint(path) != expected:
                raise RuntimeError("文件已被 TRAE 或其他程序修改，请刷新后再保存。")
            if path.exists():
                self._backup(path)
            with tempfile.NamedTemporaryFile(prefix=".trae-manager-", suffix=".tmp", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if check and _fingerprint(path) != expected:
                raise RuntimeError("保存期间文件发生变化，请刷新后重试。")
            os.replace(temporary, path)
            temporary = None
        except OSError as exc:
            raise RuntimeError("保存失败，请检查文件权限和文件占用。") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @_business_errors
    def save_mcp(self, path: Path, document: dict) -> None:
        path = Path(path).expanduser().resolve()
        _validate(document)
        with self._lock:
            expected = self._snapshots.get(path, _fingerprint(path))
            raw = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            self._write(path, raw, expected)
            self._snapshots[path] = hashlib.sha256(raw).hexdigest()

    @_business_errors
    def list_mcp(self, path: Path) -> list[dict]:
        document = self.read_mcp(path)
        active = document.get("mcpServers", {})
        disabled = document.get(_META, {}).get(_DISABLED, {})
        return [{"name": name, "transport": _server(config), "enabled": name in active and not config.get("disabled", False), "config": copy.deepcopy(config)}
                for name, config in [*active.items(), *disabled.items()]]

    @_business_errors
    def upsert_mcp(self, path: Path, name: str, config: dict) -> None:
        _mcp_name(name)
        _server(config)
        with self._lock:
            document = self.read_mcp(path)
            inactive = document.get(_META, {}).get(_DISABLED, {})
            target = inactive if name in inactive else document["mcpServers"]
            target[name] = copy.deepcopy(config)
            self.save_mcp(path, document)

    @_business_errors
    def set_mcp_enabled(self, path: Path, name: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("启用状态必须为布尔值。")
        with self._lock:
            document = self.read_mcp(path)
            active = document["mcpServers"]
            inactive = document.setdefault(_META, {}).setdefault(_DISABLED, {})
            if name not in active and name not in inactive:
                raise ValueError("找不到该 MCP Server。")
            config = active.pop(name, inactive.pop(name, None))
            config.pop("disabled", None)
            (active if enabled else inactive)[name] = config
            self.save_mcp(path, document)

    @_business_errors
    def delete_mcp(self, path: Path, name: str) -> None:
        with self._lock:
            document = self.read_mcp(path)
            active, inactive = document["mcpServers"], document.get(_META, {}).get(_DISABLED, {})
            if name not in active and name not in inactive:
                raise ValueError("找不到该 MCP Server。")
            active.pop(name, None)
            inactive.pop(name, None)
            self.save_mcp(path, document)

    @_business_errors
    def import_mcp(self, path: Path, source: Path | str | dict) -> int:
        if isinstance(source, dict):
            imported = copy.deepcopy(source)
        elif isinstance(source, Path):
            try:
                imported = parse_jsonc(source.read_text(encoding="utf-8-sig"))
            except OSError as exc:
                raise RuntimeError("无法读取导入文件。") from exc
        else:
            imported = parse_jsonc(source)
        if "mcpServers" not in imported:
            if isinstance(imported.get("servers"), dict):
                # VS Code uses "servers" for the same connection records.
                imported["mcpServers"] = imported.pop("servers")
            else:
                raise ValueError("导入配置应包含 mcpServers 或 servers 对象。")
        _validate(imported)
        with self._lock:
            document = self.read_mcp(path)
            existing = set(document["mcpServers"]) | set(document.get(_META, {}).get(_DISABLED, {}))
            incoming = set(imported.get("mcpServers", {})) | set(imported.get(_META, {}).get(_DISABLED, {}))
            if existing & incoming:
                raise ValueError("导入包含重名 MCP，请先改名；原配置未被覆盖。")
            document["mcpServers"].update(imported.get("mcpServers", {}))
            if imported.get(_META, {}).get(_DISABLED):
                document.setdefault(_META, {}).setdefault(_DISABLED, {}).update(imported[_META][_DISABLED])
            for key, value in imported.get(_META, {}).items():
                if key != _DISABLED and key not in document.setdefault(_META, {}):
                    document[_META][key] = copy.deepcopy(value)
            for key, value in imported.items():
                if key not in {"mcpServers", _META} and key not in document:
                    document[key] = value
            self.save_mcp(path, document)
            return len(incoming)

    @_business_errors
    def export_mcp(self, path: Path, destination: Path, *, include_secrets: bool = False) -> Path:
        document = self.read_mcp(path)
        if not include_secrets:
            for servers in (document.get("mcpServers", {}), document.get(_META, {}).get(_DISABLED, {})):
                for config in servers.values():
                    if "env" in config:
                        config["env"] = {key: "REDACTED" for key in config["env"]}
                    if "headers" in config:
                        config["headers"] = {key: "REDACTED" for key in config["headers"]}
                    if "args" in config:
                        # Secrets can be positional arguments as well as flag
                        # values. A redacted export intentionally needs review.
                        config["args"] = ["REDACTED" for _ in config["args"]]
                    if "url" in config:
                        parsed = urlsplit(config["url"])
                        config["url"] = parsed._replace(query="", fragment="").geturl()
        destination = Path(destination).resolve()
        if destination.exists():
            raise ValueError("导出文件已存在，请选择新文件名。")
        self._write(destination, (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), None)
        return destination

    def _root(self, kind: str, project: Path | None = None) -> Path:
        if kind not in {"skills", "rules"}:
            raise ValueError("类型必须是 skills 或 rules。")
        root = (Path(project).expanduser().resolve() / ".trae" / kind) if project is not None else (self.global_home / ("skills" if kind == "skills" else "user_rules"))
        self._roots[kind].add(root)
        return root

    @staticmethod
    def _disabled_root(root: Path, kind: str) -> Path:
        return root.parent / (".trae-manager-disabled-" + kind)

    def _owned(self, kind: str, path: Path) -> tuple[Path, Path, bool]:
        if kind not in self._roots:
            raise ValueError("类型必须是 skills 或 rules。")
        original = Path(path).absolute()
        if _is_link(original):
            raise ValueError("不能管理符号链接。")
        resolved = original.resolve()
        for root in self._roots[kind]:
            for enabled, container in ((True, root), (False, self._disabled_root(root, kind))):
                # Only the registered skills root may be a junction. A child
                # junction must not resolve to a different owned item's path.
                if not enabled and _is_link(container):
                    raise ValueError("停用区不能是符号链接或目录联接。")
                for boundary in {container.absolute(), container.resolve()}:
                    if original.is_relative_to(boundary):
                        for ancestor in (original, *original.parents):
                            if ancestor == boundary:
                                break
                            if _is_link(ancestor):
                                raise ValueError("条目路径不能经过符号链接或目录联接。")
                try:
                    relative = resolved.relative_to(container.resolve())
                except ValueError:
                    continue
                if not relative.parts:
                    continue
                if kind == "skills":
                    item = container / relative.parts[0]
                    if len(relative.parts) > 1 and relative.parts[1:] != ("SKILL.md",):
                        raise ValueError("请选择技能目录或 SKILL.md。")
                else:
                    item = resolved
                    if item.suffix.lower() != ".md":
                        raise ValueError("规则必须是 Markdown 文件。")
                if _is_link(item):
                    raise ValueError("不能管理符号链接。")
                return root, item, enabled
        raise ValueError("文件不属于当前管理的技能或规则目录。")

    @staticmethod
    def _frontmatter(text: str) -> dict:
        if not text.startswith("---"):
            return {}
        lines = text.splitlines()
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is None:
            raise ValueError("Markdown 的 YAML 头部没有闭合。")
        try:
            metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
        except (yaml.YAMLError, RecursionError) as exc:
            raise ValueError("Markdown 的 YAML 头部格式无效。") from exc
        if not isinstance(metadata, dict):
            raise ValueError("Markdown 的 YAML 头部必须是对象。")
        return metadata

    @_business_errors
    def list_items(self, kind: str, project: Path | None = None) -> list[dict]:
        root = self._root(kind, project)
        items = []
        for enabled, container in ((True, root), (False, self._disabled_root(root, kind))):
            if not container.exists():
                continue
            candidates = sorted(container.iterdir()) if kind == "skills" else sorted(container.rglob("*.md"))
            for candidate in candidates:
                if _is_link(candidate):
                    continue
                path = candidate / "SKILL.md" if kind == "skills" else candidate
                if not path.is_file() or _is_link(path):
                    continue
                try:
                    text = self.read_item(path)
                    metadata = self._frontmatter(text)
                    description = str(metadata.get("description", ""))
                    # The directory is the stable editing identifier. YAML
                    # names can differ (for example Vercel's imported skills).
                    name = candidate.name if kind == "skills" else str(candidate.relative_to(container))
                    error = ""
                except (ValueError, RuntimeError) as exc:
                    name, description, error = candidate.stem, "", str(exc)
                summary = summary_text(description, text) if not error else "内容有误，请编辑检查"
                items.append({"name": name, "path": str(path), "enabled": enabled, "description": description,
                              "summary": summary, "error": error})
        return items

    @_business_errors
    def read_item(self, path: Path) -> str:
        path = Path(path).resolve()
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_ITEM_BYTES:
                raise ValueError("Markdown 文件超过 2 MB 限制。")
            text = raw.decode("utf-8-sig")
        except OSError as exc:
            raise RuntimeError("无法读取 Markdown 文件。") from exc
        except UnicodeError as exc:
            raise ValueError("Markdown 文件必须使用 UTF-8 编码。") from exc
        self._item_snapshots[path] = hashlib.sha256(raw).hexdigest()
        return text

    @_business_errors
    def save_item(self, kind: str, name: str, text: str, project: Path | None = None) -> Path:
        root = self._root(kind, project)
        if kind == "skills":
            relative = Path(_name(name))
        else:
            if not isinstance(name, str):
                raise ValueError("规则名称无效。")
            components = name.replace("\\", "/").split("/")
            if len(components) > 4:
                raise ValueError("规则至多支持 3 层子目录。")
            for component in components:
                _name(component)
            relative = Path(*components)
            if relative.suffix.lower() != ".md":
                relative = relative.with_name(relative.name + ".md")
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_ITEM_BYTES:
            raise ValueError("Markdown 内容无效或超过 2 MB 限制。")
        metadata = self._frontmatter(text)
        if kind == "skills" and (not isinstance(metadata.get("name"), str) or not isinstance(metadata.get("description"), str) or not metadata["name"].strip() or not metadata["description"].strip()):
            raise ValueError("技能必须有 YAML 头部的 name 和 description。")
        path = root / relative / "SKILL.md" if kind == "skills" else root / relative
        if _is_link(path) or (_is_link(path.parent) and kind == "skills"):
            raise ValueError("不能覆盖符号链接。")
        disabled = self._disabled_root(root, kind) / relative
        if disabled.exists():
            if path.exists():
                raise ValueError("同时存在同名启用项和禁用项，请先解决冲突。")
            path = disabled / "SKILL.md" if kind == "skills" else disabled
        allowed_container = self._disabled_root(root, kind) if disabled.exists() else root
        if allowed_container != root and _is_link(allowed_container):
            raise ValueError("停用区不能是符号链接或目录联接。")
        for ancestor in (path, *path.parents):
            if ancestor == allowed_container:
                break
            if _is_link(ancestor):
                raise ValueError("条目路径不能经过符号链接或目录联接。")
        try:
            path.resolve().relative_to(allowed_container.resolve())
        except ValueError as exc:
            raise ValueError("目标路径经过链接指向了管理目录之外。") from exc
        with self._lock:
            canonical = path.resolve()
            self._write(canonical, text.encode("utf-8"), self._item_snapshots.get(canonical, _fingerprint(canonical)))
            self._item_snapshots[canonical] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return path

    @_business_errors
    def save_existing_item(self, kind: str, path: Path, text: str) -> Path:
        """Edit the exact owned item without changing its scope/enabled state."""
        with self._lock:
            _, item, _ = self._owned(kind, Path(path))
            target = item / "SKILL.md" if kind == "skills" else item
            if not target.is_file() or _is_link(target):
                raise ValueError("原条目已不存在或不是普通 Markdown 文件，请先刷新。")
            if not isinstance(text, str) or "\x00" in text or len(text.encode("utf-8")) > MAX_ITEM_BYTES:
                raise ValueError("Markdown 内容无效或超过 2 MB 限制。")
            metadata = self._frontmatter(text)
            if kind == "skills" and (not isinstance(metadata.get("name"), str) or not isinstance(metadata.get("description"), str) or not metadata["name"].strip() or not metadata["description"].strip()):
                raise ValueError("技能必须有 YAML 头部的 name 和 description。")
            canonical = target.resolve()
            raw = text.encode("utf-8")
            self._write(canonical, raw, self._item_snapshots.get(canonical, _fingerprint(canonical)))
            self._item_snapshots[canonical] = hashlib.sha256(raw).hexdigest()
            return target

    @_business_errors
    def toggle_item(self, kind: str, path: Path) -> Path:
        with self._lock:
            root, source, enabled = self._owned(kind, path)
            if not source.exists():
                raise ValueError("文件已不存在。")
            origin_container = root if enabled else self._disabled_root(root, kind)
            destination_container = self._disabled_root(root, kind) if enabled else root
            destination = destination_container / source.relative_to(origin_container)
            if destination.exists():
                raise ValueError("目标已有同名项，未覆盖。")
            try:
                self._backup(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.rename(destination)
            except OSError as exc:
                raise RuntimeError("启停失败，请检查权限或文件占用。") from exc
            return destination / "SKILL.md" if kind == "skills" else destination

    @_business_errors
    def remove_item(self, kind: str, path: Path) -> Path:
        with self._lock:
            root, source, _ = self._owned(kind, path)
            if not source.exists():
                raise ValueError("文件已不存在。")
            try:
                mirror = self._skill_mirror(root, source.name) if kind == "skills" else None
                sources = [source] + ([mirror] if mirror is not None and mirror.exists() else [])
                with self._config_transaction(sources):
                    backups = [self._backup(item) for item in sources]
                    for item in sources:
                        _remove_exact(item)
                backup = backups[0]
                return backup
            except OSError as exc:
                raise RuntimeError("删除失败，备份目录保留已完成的备份。") from exc

    @staticmethod
    def _check_tree(root: Path) -> None:
        total, count = 0, 0
        for path in itertools.chain((root,), root.rglob("*")):
            mode = path.lstat().st_mode
            attrs = getattr(path.lstat(), "st_file_attributes", 0)
            if path.is_symlink() or stat.S_ISLNK(mode) or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ValueError("技能包不能包含符号链接或目录联接。")
            count += 1
            if count > MAX_ARCHIVE_FILES:
                raise ValueError("技能包的文件数超出限制。")
            if path.is_file():
                total += path.stat().st_size
                if total > MAX_EXTRACTED_BYTES:
                    raise ValueError("技能包的文件数或总大小超出限制。")

    @staticmethod
    def _extract(archive: zipfile.ZipFile, destination: Path) -> None:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_FILES or sum(item.file_size for item in entries) > MAX_EXTRACTED_BYTES:
            raise ValueError("ZIP 的文件数或解压大小超出限制。")
        normalized: set[str] = set()
        for item in entries:
            raw = item.filename.replace("\\", "/")
            path = PurePosixPath(raw)
            if not raw or path.is_absolute() or any(part in {"..", "."} or ":" in part or part.endswith((".", " ")) for part in raw.rstrip("/").split("/")):
                raise ValueError("ZIP 包含不安全路径。")
            if stat.S_ISLNK((item.external_attr >> 16) & 0xFFFF):
                raise ValueError("ZIP 不能包含符号链接。")
            if item.flag_bits & 1:
                raise ValueError("不支持加密 ZIP。")
            key = str(path).casefold().rstrip("/")
            if key in normalized:
                raise ValueError("ZIP 中包含重复文件名。")
            normalized.add(key)
            for part in path.parts:
                _name(part)
            target = destination.joinpath(*path.parts)
            try:
                target.resolve().relative_to(destination.resolve())
            except ValueError as exc:
                raise ValueError("ZIP 路径越界。") from exc
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as incoming, target.open("xb") as outgoing:
                    written = 0
                    while chunk := incoming.read(65536):
                        written += len(chunk)
                        if written > item.file_size or written > MAX_EXTRACTED_BYTES:
                            raise ValueError("ZIP 实际大小超过声明限制。")
                        outgoing.write(chunk)

    @_business_errors
    def import_skill(self, source: Path | str, project: Path | None = None) -> list[Path]:
        root = self._root("skills", project)
        with tempfile.TemporaryDirectory(prefix="trae-skill-import-") as temporary:
            stage = Path(temporary)
            search_root = stage
            source_path = Path(source) if not isinstance(source, str) or not source.startswith("https://") else None
            if source_path is None:
                selection: dict = {}
                raw = self._github_zip(str(source), selection=selection)
                try:
                    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                        self._extract(archive, stage)
                except (zipfile.BadZipFile, OSError) as exc:
                    raise ValueError("下载内容不是可用 ZIP 技能包。") from exc
                subdirectory = selection.get("subdirectory", ())
                if subdirectory:
                    wrappers = list(stage.iterdir())
                    if len(wrappers) != 1 or not wrappers[0].is_dir():
                        raise ValueError("GitHub ZIP 缺少可识别的仓库根目录。")
                    search_root = wrappers[0].joinpath(*subdirectory)
                    if not search_root.is_dir() or not search_root.resolve().is_relative_to(wrappers[0].resolve()):
                        raise ValueError("GitHub 链接指定的技能子目录不存在。")
            elif source_path.is_dir():
                self._check_tree(source_path)
                if not (source_path / "SKILL.md").is_file():
                    raise ValueError("技能目录根部必须有 SKILL.md。")
                shutil.copytree(source_path, stage / source_path.name)
            elif source_path.suffix.lower() == ".md":
                text = self.read_item(source_path)
                metadata = self._frontmatter(text)
                folder = stage / _name(metadata.get("name"))
                folder.mkdir()
                (folder / "SKILL.md").write_bytes(text.encode("utf-8"))
            else:
                try:
                    if source_path.stat().st_size > MAX_ARCHIVE_BYTES:
                        raise ValueError("ZIP 超过 30 MB 下载限制。")
                    with zipfile.ZipFile(source_path) as archive:
                        self._extract(archive, stage)
                except (OSError, zipfile.BadZipFile) as exc:
                    raise ValueError("无法读取 ZIP 技能包。") from exc
            self._check_tree(stage)
            candidates = sorted({path.parent for path in search_root.rglob("SKILL.md")})
            if not candidates:
                raise ValueError("技能包中没有 SKILL.md。")
            if len(candidates) > 100:
                raise ValueError("一次最多导入 100 个技能。")
            plans: list[tuple[Path, Path]] = []
            destinations: set[str] = set()
            for candidate in candidates:
                if any(other != candidate and candidate.is_relative_to(other) for other in candidates):
                    raise ValueError("技能包不能嵌套另一个技能目录。")
                text = self.read_item(candidate / "SKILL.md")
                metadata = self._frontmatter(text)
                if not isinstance(metadata.get("name"), str) or not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
                    raise ValueError("导入技能缺少有效 name/description。")
                folder_name = _name(metadata["name"] if candidate == stage else candidate.name)
                destination = root / folder_name
                if folder_name.casefold() in destinations or destination.exists() or (self._disabled_root(root, "skills") / folder_name).exists():
                    raise ValueError("存在同名技能，导入没有覆盖原文件。")
                destinations.add(folder_name.casefold())
                plans.append((candidate, destination))
            installed: list[Path] = []
            try:
                root.mkdir(parents=True, exist_ok=True)
                for candidate, destination in plans:
                    # mkdir without exist_ok gives a final conflict check.
                    destination.mkdir()
                    installed.append(destination)
                    shutil.copytree(candidate, destination, dirs_exist_ok=True)
            except OSError as exc:
                for destination in installed:
                    shutil.rmtree(destination, ignore_errors=True)
                raise RuntimeError("导入失败；本次已写入项已回滚。") from exc
            return installed

    def import_github_skill(self, url: str, project: Path | None = None) -> list[Path]:
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("请提供 GitHub HTTPS 仓库或 ZIP 链接。")
        return self.import_skill(url, project)

    @staticmethod
    def _github_zip(url: str, *, selection: dict | None = None) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.query or parsed.fragment:
            raise ValueError("只支持公开 github.com HTTPS 仓库或 ZIP 链接。")
        parts = [unquote(item) for item in parsed.path.strip("/").split("/")]
        if (len(parts) < 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in parts[:2])
                or any(item in {"", ".", ".."} or "/" in item or "\\" in item or ":" in item or any(ord(char) < 32 for char in item) for item in parts)):
            raise ValueError("GitHub 仓库地址无效。")
        owner, repository = parts[:2]
        repository = repository.removesuffix(".git")
        if len(parts) == 2:
            candidates = [(f"https://github.com/{owner}/{repository}/archive/HEAD.zip", ())]
        elif len(parts) >= 4 and parts[2] == "archive" and parts[-1].endswith(".zip"):
            candidates = [(url, ())]
        elif len(parts) >= 4 and parts[2] == "tree":
            # GitHub branches may themselves contain slashes. Resolve the
            # longest available ref, trying at most six candidate boundaries.
            suffix = parts[3:]
            lengths = list(range(len(suffix), 0, -1))
            if len(lengths) > 6:
                lengths = lengths[:5] + [1]
            candidates = []
            for length in lengths:
                ref = "/".join(suffix[:length])
                address = f"https://github.com/{owner}/{repository}/archive/refs/heads/{quote(ref, safe='')}.zip"
                candidates.append((address, tuple(suffix[length:])))
        else:
            raise ValueError("请使用 GitHub 仓库首页、tree 子目录或 archive ZIP 链接。")
        allowed = {"github.com", "codeload.github.com"}
        session = requests.Session()
        session.trust_env = False
        def download(address: str) -> bytes | None:
            for _ in range(5):
                current = urlsplit(address)
                if current.scheme != "https" or current.hostname not in allowed or current.port not in {None, 443} or current.username or current.password:
                    raise ValueError("GitHub 下载跳转到了不允许的地址。")
                try:
                    addresses = socket.getaddrinfo(current.hostname, 443, type=socket.SOCK_STREAM)
                except OSError as exc:
                    raise RuntimeError("无法解析 GitHub 下载地址。") from exc
                if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                    raise ValueError("GitHub 下载地址解析到了非公开网络，已阻止。")
                with session.get(address, stream=True, timeout=(5, 15), allow_redirects=False, headers={"User-Agent": "TraeCNManager/1.0"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        address = urljoin(address, response.headers.get("Location", ""))
                        continue
                    if response.status_code == 404:
                        return None
                    if response.status_code != 200:
                        raise RuntimeError("GitHub 下载失败，请确认仓库公开且地址有效。")
                    declared = response.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > MAX_ARCHIVE_BYTES:
                        raise ValueError("GitHub ZIP 超过 30 MB 下载限制。")
                    chunks, total = [], 0
                    for chunk in response.iter_content(65536):
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            raise ValueError("GitHub ZIP 超过 30 MB 下载限制。")
                        chunks.append(chunk)
                    return b"".join(chunks)
            raise RuntimeError("GitHub 下载跳转次数过多。")
        try:
            for address, subdirectory in candidates:
                raw = download(address)
                if raw is not None:
                    if selection is not None:
                        selection["subdirectory"] = subdirectory
                    return raw
            raise RuntimeError("GitHub 分支或技能路径不存在；最多尝试 6 个分支边界，请核对链接或使用 ZIP。")
        except requests.RequestException as exc:
            raise RuntimeError("GitHub 下载连接失败或超时。") from exc
        finally:
            session.close()

    @_business_errors
    def export_skill(self, path: Path, destination: Path) -> Path:
        _, source, _ = self._owned("skills", path)
        self._check_tree(source)
        destination = Path(destination).resolve()
        if destination.exists():
            raise ValueError("导出文件已存在，请选择新文件名。")
        if destination.is_relative_to(source):
            raise ValueError("导出 ZIP 不能保存在被导出的技能目录内。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                for file in sorted(source.rglob("*")):
                    if file.is_file():
                        archive.write(file, str(Path(source.name) / file.relative_to(source)))
        except OSError as exc:
            raise RuntimeError("无法导出技能 ZIP。") from exc
        return destination

    def export_item(self, path: Path, destination: Path) -> Path:
        """Convenience alias for a skill ZIP export."""
        return self.export_skill(path, destination)

    @_business_errors
    def restore_backup(self, backup: Path, *, overwrite: bool = False) -> Path:
        backup = Path(backup).resolve()
        try:
            backup.relative_to(self.backup_dir)
            metadata = json.loads(backup.with_name(backup.name + ".restore.json").read_text(encoding="utf-8"))
            destination = Path(metadata["original_path"])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise ValueError("恢复备份或备份元数据无效。") from exc
        if not backup.exists():
            raise ValueError("备份文件不存在。")
        if destination.exists() and not overwrite:
            raise ValueError("原位置已有文件，恢复未覆盖。")
        try:
            if destination.exists():
                self._backup(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if backup.is_dir():
                self._check_tree(backup)
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(backup, destination)
            else:
                self._write(destination, backup.read_bytes(), _fingerprint(destination))
            return destination
        except OSError as exc:
            raise RuntimeError("恢复失败，请检查权限和文件占用。") from exc

    def probe_mcp(self, config: dict) -> dict:
        """Explicit action only: initialize and tools/list, then disconnect.

        This can download/run the command configured by the user. Call it only
        after the user clicks the application's connection test action.
        """
        transport = _server(config)
        started = time.monotonic()
        try:
            result = self._probe_stdio(config) if transport == "stdio" else self._probe_http(config, transport)
            result.update({"ok": True, "success": True, "message": "MCP initialize 和 tools/list 检测成功。"})
        except (ValueError, RuntimeError, OSError, requests.RequestException, queue.Empty, subprocess.TimeoutExpired):
            result = {"ok": False, "success": False, "message": "MCP 连接、初始化或工具列表检测失败/超时。请检查启动命令、依赖、权限和网络。", "tools": [], "tool_count": 0}
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        return result

    @staticmethod
    def _initialize() -> dict:
        return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "trae-cn-manager", "version": "1.0.0"}}}

    @staticmethod
    def _tools(result: dict) -> dict:
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise ValueError("tools/list 响应无效。")
        names = [item["name"] for item in tools if isinstance(item, dict) and isinstance(item.get("name"), str)]
        return {"tools": names, "tool_count": len(names)}

    def _probe_stdio(self, config: dict) -> dict:
        command = [config["command"], *config.get("args", [])]
        if os.name == "nt":
            resolved = shutil.which(command[0])
            if resolved:
                command[0] = resolved
        env = os.environ.copy()
        env.update(config.get("env", {}))
        kwargs: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "text": True, "encoding": "utf-8", "errors": "replace", "env": env, "bufsize": 1}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        messages: queue.Queue = queue.Queue(maxsize=100)
        stop = threading.Event()
        deadline = time.monotonic() + PROBE_TIMEOUT

        def receive() -> None:
            try:
                while not stop.is_set():
                    line = process.stdout.readline(MAX_CONFIG_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_CONFIG_BYTES:
                        break
                    try:
                        parsed = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    try:
                        messages.put(parsed, timeout=0.1)
                    except queue.Full:
                        break
            finally:
                try:
                    messages.put_nowait(None)
                except queue.Full:
                    pass

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()

        def send(value: dict) -> None:
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()

        def wait_for(identifier: int) -> dict:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeError("MCP 检测超时。")
                message = messages.get(timeout=left)
                if message is None:
                    raise RuntimeError("MCP 进程提前退出。")
                if isinstance(message, dict) and message.get("id") == identifier:
                    if "error" in message or not isinstance(message.get("result"), dict):
                        raise RuntimeError("MCP 协议响应错误。")
                    return message["result"]

        try:
            send(self._initialize())
            initialize = wait_for(1)
            if not isinstance(initialize.get("protocolVersion"), str):
                raise ValueError("initialize 响应缺少协议版本。")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            result = self._tools(wait_for(2))
            result["protocol_version"] = initialize["protocolVersion"]
            return result
        finally:
            stop.set()
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    try:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, creationflags=subprocess.CREATE_NO_WINDOW)
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
                else:
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            if process.stdout:
                process.stdout.close()

    @staticmethod
    def _bounded_stream(response: requests.Response, deadline: float, *, lines: bool = False):
        """Bound wall time even if a server sends heartbeats or incomplete lines."""
        pending: queue.Queue = queue.Queue(maxsize=100)
        stopped = threading.Event()

        def produce() -> None:
            try:
                iterator = response.iter_lines(chunk_size=1, decode_unicode=True) if lines else response.iter_content(chunk_size=4096)
                for item in iterator:
                    if stopped.is_set():
                        return
                    try:
                        pending.put(("data", item), timeout=0.1)
                    except queue.Full:
                        return
                pending.put(("end", None), timeout=0.1)
            except Exception:
                try:
                    pending.put(("error", None), timeout=0.1)
                except queue.Full:
                    pass

        threading.Thread(target=produce, daemon=True).start()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("HTTP MCP 检测超时。")
                try:
                    kind, item = pending.get(timeout=remaining)
                except queue.Empty as exc:
                    raise RuntimeError("HTTP MCP 检测超时。") from exc
                if kind == "end":
                    return
                if kind == "error":
                    raise RuntimeError("HTTP MCP 流读取失败。")
                yield item
        finally:
            stopped.set()

    @staticmethod
    def _http_message(response: requests.Response, identifier: int, deadline: float | None = None) -> dict:
        deadline = deadline or (time.monotonic() + PROBE_TIMEOUT)
        if response.status_code not in {200, 201}:
            raise RuntimeError("HTTP MCP 返回错误状态。")
        if "text/event-stream" in response.headers.get("Content-Type", ""):
            total, parts = 0, []
            for line in ConfigManager._bounded_stream(response, deadline, lines=True):
                line = line or ""
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                total += len(line)
                if total > MAX_CONFIG_BYTES:
                    raise ValueError("HTTP MCP 响应过大。")
                if line.startswith("data:"):
                    parts.append(line[5:].lstrip())
                elif not line and parts:
                    try:
                        value = json.loads("\n".join(parts))
                    except ValueError:
                        parts.clear()
                        continue
                    parts.clear()
                    if isinstance(value, dict) and value.get("id") == identifier:
                        if "error" in value or not isinstance(value.get("result"), dict):
                            raise RuntimeError("MCP 协议响应错误。")
                        return value["result"]
            raise RuntimeError("SSE 没有返回所需 MCP 响应。")
        raw = bytearray()
        for chunk in ConfigManager._bounded_stream(response, deadline):
            raw.extend(chunk)
            if len(raw) > MAX_CONFIG_BYTES:
                raise ValueError("HTTP MCP 响应过大。")
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise ValueError("HTTP MCP 响应不是 JSON。") from exc
        if not isinstance(value, dict) or value.get("id") != identifier or "error" in value or not isinstance(value.get("result"), dict):
            raise RuntimeError("HTTP MCP 响应格式错误。")
        return value["result"]

    def _probe_http(self, config: dict, transport: str) -> dict:
        session = requests.Session()
        session.trust_env = False
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **config.get("headers", {})}
        deadline = time.monotonic() + PROBE_TIMEOUT

        def timeout() -> tuple[float, float]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("HTTP MCP 检测超时。")
            return min(3, remaining), min(3, remaining)

        try:
            if transport == "sse":
                return self._probe_legacy_sse(session, config["url"], headers, deadline)
            with session.post(config["url"], json=self._initialize(), headers=headers, timeout=timeout(), stream=True, allow_redirects=False) as response:
                if response.status_code in {404, 405, 415} and transport == "http":
                    response.close()
                    return self._probe_legacy_sse(session, config["url"], headers, deadline)
                initialize = self._http_message(response, 1, deadline)
                session_id = response.headers.get("Mcp-Session-Id")
            if not isinstance(initialize.get("protocolVersion"), str):
                raise ValueError("initialize 响应缺少协议版本。")
            headers["MCP-Protocol-Version"] = initialize["protocolVersion"]
            if session_id:
                headers["Mcp-Session-Id"] = session_id
            with session.post(config["url"], json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers, timeout=timeout(), stream=True, allow_redirects=False) as response:
                if response.status_code not in {200, 202, 204}:
                    raise RuntimeError("HTTP initialized 通知失败。")
            with session.post(config["url"], json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=headers, timeout=timeout(), stream=True, allow_redirects=False) as response:
                result = self._tools(self._http_message(response, 2, deadline))
            result["protocol_version"] = initialize["protocolVersion"]
            return result
        finally:
            session.close()

    def _probe_legacy_sse(self, session: requests.Session, url: str, headers: dict, deadline: float | None = None) -> dict:
        deadline = deadline or (time.monotonic() + PROBE_TIMEOUT)
        remaining = max(0.01, deadline - time.monotonic())
        with session.get(url, headers=headers, timeout=(min(3, remaining), min(3, remaining)), stream=True, allow_redirects=False) as response:
            if response.status_code != 200 or "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise RuntimeError("SSE 握手失败。")
            lines = self._bounded_stream(response, deadline, lines=True)
            event, parts = "", []
            total, endpoint = 0, None

            def next_event() -> tuple[str, str]:
                nonlocal event, parts, total
                for raw in lines:
                    if time.monotonic() > deadline:
                        raise RuntimeError("SSE 检测超时。")
                    line = raw.decode("utf-8") if isinstance(raw, bytes) else (raw or "")
                    total += len(line)
                    if total > MAX_CONFIG_BYTES:
                        raise ValueError("SSE 响应过大。")
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        parts.append(line[5:].lstrip())
                    elif not line and parts:
                        result = event, "\n".join(parts)
                        event, parts = "", []
                        return result
                raise RuntimeError("SSE 连接提前结束。")

            while endpoint is None:
                event_name, data = next_event()
                if event_name == "endpoint":
                    endpoint = urljoin(url, data)
                    a, b = urlsplit(url), urlsplit(endpoint)
                    if (a.scheme, a.hostname, a.port) != (b.scheme, b.hostname, b.port):
                        raise ValueError("SSE endpoint 必须与原服务同源。")

            def post(value: dict) -> None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("SSE 检测超时。")
                with session.post(endpoint, json=value, headers=headers, timeout=(min(3, remaining), min(3, remaining)), stream=True, allow_redirects=False) as reply:
                    if reply.status_code not in {200, 201, 202, 204}:
                        raise RuntimeError("SSE 消息发送失败。")

            def wait_for(identifier: int) -> dict:
                while True:
                    _, data = next_event()
                    try:
                        value = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(value, dict) and value.get("id") == identifier:
                        if "error" in value or not isinstance(value.get("result"), dict):
                            raise RuntimeError("SSE 协议响应错误。")
                        return value["result"]

            post(self._initialize())
            initialize = wait_for(1)
            if not isinstance(initialize.get("protocolVersion"), str):
                raise ValueError("initialize 响应缺少协议版本。")
            post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            result = self._tools(wait_for(2))
            result["protocol_version"] = initialize["protocolVersion"]
            return result
