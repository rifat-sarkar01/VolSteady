@echo off
cd /d "%~dp0"
call .venv\Scripts\activate
pyinstaller build.spec --noconfirm --quiet
if %errorlevel% neq 0 (
    echo Build failed!
    pause
    exit /b 1
)
echo Build complete. Starting VolSteady...
start "" dist\VolSteady.exe
