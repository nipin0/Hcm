#!/usr/bin/env python3
"""MT5 Bridge — 连接真实 MT5，替换模拟 K 线和 stub 下单。

功能:
  1. 从 MT5 拉取实时 XAUUSD M5 K 线 → PG + Redis
  2. 实时推送价格 → Redis
  3. 监听 Redis signal:risk_passed → MT5 下单
  4. 下单后更新信号状态 + 写入持仓

配置:
  从 Redis hcm:config:v2 读取 mt5.server_address / mt5.account_number / mt5.password
  在 MT5 接入页面 (localhost:3000/system/mt5) 修改后即时生效

用法:
  python mt5_bridge.py
  python mt5_bridge.py --dry-run
  python mt5_bridge.py --max-retries=10
"""

import os
import sys
import json
import time
import asyncio
import logging
import math
import subprocess
from typing import Optional
from datetime import datetime, timezone, timedelta

# 模块级 logger（与 signal-tower 各模块约定一致）。
# 注意：本文件主循环逻辑统一使用 `log`（见下方 main loop 处理器），此处额外定义
# `logger` 是为 `_resolve_max_signal_age_seconds` 等辅助函数里的 `logger.critical`
# 兜底告警服务——该变量此前被遗漏定义，导致 Redis 缺键时抛
# `NameError: name 'logger' is not defined`，进而主循环每轮在信号年龄阈值解析处崩溃、
# 跳过后续的存活键续期/信号消费，桥半死、信号塔断粮。
logger = logging.getLogger(__name__)

# 捕获原生崩溃(如 MetaTrader5 原生 DLL 加载/调用失败)并 dump 堆栈到 stderr，
# 便于定位"重拉桥零输出静默死"等难复现问题。stderr 由看门狗重定向到
# bridge_launch_*.log 兜底。
import faulthandler
faulthandler.enable()

import MetaTrader5 as _mt5_global  # top-level import for reliable order_send

# Lazy import — resolved when position_sync.py exists (T02+)
# Re-imported in T04 when actually called from main()
from position_sync import sync_positions, _record_after_close_cooldown  # noqa: F401

# Live bar Redis key — inline to avoid cross-module import (bridge runs on Windows host)
LIVE_BAR_KEY_TEMPLATE = "market:bar:{symbol}:{timeframe}:live"
LIVE_BAR_TTL_SECONDS = 600

# 单条信号过期阈值（秒）—— 超过即拒绝下单。可由 Redis bridge.max_signal_age_seconds 热改。
# 2026-07-27 修正：缺省阈值与「真实交易信号主信号生产 → 执行」链路延迟适配。
#   真实链路：信号塔 scheduler 生产主信号 → signal:stream → 风控引擎消费+评估
#   → signal:risk_passed → 主机桥消费下单，跨容器+主机进程、高峰期/流积压下延迟
#   轻松达数十秒、极端可达数百秒。旧缺省 10s 与该延迟严重不适配——一旦 Redis 热键
#   bridge.max_signal_age_seconds 因任何原因丢失（本系统 Redis 曾多次被清/重建），
#   回退 10s 缺省会把所有正常真实主信号当过期拒绝 → 系统性「出信号不下单」。
#   现对齐当前生产热值 180s 作为安全兜底（仍可由 Redis 热改覆盖）。
DEFAULT_MAX_SIGNAL_AGE_SECONDS = 180

# 2026-08-05 (D7-2): 信号年龄阈值安全下限。低于此值会被强制抬到下限——
# 过低的 max_signal_age_seconds(如旧缺省 10s)会让正常真实信号被当过期拒绝，
# 导致系统性「出信号不下单」。缺失键不崩溃(回退 DEFAULT 安全值 + CRITICAL 告警)。
MIN_MAX_SIGNAL_AGE_SECONDS = 60

# ── reconcile「补缺失」闸门（2026-08-10，用户明确要求）──────────────
# 已漏掉的跟单不补：行情已错过，按现价补进去等于错价追单，主号 SL 对跟单号
# 而言可能已是即刻触发 → 补即止损。故补开只允许「刚开的新单」通过：
#   ① 时效闸门：主号开仓距今超过此秒数即视为历史单，永久放弃（不补）。
#      120s 覆盖「信号流复制瞬时抖动/跟单桥秒级重启」等真实需要兜底的场景，
#      同时排除桥启动前的老单与镜像事件早已丢失的陈年单。
#   ② 滑点闸门：即便在时效内，若现价已偏离主号开仓价超过此倍数的 ATR，
#      说明急速行情中入场位已失效，同样放弃（0.3×ATR 与桥内滑点口径同量级）。
# 两闸门任一不过 → 写 bridge:backfill_skip 标记永久放弃，不再重复尝试/刷日志。
RECONCILE_BACKFILL_MAX_AGE_SEC = 120
RECONCILE_BACKFILL_MAX_SLIP_ATR = 0.3  # 主号开仓价偏离 > 0.3×ATR 视为行情已走远，不追（防补即止损）
# 主号快照新鲜度闸门：快照 updated_at 距今超过此秒数（>2×TTL=30s）即视为
# 主号桥离线/抖动 → 跟单桥对账与 SL/TP 跟随跳过本轮（不误平、不盲补、不盲改）。
MASTER_SNAPSHOT_MAX_AGE_SEC = 40  # 主号快照新鲜度闸门：超龄即跳过本轮对账（防误平/盲补）。2026-08-18 曾临时放宽到 3600 以临时治标，2026-08-21 收回：用户要求「错过不再补单」，陈年单快照不应再判新鲜触发补开。


def _resolve_max_signal_age_seconds(redis_conn) -> int:
    """读取 bridge.max_signal_age_seconds，强制安全下限 MIN_MAX_SIGNAL_AGE_SECONDS。

    缺失 → 回退 DEFAULT_MAX_SIGNAL_AGE_SECONDS(180, 安全) 并 CRITICAL 告警(不崩溃，
    避免 Redis 清键导致系统性不下单)；越界/非法 → 同上；低于下限 → 抬到下限并告警。
    """
    try:
        raw = redis_conn.hget("hcm:config:v2", "bridge.max_signal_age_seconds")
    except Exception:
        raw = None
    if raw is None:
        logger.critical(
            "bridge.max_signal_age_seconds missing in Redis — falling back to safe default %ds "
            "(reseed via config_provider.set to make it explicit)",
            DEFAULT_MAX_SIGNAL_AGE_SECONDS,
        )
        return DEFAULT_MAX_SIGNAL_AGE_SECONDS
    try:
        sval = raw.decode() if isinstance(raw, bytes) else str(raw)
        val = int(sval)
    except (TypeError, ValueError):
        logger.critical(
            "bridge.max_signal_age_seconds invalid (%r) — falling back to safe default %ds",
            raw, DEFAULT_MAX_SIGNAL_AGE_SECONDS,
        )
        return DEFAULT_MAX_SIGNAL_AGE_SECONDS
    if val < MIN_MAX_SIGNAL_AGE_SECONDS:
        logger.critical(
            "bridge.max_signal_age_seconds=%ds below safety floor %ds — raised to %ds "
            "(too-low age would reject real signals as stale)",
            val, MIN_MAX_SIGNAL_AGE_SECONDS, MIN_MAX_SIGNAL_AGE_SECONDS,
        )
        return MIN_MAX_SIGNAL_AGE_SECONDS
    return val


class InstanceLockedError(Exception):
    """单实例锁被其他 bridge 实例持有时抛出，使进程干净退出而非无限重试成僵尸。"""
    pass


