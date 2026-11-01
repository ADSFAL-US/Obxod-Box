"""Xray process manager.

CROSS-PLATFORM NOTES:
- Desktop (Win/Linux/macOS): supervisor + local xray binary; TUN via wintun (Win) or native tun (Linux).
- Android: VpnService / leave TUN to OS; start/stop via IPC, not wintun.
- iOS: NetworkExtension packet tunnel; binary packaging differs (no detached supervisor process).
"""

# CROSS-PLATFORM: desktop supervisor+xray; Android VpnService; iOS NetworkExtension — see platform_info.py

import asyncio
import atexit
import copy
import json
import logging
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from net_utils import (
    apply_mode_to_config,
    apply_routing_to_config,
    download_geofiles,
    download_wintun,
    download_xray,
    is_admin,
    load_routing_set,
    xray_binary_name,
)

logger = logging.getLogger(__name__)

DEFAULT_XRAY_CFG = {
    "loglevel": "warning",
    "log_rotate_minutes": 10,
}

STATUS_STOPPED = "stopped"
STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_ERROR = "error"
STATUS_NETWORK_DOWN = "network_down"

_SKIP_INBOUND_PROTOS = {"dokodemo-door", "tunnel", "dns"}

PING_PORT_RANGE: tuple[int, int] = (20000, 40000)
PING_USER_AGENT = "obxod-box/ping"
PING_INBOUND_TAG = "obxod-ping-in"

MAX_LOG_SIZE_BYTES = 5 * 1024 * 1024
KEEP_TAIL_BYTES    = 512 * 1024

SUPERVISOR_READY_TIMEOUT = 6.0
SUPERVISOR_POLL_INTERVAL = 0.15
SUPERVISOR_CMD_TIMEOUT   = 4.0
SUPERVISOR_PORT_RANGE    = (45000, 55000)
SUPERVISOR_SCRIPT_NAME   = "xray_supervisor.py"


def _extract_inbound_ports(config: dict) -> list[int]:
    ports: list[int] = []
    for ib in config.get("inbounds", []) or []:
        proto = (ib.get("protocol") or "").lower()
        if proto in _SKIP_INBOUND_PROTOS or proto == "tun":
            continue
        port = ib.get("port")
        if isinstance(port, int):
            ports.append(port)
        elif isinstance(port, str):
            m = re.match(r"(\d+)", port)
            if m:
                ports.append(int(m.group(1)))
    return ports


def _has_tun(config: dict) -> bool:
    for ib in config.get("inbounds", []) or []:
        if (ib.get("protocol") or "").lower() == "tun":
            return True
    return False


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_free_port() -> int | None:
    lo, hi = PING_PORT_RANGE
    start = random.randint(lo, max(lo, hi - 200))
    order = list(range(start, hi)) + list(range(lo, start))
    for port in order:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return None


def _pick_supervisor_port() -> int:
    lo, hi = SUPERVISOR_PORT_RANGE
    return random.randint(lo, hi)


def _pythonw_or_python() -> str:
    if sys.platform != "win32":
        return sys.executable
    exe = Path(sys.executable)
    if exe.name.lower() in ("python.exe", "python"):
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(exe)


