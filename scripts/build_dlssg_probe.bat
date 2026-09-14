@echo off
setlocal EnableExtensions EnableDelayedExpansion
if "%~1"=="" exit /b 2
set "TASKDIR=%~f1"
set "ROOT=%~dp0.."
if not exist "%TASKDIR%\TASK.md" exit /b 2
where cl >nul 2>nul
if errorlevel 1 (
  set "VSWHERE=C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
  if not exist "!VSWHERE!" exit /b 2
  for /f "usebackq tokens=*" %%i in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find VC\Auxiliary\Build\vcvars64.bat`) do set "VCVARS=%%i"
  if not defined VCVARS exit /b 2
  call "!VCVARS!" >nul
  if errorlevel 1 exit /b 1
)
set "TEMP=%TASKDIR%"
set "TMP=%TASKDIR%"
cl /nologo /std:c++17 /O2 /EHsc /MT /I"%ROOT%\third_party\NVIDIA-DLSS\include" /Fo"%TASKDIR%\dlssg_probe.obj" ^
  "%~dp0dlssg_probe.cpp" "%ROOT%\third_party\NVIDIA-DLSS\lib\Windows_x86_64\x64\nvsdk_ngx_s.lib" ^
  D3D12.lib DXGI.lib Advapi32.lib User32.lib /link /OUT:"%TASKDIR%\dlssg_probe.exe" ^
  /PDB:"%TASKDIR%\dlssg_probe.pdb" /IMPLIB:"%TASKDIR%\dlssg_probe.lib"
exit /b %errorlevel%
