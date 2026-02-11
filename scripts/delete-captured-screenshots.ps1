[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $false, HelpMessage = 'Skip confirmation prompt')]
    [switch]$Force,

    [switch]$VerboseLog
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-DockerCompose {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [switch]$VerboseLog
    )

    $composeCommand = $null
    $commandArguments = $Arguments

    $dockerCommand = Get-Command -Name 'docker' -ErrorAction SilentlyContinue
    if ($dockerCommand) {
        & docker @('compose', 'version') *> $null
        if ($LASTEXITCODE -eq 0) {
            $composeCommand = 'docker'
            $commandArguments = @('compose') + $Arguments
        }
    }

    if (-not $composeCommand) {
        $dockerComposeCommand = Get-Command -Name 'docker-compose' -ErrorAction SilentlyContinue
        if ($dockerComposeCommand) {
            $composeCommand = 'docker-compose'
            $commandArguments = $Arguments
        }
    }

    if (-not $composeCommand) {
        throw 'Neither "docker compose" nor "docker-compose" is available on PATH. Install Docker with Compose support.'
    }

    if ($VerboseLog) {
        Write-Host "Using compose command: $composeCommand $($commandArguments -join ' ')" -ForegroundColor Yellow
    }

    & $composeCommand @commandArguments
}

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
    Write-Host "[info] Local img directory not found, nothing to delete." -ForegroundColor Yellow
}

$hostTargets = @()
if (Test-Path -LiteralPath $imgDir) {
    $hostTargets = @(Get-ChildItem -LiteralPath $imgDir -File -Filter 'screen-*.png' | ForEach-Object { $_.FullName })
}

$targetDescription = 'captured screenshots under img/'
if (-not $PSCmdlet.ShouldProcess($targetDescription, 'Delete captured screenshots')) {
    Write-Host '[info] Delete screenshots cancelled.' -ForegroundColor Yellow
    return
}

if (-not $Force) {
    $warning = 'This action will delete screenshots captured by the tools (files matching screen-*.png under img/ and /img).'
    if (-not $PSCmdlet.ShouldContinue($warning, 'Are you sure you want to delete captured screenshots?')) {
        Write-Host '[info] Delete screenshots cancelled.' -ForegroundColor Yellow
        return
    }
}

$deletedLocal = 0
if ($hostTargets.Count -gt 0) {
    foreach ($file in $hostTargets) {
        if (Test-Path -LiteralPath $file) {
            Remove-Item -LiteralPath $file -Force -ErrorAction Stop
            $deletedLocal++
        }
    }
    if ($deletedLocal -gt 0) {
        Write-Host "[info] Deleted $deletedLocal local screenshot(s) from $imgDir" -ForegroundColor Green
    }
} else {
    Write-Host '[info] No local screenshots found to delete.' -ForegroundColor Yellow
}

$deletedContainer = $false
$containerDeletedCount = 0

Push-Location -LiteralPath $repoRoot
try {
    $innerScript = @'
set -eu
set -o pipefail 2>/dev/null || true
shopt -s nullglob 2>/dev/null || true
files=(/img/screen-*.png)
count=${#files[@]}
if [ "$count" -eq 0 ]; then
  echo "__DELETE_COUNT__ 0"
  exit 0
fi
rm -f "${files[@]}"
echo "__DELETE_COUNT__ $count"
'@
    $innerScript = $innerScript.Replace("`r`n", "`n").Replace("`r", "`n")
    $innerScriptBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($innerScript))

    $composeArgs = @('exec', '-T', '--env', "ADB_INNER_SCRIPT=$innerScriptBase64", 'controller', 'bash', '-lc', 'printf ''%s'' "$ADB_INNER_SCRIPT" | base64 -d | bash')
    $composeOutput = Invoke-DockerCompose -Arguments $composeArgs -VerboseLog:$VerboseLog
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Docker Compose command exited with code $exitCode"
    }

    foreach ($line in $composeOutput) {
        if ($line -match '^__DELETE_COUNT__\s+(\d+)$') {
            $containerDeletedCount = [int]$matches[1]
            if ($containerDeletedCount -gt 0) {
                $deletedContainer = $true
                Write-Host "[info] Deleted $containerDeletedCount container screenshot(s) from /img" -ForegroundColor Green
            } else {
                Write-Host '[info] No container screenshots found to delete.' -ForegroundColor Yellow
            }
        } elseif ($line) {
            Write-Host $line
        }
    }
}
finally {
    Pop-Location
}

if ($deletedLocal -eq 0 -and -not $deletedContainer) {
    Write-Host '[info] Nothing was deleted (no matching screenshots found).' -ForegroundColor Yellow
}