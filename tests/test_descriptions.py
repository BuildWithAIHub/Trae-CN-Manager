from pathlib import Path

from trae_manager.configs import ConfigManager
from trae_manager.descriptions import summary_text


def test_chinese_description_is_one_sentence_and_not_a_trigger_list():
    text = "自动检查接口并生成测试报告。需要新增接口、变更返回格式或排查接口问题时使用。还可批量导出。"
    assert summary_text(text) == "自动检查接口并生成测试报告。"


def test_markdown_and_english_boilerplate_are_removed():
    text = "Use this skill to **manage MCP servers** with [configuration files](https://example.com). Includes scripts and detailed troubleshooting steps."
    assert summary_text(text) == "manage MCP servers with configuration files."


def test_long_descriptions_fit_a_single_compact_row():
    summary = summary_text("用于" + "整理项目和代码" * 30)
    assert summary.endswith("…")
    assert len(summary) <= 43
    assert "\n" not in summary


def test_rule_without_description_uses_first_real_instruction():
    rule = "---\nalwaysApply: true\n---\n# 团队规则\n\n```python\nprint('not a description')\n```\n\n## 输出\n- 默认使用中文回答。提交代码前检查格式。\n"
    assert summary_text(content=rule) == "默认使用中文回答。"


def test_empty_content_and_heading_only_have_clear_fallbacks():
    assert summary_text() == "未填写说明"
    assert summary_text(content="# Python 代码规范\n") == "Python 代码规范"


def test_version_numbers_are_not_mistaken_for_sentence_end():
    assert summary_text("Supports Python 3.11 and Node.js tools. More details here.") == "Supports Python 3.11 and Node.js tools."


def test_item_summary_is_refreshed_after_edit_and_original_content_is_unchanged(tmp_path):
    manager = ConfigManager(tmp_path / "global", tmp_path / "backups")
    original = "---\nname: sample\ndescription: 检查接口。这里有完整的触发条件与配置说明。\n---\n# 完整正文\n所有说明和脚本引用均应保留。\n"
    path = manager.save_item("skills", "sample", original)
    row = manager.list_items("skills")[0]
    assert row["summary"] == "检查接口。"
    assert row["description"] == "检查接口。这里有完整的触发条件与配置说明。"
    assert path.read_text(encoding="utf-8") == original
    edited = original.replace("检查接口。", "生成测试报告。")
    manager.save_existing_item("skills", path, edited)
    assert manager.list_items("skills")[0]["summary"] == "生成测试报告。"
    assert path.read_text(encoding="utf-8") == edited


def test_rule_summary_appears_without_yaml_description(tmp_path):
    manager = ConfigManager(tmp_path / "global", tmp_path / "backups")
    path = manager.save_item("rules", "readable", "# 规则\n\n默认使用中文回复。后面是详细条款。\n")
    assert manager.list_items("rules")[0]["summary"] == "默认使用中文回复。"
    assert "后面是详细条款" in path.read_text(encoding="utf-8")
