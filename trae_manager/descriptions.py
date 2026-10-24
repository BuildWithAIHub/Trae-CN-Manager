"""Short, extractive display text. The original Markdown is never rewritten."""
from __future__ import annotations

import re
import unicodedata


def _body(text: str) -> str:
    lines = str(text).splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is not None:
            lines = lines[end + 1:]
    return "\n".join(lines)


def _plain(text: str) -> str:
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"[`*_~]", "", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-+•]\s+|\d+[.)、]\s*|>\s*)", "", text)
    return " ".join(text.split()).strip(" :：;；-—")


def _first_prose(content: str) -> str:
    heading = ""
    fenced = None
    body = re.sub(r"<!--[\s\S]*?-->", "", _body(content))
    for line in body.splitlines():
        line = line.strip()
        fence = re.match(r"(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)[0]
            fenced = None if fenced == marker else marker
            continue
        if fenced or not line or line.startswith(("<!--", "|", "[!", "---")):
            continue
        if line.startswith("#"):
            heading = heading or _plain(line)
            continue
        value = _plain(line)
        if value:
            return value
    return heading


def summary_text(description: str = "", content: str = "", *, width: int = 84) -> str:
    """One sentence, at most about 42 Chinese or 84 Latin display columns."""
    candidate = description if isinstance(description, str) and description.strip() else _first_prose(content)
    text = _plain(candidate)
    text = re.sub(r"^(?:this skill (?:should be used when|is used (?:to|for)|helps you|provides)|use this skill (?:when|to|for)|you should use this skill when|description|用途|说明)\s*[:：-]?\s*", "", text, flags=re.I)
    if not text:
        return "未填写说明"
    # Keep decimals and abbreviations intact; prefer real sentence boundaries.
    parts = re.split(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+(?=[A-Z])", text, maxsplit=1)
    sentence = parts[0].strip()
    if not sentence:
        sentence = text
    budget = max(8, width)
    used, end = 0, 0
    for end, char in enumerate(sentence, 1):
        used += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used > budget:
            cut = sentence[:end-1].rstrip(" ,，、:：;；.-—")
            # Avoid cutting an English word when a reasonable boundary exists.
            if cut and not re.search(r"[\u3400-\u9fff]", cut) and " " in cut:
                cut = cut.rsplit(" ", 1)[0]
            return cut + "…"
    return sentence
