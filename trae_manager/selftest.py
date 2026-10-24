"""Portable-build smoke checks, entirely synthetic and temp-only."""
from __future__ import annotations
from pathlib import Path
import tempfile


def run_self_test():
    from .security import Vault, encrypt_package, decrypt_package
    from .configs import ConfigManager
    from .platforms import task_xml
    from .trae import encode_native_value, decode_native_value
    from .recovery import RecoveryManager
    from .memory import MemoryManager
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="trae-manager-selftest-") as name:
            root = Path(name)
            vault = Vault(root / "vault")
            vault.create("Synthetic-Pass-2026!", {"value": "test-only"})
            with vault.transaction() as doc:
                doc["checked"] = True
            vault.lock()
            vault.unlock("Synthetic-Pass-2026!")
            assert vault.snapshot()["checked"]
            checks.append("encrypted-vault-roundtrip")
            recovery = RecoveryManager(vault)
            code = recovery.create_code()
            vault.change_password("Synthetic-Pass-2026!", "Rotated-Synthetic-Password!", companion_builder=recovery.rotation_files)
            vault.lock()
            recovery.reset(code, "Recovered-Synthetic-Password!")
            assert vault.snapshot()["checked"]
            checks.append("recovery-and-rotation-roundtrip")
            encoded = encrypt_package({"synthetic": True}, "Separate-Backup-Password!")
            assert decrypt_package(encoded, "Separate-Backup-Password!")["synthetic"]
            checks.append("encrypted-migration-roundtrip")
            config = ConfigManager(root / "global", root / "backups")
            path = config.mcp_path(profile=root / "profile")
            config.upsert_mcp(path, "synthetic", {"command": "node", "args": []})
            config.set_mcp_enabled(path, "synthetic", False)
            assert not config.list_mcp(path)[0]["enabled"]
            config.set_mcp_enabled(path, "synthetic", True)
            assert config.list_mcp(path)[0]["enabled"]
            checks.append("mcp-toggle-roundtrip")
            memory = MemoryManager(root / "global")
            memory.merge_snapshot({"scope": "local-shared", "files": [{"relative": "user_profile.md", "content": "# Synthetic\n\nTest preference.\n"}]})
            assert memory.snapshot()["files"]
            checks.append("native-memory-snapshot-merge")
            text = '{"synthetic":true}'
            assert decode_native_value(encode_native_value(text)) == text
            checks.append("native-adapter-roundtrip")
            assert "StartWhenAvailable>true" in task_xml(root, "13:47")
            checks.append("windows-task-catchup-xml")
        return {"passed": True, "checks": checks}
    except Exception as exc:
        return {"passed": False, "checks": checks, "error_type": type(exc).__name__}


def ui_smoke(controller, destination):
    import json
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtGui import QFontDatabase
    from .ui import MainWindow, TextEditor, QFileDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    if not QFontDatabase.families():
        for filename in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
            if Path(filename).exists():
                QFontDatabase.addApplicationFont(filename)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    window = MainWindow(controller)
    window.show()
    def capture():
        checks = []
        try:
            window.grab().save(str(destination / "unlock.png"))
            for theme in ("dark", "light"):
                window.apply_theme(theme)
                editor = TextEditor("新建规则", "code-style", "# 规则\n\n默认使用中文。\n", parent=window)
                message = QMessageBox(window)
                message.setWindowTitle("操作完成")
                message.setText("已保存。")
                chooser = QFileDialog(window, "选择配置文件", str(destination))
                chooser.setOption(QFileDialog.Option.DontUseNativeDialog, True)
                for kind, dialog in (("editor", editor), ("message", message), ("file_chooser", chooser)):
                    dialog.show()
                    app.processEvents()
                    themed = bool(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
                    bar = getattr(dialog, "_trae_title_bar", None)
                    assert themed and bar is not None and bar.isVisible()
                    assert bar.window_ref is dialog
                    if kind == "editor":
                        dialog.grab().save(str(destination / ("editor-" + theme + ".png")))
                    checks.append({"kind": kind, "theme": theme, "custom_caption": themed})
                    # Exercise the same callback as the visible top-right X.
                    bar.window_ref.close()
                    app.processEvents()
                    assert not dialog.isVisible()
                    dialog.deleteLater()
            (destination / "dialogs.json").write_text(json.dumps({"passed": True, "checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
            app.quit()
        except Exception as exc:
            (destination / "dialogs.json").write_text(json.dumps({"passed": False, "error_type": type(exc).__name__, "checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
            app.exit(1)
    QTimer.singleShot(1500, capture)
    return app.exec()
