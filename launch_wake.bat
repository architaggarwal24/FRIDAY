@echo off
:: F.R.I.D.A.Y. — Wake-word mode ("Hey FRIDAY")
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python start.py --wake
pause
