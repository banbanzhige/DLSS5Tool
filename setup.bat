@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python 3。请先安装 Python 3 并勾选 "Add Python to PATH"，然后重新运行本脚本。
  echo        下载: https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo 正在创建隔离环境 .venv ...
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)
echo 正在安装 requirements.txt 中的依赖...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed
echo.
echo ========================================
echo  依赖安装完成！
echo  1) 双击 run.bat  或  运行  python gui.py
echo  2) 需要 NVIDIA 显卡 + 最新驱动
echo ========================================
pause
exit /b 0

:failed
echo.
echo [错误] 环境创建或依赖安装失败，请检查上方输出。
pause
exit /b 1
