"""Диагностика сетевого стека после запуска Xray.

Проверяем не только «процесс жив», но и то, что трафик действительно можно
перехватить: слушаются inbound-порты, создан TUN-интерфейс, в системной
таблице маршрутов есть нужный маршрут, а через прокси/TUN проходит запрос.

Если мешает другой VPN/прокси/фильтр — показываем процесс, который можно
завершить. Если проблема неизвестна — предлагаем открыть issue.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

from xray_errors import describe_exit_code

logger = logging.getLogger(__name__)

ISSUES_URL = "https://github.com/ADSFAL-US/Obxod-Box/issues/new"
DEFAULT_PROBE_URL = "https://www.gstatic.com/generate_204"
TUN_INTERFACE_NAME = "obxod-tun"
TUN_NAME_HINTS = ("obxod-tun", "wintun", "tun", "tap", "wireguard", "wg")
SKIP_IFACE_PREFIXES = ("loopback", "docker", "br-", "veth", "virbr", "vmnet")

# Тип туннеля определяем по драйверу адаптера (Win32_NetworkAdapter.ServiceName),
# а не по отображаемому имени: в Windows оно локализовано и одинаково у всех
# Wintun-адаптеров («Подключение по локальной сети»).
VPN_DRIVER_SERVICES: dict[str, str] = {
    "rvnetmp60": "Radmin VPN",
    "rvnetmp": "Radmin VPN",
    "hamachi": "Hamachi",
    "zerotierone": "ZeroTier",
    "tailscale": "Tailscale",
    "wireguard": "WireGuard",
    "nordlynx": "NordVPN",
    "protonvpn": "ProtonVPN",
    "tap0901": "OpenVPN (TAP)",
    "tap-windows6": "OpenVPN (TAP)",
    "ovpn-dco": "OpenVPN",
    "openvpn": "OpenVPN",
    "softether": "SoftEther",
    "neorouter": "NeoRouter",
    "cloudflarewarp": "Cloudflare WARP",
    "warp-svc": "Cloudflare WARP",
}

# Описание драйвера (INF, обычно английское) — резервный признак для
# адаптеров, которых нет в таблице выше.
VPN_DESC_HINTS = (
    "vpn", "wireguard", "zerotier", "hamachi", "radmin", "tailscale",
    "openvpn", "tap-windows", "tap0901", "softether", "neorouter",
    "cloudflare warp",
)

# Процессы-демоны VPN/прокси. Сравниваем ТОЛЬКО basename исполняемого файла:
# подстрочный поиск по командной строке давал ложные срабатывания
# (Code.exe с флагом --use-angle=d3d11-warp-webgl, git.exe с путём к xray).
VPN_PROCESS_NAMES: dict[str, str] = {
    "happ.exe": "HAPP",
    "happd.exe": "HAPP",
    "rvcontrolsvc.exe": "Radmin VPN",
    "rvnetsvc.exe": "Radmin VPN",
    "hamachi-2.exe": "Hamachi",
    "hamachi-2-ui.exe": "Hamachi",
    "zerotier_one.exe": "ZeroTier",
    "zerotieroneservice.exe": "ZeroTier",
    "tailscaled.exe": "Tailscale",
    "tailscale-ipn.exe": "Tailscale",
    "wireguard.exe": "WireGuard",
    "openvpn.exe": "OpenVPN",
    "warp-svc.exe": "Cloudflare WARP",
    "clash.exe": "Clash",
    "clash-verge.exe": "Clash Verge",
    "mihomo.exe": "Mihomo",
    "v2ray.exe": "v2ray",
    "v2rayn.exe": "v2rayN",
    "sing-box.exe": "sing-box",
    "nekoray.exe": "NekoRay",
    "shadowsocks.exe": "Shadowsocks",
    "trojan.exe": "Trojan",
    "hysteria.exe": "Hysteria",
    "tun2socks.exe": "tun2socks",
    "netch.exe": "Netch",
    "proxifier.exe": "Proxifier",
    "psiphon.exe": "Psiphon",
    "nordvpn-service.exe": "NordVPN",
    "protonvpn-service.exe": "ProtonVPN",
    "expressvpn.exe": "ExpressVPN",
    "surfshark-service.exe": "Surfshark",
}

# Клиентские процессы по названию VPN-адаптера. Нужны, когда у драйвера
# (miniport) нет PID: Radmin/Hamachi держат адаптер через службу-драйвер,
# а пользовательский процесс называется иначе (RvControlSvc.exe и т.п.).
VPN_CLIENT_PROCESSES: dict[str, tuple[str, ...]] = {
    "Radmin VPN": ("rvcontrolsvc.exe", "rvnetsvc.exe", "radmin.exe"),
    "Hamachi": ("hamachi-2.exe", "hamachi-2-ui.exe"),
    "ZeroTier": ("zerotier_one.exe", "zerotieroneservice.exe"),
    "Tailscale": ("tailscaled.exe", "tailscale-ipn.exe"),
    "WireGuard": ("wireguard.exe",),
    "OpenVPN": ("openvpn.exe", "openvpn-gui.exe"),
    "OpenVPN (TAP)": ("openvpn.exe", "openvpn-gui.exe"),
    "NordVPN": ("nordvpn-service.exe",),
    "ProtonVPN": ("protonvpn-service.exe",),
    "SoftEther": ("vpnclient.exe", "vpncmgr.exe"),
    "NeoRouter": ("nrservice.exe", "nrclient.exe"),
}

# Псевдо-интерфейсы Windows/системы: Teredo, ISATAP, 6to4, Hyper-V и т.п.
# Они не являются ни TUN-туннелем, ни конкурирующим VPN — их нельзя
# показывать в диагностике как проблему.
PSEUDO_IFACE_HINTS = (
    "teredo", "isatap", "pseudo", "псевдо", "6to4", "loopback", "bluetooth",
    "wi-fi direct", "wan miniport", "npcap", "hyper-v", "vethernet",
    "vmware", "virtualbox", "vbox", "docker", "cni", "flannel",
    "kube", "calico",
)

# Клиенты, которые любят держать TUN/WFP/системный прокси и мешать Xray.
# Используется только для проверки «можно ли завершить процесс» (kill_process),
# в отчёт диагностики процесс попадает лишь при доказанном конфликте.
INTERFERING_PROCESS_HINTS = (
    "clash", "mihomo", "clash-verge", "v2ray", "v2rayn", "xray",
    "sing-box", "singbox", "nekoray", "nekobox", "shadowsocks", "ss-local",
    "sslocal", "trojan", "wireguard", "openvpn", "tailscale", "zerotier",
    "hamachi", "radmin", "nordvpn", "expressvpn", "surfshark", "protonvpn",
    "psiphon", "netch", "winws", "zapret", "goodbyedpi", "byedpi",
    "spoofdpi", "dnscrypt", "privoxy", "proxifier", "netlimiter", "adguard",
    "tun2socks", "hev-socks5-tunnel", "hysteria", "tuic", "gost", "warp",
)

_SHORT_HINTS = frozenset({"xray", "tor", "wg", "gost", "warp"})

_LOG_RULES: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (
        re.compile(r"address already in use|only one usage of each socket address", re.I),
        "port_in_use",
        "Порт уже занят другим процессом",
        "Закройте приложение, которое слушает этот порт, и переподключитесь.",
    ),
    (
        re.compile(
            r"proxy/tun:\s*connection (?:was )?(?:refused|reset by peer)",
            re.I,
        ),
        "tun_connection_refused",
        "Соединение через TUN отклонено",
        "Проверьте, не мешает ли другой VPN-клиент отдельным соединениям через TUN.",
    ),
    (
        re.compile(r"permission denied|operation not permitted|access is denied", re.I),
        "permission_denied",
        "Xray не хватает прав",
        "Запустите приложение от имени администратора (Windows) или через sudo (Linux).",
    ),
    (
        re.compile(
            r"(?!.+proxy/tun:\s*connection (?:was )?(?:refused|reset by peer))"
            r"(wintun|tun|tap).*(fail|error|denied|cannot|unable|already)"
            r"|(fail|error|denied|cannot|unable).*(wintun|tap)",
            re.I,
        ),
        "tun_create_failed",
        "Не удалось создать/настроить TUN-интерфейс",
        "Закройте другие VPN-клиенты и проверьте права администратора/root.",
    ),
)

_LOG_ERROR_RE = re.compile(r"\b(error|fatal|panic|failed)\b", re.I)


@dataclass
class DiagnosticIssue:
    code: str
    severity: str  # "error" | "warning"
    title: str
    detail: str
    suggestion: str = ""
    known: bool = True
    processes: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    issue_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnosticReport:
    ok: bool
    mode: str
    summary: str
    issues: list[DiagnosticIssue] = field(default_factory=list)
    traffic_ok: bool | None = None
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "summary": self.summary,
            "issues": [i.to_dict() for i in self.issues],
            "traffic_ok": self.traffic_ok,
            "checked_at": self.checked_at,
        }


# ----------------------------------------------------------------------
# Вспомогательные проверки
# ----------------------------------------------------------------------
def _tail_text(path: Path, max_bytes: int = 24 * 1024) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _inbound_ports(runtime: dict, protocols: set[str] | None = None) -> list[int]:
    ports: list[int] = []
    for ib in runtime.get("inbounds") or []:
        proto = (ib.get("protocol") or "").lower()
        if proto in ("tun", "dokodemo-door", "tunnel", "dns"):
            continue
        if protocols is not None and proto not in protocols:
            continue
        port = ib.get("port")
        if isinstance(port, int):
            ports.append(port)
        elif isinstance(port, str):
            m = re.match(r"(\d+)", port)
            if m:
                ports.append(int(m.group(1)))
    return list(dict.fromkeys(ports))


def _has_tun(runtime: dict) -> bool:
    return any(
        (ib.get("protocol") or "").lower() == "tun"
        for ib in runtime.get("inbounds") or []
    )


def _process_tree_pids(root_pid: int | None = None) -> set[int]:
    """Наш процесс + supervisor + xray (всё, что нельзя считать чужим)."""
    root_pid = root_pid or os.getpid()
    result = {root_pid}
    if psutil is None:
        return result
    try:
        proc = psutil.Process(root_pid)
        result.update(p.pid for p in proc.children(recursive=True))
        result.update(p.pid for p in proc.parents())
    except Exception as exc:
        logger.debug("process tree: %s", exc)
    return result


def _exe_basename(name: str, exe: str) -> str:
    base = Path(exe).name if exe else ""
    return (base or name or "").strip().lower()


def _known_process_reason(name: str, exe: str = "") -> str | None:
    """Признак «это VPN/прокси-клиент» — только по basename исполняемого файла."""
    base = _exe_basename(name, exe)
    label = VPN_PROCESS_NAMES.get(base)
    if label:
        return label
    for hint in INTERFERING_PROCESS_HINTS:
        if hint in _SHORT_HINTS:
            if re.search(r"(^|[^a-z0-9])" + re.escape(hint) + r"([^a-z0-9]|$)", base):
                return hint
        elif hint in base:
            return hint
    return None


def _killable_owner(owner: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Возвращаем процесс в UI только если его реально можно завершить."""
    if not owner:
        return []
    name = str(owner.get("name") or "")
    if _known_process_reason(name) is None:
        return []
    return [owner]


