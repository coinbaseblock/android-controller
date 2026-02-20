param(
    [Parameter(Mandatory = $false, HelpMessage = 'Specific wireless target in the form IP or IP:PORT')]
    [string]$Device,

    [Parameter(Mandatory = $false, HelpMessage = 'Build images before starting containers')]
    [switch]$Build,

    [Parameter(Mandatory = $false, HelpMessage = 'Force recreate containers on startup')]
    [switch]$ForceRecreate,

    [Parameter(Mandatory = $false, HelpMessage = 'Retry forever until a wireless connection succeeds')]
    [switch]$RetryForever,

    [Parameter(Mandatory = $false, HelpMessage = 'Maximum reconnect attempts when -RetryForever is not used')]
    [ValidateRange(1, 300)]
    [int]$MaxRetries = 20,

    [Parameter(Mandatory = $false, HelpMessage = 'Delay between reconnect attempts')]
    [ValidateRange(1, 300)]
    [int]$RetryDelaySeconds = 6
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptRoot = $PSScriptRoot
if (-not $scriptRoot) {
    throw 'PSScriptRoot is not available. Please run this script from a file.'
}

$repoRoot = Split-Path -Parent $scriptRoot
if (-not (Test-Path -LiteralPath $repoRoot)) {
    throw "Repository root '$repoRoot' not found."
}

$connectScript = Join-Path $scriptRoot 'connect-wireless.ps1'
if (-not (Test-Path -LiteralPath $connectScript)) {
    throw "Missing required script: $connectScript"
}

function Invoke-DockerCompose {
    param([string[]]$Arguments)

    $dockerCommand = Get-Command -Name 'docker' -ErrorAction SilentlyContinue
    if (-not $dockerCommand) {
        throw 'Docker is not available on PATH.'
    }

    $composeArgs = @('compose') + $Arguments
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

Push-Location -LiteralPath $repoRoot
try {
    $upArgs = @('up', '-d')
    if ($Build) {
        $upArgs += '--build'
    }
    if ($ForceRecreate) {
        $upArgs += '--force-recreate'
    }

    Write-Host "[startup] docker compose $($upArgs -join ' ')" -ForegroundColor Cyan
    Invoke-DockerCompose -Arguments $upArgs

    $attempt = 0
    while ($true) {
        $attempt++

        try {
            $connectArgs = @()
            if ($Device) {
                $connectArgs += @('-Device', $Device)
            }

            Write-Host "[startup] reconnect attempt #$attempt" -ForegroundColor Yellow
            & $connectScript @connectArgs
            if ($LASTEXITCODE -eq 0) {
                Write-Host '[startup] wireless ADB is connected.' -ForegroundColor Green
                break
            }

            throw "connect-wireless.ps1 exited with code $LASTEXITCODE"
        }
        catch {
            $msg = $_.Exception.Message
            Write-Warning "Reconnect attempt #$attempt failed: $msg"
        }

        if (-not $RetryForever -and $attempt -ge $MaxRetries) {
            throw "Unable to reconnect after $MaxRetries attempts."
        }

        Start-Sleep -Seconds $RetryDelaySeconds
    }
}
finally {
    Pop-Location
}
