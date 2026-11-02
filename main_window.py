import asyncio
import base64
import ctypes
import hashlib
import json
import logging
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import toga
from toga.colors import rgb
from toga.handlers import WeakrefCallable
from toga.style import Pack

from config import VALID_MODES
from network_watcher import NetworkEvent, NetworkState, NetworkWatcher
from platform_info import get_platform, is_desktop, is_mobile, supports_system_tray
from platform_tray import TrayCallbacks, create_tray
from xray_manager import XrayManager

logger = logging.getLogger(__name__)

BG_COLOR = (45, 45, 45)
TRAY_MIN_WIDTH = 480
TRAY_MIN_HEIGHT = 560
TRAY_DEFAULT_SIZE = (800, 600)
SUBSCRIPTION_USER_AGENT = "obxod-box/1.0"
DEFAULT_PING_URL = "https://www.gstatic.com/generate_204"

_Color = None
_DrawingIcon = None
_SystemIcons = None
_NotifyIcon = None
_ContextMenu = None
_MenuItem = None
_MouseButtons = None
_SysAction = None
_WINFORMS_OK = False

if sys.platform == "win32":
    try:
        import clr  # type: ignore

        clr.AddReference("System.Drawing")
        clr.AddReference("System.Windows.Forms")
        import System  # type: ignore
        from System.Drawing import Color as _Color  # type: ignore
        from System.Drawing import Icon as _DrawingIcon  # type: ignore
        from System.Drawing import SystemIcons as _SystemIcons  # type: ignore
        from System.Windows.Forms import (  # type: ignore
            ContextMenu as _ContextMenu,
            MenuItem as _MenuItem,
            NotifyIcon as _NotifyIcon,
            MouseButtons as _MouseButtons,
        )
        _SysAction = System.Action
        _WINFORMS_OK = True
    except Exception as _e:
        logging.getLogger(__name__).warning(
            "WinForms/pythonnet недоступны — трей отключён: %s", _e
        )


# ── HTML верстка: assets/main_ui.html + модуль куба ────────────────
# WebView грузится через set_content(about:blank, html), поэтому внешние
# <script src> / <link href> не работают. Собираем инлайн при чтении.
_UI_HTML_CACHE: str | None = None

# Порядок: core css → accent css → cube.js → registry → animation modules.
_CUBE_ASSETS: tuple[tuple[str, str], ...] = (
    ("style", "cube/cube.css"),
    ("style", "cube/animations/accents.css"),
    ("style", "cube/animations/connecting.css"),
    ("style", "cube/animations/network_down.css"),
    ("script", "cube/cube.js"),
    ("script", "cube/animations/registry.js"),
    ("script", "cube/animations/connecting.js"),
    ("script", "cube/animations/network_down.js"),
)


def _read_asset(assets_dir: Path, rel: str) -> str:
    path = assets_dir / rel
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("UI asset missing %s: %s", path, e)
        return f"/* missing {rel} */\n" if rel.endswith(".css") else f"console.warn('missing {rel}');\n"


def _assemble_cube_inject(assets_dir: Path) -> tuple[str, str]:
    """Возвращает (css_block, js_block) для вставки в HTML."""
    css_parts: list[str] = []
    js_parts: list[str] = []
    for kind, rel in _CUBE_ASSETS:
        body = _read_asset(assets_dir, rel)
        if kind == "style":
            css_parts.append(f"/* --- {rel} --- */\n{body}")
        else:
            js_parts.append(f"/* --- {rel} --- */\n{body}")
    css_block = "<style>\n" + "\n".join(css_parts) + "\n</style>\n"
    js_block = (
        "<script>\n"
        + "\n".join(js_parts)
        + "\n"
        # авто-mount после DOM; setXrayStatus / setWindowVisible — мост для Python
        + "document.addEventListener('DOMContentLoaded', function () {\n"
        + "  if (window.ObxodCube) {\n"
        + "    window.ObxodCube.mount({\n"
        + "      root: document.getElementById('cube-root'),\n"
        + "      statusDot: document.getElementById('xrayDot'),\n"
        + "      statusText: document.getElementById('xrayText')\n"
        + "    });\n"
        + "  }\n"
        + "});\n"
        + "window.setXrayStatus = function (status, message) {\n"
        + "  if (window.ObxodCube) window.ObxodCube.setStatus(status, message);\n"
        + "};\n"
        + "window.setWindowVisible = function (visible) {\n"
        + "  if (window.ObxodCube) window.ObxodCube.setWindowVisible(visible);\n"
        + "};\n"
        + "</script>\n"
    )
    return css_block, js_block


def _load_ui_html() -> str:
    """Читает main_ui.html, вшивает модуль куба. Кэширует."""
    global _UI_HTML_CACHE
    if _UI_HTML_CACHE is not None:
        return _UI_HTML_CACHE

    assets_dir = Path(__file__).resolve().parent / "assets"
    path = assets_dir / "main_ui.html"
    try:
        html = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error("Не читается %s: %s", path, e)
        _UI_HTML_CACHE = (
            "<!DOCTYPE html><html><body style='background:#2d2d2d;color:#fff;"
            "font-family:sans-serif;padding:24px'>UI не найден: "
            f"{path}</body></html>"
        )
        return _UI_HTML_CACHE

    css_block, js_block = _assemble_cube_inject(assets_dir)

    # CSS куба — перед </head>; JS — перед </body>
    if "</head>" in html:
        html = html.replace("</head>", css_block + "</head>", 1)
    else:
        html = css_block + html

    if "</body>" in html:
        html = html.replace("</body>", js_block + "</body>", 1)
    else:
        html = html + js_block

    _UI_HTML_CACHE = html
    return _UI_HTML_CACHE


def _sanitize_provider_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = name[:80].strip(" .")
    return name or f"provider_{int(time.time())}"


def _config_id(kind: str, raw) -> str:
    if kind == "xray" and isinstance(raw, dict):
        payload = json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    else:
        payload = str(raw).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


