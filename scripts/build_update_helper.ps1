[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][string]$WorkDirectory
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Reuse the verified .venv first.' }
# The caller must follow REPOSITORY_HYGIENE.md and budget output/work directories.
# A one-file helper is independent of the application's occupied _internal DLLs.
& $python -m PyInstaller --noconfirm --onefile --windowed --name DLSS5Update `
    --paths $projectRoot --distpath $OutputDirectory --workpath $WorkDirectory `
    --specpath $WorkDirectory --noupx --exclude-module tkinter --exclude-module numpy `
    (Join-Path $projectRoot 'packaging\update_entry.py')
if ($LASTEXITCODE -ne 0) { throw "Updater build failed: $LASTEXITCODE" }
