import asyncio
import logging
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import toga

from loading_window import LoadingWindow
from main_window import MainWindow
from net_utils import is_admin, relaunch_as_admin, was_elevated_attempt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class App(toga.App):
    def startup(self):
        self.loading_window = LoadingWindow(self, self.on_loading_complete)
        self._main_window_ready = False
        self._finish_task = None
        self._startup_stages_task = None
        self._main_window_wrapper = None
        self.main_window = None

        # Крестик на main_window в Toga = запрос выхода из app.
        # Без on_exit приложение либо гасится, либо окно ломается.
        self.on_exit = self._on_app_exit

    def _on_app_exit(self, app, **kwargs):
        """True — выйти; False — остаться в трее."""
        wrapper = getattr(self, "_main_window_wrapper", None)
        if wrapper is not None and not getattr(wrapper, "_user_requested_exit", False):
            logger.info("on_exit: сворачиваем в трей, app не завершаем")
            try:
                wrapper.request_hide_to_tray()
            except Exception as e:
                logger.warning("hide on exit failed: %s", e)
            return False
        logger.info("on_exit: пользователь запросил выход")
        if wrapper is not None:
            try:
                wrapper.prepare_for_exit()
            except Exception as e:
                logger.debug("prepare_for_exit: %s", e)
        return True

    def on_loading_complete(self):
        """DOM загрузочного окна готов — запускаем этапы скачивания."""
        if self.loading_window is None:
            return
        if self._startup_stages_task is None:
            self._startup_stages_task = asyncio.create_task(
                self._run_startup_stages()
            )

    async def _run_startup_stages(self):
        loading_window = self.loading_window
        if loading_window is None:
            return

        try:
            await loading_window.run_download_stages()
        except Exception as e:
            logger.error("Этапы загрузки упали: %s", e, exc_info=True)

        # На случай, если за время скачивания окно уже закрыли
        if self.loading_window is None:
            return

        # Теперь создаём главное окно
        try:
            self._main_window_wrapper = MainWindow(self, self.on_main_window_ready)
            self.main_window = self._main_window_wrapper.window
        except Exception as e:
            logger.error("Не удалось создать главное окно: %s", e, exc_info=True)
            try:
                loading_window.set_status(f"Ошибка запуска: {e}")
            except Exception:
                pass

    def on_main_window_ready(self):
        if not self._main_window_ready:
            self._main_window_ready = True
            logger.info("WebView главного окна загружен. Открываем приложение...")
            wrapper = self._main_window_wrapper
            if wrapper is not None:
                wrapper.window.show()
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._finish_loading())

    async def _finish_loading(self):
        loading_window = self.loading_window
        if loading_window is None:
            return
        loading_window.set_progress(100)
        loading_window.set_status("Готово")
        await loading_window._apply_progress()
        await loading_window._apply_status()
        # маленькая пауза, чтобы пользователь увидел 100%
        await asyncio.sleep(0.25)
        try:
            loading_window.window.close()
        except Exception as e:
            logger.debug("loading window close failed: %s", e)
        self.loading_window = None


def _ensure_admin() -> None:
    """На Windows запрашивает UAC через ShellExecuteW(runas)."""
    if sys.platform != "win32":
        return
    if is_admin():
        logger.info("Запуск с правами администратора")
        return

    if was_elevated_attempt():
        logger.warning(
            "Повышение прав уже запрашивалось. "
            "TUN-режим будет недоступен, PROXY будет работать."
        )
        return

    logger.info("Нет прав администратора — запрашиваю UAC...")
    if relaunch_as_admin():
        sys.exit(0)

    logger.warning(
        "UAC отклонён или не удался. "
        "TUN-режим будет недоступен, PROXY будет работать."
    )


def main():
    _ensure_admin()
    return App("Obxod-BOX", "org.obxod.box")


if __name__ == "__main__":
    main().main_loop()
    print("приложение завершило работу")