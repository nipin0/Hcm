$ErrorActionPreference = 'Stop'

<#
  注册 HCM AI 评分 sidecar 的 OS 层守护计划任务（2026-08-16 重建）。

  为什么需要：
    sidecar（quality_scorer.py）是 LightGBM 实时打分主机进程，原仅随 start.bat 手动启动。
    机器重启/会话丢失后若无人手动重跑 start.bat，AI 质量闸门静默失效。
    本任务以每 3 分钟 + 登录时触发 ai_scorer_boot.ps1，由 OS 层兜底拉起 sidecar，
    与桥栈的 bridge_boot.ps1 / HCM_BridgeGuard 计划任务对等。

  sidecar 依赖：
    - C:\Python313（含 lightgbm 等依赖）
    - tools/models/ 下的模型文件（由 ai_scorer_boot.ps1 自愈）
    - ai.lm.enabled=true / ai.mode=coupled（配置中心）
#>

$TaskName = 'HCM_AIScorerGuard'
$Boot     = 'd:\HCM_ASST\hcm-v2\tools\ai_scorer_boot.ps1'
# 经 .vbs 包装（WindowStyle 0）调用，彻底消除计划任务每 3 分钟触发的 PowerShell 蓝窗闪烁
$Vbs      = 'd:\HCM_ASST\hcm-v2\tools\ai_scorer_boot_hidden.vbs'

# 已存在则先删（幂等）
$exist = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($exist) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output 'OLD_TASK_REMOVED'
}

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('//B "' + $Vbs + '"')

$trgLogon = New-ScheduledTaskTrigger -AtLogOn
$trgRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 3)

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
    -Description 'HCM AI 评分 sidecar OS 层守护：进程缺失自动重拉（每 3 分钟 + 登录时）' | Out-Null

Write-Output 'TASK_REGISTERED'
Get-ScheduledTask -TaskName $TaskName | ForEach-Object { $_.TaskName + ' | State=' + $_.State }
