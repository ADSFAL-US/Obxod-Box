import asyncio
import ctypes
import json
import logging
import os
import re
import sys
from pathlib import Path

import toga
from toga.colors import rgb
from toga.style import Pack
from toga.style.pack import COLUMN

from config import Config
from net_utils import (
    GEOIP_URL,
    GEOSITE_URL,
    download_geofile,
    download_wintun,
    download_xray,
)

# Безопасный импорт для Windows (Pylance не видит System.Drawing на других ОС)
if sys.platform == "win32":
    try:
        import clr

        clr.AddReference("System.Drawing")
        from System.Drawing import Color  # type: ignore
    except ImportError:
        pass

logger = logging.getLogger(__name__)

BG_COLOR = (45, 45, 45)
BG_COLOR_CSS = "#2d2d2d"
BG_COLOR_WEBVIEW = "FF2D2D2D"


def crop_svg(svg_text: str, crop: float = 50) -> str:
    """Обрезает SVG на `crop` пикселей с каждой стороны через viewBox."""
    vb_match = re.search(r'viewBox\s*=\s*["\']([^"\']+)["\']', svg_text)
    if vb_match:
        parts = re.split(r"[\s,]+", vb_match.group(1).strip())
        x, y, w, h = (float(p) for p in parts[:4])
        new_vb = f"{x + crop} {y + crop} {w - 2 * crop} {h - 2 * crop}"
        svg_text = svg_text[: vb_match.start(1)] + new_vb + svg_text[vb_match.end(1) :]
    else:
        w_match = re.search(r'\bwidth\s*=\s*["\']([\d.]+)', svg_text)
        h_match = re.search(r'\bheight\s*=\s*["\']([\d.]+)', svg_text)
        if w_match and h_match:
            w, h = float(w_match.group(1)), float(h_match.group(1))
            new_vb = f"{crop} {crop} {w - 2 * crop} {h - 2 * crop}"
            svg_text = re.sub(r"(<svg\b)", rf'\1 viewBox="{new_vb}"', svg_text, count=1)

    def fix_root(match: re.Match) -> str:
        tag = match.group(0)
        tag = re.sub(r'\s+width\s*=\s*["\'][^"\']*["\']', "", tag, count=1)
        tag = re.sub(r'\s+height\s*=\s*["\'][^"\']*["\']', "", tag, count=1)
        if "preserveAspectRatio" not in tag:
            tag = re.sub(r"(<svg\b)", r'\1 preserveAspectRatio="xMidYMid meet"', tag, count=1)
        return tag

    return re.sub(r"<svg\b[^>]*>", fix_root, svg_text, count=1)


