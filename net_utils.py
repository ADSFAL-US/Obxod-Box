# net_utils.py
"""Утилиты: права, Wintun, Xray, geofiles, трансформация конфига."""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Колбэк прогресса: progress_cb(downloaded_bytes, total_bytes).
# total_bytes == 0, если сервер не сообщил Content-Length.
ProgressCB = Callable[[int, int], None]


# ══════════════════════════════════════════════════════════════════════
# ПРАВА АДМИНИСТРАТОРА
# ══════════════════════════════════════════════════════════════════════
def is_admin() -> bool:
    if sys.platform != "win32":
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


_ELEVATED_ENV = "OBXOD_ELEVATED"


def relaunch_as_admin() -> bool:
    if sys.platform != "win32":
        return False

    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = _join_args(sys.argv[1:])
    else:
        exe = sys.executable
        params = _join_args(sys.argv)

    os.environ[_ELEVATED_ENV] = "1"

    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, None, 1,
        )
    except Exception as e:
        logger.error("ShellExecuteW(runas) упал: %s", e)
        os.environ.pop(_ELEVATED_ENV, None)
        return False

    if result > 32:
        logger.info("UAC принят, дочерний процесс запущен")
        return True

    logger.warning("ShellExecuteW(runas) вернул код %s — UAC отклонён", result)
    os.environ.pop(_ELEVATED_ENV, None)
    return False


def _join_args(args: list[str]) -> str:
    out = []
    for a in args:
        if not a:
            out.append('""')
            continue
        if " " in a or '"' in a:
            out.append('"' + a.replace('"', r'\"') + '"')
        else:
            out.append(a)
    return " ".join(out)


def was_elevated_attempt() -> bool:
    return os.environ.get(_ELEVATED_ENV) == "1"


# ══════════════════════════════════════════════════════════════════════
# ЗАФИКСИРОВАННЫЕ ВЕРСИИ
# ══════════════════════════════════════════════════════════════════════
XRAY_VERSION = "26.9.9"

WINTUN_VERSION = "0.14.1"
WINTUN_ZIP_URL = f"https://www.wintun.net/builds/wintun-{WINTUN_VERSION}.zip"
WINTUN_ZIP_SHA256 = "07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51"

GEOIP_URL = (
    "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/"
    "latest/download/geoip.dat"
)
GEOSITE_URL = (
    "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/"
    "latest/download/geosite.dat"
)

_ARCH_MAP = {
    "AMD64": "amd64",
    "x86": "x86",
    "ARM64": "arm64",
    "ARM32": "arm",
}


def _get_arch_folder() -> str:
    """Архитектура для путей внутри архива Wintun (только Windows)."""
    machine = platform.machine().upper()
    if machine in ("AMD64", "X86_64"):
        return _ARCH_MAP["AMD64"]
    if machine in ("X86", "I386", "I686"):
        return _ARCH_MAP["x86"]
    if machine in ("ARM64", "AARCH64"):
        return _ARCH_MAP["ARM64"]
    if machine in ("ARM", "ARMV7L"):
        return _ARCH_MAP["ARM32"]
    logger.warning("Неизвестная архитектура %r, использую amd64", machine)
    return _ARCH_MAP["AMD64"]


def xray_binary_name() -> str:
    """Имя исполняемого файла Xray на текущей платформе."""
    if sys.platform == "win32":
        return "xray.exe"
    return "xray"


def _xray_release_asset() -> str:
    """Имя zip-ассета в релизе XTLS/Xray-core для текущей ОС/архитектуры."""
    machine = platform.machine().upper()
    is_arm64 = machine in ("ARM64", "AARCH64")
    is_arm = machine in ("ARM", "ARMV7L") and not is_arm64

    if sys.platform == "win32":
        if is_arm64:
            return "Xray-windows-arm64-v8a.zip"
        return "Xray-windows-64.zip"
    if sys.platform == "darwin":
        if is_arm64:
            return "Xray-macos-arm64-v8a.zip"
        return "Xray-macos-64.zip"
    # linux и прочее unix-like
    if is_arm64:
        return "Xray-linux-arm64-v8a.zip"
    if is_arm:
        return "Xray-linux-arm32-v7a.zip"
    return "Xray-linux-64.zip"


def xray_zip_url() -> str:
    asset = _xray_release_asset()
    return (
        f"https://github.com/XTLS/Xray-core/releases/download/"
        f"v{XRAY_VERSION}/{asset}"
    )


