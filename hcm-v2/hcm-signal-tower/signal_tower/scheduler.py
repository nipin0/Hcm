"""Scheduler — bar_close trigger → signal production pipeline.

The scheduler is the orchestration layer of signal-tower:
1. Monitors K-line data for bar_close events
2. Triggers the signal production pipeline per symbol
3. Coordinates: indicator calculation → regime classification →
   scoring → AI invocation → signal publication

Implements staggered start for multiple symbols to avoid
resource contention.

Design: per-symbol task with independent timing.

Fix #1-#5 (2025-07-10):
  #1 Bar confirmation: prevent chasing reversals
  #2 Bar momentum: single-bar velocity scoring
  #3 AI Fallback Gate: protect ai_fallback from extreme bars
  #4 Live bar push: MT5 bridge pushes unclosed bar to Redis
  #5 Circuit breaker: bar_range > 2×ATR → skip signal
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date
from typing import Any, Optional
from .bar_quality import compute_bar_quality
from .micro_state import MicroStateClassifier, MicroState
from .precision_entry import PrecisionEntryScorer

# ─────────────────────────────────────────────────────────────────────────────
# 时段感知风险档案 (2026-07-24) —— 与桥 mt5_bridge._current_session / _session_cfg*
# 同源：按 UTC 小时切亚盘/欧盘/美盘；会话键 close.<session>.<suffix> 优先于全局键。
# ─────────────────────────────────────────────────────────────────────────────
def _current_session_utc() -> str:
    """返回当前时段：亚盘 asia / 欧盘 europe / 美盘 us（基于 UTC 小时）。

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


async def _session_float(redis, suffix: str, default: float, fallback_keys=()) -> tuple:
    """会话优先读取浮点配置。

    依次尝试 close.<session>.<suffix> → close.<suffix> → fallback_keys(全局别名键)
    → default。返回 (value, is_session) —— is_session=True 表示命中了会话专属键。
    """
    sess = _current_session_utc()
    try:
        v = await redis.hget("hcm:config:v2", f"close.{sess}.{suffix}")
    except Exception:
        v = None
    if v not in (None, ""):
        try:
            return float(v), True
        except (TypeError, ValueError):
            pass
    for key in (f"close.{suffix}", *fallback_keys):
        try:
            v = await redis.hget("hcm:config:v2", key)
        except Exception:
            v = None
        if v not in (None, ""):
            try:
                return float(v), False
            except (TypeError, ValueError):
                pass
    return float(default), False

import numpy as np


def _calc_hurst_for_regime(closes, max_lag: int = 32) -> float:
    """轻量 Hurst 估计（简化 R/S），供 regime 模糊区次级确认使用。
    与 hexp_engine.HexpEngine._hurst 同算法：log(var(ΔlnP))~log(τ) 斜率/2。
    返回 0.5 表示无足够数据（中性）。
    """
    try:
        arr = np.asarray(closes, dtype=np.float64)
        if arr.size < max_lag + 4:
            return 0.5
        logp = np.log(arr)
        lags = list(range(2, max_lag + 1))
        var_vals = []
        for tau in lags:
            if tau >= arr.size:
                break
            diff = logp[tau:] - logp[:-tau]
            var_vals.append(float(np.var(diff)))
        if len(var_vals) < 3 or any(v <= 0 for v in var_vals):
            return 0.5
        lags_u = np.log(np.asarray(lags[:len(var_vals)], dtype=np.float64))
        var_u = np.log(np.asarray(var_vals, dtype=np.float64))
        slope = np.polyfit(lags_u, var_u, 1)[0]
        return float(np.clip(slope / 2.0, 0.0, 1.0))
    except Exception:
        return 0.5


from signal_tower.indicator_calculator import (
    IndicatorCalculator,
    IndicatorResults,
    DEFAULT_RSI_PERIOD,
    DEFAULT_MACD_FAST,
    DEFAULT_MACD_SLOW,
    DEFAULT_MACD_SIGNAL,
    DEFAULT_ADX_PERIOD,
    DEFAULT_BOLL_PERIOD,
    DEFAULT_BOLL_STD,
    DEFAULT_STOCH_K,
    DEFAULT_STOCH_D,
    DEFAULT_STOCH_SMOOTH,
    DEFAULT_MA_SHORT,
    DEFAULT_MA_LONG,
    compute_pivots,
    compute_support_resistance,
    compute_round_levels,
    build_confluence_zones,
    Zone,
)
from signal_tower.regime_classifier import Regime, RegimeClassifier, RegimeConfig, RegimeResult
from signal_tower.h1_regime_classifier import H1RegimeClassifier, H1Context
from signal_tower.scoring_engine import ScoreResult, ScoringEngine
# 【2026-08-28 co_source 清除】双源信号模式(CoSourceEngine v1/v2)整体下线，只保留 HEXP。
# 原 import: from signal_tower.co_source import CoSourceEngine —— 已随 co_source.py 删除。
from signal_tower.hexp_engine import HexpEngine
from signal_tower.range_bonus import RangeBonus
from signal_tower.risk_state_sync import sync_risk_state
from signal_tower.force_close import ForceCloseDetector
from signal_tower.manual_mode import ManualModeHandler
from signal_tower.signal_publisher import SignalData, SignalPublisher
from signal_tower.watchdog import WatchdogManager
from signal_tower.quality_gate import decide as ai_quality_decide
from signal_tower.quality_gate import log_gate_decision as ai_log_gate_decision
from signal_tower.ai_async_client import (
    calibrate_lm_score,
    read_ds_out,
    produce_async_outputs,
    run_loop as ai_ds_run_loop,
)
from shared.signal_tower_defaults import (
    LIVE_BAR_KEY_TEMPLATE,
)

# ── 信号生产机制配置表（根治“机制切换硬编码分支”）──
# 全链路路由只依赖本表 + 配置中心(signal.active_model / signal_tower.mode)，
# 机制登记表：新增/切换机制只需在此登记 + 注册评分引擎，无需改热路径分支。
# 五维共识模型(five_dim) 及其 ai_dynamic 机制已弃用（2026-07-24）。
# 【2026-08-28 co_source 清除】双源信号模式(co_source)整体下线，其登记表条目已移除；
# 未登记的 active_model 经 .get(..., "default") 兜底，此处 default 语义对齐 hexp
# （与 _detect_active_model 只返 hexp/manual 保持一致，杜绝落入已删除分支）。
MECHANISM_PROFILES = {
    "manual":      {"engine": "_scoring_engine", "uses_co": False},
    "default":     {"engine": "_hexp_engine", "uses_co": True},
    # 和乘幂（hexp）：独立信号源；engine 字段登记实例属性名（_hexp_engine），
    # 但因其为异步多周期管线，实际调用走 _produce_signal/_live_score_publisher 的
    # hexp 显式分支（produce() 需 await 拉取 H1/H4/D1/M1 K 线，无法复用同步
    # compute_pre_score 热路径）。uses_co=True 使 G3 执行增强读取其 co_exec_* 覆盖。
    "hexp":        {"engine": "_hexp_engine", "uses_co": True},
}

logger = logging.getLogger(__name__)


def _format_suppress_reason(chain: list[tuple[int, str]]) -> str:
    """P2c: collapse a (priority, text) suppress chain into one readable reason.

    Highest priority wins as the primary cause; the rest are appended so the
    full diagnostic chain is retained. Returns "" when the chain is empty.
    """
    if not chain:
        return ""
    ordered = sorted(chain, key=lambda item: item[0], reverse=True)
    return " > ".join(text for _, text in ordered)


def _threshold_reason(score_result) -> str:
    """P2c: build a truthful filter reason when threshold_passed is False.

    The naive 'below_threshold(pre<threshold)' text is misleading when the real
    blocker is that *no trade direction was produced* — e.g. micro_state=RANGE,
    where pre_score can legitimately exceed threshold yet direction stays
    NO_TRADE. In that case the score is NOT below the gate; labelling it
    'below_threshold(0.498<0.450)' is factually false and misleads diagnostics.
    Distinguish the two cases so the displayed reason reflects reality.
    """
    pre = float(score_result.pre_score)
    thr = float(score_result.threshold)
    if pre < thr:
        return f"below_threshold({pre:.3f}<{thr:.3f})"
    if getattr(score_result, "direction", "") == "NO_TRADE":
        band = getattr(score_result, "co_band", "") or ""
        return f"no_trade_direction(pre={pre:.3f},thr={thr:.3f},band={band})"
    return f"below_threshold({pre:.3f}<{thr:.3f})"

# ── Default Config ─────────────────────────────

DEFAULT_SYMBOLS = ["XAUUSD", "BTCUSD"]
DEFAULT_TIMEFRAMES = {"XAUUSD": "M5", "BTCUSD": "M15"}
DEFAULT_IDLE_SLEEP = 5.0
DEFAULT_LOOP_ERROR_SLEEP = 5.0
DEFAULT_KLINE_STALE_MAX_BARS = 3
DEFAULT_CONFIG_RELOAD_INTERVAL_SECONDS = 30  # P0: hot-reload cadence for live param tuning
TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}

# P0 fix (2026-07-15): kline feed staleness ceiling (seconds). If the latest
# closed bar's open_time has not advanced for longer than this, the market-data
# feed is treated as genuinely broken — production is paused (not force-advanced
# into a self-DoS) and an ALERT is logged. 10 min = 2× M5 bar length.
KLINE_STALE_THRESHOLD_SEC = 600

# 2026-08-05 (D9-2): 生产健康断言阈值。连续 N 根 bar 未产出任何决策
# (BUY/SELL/NO_TRADE 任一) → WARNING。正常每根 bar 都会产出决策(filtered 也算)，
# 故该计数只在 _produce_signal 抛异常(被上游吞为 Symbol loop error)时才累加，
# 用于早于 consecutive_errors>=5 的 CRITICAL 给出"链路故障 vs 策略放弃"区分。
DECISION_STALL_WARN_BARS = 20


# 2026-08-05 (D8): scheduler 不再做同向持仓数闸门——持仓数上限完全由风控引擎
# rule_chain._check_open_positions 负责（account+symbol 总持仓封顶 risk.max_concurrent_signals）。


@dataclass
class SymbolState:
    """Per-symbol scheduling state."""
    symbol: str
    timeframe: str = "M5"
    bar_seconds: int = 300
    last_bar_open_time: Optional[datetime] = None
    last_signal_time: float = 0.0
    last_direction: str = ""
    consecutive_errors: int = 0
    kline_not_ready_count: int = 0
    enabled: bool = True
    # Regime / scoring thresholds (per-period, loaded from config)
    trend_adx_threshold: float = 22.0
    trend_adx_exit_threshold: float = 18.0
    dispute_diff_threshold: float = 0.05
    min_score_threshold: float = 0.15
    # ADX trading floor (config-driven, default 22.0) — must match
    # ScoringEngine._min_adx_for_trade so the directional signal is gated
    # by a minimum ADX.
    min_adx_for_trade: float = 22.0

    # 2026-08-05 (D9-2): 决策产出停滞计数。每根 bar 调用 _produce_signal 前 +1，
    # 一旦 _produce_signal 产出任何决策(BUY/SELL 或 NO_TRADE/filtered)即归零。
    bars_without_decision: int = 0
    decision_stall_warned: bool = False

    # ── Bar confirmation state ──
    prev_bar_open: float = 0.0
    prev_bar_close: float = 0.0

    # ── Live override (B4: adx_floor 跨门槛救场) ──
    # When the last bar-close signal was NO_TRADE due to adx_floor, the scheduler
    # spawns a 5s _live_override_loop that re-evaluates indicators. If live
    # conditions now pass (adx>=22 sustained 30s, score>=0.15), it emits a
    # rescue signal with signal_mode="live_override" so the operator sees
    # direction immediately rather than waiting 3-5 min for next bar close.
    last_signal_blocked_by_adx_floor: bool = False
    last_live_override_time: float = 0.0
    adx_above_floor_since: float = 0.0   # when live_adx first crossed 22
    last_signal_mode: str = ""            # "" or "M5" or "live_override"
    prev_bar_direction: str = ""

    # ── 精确触发闸门（2026-08-10）──
    # 根因：原 live 触发是「电平触发」——分值只要持续在门槛之上，每 30s 限流窗口
    # 一到就再产一条信号（实测 115 条/小时 = 每 31s 一条，而 M5 每小时仅 12 根棒）。
    # 改为「边沿触发 + 单棒单发」：
    #   live_edge_armed      —— 武装位，仅在分值/方向形成【新的上升沿】时才允许触发；
    #                           触发后落位，须分值跌回门槛下方或方向翻转才重新武装。
    #   last_live_trigger_bar —— 已触发过的 bar 开盘时间，同一根 K 线最多触发一次。
    live_edge_armed: bool = True
    last_live_trigger_bar: Optional[datetime] = None