def _pid_alive(pid) -> bool:
    """跨平台判断 PID 是否存活（Windows 用 OpenProcess；其它用 os.kill(pid, 0)）。

    用于单实例锁「持锁者是否为存活进程」判定：
      - 存活 → 本实例进入 standby（不抖动、不重复下单）；
      - 已死 → 安全接管(steal)，避免依赖锁 TTL 被动过期造成的时间窗空档/抖动断桥。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if h:
            k.CloseHandle(h)
            return True
        return False
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
        except Exception:
            return False


def _publish_bridge_heartbeat(redis_conn, alive_key, alive_ttl, my_lock_id, live_login,
                              account_id, is_master, is_follower, acc, terminal_path) -> bool:
    """持锁权威桥发布存活心跳 + 处理 restart 控制信令。

    返回 True 表示收到 bridge:control:<login>=restart，调用方应主动退出由主机看门狗重拉。
    """
    try:
        payload = json.dumps({
            "pid": my_lock_id,
            "login": live_login,
            "account_id": account_id,
            "role": "master" if is_master else ("follower" if is_follower else "standalone"),
            "server": (acc or {}).get("server_name"),
            "terminal": terminal_path,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        redis_conn.set(alive_key, payload, ex=alive_ttl)
    except Exception:
        pass
    try:
        ctl = redis_conn.get(f"bridge:control:{live_login}")
        if ctl and str(ctl).strip().lower() == "restart":
            try:
                redis_conn.delete(f"bridge:control:{live_login}")
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


def _maybe_run_backup(redis_conn) -> None:
    """监听全备份触发键，收到即用 Popen 后台启动宿主 PowerShell 备份脚本。

    设计：
      - 任意桥实例(主号/跟单)每轮都检查触发键，但用「先 get 再 delete、仅 delete
        返回 1 者」做原子竞争，确保多桥下只有一台真正执行，避免重复备份。
      - 备份脚本在 Windows 宿主运行（含 robocopy 源码、docker exec PG/Redis），
        脚本自身写入 hcm:backup:status(running/done/failed) 供前端轮询。
      - Popen 仅 spawn 子进程、立即返回，不阻塞主循环下单/追利。
    """
    try:
        trig = redis_conn.get(BACKUP_TRIGGER_KEY)
        if not trig:
            return
        if redis_conn.delete(BACKUP_TRIGGER_KEY):  # 仅赢家(删除成功)执行
            log.info("收到全备份触发 — 后台启动宿主备份脚本: %s", BACKUP_SCRIPT)
            try:
                redis_conn.set(BACKUP_STATUS_KEY, json.dumps({
                    "status": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }), ex=3600)
            except Exception:
                pass
            try:
                subprocess.Popen(
                    ["powershell", "-ExecutionPolicy", "Bypass", "-File", BACKUP_SCRIPT,
                     "-BackupRoot", BACKUP_ROOT],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as exc:
                try:
                    redis_conn.set(BACKUP_STATUS_KEY, json.dumps({
                        "status": "failed", "error": str(exc),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    }), ex=3600)
                except Exception:
                    pass
    except Exception:
        pass


# 加固C：bridge 单实例互斥锁——防止多次启动累积多实例导致"重复下单"。
# 值=本进程PID；TTL 30s，主循环每 ~10s 续期；进程崩溃后锁 TTL 到期自动释放，下个实例可接管。
# per-account 模式下改为 bridge:instance:lock:<account_id>，使每账号独立互斥——
# 多机部署时各机抢同一把账号锁，仅一台赢 → 天然负载分布 + 故障自动接管（扩展性布局的张力）。
BRIDGE_LOCK_KEY = "bridge:instance:lock"
BRIDGE_LOCK_TTL = 30
BRIDGE_LOCK_RENEW_INTERVAL = 10

# 存活心跳：运行态重写为 bridge:alive:<live_login>（与锁键同一动态身份，非硬编码、非角色组）。
# 仅由"持有单实例锁的权威桥"续期，TTL 600s、主循环每 ~10s 刷新；进程崩溃/被锁驱逐后键到期 →
# 监控可判定该 login 的桥已死亡（配合锁机制避免重复实例漏单的监控手段）。换经纪商=新终端新 login
# → 心跳键自动按新 login 生成，即插即用。
BRIDGE_ALIVE_KEY = "bridge:alive"
BRIDGE_ALIVE_TTL = 600

# 全备份触发：前端「全备份」按钮 → 后端 /api/v1/system/backup 写 hcm:backup:trigger
# → 本常驻进程(主机 Windows)监听并在宿主执行 PowerShell 备份脚本（PG+Redis+源码到 D:\HCM_ASST\backup）。
# 多桥实例并发竞争：仅 Redis delete 返回 1 的赢家执行，避免双备份。脚本自身经
# hcm:backup:status 回报进度(running/done/failed)，前端轮询展示。
BACKUP_TRIGGER_KEY = "hcm:backup:trigger"
BACKUP_STATUS_KEY = "hcm:backup:status"
BACKUP_SCRIPT = r"D:\HCM_ASST\backup_hcm.ps1"
BACKUP_ROOT = r"D:\HCM_ASST\backup"

# 消费组：默认单一组 bridge-order-group；per-account 模式改为 group:<account_id>。
# 每账号独立消费组 → 广播模型下各组独立消费 signal:risk_passed，bridge 按自身 account_id 过滤。
# 模块级默认（legacy 单账号模式）；__main__ 中若设了 --account-id 会被重写为 group:<account_id>。
BRIDGE_GROUP = "bridge-order-group"

# per-account 模式开关：由动态发现填充（终端实读 login → 反查 PG account_id）；None=未确定。
ACCOUNT_ID_MODE = None
# ── 角色驱动（根治“跟单不下单 / 建桥硬编码”）：从 hcm_broker.accounts.account_type 读取，零硬编码 ──
#    IS_FOLLOWER 桥复制 FOLLOW_MASTERS 中主号发出的全部自动信号（模式无关）；手数按跟随倍率缩放。
IS_MASTER = False
IS_FOLLOWER = False
FOLLOW_MASTERS = set()      # 本跟单桥要复制的主号 account_id 集合（来自 hcm_copy.relationships, running）
FOLLOW_CIRCUIT_BROKEN = False  # 跟单号每日盈亏熔断内存标志（仅跟单桥；触发后停跟当日，主号不受影响）
_FOLLOW_CIRCUIT_LAST_LOG_TS = 0.0  # 熔断诊断日志节流计时（模块级）
_FOLLOW_CIRCUIT_WAS_BROKEN = False  # 熔断→恢复状态机（用于恢复宽限期判定）
_FOLLOWER_CIRCUIT_RECOVERED_AT = 0.0  # 最近一次从熔断恢复的时间戳（宽限期内不重熔断）
FOLLOW_LOT_MULT = {}        # {主号 account_id: lot_multiplier} 跟单手数倍率（全复制主号，手数随跟随比例）
# 动态发现模式：由 --terminal-path=P 设定（看门狗按真实终端路径拉起）。
# None=未指定 → main() 自动扫描本机运行的 terminal64.exe；仍无则回退 MT5_PATH。
TERMINAL_PATH_MODE = None

def _load_bridge_env():
    """启动时从同目录 bridge_config.env 读取配置（手动可改的独立配置文件）。
    环境变量优先于本文件；本文件优先于下方默认值。跨机部署只需改 .env 文件。
    """
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_config.env")
    if not os.path.exists(cfg):
        return
    with open(cfg, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            os.environ.setdefault(k, v)  # 环境变量已设则不动，否则用文件值

_load_bridge_env()

PG_DSN = os.getenv("PG_DSN", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SYMBOL = "XAUUSD"
# ── 跨券商品种名映射（数据驱动，零硬编码）──
# 内部全链路统一用逻辑名 SYMBOL（PG klines / Redis 信号 / ATR 缓存键均不变）；
# 仅在调用 MT5 API 的边界（行情/下单）按本账户 broker_name 翻译成该券商实盘名。
# 映射来源：hcm_copy.symbol_mappings（WHERE follower_broker=本账户 broker_name AND is_active）；
# 换券商只需 INSERT/UPDATE 该 PG 表，代码零硬编码。
BROKER_NAME = None   # 动态发现后由 main() 填充（acc["broker_name"]）
SYMBOL_MAP = {}      # {逻辑名: 实盘名}，如 {"XAUUSD": "XAUUSD_"}
DEFAULT_TIMEFRAME = "M5"
DEFAULT_TIMEFRAMES = ["M5", "M1"]   # P2-7: multi-timeframe collection
TIMEFRAME_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440,
}
MT5_TIMEFRAME_MAP = {
    "M1": None, "M5": None, "M15": None, "M30": None,
    "H1": None, "H4": None, "D1": None,
}  # populated after mt5 import


def real_symbol(logic: str) -> str:
    """逻辑名 → 本账户实盘名。无映射/无 broker 时原样返回（向后兼容）。

    只在 MT5 API 边界调用；PG/Redis 内部键继续使用逻辑名，不受影响。
    """
    if BROKER_NAME is None or not SYMBOL_MAP:
        return logic
    return SYMBOL_MAP.get(logic, logic)


def logic_symbol(real: str) -> str:
    """实盘名 → 逻辑名（SYMBOL_MAP 逆映射）。无映射时原样返回。

    【P0-6 2026-08-03】跟单对账用：主/跟单终端品种名可能不同（历史实证
    XAUUSD 与 XAUUSD_ 并存），按品种计数对账前必须归一到逻辑名空间，
    否则 master_count 查不到 → mcount=0 → 误平跟单该品种全部持仓。
    """
    if not SYMBOL_MAP:
        return real
    for _logic, _real in SYMBOL_MAP.items():
        if _real == real:
            return _logic
    return real


def _broker_candidates(name):
    """生成 broker 名候选列表，容忍 PG 登记名与 MT5 返回名之间的细微差异。

    真实案例：accounts.broker_name='Mega' 与 symbol_mappings.follower_broker
    曾误填 'MegaFusionGroupPty'；或 MT5 运行时返回 'MegaFusionGroupPty-Trade'
    而登记为 'MegaFusionGroupPty'。精确匹配失败会导致 SYMBOL_MAP 为空→跟单桥
    拿逻辑名去实盘取不到 tick→所有跟单下单失败。

    候选含：原始、小写、去常见无意义后缀词（trade/demo/live/financial 等）、
    去末尾 '-xxx' 段。去重保序。
    """
    out = []
    n = (name or "").strip()
    if not n:
        return out
    out.append(n)
    out.append(n.lower())
    low = n.lower()
    for suf in ("-trade", "trade", "-demo", "demo", "-live", "live",
                "-live5", "live5", "financial", "financial-demo", "-server"):
        if low.endswith(suf):
            out.append(n[: -len(suf)].strip())
            break
    if "-" in n:
        out.append(n.rsplit("-", 1)[0].strip())
    seen = set()
    result = []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            result.append(x)
    return result


async def load_symbol_map(pool, broker_name):
    """从 hcm_copy.symbol_mappings 读取本券商品种映射（数据驱动，零硬编码）。

    follower_broker 匹配本账户 broker_name；返回 {master_symbol: follower_symbol}。
    换券商只需 INSERT/UPDATE 该 PG 表，代码无需改动。
    匹配采用归一化候选（见 _broker_candidates），容忍 broker 名后缀差异导致失配。
    """
    if not broker_name:
        return {}
    candidates = _broker_candidates(broker_name)
    try:
        rows = await pool.fetch(
            "SELECT master_symbol, follower_symbol FROM hcm_copy.symbol_mappings "
            "WHERE follower_broker = ANY($1) AND is_active=true",
            candidates,
        )
    except Exception as exc:
        log.warning(f"load_symbol_map failed (broker={broker_name}): {exc}")
        return {}
    mapping = {}
    for r in rows:
        m, f = r["master_symbol"], r["follower_symbol"]
        if m and f:
            mapping[m] = f
            mapping[m.upper()] = f
    return mapping

MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mt5_bridge")
# 注意：每桥各自独立日志文件（按 login 命名），避免多桥共用 bridge.log 在 Windows 下
# 并发写触发 PermissionError（曾导致桥在写错误日志时崩溃退出）。文件句柄在 main() 发现
# 到 login 后按需添加；stderr 仍由看门狗重定向到 bridge_launch.log 作兜底。
_BRIDGE_LOG_DIR = r"D:\HCM_ASST\hcm-v2\tools"


def _load_mt5_config(redis_conn):
    """读取行情数据源配置（timeframe / symbols / max_bars 等非账户项）。

    重要（账户零硬编码铁律）：本函数**绝不**读取任何账户凭证。
    login / server / password 一律由 `_discover_account(终端实登账号)`
    → 反查 `hcm_broker.accounts`(PG) 提供，main() 中覆盖写入 mt5_cfg。
    这里只负责行情参数，杜绝从 Redis 残留键(mt5.account_number 等)
    引入任何形式的硬编码账户。
    """
    cfg = redis_conn.hgetall("hcm:config:v2") or {}
    # Read timeframe from datasource config, with M5 as default
    tf_str = cfg.get("datasource.timeframe", DEFAULT_TIMEFRAME)
    if not tf_str or tf_str.strip() == "":
        tf_str = DEFAULT_TIMEFRAME
    # P2-7: read additional timeframes (comma-separated, e.g. "M1,M15")
    tfs_str = cfg.get("datasource.timeframes", "")
    extra_tfs = []
    if tfs_str and tfs_str.strip():
        extra_tfs = [t.strip() for t in tfs_str.split(",") if t.strip() and t.strip() != tf_str]
    # Dedup + merge: primary timeframe first, then extras
    all_tfs = [tf_str]
    for t in extra_tfs:
        if t not in all_tfs:
            all_tfs.append(t)
    # Read max_bars from config
    max_bars_str = cfg.get("datasource.max_bars", "500")
    try:
        max_bars = int(max_bars_str)
    except (ValueError, TypeError):
        max_bars = 500
    # Read symbols from config (default to XAUUSD)
    symbols_str = cfg.get("datasource.active_symbols", "XAUUSD")
    symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
    if not symbols:
        symbols = ["XAUUSD"]
    return {
        "timeframe": tf_str,
        "timeframes": all_tfs,          # P2-7: all collected timeframes
        "max_bars": max_bars,
        "symbols": symbols,
    }


def connect_mt5(login, server, password, terminal_path=None):
    try:
        import MetaTrader5 as mt5
    except ImportError:
        log.critical("MetaTrader5 not installed. Run: pip install MetaTrader5")
        sys.exit(1)

    # Populate MT5_TIMEFRAME_MAP after import
    MT5_TIMEFRAME_MAP.update({
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
    })

    # terminal_path: per-account 自定义终端路径（多终端/多机）；None=使用默认 MT5_PATH
    path = terminal_path or MT5_PATH
    log.info(f"Connecting MT5: login={login}, server={server}, terminal_path={path}")
    if not mt5.initialize(path=path, login=login, server=server, password=password):
        log.error(f"MT5 auth failed: {mt5.last_error()}")
        log.error("请在 localhost:3000/system/mt5 修改账户信息后重试")
        return None
    log.info("MT5 connected")
    # Diagnostic: check trade permissions
    info = mt5.account_info()
    if info:
        log.info(f"Account: balance={info.balance}, equity={info.equity}, "
                 f"trade_allowed={info.trade_allowed}, trade_expert={info.trade_expert}")
        if not info.trade_allowed:
            log.error("⚠️  MT5 account trade_allowed=False — check terminal settings!")
        if not info.trade_expert:
            log.error("⚠️  MT5 account trade_expert=False — algorithmic trading may be disabled!")
    else:
        log.error("Cannot retrieve MT5 account_info — check login status")
    ti = mt5.terminal_info()
    if ti:
        log.info(f"Terminal: community_account={ti.community_account}, "
                 f"connected={ti.connected}, trade_allowed={ti.trade_allowed}")
    # Detect broker timezone offset
    broker_utc_offset_hours = 0
    try:
        tick = mt5.symbol_info_tick("XAUUSD")
        if tick and tick.time:
            import time as _time_lib
            broker_utc_offset_hours = round(
                (tick.time - int(_time_lib.time())) / 3600.0
            )
            log.info(f"Broker timezone offset: UTC{broker_utc_offset_hours:+d}h (detected from tick.time)")
    except Exception:
        log.warning("Cannot detect broker timezone, assuming UTC+0")
    mt5._broker_utc_offset_s = broker_utc_offset_hours * 3600  # attach to mt5 object
    return mt5


def fetch_klines(mt5, symbol, timeframe_str, count=500):
    """Fetch klines for a symbol with dynamic timeframe.

    Args:
        mt5: MT5 connection object.
        symbol: Trading symbol (e.g. XAUUSD).
        timeframe_str: Timeframe string (M1/M5/M15/H1/H4/D1).
        count: Number of bars to fetch.

    Returns:
        List of rate dicts or empty list.
    """
    mt5_tf = MT5_TIMEFRAME_MAP.get(timeframe_str, mt5.TIMEFRAME_M5)
    real = real_symbol(symbol)  # 翻译为实盘名（MT5 API 边界）
    rates = mt5.copy_rates_from_pos(real, mt5_tf, 0, count)
    if rates is None:
        return []
    try:
        l = len(rates)
        if l > 0:
            return list(rates)
    except:
        pass
    return []


# 各时间框架秒数（用于判定 K 线是否已收盘，防止写入未来棒）
TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


def _align_bar_open_epoch(epoch: int, bar_sec: int):
    """把 K 线 open_time（unix 秒）圆整到周期网格最近边界。

    MT5 返回的 bar open 时间按经纪商时区对齐，桥落库/写 Redis 时减去
    ``broker_utc_offset_s`` 转 UTC。当该偏移不是周期网格的整数倍时
    （H4=14400、D1=86400 对整小时偏移取余≠0，H1/M5 因周期整除 3600
    自动对齐），落地的 open_time 会偏离 UTC 网格（出现 1/2/3 小时错位），
    污染多周期共振矩阵。系统约定全周期在 UTC 网格对齐，故统一圆整到
    最近的 ``bar_sec`` 边界。epoch 0 = UTC 午夜，对所有周期整除，因此
    ``epoch % bar_sec == 0`` 即正确对齐。

    返回 ``(snapped_epoch, deviation_sec)``；deviation 用于可观测日志。
    """
    snapped = int(round(epoch / bar_sec)) * bar_sec
    return snapped, abs(snapped - epoch)


# 错位超过该秒数才告警（过滤亚分钟级 epoch 取整噪声；真实错位为整小时级）
ALIGN_WARN_SECONDS = 60


def _rate_get(r, name, default=0):
    """Safely read an optional field from an MT5 rate namedtuple.

    Older MT5 builds may not expose ``spread`` / ``real_volume``; fall back
    to ``default`` instead of raising KeyError. Used by write_klines_to_pg.
    """
    try:
        v = r[name]
    except (KeyError, IndexError, TypeError):
        return default
    return v if v is not None else default


async def write_klines_to_pg(pool, rates, symbol, timeframe_str, broker_utc_offset_s: int = 0):
    """Write kline bars to PostgreSQL.

    Args:
        pool: asyncpg connection pool.
        rates: List of rate dicts from MT5.
        symbol: Trading symbol.
        timeframe_str: Timeframe string (M1/M5/...).
        broker_utc_offset_s: Broker timezone offset in seconds (detected at connect).
    """
    count = 0
    bar_sec = TF_SECONDS.get(timeframe_str, 300)
    now_utc = int(time.time())
    # 修复：不再无差别跳过最后一棒（正在形成的棒）。
    # 旧逻辑 rates=rates[:-1] 对 M1/M5 仅滞后 1~5 分钟影响小，但对高周期致命：
    # H1 形成棒(如 10:00)被永久跳过 → PG 停在上一根(09:00)；
    # H4 形成棒(如 09:00)被跳过 → 停在 05:00；D1 形成棒(全天)被跳过 → 停在数天前。
    # 这直接让 hexp 多周期共振矩阵(M5/H1/H4/D1)读到的 H1/H4/D1 是上一根甚至上一天的棒，
    # 表现为“行情源断开 / K线入库 0/4”。
    # 改为只跳过 open_time 严格落在未来（含经纪商时钟超前 / 周期边界 MT5 预生成的下一根）
    # 的棒；正在形成的棒 open_time 在过去，照常写入并随实时行情持续更新（ON CONFLICT 合并）。
    for r in rates:
        raw_epoch = int(r['time']) - broker_utc_offset_s
        # 周期对齐校验：把 open_time 圆整到最近 bar_sec 边界，消除
        # broker_utc_offset_s 非网格整数倍造成的 H4/D1 错位（详见 _align_bar_open_epoch）。
        snapped_epoch, dev = _align_bar_open_epoch(raw_epoch, bar_sec)
        if dev > ALIGN_WARN_SECONDS:
            log.warning(
                "K-line open_time misaligned (%s %s): raw=%s dev=%ss -> snapped=%s",
                symbol, timeframe_str, raw_epoch, dev, snapped_epoch,
            )
        ts = datetime.fromtimestamp(snapped_epoch, tz=timezone.utc)
        # 仅跳过 open_time 严格属于未来的棒（经纪商时钟超前 / 周期边界预生成下一根）；
        # 正在形成的棒 open_time 在过去，正常写入。+10s 容忍极小时钟抖动。
        if snapped_epoch > now_utc + 10:
            continue
        insert_sql = """
            INSERT INTO hcm_market.klines
                (symbol, time_frame, open_time, open, high, low, close,
                 tick_volume, spread, real_volume, source)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (symbol, time_frame, open_time) DO UPDATE SET
                high = GREATEST(hcm_market.klines.high, EXCLUDED.high),
                low = LEAST(hcm_market.klines.low, EXCLUDED.low),
                close = EXCLUDED.close,
                -- Tier1: tick_volume 取最新快照(覆盖)而非累加。旧逻辑每 5s 重写一次
                -- 同一收盘 bar 就把 tick_volume 累加 ~120 倍，导致 PG 收盘 bar 成交量
                -- 是实时 bar 的百倍 → 二者并到同一序列后实时 bar 被误判"流动性枯竭"，
                -- 质量因子 vol_q≈0.01，开 F6 时实时信号会被系统性误杀。改为覆盖后
                -- PG 与实时 bar 成交量口径一致。
                tick_volume = EXCLUDED.tick_volume,
                spread = EXCLUDED.spread,
                -- real_volume 是该根 bar 的固有属性（同一收盘 bar 被桥反复重写时值不变），
                -- 用覆盖而非累加，避免被每 5s 重写叠乘 N 倍（spread 同理取最新值）。
                real_volume = EXCLUDED.real_volume
                -- 注：source 列不在 ON CONFLICT 更新列表中，保留首次写入者的标识
                -- （2026-08-05 D4：写入源标识，便于诊断"谁在写 K线"）。
        """
        # 2026-08-05 (D4): 写入源标识。桥是当前生产环境唯一的真实 K线写入源。
        params = (
            symbol, timeframe_str, ts, float(r['open']), float(r['high']),
            float(r['low']), float(r['close']), int(r['tick_volume'] or 0),
            float(_rate_get(r, 'spread', 0.0) or 0.0),
            int(_rate_get(r, 'real_volume', 0) or 0), 'bridge',
        )
        async with pool.acquire() as conn:
            try:
                await conn.execute(insert_sql, *params)
            except Exception as exc:
                # 旧库尚未 ALTER 加 source 列时，自动补列后重试一次（self-heal）。
                err = str(exc)
                if "source" in err and ("column" in err or "42703" in err):
                    try:
                        await conn.execute(
                            "ALTER TABLE hcm_market.klines "
                            "ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'bridge'"
                        )
                        log.warning("klines.source column auto-added; retrying write")
                        await conn.execute(insert_sql, *params)
                    except Exception as exc2:
                        log.error("klines write failed (source self-heal): %s", exc2)
                else:
                    log.error("klines write failed: %s", exc)
                continue
        count += 1
    if count:
        log.info(f"K-lines written to PG: {count} ({symbol} {timeframe_str})")


async def write_price_to_redis(redis_conn, mt5, symbol, timeframe_str, broker_utc_offset_s: int = 0, write_tick: bool = True):
    """Push real-time price and latest kline to Redis with timeout protection.

    Args:
        redis_conn: Redis connection.
        mt5: MT5 connection object.
        symbol: Trading symbol.
        timeframe_str: Timeframe string.
        broker_utc_offset_s: Broker timezone offset in seconds (detected at connect).
        write_tick: 主周期=True（写 tick 实时价 + 计算 ATR）；高周期=False（仅写
            latest_kline:{symbol}:{tf} 实时棒，跳过冗余 tick/ATR，降低 MT5 调用量）。
    """
    real = real_symbol(symbol)  # 翻译为实盘名（MT5 API 边界）；Redis 键仍用逻辑名 symbol
    if write_tick:
        try:
            tick = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, mt5.symbol_info_tick, real),
                timeout=3.0
            )
        except (asyncio.TimeoutError, Exception) as exc:
            log.warning(f"write_price_to_redis: symbol_info_tick timeout/error: {exc}")
            return
        if tick is None:
            return
        redis_conn.hset("hcm:config:v2", f"market:latest:{symbol}", json.dumps({
            "bid": tick.bid, "ask": tick.ask,
            "time": tick.time - broker_utc_offset_s,  # convert broker local → UTC
            "last": tick.last,
            "updated_at": tick.time - broker_utc_offset_s,  # explicit UTC timestamp
        }))
    mt5_tf = MT5_TIMEFRAME_MAP.get(timeframe_str, mt5.TIMEFRAME_M5)
    try:
        rates = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, mt5.copy_rates_from_pos, real, mt5_tf, 0, 1),
            timeout=3.0
        )
    except (asyncio.TimeoutError, Exception) as exc:
        log.warning(f"write_price_to_redis: copy_rates_from_pos timeout/error: {exc}")
        return
    if rates is not None and len(rates) > 0:
        r = rates[-1]
        # Tier1: 实时 bar 载荷补齐 spread / real_volume，使下游 compute_bar_quality
        # 的 spread_q / vol_q 在实时 bar 上不再失明（此前实时 bar 无 spread →
        # spread_q 恒置 1.0；_rate_get 兼容旧版 MT5 rate 不含这两字段的情况）。
        _spread = _rate_get(r, 'spread', 0.0) or 0.0
        _rv = _rate_get(r, 'real_volume', 0) or 0
        # 周期对齐校验：open_time 圆整到最近 bar_sec 边界（与 PG 写入同源逻辑，
        # 避免 hcm:config:v2 字段里的 H4/D1 错位棒污染多周期共振矩阵）。
        _bar_sec = TF_SECONDS.get(timeframe_str, 300)
        _open_epoch = _align_bar_open_epoch(int(r['time']) - broker_utc_offset_s, _bar_sec)[0]
        redis_conn.hset("hcm:config:v2", f"latest_kline:{symbol}:{timeframe_str}", json.dumps({
            "open_time": _open_epoch,
            "open": float(r['open']), "high": float(r['high']),
            "low": float(r['low']), "close": float(r['close']),
            "volume": int(r['tick_volume'] or 0),
            "spread": float(_spread),
            "real_volume": int(_rv),
        }))
        # Compute and cache ATR (14-period) for SL/TP calculation — 仅主周期需要
        if write_tick:
            try:
                atr_rates = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, mt5.copy_rates_from_pos, real, mt5.TIMEFRAME_M5, 0, 15),
                    timeout=3.0
                )
            except (asyncio.TimeoutError, Exception) as exc:
                log.warning(f"write_price_to_redis: ATR copy_rates_from_pos timeout/error: {exc}")
                return
            if atr_rates is not None and len(atr_rates) >= 15:
                trs: list[float] = []
                for i in range(1, len(atr_rates)):
                    hl = float(atr_rates[i]['high']) - float(atr_rates[i]['low'])
                    hc = abs(float(atr_rates[i]['high']) - float(atr_rates[i-1]['close']))
                    lc = abs(float(atr_rates[i]['low']) - float(atr_rates[i-1]['close']))
                    trs.append(max(hl, hc, lc))
                atr_val = sum(trs[-14:]) / 14.0
                redis_conn.set(f"hcm:atr:{symbol}", str(atr_val), ex=300)


def push_live_bar_to_redis(redis_conn, mt5, symbol: str, timeframe_str: str, broker_utc_offset_s: int = 0) -> None:
    """Push the current (unclosed) live bar to Redis.

    Called from the 2s tick loop — pushes the latest unclosed bar as JSON
    to market:bar:{symbol}:{timeframe}:live with TTL=600s.

    This enables the scheduler to see the latest bar data before it closes
    in PostgreSQL (Fix #4).

    Args:
        redis_conn: Redis connection (sync).
        mt5: MT5 connection object.
        symbol: Trading symbol.
        timeframe_str: Timeframe string (M1/M5/...).
        broker_utc_offset_s: Broker timezone offset in seconds.
    """
    try:
        real = real_symbol(symbol)  # 翻译为实盘名（MT5 API 边界）；Redis 键仍用逻辑名 symbol
        mt5_tf = MT5_TIMEFRAME_MAP.get(timeframe_str, mt5.TIMEFRAME_M5)
        rates = mt5.copy_rates_from_pos(real, mt5_tf, 0, 1)
        if rates is None or len(rates) == 0:
            return
        r = rates[-1]
        key = LIVE_BAR_KEY_TEMPLATE.format(symbol=symbol, timeframe=timeframe_str)
        # 周期对齐校验：open_time 圆整到最近 bar_sec 边界（与 PG/最新棒写入同源逻辑）。
        _bar_sec = TF_SECONDS.get(timeframe_str, 300)
        _open_epoch = _align_bar_open_epoch(int(r['time']) - broker_utc_offset_s, _bar_sec)[0]
        # Tier1: 同源实时 bar 也补齐 spread / real_volume，保持质量维度一致
        payload = json.dumps({
            "open_time": _open_epoch,
            "open": float(r['open']),
            "high": float(r['high']),
            "low": float(r['low']),
            "close": float(r['close']),
            "tick_volume": int(r['tick_volume'] or 0),
            "spread": float(_rate_get(r, 'spread', 0.0) or 0.0),
            "real_volume": int(_rate_get(r, 'real_volume', 0) or 0),
            "is_closed": False,
        })
        redis_conn.set(key, payload, ex=LIVE_BAR_TTL_SECONDS)
    except Exception as exc:
        log.debug("push_live_bar_to_redis error for %s/%s: %s", symbol, timeframe_str, exc)


def _get_atr_from_redis(redis_conn, symbol: str) -> float:
    """Get latest ATR value from Redis or compute fallback.

    Args:
        redis_conn: Redis connection.
        symbol: Trading symbol.

    Returns:
        ATR value (0.0 if unavailable — caller should handle).
    """
    try:
        # Try cached ATR first
        atr = redis_conn.get(f"hcm:atr:{symbol}")
        if atr:
            return float(atr)
        # Fallback: read from latest kline cache
        cached = redis_conn.get(f"hcm:market:latest_kline:{symbol}")
        if cached:
            import json
            data = json.loads(cached)
            atr = data.get('atr', 0.0)
            if atr > 0:
                return atr
        return 0.0  # not available — caller should handle
    except Exception:
        return 0.0


def _get_max_entry_slippage(redis_conn, symbol: str) -> float:
    """计算"入场价偏移闸门"允许的最大滑点（point）。

    优先用 ATR 倍数（波动率感知），ATR 不可用时回退固定点数。
    配置（均可 Redis 热改，hcm:config:v2）：
      - bridge.max_entry_slippage_atr_mult  (默认 0.3)  → max_slip = ATR * mult
      - bridge.max_entry_slippage_pts       (默认 50)   → ATR 缺失时回退固定点数
    """
    try:
        atr_mult_raw = redis_conn.hget("hcm:config:v2", "bridge.max_entry_slippage_atr_mult")
        atr_mult = float(atr_mult_raw) if atr_mult_raw else 0.3
    except (ValueError, TypeError):
        atr_mult = 0.3
    atr = _get_atr_from_redis(redis_conn, symbol)
    if atr > 0:
        return atr * atr_mult
    # ATR 不可用时回退固定点数
    try:
        pts_raw = redis_conn.hget("hcm:config:v2", "bridge.max_entry_slippage_pts")
        return float(pts_raw) if pts_raw else 50.0
    except (ValueError, TypeError):
        return 50.0


def _get_follower_fill_max_age_ms(redis_conn) -> int:
    """跟单号读取主号成交价锚点的新鲜度阈值（毫秒）。

    主号 fill 键超龄则视为过期，跟单号回退 T0 entry_price（不强行锚定过期价，
    防主号先平、跟单延迟补开时锚定错误价）。可 Redis 热改(hcm:config:v2)。
      - bridge.follower_fill_max_age_ms  (默认 2000)
    """
    if not redis_conn:
        return 2000
    try:
        raw = redis_conn.hget("hcm:config:v2", "bridge.follower_fill_max_age_ms")
        return int(raw) if raw else 2000
    except (ValueError, TypeError):
        return 2000


def _stream_id_prefix(msg_id: str) -> int:
    """Redis stream ID 的可比较排序键（毫秒时间戳*100000 + 序列号），越大越新。

    用于 latest-wins：同一批次/跨批次只认最新一条信号，丢弃其余。
    用完整 ID（含序列号）而非仅毫秒，避免同毫秒多条信号排序歧义。
    解析失败返回 0（不会被误判为最新）。
    """
    try:
        parts = str(msg_id).split("-")
        if len(parts) == 2:
            return int(parts[0]) * 100000 + int(parts[1])
        return int(parts[0])
    except (ValueError, AttributeError, IndexError):
        return 0


# ═══════════════════════════════════════════════════════════════
#  模块级配置读取（供 _update_trailing_stops / _sync_positions 共用）
# ═══════════════════════════════════════════════════════════════

_warned_keys: set[str] = set()

# close.* 配置默认值 — 集中管理，禁用散落硬编码。
# 正式环境必须通过 PG(hcm_config.config) + Redis(hcm:config:v2) 显式配置；
# 此处仅作配置缺失时的安全回退（缺失会打 ERROR 告警，提示运维显式配置）。
CLOSE_CONFIG_DEFAULTS: dict[str, float] = {
    "close.breakeven_atr_mult": 1.0,
    # breakeven_tp_ratio: 保本门槛占 TP 距的比例上限。有效保本门槛 =
    #   min(breakeven_atr_mult×ATR, TP距×本比例)。
    #   原硬编码 0.3 是隐藏天花板：breakeven_atr_mult 设到再高也被它卡在 TP距×0.3，
    #   导致"调大 breakeven_atr_mult 没用"。提为可配置键后可面板热调、真正延后保本。
    "close.breakeven_tp_ratio": 0.5,
    "close.breakeven_buffer_atr_mult": 0.15,
    "close.trail_wide_atr_mult": 0.7,
    # ── P0 优化新增键 ──
    # trail_start_atr_mult: 仅盈利超过该 ATR 倍数后才启动移动止盈，
    #   之前只靠保本地板保护，让盈利单充分奔跑（避免被 0.5ATR 贴身扫掉）。
    "close.trail_start_atr_mult": 2.0,
    # trail_start_tp_ratio: 移动止盈线启动门槛占 TP 距的比例上限。
    #   有效 trail_start = min(atr×trail_start_atr_mult, TP距×该比例)，
    #   保证移动止盈线 / TP-relay 在固定 TP 触发前就接管追利。
    #   否则 trail_start ≥ TP距 时固定 TP 永远先触发、TP-relay 永远接管不了，
    #   单子按固定 TP 平掉、漏掉后续同向大行情（实证 ticket 365683517：
    #   trail_start≈7.7 ≥ TP距 7.65，行情暴跌到 4029.56 仍按固定 TP 平，漏掉 3.2 点）。
    "close.trail_start_tp_ratio": 0.5,
    # tp_relay_enabled: 移动止盈接力 TP 追利开关（2026-07-23）。
    #   True → 盈利超过 trail_start 后 TP 同步前移(锁 50% 盈利 + trail_wide 缓冲)接力追利；
    #   False → 仅 SL 移动保本/追利, TP 保持开仓原值不变。
    "close.tp_relay_enabled": True,
    # tp_trail_wide_atr_mult: 承接 TP 追利时, TP 跟随现价前移的缓冲(ATR 倍数)。
    #   TP 保持在价格前方该距离(tick.bid + buf / tick.ask - buf), 价格涨过原 TP 后
    #   继续承接趋势奔跑; 价格回落时棘轮不回退, TP 锁在峰值附近(只进不退)。
    "close.tp_trail_wide_atr_mult": 0.7,
    # max_sl_atr_mult: 初始/zone/AI 止损距离硬上限（ATR 倍数），封顶过宽止损。
    "close.max_sl_atr_mult": 1.8,
    # fallback_atr: Redis ATR 缺失时的安全回退值（XAUUSD 经验值）。
    "close.fallback_atr": 2.0,
    # tp_min/tp_max_atr_mult: zone→TP 锚点可接受的距离区间（ATR 倍数）。
    # 仅当对向 zone 落在区间内且 R:R≥1 时才用作 TP，否则回退 close.tp_atr_multiplier。
    "close.tp_min_atr_mult": 1.0,
    "close.tp_max_atr_mult": 9.0,
    # ── zone SL/TP 偏移（用户指令：zone 给出的 SL 做后偏移、TP 做内偏移）──
    # zone_sl_offset_atr_mult: SL 在 zone 边界外侧再退此 ATR 倍数
    #   （给“假突破探针/wick 刺穿 level”留余地，避免被扫）。
    "close.zone_sl_offset_atr_mult": 0.4,
    # zone_tp_offset_atr_mult: TP 在 zone 目标内侧收此 ATR 倍数
    #   （抢在“阻力前动能衰竭回撤”前落袋）；仅当 trailing 未覆盖(目标<trail_start)时
    #   生效，trailing 覆盖时压 zone level 不偏移。
    "close.zone_tp_offset_atr_mult": 0.3,
    # reverse_guard_enabled: 反转护栏（2026-07-28）。新信号与同品种反向持仓方向相反、
    #   且该反向仓【正在盈利 + SL 已锁到保本及以上（移动止盈已接管）】时，
    #   跳过本次反转开仓，让移动止盈继续管理趋势单。
    #   根因实证 ticket 365683517：SELL 盈利 4.07、SL 已保本(4039.81<4040.41)、
    #   tier3 追利运行中，被反转 BUY(1508390) 在暴跌前 6 秒截胡平掉，
    #   漏掉后续到 4029.56 的 10.85 点行情，反转 BUY 自身反亏 6.42。
    "close.reverse_guard_enabled": True,
    # ── P2 新增：总仓位金额移动止盈（2026-07-17 面板+桥侧）──
    "close.total_trail_enabled": False,
    "close.total_trail_start_amount": 30.0,  # 启动阈值 USD
    "close.total_trail_stop_amount": 15.0,   # 回撤保护线 USD
    "close.total_trail_check_interval": 10,  # 检查间隔秒（≤0 则默认 10）
    # ── 跟单号每日盈亏熔断（2026-08-11 部署，2026-08-21 修订分母=equity + 加绝对金额下限）──
    # 跟单桥专属：当日净盈亏占账户权益(equity)的百分比超盈利/亏损上限【且】绝对金额也超
    # 对应绝对下限(可设)即熔断，全平跟单号并停止当日跟单，次日 resume_time 自动恢复
    # （主号完全不受影响）。分母由旧 balance 改为 equity（小账户更合理，避免余额 $28 被
    # 微小绝对亏损算成数百% 误触发）；绝对下限=0 时仅按百分比判定（保持旧行为可回退）。
    # 上限百分比数值（如 5.0 = 当日净盈亏达账户 equity 的 5%）；0=该方向不限制。
    "close.follow_circuit_break_enabled": False,
    "close.follow_daily_profit_max": 0.0,   # 盈利上限（占账户 equity% ；0=不限制盈利方向）
    "close.follow_daily_loss_max": 0.0,     # 亏损上限（占账户 equity% ；0=不限制亏损方向）
    "close.follow_daily_profit_abs_min": 0.0,  # 盈利绝对金额阈值($)；百分比达标且绝对额>=此值才熔断，0=不设绝对下限
    "close.follow_daily_loss_abs_min": 0.0,    # 亏损绝对金额阈值($)；百分比达标且绝对额>=此值才熔断，0=不设绝对下限
    "close.follow_resume_time": "06:30",     # 每日恢复跟单时间（本地时区 HH:MM）
}


def _get_close_config(redis_conn, key: str, default: float) -> float:
    """从 Redis hcm:config:v2 读取 float 配置值，缺值打 WARNING 并使用默认。

    模块级函数，供 _update_trailing_stops() 和 position_sync 共用。
    使用模块级 _warned_keys 避免同一 key 重复 WARNING。

    Args:
        redis_conn: Redis connection (sync client, decode_responses=True)。
        key: 配置 key（如 "close.breakeven_atr_mult"）。
        default: 缺省默认值。

    Returns:
        float 配置值。
    """
    val = redis_conn.hget("hcm:config:v2", key)
    if val is None or val == "":
        if key not in _warned_keys:
            log.error(
                "REQUIRED close config '%s' missing in Redis/PG — using fallback %.2f. "
                "请通过 PG(hcm_config.config) + Redis(hcm:config:v2) 显式配置，禁用硬编码。",
                key, default,
            )
            _warned_keys.add(key)
        return float(default)
    try:
        return float(val)
    except (ValueError, TypeError):
        if key not in _warned_keys:
            log.warning(
                "close config key '%s' invalid value '%s' — using default %.2f",
                key, val, default,
            )
            _warned_keys.add(key)
        return float(default)


def _get_close_config_bool(redis_conn, key: str, default: bool = False) -> bool:
    """从 Redis hcm:config:v2 读取 bool 配置值。

    接受 "true"/"1"/"yes"（大小写不敏感）为 True。

    Args:
        redis_conn: Redis connection。
        key: 配置 key。
        default: 缺省默认值。

    Returns:
        bool 配置值。
    """
    val = redis_conn.hget("hcm:config:v2", key)
    if val is None or val == "":
        if key not in _warned_keys:
            log.warning(
                "close config key '%s' not set — using default %s", key, default
            )
            _warned_keys.add(key)
        return default
    return val.lower() in ("true", "1", "yes")


def _after_close_cooling(redis_conn, symbol: str) -> bool:
    """信号冷却闸门读取：持仓刚平仓后 N 秒内抑制该品种新开仓。

    读 hcm:after_close_cooldown:{symbol}，与当前 epoch 比较；键不存在或已过期→False。
    该键由 _record_after_close_cooldown() 在平仓时写入（close.after_close_cooldown_sec>0 才写），
    故关闭状态下本函数恒返回 False，无需在热路径额外读配置。
    """
    if redis_conn is None or not symbol:
        return False
    try:
        raw = redis_conn.get(f"hcm:after_close_cooldown:{symbol}")
        if raw is None:
            return False
        if isinstance(raw, bytes):
            raw = raw.decode()
        return time.time() < float(raw)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 时段感知风险档案 (2026-07-24)
#   按 UTC 小时把一天切成亚盘/欧盘/美盘，每盘可有独立 SL/TP/追利系数。
#   会话键 `close.<session>.<suffix>` 优先于全局键 `close.<suffix>`，再回退默认值。
#   这样开仓系数与移动追利系数同源、随时段切换，跨盘持仓时追利参数随当前盘动态生效。
# ─────────────────────────────────────────────────────────────────────────────
def _current_session() -> str:
    """返回当前时段：亚盘 asia / 欧盘 europe / 美盘 us。基于 UTC 小时。

    亚盘 00:00-08:00、欧盘 08:00-13:00、美盘 13:00-22:00、22:00-24:00 归亚盘(清淡段)。
    """
    h = datetime.now(timezone.utc).hour
    if h < 8:
        return "asia"
    if h < 13:
        return "europe"
    if h < 22:
        return "us"
    return "asia"


# 会话默认系数（与前端平仓面板默认值、close.py SESSION_DEFAULTS 保持一致）
SESSION_DEFAULTS = {
    "asia":   {"sl": 1.5, "tp": 1.8, "min_rr": 1.2, "be": 0.5, "be_buf": 0.15, "trail_start": 2.5, "trail_wide": 0.7, "tp_relay": True},
    "europe": {"sl": 2.0, "tp": 2.4, "min_rr": 1.2, "be": 0.5, "be_buf": 0.15, "trail_start": 2.0, "trail_wide": 0.7, "tp_relay": True},
    "us":     {"sl": 2.0, "tp": 2.6, "min_rr": 1.3, "be": 0.4, "be_buf": 0.15, "trail_start": 1.5, "trail_wide": 0.7, "tp_relay": True},
}


def _session_cfg_float(redis_conn, suffix: str, default: float) -> float:
    """会话优先的浮点配置读取。先 close.<session>.<suffix>，再 close.<suffix>，再 default。"""
    if redis_conn is not None:
        sess = _current_session()
        for key in (f"close.{sess}.{suffix}", f"close.{suffix}"):
            try:
                v = redis_conn.hget("hcm:config:v2", key)
            except Exception:
                v = None
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    return float(default)


def _session_cfg_bool(redis_conn, suffix: str, default: bool) -> bool:
    """会话优先的布尔配置读取。先 close.<session>.<suffix>，再 close.<suffix>，再 default。"""
    if redis_conn is not None:
        sess = _current_session()
        for key in (f"close.{sess}.{suffix}", f"close.{suffix}"):
            try:
                v = redis_conn.hget("hcm:config:v2", key)
            except Exception:
                v = None
            if v not in (None, ""):
                return str(v).lower() in ("true", "1", "yes")
    return default


def _audit_signal_stage(redis_conn, sid, stage, **fields):
    """审计信号执行链路(桥端): signal_id → executed/expired/zone_dropped/bridge_failed/...

    与风控端 stream_consumer._audit_signal_stage 对齐，统一用独立 key
    hcm:signal_exec:{sid}:{stage}(TTL 7天)，定位漏单环节。
    """
    if redis_conn is None or sid in (None, 0):
        return
    try:
        _payload = {"at": datetime.now(timezone.utc).isoformat()}
        _payload.update({k: str(v) for k, v in fields.items()})
        _key = f"hcm:signal_exec:{sid}:{stage}"
        redis_conn.set(_key, json.dumps(_payload), ex=7 * 86400)
    except Exception:
        pass


def _is_signal_fresh(msg_data: dict, max_age: int, redis_conn=None) -> bool:
    """判断信号是否足够新鲜、允许下单（禁止过期/缺时间戳信号下单）。

    规则（防御纵深，任一不满足即拒绝）：
      - timestamp 缺失            → 拒绝（绝不 'let through'）
      - timestamp 解析失败        → 拒绝
      - age > max_age 秒          → 拒绝（过期信号，行情已变）
      - age < -5 秒（未来时间）    → 拒绝（时钟异常，避免追错行情）

    Args:
        msg_data: Redis stream 消息体（含 timestamp / signal_id）。
        max_age: 允许的最大年龄（秒），来自 bridge.max_signal_age_seconds。

    Returns:
        True 表示可下单；False 表示必须拒绝。
    """
    sid = msg_data.get("signal_id", "?")
    # 优先用信号塔在 T0 打的原始时间戳；缺失才退回发射时刻 T1
    ts_raw = msg_data.get("signal_generated_at") or msg_data.get("timestamp", "")
    if not ts_raw:
        log.warning("REJECT signal %s: missing 'timestamp' field", sid)
        return False
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception as exc:
        log.warning("REJECT signal %s: bad timestamp '%s': %s", sid, ts_raw, exc)
        return False
    if age > max_age:
        log.warning("REJECT stale signal %s: age=%.0fs > %ds", sid, age, max_age)
        return False
    # 【2026-08-24 修复】未来时间容差：原硬编码 -5s，会误拒信号塔容器时钟偏差
    # （实测 signal-tower 容器比宿主桥快 ~146s，信号 timestamp 超前 → age<-5 → 误判
    # "未来时间"拒绝 → 错过下单）。改为可配置 bridge.max_clock_skew_sec（默认 180），
    # 容忍跨容器/主机正常时钟偏差，仅拒绝远超合理偏差的时钟异常（防追错行情）。
    _clock_skew = _get_close_config(redis_conn, "bridge.max_clock_skew_sec", 180.0)
    if age < -_clock_skew:
        log.warning("REJECT signal %s: timestamp %.0fs in the future (>skew %ds)", sid, age, int(_clock_skew))
        return False
    return True


def _detect_tp_lock_conflict(redis_conn, atr: float, lock_trigger: float) -> None:
    """检测 TP 距离与锁利触发距离的冲突。

    若 tp_distance < lock_trigger，Tier2/Tier3 在 TP 触发前无法生效。
    纯告警，不改配置值。

    Args:
        redis_conn: Redis connection。
        atr: 当前 ATR 值。
        lock_trigger: 已计算的 Tier2 锁利触发距离 (= ATR * close.lock_trigger_atr_mult)。
    """
    tp_mult_str = redis_conn.hget("hcm:config:v2", "close.tp_atr_multiplier")
    if not tp_mult_str:
        return  # TP not configured, no conflict
    try:
        tp_mult = float(tp_mult_str)
    except ValueError:
        return
    if tp_mult <= 0:
        return
    tp_distance = atr * tp_mult
    if tp_distance < lock_trigger:
        log.warning(
            "TP/锁利冲突: tp_distance=%.1f < lock_trigger=%.1f "
            "(Tier2/Tier3 在 TP 触发前无法生效). "
            "建议: 增大 close.tp_atr_multiplier 或减小 close.lock_trigger_atr_mult",
            tp_distance, lock_trigger,
        )


# ── 账户可用性铁律（2026-08-28 新增）──
# 模块级缓存：MT5 账号是否登录且可交易。由各路径统一读取，避免每次 IPC 调 account_info()。
# 健康检查循环（每 30s）与消费前闸门（每 2s 现查）共同刷新；启动期由 discovery 结果初始化。
# 任何 MT5 写操作（开仓/平仓）前必须过 mt5_trade_ready() 铁律：不可用则【硬失败】，绝不操作。
MT5_TRADE_OK = True


def mt5_trade_ready(mt5, force_check: bool = False) -> bool:
    """账户可用性铁律（不可绕过的最后兜底）。

    返回 False 的语义：终端未连 / 账号登出 / trade_allowed=False / account_info() 异常。
    此时任何 MT5 写操作（开仓、平仓）都应立即中止，绝不下单/平仓——
    账户状态未知或不可交易时操作风险极高（可能下到错误/冻结账号）。

    force_check=True 时现查 account_info()（用于消费前高频关键路径，并顺带刷新缓存）；
    否则读模块级缓存 MT5_TRADE_OK（高频路径如 sync_positions 用，零 IPC 开销）。
    """
    global MT5_TRADE_OK
    if force_check:
        try:
            _ai = mt5.account_info()
        except Exception as _aie:
            log.warning("MT5 account_info() raised in trade-ready check: %s", _aie)
            _ai = None
        ok = bool(_ai) and bool(getattr(_ai, "trade_allowed", False))
        MT5_TRADE_OK = ok
        return ok
    return MT5_TRADE_OK


def place_mt5_order(mt5, signal_data, symbol, timeframe_str, redis_conn=None):
    """Place an order on MT5 based on signal data.

    Args:
        mt5: MT5 connection object.
        signal_data: Signal dict from Redis stream.
        symbol: Trading symbol.
        timeframe_str: Timeframe string (for logging context).
        redis_conn: Redis connection for reading close config.

    Returns:
        Dict with code, message, mt5_ticket, filled_price, volume.
    """
    # ── 账户可用性铁律（2026-08-28 新增）──
    # 所有开仓路径（含 reconcile 补开、信号消费）的统一兜底闸门。账户不可用则硬失败返回，
    # 绝不向未知/冻结账号下单。调用方收到 code=-1 应视为失败（不误判成功、留待重试/留流）。
    if not mt5_trade_ready(mt5):
        log.critical("MT5 account NOT trade-ready — place_mt5_order BLOCKED by trade-ready guard "
                     "(code=-1, no order sent)")
        return {"code": -1, "message": "MT5 account unavailable — order blocked by trade-ready guard",
                "mt5_ticket": None, "filled_price": None, "volume": 0.0}
    sig_id = signal_data.get('signal_id', 0)
    direction = signal_data.get('direction', 'BUY')
    lot = float(signal_data.get('lot', 0.01))
    if lot <= 0:
        lot = 0.01
    # 跟单桥：按跟随关系手数倍率缩放（全复制主号，手数随跟随比例；主号桥不变）
    if IS_FOLLOWER and FOLLOW_LOT_MULT:
        _src_acct = int(signal_data.get("account_id", 0) or 0)
        _mult = FOLLOW_LOT_MULT.get(_src_acct)
        if _mult:
            lot = round(lot * float(_mult), 2)
            if lot <= 0:
                lot = 0.01
    # ── 2026-08-25 修复：消费风控下发的 suggested_lot_ratio（extreme_pending 极值追单×0.5）──
    # 此前该字段在风控 rule_chain 设置、stream_consumer 透传，但桥从未读取 → 轻仓减半
    # 形同虚设，极值追单按满手数开仓（与"防接刀、控仓"初衷相悖）。在此（跟单倍率之后、
    # 最小手数钳制之前）应用，确保减半后仍满足 volume_min。
    try:
        _lot_ratio = float(signal_data.get("suggested_lot_ratio", 1.0) or 1.0)
    except Exception:
        _lot_ratio = 1.0
    if _lot_ratio not in (0.0, 1.0):
        _reduced = round(lot * _lot_ratio, 2)
        if _reduced > 0:
            log.info("suggested_lot_ratio=%.4f applied → lot %.4f → %.4f", _lot_ratio, lot, _reduced)
            lot = _reduced
        if lot <= 0:
            lot = 0.01
    sl = float(signal_data.get('sl_price', 0))
    tp = float(signal_data.get('tp1', 0))
    real = real_symbol(symbol)  # 翻译为实盘名（MT5 API 边界）；ATR/日志用逻辑名 symbol

    # ── 最小手数钳制：根治「主号 0.006 手被经纪商 Invalid volume 拒绝、跟单 0.01 手却成交」 ──
    # 主号桥按余额/倍率算出的手数可能低于经纪商最小交易量（如 0.006 < volume_min 0.01），
    # 直接发送会被 MT5 以 "Invalid volume" 拒绝 → 主号不下单；跟单桥因 round(.,2) 进位到 0.01
    # 才侥幸通过。此处统一钳制到 volume_min 并向上取整到 volume_step 整数倍，主/跟单桥一致生效。
    try:
        _sinfo = _mt5_global.symbol_info(real)
        if _sinfo is not None:
            _vmin = float(getattr(_sinfo, "volume_min", 0.0) or 0.0)
            _vstep = float(getattr(_sinfo, "volume_step", 0.01) or 0.01)
            if _vmin > 0 and lot < _vmin:
                log.warning("Lot %.4f < volume_min %.4f for %s — clamping up to min", lot, _vmin, real)
                lot = _vmin
            if _vstep > 0:
                lot = math.ceil(lot / _vstep - 1e-9) * _vstep
            lot = round(lot, 2)
            if lot <= 0:
                lot = _vmin if _vmin > 0 else 0.01
    except Exception as _e:
        log.warning("volume clamp failed for %s: %s", real, _e)

    # Get tick first (needed for SL/TP)
    tick = _mt5_global.symbol_info_tick(real)
    if tick is None:
        log.error(f"No tick data for {real} (logic {symbol}) — MT5 may be disconnected")
        return {"code": -1, "message": f"No tick data for {symbol}"}

    # ── ATR (with fallback) for SL/TP sizing ──
    # P0-1: 单一可靠 ATR 源；Redis 缺失时回退到 close.fallback_atr（默认 2.0），
    #   保证 SL/TP 永远能算出来（消灭“ATR=0 → 无 TP”的 40% 持仓）。
    atr = _get_atr_from_redis(redis_conn, symbol) if redis_conn else 0.0
    if atr <= 0:
        try:
            fb = float(redis_conn.hget("hcm:config:v2", "close.fallback_atr")) if redis_conn else 0.0
        except Exception:
            fb = 0.0
        atr = fb or CLOSE_CONFIG_DEFAULTS["close.fallback_atr"]
        log.warning("ATR from redis unavailable for %s — using fallback atr=%.3f", symbol, atr)

    # ── 入场价偏移闸门（核心修复）：信号基于 T0 的 entry_price，若行情已漂离则拒绝 ──
    # 这是"错过入场点=直接止损"的真正防线，替代原来不靠谱的墙钟秒数闸门。
    entry_price = float(signal_data.get("entry_price", 0.0) or 0.0)
    # 【阶段1 去跟单化 2026-08-24】移除 B-2 跟单 master-fill 锚点：
    # 跟单号不再用主号真实成交价(hcm:master:fill:{sid})作滑点基准——主号成交价在链路延迟下
    # 已漂移，且此依赖让跟单号滑点判断受主号执行影响。现在所有账号（含跟单号）一律用
    # 信号 T0 entry_price + 自身实时 tick（与主号完全同口径），滑点闸门公平一致。
    if entry_price > 0:
        exec_price = tick.ask if direction == "BUY" else tick.bid
        slip_pts = abs(exec_price - entry_price)
        max_slip = _get_max_entry_slippage(redis_conn, symbol) if redis_conn else 50.0
        if slip_pts > max_slip:
            log.warning(
                "REJECT ENTRY_MISSED signal %s: price slipped %.2f (exec=%.2f entry=%.2f) "
                "> max_slip=%.2f — 行情已远离入场点，跳过",
                sig_id, slip_pts, exec_price, entry_price, max_slip,
            )
            return {"code": -2, "message": f"ENTRY_MISSED: slippage {slip_pts:.1f}pts > {max_slip:.1f}pts"}

    # ── P1c (collaboration): consume AI risk multipliers + ④ zone co-decides SL ──
    # GATED by signal_tower.ai_risk_enabled (default OFF → no behavior change
    # until explicitly enabled). When ON:
    #   • AI-provided sl/tp ATR multipliers override the global close.* multipliers
    #   • a present zone tightens SL to just outside the structure boundary
    #     (② AI and ④ Zone co-decide risk).
    if redis_conn:
        _tp_baseline_atr = None  # ai_tp 基线倍数：zone 锚点无效时兜底(等价于会话 TP)
        try:
            ai_flag = redis_conn.hget("hcm:config:v2", "signal_tower.ai_risk_enabled")
            ai_risk_on = bool(ai_flag and str(ai_flag).lower() in ("1", "true"))
        except Exception:
            ai_risk_on = False
        if ai_risk_on:
            ai_sl = float(signal_data.get("ai_sl_mult", 0) or 0)
            ai_tp = float(signal_data.get("ai_tp_mult", 0) or 0)
            zone_lv = float(signal_data.get("zone_level", 0) or 0)
            zone_tp = signal_data.get("zone_type", "") or ""
            try:
                atr = _get_atr_from_redis(redis_conn, symbol) if redis_conn else 0.0
            except Exception:
                atr = 0.0
            if (ai_sl > 0 or ai_tp > 0) and atr > 0:
                if ai_sl > 0 and sl == 0:
                    sl_dist = atr * ai_sl
                    sl = (tick.ask - sl_dist) if direction == "BUY" else (tick.bid + sl_dist)
                if ai_tp > 0:
                    # 仅记录 ATR 倍数基线，不直接设 tp，让下方 zone 锚点优先；
                    # zone 无效/超范围时再用此基线兜底(等价于会话 TP 倍数)。
                    _tp_baseline_atr = ai_tp
                log.info("P1c AI risk applied: ai_sl_mult=%.2f ai_tp_mult=%.2f → sl=%.2f (tp baseline %.2fATR, zone anchor may override)",
                         ai_sl, ai_tp, sl, _tp_baseline_atr)
            # ④ co-decides risk: SL 压在 zone 边界外侧；再做后偏移(back-offset)给
            #   “假突破探针/wick 刺穿 level”留余地，避免被扫。偏移值从配置读(禁硬编码)。
            if zone_lv > 0 and zone_tp in ("SUPPORT", "RESISTANCE", "PIVOT") and atr > 0:
                sl_off = _get_close_config(
                    redis_conn, "close.zone_sl_offset_atr_mult",
                    CLOSE_CONFIG_DEFAULTS["close.zone_sl_offset_atr_mult"],
                )
                band = max(2.0, atr * 0.15) + sl_off * atr
                if direction == "BUY":
                    sl_via_zone = zone_lv - band
                    if sl == 0 or sl_via_zone > sl:
                        sl = sl_via_zone
                else:
                    sl_via_zone = zone_lv + band
                    if sl == 0 or sl_via_zone < sl:
                        sl = sl_via_zone
                log.info("P1c ④ zone SL back-offset %.2fATR: zone=%.2f band=%.2f → sl=%.2f",
                         sl_off, zone_lv, band, sl)

    # ── SL/TP sizing (ATR multiplier mode) ──
    # atr 已在上方统一获取（含回退），此处不再单独重取。
    max_sl_mult = _get_close_config(
        redis_conn, "close.max_sl_atr_mult",
        CLOSE_CONFIG_DEFAULTS["close.max_sl_atr_mult"],
    ) if redis_conn else CLOSE_CONFIG_DEFAULTS["close.max_sl_atr_mult"]

    # SL: 仅当 AI/zone 路径未设置时，用【时段系数】设初始宽保护。
    # _session_cfg_float 优先读 close.<session>.trailing_stop_distance（亚/欧/美盘独立，
    #   已在 hcm:config:v2 配=2），回退 close.trailing_stop_distance，再回退 default 2.0。
    # 主号/跟单号共用本函数 → 初值同口径、天然统一。
    if redis_conn and sl == 0:
        try:
            trailing_enabled = redis_conn.hget("hcm:config:v2", "close.trailing_stop_enabled")
            if trailing_enabled and trailing_enabled.lower() in ('true', '1'):
                _sess = _current_session()
                sl_mult_str = _session_cfg_float(
                    redis_conn, "trailing_stop_distance", SESSION_DEFAULTS[_sess]["sl"])
                if not sl_mult_str:
                    log.warning("SL not applied: close.%s.trailing_stop_distance not configured", _sess)
                else:
                    sl_mult = float(sl_mult_str)
                    sl_distance = atr * sl_mult
                    if direction == "BUY":
                        sl = tick.ask - sl_distance
                    else:
                        sl = tick.bid + sl_distance
        except Exception as exc:
            log.error(f"SL calc failed: {exc}")

    # ── P0-1: 封顶止损距离 ≤ max_sl_atr_mult × atr ──
    # 防止 zone/AI 把 SL 拉得过宽（实测均值 2.67ATR），统一风险暴露。
    if sl > 0:
        entry_ref = tick.ask if direction == "BUY" else tick.bid
        sl_dist = abs(entry_ref - sl)
        max_sl_dist = atr * max_sl_mult
        if sl_dist > max_sl_dist:
            old_sl = sl
            sl = (entry_ref - max_sl_dist) if direction == "BUY" else (entry_ref + max_sl_dist)
            log.info("SL capped %.3f→%.3f (dist %.2f→%.2f, max %.1fATR)",
                     old_sl, sl, sl_dist, max_sl_dist, max_sl_mult)

    # TP: 强制设置（消灭“无 TP 持仓”）。
    # P0/Zone: 优先用 signal 携带的「对向 zone」做 TP 锚点（结构目标位）；
    #   仅当 zone 落在区间(tp_min~tp_max ATR)且 R:R≥1 才采用，否则回退 ATR 倍数。
    if redis_conn:
        try:
            zone_tp = float(signal_data.get("zone_tp_level", 0) or 0)
            entry_ref = tick.ask if direction == "BUY" else tick.bid
            tp_set = False
            if zone_tp > 0 and atr > 0:
                same_dir = (direction == "BUY" and zone_tp > entry_ref) or \
                           (direction == "SELL" and zone_tp < entry_ref)
                tp_dist = abs(zone_tp - entry_ref)
                tp_atr = tp_dist / atr
                tp_min = _get_close_config(
                    redis_conn, "close.tp_min_atr_mult",
                    CLOSE_CONFIG_DEFAULTS["close.tp_min_atr_mult"],
                )
                tp_max = _get_close_config(
                    redis_conn, "close.tp_max_atr_mult",
                    CLOSE_CONFIG_DEFAULTS["close.tp_max_atr_mult"],
                )
                trail_start = _session_cfg_float(
                    redis_conn, "trail_start_atr_mult",
                    CLOSE_CONFIG_DEFAULTS["close.trail_start_atr_mult"],
                )
                sl_dist_now = abs(entry_ref - sl) if sl > 0 else (atr * max_sl_mult)
                rr = tp_dist / sl_dist_now if sl_dist_now > 0 else 0.0
                if same_dir and (tp_min <= tp_atr <= tp_max) and rr >= 1.0:
                    # trailing 协同：目标距 ≥ trail_start(2ATR) 时移动止损必触发，
                    # 回撤被兜住 → TP 压 zone level 不内偏移；否则做内偏移(抢在衰竭前)。
                    tp_off = _get_close_config(
                        redis_conn, "close.zone_tp_offset_atr_mult",
                        CLOSE_CONFIG_DEFAULTS["close.zone_tp_offset_atr_mult"],
                    )
                    if trail_start > 0 and tp_atr >= trail_start:
                        tp = round(zone_tp, 2)          # 不内偏移，靠 trailing 锁利
                        log.info("TP(zone anchor, trailing-covered)=%.3f (%.1fATR, R:R=%.2f)",
                                 tp, tp_atr, rr)
                    else:
                        tp_cand = (zone_tp - tp_off * atr) if direction == "BUY" else (zone_tp + tp_off * atr)
                        rr2 = abs(tp_cand - entry_ref) / sl_dist_now if sl_dist_now > 0 else 0.0
                        if rr2 >= 1.0:
                            tp = round(tp_cand, 2)       # 内偏移生效
                            log.info("TP(zone anchor, inward-offset %.2fATR)=%.3f (%.1fATR, R:R=%.2f)",
                                     tp_off, tp, (abs(tp - entry_ref) / atr), rr2)
                        else:
                            tp = round(zone_tp, 2)       # 偏移会破 R:R → 不偏移，保 zone 锚点
                            log.info("TP(zone anchor, offset skipped R:R<1)=%.3f (%.1fATR, R:R=%.2f)",
                                     tp, tp_atr, rr)
                    tp_set = True
                else:
                    log.info("TP zone anchor skipped (same_dir=%s tp_atr=%.2f rr=%.2f) → ATR fallback",
                             same_dir, tp_atr, rr)
            if not tp_set:
                if _tp_baseline_atr and _tp_baseline_atr > 0 and atr > 0:
                    # zone 锚点无效 → 回退 ai_tp 基线(即信号塔已会话化的 TP 倍数下限)
                    tp_distance = atr * _tp_baseline_atr
                    tp = (tick.ask + tp_distance) if direction == "BUY" else (tick.bid - tp_distance)
                    log.info("TP forced (ai_tp baseline=session): tp=%.3f (%.1fATR)", tp, _tp_baseline_atr)
                else:
                    _sess = _current_session()
                    tp_mult_str = _session_cfg_float(
                        redis_conn, "tp_atr_multiplier", SESSION_DEFAULTS[_sess]["tp"])
                    if not tp_mult_str:
                        log.warning("TP not applied: close.%s.tp_atr_multiplier not configured", _sess)
                    else:
                        tp_mult = float(tp_mult_str)
                        if tp_mult <= 0:
                            log.info("TP disabled: close.%s.tp_atr_multiplier <= 0", _sess)
                        else:
                            tp_distance = atr * tp_mult
                            if direction == "BUY":
                                tp = tick.ask + tp_distance
                            else:
                                tp = tick.bid - tp_distance
                            log.info("TP forced (session %s): tp=%.3f (%.1fATR=%.2f pts)",
                                     _sess, tp, tp_mult, tp_distance)
        except Exception as exc:
            log.error(f"TP calc failed: {exc}")

    # ── Defense in depth: never place an order for a non-tradable direction ──
    if direction not in ("BUY", "SELL"):
        log.error(
            f"Refusing to place order for non-tradable direction: {direction!r} (signal {sig_id})"
        )
        return {"code": -1, "message": f"non-tradable direction: {direction}"}
    order_type = _mt5_global.ORDER_TYPE_BUY if direction == "BUY" else _mt5_global.ORDER_TYPE_SELL
    price = tick.ask if direction == "BUY" else tick.bid
    # 滑点可观测性（建议4）：每次下单打印 entry_price / exec_price / slip_pts
    slip_pts = abs(price - entry_price) if entry_price > 0 else 0.0

    # ── R:R 已在信号塔源头保证 (2026-07-24 根因修复) ──
    # 原桥内 R:R 硬约束已删除: 它把 SL 收紧到 0.9×TP(≈7pt), 导致窄幅震荡频繁止损。
    # 现在信号塔强制 ai_tp_mult ≥ close.trailing_stop_distance × signal_tower.min_rr
    # (默认 2.0×1.2=2.4 → TP≥2.4×ATR, 桥 SL≈1.8×ATR 封顶, R:R≈1.33), 源头健康,
    # 桥不再需要收窄 SL。若信号塔下限未生效, R:R 倒挂会重现 —— 此时应回信号塔修, 而非加回此 guard。
    log.info(
        f"Placing {direction} {lot} lot {symbol} (real {real}) @ {price} (bid={tick.bid} ask={tick.ask}) "
        f"entry={entry_price} slip={slip_pts:.2f} signal={sig_id}"
    )

    # magic 透传规则：信号里有 magic 字段就用（包括 0），完全没传（None）才用 123456 默认
    # 修复：manual_mirror 透传主号原 magic（=0 时保留 0，不强行覆盖为 123456）
    _magic_raw = signal_data.get("magic", None)
    if _magic_raw is None:
        magi = 123456  # 信号完全没 magic 字段（AI 模型默认）
    else:
        magi = int(_magic_raw or 0)  # 显式传 0 也保留 0

    request = {
        "action": _mt5_global.TRADE_ACTION_DEAL,
        "symbol": real,
        "volume": lot,
        "type": order_type,
        "price": price,
        "deviation": 50,
        "magic": magi,
        "comment": f"HCM_v2_mirror_{sig_id}" if _magic_raw is not None else f"HCM_v2_signal_{sig_id}",
        "type_time": _mt5_global.ORDER_TIME_GTC,
        "type_filling": _mt5_global.ORDER_FILLING_IOC,
    }
    # Only add sl/tp if non-zero (MT5 rejects sl=0 / tp=0)
    if sl > 0:
        request["sl"] = sl
    if tp > 0:
        request["tp"] = tp

    result = _mt5_global.order_send(request)
    if result is None:
        err = _mt5_global.last_error()
        return {"code": -1, "message": f"order_send returned None (last_error={err})"}
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"code": result.retcode, "message": result.comment or f"retcode={result.retcode}"}
    return {
        "code": 0, "message": "ok", "mt5_ticket": result.order,
        "filled_price": result.price, "volume": result.volume,
        "sl": sl, "tp": tp,
    }


# ── P1a Zone-Trigger Helpers (gated OFF by signal_tower.zone_trigger_enabled) ──

def _price_in_zone_band(tick, direction: str, zone_level: float,
                         band_pts: float = 5.0) -> bool:
    """P1a: is current price within the trigger tolerance band of zone_level?"""
    price = tick.ask if direction == "BUY" else tick.bid
    return abs(price - zone_level) <= band_pts


def _defer_zone_signal(redis_conn, sid: int, msg_data: dict, ttl: int) -> None:
    """P1a: store a deferred signal to Redis with TTL (= timeout, no dead order)."""
    try:
        redis_conn.set(f"bridge:zone_pending:{sid}", json.dumps(msg_data, default=str), ex=ttl)
        log.info("ZONE DEFER signal %s: price not at zone — deferred %ds", sid, ttl)
    except Exception as exc:
        log.error("zone defer store failed for %s: %s", sid, exc)


# ── T3c ATR 波动过滤 Helpers (gated OFF by signal_tower.zone_atr_filter_enabled) ──

async def _compute_atr14(pool, symbol: str, n: int = 31) -> Optional[float]:
    """T3c: 从 PG 最近 n 根 M5 klines 计算 ATR(14)。失败/数据不足返回 None。"""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT high, low, close FROM hcm_market.klines
                   WHERE symbol=$1 AND time_frame='M5' ORDER BY open_time DESC LIMIT $2""",
                symbol, n,
            )
        if len(rows) < 15:
            return None
        rows = list(reversed(rows))  # 时间升序
        trs = []
        for i in range(1, len(rows)):
            h, l, c = float(rows[i]["high"]), float(rows[i]["low"]), float(rows[i]["close"])
            pc = float(rows[i - 1]["close"])
            tr = max(h - l, abs(h - pc), abs(l - pc))  # True Range
            trs.append(tr)
        if len(trs) < 14:
            return None
        return sum(trs[-14:]) / 14.0
    except Exception as exc:
        log.error("atr14 compute failed (%s): %s", symbol, exc)
        return None


