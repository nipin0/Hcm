"""Scheduler — bar_close trigger → signal production pipeline.

The scheduler is the orchestration layer of signal-tower:
1. Monitors K-line data for bar_close events
2. Triggers the signal production pipeline per symbol
3. Coordinates: indicator calculation → regime classification →
   scoring → AI invocation → signal publication

Implements staggered start for multiple symbols to avoid
resource contention.

Design: per-symbol task with independent timing.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

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
)
from signal_tower.regime_classifier import Regime, RegimeClassifier, RegimeConfig, RegimeResult
from signal_tower.scoring_engine import ScoreResult, ScoringEngine
from signal_tower.range_bonus import RangeBonus
from signal_tower.ai_invoker import AiInvoker, AiResponse
from signal_tower.signal_publisher import SignalData, SignalPublisher
from signal_tower.watchdog import WatchdogManager

logger = logging.getLogger(__name__)

# ── Default Config ─────────────────────────────

DEFAULT_SYMBOLS = ["XAUUSD", "BTCUSD"]
DEFAULT_TIMEFRAMES = {"XAUUSD": "M5", "BTCUSD": "M15"}
DEFAULT_IDLE_SLEEP = 5.0
DEFAULT_LOOP_ERROR_SLEEP = 5.0
DEFAULT_KLINE_STALE_MAX_BARS = 3
DEFAULT_CONFIG_RELOAD_INTERVAL_SECONDS = 30  # P0: hot-reload cadence for live param tuning
TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}


@dataclass
class SymbolState:
    """Per-symbol scheduling state."""
    symbol: str
    timeframe: str = "M5"
    last_bar_open_time: Optional[datetime] = None
    last_signal_time: float = 0.0
    last_direction: str = ""
    consecutive_errors: int = 0
    kline_not_ready_count: int = 0
    enabled: bool = True


class Scheduler:
    """Orchestrates the signal production pipeline.

    On bar_close:
      1. Fetch K-line data from PostgreSQL
      2. Compute technical indicators
      3. Classify market regime (five-level)
      4. Compute pre_score with regime-aware weights
      5. Optionally invoke DeepSeek for AI confirmation
      6. Apply range bonus (RANGE regime)
      7. Publish signal via dual-write

    Example:
        scheduler = Scheduler(db_pool, redis_client, config_provider,
                             ai_invoker, signal_publisher, watchdog)
        await scheduler.start()
    """

    def __init__(
        self,
        db_pool: Any = None,
        redis_client: Any = None,
        config_provider: Any = None,
        ai_invoker: Optional[AiInvoker] = None,
        signal_publisher: Optional[SignalPublisher] = None,
        watchdog: Optional[WatchdogManager] = None,
    ):
        """Initialize Scheduler.

        Args:
            db_pool: DatabasePool instance for K-line queries.
            redis_client: RedisClient instance for cache reads.
            config_provider: ConfigProviderV3 for runtime parameters.
            ai_invoker: AiInvoker for DeepSeek calls.
            signal_publisher: SignalPublisher for dual-write.
            watchdog: WatchdogManager for health monitoring.
        """
        self._db = db_pool
        self._redis = redis_client
        self._config = config_provider
        self._ai_invoker = ai_invoker
        self._signal_publisher = signal_publisher
        self._watchdog = watchdog

        # Core engines
        self._indicator_calc = IndicatorCalculator()
        self._regime_classifier = RegimeClassifier()
        self._scoring_engine = ScoringEngine(config_provider=config_provider)
        self._range_bonus = RangeBonus()

        # Per-symbol state
        self._symbols: dict[str, SymbolState] = {}
        self._tasks: dict[str, asyncio.Task] = {}

        # Global state
        self._running = False
        self._start_time: float = 0.0
        self._account_id: Optional[int] = None  # cached from DB

        # Config hot-reload (P0: live parameter tuning)
        self._config_reload_task: Optional[asyncio.Task] = None
        self._config_reload_interval: int = DEFAULT_CONFIG_RELOAD_INTERVAL_SECONDS
        self._stats: dict[str, int] = {
            "loops": 0,
            "signals_produced": 0,
            "signals_bypassed": 0,
            "errors": 0,
        }

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
            state = SymbolState(symbol=sym, timeframe=tf)
            self._symbols[sym] = state

        self._running = True
        self._start_time = time.time()

        # Start per-symbol tasks with staggered delay
        for i, sym in enumerate(symbols):
            task = asyncio.create_task(self._symbol_loop(self._symbols[sym]))
            self._tasks[sym] = task
            # Stagger to avoid all symbols triggering simultaneously
            if i > 0:
                await asyncio.sleep(1.0)

        # P0: periodic hot-reload of runtime parameters (no restart needed)
        if self._config is not None:
            self._config_reload_task = asyncio.create_task(self._config_reload_loop())

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
        logger.info("Scheduler stopped (stats=%s)", self._stats)

    # ── Symbol Loop ─────────────────────────────

    async def _symbol_loop(self, state: SymbolState) -> None:
        """Per-symbol main loop: detect bar_close → produce signal.

        Args:
            state: SymbolState for this symbol.
        """
        logger.info("Symbol loop started: %s (tf=%s)", state.symbol, state.timeframe)
        tf_seconds = TIMEFRAME_SECONDS.get(state.timeframe, 300)

        while self._running:
            if self._watchdog:
                self._watchdog.mark_loop_start()

            try:
                # Wait for bar_close
                bar_closed = await self._wait_bar_close(state, tf_seconds)
                if not bar_closed:
                    continue

                # Produce signal
                await self._produce_signal(state)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Symbol loop error (%s): %s", state.symbol, exc)
                state.consecutive_errors += 1
                self._stats["errors"] += 1

                if self._watchdog:
                    await self._watchdog.report_step(
                        "loop_error", 0.0,
                    )

                await asyncio.sleep(DEFAULT_LOOP_ERROR_SLEEP)

            if self._watchdog:
                self._watchdog.mark_loop_end()
                await self._watchdog.beat()

            self._stats["loops"] += 1

    # ── Bar Close Detection ─────────────────────

    async def _wait_bar_close(
        self, state: SymbolState, tf_seconds: int
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
            rows = await self._db.fetch(
                """SELECT open_time, close, high, low
                   FROM hcm_market.klines
                   WHERE symbol=$1 AND time_frame=$2
                   ORDER BY open_time DESC LIMIT 1""",
                state.symbol, state.timeframe,
            )

            if not rows:
                state.kline_not_ready_count += 1
                await asyncio.sleep(DEFAULT_IDLE_SLEEP)
                return False

            latest_open = rows[0]["open_time"]

            # Check if this is a new bar
            if state.last_bar_open_time is None:
                state.last_bar_open_time = latest_open
                state.kline_not_ready_count = 0
                await asyncio.sleep(1.0)
                return False

            if latest_open == state.last_bar_open_time:
                # Same bar — wait
                now = datetime.now(timezone.utc)
                seconds_in_bar = (now - latest_open.replace(tzinfo=timezone.utc)).total_seconds()
                if seconds_in_bar < tf_seconds:
                    # Bar still forming — wait until close
                    wait_time = tf_seconds - seconds_in_bar + 0.5
                    if wait_time > 0.5:
                        await asyncio.sleep(min(wait_time, 5.0))
                else:
                    # Bar should have closed — poll faster
                    await asyncio.sleep(1.0)
                return False

            # New bar detected!
            state.last_bar_open_time = latest_open
            state.kline_not_ready_count = 0
            logger.debug("Bar closed: %s %s → %s", state.symbol, state.timeframe, latest_open)
            return True

        except Exception as exc:
            logger.warning("Kline query failed for %s: %s", state.symbol, exc)
            state.kline_not_ready_count += 1
            await asyncio.sleep(DEFAULT_LOOP_ERROR_SLEEP)
            return False

    # ── Signal Production Pipeline ──────────────

    async def _produce_signal(self, state: SymbolState) -> None:
        """Execute the full signal production pipeline.

        Steps: indicators → regime → scoring → AI → publish.

        Args:
            state: SymbolState for current symbol.
        """
        trace_id = uuid.uuid4().hex[:12]
        logger.info("Signal production: symbol=%s, trace_id=%s", state.symbol, trace_id)

        # ── Step 1: Fetch K-line data ───────────
        t0 = time.time()
        klines = await self._fetch_klines(state.symbol, state.timeframe)
        if len(klines) < 30:
            logger.warning("Insufficient kline data: %s (%d bars)", state.symbol, len(klines))
            return

        closes = np.array([k["close"] for k in klines], dtype=np.float64)
        highs = np.array([k["high"] for k in klines], dtype=np.float64)
        lows = np.array([k["low"] for k in klines], dtype=np.float64)

        if self._watchdog:
            await self._watchdog.report_step("kline", time.time() - t0)

        # ── Step 2: Compute Indicators ──────────
        t0 = time.time()
        indicators = self._indicator_calc.compute_all(closes, highs, lows)

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
        )

        # ── Step 4: Scoring ─────────────────────
        score_result = self._scoring_engine.compute_pre_score(indicators, regime_result)

        if not score_result.threshold_passed:
            logger.info(
                "Signal skipped: score=%.3f < threshold=%.3f (%s, %s)",
                score_result.pre_score, score_result.threshold,
                state.symbol, regime_result.regime.value,
            )
            return

        # ── Step 5: Cooldown Check ──────────────
        cooldown = self._scoring_engine.compute_cooldown(
            regime_result.regime,
            score_result.pre_score,
            score_result.direction,
            state.last_direction,
        )
        elapsed_since_last = time.time() - state.last_signal_time
        if elapsed_since_last < cooldown:
            logger.debug(
                "Signal suppressed: cooldown (%ds remaining)",
                cooldown - int(elapsed_since_last),
            )
            return

        # ── Step 6: AI Invocation ───────────────
        t0 = time.time()
        ai_response: Optional[AiResponse] = None
        bypass_reason: Optional[str] = None

        # Check if we should bypass AI
        bypass_ai = (
            self._ai_invoker is None
            or self._ai_invoker.is_bypass
            or score_result.pre_score >= 0.80  # High confidence bypass
        )

        if self._ai_invoker is not None and self._ai_invoker.is_bypass:
            bypass_reason = "circuit_breaker_open"
        elif score_result.pre_score >= 0.80:
            bypass_reason = "pre_score_bypass_floor"

        if not bypass_ai:
            try:
                prompt = self._build_prompt(
                    state.symbol, state.timeframe, indicators,
                    regime_result, score_result,
                )
                ai_response = await self._ai_invoker.invoke(
                    prompt,
                    pre_score_direction=score_result.direction,
                    trace_id=trace_id,
                )
                if not ai_response.success:
                    bypass_reason = "ai_fallback"
                    ai_response = None
            except Exception as exc:
                logger.warning("AI invocation failed: %s → using pre_score direction", exc)
                bypass_reason = "ai_error"

            if self._watchdog:
                await self._watchdog.report_step(
                    "deepseek", time.time() - t0, timeout_seconds=15.0
                )

        # ── Step 7: Final Direction ─────────────
        if ai_response is not None and ai_response.success:
            final_direction = ai_response.direction
            final_confidence = ai_response.confidence
        else:
            final_direction = score_result.direction
            final_confidence = score_result.pre_score
            self._stats["signals_bypassed"] += 1

        if final_direction == "NO_TRADE":
            logger.info("Signal NO_TRADE: %s", state.symbol)
            return

        # ── Step 8: Publish Signal ──────────────
        t0 = time.time()
        signal_id = await self._signal_publisher.generate_signal_id() if self._signal_publisher else int(time.time() * 1000000) % 1000000000

        signal_data = SignalData(
            signal_id=signal_id,
            task_id=0,
            account_id=await self._resolve_account_id(),
            symbol=state.symbol,
            time_frame=state.timeframe,
            direction=final_direction,
            entry_price=indicators.close,
            sl_price=0.0,
            tp1=0.0,
            tp2=0.0,
            lot=0.0,
            confidence=final_confidence,
            signal_mode="ai_dynamic" if ai_response is not None else "indicator_scoring",
            indicator_values={
                "adx_14": round(indicators.adx_14, 2),
                "rsi_14": round(indicators.rsi_14, 2),
                "macd": round(indicators.macd, 4),
            },
            fallback_reason=bypass_reason,
            regime=regime_result.regime.value,
            pre_score=round(score_result.pre_score, 4),
            weight_scheme=score_result.weight_scheme,
            position_in_range=round(score_result.range_position.pct_b_range, 4) if score_result.range_position else None,
            trace_id=trace_id,
        )

        if self._signal_publisher:
            await self._signal_publisher.publish(signal_data)

        if self._watchdog:
            await self._watchdog.report_step("publish", time.time() - t0)

        # Update state
        state.last_signal_time = time.time()
        state.last_direction = final_direction
        self._stats["signals_produced"] += 1

        logger.info(
            "Signal produced: id=%d, symbol=%s, direction=%s, "
            "pre_score=%.3f, confidence=%.2f, regime=%s, "
            "bypass=%s, trace_id=%s",
            signal_id, state.symbol, final_direction,
            score_result.pre_score, final_confidence,
            regime_result.regime.value, bypass_reason or "none", trace_id,
        )

    # ── K-line Fetching ─────────────────────────

    async def _fetch_klines(
        self, symbol: str, timeframe: str, limit: int = 100
    ) -> list[dict]:
        """Fetch recent klines from PostgreSQL.

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
                """SELECT open_time, open, high, low, close, tick_volume
                   FROM hcm_market.klines
                   WHERE symbol=$1 AND time_frame=$2
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
                }
                for r in reversed(rows)
            ]
            return klines
        except Exception as exc:
            logger.warning("Kline fetch failed for %s/%s: %s", symbol, timeframe, exc)
            return []

    # ── Prompt Builder ──────────────────────────

    def _build_prompt(
        self,
        symbol: str,
        timeframe: str,
        indicators: IndicatorResults,
        regime: RegimeResult,
        score: ScoreResult,
    ) -> str:
        """Build DeepSeek prompt from indicators and regime.

        Args:
            symbol: Trading symbol.
            timeframe: Timeframe.
            indicators: Computed indicators.
            regime: Regime result.
            score: Scoring result.

        Returns:
            Prompt string.
        """
        category = "metals" if symbol.startswith("XAU") else "crypto"
        category_label = "贵金属" if category == "metals" else "加密货币"
        category_traits = (
            "黄金为传统避险资产，与美元/美债收益率通常负相关。关注地缘风险、央行购金、实际利率。"
            if category == "metals"
            else "BTC 为高风险数字资产，受 ETF 资金流、监管政策、比特币主导率影响显著。波动率远高于传统资产。"
        )

        return f"""你是{category_label}（{symbol}）量化交易专家。{category_traits}

