#!/usr/bin/env bash
# Obxod Linux startup script
# Usage:
#   ./start.sh           — обычный запуск
#   ./start.sh debug     — с консольными логами
#   sudo ./start.sh      — для TUN-режима (нужен root)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUNTIME="$SCRIPT_DIR/runtime"
PY="$RUNTIME/python/bin/python3"
REQ="$SCRIPT_DIR/requirements.txt"
GET_PIP_URL="https://bootstrap.pypa.io/get-pip.py"

# ── Версия портативного рантайма (аналог PY_VER в start.bat) ─────────
# Актуальные теги: https://github.com/astral-sh/python-build-standalone/releases
# Меняем пару PBS_TAG / PBS_PY_VER — и всё.
PBS_TAG="20241206"
PBS_PY_VER="3.11.11"

DEBUG=0
case "${1:-}" in
  debug|-d|--debug) DEBUG=1 ;;
esac

# ── helpers ──────────────────────────────────────────────────────────
log()  { printf '=== %s ===\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

detect_arch() {
  local m; m="$(uname -m)"
  case "$m" in
    x86_64|amd64)  echo "x86_64"  ;;
    aarch64|arm64) echo "aarch64" ;;
    *) die "Неподдерживаемая архитектура: $m (нужен x86_64 или aarch64)" ;;
  esac
}

check_tools() {
  command -v curl >/dev/null 2>&1 || die "Нужен curl. Установите: sudo apt install curl"
  command -v tar  >/dev/null 2>&1 || die "Нужен tar (обычно уже есть)."
}

# Доустановить/обновить зависимости, если версии в requirements.txt разошлись
# с тем, что уже стоит в рантайме (например, toga 0.5.7 с фиксом зависания ввода).
sync_requirements() {
  if "$PY" - "$REQ" <<'PY'
import importlib.metadata as md
import pathlib
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
pins = [
    line.split("==", 1)[1].split(";")[0].strip()
    for line in text.splitlines()
    if line.strip().lower().startswith("toga==")
]
sys.exit(0 if pins and md.version("toga") == pins[0] else 1)
PY
  then
    return 0
  fi

  log "Обновляю зависимости под requirements.txt"
  "$PY" -m pip install --no-warn-script-location --disable-pip-version-check -r "$REQ" \
    || die "Не удалось обновить зависимости."
}

ensure_runtime() {
  # Уже готово?
  if [[ -x "$PY" ]] && "$PY" -c "import toga, psutil" 2>/dev/null; then
    sync_requirements
    return 0
  fi

  log "Первый запуск: подготовка окружения"
  echo "Это займёт пару минут, нужен доступ в интернет."
  echo

  check_tools

  # Если runtime есть, но битый — сносим и пересобираем (как в start.bat)
  if [[ -d "$RUNTIME" ]]; then
    rm -rf "$RUNTIME"
  fi
  mkdir -p "$RUNTIME"

  local arch tarball url
  arch="$(detect_arch)"
  tarball="cpython-${PBS_PY_VER}+${PBS_TAG}-${arch}-unknown-linux-gnu-install_only.tar.gz"
  url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${tarball}"

  echo "[1/3] Скачиваю портативный Python $PBS_PY_VER ($arch)..."
  if ! curl -fL --retry 3 -o "$RUNTIME/python.tar.gz" "$url"; then
    die "Не удалось скачать Python. Проверьте URL/интернет: $url"
  fi

  echo "[2/3] Распаковываю..."
  tar -xzf "$RUNTIME/python.tar.gz" -C "$RUNTIME"
  rm -f "$RUNTIME/python.tar.gz"

  # На всякий случай — вдруг бинарь называется python3.X
  if [[ ! -x "$PY" ]]; then
    local found
    found="$(find "$RUNTIME/python/bin" -maxdepth 1 -type f -executable -name 'python3*' 2>/dev/null | head -1)"
    [[ -n "$found" ]] || die "После распаковки не нашёл python в $RUNTIME/python/bin"
    PY="$found"
  fi

  # install_only-сборки уже с pip, но подстрахуемся
  if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "[2.5] Устанавливаю pip..."
    curl -fL --retry 3 -o "$RUNTIME/get-pip.py" "$GET_PIP_URL" || die "Не удалось скачать get-pip.py"
    "$PY" "$RUNTIME/get-pip.py" --no-warn-script-location || die "pip не установился."
    rm -f "$RUNTIME/get-pip.py"
  fi

  echo "[3/3] Ставлю зависимости..."
  [[ -f "$REQ" ]] || die "Не найден $REQ"
  "$PY" -m pip install --no-warn-script-location -r "$REQ" \
    || die "Не удалось установить зависимости."

  # Контрольная проверка (как в start.bat)
  "$PY" -c "import toga, psutil; from toga.handlers import WeakrefCallable" \
    || die "Импорт toga/psutil не удался после установки."

  echo
  log "Окружение готово."
  echo
}

# ── системные подсказки для GUI (GTK/WebKit) ─────────────────────────
hint_linux_deps() {
  [[ "$(uname -s)" == "Linux" ]] || return 0
  if ! "$PY" -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null; then
    warn "GTK3 / PyGObject не найдены. Для UI на Linux обычно нужно:"
    warn "  Debian/Ubuntu: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1"
    warn "  Fedora:        sudo dnf install python3-gobject gtk3 webkit2gtk4.1"
  fi
}

# ── main ─────────────────────────────────────────────────────────────
ensure_runtime
hint_linux_deps

export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

if [[ "$DEBUG" == "1" ]]; then
  log "DEBUG: запуск с консолью"
  exec "$PY" "$SCRIPT_DIR/main.py"
else
  mkdir -p "$SCRIPT_DIR/logs"
  exec "$PY" "$SCRIPT_DIR/main.py" \
    >>"$SCRIPT_DIR/logs/startup.log" 2>&1
fi