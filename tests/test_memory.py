"""Memory tests use synthetic Markdown only; no live memories are read."""
import copy
from pathlib import Path
from unittest import mock

import pytest

from trae_manager.memory import MemoryManager


def write(home, relative, content):
    path = home / "memory" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def snapshot(*files):
    return {"scope": "local-shared", "captured_at": "2026-10-24T00:00:00+00:00", "files": [{"relative": path, "content": content} for path, content in files]}


def test_memory_snapshot_contains_only_native_user_and_project_markdown(tmp_path):
    write(tmp_path, "user_profile.md", "# 偏好\n\n喜欢中文。\n")
    write(tmp_path, "projects/encoded-path/project_memory.md", "# 项目约定\n\n用 pytest。\n")
    write(tmp_path, "chat_history.md", "PRIVATE CHAT MUST NOT BE EXPORTED")
    write(tmp_path, "projects/encoded-path/token.json", "PRIVATE TOKEN")
    manager = MemoryManager(tmp_path)
    rows = manager.list_memories()
    assert {r["relative"] for r in rows} == {"user_profile.md", "projects/encoded-path/project_memory.md"}
    result = manager.snapshot()
    assert result["scope"] == "local-shared"
    assert result["captured_at"]
    assert all(set(row) == {"relative", "content"} for row in result["files"])


def test_merge_preserves_target_frontmatter_and_sections_without_deleting(tmp_path):
    path = write(tmp_path, "user_profile.md", "---\nversion: native-target\nupdated_at: unchanged\n---\n# 偏好\n\n使用中文。\n\n# 保留\n\n不能删除。\n")
    source = snapshot(("user_profile.md", "---\nversion: other-account\n---\n# 偏好\n\n使用中文。\n\n喜欢短句。\n\n# 新章节\n\n项目用 Python。\n"))
    manager = MemoryManager(tmp_path)
    preview = manager.preview_merge(source)
    before = path.read_text(encoding="utf-8")
    assert "喜欢短句" not in before
    assert preview["changed_count"] == 1
    assert preview["duplicates"] == 1
    result = manager.merge_snapshot(source)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\nversion: native-target\nupdated_at: unchanged\n---\n")
    assert "other-account" not in text
    assert text.count("使用中文。") == 1
    assert "不能删除。" in text
    assert text.index("喜欢短句。") < text.index("# 保留")
    assert "项目用 Python。" in text
    assert result["written"] == ["user_profile.md"]
    assert result["message"]


def test_repeated_merge_is_idempotent_and_does_not_rewrite(tmp_path):
    path = write(tmp_path, "user_profile.md", "# Prefs\n\nKeep me.\n")
    source = snapshot(("user_profile.md", "# Prefs\n\nKeep me.\n\nNew memory.\n"))
    manager = MemoryManager(tmp_path)
    manager.merge_snapshot(source)
    content = path.read_bytes()
    with mock.patch("trae_manager.memory.atomic_write") as save:
        result = manager.merge_snapshot(source)
    save.assert_not_called()
    assert result["changed_count"] == 0
    assert result["written"] == []
    assert path.read_bytes() == content


def test_fenced_code_headings_remain_code_and_frontmatter_is_kept(tmp_path):
    path = write(tmp_path, "user_profile.md", "---\nversion: 1\n...\n# Notes\n\nA.\n")
    source = snapshot(("user_profile.md", "# Notes\n\n```python\n# not a heading\n\nprint('hello')\n```\n"))
    MemoryManager(tmp_path).merge_snapshot(source)
    text = path.read_text(encoding="utf-8")
    assert "```python\n# not a heading\n\nprint('hello')\n```" in text
    assert text.startswith("---\nversion: 1\n...\n")


def test_missing_native_folder_is_created_only_when_merging(tmp_path):
    manager = MemoryManager(tmp_path)
    source = snapshot(("projects/encoded/project_memory.md", "---\nversion: 1\n---\n# Project\n\nFact.\n"))
    assert manager.snapshot()["files"] == []
    assert manager.preview_merge(source)["add_count"] == 1
    assert not (tmp_path / "memory").exists()
    result = manager.merge_snapshot(source)
    assert result["add_count"] == 1
    assert manager.snapshot()["files"] == source["files"]


@pytest.mark.parametrize("relative", ["../user_profile.md", "/user_profile.md", "memory/user_profile.md", "projects/../user_profile.md", "projects/x/y/project_memory.md", "projects/x/token.json", "projects\\x\\project_memory.md", "projects/a:b/project_memory.md", "projects/x./project_memory.md", "projects//x/project_memory.md", "./user_profile.md", "projects/CON/project_memory.md", "projects/NUL.txt/project_memory.md", "projects/a*/project_memory.md"])
def test_untrusted_paths_rejected_without_writing(tmp_path, relative):
    manager = MemoryManager(tmp_path)
    with pytest.raises(ValueError):
        manager.merge_snapshot(snapshot((relative, "untrusted")))
    assert not (tmp_path / "memory").exists()


@pytest.mark.parametrize("source", [{}, {"scope": "account-private", "files": []}, {"scope": "local-shared", "files": "bad"}, {"scope": "local-shared", "files": [{"relative": "user_profile.md", "content": None}]}, {"scope": "local-shared", "files": [{"relative": "user_profile.md", "content": "null\x00bytes"}]}])
def test_malformed_snapshot_is_rejected(tmp_path, source):
    with pytest.raises(ValueError):
        MemoryManager(tmp_path).merge_snapshot(source)


