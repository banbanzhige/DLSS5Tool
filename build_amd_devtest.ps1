[CmdletBinding()]
param([switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
Push-Location $taskRoot
try {
    & cmd /c native_amd_probe\build.bat
    if ($LASTEXITCODE -ne 0) { throw 'Native probe compilation failed.' }
    if (-not $SkipTests) {
        & $taskPython -m unittest tests.test_amd_devtest -v
        if ($LASTEXITCODE -ne 0) { throw 'AMD developer test unit tests failed.' }
    }
    $release = Join-Path $taskRoot 'dist\DLSS5Tool-AMD-dev1'
    if (Test-Path -LiteralPath $release) {
        # PyInstaller's --noconfirm recreates COLLECT. Refuse before it can erase
        # any user runtime or results placed into an earlier developer build.
        if (Test-Path -LiteralPath (Join-Path $release 'results')) { throw 'Existing developer build has test results; preserve it before rebuilding.' }
        $oldRuntime = Join-Path $release 'amd_backend'
        if (Test-Path -LiteralPath $oldRuntime) {
            $userFiles = @(Get-ChildItem -LiteralPath $oldRuntime -Force | Where-Object { $_.Name -notin @('README.txt','amd_probe.exe') })
            if ($userFiles.Count -gt 0) { throw 'Existing developer build has user runtime files; preserve it before rebuilding.' }
        }
    }
    & $taskPython -m PyInstaller --noconfirm AMD-DevTest.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    $runtime = Join-Path $release 'amd_backend'
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    Copy-Item -LiteralPath (Join-Path $taskRoot 'amd_backend\README.txt') -Destination $runtime -Force
    # The official installer is intended to sit next to an application EXE.
    # This is OUR native host (not an upstream proxy); the test controller uses
    # its protected _internal copy to prevent local version.dll auto-injection.
    Copy-Item -LiteralPath (Join-Path $taskRoot 'build\amd_probe\amd_probe.exe') -Destination $runtime -Force
    Copy-Item -LiteralPath (Join-Path $taskRoot 'AMD_DEVTEST.md') -Destination $release -Force
    Copy-Item -LiteralPath (Join-Path $taskRoot 'LICENSE') -Destination $release -Force
    Copy-Item -LiteralPath (Join-Path $taskRoot 'third_party\FidelityFX-1.1.4\LICENSE.txt') -Destination (Join-Path $release 'FIDELITYFX_LICENSE.txt') -Force
    Copy-Item -LiteralPath (Join-Path $taskRoot 'amd_backend\README.txt') -Destination (Join-Path $release 'UPSTREAM_COMPONENTS.txt') -Force
    # No catch-all copying from source/runtime directories: these can contain
    # user-supplied proprietary binaries, weights, settings, or personal data.
    $forbidden = @(Get-ChildItem -LiteralPath $release -Recurse -File | Where-Object {
        $_.Name -in @('version.dll','dlssnr_on_amd_setup.exe','dlssnr_on_amd_weights.bin','nvngx_dlssnr.dll','nvngx_vsr.dll')
    })
    if ($forbidden.Count -gt 0) { throw 'User/upstream components detected in release directory. Use a fresh build directory; do not distribute it.' }
    $archive = Join-Path $taskRoot 'dist\DLSS5Tool-AMD-dev1-win64.zip'
    Compress-Archive -Path $release -DestinationPath $archive -CompressionLevel Optimal -Force
    Get-Item -LiteralPath $archive | Select-Object FullName, Length
    Get-FileHash -LiteralPath $archive -Algorithm SHA256
} finally { Pop-Location }
