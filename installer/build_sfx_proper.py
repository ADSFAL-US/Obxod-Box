#!/usr/bin/env python3
"""
7-Zip SFX Builder for Obxod-BOX.
Creates SFX with installer/, runtime/, install.bat, payload.zip inside 7z archive.
"""
import os
import sys
import shutil
import zipfile
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class SevenZipSFXBuilder:
    """Builds SFX with installer/, runtime/, install.bat, payload.zip inside 7z archive."""

    def __init__(self, source_dir: Path, output_dir: Path, version: str = "1.0.0"):
        self.source_dir = source_dir.resolve()
        self.output_dir = output_dir.resolve()
        self.version = version
        self.app_name = "Obxod-BOX"
        self.seven_zip = Path(r"C:\Program Files\7-Zip\7z.exe")
        self.sfx_module = Path(__file__).parent / "7zsd_All.sfx"

    def check_7zip(self) -> bool:
        return self.seven_zip.exists() and self.sfx_module.exists()

    def build_payload(self, payload_path: Path) -> Path:
        """Create payload.zip with FINAL APPLICATION only (no installer, no runtime, no binaries, no user configs)."""
        print(f"Payload: {payload_path}")
        # Only include application source code and assets - NOT bin/, cfg/, geo/, runtime/
        # These are downloaded/created on first run per .buildignore rules
        items = ["main.py","main_window.py","config.py","net_utils.py",
            "network_watcher.py","platform_info.py","platform_tray.py",
            "xray_manager.py","xray_supervisor.py","loading_window.py",
            "start.bat","start.sh","requirements.txt",
            "assets","version.py"]
        skip = {"__pycache__",".pyc",".pyo",".pyd",".venv","venv","build","build-bundle",
            ".git",".idea",".vscode","logs",".log","mkbuild.ps1",".buildignore",
            "bin","cfg","geo","runtime"}  # Explicitly exclude these dirs
        with zipfile.ZipFile(payload_path, 'w', zipfile.ZIP_DEFLATED, 9) as zf:
            for it in items:
                ip = self.source_dir / it
                if ip.is_file():
                    zf.write(ip, it); print(f"  + {it}")
                elif ip.is_dir():
                    for f in ip.rglob("*"):
                        if f.is_file():
                            rel = f.relative_to(self.source_dir)
                            if any(s in str(rel) for s in skip): continue
                            zf.write(f, str(rel).replace('\\', '/')); print(f"  + {rel}")
        print(f"Payload: {payload_path} ({payload_path.stat().st_size/1024/1024:.1f} MB)")
        return payload_path

    def config(self) -> str:
        return f''';!@Install@!UTF-8!
Title="{self.app_name} Setup v{self.version}"
RunProgram="install.bat"
AutoInstall="install.bat --silent"
GUIMode="2"
OverwriteMode="2"
InstallPath="%TEMP%\\{self.app_name.lower()}-install"
;!@InstallEnd@!

;!@Install@!UTF-8:Language:1049@!
Title="Установка {self.app_name} v{self.version}"
;!@InstallEnd@:Language:1049@!
'''

    def build(self) -> Path:
        if not self.check_7zip():
            raise RuntimeError(f"7-Zip not found or 7zsd_All.sfx missing")

        print(f"Building {self.app_name} SFX v{self.version} (7-Zip SFX)")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        payload = self.output_dir / f"payload-v{self.version}.zip"
        sfx = self.output_dir / f"{self.app_name}-Setup-v{self.version}.exe"

        try:
            # 1. Create payload.zip with FINAL APPLICATION only
            self.build_payload(payload)

            # 2. Create temp directory with installer/, runtime/, install.bat, payload.zip
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)

                # Copy installer directory
                shutil.copytree(self.source_dir / "installer", tmp_path / "installer")

                # Copy runtime directory
                shutil.copytree(self.source_dir / "runtime", tmp_path / "runtime")

                # Copy install.bat to ROOT of archive
                shutil.copy2(self.source_dir / "installer" / "install.bat", tmp_path / "install.bat")

                # Copy payload.zip
                shutil.copy2(payload, tmp_path / "payload.zip")

                # Create 7z archive with installer/, runtime/, install.bat, payload.zip
                archive_path = self.output_dir / f"archive-v{self.version}.7z"
                r = subprocess.run([
                    str(self.seven_zip), "a",
                    str(archive_path),
                    str(tmp_path / "installer"),
                    str(tmp_path / "runtime"),
                    str(tmp_path / "install.bat"),
                    str(tmp_path / "payload.zip")
                ], capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(f"7z failed: {r.stderr}")

                # Verify archive contents
                r = subprocess.run([
                    str(self.seven_zip), "l", str(archive_path)
                ], capture_output=True, text=True)
                print(f"Archive contents:\n{r.stdout}")

                # Now prepend SFX module + config + archive
                sfx_module_data = self.sfx_module.read_bytes()
                config_data = self.config().encode('utf-8')
                archive_data = (self.output_dir / f"archive-v{self.version}.7z").read_bytes()

                sfx = self.output_dir / f"{self.app_name}-Setup-v{self.version}.exe"
                with open(sfx, 'wb') as f:
                    f.write(self.sfx_module.read_bytes())
                    f.write(self.config().encode('utf-8'))
                    f.write((self.output_dir / f"archive-v{self.version}.7z").read_bytes())

                print(f"SFX: {sfx} ({sfx.stat().st_size/1024/1024:.1f} MB)")
                return sfx
        finally:
            if payload.exists(): payload.unlink()
            archive_path = self.output_dir / f"archive-v{self.version}.7z"
            if archive_path.exists(): archive_path.unlink()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("-s","--source",type=Path,default=".")
    ap.add_argument("-o","--output",type=Path,default="./build-bundle")
    ap.add_argument("-v","--version",default="1.0.0")
    a = ap.parse_args()
    b = SevenZipSFXBuilder(a.source, a.output, a.version)
    print(f"Done: {b.build()}")


if __name__ == "__main__":
    main()