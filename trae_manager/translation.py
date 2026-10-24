"""Small opt-in translation requests. Text and credentials are never logged."""
from __future__ import annotations

import html
import json
import re
from urllib.parse import urlsplit, urlunsplit

import requests

MYMEMORY_ENDPOINT = "https://api.mymemory.translated.net/get"
MAX_TEXT_BYTES = 500
MAX_RESPONSE_BYTES = 1024 * 1024


class TranslationError(ValueError):
    """A safe, user-facing error without request URLs or response bodies."""


def normalize_endpoint(provider: str, endpoint: str = "") -> str:
    if provider == "mymemory":
        if not isinstance(endpoint, str):
            raise TranslationError("翻译服务地址格式不正确。")
        if endpoint and endpoint.strip().rstrip("/") != MYMEMORY_ENDPOINT:
            raise TranslationError("MyMemory 使用固定服务地址，请选择自定义服务以填写其他地址。")
        return MYMEMORY_ENDPOINT
    if provider != "libre":
        raise TranslationError("请选择 MyMemory 或 LibreTranslate。")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise TranslationError("请填写 LibreTranslate 服务地址。")
    endpoint = endpoint.strip()
    if "\\" in endpoint or any(ord(c) < 32 or c.isspace() for c in endpoint):
        raise TranslationError("翻译服务地址格式不正确。")
    try:
        parts = urlsplit(endpoint)
        hostname = parts.hostname
        _ = parts.port
        if (not hostname or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.scheme not in {"https", "http"}):
            raise ValueError()
        if parts.scheme == "http" and hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise TranslationError("请使用 HTTPS 地址；本机 localhost 服务可使用 HTTP。地址中不能包含账号、参数或锚点。") from None
    path = parts.path.rstrip("/")
    if not path.endswith("/translate"):
        path += "/translate"
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))


def prepare_text(text: str, target: str) -> tuple[str, str, str]:
    if target not in {"zh-CN", "en"}:
        raise TranslationError("仅支持中文与英文互译。")
    if not isinstance(text, str) or not text.strip():
        raise TranslationError("当前没有可翻译的用途说明。")
    if re.search(r"\r?\n\s*\r?\n", text):
        raise TranslationError("每次仅翻译一段用途说明。")
    text = " ".join(text.split())
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise TranslationError("用途说明包含无法识别的文字，请检查后重试。") from None
    if size > MAX_TEXT_BYTES:
        raise TranslationError("用途说明过长，每次最多翻译 500 字节，请先缩短说明。")
    source = "zh-CN" if re.search(r"[\u3400-\u9fff\uf900-\ufaff]", text) else "en"
    return text, source, target


def _read_json(response) -> dict:
    chunks = []
    size = 0
    try:
        for chunk in response.iter_content(chunk_size=8192):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise TranslationError("翻译服务响应过大，请稍后重试。")
            chunks.append(chunk)
        result = json.loads(b"".join(chunks).decode("utf-8-sig"))
    except TranslationError:
        raise
    except (ValueError, UnicodeError):
        raise TranslationError("翻译服务返回了无法识别的内容，请稍后重试。") from None
    if not isinstance(result, dict):
        raise TranslationError("翻译服务返回了无法识别的内容，请稍后重试。")
    return result


def request_translation(text: str, source: str, target: str, *, provider: str,
                        endpoint: str, api_key: str = "") -> str:
    """One request for an already-consented purpose, with strict response checks."""
    endpoint = normalize_endpoint(provider, endpoint)
    try:
        with requests.Session() as session:
            # Do not inherit account credentials, cookies, proxy auth or .netrc.
            session.trust_env = False
            options = {"timeout": (6, 12), "allow_redirects": False, "stream": True}
            if provider == "mymemory":
                response = session.get(endpoint, params={"q": text, "langpair": f"{source}|{target}", "mt": 1}, **options)
            else:
                body = {"q": text, "source": "zh" if source == "zh-CN" else "en",
                        "target": "zh" if target == "zh-CN" else "en", "format": "text"}
                if api_key:
                    body["api_key"] = api_key
                response = session.post(endpoint, json=body, **options)
            with response:
                if response.status_code == 429:
                    raise TranslationError("翻译次数已达服务上限，请稍后重试或在翻译设置中更换服务。")
                if response.status_code in {401, 403}:
                    raise TranslationError("翻译服务未授权，请检查翻译设置中的密钥和服务权限。")
                if response.status_code != 200:
                    raise TranslationError("翻译服务暂时不可用，请稍后重试。")
                data = _read_json(response)
        if provider == "mymemory":
            if data.get("quotaFinished") is True or str(data.get("responseStatus")) == "429":
                raise TranslationError("今日翻译额度已用完，请明日再试或更换翻译服务。")
            if str(data.get("responseStatus")) != "200":
                raise TranslationError("翻译服务未完成请求，请稍后重试。")
            value = data.get("responseData")
            translated = value.get("translatedText") if isinstance(value, dict) else None
        else:
            if data.get("error"):
                raise TranslationError("翻译服务未完成请求，请检查服务语言和密钥设置。")
            translated = data.get("translatedText")
        if not isinstance(translated, str) or not translated.strip() or len(translated) > 10000:
            raise TranslationError("翻译服务未返回有效译文，请稍后重试。")
        return html.unescape(translated.strip())
    except TranslationError:
        raise
    except Exception:
        # requests errors can include the complete query and key: do not retain
        # their text, their cause, or an arbitrary server's response in UI logs.
        raise TranslationError("无法连接翻译服务，请检查网络或翻译设置后重试。") from None
