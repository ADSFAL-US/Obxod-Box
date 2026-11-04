#!/usr/bin/env python3
"""
Build script for Obxod-BOX Installer.
Builds the SFX installer and places it in ./build-bundle/
"""
import sys
import subprocess
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

def build_installer(version: str = "1.0.0", use_pure_python: bool = True):
    """Build the installer."""
    print(f"Building Obxod-BOX Installer v{version}")
    print("=" * 50)

    script_dir = Path(__file__).parent
    builder_script = script_dir / "build_sfx_proper.py"

    if not builder_script.exists():
        print(f"Builder script not found: {builder_script}")
        return False
    
    cmd = [
        sys.executable, str(builder_script),
        "--version", version,
        "--output", "./build-bundle",
        "--source", "."
    ]
    
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    
    if result.returncode == 0:
        print("=" * 50)
        print("Installer build successful!")
        return True
    else:
        print("=" * 50)
        print("Installer build failed!")
        return False


def test_installer(install_path: str = "C:/obxod-box"):
    """Test the installer by installing to the specified path."""
    print(f"Testing installer at: {install_path}")
    print("=" * 50)
    
    # Find the latest payload
    bundle_dir = Path("./build-bundle")
    payloads = list(bundle_dir.glob("payload-v*.zip"))
    
    if not payloads:
        print("No payload found. Build the installer first.")
        return False
    
    # Use the latest payload
    latest_payload = max(payloads, key=lambda p: p.stat().st_mtime)
    print(f"Using payload: {latest_payload}")
    
    # Run CLI installer
    cmd = [
        sys.executable, "-m", "installer.cli",
        "--payload", str(latest_payload),
        "--path", install_path,
        "--desktop-shortcut",
        "--start-menu-shortcut",
        "--run-after"
    ]
    
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    
    if result.returncode == 0:
        print("=" * 50)
        print("Installation test successful!")
        
        # Verify
        verify_cmd = [
            sys.executable, "-m", "installer.cli",
            "--verify-only",
            "--path", install_path
        ]
        print(f"Verifying...")
        subprocess.run(verify_cmd)
        return True
    else:
        print("=" * 50)
        print("Installation test failed!")
        return False


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Build and test Obxod-BOX Installer")
    parser.add_argument("--version", "-v", default="1.0.0", help="Version string")
    parser.add_argument("--test", "-t", action="store_true", help="Test installer after build")
    parser.add_argument("--test-path", default="C:/obxod-box", help="Test installation path")
    parser.add_argument("--pure-python", action="store_true", default=True, help="Use pure Python builder")
    parser.add_argument("--7zip", action="store_false", dest="pure_python", help="Use 7-Zip builder")
    
    args = parser.parse_args()
    
    # Build
    success = build_installer(args.version, args.pure_python)
    
    if success and args.test:
        print()
        test_installer(args.test_path)


if __name__ == "__main__":
    main()