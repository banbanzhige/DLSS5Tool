@echo off
setlocal

where cl >nul 2>nul
if errorlevel 1 (
  set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
  if not exist "%VSWHERE%" (
    echo [error] Visual Studio Build Tools or vswhere.exe was not found.
    echo Install Visual Studio 2022 Build Tools with Desktop development with C++.
    exit /b 1
  )
  for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find VC\Auxiliary\Build\vcvars64.bat`) do set "VCVARS=%%i"
  if not defined VCVARS (
    echo [error] The Visual C++ x64 build environment was not found.
    exit /b 1
  )
  call "%VCVARS%" >nul
  if errorlevel 1 exit /b 1
)

set "ROOT=%~dp0.."
set "NGX_INCLUDE=%ROOT%\third_party\NVIDIA-DLSS\include"
set "NGX_LIB=%ROOT%\third_party\NVIDIA-DLSS\lib\Windows_x86_64\x64\nvsdk_ngx_s.lib"

if not exist "%NGX_INCLUDE%\nvsdk_ngx.h" (
  echo [error] NVIDIA DLSS SDK headers were not found at:
  echo         %NGX_INCLUDE%
  echo Clone https://github.com/NVIDIA/DLSS.git into third_party\NVIDIA-DLSS
  echo and review the SDK license before building.
  exit /b 1
)
if not exist "%NGX_LIB%" (
  echo [error] NVIDIA NGX import library was not found at:
  echo         %NGX_LIB%
  exit /b 1
)

cl /nologo /std:c++17 /O2 /EHsc /MT /LD /I"%NGX_INCLUDE%" ^
  "%~dp0dlssnr_host_v2.cpp" "%NGX_LIB%" Advapi32.lib User32.lib ^
  /link /OUT:"%ROOT%\dlssnr_host_v2.dll" /PDB:"%ROOT%\dlssnr_host_v2.pdb"
exit /b %errorlevel%