async def _atr_filter_blocks(pool, redis_conn, symbol: str,
                             entry_price: float, tick_price: float,
                             direction: str, default_mult: float = 3.0) -> bool:
    """T3c ATR 波动过滤：价格相对信号入场价 spike 超过 mult×ATR 时拦截（不追单）。

    默认关闭（signal_tower.zone_atr_filter_enabled=false）。启用后，zone 触达
    成交前检查 |tick_price - entry_price|，若远大于近期波动（mult×ATR），判定为
    尖刺扫单而非真实触达，丢弃该 deferred 键（不成交、不追单）。
    """
    try:
        ztc = redis_conn.hget("hcm:config:v2", "signal_tower.zone_atr_filter_enabled")
        if not (ztc and str(ztc).lower() in ("1", "true")):
            return False
        try:
            mult = float(redis_conn.hget("hcm:config:v2", "signal_tower.zone_atr_filter_mult") or default_mult)
        except Exception:
            mult = default_mult
        atr = await _compute_atr14(pool, symbol)
        if atr is None or atr <= 0:
            return False
        if abs(tick_price - float(entry_price)) > mult * atr:
            log.warning(
                "ATR FILTER BLOCK signal %s: |tick %.2f - entry %.2f|=%.2f > %.2f×ATR(%.2f)",
                symbol, tick_price, entry_price,
                abs(tick_price - float(entry_price)), mult, atr,
            )
            return True
        return False
    except Exception as exc:
        log.error("atr filter error: %s", exc)
        return False


async def _mark_signal_blocked(pool, sid, reason: str) -> None:
    """【2026-08-24 修复】桥端拦截时回写 PG fallback_reason（卡点标记）。

    根治"过风控未成交但信号漏斗显示'未标记卡点'"：
    此前桥端各闸门（cooldown/reverse_guard/position_cap/bridge_failed）拦截只写
    Redis 审计 key，从不回写 PG 信号表 fallback_reason → 前端漏斗 map_funnel_reason
    一律显示"未标记"，无法区分"过风控未成交"里哪些是冷却/反转护栏/持仓数/下单失败拦掉的。
    本函数在拦截分支 return False 前回写卡点名（保持 signal_status=1，不覆盖已成交 status=3）。
    """
    if pool is None or not sid:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE hcm_signal.signals SET fallback_reason=$2, "
                "block_reason=$2, updated_at=now() "
                "WHERE signal_id=$1 AND signal_status<>3", sid, reason)
    except Exception as _e:
        log.warning("mark signal %s blocked=%s failed (non-fatal): %s", sid, reason, _e)


async def _execute_signal(pool, mt5, redis_conn, msg_data: dict,
                           msg_symbol: str, timeframe_str: str,
                           dry_run: bool) -> bool:
    """P1a refactor: place order + persist PG + mark processed.

    Extracted from the main loop so both the immediate path AND the
    zone-trigger recheck can reuse the same order + persistence logic.
    Returns True when the order was placed (or dry-run logged).
    """
    sid = int(msg_data.get("signal_id", 0))
    # ── 反转护栏（2026-07-28）──
    # 新信号与同品种反向持仓相反、且反向仓【盈利中 + SL 已锁保本及以上（追利已接管）】
    # 时跳过开仓：反转单截胡会把趋势单在行情延续前平掉（实证 365683517 被反转 BUY
    # 在暴跌前 6 秒截断，漏掉 10.85 点；反转单自身反亏 6.42）。热开关
    # close.reverse_guard_enabled（Redis hcm:config:v2，默认 True）。
    _dir = str(msg_data.get("direction", "")).upper()

    # ── 信号冷却闸门（2026-08-11）：持仓平仓后 N 秒内抑制该品种新开仓 ──
    # 仅拦截 BUY/SELL 开仓信号；平仓/改仓/部分平仓路径不受影响。冷却期间到达的信号
    # 直接丢弃（不成交、但会被标记 processed 去重）→ 冷却结束后新信号正常触发。
    # 作用域：仅按交易品种（per symbol），主号与跟单号共享同一 Redis 键，故双方都生效。
    if _dir in ("BUY", "SELL"):
        _cool_sym = logic_symbol(msg_symbol) or msg_symbol
        if _after_close_cooling(redis_conn, _cool_sym):
            log.info("After-close cooldown: skip %s open for %s (cooling active)", _dir, _cool_sym)
            await _mark_signal_blocked(pool, sid, "bridge_after_close_cooldown")
            return False
    if _dir in ("BUY", "SELL") and _get_close_config_bool(
            redis_conn, "close.reverse_guard_enabled",
            CLOSE_CONFIG_DEFAULTS["close.reverse_guard_enabled"]):
        try:
            _real = real_symbol(msg_symbol) or msg_symbol
            _opp_type = 1 if _dir == "BUY" else 0  # BUY 信号 ↔ SELL 持仓(type=1)
            for _p in (mt5.positions_get() or []):
                if _p.symbol not in (_real, msg_symbol) or _p.type != _opp_type:
                    continue
                _entry = float(_p.price_open or 0.0)
                _psl = float(_p.sl or 0.0)
                _locked = _psl > 0 and (
                    (_p.type == 1 and _psl <= _entry)   # SELL: SL 降到开仓价及以下=保本已锁
                    or (_p.type == 0 and _psl >= _entry)  # BUY: SL 抬到开仓价及以上
                )
                if float(_p.profit or 0.0) > 0 and _locked:
                    log.info(
                        "REVERSE GUARD: block %s signal %s — opposite ticket=%s "
                        "profit=%.2f trailing-locked (sl=%.2f entry=%.2f); "
                        "let trailing manage the trend position",
                        _dir, sid, _p.ticket, _p.profit, _psl, _entry)
                    _audit_signal_stage(redis_conn, sid, "reverse_guard_blocked",
                                        ticket=_p.ticket)
                    await _mark_signal_blocked(pool, sid, "bridge_reverse_guard")
                    return False
        except Exception as _exc:
            log.warning("reverse guard check failed sid=%s: %s (fail-open)", sid, _exc)
    # ── 阶段2 账号维度持仓数上限（2026-08-24 去跟单化）──
    # 每账号开仓前检查自己的实时持仓数（mt5.positions_get()），超过全局 risk_max_open_positions
    # 上限即拒绝（仅 BUY/SELL 开仓；平仓/改仓不受限）。risk-engine 的持仓风控只评估信号
    # account_id(=主号)，跟单号独立下单后其持仓不计入 → 此检查补上账号维度持仓数兜底，
    # 防止任一账号（含跟单号）无限开仓。复用全局配置，与现有风控阈值一致。
    if _dir in ("BUY", "SELL"):
        try:
            # 【2026-08-28 口径统一（风控为主）】账号维度最大持仓数改用与风控引擎
            # 同一真源 risk.max_concurrent_signals（用户裁定：统一口径、风控为主）。
            # 原两键两值：桥读 risk_max_open_positions(=3)、风控读
            # risk.max_concurrent_signals(=5)，同语义不同值 → 限仓口径不一致。
            # 旧键保留为回退：风控键缺失/非法(<=0)时才使用，避免配置丢失时兜底失效。
            # 注意：旧键仍在 shared/redis_client.py 的 CRITICAL_SAFETY_KEYS
            # 启动自检清单内，待确认无回归后再决定是否清理。
            _cap_val = _get_close_config(redis_conn, "risk.max_concurrent_signals", 0.0)
            if _cap_val <= 0:
                _cap_val = _get_close_config(redis_conn, "risk_max_open_positions", 3.0)
            _max_pos = int(_cap_val)
            if _max_pos > 0:
                _pos_all = mt5.positions_get() or []
                _open_cnt = len(_pos_all)
                if _open_cnt >= _max_pos:
                    log.warning(
                        "ACCOUNT POSITION CAP: block %s signal %s sym=%s — open_positions=%d >= %d",
                        _dir, sid, msg_symbol, _open_cnt, _max_pos)
                    _audit_signal_stage(redis_conn, sid, "position_cap_blocked")
                    await _mark_signal_blocked(pool, sid, "bridge_position_cap")
                    return False
        except Exception as _cap_err:
            log.warning("account position cap check failed sid=%s: %s (fail-open)", sid, _cap_err)
    if not dry_run:
        result = place_mt5_order(mt5, msg_data, msg_symbol, timeframe_str, redis_conn)
        if result['code'] == 0:
            _audit_signal_stage(redis_conn, sid, "executed", ticket=result.get('mt5_ticket'))
            log.info("✅ MT5 ticket=%s lot=%s", result.get('mt5_ticket'), result.get('volume'))
            _exec_account = ACCOUNT_ID_MODE if ACCOUNT_ID_MODE is not None else (
                int(msg_data.get("account_id", 0)) or 0)
            # 【E 组 P2-11】开仓价以 MT5 实际成交价为准（原用信号 entry_price，
            # PG 开仓价与实盘存在永久滑点偏差）
            _open_price = float(result.get("filled_price", 0) or msg_data.get("entry_price", 0))
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE hcm_signal.signals SET signal_status=3, updated_at=now() "
                    "WHERE signal_id=$1", sid)
                # ── 真实订单落库（修复：自动执行路径此前只写 positions、漏写 orders，
                #    致跟单复制单不进 orders 表、风控去重/每日亏损/可用保证金统计口径失真）──
                _oid = await conn.fetchval(
                    """
                    INSERT INTO hcm_trading.orders
                        (signal_id, account_id, mt5_ticket, symbol, direction,
                         open_price, lot, sl, tp, order_status, open_time)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,1,now())
                    RETURNING order_id
                    """,
                    sid, _exec_account, result.get("mt5_ticket"), msg_symbol,
                    msg_data.get("direction", "BUY"), _open_price,
                    result.get("volume", 0.01),
                    result.get("sl", 0.0), result.get("tp", 0.0))
                await conn.execute("""
                    INSERT INTO hcm_trading.positions
                    (account_id, symbol, direction, open_price, current_price, lot, sl, tp, mt5_ticket, open_time, signal_id, order_id)
                    VALUES ($1,$2,$3,$4,$4,$5,$6,$7,$8,now(),$9,$10)
                """, _exec_account, msg_symbol,
                     msg_data.get("direction", "BUY"),
                     _open_price,
                     result.get("volume", 0.01),
                    result.get("sl", 0.0),
                    result.get("tp", 0.0),
                    result.get("mt5_ticket"),
                    sid, _oid)
            # P3 归因修复：建立 ticket→signal_id 映射，供平仓对账找回 signal_id
            # MT5 持仓对象无 signal_id 属性，UPSERT 路径会丢；Redis 映射与开仓路径无关
            _ticket = result.get("mt5_ticket")
            if _ticket:
                try:
                    redis_conn.set(f"hcm:signal_for_ticket:{_ticket}", sid, ex=2592000)
                except Exception as _e:
                    log.warning("signal map set failed ticket=%s: %s", _ticket, _e)
                # G4 复核（2026-07-23）：主号已先平（CLOSE 早于 OPEN 到达）时，
                # signal_id(==主号 ticket) 挂有 pending-close → 开仓后立即平掉该跟单号，
                # 杜绝孤儿仓，确保主号动作低延时复刻。
                try:
                    _pc = redis_conn.get(f"bridge:mirror_pending_close:{ACCOUNT_ID_MODE}:{sid}")
                    if _pc:
                        redis_conn.delete(f"bridge:mirror_pending_close:{ACCOUNT_ID_MODE}:{sid}")
                        _pc_sym = _pc.decode() if isinstance(_pc, bytes) else str(_pc)
                        log.info("G4 pending-close reconcile: closing just-opened ticket=%s (master=%s)", _ticket, sid)
                        await _force_close_positions(mt5, redis_conn, _pc_sym, "ticket", int(sid))
                except Exception as _e:
                    log.warning("pending-close reconcile failed sid=%s: %s", sid, _e)
            # ── 真实开仓事件 → 通知(钉钉)单一事实源 ──
            # dispatcher 处于 stub 模式会推假 ticket；通知改为由 MT5 真实开仓驱动。
            try:
                _exec_symbol = real_symbol(msg_symbol) or msg_symbol
                _exec_account = ACCOUNT_ID_MODE if ACCOUNT_ID_MODE else int(msg_data.get("account_id", 0) or 0)
                redis_conn.xadd("order:executed", {
                    "account_id": str(_exec_account),
                    "account_role": "master" if IS_MASTER else ("follower" if IS_FOLLOWER else "standalone"),
                    "symbol": _exec_symbol,
                    "direction": str(msg_data.get("direction", "BUY")).upper(),
                    "lot": str(result.get("volume", msg_data.get("lot", 0.01))),
                    "filled_price": str(result.get("filled_price", 0.0)),
                    "entry_price": str(msg_data.get("entry_price", result.get("filled_price", 0.0))),
                    "mt5_ticket": str(result.get("mt5_ticket", 0)),
                    "signal_id": str(sid),
                    "ts": str(int(time.time())),
                })
                log.info("📨 order:executed published ticket=%s symbol=%s dir=%s",
                         result.get("mt5_ticket"), _exec_symbol, msg_data.get("direction"))
            except Exception as _e:
                log.warning("order:executed publish failed ticket=%s: %s", result.get("mt5_ticket"), _e)
            # ── B-1 跟单锚点：主号(非跟单)真实成交后写「成交价」给跟单号 ──
            # 跟单号 place_mt5_order 以「主号真实成交价」作滑点基准(而非 T0 entry_price 快照)，
            # 价差收敛到跨经纪商点差级(而非 T0→T_now 链路延迟漂移)。主号路径仍用 T0 基准+0.3 闸门，
            # 此键仅作跟单号读取锚点，不反向影响主号。失败静默(fail-open)。
            if (IS_MASTER or not IS_FOLLOWER) and _ticket:
                try:
                    _fill = float(result.get("filled_price") or result.get("price") or 0.0)
                    if _fill > 0:
                        redis_conn.set(
                            f"hcm:master:fill:{sid}",
                            json.dumps({"price": _fill, "ts": int(time.time() * 1000)}),
                            ex=300)
                except Exception as _e:
                    log.debug("master fill publish skipped sid=%s: %s", sid, _e)
            return True
        else:
            _audit_signal_stage(redis_conn, sid, "bridge_failed",
                                code=result.get("code"), msg=result.get("message"))
            await _mark_signal_blocked(
                pool, sid, f"bridge_failed({result.get('code')})")
            log.error("❌ MT5 order failed: %s", result.get('message', '?'))
            return False
    else:
        log.info("[DRY] %s %s lot", msg_data.get('direction'), msg_data.get('lot', 0.01))
        return True


