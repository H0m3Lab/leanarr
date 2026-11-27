<#
.SYNOPSIS
  Installs the Leanarr remote worker on a Windows PC with an NVIDIA GPU (by H0m3Lab).

.DESCRIPTION
  Run once in an ELEVATED PowerShell (Run as administrator):
      powershell -ExecutionPolicy Bypass -File .\Setup-LeanarrWorker.ps1 -Token <worker token>

  What it does:
    1. Checks for an NVIDIA GPU (nvidia-smi).
    2. Installs Python 3.12 with winget if it's missing.
    3. Downloads ffmpeg (gyan.dev "release essentials" build, has NVENC) and checks its SHA-256.
    4. Downloads the worker program from the Leanarr server and tests that ffmpeg can use NVENC.
    5. Creates the scheduled task "Leanarr Worker": runs as SYSTEM at startup (no login needed),
       restarts if it stops, and fetches the newest worker from the server each time it starts.

  Everything lives in C:\LeanarrWorker (log: C:\LeanarrWorker\worker.log).
  The worker only talks to the Leanarr server: it downloads one video at a time, encodes it on the
  GPU and uploads the result. It never touches the media library or any network share.
  When jobs run is set on the Leanarr settings page ("Remote worker hours").

  Remove:  Unregister-ScheduledTask -TaskName "Leanarr Worker" -Confirm:$false ; Remove-Item C:\LeanarrWorker -Recurse
#>
param(
    [Parameter(Mandatory)] [string]$Token,
    [Parameter(Mandatory)] [string]$Server,   # e.g. http://your-leanarr-host:8282 (the /worker-setup page fills it in)
    [ValidateSet('nvenc', 'x265', 'uhd')] [string]$Kind = 'nvenc',   # uhd = 4K worker (x265 + VMAF, no GPU needed)
    [string]$Dir     = 'C:\LeanarrWorker'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Please run this in PowerShell opened with "Run as administrator".'
}
# a 4K worker runs next to a GPU worker on the same PC: own task, work folder, log and name
$sfx      = if ($Kind -eq 'uhd') { '-4k' } else { '' }
$TaskName = if ($Kind -eq 'uhd') { 'Leanarr 4K Worker' } else { 'Leanarr Worker' }
$Work     = "$Dir\work$sfx"
$LogFile  = "$Dir\worker$sfx.log"
$Name     = if ($Kind -eq 'uhd') { "$env:COMPUTERNAME-4K" } else { $env:COMPUTERNAME }
New-Item -ItemType Directory -Force -Path $Dir, $Work | Out-Null
if ($Kind -eq 'uhd') {
    $free = (Get-PSDrive ($Dir.Substring(0, 1))).Free / 1GB
    Write-Host ("Free space on {0}: {1:N0} GB (a 4K movie needs up to ~2x its size while it is worked on)" -f $Dir.Substring(0, 2), $free)
    if ($free -lt 150) { Write-Warning 'Less than 150 GB free: big 4K remuxes will be refused until there is more room.' }
}

# 1. GPU
if ($Kind -eq 'nvenc') {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) { throw 'nvidia-smi not found: is the NVIDIA driver installed?' }
    Write-Host ("GPU: " + ((& nvidia-smi --query-gpu=name,driver_version --format=csv,noheader) -join ', '))
}

# 2. Python
function Find-Python {
    foreach ($c in @("$env:ProgramFiles\Python312\python.exe", "$env:ProgramFiles\Python313\python.exe")) {
        if (Test-Path $c) { return $c }
    }
    $py = Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch 'WindowsApps' } | Select-Object -First 1
    if ($py) { return $py.Source }
    return $null
}
$python = Find-Python
if (-not $python) {
    Write-Host 'Installing Python 3.12 (winget)...'
    winget install --id Python.Python.3.12 -e --scope machine --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $python = Find-Python
    if (-not $python) { throw 'Python install failed; install Python 3.12 manually and run this again.' }
}
Write-Host "Python: $python"

