#!/usr/bin/env python3
"""timesfm_daily_launcher.py — 常驻拉起 TimesFM 每日增量抽取调度器（DETACHED）。

与 quality_scorer_launcher.py 同构（项目既有范式）：
  start.bat / 计划任务 → 本 launcher → timesfm_daily_scheduler.py（脱离会话生命周期）

为什么需要单实例互斥：
  若守护脚本与手动 start.bat 并发拉起多个调度器，会重复跑同一窗口的抽取，
  既浪费 GPU/CPU（每次约 1.5s/根），又会让 --lib-cache 被并发读写而损坏。
  故沿用 Windows 命名互斥体（内核级原子，进程退出自动释放）+ 运行中实例探测。

解释器用 D:\\.venv_timesfm\\Scripts\\pythonw.exe（无窗口）：
  该 venv 才装了 torch / timesfm / psycopg2；生产 C:\\Python313 不装这些重依赖，
  避免污染生产解释器。
"""
import ctypes
import os
import subprocess
import sys

PY = r"D:/.venv_timesfm/Scripts/pythonw.exe"
TOOLS = r"D:/HCM_ASST/hcm-v2/tools"
LOG = os.path.join(TOOLS, "_logs", "timesfm_daily_launcher.log")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

MUTEX_NAME = "Global\\hcm_timesfm_daily_scheduler_singleton"
ERROR_ALREADY_EXISTS = 183


def _acquire_singleton():
    """Windows 命名互斥体单实例锁；已被占用返回 None。"""
    if not sys.platform.startswith("win"):
        return True
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return None
        return handle
    except Exception:  # noqa: BLE001
        return True  # 互斥体不可用时不阻塞拉起（保守放行）


def _scheduler_running() -> int | None:
    """探测是否已有 timesfm_daily_scheduler.py 在跑（返回 PID）。"""
    if not sys.platform.startswith("win"):
        return None
    try:
        # 探测必须**排除调用者自身**，否则会自匹配：
        # 本机 venv(D:\.venv_timesfm) 的 python/pythonw 是 shim（uv 风格），
        # 会再 exec 出真正的解释器子进程；而下面 -Command 的命令行里含
        # 'timesfm_daily_scheduler.py' 这个模式串，PowerShell 自己会被匹配上
        # （实测返回 PowerShell 自身 PID 21676，导致守护永远不拉起）。
        # 故：按命令行匹配 + 排除 powershell/wscript/cmd/conhost 等调用者。
        # 不按 Name 过滤——真实工作者可能是 python.exe 而非 pythonw.exe。
        ps = (
            "Get-CimInstance Win32_Process | Where-Object { "
            "$_.CommandLine -like '*timesfm_daily_scheduler.py*' -and "
            "$_.Name -notmatch '^(powershell|pwsh|wscript|cscript|cmd|conhost)\\.exe$' } | "
            "Select-Object -First 1 -ExpandProperty ProcessId"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            stderr=subprocess.DEVNULL, timeout=20, creationflags=CREATE_NO_WINDOW,
        )
        txt = out.decode("utf-8", "ignore").strip()
        return int(txt) if txt.isdigit() else None
    except Exception:  # noqa: BLE001
        return None


def main():
    mutex = _acquire_singleton()
    if mutex is None:
        print("[launcher] another launcher holds singleton mutex; exiting")
        return

    existing = _scheduler_running()
    if existing:
        print(f"[launcher] scheduler already running pid={existing}; skip spawn")
        return

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    # 日志由调度器内部轮转；launcher 不持有日志文件句柄（避免涨爆磁盘，2026-08-27 教训）
    p = subprocess.Popen(
        [PY, "timesfm_daily_scheduler.py", "--daemon"],
        cwd=TOOLS,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    print(f"[launcher] scheduler launched pid={p.pid}, log={LOG}")
    sys.exit(0)  # 拉起即退出，单实例由互斥体 + 运行中探测保证


if __name__ == "__main__":
    main()
