<#
HCM 桥栈主机级守护（OS 层兜底）
=================================================================
背景 / 根因
  2026-08-08 15:14 桥栈整体停摆 2 天，事后定位为两条根因叠加：
    ① tools\ 下的 bridge_watchdog_launcher.py / bridge_watchdog_pa.py /
       position_sync.py 三个文件消失 —— start.bat 的唯一启动入口
       (bridge_watchdog_launcher.py) 不存在，重跑 start.bat 也起不来；
       且 mt5_bridge.py 硬依赖 position_sync，手动启动同样 import 崩溃。
    ② hcm-web 的"一键自愈"跑在 Linux 容器里，它的 restart_bridge 只能往
       Redis 写 bridge:control:<login>=restart 信令，依赖【主机上还活着的】
       桥读到信令后退出、再由【主机看门狗】重拉。当桥与看门狗在主机侧同时
       死亡时，容器没有任何手段能启动 Windows 进程 —— 自愈 100% 空转。

  既有的 launcher→watchdog→bridge 是"双层自愈"，但整条链都活在同一棵进程树上：
  树根(launcher)一死，或脚本文件被删，整条链就再也回不来，没有任何外部力量兜底。

本脚本的职责 = 补上最外面那一层（OS 层），由 Windows 计划任务周期调用：
    1) 完整性自愈：关键脚本缺失 → 自动从 _baseline 还原
    2) 进程自愈  ：launcher 不在 → 幂等拉起（launcher 自带 Global 互斥体，
                   重复调用会立即自退，不会打架）
    3) 基线维护  ：文件存在且能通过编译 → 刷新 _baseline 副本，
                   使基线始终跟随"最后一个已知良好版本"

用法
    powershell -NoProfile -ExecutionPolicy Bypass -File bridge_boot.ps1
    powershell ... -File bridge_boot.ps1 -Snapshot   # 仅刷新基线，不拉起
#>

param(
    [switch]$Snapshot
)

$ErrorActionPreference = 'SilentlyContinue'

$TOOLS    = 'd:\HCM_ASST\hcm-v2\tools'
$BASELINE = Join-Path $TOOLS '_baseline'
$PY       = 'C:\Python313\python.exe'
$LAUNCHER = Join-Path $TOOLS 'bridge_watchdog_launcher.py'
$BOOTLOG  = Join-Path $TOOLS 'bridge_boot.log'
$LNCLOG   = Join-Path $TOOLS 'bridge_launcher.log'

# 桥栈运行所必需的四个脚本，缺任何一个整条链都起不来
$CRITICAL = @(
    'mt5_bridge.py',
    'position_sync.py',
    'bridge_watchdog_pa.py',
    'bridge_watchdog_launcher.py'
)

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

# ── 1) 完整性自愈 + 基线维护 ──────────────────────────────────────────
$restored = 0
foreach ($name in $CRITICAL) {
    $live = Join-Path $TOOLS $name
    $base = Join-Path $BASELINE $name

    if (-not (Test-Path -LiteralPath $live)) {
        # 工作文件丢失 → 从基线还原
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

    # 工作文件在 → 仅当能通过编译（语法完好）才允许刷新基线，
    # 避免把一个损坏/截断的文件固化成"已知良好版本"
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

if ($Snapshot) {
    Write-BootLog ("snapshot-only run done (restored=" + $restored + ")")
    Write-Output ("SNAPSHOT_DONE restored=" + $restored)
    return
}

# ── 2) 进程自愈：launcher 不在则幂等拉起 ──────────────────────────────
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -like '*bridge_watchdog_launcher.py*' }

if ($running) {
    Write-Output ('LAUNCHER_ALIVE pid=' + ($running | Select-Object -First 1).ProcessId)
    return
}

if (-not (Test-Path -LiteralPath $LAUNCHER)) {
    Write-BootLog 'FATAL: launcher script unavailable, cannot spawn bridge stack'
    Write-Output 'LAUNCHER_MISSING'
    return
}

# 与 start.bat 完全一致的 DETACHED_PROCESS 拉起方式。
# 必须是真·无控制台的 DETACHED，否则 MetaTrader5 原生 DLL 在其子进程中会静默死。
$code = "import subprocess; subprocess.Popen([r'$PY', r'$LAUNCHER'], " +
        "creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, " +
        "stdout=open(r'$LNCLOG','a'), stderr=subprocess.STDOUT)"
& $PY -c $code

Write-BootLog ('launcher respawned by OS-level guard (restored=' + $restored + ')')
Write-Output 'LAUNCHER_SPAWNED'
