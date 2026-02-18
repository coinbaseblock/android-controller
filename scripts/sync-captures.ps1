[CmdletBinding()]
param(
    [switch]$DeleteOld,
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

$capturesDir = Join-Path -Path $repoRoot -ChildPath 'img/captures'
if (-not (Test-Path -LiteralPath $capturesDir)) {
    New-Item -ItemType Directory -Path $capturesDir -Force | Out-Null
}

if ($DeleteOld) {
    $oldFiles = @(Get-ChildItem -LiteralPath $capturesDir -File -ErrorAction SilentlyContinue)
    if ($oldFiles.Count -gt 0) {
        foreach ($file in $oldFiles) {
            Remove-Item -LiteralPath $file.FullName -Force
        }
        Write-Host "[info] Deleted $($oldFiles.Count) old file(s) in $capturesDir" -ForegroundColor Yellow
    }
    elseif ($VerboseLog) {
        Write-Host "[info] No old files found in $capturesDir" -ForegroundColor Yellow
    }
}

$dockerCopyTarget = $capturesDir
if ($dockerCopyTarget -match '^[A-Za-z]:\\') {
    $dockerCopyTarget = $dockerCopyTarget -replace '\\', '/'
}

Push-Location -LiteralPath $repoRoot
try {
    $copyArgs = @('cp', 'controller:/img/captures/.', $dockerCopyTarget)
    Invoke-DockerCompose -Arguments $copyArgs -VerboseLog:$VerboseLog
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose copy command exited with code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "✅ Synced captures to $capturesDir" -ForegroundColor Green
