<#
  HCM TimesFM 每日 T+1 抽取调度器 — 主机级守护（OS 层兜底）
  =================================================================
  背景 / 根因
    与 ai_scorer_boot.ps1 完全同源的风险：关键进程若只随 start.bat 手动启动，
    机器重启 / 会话丢失且无人手动重跑时进程永久消失，而容器侧无任何手段能拉起
    Windows 宿主进程（自愈 100% 空转）。2026-08-29 曾发生整栈停机 1h22m，
    正是因此无人值守。

    本调度器是「B 方案：先跑架构、再积累样本」的产能来源：它停摆 = 样本停止积累 =
    13 天后的 §7.2 重测没有数据。故必须纳入 OS 层守护。

  本脚本职责（由 Windows 计划任务每 10 分钟 + 登录时调用）：
    1) 完整性自愈：关键脚本缺失 → 从 _baseline 还原
    2) 进程自愈  ：调度器不在 → 经 launcher 幂等拉起
                   （launcher 自带 Global 互斥体，重复调用立即自退，不会打架）
    3) 基线维护  ：工作文件存在且能 py_compile → 刷新 _baseline 副本

  与 ai_scorer_boot.ps1 的差异（重要，勿照搬）：
    AI 评分 sidecar 是持续发布者，可用「Redis 快照新鲜度」判定僵死；
    本调度器是**每日批处理**，合法状态下就是长时间空闲（IDLE 心跳），
    故不能用"心跳新鲜度"判僵死，只判进程是否存在。

  用法
    powershell -NoProfile -ExecutionPolicy Bypass -File timesfm_daily_boot.ps1
    powershell ... -File timesfm_daily_boot.ps1 -Snapshot   # 仅刷新基线，不拉起
#>

param(
    [switch]$Snapshot
)

$ErrorActionPreference = 'SilentlyContinue'

$TOOLS    = 'd:\HCM_ASST\hcm-v2\tools'
$BASELINE = Join-Path $TOOLS '_baseline'
# 解释器必须用 .venv_timesfm（装了 torch / timesfm / psycopg2）
$PY       = 'D:\.venv_timesfm\Scripts\python.exe'
$PYW      = 'D:\.venv_timesfm\Scripts\pythonw.exe'
$LAUNCHER = Join-Path $TOOLS 'timesfm_daily_launcher.py'
$BOOTLOG  = Join-Path $TOOLS 'timesfm_daily_boot.log'

# 调度器运行所必需的文件
$CRITICAL = @(
    'timesfm_daily_scheduler.py',
    'timesfm_daily_launcher.py',
    'timesfm_features.py'
)
# 特征一致性所依赖的产物（PCA 变换 + 检索库缓存）
# 注意：tmf_hist_lib_v1.npz 是**增量一致性**的关键状态，缺失会导致
# tmf_hist_sim 退回"每批从空库起步"的旧行为，使新旧样本分布不可比。
$MODEL_FILES = @(
    'tmf_pca_v1.pkl',
    'tmf_hist_lib_v1.npz'
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

# ── 2) 产物自愈（PCA / 检索库缓存缺失 → 从 _baseline 还原）────────────────
if (-not (Test-Path -LiteralPath $MODEL_DIR)) {
    New-Item -ItemType Directory -Path $MODEL_DIR -Force | Out-Null
}
foreach ($m in $MODEL_FILES) {
    $live = Join-Path $MODEL_DIR $m
    $base = Join-Path $BASELINE $m
    if (-not (Test-Path -LiteralPath $live)) {
        if (Test-Path -LiteralPath $base) {
            Copy-Item -LiteralPath $base -Destination $live -Force
            Write-BootLog ("RESTORED missing artifact from baseline: " + $m)
            $restored++
        }
        else {
            # 首次部署时尚未生成 lib 缓存属预期，不判 FATAL
            Write-BootLog ("WARN: artifact " + $m + " missing AND no baseline copy")
        }
    }
    else {
        if ((Get-Sha256 $live) -ne (Get-Sha256 $base)) {
            Copy-Item -LiteralPath $live -Destination $base -Force
            Write-BootLog ("artifact baseline refreshed: " + $m)
        }
    }
}

if ($Snapshot) {
    Write-BootLog ("snapshot-only run done (restored=" + $restored + ")")
    Write-Output ("SNAPSHOT_DONE restored=" + $restored)
    return
}

# ── 3) 进程自愈：调度器不在则幂等拉起 ────────────────────────────────────
# 注意：不按进程名过滤。本 venv 的 python/pythonw 是 shim（uv 风格），会再 exec
# 出真正的解释器子进程（实测 pythonw.exe -> python.exe），按名过滤会漏判。
# 且必须排除 powershell/wscript/cmd/conhost 等调用者，否则本脚本派生的
# PowerShell（其命令行含同样的模式串）会把自己算成"调度器已在跑"。
$running = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like '*timesfm_daily_scheduler.py*' -and
    $_.Name -notmatch '^(powershell|pwsh|wscript|cscript|cmd|conhost)\.exe$'
}

if ($running) {
    Write-Output ('SCHED_ALIVE pid=' + ($running | Select-Object -First 1).ProcessId)

    # 可观测性：把 Redis 心跳打到 boot 日志，便于事后判断"活着但一直失败"
    try {
        $hb = & docker exec hcm-v2-redis-1 redis-cli GET 'hcm:ai:timesfm:daily' 2>$null
        if ($hb) { Write-BootLog ('heartbeat=' + $hb) }
    }
    catch { }
    return
}

if (-not (Test-Path -LiteralPath $LAUNCHER)) {
    Write-BootLog 'FATAL: launcher script unavailable, cannot spawn TimesFM daily scheduler'
    Write-Output 'LAUNCHER_MISSING'
    return
}

$code = "import subprocess; subprocess.Popen([r'$PYW', r'$LAUNCHER'], " +
        "creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, " +
        "stdout=open(r'" + (Join-Path $TOOLS 'timesfm_daily_launcher.log') + "','a'), stderr=subprocess.STDOUT)"
& $PY -c $code

Write-BootLog ('TimesFM daily scheduler launcher respawned by OS-level guard (restored=' + $restored + ')')
Write-Output 'SCHED_SPAWNED'
