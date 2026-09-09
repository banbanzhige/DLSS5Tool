@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq tokens=*" %%i in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find VC\Auxiliary\Build\vcvars64.bat`) do set "VCVARS=%%i"
if not defined VCVARS exit /b 1
call "!VCVARS!" >nul
if errorlevel 1 exit /b 1
set "OUTPUT=%~dp0..\output\tile-fix-build"
if not exist "%OUTPUT%" mkdir "%OUTPUT%"
cl /nologo /std:c++17 /O2 /EHsc /MT "%~dp0native_tile_blend.cpp" /Fo"%OUTPUT%\native_tile_blend.obj" /Fe"%OUTPUT%\native_tile_blend.exe"
if errorlevel 1 exit /b 1
"%OUTPUT%\native_tile_blend.exe"
exit /b %errorlevel%
