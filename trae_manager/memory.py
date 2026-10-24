"""TRAE's shared local Markdown memories, with explicit account snapshots.

The native client stores memories under its global home, not in per-account
folders. Account-specific copies belong in the manager's encrypted vault.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import unicodedata

from .security import atomic_write


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_FILES = 500
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_RESERVED_WINDOWS = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.I)


def _key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _frontmatter(text: str) -> tuple[str, str]:
    """Preserve native metadata verbatim; never merge one account's metadata."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.strip() in ("---", "..."):
                return "".join(lines[:index + 1]), "".join(lines[index + 1:])
    return "", text


def _sections(text: str) -> list[dict]:
    """Recognize headings outside fenced code; paragraphs remain intact."""
    sections = [{"heading": "", "blocks": []}]
    paragraph: list[str] = []
    fence = ""
    def flush():
        if paragraph:
            sections[-1]["blocks"].append("\n".join(paragraph).strip())
            paragraph.clear()
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            run = marker.group(1)
            if not fence:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = ""
            paragraph.append(line)
            continue
        heading = _HEADING.match(line) if not fence else None
        if heading:
            flush()
            sections.append({"heading": line.strip(), "blocks": []})
        elif not line.strip() and not fence:
            flush()
        else:
            paragraph.append(line)
    flush()
    return sections


def _merge(target: str, source: str) -> tuple[str, int, int]:
    front, body = _frontmatter(target)
    _, source_body = _frontmatter(source)
    sections = _sections(body)
    seen = {_key(block) for section in sections for block in section["blocks"]}
    added = duplicates = 0
    for section in _sections(source_body):
        heading_key = _key(section["heading"])
        destination = next((item for item in sections if _key(item["heading"]) == heading_key), None)
        novel = []
        for block in section["blocks"]:
            key = _key(block)
            if key in seen:
                duplicates += 1
            else:
                novel.append(block)
                seen.add(key)
                added += 1
        if novel:
            if destination is None:
                destination = {"heading": section["heading"], "blocks": []}
                sections.append(destination)
            destination["blocks"].extend(novel)
    if not added:
        return target, 0, duplicates
    parts = []
    for section in sections:
        parts.extend([section["heading"]] if section["heading"] else [])
        parts.extend(section["blocks"])
    merged_body = "\n\n".join(parts).strip()
    return front + ("\n" if front else "") + merged_body + "\n", added, duplicates


