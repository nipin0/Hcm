"""Decision Engine — PASS / REJECT / DEGRADE output for risk checks.

Produces a three-way decision based on the rule chain result:
- PASS: All rules passed, signal safe to forward.
- REJECT: One or more hard rules failed, signal blocked.
- DEGRADE: Signal passed but with warnings — lot reduced or tagged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from risk_engine.rule_chain import RuleChainResult

logger = logging.getLogger(__name__)

# ── Decision Constants ─────────────────────────

DECISION_PASS = "PASS"
DECISION_REJECT = "REJECT"
DECISION_DEGRADE = "DEGRADE"

# Rules that are considered "soft" — failure → DEGRADE instead of REJECT
# [2026-07-24 修复] risk_cool_minutes（同向开仓冷却）已移出软规则，改为硬规则：
# 冷却是时间闸门，降级放行会让"冷却期内仍开仓→同向连开"，故冷却失败必须 REJECT 阻断。
# 仅点差(risk_max_spread_pips)保留为软规则（点差偏大时降级缩手数，而非完全阻断）。
SOFT_RULES = {
    "risk_max_spread_pips",
}

# Rules that trigger DEGRADE when value is within a warning band
DEGRADE_WARN_RATIO = 0.80  # If actual >= 80% of threshold, consider DEGRADE


@dataclass
class DecisionResult:
    """Decision output from the risk engine."""
    decision: str = DECISION_PASS
    rejected_rules: list[str] = field(default_factory=list)
    degrade_reasons: list[str] = field(default_factory=list)
    adjusted_lot: Optional[float] = None
    original_lot: float = 0.0


class DecisionEngine:
    """Makes PASS/REJECT/DEGRADE decisions based on rule chain results.

    Decision logic:
    - All rules passed → PASS
    - Any hard rule failed → REJECT
    - Only soft rules failed → DEGRADE (signal forwarded with warnings)
    - Near-threshold (>=80% of limit) → DEGRADE (precautionary)

    Example:
        engine = DecisionEngine(config_provider)
        result = await engine.decide(signal_data, rule_chain_result)
        if result.decision == "PASS":
            print("Signal approved")
    """

    def __init__(self, config_provider: Any = None):
        """Initialize DecisionEngine.

        Args:
            config_provider: ConfigProviderV3 instance for configurable thresholds.
        """
        self._config = config_provider

    # ── Main API ────────────────────────────────

    async def decide(
        self,
        signal_data: dict,
        rule_result: RuleChainResult,
    ) -> str:
        """Decide the fate of a signal based on rule chain results.

        Args:
            signal_data: Original signal data dict.
            rule_result: RuleChainResult from rule chain evaluation.

        Returns:
            Decision string: PASS, REJECT, or DEGRADE.
        """
        # If all passed — PASS
        if rule_result.passed:
            # Check for near-threshold warnings → DEGRADE
            near_threshold = self._check_near_threshold(rule_result)
            if near_threshold:
                logger.info(
                    "Signal near threshold: signal_id=%s, reasons=%s",
                    signal_data.get("signal_id"), near_threshold,
                )
                return DECISION_DEGRADE
            return DECISION_PASS

        # Separate hard vs soft failures
        hard_failures = []
        soft_failures = []
        for rule_name in rule_result.rejected_rules:
            if rule_name in SOFT_RULES:
                soft_failures.append(rule_name)
            else:
                hard_failures.append(rule_name)

        # Any hard failure → REJECT
        if hard_failures:
            logger.info(
                "Signal REJECTED: signal_id=%s, hard_rules=%s",
                signal_data.get("signal_id"), hard_failures,
            )
            return DECISION_REJECT

        # Only soft failures → DEGRADE
        if soft_failures:
            logger.info(
                "Signal DEGRADED: signal_id=%s, soft_rules=%s",
                signal_data.get("signal_id"), soft_failures,
            )
            return DECISION_DEGRADE

        # Fallback
        return DECISION_PASS

    # ── Near-Threshold Detection ────────────────

    def _check_near_threshold(self, rule_result: RuleChainResult) -> list[str]:
        """Check if any passed rules are near their threshold.

        If a rule passed but the actual value is >= 80% of the threshold,
        it's flagged as near-threshold for precautionary DEGRADE.

        Args:
            rule_result: RuleChainResult with all passed rules.

        Returns:
            List of rule names that are near their threshold.
        """
        near_rules = []
        for r in rule_result.results:
            if not r.passed:
                continue
            if r.threshold == 0:
                continue

            # For rules where lower is better (confidence/margin): pass means actual >= threshold
            # The ratio rules only apply for upper-bound checks (lot/spread etc.)
            upper_bound_rules = {
                "risk_max_lot_single",
                "risk_max_total_lot",
                "risk_max_open_positions",
                "risk_max_spread_pips",
            }

            # 【2026-09-08 审计修复 P1】risk_max_daily_loss 不属于 upper-bound 语义：
            # rule_chain 里 actual_value = 当日 realized_pnl（**负=亏损、正=盈利**），
            # threshold = 允许亏损额(正数)。原实现 `actual>0 → ratio=actual/threshold`
            # 恰好判反：盈利 +500/200=2.5 → 误判"接近熔断"触发 DEGRADE；
            # 而真正亏损(actual<0)被 `actual>0` 过滤掉 → 接近亏损上限反而不预警。
            # 改为按"亏损额占阈值比例"判定，盈利日不参与。
            if r.rule_name == "risk_max_daily_loss":
                _loss = -min(float(r.actual_value or 0.0), 0.0)  # 亏损取正、盈利归 0
                if _loss > 0 and r.threshold > 0:
                    _ratio = _loss / float(r.threshold)
                    if _ratio >= DEGRADE_WARN_RATIO:
                        near_rules.append(
                            f"{r.rule_name}: 亏损 {_loss:.2f}/{r.threshold} ({_ratio:.0%})"
                        )
                continue

            if r.rule_name in upper_bound_rules and r.actual_value > 0:
                ratio = r.actual_value / r.threshold
                if ratio >= DEGRADE_WARN_RATIO:
                    near_rules.append(
                        f"{r.rule_name}: {r.actual_value}/{r.threshold} ({ratio:.0%})"
                    )

        return near_rules

    # ── Lot Adjustment ──────────────────────────

    def compute_adjusted_lot(
        self,
        original_lot: float,
        max_single_lot: float,
        max_total_lot: float,
        current_total_lot: float,
    ) -> float:
        """Compute an adjusted lot size when DEGRADE is applied.

        Clamps the lot to fit within remaining total lot capacity.

        Args:
            original_lot: Signal's original lot size.
            max_single_lot: Maximum per-signal lot.
            max_total_lot: Maximum total lot across all positions.
            current_total_lot: Current total lot in use.

        Returns:
            Adjusted lot size (clamped).
        """
        remaining = max(0.0, max_total_lot - current_total_lot)
        adjusted = min(original_lot, max_single_lot, remaining)
        adjusted = max(0.01, adjusted)  # Minimum lot floor
        return round(adjusted, 2)

    # ── Config ──────────────────────────────────

    async def load_config(self) -> None:
        """Load decision engine parameters from ConfigProviderV3."""
        if self._config is None:
            return
        try:
            # Load any additional decision parameters
            logger.info("DecisionEngine config loaded")
        except Exception as exc:
            logger.warning("DecisionEngine config load failed: %s", exc)
