<#
  HCM AI 质量评分 sidecar 主机级守护（OS 层兜底）
  =================================================================
  背景 / 根因
    2026-08-08 桥栈整体停摆 2 天的教训：关键进程只随 start.bat 手动启动，
    一旦该窗口关闭/机器重启且无人手动重跑，进程永久消失，且容器侧无任何手段
    能拉起 Windows 主机进程（自愈 100% 空转）。

    sidecar（quality_scorer.py，LightGBM 实时打分进程）与桥栈面临完全相同的
    风险：它只随 start.bat 手动拉起，机器重启后若无人手动运行 start.bat，
    AI 质量闸门静默失效（信号塔 _read_ai_quality 读到 lm_score=None → 裸跑 HEXP）。

  本脚本职责 = 补上 OS 层守护，由 Windows 计划任务周期（每 3 分钟）调用：
    1) 完整性自愈：关键脚本缺失 → 自动从 _baseline 还原
    2) 进程自愈  ：sidecar（pythonw.exe * quality_scorer.py）不在 → 幂等拉起
                    （launcher 自带 Global 互斥体，重复调用会立即自退，不会打架）
    3) 模型自愈  ：tools/models/ 下模型文件缺失 → 从 _baseline 还原
    4) 基线维护  ：工作文件存在且能通过编译 → 刷新 _baseline 副本

  用法
    powershell -NoProfile -ExecutionPolicy Bypass -File ai_scorer_boot.ps1
    powershell ... -File ai_scorer_boot.ps1 -Snapshot   # 仅刷新基线，不拉起
#>

param(
    [switch]$Snapshot
)

$ErrorActionPreference = 'SilentlyContinue'

$TOOLS    = 'd:\HCM_ASST\hcm-v2\tools'
$BASELINE = Join-Path $TOOLS '_baseline'
$PY       = 'C:\Python313\python.exe'
$PYW      = 'C:\Python313\pythonw.exe'
$LAUNCHER = Join-Path $TOOLS 'quality_scorer_launcher.py'
$BOOTLOG  = Join-Path $TOOLS 'ai_scorer_boot.log'

# sidecar 运行所必需的文件（缺任何一个都无法产出 AI 评分）
$CRITICAL = @(
    'quality_scorer.py',
    'quality_scorer_launcher.py',
    'quality_features.py'
)
# 固化到 tools/models/ 的模型文件（缺失则 AI 闸门降级为纯 HEXP）
$MODEL_FILES = @(
    'lgbm_quality_final.txt',
    'calib_final.pkl'
)
$MODEL_DIR = Join-Path $TOOLS 'models'

function Write-BootLog([string]$msg) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $msg
    Add-Content -LiteralPath $BOOTLOG -Value $line -Encoding utf8
}

function Get-Sha256([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return '' }
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
}

if (-not (Test-Path -LiteralPath $BASELINE)) {
    New-Item -ItemType Directory -Path $BASELINE -Force | Out-Null
}

# ── 1) 完整性自愈 + 基线维护（脚本）─────────────────────────────────────
$restored = 0
foreach ($name in $CRITICAL) {
    $live = Join-Path $TOOLS $name
    $base = Join-Path $BASELINE $name

    if (-not (Test-Path -LiteralPath $live)) {
        if (Test-Path -LiteralPath $base) {
            Copy-Item -LiteralPath $base -Destination $live -Force
            Write-BootLog ("RESTORED missing script from baseline: " + $name)
            $restored++
        }
        else {
            Write-BootLog ("FATAL: " + $name + " missing AND no baseline copy available")
        }
        continue
    }

    if ((Get-Sha256 $live) -ne (Get-Sha256 $base)) {
        & $PY -m py_compile $live 2>$null
        if ($LASTEXITCODE -eq 0) {
            Copy-Item -LiteralPath $live -Destination $base -Force
            Write-BootLog ("baseline refreshed: " + $name)
        }
        else {
            Write-BootLog ("WARN: " + $name + " failed py_compile; baseline kept unchanged")
        }
    }
}

