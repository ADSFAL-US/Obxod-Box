"""Кроссплатформенный system tray.

Windows: WinForms NotifyIcon (pythonnet)
Linux:   заглушка с логом (StatusNotifierItem можно добавить позже)
macOS:   заглушка
Android/iOS: нет system tray — no-op, hide = background app

API:
  tray = create_tray(callbacks)
  tray.install()
  tray.dispose()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from platform_info import Platform, get_platform

logger = logging.getLogger(__name__)


@dataclass
class TrayCallbacks:
    on_open: Callable[[], None]
    on_exit: Callable[[], None]


class TrayBackend(Protocol):
    def install(self) -> bool: ...
    def dispose(self) -> None: ...
    @property
    def installed(self) -> bool: ...


class NoOpTray:
    """Mobile / unsupported: нет иконки в трее."""

    def __init__(self, callbacks: TrayCallbacks):
        self._callbacks = callbacks
        self._installed = False

    def install(self) -> bool:
        logger.info("Tray: no-op backend (platform=%s)", get_platform().value)
        self._installed = True
        return True

    def dispose(self) -> None:
        self._installed = False

    @property
    def installed(self) -> bool:
        return self._installed


class WinFormsTray:
    """Windows NotifyIcon через pythonnet."""

    def __init__(self, callbacks: TrayCallbacks, icon_path: str | None = None):
        self._callbacks = callbacks
        self._icon_path = icon_path
        self._tray = None
        self._menu = None
        self._installed = False

    def install(self) -> bool:
        if self._installed:
            return True
        try:
            import clr  # type: ignore
            clr.AddReference("System.Drawing")
            clr.AddReference("System.Windows.Forms")
            from System.Drawing import Icon as DrawingIcon  # type: ignore
            from System.Drawing import SystemIcons  # type: ignore
            from System.Windows.Forms import (  # type: ignore
                ContextMenuStrip, ToolStripMenuItem, NotifyIcon, MouseButtons,
            )
        except Exception as e:
            logger.warning("WinForms tray unavailable: %s", e)
            return False

        try:
            # ContextMenu/MenuItem — legacy API: в .NET Core они бросают
            # PlatformNotSupportedException. ContextMenuStrip работает и там,
            # и на .NET Framework.
            menu = ContextMenuStrip()
            item_open = ToolStripMenuItem("Открыть")
            item_open.Click += lambda s, e: self._callbacks.on_open()
            item_exit = ToolStripMenuItem("Выход")
            item_exit.Click += lambda s, e: self._callbacks.on_exit()
            menu.Items.Add(item_open)
            menu.Items.Add(item_exit)
            self._menu = menu

            tray = NotifyIcon()
            try:
                if self._icon_path:
                    tray.Icon = DrawingIcon(self._icon_path)
                else:
                    tray.Icon = SystemIcons.Application
            except Exception:
                tray.Icon = SystemIcons.Application

            tray.Text = "Obxod-BOX"
            tray.ContextMenuStrip = menu
            tray.DoubleClick += lambda s, e: self._callbacks.on_open()

            def _on_mouse_up(sender, event):
                try:
                    if event.Button == MouseButtons.Left:
                        self._callbacks.on_open()
                except Exception as ex:
                    logger.debug("tray mouse up: %s", ex)

            tray.MouseUp += _on_mouse_up
            tray.Visible = True
            self._tray = tray
            self._installed = True
            logger.info("WinForms tray installed")
            return True
        except Exception as e:
            logger.warning("WinForms tray install failed: %s", e, exc_info=True)
            self.dispose()
            return False

    def dispose(self) -> None:
        tray, menu = self._tray, self._menu
        self._tray = self._menu = None
        self._installed = False
        if tray is not None:
            try:
                tray.Visible = False
            except Exception as exc:
                logger.debug("dispose: %s", exc)
            try:
                tray.Dispose()
            except Exception as exc:
                logger.debug("dispose: %s", exc)
        if menu is not None:
            try:
                menu.Dispose()
            except Exception as exc:
                logger.debug("dispose: %s", exc)

    @property
    def installed(self) -> bool:
        return self._installed


class LinuxTray:
    """Linux StatusNotifier / AppIndicator через PyGObject.

    Пробует по очереди:
      1. AyatanaAppIndicator3
      2. AppIndicator3
      3. Gtk.StatusIcon (legacy)
    Если ничего нет — no-op (hide окна всё равно работает).
    """

    def __init__(self, callbacks: TrayCallbacks, icon_path: str | None = None):
        self._callbacks = callbacks
        self._icon_path = icon_path
        self._installed = False
        self._indicator = None
        self._menu = None
        self._status_icon = None
        self._backend = None  # "ayatana" | "appindicator" | "statusicon"

    def install(self) -> bool:
        if self._installed:
            return True

        if self._try_appindicator("AyatanaAppIndicator3"):
            self._backend = "ayatana"
            self._installed = True
            logger.info("Linux tray: AyatanaAppIndicator3")
            return True
        if self._try_appindicator("AppIndicator3"):
            self._backend = "appindicator"
            self._installed = True
            logger.info("Linux tray: AppIndicator3")
            return True
        if self._try_status_icon():
            self._backend = "statusicon"
            self._installed = True
            logger.info("Linux tray: Gtk.StatusIcon (legacy)")
            return True

        logger.warning(
            "Linux tray: нет AppIndicator/StatusIcon. "
            "Установите: libayatana-appindicator / libappindicator "
            "(пакет python-gi + gir1.2-ayatanaappindicator3-0.1). "
            "Hide-to-tray окна всё равно работает."
        )
        self._installed = True  # no-op, но callbacks зарегистрированы
        return True

    def _resolve_icon(self) -> str:
        """Путь к иконке или имя theme-icon."""
        if self._icon_path:
            from pathlib import Path
            p = Path(self._icon_path)
            if p.is_file():
                return str(p.resolve())
        # theme fallback
        return "network-workgroup"

    def _try_appindicator(self, module: str) -> bool:
        try:
            import gi
            gi.require_version(module, "0.1")
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk  # type: ignore
            import importlib
            mod = importlib.import_module(f"gi.repository.{module}")
        except Exception as e:
            logger.debug("AppIndicator %s unavailable: %s", module, e)
            return False

        try:
            from pathlib import Path

            menu = Gtk.Menu()
            item_open = Gtk.MenuItem(label="Открыть")
            item_open.connect("activate", lambda *_: self._callbacks.on_open())
            item_exit = Gtk.MenuItem(label="Выход")
            item_exit.connect("activate", lambda *_: self._callbacks.on_exit())
            menu.append(item_open)
            menu.append(item_exit)
            menu.show_all()
            self._menu = menu

            icon = self._resolve_icon()
            cat = mod.IndicatorCategory.APPLICATION_STATUS
            icon_path = Path(icon)
            if icon_path.is_file():
                ind = mod.Indicator.new(
                    "obxod-box",
                    str(icon_path.resolve()),
                    cat,
                )
                try:
                    ind.set_icon_full(str(icon_path.resolve()), "Obxod")
                except Exception:
                    try:
                        ind.set_icon(str(icon_path.resolve()))
                    except Exception as exc:
                        logger.debug("_try_appindicator: %s", exc)
            else:
                ind = mod.Indicator.new("obxod-box", icon, cat)

            ind.set_status(mod.IndicatorStatus.ACTIVE)
            ind.set_menu(menu)
            try:
                ind.set_title("Obxod-BOX")
            except Exception as exc:
                logger.debug("_try_appindicator: %s", exc)

            self._indicator = ind
            return True
        except Exception as e:
            logger.debug("AppIndicator %s install failed: %s", module, e)
            self._indicator = None
            self._menu = None
            return False

    def _try_status_icon(self) -> bool:
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk  # type: ignore
        except Exception as e:
            logger.debug("Gtk StatusIcon unavailable: %s", e)
            return False

        try:
            # StatusIcon deprecated but still works on X11
            if not hasattr(Gtk, "StatusIcon"):
                return False
            icon = Gtk.StatusIcon()
            path = self._resolve_icon()
            from pathlib import Path
            if Path(path).is_file():
                icon.set_from_file(path)
            else:
                icon.set_from_icon_name(path)
            icon.set_tooltip_text("Obxod-BOX")
            icon.set_visible(True)

            menu = Gtk.Menu()
            item_open = Gtk.MenuItem(label="Открыть")
            item_open.connect("activate", lambda *_: self._callbacks.on_open())
            item_exit = Gtk.MenuItem(label="Выход")
            item_exit.connect("activate", lambda *_: self._callbacks.on_exit())
            menu.append(item_open)
            menu.append(item_exit)
            menu.show_all()
            self._menu = menu

            def on_activate(_icon):
                self._callbacks.on_open()

            def on_popup(_icon, button, time):
                menu.popup(None, None, None, None, button, time)

            icon.connect("activate", on_activate)
            icon.connect("popup-menu", on_popup)
            self._status_icon = icon
            return True
        except Exception as e:
            logger.debug("StatusIcon install failed: %s", e)
            self._status_icon = None
            self._menu = None
            return False

    def dispose(self) -> None:
        self._installed = False
        try:
            if self._indicator is not None:
                try:
                    from gi.repository import AyatanaAppIndicator3 as _A  # type: ignore
                    self._indicator.set_status(_A.IndicatorStatus.PASSIVE)
                except Exception:
                    try:
                        from gi.repository import AppIndicator3 as _A  # type: ignore
                        self._indicator.set_status(_A.IndicatorStatus.PASSIVE)
                    except Exception as exc:
                        logger.debug("dispose: %s", exc)
        except Exception as exc:
            logger.debug("dispose: %s", exc)
        self._indicator = None
        if self._status_icon is not None:
            try:
                self._status_icon.set_visible(False)
            except Exception as exc:
                logger.debug("dispose: %s", exc)
            self._status_icon = None
        self._menu = None

    @property
    def installed(self) -> bool:
        return self._installed


def create_tray(
    callbacks: TrayCallbacks,
    icon_path: str | None = None,
) -> TrayBackend:
    plat = get_platform()
    if plat == Platform.WINDOWS:
        return WinFormsTray(callbacks, icon_path=icon_path)
    if plat == Platform.LINUX:
        return LinuxTray(callbacks, icon_path=icon_path)
    if plat == Platform.MACOS:
        return LinuxTray(callbacks, icon_path=icon_path)  # same stub for now
    return NoOpTray(callbacks)
