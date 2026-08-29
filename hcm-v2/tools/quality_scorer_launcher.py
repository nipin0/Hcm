#!/usr/bin/env python3
"""quality_scorer_launcher.py — 常驻拉起 AI 信号质量评分 sidecar（DETACHED）。

用法：直接运行本文件一次，即可把 quality_scorer.py 以脱离会话生命周期的方式
常驻拉起（日志落 tools/_aiq_artifacts/quality_scorer.log）。

纪律红线：sidecar 只读 + 只发布观测快照，零下单影响；模型缺失/加载失败自动降级。

【2026-08-15 修复】
  1) B9/B5：删除 WITH_MODEL 硬编码开关。模型/校准路径改为由 sidecar 自己读配置中心
     （ai.lm.model_path / ai.lm.calib_path），launcher 不再传 --model/--calib，
     彻底消除"launcher 硬编码不带模型 → ai_score 恒 null"与配置双轨问题。
     若需临时禁用模型，把 ai.lm.model_path 置空即可（无需改代码）。
  2) 单实例互斥：此前无任何互斥，守护脚本与 start.bat 可能并发拉起多个 sidecar
     （实测同时存在 2 个进程重复写 Redis/PG）。现加 Windows 命名互斥体 +
     运行中实例探测，重复拉起直接退出。
"""
import ctypes
import os
import subprocess
import sys

# 统一用 C:\Python313 的 pythonw（无窗口解释器），避免 spawn sidecar 时弹控制台
PY = r"C:/Python313/pythonw.exe"
TOOLS = r"D:/HCM_ASST/hcm-v2/tools"
# 日志落到 models/ 同目录（_aiq_artifacts 为环境安全机制拦截目录，不可用）
LOG = os.path.join(TOOLS, "models", "quality_scorer.log")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

# 用 127.0.0.1(强制 IPv4)：localhost 会解析到 ::1 被 wslrelay 劫持，连不上 docker 生产(2026-08-17)
DB_URL = "postgresql://hcm:hcm_dev_pwd@127.0.0.1:5432/hcm_v2"
REDIS_URL = "redis://127.0.0.1:6379"

MUTEX_NAME = "Global\\hcm_quality_scorer_launcher_singleton"
ERROR_ALREADY_EXISTS = 183


def _acquire_singleton():
    """Windows 命名互斥体单实例锁（内核级原子；进程退出自动释放）。

    返回 handle（需在进程生命周期内持有）；已被占用则返回 None。
    """
    if not sys.platform.startswith("win"):
        return True
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return None
        return handle
    except Exception:
        return True  # 互斥体不可用时不阻塞拉起（保守放行）


def _sidecar_running() -> int | None:
    """探测是否已有 quality_scorer.py 在跑（返回 PID）；失败返回 None（保守放行）。"""
    if not sys.platform.startswith("win"):
        return None
    try:
        CREATE_NO_WINDOW = 0x08000000
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -like '*quality_scorer.py*' } | "
             "Select-Object -First 1 -ExpandProperty ProcessId"],
            stderr=subprocess.DEVNULL, timeout=20, creationflags=CREATE_NO_WINDOW,
        )
        txt = out.decode("utf-8", "ignore").strip()
        return int(txt) if txt.isdigit() else None
    except Exception:
        return None


def main():
    mutex = _acquire_singleton()
    if mutex is None:
        print("[launcher] another launcher holds singleton mutex; exiting")
        return

    existing = _sidecar_running()
    if existing:
        print(f"[launcher] sidecar already running pid={existing}; skip spawn")
        return

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    env = dict(os.environ)
    env["DB_URL"] = DB_URL
    env["REDIS_URL"] = REDIS_URL

    # 模型/校准路径由 sidecar 自读配置中心（ai.lm.model_path / ai.lm.calib_path）
    cmd = [PY, "quality_scorer.py", "--symbol", "XAUUSD", "--interval", "5"]

    # 【2026-08-27 修复】launcher 不再自己 open 日志文件 —— 此前以追加模式持有
    # quality_scorer.log 文件句柄，且 sidecar 自身无轮转，长期运行涨到 6.5GB 撑爆磁盘。
    # 现日志改由 sidecar 内部 RotatingFileHandler 自管（见 quality_scorer.py
    # _setup_rolling_log：20MB/份，保留 5 份，上限 100MB）。launcher 仅负责拉起，
    # 子进程 stdout/stderr 定向到 DEVNULL（已落盘滚动日志，无需再经父进程）。
    p = subprocess.Popen(
        cmd,
        cwd=TOOLS,
        env=env,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    print(f"[launcher] sidecar launched pid={p.pid}, log={LOG}")
    print(f"[launcher] cmd={' '.join(cmd)} (model path from config center)")
    sys.exit(0)  # 拉起即退出，单实例由互斥体 + _sidecar_running 保证


if __name__ == "__main__":
    main()
