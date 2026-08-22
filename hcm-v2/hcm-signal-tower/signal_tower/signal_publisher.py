"""Signal Publisher — Redis Stream XADD + PostgreSQL INSERT dual-write.

Publishes trading signals to:
1. Redis Stream: XADD signal:stream (primary, event-driven)
2. PostgreSQL: INSERT hcm_signal.signals (authoritative, queryable)

Features:
- Dual-write with retry (3 attempts)
- Dead letter queue on persistent failure
- Prometheus metrics
- Signal ID generation
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared.llm_client import DeepSeekClient, parse_json_block

from signal_tower.co_source import _CALIB_KEYS
from signal_tower.regime_classifier import Regime

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

SIGNAL_STREAM = "signal:stream"
DEAD_LETTER_STREAM = "signal:dead"
DEFAULT_RETRY_MAX = 3
DEFAULT_RETRY_DELAY = 0.5
STREAM_MAXLEN = 10000

# 自动写回校准因子的总开关（经 ConfigProvider.set 可热关，调试/人工接管时用）
CALIB_AUTO_APPLY_KEY = "co.calib.auto_apply"


def _regime_to_calib_key(regime_str: str) -> Optional[str]:
    """把 DB 的 m5_regime 字符串映射到 co.calib.* 配置键（复用 co_source._CALIB_KEYS）。

    DB 中 m5_regime 可能以 Regime 的 value 或 name 形式存储，这里两者都匹配，
    与 local_calibrator._regime_key 口径一致。
    """
    s = (regime_str or "").upper()
    for reg, key in _CALIB_KEYS.items():
        if reg.value.upper() == s or reg.name.upper() == s:
            return key
    return None


@dataclass
class SignalData:
    """Complete signal data for publishing."""
    signal_id: int = 0
    task_id: int = 0
    account_id: int = 0
    symbol: str = ""
    time_frame: str = "M5"
    direction: str = "NO_TRADE"
    entry_price: float = 0.0
    sl_price: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    lot: float = 0.0
    confidence: float = 0.0
    signal_mode: str = "indicator_scoring"
    magic: int = 0  # MT5 magic 号（手动跟单时透传主号原 magic，桥侧下单时用此值覆盖默认 123456）
    indicator_values: dict = field(default_factory=dict)
    macro_snapshot_id: Optional[int] = None
    sentiment_snapshot_id: Optional[int] = None
    fallback_reason: Optional[str] = None
    regime: Optional[str] = None
    pre_score: Optional[float] = None
    weight_scheme: Optional[str] = None
    position_in_range: Optional[float] = None
    trace_id: str = ""
    # ── Manual mirror (2026-07-20): 区分 open/close/modify/partial_close/add 的复合去重键 ──
    # 与 signal_id(ticket) 配合：同 ticket 的五类事件各自独立放行，不再因共用 ticket 被塌缩吞掉。
    action: str = ""
    # ── Manual mirror precise lifecycle (2026-07-23): 按票精确复刻主号动作 ──
    # close_mode="all"    → 平整该品种全部跟单号（兜底）
    # close_mode="ticket" → 仅平 close_ticket 对应的跟单号（低延时精准平仓）
    # close_ticket：主号被平/改/减仓的 ticket（manual_mirror 中 signal_id == 主号 ticket）
    close_mode: str = "all"
    close_ticket: int = 0
    # ── P0 (2026-07-15): precise-entry zone info (informational, no execution impact) ──
    zone_level: float = 0.0
    zone_type: str = ""
    zone_strength: int = 0
    zone_tp_level: float = 0.0  # 对向 zone（TP 锚点）；0.0 = 未提供 → 桥侧回退 ATR TP
    # ── P1a/P1c collaboration (consumed by mt5_bridge for precise execution) ──
    # AI-provided risk multipliers (0.0 = not provided by AI this cycle).
    ai_sl_mult: float = 0.0
    ai_tp_mult: float = 0.0
    suggested_lot_ratio: float = 1.0
    # AI 手数分档（"none"/"low"/"mid"/"high"）→ 风控引擎选档用，链动动态手数
    # （2026-08-14 需求）：AI 只决定进哪一档，实际倍率由风控面板
    # risk.lot_multiplier_{low|mid|high} + risk.lot_base 决定。
    ai_lot_tier: str = "none"
    # Seconds the bridge may wait for price to touch zone_level before filling
    # (0 = fill immediately at market). P1a zone-trigger hint.
    entry_trigger_wait: int = 0
    co_exec_fb: int = 0  # 盲点兜底单：方向 H1 兜底、进场点位交 M5(zone)
    produced_at: str = ""  # T0: 信号生产决策时刻 (UTC ISO)，供桥侧计算端到端延迟
    # 2026-08-10 修复：scheduler._run_shadow_hexp 构造 SignalData 时传入 created_at
    # （datetime），此前 SignalData 无此字段 → TypeError: __init__() got an unexpected
    # keyword argument 'created_at'，影子评估每 5 分钟抛一次 MODULE-LEVEL BUG。
    # 用字符串注解避免运行时对 datetime 名的依赖（本模块未直接 import datetime）。
    created_at: "Optional[datetime]" = None

    # ── P2 (2026-07-16): per-component buy/sell scores from scoring engine ──
    # Used by dashboard signal gauges to display the real indicators that
    # produced the signal score, not a parallel recomputation.
    component_scores: dict = field(default_factory=dict)


class SignalPublisher:
    """Publishes trading signals to Redis Stream and PostgreSQL.

    Dual-write ensures both real-time event delivery (Redis Stream
    for consumer groups) and durable storage (PostgreSQL for querying).

    Example:
        publisher = SignalPublisher(redis_client, db_pool)
        success = await publisher.publish(signal_data)
    """

    def __init__(
        self,
        redis_client: Any = None,
        db_pool: Any = None,
        retry_max: int = DEFAULT_RETRY_MAX,
        retry_delay: float = DEFAULT_RETRY_DELAY,
    ):
        """Initialize SignalPublisher.

        Args:
            redis_client: RedisClient instance for Stream operations.
            db_pool: DatabasePool instance for PostgreSQL writes.
            retry_max: Max retries per operation.
            retry_delay: Delay between retries in seconds.
        """
        self._redis = redis_client
        self._db = db_pool
        self._retry_max = retry_max
        self._retry_delay = retry_delay

        self._stats: dict[str, int] = {
            "signals_published_redis": 0,
            "signals_published_pg": 0,
            "signals_failed_redis": 0,
            "signals_failed_pg": 0,
            "signals_dead_letter": 0,
        }

    # ── Main API ────────────────────────────────

    async def publish(self, signal: SignalData, to_stream: bool = True) -> bool:
        """Publish a signal via dual-write.

        Order: Redis Stream first (real-time), then PostgreSQL (durable).

        【P0-3/D 组 2026-08-03】signal_status 生命周期统一为：
          0 = published 在途（待风控审理）
          1 = 风控 PASS/DEGRADE（过风控，待成交）
          2 = 风控 REJECT（真丢弃）
          3 = 桥成交（filled）
          4 = publish_failed（从未进流）
        to_stream=False 时仅落 PG 不进流（filtered/NO_TRADE 诊断信号），
        状态置 0；此类信号 signal_dir 恒为 NO_TRADE，不进风控/桥。

        Robustness contract (2026-07-31 root-cause fix):

          Redis Stream is the ONLY real-time path that drives
          risk-engine → bridge execution. If ``XADD`` fails, the signal can
          NEVER be filled, so we must NOT leave a ``status=2`` row
          ("passed gate, pending fill") in PostgreSQL — that would pollute
          the funnel's "过闸门未成交" count with phantom orphans that were
          never actually published to the stream.

          * Redis success → PG ``signal_status = 2`` (correct: passed, in-flight).
          * Redis failure → PG ``signal_status = 4`` (publish_failed / orphan)
            + a dead-letter entry. The signal is recorded as FAILED, never as a
            pending fill, so it can never masquerade as "passed gate not filled".

        Args:
            signal: SignalData to publish.

        Returns:
            True if Redis Stream publish succeeded.
        """
        t0 = time.time()

        # 1. Redis Stream XADD (primary real-time path)
        #    to_stream=False（filtered/NO_TRADE 诊断信号）跳过进流，仅落 PG。
        redis_ok = await self._publish_to_redis(signal) if to_stream else False

        # 2. PostgreSQL INSERT (durable). Status tracks whether the signal
        #    actually reached the real-time stream (see docstring above).
        #    【P0-3】在途=0（原 2 与风控 REJECT=2 撞语义）；发布失败=4。
        if to_stream:
            pg_status = 0 if redis_ok else 4
        else:
            pg_status = 0
        pg_ok = await self._publish_to_pg(signal, status=pg_status)

        latency_ms = int((time.time() - t0) * 1000)

        if not to_stream:
            # filtered/NO_TRADE 诊断信号：仅 PG 落库，不进流非失败、不进死信
            return pg_ok

        if redis_ok:
            if pg_ok:
                logger.info(
                    "Signal published: id=%d, symbol=%s, direction=%s, latency=%dms",
                    signal.signal_id, signal.symbol, signal.direction, latency_ms,
                )
            else:
                logger.warning(
                    "Signal partial publish: Redis OK, PG failed (id=%d). "
                    "Signal available via Stream but not durable.",
                    signal.signal_id,
                )
            return True

        # Redis failed → signal never reached downstream. Record as failed,
        # never as a pending fill.
        await self._publish_to_dead_letter(signal, "redis_stream_failed")
        if pg_ok:
            logger.error(
                "Signal publish FAILED but persisted (id=%d): Redis down, "
                "PG status=4 (publish_failed). Orphan recorded, will not fill.",
                signal.signal_id,
            )
        else:
            logger.critical(
                "CRITICAL: Signal LOST (id=%d): Redis down AND PG failed — "
                "no record of this signal anywhere.",
                signal.signal_id,
            )
        return False

    # ── Redis Stream ────────────────────────────

    async def _publish_to_redis(self, signal: SignalData) -> bool:
        """Publish signal to Redis Stream with retry.

        Args:
            signal: SignalData.

        Returns:
            True on success.
        """
        if self._redis is None or not self._redis.is_initialized:
            logger.warning("Redis not available — skipping Stream publish")
            self._stats["signals_failed_redis"] += 1
            return False

        stream_data = {
            "event": "signal_created",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal_generated_at": signal.produced_at,
            "signal_id": signal.signal_id,
            "task_id": signal.task_id,
            "account_id": signal.account_id,
            "symbol": signal.symbol,
            "time_frame": signal.time_frame,
            "direction": signal.direction,
            "entry_price": signal.entry_price,
            "sl_price": signal.sl_price,
            "tp1": signal.tp1,
            "tp2": signal.tp2,
            "lot": signal.lot,
            "confidence": signal.confidence,
            "signal_mode": signal.signal_mode,
            "magic": signal.magic,
            "indicator_values": signal.indicator_values,
            "macro_snapshot_id": signal.macro_snapshot_id or 0,
            "sentiment_snapshot_id": signal.sentiment_snapshot_id or 0,
            "fallback_reason": signal.fallback_reason or "",
            "regime": signal.regime or "",
            "pre_score": signal.pre_score or 0.0,
            "weight_scheme": signal.weight_scheme or "",
            "position_in_range": signal.position_in_range or 0.0,
            "trace_id": signal.trace_id,
            "action": signal.action,
            "close_mode": signal.close_mode,
            "close_ticket": signal.close_ticket,
            "zone_level": signal.zone_level,
            "zone_type": signal.zone_type,
            "zone_strength": signal.zone_strength,
            "zone_tp_level": signal.zone_tp_level,
            # ── P1a/P1c collaboration fields (consumed by mt5_bridge) ──
            "ai_sl_mult": signal.ai_sl_mult,
            "ai_tp_mult": signal.ai_tp_mult,
            "suggested_lot_ratio": signal.suggested_lot_ratio,
            # 【B2 修复 2026-08-14】AI 手数分档此前未透传 stream → 风控
            # _apply_dynamic_lot 的 ai_lot_tier 分支恒 "none" 死代码。
            "ai_lot_tier": getattr(signal, "ai_lot_tier", "none") or "none",
            "entry_trigger_wait": signal.entry_trigger_wait,
            "co_exec_fb": int(getattr(signal, "co_exec_fb", 0) or 0),
            # ── P2: scoring-engine component breakdown for dashboard gauges ──
            "component_scores": json.dumps(signal.component_scores),
        }

        for attempt in range(1, self._retry_max + 1):
            try:
                msg_id = await self._redis.xadd(
                    SIGNAL_STREAM,
                    stream_data,
                    maxlen=STREAM_MAXLEN,
                )
                if msg_id:
                    self._stats["signals_published_redis"] += 1
                    logger.debug("Signal XADD: id=%d → %s (msg_id=%s)",
                               signal.signal_id, SIGNAL_STREAM, msg_id)
                    return True

            except Exception as exc:
                logger.warning(
                    "Redis XADD attempt %d/%d failed (signal_id=%d): %s",
                    attempt, self._retry_max, signal.signal_id, exc,
                )

            if attempt < self._retry_max:
                await asyncio.sleep(self._retry_delay * attempt)

        self._stats["signals_failed_redis"] += 1
        logger.error("Redis XADD all retries exhausted (signal_id=%d)", signal.signal_id)
        return False

    # ── PostgreSQL ──────────────────────────────

    async def _publish_to_pg(self, signal: SignalData, status: int = 2) -> bool:
        """Insert signal into PostgreSQL with retry.

        Args:
            signal: SignalData.
            status: Explicit ``signal_status`` to persist. Defaults to 2
                (passed gate, in-flight). ``publish()`` passes 4 when the
                Redis Stream XADD failed, so a failed publish is recorded as
                ``publish_failed`` rather than a phantom pending fill.

        Returns:
            True on success.
        """
        if self._db is None or not self._db.is_initialized:
            logger.warning("PostgreSQL not available — skipping PG insert")
            self._stats["signals_failed_pg"] += 1
            return False

        for attempt in range(1, self._retry_max + 1):
            try:
                _tag = await self._db.execute(
                    """INSERT INTO hcm_signal.signals
                       (signal_id, task_id, account_id, symbol, time_frame,
                        signal_dir, entry_price, sl_price, tp1, tp2, lot,
                        confidence, signal_mode, indicator_values,
                        macro_snapshot_id, sentiment_snapshot_id,
                        fallback_reason, pre_score, weight_scheme,
                        position_in_range, regime,
                        zone_level, zone_type, zone_strength,
                        created_at, signal_status)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                               $12, $13, $14, $15, $16, $17, $18,
                               $19, $20, $21, $22, $23, $24, $25, $26)
                       ON CONFLICT (signal_id) DO NOTHING""",
                    signal.signal_id,
                    signal.task_id if signal.task_id > 0 else None,
                    signal.account_id,
                    signal.symbol,
                    signal.time_frame,
                    signal.direction,
                    signal.entry_price,
                    signal.sl_price,
                    signal.tp1,
                    signal.tp2,
                    signal.lot,
                    signal.confidence,
                    signal.signal_mode,
                        # P1a/P1c: persist collaboration fields inside the existing
                        # indicator_values JSONB (no PG schema migration required).
                        json.dumps({
                            **signal.indicator_values,
                            "_collab": {
                                "ai_sl_mult": signal.ai_sl_mult,
                                "ai_tp_mult": signal.ai_tp_mult,
                                "suggested_lot_ratio": signal.suggested_lot_ratio,
                                "entry_trigger_wait": signal.entry_trigger_wait,
                                "zone_tp_level": signal.zone_tp_level,
                            },
                            "_component_scores": signal.component_scores,
                        }),
                    signal.macro_snapshot_id,
                    signal.sentiment_snapshot_id,
                    signal.fallback_reason or "",
                    signal.pre_score,
                    signal.weight_scheme or "",
                    signal.position_in_range,
                    signal.regime or "",
                    signal.zone_level if getattr(signal, "zone_level", 0) else None,
                    signal.zone_type or None,
                    signal.zone_strength if getattr(signal, "zone_strength", 0) else 0,
                    datetime.now(timezone.utc),
                    status,
                )
                if isinstance(_tag, str) and _tag.strip().endswith(" 0"):
                    # ON CONFLICT 空操作：id 撞车（历史计数器落后），旧行已在库，
                    # 不视为失败，但打 WARNING 供观测；下方 bump 自愈防再撞。
                    logger.warning(
                        "PG INSERT conflict: signal_id=%d already exists (kept existing row)",
                        signal.signal_id,
                    )
                self._stats["signals_published_pg"] += 1
                await self._bump_signal_id_counter(signal.signal_id)
                return True

            except Exception as exc:
                logger.warning(
                    "PG INSERT attempt %d/%d failed (signal_id=%d): %s",
                    attempt, self._retry_max, signal.signal_id, exc,
                )

            if attempt < self._retry_max:
                await asyncio.sleep(self._retry_delay * attempt)

        self._stats["signals_failed_pg"] += 1
        logger.error("PG INSERT all retries exhausted (signal_id=%d)", signal.signal_id)
        return False

    # ── Dead Letter Queue ───────────────────────

    async def _publish_to_dead_letter(self, signal: SignalData, reason: str) -> None:
        """Write failed signal to dead letter queue.

        Args:
            signal: Failed SignalData.
            reason: Failure reason.
        """
        self._stats["signals_dead_letter"] += 1

        dead_data = {
            "original_signal_id": signal.signal_id,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "failed_at": "redis_stream",
            "error": reason,
            "retry_count": self._retry_max,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if self._redis is not None and self._redis.is_initialized:
            try:
                await self._redis.xadd(DEAD_LETTER_STREAM, dead_data, maxlen=STREAM_MAXLEN)
                logger.error(
                    "Signal dead letter: id=%d, reason=%s → %s",
                    signal.signal_id, reason, DEAD_LETTER_STREAM,
                )
            except Exception as exc:
                logger.critical(
                    "CRITICAL: Cannot write to dead letter queue (signal_id=%d): %s",
                    signal.signal_id, exc,
                )
        else:
            logger.critical(
                "CRITICAL: No Redis available for dead letter (signal_id=%d, reason=%s)",
                signal.signal_id, reason,
            )

    # ── Signal ID Generation ────────────────────

    async def generate_signal_id(self) -> int:
        """Generate a unique signal ID.

        Uses Redis INCR for distributed ID generation, falling back
        to timestamp-based IDs if Redis is unavailable.

        Returns:
            Unique signal ID.
        """
        if self._redis is not None and self._redis.is_initialized:
            try:
                return await self._redis.raw.incr("hcm:signal_id_counter")
            except Exception:
                pass

        # Fallback: timestamp-based
        return int(time.time() * 1000000) % 1000000000

    async def _bump_signal_id_counter(self, inserted_id: int) -> None:
        """【B7 修复 2026-08-14】落库成功后把 Redis 计数器自愈校准到 ≥ 已用 id。

        根因：PG 历史存在超大 signal_id（如 3.7 亿的手工/历史行），若有流程按
        max(signal_id)+1 重置过计数器，此后 INCR 会长期撞 signals_pkey。
        此处用 Lua 原子 max-set：counter < inserted_id 时抬到 inserted_id，
        每次落库自愈，计数器永不再落后于实际已用 id。失败静默（不影响发布）。
        """
        if self._redis is None or not self._redis.is_initialized:
            return
        try:
            await self._redis.raw.eval(
                "local c=tonumber(redis.call('GET', KEYS[1]) or '0');"
                "local n=tonumber(ARGV[1]);"
                "if c < n then redis.call('SET', KEYS[1], n) end; return 1",
                1, "hcm:signal_id_counter", str(int(inserted_id)),
            )
        except Exception:
            pass

    # ── Stats & Health ──────────────────────────

    def get_stats(self) -> dict:
        """Get publisher statistics.

        Returns:
            Dict with publish counts.
        """
        return dict(self._stats)

    async def health_check(self) -> dict:
        """Check publisher health.

        Returns:
            Dict with status and stats.
        """
        redis_ok = False
        pg_ok = False

        if self._redis and self._redis.is_initialized:
            try:
                redis_ok = await self._redis.ping()
            except Exception:
                pass

        if self._db and self._db.is_initialized:
            try:
                pg_check = await self._db.health_check()
                pg_ok = pg_check.get("status") == "healthy"
            except Exception:
                pass

        return {
            "status": "healthy" if redis_ok else "degraded",
            "redis_ok": redis_ok,
            "pg_ok": pg_ok,
            "stats": self.get_stats(),
        }

    # ── Labeled Sample (P1b 标注采集) ────────────

    async def save_labeled_sample(
        self,
        signal_id: int,
        symbol: str,
        bar_time: Any,        # datetime 或 ISO str
        m5_regime: str,
        direction: str,
        raw_score: Optional[float] = None,
        calibrated: Optional[float] = None,
        label: Optional[str] = None,
    ) -> bool:
        """写入一条标注样本到 ``hcm_ai.labeled_samples``（P1b 校准层数据源）。

        每笔信号产生时调用一次，初始 ``label=NULL``。
        P1b.1 平仓回报流程回填 ``label='win'/'loss'``。

        重复调用（同 symbol+timeframe+bar_time）由 UNIQUE 约束静默忽略。
        """
        if self._db is None or not self._db.is_initialized:
            return False
        try:
            # 归一化为带时区的 datetime。state.last_bar_open_time 本就是 datetime，
            # 但 P1b 回填/测试路径可能传 ISO 字符串。注意：不能把 datetime 转成
            # isoformat 字符串再以 ``$N::timestamptz`` 传入——asyncpg 在显式 cast 下
            # 会把参数推断为 timestamptz 并要求收到 datetime 对象，传 str 会报
            # "expected datetime ... got str"（即偶发的 Labeled sample write FAILED）。
            if isinstance(bar_time, datetime):
                bar_dt = bar_time
            elif isinstance(bar_time, str):
                s = bar_time.strip()
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                try:
                    bar_dt = datetime.fromisoformat(s)
                except Exception:
                    logger.warning("Labeled sample skipped: unparseable bar_time=%r", bar_time)
                    return False
            else:
                logger.warning("Labeled sample skipped: unsupported bar_time type=%r", type(bar_time))
                return False
            if bar_dt.tzinfo is None:
                bar_dt = bar_dt.replace(tzinfo=timezone.utc)
            await self._db.execute(
                "INSERT INTO hcm_ai.labeled_samples "
                "(signal_id, symbol, timeframe, bar_time, m5_regime, direction, raw_score, calibrated, label) "
                "VALUES ($1, $2, 'M5', $3, $4, $5, $6, $7, $8) "
                "ON CONFLICT (symbol, timeframe, bar_time) DO NOTHING",
                signal_id, symbol, bar_dt, m5_regime, direction,
                raw_score, calibrated, label,
            )
            return True
        except Exception as exc:
            logger.error("Labeled sample write FAILED (schema/table issue): %s", exc)
            return False

    async def reconcile_labels(self) -> int:
        """P0: 把已平仓成交的盈亏回写 labeled_samples.label，给校准层/Optuna 喂真实数据。

        两步（均按 signal_id 聚合 hcm_trading.orders 净盈亏，同 signal_id 可能含
        主号+跟单号两笔复制成交，按【净盈亏符号】标定 win/loss，符号一致不影响胜率统计）：
          1) 播种：补齐「已平仓但尚无 labeled_samples 行」的历史信号（来源
             hcm_signal.signals + 聚合成交净盈亏）。
          2) 标定：将 label 仍为 NULL、且对应成交已平仓的样本标记为
             win(净盈亏>0) / loss(否则)。

        这是 AI 自我发展闭环的「眼睛」——local_calibrator / Optuna 没有真实盈亏
        样本就永远冷启动跳过。返回当前已标定(label IN win/loss)样本总数，供日志观测。
        """
        if self._db is None or not getattr(self._db, "is_initialized", False):
            return 0
        try:
            # 1) 播种缺失的历史样本（信号特征取自 signals 表，盈亏取自 orders 聚合）
            await self._db.execute(
                """
                INSERT INTO hcm_ai.labeled_samples
                    (signal_id, symbol, timeframe, bar_time, m5_regime, direction, raw_score, calibrated, label)
                SELECT s.signal_id, s.symbol, s.time_frame, s.created_at,
                       COALESCE(s.regime, ''), s.signal_dir, s.pre_score, 1.0,
                       CASE WHEN agg.net_profit > 0 THEN 'win' ELSE 'loss' END
                FROM hcm_signal.signals s
                JOIN (
                    SELECT o.signal_id AS sid, SUM(o.profit) AS net_profit
                    FROM hcm_trading.orders o
                    WHERE o.signal_id IS NOT NULL
                      AND o.close_time IS NOT NULL
                      AND o.order_status = 1
                    GROUP BY o.signal_id
                ) agg ON s.signal_id = agg.sid
                WHERE NOT EXISTS (
                    SELECT 1 FROM hcm_ai.labeled_samples ls WHERE ls.signal_id = s.signal_id
                )
                ON CONFLICT (symbol, timeframe, bar_time) DO NOTHING
                """
            )
            # 2) 标定所有已平仓且尚未标定的样本
            await self._db.execute(
                """
                UPDATE hcm_ai.labeled_samples ls
                SET label = CASE WHEN agg.net_profit > 0 THEN 'win' ELSE 'loss' END
                FROM (
                    SELECT o.signal_id AS sid, SUM(o.profit) AS net_profit
                    FROM hcm_trading.orders o
                    WHERE o.signal_id IS NOT NULL
                      AND o.close_time IS NOT NULL
                      AND o.order_status = 1
                    GROUP BY o.signal_id
                ) agg
                WHERE ls.signal_id = agg.sid
                  AND ls.label IS NULL
                  AND agg.net_profit IS NOT NULL
                """
            )
            total = await self._db.fetchval(
                "SELECT COUNT(*) FROM hcm_ai.labeled_samples WHERE label IN ('win','loss')"
            )
            return int(total) if total is not None else 0
        except Exception as exc:  # noqa: BLE001
            logger.error("reconcile_labels failed: %s", exc)
            return 0

    # ── 每日校准快照（P1：AI 自我发展的「日历」）────────
    # 与 local_calibrator 完全一致的校准数学：
    #   calib = clamp(1.0 + (win_rate - 0.5), CALIB_CLAMP_MIN, CALIB_CLAMP_MAX)
    #   win_rate 偏离基准 0.5 越多，因子越偏离 1.0（>1 放大可信信号，<1 压制弱信号）。
    CALIB_ALPHA = 1.0
    CALIB_CLAMP_MIN = 0.6
    CALIB_CLAMP_MAX = 1.4
    CALIB_MIN_SAMPLES = 20   # 单态最小累计样本（不足→冷启动 1.0）
    CALIB_MIN_DAYS = 7       # 最小样本日（不足→冷启动 1.0）

    CALIB_DAILY_UPSERT_SQL = """
        INSERT INTO hcm_ai.calibration_daily
            (report_date, m5_regime, trades, wins, losses, win_rate,
             calib_factor, sample_days, cold_start, computed_at)
        SELECT
            rd,
            m5_regime,
            trades,
            wins,
            trades - wins AS losses,
            CASE WHEN trades > 0 THEN wins::numeric / trades ELSE 0 END,
            CASE
                WHEN cum_trades >= $1 AND sample_days >= $2 AND cum_trades > 0
                    THEN LEAST($3, GREATEST($4,
                              1.0 + (cum_wins::numeric / cum_trades - 0.5)))
                ELSE 1.0
            END,
            sample_days,
            (cum_trades < $5 OR sample_days < $6),
            now()
        FROM (
            SELECT
                rd, m5_regime, trades, wins,
                SUM(trades) OVER w AS cum_trades,
                SUM(wins)  OVER w AS cum_wins,
                COUNT(*)   OVER (PARTITION BY m5_regime) AS sample_days
            FROM (
                SELECT
                    date(bar_time) AS rd,
                    m5_regime,
                    COUNT(*) AS trades,
                    SUM((label = 'win')::int) AS wins
                FROM hcm_ai.labeled_samples
                WHERE label IN ('win', 'loss')
                  AND m5_regime <> ''
                GROUP BY date(bar_time), m5_regime
            ) daily
            WINDOW w AS (
                PARTITION BY m5_regime
                ORDER BY rd
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            )
        ) agg
        ON CONFLICT (report_date, m5_regime) DO UPDATE SET
            trades      = EXCLUDED.trades,
            wins        = EXCLUDED.wins,
            losses      = EXCLUDED.losses,
            win_rate    = EXCLUDED.win_rate,
            calib_factor = EXCLUDED.calib_factor,
            sample_days = EXCLUDED.sample_days,
            cold_start  = EXCLUDED.cold_start,
            computed_at = EXCLUDED.computed_at;
        """

    async def reconcile_calibration_daily(self) -> int:
        """P1：把「截至每日的累计胜率→校准因子」算成时序快照写进 calibration_daily。

        每个标注样本归属的交易日(date(bar_time))，按 M5 体制做「累计」胜率→校准因子
        （窗口函数 cum 累计到当日），存为 (report_date, m5_regime) 一行。这样前端
        「校准时序」页可按时序回放各体制校准因子/胜率的演化——AI 自我发展的历史轨迹。

        仅在有新标注样本时才重算（computed_at 早于最新标注时间即跳过），省 PG 负载；
        首次运行（表为空）会全量回填历史。

        注意：本函数只落库，不标记 applied。applied 字段仅当校准因子经
        mark_calibration_applied（在自动写回 co.calib.* 成功后）标记，
        避免"落库即当前生效"的虚假状态。

        返回当前快照总行数，供日志观测。
        """
        if self._db is None or not getattr(self._db, "is_initialized", False):
            return 0
        try:
            last = await self._db.fetchval(
                "SELECT MAX(computed_at) FROM hcm_ai.calibration_daily"
            )
            newest = await self._db.fetchval(
                "SELECT MAX(created_at) FROM hcm_ai.labeled_samples "
                "WHERE label IN ('win', 'loss')"
            )
            if last is not None and newest is not None and last >= newest:
                return 0  # 无新标注，跳过
            await self._db.execute(
                self.CALIB_DAILY_UPSERT_SQL,
                self.CALIB_MIN_SAMPLES, self.CALIB_MIN_DAYS,
                self.CALIB_CLAMP_MAX, self.CALIB_CLAMP_MIN,
                self.CALIB_MIN_SAMPLES, self.CALIB_MIN_DAYS,
            )
            n = await self._db.fetchval("SELECT COUNT(*) FROM hcm_ai.calibration_daily")
            return int(n) if n is not None else 0
        except Exception as exc:  # noqa: BLE001
            logger.error("reconcile_calibration_daily failed: %s", exc)
            return 0

    async def compute_latest_calib_factors(self) -> Optional[dict]:
        """读最新校准快照，返回非冷启动且偏离 1.0 的体制校准因子(co.calib.* 键→值)。

        返回 None 表示所有体制仍在冷启动或因子恰为 1.0（无需/不应写回配置，
        以免把人工调优的因子覆盖成 1.0）。
        """
        if self._db is None or not getattr(self._db, "is_initialized", False):
            return None
        try:
            latest_date = await self._db.fetchval(
                "SELECT MAX(report_date) FROM hcm_ai.calibration_daily"
            )
            if latest_date is None:
                return None
            rows = await self._db.fetch(
                "SELECT m5_regime, calib_factor, cold_start "
                "FROM hcm_ai.calibration_daily WHERE report_date = $1",
                latest_date,
            )
            factors: dict = {}
            for r in rows:
                calib = float(r["calib_factor"])
                if bool(r["cold_start"]) or calib == 1.0:
                    continue  # 冷启动或未调整：不写回，避免覆盖人工配置
                key = _regime_to_calib_key(r["m5_regime"])
                if key:
                    factors[key] = round(calib, 4)
            return factors or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("compute_latest_calib_factors failed: %s", exc)
            return None

    async def mark_calibration_applied(self) -> None:
        """把最新 report_date 的快照行标记为 applied=TRUE。

        调用方必须已在之前成功把因子写回 co.calib.*，否则 applied 会产生
        "已生效"的虚假状态。本函数幂等，多次调用无害。
        """
        if self._db is None or not getattr(self._db, "is_initialized", False):
            return
        try:
            await self._db.execute(
                "UPDATE hcm_ai.calibration_daily SET applied = TRUE "
                "WHERE report_date = (SELECT MAX(report_date) "
                "FROM hcm_ai.calibration_daily)"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("mark_calibration_applied failed: %s", exc)
            return

    async def generate_calibration_diagnosis(self, trend_days: int = 14) -> Optional[dict]:
        """P1：用 DeepSeek 把最新校准快照 + 近期趋势生成「自然语言诊断」。

        前期 AiScorer 的提示词是「宏观/情绪/事件因子打分」(输出 score 0-30/bias/summary)，
        用途是给市场因子赋分，**不直接适配校准诊断**——它强制数值分、评估宏观风险，
        而非对各体制胜率/校准因子的趋势研判与调参建议。此处以其「system=分析师 + 仅回
        JSON」的结构骨架为基础，精确改写为「校准诊断分析师」角色：输入各体制 win_rate /
        calib_factor / 样本日 / 近期趋势，输出 narrative(中文自然语言诊断) +
        recommendations(具体调参建议，如 co.gate.<regime>.trend) + needs_human_review +
        confidence。

        无 DeepSeek 配置或调用失败时返回 None，由 web 端点回退到确定性诊断（离线可渲染）。
        """
        if self._db is None or not getattr(self._db, "is_initialized", False):
            return None
        try:
            # 1) 读 DeepSeek 配置（PG hcm_config.metadata 真源，缺则回退环境变量）
            async def _cfg(key: str) -> Optional[str]:
                return await self._db.fetchval(
                    "SELECT current_value FROM hcm_config.metadata WHERE config_key=$1", key
                )

            api_key = await _cfg("deepseek.api_key")
            base_url = await _cfg("deepseek.api_base")
            model = await _cfg("deepseek.model")
            client = DeepSeekClient(api_key, base_url, model)
            if not client.is_available:
                logger.info("Calibration diagnosis skipped: DeepSeek API key not configured")
                return None

            # 2) 读最新日快照 + 近期趋势
            latest_date = await self._db.fetchval(
                "SELECT MAX(report_date) FROM hcm_ai.calibration_daily"
            )
            if latest_date is None:
                return None
            rows = await self._db.fetch(
                "SELECT report_date, m5_regime, trades, wins, win_rate, "
                "calib_factor, sample_days, cold_start FROM hcm_ai.calibration_daily "
                "ORDER BY report_date, m5_regime"
            )
            per_regime: dict = {}
            total_trades = 0
            latest_rows = [r for r in rows if r["report_date"] == latest_date]
            for r in latest_rows:
                per_regime[r["m5_regime"]] = {
                    "win_rate": float(r["win_rate"]),
                    "calib_factor": float(r["calib_factor"]),
                    "trades": int(r["trades"]),
                    "cold_start": bool(r["cold_start"]),
                }
                total_trades += int(r["trades"])
            total_days = max((int(r["sample_days"]) for r in latest_rows), default=0)

            # 近期趋势（每体制 calib 序列）
            trend: dict = defaultdict(list)
            for r in rows:
                if (latest_date - r["report_date"]).days <= trend_days:
                    trend[r["m5_regime"]].append(
                        (r["report_date"].isoformat(), round(float(r["calib_factor"]), 3))
                    )

            # 3) 组装「精确适配校准诊断」的提示词（结构沿用 AiScorer 的 system=分析师+仅回JSON）
            per_lines = []
            for reg, d in sorted(per_regime.items()):
                cs = "（冷启动·样本不足）" if d["cold_start"] else ""
                per_lines.append(
                    f"- {reg}: 胜率={d['win_rate'] * 100:.1f}%，校准因子={d['calib_factor']:.2f}"
                    f"，样本={d['trades']}笔 {cs}"
                )
            trend_lines = []
            for reg, seq in sorted(trend.items()):
                s = " → ".join(f"{d}:{c}" for d, c in seq)
                trend_lines.append(f"- {reg}: {s}")
            per_block = "\n".join(per_lines) if per_lines else "（无）"
            trend_block = "\n".join(trend_lines) if trend_lines else "（无）"

            system_prompt = (
                "你是一名量化交易策略校准诊断分析师，服务于黄金(XAUUSD) M5 自动交易系统。"
                "你依据各 M5 市况体制的胜率与校准因子历史，给出中文自然语言诊断与具体调参建议。"
                "必须且只能输出一个 JSON 对象，不要包含 markdown 代码块或任何额外文字。"
            )
            user_prompt = (
                "你是黄金(XAUUSD) M5 自动交易策略的「校准诊断分析师」。\n"
                "策略按 M5 市况体制(M5_regime)分别统计胜率，并用校准因子 calib_factor 缩放各体制"
                "信号评分(raw_score × calib)：calib>1 放大可信体制、calib<1 压缩不可信体制；"
                "基准胜率 0.5 对应 calib=1.0，calib 区间固定[0.6, 1.4]。\n\n"
                f"【最新校准快照】(report_date={latest_date}，累计 {total_days} 样本日 / {total_trades} 笔)\n"
                f"{per_block}\n\n"
                f"【近 {trend_days} 日各体制校准因子趋势】(用于判断改善/恶化)\n"
                f"{trend_block}\n\n"
                "【研判口径】\n"
                "- 胜率<0.40 视为弱体制(长期亏损)，>0.55 视为强体制。\n"
                "- 冷启动(cold_start)表示样本不足，calib 锁定 1.0，不可据此调参。\n"
                "- 共源信号下单门槛的真实配置键为 co.gate.strong.trend / co.gate.weak.trend / "
                "co.gate.shock.trend（0-100，越大越难下单；体制只有 strong/weak/shock 三种，"
                "不存在 co.gate.TREND / co.gate.RANGE / co.gate.NEUTRAL 这类键）。\n"
                "- 校准因子由系统每日自动写回 co.calib.<regime>（如 co.calib.trend / "
                "co.calib.range），你无需也无法直接修改它们，仅在 narrative 中说明。\n\n"
                "【请输出JSON】\n"
                "{\n"
                '  "narrative": "<2-4段中文自然语言诊断：概括各体制当前胜率与校准因子状态、'
                '与近期趋势对比、指出最需关注的体制>",\n'
                '  "recommendations": ["<针对弱体制的具体调参建议，仅可引用真实存在的键，'
                '如 收紧 co.gate.weak.trend 至 55 抑制该体制下单；'
                '禁止虚构 co.gate.TREND/RANGE/NEUTRAL 等不存在的键>", "..."],\n'
                '  "needs_human_review": <bool：是否存在胜率持续<0.35 或样本充足但校准因子触及 '
                '边界 0.6/1.4 等需人工介入的情况>,\n'
                '  "confidence": "<high|medium|low：基于样本日数量与数据一致性>"\n'
                "}\n"
            )

            raw = await client.complete(system_prompt, user_prompt)
            parsed = parse_json_block(raw)
            if not parsed or "narrative" not in parsed:
                # 模型未返回可用 JSON → 把原始文本当作诊断兜底
                content = (raw or "").strip() or "（AI 返回内容无法解析）"
                recs: list = []
                needs = False
                conf = "low"
            else:
                content = str(parsed.get("narrative", "")).strip()
                recs = [str(x) for x in (parsed.get("recommendations") or [])]
                needs = bool(parsed.get("needs_human_review", False))
                conf = str(parsed.get("confidence", "low")).lower()
                if conf not in ("high", "medium", "low"):
                    conf = "low"

            # 4) 落库（按 report_date upsert）
            await self._db.execute(
                "INSERT INTO hcm_ai.calibration_diagnosis "
                "(report_date, content, recommendations, needs_review, confidence, model) "
                "VALUES ($1,$2,$3,$4,$5,$6) "
                "ON CONFLICT (report_date) DO UPDATE SET "
                "content=EXCLUDED.content, recommendations=EXCLUDED.recommendations, "
                "needs_review=EXCLUDED.needs_review, confidence=EXCLUDED.confidence, "
                "model=EXCLUDED.model, created_at=now()",
                latest_date, content, recs, needs, conf, client.model,
            )
            logger.info(
                "Calibration AI diagnosis generated for %s (confidence=%s, review=%s)",
                latest_date, conf, needs,
            )
            return {
                "report_date": latest_date.isoformat() if hasattr(latest_date, "isoformat") else str(latest_date),
                "content": content,
                "recommendations": recs,
                "needs_review": needs,
                "confidence": conf,
                "model": client.model,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_calibration_diagnosis failed: %s", exc)
            return None

    # ── Consumer Group Setup ────────────────────

    async def setup_consumer_groups(self) -> None:
        """Create Redis Stream consumer groups for signal consumption.
        
        Uses start_id="0" so that messages published before consumers
        come online are NOT missed (the group starts from stream beginning
        on first creation; BUSYGROUP on subsequent calls is harmless)."""
        if self._redis is None or not self._redis.is_initialized:
            return

        groups = [
            (SIGNAL_STREAM, "risk-engine-group"),
            (SIGNAL_STREAM, "web-push-group"),
        ]

        for stream, group in groups:
            try:
                await self._redis.xgroup_create(stream, group, mkstream=True, start_id="0")
                logger.info("Consumer group created: %s on %s", group, stream)
            except Exception as exc:
                logger.debug("Consumer group %s on %s: %s", group, stream, exc)
