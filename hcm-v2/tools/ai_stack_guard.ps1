<#
HCM - AI stack (TimesFM + LightGBM sidecar) unified host-level guard
=================================================================
NOTE: This file MUST stay ASCII-only. Windows PowerShell 5.1 reads .ps1 as
ANSI unless a UTF-8 BOM is present; non-ASCII bytes get mangled and break
parsing. Keep all comments/messages in English.

Duties (mirrors bridge_boot.ps1; OS-level fallback, no manual intervention):
  1) Script integrity: missing .py -> restore from _baseline;
     present and py_compile OK -> refresh baseline copy
  2) Liveness via Redis heartbeat (NOT just PID) -> detects "running but stuck"
  3) Dedup: keep the PID reported by the heartbeat, kill the rest
  4) On-demand heal: consume signal ai:heal:request:{comp} (UI button / API)
  5) Status writeback: hcm:ai:{comp}:status (TTL 180s) for the health page

Liveness criteria
  lightgbm : ai.lm.health_check  ts field (epoch sec, updated ~5s)   timeout 120s
  timesfm  : hcm:ai:timesfm:daily at field ("yyyy-MM-dd HH:mm:ss" UTC,
             refreshed each 30min check)                             timeout 7200s
  auto_retrain: hcm:ai:retrain:daemon at field ("yyyy-MM-dd HH:mm:ss",
             refreshed each ~1h daemon loop)                        timeout 172800s (48h TTL)

Usage:
  powershell -NoProfile -ExecutionPolicy Bypass -File ai_stack_guard.ps1
  powershell ... -File ai_stack_guard.ps1 -Snapshot   # baseline maintenance only
#>

param(
    [switch]$Snapshot,
    [switch]$Dry          # detect-only: never kill, never spawn (safe debugging)
)

$ErrorActionPreference = 'SilentlyContinue'

$TOOLS    = 'd:/HCM_ASST/hcm-v2/tools'
$BASELINE = Join-Path $TOOLS '_baseline'
$GUARDLOG = Join-Path $TOOLS 'ai_stack_guard.log'
$REDIS_CT = 'hcm-v2-redis-1'

$LIGHTGBM = @{
    Name      = 'lightgbm'
    Display   = 'LightGBM sidecar'
    Pattern   = 'quality_scorer.py'
    Py        = 'C:\Python313\pythonw.exe'
    Launcher  = Join-Path $TOOLS 'quality_scorer_launcher.py'
    BeatKey   = 'ai.lm.health_check'
    BeatMode  = 'epoch'
    MaxAgeSec = 120
    ProcName  = 'pythonw.exe'
    Critical  = @('quality_scorer.py', 'quality_scorer_launcher.py', 'quality_features.py')
}

$TIMESFM = @{
    Name      = 'timesfm'
    Display   = 'TimesFM scheduler'
    Pattern   = 'timesfm_daily_scheduler.py'
    Py        = 'D:\.venv_timesfm\Scripts\pythonw.exe'
    Launcher  = Join-Path $TOOLS 'timesfm_daily_launcher.py'
    BeatKey   = 'hcm:ai:timesfm:daily'
    BeatMode  = 'at'
    MaxAgeSec = 7200
    ProcName  = 'pythonw.exe'
    Critical  = @('timesfm_daily_scheduler.py', 'timesfm_daily_launcher.py', 'timesfm_features.py')
}

# 2026-09-03 加固：LightGBM 自动重训守护此前不在 $COMPONENTS 中，无任何看门狗，
# 进程一死即永久停工（历史上守护被停后无人拉起）。现纳入统一守卫：
#   - 守护以 C:\Python313\python.exe 运行（非 pythonw.exe），故 ProcName='python.exe'，
#     进程存活判定须按此解释器名过滤，否则会被误判为"从未运行"而每轮空转拉起 launcher。
#   - 守护只切质量头(ai.lm.model_path/calib_path)，决策 rollback=保持当前/不采纳新候选，
#     绝不回滚到旧版、也从不碰 entry/dir 头 → 加入守卫重活不会冲掉已部署的好模型(v80/v81)。
#   - 守护主循环每次迭代末写 hcm:ai:retrain:daemon 心跳(TTL 48h)，故 MaxAgeSec=172800。
$AUTORETRAIN = @{
    Name      = 'auto_retrain'
    Display   = 'LightGBM auto-retrain daemon'
    Pattern   = 'auto_retrain.py'
    Py        = 'C:\Python313\python.exe'
    Launcher  = Join-Path $TOOLS 'auto_retrain_launcher.ps1'
    BeatKey   = 'hcm:ai:retrain:daemon'
    BeatMode  = 'at'
    MaxAgeSec = 172800
    ProcName  = 'python.exe'
    Critical  = @('auto_retrain.py')
}

$COMPONENTS = @($LIGHTGBM, $TIMESFM, $AUTORETRAIN)

function Write-GuardLog([string]$msg) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $msg
    Add-Content -LiteralPath $GUARDLOG -Value $line -Encoding utf8
}

function Get-Sha256([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return '' }
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
}