class MainWindow:
    def __init__(self, app, on_ready=None):
        self.app = app
        self.on_ready = on_ready
        self._ready_notified = False
        self._web_message_handler_installed = False
        self.config = app.config

        self._loaded_configs: list[dict] = []
        self._import_lock = asyncio.Lock()
        self._toggle_lock = asyncio.Lock()
        self._ping_lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()
        self._ping_generation = 0

        self._is_resizing: bool = False
        self._resize_timer: threading.Timer | None = None
        self._saved_normal_size: tuple[int, int] = (800, 600)
        self._was_minimized: bool = False

        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._last_js_visible: bool | None = None

        self._tray = None
        self._tray_menu = None
        self._tray_installed: bool = False
        self._tray_backend = None
        self._user_requested_exit: bool = False
        self._is_hidden_to_tray: bool = False
        self._handling_minimize: bool = False
        self._saved_bounds: tuple[int, int, int, int] | None = None

        self.xray_manager = XrayManager(app)

        self._network_watcher = NetworkWatcher(
            interval=float(self.config.get("network_watch_interval", 2)),
        )
        self._want_xray_running = False
        self._network_ok = True

        self._setting_validators = {
            "xray_loglevel": lambda v: v in ("debug", "info", "warning", "error", "none"),
            "xray_log_rotate_minutes": lambda v: isinstance(v, int) and 1 <= v <= 1440,
            "network_watch_enabled": lambda v: isinstance(v, bool),
            "network_watch_interval": lambda v: isinstance(v, (int, float)) and 1 <= v <= 60,
            "network_auto_reconnect": lambda v: isinstance(v, bool),
            "network_restart_on_iface_change": lambda v: isinstance(v, bool),
            "ping_url": lambda v: isinstance(v, str) and v.lower().startswith(("http://", "https://")) and len(v) < 2048,
            "ping_timeout": lambda v: isinstance(v, int) and 1 <= v <= 30,
            "ping_concurrency": lambda v: isinstance(v, int) and 1 <= v <= 20,
            "routing_set": lambda v: v is None or (
                isinstance(v, str)
                and 0 < len(v) <= 100
                and "/" not in v and "\\" not in v and ".." not in v
            ),
            "auto_update_enabled": lambda v: isinstance(v, bool),
        }

        window_size = (800, 600)
        self._saved_normal_size = window_size

        self.window = toga.Window(
            title="Obxod-BOX",
            size=window_size,
            resizable=True,
        )
        # GTK: до realize screen_position падает (get_window() is None)
        try:
            screens = getattr(app, "screens", None) or []
            if screens:
                screen = screens[0]
                self.window.screen_position = (
                    (screen.size.width - window_size[0]) // 2,
                    (screen.size.height - window_size[1]) // 2,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("main window center skipped: %s", exc)
        try:
            self.window.hide()
        except Exception:
            pass
        self._apply_dark_titlebar()
        try:
            self._apply_native_backcolor(self.window._impl.native)
        except Exception:
            pass

        self._install_size_guard()
        self.window.on_close = self._toga_on_close

        self.webview = toga.WebView(
            style=Pack(flex=1),
            on_webview_load=self._on_webview_load,
            on_navigation_starting=self._on_navigation_starting,
        )
        self.webview.set_content("about:blank", self._render_html())

        main_box = toga.Box(
            children=[self.webview],
            style=Pack(background_color=rgb(*BG_COLOR), flex=1),
        )
        self._apply_native_backcolor(main_box._impl.native)
        self.window.content = main_box

    # ------------------------------------------------------------------
    # Трей
    # ------------------------------------------------------------------
    def _install_tray(self) -> None:
        if self._tray_installed:
            return
        if not supports_system_tray():
            logger.info("System tray not supported on %s", get_platform().value)
            return

        assets = Path(__file__).resolve().parent / "assets"
        icon_path = assets / "icon.ico"
        if not icon_path.exists():
            # Linux/macOS: png логотип
            for candidate in ("logo500x500.png", "icon.png", "logo.png"):
                p = assets / candidate
                if p.exists():
                    icon_path = p
                    break
        callbacks = TrayCallbacks(
            on_open=lambda: self._ui(self._restore_from_tray),
            on_exit=lambda: self._ui(self._exit_from_tray),
        )
        backend = create_tray(
            callbacks,
            icon_path=str(icon_path) if icon_path.exists() else None,
        )
        ok = backend.install()
        if ok:
            self._tray_backend = backend
            self._tray_installed = True
            logger.info("Tray backend installed (%s)", type(backend).__name__)
        else:
            logger.warning("Tray backend failed to install")

    def _schedule_install_tray(self) -> None:
        """Ставит трей на UI-потоке через BeginInvoke.

        Гарантирует, что NotifyIcon окажется привязан к потоку с message
        pump, даже если Toga дёрнет on_webview_load из неглавного потока.
        """
        if self._tray_installed:
            return
        try:
            native = self.window._impl.native
            if (
                _SysAction is not None
                and getattr(native, "IsHandleCreated", False)
            ):
                native.BeginInvoke(_SysAction(self._install_tray))
                return
        except Exception as e:
            logger.debug("schedule install tray failed: %s", e)
        # Фолбэк — ставим как есть.
        self._install_tray()

    def _ui(self, fn) -> None:
        """Выполнить fn на UI-потоке главного окна (BeginInvoke)."""
        try:
            native = self.window._impl.native
            if _SysAction is not None and hasattr(native, "IsHandleCreated") and native.IsHandleCreated:
                if native.InvokeRequired:
                    native.BeginInvoke(_SysAction(fn))
                    return
            fn()
        except Exception as e:
            logger.debug("UI marshal failed, calling directly: %s", e)
            try:
                fn()
            except Exception as e2:
                logger.warning("UI callback failed: %s", e2)

    def _on_tray_mouse_up(self, sender, event) -> None:
        try:
            is_left = False
            if _MouseButtons is not None:
                is_left = event.Button == _MouseButtons.Left
            else:
                is_left = int(event.Button) in (1048576, 1)
            if is_left:
                self._ui(self._restore_from_tray)
        except Exception as e:
            logger.debug("tray mouse up: %s", e)

    def _dispose_tray(self) -> None:
        backend = self._tray_backend
        self._tray_backend = None
        self._tray = None
        self._tray_menu = None
        self._tray_installed = False
        if backend is not None:
            try:
                backend.dispose()
            except Exception as e:
                logger.debug("tray dispose: %s", e)

    def _toga_on_close(self, window, **kwargs):
        if self._user_requested_exit:
            try:
                self._dispose_tray()
            except Exception:
                pass
            return True
        self.request_hide_to_tray()
        return False

    def request_hide_to_tray(self) -> None:
        try:
            native = self.window._impl.native
            if _SysAction is not None and getattr(native, "IsHandleCreated", True):
                native.BeginInvoke(_SysAction(self._hide_to_tray))
            else:
                self._hide_to_tray()
        except Exception as e:
            logger.debug("schedule hide failed: %s", e)
            try:
                self._hide_to_tray()
            except Exception:
                pass

    def prepare_for_exit(self) -> None:
        """Вызвать перед реальным выходом (dispose tray, stop xray)."""
        self._user_requested_exit = True
        self._dispose_tray()
        # stop() ходит в сокет supervisor'а до 4с — не держим UI.
        # Супервизор сам убьёт xray, если не успеем.
        try:
            threading.Thread(
                target=self.xray_manager.stop,
                name="xray-stop-on-exit",
                daemon=True,
            ).start()
        except Exception as e:
            logger.debug("xray stop on exit: %s", e)
        try:
            native = self.window._impl.native
            try:
                native.ShowInTaskbar = True
            except Exception:
                pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Геометрия окна
    # ------------------------------------------------------------------
    def _clamp_size(self, width: int, height: int) -> tuple[int, int]:
        width = int(width) if width else 0
        height = int(height) if height else 0
        if width < TRAY_MIN_WIDTH or height < TRAY_MIN_HEIGHT:
            dw, dh = self._saved_normal_size
            if dw >= TRAY_MIN_WIDTH and dh >= TRAY_MIN_HEIGHT:
                width = max(width, dw)
                height = max(height, dh)
        if width < TRAY_MIN_WIDTH or height < TRAY_MIN_HEIGHT:
            width, height = TRAY_DEFAULT_SIZE
        return int(width), int(height)

    def _capture_bounds(self) -> tuple[int, int, int, int]:
        native = self.window._impl.native
        left = top = None
        width = height = None

        try:
            state = int(native.WindowState)
            cur_w, cur_h = int(native.Width), int(native.Height)
            cur_l, cur_t = int(native.Left), int(native.Top)
            if state == 0 and cur_w >= TRAY_MIN_WIDTH and cur_h >= TRAY_MIN_HEIGHT:
                left, top, width, height = cur_l, cur_t, cur_w, cur_h
        except Exception:
            pass

        if width is None or height is None:
            try:
                rb = native.RestoreBounds
                rw, rh = int(rb.Width), int(rb.Height)
                if rw >= TRAY_MIN_WIDTH and rh >= TRAY_MIN_HEIGHT:
                    width, height = rw, rh
                    left = int(rb.X) if left is None else left
                    top = int(rb.Y) if top is None else top
            except Exception:
                pass

        if width is None or height is None:
            try:
                sw, sh = self._saved_normal_size
                if sw >= TRAY_MIN_WIDTH and sh >= TRAY_MIN_HEIGHT:
                    width, height = int(sw), int(sh)
            except Exception:
                pass

        width, height = self._clamp_size(width or 0, height or 0)

        if left is None or top is None or left < -5000 or top < -5000:
            try:
                left, top = int(native.Left), int(native.Top)
            except Exception:
                left, top = 100, 100
        if left < -5000 or top < -5000:
            left, top = 100, 100

        return int(left), int(top), int(width), int(height)

    def _apply_bounds(self, left: int, top: int, width: int, height: int) -> None:
        width, height = self._clamp_size(width, height)
        native = self.window._impl.native
        try:
            native.WindowState = 0
        except Exception:
            pass
        # Только native.SetBounds. НЕ дублируем через self.window.size:
        # Toga переводит window.size в ClientSize, что прибавляет размер
        # рамки к текущему outer-размеру и приводит к «расползанию» окна
        # на каждом цикле hide → restore.
        try:
            native.SetBounds(int(left), int(top), int(width), int(height))
        except Exception:
            try:
                native.Width = int(width)
                native.Height = int(height)
                native.Left = int(left)
                native.Top = int(top)
            except Exception as e:
                logger.debug("native bounds apply failed: %s", e)
        self._saved_normal_size = (int(width), int(height))
        self._saved_bounds = (int(left), int(top), int(width), int(height))

    def _hide_to_tray(self) -> None:
        if self._is_hidden_to_tray:
            return
        try:
            native = self.window._impl.native
            self._saved_bounds = self._capture_bounds()
            # Защита от мусорных bounds после minimize/resize race
            try:
                l, t, w, h = self._saved_bounds
                w, h = self._clamp_size(w, h)
                self._saved_bounds = (l, t, w, h)
            except Exception:
                pass

            self._is_hidden_to_tray = True
            self._last_js_visible = False
            # JS best-effort; не блокируем hide если webview завис
            self._schedule_js_best_effort(
                "window.setWindowVisible && window.setWindowVisible(false);"
            )

            try:
                if hasattr(native, "WindowState") and int(native.WindowState) != 0:
                    native.WindowState = 0
            except Exception:
                pass

            try:
                if hasattr(native, "ShowInTaskbar"):
                    native.ShowInTaskbar = False
            except Exception:
                pass

            hidden = False
            try:
                if hasattr(native, "Hide"):
                    native.Hide()
                    hidden = True
            except Exception:
                pass
            if not hidden:
                try:
                    if hasattr(native, "Visible"):
                        native.Visible = False
                        hidden = True
                except Exception:
                    pass
            if not hidden:
                try:
                    self.window.hide()
                    hidden = True
                except Exception:
                    pass

            if not hidden:
                self._is_hidden_to_tray = False
                logger.warning("hide to tray failed — окно осталось видимым")
                return

            logger.info("Окно скрыто в трей (bounds=%s)", self._saved_bounds)
        except Exception as e:
            self._is_hidden_to_tray = False
            logger.warning("hide to tray failed: %s", e)

    def _force_foreground(self, native) -> None:
        try:
            native.TopMost = True
            native.Activate()
            native.BringToFront()
            native.Focus()
            native.TopMost = False
        except Exception:
            pass

        if sys.platform != "win32":
            return
        try:
            hwnd = int(native.Handle.ToInt64())
        except Exception:
            try:
                hwnd = int(native.Handle.ToInt32())
            except Exception:
                return
        if not hwnd:
            return

        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            SW_SHOW = 5
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_SHOW)
            user32.ShowWindow(hwnd, SW_RESTORE)

            fg = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg, None)
            cur_tid = kernel32.GetCurrentThreadId()
            attached = False
            if fg_tid and fg_tid != cur_tid:
                attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))

            user32.BringWindowToTop(hwnd)
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)

            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        except Exception as e:
            logger.debug("force foreground win32 failed: %s", e)



    # Минимальный «нормальный» размер WebView; меньше — считаем сломанным layout
    _WEBVIEW_BROKEN_PX = 120

    def _webview_is_broken(self) -> bool:
        """True если native WebView подозрительно маленький (после Hide/Show)."""
        try:
            wv = self.webview._impl.native
            w, h = int(wv.Width), int(wv.Height)
            return w < self._WEBVIEW_BROKEN_PX or h < self._WEBVIEW_BROKEN_PX
        except Exception:
            return False

    def _relayout_webview(self, *, force: bool = False) -> None:
        """Починить WebView только если он схлопнулся (или force=True).

        Dock=Fill — ок для ресайза пользователем: контрол следует за ClientSize.
        Явный SetBounds — только чтобы вытащить из stale 50×50; размеры берём
        из *текущего* ClientSize, не из сохранённых bounds окна.
        """
        if self._is_resizing and not force:
            return
        if not force and not self._webview_is_broken():
            return

        try:
            wv = self.webview._impl.native
        except Exception:
            wv = None
        if wv is None:
            return

        try:
            form = self.window._impl.native
        except Exception:
            form = None

        # Dock Fill — после этого ручной ресайз окна сам тянет WebView
        try:
            if hasattr(wv, "Dock"):
                wv.Dock = 5  # DockStyle.Fill
        except Exception as e:
            logger.debug("webview Dock=Fill failed: %s", e)

        parent = None
        try:
            parent = wv.Parent
        except Exception:
            pass

        cw = ch = None
        for src in (parent, form):
            if src is None:
                continue
            try:
                cs = src.ClientSize
                cw, ch = int(cs.Width), int(cs.Height)
                if cw >= self._WEBVIEW_BROKEN_PX and ch >= self._WEBVIEW_BROKEN_PX:
                    break
            except Exception:
                try:
                    cw, ch = int(src.Width), int(src.Height)
                    if cw >= self._WEBVIEW_BROKEN_PX and ch >= self._WEBVIEW_BROKEN_PX:
                        break
                except Exception:
                    pass

        if cw and ch and cw >= self._WEBVIEW_BROKEN_PX and ch >= self._WEBVIEW_BROKEN_PX:
            try:
                # Anchor none while setting bounds if needed, then dock again
                if hasattr(wv, "SetBounds"):
                    wv.SetBounds(0, 0, cw, ch)
                else:
                    wv.Left, wv.Top, wv.Width, wv.Height = 0, 0, cw, ch
            except Exception as e:
                logger.debug("webview SetBounds failed: %s", e)
        else:
            logger.warning("relayout: skip SetBounds, client=%sx%s", cw, ch)

        for obj in (wv, parent, form):
            if obj is None:
                continue
            for meth in ("PerformLayout",):
                try:
                    getattr(obj, meth)()
                except Exception:
                    pass
            try:
                if hasattr(obj, "Invalidate"):
                    obj.Invalidate(True)
            except Exception:
                pass

        try:
            logger.info(
                "WebView relayout (force=%s): %sx%s",
                force,
                getattr(wv, "Width", "?"),
                getattr(wv, "Height", "?"),
            )
        except Exception:
            pass

    def _cancel_relayout_timers(self) -> None:
        for attr in ("_relayout_timer1", "_relayout_timer2"):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.cancel()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _schedule_relayout_webview(self) -> None:
        """Force сразу + отложенная проверка *только если всё ещё broken*."""
        self._cancel_relayout_timers()
        try:
            self._relayout_webview(force=True)
        except Exception as e:
            logger.debug("relayout force: %s", e)

        def _deferred():
            try:
                # Не трогаем, если юзер уже ресайзит или WebView уже нормальный
                if self._is_resizing:
                    return
                if not self._webview_is_broken():
                    return
                self._ui(lambda: self._relayout_webview(force=True))
            except Exception as e:
                logger.debug("deferred relayout: %s", e)

        try:
            self._relayout_timer1 = threading.Timer(0.2, _deferred)
            self._relayout_timer1.daemon = True
            self._relayout_timer1.start()
            self._relayout_timer2 = threading.Timer(0.6, _deferred)
            self._relayout_timer2.daemon = True
            self._relayout_timer2.start()
        except Exception:
            pass

    def _restore_from_tray(self) -> None:
        try:
            native = self.window._impl.native
            bounds = self._saved_bounds or self._capture_bounds()
            left, top, width, height = bounds

            self._apply_bounds(left, top, width, height)

            try:
                if hasattr(native, "ShowInTaskbar"):
                    native.ShowInTaskbar = True
            except Exception:
                pass
            try:
                if hasattr(native, "Visible"):
                    native.Visible = True
            except Exception:
                pass
            try:
                if hasattr(native, "Show"):
                    native.Show()
            except Exception:
                pass
            try:
                self.window.show()
            except Exception:
                pass

            self._apply_bounds(left, top, width, height)

            try:
                self._apply_dark_titlebar()
            except Exception:
                pass
            try:
                if (
                    _SysAction is not None
                    and hasattr(native, "IsHandleCreated")
                    and native.IsHandleCreated
                ):
                    native.BeginInvoke(_SysAction(self._apply_dark_titlebar))
            except Exception as e:
                logger.debug("deferred dark titlebar failed: %s", e)

            try:
                if hasattr(native, "Invalidate"):
                    native.Invalidate(True)
            except Exception:
                pass

            try:
                self._force_foreground(native)
            except Exception:
                pass

            try:
                wv = self.webview._impl.native
                if wv is not None:
                    try:
                        wv.Focus()
                    except Exception:
                        pass
            except Exception:
                pass

            # WebView2 после Hide() часто залипает на крошечном размере (50×50)
            # и серый фон окна — это пустой клиент вокруг него.
            try:
                self._schedule_relayout_webview()
            except Exception as e:
                logger.debug("relayout after restore: %s", e)

            # Сначала снимаем флаг hidden, потом JS — чтобы poller снова пушил статус
            self._is_hidden_to_tray = False
            self._last_js_visible = None  # force re-sync
            self._schedule_js_best_effort(
                "window.setWindowVisible && window.setWindowVisible(true);"
            )
            # Повтор через 200ms — WebView2 часто «проглатывает» первый inject после Hide
            try:
                t = threading.Timer(
                    0.2,
                    lambda: self._schedule_js_best_effort(
                        "window.setWindowVisible && window.setWindowVisible(true);"
                    ),
                )
                t.daemon = True
                t.start()
            except Exception:
                pass

            try:
                loop = self._main_loop
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(
                        lambda: asyncio.ensure_future(self._repush_state())
                    )
            except Exception as e:
                logger.debug("re-push schedule failed: %s", e)

            logger.info("Окно восстановлено из трея (bounds=%s)", self._saved_bounds)
        except Exception as e:
            logger.warning("restore from tray failed: %s", e)
            # Не оставляем флаг в True если окно уже показали
            try:
                self._is_hidden_to_tray = False
            except Exception:
                pass

    def _exit_from_tray(self) -> None:
        """Полный выход из меню трея.

        app.exit() откладываем через BeginInvoke: вызов Application.Exit()
        из середины обработчика WinForms-меню ломает dispose формы.
        """
        logger.info("Выход из приложения по команде из трея")
        self.prepare_for_exit()

        def _do_exit():
            try:
                self.app.exit()
            except Exception as e:
                logger.warning("app.exit() failed: %s", e)

        try:
            native = self.window._impl.native
            if (
                _SysAction is not None
                and hasattr(native, "IsHandleCreated")
                and native.IsHandleCreated
            ):
                native.BeginInvoke(_SysAction(_do_exit))
                return
        except Exception as e:
            logger.debug("schedule app.exit failed: %s", e)
        _do_exit()

    # ------------------------------------------------------------------
    # Защита размера окна
    # ------------------------------------------------------------------
    def _install_size_guard(self) -> None:
        if sys.platform != "win32":
            return
        try:
            native = self.window._impl.native
            native.Resize += self._on_native_resize
            logger.info("Size guard установлен (saved=%s)", self._saved_normal_size)
        except Exception as e:
            logger.debug("Не удалось установить size guard: %s", e)

    def _on_native_resize(self, sender, event) -> None:
        try:
            # Юзер тянет окно — не даём отложенному relayout перебить размер
            self._cancel_relayout_timers()
            native = self.window._impl.native

            try:
                state_int = int(native.WindowState)
            except Exception:
                state_int = -1

            if state_int == 1:
                if self._is_hidden_to_tray or self._handling_minimize:
                    return
                self._handling_minimize = True
                try:
                    self._saved_bounds = self._capture_bounds()
                    try:
                        native.WindowState = 0
                    except Exception:
                        pass
                    # Hide на UI-потоке; флаг снимаем только после _hide_to_tray
                    def _do_hide():
                        try:
                            self._hide_to_tray()
                        except Exception as e:
                            logger.warning("_hide_to_tray failed in minimize handler: %s", e)
                        finally:
                            # Гарантированно сбрасываем флаг, даже при ошибке
                            self._handling_minimize = False
                    if _SysAction is not None:
                        native.BeginInvoke(_SysAction(_do_hide))
                    else:
                        _do_hide()
                    logger.info("Minimize → трей (bounds=%s)", self._saved_bounds)
                except Exception as e:
                    self._handling_minimize = False
                    logger.warning("Minimize handler error: %s", e)
                return

            if self._is_hidden_to_tray or self._handling_minimize:
                return

            self._is_resizing = True
            if self._resize_timer is not None:
                self._resize_timer.cancel()
            self._resize_timer = threading.Timer(0.5, self._clear_resize_flag)
            self._resize_timer.daemon = True
            self._resize_timer.start()

            if state_int in (0, 2):
                self._sync_js_visible(True)

            try:
                nw, nh = int(native.Width), int(native.Height)
            except Exception:
                nw, nh = self.window.size
            if (
                state_int == 0
                and nw >= TRAY_MIN_WIDTH
                and nh >= TRAY_MIN_HEIGHT
            ):
                self._saved_normal_size = (nw, nh)
                try:
                    self._saved_bounds = (
                        int(native.Left),
                        int(native.Top),
                        nw,
                        nh,
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.debug("resize handler error: %s", e)

    def _sync_js_visible(self, visible: bool) -> None:
        if self._last_js_visible == visible:
            return
        self._last_js_visible = visible
        # Don't sync visibility when hidden to tray
        if self._is_hidden_to_tray:
            return
        js = (
            "window.setWindowVisible && "
            f"window.setWindowVisible({str(visible).lower()});"
        )
        self._schedule_js(js)

    def _clear_resize_flag(self) -> None:
        self._is_resizing = False

    def _render_html(self) -> str:
        initial_mode = json.dumps(self.config.mode)
        return _load_ui_html().replace('"__INITIAL_MODE__"', initial_mode)

    # ------------------------------------------------------------------
    # WebView / сообщения
    # ------------------------------------------------------------------
    def _on_navigation_starting(self, widget, url: str | None = None, **kwargs) -> bool:
        if not url:
            return True
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or parsed_url.netloc != "obxod.local":
            return True
        mode = parse_qs(parsed_url.query).get("mode", [None])[0]
        self._save_mode(mode)
        return False

    def _on_webview_load(self, widget, **kwargs):
        try:
            self._main_loop = asyncio.get_running_loop()
            # Прокидываем loop в XrayManager — ему нужно ставить задачи
            # из рабочих потоков (asyncio.to_thread(start/stop)).
            self.xray_manager.bind_loop(self._main_loop)
        except RuntimeError:
            self._main_loop = None

        self._install_web_message_handler()

        if not self._ready_notified:
            self._ready_notified = True
            self._schedule_install_tray()
            asyncio.create_task(self._bootstrap())
            asyncio.create_task(self._xray_status_poller())
            if self.on_ready:
                self.on_ready()
        else:
            asyncio.create_task(self._repush_state())

    def _install_web_message_handler(self) -> None:
        if self._web_message_handler_installed:
            return

        # Windows WebView2 — event-driven bridge
        core_webview = getattr(self.webview._impl.native, "CoreWebView2", None)
        if core_webview is not None:
            try:
                core_webview.WebMessageReceived += WeakrefCallable(self._on_web_message)
                self._web_message_handler_installed = True
                logger.info("WebView2 message bridge installed")
                return
            except Exception as e:
                logger.warning("WebView2 bridge failed: %s", e)

        # Linux / macOS (WebKit): poll JS queue __obxodQueue
        self._web_message_handler_installed = True
        logger.info("WebKit message bridge: polling __obxodQueue")
        try:
            asyncio.create_task(self._poll_js_message_queue())
        except RuntimeError:
            logger.warning("no running loop for JS queue poller")

    def _dispatch_web_message(self, msg: dict) -> None:
        action = msg.get("action")
        logger.info("web message: %s %s", action, {k: v for k, v in msg.items() if k != "action"})
        if action == "set_mode":
            self._save_mode(msg.get("mode"))
        elif action == "reload":
            self._handle_reload()
        elif action == "import":
            asyncio.create_task(
                self._handle_import(msg.get("url"), msg.get("name"))
            )
        elif action == "update_subscription":
            asyncio.create_task(self._handle_update_subscription())
        elif action == "select_provider":
            asyncio.create_task(self._handle_provider_change(msg.get("name")))
        elif action == "select_config":
            asyncio.create_task(self._handle_config_selection(msg.get("index")))
        elif action == "toggle_connection":
            asyncio.create_task(self._handle_toggle_connection())
        elif action == "update_setting":
            asyncio.create_task(
                self._handle_update_setting(msg.get("key"), msg.get("value"))
            )
        elif action == "ping_all":
            asyncio.create_task(self._handle_ping_all())
        else:
            logger.debug("unknown web action: %r", action)

    def _on_web_message(self, sender, event) -> None:
        try:
            raw_msg = str(event.TryGetWebMessageAsString())
        except Exception as exc:
            logger.warning("Не удалось прочитать сообщение WebView2: %s", exc)
            return

        try:
            msg = json.loads(raw_msg)
        except json.JSONDecodeError:
            msg = {"action": "set_mode", "mode": raw_msg}

        if isinstance(msg, dict):
            self._dispatch_web_message(msg)

    def _queue_js_state(self, kind: str, payload: dict) -> None:
        """Кладёт payload в pending — применится внутри poll (единственный надёжный канал на WebKit)."""
        if not hasattr(self, "_pending_js"):
            self._pending_js: dict[str, dict] = {}
        self._pending_js[kind] = payload

    async def _poll_js_message_queue(self) -> None:
        """Двусторонний мост WebKit:
        - JS→Python: __obxodQueue
        - Python→JS: pending state/routing/settings/status вшиваются в тот же evaluate
        """
        while True:
            try:
                if getattr(self, "_is_hidden_to_tray", False):
                    await asyncio.sleep(0.4)
                    continue
                # Additional safety: don't evaluate JS on hidden window
                if self._is_hidden_to_tray:
                    await asyncio.sleep(0.4)
                    continue

                pending = getattr(self, "_pending_js", None) or {}
                # забираем снимок и очищаем до await (новые push'и подождут следующий тик)
                self._pending_js = {}

                inject_parts: list[str] = []
                if "state" in pending:
                    inject_parts.append(
                        "try{if(window.applyAppState)window.applyAppState("
                        + json.dumps(pending["state"], ensure_ascii=True)
                        + ");}catch(e){console.error('applyAppState',e);}"
                    )
                if "routing" in pending:
                    inject_parts.append(
                        "try{var _r="
                        + json.dumps(pending["routing"], ensure_ascii=True)
                        + ";if(window.setRoutingSets)window.setRoutingSets(_r.sets,_r.active);}"
                        "catch(e){console.error('setRoutingSets',e);}"
                    )
                if "settings" in pending:
                    inject_parts.append(
                        "try{if(window.setSettings)window.setSettings("
                        + json.dumps(pending["settings"], ensure_ascii=True)
                        + ");}catch(e){console.error('setSettings',e);}"
                    )
                if "xray_status" in pending:
                    st = pending["xray_status"]
                    inject_parts.append(
                        "try{if(window.setXrayStatus)window.setXrayStatus("
                        + json.dumps(st.get("status"), ensure_ascii=True) + ","
                        + json.dumps(st.get("message") or "", ensure_ascii=True)
                        + ");}catch(e){console.error('setXrayStatus',e);}"
                    )

                inject = "".join(inject_parts)
                js = (
                    "(function(){"
                    + inject
                    + "var q=window.__obxodQueue||[];"
                    "window.__obxodQueue=[];"
                    "return JSON.stringify(q);"
                    "})()"
                )

                # Safety check: don't evaluate JS when window is hidden to tray
                if self._is_hidden_to_tray:
                    await asyncio.sleep(0.12)
                    continue

                raw = await self.webview.evaluate_javascript(js)
                items: list = []
                if raw is None or raw == "" or raw == "null":
                    await asyncio.sleep(0.12)
                    continue
                if isinstance(raw, (list, tuple)):
                    items = list(raw)
                else:
                    text = str(raw).strip()
                    if text.startswith('"') and text.endswith('"'):
                        try:
                            text = json.loads(text)
                        except json.JSONDecodeError:
                            pass
                    try:
                        items = json.loads(text) if isinstance(text, str) else text
                    except json.JSONDecodeError:
                        items = []
                if not isinstance(items, list):
                    items = []
                for item in items:
                    if isinstance(item, dict):
                        self._dispatch_web_message(item)
                    elif isinstance(item, str):
                        try:
                            obj = json.loads(item)
                            if isinstance(obj, dict):
                                self._dispatch_web_message(obj)
                        except json.JSONDecodeError:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("js queue poll: %s", e)
            await asyncio.sleep(0.12)

    def _handle_reload(self) -> None:
        try:
            logger.info("Перезагрузка UI (F5)")
            self._ready_notified = False
            self.webview.set_content("about:blank", self._render_html())
        except Exception as e:
            logger.warning("reload failed: %s", e)

    async def _repush_state(self) -> None:
        try:
            providers = await asyncio.to_thread(self._list_providers)
            active = self.config.get("active_provider")
            selected_index = -1
            saved_id = self.config.get("active_config_id")
            if saved_id:
                for i, r in enumerate(self._loaded_configs):
                    if r["id"] == saved_id:
                        selected_index = i
                        break
            await self._push_state(providers, active, self._loaded_configs, selected_index)
            await self._push_settings()
            await self._push_routing_sets()
            status, msg = await asyncio.to_thread(
                lambda: (self.xray_manager.status, self.xray_manager.status_message)
            )
            await self._push_xray_status(status, msg)
        except Exception as e:
            logger.debug("_repush_state failed: %s", e)

    def _save_mode(self, mode: str | None) -> None:
        if mode not in VALID_MODES:
            logger.warning("Интерфейс передал недопустимый режим: %r", mode)
            return
        old_mode = self.config.mode
        self.config.mode = mode
        self.config.save()
        logger.info("Режим из интерфейса сохранён: %s (было %s)", mode, old_mode)

        if old_mode != mode:
            asyncio.create_task(self._restart_xray_after_mode_change_if_running())

    async def _restart_xray_after_mode_change_if_running(self) -> None:
        running = await asyncio.to_thread(lambda: self.xray_manager.is_running)
        if not running:
            return
        record = self._get_active_record()
        if record is None or record["kind"] != "xray":
            return
        try:
            async with self._restart_lock:
                await self.xray_manager.restart(record["raw"])
        except Exception as e:
            logger.error("Авто-перезапуск после смены режима упал: %s", e, exc_info=True)

    async def _restart_xray_after_routing_change(self) -> None:
        record = self._get_active_record()
        if record is None or record["kind"] != "xray":
            return
        try:
            async with self._restart_lock:
                logger.info("Перезапуск Xray после смены пресета маршрутизации")
                await self.xray_manager.restart(record["raw"])
        except Exception as e:
            logger.error(
                "Авто-перезапуск после смены маршрутизации упал: %s",
                e, exc_info=True,
            )

    # ------------------------------------------------------------------
    # JS-мост
    # ------------------------------------------------------------------
    async def _push_js_raw(self, js: str) -> None:
        # Safety check: don't push JS when window is hidden to tray
        # This prevents WebView2 evaluate_javascript from hanging on hidden window
        if self._is_hidden_to_tray:
            return
        try:
            await self.webview.evaluate_javascript(js)
        except Exception as e:
            logger.debug("JS push failed: %s", e)

    async def _push_js(self, js: str) -> None:
        if self._is_resizing or self._is_hidden_to_tray:
            return
        await self._push_js_raw(js)

    async def _push_js_safe(self, js: str) -> None:
        """Safe version that checks _is_hidden_to_tray before pushing."""
        if self._is_hidden_to_tray:
            return
        await self._push_js_raw(js)

    def _schedule_js(self, js: str) -> None:
        loop = self._main_loop
        if loop is None or not loop.is_running():
            return
        # Safety check: don't schedule JS when window is hidden to tray
        if self._is_hidden_to_tray:
            return
        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._push_js_safe(js))
            )
        except Exception as e:
            logger.debug("schedule_js failed: %s", e)

    def _schedule_js_best_effort(self, js: str) -> None:
        """Как _schedule_js, но глотает ошибки (критично после Hide/restore)."""
        try:
            self._schedule_js(js)
        except Exception as e:
            logger.debug("best-effort JS failed: %s", e)


    async def _xray_status_poller(self) -> None:
        last: tuple[str, str] | None = None
        while True:
            try:
                # Не трогаем WebView, пока окно в трее — иначе evaluate_javascript
                # после Hide() может зависнуть и оставить UI «inactive» при restore.
                if self._is_hidden_to_tray:
                    await asyncio.sleep(1.2)
                    continue
                # status/message — socket I/O к supervisor'у, уводим в поток.
                cur = await asyncio.to_thread(
                    lambda: (self.xray_manager.status, self.xray_manager.status_message)
                )
                if cur != last:
                    last = cur
                    await self._push_xray_status(*cur)
            except Exception as e:
                logger.debug("status poller error: %s", e)
            await asyncio.sleep(1.2)

    async def _push_xray_status(self, status: str, message: str) -> None:
        if sys.platform == "win32":
            js = (
                "window.setXrayStatus("
                + json.dumps(status, ensure_ascii=True) + ", "
                + json.dumps(message or "", ensure_ascii=True)
                + ");"
            )
            await self._push_js_safe(js)
        else:
            self._queue_js_state("xray_status", {"status": status, "message": message or ""})

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------
    async def _handle_update_setting(self, key, value) -> None:
        validator = self._setting_validators.get(key)
        if validator is None:
            logger.warning("Неизвестная настройка: %r", key)
            return
        try:
            if not validator(value):
                logger.warning("Невалидное значение %s=%r", key, value)
                return
        except Exception as e:
            logger.warning("Ошибка валидации %s=%r: %s", key, value, e)
            return

        self.config.set(key, value)
        self.config.save()
        logger.info("Настройка обновлена: %s = %r", key, value)

        await self._apply_setting_runtime(key, value)

    async def _apply_setting_runtime(self, key: str, value) -> None:
        if key == "xray_loglevel":
            self.xray_manager.set_loglevel(value)
        elif key == "xray_log_rotate_minutes":
            self.xray_manager.set_log_rotate_minutes(int(value))
        elif key == "routing_set":
            running = await asyncio.to_thread(lambda: self.xray_manager.is_running)
            if running:
                asyncio.create_task(self._restart_xray_after_routing_change())
        elif key == "network_watch_interval":
            self._network_watcher.interval = float(value)
            logger.info("NetworkWatcher interval → %s c", value)
        elif key == "network_watch_enabled":
            if value:
                self._network_watcher.start(self._handle_network_event)
                logger.info("NetworkWatcher запущен из настроек")
            else:
                await self._network_watcher.stop()
                logger.info("NetworkWatcher остановлен из настроек")
                running = await asyncio.to_thread(lambda: self.xray_manager.is_running)
                if self._network_ok and not running:
                    await self._push_xray_status(
                        "stopped", "Нажмите на куб для подключения"
                    )

    async def _push_settings(self) -> None:
        payload = {
            "xray_loglevel":                   self.config.get("xray_loglevel", "warning"),
            "xray_log_rotate_minutes":         self.config.get("xray_log_rotate_minutes", 10),
            "network_watch_enabled":           bool(self.config.get("network_watch_enabled", True)),
            "network_watch_interval":          self.config.get("network_watch_interval", 2),
            "network_auto_reconnect":          bool(self.config.get("network_auto_reconnect", True)),
            "network_restart_on_iface_change": bool(self.config.get("network_restart_on_iface_change", True)),
            "ping_url":                        self.config.get("ping_url", DEFAULT_PING_URL),
            "ping_timeout":                    self.config.get("ping_timeout", 5),
            "ping_concurrency":                self.config.get("ping_concurrency", 8),
            "routing_set":                     self.config.get("routing_set"),
            "auto_update_enabled":             bool(self.config.get("auto_update_enabled", True)),
        }
        if sys.platform == "win32":
            js = "window.setSettings(" + json.dumps(payload, ensure_ascii=True) + ");"
            await self._push_js_safe(js)
        else:
            self._queue_js_state("settings", payload)

    # ------------------------------------------------------------------
    # Пресеты маршрутизации
    # ------------------------------------------------------------------
    def _routing_dir(self) -> Path:
        return Path(__file__).resolve().parent / "cfg" / "routing"

    def _list_routing_sets(self) -> list[dict]:
        from net_utils import list_routing_sets
        return list_routing_sets(self._routing_dir())

    async def _push_routing_sets(self) -> None:
        sets = await asyncio.to_thread(self._list_routing_sets)
        active = self.config.get("routing_set")
        if active and not any(s["name"] == active for s in sets):
            logger.warning("Активный пресет %r не найден, сбрасываю", active)
            active = None
            self.config.set("routing_set", None)
            self.config.save()
        data = {"sets": sets, "active": active}
        logger.info("push routing sets: %d, active=%r", len(sets), active)
        if sys.platform == "win32":
            payload = json.dumps(data, ensure_ascii=True)
            js = (
                "(function(){try{var d=" + payload + ";"
                "if(window.setRoutingSets)window.setRoutingSets(d.sets,d.active);}"
                "catch(e){console.error(e);}return true;})()"
            )
            await self._push_js_safe(js)
        else:
            self._queue_js_state("routing", data)

    # ------------------------------------------------------------------
    # Пинг всех конфигов
    # ------------------------------------------------------------------
    async def _handle_ping_all(self) -> None:
        if self._ping_lock.locked():
            logger.info("Пинг уже идёт — игнорирую")
            return
        async with self._ping_lock:
            await self._ping_all_locked()

    async def _ping_all_locked(self) -> None:
        records = list(self._loaded_configs)
        if not records:
            return

        self._ping_generation += 1
        gen = self._ping_generation

        url = str(self.config.get("ping_url", DEFAULT_PING_URL))
        timeout = float(self.config.get("ping_timeout", 5))
        concurrency = int(self.config.get("ping_concurrency", 8))
        concurrency = max(1, min(20, concurrency))

        logger.info(
            "Ping: %d конфигов, url=%s, timeout=%.1fs, concurrency=%d",
            len(records), url, timeout, concurrency,
        )

        await self._push_js_safe("window.setPingBusy(true); window.setPingReset();")

        sem = asyncio.Semaphore(concurrency)

        async def worker(index: int, record: dict) -> None:
            async with sem:
                if gen != self._ping_generation:
                    return
                ms: int | None
                if record.get("kind") != "xray" or not isinstance(record.get("raw"), dict):
                    ms = None
                else:
                    try:
                        ms = await asyncio.to_thread(
                            self.xray_manager.ping_config_sync,
                            record["raw"], url, timeout,
                        )
                    except Exception as e:
                        logger.info("Ping [%d] '%s' failed: %s",
                                    index, record["meta"].get("name"), e)
                        ms = None

                if gen != self._ping_generation:
                    return
                await self._push_js_safe(
                    f"window.setPingResult({index}, {json.dumps(ms)});"
                )

        try:
            await asyncio.gather(
                *(worker(i, r) for i, r in enumerate(records)),
                return_exceptions=True,
            )
        finally:
            if gen == self._ping_generation:
                await self._push_js_safe("window.setPingBusy(false);")

    # ------------------------------------------------------------------
    # Кнопка подключения (куб)
    # ------------------------------------------------------------------
    async def _handle_toggle_connection(self) -> None:
        if self._toggle_lock.locked():
            return
        async with self._toggle_lock:
            try:
                running = await asyncio.to_thread(lambda: self.xray_manager.is_running)
                if running:
                    logger.info("Toggle: останавливаю Xray")
                    self._want_xray_running = False
                    await asyncio.to_thread(self.xray_manager.stop)
                    return

                record = self._get_active_record()
                if record is None:
                    logger.warning("Toggle: нет выбранного конфига")
                    await self._push_xray_status("error", "Сначала выберите конфиг")
                    return

                if record["kind"] != "xray":
                    logger.warning("Toggle: выбран link-конфиг")
                    await self._push_xray_status(
                        "error", "Link-конфиг ещё не поддерживается"
                    )
                    return

                if not self._network_ok:
                    self._want_xray_running = True
                    self.xray_manager.set_network_down()
                    return

                logger.info("Toggle: запускаю Xray с конфигом id=%s", record["id"])
                self._want_xray_running = True
                await asyncio.to_thread(self.xray_manager.start, record["raw"])

            except Exception as e:
                logger.error("Toggle error: %s", e, exc_info=True)
                await self._push_xray_status("error", str(e))

    def _get_active_record(self) -> dict | None:
        idx = self.config.get("active_config_id")
        if not idx:
            return None
        for r in self._loaded_configs:
            if r["id"] == idx:
                return r
        return None

    # ------------------------------------------------------------------
    # Сетевые события
    # ------------------------------------------------------------------
    async def _handle_network_event(self, event: NetworkEvent, state: NetworkState) -> None:
        if not self.config.get("network_watch_enabled", True):
            return

        logger.info(
            "Network event: %s (iface=%r up=%s, want_xray=%s)",
            event.value, state.iface, state.is_up, self._want_xray_running,
        )

        if event == NetworkEvent.DOWN:
            self._network_ok = False
            running = await asyncio.to_thread(lambda: self.xray_manager.is_running)
            if running:
                logger.info("Сеть пропала — останавливаю Xray")
                await asyncio.to_thread(self.xray_manager.stop)
            self.xray_manager.set_network_down()
            await self._push_xray_status("network_down", "Нет сети — ждём подключения...")
            return

        # UP / INTERFACE_CHANGED — только если probe реально нашёл физическую сеть
        if not state.is_up:
            logger.info("Событие %s без is_up — игнорирую", event.value)
            return

        self._network_ok = True

        if not self.config.get("network_auto_reconnect", True):
            running = await asyncio.to_thread(lambda: self.xray_manager.is_running)
            if not running:
                await self._push_xray_status(
                    "stopped",
                    "Сеть восстановлена — нажмите на куб",
                )
            return

        if event == NetworkEvent.INTERFACE_CHANGED:
            if not self.config.get("network_restart_on_iface_change", True):
                return
            # Не рестартуем, если Xray только что стартовал (TUN поднимается)
            # и «смена» — артефакт маршрута. Probe уже фильтрует tun, но
            # debounce на всякий случай.
            logger.info("Сменился физический интерфейс → %r — рестартую Xray", state.iface)

        if not self._want_xray_running:
            return

        record = self._get_active_record()
        if record is None or record["kind"] != "xray":
            logger.warning("Network up, но нет валидного конфига для перезапуска")
            return

        try:
            async with self._restart_lock:
                logger.info("Сеть восстановлена — перезапускаю Xray")
                await self.xray_manager.restart(record["raw"])
        except Exception as e:
            logger.error("Перезапуск после сети упал: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # Провайдеры
    # ------------------------------------------------------------------
    def _providers_dir(self) -> Path:
        return Path(__file__).resolve().parent / "cfg" / "providers"

    def _list_providers(self) -> list[dict]:
        d = self._providers_dir()
        if not d.exists():
            return []
        out: list[dict] = []
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Не удалось прочитать %s: %s", f, e)
                continue
            name = data.get("name") or f.stem
            configs = data.get("configs") or []
            out.append({"name": name, "file": f.name, "count": len(configs)})
        return out

    def _load_provider_configs(self, name: str) -> list[dict]:
        d = self._providers_dir()
        path = d / f"{name}.json"
        if not path.exists():
            logger.warning("Файл провайдера не найден: %s", path)
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("Не удалось прочитать %s: %s", path, e)
            return []

        records: list[dict] = []
        for i, item in enumerate(data.get("configs") or []):
            kind = item.get("kind", "xray")
            raw = item.get("raw")
            cid = item.get("id") or _config_id(kind, raw)

            meta = dict(item.get("meta") or {})
            if kind == "xray" and isinstance(raw, dict):
                meta = {**self._extract_metadata_from_xray(raw, i), **meta}
            elif kind == "link" and isinstance(raw, str):
                meta = {**self._extract_metadata_from_link(raw, i), **meta}
            meta["index"] = i

            records.append({"id": cid, "kind": kind, "raw": raw, "meta": meta})
        return records

    async def _handle_provider_change(self, name: str | None) -> None:
        if not name:
            return
        providers = await asyncio.to_thread(self._list_providers)
        if not any(p["name"] == name for p in providers):
            logger.warning("Запрошен несуществующий провайдер: %s", name)
            return

        running = await asyncio.to_thread(lambda: self.xray_manager.is_running)
        if running:
            logger.info("Смена провайдера: останавливаю Xray")
            await asyncio.to_thread(self.xray_manager.stop)
            self._want_xray_running = False

        self._ping_generation += 1

        records = await asyncio.to_thread(self._load_provider_configs, name)
        self._loaded_configs = records

        self.config.set("active_provider", name)
        self.config.set("active_config_id", None)
        self.config.save()

        await self._push_state(providers, name, records, -1)
        logger.info("Переключён провайдер: %s (%d конфигов)", name, len(records))

    async def _push_state(
        self,
        providers: list[dict],
        active_provider: str | None,
        records: list[dict],
        selected_index: int,
    ) -> None:
        """Атомарно пушит providers+configs одним вызовом (WebKit надёжнее)."""
        providers_payload = [
            {"name": p["name"], "count": p["count"]} for p in providers
        ]
        configs_payload = []
        for r in records:
            meta = dict(r.get("meta") or {})
            # только плоские поля — без сырого raw
            configs_payload.append({
                "name": meta.get("name") or "Config",
                "protocol": meta.get("protocol"),
                "transport": meta.get("transport"),
                "security": meta.get("security"),
                "index": meta.get("index"),
            })

        state = {
            "providers": providers_payload,
            "activeProvider": active_provider,
            "configs": configs_payload,
            "selectedIndex": int(selected_index),
        }
        logger.info(
            "push state: providers=%d active=%r configs=%d selected=%d",
            len(providers_payload), active_provider,
            len(configs_payload), selected_index,
        )
        # Windows WebView2 — прямой evaluate; Linux — через poll-канал
        if sys.platform == "win32":
            payload = json.dumps(state, ensure_ascii=True)
            js = (
                "(function(){try{if(window.applyAppState)window.applyAppState("
                + payload + ");}catch(e){console.error(e);}return true;})()"
            )
            await self._push_js_safe(js)
        else:
            self._queue_js_state("state", state)

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    async def _bootstrap(self) -> None:
        await asyncio.sleep(0.05)
        try:
            asyncio.create_task(
                asyncio.to_thread(self.xray_manager.ensure_geofiles)
            )

            providers = await asyncio.to_thread(self._list_providers)
            active = self.config.get("active_provider")
            if not active or not any(p["name"] == active for p in providers):
                active = providers[0]["name"] if providers else None

            records: list[dict] = []
            selected_index = -1

            if active:
                records = await asyncio.to_thread(self._load_provider_configs, active)
                saved_id = self.config.get("active_config_id")
                if saved_id:
                    for i, r in enumerate(records):
                        if r["id"] == saved_id:
                            selected_index = i
                            break

            self.config.set("active_provider", active)
            self.config.set("active_config_id",
                            records[selected_index]["id"] if selected_index >= 0 else None)
            self.config.save()

            self._loaded_configs = records
            await self._push_state(providers, active, records, selected_index)

            self.xray_manager.set_loglevel(self.config.get("xray_loglevel", "warning"))
            self.xray_manager.set_log_rotate_minutes(
                int(self.config.get("xray_log_rotate_minutes", 10))
            )

            await self._push_settings()
            await self._push_routing_sets()

            if self.config.get("network_watch_enabled", True):
                self._network_watcher.start(self._handle_network_event)
                logger.info(
                    "NetworkWatcher запущен (interval=%ss)",
                    self._network_watcher.interval,
                )

            logger.info(
                "Bootstrap: провайдер=%r, конфигов=%d, выбран=%d, routing=%r",
                active, len(records), selected_index,
                self.config.get("routing_set"),
            )
        except Exception as e:
            logger.error("Bootstrap failed: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # Скачивание
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_html(content: bytes) -> bool:
        head = content[:512].lstrip().lower()
        return head.startswith(b"<!doctype") or head.startswith(b"<html")

    @staticmethod
    def _blocking_fetch(url: str) -> tuple[bytes, dict]:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": SUBSCRIPTION_USER_AGENT,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.read(), dict(response.headers)

    async def _fetch_subscription_raw(self, url: str) -> tuple[bytes, dict]:
        content, headers = await asyncio.to_thread(self._blocking_fetch, url)
        logger.info(
            "Ответ сервера: %d байт, Content-Type=%r, HTML=%s",
            len(content), headers.get("Content-Type"),
            self._looks_like_html(content),
        )
        if self._looks_like_html(content):
            raise RuntimeError(
                f"Сервер вернул HTML вместо конфигов. "
                f"Проверьте User-Agent ({SUBSCRIPTION_USER_AGENT!r}) "
                f"или endpoint подписки."
            )
        return content, headers

    # ------------------------------------------------------------------
    # Импорт
    # ------------------------------------------------------------------
    async def _handle_import(self, url: str, name: str) -> None:
        if self._import_lock.locked():
            logger.info("Импорт уже идёт — второй запрос проигнорирован")
            return
        async with self._import_lock:
            await self._import_locked(url, name)

    async def _import_locked(self, url: str, name: str) -> None:
        logger.info("=" * 60)
        logger.info("НАЧАЛО ИМПОРТА ПОДПИСКИ")
        logger.info("URL: %s", url)
        logger.info("Имя от пользователя: %r", name)

        try:
            content, headers = await self._fetch_subscription_raw(url)
            decoded = self._decode_subscription_content(content)
            if decoded is None:
                raise ValueError("Не удалось декодировать тело подписки.")

            logger.info("Первые 300 символов ответа:\n%s",
                        decoded[:300].replace("\n", "\\n"))

            xray_configs = self._parse_xray_configs(decoded)
            share_links: list[str] = []
            if not xray_configs:
                logger.info("Xray-массив не обнаружен, пробую share-ссылки.")
                share_links = self._parse_share_links(decoded)

            if not xray_configs and not share_links:
                raise ValueError(
                    "В подписке не найдено ни Xray-конфигов, ни share-ссылок."
                )

            records: list[dict] = []
            if xray_configs:
                for i, cfg in enumerate(xray_configs):
                    records.append({
                        "id": _config_id("xray", cfg),
                        "kind": "xray",
                        "raw": cfg,
                        "meta": self._extract_metadata_from_xray(cfg, i),
                    })
            else:
                for i, link in enumerate(share_links):
                    records.append({
                        "id": _config_id("link", link),
                        "kind": "link",
                        "raw": link,
                        "meta": self._extract_metadata_from_link(link, i),
                    })

            logger.info("Распознано конфигов: %d", len(records))

            provider_name = self._resolve_provider_name(name, headers, records)
            provider_name = _sanitize_provider_name(provider_name)
            logger.info("Имя провайдера: %s", provider_name)

            provider_dir = self._providers_dir()
            provider_dir.mkdir(parents=True, exist_ok=True)
            provider_file = provider_dir / f"{provider_name}.json"

            data_to_save = {
                "name": provider_name,
                "url": url,
                "updated_at": time.time(),
                "configs": records,
            }
            provider_file.write_text(
                json.dumps(data_to_save, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("Сохранено в %s", provider_file)

            running = await asyncio.to_thread(lambda: self.xray_manager.is_running)
            if running:
                logger.info("Импорт: останавливаю Xray (конфиг сменился)")
                await asyncio.to_thread(self.xray_manager.stop)
                self._want_xray_running = False

            self._ping_generation += 1
            self._loaded_configs = records

            self.config.set("active_provider", provider_name)
            self.config.set("active_config_id", None)
            self.config.save()

            providers = await asyncio.to_thread(self._list_providers)
            await self._push_state(providers, provider_name, records, -1)

        except Exception as e:
            logger.error("Ошибка импорта: %s", e, exc_info=True)
            js = (
                "window.showImportError("
                + json.dumps(str(e), ensure_ascii=False)
                + ");"
            )
            await self._push_js_safe(js)

        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # Обновление подписки
    # ------------------------------------------------------------------
    async def _handle_update_subscription(self) -> None:
        if self._import_lock.locked():
            logger.info("Обновление/импорт уже идёт — запрос проигнорирован")
            return
        async with self._import_lock:
            await self._update_subscription_locked()

    async def _update_subscription_locked(self) -> None:
        provider_name = self.config.get("active_provider")
        if not provider_name:
            await self._push_js_safe(
                "window.showImportError('Нет активного провайдера');"
            )
            return

        provider_file = self._providers_dir() / f"{provider_name}.json"
        if not provider_file.exists():
            await self._push_js_safe(
                "window.showImportError('Файл провайдера не найден');"
            )
            return

        try:
            data = json.loads(provider_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("Не читается %s: %s", provider_file, e)
            await self._push_js_safe(
                "window.showImportError("
                + json.dumps(str(e), ensure_ascii=False)
                + ");"
            )
            return

        url = data.get("url")
        if not url:
            await self._push_js_safe(
                "window.showImportError('Для этого провайдера не сохранён URL подписки');"
            )
            return

        await self._push_js_safe("window.setRefreshBusy(true);")
        try:
            content, headers = await self._fetch_subscription_raw(url)
            decoded = self._decode_subscription_content(content)
            if decoded is None:
                raise ValueError("Не удалось декодировать тело подписки.")

            xray_configs = self._parse_xray_configs(decoded)
            share_links: list[str] = []
            if not xray_configs:
                share_links = self._parse_share_links(decoded)
            if not xray_configs and not share_links:
                raise ValueError("В подписке не найдено конфигов.")

            records: list[dict] = []
            if xray_configs:
                for i, cfg in enumerate(xray_configs):
                    records.append({
                        "id": _config_id("xray", cfg),
                        "kind": "xray",
                        "raw": cfg,
                        "meta": self._extract_metadata_from_xray(cfg, i),
                    })
            else:
                for i, link in enumerate(share_links):
                    records.append({
                        "id": _config_id("link", link),
                        "kind": "link",
                        "raw": link,
                        "meta": self._extract_metadata_from_link(link, i),
                    })

            data_to_save = {
                "name": provider_name,
                "url": url,
                "updated_at": time.time(),
                "configs": records,
            }
            provider_file.write_text(
                json.dumps(data_to_save, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            running = await asyncio.to_thread(lambda: self.xray_manager.is_running)
            if running:
                logger.info("Обновление подписки: останавливаю Xray")
                await asyncio.to_thread(self.xray_manager.stop)
                self._want_xray_running = False

            self._ping_generation += 1
            self._loaded_configs = records
            # ID активного конфига сбрасываем — он мог исчезнуть.
            self.config.set("active_config_id", None)
            self.config.save()

            providers = await asyncio.to_thread(self._list_providers)
            await self._push_state(providers, provider_name, records, -1)
            logger.info(
                "Подписка %r обновлена: %d конфигов",
                provider_name, len(records),
            )

        except Exception as e:
            logger.error("Ошибка обновления подписки: %s", e, exc_info=True)
            await self._push_js_safe(
                "window.showImportError("
                + json.dumps(str(e), ensure_ascii=False)
                + ");"
            )
        finally:
            await self._push_js_safe("window.setRefreshBusy(false);")

    # ------------------------------------------------------------------
    # Парсинг
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_xray_configs(decoded: str) -> list[dict]:
        try:
            obj = json.loads(decoded)
        except json.JSONDecodeError:
            return []
        items = obj if isinstance(obj, list) else [obj]
        configs = []
        for item in items:
            if isinstance(item, dict) and "outbounds" in item:
                configs.append(item)
        return configs

    @staticmethod
    def _extract_metadata_from_xray(cfg: dict, index: int) -> dict:
        meta = {
            "index": index,
            "name": cfg.get("remarks") or cfg.get("name") or f"Config #{index}",
            "protocol": "?",
            "transport": "tcp",
            "security": "none",
            "type": "json",
        }
        try:
            outbounds = cfg.get("outbounds") or []
            target = None
            for ob in outbounds:
                proto = ob.get("protocol")
                if proto and proto not in ("freedom", "blackhole", "dns"):
                    target = ob
                    break
            if target:
                meta["protocol"] = (target.get("protocol") or "?").lower()
                stream = target.get("streamSettings") or {}
                net = (stream.get("network") or "tcp").lower()
                meta["transport"] = net
                sec = (stream.get("security") or "none").lower()
                meta["security"] = sec or "none"
        except Exception:
            pass
        return meta

    @staticmethod
    def _extract_metadata_from_link(link: str, index: int) -> dict:
        meta = {
            "index": index,
            "name": f"Config #{index}",
            "protocol": "?",
            "transport": "tcp",
            "security": "none",
            "type": "link",
        }
        try:
            if link.lower().startswith("vmess://"):
                b64 = link[len("vmess://"):]
                padding = 4 - (len(b64) % 4)
                if padding != 4:
                    b64 += "=" * padding
                obj = json.loads(base64.b64decode(b64).decode("utf-8"))
                meta["protocol"] = "vmess"
                if obj.get("ps"):
                    meta["name"] = obj["ps"]
                meta["transport"] = (obj.get("net") or "tcp").lower()
                sec = (obj.get("tls") or "").lower()
                meta["security"] = sec if sec else "none"
                return meta

            parsed = urlparse(link)
            meta["protocol"] = (parsed.scheme or "?").lower()
            if parsed.fragment:
                meta["name"] = unquote(parsed.fragment)
            qs = parse_qs(parsed.query)
            meta["transport"] = (qs.get("type", ["tcp"])[0] or "tcp").lower()
            meta["security"] = (qs.get("security", ["none"])[0] or "none").lower()
        except Exception:
            pass
        return meta

    @staticmethod
    def _parse_share_links(decoded: str) -> list[str]:
        proto_re = re.compile(
            r"^(vless|vmess|trojan|ss|ssr|hysteria|hysteria2|hy2|tuic)://",
            re.IGNORECASE,
        )
        links = []
        for line in decoded.splitlines():
            line = line.strip()
            if line and proto_re.match(line):
                links.append(line)
        return links

    @staticmethod
    def _decode_subscription_content(content: bytes) -> str | None:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            logger.info("UTF-8 не подошёл, пробую Base64...")

        try:
            b64_str = content.decode("ascii", errors="ignore").strip()
            b64_str = re.sub(r"\s+", "", b64_str)
            padding = 4 - (len(b64_str) % 4)
            if padding != 4:
                b64_str += "=" * padding
            return base64.b64decode(b64_str, validate=True).decode("utf-8")
        except Exception as e:
            logger.info("Base64 не подошёл: %s", e)

        try:
            return base64.urlsafe_b64decode(content).decode("utf-8")
        except Exception as e:
            logger.info("URL-safe Base64 не подошёл: %s", e)

        try:
            return content.decode("utf-8", errors="ignore")
        except Exception:
            return None

    @staticmethod
    def _resolve_provider_name(
        user_name: str, headers: dict, records: list[dict]
    ) -> str:
        for r in records:
            if r["kind"] == "link" and isinstance(r["raw"], str):
                link = r["raw"]
                if "#" in link:
                    frag = link.split("#", 1)[1].strip()
                    if frag:
                        return unquote(frag)

        cd = headers.get("Content-Disposition", "")
        m = re.search(r'filename="?([^";]+)"?', cd)
        if m:
            return m.group(1).replace(".json", "").strip()

        if user_name and user_name.strip():
            return user_name.strip()

        return f"provider_{int(time.time())}"

    # ------------------------------------------------------------------
    # Выбор конфига
    # ------------------------------------------------------------------
    async def _handle_config_selection(self, index) -> None:
        # JSON/JS bridge может отдать int, float или str
        try:
            index = int(index)
        except (TypeError, ValueError):
            logger.warning("select_config: плохой index=%r", index)
            return
        if not (0 <= index < len(self._loaded_configs)):
            logger.warning(
                "Недопустимый индекс конфига: %r (всего %d)",
                index, len(self._loaded_configs),
            )
            return

        record = self._loaded_configs[index]
        name = record["meta"].get("name") or f"Config #{index}"

        self.config.set("active_config_id", record["id"])
        self.config.save()
        logger.info("Выбран конфиг [%d] id=%s: %s (kind=%s)",
                    index, record["id"], name, record["kind"])

        # Подтверждаем выбор в UI (на случай рассинхрона)
        await self._push_state(
            await asyncio.to_thread(self._list_providers),
            self.config.get("active_provider"),
            self._loaded_configs,
            index,
        )

    # ------------------------------------------------------------------
    # Windows-специфичное
    # ------------------------------------------------------------------
    def _apply_dark_titlebar(self):
        if sys.platform != "win32":
            return
        try:
            native = self.window._impl.native
            if native is None:
                return
            try:
                hwnd = int(native.Handle.ToInt64())
            except Exception:
                hwnd = int(native.Handle.ToInt32())
            if not hwnd:
                return

            dwm = ctypes.windll.dwmapi
            value = ctypes.c_int(1)

            for attr in (20, 19):
                res = dwm.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
                )
                if res == 0:
                    break
        except Exception as exc:
            logger.warning("Не удалось применить тёмный заголовок: %s", exc)

    @staticmethod
    def _apply_native_backcolor(native):
        if sys.platform != "win32" or native is None or _Color is None:
            return
        try:
            native.BackColor = _Color.FromArgb(*BG_COLOR)
        except Exception as exc:
            logger.debug("BackColor не применился: %s", exc)