async def _force_close_positions(mt5, redis_conn, symbol: str, mode: str = "all", master_ticket: int = 0) -> int:
    """P1 FORCE_CLOSE：平掉指定 symbol 的持仓（P3 安全反转）。

    mode='all' 平全部；mode='half' 每仓平一半手数（多仓时）。
    无持仓返回 0（安全 no-op，不报错）。返回实际平仓笔数。

    支持逻辑名（如 'XAUUSD'）和真实 broker 名（如 'XAUUSD_'）：
    - 优先用 real_symbol(symbol) 转换后查（精确）
    - 兜底：查全量 positions，再按 real_symbol(symbol) 或 symbol 匹配
    """
    # ── 账户可用性铁律（2026-08-28 新增）──
    # 平仓路径统一兜底闸门：账户不可用则【硬失败】跳过平仓（持仓保留，不误平/不卡死），
    # 恢复后下一轮正常平。绝不向未知/冻结账号发平仓指令（可能平错/平不掉）。
    if not mt5_trade_ready(mt5):
        log.critical("MT5 account NOT trade-ready — FORCE_CLOSE SKIPPED by trade-ready guard "
                     "(positions retained)")
        return 0
    real_sym = real_symbol(symbol) if symbol else ""
    try:
        # 根因修复(2026-07-27)：一律取【全量】持仓，绝不用 mt5.positions_get(symbol=...) 按名查询——
        # 跨经纪商终端(如 STARTRADER)未把品种加入 Market Watch、或经纪商品种名带后缀时，按名查询
        # 会返回空，导致「主号平仓→跟单号未跟随」的孤儿单。全量查询后在 Python 侧按品种名过滤；
        # 精确按票平仓(mode='ticket')完全不过品种名，仅靠下方循环 hcm:signal_for_ticket 映射匹配，
        # 彻底免疫经纪商品种名/行情订阅差异。
        all_pos = mt5.positions_get()
    except Exception as exc:
        log.error("FORCE_CLOSE positions_get failed for %s (real=%s): %s", symbol, real_sym, exc)
        return 0
    if all_pos is None:
        all_pos = []
    if mode == "ticket":
        # 精确按票平仓：不按品种名过滤（品种名不可靠），依赖下方循环映射精确匹配
        positions = list(all_pos)
    else:
        if not (real_sym or symbol):
            positions = list(all_pos)
        else:
            positions = [p for p in all_pos
                         if (real_sym and p.symbol == real_sym)
                         or (symbol and p.symbol == symbol)]
    if not positions:
        log.info("FORCE_CLOSE: no open positions for %s (real=%s) — nothing to close", symbol, real_sym)
        return 0
    closed = 0
    for pos in positions:
        # 2026-07-23 精确按票平仓：仅平「signal_id == 主号被平 ticket」的跟单号，
        # 避免 mode=all 误伤同品种其他跟单号（修复 G1/G2：多仓/分批建仓误平）。
        if master_ticket:
            try:
                _mapped = redis_conn.get(f"hcm:signal_for_ticket:{pos.ticket}")
                if _mapped is None:
                    log.info("FORCE_CLOSE skip (no master map): follower ticket=%s", pos.ticket)
                    continue
                _mapped_sid = int(_mapped.decode() if isinstance(_mapped, bytes) else _mapped)
                if _mapped_sid != int(master_ticket):
                    log.info("FORCE_CLOSE skip (ticket mismatch): follower=%s map=%s target=%s",
                             pos.ticket, _mapped_sid, master_ticket)
                    continue
            except Exception as e:
                log.warning("FORCE_CLOSE map check failed ticket=%s: %s", pos.ticket, e)
                continue
        try:
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                log.warning("FORCE_CLOSE: no tick for %s, skip ticket=%s", pos.symbol, pos.ticket)
                continue
            close_volume = pos.volume
            if mode == "half" and len(positions) > 1:
                close_volume = round(pos.volume / 2.0, 2)
            close_type = (_mt5_global.ORDER_TYPE_BUY
                          if pos.type == _mt5_global.ORDER_TYPE_SELL
                          else _mt5_global.ORDER_TYPE_SELL)
            close_price = tick.ask if pos.type == _mt5_global.ORDER_TYPE_SELL else tick.bid
            request = {
                "action": _mt5_global.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": close_volume,
                "type": close_type,
                "position": pos.ticket,
                "price": close_price,
                "deviation": 50,
                "magic": pos.magic,
                "comment": "HCM_FORCE_CLOSE",
                "type_time": _mt5_global.ORDER_TIME_GTC,
                "type_filling": _mt5_global.ORDER_FILLING_IOC,
            }
            result = _mt5_global.order_send(request)
            if result is None:
                log.error("FORCE_CLOSE order_send None ticket=%s: %s", pos.ticket, _mt5_global.last_error())
                continue
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                log.error("FORCE_CLOSE failed ticket=%s retcode=%s (%s)",
                          pos.ticket, result.retcode, result.comment)
                continue
            closed += 1
            # ── 信号冷却（2026-08-11）：强制平仓（含跟单镜像平仓）→ 抑制该品种 N 秒新开仓 ──
            # 仅全平（all/ticket）触发；half 部分平仓不算一次完整平仓，不重置冷却。
            if mode != "half":
                _record_after_close_cooldown(redis_conn, logic_symbol(pos.symbol))
            log.warning("FORCE_CLOSE closed ticket=%s vol=%.2f mode=%s", pos.ticket, close_volume, mode)
        except Exception as exc:
            log.exception("FORCE_CLOSE position close error ticket=%s: %s", pos.ticket, exc)
    return closed


# ─────────────────────────────────────────────────────────────────────────────
# 跟单号每日盈亏熔断（2026-08-11）
# 跟单桥专属：当日净盈亏 = 浮动盈亏(全部跟单持仓) + 今日已平仓落袋盈亏(MT5 历史成交)。
# 超盈利/亏损上限 → 全平跟单号、置 Redis 熔断标志(到期=次日 resume_time 自动恢复)、停止当日跟单。
# 主号桥(IS_MASTER)不触发——主号盈亏不受此控制，互不影响。
# ─────────────────────────────────────────────────────────────────────────────

_FOLLOW_CIRCUIT_KEY_TMPL = "bridge:follow:circuit:{account}"  # 熔断标志键（带 TTL 到次日恢复时间）
# 【2026-08-25 按账户迁移·本金基准】每日 07:00(resume_time) 记录跟单号账户本金 balance，
# 作熔断百分比分母的固定基准（键值=7点时本金）。每日 07:00 由主循环刷新。
_FOLLOW_BASELINE_KEY_TMPL = "bridge:follow:baseline:{account}"
_FOLLOW_BASELINE_LAST_DAY = None  # 当日是否已记录本金基准（date 类型）
_DEAL_ENTRY_OUT = getattr(_mt5_global, "DEAL_ENTRY_OUT", 1)


def _follower_daily_pnl(mt5) -> float:
    """计算跟单号当日净盈亏 = 浮动盈亏(全盘跟单持仓) + 今日已平仓落袋盈亏。

    跟单桥连接的 MT5 终端只含跟单号持仓，故 positions_get() 即跟单号全部持仓，
    history_deals_get(今日) 即跟单号今日全部平仓成交。无需按 account 过滤。
    """
    net = 0.0
    try:
        positions = mt5.positions_get()
    except Exception as exc:
        log.warning("follower daily pnl: positions_get failed: %s", exc)
        positions = None
    if positions:
        for p in positions:
            try:
                net += float(p.profit)
            except Exception:
                pass
    try:
        # MT5 history_deals_get 的 deal time 是经纪商服务器时间（通常为 UTC），
        # 故“今日”边界必须与 UTC 对齐，避免主机本地时区(如 UTC+8)把昨日深夜
        # 亏损误算进“今日”净盈亏、放大触发比例。
        now = datetime.now(timezone.utc)
        today_start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=timezone.utc)
        deals = mt5.history_deals_get(today_start, now)
    except Exception as exc:
        log.warning("follower daily pnl: history_deals_get failed: %s", exc)
        deals = None
    if deals:
        for d in deals:
            try:
                if getattr(d, "entry", None) == _DEAL_ENTRY_OUT:
                    net += float(d.profit)
            except Exception:
                pass
    return net


def _follower_account_balance(mt5) -> float:
    """取跟单号账户本金(balance)（熔断百分比基数分母）。

    【2026-08-25 按用户需求改】分母基准 = 账户本金(balance) 而非权益(equity)。
    熔断公式：(浮盈 + 今日已实现) / 7点时本金(balance) × 100，百分比语义 =
    "当日相对初始本金的盈亏比率"，基准固定不回冲（避免 equity 分母随浮盈动态
    变化导致的百分比失真：盈利稀释/亏损放大）。实际分母优先取每日 07:00 记录的
    baseline（Redis bridge:follow:baseline:{account}），本函数返回当前本金作回退。
    """
    try:
        info = mt5.account_info()
    except Exception as exc:
        log.warning("follower circuit: account_info failed: %s", exc)
        return 0.0
    if info is None:
        return 0.0
    try:
        return float(info.balance)
    except Exception:
        return 0.0