【任务】基于技术指标方向判定 + 预计算的外部环境赋分，输出最终交易信号。
规则：外部因子赋分已由采集服务预计算，你只需参考，不需要重新评估原始数据。

【品种上下文】
交易品种={symbol} | 品种类别={category} | 推理周期={timeframe}
品种特征={category_traits}

【技术面 — 方向来源】
技术指标: RSI={indicators.rsi_14:.1f} | MACD={indicators.macd:.4f} | ADX={indicators.adx_14:.1f} | %b={indicators.pct_b:.3f} | StochK={indicators.stoch_k:.1f}
MA排列: {indicators.ma_alignment} | DI差值: {indicators.di_diff:.1f}
预评分方向={score.direction} 评分={score.pre_score:.3f} | 推理周期={timeframe}

【AI市况】
市况={regime.regime.value}({regime.strength:.2f}) | ADX趋势: {'上升' if regime.adx_rising_bars > 0 else '下降' if regime.adx_falling_bars > 0 else '平稳'}

【手动覆盖】（仅输入时）
无

【外部环境 — 预计算赋分（不需重新评估原始数据）】
宏观({category}): 读取缓存 | 情绪({category}): 读取缓存
事件: 读取缓存 | 流动性: 读取缓存

【请输出JSON】
{{
  "direction": "BUY或SELL或NO_TRADE",
  "confidence": 0.0-1.0,
  "environment_note": "基于预计算赋分的一句话说明环境判断",
  "risk_note": "具体风险",
  "suggested_lot_ratio": 0.0-1.0,
  "sl_atr_multiplier": 1.5,
  "tp_atr_multiplier": 4.0
}}"""

    # ── Config ──────────────────────────────────

    async def _resolve_account_id(self) -> Optional[int]:
        """Resolve the active master account_id from broker accounts.

        Queries hcm_broker.accounts for the first active master account.
        Result is cached after the first successful query.

        Returns:
            account_id (int) on success, None if no active master found
            and DB is unavailable.
        """
        if self._account_id is not None:
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
                    logger.info("Resolved account_id=%d from broker accounts", self._account_id)
                    return self._account_id
            except Exception as exc:
                logger.warning("Failed to query broker accounts: %s", exc)

        # Fallback: use seed default account_id=1
        self._account_id = 1
        logger.warning("No active master account found — falling back to account_id=1")
        return self._account_id

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

        P0: indicator periods + regime detection thresholds are now wired
        through config_provider so they can be tuned live (no restart). All
        runtime parameters follow the same config-center governance as O2.
        """
        if self._config is None:
            return

        try:
            # Hot-reload cadence (self-referential, tunable)
            self._config_reload_interval = max(
                5,
                await self._config.get_int(
                    "signal_tower.config_reload_interval",
                    DEFAULT_CONFIG_RELOAD_INTERVAL_SECONDS,
                ),
            )
            # P0-critical loads first — independent of other loaders
            await self._load_indicator_config()
            await self._load_regime_config()
            # Existing loaders (each has internal error handling)
            await self._scoring_engine.load_config()
            if self._watchdog:
                await self._watchdog.load_config()
            logger.info(
                "Scheduler config loaded (reload interval=%ds)",
                self._config_reload_interval,
            )
        except Exception as exc:
            logger.warning("Scheduler config load failed: %s", exc)

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
                trend_strong_adx_threshold=await self._config.get_float("trend_strong_adx_threshold", 28.0),
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
            )
            self._regime_classifier.update_config(cfg)
            logger.info(
                "RegimeClassifier config updated from config_provider: regime_adx_trend=%.1f range_bbw_max=%.2f",
                cfg.regime_adx_trend, cfg.range_bbw_max,
            )
        except Exception as exc:
            logger.warning("Regime config load failed: %s (keeping previous config)", exc)

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
