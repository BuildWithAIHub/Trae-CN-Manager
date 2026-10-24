"""Synthetic-only TRAE tests: no live account reads or HTTP calls."""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from trae_manager.trae import (
    API_ORIGIN, AUTH_KEY, CLAIM_PATH, DEVICE_KEY_PREFIX, STATUS_PATH, USAGE_PATH,
    TraeClient, TraeError, _derive_native, _PRIVATE_HEADER,
    decode_native_value, encode_native_value,
)


@pytest.fixture
def snapshot():
    return {"schema_version": 1, "device_id": "1234567890123456", "login": {"token": "SYNTHETIC_TOKEN_DO_NOT_USE", "refreshToken": "SYNTHETIC_REFRESH", "expiredAt": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "userId": "synthetic-user", "host": API_ORIGIN, "account": {"scope": "marscode", "username": "测试账号"}}}


def response(body, status=200):
    value = mock.Mock(status_code=status)
    value.json.return_value = body
    return value


def client_with(*responses):
    client = TraeClient()
    client.session = mock.Mock()
    client.session.post.side_effect = responses
    return client


def test_native_codec_roundtrip_and_corruption():
    encoded = encode_native_value('{"昵称":"synthetic"}')
    assert decode_native_value(encoded) == '{"昵称":"synthetic"}'
    raw = bytearray(base64.b64decode(encoded))
    raw[-17] ^= 1
    with pytest.raises(TraeError):
        decode_native_value(base64.b64encode(raw).decode())
    with pytest.raises(TraeError):
        decode_native_value("not a native record")


def test_native_private_container_compatibility():
    # Independent container assembly exercises the second native header, not an
    # encode/decode roundtrip using the same header selection.
    seed, text = bytes(range(32)), b'{"synthetic":true}'
    key, iv = _derive_native(seed, private=True)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(hashlib.sha512(text).digest() + text) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encoded = base64.b64encode(_PRIVATE_HEADER + seed + encryptor.update(padded) + encryptor.finalize()).decode()
    assert decode_native_value(encoded) == text.decode()


def test_profile_inspection_preserves_full_native_snapshot(tmp_path, snapshot):
    state_dir = tmp_path / "User" / "globalStorage"
    state_dir.mkdir(parents=True)
    login = snapshot["login"] | {"future_native_field": {"preserve": True}}
    pair = {"privateKeyPEM": "SYNTHETIC_PRIVATE", "publicKeyPEM": "SYNTHETIC_PUBLIC"}
    state_file = state_dir / "storage.json"
    state_file.write_text(json.dumps({AUTH_KEY: encode_native_value(json.dumps(login)), DEVICE_KEY_PREFIX + snapshot["device_id"]: encode_native_value(json.dumps(pair))}), encoding="utf-8")
    original = state_file.read_bytes()
    inspected = TraeClient().inspect_profile(tmp_path)
    assert inspected["login"] == login
    assert inspected["device_id"] == snapshot["device_id"]
    assert inspected["device_key_pairs"][snapshot["device_id"]] == pair
    assert inspected["profile_path"] == str(tmp_path.resolve())
    assert state_file.read_bytes() == original


def test_profile_ambiguous_device_is_not_fabricated(tmp_path, snapshot):
    folder = tmp_path / "User" / "globalStorage"
    folder.mkdir(parents=True)
    pair = encode_native_value(json.dumps({"privateKeyPEM": "synthetic", "publicKeyPEM": "synthetic"}))
    state = {AUTH_KEY: encode_native_value(json.dumps(snapshot["login"])), DEVICE_KEY_PREFIX + "111": pair, DEVICE_KEY_PREFIX + "222": pair}
    (folder / "storage.json").write_text(json.dumps(state), encoding="utf-8")
    result = TraeClient().inspect_profile(tmp_path)
    assert result["device_id"] == ""
    assert not result["device_verified"]


def test_aha_signing_pair_takes_precedence_over_stale_legacy_device(tmp_path, snapshot):
    folder = tmp_path / "User" / "globalStorage"
    folder.mkdir(parents=True)
    pair = encode_native_value(json.dumps({"privateKeyPEM": "synthetic", "publicKeyPEM": "synthetic"}))
    state = {AUTH_KEY: encode_native_value(json.dumps(snapshot["login"])), "device_id": "stale-device", "has_device_id_updated_to_aha": "true", DEVICE_KEY_PREFIX + snapshot["device_id"]: pair}
    (folder / "storage.json").write_text(json.dumps(state), encoding="utf-8")
    result = TraeClient().inspect_profile(tmp_path)
    assert result["device_id"] == snapshot["device_id"]
    assert result["device_id_source"] == "native-keypair"
    assert result["device_available"]
    assert not result["device_verified"]  # Offline discovery cannot prove server acceptance.


