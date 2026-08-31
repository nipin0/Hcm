@echo off
REM 立即返回：用 start 把 watchdog 脱离当前会话后台运行，
REM 抽取水位线推进后自动衔接 auto_retrain.py（LightGBM 重训）。
start "" /min D:\.venv_timesfm\Scripts\pythonw.exe D:\HCM_ASST\hcm-v2\tools\_tmf_then_retrain.py
exit /b 0
