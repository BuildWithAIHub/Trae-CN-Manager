"""Discovery tests use synthetic directory trees, never the machine's disks."""
import json
from pathlib import Path
import threading
from unittest import mock

import pytest

from trae_manager.locations import LocationManager


def fake_client(executable=None, profiles=None, global_home=None):
    client = mock.Mock()
    client.discover.return_value = {"executable": str(executable) if executable else None, "profiles": [{"path": str(path)} for path in profiles or []], "global_home": str(global_home) if global_home else "", "recent_projects": [], "compatibility": {"state": "test"}}
    client._compatibility.return_value = {"state": "test"}
    return client


def installation(root, version="3.4.3", official=True):
    root.mkdir(parents=True)
    exe = root / "Trae CN.exe"
    exe.write_bytes(b"synthetic-not-executable")
    product = root / "resources" / "app" / "product.json"
    product.parent.mkdir(parents=True)
    product.write_text(json.dumps({"packageType": "TRAE_CN" if official else "TRAE", "providerCode": "cn", "appVersion": version}), encoding="utf-8")
    return exe


def profile(root, native=True):
    storage = root / "User" / "globalStorage" / "storage.json"
    storage.parent.mkdir(parents=True)
    storage.write_text("DO NOT READ ACCOUNT DATA", encoding="utf-8")
    (root / "Cache").mkdir()
    if native:
        (root / "aha").mkdir()
    return root


def test_scan_requires_explicit_permission_without_touching_disks(tmp_path):
    manager = LocationManager(tmp_path / "state", fake_client())
    with mock.patch("trae_manager.locations.os.scandir") as listing:
        with pytest.raises(PermissionError):
            manager.scan([tmp_path])
        listing.assert_not_called()
    first = manager.inspect()
    assert first["scan_consent"] is None
    assert first["scan_decided"] is False
    manager.consent(False)
    assert manager.inspect()["scan_consent"] is False
    assert manager.inspect()["auto_scan_requested"] is False


def test_scan_recognizes_relocated_executable_profile_cache_and_home(tmp_path):
    disk = tmp_path / "disk"
    exe = installation(disk / "custom" / "app")
    data = profile(disk / "custom-trae-data")
    home = disk / "not-a-user-dir" / ".trae-cn"
    home.mkdir(parents=True)
    (home / "skills").mkdir()
    manager = LocationManager(tmp_path / "state", fake_client())
    manager.consent(True)
    result = manager.scan([disk])
    locations = {(r["kind"], r["path"]) for r in result["locations"]}
    assert ("executable", str(exe)) in locations
    assert ("profile", str(data)) in locations
    assert ("cache", str(data / "Cache")) in locations
    assert ("global_home", str(home)) in locations
    assert result["version"] == "3.4.3"
    assert result["scan"]["status"] == "complete"
    assert result["auto_scan_requested"] is False


def test_scan_reads_product_metadata_but_not_account_or_project_contents(tmp_path):
    disk = tmp_path / "disk"
    installation(disk / "app")
    data = profile(disk / "trae-data")
    (disk / "project").mkdir()
    (disk / "project" / "secret.txt").write_text("PRIVATE_PROJECT", encoding="utf-8")
    manager = LocationManager(tmp_path / "state", fake_client())
    manager.consent(True)
    actual_read = Path.read_text
    reads = []
    def read(path, *args, **kwargs):
        reads.append(path)
        assert path.name in ("product.json", "locations.json")
        return actual_read(path, *args, **kwargs)
    with mock.patch.object(Path, "read_text", read):
        result = manager.scan([disk])
    assert result["profiles"][0]["path"] == str(data)
    assert all(path.name != "storage.json" for path in reads)


def test_inspect_never_scans_and_requests_only_one_automatic_attempt(tmp_path):
    manager = LocationManager(tmp_path / "state", fake_client(), max_directories=1)
    manager.consent(True)
    with mock.patch.object(manager, "scan", side_effect=AssertionError("inspect must not scan")):
        assert manager.inspect()["auto_scan_requested"] is False
    disk = tmp_path / "disk"
    (disk / "nested").mkdir(parents=True)
    result = manager.scan([disk])
    assert result["scan"]["partial"] is True
    assert manager.inspect()["needs_scan"] is True
    assert manager.inspect()["auto_scan_requested"] is False


def test_path_notice_acknowledged_once_and_migration_requests_new_scan(tmp_path):
    old = installation(tmp_path / "old")
    client = fake_client(old)
    manager = LocationManager(tmp_path / "state", client)
    manager.consent(True)
    initial = manager.scan([old.parent])
    manager.acknowledge(initial["notice"]["id"])
    assert manager.inspect()["notice"] is None
    old.unlink()
    client.discover.return_value["executable"] = None
    moved = manager.inspect()
    assert moved["notice"]["kind"] == "missing"
    assert moved["auto_scan_requested"] is False
    new = installation(tmp_path / "new")
    found = manager.scan([new.parent])
    assert found["executable"] == str(new)
    manager.acknowledge(found["notice"]["id"])
    assert manager.inspect()["notice"] is None
    assert manager.inspect()["auto_scan_requested"] is False