# ══════════════════════════════════════════════════════════════════════
# СКАЧИВАНИЕ БИНАРНЫХ ЗАВИСИМОСТЕЙ
# ══════════════════════════════════════════════════════════════════════
def _urlretrieve_with_progress(
    url: str,
    dest: Path,
    progress_cb: ProgressCB | None = None,
) -> None:
    """urllib.request.urlretrieve с прогресс-колбэком.

    progress_cb(downloaded_bytes, total_bytes) вызывается по мере загрузки.
    Если сервер не сообщил Content-Length, total_bytes == 0.
    Исключения из колбэка проглатываются — прогресс не должен ломать загрузку.
    """
    if progress_cb is None:
        urllib.request.urlretrieve(url, dest)
        return

    # urlretrieve вызывает reporthook(block_num, block_size, total_size).
    # block_size обычно 8192 (для https — chunk от http.client).
    def hook(block_num: int, block_size: int, total_size: int) -> None:
        downloaded = block_num * block_size
        total = total_size if total_size > 0 else 0
        if total > 0 and downloaded > total:
            downloaded = total
        try:
            progress_cb(int(downloaded), int(total))
        except Exception as exc:
            logger.debug("hook: %s", exc)

    urllib.request.urlretrieve(url, dest, reporthook=hook)


def download_wintun(
    dest_dir: Path,
    progress_cb: ProgressCB | None = None,
) -> Path:
    """Скачивает wintun.dll в dest_dir, проверяет SHA256.

    Только Windows: на Linux/macOS TUN использует ядро, Wintun не нужен.
    """
    if sys.platform != "win32":
        raise RuntimeError("Wintun нужен только на Windows.")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dll_path = dest_dir / "wintun.dll"

    if dll_path.exists():
        logger.info("wintun.dll уже существует: %s", dll_path)
        if progress_cb is not None:
            try:
                progress_cb(1, 1)
            except Exception as exc:
                logger.debug("download_wintun: %s", exc)
        return dll_path

    zip_path = dest_dir / f"wintun-{WINTUN_VERSION}.zip"

    logger.info("Скачиваю Wintun %s...", WINTUN_VERSION)
    try:
        _urlretrieve_with_progress(WINTUN_ZIP_URL, zip_path, progress_cb)
    except Exception as e:
        raise RuntimeError(f"Не удалось скачать Wintun: {e}") from e

    sha256 = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    actual_hash = sha256.hexdigest()
    if actual_hash != WINTUN_ZIP_SHA256:
        zip_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Хеш Wintun не совпал! Ожидался {WINTUN_ZIP_SHA256}, "
            f"получен {actual_hash}."
        )
    logger.info("SHA256 Wintun совпал.")

    arch_folder = _get_arch_folder()
    dll_in_zip = f"wintun/bin/{arch_folder}/wintun.dll"
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if dll_in_zip not in zf.namelist():
                raise RuntimeError(
                    f"В архиве нет {dll_in_zip}. "
                    f"Доступные файлы: {zf.namelist()[:10]}..."
                )
            with zf.open(dll_in_zip) as src, open(dll_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"Архив Wintun повреждён: {e}") from e
    finally:
        zip_path.unlink(missing_ok=True)

    logger.info("wintun.dll извлечён в %s", dll_path)
    return dll_path


