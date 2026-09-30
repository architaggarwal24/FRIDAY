@echo off
:: F.R.I.D.A.Y. — Push-to-talk mode (default)
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python start.py
pause
