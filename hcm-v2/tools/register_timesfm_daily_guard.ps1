$ErrorActionPreference = 'Stop'

<#
  注册 HCM TimesFM 每日 T+1 增量抽取调度器的 OS 层守护计划任务。

  为什么需要：
    调度器是「B 方案：先跑架构、再积累样本」的产能来源，属主机进程
    （依赖 D:\.venv_timesfm 的 torch/timesfm，不在容器内）。
    若仅随 start.bat 手动启动，机器重启/会话丢失后无人手动重跑，
    样本积累会静默停摆，导致 13 天后的 §7.2 重测无数据可用。
    本任务以每 10 分钟 + 登录时触发 timesfm_daily_boot.ps1，由 OS 层兜底拉起，
    与桥栈的 bridge_boot.ps1 / HCM_BridgeGuard、AI 评分的
    ai_scorer_boot.ps1 / HCM_AIScorerGuard 完全对等。

  调度器依赖：
    - D:\.venv_timesfm（torch / timesfm / psycopg2 / numpy）
    - D:\.venv_timesfm\model\ms\timesfm-2.5-200m-pytorch（882MB 本地权重，离线加载）
    - tools/models/tmf_pca_v1.pkl（PCA 变换，与拟合参数严格一致）
    - tools/models/tmf_hist_lib_v1.npz（检索库缓存，缺失则 hist_sim 分布不可比）
    - PG 5432 / Redis 6379（经 127.0.0.1，勿用 localhost：会解析到 ::1 被 wslrelay 劫持）

  架构定位（铁律 5.1）：本调度器只产出离线特征，零下单影响；
  在 §7.2 门禁达标前质量头不参与决策（G0 影子模式）。
#>

$TaskName = 'HCM_TimesFMDailyGuard'
$Boot     = 'd:\HCM_ASST\hcm-v2\tools\timesfm_daily_boot.ps1'
# 经 .vbs 包装（WindowStyle 0）调用，彻底消除计划任务触发时的 PowerShell 蓝窗闪烁
$Vbs      = 'd:\HCM_ASST\hcm-v2\tools\timesfm_daily_boot_hidden.vbs'

# 前置检查：权重与 PCA 必须在位，否则注册了也是空转
$modelDir = 'D:\.venv_timesfm\model\ms\timesfm-2.5-200m-pytorch'
if (-not (Test-Path -LiteralPath $modelDir)) {
    Write-Output ('MODEL_DIR_MISSING: ' + $modelDir)
    exit 1
}
if (-not (Test-Path -LiteralPath 'd:\HCM_ASST\hcm-v2\tools\models\tmf_pca_v1.pkl')) {
    Write-Output 'PCA_MISSING: tools\models\tmf_pca_v1.pkl'
    exit 1
}
if (-not (Test-Path -LiteralPath $Boot)) {
    Write-Output ('BOOT_MISSING: ' + $Boot)
    exit 1
}

# 已存在则先删（幂等）
$exist = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($exist) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output 'OLD_TASK_REMOVED'
}

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('//B "' + $Vbs + '"')

$trgLogon = New-ScheduledTaskTrigger -AtLogOn
# 每 10 分钟：批处理型守护，无需像评分 sidecar 那样 3 分钟高频
$trgRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 10)

$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger @($trgLogon, $trgRepeat) `
    -Settings $settings `
    -Description 'HCM TimesFM 每日 T+1 增量抽取调度器 OS 层守护：进程缺失自动重拉（每 10 分钟 + 登录时）' | Out-Null

Write-Output 'TASK_REGISTERED'
Get-ScheduledTask -TaskName $TaskName | ForEach-Object { $_.TaskName + ' | State=' + $_.State }