def test_version_update_does_not_repeat_location_notice(tmp_path):
    exe = installation(tmp_path / "app")
    manager = LocationManager(tmp_path / "state", fake_client(exe))
    manager.consent(True)
    found = manager.scan([exe.parent])
    manager.acknowledge(found["notice"]["id"])
    metadata = exe.parent / "resources" / "app" / "product.json"
    value = json.loads(metadata.read_text(encoding="utf-8"))
    value["appVersion"] = "3.5.0"
    metadata.write_text(json.dumps(value), encoding="utf-8")
    result = manager.inspect()
    assert result["version"] == "3.5.0"
    assert result["notice"] is None


def test_manual_paths_validate_official_product_and_profile(tmp_path):
    good = installation(tmp_path / "good")
    other = installation(tmp_path / "other", official=False)
    data = profile(tmp_path / "data")
    home = tmp_path / "custom-skills-home"
    home.mkdir()
    (home / "skills").mkdir()
    manager = LocationManager(tmp_path / "state", fake_client())
    with pytest.raises(ValueError):
        manager.set_manual(executable=other)
    with pytest.raises(ValueError):
        manager.set_manual(profile=home)
    result = manager.set_manual(executable=good, profile=data, global_home=home, cache=data / "Cache")
    assert result["executable"] == str(good)
    assert result["global_home"] == str(home)
    assert result["profiles"][0]["path"] == str(data)


def test_scan_cancel_limit_and_progress_callback(tmp_path):
    disk = tmp_path / "disk"
    (disk / "nested").mkdir(parents=True)
    manager = LocationManager(tmp_path / "state", fake_client())
    manager.consent(True)
    event = threading.Event()
    event.set()
    result = manager.scan([disk], cancel=event, progress=lambda _: (_ for _ in ()).throw(RuntimeError("closed UI")))
    assert result["scan"]["status"] == "cancelled"
    assert result["scan"]["directories"] == 0
    assert result["notice"]["kind"] == "partial"
    manager.acknowledge(result["notice"]["id"])
    assert manager.inspect()["notice"] is None
    assert manager.inspect()["auto_scan_requested"] is False


def test_permission_denied_is_partial_and_visible_once(tmp_path):
    disk = tmp_path / "disk"
    disk.mkdir()
    denied = disk / "locked"
    denied.mkdir()
    manager = LocationManager(tmp_path / "state", fake_client())
    manager.consent(True)
    real_scan = __import__("os").scandir
    def scandir(path):
        if Path(path) == denied:
            raise PermissionError("synthetic permission denied")
        return real_scan(path)
    with mock.patch("trae_manager.locations.os.scandir", side_effect=scandir):
        result = manager.scan([disk])
    assert result["scan"]["status"] == "partial"
    assert result["scan"]["denied_count"] == 1
    assert result["notice"]["kind"] == "permission"
    manager.acknowledge(result["notice"]["id"])
    assert manager.inspect()["notice"] is None


def test_duplicate_roots_and_symlinks_do_not_recurse(tmp_path):
    disk = tmp_path / "disk"
    installation(disk / "app")
    manager = LocationManager(tmp_path / "state", fake_client())
    manager.consent(True)
    try:
        (disk / "loop").symlink_to(disk, target_is_directory=True)
    except OSError:
        pass  # Windows can restrict link creation; duplicated roots still test it.
    result = manager.scan([disk, disk, disk / "."])
    assert result["scan"]["directories"] == 2
    assert len([row for row in result["locations"] if row["kind"] == "executable"]) == 1


def test_other_ide_storage_layout_is_not_activated_as_trae_profile(tmp_path):
    disk = tmp_path / "disk"
    vscode = profile(disk / "Code", native=False)
    arbitrary = profile(disk / "moved-profile", native=False)
    marked = profile(disk / "manager-account-id")
    (marked / ".trae-cn-manager-profile").write_text("synthetic marker", encoding="utf-8")
    manager = LocationManager(tmp_path / "state", fake_client())
    manager.consent(True)
    result = manager.scan([disk])
    assert [row["path"] for row in result["profiles"]] == [str(marked)]
    assert set(result["scan"]["unverified_profiles"]) == {str(vscode), str(arbitrary)}
    assert not any(row["path"] == str(vscode / "Cache") for row in result["locations"])
    confirmed = manager.set_manual(profile=arbitrary)
    assert str(arbitrary) in [row["path"] for row in confirmed["profiles"]]


def test_previously_confirmed_arbitrary_profile_remains_detectable(tmp_path):
    data = profile(tmp_path / "relocated" / "random-name")
    manager = LocationManager(tmp_path / "state", fake_client(profiles=[data]))
    manager.consent(True)
    result = manager.scan([data.parent])
    assert [row["path"] for row in result["profiles"]] == [str(data)]


