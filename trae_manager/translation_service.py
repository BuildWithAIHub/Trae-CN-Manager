"""Encrypted translation preferences/cache, separate from account/UI summaries."""
from __future__ import annotations

import hashlib
import json
import uuid

from .translation import (MYMEMORY_ENDPOINT, TranslationError, normalize_endpoint,
                          prepare_text, request_translation)


def _settings(doc):
    saved = doc.get("translation_settings") or {}
    return {"provider": saved.get("provider", "mymemory"),
            "endpoint": saved.get("endpoint", MYMEMORY_ENDPOINT),
            "api_key": saved.get("api_key", ""),
            "consent": saved.get("consent") is True,
            "generation": saved.get("generation", "")}


def _public(config):
    return {"provider": config["provider"], "endpoint": config["endpoint"],
            "has_api_key": bool(config["api_key"]), "consent": config["consent"]}


class TranslationServiceMixin:
    """Requires Controller's vault, is_unlocked and reentrant _mutex."""

    def translation_settings(self):
        with self._mutex:
            return _public(_settings(self.vault.snapshot()))

    def configure_translation(self, provider, endpoint="", api_key=None, consent=False, clear_key=False):
        endpoint = normalize_endpoint(provider, endpoint)
        if not isinstance(consent, bool) or not isinstance(clear_key, bool):
            raise TranslationError("翻译设置格式不正确。")
        if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 8192
                                    or any(ord(c) < 32 for c in api_key)):
            raise TranslationError("翻译密钥格式不正确。")
        with self._mutex, self.vault.transaction() as doc:
            previous = _settings(doc)
            same_service = previous["provider"] == provider and previous["endpoint"] == endpoint
            key = previous["api_key"] if same_service and api_key is None else (api_key or "")
            if clear_key or provider == "mymemory":
                key = ""
            config = {"provider": provider, "endpoint": endpoint, "api_key": key,
                      "consent": consent, "generation": uuid.uuid4().hex}
            doc["translation_settings"] = config
            doc["translation_cache"] = {}
            return _public(config)

    def translate_description(self, text, target="zh-CN"):
        text, source, target = prepare_text(text, target)
        with self._mutex:
            doc = self.vault.snapshot()
            config = _settings(doc)
            if source == target:
                return {"text": text, "source": source, "target": target,
                        "provider": config["provider"], "cached": False, "local": True}
            if not config["consent"]:
                raise TranslationError("请先同意将当前用途文字发送至所选翻译服务。")
            # Retain only the object identity to detect a lock/unlock cycle while
            # a request is running; key bytes are neither copied nor inspected.
            session_identity = self.vault._key
            cache_key = hashlib.sha256(json.dumps([config["provider"], config["endpoint"],
                                                  text, source, target], ensure_ascii=False).encode("utf-8")).hexdigest()
            cache = doc.get("translation_cache") or {}
            cached = cache.get(cache_key)
            if isinstance(cached, str) and cached:
                return {"text": cached, "source": source, "target": target,
                        "provider": config["provider"], "cached": True}
        translated = request_translation(text, source, target, provider=config["provider"],
                                         endpoint=config["endpoint"], api_key=config["api_key"])
        with self._mutex:
            if not self.is_unlocked or self.vault._key is not session_identity:
                raise TranslationError("账号库已锁定，请解锁后重新翻译。")
            with self.vault.transaction() as current_doc:
                current = _settings(current_doc)
                if current != config or not current["consent"]:
                    raise TranslationError("翻译设置已变更，请重新翻译。")
                cache = current_doc.get("translation_cache")
                if not isinstance(cache, dict):
                    cache = {}
                cache.pop(cache_key, None)
                cache[cache_key] = translated
                while len(cache) > 200:
                    cache.pop(next(iter(cache)))
                current_doc["translation_cache"] = cache
        return {"text": translated, "source": source, "target": target,
                "provider": config["provider"], "cached": False}
