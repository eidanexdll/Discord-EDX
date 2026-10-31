@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Discord Quest Completer

python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('rich') else 1)" || python -m pip install rich

python main.py
echo.
pause