def test_stale_acknowledgement_cannot_dismiss_new_results(tmp_path):
    manager = LocationManager(tmp_path / "state", fake_client())
    first = manager.inspect()
    manager.consent(False)
    current = manager.inspect()
    assert first["notice"]["id"] != current["notice"]["id"]
    with pytest.raises(ValueError):
        manager.acknowledge(first["notice"]["id"])


def test_corrupt_state_is_conservative_not_implicit_consent(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "locations.json").write_text('{"schema_version": 1, "scan_consent": true, "notice_generation": null}', encoding="utf-8")
    manager = LocationManager(state, fake_client())
    # A parseable but malformed state must not crash discovery.
    assert manager.inspect()["scan_consent"] is None


def test_fast_complete_does_not_request_full_scan_even_before_consent(tmp_path):
    exe = installation(tmp_path / "app")
    data = profile(tmp_path / "data")
    home = tmp_path / "global"
    (home / "skills").mkdir(parents=True)
    manager = LocationManager(tmp_path / "state", fake_client(exe, [data], home))
    with mock.patch.object(manager, "scan", side_effect=AssertionError("no deep scan")):
        result = manager.inspect()
    assert result["scan_consent"] is None
    assert result["quick_complete"] is True
    assert result["quick_status"] == "found"
    assert result["auto_scan_requested"] is False
    assert result["needs_scan"] is False
    assert result["notice"]["kind"] == "found"
    assert all("source" in row and "confidence" in row and "selected" in row for row in result["locations"])


@pytest.mark.parametrize("profile_suffix", ["", "User", "User/globalStorage", "User/globalStorage/storage.json"])
def test_manual_inputs_accept_quoted_install_folder_and_profile_file(tmp_path, profile_suffix):
    exe = installation(tmp_path / "app")
    data = profile(tmp_path / "profile", native=False)
    manager = LocationManager(tmp_path / "state", fake_client())
    result = manager.set_manual(executable=f'"{exe.parent}"', profile=f'"{data / profile_suffix}"')
    assert Path(result["executable"]) == exe
    assert [row["path"] for row in result["profiles"]] == [str(data)]
    assert result["profiles"][0]["source"] == "manual"


def test_cached_confirmed_active_paths_skip_all_discovery_and_remain_fast(tmp_path):
    from time import monotonic
    exe = installation(tmp_path / "app")
    data = profile(tmp_path / "data", native=False)
    home = tmp_path / "global"
    (home / "skills").mkdir(parents=True)
    client = fake_client()
    manager = LocationManager(tmp_path / "state", client)
    result = manager.set_manual(executable=exe, profile=data, global_home=home)
    manager.acknowledge(result["notice"]["id"])
    client.discover.side_effect = AssertionError("confirmed cache must skip discover")
    start = monotonic()
    result = manager.inspect()
    assert monotonic() - start < 0.2
    assert result["notice"] is None
    assert result["quick_complete"] is True
    assert result["discovery_ms"] < 200


def test_old_full_scan_cache_does_not_activate_test_profiles_or_backups(tmp_path):
    exe = installation(tmp_path / "live")
    data = profile(tmp_path / "live-data")
    backup = installation(tmp_path / ".updates" / "old")
    old_profile = profile(tmp_path / "pytest-fake" / "synthetic")
    state = tmp_path / "state"
    state.mkdir()
    (state / "locations.json").write_text(json.dumps({"schema_version": 1, "selected": {"executable": str(backup), "profiles": [str(old_profile)]}, "cached": [{"kind": "executable", "path": str(backup)}, {"kind": "profile", "path": str(old_profile)}], "scan_consent": True, "last_scan": {"status": "partial"}}), encoding="utf-8")
    manager = LocationManager(state, fake_client(exe, [data]))
    result = manager.inspect()
    assert result["executable"] == str(exe)
    assert [row["path"] for row in result["profiles"]] == [str(data)]
    assert not any(row["path"] == str(backup) for row in result["locations"])
    assert result["auto_scan_requested"] is False


def test_unrelated_old_cache_disappearing_does_not_prompt_again(tmp_path):
    exe = installation(tmp_path / "app")
    data = profile(tmp_path / "data")
    home = tmp_path / "global"
    (home / "skills").mkdir(parents=True)
    manager = LocationManager(tmp_path / "state", fake_client())
    result = manager.set_manual(executable=exe, profile=data, global_home=home)
    manager.acknowledge(result["notice"]["id"])
    (data / "Cache").rmdir()
    result = manager.inspect()
    assert result["notice"] is None
    assert result["auto_scan_requested"] is False


def test_name_containing_trae_without_native_evidence_is_only_candidate(tmp_path):
    disk = tmp_path / "disk"
    data = profile(disk / "Trae-fake", native=False)
    manager = LocationManager(tmp_path / "state", fake_client())
    manager.consent(True)
    result = manager.scan([disk])
    assert result["profiles"] == []
    assert result["scan"]["unverified_profiles"] == [str(data)]
