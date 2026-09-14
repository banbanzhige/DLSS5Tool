@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Please run setup.bat first.
  pause
  exit /b 1
)
if not exist "runtime\dlssg_video_worker.exe" (
  echo Missing frame generation worker. Run scripts\build_dlssg_video.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B -m dlss5tool.frame_generation_ui
if errorlevel 1 pause
