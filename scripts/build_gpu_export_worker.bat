@echo off
setlocal EnableExtensions EnableDelayedExpansion
if "%~1"=="" exit /b 2
set "OUT=%~f1"
set "ROOT=%~dp0.."
if not exist "%OUT%\TASK.md" exit /b 2
set "TEMP=%OUT%"
set "TMP=%OUT%"
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq tokens=*" %%i in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find VC\Auxiliary\Build\vcvars64.bat`) do set "VCVARS=%%i"
if not defined VCVARS exit /b 1
call "!VCVARS!" >nul
if errorlevel 1 exit /b 1
cl /nologo /std:c++17 /O2 /EHsc /MT /DGPU_EXPORT /I"%ROOT%\third_party\NVIDIA-DLSS\include" ^
 /Fo"%OUT%\gpu-export-worker.obj" "%ROOT%\scripts\dlssg_video_worker.cpp" ^
 "%ROOT%\third_party\NVIDIA-DLSS\lib\Windows_x86_64\x64\nvsdk_ngx_s.lib" D3D12.lib DXGI.lib Advapi32.lib User32.lib ^
 /link /OUT:"%OUT%\gpu-export-worker.exe" /PDB:"%OUT%\gpu-export-worker.pdb" /IMPLIB:"%OUT%\gpu-export-worker.lib"
exit /b %errorlevel%