def test_migrated_aha_profile_does_not_use_unverified_legacy_id(tmp_path, snapshot):
    folder = tmp_path / "User" / "globalStorage"
    folder.mkdir(parents=True)
    state = {AUTH_KEY: encode_native_value(json.dumps(snapshot["login"])), "device_id": "stale-device", "has_device_id_updated_to_aha": True}
    (folder / "storage.json").write_text(json.dumps(state), encoding="utf-8")
    result = TraeClient().inspect_profile(tmp_path)
    assert not result["device_id"]
    assert result["device_id_source"] == "unknown"


def test_checkin_requires_verified_post_claim_status(snapshot):
    client = client_with(response({"enable": True, "checked_in": False}), response({"code": 0, "credits": 100}), response({"enable": True, "checked_in": True}))
    result = client.checkin(snapshot)
    assert result["state"] == "success"
    assert result["credits"] == 100
    assert [call.args[0] for call in client.session.post.call_args_list] == [API_ORIGIN + STATUS_PATH, API_ORIGIN + CLAIM_PATH, API_ORIGIN + STATUS_PATH]
    for call in client.session.post.call_args_list:
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["json"] == {"req_source": 1}
        assert call.kwargs["headers"]["x-device-id"] == snapshot["device_id"]
        assert call.kwargs["timeout"] == (5, 20)
    assert "SYNTHETIC" not in json.dumps(result)


def test_http_200_alone_is_not_success(snapshot):
    client = client_with(response({"enable": True, "checked_in": False}), response({"code": 0}), response({"enable": True, "checked_in": False}))
    assert client.checkin(snapshot)["state"] == "error"


@pytest.mark.parametrize("body", [{"code": "0"}, {"enable": 1, "checked_in": False}, {"enable": True}, {"code": True, "enable": True, "checked_in": False}])
def test_malformed_status_never_claims(snapshot, body):
    client = client_with(response(body))
    assert client.checkin(snapshot)["state"] == "error"
    assert client.session.post.call_count == 1


def test_already_signed_does_not_claim(snapshot):
    client = client_with(response({"enable": True, "checked_in": True}))
    assert client.checkin({"credentials": snapshot})["state"] == "already"
    assert client.session.post.call_count == 1


def test_business_failure_is_not_success_or_retried(snapshot):
    client = client_with(response({"enable": True, "checked_in": False}), response({"code": 9074, "message": "echo SYNTHETIC_TOKEN_DO_NOT_USE"}))
    result = client.checkin(snapshot)
    assert result["state"] == "unavailable"
    assert client.session.post.call_count == 2
    assert "SYNTHETIC" not in json.dumps(result)


@pytest.mark.parametrize("status,expected", [(401, "expired"), (403, "unavailable"), (302, "error"), (500, "error")])
def test_http_failures(snapshot, status, expected):
    client = client_with(response({}, status))
    assert client.checkin(snapshot)["state"] == expected


def test_expired_login_and_missing_device_do_not_call_service(snapshot):
    client = client_with()
    expired = snapshot | {"login": snapshot["login"] | {"expiredAt": "2000-01-01T00:00:00Z"}}
    assert client.checkin(expired)["state"] == "expired"
    assert client.checkin(snapshot | {"device_id": ""})["state"] == "unavailable"
    client.session.post.assert_not_called()


def test_network_error_does_not_expose_request_token(snapshot):
    client = client_with(requests.ConnectionError("SYNTHETIC_TOKEN_DO_NOT_USE"))
    result = client.checkin(snapshot)
    assert result["state"] == "error"
    assert "SYNTHETIC" not in json.dumps(result)


def pack(limit, used, kind=0):
    return {"entitlement_base_info": {"quota": {"credits_limit": limit}, "product_type": kind}, "usage": {"credits_amount": used}}


def test_usage_sums_packs_and_clamps_each_remaining(snapshot):
    client = client_with(response({"code": 0, "is_credits_billing": True, "user_entitlement_pack_list": [pack(100, 120), pack(300, 50, 1)]}))
    result = client.usage(snapshot)
    assert (result["limit"], result["used"], result["remaining"]) == (400, 170, 250)
    assert "Pro" in result["plan"]
    call = client.session.post.call_args
    assert call.args[0] == API_ORIGIN + USAGE_PATH
    assert call.kwargs["json"] == {"require_usage": True, "req_source": 1}


def test_unlimited_usage_is_json_serializable(snapshot):
    client = client_with(response({"user_entitlement_pack_list": [pack(-1, 42)]}))
    result = client.usage(snapshot)
    assert result["unlimited"]
    assert result["remaining"] is None
    assert result["used"] == 42
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("body", [{}, {"user_entitlement_pack_list": []}, {"user_entitlement_pack_list": [pack(100, "invalid")]}, {"user_entitlement_pack_list": [pack(100, float("nan"))]}])
def test_missing_usage_never_invents_zero_balance(snapshot, body):
    client = client_with(response(body))
    result = client.usage(snapshot)
    assert result["remaining"] is None
    assert result["limit"] is None


