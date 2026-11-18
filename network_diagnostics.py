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
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

logger = logging.getLogger(__name__)

ISSUES_URL = "https://github.com/ADSFAL-US/Obxod-Box/issues/new"
DEFAULT_PROBE_URL = "https://www.gstatic.com/generate_204"
TUN_INTERFACE_NAME = "obxod-tun"
TUN_NAME_HINTS = ("obxod-tun", "wintun", "tun", "tap", "wireguard", "wg")
VIRTUAL_IFACE_HINTS = (
    "tun", "tap", "wintun", "wireguard", "tailscale", "zerotier", "openvpn",
    "nordlynx", "proton", "hamachi", "radmin", "warp", "vpn",
)
SKIP_IFACE_PREFIXES = ("lo", "loopback", "docker", "br-", "veth", "virbr", "vmnet")

# Клиенты, которые любят держать TUN/WFP/системный прокси и мешать Xray.
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
        re.compile(r"permission denied|operation not permitted|access is denied", re.I),
        "permission_denied",
        "Xray не хватает прав",
        "Запустите приложение от имени администратора (Windows) или через sudo (Linux).",
    ),
    (
        re.compile(
            r"(wintun|tun|tap).*(fail|error|denied|cannot|unable|already)"
            r"|(fail|error|denied|cannot|unable).*(wintun|tun|tap)",
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
    issue_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnosticReport:
    ok: bool
    mode: str
    summary: str
    issues: list[DiagnosticIssue] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "summary": self.summary,
            "issues": [i.to_dict() for i in self.issues],
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


def _inbound_ports(runtime: dict) -> list[int]:
    ports: list[int] = []
    for ib in runtime.get("inbounds") or []:
        proto = (ib.get("protocol") or "").lower()
        if proto in ("tun", "dokodemo-door", "tunnel", "dns"):
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


def _hint_matches(name: str, cmdline: str, hint: str) -> bool:
    blob = f"{name} {cmdline}".lower()
    if hint in _SHORT_HINTS:
        return re.search(r"(^|[^a-z0-9])" + re.escape(hint) + r"([^a-z0-9]|$)", blob) is not None
    return hint in blob


def _known_process_reason(name: str, cmdline: str) -> str | None:
    for hint in INTERFERING_PROCESS_HINTS:
        if _hint_matches(name, cmdline, hint):
            return hint
    return None


def _collect_interfering_processes(own_pids: set[int]) -> list[dict[str, Any]]:
    if psutil is None:
        return []
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    try:
        iterator = psutil.process_iter(["pid", "name", "exe", "cmdline"])
    except Exception as exc:
        logger.debug("process_iter: %s", exc)
        return []

    for proc in iterator:
        try:
            pid = int(proc.info.get("pid") or 0)
            if not pid or pid in own_pids or pid in seen:
                continue
            name = str(proc.info.get("name") or "")
            exe = str(proc.info.get("exe") or "")
            cmdline = " ".join(proc.info.get("cmdline") or [])
            reason = _known_process_reason(f"{name} {exe}", cmdline)
            if reason is None:
                continue
            seen.add(pid)
            found.append({
                "pid": pid,
                "name": name or Path(exe).name or f"pid {pid}",
                "reason": reason,
            })
            if len(found) >= 12:
                break
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
            if conn.status not in (psutil.CONN_LISTEN, psutil.CONN_ESTABLISHED):
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


def _interface_names() -> dict[str, bool]:
    """Имя интерфейса -> поднят ли он."""
    if psutil is None:
        return {}
    try:
        stats = psutil.net_if_stats()
        return {name: bool(st.isup) for name, st in stats.items()}
    except Exception as exc:
        logger.debug("net_if_stats: %s", exc)
        return {}


def _default_route_devices() -> list[str]:
    if sys.platform.startswith("linux"):
        try:
            r = subprocess.run(
                ["ip", "-4", "route", "show", "default"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            devices = re.findall(r"\bdev\s+(\S+)", r.stdout or "")
            return list(dict.fromkeys(devices))
        except Exception as exc:
            logger.debug("ip route default: %s", exc)
    elif sys.platform == "win32":
        command = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
            "| Select-Object -ExpandProperty InterfaceAlias"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True, text=True, timeout=5, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]
        except Exception as exc:
            logger.debug("Get-NetRoute default: %s", exc)
    elif sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["route", "-n", "get", "default"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            for line in (r.stdout or "").splitlines():
                if line.strip().startswith("interface:"):
                    return [line.split(":", 1)[1].strip()]
        except Exception as exc:
            logger.debug("route get default: %s", exc)
    return []


def _is_tun_name(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in TUN_NAME_HINTS)


def _is_vpn_iface(name: str) -> bool:
    low = name.lower()
    if low.startswith(SKIP_IFACE_PREFIXES):
        return False
    return any(hint in low for hint in VIRTUAL_IFACE_HINTS)


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
    mode = str(getattr(config, "mode", "proxy") or "proxy").lower()
    runtime = dict(getattr(manager, "runtime_config", {}) or {})
    xray = dict(getattr(manager, "xray_status", {}) or {})
    running = bool(xray.get("running"))
    exit_code = xray.get("exit_code")
    own_pids = _process_tree_pids()
    xray_pid = xray.get("pid")
    if isinstance(xray_pid, int):
        own_pids.add(xray_pid)

    def cfg_get(key: str, default):
        getter = getattr(config, "get", None)
        if callable(getter):
            return getter(key, default)
        return getattr(config, key, default)

    url = str(probe_url or cfg_get("ping_url", DEFAULT_PROBE_URL))
    if not url.lower().startswith(("http://", "https://")):
        url = DEFAULT_PROBE_URL
    timeout = float(probe_timeout or cfg_get("ping_timeout", 5) or 5)
    timeout = max(2.0, min(10.0, timeout))

    # 1. Жив ли сам Xray.
    if not running:
        if exit_code is not None:
            issues.append(DiagnosticIssue(
                code="xray_exited",
                severity="error",
                title="Xray завершился",
                detail=f"Процесс Xray остановился с кодом {exit_code}.",
                suggestion=(
                    "Нажмите «Проверить снова» после переподключения. Если ошибка "
                    "повторяется — приложите лог Xray к issue."
                ),
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
                    processes = [owner] if owner else []
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
                            processes=[owner],
                        ))

    # 3. TUN-интерфейс и маршруты.
    ifaces = _interface_names()
    tun_ifaces = [name for name in ifaces if _is_tun_name(name)]
    own_tun_ifaces = [
        name for name in tun_ifaces
        if "obxod" in name.lower() or TUN_INTERFACE_NAME.lower() in name.lower()
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
        elif not any(ifaces.get(name) for name in own_tun_ifaces):
            issues.append(DiagnosticIssue(
                code="tun_down",
                severity="error",
                title="TUN-интерфейс не поднят",
                detail=f"Интерфейс {', '.join(own_tun_ifaces)} существует, но выключен.",
                suggestion="Переподключитесь. Если не помогает — проверьте права и лог Xray.",
            ))

    other_vpn_ifaces = [
        name for name, up in ifaces.items()
        if up and _is_vpn_iface(name)
        and not _is_tun_name(name)
        and name not in own_tun_ifaces
    ]
    if other_vpn_ifaces:
        issues.append(DiagnosticIssue(
            code="other_vpn_iface",
            severity="warning",
            title="Активен другой VPN-интерфейс",
            detail="Найдены интерфейсы: " + ", ".join(sorted(other_vpn_ifaces)),
            suggestion=(
                "Два VPN-клиента могут бороться за маршруты. "
                "Завершите лишний клиент и переподключитесь."
            ),
        ))

    default_devices = _default_route_devices()
    if expected_tun and running and default_devices:
        tun_lower = {name.lower() for name in own_tun_ifaces}
        tun_default = [
            dev for dev in default_devices
            if dev.lower() in tun_lower
            or (not own_tun_ifaces and _is_tun_name(dev))
        ]
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
        other_defaults = [
            dev for dev in default_devices
            if dev not in tun_default and _is_vpn_iface(dev)
        ]
        if other_defaults:
            issues.append(DiagnosticIssue(
                code="vpn_default_route",
                severity="warning",
                title="Другой VPN держит default route",
                detail="Default route указывает на: " + ", ".join(other_defaults),
                suggestion="Завершите другой VPN-клиент, чтобы Xray смог перехватить трафик.",
            ))

    # 4. Реальная проверка прохождения трафика.
    if running and mode in ("proxy", "both") and ports:
        failed: list[int] = []
        for port in ports:
            if not _port_open(port):
                continue
            try:
                _probe_via_proxy(port, url, timeout)
                break
            except Exception as exc:
                logger.info("diagnostics: proxy port %s probe failed: %s", port, exc)
                failed.append(port)
        else:
            if failed or ports:
                issues.append(DiagnosticIssue(
                    code="proxy_probe_failed",
                    severity="error",
                    title="Трафик через прокси не проходит",
                    detail=(
                        "Не удалось выполнить запрос через локальный прокси-порт"
                        + (f" {failed[0]}" if failed else "") + "."
                    ),
                    suggestion=(
                        "Проверьте конфиг сервера и интернет. Если ошибка повторяется "
                        "на рабочих конфигах — приложите лог к issue."
                    ),
                ))

    if running and mode in ("tun", "both"):
        try:
            _probe_direct(url, timeout)
        except Exception as exc:
            logger.info("diagnostics: tun probe failed: %s", exc)
            issues.append(DiagnosticIssue(
                code="tun_probe_failed",
                severity="error",
                title="Трафик через TUN не проходит",
                detail=f"Запрос напрямую не удался: {exc}",
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
                issues.append(DiagnosticIssue(
                    code=code,
                    severity="error",
                    title=title,
                    detail=clean[:500],
                    suggestion=suggestion,
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

    # 6. Другие VPN/прокси-клиенты.
    processes = _collect_interfering_processes(own_pids)
    if processes:
        names = ", ".join(
            f"{p['name']} (pid {p['pid']})" for p in processes[:6]
        )
        issues.append(DiagnosticIssue(
            code="interfering_process",
            severity="warning",
            title="Найдены другие VPN/прокси-клиенты",
            detail=f"Они могут перехватывать трафик: {names}",
            suggestion=(
                "Завершите лишний клиент и нажмите «Проверить снова». "
                "Обычно достаточно оставить только Obxod-BOX."
            ),
            processes=processes,
        ))

    # 7. Неизвестная причина провала: если нет ни одного понятного
    #    конфликта/маршрутной проблемы, предлагаем отправить отчёт в Issues.
    known_cause_codes = {
        "interfering_process", "other_vpn_iface", "vpn_default_route",
        "tun_route_missing", "port_conflict", "proxy_port_closed",
        "port_in_use", "permission_denied", "tun_create_failed",
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
    )


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
        cmdline = " ".join(proc.cmdline())
    except (psutil.Error, OSError) as exc:
        return False, f"Не удалось прочитать процесс: {exc}"

    if _known_process_reason(f"{name} {proc.exe()}", cmdline) is None:
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
