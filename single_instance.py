"""Гарантия единственного экземпляра приложения.

Второй запуск не создаёт новое окно: он просит уже работающий экземпляр
развернуться и завершается. Если работающий экземпляр не отвечает,
пользователю предлагается его перезапустить.

Мультипроцессинга, без которого клиент не живёт, нет: supervisor и xray
запускаются по требованию и переживают перезапуск UI. Поэтому второй
экземпляр всегда только разворачивает первый.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MUTEX_NAME = "Local\\Obxod-BOX-single-instance"
ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5

_mutex_handle: int | None = None


# ----------------------------------------------------------------------
# Общие пути / процессы
# ----------------------------------------------------------------------
def _instance_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Obxod-BOX" / "instance.json"


def _read_instance() -> dict | None:
    try:
        return json.loads(_instance_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_instance(data: dict) -> None:
    path = _instance_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:
        logger.debug("instance file write failed: %s", e)


def _remove_instance() -> None:
    try:
        _instance_file().unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def terminate_pid(pid: int, timeout: float = 5.0) -> bool:
    """Завершает процесс и ждёт его смерти."""
    if not pid or pid == os.getpid():
        return False
    if sys.platform == "win32":
        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            logger.warning("terminate: OpenProcess(%d) failed: %s",
                           pid, ctypes.get_last_error())
            return False
        try:
            kernel32.TerminateProcess(ctypes.c_void_p(handle), 1)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


# ----------------------------------------------------------------------
# Мьютекс
# ----------------------------------------------------------------------
def acquire(retries: int = 0, delay: float = 0.2) -> bool:
    """True — мы первый экземпляр. False — приложение уже запущено.

    retries нужен после terminate_pid: ядро освобождает мьютекс не мгновенно.
    """
    global _mutex_handle
    if sys.platform != "win32":
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p,
    ]

    for attempt in range(retries + 1):
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        err = ctypes.get_last_error()

        if not handle:
            if err == ERROR_ACCESS_DENIED:
                # Мьютекс есть, но создан с другими правами — значит, уже запущено
                return False
            logger.warning("CreateMutexW failed (%s) — работаем без защиты", err)
            return True

        if err != ERROR_ALREADY_EXISTS:
            _mutex_handle = handle
            return True

        kernel32.CloseHandle(ctypes.c_void_p(handle))
        if attempt < retries:
            time.sleep(delay)

    return False


def release() -> None:
    global _mutex_handle
    if _mutex_handle is None or sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(ctypes.c_void_p(_mutex_handle))
    except Exception as e:
        logger.debug("mutex release failed: %s", e)
    _mutex_handle = None


# ----------------------------------------------------------------------
# Канал «развернись»
# ----------------------------------------------------------------------
class InstanceServer:
    """Слушает localhost и по команде restore поднимает окно.

    on_restore должен вернуть True, только если UI-поток реально выполнил
    разворачивание. Иначе второй экземпляр считает приложение зависшим.
    """

    def __init__(self, on_restore: Callable[[], bool]) -> None:
        self._on_restore = on_restore
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(4)
            sock.settimeout(0.5)
        except OSError as e:
            logger.warning("instance server не запущен: %s", e)
            return

        self._sock = sock
        port = sock.getsockname()[1]
        _write_instance({"pid": os.getpid(), "port": port})
        self._thread = threading.Thread(
            target=self._serve, name="instance-server", daemon=True
        )
        self._thread.start()
        logger.info("instance server слушает 127.0.0.1:%d", port)

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        _remove_instance()

    def _serve(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.settimeout(2.0)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                cmd = ""
                try:
                    cmd = str(json.loads(buf.split(b"\n", 1)[0]).get("cmd") or "")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

                if cmd == "restore":
                    logger.info("Запрос на разворачивание окна из второго экземпляра")
                    ok = False
                    try:
                        ok = bool(self._on_restore())
                    except Exception as e:
                        logger.warning("restore callback failed: %s", e)
                    conn.sendall(
                        b'{"ok": true}\n' if ok else b'{"ok": false}\n'
                    )
                else:
                    conn.sendall(b'{"ok": false}\n')
            except OSError as e:
                logger.debug("instance server conn: %s", e)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass


def request_restore(timeout: float = 3.0) -> bool:
    """Просит работающий экземпляр развернуться. True — он ответил."""
    info = _read_instance()
    if not info:
        logger.info("instance.json нет — работающий экземпляр не найден")
        return False

    pid = info.get("pid")
    port = info.get("port")
    if not isinstance(pid, int) or not isinstance(port, int):
        return False
    if not pid_alive(pid):
        logger.info("instance.json устарел (pid=%s мёртв)", pid)
        _remove_instance()
        return False

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b'{"cmd": "restore"}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except OSError as e:
        logger.warning("работающий экземпляр (pid=%s) не отвечает: %s", pid, e)
        return False

    try:
        return bool(json.loads(buf.split(b"\n", 1)[0]).get("ok"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def stale_pid() -> int | None:
    info = _read_instance()
    if not info:
        return None
    pid = info.get("pid")
    if isinstance(pid, int) and pid_alive(pid):
        return pid
    return None


# ----------------------------------------------------------------------
# Диалог
# ----------------------------------------------------------------------
def ask_restart_stale() -> bool:
    """Спрашивает, перезапускать ли неотвечающий экземпляр."""
    if sys.platform != "win32":
        return False
    try:
        import clr  # type: ignore

        clr.AddReference("System.Windows.Forms")
        from System.Threading import (  # type: ignore
            ApartmentState,
            Thread as NetThread,
            ThreadStart,
        )
    except Exception as e:
        logger.warning("диалог недоступен: %s", e)
        return False

    answer = {"yes": False}
    done = threading.Event()

    def ui() -> None:
        try:
            import System.Windows.Forms as WinForms  # type: ignore

            result = WinForms.MessageBox.Show(
                "Obxod-BOX уже запущен, но не отвечает.\n\n"
                "Завершить его и запустить заново?",
                "Obxod-BOX",
                WinForms.MessageBoxButtons.YesNo,
                WinForms.MessageBoxIcon.Warning,
            )
            answer["yes"] = result == WinForms.DialogResult.Yes
        except Exception as e:
            logger.warning("диалог не показан: %s", e)
        finally:
            done.set()

    thread = NetThread(ThreadStart(ui))
    thread.SetApartmentState(ApartmentState.STA)
    thread.IsBackground = True
    thread.Start()
    done.wait()
    return answer["yes"]


def show_already_running() -> None:
    """Сообщает, что приложение уже запущено (pid неизвестен)."""
    if sys.platform != "win32":
        return
    try:
        import clr  # type: ignore

        clr.AddReference("System.Windows.Forms")
        from System.Threading import (  # type: ignore
            ApartmentState,
            Thread as NetThread,
            ThreadStart,
        )
    except Exception as e:
        logger.warning("диалог недоступен: %s", e)
        return

    done = threading.Event()

    def ui() -> None:
        try:
            import System.Windows.Forms as WinForms  # type: ignore

            WinForms.MessageBox.Show(
                "Obxod-BOX уже запущен.\n\n"
                "Окно приложения должно быть на панели задач или в трее.",
                "Obxod-BOX",
                WinForms.MessageBoxButtons.OK,
                WinForms.MessageBoxIcon.Information,
            )
        except Exception as e:
            logger.warning("диалог не показан: %s", e)
        finally:
            done.set()

    thread = NetThread(ThreadStart(ui))
    thread.SetApartmentState(ApartmentState.STA)
    thread.IsBackground = True
    thread.Start()
    done.wait()
