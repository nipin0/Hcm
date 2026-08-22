' HCM AI 评分 sidecar 守护 - 无窗口包装器
Option Explicit
Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""d:\HCM_ASST\hcm-v2\tools\quality_scorer_guard.ps1"""
sh.Run cmd, 0, False
