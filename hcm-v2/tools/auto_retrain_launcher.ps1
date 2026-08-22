# auto_retrain_launcher.ps1 — 拉起 LightGBM 自动重训守护（路径 A+B+C 常驻）。
# 与 bridge_boot.ps1 同模式：DETACHED 拉起，互斥体防重复，失败静默。
# 由 start.bat 或计划任务调用；不随 Windows 自启（需主机会话连 MT5/PG 同机）。

$ErrorActionPreference = 'SilentlyContinue'
$PS = 'C:\Python313\python.exe'
$SCRIPT = Join-Path $PSScriptRoot 'auto_retrain.py'
$LOG = Join-Path $PSScriptRoot 'auto_retrain_daemon.log'

# 互斥体：防止 launcher 自身重复拉起
$mtxName = 'Global\hcm_auto_retrain_launcher'
$mtx = New-Object System.Threading.Mutex($false, $mtxName)
if (-not $mtx.WaitOne(0)) {
    Write-Host "auto_retrain launcher already running, exit"
    exit 0
}

# 检查是否已有 python 在跑 auto_retrain.py
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine.Contains('auto_retrain.py')
}
if ($running) {
    Write-Host "auto_retrain daemon already alive pid=$($running.ProcessId), skip"
    exit 0
}

# DETACHED 拉起守护（每 24h 一轮）
$pinfo = New-Object System.Diagnostics.ProcessStartInfo
$pinfo.FileName = $PS
$pinfo.Arguments = "$SCRIPT --daemon --interval-hours 24"
$pinfo.WorkingDirectory = $PSScriptRoot
$pinfo.WindowStyle = 'Hidden'
$pinfo.CreateNoWindow = $true
$pinfo.UseShellExecute = $false
$pinfo.RedirectStandardOutput = $false
$pinfo.RedirectStandardError = $false
try {
    $p = [System.Diagnostics.Process]::Start($pinfo)
    Add-Content -Path $LOG -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [launcher] started auto_retrain daemon pid=$($p.Id)"
    Write-Host "auto_retrain daemon started pid=$($p.Id)"
} catch {
    Add-Content -Path $LOG -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [launcher] FAILED: $_"
}
