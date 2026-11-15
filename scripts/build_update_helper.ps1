[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][string]$WorkDirectory
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Reuse the verified .venv first.' }
$appIcon = Join-Path $projectRoot 'assets\app.ico'
if (-not (Test-Path -LiteralPath $appIcon -PathType Leaf)) { throw 'Missing required updater application icon.' }
# The caller must follow REPOSITORY_HYGIENE.md and budget output/work directories.
# A one-file helper is independent of the application's occupied _internal DLLs.
& $python -m PyInstaller --noconfirm --onefile --windowed --name DLSS5Update `
    --icon $appIcon `
    --paths $projectRoot --distpath $OutputDirectory --workpath $WorkDirectory `
    --specpath $WorkDirectory --noupx --exclude-module tkinter --exclude-module numpy `
    (Join-Path $projectRoot 'packaging\update_entry.py')
if ($LASTEXITCODE -ne 0) { throw "Updater build failed: $LASTEXITCODE" }

# Mandatory even when the main build uses -SkipTests. Source-only tests cannot
# prove that the frozen helper contains the same update compatibility rules.
$builtHelper = Join-Path $OutputDirectory 'DLSS5Update.exe'
foreach ($probeCase in @(
    @{ Edition = 'lite'; Readme = 'missing' },
    @{ Edition = 'full'; Readme = 'modified' }
)) {
    $probeOutput = Join-Path $WorkDirectory ("document-contract-" + $probeCase.Edition + '-' + $probeCase.Readme)
    & $python -B (Join-Path $projectRoot 'scripts\probe_file_update.py') `
        --helper $builtHelper --output $probeOutput --legacy-runtime --change-documents `
        --edition $probeCase.Edition --readme-state $probeCase.Readme
    if ($LASTEXITCODE -ne 0) { throw "Frozen updater document compatibility failed: $LASTEXITCODE" }
}
