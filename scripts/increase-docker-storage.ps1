<#
  increase-docker-storage.ps1
  สคริปต์สำหรับเพิ่มขนาด Virtual Disk ของ Docker Desktop บน Windows

  วิธีใช้ (รันใน PowerShell ด้วยสิทธิ์ Administrator):
    .\scripts\increase-docker-storage.ps1 -SizeGB 128

  ค่า default: 256 GB (Docker Desktop default คือ 64 GB)
#>

param(
    [int]$SizeGB = 256
)

$ErrorActionPreference = "Stop"

Write-Host "=== Docker Desktop Virtual Disk Size Increase ===" -ForegroundColor Cyan
Write-Host ""

# --- 1. หา Docker Desktop settings.json ---
$settingsPath = "$env:APPDATA\Docker\settings.json"
if (-Not (Test-Path $settingsPath)) {
    Write-Host "[ERROR] ไม่พบไฟล์ $settingsPath" -ForegroundColor Red
    Write-Host "กรุณาติดตั้ง Docker Desktop ก่อน" -ForegroundColor Red
    exit 1
}

# --- 2. อ่าน settings ปัจจุบัน ---
$settings = Get-Content $settingsPath -Raw | ConvertFrom-Json
$currentSizeMB = if ($settings.PSObject.Properties["diskSizeMB"]) { $settings.diskSizeMB } else { 65536 }
$currentSizeGB = [math]::Round($currentSizeMB / 1024, 1)

Write-Host "ขนาดปัจจุบัน : $currentSizeGB GB ($currentSizeMB MB)" -ForegroundColor Yellow
Write-Host "ขนาดใหม่ที่ต้องการ: $SizeGB GB" -ForegroundColor Green
Write-Host ""

if ($SizeGB -le $currentSizeGB) {
    Write-Host "[SKIP] ขนาดใหม่ไม่มากกว่าขนาดปัจจุบัน ไม่ต้องเปลี่ยน" -ForegroundColor Yellow
    exit 0
}

# --- 3. หยุด Docker Desktop ---
Write-Host "กำลังหยุด Docker Desktop..." -ForegroundColor Yellow
$dockerProcess = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if ($dockerProcess) {
    & "C:\Program Files\Docker\Docker\DockerCli.exe" -SwitchDaemon 2>$null
    Stop-Process -Name "Docker Desktop" -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 5
}

# --- 4. อัปเดต settings.json ---
$newSizeMB = $SizeGB * 1024
$settings | Add-Member -MemberType NoteProperty -Name "diskSizeMB" -Value $newSizeMB -Force
$settings | ConvertTo-Json -Depth 10 | Set-Content $settingsPath -Encoding UTF8

Write-Host "อัปเดต settings.json: diskSizeMB = $newSizeMB ($SizeGB GB)" -ForegroundColor Green

# --- 5. Resize VHDX (WSL2 backend) ---
$vhdxPath = "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx"
if (-Not (Test-Path $vhdxPath)) {
    # ลองหาจาก distro data
    $vhdxPath = "$env:LOCALAPPDATA\Docker\wsl\data\ext4.vhdx"
}

if (Test-Path $vhdxPath) {
    Write-Host ""
    Write-Host "กำลัง resize VHDX: $vhdxPath" -ForegroundColor Yellow
    Write-Host "ขนาดใหม่: $SizeGB GB" -ForegroundColor Yellow

    # ใช้ diskpart เพื่อ expand VHDX
    $diskpartScript = @"
select vdisk file="$vhdxPath"
expand vdisk maximum=$($SizeGB * 1024)
"@
    $tempFile = [System.IO.Path]::GetTempFileName()
    $diskpartScript | Set-Content $tempFile -Encoding ASCII

    try {
        $result = & diskpart /s $tempFile 2>&1
        Write-Host $result -ForegroundColor Gray
        Write-Host ""
        Write-Host "[OK] VHDX expanded สำเร็จ" -ForegroundColor Green
    } catch {
        Write-Host "[WARN] ไม่สามารถ expand VHDX ได้โดยตรง: $_" -ForegroundColor Yellow
        Write-Host "Docker Desktop จะ resize เองเมื่อเริ่มใหม่" -ForegroundColor Yellow
    } finally {
        Remove-Item $tempFile -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "[INFO] ไม่พบ VHDX file — Docker Desktop จะจัดการเองเมื่อเริ่มใหม่" -ForegroundColor Yellow
}

# --- 6. เริ่ม Docker Desktop ใหม่ ---
Write-Host ""
Write-Host "กำลังเริ่ม Docker Desktop ใหม่..." -ForegroundColor Yellow
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"

Write-Host ""
Write-Host "=== เสร็จสิ้น ===" -ForegroundColor Cyan
Write-Host "Docker Desktop virtual disk ถูกเพิ่มเป็น $SizeGB GB" -ForegroundColor Green
Write-Host "รอให้ Docker Desktop เริ่มต้นเสร็จ แล้วรัน: docker system df" -ForegroundColor Yellow
Write-Host ""
Write-Host "ถ้าต้องการ prune ข้อมูลเก่าเพิ่มเติม:" -ForegroundColor Yellow
Write-Host "  docker system prune -a --volumes" -ForegroundColor White
Write-Host "  docker builder prune -a" -ForegroundColor White