# ── 2) 模型自愈（tools/models/ 缺失 → 从 _baseline 还原）────────────────
if (-not (Test-Path -LiteralPath $MODEL_DIR)) {
    New-Item -ItemType Directory -Path $MODEL_DIR -Force | Out-Null
}
foreach ($m in $MODEL_FILES) {
    $live = Join-Path $MODEL_DIR $m
    $base = Join-Path $BASELINE $m
    if (-not (Test-Path -LiteralPath $live)) {
        if (Test-Path -LiteralPath $base) {
            Copy-Item -LiteralPath $base -Destination $live -Force
            Write-BootLog ("RESTORED missing model from baseline: " + $m)
            $restored++
        }
        else {
            Write-BootLog ("WARN: model " + $m + " missing AND no baseline copy available → AI gate will degrade to pure HEXP")
        }
    }
    else {
        # 基线同步：models 更新后（如重新训练）刷新基线，保证可还原到最新良好版本
        if ((Get-Sha256 $live) -ne (Get-Sha256 $base)) {
            Copy-Item -LiteralPath $live -Destination $base -Force
            Write-BootLog ("model baseline refreshed: " + $m)
        }
    }
}

if ($Snapshot) {
    Write-BootLog ("snapshot-only run done (restored=" + $restored + ")")
    Write-Output ("SNAPSHOT_DONE restored=" + $restored)
    return
}

# ── 3) 进程自愈：sidecar 不在则幂等拉起；在但僵死(崩溃循环不发布快照)则重启 ──
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
           Where-Object { $_.CommandLine -like '*quality_scorer.py*' }

if ($running) {
    # 【加固·2026-08-18】健康探针：sidecar 进程"在"不代表"活"。
    # 根因：score_one 漏定义 _num → 每轮 NameError 被主循环 except 吞成
    # "scoring loop error"，进程不退出却永不发布 AI 快照（面板离线）。
    # 守护脚本若仅判"进程在"就跳过 → 永不重拉新码。现加 Redis 快照新鲜度探测：
    # hcm:live:hexp:ai:XAUUSD 的 ts 距今 > 60s（2×interval=5s，留足余量）→
    # 判定僵死，杀掉全部 sidecar 进程后走下方重拉逻辑。
    $fresh = $false
    try {
        $ping = & docker exec hcm-v2-redis-1 redis-cli GET "hcm:live:hexp:ai:XAUUSD" 2>$null
        if ($ping) {
            $ts = ([regex]'"ts":\s*([\d.]+)').Match($ping).Groups[1].Value
            if ($ts -and (([DateTimeOffset]::Now.ToUnixTimeSeconds()) - [double]$ts) -lt 60) {
                $fresh = $true
            }
        }
    }
    catch { }

    if ($fresh) {
        Write-Output ('SCORER_ALIVE pid=' + ($running | Select-Object -First 1).ProcessId)
        return
    }

    # 僵死判定：进程在但快照陈旧/缺失 → 清掉全部 sidecar 实例，下方重新拉起
    Write-BootLog ('sidecar process present but snapshot stale/missing — killing ' +
                   ($running.ProcessId -join ',') + ' for respawn')
    $running | ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch { }
    }
    Start-Sleep -Seconds 2
    # 重新探测，确保已清空
    $running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
               Where-Object { $_.CommandLine -like '*quality_scorer.py*' }
    if ($running) {
        Write-Output 'SCORER_KILL_FAILED'
        return
    }
}

if (-not (Test-Path -LiteralPath $LAUNCHER)) {
    Write-BootLog 'FATAL: launcher script unavailable, cannot spawn AI scorer'
    Write-Output 'LAUNCHER_MISSING'
    return
}

# 通过 launcher 拉起（launcher 自带互斥体 + 运行中实例探测，保证单实例）
$code = "import subprocess; subprocess.Popen([r'$PY', r'$LAUNCHER'], " +
        "creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, " +
        "stdout=open(r'" + (Join-Path $TOOLS 'ai_scorer_launcher.log') + "','a'), stderr=subprocess.STDOUT)"
& $PY -c $code

Write-BootLog ('AI scorer launcher respawned by OS-level guard (restored=' + $restored + ')')
Write-Output 'SCORER_SPAWNED'