def _follower_resume_ttl(resume_time_str: str) -> int:
    """计算熔断键 TTL = 距现在最近的【次日】resume_time(HH:MM, 本地时区)。

    设计意图：熔断后到「次日 resume_time」自动恢复跟单（见部署记忆 40554216），
    而非永久停跟。旧实现强制要求“距现在 ≥24h 的 resume_time”，导致下午触发的熔断
    被推到「后天」（≈46h 才恢复），跟单号长时间不复活，用户误以为熔断失效/卡死。

    现改为：恢复点固定为触发日【次日】的 resume_time——触发时刻晚于 resume_time 时
    距现在约 16~24h，早于 resume_time 时已 +1 天（约 24~25h），均跨过当天，既不会
    “几小时内就恢复”提前跟单，也不会被拉长到后天。恢复后由 _check_follower_circuit_break
    的宽限期（_FOLLOWER_CIRCUIT_RECOVERED_AT）兜底，避免恢复瞬间当日盈亏偶超阈即再熔断。
    """
    hh, mm = 7, 0
    try:
        _p = str(resume_time_str).split(":")
        hh, mm = int(_p[0]), int(_p[1])
    except Exception:
        pass
    now = datetime.now()
    # 固定取触发日次日的 resume_time 作为恢复点（永远跨到明天，杜绝提前恢复）
    target = (now + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    ttl = int((target - now).total_seconds())
    return max(ttl, 3600)  # 兜底：至少 1h，防边界计算异常


async def _check_follower_circuit_break(mt5, redis_conn, pool, account_id) -> None:
    """跟单号每日盈亏熔断检查（主循环每 ~30s 调用一次，仅跟单桥）。

    行为：
      · 未启用 → 清除内存熔断标志，返回。
      · 熔断标志键仍存在(Redis) → 维持内存标志=True（当日仍暂停），返回。
      · 标志键已过期(到 resume_time) → 清除内存标志（自动恢复跟单）。
      · 计算当日净盈亏，盈利/亏损超上限 → 全平跟单号 + 置熔断标志键(TTL=到次日 resume_time)。
    通过模块全局 FOLLOW_CIRCUIT_BROKEN 控制信号消费与 reconcile 补开的闸门。

    【2026-08-25 按账户迁移】熔断阈值不再读全局 close.follow_*，改为读本跟单号
    hcm_copy.relationships 的 max_daily_profit / max_daily_loss / circuit_break_enabled
    （per-account 默认值：running 关系已回填全局旧值 30/50）。分母由"当前 equity"
    改为"每日 07:00 记录的账户本金 balance 基准"（Redis bridge:follow:baseline:{account}，
    缺失回退当前 balance）。MT5 未激活账户不接受信号且跳过熔断。
    """
    global FOLLOW_CIRCUIT_BROKEN, _FOLLOW_CIRCUIT_LAST_LOG_TS, _FOLLOW_CIRCUIT_WAS_BROKEN, _FOLLOWER_CIRCUIT_RECOVERED_AT
    # ── 未激活闸门：MT5 未激活账户不接受信号 + 跳过熔断（用户需求：MT5激活态才接受信号）──
    try:
        if pool is not None:
            _acc = await pool.fetchrow(
                "SELECT is_active FROM hcm_broker.accounts WHERE account_id=$1",
                account_id,
            )
            if _acc is not None and _acc["is_active"] is not True:
                FOLLOW_CIRCUIT_BROKEN = False
                return  # 未激活：不熔断、也不接收信号（信号接收闸门同样检查 is_active）
    except Exception as _acc_exc:
        log.warning("follower circuit: account active check failed: %s", _acc_exc)
    # ── 按账户熔断阈值（读 relationships，per-account；缺/异常回退全局旧值兜底）──
    _p_max = 0.0
    _l_max = 0.0
    _c_enabled = True
    _rel = None
    try:
        if pool is not None:
            _rel = await pool.fetchrow(
                "SELECT max_daily_profit, max_daily_loss, circuit_break_enabled "
                "FROM hcm_copy.relationships "
                "WHERE copy_account_id=$1 AND status='running' LIMIT 1",
                account_id,
            )
            if _rel is not None:
                _p_max = float(_rel["max_daily_profit"] or 0.0)
                _l_max = float(_rel["max_daily_loss"] or 0.0)
                _c_enabled = bool(_rel["circuit_break_enabled"])
    except Exception as _rel_exc:
        log.warning("follower circuit: relationships read failed: %s", _rel_exc)
    if _rel is None:
        # 无 running 跟单关系或查询失败 → 回退全局旧值（兼容未迁移数据/兜底）
        _p_max = _get_close_config(
            redis_conn, "close.follow_daily_profit_max",
            CLOSE_CONFIG_DEFAULTS["close.follow_daily_profit_max"],
        )
        _l_max = _get_close_config(
            redis_conn, "close.follow_daily_loss_max",
            CLOSE_CONFIG_DEFAULTS["close.follow_daily_loss_max"],
        )
    if not _c_enabled:
        FOLLOW_CIRCUIT_BROKEN = False
        return
    key = _FOLLOW_CIRCUIT_KEY_TMPL.format(account=account_id)
    try:
        _existing = redis_conn.get(key)
    except Exception:
        _existing = None
    if _existing:
        # 熔断仍生效（当日暂停），维持标志；键到 TTL 自动过期后下次检查恢复
        FOLLOW_CIRCUIT_BROKEN = True
        _FOLLOW_CIRCUIT_WAS_BROKEN = True
        return
    # 标志已过期/不存在 → 确认未熔断
    FOLLOW_CIRCUIT_BROKEN = False
    if _FOLLOW_CIRCUIT_WAS_BROKEN:
        # 刚从熔断恢复：记录恢复时刻，进入宽限期（默认 1h 内不重新熔断），
        # 避免恢复瞬间当日净盈亏偶超阈值导致“清键立刻再熔断”的死循环，
        # 给跟单号追赶主号持仓的时间。
        _FOLLOW_CIRCUIT_WAS_BROKEN = False
        _FOLLOWER_CIRCUIT_RECOVERED_AT = time.time()
    if time.time() - _FOLLOWER_CIRCUIT_RECOVERED_AT < 3600:
        return  # 恢复宽限期内：只放行跟单，不重新评估熔断
    profit_max = _p_max
    loss_max = _l_max
    if profit_max <= 0 and loss_max <= 0:
        return  # 双向均未设置上限（百分比 0=不限制）→ 不熔断
    try:
        balance = _follower_account_balance(mt5)
    except Exception as exc:
        log.warning("follower circuit: balance fetch failed: %s", exc)
        balance = 0.0
    try:
        net = _follower_daily_pnl(mt5)
    except Exception as exc:
        log.warning("follower circuit: pnl calc failed: %s", exc)
        return
    # 限额百分比基数分母：优先取【每日 07:00 记录的账户本金 balance 基准】(Redis
    # bridge:follow:baseline:{account})，缺失则回退当前本金 balance（_follower_account_balance
    # 已改为返回 balance）。【2026-08-25 按用户需求】分母=本金基准，非 equity——百分比
    # 语义="当日相对初始本金的盈亏比率"，基准固定不回冲，避免 equity 分母动态失真。
    base = 0.0
    _base_label = "baseline_balance"
    try:
        _bs = redis_conn.get(_FOLLOW_BASELINE_KEY_TMPL.format(account=account_id))
        if _bs:
            base = float(_bs)
    except Exception:
        base = 0.0
    if base <= 0:
        base = balance if balance > 0 else 0.0
        if base <= 0:
            log.warning(
                "follower circuit: baseline & balance unavailable (<=0) — skip check"
            )
            return
    # 净盈亏 / 7点本金基准 × 100
    profit_pct = (net / base * 100.0) if net > 0 else 0.0
    loss_pct = (-net / base * 100.0) if net < 0 else 0.0
    # 绝对金额下限：百分比达标【且】绝对金额也超下限才熔断，避免小账户被微小绝对亏损误杀。
    profit_abs_min = float(_get_close_config(
        redis_conn, "close.follow_daily_profit_abs_min",
        CLOSE_CONFIG_DEFAULTS["close.follow_daily_profit_abs_min"],
    ))
    loss_abs_min = float(_get_close_config(
        redis_conn, "close.follow_daily_loss_abs_min",
        CLOSE_CONFIG_DEFAULTS["close.follow_daily_loss_abs_min"],
    ))
    # 可观测诊断：每 300s 打印一次熔断计算值（net/base/profit_pct），便于核对是否生效
    _now_ts = time.time()
    global _FOLLOW_CIRCUIT_LAST_LOG_TS
    if _now_ts - _FOLLOW_CIRCUIT_LAST_LOG_TS > 300:
        _FOLLOW_CIRCUIT_LAST_LOG_TS = _now_ts
        log.info(
            "follower circuit check: net=%.2f base=%.2f(%s) profit_pct=%.2f%% loss_pct=%.2f%% "
            "limits(profit=%.2f loss=%.2f abs_min profit=%.2f loss=%.2f)",
            net, base, _base_label, profit_pct, loss_pct,
            profit_max, loss_max, profit_abs_min, loss_abs_min,
        )
    hit = None
    # 双重条件：百分比达标 且 绝对金额达标（绝对下限>0 时）。
    if profit_max > 0 and profit_pct >= profit_max:
        if profit_abs_min <= 0 or net >= profit_abs_min:
            hit = ("profit", net, profit_pct, profit_max)
        else:
            log.info(
                "follower circuit: profit pct %.2f%% >= limit %.2f%% but abs net %.2f < "
                "abs_min %.2f — not tripping",
                profit_pct, profit_max, net, profit_abs_min,
            )
    elif loss_max > 0 and loss_pct >= loss_max:
        if loss_abs_min <= 0 or (-net) >= loss_abs_min:
            hit = ("loss", net, loss_pct, loss_max)
        else:
            log.info(
                "follower circuit: loss pct %.2f%% >= limit %.2f%% but abs net %.2f < "
                "abs_min %.2f — not tripping",
                loss_pct, loss_max, -net, loss_abs_min,
            )
    if hit:
        _kind, _net, _pct, _limit = hit
        try:
            _resume = redis_conn.hget("hcm:config:v2", "close.follow_resume_time")
        except Exception:
            _resume = None
        if not _resume:
            _resume = CLOSE_CONFIG_DEFAULTS["close.follow_resume_time"]
        try:
            ttl = _follower_resume_ttl(_resume)
        except Exception:
            ttl = 86400
        log.warning(
            "FOLLOWER CIRCUIT-BREAK (%s): daily net PnL=%.2f (%.2f%% of %s %.2f) "
            "hit limit=%.2f%% — closing all follower positions, pausing follow >=24h "
            "(resume_time=%s, ttl=%ds ≈ %.1fh, until %s)",
            _kind, _net, _pct, _base_label, base, _limit,
            _resume, ttl, ttl / 3600.0,
            (datetime.now() + timedelta(seconds=ttl)).strftime("%Y-%m-%d %H:%M"),
        )
        try:
            closed = await _force_close_positions(mt5, redis_conn, None, "all")
        except Exception as fc_exc:
            log.error("follower circuit: force-close failed: %s", fc_exc)
            closed = -1
        try:
            redis_conn.set(key, json.dumps({
                "kind": _kind, "net_pnl": round(_net, 2),
                "pct": round(_pct, 2), "limit_pct": _limit,
                "base_kind": _base_label, "base": round(base, 2),
                "balance": round(balance, 2),
                "at": int(time.time()), "closed": closed,
            }), ex=ttl)
        except Exception as set_exc:
            log.warning("follower circuit: set flag failed: %s", set_exc)
        FOLLOW_CIRCUIT_BROKEN = True


_RECON_LAST_LOG = 0.0  # reconcile 审计日志节流计时


async def _force_close_one(mt5, redis_conn, pos) -> bool:
    """精确平掉跟单号单个持仓（按 ticket），用于 reconcile 清同品种多余/重复单。返回是否成功。"""
    try:
        tick = mt5.symbol_info_tick(pos.symbol)
        if not tick:
            log.warning("Reconcile: no tick for %s, skip ticket=%s", pos.symbol, pos.ticket)
            return False
        close_type = (_mt5_global.ORDER_TYPE_BUY
                      if pos.type == _mt5_global.ORDER_TYPE_SELL
                      else _mt5_global.ORDER_TYPE_SELL)
        close_price = tick.ask if pos.type == _mt5_global.ORDER_TYPE_SELL else tick.bid
        request = {
            "action": _mt5_global.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": close_price,
            "deviation": 50,
            "magic": pos.magic,
            "comment": "HCM_RECONCILE",
            "type_time": _mt5_global.ORDER_TIME_GTC,
            "type_filling": _mt5_global.ORDER_FILLING_IOC,
        }
        result = _mt5_global.order_send(request)
        if result is None:
            log.error("Reconcile: order_send None ticket=%s: %s", pos.ticket, _mt5_global.last_error())
            return False
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("Reconcile: close failed ticket=%s retcode=%s (%s)",
                      pos.ticket, result.retcode, result.comment)
            return False
        log.warning("Reconcile: closed excess follower ticket=%s sym=%s vol=%.2f",
                    pos.ticket, pos.symbol, pos.volume)
        return True
    except Exception as exc:
        log.exception("Reconcile: position close error ticket=%s: %s", pos.ticket, exc)
        return False


async def _open_follower_position(mt5, redis_conn, pool, mp: dict,
                                  master_account_id, msid: str) -> bool:
    """焊死·补缺失：复制主号一笔持仓到跟单号（reconcile 对账兜底）。

    当 reconcile 发现主号持仓集合含某 signal_id、但跟单号无对应持仓时调用。
    市价开跟单仓，复制主号方向/手数/SL/TP（手数按 FOLLOW_LOT_MULT 缩放），
    并登记与主号一致的 hcm:signal_for_ticket 映射，使后续主号平仓镜像能精确匹配。
    主号快照 symbol 经 real_symbol() 翻译为跟单实盘名（XAUUSD→XAUUSD_），规避品种名不一致。
    """
    try:
        _real = real_symbol(mp.get("symbol") or SYMBOL)
        if not mt5.symbol_select(_real, True):
            log.warning("Reconcile open: symbol_select failed %s", _real)
            return False
        tick = mt5.symbol_info_tick(_real)
        if not tick:
            log.warning("Reconcile open: no tick for %s (real=%s)", mp.get("symbol"), _real)
            return False
        direction = mp.get("direction")
        if direction not in ("BUY", "SELL"):
            log.warning("Reconcile open: bad direction %r", direction)
            return False
        _mult = FOLLOW_LOT_MULT.get(master_account_id, 1.0) or 1.0
        vol = float(mp.get("volume") or 0.0) * _mult
        if vol <= 0:
            log.warning("Reconcile open: zero volume for master=%s", master_account_id)
            return False
        sl = float(mp.get("sl") or 0.0)
        tp = float(mp.get("tp") or 0.0)
        if direction == "BUY":
            otype = _mt5_global.ORDER_TYPE_BUY
            price = tick.ask
        else:
            otype = _mt5_global.ORDER_TYPE_SELL
            price = tick.bid
        request = {
            "action": _mt5_global.TRADE_ACTION_DEAL,
            "symbol": _real,
            "volume": vol,
            "type": otype,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 50,
            "magic": 0,
            "comment": f"HCM_RECONCILE_OPEN:{msid}",
            "type_time": _mt5_global.ORDER_TIME_GTC,
            "type_filling": _mt5_global.ORDER_FILLING_IOC,
        }
        result = _mt5_global.order_send(request)
        if result is None:
            log.error("Reconcile open: order_send None sym=%s: %s", _real, _mt5_global.last_error())
            return False
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("Reconcile open failed sym=%s vol=%.2f retcode=%s (%s)",
                      _real, vol, result.retcode, result.comment)
            return False
        _fticket = result.order
        try:
            redis_conn.set(f"hcm:signal_for_ticket:{_fticket}", msid, ex=2592000)
        except Exception as _e:
            log.warning("Reconcile open: signal map set failed ticket=%s: %s", _fticket, _e)
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO hcm_trading.positions
                            (account_id, symbol, direction, open_price, current_price,
                             lot, sl, tp, mt5_ticket, open_time, signal_id, order_id)
                        VALUES ($1,$2,$3,$4,$4,$5,$6,$7,$8,now(),$9,$10)
                        ON CONFLICT (mt5_ticket) DO NOTHING
                        """,
                        ACCOUNT_ID_MODE, _real, direction, price, vol, sl, tp,
                        _fticket, msid, 0)
            except Exception as _pe:
                log.warning("Reconcile open: PG insert failed ticket=%s: %s", _fticket, _pe)
        log.warning(
            "Reconcile: OPENED follower position sym=%s vol=%.2f sl=%.2f tp=%.2f "
            "master_sid=%s follower_ticket=%s",
            _real, vol, sl, tp, msid, _fticket)
        return True
    except Exception as exc:
        log.exception("Reconcile open error: %s", exc)
        return False


async def reconcile_follower_positions(mt5, redis_conn, pool) -> None:
    """跟单桥持仓对账兜底：根治手动平仓事件丢失→孤儿单，以及同品种重复开仓→2x 敞口。

    周期对比「主号实时持仓快照」(hcm:master:positions:{master_aid}) 与「跟单号本地持仓」，
    按品种计数：跟单号某品种持仓数 > 主号该品种持仓数 → 平掉多余的（excess）持仓，
    保持 跟单数 == 主号数 的 1:1 复制（既清跨品种孤儿单，也清同品种重复单）。

      · 主号快照不可用（主号桥离线/无数据）→ 跳过，不平（防误平）。
      · 仅做「清多余」单向兜底，不反向开仓（开仓由信号链路负责，避免重复开仓）。
    仅 IS_FOLLOWER 且 FOLLOW_MASTERS 非空时生效。
    """
    if not IS_FOLLOWER or not FOLLOW_MASTERS:
        return
    global _RECON_LAST_LOG
    if time.time() - _RECON_LAST_LOG > 60:
        log.info(f"Reconcile active: IS_FOLLOWER={IS_FOLLOWER} masters={FOLLOW_MASTERS} (audit tick)")
        _RECON_LAST_LOG = time.time()

    def _pts(p):
        try:
            return float(getattr(p, "time", 0) or 0)
        except Exception:
            return 0.0

    # 主号实时持仓：按品种计数
    # 键「存在但为空」(主号确实空仓) → 该品种 master_count=0 → 跟单多余的全部平；
    # 键「不存在/读失败」(主号桥离线) → 防误平跳过。
    master_count = {}
    for mid in FOLLOW_MASTERS:
        key = f"hcm:master:positions:{mid}"
        try:
            if not redis_conn.exists(key):
                return
            snap = redis_conn.hgetall(key)
        except Exception:
            return
        for info_json in snap.values():
            try:
                if isinstance(info_json, bytes):
                    info_json = info_json.decode()
                d = json.loads(info_json)
                # 新鲜度闸门：快照超龄 → 主号桥离线，整轮跳过（不误平孤儿）
                _ua = d.get("updated_at") if isinstance(d, dict) else None
                if _ua:
                    try:
                        if (time.time() - int(_ua)) > MASTER_SNAPSHOT_MAX_AGE_SEC:
                            log.warning(
                                "Reconcile: master snapshot stale (age=%.0fs > %ds) for %s — "
                                "skip whole pass (refuse blind close)",
                                time.time() - int(_ua), MASTER_SNAPSHOT_MAX_AGE_SEC, mid)
                            return
                    except Exception:
                        pass
                s = d.get("symbol")
                if s:
                    # 【P0-6】归一到逻辑名空间再计数（跨券商品种名可能不同）
                    s = logic_symbol(s)
                    master_count[s] = master_count.get(s, 0) + 1
            except Exception:
                pass  # 跳过哨兵/非持仓字段
    # 跟单号实时持仓：按品种分组（【P0-6】归一逻辑名，与主号快照同空间比较）
    # 【2026-08-18 跟单未同步根治】空仓不再直接 return：跟单号无持仓时，
    # 仍须执行下方 signal_id 对齐补开（主号有仓、跟单缺 → 补开）。旧逻辑
    # `if not fpos: return` 使跟单空仓时 reconcile 整段跳过，主号开新单跟单永不补开。
    try:
        fpos = mt5.positions_get()
    except Exception as e:
        log.warning(f"Reconcile: positions_get failed: {e}")
        return
    if not fpos:
        fpos = []
    follower_by_sym = {}
    for p in fpos:
        follower_by_sym.setdefault(logic_symbol(p.symbol), []).append(p)
    now_ts = time.time()

    # ── 焊死·按 signal_id 精确对齐：补缺失 + 清孤儿 ──
    # 【2026-08-18 重构】此块原在下方 `for sym` 循环内，跟单空仓时因 fpos 为空、
    # for 循环不进入 → 补开逻辑永不执行（跟单未同步根因）。现提升到 reconcile 顶层，
    # 无论跟单空仓与否都执行：主号有、跟单缺 → 补开；跟单有、主号无 → 清孤儿。
    # 根治两层缺陷：
    #  ① 品种名 XAUUSD vs XAUUSD_ 不匹配 → master_count.get(XAUUSD_)==0 →
    #     把跟单号整个品种持仓当“多余”全平（跟单号被异常清仓）。
    #  ② reconcile 只平多余、不补缺失 → 主号桥启动前已开的老单（信号过期无法复制）
    #     或镜像平仓事件丢失的单，跟单号永远缺单，无法自愈。
    # 做法：主号每笔持仓经 hcm:signal_for_ticket:{master_ticket} 反查 signal_id
    #       （缺则回退主号 ticket）；跟单号每笔持仓同样反查 signal_id。
    #       主号有、跟单缺 → 市价补开（复制主号方向/手数/SL/TP，按跟随倍率缩放）；
    #       跟单有、主号无 → 平孤儿（刚开仓 20s 安全闸保护）。
    try:
        _master_ids = {}
        for _mid in FOLLOW_MASTERS:
            _key = f"hcm:master:positions:{_mid}"
            try:
                _snap = redis_conn.hgetall(_key)
            except Exception:
                continue
            # 新鲜度闸门：该主号快照超龄 → 本轮按 signal_id 对齐整段跳过
            try:
                _stale = False
                for _v in _snap.values():
                    try:
                        _vd = json.loads(_v.decode() if isinstance(_v, bytes) else _v)
                        _ua = _vd.get("updated_at") if isinstance(_vd, dict) else None
                        if _ua and (time.time() - int(_ua)) > MASTER_SNAPSHOT_MAX_AGE_SEC:
                            _stale = True
                            break
                    except Exception:
                        pass
                if _stale:
                    log.warning(
                        "Reconcile align: master snapshot stale for %s — skip signal-id align pass",
                        _mid)
                    raise StopIteration
            except StopIteration:
                break
            except Exception:
                pass
            for _mt, _mv in _snap.items():
                try:
                    _mt = _mt.decode() if isinstance(_mt, bytes) else _mt
                    _md = json.loads(_mv) if isinstance(_mv, str) else _mv
                except Exception:
                    continue
                if not isinstance(_md, dict):
                    continue
                _msid = redis_conn.get(f"hcm:signal_for_ticket:{_mt}")
                _msid = _msid.decode() if isinstance(_msid, bytes) else _msid
                if not _msid:
                    _msid = str(_mt)
                _master_ids[_msid] = _md
        _follower_ids = {}
        for _fp in fpos:
            _fsid = redis_conn.get(f"hcm:signal_for_ticket:{_fp.ticket}")
            _fsid = _fsid.decode() if isinstance(_fsid, bytes) else _fsid
            if not _fsid:
                _fsid = str(_fp.ticket)
            _follower_ids[_fsid] = _fp
        # 【阶段1 去跟单化 2026-08-24】移除「补缺失」逻辑（主号有、跟单缺→补开）：
        # 跟单号改为独立下单后，信号链路已保证跟单号与主号同时收到同一信号并各自开仓，
        # reconcile 再按主号持仓补开会与独立开仓冲突（重复开仓 / 追高）。跟单号错过即
        # 放弃（行情已走远，补了止损），不再兜底补开。
        #
        # 原逻辑保留在 git 历史：RECONCILE_BACKFILL_MAX_AGE_SEC / RECONCILE_BACKFILL_MAX_SLIP_ATR
        # / _open_follower_position 补开路径已随去跟单化移除。仅保留下方「清孤儿」与
        # 「按品种 excess 清理」作为孤儿/重复单的风控兜底。
        # 跟单有、主号无 → 清孤儿
        for _sid, _fp in _follower_ids.items():
            if _sid in _master_ids:
                continue
            _pts2 = getattr(_fp, "time", 0) or 0
            if now_ts - float(_pts2) < 20:
                continue
            log.warning("Reconcile: follower ORPHAN signal_id=%s ticket=%s → close",
                        _sid, _fp.ticket)
            await _force_close_one(mt5, redis_conn, _fp)
    except Exception as _align_exc:
        log.exception("Reconcile: signal-id align pass failed: %s", _align_exc)

    # ── 按品种计数 excess 清理（清多余 / 同品种重复单）──
    # 仅当跟单号有持仓时执行；与上方 signal_id 精确对齐互补。
    for sym, fpositions in follower_by_sym.items():
        mcount = master_count.get(sym, 0)
        excess = len(fpositions) - mcount
        if excess <= 0:
            continue  # 跟单数 <= 主号数，正常 1:1，不操作
        # 保留较新的 mcount 个，平掉较早开仓的 excess 个多余/重复单
        fpositions.sort(key=_pts)
        to_close = fpositions[:excess]
        # 安全闸：跳过 20s 内刚开仓的持仓，给主号快照同步时间，避免误平刚复制的合法单
        actually_close = [p for p in to_close if now_ts - _pts(p) >= 20]
        if actually_close:
            log.warning("Reconcile: follower has %d %s vs master %d → closing %d excess",
                        len(fpositions), sym, mcount, len(actually_close))
            for pos in actually_close:
                await _force_close_one(mt5, redis_conn, pos)

def _follower_pos_matches_master(redis_conn, pos, target_ticket: int) -> bool:
    """判定跟单持仓 pos 是否镜像自主号被改/被平的那一笔（target_ticket=主号 signal_id/票）。"""
    try:
        _m = redis_conn.get(f"hcm:signal_for_ticket:{pos.ticket}")
        if _m is None:
            return False
        _m = _m.decode() if isinstance(_m, bytes) else _m
        return int(_m) == int(target_ticket)
    except Exception:
        return False


async def _sync_follower_sl_tp(mt5, redis_conn) -> None:
    """焊死·跟单 SL/TP 强制跟随主号：直接读主号持仓快照，把每笔主号持仓的精确 SL/TP
    镜像到对应跟单持仓，完全绕开「信号塔 mirror→风控→risk_passed」事件长链。
    任一环节(桥重启/抖动/字段映射)导致 modify 事件丢失，本周期兜底仍保证跟单 SL/TP
    与主号一致——根治『跟单保本/移动止盈不随主号移动』。
    仅 IS_FOLLOWER 且 FOLLOW_MASTERS 非空时生效；主号快照不可用→跳过(保守)。
    """
    if not IS_FOLLOWER or not FOLLOW_MASTERS:
        return
    # 主号快照：master_ticket -> {sl,tp,symbol,...}
    master_by_ticket = {}
    for mid in FOLLOW_MASTERS:
        key = f"hcm:master:positions:{mid}"
        try:
            if not redis_conn.exists(key):
                return
            snap = redis_conn.hgetall(key)
        except Exception:
            return
        for mt, mv in snap.items():
            if mt in (b"synced", "synced"):
                continue
            try:
                mt = mt.decode() if isinstance(mt, bytes) else mt
                d = json.loads(mv.decode() if isinstance(mv, bytes) else mv)
            except Exception:
                continue
            if isinstance(d, dict):
                # 新鲜度闸门：快照超龄 → 视为主号桥离线，本快照作废、整轮跳过
                _ua = d.get("updated_at") or 0
                try:
                    _ua = int(_ua)
                except Exception:
                    _ua = 0
                if _ua and (time.time() - _ua) > MASTER_SNAPSHOT_MAX_AGE_SEC:
                    log.warning(
                        "SLTP-FOLLOW: master snapshot stale (age=%.0fs > %ds) for %s — "
                        "skip this round (refuse blind SL/TP change)",
                        time.time() - _ua, MASTER_SNAPSHOT_MAX_AGE_SEC, mid)
                    return
                master_by_ticket[mt] = d
    if not master_by_ticket:
        return
    # master_ticket -> signal_id（与跟单持仓映射同空间比较）
    master_sid = {}
    for mt in master_by_ticket:
        # mt 已是解码后的字符串 key（与 master_by_ticket 同键空间），直接用于映射查询
        try:
            _s = redis_conn.get(f"hcm:signal_for_ticket:{mt}")
        except Exception:
            _s = None
        _s = _s.decode() if isinstance(_s, bytes) else _s
        master_sid[mt] = _s or str(mt)
    # 跟单本地持仓
    try:
        fpos = mt5.positions_get() or []
    except Exception:
        return
    if not fpos:
        return
    for p in fpos:
        try:
            _fs = redis_conn.get(f"hcm:signal_for_ticket:{p.ticket}")
        except Exception:
            _fs = None
        _fs = _fs.decode() if isinstance(_fs, bytes) else _fs
        fsid = _fs or str(p.ticket)
        m = None
        for mt_key, sid in master_sid.items():
            if sid == fsid:
                m = master_by_ticket[mt_key]
                break
        if m is None:
            continue
        new_sl = float(m.get("sl") or 0.0)
        new_tp = float(m.get("tp") or 0.0)
        cur_sl = float(p.sl) if p.sl else 0.0
        cur_tp = float(p.tp) if p.tp else 0.0
        if abs(cur_sl - new_sl) <= 1e-9 and abs(cur_tp - new_tp) <= 1e-9:
            continue  # 已一致，免打扰
        try:
            result = _mt5_global.order_send({
                "action": _mt5_global.TRADE_ACTION_SLTP,
                "position": p.ticket,
                "sl": round(new_sl, 5) if new_sl > 0 else 0.0,
                "tp": round(new_tp, 5) if new_tp > 0 else 0.0,
            })
            if result is None:
                log.warning("SLTP-FOLLOW order_send None ticket=%s: %s", p.ticket, _mt5_global.last_error())
                continue
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                # 加固：实盘受限价规则挡住（retcode=10016 Invalid stops）时，
                # 主号 SL 比跟单当前 SL 更松 → 仅当「收紧 SL」被拒才值得重试，
                # 用 max(new_sl, cur_sl*0.99) 放宽到当前 SL 内侧 0.01 倍，避免反复穿透。
                if result.retcode == 10016 and new_sl > 0 and new_sl > cur_sl:
                    _retry_sl = round(max(new_sl, cur_sl * 0.99), 5)
                    if _retry_sl > new_sl:
                        _r2 = _mt5_global.order_send({
                            "action": _mt5_global.TRADE_ACTION_SLTP,
                            "position": p.ticket,
                            "sl": _retry_sl,
                            "tp": round(new_tp, 5) if new_tp > 0 else 0.0,
                        })
                        if _r2 is not None and _r2.retcode == mt5.TRADE_RETCODE_DONE:
                            log.info("SLTP-FOLLOW: follower ticket=%s sl relaxed %s→%s (master %s, retry ok)",
                                     p.ticket, new_sl, _retry_sl, new_sl)
                            continue
                log.warning("SLTP-FOLLOW failed ticket=%s retcode=%s (%s)",
                            p.ticket, result.retcode, result.comment)
                continue
            log.info("SLTP-FOLLOW: follower ticket=%s sl=%s tp=%s (master)", p.ticket, new_sl, new_tp)
        except Exception as exc:
            log.warning("SLTP-FOLLOW error ticket=%s: %s", p.ticket, exc)


async def _modify_follower_position(mt5, redis_conn, symbol: str, sl: float, tp: float,
                                    target_ticket: int = 0) -> int:
    """Manual mirror MODIFY：把主号变动后的 SL/TP 镜像到跟单号对应持仓（order_modify）。

    target_ticket>0 时【仅改】hcm:signal_for_ticket 映射匹配该主号持仓的那一笔跟单仓
    （精确按票，根治『主号改一仓→跟单同品种全仓被改』）；target_ticket=0 时按品种定位
    全部同品种仓（兼容无映射兜底）。无匹配持仓安全 no-op。
    """
    real_sym = real_symbol(symbol) if symbol else ""
    try:
        # 一律取全量持仓后 Python 侧按品种名过滤，不用按名查询
        # （跨经纪商终端未订阅行情时按名查询返回空，导致 SL/TP 镜像漏改）。
        all_pos = mt5.positions_get()
    except Exception as exc:
        log.error("MODIFY positions_get failed for %s (real=%s): %s", symbol, real_sym, exc)
        return 0
    if all_pos is None:
        all_pos = []
    if not (real_sym or symbol):
        positions = list(all_pos)
    else:
        positions = [p for p in all_pos
                     if (real_sym and p.symbol == real_sym)
                     or (symbol and p.symbol == symbol)]
    if not positions:
        log.info("MODIFY: no open positions for %s (real=%s) — nothing to modify", symbol, real_sym)
        return 0
    # 【2026-08-13 清废逻辑】target_ticket<=0（主号改仓事件未解析出 close_ticket）时，
    # 不再按品种广播改全仓（历史误伤源：主号改一仓→跟单同品种全仓被改），
    # 直接 no-op 交 reconcile_follower_positions 按票兜底。
    if not target_ticket:
        log.warning("MODIFY target_ticket empty for %s — skip broadcast, leave to reconcile", symbol)
        return 0
    # 精确按票：只改映射匹配 target_ticket 的那一笔
    if target_ticket:
        positions = [p for p in positions if _follower_pos_matches_master(redis_conn, p, target_ticket)]
        if not positions:
            log.info("MODIFY: no follower position mapped to master target=%s for %s", target_ticket, symbol)
            return 0
    modified = 0
    for pos in positions:
        try:
            request = {
                "action": _mt5_global.TRADE_ACTION_SLTP,
                "position": pos.ticket,
                "sl": round(sl, 5) if sl > 0 else 0.0,
                "tp": round(tp, 5) if tp > 0 else 0.0,
            }
            result = _mt5_global.order_send(request)
            if result is None:
                log.error("MODIFY order_send None ticket=%s: %s", pos.ticket, _mt5_global.last_error())
                continue
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                log.error("MODIFY failed ticket=%s retcode=%s (%s)",
                          pos.ticket, result.retcode, result.comment)
                continue
            modified += 1
            log.warning("MODIFY ticket=%s sl=%s tp=%s", pos.ticket, sl, tp)
        except Exception as exc:
            log.exception("MODIFY position error ticket=%s: %s", pos.ticket, exc)
    return modified


async def _follower_consume_sltp(mt5, redis_conn) -> None:
    """毫秒级跟单 SL/TP 镜像消费：阻塞消费主号桥发布的 hcm:master:sltp 事件，
    经 _modify_follower_position 精确按票把主号 auto 持仓 SL/TP 即时镜像到跟单仓。

    事件驱动（xreadgroup block=50ms），主循环每轮调用 → 端到端延迟 ~50-250ms，
    彻底消灭原 10s _sync_follower_sl_tp 轮询滞后（行情已跑远才跟上）。
    10s 轮询保留作安全网（兜底事件丢失）。仅 IS_FOLLOWER 且 FOLLOW_MASTERS 非空时生效。
    熔断(FOLLOW_CIRCUIT_BROKEN)不阻断本路径——跟单仓仍需 SL 保护。
    """
    if not IS_FOLLOWER or not FOLLOW_MASTERS:
        return
    _gid = BRIDGE_GROUP
    _cname = f"sltp-consumer-{os.getpid()}"
    try:
        resp = redis_conn.xreadgroup(_gid, _cname, {"hcm:master:sltp": ">"}, count=32, block=50)
    except Exception as e:
        if "NOGROUP" in str(e).upper():
            try:
                redis_conn.xgroup_create("hcm:master:sltp", _gid, mkstream=True, id="$")
            except Exception:
                pass
        else:
            log.warning("SLTP-CONSUME xreadgroup error: %s", e)
        return
    if not resp:
        return
    for _stream, messages in resp:
        for msg_id, fields in messages:
            try:
                def _g(k):
                    _v = fields.get(k)
                    return _v.decode() if isinstance(_v, bytes) else _v
                master_ticket = int(float(_g("master_ticket") or 0))
                symbol = _g("symbol") or ""
                sl = float(_g("sl") or 0.0)
                tp = float(_g("tp") or 0.0)
                account_id = int(float(_g("account_id") or 0))
                if account_id not in FOLLOW_MASTERS:
                    redis_conn.xack("hcm:master:sltp", _gid, msg_id)
                    continue
                # 解析 signal_id：auto 单经 hcm:signal_for_ticket 映射到模型 signal_id，
                # 手动单回退主号 ticket 本身；与 _modify_follower_position/_follower_pos_matches_master 同空间。
                target = master_ticket
                try:
                    _m = redis_conn.get(f"hcm:signal_for_ticket:{master_ticket}")
                    if _m:
                        target = int(_m.decode() if isinstance(_m, bytes) else _m)
                except Exception:
                    pass
                await _modify_follower_position(mt5, redis_conn, symbol, sl, tp, target_ticket=target)
            except Exception as e:
                log.warning("SLTP-CONSUME event failed id=%s: %s", msg_id, e)
            finally:
                try:
                    redis_conn.xack("hcm:master:sltp", _gid, msg_id)
                except Exception:
                    pass


async def _partial_close_follower_position(mt5, redis_conn, symbol: str, volume: float,
                                            target_ticket: int = 0) -> int:
    """Manual mirror PARTIAL_CLOSE：按 symbol 定位跟单号持仓，平掉指定增量手数（1:1 镜像）。

    volume = 主号本次平掉的增量；跟单号平相同增量。无持仓安全 no-op；增量<=0 跳过。
    """
    real_sym = real_symbol(symbol) if symbol else ""
    try:
        # 根因修复(2026-07-27)：一律取全量持仓后 Python 侧按品种名过滤，不用按名查询
        # （跨经纪商终端未订阅行情时按名查询返回空，导致部分平仓镜像漏平）。
        all_pos = mt5.positions_get()
    except Exception as exc:
        log.error("PARTIAL_CLOSE positions_get failed for %s (real=%s): %s", symbol, real_sym, exc)
        return 0
    if all_pos is None:
        all_pos = []
    if not (real_sym or symbol):
        positions = list(all_pos)
    else:
        positions = [p for p in all_pos
                     if (real_sym and p.symbol == real_sym)
                     or (symbol and p.symbol == symbol)]
    if not positions:
        log.info("PARTIAL_CLOSE: no open positions for %s (real=%s) — nothing to close", symbol, real_sym)
        return 0
    # 【2026-08-13 清废逻辑】target_ticket<=0（主号部分平仓事件未解析出 close_ticket）时，
    # 不再按品种广播减全仓（历史误伤源），直接 no-op 交 reconcile_follower_positions 按票兜底。
    if not target_ticket:
        log.warning("PARTIAL_CLOSE target_ticket empty for %s — skip broadcast, leave to reconcile", symbol)
        return 0
    if target_ticket:
        positions = [p for p in positions if _follower_pos_matches_master(redis_conn, p, target_ticket)]
        if not positions:
            log.info("PARTIAL_CLOSE: no follower position mapped to master target=%s for %s", target_ticket, symbol)
            return 0
    closed = 0
    for pos in positions:
        try:
            close_volume = round(min(volume, pos.volume), 2)
            if close_volume <= 0:
                continue
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                log.warning("PARTIAL_CLOSE: no tick for %s, skip ticket=%s", pos.symbol, pos.ticket)
                continue
            close_type = (_mt5_global.ORDER_TYPE_BUY
                          if pos.type == _mt5_global.ORDER_TYPE_SELL
                          else _mt5_global.ORDER_TYPE_SELL)
            close_price = tick.ask if pos.type == _mt5_global.ORDER_TYPE_SELL else tick.bid
            request = {
                "action": _mt5_global.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": close_volume,
                "type": close_type,
                "position": pos.ticket,
                "price": close_price,
                "deviation": 50,
                "magic": pos.magic,
                "comment": "HCM_MIRROR_PARTIAL",
                "type_time": _mt5_global.ORDER_TIME_GTC,
                "type_filling": _mt5_global.ORDER_FILLING_IOC,
            }
            result = _mt5_global.order_send(request)
            if result is None:
                log.error("PARTIAL_CLOSE order_send None ticket=%s: %s", pos.ticket, _mt5_global.last_error())
                continue
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                log.error("PARTIAL_CLOSE failed ticket=%s retcode=%s (%s)",
                          pos.ticket, result.retcode, result.comment)
                continue
            closed += 1
            log.warning("PARTIAL_CLOSE ticket=%s vol=%.2f", pos.ticket, close_volume)
        except Exception as exc:
            log.exception("PARTIAL_CLOSE position error ticket=%s: %s", pos.ticket, exc)
    return closed


async def _recheck_zone_pending(pool, mt5, redis_conn, dry_run: bool) -> None:
    """P1a: scan deferred zone signals; fill when price touches the zone.

    Redis TTL handles the timeout — keys auto-delete, no dead orders.
    """
    # 【E 组 P2-6 2026-08-03】deferred 成交路径补齐主循环同款闸门：
    # 原触达/超时兜底直接 _execute_signal，绕过 is_active/status 与信号新鲜度，
    # 账户停用期间 deferred 信号触达仍会下单。
    try:
        _live = redis_conn.get(f"account.{ACCOUNT_ID_MODE}.status")
        if _live and _live != "running":
            return
        _act = redis_conn.get(f"account.{ACCOUNT_ID_MODE}.is_active")
        if _act is None:
            _act = "true" if ACCOUNT_IS_ACTIVE else "false"
        if _act != "true":
            return
    except Exception:
        pass
    try:
        _zr_max_age = _resolve_max_signal_age_seconds(redis_conn)
    except Exception:
        _zr_max_age = DEFAULT_MAX_SIGNAL_AGE_SECONDS
    try:
        keys = redis_conn.keys("bridge:zone_pending:*")
    except Exception:
        return
    for key in keys:
        try:
            raw = redis_conn.get(key)
            if not raw:
                continue
            msg_data = json.loads(raw)
        except Exception:
            continue
        sid = int(msg_data.get("signal_id", 0))
        direction = msg_data.get("direction", "")
        if direction not in ("BUY", "SELL"):
            redis_conn.delete(key)
            continue
        # 【E 组 P2-6】deferred 成交前补信号新鲜度检查（原绕过 Safety Rail 1）
        if not _is_signal_fresh(msg_data, _zr_max_age, redis_conn):
            log.info("ZONE pending signal %s expired — dropped", sid)
            _audit_signal_stage(redis_conn, sid, "expired")
            redis_conn.delete(key)
            continue
        symbol = msg_data.get("symbol", "")
        zl = float(msg_data.get("zone_level", 0) or 0)
        if zl <= 0:
            redis_conn.delete(key)
            continue
        tick = _mt5_global.symbol_info_tick(real_symbol(symbol))
        if tick is None:
            continue
        if _price_in_zone_band(tick, direction, zl):
            # T3c ATR 波动过滤：尖刺扫单不成交（丢弃 deferred，不追单）
            tick_price = tick.ask if direction == "BUY" else tick.bid
            entry_price = float(msg_data.get("entry_price", 0) or 0)
            if await _atr_filter_blocks(pool, redis_conn, symbol,
                                        entry_price, tick_price, direction, 3.0):
                log.info("ZONE TOUCH signal %s: ATR filter blocked (spike) — dropped", sid)
                _audit_signal_stage(redis_conn, sid, "zone_dropped")
                redis_conn.delete(key)
                redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
                continue
            log.info("ZONE TOUCH signal %s: price at zone %.2f — filling",
                     sid, zl)
            await _execute_signal(pool, mt5, redis_conn, msg_data,
                                  symbol, "zone", dry_run)
            redis_conn.delete(key)
            redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
        else:
            # ── C 修复 (2026-07-23): 超时兜底市价追 ──
            # 原逻辑：不在 band 就一直等，Redis TTL 过期自动删→信号彻底作废
            #   （「有决策无成交」的并发根因之一）。现改为：TTL 将到期(<=30s)
            #   仍不在 band → 改市价追单，避免踏空。无死单：成交/拒单后均清
            #   除 deferred 键并标记 processed。
            _ttl = redis_conn.ttl(key)
            if _ttl is not None and _ttl != -1 and _ttl <= 30:
                _is_fb = bool(int(msg_data.get("co_exec_fb", 0) or 0))
                if _is_fb:
                    # 盲点兜底单：未触达 M5 结构位 → 直接丢弃，绝不市价追单
                    log.info(
                        "ZONE TIMEOUT (H1-fallback %s %s): not at zone %.2f (ttl=%s) — DROP (no chase)",
                        symbol, direction, zl, _ttl,
                    )
                    redis_conn.delete(key)
                    redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
                else:
                    log.info(
                        "ZONE TIMEOUT fallback signal %s: not at zone %.2f (ttl=%s) — MARKET fill",
                        sid, zl, _ttl,
                    )
                    await _execute_signal(pool, mt5, redis_conn, msg_data,
                                          symbol, "zone", dry_run)
                    redis_conn.delete(key)
                    redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
            # 否则继续等待；Redis TTL 到期自动删（无死单兜底）


async def _selfheal_config_from_pg(pool, redis_conn):
    """加固D1：启动自愈——hcm:config:v2 缺键时从 PG hcm_config.metadata 回填。

    背景：Redis 是易失热层（曾因容器重建/被清导致 hcm:config:v2 全空 → bridge 读不到配置）。
    PG hcm_config.metadata 才是唯一 SoT。启动时用 HSETNX 只补 Redis 缺失的键，
    绝不覆盖运行中的热值（正常情况下 Redis 已有全部键，此处 0 回填、纯保险）。
    """
    try:
        rows = await pool.fetch(
            "SELECT config_key, COALESCE(NULLIF(current_value, ''), default_value) AS val "
            "FROM hcm_config.metadata"
        )
    except Exception as exc:
        log.warning(f"Config self-heal: PG read failed, skip (Redis untouched): {exc}")
        return
    if not rows:
        log.warning("Config self-heal: PG metadata empty, skip")
        return
    try:
        existing = redis_conn.hlen("hcm:config:v2")
    except Exception:
        existing = -1
    filled = 0
    for r in rows:
        k = r["config_key"]
        v = r["val"]
        if k is None or v is None:
            continue
        try:
            if redis_conn.hsetnx("hcm:config:v2", k, str(v)):  # 只补缺键，不覆盖热值
                filled += 1
        except Exception as exc:
            log.warning(f"Config self-heal: HSETNX {k} failed: {exc}")
    if filled:
        log.warning(
            f"Config self-heal: Redis had {existing} keys, BACKFILLED {filled} missing "
            f"from PG ({len(rows)} total) — Redis was likely wiped/rebuilt"
        )
    else:
        log.info(f"Config self-heal: Redis config intact ({existing} keys), 0 backfill needed")


# ─────────────────────────────────────────────────────────────────────────────
# 动态发现（禁用硬编码）：连终端 → 读 account_info().login → 反查 PG account_number
#   设计：bridge 不再接收 --account-id 硬编码，而是连上终端后读取「MT5 实际登录的
#   账号」，再用该 login 反查 hcm_broker.accounts 得到 account_id / 密码 / 服务器。
#   效果：「在 MT5 里登哪个号，bridge 就服务哪个号」；换账户零代码改动。
#   单实例锁改为按 account_number(live login) 而非 account_id，更精准防重复下单。
# ─────────────────────────────────────────────────────────────────────────────

def _scan_first_terminal():
    """扫描本机运行的 terminal64.exe / terminal.exe，返回第一个真实路径；无则 None。"""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == -1:
            return None
        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.CHAR * 260),
            ]
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ret = kernel32.Process32First(snapshot, ctypes.byref(pe))
        found = []
        while ret:
            name = pe.szExeFile.decode("ascii", "ignore").lower()
            pid = pe.th32ProcessID
            if "terminal" in name:
                hProc = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                if hProc:
                    buf = ctypes.create_string_buffer(4096)
                    size = wintypes.DWORD(4096)
                    if kernel32.QueryFullProcessImageNameA(hProc, 0, buf, ctypes.byref(size)):
                        found.append(buf.value.decode("mbcs", "ignore"))
                    kernel32.CloseHandle(hProc)
            ret = kernel32.Process32Next(snapshot, ctypes.byref(pe))
        kernel32.CloseHandle(snapshot)
        return found[0] if found else None
    except Exception as exc:
        log.warning(f"_scan_first_terminal failed: {exc}")
        return None


def _discover_account(terminal_path):
    """连上指定终端（path only，复用终端 GUI 已登录会话），读实际登录账号。
    返回 (login:int, server:str, mt5_module) 或 None（终端未登录/初始化失败）。
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        log.critical("MetaTrader5 not installed. Run: pip install MetaTrader5")
        return None
    if not mt5.initialize(path=terminal_path):
        log.error(f"MT5 init failed for {terminal_path}: {mt5.last_error()}")
        return None
    # 填充时间框架映射（原在 connect_mt5 内；动态发现复用连接时也需填充）
    MT5_TIMEFRAME_MAP.update({
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
    })
    info = mt5.account_info()
    if not info:
        log.error(f"终端 {terminal_path} 未登录任何账号（account_info=None）——请先在 MT5 登录")
        try:
            mt5.shutdown()
        except Exception:
            pass
        return None
    server = getattr(info, "server", "") or ""
    # 检测经纪商时区偏移并挂到 mt5 对象（原在 connect_mt5 内设置；动态发现复用连接时也需设置）
    broker_utc_offset_hours = 0
    try:
        tick = mt5.symbol_info_tick("XAUUSD")
        if tick and tick.time:
            import time as _time_lib
            broker_utc_offset_hours = round((tick.time - int(_time_lib.time())) / 3600.0)
            log.info(f"Broker timezone offset: UTC{broker_utc_offset_hours:+d}h (detected from tick.time)")
    except Exception:
        log.warning("Cannot detect broker timezone, assuming UTC+0")
    mt5._broker_utc_offset_s = broker_utc_offset_hours * 3600
    return int(info.login), server, mt5


async def main(dry_run=False):
    import asyncpg
    import redis as redis_py
    global MT5_TRADE_OK  # 铁律缓存：main() 内多处刷新模块级变量

    pool = await asyncpg.create_pool(PG_DSN)
    redis_conn = redis_py.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_conn.ping()
    log.info("PG + Redis connected")

    # ── 模块级全局状态统一声明：确保 main 内赋值对 place_mt5_order/_execute_signal 等子函数可见 ──
    global ACCOUNT_ID_MODE, BRIDGE_GROUP, BRIDGE_LOCK_KEY
    global IS_MASTER, IS_FOLLOWER, FOLLOW_MASTERS, FOLLOW_LOT_MULT
    global BROKER_NAME, SYMBOL_MAP, ACCOUNT_IS_ACTIVE, ACCOUNT_STATUS_PG
    # 【2026-08-28 熔断恢复修复】_FOLLOW_BASELINE_LAST_DAY / _follow_circuit_last_check
    # 在 main 主循环内被赋值（行 4070 / 4077），若不在函数作用域顶层 global 声明，
    # Python 会将其视作局部变量 → 读取时抛 UnboundLocalError（实测 07:00 每日触发
    # "follower baseline record failed"），导致 bridge:follow:baseline:{account} 基准键
    # 永不写入 → 熔断分母回退当前 balance（当日亏损后变小）→ 百分比放大 → 误触发熔断。
    global _FOLLOW_BASELINE_LAST_DAY, _follow_circuit_last_check

    # ── 动态发现（禁用硬编码）：先连终端读实登账号，再反查 PG 得 account_id ──
    # 必须在抢锁/建组之前完成，因为锁键与消费组名都依赖发现到的 account_id / live login。
    terminal_path = TERMINAL_PATH_MODE or _scan_first_terminal() or MT5_PATH
    disc = _discover_account(terminal_path)
    if disc is None:
        log.error("动态发现失败：终端 %s 未登录任何账号或初始化失败，bridge 退出。", terminal_path)
        await pool.close()
        return
    live_login, live_server, disc_mt5 = disc

    # 每桥独立日志文件（按 login 命名），避免多桥共用 bridge.log 在 Windows 下并发写冲突
    try:
        _fh = logging.FileHandler(
            os.path.join(_BRIDGE_LOG_DIR, f"bridge_{live_login}.log"), encoding="utf-8"
        )
        _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(_fh)
    except Exception as exc:
        log.warning(f"无法为 bridge_{live_login} 建立独立日志文件: {exc}")

    acc = await pool.fetchrow(
        "SELECT account_id, account_number, password_enc, server_name, status, "
        "terminal_path, broker_name, account_type "
        "FROM hcm_broker.accounts WHERE account_number=$1",
        live_login,
    )
    if not acc:
        log.error(
            "动态发现：终端登录账号 %s 未在面板注册 (hcm_broker.accounts 无匹配 account_number)。"
            "请先在系统→MT5账户管理录入该账号及其密码；bridge 退出。",
            live_login,
        )
        try:
            disc_mt5.shutdown()
        except Exception:
            pass
        await pool.close()
        return

    # 设置全局（供后续单实例锁 / 消费组 / 信号过滤使用）——零硬编码账户/路径
    ACCOUNT_ID_MODE = acc["account_id"]
    BRIDGE_GROUP = f"group:{ACCOUNT_ID_MODE}"
    BRIDGE_LOCK_KEY = f"bridge:instance:lock:{live_login}"
    BRIDGE_ALIVE_KEY = f"bridge:alive:{live_login}"
    # ── 角色：从 MT5 用户列表(hcm_broker.accounts.account_type)读取，零硬编码 ──
    IS_MASTER = (acc.get("account_type") == "master")
    IS_FOLLOWER = (acc.get("account_type") == "follower")
    # 根治：is_active 不再参与启动发现（停用不掉桥），仅作为运行时实时下单闸门；
    # ACCOUNT_IS_ACTIVE 初始值取自 PG，后续由主循环每 5s 实时重载覆盖。
    ACCOUNT_IS_ACTIVE = bool(acc.get("is_active", True))
    ACCOUNT_STATUS_PG = acc.get("status") or "stopped"
    # 根治：桥启动即默认 running（保存即能用 / 切换账号不掉桥）。
    # 仅当用户经 UI 显式“停止/暂停”时才尊重该状态；Redis 键缺失视为未配置→置 running，
    # 避免“点一次停止后键残留 stopped、桥重启仍被卡死”的陷阱。
    try:
        _cur = redis_conn.get(f"account.{ACCOUNT_ID_MODE}.status")
        if _cur is None:
            redis_conn.set(f"account.{ACCOUNT_ID_MODE}.status", "running")
            log.info("账户 %s 启动自检：Redis status 缺失→默认置 running（即时可交易）", ACCOUNT_ID_MODE)
    except Exception as e:
        log.warning(f"账户 {ACCOUNT_ID_MODE} status 启动自检失败（非致命）：{e}")
    log.info("动态发现：终端 %s 登录账号 %s → account_id=%s server=%s status=%s account_type=%s "
             "(IS_MASTER=%s IS_FOLLOWER=%s)",
             terminal_path, live_login, ACCOUNT_ID_MODE, acc["server_name"], acc["status"],
             acc.get("account_type"), IS_MASTER, IS_FOLLOWER)

    # ── 跟单桥：读跟随关系，确定要复制哪些主号的自动信号（模式无关）──
    #    数据来自 hcm_copy.relationships（主→跟映射，仅 running），绝不硬编码 account_id。
    FOLLOW_MASTERS = set()
    FOLLOW_LOT_MULT = {}
    if IS_FOLLOWER:
        rels = await pool.fetch(
            "SELECT master_account_id, lot_multiplier "
            "FROM hcm_copy.relationships "
            "WHERE copy_account_id=$1 AND status='running'",
            ACCOUNT_ID_MODE,
        )
        for r in rels:
            m = int(r["master_account_id"])
            FOLLOW_MASTERS.add(m)
            FOLLOW_LOT_MULT[m] = float(r.get("lot_multiplier") or 1.0)
        log.info("跟单桥：将复制主号 account_ids=%s 的自动信号（lot_mult=%s）",
                 sorted(FOLLOW_MASTERS), FOLLOW_LOT_MULT)
    else:
        log.info("主号/独立桥：仅执行本账户信号（account_type=%s，无跟单复制）",
                 acc.get("account_type"))
    if live_server and acc["server_name"] and live_server != acc["server_name"]:
        log.warning("⚠️ 终端登录服务器 %s 与 PG 登记 %s 不一致，下单以 PG 登记为准",
                    live_server, acc["server_name"])

    # 加载跨券商品种名映射（数据驱动，零硬编码；复用 hcm_copy.symbol_mappings）
    global BROKER_NAME, SYMBOL_MAP
    BROKER_NAME = acc.get("broker_name") or None
    SYMBOL_MAP = await load_symbol_map(pool, BROKER_NAME)
    if SYMBOL_MAP:
        log.info("已加载品种映射(broker=%s): %s", BROKER_NAME, SYMBOL_MAP)
    else:
        log.info("无品种名映射(broker=%s)，逻辑名直连 MT5", BROKER_NAME)

    # 用本券商实盘名重检时区偏移（_discover_account 用逻辑名 XAUUSD 检测，
    # 若实盘名不同会导致检测失败/偏移=0；此处用翻译后的实盘名修正）
    try:
        _real = real_symbol(SYMBOL)
        _tick = disc_mt5.symbol_info_tick(_real)
        if _tick and _tick.time:
            import time as _time_lib
            _off_h = round((_tick.time - int(_time_lib.time())) / 3600.0)
            disc_mt5._broker_utc_offset_s = _off_h * 3600
            log.info("时区偏移重检(实盘名 %s): UTC%+dh", _real, _off_h)
    except Exception as e:
        log.warning("时区偏移重检失败，沿用 discovery 值: %s", e)

    # ── 加固C：单实例互斥锁——占不到锁即拒绝启动，杜绝多 bridge 并存导致重复下单。
    # 根治「换账户/重启 start.bat 死桥」：锁冲突不再硬退出互踢。
    #   持锁者是【存活】进程 → 本实例干净退出（该终端已有存活桥，看门狗不会重复拉起 → 无抖动）；
    #   持锁者【已死】(崩溃/强杀但 TTL 未到期) → 安全接管(steal)，不退出、不抖动；
    #   无锁 → 正常抢占。
    my_lock_id = str(os.getpid())
    got_lock = redis_conn.set(BRIDGE_LOCK_KEY, my_lock_id, nx=True, ex=BRIDGE_LOCK_TTL)
    if not got_lock:
        holder = redis_conn.get(BRIDGE_LOCK_KEY)
        if holder == my_lock_id:
            # 同一进程内 run_bridge_forever 重试——是我们自己的锁，刷新 TTL 继续
            redis_conn.expire(BRIDGE_LOCK_KEY, BRIDGE_LOCK_TTL)
            log.info(f"Re-acquired own single-instance lock (PID {my_lock_id})")
        elif holder and _pid_alive(holder):
            # 真·重复实例（另一存活进程持锁）→ 干净退出，避免重复下单；
            # 该终端已有存活桥=持锁进程，看门狗不会重复拉起本实例，故无抖动断桥。
            log.critical(
                f"Another LIVE bridge holds {BRIDGE_LOCK_KEY} (holder PID={holder}). "
                f"Refusing to start to avoid DUPLICATE ORDERS. This instance (PID={my_lock_id}) exits."
            )
            await pool.close()
            try:
                disc_mt5.shutdown()
            except Exception:
                pass
            raise InstanceLockedError(holder)
        else:
            # 持锁者已死（崩溃/强杀但锁 TTL 未到期）→ 安全接管(steal)，不退出、不抖动
            log.warning(
                f"Stale lock detected for {BRIDGE_LOCK_KEY} (holder PID={holder} dead) "
                f"— safely taking over (PID {my_lock_id})"
            )
            redis_conn.set(BRIDGE_LOCK_KEY, my_lock_id, xx=True, ex=BRIDGE_LOCK_TTL)
            try:
                redis_conn.set(f"bridge:terminal_for:{live_login}", terminal_path, ex=BRIDGE_LOCK_TTL + 5)
            except Exception:
                pass
    else:
        log.info(f"Acquired single-instance lock {BRIDGE_LOCK_KEY}=PID {my_lock_id} (TTL {BRIDGE_LOCK_TTL}s)")
        # 看门狗据此判断「该终端已有 bridge 在跑」，避免重复拉起（不依赖脆弱的进程命令行解析）
        try:
            redis_conn.set(f"bridge:terminal_for:{live_login}", terminal_path, ex=BRIDGE_LOCK_TTL + 5)
        except Exception as exc:
            log.warning(f"set bridge:terminal_for failed: {exc}")

    # ── 加固D1：配置启动自愈——hcm:config:v2 缺键时从 PG hcm_config.metadata 回填 ──
    # 用 HSETNX 语义：只补缺失键，绝不覆盖运行中的热值。防 Redis 容器重建/被清后配置丢失。
    await _selfheal_config_from_pg(pool, redis_conn)

    # ── 消费者组初始化：启动时确定性丢弃全部积压（实时优先） ──
    # 逻辑缺陷修复：原逻辑仅在「首次创建」时用 start_id="$"，重启不重置 →
    # 重启后 XREADGROUP ">" 从旧 last-delivered-id 重放整段积压，FIFO 把新鲜信号
    # 堵在队尾（曾 7192 条≈24min 才排到队尾）。现改为：确保组存在 → 删除历史消费者
    # （修复 PEL/消费者泄漏）→ 重置到最新($)，只处理启动后到达的信号。
    try:
        redis_conn.xgroup_create("signal:risk_passed", BRIDGE_GROUP,
                                 mkstream=True, id="$")
        log.info(f"Created consumer group {BRIDGE_GROUP} (mkstream, id=$)")
    except Exception as exc:
        if "BUSYGROUP" in str(exc).upper():
            pass  # group already exists — normal, idempotent
        else:
            # 加固A：不再静默吞错。建组真失败要可见（曾因 start_id= 参数名错被 pass 吞掉 → 永久 NOGROUP）
            log.exception(f"xgroup_create failed unexpectedly (NOT BUSYGROUP): {exc}")

    cur_consumer = f"bridge-consumer-{os.getpid()}"
    try:
        consumers = redis_conn.xinfo_consumers("signal:risk_passed", BRIDGE_GROUP)
        for c in consumers:
            if c.get("name") != cur_consumer:
                try:
                    redis_conn.xgroup_delconsumer(
                        "signal:risk_passed", BRIDGE_GROUP, c["name"]
                    )
                    log.info(f"Removed stale consumer: {c['name']}")
                except Exception as e:
                    log.warning(f"Failed to remove consumer {c.get('name')}: {e}")
    except Exception as e:
        log.warning(f"xinfo_consumers failed: {e}")

    # 重置到最新：只处理启动后到达的信号，确定性丢弃全部历史积压
    try:
        backlog = redis_conn.xlen("signal:risk_passed")
        redis_conn.xgroup_setid("signal:risk_passed", BRIDGE_GROUP, "$")
        log.info(
            f"Startup: discarded {backlog} backlogged signal(s); "
            f"only real-time signals arriving after now will be ordered"
        )
    except Exception as e:
        log.warning(f"xgroup_setid failed: {e}")

    # [2026-08-25] 跟单桥消费组：hcm:master:sltp（主号桥发布的毫秒级 SL/TP 事件流）。
    # 与 signal:risk_passed 同名 group:{id} 跨流互不干扰；启动时确定性丢弃历史积压
    # （SL/TP 瞬态，陈旧事件不应再驱动跟单改单）。
    if IS_FOLLOWER:
        try:
            redis_conn.xgroup_create("hcm:master:sltp", BRIDGE_GROUP, mkstream=True, id="$")
            log.info(f"Created sltp consumer group {BRIDGE_GROUP} (mkstream, id=$)")
        except Exception as exc:
            if "BUSYGROUP" not in str(exc).upper():
                log.exception(f"sltp xgroup_create failed (NOT BUSYGROUP): {exc}")
        try:
            redis_conn.xgroup_setid("hcm:master:sltp", BRIDGE_GROUP, "$")
            log.info("Startup: sltp group reset to latest ($), discarded backlog")
        except Exception as exc:
            log.warning(f"sltp xgroup_setid failed: {exc}")

    mt5_cfg = _load_mt5_config(redis_conn)

    # 用 PG 凭证覆盖（密码铁律：以 MT5 登录时输入的经纪商密码为准，不从 legacy/猜测复制）
    mt5_cfg["login"] = acc["account_number"]
    mt5_cfg["server"] = acc["server_name"]
    mt5_cfg["password"] = acc["password_enc"]
    mt5_cfg["terminal_path"] = acc["terminal_path"]  # 仅作展示/冗余；实际连接用 discovery 的 terminal_path

    if acc["status"] != "running":
        log.warning(
            "Account %s status=%s (not running) — bridge started but will skip all signals "
            "until status flips to running (live, no restart needed).",
            ACCOUNT_ID_MODE, acc["status"],
        )

    if mt5_cfg['login'] == 0:
        log.error("MT5 account_number not configured (got 0), refusing to connect. "
                   "Please configure MT5 credentials in system/mt5 page.")
        await pool.close()
        try:
            disc_mt5.shutdown()
        except Exception:
            pass
        return

    log.info(
        "MT5 config: login=%s, server=%s, timeframe=%s, symbols=%s, max_bars=%s",
        mt5_cfg['login'], mt5_cfg['server'],
        mt5_cfg['timeframe'], mt5_cfg['symbols'], mt5_cfg['max_bars'],
    )

    timeframe_str = mt5_cfg['timeframe']       # primary
    all_timeframes = mt5_cfg.get('timeframes', [timeframe_str])  # P2-7
    symbols = mt5_cfg['symbols']
    primary_symbol = symbols[0]
    max_bars = mt5_cfg['max_bars']

    # 动态发现模式下 MT5 已在 discovery 阶段初始化并复用终端 GUI 已登录会话，直接复用连接
    mt5 = disc_mt5
    ai = mt5.account_info()
    log.info("MT5 connected (reused from discovery) login=%s trade_allowed=%s",
             live_login, (ai.trade_allowed if ai else '?'))
    # 启动期初始化铁律缓存：discovery 阶段已确认账号登录，直接据此置位（不默认 True），
    # 避免启动 30s 内健康检查未跑、缓存误判可用。
    MT5_TRADE_OK = bool(ai) and bool(getattr(ai, "trade_allowed", False))

    # Load initial klines for primary symbol — all timeframes (P2-7)
    for tf in all_timeframes:
        rates = []
        for sym in symbols:
            try:
                r = fetch_klines(mt5, sym, tf, max_bars)
                if len(r) > 0:
                    rates = r
                    log.info(f"Found klines for {sym}: {len(r)} bars ({tf})")
                    break
            except Exception as e:
                log.warning(f"Cannot fetch {sym} ({tf}): {e}")
        if len(rates) > 0:
            await write_klines_to_pg(pool, rates, primary_symbol, tf, mt5._broker_utc_offset_s)
            log.info(f"Initial K-lines loaded: {len(rates)} ({primary_symbol} {tf})")
        else:
            log.warning(f"No K-line data for {tf} — will retry on next cycle")

    last_kline = 0
    last_tick = 0
    last_risk = time.time()
    last_trail = time.time()
    last_sync = time.time()   # P0: position_sync 独立 ~1s 高频轮询（原绑在 5s trailing 门控，导致手动镜像检测延迟 ~6s）
    last_reconcile = 0.0      # 跟单桥持仓对账兜底计时（初始=0 → 启动后立即触发一次，秒级自愈孤儿单）
    last_direct_close = 0.0   # [2026-07-24] 直连快速通道：每 2s 消费 hcm:direct_close:* 平仓指令
    last_lock_hb = time.time()   # 加固C：单实例锁心跳续期计时
    last_cfg_reload = time.time()   # 根治：运行中实时重载账户配置 + 检测切换账号计时
    _follow_circuit_last_check = 0.0  # 跟单号每日盈亏熔断检查计时
    # P1a zone-trigger deferred recheck
    zone_trigger_on = False
    last_zone_cfg_check = 0
    last_zone_recheck = 0
    last_backup_check = 0
    mode = "[DRY RUN]" if dry_run else ""

    log.info(f"Bridge running {mode} — Ctrl+C to stop")
    bridge_has_lock = True  # 单实例锁持有标志：standby(重复实例存活)时为 False，闸门跳过下单
    while True:
        now = time.time()
        try:
            # 全备份触发监听（任意实例均可；仅 Redis delete 赢家真正执行，多桥不双备份）
            if now - last_backup_check > 3:
                last_backup_check = now
                _maybe_run_backup(redis_conn)

            # 加固C：单实例锁心跳——每 ~10s 续期。根治「换账户/重启 start.bat 死桥」：
            #   锁被【存活】进程持有 → 本实例进入 standby（跳过本期下单、不退出、不抖动），
            #     持锁者死亡后下一轮自动接管(steal)，杜绝两桥互踢造成的抖动/断桥；
            #   锁被【已死】持锁者占用 或 无锁 → 安全接管(steal) 并续期，不退出。
            if now - last_lock_hb > BRIDGE_LOCK_RENEW_INTERVAL:
                try:
                    _cur = redis_conn.get(BRIDGE_LOCK_KEY)
                    if _cur == my_lock_id:
                        # 我是持锁权威桥 → 续期锁 + 存活心跳 + 处理自愈控制信令
                        bridge_has_lock = True
                        redis_conn.expire(BRIDGE_LOCK_KEY, BRIDGE_LOCK_TTL)
                        try:
                            redis_conn.set(f"bridge:terminal_for:{live_login}", terminal_path, ex=BRIDGE_LOCK_TTL + 5)
                        except Exception:
                            pass
                        if _publish_bridge_heartbeat(
                            redis_conn, BRIDGE_ALIVE_KEY, BRIDGE_ALIVE_TTL,
                            my_lock_id, live_login, ACCOUNT_ID_MODE,
                            IS_MASTER, IS_FOLLOWER, acc, terminal_path):
                            log.warning("收到自愈重启指令 — 主动退出，看门狗将重拉")
                            break
                    elif _cur and _pid_alive(_cur):
                        # 真·重复实例(存活)持锁 → standby：跳过本期下单，避免重复单；
                        # 不退出、不抖动，持锁者死亡后自动接管。
                        bridge_has_lock = False
                        log.info(f"Lock held by LIVE PID {_cur} — standby (no orders), will retry")
                        last_lock_hb = now
                        await asyncio.sleep(5)
                        continue
                    else:
                        # 无锁 或 持锁者已死 → 安全接管(steal，不退出、不抖动)
                        bridge_has_lock = True
                        if _cur:
                            log.warning(
                                f"Stale lock stolen from dead holder PID={_cur} — taking over (PID={my_lock_id})")
                        else:
                            log.info(f"Lock vacant — acquiring {BRIDGE_LOCK_KEY} (PID {my_lock_id})")
                        try:
                            redis_conn.set(
                                BRIDGE_LOCK_KEY, my_lock_id,
                                nx=(_cur is None), xx=(_cur is not None), ex=BRIDGE_LOCK_TTL)
                            redis_conn.set(f"bridge:terminal_for:{live_login}", terminal_path, ex=BRIDGE_LOCK_TTL + 5)
                        except Exception:
                            pass
                        if _publish_bridge_heartbeat(
                            redis_conn, BRIDGE_ALIVE_KEY, BRIDGE_ALIVE_TTL,
                            my_lock_id, live_login, ACCOUNT_ID_MODE,
                            IS_MASTER, IS_FOLLOWER, acc, terminal_path):
                            log.warning("收到自愈重启指令 — 主动退出，看门狗将重拉")
                            break
                except Exception as e:
                    log.warning(f"Lock heartbeat failed (continuing): {e}")
                last_lock_hb = now

            # standby 闸门：未持锁(重复实例存活)时，跳过本轮全部下单/追利/平仓，杜绝重复单；
            # 持锁者死亡后下一轮锁续期块会 steal 并把 bridge_has_lock 置 True，自动恢复交易。
            if not bridge_has_lock:
                continue

            # ── 根治：运行中实时重载账户配置 + 检测切换账号（停用/启用/角色/跟单关系 保存即生效；切账号不掉桥）──
            if now - last_cfg_reload > 5:
                # 1) 检测终端登录账号是否切换（同一终端换号登录）
                try:
                    _ai_now = mt5.account_info()
                    _cur_login = int(_ai_now.login) if _ai_now else None
                except Exception:
                    _cur_login = None
                if _cur_login is not None and _cur_login != live_login:
                    log.warning(
                        "终端登录账号已切换 %s → %s，触发桥热重初始化（同进程重新发现，不掉桥）",
                        live_login, _cur_login,
                    )
                    break  # run_bridge_forever 会重新 main() -> 重新发现并抢新账号锁/消费组
                # 2) 实时重载账户行（is_active/status/account_type）+ 跟单关系
                if _cur_login is not None:
                    try:
                        _row = await pool.fetchrow(
                            "SELECT account_id, account_type, status, is_active "
                            "FROM hcm_broker.accounts WHERE account_number=$1",
                            live_login,
                        )
                        if _row:
                            ACCOUNT_ID_MODE = int(_row["account_id"])
                            IS_MASTER = (_row["account_type"] == "master")
                            IS_FOLLOWER = (_row["account_type"] == "follower")
                            ACCOUNT_IS_ACTIVE = bool(_row["is_active"])
                            ACCOUNT_STATUS_PG = _row["status"] or "stopped"
                            BRIDGE_GROUP = f"group:{ACCOUNT_ID_MODE}"
                            BRIDGE_ALIVE_KEY = f"bridge:alive:{live_login}"
                            if IS_FOLLOWER:
                                _rels = await pool.fetch(
                                    "SELECT master_account_id, lot_multiplier "
                                    "FROM hcm_copy.relationships "
                                    "WHERE copy_account_id=$1 AND status='running'",
                                    ACCOUNT_ID_MODE,
                                )
                                FOLLOW_MASTERS = {int(r["master_account_id"]) for r in _rels}
                                FOLLOW_LOT_MULT = {
                                    int(r["master_account_id"]): float(r.get("lot_multiplier") or 1.0)
                                    for r in _rels
                                }
                            log.debug(
                                "Live config reload: account_id=%s is_active=%s status=%s "
                                "IS_MASTER=%s IS_FOLLOWER=%s follow_masters=%s",
                                ACCOUNT_ID_MODE, ACCOUNT_IS_ACTIVE, ACCOUNT_STATUS_PG,
                                IS_MASTER, IS_FOLLOWER, sorted(FOLLOW_MASTERS),
                            )
                    except Exception as e:
                        log.warning(f"Live config reload failed (will retry): {e}")
                last_cfg_reload = now

            # Real-time tick push every 2s (primary symbol) — 全周期
            if now - last_tick > 2:
                try:
                    # 主周期：实时 tick 价 + 主周期实时棒 + ATR
                    await asyncio.wait_for(
                        write_price_to_redis(redis_conn, mt5, primary_symbol, timeframe_str, mt5._broker_utc_offset_s),
                        timeout=5.0
                    )
                    # 各高周期(H1/H4/D1...)实时棒：原仅写主周期 M5 的 latest_kline，
                    # 导致 hexp 多周期共振矩阵(H1/H4/D1)只能读陈旧的 PG 棒 → 行情源断开。
                    # 现对每个周期都写 latest_kline:{symbol}:{tf}（write_tick=False 跳过冗余
                    # tick/ATR），供 scheduler 实时合并，保持高周期实时行情数据。
                    for tf in all_timeframes:
                        if tf == timeframe_str:
                            continue
                        await asyncio.wait_for(
                            write_price_to_redis(redis_conn, mt5, primary_symbol, tf, mt5._broker_utc_offset_s, write_tick=False),
                            timeout=5.0,
                        )
                    # ── Push live bar to Redis (Fix #4) — 全周期 ──
                    for tf in all_timeframes:
                        await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(
                                None, push_live_bar_to_redis, redis_conn, mt5,
                                primary_symbol, tf, mt5._broker_utc_offset_s,
                            ),
                            timeout=3.0,
                        )
                except asyncio.TimeoutError:
                    log.warning("write_price_to_redis timed out after 5s — skipping this tick")
                last_tick = now

            # MT5 connection health check every 30s
            if not hasattr(main, '_last_health_check'):
                main._last_health_check = 0  # type: ignore[attr-defined]
            if now - main._last_health_check > 30:  # type: ignore[attr-defined]
                try:
                    info = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, mt5.terminal_info),
                        timeout=3.0
                    )
                    if not info:
                        log.error("MT5 terminal disconnected — triggering full reconnect")
                        raise ConnectionError("MT5 terminal_info() returned None")
                    # 铁律缓存刷新：终端连着 ≠ 账号可交易。额外查 account_info() 确认登录态与
                    # trade_allowed，合并写模块级 MT5_TRADE_OK（供 mt5_trade_ready() 无 IPC 读取）。
                    try:
                        _ai = mt5.account_info()
                    except Exception as _aie:
                        log.warning("health-check account_info() raised: %s", _aie)
                        _ai = None
                    MT5_TRADE_OK = bool(_ai) and bool(getattr(_ai, "trade_allowed", False))
                    if not MT5_TRADE_OK:
                        log.error("MT5 account NOT trade-ready (account_info=%s) — trade-ready guard ACTIVE",
                                  ('None' if _ai is None else f"trade_allowed={_ai.trade_allowed}"))
                except asyncio.TimeoutError:
                    log.warning("MT5 terminal_info() timeout — skipping health check")
                main._last_health_check = now  # type: ignore[attr-defined]

            # New K-line detection every 5s (all symbols × all timeframes) — P2-7
            # 取最近 3 根：MT5 在周期边界的 copy_rates_from_pos(...,0,2) 只返回
            # [当前棒(未收盘), 下一根刚生成的棒]，取不到已收盘的上一根。
            # 故取 3 根并整批交给 write_klines_to_pg，由其内部“未收盘棒防御”
            # 跳过当前/未来棒，仅持久化已收盘棒（ON CONFLICT 合并，不丢不重）。
            if now - last_kline > 5:
                for tf in all_timeframes:
                    for sym in symbols:
                        rates = fetch_klines(mt5, sym, tf, 3)
                        if rates is not None and len(rates) >= 2:
                            latest = rates[-1]
                            bar_time = latest['time']
                            bar_key = f"_last_bar_time:{sym}:{tf}"
                            last_bar = getattr(main, bar_key, 0)
                            if bar_time != last_bar:
                                # 新棒出现（上一根已收盘 / 进入新的形成棒）→ 整批写入
                                await write_klines_to_pg(pool, rates, sym, tf, mt5._broker_utc_offset_s)
                                setattr(main, bar_key, bar_time)
                                log.info(
                                    f"Bar synced: {sym} {tf} (latest="
                                    f"{datetime.fromtimestamp(bar_time, tz=timezone.utc)})"
                                )
                            else:
                                # 同一根形成棒：节流更新（每 60s），保持 H1/H4/D1
                                # 实时行情数据，供 hexp 多周期共振矩阵使用当前形成棒。
                                fr_key = f"_last_forming_write:{sym}:{tf}"
                                if now - getattr(main, fr_key, 0) >= 60:
                                    await write_klines_to_pg(pool, rates, sym, tf, mt5._broker_utc_offset_s)
                                    setattr(main, fr_key, now)
                last_kline = now

            # Check for risk-passed signals to execute orders (consumer group — new only)
            if now - last_risk > 2:
                # ── MT5 实时可用性闸门（2026-08-28 修复）──
                # 根因：消费组 group:<account_id> 仅标识"本桥在消费"，不保证 MT5 账号已登录/
                # 可交易。若终端连着但账号登出(trade_allowed=False)或 account_info() 返回 None
                # （意外断开），原逻辑仍 XREADGROUP 并 XACK 消费 → place_mt5_order 时
                # symbol_info_tick=None 下单失败 → 信号被 ACK 永久丢失（"group 活着但 MT5 停了，
                # 信号照吃不报"）。
                # 修复：消费前先查 MT5 实时可用性，不可用则【根本不调用 XREADGROUP】，信号留在
                # 流里（未被任何消费者读、未进 PEL）→ 账户恢复后下一轮正常消费执行，杜绝静默丢失。
                # 用 XREADGROUP ">" 模式，任何被读的消息即进 PEL，故"不读"才是最安全的保留方式。
                # 用 mt5_trade_ready(force_check=True) 现查并刷新模块级 MT5_TRADE_OK 缓存（供
                # place_mt5_order / _force_close_positions 铁律兜底读取，零额外 IPC）。
                _ai = mt5.account_info() if mt5 else None
                _trade_ok = bool(_ai) and bool(getattr(_ai, "trade_allowed", False))
                MT5_TRADE_OK = _trade_ok
                if not _trade_ok:
                    if now - getattr(main, "_mt5_dead_log_ts", 0) > 30:
                        log.critical(
                            "MT5 account %s UNAVAILABLE (account_info=%s, trade_allowed=%s) — "
                            "SKIP signal consumption; signals retained in stream until MT5 recovers",
                            ACCOUNT_ID_MODE,
                            _ai is not None,
                            getattr(_ai, "trade_allowed", False) if _ai else False,
                        )
                        main._mt5_dead_log_ts = now  # type: ignore[attr-defined]
                    last_risk = now  # 仍按节奏节流，避免空转过快
                    continue
                try:
                    msgs = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: redis_conn.xreadgroup(
                                groupname=BRIDGE_GROUP,
                                consumername=f"bridge-consumer-{os.getpid()}",
                                streams={"signal:risk_passed": ">"},
                                count=10,
                                # B-4 优化：跟单号收紧 block 到 200ms（降链接延迟①），主号维持 1000ms
                                # 不影响风控实时性（主号下单延迟容忍度高）。仅 IS_FOLLOWER 生效。
                                block=200 if IS_FOLLOWER else 1000,
                            )
                        ),
                        timeout=3.0
                    )
                except asyncio.TimeoutError:
                    log.debug("xreadgroup timeout (no new messages)")
                    msgs = None
                except Exception as exc:
                    # 加固A：NOGROUP 自愈——若消费者组/流被删（DEL signal:risk_passed 会连组一起删），
                    # 自动重建组并从最新($)消费，下一轮即恢复下单，杜绝"悄悄永久不下单"。
                    if "NOGROUP" in str(exc).upper():
                        log.error(f"NOGROUP detected — consumer group/stream missing, rebuilding: {exc}")
                        try:
                            redis_conn.xgroup_create("signal:risk_passed", BRIDGE_GROUP,
                                                     mkstream=True, id="$")
                            log.error("Rebuilt bridge-order-group (mkstream, id=$) — will resume next cycle")
                        except Exception as ce:
                            if "BUSYGROUP" not in str(ce).upper():
                                log.exception(f"Failed to rebuild consumer group after NOGROUP: {ce}")
                    else:
                        log.warning(f"xreadgroup error: {exc}")
                    msgs = None
                # 过期阈值（D7-2: 经 _resolve_max_signal_age_seconds 强制安全下限，缺失/越界安全兜底）
                max_signal_age = _resolve_max_signal_age_seconds(redis_conn)
                if msgs:
                    total_msgs = sum(len(entries) for _stream_name, entries in msgs)
                    log.info(f"XREADGROUP returned {total_msgs} messages")
                    for _stream_name, entries in msgs:
                        if not entries:
                            continue
                        # ── latest-wins：只认本批次最新一条，其余直接丢弃（不执行） ──
                        entries_sorted = sorted(
                            entries, key=lambda m: _stream_id_prefix(m[0]), reverse=True
                        )
                        latest_id, latest_data = entries_sorted[0]

                        # ── per-account 过滤 + 实时启停（广播模型核心）──
                        # 广播模型下每组都收到全部信号副本，仅处理目标为本账号者；其余直接
                        # ack 丢弃，且绝不写 bridge:processed 标记（该键全局共享，误写会误杀
                        # 其它账号的待处理信号）。
                        if ACCOUNT_ID_MODE:
                            sig_account = int(latest_data.get("account_id", 0) or 0)
                            sig_mode = str(latest_data.get("signal_mode", ""))
                            # 手动模式镜像护栏：镜像信号仅供 copy-trading 扇出给跟单号，
                            # 主号桥不得重复执行（否则主号双重下单）
                            if sig_mode == "manual_mirror":
                                # 主号桥：sig_account=自己（主号）→ 跳过
                                # 跟单号桥：sig_account=主号 != 自己 → 落到主路径，下单到本桥 MT5
                                if sig_account == ACCOUNT_ID_MODE:
                                    for _dmid, _dmsg in entries_sorted:
                                        redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, _dmid)
                                    continue
                                log.info(
                                    "Follower bridge: executing manual_mirror signal %s from account_id=%s → this account=%s",
                                    int(latest_data.get("signal_id", 0)),
                                    sig_account, ACCOUNT_ID_MODE,
                                )
                            elif sig_account != ACCOUNT_ID_MODE:
                                # 自动信号（co_source / live_override 等，模式无关）：
                                #   · 主号桥：仅执行 account_id==本桥 的信号（自动信号 account_id 恒为主号）。
                                #   · 跟单桥【阶段1 去跟单化 2026-08-24】：对来自 FOLLOW_MASTERS 主号的信号
                                #     直接独立执行（不复制主号成交、不等主号建仓）。信号已广播，跟单号
                                #     与主号同时收到同一份 risk_passed，走同一链路独立下单。
                                if IS_FOLLOWER:
                                    if sig_account not in FOLLOW_MASTERS:
                                        for _dmid, _dmsg in entries_sorted:
                                            redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, _dmid)
                                        continue
                                    # 【阶段1 去跟单化 2026-08-24】跟单号改为「独立下单」，不再复制主号成交：
                                    # 移除 A-护栏（主号真实持仓交叉校验 hcm:master:positions / PG 二次确认 /
                                    # pending DEFER）——这些让跟单号被迫"等主号先建仓"再复制，是
                                    # "跟单号跟不上主号"的结构性延迟源。信号本身已广播，跟单号与主号
                                    # 同时收到同一份 risk_passed，直接独立执行（手数按账号倍率缩放）。
                                    # 共享信号级风控（主号被拒则全部不开），滑点/仓位由各账号自身闸门兜底。
                                    log.info(
                                        "Follower bridge: independent execution of auto signal %s (mode=%s) from master account_id=%s",
                                        int(latest_data.get("signal_id", 0)), sig_mode, sig_account,
                                    )
                                else:
                                    for _dmid, _dmsg in entries_sorted:
                                        redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, _dmid)
                                    continue
                            # ── 兜底护栏（防止 risk-engine 丢 signal_mode 字段）：
                            #    signal_tower.mode=manual 且非 manual_mirror 标记的信号
                            #    若 account_id==自己且 signal_id 是 MT5 ticket（9-10 位整数），
                            #    说明是镜像信号被 risk-engine 丢字段后误传入 → 直接跳过
                            #    （跟单号桥不会到这里，因为 manual_mirror 分支已处理）──
                            if sig_mode != "manual_mirror":
                                try:
                                    _is_manual = redis_conn.hget("hcm:config:v2", "signal_tower.mode")
                                except Exception:
                                    _is_manual = None
                                _sig_sid = int(latest_data.get("signal_id", 0) or 0)
                                # 跟单桥在手动模式下仍复制主号自动信号（用户要求“无论信号模式跟单号全复制主号”）
                                if (not IS_FOLLOWER) and _is_manual and str(_is_manual).strip().lower() == "manual":
                                    for _dmid, _dmsg in entries_sorted:
                                        redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, _dmid)
                                    log.info(
                                        "Manual mode guard: skipped signal %s (master/standalone bridge, manual active)",
                                        _sig_sid,
                                    )
                                    continue
                                # 注：原先此处有“MT5-ticket 护栏”(100M<=sid<=9.99B 时主桥跳过自身执行)。
                                # 但新信号 signal_id 现已落入该区间(实测 ~4.77 亿)，导致主桥把所有自动信号
                                # 当镜像跳过、只有跟单桥复制 → “主无单/跟有单”(bug)。镜像信号已在上方
                                # sig_mode=="manual_mirror" 分支按 sig_account==本桥 跳过，此护栏对自动信号
                                # 纯属误杀，故移除。主桥恢复正常执行自身自动信号。
                            # 账户启停实时控制：非 running 跳过（凭证保留，无需重启 bridge）
                            _live = redis_conn.get(f"account.{ACCOUNT_ID_MODE}.status")
                            # 注意：Redis 连接已设 decode_responses=True，get 返回 str（非 bytes），不可再 .decode()
                            if _live and _live != "running":
                                log.info(
                                    "Account %s status=%s (not running) — skipping signal %s",
                                    ACCOUNT_ID_MODE, _live,
                                    int(latest_data.get("signal_id", 0)),
                                )
                                for _dmid, _dmsg in entries_sorted:
                                    redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, _dmid)
                                continue

                            # 跟单号每日盈亏熔断：当日暂停跟单（主号不受影响），丢弃并 ack 信号
                            if IS_FOLLOWER and FOLLOW_CIRCUIT_BROKEN:
                                log.info(
                                    "Follower circuit-break active — skipping signal %s "
                                    "(daily PnL limit hit, follower paused)",
                                    int(latest_data.get("signal_id", 0)),
                                )
                                for _dmid, _dmsg in entries_sorted:
                                    redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, _dmid)
                                continue

                            # 根治：is_active=false（UI 停用）只跳过下单，桥进程保持存活，重新启用即时生效（保存即能用）
                        # 优先读 Redis 即时广播（停用秒级生效），PG 每 5s 重载的 ACCOUNT_IS_ACTIVE 作为兜底
                        _act = redis_conn.get(f"account.{ACCOUNT_ID_MODE}.is_active")
                        if _act is None:
                            _act = "true" if ACCOUNT_IS_ACTIVE else "false"
                        if _act != "true":
                            log.info(
                                "Account %s is_active=%s (disabled) — skipping signal %s "
                                "(bridge alive; re-enable to resume)",
                                ACCOUNT_ID_MODE, _act, int(latest_data.get("signal_id", 0)),
                            )
                            for _dmid, _dmsg in entries_sorted:
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, _dmid)
                            continue

                        for dmid, dmsg in entries_sorted[1:]:
                            dsid = int(dmsg.get("signal_id", 0))
                            log.info(
                                f"DROP non-latest signal {dsid} (batch latest="
                                f"{int(latest_data.get('signal_id', 0))}) — latest-wins"
                            )
                            redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{dsid}", "done", ex=2592000)
                            redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, dmid)
                        # 跨批次乱序/重复守卫：比已执行最新信号更旧 → 跳过（防补刀）
                        latest_prefix = _stream_id_prefix(latest_id)
                        last_prefix = getattr(main, "_last_exec_prefix", 0)
                        if latest_prefix <= last_prefix:
                            lsid = int(latest_data.get("signal_id", 0))
                            log.info(
                                f"SKIP older/duplicate signal {lsid} (prefix {latest_prefix} "
                                f"<= last {last_prefix}) — latest-wins guard"
                            )
                            redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{lsid}", "done", ex=2592000)
                            redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, latest_id)
                            continue
                        main._last_exec_prefix = latest_prefix  # type: ignore
                        # 把最新信号当作当前处理条目，复用原 inner-loop 处理链（仅迭代单条=latest）
                        for msg_id, msg_data in [(latest_id, latest_data)]:
                            sid = int(msg_data.get("signal_id", 0))
                            direction = msg_data.get('direction')
                            # ── Manual mirror CLOSE：提升到 dedup 前执行（开仓已使 processed 标记覆盖同 sid）──
                            if direction == "CLOSE":
                                msg_symbol = msg_data.get("symbol", primary_symbol)
                                # 2026-07-24 修复：精确按票平仓，绝不按 symbol 平掉跟单号同品种全部持仓。
                                # 主号被平 ticket 经 signal_tower/position_sync 解析为跟单号开仓时记录的
                                # signal_id（hcm:signal_for_ticket），跟单号仅平与之对应的那一仓，
                                # 根治「主号止损1单 → 跟单3单全平」的误平 BUG。缺映射时不兜底平全部
                                # （避免误平多仓），交 reconcile_follower_positions 周期对账兜底孤儿。
                                close_ticket = int(msg_data.get("close_ticket") or 0)
                                if close_ticket > 0:
                                    close_mode = "ticket"
                                    log.info("Manual mirror CLOSE: signal_id=%s symbol=%s mode=%s ticket=%s → precise close follower",
                                             sid, msg_symbol, close_mode, close_ticket)
                                    try:
                                        closed = await _force_close_positions(
                                            mt5, redis_conn, msg_symbol, close_mode, close_ticket
                                        )
                                        # G4 复核：主号先平、跟单号尚未开仓 → 登记 pending-close，
                                        # 待 _execute_signal 开仓后立刻平掉该跟单号（低延时复刻）。
                                        if closed == 0 and close_ticket:
                                            try:
                                                redis_conn.set(
                                                    f"bridge:mirror_pending_close:{ACCOUNT_ID_MODE}:{close_ticket}",
                                                    msg_symbol, ex=86400,
                                                )
                                                log.info("G4 pending-close registered: master=%s sym=%s", close_ticket, msg_symbol)
                                            except Exception as pc_exc:
                                                log.warning("pending-close register failed: %s", pc_exc)
                                    except Exception as fc_exc:
                                        log.exception("Manual mirror CLOSE failed: %s", fc_exc)
                                else:
                                    # 缺主号 ticket 映射 → 不按 symbol 平全部（避免误平多仓），交 reconcile 兜底
                                    log.warning(
                                        "Manual mirror CLOSE: signal_id=%s symbol=%s — close_ticket empty, "
                                        "SKIP (avoid over-close)",
                                        sid, msg_symbol,
                                    )
                                    closed = 0
                                redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                continue
                            # ── Manual mirror MODIFY：主号改 SL/TP → 跟单号同品种持仓 order_modify
                            #    （先于 dedup，避免被开仓 signal_id 的 PG 持久去重拦截）──
                            if direction == "MODIFY":
                                msg_symbol = msg_data.get("symbol", primary_symbol)
                                new_sl = float(msg_data.get("sl_price", 0) or 0)
                                new_tp = float(msg_data.get("tp1", 0) or 0)
                                close_ticket = int(msg_data.get("close_ticket") or 0)
                                log.info(
                                    "Manual mirror MODIFY: signal_id=%s symbol=%s sl=%s tp=%s target=%s",
                                    sid, msg_symbol, new_sl, new_tp, close_ticket,
                                )
                                try:
                                    await _modify_follower_position(mt5, redis_conn, msg_symbol, new_sl, new_tp, target_ticket=close_ticket)
                                except Exception as m_exc:
                                    log.exception("Manual mirror MODIFY failed: %s", m_exc)
                                redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}:modify", "done", ex=2592000)
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                continue
                            # ── Manual mirror PARTIAL_CLOSE：主号部分平仓 → 跟单号平相同增量（1:1）──
                            if direction == "PARTIAL_CLOSE":
                                msg_symbol = msg_data.get("symbol", primary_symbol)
                                close_vol = float(msg_data.get("lot", 0) or 0)
                                close_ticket = int(msg_data.get("close_ticket") or 0)
                                log.info(
                                    "Manual mirror PARTIAL_CLOSE: signal_id=%s symbol=%s vol=%s target=%s",
                                    sid, msg_symbol, close_vol, close_ticket,
                                )
                                try:
                                    await _partial_close_follower_position(mt5, redis_conn, msg_symbol, close_vol, target_ticket=close_ticket)
                                except Exception as pc_exc:
                                    log.exception("Manual mirror PARTIAL_CLOSE failed: %s", pc_exc)
                                redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}:partial_close", "done", ex=2592000)
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                continue
                            # ── Manual mirror ADD：主号加仓 → 跟单号开一笔等量新仓（1:1 继承加仓动作）──
                            #    master_stream 的 add 事件经 signal_tower 镜像为 signal_mode=manual_mirror、
                            #    direction=BUY/SELL、action=add、lot=主号加仓量、signal_id=add_id；
                            #    此处按 FOLLOW_LOT_MULT 缩放开新仓并登记 hcm:signal_for_ticket 映射，
                            #    使后续主号平仓镜像能精确匹配。
                            if msg_data.get("action") == "add":
                                msg_symbol = msg_data.get("symbol", primary_symbol)
                                add_dir = (msg_data.get("direction") or "").upper()
                                add_vol = float(msg_data.get("lot", 0) or 0)
                                add_sl = float(msg_data.get("sl_price", msg_data.get("sl", 0)) or 0)
                                add_tp = float(msg_data.get("tp1", msg_data.get("tp", 0)) or 0)
                                master_account_id = int(msg_data.get("account_id") or 0)
                                # 注释码对齐：add_id = 主号ticket*100000 + 加仓序号(position_sync._publish_manual_master_add)，
                                # 反解为「主号原开仓 signal_id」，使跟单号加仓注释与主号原持仓注释一致（便于核对）。
                                # 主号平仓镜像经 hcm:signal_for_ticket 解析出同一 signal_id 精确匹配，故映射值也必须用解析值
                                # （不能用合成 add_id，否则跟单号加仓仓永远关不掉=孤儿单）。
                                _add_resolved = sid
                                if sid and sid > 0:
                                    _mt = sid // 100000
                                    _add_resolved = _mt
                                    try:
                                        _m = redis_conn.get(f"hcm:signal_for_ticket:{_mt}")
                                        if _m:
                                            _add_resolved = int(_m.decode() if isinstance(_m, bytes) else _m)
                                    except Exception:
                                        pass
                                if add_dir not in ("BUY", "SELL") or add_vol <= 0:
                                    log.warning(
                                        "Manual mirror ADD: signal_id=%s (resolved=%s) bad dir/vol dir=%s vol=%s — SKIP",
                                        sid, _add_resolved, add_dir, add_vol,
                                    )
                                else:
                                    log.info(
                                        "Manual mirror ADD: signal_id=%s (resolved=%s) symbol=%s dir=%s vol=%s sl=%s tp=%s master=%s",
                                        sid, _add_resolved, msg_symbol, add_dir, add_vol, add_sl, add_tp, master_account_id,
                                    )
                                    try:
                                        _mp = {
                                            "symbol": msg_symbol,
                                            "direction": add_dir,
                                            "volume": add_vol,
                                            "sl": add_sl,
                                            "tp": add_tp,
                                        }
                                        await _open_follower_position(
                                            mt5, redis_conn, pool, _mp, master_account_id, str(_add_resolved)
                                        )
                                    except Exception as add_exc:
                                        log.exception("Manual mirror ADD failed: %s", add_exc)
                                redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}:add", "done", ex=2592000)
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                continue
                            # ── Safety Rail 1 (MOVED UP + STRICT): 禁止过期/缺时间戳信号下单 ──
                            # 现优先用 signal_generated_at(T0) 算 age；前置为第一道闸门。
                            if not _is_signal_fresh(msg_data, max_signal_age, redis_conn):
                                _audit_signal_stage(redis_conn, sid, "expired")
                                # 【2026-08-24 修复】expired/时钟偏差拒绝也回写 PG fallback_reason，
                                # 使"过风控未成交"在信号漏斗标记具体卡点（此前 fallback_reason 恒空
                                # → 显示"未标记"，无法定位是过期还是时钟偏差导致错过下单）。
                                await _mark_signal_blocked(pool, sid, "bridge_signal_expired")
                                redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                continue
                            # 2026-08-05 (D7-1): E2E 延迟诊断——信号从生产(T0)到桥消费的耗时
                            _e2e_raw = msg_data.get("signal_generated_at") or msg_data.get("timestamp", "")
                            if _e2e_raw:
                                try:
                                    _e2e_ts = datetime.fromisoformat(str(_e2e_raw).replace("Z", "+00:00"))
                                    if _e2e_ts.tzinfo is None:
                                        _e2e_ts = _e2e_ts.replace(tzinfo=timezone.utc)
                                    _e2e = (datetime.now(timezone.utc) - _e2e_ts).total_seconds()
                                    if _e2e > 0:
                                        log.debug("E2E latency for signal %s/%s: %.1fs (generated_at=%s)",
                                                  sid, msg_data.get("symbol", ""), _e2e, _e2e_raw)
                                except Exception:
                                    pass
                            # 【阶段1 去跟单化 2026-08-24】移除「主号真实持仓对账」守卫（跟单复制开仓前
                            # 检查主号是否已有该信号 open 持仓，无则 SKIP）。此为复制跟单语义残留——
                            # 跟单号必须等主号持仓出现才复制，与独立下单冲突（实证：跟单号收到信号但
                            # 因"master has NO open position"被 SKIP、主号却开仓）。跟单号已独立下单，
                            # 不应再以主号持仓为前提。孤儿/重复单由 reconcile「清孤儿」兜底。
                            # Dedup — Redis fast path
                            if redis_conn.get(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}"):
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                continue
                            # Dedup — PG durable fallback (Redis key may have expired)
                            # 【P0-5 修复 2026-08-03】原仅查 orders，但 orders 行只在平仓
                            # 对账时写入，持仓存活期间无行 → Redis 丢键时同信号可重复开仓。
                            # positions 在开仓成功时即写入（含 signal_id），UNION 两表覆盖
                            # 持仓期与历史全时段。
                            async with pool.acquire() as conn:
                                row = await conn.fetchrow(
                                    "SELECT 1 FROM hcm_trading.positions WHERE signal_id=$1 AND account_id=$2 "
                                    "UNION SELECT 1 FROM hcm_trading.orders WHERE signal_id=$1 AND account_id=$2 LIMIT 1",
                                    sid, ACCOUNT_ID_MODE
                                )
                                if row:
                                    redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)  # restore marker
                                    redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                    continue
                            msg_symbol = msg_data.get("symbol", primary_symbol)
                            # ── Safety Rail 2: Order rate limit (max 50/min) ──
                            if not hasattr(main, '_order_count_window'):
                                main._order_count_window = []  # type: ignore
                            now_ts = time.time()
                            main._order_count_window = [t for t in main._order_count_window if now_ts - t < 60]  # type: ignore
                            if len(main._order_count_window) >= 50:
                                log.critical(
                                    f"BRIDGE RATE LIMIT: {len(main._order_count_window)} orders/min — "
                                    f"REJECT signal {sid} to prevent flood"
                                )
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                continue
                            main._order_count_window.append(now_ts)  # type: ignore
                            log.info(f"Order: signal_id={sid}, symbol={msg_symbol}, {msg_data.get('direction')}")
                            # ── Safety Rail 3: reject non-tradable directions ──
                            # Root-cause fix 2026-07-14: the signal tower emits NO_TRADE /
                            # HOLD when ADX is below floor or midbar is unconfirmed. The
                            # bridge MUST NOT open a position for these. Previously
                            # place_mt5_order() fell through to ORDER_TYPE_SELL for any
                            # non-BUY direction, opening spurious SELL positions
                            # (see tickets 353488252, 353606173, etc.).
                            direction = msg_data.get('direction')
                            # ── P1 FORCE_CLOSE：强制平仓（P3 安全反转）──
                            if msg_data.get("signal_type") == "force_close" or direction == "FORCE_CLOSE":
                                # 【2026-08-18 加固】禁止"平一单→平全仓"：
                                # 若 FORCE_CLOSE 带 close_ticket（主号只平/强平某一单），
                                # 则按 ticket 精确平对应跟单仓，绝不按 symbol 平掉跟单同品种全部持仓；
                                # 无 ticket（策略级品种全平，主号同品种也全平）才按 close_mode(all/half) 执行。
                                close_ticket = int(msg_data.get("close_ticket") or 0)
                                close_mode = msg_data.get("close_mode", "all")
                                if close_ticket > 0:
                                    close_mode = "ticket"
                                log.warning(
                                    "FORCE_CLOSE received: symbol=%s mode=%s ticket=%s — closing positions",
                                    msg_symbol, close_mode, close_ticket,
                                )
                                try:
                                    await _force_close_positions(
                                        mt5, redis_conn, msg_symbol, close_mode,
                                        close_ticket if close_mode == "ticket" else 0,
                                    )
                                except Exception as fc_exc:
                                    log.exception("FORCE_CLOSE execution failed: %s", fc_exc)
                                redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                continue
                            if direction not in ('BUY', 'SELL'):
                                log.warning(
                                    f"SKIP non-tradable signal {sid}: direction={direction} "
                                    f"— not opening position (NO_TRADE/HOLD rejected)"
                                )
                                redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                continue
                            # ── P1a Zone-Trigger Gate (gated OFF by signal_tower.zone_trigger_enabled) ──
                            # When ON and the signal carries entry_trigger_wait > 0 with a zone,
                            # check whether price is already within the trigger tolerance band.
                            # - In band → fill immediately (zone-touched, market order).
                            # - Not in band → store a deferred key in Redis with TTL = wait
                            #   seconds. A periodic recheck below fills when price reaches the
                            #   zone; expired keys are auto-deleted by Redis (no dead orders).
                            deferred = False
                            if zone_trigger_on:
                                zt_wait = int(msg_data.get("entry_trigger_wait", 0) or 0)
                                zl = float(msg_data.get("zone_level", 0) or 0)
                                if zt_wait > 0 and zl > 0:
                                    tick = _mt5_global.symbol_info_tick(real_symbol(msg_symbol))
                                    if tick and not _price_in_zone_band(tick, direction, zl):
                                        _defer_zone_signal(redis_conn, sid, msg_data, zt_wait)
                                        redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
                                        redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                        deferred = True
                                    elif tick:
                                        # T3c ATR 波动过滤：尖刺扫单不立即成交（丢弃该信号，不追单）
                                        _tick_price = tick.ask if direction == "BUY" else tick.bid
                                        _entry_price = float(msg_data.get("entry_price", 0) or 0)
                                        if await _atr_filter_blocks(pool, redis_conn, msg_symbol,
                                                                   _entry_price, _tick_price, direction, 3.0):
                                            log.info("ZONE TOUCH signal %s: ATR filter blocked — dropped (no fill)", sid)
                                            redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
                                            redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                                            deferred = True
                                        else:
                                            log.info("ZONE TOUCH signal %s: price at zone %.2f — filling immediately",
                                                     sid, zl)
                            if not deferred:
                                await _execute_signal(pool, mt5, redis_conn, msg_data,
                                                       msg_symbol, timeframe_str, dry_run)
                                redis_conn.set(f"bridge:processed:{ACCOUNT_ID_MODE}:{sid}", "done", ex=2592000)
                                redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, msg_id)
                last_risk = now
            # ── P1a: read zone-trigger config + recheck pending (every 5s) ──
            if now - last_zone_cfg_check > 5:
                try:
                    ztc = redis_conn.hget("hcm:config:v2", "signal_tower.zone_trigger_enabled")
                    zone_trigger_on = bool(ztc and str(ztc).lower() in ("1", "true"))
                except Exception:
                    zone_trigger_on = False
                if zone_trigger_on and now - last_zone_recheck > 2:
                    await _recheck_zone_pending(pool, mt5, redis_conn, dry_run)
                    last_zone_recheck = now
                last_zone_cfg_check = now

            # Trailing stop: check open positions every 5s and move SL
            if now - last_trail > 5:
                if IS_FOLLOWER:
                    # 跟单桥禁止独立管理 SL/TP：SL/TP 唯一真相源是主号，主号 trailing 改动经
                    # position_sync 检测→manual_mode:master_stream modify 事件→跟单桥 _modify_follower_position
                    # 镜像下发。跟单桥若自己跑 trailing，会用实盘经纪商 tick 把跟单号 SL/TP 推得比主号更激进，
                    # 且 _update_total_trailing_stop 会在跟单号自身总盈亏回撤时"全部平仓"，
                    # 二者皆导致「主号仍持仓、跟单号已平仓」的错位平仓。
                    log.debug("Trailing skipped: IS_FOLLOWER bridge, SL/TP driven by master mirror")
                    # [2026-08-22] 跟单号复刻主号：跟单桥不独立抬 SL，但独立写保本标志
                    #（SL 由主号镜像驱动，读本地 positions_get 即知当前 SL 是否达保本），
                    # 使风控「同向保本闸门」在跟单号上也走毫秒级 Redis 快路径，行为与主号一致。
                    try:
                        _write_be_flags(mt5, redis_conn)
                    except Exception as e:
                        log.error(f"follower BE flag write failed: {e}")
                else:
                    # 加固A2：断连时内部 MT5 调用可能抛异常，局部捕获避免冒泡到主循环
                    try:
                        await _update_trailing_stops(mt5, redis_conn, pool)
                    except Exception as e:
                        log.error(f"trailing stop update failed: {e}")
                    # P2: 总仓位金额移动止盈（仅检查 + 必要时全平，不移动 SL）
                    try:
                        _update_total_trailing_stop(mt5, redis_conn)
                    except Exception as e:
                        log.error(f"total trailing stop update failed: {e}")
                last_trail = now

            # [2026-08-25] 毫秒级跟单 SL/TP：每轮消费 hcm:master:sltp（block=50ms 上限），
            # 主号 trailing/手动改 SL/TP 即时镜像到跟单仓（~50-250ms），10s 轮询作安全网。
            if IS_FOLLOWER:
                try:
                    await _follower_consume_sltp(mt5, redis_conn)
                except Exception as _sltp_exc:
                    log.error(f"follower consume sltp failed: {_sltp_exc}")

            # P0优化：position_sync 与 trailing-stop 解耦，单独高频轮询。
            # 【2026-08-28 毫秒级同步改造】原 ~1s 门控 → 降到 ~0.2s，使主号开/平仓动作
            # 检测与 hcm:direct_close:* 发布延迟从 ~1s 降到 ~0.2s，配合跟单桥 0.1s 消费，
            # 端到端平仓同步进入 tick 级（<0.5s）。trailing-stop 仍维持 5s（上方块）。
            if now - last_sync > 0.2:
                try:
                    await asyncio.wait_for(
                        sync_positions(mt5, pool, redis_conn, account_id_mode=ACCOUNT_ID_MODE),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    log.warning("sync_positions timed out after 5s")
                except Exception as e:
                    log.error(f"sync_positions failed: {e}")
                last_sync = now

            # 跟单桥持仓对账兜底：周期对比主号实时持仓快照与本地持仓，清孤儿单。
            # 根治手动平仓事件因桥重启/抖动丢失→跟单不跟上。运行时毫秒级跟单由 signal:risk_passed 事件链路负责。
            if now - last_reconcile > 10.0:
                try:
                    # 焊死·跟单 SL/TP 强制跟随主号：直接读主号快照，绕开事件长链，兜底根治
                    # 『跟单保本/移动止盈不随主号移动』（即便 modify 事件因任何原因丢失亦生效）。
                    if IS_FOLLOWER:
                        await _sync_follower_sl_tp(mt5, redis_conn)
                except Exception as e:
                    # 【2026-08-13 修复观测性】拆开两函数各自的异常，并用 exception 保留 Traceback，
                    # 避免把 _sync_follower_sl_tp 的异常误标成 reconcile_follower_positions（旧代码根因）。
                    log.exception("sync_follower_sl_tp failed: %s", e)
                try:
                    await reconcile_follower_positions(mt5, redis_conn, pool)
                except Exception as e:
                    log.exception("reconcile_follower_positions failed: %s", e)
                last_reconcile = now

            # 跟单号每日盈亏熔断检查（仅跟单桥；每 30s；超阈值全平+停跟，次日 resume_time 自动恢复）
            if IS_FOLLOWER and now - _follow_circuit_last_check > 30:
                try:
                    # 【2026-08-25 按账户迁移】传 pool：熔断阈值改读本跟单号 relationships
                    # (max_daily_profit/max_daily_loss/circuit_break_enabled) + 账户激活闸门。
                    await _check_follower_circuit_break(mt5, redis_conn, pool, ACCOUNT_ID_MODE)
                except Exception as cb_exc:
                    log.error(f"follower circuit check failed: {cb_exc}")
                # 每日 07:00 记录跟单号账户本金 balance 作熔断分母固定基准
                # (Redis bridge:follow:baseline:{account})；当日已记录则跳过。
                # 【2026-08-25 时区修正】resume_time(close.follow_resume_time=07:00) 是【本地时间】
                # (记忆 40554216)，故 baseline 记录也必须用本地时间 datetime.now()（非 UTC），
                # 否则本地 07:00(=UTC 前一日 23:00) 永远不满足 hour==7 → baseline 键一直缺失，
                # 分母回退当前 balance 而非当日初始本金基准（语义不准）。
                try:
                    _local_now = datetime.now()  # 本地时区，与 resume_time 口径一致；_FOLLOW_BASELINE_LAST_DAY 已在 main 顶层 global
                    if _local_now.hour == 7 and _FOLLOW_BASELINE_LAST_DAY != _local_now.date():
                        _bal = _follower_account_balance(mt5)
                        if _bal > 0:
                            redis_conn.set(
                                _FOLLOW_BASELINE_KEY_TMPL.format(account=ACCOUNT_ID_MODE),
                                str(round(_bal, 2)),
                            )
                            _FOLLOW_BASELINE_LAST_DAY = _local_now.date()
                            log.info(
                                "follower baseline: set balance=%.2f (07:00 local daily baseline)",
                                _bal,
                            )
                except Exception as _bs_exc:
                    log.warning("follower baseline record failed: %s", _bs_exc)
                _follow_circuit_last_check = now

            # [2026-07-24 直连快速通道] 消费主号桥直接写入的 hcm:direct_close:* 平仓指令。
            # 绕过 signal_tower→risk→risk_passed 长链（该链因 ack 失败/XPENDING 堆积/
            # 信号塔重启 xgroup_setid→"$" 抛历史消息而不可靠）。
            # 【2026-08-28 毫秒级同步改造】原每 2s 巡检 → 改为紧跟主循环每轮（>0.1s 门控），
            # 配合主循环 sleep(0.1) 与 sync_positions 0.2s 高频发布，端到端平仓同步延迟
            # 从 ~2s 降到 ~0.3s（tick 级）；保留原逻辑作兜底，仅缩短巡检间隔。
            # 仅 IS_FOLLOWER 生效（跟单桥），保留原事件链路为第一优先路径。
            if IS_FOLLOWER and now - last_direct_close > 0.1:
                try:
                    _dc_keys = redis_conn.keys("hcm:direct_close:*")
                    if _dc_keys:
                        for _dk in _dc_keys:
                            try:
                                _raw = redis_conn.get(_dk)
                                if not _raw:
                                    redis_conn.delete(_dk)
                                    continue
                                _dc = json.loads(_raw)
                                _sym = _dc.get("symbol", "")
                                _close_ticket = int(_dc.get("resolved", 0) or 0)
                                if _sym and _close_ticket > 0:
                                    # 【E 组 P2-7】多跟单桥共享 direct_close 键：
                                    # 原"处理后即 delete"先删先得 → 其余跟单号丢平仓指令。
                                    # 改为键不删（TTL 600s 自然过期），每桥写自身处理标记防重。
                                    _dc_done = f"bridge:processed:{ACCOUNT_ID_MODE}:direct:{_close_ticket}"
                                    if redis_conn.get(_dc_done):
                                        continue
                                    log.info(
                                        "Direct close: processing ticket=%s symbol=%s "
                                        "(master ticket=%s account=%s)",
                                        _close_ticket, _sym,
                                        _dc.get("master_ticket"), _dc.get("account_id"),
                                    )
                                    closed = await _force_close_positions(
                                        mt5, redis_conn, _sym, "ticket", _close_ticket,
                                    )
                                    if closed > 0:
                                        log.info(
                                            "Direct close SUCCESS: closed %s positions for ticket=%s",
                                            closed, _close_ticket,
                                        )
                                    else:
                                        # 可能尚未开仓或已平；G4 补偿登记 pending-close
                                        log.info(
                                            "Direct close: no position to close for ticket=%s "
                                            "(registering pending-close as fallback)",
                                            _close_ticket,
                                        )
                                        try:
                                            redis_conn.set(
                                                f"bridge:mirror_pending_close:{ACCOUNT_ID_MODE}:{_close_ticket}",
                                                _sym, ex=86400,
                                            )
                                        except Exception:
                                            pass
                                    redis_conn.set(_dc_done, "done", ex=86400)
                                else:
                                    redis_conn.delete(_dk)  # 无效 payload 清理残留
                            except Exception as _de:
                                log.error("Direct close processing for key %s failed: %s", _dk, _de)
                                # 解析失败的 key 不要残留（循环爆炸）
                                try:
                                    redis_conn.delete(_dk)
                                except Exception:
                                    pass
                except Exception as e:
                    log.error("Direct close check failed: %s", e)
                last_direct_close = now

            await asyncio.sleep(0.1)
        except KeyboardInterrupt:
            break
        except ConnectionError:
            raise  # propagate to outer wrapper for full reconnect
        except Exception as e:
            log.error(f"Loop: {e}")
            await asyncio.sleep(5)

    # 加固C：干净退出时释放单实例锁（仅当仍是自己的锁）；崩溃退出则靠 TTL 自动过期
    try:
        if redis_conn.get(BRIDGE_LOCK_KEY) == my_lock_id:
            redis_conn.delete(BRIDGE_LOCK_KEY)
            try:
                redis_conn.delete(f"bridge:terminal_for:{live_login}")
            except Exception:
                pass
            log.info(f"Released single-instance lock (PID {my_lock_id})")
    except Exception as e:
        log.warning(f"Lock release failed (TTL will expire it): {e}")

    mt5.shutdown()
    await pool.close()


async def run_bridge_forever(dry_run: bool = False, max_retries: int = 0) -> None:
    """外层自愈循环——任何致命错误都自动重启整个桥。

    Args:
        dry_run: 不执行真实下单。
        max_retries: 最大重试次数（0=无限）。达到上限后 raise 终止。
    """
    retry = 0
    while True:
        try:
            await main(dry_run=dry_run)
            # main() 正常退出（如 MT5 未配置）
            log.warning("main() exited normally, retrying in 10s")
            await asyncio.sleep(10)
            retry = 0  # reset on clean exit
        except KeyboardInterrupt:
            log.info("Bridge stopped by user (Ctrl+C)")
            break
        except InstanceLockedError as e:
            # 锁冲突：干净退出，绝不重试（对比旧逻辑：return 被误判正常退出→无限重试僵尸）
            log.critical(f"Instance lock conflict ({e}) — exiting cleanly, no retry.")
            raise SystemExit(1)
        except Exception as exc:
            retry += 1
            if max_retries > 0 and retry > max_retries:
                log.critical(f"Max retries ({max_retries}) exhausted — giving up")
                raise
            wait = min(retry * 5, 60)  # 5s, 10s, 15s, ... max 60s 指数退避
            log.error(
                f"Bridge FATAL: {exc.__class__.__name__}: {exc}. "
                f"Reconnecting in {wait}s (attempt {retry}"
                f"{'/' + str(max_retries) if max_retries else ''})"
            )
            await asyncio.sleep(wait)


def _publish_trail_sltp_event(redis_conn, pos, new_sl: float, new_tp: float, account_id: int) -> None:
    """trailing 成功移动主号 SL/TP → XADD hcm:master:sltp（毫秒级跟单消费专用流）。

    真·毫秒级根因点：主号 SL 一动即发，跟单桥 xreadgroup(block=50) 即时镜像，
    消灭 10s 轮询滞后。字段 str 化；master_ticket 供跟单桥经 hcm:signal_for_ticket 解析 signal_id。
    """
    if redis_conn is None:
        return
    try:
        redis_conn.xadd(
            "hcm:master:sltp",
            {
                "master_ticket": str(int(pos.ticket)),
                "symbol": str(pos.symbol),
                "sl": str(round(float(new_sl), 5)),
                "tp": str(round(float(new_tp), 5)),
                "account_id": str(int(account_id)),
                "action": "modify",
                "ts": str(int(time.time())),
            },
            maxlen=5000,
        )
        log.info(
            "Trail SL/TP event published (sltp stream): ticket=%s symbol=%s sl=%s tp=%s (account=%s)",
            pos.ticket, pos.symbol, new_sl, new_tp, account_id,
        )
    except Exception as e:
        log.error("Trail SL/TP event publish failed ticket=%s: %s", pos.ticket, e)


async def _update_trailing_stops(mt5, redis_conn, pool):
    """保本 + 单线移动止盈（方案乙，2026-07-14）。

    修复原三档(max 棘轮)的缺陷：固定「锁利档」(entry+lock_amount) 会把 SL 钉死，
    导致价格从 +0.5ATR 涨到 +2ATR 这段最肥行情里 SL 完全不跟价格。

    新逻辑（单一移动止盈线，BUY 只上 / SELL 只下）：
      1) 保本门槛：盈利 > ATR × close.breakeven_atr_mult
         → SL 抬到 开仓价 ± ATR × close.breakeven_buffer_atr_mult（覆盖点差/滑点，P&L≥0）
      2) 单线移动止盈：SL 跟随价格，保持 ATR × close.trail_wide_atr_mult 的距离
         → 取 max(保本地板, 价格-距离)[BUY] / min(保本地板, 价格+距离)[SELL]
         → 早期由保本地板封底，价格跑远后自动由移动线接管，SL 平滑跟随价格
      3) 棘轮：SL 只朝有利方向移动，绝不后退；初始宽保护(close.trailing_stop_distance)保留。
    所有阈值从 Redis close.* 读取（禁用硬编码；缺失键回退到与配置一致的默认值）。
    """
    try:
        enabled = redis_conn.hget("hcm:config:v2", "close.trailing_stop_enabled")
        if not enabled or enabled.lower() not in ('true', '1'):
            return
    except Exception:
        return
    try:
        positions = mt5.positions_get()
        if not positions:
            return

        atr = _get_atr_from_redis(redis_conn, "XAUUSD")
        if atr <= 0:
            log.warning("Trailing stop skipped: ATR unavailable (0.0)")
            return

        # Read trailing stop distance (required) — 会话化：取当前盘 close.<session>.trailing_stop_distance
        # 注意: close.trailing_stop_distance 的语义是「初始保护距离」(ATR 倍数)，
        # 名称中 "distance" 有误导（暗示绝对 pips），但保留兼容现有 Redis key 命名。
        sl_mult = _session_cfg_float(redis_conn, "trailing_stop_distance", 2.0)
        if sl_mult <= 0:
            log.warning("Trailing stop skipped: trailing_stop_distance <= 0")
            return

        # 所有阈值均从 Redis/PG 读取（禁用散落硬编码；会话键 close.<session>.<k> 优先，缺失回退默认值）
        be_trigger = atr * _session_cfg_float(
            redis_conn, "breakeven_atr_mult",
            CLOSE_CONFIG_DEFAULTS["close.breakeven_atr_mult"],
        )
        be_buffer = atr * _session_cfg_float(
            redis_conn, "breakeven_buffer_atr_mult",
            CLOSE_CONFIG_DEFAULTS["close.breakeven_buffer_atr_mult"],
        )
        trail_wide = atr * _session_cfg_float(
            redis_conn, "trail_wide_atr_mult",
            CLOSE_CONFIG_DEFAULTS["close.trail_wide_atr_mult"],
        )
        # ── P0-2: 仅盈利超过该 ATR 倍数后才启动移动止盈线 ──
        # 之前只靠保本地板保护，让盈利单充分奔跑，避免被 trail_wide 贴身扫掉。
        trail_start = atr * _session_cfg_float(
            redis_conn, "trail_start_atr_mult",
            CLOSE_CONFIG_DEFAULTS["close.trail_start_atr_mult"],
        )
        # 移动止盈线启动门槛占 TP 距的比例上限: 有效 trail_start 不超过该比例,
        # 保证移动止盈线 / TP-relay 在固定 TP 触发前就接管追利(否则 trail_start≥TP距
        # 时固定 TP 永远先触发, TP-relay 永远接管不了, 实证 ticket 365683517)。
        trail_start_tp_ratio = _session_cfg_float(
            redis_conn, "trail_start_tp_ratio",
            CLOSE_CONFIG_DEFAULTS["close.trail_start_tp_ratio"],
        )
        # 保本门槛占 TP 距的比例上限（2026-08-11 由硬编码 0.3 提为可配置键）
        breakeven_tp_ratio = _session_cfg_float(
            redis_conn, "breakeven_tp_ratio",
            CLOSE_CONFIG_DEFAULTS["close.breakeven_tp_ratio"],
        )
        initial_distance = atr * sl_mult

        # 承接 TP 追利缓冲: TP 跟随现价前移的距离(ATR 倍数), 会话化优先
        tp_trail_wide = atr * _session_cfg_float(
            redis_conn, "tp_trail_wide_atr_mult",
            CLOSE_CONFIG_DEFAULTS.get("close.tp_trail_wide_atr_mult", 0.7),
        )

        # 移动止盈接力 TP 追利开关（2026-07-23，会话化 2026-07-24）：PG/Redis 热改即时生效
        tp_relay_enabled = _session_cfg_bool(
            redis_conn, "tp_relay_enabled",
            CLOSE_CONFIG_DEFAULTS["close.tp_relay_enabled"],
        )

        # Redis 保本标志（2026-08-22）：供风控「同向保本闸门」毫秒级读取。
        # 每方向仅保留【最新一笔】持仓（open_time 最大）的保本状态；达保本→set "1"(TTL15s)，
        # 未达/无持仓→delete，让标志过期回退 DB 真值源（防"已平仓仍被旧标志拦截"）。
        _be_latest: dict[str, tuple] = {}
        # 【2026-08-25 口径对齐】与 _write_be_flags / 风控 DB 回退同源容差 risk.cool_be_tolerance，
        # 避免内联标志(裸 sl>=entry) 与 _write_be_flags(sl>=entry-tol) 判定相反导致标志抖动。
        _be_tol = _get_close_config(redis_conn, "risk.cool_be_tolerance", 0.0)

        for pos in positions:
            if pos.sl == 0 and pos.price_open is None:
                continue  # need price_open to calculate

            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                continue

            entry = pos.price_open  # entry price
            new_sl = pos.sl or 0.0
            pos_type = "BUY" if pos.type == 0 else "SELL" if pos.type == 1 else ""

            # ── BE 标志记录（先用当前 SL 估算；下方修改成功后用 new_sl 覆盖）──
            _dir = "BUY" if pos.type == 0 else "SELL"
            _eff_sl = pos.sl or 0.0
            _at_be = (_eff_sl >= entry - _be_tol) if _dir == "BUY" else (_eff_sl <= entry + _be_tol)
            _prev_t = (_be_latest.get(_dir) or (0,))[0]
            if _dir and pos.time is not None and pos.time >= _prev_t:
                _be_latest[_dir] = (pos.time, _at_be)

            # ── 灵敏保本门槛 (2026-07-23): 盈利达 TP 距30% 或 0.3×ATR 即保本 ──
            # 原 be_trigger=0.8×ATR 在 ATR 偏高(≈10)时会高于单子最大盈利(到 TP 即平),
            # 导致 SL 永远跨不过保本门槛→SL 永不前移。改为绑定 TP 距, 确保盈利单能保本。
            _tp_dist = abs((pos.tp or 0) - entry) if (pos.tp or 0) > 0 else (atr * 3.0)
            # 有效移动止盈启动门槛: 不超过 TP 距的固定比例, 保证在固定 TP 触发前就接管。
            # trail_start≥TP距 时固定 TP 永远先触发, TP-relay 永远接管不了(实证365683517)。
            _trail_start_eff = min(trail_start, _tp_dist * trail_start_tp_ratio)
            # [2026-07-24 保本过早修复] 去掉硬编码 atr*0.3：盈利仅 0.3ATR 即保本会把
            # 盈利单贴身扫成微利（桥日志实锤 #480091263 曾+1.39 被保本扫到-0.21）。
            # 现在保本门槛 = min(be_trigger=breakeven_atr_mult×ATR, TP距×breakeven_tp_ratio)，
            # breakeven_tp_ratio 为可配置键(默认 0.5)，可经面板/Redis 热调真正延后保本。
            _be_trig = min(be_trigger, _tp_dist * breakeven_tp_ratio)
            _be_trig = max(_be_trig, 1.0)
            new_tp = pos.tp or 0.0  # TP 接力: 默认保持, 仅当前移更优时更新
            _tp_cand = None  # TP 接力候选(关闭或保本段时为 None)

            if pos.type == 0:  # BUY
                profit = tick.bid - entry  # current profit (price distance)
                if profit <= _be_trig:
                    # 未过保本门槛：仅对完全无 SL 的仓位设初始宽保护；亏损单/已有SL单保持不动
                    if pos.sl == 0:
                        new_sl = round(tick.bid - initial_distance, 2)
                        log.info(f"#{pos.ticket} init SL→{new_sl}")
                    continue
                # 过保本门槛：先抬保本地板；仅当盈利 > trail_start 才启动移动止盈线
                breakeven_floor = round(entry + be_buffer, 2)
                if profit > _trail_start_eff:
                    trailing_sl = round(tick.bid - trail_wide, 2)
                    candidate = max(breakeven_floor, trailing_sl)
                    # 移动止盈接力 TP 追利: TP 同步前移, 锁 50% 盈利 + trail_wide 缓冲
                    if tp_relay_enabled:
                        # 承接 TP 追利: TP 跟随现价前移, 保持在价格前方 tp_trail_wide 缓冲,
                        # 价格涨过原 TP 后继续承接趋势奔跑; 价格回落时棘轮不回退(只进不退)。
                        _tp_cand = round(tick.bid + tp_trail_wide, 2)
                else:
                    candidate = breakeven_floor
                    _tp_cand = None
                if candidate > pos.sl:
                    new_sl = candidate
                    reason = "breakeven" if candidate == breakeven_floor else "trailing"
                    log.info(f"#{pos.ticket} {reason}: profit={profit:.1f}, SL {pos.sl}→{new_sl}")
                if _tp_cand is not None and pos.tp and _tp_cand > pos.tp + 0.01:
                    new_tp = _tp_cand
                    log.info(f"#{pos.ticket} TP-relay: TP {pos.tp}→{new_tp} (profit={profit:.1f})")

            elif pos.type == 1:  # SELL
                profit = entry - tick.ask  # current profit (positive when price falls)
                if profit <= _be_trig:
                    # 未过保本门槛：仅对完全无 SL 的仓位设初始宽保护；亏损单/已有SL单保持不动
                    if pos.sl == 0:
                        new_sl = round(tick.ask + initial_distance, 2)
                        log.info(f"#{pos.ticket} init SL→{new_sl}")
                    continue
                breakeven_floor = round(entry - be_buffer, 2)
                if profit > _trail_start_eff:
                    trailing_sl = round(tick.ask + trail_wide, 2)
                    candidate = min(breakeven_floor, trailing_sl)
                    if tp_relay_enabled:
                        # 承接 TP 追利: TP 跟随现价前移, 保持在价格前方 tp_trail_wide 缓冲
                        # (SELL 在价格下方), 价格跌破原 TP 后继续承接趋势奔跑; 棘轮只进不退。
                        _tp_cand = round(tick.ask - tp_trail_wide, 2)
                else:
                    candidate = breakeven_floor
                    _tp_cand = None
                # 【E 组 P2-3】SELL 无 SL 仓(pos.sl==0)时 candidate>0 即应接受：
                # 原 `candidate < pos.sl(=0)` 恒假 → SELL 无 SL 仓保本/追利永不生效
                #（BUY 侧 candidate>pos.sl 在 sl=0 时天然成立，两侧不对称）。
                if (pos.sl == 0 and candidate > 0) or (candidate < pos.sl):
                    new_sl = candidate
                    reason = "breakeven" if candidate == breakeven_floor else "trailing"
                    log.info(f"#{pos.ticket} {reason}: profit={profit:.1f}, SL {pos.sl}→{new_sl}")
                if _tp_cand is not None and pos.tp and _tp_cand < pos.tp - 0.01:
                    new_tp = _tp_cand
                    log.info(f"#{pos.ticket} TP-relay: TP {pos.tp}→{new_tp} (profit={profit:.1f})")

            # 发送: SL 前移 或 TP 接力前移 (任一变化即提交; 棘轮保证只优化不回退)
            if new_sl > 0 and (new_sl != pos.sl or new_tp != (pos.tp or 0)):
                request = {
                    "action": _mt5_global.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "symbol": pos.symbol,
                    "sl": new_sl,
                    "tp": new_tp,
                }
                r = _mt5_global.order_send(request)
                if r and r.retcode == 10009:
                    log.info(f"Trail #{pos.ticket} {pos_type}: SL {pos.sl}→{new_sl} TP→{new_tp}")
                    # 以最新下发 SL 覆盖 BE 标志（该笔为最新持仓时生效）
                    _at_be2 = (new_sl >= entry - _be_tol) if _dir == "BUY" else (new_sl <= entry + _be_tol)
                    _be_latest[_dir] = (pos.time, _at_be2)
                    # 【G1-2026-08-25】移动 SL 成功后立即回写 PG，消除 position_sync 周期差
                    # 导致的引擎读旧 SL 误判 at_be=False → 误拒同向新单。
                    # 主号/跟单号经 mt5_ticket 关联，覆盖当前运行账户的全部持仓。
                    try:
                        async with pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE hcm_trading.positions SET sl=$1, updated_at=now() "
                                "WHERE mt5_ticket=$2",
                                round(new_sl, 5), pos.ticket,
                            )
                    except Exception as _pg_exc:
                        log.warning(f"Trail PG flush #{pos.ticket} SL failed: {_pg_exc}")
                    # 【2026-08-25 毫秒级跟单 SL/TP】trailing 一动即发布 hcm:master:sltp 事件，
                    # 跟单桥主循环内 xreadgroup(block=50) 即时镜像（~50-250ms），
                    # 彻底消除原 10s _sync_follower_sl_tp 轮询滞后（行情已跑远才跟上）。
                    # 仅主号桥走到此（跟单桥 trailing 已跳过），account_id=主号。
                    try:
                        _publish_trail_sltp_event(redis_conn, pos, new_sl, new_tp, ACCOUNT_ID_MODE)
                    except Exception as _sltp_exc:
                        log.warning(f"Trail SL/TP event publish failed #{pos.ticket}: {_sltp_exc}")
                else:
                    retcode = r.retcode if r else "N/A"
                    err_info = ""
                    try:
                        if r:
                            err_info = r.comment or ""
                        if not err_info:
                            err_info = str(mt5.last_error())
                    except Exception:
                        err_info = "unknown"
                    log.error(
                        f"Trail FAIL #{pos.ticket} {pos_type}: "
                        f"SL {pos.sl}→{new_sl} TP→{new_tp}, retcode={retcode}, {err_info}"
                    )
        # 写 Redis 保本标志（毫秒级供风控读取）：达保本 set "1"(TTL 15s)，未达/无持仓 delete
        if ACCOUNT_ID_MODE:
            try:
                for _d in ("BUY", "SELL"):
                    _t, _be = _be_latest.get(_d, (0, False))
                    _key = f"hcm:pos:be:{ACCOUNT_ID_MODE}:{_d}"
                    if _be:
                        redis_conn.set(_key, "1", ex=15)
                    else:
                        redis_conn.delete(_key)
            except Exception as _be_exc:
                log.error(f"BE flag write failed: {_be_exc}")
            # 【2026-08-25】主号也写 symbol 级保本标志（供 hexp 极值分层裁决）：
            # _write_be_flags 重读 MT5 当前 SL，聚合 symbol→dir→be 写 hcm:pos:be:sym:*。
            # 跟单桥在 _update_trailing_stops 外已单独调用（IS_FOLLOWER 分支），主号这里补。
            if not IS_FOLLOWER:
                try:
                    _write_be_flags(mt5, redis_conn)
                except Exception as _sb_exc:
                    log.error(f"symbol BE flag write failed: {_sb_exc}")
    except Exception as e:
        log.error(f"_update_trailing_stops error: {e}")


