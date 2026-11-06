"""Мониторинг активного физического сетевого интерфейса.

Следит за физическим адаптером (default route на non-virtual iface) и
сообщает: сеть пропала, появилась, сменился интерфейс.

Важно: собственный TUN (obxod-tun и т.п.) никогда не считается «сетью» —
иначе старт VPN выглядит как смена интерфейса и провоцирует reconnect-loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

logger = logging.getLogger(__name__)

_VIRTUAL_HINTS = (
    "radmin", "hamachi", "zerotier", "tailscale", "wireguard",
    "tap-windows", "tap-win32", "openvpn", "wintun",
    "hyper-v", "vethernet", "vmware", "virtualbox", "vbox",
    "loopback", "bluetooth", "wan miniport", "npcap",
    "docker", "teredo", "isatap", "wi-fi direct",
    "br-", "veth", "virbr", "docker0", "cni", "flannel",
    "kube", "calico",
    "tun", "tap", "wg", "zt", "tailscale0",
    "obxod", "xray",
)

_SKIP_EXACT = frozenset({"lo", "lo0", "localhost"})


def _is_virtual(name: str | None, desc: str | None = None) -> bool:
    if not name:
        return True
    n = name.strip()
    if n.lower() in _SKIP_EXACT:
        return True
    if re.fullmatch(r"lo\d*", n, re.I):
        return True
    blob = f"{n} {desc or ''}".lower()
    return any(hint in blob for hint in _VIRTUAL_HINTS)


def _has_real_ipv4(iface_addrs) -> bool:
    for a in iface_addrs or []:
        if getattr(a, "family", None) != socket.AF_INET:
            continue
        addr = (a.address or "").strip()
        if not addr or addr.startswith("127.") or addr.startswith("169.254."):
            continue
        return True
    return False


def _linux_default_devs() -> list[str]:
    out: list[str] = []
    try:
        r = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        for line in (r.stdout or "").splitlines():
            m = re.search(r"\bdev\s+(\S+)", line)
            if m:
                out.append(m.group(1))
    except Exception as e:
        logger.debug("ip route: %s", e)
    return out


def _darwin_default_dev() -> str | None:
    try:
        r = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("interface:"):
                return line.split(":", 1)[1].strip()
    except Exception as e:
        logger.debug("route get default: %s", e)
    return None


def _iface_is_up(name: str) -> bool:
    if psutil is None:
        return True
    try:
        st = psutil.net_if_stats().get(name)
        return bool(st and st.isup)
    except Exception:
        return True


def _probe_physical_default() -> "NetworkState | None":
    candidates: list[str] = []
    if sys.platform.startswith("linux"):
        candidates = _linux_default_devs()
    elif sys.platform == "darwin":
        d = _darwin_default_dev()
        if d:
            candidates = [d]

    for name in candidates:
        if _is_virtual(name):
            logger.debug("default route via virtual %r — пропускаю", name)
            continue
        if not _iface_is_up(name):
            continue
        return NetworkState(iface=name, is_up=True)
    return None


def _probe_psutil_physical() -> "NetworkState":
    if psutil is None:
        return NetworkState(iface=None, is_up=False)
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        ranked: list[tuple[int, str]] = []
        for name, st in stats.items():
            if not st.isup or _is_virtual(name):
                continue
            if not _has_real_ipv4(addrs.get(name, [])):
                continue
            nl = name.lower()
            rank = 2
            if nl.startswith(("wl", "wifi", "wlan")):
                rank = 0
            elif nl.startswith(("eth", "enp", "ens", "eno", "en")):
                rank = 1
            ranked.append((rank, name))
        if not ranked:
            return NetworkState(iface=None, is_up=False)
        ranked.sort()
        return NetworkState(iface=ranked[0][1], is_up=True)
    except Exception as e:
        logger.debug("psutil probe: %s", e)
        return NetworkState(iface=None, is_up=False)


def _probe_network() -> "NetworkState":
    """Активный физический интерфейс или down. Никогда не возвращает TUN."""
    via = _probe_physical_default()
    if via is not None:
        return via

    via_ps = _probe_psutil_physical()
    if via_ps.is_up:
        # Linux без default route = для VPN считаем сеть down
        if sys.platform.startswith("linux"):
            logger.debug(
                "physical iface %r up but no default route → down",
                via_ps.iface,
            )
            return NetworkState(iface=via_ps.iface, is_up=False)
        return via_ps

    return NetworkState(iface=None, is_up=False)


class NetworkEvent(str, Enum):
    NONE = "none"
    DOWN = "down"
    UP = "up"
    INTERFACE_CHANGED = "changed"


@dataclass
class NetworkState:
    iface: str | None = None
    is_up: bool = False


class NetworkWatcher:
    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._state = NetworkState()
        self._on_event: Callable[[NetworkEvent, NetworkState], Awaitable[None]] | None = None
        self._running = False

    def start(self, on_event) -> None:
        if self._task and not self._task.done():
            return
        self._on_event = on_event
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        self._state = await asyncio.to_thread(_probe_network)
        logger.info(
            "NetworkWatcher: начальное состояние iface=%r up=%s",
            self._state.iface, self._state.is_up,
        )

        while self._running:
            await asyncio.sleep(self.interval)
            try:
                new_state = await asyncio.to_thread(_probe_network)
                event = self._diff(self._state, new_state)
                if event != NetworkEvent.NONE:
                    logger.info(
                        "NetworkWatcher: %s (was iface=%r up=%s → iface=%r up=%s)",
                        event.value,
                        self._state.iface, self._state.is_up,
                        new_state.iface, new_state.is_up,
                    )
                    self._state = new_state
                    if self._on_event:
                        try:
                            await self._on_event(event, new_state)
                        except Exception as e:
                            logger.error("on_event error: %s", e, exc_info=True)
                else:
                    self._state = new_state
            except Exception as e:
                logger.error("NetworkWatcher loop error: %s", e, exc_info=True)

    @staticmethod
    def _diff(old: NetworkState, new: NetworkState) -> NetworkEvent:
        if old.is_up and not new.is_up:
            return NetworkEvent.DOWN
        if not old.is_up and new.is_up:
            return NetworkEvent.UP
        if old.is_up and new.is_up and old.iface != new.iface:
            return NetworkEvent.INTERFACE_CHANGED
        return NetworkEvent.NONE

    @property
    def state(self) -> NetworkState:
        return self._state
