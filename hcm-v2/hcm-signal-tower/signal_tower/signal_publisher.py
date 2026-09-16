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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

SIGNAL_STREAM = "signal:stream"
DEAD_LETTER_STREAM = "signal:dead"
DEFAULT_RETRY_MAX = 3
DEFAULT_RETRY_DELAY = 0.5
STREAM_MAXLEN = 10000

# ── 订单状态枚举（hcm_trading.orders.order_status）────────────────────────
# 铁律 4.6：禁止在 SQL/代码里裸写 1/2 等状态字面量（历史上裸写 order_status=1
# 曾导致 1015 笔订单假 open、风控冷却全面失效，见铁律 10.1）。
# 语义经 PG 实测确认（2026-08-28）：
#   OPEN(1)  → 492 行，close_time 全部为 NULL；
#   CLOSED(2)→ 1157 行，close_time 全部非空。
ORDER_STATUS_OPEN = 1
ORDER_STATUS_CLOSED = 2


# ── 2026-09-08：signal_mode → MT5 magic 逻辑编号 ──
# 需求：自动信号开仓时把来源逻辑写入 MT5 magic，终端可直接辨识该单由哪条逻辑触发。
# 0 保留 = 手动单 / 未归类；manual_mirror 保持透传主号原 magic（既有语义，不映射）。
# 注：live_override 的两个 bar 内子来源（adx 救援 / 实时评分边沿）signal_mode 相同 → 同码 21。
SIGNAL_MODE_MAGIC = {
    "hexp": 11,           # HEXP 乘幂引擎 M5 bar 收盘主信号（signal_mode 前缀 "HEXP"）
    "scoring": 12,        # 默认评分引擎 M5 bar 收盘主信号（"<REGIME>_WEIGHTS" / "indicator_scoring"）
    "live_override": 21,  # bar 内实时触发（adx 救援 / 实时评分边沿 / momentum_pending 放行）
    # 【2026-09-09】RANGE 均值回归（range_strategy 注入）→ MT5 magic 55。
    # 注意：RANGE 是注入到 HEXP 结果之上，signal_mode 仍形如 "HEXP:Regime.RANGE"，
    #   仅凭 mode 字符串无法与 HEXP 自身在震荡市出的信号区分（后者应仍为 11）。
    #   故 magic_for_signal_mode 不识别 RANGE，改由 scheduler 依 range_mode 标志
    #   显式覆盖（单一真源仍在此表）。
    "range": 55,
    # 【2026-09-15 §12-4】行情状态机 FSM 的子模式（方案 §18.3 状态→订单映射）。
    # 为什么必须有**两个**子模式而不是一个 `state_fsm`：
    #   `tools/position_sync.py` 的平仓归因要靠它区分"**震荡**止损"与"趋势止损" ——
    #   规格 9.4 的 4ATR 锁止预算是**震荡态专用**的，若趋势亏损也计入会无端触发 S5。
    #   故子模式字符串里必须带 "osc" 才能被识别（见 position_sync 的过滤逻辑）。
    "state_osc": 61,      # S1 箱体逆势单
    "state_trend": 62,    # S2/S3/S4 顺势单（含加仓）
}


