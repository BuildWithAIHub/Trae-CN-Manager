"""Build the single transferable setup EXE from an already built portable app."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import zipfile


MANIFEST = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity type="win32" name="TraeCNManager.Setup" version="1.0.0.0"/>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
    <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
  </requestedPrivileges></security></trustInfo>
  <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
    <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
    <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
  </windowsSettings></application>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application>
    <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
  </application></compatibility>
</assembly>'''


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from trae_manager import __version__
    source = root / "dist" / "TraeCNManager"
    if not (source / "TraeCNManager.exe").is_file():
        raise SystemExit("Run python scripts/build.py first")
    build = root / "build" / "installer"
    build.mkdir(parents=True, exist_ok=True)
    manifest = build / "setup.manifest"
    manifest.write_text(MANIFEST, encoding="utf-8")
    payload = build / "app-payload.zip"
    files = {}
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if relative.parts[0].lower() in {"data", ".updates"} or path.suffix.lower() in {".vault", ".key", ".tcmbackup", ".log"}:
                raise ValueError("Release directory contains private/runtime data")
            if path.is_symlink():
                raise ValueError("Release directory contains a link")
            name = relative.as_posix()
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            archive.write(path, name)
        archive.writestr("payload-manifest.json", json.dumps({"version": __version__, "files": files}))
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed", "--noupx", "--name", f"TraeCNManager-{__version__}-Setup-x64", "--distpath", str(root / "dist"), "--workpath", str(build / "pyinstaller"), "--specpath", str(build), "--paths", str(root), "--add-data", f"{payload};.", "--exclude-module", "PySide6", "--exclude-module", "cryptography", "--exclude-module", "pytest", "--exclude-module", "numpy"]
    assets = root / "assets"
    if not (assets / "app.ico").exists() or not (assets / "app.png").exists():
        raise SystemExit("Icon assets are missing. Run python scripts/build.py first")
    command.extend(["--manifest", str(manifest), "--icon", str(assets / "app.ico"),
                    "--add-data", f"{assets / 'app.ico'};assets",
                    "--add-data", f"{assets / 'app.png'};assets"])
    command.append(str(root / "installer" / "setup.py"))
    subprocess.run(command, cwd=root, check=True)


if __name__ == "__main__":
    main()
