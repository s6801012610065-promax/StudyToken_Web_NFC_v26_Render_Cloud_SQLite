@echo off
cd /d "%~dp0"
python -m pip install -r requirements-local.txt
python app.py
pause
