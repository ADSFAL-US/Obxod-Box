"""
Obxod-BOX Installer Package
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from version import __version__  # noqa: F401

__author__ = "Obxod-BOX Team"