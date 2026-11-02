"""Определение платформы и capabilities приложения."""
from __future__ import annotations

import sys
from enum import Enum
from functools import lru_cache


class Platform(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    ANDROID = "android"
    IOS = "ios"
    UNKNOWN = "unknown"


@lru_cache(maxsize=1)
def get_platform() -> Platform:
    # Android/iOS via BeeWare: sys.platform is still linux/darwin, but
    # toga/android markers or env may be set. Detect common cases.
    try:
        import toga  : F401
        # Briefcase sets these
        import os
        if os.environ.get("BRIEFCASE_PLATFORM") == "android" or "ANDROID_ARGUMENT" in os.environ:
            return Platform.ANDROID
        if os.environ.get("BRIEFCASE_PLATFORM") == "iOS":
            return Platform.IOS
    except Exception:
        pass

    p = sys.platform
    if p == "win32":
        return Platform.WINDOWS
    if p == "darwin":
        return Platform.MACOS
    if p.startswith("linux"):
        return Platform.LINUX
    return Platform.UNKNOWN


def supports_system_tray() -> bool:
    """Desktop tray (NotifyIcon / StatusNotifier / NSStatusItem)."""
    return get_platform() in (Platform.WINDOWS, Platform.LINUX, Platform.MACOS)


def supports_tun() -> bool:
    """System-wide TUN/VPN interface."""
    # Mobile uses platform VPN APIs (NetworkExtension / VpnService), not raw TUN from Python.
    return get_platform() in (Platform.WINDOWS, Platform.LINUX, Platform.MACOS)


def supports_winforms_tray() -> bool:
    return get_platform() == Platform.WINDOWS


def is_mobile() -> bool:
    return get_platform() in (Platform.ANDROID, Platform.IOS)


def is_desktop() -> bool:
    return get_platform() in (Platform.WINDOWS, Platform.LINUX, Platform.MACOS)
