Option Explicit
Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""d:\HCM_ASST\hcm-v2\tools\ai_scorer_boot.ps1"""
sh.Run cmd, 0, False
