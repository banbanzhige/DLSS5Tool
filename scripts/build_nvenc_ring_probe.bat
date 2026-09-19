@echo off
setlocal EnableExtensions EnableDelayedExpansion
if "%~1"=="" exit /b 2
set "OUT=%~f1"
set "ROOT=%~dp0.."
if not exist "%OUT%\TASK.md" exit /b 2
set "HEADER=%ROOT%\tmp\encode-acceptance-20260916"
if not exist "%HEADER%\nvEncodeAPI.h" exit /b 2
set "TEMP=%OUT%"
set "TMP=%OUT%"
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq tokens=*" %%i in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find VC\Auxiliary\Build\vcvars64.bat`) do set "VCVARS=%%i"
if not defined VCVARS exit /b 1
call "!VCVARS!" >nul
if errorlevel 1 exit /b 1
if not exist "%OUT%\native-encoder" mkdir "%OUT%\native-encoder"
cl /nologo /std:c++17 /O2 /EHsc /MT /LD /I"%HEADER%" /Fo"%OUT%\native-encoder\ring.obj" ^
 "%ROOT%\scripts\gpu_nvenc_ring.cpp" /link /OUT:"%OUT%\native-encoder\ring.dll" ^
 /PDB:"%OUT%\native-encoder\ring.pdb" /IMPLIB:"%OUT%\native-encoder\ring.lib"
exit /b %errorlevel%
