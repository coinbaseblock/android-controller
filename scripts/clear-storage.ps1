[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $false, HelpMessage = 'Drive letter to manage (default: drive where repository is located, e.g. D:)')]
    [ValidatePattern('^[A-Za-z]:$')]
    [string]$Drive,

    [Parameter(Mandatory = $false, HelpMessage = 'Minimum free space target in GB. If free space is lower, cleanup actions are recommended.')]
    [ValidateRange(1, 4096)]
    [int]$TargetFreeGB = 10,

    [Parameter(Mandatory = $false, HelpMessage = 'Delete generated files older than this many days (default: 7)')]
    [ValidateRange(1, 3650)]
    [int]$OlderThanDays = 7,

    [Parameter(Mandatory = $false, HelpMessage = 'Keep the newest N screenshots under img/ even if they are old')]
    [ValidateRange(0, 100000)]
    [int]$KeepRecentScreenshots = 100,

    [Parameter(Mandatory = $false, HelpMessage = 'Include Docker prune when free space is lower than target')]
    [switch]$PruneDocker,

    [Parameter(Mandatory = $false, HelpMessage = 'Use aggressive Docker cleanup (includes volumes). Use with caution.')]
    [switch]$AggressiveDocker,

    [Parameter(Mandatory = $false, HelpMessage = 'Show report only (do not delete files)')]
    [switch]$ReportOnly,

    [Parameter(Mandatory = $false, HelpMessage = 'Skip confirmation prompts')]
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Format-Bytes {
    param([Parameter(Mandatory = $true)][double]$Bytes)

    if ($Bytes -lt 1KB) { return ('{0:N0} B' -f $Bytes) }
    if ($Bytes -lt 1MB) { return ('{0:N2} KB' -f ($Bytes / 1KB)) }
    if ($Bytes -lt 1GB) { return ('{0:N2} MB' -f ($Bytes / 1MB)) }
    return ('{0:N2} GB' -f ($Bytes / 1GB))
}

function Get-DirSizeBytes {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return [int64]0
    }

    $sum = 0L
    foreach ($file in (Get-ChildItem -LiteralPath $Path -File -Recurse -ErrorAction SilentlyContinue)) {
        $sum += $file.Length
    }
    return $sum
}

function Remove-FilesSafe {
    param(
        [Parameter(Mandatory = $false)]
        [AllowNull()]
        [System.Collections.Generic.List[System.IO.FileInfo]]$Files,
        [Parameter(Mandatory = $true)][string]$Reason,
        [switch]$ReportOnly,
        [switch]$Force
    )

    if ($null -eq $Files -or $Files.Count -eq 0) {
        Write-Host "[info] No files matched for: $Reason" -ForegroundColor Yellow
        return [int64]0
    }

    $bytes = 0L
    foreach ($item in $Files) { $bytes += $item.Length }

    if ($ReportOnly) {
        Write-Host "[report] $Reason -> would remove $($Files.Count) file(s), reclaim $(Format-Bytes $bytes)" -ForegroundColor Cyan
        return [int64]0
    }

    if (-not $PSCmdlet.ShouldProcess($Reason, "Delete $($Files.Count) file(s)")) {
        Write-Host "[info] Skipped: $Reason" -ForegroundColor Yellow
        return [int64]0
    }

    if (-not $Force) {
        $warning = "This will permanently delete $($Files.Count) file(s) and reclaim about $(Format-Bytes $bytes)."
        if (-not $PSCmdlet.ShouldContinue($warning, "Continue cleanup for '$Reason'?")) {
            Write-Host "[info] Cancelled: $Reason" -ForegroundColor Yellow
            return [int64]0
        }
    }

    $deletedBytes = 0L
    $deletedCount = 0
    foreach ($item in $Files) {
        if (Test-Path -LiteralPath $item.FullName) {
            Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
            $deletedBytes += $item.Length
            $deletedCount++
        }
    }

    Write-Host "✅ $Reason -> deleted $deletedCount file(s), reclaimed $(Format-Bytes $deletedBytes)" -ForegroundColor Green
    return $deletedBytes
}

function Invoke-DockerCleanup {
    param(
        [switch]$Aggressive,
        [switch]$ReportOnly
    )

    $dockerCommand = Get-Command -Name 'docker' -ErrorAction SilentlyContinue
    if (-not $dockerCommand) {
        Write-Host '[warn] docker command not found, skipped Docker cleanup.' -ForegroundColor Yellow
        return
    }

    if ($ReportOnly) {
        if ($Aggressive) {
            Write-Host '[report] Would run: docker builder prune -af && docker system prune -af --volumes' -ForegroundColor Cyan
        } else {
            Write-Host '[report] Would run: docker builder prune -f && docker system prune -f' -ForegroundColor Cyan
        }
        return
    }

    if ($Aggressive) {
        Write-Host '[info] Running aggressive Docker cleanup...' -ForegroundColor Yellow
        & docker builder prune -af
        & docker system prune -af --volumes
    } else {
        Write-Host '[info] Running safe Docker cleanup...' -ForegroundColor Yellow
        & docker builder prune -f
        & docker system prune -f
    }

    if ($LASTEXITCODE -ne 0) {
        throw 'Docker cleanup failed.'
    }
}

$scriptRoot = $PSScriptRoot
if (-not $scriptRoot) {
    throw 'PSScriptRoot is not available. Please run this script from a file.'
}
$repoRoot = Split-Path -Parent $scriptRoot
if (-not (Test-Path -LiteralPath $repoRoot)) {
    throw "Repository root '$repoRoot' not found."
}

