"""
CLI Installer for Obxod-BOX.
Used for automated testing and silent installations.
"""
import argparse
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from installer.core import (
    ObxodBoxInstaller,
    InstallConfig,
    InstallMode,
    InstallerError
)


def create_cli_parser() -> argparse.ArgumentParser:
    """Create command line argument parser."""
    parser = argparse.ArgumentParser(
        prog="obxod-box-installer",
        description="Obxod-BOX CLI Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Install to default location (C:/obxod-box)
  python -m installer.cli --payload release.zip

  # Install to custom location
  python -m installer.cli --payload release.zip --path "D:/Apps/Obxod-BOX"

  # Silent install (no prompts, no shortcuts)
  python -m installer.cli --payload release.zip --silent --no-shortcuts

  # Install with desktop shortcut only
  python -m installer.cli --payload release.zip --desktop-shortcut --no-start-menu-shortcut
        """
    )

    parser.add_argument(
        "--payload", "-p",
        type=Path,
        required=True,
        help="Path to the payload ZIP archive"
    )

    parser.add_argument(
        "--path", "-d",
        type=Path,
        default=Path("C:/obxod-box"),
        help="Installation directory (default: C:/obxod-box)"
    )

    parser.add_argument(
        "--silent", "-s",
        action="store_true",
        help="Silent installation (no prompts, minimal output)"
    )

    parser.add_argument(
        "--no-shortcuts",
        action="store_true",
        help="Don't create any shortcuts"
    )

    parser.add_argument(
        "--desktop-shortcut",
        action="store_true",
        default=True,
        help="Create desktop shortcut (default: True)"
    )

    parser.add_argument(
        "--no-desktop-shortcut",
        action="store_false",
        dest="desktop_shortcut",
        help="Don't create desktop shortcut"
    )

    parser.add_argument(
        "--start-menu-shortcut",
        action="store_true",
        default=True,
        help="Create Start Menu shortcut (default: True)"
    )

    parser.add_argument(
        "--no-start-menu-shortcut",
        action="store_false",
        dest="start_menu_shortcut",
        help="Don't create Start Menu shortcut"
    )

    parser.add_argument(
        "--run-after",
        action="store_true",
        help="Launch application after installation"
    )

    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify existing installation, don't install"
    )

    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Uninstall Obxod-BOX from the specified path"
    )

    parser.add_argument(
        "--version", "-v",
        action="version",
        version="Obxod-BOX Installer 1.0.0"
    )

    return parser


def print_progress(percent: int, message: str):
    """Progress callback for CLI."""
    if percent % 10 == 0 or percent == 100:
        # Use ASCII characters for Windows compatibility
        print(f"[{percent:3d}%] {message}")


def verify_installation(install_path: Path) -> bool:
    """Verify an existing installation."""
    print(f"Verifying installation at: {install_path}")

    # Check for main.py (Python entry point)
    exe_path = install_path / "main.py"
    if not exe_path.exists():
        print(f"[FAIL] Main entry point not found: {exe_path}")
        return False
    print(f"[OK] Main entry point found: {exe_path}")

    # Check for Python runtime
    pythonw_exe = install_path / "runtime" / "pythonw.exe"
    if not pythonw_exe.exists():
        print("[WARN] pythonw.exe not found in runtime")
    else:
        print("[OK] pythonw.exe found")

    # Check critical files
    critical_files = [
        'main.py',
        'main_window.py',
        'config.py',
        'xray_manager.py',
    ]

    all_ok = True
    for f in critical_files:
        if (install_path / f).exists():
            print(f"[OK] Found: {f}")
        else:
            print(f"[FAIL] Missing: {f}")
            all_ok = False

    # Check assets
    assets_dir = install_path / 'assets'
    if assets_dir.exists():
        print("[OK] Assets directory found")
    else:
        print("[FAIL] Assets directory missing")
        all_ok = False

    # Check bin directory
    bin_dir = install_path / 'bin'
    if bin_dir.exists():
        print("[OK] Bin directory found")
        for f in ['xray.exe', 'wintun.dll']:
            if (bin_dir / f).exists():
                print(f"  [OK] {f}")
            else:
                print(f"  [FAIL] {f} missing")
                all_ok = False
    else:
        print("[FAIL] Bin directory missing")
        all_ok = False

    # Check runtime
    runtime_dir = install_path / 'runtime'
    if runtime_dir.exists():
        print("[OK] Runtime directory found")
    else:
        print("[FAIL] Runtime directory missing")
        all_ok = False

    # Check shortcuts
    import os
    desktop = Path(os.environ.get('USERPROFILE', '')) / 'Desktop'
    desktop_shortcut = desktop / 'Obxod-BOX.lnk'
    if desktop_shortcut.exists():
        print("[OK] Desktop shortcut found")
    else:
        print("[WARN] Desktop shortcut not found")

    start_menu = Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Obxod-BOX'
    start_menu_shortcut = start_menu / 'Obxod-BOX.lnk'
    if start_menu_shortcut.exists():
        print("[OK] Start Menu shortcut found")
    else:
        print("[WARN] Start Menu shortcut not found")

    return all_ok


def uninstall(install_path: Path) -> bool:
    """Uninstall Obxod-BOX."""
    print(f"Uninstalling from: {install_path}")

    if not install_path.exists():
        print(f"Installation directory not found: {install_path}")
        return False

    try:
        # Remove installation directory
        import shutil
        shutil.rmtree(install_path)
        print("[OK] Removed installation directory")

        # Remove shortcuts
        import os
        desktop = Path(os.environ.get('USERPROFILE', '')) / 'Desktop'
        desktop_shortcut = desktop / 'Obxod-BOX.lnk'
        if desktop_shortcut.exists():
            desktop_shortcut.unlink()
            print("[OK] Removed desktop shortcut")

        start_menu = (
            Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows'
            / 'Start Menu' / 'Programs' / 'Obxod-BOX'
        )
        if start_menu.exists():
            shutil.rmtree(start_menu)
            print("[OK] Removed Start Menu folder")

        # Remove uninstall registry entry
        try:
            import winreg
            winreg.DeleteKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Uninstall\org.obxod.box"
            )
            print("[OK] Removed uninstall registry entry")
        except ImportError:
            pass

        print("Uninstall complete!")
        return True

    except Exception as e:
        print(f"[FAIL] Uninstall failed: {e}")
        return False


def main():
    """Main CLI entry point."""
    parser = create_cli_parser()
    args = parser.parse_args()

    # Validate payload
    if not args.payload.exists():
        print(f"Error: Payload not found: {args.payload}")
        return 1

    # Handle verify-only
    if args.verify_only:
        success = verify_installation(args.path.resolve())
        return 0 if success else 1

    # Handle uninstall
    if args.uninstall:
        success = uninstall(args.path.resolve())
        return 0 if success else 1

    # Create config
    config = InstallConfig(
        install_path=args.path.resolve(),
        create_shortcut=not args.no_shortcuts,
        create_desktop_shortcut=args.desktop_shortcut,
        create_start_menu_shortcut=args.start_menu_shortcut,
        run_after_install=args.run_after,
        mode=InstallMode.CLI,
        silent=args.silent
    )

    # Create installer
    installer = ObxodBoxInstaller(config, print_progress if not args.silent else None)

    # Run installation
    print(f"Installing Obxod-BOX to: {config.install_path}")
    print(f"Payload: {args.payload}")
    print("-" * 50)

    try:
        success = installer.install(args.payload)

        print("-" * 50)
        if success:
            print("[OK] Installation completed successfully!")

            # Verify
            print("\nVerifying installation...")
            if verify_installation(config.install_path):
                print("[OK] Verification passed!")
            else:
                print("[WARN] Verification found issues")

            return 0
        else:
            print("[FAIL] Installation failed!")
            if installer.last_error:
                print(f"Reason: {installer.last_error}")
            return 1

    except InstallerError as e:
        print(f"[FAIL] Installation error: {e}")
        return 1
    except Exception as e:
        print(f"[FAIL] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
