@echo off
setlocal EnableExtensions
set "ROOT=%~dp0.."
set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
where cl >nul 2>nul
if errorlevel 1 (
  if not exist "%VCVARS%" (
    echo [error] Run in an x64 Native Tools command prompt for Visual Studio.
    exit /b 1
  )
  call "%VCVARS%" >nul
  if errorlevel 1 exit /b 1
)
set "SDK=%ROOT%\third_party\FidelityFX-1.1.4\ffx-api\include"
if not exist "%SDK%\ffx_api\ffx_upscale.h" (
  echo [error] Obtain the public FSR API headers from FidelityFX-SDK v1.1.4.
  exit /b 1
)
if not exist "%ROOT%\build\amd_probe" mkdir "%ROOT%\build\amd_probe"
cl /nologo /std:c++17 /utf-8 /O2 /EHsc /MT /W4 /I"%SDK%" ^
  "%~dp0amd_probe.cpp" /Fo"%ROOT%\build\amd_probe\amd_probe.obj" ^
  /link /OUT:"%ROOT%\build\amd_probe\amd_probe.exe" d3d12.lib dxgi.lib d3dcompiler.lib user32.lib
exit /b %errorlevel%