if (-not $Drive) {
    $resolvedRepo = Resolve-Path -LiteralPath $repoRoot
    $Drive = [System.IO.Path]::GetPathRoot($resolvedRepo.Path).TrimEnd('\\')
}

$driveInfo = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$Drive'" -ErrorAction SilentlyContinue
if (-not $driveInfo) {
    throw "Drive '$Drive' not found. Example valid value: D:"
}

$imgDir = Join-Path -Path $repoRoot -ChildPath 'img'
$dataDir = Join-Path -Path $repoRoot -ChildPath 'data'
$cutoff = (Get-Date).AddDays(-$OlderThanDays)

$beforeFreeBytes = [int64]$driveInfo.FreeSpace
$beforeTotalBytes = [int64]$driveInfo.Size

Write-Host '========== Storage Report (Before) ==========' -ForegroundColor Cyan
Write-Host "Drive: $Drive"
Write-Host "Free: $(Format-Bytes $beforeFreeBytes) / Total: $(Format-Bytes $beforeTotalBytes)"
Write-Host "Target free space: $TargetFreeGB GB"
Write-Host "img size: $(Format-Bytes (Get-DirSizeBytes -Path $imgDir))"
Write-Host "data size: $(Format-Bytes (Get-DirSizeBytes -Path $dataDir))"

$toDeleteScreens = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'
if (Test-Path -LiteralPath $imgDir) {
    $screens = @(Get-ChildItem -LiteralPath $imgDir -File -Filter 'screen-*.png' | Sort-Object LastWriteTime -Descending)
    $preserve = @()
    if ($KeepRecentScreenshots -gt 0 -and $screens.Count -gt 0) {
        $preserve = @($screens | Select-Object -First ([Math]::Min($KeepRecentScreenshots, $screens.Count)))
    }
    $preserveSet = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($item in $preserve) { [void]$preserveSet.Add($item.FullName) }

    foreach ($file in $screens) {
        if ($file.LastWriteTime -lt $cutoff -and -not $preserveSet.Contains($file.FullName)) {
            $toDeleteScreens.Add($file)
        }
    }
}

$toDeleteData = New-Object 'System.Collections.Generic.List[System.IO.FileInfo]'
if (Test-Path -LiteralPath $dataDir) {
    $patterns = @('screen-*.png', 'ui-*.xml', 'dump-*.xml', '*.tmp', '*.log')
    foreach ($pattern in $patterns) {
        foreach ($file in (Get-ChildItem -LiteralPath $dataDir -File -Recurse -Filter $pattern -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt $cutoff })) {
            $toDeleteData.Add($file)
        }
    }
}

$reclaimedBytes = 0L
$reclaimedBytes += Remove-FilesSafe -Files $toDeleteScreens -Reason "Delete old captured screenshots in img/ (keep newest $KeepRecentScreenshots)" -ReportOnly:$ReportOnly -Force:$Force
$reclaimedBytes += Remove-FilesSafe -Files $toDeleteData -Reason 'Delete old generated temp/log/ui dump files in data/' -ReportOnly:$ReportOnly -Force:$Force

$afterDriveInfo = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$Drive'" -ErrorAction Stop
$afterFreeBytes = [int64]$afterDriveInfo.FreeSpace

$belowTarget = ($afterFreeBytes -lt ($TargetFreeGB * 1GB))
if ($belowTarget) {
    Write-Host "[warn] Free space is still below target after local cleanup: $(Format-Bytes $afterFreeBytes) < $TargetFreeGB GB" -ForegroundColor Yellow

    if ($PruneDocker) {
        if ($ReportOnly) {
            Invoke-DockerCleanup -Aggressive:$AggressiveDocker -ReportOnly
        } else {
            if ($PSCmdlet.ShouldProcess('Docker cache and unused artifacts', 'Prune Docker storage')) {
                if (-not $Force) {
                    $warn = if ($AggressiveDocker) {
                        'Aggressive Docker prune can remove unused volumes. Continue?'
                    } else {
                        'Safe Docker prune will remove build cache and unused resources. Continue?'
                    }
                    if (-not $PSCmdlet.ShouldContinue($warn, 'Docker cleanup confirmation')) {
                        Write-Host '[info] Docker cleanup skipped by user choice.' -ForegroundColor Yellow
                    } else {
                        Invoke-DockerCleanup -Aggressive:$AggressiveDocker
                    }
                } else {
                    Invoke-DockerCleanup -Aggressive:$AggressiveDocker
                }
            }
        }
    } else {
        Write-Host '[hint] Re-run with -PruneDocker to cleanup Docker cache when free space is low.' -ForegroundColor Yellow
    }
}

$finalDriveInfo = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$Drive'" -ErrorAction Stop
$finalFreeBytes = [int64]$finalDriveInfo.FreeSpace
$deltaBytes = $finalFreeBytes - $beforeFreeBytes

Write-Host '========== Storage Report (After) ==========' -ForegroundColor Cyan
Write-Host "Free now: $(Format-Bytes $finalFreeBytes) / Total: $(Format-Bytes ([int64]$finalDriveInfo.Size))"
Write-Host "Space delta: $(Format-Bytes $deltaBytes)"
Write-Host "Local reclaimed estimate: $(Format-Bytes $reclaimedBytes)"

if ($ReportOnly) {
    Write-Host 'ℹ️ Report-only mode: no files were deleted.' -ForegroundColor Cyan
} elseif ($finalFreeBytes -ge ($TargetFreeGB * 1GB)) {
    Write-Host '✅ Cleanup completed and free space target is satisfied.' -ForegroundColor Green
} else {
    Write-Host '⚠️ Cleanup completed but free space is still below target. Consider increasing disk size or moving project data.' -ForegroundColor Yellow
}
