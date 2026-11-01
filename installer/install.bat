@echo off
chcp 65001 >nul
title Obxod-BOX Setup
setlocal EnableDelayedExpansion

set "SILENT=0"
if /i "%~1"=="--silent" set "SILENT=1"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

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
set "OBX_ROOT=%ROOT%"

cd /d "%ROOT%"

rem --- запуск GUI ---
rem  payload передаём позиционно; silent — только если функция его принимает
rem  (проверяем через inspect, чтобы не падать на несовпадении сигнатуры).
set "BOOT=import sys,os,inspect;r=os.environ['OBX_ROOT'];sys.path[:0]=[r,os.path.join(r,'runtime'),os.path.join(r,'runtime','Lib'),os.path.join(r,'runtime','Lib','site-packages'),os.path.join(r,'runtime','DLLs')];import installer.gui as g;p=os.environ['OBX_PAYLOAD'];s=os.environ.get('OBX_SILENT')=='1';kw={'silent':s} if 'silent' in inspect.signature(g.run_gui_installer).parameters else {};g.run_gui_installer(p,**kw)"

"%PY%" -c "%BOOT%"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%