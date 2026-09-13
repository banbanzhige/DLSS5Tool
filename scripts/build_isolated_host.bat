@echo off
setlocal EnableExtensions EnableDelayedExpansion
if "%~2"=="" exit /b 2
set "SOURCE=%~f1"
set "OUTPUT=%~f2"
set "ROOT=%~dp0.."
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq tokens=*" %%i in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find VC\Auxiliary\Build\vcvars64.bat`) do set "VCVARS=%%i"
if not defined VCVARS exit /b 1
call "!VCVARS!" >nul
if errorlevel 1 exit /b 1
if not exist "%OUTPUT%" mkdir "%OUTPUT%"
cl /nologo /std:c++17 /O2 /EHsc /MT /LD /I"%ROOT%\third_party\NVIDIA-DLSS\include" /I"%ROOT%\native\host_v2" /Fo"%OUTPUT%\host.obj" ^
 "%SOURCE%" "%ROOT%\third_party\NVIDIA-DLSS\lib\Windows_x86_64\x64\nvsdk_ngx_s.lib" D3D12.lib DXGI.lib Advapi32.lib User32.lib ^
 /link /OUT:"%OUTPUT%\candidate.dll" /PDB:"%OUTPUT%\host.pdb" /IMPLIB:"%OUTPUT%\host.lib"
exit /b %errorlevel%
