<#+
.SYNOPSIS
    Helper script to control an Android device via adb.

.DESCRIPTION
    Provides simple commands for taps, swipes, and key events using adb inside the
    running Docker controller service.

.EXAMPLE
    # Swipe from left to right on the default device
    .\control-device.ps1 -Action Swipe -X 200 -Y 1000 -X2 900 -Y2 1000 -Duration 300

.EXAMPLE
    # Send the HOME key event to a specific device
    .\control-device.ps1 -Action Key -KeyName HOME -Serial emulator-5554

.NOTES␊
    Requires Docker (with Compose) and the controller service to be running.
#>

[CmdletBinding()]
param(
    [ValidateSet("Tap", "Swipe", "Key")]
    [string]$Action,

    [Parameter()]
    [int]$X,

    [Parameter()]
    [int]$Y,

    [Parameter()]
    [int]$X2,

    [Parameter()]
    [int]$Y2,

    [Parameter()]
    [int]$Duration = 300,

    [Parameter()]
    [int]$Keycode,

    [Parameter()]
    [string]$KeyName,

    [Parameter()]
    [string]$Serial,

    [switch]$Help,

    [switch]$VerboseLog
)

function Show-Usage {
    Write-Host "Usage:" -ForegroundColor Cyan
    Write-Host "  .\\control-device.ps1 -Action <Tap|Swipe|Key> [options]" -ForegroundColor Cyan
    Write-Host
    Write-Host "Common options:" -ForegroundColor Cyan
    Write-Host "  -Serial <serial>    Target a specific device (use adb devices to list)"
    Write-Host "  -Duration <ms>      Swipe duration in milliseconds (default: 300)"
    Write-Host
    Write-Host "Tap requires:" -ForegroundColor Cyan
    Write-Host "  -X <int> -Y <int>   Coordinates to tap"
    Write-Host
    Write-Host "Swipe requires:" -ForegroundColor Cyan
    Write-Host "  -X <int> -Y <int> -X2 <int> -Y2 <int> [-Duration <ms>]"
    Write-Host
    Write-Host "Key requires one of:" -ForegroundColor Cyan
    Write-Host "  -Keycode <int>      Numeric Android keycode"
    Write-Host "  -KeyName <string>   Named Android key (e.g. HOME, BACK, RECENTS)"
    Write-Host
    Write-Host "Examples:" -ForegroundColor Cyan
    Write-Host "  # Tap near the bottom center of the screen"
    Write-Host "  .\\control-device.ps1 -Action Tap -X 540 -Y 1600"
    Write-Host
    Write-Host "  # Swipe left to right over 300ms"
    Write-Host "  .\\control-device.ps1 -Action Swipe -X 200 -Y 1000 -X2 900 -Y2 1000 -Duration 300"
    Write-Host
    Write-Host "  # Press HOME on a specific device"
    Write-Host "  .\\control-device.ps1 -Action Key -KeyName HOME -Serial emulator-5554"
}

function Fail-And-ShowUsage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    Write-Error $Message
    Write-Host
    Show-Usage
    exit 1
}

if ($Help) {
    Show-Usage
    return
}

if (-not $PSBoundParameters.ContainsKey("Action")) {
    Fail-And-ShowUsage -Message "Missing required -Action parameter."
}

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

function Invoke-AdbCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $adbArgs = @()
    if ($Serial) {
        $adbArgs += "-s"
        $adbArgs += $Serial
    }

    $adbArgs += $Arguments

    $composeArgs = @('exec', '-T', 'controller', 'adb') + $adbArgs

    Push-Location -LiteralPath $repoRoot
    try {
        Invoke-DockerCompose -Arguments $composeArgs -VerboseLog:$VerboseLog
        if ($LASTEXITCODE -ne 0) {
            throw "adb exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

switch ($Action) {
    "Tap" {
        if ($PSBoundParameters.ContainsKey("X") -and $PSBoundParameters.ContainsKey("Y")) {
            Invoke-AdbCommand -Arguments @("shell", "input", "tap", $X, $Y)
        } else {
            Fail-And-ShowUsage -Message "Tap action requires -X and -Y parameters."
        }
    }
    "Swipe" {
        $required = @("X", "Y", "X2", "Y2")
        foreach ($paramName in $required) {
            if (-not $PSBoundParameters.ContainsKey($paramName)) {
                Fail-And-ShowUsage -Message "Swipe action requires -X, -Y, -X2, and -Y2 parameters."
            }
        }

        Invoke-AdbCommand -Arguments @("shell", "input", "swipe", $X, $Y, $X2, $Y2, $Duration)
    }
    "Key" {
        if ($PSBoundParameters.ContainsKey("Keycode")) {
            Invoke-AdbCommand -Arguments @("shell", "input", "keyevent", $Keycode)
        } elseif ($PSBoundParameters.ContainsKey("KeyName")) {
            Invoke-AdbCommand -Arguments @("shell", "input", "keyevent", $KeyName)
        } else {
            Fail-And-ShowUsage -Message "Key action requires either -Keycode or -KeyName."
        }
    }
    default {
        Fail-And-ShowUsage -Message "Unsupported action: $Action"
    }
}

Write-Host "Command completed successfully." -ForegroundColor Green