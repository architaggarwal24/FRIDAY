@echo off
echo ============================================================
echo  F.R.I.D.A.Y. — Setup
echo ============================================================
cd /d "%~dp0"

:: If a project-local venv already exists, install into that (same one
:: launch.bat/launch_text.bat/launch_wake.bat prefer) rather than whatever
:: `python`/`pip` resolve to globally — installing into one and running
:: from the other is exactly what silently defeats "pip install" (packages
:: show up as present in the venv, FRIDAY still can't see them).
if exist ".venv\Scripts\activate.bat" (
    echo Found .venv — installing into it.
    call ".venv\Scripts\activate.bat"
) else (
    echo No .venv found — installing into the global Python on PATH.
    echo ^(Run `python -m venv .venv` first if you'd rather keep this project-local.^)
)

:: Install Python dependencies
echo.
echo [1/3] Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed. Make sure Python 3.11+ is installed.
    pause
    exit /b 1
)

:: Download Whisper model
echo.
echo [2/3] Whisper model will be downloaded on first run...
echo       (large-v3 = ~3GB, stored in .models/whisper/)

:: Install Electron UI dependencies
echo.
echo [3/3] Installing Electron UI dependencies...
cd ui
call npm install
if errorlevel 1 (
    echo WARNING: npm install failed. UI won't work but voice backend still works.
    echo          Install Node.js from https://nodejs.org/ and run 'npm install' in the ui/ folder.
)
cd ..

echo.
echo ============================================================
echo  Setup complete!
echo.
echo  NEXT STEPS:
echo  1. Copy .env.example to .env
echo  2. Fill in your API keys in .env
echo  3. Run launch.bat to start F.R.I.D.A.Y.
echo ============================================================
pause
