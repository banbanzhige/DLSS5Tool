@echo off
setlocal
cd /d "%~dp0"
rem Development runs use current worker source and the existing CUDA environment.
if not defined DLSS5TOOL_GUIDANCE_PYTHON if exist "tmp\guidance-cuda-env\Scripts\python.exe" set "DLSS5TOOL_GUIDANCE_PYTHON=%CD%\tmp\guidance-cuda-env\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" gui.py
  exit /b %errorlevel%
)
python gui.py
