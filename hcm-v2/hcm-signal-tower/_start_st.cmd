@echo off
cd /d D:\HCM_ASST\hcm-v2\hcm-signal-tower
start "signal_tower" /min C:\Python313\python.exe -u main.py > D:\HCM_ASST\hcm-v2\hcm-signal-tower\_st_run.log 2>&1
exit
