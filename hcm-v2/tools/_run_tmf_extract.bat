@echo off
D:\.venv_timesfm\Scripts\python.exe D:\HCM_ASST\hcm-v2\tools\timesfm_features.py ^
  --extract ^
  --pca D:\HCM_ASST\hcm-v2\tools\models\tmf_pca_v1.pkl ^
  --start 2026-06-01 --end 2026-08-30 ^
  --at-signal-times --create-table ^
  > D:\HCM_ASST\hcm-v2\tools\_tmf_extract.log 2>&1
echo EXITCODE=%ERRORLEVEL% >> D:\HCM_ASST\hcm-v2\tools\_tmf_extract.log
