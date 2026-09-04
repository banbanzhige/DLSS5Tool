[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
Push-Location $projectRoot

try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "Creating build environment..."
        py -3 -m venv (Join-Path $projectRoot ".venv")
    }

    if (-not $SkipInstall) {
        Write-Host "Installing application and build dependencies..."
        & $venvPython -m pip install --upgrade pip
        & $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt")
        & $venvPython -m pip install "pyinstaller>=6.0,<7"
    }

    $version = (& $venvPython -c "import app_version; print(app_version.APP_VERSION)").Trim()
    if (-not $version) {
        throw "Unable to read application version."
    }

    foreach ($requiredFile in @("dlssnr_host_v2.dll", "nvngx_dlssnr.dll")) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $requiredFile))) {
            throw "Missing required runtime file: $requiredFile"
        }
    }

    if (-not $SkipTests) {
        Write-Host "Running tests..."
        & $venvPython -m compileall -q -x "third_party|\.venv|build|dist" .
        & $venvPython -m unittest discover -v
    }

    Write-Host "Building portable $version release..."
    & $venvPython -m PyInstaller --noconfirm --clean (Join-Path $projectRoot "DLSS5Tool.spec")

    $releaseName = "DLSS5Tool-$version"
    $releaseDir = Join-Path $projectRoot "dist\$releaseName"
    $releaseExe = Join-Path $releaseDir "DLSS5Tool.exe"
    if (-not (Test-Path -LiteralPath $releaseExe)) {
        throw "Build completed without the expected executable: $releaseExe"
    }

    Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "CHANGELOG.md") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "THIRD_PARTY_NOTICES.md") -Destination $releaseDir -Force

    $zipPath = Join-Path $projectRoot "dist\$releaseName-win64.zip"
    Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -CompressionLevel Optimal -Force

    Write-Host "Release executable: $releaseExe"
    Write-Host "Release archive:    $zipPath"
}
finally {
    Pop-Location
}