class MemoryManager:
    def __init__(self, global_home: Path, backup_callback=None):
        self.global_home = Path(global_home).expanduser().resolve()
        self.root = self.global_home / "memory"
        self.backup_callback = backup_callback
        self._lock = threading.RLock()

    @staticmethod
    def _relative(relative: str) -> str:
        if not isinstance(relative, str) or "\\" in relative or "\x00" in relative:
            raise ValueError("记忆路径无效。")
        path = PurePosixPath(relative)
        parts = path.parts
        valid = parts == ("user_profile.md",) or (len(parts) == 3 and parts[0] == "projects" and parts[2] == "project_memory.md")
        if not valid or path.is_absolute() or any(p in ("", ".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', p) or p.endswith((" ", ".")) or _RESERVED_WINDOWS.match(p) for p in parts):
            raise ValueError("仅支持 TRAE 的用户记忆和项目记忆 Markdown 文件。")
        # PurePath normalizes repeated separators and dot segments; reject them
        # instead of silently accepting a noncanonical snapshot path.
        if path.as_posix() != relative:
            raise ValueError("记忆路径必须使用规范的相对路径。")
        return relative

    def _path(self, relative: str) -> Path:
        relative = self._relative(relative)
        path = self.root.joinpath(*PurePosixPath(relative).parts)
        current = self.global_home
        for name in ("memory", *PurePosixPath(relative).parts):
            current /= name
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("记忆目录中包含符号链接或目录联接，已停止操作。")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("记忆路径超出允许的目录。")
        return path

    def _read(self, relative: str) -> tuple[str | None, str | None]:
        path = self._path(relative)
        try:
            info = path.stat()
        except FileNotFoundError:
            return None, None
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError("记忆文件类型不支持或超过 2 MB。")
        # Read through an open handle, checking both handle and directory entry.
        # This detects replacement while reading and prevents accepting a link.
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        self._path(relative)
        fresh = path.stat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if identity(before) != identity(after) or identity(after) != identity(fresh):
            raise ValueError("记忆文件正在被其他程序修改，请稍后重试。")
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("记忆文件超过 2 MB。")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeError:
            raise ValueError("记忆文件必须是 UTF-8 Markdown。") from None
        return content, hashlib.sha256(raw).hexdigest()

    def _list(self) -> list[dict]:
        self._path("user_profile.md")
        paths = ["user_profile.md"]
        projects = self.root / "projects"
        self._path("projects/_check_/project_memory.md")
        if projects.is_dir():
            with os.scandir(projects) as entries:
                for entry in entries:
                    if len(paths) >= MAX_FILES:
                        raise ValueError("记忆文件数量超过 500，请减少后再试。")
                    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                    if entry.is_symlink() or attributes & 0x400:
                        raise ValueError("记忆项目目录中包含符号链接或目录联接。")
                    if entry.is_dir(follow_symlinks=False):
                        paths.append(f"projects/{entry.name}/project_memory.md")
        result = []
        total = 0
        for relative in sorted(paths):
            content, _ = self._read(relative)
            if content is None:
                continue
            total += len(content.encode("utf-8"))
            if total > MAX_TOTAL_BYTES:
                raise ValueError("记忆总大小超过 8 MB。")
            _, body = _frontmatter(content)
            title = next((match.group(2) for line in body.splitlines() if (match := _HEADING.match(line))), "用户记忆" if relative == "user_profile.md" else PurePosixPath(relative).parts[1])
            result.append({"relative": relative, "title": title, "content": content})
        return result

    def list_memories(self) -> list[dict]:
        with self._lock:
            return self._list()

    def snapshot(self) -> dict:
        with self._lock:
            return {"scope": "local-shared", "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": [{"relative": row["relative"], "content": row["content"]} for row in self._list()]}

    def _validate_snapshot(self, snapshot: dict) -> list[dict]:
        if not isinstance(snapshot, dict) or snapshot.get("scope") != "local-shared" or not isinstance(snapshot.get("files"), list):
            raise ValueError("不是受支持的本地共享记忆快照。")
        files = snapshot["files"]
        if len(files) > MAX_FILES:
            raise ValueError("记忆快照文件数量超过 500。")
        result, seen, total = [], set(), 0
        for row in files:
            if not isinstance(row, dict):
                raise ValueError("记忆快照文件格式无效。")
            relative = self._relative(row.get("relative"))
            if relative.casefold() in seen:
                raise ValueError("记忆快照包含重复路径。")
            seen.add(relative.casefold())
            content = row.get("content")
            if not isinstance(content, str) or "\x00" in content:
                raise ValueError("记忆快照内容必须是 Markdown 文本。")
            size = len(content.encode("utf-8"))
            total += size
            if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("记忆快照超过文件或总大小限制。")
            result.append({"relative": relative, "content": content})
        return result

    def _plan(self, snapshot: dict) -> tuple[dict, list[dict]]:
        source = self._validate_snapshot(snapshot)
        target = self._list()
        details, writes = [], []
        add_count = changed_count = duplicates = 0
        for row in source:
            relative = row["relative"]
            content, fingerprint = self._read(relative)
            if content is None:
                merged, added, repeated = row["content"], len([b for s in _sections(_frontmatter(row["content"])[1]) for b in s["blocks"]]), 0
                action = "add"
                add_count += 1
            else:
                merged, added, repeated = _merge(content, row["content"])
                action = "change" if merged != content else "unchanged"
                changed_count += action == "change"
            duplicates += repeated
            details.append({"relative": relative, "action": action, "added_paragraphs": added, "duplicates": repeated})
            if action != "unchanged":
                writes.append({"relative": relative, "content": merged, "fingerprint": fingerprint})
        return {"source_count": len(source), "target_count": len(target), "add_count": add_count, "changed_count": changed_count, "duplicates": duplicates, "details": details, "scope": "local-shared"}, writes

    def preview_merge(self, snapshot: dict) -> dict:
        with self._lock:
            return self._plan(snapshot)[0]

    def merge_snapshot(self, snapshot: dict) -> dict:
        with self._lock:
            summary, writes = self._plan(snapshot)
            # Preflight all files before any mutation. Existing files are backed
            # up before writing; service also stores the full encrypted history.
            for row in writes:
                path = self._path(row["relative"])
                if row["fingerprint"] is not None and self.backup_callback:
                    self.backup_callback(path)
            for row in writes:
                _, current = self._read(row["relative"])
                if current != row["fingerprint"]:
                    raise ValueError("记忆已被其他程序修改，已停止合并；请刷新预览。")
            written = []
            for row in writes:
                path = self._path(row["relative"])
                _, current = self._read(row["relative"])
                if current != row["fingerprint"]:
                    raise ValueError("记忆已被其他程序修改，已停止合并；请刷新预览。")
                atomic_write(path, row["content"].encode("utf-8"))
                written.append(row["relative"])
            return {**summary, "written": written, "message": f"记忆合并完成：新增 {summary['add_count']} 个文件，更新 {summary['changed_count']} 个文件，跳过 {summary['duplicates']} 个重复段落。"}

    def restore_snapshot(self, snapshot: dict) -> dict:
        """Explicit full-memory rollback, including removal of later additions.

        This is deliberately separate from merge_snapshot: ordinary account
        synchronization never removes memories. The caller presents restore
        confirmation and retains the current snapshot in encrypted history.
        """
        with self._lock:
            source = self._validate_snapshot(snapshot)
            # The application targets Windows: differently cased snapshot names
            # may refer to the same file. Do not schedule a delete and a write
            # for the same native memory under two spellings.
            existing = {row["relative"].casefold(): row for row in self._list()}
            desired = {row["relative"].casefold(): row for row in source}
            changes = []
            for key in sorted(existing.keys() | desired.keys()):
                relative = (existing.get(key) or desired[key])["relative"]
                content, fingerprint = self._read(relative)
                replacement = desired.get(key, {}).get("content")
                if content != replacement:
                    changes.append({"relative": relative, "fingerprint": fingerprint, "content": replacement})
            for row in changes:
                if row["fingerprint"] is not None and self.backup_callback:
                    self.backup_callback(self._path(row["relative"]))
            for row in changes:
                _, fingerprint = self._read(row["relative"])
                if fingerprint != row["fingerprint"]:
                    raise ValueError("记忆已被其他程序修改，已停止恢复；请刷新后重试。")
            removed, restored = [], []
            for row in changes:
                path = self._path(row["relative"])
                _, fingerprint = self._read(row["relative"])
                if fingerprint != row["fingerprint"]:
                    raise ValueError("记忆已被其他程序修改，已停止恢复；请刷新后重试。")
                if row["content"] is None:
                    path.unlink()
                    removed.append(row["relative"])
                else:
                    atomic_write(path, row["content"].encode("utf-8"))
                    restored.append(row["relative"])
            return {"restored": restored, "removed": removed, "scope": "local-shared", "message": f"记忆恢复完成：写入 {len(restored)} 个文件，移除 {len(removed)} 个快照之后新增的记忆文件。"}