class Scheduler:
    """Orchestrates the signal production pipeline.

    On bar_close:
      0. Market circuit check (Fix #5)
      1. Fetch K-line data from PostgreSQL + Redis live bar (Fix #4)
      2. Compute technical indicators
      3. Classify market regime (five-level)
      4. Bar confirmation (Fix #1)
      5. Compute pre_score with regime-aware weights + bar_momentum (Fix #2)
      6. Apply cooldown check
      7. (AI Invocation removed — five_dim model deprecated 2026-07-24; co_source local scoring only)
      8. Publish signal via dual-write

    Example:
        scheduler = Scheduler(db_pool, redis_client, config_provider,
                             signal_publisher, watchdog)
        await scheduler.start()
    """

    def __init__(
        self,
        db_pool: Any = None,
        redis_client: Any = None,
        config_provider: Any = None,
        signal_publisher: Optional[SignalPublisher] = None,
        watchdog: Optional[WatchdogManager] = None,
    ):
        """Initialize Scheduler.

        Args:
            db_pool: DatabasePool instance for K-line queries.
            redis_client: RedisClient instance for cache reads.
            config_provider: ConfigProviderV3 for runtime parameters.
            signal_publisher: SignalPublisher for dual-write.
            watchdog: WatchdogManager for health monitoring.
        """
        self._db = db_pool
        self._redis = redis_client
        self._config = config_provider
        self._signal_publisher = signal_publisher
        self._watchdog = watchdog

        # Core engines
        self._indicator_calc = IndicatorCalculator()
        self._regime_classifier = RegimeClassifier()
        self._scoring_engine = ScoringEngine(config_provider=config_provider)
        # 【2026-08-28 co_source 清除】原共源信号增强引擎 CoSourceEngine 初始化已移除
        # （双源模式整体下线，只保留 HEXP；co_source.py 已删除）。
        # ── 和乘幂引擎（hexp；signal.active_model=hexp 时生效）──
        # 独立信号源：HP-Score 广义均值 + k 自适应 + 多周期共振 + 微结构动量。
        # kline_fetcher 复用 _fetch_klines（PG + Redis 实时 bar 合并），
        # redis 用于发布 hcm:live:hexp:{symbol} 实时快照（TTL15s）。
        self._hexp_engine = HexpEngine(
            config_provider=config_provider,
            kline_fetcher=self._fetch_klines,
            redis_client=redis_client,
        )
        self._force_close = ForceCloseDetector(
            config_provider=config_provider, redis_client=redis_client,
        )
        # 手动模式 — 全部继承主账号所有动作
        self._manual_mode = ManualModeHandler(
            redis_client=redis_client, config_provider=config_provider,
        )
        self._range_bonus = RangeBonus()

        # ── P0/P1: H1 多周期融合（HMTS 状态判定层）──
        self._h1_classifier = H1RegimeClassifier(config_provider=config_provider)

        # ── Phase 0 (2026-08-05): 微观状态机 + 精准买点分（shadow-only）──
        # 与现有评分链路并行计算，仅输出对比样本，绝不改变交易行为。
        self._micro_state = MicroStateClassifier(config_provider=config_provider)
        self._precision_entry = PrecisionEntryScorer(config_provider=config_provider)
        self._shadow_v2_loaded = False

        # Per-symbol state
        self._symbols: dict[str, SymbolState] = {}
        self._tasks: dict[str, asyncio.Task] = {}

        # Global state
        self._running = False
        self._start_time: float = 0.0
        self._account_id: Optional[int] = None  # cached from DB
        self._account_id_resolved_at: float = 0.0  # 根治：主号缓存时间戳，周期重解析

        # Config hot-reload (P0: live parameter tuning)
        self._config_reload_task: Optional[asyncio.Task] = None
        self._config_reload_interval: int = DEFAULT_CONFIG_RELOAD_INTERVAL_SECONDS

        # P0 fix (2026-07-15): kline feed staleness ceiling (seconds). Beyond
        # this, the feed is treated as broken and production is paused (no
        # force-advance self-DoS). See KLINE_STALE_THRESHOLD_SEC.
        self._kline_stale_threshold_sec: int = KLINE_STALE_THRESHOLD_SEC

        # P0 fix (2026-07-15): per-(symbol,timeframe) idempotency guard.
        # Stores the last bar open_time that was actually produced so a stuck
        # feed (repeated force-advance on the same bar) cannot re-publish the
        # same signal. Key = "symbol:timeframe" → open_time.
        self._last_produced_open_time: dict[str, Any] = {}
        self._stats: dict[str, int] = {
            "loops": 0,
            "signals_produced": 0,
            "signals_bypassed": 0,
            "errors": 0,
            "circuit_trips": 0,
            "bar_confirm_fails": 0,
            "ai_gate_rejects": 0,
        }
        # 2026-08-18：AI 闸门决策落库暂存（signal_id 生成前先缓存，生成后补记真实 signal_id，
        # 供 daily_kpi 用 gate_decision.signal_id ↔ orders.signal_id 做 AI 盈亏归因）。
        # key=symbol → (_snap, _decision, _ai_cfg)
        self._pending_ai_gate: dict[str, tuple] = {}

        # ── Prompt templates (per-model routing, P2 multi-model) ──
        # Global (legacy) fallback templates — loaded from Redis.
        self._prompt_template: Optional[str] = None          # user_prompt_template (global)
        self._system_prompt: Optional[str] = None            # system_prompt (global)
        # Per-model overrides, keyed by active model: ai_dynamic / co_source / manual
        self._prompt_templates: dict[str, str] = {}          # {model: user_prompt_template}
        self._system_prompts: dict[str, str] = {}            # {model: system_prompt}

        # ── Live override (B 修复) 配置 —— 全部 config 驱动，禁硬编码 ──
        # 这些参数经 self._config 读取(PG metadata → Redis hcm:config:v2)，
        # 由 seed 脚本双写落库，重启/热重载均生效。
        self._live_override_enabled: bool = True
        self._live_override_sustained_sec: int = 30
        self._live_override_rate_limit_sec: int = 60
        self._live_override_check_interval_sec: int = 5
        # ── AI 门控优化配置 ──
        # False(默认): AI 返回 NO_TRADE 视为"弃权"，不否决已通过 floor+threshold
        #   的评分信号(防止 AI 单方面杀死可交易信号)。True: 允许 AI 以 NO_TRADE 否决。
        self._ai_allow_notrade_veto: bool = False

        # ── 闭环：LightGBM(sidecar) + DeepSeek(异步校准) 闸门 ──
        # DeepSeek 客户端（构造自 .env 的 DEEPSEEK_API_KEY）；None 时 run_loop 自动降级。
        self._deepseek_client: Any = None
        # 后台 DeepSeek 校准循环的任务（每 active symbol 一个）
        self._ai_ds_tasks: dict[str, asyncio.Task] = {}
        # 融合闸门最近一次决策的快照（供面板观测，键=symbol）
        self._ai_quality_last: dict[str, dict] = {}

        # ── 信号引擎状态发布 + 远程激活（供 /api/system/pipeline 检测引擎存活与激活模型）──
        self._published_model: Optional[str] = None        # 最近一次发布的激活模型
        self._mode_switched_at: Optional[datetime] = None  # 最近一次模型切换时间
        self._engine_status_key: str = "hcm:signal_tower:engine_status"
        self._control_key: str = "hcm:signal_tower:control"

    # ── Lifecycle ───────────────────────────────

    async def start(
        self, symbols: Optional[list[str]] = None
    ) -> None:
        """Start the scheduler with per-symbol tasks.

        Args:
            symbols: List of symbols to schedule (default: from config).
        """
        if symbols is None:
            if self._config is not None:
                active_json = await self._config.get_json(
                    "active_symbols", DEFAULT_SYMBOLS
                )
                symbols = active_json if isinstance(active_json, list) else DEFAULT_SYMBOLS
            else:
                symbols = DEFAULT_SYMBOLS

        # Initialize per-symbol state — read timeframe from config, falling
        # back to DEFAULT_TIMEFRAMES for each symbol.
        config_timeframe: Optional[str] = None
        if self._config is not None:
            try:
                config_timeframe = await self._config.get("datasource.timeframe")
            except Exception:
                pass
        # Normalize: if config returns empty/M5 (the default), use DEFAULT_TIMEFRAMES fallback
        if not config_timeframe or config_timeframe.strip() == "" or config_timeframe == "M5":
            config_timeframe = None

        for i, sym in enumerate(symbols):
            if config_timeframe is not None:
                tf = config_timeframe
            else:
                tf = DEFAULT_TIMEFRAMES.get(sym, "M5")

            # Load per-period regime/scoring thresholds from Redis (no hardcoding)
            trend_adx = 22.0
            trend_adx_exit = 18.0
            dispute_diff = 0.05
            min_score = 0.15
            if self._config is not None:
                try:
                    trend_adx = await self._config.get_float(f"scoring.{tf}.trend_adx_threshold", 22.0)
                    trend_adx_exit = await self._config.get_float(f"scoring.{tf}.trend_adx_exit_threshold", 18.0)
                    dispute_diff = await self._config.get_float(f"scoring.{tf}.dispute_diff_threshold", 0.05)
                    min_score = await self._config.get_float(f"scoring.{tf}.min_score_threshold", 0.15)
                except Exception:
                    pass

            bar_sec = TIMEFRAME_SECONDS.get(tf, 300)
            state = SymbolState(
                symbol=sym, timeframe=tf,
                bar_seconds=bar_sec,
                trend_adx_threshold=trend_adx, trend_adx_exit_threshold=trend_adx_exit,
                dispute_diff_threshold=dispute_diff, min_score_threshold=min_score,
            )
            self._symbols[sym] = state

        self._running = True
        self._start_time = time.time()

        # Start per-symbol tasks with staggered delay
        for i, sym in enumerate(symbols):
            st = self._symbols[sym]
            # Main loop — wait for bar close, produce signal
            task = asyncio.create_task(self._symbol_loop(st))
            self._tasks[sym] = task
            # Manual mode mirror loop: poll Redis key every 1s so master trades
            # are mirrored immediately instead of waiting for the next M5 bar close.
            task = asyncio.create_task(self._manual_mode_loop(st))
            self._tasks[f"{sym}:manual"] = task
            # B4: sub-bar live override rescue for adx_floor stale NO_TRADE.
            # Runs every 5s. Emits a live_override signal when live ADX
            # crosses 22 sustained — rescues 3-5 min M5 staleness.
            asyncio.create_task(self._live_override_loop(st))
            # Live ADX publisher — runs every 5s, writes real ADX to Redis.
            # Dashboard reads this instead of duplicating ADX computation.
            asyncio.create_task(self._live_adx_publisher(st))
            # B 层: 实时评分快照发布 + bar 内阈值穿越触发（贴合实时价格变动）
            asyncio.create_task(self._live_score_publisher(st))
            # Stagger to avoid all symbols triggering simultaneously
            if i > 0:
                await asyncio.sleep(1.0)

        # P0: periodic hot-reload of runtime parameters (no restart needed)
        if self._config is not None:
            self._config_reload_task = asyncio.create_task(self._config_reload_loop())

        # D6: 周期性风险态同步（每 300s 写入 Redis，使 F3/F5/风险偏移生效）
        asyncio.create_task(self._periodic_risk_sync(interval_sec=300))

        # P0: 周期性标注回写（每 900s 把已平仓盈亏写回 labeled_samples.label，
        # 解锁校准层/Optuna 的真实盈亏样本，是 AI 自我发展的前提）
        asyncio.create_task(self._label_reconcile_loop(interval_sec=900))

        # P1: 周期性每日校准快照（每 3600s 重算各体制累计胜率→校准因子时序，
        # 供「校准时序」页回放 AI 自我发展轨迹；仅新标注时才重算）
        asyncio.create_task(self._calibration_daily_loop(interval_sec=3600))

        # 2026-08-18：AI 每日 KPI 聚合（方案 B：日表 + 后台每小时聚合 + AI 真实盈亏贡献）。
        # fail-open：DB 故障/表缺失静默跳过，不影响交易主循环。
        asyncio.create_task(self._ai_daily_kpi_loop(interval_sec=3600))

        # ── 闭环：启动 DeepSeek 异步校准循环（每 active symbol 一个）──
        # 拉起前确保 DeepSeek 客户端已构造（无 key 时 run_loop 自动降级，不会崩）。
        await self._ensure_deepseek_client()
        for sym in symbols:
            t = asyncio.create_task(self._ai_ds_loop_for_symbol(sym))
            self._ai_ds_tasks[sym] = t

        logger.info("Scheduler started: symbols=%s, timeframes=%s", symbols,
                    {s: self._symbols[s].timeframe for s in symbols})

    async def stop(self) -> None:
        """Gracefully stop all per-symbol tasks."""
        self._running = False
        # Cancel config hot-reload loop
        if self._config_reload_task is not None:
            self._config_reload_task.cancel()
            try:
                await self._config_reload_task
            except asyncio.CancelledError:
                pass
            self._config_reload_task = None
        for sym, task in self._tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        # 取消 DeepSeek 校准循环
        for sym, task in self._ai_ds_tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._ai_ds_tasks.clear()
        logger.info("Scheduler stopped (stats=%s)", self._stats)

    # ── 闭环：DeepSeek 异步校准（LightGBM 融合闸门的数据供给侧）──
    async def _deepseek_config(self) -> dict:
        """从配置中心(PG/Redis 真源)读取 DeepSeek 凭证；不依赖 .env 硬编码。

        配置键(由 DeepSeek AI 配置面板写入 PG hcm_config.metadata + Redis 双写)：
          - deepseek.api_key
          - deepseek.api_base  (默认 https://api.deepseek.com)
          - deepseek.model     (默认 deepseek-chat)
        缺键时返回空串，由调用方决定是否降级（不抛异常、不回退 .env 硬编码）。
        """
        out: dict = {}
        if self._config is None:
            return out
        for k in ("deepseek.api_key", "deepseek.api_base", "deepseek.model"):
            try:
                v = await self._config.get(k)
                if v is not None and str(v).strip() != "":
                    out[k] = v
            except Exception:
                pass
        return out

    async def _ensure_deepseek_client(self) -> None:
        """惰性构造 DeepSeek 客户端；无 key 时保持 None（run_loop 自动降级）。

        凭证只从配置中心(deepseek.api_key 等)读取，禁止硬编码或依赖 .env 注入，
        符合"配置真源 = PG hcm_config.metadata ↔ Redis hcm:config:v2"的纪律。
        """
        if self._deepseek_client is not None:
            return
        try:
            from shared.llm_client import DeepSeekClient
            ds = await self._deepseek_config()
            api_key = ds.get("deepseek.api_key")
            base_url = ds.get("deepseek.api_base")
            model = ds.get("deepseek.model")
            self._deepseek_client = DeepSeekClient(
                api_key=api_key, base_url=base_url, model=model,
            )
            logger.info("DeepSeek client initialized (%s)",
                        "available" if getattr(self._deepseek_client, "is_available", False) else "NO KEY → async-loop degrades")
        except Exception as exc:
            logger.warning("DeepSeek client init failed: %s — async loop will degrade", exc)
            self._deepseek_client = None

    async def _ai_cfg_dict(self) -> dict:
        """热读 ai.* 配置 → dict，供 ai_async_client.run_loop / calibrate_lm_score 使用。

        【2026-08-18 解耦】ai.fuse.w_lm / ai.fuse.w_ds 已废弃（DeepSeek 不再与
        LightGBM 融合），故从白名单移除；ai.fuse.ds_max_age_sec 保留 —— 它现仅用于
        判定 DeepSeek 观测票是否陈旧（陈旧则观测字段置空，不影响裁决）。
        """
        if self._config is None:
            return dict(CFG_FALLBACK)
        keys = [
            "ai.enabled", "ai.mode",
            "ai.ds.enabled", "ai.ds.timeout_sec",
            "ai.fuse.ds_max_age_sec",
            "ai.lm.pass_threshold", "ai.lm.down_threshold", "ai.lm.up_threshold",
            # 【B6 修复 2026-08-14】veto_floor 此前漏进白名单 → 面板调否决线不生效
            "ai.lm.veto_floor",
            "ai.lm.veto_quantile", "ai.lm.down_quantile", "ai.lm.up_quantile",
            # 【B8】sl_coeff clamp 上下限配置化（此前 P1a 硬编码 0.8/1.5）
            "ai.ds.sl_coeff_min", "ai.ds.sl_coeff_max",
            "ai.cpl.enabled", "ai.cpl.w_trend", "ai.cpl.w_neutral", "ai.cpl.w_range",
            "ai.cpl.k_trend_min", "ai.cpl.k_range_max", "ai.cpl.tier_high",
            "ai.cpl.tier_mid", "ai.cpl.tier_low", "ai.cpl.lot_high", "ai.cpl.lot_low",
        ]
        out: dict = {}
        for k in keys:
            try:
                v = await self._config.get(k)
                # config_provider.get 缺键返回 ""（非 None）→ 仅保留非空值
                if v is not None and str(v).strip() != "":
                    out[k] = v
            except Exception:
                pass
        return out

    async def _ai_ds_loop_for_symbol(self, symbol: str) -> None:
        """每 symbol 的 DeepSeek 校准后台循环；读 hcm:live:hexp:{symbol} 快照喂给 run_loop。"""
        import asyncio as _asyncio

        async def _get_snapshot(sym: str) -> Optional[dict]:
            if self._redis is None:
                return None
            try:
                raw = await self._redis.get(f"hcm:live:hexp:{sym.upper()}")
                if not raw:
                    return None
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "ignore")
                import json as _json
                snap = _json.loads(raw) if raw else None
                if not isinstance(snap, dict):
                    return None
                # 2026-08-15 闭环补全：把外部因子(market_intel)并入快照，
                # 既供 build_prompt 让 DeepSeek 知情，也供 run_loop 做"事件态"触发判断。
                # 外部因子键：composite / macro / sentiment / event / liquidity(0~1) + stub_mode。
                ext = {}
                for _dim in ("composite", "macro", "sentiment", "event", "liquidity"):
                    try:
                        _v = await self._redis.get(f"hcm:market:{_dim}:score")
                        ext[_dim] = float(_v) if _v is not None else None
                    except Exception:
                        ext[_dim] = None
                try:
                    _stub = await self._redis.get("hcm:market:stub_mode")
                    ext["stub_mode"] = str(_stub).lower() == "true" if _stub else None
                except Exception:
                    ext["stub_mode"] = None
                snap["external"] = ext
                # 【2026-08-15 三处对齐-2】把 LightGBM 实际推理用的 25 维特征
                # (hcm:live:hexp:ai:{symbol}.lm_features) 合并进快照，供 build_prompt
                # 与 LightGBM 同构消费（见 ai_async_client.build_prompt）。
                # 两个键同源、TTL 一致(15s)，缺失时不影响 prompt（仅降级缺该段）。
                try:
                    _lm_raw = await self._redis.get(f"hcm:live:hexp:ai:{sym.upper()}")
                    if _lm_raw:
                        if isinstance(_lm_raw, (bytes, bytearray)):
                            _lm_raw = _lm_raw.decode("utf-8", "ignore")
                        _lm_obj = _json.loads(_lm_raw) if isinstance(_lm_raw, str) else _lm_raw
                        if isinstance(_lm_obj, dict):
                            _lmf = _lm_obj.get("lm_features")
                            if isinstance(_lmf, dict) and _lmf:
                                snap["lm_features"] = _lmf
                except Exception:
                    pass
                return snap
            except Exception:
                return None

        await ai_ds_run_loop(
            symbol,
            _get_snapshot,
            await self._ai_cfg_dict(),
            self._redis,
            llm=self._deepseek_client,
            cache_ttl_sec=1800,
            # 【2026-08-15 规则触发】interval_sec 不再是固定轮询间隔，而是触发缺失时
            # 的兜底轮询步长(传给 run_loop 的 trigger_poll_sec 由 run_loop 内部默认 2s)。
            # DeepSeek 仅在规则命中(ai:ds:trigger:{symbol} 标志被 _read_ai_quality 写入)时才调用。
            interval_sec=60,
            cfg_reader=self._ai_cfg_dict,
            enabled_reader=lambda: self._config.get_bool("ai.ds.enabled", False) if self._config else False,
            # 【B4 修复 2026-08-14】此前未传 db_pool → ds_output/runtime_event 观测表恒空
            db_pool=self._db if (self._db is not None and self._db.is_initialized) else None,
            # 规则触发标志键（scheduler 侧规则命中时写入）
            trigger_key=f"ai:ds:trigger:{symbol.upper()}",
            trigger_poll_sec=2.0,
        )

    async def _read_ai_quality(self, symbol: str) -> Optional[dict]:
        """在 _produce_signal 中调用：读 LightGBM 分 → 闸门决策。

        【2026-08-18 DeepSeek/LightGBM 评分解耦】
        运行期 c_ai【单源取 LightGBM】(hcm:live:hexp:ai:{symbol}.ai_score)，
        经 quality_gate 裁决 hexp 信号是否值得下单、给几倍手数。

        DeepSeek 异步票(ai:ds:out:{symbol}) 仍被读取，但【仅作观测落库】：
          - 不参与 c_ai 计算、不参与 VETO/升降级/手数分档、不缩放 SL
          - 其真正赋能路径 = 离线训练管线：
              build_labels --ds-calibrate → labels.csv 的 ds_calib_weight
              → train_signal_quality.py 用作 LightGBM 训练 sample_weight
            即 DeepSeek 通过"影响模型怎么学"来校准 LightGBM，而非运行期改分。
        规则触发写 ai:ds:trigger 仍保留（为训练管线持续积累 DeepSeek 票）。

        2026-08-14 加固：入口 try-except 确保任何配置/Redis 异常都返回 None（而非
        抛出导致 _produce_signal 崩溃、被 symbol loop 静默吞掉）。
        """
        try:
            if self._config is None or not await self._config.get_bool("ai.enabled", False):
                return None

            cfg = await self._ai_cfg_dict()
        except Exception as _e:  # noqa: BLE001
            logger.warning("read_ai_quality config error (symbol=%s): %s", symbol, _e)
            return None

        # 1) LightGBM 票（sidecar 实时发布，0-100）
        # 【2026-08-17 方案⑤】sidecar 断流兜底：
        #   sidecar 每 ~5s 写 hcm:live:hexp:ai:{symbol}（TTL=15s）。若 sidecar 崩/断流，
        #   Redis 残留旧 ai_score 直到 TTL 过期（≤15s 空窗）。coupled 模式若拿旧分裁决，
        #   会误把"断流前的真实低分残留"当成当前 AI 判断 VETO 正常信号（假低分误杀）。
        #   故此处强制校验 ts 新鲜度：age > ai.lm.max_age_sec(默认60s) → lm_score=None
        #   → calibrate_lm_score 返回 source="none" → quality_gate 透传纯 HEXP（不误杀）。
        #   模型真在场且分低（fresh lm_score < veto_floor）→ 仍 VETO（保留 AI 否决权）。
        lm_score = None
        ai_direction = None
        ai_dir_prob = None
        ai_entry = None
        try:
            if self._redis is not None:
                raw = await self._redis.get(f"hcm:live:hexp:ai:{symbol.upper()}")
                if raw:
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode("utf-8", "ignore")
                    import json as _json
                    obj = _json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(obj, dict):
                        _ai = obj.get("ai_score")
                        # 【阶段 1·方向共振】从 sidecar 快照读 dir_lm(方向头)输出，
                        # 供 ai_quality_decide 与 dir_hexp(HEXP 方向)共振(同向增强/反向否决)。
                        # 字段缺失/异常 → 保持 None（不共振，向后兼容）。
                        _ad = obj.get("ai_direction")
                        _adp = obj.get("ai_dir_prob")
                        ai_direction = _ad if _ad in ("BUY", "SELL", "HOLD") else None
                        ai_dir_prob = None
                        if _adp is not None:
                            try:
                                ai_dir_prob = float(_adp)
                            except (TypeError, ValueError):
                                ai_dir_prob = None
                        # 【阶段 2·买点共振】从 sidecar 快照读 entry_lm(买点头)好买点概率，
                        # 供 ai_quality_decide 与 hexp entry_quality 共振(增强好点位/否决差点位)。
                        # 字段缺失/异常 → 保持 None（不共振，向后兼容）。
                        _ae = obj.get("ai_entry")
                        ai_entry = None
                        if _ae is not None:
                            try:
                                _ae_f = float(_ae)
                                if 0.0 <= _ae_f <= 1.0:
                                    ai_entry = _ae_f
                            except (TypeError, ValueError):
                                ai_entry = None
                        if _ai is not None:
                            try:
                                lm_score = float(_ai)
                                # 新鲜度校验（仅当 sidecar 写了 ts 才判；旧值无 ts 保守视过期）
                                _ts = obj.get("ts")
                                try:
                                    _max_age = float(await self._config.get("ai.lm.max_age_sec") or 60)
                                except (TypeError, ValueError):
                                    _max_age = 60.0
                                if _ts is not None and _max_age > 0:
                                    _age = time.time() - float(_ts)
                                    if _age > _max_age:
                                        logger.info(
                                            "AI LM score stale (symbol=%s): age=%.1fs > max_age=%.1fs "
                                            "→ treat as missing (pass-through HEXP)",
                                            symbol, _age, _max_age,
                                        )
                                        lm_score = None
                                elif _ts is None:
                                    # sidecar 未发布 ts（极老版本）→ 保守视过期，不误用旧分
                                    logger.debug(
                                        "AI LM score missing ts (symbol=%s) → treat as missing",
                                        symbol,
                                    )
                                    lm_score = None
                            except (TypeError, ValueError):
                                lm_score = None
        except Exception:
            lm_score = None

        # 2) DeepSeek 异步票
        ds_out = await read_ds_out(self._redis, symbol) if self._redis is not None else None

        # 2b) 规则触发：命中以下任一规则才写 ai:ds:trigger 标志让 run_loop 调 DeepSeek
        # （2026-08-15 改时间轮询为规则触发 + 外部因子事件态触发）。
        #   规则A stale：DeepSeek 票缺失或距上次成功 > ai.ds.trigger_stale_sec(默认120s)
        #   规则B event：外部因子 event 维度(0~1) >= ai.ds.trigger_event_min(默认0.6)
        #            = 重大财经事件窗口临近，技术信号易失真，需 AI 语义补盲
        #   规则C composite跳变：composite 较上次记录跳变 >= ai.ds.trigger_composite_delta
        #            (默认0.15) = 外部综合风险骤升/骤降，需重新校准 fake_prob
        # 三条规则任一命中即触发，避免"无事件时也每120s空转烧配额"。
        if self._redis is not None:
            _fire = False
            _reason = []
            try:
                _stale_sec = float(await self._config.get("ai.ds.trigger_stale_sec") or 120)
            except (TypeError, ValueError):
                _stale_sec = 120.0
            # 规则A stale
            _stale = True
            if isinstance(ds_out, dict) and ds_out.get("ts") is not None:
                try:
                    _age = time.time() - float(ds_out["ts"])
                    _stale = _age > _stale_sec
                except (TypeError, ValueError):
                    _stale = True
            if _stale:
                _fire = True
                _reason.append("stale")
            # 规则B/C：外部因子（从 HEXP 快照的 external 或 Redis 直接读）
            _ext = None
            try:
                _raw_ext = await self._redis.get(f"hcm:live:hexp:{symbol.upper()}")
                if _raw_ext:
                    if isinstance(_raw_ext, (bytes, bytearray)):
                        _raw_ext = _raw_ext.decode("utf-8", "ignore")
                    import json as _json
                    _obj = _json.loads(_raw_ext) if isinstance(_raw_ext, str) else _raw_ext
                    _ext = (_obj or {}).get("external") if isinstance(_obj, dict) else None
            except Exception:
                _ext = None
            if not isinstance(_ext, dict):
                _ext = {}
            try:
                _event_min = float(await self._config.get("ai.ds.trigger_event_min") or 0.6)
            except (TypeError, ValueError):
                _event_min = 0.6
            _evt = _ext.get("event")
            if isinstance(_evt, (int, float)) and _evt >= _event_min:
                _fire = True
                _reason.append(f"event={_evt:.2f}>={_event_min}")
            try:
                _delta = float(await self._config.get("ai.ds.trigger_composite_delta") or 0.15)
            except (TypeError, ValueError):
                _delta = 0.15
            _comp = _ext.get("composite")
            if isinstance(_comp, (int, float)):
                try:
                    _prev = await self._redis.get(f"ai:ds:composite_last:{symbol.upper()}")
                    _prev = float(_prev) if _prev is not None else _comp
                    if abs(_comp - _prev) >= _delta:
                        _fire = True
                        _reason.append(f"compositeΔ={abs(_comp-_prev):.2f}>={_delta}")
                    await self._redis.set(
                        f"ai:ds:composite_last:{symbol.upper()}", str(_comp), ex=86400
                    )
                except Exception:
                    pass
            if _fire:
                try:
                    await self._redis.set(
                        f"ai:ds:trigger:{symbol.upper()}",
                        ";".join(_reason), ex=int(_stale_sec),
                    )
                    logger.debug(
                        "DeepSeek trigger fired (symbol=%s): %s",
                        symbol, ";".join(_reason),
                    )
                except Exception:
                    pass

        # 【2026-08-24 修复·方案C根因】写 ai:ds:trigger 标志的逻辑已移到 ai.mode 判断之前
        # （见上方 783 段），确保 decoupled 模式下也持续为训练管线积累 DeepSeek 票。
        # 运行期 AI 裁决（c_ai fusion）仅 coupled 模式执行；decoupled 模式写完 trigger 即返回
        # None，由调用方回退纯 HEXP 信号（与 2026-08-18 解耦语义一致）。
        if (await self._config.get("ai.mode", "decoupled")) != "coupled":
            return None

        # 3) 产出 c_ai —— 【单源 LightGBM】，DeepSeek 不参与（2026-08-18 解耦）
        fusion = calibrate_lm_score(lm_score, ds_out, cfg)

        # 4) DeepSeek 字段【仅观测透传】（2026-08-18 解耦）：
        #    ai_sl_coeff / continuity_score 不再注入开仓 ai_sl_mult ——
        #    开仓 SL 宽度已由 LightGBM lm_score 单源缩放（见 P1a 的 sl_scale 段）。
        #    这两个字段保留返回，仅供：
        #      a) 持仓期 continuity_engine 调仓（既有独立职责，不属开仓裁决）
        #      b) gate_decision 观测落库 / 训练样本回溯
        #    命名加 ds_ 前缀以杜绝被误当作开仓因子消费。
        if isinstance(ds_out, dict):
            try:
                fusion["ds_sl_coeff_obs"] = float(ds_out.get("ai_sl_coeff") or 1.0)
            except (TypeError, ValueError):
                fusion["ds_sl_coeff_obs"] = 1.0
            try:
                fusion["ds_continuity_obs"] = int(ds_out.get("continuity_score") or 0)
            except (TypeError, ValueError):
                fusion["ds_continuity_obs"] = 0
        else:
            fusion["ds_sl_coeff_obs"] = 1.0
            fusion["ds_continuity_obs"] = 0

        # 5) 【阶段 1/2 方向头/买点头】把 sidecar 三头输出随 ai_q 返回给 _produce_signal。
        #    此前这三个变量是本方法局部变量，而 _produce_signal 直接按同名引用 →
        #    NameError「name 'ai_direction' is not defined」（实时评分快照路径持续告警）。
        #    现显式挂到返回 dict 上，由调用方通过 ai_q.get(...) 取值（字段缺失 → None）。
        fusion["ai_direction"] = ai_direction
        fusion["ai_dir_prob"] = ai_dir_prob
        fusion["ai_entry"] = ai_entry

        return fusion

    # ── Symbol Loop ─────────────────────────────

    # ── 信号引擎状态发布 + 远程激活 ─────────────────────────────
    async def _publish_engine_status(self) -> None:
        """每循环发布引擎状态到 Redis，供 /api/system/pipeline 检测引擎存活与激活模型。"""
        try:
            now = datetime.now(timezone.utc)
            active_model = await self._detect_active_model()
            if active_model != self._published_model:
                self._mode_switched_at = now
                self._published_model = active_model
            last_prod_raw = None
            if self._redis is not None:
                try:
                    last_prod_raw = await self._redis.get(
                        "hcm:signal_tower:last_production:XAUUSD:M5"
                    )
                except Exception:
                    last_prod_raw = None
            last_prod_ts: Optional[float] = None
            if last_prod_raw:
                try:
                    last_prod_ts = datetime.fromisoformat(
                        last_prod_raw.replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    last_prod_ts = None
            status = {
                "running": True,
                "active_model": active_model,
                "last_loop_at": now.timestamp(),
                "loops": self._stats.get("loops", 0),
                "signals_produced": self._stats.get("signals_produced", 0),
                "last_production_at": last_prod_ts,
                "mode_switched_at": (
                    self._mode_switched_at.timestamp() if self._mode_switched_at else None
                ),
                "ts": now.timestamp(),
            }
            if self._redis is not None:
                await self._redis.set(
                    self._engine_status_key, json.dumps(status), ex=600
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("publish_engine_status failed: %s", e)

    async def _publish_engine_alert(self, code: str, detail: str) -> None:
        """发布引擎异常告警到 Redis channel ``engine:alert``（供监控/诊断订阅）。

        D2+D11 (2026-08-04): 信号生产模块级 bug 或 symbol 循环连续失败时即时通知，
        避免静默停滞。发布失败静默忽略（不影响主流程）。
        """
        if self._redis is None:
            return
        try:
            await self._redis.publish(
                "engine:alert",
                json.dumps({
                    "code": code,
                    "detail": detail,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }),
            )
        except Exception:  # noqa: BLE001
            pass

    async def _maybe_activate(self) -> None:
        """消费 /api/system/engine/activate 的激活指令：重置棒检测状态以恢复生产。"""
        try:
            if self._redis is None:
                return
            ctrl = await self._redis.get(self._control_key)
            if not ctrl:
                return
            try:
                await self._redis.delete(self._control_key)
            except Exception:
                pass
            logger.info(
                "Engine ACTIVATE received → resetting bar/production state to resume signal production"
            )
            for st in self._symbols.values():
                st.last_bar_open_time = None
            self._last_produced_open_time.clear()
            self._published_model = None  # 重新检测激活模型
        except Exception as e:  # noqa: BLE001
            logger.warning("engine activate handling failed: %s", e)

    async def _symbol_loop(self, state: SymbolState) -> None:
        """Per-symbol main loop: detect bar_close → produce signal.

        Args:
            state: SymbolState for this symbol.
        """
        logger.info("Symbol loop started: %s (tf=%s)", state.symbol, state.timeframe)
        tf_seconds = TIMEFRAME_SECONDS.get(state.timeframe, 300)

        while self._running:
            # Reset stale last_bar_open_time to force fresh bar detection
            if state.last_bar_open_time is not None:
                now = datetime.now(timezone.utc)
                if state.last_bar_open_time.tzinfo is None:
                    last = state.last_bar_open_time.replace(tzinfo=timezone.utc)
                else:
                    last = state.last_bar_open_time
                age_hours = (now - last).total_seconds() / 3600
                if age_hours > 1:
                    logger.warning(
                        "Resetting stale last_bar_open_time (%.1fh old) for %s",
                        age_hours, state.symbol,
                    )
                    state.last_bar_open_time = None

            # ── 引擎激活指令 + 每循环状态发布（供管线检测存活/激活模型）──
            await self._maybe_activate()
            await self._publish_engine_status()

            if self._watchdog:
                self._watchdog.mark_loop_start()

            try:
                # Wait for bar_close
                bar_closed = await self._wait_bar_close(state, tf_seconds)
                if not bar_closed:
                    continue

                # Produce signal
                # 2026-08-05 (D9-2): 每根 bar 进入前累加停滞计数；一旦本 bar 产出
                # 决策(_produce_signal 内 publish/filtered 会归零)，计数不增。
                state.bars_without_decision += 1
                await self._produce_signal(state)
                state.consecutive_errors = 0  # 成功 → 重置连续错误计数
                # 决策停滞预警：连续 N 根 bar 无 BUY/SELL/NO_TRADE → 链路故障信号
                if state.bars_without_decision >= DECISION_STALL_WARN_BARS and not state.decision_stall_warned:
                    logger.warning(
                        "No decision produced for %d consecutive bars (%s) — "
                        "signal production likely STALLED (not a strategy skip). "
                        "Check for Symbol loop errors above.",
                        state.bars_without_decision, state.symbol,
                    )
                    state.decision_stall_warned = True

            except asyncio.CancelledError:
                break
            except Exception as exc:
                # D2+D11 (2026-08-04): 异常分级 + 完整 traceback。
                # 旧 logger.error 无堆栈，模块级 bug（UnboundLocalError 等）静默停滞无可见报错。
                logger.exception("Symbol loop error (%s): %s", state.symbol, exc)
                state.consecutive_errors += 1
                self._stats["errors"] += 1

                # 致命异常（代码缺陷）→ 立即 CRITICAL 告警
                _fatal_types = (UnboundLocalError, AttributeError, NameError, TypeError)
                if isinstance(exc, _fatal_types):
                    logger.critical(
                        "MODULE-LEVEL BUG in symbol loop (%s): %s — "
                        "engine likely stalled, investigate immediately",
                        state.symbol, type(exc).__name__,
                    )
                    await self._publish_engine_alert(
                        "symbol_loop_module_bug",
                        f"{state.symbol}: {type(exc).__name__}: {exc}",
                    )

                # 连续失败 ≥5 次：该 symbol 循环停滞，二阶兜底告警
                if state.consecutive_errors >= 5:
                    logger.critical(
                        "Symbol loop STALLED (%s): %d consecutive errors "
                        "(likely module-level bug) — last: %s",
                        state.symbol, state.consecutive_errors, exc,
                    )
                    await self._publish_engine_alert(
                        "symbol_loop_stalled",
                        f"{state.symbol}: {state.consecutive_errors} consecutive errors: {exc}",
                    )

                if self._watchdog:
                    await self._watchdog.report_step(
                        "loop_error", 0.0,
                    )

                await asyncio.sleep(DEFAULT_LOOP_ERROR_SLEEP)

            if self._watchdog:
                self._watchdog.mark_loop_end()
                await self._watchdog.beat()

            self._stats["loops"] += 1

    async def _manual_mode_loop(self, state: SymbolState) -> None:
        """Manual mode: event-driven mirror loop (P1优化).

        原实现每 1s 轮询 Redis LIST；现改由 signal_tower 通过 XREADGROUP 阻塞消费
        manual_mode:master_stream:{symbol} Stream，主号事件到达即镜像（延迟 ≤300ms），
        彻底消除固定 1s 轮询延迟。非手动模式下退化为 1s 低频检查模式切换。
        """
        logger.info("Manual mode loop started: %s", state.symbol)
        while self._running:
            try:
                if hasattr(self, "_manual_mode"):
                    # 手动镜像 ALWAYS-ON（独立于 signal_tower.mode）。
                    # 主号(MT5)手动事件到达即镜像为子号信号；无事件时 XREADGROUP 阻塞 300ms
                    # 返回 None，循环继续，开销可忽略。模型信号由 _produce_signal 独立产出，
                    # 两者互不干扰 → 实现「手动镜像」与「模型信号」并存。
                    master_trade = await self._manual_mode.get_master_trade(
                        state.symbol, block_ms=300,
                    )
                    if master_trade:
                        # [2026-07-24 ack 根因修复] finally 中无条件 XACK 每条已消费消息，
                        # 根治 XPENDING 堆积（原先仅在 publish 成功时 ack；去重/无效/失败路径
                        # 不 ack → 堆积 5787，重启 xgroup_setid("$") 抛历史 → 平仓镜像丢失）。
                        try:
                            result = await self._manual_mode.mirror_trade(
                                state.symbol, master_trade, self._signal_publisher,
                            )
                            if result:
                                logger.info(
                                    "Manual mirror (always-on): mirrored master trade for %s (sid=%s)",
                                    state.symbol, result.get("master_signal_id"),
                                )
                        finally:
                            _ack_id = master_trade.get("_msg_id")
                            if _ack_id:
                                await self._manual_mode.ack_master_trade(
                                    state.symbol, _ack_id,
                                )
                    # 事件驱动：立即回到 XREADGROUP 阻塞等待，无需 1s 轮询
                    continue
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Manual mode loop error (%s): %s", state.symbol, exc)
                await asyncio.sleep(1.0)

    async def _live_override_loop(self, state: SymbolState) -> None:
        """B4: Sub-bar live override rescue for adx_floor stale NO_TRADE.

        Runs every 5s. Active only when the last M5 bar-close signal was
        NO_TRADE due to adx_floor (engine's min_adx_for_trade gate). When
        live ADX rises above the floor and is sustained for 30s, emits a
        live_override signal via _produce_signal(live_override=True).

        Constraints:
        - Rate-limited: max 1 override per 60s (per symbol).
        - Sustained: live_adx >= floor for >= 30s (not noise).
        - Non-spamming: does NOT trigger if last signal was already a
          live_override (avoid retry loops).

        Why a separate loop: between M5 bar closes (5 min apart), ADX can
        cross 22 and stay there. Without this, the operator sees 3-5 min
        of "ADX 26.9 已激活" while the signal is still NO_TRADE blocked
        on the last bar's stale ADX=17.7. The rescue fires a real
        direction signal so the bridge can act.
        """
        logger.info("Live override loop started: %s (tf=%s)", state.symbol, state.timeframe)

        while self._running:
            # 参数全部 config 驱动(落库于 PG metadata + Redis)，热重载生效
            check_interval = self._live_override_check_interval_sec
            sustained_required_sec = self._live_override_sustained_sec
            rate_limit_sec = self._live_override_rate_limit_sec
            await asyncio.sleep(check_interval)
            try:
                # 总开关：关闭则不救援(沿用 P0-3 严格 floor 语义)
                if not self._live_override_enabled:
                    continue
                # Skip if not currently blocked by adx_floor
                if not state.last_signal_blocked_by_adx_floor:
                    continue
                # Rate limit
                if state.last_live_override_time > 0:
                    if time.time() - state.last_live_override_time < rate_limit_sec:
                        continue
                # Fetch live M5 klines (need >=20 bars for ADX 14)
                if self._db is None or not self._db.is_initialized:
                    continue
                klines = await self._fetch_klines(state.symbol, state.timeframe, limit=30)
                if len(klines) < 20:
                    continue
                closes = np.array([k["close"] for k in klines], dtype=np.float64)
                highs = np.array([k["high"] for k in klines], dtype=np.float64)
                lows = np.array([k["low"] for k in klines], dtype=np.float64)
                # Compute live ADX inline (returns tuple of 3 arrays)
                adx_arr, _, _ = self._indicator_calc.compute_adx(highs, lows, closes)
                live_adx = float(adx_arr[-1]) if len(adx_arr) > 0 else 0.0
                if live_adx < state.min_adx_for_trade:
                    state.adx_above_floor_since = 0.0
                    continue
                # Track sustained above floor
                now = time.time()
                if state.adx_above_floor_since == 0.0:
                    state.adx_above_floor_since = now
                if now - state.adx_above_floor_since < sustained_required_sec:
                    continue
                # Conditions met — emit live override signal (B 修复：
                # 把实时 ADX 传入，使 floor 判定用实时值，且绕过 per-bar guard)
                logger.info(
                    "Live override firing: %s live_adx=%.1f sustained=%.0fs",
                    state.symbol, live_adx, now - state.adx_above_floor_since,
                )
                await self._produce_signal(
                    state, live_override=True, live_adx_override=live_adx,
                )
                # ── B 修复(2026-07-20)：救援已触发一次，立即解除 arming 标志 ──
                # 原逻辑仅在"信号成功发布"(threshold_passed)的 L1335 复位该标志；
                # 但 override 信号若判 NEUTRAL/no-trade 会在 L958 提前 return，
                # 标志永不复位 → live override 循环每 5s 永久空转重产。
                # 现发射即复位：一次 adx_floor 拦截 = 一次救援；下一次真正
                # bar 收盘 adx_floor 拦截会重新 arming。
                state.last_signal_blocked_by_adx_floor = False
                state.adx_above_floor_since = 0.0
                state.last_live_override_time = time.time()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Live override error (%s): %s", state.symbol, exc)

    async def _live_score_publisher(self, state: SymbolState) -> None:
        """B 层: 每 3s 用实时 bar 重算评分快照并发布；C 层护栏下 bar 内即时触发成交。

        1. 实时评分快照: 用 A 层已实时化的 klines 计算 pre_score/direction/threshold，
           发布到 hcm:live:score:{symbol}_{tf} 供面板实时跳动（判断分随价格变动而变）。
        2. bar 内触发: 当实时 pre_score 稳定越过交易阈值且 direction 连续 N 周期同向时，
           调用 _produce_signal(live_override=True) —— 内部跑完整管线(含 zone/实时 bar)
           只在真越过门槛才发单，使「判断分过阈」在价格触及时即时成交而非等 5min bar 收盘。

        C 层护栏:
        - 方向稳定判据: direction 需连续 >=2 个周期(6s)同向才允许触发，过滤 bar 开盘跳变噪声。
        - 限流: 每符号 >=30s 最多触发一次，与 _live_override_loop 互不 spam。
        - 复用 _produce_signal 内部完整 floor+threshold 判定与滑点闸门(max_slip)，不绕过风控。
        """
        logger.info("Live score publisher started: %s (tf=%s)", state.symbol, state.timeframe)
        stable_dir: str = ""
        stable_count: int = 0
        # 【D 组 2026-08-03】限流状态与 _live_override_loop 共享
        # (state.last_live_override_time)，替代原局部 last_trigger_time：
        # 两条 live 触发路径原各自独立限流，可在同一窗口重复产单。
        trigger_rate_limit_sec: int = 30
        trigger_stable_required: int = 2
        while self._running:
            await asyncio.sleep(3)
            try:
                if self._db is None or not self._db.is_initialized:
                    continue
                klines = await self._fetch_klines(state.symbol, state.timeframe, limit=100)
                if len(klines) < 30:
                    continue
                closes = np.array([k["close"] for k in klines], dtype=np.float64)
                highs = np.array([k["high"] for k in klines], dtype=np.float64)
                lows = np.array([k["low"] for k in klines], dtype=np.float64)
                opens = np.array([k["open"] for k in klines], dtype=np.float64)
                indicators = self._indicator_calc.compute_all(closes, highs, lows, opens)
                # 实时 ADX 覆盖（与 floor 判定同源，避免面板/引擎分裂）
                live_adx = await self._fetch_live_adx(state)
                if live_adx is not None:
                    indicators.adx_14 = float(live_adx)
                # 市况分类（与 _produce_signal Step3 同口径）
                manual_score = await self._get_manual_regime_score(state.symbol)
                regime_result = self._regime_classifier.classify(
                    adx=indicators.adx_14,
                    adx_values=indicators.adx_values,
                    bbw=indicators.bbw,
                    bbw_ma20=indicators.bbw_ma20,
                    close=indicators.close,
                    recent_highs=indicators.recent_highs,
                    recent_lows=indicators.recent_lows,
                    pct_b=indicators.pct_b,
                    manual_regime_score=manual_score,
                    plus_di=indicators.plus_di,
                    minus_di=indicators.minus_di,
                    ma_alignment=indicators.ma_alignment,
                    hurst=_calc_hurst_for_regime(indicators.recent_closes),
                )
                # B 层保守: 不传 zone（zone_bonus 会让 pre_score 偏高），仅作触发探针；
                # 真正发单由 _produce_signal 完整管线(含 zone) 判定，不漏判也不误放。
                # 和乘幂激活时改走 hexp 独立管线（面板实时评分与真实下单闸门同口径，
                # 否则面板停留在 co_source 口径会误导"已过门槛"）；live=True 时引擎
                # 额外发布 hcm:live:hexp:{symbol} 完整快照（TTL15s）。
                _live_model = await self._detect_active_model()
                if _live_model == "hexp":
                    score_result = await self._hexp_engine.produce(
                        state.symbol, indicators, regime_result, live=True,
                    )
                else:
                    score_result = self._scoring_engine.compute_pre_score(
                        indicators, regime_result, live_adx=indicators.adx_14,
                    )
                    # 【2026-08-28 co_source 清除】此处原为 co_source.apply(v1) 施加
                    # F1–F5 过滤 + 校准因子 + 自适应门槛；双源模式整体下线后，
                    # 实时评分快照直接沿用 scoring_engine 结论（co_source 模式已不存在，
                    # 面板口径与真实下单闸门天然一致——唯一闸门就是 HEXP）。
                # 发布实时评分快照（面板实时跳动）
                if self._redis is not None and self._redis.is_initialized:
                    import json as _json
                    await self._redis.set(
                        f"hcm:live:score:{state.symbol}_{state.timeframe}",
                        _json.dumps({
                            "pre_score": round(score_result.pre_score, 4),
                            "direction": score_result.direction,
                            "threshold": round(score_result.threshold, 4),
                            "threshold_passed": bool(score_result.threshold_passed),
                            "close": float(closes[-1]),
                            "adx": float(indicators.adx_14),
                            "ts": time.time(),
                        }),
                        ex=15,
                    )
                # ── C 层护栏: 边沿触发 + 单棒单发 + 方向稳定 + 限流 ──
                # 【2026-08-10 精确触发根治】原逻辑为「电平触发」：threshold_passed 只要
                # 持续为真，每过 trigger_rate_limit_sec 就再发一条 —— 同一波行情被反复
                # 当成新信号（实测 115 条/小时）。现要求必须是【新的上升沿】且【本根 K 线
                # 尚未触发过】才允许发单，把「持续过阈」压缩为「一次事件一单」。
                if not self._live_override_enabled:
                    stable_dir, stable_count = "", 0
                    state.live_edge_armed = True
                    continue
                _cur_bar = klines[-1].get("open_time") if klines else None
                if score_result.direction in ("BUY", "SELL"):
                    if score_result.direction == stable_dir:
                        stable_count += 1
                    else:
                        # 方向翻转 = 全新事件，重新武装
                        stable_dir, stable_count = score_result.direction, 1
                        state.live_edge_armed = True
                    if not score_result.threshold_passed:
                        # 分值回落到门槛下方 → 重新武装，等待下一次真实上升沿
                        state.live_edge_armed = True
                    elif not state.live_edge_armed:
                        pass  # 分值持续在阈上 = 同一事件延续，不重复产单
                    elif _cur_bar is not None and state.last_live_trigger_bar == _cur_bar:
                        pass  # 同一根 K 线已触发过，单棒单发
                    elif stable_count < trigger_stable_required:
                        pass  # 方向尚未稳定
                    elif time.time() - state.last_live_override_time < trigger_rate_limit_sec:
                        pass  # 限流窗口内
                    else:
                        logger.info(
                            "Live score trigger(edge): %s dir=%s pre=%.3f thr=%.3f stable=%d bar=%s",
                            state.symbol, score_result.direction,
                            score_result.pre_score, score_result.threshold,
                            stable_count, _cur_bar,
                        )
                        await self._produce_signal(
                            state, live_override=True, live_adx_override=indicators.adx_14,
                        )
                        # 【D 组】写共享限流状态：本次触发对 _live_override_loop 同样生效
                        state.last_live_override_time = time.time()
                        # 落位武装 + 记录本棒，杜绝同一事件/同一根 K 线重复触发
                        state.live_edge_armed = False
                        state.last_live_trigger_bar = _cur_bar
                        stable_dir, stable_count = "", 0
                else:
                    stable_dir, stable_count = "", 0
                    # 方向消失（NO_TRADE）→ 重新武装
                    state.live_edge_armed = True
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Live score publisher error (%s): %s", state.symbol, exc)

    async def _live_adx_publisher(self, state: SymbolState) -> None:
        """Publish live ADX to Redis every 5s — single source of truth.

        Computed by IndicatorCalculator on the last 30 M5 klines from PG
        (fed by MT5 bridge). Consumers read `hcm:live:adx:{symbol}_{tf}`
        instead of duplicating ADX logic.
        """
        key: str = f"hcm:live:adx:{state.symbol}_{state.timeframe}"
        logger.info("Live ADX publisher started: %s (tf=%s)", state.symbol, state.timeframe)
        while self._running:
            await asyncio.sleep(5)
            try:
                if self._db is None or not self._db.is_initialized:
                    continue
                klines = await self._fetch_klines(state.symbol, state.timeframe, limit=30)
                if len(klines) < 20:
                    continue
                closes = np.array([k["close"] for k in klines], dtype=np.float64)
                highs = np.array([k["high"] for k in klines], dtype=np.float64)
                lows = np.array([k["low"] for k in klines], dtype=np.float64)
                adx_arr, plus_di_arr, minus_di_arr = self._indicator_calc.compute_adx(highs, lows, closes)
                live_adx: float = float(adx_arr[-1]) if len(adx_arr) > 0 else 20.0
                live_plus_di: float = float(plus_di_arr[-1]) if len(plus_di_arr) > 0 else 25.0
                live_minus_di: float = float(minus_di_arr[-1]) if len(minus_di_arr) > 0 else 25.0
                # Publish to Redis (TTL 30s so stale values expire)
                if self._redis is not None:
                    import json as _json
                    payload = _json.dumps({
                        "raw_value": round(live_adx, 2),
                        "plus_di": round(live_plus_di, 2),
                        "minus_di": round(live_minus_di, 2),
                        "ts": time.time(),
                    })
                    await self._redis.set(key, payload, ex=30)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Live ADX publisher error (%s): %s", state.symbol, exc)

    async def _fetch_live_adx(self, state: SymbolState) -> Optional[float]:
        """T3a: 从 Redis 读取实时 ADX（hcm:live:adx:{symbol}_{tf}）用于 floor 判定。

        与面板顶部显示的实时 ADX 同源，消除「bar-close ADX 卡 floor vs
        面板实时 ADX 已激活」的分裂。读取失败/缺失时返回 None，由调用方
        回退到 bar-close ADX（绝不致信号流程中断）。
        """
        if self._redis is None:
            return None
        try:
            import json as _json
            raw = await self._redis.get(
                f"hcm:live:adx:{state.symbol}_{state.timeframe}"
            )
            if not raw:
                return None
            data = _json.loads(raw)
            val = data.get("raw_value")
            return float(val) if val is not None else None
        except Exception as exc:
            logger.debug("T3a live_adx fetch failed (%s): %s", state.symbol, exc)
            return None

    # ── Bar Close Detection ─────────────────────

    async def _wait_bar_close(
        self, state: SymbolState, tf_seconds: int,
        timeframe_override: Optional[str] = None,
        state_attr: str = "last_bar_open_time",
    ) -> bool:
        """Wait for the current bar to close.

        Strategy: poll PG for latest kline, detect when open_time changes.
        Sleep-based polling with adaptive interval.

        Args:
            state: SymbolState.
            tf_seconds: Timeframe duration in seconds.

        Returns:
            True if a new bar closed.
        """
        if self._db is None or not self._db.is_initialized:
            await asyncio.sleep(DEFAULT_IDLE_SLEEP)
            return False

        try:
            # Get latest kline
            tf = timeframe_override or state.timeframe
            rows = await self._db.fetch(
                """SELECT open_time, close, high, low
                   FROM hcm_market.klines
                   WHERE symbol=$1 AND time_frame=$2 AND open_time <= now()
                   ORDER BY open_time DESC LIMIT 1""",
                state.symbol, tf,
            )

            if not rows:
                state.kline_not_ready_count += 1
                await asyncio.sleep(DEFAULT_IDLE_SLEEP)
                return False

            latest_open = rows[0]["open_time"]

            # Check if this is a new bar
            last_bar = getattr(state, state_attr, None)
            if last_bar is None:
                setattr(state, state_attr, latest_open)
                state.kline_not_ready_count = 0
                await asyncio.sleep(1.0)
                return False

            if latest_open == last_bar:
                # Same bar — wait
                now = datetime.now(timezone.utc)
                seconds_in_bar = (now - latest_open.replace(tzinfo=timezone.utc)).total_seconds()
                if seconds_in_bar < tf_seconds:
                    wait_time = tf_seconds - seconds_in_bar + 0.5
                    if wait_time > 0.5:
                        await asyncio.sleep(min(wait_time, 5.0))
                else:
                    if seconds_in_bar > tf_seconds + 30:
                        # P0 fix (2026-07-15): previously this branch returned
                        # True with NO sleep. The caller (_symbol_loop) then
                        # re-entered _wait_bar_close immediately on the SAME
                        # stuck bar, force-advancing again → tight busy-poll
                        # that re-published a NO_TRADE every ~50ms (self-DoS,
                        # ~19 writes/s, drowned real signals and hammered PG).
                        # Now we distinguish mild vs. severe staleness:
                        if seconds_in_bar > self._kline_stale_threshold_sec:
                            # Genuinely broken feed: no real new bar exists.
                            # Pause production (do NOT force-advance) and alert,
                            # backing off so we stop querying/hammering PG.
                            logger.error(
                                "ALERT kline feed STALE for %s/%s: latest bar "
                                "open_time unmoved for %.0fs (>= threshold %ds). "
                                "Pausing signal production until feed recovers.",
                                state.symbol, tf, seconds_in_bar,
                                self._kline_stale_threshold_sec,
                            )
                            await asyncio.sleep(min(tf_seconds, 30))
                            return False
                        # Mild delay (late but plausibly live bar): allow one
                        # force-advance, but RATE-LIMIT it. The per-bar
                        # idempotency guard in _produce_signal suppresses any
                        # duplicate writes for this same stuck bar.
                        logger.warning(
                            "Bar close delayed for %s/%s (stuck %.0fs), forcing advance",
                            state.symbol, tf, seconds_in_bar,
                        )
                        setattr(state, state_attr, latest_open)
                        await asyncio.sleep(min(tf_seconds, 30))
                        return True
                    await asyncio.sleep(1.0)
                return False

            # New bar detected!
            setattr(state, state_attr, latest_open)
            state.kline_not_ready_count = 0
            logger.debug("Bar closed: %s %s → %s", state.symbol, tf, latest_open)
            return True

        except Exception as exc:
            logger.warning("Kline query failed for %s: %s", state.symbol, exc)
            state.kline_not_ready_count += 1
            await asyncio.sleep(DEFAULT_LOOP_ERROR_SLEEP)
            return False

    # ── Signal Production Pipeline ──────────────

    async def _get_live_entry_price(self, symbol: str, direction: str) -> Optional[float]:
        """读取桥实时播报的最新 tick 价，作为信号 entry_price 的优先来源。

        根因：原 entry_price 用 M5 棒收盘价（indicators.close）。信号经
        塔→stream→风控→桥 有处理延迟，行情快速移动时桥执行时实时价已漂离棒收盘价，
        触发桥滑点闸门（max_slip≈ATR×0.3）拒单 → 表现「出信号却不下单」。

        本方法读取 mt5_bridge.write_price_to_redis 持续写入的
        hcm:config:v2[market:latest:{symbol}] = {"bid","ask","last",...}（桥每 tick
        实时更新），按方向取同盘口价（BUY→ask, SELL→bid，last>0 优先用 last），
        使 entry_price 贴近桥实际下单价，根除入场价滞后导致的拒单。

        缺失/解析失败返回 None，由调用方回退到 indicators.close（绝不中断信号流程）。
        """
        if self._redis is None:
            return None
        try:
            import json as _json
            raw = await self._redis.hget("hcm:config:v2", f"market:latest:{symbol}")
            if not raw:
                return None
            data = _json.loads(raw)
            last = float(data.get("last") or 0.0)
            ask = float(data.get("ask") or 0.0)
            bid = float(data.get("bid") or 0.0)
            if last > 0:
                return last
            if direction == "BUY" and ask > 0:
                return ask
            if direction == "SELL" and bid > 0:
                return bid
            return ask if ask > 0 else (bid if bid > 0 else None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Live entry price read failed for %s: %s", symbol, exc)
            return None

    async def _produce_signal(
        self,
        state: SymbolState,
        live_override: bool = False,
        live_adx_override: Optional[float] = None,
    ) -> None:
        """Execute the full signal production pipeline.

        Steps:
          0. Market circuit check (Fix #5)
          1. Fetch K-lines + Redis live bar merge (Fix #4)
          2. Compute indicators (with ATR, bar_open)
          3. Regime classification
          4. Bar confirmation (Fix #1)
          5. Scoring (with bar_momentum Fix #2)
          6. Cooldown check
          7. AI invocation + AI Fallback Gate (Fix #3)
          8. Publish

        Args:
            state: SymbolState for current symbol.
        """
        trace_id = uuid.uuid4().hex[:12]
        logger.info("Signal production: symbol=%s, trace_id=%s", state.symbol, trace_id)

        # ── AI 手数分档透传初始化（链动风控面板动态手数，2026-08-14 需求）──
        # ai_lot_tier: "none"(默认，纯 HEXP 不干预手数) / "low" / "mid" / "high"
        # suggested_lot: 实际下单手数倍率基底，由 DeepSeek/co_source G3 决定；
        #   此处初始化为 1.0，防止 AI block 早期 return 时引用未绑定变量。
        ai_lot_tier: str = "none"
        suggested_lot: float = 1.0

        # ── P2c: suppress-reason aggregation ──
        # Each gating layer records its reason with a priority so the final
        # published reason is the single most-significant cause (highest
        # priority wins), with the full chain retained for diagnostics.
        # Priority: structure_conflict(40) > ai_veto(30) > cooldown(20) > threshold(10)
        suppress_chain: list[tuple[int, str]] = []

        # ── 手动镜像已由 _manual_mode_loop(ALWAYS-ON) 实时事件驱动处理，
        #    此处不再因 mode=manual 而短路 AI 信号生产——保证「手动镜像」与「模型信号」并存。
        #    signal_tower.mode 现在只影响模型路由/提示词，不再抑制信号产出。

        # ── P0 fix (2026-07-15): per-bar idempotency guard ──
        # When _wait_bar_close force-advances on a stale feed, _produce_signal
        # can be re-entered on the SAME bar. Without this guard it re-runs the
        # full pipeline and re-publishes a NO_TRADE every loop (self-DoS). Once
        # a bar's open_time has been produced, subsequent force-advances on that
        # identical bar become no-ops — including skipping the kline fetch.
        # B 修复: live_override 救援在「同一根 M5 bar 内」触发(距上次收盘不足
        # 5 分钟)，必须绕过本 guard 才能发新信号——其自身已有 sustained(30s)+
        # rate_limit(60s)+last_signal_blocked_by_adx_floor 三重限流，不会 spam。
        if not live_override:
            current_bar_open = getattr(state, "last_bar_open_time", None)
            guard_key = f"{state.symbol}:{state.timeframe}"
            # D3 (2026-08-04): 幂等闸门外移到 Redis（跨重启持久），内存兜底。
            # 原纯内存 dict 在信号塔重启后丢失 → 同 bar 重复生产。现在 Redis 作为
            # 跨重启真源（TTL 24h），内存仍保留作 Redis 瞬断时对照。
            guard_redis_key = f"signal_tower:last_bar:{state.symbol}:{state.timeframe}"
            last_produced: Any = self._last_produced_open_time.get(guard_key)
            if self._redis is not None:
                try:
                    _rp = await self._redis.get(guard_redis_key)
                    if _rp:
                        last_produced = datetime.fromisoformat(_rp)
                except (ValueError, TypeError, Exception):
                    pass
            if (
                last_produced is not None
                and current_bar_open is not None
                and last_produced == current_bar_open
            ):
                logger.debug(
                    "Skip duplicate production: %s/%s open_time=%s already produced "
                    "(stale-feed guard)",
                    state.symbol, state.timeframe, current_bar_open,
                )
                return
            if current_bar_open is not None:
                self._last_produced_open_time[guard_key] = current_bar_open
                if self._redis is not None:
                    try:
                        await self._redis.set(
                            guard_redis_key,
                            current_bar_open.isoformat(),
                            ex=86400,
                        )
                    except Exception:
                        pass
        else:
            logger.debug(
                "Live override production: %s/%s (idempotency guard bypassed)",
                state.symbol, state.timeframe,
            )

        # ── Signal production heartbeat — pipeline uses this to determine engine health
        if self._redis is not None and self._redis.is_initialized:
            try:
                await self._redis.set(
                    f"hcm:signal_tower:last_production:{state.symbol}:{state.timeframe}",
                    datetime.now(timezone.utc).isoformat(),
                    ex=300,  # TTL 5 min
                )
            except Exception:
                pass

        # ── Step 1: Fetch K-line data ───────────
        t0 = time.time()
        klines = await self._fetch_klines(state.symbol, state.timeframe)
        # ── Bar 质量分日志（验证用, DEBUG 级, 不影响信号）──
        if klines:
            _lb = klines[-1]
            # Tier2: 调试日志也对齐到 F6 实际使用的 bar（正在形成的实时 bar 时回退收盘 bar）
            if _lb.get("is_live_forming") and len(klines) >= 2:
                _lb = klines[-2]
            logger.debug(
                "Bar quality %s/%s latest: quality=%.3f vol_q=%.2f spread_q=%.2f "
                "body_ratio=%.2f pin=%.2f outlier=%s",
                state.symbol, state.timeframe,
                _lb.get("quality", 1.0), _lb.get("vol_q", 1.0),
                _lb.get("spread_q", 1.0), _lb.get("body_ratio", 0.0),
                _lb.get("pin", 0.0), _lb.get("outlier", False),
            )
        if len(klines) < 30:
            logger.warning("Insufficient kline data: %s (%d bars)", state.symbol, len(klines))
            return

        closes = np.array([k["close"] for k in klines], dtype=np.float64)
        highs = np.array([k["high"] for k in klines], dtype=np.float64)
        lows = np.array([k["low"] for k in klines], dtype=np.float64)
        opens = np.array([k["open"] for k in klines], dtype=np.float64)

        if self._watchdog:
            await self._watchdog.report_step("kline", time.time() - t0)

        # ── Step 1: Compute Indicators ──────────
        t0 = time.time()
        indicators = self._indicator_calc.compute_all(closes, highs, lows, opens)

        # ── P0/P1: H1 多周期上下文（HMTS 状态判定层）──
        # 拉取 H1 klines（复用 _fetch_klines，失败/不足则 h1_context=None 安全退化到 M5 only）。
        h1_context: Optional[H1Context] = None
        try:
            h1_klines = await self._fetch_klines(state.symbol, "H1", limit=200)
            if len(h1_klines) >= 50:
                h1_closes = np.array([k["close"] for k in h1_klines], dtype=np.float64)
                h1_highs = np.array([k["high"] for k in h1_klines], dtype=np.float64)
                h1_lows = np.array([k["low"] for k in h1_klines], dtype=np.float64)
                h1_opens = np.array([k["open"] for k in h1_klines], dtype=np.float64)
                h1_ind = self._indicator_calc.compute_all(
                    h1_closes, h1_highs, h1_lows, h1_opens
                )
                h1_context = self._h1_classifier.classify(
                    h1_ind, symbol=state.symbol, m5_recent_closes=closes.tolist(),
                )
                # 写回 indicators 供发布/归因落库
                indicators.h1_regime = h1_context.regime if h1_context else None
                indicators.h1_trend_direction = (
                    h1_context.trend_direction if h1_context else ""
                )
                indicators.h1_trend_strength = (
                    h1_context.trend_strength if h1_context else 0.0
                )
                indicators.h1_adx = h1_context.adx if h1_context else 0.0
                logger.debug(
                    "H1 context (%s): regime=%s dir=%s strength=%.2f adx=%.1f",
                    state.symbol,
                    indicators.h1_regime,
                    indicators.h1_trend_direction,
                    indicators.h1_trend_strength,
                    indicators.h1_adx,
                )
        except Exception as exc:
            logger.debug("H1 context compute skipped (%s): %s", state.symbol, exc)
            h1_context = None

        # B 修复: live_override 救援时用实时 ADX 覆盖 indicators.adx_14，
        # 使 floor 判定与面板 live_adx_14 同源一致，且发布信号里的 adx_14
        # 也反映实时值(避免面板/引擎 ADX 显示不同步)。
        if live_adx_override is not None:
            indicators.adx_14 = float(live_adx_override)
            logger.info(
                "Live override: using live_adx=%.2f for floor check (%s/%s)",
                indicators.adx_14, state.symbol, state.timeframe,
            )

        # ── T3a: 实时 ADX 同步（所有信号路径）──
        # bar-close 路径原本用 bar-close ADX(indicators.adx_14) 做 floor，
        # 而面板顶部显示的是实时 ADX(hcm:live:adx)，二者在 5 分钟 bar 间隙内
        # 会分裂（面板显示 ADX 已激活，引擎却用上次收盘 ADX 卡 floor）。
        # 现统一：floor 判定优先用实时 ADX，与 live_override 救援路径同源；
        # 取不到实时值时安全回退 bar-close ADX（绝不致流程中断）。
        adx_for_floor = live_adx_override
        if adx_for_floor is None:
            adx_for_floor = await self._fetch_live_adx(state)
            if adx_for_floor is not None:
                logger.info(
                    "T3a: using live_adx=%.2f for floor check (bar-close path, %s/%s)",
                    adx_for_floor, state.symbol, state.timeframe,
                )

        # ── P0 (2026-07-15): confluence zone for precise entry (informational) ──
        zone_level, zone_type, zone_strength = await self._compute_entry_zone(
            state, indicators.close
        )

        if self._watchdog:
            await self._watchdog.report_step("indicator", time.time() - t0, timeout_seconds=5.0)

        # ── Step 3: Regime Classification ───────
        t0 = time.time()
        manual_score = await self._get_manual_regime_score(state.symbol)
        regime_result = self._regime_classifier.classify(
            adx=indicators.adx_14,
            adx_values=indicators.adx_values,
            bbw=indicators.bbw,
            bbw_ma20=indicators.bbw_ma20,
            close=indicators.close,
            recent_highs=indicators.recent_highs,
            recent_lows=indicators.recent_lows,
            pct_b=indicators.pct_b,
            manual_regime_score=manual_score,
            plus_di=indicators.plus_di,
            minus_di=indicators.minus_di,
            ma_alignment=indicators.ma_alignment,
            hurst=_calc_hurst_for_regime(indicators.recent_closes),
        )

        # ── 提前解析 active_model 与机制配置（供 Step4 引擎选择 + bypass + G3 覆盖复用）──
        active_model = await self._detect_active_model()
        profile = self._mechanism_profile(active_model)

        # ── Step 4: Scoring (Tier 2: zone synergy fed into scoring engine) ──
        # 引擎按机制配置表选择（当前各机制共用 _scoring_engine；新机制只需在 MECHANISM_PROFILES 登记）
        if active_model == "hexp":
            # 和乘幂独立信号源：异步多周期管线（内部自拉 H1/H4/D1/M1 K 线），
            # 输出 ScoreResult 契约 + hexp 元数据（grade/hp_score/k/period_states/…）。
            # 下游 Step4b co_source.apply 对非 co_source 模型原样透传，无需特判。
            score_result = await self._hexp_engine.produce(
                state.symbol, indicators, regime_result, live=False,
                zone_level=zone_level, zone_type=zone_type, zone_strength=zone_strength,
            )
        else:
            _engine = getattr(self, profile["engine"], self._scoring_engine)
            score_result = _engine.compute_pre_score(
                indicators, regime_result,
                zone_level=zone_level, zone_type=zone_type, zone_strength=zone_strength,
                live_adx=adx_for_floor,
                h1_context=h1_context,
                signal_production=True,
            )
        # 2026-08-05 (D9-1): 阶段标记——评分完成。异常时此日志为最后一个成功阶段。
        logger.info(
            "Stage scoring_done: %s dir=%s pre=%.3f passed=%s regime=%s",
            state.symbol, score_result.direction, score_result.pre_score,
            score_result.threshold_passed, regime_result.regime.value,
        )

        # ── Step 4b: 共源信号增强（Phase 1a）──
        # 仅当 signal.active_model == "co_source" 时 CoSourceEngine.apply() 才会施
        # 加 F1–F5 过滤 + 校准因子 + 自适应门槛；默认模型下 apply() 原样返回，
        # 默认评分引擎字节级不变（约束 ③ backward-compatible fallback）。
        # risk_level / event_window / consecutive_losses 来自 Redis 风险态（P1a 缺省
        # low/False/0，P2 批量 AI 写入后自动生效）。
        risk_level, event_window, consec_loss = await self._read_co_risk_state()
        # [2026-07-30 C 组] 把最新棒的质量分 / 点差质量分透传给共源 F6 闸门
        # （klines[-1] 经 compute_bar_quality 已含 quality / spread_q）
        # Tier2: 若最后一根是"正在形成的实时 bar"(由 _fetch_klines 合并时打
        # is_live_forming 标记)，则 F6 质量闸门与调试日志对齐到上一根"已收盘
        # 完整 bar"，避免对不完整 bar 评质量(spread/volume 部分数据)导致信号被误杀；
        # 实时 bar 仍保留在指标数组(closes/highs/...)中提供实时性(B 修复意图)。
        last_k = klines[-1] if klines else {}
        if last_k.get("is_live_forming") and len(klines) >= 2:
            last_k = klines[-2]
            logger.debug(
                "Tier2: last bar is live-forming; F6 quality gate uses closed bar "
                "(quality=%.3f spread_q=%.2f)",
                last_k.get("quality", 1.0), last_k.get("spread_q", 1.0),
            )
        _bar_quality = float(last_k.get("quality", 1.0))
        _spread_q = float(last_k.get("spread_q", 1.0))

        # 【2026-08-28 co_source 清除】双源信号模式(co_source v1 apply / v2 apply_v2
        # 收敛决策)整体下线，系统只保留 HEXP 引擎。此处原为 co_source.apply(v1) +
        # apply_v2(v2) 两段调用，现整体移除；score_result 直接沿用上游结论
        # （active_model="hexp" 时为 HexpEngine.produce() 产出，否则为默认评分引擎）。
        # 注意：_compute_v2_inputs 必须保留 —— 其产出的 _ms/_eq/_theta 被下方 HEXP
        # 入场闸门(1961-2013)直接使用，与 co_source 本体无关（micro_state/precision_entry
        # 是独立模块，仅配置键曾复用 co.v2.* 命名，已迁至 hexp.entry.*）。
        _ms, _eq, _theta, _brk = await self._compute_v2_inputs(
            state, indicators, regime_result, h1_context, score_result)
        # 2026-08-05 (D9-1): 阶段标记——共源闸门已施加(F1–F5/校准因子/自适应门槛)。
        logger.info(
            "Stage co_gate_applied: %s dir=%s pre=%.3f thr=%.3f passed=%s band=%s",
            state.symbol, score_result.direction, score_result.pre_score,
            score_result.threshold, score_result.threshold_passed,
            getattr(score_result, "co_band", "?"),
        )

        # ── hexp 入场时机裁决（2026-08-26：micro_state/precision_entry 在 hexp 模式参与）──
        # 目标：避免"趋势衰竭（TREND_EXHAUST）高位/低位追单"。micro_state 判 TREND_EXHAUST
        # （ADX 回落 + 顶/底背离 + MACD 衰减）时，hexp 顺势方向单被否决（NO_TRADE）。
        # 只拦 TREND_EXHAUST（衰竭末端，追单必亏），不拦 TREND_ACCEL（加速中段仍有空间）、
        # 不拦 TREND_PULLBACK（回踩核心买点）。_ms/_eq/_theta 由 _compute_v2_inputs 复用，
        # 无额外计算开销。开关 hexp.entry_gate_enabled（默认 False），可 config_provider 热回退。
        if active_model == "hexp" and _ms is not None:
            _eg_enabled = False
            if self._config is not None:
                try:
                    _eg_enabled = bool(await self._config.get_bool("hexp.entry_gate_enabled", False))
                except Exception:
                    _eg_enabled = False
            if _eg_enabled:
                _ms_state = getattr(_ms, "state", None)
                _ms_dir = getattr(_ms, "direction", "") or ""
                _ms_dir_map = {"UP": "BUY", "DOWN": "SELL"}
                _cur_dir = score_result.direction
                # 精准点位闸门（precision_entry 三维买点分：R:R 位置 + 方向对齐）独立开关，
                # 默认 False=向后兼容；接入后对 hexp 已有方向的信号做"点位质量否决"。
                # 1) 衰竭否决：趋势末端追单必亏（保留，防接刀）
                # 2026-08-27 方案A：移除原 "1b) 方向对齐否决"（micro_state 反向即否决 hexp
                # passed 信号）。该闸门用 micro_state 单维否决 6 维综合已 passed 的信号，
                # 与"放行严格回到 scorecard_total"冲突；micro_state 仅作方向补强（见 #2），
                # 不作为否决闸门。故彻底删除 1b 分支及 _pg_enabled 读取。
                if _cur_dir in ("BUY", "SELL") and _ms_state == MicroState.TREND_EXHAUST:
                    score_result.direction = "NO_TRADE"
                    score_result.threshold_passed = False
                    if not score_result.fallback_reason:
                        score_result.fallback_reason = (
                            "hexp_entry_gate(TREND_EXHAUST 衰竭末端不追)"
                        )
                    logger.info(
                        "hexp entry gate BLOCK %s: dir 被否决, micro_state=TREND_EXHAUST "
                        "(衰竭末端不追单)",
                        state.symbol)
                # 2) 方案3：micro_state 直接参与方向 —— 回踩/反转给出明确趋势方向、
                #    而 hexp 因滞后组未确认仍 NO_TRADE 时，用 micro_state 方向补方向，
                #    并以精准买点分 entry_quality >= 自适应门槛 θ 保证质量（不无脑放行）。
                elif (_cur_dir == "NO_TRADE" and _ms_dir in _ms_dir_map
                      and _ms_state in (MicroState.REVERSAL, MicroState.TREND_PULLBACK)):
                    _new_dir = _ms_dir_map[_ms_dir]
                    if float(_eq) >= float(_theta):
                        score_result.direction = _new_dir
                        # 【2026-08-28 P1-6】"补方向后强制过闸"改为可热关（默认 True =
                        # 保持现有生产行为，不擅自改变信号输出）。
                        #
                        # 风险说明：本分支由 micro_state 单维状态 + 买点分 _eq>=θ
                        # 造出方向，并直接置 threshold_passed=True，绕过了 hexp 的
                        # 6 维综合分（scorecard_total）闸门；且本段**先于** AI 闸门
                        # 执行，AI 随后看到 passed=True 即可合法 UPGRADE，形成
                        # "micro_state 造方向 + AI 升级"的非 hexp 放行链，与铁律 5.1
                        # 「辅助模块无独立开仓权」的精神冲突。
                        #
                        # 保持默认 True 以不动既有策略；运维可经配置中心把
                        # hexp.entry_gate_force_pass 置为 false 止血：届时仅补方向、
                        # 不再强制过闸，交由 6 维综合分正常裁决。
                        _force_pass = True
                        if self._config is not None:
                            try:
                                _force_pass = bool(await self._config.get_bool(
                                    "hexp.entry_gate_force_pass", True))
                            except Exception:
                                _force_pass = True
                        score_result.threshold_passed = _force_pass
                        score_result.pre_score = round(float(_eq), 4)
                        if not score_result.fallback_reason:
                            score_result.fallback_reason = (
                                f"hexp_entry_gate({_ms_state.value} 补方向 {_new_dir})"
                            )
                        logger.info(
                            "hexp entry gate FILL %s: micro_state=%s dir=%s eq=%.3f>=θ=%.3f "
                            "→ 补方向 %s",
                            state.symbol, _ms_state.value, _ms_dir, _eq, _theta, _new_dir)
                    else:
                        logger.info(
                            "hexp entry gate SKIP %s: micro_state=%s dir=%s eq=%.3f<θ=%.3f "
                            "(买点分不足，不补方向)",
                            state.symbol, _ms_state.value, _ms_dir, _eq, _theta)

        # P2: always expose component breakdown + real raw indicators to dashboard
        await self._publish_component_scores(state, indicators, regime_result, score_result)
        await self._publish_raw_indicators(state, indicators)
        # P0: 发布 H1 上下文到 Redis 供面板观察 HMTS 状态判定层
        await self._publish_h1_context(state, h1_context)

        # 【2026-08-28 co_source 清除】原「Phase 0 微观状态机 + 精准买点分 shadow 对比」
        # （_run_shadow_v2）是 co_source v2 的影子采集通道，依赖 co_source.apply 产出的
        # _legacy 作为 "old" 端对照。双源模式整体下线后该对照无意义，整体移除。
        # 注意：micro_state/precision_entry 本身保留（HEXP 入场闸门仍直接使用，见上方
        # hexp entry gate 段），此处删除的只是 v2 影子落样通道。

        # ── Hexp 影子模式：双跑和乘幂、落库不下单（验证准确率，零实盘影响）──
        await self._run_shadow_hexp(
            state, indicators, regime_result, h1_context, score_result,
            zone_level=zone_level, active_model=active_model,
        )

        # P3 FORCE_CLOSE: 每次 M5 bar 检查 H1 趋势是否翻转
        if h1_context is not None and hasattr(self, "_force_close"):
            fc_result = await self._force_close.check_and_publish(
                state.symbol, h1_context,
                h1_adx=getattr(h1_context, "adx", None),
            )
            if fc_result:
                logger.warning("FORCE_CLOSE fired: %s — %s",
                               state.symbol, fc_result.get("reason", ""))

        # ── 闭环：AI 融合闸门（前移·2026-08-14 修复）──
        # 原逻辑在 L2136（本函数末尾）才评估 AI，但 hexp 99% 信号 threshold_passed=False
        # 在更早处 return，导致 AI 闸门形同虚设。现前移：即使 hexp 未放行，只要给了
        # 明确方向(BUY/SELL)，也送 AI 裁决——AI 高分可"打开"该信号（双信号融合赋能）。
        ai_q = await self._read_ai_quality(state.symbol)
        logger.info("AI gate probe: symbol=%s ai_q_is_none=%s c_ai=%s",
                    state.symbol, ai_q is None, (ai_q or {}).get("c_ai"))
        if ai_q is not None and ai_q.get("c_ai") is not None:
            # 注：final_direction 在 L1847 才赋值，此处用 score_result.direction
            _direction = getattr(score_result, "direction", "NO_TRADE") or "NO_TRADE"
            _snap = {
                "hp_score": getattr(score_result, "hp_score", 0.0) or 0.0,
                # 2026-08-27：补 scorecard_total 进快照，供 quality_gate 手数链动使用
                # 6 维综合分（稳定），替代 hp_score 强度单维决定动态手数档。
                "scorecard_total": float(getattr(score_result, "scorecard_total", 0.0) or 0.0),
                "k": getattr(score_result, "k_value", 1.0) or 1.0,
                "grade": getattr(score_result, "grade", "C") or "C",
                "passed": bool(score_result.threshold_passed),
                "direction": _direction,
            }
            _ai_cfg = await self._ai_cfg_dict()
            # 【阶段 1·方向共振】把 sidecar 的 dir_lm 传给 gate，与 _snap.direction(dir_hexp)共振。
            # 【阶段 2·买点共振】把 sidecar 的 entry_lm(ai_entry 好买点概率)传给 gate，
            # 与 hexp entry_quality 共振(增强好点位/否决差点位)。两者默认 None → 不共振。
            # 取值来自 ai_q（_read_ai_quality 随 fusion 返回），不可用同名局部变量
            # （那是另一方法的局部作用域 → NameError）。
            _decision = ai_quality_decide(
                _snap, ai_q["c_ai"], _ai_cfg, c_ai_meta=ai_q,
                ai_direction=ai_q.get("ai_direction"),
                ai_dir_prob=ai_q.get("ai_dir_prob"),
                ai_entry=ai_q.get("ai_entry"),
            )
            self._ai_quality_last[state.symbol] = _decision
            # 2026-08-18 变更：AI 闸门决策暂存，待 signal_id 生成后（L2314 之后）补记真实
            # signal_id 落库 hcm_ai.gate_decision。VETO/被拦信号在下方 return 不再发布，
            # 其 signal_id 永远为 None（且不成交，无需归因盈亏），故仅暂存"会发布"的路径。
            self._pending_ai_gate[state.symbol] = (_snap, _decision, _ai_cfg)
            _q_action = _decision.get("action", "HOLD")
            # AI 手数分档（low/mid/high/none）→ 链动风控面板动态手数（2026-08-14 需求）
            _q_tier = str(_decision.get("lot_tier", "none") or "none")
            logger.info(
                "AI quality gate %s: action=%s c_ai=%.2f (lm=%.1f ds=%.1f src=%s) "
                "hexp_passed=%s hexp_grade=%s → final_grade=%s lot_tier=%s",
                state.symbol, _q_action, ai_q["c_ai"],
                ai_q.get("lm_score"), ai_q.get("ds_score"), ai_q.get("source"),
                _snap["passed"], _snap["grade"], _decision.get("final_grade"), _q_tier,
            )
            if _q_action == "VETO":
                self._stats["ai_gate_rejects"] += 1
                logger.info(
                    "AI quality gate VETO: %s %s c_ai=%.2f — signal suppressed",
                    state.symbol, _direction, ai_q["c_ai"],
                )
                return
            # 【阶段 1·纪律修正】原 ai_opened 覆盖逻辑已删除：铁律要求 AI 绝不独立开出
            # HEXP 没给的方向。hexp 已拦(passed=False)的信号，AI 不得覆盖 threshold_passed
            # 继续发布。AI 仅能 VETO（反向否决）/ UPGRADE（增强已放行信号，见 quality_gate）。
            # 升级/降级 → 覆盖 hexp grade（下游语义/展示）
            if _decision.get("final_grade"):
                score_result.grade = _decision["final_grade"]
            # 手数分档透传：AI 只选档(low/mid/high)，实际倍率由风控面板动态手数决定
            # （不再在 signal_tower 侧乘固定倍率，避免双重倍率叠加）。
            ai_lot_tier = _q_tier
            # 【2026-08-28 口径修正】lot_tier_for 输入已是 scorecard_total(0~1 口径)，阈值
            # 已对齐为 0.65/0.55/0.45（见 quality_gate.py 默认值与 DB 热调）。若返回 none，
            # 表示 scorecard_total < 0.45（真实极弱信号），此处归一为 "low"（最小档）作为
            # 回退兜底：风控 _apply_dynamic_lot 走 ai_tier="low" 分支 → risk.lot_multiplier_low
            # （0.5）→ 最小手数 0.01，避免回退到 confidence 分档造成口径漂移。
            # 注：这是有意的兜底（弱信号最小档），非掩盖 bug——阈值已正确，none 即真弱。
            if ai_lot_tier == "none":
                ai_lot_tier = "low"
            # 【2026-08-27 修订】放行严格回到 6 维综合(scorecard_total)：lot_tier=none 仅当
            # HEXP 未 passed 时才抑制信号；HEXP 已 passed（6 维达标）时，hp_score 单维低导致的
            # none 不再抑制下单（仅观测），杜绝单维绕过综合闸门。手数档交由风控动态手数兜底。
            # cpl 未启用时 decide 恒返回 none（语义=不干预手数），绝不误杀信号。
            if ai_lot_tier == "none" and _decision.get("cpl_enabled"):
                if not score_result.threshold_passed:
                    # 极弱且 HEXP 未过闸 → 不发信号（等价原 lot_mult=0 语义）
                    logger.info(
                        "AI quality gate zero-tier %s: c_ai=%.2f total=%.2f — signal suppressed (hexp not passed)",
                        state.symbol, ai_q["c_ai"], float(_decision.get("total_score") or 0.0),
                    )
                    return
                # HEXP 已过闸（6 维综合达标）：单维 none 不抑制，放行交风控最低手数兜底
                logger.info(
                    "AI quality gate zero-tier but HEXP PASSED %s: hp=%.1f total=%.2f "
                    "— keep signal (6-dim gate honored), lot_tier none→risk fallback",
                    state.symbol, getattr(score_result, "hp_score", 0.0),
                    float(_decision.get("total_score") or 0.0),
                )

            # 2026-08-27 修订：耦合总分(coupling_total)仅用于手数链动(lot_tier，见 quality_gate 275
            # 行的 lot_tier_for(total))，不再作为下单闸门——下单严格由 HEXP 6 维 passed 决定。
            # 此处仅观测耦合分是否低于 hexp.coupling_pass_threshold（供报表/诊断），绝不改写
            # threshold_passed（不拦下单）。AI 耦合时"总分链动"=手数倍率用耦合总分；解耦时
            # 总分只计 hp_score（quality_gate 解耦分支 total_score=hp_score）。两种情况下单闸门
            # 均回归 6 维综合(scorecard_total)。
            if _decision.get("coupling_pass") is False and score_result.threshold_passed:
                _cp_thr = float(_ai_cfg.get("hexp.coupling_pass_threshold", 50.0))
                _cp_total = float(_decision.get("total_score") or 0.0)
                self._stats["ai_gate_rejects"] += 1
                logger.info(
                    "AI quality gate COUPLING-BELOW-MIN(obs) %s %s: total=%.2f < threshold=%.2f "
                    "(c_ai=%.2f hp=%.1f) — NOT suppressed (6-dim hexp gate honored, coupling drives lot only)",
                    state.symbol, _direction, _cp_total, _cp_thr,
                    ai_q["c_ai"], getattr(score_result, "hp_score", 0.0),
                )
                # 不修改 threshold_passed：耦合分只影响手数档，不影响放行闸门

        if not score_result.threshold_passed:
            logger.info(
                "Signal skipped: score=%.3f < threshold=%.3f (%s, %s)",
                score_result.pre_score, score_result.threshold,
                state.symbol, regime_result.regime.value,
            )
            # B3 fix: prefer the engine's canonical fallback_reason (e.g. adx_floor)
            # over the constructed "below_threshold(score<thr)" string. The engine
            # is the only place that knows the *real* reason.
            engine_reason = score_result.fallback_reason
            if engine_reason:
                suppress_chain.append((10, engine_reason))
            else:
                suppress_chain.append((10, _threshold_reason(score_result)))
            # B4: track adx_floor block for live override rescue (only for M5
            # bar-close signals — live_override signals shouldn't re-arm the rescue)
            if not live_override and "adx_floor" in (engine_reason or ""):
                state.last_signal_blocked_by_adx_floor = True
            # Publish filtered signal so PG captures pre_score for diagnostics
            await self._publish_filtered_signal(
                state, indicators, regime_result, score_result, trace_id,
                zone_level=zone_level, zone_type=zone_type, zone_strength=zone_strength,
                suppress_reason=_format_suppress_reason(suppress_chain),
            )
            return

        # ── Step 6: Cooldown Check ──────────────
        # vol_ratio = relative band width (bbw / bbw_ma20) drives P1 adaptive cooldown
        vol_ratio = (indicators.bbw / indicators.bbw_ma20) if indicators.bbw_ma20 > 0 else 1.0
        bar_seconds = TIMEFRAME_SECONDS.get(state.timeframe, 300)
        cooldown = self._scoring_engine.compute_cooldown(
            regime_result.regime,
            score_result.pre_score,
            score_result.direction,
            state.last_direction,
            vol_ratio,
            bar_seconds=bar_seconds,
        )
        elapsed_since_last = time.time() - state.last_signal_time
        if elapsed_since_last < cooldown:
            remaining = int(cooldown - elapsed_since_last)
            logger.info(
                "Signal suppressed: cooldown active for %s/%s (remaining %ds, cooldown %ds, regime %s)",
                state.symbol, state.timeframe, remaining, cooldown, regime_result.regime,
            )
            # P2c: make the suppression observable instead of a silent return.
            suppress_chain.append((20, f"cooldown_active(remaining={remaining}s,cooldown={cooldown}s)"))
            await self._publish_filtered_signal(
                state, indicators, regime_result, score_result, trace_id,
                zone_level=zone_level, zone_type=zone_type, zone_strength=zone_strength,
                suppress_reason=_format_suppress_reason(suppress_chain),
            )
            return

        # 2026-08-05 (D8): 移除 scheduler 的同向持仓数闸门(same_dir_bypass_limit)。
        # 持仓数上限现已完全由风控引擎 rule_chain._check_open_positions 负责
        # （account+symbol 总持仓封顶 risk.max_concurrent_signals，且本就是更紧的 binding
        # 约束，移除本闸门不会改变实际下单量），信号塔不再重复判定，消除"两套机制并存"
        # 的语义债与日志混乱（same_dir_bypass_limit vs Cooldown HIT）。
        # 注意：下方的 cooldown_active 是 scoring_engine 的 vol/regime 自适应节流
        # （基于 last_signal_time 的信号产出节奏），与风控引擎的"持仓时间窗冷却"
        # (risk.cooldown_minutes) 是两回事，后者由风控引擎独家负责，此处不重复。

        # ── Step 7: AI Invocation (REMOVED — 2026-07-24) ──
        # 五维共识模型(five_dim) 及其 DeepSeek 调用已弃用；当前生产机制为
        # co_source（逐信号本地评分，不调 AI）。2026-08-15 清理：删除残留的
        # ai_response 死变量与死分支（恒为 None，所有 `if ai_response is not None`
        # 分支永不可达，纯误导）。final_direction 直接采用评分方向。

        # ── Step 8: Final Direction ──
        # 直接采用评分方向（原 AI 对称否决分支已随 five_dim 弃用而删除）。
        final_direction = score_result.direction

        # ── 2026-08-28 修复：外部市场多因子分(composite)参与方向/仓位 ──
        # composite 来自 hcm-market-intel 的宏观+情绪+事件+流动性 4 维聚合(0~1，Redis
        # hcm:market:composite:score，每 30s 刷新)，代表外部市场环境对本品种交易的有利度。
        # 此前该分从未被读取→composite_score 列全 NULL，多因子评分形同虚设。
        # 设计（不颠覆 HEXP 6 维综合闸门，仅作外部因子协同）：
        #   (a) 方向门控：当 composite 极低(<0.35) 且 本信号属「弱证据」——
        #       即 NEUTRAL regime / 均值回归确认单(neutral_rsi_confirmed) / 盲点兜底单(h1_fallback)
        #       ——降级为 NO_TRADE。强趋势单(HEXP 已 passed + ADX 高)不受影响，避免矫枉过正。
        #       这正是昨日(08-27) SELL 在震荡市+恶劣外部环境下被反复扫损的根因。
        #   (b) 仓位衰减：composite 低→suggested_lot 乘衰减系数(0.5~1.0)，高→不衰减；
        #       与 AI 手数分档(ai_lot_tier)协同，不颠覆既有分档语义。
        _market_composite = 0.5  # 缺省中性（缺失时既不杀也不加成）
        if self._redis is not None:
            try:
                _mc_raw = await self._redis.get("hcm:market:composite:score")
                if _mc_raw is not None:
                    _market_composite = float(_mc_raw)
            except Exception:
                pass
        _composite_atten = 0.5 + 0.5 * max(0.0, min(1.0, _market_composite))  # 0.5~1.0
        # 弱证据信号判定（注意 _is_fb 在本段之后才定义，此处直接用 score_result.co_exec_fb）
        _weak_signal = (
            regime_result.regime.value == "NEUTRAL"
            or getattr(score_result, "neutral_rsi_confirmed", False)
            or bool(getattr(score_result, "co_exec_fb", 0))
        )
        if (final_direction in ("BUY", "SELL")
                and _market_composite < 0.35
                and _weak_signal
                and score_result.threshold_passed):
            # 仅在「弱证据 + 外部市场极端不利」时降级，强趋势单(threshold 强证据)保留。
            logger.info(
                "Composite gate NO_TRADE %s/%s %s: market_composite=%.2f<0.35 & weak_signal "
                "(regime=%s rsi_conf=%s fb=%s) → suppress (keep HEXP passed, external env bad)",
                state.symbol, state.timeframe, final_direction,
                _market_composite, regime_result.regime.value,
                getattr(score_result, "neutral_rsi_confirmed", False), _is_fb,
            )
            final_direction = "NO_TRADE"

        # 2026-08-27 修复：confidence 必须 0–100 制，与风控 risk_min_confidence
        # （0–100）及手数分档(score_tier_low/mid)口径对齐。pre_score 是 0–1 制，
        # 原样填入会让满分(A类100分)信号被风控误判 confidence=1.00<60 → REJECT。
        # 改用 6 维综合分 scorecard_total（0–100，放行强度权威口径）。
        final_confidence = float(getattr(score_result, "scorecard_total", 0.0) or 0.0)
        self._stats["signals_bypassed"] += 1

        # ── 【2026-08-26 反向单】momentum_flip 高位动量反向候选真下单路径 ──
        # 背景：hexp 的 momentum_flip 已把原 BUY/SELL 封成 NO_TRADE（_flip_block），
        # 同时记录 reverse_candidate（方向与原相反、顶部做空/底部做多）。
        # 当 reverse_candidate.order_intent=True（开关 hexp.reverse_order_enabled 打开）
        # 且原评分被 flip 拦成 NO_TRADE 时，本分支把 final_direction 覆写为候选的反向
        # 方向，使信号真正产出（接刀单）。该信号仍须经风控 rule_chain 的接刀护栏
        # （_check_reverse_order）裁决，未达保本/持仓条件则被拒（防盲目接刀）。
        # 开关默认 False → 整个分支不触发，行为与旧"纯观测"一致（零实盘影响）。
        _reverse_order = bool(
            (getattr(score_result, "reverse_candidate", None) or {}).get("order_intent", False)
        )
        if _reverse_order:
            _rc = getattr(score_result, "reverse_candidate", None) or {}
            _rc_dir = _rc.get("dir")
            if _rc_dir in ("BUY", "SELL") and final_direction == "NO_TRADE":
                logger.info(
                    "Reverse order intent: override final_direction %s → %s "
                    "(reverse_candidate at pos=%.2f) — routed to risk engine",
                    final_direction, _rc_dir, _rc.get("pos", 0.0),
                )
                final_direction = _rc_dir
                final_confidence = float(getattr(score_result, "scorecard_total", 0.0) or 0.0)
            elif _rc_dir in ("BUY", "SELL"):
                # 原评分已给出方向（非 flip 拦下）→ 反向候选不再覆写，避免与正常信号冲突
                _reverse_order = False

        # 【2026-08-17 修复】bypass_reason 缺失定义：原代码在 SignalData.fallback_reason
        # 与最终日志均引用 bypass_reason，但函数内从未赋值 → live override 触发路径每
        # 次走到 _produce_signal 必抛 NameError('bypass_reason')（实时评分快照失效 +
        # bar 内即时成交路径崩溃）。此处从 suppress_chain 取最高优先级原因聚合，
        # 无则 "none"。语义：记录本信号是否绕过某闸门及原因，供面板/落库诊断。
        if suppress_chain:
            _top = max(suppress_chain, key=lambda x: x[0])
            bypass_reason = _top[1]
        else:
            bypass_reason = "none"

        if final_direction == "NO_TRADE":
            logger.info("Signal NO_TRADE: %s", state.symbol)
            # engine's canonical reason (e.g. adx_floor) takes priority
            # over generic "no_trade_direction" so the dashboard shows the
            # *real* reason the signal was blocked.
            engine_reason = score_result.fallback_reason
            if engine_reason:
                suppress_chain.append((15, engine_reason))
            else:
                suppress_chain.append((15, "no_trade_direction"))
            # Audit 2026-07-14: still publish NO_TRADE so dashboard's
            # `latestInference` reflects current state (pre_score visible).
            await self._publish_filtered_signal(
                state, indicators, regime_result, score_result, trace_id,
                zone_level=zone_level, zone_type=zone_type, zone_strength=zone_strength,
                suppress_reason=_format_suppress_reason(suppress_chain),
            )
            return

        # ── P1a/P1c: enrich with AI risk params + zone-trigger hint ──
        # 2026-08-15 清理：原 ai_response 死变量（恒 None）已删；以下为兜底初值，
        # 随后由 co_source G3 / 会话系数 / LightGBM lm_score 缩放覆盖
        # （2026-08-18 解耦：DeepSeek ai_sl_coeff 不再覆盖开仓 SL）。
        ai_sl_mult = 0.0
        ai_tp_mult = 0.0
        suggested_lot = 1.0

        # P1 co_source G3 执行增强：用本地自适应门槛参数覆盖 AI 返回。
        # co_source 模式 bypass AI 后 ai_* 默认 0/1，必须由此提供 SL/TP/手数，
        # 否则 ai_sl_mult=0 会导致无止损（危险）。bridge 已消费 ai_sl_mult/ai_tp_mult。
        if profile["uses_co"]:
            _co_sl = getattr(score_result, "co_exec_sl_atr_mult", None)
            _co_rr = getattr(score_result, "co_exec_rr_min", None)
            _co_lot = getattr(score_result, "co_exec_lot_mult", None)
            if _co_sl:
                ai_sl_mult = float(_co_sl)
            if _co_sl and _co_rr:
                ai_tp_mult = float(_co_sl) * float(_co_rr)
            if _co_lot:
                suggested_lot = float(_co_lot)
            logger.info(
                "co_source G3 applied: sl_mult=%.2f tp_mult=%.2f lot=%.2f band=%s",
                ai_sl_mult, ai_tp_mult, suggested_lot,
                getattr(score_result, "co_band", "?"),
            )
        # ── P1 根因修复 + 时段化 (2026-07-24): TP/SL 倍数按当前盘口取值, 使 R:R≥min_rr ──
        # 会话键 close.<session>.trailing_stop_distance / close.<session>.min_rr 优先于
        # 全局键；min_rr 额外兼容 signal_tower.min_rr 别名。桥初始 SL = sl_mult×ATR。
        # 会话 SL 覆盖: 若命中会话专属 sl 键, 用会话系数覆盖模型 ai_sl_mult(实现亚紧美宽)。
        _sess = _current_session_utc()
        _sl_mult, _sl_session = await _session_float(self._redis, "trailing_stop_distance", 2.0)
        _min_rr, _ = await _session_float(
            self._redis, "min_rr", 1.2, fallback_keys=("signal_tower.min_rr",)
        )
        if _sl_session:
            _old_sl = ai_sl_mult
            ai_sl_mult = _sl_mult
            logger.info("Session SL override [%s]: ai_sl_mult=%.2f → %.2f (R:R≥%.2f)",
                        _sess, _old_sl, ai_sl_mult, _min_rr)
        # ── LightGBM ai_score → SL 宽度缩放 (2026-08-18 设计修正) ──
        # 期望（用户确认）：AI 分的高低缩放 SL 宽度，数值范围参考设计文档
        # hcm-ai-quality-scorer-design.md 的 ai_sl_coeff 区间 0.8~1.5×ATR。
        # 方向=分高→宽(1.5)、分低→窄(0.8)：scale = min + (ai_score/100)×(max-min)。
        #   - AI 有效（新鲜 lm_score 且 source≠none）→【替代】会话/co_source 值作为
        #     SL 距离(0.8~1.5×ATR)，并显式写 sl_price/tp1（桥对非零 sl_price 直接采用，
        #     无需 signal_tower.ai_risk_enabled，与 extreme_chase 同一机制）。
        #   - AI 断联/失效（lm_score=None / source=none）→ 回退会话 SL（不写 sl_price，
        #     桥用 close.<session>.trailing_stop_distance 重算）。
        # DeepSeek 不再直接干预订单 SL/TP（2026-08-18 语义修正）：其 ai_sl_coeff 仅
        # 作为训练特征辅助校准 LightGBM，不作为开仓 SL 缩放因子。
        _ai_sl_scale_applied = False
        _ai_lm_score = (ai_q or {}).get("lm_score")
        _ai_sl_enabled = True
        try:
            if self._config is not None:
                _ai_sl_enabled = bool(await self._config.get_bool("ai.lm.sl_scale_enabled", True))
        except Exception:
            _ai_sl_enabled = True
        if (_ai_sl_enabled
                and _ai_lm_score is not None
                and (ai_q or {}).get("source") not in ("none",)):
            try:
                _ai_score_f = float(_ai_lm_score)
                _ai_sl_min = float(await self._config.get("ai.lm.sl_scale_min", 0.8) or 0.8)
                _ai_sl_max = float(await self._config.get("ai.lm.sl_scale_max", 1.5) or 1.5)
                if _ai_sl_max <= _ai_sl_min:
                    _ai_sl_min, _ai_sl_max = 0.8, 1.5
            except Exception:
                _ai_score_f, _ai_sl_min, _ai_sl_max = 0.0, 0.8, 1.5
            _ai_sl_scale = _ai_sl_min + (_ai_score_f / 100.0) * (_ai_sl_max - _ai_sl_min)
            _ai_sl_scale = min(max(_ai_sl_scale, _ai_sl_min), _ai_sl_max)
            _sl_before = ai_sl_mult
            ai_sl_mult = round(_ai_sl_scale, 4)  # 替代会话/co_source 值，0.8~1.5×ATR
            _ai_sl_scale_applied = True
            logger.info(
                "LightGBM ai_score SL scale: score=%.1f scale=%.2f sl_mult=%.2f → %.2f "
                "(0.8~1.5, 分高→宽/分低→窄; AI 有效替代会话 SL)",
                _ai_score_f, _ai_sl_scale, _sl_before, ai_sl_mult,
            )
        else:
            logger.info(
                "AI SL scale DISABLED/failed: enabled=%s lm_score=%s source=%s → fallback "
                "session SL (no explicit sl_price)",
                _ai_sl_enabled, _ai_lm_score, (ai_q or {}).get("source"),
            )
        # TP 下限用【最终】ai_sl_mult(LightGBM 单源缩放) × min_rr → SL 被校宽时 TP 同步
        # 抬升, 保住 R:R≥min_rr（原按会话值算, 校准加宽后 R:R 会被稀释到 <1.2）。
        _tp_floor = ai_sl_mult * _min_rr
        if ai_tp_mult > 0:
            if ai_tp_mult < _tp_floor:
                logger.info("[%s] TP mult floor: ai_tp_mult=%.2f < %.2f → raise to %.2f (R:R≥%.2f)",
                            _sess, ai_tp_mult, _tp_floor, _tp_floor, _min_rr)
                ai_tp_mult = _tp_floor
        else:
            ai_tp_mult = _tp_floor  # AI 未给 TP 倍数 → 下限兜底, 杜绝无 TP
            logger.info("[%s] TP mult floor: ai_tp_mult unset → use %.2f (R:R≥%.2f)",
                        _sess, _tp_floor, _min_rr)
        # ── P1a/P1c: enrich with AI risk params + zone-trigger hint ──
        # 方案B (2026-07-23): 方向对齐的入场 zone 解析（根治 SELL 误等下方 PIVOT）。
        # _compute_entry_zone 返回的 nearest zone 方向不感知（仅用于打分 Tier2/prompt）。
        # 此处按 final_direction 重选「方向对齐 + 近端(atr*1.5 内)」的入场确认位：
        #   SELL → 现价上方偏近 RESISTANCE/PIVOT（等触达/反弹至阻力做空）
        #   BUY  → 现价下方偏近 SUPPORT/PIVOT（等触达/回踩至支撑做多）
        # 找不到方向对齐的近端 zone → 不延迟，市价成交；对向 zone 留给
        # _compute_target_zone 作 TP 锚点（与既有 TP 设计一致）。
        # ── P1a/P1c: enrich with AI risk params + zone-trigger hint ──
        # 方案B 修订 (2026-07-23): 拒绝强制延迟（根治 zone gate 后「有决策无成交」）。
        # 原逻辑：找到「方向对齐+近端(atr*1.5 内)」zone 即延迟 900s 等回踩/反弹。
        #   问题：atr*1.5 过宽→几乎全命中；且延迟对象(反向结构)与趋势动量反向
        #   →900s TTL 超时全丢弃→信号决策出了、成交全踏空。
        # 新逻辑（A：收窄触发）：
        #   - 仅当「实时价已在 zone band 内(犹豫于结构位)」才延迟(短 TTL=120s)；
        #   - 强趋势(adx>=28)或价格已离开结构位→直接市价(entry_trigger_wait=0)；
        #   - 取消「RSI 极端伪 zone 等回调 1.2ATR」的强制延迟(易踏空)，zone 仅作
        #     展示/TP 锚点，不延迟。
        #   zone gate 退化为「结构位卡点精度增强」，不再是默认延迟行为。
        _atr_z = float(getattr(indicators, 'atr_14', 4.0) or 4.0)
        # 放宽触发带：原 max(3, atr*0.3) 对黄金(ATR≈10)仅 3pt，使 99% 方向对齐 zone
        # 因 gap>3pt 直接市价、Zone「犹豫于结构位等确认」特长形同虚设。改为
        # max(5, atr*0.7)（ATR=10→7pt），让近端结构位信号真正触发 120s 短 TTL 等待，
        # 吃回踩/突破确认（短 TTL 防踏空，吸取 7-23 超时丢单教训）。
        _zone_touch_band = max(5.0, _atr_z * 0.7)  # 与桥侧 _price_in_zone_band band 对齐
        _live_px = await self._get_live_entry_price(state.symbol, final_direction)
        _strong_trend = (adx_for_floor or 0) >= 28
        _entry_zone = await self._resolve_entry_zone_for_direction(
            state, indicators.close, final_direction, indicators.atr_14)
        _is_fb = bool(getattr(score_result, "co_exec_fb", False))
        zone_level, zone_type, zone_strength = 0.0, "", 0  # 安全初始化（兜底分支可能不赋）
        if getattr(score_result, "neutral_rsi_confirmed", False):
            # 【C 组 2026-08-03】NEUTRAL RSI 均值回归确认单豁免 zone 延迟：
            # 该单贴着支撑/阻力触发（gap 必在 band 内），等 120s 回踩大概率
            # 错过反弹/反转窗口 → 直接市价。
            entry_trigger_wait = 0
            logger.info(
                "Entry zone skipped for NEUTRAL RSI confirmed %s/%s %s → MARKET",
                state.symbol, state.timeframe, final_direction,
            )
        elif _is_fb:
            # 盲点兜底单：方向由 H1 兜底、进场点位必须交 M5（结构位）。绝不市价追。
            # 放宽 zone 搜索半径(atr*5)，低波动行情下几乎总能找到方向对齐结构位，
            # 等价格触达该结构位才成交；若 M5 完全给不出结构位则放弃本轮（不市价追）。
            _fb_zone = await self._resolve_entry_zone_for_direction(
                state, indicators.close, final_direction, indicators.atr_14, max_gap_mult=5.0)
            if _fb_zone is None:
                logger.info(
                    "FB order skipped %s %s/%s: M5 has no entry zone → skip this bar (no chase, h1_fallback)",
                    final_direction, state.symbol, state.timeframe,
                )
                return
            zone_level, zone_type, zone_strength = _fb_zone
            entry_trigger_wait = 120  # 等 M5 结构位触达才成交（桥端 zone-trigger；超时不市价追，由桥端 fb 丢弃）
            logger.info(
                "FB order %s %s/%s: wait M5 zone touch %.2f (%s) wait=120 (h1_fallback, no chase)",
                final_direction, state.symbol, state.timeframe, zone_level, zone_type,
            )
        elif _entry_zone is not None:
            zone_level, zone_type, zone_strength = _entry_zone
            if _live_px is not None:
                _gap = abs(zone_level - _live_px)
                if (not _strong_trend) and (_gap <= _zone_touch_band):
                    # 价格正犹豫在结构位 → 仅等短 TTL 确认触达，避滑点追单
                    entry_trigger_wait = 120
                    logger.info(
                        "Entry zone (touch-only) %s/%s %s: %.2f (%s) gap=%.2f<=band=%.2f → wait=120",
                        state.symbol, state.timeframe, final_direction,
                        zone_level, zone_type, _gap, _zone_touch_band,
                    )
                else:
                    # 已离开结构位/强趋势 → 市价成交，拒绝延迟踏空
                    entry_trigger_wait = 0
                    logger.info(
                        "Entry zone %s/%s %s: %.2f gap=%.2f (>band=%.2f) strong_trend=%s → MARKET",
                        state.symbol, state.timeframe, final_direction,
                        zone_level, _gap, _zone_touch_band, _strong_trend,
                    )
            else:
                # 无实时价 → 不延迟，市价追（避免无依据强制等待）
                entry_trigger_wait = 0
        else:
            entry_trigger_wait = 0  # 无方向对齐 zone → 市价

        # ── 方案 B (2026-08-19): zone 硬闸门 —— 把「逆结构位」挡在门外 ──
        # 与方案 A (hexp.extreme.* Donchian 极值反转陷阱) 互补、串联：
        #   方案 A 拦「极值区+动量反向」的接刀单；本闸门拦「方向逆着结构位开仓」
        #   （阻力上方硬 BUY / 支撑下方硬 SELL）——入场点本身就逆着结构。
        # 仅当存在方向对齐结构位(_entry_zone 命中)且价格已显著越过该位(逆结构)时硬封；
        # 价格仍在结构位同侧(顺势)或无非对齐 zone → 不拦，交给方案 A/门槛裁决。
        # 独立开关 hexp.zone.hard_block_enabled(默认 False，需显式开启)；
        # hexp.zone.hard_block_atr_mult 控制「越过多远算逆结构」(ATR 倍数，默认 0.3)。
        # 注意：此闸门在 P1a 段、方案 A(extreme) 之后执行——extreme 已 NO_TRADE 的单
        # 不会二次处理；本闸门只拦 extreme 放行但方向逆结构位的单。
        _zone_hard_block = False
        if (final_direction in ("BUY", "SELL")
                and _entry_zone is not None
                and self._config is not None):
            try:
                _zb_enabled = bool(await self._config.get_bool(
                    "hexp.zone.hard_block_enabled", False))
            except Exception:  # noqa: BLE001
                _zb_enabled = False
            if _zb_enabled:
                _zb_atr_mult = 0.3
                try:
                    _zb_atr_mult = float(await self._config.get_float(
                        "hexp.zone.hard_block_atr_mult", 0.3))
                except Exception:  # noqa: BLE001
                    _zb_atr_mult = 0.3
                _zb_atr = _atr_z if _atr_z > 0 else 0.0
                _zb_band = _zb_atr * _zb_atr_mult if _zb_atr > 0 else 0.5
                _zb_px = _live_px if _live_px is not None else indicators.close
                _zb_dist = _zb_px - zone_level  # >0: 价在结构位上方；<0: 价在结构位下方
                # BUY 应在支撑下方(价 <= 支撑)，若价显著高于支撑(逆结构)→ 封
                # SELL 应在阻力上方(价 >= 阻力)，若价显著低于阻力(逆结构)→ 封
                _zb_against = (
                    (final_direction == "BUY" and _zb_dist > _zb_band)
                    or (final_direction == "SELL" and _zb_dist < -_zb_band)
                )
                if _zb_against:
                    _zone_hard_block = True
                    logger.info(
                        "hexp_zone_blocked %s/%s %s: price=%.2f against %s zone=%.2f "
                        "(band=%.2f, dir=%s) → NO_TRADE",
                        state.symbol, state.timeframe, final_direction,
                        _zb_px, zone_type, zone_level, _zb_band, final_direction,
                    )
        # 2026-08-05 (D9-1): 阶段标记——入场 zone 解析完成(已定 entry_trigger_wait)。
        logger.info(
            "Stage zone_resolved: %s/%s %s entry_trigger_wait=%ds zone=%.2f zone_blocked=%s",
            state.symbol, state.timeframe, final_direction,
            entry_trigger_wait, zone_level, _zone_hard_block,
        )

        # ── 方案 B (2026-08-19): zone 硬闸门拦截 → 走 NO_TRADE 发布路径 ──
        # 与方案 A 同源回写(threshold_passed=False + direction=NO_TRADE)，
        # 但原因标 hexp_zone_blocked 便于归因；复用 _publish_filtered_signal 落库。
        if _zone_hard_block:
            score_result.direction = "NO_TRADE"
            score_result.threshold_passed = False
            score_result.fallback_reason = "hexp_zone_blocked"
            suppress_chain.append((18, "hexp_zone_blocked"))
            logger.info(
                "Signal blocked by hexp zone hard-gate: %s/%s %s "
                "(against %s zone=%.2f) → NO_TRADE",
                state.symbol, state.timeframe, final_direction,
                zone_type, zone_level,
            )
            await self._publish_filtered_signal(
                state, indicators, regime_result, score_result, trace_id,
                zone_level=zone_level, zone_type=zone_type, zone_strength=zone_strength,
                suppress_reason=_format_suppress_reason(suppress_chain),
            )
            return

        # ── P0/Zone: TP 锚点 = 对向结构 zone（下一阻力/支撑）──
        # 对向结构 zone 始终传给桥做 TP 锚点。entry_trigger_wait 现已由上方
        # 收窄逻辑决定（仅在价格犹豫于结构位时=120s，否则=0 市价）。
        # 桥端是否据此等价格触 zone 才成交，由 signal_tower.zone_trigger_enabled
        #（当前=true）控制；但信号塔侧绝大多数方向信号现在直接市价，不再强制延迟。
        zone_tp_level = (
            await self._compute_target_zone(state, indicators.close, final_direction)
            if final_direction in ("BUY", "SELL") else 0.0
        )

        # ── Step 9: Publish Signal ──────────────
        t0 = time.time()

        # ── P1-5 Bar-level entry confirmation (graded discount, not hard block) ──
        #   信号生成于 bar close 时刻。如果当前 bar 强反向（实体幅度 >
        #   scoring.bar_vs_signal_atr_mult × ATR，默认 2.5），
        #   则对 pre_score 乘性折扣 scoring.bar_vs_signal_penalty（默认 0.5）后继续
        #   走 co_source.apply 门槛裁决（高 pre 信号仍可能过闸）；弱反向或中性 bar
        #   仅记警告、允许通过。极端棒反方向不再一刀切 NO_TRADE，避免波动市不下单。
        bar_open_val = float(getattr(indicators, 'bar_open', 0) or 0)
        bar_close_val = indicators.close
        bar_atr = float(getattr(indicators, 'atr_14', 0) or 0)
        _bar_range_abs = abs(bar_close_val - bar_open_val)
        _bar_against = (
            (final_direction == "BUY" and bar_close_val < bar_open_val)
            or (final_direction == "SELL" and bar_close_val > bar_open_val)
        )
        _bar_mult = await self._config.get_float(
            "scoring.bar_vs_signal_atr_mult", 1.0) if self._config else 1.0
        _bar_penalty = await self._config.get_float(
            "scoring.bar_vs_signal_penalty", 0.5) if self._config else 0.5
        _bar_strong = bar_atr > 0 and (_bar_range_abs / bar_atr) > _bar_mult
        if bar_open_val > 0 and _bar_against and _bar_strong:
            # [2026-08-04 治本⑤] 强反向棒不再硬拦截(NO_TRADE)，改为乘性折扣 pre_score，
            # 让高 pre 信号在极端棒反向下仍可能过闸(分级降分)，避免波动市一刀切不下单。
            # 折扣后 score_result 继续走下方 co_source.apply 的门槛裁决，仅当仍低于门槛才不过。
            score_result.pre_score = round(max(0.0, score_result.pre_score * _bar_penalty), 4)
            logger.info(
                "P1-5 bar vs signal (strong, downgraded x%.2f): %s o=%.2f c=%.2f range=%.2f atr=%.2f → pre_score=%.4f",
                _bar_penalty, final_direction, bar_open_val, bar_close_val,
                _bar_range_abs, bar_atr, score_result.pre_score,
            )
            suppress_chain.append((25, f"bar_vs_signal_strong_downgrade({final_direction},o={bar_open_val:.2f},c={bar_close_val:.2f})"))
        elif bar_open_val > 0 and _bar_against:
            logger.info(
                "P1-5 bar vs signal (weak, allowed): %s o=%.2f c=%.2f range=%.2f atr=%.2f",
                final_direction, bar_open_val, bar_close_val, _bar_range_abs, bar_atr,
            )
            suppress_chain.append((20, f"bar_weak_vs_{final_direction}(o={bar_open_val:.2f},c={bar_close_val:.2f})"))

        signal_id = await self._signal_publisher.generate_signal_id() if self._signal_publisher else int(time.time() * 1000000) % 1000000000

        # 2026-08-18：补记 AI 闸门决策（带真实 signal_id，供 daily_kpi 盈亏归因）。
        # 仅当该 symbol 本轮有暂存的闸门决策（ai_quality_decide 已跑）才落库。
        _pending = self._pending_ai_gate.pop(state.symbol, None)
        if _pending is not None:
            _snap, _decision, _ai_cfg = _pending
            await ai_log_gate_decision(
                self._db if (self._db is not None and getattr(self._db, "is_initialized", False)) else None,
                signal_id, state.symbol, _snap, _decision, cfg=_ai_cfg,
            )

        acc_id = await self._resolve_account_id()
        if acc_id is None:
            logger.error("无法解析主交易账户（无 active master / DB 不可用），跳过信号发射")
            return
        # ── P1-6 实时入场价（治本修复：出信号不下单）──
        # 优先用桥实时 tick 价（贴近实际成交盘口），缺失回退 bar 收盘价。
        live_entry_price = await self._get_live_entry_price(state.symbol, final_direction)
        if live_entry_price:
            logger.info(
                "Live entry price for %s/%s %s: %.2f (vs bar-close %.2f)",
                state.symbol, state.timeframe, final_direction,
                live_entry_price, indicators.close,
            )
        # ── C (2026-08-13): 极值追单收紧 SL 距离 ──
        # extreme_chase=True（极值区且 mm 动量仍朝原方向，A 已允许追单）时，把 SL 的 ATR
        # 倍数 × chase_sl_mult（默认 0.7）收紧，末端扫损单笔亏损更小；TP 不变 → R:R 改善。
        # 实现：显式算出 sl_price/tp1 写入 SignalData —— 桥对非零 sl_price 直接采用、且
        # 封顶 max_sl_atr_mult=1.8 不影响，规避默认关闭的 ai_risk_enabled 链路（默认走
        # close.<session>.trailing_stop_distance 重算 SL，改 ai_sl_mult 不生效）。
        _chase_sl_price = 0.0
        _chase_tp1 = 0.0
        if getattr(score_result, "extreme_chase", False):
            _chase = 0.7
            try:
                if self._config is not None:
                    _chase = float(await self._config.get_float(
                        "hexp.extreme.chase_sl_mult", 0.7))
            except Exception:
                pass
            _tight_sl = min(ai_sl_mult * _chase, 1.8)  # 防御性封顶，避免超过桥 max_sl_atr_mult
            _atr_c = float(getattr(indicators, "atr_14", 0.0) or 0.0)
            _entry_c = live_entry_price if live_entry_price else indicators.close
            if _atr_c > 0 and _entry_c > 0:
                if final_direction == "BUY":
                    _chase_sl_price = round(_entry_c - _tight_sl * _atr_c, 5)
                    _chase_tp1 = round(_entry_c + ai_tp_mult * _atr_c, 5)
                elif final_direction == "SELL":
                    _chase_sl_price = round(_entry_c + _tight_sl * _atr_c, 5)
                    _chase_tp1 = round(_entry_c - ai_tp_mult * _atr_c, 5)
                logger.info(
                    "Extreme chase SL tightened [%s]: sl_mult=%.2f→%.2f (×%.2f) "
                    "sl=%.2f tp=%.2f (R:R improved)",
                    _sess, ai_sl_mult, _tight_sl, _chase,
                    _chase_sl_price, _chase_tp1)
            ai_sl_mult = round(_tight_sl, 4)  # 同步信号记录（仅观测，桥默认不消费）
        # ── AI SL 缩放显式写价 (2026-08-18) ──
        # AI 有效时用【AI 缩放后】ai_sl_mult(0.8~1.5×ATR) 显式算 sl_price/tp1 写入
        # SignalData，桥对非零 sl_price 直接采用（无需 ai_risk_enabled）。
        # extreme_chase 优先（更特殊）；AI 断联/失效时 _ai_sl_scale_applied=False → 不写，
        # 桥回退会话 SL。extreme_chase 已写 _chase_sl_price 时本块不覆盖。
        _ai_sl_price = 0.0
        _ai_tp1 = 0.0
        if _ai_sl_scale_applied and _chase_sl_price == 0.0:
            _atr_a = float(getattr(indicators, "atr_14", 0.0) or 0.0)
            _entry_a = live_entry_price if live_entry_price else indicators.close
            if _atr_a > 0 and _entry_a > 0:
                _sl_a = min(ai_sl_mult, 1.8)  # 防御性封顶，与桥 max_sl_atr_mult 一致
                if final_direction == "BUY":
                    _ai_sl_price = round(_entry_a - _sl_a * _atr_a, 5)
                    _ai_tp1 = round(_entry_a + ai_tp_mult * _atr_a, 5)
                elif final_direction == "SELL":
                    _ai_sl_price = round(_entry_a + _sl_a * _atr_a, 5)
                    _ai_tp1 = round(_entry_a - ai_tp_mult * _atr_a, 5)
                logger.info(
                    "AI SL scale explicit price [%s]: sl_mult=%.2f sl=%.2f tp=%.2f "
                    "(bridge adopts nonzero sl_price)",
                    _sess, _sl_a, _ai_sl_price, _ai_tp1)
        _ind_vals = {
            "adx_14": round(indicators.adx_14, 2),
            "rsi_14": round(indicators.rsi_14, 2),
            "macd": round(indicators.macd, 4),
            "atr_14": round(indicators.atr_14, 2),
            # P0/P1 H1 状态判定层：落库以便事后归因该笔信号是否与 H1 同向
            "h1_regime": indicators.h1_regime,
            "h1_trend_direction": indicators.h1_trend_direction,
            "h1_trend_strength": round(indicators.h1_trend_strength, 3),
            "h1_adx": round(indicators.h1_adx, 2),
        }
        # B: hexp 落库持久化（纯观测，零下单影响）——和乘幂元数据并入 indicator_values
        if getattr(score_result, "weight_scheme", "").startswith("HEXP"):
            _ind_vals["_hexp"] = {
                "hp_score": getattr(score_result, "hp_score", None),
                "hp_strength": getattr(score_result, "hp_strength", None),
                "dir_sum": getattr(score_result, "dir_sum", None),
                "k": getattr(score_result, "k_value", None),
                "verdict": getattr(score_result, "resonance_verdict", None),
                # mm 仅为微动量强度（非方向）：前端画方向箭头必须用下方 direction 字段，
                # 禁止用 mm 符号判断方向（mm 在 0 附近高频抖动 → 方向闪烁缺陷，2026-08-27）。
                "mm": getattr(score_result, "mm_score", None),
                "mm_smoothed": getattr(score_result, "mm_smoothed", None),
                # 综合裁决方向（多因子加权和+EMA平滑+迟滞），面板方向箭头唯一正确来源。
                "direction": getattr(score_result, "direction", "NO_TRADE"),
                "grade": getattr(score_result, "grade", None),
                "factor_scores": getattr(score_result, "factor_scores", None),
                "trend_scores": getattr(score_result, "trend_scores", None),
                "period_states": getattr(score_result, "period_states", None),
                "factor_raws": getattr(score_result, "factor_raws", None),
                "trend_phase": getattr(score_result, "trend_phase", None),
                "di_plus": getattr(indicators, "plus_di", None),
                "di_minus": getattr(indicators, "minus_di", None),
                # 2026-08-26 反向单（由"纯观测"升级为"经风控后下单"）：momentum_flip 高位
                # 动量反向时记录的反向候选(dir/pos/er/mm/close/verdict/order_intent)。
                # order_intent=True 时本信号将覆写 final_direction 为候选方向并经风控
                # 接刀护栏裁决；False 仅观测（供 SQL 回测胜率，不下单）。
                "reverse_candidate": getattr(score_result, "reverse_candidate", None),
                "reverse_order": bool(
                    (getattr(score_result, "reverse_candidate", None) or {}).get("order_intent", False)
                ),
                # 2026-08-24 趋势抢跑观测：phase=ignite+动量同向+pos 中低位的顺势启动候选。
                # 写进生产 hexp 信号的 indicator_values._hexp（shadow 在 active_model=hexp 时
                # 被跳过，故必须挂主信号才能积累评估数据）。仅观测不下单。
                "trend_start_candidate": getattr(score_result, "trend_start_candidate", None),
            }
        # 2026-08-27 周期位置观测字段：hexp 与 co(和乘幂) 路径统一落库（不在 _hexp 子结构内），
        # 支撑两侧守卫命中率统计。co_source 不设 mm，本无 mm 闪烁缺陷；此处补齐位置观测一致性。
        _ind_vals["position_cycle"] = getattr(score_result, "position_cycle", None)
        _ind_vals["position_z"] = getattr(score_result, "position_z", None)
        _ind_vals["cycle_pos_blocked"] = bool(getattr(score_result, "cycle_pos_blocked", False))
        signal_data = SignalData(
            signal_id=signal_id,
            task_id=0,
            account_id=acc_id,
            symbol=state.symbol,
            time_frame=state.timeframe,
            direction=final_direction,
            entry_price=live_entry_price if live_entry_price else indicators.close,
            # 2026-08-18: extreme_chase 优先；否则 AI SL 缩放显式价(_ai_sl_price)；
            # 两者都无(=0) → 桥回退会话 SL。
            sl_price=_chase_sl_price if _chase_sl_price else _ai_sl_price,
            tp1=_chase_tp1 if _chase_tp1 else _ai_tp1,
            tp2=0.0,
            lot=0.0,
            confidence=final_confidence,
            signal_mode=(
                "live_override" if live_override
                else (score_result.weight_scheme or "scoring")[:30]
            ),
            indicator_values=_ind_vals,
            fallback_reason=bypass_reason,
            regime=regime_result.regime.value,
            pre_score=round(score_result.pre_score, 4),
            weight_scheme=score_result.weight_scheme,
            # 2026-08-25 极值分层裁决：hexp 极值+保本追单候选 → 透传，风控保本闸门最终裁决
            extreme_pending=bool(getattr(score_result, "extreme_pending", False)),
            # 2026-08-26 反向单：经 momentum_flip 封 NO_TRADE 后由 reverse_candidate 覆写
            # 方向产出的接刀单，透传标记 → 风控 _check_reverse_order 接刀护栏裁决。
            reverse_order=_reverse_order,
            # 2026-08-13 修复：优先用 hexp 引擎落库的 Donchian 分位（sr.position_in_range），
            # 支撑「高位做多/低位做空」SQL 敏捷识别；非 hexp 路径回退 range_position。
            position_in_range=(
                getattr(score_result, "position_in_range", None)
                if getattr(score_result, "position_in_range", None) is not None
                else (round(score_result.range_position.pct_b_range, 4) if score_result.range_position else None)
            ),
            # 2026-08-27 C4 位置/极值溯源字段落库：复盘"高位开多/低位开空"止损归因。
            position_cycle=getattr(score_result, "position_cycle", None),
            position_z=getattr(score_result, "position_z", None),
            ma_raw=getattr(score_result, "ma_raw", None),
            cycle_pos_blocked=bool(getattr(score_result, "cycle_pos_blocked", False)),
            extreme_reversal_blocked=bool(getattr(score_result, "extreme_reversal_blocked", False)),
            threshold_passed=(
                None if getattr(score_result, "threshold_passed", None) is None
                else bool(score_result.threshold_passed)
            ),
            trace_id=trace_id,
            produced_at=datetime.now(timezone.utc).isoformat(),  # D7-1: T0 信号生产决策时刻
            zone_level=zone_level,
            zone_type=zone_type,
            zone_strength=zone_strength,
            zone_tp_level=zone_tp_level,
            # ── P1a/P1c collaboration fields (consumed by mt5_bridge) ──
            ai_sl_mult=ai_sl_mult,
            ai_tp_mult=ai_tp_mult,
            # 2026-08-28 修复：外部市场 composite 参与仓位——弱环境衰减(0.5~1.0)，
            # 与 AI 手数分档协同（衰减作用在基底 suggested_lot 上，不颠覆 ai_lot_tier 分档）。
            suggested_lot_ratio=round(suggested_lot * _composite_atten, 4),
            # 2026-08-28 修复：composite 落库（此前该列全 NULL，多因子评分未接线）。
            composite_score=round(_market_composite, 4),
            # AI 手数分档（low/mid/high/none）→ 风控引擎选档用（链动动态手数）
            ai_lot_tier=ai_lot_tier,
            entry_trigger_wait=entry_trigger_wait,
            co_exec_fb=1 if _is_fb else 0,
            # ── P2: expose scoring-engine component breakdown to dashboard ──
            component_scores=score_result.component_scores,
        )

        # 注：AI 融合闸门已在上方 threshold_passed 检查之前前移评估（2026-08-14 修复），
        # 此处不再重复。scheduler L1710 前的块负责 VETO/OPEN/手数倍率协同。

        # 2026-08-05 (D9-1): 阶段标记——即将发布(经 signal:stream → 风控 → 桥)。
        logger.info(
            "Stage publishing: %s id=%d %s pre=%.3f conf=%.2f wait=%ds zone=%.2f",
            state.symbol, signal_id, final_direction, score_result.pre_score,
            final_confidence, entry_trigger_wait, zone_level,
        )
        # 2026-08-05 (D9-2): BUY/SELL 决策产出 → 重置停滞计数。
        state.bars_without_decision = 0
        state.decision_stall_warned = False
        if self._signal_publisher:
            await self._signal_publisher.publish(signal_data)
            # P1b: 标注采集 — 每条信号写入 labeled_samples 供校准层消费
            if score_result and regime_result:
                await self._signal_publisher.save_labeled_sample(
                    signal_id=signal_data.signal_id,
                    symbol=state.symbol,
                    bar_time=state.last_bar_open_time,
                    m5_regime=regime_result.regime.value,
                    direction=final_direction,
                    raw_score=score_result.pre_score,
                    calibrated=score_result.pre_score,  # post co_source.apply() already calibrated
                )

        if self._watchdog:
            await self._watchdog.report_step("publish", time.time() - t0)

        # Update state
        state.last_signal_time = time.time()
        state.last_direction = final_direction
        # B4: successful signal published → clear adx_floor rescue flag.
        # Either an M5 signal cleared the floor, or a live_override was emitted.
        state.last_signal_blocked_by_adx_floor = False
        state.adx_above_floor_since = 0.0
        if live_override:
            state.last_live_override_time = time.time()
            state.last_signal_mode = "live_override"
            logger.info("Live override published: %s, direction=%s, score=%.3f",
                        state.symbol, final_direction, score_result.pre_score)
        self._stats["signals_produced"] += 1

        logger.info(
            "Signal produced: id=%d, symbol=%s, direction=%s, "
            "pre_score=%.3f, confidence=%.2f, regime=%s, "
            "bypass=%s, bar_momentum=%.3f, trace_id=%s",
            signal_id, state.symbol, final_direction,
            score_result.pre_score, final_confidence,
            regime_result.regime.value, bypass_reason or "none",
            score_result.bar_momentum_applied, trace_id,
        )

    async def _publish_filtered_signal(
        self, state: SymbolState, indicators: IndicatorResults,
        regime_result: RegimeResult, score_result: ScoreResult,
        trace_id: str,
        extra_reason: str = "",
        zone_level: float = 0.0, zone_type: str = "", zone_strength: int = 0,
        suppress_reason: str = "",
    ) -> None:
        # 2026-08-05 (D9-2): 任何决策(NO_TRADE/filtered)产出 → 重置停滞计数。
        state.bars_without_decision = 0
        state.decision_stall_warned = False
        """Publish a below-threshold signal to PG for diagnostic visibility.

        Score 0.1-0.28 signals that would otherwise be silently dropped are
        written with direction=NO_TRADE so the real pre_score is captured.
        """
        if self._signal_publisher is None:
            return

        signal_id = await self._signal_publisher.generate_signal_id()

        acc_id = await self._resolve_account_id()
        if acc_id is None:
            logger.error("无法解析主交易账户（无 active master / DB 不可用），跳过信号发射")
            return
        signal_data = SignalData(
            signal_id=signal_id,
            task_id=0,
            account_id=acc_id,
            symbol=state.symbol,
            time_frame=state.timeframe,
            direction="NO_TRADE",
            entry_price=indicators.close,
            sl_price=0.0,
            tp1=0.0,
            tp2=0.0,
            lot=0.0,
            confidence=float(getattr(score_result, "scorecard_total", 0.0) or 0.0),
            signal_mode="filtered",
            indicator_values={
                "adx_14": round(indicators.adx_14, 2),
                "rsi_14": round(indicators.rsi_14, 2),
                "macd": round(indicators.macd, 4),
                "atr_14": round(float(getattr(indicators, "atr_14", 0) or 0), 2),
                # P0/P1 H1 状态判定层：落库以便事后归因该笔信号是否与 H1 同向
                "h1_regime": indicators.h1_regime,
                "h1_trend_direction": indicators.h1_trend_direction,
                "h1_trend_strength": round(indicators.h1_trend_strength, 3),
                "h1_adx": round(indicators.h1_adx, 2),
            },
            fallback_reason=(
                # P2c: prefer the aggregated suppress reason when supplied —
                # it carries the single most-significant cause plus the chain.
                # Otherwise fall back to the legacy per-case reason.
                suppress_reason
                or (
                    (
                        _threshold_reason(score_result)
                        + (f"|{extra_reason}" if extra_reason else "")
                    )
                    if not score_result.threshold_passed
                    else (extra_reason or f"no_trade(dir={score_result.direction})")
                )
            ),
            regime=regime_result.regime.value,
            pre_score=round(score_result.pre_score, 4),
            weight_scheme=score_result.weight_scheme,
            # 2026-08-25 极值分层裁决：hexp 极值+保本追单候选 → 透传，风控保本闸门最终裁决
            extreme_pending=bool(getattr(score_result, "extreme_pending", False)),
            # 2026-08-13 修复：优先用 hexp 引擎落库的 Donchian 分位（sr.position_in_range），
            # 支撑「高位做多/低位做空」SQL 敏捷识别；非 hexp 路径回退 range_position。
            position_in_range=(
                getattr(score_result, "position_in_range", None)
                if getattr(score_result, "position_in_range", None) is not None
                else (round(score_result.range_position.pct_b_range, 4) if score_result.range_position else None)
            ),
            # 2026-08-27 C4 位置/极值溯源字段落库（filtered 路径保持一致口径）。
            position_cycle=getattr(score_result, "position_cycle", None),
            position_z=getattr(score_result, "position_z", None),
            ma_raw=getattr(score_result, "ma_raw", None),
            cycle_pos_blocked=bool(getattr(score_result, "cycle_pos_blocked", False)),
            extreme_reversal_blocked=bool(getattr(score_result, "extreme_reversal_blocked", False)),
            threshold_passed=(
                None if getattr(score_result, "threshold_passed", None) is None
                else bool(score_result.threshold_passed)
            ),
            trace_id=trace_id,
            zone_level=zone_level,
            zone_type=zone_type,
            zone_strength=zone_strength,
            # ── P2: expose scoring-engine component breakdown to dashboard ──
            component_scores=score_result.component_scores,
        )

        # 【D 组 2026-08-03】filtered/NO_TRADE 诊断信号仅落 PG、不进 signal:stream：
        # 原实现会让 NO_TRADE 白跑风控整条规则链并转发 risk_passed，直到桥才丢弃，
        # 浪费吞吐且污染信号状态。塔内拦截信号对执行链路无意义。
        await self._signal_publisher.publish(signal_data, to_stream=False)
        logger.debug(
            "Filtered signal recorded (PG only): id=%d, symbol=%s, pre_score=%.3f (below threshold)",
            signal_id, state.symbol, score_result.pre_score,
        )

    # ── P2: publish real-time scoring-engine component scores ──

    async def _publish_component_scores(
        self,
        state: SymbolState,
        indicators: IndicatorResults,
        regime_result: RegimeResult,
        score_result: ScoreResult,
    ) -> None:
        """Write the scoring-engine component breakdown to Redis.

        Key: hcm:live:component_scores:{symbol}_{timeframe}
        Dashboard signal gauges read this to display the *actual* indicators
        and buy/sell ratios used by the scoring engine, rather than a
        parallel recomputation.
        """
        if self._redis is None or not self._redis.is_initialized:
            return
        try:
            weights = self._scoring_engine._select_weight_scheme(
                regime_result.regime,
                adx=indicators.adx_14,
                bbw=indicators.bbw,
            )
            payload = {
                "component_scores": {k: list(v) for k, v in score_result.component_scores.items()},
                "weights": {k: float(v) for k, v in weights.items()},
                "weight_scheme": score_result.weight_scheme,
                "regime": regime_result.regime.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await self._redis.raw.set(
                f"hcm:live:component_scores:{state.symbol}_{state.timeframe}",
                json.dumps(payload, ensure_ascii=False),
                ex=3600,
            )
        except Exception as exc:
            logger.debug("Component scores Redis write failed for %s: %s", state.symbol, exc)

    # ── P2b: publish the REAL raw indicator readings (not scored numbers) ──

    async def _publish_raw_indicators(
        self,
        state: SymbolState,
        indicators: IndicatorResults,
    ) -> None:
        """Write the authentic, raw indicator readings to Redis for the dashboard.

        Key: hcm:live:indicators:{symbol}_{timeframe}
        This is the genuine market data (ADX level, +DI/-DI spread, RSI,
        MACD histogram, Bollinger %b/BBW, Stochastic %K/%D, MA values,
        single-bar momentum, ATR) that *drives* the scoring engine — surfaced
        so the dashboard shows real values, not abstract normalized scores.
        """
        if self._redis is None or not self._redis.is_initialized:
            return
        try:
            bar_high = indicators.recent_highs[-1] if indicators.recent_highs else 0.0
            bar_low = indicators.recent_lows[-1] if indicators.recent_lows else 0.0
            bar_range = max(0.0, float(bar_high) - float(bar_low))
            payload = {
                "adx_14": round(float(indicators.adx_14), 2),
                "plus_di": round(float(indicators.plus_di), 2),
                "minus_di": round(float(indicators.minus_di), 2),
                "di_diff": round(float(indicators.di_diff), 2),
                "rsi_14": round(float(indicators.rsi_14), 2),
                "macd": round(float(indicators.macd), 4),
                "macd_signal": round(float(indicators.macd_signal), 4),
                "macd_histogram": round(float(indicators.macd_histogram), 4),
                "boll_upper": round(float(indicators.boll_upper), 2),
                "boll_middle": round(float(indicators.boll_middle), 2),
                "boll_lower": round(float(indicators.boll_lower), 2),
                "pct_b": round(float(indicators.pct_b), 3),
                "bbw": round(float(indicators.bbw), 4),
                "bbw_ma20": round(float(indicators.bbw_ma20), 4),
                "stoch_k": round(float(indicators.stoch_k), 2),
                "stoch_d": round(float(indicators.stoch_d), 2),
                "ma_short": round(float(indicators.ma_short), 2),
                "ma_long": round(float(indicators.ma_long), 2),
                "ma_alignment": indicators.ma_alignment,
                "atr_14": round(float(indicators.atr_14), 2),
                "bar_open": round(float(indicators.bar_open), 2),
                "close": round(float(indicators.close), 2),
                "bar_high": round(float(bar_high), 2),
                "bar_low": round(float(bar_low), 2),
                "bar_range": round(float(bar_range), 2),
                "timeframe": state.timeframe,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await self._redis.raw.set(
                f"hcm:live:indicators:{state.symbol}_{state.timeframe}",
                json.dumps(payload, ensure_ascii=False),
                ex=3600,
            )
        except Exception as exc:
            logger.debug("Raw indicators Redis write failed for %s: %s", state.symbol, exc)

    async def _publish_h1_context(
        self,
        state: SymbolState,
        h1_context: Optional[H1Context],
    ) -> None:
        """Write the H1 multi-timeframe regime context to Redis for the dashboard.

        Key: hcm:live:h1_regime:{symbol}
        Surfaces the HMTS state-judgement layer (H1 regime / direction /
        strength / ADX) so operators can verify the H1 filter is working and
        validate classification accuracy against manual review.
        """
        if self._redis is None or not self._redis.is_initialized:
            return
        try:
            payload = {
                "regime": h1_context.regime if h1_context else None,
                "trend_direction": h1_context.trend_direction if h1_context else "",
                "trend_strength": round(h1_context.trend_strength, 3) if h1_context else 0.0,
                "adx": round(h1_context.adx, 2) if h1_context else 0.0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await self._redis.raw.set(
                f"hcm:live:h1_regime:{state.symbol}",
                json.dumps(payload, ensure_ascii=False),
                ex=3600,
            )
        except Exception as exc:
            logger.debug("H1 context Redis write failed for %s: %s", state.symbol, exc)

    # ═══════════════════════════════════════════════════════════════
    #  Phase 0 (2026-08-05): 微观状态机 + 精准买点分 — shadow 对比
    # ═══════════════════════════════════════════════════════════════
    # ── Phase 1/2/3 灰度辅助 ──────────────────────
    async def _is_v2_enabled(self) -> bool:
        """co.v2_enabled 灰度总开关（默认 False）。经 ConfigProvider 读 PG/Redis 真源。"""
        if self._config is None:
            return False
        try:
            return bool(await self._config.get_bool("co.v2_enabled", False))
        except Exception:  # pragma: no cover - 配置缺失时保守关闭
            return False

    async def _compute_v2_inputs(
        self, state: "SymbolState", indicators: Any, regime_result: Any,
        h1_context: Any, score_result: Any,
    ) -> tuple:
        """Phase 1/2/3：计算微观状态机 + 精准买点分 + 自适应门槛 θ（供 apply_v2 使用）。

        与 shadow 共用 self._micro_state / self._precision_entry 实例（复用
        _shadow_v2_loaded 加载标志，避免重复 load_config）。当 co.v2_shadow_enabled
        与 co.v2_enabled 均关闭时返回 (None,0,0)（零开销）。
        """
        # 【2026-08-28 co_source 清除】去掉 _co_source 依赖检查；本方法只依赖
        # micro_state / precision_entry（二者已被 HEXP 入场闸门直接使用）。
        if self._micro_state is None or self._precision_entry is None:
            return None, 0.0, 0.0, None
        _want_compute = True
        if self._config is not None:
            try:
                _want_compute = (
                    await self._config.get_bool("co.v2_shadow_enabled", True)
                    or await self._config.get_bool("co.v2_enabled", False)
                )
            except Exception:
                _want_compute = True
        if not _want_compute:
            return None, 0.0, 0.0, None
        if not self._shadow_v2_loaded:
            await self._micro_state.load_config()
            await self._precision_entry.load_config()
            self._shadow_v2_loaded = True
        _ms = self._micro_state.classify(indicators, regime_result, h1_context)
        _eq, _breakdown = self._precision_entry.compute(
            indicators, _ms, h1_context, score_result)
        _theta = self._micro_state.adaptive_theta(
            _ms.state, getattr(regime_result, "vol_factor", 1.0))
        return _ms, _eq, _theta, _breakdown

    async def _run_shadow_v2(
        self,
        state: "SymbolState",
        indicators: Any,
        regime_result: Any,
        h1_context: Any,
        score_result: Any,
        zone_level: float = 0.0,
        legacy_passed: Optional[bool] = None,
        legacy_reason: str = "",
        ms_pre: Any = None,
        eq_pre: float = 0.0,
        theta_pre: float = 0.0,
    ) -> None:
        """Phase 0 shadow：并行计算微观状态 + 精准买点分，与现有评分对比。

        仅日志 + 落 Redis，绝不改变交易行为。用于采集"现有拦截 vs 新买点"
        差异样本，确保 Phase 1/2 放行前新买点不劣化。

        Redis 落点：
          - hcm:live:micro_state:{symbol}   最新微观状态（供面板观察）
          - hcm:shadow:v2:{symbol}          最近 200 条对比样本（capped list）
        灰度开关：co.v2_shadow_enabled（默认 True；置 False 即停采，退回零开销）。

        legacy_passed / legacy_reason：当 co.v2_enabled 启用时，score_result 已是
        apply_v2 的权威决策，故 shadow 的 "old" 端必须显式传入 legacy（v1）决策，
        才能继续观测新旧分歧；未传则回退读 score_result（v2 关闭时）。
        """
        try:
            if self._config is not None:
                _enabled = await self._config.get_bool("co.v2_shadow_enabled", True)
                if not _enabled:
                    return
            if not self._shadow_v2_loaded:
                await self._micro_state.load_config()
                await self._precision_entry.load_config()
                self._shadow_v2_loaded = True

            # 复用 _compute_v2_inputs 已算好的微观态/买点分/θ（避免热路径双重计算）；
            # 仅当未传入（极罕见：两者都关但 shadow 仍开）才兜底重算。
            if ms_pre is not None:
                _ms, _eq, _theta = ms_pre, eq_pre, theta_pre
                _breakdown: dict = {}
            else:
                _ms = self._micro_state.classify(indicators, regime_result, h1_context)
                _eq, _breakdown = self._precision_entry.compute(
                    indicators, _ms, h1_context, score_result)
                _theta = self._micro_state.adaptive_theta(
                    _ms.state, getattr(regime_result, "vol_factor", 1.0))
            _new_passed = _eq >= _theta

            # "old" 端：v2 启用时取显式 legacy 决策，否则回退 score_result
            if legacy_passed is not None:
                _old_passed = bool(legacy_passed)
                _old_reason = legacy_reason or ""
            else:
                _old_passed = bool(getattr(score_result, "threshold_passed", False))
                _old_reason = getattr(score_result, "fallback_reason", "") or ""
            if _new_passed == _old_passed:
                _verdict = "AGREE"
            elif _new_passed:
                _verdict = "NEW_PASS_OLD_BLOCK"
            else:
                _verdict = "NEW_BLOCK_OLD_PASS"

            logger.info(
                "SHADOW v2 %s: micro=%s(%s) dir=%s eq=%.3f theta=%.3f newPass=%s "
                "| oldPass=%s reason=%s [%s]",
                state.symbol, _ms.state.value, _ms.direction,
                getattr(score_result, "direction", "?"),
                _eq, _theta, _new_passed, _old_passed, _old_reason, _verdict,
            )

            if self._redis is None or not self._redis.is_initialized:
                return
            _now = datetime.now(timezone.utc).isoformat()
            _live_payload = {
                "micro_state": _ms.state.value,
                "direction": _ms.direction,
                "strength": _ms.strength,
                "entry_quality": round(_eq, 4),
                "theta": _theta,
                "new_pass": _new_passed,
                "old_pass": _old_passed,
                "verdict": _verdict,
                "details": _ms.details,
                "updated_at": _now,
            }
            await self._redis.raw.set(
                f"hcm:live:micro_state:{state.symbol}",
                json.dumps(_live_payload, ensure_ascii=False),
                ex=3600,
            )
            _sample = {
                "ts": _now,
                "symbol": state.symbol,
                "micro_state": _ms.state.value,
                "micro_dir": _ms.direction,
                "score_dir": getattr(score_result, "direction", "?"),
                "pre_score": round(float(getattr(score_result, "pre_score", 0.0) or 0.0), 4),
                "old_threshold": round(float(getattr(score_result, "threshold", 0.0) or 0.0), 4),
                "old_passed": _old_passed,
                "old_reason": _old_reason,
                "entry_quality": round(_eq, 4),
                "theta": _theta,
                "new_passed": _new_passed,
                "verdict": _verdict,
                "zone_level": zone_level,
                "breakdown": _breakdown,
            }
            _key = f"hcm:shadow:v2:{state.symbol}"
            await self._redis.raw.lpush(_key, json.dumps(_sample, ensure_ascii=False))
            await self._redis.raw.ltrim(_key, 0, 199)
        except Exception as exc:
            logger.debug("SHADOW v2 failed for %s: %s", getattr(state, "symbol", "?"), exc)

    # ═══════════════════════════════════════════════════════════════
    #  Fix #1: Bar Direction Confirmation

    # ═══════════════════════════════════════════════════════════════
    #  Fix #3: AI Fallback Gate

    # ═══════════════════════════════════════════════════════════════
    #  Fix #4: Redis Live Bar Read

    # ─────────────────────────────────────────────────────────────────
    #  Hexp Shadow (双跑落库、不下单)
    #  设计目标：和乘幂(hexp)引擎激活前，先并行运行、把 BUY/SELL 决策落库到
    #  hcm_signal.signals (signal_mode='hexp_shadow')，但【绝不】publish 到
    #  signal:stream → 不进风控引擎 → 不进桥 → 零实盘影响。
    #  后续由 _reconcile_hexp_shadow 用历史 K 线模拟 SL/TP 命中、计算方向命中，
    #  落 hcm_signal.hexp_shadow_eval，供「信号对照报表」评估 hexp 准确率。
    #  开关：hexp.shadow_enabled (默认 False)。M1 数据缺失时 MM 因子退化由引擎兜底。
    # ─────────────────────────────────────────────────────────────────
    async def _run_shadow_hexp(
        self, state, indicators, regime_result, h1_context, score_result, zone_level=0.0,
        active_model=None,
    ):
        """双跑和乘幂引擎，仅落库不下单。"""
        # 守卫：当 hexp 本身就是生产模型时不再双跑影子（避免 self-vs-self 对照、
        # 每根 bar 重复拉取多周期 K 线造成的无谓开销）。
        # 但趋势启动候选(trend_start_candidate)在生产信号里持续落库，需周期性触发
        # reconcile 评估其"假设成交"胜率（影子评估，零实盘影响）。
        if active_model == "hexp":
            _now = time.time()
            if _now - getattr(self, "_hexp_shadow_last_recon", 0.0) >= 60:
                self._hexp_shadow_last_recon = _now
                await self._reconcile_hexp_shadow()
            return
        if self._config is not None:
            _enabled = await self._config.get_bool("hexp.shadow_enabled", False)
        else:
            _enabled = False
        if not _enabled or self._hexp_engine is None:
            return

        _hexp_sr = await self._hexp_engine.produce(
            state.symbol, indicators, regime_result, live=False,
        )
        _hdir = getattr(_hexp_sr, "direction", "NO_TRADE")
        _hpre = round(float(getattr(_hexp_sr, "pre_score", 0.0) or 0.0), 4)
        _co_dir = getattr(score_result, "direction", "NO_TRADE")

        if _hdir not in ("BUY", "SELL"):
            logger.info(
                "SHADOW hexp %s: NO_TRADE pre=%.3f (co_dir=%s) reason=%s",
                state.symbol, _hpre, _co_dir, getattr(_hexp_sr, "fallback_reason", ""),
            )
            return

        _agree = (_hdir == _co_dir)
        _atr = float(getattr(indicators, "atr_14", 4.0) or 4.0)
        _live = await self._get_live_entry_price(state.symbol, _hdir)
        _entry = _live if _live else indicators.close
        _sl_mult = float(getattr(_hexp_sr, "co_exec_sl_atr_mult", 0.0) or 0.0)
        _rr = float(getattr(_hexp_sr, "co_exec_rr_min", 0.0) or 0.0)
        if _sl_mult <= 0:
            _sl_mult = 2.0
        if _rr <= 0:
            _rr = 1.2
        _tp_mult = _sl_mult * _rr
        if _hdir == "BUY":
            _sl = _entry - _sl_mult * _atr
            _tp = _entry + _tp_mult * _atr
        else:
            _sl = _entry + _sl_mult * _atr
            _tp = _entry - _tp_mult * _atr
        _grade = getattr(_hexp_sr, "grade", "C")
        _verdict = getattr(_hexp_sr, "verdict", 0.0)

        _sid = await self._signal_publisher.generate_signal_id() if self._signal_publisher else int(time.time() * 1_000_000) % 1_000_000_000
        _sd = SignalData(
            signal_id=_sid,
            task_id=0,
            account_id=(await self._resolve_account_id() or 0),
            symbol=state.symbol,
            time_frame=state.timeframe,
            direction=_hdir,
            entry_price=round(_entry, 5),
            sl_price=round(_sl, 5),
            tp1=round(_tp, 5),
            tp2=0.0,
            lot=0.0,
            confidence=round(float(getattr(_hexp_sr, "confidence", _hpre) or _hpre), 4),
            signal_mode="hexp_shadow",
            indicator_values={
                "adx_14": round(getattr(indicators, "adx_14", 0.0), 2),
                "rsi_14": round(getattr(indicators, "rsi_14", 0.0), 2),
                "atr_14": round(_atr, 2),
                "h1_regime": getattr(indicators, "h1_regime", ""),
                "h1_trend_direction": getattr(indicators, "h1_trend_direction", ""),
                "h1_trend_strength": round(getattr(indicators, "h1_trend_strength", 0.0), 3),
                "hp_score": round(float(getattr(_hexp_sr, "hp_score", 0.0) or 0.0), 3),
                "k": round(float(getattr(_hexp_sr, "k", 0.0) or 0.0), 3),
                "verdict": round(float(_verdict), 3),
                "grade": _grade,
                "co_dir": _co_dir,
                "co_agree": _agree,
                "co_pre_score": round(float(getattr(score_result, "pre_score", 0.0) or 0.0), 4),
                # 2026-08-24 趋势抢跑候选观测：hexp 引擎 phase=ignite+动量同向+pos 中低位时
                # 产出的顺势启动候选（dir/pos/mm/er/squeeze/ignite/verdict/grade），
                # 仅观测不下单，供 _reconcile_hexp_shadow 对比"趋势启动候选 vs 普通信号"胜率。
                "trend_start": getattr(_hexp_sr, "trend_start_candidate", None),
            },
            fallback_reason=getattr(_hexp_sr, "fallback_reason", ""),
            regime=getattr(regime_result, "regime", "UNKNOWN"),
            pre_score=_hpre,
            weight_scheme="HEXP",
            position_in_range=None,
            trace_id=getattr(state, "trace_id", None),
            produced_at=datetime.now(timezone.utc).isoformat(),
            created_at=datetime.now(timezone.utc),
            zone_level=zone_level,
            zone_type="",
            zone_strength=0.0,
            zone_tp_level=0.0,
            ai_sl_mult=_sl_mult,
            ai_tp_mult=_tp_mult,
            suggested_lot_ratio=float(getattr(_hexp_sr, "co_exec_lot_mult", 1.0) or 1.0),
            entry_trigger_wait=0,
        )
        # 关键：to_stream=False → 仅落 PG，不进 signal:stream → 不触发风控/桥
        if self._signal_publisher is not None:
            await self._signal_publisher.publish(_sd, to_stream=False)
        logger.info(
            "SHADOW hexp %s: dir=%s pre=%.3f sl_mult=%.2f tp_mult=%.2f grade=%s "
            "| co_dir=%s agree=%s id=%d",
            state.symbol, _hdir, _hpre, _sl_mult, _tp_mult, _grade, _co_dir, _agree, _sid,
        )

        # 节流触发评估对账（每 60s 一次），把落库信号模拟成"假设成交"结果
        _now = time.time()
        if _now - getattr(self, "_hexp_shadow_last_recon", 0.0) >= 60:
            self._hexp_shadow_last_recon = _now
            await self._reconcile_hexp_shadow()

    async def _reconcile_hexp_shadow(self):
        """用历史 K 线把 hexp_shadow 信号模拟成 SL/TP 命中，落 hexp_shadow_eval。

        准确率口径（不依赖真实下单）：
          * outcome: 'win'  = 未来 N 根 M5 内 TP 先于 SL 被触达
                    'loss' = SL 先于 TP 被触达
                    'expired' = 窗口内两者皆未触达（计入方向命中，但不计 win/loss）
          * dir_hit: 窗口内价格朝预测方向移动 >= dir_atr_ratio*ATR（独立于 SL/TP 的方向准确率）
        """
        if self._db is None or not self._db.is_initialized:
            return
        try:
            _eval_bars = int(await self._config.get_int("hexp.shadow.eval_bars", 60)) if self._config else 60
            _dir_ratio = float(await self._config.get_float("hexp.shadow.dir_atr_ratio", 0.5)) if self._config else 0.5
            _horizon = timedelta(minutes=(_eval_bars + 2) * 5)
            _rows = await self._db.fetch(
                """
                SELECT s.signal_id, s.symbol, s.time_frame, s.signal_dir,
                       s.entry_price, s.sl_price, s.tp1, s.created_at,
                       (s.indicator_values->>'atr_14')::float AS atr
                FROM hcm_signal.signals s
                WHERE s.signal_mode = 'hexp_shadow'
                  AND s.signal_dir IN ('BUY','SELL')
                  AND s.created_at < NOW() - $1::interval
                  AND NOT EXISTS (
                      SELECT 1 FROM hcm_signal.hexp_shadow_eval e
                      WHERE e.signal_id = s.signal_id
                  )
                ORDER BY s.created_at ASC
                LIMIT 500
                """,
                _horizon,
            )
            for _r in _rows:
                _sid = _r["signal_id"]
                _sym = _r["symbol"]
                _tf = _r["time_frame"]
                _dir = _r["signal_dir"]
                _entry = float(_r["entry_price"])
                _sl = float(_r["sl_price"])
                _tp = float(_r["tp1"])
                _atr = float(_r["atr"] or 4.0)
                _created = _r["created_at"]
                _dir_pips = _dir_ratio * _atr
                _kl = await self._db.fetch(
                    """
                    SELECT open_time, high, low, close
                    FROM hcm_market.klines
                    WHERE symbol = $1 AND time_frame = $2
                      AND open_time > $3
                    ORDER BY open_time ASC
                    LIMIT $4
                    """,
                    _sym, _tf, _created, _eval_bars,
                )
                _outcome = "expired"
                _pnl_r = 0.0
                _dir_hit = False
                for _b in _kl:
                    _high = float(_b["high"])
                    _low = float(_b["low"])
                    _close = float(_b["close"])
                    if _dir == "BUY":
                        if _low <= _sl:
                            _outcome = "loss"
                            _pnl_r = -1.0
                            break
                        if _high >= _tp:
                            _outcome = "win"
                            _pnl_r = (_tp - _entry) / max(_entry - _sl, 1e-9)
                            break
                        if _close >= _entry + _dir_pips:
                            _dir_hit = True
                    else:
                        if _high >= _sl:
                            _outcome = "loss"
                            _pnl_r = -1.0
                            break
                        if _low <= _tp:
                            _outcome = "win"
                            _pnl_r = (_entry - _tp) / max(_sl - _entry, 1e-9)
                            break
                        if _close <= _entry - _dir_pips:
                            _dir_hit = True
                await self._db.execute(
                    """
                    INSERT INTO hcm_signal.hexp_shadow_eval
                        (signal_id, symbol, time_frame, signal_dir, entry_price,
                         sl_price, tp1, created_at, eval_at, horizon_bars,
                         outcome, dir_hit, pnl_r)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW(),$9,$10,$11,$12)
                    ON CONFLICT (signal_id) DO NOTHING
                    """,
                    _sid, _sym, _tf, _dir, _entry, _sl, _tp, _created,
                    _eval_bars, _outcome, _dir_hit, round(_pnl_r, 4),
                )
            # ── 趋势启动候选影子评估（2026-08-26）──
            # 读生产信号里落库的 trend_start_candidate（indicator_values._hexp.trend_start_candidate），
            # 用 hexp 默认 SL/TP 参数（sl_atr_mult=2.0 / rr_min=1.5）模拟"在趋势启动初期
            # 轻仓顺势下单"的假设成交 win/loss，落 hexp_shadow_eval（复用 signal_id 唯一键，
            # 生产信号与 hexp_shadow 信号 signal_id 同序列、不冲突）。
            # 供「趋势启动下单」在开 hexp.trend_start_order_enabled 真下单前评估胜率/盈亏比。
            _ts_rows = await self._db.fetch(
                """
                SELECT s.signal_id, s.symbol, s.time_frame, s.created_at,
                       (s.indicator_values->'_hexp'->>'trend_start_candidate')::jsonb AS ts
                FROM hcm_signal.signals s
                WHERE s.indicator_values->'_hexp'->>'trend_start_candidate' IS NOT NULL
                  AND s.created_at < NOW() - $1::interval
                  AND NOT EXISTS (
                      SELECT 1 FROM hcm_signal.hexp_shadow_eval e
                      WHERE e.signal_id = s.signal_id
                  )
                ORDER BY s.created_at ASC
                LIMIT 500
                """,
                _horizon,
            )
            for _r in _ts_rows:
                _ts = _r["ts"]
                if not isinstance(_ts, dict):
                    continue
                _ts_dir = _ts.get("dir")
                _ts_close = float(_ts.get("close") or 0.0)
                _ts_atr = float(_ts.get("atr") or 4.0)
                if _ts_dir not in ("BUY", "SELL") or _ts_close <= 0 or _ts_atr <= 0:
                    continue
                # 趋势启动单 SL/TP 参数（与 hexp.exec.sl_atr_mult=2.0 / rr_min=1.5 一致）
                _ts_sl_mult = 2.0
                _ts_rr = 1.5
                _ts_tp_mult = _ts_sl_mult * _ts_rr
                if _ts_dir == "BUY":
                    _ts_sl = _ts_close - _ts_sl_mult * _ts_atr
                    _ts_tp = _ts_close + _ts_tp_mult * _ts_atr
                else:
                    _ts_sl = _ts_close + _ts_sl_mult * _ts_atr
                    _ts_tp = _ts_close - _ts_tp_mult * _ts_atr
                _ts_kl = await self._db.fetch(
                    """
                    SELECT open_time, high, low, close
                    FROM hcm_market.klines
                    WHERE symbol = $1 AND time_frame = $2
                      AND open_time > $3
                    ORDER BY open_time ASC
                    LIMIT $4
                    """,
                    _r["symbol"], _r["time_frame"], _r["created_at"], _eval_bars,
                )
                _ts_outcome = "expired"
                _ts_pnl_r = 0.0
                _ts_dir_hit = False
                for _b in _ts_kl:
                    _hi = float(_b["high"])
                    _lo = float(_b["low"])
                    _cl = float(_b["close"])
                    if _ts_dir == "BUY":
                        if _lo <= _ts_sl:
                            _ts_outcome = "loss"
                            _ts_pnl_r = -1.0
                            break
                        if _hi >= _ts_tp:
                            _ts_outcome = "win"
                            _ts_pnl_r = (_ts_tp - _ts_close) / max(_ts_close - _ts_sl, 1e-9)
                            break
                        if _cl >= _ts_close + _dir_ratio * _ts_atr:
                            _ts_dir_hit = True
                    else:
                        if _hi >= _ts_sl:
                            _ts_outcome = "loss"
                            _ts_pnl_r = -1.0
                            break
                        if _lo <= _ts_tp:
                            _ts_outcome = "win"
                            _ts_pnl_r = (_ts_close - _ts_tp) / max(_ts_sl - _ts_close, 1e-9)
                            break
                        if _cl <= _ts_close - _dir_ratio * _ts_atr:
                            _ts_dir_hit = True
                await self._db.execute(
                    """
                    INSERT INTO hcm_signal.hexp_shadow_eval
                        (signal_id, symbol, time_frame, signal_dir, entry_price,
                         sl_price, tp1, created_at, eval_at, horizon_bars,
                         outcome, dir_hit, pnl_r)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW(),$9,$10,$11,$12)
                    ON CONFLICT (signal_id) DO NOTHING
                    """,
                    _r["signal_id"], _r["symbol"], _r["time_frame"], _ts_dir, _ts_close,
                    _ts_sl, _ts_tp, _r["created_at"],
                    _eval_bars, _ts_outcome, _ts_dir_hit, round(_ts_pnl_r, 4),
                )
            logger.info("SHADOW hexp reconcile: evaluated %d shadow signals, %d trend-start candidates",
                        len(_rows), len(_ts_rows))
        except Exception as _e:
            logger.warning("SHADOW hexp reconcile failed: %s", _e)

    # ── K-line Fetching ─────────────────────────

    async def _fetch_klines(
        self, symbol: str, timeframe: str, limit: int = 100
    ) -> list[dict]:
        """Fetch recent klines from PostgreSQL, merged with Redis live bar.

        PostgreSQL query first. Then, if Redis has a live bar with open_time
        newer than the last PG bar, it is appended to the list.

        Args:
            symbol: Trading symbol.
            timeframe: Timeframe string.
            limit: Max bars to fetch.

        Returns:
            List of kline dicts sorted by open_time ascending.
        """
        if self._db is None or not self._db.is_initialized:
            return []

        try:
            rows = await self._db.fetch(
                """SELECT open_time, open, high, low, close, tick_volume, spread, real_volume
                   FROM hcm_market.klines
                   WHERE symbol=$1 AND time_frame=$2 AND open_time <= now()
                   ORDER BY open_time DESC LIMIT $3""",
                symbol, timeframe, limit,
            )
            # Reverse to ascending order for indicator calculation
            klines = [
                {
                    "open_time": r["open_time"],
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "tick_volume": int(r["tick_volume"]),
                    # A 组: spread / real_volume 列（历史 NULL → 0，bar_quality 按中性处理）
                    "spread": float(r["spread"]) if r["spread"] is not None else 0.0,
                    "real_volume": int(r["real_volume"]) if r["real_volume"] is not None else 0,
                }
                for r in reversed(rows)
            ]
            # ── A 层: 合并 Redis 实时 bar（消除一根 bar 滞后）──
            # 桥 write_price_to_redis 每 tick 把当前未收盘 bar 实时 OHLC 写入
            # hcm:config:v2[latest_kline:{symbol}:{tf}]；此处合并进序列，使
            # closes[-1]/highs[-1]/lows[-1] 反映实时价而非上一根收盘。
            # 一处改动 → 指标/评分/live_adx/H1 上下文全链路实时化。
            try:
                import json as _json

                def _ot_epoch(ot):
                    if ot is None:
                        return 0
                    if isinstance(ot, (int, float)):
                        return int(ot)
                    try:
                        return int(ot.timestamp())
                    except Exception:
                        return 0

                if self._redis is not None and self._redis.is_initialized:
                    raw = await self._redis.hget(
                        "hcm:config:v2", f"latest_kline:{symbol}:{timeframe}"
                    )
                    if raw:
                        lb = _json.loads(raw)
                        lb_open = _ot_epoch(lb.get("open_time"))
                        pg_last = _ot_epoch(klines[-1]["open_time"]) if klines else 0
                        if lb_open and lb_open >= pg_last:
                            live_bar = {
                                "open_time": lb.get("open_time"),
                                "open": float(lb["open"]),
                                "high": float(lb["high"]),
                                "low": float(lb["low"]),
                                "close": float(lb["close"]),
                                "tick_volume": int(lb.get("tick_volume", 0) or 0),
                                # Tier1: 实时 bar 透传 spread / real_volume，
                                # 使 compute_bar_quality 的 spread_q / vol_q 不再失明/失真
                                "spread": float(lb.get("spread", 0.0) or 0.0),
                                "real_volume": int(lb.get("real_volume", 0) or 0),
                            }
                            if lb_open == pg_last:
                                # 同一根（刚收盘）bar：用实时 OHLC 覆盖，质量由
                                # compute_bar_quality 在完整数据上重算，可直接用于 F6
                                klines[-1] = live_bar
                            else:
                                # 新周期刚开的"正在形成 bar"：打标记，供 _produce_signal
                                # 把 F6 质量闸门对齐到上一根"已收盘完整 bar"（Tier2）
                                live_bar["is_live_forming"] = True
                                klines.append(live_bar)
                            logger.debug(
                                "Live bar merged %s/%s open=%d close=%.3f",
                                symbol, timeframe, lb_open, live_bar["close"],
                            )
            except Exception as exc:
                logger.debug("live bar merge skipped (%s/%s): %s", symbol, timeframe, exc)
            # ── Bar 质量分（计算层, 纯函数, 默认不影响信号）──
            # 为每根 bar 附加 quality/vol_q/spread_q/body_ratio/pin/outlier 字段；
            # 当前信号闸门不消费这些字段，仅作数据增强与后续质量过滤的前提。
            compute_bar_quality(klines)
            return klines
        except Exception as exc:
            logger.warning("Kline fetch failed for %s/%s: %s", symbol, timeframe, exc)
            return []

    # ── P0 (2026-07-15): confluence zone for precise entry (informational) ──

    @staticmethod
    def _prev_day_hlc(klines: list) -> tuple:
        """Derive previous calendar day H/L/C from klines (any time-frame).

        D1 klines are not collected, so daily pivots are computed from the
        bars of the previous UTC day found in the supplied klines. H1/M15/M5
        all carry enough history to cover the prior day; whichever time-frame
        _compute_entry_zone falls back to is fine. Returns (None, None, None)
        if the previous day is not present in the supplied window.
        """
        if not klines:
            return (None, None, None)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        prev_bars = []
        for k in klines:
            ot = k.get("open_time")
            if ot is None:
                continue
            try:
                d = ot.date() if hasattr(ot, "date") else None
            except Exception:
                d = None
            if d == yesterday:
                prev_bars.append(k)
        if not prev_bars:
            return (None, None, None)
        prev_h = max(float(k["high"]) for k in prev_bars)
        prev_l = min(float(k["low"]) for k in prev_bars)
        prev_c = float(prev_bars[-1]["close"])
        return (prev_h, prev_l, prev_c)

    async def _fetch_and_build_zones(
        self, state: "SymbolState", current_price: float
    ) -> list:
        """Fetch klines and build the full confluence-zone list for the symbol.

        Shared by _compute_entry_zone (nearest zone) and _compute_target_zone
        (next profit-direction zone for TP anchoring). Returns [] on any failure
        so callers never block signal production.
        """
        # H1 preferred, falls back to M15 then M5. Dynamic limit covers the
        # previous UTC day for daily-pivot derivation.
        tf_chain = ("H1", "M15", "M5")
        tf_limit = {"H1": 200, "M15": 400, "M5": 600}
        tf_params = {
            "H1":  {"cm": 0.3, "ms": 3},
            "M15": {"cm": 0.5, "ms": 3},
            "M5":  {"cm": 1.0, "ms": 2},
        }
        try:
            klines = None
            tf_used = None
            for tf in tf_chain:
                k = await self._fetch_klines(state.symbol, tf, limit=tf_limit.get(tf, 200))
                if len(k) >= 14:
                    klines, tf_used = k, tf
                    break
            if klines is None:
                logger.debug("Zone: no usable time-frame for %s", state.symbol)
                return []
            highs = np.array([float(x["high"]) for x in klines], dtype=np.float64)
            lows = np.array([float(x["low"]) for x in klines], dtype=np.float64)
            closes = np.array([float(x["close"]) for x in klines], dtype=np.float64)
            atr = IndicatorCalculator.compute_atr(highs, lows, closes, period=14)

            params = tf_params.get(tf_used, {"cm": 0.3, "ms": 3})
            prev_h, prev_l, prev_c = self._prev_day_hlc(klines)
            pivots = compute_pivots(prev_h, prev_l, prev_c) if prev_h is not None else {}
            sr = compute_support_resistance(highs, lows, atr, cluster_mult=params["cm"])
            rounds = compute_round_levels(current_price)
            session = [(prev_h, "SESSION"), (prev_l, "SESSION")] if prev_h is not None else []

            zones = build_confluence_zones(
                pivots, sr, rounds, session, current_price,
                atr=atr, cluster_mult=params["cm"], min_strength=params["ms"],
            )
            logger.debug("Zone for %s via %s (cm=%.1f ms=%d): %d zones",
                         state.symbol, tf_used, params["cm"], params["ms"], len(zones))
            return zones
        except Exception as exc:
            logger.warning("Zone build failed for %s: %s", state.symbol, exc)
            return []

    async def _compute_target_zone(
        self, state: "SymbolState", current_price: float, direction: str
    ) -> float:
        """Next structural zone in the profit direction — TP anchor point.

        BUY  → nearest RESISTANCE/PIVOT (fallback: any level) ABOVE price.
        SELL → nearest SUPPORT/PIVOT (fallback: any level) BELOW price.
        Returns 0.0 when no usable zone (bridge falls back to ATR TP).
        """
        try:
            zones = await self._fetch_and_build_zones(state, current_price)
            if not zones:
                return 0.0
            if direction == "BUY":
                cands = [z for z in zones if z.level > current_price
                         and z.ztype in ("RESISTANCE", "PIVOT")]
                if not cands:
                    cands = [z for z in zones if z.level > current_price]
            else:
                cands = [z for z in zones if z.level < current_price
                         and z.ztype in ("SUPPORT", "PIVOT")]
                if not cands:
                    cands = [z for z in zones if z.level < current_price]
            if not cands:
                return 0.0
            tgt = min(cands, key=lambda z: abs(z.level - current_price))
            return float(tgt.level)
        except Exception as exc:
            logger.warning("Target zone failed for %s: %s", state.symbol, exc)
            return 0.0

    async def _compute_entry_zone(
        self, state: "SymbolState", current_price: float
    ) -> tuple:
        """Compute the nearest confluence zone to current price.

        Returns (zone_level, zone_type, zone_strength). Purely informational
        in P0 — does NOT alter execution. On any failure returns
        (0.0, "", 0) so signal production is never blocked.
        """
        try:
            zones = await self._fetch_and_build_zones(state, current_price)
            if not zones:
                return (0.0, "", 0)
            nearest = min(zones, key=lambda z: abs(z.level - current_price))
            return (nearest.level, nearest.ztype, nearest.strength)
        except Exception as exc:
            logger.warning("Zone computation failed for %s: %s", state.symbol, exc)
            return (0.0, "", 0)

    async def _resolve_entry_zone_for_direction(
        self, state: "SymbolState", current_price: float,
        direction: str, atr: float,
        max_gap_mult: float = 1.5,
    ) -> Optional[tuple]:
        """方案B (2026-07-23): 方向对齐且偏近的入场确认 zone 解析。

        _compute_entry_zone 返回「离现价最近的 zone」（方向不感知），仅用于
        打分(Tier2)/prompt 上下文。但它会让 SELL 误选下方几十点 PIVOT 作入场
        等待位 → 桥等跌破远低于现价的支撑才空，牺牲利润/超时丢单。

        本方法按交易方向筛选真正的入场确认位：
          - SELL → 现价上方偏近的 RESISTANCE/PIVOT（等反弹/触达阻力做空）
          - BUY  → 现价下方偏近的 SUPPORT/PIVOT（等回踩/触达支撑做多）
        对向 zone（如 SELL 的下方 PIVOT）不在此返回——它本就是 _compute_target_zone
        的 TP 锚点，不应误作入场等待位。

        仅当 |zone - price| <= atr*1.5（近端）才返回；过远（远端阻力）或对向
        zone 均返回 None，调用方应市价成交（不延迟、不牺牲利润）。
        返回 (level, ztype, strength) 或 None。
        """
        if direction not in ("BUY", "SELL"):
            return None
        try:
            zones = await self._fetch_and_build_zones(state, current_price)
            if not zones:
                return None
            if direction == "SELL":
                cands = [z for z in zones
                         if z.ztype in ("RESISTANCE", "PIVOT")
                         and z.level >= current_price]
            else:
                cands = [z for z in zones
                         if z.ztype in ("SUPPORT", "PIVOT")
                         and z.level <= current_price]
            if not cands:
                return None
            near = min(cands, key=lambda z: abs(z.level - current_price))
            gap = abs(near.level - current_price)
            _max_gap = (atr * max_gap_mult) if atr and atr > 0 else 30.0
            if gap > _max_gap:
                # 方向对齐但离现价过远（如远处 RESISTANCE）→ 不值得等触达，
                # 返回 None 让调用方直接市价成交，避免牺牲利润/超时丢单。
                logger.info(
                    "Entry zone %s/%s %s skipped (too far): %.2f gap=%.2f > max=%.2f",
                    state.symbol, state.timeframe, direction,
                    near.level, gap, _max_gap,
                )
                return None
            return (float(near.level), near.ztype, near.strength)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Entry zone resolve failed for %s/%s: %s",
                           state.symbol, direction, exc)
            return None

    # ── Prompt Builder ──────────────────────────

    def _build_prompt(
        self,
        symbol: str,
        timeframe: str,
        indicators: IndicatorResults,
        regime: RegimeResult,
        score: ScoreResult,
        zone_level: float = 0.0,
        zone_type: str = "",
        zone_strength: int = 0,
        active_model: str = "ai_dynamic",
    ) -> str:
        """Build a compact, data-dense DeepSeek prompt.

        Priority: user-configured Redis template → hardcoded fallback.
        Template variables: {symbol} {timeframe} {regime} {regime_strength}
        {rsi} {macd} {adx} {bbw} {pct_b} {stoch_k} {atr} {bar_open} {close}
        {bar_momentum} {ma_alignment} {pre_dir} {pre_score} {trend_note} {momentum_note}
        """
        regime_label = regime.regime.value
        trend_note = ""
        if regime_label == "TREND":
            trend_note = "TREND: align signal with MACD/MA. Reject counter-trend."
        elif regime_label == "RANGE":
            trend_note = "RANGE: only reverse at extreme %b."
        elif regime_label == "PRE_TREND":
            trend_note = "PRE_TREND: prefer breakout with momentum>1.5."

        momentum_note = ""
        bar_mom = float(getattr(indicators, 'bar_momentum', 0) or 0)
        if bar_mom > 2.0:
            momentum_note = "STRONG momentum — follow direction."
        elif bar_mom < 0.5:
            momentum_note = "WEAK momentum — noise, prefer NO_TRADE."

        vars_dict = {
            "symbol": symbol, "timeframe": timeframe,
            "regime": regime_label, "regime_strength": f"{regime.strength:.2f}",
            "rsi": f"{indicators.rsi_14:.0f}", "macd": f"{indicators.macd:.2f}",
            "adx": f"{indicators.adx_14:.0f}", "bbw": f"{indicators.bbw:.3f}",
            "pct_b": f"{indicators.pct_b:.3f}", "stoch_k": f"{indicators.stoch_k:.0f}",
            "atr": f"{indicators.atr_14:.1f}", "bar_open": f"{indicators.bar_open:.2f}",
            "close": f"{indicators.close:.2f}", "bar_momentum": f"{bar_mom:.2f}",
            "ma_alignment": str(indicators.ma_alignment),
            "pre_dir": score.direction, "pre_score": f"{score.pre_score:.3f}",
            "trend_note": trend_note, "momentum_note": momentum_note,
            # P2a: surface ④ Zone structure to the AI so it can vet direction
            "zone_level": f"{zone_level:.2f}", "zone_type": zone_type,
            "zone_strength": f"{zone_strength}",
        }

        # 1. Try model-specific template, then global fallback
        user_template = self._resolve_prompt_template(active_model)
        if user_template:
            try:
                return user_template.format(**vars_dict)
            except (KeyError, ValueError) as exc:
                logger.warning("User prompt template error: %s — falling back to default", exc)

        # 2. Hardcoded fallback (compact, token-efficient, always safe)
        zone_ctx = (
            f"ZONE level={zone_level:.2f} type={zone_type} strength={zone_strength}\n"
            f"Structure rule: if price is far from the nearest zone OR the zone "
            f"conflicts with the direction (e.g. BUY but price below a RESISTANCE), "
            f"prefer NO_TRADE.\n"
        ) if zone_level > 0 else ""
        return (
            f"XAUUSD {timeframe} | {regime_label} r={regime.strength:.2f}\n"
            f"RSI={indicators.rsi_14:.0f} MACD={indicators.macd:.2f} ADX={indicators.adx_14:.0f} "
            f"%%b={indicators.pct_b:.3f} StochK={indicators.stoch_k:.0f}\n"
            f"ATR14={indicators.atr_14:.1f} bar_open={indicators.bar_open:.2f} close={indicators.close:.2f} "
            f"bar_momentum={bar_mom:.2f} (range/ATR)\n"
            f"MA={indicators.ma_alignment} pre_dir={score.direction} pre_score={score.pre_score:.3f}\n"
            f"{zone_ctx}"
            f"{trend_note} {momentum_note}\n"
            f"Output JSON: "
            f'{{"direction":"BUY|SELL|NO_TRADE","confidence":0-1,'
            f'"sl_atr_mult":1.5-2.5,"tp_atr_mult":2.0-4.0,'
            f'"reason":"<50chars","risk":"<30chars"}}'
        )

    # ── Config ──────────────────────────────────

    def _resolve_prompt_template(self, active_model: str) -> Optional[str]:
        """Resolve the user prompt template for ``active_model``.

        Fallback chain: model-specific key → legacy global
        ``signal_tower.prompt.user_prompt_template`` → None (hardcoded fallback).
        """
        per_model = self._prompt_templates.get(active_model)
        if per_model:
            return per_model
        return getattr(self, "_prompt_template", None)

    def _system_prompt_for(self, active_model: str) -> Optional[str]:
        """Resolve the system prompt for ``active_model``.

        Fallback chain: model-specific key → legacy global
        ``signal_tower.prompt.system_prompt`` → None (scheduler hardcoded default).
        """
        per_model = self._system_prompts.get(active_model)
        if per_model:
            return per_model
        return getattr(self, "_system_prompt", None)

    def _mechanism_profile(self, active_model: str) -> dict:
        """按 active_model 返回机制配置（评分引擎 / 是否启用 co_source 增强）。"""
        return MECHANISM_PROFILES.get(active_model, MECHANISM_PROFILES["default"])

    async def _detect_active_model(self) -> str:
        """Detect which signal model is currently active for prompt routing.

        Returns one of ``"hexp"``, ``"manual"``:
          - manual: signal_tower.mode == "manual"
          - hexp:   其余全部情况（含 signal.active_model == "hexp"）

        【2026-08-28 co_source 清除】双源信号模式整体下线，信号源只剩 HEXP 与 manual。
        原逻辑在 active_model 既非 hexp 也非 co_source（含异常）时回退 co_source；
        现统一回退 **hexp**（生产唯一引擎），避免任何路径再落入已删除的 co_source 分支。
        """
        try:
            mode = (await self._config.get("signal_tower.mode", "hexp")) or "hexp"
            mode = mode.strip().lower()
            if mode == "manual":
                return "manual"
            active = (await self._config.get("signal.active_model", "hexp")) or "hexp"
            active = active.strip().lower()
            if active == "manual":
                return "manual"
        except Exception as exc:
            logger.warning("Active model detection failed: %s → hexp (default)", exc)
        return "hexp"

    async def _resolve_account_id(self) -> Optional[int]:
        """Resolve the active master account_id from broker accounts.

        根治：每 30s 周期重解析（而非永久缓存），使在 UI 切换/停用主号后，
        信号生产目标账户即时跟随（保存即能用，切换账户不掉信号）。
        查询失败时沿用上次缓存值，避免 DB 抖动导致信号中断。

        Returns:
            account_id (int) on success, None if no active master found
            and DB is unavailable.
        """
        _now = time.time()
        if self._account_id is not None and (_now - self._account_id_resolved_at) < 30:
            return self._account_id

        if self._db is not None and self._db.is_initialized:
            try:
                row = await self._db.fetchrow(
                    """SELECT account_id FROM hcm_broker.accounts
                       WHERE is_active = true AND account_type = 'master'
                       ORDER BY account_id LIMIT 1"""
                )
                if row is not None:
                    self._account_id = int(row["account_id"])
                    self._account_id_resolved_at = _now
                    logger.info("Resolved account_id=%d from broker accounts", self._account_id)
                    return self._account_id
                # 查到 0 行（无 active master）：清空缓存，fail-closed
                self._account_id = None
            except Exception as exc:
                logger.warning("Failed to query broker accounts: %s", exc)
                # 查询失败但已有缓存值时沿用，避免抖动
                if self._account_id is not None:
                    return self._account_id

        if self._account_id is not None:
            return self._account_id
        # 账户解析失败（DB 不可用 / 无 active master）：fail-closed，
        # 返回 None 让上游停止发射账户不明的信号，而非静默转发到停用种子账户。
        logger.error("No active master account resolved — refusing to emit signal with unknown account_id")
        return None

    async def _get_manual_regime_score(self, symbol: str) -> Optional[int]:
        """Get manual regime score from config if set.

        Args:
            symbol: Trading symbol.

        Returns:
            Manual score or None.
        """
        if self._config is None:
            return None

        try:
            raw = await self._config.get_with_resolution(
                "manual_regime_score", symbol=symbol
            )
            if raw and raw.strip():
                return int(raw)
        except Exception:
            pass

        return None

    async def load_config(self) -> None:
        """Load scheduler parameters from config_provider.

        P0: indicator periods + regime detection thresholds + market guard
        configs are now wired through config_provider so they can be tuned
        live (no restart). All runtime parameters follow the same
        config-center governance as O2.
        """
        if self._config is None:
            return

        # D1 (2026-08-04): per-loader 隔离。任一子加载器异常仅告警该加载器，
        # 不再整批跳过（避免一个键缺失导致全部配置停留在旧值且无感知）。
        async def _safe_load(name: str, _coro):
            try:
                await _coro
            except Exception as _e:
                logger.error(
                    "Scheduler sub-config load FAILED [%s]: %s — using last good value",
                    name, _e,
                )

        try:
            self._config_reload_interval = max(
                5,
                await self._config.get_int(
                    "signal_tower.config_reload_interval",
                    DEFAULT_CONFIG_RELOAD_INTERVAL_SECONDS,
                ),
            )
        except Exception as exc:
            logger.error("config_reload_interval load failed (using default %ds): %s",
                         DEFAULT_CONFIG_RELOAD_INTERVAL_SECONDS, exc)
            self._config_reload_interval = DEFAULT_CONFIG_RELOAD_INTERVAL_SECONDS

        await _safe_load("_load_indicator_config", self._load_indicator_config())
        await _safe_load("_load_regime_config", self._load_regime_config())
        await _safe_load("scoring_engine.load_config", self._scoring_engine.load_config())
        if hasattr(self, "_h1_classifier"):
            await _safe_load("h1_classifier.load_config", self._h1_classifier.load_config())
        # 【2026-08-28 co_source 清除】原 co_source.load_config 热重载已移除
        # （双源模式下线，实例不再存在）。
        # BUG-16 修复：和乘幂引擎接入 30s 热重载链（此前唯一未注册的引擎，
        # 改 hexp.* 参数须重启信号塔才生效，违背"保存即热生效"）。
        if hasattr(self, "_hexp_engine"):
            await _safe_load("hexp_engine.load_config", self._hexp_engine.load_config())
        if hasattr(self, "_force_close"):
            await _safe_load("force_close.load_config", self._force_close.load_config())
        # ── Phase 0: 微观状态机 + 精准买点分 配置热加载（shadow）──
        if hasattr(self, "_micro_state"):
            await _safe_load("micro_state.load_config", self._micro_state.load_config())
        if hasattr(self, "_precision_entry"):
            await _safe_load("precision_entry.load_config", self._precision_entry.load_config())
        if hasattr(self, "_manual_mode"):
            await _safe_load("manual_mode.load_config", self._manual_mode.load_config())
        if self._watchdog:
            await _safe_load("watchdog.load_config", self._watchdog.load_config())
        await _safe_load("_load_prompt_templates", self._load_prompt_templates())
        await _safe_load("_load_symbol_thresholds", self._load_symbol_thresholds())
        await _safe_load("_load_gate_config", self._load_gate_config())
        logger.info(
            "Scheduler config loaded (reload interval=%ds)",
            self._config_reload_interval,
        )

    # ── 共源信号：风险态读取（P1a，安全回退）────────
    async def _read_co_risk_state(self) -> tuple[str, bool, int]:
        """从 Redis 读取共源信号所需的风险态，缺省值安全回退。

        Returns:
            (risk_level, event_window, consecutive_losses)
            - risk_level: low/med/high（默认 low）
            - event_window: 是否处于重大数据窗口（默认 False；P2 批量 AI 写入）
            - consecutive_losses: 连续亏损笔数（默认 0；由成交回库维护）
        """
        risk_level = "low"
        event_window = False
        consec = 0
        redis = getattr(self, "_redis", None)
        if redis is None:
            return risk_level, event_window, consec
        try:
            lvl = await redis.get("hcm:risk:daily_level")
            if lvl:
                _lvl = (lvl.decode() if isinstance(lvl, bytes) else str(lvl)).strip().lower()
                if _lvl in ("low", "med", "high"):
                    risk_level = _lvl
            ev = await redis.get("hcm:risk:event_window")
            if ev:
                event_window = str(ev.decode() if isinstance(ev, bytes) else ev).strip().lower() in ("1", "true", "yes", "on")
            cl = await redis.get("hcm:risk:consecutive_loss")
            if cl:
                try:
                    consec = int(str(cl.decode() if isinstance(cl, bytes) else cl))
                except (ValueError, TypeError):
                    consec = 0
        except Exception as exc:
            logger.debug("CoSource risk-state read failed (using defaults): %s", exc)
        return risk_level, event_window, consec

    # ── 共源信号：风险态周期同步（D6）────────
    async def _periodic_risk_sync(self, interval_sec: int = 300) -> None:
        """D6: 周期从 PG 推算共源信号风险态写入 Redis，使 F3/F5/风险偏移生效。

        每 interval_sec 秒调用 risk_state_sync.sync_risk_state；db/redis 未就绪时
        静默跳过。写入的键与 _read_co_risk_state 解析严格匹配。
        """
        await asyncio.sleep(10)  # 启动后稍候，确保 db/redis 连接就绪
        while True:
            try:
                db = self._db
                redis = self._redis
                if (
                    db is not None
                    and getattr(db, "is_initialized", False)
                    and redis is not None
                    and getattr(redis, "is_initialized", False)
                ):
                    await sync_risk_state(redis, db, config_provider=self._config)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Periodic risk-state sync failed: %s", exc)
            await asyncio.sleep(interval_sec)

    # ── 标注回写（P0：AI 自我发展的「眼睛」）────────
    async def _label_reconcile_loop(self, interval_sec: int = 900) -> None:
        """周期把已平仓成交的盈亏回写 labeled_samples.label。

        每 interval_sec(默认15分钟) 调用 signal_publisher.reconcile_labels：
        补齐历史样本 + 标定新平仓样本，使 local_calibrator / Optuna 有真实
        盈亏数据可用（否则校准层永远冷启动跳过）。db 未就绪时静默跳过。
        """
        await asyncio.sleep(15)  # 启动稍候，确保 publisher/db 依赖就绪
        while True:
            try:
                pub = self._signal_publisher
                if pub is not None and getattr(pub, "_db", None) is not None:
                    n = await pub.reconcile_labels()
                    if n:
                        logger.info("Label reconcile: %d labeled samples ready", n)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Label reconcile loop failed: %s", exc)
            await asyncio.sleep(interval_sec)

    # ── 每日校准快照（P1：校准报告时序）────────
    async def _calibration_daily_loop(self, interval_sec: int = 3600) -> None:
        """周期计算每日校准快照（含历史回填），解锁「校准时序」页展示。

        每小时调用 signal_publisher.reconcile_calibration_daily()：
        仅在有新标注样本时才重算（函数内 skip 判定），首次运行全量回填。
        重算后各体制校准因子/胜率按时序落库 calibration_daily，
        前端「校准时序」页即能回放 AI 自我发展的历史轨迹。
        """
        await asyncio.sleep(45)  # 启动稍候，确保 publisher/db 依赖就绪
        while True:
            try:
                pub = self._signal_publisher
                if pub is not None and getattr(pub, "_db", None) is not None:
                    n = await pub.reconcile_calibration_daily()
                    if not n:
                        continue
                    logger.info("Calibration daily: %d snapshot rows", n)
                    # 读最新校准因子（仅非冷启动且偏离 1.0 的体制）
                    factors = await pub.compute_latest_calib_factors()
                    # 有新快照才重新生成 AI 自然语言诊断（无 key/失败则静默跳过，
                    # web 端点回退到确定性诊断）。诊断在写回前生成，供护栏判 needs_review
                    diag = await pub.generate_calibration_diagnosis()
                    if diag:
                        logger.info(
                            "Calibration AI diagnosis: confidence=%s, review=%s",
                            diag.get("confidence"), diag.get("needs_review"),
                        )
                    # 闭环：把校准因子自动写回 co.calib.*（热重载循环会重读生效）。
                    # 护栏：显式关闭(auto_apply=False)或 AI 建议需人工复核时跳过写回，
                    # 但仍保留快照与诊断供观测。
                    if factors and self._config is not None:
                        auto_apply = await self._config.get_bool(
                            "co.calib.auto_apply", True
                        )
                        needs_review = bool(diag.get("needs_review")) if diag else False
                        if auto_apply and not needs_review:
                            await self._config.set_batch(factors, category="co_source")
                            await pub.mark_calibration_applied()
                            logger.info("Calibration auto-applied to co.calib.*: %s", factors)
                        else:
                            logger.info(
                                "Calibration NOT auto-applied (auto_apply=%s, needs_review=%s): %s",
                                auto_apply, needs_review, factors,
                            )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Calibration daily loop failed: %s", exc)
            await asyncio.sleep(interval_sec)

    # ── AI 每日 KPI 聚合（2026-08-18：方案 B 日表 + 后台每小时聚合）──
    async def _ai_daily_kpi_loop(self, interval_sec: int = 3600) -> None:
        """每小时聚合 4 张观测表 → hcm_ai.daily_kpi（含 AI 真实盈亏贡献）。

        聚合触发方式：后台任务，每小时跑一次；检查"昨天是否已聚合"，
        00:05 后自动补算昨日整日 KPI，历史可回溯（首次运行回填过去 30 天）。
        fail-open：DB 不可用/表缺失静默跳过，不影响交易主循环。
        """
        await asyncio.sleep(60)  # 启动稍候，确保 db 依赖就绪
        # 历史回填窗口（天）：首次运行补算过去 N 天
        _backfill_days = 30
        _last_backfilled = False
        while True:
            try:
                db = self._db
                if db is None or not getattr(db, "is_initialized", False):
                    await asyncio.sleep(interval_sec)
                    continue
                # 1) 历史回填（仅首次）
                if not _last_backfilled:
                    for d in range(_backfill_days, 0, -1):
                        _day = (datetime.now().date() - timedelta(days=d))
                        await self._aggregate_daily_kpi(db, _day)
                    _last_backfilled = True
                # 2) 每日补算：仅当"昨天"已完整过去（now >= 昨天+1天 的 00:05）
                _now = datetime.now()
                _yesterday = (_now.date() - timedelta(days=1))
                if _now.hour >= 0 and _now.minute >= 5:  # 跨日且已过 00:05
                    await self._aggregate_daily_kpi(db, _yesterday)
                # 3) 当天实时聚合（upsert，供前端当日进度查看）
                await self._aggregate_daily_kpi(db, _now.date())
            except Exception as exc:  # noqa: BLE001
                logger.warning("AI daily KPI loop failed: %s", exc)
            await asyncio.sleep(interval_sec)

    async def _aggregate_daily_kpi(self, db, trade_date: date) -> None:
        """聚合单日 KPI 并 upsert 进 hcm_ai.daily_kpi（幂等）。

        AI 盈亏归因：gate_decision.signal_id ↔ orders.signal_id JOIN。
        标记"AI 赋能单"= 该 signal_id 在 gate_decision 中有记录（VETO/UPGRADE/
        ai_opened/source!=none 任一）。其盈亏从 orders.profit 聚合。
        """
        _day_start = datetime.combine(trade_date, datetime.min.time())
        _day_end = _day_start + timedelta(days=1)
        try:
            # ① 系统健康监控（runtime_event）
            _ev = await db.fetchrow(
                """SELECT
                       COUNT(*) FILTER (WHERE event_type='lm_inference') AS lm,
                       COUNT(*) FILTER (WHERE event_type='ds_success') AS ds_ok,
                       COUNT(*) FILTER (WHERE event_type='ds_fail') AS ds_fail,
                       COUNT(*) FILTER (WHERE event_type='ds_timeout') AS ds_to,
                       COUNT(*) FILTER (WHERE event_type='cache_hit') AS ch,
                       COUNT(*) FILTER (WHERE event_type='price_offset_fuse') AS fuse,
                       COUNT(*) FILTER (WHERE event_type='degrade') AS deg
                   FROM hcm_ai.runtime_event
                   WHERE created_at >= $1 AND created_at < $2""",
                _day_start, _day_end,
            )
            # ② 信号分层（gate_decision）
            _gd = await db.fetchrow(
                """SELECT
                       COUNT(*) FILTER (WHERE passed) AS passed,
                       COUNT(*) FILTER (WHERE action='VETO') AS veto,
                       COUNT(*) FILTER (WHERE action='UPGRADE') AS up,
                       COUNT(*) FILTER (WHERE action='DOWNGRADE') AS down,
                       COUNT(*) FILTER (WHERE action='UPGRADE' AND NOT passed) AS opened
                   FROM hcm_ai.gate_decision
                   WHERE created_at >= $1 AND created_at < $2""",
                _day_start, _day_end,
            )
            # HEXP 候选数（从 signals 表估算：signal_mode 含 hexp/co_source 且非 manual_mirror）
            _hp = await db.fetchval(
                """SELECT COUNT(*) FROM hcm_signal.signals
                   WHERE created_at >= $1 AND created_at < $2
                     AND signal_mode NOT LIKE '%manual_mirror%'""",
                _day_start, _day_end,
            ) or 0
            # ③ 交易绩效 + ④ AI 盈亏贡献（JOIN gate_decision ↔ orders）
            _tr = await db.fetchrow(
                """SELECT
                       COUNT(*) FILTER (WHERE o.profit > 0) AS win,
                       COUNT(*) FILTER (WHERE o.profit < 0) AS loss,
                       COUNT(o.order_id) AS total,
                       COALESCE(SUM(o.profit), 0) AS pnl,
                       COUNT(o.order_id) FILTER (WHERE gd.signal_id IS NOT NULL) AS ai_orders,
                       COALESCE(SUM(o.profit) FILTER (WHERE gd.signal_id IS NOT NULL), 0) AS ai_pnl
                   FROM hcm_trading.orders o
                   LEFT JOIN hcm_ai.gate_decision gd ON gd.signal_id = o.signal_id
                   WHERE o.open_time >= $1 AND o.open_time < $2""",
                _day_start, _day_end,
            )
            # 融合来源分布（从 gate_decision.c_ai_meta->>'source'）
            _src = await db.fetchrow(
                """SELECT
                       COUNT(*) FILTER (WHERE gd.signal_id IS NOT NULL AND gd.c_ai_meta->>'source'='fused') AS fused,
                       COUNT(*) FILTER (WHERE gd.signal_id IS NOT NULL AND gd.c_ai_meta->>'source'='lm_only') AS lm_only,
                       COUNT(*) FILTER (WHERE gd.signal_id IS NOT NULL AND gd.c_ai_meta->>'source'='ds_only') AS ds_only
                   FROM hcm_trading.orders o
                   JOIN hcm_ai.gate_decision gd ON gd.signal_id = o.signal_id
                   WHERE o.open_time >= $1 AND o.open_time < $2""",
                _day_start, _day_end,
            )

            _lm = int(_ev["lm"] or 0)
            _ds_ok = int(_ev["ds_ok"] or 0)
            _ds_fail = int(_ev["ds_fail"] or 0)
            _ds_to = int(_ev["ds_to"] or 0)
            _ch = int(_ev["ch"] or 0)
            _fuse = int(_ev["fuse"] or 0)
            _deg = int(_ev["deg"] or 0)
            _ds_calls = _ds_ok + _ds_fail + _ds_to
            _passed = int(_gd["passed"] or 0)
            _veto = int(_gd["veto"] or 0)
            _up = int(_gd["up"] or 0)
            _down = int(_gd["down"] or 0)
            _opened = int(_gd["opened"] or 0)
            _win = int(_tr["win"] or 0)
            _loss = int(_tr["loss"] or 0)
            _total = int(_tr["total"] or 0)
            _pnl = float(_tr["pnl"] or 0)
            _ai_orders = int(_tr["ai_orders"] or 0)
            _ai_pnl = float(_tr["ai_pnl"] or 0)
            _non_ai_pnl = round(_pnl - _ai_pnl, 2)
            _ai_ratio = round(_ai_pnl / _pnl, 4) if _pnl not in (0, 0.0) else 0.0
            _fused = int(_src["fused"] or 0)
            _lm_only = int(_src["lm_only"] or 0)
            _ds_only = int(_src["ds_only"] or 0)

            await db.execute(
                """INSERT INTO hcm_ai.daily_kpi
                   (trade_date, lm_inferences, ds_calls, ds_success, ds_fail, ds_timeout,
                    cache_hits, fuse_events, degrade_events, hp_candidates, ai_passed,
                    ai_vetoed, ai_upgraded, ai_downdgraded, ai_opened, total_orders,
                    total_pnl, win_orders, loss_orders, ai_enhanced_orders, ai_enhanced_pnl,
                    non_ai_pnl, ai_contrib_ratio, fused_orders, lm_only_orders, ds_only_orders,
                    updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,
                           $20,$21,$22,$23,$24,$25,$26, now())
                   ON CONFLICT (trade_date) DO UPDATE SET
                       lm_inferences=$2, ds_calls=$3, ds_success=$4, ds_fail=$5, ds_timeout=$6,
                       cache_hits=$7, fuse_events=$8, degrade_events=$9, hp_candidates=$10,
                       ai_passed=$11, ai_vetoed=$12, ai_upgraded=$13, ai_downdgraded=$14,
                       ai_opened=$15, total_orders=$16, total_pnl=$17, win_orders=$18,
                       loss_orders=$19, ai_enhanced_orders=$20, ai_enhanced_pnl=$21,
                       non_ai_pnl=$22, ai_contrib_ratio=$23, fused_orders=$24,
                       lm_only_orders=$25, ds_only_orders=$26, updated_at=now()""",
                trade_date, _lm, _ds_calls, _ds_ok, _ds_fail, _ds_to,
                _ch, _fuse, _deg, int(_hp), _passed, _veto, _up, _down, _opened,
                _total, round(_pnl, 2), _win, _loss, _ai_orders, round(_ai_pnl, 2),
                _non_ai_pnl, _ai_ratio, _fused, _lm_only, _ds_only,
            )
            logger.info(
                "AI daily KPI aggregated: date=%s orders=%d pnl=%.2f ai_orders=%d ai_pnl=%.2f",
                trade_date, _total, _pnl, _ai_orders, _ai_pnl,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Aggregate daily KPI failed for %s: %s", trade_date, exc)

    # ── Config Loaders (P0) ─────────────────────

    async def _load_indicator_config(self) -> None:
        """Rebuild IndicatorCalculator with periods read from config_provider.

        IndicatorCalculator is stateless across bars, so recreating it on each
        config load is safe and keeps it in sync with live-tuned periods.
        """
        if self._config is None:
            return
        try:
            periods = {
                "rsi_period": await self._config.get_int("indicator_rsi_period", DEFAULT_RSI_PERIOD),
                "macd_fast": await self._config.get_int("indicator_macd_fast", DEFAULT_MACD_FAST),
                "macd_slow": await self._config.get_int("indicator_macd_slow", DEFAULT_MACD_SLOW),
                "macd_signal": await self._config.get_int("indicator_macd_signal", DEFAULT_MACD_SIGNAL),
                "adx_period": await self._config.get_int("indicator_adx_period", DEFAULT_ADX_PERIOD),
                "boll_period": await self._config.get_int("indicator_boll_period", DEFAULT_BOLL_PERIOD),
                "boll_std": await self._config.get_float("indicator_boll_std", DEFAULT_BOLL_STD),
                "stoch_k": await self._config.get_int("indicator_stoch_k", DEFAULT_STOCH_K),
                "stoch_d": await self._config.get_int("indicator_stoch_d", DEFAULT_STOCH_D),
                "stoch_smooth": await self._config.get_int("indicator_stoch_smooth", DEFAULT_STOCH_SMOOTH),
                "ma_short": await self._config.get_int("indicator_ma_short", DEFAULT_MA_SHORT),
                "ma_long": await self._config.get_int("indicator_ma_long", DEFAULT_MA_LONG),
            }
            self._indicator_calc = IndicatorCalculator(**periods)
            logger.info(
                "IndicatorCalculator rebuilt from config_provider: rsi_period=%s adx_period=%s boll_period=%s",
                periods["rsi_period"], periods["adx_period"], periods["boll_period"],
            )
        except Exception as exc:
            logger.warning("Indicator config load failed: %s (keeping previous calculator)", exc)

    async def _load_regime_config(self) -> None:
        """Build RegimeConfig from config_provider and push into the classifier.

        Uses RegimeClassifier.update_config() which swaps self._cfg in place,
        preserving in-flight confirmation/lock state.
        """
        if self._config is None:
            return
        try:
            cfg = RegimeConfig(
                pretrend_adx_rising_bars=await self._config.get_int("pretrend_adx_rising_bars", 3),
                pretrend_breakout_factor=await self._config.get_float("pretrend_breakout_factor", 1.005),
                pretrend_breakout_lookback=await self._config.get_int("pretrend_breakout_lookback", 20),
                pretrend_threshold_floor=await self._config.get_float("pretrend_threshold_floor", 0.42),
                regime_adx_trend=await self._config.get_float("regime_adx_trend", 24.0),
                trend_strong_adx_threshold=await self._config.get_float("regime.trend_strong_adx_threshold", 28.0),
                fade_adx_falling_bars=await self._config.get_int("fade_adx_falling_bars", 3),
                fade_bbw_ratio_max=await self._config.get_float("fade_bbw_ratio_max", 1.0),
                regime_adx_range=await self._config.get_float("regime_adx_range", 22.0),
                range_bbw_max=await self._config.get_float("range_bbw_max", 1.0),
                switch_pretrend_confirm_bars=await self._config.get_int("switch_pretrend_confirm_bars", 0),
                switch_pretrend_lock_bars=await self._config.get_int("switch_pretrend_lock_bars", 0),
                switch_trend_confirm_bars=await self._config.get_int("switch_trend_confirm_bars", 2),
                switch_trend_lock_bars=await self._config.get_int("switch_trend_lock_bars", 1),
                switch_fade_confirm_bars=await self._config.get_int("switch_fade_confirm_bars", 2),
                switch_fade_lock_bars=await self._config.get_int("switch_fade_lock_bars", 0),
                switch_range_confirm_bars=await self._config.get_int("switch_range_confirm_bars", 2),
                switch_range_lock_bars=await self._config.get_int("switch_range_lock_bars", 1),
                # P1 — volatility-adaptive
                vol_adapt_enable=await self._config.get_bool("regime_vol_adapt_enable", False),
                vol_adapt_scale=await self._config.get_float("regime_vol_adapt_scale", 0.15),
                vol_adapt_band_ref=await self._config.get_float("regime_vol_adapt_band_ref", 1.0),
                # NEUTRAL 模糊区次级确认（2026-08-26）
                fuzzy_enable=await self._config.get_bool("regime_fuzzy_enable", True),
                fuzzy_hurst_trend=await self._config.get_float("regime_fuzzy_hurst_trend", 0.5),
                fuzzy_bbw_shrink_max=await self._config.get_float("regime_fuzzy_bbw_shrink_max", 1.0),
                fuzzy_use_bbw=await self._config.get_bool("regime_fuzzy_use_bbw", True),
            )
            self._regime_classifier.update_config(cfg)
            logger.info(
                "RegimeClassifier config updated from config_provider: regime_adx_trend=%.1f range_bbw_max=%.2f vol_adapt=%s",
                cfg.regime_adx_trend, cfg.range_bbw_max, cfg.vol_adapt_enable,
            )
        except Exception as exc:
            logger.warning("Regime config load failed: %s (keeping previous config)", exc)

    async def _load_symbol_thresholds(self) -> None:
        """Hot-reload per-symbol regime/scoring thresholds from config_provider.

        Called every config_reload_interval so parameter changes take effect
        without restarting the signal tower.
        """
        if self._config is None:
            return
        for sym, state in self._symbols.items():
            tf = state.timeframe
            try:
                state.trend_adx_threshold = await self._config.get_float(
                    f"scoring.{tf}.trend_adx_threshold", 22.0,
                )
                state.trend_adx_exit_threshold = await self._config.get_float(
                    f"scoring.{tf}.trend_adx_exit_threshold", 18.0,
                )
                state.dispute_diff_threshold = await self._config.get_float(
                    f"scoring.{tf}.dispute_diff_threshold", 0.05,
                )
                state.min_score_threshold = await self._config.get_float(
                    f"scoring.{tf}.min_score_threshold", 0.15,
                )
                state.min_adx_for_trade = await self._config.get_float(
                    f"scoring.{tf}.min_adx_for_trade", 22.0,
                )
            except Exception as exc:
                logger.warning("Symbol thresholds for %s/%s load failed: %s", sym, tf, exc)

    async def _load_gate_config(self) -> None:
        """Hot-reload live override + AI 门控 参数(全部 config 驱动，落库生效)。

        - scoring.live_override_enabled: 总开关(默认开)
        - scoring.live_override_sustained_sec: 实时 ADX 需持续 ≥floor 的秒数(默认30)
        - scoring.live_override_rate_limit_sec: 两次救援最小间隔秒(默认60)
        - scoring.live_override_check_interval_sec: 轮询间隔秒(默认5)
        - scoring.ai_allow_notrade_veto: AI 能否以 NO_TRADE 否决已通过信号(默认False)
        """
        if self._config is None:
            return
        try:
            self._live_override_enabled = await self._config.get_bool(
                "scoring.live_override_enabled", True,
            )
            self._live_override_sustained_sec = await self._config.get_int(
                "scoring.live_override_sustained_sec", 30,
            )
            self._live_override_rate_limit_sec = await self._config.get_int(
                "scoring.live_override_rate_limit_sec", 60,
            )
            self._live_override_check_interval_sec = await self._config.get_int(
                "scoring.live_override_check_interval_sec", 5,
            )
            self._ai_allow_notrade_veto = await self._config.get_bool(
                "scoring.ai_allow_notrade_veto", False,
            )
            logger.info(
                "Gate config loaded: live_override_enabled=%s sustained=%ds "
                "rate_limit=%ds check=%ds ai_allow_notrade_veto=%s",
                self._live_override_enabled, self._live_override_sustained_sec,
                self._live_override_rate_limit_sec, self._live_override_check_interval_sec,
                self._ai_allow_notrade_veto,
            )
        except Exception as exc:
            logger.warning("Gate config load failed: %s (keeping previous values)", exc)

    async def _load_prompt_templates(self) -> None:
        """Load per-model + global prompt templates/system prompts from config.

        Keys (Redis hcm:config:v2 / PG hcm_config.metadata):
          - signal_tower.prompt.user_prompt_template   (global legacy fallback)
          - signal_tower.prompt.system_prompt           (global legacy fallback)
          - signal_tower.prompt.<model>.user_prompt_template
          - signal_tower.prompt.<model>.system_prompt
        where <model> ∈ {co_source, manual}.

        Falls back to hardcoded defaults if missing or broken.
        """
        if self._config is None:
            self._prompt_template = None
            self._system_prompt = None
            self._prompt_templates = {}
            self._system_prompts = {}
            return
        try:
            # Global (legacy) fallbacks
            raw_user = await self._config.get("signal_tower.prompt.user_prompt_template", "")
            self._prompt_template = raw_user.strip() if raw_user and raw_user.strip() else None
            raw_sys = await self._config.get("signal_tower.prompt.system_prompt", "")
            self._system_prompt = raw_sys.strip() if raw_sys and raw_sys.strip() else None

            # Per-model overrides
            self._prompt_templates = {}
            self._system_prompts = {}
            # 【2026-08-28 co_source 清除】原 ("co_source", "manual") → 双源模式下线后
            # 只保留 hexp / manual 两套 per-model 提示词覆盖。
            for m in ("hexp", "manual"):
                u = await self._config.get(f"signal_tower.prompt.{m}.user_prompt_template", "")
                if u and u.strip():
                    self._prompt_templates[m] = u.strip()
                s = await self._config.get(f"signal_tower.prompt.{m}.system_prompt", "")
                if s and s.strip():
                    self._system_prompts[m] = s.strip()

            logger.info(
                "Prompt templates loaded: global_user=%s per_model_user=%d "
                "global_sys=%s per_model_sys=%d",
                bool(self._prompt_template), len(self._prompt_templates),
                bool(self._system_prompt), len(self._system_prompts),
            )
        except Exception as exc:
            logger.warning("Prompt template load failed: %s", exc)
            self._prompt_template = None
            self._system_prompt = None
            self._prompt_templates = {}
            self._system_prompts = {}

    async def _config_reload_loop(self) -> None:
        """Periodically re-load runtime parameters so config edits take effect
        without a service restart (P0 agility requirement).

        Safe to run alongside signal production: IndicatorCalculator rebuild
        is an atomic reference swap and RegimeClassifier.update_config preserves
        confirmation/lock state.
        """
        logger.info("Config hot-reload loop started (interval=%ds)", self._config_reload_interval)
        while self._running:
            await asyncio.sleep(self._config_reload_interval)
            try:
                await self.load_config()
            except Exception as exc:  # never let the loop die
                logger.warning("Periodic config reload failed: %s", exc)

    # ── Stats ───────────────────────────────────

    def get_stats(self) -> dict:
        """Get scheduler statistics.

        Returns:
            Dict with runtime stats.
        """
        return {
            **self._stats,
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "active_symbols": list(self._symbols.keys()),
            "running": self._running,
        }

    async def health_check(self) -> dict:
        """Check scheduler health.

        Returns:
            Dict with status and stats.
        """
        return {
            "status": "healthy" if self._running else "stopped",
            "stats": self.get_stats(),
        }
