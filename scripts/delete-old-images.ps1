[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $false, HelpMessage = 'Delete files older than this many days (default: 7)')]
    [ValidateRange(1, 3650)]
    [int]$OlderThanDays = 7,

    [Parameter(Mandatory = $false, HelpMessage = 'Skip confirmation prompt')]
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptRoot = $PSScriptRoot
if (-not $scriptRoot) {
    throw 'PSScriptRoot is not available. Please run this script from a file, not via stdin.'
}

$repoRoot = Split-Path -Parent $scriptRoot
if (-not (Test-Path -LiteralPath $repoRoot)) {
    throw "Repository root '$repoRoot' not found."
}

$imgDir = Join-Path -Path $repoRoot -ChildPath 'img'
if (-not (Test-Path -LiteralPath $imgDir)) {
    Write-Host "[info] Directory not found: $imgDir" -ForegroundColor Yellow
    return
}

$cutoff = (Get-Date).AddDays(-$OlderThanDays)
$targets = @(Get-ChildItem -LiteralPath $imgDir -File -Recurse | Where-Object { $_.LastWriteTime -lt $cutoff })

if ($targets.Count -eq 0) {
    Write-Host "[info] No images older than $OlderThanDays day(s) found under $imgDir" -ForegroundColor Yellow
    return
}

$targetDescription = "old images under img/ (older than $OlderThanDays day(s))"
if (-not $PSCmdlet.ShouldProcess($targetDescription, 'Delete old images')) {
    Write-Host '[info] Delete old images cancelled.' -ForegroundColor Yellow
    return
}

if (-not $Force) {
    $warning = "This action will permanently delete $($targets.Count) file(s) older than $OlderThanDays day(s)."
    if (-not $PSCmdlet.ShouldContinue($warning, 'Are you sure you want to continue?')) {
        Write-Host '[info] Delete old images cancelled.' -ForegroundColor Yellow
        return
    }
}

$deleted = 0
foreach ($file in $targets) {
    if (Test-Path -LiteralPath $file.FullName) {
        Remove-Item -LiteralPath $file.FullName -Force -ErrorAction Stop
        $deleted++
    }
}

Write-Host "✅ Deleted $deleted old image file(s) from $imgDir" -ForegroundColor Green
