"""Xray supervisor — отдельный процесс-надзиратель над xray.

Запускается главным приложением как detached process и становится
родителем xray. Если главное приложение умирает — supervisor убивает
xray и завершается сам.

Протокол общения с родителем: построчный JSON по TCP на 127.0.0.1:<port>.
Команды: ping / status / start / stop / restart / quit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("xray_supervisor")

PARENT_POLL_INTERVAL = 1.0


def _is_process_alive(pid: int) -> bool:
    """Проверяет, жив ли процесс по PID (без зомби)."""
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None

    if psutil is not None:
        try:
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    # Фолбэк без psutil
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class Supervisor:
    def __init__(
        self,
        parent_pid: int,
        port: int,
        xray_exe: Path,
        cwd: Path,
        geo_dir: Path,
    ):
        self.parent_pid = parent_pid
        self.port = port
        self.xray_exe = xray_exe
        self.cwd = cwd
        self.geo_dir = geo_dir

        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Управление xray
    # ------------------------------------------------------------------
    def _spawn_xray(self, config: dict) -> tuple[bool, str]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True, "already running"

            env = os.environ.copy()
            env["XRAY_LOCATION_ASSET"] = str(self.geo_dir.resolve())

            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NO_WINDOW

            try:
                self._proc = subprocess.Popen(
                    [str(self.xray_exe), "run", "-c", "stdin:"],
                    cwd=str(self.cwd),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            except Exception as e:
                self._proc = None
                return False, f"Popen failed: {e}"

            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(json.dumps(config, ensure_ascii=False))
                self._proc.stdin.flush()
                self._proc.stdin.close()
            except Exception as e:
                try:
                    self._proc.kill()
                except Exception:
                    pass
                self._proc = None
                return False, f"stdin failed: {e}"

            logger.info("xray started (pid=%s)", self._proc.pid)
            return True, f"pid={self._proc.pid}"

    def _stop_xray(self) -> tuple[bool, str]:
        with self._lock:
            proc = self._proc
            if proc is None:
                return True, "not running"
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    logger.warning("xray not exiting — kill()")
                    proc.kill()
                    proc.wait(timeout=1.5)
            except Exception as e:
                return False, str(e)
            finally:
                self._proc = None
            logger.info("xray stopped")
            return True, "stopped"

    def _xray_status(self) -> dict:
        with self._lock:
            proc = self._proc
            if proc is None:
                return {"running": False, "pid": None, "exit_code": None}
            rc = proc.poll()
            if rc is None:
                return {"running": True, "pid": proc.pid, "exit_code": None}
            return {"running": False, "pid": None, "exit_code": rc}

    # ------------------------------------------------------------------
    # Обработка команд от родителя
    # ------------------------------------------------------------------
    def _handle(self, cmd: dict) -> dict:
        action = cmd.get("cmd")
        if action == "ping":
            return {"ok": True, "xray": self._xray_status()}
        if action == "status":
            return {"ok": True, "xray": self._xray_status()}
        if action == "start":
            cfg = cmd.get("config")
            if not isinstance(cfg, dict):
                return {"ok": False, "error": "config required"}
            ok, msg = self._spawn_xray(cfg)
            return {"ok": ok, "message": msg, "xray": self._xray_status()}
        if action == "stop":
            ok, msg = self._stop_xray()
            return {"ok": ok, "message": msg}
        if action == "restart":
            self._stop_xray()
            time.sleep(0.2)
            cfg = cmd.get("config")
            if not isinstance(cfg, dict):
                return {"ok": False, "error": "config required"}
            ok, msg = self._spawn_xray(cfg)
            return {"ok": ok, "message": msg, "xray": self._xray_status()}
        if action == "quit":
            self._stop_xray()
            self._stop.set()
            return {"ok": True, "message": "shutting down"}
        return {"ok": False, "error": f"unknown command: {action!r}"}

    # ------------------------------------------------------------------
    # Watcher: жив ли родитель
    # ------------------------------------------------------------------
    def _watcher(self) -> None:
        while not self._stop.is_set():
            if not _is_process_alive(self.parent_pid):
                logger.warning(
                    "parent %d dead — killing xray and exiting", self.parent_pid
                )
                self._stop_xray()
                self._stop.set()
                break
            self._stop.wait(PARENT_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # TCP-сервер
    # ------------------------------------------------------------------
    def _serve(self, conn: socket.socket) -> None:
        try:
            buf = b""
            while not self._stop.is_set():
                try:
                    data = conn.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        cmd = json.loads(line.decode("utf-8"))
                    except Exception as e:
                        conn.sendall(
                            (json.dumps({"ok": False, "error": f"bad json: {e}"}) + "\n")
                            .encode("utf-8")
                        )
                        continue
                    try:
                        resp = self._handle(cmd)
                    except Exception as e:
                        logger.exception("handler error")
                        resp = {"ok": False, "error": str(e)}
                    try:
                        conn.sendall(
                            (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")
                        )
                    except OSError:
                        return
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def run(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", self.port))
        except OSError as e:
            logger.error("bind failed on 127.0.0.1:%d: %s", self.port, e)
            return
        srv.listen(4)
        srv.settimeout(1.0)

        logger.info(
            "listening on 127.0.0.1:%d, parent=%d", self.port, self.parent_pid
        )

        t = threading.Thread(target=self._watcher, daemon=True, name="parent-watcher")
        t.start()

        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                ct = threading.Thread(
                    target=self._serve, args=(conn,), daemon=True, name="client"
                )
                ct.start()
        finally:
            try:
                srv.close()
            except Exception:
                pass
            self._stop_xray()
            logger.info("supervisor exited")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--parent-pid", type=int, required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--xray-exe", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--geo-dir", required=True)
    p.add_argument("--log-file", required=True)
    args = p.parse_args()

    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        filename=str(log_path),
        filemode="a",
        encoding="utf-8",
    )

    sup = Supervisor(
        parent_pid=args.parent_pid,
        port=args.port,
        xray_exe=Path(args.xray_exe),
        cwd=Path(args.cwd),
        geo_dir=Path(args.geo_dir),
    )
    try:
        sup.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()