def test_duplicate_path_and_file_size_limits(tmp_path):
    manager = MemoryManager(tmp_path)
    with pytest.raises(ValueError):
        manager.merge_snapshot(snapshot(("user_profile.md", "one"), ("user_profile.md", "two")))
    with mock.patch("trae_manager.memory.MAX_FILE_BYTES", 4):
        with pytest.raises(ValueError):
            manager.preview_merge(snapshot(("user_profile.md", "too long")))
    with mock.patch("trae_manager.memory.MAX_TOTAL_BYTES", 8):
        with pytest.raises(ValueError):
            manager.preview_merge(snapshot(("user_profile.md", "12345"), ("projects/p/project_memory.md", "12345")))


def test_existing_non_utf8_or_oversized_memory_is_not_modified(tmp_path):
    path = write(tmp_path, "user_profile.md", "original")
    path.write_bytes(b"\xff\x00")
    with pytest.raises(ValueError):
        MemoryManager(tmp_path).snapshot()
    assert path.read_bytes() == b"\xff\x00"
    path.write_bytes(b"123456")
    with mock.patch("trae_manager.memory.MAX_FILE_BYTES", 5):
        with pytest.raises(ValueError):
            MemoryManager(tmp_path).snapshot()


def test_backup_callback_runs_before_write_and_stops_on_concurrent_change(tmp_path):
    path = write(tmp_path, "user_profile.md", "# Prefs\n\nOriginal.\n")
    source = snapshot(("user_profile.md", "# Prefs\n\nAdded.\n"))
    observed = []
    def backup(changed_path):
        observed.append(changed_path.read_text(encoding="utf-8"))
        changed_path.write_text("Changed by native TRAE during preview.", encoding="utf-8")
    manager = MemoryManager(tmp_path, backup_callback=backup)
    with pytest.raises(ValueError, match="其他程序"):
        manager.merge_snapshot(source)
    assert observed == ["# Prefs\n\nOriginal.\n"]
    assert path.read_text(encoding="utf-8") == "Changed by native TRAE during preview."


def test_backup_failure_leaves_all_files_intact(tmp_path):
    path = write(tmp_path, "user_profile.md", "old")
    def backup(_):
        raise OSError("synthetic backup failure")
    with pytest.raises(OSError):
        MemoryManager(tmp_path, backup).merge_snapshot(snapshot(("user_profile.md", "new")))
    assert path.read_text(encoding="utf-8") == "old"


def test_merge_never_deletes_target_only_project_memory(tmp_path):
    path = write(tmp_path, "projects/target-only/project_memory.md", "Keep this.")
    manager = MemoryManager(tmp_path)
    manager.merge_snapshot(snapshot(("user_profile.md", "New user memory.")))
    assert path.read_text(encoding="utf-8") == "Keep this."
    manager.merge_snapshot(snapshot())
    assert path.exists()


def test_restore_exactly_recovers_snapshot_and_removes_later_memory_only(tmp_path):
    path = write(tmp_path, "user_profile.md", "Original memory.")
    manager = MemoryManager(tmp_path)
    original = manager.snapshot()
    manager.merge_snapshot(snapshot(("user_profile.md", "Added memory."), ("projects/new/project_memory.md", "New project memory.")))
    unrelated = write(tmp_path, "chat_history.md", "KEEP CHAT")
    result = manager.restore_snapshot(original)
    assert path.read_text(encoding="utf-8") == "Original memory."
    assert result["removed"] == ["projects/new/project_memory.md"]
    assert unrelated.read_text(encoding="utf-8") == "KEEP CHAT"
    assert manager.snapshot()["files"] == original["files"]


def test_restore_case_only_spelling_does_not_delete_same_windows_memory(tmp_path):
    path = write(tmp_path, "projects/Encoded-Project/project_memory.md", "current")
    manager = MemoryManager(tmp_path)
    result = manager.restore_snapshot(snapshot(("projects/encoded-project/project_memory.md", "restored")))
    assert path.read_text(encoding="utf-8") == "restored"
    assert result["removed"] == []
    assert result["restored"] == ["projects/Encoded-Project/project_memory.md"]


def test_symlink_memory_root_cannot_read_or_overwrite_outside(tmp_path):
    home, outside = tmp_path / "home", tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    target = outside / "user_profile.md"
    target.write_text("DO NOT READ OR WRITE", encoding="utf-8")
    try:
        (home / "memory").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows link creation permission unavailable")
    manager = MemoryManager(home)
    with pytest.raises(ValueError, match="符号链接"):
        manager.snapshot()
    with pytest.raises(ValueError):
        manager.merge_snapshot(snapshot(("user_profile.md", "attempt")))
    assert target.read_text(encoding="utf-8") == "DO NOT READ OR WRITE"


def test_input_snapshot_is_not_mutated(tmp_path):
    source = snapshot(("user_profile.md", "# Hello\n\nMemory."))
    before = copy.deepcopy(source)
    manager = MemoryManager(tmp_path)
    manager.preview_merge(source)
    manager.merge_snapshot(source)
    assert source == before
