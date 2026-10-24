"""Sequential, cancellable translation of explicitly selected purpose summaries."""
from __future__ import annotations

from .translation import TranslationError, prepare_text


def _session(controller):
    with controller._mutex:
        if not controller.is_unlocked:
            raise TranslationError("账号库已锁定，请解锁后重新翻译。")
        doc = controller.vault.snapshot()
        return controller.vault._key, (doc.get("translation_settings") or {}).get("generation", "")


def translate_purposes(controller, entries, target, cancel, progress=None, *, interval=.3):
    """Translate only supplied one-line purposes; never read a file or claim all succeeded.

    Every successful item uses the regular encrypted backend cache. A cancelled
    in-flight request can finish, but no subsequent request starts. This routine
    runs in a worker; progress receives plain data, never a QWidget.
    """
    session = _session(controller)
    completed, seen = [], {}
    counts = {"translated": 0, "cached": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    stopped, invalidated = "", False
    for entry in entries:
        if cancel.is_set():
            stopped = "已取消"
            break
        try:
            current_session = _session(controller)
        except TranslationError:
            stopped, invalidated = "账号库已锁定", True
            break
        if current_session[0] is not session[0] or current_session[1] != session[1]:
            stopped = "账号库或翻译设置已变更"
            invalidated = True
            break
        record = {"key": entry["key"], "name": entry.get("name", "条目")}
        requested = False
        try:
            if entry.get("invalid") or not entry.get("text", "").strip():
                record["state"] = "skipped"
            else:
                text, source, language = prepare_text(entry["text"], target)
                if (text, language) in seen:
                    result = dict(seen[text, language])
                    record.update(state="unchanged" if source == language else "cached", result=result)
                else:
                    result = controller.translate_description(text, language)
                    current_session = _session(controller)
                    if current_session[0] is not session[0] or current_session[1] != session[1]:
                        raise TranslationError("账号库或翻译设置已变更，请重新翻译。")
                    seen[text, language] = result
                    state = "unchanged" if source == language else "cached" if result.get("cached") else "translated"
                    requested = state == "translated"
                    record.update(state=state, result=result)
        except Exception as error:
            message = str(error) if isinstance(error, TranslationError) else "此项翻译未完成，请稍后重试。"
            record.update(state="failed", message=message)
            if (not controller.is_unlocked or any(term in message for term in
                    ("额度", "上限", "翻译服务", "未授权", "无法连接", "已锁定", "已变更", "先同意"))):
                stopped = message
            if not controller.is_unlocked or any(term in message for term in ("已锁定", "已变更")):
                invalidated = True
        completed.append(record)
        counts[record["state"]] += 1
        if progress:
            progress(len(completed), len(entries), record["name"])
        if stopped:
            break
        if requested and interval > 0 and len(completed) < len(entries):
            cancel.wait(interval)
    if cancel.is_set() and len(completed) < len(entries):
        stopped = "已取消"
    return {"items": completed, "counts": counts, "total": len(entries),
            "remaining": len(entries) - len(completed), "stopped": stopped, "invalidated": invalidated}
