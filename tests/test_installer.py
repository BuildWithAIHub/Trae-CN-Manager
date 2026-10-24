"""Installer acceptance: standalone payload, private data and failed upgrades."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import zipfile

import pytest

from installer.core import install, MARKER, safe_relative


def payload(tmp_path, files=None, hashes=None):
    files = files or {"TraeCNManager.exe": b"new exe", "_internal/python311.dll": b"new runtime", "LICENSE": b"license"}
    archive = tmp_path / "payload.zip"
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    manifest.update(hashes or {})
    with zipfile.ZipFile(archive, "w") as output:
        for name, data in files.items():
            output.writestr(name, data)
        output.writestr("payload-manifest.json", json.dumps({"version": "0.2.1", "files": manifest}))
    return archive


def test_complete_install_and_upgrade_preserve_user_data(tmp_path):
    archive = payload(tmp_path)
    destination = tmp_path / "installed"
    result = install(archive, destination)
    assert result["installed"] and result["files"] == 3
    assert (destination / "_internal/python311.dll").read_bytes() == b"new runtime"
    data = destination / "data"
    data.mkdir()
    for name in ("accounts.vault", "background.key", "recovery.json", "locations.json"):
        (data / name).write_bytes(b"private-user-content")
    (destination / "my-note.txt").write_text("keep", encoding="utf-8")
    (destination / "TraeCNManager.exe").write_bytes(b"old exe")
    install(archive, destination)
    assert all(path.read_bytes() == b"private-user-content" for path in data.iterdir())
    assert (destination / "my-note.txt").read_text(encoding="utf-8") == "keep"
    assert (destination / "TraeCNManager.exe").read_bytes() == b"new exe"
    assert json.loads((destination / MARKER).read_text(encoding="utf-8"))["version"] == "0.2.1"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "_internal/../../escape", "data/accounts.vault", "DATA/locations.json", "CON", "_internal/aux.txt", "a\\b", "file:stream", "ending."])
def test_payload_rejects_unsafe_or_private_paths(name):
    with pytest.raises(ValueError):
        safe_relative(name)


def test_corrupt_payload_never_modifies_destination(tmp_path):
    archive = payload(tmp_path, hashes={"LICENSE": "0" * 64})
    destination = tmp_path / "installed"
    with pytest.raises(ValueError, match="完整性"):
        install(archive, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".tcn-setup-*"))


def test_failed_upgrade_rolls_back_changed_files(tmp_path):
    archive = payload(tmp_path)
    destination = tmp_path / "installed"
    destination.mkdir()
    (destination / "TraeCNManager.exe").write_bytes(b"original exe")
    (destination / "LICENSE").write_bytes(b"original license")
    (destination / "data").mkdir()
    (destination / "data/accounts.vault").write_bytes(b"unchanged")
    import installer.core as core
    replace = core.os.replace
    def fail_on_exe(source, target):
        if Path(target).name == "TraeCNManager.exe":
            raise PermissionError("file in use")
        return replace(source, target)
    with patch("installer.core.os.replace", side_effect=fail_on_exe), pytest.raises(PermissionError):
        install(archive, destination)
    assert (destination / "LICENSE").read_bytes() == b"original license"
    assert (destination / "TraeCNManager.exe").read_bytes() == b"original exe"
    assert (destination / "data/accounts.vault").read_bytes() == b"unchanged"
    assert not (destination / "_internal").exists()
    assert not (destination / MARKER).exists()
    assert not list(destination.rglob("*.tcn-installing"))


def test_do_not_install_into_unrelated_nonempty_directory(tmp_path):
    archive = payload(tmp_path)
    (tmp_path / "work").mkdir()
    (tmp_path / "work/user.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="已有其他内容"):
        install(archive, tmp_path / "work")


def test_rollback_failure_retains_originals_and_continues_other_restores(tmp_path):
    archive = payload(tmp_path)
    destination = tmp_path / "installed"
    install(archive, destination)
    (destination / "LICENSE").write_bytes(b"original license")
    (destination / "_internal/python311.dll").write_bytes(b"original runtime")
    import installer.core as core
    replace = core._replace_staged
    def locked(source, target):
        if target.name == "TraeCNManager.exe" or (target.name == "LICENSE" and "previous" in source.parts):
            raise PermissionError("simulated locked file")
        return replace(source, target)
    with patch("installer.core._replace_staged", side_effect=locked), pytest.raises(RuntimeError, match="备份已保留"):
        install(archive, destination)
    backups = list(tmp_path.glob(".tcn-setup-*/previous"))
    assert len(backups) == 1
    assert (backups[0] / "LICENSE").read_bytes() == b"original license"
    assert (destination / "_internal/python311.dll").read_bytes() == b"original runtime"
    recovery = json.loads((backups[0].parent / "recovery.json").read_text(encoding="utf-8"))
    assert recovery["unrestored_files"] == ["LICENSE"]


def test_fixed_temp_name_is_never_followed_or_overwritten(tmp_path):
    archive = payload(tmp_path)
    destination = tmp_path / "installed"
    destination.mkdir()
    (destination / "TraeCNManager.exe").write_bytes(b"old")
    reserved = destination / "TraeCNManager.exe.tcn-installing"
    reserved.write_bytes(b"unrelated file, do not touch")
    install(archive, destination)
    assert reserved.read_bytes() == b"unrelated file, do not touch"
    assert not list(destination.rglob(".tcn-install-*"))
