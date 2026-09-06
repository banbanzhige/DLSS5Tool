@echo off
setlocal EnableExtensions EnableDelayedExpansion

where cl >nul 2>nul
if errorlevel 1 (
  set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
  if not exist "!VSWHERE!" set "VSWHERE=C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
  if not exist "!VSWHERE!" (
    echo [error] Visual Studio 2022 Build Tools were not found.
    exit /b 1
  )
  for /f "usebackq tokens=*" %%i in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find VC\Auxiliary\Build\vcvars64.bat`) do set "VCVARS=%%i"
  if not defined VCVARS (
    echo [error] The Visual C++ x64 build environment was not found.
    exit /b 1
  )
  call "!VCVARS!" >nul
  if errorlevel 1 exit /b 1
)

set "ROOT=%~dp0.."
if not defined NV_RTX_VIDEO_SDK set "NV_RTX_VIDEO_SDK=%ROOT%\third_party\RTX_Video_SDK"
set "RTX_INCLUDE=%NV_RTX_VIDEO_SDK%\include"
set "RTX_LIB=%NV_RTX_VIDEO_SDK%\lib\Windows\x64\nvsdk_ngx_s.lib"

if not exist "%RTX_INCLUDE%\nvsdk_ngx_helpers_vsr.h" (
  echo [error] RTX Video SDK 1.1 headers were not found.
  echo Set NV_RTX_VIDEO_SDK to the extracted SDK root, then run this script again.
  exit /b 1
)
if not exist "%RTX_LIB%" (
  echo [error] NVIDIA NGX import library was not found at:
  echo         %RTX_LIB%
  exit /b 1
)

cl /nologo /std:c++17 /O2 /EHsc /MT /LD /I"%RTX_INCLUDE%" ^
  "%~dp0vsr_host.cpp" "%RTX_LIB%" D3D12.lib DXGI.lib Advapi32.lib User32.lib ^
  /link /OUT:"%ROOT%\vsr_host.dll" /PDB:"%ROOT%\vsr_host.pdb"
if errorlevel 1 exit /b %errorlevel%

set "RTX_RUNTIME=%NV_RTX_VIDEO_SDK%\bin\Windows\x64\rel\nvngx_vsr.dll"
if not exist "%RTX_RUNTIME%" (
  echo [error] RTX Video VSR runtime was not found at:
  echo         %RTX_RUNTIME%
  exit /b 1
)
copy /y "%RTX_RUNTIME%" "%ROOT%\nvngx_vsr.dll" >nul
echo [ok] Built vsr_host.dll and staged nvngx_vsr.dll.
exit /b 0
