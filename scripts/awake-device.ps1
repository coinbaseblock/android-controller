param(
    [Parameter(Mandatory = $false)]
    [string]$Device,

    [Parameter(Mandatory = $false)]
    [switch]$Disable,

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

if ($VerboseLog) {
    Write-Host "Using repository root: $repoRoot"
}

$deviceArg = if ($Device) { $Device } else { '' }
$disableFlag = if ($Disable) { '1' } else { '0' }

$innerScript = @'
set -eu
set -o pipefail 2>/dev/null || true

if [ -z "${DEVICE_ARG:-}" ]; then
  DEVICE_ARG=$(adb devices | awk '$2 == "device" {print $1; exit}')
fi

if [ -z "${DEVICE_ARG:-}" ]; then
  echo "[error] ไม่พบอุปกรณ์ที่สถานะพร้อมใช้งาน (device). ใช้พารามิเตอร์ --device <serial|ip:port>." >&2
  exit 1
fi

if ! adb devices | awk '$2 == "device" {print $1}' | grep -qx "$DEVICE_ARG"; then
  case "$DEVICE_ARG" in
    *:*)
      echo "[info] เชื่อมต่อไปยัง $DEVICE_ARG ..." >&2
      adb connect "$DEVICE_ARG" || true
      ;;
  esac
fi

if ! adb devices | awk '$2 == "device" {print $1}' | grep -qx "$DEVICE_ARG"; then
  echo "[error] อุปกรณ์ $DEVICE_ARG ยังไม่พร้อม (status != device)." >&2
  exit 1
fi

if [ "${AWAKE_DISABLE:-0}" = "1" ]; then
  echo "[info] ปิดโหมดหน้าจอติดตลอดบน $DEVICE_ARG" >&2
  adb -s "$DEVICE_ARG" shell svc power stayon false || true
  adb -s "$DEVICE_ARG" shell settings delete global stay_on_while_plugged_in || true
  exit 0
fi

echo "[info] เปิดโหมดหน้าจอติดตลอดบน $DEVICE_ARG" >&2
adb -s "$DEVICE_ARG" shell svc power stayon true
adb -s "$DEVICE_ARG" shell settings put global stay_on_while_plugged_in 3 >/dev/null 2>&1 || true

# ปลุกหน้าจอด้วยคีย์ WAKEUP; fallback ไปที่ปุ่ม Power หากไม่รองรับ
if ! adb -s "$DEVICE_ARG" shell input keyevent 224 >/dev/null 2>&1; then
  adb -s "$DEVICE_ARG" shell input keyevent 26 >/dev/null 2>&1 || true
fi

echo "[info] หน้าจอถูกปลุกแล้ว" >&2
'@

$innerScript = $innerScript.Replace("`r`n", "`n").Replace("`r", "`n")
$innerScriptBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($innerScript))

if ($VerboseLog) {
    Write-Host "ADB inner script:" -ForegroundColor Cyan
    Write-Host $innerScript
    Write-Host "Passing device argument to container: '$deviceArg'"
    Write-Host "Disable flag: $disableFlag"
    Write-Host "Encoded inner script length: $($innerScriptBase64.Length) characters"
}

Push-Location -LiteralPath $repoRoot
try {
    $composeArgs = @(
        'exec', '-T',
        '--env', "DEVICE_ARG=$deviceArg",
        '--env', "ADB_INNER_SCRIPT=$innerScriptBase64",
        '--env', "AWAKE_DISABLE=$disableFlag",
        'controller', 'bash', '-lc',
        'printf ''%s'' "$ADB_INNER_SCRIPT" | base64 -d | bash'
    )

    Invoke-DockerCompose -Arguments $composeArgs -VerboseLog:$VerboseLog
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Docker Compose command exited with code $exitCode"
    }
}
finally {
    Pop-Location
}

if ($Disable) {
    Write-Host "✅ Stay-awake mode disabled on device"
} else {
    Write-Host "✅ Device awakened and set to stay awake"
}