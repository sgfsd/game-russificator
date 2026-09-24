@echo off
chcp 65001 >nul
rem Запуск русификатора из исходников (окно программы, без консоли).
rem Первый запуск сам создаёт окружение .venv и ставит в него компоненты (нужен интернет).
rem Готовая сборка без Python — Russificator.exe из раздела Releases на GitHub.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" goto run

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Скачайте готовую сборку Russificator.exe ^(раздел Releases^)
    echo или установите Python 3.10+ с https://www.python.org/downloads/
    pause
    exit /b 1
)
echo Первый запуск: устанавливаю компоненты, это займёт несколько минут...
python -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -c requirements-lock.txt -e .
if errorlevel 1 goto fail

:run
start "" ".venv\Scripts\pythonw.exe" "%~dp0Русификатор.pyw" %*
exit /b 0

:fail
echo Не удалось установить компоненты. Проверьте интернет и запустите start.bat ещё раз.
rmdir /s /q .venv 2>nul
pause
exit /b 1
