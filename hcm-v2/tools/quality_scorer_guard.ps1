<#
HCM · AI 信号质量评分 sidecar 主机级守护（OS 层兜底）
=================================================================
背景：
  quality_scorer.py 是独立 sidecar（只读 + 只发布 hcm:live:hexp:ai:{sym} 观测快照，
  零下单影响）。它在常驻 Python 进程里每 5s 跑一轮；进程若崩溃/被Kill需自动重拉。
  本脚本由 Windows 计划任务 HCM_AIQGuard 周期调用，职责与 bridge_boot.ps1 对称：
    1) 关键脚本缺失 → 从 _baseline 还原
    2) sidecar 不在 → 幂等拉起（launcher 用 DETACHED_PROCESS，进程树独立于本脚本）
    3) 基线维护：文件存在且能编译 → 刷新 _baseline 副本

注意（与桥栈不同）：sidecar 不连 MT5，无 Windows 会话隔离约束，可安全在 Session 0 运行；
但为了和桥栈一致的用户会话内恢复，计划任务仍用 -AtLogOn + 每 3 分钟巡检。

用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File quality_scorer_guard.ps1
    powershell ... -File quality_scorer_guard.ps1 -Snapshot   # 仅刷新基线，不拉起
#>

param(
    [switch]$Snapshot
)

$ErrorActionPreference = 'SilentlyContinue'

$TOOLS    = 'd:/HCM_ASST/hcm-v2/tools'
$BASELINE = Join-Path $TOOLS '_baseline'
# 无窗口解释器：pythonw 不弹控制台窗口，根治 guard 拉起 launcher 时反复弹窗
$PY       = 'C:\Python313\pythonw.exe'
$LAUNCHER = Join-Path $TOOLS 'quality_scorer_launcher.py'
$GUARDLOG = Join-Path $TOOLS 'quality_scorer_guard.log'

# sidecar 运行所需文件（缺失则整条链起不来）
$CRITICAL = @(
    'quality_scorer.py',
    'quality_scorer_launcher.py',
    'quality_features.py'
)

function Write-GuardLog([string]$msg) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $msg
    Add-Content -LiteralPath $GUARDLOG -Value $line -Encoding utf8
}

function Get-Sha256([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return '' }
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
}

if (-not (Test-Path -LiteralPath $BASELINE)) {
    New-Item -ItemType Directory -Path $BASELINE -Force | Out-Null
}

# ── 1) 完整性自愈 + 基线维护 ──────────────────────────────────────────
$restored = 0
foreach ($name in $CRITICAL) {
    $live = Join-Path $TOOLS $name
    $base = Join-Path $BASELINE $name

    if (-not (Test-Path -LiteralPath $live)) {
        if (Test-Path -LiteralPath $base) {
            Copy-Item -LiteralPath $base -Destination $live -Force
            Write-GuardLog ("RESTORED missing script from baseline: " + $name)
            $restored++
        }
        else {
            Write-GuardLog ("FATAL: " + $name + " missing AND no baseline copy available")
        }
        continue
    }

    if ((Get-Sha256 $live) -ne (Get-Sha256 $base)) {
        & $PY -m py_compile $live 2>$null
        if ($LASTEXITCODE -eq 0) {
            Copy-Item -LiteralPath $live -Destination $base -Force
            Write-GuardLog ("baseline refreshed: " + $name)
        }
        else {
            Write-GuardLog ("WARN: " + $name + " failed py_compile; baseline kept unchanged")
        }
    }
}

if ($Snapshot) {
    Write-GuardLog ("snapshot-only run done (restored=" + $restored + ")")
    Write-Output ("SNAPSHOT_DONE restored=" + $restored)
    return
}

# ── 2) 进程自愈：sidecar 不在则幂等拉起 ──────────────────────────────
# 注意：sidecar 现在由 pythonw.exe 运行（无窗口），探测必须查 pythonw 而非 python，
# 否则永远探测不到 → 每轮都重拉 → 进程堆积。
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
           Where-Object { $_.CommandLine -like '*quality_scorer.py*' }

if ($running) {
    Write-Output ('SIDECAR_ALIVE pid=' + ($running | Select-Object -First 1).ProcessId)
    return
}

if (-not (Test-Path -LiteralPath $LAUNCHER)) {
    Write-GuardLog 'FATAL: launcher script unavailable, cannot spawn sidecar'
    Write-Output 'LAUNCHER_MISSING'
    return
}

# 与 quality_scorer_launcher.py 一致的 DETACHED_PROCESS 拉起方式
$code = "import subprocess; subprocess.Popen([r'$PY', r'$LAUNCHER'], " +
        "creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, " +
        "stdout=open(r'$TOOLS\quality_scorer.log','a'), stderr=subprocess.STDOUT)"
& $PY -c $code

Write-GuardLog ('sidecar respawned by OS-level guard (restored=' + $restored + ')')
Write-Output 'SIDECAR_SPAWNED'
