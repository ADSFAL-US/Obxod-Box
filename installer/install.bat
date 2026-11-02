@echo off
chcp 65001 >nul
title Obxod-BOX Setup
setlocal EnableDelayedExpansion

set "SILENT=0"
set "NO_SHORTCUTS=0"
set "RESTART=0"
set "INSTALL_DIR="

:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--silent" set "SILENT=1" & shift & goto :parse_args
if /i "%~1"=="--no-shortcuts" set "NO_SHORTCUTS=1" & shift & goto :parse_args
if /i "%~1"=="--restart" set "RESTART=1" & shift & goto :parse_args
if /i "%~1"=="--install-dir" set "INSTALL_DIR=%~2" & shift & shift & goto :parse_args
shift
goto :parse_args
:args_done

rem --- Определяем корень установки ---
if not defined INSTALL_DIR (
    set "ROOT=%~dp0"
) else (
    set "ROOT=%INSTALL_DIR%"
)
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

rem --- Self-delete path (for auto-update scenario) ---
set "INSTALLER_EXE=%~f0"

set "PY=%ROOT%\runtime\python.exe"
if not exist "%PY%" (
    echo ERROR: Python runtime not found at %PY%
    if "%SILENT%"=="0" pause
    exit /b 1
)

set "GUI=%ROOT%\installer\gui.py"
if not exist "%GUI%" (
    echo ERROR: GUI installer not found at %GUI%
    if "%SILENT%"=="0" pause
    exit /b 1
)

rem --- payload ---
set "PAYLOAD=%ROOT%\payload.zip"
if not exist "%PAYLOAD%" (
    echo ERROR: payload.zip not found at %PAYLOAD%
    if "%SILENT%"=="0" pause
    exit /b 1
)
set "OBX_PAYLOAD=%PAYLOAD%"
set "OBX_SILENT=%SILENT%"
set "OBX_NO_SHORTCUTS=%NO_SHORTCUTS%"
set "OBX_RESTART=%RESTART%"
set "OBX_ROOT=%ROOT%"
set "OBX_INSTALLER_EXE=%INSTALLER_EXE%"

cd /d "%ROOT%"

if "%SILENT%"=="1" goto :silent_install

rem --- запуск GUI ---
rem  payload передаём позиционно; silent — только если функция его принимает
rem  (проверяем через inspect, чтобы не падать на несовпадении сигнатуры).
set "BOOT=import sys,os,inspect;r=os.environ['OBX_ROOT'];sys.path[:0]=[r,os.path.join(r,'runtime'),os.path.join(r,'runtime','Lib'),os.path.join(r,'runtime','Lib','site-packages'),os.path.join(r,'runtime','DLLs')];import installer.gui as g;p=os.environ['OBX_PAYLOAD'];s=os.environ.get('OBX_SILENT')=='1';kw={'silent':s} if 'silent' in inspect.signature(g.run_gui_installer).parameters else {};g.run_gui_installer(p,**kw)"

"%PY%" -c "%BOOT%"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

:silent_install
rem --- Тихая установка без GUI ---
rem --- SFX_ROOT — временная папка SFX (где извлечены runtime, installer, payload)
rem --- ROOT — целевая папка установки клиента
set "SFX_ROOT=%~dp0"
if "%SFX_ROOT:~-1%"=="\" set "SFX_ROOT=%SFX_ROOT:~0,-1%"

echo [1/4] Копирую рантайм из SFX в целевую папку...
if not exist "%ROOT%\runtime" mkdir "%ROOT%\runtime"
xcopy /e /i /q "%SFX_ROOT%\runtime" "%ROOT%\runtime\"

echo [2/4] Копирую файлы установки...
"%PY%" -c "import sys,os;r=os.environ['OBX_ROOT'];sys.path[:0]=[r,os.path.join(r,'runtime'),os.path.join(r,'runtime','Lib'),os.path.join(r,'runtime','Lib','site-packages'),os.path.join(r,'runtime','DLLs')];from installer.core import ObxodBoxInstaller, InstallConfig, InstallMode;from pathlib import Path;c=InstallConfig(install_path=Path(r),create_shortcut=False,create_desktop_shortcut=False,create_start_menu_shortcut=False,run_after_install=False,mode=InstallMode.CLI,silent=True,no_shortcuts=(os.environ.get('OBX_NO_SHORTCUTS')=='1'),restart_after_install=(os.environ.get('OBX_RESTART')=='1'));i=ObxodBoxInstaller(c);i.install(Path(os.environ['OBX_PAYLOAD']))"
set "RC=%ERRORLEVEL%"

if "%RC%"=="0" (
    echo [OK] Установка завершена успешно.
    if "%RESTART%"=="1" (
        echo [3/4] Перезапускаю приложение...
        start "" "%ROOT%\start.bat"
    )
) else (
    echo [FAIL] Ошибка установки. Код: %RC%
)

rem --- Самоудаление установщика ---
if exist "%INSTALLER_EXE%" (
    echo [4/4] Удаляю установщик...
    set "DELETE_BAT=%TEMP%\obxod_delete_%RANDOM%.bat"
    echo @echo off > "%DELETE_BAT%"
    echo :wait_loop >> "%DELETE_BAT%"
    echo if exist "%INSTALLER_EXE%" del /q /f "%INSTALLER_EXE%" >> "%DELETE_BAT%"
    echo del "%DELETE_BAT%" >> "%DELETE_BAT%"
    start /b "" "%DELETE_BAT%"
)

endlocal & exit /b %RC%