def magic_for_signal_mode(signal_mode: str) -> int:
    """signal_mode → MT5 magic 逻辑编号；未识别/空返回 0（手动/未归类）。"""
    m = str(signal_mode or "").strip().upper()
    if m.startswith("HEXP"):
        return SIGNAL_MODE_MAGIC["hexp"]
    if m == "LIVE_OVERRIDE":
        return SIGNAL_MODE_MAGIC["live_override"]
    if m == "INDICATOR_SCORING" or m.endswith("_WEIGHTS"):
        return SIGNAL_MODE_MAGIC["scoring"]
    # FSM 子模式（精确匹配，避免把未来的 state_* 变体误映射）
    if m == "STATE_OSC":
        return SIGNAL_MODE_MAGIC["state_osc"]
    if m == "STATE_TREND":
        return SIGNAL_MODE_MAGIC["state_trend"]
    return 0


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
    # ── 2026-08-28 修复：多因子外部市场评分(composite)落库 + 参与方向/仓位 ──
    # 此前 composite_score 列在 SignalData 与 INSERT 中均缺失 → 全表 NULL（BUG，非设计）。
    # 取值自 hcm-market-intel 计算的宏观+情绪+事件+流动性 4 维聚合分(0~1，Redis
    # hcm:market:composite:score，每 30s 刷新)，代表「外部市场环境对本品种交易的有利度」。
    # 用途：(1) 落库供复盘归因；(2) 弱证据信号(composite 极低 + NEUTRAL/兜底/RSI均值回归)
    # 降级为 NO_TRADE，根治震荡市恶劣环境下被反复扫损；(3) 作为 suggested_lot 衰减系数，
    # composite 低→减仓，高→不衰减（与 AI 手数分档协同，不颠覆既有分档）。
    composite_score: float = 0.0
    signal_mode: str = "indicator_scoring"
    magic: int = 0  # MT5 magic 号（手动跟单时透传主号原 magic，桥侧下单时用此值覆盖默认 123456）
    # ── 【2026-09-15 §43】行情状态机（FSM）字段：**跨组件消费，故为顶层流字段** ──
    # 与 `ai_sl_mult` / `sl_locked` / `zone_tp_level` 同一约定。
    # 为什么不塞进 `indicator_values`：它到下游是 **JSON 字符串**（redis 封装层
    # `shared/redis_client.py:146` 对 dict/list 做 json.dumps），风控与桥都得先 json.loads；
    # 而既有跨组件字段一律用顶层。
    # ⚠ **新增流字段必须同步加进 risk-engine 的 `_publish_risk_passed` 白名单**，
    #   否则到不了桥 —— 历史事故：`zone_level` 等曾漏传 → 桥侧取 0 → "出信号不下单"。
    fsm_state: str = ""
    fsm_lot_multiplier: float = 1.0     # S1 梯度(0.5/1.0/1.5/2.0) / 趋势=1.0，乘在风控 base 上
    indicator_values: dict = field(default_factory=dict)
    macro_snapshot_id: Optional[int] = None
    sentiment_snapshot_id: Optional[int] = None
    fallback_reason: Optional[str] = None
    regime: Optional[str] = None
    pre_score: Optional[float] = None
    weight_scheme: Optional[str] = None
    position_in_range: Optional[float] = None
    # 2026-08-27 C4 位置/极值溯源字段：复盘"高位开多/低位开空"止损归因用。
    position_cycle: Optional[float] = None   # 长窗口极值分位[0,1]
    position_z: Optional[float] = None       # (close-SMA)/ATR 偏离
    ma_raw: Optional[float] = None            # 0-100 多头度
    cycle_pos_blocked: bool = False          # 周期位置守卫是否拦截
    extreme_reversal_blocked: bool = False   # 极值反转护栏是否拦截
    threshold_passed: Optional[bool] = None  # 极值护栏是否放行
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
    # 【2026-09-08 审计修复 P0】信号塔已"锁定止损"标记：趋势启动单(3.5ATR)等由本塔
    # 显式给定、不应被桥侧「会话 SL 下限兜底」(mt5_bridge 把 <2ATR 的止损抬到会话值)
    # 反向拉宽的场景置 True。经风控 stream_consumer 透传至 signal:risk_passed，
    # 由桥 place_mt5_order 消费。默认 False = 桥侧行为完全不变。
    sl_locked: bool = False
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
    # 2026-08-25 极值分层裁决：hexp 极值区+动量回撤但该 symbol 已有同向保本持仓时置 True，
    # 不硬封方向，交由风控保本闸门最终裁决（放行+轻仓 / 拦截）。True 表示"极值追单候选"。
    extreme_pending: bool = False
    # 2026-08-26 反向单：经 momentum_flip 封 NO_TRADE 后由 reverse_candidate 覆写方向产出的
    # 接刀单。True 表示本信号为高位动量反转反向单，交由风控 _check_reverse_order 接刀护栏
    # 裁决（按账户保本/持仓状态，未达条件则拒绝，防盲目接刀）。
    reverse_order: bool = False
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
            # ── FSM 字段（跨组件消费；新增流字段必须同步风控白名单，见 SignalData 注释）──
            "fsm_state": signal.fsm_state,
            "fsm_lot_multiplier": signal.fsm_lot_multiplier,
            # 【2026-09-16 冲突③修复】trail_mult/exit_ready/trail_lookback 不再经信号字段透传：
            # 它们由塔每 bar 刷新的 `hcm:state:directive:{symbol}` 承载（桥 FSM 移动止损分支的
            # 唯一真值源），信号字段那份是开仓时冻结快照且桥从不消费 → 留双真值易失同步，删除。
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
            # 2026-08-25 极值分层裁决标记：hexp 极值+保本追单候选，风控保本闸门消费
            # 【2026-09-08 审计修复 P0】Redis Stream 字段值只能是字符串：Python bool
            # 写入后被编码为 "True"/"False"，下游裸用 bool() 时 bool("False") 恒为
            # True（非空字符串恒真）→ extreme_pending 恒真使每条信号都进极值追单
            # 分支（手数被无条件 ×0.5、DB 抖动时 fail-closed 全量拒单）。
            # 源头统一写 0/1 规范值，下游按 "1" 判定（见 risk rule_chain._as_bool）。
            "extreme_pending": 1 if getattr(signal, "extreme_pending", False) else 0,
            # 2026-08-26 反向单标记：momentum_flip 封 NO_TRADE 后覆写方向产出的接刀单，
            # 风控 _check_reverse_order 接刀护栏消费
            "reverse_order": 1 if getattr(signal, "reverse_order", False) else 0,
            # 【2026-09-08 审计修复 P0】"信号塔已锁定止损"标记（趋势抢跑/极值追单等
            # 由信号塔显式给定止损的场景置 1）→ 风控透传 → 桥跳过会话 SL 下限兜底。
            "sl_locked": 1 if getattr(signal, "sl_locked", False) else 0,
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
                        composite_score,
                        created_at, signal_status)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                               $12, $13, $14, $15, $16, $17, $18,
                               $19, $20, $21, $22, $23, $24, $25, $26, $27)
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
                            # 2026-08-28 修复：C4 溯源字段(position_cycle/position_z/ma_raw/
                            # cycle_pos_blocked/extreme_reversal_blocked/threshold_passed)
                            # 因 PG 表 hcm_signal.signals 无对应列而无法直接落库，暂存于
                            # indicator_values JSONB（与 _collab/_component_scores 同源），
                            # 避免 INSERT 整体失败。后续若需独立列再做 ALTER TABLE。
                            "_c4_trace": {
                                "position_cycle": signal.position_cycle,
                                "position_z": signal.position_z,
                                "ma_raw": signal.ma_raw,
                                "cycle_pos_blocked": signal.cycle_pos_blocked,
                                "extreme_reversal_blocked": signal.extreme_reversal_blocked,
                                "threshold_passed": signal.threshold_passed,
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
                    signal.composite_score,
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
                      AND o.order_status = $1
                    GROUP BY o.signal_id
                ) agg ON s.signal_id = agg.sid
                WHERE NOT EXISTS (
                    SELECT 1 FROM hcm_ai.labeled_samples ls WHERE ls.signal_id = s.signal_id
                )
                ON CONFLICT (symbol, timeframe, bar_time) DO NOTHING
                """,
                ORDER_STATUS_CLOSED,
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
                      AND o.order_status = $1
                    GROUP BY o.signal_id
                ) agg
                WHERE ls.signal_id = agg.sid
                  AND ls.label IS NULL
                  AND agg.net_profit IS NOT NULL
                """,
                ORDER_STATUS_CLOSED,
            )
            total = await self._db.fetchval(
                "SELECT COUNT(*) FROM hcm_ai.labeled_samples WHERE label IN ('win','loss')"
            )
            return int(total) if total is not None else 0
        except Exception as exc:  # noqa: BLE001
            logger.error("reconcile_labels failed: %s", exc)
            return 0

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
