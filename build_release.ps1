[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
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

    $version = (& $venvPython -c "import app_version; print(app_version.APP_VERSION)").Trim()
    Assert-ExternalSuccess "Reading the application version"
    if (-not $version) {
        throw "Unable to read application version."
    }

    foreach ($requiredFile in @(
        "dlssnr_host_v2.dll", "nvngx_dlssnr.dll", "vsr_host.dll", "nvngx_vsr.dll"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $requiredFile))) {
            throw "Missing required runtime file: $requiredFile"
        }
    }

    if (-not $SkipTests) {
        Write-Host "Running tests..."
        & $venvPython -m compileall -q -x "third_party|\.venv|build|dist" .
        Assert-ExternalSuccess "Compiling Python sources"
        & $venvPython -m unittest discover -v
        Assert-ExternalSuccess "Running tests"
    }

    Write-Host "Building portable $version release..."
    & $venvPython -m PyInstaller --noconfirm --clean (Join-Path $projectRoot "DLSS5Tool.spec")
    Assert-ExternalSuccess "Building the portable application"

    $releaseName = "DLSS5Tool-$version"
    $releaseDir = Join-Path $projectRoot "dist\$releaseName"
    $releaseExe = Join-Path $releaseDir "DLSS5Tool.exe"
    if (-not (Test-Path -LiteralPath $releaseExe)) {
        throw "Build completed without the expected executable: $releaseExe"
    }

    Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "README.en.md") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "CHANGELOG.md") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination $releaseDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "THIRD_PARTY_NOTICES.md") -Destination $releaseDir -Force

    $rtxSdkRoot = $env:NV_RTX_VIDEO_SDK
    if (-not $rtxSdkRoot) {
        $rtxSdkRoot = Join-Path $projectRoot "third_party\RTX_Video_SDK"
    }
    $rtxLicense = Join-Path $rtxSdkRoot "NVIDIA_RTX_Video_SDK_License.pdf"
    if (-not (Test-Path -LiteralPath $rtxLicense)) {
        throw "Missing RTX Video SDK license for distribution: $rtxLicense"
    }
    Copy-Item -LiteralPath $rtxLicense -Destination $releaseDir -Force

    $zipPath = Join-Path $projectRoot "dist\$releaseName-win64.zip"
    Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -CompressionLevel Optimal -Force

    Write-Host "Release executable: $releaseExe"
    Write-Host "Release archive:    $zipPath"
}
finally {
    Pop-Location
}
