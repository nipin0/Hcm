"""Rule Chain — sequential risk rule evaluation from ConfigProviderV3.

Evaluates trading signals against a chain of risk rules with configurable
parameters read from ConfigProviderV3. Rules are evaluated in order and
short-circuit on the first rejection.

Rule Chain Order (matching PRD spec):
  risk_min_confidence → risk_max_lot_single → risk_max_total_lot →
  risk_max_open_positions → risk_max_daily_loss → risk_min_margin →
  risk_max_spread_pips → risk_cool_minutes → risk_spread_check_enabled
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 【2026-08-28 P0-4】订单状态枚举 ──────────────────────────────────────
# 铁律 4.6：禁止在 SQL/代码里裸写 1/2 等状态字面量（裸写 order_status=1 曾导致
# 1015 笔订单假 open、风控冷却全面失效，见铁律 10.1）。
# 语义经 PG 实测确认（2026-08-28）：OPEN(1)=492 行且 close_time 全部为 NULL；
# CLOSED(2)=1157 行且 close_time 全部非空。
ORDER_STATUS_OPEN = 1
ORDER_STATUS_CLOSED = 2

# ── 【2026-08-28 P0-4】DB 故障失败模式：fail-closed（故障安全）──────────
# 铁律 10.2：风控闸门不得单一依赖滞后表，且故障时不得"放行"。原实现在 DB 异常时
# 返回 0（fail-open），等于瞬间放开限仓与亏损熔断 —— DB 抖动即可打满仓位。
# 改为返回超出阈值的哨兵值，使闸门拒绝交易（表现为短暂拒单，属故障安全侧）。
# 如需临时恢复 fail-open，把这三个常量改回 0 / 0.0 / 0.0 即可，无需改逻辑。
DB_FAIL_CLOSED_COUNT = 9999      # 持仓数 → 触发限仓拒绝
DB_FAIL_CLOSED_LOT = 99999.0     # 总手数 → 触发限仓拒绝
DB_FAIL_CLOSED_LOSS = -99999.0   # 日亏   → 触发熔断

# ── 【2026-09-08 审计修复 P0】Redis Stream 布尔字段统一解析 ──────────────
# Redis Stream 的 field value 只能是字符串：Python bool 写入后被编码为
# "True"/"False"，消费侧裸用 bool() 时 bool("False") is True（非空字符串恒真）
# → 所有信号都被当成 True：
#   · extreme_pending 恒真 → 每条信号都进极值追单分支，suggested_lot_ratio 被
#     无条件 ×0.5，且该分支 DB 不可达时 fail-closed 全量拒单（与正常路径
#     fail-open 语义相反，DB 抖动即全局拒单）；
#   · reverse_order 恒真 → 反向单护栏误伤正常开仓单。
# 解析一律走本函数，禁止再裸用 bool(...) 解析 stream 字段。
def _as_bool(v: Any, default: bool = False) -> bool:
    """把 Redis Stream 里的布尔字段（str/int/bool）安全解析为 bool。"""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    return default


# ── Rule result types ──────────────────────────


@dataclass
class RuleResult:
    """Result of a single rule evaluation."""
    rule_name: str = ""
    passed: bool = True
    actual_value: float = 0.0
    threshold: float = 0.0
    message: str = ""


@dataclass
class RuleChainResult:
    """Aggregate result of the rule chain evaluation."""
    passed: bool = True
    results: list[RuleResult] = field(default_factory=list)
    rejected_rules: list[str] = field(default_factory=list)
    violations: dict[str, dict] = field(default_factory=dict)

    @property
    def first_rejection(self) -> Optional[RuleResult]:
        """Get the first rule that rejected the signal."""
        for r in self.results:
            if not r.passed:
                return r
        return None


class RuleChain:
    """Sequential risk rule chain for validating trading signals.

    Each rule is evaluated in order. The chain short-circuits on the
    first rejection (fail-fast). All thresholds are loaded from
    ConfigProviderV3 at startup and can be hot-reloaded.

    Example:
        chain = RuleChain(config_provider)
        await chain.load_config()
        result = await chain.evaluate(signal_data)
        if result.passed:
            print("Signal passed all risk checks")
    """

    def __init__(
        self,
        config_provider: Any = None,
        db_pool: Any = None,
        redis_client: Any = None,
    ):
        """Initialize RuleChain.

        Args:
            config_provider: ConfigProviderV3 instance for rule thresholds.
            db_pool: DatabasePool for querying open positions/daily losses.
            redis_client: RedisClient for cooldown tracking.
        """
        self._config = config_provider
        self._db = db_pool
        self._redis = redis_client

        # Cached thresholds (hot-reloadable) — MUST call load_config() before evaluate()
        self._min_confidence: float = -1.0
        self._max_lot_single: float = -1.0
        self._max_total_lot: float = -1.0
        self._max_open_positions: int = -1
        self._max_daily_loss: float = -1.0
        self._min_margin: float = -1.0
        self._max_spread_pips: float = -1.0
        self._cool_minutes: int = -1
        # [2026-08-22] 同向保本闸门：保本判定容差（价格单位）。
        # 桥保本时 SL = entry ± 0.15×ATR 严格优于 entry，默认 0 即可（仅兜浮点/点差）。
        self._be_tolerance: float = 0.0
        self._spread_check_enabled: bool = False

        # ── 分值驱动最大持仓数（2026-08-11 v2.5）──
        # 高分值信号允许更多持仓、低分值保守限制。
        # 三个档位阈值 + 对应最大持仓数，由前端面板热调。
        self._score_driven_positions_enabled: bool = False
        self._score_tier_low_positions: float = 0.50   # 低分值门槛（score < 此值）
        self._score_tier_mid_positions: float = 0.70   # 中分值门槛（低 ≤ score < 中）
        self._max_positions_low: int = 1               # 低分值最大持仓数
        self._max_positions_mid: int = 2               # 中分值最大持仓数
        self._max_positions_high: int = 3              # 高分值最大持仓数（score ≥ 中门槛）

        # Whether thresholds were ever loaded successfully. While False,
        # evaluate() FAILS CLOSED (rejects the signal) — a trading system must
        # never trade unconstrained when its risk gate is blind (the 7-20
        # explosive order-opening incident was exactly a blind/permissive gate).
        # The rejected signal is still consumed/acked downstream (no
        # signal:stream backup), and manual-mirror lifecycle actions
        # (close/modify) are bypassed BEFORE evaluate() in stream_consumer.py,
        # so closes/modifies still propagate even when config is unavailable.
        self._config_loaded: bool = False

    # ── Main API ────────────────────────────────

    async def evaluate(self, signal_data: dict) -> RuleChainResult:
        """Evaluate a signal through the full rule chain.

        Rules are evaluated in order with short-circuit on first rejection.

        Args:
            signal_data: Signal dict from Redis Stream message.

        Returns:
            RuleChainResult with all individual rule results.
        """
        result = RuleChainResult()

        # ── Fail-closed mode (2026-07-25) ──
        # If thresholds were NEVER loaded (config missing / provider down),
        # REJECT the signal (fail-closed) instead of permissive PASS. A blind
        # risk gate must not trade unconstrained: the 7-20 explosive
        # order-opening incident proved a permissive gate is worse than a
        # brief trading halt. The signal is still consumed/acked downstream
        # (no signal:stream backup). Manual-mirror lifecycle actions are
        # bypassed before evaluate() in stream_consumer.py, so closes/modifies
        # still propagate. Operators are alerted via CRITICAL log below.
        if not self._config_loaded:
            logger.critical(
                "RuleChain.evaluate: thresholds NOT loaded — FAIL-CLOSED, "
                "rejecting signal (risk gate blind)"
            )
            result.results.append(RuleResult(
                rule_name="config_unloaded",
                passed=False,
                actual_value=0.0,
                threshold=0.0,
                message="FAIL-CLOSED: risk thresholds not loaded; signal rejected",
            ))
            result.rejected_rules.append("config_unloaded")
            return result

        # Rule 1: Minimum Confidence
        r = self._check_confidence(signal_data)
        result.results.append(r)
        if not r.passed:
            result.passed = False
            result.rejected_rules.append(r.rule_name)
            result.violations[r.rule_name] = {
                "actual": r.actual_value,
                "threshold": r.threshold,
            }
            return result

        # Rule 2: Maximum Single Lot
        r = self._check_max_single_lot(signal_data)
        result.results.append(r)
        if not r.passed:
            result.passed = False
            result.rejected_rules.append(r.rule_name)
            result.violations[r.rule_name] = {
                "actual": r.actual_value,
                "threshold": r.threshold,
            }
            return result

        # Rule 3: Maximum Total Lot
        r = await self._check_max_total_lot(signal_data)
        result.results.append(r)
        if not r.passed:
            result.passed = False
            result.rejected_rules.append(r.rule_name)
            result.violations[r.rule_name] = {
                "actual": r.actual_value,
                "threshold": r.threshold,
            }
            return result

        # Rule 4: Maximum Open Positions
        r = await self._check_open_positions(signal_data)
        result.results.append(r)
        if not r.passed:
            result.passed = False
            result.rejected_rules.append(r.rule_name)
            result.violations[r.rule_name] = {
                "actual": r.actual_value,
                "threshold": r.threshold,
            }
            return result

        # Rule 5: Maximum Daily Loss
        r = await self._check_daily_loss(signal_data)
        result.results.append(r)
        if not r.passed:
            result.passed = False
            result.rejected_rules.append(r.rule_name)
            result.violations[r.rule_name] = {
                "actual": r.actual_value,
                "threshold": r.threshold,
            }
            return result

        # Rule 6: Minimum Margin
        r = await self._check_margin(signal_data)
        result.results.append(r)
        if not r.passed:
            result.passed = False
            result.rejected_rules.append(r.rule_name)
            result.violations[r.rule_name] = {
                "actual": r.actual_value,
                "threshold": r.threshold,
            }
            return result

        # Rule 7: Maximum Spread (if enabled)
        if self._spread_check_enabled:
            r = self._check_spread(signal_data)
            result.results.append(r)
            if not r.passed:
                result.passed = False
                result.rejected_rules.append(r.rule_name)
                result.violations[r.rule_name] = {
                    "actual": r.actual_value,
                    "threshold": r.threshold,
                }
                return result

        # Rule 8: Cooldown Period — real Redis-backed check
        r = await self._check_cooldown(signal_data)
        result.results.append(r)
        if not r.passed:
            result.passed = False
            result.rejected_rules.append(r.rule_name)
            result.violations[r.rule_name] = {
                "actual": r.actual_value,
                "threshold": r.threshold,
            }
            return result

        # ── 反向接刀护栏（2026-08-26）──
        # 仅当信号标记 reverse_order=True（momentum_flip 封 NO_TRADE 后覆写方向产出的
        # 高位动量反转接刀单）时触发。反向接刀逆原趋势、风险高，须已有同向持仓且已
        # 保本（利润垫）才放行 + 轻仓（×0.5）；否则拒绝（防盲目接刀）。
        # 复用与 _check_cooldown 同口径的 BE 真值源（Redis 标志 / PG positions），
        # DB 不可达 → fail-closed 拒绝（从严，防接刀）。
        _rro = _as_bool(signal_data.get("reverse_order", False))
        if _rro:
            r = await self._check_reverse_order(signal_data)
            result.results.append(r)
            if not r.passed:
                result.passed = False
                result.rejected_rules.append(r.rule_name)
                result.violations[r.rule_name] = {
                    "actual": r.actual_value,
                    "threshold": r.threshold,
                }
                return result

        # Rule 9: Spread Check Enabled flag (pass-through, already handled in #7)
        r = RuleResult(
            rule_name="risk_spread_check_enabled",
            passed=True,
            actual_value=float(self._spread_check_enabled),
            threshold=1.0,
            message="Spread check is enabled" if self._spread_check_enabled else "Spread check is disabled",
        )
        result.results.append(r)

        return result

    # ── Individual Rule Checks ──────────────────

    def _check_confidence(self, signal_data: dict) -> RuleResult:
        """Check signal confidence meets minimum threshold.

        Args:
            signal_data: Signal data dict.

        Returns:
            RuleResult for risk_min_confidence.
        """
        confidence = float(signal_data.get("confidence", 0) or 0.0)
        # 【2026-09-08 审计修复 P0】0–100 与 0–1 制混用 → 置信度闸恒过。
        # scheduler 下发的是 scorecard_total（**0–100 制**，实测 46.25/62/…），而
        # risk_min_confidence 是 0–1 制（0.10）。原实现直接比较：46.25 >= 0.10 恒真
        # → 该闸门从未拦过任何信号。归一到 0–1 后再比较（与 _apply_dynamic_lot
        # 中 `if score > 1.0: score /= 100` 同口径，避免两处阈值语义分裂）。
        conf_norm = confidence / 100.0 if confidence > 1.0 else confidence
        passed = conf_norm >= self._min_confidence
        return RuleResult(
            rule_name="risk_min_confidence",
            passed=passed,
            actual_value=conf_norm,
            threshold=self._min_confidence,
            message=f"Confidence {conf_norm:.4f} >= {self._min_confidence:.4f}" if passed
            else f"Confidence {conf_norm:.4f} < {self._min_confidence:.4f}",
        )

    def _check_max_single_lot(self, signal_data: dict) -> RuleResult:
        """Check single signal lot does not exceed maximum.

        Args:
            signal_data: Signal data dict.

        Returns:
            RuleResult for risk_max_lot_single.
        """
        lot = float(signal_data.get("lot", 0))
        passed = lot <= self._max_lot_single
        return RuleResult(
            rule_name="risk_max_lot_single",
            passed=passed,
            actual_value=lot,
            threshold=self._max_lot_single,
            message=f"Lot {lot} <= {self._max_lot_single}" if passed
            else f"Lot {lot} > {self._max_lot_single}",
        )

    async def _check_max_total_lot(self, signal_data: dict) -> RuleResult:
        """Check total lot (existing positions + new) does not exceed max.

        Args:
            signal_data: Signal data dict.

        Returns:
            RuleResult for risk_max_total_lot.
        """
        lot = float(signal_data.get("lot", 0))
        account_id = int(signal_data.get("account_id", 0))

        # Sum existing positions from DB if available
        existing_lot = 0.0
        if self._db is not None and self._db.is_initialized and account_id > 0:
            try:
                existing_lot = await self._get_account_total_lot(account_id)
            except Exception as exc:
                logger.warning("Failed to query existing total lot for account_id=%s: %s", account_id, exc)

        total_lot = existing_lot + lot
        passed = total_lot <= self._max_total_lot
        return RuleResult(
            rule_name="risk_max_total_lot",
            passed=passed,
            actual_value=total_lot,
            threshold=self._max_total_lot,
            message=f"Total lot {total_lot} (existing={existing_lot} + new={lot}) <= {self._max_total_lot}" if passed
            else f"Total lot {total_lot} (existing={existing_lot} + new={lot}) > {self._max_total_lot}",
        )

    async def _check_open_positions(self, signal_data: dict) -> RuleResult:
        """Check number of open positions does not exceed max.

        When score-driven positions is enabled, the max threshold is dynamically
        selected based on signal confidence/pre_score:
          - score <  score_tier_low  → max_positions_low  (conservative)
          - score >= score_tier_mid  → max_positions_high (aggressive)
          - otherwise                → max_positions_mid  (normal)

        Args:
            signal_data: Signal data dict.

        Returns:
            RuleResult for risk_max_open_positions.
        """
        account_id = int(signal_data.get("account_id", 0))
        symbol = signal_data.get("symbol", "")
        # 【2026-08-13 修复】按「同向实时持仓数」判定：仅统计与信号同方向的 open 持仓，
        # 反向持仓不再挤占额度。空方向/非 BUY·SELL 时退化为全方向计数（保持兼容）。
        direction = str(signal_data.get("direction", "") or "").upper()
        if direction not in ("BUY", "SELL"):
            direction = ""

        open_count = 0
        if self._db is not None and self._db.is_initialized and account_id > 0:
            try:
                open_count = await self._get_open_positions_count(account_id, symbol, direction)
            except Exception as exc:
                logger.warning("Failed to query open positions for account_id=%s: %s", account_id, exc)

        # ── 分值驱动动态最大持仓数（2026-08-11 v2.5）──
        threshold = self._max_open_positions  # fallback: fixed global cap
        threshold_source = "fixed"

        if self._score_driven_positions_enabled:
            score = float(signal_data.get("confidence", 0) or 0)
            # Also try pre_score if confidence is not set
            if score == 0:
                score = float(signal_data.get("pre_score", 0) or 0)

            if score >= self._score_tier_mid_positions:
                threshold = self._max_positions_high
                threshold_source = f"score_driven_high(score={score:.3f}>={self._score_tier_mid_positions})"
            elif score >= self._score_tier_low_positions:
                threshold = self._max_positions_mid
                threshold_source = f"score_driven_mid(score={score:.3f}>={self._score_tier_low_positions})"
            else:
                threshold = self._max_positions_low
                threshold_source = f"score_driven_low(score={score:.3f}<{self._score_tier_low_positions})"

        passed = open_count < threshold
        return RuleResult(
            rule_name="risk_max_open_positions",
            passed=passed,
            actual_value=float(open_count),
            threshold=float(threshold),
            message=(f"Open positions {open_count} < {threshold} [{threshold_source}]" if passed
                     else f"Open positions {open_count} >= {threshold} [{threshold_source}]"),
        )

    async def _check_daily_loss(self, signal_data: dict) -> RuleResult:
        """Check daily realized loss does not exceed max.

        Args:
            signal_data: Signal data dict.

        Returns:
            RuleResult for risk_max_daily_loss.
        """
        account_id = int(signal_data.get("account_id", 0))

        daily_loss = 0.0
        if self._db is not None and self._db.is_initialized and account_id > 0:
            try:
                daily_loss = await self._get_daily_loss(account_id)
            except Exception as exc:
                logger.warning("Failed to query daily loss for account_id=%s: %s", account_id, exc)

        # 【C 组 2026-08-03】daily_loss 语义：realized_pnl 负值=亏损。
        # 原 abs() 会把大额盈利日也当日亏拦截（盈利 +500 → |500|>200 → 误 REJECT）。
        # 修正：仅当亏损超过阈值才拦截，盈利日恒放行。
        passed = daily_loss >= -self._max_daily_loss
        return RuleResult(
            rule_name="risk_max_daily_loss",
            passed=passed,
            actual_value=daily_loss,
            threshold=self._max_daily_loss,
            message=f"Daily pnl {daily_loss:.2f} >= -{self._max_daily_loss}" if passed
            else f"Daily loss {daily_loss:.2f} < -{self._max_daily_loss}",
        )

    async def _check_margin(self, signal_data: dict) -> RuleResult:
        """Check free margin meets minimum requirement.

        Args:
            signal_data: Signal data dict.

        Returns:
            RuleResult for risk_min_margin.
        """
        account_id = int(signal_data.get("account_id", 0))

        free_margin = float("inf")
        if self._db is not None and self._db.is_initialized and account_id > 0:
            try:
                free_margin = await self._get_free_margin(account_id)
            except Exception as exc:
                logger.warning("Failed to query free margin for account_id=%s: %s", account_id, exc)

        passed = free_margin >= self._min_margin
        return RuleResult(
            rule_name="risk_min_margin",
            passed=passed,
            actual_value=free_margin,
            threshold=self._min_margin,
            message=f"Free margin {free_margin:.2f} >= {self._min_margin}" if passed
            else f"Free margin {free_margin:.2f} < {self._min_margin}",
        )

    def _check_spread(self, signal_data: dict) -> RuleResult:
        """Check current spread does not exceed max.

        Uses spread value from signal if available, otherwise passes.

        Args:
            signal_data: Signal data dict.

        Returns:
            RuleResult for risk_max_spread_pips.
        """
        spread = float(signal_data.get("spread", 0))
        if spread == 0:
            # No spread data in signal — pass
            return RuleResult(
                rule_name="risk_max_spread_pips",
                passed=True,
                actual_value=0.0,
                threshold=self._max_spread_pips,
                message="No spread data in signal — skipping",
            )

        passed = spread <= self._max_spread_pips
        return RuleResult(
            rule_name="risk_max_spread_pips",
            passed=passed,
            actual_value=spread,
            threshold=self._max_spread_pips,
            message=f"Spread {spread:.1f} pips <= {self._max_spread_pips:.1f}" if passed
            else f"Spread {spread:.1f} pips > {self._max_spread_pips:.1f}",
        )

    async def _check_cooldown(self, signal_data: dict) -> RuleResult:
        """同向保本闸门（纯 BE，2026-08-22 替代旧时间窗冷却）。

        需求（用户 2026-08-22）：取消 risk.cooldown_minutes 时间窗限制；改为
        毫秒级巡查【最新一笔同向 open 持仓】的 SL 价：
          - 未达保本止损价 → 禁止开新单（REJECT）；
          - 已达保本（BUY: sl >= entry - tol / SELL: sl <= entry + tol）→ 放行；
          依次阶梯加仓，直到 Rule 4（max_open_positions）封顶（本规则不替代限仓）。

        读取路径（毫秒级）：
          1) 快路径：Redis 保本标志 `hcm:pos:be:{account}:{dir}`（桥 trailing 抬 SL
             时写入，TTL 15s）。仅当标志 == "1"（达保本）时直接放行；
          2) 回退真值源：实时查 PG positions（每次信号毫秒级、不缓存），取最新一笔
             同向 open 持仓的 entry_price/sl_price 判定。

        失败行为（用户已拍板）：DB/Redis 源不可用 → fail-open 放行（记 CRITICAL）。
        sl_price IS NULL（未知）→ fail-open 放行（一致）。
        """
        account_id = int(signal_data.get("account_id", 0))
        symbol = signal_data.get("symbol", "")
        direction = str(signal_data.get("direction", "") or "").upper()

        # 无方向信号（NO_TRADE/HOLD）不闸
        if direction not in ("BUY", "SELL"):
            return RuleResult(
                rule_name="risk_cool_minutes",
                passed=True,
                actual_value=0.0,
                threshold=0.0,
                message=f"no direction ({direction or 'NONE'}) — skipped",
            )

        tol = float(getattr(self, "_be_tolerance", 0.0) or 0.0)

        # ── 2026-08-25 极值分层裁决：hexp 极值+保本追单候选（extreme_pending=True）──
        # hexp 检测到"极值区+动量回撤但该 symbol 已有同向保本持仓"时不再硬封，而是标记
        # extreme_pending 放行到此做最终裁决：本账户同向确实已保本 → 放行 + 轻仓追单(×0.5)；
        # 本账户未保本（symbol 级标志可能来自其他账户/残留）→ 拒绝，防接刀。
        # 【2026-08-25 加固】extreme_pending 分支必须与正常路径共用同一 BE 真值源：
        #   Redis 标志非"1"/缺失/Redis 异常时，回退查 PG positions（_account_be_from_db，
        #   与 575+ 同口径），不再直接拒单——修复"账户已保本但 BE 标志过期(TTL15s)/Redis
        #   抖动 → 极值追单被误拒（出信号不下单）"。仅当 PG 也确认未保本/无持仓/sl 未知/
        #   DB 不可达（无法确认保本）才拒绝（防接刀，从严 fail-closed）。
        _extreme_pending = _as_bool(signal_data.get("extreme_pending", False))
        if _extreme_pending and direction in ("BUY", "SELL"):
            _account_be = False
            _be_source = "redis"
            if self._redis is not None:
                try:
                    _af = await self._redis.get(f"hcm:pos:be:{account_id}:{direction}")
                    _account_be = (_af is not None and str(_af).strip() == "1")
                except Exception:
                    _account_be = False
            if not _account_be:
                # Redis 未确认 → 回退 PG 真值源（与正常路径一致），避免误拒合法极值追单
                _be_source = "pg"
                _db_be, _db_err = await self._account_be_from_db(account_id, direction, tol)
                if _db_err:
                    # DB 不可达：无法确认保本 → 从严拒绝（fail-closed，防接刀），记 CRITICAL
                    logger.critical(
                        "extreme_pending BE check DB FAILED (account=%s, dir=%s) — "
                        "reject (fail-closed, 防极值接刀)", account_id, direction,
                    )
                    return RuleResult(
                        rule_name="risk_cool_minutes",
                        passed=False,
                        actual_value=0.0,
                        threshold=0.0,
                        message="extreme_pending + BE 真值源(DB)不可达 → 拒绝(防极值接刀)",
                    )
                # 【2026-08-25 BUG 修复】无同向持仓(_db_be=None) → 放行，等价正常路径的
                # entry is None 语义。原 `bool(None)=False` 会把"账户根本无同向持仓"误判成
                # "有持仓但未保本"而拒绝，导致 A 级 SELL 信号在无持仓时被 risk_cool_minutes
                # 错误拦截（接刀护栏本意是拦"已有持仓未保本仍追单"，不拦首笔开仓）。
                if _db_be is None:
                    return RuleResult(
                        rule_name="risk_cool_minutes",
                        passed=True,
                        actual_value=0.0,
                        threshold=0.0,
                        message="extreme_pending + 无同向持仓 → 放行(首笔开仓,无接刀风险)",
                    )
                _account_be = bool(_db_be)
            if _account_be:
                # 账户同向已保本（Redis 或 PG 确认）→ 放行 + 轻仓（极值追单，suggested_lot_ratio×0.5）
                try:
                    _cur = float(signal_data.get("suggested_lot_ratio", 1.0) or 1.0)
                    signal_data["suggested_lot_ratio"] = round(_cur * 0.5, 4)
                except Exception:
                    signal_data["suggested_lot_ratio"] = 0.5
                return RuleResult(
                    rule_name="risk_cool_minutes",
                    passed=True,
                    actual_value=1.0,
                    threshold=0.0,
                    message=f"extreme_pending + 账户同向已保本({_be_source}) → 放行(轻仓×0.5 追单)",
                )
            else:
                return RuleResult(
                    rule_name="risk_cool_minutes",
                    passed=False,
                    actual_value=0.0,
                    threshold=0.0,
                    message="extreme_pending + 账户未保本(Redis/PG 均确认) → 拒绝(防极值接刀)",
                )

        # ── 快路径：Redis 保本标志（毫秒级；仅 "1" 视为已达保本，其余回退 DB）──
        if self._redis is not None:
            try:
                flag = await self._redis.get(f"hcm:pos:be:{account_id}:{direction}")
                if flag is not None and str(flag).strip() == "1":
                    return RuleResult(
                        rule_name="risk_cool_minutes",
                        passed=True,
                        actual_value=1.0,
                        threshold=0.0,
                        message="Redis BE flag=1 最新同向持仓已达保本，放行",
                    )
            except Exception as exc:
                logger.warning("Redis BE flag read failed (fallback to DB): %s", exc)

        # ── 回退真值源：实时查库（毫秒级，不缓存）──
        entry = None
        sl = None
        if self._db is not None and self._db.is_initialized and account_id > 0:
            try:
                # 【2026-08-28 P0-4 已回退】此处曾尝试 UNION hcm_trading.orders 的
                # 未平记录做双源校验，实测不成立并已回退，原因留档：
                # position_sync.py 平仓时是 **INSERT 一条 order_status=2 的新行**，
                # 而非 UPDATE 原开仓行 → orders 中 order_status=1 的开仓行 close_time
                # 永远为 NULL（累积 492 条僵尸记录）。故 `order_status=OPEN AND
                # close_time IS NULL` 返回的是"全部历史开仓"而非"当前未平"，
                # 直接 UNION 会把持仓数放大到 92~239 → 限仓闸门永久拒单。
                # 结论：当前未平持仓的唯一可靠真值源仍是 positions 表；
                # 若要真正满足铁律 10.2 的双源，第二源应取 MT5 实时持仓快照
                # （桥写入的 Redis 键），而非 orders。见待办 P0-4'。
                row = await self._db.fetchrow(
                    "SELECT open_price, sl FROM hcm_trading.positions "
                    "WHERE account_id=$1 AND direction=$2 "
                    "AND direction IN ('BUY','SELL') "
                    "AND status='open' "
                    "AND mt5_ticket IS NOT NULL AND mt5_ticket > 0 AND lot > 0 "
                    "AND open_price IS NOT NULL "
                    "ORDER BY open_time DESC LIMIT 1",
                    account_id, direction,
                )
                if row is not None:
                    entry = float(row["open_price"])
                    sl = row["sl"]
            except Exception as exc:
                # DB 故障 fail-open（已拍板），但必须可观测
                logger.critical(
                    "DB FAILED in _check_cooldown(BE gate, account=%s, dir=%s): %s "
                    "— fail-open, risk gate DEGRADED",
                    account_id, direction, exc,
                )
                return RuleResult(
                    rule_name="risk_cool_minutes",
                    passed=True,
                    actual_value=0.0,
                    threshold=0.0,
                    message="cooldown DB error — fail-open",
                )

        # 无同向 open 持仓（flat）→ 放行
        if entry is None:
            return RuleResult(
                rule_name="risk_cool_minutes",
                passed=True,
                actual_value=0.0,
                threshold=0.0,
                message="no same-direction open position — passed",
            )

        # sl 未知 → fail-open（已拍板；备选更安全：视为未达保本拦截）
        if sl is None:
            return RuleResult(
                rule_name="risk_cool_minutes",
                passed=True,
                actual_value=0.0,
                threshold=0.0,
                message="sl_price unknown — fail-open",
            )

        at_be = (sl >= entry - tol) if direction == "BUY" else (sl <= entry + tol)
        if at_be:
            return RuleResult(
                rule_name="risk_cool_minutes",
                passed=True,
                actual_value=round(float(sl), 2),
                threshold=round(float(entry), 2),
                message=f"最新同向持仓SL={sl:.2f} 已达保本(entry={entry:.2f})，放行",
            )
        return RuleResult(
            rule_name="risk_cool_minutes",
            passed=False,
            actual_value=round(float(sl), 2),
            threshold=round(float(entry), 2),
            message=f"最新同向持仓SL={sl:.2f} 未达保本(entry={entry:.2f})，禁止开新单",
        )


    async def _account_be_from_db(self, account_id: int, direction: str, tol: float):
        """查 PG positions 判定账户最新同向持仓是否已达保本（extreme_pending 回退真值源）。

        Returns:
            (at_be, err):
              at_be=True   → 有同向持仓且 SL 已达保本；
              at_be=False  → 有同向持仓但 SL 未达保本；
              at_be=None   → 无同向持仓 / sl 未知（无法确认保本）；
              err=True     → DB 查询异常（调用方应从严处理）。
        与 _check_cooldown 正常路径的 PG 查询同口径（同 SQL、同 tol 判定）。
        """
        if self._db is None or not self._db.is_initialized or account_id <= 0:
            return (None, False)
        try:
            row = await self._db.fetchrow(
                "SELECT open_price, sl FROM hcm_trading.positions "
                "WHERE account_id=$1 AND direction=$2 "
                "AND direction IN ('BUY','SELL') "
                "AND status='open' "
                "AND mt5_ticket IS NOT NULL AND mt5_ticket > 0 AND lot > 0 "
                "AND open_price IS NOT NULL "
                "ORDER BY open_time DESC LIMIT 1",
                account_id, direction,
            )
            if row is None:
                return (None, False)
            entry = float(row["open_price"])
            sl = row["sl"]
            if sl is None:
                return (None, False)
            at_be = (sl >= entry - tol) if direction == "BUY" else (sl <= entry + tol)
            return (at_be, False)
        except Exception as exc:
            logger.critical(
                "DB FAILED in _account_be_from_db(BE gate, account=%s, dir=%s): %s",
                account_id, direction, exc,
            )
            return (None, True)


    async def _check_reverse_order(self, signal_data: dict) -> "RuleResult":
        """反向接刀护栏（2026-08-26）。

        signal_data["reverse_order"]=True 表示本信号是 momentum_flip 封 NO_TRADE 后
        由 hexp reverse_candidate 覆写方向产出的「高位动量反转接刀单」（顶部 SELL /
        底部 BUY，逆原趋势）。

        接刀逆原趋势、风险高。分层放行（2026-08-26 起）：
          - 首单（无同向 open 持仓，flat）→ 豁免直接放行 + 最轻仓（×0.3）；
            反转起点第一笔裸接刀本就无利润垫，允许用户策略「首单直接下单」；
          - 次单（已有同向持仓）→ 走 BE 校验：
              · Redis hcm:pos:be:{account}:{dir}=1 或 PG 同向持仓 SL≥保本 → 放行 + 轻仓×0.5；
              · 有同向持仓但 SL 未达保本 → 拒绝（利润垫不足）；
          - DB 不可达 / 无法确认保本（Redis 标志异常分支）→ fail-closed 拒绝（从严）。
        与 _check_cooldown 的 extreme_pending 分支共用同一 BE 真值源范式
        （Redis 标志 + PG positions 回退），保证一致性。
        """
        direction = (signal_data.get("direction") or "NO_TRADE") if isinstance(signal_data, dict) else "NO_TRADE"
        account_id = int(signal_data.get("account_id", 0) or 0) if isinstance(signal_data, dict) else 0
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            account_id = 0

        if direction not in ("BUY", "SELL"):
            # 无明确方向 → 不接刀（防御）
            return RuleResult(
                rule_name="risk_reverse_order",
                passed=False,
                actual_value=0.0,
                threshold=0.0,
                message=f"reverse_order but direction={direction} — reject",
            )

        tol = float(getattr(self, "_be_tolerance", 0.0) or 0.0)

        # ── BE 真值源：Redis 标志优先，缺失/异常 → 回退 PG ──
        _account_be = False
        _be_source = "redis"
        if self._redis is not None:
            try:
                _af = await self._redis.get(f"hcm:pos:be:{account_id}:{direction}")
                _account_be = (_af is not None and str(_af).strip() == "1")
            except Exception:
                _account_be = False
        if not _account_be:
            _be_source = "pg"
            _db_be, _db_err = await self._account_be_from_db(account_id, direction, tol)
            if _db_err:
                # DB 不可达：无法确认保本 → 从严拒绝（fail-closed，防接刀）
                logger.critical(
                    "reverse_order BE check DB FAILED (account=%s, dir=%s) — "
                    "reject (fail-closed, 防接刀)", account_id, direction,
                )
                return RuleResult(
                    rule_name="risk_reverse_order",
                    passed=False,
                    actual_value=0.0,
                    threshold=0.0,
                    message="reverse_order + BE 真值源(DB)不可达 → 拒绝(防接刀)",
                )
            if _db_be is True:
                _account_be = True
            elif _db_be is False:
                # 有同向持仓但未保本 → 拒绝（利润垫不足）
                return RuleResult(
                    rule_name="risk_reverse_order",
                    passed=False,
                    actual_value=0.0,
                    threshold=0.0,
                    message=f"reverse_order: 同向持仓未达保本(利润垫不足) → 拒绝(防接刀)",
                )
            # _db_be is None → 无同向 open 持仓（flat）。
            # 【2026-08-26 修复】首单接刀豁免：反转起点第一笔裸接刀直接放行（轻仓×0.3），
            # 次单（已有同向持仓）仍走上方 BE 校验路径。与用户策略「首单直接下单、
            # 次单走风控」一致——首单是反转起点的第一笔，本就无利润垫，不应被拒绝。
            try:
                _sig_lot = float(signal_data.get("suggested_lot_ratio", 1.0) or 1.0)
            except (TypeError, ValueError):
                _sig_lot = 1.0
            signal_data["suggested_lot_ratio"] = _sig_lot * 0.3
            return RuleResult(
                rule_name="risk_reverse_order",
                passed=True,
                actual_value=0.0,  # 首单无利润垫，豁免
                threshold=1.0,
                message="reverse_order: 首单裸接刀(无同向持仓)豁免放行 + 轻仓×0.3",
            )

        # BE 已确认（次单已保本）→ 放行 + 轻仓接刀（×0.5）
        try:
            _sig_lot = float(signal_data.get("suggested_lot_ratio", 1.0) or 1.0)
        except (TypeError, ValueError):
            _sig_lot = 1.0
        signal_data["suggested_lot_ratio"] = _sig_lot * 0.5
        return RuleResult(
            rule_name="risk_reverse_order",
            passed=True,
            actual_value=1.0,  # 已保本
            threshold=1.0,
            message=f"reverse_order: 账户同向已保本(BE源={_be_source})，放行 + 轻仓×0.5",
        )


    # ── Database Helpers ────────────────────────

    async def _get_account_total_lot(self, account_id: int) -> float:
        """Get total lot size of all open positions for an account.

        Args:
            account_id: MT5 account ID.

        Returns:
            Total lot size.
        """
        if self._db is None:
            logger.critical(
                "DB NOT READY in _get_account_total_lot(account=%s) — fail-closed",
                account_id)
            return DB_FAIL_CLOSED_LOT
        try:
            # 【2026-08-28 P0-4 已回退】曾 UNION orders 未平记录做双源，实测不成立
            # （orders 开仓行 close_time 永为 NULL，原因见 _check_cooldown 处说明），
            # 会把在途手数放大数十倍 → 限仓永久拒单。维持 positions 单源。
            row = await self._db.fetchval(
                "SELECT COALESCE(SUM(lot), 0) FROM hcm_trading.positions "
                "WHERE account_id=$1 AND status='open'",
                account_id,
            )
            return float(row) if row else 0.0
        except Exception as exc:
            # 【P0-4】fail-open → fail-closed：DB 故障时拒绝交易（故障安全），
            # 而非返回 0.0 放开限仓。铁律 10.2。
            logger.critical(
                "DB FAILED in _get_account_total_lot(account=%s): %s — fail-closed, trading BLOCKED",
                account_id, exc)
            return DB_FAIL_CLOSED_LOT

    async def _get_open_positions_count(self, account_id: int, symbol: str, direction: str = "") -> int:
        """Get number of open positions for an account/symbol(/direction).

        Args:
            account_id: MT5 account ID.
            symbol: Trading symbol (empty = all).
            direction: Optional BUY/SELL filter (empty = both directions).

        Returns:
            Count of open positions.
        """
        if self._db is None:
            logger.critical(
                "DB NOT READY in _get_open_positions_count(account=%s) — fail-closed",
                account_id)
            return DB_FAIL_CLOSED_COUNT
        try:
            # Count only real MT5 positions — exclude phantom rows:
            # - NO_TRADE direction
            # - missing mt5_ticket (not synced from MT5)
            # - zero lot (signal records saved as positions)
            #
            # 【2026-08-28 P0-4 已回退】曾 UNION orders 未平记录做双源，实测不成立：
            # orders 的 order_status=1 开仓行 close_time 永远为 NULL（平仓是 INSERT
            # 新行而非 UPDATE），UNION 后持仓数虚高至 92~239（阈值仅 5）→ 永久拒单。
            # 维持 positions 单源，详见 _check_cooldown 处的完整说明与待办 P0-4'。
            where_real = (
                "status='open' AND direction IN ('BUY','SELL') "
                "AND mt5_ticket IS NOT NULL AND mt5_ticket > 0 "
                "AND lot > 0"
            )
            sql = f"SELECT COUNT(*) FROM hcm_trading.positions WHERE account_id=$1 AND {where_real}"
            args: list = [account_id]
            if symbol:
                args.append(symbol)
                sql += f" AND symbol=${len(args)}"
            if direction:
                args.append(direction.upper())
                sql += f" AND direction=${len(args)}"
            row = await self._db.fetchval(sql, *args)
            return int(row) if row else 0
        except Exception as exc:
            # 【P0-4】fail-open → fail-closed：DB 故障时限仓闸门必须拒绝交易，
            # 而非返回 0 放开限仓（原实现 DB 抖动即可打满仓位）。铁律 10.2。
            logger.critical(
                "DB FAILED in _get_open_positions_count(account=%s, symbol=%s, dir=%s): %s — fail-closed, trading BLOCKED",
                account_id, symbol, direction, exc)
            return DB_FAIL_CLOSED_COUNT

    async def _get_daily_loss(self, account_id: int) -> float:
        """Get realized daily loss for an account.

        Args:
            account_id: MT5 account ID.

        Returns:
            Realized loss for today (negative = loss).
        """
        if self._db is None:
            logger.critical(
                "DB NOT READY in _get_daily_loss(account=%s) — fail-closed",
                account_id)
            return DB_FAIL_CLOSED_LOSS
        try:
            # 【2026-08-28 P0-4/P0-6】真值源修正 —— 原实现只查
            # hcm_trade.closed_positions，而该表实测为**空表（0 行）**
            # （对比 hcm_trading.orders order_status=CLOSED 有 1157 行，最新 08-28）
            # → 当日亏损恒为 0 → **每日亏损熔断从未生效**。
            # 改为以 orders 已平记录为主真值源；closed_positions 作为归档补充保留
            # （当前为空，UNION ALL 不产生重复计数；若将来启用归档写入，须复核
            # 两表是否会 double count 同一笔平仓）。
            row = await self._db.fetchval(
                "SELECT COALESCE(SUM(pnl), 0) FROM ( "
                "  SELECT profit AS pnl FROM hcm_trading.orders "
                "  WHERE account_id=$1 AND order_status=$2 AND close_time IS NOT NULL "
                "    AND close_time >= CURRENT_DATE "
                "  UNION ALL "
                "  SELECT realized_pnl AS pnl FROM hcm_trade.closed_positions "
                "  WHERE account_id=$1 AND close_time >= CURRENT_DATE "
                ") t",
                account_id, ORDER_STATUS_CLOSED,
            )
            return float(row) if row else 0.0
        except Exception as exc:
            # 【P0-4】fail-open → fail-closed：DB 故障时必须触发熔断（拒绝交易），
            # 而非返回 0.0 让亏损闸门静默失效。铁律 10.2。
            logger.critical(
                "DB FAILED in _get_daily_loss(account=%s): %s — fail-closed, trading BLOCKED",
                account_id, exc)
            return DB_FAIL_CLOSED_LOSS

    async def _get_free_margin(self, account_id: int) -> float:
        """Get free margin for an account.

        Args:
            account_id: MT5 account ID.

        Returns:
            Free margin value.
        """
        if self._db is None:
            return float("inf")
        try:
            row = await self._db.fetchval(
                "SELECT COALESCE(free_margin, 0) FROM hcm_trade.account_snapshots "
                "WHERE account_id=$1 ORDER BY created_at DESC LIMIT 1",
                account_id,
            )
            # 【C 组 2026-08-03】区分"无快照"(None→放行) 与"保证金为 0"(必拦)：
            # 原 `if row` 把 0.0 当假值返回 inf，爆仓账户保证金闸必过。
            return float("inf") if row is None else float(row)
        except Exception as exc:
            # 【D 组】DB 故障 fail-open 必须可观测
            logger.critical(
                "DB FAILED in _get_free_margin(account=%s): %s — fail-open inf, risk gate DEGRADED",
                account_id, exc)
            return float("inf")

    # ── Config Loading ──────────────────────────

    async def load_config(self) -> None:
        """Load rule thresholds from ConfigProviderV3.

        Resilience (2026-07-23): this method NO LONGER raises on failure.
        - If thresholds were loaded successfully before, keep the last-good
          values and log a WARNING — the engine keeps running with stale
          thresholds instead of crashing and stalling the whole pipeline.
        - If thresholds were NEVER loaded (first startup, config missing),
          leave self._config_loaded=False so evaluate() FAILS CLOSED
          (rejects signals). A blind risk gate must not trade unconstrained
          (the 7-20 explosive order-opening incident). The signal is still
          consumed/acked downstream (no signal:stream backup). A CRITICAL log
          alerts operators.
        """
        if self._config is None:
            logger.critical(
                "RuleChain: ConfigProviderV3 unavailable — DEGRADED, "
                "evaluate() will PASS all signals (no risk control)"
            )
            self._config_loaded = False
            return

        try:
            new_min_conf = await self._config.get_float("risk_min_confidence")
            new_max_lot = await self._config.get_float("risk.max_lot_per_trade")
            new_total = await self._config.get_float("risk.max_total_exposure")
            # [2026-07-24 修复] 默认改 10（原 get_int 默认 0 → 缺失即禁用检查 → 7-20 爆炸式开单）。
            # 即使 PG/Redis 中 risk.max_concurrent_signals 缺失，也维持最多 10 单上限，
            # 防止“配置未 seed / Redis 重启丢失 → 最大单数被完全关闭”导致参数控制失效。
            new_max_pos = await self._config.get_int("risk.max_concurrent_signals", 10)
            new_daily = await self._config.get_float("risk.max_daily_loss")
            new_margin = await self._config.get_float("risk.margin_call_level")
            new_spread = await self._config.get_float("risk_spread_max_multiplier", 999.0)
            new_cool = await self._config.get_int("risk.cooldown_minutes", 5)
            # [2026-08-22] 同向保本闸门容差（价格单位，默认 0）
            new_be_tol = await self._config.get_float("risk.cool_be_tolerance", 0.0)
            try:
                spread_mult = await self._config.get_float("risk_spread_max_multiplier", 0)
                new_spread_enabled = spread_mult > 0
            except Exception:
                new_spread_enabled = False

            # ── 分值驱动最大持仓数（2026-08-11 v2.5）──
            new_score_pos_enabled = await self._config.get_bool("risk.score_driven_positions_enabled", False)
            new_score_tier_low = await self._config.get_float("risk.score_tier_low_positions", 0.50)
            new_score_tier_mid = await self._config.get_float("risk.score_tier_mid_positions", 0.70)
            new_max_pos_low = await self._config.get_int("risk.max_positions_score_low", 1)
            new_max_pos_mid = await self._config.get_int("risk.max_positions_score_mid", 2)
            new_max_pos_high = await self._config.get_int("risk.max_positions_score_high", 3)

            # All reads succeeded → commit atomically.
            self._min_confidence = new_min_conf
            self._max_lot_single = new_max_lot
            self._max_total_lot = new_total
            self._max_open_positions = new_max_pos
            self._max_daily_loss = new_daily
            self._min_margin = new_margin
            self._max_spread_pips = new_spread
            self._cool_minutes = new_cool
            self._be_tolerance = new_be_tol
            self._spread_check_enabled = new_spread_enabled
            self._score_driven_positions_enabled = new_score_pos_enabled
            self._score_tier_low_positions = new_score_tier_low
            self._score_tier_mid_positions = new_score_tier_mid
            self._max_positions_low = new_max_pos_low
            self._max_positions_mid = new_max_pos_mid
            self._max_positions_high = new_max_pos_high
            self._config_loaded = True
            logger.info(
                "RuleChain config loaded: confidence=%.2f, single_lot=%.2f, "
                "total_exposure=%.2f, max_pos=%d, daily_loss=%.2f, min_margin=%.2f, "
                "max_spread=%.1f, cool_min=%d, be_tol=%.2f, spread_check=%s, "
                "score_pos_enabled=%s tier_low=%.2f tier_mid=%.2f pos_low=%d pos_mid=%d pos_high=%d",
                self._min_confidence, self._max_lot_single, self._max_total_lot,
                self._max_open_positions, self._max_daily_loss, self._min_margin,
                self._max_spread_pips, self._cool_minutes, self._be_tolerance,
                self._spread_check_enabled,
                self._score_driven_positions_enabled, self._score_tier_low_positions,
                self._score_tier_mid_positions, self._max_positions_low,
                self._max_positions_mid, self._max_positions_high,
            )
        except Exception as exc:
            if self._config_loaded:
                # Keep last-good thresholds; engine keeps running.
                logger.warning(
                    "RuleChain config hot-reload failed (keeping last-good thresholds): %s",
                    exc,
                )
            else:
                # Never loaded — fail closed (reject), but scream.
                logger.critical(
                    "RuleChain config load failed and never loaded before: %s — "
                    "FAIL-CLOSED, evaluate() will REJECT all signals (no risk control)",
                    exc,
                )
                self._config_loaded = False
