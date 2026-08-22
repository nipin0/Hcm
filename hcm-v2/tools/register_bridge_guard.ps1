$ErrorActionPreference = 'Stop'

<#
  注意(2026-08-13)：看门狗已改为「不随 Windows 自动启动」，仅随 HCM 系统(start.bat)手动启动。
  原因：看门狗拉桥需连接 MT5(MetaTrader5.initialize)，而该 API 受 Windows 会话隔离，
  只能在「用户登录会话」内连接用户运行的 terminal.exe；若交给计划任务在 Session 0/SYSTEM
  下自启动(即"随 Windows 开机")，mt5.initialize 会连不上。因此本脚本不再被 start.bat 调用，
  仅作可选的手动注册入口——且触发器仍用 -AtLogOn(随用户登录、在其会话内运行，唯一 MT5 兼容方式)。
  若你希望看门狗在 Windows 登录时自动拉起，可手动运行本脚本；否则请勿运行。
#>

$TaskName = 'HCM_BridgeGuard'
$Vbs      = 'd:\HCM_ASST\hcm-v2\tools\bridge_boot_hidden.vbs'

# 已存在则先删（幂等，便于重复执行本脚本）
$exist = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($exist) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output 'OLD_TASK_REMOVED'
}

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('//B "' + $Vbs + '"')

# 触发器 1：登录即跑（开机后尽快恢复桥栈）
$trgLogon = New-ScheduledTaskTrigger -AtLogOn

# 触发器 2：每 3 分钟巡检一次（省略 RepetitionDuration = 无限期；
# 显式传 [TimeSpan]::MaxValue 会被任务计划 XML 判为越界 P99999999D 而注册失败）
$trgRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 3)

$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger @($trgLogon, $trgRepeat) `
    -Settings $settings `
    -Description 'HCM 桥栈 OS 层守护：关键脚本缺失自动还原 + launcher 幂等重拉（补齐容器自愈无法启动 Windows 进程的盲区）' | Out-Null

Write-Output 'TASK_REGISTERED'
Get-ScheduledTask -TaskName $TaskName | ForEach-Object { $_.TaskName + ' | State=' + $_.State }