@pytest.mark.parametrize("usage", [{}, {"credits_amount": None}, None])
def test_native_unconsumed_credit_pack_defaults_usage_only(snapshot, usage):
    unconsumed = pack(200, 0)
    unconsumed["usage"] = usage
    client = client_with(response({"is_credits_billing": True, "user_entitlement_pack_list": [pack(400, 400), pack(200, 152.125), unconsumed]}))
    result = client.usage(snapshot)
    assert result["limit"] == 800
    assert result["used"] == 552.125
    assert result["remaining"] == 247.875
    assert "state" not in result


def test_noncredit_entitlements_do_not_make_a_missing_balance_zero(snapshot):
    client = client_with(response({"is_credits_billing": True, "user_entitlement_pack_list": [{"entitlement_base_info": {"quota": {"basic_usage_limit": 100}}, "usage": {}}]}))
    result = client.usage(snapshot)
    assert result["remaining"] is None
    assert result["limit"] is None


def test_discovery_uses_redirected_profile_roots_and_deduplicates(tmp_path, monkeypatch):
    appdata = tmp_path / "Roaming"
    appdata.mkdir()
    primary = appdata / "Trae CN"
    primary.mkdir()
    (primary / "User" / "globalStorage").mkdir(parents=True)
    (primary / "User" / "globalStorage" / "storage.json").write_text("synthetic-only", encoding="utf-8")
    (primary / "aha").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("USERPROFILE", str(home))
    executable = tmp_path / "app" / "Trae CN.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic")
    resources = executable.parent / "resources" / "app"
    resources.mkdir(parents=True)
    (resources / "product.json").write_text('{"appVersion":"3.4.3","packageType":"TRAE_CN","providerCode":"cn"}', encoding="utf-8")
    with mock.patch("trae_manager.trae._registry_candidates", return_value=([], [])), mock.patch("trae_manager.trae._running_trae", return_value=[]):
        result = TraeClient(executable).discover()
    assert result["version"] == "3.4.3"
    assert [(row["name"], row["path"]) for row in result["profiles"]] == [("Trae CN", str(primary.resolve()))]
    assert result["global_home"] == str(home / ".trae-cn")


def test_launch_initializes_empty_profile_and_never_kills(tmp_path):
    executable = tmp_path / "Trae CN.exe"
    executable.write_bytes(b"synthetic")
    profile = tmp_path / "new profile"
    sentinel = mock.Mock()
    with mock.patch("trae_manager.trae.subprocess.Popen", return_value=sentinel) as spawn:
        assert TraeClient(executable).launch(profile) is sentinel
    assert (profile / "User").is_dir()
    assert not (profile / "User" / "globalStorage" / "storage.json").exists()
    assert spawn.call_args.args[0] == [str(executable), "--user-data-dir", str(profile.resolve()), "--new-window"]
    assert spawn.call_args.kwargs["shell"] is False


@pytest.mark.parametrize("native_version", ["3.4.3", "3.4.7"])
def test_compatibility_checks_schema_markers_and_version(tmp_path, native_version):
    executable = tmp_path / "Trae CN.exe"
    executable.write_bytes(b"synthetic")
    resources = tmp_path / "resources" / "app"
    (resources / "out").mkdir(parents=True)
    product = {"appVersion": native_version, "packageType": "TRAE_CN", "providerCode": "cn", "bootConfig": {"ug": {"trae": {"normal": API_ORIGIN}}}}
    product_path = resources / "product.json"
    product_path.write_text(json.dumps(product), encoding="utf-8")
    marker_source = " ".join((STATUS_PATH, CLAIM_PATH, USAGE_PATH, "_requestCheckinCredits", "userStorage.js", "byteCrypto.js", "getDeviceKeyPair", "storeUserInfo"))
    (resources / "out" / "main.js").write_text(marker_source, encoding="utf-8")
    client = TraeClient(executable)
    assert client._compatibility()["state"] == "baseline"
    product["appVersion"] = "3.4.4"
    product_path.write_text(json.dumps(product), encoding="utf-8")
    assert client._compatibility()["state"] == "unverified"
    product["appVersion"] = native_version
    product_path.write_text(json.dumps(product), encoding="utf-8")
    (resources / "out" / "main.js").write_text("changed schema", encoding="utf-8")
    assert client._compatibility()["state"] == "unverified"


