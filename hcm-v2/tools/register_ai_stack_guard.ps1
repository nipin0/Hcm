# Register HCM_AIStackGuard - OS-level watchdog for TimesFM + LightGBM sidecar.
# ASCII-only on purpose: Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI and
# mangles non-ASCII bytes, which breaks parsing.
#
# Runs ai_stack_guard.ps1 (via hidden .vbs, no console flash) every 3 minutes
# and at logon. The guard is heartbeat-aware, so it also recovers processes that
# are running but stuck (not just missing ones).
#
# NOTE: -RepetitionDuration is deliberately omitted. Passing [TimeSpan]::MaxValue
# produces an out-of-range XML duration (P99999999D) and registration fails.

$ErrorActionPreference = 'Stop'

$TaskName = 'HCM_AIStackGuard'
$Vbs      = 'd:\HCM_ASST\hcm-v2\tools\ai_stack_guard_hidden.vbs'

if (-not (Test-Path -LiteralPath $Vbs)) {
    Write-Output ('VBS_MISSING: ' + $Vbs)
    exit 1
}

$exist = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($exist) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output 'OLD_TASK_REMOVED'
}

$action   = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('//B "' + $Vbs + '"')
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
    -Description 'HCM AI stack watchdog (TimesFM + LightGBM sidecar): heartbeat-aware auto-restart every 3 minutes and at logon' | Out-Null

Write-Output 'TASK_REGISTERED'
Get-ScheduledTask -TaskName $TaskName | ForEach-Object { $_.TaskName + ' | State=' + $_.State }
