@echo off
cd /d "%~dp0"
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
python nfc_bridge.py --list-ports
python nfc_bridge.py --check-reader
pause
