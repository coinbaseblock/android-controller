<#
.SYNOPSIS
    เพิ่มขนาด Virtual Disk ของ Docker Desktop เพื่อให้มีพื้นที่มากขึ้น

.DESCRIPTION
    Docker Desktop ใช้ virtual disk (vhdx) ที่มีขนาดจำกัด (ค่าเริ่มต้น ~64 GB)
    สคริปต์นี้จะปรับขนาดให้ใหญ่ขึ้นตามที่กำหนด

    ขั้นตอน:
    1. หยุด Docker Desktop
    2. แก้ไข settings.json เพิ่มขนาด diskSizeGiB
    3. ขยาย vhdx file ด้วย Hyper-V / diskpart
    4. เริ่ม Docker Desktop ใหม่

.PARAMETER SizeGB
    ขนาดดิสก์ใหม่ (GB) ค่าเริ่มต้น 256 GB

.EXAMPLE
    .\increase-docker-disk.ps1
    .\increase-docker-disk.ps1 -SizeGB 512
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $false, HelpMessage = 'ขนาดดิสก์ใหม่ (GB)')]
    [ValidateRange(64, 2048)]
    [int]$SizeGB = 256
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host "=== Docker Desktop Disk Size Increase ===" -ForegroundColor Cyan
Write-Host "ขนาดใหม่: $SizeGB GB" -ForegroundColor Yellow
Write-Host ""

# ---- 1. ค้นหา Docker Desktop settings ----
$settingsPath = Join-Path $env:APPDATA "Docker\settings.json"
if (-not (Test-Path $settingsPath)) {
    # ลองอีก path
    $settingsPath = Join-Path $env:LOCALAPPDATA "Docker\settings.json"
}
if (-not (Test-Path $settingsPath)) {
    throw "ไม่พบ Docker Desktop settings.json ที่ $settingsPath"
}

Write-Host "[1/4] พบ settings: $settingsPath" -ForegroundColor Green

# ---- 2. อ่านและแก้ไข settings ----
$settings = Get-Content $settingsPath -Raw | ConvertFrom-Json

$currentSize = if ($settings.PSObject.Properties['diskSizeGiB']) { $settings.diskSizeGiB } else { 64 }
Write-Host "       ขนาดปัจจุบัน: $currentSize GB" -ForegroundColor Gray

if ($SizeGB -le $currentSize) {
    Write-Host "ขนาดใหม่ ($SizeGB GB) ไม่มากกว่าขนาดปัจจุบัน ($currentSize GB)" -ForegroundColor Yellow
    Write-Host "ใช้ -SizeGB ที่มากกว่า $currentSize" -ForegroundColor Yellow
    return
}

if (-not $PSCmdlet.ShouldProcess("Docker Desktop disk", "เพิ่มขนาดจาก $currentSize GB เป็น $SizeGB GB")) {
    return
}

# ---- 3. หยุด Docker Desktop ----
Write-Host "[2/4] กำลังหยุด Docker Desktop..." -ForegroundColor Yellow
$dockerProcess = Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue
if ($dockerProcess) {
    & "C:\Program Files\Docker\Docker\Docker Desktop.exe" -Quit 2>$null
    Start-Sleep -Seconds 5
    # รอให้ process หยุดจริง
    $timeout = 60
    $elapsed = 0
    while ((Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue) -and $elapsed -lt $timeout) {
        Start-Sleep -Seconds 2
        $elapsed += 2
    }
    if ($elapsed -ge $timeout) {
        Write-Host "  Docker Desktop ไม่ยอมหยุด ลอง force stop..." -ForegroundColor Red
        Stop-Process -Name "Docker Desktop" -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }
}
Write-Host "       Docker Desktop หยุดแล้ว" -ForegroundColor Green

# ---- 4. แก้ settings.json ----
Write-Host "[3/4] กำลังแก้ไข settings.json..." -ForegroundColor Yellow

# Backup settings
$backupPath = "$settingsPath.backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
Copy-Item $settingsPath $backupPath
Write-Host "       Backup: $backupPath" -ForegroundColor Gray

if ($settings.PSObject.Properties['diskSizeGiB']) {
    $settings.diskSizeGiB = $SizeGB
} else {
    $settings | Add-Member -NotePropertyName 'diskSizeGiB' -NotePropertyValue $SizeGB
}

$settings | ConvertTo-Json -Depth 10 | Set-Content $settingsPath -Encoding UTF8
Write-Host "       settings.json อัพเดทเป็น $SizeGB GB" -ForegroundColor Green

# ---- 5. ขยาย VHDX file (ถ้าใช้ WSL2 backend) ----
$vhdxPaths = @(
    "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx",
    "$env:LOCALAPPDATA\Docker\wsl\data\ext4.vhdx",
    "$env:APPDATA\Docker\wsl\disk\docker_data.vhdx"
)

$vhdxFile = $null
foreach ($p in $vhdxPaths) {
    if (Test-Path $p) {
        $vhdxFile = $p
        break
    }
}

if ($vhdxFile) {
    Write-Host "[3.5] พบ VHDX: $vhdxFile" -ForegroundColor Yellow
    Write-Host "       กำลังขยาย VHDX เป็น $SizeGB GB..." -ForegroundColor Yellow

    try {
        # ใช้ diskpart เพื่อขยาย vhdx
        $sizeInMB = $SizeGB * 1024
        $diskpartScript = @"
select vdisk file="$vhdxFile"
expand vdisk maximum=$sizeInMB
"@
        $tempScript = [System.IO.Path]::GetTempFileName()
        $diskpartScript | Set-Content $tempScript -Encoding ASCII

        $result = Start-Process -FilePath "diskpart" -ArgumentList "/s `"$tempScript`"" `
            -Wait -PassThru -Verb RunAs -WindowStyle Hidden 2>$null

        Remove-Item $tempScript -ErrorAction SilentlyContinue

        if ($result.ExitCode -eq 0) {
            Write-Host "       VHDX ขยายสำเร็จ" -ForegroundColor Green
        } else {
            Write-Host "       diskpart exit code: $($result.ExitCode) — อาจต้อง run as Administrator" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "       ไม่สามารถขยาย VHDX อัตโนมัติ: $_" -ForegroundColor Yellow
        Write-Host "       Docker Desktop จะขยายเองตอนเปิดใหม่ตาม settings.json" -ForegroundColor Yellow
    }
} else {
    Write-Host "[3.5] ไม่พบ VHDX file — Docker Desktop จะจัดการเองตาม settings.json" -ForegroundColor Yellow
}

# ---- 6. เริ่ม Docker Desktop ใหม่ ----
Write-Host "[4/4] กำลังเปิด Docker Desktop..." -ForegroundColor Yellow
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"

Write-Host ""
Write-Host "=== เสร็จสิ้น ===" -ForegroundColor Green
Write-Host "Docker Desktop กำลังเริ่มใหม่ด้วยขนาดดิสก์ $SizeGB GB" -ForegroundColor Green
Write-Host "รอ 30-60 วินาทีให้ Docker พร้อมใช้งาน แล้วรัน:" -ForegroundColor Gray
Write-Host "  docker system df          # ดูพื้นที่ที่ใช้" -ForegroundColor Gray
Write-Host "  docker system prune -f    # ลบ image/container เก่า (ถ้าต้องการ)" -ForegroundColor Gray
Write-Host ""
Write-Host "จากนั้นรัน docker-compose ใหม่:" -ForegroundColor Gray
Write-Host "  docker-compose down && docker-compose up -d --build" -ForegroundColor Gray
