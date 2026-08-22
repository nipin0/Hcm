#!/usr/bin/env python3
"""桥栈保活 launcher —— 看门狗自身的守护进程（彻底修复"桥经常出问题"）。

设计动机：
  看门狗(watchdog)一旦崩溃，所有桥都无人托管（此前已发生，导致信号全断）。
  本 launcher 极简、几乎不会死，常驻检测 watchdog 存活，死后自动重拉。
  再由 watchdog 兜底各桥崩溃 —— 形成「launcher ⊇ watchdog ⊇ bridge」双层自愈。

进程模型（关键：全程非 DETACHED，避免 MetaTrader5 原生 DLL 在 detached
祖父下重拉静默死）：
  start.bat ─Start-Process -WindowStyle Hidden─▶ 本 launcher（隐藏窗口，普通控制台进程）
    本 launcher ─CREATE_NEW_CONSOLE─▶ watchdog（隐藏窗口）
      watchdog ─CREATE_NEW_CONSOLE─▶ mt5_bridge.py（双账户，数据驱动零硬编码）

launcher 自身只做 sleep + OpenProcess 探活 + 必要时 spawn，无复杂依赖，崩溃概率极低。
"""
import os
import sys
import time
import subprocess
import ctypes

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PY = r"C:\Python313\python.exe"
WATCHDOG = os.path.join(TOOLS_DIR, "bridge_watchdog_pa.py")
LOG = os.path.join(TOOLS_DIR, "bridge_launcher.log")
LOCK = os.path.join(TOOLS_DIR, "bridge_launcher.lock")
CHECK_INTERVAL = 15  # 每 15s 探一次 watchdog 存活

kernel32 = ctypes.windll.kernel32

# Windows 命名互斥体 — 内核级单实例, 进程死亡自动释放, 作为第一防线杜绝双 launcher 并存
# (旧代码仅靠进程扫描 + LOCK 文件, 在 PowerShell 超时 except-pass / LOCK 读异常时会静默漏网,
# 导致双 launcher 互抢看门狗互斥体→看门狗打架/频闪)。
_LAUNCHER_MUTEX_HANDLE = None


def _acquire_launcher_singleton():
    """内核级单实例锁。若已有存活 launcher 持有互斥体则返回 False(本实例应退出)。"""
    global _LAUNCHER_MUTEX_HANDLE
    try:
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, "Global\\hcm_bridge_launcher_singleton")
        if not h:
            return True  # 创建失败则放行(降级, 不阻断)
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _LAUNCHER_MUTEX_HANDLE = h  # 持句柄防 GC; 进程退出(含崩溃)时内核自动释放
        return True
    except Exception:
        return True


def _find_existing_watchdog(self_pid):
    """认领既有存活 watchdog(可能由其它 launcher 拉起), 避免本 launcher 反复 spawn 撞互斥体秒退。

    双 launcher 漏网(互斥体竟被绕过)时的自愈: 直接接管已存在的 watchdog, 而非再拉一个抢锁。
    """
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter 'Name=''python.exe''' | "
             "Select-Object ProcessId,CommandLine | "
             "ForEach-Object { ($_.ProcessId.ToString() + '|' + ($_.CommandLine -join '')) }"],
            stderr=subprocess.DEVNULL, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        ).decode("utf-8", "ignore")
        for line in out.splitlines():
            if "|" not in line:
                continue
            pid_s, cmd = line.split("|", 1)
            if "bridge_watchdog_pa.py" not in cmd:
                continue
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            if pid == self_pid or not is_alive(pid):
                continue
            return pid
    except Exception:
        pass
    return None


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as lf:
            lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def is_alive(pid):
    if not pid:
        return False
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    kernel32.CloseHandle(h)
    return True


def spawn_watchdog():
    # 用 DETACHED_PROCESS 拉 watchdog（而非 CREATE_NEW_CONSOLE）。
    # 实测：CREATE_NEW_CONSOLE 父 → CREATE_NEW_CONSOLE 子(watchdog) 会零输出秒死；
    # DETACHED 祖父拉起的子进程才稳定（与 4152 成功模式一致）。watchdog 自身
    # 用 _hide_console_window 隐藏窗口，故 DETACHED 不影响隐蔽性。
    # stdout 重定向到 console 日志兜底早期崩溃（含 faulthandler 原生崩溃 dump）。
    console_log = open(os.path.join(TOOLS_DIR, "bridge_watchdog_console.log"), "ab", buffering=0)
    p = subprocess.Popen(
        [PY, WATCHDOG],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        stdout=console_log,
        stderr=subprocess.STDOUT,
        close_fds=False,
    )
    console_log.close()
    return p.pid


def _other_launcher_alive(self_pid):
    """检测是否有其它 launcher 实例在跑（命令行含 bridge_watchdog_launcher.py 且非自身）。"""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter 'Name=''python.exe''' | "
             "Select-Object ProcessId,CommandLine | "
             "ForEach-Object { ($_.ProcessId.ToString() + '|' + ($_.CommandLine -join '')) }"],
            stderr=subprocess.DEVNULL, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        ).decode("utf-8", "ignore")
        for line in out.splitlines():
            if "|" not in line:
                continue
            pid_s, cmd = line.split("|", 1)
            if "bridge_watchdog_launcher.py" not in cmd:
                continue
            try:
                pid = int(pid_s)
            except ValueError:
                continue
            if pid == self_pid or not is_alive(pid):
                continue
            return pid
    except Exception:
        pass
    return None


def acquire_single_instance():
    """进程级单实例锁：若已有存活的 launcher 实例则退出，避免重复看门狗叠加。

    双重保险：(1) 扫描进程命令行发现其它 launcher 实例则退出；(2) LOCK 文件
    记录本进程 pid，若其中 pid 仍存活则退出。旧实例崩溃后 pid 已死，新实例
    可安全接管并覆盖 LOCK。
    """
    self_pid = os.getpid()
    other = _other_launcher_alive(self_pid)
    if other:
        _log(f"another launcher already running (pid={other}); exiting to avoid duplicate watchdog")
        sys.exit(0)
    if os.path.exists(LOCK):
        try:
            with open(LOCK, "r", encoding="utf-8") as lf:
                old_pid = int((lf.read().strip() or "0"))
            if is_alive(old_pid) and old_pid != self_pid:
                _log(f"another launcher already running (pid={old_pid}); exiting")
                sys.exit(0)
        except Exception:
            pass
    with open(LOCK, "w", encoding="utf-8") as lf:
        lf.write(str(self_pid))


def main() -> None:
    if not _acquire_launcher_singleton():
        # 第一防线: 内核互斥体已被其它 launcher 持有 → 直接退出, 不与其争抢
        sys.stderr.write("another launcher holds Global\\hcm_bridge_launcher_singleton; exiting\n")
        sys.exit(0)
    acquire_single_instance()  # 第二防线: 进程扫描 + LOCK 文件(兜底)
    _log("launcher started (watchdog self-heal; DETACHED chain; singleton ON)")
    wd_pid = None
    while True:
        try:
            if not is_alive(wd_pid):
                # 认领既有存活 watchdog, 避免反复 spawn 撞互斥体秒退(双 launcher 漏网时的自愈)
                existing = _find_existing_watchdog(os.getpid())
                if existing and is_alive(existing):
                    wd_pid = existing
                    _log(f"adopted existing watchdog pid={wd_pid}")
                else:
                    wd_pid = spawn_watchdog()
                    _log(f"spawned watchdog pid={wd_pid}")
            time.sleep(CHECK_INTERVAL)
        except Exception as exc:
            _log(f"launcher loop error: {exc}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
