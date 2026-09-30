@echo off
echo ============================================================
echo  F.R.I.D.A.Y. — CUDA DLL Fix
echo  Run this if you see: cublas64_12.dll is not found
echo ============================================================
echo.
echo Installing CUDA DLLs via pip...
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12
echo.
echo Done. Try launch.bat again.
pause
