@echo off
cd /d "%~dp0"
if not exist bridge.env (
  echo bridge.env not found. Copy bridge.env.example to bridge.env and edit it.
  pause & exit /b 1
)
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
:loop
python nfc_bridge.py
echo Bridge exited. Restarting in 5 seconds (close this window to stop)...
timeout /t 5 >nul
goto loop
