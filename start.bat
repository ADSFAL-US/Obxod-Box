@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "RUNTIME=%~dp0runtime"
set "PY=%RUNTIME%\python.exe"
set "PYW=%RUNTIME%\pythonw.exe"
set "PY_VER=3.11.9"
set "PY_ZIP_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-embed-amd64.zip"
set "GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py"
set "REQ=%~dp0requirements.txt"

REM ── Разбор аргументов ────────────────────────────────────────────
set "DEBUG=0"
if /i "%~1"=="debug"   set "DEBUG=1"
if /i "%~1"=="-d"      set "DEBUG=1"
if /i "%~1"=="--debug" set "DEBUG=1"

REM ════════════════════════════════════════════════════════════════
REM  Окружение (скачать / восстановить, если сломано)
REM ════════════════════════════════════════════════════════════════
call :ensure_runtime
if errorlevel 1 exit /b 1

REM Проверяем, что критичные зависимости реально импортируются
"%PY%" -c "import toga, psutil; from toga.handlers import WeakrefCallable" 2>nul
if errorlevel 1 (
    echo.
    echo [WARN] Зависимости неполные. Пересобираю окружение...
    rmdir /s /q "%RUNTIME%" 2>nul
    call :ensure_runtime
    if errorlevel 1 exit /b 1
)

REM ════════════════════════════════════════════════════════════════
REM  Запуск
REM ════════════════════════════════════════════════════════════════
if "%DEBUG%"=="1" goto :run_debug
goto :run_silent


:run_debug
echo.
echo === DEBUG: запуск с консолью ===
echo.
"%PY%" "%~dp0main.py"
set "RC=!errorlevel!"
echo.
echo === Приложение завершилось. Код: !RC! ===
pause
exit /b !RC!


:run_silent
if not exist "%~dp0logs" mkdir "%~dp0logs"
REM Логи приложение пишет само (см. main.py). Консоли не создаём.
start "" "%PYW%" "%~dp0main.py"
exit /b 0


REM ════════════════════════════════════════════════════════════════
REM  ensure_runtime
REM ════════════════════════════════════════════════════════════════
:ensure_runtime
if exist "%PY%" (
    if exist "%RUNTIME%\Lib\site-packages\toga" (
        exit /b 0
    )
)

echo.
echo === Первый запуск: подготовка окружения ===
echo Это займёт пару минут, нужен доступ в интернет.
echo.

if not exist "%RUNTIME%" mkdir "%RUNTIME%"

echo [1/4] Скачиваю Python %PY_VER%...
curl -L -o "%RUNTIME%\python.zip" "%PY_ZIP_URL%"
if errorlevel 1 (
    echo ОШИБКА: не удалось скачать Python.
    pause
    exit /b 1
)

echo [2/4] Распаковываю...
powershell -NoProfile -Command "Expand-Archive -LiteralPath '%RUNTIME%\python.zip' -DestinationPath '%RUNTIME%' -Force"
del /q "%RUNTIME%\python.zip"

for %%f in ("%RUNTIME%\python*._pth") do (
    echo Lib\site-packages>> "%%~ff"
    echo import site>> "%%~ff"
)
if not exist "%RUNTIME%\Lib\site-packages" mkdir "%RUNTIME%\Lib\site-packages"

echo [3/4] Устанавливаю pip...
curl -L -o "%RUNTIME%\get-pip.py" "%GET_PIP_URL%"
"%PY%" "%RUNTIME%\get-pip.py" --no-warn-script-location
if errorlevel 1 (
    echo ОШИБКА: pip не установился.
    pause
    exit /b 1
)
del /q "%RUNTIME%\get-pip.py"

echo [4/4] Ставлю зависимости...
if not exist "%REQ%" (
    echo ОШИБКА: не найден %REQ%
    pause
    exit /b 1
)
"%PY%" -m pip install --no-warn-script-location -r "%REQ%"
if errorlevel 1 (
    echo ОШИБКА: не удалось установить зависимости.
    pause
    exit /b 1
)

echo.
echo === Окружение готово. ===
echo.
exit /b 0