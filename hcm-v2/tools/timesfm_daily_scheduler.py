#!/usr/bin/env python3
"""timesfm_daily_scheduler.py — TimesFM 特征「每日 T+1」增量抽取调度器（主机进程）。

定位（铁律 5.1 边界）：
  本调度器属**离线特征层**，只读 PG 的 K 线/信号，只写 hcm_ai.timesfm_features，
  **零下单影响、零交易决策影响**。它产出的特征是质量头的候选输入，
  在 §7.2 门禁达标前不参与任何决策（G0 影子模式）。

为什么必须增量 + 必须带 --lib-cache：
  相似度检索库（hist_sim 的分母）原实现每批从空库起步，导致 tmf_hist_sim
  依赖"该信号在本批内的位置"：单次整批 861 条时末段有 2048 条候选，
  而每日增量每批仅约 30 条。两侧分布不可比，混训会退化成批次伪影。
  传 --lib-cache 后启动时载入上批库、结束时回写，使「每日增量」与
  「单次整批」在数学上完全等价（每次都等价于"所有严格早于该信号的历史向量"）。

用法：
  python timesfm_daily_scheduler.py --once     # 立即跑一次并退出（手动/回填）
  python timesfm_daily_scheduler.py            # 常驻，每日 UTC 21:30 触发
  python timesfm_daily_scheduler.py --daemon --at-hour 21 --at-minute 30
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# ── 常量（禁止魔法数字散落 —— 铁律四）──
TOOLS = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(TOOLS, "timesfm_features.py")
MODEL_DIR = r"D:\.venv_timesfm\model\ms\timesfm-2.5-200m-pytorch"
PCA_FILE = os.path.join(TOOLS, "models", "tmf_pca_v1.pkl")
LIB_CACHE = os.path.join(TOOLS, "models", "tmf_hist_lib_v1.npz")
LOG_FILE = os.path.join(TOOLS, "_logs", "timesfm_daily.log")

# 用 127.0.0.1(强制 IPv4)：localhost 会解析到 ::1 被 wslrelay 劫持，连不上 docker 生产
DB_URL = "postgresql://hcm:hcm_dev_pwd@127.0.0.1:5432/hcm_v2"
REDIS_URL = "redis://127.0.0.1:6379"

SYMBOL = "XAUUSD"
TMF_VERSION = "tfm25_pca_v1_sig"
TIME_FRAME = "M5"          # 主周期，与整批抽取保持一致
PCA_DIM = 8                # 与 tmf_pca_v1.pkl 的 meta.pca_dim 一致
EMBED_NORM = "l2"          # 与 meta.embed_norm 一致
# normalize_inputs=False：PCA 拟合时即为 False，必须一致，否则脚本的一致性护栏会直接拒绝
NORMALIZE_INPUTS = False

HEARTBEAT_KEY = "hcm:ai:timesfm:daily"
LOG_MAX_BYTES = 20 * 1024 * 1024   # 单份日志上限 20MB（防撑爆磁盘，见 2026-08-27 教训）
LOG_BACKUPS = 3

# 触发时刻（UTC）：21:30 —— 纽约收盘(21:00/22:00)之后，且距 UTC 换日尚有余量，
# 保证"昨日"整日 K 线已入库完毕。
DEFAULT_AT_HOUR = 21
DEFAULT_AT_MINUTE = 30
DEFAULT_CHECK_INTERVAL_MIN = 30
DEFAULT_OVERLAP_DAYS = 2     # 回看天数：容忍跨日/停机导致的漏抽
DEFAULT_FALLBACK_START = "2026-08-10"   # 库里查不到水位线时的兜底起点
DEFAULT_TIMEOUT_MIN = 240    # 单次抽取超时（整批 861 条约 18 分钟，留足余量）

# 「没有新活」的良性退出：脚本以 SystemExit 抛出，属预期情况而非故障
BENIGN_MARKERS = (
    "窗口内无 HEXP 信号",
    "指定区间内无可用 bar",
)

# ── 单实例互斥（Windows 命名互斥体：内核级原子，进程退出自动释放）──
# 两把锁各司其职，不可合并：
#   DAEMON  锁：长驻调度器持有**整个生命周期**。锁只加在 launcher（拉起即退出的
#               短命进程）上是不够的——实测计划任务与手动启动各拉起一个调度器，
#               两个进程并存，会重复抽同一窗口并争抢写同一个 lib-cache 文件。
#   EXTRACT 锁：仅在执行抽取期间持有。使手动 `--once` 也不会与守护进程的定时
#               抽取并发写同一张表 / 同一个 npz。
MUTEX_DAEMON = "Global\\hcm_timesfm_daily_scheduler"
MUTEX_EXTRACT = "Global\\hcm_timesfm_daily_extract"
ERROR_ALREADY_EXISTS = 183

# 必须长期持有该句柄：若被 GC 回收，内核互斥体随即释放，锁形同虚设
_daemon_mutex_handle = None


def _acquire_mutex(name: str):
    """Windows 命名互斥体；已被占用返回 None；非 Windows / 不可用时返回 True（放行）。"""
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return None
        return h
    except Exception:  # noqa: BLE001
        return True  # 互斥体不可用时不阻塞启动（保守放行）


def _release_mutex(handle, name: str) -> None:
    """显式释放互斥体（仅用于短生命周期的 EXTRACT 锁）。

    DAEMON 锁**不可**调用本函数：它在调度器整个生命周期内都必须保持持有。
    """
    if handle is None or handle is True:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(int(handle))
    except Exception as e:  # noqa: BLE001
        log(f"[warn] release mutex {name} failed: {e}")


def log(msg: str) -> None:
    """写日志（带轮转）+ 打控制台。

    控制台打印**必须完全静默降级**：
    【2026-08-30 实测教训】Windows GBK 控制台无法编码某些字符时 print 抛
    UnicodeEncodeError；若"降级分支"再打印同一批字符（如子进程输出被
    errors='replace' 替换出的 U+FFFD）会二次抛出，直接崩掉调度器主循环——
    一次整批 18 分钟跑到 861/861 才崩，白跑。故此处不做任何重编码重试，
    只放弃控制台输出，日志落盘独立进行、不受影响。
    """
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            for i in range(LOG_BACKUPS, 0, -1):
                src = f"{LOG_FILE}.{i - 1}" if i > 1 else LOG_FILE
                dst = f"{LOG_FILE}.{i}"
                if os.path.exists(src):
                    os.replace(src, dst)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        # 日志写失败绝不能影响调度主流程
        print(f"[log-failed] {e}", flush=True)


def heartbeat(status: str, extra: dict | None = None) -> None:
    """存活心跳：写 Redis，供面板/运维判断调度器是否在线（铁律 11.1 判定依据）。"""
    try:
        import redis
        r = redis.Redis.from_url(REDIS_URL, socket_timeout=5, decode_responses=True)
        blob = {"pid": os.getpid(), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": status}
        if extra:
            blob.update(extra)
        import json
        # TTL 取 2 倍检查间隔 + 余量，避免"死守护"被误判为在线
        r.set(HEARTBEAT_KEY, json.dumps(blob, ensure_ascii=False), ex=48 * 3600)
    except Exception as e:  # noqa: BLE001
        log(f"[heartbeat] write failed (non-fatal): {e}")


def resolve_start(overlap_days: int, fallback_start: str) -> str:
    """起点 = 该版本已落库的最大 bar_time 回看 overlap_days 天（YYYY-MM-DD）。

    回看的必要性：若某天调度器未运行（停机/重启），不回看会永久漏掉那段信号。
    因落库是 ON CONFLICT DO UPDATE 幂等 upsert，回看重复抽取无副作用；
    且 --lib-cache 会按时间戳剔除不早于本批首目标的向量，不会污染检索库。
    """
    try:
        import psycopg2
        conn = psycopg2.connect(DB_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(bar_time) FROM hcm_ai.timesfm_features "
                    "WHERE symbol = %s AND time_frame = %s AND tmf_version = %s",
                    (SYMBOL, TIME_FRAME, TMF_VERSION),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row and row[0] is not None:
            start = (row[0] - timedelta(days=overlap_days)).date()
            log(f"watermark max(bar_time)={row[0]} -> start={start} (overlap={overlap_days}d)")
            return start.isoformat()
        log(f"[warn] 库内无 {TMF_VERSION} 水位线，使用兜底起点 {fallback_start}")
        return fallback_start
    except Exception as e:  # noqa: BLE001
        log(f"[warn] 水位线查询失败({e})，使用兜底起点 {fallback_start}")
        return fallback_start


def build_cmd(start: str, end: str) -> list[str]:
    """构造抽取命令（参数与历史整批严格一致，否则 embedding 分布不同 → 特征不可比）。"""
    cmd = [
        sys.executable, SCRIPT,
        "--extract",
        "--db-url", DB_URL,
        "--symbol", SYMBOL,
        "--model-dir", MODEL_DIR,
        "--version", TMF_VERSION,
        "--pca", PCA_FILE,
        "--pca-dim", str(PCA_DIM),
        "--embed-norm", EMBED_NORM,
        "--at-signal-times",
        "--lib-cache", LIB_CACHE,
        "--start", start,
        "--end", end,
    ]
    if NORMALIZE_INPUTS:
        cmd.append("--normalize-inputs")
    return cmd


def run_extraction(start: str, end: str, timeout_min: int) -> tuple[bool, str]:
    """执行一次增量抽取。返回 (是否成功, 结果摘要)。

    fail-open 语义：只有「抽取失败」才返回 False；「无新信号」属正常，返回 True。
    """
    if start >= end:
        return True, f"no-new-window (start={start} >= end={end})"
    cmd = build_cmd(start, end)
    log(f"run: start={start} end={end} timeout={timeout_min}min")
    log(f"cmd: {' '.join(cmd)}")
    # 子进程 stdout 走管道时，Windows 按 locale(GBK) 编码而非 UTF-8，
    # 若此处仍按 utf-8 解码，中文会全部变成 U+FFFD（实测触发过一次崩溃链）。
    # 故强制子进程以 UTF-8 输出，保证解码口径一致。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(
            cmd, cwd=TOOLS, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env,
            timeout=timeout_min * 60,
        )
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout_min} min"
    except Exception as e:  # noqa: BLE001
        return False, f"SPAWN_FAILED: {e}"

    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    # 只留最后几行，避免刷屏
    for line in out.splitlines()[-5:]:
        log(f"  | {line}")

    if p.returncode == 0:
        return True, "OK"
    # 良性退出判定：脚本以 SystemExit 报"无新活"，不算故障
    if any(m in err or m in out for m in BENIGN_MARKERS):
        return True, "NO_NEW_SIGNALS"
    for line in err.splitlines()[-8:]:
        log(f"  ! {line}")
    return False, f"FAILED rc={p.returncode}"


def trigger_retrain(timeout_min: int = 120) -> tuple[bool, str]:
    """抽取成功后衔接 LightGBM 自动重训（auto_retrain.py --once）。

    【自动调度 2026-08-30】把新 TimesFM 特征即时喂给质量模型：抽取落库后
    自动跑 build_labels -> quality_features -> train_signal_quality -> 裁判 -> 切换。

    fail-open 语义：仅记录结果，绝不因重训失败/超时影响抽取主流程（特征层可延迟，
    调度器本身必须先活着）。子进程继承本调度器解释器(sys.executable=venv pythonw)，
    故 lightgbm 等依赖可用。
    """
    cmd = [sys.executable, os.path.join(TOOLS, "auto_retrain.py"), "--once"]
    log(f"trigger_retrain: {' '.join(cmd)} (timeout={timeout_min}min)")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(cmd, cwd=TOOLS, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           timeout=timeout_min * 60)
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout_min}min"
    except Exception as e:  # noqa: BLE001
        return False, f"SPAWN_FAILED: {e}"
    for line in (p.stdout or "").splitlines()[-8:]:
        log(f"  |retrain| {line}")
    if p.returncode == 0:
        return True, "OK"
    return False, f"rc={p.returncode}"


def should_run(now: datetime, at_hour: int, at_minute: int, last_run_date: str | None) -> bool:
    """是否到达今日触发点且今日尚未跑过。"""
    if last_run_date == now.date().isoformat():
        return False
    return (now.hour, now.minute) >= (at_hour, at_minute)


def main() -> None:
    ap = argparse.ArgumentParser(description="TimesFM 特征每日 T+1 增量抽取调度器")
    ap.add_argument("--once", action="store_true", help="立即跑一次并退出（手动/回填）")
    ap.add_argument("--daemon", action="store_true", help="常驻循环（默认）")
    ap.add_argument("--at-hour", type=int, default=DEFAULT_AT_HOUR, help="触发时刻 UTC 时")
    ap.add_argument("--at-minute", type=int, default=DEFAULT_AT_MINUTE, help="触发时刻 UTC 分")
    ap.add_argument("--check-interval-min", type=int, default=DEFAULT_CHECK_INTERVAL_MIN)
    ap.add_argument("--overlap-days", type=int, default=DEFAULT_OVERLAP_DAYS)
    ap.add_argument("--fallback-start", default=DEFAULT_FALLBACK_START)
    ap.add_argument("--timeout-min", type=int, default=DEFAULT_TIMEOUT_MIN)
    # 回填专用：指定起点/终点，忽略水位线
    ap.add_argument("--force-start", default=None)
    ap.add_argument("--force-end", default=None)
    args = ap.parse_args()

    def _do_run() -> tuple[str, str]:
        # 抽取锁：仅在执行期间持有，防止手动 --once 与守护定时抽取并发写同表/同 npz
        lock = _acquire_mutex(MUTEX_EXTRACT)
        if lock is None:
            return "SKIPPED", "another extraction is already in progress"
        try:
            if args.force_start:
                start = args.force_start
            else:
                start = resolve_start(args.overlap_days, args.fallback_start)
            end = args.force_end or datetime.now(timezone.utc).date().isoformat()
            ok, summary = run_extraction(start, end, args.timeout_min)
            # 【自动调度 2026-08-30】抽取成功后衔接 LightGBM 自动重训，把新
            # TimesFM 特征即时喂给质量模型（fail-open：失败不影响抽取结果）。
            if ok:
                r_ok, r_summary = trigger_retrain()
                log(f"post-extract retrain: ok={r_ok} summary={r_summary}")
                if not r_ok:
                    summary = f"{summary}; retrain={r_summary}"
            return ("OK" if ok else "FAILED"), summary
        finally:
            _release_mutex(lock, MUTEX_EXTRACT)

    if args.once:
        log("=== once mode ===")
        status, summary = _do_run()
        log(f"=== done: status={status} summary={summary} ===")
        heartbeat(status, {"mode": "once", "summary": summary,
                           "last_run": datetime.now(timezone.utc).date().isoformat()})
        sys.exit(0 if status == "OK" else 1)

    # DAEMON 锁：整个生命周期持有。若已有调度器在跑（计划任务已拉起），
    # 这里会拿到 None，直接退出，避免重复抽取 + 争抢 lib-cache。
    global _daemon_mutex_handle
    _daemon_mutex_handle = _acquire_mutex(MUTEX_DAEMON)
    if _daemon_mutex_handle is None:
        log("another scheduler daemon already holds the singleton mutex; exiting")
        sys.exit(0)

    log(f"daemon started: at={args.at_hour:02d}:{args.at_minute:02d} UTC "
        f"check={args.check_interval_min}min overlap={args.overlap_days}d "
        f"pid={os.getpid()}")
    last_run_date: str | None = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            if should_run(now, args.at_hour, args.at_minute, last_run_date):
                status, summary = _do_run()
                log(f"daily run done: status={status} summary={summary}")
                if status == "OK":
                    last_run_date = now.date().isoformat()
                heartbeat(status, {"mode": "daemon", "summary": summary,
                                   "last_run": last_run_date})
            else:
                heartbeat("IDLE", {"mode": "daemon", "last_run": last_run_date,
                                   "next_due": f"{args.at_hour:02d}:{args.at_minute:02d}Z"})
        except Exception as e:  # noqa: BLE001
            # 主循环绝不允许因异常退出：特征层可延迟，调度器本身必须先活着
            log(f"[daemon] loop crashed (non-fatal): {e}")
            try:
                heartbeat("ERROR", {"mode": "daemon", "error": str(e)})
            except Exception:  # noqa: BLE001
                pass
        time.sleep(max(60, args.check_interval_min * 60))


if __name__ == "__main__":
    main()
