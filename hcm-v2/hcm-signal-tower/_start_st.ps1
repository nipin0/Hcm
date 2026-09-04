$log = "D:/HCM_ASST/hcm-v2/hcm-signal-tower/_st_run.log"
Start-Process -FilePath "C:/Python313/python.exe" -ArgumentList "-u","main.py" -WorkingDirectory "D:/HCM_ASST/hcm-v2/hcm-signal-tower" -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden
Write-Host "signal_tower launched"