class LoadingWindow:
    def __init__(self, app, on_complete):
        self.app = app
        self.on_complete = on_complete
        self._window_shown = False
        self._dom_check_started = False
        self._progress = 0
        self._status = ""
        self._loop: asyncio.AbstractEventLoop | None = None

        # Загружаем конфигурацию и применяем значения.
        self.app.config = Config()
        self._apply_config()

        os.environ["WEBVIEW2_DEFAULT_BACKGROUND_COLOR"] = BG_COLOR_WEBVIEW

        window_size = (300, 400)
        self.window = toga.Window(
            title="Loading",
            size=window_size,
            resizable=False,
        )
        # На GTK окно ещё не realized → get_window() is None → screen_position падает.
        self._center_window(self.window, window_size, app)
        try:
            self.window.hide()
        except Exception:
            pass
        self._apply_dark_titlebar()
        self._apply_native_backcolor(getattr(self.window, "_impl", None) and self.window._impl.native)

        self.webview = toga.WebView(
            style=Pack(flex=1),
            on_webview_load=self.on_webview_loaded,
        )

        main_box = toga.Box(
            children=[self.webview],
            style=Pack(direction=COLUMN, background_color=rgb(*BG_COLOR), flex=1),
        )
        self._apply_native_backcolor(main_box._impl.native)
        self.window.content = main_box
        self.load_svg_animation()

    @staticmethod
    def _center_window(window, window_size, app) -> None:
        """Центрирует окно. На GTK до realize screen_position может кидать TypeError."""
        try:
            screens = getattr(app, "screens", None) or []
            if not screens:
                return
            screen = screens[0]
            pos = (
                (screen.size.width - window_size[0]) // 2,
                (screen.size.height - window_size[1]) // 2,
            )
            window.screen_position = pos
        except Exception as exc:  # noqa: BLE001
            logger.debug("center window skipped: %s", exc)
            try:
                # фолбэк: абсолютная позиция (может проигнорироваться до show)
                window.position = (
                    max(0, (1920 - window_size[0]) // 2),
                    max(0, (1080 - window_size[1]) // 2),
                )
            except Exception:
                pass

    def _apply_config(self) -> None:
        """Применяет значения из конфигурации к поведению приложения."""
        mode = self.app.config.mode
        logger.info("Режим работы из конфига: %s", mode)
        # TODO: применить mode к ядру (настройка TUN/прокси/обоих).
        # Пока просто фиксируем — в следующих commit'ах тут будет вызов ядра.

    def _apply_dark_titlebar(self):
        if sys.platform != "win32":
            return
        try:
            hwnd = self.window._impl.native.Handle.ToInt32()
            dwm = ctypes.windll.dwmapi
            value = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось применить тёмный заголовок: %s", exc)

    @staticmethod
    def _apply_native_backcolor(native):
        if sys.platform != "win32" or native is None:
            return
        try:
            native.BackColor = Color.FromArgb(*BG_COLOR)
        except Exception as exc:  # noqa: BLE001
            logger.debug("BackColor не применился: %s", exc)

    def on_webview_loaded(self, widget, **kwargs):
        if not self._dom_check_started:
            self._dom_check_started = True
            # Используем asyncio.create_task вместо self.app.loop.create_task
            asyncio.create_task(self._wait_for_dom_and_show())

    async def _wait_for_dom_and_show(self, timeout: float = 5.0):
        self._loop = asyncio.get_running_loop()
        elapsed = 0.0
        step = 0.25
        while elapsed < timeout:
            try:
                ready = await self.webview.evaluate_javascript(
                    "document.body.dataset.svgReady === 'true'"
                )
                if ready:
                    break
            except (RuntimeError, ValueError) as exc:
                logger.debug("WebView DOM is not ready yet: %s", exc)
            await asyncio.sleep(step)
            elapsed += step

        if not self._window_shown:
            self._window_shown = True
            self.window.show()
            await self._apply_progress()
            await self._apply_status()
        if self.on_complete:
            self.on_complete()

    # ── Планирование JS-вызовов из любого потока ───────────────────────
    def _schedule_js(self, js: str) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._eval_js(js))
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("schedule_js failed: %s", exc)

    async def _eval_js(self, js: str) -> None:
        try:
            # выражение с return — надёжнее на WebKitGTK, чем «голый» statement
            wrapped = "(function(){" + js + "; return true;})()"
            await self.webview.evaluate_javascript(wrapped)
        except Exception as exc:  # noqa: BLE001
            logger.debug("JS eval failed: %s", exc)

    # ── Публичное API для прогресса/статуса ────────────────────────────
    def set_progress(self, value: int) -> None:
        """Запоминает прогресс и обновляет индикатор (потокобезопасно)."""
        self._progress = max(0, min(100, int(value)))
        if self._window_shown:
            self._schedule_js(
                f"if(window.setProgress) window.setProgress({self._progress})"
            )

    def set_status(self, text: str) -> None:
        """Обновляет подпись под прогресс-баром (потокобезопасно)."""
        self._status = text or ""
        # WebKitGTK иногда не применяет DOM-подпись — дублируем в title окна
        try:
            self.window.title = (self._status[:80] if self._status else "Loading")
        except Exception:
            pass
        if self._window_shown:
            self._schedule_js(
                "if(window.setStatus) window.setStatus("
                + json.dumps(self._status, ensure_ascii=False) + ")"
            )

    async def _apply_progress(self) -> None:
        try:
            await self._eval_js(
                f"if(window.setProgress) window.setProgress({self._progress})"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Не удалось обновить прогресс загрузки: %s", exc)

    async def _apply_status(self) -> None:
        try:
            try:
                self.window.title = (self._status[:80] if self._status else "Loading")
            except Exception:
                pass
            await self._eval_js(
                "if(window.setStatus) window.setStatus("
                + json.dumps(self._status or "", ensure_ascii=False) + ")"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Не удалось обновить статус загрузки: %s", exc)

    # ── Этапы загрузки ─────────────────────────────────────────────────
    async def run_download_stages(self) -> None:
        """Скачивает Xray, Wintun, geoip, geosite с прогрессом и подписями."""
        base = Path(__file__).resolve().parent
        bin_dir = base / "bin"
        geo_dir = base / "geo"

        async def run_stage(
            start: int,
            end: int,
            text: str,
            work,  # callable(progress_cb) -> result, выполнится в отдельном потоке
        ) -> None:
            logger.info("Этап: %s (%d%%–%d%%)", text, start, end)
            self._status = text
            self._progress = start
            # сразу ждём отрисовку (не только fire-and-forget schedule)
            await self._apply_status()
            await self._apply_progress()
            await asyncio.sleep(0.08)

            def stage_cb(downloaded: int, total: int) -> None:
                if total <= 0:
                    # Размер неизвестен — просто покажем, что работаем.
                    return
                frac = min(1.0, max(0.0, downloaded / total))
                pct = int(start + (end - start) * frac)
                self.set_progress(pct)

            def worker():
                return work(stage_cb)

            try:
                await asyncio.to_thread(worker)
            except Exception as exc:  # noqa: BLE001
                logger.error("Этап '%s' упал: %s", text, exc, exc_info=True)
                raise
            self.set_progress(end)

        try:
            await run_stage(
                0, 40,
                "Скачиваю Xray core",
                lambda cb: download_xray(bin_dir, progress_cb=cb),
            )
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"Не удалось скачать Xray core: {exc}")

        # Wintun нужен только на Windows (TUN-драйвер).
        if sys.platform == "win32":
            try:
                await run_stage(
                    40, 55,
                    "Скачиваю Wintun",
                    lambda cb: download_wintun(bin_dir, progress_cb=cb),
                )
            except Exception as exc:  # noqa: BLE001
                self.set_status(f"Не удалось скачать Wintun: {exc}")
        else:
            self.set_progress(55)

        try:
            await run_stage(
                55, 75,
                "Скачиваю geoip.dat",
                lambda cb: download_geofile(
                    GEOIP_URL, geo_dir / "geoip.dat", "geoip.dat", cb,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"Не удалось скачать geoip.dat: {exc}")

        try:
            await run_stage(
                75, 95,
                "Скачиваю geosite.dat",
                lambda cb: download_geofile(
                    GEOSITE_URL, geo_dir / "geosite.dat", "geosite.dat", cb,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"Не удалось скачать geosite.dat: {exc}")

        self.set_status("Подготовка завершена")
        self.set_progress(95)

    def load_svg_animation(self):
        svg_path = self.app.paths.app / "assets" / "startup-animation500x500.svg"
        try:
            raw_svg = svg_path.read_text(encoding="utf-8")
        except OSError:
            # Фолбэк, если ассета нет
            raw_svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
                '<circle cx="100" cy="100" r="80" fill="#6c5ce7" />'
                '<text x="100" y="110" fill="#ffffff" text-anchor="middle" '
                'font-family="sans-serif" font-size="20">Loading...</text></svg>'
            )

        svg = crop_svg(raw_svg, crop=50)

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body {{
    margin: 0; padding: 0; height: 100%; width: 100%;
    background: {BG_COLOR_CSS}; overflow: hidden; font-family: sans-serif;
  }}
  body {{ display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; }}
  .stage {{ flex: 1; width: 100%; display: flex; align-items: center; justify-content: center; overflow: hidden; visibility: hidden; }}
  .stage > svg {{ max-width: 100%; max-height: 100%; width: auto; height: auto; display: block; }}
  .progress-container {{
      width: 60%; max-width: 200px; height: 6px; background: #3a3a3a;
      border-radius: 3px; margin: 0 0 10px 0; overflow: hidden;
  }}
  .progress-bar {{ width: 0%; height: 100%; background: #6c5ce7; border-radius: 3px; transition: width 0.25s linear; }}
  .status-text {{
      font-size: 13px; color: #d0d0d0; min-height: 18px;
      max-width: 92%; text-align: center; padding: 4px 12px 20px 12px;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      letter-spacing: 0.2px;
      flex-shrink: 0;
  }}
</style>
</head>
<body>
  <div class="stage" id="stage">{svg}</div>
  <div class="progress-container">
      <div class="progress-bar" id="progressBar"></div>
  </div>
  <div class="status-text" id="statusText"></div>
  <script>
    window.addEventListener('load', function() {{
      var stage = document.getElementById('stage');
      var svg = stage.querySelector('svg');
      if (svg && typeof svg.pauseAnimations === 'function') {{ svg.pauseAnimations(); }}
      stage.style.visibility = 'visible';
      if (svg && typeof svg.unpauseAnimations === 'function') {{ svg.unpauseAnimations(); }}
      document.body.dataset.svgReady = 'true';
    }});

    window.setProgress = function(value) {{
      var progress = Math.max(0, Math.min(100, value));
      document.getElementById('progressBar').style.width = progress + '%';
    }};

    window.setStatus = function(text) {{
      var el = document.getElementById('statusText');
      if (el) el.textContent = text || '';
    }};
  </script>
</body>
</html>"""
        self.webview.set_content("about:blank", html)