def _write_be_flags(mt5, redis_conn) -> None:
    """写 Redis 保本标志（主桥/跟单桥共用，2026-08-22）。

    供风控「同向保本闸门」毫秒级读取 `hcm:pos:be:{ACCOUNT_ID_MODE}:{BUY|SELL}`：
      每方向仅保留【最新一笔】持仓（open_time 最大）的保本状态；
      达保本（BUY: sl>=entry / SELL: sl<=entry）→ set "1"(TTL 15s)；
      未达 / 无持仓 → delete（标志缺失 → 风控回退查 PG 真值源，防"已平仓仍被旧标志拦截"）。
    注：与 _update_trailing_stops 内的内联标志写入语义一致（该处用循环内 new_sl 即时值，
        本函数重读 MT5 当前 SL；二者等价，因 order_send 成功后同会话 positions_get 即返回新 SL）。
    """
    if not ACCOUNT_ID_MODE:
        return
    try:
        _positions = mt5.positions_get()
    except Exception:
        return  # 读取失败不盲删，让旧标志 TTL 过期回退 DB
    if not _positions:
        # 无持仓（flat）→ 清空两方向标志（含 symbol 级）
        for _d in ("BUY", "SELL"):
            try:
                redis_conn.delete(f"hcm:pos:be:{ACCOUNT_ID_MODE}:{_d}")
            except Exception:
                pass
        return
    _latest: dict[str, tuple] = {}
    # 【2026-08-25 symbol 级保本标志】hexp 极值护栏分层裁决需判断"该 symbol 是否有保本持仓"
    # （hexp 无 account_id，读不了账户级键）。聚合本桥各持仓的 symbol→dir→be：
    # 任一持仓达保本即该 symbol:dir 置 1，供 hexp 决定是否豁免极值硬封。
    _sym_be: dict[str, dict[str, bool]] = {}
    # 【2026-08-25 加固】与风控 _check_cooldown DB 回退同源容差 risk.cool_be_tolerance，
    # 避免同一持仓在 Redis 标志(裸 sl>=entry) 与 PG 真值源(sl>=entry-tol) 判定相反。
    _be_tol = _get_close_config(redis_conn, "risk.cool_be_tolerance", 0.0)
    for _pos in _positions:
        _dir = "BUY" if _pos.type == 0 else "SELL"
        _entry = getattr(_pos, "price_open", None)
        _sl = getattr(_pos, "sl", None)
        _t = getattr(_pos, "time", 0) or 0
        _sym = getattr(_pos, "symbol", "XAUUSD")
        if _entry is None or _sl is None:
            continue
        _be = (_sl >= _entry - _be_tol) if _dir == "BUY" else (_sl <= _entry + _be_tol)
        if _t >= (_latest.get(_dir) or (0,))[0]:
            _latest[_dir] = (_t, _be)
        # symbol 级：任一持仓达保本即置 True
        _sb = _sym_be.setdefault(_sym, {})
        if _be:
            _sb[_dir] = True
        else:
            _sb.setdefault(_dir, False)
    for _d in ("BUY", "SELL"):
        _t, _be = _latest.get(_d, (0, False))
        _key = f"hcm:pos:be:{ACCOUNT_ID_MODE}:{_d}"
        try:
            if _be:
                redis_conn.set(_key, "1", ex=15)
            else:
                redis_conn.delete(_key)
        except Exception:
            pass
    # symbol 级标志写入（TTL 15s，与账户级一致；hexp 读它做极值豁免判断）
    for _sym, _dirs in _sym_be.items():
        for _d in ("BUY", "SELL"):
            _skey = f"hcm:pos:be:sym:{_sym}:{_d}"
            try:
                if _dirs.get(_d):
                    redis_conn.set(_skey, "1", ex=15)
                else:
                    redis_conn.delete(_skey)
            except Exception:
                pass


