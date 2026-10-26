[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$TaskDirectory)
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))

function Assert-SafePath([string]$Candidate,[string]$Scope,[bool]$AllowRoot=$false) {
    $absolute=[IO.Path]::GetFullPath($Candidate)
    $scopePath=[IO.Path]::GetFullPath($Scope).TrimEnd('\')
    if (-not ($absolute.StartsWith($scopePath+'\',[StringComparison]::OrdinalIgnoreCase) -or
            ($AllowRoot -and $absolute.Equals($scopePath,[StringComparison]::OrdinalIgnoreCase)))) {
        throw "Path escaped expected scope: $absolute"
    }
    $cursor=$absolute
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Reparse point: $cursor"
            }
        }
        $parent=Split-Path -Parent $cursor
        if ($parent -eq $cursor) { break }
        $cursor=$parent
    }
    if ((Test-Path -LiteralPath $absolute) -and (Resolve-Path -LiteralPath $absolute).ProviderPath -ne $absolute) {
        throw "Unexpected resolved path: $absolute"
    }
    return $absolute
}
function Sha256([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

$task=Assert-SafePath $TaskDirectory (Join-Path $projectRoot 'tmp')
$runtime=Assert-SafePath (Join-Path $projectRoot 'runtime') $projectRoot
$cudaRoot=Assert-SafePath (Join-Path $runtime 'vsr-cuda') $runtime
$plan=Get-Content -Raw -LiteralPath (Assert-SafePath (Join-Path $task 'install-plan.json') $task) | ConvertFrom-Json
if ($plan.task -ne $task) { throw 'Install plan belongs to another task' }
$proofPath=Assert-SafePath (Join-Path $task 'verification-summary.json') $task
if ((Sha256 $proofPath) -ne $plan.proof_sha256) { throw 'Verification evidence changed after planning' }
$proof=Get-Content -Raw -LiteralPath $proofPath | ConvertFrom-Json
if ($proof.status -ne 'verified') { throw 'Native upgrade has not passed verification' }
foreach ($entry in $proof.source_manifest.PSObject.Properties) {
    $source=Assert-SafePath (Join-Path $projectRoot $entry.Name) $projectRoot
    if ((Sha256 $source) -ne $entry.Value) { throw "Verified source changed: $source" }
}
$hostTarget=Assert-SafePath $plan.d3d_target $runtime
if ($hostTarget -ne (Join-Path $runtime 'vsr_host.dll')) { throw 'Unexpected native host target' }
$hostSource=Assert-SafePath $plan.d3d_source $task
if ((Sha256 $hostSource) -ne $plan.candidate_d3d_sha256) { throw 'Native candidate hash mismatch' }
$currentHash=Sha256 $hostTarget
if ($currentHash -notin @($plan.previous_d3d_sha256,$plan.candidate_d3d_sha256)) {
    throw 'Current host changed independently; preserve it and reconcile before installing'
}
foreach ($entry in $plan.files) {
    $source=Assert-SafePath $entry.source $projectRoot
    $target=Assert-SafePath $entry.destination $cudaRoot
    if ((Sha256 $source) -ne $entry.sha256) { throw "Candidate changed: $source" }
    if ((Test-Path -LiteralPath $target) -and (Sha256 $target) -ne $entry.sha256) {
        throw "An existing different CUDA component must be preserved: $target"
    }
}
$rollback=Assert-SafePath $plan.rollback $task
if (Test-Path -LiteralPath $rollback) {
    if ((Sha256 $rollback) -ne $plan.previous_d3d_sha256) { throw 'Existing rollback differs; preserve it' }
} else {
    if ($currentHash -ne $plan.previous_d3d_sha256) { throw 'Verified previous host is unavailable for rollback' }
    $null=New-Item -ItemType Directory -Path (Split-Path -Parent $rollback) -Force
    Copy-Item -LiteralPath $hostTarget -Destination $rollback
    if ((Sha256 $rollback) -ne $plan.previous_d3d_sha256) { throw 'Rollback copy verification failed' }
}
foreach ($entry in $plan.files) {
    $source=Assert-SafePath $entry.source $projectRoot
    $target=Assert-SafePath $entry.destination $cudaRoot
    $null=New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force
    if (-not (Test-Path -LiteralPath $target)) { Copy-Item -LiteralPath $source -Destination $target }
    if ((Sha256 $target) -ne $entry.sha256) { throw "Installed component hash mismatch: $target" }
}
if ($currentHash -ne $plan.candidate_d3d_sha256) {
    $staging=Assert-SafePath (Join-Path $task 'install-staging\vsr_host.dll') $task
    $null=New-Item -ItemType Directory -Path (Split-Path -Parent $staging) -Force
    Copy-Item -LiteralPath $hostSource -Destination $staging
    if ((Sha256 $staging) -ne $plan.candidate_d3d_sha256) { throw 'Host staging verification failed' }
    # Same-volume atomic replacement: a loaded/locked target fails without
    # deleting the verified previous host. The verified rollback stays intact.
    $null=Assert-SafePath $staging $task
    $null=Assert-SafePath $hostTarget $runtime
    $replaceBackup=Assert-SafePath (Join-Path $task 'install-staging\pre-replace-vsr_host.dll') $task
    if ((Test-Path -LiteralPath $replaceBackup) -and (Sha256 $replaceBackup) -ne $plan.previous_d3d_sha256) {
        throw 'Unexpected existing replacement backup; preserve it'
    }
    [IO.File]::Replace($staging,$hostTarget,$replaceBackup)
    if ((Sha256 $replaceBackup) -ne $plan.previous_d3d_sha256) { throw 'Atomic replacement backup mismatch' }
}
if ((Sha256 $hostTarget) -ne $plan.candidate_d3d_sha256) { throw 'Installed host verification failed' }
$record=@{status='installed';task=$task;installed=(Get-Date -Format o);host=$hostTarget;
    host_sha256=(Sha256 $hostTarget);rollback=$rollback;rollback_sha256=(Sha256 $rollback);
    cuda_root=$cudaRoot;default_route='Existing D3D12 path retained; CUDA device-frame backend is explicit';
    files=$plan.files;published=$false;packaged=$false}
$record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $task 'installed.json') -Encoding utf8
$record | Select-Object status,host,host_sha256,rollback,cuda_root,default_route | ConvertTo-Json
