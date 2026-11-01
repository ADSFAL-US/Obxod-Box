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
VENV="$RUNTIME/venv"
REQ="$SCRIPT_DIR/requirements.txt"
PYTHON_MIN="3.10"

DEBUG=0
case "${1:-}" in
  debug|-d|--debug) DEBUG=1 ;;
esac

# ── helpers ──────────────────────────────────────────────────────────
log()  { printf '=== %s ===\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

find_python() {
  local candidates=(python3 python3.12 python3.11 python3.10 python)
  local c ver
  for c in "${candidates[@]}"; do
    if command -v "$c" >/dev/null 2>&1; then
      ver="$("$c" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
      if [[ -n "$ver" ]]; then
        # shellcheck disable=SC2072
        if printf '%s\n%s\n' "$PYTHON_MIN" "$ver" | sort -V | head -1 | grep -qx "$PYTHON_MIN"; then
          echo "$c"
          return 0
        fi
      fi
    fi
  done
  return 1
}

ensure_runtime() {
  if [[ -x "$VENV/bin/python" ]] && \
     "$VENV/bin/python" -c "import toga, psutil" 2>/dev/null; then
    return 0
  fi

  log "Первый запуск: подготовка окружения"
  echo "Это займёт пару минут, нужен доступ в интернет."
  echo

  local py
  py="$(find_python)" || die "Нужен Python >= $PYTHON_MIN (python3). Установите: sudo apt install python3 python3-venv python3-pip"

  mkdir -p "$RUNTIME"

  if [[ ! -x "$VENV/bin/python" ]]; then
    echo "[1/2] Создаю venv..."
    "$py" -m venv "$VENV" || die "Не удалось создать venv. Установите python3-venv."
  fi

  # shellcheck disable=SC1091
  source "$VENV/bin/activate"

  echo "[2/2] Ставлю зависимости..."
  python -m pip install --upgrade pip setuptools wheel -q
  if [[ ! -f "$REQ" ]]; then
    die "Не найден $REQ"
  fi
  python -m pip install -r "$REQ" || die "Не удалось установить зависимости (toga, psutil)."

  # Быстрая проверка импорта
  python -c "import toga, psutil" || die "Импорт toga/psutil не удался после установки."

  echo
  log "Окружение готово."
  echo
}

# ── системные подсказки для GUI (GTK/WebKit) ─────────────────────────
hint_linux_deps() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    return 0
  fi
  # Toga на Linux тянет GTK + WebKitGTK. Если чего-то нет — подскажем.
  if ! python -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null; then
    warn "GTK3 / PyGObject не найдены. Для UI на Linux обычно нужно:"
    warn "  Debian/Ubuntu: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1"
    warn "  Fedora:        sudo dnf install python3-gobject gtk3 webkit2gtk4.1"
  fi
}

# ── main ─────────────────────────────────────────────────────────────
ensure_runtime
# shellcheck disable=SC1091
source "$VENV/bin/activate"
hint_linux_deps

export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

if [[ "$DEBUG" == "1" ]]; then
  log "DEBUG: запуск с консолью"
  exec python "$SCRIPT_DIR/main.py"
else
  mkdir -p "$SCRIPT_DIR/logs"
  # Логи пишет само приложение; stdout/stderr в файл на всякий случай.
  exec python "$SCRIPT_DIR/main.py" \
    >>"$SCRIPT_DIR/logs/startup.log" 2>&1
fi