def _happ_processes() -> list[dict[str, Any]]:
    """Возвращает только точные процессы HAPP, найденные в системе."""
    if psutil is None:
        return []
    try:
        iterator = psutil.process_iter(["pid", "name", "exe"])
    except Exception as exc:
        logger.warning("Cannot enumerate processes while checking HAPP: %s", exc)
        return []

    found = []
    for proc in iterator:
        try:
            name = str(proc.info.get("name") or "")
            exe = str(proc.info.get("exe") or "")
            if _exe_basename(name, exe) not in ("happ.exe", "happd.exe"):
                continue
            pid = int(proc.info.get("pid") or 0)
            if pid:
                found.append({
                    "pid": pid,
                    "name": name or Path(exe).name,
                    "reason": "HAPP",
                })
        except (psutil.Error, OSError, ValueError):
            continue
    return found


def _service_pid(service: str) -> int | None:
    """PID процесса, обслуживающего драйвер/службу адаптера (только Windows)."""
    if psutil is None or not service or sys.platform != "win32":
        return None
    getter = getattr(psutil, "win_service_get", None)
    if getter is None:
        return None
    try:
        info = getter(service).as_dict()
    except Exception as exc:
        logger.debug("win_service_get(%s): %s", service, exc)
        return None
    pid = info.get("pid")
    return int(pid) if pid else None


