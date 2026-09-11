[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$SkipTests,
    [string]$OutputDirectory,
    [string]$WorkDirectory
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

function Assert-ExternalSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

Push-Location $projectRoot

try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "Creating build environment..."
        py -3 -m venv (Join-Path $projectRoot ".venv")
        Assert-ExternalSuccess "Creating the Python environment"
    }

    if (-not $SkipInstall) {
        Write-Host "Installing application and build dependencies..."
        & $venvPython -m pip install --upgrade pip
        Assert-ExternalSuccess "Updating pip"
        & $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt")
        Assert-ExternalSuccess "Installing application dependencies"
        & $venvPython -m pip install "pyinstaller>=6.0,<7"
        Assert-ExternalSuccess "Installing PyInstaller"
    }

    $version = (& $venvPython -c "from dlss5tool.app_version import APP_VERSION; print(APP_VERSION)").Trim()
    Assert-ExternalSuccess "Reading the application version"
    if (-not $version) {
        throw "Unable to read application version."
    }

    foreach ($requiredFile in @(
        "dlssnr_host_v2.dll", "nvngx_dlssnr.dll", "vsr_host.dll", "nvngx_vsr.dll"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "runtime\$requiredFile"))) {
            throw "Missing required runtime file: $requiredFile"
        }
    }

    if (-not $SkipTests) {
        Write-Host "Running tests..."
        & $venvPython -m compileall -q -x "third_party|\.venv|build|dist|tmp|output|mods" .
        Assert-ExternalSuccess "Compiling Python sources"
        & $venvPython -m unittest discover -v
        Assert-ExternalSuccess "Running tests"
    }

    Write-Host "Building portable $version release..."
    $distRoot = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $projectRoot 'dist' }
    $buildRoot = if ($WorkDirectory) { [IO.Path]::GetFullPath($WorkDirectory) } else { Join-Path $projectRoot 'build' }
    if ((Test-Path -LiteralPath (Join-Path $distRoot "DLSS5Tool-$version")) -or
        (Test-Path -LiteralPath (Join-Path $distRoot "DLSS5Tool-$version-win64.zip"))) {
        throw 'Release target already exists; choose a new OutputDirectory to preserve previous packages.'
    }
    & $venvPython -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $buildRoot (Join-Path $projectRoot "packaging\DLSS5Tool.spec")
    Assert-ExternalSuccess "Building the portable application"

    $releaseName = "DLSS5Tool-$version"
    $releaseDir = Join-Path $distRoot $releaseName
    $releaseExe = Join-Path $releaseDir "DLSS5Tool.exe"
    if (-not (Test-Path -LiteralPath $releaseExe)) {
        throw "Build completed without the expected executable: $releaseExe"
    }

    & (Join-Path $projectRoot 'scripts\build_update_helper.ps1') `
        -OutputDirectory $releaseDir -WorkDirectory (Join-Path $buildRoot 'update-helper')
    if (-not (Test-Path -LiteralPath (Join-Path $releaseDir 'DLSS5Update.exe'))) {
        throw 'Standalone update helper is missing.'
    }

    Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "README.en.md") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "CHANGELOG.md") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "THIRD_PARTY_NOTICES.md") -Destination $releaseDir -Force

    # Only the instructions are shipped; never copy user models, Python or EXEs.
    $releaseMods = Join-Path $releaseDir "mods"
    New-Item -ItemType Directory -Path $releaseMods -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $projectRoot "mods\README.md") -Destination $releaseMods -Force
    $unexpectedMods = @(Get-ChildItem -LiteralPath $releaseMods -Force | Where-Object { $_.Name -ne "README.md" })
    if ($unexpectedMods.Count -gt 0) {
        throw "Refusing to package user modules from $releaseMods. Build into a clean release directory."
    }

    $rtxSdkRoot = $env:NV_RTX_VIDEO_SDK
    if (-not $rtxSdkRoot) {
        $rtxSdkRoot = Join-Path $projectRoot "third_party\RTX_Video_SDK"
    }
    $rtxLicense = Join-Path $rtxSdkRoot "NVIDIA_RTX_Video_SDK_License.pdf"
    if (-not (Test-Path -LiteralPath $rtxLicense)) {
        throw "Missing RTX Video SDK license for distribution: $rtxLicense"
    }
    Copy-Item -LiteralPath $rtxLicense -Destination $releaseDir -Force

    # AMD validation and experiments are source-only, not release contents.
    & $venvPython (Join-Path $projectRoot "scripts\check_release_contents.py") $releaseDir
    Assert-ExternalSuccess "Checking release content isolation"

    $zipPath = Join-Path $distRoot "$releaseName-win64.zip"
    Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -CompressionLevel Optimal -Force

    Write-Host "Release executable: $releaseExe"
    Write-Host "Release archive:    $zipPath"
}
finally {
    Pop-Location
}
