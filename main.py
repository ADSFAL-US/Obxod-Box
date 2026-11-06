import asyncio
import atexit
import faulthandler
import logging
import os
import sys
import threading
import time
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

_LOG_DIR = _APP_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# В silent-режиме (pythonw) stderr нет — падение нативного кода иначе не увидеть.
try:
    _FAULT_FILE = (_LOG_DIR / "fault.log").open("a", encoding="utf-8")
    faulthandler.enable(file=_FAULT_FILE, all_threads=True)
except Exception as _e:  # pragma: no cover
    logger.warning("faulthandler не включён: %s", _e)

# ВАЖНО: до импорта toga. Toga загружает CLR при импорте и запоминает
# выбранный рантайм; чужой DOTNET_ROOT уводит hostfxr на старую установку
# без нужной версии, и Toga молча откатывается на .NET Framework.
import dotnet_runtime  # noqa: E402

dotnet_runtime.prepare_env()

# CLR нужно выбрать до импорта наших модулей: они делают `import clr`
# на уровне модуля, а pythonnet фиксирует рантайм при первой загрузке.
dotnet_runtime.select_runtime()

import toga  # noqa: E402

import single_instance  # noqa: E402
from loading_window import LoadingWindow  # noqa: E402
from main_window import MainWindow  # noqa: E402
from net_utils import is_admin, relaunch_as_admin, was_elevated_attempt  # noqa: E402
from ui_watchdog import UIWatchdog  # noqa: E402


class App(toga.App):
    def startup(self):
        self.loading_window = LoadingWindow(self, self.on_loading_complete)
        self._main_window_ready = False
        self._finish_task = None
        self._startup_stages_task = None
        self._main_window_wrapper = None
        self.main_window = None
        self._watchdog = None
        self._heartbeat_task = None
        self._instance_server = None

        # Крестик на main_window в Toga = запрос выхода из app.
        # Без on_exit приложение либо гасится, либо окно ломается.
        self.on_exit = self._on_app_exit

        self._start_instance_server()
        self._start_watchdog()

    # ------------------------------------------------------------------
    # Единственный экземпляр
    # ------------------------------------------------------------------
    def _start_instance_server(self) -> None:
        """Слушает запросы второго запуска: «разверни окно»."""
        try:
            self._instance_server = single_instance.InstanceServer(
                self._restore_from_second_instance
            )
            self._instance_server.start()
        except Exception as e:
            logger.warning("instance server не запущен: %s", e)

    def _restore_from_second_instance(self) -> bool:
        """Вызывается из потока сервера. True — UI реально развернул окно."""
        wrapper = getattr(self, "_main_window_wrapper", None)
        if wrapper is None:
            # Приложение ещё стартует — окно появится само
            logger.info("Второй экземпляр: главное окно ещё не создано")
            return True

        done = threading.Event()
        result = {"ok": False}

        def _do() -> None:
            try:
                wrapper._restore_from_tray()
                result["ok"] = True
            except Exception as e:
                logger.warning("restore from second instance failed: %s", e)
            finally:
                done.set()

        wrapper._ui_deferred(_do)
        # UI-поток завис — BeginInvoke не выполнится, и мы это увидим
        if not done.wait(timeout=3.0):
            logger.warning("UI не ответил на запрос разворачивания за 3 c")
            return False
        return result["ok"]

    # ------------------------------------------------------------------
    # Сторож UI-потока
    # ------------------------------------------------------------------
    def _start_watchdog(self) -> None:
        try:
            self._watchdog = UIWatchdog(_APP_DIR, on_restart=self._watchdog_restart)
            self._watchdog.start()
            self._heartbeat_task = asyncio.create_task(self._heartbeat())
        except Exception as e:
            logger.warning("UI watchdog не запущен: %s", e)

    async def _heartbeat(self) -> None:
        """Тикает только пока event loop (он же UI-поток) жив."""
        while True:
            watchdog = self._watchdog
            if watchdog is not None:
                watchdog.beat()
            await asyncio.sleep(0.5)

    def _watchdog_restart(self) -> None:
        """Вызывается из потока сторожа, когда UI уже мёртв."""
        wrapper = getattr(self, "_main_window_wrapper", None)
        manager = getattr(wrapper, "xray_manager", None)
        if manager is None:
            return
        try:
            t = threading.Thread(target=manager.stop, name="xray-stop-restart", daemon=True)
            t.start()
            t.join(timeout=3.0)
        except Exception as e:
            logger.warning("watchdog: остановка Xray не удалась: %s", e)

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
            except Exception as exc:
                logger.debug("_run_startup_stages: %s", exc)

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


def _wait_for_previous_instance() -> None:
    """Ждём выхода старого процесса после аварийного перезапуска.

    WebView2 не поддерживает два процесса на одной user data folder, поэтому
    новая копия не должна стартовать раньше, чем умрёт старая.
    """
    pid = None
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--wait-pid" and i + 1 < len(argv):
            try:
                pid = int(argv[i + 1])
            except ValueError:
                pid = None
            break
        if arg.startswith("--wait-pid="):
            try:
                pid = int(arg.split("=", 1)[1])
            except ValueError:
                pid = None
            break

    if not pid or pid == os.getpid():
        return

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if not single_instance.pid_alive(pid):
            logger.info("Предыдущий экземпляр (pid=%d) завершился", pid)
            return
        time.sleep(0.2)
    logger.warning("Предыдущий экземпляр (pid=%d) не завершился за 15 c", pid)


def _handle_second_instance() -> bool:
    """Второй запуск: развернуть уже работающее окно.

    True — можно продолжать запуск (мы стали единственным экземпляром,
    например после перезапуска зависшего).
    """
    if single_instance.request_restore():
        logger.info("Приложение уже запущено — разворачиваю существующее окно")
        return False

    pid = single_instance.stale_pid()
    if pid is None:
        logger.warning(
            "Экземпляр уже запущен, но не отвечает и его pid неизвестен "
            "(возможно, запущена старая версия)"
        )
        single_instance.show_already_running()
        return False

    logger.warning("Экземпляр (pid=%d) не отвечает", pid)
    if not single_instance.ask_restart_stale():
        logger.info("Пользователь отказался перезапускать зависший экземпляр")
        return False

    if not single_instance.terminate_pid(pid):
        logger.error("Не удалось завершить зависший экземпляр (pid=%d)", pid)
        return False

    logger.info("Зависший экземпляр (pid=%d) завершён", pid)
    time.sleep(0.3)
    if not single_instance.acquire(retries=15, delay=0.2):
        logger.error("Не удалось стать единственным экземпляром после перезапуска")
        return False
    return True


def main():
    _wait_for_previous_instance()

    if not single_instance.acquire():
        if not _handle_second_instance():
            return None
    atexit.register(single_instance.release)

    _ensure_admin()
    return App("Obxod-BOX", "org.obxod.box")


if __name__ == "__main__":
    app = main()
    if app is None:
        sys.exit(0)
    app.main_loop()
    print("приложение завершило работу")
