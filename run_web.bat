@echo off
rem Original local mode: PN532 on this PC read directly by the web app (SQLite). For Render use the Local NFC Bridge instead.
cd /d "%~dp0"
python -m pip install -r requirements-local.txt
set NFC_MODE=local
python app.py
pause