def download_xray(
    dest_dir: Path,
    progress_cb: ProgressCB | None = None,
) -> Path:
    """Скачивает официальный бинарник Xray в dest_dir. Версия зафиксирована.

    Windows → xray.exe, Linux/macOS → xray (с +x).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    bin_name = xray_binary_name()
    exe_path = dest_dir / bin_name

    if exe_path.exists():
        logger.info("%s уже существует: %s", bin_name, exe_path)
        if progress_cb is not None:
            try:
                progress_cb(1, 1)
            except Exception as exc:
                logger.debug("download_xray: %s", exc)
        return exe_path

    asset = _xray_release_asset()
    zip_path = dest_dir / f"{asset.replace('.zip', '')}-{XRAY_VERSION}.zip"
    url = xray_zip_url()

    logger.info("Скачиваю Xray %s (%s)...", XRAY_VERSION, asset)
    try:
        _urlretrieve_with_progress(url, zip_path, progress_cb)
    except Exception as e:
        raise RuntimeError(f"Не удалось скачать Xray: {e}") from e

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            # Ищем xray.exe или xray (без расширения) в корне или подпапке
            candidates = ("xray.exe", "xray")
            member = next(
                (
                    n for n in names
                    if n in candidates
                    or any(n.endswith("/" + c) for c in candidates)
                ),
                None,
            )
            if member is None:
                raise RuntimeError(
                    f"В архиве Xray нет бинарника. Файлы: {names[:10]}"
                )
            with zf.open(member) as src, open(exe_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"Архив Xray повреждён: {e}") from e
    finally:
        zip_path.unlink(missing_ok=True)

    if sys.platform != "win32":
        try:
            exe_path.chmod(exe_path.stat().st_mode | 0o111)
        except OSError as e:
            logger.warning("Не удалось выставить +x на %s: %s", exe_path, e)

    logger.info("%s извлечён в %s", bin_name, exe_path)
    return exe_path


def download_geofile(
    url: str,
    dest: Path,
    name: str,
    progress_cb: ProgressCB | None = None,
) -> Path:
    """Скачивает один geo-файл, если его ещё нет."""
    if dest.exists():
        logger.info("%s уже существует: %s", name, dest)
        if progress_cb is not None:
            try:
                progress_cb(1, 1)
            except Exception as exc:
                logger.debug("download_geofile: %s", exc)
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Скачиваю %s...", name)
    try:
        _urlretrieve_with_progress(url, dest, progress_cb)
    except Exception as e:
        logger.warning("Не удалось скачать %s: %s", name, e)
        raise
    logger.info("%s сохранён: %s", name, dest)
    return dest


def download_geofiles(geo_dir: Path) -> tuple[Path, Path]:
    """Скачивает geoip.dat и geosite.dat в geo_dir (если их нет).

    Совместимо со старым API без колбэка прогресса.
    """
    geo_dir.mkdir(parents=True, exist_ok=True)
    geoip = geo_dir / "geoip.dat"
    geosite = geo_dir / "geosite.dat"

    for path, url, name in (
        (geoip, GEOIP_URL, "geoip.dat"),
        (geosite, GEOSITE_URL, "geosite.dat"),
    ):
        if path.exists():
            logger.info("%s уже существует: %s", name, path)
            continue
        try:
            download_geofile(url, path, name)
        except Exception as e:
            logger.warning("Не удалось скачать %s: %s", name, e)

    return geoip, geosite


# ══════════════════════════════════════════════════════════════════════
# ТРАНСФОРМАЦИЯ XRAY-КОНФИГА ПО РЕЖИМУ
# ══════════════════════════════════════════════════════════════════════
TUN_INBOUND_TAG = "obxod-tun-in"
TUN_INTERFACE_NAME = "obxod-tun"
TUN_GATEWAY_V4 = "172.19.0.1/30"
TUN_GATEWAY_V6 = "fdfe:dcba:9876::1/126"


def _build_tun_inbound() -> dict:
    return {
        "tag": TUN_INBOUND_TAG,
        "protocol": "tun",
        "settings": {
            "name": TUN_INTERFACE_NAME,
            "MTU": 1500,
            "gateway": [TUN_GATEWAY_V4, TUN_GATEWAY_V6],
            "dns": ["1.1.1.1", "8.8.8.8"],
            "autoSystemRoutingTable": ["0.0.0.0/0", "::/0"],
        },
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
        },
    }


def _strip_tun_inbound(config: dict) -> dict:
    inbounds = [
        ib for ib in (config.get("inbounds") or [])
        if ib.get("tag") != TUN_INBOUND_TAG
        and (ib.get("protocol") or "").lower() != "tun"
    ]
    config["inbounds"] = inbounds
    return config


def _has_tun_inbound(config: dict) -> bool:
    for ib in config.get("inbounds") or []:
        if ib.get("tag") == TUN_INBOUND_TAG:
            return True
        if (ib.get("protocol") or "").lower() == "tun":
            return True
    return False


def _resolve_tun_target(cfg: dict) -> tuple[str, str] | None:
    """Определяет, куда направить трафик из TUN-инбаунда.

    Приоритет:
      1. Первый балансировщик (routing.balancers[0].tag) — для авто-выбора.
      2. Первый "боевой" outbound (не freedom/blackhole/dns) — обычный конфиг.
    Возвращает (ключ, значение): ("balancerTag", "auto-select") или
    ("outboundTag", "proxy"). None — если ничего не нашли.
    """
    routing = cfg.get("routing") or {}
    balancers = routing.get("balancers") or []
    if balancers:
        tag = balancers[0].get("tag")
        if tag:
            return "balancerTag", tag

    for ob in cfg.get("outbounds") or []:
        proto = (ob.get("protocol") or "").lower()
        if proto in ("freedom", "blackhole", "dns"):
            continue
        tag = ob.get("tag")
        if tag:
            return "outboundTag", tag

    return None


def apply_mode_to_config(xray_config: dict, mode: str, physical_iface: str | None = None) -> dict:
    import copy
    cfg = copy.deepcopy(xray_config)

    want_tun = mode in ("tun", "both")
    want_proxy = mode in ("proxy", "both")

    _strip_tun_inbound(cfg)

    if not want_proxy:
        keep = []
        for ib in cfg.get("inbounds") or []:
            proto = (ib.get("protocol") or "").lower()
            if proto in ("socks", "http", "mixed"):
                continue
            keep.append(ib)
        cfg["inbounds"] = keep

    if want_tun:
        cfg.setdefault("inbounds", [])
        if not _has_tun_inbound(cfg):
            tun = _build_tun_inbound()
            # Явно прописываем физический интерфейс для outbound-трафика Xray
            if physical_iface:
                tun["settings"]["autoOutboundsInterface"] = physical_iface
            cfg["inbounds"].append(tun)

        # routing как раньше
        target = _resolve_tun_target(cfg)
        if target is not None:
            key, value = target
            cfg.setdefault("routing", {})
            cfg["routing"].setdefault("rules", [])
            already = any(
                TUN_INBOUND_TAG in (r.get("inboundTag") or [])
                for r in cfg["routing"]["rules"]
            )
            if not already:
                rule = {"type": "field", "inboundTag": [TUN_INBOUND_TAG]}
                rule[key] = value
                cfg["routing"]["rules"].insert(0, rule)

    return cfg

# ══════════════════════════════════════════════════════════════════════
# ПРЕСЕТЫ МАРШРУТИЗАЦИИ (cfg/routing/*.json)
# ══════════════════════════════════════════════════════════════════════
def list_routing_sets(routing_dir: Path) -> list[dict]:
    """Возвращает список доступных пресетов маршрутизации (без .json)."""
    if not routing_dir.exists():
        return []
    out: list[dict] = []
    for f in sorted(routing_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Не удалось прочитать пресет %s: %s", f, e)
            continue
        out.append({
            "name": f.stem,
            "title": data.get("name") or f.stem,
            "description": data.get("description") or "",
        })
    return out


def load_routing_set(routing_dir: Path, name: str | None) -> dict | None:
    """Загружает один пресет по имени (без расширения)."""
    if not name:
        return None
    # Простейшая защита от path traversal.
    if "/" in name or "\\" in name or ".." in name:
        logger.warning("Недопустимое имя пресета: %r", name)
        return None
    path = routing_dir / f"{name}.json"
    if not path.exists():
        logger.warning("Пресет маршрутизации не найден: %s", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Ошибка чтения пресета %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        logger.error("Пресет %s должен быть JSON-объектом", path)
        return None
    return data


def _resolve_outbound_targets(cfg: dict) -> dict[str, dict]:
    """Определяет, куда направлять роли proxy / direct / block.

    Возвращает dict role -> {"outboundTag": ...} или {"balancerTag": ...}.
    Для proxy приоритет у балансировщика (если он есть).
    """
    targets: dict[str, dict] = {}

    balancers = (cfg.get("routing") or {}).get("balancers") or []
    proxy_balancer_tag = balancers[0].get("tag") if balancers else None

    proxy_tag: str | None = None
    direct_tag: str | None = None
    block_tag: str | None = None

    for ob in cfg.get("outbounds") or []:
        proto = (ob.get("protocol") or "").lower()
        tag = ob.get("tag")
        if not tag:
            continue
        if proto == "freedom":
            direct_tag = direct_tag or tag
        elif proto == "blackhole":
            block_tag = block_tag or tag
        elif proto == "dns":
            continue
        else:
            proxy_tag = proxy_tag or tag

    if proxy_balancer_tag:
        targets["proxy"] = {"balancerTag": proxy_balancer_tag}
    elif proxy_tag:
        targets["proxy"] = {"outboundTag": proxy_tag}

    targets["direct"] = {"outboundTag": direct_tag or "direct"}
    targets["block"] = {"outboundTag": block_tag or "block"}
    return targets


def apply_routing_to_config(xray_config: dict, routing: dict | None) -> dict:
    """Применяет пресет маршрутизации к runtime-конфигу Xray.

    Правила пресета вставляются В НАЧАЛО routing.rules (перед всеми
    сгенерированными apply_mode_to_config правилами), чтобы whitelist
    имел приоритет над catch-all TUN-правилом.
    """
    import copy
    cfg = copy.deepcopy(xray_config)
    if not routing or not routing.get("rules"):
        return cfg

    targets = _resolve_outbound_targets(cfg)
    if "proxy" not in targets:
        logger.warning("Не найден proxy-outbound — пресет маршрутизации пропущен")
        return cfg

    resolved: list[dict] = []
    for raw in routing["rules"]:
        if not isinstance(raw, dict):
            continue
        role = raw.get("outbound", "proxy")
        if role not in targets:
            logger.warning("Неизвестная роль outbound=%r в пресете", role)
            continue
        rule = {k: v for k, v in raw.items() if k not in ("outbound", "note")}
        rule.update(targets[role])
        resolved.append(rule)

    if not resolved:
        return cfg

    cfg.setdefault("routing", {})
    if routing.get("domainStrategy"):
        cfg["routing"]["domainStrategy"] = routing["domainStrategy"]

    existing = cfg["routing"].get("rules") or []
    cfg["routing"]["rules"] = resolved + existing
    return cfg
