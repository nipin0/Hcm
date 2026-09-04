' Hidden launcher for ai_stack_guard.ps1 (window style 0 = no console flash)
Set ws = CreateObject("WScript.Shell")
ws.Run "powershell -NoProfile -ExecutionPolicy Bypass -File ""d:\HCM_ASST\hcm-v2\tools\ai_stack_guard.ps1""", 0, False
