@echo off
chcp 65001 >nul
title Obxod-BOX Setup
setlocal EnableDelayedExpansion

rem ── Аргументы ────────────────────────────────────────────────────
rem  --silent              тихая установка без GUI
rem  --install-dir <path>  целевая папка установки
rem  --no-shortcuts        не создавать ярлыки
rem  --restart             запустить приложение после установки
rem  --keep-files          не удалять распакованные файлы установщика
set "SILENT=0"
set "NO_SHORTCUTS=0"
set "RESTART=0"
set "KEEP_FILES=0"
set "INSTALL_DIR="

:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--silent" set "SILENT=1" & shift & goto :parse_args
if /i "%~1"=="--no-shortcuts" set "NO_SHORTCUTS=1" & shift & goto :parse_args
if /i "%~1"=="--restart" set "RESTART=1" & shift & goto :parse_args
if /i "%~1"=="--keep-files" set "KEEP_FILES=1" & shift & goto :parse_args
if /i "%~1"=="--install-dir" set "INSTALL_DIR=%~2" & shift & shift & goto :parse_args
shift
goto :parse_args
:args_done

rem ── Пути ─────────────────────────────────────────────────────────
rem  ROOT — папка, куда SFX распаковал installer/, runtime/, payload.zip
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

if not defined INSTALL_DIR set "INSTALL_DIR=C:\obxod-box"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

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

set "PAYLOAD=%ROOT%\payload.zip"
if not exist "%PAYLOAD%" (
    echo ERROR: payload.zip not found at %PAYLOAD%
    if "%SILENT%"=="0" pause
    exit /b 1
)

set "OBX_PAYLOAD=%PAYLOAD%"
set "OBX_SILENT=%SILENT%"
set "OBX_ROOT=%ROOT%"
set "OBX_INSTALL_DIR=%INSTALL_DIR%"
set "OBX_NO_SHORTCUTS=%NO_SHORTCUTS%"
set "OBX_RESTART=%RESTART%"
set "OBX_RUNTIME=%ROOT%\runtime"

cd /d "%ROOT%"

rem ── Запуск установщика ───────────────────────────────────────────
rem  payload передаём позиционно; silent/install_dir — только если функция
rem  их принимает (проверяем через inspect, чтобы не падать на несовпадении).
set "BOOT=import sys,os,inspect;r=os.environ['OBX_ROOT'];sys.path[:0]=[r,os.path.join(r,'runtime'),os.path.join(r,'runtime','Lib'),os.path.join(r,'runtime','Lib','site-packages'),os.path.join(r,'runtime','DLLs')];import installer.gui as g;p=os.environ['OBX_PAYLOAD'];params=inspect.signature(g.run_gui_installer).parameters;kw={};kw.update({'silent':os.environ.get('OBX_SILENT')=='1'} if 'silent' in params else {});kw.update({'install_dir':os.environ.get('OBX_INSTALL_DIR')} if 'install_dir' in params else {});kw.update({'no_shortcuts':os.environ.get('OBX_NO_SHORTCUTS')=='1'} if 'no_shortcuts' in params else {});kw.update({'restart':os.environ.get('OBX_RESTART')=='1'} if 'restart' in params else {});kw.update({'runtime_source':os.environ.get('OBX_RUNTIME')} if 'runtime_source' in params else {});sys.exit(g.run_gui_installer(p,**kw) or 0)"

"%PY%" -c "%BOOT%"
set "RC=%ERRORLEVEL%"

if %RC% NEQ 0 (
    echo.
    echo ОШИБКА: установщик завершился с кодом %RC%
    if "%SILENT%"=="0" pause
)

rem ── Очистка распакованных файлов установщика ─────────────────────
rem  install.bat выполняется из %ROOT%, поэтому папку удаляем отдельным
rem  процессом после выхода из текущего. Скрипт обязан сменить CWD:
rem  иначе rmdir не сможет удалить каталог, в котором сам находится.
if "%KEEP_FILES%"=="0" (
    set "CLEANUP_BAT=%TEMP%\obxod_cleanup_%RANDOM%%RANDOM%.bat"
    > "!CLEANUP_BAT!" echo @echo off
    >> "!CLEANUP_BAT!" echo cd /d "%%TEMP%%"
    >> "!CLEANUP_BAT!" echo rem Give the parent cmd.exe time to finish reading install.bat
    >> "!CLEANUP_BAT!" echo timeout /t 2 /nobreak ^>nul
    >> "!CLEANUP_BAT!" echo set /a tries=0
    >> "!CLEANUP_BAT!" echo :wait_loop
    >> "!CLEANUP_BAT!" echo rmdir /s /q "%ROOT%" 2^>nul
    >> "!CLEANUP_BAT!" echo if not exist "%ROOT%" goto :done
    >> "!CLEANUP_BAT!" echo set /a tries+=1
    >> "!CLEANUP_BAT!" echo if %%tries%% geq 30 goto :done
    >> "!CLEANUP_BAT!" echo timeout /t 1 /nobreak ^>nul
    >> "!CLEANUP_BAT!" echo goto :wait_loop
    >> "!CLEANUP_BAT!" echo :done
    >> "!CLEANUP_BAT!" echo del /q /f "%%~f0" 2^>nul
    start "" /b cmd /c "!CLEANUP_BAT!"
)

endlocal & exit /b %RC%
