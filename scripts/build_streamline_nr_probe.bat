@echo off
setlocal EnableExtensions
if "%~2"=="" exit /b 2
if not exist "%~1\sl.h" exit /b 2
if not exist "%~2" exit /b 2
for /f "usebackq delims=" %%i in (`"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find VC\Auxiliary\Build\vcvars64.bat`) do set "PROBE_VCVARS=%%i"
if not defined PROBE_VCVARS exit /b 2
call "%PROBE_VCVARS%" >nul
if errorlevel 1 exit /b 1
cl /nologo /std:c++20 /O2 /EHsc /MD /I"%~f1" /Fo"%~f2\streamline_nr_probe.obj" /Fe"%~f2\streamline_nr_probe.exe" "%~dp0streamline_nr_probe.cpp" /link d3d12.lib dxgi.lib wintrust.lib crypt32.lib
exit /b %errorlevel%
