"""Risk Engine Stream Consumer — Redis Stream XREADGROUP consumer.

Consumes from signal:stream using the risk-engine-group consumer group.
Features:
- XGROUP CREATE on startup (idempotent)
- Pending message recovery (unacknowledged from previous crashes)
- ACK on successful processing
- Dead letter queue on persistent failure (3 retries)
- Graceful shutdown with inflight message drain
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.redis_client import RedisClient, StreamMessage, validate_safety_config, CRITICAL_SAFETY_KEYS
from shared.errors import ErrorCode, HcmError

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

SIGNAL_STREAM = "signal:stream"
RISK_PASSED_STREAM = "signal:risk_passed"
DEAD_LETTER_STREAM = "signal:dead"
GROUP_NAME = "risk-engine-group"
DEFAULT_CONSUMER_NAME = "risk-consumer-1"
DEFAULT_BLOCK_MS = 5000
DEFAULT_RETRY_MAX = 3
DEFAULT_RETRY_DELAY = 0.5


@dataclass
class RiskConsumerConfig:
    """Configuration for the risk engine stream consumer."""
    group_name: str = GROUP_NAME
    consumer_name: str = DEFAULT_CONSUMER_NAME
    signal_stream: str = SIGNAL_STREAM
    risk_passed_stream: str = RISK_PASSED_STREAM
    dead_stream: str = DEAD_LETTER_STREAM
    block_ms: int = DEFAULT_BLOCK_MS
    retry_max: int = DEFAULT_RETRY_MAX
    retry_delay: float = DEFAULT_RETRY_DELAY


class RiskStreamConsumer:
    """Redis Stream consumer that reads trading signals and feeds them
    into the risk rule chain.

    Operates as part of the risk-engine-group consumer group on
    signal:stream. Each message is processed through the rule chain
    and either published to signal:risk_passed (PASS/DEGRADE) or
    blocked (REJECT — recorded and discarded).

    Example:
        consumer = RiskStreamConsumer(
            redis_client=redis_client,
            rule_chain=rule_chain,
            decision_engine=decision_engine,
        )
        await consumer.start()
    """

    def __init__(
        self,
        redis_client: RedisClient,
        rule_chain: Any = None,
        decision_engine: Any = None,
        config: Optional[RiskConsumerConfig] = None,
        db_pool: Any = None,
    ):
        """Initialize RiskStreamConsumer.

        Args:
            redis_client: Initialized RedisClient instance.
            rule_chain: RuleChain instance for signal validation.
            decision_engine: DecisionEngine instance for PASS/REJECT/DEGRADE.
            config: Optional consumer configuration.
            db_pool: DatabasePool for updating signal status.
        """
        self._redis = redis_client
        self._rule_chain = rule_chain
        self._decision = decision_engine
        self._config = config or RiskConsumerConfig()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._db = db_pool

        # Statistics
        self._stats: dict[str, int] = {
            "messages_consumed": 0,
            "messages_passed": 0,
            "messages_rejected": 0,
            "messages_degraded": 0,
            "messages_dead_letter": 0,
            "messages_ack_failed": 0,
        }

    async def _apply_dynamic_lot(self, signal_data: dict) -> None:
        """Compute dynamic lot size based on confidence (config-driven, no hardcode).

        手动镜像白名单：signal_mode='manual_mirror' 时跳过 dynamic_lot（透传主号原 lot，
        1:1 镜像；风险硬规则仍由 rule_chain 评估）。
        """
        try:
            if self._redis is None:
                return
            # 手动模式镜像：原样保留主号手数
            if str(signal_data.get("signal_mode", "")) == "manual_mirror":
                return

            async def get(key: str, default: float) -> float:
                try:
                    v = await self._redis.hget("hcm:config:v2", key)
                    return float(v) if v is not None else default
                except Exception:
                    return default

            symbol = str(signal_data.get("symbol", ""))
            max_lot = await get("risk.max_lot_per_trade", 0.0)
            # 基础手数：优先品种 tower.lot_size，但仅在其不超过单笔上限时采用；否则回退 risk.lot_base
            base = await get("risk.lot_base", 0.01)
            if symbol:
                tower_lot = await get(f"symbol.{symbol}.tower.lot_size", None)
                if tower_lot and (max_lot <= 0 or tower_lot <= max_lot):
                    base = tower_lot
            # 共源 G3 / AI 动态手数倍率：scheduler 已将 co_exec_lot_mult 与 suggested_lot_ratio 合并进此字段
            co_ai_mult = float(signal_data.get("suggested_lot_ratio", 1.0) or 1.0)
            score = float(signal_data.get("confidence", 0.0))
            # 【2026-08-31 口径修复】confidence 为 0–100 制（2026-08-28 迁移，耦合分/scorecard_total），
            # 而 risk.score_tier_{low,mid,high} 为 0–1 制（生产=0.50/0.80/0.95）。直接比较会让任意
            # 0–100 分值恒 ≥ 0.95 → 恒判 high 档(×1.5)，低耦合分(62/67/72)本应 <80→low(×0.5) 却被放大。
            # 归一为 0–1 后再分档（若输入已是 0–1 制则原样保留，兼容历史），零新增配置键。
            if score > 1.0:
                score = score / 100.0
            high_threshold = await get("risk.score_tier_high", 0.85)
            mid_threshold = await get("risk.score_tier_mid", 0.65)
            low_threshold = await get("risk.score_tier_low", 0.50)
            mult_high = await get("risk.lot_multiplier_high", 2.0)
            mult_mid = await get("risk.lot_multiplier_mid", 1.0)
            mult_low = await get("risk.lot_multiplier_low", 0.5)

            # ── AI 手数分档链动（2026-08-14 需求）──
            # 若 signal_tower 经 AI 融合闸门给出了明确档位（low/mid/high），
            # 则优先用 AI 选的档决定倍率（读风控面板 risk.lot_multiplier_*），
            # 且不再叠加 suggested_lot_ratio 的 AI 倍率（co_ai_mult→1），
            # 保证「准确下单值 = risk.lot_base × 风控档倍率」完全由风控面板决定。
            ai_tier = str(signal_data.get("ai_lot_tier", "none") or "none").lower()
            if ai_tier in ("low", "mid", "high"):
                tier = ai_tier
                multiplier = {"low": mult_low, "mid": mult_mid, "high": mult_high}[ai_tier]
                co_ai_mult = 1.0  # AI 只选档，倍率已含在 multiplier 内
                logger.info(
                    "AI lot_tier override: signal_id=%s ai_tier=%s → multiplier=%.2f "
                    "(risk-panel driven, co_ai_mult disabled)",
                    signal_data.get("signal_id", 0), ai_tier, multiplier,
                )
            elif score >= high_threshold:
                multiplier = mult_high
                tier = "high"
            elif score >= mid_threshold:
                multiplier = mult_mid
                tier = "mid"
            elif score >= low_threshold:
                multiplier = mult_low
                tier = "low"
            else:
                # 低置信（< score_tier_low 但已通过 min_confidence 闸门）给最小档而非 0，
                # 避免被桥侧 if lot<=0 兜底掩盖意图（修复 B1-RC-C）
                multiplier = mult_low
                tier = "low"

            raw_lot = base * multiplier * (co_ai_mult if co_ai_mult > 0 else 1.0)
            # 安全封顶：不超过风控单笔上限，防止 tower.lot_size / 倍率误配导致超限被整单拒绝
            if max_lot and max_lot > 0 and raw_lot > max_lot:
                raw_lot = max_lot
            new_lot = round(raw_lot, 4) if raw_lot > 0 else 0.0
            old_lot = float(signal_data.get("lot", 0.0))
            if new_lot != old_lot:
                signal_data["lot"] = new_lot
                logger.info(
                    "Dynamic lot: signal_id=%s score=%.2f → tier=%s, lot=%.4f "
                    "(base=%.4f mult=%.2f co_ai=%.2f max_lot=%.4f, was %.4f)",
                    signal_data.get("signal_id", 0), score, tier, new_lot,
                    base, multiplier, co_ai_mult, max_lot, old_lot,
                )
        except Exception as exc:
            logger.warning("Dynamic lot calc failed: %s (keeping original lot)", exc)

    # ── Lifecycle ───────────────────────────────

    async def start(self) -> bool:
        """Start the consumer loop as a background task.

        Returns:
            True  — 消费循环已启动（或本就在运行）；
            False — 启动自检失败（安全配置校验不通过 / 校验抛异常），
                    消费循环**未**启动，风控处于停摆状态。

        【2026-08-28 P1-11】原实现在启动自检失败时静默 `return`，调用方（main.py）
        既不检查返回值也照常打印 "fully initialized" → 风控实际停摆，而容器健康检查
        与日志都显示"健康"，signal:stream 中的信号无人裁决、风控形同虚设。
        改为返回 bool，供调用方 fail-fast（见 main.py 的启动检查）。
        """
        if self._running:
            return True

        # Ensure consumer group exists.
        # start_id="0" means: if the group is being created for the first
        # time, start consuming from the very first message in the stream.
        # This guarantees no signal is missed even if signals were published
        # before the risk engine came online.  On subsequent restarts the
        # group already exists (BUSYGROUP) and start_id is ignored — the
        # group continues from its last delivered position.
        # 【2026-08-28 P0-5】start_id 由 "0" 改为 "$"。
        # 原值 "0"（E 组 P2-15 引入，意图是"风控上线前发布的信号不丢失"）意味着：
        # 一旦消费组不存在（Redis 重建 / RDB 回滚 / 运维 XGROUP DESTROY），会把
        # signal:stream 内 maxlen=10000 的历史信号全量重放，经风控 PASS 后推入
        # signal:risk_passed，由桥/跟单批量开仓 —— 属不可接受的放大面。
        # 改为 "$" 后仅消费启动之后的新信号；"不丢信号"由采集侧的持久化和
        # 桥的年龄闸门（bridge.max_signal_age_seconds）保证，而非靠全量重放。
        await self._redis.xgroup_create(
            self._config.signal_stream,
            self._config.group_name,
            mkstream=True,
            start_id="$",
        )

        # Also create group for risk_passed stream (for future consumers)
        await self._redis.xgroup_create(
            self._config.risk_passed_stream,
            "dispatcher-group",
            mkstream=True,
            start_id="$",
        )
        await self._redis.xgroup_create(
            self._config.risk_passed_stream,
            "copy-trading-group",
            mkstream=True,
            start_id="$",
        )

        # ── Safety Rail: validate critical config values ──
        try:
            config_hash = await self._redis.hgetall("hcm:config:v2")
            ok, errors = validate_safety_config(config_hash)
            if not ok:
                for e in errors:
                    logger.critical("SAFETY FAIL: %s", e)
                logger.critical("Aborting startup — safety config validation failed")
                return False
            logger.info("Safety config validated: %d keys OK", len(CRITICAL_SAFETY_KEYS))
        except Exception as exc:
            logger.critical("Safety validation failed: %s — aborting startup", exc)
            return False

        self._running = True
        self._task = asyncio.create_task(self._consume_loop())
        logger.info(
            "RiskStreamConsumer started: group=%s, stream=%s",
            self._config.group_name, self._config.signal_stream,
        )
        return True

    async def stop(self) -> None:
        """Gracefully stop the consumer loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("RiskStreamConsumer stopped (stats=%s)", self._stats)

    # ── Main Loop ───────────────────────────────

    async def _consume_loop(self) -> None:
        """Main consumption loop: XREADGROUP → validate → decide → ACK."""
        logger.info(
            "RiskStreamConsumer loop started: consumer=%s",
            self._config.consumer_name,
        )

        # 1. Recover pending messages first
        await self._recover_pending()

        # 2. Main consumption loop
        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    group=self._config.group_name,
                    consumer=self._config.consumer_name,
                    streams={self._config.signal_stream: ">"},
                    count=1,
                    block=self._config.block_ms,
                )

                for msg in messages:
                    await self._process_message(msg)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("RiskStreamConsumer loop error: %s", exc)
                await asyncio.sleep(1.0)

        logger.info("RiskStreamConsumer loop ended")

    # ── Message Processing ──────────────────────

    async def _process_message(self, msg: StreamMessage) -> None:
        """Process a single stream message through the risk pipeline.

        Args:
            msg: StreamMessage from Redis Stream.
        """
        self._stats["messages_consumed"] += 1
        trace_id = msg.data.get("trace_id", "")
        symbol = msg.data.get("symbol", "?")
        signal_id = msg.data.get("signal_id", 0)
        t0 = time.time()

        logger.debug(
            "Risk processing: signal_id=%s, symbol=%s, msg_id=%s",
            signal_id, symbol, msg.message_id,
        )

        success = False
        last_error: Optional[str] = None

        for attempt in range(1, self._config.retry_max + 1):
            try:
                # ── 手动跟单生命周期动作免规则链 (2026-07-23，根因修复) ──
                # manual_mirror 的 close/modify/partial_close/add 是对「已通过风控并开仓」的跟单号
                # 的跟随操作，不消耗新开仓风控额度。若仍跑规则链，跟单号已持仓会使
                # risk_max_open_positions（开仓数已满）与 risk_cool_minutes（开仓冷却未过）把平仓/
                # 改单直接 REJECT，导致「主号平仓、跟单号不继承」（即"跟单号没同步"）。
                # 故对生命周期动作强制放行；硬上限/硬止损等不可逆护栏仍在桥侧执行。
                _sig_mode = str(msg.data.get("signal_mode", "")).lower()
                _sig_action = str(msg.data.get("action", "")).lower()
                _LIFECYCLE = ("close", "modify", "partial_close", "add", "add_position")
                if _sig_mode == "manual_mirror" and _sig_action in _LIFECYCLE:
                    from risk_engine.rule_chain import RuleChainResult, RuleResult
                    rule_result = RuleChainResult(
                        passed=True,
                        results=[RuleResult(
                            rule_name="manual_mirror_lifecycle_bypass",
                            passed=True,
                            message="手动跟单生命周期动作免规则链",
                        )],
                        rejected_rules=[],
                        violations={},
                    )
                    decision = "PASS"
                    logger.info(
                        "Manual mirror lifecycle bypass rule chain: action=%s sig=%s",
                        _sig_action, msg.data.get("signal_id"),
                    )
                else:
                    # 先按置信分/共源/AI 计算动态手数，再走规则链，
                    # 使 risk_max_lot_single / risk_max_total_lot 上限闸对真实手数生效（修复 B1-RC-A）
                    await self._apply_dynamic_lot(msg.data)

                    # Run rule chain
                    if self._rule_chain is not None:
                        rule_result = await self._rule_chain.evaluate(msg.data)
                    else:
                        # Default: pass all if no rule chain configured
                        from risk_engine.rule_chain import RuleChainResult
                        rule_result = RuleChainResult(passed=True)

                    # Make decision — DecisionEngine expects RuleChainResult
                    if self._decision is not None:
                        decision = await self._decision.decide(msg.data, rule_result)
                    else:
                        decision = "PASS"

                # Handle decision
                if decision == "REJECT":
                    self._stats["messages_rejected"] += 1
                    logger.info(
                        "Signal REJECTED: signal_id=%s, symbol=%s, rules=%s",
                        signal_id, symbol, rule_result.rejected_rules,
                    )
                    await self._audit_signal_stage(
                        signal_id, "rejected",
                        reason=",".join(map(str, rule_result.rejected_rules)),
                    )
                    await self._update_signal_status(
                        signal_id, 2, decision,
                        signal_mode=str(msg.data.get("signal_mode", "")),
                        block_reason=",".join(map(str, rule_result.rejected_rules)))
                elif decision == "DEGRADE":
                    self._stats["messages_degraded"] += 1
                    # Publish degraded signal to risk_passed
                    # 【P0-4】发布失败不得标记 PASS/ACK：抛错走重试，最终进死信，
                    # 杜绝"信号被 ACK、PG 标 PASS 却永远到不了桥"的假 PASS。
                    _pub = await self._publish_risk_passed(msg.data, decision, rule_result)
                    if not _pub:
                        raise RuntimeError(
                            f"risk_passed publish failed signal_id={signal_id}")
                    # 【2026-09-08 审计清理】原此处调用 rule_chain.mark_cooldown() 启动
                    # 冷却计时，但该方法自 2026-08-03 起已废弃为空实现（同向闸门改为
                    # 「同向保本闸门」_check_cooldown，不再用 Redis 时间窗键）。
                    # 调用点与空方法一并删除，避免维护者误以为冷却在此启动。
                    await self._update_signal_status(
                        signal_id, 1, decision,
                        signal_mode=str(msg.data.get("signal_mode", "")))
                else:  # PASS
                    self._stats["messages_passed"] += 1
                    logger.info(
                        "Signal PASSED: signal_id=%s, symbol=%s, direction=%s, score=%.3f, rules=%s",
                        signal_id, symbol,
                        msg.data.get("direction", "?"),
                        float(msg.data.get("pre_score", 0)),
                        ", ".join(r.rule_name for r in rule_result.results if r.passed)
                        if rule_result and rule_result.results else "n/a",
                    )
                    _pub = await self._publish_risk_passed(msg.data, decision, rule_result)
                    if not _pub:
                        raise RuntimeError(
                            f"risk_passed publish failed signal_id={signal_id}")
                    # 【2026-09-08 审计清理】原此处调用 rule_chain.mark_cooldown() 启动
                    # 冷却计时，但该方法自 2026-08-03 起已废弃为空实现（同向闸门改为
                    # 「同向保本闸门」_check_cooldown，不再用 Redis 时间窗键）。
                    # 调用点与空方法一并删除，避免维护者误以为冷却在此启动。
                    await self._update_signal_status(
                        signal_id, 1, decision,
                        signal_mode=str(msg.data.get("signal_mode", "")))

                success = True
                break

            except HcmError as exc:
                last_error = f"[{exc.code.value}] {exc.message}"
                logger.warning(
                    "Risk processing attempt %d/%d failed (signal_id=%s): %s",
                    attempt, self._config.retry_max, signal_id, exc,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Risk processing attempt %d/%d failed (signal_id=%s): %s",
                    attempt, self._config.retry_max, signal_id, exc,
                )

            if attempt < self._config.retry_max:
                await asyncio.sleep(self._config.retry_delay * attempt)

        # ACK or dead letter
        if success:
            acked = await self._redis.xack(
                self._config.signal_stream,
                self._config.group_name,
                msg.message_id,
            )
            if acked == 0:
                self._stats["messages_ack_failed"] += 1
                logger.warning("ACK returned 0 for msg_id=%s", msg.message_id)
        else:
            # Move to dead letter queue
            self._stats["messages_dead_letter"] += 1
            await self._send_to_dead_letter(msg, last_error or "unknown error")
            # Still ACK so we don't reprocess indefinitely
            await self._redis.xack(
                self._config.signal_stream,
                self._config.group_name,
                msg.message_id,
            )

        latency_ms = int((time.time() - t0) * 1000)
        logger.debug(
            "Risk processing done: signal_id=%s, latency=%dms, success=%s",
            signal_id, latency_ms, success,
        )

    async def _audit_signal_stage(self, signal_id, stage, **fields):
        """审计信号执行链路: signal_id → risk_passed/rejected/executed/expired/...

        用独立 key hcm:signal_exec:{sid}:{stage} 累积各阶段状态(TTL 7天)，
        便于定位『信号产出 → 风控 → 桥执行』链路中任一环节的漏单
        （如某 sig 有 risk_passed 却无 executed/bridge_failed，即丢失在桥端）。
        """
        if signal_id is None:
            return
        try:
            _payload = {"at": datetime.now(timezone.utc).isoformat()}
            _payload.update({k: str(v) for k, v in fields.items()})
            _key = f"hcm:signal_exec:{signal_id}:{stage}"
            await self._redis.set(_key, json.dumps(_payload), ex=7 * 86400)
        except Exception as _e:
            logger.warning("audit signal %s stage=%s failed: %s", signal_id, stage, _e)

    # ── Risk Passed Publishing ──────────────────

    async def _publish_risk_passed(
        self,
        signal_data: dict,
        decision: str,
        rule_result: Any,
    ) -> Optional[str]:
        """Publish a risk-passed signal to signal:risk_passed stream.

        Args:
            signal_data: Original signal data from signal:stream.
            decision: PASS or DEGRADE.
            rule_result: Rule chain evaluation result.

        Returns:
            Message ID of the published message, or None on failure.
        """
        risk_passed_data = {
            "event": "risk_check_passed" if decision == "PASS" else "risk_check_degraded",
            # 发射时刻(T1)：保留用于审计；新鲜度闸门改用 signal_generated_at
            "timestamp": datetime.now(timezone.utc).isoformat(),
            # 透传信号塔在 T0(信号基于行情计算那一刻)打的原始时间戳，
            # 不被上面的 timestamp 覆盖——bridge 据此算"行情真实年龄"，
            # 并能做 latest-wins 排序。修复：原先 timestamp 在 T1 重写，T0→T1 风控延迟被吞掉。
            # 2026-08-05 (D7-1): 优先取信号塔显式写入的 signal_generated_at(T0=produced_at)；
            # 缺失时回退旧版 timestamp(发射时刻)以兼容未升级的信号塔。
            "signal_generated_at": signal_data.get("signal_generated_at") or signal_data.get("timestamp", ""),
            "signal_id": signal_data.get("signal_id", 0),
            "task_id": signal_data.get("task_id", 0),
            "account_id": signal_data.get("account_id", 0),
            "symbol": signal_data.get("symbol", ""),
            "time_frame": signal_data.get("time_frame", "M5"),
            "direction": signal_data.get("direction", ""),
            "entry_price": signal_data.get("entry_price", 0.0),
            "sl_price": signal_data.get("sl_price", 0.0),
            "tp1": signal_data.get("tp1", 0.0),
            "tp2": signal_data.get("tp2", 0.0),
            "lot": signal_data.get("lot", 0.0),
            "confidence": signal_data.get("confidence", 0.0),
            "decision": decision,
            "passed_rules": json.dumps(
                [r.rule_name for r in rule_result.results if r.passed]
            ) if rule_result.results else "[]",
            "rejected_rules": json.dumps(rule_result.rejected_rules),
            "violations": json.dumps(rule_result.violations),
            "trace_id": signal_data.get("trace_id", ""),
            "signal_mode": signal_data.get("signal_mode", ""),  # 修复：manual_mirror 护栏依赖此字段
            "magic": signal_data.get("magic", 0),  # 透传主号原 magic（手动跟单桥侧下单时用主号原 magic）
            "action": signal_data.get("action", ""),  # 透传 manual_mirror 动作（open/close/modify/partial_close/add）
            "close_mode": signal_data.get("close_mode", "all"),  # 2026-07-23：精确平仓模式
            "close_ticket": int(signal_data.get("close_ticket", 0) or 0),  # 2026-07-23：主号被平/改的 ticket
            # ── P1a/P1c 协作字段透传（zone-trigger 延迟/等回踩入场 + AI 风险 SL/TP 锚点）──
            # 桥侧 (mt5_bridge.py) 的 P1a zone gate 依赖 zone_level/zone_type/entry_trigger_wait
            # 才能按信号塔意图延迟/等回踩入场；P1c 依赖 ai_sl_mult/ai_tp_mult/suggested_lot_ratio
            # 计算 SL/TP 锚点。此前白名单漏传这些字段 → 桥侧 zl=0 → zone gate 永不触发 →
            # 信号直接市价追单被滑点闸门拒单（"出信号不下单"的并发根因之一）。必须透传。
            "zone_level": float(signal_data.get("zone_level", 0) or 0),
            "zone_type": signal_data.get("zone_type", "") or "",
            "zone_strength": int(signal_data.get("zone_strength", 0) or 0),
            "zone_tp_level": float(signal_data.get("zone_tp_level", 0) or 0),
            "entry_trigger_wait": int(signal_data.get("entry_trigger_wait", 0) or 0),
            "ai_sl_mult": float(signal_data.get("ai_sl_mult", 0) or 0),
            "ai_tp_mult": float(signal_data.get("ai_tp_mult", 0) or 0),
            "suggested_lot_ratio": float(signal_data.get("suggested_lot_ratio", 1.0) or 1.0),
            # AI 手数分档（low/mid/high/none）→ 桥端跟单/观测用（链动风控动态手数）
            "ai_lot_tier": str(signal_data.get("ai_lot_tier", "none") or "none"),
            "co_exec_fb": int(signal_data.get("co_exec_fb", 0) or 0),  # 盲点兜底单：桥端 zone 到期不市价追
            # 2026-08-26 反向单标记：momentum_flip 封 NO_TRADE 后覆写方向产出的接刀单，
            # 风控 _check_reverse_order 消费；透传供桥端诊断/日志识别。
            # 【2026-09-08 审计修复 P0】Redis Stream 字段值只能是字符串：Python bool
            # 写入后被编码为 "True"/"False"，下游裸用 bool() 时 bool("False") 恒为
            # True → 反向单护栏误伤正常开仓单。统一写 0/1 规范值，下游按 "1" 判定。
            "reverse_order": 1 if str(signal_data.get("reverse_order", False)).strip().lower()
                             in ("1", "true", "yes", "on") else 0,
            # 【2026-09-08 审计修复 P0】"信号塔已锁定止损"标记透传给桥：桥侧据此跳过
            # 会话 SL 下限兜底（避免把信号塔精确止损反向拉宽）。默认 0 = 桥侧行为不变。
            "sl_locked": 1 if str(signal_data.get("sl_locked", 0)).strip().lower()
                         in ("1", "true", "yes", "on") else 0,
        }

        try:
            msg_id = await self._redis.xadd(
                self._config.risk_passed_stream,
                risk_passed_data,
            )
            if msg_id:
                logger.debug(
                    "Risk passed published: signal_id=%s, decision=%s → %s",
                    signal_data.get("signal_id"), decision, msg_id,
                )
                await self._audit_signal_stage(
                    signal_data.get("signal_id"), "risk_passed", decision=decision,
                )
            return msg_id
        except Exception as exc:
            logger.error(
                "Failed to publish risk_passed for signal_id=%s: %s",
                signal_data.get("signal_id"), exc,
            )
            return None

    # ── Dead Letter ─────────────────────────────

    async def _send_to_dead_letter(
        self,
        msg: StreamMessage,
        error: str,
    ) -> None:
        """Send a failed message to the dead letter queue.

        Args:
            msg: Original StreamMessage that failed.
            error: Error description.
        """
        dead_data = {
            "original_signal_id": msg.data.get("signal_id", 0),
            "symbol": msg.data.get("symbol", "?"),
            "direction": msg.data.get("direction", "?"),
            "failed_consumer": self._config.group_name,
            "error": error,
            "retry_count": self._config.retry_max,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "original_stream": msg.stream,
            "original_payload": json.dumps(msg.data, default=str),
        }

        try:
            await self._redis.xadd(self._config.dead_stream, dead_data)
            logger.error(
                "Signal dead letter: signal_id=%s, error=%s → %s",
                dead_data["original_signal_id"], error, self._config.dead_stream,
            )
        except Exception as exc:
            logger.critical(
                "CRITICAL: Cannot write to dead letter queue (signal_id=%s): %s",
                dead_data["original_signal_id"], exc,
            )

    # ── Pending Recovery ────────────────────────

    async def _recover_pending(self) -> None:
        """Recover and reprocess pending (unacknowledged) messages from
        the previous session's crash or disconnection."""
        try:
            pending_count = await self._redis.xpending(
                self._config.signal_stream,
                self._config.group_name,
            )
            if pending_count == 0:
                return

            logger.warning(
                "Found %d pending messages in group=%s — recovering",
                pending_count, self._config.group_name,
            )

            # Read pending messages using "0" (beginning of pending list)
            messages = await self._redis.xreadgroup(
                group=self._config.group_name,
                consumer=self._config.consumer_name,
                streams={self._config.signal_stream: "0"},
                count=min(pending_count, 50),
                block=1000,
            )

            for msg in messages:
                logger.info(
                    "Recovering pending message: signal_id=%s, msg_id=%s",
                    msg.data.get("signal_id"), msg.message_id,
                )
                await self._process_message(msg)

            # Check remaining
            remaining = await self._redis.xpending(
                self._config.signal_stream,
                self._config.group_name,
            )
            if remaining > 0:
                logger.warning(
                    "%d pending messages still remain after recovery", remaining,
                )
            else:
                logger.info("All pending messages recovered")

        except Exception as exc:
            logger.error("Pending message recovery failed: %s", exc)

    # ── Stats & Health ──────────────────────────

    def get_stats(self) -> dict:
        """Get consumer statistics.

        Returns:
            Dict with consumption counts.
        """
        return dict(self._stats)

    async def _update_signal_status(self, signal_id: int, status: int, decision: str = "",
                                    signal_mode: str = "", block_reason: str = "") -> None:
        """Update signal_status in PostgreSQL.

        语义（【B 组】统一）：0=published 在途 / 1=风控 PASS 未成交 / 2=风控 REJECT /
        3=桥已成交 / 4=publish_failed。

        【卡点② 修复·2026-08-03】两道护栏，杜绝「已成交(3)被覆盖回 1」：
        1. manual_mirror 信号不回写 signals 状态——手动/自动镜像的 close/modify/
           partial_close 复用【原开仓信号的 signal_id】（position_sync 经
           hcm:signal_for_ticket 解析），open 动作则用主号 ticket 当 signal_id。
           这些都不是新信号行，回写会把桥开仓时写的 3 覆盖成 1（实证：1512421
           11:48:52 成交置 3，12:04:34 平仓镜像 PASS 又置回 1）。
        2. SQL 加 `AND signal_status <> 3` —— 任何来源都不得让已成交状态降级。

        【2026-08-25 卡点标注修复】status=2(REJECT) 时把 rejected_rules 回写
        block_reason，使面板「最近信号」能看到风控拒绝的精准卡点（此前只写 Redis
        审计键，PG block_reason 恒空 → 面板"未标注卡点"）。

        Args:
            signal_id: Signal identifier.
            status: 1=PASS, 2=REJECT.
            decision: Decision string for logging.
            signal_mode: 信号模式（manual_mirror 时跳过回写）。
            block_reason: 拒绝原因（status=2 时写入 block_reason 列）。
        """
        if self._db is None or not self._db.is_initialized:
            return
        if str(signal_mode).lower() == "manual_mirror":
            logger.debug(
                "Skip signal_status write for manual_mirror signal_id=%s (reuses原开仓 signal_id)",
                signal_id)
            return
        try:
            if status == 2 and block_reason:
                await self._db.execute(
                    "UPDATE hcm_signal.signals SET signal_status=$1, block_reason=$2, "
                    "updated_at=now() WHERE signal_id=$3 AND signal_status <> 3",
                    status, block_reason, signal_id,
                )
            else:
                await self._db.execute(
                    "UPDATE hcm_signal.signals SET signal_status=$1, updated_at=now() "
                    "WHERE signal_id=$2 AND signal_status <> 3",
                    status, signal_id,
                )
            logger.debug("Signal status updated: signal_id=%s, status=%s", signal_id, status)
        except Exception as exc:
            logger.warning("Failed to update signal_status: signal_id=%s: %s", signal_id, exc)

    async def health_check(self) -> dict:
        """Check consumer health.

        Returns:
            Dict with status and stats.
        """
        redis_ok = False
        if self._redis.is_initialized:
            try:
                redis_ok = await self._redis.ping()
            except Exception:
                pass

        pending = 0
        if redis_ok:
            try:
                pending = await self._redis.xpending(
                    self._config.signal_stream,
                    self._config.group_name,
                )
            except Exception:
                pass

        return {
            "status": "healthy" if redis_ok and self._running else "degraded",
            "redis_ok": redis_ok,
            "running": self._running,
            "pending_messages": pending,
            "stats": self.get_stats(),
        }
