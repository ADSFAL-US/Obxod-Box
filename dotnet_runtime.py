"""Проверка и установка .NET Desktop Runtime для WinForms-бэкенда Toga.

Toga на Windows пытается загрузить .NET Core (сейчас 10.0) и, если его нет,
молча откатывается на .NET Framework 4.x. Работать это может, но именно
такая конфигурация даёт странные зависания ввода и отсутствие части фиксов
рантайма. Поэтому при старте проверяем наличие нужной мажорной версии и,
если её нет, ставим официальный Windows Desktop Runtime.

Установка полностью автоматическая, но не критичная: любые сбои логируются,
а приложение продолжает запуск (Toga снова использует .NET Framework).
"""

from __future__ import annotations

import importlib
import logging
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Мажорная версия, которую требует текущая Toga (`toga_winforms/resources/runtime.json`).
DEFAULT_DOTNET_MAJOR = 10
# Официальные aka.ms-редиректы на установщик Windows Desktop Runtime.
DOWNLOAD_URL_TEMPLATE = (
    "https://aka.ms/dotnet/{major}.0/windowsdesktop-runtime-win-{arch}.exe"
)
INSTALL_TIMEOUT = 15 * 60
# Флаг «этот запуск уже перезапускался ради .NET» — защита от цикла.
_RESTARTED_ENV = "OBXOD_DOTNET_RESTARTED"


def _runtime_json_major() -> int | None:
    """Мажорная версия .NET из конфига Toga, если он доступен.

    Ищем путь через find_spec: импортировать toga_winforms здесь нельзя —
    именно импорт загружает CLR и фиксирует .NET Framework/.NET Core.
    """
    try:
        import importlib.util
        import json

        spec = importlib.util.find_spec("toga_winforms")
        if spec is None or not spec.origin:
            return None
        cfg = Path(spec.origin).parent / "resources" / "runtime.json"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        for fw in data.get("runtimeOptions", {}).get("frameworks", []):
            if fw.get("name") == "Microsoft.WindowsDesktop.App":
                return int(str(fw.get("version", "")).split(".")[0])
    except Exception as e:
        logger.debug("dotnet: не удалось прочитать runtime.json: %s", e)
    return None


def required_major() -> int:
    return _runtime_json_major() or DEFAULT_DOTNET_MAJOR


def _arch_tag() -> str:
    machine = (platform.machine() or "").upper()
    if machine in ("ARM64", "AARCH64"):
        return "arm64"
    if machine in ("X86", "I386", "I686"):
        return "x86"
    return "x64"


def _candidate_roots() -> list[Path]:
    """Все места, где может лежать .NET (в порядке приоритета)."""
    roots: list[Path] = []
    for env in ("DOTNET_ROOT_X64", "DOTNET_ROOT", "DOTNET_ROOT(x86)"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value))
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value) / "dotnet")
    home = os.environ.get("USERPROFILE")
    if home:
        roots.append(Path(home) / ".dotnet")

    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _versions_in_root(root: Path, major: int) -> list[str]:
    prefix = f"{major}."
    versions: set[str] = set()
    for name in ("Microsoft.WindowsDesktop.App", "Microsoft.NETCore.App"):
        directory = root / "shared" / name
        try:
            for entry in directory.iterdir():
                if entry.is_dir() and entry.name.startswith(prefix):
                    versions.add(entry.name)
        except OSError:
            continue
    return sorted(versions, reverse=True)


def _registry_versions() -> set[str]:
    versions: set[str] = set()
    if sys.platform != "win32":
        return versions
    try:
        import winreg

        root = r"SOFTWARE\dotnet\Setup\InstalledVersions"
        for arch in ("x64", "arm64", "x86"):
            for fx in ("Microsoft.WindowsDesktop.App", "Microsoft.NETCore.App"):
                try:
                    key = winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, rf"{root}\{arch}\sharedfx\{fx}"
                    )
                except OSError:
                    continue
                with key:
                    index = 0
                    while True:
                        try:
                            name, _value, _kind = winreg.EnumValue(key, index)
                        except OSError:
                            break
                        index += 1
                        versions.add(name.strip("v"))
    except Exception as e:
        logger.debug("dotnet: реестр недоступен: %s", e)
    return versions


def installed_versions(major: int | None = None) -> list[str]:
    """Установленные версии Windows Desktop Runtime (по убыванию)."""
    major = required_major() if major is None else major
    versions: set[str] = set()

    for root in _candidate_roots():
        versions.update(_versions_in_root(root, major))

    for version in _registry_versions():
        if version.startswith(f"{major}."):
            versions.add(version)

    return sorted(versions, reverse=True)


def usable_root(major: int | None = None) -> Path | None:
    """Корень .NET, из которого реально загрузится нужный runtime.

    clr_loader/hostfxr ищет рантайм в DOTNET_ROOT, поэтому установленный
    в «Program Files» .NET может быть не виден при чужом DOTNET_ROOT.
    """
    major = required_major() if major is None else major
    for root in _candidate_roots():
        if _versions_in_root(root, major):
            return root
    return None