def _update_total_trailing_stop(mt5, redis_conn):
    """总仓位金额移动止盈（2026-07-17 P2 新增）。

    逻辑：
      1) 遍历所有持仓，求总盈亏（USD）
      2) 当总盈亏 >= close.total_trail_start_amount → 启动跟踪，记录峰值到 Redis
      3) 当总盈亏从峰值回撤到 <= close.total_trail_stop_amount → 全部平仓锁利
      4) 未到启动线时重置峰值；全部平仓后也重置
    所有阈值从 Redis 读，无硬编码；close.total_trail_enabled 关闭则跳过。
    """
    try:
        enabled = redis_conn.hget("hcm:config:v2", "close.total_trail_enabled")
        if not enabled or enabled.lower() not in ('true', '1'):
            return
    except Exception:
        return

    try:
        # 按面板配置的检查间隔节流（默认 10s；≤0 视为 10）
        try:
            check_interval = int(float(redis_conn.hget("hcm:config:v2", "close.total_trail_check_interval") or 10))
        except Exception:
            check_interval = 10
        if check_interval <= 0:
            check_interval = 10
        now_ts = time.time()
        last_chk_str = redis_conn.get(f"hcm:positions:total_trail_last_check:{ACCOUNT_ID_MODE}")
        if last_chk_str and (now_ts - float(last_chk_str)) < check_interval:
            return
        redis_conn.set(f"hcm:positions:total_trail_last_check:{ACCOUNT_ID_MODE}", str(now_ts))
    except Exception:
        # 节流失败也继续执行（不阻塞主路径）
        pass

    try:
        positions = mt5.positions_get()
        if not positions:
            try:
                redis_conn.delete(f"hcm:positions:peak_profit:{ACCOUNT_ID_MODE}")
            except Exception:
                pass
            return

        # 读配置（无值用面板默认值）
        try:
            start_amount = float(redis_conn.hget("hcm:config:v2", "close.total_trail_start_amount") or 30)
            stop_amount = float(redis_conn.hget("hcm:config:v2", "close.total_trail_stop_amount") or 15)
        except Exception:
            start_amount, stop_amount = 30.0, 15.0

        # 计算总盈亏
        total_profit = 0.0
        pos_data = []
        for pos in positions:
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                continue
            # 【E 组 P2-2】优先用 MT5 持仓自带 profit（券商口径真实 USD）；
            # 原手算未乘合约乘数，与 position_sync(×100) 口径差 100 倍，
            # 致 start_amount=30 实际需 ~3000 美元利润才触发。手算仅作兜底。
            _pos_profit = float(getattr(pos, "profit", 0.0) or 0.0)
            if _pos_profit != 0.0:
                p = _pos_profit
            elif pos.type == 0:  # BUY
                p = (tick.bid - pos.price_open) * pos.volume * 100
            elif pos.type == 1:  # SELL
                p = (pos.price_open - tick.ask) * pos.volume * 100
            else:
                p = 0.0
            total_profit += p
            pos_data.append((pos, tick, p))

        # 读/写峰值
        try:
            peak_str = redis_conn.get(f"hcm:positions:peak_profit:{ACCOUNT_ID_MODE}")
            peak = float(peak_str) if peak_str else 0.0
        except Exception:
            peak = 0.0

        # 未到启动线 → 重置峰值
        if total_profit < start_amount:
            if peak > 0:
                try:
                    redis_conn.delete(f"hcm:positions:peak_profit:{ACCOUNT_ID_MODE}")
                except Exception:
                    pass
            return

        # 启动跟踪 → 更新峰值
        if total_profit > peak:
            peak = total_profit
            try:
                redis_conn.set(f"hcm:positions:peak_profit:{ACCOUNT_ID_MODE}", str(peak))
            except Exception:
                pass
            log.info(f"Total trail: peak profit updated to {peak:.2f} USD (n={len(pos_data)})")

        # 回撤保护：峰值-当前 >= 峰值-保护线 → 全部平仓
        if peak > 0 and total_profit <= stop_amount:
            log.info(
                f"Total trail triggered: peak={peak:.2f}, current={total_profit:.2f}, "
                f"stop={stop_amount:.2f}, n={len(pos_data)} → closing all"
            )
            for pos, tick, p in pos_data:
                try:
                    close_type = _mt5_global.ORDER_TYPE_SELL if pos.type == 0 else _mt5_global.ORDER_TYPE_BUY
                    r = _mt5_global.order_send({
                        "action": _mt5_global.TRADE_ACTION_DEAL,
                        "position": pos.ticket,
                        "symbol": pos.symbol,
                        "volume": pos.volume,
                        "type": close_type,
                        "price": tick.bid if pos.type == 0 else tick.ask,
                    })
                    if r and r.retcode == 10009:
                        log.info(f"Total trail closed #{pos.ticket} P&L~{p:.2f}")
                    else:
                        retcode = r.retcode if r else "N/A"
                        log.error(f"Total trail FAIL #{pos.ticket}: retcode={retcode}")
                except Exception as e:
                    log.error(f"Total trail close #{pos.ticket} error: {e}")
            try:
                redis_conn.delete(f"hcm:positions:peak_profit:{ACCOUNT_ID_MODE}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"_update_total_trailing_stop error: {e}")


if __name__ == "__main__":
    # 隐藏控制台窗口（仅 Windows）：进程仍持有控制台(non-DETACHED)，
    # 故 import MetaTrader5 原生 DLL 稳定；仅窗口不可见。
    try:
        import ctypes as _ct
        _hwnd = _ct.windll.kernel32.GetConsoleWindow()
        if _hwnd:
            _ct.windll.user32.ShowWindow(_hwnd, 0)
    except Exception:
        pass
    dry_run = "--dry-run" in sys.argv
    max_retries = 0  # 默认无限重试
    # 支持 --max-retries=N 参数
    for arg in sys.argv:
        if arg.startswith("--max-retries="):
            max_retries = int(arg.split("=")[1])
        elif arg.startswith("--terminal-path="):
            # 动态发现模式：指定要服务的 MT5 终端路径；bridge 自行读取该终端实登账号。
            # 不硬编码任何账户 ID——「MT5 里登哪个号，bridge 就服务哪个号」。
            TERMINAL_PATH_MODE = arg.split("=", 1)[1]
    print(f"[bridge] terminal_path={TERMINAL_PATH_MODE or '(auto-scan)'} "
          f"— 将动态发现 MT5 实登账号（禁用硬编码账户）")
    asyncio.run(run_bridge_forever(dry_run=dry_run, max_retries=max_retries))
