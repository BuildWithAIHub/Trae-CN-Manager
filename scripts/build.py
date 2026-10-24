"""Build a replaceable-Qt portable directory; run on Windows x64."""
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    build = root / "build"
    build.mkdir(exist_ok=True)
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFontDatabase
    from trae_manager.brand import write_assets
    app = QApplication.instance() or QApplication([])
    if not QFontDatabase.families():
        font = Path("C:/Windows/Fonts/segoeui.ttf")
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    icon = write_assets(root / "assets")
    shutil.copy2(icon, build / "app.ico")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
               "--noupx", "--name", "TraeCNManager", "--distpath", str(root / "dist"),
               "--workpath", str(build / "pyinstaller"), "--specpath", str(build),
               "--paths", str(root), "--hidden-import", "cryptography.hazmat.bindings._rust",
               "--exclude-module", "tkinter", "--exclude-module", "matplotlib",
               "--exclude-module", "numpy", "--exclude-module", "pandas",
               "--exclude-module", "PyQt5", "--exclude-module", "PyQt6",
               "--exclude-module", "PySide2", str(root / "main.py")]
    if icon.exists():
        command[3:3] = ["--icon", str(icon)]
    subprocess.run(command, cwd=root, check=True)
    target = root / "dist" / "TraeCNManager"
    for name in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.md"):
        if (root / name).exists():
            shutil.copy2(root / name, target / name)
    if (root / "docs").is_dir():
        shutil.copytree(root / "docs", target / "docs", dirs_exist_ok=True)
    licenses = target / "licenses"
    licenses.mkdir(exist_ok=True)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, licenses / "PYTHON-LICENSE.txt")
    # The standalone setup UI uses the Tcl/Tk bundled with this same Python.
    for folder in (Path(sys.base_prefix) / "tcl").glob("*8.*"):
        terms = folder / "license.terms"
        if terms.is_file():
            shutil.copy2(terms, licenses / (folder.name + "-license.terms"))
    if (root / "licenses").is_dir():
        shutil.copytree(root / "licenses", licenses, dirs_exist_ok=True)
    import importlib.metadata
    for package in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "cryptography", "requests", "PyYAML", "PyInstaller", "certifi", "urllib3", "idna", "charset-normalizer"):
        try:
            distribution = importlib.metadata.distribution(package)
        except importlib.metadata.PackageNotFoundError:
            continue
        for entry in distribution.files or []:
            if any("license" in part.lower() or "copying" in part.lower() or "notice" in part.lower() for part in entry.parts):
                source = Path(distribution.locate_file(entry))
                if source.is_file():
                    destination = licenses / package / source.name
                    # Preserve distinct notices which have equal basenames.
                    if destination.exists():
                        destination = licenses / package / ("_".join(entry.parts[-3:]))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    print(str(target))


if __name__ == "__main__":
    main()