function Invoke-Redis {
    param([string[]]$RedisArgs)
    return (& docker exec $REDIS_CT redis-cli @RedisArgs 2>$null)
}

# Returns @(ageSeconds, procId); @(-1, 0) when the heartbeat is UNREADABLE.
# IMPORTANT: -1 means "cannot tell" (Redis down / key missing / parse failure) and
# must NEVER be treated as stale -- otherwise a transient Redis hiccup would make
# the guard kill perfectly healthy processes. Only a readable-but-old heartbeat
# (age > MaxAgeSec) may justify a restart.
function Get-Heartbeat($c) {
    $raw = Invoke-Redis @('GET', $c.BeatKey)
    if (-not $raw) { return @(-1, 0) }
    try {
        $j = ($raw -join '') | ConvertFrom-Json
        $procId = 0
        if ($j.PSObject.Properties.Name -contains 'pid') { $procId = [int]$j.pid }

        $beatTime = $null
        if ($c.BeatMode -eq 'epoch') {
            $ts = [double]$j.ts
            $beatTime = (Get-Date '1970-01-01T00:00:00Z').AddSeconds($ts)
        }
        else {
            $beatTime = [datetime]::ParseExact([string]$j.at, 'yyyy-MM-dd HH:mm:ss',
                                               [Globalization.CultureInfo]::InvariantCulture)
        }
        $age = ((Get-Date).ToUniversalTime() - $beatTime.ToUniversalTime()).TotalSeconds
        if ($age -lt 0) { $age = 0 }
        return @([int]$age, $procId)
    }
    catch {
        return @(-1, 0)
    }
}

if (-not (Test-Path -LiteralPath $BASELINE)) {
    New-Item -ItemType Directory -Path $BASELINE -Force | Out-Null
}

