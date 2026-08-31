@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" gui.py
  exit /b %errorlevel%
)
python gui.py
