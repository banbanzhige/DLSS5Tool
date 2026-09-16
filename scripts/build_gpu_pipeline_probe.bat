@echo off
setlocal EnableExtensions EnableDelayedExpansion
if "%~1"=="" exit /b 2
set "OUT=%~f1"
set "ROOT=%~dp0.."
if not exist "%OUT%\TASK.md" exit /b 2
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq tokens=*" %%i in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find VC\Auxiliary\Build\vcvars64.bat`) do set "VCVARS=%%i"
if not defined VCVARS exit /b 1
call "!VCVARS!" >nul
if errorlevel 1 exit /b 1
set "SDK=%ROOT%\tmp\RTX_Video_SDK_v1.1.0"
if not exist "%OUT%\native-vsr" mkdir "%OUT%\native-vsr"
if not exist "%OUT%\native-color" mkdir "%OUT%\native-color"
cl /nologo /std:c++17 /O2 /EHsc /MT /LD /I"%SDK%\include" /Fo"%OUT%\native-vsr\host.obj" ^
 "%ROOT%\scripts\gpu_vsr_probe.cpp" "%SDK%\lib\Windows\x64\nvsdk_ngx_s.lib" D3D12.lib DXGI.lib Advapi32.lib User32.lib ^
 /link /OUT:"%OUT%\native-vsr\candidate.dll" /PDB:"%OUT%\native-vsr\host.pdb" /IMPLIB:"%OUT%\native-vsr\host.lib"
if errorlevel 1 exit /b 1
call "%ROOT%\scripts\build_isolated_host.bat" "%ROOT%\scripts\gpu_color_probe.cpp" "%OUT%\native-color"
if errorlevel 1 exit /b 1
if not exist "%OUT%\nvEncodeAPI.h" exit /b 0
if not exist "%OUT%\native-encoder" mkdir "%OUT%\native-encoder"
cl /nologo /std:c++17 /O2 /EHsc /MT /LD /I"%OUT%" /Fo"%OUT%\native-encoder\host.obj" ^
 "%ROOT%\scripts\gpu_nvenc_probe.cpp" /link /OUT:"%OUT%\native-encoder\candidate.dll" ^
 /PDB:"%OUT%\native-encoder\host.pdb" /IMPLIB:"%OUT%\native-encoder\host.lib"
if errorlevel 1 exit /b 1
if not exist "%OUT%\native-dlssg" mkdir "%OUT%\native-dlssg"
cl /nologo /std:c++17 /O2 /EHsc /MT /DGPU_PIPELINE_PROBE /I"%ROOT%\third_party\NVIDIA-DLSS\include" ^
 /Fo"%OUT%\native-dlssg\worker.obj" "%ROOT%\scripts\dlssg_video_worker.cpp" ^
 "%ROOT%\third_party\NVIDIA-DLSS\lib\Windows_x86_64\x64\nvsdk_ngx_s.lib" D3D12.lib DXGI.lib Advapi32.lib User32.lib ^
 /link /OUT:"%OUT%\native-dlssg\candidate.exe" /PDB:"%OUT%\native-dlssg\worker.pdb" /IMPLIB:"%OUT%\native-dlssg\worker.lib"
if errorlevel 1 exit /b 1
rem Compile without the research macro too: the normal protocol must still build.
cl /nologo /std:c++17 /O2 /EHsc /MT /I"%ROOT%\third_party\NVIDIA-DLSS\include" ^
 /Fo"%OUT%\native-dlssg\control.obj" "%ROOT%\scripts\dlssg_video_worker.cpp" ^
 "%ROOT%\third_party\NVIDIA-DLSS\lib\Windows_x86_64\x64\nvsdk_ngx_s.lib" D3D12.lib DXGI.lib Advapi32.lib User32.lib ^
 /link /OUT:"%OUT%\native-dlssg\control.exe" /PDB:"%OUT%\native-dlssg\control.pdb" /IMPLIB:"%OUT%\native-dlssg\control.lib"
exit /b %errorlevel%
