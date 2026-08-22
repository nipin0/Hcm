' HCM 桥栈守护 - 无窗口包装器
' 计划任务直接调 powershell 会每次闪一个蓝窗（与历史上看门狗每 20s 闪窗问题同源）。
' 用 WScript.Shell.Run 的 window style = 0 真正隐藏，且 bWaitOnReturn=False 立即返回。
Option Explicit
Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""d:\HCM_ASST\hcm-v2\tools\bridge_boot.ps1"""
sh.Run cmd, 0, False