def _processes_for_adapters(
    adapters: list[dict[str, Any]], own_pids: set[int]
) -> list[dict[str, Any]]:
    """Процессы, которые реально владеют конфликтующими адаптерами.

    Сначала пробуем PID службы драйвера (Win32_NetworkAdapter.ServiceName).
    У miniport-драйверов (Radmin, Hamachi) PID нет — тогда ищем процесс
    клиента по basename, сопоставляя его с названием адаптера.
    """
    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(pid: int, name: str, reason: str) -> None:
        if not pid or pid in own_pids or pid in seen:
            return
        seen.add(pid)
        found.append({"pid": pid, "name": name or f"pid {pid}", "reason": reason})

    for adapter in adapters:
        label = str(adapter.get("label") or "vpn")
        pid = _service_pid(str(adapter.get("service") or ""))
        if pid:
            try:
                add(pid, psutil.Process(pid).name(), label)
                continue
            except Exception as exc:
                logger.debug("adapter owner %s: %s", pid, exc)

        keyword = label.split()[0].lower()
        if len(keyword) < 4 or psutil is None:
            continue
        candidates = VPN_CLIENT_PROCESSES.get(label, ())
        try:
            iterator = psutil.process_iter(["pid", "name", "exe"])
        except Exception as exc:
            logger.debug("process_iter: %s", exc)
            continue
        for proc in iterator:
            try:
                name = str(proc.info.get("name") or "")
                exe = str(proc.info.get("exe") or "")
                base = _exe_basename(name, exe)
                if candidates:
                    if base not in candidates:
                        continue
                elif keyword not in base:
                    continue
                if _known_process_reason(name, exe) is None:
                    continue
                add(int(proc.info.get("pid") or 0), name, label)
            except (psutil.Error, OSError, ValueError):
                continue
    return found


def _port_owner(port: int) -> dict[str, Any] | None:
    if psutil is None:
        return None
    try:
        connections = psutil.net_connections(kind="tcp")
    except Exception as exc:
        logger.debug("net_connections: %s", exc)
        return None

    for conn in connections:
        try:
            if not conn.laddr or conn.laddr.port != port:
                continue
            if conn.status != psutil.CONN_LISTEN:
                continue
            pid = int(conn.pid or 0)
            if not pid:
                continue
            name = ""
            try:
                name = psutil.Process(pid).name()
            except Exception as exc:
                logger.debug("port owner name: %s", exc)
            return {"pid": pid, "name": name or f"pid {pid}", "reason": "listener"}
        except (ValueError, AttributeError):
            continue
    return None


def _run_powershell_json(script: str, timeout: float = 10.0) -> dict[str, Any] | None:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        logger.debug("powershell snapshot: %s", exc)
        return None
    raw = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.debug("powershell snapshot json: %s (%s)", exc, raw[:200])
        return None
    return data if isinstance(data, dict) else None


def _resolve_probe_ip(url: str) -> str | None:
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        logger.debug("resolve %s: %s", host, exc)
        return None
    for info in infos:
        addr = info[4][0]
        if addr:
            return str(addr)
    return None


def _adapter_label(adapter: dict[str, Any]) -> str | None:
    """Тип VPN-адаптера по драйверу (ServiceName), затем по описанию."""
    service = str(adapter.get("service") or "").strip().lower()
    if service in VPN_DRIVER_SERVICES:
        return VPN_DRIVER_SERVICES[service]
    desc = str(adapter.get("desc") or "").lower()
    if any(hint in desc for hint in VPN_DESC_HINTS):
        return str(adapter.get("desc") or "VPN")
    return None


def _is_pseudo_iface(name: str) -> bool:
    low = (name or "").lower()
    # "lo" — точное имя loopback в Linux; как префикс оно ошибочно отсекало
    # английские «Local Area Connection».
    if low in ("lo", "loopback"):
        return True
    if low.startswith(SKIP_IFACE_PREFIXES):
        return True
    return any(hint in low for hint in PSEUDO_IFACE_HINTS)


def _is_own_tun(name: str) -> bool:
    low = (name or "").lower()
    return "obxod" in low or TUN_INTERFACE_NAME.lower() == low


# Radmin VPN: miniport RvNetMP60 умеет «залипать» в состоянии ошибки драйвера.
# Адаптер при этом не поднимается и маршрутов не держит, но NDIS-фильтр
# продолжает резать трафик. Лечим отключением адаптера и остановкой служб
# Radmin — драйвер НЕ удаляем: после pnputil /remove-device Radmin не может
# подключиться до ручной переустановки драйвера.
RADMIN_SERVICES = frozenset({"rvnetmp60", "rvnetmp"})
RADMIN_PROCESS_NAMES = frozenset({"rvcontrolsvc.exe", "rvnetsvc.exe", "radmin.exe"})

# ConfigManagerErrorCode, означающие сбой драйвера, а не штатное отключение.
# 22 (CM_PROB_DISABLED) сюда НЕ входит: это нормально выключенный адаптер.
BROKEN_DEVICE_ERROR_CODES = frozenset({
    10,  # CM_PROB_FAILED_START
    24,  # CM_PROB_DEVICE_NOT_THERE
    28,  # CM_PROB_FAILED_INSTALL
    31,  # CM_PROB_FAILED_ADD
    39,  # CM_PROB_FAILED_LOAD
    43,  # CM_PROB_FAILED_POST_START
    52,  # CM_PROB_UNSIGNED_DRIVER
})


def _is_radmin_adapter(adapter: dict[str, Any]) -> bool:
    service = str(adapter.get("service") or "").strip().lower()
    if service in RADMIN_SERVICES:
        return True
    desc = str(adapter.get("desc") or "").lower()
    return "radmin" in desc


