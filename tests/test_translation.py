"""Translation is manual, scoped, revocable and private at rest."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import requests

from trae_manager.security import Vault
from trae_manager.translation import (MYMEMORY_ENDPOINT, TranslationError,
                                      normalize_endpoint, prepare_text, request_translation)
from trae_manager.translation_service import TranslationServiceMixin


class TestController(TranslationServiceMixin):
    __test__ = False

    def __init__(self, root):
        self.vault = Vault(root)
        self.vault.create("translation-test-password", {"settings": {}, "logs": []})
        self._mutex = threading.RLock()

    @property
    def is_unlocked(self):
        return self.vault.unlocked


class Response:
    def __init__(self, data=None, status=200, raw=None):
        self.status_code = status
        self.raw = raw if raw is not None else json.dumps(data).encode()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw), chunk_size):
            yield self.raw[start:start + chunk_size]


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, endpoint, **kwargs):
        self.calls.append(("GET", endpoint, kwargs))
        return self.response

    def post(self, endpoint, **kwargs):
        self.calls.append(("POST", endpoint, kwargs))
        return self.response


class TranslationProtocolTests(unittest.TestCase):
    def test_mymemory_sends_only_selected_text_and_direction(self):
        response = Response({"responseStatus": 200, "quotaFinished": False,
                             "responseData": {"translatedText": "检查 &amp; 报告"}})
        session = Session(response)
        with patch("trae_manager.translation.requests.Session", return_value=session):
            result = request_translation("Check interfaces.", "en", "zh-CN", provider="mymemory",
                                         endpoint=MYMEMORY_ENDPOINT)
        self.assertEqual(result, "检查 & 报告")
        self.assertFalse(session.trust_env)
        method, endpoint, options = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(endpoint, MYMEMORY_ENDPOINT)
        self.assertEqual(options["params"], {"q": "Check interfaces.", "langpair": "en|zh-CN", "mt": 1})
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["stream"])
        self.assertEqual(options["timeout"], (6, 12))
        self.assertNotIn("headers", options)
        self.assertNotIn("auth", options)
        self.assertTrue(response.closed)

    def test_libre_uses_post_body_for_key_and_reverse_direction(self):
        session = Session(Response({"translatedText": "Generate reports."}))
        with patch("trae_manager.translation.requests.Session", return_value=session):
            self.assertEqual(request_translation("生成报告。", "zh-CN", "en", provider="libre",
                                                endpoint="http://127.0.0.1:5000", api_key="private-key"),
                             "Generate reports.")
        method, endpoint, options = session.calls[0]
        self.assertEqual((method, endpoint), ("POST", "http://127.0.0.1:5000/translate"))
        self.assertEqual(options["json"], {"q": "生成报告。", "source": "zh", "target": "en",
                                          "format": "text", "api_key": "private-key"})
        self.assertNotIn("params", options)

    def test_unsafe_or_ambiguous_endpoints_are_rejected(self):
        for endpoint in ["http://example.com/", "https://name:password@example.com/",
                         "https://example.com/?q=secret", "https://example.com/#secret",
                         "file:///tmp/a", "https://example.com:bad/", "https://example.com\n/",
                         "https://example.com\\@other.example/"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(TranslationError):
                normalize_endpoint("libre", endpoint)
        for endpoint in ["http://localhost:5000", "http://[::1]:5000", "https://example.com/api/"]:
            self.assertTrue(normalize_endpoint("libre", endpoint).endswith("/translate"))
        with self.assertRaises(TranslationError):
            normalize_endpoint("mymemory", "https://evil.example/")

    def test_limits_apply_to_utf8_bytes_and_single_paragraph(self):
        self.assertEqual(prepare_text("检查接口。", "en"), ("检查接口。", "zh-CN", "en"))
        for text, target in [("中" * 167, "en"), ("", "en"), ("first\n\nsecond", "zh-CN"), ("ok", "fr")]:
            with self.subTest(text=text[:15], target=target), self.assertRaises(TranslationError):
                prepare_text(text, target)

    def test_http_api_quota_and_invalid_body_are_not_translations(self):
        cases = [Response(status=429), Response(status=302), Response(status=403),
                 Response({"responseStatus": 403, "responseData": {"translatedText": "PRIVATE_SERVER_BODY"}}),
                 Response({"responseStatus": 200, "quotaFinished": True, "responseData": {"translatedText": "quota"}}),
                 Response({"responseStatus": 200, "responseData": {"translatedText": ""}}),
                 Response(raw=b"PRIVATE_SERVER_BODY"), Response(raw=b"x" * (1024 * 1024 + 1))]
        for response in cases:
            with self.subTest(response=response.status_code):
                with patch("trae_manager.translation.requests.Session", return_value=Session(response)):
                    with self.assertRaises(TranslationError) as captured:
                        request_translation("selected purpose", "en", "zh-CN", provider="mymemory",
                                            endpoint=MYMEMORY_ENDPOINT)
                self.assertNotIn("PRIVATE_SERVER_BODY", str(captured.exception))
                self.assertTrue(response.closed)

    def test_network_errors_do_not_reveal_query_or_secret(self):
        session = Session(None)
        session.get = lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.RequestException("https://example.invalid/?q=PRIVATE_TEXT&key=PRIVATE_KEY"))
        with patch("trae_manager.translation.requests.Session", return_value=session):
            with self.assertRaises(TranslationError) as captured:
                request_translation("PRIVATE_TEXT", "en", "zh-CN", provider="mymemory", endpoint=MYMEMORY_ENDPOINT)
        self.assertNotIn("PRIVATE", str(captured.exception))
        self.assertTrue(captured.exception.__suppress_context__)


class TranslationVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.controller = TestController(self.root)

    def tearDown(self):
        self.controller.vault.lock()
        self.temp.cleanup()

    def configure(self, **kwargs):
        return self.controller.configure_translation("mymemory", consent=True, **kwargs)

    def test_no_consent_no_request_and_same_language_is_local(self):
        self.assertEqual(self.controller.translation_settings(), {"provider": "mymemory",
                         "endpoint": MYMEMORY_ENDPOINT, "has_api_key": False, "consent": False})
        with patch("trae_manager.translation_service.request_translation") as request:
            with self.assertRaises(TranslationError):
                self.controller.translate_description("Generate reports.", "zh-CN")
            self.assertEqual(self.controller.translate_description("生成报告。", "zh-CN")["text"], "生成报告。")
            request.assert_not_called()

    def test_cache_is_encrypted_bounded_and_direction_specific(self):
        self.configure()
        with patch("trae_manager.translation_service.request_translation", return_value="检查接口。") as request:
            first = self.controller.translate_description("UNIQUE_SOURCE_PURPOSE", "zh-CN")
            second = self.controller.translate_description("UNIQUE_SOURCE_PURPOSE", "zh-CN")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        request.assert_called_once()
        document = self.controller.vault.snapshot()
        self.assertEqual(len(document["translation_cache"]), 1)
        self.assertNotIn("UNIQUE_SOURCE_PURPOSE", json.dumps(document["translation_cache"]))
        self.assertEqual(document["settings"], {})
        with self.controller.vault.transaction() as doc:
            doc["translation_cache"] = {str(i): "cached" for i in range(200)}
        with patch("trae_manager.translation_service.request_translation", return_value="新译文"):
            self.controller.translate_description("A new purpose", "zh-CN")
        cache = self.controller.vault.snapshot()["translation_cache"]
        self.assertEqual(len(cache), 200)
        self.assertNotIn("0", cache)
        for path in self.root.iterdir():
            raw = path.read_bytes()
            self.assertNotIn(b"UNIQUE_SOURCE_PURPOSE", raw)
            self.assertNotIn("检查接口。".encode(), raw)

    def test_keys_stay_encrypted_and_cannot_follow_different_endpoint(self):
        public = self.controller.configure_translation("libre", "https://first.example", "PRIVATE_TRANSLATOR_KEY", True)
        self.assertTrue(public["has_api_key"])
        self.assertNotIn("PRIVATE_TRANSLATOR_KEY", str(public))
        self.assertNotIn("api_key", self.controller.translation_settings())
        for path in self.root.iterdir():
            self.assertNotIn(b"PRIVATE_TRANSLATOR_KEY", path.read_bytes())
        preserved = self.controller.configure_translation("libre", "https://first.example", consent=True)
        self.assertTrue(preserved["has_api_key"])
        changed = self.controller.configure_translation("libre", "https://second.example")
        self.assertFalse(changed["has_api_key"])
        self.assertFalse(changed["consent"])
        self.assertEqual(self.controller.vault.snapshot()["translation_cache"], {})

    def test_revoking_consent_clears_cache_and_blocks_request(self):
        self.configure()
        with patch("trae_manager.translation_service.request_translation", return_value="报告"):
            self.controller.translate_description("Reports", "zh-CN")
        self.controller.configure_translation("mymemory", consent=False)
        self.assertEqual(self.controller.vault.snapshot()["translation_cache"], {})
        with patch("trae_manager.translation_service.request_translation") as request:
            with self.assertRaises(TranslationError):
                self.controller.translate_description("Reports", "zh-CN")
            request.assert_not_called()

    def test_changes_during_network_do_not_cache_or_return_result(self):
        self.configure()
        def revoke(*args, **kwargs):
            self.controller.configure_translation("mymemory", consent=False)
            return "must not be returned"
        with patch("trae_manager.translation_service.request_translation", side_effect=revoke):
            with self.assertRaises(TranslationError):
                self.controller.translate_description("Reports", "zh-CN")
        self.assertEqual(self.controller.vault.snapshot()["translation_cache"], {})

    def test_lock_unlock_cycle_during_network_invalidates_request(self):
        self.configure()
        def lock_unlock(*args, **kwargs):
            self.controller.vault.lock()
            self.controller.vault.unlock("translation-test-password")
            return "must not be returned"
        with patch("trae_manager.translation_service.request_translation", side_effect=lock_unlock):
            with self.assertRaises(TranslationError):
                self.controller.translate_description("Reports", "zh-CN")
        self.assertEqual(self.controller.vault.snapshot()["translation_cache"], {})

    def test_locked_settings_cache_and_config_are_inaccessible(self):
        self.controller.vault.lock()
        for operation in [self.controller.translation_settings, self.configure,
                          lambda: self.controller.translate_description("Reports", "zh-CN")]:
            with self.assertRaises(ValueError):
                operation()

    def test_network_does_not_hold_controller_mutex(self):
        self.configure()
        acquired = []
        def inspect_mutex(*args, **kwargs):
            def another_operation():
                ok = self.controller._mutex.acquire(timeout=1)
                acquired.append(ok)
                if ok:
                    self.controller._mutex.release()
            thread = threading.Thread(target=another_operation)
            thread.start()
            thread.join(2)
            return "检查接口。"
        with patch("trae_manager.translation_service.request_translation", side_effect=inspect_mutex):
            self.controller.translate_description("Check interfaces.", "zh-CN")
        self.assertEqual(acquired, [True])
