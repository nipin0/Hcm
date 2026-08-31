# 停掉旧的 signal_tower（main.py）进程
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*main.py*' } | ForEach-Object {
    Write-Host "KILL PID=$($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force
}
Start-Sleep -Seconds 2
# 确认端口 8002 释放
$port = (Get-NetTCPConnection -LocalPort 8002 -ErrorAction SilentlyContinue)
if ($port) { Write-Host "WARN: 8002 still held by $($port.OwningProcess)" } else { Write-Host "8002 free" }
