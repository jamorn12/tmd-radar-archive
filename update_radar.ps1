# กำหนดโฟลเดอร์ที่เก็บเรดาร์
$folderPath = "docs/nowcast/PHS"

if (-not (Test-Path $folderPath)) {
    Write-Host "❌ ไม่พบโฟลเดอร์ $folderPath กรุณารันสคริปต์ใน root ของโปรเจกต์" -ForegroundColor Red
    exit
}

# ค้นหาไฟล์ภาพ .png หรือ .jpg ที่ไม่ใช่ไฟล์ขยะ
$files = Get-ChildItem -Path $folderPath -Filter "PHS_*.*" | 
         Where-Object { $_.Extension -in ".png", ".jpg" -and $_.Name -notlike "*icon*" -and $_.Name -notlike "*temp*" } | 
         Sort-Object Name

if ($files.Count -eq 0) {
    Write-Host "❌ ไม่พบไฟล์ภาพเรดาร์ในโฟลเดอร์ $folderPath" -ForegroundColor Red
    exit
}

$obsFrames = @()
foreach ($file in $files) {
    $filename = $file.Name
    try {
        $parts = $filename.Split("_")
        $dateStr = $parts[1]
        $timeStr = $parts[2].Replace("Z", "").Split(".")[0]
        
        $year = [int]$dateStr.Substring(0,4)
        $month = [int]$dateStr.Substring(4,2)
        $day = [int]$dateStr.Substring(6,2)
        $hour = [int]$timeStr.Substring(0,2)
        $min = [int]$timeStr.Substring(2,2)
        
        $dt = Get-Date -Year $year -Month $month -Day $day -Hour $hour -Minute $min -Second 0
        $dto = [DateTimeOffset]($dt.ToUniversalTime())
        $timestamp = [int]$dto.ToUnixTimeSeconds()

        $obsFrames += [PSCustomObject]@{
            t = $timestamp
            url = $filename
        }
    } catch {
        Write-Host "⚠️ ข้ามไฟล์ $filename เนื่องจากแปลงเวลาไม่ได้" -ForegroundColor Yellow
    }
}

if ($obsFrames.Count -eq 0) {
    Write-Host "❌ ไม่สามารถสกัด Timestamp จากชื่อไฟล์ได้เลย" -ForegroundColor Red
    exit
}

# เรียงลำดับและเลือก 6 เฟรมล่าสุดมาทำเป็นอดีต + ปัจจุบัน
$recentObs = $obsFrames | Sort-Object t | Select-Object -Last 6
$totalObs = $recentObs.Count
$frames = @()

for ($i = 0; $i -lt $totalObs; $i++) {
    $offset = ($i - ($totalObs - 1)) * 15
    $frames += [PSCustomObject]@{
        t = $recentObs[$i].t
        url = $recentObs[$i].url
        offset_min = $offset
        kind = "obs"
    }
}

# สร้างเฟรมพยากรณ์ Nowcast ล่วงหน้า 4 เฟรม (+15, +30, +45, +60 นาที)
$latestT = $recentObs[-1].t
$latestUrl = $recentObs[-1].url
for ($step = 1; $step -le 4; $step++) {
    $futureOffset = $step * 15
    $futureT = $latestT + ($futureOffset * 60)
    $frames += [PSCustomObject]@{
        t = $futureT
        url = $latestUrl
        offset_min = $futureOffset
        kind = "nowcast"
    }
}

$manifest = [PSCustomObject]@{
    projection = "+proj=laea +lat_0=16.77 +lon_0=100.22 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    grid = @(160, 160)
    kmperpixel = 3.0
    motion = [PSCustomObject]@{
        speed_kmh = 15.0
        direction_deg = 270
    }
    frames = $frames
}

$jsonOutput = $manifest | ConvertTo-Json -Depth 10
$manifestPath = Join-Path $folderPath "latest.json"
Set-Content -Path $manifestPath -Value $jsonOutput -Encoding utf8

Write-Host "✅ สร้าง Manifest สำเร็จ! (รวม $($frames.Count) เฟรม)" -ForegroundColor Green

# อัปเดตขึ้น GitHub อัตโนมัติ
git add $manifestPath
git commit -m "fix: auto-update latest.json via PowerShell script"
git push origin main

Write-Host "🚀 พุชขึ้น GitHub สำเร็จเรียบร้อย ลุยได้เลย!" -ForegroundColor Green
