"""Granular configuration tests; all files, junctions and copies are temporary."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote
import zipfile

from trae_manager.configs import ConfigManager
from trae_manager.config_extra import _is_link, _unlink_link


SKILL = "---\nname: sample\ndescription: A sample skill\n---\n# Instructions\n"
PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
PRIVATE = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, *_args, **_kwargs):
        yield self.body

    def close(self):
        pass


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        pass


class ConfigExtraTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="trae-config-v2-tests-")
        self.root = Path(self.temporary.name).resolve()
        self.manager = ConfigManager(self.root / "global", self.root / "backups")
        self.native = self.manager.global_home / "skills"
        self.mcp = self.root / "profile" / "User" / "mcp.json"

    def tearDown(self):
        # Remove only junctions themselves before TemporaryDirectory cleanup.
        if _is_link(self.native):
            _unlink_link(self.native)
        self.temporary.cleanup()

    def skill(self, name="sample"):
        return self.manager.save_item("skills", name, SKILL).parent

    def mcp_seed(self):
        self.manager.save_mcp(self.mcp, {"mcpServers": {"one": {"command": "one"}, "two": {"url": "https://example.org/mcp"}}, "future": {"preserve": True}})

    def archive_bytes(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("sample/SKILL.md", SKILL)
            archive.writestr("sample/scripts/test.py", "raise AssertionError('must not run')")
        return data.getvalue()

    def test_resource_lifecycle_outside_discovery_tree(self):
        skill = self.skill()
        self.manager.save_skill_file(skill, "scripts/test.py", "print('example')")
        disabled = self.manager.set_skill_entry_enabled(skill, "scripts", False)
        self.assertFalse((skill / "scripts").exists())
        self.assertFalse(disabled.is_relative_to(self.native))
        self.assertEqual(self.manager.read_skill_file(skill, "scripts/test.py"), "print('example')")
        tree = {entry["relative"]: entry for entry in self.manager.list_skill_tree(skill)}
        self.assertFalse(tree["scripts/test.py"]["enabled"])
        self.assertTrue(tree["SKILL.md"]["enabled"])
        self.manager.set_skill_entry_enabled(skill, "scripts", True)
        self.assertTrue((skill / "scripts" / "test.py").is_file())
        backup = self.manager.delete_skill_entry(skill, "scripts/test.py")
        self.assertTrue(backup.is_file())
        self.assertFalse((skill / "scripts" / "test.py").exists())

    def test_new_children_of_disabled_directory_stay_disabled(self):
        skill = self.skill()
        self.manager.save_skill_file(skill, "scripts/old.py", "old")
        disabled = self.manager.set_skill_entry_enabled(skill, "scripts", False)
        saved = self.manager.save_skill_file(skill, "scripts/nested/new.py", "new")
        folder = self.manager.create_skill_directory(skill, "scripts/empty")
        self.assertEqual(saved, disabled / "nested" / "new.py")
        self.assertEqual(folder, disabled / "empty")
        self.assertFalse((skill / "scripts").exists())

    def test_skill_resource_external_edit_is_not_overwritten(self):
        skill = self.skill()
        path = self.manager.save_skill_file(skill, "resource.txt", "original")
        self.manager.read_skill_file(skill, "resource.txt")
        path.write_text("edited by TRAE", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.manager.save_skill_file(skill, "resource.txt", "manager edit")
        self.assertEqual(path.read_text(), "edited by TRAE")

    def test_binary_invalid_metadata_and_invalid_relative_paths(self):
        skill = self.skill()
        (skill / "binary").write_bytes(b"\x00\xff")
        for method in (self.manager.read_skill_file, lambda s, r: self.manager.save_skill_file(s, r, "overwrite")):
            with self.assertRaises(ValueError):
                method(skill, "binary")
        with self.assertRaises(ValueError):
            self.manager.save_skill_file(skill, "SKILL.md", "missing metadata")
        for relative in ["", "../escape", "/absolute", "C:/escape", "a/./b", "a//b", "NUL", "x."]:
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.manager.save_skill_file(skill, relative, "data")

    def test_SKILL_entry_disables_whole_skill(self):
        skill = self.skill()
        target = self.manager.set_skill_entry_enabled(skill, "SKILL.md", False)
        self.assertFalse(skill.exists())
        self.assertTrue(target.is_file())
        disabled_skill = target.parent
        self.assertTrue(all(not entry["enabled"] for entry in self.manager.list_skill_tree(disabled_skill)))
        with self.assertRaises(ValueError):
            self.manager.set_skill_entry_enabled(disabled_skill, "resource", False)
        self.manager.set_skill_entry_enabled(disabled_skill, "SKILL.md", True)
        self.assertTrue(skill.exists())

    def test_resource_directory_merge_conflict_preflight(self):
        skill = self.skill()
        self.manager.save_skill_file(skill, "scripts/a.txt", "disabled")
        self.manager.set_skill_entry_enabled(skill, "scripts", False)
        self.manager.save_skill_file(skill, "other.txt", "active")
        (skill / "scripts").mkdir()
        (skill / "scripts" / "a.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manager.set_skill_entry_enabled(skill, "scripts", True)
        self.assertEqual((skill / "scripts" / "a.txt").read_text(), "keep")
        mirror = self.manager._skill_mirror(self.native, skill.name)
        self.assertEqual((mirror / "scripts" / "a.txt").read_text(), "disabled")

    def test_resource_directory_merge_without_conflict(self):
        skill = self.skill()
        self.manager.save_skill_file(skill, "scripts/a.txt", "a")
        self.manager.set_skill_entry_enabled(skill, "scripts", False)
        (skill / "scripts").mkdir()
        (skill / "scripts" / "b.txt").write_text("b", encoding="utf-8")
        self.manager.set_skill_entry_enabled(skill, "scripts", True)
        self.assertEqual(sorted(p.name for p in (skill / "scripts").iterdir()), ["a.txt", "b.txt"])

    def test_batch_enable_deduplicates_and_preserves_project_scope(self):
        first, second = self.skill("one"), self.skill("two")
        paths = self.manager.set_items_enabled("skills", [first, first / "SKILL.md", second], False)
        self.assertEqual(len(paths), 2)
        active = self.manager.set_items_enabled("skills", paths, True)
        self.assertEqual({p.parent for p in active}, {first, second})
        project = self.root / "project"
        rule = self.manager.save_item("rules", "nested/a", "rule", project)
        disabled = self.manager.set_items_enabled("rules", [rule], False)
        self.assertFalse(rule.exists())
        self.assertEqual(self.manager.set_items_enabled("rules", disabled, True), [rule])
        self.assertEqual(self.manager.set_items_enabled("skills", [], False), [])

    def test_batch_conflict_changes_nothing(self):
        first, second = self.skill("one"), self.skill("two")
        conflict = self.manager._disabled_root(self.native, "skills") / second.name
        conflict.mkdir(parents=True)
        (conflict / "SKILL.md").write_text(SKILL)
        with self.assertRaises(ValueError):
            self.manager.set_items_enabled("skills", [first, second], False)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertFalse((conflict.parent / "one").exists())

    def test_batch_enable_failure_rolls_back_completed_moves(self):
        first, second = self.skill("one"), self.skill("two")
        original = self.manager.toggle_item
        calls = []

        def fail_second(kind, path):
            calls.append(path)
            result = original(kind, path)
            if len(calls) == 2:
                raise RuntimeError("injected failure after second move")
            return result

        with patch.object(self.manager, "toggle_item", side_effect=fail_second), self.assertRaises(RuntimeError):
            self.manager.set_items_enabled("skills", [first, second], False)
        self.assertEqual((first / "SKILL.md").read_text(), SKILL)
        self.assertEqual((second / "SKILL.md").read_text(), SKILL)
        disabled = self.manager._disabled_root(self.native, "skills")
        self.assertFalse((disabled / "one").exists())
        self.assertFalse((disabled / "two").exists())

    def test_batch_delete_failure_restores_items_and_disabled_resources(self):
        first, second = self.skill("one"), self.skill("two")
        self.manager.save_skill_file(first, "old.txt", "keep")
        mirror_file = self.manager.set_skill_entry_enabled(first, "old.txt", False)
        original = self.manager.remove_item

        def fail_second(kind, path):
            if Path(path).name == "two":
                raise RuntimeError("injected failure")
            return original(kind, path)

        with patch.object(self.manager, "remove_item", side_effect=fail_second), self.assertRaises(RuntimeError):
            self.manager.delete_items("skills", [first, second])
        self.assertTrue((first / "SKILL.md").exists())
        self.assertTrue((second / "SKILL.md").exists())
        self.assertEqual(mirror_file.read_text(), "keep")

    def test_singular_delete_cleans_disabled_resources(self):
        skill = self.skill()
        self.manager.save_skill_file(skill, "old.txt", "keep")
        mirror_file = self.manager.set_skill_entry_enabled(skill, "old.txt", False)
        backup = self.manager.remove_item("skills", skill)
        self.assertTrue((backup / "SKILL.md").exists())
        self.assertFalse(skill.exists())
        self.assertFalse(mirror_file.exists())

    def test_invalid_batches_are_business_errors(self):
        for paths in ["x", 42, [42]]:
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.manager.set_items_enabled("skills", paths, False)
        with self.assertRaises(ValueError):
            self.manager.set_items_enabled("skills", [], 0)
        outside = self.root / "keep"
        outside.mkdir()
        with self.assertRaises(ValueError):
            self.manager.delete_items("skills", [outside])
        self.assertTrue(outside.exists())
        with self.assertRaises(ValueError):
            self.manager.delete_items("unknown", [])

    def test_save_existing_disabled_rule_keeps_scope_and_disabled_state(self):
        project = self.root / "project"
        path = self.manager.save_item("rules", "nested/style", "original", project)
        disabled = self.manager.toggle_item("rules", path)
        self.manager.read_item(disabled)
        saved = self.manager.save_existing_item("rules", disabled, "edited")
        self.assertEqual(saved, disabled)
        self.assertFalse(path.exists())
        self.assertEqual(disabled.read_text(), "edited")
        self.assertFalse(self.manager.list_items("rules", project)[0]["enabled"])

    def test_save_existing_skill_preserves_custom_root_and_disabled_state(self):
        skill = self.skill()
        target = self.root / "custom"
        self.manager.configure_skills_root(target)
        disabled = self.manager.toggle_item("skills", skill)
        saved = self.manager.save_existing_item("skills", disabled, SKILL + "edited\n")
        self.assertEqual(saved, disabled)
        self.assertFalse((target / "sample").exists())
        self.assertIn("edited", disabled.read_text())

    def test_save_existing_conflict_and_unmanaged_path_are_rejected(self):
        path = self.manager.save_item("rules", "one", "original")
        self.manager.read_item(path)
        path.write_text("TRAE edit")
        with self.assertRaises(RuntimeError):
            self.manager.save_existing_item("rules", path, "manager edit")
        self.assertEqual(path.read_text(), "TRAE edit")
        foreign = self.root / "foreign.md"
        foreign.write_text("keep")
        with self.assertRaises(ValueError):
            self.manager.save_existing_item("rules", foreign, "overwrite")
        self.assertEqual(foreign.read_text(), "keep")

    def test_mcp_selected_and_all_operations(self):
        self.mcp_seed()
        self.manager.set_mcp_servers_enabled(self.mcp, ["one", "one"], False)
        self.assertEqual({s["name"] for s in self.manager.list_mcp(self.mcp) if s["enabled"]}, {"two"})
        self.manager.set_mcp_servers_enabled(self.mcp, None, True)
        self.assertTrue(all(s["enabled"] for s in self.manager.list_mcp(self.mcp)))
        self.manager.delete_mcp_servers(self.mcp, ["one"])
        self.assertEqual([s["name"] for s in self.manager.list_mcp(self.mcp)], ["two"])
        self.manager.delete_mcp_servers(self.mcp, None)
        self.assertEqual(self.manager.list_mcp(self.mcp), [])
        self.assertEqual(self.manager.read_mcp(self.mcp)["future"], {"preserve": True})

    def test_invalid_mcp_selection_never_partially_saves(self):
        self.mcp_seed()
        original = self.mcp.read_bytes()
        for selection in [["one", "missing"], "one", 3, [[]], [None]]:
            for operation in [lambda names: self.manager.set_mcp_servers_enabled(self.mcp, names, False), lambda names: self.manager.delete_mcp_servers(self.mcp, names)]:
                with self.subTest(selection=selection), self.assertRaises(ValueError):
                    operation(selection)
                self.assertEqual(self.mcp.read_bytes(), original)

    def test_deeplink_base64_json_and_urlsafe_without_padding(self):
        document = {"command": "npx", "args": ["-y", "example"]}
        raw = json.dumps(document).encode()
        variants = [base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("="), json.dumps(document)]
        with patch("trae_manager.config_extra.requests.Session") as session:
            for index, encoded in enumerate(variants):
                count = self.manager.import_mcp_url(self.mcp, "trae-cn://trae.ai/mcp-import?name=test" + str(index) + "&config=" + quote(encoded, safe=""))
                self.assertEqual(count, 1)
        session.assert_not_called()
        self.assertEqual(len(self.manager.list_mcp(self.mcp)), 3)

    def test_invalid_deeplink_and_missing_single_server_name(self):
        for url in ["trae-cn://trae.ai/other?config=e30=", "trae-cn://trae.ai/mcp-import", "trae-cn://trae.ai/mcp-import?name=x&config=not-base64!"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.manager.import_mcp_url(self.mcp, url)
        with patch.object(self.manager, "_remote_data", return_value=(b'{"command":"npx"}', "https://example.org/mcp.json")), self.assertRaises(ValueError):
            self.manager.import_mcp_url(self.mcp, "https://example.org/mcp.json")

    def test_remote_mcp_jsonc_and_github_blob_conversion(self):
        session = Session([Response(b'{/* comment */"mcpServers":{"x":{"command":"x"}},}')])
        with patch("trae_manager.config_extra.requests.Session", return_value=session), patch("trae_manager.config_extra.socket.getaddrinfo", return_value=PUBLIC):
            self.assertEqual(self.manager.import_mcp_url(self.mcp, "https://github.com/owner/repo/blob/main/mcp.json"), 1)
        self.assertEqual(session.calls[0][0], "https://raw.githubusercontent.com/owner/repo/main/mcp.json")
        self.assertFalse(session.trust_env)

    def test_remote_url_rejects_unsafe_destinations_and_credentials(self):
        for url in ["http://example.org/a", "file:///C:/secret", "https://user:pass@github.com/o/r/blob/main/a", "https://user:pass@example.org/a"]:
            with patch("trae_manager.config_extra.requests.Session") as session, self.subTest(url=url), self.assertRaises(ValueError):
                self.manager._remote_data(url, 100)
            session.assert_not_called()
        with patch("trae_manager.config_extra.requests.Session", return_value=Session([])), patch("trae_manager.config_extra.socket.getaddrinfo", return_value=PRIVATE), self.assertRaises(ValueError):
            self.manager._remote_data("https://127.0.0.1/a", 100)

    def test_remote_redirect_revalidates_target(self):
        session = Session([Response(status=302, headers={"Location": "https://localhost/a"})])
        with patch("trae_manager.config_extra.requests.Session", return_value=session), patch("trae_manager.config_extra.socket.getaddrinfo", side_effect=[PUBLIC, PRIVATE]), self.assertRaises(ValueError):
            self.manager._remote_data("https://example.org/a", 100)
        self.assertEqual(len(session.calls), 1)

    def test_remote_limits_stream_endpoint_and_http_errors(self):
        for response in [Response(b"12345"), Response(headers={"Content-Length": "999"}), Response(headers={"Content-Type": "text/event-stream"})]:
            with patch("trae_manager.config_extra.requests.Session", return_value=Session([response])), patch("trae_manager.config_extra.socket.getaddrinfo", return_value=PUBLIC), self.assertRaises(ValueError):
                self.manager._remote_data("https://example.org/a", 4)
        with patch("trae_manager.config_extra.requests.Session", return_value=Session([Response(status=404)])), patch("trae_manager.config_extra.socket.getaddrinfo", return_value=PUBLIC), self.assertRaises(RuntimeError):
            self.manager._remote_data("https://example.org/a", 4)

    def test_remote_skill_zip_and_markdown_are_data_only(self):
        with patch.object(self.manager, "_remote_data", return_value=(self.archive_bytes(), "https://example.org/skill.zip")), patch("subprocess.Popen") as process:
            installed = self.manager.import_skill_url("https://example.org/skill.zip")
        process.assert_not_called()
        self.assertTrue((installed[0] / "scripts" / "test.py").is_file())
        self.manager.remove_item("skills", installed[0])
        with patch.object(self.manager, "_remote_data", return_value=(SKILL.encode(), "https://example.org/SKILL.md")):
            installed = self.manager.import_skill_url("https://example.org/SKILL.md")
        self.assertEqual(installed[0].name, "sample")
        self.assertIn("主文件", self.manager.last_skill_import_message)

    def test_local_skill_markdown_import(self):
        markdown = self.root / "download.md"
        markdown.write_text(SKILL, encoding="utf-8")
        installed = self.manager.import_skill(markdown)
        self.assertEqual(installed[0].name, "sample")
        self.assertEqual((installed[0] / "SKILL.md").read_text(), SKILL)

    def test_mcp_folder_search_is_bounded(self):
        folder = self.root / "configs"
        (folder / "one" / "two").mkdir(parents=True)
        for path in [folder / "mcp.json", folder / "one" / ".mcp.json", folder / "one" / "two" / "mcp_config.json"]:
            path.write_text("{}")
        (folder / "ignore.json").write_text("{}")
        self.assertEqual(len(self.manager.list_mcp_files(folder, max_depth=0)), 1)
        self.assertEqual(len(self.manager.list_mcp_files(folder, max_depth=1)), 2)
        self.assertEqual(len(self.manager.list_mcp_files(folder, max_count=1)), 1)
        for args in [{"max_depth": -1}, {"max_count": 201}]:
            with self.assertRaises(ValueError):
                self.manager.list_mcp_files(folder, **args)

    def test_custom_storage_native_junction_and_full_lifecycle(self):
        skill = self.skill()
        target = self.root / "custom-skills"
        result = self.manager.configure_skills_root(target)
        self.assertTrue(_is_link(self.native))
        self.assertEqual(self.native.resolve(), target.resolve())
        self.assertEqual(Path(result["root"]), target.resolve())
        self.assertTrue((self.native / "sample" / "SKILL.md").is_file())
        self.assertEqual(self.manager._root("skills"), self.native)
        self.assertEqual(self.manager._owned("skills", target / "sample")[0], self.native)
        self.assertEqual(len(self.manager.list_items("skills")), 1)
        saved = self.manager.save_skill_file(skill, "scripts/a.txt", "a")
        self.assertEqual(saved.resolve(), target / "sample" / "scripts" / "a.txt")
        disabled_resource = self.manager.set_skill_entry_enabled(skill, "scripts", False)
        self.assertFalse(disabled_resource.is_relative_to(target))
        self.manager.set_skill_entry_enabled(skill, "scripts", True)
        disabled = self.manager.set_items_enabled("skills", [skill], False)
        self.assertFalse((target / "sample").exists())
        self.manager.set_items_enabled("skills", disabled, True)
        self.manager.delete_items("skills", [skill])
        self.assertTrue(target.is_dir())
        self.assertTrue(_is_link(self.native))
        self.assertFalse((target / "sample").exists())

    def test_switch_custom_storage_then_return_default_preserves_old_targets(self):
        self.skill()
        first, second = self.root / "first", self.root / "second"
        self.manager.configure_skills_root(first)
        self.manager.configure_skills_root(second)
        self.assertTrue((first / "sample" / "SKILL.md").exists())
        self.assertEqual(self.native.resolve(), second.resolve())
        self.manager.configure_skills_root(None)
        self.assertFalse(_is_link(self.native))
        self.assertTrue((self.native / "sample" / "SKILL.md").exists())
        self.assertTrue((second / "sample" / "SKILL.md").exists())
        self.assertFalse((self.manager.global_home / ".trae-manager-skills-root.json").exists())
        self.assertEqual(len(self.manager.list_items("skills")), 1)

    def test_storage_conflict_does_not_change_existing_directories(self):
        skill = self.skill()
        target = self.root / "custom"
        (target / "sample").mkdir(parents=True)
        (target / "sample" / "SKILL.md").write_text("keep")
        with self.assertRaises(ValueError):
            self.manager.configure_skills_root(target)
        self.assertFalse(_is_link(self.native))
        self.assertEqual((skill / "SKILL.md").read_text(), SKILL)
        self.assertEqual((target / "sample" / "SKILL.md").read_text(), "keep")

    def test_storage_partial_copy_failure_removes_partial_files(self):
        skill = self.skill()
        (skill / "resource.txt").write_text("resource")
        target = self.root / "custom"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        original_copy = shutil.copy2
        calls = []

        def fail_second(source, destination, *args, **kwargs):
            if Path(destination).is_relative_to(target):
                calls.append(destination)
                if len(calls) == 2:
                    Path(destination).write_text("partial")
                    raise OSError("injected copy failure")
            return original_copy(source, destination, *args, **kwargs)

        with patch("trae_manager.config_extra.shutil.copy2", side_effect=fail_second), self.assertRaises(RuntimeError):
            self.manager.configure_skills_root(target)
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["keep.txt"])
        self.assertTrue((skill / "SKILL.md").exists())
        self.assertFalse(_is_link(self.native))

    def test_storage_link_creation_failure_restores_original(self):
        skill = self.skill()
        target = self.root / "custom"
        with patch.object(self.manager, "_make_directory_link", side_effect=OSError("injected link failure")), self.assertRaises(RuntimeError):
            self.manager.configure_skills_root(target)
        self.assertEqual((skill / "SKILL.md").read_text(), SKILL)
        self.assertFalse(_is_link(self.native))
        self.assertFalse(target.exists())

    def test_storage_marker_failure_restores_old_native_junction(self):
        self.skill()
        first, second = self.root / "first", self.root / "second"
        self.manager.configure_skills_root(first)
        marker = self.manager.global_home / ".trae-manager-skills-root.json"
        old_marker = marker.read_bytes()
        with patch.object(self.manager, "_write", side_effect=RuntimeError("injected marker failure")), self.assertRaises(RuntimeError):
            self.manager.configure_skills_root(second)
        self.assertTrue(_is_link(self.native))
        self.assertEqual(self.native.resolve(), first.resolve())
        self.assertEqual(marker.read_bytes(), old_marker)
        self.assertTrue((first / "sample" / "SKILL.md").exists())
        self.assertFalse(second.exists())

    def test_storage_marker_failure_restores_original_normal_directory(self):
        skill = self.skill()
        target = self.root / "custom"
        with patch.object(self.manager, "_write", side_effect=RuntimeError("injected marker failure")), self.assertRaises(RuntimeError):
            self.manager.configure_skills_root(target)
        self.assertFalse(_is_link(self.native))
        self.assertEqual((skill / "SKILL.md").read_text(), SKILL)
        self.assertFalse(target.exists())
        self.assertFalse((self.manager.global_home / ".trae-manager-skills-root.json").exists())

    def test_disabled_resources_survive_storage_migration_and_default_restore(self):
        skill = self.skill()
        self.manager.save_skill_file(skill, "disabled.txt", "keep")
        disabled = self.manager.set_skill_entry_enabled(skill, "disabled.txt", False)
        target = self.root / "custom"
        self.manager.configure_skills_root(target)
        self.assertEqual(self.manager.read_skill_file(skill, "disabled.txt"), "keep")
        self.assertTrue(disabled.exists())
        self.manager.configure_skills_root(None)
        self.assertEqual(self.manager.read_skill_file(skill, "disabled.txt"), "keep")
        self.manager.set_skill_entry_enabled(skill, "disabled.txt", True)
        self.assertTrue((skill / "disabled.txt").exists())

    def test_resource_merge_failure_restores_active_and_disabled_directories(self):
        skill = self.skill()
        self.manager.save_skill_file(skill, "scripts/a.txt", "a")
        disabled = self.manager.set_skill_entry_enabled(skill, "scripts", False)
        (skill / "scripts").mkdir()
        (skill / "scripts" / "b.txt").write_text("b")
        original = shutil.copytree

        def fail_copy(source, destination, *args, **kwargs):
            if kwargs.get("dirs_exist_ok"):
                (Path(destination) / "a.txt").write_text("partial")
                raise OSError("injected merge failure")
            return original(source, destination, *args, **kwargs)

        with patch("trae_manager.config_extra.shutil.copytree", side_effect=fail_copy), self.assertRaises(RuntimeError):
            self.manager.set_skill_entry_enabled(skill, "scripts", True)
        self.assertEqual(sorted(p.name for p in (skill / "scripts").iterdir()), ["b.txt"])
        self.assertEqual((disabled / "a.txt").read_text(), "a")

    def test_restore_default_failure_restores_custom_link(self):
        self.skill()
        target = self.root / "custom"
        self.manager.configure_skills_root(target)
        marker = self.manager.global_home / ".trae-manager-skills-root.json"
        original_unlink = Path.unlink

        def fail_marker(path, *args, **kwargs):
            if path == marker:
                raise OSError("injected marker deletion failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_marker), self.assertRaises(RuntimeError):
            self.manager.configure_skills_root(None)
        self.assertTrue(_is_link(self.native))
        self.assertEqual(self.native.resolve(), target.resolve())
        self.assertTrue((self.native / "sample" / "SKILL.md").exists())

    def test_unowned_junction_and_child_junction_targets_are_preserved(self):
        target = self.root / "existing"
        (target / "sample").mkdir(parents=True)
        (target / "sample" / "SKILL.md").write_text(SKILL)
        self.native.parent.mkdir(parents=True)
        self.manager._make_directory_link(self.native, target)
        with self.assertRaises(ValueError):
            self.manager.configure_skills_root(None)
        self.assertTrue((target / "sample" / "SKILL.md").exists())
        alias = target / "alias"
        self.manager._make_directory_link(alias, target / "sample")
        try:
            for path in [alias, alias / "SKILL.md", self.native / "alias" / "SKILL.md"]:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.manager.remove_item("skills", path)
            self.assertTrue((target / "sample" / "SKILL.md").exists())
        finally:
            _unlink_link(alias)

    def test_storage_rejects_nested_destination_and_handles_empty_default(self):
        skill = self.skill()
        with self.assertRaises(ValueError):
            self.manager.configure_skills_root(skill / "nested")
        self.assertTrue(skill.exists())
        self.manager.remove_item("skills", skill)
        target = self.root / "empty-custom"
        self.manager.configure_skills_root(target)
        self.assertTrue(_is_link(self.native))
        self.manager.configure_skills_root(None)
        self.assertTrue(self.native.is_dir())
        self.assertFalse(_is_link(self.native))

    def test_broken_custom_junction_can_return_to_default(self):
        target = self.root / "empty-custom"
        self.manager.configure_skills_root(target)
        target.rmdir()
        self.assertTrue(_is_link(self.native))
        self.assertFalse(self.native.exists())
        self.manager.configure_skills_root(None)
        self.assertTrue(self.native.is_dir())
        self.assertFalse(_is_link(self.native))


if __name__ == "__main__":
    unittest.main()
