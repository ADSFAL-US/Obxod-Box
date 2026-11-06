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
    REM Рантайм может быть занят работающим экземпляром приложения:
    REM удаление тогда провалится, и ensure_runtime доустановит поверх.
    rmdir /s /q "%RUNTIME%" 2>nul
    call :ensure_runtime
    if errorlevel 1 exit /b 1
)

REM Версии в requirements.txt могли обновиться (например, toga 0.5.7 с фиксом
REM зависания ввода WinForms). Рантайм при этом уже существует, поэтому
REM ensure_runtime его не тронет — доустанавливаем поверх.
call :sync_requirements
if errorlevel 1 exit /b 1

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
REM  sync_requirements
REM ════════════════════════════════════════════════════════════════
:sync_requirements
REM Быстрый выход, если установленные версии совпадают с requirements.txt.
REM Сравнение в Python: разбирать requirements парсером в bat-файле ненадёжно.
"%PY%" -c "import sys,importlib.metadata as m,pathlib; r=pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'); p=[l.split('==')[1].split(';')[0].strip() for l in r.splitlines() if l.strip().lower().startswith('toga==')]; sys.exit(0 if p and m.version('toga')==p[0] else 1)" "%REQ%" >nul 2>&1
if not errorlevel 1 exit /b 0

echo.
echo === Обновляю зависимости под requirements.txt ===
"%PY%" -m pip install --no-warn-script-location --disable-pip-version-check -r "%REQ%"
if errorlevel 1 (
    echo ОШИБКА: не удалось обновить зависимости.
    pause
    exit /b 1
)
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
REM Распаковываем во временную папку и переносим файлы: Expand-Archive -Force
REM падает, если целевые файлы заняты запущенным python.exe/pythonw.exe.
set "PY_TMP=%RUNTIME%\_py_unpack"
if exist "%PY_TMP%" rmdir /s /q "%PY_TMP%" 2>nul
powershell -NoProfile -Command "Expand-Archive -LiteralPath '%RUNTIME%\python.zip' -DestinationPath '%PY_TMP%' -Force"
if errorlevel 1 (
    echo ОШИБКА: не удалось распаковать Python.
    pause
    exit /b 1
)
del /q "%RUNTIME%\python.zip" 2>nul
xcopy /e /i /y /q "%PY_TMP%\*" "%RUNTIME%\" >nul
rmdir /s /q "%PY_TMP%" 2>nul

REM ._pth должен содержать Lib\site-packages и import site ровно по одному разу
for %%f in ("%RUNTIME%\python*._pth") do (
    findstr /x /c:"Lib\site-packages" "%%~ff" >nul 2>&1 || echo Lib\site-packages>> "%%~ff"
    findstr /x /c:"import site" "%%~ff" >nul 2>&1 || echo import site>> "%%~ff"
)
if not exist "%RUNTIME%\Lib\site-packages" mkdir "%RUNTIME%\Lib\site-packages"

echo [3/4] Устанавливаю pip...
REM get-pip.py не подходит: он запускает "<tmp>/pip install ...", из-за чего
REM sys.argv[0] == "pip" и pip 26.x отказывается себя обновлять
REM ("To modify pip, please run the following command"). Ставим wheel напрямую.
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command ^
        "$ErrorActionPreference='Stop';" ^
        "$j = Invoke-RestMethod 'https://pypi.org/pypi/pip/json';" ^
        "$w = ($j.urls | Where-Object { $_.packagetype -eq 'bdist_wheel' } | Select-Object -First 1).url;" ^
        "if (-not $w) { throw 'pip wheel not found on PyPI' };" ^
        "Invoke-WebRequest -Uri $w -OutFile '%RUNTIME%\pip.zip';" ^
        "Expand-Archive -LiteralPath '%RUNTIME%\pip.zip' -DestinationPath '%RUNTIME%\Lib\site-packages' -Force"
    if errorlevel 1 (
        echo ОШИБКА: pip не установился.
        pause
        exit /b 1
    )
    del /q "%RUNTIME%\pip.zip" 2>nul
) else (
    echo pip уже установлен, пропускаю.
)

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