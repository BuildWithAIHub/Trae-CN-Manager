"""Verified, reversible installation of the complete portable application."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile


MANIFEST = "payload-manifest.json"
MARKER = ".trae-cn-manager-install.json"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def safe_relative(name: str) -> Path:
    parts = PurePosixPath(name).parts
    if (not parts or "\\" in name or ":" in name or name.startswith("/")
            or any(p in (".", "..") or p.endswith((".", " ")) for p in parts)):
        raise ValueError("安装包包含不安全的文件路径")
    if parts[0].lower() in {"data", ".updates", "accounts.vault", MARKER.lower()}:
        raise ValueError("安装包不得包含用户数据或安装状态")
    if any(p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for p in parts):
        raise ValueError("安装包包含保留文件名")
    return Path(*parts)


def _assert_destination(root: Path, relative: Path) -> Path:
    target = root / relative
    # Do not traverse a user-created symlink or NTFS junction when upgrading.
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            attributes = current.lstat()
            if current.is_symlink() or getattr(attributes, "st_file_attributes", 0) & 0x400:
                raise ValueError("安装目标含目录链接，请选择普通文件夹")
    if not target.resolve().is_relative_to(root):
        raise ValueError("安装目标超出所选文件夹")
    return target


@contextmanager
def _staging_directory(parent):
    path = Path(tempfile.mkdtemp(prefix=".tcn-setup-", dir=parent))
    keep = [False]
    try:
        yield path, keep
    finally:
        if not keep[0]:
            shutil.rmtree(path)


def _replace_staged(source, target):
    # Exclusive, random same-directory temp: never follow a pre-existing link.
    descriptor, name = tempfile.mkstemp(prefix=".tcn-install-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as stream:
            shutil.copyfileobj(stream, output)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def install(payload: Path, destination: Path, progress=None) -> dict:
    """Only replace manifest-owned files. Never read, copy or remove data/."""
    destination = destination.expanduser().absolute()
    if destination.is_symlink() or (destination.exists() and getattr(destination.lstat(), "st_file_attributes", 0) & 0x400):
        raise ValueError("安装目录不能是目录链接")
    destination = destination.resolve()
    if destination == Path(destination.anchor) or destination == Path.home().resolve():
        raise ValueError("请选择独立的软件文件夹")
    if destination.exists() and any(destination.iterdir()) and not ((destination / "TraeCNManager.exe").is_file() or (destination / MARKER).is_file()):
        raise ValueError("所选文件夹已有其他内容，请选择空文件夹或本软件的已有安装目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    existed = destination.exists()
    with _staging_directory(destination.parent) as (staging, keep_staging):
        unpacked, previous = staging / "new", staging / "previous"
        unpacked.mkdir()
        with zipfile.ZipFile(payload) as archive:
            manifest = json.loads(archive.read(MANIFEST))
            files = manifest["files"]
            if not isinstance(files, dict) or "TraeCNManager.exe" not in files or not any(name.startswith("_internal/") for name in files):
                raise ValueError("安装包不完整：缺少主程序或运行依赖")
            seen = set()
            for index, info in enumerate(archive.infolist()):
                if info.filename == MANIFEST:
                    continue
                relative = safe_relative(info.filename)
                folded = str(relative).casefold()
                if folded in seen or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError("安装包包含重复文件、目录或文件链接")
                seen.add(folded)
                if info.filename not in files or info.file_size > 512 * 1024 * 1024:
                    raise ValueError("安装包文件清单不匹配")
                target = unpacked / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if digest(target) != files[info.filename]:
                    raise ValueError("安装包完整性校验失败，请重新下载")
                if progress:
                    progress(min(70, int((index + 1) / len(files) * 70)))
            if len(seen) != len(files):
                raise ValueError("安装包缺少清单中的文件")
        # Validate all paths before the first write; leave user data untouched.
        targets = [(name, _assert_destination(destination, safe_relative(name))) for name in files]
        marker = _assert_destination(destination, Path(MARKER))
        (unpacked / MARKER).write_text(json.dumps({"product": "TraeCNManager", "version": manifest["version"], "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")
        committed = []
        created_dirs = []
        destination.mkdir(exist_ok=True)
        try:
            # The executable is last, so a failed DLL copy leaves the old EXE.
            targets.sort(key=lambda item: item[0] == "TraeCNManager.exe")
            targets.append((MARKER, marker))
            for index, (name, target) in enumerate(targets):
                if target.exists() and not target.is_file():
                    raise ValueError("文件路径已被文件夹占用")
                old = previous / name
                had_old = target.exists()
                if had_old:
                    old.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, old)
                missing = target.parent
                while missing != destination and not missing.exists():
                    created_dirs.append(missing)
                    missing = missing.parent
                target.parent.mkdir(parents=True, exist_ok=True)
                _replace_staged(unpacked / name, target)
                committed.append((target, old, had_old))
                if progress:
                    progress(70 + int((index + 1) / len(targets) * 29))
        except Exception as original:
            failed = []
            for target, old, had_old in reversed(committed):
                try:
                    if had_old:
                        _replace_staged(old, target)
                    else:
                        target.unlink(missing_ok=True)
                except Exception:
                    failed.append(str(target.relative_to(destination)))
            if failed:
                keep_staging[0] = True
                try:
                    (staging / "recovery.json").write_text(json.dumps({"destination": str(destination), "unrestored_files": failed}, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError:
                    pass
                raise RuntimeError(f"安装失败，部分文件被占用而未能恢复。原文件备份已保留于：{previous}。请退出旧程序后恢复，或重新运行安装器。") from original
            for directory in sorted(set(created_dirs), key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if not existed:
                try:
                    destination.rmdir()
                except OSError:
                    pass
            raise
        if progress:
            progress(100)
        return {"installed": True, "version": manifest["version"], "directory": str(destination), "files": len(files), "user_data_preserved": True}
