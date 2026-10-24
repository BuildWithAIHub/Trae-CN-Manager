"""Fast discovery tests are synthetic and never query live processes/accounts."""
import json
from pathlib import Path
import struct
from unittest import mock

from trae_manager.trae import TraeClient, _shortcut_target, _user_data_argument


def installed(root):
    exe = root / "Trae CN.exe"
    root.mkdir(parents=True)
    exe.write_bytes(b"synthetic")
    app = root / "resources" / "app"
    app.mkdir(parents=True)
    (app / "product.json").write_text(json.dumps({"packageType": "TRAE_CN", "providerCode": "cn", "appVersion": "3.4.3", "bootConfig": {"ug": {"trae": {"normal": "https://api.trae.cn"}}}}), encoding="utf-8")
    return exe


def native_profile(root):
    folder = root / "User" / "globalStorage"
    folder.mkdir(parents=True)
    (folder / "storage.json").write_text("NEVER READ THIS SYNTHETIC ACCOUNT CONTENT", encoding="utf-8")
    (root / "aha").mkdir()
    return root


def test_running_official_process_precedes_registry_and_shortcuts(tmp_path, monkeypatch):
    exe = installed(tmp_path / "live")
    data = native_profile(tmp_path / "isolated")
    monkeypatch.setenv("APPDATA", str(tmp_path / "not-installed"))
    client = TraeClient()
    with mock.patch("trae_manager.trae._running_trae", return_value=[{"executable": str(exe), "profile": str(data)}]), mock.patch("trae_manager.trae._registry_candidates", side_effect=AssertionError("process already found")), mock.patch("trae_manager.trae._hidden_powershell", side_effect=AssertionError("no powershell")):
        result = client.discover()
    assert result["executable"] == str(exe)
    assert result["executable_source"] == "process"
    assert result["profiles"][0]["path"] == str(data)
    assert result["profiles"][0]["source"] == "process"


def test_compatibility_marker_read_is_cached_and_invalidates_after_update(tmp_path):
    from trae_manager.trae import STATUS_PATH, CLAIM_PATH, USAGE_PATH
    exe = installed(tmp_path / "app")
    main = exe.parent / "resources" / "app" / "out" / "main.js"
    main.parent.mkdir()
    text = "\n".join([STATUS_PATH, CLAIM_PATH, USAGE_PATH, "_requestCheckinCredits", "userStorage.js", "byteCrypto.js", "getDeviceKeyPair", "storeUserInfo"])
    main.write_text(text, encoding="utf-8")
    client = TraeClient(exe)
    assert client._compatibility()["state"] == "baseline"
    actual = Path.read_text
    def read(path, *args, **kwargs):
        if path == main:
            raise AssertionError("unchanged main.js must not be reread")
        return actual(path, *args, **kwargs)
    with mock.patch.object(Path, "read_text", read):
        assert client._compatibility()["state"] == "baseline"
    main.write_text("different schema", encoding="utf-8")
    assert client._compatibility()["state"] == "unverified"


def test_local_unicode_shell_link_is_read_without_powershell(tmp_path):
    target = str(tmp_path / "中文目录" / "Trae CN.exe")
    encoded = (target + "\x00").encode("utf-16le")
    header = bytearray(76)
    struct.pack_into("<I", header, 0, 76)
    struct.pack_into("<I", header, 20, 2)
    info = bytearray(36)
    struct.pack_into("<II", info, 0, 36 + len(encoded), 36)
    struct.pack_into("<I", info, 28, 36)
    shortcut = tmp_path / "Trae CN.lnk"
    shortcut.write_bytes(header + info + encoded)
    with mock.patch("trae_manager.trae._hidden_powershell", side_effect=AssertionError("native shortcut parser expected")):
        assert _shortcut_target(shortcut) == Path(target)
        assert _shortcut_target(shortcut) == Path(target)


def test_user_data_argument_returns_only_profile_and_handles_quotes():
    assert _user_data_argument('"E:\\App\\Trae CN.exe" --user-data-dir "G:\\资料\\账号 1" --token PRIVATE') == "G:\\资料\\账号 1"
    assert _user_data_argument('app.exe --user-data-dir=F:\\Profile --other project') == "F:\\Profile"
    assert _user_data_argument('app.exe --token PRIVATE') is None


def test_known_profile_junctions_deduplicate_to_physical_location(tmp_path, monkeypatch):
    exe = installed(tmp_path / "app")
    appdata = tmp_path / "Roaming"
    appdata.mkdir()
    real = native_profile(appdata / "Trae CN")
    alternate = appdata / "TraeCode CN"
    try:
        alternate.symlink_to(real, target_is_directory=True)
    except OSError:
        return  # Symlink privileges can be unavailable on Windows.
    monkeypatch.setenv("APPDATA", str(appdata))
    with mock.patch("trae_manager.trae._running_trae", return_value=[]):
        result = TraeClient(exe).discover()
    assert [row["path"] for row in result["profiles"]] == [str(real.resolve())]


def test_cached_executable_still_discovers_other_running_profile(tmp_path, monkeypatch):
    exe = installed(tmp_path / "app")
    appdata = tmp_path / "Roaming"
    default = native_profile(appdata / "Trae CN")
    separate = native_profile(tmp_path / "elsewhere" / "account")
    monkeypatch.setenv("APPDATA", str(appdata))
    with mock.patch("trae_manager.trae._running_trae", return_value=[{"executable": str(exe), "profile": str(separate)}, {"executable": str(exe), "profile": str(default)}]), mock.patch("trae_manager.trae._registry_candidates", side_effect=AssertionError("cache is already valid")):
        result = TraeClient(exe).discover()
    assert result["executable_source"] == "cache"
    assert {row["path"] for row in result["profiles"]} == {str(default), str(separate)}
    assert len(result["profiles"]) == 2