_ROOT_ENV_BY_ARCH = {
    "x64": "DOTNET_ROOT_X64",
    "arm64": "DOTNET_ROOT_ARM64",
    "x86": "DOTNET_ROOT(x86)",
}


def prepare_env() -> Path | None:
    """Переключает DOTNET_ROOT на корень, где есть нужный runtime.

    Должно вызываться ДО импорта toga: toga_winforms загружает CLR при
    импорте, и если DOTNET_ROOT указывает на старую пользовательскую
    установку без нужной версии, hostfxr не найдёт рантайм и Toga молча
    откатится на .NET Framework.

    Меняется только окружение текущего процесса — системные настройки
    не затрагиваются.
    """
    if sys.platform != "win32":
        return None

    root = usable_root()
    if root is None:
        logger.info("dotnet: подходящий runtime не найден — Toga использует .NET Framework")
        return None

    root_str = str(root)
    arch_env = _ROOT_ENV_BY_ARCH.get(_arch_tag())
    targets = ["DOTNET_ROOT"]
    if arch_env:
        targets.append(arch_env)

    if all(os.environ.get(name) == root_str for name in targets):
        return root

    for name in targets:
        os.environ[name] = root_str
    logger.info("dotnet: DOTNET_ROOT переключён на %s", root_str)
    return root


def select_runtime() -> bool:
    """Загружает CLR заранее, чтобы Toga выбрала .NET Core.

    Python.NET фиксирует рантайм при первой загрузке: если до импорта
    toga_winforms кто-то сделает `import clr` (а это делают наши модули
    на уровне модуля), pythonnet поднимет .NET Framework по умолчанию и
    Toga уже не сможет переключиться на .NET Core. Поэтому здесь мы
    первыми импортируем toga_winforms — он сам выберет coreclr.

    Возвращает True, если в итоге загружен .NET Core.
    """
    if sys.platform != "win32":
        return False
    if using_dotnet_core():
        return True
    try:
        # Импорт ради побочного эффекта: toga_winforms выбирает CLR.
        importlib.import_module("toga_winforms")
    except Exception as e:
        logger.warning("dotnet: не удалось предварительно загрузить CLR: %s", e)
        return False
    return using_dotnet_core()


def is_installed(major: int | None = None) -> bool:
    """True, если runtime нужной версии реально доступен для загрузки."""
    if sys.platform != "win32":
        return True
    return usable_root(major) is not None


def _notify(progress_cb: Callable[[int, int], None] | None, done: int, total: int) -> None:
    if progress_cb is None:
        return
    try:
        progress_cb(int(done), int(total))
    except Exception as exc:
        logger.debug("_notify: %s", exc)


def _download(progress_cb: Callable[[int, int], None] | None) -> Path:
    major = required_major()
    url = DOWNLOAD_URL_TEMPLATE.format(major=major, arch=_arch_tag())
    logger.info("dotnet: скачиваю Windows Desktop Runtime %s (%s)", major, url)

    fd, tmp_name = tempfile.mkstemp(prefix=f"dotnet-{major}-", suffix=".exe")
    os.close(fd)
    target = Path(tmp_name)

    def hook(block_num: int, block_size: int, total_size: int) -> None:
        _notify(progress_cb, block_num * block_size, total_size)

    urllib.request.urlretrieve(url, target, reporthook=hook)
    logger.info("dotnet: установщик скачан (%d байт)", target.stat().st_size)
    return target


def _run_installer(installer: Path) -> bool:
    """Тихая установка с повышением прав. True — установщик отработал успешно."""
    if sys.platform != "win32":
        return False

    args = (
        f'"{installer}" /install /quiet /norestart'
    )
    try:
        import ctypes

        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        # "runas" — единственный способ получить UAC из неуправляемого процесса.
        # Возвращаемое значение > 32 означает, что процесс запущен.
        handle = shell32.ShellExecuteW(None, "runas", "cmd.exe", f"/c {args}", None, 0)
    except Exception as e:
        logger.warning("dotnet: не удалось запустить установщик: %s", e)
        return False

    if not handle or int(handle) <= 32:
        logger.warning("dotnet: ShellExecuteW вернул %s (UAC отклонён?)", handle)
        return False

    # Ждём завершения по появлению нужной версии в файловой системе: своего
    # PID ShellExecuteW не отдаёт, а гонять внешние утилиты ради этого не нужно.
    major = required_major()
    deadline = time.monotonic() + INSTALL_TIMEOUT
    while time.monotonic() < deadline:
        if installed_versions(major):
            return True
        time.sleep(2.0)

    logger.warning("dotnet: установка не завершилась за %d c", INSTALL_TIMEOUT)
    return False


