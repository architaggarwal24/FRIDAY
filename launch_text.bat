@echo off
:: F.R.I.D.A.Y. — Text-only mode (no mic/TTS)
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python start.py --text
pause