# --- 1) script integrity + baseline maintenance -------------------------
foreach ($c in $COMPONENTS) {
    foreach ($name in $c.Critical) {
        $live = Join-Path $TOOLS $name
        $base = Join-Path $BASELINE $name

        if (-not (Test-Path -LiteralPath $live)) {
            if (Test-Path -LiteralPath $base) {
                Copy-Item -LiteralPath $base -Destination $live -Force
                Write-GuardLog ('RESTORED [' + $c.Name + '] missing script: ' + $name)
            }
            else {
                Write-GuardLog ('FATAL [' + $c.Name + '] ' + $name + ' missing AND no baseline')
            }
            continue
        }
        if ((Get-Sha256 $live) -ne (Get-Sha256 $base)) {
            # pythonw is a GUI-subsystem binary; under the scheduled task its native
            # exit code is NOT reliably captured by $LASTEXITCODE (every run looked
            # like a failure, so the baseline was never refreshed). Start-Process
            # -Wait -PassThru returns a deterministic ExitCode for pythonw/python.
            $cp = Start-Process -FilePath $c.Py -ArgumentList @('-m', 'py_compile', $live) `
                    -Wait -PassThru -WindowStyle Hidden
            if ($cp.ExitCode -eq 0) {
                Copy-Item -LiteralPath $live -Destination $base -Force
            }
            else {
                Write-GuardLog ('WARN [' + $c.Name + '] ' + $name + ' py_compile failed; baseline kept')
            }
        }
    }
}

if ($Snapshot) {
    Write-GuardLog 'snapshot-only run done'
    Write-Output 'SNAPSHOT_DONE'
    return
}

# --- 2) per component: liveness / dedup / heal / status ------------------
foreach ($c in $COMPONENTS) {
    $name = $c.Name

    $sigKey = 'ai:heal:request:' + $name
    $sig = Invoke-Redis @('GET', $sigKey)
    $forced = [bool]$sig

    $age, $beatPid = Get-Heartbeat $c
    # age -1 => heartbeat unreadable (Redis down?). We CANNOT judge liveness,
    # so treat as "unknown" and never restart on that basis (fail-safe).
    $readable = ($age -ge 0)
    $alive = if ($readable) { ($age -lt $c.MaxAgeSec) } else { $true }

    $procs = @(Get-CimInstance Win32_Process -Filter "Name='$($c.ProcName)'" |
               Where-Object { $_.CommandLine -like ('*' + $c.Pattern + '*') })

    $action = 'none'
    $healed = $false

    # 2a) dedup: only when processes exceed the NORMAL launcher->daemon pair.
    #     Both components legitimately run as parent(launcher-spawned wrapper, ~0 CPU)
    #     -> child(real daemon writing heartbeats). Killing the parent is harmful:
    #     the launcher would see its child gone and respawn, causing churn.
    #     So we only prune when there are MORE than 2 matching processes.
    if ($procs.Count -gt 2) {
        $keep = $null
        if ($beatPid -gt 0) {
            $keep = $procs | Where-Object { $_.ProcessId -eq $beatPid } | Select-Object -First 1
        }
        if (-not $keep) {
            $keep = $procs | Sort-Object CreationDate | Select-Object -First 1
        }
        # protect the heartbeat pid and its direct parent from pruning
        $protect = @($keep.ProcessId)
        if ($keep.ParentProcessId) { $protect += [int]$keep.ParentProcessId }
        foreach ($p in $procs) {
            if ($protect -notcontains $p.ProcessId) {
                if ($Dry) {
                    Write-GuardLog ('DRY [' + $name + '] would kill duplicate pid=' + $p.ProcessId +
                                    ' (keep=' + $keep.ProcessId + ')')
                }
                else {
                    Stop-Process -Id $p.ProcessId -Force
                    Write-GuardLog ('DEDUP [' + $name + '] killed duplicate pid=' + $p.ProcessId +
                                    ' (keep=' + $keep.ProcessId + ')')
                }
            }
        }
        $procs = @($keep)
        $action = 'dedup'
    }

    # 2b) stuck: process present AND heartbeat readable-but-old -> kill then respawn.
    #     Guarded by $readable: an unreadable heartbeat never triggers a restart.
    if ($procs.Count -eq 1 -and $readable -and -not $alive) {
        if ($Dry) {
            Write-GuardLog ('DRY [' + $name + '] would kill stale pid=' + $procs[0].ProcessId +
                            ' (age=' + $age + 's)')
        }
        else {
            Stop-Process -Id $procs[0].ProcessId -Force
            Write-GuardLog ('STALE [' + $name + '] pid=' + $procs[0].ProcessId +
                            ' heartbeat age=' + $age + 's > ' + $c.MaxAgeSec + 's -> killed for respawn')
        }
        $procs = @()
        $action = 'respawn_stale'
    }

    # 2c) missing or forced -> spawn
    if ($procs.Count -eq 0 -or $forced) {
        if ($procs.Count -eq 0 -and -not $forced) { $action = 'respawn_dead' }
        if ($forced) { $action = 'respawn_forced' }

        if ($Dry) {
            Write-GuardLog ('DRY [' + $name + '] would spawn via ' + $c.Launcher +
                            ' (action=' + $action + ')')
        }
        elseif (Test-Path -LiteralPath $c.Launcher) {
            # Launch the component launcher as a detached hidden process.
            # Use Start-Process directly (NOT "python -c subprocess.Popen([...])"):
            # embedding a JSON arg list inside `python -c` loses the double quotes
            # across the Win32 command-line parser, producing a SyntaxError.
            if ($c.Launcher -like '*.ps1') {
                $sp = Start-Process -FilePath 'powershell.exe' `
                        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $c.Launcher) `
                        -WindowStyle Hidden -PassThru
            }
            else {
                $sp = Start-Process -FilePath $c.Py `
                        -ArgumentList @($c.Launcher) `
                        -WindowStyle Hidden -PassThru
            }
            Write-GuardLog ('SPAWN [' + $name + '] launcher invoked (action=' + $action + ')')
            $healed = $true
            Start-Sleep -Seconds 6
        }
        else {
            Write-GuardLog ('FATAL [' + $name + '] launcher missing, cannot spawn')
        }
    }

    if ($forced -and -not $Dry) { Invoke-Redis @('DEL', $sigKey) | Out-Null }

    # 2d) status writeback (TTL 180s: stale key => guard itself is down)
    $procs2 = @(Get-CimInstance Win32_Process -Filter "Name='$($c.ProcName)'" |
                Where-Object { $_.CommandLine -like ('*' + $c.Pattern + '*') })
    $age2, $beatPid2 = Get-Heartbeat $c
    $readableOut = ($age2 -ge 0)
    $aliveOut = if ($readableOut) { ($age2 -lt $c.MaxAgeSec) } else { $false }
    # Write status as pipe-delimited ASCII text, NOT JSON: Windows PowerShell 5.1
    # strips embedded double quotes when passing an argument to a native process
    # (docker exec -> redis-cli), corrupting JSON (stored keys lost their quotes,
    # e.g. "{dry:false,...}", so the web endpoint could not parse it). The pipe
    # format contains no quotes and survives native argument passing intact.
    # Web endpoint web/api/system.py parses this pipe format (with JSON fallback).
    $status = ('name=' + $c.Display +
               '|pid=' + $(if ($procs2.Count -gt 0) { $procs2[0].ProcessId } else { 0 }) +
               '|running=' + [bool]($procs2.Count -gt 0) +
               '|beat_age_s=' + $age2 +
               '|readable=' + ($age2 -ge 0) +
               '|alive=' + $(if ($age2 -ge 0) { ($age2 -lt $c.MaxAgeSec) } else { $false }) +
               '|action=' + $action +
               '|healed=' + [bool]$healed +
               '|dry=' + [bool]$Dry +
               '|ts=' + [int][double]::Parse((Get-Date -UFormat %s)))

    if (-not $Dry) {
        Invoke-Redis @('SET', ('hcm:ai:' + $name + ':status'), $status, 'EX', '180') | Out-Null
    }

    Write-Output ('[' + $name + '] running=' + ($procs2.Count -gt 0) + ' beatAge=' + $age2 +
                  's alive=' + $aliveOut + ' readable=' + $readableOut + ' action=' + $action)
}

Write-GuardLog 'cycle done'