def _broken_radmin_adapters(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Radmin-адаптеры со сбоем драйвера (не просто выключенные)."""
    result: list[dict[str, Any]] = []
    for adapter in snapshot.get("adapters") or []:
        if not _is_radmin_adapter(adapter):
            continue
        try:
            error = int(adapter.get("error") or 0)
        except (TypeError, ValueError):
            error = 0
        if error not in BROKEN_DEVICE_ERROR_CODES:
            continue
        result.append({
            "name": str(adapter.get("name") or "Radmin VPN"),
            "label": "Radmin VPN",
            "service": str(adapter.get("service") or ""),
            "desc": str(adapter.get("desc") or ""),
            "device": str(adapter.get("pnp") or ""),
            "error": error,
        })
    return result


def _radmin_adapters(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Все Radmin-адаптеры (включая выключенные) — для «залипшего» случая."""
    result: list[dict[str, Any]] = []
    for adapter in snapshot.get("adapters") or []:
        if not _is_radmin_adapter(adapter):
            continue
        try:
            error = int(adapter.get("error") or 0)
        except (TypeError, ValueError):
            error = 0
        result.append({
            "name": str(adapter.get("name") or "Radmin VPN"),
            "label": "Radmin VPN",
            "service": str(adapter.get("service") or ""),
            "desc": str(adapter.get("desc") or ""),
            "device": str(adapter.get("pnp") or ""),
            "error": error,
            "up": int(adapter.get("status") or 0) == 2,
        })
    return result


def _radmin_started_before_us() -> bool:
    """Radmin-служба запущена раньше нашего процесса.

    Известный сценарий: Radmin, поднятый до Obxod-BOX, забирает сеть и
    блокирует соединение даже после отключения адаптера.
    """
    if psutil is None or sys.platform != "win32":
        return False
    try:
        own_start = psutil.Process(os.getpid()).create_time()
    except Exception as exc:
        logger.debug("own create_time: %s", exc)
        return False
    try:
        for proc in psutil.process_iter(["name", "create_time"]):
            name = str(proc.info.get("name") or "").lower()
            if name not in RADMIN_PROCESS_NAMES:
                continue
            started = proc.info.get("create_time")
            if started and started < own_start:
                return True
    except Exception as exc:
        logger.debug("radmin start time: %s", exc)
    return False


def _radmin_actions(adapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for adapter in adapters:
        device = str(adapter.get("device") or "")
        if not device:
            continue
        actions.append({
            "action": "disable_interface",
            "label": "Отключить интерфейс Radmin VPN",
            "device": device,
            "name": str(adapter.get("name") or "Radmin VPN"),
        })
    return actions


def _network_snapshot(probe_ip: str | None) -> dict[str, Any]:
    """Адаптеры + маршруты + фактический интерфейс до probe_ip.

    Возвращает {"adapters": [...], "routes": [...], "probe_iface": str|None}.
    """
    if sys.platform == "win32":
        probe_line = ""
        if probe_ip:
            probe_line = (
                f"$r = Find-NetRoute -RemoteIPAddress '{probe_ip}' "
                "| Where-Object { $_.DestinationPrefix } | Select-Object -First 1; "
                "if ($r) { $probe = [pscustomobject]@{ iface = $r.InterfaceAlias; "
                "next = $r.NextHop; dest = $r.DestinationPrefix } }"
            )
        script = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$ErrorActionPreference = 'SilentlyContinue'; "
            "$adapters = @(Get-CimInstance Win32_NetworkAdapter "
            "| Where-Object { $_.NetConnectionID } | ForEach-Object { "
            "[pscustomobject]@{ name = $_.NetConnectionID; desc = $_.Description; "
            "service = $_.ServiceName; status = [int]$_.NetConnectionStatus; "
            "error = [int]$_.ConfigManagerErrorCode; "
            "enabled = [bool]$_.NetEnabled; pnp = $_.PNPDeviceID } }); "
            "$routes = @(Get-NetRoute -AddressFamily IPv4 "
            "| Where-Object { $_.DestinationPrefix -in @('0.0.0.0/0','0.0.0.0/1','128.0.0.0/1') } "
            "| ForEach-Object { [pscustomobject]@{ dest = $_.DestinationPrefix; "
            "iface = $_.InterfaceAlias; metric = [int]$_.RouteMetric; "
            "ifmetric = [int]$_.InterfaceMetric } }); "
            "$probe = $null; " + probe_line + " "
            "[pscustomobject]@{ adapters = $adapters; routes = $routes; probe = $probe } "
            "| ConvertTo-Json -Depth 4 -Compress"
        )
        data = _run_powershell_json(script)
        if data is None:
            return {"adapters": [], "routes": [], "probe_iface": None}
        probe = data.get("probe") or {}
        return {
            "adapters": [a for a in (data.get("adapters") or []) if isinstance(a, dict)],
            "routes": [r for r in (data.get("routes") or []) if isinstance(r, dict)],
            "probe_iface": (probe.get("iface") or None) if isinstance(probe, dict) else None,
        }

    if sys.platform.startswith("linux"):
        adapters: list[dict[str, Any]] = []
        try:
            stats = psutil.net_if_stats() if psutil else {}
            for name, st in stats.items():
                adapters.append({
                    "name": name, "desc": name, "service": "",
                    "status": 2 if st.isup else 7,
                })
        except Exception as exc:
            logger.debug("net_if_stats: %s", exc)
        routes: list[dict[str, Any]] = []
        probe_iface = None
        try:
            r = subprocess.run(
                ["ip", "-4", "route", "show", "default"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            for dev in re.findall(r"\bdev\s+(\S+)", r.stdout or ""):
                routes.append({"dest": "0.0.0.0/0", "iface": dev})
            if probe_ip:
                r = subprocess.run(
                    ["ip", "-4", "route", "get", probe_ip],
                    capture_output=True, text=True, timeout=3, check=False,
                )
                m = re.search(r"\bdev\s+(\S+)", r.stdout or "")
                probe_iface = m.group(1) if m else None
        except Exception as exc:
            logger.debug("ip route: %s", exc)
        return {"adapters": adapters, "routes": routes, "probe_iface": probe_iface}

    if sys.platform == "darwin":
        adapters = []
        try:
            stats = psutil.net_if_stats() if psutil else {}
            for name, st in stats.items():
                adapters.append({
                    "name": name, "desc": name, "service": "",
                    "status": 2 if st.isup else 7,
                })
        except Exception as exc:
            logger.debug("net_if_stats: %s", exc)
        routes = []
        probe_iface = None
        try:
            r = subprocess.run(
                ["route", "-n", "get", "default"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            for line in (r.stdout or "").splitlines():
                if line.strip().startswith("interface:"):
                    routes.append({
                        "dest": "0.0.0.0/0",
                        "iface": line.split(":", 1)[1].strip(),
                    })
            if probe_ip:
                r = subprocess.run(
                    ["route", "-n", "get", probe_ip],
                    capture_output=True, text=True, timeout=3, check=False,
                )
                for line in (r.stdout or "").splitlines():
                    if line.strip().startswith("interface:"):
                        probe_iface = line.split(":", 1)[1].strip()
                        break
        except Exception as exc:
            logger.debug("route get: %s", exc)
        return {"adapters": adapters, "routes": routes, "probe_iface": probe_iface}

    return {"adapters": [], "routes": [], "probe_iface": None}


def _winning_default_iface(snapshot: dict[str, Any]) -> str | None:
    """Интерфейс, который реально выигрывает default route (metric+ifmetric)."""
    best: tuple[int, str] | None = None
    for route in snapshot.get("routes") or []:
        if str(route.get("dest") or "") != "0.0.0.0/0":
            continue
        iface = str(route.get("iface") or "")
        if not iface:
            continue
        try:
            metric = int(route.get("metric") or 0) + int(route.get("ifmetric") or 0)
        except (TypeError, ValueError):
            metric = 0
        if best is None or metric < best[0]:
            best = (metric, iface)
    return best[1] if best else None


def _conflicting_adapters(
    snapshot: dict[str, Any], own_tun_names: set[str], *, traffic_failed: bool = False
) -> list[dict[str, Any]]:
    """Поднятые VPN-адаптеры, которые действительно мешают нам.

    Конфликт подтверждается фактами, а не именем процесса:
      * трафик до probe-хоста реально уходит через чужой VPN-адаптер;
      * чужой VPN-адаптер выигрывает default route (меньшая суммарная метрика);
      * проба трафика провалилась, и чужой VPN-адаптер поднят — единственный
        обоснованный подозреваемый.

    Адаптер, который просто существует (выключен, без маршрутов, проигрывает
    по метрике), конфликтом не считается — именно это давало ложные
    предупреждения про Discord/WARP/VS Code/ZeroTier.
    """
    own_lower = {name.lower() for name in own_tun_names}
    probe_iface = str(snapshot.get("probe_iface") or "").lower()
    winning = (_winning_default_iface(snapshot) or "").lower()
    result: list[dict[str, Any]] = []
    for adapter in snapshot.get("adapters") or []:
        name = str(adapter.get("name") or "")
        low = name.lower()
        if not name or low in own_lower or _is_own_tun(name):
            continue
        if int(adapter.get("status") or 0) != 2:
            continue
        if _is_pseudo_iface(name):
            continue
        label = _adapter_label(adapter)
        if not label:
            continue
        captures_probe = bool(probe_iface) and low == probe_iface
        wins_default = bool(winning) and low == winning
        if not (captures_probe or wins_default or traffic_failed):
            continue
        result.append({
            "name": name,
            "label": label,
            "service": str(adapter.get("service") or ""),
            "desc": str(adapter.get("desc") or ""),
            "device": str(adapter.get("pnp") or ""),
            "captures_probe": captures_probe,
            "wins_default": wins_default,
        })
    return result


def _build_proxy_opener(port: int) -> urllib.request.OpenerDirector:
    proxy = f"http://127.0.0.1:{port}"
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )


def _probe_http(opener: urllib.request.OpenerDirector, url: str, timeout: float) -> None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "obxod-box/diagnostics",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "Cache-Control": "no-cache",
        },
    )
    with opener.open(req, timeout=timeout) as resp:
        resp.read(1024)


def _probe_via_proxy(port: int, url: str, timeout: float) -> None:
    _probe_http(_build_proxy_opener(port), url, timeout)


def _probe_direct(url: str, timeout: float) -> None:
    # Пустой ProxyHandler отключает переменные окружения http_proxy/https_proxy.
    _probe_http(urllib.request.build_opener(urllib.request.ProxyHandler({})), url, timeout)


def _probe_with_retries(
    probe, url: str, timeout: float, attempts: int = 3, delay: float = 0.5
) -> tuple[bool, Exception | None]:
    """Повторяет пробу, чтобы одиночный таймаут не считался блокировкой.

    ZeroTier/Radmin блокируют соединение нестабильно: первая попытка может
    упасть, а следующая пройти. Ошибкой считаем только полный провал всех
    попыток. Общее время ограничено двумя таймаутами, чтобы диагностика
    не висела.
    """
    last: Exception | None = None
    deadline = time.monotonic() + max(timeout * 2.0, 4.0)
    for attempt in range(max(1, attempts)):
        try:
            probe(url, timeout)
            return True, None
        except Exception as exc:
            last = exc
            logger.info(
                "diagnostics: probe attempt %d/%d failed: %s",
                attempt + 1, attempts, exc,
            )
            if attempt + 1 >= attempts or time.monotonic() >= deadline:
                break
            time.sleep(delay)
    return False, last


# ----------------------------------------------------------------------
# Публичный API
# ----------------------------------------------------------------------
def run_diagnostics(
    manager,
    config,
    *,
    probe_url: str | None = None,
    probe_timeout: float | None = None,
) -> DiagnosticReport:
    """Проверяет сетевой стек вокруг запущенного Xray.

    manager — XrayManager (нужны runtime_config/xray_status/log_file).
    config  — app.config (mode/ping_url/ping_timeout).
    """
    issues: list[DiagnosticIssue] = []

    def cfg_get(key: str, default):
        getter = getattr(config, "get", None)
        if callable(getter):
            return getter(key, default)
        return getattr(config, key, default)

    mode = str(
        getattr(config, "mode", None)
        or cfg_get("mode", "proxy")
        or "proxy"
    ).lower()
    runtime = dict(getattr(manager, "runtime_config", {}) or {})
    xray = dict(getattr(manager, "xray_status", {}) or {})
    running = bool(xray.get("running"))
    exit_code = xray.get("exit_code")
    own_pids = _process_tree_pids()
    xray_pid = xray.get("pid")
    if isinstance(xray_pid, int):
        own_pids.add(xray_pid)

    url = str(probe_url or cfg_get("ping_url", DEFAULT_PROBE_URL))
    if not url.lower().startswith(("http://", "https://")):
        url = DEFAULT_PROBE_URL
    timeout = float(probe_timeout or cfg_get("ping_timeout", 5) or 5)
    timeout = max(2.0, min(10.0, timeout))

    # 1. Жив ли сам Xray.
    if not running:
        if exit_code is not None:
            reason = describe_exit_code(exit_code)
            issues.append(DiagnosticIssue(
                code="xray_exited",
                severity="error",
                title=reason.title,
                detail=reason.detail,
                suggestion=reason.suggestion or (
                    "Нажмите «Проверить снова» после переподключения."
                ),
                known=reason.known,
                issue_url=None if reason.known else ISSUES_URL,
            ))
        else:
            issues.append(DiagnosticIssue(
                code="xray_not_running",
                severity="error",
                title="Xray не запущен",
                detail="Supervisor не сообщает о работающем процессе Xray.",
                suggestion="Попробуйте отключиться и подключиться снова.",
            ))

    # 2. Inbound-порты и конфликты за порт.
    ports = _inbound_ports(runtime)
    if running:
        if mode in ("proxy", "both"):
            if not ports:
                issues.append(DiagnosticIssue(
                    code="proxy_no_inbound",
                    severity="error",
                    title="Нет прокси-портов",
                    detail="В runtime-конфиге нет ни одного socks/http/mixed inbound.",
                    suggestion="Проверьте конфигурацию: в режиме PROXY нужен inbound.",
                ))
            for port in ports:
                if not _port_open(port):
                    owner = _port_owner(port)
                    processes = _killable_owner(owner)
                    issues.append(DiagnosticIssue(
                        code="proxy_port_closed",
                        severity="error",
                        title=f"Порт {port} не слушается",
                        detail=(
                            "Xray не открыл локальный прокси-порт. "
                            + (
                                f"Порт занят процессом {owner['name']} (pid {owner['pid']})."
                                if owner else
                                "Порт никто не слушает — возможно, Xray упал или не смог занять порт."
                            )
                        ),
                        suggestion=(
                            "Завершите мешающий процесс и переподключитесь. "
                            "Если порт свободен — приложите лог Xray к issue."
                        ),
                        processes=processes,
                    ))
                else:
                    owner = _port_owner(port)
                    if owner and owner.get("pid") not in own_pids:
                        issues.append(DiagnosticIssue(
                            code="port_conflict",
                            severity="warning",
                            title=f"Порт {port} слушает посторонний процесс",
                            detail=(
                                f"Порт {port} слушает {owner['name']} "
                                f"(pid {owner['pid']}), а не Xray."
                            ),
                            suggestion="Завершите этот процесс — он может перехватывать трафик.",
                            processes=_killable_owner(owner),
                        ))

    # 3. TUN-интерфейс и маршруты.
    # Снимок сети берём один раз: адаптеры (с драйвером), маршруты и
    # фактический интерфейс, через который уходит трафик до probe-хоста.
    probe_ip = _resolve_probe_ip(url)
    snapshot = _network_snapshot(probe_ip)
    adapters = snapshot.get("adapters") or []
    adapter_status = {
        str(a.get("name") or ""): int(a.get("status") or 0) for a in adapters
    }
    own_tun_ifaces = [
        str(a.get("name") or "") for a in adapters
        if _is_own_tun(str(a.get("name") or ""))
    ]
    expected_tun = mode in ("tun", "both")
    if expected_tun and running:
        if not own_tun_ifaces:
            issues.append(DiagnosticIssue(
                code="tun_missing",
                severity="error",
                title="TUN-интерфейс не создан",
                detail=(
                    f"Ожидался интерфейс {TUN_INTERFACE_NAME!r}, но система его не видит. "
                    "Трафик не перехватывается."
                ),
                suggestion=(
                    "Запустите приложение от администратора (Windows) или через sudo (Linux). "
                    "Если уже есть другой VPN/TUN — завершите его."
                ),
            ))
        elif not any(adapter_status.get(name) == 2 for name in own_tun_ifaces):
            issues.append(DiagnosticIssue(
                code="tun_down",
                severity="error",
                title="TUN-интерфейс не поднят",
                detail=f"Интерфейс {', '.join(own_tun_ifaces)} существует, но выключен.",
                suggestion="Переподключитесь. Если не помогает — проверьте права и лог Xray.",
            ))

    default_devices = [
        str(r.get("iface") or "") for r in (snapshot.get("routes") or [])
        if str(r.get("dest") or "") == "0.0.0.0/0" and r.get("iface")
    ]
    default_devices = list(dict.fromkeys(default_devices))
    probe_iface = str(snapshot.get("probe_iface") or "")
    if expected_tun and running and default_devices:
        tun_lower = {name.lower() for name in own_tun_ifaces}
        # Фактический маршрут до probe-хоста важнее списка default-маршрутов:
        # при split-route (0.0.0.0/1 + 128.0.0.0/1) записи 0.0.0.0/0 может
        # не быть вовсе, хотя трафик идёт через наш TUN.
        tun_default = [dev for dev in default_devices if dev.lower() in tun_lower]
        if probe_iface and probe_iface.lower() in tun_lower:
            tun_default = tun_default or [probe_iface]
        if not tun_default:
            issues.append(DiagnosticIssue(
                code="tun_route_missing",
                severity="warning",
                title="Маршрут по умолчанию не через TUN",
                detail="Сейчас default route идёт через: " + ", ".join(default_devices),
                suggestion=(
                    "Трафик может идти мимо туннеля. Переподключитесь, проверьте права "
                    "администратора/root и закройте другие VPN-клиенты."
                ),
            ))

    # 4. Реальная проверка прохождения трафика.
    # urllib умеет только HTTP-прокси, поэтому SOCKS-only inbound проверяем
    # лишь на открытый порт (выше), без ложного «трафик не проходит».
    traffic_ok: bool | None = None
    proxy_probe_ports = _inbound_ports(runtime, {"http", "mixed"})
    if running and mode in ("proxy", "both") and proxy_probe_ports:
        failed: list[int] = []
        probe_ok = False
        for port in proxy_probe_ports:
            if not _port_open(port):
                continue
            ok, _ = _probe_with_retries(
                lambda u, t, p=port: _probe_via_proxy(p, u, t), url, timeout
            )
            if ok:
                probe_ok = True
                break
            failed.append(port)
        if probe_ok:
            traffic_ok = True
        elif failed:
            traffic_ok = False
            issues.append(DiagnosticIssue(
                code="proxy_probe_failed",
                severity="error",
                title="Трафик через прокси не проходит",
                detail=(
                    "Не удалось выполнить запрос через локальный прокси-порт "
                    f"{failed[0]} (3 попытки)."
                ),
                suggestion=(
                    "Проверьте конфиг сервера и интернет. Если ошибка повторяется "
                    "на рабочих конфигах — приложите лог к issue."
                ),
            ))

    if running and mode in ("tun", "both"):
        ok, exc = _probe_with_retries(_probe_direct, url, timeout)
        traffic_ok = ok
        if not ok:
            issues.append(DiagnosticIssue(
                code="tun_probe_failed",
                severity="error",
                title="Трафик через TUN не проходит",
                detail=f"Запрос напрямую не удался (3 попытки): {exc}",
                suggestion=(
                    "Проверьте, что TUN поднят и default route идёт через него. "
                    "Закройте другие VPN-клиенты. Если не помогает — приложите лог Xray."
                ),
            ))

    # 5. Лог Xray: известные ошибки перехвата.
    log_text = _tail_text(Path(getattr(manager, "log_file", "")))
    if log_text:
        seen_codes: set[str] = set()
        for line in reversed(log_text.splitlines()):
            clean = line.strip()
            if not clean:
                continue
            for pattern, code, title, suggestion in _LOG_RULES:
                if code in seen_codes or not pattern.search(clean):
                    continue
                seen_codes.add(code)
                severity = "error"
                processes = []
                actions = []
                if code == "tun_connection_refused":
                    processes = _happ_processes()
                    if traffic_ok is True:
                        severity = "warning"
                    if processes:
                        title = "HAPP может мешать соединениям через TUN"
                        suggestion = (
                            "HAPP ещё запущен в фоне. Завершите его кнопкой ниже "
                            "и переподключитесь."
                        )
                        actions.append({
                            "action": "kill_processes",
                            "pids": [proc["pid"] for proc in processes],
                            "label": "Завершить HAPP",
                        })
                issues.append(DiagnosticIssue(
                    code=code,
                    severity=severity,
                    title=title,
                    detail=clean[:500],
                    suggestion=suggestion,
                    processes=processes,
                    actions=actions,
                ))
                break

        if not seen_codes and not running:
            error_lines = [ln.strip() for ln in log_text.splitlines() if _LOG_ERROR_RE.search(ln)]
            if error_lines:
                issues.append(DiagnosticIssue(
                    code="unknown_xray_error",
                    severity="error",
                    title="Неизвестная ошибка Xray",
                    detail=error_lines[-1][:500],
                    suggestion=(
                        "Готового решения нет. Откройте issue и приложите полный лог Xray."
                    ),
                    known=False,
                    issue_url=ISSUES_URL,
                ))

    # 6. Другие VPN/прокси-клиенты — только при доказанном конфликте.
    # Раньше здесь был поиск по подстроке в имени/командной строке, из-за чего
    # в отчёт попадали Code.exe (флаг --use-angle=d3d11-warp-webgl), git.exe
    # (путь к xray.exe) и любой выключенный VPN-адаптер. Теперь конфликт
    # подтверждается фактами: адаптер поднят, его драйвер — VPN, и (если трафик
    # проходит) через него реально уходит маршрут. Если трафик не проходит,
    # поднятый VPN-адаптер — единственный обоснованный подозреваемый.
    conflicting = _conflicting_adapters(
        snapshot,
        set(own_tun_ifaces),
        traffic_failed=traffic_ok is False,
    ) if expected_tun else []
    if conflicting:
        processes = _processes_for_adapters(conflicting, own_pids)
        labels = ", ".join(f"{a['label']} ({a['name']})" for a in conflicting)
        capturing = [a for a in conflicting if a["captures_probe"] or a["wins_default"]]
        radmin_conflicts = [a for a in conflicting if _is_radmin_adapter(a)]
        actions = _radmin_actions(radmin_conflicts) if radmin_conflicts else []
        if traffic_ok is False and capturing:
            severity = "error"
            detail = (
                f"Трафик не проходит, и через эти адаптеры уходит маршрут: {labels}."
            )
            suggestion = (
                "Завершите указанный клиент и нажмите «Проверить снова». "
                "Если процесса нет в списке — отключите адаптер вручную."
            )
        elif traffic_ok is False:
            severity = "warning"
            detail = (
                f"Трафик не проходит. Подняты сторонние VPN-адаптеры: {labels}. "
                "Они могут блокировать соединение на уровне фильтра."
            )
            suggestion = (
                "Если переподключение не помогает — временно отключите этот клиент "
                "и нажмите «Проверить снова»."
            )
        else:
            severity = "warning"
            detail = (
                f"Трафик проходит, но маршрут делят адаптеры: {labels}. "
                "Возможна утечка мимо туннеля."
            )
            suggestion = (
                "Если соединение нестабильно — завершите лишний клиент. "
                "Если всё работает, предупреждение можно игнорировать."
            )
        if radmin_conflicts:
            suggestion = (
                "Radmin VPN блокирует соединение намертво: завершение процесса "
                "не помогает. Отключите его интерфейс — Radmin поднимет его "
                "сам при следующем подключении."
            )
        issues.append(DiagnosticIssue(
            code="interfering_process",
            severity=severity,
            title="Другой VPN забирает трафик",
            detail=detail,
            suggestion=suggestion,
            processes=processes,
            actions=actions,
        ))

    # 6b. Radmin VPN — персональная обработка.
    # Два доказанных сценария:
    #  1) miniport в состоянии сбоя драйвера: адаптер не поднимается, но его
    #     NDIS-фильтр остаётся в стеке и режет трафик;
    #  2) Radmin запущен раньше нас и «залип»: адаптер выключен, маршрутов нет,
    #     но соединение блокируется, пока интерфейс не отключить.
    # В обоих случаях помогает отключение адаптера + остановка служб Radmin.
    # Драйвер не удаляем: после pnputil /remove-device Radmin не подключается
    # до ручной переустановки драйвера.
    radmin_all = _radmin_adapters(snapshot)
    broken_radmin = _broken_radmin_adapters(snapshot)
    if broken_radmin:
        names = ", ".join(a["name"] for a in broken_radmin)
        codes = ", ".join(str(a["error"]) for a in broken_radmin)
        issues.append(DiagnosticIssue(
            code="radmin_broken_interface",
            severity="error" if traffic_ok is False else "warning",
            title="Radmin VPN сломал сетевой интерфейс",
            detail=(
                f"Адаптер {names} в состоянии сбоя драйвера (код {codes}). "
                "Он не поднят и не держит маршрут, но его фильтр остаётся в стеке "
                "NDIS и блокирует соединение."
            ),
            suggestion=(
                "Отключите интерфейс Radmin VPN и переподключитесь. "
                "Radmin поднимет его сам при следующем подключении."
            ),
            actions=_radmin_actions(broken_radmin),
        ))
    elif (
        traffic_ok is False
        and radmin_all
        and _radmin_started_before_us()
    ):
        issues.append(DiagnosticIssue(
            code="radmin_stale_interface",
            severity="error",
            title="Radmin VPN блокирует соединение",
            detail=(
                "Radmin VPN был запущен раньше Obxod-BOX и держит сетевой фильтр. "
                "Адаптер выключен и маршрутов не держит, но соединение блокируется, "
                "пока его службы работают."
            ),
            suggestion=(
                "Отключите интерфейс Radmin VPN и переподключитесь. "
                "Radmin поднимет его сам при следующем подключении."
            ),
            actions=_radmin_actions(radmin_all),
        ))

    # 7. Неизвестная причина провала: если нет ни одного понятного
    #    конфликта/маршрутной проблемы, предлагаем отправить отчёт в Issues.
    known_cause_codes = {
        "interfering_process", "tun_route_missing", "port_conflict",
        "proxy_port_closed", "port_in_use", "permission_denied",
        "tun_create_failed", "radmin_broken_interface",
        "radmin_stale_interface",
    }
    if not any(i.code in known_cause_codes for i in issues):
        for issue in issues:
            if issue.code in ("proxy_probe_failed", "tun_probe_failed"):
                issue.known = False
                issue.issue_url = ISSUES_URL

    for issue in issues:
        if not issue.known and not issue.issue_url:
            issue.issue_url = ISSUES_URL

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity != "error"]
    if errors:
        summary = (
            f"Обнаружено проблем: {len(errors)}. "
            "Соединение может не перехватывать трафик — исправьте ошибки ниже."
        )
    elif warnings:
        summary = (
            f"Подключение работает, но есть предупреждения: {len(warnings)}. "
            "Проверьте, не мешают ли другие клиенты."
        )
    else:
        summary = "Сетевой стек работает штатно: Xray слушает, трафик проходит."

    return DiagnosticReport(
        ok=not errors,
        mode=mode,
        summary=summary,
        issues=issues,
        traffic_ok=traffic_ok,
    )


def _run_hidden(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True, timeout=timeout, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _stop_radmin_services() -> list[str]:
    """Останавливает службы клиента Radmin VPN (best effort).

    Radmin запускает их сам при нажатии «Подключиться», поэтому остановка
    ничего не ломает — в отличие от удаления драйвера. Имена служб у разных
    версий отличаются, поэтому ищем их по имени и по пути к исполняемому файлу.
    """
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$ErrorActionPreference = 'SilentlyContinue'; "
        "$svc = Get-CimInstance Win32_Service | Where-Object { "
        "$_.ServiceType -notmatch 'Driver' -and ("
        "$_.Name -match 'rvcontrolsvc|rvnetsvc' -or "
        "$_.PathName -match 'rvcontrolsvc|rvnetsvc') }; "
        "$stopped = @(); "
        "foreach ($s in $svc) { "
        "if ($s.State -eq 'Stopped') { continue }; "
        "Stop-Service -Name $s.Name -Force -ErrorAction SilentlyContinue; "
        "$now = Get-Service -Name $s.Name -ErrorAction SilentlyContinue; "
        "if ($now -and $now.Status -eq 'Stopped') { $stopped += $s.Name } }; "
        "$stopped | ConvertTo-Json -Compress"
    )
    try:
        r = _run_hidden([
            "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
        ], timeout=30)
    except Exception as exc:
        logger.debug("остановка служб Radmin: %s", exc)
        return []
    raw = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.debug("остановка служб Radmin: %s", raw[:200])
        return []
    if isinstance(data, str):
        return [data]
    if isinstance(data, list):
        return [str(item) for item in data if item]
    return []


def disable_interface(device: str) -> tuple[bool, str]:
    """Отключает сетевой интерфейс Radmin VPN, не удаляя драйвер.

    Раньше здесь вызывался ``pnputil /remove-device``: он выдёргивал miniport
    вместе с драйвером, и Radmin после этого не подключался до ручной
    переустановки драйвера. Теперь адаптер только отключается (а при сбое
    драйвера — через ``pnputil /disable-device``), а службы Radmin
    останавливаются: клиент поднимает их сам при следующем подключении.

    Устройство трогаем только если это действительно Radmin-адаптер, чтобы
    мост не превратился в универсальный «отключи любое устройство».
    """
    device = (device or "").strip()
    if not device:
        return False, "Не указан идентификатор устройства"
    if sys.platform != "win32":
        return False, "Отключение интерфейса поддерживается только в Windows"

    snapshot = _network_snapshot(None)
    target = None
    for adapter in snapshot.get("adapters") or []:
        if str(adapter.get("pnp") or "").lower() == device.lower():
            target = adapter
            break
    if target is None:
        return False, "Устройство не найдено — возможно, оно уже отключено"
    if not _is_radmin_adapter(target):
        return False, "Это не Radmin-адаптер — не трогаем"

    name = str(target.get("name") or "")

    disabled = False
    if name:
        try:
            r = _run_hidden([
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"Disable-NetAdapter -Name '{name.replace(chr(39), chr(39) * 2)}' "
                "-Confirm:$false -ErrorAction Stop",
            ])
        except Exception as exc:
            logger.debug("Disable-NetAdapter %s: %s", name, exc)
            r = None
        if r is not None:
            if r.returncode == 0:
                disabled = True
            else:
                logger.debug(
                    "Disable-NetAdapter %s: код %s (%s)",
                    name, r.returncode,
                    ((r.stderr or b"") + (r.stdout or b"")).decode(
                        "utf-8", errors="replace"
                    )[:200],
                )

    # Адаптер в состоянии сбоя драйвера Disable-NetAdapter не берёт —
    # отключаем само PnP-устройство (драйвер при этом остаётся в системе).
    if not disabled:
        try:
            r = _run_hidden(["pnputil", "/disable-device", device])
        except Exception as exc:
            return False, f"pnputil не запустился: {exc}"
        out = ((r.stdout or b"") + (r.stderr or b"")).decode(
            "utf-8", errors="replace"
        ).strip()
        if r.returncode != 0:
            detail = out or f"код {r.returncode}"
            if "access" in detail.lower() or "отказано" in detail.lower():
                return False, (
                    "Нужны права администратора — перезапустите приложение от админа"
                )
            return False, f"Не удалось отключить интерфейс: {detail[:300]}"

    label = name or device
    # Службы останавливаем после отключения адаптера: Radmin поднимает их сам
    # при следующем подключении, а до этого они не должны вернуть фильтр.
    stopped = _stop_radmin_services()
    if stopped:
        return True, (
            f"Интерфейс {label} отключён, службы Radmin остановлены "
            f"({', '.join(stopped)})"
        )
    return True, f"Интерфейс {label} отключён"


def kill_process(pid: int, protected_pids: set[int] | None = None) -> tuple[bool, str]:
    """Завершает известный мешающий процесс. Возвращает (ok, сообщение)."""
    if psutil is None:
        return False, "psutil недоступен"
    protected = set(protected_pids or set())
    protected.update(_process_tree_pids())
    if pid <= 0 or pid in protected:
        return False, "Этот процесс защищён"

    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        return False, f"Процесс недоступен: {exc}"

    try:
        name = proc.name()
    except (psutil.Error, OSError) as exc:
        return False, f"Не удалось прочитать процесс: {exc}"

    try:
        exe = proc.exe()
    except (psutil.Error, OSError):
        exe = ""
    if _known_process_reason(name, exe) is None:
        return False, "Процесс не похож на известный VPN/прокси — не трогаем"

    try:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
    except (psutil.Error, OSError) as exc:
        return False, f"Не удалось завершить {name}: {exc}"
    return True, f"Процесс {name} (pid {pid}) завершён"


def report_as_json(report: DiagnosticReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False)
