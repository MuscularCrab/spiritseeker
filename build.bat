@echo off
REM Build SpiritSeeker.exe - run from the repo root.
REM Requires Python 3.11+ on PATH (or edit PYTHON below).
setlocal
cd /d "%~dp0"

set PYTHON=python
if exist ".venv\Scripts\python.exe" set PYTHON=.venv\Scripts\python.exe

echo === Installing dependencies ===
%PYTHON% -m pip install --upgrade pip >nul
%PYTHON% -m pip install -r requirements.txt pyinstaller || goto :error

echo === Building SpiritSeeker.exe ===
%PYTHON% -m PyInstaller --noconfirm --clean ^
    --onefile --windowed ^
    --name SpiritSeeker ^
    --icon assets\icon.ico ^
    --add-data "assets;assets" ^
    --collect-all imageio_ffmpeg ^
    run.py || goto :error

echo.
echo Build complete: dist\SpiritSeeker.exe
exit /b 0

:error
echo Build FAILED.
exit /b 1
