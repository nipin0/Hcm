# HCM-V2 全备份宿主脚本（由 mt5_bridge 监听 hcm:backup:trigger 后 Popen 调用）
# 用法: powershell -ExecutionPolicy Bypass -File backup_hcm.ps1 -BackupRoot D:\HCM_ASST\backup
param(
    [string]$BackupRoot = "D:\HCM_ASST\backup"
)

$ErrorActionPreference = "Stop"
$PY = "C:\Python313\python.exe"
$SCRIPT = "D:\HCM_ASST\backup_hcm.py"

try {
    if (-not (Test-Path $SCRIPT)) {
        throw "backup script not found: $SCRIPT"
    }
    # 调用 Python 核心（Python 内部负责写 running/done/failed 状态）
    & $PY $SCRIPT
    exit $LASTEXITCODE
}
catch {
    # 异常时尝试回报 failed 状态
    try {
        $msg = $_.Exception.Message -replace '"', "'"
        $payload = "{`"status`":`"failed`",`"error`":`"$msg`",`"finished_at`":`"$(Get-Date -Format o)`"}"
        docker exec hcm-v2-redis-1 redis-cli SET hcm:backup:status $payload EX 3600 | Out-Null
    } catch {}
    Write-Error $_
    exit 1
}