def test_restore_is_auth_only_and_preserves_existing_settings(tmp_path, snapshot):
    target = tmp_path / "manager-instance"
    client = TraeClient()
    pair = {"privateKeyPEM": "SYNTHETIC_PRIVATE", "publicKeyPEM": "SYNTHETIC_PUBLIC"}
    account = snapshot | {"device_key_pairs": {snapshot["device_id"]: pair}, "storage_keys": {"unrelated-user-setting": "MUST_NOT_IMPORT", "iCubeAuthInfo://usertag": encode_native_value(json.dumps({"synthetic-user": "cn", "other-user": "row"}))}}
    result = client.restore_account(account, target)
    assert result["state"] == "restored"
    state_path = target / "User" / "globalStorage" / "storage.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "unrelated-user-setting" not in state
    assert "SYNTHETIC_TOKEN_DO_NOT_USE" not in state_path.read_text(encoding="utf-8")
    assert json.loads(decode_native_value(state[AUTH_KEY])) == snapshot["login"]
    assert json.loads(decode_native_value(state["iCubeAuthInfo://usertag"])) == {"synthetic-user": "cn"}
    state["existing-setting"] = {"preserve": True}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    client.restore_account(account, target)
    restored = json.loads(state_path.read_text(encoding="utf-8"))
    assert restored["existing-setting"] == {"preserve": True}
    assert client.inspect_profile(target)["device_id"] == snapshot["device_id"]


def test_restore_refuses_default_and_unowned_nonempty_targets(tmp_path, monkeypatch, snapshot):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    default = appdata / "Trae CN"
    with pytest.raises(TraeError):
        TraeClient().restore_account(snapshot, default)
    assert not default.exists()
    unowned = tmp_path / "existing"
    unowned.mkdir()
    (unowned / "important.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(TraeError):
        TraeClient().restore_account(snapshot, unowned)
    assert (unowned / "important.txt").read_text(encoding="utf-8") == "preserve"


def test_restore_failure_retains_original_native_store(tmp_path, snapshot):
    client = TraeClient()
    target = tmp_path / "manager-profile"
    client.restore_account(snapshot, target)
    state_file = target / "User" / "globalStorage" / "storage.json"
    original = state_file.read_bytes()
    with mock.patch("trae_manager.trae.os.replace", side_effect=OSError("synthetic failure")):
        with pytest.raises(TraeError):
            client.restore_account(snapshot, target)
    assert state_file.read_bytes() == original
    assert not list(state_file.parent.glob(".manager-auth-*.tmp"))


def test_launch_account_opens_original_profile_without_restore(tmp_path, snapshot):
    executable = tmp_path / "Trae CN.exe"
    executable.write_bytes(b"synthetic")
    original_profile = tmp_path / "original-profile"
    original_profile.mkdir()
    (original_profile / "sentinel.txt").write_text("preserve", encoding="utf-8")
    client = TraeClient(executable)
    with mock.patch("trae_manager.trae.subprocess.Popen") as spawn:
        client.launch_account(snapshot | {"profile_path": str(original_profile)})
    assert spawn.call_args.args[0][2] == str(original_profile.resolve())
    assert (original_profile / "sentinel.txt").read_text(encoding="utf-8") == "preserve"
    assert not (original_profile / ".trae-cn-manager-profile").exists()


def test_launch_account_never_silently_opens_another_native_user(tmp_path, snapshot):
    executable = tmp_path / "Trae CN.exe"
    executable.write_bytes(b"synthetic")
    original_profile = tmp_path / "original-profile"
    native = original_profile / "User" / "globalStorage"
    native.mkdir(parents=True)
    second_login = snapshot["login"] | {"userId": "different-synthetic-user"}
    (native / "storage.json").write_text(json.dumps({AUTH_KEY: encode_native_value(json.dumps(second_login))}), encoding="utf-8")
    client = TraeClient(executable)
    with mock.patch("trae_manager.trae.subprocess.Popen") as spawn:
        with pytest.raises(TraeError):
            client.launch_account(snapshot | {"user_id": snapshot["login"]["userId"], "profile_path": str(original_profile)})
    spawn.assert_not_called()
    assert json.loads(decode_native_value(json.loads((native / "storage.json").read_text(encoding="utf-8"))[AUTH_KEY])) == second_login


def test_native_auth_error_metadata_maps_to_expired(snapshot):
    client = client_with(response({"ResponseMetadata": {"Error": {"Code": "20324", "Message": "SYNTHETIC_TOKEN_DO_NOT_USE"}}}))
    result = client.checkin(snapshot)
    assert result["state"] == "expired"
    assert "SYNTHETIC" not in json.dumps(result)


def test_restore_refuses_instance_started_by_this_manager(tmp_path, snapshot):
    executable = tmp_path / "Trae CN.exe"
    executable.write_bytes(b"synthetic")
    profile = tmp_path / "managed-profile"
    client = TraeClient(executable)
    process = mock.Mock()
    process.poll.return_value = None
    with mock.patch("trae_manager.trae.subprocess.Popen", return_value=process):
        client.launch(profile)
    with pytest.raises(TraeError):
        client.restore_account(snapshot, profile)
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
