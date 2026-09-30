@echo off
echo ============================================================
echo  F.R.I.D.A.Y. — Fix aider-chat dependency conflicts
echo ============================================================
echo.
echo Restoring packages that aider downgraded...

pip install "numpy>=2.0" --upgrade
pip install "pillow>=9.2.0,<12.0" --upgrade
pip install "huggingface-hub>=1.5.0,<2.0" --upgrade
pip install "openai>=1.30.0" --upgrade
pip install "pydantic>=2.0" --upgrade
pip install "rich>=13.0" --upgrade
pip install "anyio>=4.0" --upgrade
pip install "aiohttp>=3.13" --upgrade

echo.
echo Done. FRIDAY dependencies restored.
echo Note: aider still works — it uses litellm which doesn't need latest openai.
pause
