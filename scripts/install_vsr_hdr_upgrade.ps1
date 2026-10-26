[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$TaskDirectory)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
function Assert-PlainPath([string]$Candidate,[string]$Scope) {
    $absolute = [IO.Path]::GetFullPath($Candidate)
    $scopePath = [IO.Path]::GetFullPath($Scope).TrimEnd('\')
    if (-not $absolute.StartsWith($scopePath+'\',[StringComparison]::OrdinalIgnoreCase)) { throw "Path outside scope: $absolute" }
    $cursor = $absolute
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse path: $cursor" }
        }
        $parent = Split-Path -Parent $cursor
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
    return $absolute
}
function Sha256([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
$task = Assert-PlainPath $TaskDirectory (Join-Path $projectRoot 'tmp')
$plan = Get-Content -LiteralPath (Assert-PlainPath (Join-Path $task 'install-plan.json') $task) -Raw | ConvertFrom-Json
$proofPath = Assert-PlainPath $plan.proof $task
if ($plan.task -ne $task -or (Sha256 $proofPath) -ne $plan.proof_sha256) { throw 'Install plan/evidence mismatch' }
$proof = Get-Content -LiteralPath $proofPath -Raw | ConvertFrom-Json
if ($proof.status -ne 'verified') { throw 'Upgrade has not passed acceptance' }
foreach ($entry in $proof.source_manifest.PSObject.Properties) {
    $source = Assert-PlainPath (Join-Path $projectRoot $entry.Name) $projectRoot
    if ((Sha256 $source) -ne $entry.Value) { throw "Source changed after verification: $source" }
}
$runtime = Assert-PlainPath (Join-Path $projectRoot 'runtime') $projectRoot
$target = Assert-PlainPath $plan.target $runtime
$candidate = Assert-PlainPath $plan.candidate $task
$rollback = Assert-PlainPath $plan.rollback $task
if ($target -ne (Join-Path $runtime 'vsr_host.dll')) { throw 'Unexpected host target' }
if ((Sha256 $candidate) -ne $plan.candidate_sha256) { throw 'Candidate changed' }
$provider = Assert-PlainPath (Join-Path $runtime 'nvngx_vsr.dll') $runtime
if ((Sha256 $provider) -ne $plan.provider_sha256) { throw 'Provider changed after acceptance' }
$previous = Sha256 $target
if ($previous -notin @($plan.previous_sha256,$plan.candidate_sha256)) { throw 'Current host changed independently; retain it' }
if (Test-Path -LiteralPath $rollback) {
    if ((Sha256 $rollback) -ne $plan.previous_sha256) { throw 'Existing rollback differs; retain it' }
} else {
    if ($previous -ne $plan.previous_sha256) { throw 'Verified old host unavailable for rollback' }
    $null = New-Item -ItemType Directory -Path (Split-Path -Parent $rollback) -Force
    Copy-Item -LiteralPath $target -Destination $rollback
    if ((Sha256 $rollback) -ne $plan.previous_sha256) { throw 'Rollback verification failed' }
}
if ($previous -ne $plan.candidate_sha256) {
    $stage = Assert-PlainPath (Join-Path $task 'install-staging\vsr_host.dll') $task
    $null = New-Item -ItemType Directory -Path (Split-Path -Parent $stage) -Force
    if ((Test-Path -LiteralPath $stage) -and (Sha256 $stage) -ne $plan.candidate_sha256) { throw 'Different staging file retained' }
    if (-not (Test-Path -LiteralPath $stage)) { Copy-Item -LiteralPath $candidate -Destination $stage }
    if ((Sha256 $stage) -ne $plan.candidate_sha256) { throw 'Staging verification failed' }
    $null = Assert-PlainPath $stage $task
    $null = Assert-PlainPath $target $runtime
    # Use an explicit backup path: PowerShell converts a null string argument
    # into an empty path for this overload. Both old-host copies are verified.
    $replaceBackup = Assert-PlainPath (Join-Path $task 'install-staging\pre-replace-vsr_host.dll') $task
    if ((Test-Path -LiteralPath $replaceBackup) -and (Sha256 $replaceBackup) -ne $plan.previous_sha256) { throw 'Unexpected replacement backup retained' }
    [IO.File]::Replace($stage,$target,$replaceBackup)
    if ((Sha256 $replaceBackup) -ne $plan.previous_sha256) { throw 'Atomic backup verification failed' }
}
if ((Sha256 $target) -ne $plan.candidate_sha256) { throw 'Installed host hash mismatch' }
$record = [pscustomobject]@{status='installed-file-verified';host=$target;sha256=(Sha256 $target);
    rollback=$rollback;rollback_sha256=(Sha256 $rollback);provider_sha256=(Sha256 $provider);
    post_install_smoke_pending=$true;packaged=$false;published=$false}
$record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $task 'installed.json') -Encoding utf8
$record | ConvertTo-Json -Depth 5
