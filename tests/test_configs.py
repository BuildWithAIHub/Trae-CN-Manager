"""All configuration tests use temporary directories and mocked transports."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from trae_manager.configs import ConfigManager, parse_jsonc


SKILL = "---\nname: sample\ndescription: A sample skill\n---\n# Instructions\nNo scripts execute during import.\n"


class Response:
    def __init__(self, body=b"", status=200, headers=None, lines=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.lines = lines or []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_content(self, _=65536, **kwargs):
        yield self.body

    def iter_lines(self, **kwargs):
        yield from self.lines

    def close(self):
        pass


class Session:
    def __init__(self, responses, gets=None):
        self.responses = list(responses)
        self.gets = list(gets or [])
        self.calls = []
        self.trust_env = True

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.gets.pop(0)

    def close(self):
        pass


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="trae-config-tests-")
        self.root = Path(self.temporary.name)
        self.manager = ConfigManager(self.root / "global", self.root / "backups")
        self.mcp = self.manager.mcp_path(profile=self.root / "profile")

    def tearDown(self):
        self.temporary.cleanup()

    def seed(self, document=None):
        self.manager.save_mcp(self.mcp, document or {"mcpServers": {"test": {"command": "example", "args": ["--value", "x"]}}})

    def archive(self, name="skill.zip", entries=None):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for filename, content in (entries or {"sample/SKILL.md": SKILL}).items():
                archive.writestr(filename, content)
        return path

    def test_jsonc_keeps_strings_and_unknown_fields(self):
        text = '''{
          // comment
          "mcpServers": {"remote": {"url": "https://example.com/a//b?x=/*literal*/",},},
          "future": {"nested": [1, 2,], "literal": "comma,}"},
        }'''
        document = parse_jsonc(text)
        self.manager.save_mcp(self.mcp, document)
        actual = self.manager.read_mcp(self.mcp)
        self.assertEqual(actual["future"], {"nested": [1, 2], "literal": "comma,}"})
        self.assertIn("/*literal*/", actual["mcpServers"]["remote"]["url"])

    def test_jsonc_invalid_duplicate_nan_unclosed(self):
        for raw in ['{"x":1,"x":2}', '{"x":NaN}', '/* unclosed', '[1]', '{']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_jsonc(raw)

    def test_missing_config_is_empty_without_writing(self):
        self.assertEqual(self.manager.read_mcp(self.mcp), {"mcpServers": {}})
        self.assertFalse(self.mcp.exists())

    def test_scope_paths(self):
        self.assertEqual(self.mcp, self.root / "profile" / "User" / "mcp.json")
        self.assertEqual(self.manager.mcp_path(project=self.root / "project"), self.root / "project" / ".trae" / "mcp.json")

    def test_backups_capture_original_and_restore(self):
        self.seed()
        original = self.mcp.read_bytes()
        self.manager.upsert_mcp(self.mcp, "second", {"url": "http://127.0.0.1:9000/mcp"})
        backups = [p for p in self.manager.backup_dir.iterdir() if not p.name.endswith(".restore.json")]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(json.loads(backups[0].with_name(backups[0].name + ".restore.json").read_text(encoding="utf-8"))["original_path"], str(self.mcp))
        with self.assertRaises(ValueError):
            self.manager.restore_backup(backups[0])
        self.manager.restore_backup(backups[0], overwrite=True)
        self.assertEqual(self.mcp.read_bytes(), original)

    def test_concurrent_external_change_is_not_overwritten(self):
        self.seed()
        document = self.manager.read_mcp(self.mcp)
        external = b'{"mcpServers":{},"external":true}'
        self.mcp.write_bytes(external)
        with self.assertRaises(RuntimeError):
            self.manager.save_mcp(self.mcp, document)
        self.assertEqual(self.mcp.read_bytes(), external)

    def test_concurrent_creation_is_not_overwritten(self):
        document = self.manager.read_mcp(self.mcp)
        self.mcp.parent.mkdir(parents=True)
        self.mcp.write_text('{"mcpServers":{},"external":true}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.manager.save_mcp(self.mcp, document)

    def test_change_during_backup_detected_and_temporary_removed(self):
        self.seed()
        document = self.manager.read_mcp(self.mcp)
        original_backup = self.manager._backup

        def change(path):
            backup = original_backup(path)
            path.write_text('{"mcpServers":{},"external":true}', encoding="utf-8")
            return backup

        with patch.object(self.manager, "_backup", side_effect=change), self.assertRaises(RuntimeError):
            self.manager.save_mcp(self.mcp, document)
        self.assertTrue(json.loads(self.mcp.read_text())["external"])
        self.assertFalse(list(self.mcp.parent.glob(".trae-manager-*.tmp")))

    def test_invalid_server_does_not_create_file(self):
        invalid = [None, {"args": []}, {"command": "x", "url": "http://localhost"}, {"command": "x", "args": "bad"}, {"command": "x", "env": {"KEY": 1}}, {"url": "file:///private"}, {"url": "https://user:password@example.com/mcp"}]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.manager.save_mcp(self.mcp, {"mcpServers": {"bad": config}})
        self.assertFalse(self.mcp.exists())

    def test_mcp_toggle_removes_from_active_for_project_compatibility(self):
        self.seed({"mcpServers": {"test": {"command": "example", "custom": {"preserve": True}}}, "future": [1, 2]})
        self.manager.set_mcp_enabled(self.mcp, "test", False)
        document = self.manager.read_mcp(self.mcp)
        self.assertNotIn("test", document["mcpServers"])
        self.assertEqual(document["_trae_manager"]["disabledMcpServers"]["test"]["custom"], {"preserve": True})
        self.assertFalse(self.manager.list_mcp(self.mcp)[0]["enabled"])
        self.manager.upsert_mcp(self.mcp, "test", {"command": "updated"})
        self.assertFalse(self.manager.list_mcp(self.mcp)[0]["enabled"])
        self.manager.set_mcp_enabled(self.mcp, "test", True)
        self.assertTrue(self.manager.list_mcp(self.mcp)[0]["enabled"])
        self.assertEqual(self.manager.read_mcp(self.mcp)["future"], [1, 2])
        self.manager.delete_mcp(self.mcp, "test")
        self.assertEqual(self.manager.list_mcp(self.mcp), [])

    def test_native_disabled_supported_and_unknown_delete_rejected(self):
        self.seed({"mcpServers": {"native": {"command": "x", "disabled": True}}})
        self.assertFalse(self.manager.list_mcp(self.mcp)[0]["enabled"])
        self.manager.set_mcp_enabled(self.mcp, "native", True)
        self.assertTrue(self.manager.list_mcp(self.mcp)[0]["enabled"])
        with self.assertRaises(ValueError):
            self.manager.delete_mcp(self.mcp, "missing")

    def test_import_conflicts_are_atomic(self):
        self.seed()
        before = self.mcp.read_bytes()
        with self.assertRaises(ValueError):
            self.manager.import_mcp(self.mcp, {"mcpServers": {"new": {"command": "new"}, "test": {"command": "overwrite"}}})
        self.assertEqual(self.mcp.read_bytes(), before)
        count = self.manager.import_mcp(self.mcp, '{"mcpServers":{"new":{"command":"new"}},"future":true,}')
        self.assertEqual(count, 1)
        self.assertTrue(self.manager.read_mcp(self.mcp)["future"])

    def test_vscode_import_and_unknown_manager_metadata(self):
        self.manager.import_mcp(self.mcp, {"servers": {"server": {"type": "stdio", "command": "example"}}, "_trae_manager": {"future": {"v": 2}}})
        document = self.manager.read_mcp(self.mcp)
        self.assertIn("server", document["mcpServers"])
        self.assertEqual(document["_trae_manager"]["future"], {"v": 2})
        with self.assertRaises(ValueError):
            self.manager.import_mcp(self.mcp, {"command": "individual-is-not-a-document"})

    def test_export_redacts_and_requires_new_destination(self):
        self.seed({"mcpServers": {"secret": {"command": "x", "args": ["--token", "very-secret"], "env": {"TOKEN": "secret"}, "headers": {"Authorization": "Bearer secret"}}}})
        target = self.root / "export.json"
        self.manager.export_mcp(self.mcp, target)
        output = target.read_text(encoding="utf-8")
        self.assertNotIn("very-secret", output)
        self.assertNotIn("Bearer secret", output)
        self.assertIn("REDACTED", output)
        with self.assertRaises(ValueError):
            self.manager.export_mcp(self.mcp, target)
        original = self.root / "explicit-secrets.json"
        self.manager.export_mcp(self.mcp, original, include_secrets=True)
        self.assertIn("very-secret", original.read_text())

    def test_skills_lifecycle_and_directory_identifier(self):
        path = self.manager.save_item("skills", "folder-name", SKILL)
        items = self.manager.list_items("skills")
        self.assertEqual(items[0]["name"], "folder-name")
        self.assertEqual(items[0]["description"], "A sample skill")
        self.assertEqual(self.manager.read_item(path), SKILL)
        disabled = self.manager.toggle_item("skills", path)
        self.assertFalse(path.exists())
        self.assertFalse(self.manager.list_items("skills")[0]["enabled"])
        self.manager.save_item("skills", "folder-name", SKILL + "edited\n")
        self.assertIn("edited", disabled.read_text())
        active = self.manager.toggle_item("skills", disabled)
        backup = self.manager.remove_item("skills", active)
        self.assertFalse(active.exists())
        self.assertTrue((backup / "SKILL.md").exists())
        self.manager.restore_backup(backup)
        self.assertTrue(active.exists())

    def test_rule_project_scope_and_nested_directories(self):
        project = self.root / "project"
        path = self.manager.save_item("rules", "frontend/style", "---\nalwaysApply: true\n---\nUse rules.\n", project)
        self.assertEqual(path, project / ".trae" / "rules" / "frontend" / "style.md")
        self.assertEqual(len(self.manager.list_items("rules", project)), 1)
        self.assertEqual(self.manager.list_items("rules"), [])
        disabled = self.manager.toggle_item("rules", path)
        self.assertFalse(path.exists())
        self.assertTrue(disabled.exists())
        self.manager.toggle_item("rules", disabled)
        self.assertTrue(path.exists())

    def test_item_external_modification_detected(self):
        path = self.manager.save_item("rules", "rule", "original")
        self.manager.read_item(path)
        path.write_text("external", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.manager.save_item("rules", "rule", "edited")
        self.assertEqual(path.read_text(), "external")

    def test_disabled_item_conflict_does_not_replace_active_content(self):
        path = self.manager.save_item("skills", "sample", SKILL)
        disabled = self.manager.toggle_item("skills", path)
        path.parent.mkdir(parents=True)
        path.write_text(SKILL + "active-copy", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manager.save_item("skills", "sample", SKILL + "overwrite")
        self.assertIn("active-copy", path.read_text())
        self.assertNotIn("active-copy", disabled.read_text())

    def test_yaml_tags_are_never_executed(self):
        payload = "---\nname: sample\ndescription: !!python/object/apply:os.system ['do-not-run']\n---\n"
        with patch("os.system") as system, self.assertRaises(ValueError):
            self.manager.save_item("skills", "malicious", payload)
        system.assert_not_called()

    def test_unmanaged_and_invalid_paths_rejected(self):
        outside = self.root / "unrelated.md"
        outside.write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manager.remove_item("rules", outside)
        for name in ["../escape", "C:/escape", "CON", "x.", "a/b/c/d/e"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.manager.save_item("rules", name, "rule")
        with self.assertRaises(ValueError):
            self.manager.save_item("skills", "bad", "# Missing metadata")
        self.assertTrue(outside.exists())

    def test_zip_import_export_does_not_execute_scripts(self):
        archive = self.archive(entries={"sample/SKILL.md": SKILL, "sample/scripts/task.py": "raise Exception('must not run')"})
        installed = self.manager.import_skill(archive)
        self.assertEqual(len(installed), 1)
        self.assertTrue((installed[0] / "scripts" / "task.py").exists())
        output = self.manager.export_item(installed[0], self.root / "out.zip")
        with zipfile.ZipFile(output) as result:
            self.assertIn("sample/SKILL.md", result.namelist())
        with self.assertRaises(ValueError):
            self.manager.import_skill(archive)

    def test_root_zip_skill_uses_metadata_name(self):
        installed = self.manager.import_skill(self.archive(entries={"SKILL.md": SKILL}))
        self.assertEqual(installed[0].name, "sample")

    def test_directory_import_preserves_resources(self):
        source = self.root / "source"
        source.mkdir()
        (source / "SKILL.md").write_text(SKILL)
        (source / "asset.txt").write_text("asset")
        installed = self.manager.import_skill(source, project=self.root / "project")
        self.assertEqual((installed[0] / "asset.txt").read_text(), "asset")

    def test_zip_traversal_absolute_device_and_symlink_rejected(self):
        names = ["../escape.md", "C:/escape.md", "/absolute.md", "a/../escape.md", "a\\..\\escape.md", "CON", "a/./bad.md"]
        for index, name in enumerate(names):
            archive = self.archive(name=f"bad-{index}.zip", entries={"sample/SKILL.md": SKILL, name: "evil"})
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.manager.import_skill(archive)
        archive = self.root / "symlink.zip"
        with zipfile.ZipFile(archive, "w") as file:
            info = zipfile.ZipInfo("sample/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            file.writestr(info, "../../outside")
            file.writestr("sample/SKILL.md", SKILL)
        with self.assertRaises(ValueError):
            self.manager.import_skill(archive)
        self.assertFalse((self.root / "escape.md").exists())

    def test_zip_limits_missing_metadata_and_bundle_collision(self):
        archive = self.archive(entries={"sample/SKILL.md": SKILL})
        with patch("trae_manager.configs.MAX_EXTRACTED_BYTES", 3), self.assertRaises(ValueError):
            self.manager.import_skill(archive)
        with patch("trae_manager.configs.MAX_ARCHIVE_FILES", 0), self.assertRaises(ValueError):
            self.manager.import_skill(archive)
        with self.assertRaises(ValueError):
            self.manager.import_skill(self.archive(name="missing.zip", entries={"README.md": "No skill"}))
        invalid = self.archive(name="invalid.zip", entries={"bad/SKILL.md": "---\nname: missing-description\n---\n"})
        with self.assertRaises(ValueError):
            self.manager.import_skill(invalid)
        bundle = self.archive(name="bundle.zip", entries={"one/sample/SKILL.md": SKILL, "two/sample/SKILL.md": SKILL})
        with self.assertRaises(ValueError):
            self.manager.import_skill(bundle)
        self.assertFalse((self.manager.global_home / "skills").exists())

    def test_nested_skills_and_duplicate_zip_entries_rejected(self):
        nested = self.archive(entries={"parent/SKILL.md": SKILL, "parent/child/SKILL.md": SKILL})
        with self.assertRaises(ValueError):
            self.manager.import_skill(nested)
        duplicate = self.archive(name="duplicate.zip", entries={"A/SKILL.md": SKILL, "a/SKILL.md": SKILL})
        with self.assertRaises(ValueError):
            self.manager.import_skill(duplicate)

    def test_github_only_public_allowlisted_network(self):
        for url in ["http://github.com/a/b", "https://127.0.0.1/a.zip", "https://evil.example/a/b", "https://github.com.evil.example/a/b", "https://user:pass@github.com/a/b"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.manager.import_github_skill(url)
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.113.3", 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        session = Session([], gets=[Response(status=302, headers={"Location": "https://internal.example/evil.zip"})])
        with patch("trae_manager.configs.requests.Session", return_value=session), patch("trae_manager.configs.socket.getaddrinfo", return_value=public), self.assertRaises(ValueError):
            self.manager.import_github_skill("https://github.com/example/repo")
        self.assertEqual(len(session.calls), 1)
        with patch("trae_manager.configs.requests.Session", return_value=Session([])), patch("trae_manager.configs.socket.getaddrinfo", return_value=private), self.assertRaises(ValueError):
            self.manager.import_github_skill("https://github.com/example/repo")

    def test_github_zip_import_mocked(self):
        raw = self.archive().read_bytes()
        session = Session([], gets=[Response(status=302, headers={"Location": "https://codeload.github.com/example/repo/zip/HEAD"}), Response(raw)])
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.113.3", 443))]
        with patch("trae_manager.configs.requests.Session", return_value=session), patch("trae_manager.configs.socket.getaddrinfo", return_value=public):
            installed = self.manager.import_github_skill("https://github.com/example/repo")
        self.assertEqual(installed[0].name, "sample")
        self.assertFalse(session.trust_env)

    def test_github_tree_import_filters_selected_subdirectory(self):
        raw = self.archive(entries={"repo-main/skills/selected/SKILL.md": SKILL,
                                    "repo-main/skills/selected/asset.txt": "selected-resource",
                                    "repo-main/skills/unrelated/SKILL.md": SKILL}).read_bytes()
        session = Session([], gets=[Response(status=404), Response(status=404), Response(raw)])
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.113.3", 443))]
        with patch("trae_manager.configs.requests.Session", return_value=session), patch("trae_manager.configs.socket.getaddrinfo", return_value=public):
            installed = self.manager.import_github_skill("https://github.com/example/repo/tree/main/skills/selected")
        self.assertEqual([path.name for path in installed], ["selected"])
        self.assertEqual((installed[0] / "asset.txt").read_text(), "selected-resource")
        self.assertFalse((installed[0].parent / "unrelated").exists())
        self.assertIn("main%2Fskills%2Fselected.zip", session.calls[0][1])
        self.assertIn("/main.zip", session.calls[-1][1])

    def test_github_tree_branch_with_slash(self):
        raw = self.archive(entries={"repo-feature-new/skills/selected/SKILL.md": SKILL,
                                    "repo-feature-new/other/SKILL.md": SKILL}).read_bytes()
        session = Session([], gets=[Response(status=404), Response(status=404), Response(raw)])
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.113.3", 443))]
        with patch("trae_manager.configs.requests.Session", return_value=session), patch("trae_manager.configs.socket.getaddrinfo", return_value=public):
            installed = self.manager.import_github_skill("https://github.com/example/repo/tree/feature/new/skills/selected")
        self.assertEqual([path.name for path in installed], ["selected"])
        self.assertIn("feature%2Fnew.zip", session.calls[-1][1])

    def test_github_deep_tree_has_six_candidate_limit_and_main_fallback(self):
        subtree = "a/b/c/d/e/f/selected"
        raw = self.archive(entries={f"repo-main/{subtree}/SKILL.md": SKILL}).read_bytes()
        session = Session([], gets=[*(Response(status=404) for _ in range(5)), Response(raw)])
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.113.3", 443))]
        with patch("trae_manager.configs.requests.Session", return_value=session), patch("trae_manager.configs.socket.getaddrinfo", return_value=public):
            installed = self.manager.import_github_skill("https://github.com/example/repo/tree/main/" + subtree)
        self.assertEqual(len(session.calls), 6)
        self.assertEqual(installed[0].name, "selected")

    def test_github_tree_without_selected_skill_does_not_install_other_skills(self):
        raw = self.archive(entries={"repo-main/selected/README.md": "not a skill",
                                    "repo-main/other/SKILL.md": SKILL}).read_bytes()
        session = Session([], gets=[Response(status=404), Response(raw)])
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.113.3", 443))]
        with patch("trae_manager.configs.requests.Session", return_value=session), patch("trae_manager.configs.socket.getaddrinfo", return_value=public), self.assertRaises(ValueError):
            self.manager.import_github_skill("https://github.com/example/repo/tree/main/selected")
        self.assertFalse((self.manager.global_home / "skills").exists())

    def test_github_tree_ref_attempts_are_bounded(self):
        session = Session([], gets=[Response(status=404) for _ in range(6)])
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.113.3", 443))]
        with patch("trae_manager.configs.requests.Session", return_value=session), patch("trae_manager.configs.socket.getaddrinfo", return_value=public), self.assertRaises(RuntimeError):
            self.manager.import_github_skill("https://github.com/example/repo/tree/main/a/b/c/d/e/f/g")
        self.assertEqual(len(session.calls), 6)

    def test_github_tree_encoded_traversal_rejected_before_network(self):
        with patch("trae_manager.configs.requests.Session") as session, self.assertRaises(ValueError):
            self.manager.import_github_skill("https://github.com/example/repo/tree/main/%2e%2e/private")
        session.assert_not_called()

    def test_reads_and_imports_never_start_a_process(self):
        with patch("trae_manager.configs.subprocess.Popen") as popen:
            self.seed()
            self.manager.list_mcp(self.mcp)
            self.manager.import_skill(self.archive())
            popen.assert_not_called()

    def test_stdio_probe_mocked_initialize_and_tools(self):
        class Process:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}) + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "test_tool"}]}}) + "\n")
                self.pid = 999999

            def wait(self, timeout=None):
                return 0

        process = Process()
        with patch("trae_manager.configs.subprocess.Popen", return_value=process) as popen:
            result = self.manager.probe_mcp({"command": "fake-server", "env": {"TOKEN": "not-logged"}})
        self.assertTrue(result["success"])
        self.assertEqual(result["tools"], ["test_tool"])
        self.assertFalse(popen.call_args.kwargs.get("shell", False))
        self.assertNotIn("not-logged", json.dumps(result))

    def test_stdio_probe_error_does_not_leak_process_details(self):
        with patch("trae_manager.configs.subprocess.Popen", side_effect=OSError("secret-token-123")):
            result = self.manager.probe_mcp({"command": "fake-server"})
        self.assertFalse(result["ok"])
        self.assertNotIn("secret-token-123", json.dumps(result))

    def test_http_probe_mocked_initialize_and_tools(self):
        responses = [Response(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}).encode(), headers={"Content-Type": "application/json", "Mcp-Session-Id": "session"}), Response(status=202), Response(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "http_tool"}]}}).encode())]
        session = Session(responses)
        with patch("trae_manager.configs.requests.Session", return_value=session):
            result = self.manager.probe_mcp({"url": "https://example.invalid/mcp", "headers": {"Authorization": "Bearer private-token"}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["tool_count"], 1)
        self.assertEqual(session.calls[1][2]["headers"]["Mcp-Session-Id"], "session")
        self.assertNotIn("private-token", json.dumps(result))

    def test_legacy_sse_probe_mocked(self):
        stream = Response(headers={"Content-Type": "text/event-stream"}, lines=["event: endpoint", "data: /message?session=1", "", "event: message", 'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}', "", "event: message", 'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}', ""])
        session = Session([Response(status=202), Response(status=202), Response(status=202)], gets=[stream])
        with patch("trae_manager.configs.requests.Session", return_value=session):
            result = self.manager.probe_mcp({"url": "https://example.invalid/sse", "type": "sse"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["tools"], [])

    def test_sse_cross_origin_endpoint_rejected(self):
        stream = Response(headers={"Content-Type": "text/event-stream"}, lines=["event: endpoint", "data: https://elsewhere.invalid/message", ""])
        session = Session([], gets=[stream])
        with patch("trae_manager.configs.requests.Session", return_value=session):
            result = self.manager.probe_mcp({"url": "https://example.invalid/sse", "type": "sse"})
        self.assertFalse(result["ok"])
        self.assertEqual(len(session.calls), 1)

    def test_http_sse_message_parser(self):
        response = Response(headers={"Content-Type": "text/event-stream"}, lines=[": heartbeat", "data: malformed", "", 'data: {"id":2,"result":{"tools":[]}}', ""])
        self.assertEqual(self.manager._http_message(response, 2), {"tools": []})

    def test_http_absolute_deadline_for_stalled_stream(self):
        unblock = threading.Event()

        class StalledResponse(Response):
            def iter_content(self, **kwargs):
                unblock.wait(1)
                yield b"{}"

        started = time.monotonic()
        try:
            with self.assertRaises(RuntimeError):
                self.manager._http_message(StalledResponse(), 1, time.monotonic() + 0.03)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            unblock.set()

    def test_http_automatic_legacy_sse_fallback(self):
        stream = Response(headers={"Content-Type": "text/event-stream"}, lines=["event: endpoint", "data: /message", "", 'data: {"id":1,"result":{"protocolVersion":"2024-11-05"}}', "", 'data: {"id":2,"result":{"tools":[]}}', ""])
        session = Session([Response(status=405), Response(status=202), Response(status=202), Response(status=202)], gets=[stream])
        with patch("trae_manager.configs.requests.Session", return_value=session):
            result = self.manager.probe_mcp({"url": "https://example.invalid/sse"})
        self.assertTrue(result["ok"])

    def test_io_errors_are_business_exceptions(self):
        with patch.object(Path, "read_bytes", side_effect=PermissionError("private-path")), self.assertRaises(RuntimeError):
            self.manager.read_mcp(self.mcp)

    def test_cannot_restore_foreign_backup(self):
        with self.assertRaises(ValueError):
            self.manager.restore_backup(self.root / "foreign.json")


if __name__ == "__main__":
    unittest.main()