# 3. ffmpeg (verified download)
$ffmpeg = "$Dir\ffmpeg\bin\ffmpeg.exe"
if ($Kind -eq 'uhd') {
    # 4K worker: BtbN's GPL build (libx265 + libvmaf with the 4K model built in), checksum from the same release
    $ffmpeg = "$Dir\ffmpeg-uhd\bin\ffmpeg.exe"
    if (-not (Test-Path $ffmpeg)) {
        $rel  = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest'
        $file = 'ffmpeg-n8.1-latest-win64-gpl-8.1.zip'
        Write-Host "Downloading ffmpeg ($rel/$file)..."
        Invoke-WebRequest "$rel/$file" -OutFile "$Dir\ffmpeg-uhd.zip" -UseBasicParsing
        $sums = (Invoke-WebRequest "$rel/checksums.sha256" -UseBasicParsing).Content
        if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }   # GitHub serves it as binary
        $line = ($sums -split "`n") | Where-Object { $_ -match [regex]::Escape($file) } | Select-Object -First 1
        if (-not $line) { Remove-Item "$Dir\ffmpeg-uhd.zip"; throw "no checksum for $file" }
        $want = ($line.Trim() -split '\s+')[0].ToLower()
        $got = (Get-FileHash "$Dir\ffmpeg-uhd.zip" -Algorithm SHA256).Hash.ToLower()
        if ($got -ne $want) { Remove-Item "$Dir\ffmpeg-uhd.zip"; throw "ffmpeg checksum mismatch ($got vs $want)" }
        Expand-Archive "$Dir\ffmpeg-uhd.zip" "$Dir\ffmpeg-uhd-tmp" -Force
        $inner = Get-ChildItem "$Dir\ffmpeg-uhd-tmp" -Directory | Select-Object -First 1
        if (Test-Path "$Dir\ffmpeg-uhd") { Remove-Item "$Dir\ffmpeg-uhd" -Recurse -Force }
        Move-Item $inner.FullName "$Dir\ffmpeg-uhd"
        Remove-Item "$Dir\ffmpeg-uhd-tmp", "$Dir\ffmpeg-uhd.zip" -Recurse -Force
    }
}
if (-not (Test-Path $ffmpeg)) {
    $base = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
    Write-Host "Downloading ffmpeg ($base)..."
    Invoke-WebRequest $base -OutFile "$Dir\ffmpeg.zip" -UseBasicParsing
    $want = ((Invoke-WebRequest "$base.sha256" -UseBasicParsing).Content -split '\s+')[0].Trim().ToLower()
    $got = (Get-FileHash "$Dir\ffmpeg.zip" -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $want) { Remove-Item "$Dir\ffmpeg.zip"; throw "ffmpeg checksum mismatch ($got vs $want)" }
    Expand-Archive "$Dir\ffmpeg.zip" "$Dir\ffmpeg-tmp" -Force
    $inner = Get-ChildItem "$Dir\ffmpeg-tmp" -Directory | Select-Object -First 1
    if (Test-Path "$Dir\ffmpeg") { Remove-Item "$Dir\ffmpeg" -Recurse -Force }
    Move-Item $inner.FullName "$Dir\ffmpeg"
    Remove-Item "$Dir\ffmpeg-tmp", "$Dir\ffmpeg.zip" -Recurse -Force
}
Write-Host ("ffmpeg: " + ((& $ffmpeg -hide_banner -version)[0]))
if ($Kind -eq 'nvenc' -and -not ((& $ffmpeg -hide_banner -encoders) -match 'hevc_nvenc')) {
    throw 'This ffmpeg build has no hevc_nvenc encoder.'
}

# 4. worker program from the server + a real NVENC test (2 seconds of test video)
Invoke-WebRequest "$Server/download/leanarr_worker.py" -OutFile "$Dir\leanarr_worker.py" -UseBasicParsing
Write-Host "Worker downloaded from $Server"
if ($Kind -eq 'uhd') {
    # the same check the worker runs at every start: an x265 encode + a VMAF measurement with the 4K model
    & $python "$Dir\leanarr_worker.py" --server $Server --token $Token --kind uhd --ffmpeg $ffmpeg --work-dir $Work --selftest
    if ($LASTEXITCODE -ne 0) { throw '4K self-test failed (see the message above).' }
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    Write-Host ("CPU: {0} ({1} cores / {2} threads)" -f $cpu.Name.Trim(), $cpu.NumberOfCores, $cpu.NumberOfLogicalProcessors)
}
if ($Kind -eq 'nvenc') {
    & $ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=size=1920x1080:rate=24 -t 2 -c:v hevc_nvenc -preset p7 -cq 24 -y "$Work\nvenc-test.mkv"
    if ($LASTEXITCODE -ne 0) { throw 'NVENC test encode failed (driver too old or GPU busy?).' }
    Remove-Item "$Work\nvenc-test.mkv" -Force
    Write-Host 'NVENC test encode: OK'
}

# 5. runner (self-updating) + scheduled task
$runner = @"
# Leanarr worker runner (created by Setup-LeanarrWorker.ps1). Fetches the newest worker, then runs it.
try { Invoke-WebRequest '$Server/download/leanarr_worker.py' -OutFile '$Dir\leanarr_worker.py.new' -UseBasicParsing -TimeoutSec 30
      Move-Item '$Dir\leanarr_worker.py.new' '$Dir\leanarr_worker.py' -Force } catch { }
& '$python' '$Dir\leanarr_worker.py' --server '$Server' --token '$Token' --kind '$Kind' --name '$Name' ``
    --ffmpeg '$ffmpeg' --work-dir '$Work' --log-file '$LogFile'
"@
Set-Content -Path "$Dir\run$sfx.ps1" -Value $runner -Encoding UTF8
# the token is in run*.ps1: only SYSTEM and Administrators may read it
icacls $Dir /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' | Out-Null

$action   = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dir\run$sfx.ps1`""
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep 8
Write-Host ''
Write-Host "$TaskName installed and started." -ForegroundColor Green
if (Test-Path $LogFile) { Get-Content $LogFile -Tail 3 }
Write-Host "Log: $LogFile   Status page: $Server"
