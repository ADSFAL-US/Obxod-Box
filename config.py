"""Система конфигурации приложения."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_MODES: tuple[str, ...] = ("tun", "proxy", "both")

DEFAULTS: dict[str, Any] = {
    "mode": "tun",
    "active_provider": None,
    "active_config_id": None,
    "xray_loglevel": "warning",
    "xray_log_rotate_minutes": 10,
    # ── Автопереподключение при смене сети ──
    "network_watch_enabled": True,
    "network_watch_interval": 2,
    "network_auto_reconnect": True,
    "network_restart_on_iface_change": True,
    # ── Пинг конфигов ──
    "ping_url": "https://www.gstatic.com/generate_204",
    "ping_timeout": 5,
    "ping_concurrency": 8,
    # ── роутинг ──
    "routing_set": "RU-auto-routing",
    # ── автообновления ──
    "auto_update_enabled": True,
}


class Config:
    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = path or (Path(__file__).resolve().parent / "cfg" / "app-config.json")
        self._data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            file_data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Не удалось прочитать конфиг %s: %s. Использую дефолты.",
                           self._path, exc)
            file_data = {}

        merged = dict(DEFAULTS)
        for key in DEFAULTS:
            if key in file_data:
                merged[key] = file_data[key]

        self._validate(merged)
        self._data = merged

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: self._data.get(k, DEFAULTS[k]) for k in DEFAULTS}
            self._path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            logger.info("Конфигурация сохранена в %s", self._path)
        except OSError as exc:
            logger.error("Не удалось сохранить конфиг %s: %s", self._path, exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if key == "mode" and value not in VALID_MODES:
            raise ValueError(f"Недопустимый режим: {value!r}. Допустимо: {VALID_MODES}")
        self._data[key] = value

    @property
    def mode(self) -> str:
        return str(self._data.get("mode", DEFAULTS["mode"]))

    @mode.setter
    def mode(self, value: str) -> None:
        self.set("mode", value)

    @property
    def path(self) -> Path:
        return self._path

    def _validate(self, data: dict[str, Any]) -> None:
        mode = data.get("mode")
        if mode is not None and mode not in VALID_MODES:
            logger.warning("В конфиге недопустимый mode=%r. Будет использован дефолт.", mode)
            data["mode"] = DEFAULTS["mode"]
