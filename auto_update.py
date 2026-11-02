"""Модуль автообновлений Obxod-BOX.

Проверяет последний релиз в GitHub репозитории, скачивает установщик
под текущую платформу и запускает его в тихом режиме.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com/repos/ADSFAL-US/Obxod-Box/releases/latest"

# Имя ассета установщика для Windows
WINDOWS_ASSET_PREFIX = "Obxod-BOX-Setup-v"
WINDOWS_ASSET_SUFFIX = ".exe"


def get_downloads_folder() -> Path:
    """Возвращает папку загрузок пользователя."""
    return Path.home() / "Downloads"


def get_platform_asset_name() -> Optional[str]:
    """Возвращает суффикс имени ассета для текущей платформы или None.

    На данный момент установщик есть только для Windows.
    """
    if sys.platform == "win32":
        return "windows"
    return None


def is_newer_version(candidate: str, current: str) -> bool:
    """Сравнивает две версии в формате semver (X.Y.Z).

    Возвращает True, если candidate новее current.
    """
    def parse(v: str) -> tuple[int, int, int]:
        parts = v.strip().lstrip("v").split(".")
        return tuple(int(p) for p in parts[:3])  # type: ignore[return-value]

    try:
        cand = parse(candidate)
        curr = parse(current)
    except (ValueError, AttributeError):
        logger.warning("Не удалось распарсить версии: candidate=%r, current=%r", candidate, current)
        return False

    return cand > curr


def check_for_update(current_version: str) -> Optional[dict]:
    """Проверяет наличие новой версии в GitHub releases.

    Возвращает dict с ключами:
        version      — строка версии (например, "1.0.1")
        download_url — URL для скачивания установщика
        asset_name   — имя файла ассета
    Или None, если обновлений нет или произошла ошибка.
    """
    try:
        req = urllib.request.Request(
            GITHUB_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "Obxod-BOX/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))

        tag_name = data.get("tag_name", "")
        if not tag_name:
            logger.warning("GitHub release не содержит tag_name")
            return None

        # Убираем префикс 'v' если есть
        release_version = tag_name.lstrip("v")

        if not is_newer_version(release_version, current_version):
            logger.info("Обновлений нет. Текущая: %s, последняя: %s", current_version, release_version)
            return None

        # Ищем ассет установщика для текущей платформы
        assets = data.get("assets", [])
        platform = get_platform_asset_name()
        if platform is None:
            logger.info("Нет установщика для платформы %s", sys.platform)
            return {
                "version": release_version,
                "download_url": None,
                "asset_name": None,
                "no_installer_for_platform": True,
            }

        expected_name = f"{WINDOWS_ASSET_PREFIX}{release_version}{WINDOWS_ASSET_SUFFIX}"
        for asset in assets:
            asset_name = asset.get("name", "")
            if asset_name == expected_name:
                download_url = asset.get("browser_download_url")
                logger.info(
                    "Найдено обновление: v%s, ассет: %s",
                    release_version, asset_name,
                )
                return {
                    "version": release_version,
                    "download_url": download_url,
                    "asset_name": asset_name,
                    "no_installer_for_platform": False,
                }

        logger.warning(
            "Установщик %s не найден в релизе v%s",
            expected_name, release_version,
        )
        return {
            "version": release_version,
            "download_url": None,
            "asset_name": None,
            "no_installer_for_platform": True,
        }

    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка при проверке обновлений: %s", exc)
        return None


def download_installer(
    url: str,
    dest_dir: Path,
    progress_cb: Optional[callable] = None,
) -> Path:
    """Скачивает установщик в указанную папку.

    Возвращает путь к скачанному файлу.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_name = url.split("/")[-1]
    dest_path = dest_dir / file_name

    logger.info("Скачиваю установщик: %s -> %s", url, dest_path)

    def hook(block_num: int, block_size: int, total_size: int) -> None:
        if progress_cb is None:
            return
        try:
            downloaded = block_num * block_size
            total = total_size if total_size > 0 else 0
            if total > 0 and downloaded > total:
                downloaded = total
            progress_cb(int(downloaded), int(total))
        except Exception:  # noqa: BLE001
            pass

    urllib.request.urlretrieve(url, dest_path, reporthook=hook)
    logger.info("Установщик скачан: %s (%d байт)", dest_path, dest_path.stat().st_size)
    return dest_path


def run_installer_silent(installer_path: Path, install_dir: Path) -> None:
    """Запускает установщик в тихом режиме с флагами перезапуска и отключения ярлыков.

    Установщик запускается как отдельный процесс, клиент должен завершиться после этого.
    """
    installer_path = Path(installer_path)
    install_dir = Path(install_dir)

    if not installer_path.exists():
        raise FileNotFoundError(f"Установщик не найден: {installer_path}")

    cmd = [
        str(installer_path),
        "--silent",
        "--no-shortcuts",
        "--restart",
    ]

    logger.info(
        "Запускаю установщик: %s с флагами --silent --no-shortcuts --restart",
        installer_path,
    )

    # Запускаем установщик и не ждем его завершения — он сам перезапустит клиент
    subprocess.Popen(
        cmd,
        cwd=str(install_dir),
        close_fds=True,
    )
