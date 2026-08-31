#!/usr/bin/env python3
"""_tmf_then_retrain.py - TimesFM 抽取完成后的自动衔接（一次性 handoff）。

完成判据（按优先级）：
  1) 抽取日志 _tmf_extract.log 末尾出现「EXITCODE=0」- 由 _run_tmf_extract.bat 在
     python 进程退出后追加，是「抽取真正结束」的可靠信号（权重下载/模型加载/推理/落库
     全部完成）。EXITCODE 非 0 -> 判定失败，中止衔接（不拿脏数据重训）。
  2) 兜底：hcm_ai.timesfm_features 水位线(max bar_time)推进过阈值。但重跑场景下新窗口
     可能不含比旧水位线更晚的 bar（无新信号），水位线不前进，故仅作兜底。

触发后拉起 auto_retrain.py --once（build_labels -> quality_features ->
train_signal_quality -> 裁判 -> 切换 LightGBM 模型），把新 TimesFM 特征喂给质量模型。

健壮性：
  - Windows 命名互斥体单实例：重复拉起直接退出（幂等，不双触发）。
  - 完成后写 _tmf_retrain_done.flag，即使被误重启也只触发一次。
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

TOOLS = os.path.dirname(os.path.abspath(__file__))
DB_URL = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"
LOG_FILE = os.path.join(TOOLS, "_tmf_retrain.log")
EXTRACT_LOG = os.path.join(TOOLS, "_tmf_extract.log")
DONE_FLAG = os.path.join(TOOLS, "_tmf_retrain_done.flag")

WATERMARK_THRESHOLD = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
POLL_INTERVAL_S = 20
MAX_WAIT_S = 6 * 3600

MUTEX_NAME = "Global\\hcm_tmf_then_retrain"


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def acquire_singleton() -> bool:
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        acquire_singleton._handle = h  # 保活，避免互斥体随 GC 释放
        return True
    except Exception:  # noqa: BLE001
        return True


def extract_exitcode() -> str | None:
    try:
        if not os.path.exists(EXTRACT_LOG):
            return None
        with open(EXTRACT_LOG, "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()[-2000:]
        found = None
        for line in tail.splitlines():
            if line.startswith("EXITCODE="):
                found = line.split("=", 1)[1].strip()
        return found
    except Exception:  # noqa: BLE001
        return None


def current_watermark():
    import psycopg2
    conn = psycopg2.connect(DB_URL, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(bar_time) FROM hcm_ai.timesfm_features "
                "WHERE symbol='XAUUSD' AND time_frame='M5'"
            )
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def trigger_retrain():
    cmd = [sys.executable, os.path.join(TOOLS, "auto_retrain.py"), "--once"]
    log(f"run: {' '.join(cmd)}")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(cmd, cwd=TOOLS, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           timeout=120 * 60)
    except subprocess.TimeoutExpired:
        log("[ABORT] auto_retrain timeout (120min)")
        return False
    except Exception as e:  # noqa: BLE001
        log(f"[ABORT] auto_retrain spawn failed: {e}")
        return False
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    for line in out.splitlines()[-40:]:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("  |retrain| " + line + "\n")
        except Exception:  # noqa: BLE001
            pass
    if p.returncode == 0:
        log("=== retrain OK (rc=0) ===")
        return True
    log(f"[WARN] retrain rc={p.returncode} (see auto_retrain.log)")
    return False


def mark_done() -> None:
    try:
        with open(DONE_FLAG, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    if os.path.exists(DONE_FLAG):
        log("[info] DONE_FLAG exists; already triggered before, exit")
        sys.exit(0)
    if not acquire_singleton():
        log("[info] another instance running; exit")
        sys.exit(0)

    log("=== tmf_then_retrain watchdog started ===")
    log(f"watermark_threshold={WATERMARK_THRESHOLD.isoformat()}")
    deadline = time.time() + MAX_WAIT_S
    while True:
        ec = extract_exitcode()
        if ec is not None:
            if ec == "0":
                log("extract EXITCODE=0 -> extraction done; launching retrain")
                trigger_retrain()
                mark_done()
                sys.exit(0)
            log(f"[ABORT] extract EXITCODE={ec} (non-zero); skip retrain")
            sys.exit(2)
        try:
            mx = current_watermark()
        except Exception as e:  # noqa: BLE001
            log(f"[warn] watermark query failed (retry): {e}")
            mx = None
        if mx is not None and mx > WATERMARK_THRESHOLD:
            log(f"watermark advanced to {mx} -> launching retrain")
            trigger_retrain()
            mark_done()
            sys.exit(0)
        if time.time() > deadline:
            log("[TIMEOUT] extraction did not finish within window; abort")
            sys.exit(3)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
