"""Сторож UI-потока.

UI-поток приложения (WinForms + asyncio loop) раз в секунду отмечается
heartbeat'ом. Если отметок нет дольше порога — поток заблокирован в нативном
коде: Windows рисует «не отвечает», но собственный диалог может так и не
показать. Сторож живёт в отдельном STA-потоке со своим message loop, поэтому
показывает окно даже когда основной UI мёртв.

Пользователю предлагается два действия: перезапустить приложение или
отменить (оставить как есть).

Сон и гибернация не считаются зависанием: пока компьютер спит, UI-поток
заморожен, и heartbeat'ов нет. Поэтому время простоя меряется часами,
которые не тикают во сне (QueryUnbiasedInterruptTime), а пробуждение
дополнительно детектируется по расхождению с обычными часами.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

HANG_THRESHOLD = 7.0
CHECK_INTERVAL = 1.0
REPEAT_AFTER = 60.0
# Расхождение «обычных» часов и часов без сна, которое считаем пробуждением.
# Оба источника тикают одинаково вне сна, поэтому порог можно держать низким.
SUSPEND_GAP = 2.0
# Насколько сам сторож должен «проспать», чтобы это считалось заморозкой
# всего процесса (Modern Standby), а не нагрузкой на машину.
PROCESS_FREEZE_GAP = 5.0


def _make_clock() -> Callable[[], float]:
    """Монотонные часы, не считающие время сна/гибернации.

    time.monotonic() на Windows — это GetTickCount64(), он тикает и во сне.
    После пробуждения разница с последним heartbeat'ом равна длительности сна,
    и сторож решает, что UI завис на часы. QueryUnbiasedInterruptTime время сна
    не считает, поэтому такой ложной тревоги не будет.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            query = kernel32.QueryUnbiasedInterruptTime
            query.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
            query.restype = ctypes.c_int

            def _unbiased() -> float:
                # Буфер создаётся на каждый вызов: clock зовут из UI-потока
                # (beat) и из потока сторожа одновременно.
                buf = ctypes.c_ulonglong(0)
                try:
                    if not query(ctypes.byref(buf)):
                        raise OSError(
                            ctypes.get_last_error(), "QueryUnbiasedInterruptTime"
                        )
                    return buf.value / 10_000_000.0
                except Exception:
                    # Сбой вызова не должен ронять heartbeat UI-потока.
                    return time.monotonic()

            _unbiased()  # проверяем, что вызов работает
            return _unbiased
        except Exception as e:
            logger.debug("watchdog: часы без сна недоступны: %s", e)
    return time.monotonic


