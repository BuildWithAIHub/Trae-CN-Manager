"""Batch translation keeps scope, opt-in, cancellation and partial results explicit."""
import copy
import threading
from unittest.mock import patch

import pytest

from trae_manager.translation import TranslationError
from trae_manager.translation_batch import translate_purposes
from test_translation import TestController


def entries(*texts):
    return [{"key": f"item-{i}", "name": f"Item {i}", "text": text} for i, text in enumerate(texts)]


def test_batch_reuses_duplicates_local_language_and_encrypted_cache(tmp_path):
    controller = TestController(tmp_path)
    controller.configure_translation("mymemory", consent=True)
    values = entries("Check interfaces.", "Check interfaces.", "整理文档。")
    with patch("trae_manager.translation_service.request_translation", return_value="检查接口。") as request:
        result = translate_purposes(controller, values, "zh-CN", threading.Event(), interval=0)
        again = translate_purposes(controller, values, "zh-CN", threading.Event(), interval=0)
    assert request.call_count == 1
    assert result["counts"] == {"translated": 1, "cached": 1, "unchanged": 1, "skipped": 0, "failed": 0}
    assert again["counts"]["cached"] == 2
    assert result["remaining"] == 0 and result["stopped"] == ""
    assert b"Check interfaces" not in controller.vault.path.read_bytes()
    controller.vault.lock()


def test_batch_without_consent_never_uses_network(tmp_path):
    controller = TestController(tmp_path)
    with patch("trae_manager.translation_service.request_translation") as request:
        result = translate_purposes(controller, entries("Check interfaces.", "Write docs."), "zh-CN", threading.Event(), interval=0)
    request.assert_not_called()
    assert result["counts"]["failed"] == 1 and result["remaining"] == 1
    assert "同意" in result["stopped"]
    controller.vault.lock()


def test_cancel_before_start_or_between_items_prevents_next_request(tmp_path):
    controller = TestController(tmp_path)
    controller.configure_translation("mymemory", consent=True)
    cancel = threading.Event()
    cancel.set()
    values = entries("Check interfaces.", "Write docs.")
    with patch("trae_manager.translation_service.request_translation", return_value="检查接口。") as request:
        before = translate_purposes(controller, values, "zh-CN", cancel, interval=0)
        assert before["remaining"] == 2
        request.assert_not_called()
        cancel.clear()
        result = translate_purposes(controller, values, "zh-CN", cancel,
                                    lambda *_: cancel.set(), interval=0)
    assert request.call_count == 1
    assert result["counts"]["translated"] == 1 and result["remaining"] == 1
    assert result["stopped"] == "已取消"
    controller.vault.lock()


def test_partial_failure_and_quota_are_reported_without_claiming_all_done(tmp_path):
    controller = TestController(tmp_path)
    controller.configure_translation("mymemory", consent=True)
    values = entries("Check interfaces.", "Write docs.", "Generate a report.")
    source = copy.deepcopy(values)
    with patch("trae_manager.translation_service.request_translation", side_effect=["检查接口。", TranslationError("今日翻译额度已用完。")]) as request:
        result = translate_purposes(controller, values, "zh-CN", threading.Event(), interval=0)
    assert request.call_count == 2
    assert result["counts"]["translated"] == 1 and result["counts"]["failed"] == 1
    assert result["remaining"] == 1 and "额度" in result["stopped"]
    assert values == source
    assert result["items"][0]["result"]["text"] == "检查接口。"
    controller.vault.lock()


def test_settings_change_between_requests_invalidates_batch(tmp_path):
    controller = TestController(tmp_path)
    controller.configure_translation("mymemory", consent=True)
    with patch("trae_manager.translation_service.request_translation", return_value="检查接口。") as request:
        result = translate_purposes(controller, entries("Check interfaces.", "Write docs."), "zh-CN", threading.Event(),
                                    lambda *_: controller.configure_translation("mymemory", consent=False), interval=0)
    assert request.call_count == 1 and result["invalidated"]
    assert result["remaining"] == 1
    controller.vault.lock()


def test_lock_unlock_between_requests_cannot_continue_old_batch(tmp_path):
    controller = TestController(tmp_path)
    controller.configure_translation("mymemory", consent=True)

    def restart(*_):
        controller.vault.lock()
        controller.vault.unlock("translation-test-password")

    with patch("trae_manager.translation_service.request_translation", return_value="检查接口。") as request:
        result = translate_purposes(controller, entries("Check interfaces.", "Write docs."), "zh-CN", threading.Event(), restart, interval=0)
    assert request.call_count == 1 and result["invalidated"]
    controller.vault.lock()


def test_invalid_or_empty_purposes_do_not_get_uploaded(tmp_path):
    controller = TestController(tmp_path)
    values = entries("", "整理文档。")
    values[0]["invalid"] = True
    with patch("trae_manager.translation_service.request_translation") as request:
        result = translate_purposes(controller, values, "zh-CN", threading.Event(), interval=0)
    request.assert_not_called()
    assert result["counts"]["skipped"] == result["counts"]["unchanged"] == 1
    controller.vault.lock()


@pytest.mark.parametrize("message", ["翻译服务暂时不可用，请稍后重试。", "翻译服务未完成请求，请稍后重试。"])
def test_service_outage_stops_batch_without_repeated_requests(tmp_path, message):
    controller = TestController(tmp_path)
    controller.configure_translation("mymemory", consent=True)
    with patch("trae_manager.translation_service.request_translation", side_effect=TranslationError(message)) as request:
        result = translate_purposes(controller, entries("Check interfaces.", "Write docs."), "zh-CN", threading.Event(), interval=0)
    assert request.call_count == 1
    assert result["remaining"] == 1 and result["counts"]["failed"] == 1
    assert result["stopped"] == message
    controller.vault.lock()