class SupervisorClient:
    """Тонкий синхронный клиент для общения с supervisor-процессом."""

    def __init__(self, port: int, timeout: float = SUPERVISOR_CMD_TIMEOUT):
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    def _connect_locked(self) -> bool:
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
            s.settimeout(self.timeout)
            self._sock = s
            return True
        except OSError:
            self._sock = None
            return False

    def _close_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def connect(self) -> bool:
        with self._lock:
            if self._sock is not None:
                return True
            return self._connect_locked()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _send(self, cmd: dict) -> dict:
        with self._lock:
            if self._sock is None:
                if not self._connect_locked():
                    return {"ok": False, "error": "connect failed"}
            try:
                self._sock.sendall(
                    (json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8")
                )
                buf = b""
                while b"\n" not in buf:
                    chunk = self._sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("connection closed")
                    buf += chunk
                line = buf.split(b"\n", 1)[0]
                return json.loads(line.decode("utf-8"))
            except Exception as e:
                self._close_locked()
                return {"ok": False, "error": str(e)}

    def ping(self) -> bool:
        return bool(self._send({"cmd": "ping"}).get("ok"))

    def start(self, config: dict) -> dict:
        return self._send({"cmd": "start", "config": config})

    def stop(self) -> dict:
        return self._send({"cmd": "stop"})

    def restart(self, config: dict) -> dict:
        return self._send({"cmd": "restart", "config": config})

    def status(self) -> dict:
        return self._send({"cmd": "status"})

    def quit(self) -> dict:
        return self._send({"cmd": "quit"})


class XrayManager:
    def __init__(self, app):
        self.app = app
        self.cfg_path = Path(__file__).resolve().parent / "cfg" / "x-ray_conf.json"
        self.logs_dir = Path(__file__).resolve().parent / "logs"
        self.bin_dir = Path(__file__).resolve().parent / "bin"
        self.geo_dir = Path(__file__).resolve().parent / "geo"

        self.log_file = self.logs_dir / "x-ray.log"
        self.supervisor_log_file = self.logs_dir / "supervisor.log"
        self.xray_exe = self.bin_dir / xray_binary_name()

        self._config: dict = {}
        self._cleaner_task: asyncio.Task | None = None
        self._ready_task: asyncio.Task | None = None

        # ── Supervisor ──
        self._supervisor_proc: subprocess.Popen | None = None
        self._supervisor_client: SupervisorClient | None = None
        self._supervisor_port: int = 0

        self._status: str = STATUS_STOPPED
        self._status_message: str = ""
        self._current_inbound_ports: list[int] = []
        self._current_mode: str = ""
        self._current_had_tun: bool = False

        self._last_raw_config: dict | None = None

        # Event loop, к которому привязан менеджер.
        # Нужен, чтобы запускать/отменять asyncio-задачи из рабочих потоков
        # (start/stop часто вызываются через asyncio.to_thread).
        self._loop_ref: asyncio.AbstractEventLoop | None = None

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.load_config()

        atexit.register(self._shutdown_supervisor)

    # ------------------------------------------------------------------
    # Event loop plumbing
    # ------------------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """MainWindow вызывает после инициализации loop'а."""
        self._loop_ref = loop

    def _schedule_on_loop(self, fn) -> None:
        """Выполнить fn на event loop'е. Если мы уже на нём — синхронно."""
        try:
            asyncio.get_running_loop()
            fn()
            return
        except RuntimeError:
            pass
        loop = self._loop_ref
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(fn)
                return
            except RuntimeError:
                pass
        logger.warning(
            "XrayManager: нет активного loop для %s",
            getattr(fn, "__name__", fn),
        )

    # ------------------------------------------------------------------
    # Конфиг
    # ------------------------------------------------------------------
    def load_config(self) -> dict:
        if self.cfg_path.exists():
            try:
                self._config = json.loads(self.cfg_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error("Не удалось прочитать %s: %s", self.cfg_path, e)
                self._config = dict(DEFAULT_XRAY_CFG)
        else:
            self._config = dict(DEFAULT_XRAY_CFG)
            self.save_config()
        return self._config

    def save_config(self) -> None:
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text(
            json.dumps(self._config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def set_loglevel(self, level: str) -> None:
        if self._config.get("loglevel") == level:
            return
        self._config["loglevel"] = level
        self.save_config()
        logger.info("Xray loglevel → %s (применится при следующем старте)", level)

    def set_log_rotate_minutes(self, minutes: int) -> None:
        minutes = max(1, int(minutes))
        if self._config.get("log_rotate_minutes") == minutes:
            return
        self._config["log_rotate_minutes"] = minutes
        self.save_config()
        logger.info("Xray log_rotate_minutes → %d", minutes)

    # ------------------------------------------------------------------
    # Подготовка бинарей
    # ------------------------------------------------------------------
    def ensure_xray_exe(self) -> Path:
        if self.xray_exe.exists():
            return self.xray_exe
        logger.info("%s не найден, скачиваю...", self.xray_exe.name)
        path = download_xray(self.bin_dir)
        self.xray_exe = path
        return path

    def ensure_geofiles(self) -> None:
        try:
            download_geofiles(self.geo_dir)
        except Exception as e:
            logger.warning("Не удалось подготовить geofiles: %s", e)

    # ------------------------------------------------------------------
    # Supervisor
    # ------------------------------------------------------------------
    def _supervisor_script_path(self) -> Path:
        return Path(__file__).resolve().parent / SUPERVISOR_SCRIPT_NAME

    def _spawn_supervisor(self) -> SupervisorClient:
        script = self._supervisor_script_path()
        if not script.exists():
            raise RuntimeError(f"Не найден {script}")

        launcher = _pythonw_or_python()

        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
            )

        last_err: Exception | None = None
        for _ in range(4):
            port = _pick_supervisor_port()
            args = [
                launcher, "-u", str(script),
                "--parent-pid", str(os.getpid()),
                "--port", str(port),
                "--xray-exe", str(self.xray_exe),
                "--cwd", str(self.bin_dir),
                "--geo-dir", str(self.geo_dir),
                "--log-file", str(self.supervisor_log_file),
            ]
            try:
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                    close_fds=True,
                )
            except Exception as e:
                last_err = e
                continue

            client = SupervisorClient(port)
            deadline = time.monotonic() + SUPERVISOR_READY_TIMEOUT
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    last_err = RuntimeError(
                        f"supervisor exited early (code={proc.returncode})"
                    )
                    break
                if client.connect() and client.ping():
                    self._supervisor_proc = proc
                    self._supervisor_client = client
                    self._supervisor_port = port
                    logger.info(
                        "supervisor ready on 127.0.0.1:%d (pid=%d, launcher=%s)",
                        port, proc.pid, Path(launcher).name,
                    )
                    return client
                time.sleep(SUPERVISOR_POLL_INTERVAL)

            try:
                proc.kill()
            except Exception:
                pass
            client.close()

        raise RuntimeError(f"Не удалось запустить supervisor: {last_err}")

    def _ensure_supervisor(self) -> SupervisorClient:
        client = self._supervisor_client
        if client is not None and client.ping():
            return client

        if self._supervisor_proc is not None and self._supervisor_proc.poll() is None:
            if client is not None:
                client.close()
            fresh = SupervisorClient(self._supervisor_port)
            if fresh.connect() and fresh.ping():
                self._supervisor_client = fresh
                logger.info("supervisor reconnected on port %d", self._supervisor_port)
                return fresh
            logger.warning("supervisor process alive but unresponsive — killing")
            try:
                self._supervisor_proc.kill()
                self._supervisor_proc.wait(timeout=2.0)
            except Exception:
                pass

        self._supervisor_proc = None
        self._supervisor_client = None
        return self._spawn_supervisor()

    def _shutdown_supervisor(self) -> None:
        client = self._supervisor_client
        proc = self._supervisor_proc
        self._supervisor_client = None
        self._supervisor_proc = None

        if client is not None:
            try:
                client.quit()
            except Exception:
                pass
            client.close()

        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Сборка runtime-конфига
    # ------------------------------------------------------------------
    def _build_runtime_config(self, xray_config: dict) -> dict:
        mode = str(self.app.config.mode or "proxy").lower()

        if mode in ("tun", "both"):
            if not is_admin():
                if sys.platform == "win32":
                    raise RuntimeError(
                        "TUN-режим требует прав администратора. "
                        "Перезапустите приложение от имени администратора."
                    )
                raise RuntimeError(
                    "TUN-режим требует прав root. "
                    "Запустите: sudo ./start.sh  (или через pkexec)."
                )
            # Wintun — только Windows; на Linux/macOS Xray использует native TUN.
            if sys.platform == "win32":
                try:
                    dll_path = download_wintun(self.bin_dir)
                    logger.info("Использую wintun.dll: %s", dll_path)
                except RuntimeError as e:
                    raise RuntimeError(
                        f"Не удалось подготовить wintun.dll: {e}"
                    ) from e

        physical_iface = None
        if mode in ("tun", "both"):
            physical_iface = self._get_physical_interface()
            logger.info("Физический интерфейс для outbound: %r", physical_iface)

        runtime = apply_mode_to_config(xray_config, mode, physical_iface=physical_iface)

        routing_set = self.app.config.get("routing_set")
        if routing_set:
            routing_dir = Path(__file__).resolve().parent / "cfg" / "routing"
            preset = load_routing_set(routing_dir, routing_set)
            if preset:
                runtime = apply_routing_to_config(runtime, preset)
                logger.info(
                    "Применён пресет маршрутизации %r (%d правил)",
                    routing_set, len(preset.get("rules") or []),
                )
            else:
                logger.warning(
                    "Пресет %r не найден — работаю без маршрутизации", routing_set,
                )

        runtime["log"] = {
            "access": "none",
            "error": str(self.log_file.resolve()),
            "loglevel": self._config.get("loglevel", "warning"),
            "dnsLog": False,
        }
        self._current_mode = mode
        self._current_had_tun = _has_tun(runtime)
        return runtime

    def _get_physical_interface(self) -> str:
        """Имя активного физического интерфейса для autoOutboundsInterface."""
        if sys.platform == "win32":
            ps_command = (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "Get-NetAdapter | Where-Object Status -eq 'Up' "
                "| Select-Object -First 1 -ExpandProperty Name"
            )
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_command],
                    capture_output=True, encoding="utf-8", errors="replace", timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                name = (result.stdout or "").strip()
                if name and not any(ord(c) < 32 for c in name):
                    return name
                logger.warning("PowerShell вернул странное имя интерфейса: %r", name)
            except Exception as e:
                logger.warning("Не удалось определить физический интерфейс: %s", e)
            return "Ethernet"

        # Linux / macOS: физический iface с default route (не TUN)
        try:
            from network_watcher import _probe_network
            st = _probe_network()
            if st.iface and st.is_up:
                return st.iface
            if st.iface:
                return st.iface
        except Exception as e:
            logger.debug("network_watcher probe for outbound: %s", e)

        if sys.platform.startswith("linux"):
            try:
                r = subprocess.run(
                    ["ip", "-4", "route", "show", "default"],
                    capture_output=True, text=True, timeout=3,
                )
                for line in (r.stdout or "").splitlines():
                    m = re.search(r"\bdev\s+(\S+)", line)
                    if not m:
                        continue
                    name = m.group(1)
                    if name.startswith(("lo", "tun", "tap", "wg", "docker", "br-", "veth", "obxod")):
                        continue
                    return name
            except Exception as e:
                logger.debug("ip route iface: %s", e)

        return "eth0"


    # ------------------------------------------------------------------
    # Ping-движок
    # ------------------------------------------------------------------
    def _build_ping_config(self, xray_config: dict, port: int) -> dict:
        cfg = copy.deepcopy(xray_config)

        original_tag = None
        for ib in cfg.get("inbounds") or []:
            proto = (ib.get("protocol") or "").lower()
            if proto in ("socks", "http", "mixed"):
                original_tag = ib.get("tag")
                break

        cfg["inbounds"] = [{
            "tag": original_tag or PING_INBOUND_TAG,
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "mixed",
            "settings": {"auth": "noauth", "udp": False},
            "sniffing": {"enabled": False},
        }]

        cfg["log"] = {
            "access": "none",
            "error": "",
            "loglevel": "error",
            "dnsLog": False,
        }
        return cfg

    @staticmethod
    def _http_get_via_proxy(port: int, url: str, timeout: float) -> None:
        proxy = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": PING_USER_AGENT,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Cache-Control": "no-cache",
            },
        )
        with opener.open(req, timeout=timeout) as resp:
            resp.read()

    def _spawn_ping_xray(self, xray_config: dict, timeout: float) -> tuple[subprocess.Popen, int]:
        self.ensure_xray_exe()

        env = os.environ.copy()
        env["XRAY_LOCATION_ASSET"] = str(self.geo_dir.resolve())

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        last_err: Exception | None = None
        for _ in range(4):
            port = _find_free_port()
            if port is None:
                raise RuntimeError("Нет свободных портов в диапазоне "
                                   f"{PING_PORT_RANGE[0]}–{PING_PORT_RANGE[1]}")

            cfg = self._build_ping_config(xray_config, port)
            cfg_json = json.dumps(cfg, ensure_ascii=False)

            try:
                proc = subprocess.Popen(
                    [str(self.xray_exe), "run", "-c", "stdin:"],
                    cwd=str(self.bin_dir),
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
                last_err = e
                continue

            try:
                if proc.stdin is not None:
                    proc.stdin.write(cfg_json)
                    proc.stdin.flush()
                    proc.stdin.close()
            except Exception as e:
                last_err = e
                try:
                    proc.kill()
                except Exception:
                    pass
                continue

            deadline = time.monotonic() + min(timeout, 8.0)
            ok = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if _port_open("127.0.0.1", port, timeout=0.25):
                    ok = True
                    break
                time.sleep(0.05)

            if ok:
                return proc, port

            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass
            last_err = RuntimeError("Xray не поднял ping-порт")

        raise RuntimeError(f"Не удалось запустить ping-xray: {last_err}")

    def ping_config_sync(self, xray_config: dict, url: str, timeout: float) -> int:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"Некорректный ping-URL: {url!r}")

        proc, port = self._spawn_ping_xray(xray_config, timeout)
        try:
            self._http_get_via_proxy(port, url, timeout)
            t0 = time.perf_counter()
            self._http_get_via_proxy(port, url, timeout)
            return int((time.perf_counter() - t0) * 1000)
        finally:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Статус
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        client = self._supervisor_client
        if client is None:
            return
        try:
            resp = client.status()
        except Exception:
            return
        if not resp.get("ok"):
            return
        xray = resp.get("xray") or {}
        if xray.get("running"):
            return
        rc = xray.get("exit_code")
        if rc is None:
            return
        if self._status in (STATUS_STOPPED, STATUS_ERROR):
            return
        logger.warning("Xray завершился (exit=%s)", rc)
        self._current_inbound_ports = []
        self._current_had_tun = False
        self._set_status(STATUS_ERROR, f"Xray завершился (код {rc})")

    @property
    def status(self) -> str:
        self._refresh()
        return self._status

    @property
    def status_message(self) -> str:
        return self._status_message

    @property
    def last_raw_config(self) -> dict | None:
        return self._last_raw_config

    def set_network_down(self) -> None:
        self._set_status(STATUS_NETWORK_DOWN, "Нет сети")

    def _set_status(self, status: str, message: str = "") -> None:
        if self._status == status and self._status_message == message:
            return
        self._status = status
        self._status_message = message
        logger.info("Xray status: %s (%s)", status, message)

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------
    def start(self, xray_config: dict) -> None:
        self._refresh()
        if self.is_running:
            logger.warning("Xray уже запущен — повторный старт проигнорирован")
            return

        self.load_config()
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        try:
            self.ensure_xray_exe()
        except Exception as e:
            self._set_status(STATUS_ERROR, str(e))
            raise

        try:
            runtime = self._build_runtime_config(xray_config)
        except Exception as e:
            self._set_status(STATUS_ERROR, str(e))
            raise

        self._last_raw_config = dict(xray_config)
        self._current_inbound_ports = _extract_inbound_ports(runtime)

        try:
            old_size = self.log_file.stat().st_size if self.log_file.exists() else 0
            self.log_file.write_text("", encoding="utf-8")
            if old_size > 0:
                logger.info("Лог перед стартом был %d байт — очищен", old_size)
        except OSError:
            pass

        try:
            client = self._ensure_supervisor()
        except Exception as e:
            self._set_status(STATUS_ERROR, f"supervisor start failed: {e}")
            raise

        resp = client.start(runtime)
        if not resp.get("ok"):
            msg = resp.get("error") or resp.get("message") or "start failed"
            self._set_status(STATUS_ERROR, f"Xray start failed: {msg}")
            raise RuntimeError(msg)

        logger.info(
            "Xray запущен через supervisor (mode=%s, TUN=%s, config=%d байт)",
            self._current_mode, self._current_had_tun,
            len(json.dumps(runtime, ensure_ascii=False)),
        )

        self._set_status(STATUS_STARTING, "Запуск Xray...")
        # Пост-старт (cleaner + ready-watcher) требует event loop.
        # Метод могут вызвать из to_thread → ставим задачи через loop.
        self._schedule_on_loop(self._post_start_on_loop)

    def _post_start_on_loop(self) -> None:
        """Runs on the event loop thread."""
        self._start_cleaner()
        if self._ready_task and not self._ready_task.done():
            self._ready_task.cancel()
        try:
            self._ready_task = asyncio.create_task(self._await_ready())
        except RuntimeError:
            self._ready_task = None

    async def _await_ready(self, timeout: float = 20.0) -> None:
        ports = list(self._current_inbound_ports)
        is_tun_mode = self._current_had_tun
        deadline = time.monotonic() + timeout

        try:
            while time.monotonic() < deadline:
                client = self._supervisor_client
                if client is not None:
                    try:
                        # client.status() — socket I/O, не блокируем loop.
                        resp = await asyncio.to_thread(client.status)
                        if resp.get("ok"):
                            xray = resp.get("xray") or {}
                            if not xray.get("running") and xray.get("exit_code") is not None:
                                rc = xray.get("exit_code")
                                logger.warning(
                                    "Xray умер при старте (exit=%s). Смотри %s",
                                    rc, self.log_file.name,
                                )
                                self._set_status(
                                    STATUS_ERROR, f"Xray завершился (код {rc})"
                                )
                                return
                    except Exception:
                        pass

                if ports:
                    if any(_port_open("127.0.0.1", p) for p in ports):
                        self._set_status(STATUS_RUNNING, "Xray работает")
                        return
                elif is_tun_mode and time.monotonic() > deadline - timeout + 2.0:
                    self._set_status(STATUS_RUNNING, "Xray работает (TUN)")
                    return

                await asyncio.sleep(0.4)

            self._set_status(STATUS_ERROR,
                             f"Xray не поднялся за {int(timeout)} сек")
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
        # Отмена asyncio-задач — через loop: метод могут вызвать
        # из рабочего потока (asyncio.to_thread).
        self._schedule_on_loop(self._cancel_loop_tasks)

        client = self._supervisor_client
        if client is not None:
            try:
                resp = client.stop()
                if not resp.get("ok"):
                    logger.warning("supervisor stop failed: %s",
                                   resp.get("error") or resp.get("message"))
            except Exception as e:
                logger.warning("supervisor stop exception: %s", e)

        self._current_inbound_ports = []
        self._current_had_tun = False
        self._set_status(STATUS_STOPPED, "")

    def _cancel_loop_tasks(self) -> None:
        """Runs on the event loop thread."""
        for attr in ("_ready_task", "_cleaner_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
            setattr(self, attr, None)

    async def restart(self, xray_config: dict) -> None:
        # stop()/start() ходят в сокет supervisor'а — уводим в поток,
        # чтобы не подвешивать event loop.
        await asyncio.to_thread(self.stop)
        await asyncio.sleep(0.3)
        await asyncio.to_thread(self.start, xray_config)

    @property
    def is_running(self) -> bool:
        self._refresh()
        client = self._supervisor_client
        if client is None:
            return False
        try:
            resp = client.status()
        except Exception:
            return False
        if not resp.get("ok"):
            return False
        return bool((resp.get("xray") or {}).get("running"))

    # ------------------------------------------------------------------
    # Ротация логов
    # ------------------------------------------------------------------
    def _clean_logs(self) -> None:
        if not self.log_file.exists():
            return

        try:
            size = self.log_file.stat().st_size
        except OSError:
            return

        if size > MAX_LOG_SIZE_BYTES:
            try:
                with open(self.log_file, "rb") as f:
                    f.seek(max(0, size - KEEP_TAIL_BYTES))
                    tail = f.read()
                nl = tail.find(b"\n")
                if nl >= 0:
                    tail = tail[nl + 1:]
                with open(self.log_file, "wb") as f:
                    f.write(tail)
                logger.info(
                    "Лог усечён: %d → %d байт (size cap %d)",
                    size, len(tail), MAX_LOG_SIZE_BYTES,
                )
                return
            except OSError as e:
                logger.debug("Не удалось усечь большой лог: %s", e)
                return

        cutoff = datetime.now() - timedelta(
            minutes=int(self._config.get("log_rotate_minutes", 10))
        )
        try:
            with open(self.log_file, "r+", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                kept = [
                    ln for ln in lines
                    if (ts := self._parse_timestamp(ln)) is None or ts >= cutoff
                ]
                if len(kept) != len(lines):
                    f.seek(0)
                    f.truncate()
                    f.writelines(kept)
                    f.flush()
        except PermissionError:
            logger.debug("Лог занят Xray, пропускаю цикл ротации")
        except OSError as e:
            logger.debug("Ошибка ротации логов: %s", e)

    @staticmethod
    def _parse_timestamp(line: str) -> datetime | None:
        m = re.match(r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})", line)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")
            except ValueError:
                pass
        return None

    async def _clean_loop(self) -> None:
        interval = max(20, int(self._config.get("log_rotate_minutes", 10)) * 60 // 10)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await asyncio.to_thread(self._clean_logs)
                except Exception as e:
                    logger.error("Ошибка очистки логов: %s", e, exc_info=True)
        except asyncio.CancelledError:
            return

    def _start_cleaner(self) -> None:
        if self._cleaner_task is None or self._cleaner_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._cleaner_task = loop.create_task(self._clean_loop())
            except RuntimeError:
                pass