class UIWatchdog:
    def __init__(
        self,
        app_dir: Path | str,
        on_restart: Callable[[], None] | None = None,
        threshold: float = HANG_THRESHOLD,
    ) -> None:
        self._app_dir = Path(app_dir)
        self._on_restart = on_restart
        self._threshold = threshold
        self._clock = _make_clock()
        self._last_beat = self._clock()
        self._last_wall = time.monotonic()
        self._last_clock = self._last_beat
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._dialog_open = False
        self._last_shown = float("-inf")

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def beat(self) -> None:
        """Отметка «UI жив». Вызывается из event loop'а (он на UI-потоке)."""
        self._last_beat = self._clock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._last_beat = self._clock()
        self._last_wall = time.monotonic()
        self._last_clock = self._last_beat
        self._thread = threading.Thread(
            target=self._run, name="ui-watchdog", daemon=True
        )
        self._thread.start()
        logger.info("UI watchdog запущен (порог %.0f c)", self._threshold)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Наблюдение
    # ------------------------------------------------------------------
    def _resumed_from_sleep(self) -> bool:
        """True, если между итерациями компьютер был в сне.

        Обычные часы (time.monotonic) во сне тикают, часы без сна — нет.
        Расхождение между ними и есть длительность сна.
        """
        wall = time.monotonic()
        now = self._clock()
        slept = (wall - self._last_wall) - (now - self._last_clock)
        self._last_wall = wall
        self._last_clock = now
        if slept <= SUSPEND_GAP:
            return False
        logger.info("watchdog: пробуждение после сна (%.0f c) — сброс таймера", slept)
        # UI-поток ещё не успел проснуться и дать heartbeat — даём ему шанс.
        # max() — чтобы не откатить отметку, поставленную UI-потоком только что.
        self._last_beat = max(self._last_beat, now)
        return True

    def _run(self) -> None:
        last_tick = self._clock()
        while not self._stop.wait(CHECK_INTERVAL):
            now = self._clock()
            # Сторож сам проспал больше своего интервала — заморожен весь
            # процесс (Modern Standby, DAM), а не UI-поток. Это не зависание.
            if now - last_tick > CHECK_INTERVAL + PROCESS_FREEZE_GAP:
                logger.info(
                    "watchdog: процесс был приостановлен (%.0f c) — сброс таймера",
                    now - last_tick,
                )
                self._last_beat = max(self._last_beat, now)
                self._last_wall = time.monotonic()
                self._last_clock = now
                last_tick = now
                continue
            last_tick = now
            if self._resumed_from_sleep():
                continue
            idle = now - self._last_beat
            if idle < self._threshold:
                continue
            with self._lock:
                if self._dialog_open:
                    continue
                if time.monotonic() - self._last_shown < REPEAT_AFTER:
                    continue
                self._dialog_open = True
                self._last_shown = time.monotonic()

            logger.warning("UI не отвечает %.1f c — показываю диалог", idle)
            self._dump_stacks(idle)
            try:
                self._show_dialog(idle)
            except Exception as e:
                logger.warning("watchdog: диалог не показан: %s", e)
            finally:
                with self._lock:
                    self._dialog_open = False
                # после закрытия диалога даём UI шанс ожить
                self._last_beat = max(self._last_beat, self._clock())
                self._last_wall = time.monotonic()
                self._last_clock = self._last_beat

    def _dump_stacks(self, idle: float) -> None:
        """Пишет стеки всех потоков — иначе зависание в логах не видно."""
        try:
            names = {t.ident: t.name for t in threading.enumerate()}
            lines = [
                f"=== UI hang {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(idle {idle:.1f}s) ==="
            ]
            for ident, frame in sys._current_frames().items():
                lines.append(f"\n--- thread {names.get(ident, ident)} ---")
                lines.extend(traceback.format_stack(frame))
            path = self._app_dir / "logs" / "ui-hang.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            logger.warning("watchdog: стеки потоков записаны в %s", path)
        except Exception as e:
            logger.debug("watchdog: dump stacks failed: %s", e)

    # ------------------------------------------------------------------
    # Диалог
    # ------------------------------------------------------------------
    def _show_dialog(self, idle: float) -> None:
        if sys.platform != "win32":
            return
        try:
            import clr  # type: ignore

            clr.AddReference("System.Windows.Forms")
            clr.AddReference("System.Drawing")
            from System.Threading import (  # type: ignore
                ApartmentState,
                ThreadStart,
            )
            from System.Threading import (
                Thread as NetThread,
            )
        except Exception as e:
            logger.warning("watchdog: WinForms недоступны: %s", e)
            return

        done = threading.Event()

        def ui() -> None:
            try:
                self._build_dialog(idle)
            except Exception as e:
                logger.warning("watchdog: окно не создано: %s", e)
            finally:
                done.set()

        thread = NetThread(ThreadStart(ui))
        thread.SetApartmentState(ApartmentState.STA)
        thread.IsBackground = True
        thread.Start()
        done.wait()

    def _build_dialog(self, idle: float) -> None:
        import System.Windows.Forms as WinForms  # type: ignore
        from System.Drawing import Color, Size  # type: ignore

        form = WinForms.Form()
        form.Text = "Obxod-BOX — интерфейс не отвечает"
        form.FormBorderStyle = WinForms.FormBorderStyle.FixedDialog
        form.StartPosition = WinForms.FormStartPosition.CenterScreen
        form.MaximizeBox = False
        form.MinimizeBox = False
        form.ShowInTaskbar = True
        form.TopMost = True
        form.ClientSize = Size(470, 210)
        form.BackColor = Color.FromArgb(45, 45, 45)
        form.ForeColor = Color.White

        label = WinForms.Label()
        label.Text = (
            "Похоже, интерфейс приложения перестал отвечать.\n\n"
            f"UI не реагирует уже {int(idle)} с. Ядро и туннель при этом "
            "продолжают работать.\n\n"
            "Перезапустить приложение?"
        )
        label.SetBounds(20, 18, 430, 110)
        label.ForeColor = Color.White
        form.Controls.Add(label)

        btn_restart = WinForms.Button()
        btn_restart.Text = "Перезапустить"
        btn_restart.SetBounds(240, 150, 140, 36)
        btn_restart.DialogResult = WinForms.DialogResult.OK
        form.Controls.Add(btn_restart)

        btn_cancel = WinForms.Button()
        btn_cancel.Text = "Отмена"
        btn_cancel.SetBounds(390, 150, 60, 36)
        btn_cancel.DialogResult = WinForms.DialogResult.Cancel
        form.Controls.Add(btn_cancel)

        form.AcceptButton = btn_restart
        form.CancelButton = btn_cancel

        result = form.ShowDialog()
        form.Dispose()

        if result == WinForms.DialogResult.OK:
            self._restart()

    # ------------------------------------------------------------------
    # Перезапуск
    # ------------------------------------------------------------------
    def _restart(self) -> None:
        logger.warning("watchdog: пользователь запросил перезапуск")
        if self._on_restart is not None:
            try:
                self._on_restart()
            except Exception as e:
                logger.warning("watchdog: on_restart упал: %s", e)
        try:
            self._spawn_new_instance()
        except Exception as e:
            logger.error("watchdog: не удалось запустить новую копию: %s", e)
        # supervisor увидит смерть родителя и погасит xray сам
        os._exit(0)

    def _spawn_new_instance(self) -> None:
        if getattr(sys, "frozen", False):
            args = [sys.executable]
        else:
            main_py = self._app_dir / "main.py"
            if not main_py.exists():
                raise FileNotFoundError(main_py)
            args = [sys.executable, str(main_py)]

        # Новая копия ждёт смерти текущего процесса: WebView2 не терпит
        # два процесса на одной user data folder.
        args += ["--wait-pid", str(os.getpid())]

        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )

        subprocess.Popen(
            args,
            cwd=str(self._app_dir),
            close_fds=True,
            creationflags=creationflags,
        )
        logger.info("watchdog: запущена новая копия приложения")
