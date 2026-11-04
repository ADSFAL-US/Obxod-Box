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
        self.seven_zip = self._find_7zip()
        self.sfx_module = Path(__file__).parent / "7zsd_All.sfx"

    @staticmethod
    def _find_7zip() -> Path:
        """Locate 7z.exe: PATH first, then the default install locations."""
        found = shutil.which("7z") or shutil.which("7z.exe")
        if found:
            return Path(found)
        for candidate in (
            Path(r"C:\Program Files\7-Zip\7z.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
        ):
            if candidate.exists():
                return candidate
        return Path("7z.exe")

    def check_7zip(self) -> bool:
        return self.seven_zip.exists() and self.sfx_module.exists()

    def build_payload(self, payload_path: Path) -> Path:
        """Create payload.zip with FINAL APPLICATION only (no installer, no runtime, no binaries, no user configs)."""
        print(f"Payload: {payload_path}")
        # Only include application source code and assets - NOT bin/, cfg/, geo/, runtime/
        # These are downloaded/created on first run per .buildignore rules.
        # Root-level .py modules are picked up automatically so a new module can never
        # be silently left out of the installer payload.
        items = sorted(
            p.name for p in self.source_dir.glob("*.py") if p.is_file()
        ) + [
            "start.bat", "start.sh", "requirements.txt", "assets",
        ]
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
        # 7zSD accepts exactly one config block. Anything after ;!@InstallEnd@!
        # is treated as archive data and breaks extraction.
        # RunProgram/AutoInstall go through cmd.exe: CreateProcess cannot start a .bat
        # directly ("The system cannot find the file specified"). The working directory
        # of RunProgram is the extraction directory, so a bare name is enough.
        # The trailing space matters: 7zSD appends command-line arguments to the
        # AutoInstall string verbatim, so without it "-ai --install-dir X" would
        # produce "--silent--install-dir X".
        # SelfDelete removes the SFX executable after a successful run (auto-update).
        return (
            ";!@Install@!UTF-8!\r\n"
            f'Title="{self.app_name} Setup v{self.version}"\r\n'
            'RunProgram="cmd.exe /c install.bat "\r\n'
            'AutoInstall="cmd.exe /c install.bat --silent "\r\n'
            'GUIMode="2"\r\n'
            'OverwriteMode="2"\r\n'
            'SelfDelete="1"\r\n'
            f'InstallPath="%TEMP%\\{self.app_name.lower()}-install"\r\n'
            ";!@InstallEnd@!\r\n"
        )

    def build(self) -> Path:
        if not self.check_7zip():
            raise RuntimeError("7-Zip not found or 7zsd_All.sfx missing")

        print(f"Building {self.app_name} SFX v{self.version} (7-Zip SFX)")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        payload = self.output_dir / f"payload-v{self.version}.zip"
        archive_path = self.output_dir / f"archive-v{self.version}.7z"
        sfx = self.output_dir / f"{self.app_name}-Setup-v{self.version}.exe"

        try:
            # 1. Create payload.zip with FINAL APPLICATION only
            self.build_payload(payload)

            # 2. Create temp directory with installer/, runtime/, install.bat, payload.zip
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)

                shutil.copytree(self.source_dir / "installer", tmp_path / "installer")
                shutil.copytree(self.source_dir / "runtime", tmp_path / "runtime")
                shutil.copy2(self.source_dir / "installer" / "install.bat", tmp_path / "install.bat")
                shutil.copy2(payload, tmp_path / "payload.zip")

                # 3. Create 7z archive. Run from tmp_path with relative names so the
                #    archive root is exactly installer/, runtime/, install.bat, payload.zip.
                #    -mf=off is required: 7-Zip >= 23 auto-applies BCJ/ARM64 filters,
                #    which the 2016-era 7zSD SFX module cannot decode ("unsupported method").
                r = subprocess.run([
                    str(self.seven_zip), "a", "-t7z",
                    str(archive_path),
                    "installer", "runtime", "install.bat", "payload.zip",
                    "-m0=LZMA2", "-mx=5", "-mf=off", "-bso0", "-bsp0",
                ], cwd=str(tmp_path), capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError(f"7z failed: {r.stderr or r.stdout}")

                # 4. Prepend SFX module + config to the archive
                with open(sfx, "wb") as f:
                    f.write(self.sfx_module.read_bytes())
                    f.write(self.config().encode("utf-8"))
                    f.write(archive_path.read_bytes())

                print(f"SFX: {sfx} ({sfx.stat().st_size / 1024 / 1024:.1f} MB)")
                return sfx
        finally:
            for tmp_file in (payload, archive_path):
                if tmp_file.exists():
                    tmp_file.unlink()


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