def _ask_user(major: int) -> bool:
    """Спрашивает пользователя, ставить ли .NET. True — согласие."""
    if sys.platform != "win32":
        return False
    try:
        import clr  # type: ignore

        clr.AddReference("System.Windows.Forms")
        from System.Threading import (  # type: ignore
            ApartmentState,
            Thread as NetThread,
            ThreadStart,
        )
    except Exception as e:
        logger.warning("dotnet: диалог недоступен: %s", e)
        return False

    answer = {"yes": False}
    done = threading.Event()

    def ui() -> None:
        try:
            import System.Windows.Forms as WinForms  # type: ignore

            result = WinForms.MessageBox.Show(
                "Для корректной работы интерфейса нужен "
                f".NET Desktop Runtime {major}.\n\n"
                "Он не установлен. Установить сейчас?\n"
                "Установка займёт пару минут и потребует подтверждения прав "
                "администратора.",
                "Obxod-BOX",
                WinForms.MessageBoxButtons.YesNo,
                WinForms.MessageBoxIcon.Question,
            )
            answer["yes"] = result == WinForms.DialogResult.Yes
        except Exception as e:
            logger.warning("dotnet: диалог не показан: %s", e)
        finally:
            done.set()

    thread = NetThread(ThreadStart(ui))
    thread.SetApartmentState(ApartmentState.STA)
    thread.IsBackground = True
    thread.Start()
    done.wait()
    return answer["yes"]


def confirm_install(major: int | None = None) -> bool:
    """Спрашивает разрешение на установку .NET Desktop Runtime."""
    if sys.platform != "win32" or is_installed(major):
        return False
    major = required_major() if major is None else major
    agreed = _ask_user(major)
    logger.info("dotnet: пользователь %s установку", "разрешил" if agreed else "отклонил")
    return agreed


def ensure_runtime(
    progress_cb: Callable[[int, int], None] | None = None,
    status_cb: Callable[[str], None] | None = None,
) -> bool:
    """Гарантирует наличие нужного .NET Desktop Runtime.

    Возвращает True, если runtime уже был или успешно установился.
    Никогда не бросает исключений: отсутствие .NET не должно мешать запуску.
    """
    if sys.platform != "win32":
        return True

    major = required_major()
    versions = installed_versions(major)
    if versions:
        logger.info("dotnet: Windows Desktop Runtime %s уже установлен (%s)", major, versions[0])
        return True

    logger.info("dotnet: Windows Desktop Runtime %s не найден — устанавливаю", major)

    def status(text: str) -> None:
        logger.info("dotnet: %s", text)
        if status_cb is not None:
            try:
                status_cb(text)
            except Exception as exc:
                logger.debug("status: %s", exc)

    installer: Path | None = None
    try:
        status(f"Устанавливаю .NET Desktop Runtime {major}...")
        installer = _download(progress_cb)

        status(f"Установка .NET Desktop Runtime {major} (может занять пару минут)...")
        if not _run_installer(installer):
            status("Не удалось установить .NET — работаю на .NET Framework")
            return False

        if installed_versions(major):
            status(f".NET Desktop Runtime {major} установлен")
            return True

        status("Установщик завершился, но runtime не найден — работаю на .NET Framework")
        return False
    except Exception as e:
        logger.warning("dotnet: установка не удалась: %s", e)
        status("Не удалось установить .NET — работаю на .NET Framework")
        return False
    finally:
        if installer is not None:
            try:
                installer.unlink()
            except OSError:
                pass


def using_dotnet_core() -> bool:
    """Загружен ли в текущем процессе .NET Core (а не .NET Framework).

    Смотрим только уже импортированный модуль: импорт здесь загрузил бы
    CLR и зафиксировал рантайм раньше времени.
    """
    module = sys.modules.get("toga_winforms")
    if module is None:
        return False
    return bool(getattr(module, "_use_dotnet_core", False))


def restart_required() -> bool:
    """Нужен ли перезапуск, чтобы Toga подхватила нужный .NET.

    Рантайм выбирается при импорте toga_winforms, поэтому сменить
    его в уже запущенном процессе нельзя.
    """
    if sys.platform != "win32":
        return False
    # Защита от цикла: после перезапуска принимаем
    # текущий рантайм как есть, чтобы не зациклиться.
    if os.environ.get(_RESTARTED_ENV) == "1":
        return False
    # Если toga_winforms ещё не импортирован, смысла нет.
    if "toga_winforms" not in sys.modules:
        return False
    return not using_dotnet_core() and is_installed()


def restart_app() -> bool:
    """Перезапускает приложение с новым рантаймом."""
    app_dir = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        args = [sys.executable, *sys.argv[1:]]
    else:
        args = [sys.executable, str(app_dir / "main.py"), *sys.argv[1:]]
    args += ["--wait-pid", str(os.getpid())]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    env = os.environ.copy()
    env[_RESTARTED_ENV] = "1"

    try:
        subprocess.Popen(
            args,
            cwd=str(app_dir),
            close_fds=True,
            env=env,
            creationflags=creationflags,
        )
    except Exception as e:
        logger.warning("dotnet: не удалось перезапустить: %s", e)
        return False

    logger.info("dotnet: перезапуск ради .NET Core")
    os._exit(0)
