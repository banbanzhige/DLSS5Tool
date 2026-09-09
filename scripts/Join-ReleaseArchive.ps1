[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidateSet('full','addon')][string]$Edition)

# Optional recovery path for standard ZIP volumes, not an installer.
# Run beside package-report.json and all .zip.00N parts. Never executes payloads.
$ErrorActionPreference = 'Stop'
$reportPath = Join-Path $PSScriptRoot 'package-report.json'
$report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
$entry = $report.editions.$Edition
if ($report.version -notmatch '^v\d+\.\d+\.\d+$') { throw 'Invalid package version.' }
$expectedName = "DLSS5Tool-$($report.version)-win64-$Edition.zip"
if ($entry.name -ne $expectedName -or @($entry.volumes).Count -eq 0) { throw 'Invalid archive entry.' }
$target = Join-Path $PSScriptRoot $expectedName
if (Test-Path -LiteralPath $target) { throw "Output already exists; nothing overwritten: $target" }
$index = 1
foreach ($part in $entry.volumes) {
    $expectedPart = $expectedName + ('.{0:D3}' -f $index)
    if ($part.name -ne $expectedPart) { throw 'Unexpected or unsafe volume name.' }
    $path = Join-Path $PSScriptRoot $expectedPart
    if ((Get-Item -LiteralPath $path).Length -ne $part.size) { throw "Incorrect size: $expectedPart" }
    Write-Host "Verifying $expectedPart"
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $part.sha256) {
        throw "Checksum mismatch: $expectedPart"
    }
    $index++
}
$partial = "$target.partial"
$stream = [System.IO.File]::Open($partial, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
try {
    foreach ($part in $entry.volumes) {
        $inputStream = [System.IO.File]::OpenRead((Join-Path $PSScriptRoot $part.name))
        try { $inputStream.CopyTo($stream) } finally { $inputStream.Dispose() }
    }
} finally { $stream.Dispose() }
if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $entry.sha256) {
    throw "Combined checksum mismatch. Partial output retained for inspection: $partial"
}
# No force/overwrite. Inputs are retained; a failure never deletes downloaded parts.
[System.IO.File]::Move($partial, $target)
Write-Host "Verified ZIP ready to extract: $target"
