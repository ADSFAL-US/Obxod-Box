"""
Core installer logic for Obxod-BOX.
Handles installation, updates, shortcut creation, and uninstallation.
"""
import os
import sys
import shutil
import subprocess
import json
import zipfile
import tempfile
import logging
from pathlib import Path
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass
from enum import Enum

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class InstallMode(Enum):
    GUI = "gui"
    CLI = "cli"


@dataclass
class InstallConfig:
    """Configuration for installation."""
    install_path: Path
    create_shortcut: bool = True
    create_desktop_shortcut: bool = True
    create_start_menu_shortcut: bool = True
    run_after_install: bool = False
    mode: InstallMode = InstallMode.GUI
    silent: bool = False
    runtime_source: Optional[Path] = None


class InstallerError(Exception):
    """Custom exception for installer errors."""
    pass


class ObxodBoxInstaller:
    """Main installer class for Obxod-BOX."""
    
    APP_NAME = "Obxod-BOX"
    APP_ID = "org.obxod.box"
    EXECUTABLE_NAME = "main.py"  # Python entry point
    UNINSTALLER_NAME = "uninstall.bat"

    # Каталоги, которые не бэкапим: runtime — десятки тысяч мелких файлов,
    # bin/geo скачиваются заново, logs — мусор. Бэкап нужен только чтобы
    # откатить исходники при неудачном обновлении.
    BACKUP_SKIP = ("runtime", "bin", "geo", "logs", "__pycache__")

    # Пользовательские данные, которые нельзя удалять при откате, даже если
    # в бэкапе их не было.
    ROLLBACK_KEEP = BACKUP_SKIP + ("cfg",)

    CONFIG_RELATIVE_PATH = Path("cfg") / "app-config.json"
    
    def __init__(self, config: InstallConfig, progress_callback: Optional[Callable[[int, str], None]] = None):
        self.config = config
        self.progress_callback = progress_callback
        self.install_path = config.install_path.resolve()
        self.temp_dir: Optional[Path] = None
        self.payload_path: Optional[Path] = None
        self.last_error: str = ""
        
    def _report_progress(self, percent: int, message: str):
        """Report progress to callback if available."""
        logger.info(f"[{percent}%] {message}")
        if self.progress_callback:
            self.progress_callback(percent, message)
    
    def _run_command(self, cmd: list, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run a command and return result."""
        logger.debug(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise InstallerError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
        return result
    
    def extract_payload(self, payload_path: Path) -> Path:
        """Extract the payload archive to a temporary directory.
        If payload_path is a directory (already extracted by SFX), use it directly.
        """
        self._report_progress(10, "Preparing payload...")
        payload_path = Path(payload_path)
        self.payload_path = payload_path
    
        if payload_path.is_dir():
            # Payload already extracted by SFX module
            self.temp_dir = payload_path
            logger.info(f"Using already extracted payload: {self.temp_dir}")
        else:
            # Create temp directory for extraction
            self.temp_dir = Path(tempfile.mkdtemp(prefix="obxod-box-install-"))
            logger.info(f"Extracting to: {self.temp_dir}")
        
            # Extract using zipfile (no external dependencies needed)
            try:
                with zipfile.ZipFile(payload_path, 'r') as zf:
                    zf.extractall(self.temp_dir)
            except Exception as e:
                raise InstallerError(f"Failed to extract payload: {e}")
    
        self._report_progress(30, "Payload ready")
        return self.temp_dir
    
    def check_existing_installation(self) -> bool:
        """Check if Obxod-BOX is already installed."""
        exe_path = self.install_path / self.EXECUTABLE_NAME
        return exe_path.exists()
    
    def backup_existing_installation(self) -> Optional[Path]:
        """Backup existing installation before update.

        Копируем только исходники: runtime/ (десятки тысяч файлов) и
        скачиваемые bin/geo/logs в бэкап не попадают — иначе обновление
        висит минутами на копировании.
        """
        if not self.install_path.exists():
            return None

        self._report_progress(35, "Backing up existing installation...")
        backup_dir = self.install_path.parent / f"{self.install_path.name}.backup"

        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)

        def skip_heavy(_dir, names):
            return [n for n in names if n in self.BACKUP_SKIP]

        shutil.copytree(self.install_path, backup_dir, ignore=skip_heavy)
        logger.info(f"Backed up to: {backup_dir}")
        return backup_dir

    def restore_backup(self, backup_dir: Path) -> bool:
        """Restore from backup on failure.

        Копируем по файлам, а не одним copytree: часть файлов может быть
        занята (DLL работающего клиента), и тогда нужно восстановить всё
        остальное, а не падать на первом же отказе.

        Возвращает True, если удалось восстановить все файлы.
        """
        self._report_progress(90, "Restoring backup...")
        if not backup_dir.exists():
            logger.error("Backup directory is missing: %s", backup_dir)
            return False

        try:
            # Сначала убираем то, чего до обновления не было: иначе новые
            # модули из неудачного пакета остались бы рядом со старыми и
            # клиент снова падал бы при импорте.
            self._remove_new_payload_entries(backup_dir)

            # Не удаляем install_path целиком: в бэкапе нет runtime/, и после
            # отката приложение осталось бы без интерпретатора.
            failed = self._copy_tree_tolerant(backup_dir, self.install_path)
        except OSError as exc:
            logger.exception("Failed to restore backup: %s", exc)
            return False

        if failed:
            logger.error("Rollback restored most files, %d could not be copied: %s",
                         len(failed), ", ".join(failed[:5]))
            return False
        logger.info("Backup restored")
        return True

    def _copy_tree_tolerant(self, src: Path, dst: Path) -> list:
        """Копирует дерево, пропуская файлы, которые не удалось записать."""
        failed = []
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in self.BACKUP_SKIP]
            rel = Path(root).relative_to(src)
            target_dir = dst / rel
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("Cannot create %s: %s", target_dir, exc)
                failed.append(str(target_dir))
                continue
            for name in files:
                source = Path(root) / name
                target = target_dir / name
                try:
                    shutil.copy2(source, target)
                except OSError as exc:
                    logger.warning("Cannot restore %s: %s", target, exc)
                    failed.append(str(target))
        return failed

    def _remove_new_payload_entries(self, backup_dir: Path) -> None:
        """Удаляет из install_path элементы, которых нет в бэкапе."""
        if not self.temp_dir or not self.temp_dir.exists():
            return
        try:
            entries = list(self.temp_dir.iterdir())
        except OSError as exc:
            logger.warning("Cannot list payload for rollback cleanup: %s", exc)
            return

        for entry in entries:
            if entry.name in self.ROLLBACK_KEEP:
                continue
            backup_entry = backup_dir / entry.name
            target = self.install_path / entry.name
            if not backup_entry.exists():
                self._remove_path(target, "added by failed update")
                continue
            if entry.is_dir() and backup_entry.is_dir():
                self._remove_extra_children(entry, backup_entry, target)

    def _remove_extra_children(self, src_dir: Path, backup_dir: Path, target_dir: Path) -> None:
        """Удаляет из target_dir файлы, которых не было в бэкапе."""
        if not target_dir.is_dir():
            return
        try:
            entries = list(src_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name in self.ROLLBACK_KEEP:
                continue
            backup_entry = backup_dir / entry.name
            target = target_dir / entry.name
            if not backup_entry.exists():
                self._remove_path(target, "added by failed update")
                continue
            if entry.is_dir():
                self._remove_extra_children(entry, backup_entry, target)

    def _clean_partial_install(self) -> None:
        """Удаляет файлы недописанной первой установки.

        Накатывать нечего (бэкапа нет), но оставлять полуустановку нельзя:
        повторная попытка смешала бы старые и новые модули.
        """
        if not self.temp_dir or not self.temp_dir.exists():
            return
        self._report_progress(92, "Cleaning partial installation...")
        try:
            entries = list(self.temp_dir.iterdir())
        except OSError as exc:
            logger.warning("Cannot list payload for cleanup: %s", exc)
            return
        for entry in entries:
            if entry.name in self.ROLLBACK_KEEP:
                continue
            self._remove_path(self.install_path / entry.name, "from failed fresh install")

    def _remove_path(self, target: Path, reason: str) -> None:
        if not target.exists():
            return
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            logger.info("Removed %s: %s", reason, target)
        except OSError as exc:
            logger.warning("Cannot remove %s: %s", target, exc)

    def disable_auto_update(self) -> bool:
        """Выключает автообновление в конфиге установленного клиента.

        Нужно после отката: иначе клиент при следующем запуске снова скачает
        тот же установщик и упадёт на нём же.
        """
        config_path = self.install_path / self.CONFIG_RELATIVE_PATH
        try:
            data: Dict[str, Any] = {}
            if config_path.exists():
                try:
                    loaded = json.loads(config_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Cannot read client config %s: %s", config_path, exc)

            data["auto_update_enabled"] = False
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            logger.info("Auto-update disabled in %s", config_path)
            return True
        except OSError as exc:
            logger.error("Failed to disable auto-update: %s", exc)
            return False
    
    def _stop_running_app(self, timeout: float = 15.0) -> None:
        """Завершает процессы, запущенные из папки установки.

        Без этого обновление падает с Access denied: pythonw.exe держит
        python311.dll, а xray.exe — wintun.dll.
        """
        try:
            import psutil
        except ImportError:
            logger.warning("psutil недоступен, не могу остановить клиент")
            return

        root = str(self.install_path).lower().rstrip("\\") + "\\"
        victims = []
        for proc in psutil.process_iter(["pid", "exe"]):
            try:
                exe = (proc.info.get("exe") or "").lower()
            except (psutil.Error, OSError):
                continue
            if exe.startswith(root):
                victims.append(proc)
        if not victims:
            return

        logger.info("Останавливаю запущенный клиент: %s",
                    ", ".join(f"{p.pid}" for p in victims))
        for proc in victims:
            try:
                proc.terminate()
            except (psutil.Error, OSError):
                pass

        _, alive = psutil.wait_procs(victims, timeout=timeout)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.Error, OSError):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=5.0)

    def install_files(self):
        """Copy extracted files to installation directory."""
        self._report_progress(40, "Installing files...")

        # Клиент мог остаться запущенным (автообновление) — иначе копирование
        # упадёт на занятых DLL.
        self._stop_running_app()

        # Create install directory
        self.install_path.mkdir(parents=True, exist_ok=True)
        
        # Copy all files from temp to install path
        for item in self.temp_dir.iterdir():
            dest = self.install_path / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        # The payload only carries application sources; the Python runtime ships
        # alongside the installer and must be copied into the installation too.
        if self.config.runtime_source and self.config.runtime_source.is_dir():
            runtime_dest = self.install_path / "runtime"
            if runtime_dest.exists():
                shutil.rmtree(runtime_dest, ignore_errors=True)
            # Если удалить не удалось (файлы всё ещё заняты) — копируем поверх,
            # иначе установка падала бы на Access denied.
            shutil.copytree(
                self.config.runtime_source, runtime_dest, dirs_exist_ok=True
            )
            logger.info(f"Runtime copied from {self.config.runtime_source}")
        
        self._report_progress(60, "Files installed")
    
    def create_shortcuts(self):
        """Create desktop and start menu shortcuts."""
        if not self.config.create_shortcut:
            return
            
        self._report_progress(70, "Creating shortcuts...")
        
        start_bat = self.install_path / "start.bat"
        if not start_bat.exists():
            logger.warning(f"start.bat not found at {start_bat}, skipping shortcuts")
            return
        
        # Create shortcuts using PowerShell
        self._create_windows_shortcut(
            self.install_path,
            "Obxod-BOX",
            self.config.create_desktop_shortcut,
            self.config.create_start_menu_shortcut
        )
        
        self._report_progress(80, "Shortcuts created")
    
    def _create_windows_shortcut(self, work_dir: Path, name: str, 
                                  desktop: bool = True, start_menu: bool = True):
        """Create Windows shortcuts using PowerShell.
        
        Creates a shortcut that runs start.bat via cmd.exe.
        """
        start_bat = work_dir / "start.bat"
        if not start_bat.exists():
            logger.warning(f"start.bat not found at {start_bat}, skipping shortcuts")
            return
        
        # Use cmd.exe /c to run the batch file properly
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        
        ps_script = f"""
$WshShell = New-Object -ComObject WScript.Shell
$targetPath = "{comspec}"
$arguments = '/c ""{start_bat}""'
$workDir = "{work_dir}"
$name = "{name}"
"""
        
        if desktop:
            ps_script += f"""
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcut = $WshShell.CreateShortcut("$desktop\\$name.lnk")
$shortcut.TargetPath = $targetPath
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $workDir
$shortcut.Description = "Obxod-BOX VPN Client"
$shortcut.Save()
"""
        
        if start_menu:
            ps_script += f"""
$startMenu = [Environment]::GetFolderPath('Programs')
$appFolder = "$startMenu\\$name"
if (-not (Test-Path $appFolder)) {{ New-Item -ItemType Directory -Path $appFolder | Out-Null }}
$shortcut = $WshShell.CreateShortcut("$appFolder\\$name.lnk")
$shortcut.TargetPath = $targetPath
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $workDir
$shortcut.Description = "Obxod-BOX VPN Client"
$shortcut.Save()
"""
        
        try:
            self._run_command(["powershell", "-NoProfile", "-Command", ps_script])
        except InstallerError as e:
            logger.warning(f"Failed to create shortcuts: {e}")
    
    def create_uninstaller(self):
        """Create uninstaller executable."""
        self._report_progress(85, "Creating uninstaller...")
    
        install_path_str = str(self.install_path)
        app_name = self.APP_NAME
    
        # Create uninstaller that uses a separate PowerShell script file
        # This avoids quoting issues in batch files
        uninstaller_script = (
            "@echo off\n"
            "chcp 65001 >nul\n"
            f"echo Uninstalling {app_name}...\n"
            "if \"%~1\"==\"_do_uninstall\" goto :_do_uninstall\n"
            "set \"INSTALL_PATH=%~dp0\"\n"
            "set \"INSTALL_PATH=%INSTALL_PATH:~0,-1%\"\n"
            "set \"TEMP_UNINSTALLER=%TEMP%\\%~n0_%RANDOM%.bat\"\n"
            "set \"PS_SCRIPT=%TEMP%\\%~n0_uninstall_%RANDOM%.ps1\"\n"
            "copy /y \"%~f0\" \"%TEMP_UNINSTALLER%\" >nul\n"
            "start /b \"\" cmd /c \"%TEMP_UNINSTALLER%\" _do_uninstall\n"
            "exit /b\n"
            "\n"
            ": _do_uninstall\n"
            "cd /d C:\\\n"
            f'cmd /c rmdir /s /q "{install_path_str}"\n'
            "echo Removing shortcuts...\n"
            f'echo $desktop = [Environment]::GetFolderPath(\'Desktop\') > "%PS_SCRIPT%"\n'
            f'echo $lnk = "$desktop\\{app_name}.lnk" >> "%PS_SCRIPT%"\n'
            f'echo if (Test-Path $lnk) {{ Remove-Item $lnk -Force; Write-Host "Removed: $lnk" }} >> "%PS_SCRIPT%"\n'
            f'echo $startMenu = [Environment]::GetFolderPath(\'Programs\') >> "%PS_SCRIPT%"\n'
            f'echo $appFolder = "$startMenu\\{app_name}" >> "%PS_SCRIPT%"\n'
            f'echo if (Test-Path $appFolder) {{ Remove-Item $appFolder -Recurse -Force; Write-Host "Removed: $appFolder" }} >> "%PS_SCRIPT%"\n'
            'powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%"\n'
            "del \"%PS_SCRIPT%\" 2>nul\n"
            "echo Uninstall complete.\n"
            "del \"%TEMP_UNINSTALLER%\" 2>nul\n"
            "pause\n"
        )
    
        uninstaller_path = self.install_path / self.UNINSTALLER_NAME
        uninstaller_path.write_text(uninstaller_script, encoding='utf-8')
    
        # Also create a proper uninstall entry in Windows
        self._register_uninstaller()
    
    def _register_uninstaller(self):
        """Register uninstaller in Windows Add/Remove Programs."""
        try:
            import winreg
            uninstall_key = winreg.CreateKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{self.APP_ID}"
            )
            winreg.SetValueEx(uninstall_key, "DisplayName", 0, winreg.REG_SZ, self.APP_NAME)
            winreg.SetValueEx(uninstall_key, "UninstallString", 0, winreg.REG_SZ, 
                            f'"{self.install_path / self.UNINSTALLER_NAME}"')
            winreg.SetValueEx(uninstall_key, "DisplayIcon", 0, winreg.REG_SZ, 
                            str(self.install_path / self.EXECUTABLE_NAME))
            winreg.SetValueEx(uninstall_key, "Publisher", 0, winreg.REG_SZ, "Obxod-BOX Team")
            winreg.SetValueEx(uninstall_key, "DisplayVersion", 0, winreg.REG_SZ, "1.0.0")
            winreg.SetValueEx(uninstall_key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(uninstall_key, "NoRepair", 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(uninstall_key)
        except Exception as e:
            logger.warning(f"Failed to register uninstaller: {e}")
    
    def verify_installation(self) -> bool:
        """Verify installation is complete and working."""
        self._report_progress(90, "Verifying installation...")
        
        # Check for main.py (Python entry point)
        exe_path = self.install_path / "main.py"
        if not exe_path.exists():
            logger.error(f"Main entry point not found in {self.install_path}")
            return False
        
        # Check for Python runtime
        pythonw_exe = self.install_path / "runtime" / "pythonw.exe"
        if not pythonw_exe.exists():
            logger.warning(f"pythonw.exe not found in runtime")
        
        # Check for start.bat
        if not (self.install_path / "start.bat").exists():
            logger.warning("start.bat not found in installation directory")
        
        # Check critical files
        critical_files = [
            'main.py',
            'main_window.py',
            'config.py',
            'xray_manager.py',
        ]
        
        for f in critical_files:
            if not (self.install_path / f).exists():
                logger.warning(f"Critical file missing: {f}")
        
        # Check assets
        assets_dir = self.install_path / 'assets'
        if not assets_dir.exists():
            logger.warning("Assets directory missing")
        
        # Check bin directory
        bin_dir = self.install_path / 'bin'
        if not bin_dir.exists():
            logger.warning("Bin directory missing")
        else:
            for f in ['xray.exe', 'wintun.dll']:
                if not (bin_dir / f).exists():
                    logger.warning(f"Binary missing: {f}")
        
        # Check runtime
        runtime_dir = self.install_path / 'runtime'
        if not runtime_dir.exists():
            logger.warning("Runtime directory missing")
        
        self._report_progress(95, "Installation verified")
        return True
    
    def _launch_app(self) -> None:
        """Запускает клиент после установки/отката."""
        start_bat = self.install_path / "start.bat"
        if not start_bat.exists():
            logger.warning(f"start.bat not found at {start_bat}, cannot launch application")
            return
        try:
            subprocess.Popen([str(start_bat)], cwd=str(self.install_path))
            logger.info("Application launched")
        except Exception as e:
            logger.warning(f"Failed to launch application: {e}")

    def run_post_install(self):
        """Run post-installation tasks."""
        self._report_progress(98, "Finalizing...")

        if self.config.run_after_install:
            self._launch_app()

        self._report_progress(100, "Installation complete!")
    
    def cleanup(self):
        """Clean up temporary files."""
        if self.temp_dir and self.temp_dir.exists():
            try:
                shutil.rmtree(self.temp_dir)
                logger.info(f"Cleaned up temp directory: {self.temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp directory: {e}")
    
    def _rollback(self, backup_dir: Path) -> bool:
        """Откатывает обновление и глушит автообновление клиента."""
        restored = self.restore_backup(backup_dir)
        if restored:
            self._report_progress(95, "Previous version restored")
        else:
            self._report_progress(95, "Rollback finished with errors")

        # Откат не должен оставить клиент в режиме, когда он снова тянет
        # сломанное обновление: конфиг выключаем независимо от результата.
        if not self.disable_auto_update():
            self._report_progress(96, "Failed to disable auto-update")
        return restored

    def install(self, payload_path: Path) -> bool:
        """Main installation routine."""
        backup_dir = None
        self.last_error = ""
        install_touched = False
        try:
            self._report_progress(5, "Starting installation...")

            # Extract payload
            self.extract_payload(payload_path)

            # Check for existing installation
            is_update = self.check_existing_installation()
            if is_update:
                self._report_progress(20, "Existing installation found - updating...")
                backup_dir = self.backup_existing_installation()
                if backup_dir is None:
                    raise InstallerError("Failed to create backup, update aborted")
            else:
                self._report_progress(20, "Fresh installation...")

            # Install files
            install_touched = True
            self.install_files()
            
            # Create shortcuts
            self.create_shortcuts()
            
            # Create uninstaller
            self.create_uninstaller()
            
            # Verify
            if not self.verify_installation():
                raise InstallerError("Installation verification failed")
            
            # Post-install
            self.run_post_install()

            if backup_dir is not None:
                shutil.rmtree(backup_dir, ignore_errors=True)
            
            logger.info(f"Installation {'updated' if is_update else 'completed'} successfully!")
            return True
            
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Installation failed: {e}", exc_info=True)
            if not install_touched:
                # Файлы не тронуты — откатывать нечего и гасить автообновление
                # не нужно, установка упала до изменений.
                return False
            if backup_dir and backup_dir.exists():
                if not self._rollback(backup_dir):
                    self.last_error += (
                        f"\nRollback incomplete, backup kept at: {backup_dir}"
                    )
                # Клиент был остановлен перед копированием файлов — после
                # отката его нужно поднять обратно, иначе пользователь
                # останется без приложения после неудачного автообновления.
                if self.config.run_after_install:
                    self._launch_app()
            elif (self.install_path / self.CONFIG_RELATIVE_PATH).exists():
                # Свежая установка без бэкапа: клиента раньше не было, но
                # конфиг остался — гасим автообновление, чтобы он не тянул
                # сломанный пакет бесконечно.
                self.disable_auto_update()
            else:
                # Первая установка упала: убираем недописанные файлы, чтобы
                # повторная попытка не смешивала их с новыми.
                self._clean_partial_install()
            return False
        finally:
            self.cleanup()


def create_installer_config(
    install_path: str,
    create_shortcut: bool = True,
    create_desktop_shortcut: bool = True,
    create_start_menu_shortcut: bool = True,
    run_after_install: bool = False,
    mode: InstallMode = InstallMode.GUI,
    silent: bool = False
) -> InstallConfig:
    """Factory function to create install config."""
    return InstallConfig(
        install_path=Path(install_path),
        create_shortcut=create_shortcut,
        create_desktop_shortcut=create_desktop_shortcut,
        create_start_menu_shortcut=create_start_menu_shortcut,
        run_after_install=run_after_install,
        mode=mode,
        silent=silent
    )
