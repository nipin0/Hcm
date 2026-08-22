"""共源信号增强引擎（Co-Source Signal Enhancer）.

PRD: ``hcm-co-source-model-prd.md`` (v1.1) — Phase 1a 实施核心模块。

设计原则（三条硬约束，用户最高优先级）：
  ① **禁用硬编码**：所有阈值 / 因子 / 门槛均经 ``ConfigProviderV3`` 读取，
     代码中仅允许 ``get_*()`` 的 *default 回退* 出现字面量；绝不在逻辑分支里
     写死数值。PRD 表未给出但逻辑必需的键（见下文 ``_EXT_*``）一律补为配置键。
  ② **PG + Redis 双写**：本模块 **只读**。配置由 ``ConfigProviderV3`` 统一 SoT
     （``set()`` 已是 PG→Redis 双写），共源引擎不写任何配置。
  ③ **向后兼容 / fallback**：``signal.active_model != "co_source"`` 时
     ``apply()`` **原样返回** 传入的 ``ScoreResult``，默认评分引擎字节级不变。

Phase 1a 范围（**无需标注数据即可验证**）：
  - 5 态校准因子（冷启动期恒 1.0；P1b 写入非 1.0 值后才生效）
  - F1–F5 假信号过滤（全本地、毫秒级、零网络）
  - 自适应入市门槛（强趋势 / 弱趋势 / 震荡拦截 / 突发波动 × 风险等级 low/med/high）

尺度归一说明（关键）：
  引擎 ``ScoreResult.pre_score`` 为 **0–1 尺度**（``min_score_threshold=0.15``，
  体制门槛上限 ~0.78）。PRD §4.5.3 的门槛/扣分值为 **0–100 尺度**（65/70/80/20/25）。
  全部经 ``co.gate.score_scale``（默认 100）归一：``effective = value / score_scale``。
  这样既忠于 PRD 字面配置值，又不会破坏 0–1 引擎尺度（满足约束 ①，无硬编码 /100）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from signal_tower.regime_classifier import Regime, RegimeResult
from signal_tower.indicator_calculator import IndicatorResults
from signal_tower.h1_regime_classifier import H1Context
from signal_tower.scoring_engine import ScoreResult
from signal_tower.micro_state import MicroState, MicroStateResult

logger = logging.getLogger(__name__)

# ── 激活开关 ──
ACTIVE_MODEL_KEY = "signal.active_model"
CO_SOURCE_MODEL = "co_source"

# ── G1 校准因子键（5 态 → 配置键）──
_CALIB_KEYS = {
    Regime.PRE_TREND: "co.calib.pre_trend",
    Regime.TREND: "co.calib.trend",
    Regime.TREND_FADE: "co.calib.trend_fade",
    Regime.RANGE: "co.calib.range",
    Regime.NEUTRAL: "co.calib.neutral",
}
_CALIB_DEFAULT = 1.0
_CALIB_MIN_DAYS_DEFAULT = 7

# ── G3 自适应门槛键（强/弱/突发 各有 4 字段）──
_GATE_KEYS = {
    "strong": {
        "trend": "co.gate.strong.trend",
        "lot": "co.gate.strong.lot",
        "sl_atr": "co.gate.strong.sl_atr",
        "rr_min": "co.gate.strong.rr_min",
    },
    "weak": {
        "trend": "co.gate.weak.trend",
        "lot": "co.gate.weak.lot",
        "sl_atr": "co.gate.weak.sl_atr",
        "rr_min": "co.gate.weak.rr_min",
    },
    "shock": {
        "trend": "co.gate.shock.trend",
        "lot": "co.gate.shock.lot",
        "sl_atr": "co.gate.shock.sl_atr",
        "rr_min": "co.gate.shock.rr_min",
    },
}

# NEUTRAL 体制专属评分门槛的兜底值（0-1 尺度）。
# 仅当配置键 scoring.neutral_min_score_threshold 在 PG/Redis 均缺失时才启用，且会打 CRITICAL 告警，
# 不再作为静默默认值——配置必须是唯一真源。注意：不存在 co.gate.neutral.trend 这类键（旧审计误引）。
_NEUTRAL_MIN_SCORE_FALLBACK = 0.45


class CoSourceEngine:
    """共源信号增强引擎（Phase 1a：过滤 + 校准 + 自适应门槛）。

    用法（由 ``scheduler._produce_signal`` 调用）：::

        score_result = self._scoring_engine.compute_pre_score(...)
        score_result = await self._co_source.apply(
            score_result, indicators, regime_result,
            h1_context=h1_context,
            risk_level=risk_level,          # 来自 Redis hcm:risk:daily_level
            event_window=event_window,      # 来自 Redis hcm:risk:event_window
            consecutive_losses=consec,      # 来自 Redis hcm:risk:consecutive_loss
        )
        # 之后继续 threshold_passed 判定（apply 已重算 threshold_passed）
    """

    def __init__(self, config_provider: Any = None) -> None:
        self._config = config_provider
        # ── 校准因子（G1）──
        self._calib: dict[Regime, float] = {r: _CALIB_DEFAULT for r in _CALIB_KEYS}
        self._calib_min_days: int = _CALIB_MIN_DAYS_DEFAULT
        # ── 自适应门槛（G3）──
        self._score_scale: float = 100.0          # 0–100 → 0–1 归一
        self._adx_strong: float = 0.0             # 由 load_config 从 co.gate.adx_strong 读取（无硬编码默认值）
        self._with_trend_trend: float = 30.0      # 顺 H1 方向放宽门槛(0-100)，由 co.gate.with_trend.trend 热读(B 项)
        self._shock_atr_mult: float = 2.0         # 突发波动：vol_factor ≥ 此值
        self._range_block: bool = True            # 震荡/中性市拦截
        # NEUTRAL 体制专属评分门槛（真实键名 scoring.neutral_min_score_threshold，0-1 尺度）。
        # 此初值仅作为 load_config 前的占位；加载后会由配置覆盖（缺失则 CRITICAL 告警后取兜底）。
        self._neutral_min_score: float = _NEUTRAL_MIN_SCORE_FALLBACK
        # 重构方案 Phase1: NEUTRAL RSI 均值回归专属门槛（不再裸奔 threshold=0）。
        # 仅当 neutral_rsi_confirmed 时由 _apply_adaptive_gate 使用；默认 0.15 过滤纯噪声。
        self._neutral_rsi_min_score: float = 0.15
        self._gate: dict[str, dict[str, float]] = {
            b: {"trend": 65.0, "lot": 1.0, "sl_atr": 0.5, "rr_min": 2.0}
            for b in ("strong", "weak", "shock")
        }
        self._risk_high_offset: float = 10.0
        self._risk_med_offset: float = 5.0
        # ── F1–F5 假信号过滤（G2）──
        self._f1_enabled: bool = True
        self._f1_penalty: float = 20.0
        self._f2_enabled: bool = True
        self._f2_ratio: float = 50.0              # 当前带宽 / 近20根均值 < 此% → 收口
        self._f3_enabled: bool = True
        self._f3_minutes: int = 30
        self._f3_penalty: float = 25.0
        self._f4_enabled: bool = True
        self._f4_rsi_upper: float = 70.0
        self._f4_rsi_lower: float = 30.0
        self._f5_enabled: bool = True
        self._f5_consecutive: int = 3
        self._f5_penalty: float = 30.0
        # [2026-07-30 C 组] F6 棒质量 / 点差质量闸门（灰度开关，默认关闭）
        self._f6_enabled: bool = False
        self._f6_quality_min: float = 0.55
        self._f6_spread_q_max: float = 2.0
        # [重构方案 Phase2 v2] 去除硬编码：v2 折扣与执行参数全部配置驱动
        self._v2_enabled: bool = False
        self._v2_f2_discount: float = 0.15
        self._v2_f4_discount: float = 0.15
        self._v2_exec_lot_mult: float = 1.0
        self._v2_exec_sl_atr_mult: float = 2.0
        self._v2_exec_rr_min: float = 1.2
        # [2026-08-05] 回踩保护：行情回调（TREND_PULLBACK）时停止下顺势趋势单。
        # 默认开启；co.v2.pullback_block_depth_atr=0 表示任何回踩都拦，
        # 调大该值可仅在"深度回调"才拦（浅回踩仍作为买点保留）。
        self._v2_pullback_block_enabled: bool = True
        self._v2_pullback_block_depth_atr: float = 0.0
        # [2026-08-10] 盲点修复：M5 无方向信念（RANGE/低波动，引擎因 range.block 判 NO_TRADE）
        # 但 H1 已确认方向时，用 H1 方向兜底，根治"明显趋势却不下单"。开关 + 强度门槛 + 降仓系数。
        self._v2_h1_fb_enabled: bool = True
        self._v2_h1_fb_min_strength: float = 0.50
        self._v2_h1_fb_lot_mult: float = 0.6

    # ── 配置加载（热重载时调用）───────────────
    async def load_config(self) -> None:
        """从 ConfigProviderV3 加载全部共源参数（约束 ①：零硬编码读取）。"""
        if self._config is None:
            logger.warning("CoSourceEngine: no config_provider, using built-in defaults")
            return
        try:
            # G1 校准因子
            for regime, key in _CALIB_KEYS.items():
                self._calib[regime] = await self._config.get_float(key, _CALIB_DEFAULT)
            self._calib_min_days = await self._config.get_int(
                "co.calib.min_days", _CALIB_MIN_DAYS_DEFAULT
            )
            # G3 自适应门槛
            self._score_scale = await self._config.get_float("co.gate.score_scale", 100.0)
            # [2026-07-24 审计加固] 原无代码兜底：配置缺失时 adx_strong=0.0 → 强趋势门槛失效，
            # 一切行情都判 strong 带 → 过度放大手数/放松拦截。
            # [2026-07-31 C 项·ADX 收敛] M5 侧"强趋势 ADX"统一为 18（与 scoring.trend_strong_adx_threshold
            # 反向阻断、scoring.min_adx_for_trade 地板一致）；H1 慢线专属 regime.trend_strong_adx_threshold=28
            # 为另一概念（时间框架更长、需更高确认），不并入此值。
            self._adx_strong = await self._config.get_float("co.gate.adx_strong", 18.0)
            self._shock_atr_mult = await self._config.get_float("co.gate.shock.atr_mult", 2.0)
            self._range_block = await self._config.get_bool("co.gate.range.block", False)  # [2026-08-01] 对齐部署
            # NEUTRAL 体制专属评分门槛（co_source 自适应闸门 range/neutral 带使用）。
            # 【2026-08-01 配置清理】真实键名 = scoring.neutral_min_score_threshold（0-1 尺度，不经 score_scale 归一）。
            # 不存在 co.gate.neutral.trend 这种键（旧审计误引、引擎从不读取）。
            # 禁用静默硬编码：配置缺失时显式 CRITICAL 告警后再兜底，确保配置为唯一真源。
            _neutral_cfg = await self._config.get("scoring.neutral_min_score_threshold")
            if _neutral_cfg is None:
                logger.critical(
                    "CoSourceEngine: scoring.neutral_min_score_threshold NOT seeded in config "
                    "(PG/Redis) — NEUTRAL gate falls back to hardcoded %.2f. Seed it explicitly!",
                    _NEUTRAL_MIN_SCORE_FALLBACK,
                )
                self._neutral_min_score = _NEUTRAL_MIN_SCORE_FALLBACK
            else:
                try:
                    self._neutral_min_score = float(_neutral_cfg)
                except (ValueError, TypeError):
                    logger.critical(
                        "CoSourceEngine: scoring.neutral_min_score_threshold=%r not float — "
                        "falling back to %.2f",
                        _neutral_cfg, _NEUTRAL_MIN_SCORE_FALLBACK,
                    )
                    self._neutral_min_score = _NEUTRAL_MIN_SCORE_FALLBACK
            # 重构方案 Phase1: NEUTRAL RSI 均值回归专属门槛（不再裸奔 threshold=0）。
            self._neutral_rsi_min_score = await self._config.get_float(
                "co.neutral_rsi_min_score", 0.15
            )
            # [2026-07-31 B 项] 顺 H1 方向（高空/低多）放宽评分门槛：方向等于 h1_bias 时
            # 用此门槛（默认 30→0.30）覆盖 M5 体制的 0.40/0.50/0.45，使"顺 H1 高空"真正能出单。
            self._with_trend_trend = await self._config.get_float(
                "co.gate.with_trend.trend", 30.0
            )
            for band, keys in _GATE_KEYS.items():
                # [2026-08-01] 趋势带门槛回退默认对齐部署（strong=40/weak=50/shock=20 由各自键覆盖；
                # 缺失时取 40.0 作中枢，避免回退到旧 65.0 误拦）。
                self._gate[band] = {
                    "trend": await self._config.get_float(keys["trend"], 40.0),
                    "lot": await self._config.get_float(keys["lot"], 1.0),
                    "sl_atr": await self._config.get_float(keys["sl_atr"], 0.5),
                    "rr_min": await self._config.get_float(keys["rr_min"], 2.0),
                }
            self._risk_high_offset = await self._config.get_float(
                "co.gate.risk.high_offset", 5.0
            )
            self._risk_med_offset = await self._config.get_float(
                "co.gate.risk.med_offset", 2.0
            )
            # G2 假信号过滤
            self._f1_enabled = await self._config.get_bool("co.filter.f1_enabled", True)
            self._f1_penalty = await self._config.get_float("co.filter.f1_penalty", 20.0)
            self._f2_enabled = await self._config.get_bool("co.filter.f2_enabled", True)  # 对齐设计默认（Phase 1a / __init__ / 前端 suggested）
            self._f2_ratio = await self._config.get_float("co.filter.f2_ratio", 50.0)
            self._f3_enabled = await self._config.get_bool("co.filter.f3_enabled", True)
            self._f3_minutes = await self._config.get_int("co.filter.f3_minutes", 30)
            self._f3_penalty = await self._config.get_float("co.filter.f3_penalty", 25.0)
            self._f4_enabled = await self._config.get_bool("co.filter.f4_enabled", True)  # 对齐设计默认（Phase 1a / __init__ / 前端 suggested）
            self._f4_rsi_upper = await self._config.get_float("co.filter.f4_rsi_upper", 70.0)
            self._f4_rsi_lower = await self._config.get_float("co.filter.f4_rsi_lower", 30.0)
            self._f5_enabled = await self._config.get_bool("co.filter.f5_enabled", True)  # 对齐设计默认（Phase 1a / __init__ / 前端 suggested）
            self._f5_consecutive = await self._config.get_int("co.filter.f5_consecutive", 3)
            self._f5_penalty = await self._config.get_float("co.filter.f5_penalty", 30.0)
            # [2026-07-30 C 组] F6 棒质量 / 点差质量闸门（灰度开关）
            self._f6_enabled = await self._config.get_bool("co.filter.f6_quality_enabled", False)
            self._f6_quality_min = await self._config.get_float("co.filter.f6_quality_min", 0.55)
            self._f6_spread_q_max = await self._config.get_float("co.filter.f6_spread_q_max", 2.0)
            # [重构方案 Phase2 v2] 去除硬编码：v2 折扣与执行参数配置驱动（零硬编码读取）
            self._v2_enabled = await self._config.get_bool("co.v2_enabled", False)
            self._v2_f2_discount = await self._config.get_float("co.v2.filter.f2_discount", 0.15)
            self._v2_f4_discount = await self._config.get_float("co.v2.filter.f4_discount", 0.15)
            self._v2_exec_lot_mult = await self._config.get_float("co.v2.exec.lot_mult", 1.0)
            self._v2_exec_sl_atr_mult = await self._config.get_float("co.v2.exec.sl_atr_mult", 2.0)
            self._v2_exec_rr_min = await self._config.get_float("co.v2.exec.rr_min", 1.2)
            # [2026-08-05] 回踩保护：回调（TREND_PULLBACK）时停止下顺势趋势单
            self._v2_pullback_block_enabled = await self._config.get_bool(
                "co.v2.pullback_block_enabled", True)
            self._v2_pullback_block_depth_atr = await self._config.get_float(
                "co.v2.pullback_block_depth_atr", 0.0)
            # [2026-08-10] 盲点修复：H1 方向兜底（M5 无方向信念时采用 H1 已确认方向）
            self._v2_h1_fb_enabled = await self._config.get_bool(
                "co.v2.h1_fallback_enabled", True)
            self._v2_h1_fb_min_strength = await self._config.get_float(
                "co.v2.h1_fallback_min_strength", 0.50)
            self._v2_h1_fb_lot_mult = await self._config.get_float(
                "co.v2.h1_fallback_lot_mult", 0.6)
            # [2026-07-25] 闸门可观测性：每次热重载/启动打印有效闸门，使"配置了≠生效了"
            # 能直接从日志确认（强/弱/冲击趋势门槛 + 激活模型 + range 拦截 + 归一尺度）。
            effective_active = await self._get_active_model()
            logger.info(
                "CoSourceEngine config loaded | co gate effective: active=%s "
                "strong_trend=%.2f weak_trend=%.2f shock_trend=%.2f with_trend_trend=%.1f "
                "adx_strong=%.1f range_block=%s score_scale=%.0f f6_quality=%s "
                "v2=%s v2_f2d=%.2f v2_f4d=%.2f v2_lot=%.2f v2_sl=%.2f v2_rr=%.2f "
                "v2_pb=%s v2_pb_depth=%.2f",
                effective_active,
                self._gate["strong"]["trend"], self._gate["weak"]["trend"],
                self._gate["shock"]["trend"], self._with_trend_trend,
                self._adx_strong,
                self._range_block, self._score_scale,
                self._f6_enabled,
                self._v2_enabled, self._v2_f2_discount, self._v2_f4_discount,
                self._v2_exec_lot_mult, self._v2_exec_sl_atr_mult, self._v2_exec_rr_min,
                self._v2_pullback_block_enabled, self._v2_pullback_block_depth_atr,
            )
            logger.info(
                "CoSourceEngine config loaded | h1_directional_fallback: enabled=%s "
                "min_strength=%.2f lot_mult=%.2f",
                self._v2_h1_fb_enabled, self._v2_h1_fb_min_strength, self._v2_h1_fb_lot_mult,
            )
        except Exception as exc:  # 单点失败不应拖垮信号生产
            logger.warning("CoSourceEngine config load failed: %s (using defaults)", exc)

    # ── 主入口 ────────────────────────────────
    async def apply(
        self,
        score_result: ScoreResult,
        indicators: IndicatorResults,
        regime_result: RegimeResult,
        h1_context: Optional[H1Context] = None,
        risk_level: str = "low",
        event_window: bool = False,
        consecutive_losses: int = 0,
        bar_quality: float = 1.0,
        spread_q: float = 1.0,
    ) -> ScoreResult:
        """对默认引擎产出的 ScoreResult 施加共源增强。

        Args:
            score_result: 默认评分引擎（ScoringEngine）产出的结果。
            indicators: 当前 bar 的指标容器。
            regime_result: M5 体制分类结果。
            h1_context: H1 多周期上下文（可选）。
            risk_level: 当日风险等级 low/med/high（由调度器从 Redis 读）。
            event_window: 是否处于重大数据窗口（F3 触发，由调度器从 Redis 读）。
            consecutive_losses: 连续亏损笔数（F5 触发，由调度器从 Redis 读）。

        Returns:
            - ``signal.active_model != "co_source"`` → 原样返回（fallback，字节级不变）
            - 否则 → 施加 F1–F6 过滤 + 校准因子 + 自适应门槛后的 ScoreResult
        """
        # 约束 ③：非共源模型 → 字节级透传，默认引擎完全不受影响。
        # [2026-07-25 根因修复] self._config 缺失时回退 co_source（开闸而非关闸），
        # 与 scheduler._detect_active_model 一致，避免配置缺失即静默关闭共源闸门。
        active = (await self._get_active_model()) if self._config is not None else CO_SOURCE_MODEL
        if active != CO_SOURCE_MODEL:
            return score_result

        # ── 重构方案 Phase1: 已确认的 NEUTRAL RSI 均值回归信号 ──
        # 旧实现阈值=0 裸奔放行（绕过 F4/F6 与自适应门槛），导致中立市浅层 RSI
        # 极值也被交易、胜率低。现改为：尊重 scoring_engine 已判 NO_TRADE 的否决，
        # 否则继续走下方 Step1(F4 跳过)/Step3 自适应门槛(专属较低阈值
        # co.neutral_rsi_min_score)，由 Step4 按真实 pre_score 裁决——不再裸奔。
        # （NO_TRADE 防御保留在下方 Step1 之前。）
        if (getattr(score_result, "neutral_rsi_confirmed", False)
                and score_result.direction == "NO_TRADE"):
            # scoring_engine 已否决（H1 防火墙/动量门控/校准硬闸门）→ 尊重否决
            return score_result

        # 防御：默认引擎已判定 NO_TRADE 的信号无需再处理
        if score_result.direction == "NO_TRADE":
            return score_result

        # ── Step 1: F1–F5 假信号过滤（在 pre_score 上扣分 / 作废）──
        score_result = self._apply_filters(
            score_result, indicators, regime_result, h1_context,
            event_window, consecutive_losses, bar_quality, spread_q,
        )
        # ── Step 2: 校准因子（冷启动期恒 1.0，读 co.calib.*）──
        # 仅对仍持有方向(未被判 NO_TRADE)的信号做校准，避免覆盖结构性拦截 reason。
        if score_result.direction != "NO_TRADE":
            factor = self._calibration_factor(regime_result.regime)
            if factor != 1.0:
                score_result.pre_score = round(score_result.pre_score * factor, 4)
                score_result.fallback_reason = (
                    f"co_calib({regime_result.regime.value},x{factor:.2f})"
                )

        # ── Step 3: 自适应入市门槛（覆盖 threshold，单一权威）──
        # 架构去混乱(2026-07-31)：无论上文 F1–F5 是否判 NO_TRADE，本函数都重算
        # threshold，杜绝 scoring_engine 基线值泄漏为最终门槛（双裁决混乱）。
        # co_source 自适应闸门是生产链路唯一权威的评分门槛裁决源。
        self._apply_adaptive_gate(score_result, indicators, regime_result, risk_level)

        # ── Step 4: 重算门控结果 ──
        # 仅对仍持方向(未被 F1–F5 / range_block 结构性拦截)的信号按 co_source 门槛裁决；
        # 已判 NO_TRADE 的信号保持 False，避免上方未清零的 pre_score 把拦截单误翻为通过。
        if score_result.direction != "NO_TRADE":
            score_result.threshold_passed = score_result.pre_score >= score_result.threshold
        return score_result

    # ── Phase 1/2/3 收敛决策（co.v2_enabled 时由 scheduler 调用）───────────
    async def apply_v2(
        self,
        score_result: ScoreResult,
        ind: IndicatorResults,
        regime_result: RegimeResult,
        h1_context: Optional[H1Context],
        micro_state_result: Optional[MicroStateResult],
        entry_quality: float,
        theta: float,
        risk_level: str = "low",
        event_window: bool = False,
        consecutive_losses: int = 0,
        bar_quality: float = 1.0,
        spread_q: float = 1.0,
    ) -> ScoreResult:
        """Phase 1+2+3 收敛决策引擎（仅当 co.v2_enabled=True 时由 scheduler 调用）。

        设计（docs/cosource_refactor_plan.md 六）：
          • Phase 1：方向与分数由 precision_entry 的 entry_quality 产出，co_source 仅作门槛裁决；
                    旧的 overheat/lag_momentum/lagging_discount 折扣已在 scoring_engine 内被开关旁路。
          • Phase 2：F1/F2/F4 降级为因子（折扣，不再作废方向）；NEUTRAL RSI 通道限定在
                    REVERSAL/RANGE 微观态且需结构确认才放行均值回归。
          • Phase 3：band 门槛统一为 θ(状态, 波动率)；with_trend 豁免并入方向对齐度（顺 H1 放松门槛）。

        本方法是 *附加* 权威路径：默认引擎 compute_pre_score + co_source.apply(v1) 仍照常运行，
        其结论作为 shadow legacy 落样；apply_v2 仅改写 score_result 的方向/分数/门槛/是否放行，
        下游（限速/发布/SL·TP·lot）无缝沿用改写后的字段。co.v2_enabled=False 时 scheduler 不调用本方法。
        """
        scale = self._score_scale if self._score_scale > 0 else 100.0
        ms: Optional[MicroStateResult] = micro_state_result
        eq: float = max(0.0, min(1.0, float(entry_quality or 0.0)))
        # Phase 1：方向基线 = 引擎权威方向（score_result.direction，已含 H1 防火墙 /
        # min_adx / lag_momentum 等结构性否决）；仅当微观状态有明确方向信念
        # （UP/DOWN）时才覆盖。严禁泄漏空串 ""（RANGE/中性态 ms.direction 默认
        # ""，否则下游 signal_dir 非法、threshold_passed 还会被误判 True）。
        _ms_dir = ms.direction if (ms is not None and ms.direction in ("BUY", "SELL")) else ""
        _engine_dir = score_result.direction if score_result.direction in ("BUY", "SELL") else "NO_TRADE"
        direction: str = _ms_dir if _ms_dir else _engine_dir
        # 标记 M5 是否曾表达过方向信念：用于盲点兜底时避免覆盖真实 M5 信号。
        _m5_had_direction = bool(_ms_dir) or (_engine_dir != "NO_TRADE")
        # H1 方向禁区判定用 compute_pre_score 已写入的 h1_bias（与 H1Context 一致）
        h1_bias = getattr(score_result, "h1_bias", None)
        reason: str = ""

        # ── H1 方向禁区（硬闸，保留）：与已确认 H1 趋势逆势 → 作废 ──
        if direction != "NO_TRADE" and h1_bias is not None and direction != h1_bias:
            direction = "NO_TRADE"
            reason = f"v2_h1_counter_trend(dir={ms.direction if ms else '?'},h1={h1_bias})"
            eq = 0.0

        # ── Phase 2.4：回踩保护（2026-08-05）──
        # 行情回调（TREND_PULLBACK=价格逆趋势回撤）时停止下顺势趋势单，避免回踩
        # 未结束/演变为反转导致顺势单被扫损。仅拦顺势方向（direction==h1_bias，逆势单
        # 已在上方 H1 禁区作废）。开关 co.v2.pullback_block_enabled（默认 True）；
        # 深度阈值 co.v2.pullback_block_depth_atr（默认 0=任何回踩都拦；调大则仅深度
        # 回调才拦，浅回踩仍作为买点保留）。
        if (direction != "NO_TRADE" and self._v2_pullback_block_enabled
                and ms is not None and ms.state == MicroState.TREND_PULLBACK):
            _pb_depth = float((ms.details or {}).get("pullback_depth_atr", 0.0))
            if _pb_depth >= self._v2_pullback_block_depth_atr:
                direction = "NO_TRADE"
                reason = (f"v2_pullback_block(dir={ms.direction} depth={_pb_depth:.2f}ATR"
                          f">=block {self._v2_pullback_block_depth_atr:.2f})")
                eq = 0.0

        # ── Phase 1.5：[2026-08-10] 盲点修复 —— M5 无方向信念但 H1 已确认 → 用 H1 方向兜底 ──
        # 场景：M5 处于 RANGE/低波动（ADX<22、BBW 窄），scoring_engine 因 co.gate.range.block
        # 把方向判为 NO_TRADE；但 H1（更高周期、ADX 天然更高）已确认 SELL/BUY。原逻辑 H1 只能
        # "降门槛"不能"造方向"，导致"明显趋势却不下单"。此处当 M5 完全没有方向信念
        # （_m5_had_direction=False，即既无微观方向也无引擎方向）且 H1 给出已确认 bias 且
        # 强度达标时，用 H1 方向兜底。F6 棒质量/点差闸门（下方 Phase 2）仍会在其后拦截劣质棒。
        _is_fb = False
        # 方案 B：盲点兜底必须带两道安全守卫，避免 H1 滞后标签在 M5 已反弹时仍强行逆势下单。
        #   守卫① h1_context.direction_confirmed：H1 方向须是近 N 根 H1 收盘真实确认的，
        #           而非陈旧 regime 标签（下跌末期的滞后锁空在此判 False）。
        #   守卫② 末棒逆 H1（ms.details["last_bar_against"]）：M5 最新一根棒已在逆 H1 拐头，
        #           即便 H1 还没翻，也不逆向兜底。
        _h1_confirmed = bool(h1_context.direction_confirmed) if h1_context is not None else False
        _m5_against_h1 = bool((ms.details or {}).get("last_bar_against", False)) if ms is not None else False
        if (self._v2_h1_fb_enabled and direction == "NO_TRADE" and not _m5_had_direction
                and h1_bias is not None and h1_context is not None
                and h1_context.trend_strength >= self._v2_h1_fb_min_strength
                and _h1_confirmed                       # 守卫①：H1 是近期收盘确认的，非滞后标签
                and not _m5_against_h1):                # 守卫②：M5 末棒没在逆 H1 反弹
            direction = h1_bias
            _is_fb = True
            reason = (f"v2_h1_directional_fallback(h1={h1_bias},"
                      f"strength={h1_context.trend_strength:.2f})")
            logger.info(
                "CoSource v2 H1 directional fallback → adopt %s "
                "(M5 had no direction, h1_strength=%.2f >= %.2f, confirmed=%s)",
                direction, h1_context.trend_strength, self._v2_h1_fb_min_strength, _h1_confirmed,
            )
        elif (self._v2_h1_fb_enabled and direction == "NO_TRADE" and not _m5_had_direction
                and h1_bias is not None and h1_context is not None
                and h1_context.trend_strength >= self._v2_h1_fb_min_strength
                and not _is_fb):
            # 满足旧条件但不满足守卫 → 记可观测日志（被哪道守卫挡），保持观望。
            logger.info(
                "CoSource v2 H1 fallback SUPPRESSED (stay NO_TRADE): "
                "h1=%s strength=%.2f confirmed=%s m5_against_h1=%s",
                h1_bias, h1_context.trend_strength, _h1_confirmed, _m5_against_h1,
            )

        # ── Phase 2：NEUTRAL RSI 通道仅在 REVERSAL/RANGE 微观态 + 结构确认才放行 ──
        _neutral_ok = bool(getattr(score_result, "neutral_rsi_confirmed", False))
        if _neutral_ok and not (
            ms is not None and ms.state in (MicroState.REVERSAL, MicroState.RANGE)
        ):
            # 非回撤/区间态的 RSI 均值回归信号不在此路径放行（交给方向与质量裁决）
            score_result.neutral_rsi_confirmed = False
            _neutral_ok = False

        # ── Phase 2：F1/F2/F4 降级为因子（折扣，不废方向）──
        if direction != "NO_TRADE":
            # F1 周期背离 → 折扣
            if self._f1_enabled:
                _highs = getattr(ind, "recent_highs", None) or []
                _lows = getattr(ind, "recent_lows", None) or []
                _hist = getattr(ind, "macd_histogram", 0.0) or 0.0
                _prev = getattr(ind, "macd_histogram_previous", 0.0) or 0.0
                _close = getattr(ind, "close", 0.0) or 0.0
                _div = False
                if direction == "BUY" and _highs and _close >= max(_highs) and _hist <= _prev:
                    _div = True
                elif direction == "SELL" and _lows and _close <= min(_lows) and _hist >= _prev:
                    _div = True
                if _div:
                    eq = max(0.0, eq - min(eq, self._f1_penalty / scale))
            # F2 布林收口 → 轻度折扣（不再作废）
            if self._f2_enabled:
                _bbw = getattr(ind, "bbw", 0.0) or 0.0
                _bbw_ma = getattr(ind, "bbw_ma20", 0.0) or 0.0
                if _bbw_ma > 0 and _bbw < _bbw_ma * (self._f2_ratio / 100.0):
                    eq = max(0.0, eq - self._v2_f2_discount)
            # F4 超买超卖钝化 → 折扣（中性 RSI 已确认且在 REVERSAL/RANGE 态则跳过）
            if self._f4_enabled and not _neutral_ok:
                _rsi = getattr(ind, "rsi_14", 50.0) or 50.0
                if direction == "BUY" and _rsi <= self._f4_rsi_lower:
                    eq = max(0.0, eq - self._v2_f4_discount)
                if direction == "SELL" and _rsi >= self._f4_rsi_upper:
                    eq = max(0.0, eq - self._v2_f4_discount)

        # ── 独立风险闸 F3/F5/F6（真实风险，保留）──
        if direction != "NO_TRADE":
            # F3 数据窗口期降分
            if self._f3_enabled and event_window:
                eq = max(0.0, eq - min(eq, self._f3_penalty / scale))
            # F5 连续亏损熔断：折扣 + 极端补充触发
            if self._f5_enabled and consecutive_losses >= self._f5_consecutive:
                eq = max(0.0, eq - min(eq, self._f5_penalty / scale))
                score_result.co_emergency_trigger = True
            # F6 棒质量 / 点差质量闸门：异常 → 作废
            if self._f6_enabled:
                if bar_quality < self._f6_quality_min:
                    direction = "NO_TRADE"
                    reason = f"v2_f6_quality_low(q={bar_quality:.3f}<{self._f6_quality_min:.2f})"
                    eq = 0.0
                elif spread_q > self._f6_spread_q_max:
                    direction = "NO_TRADE"
                    reason = f"v2_f6_spread_wide(sq={spread_q:.2f}>{self._f6_spread_q_max:.2f})"
                    eq = 0.0

        # ── Phase 3：单一权威门槛 θ(状态,波动率)；with_trend 豁免并入方向对齐 ──
        _theta = float(theta if theta is not None else 0.0)
        if direction != "NO_TRADE" and h1_bias is not None and direction == h1_bias:
            # 顺 H1：门槛放开到 with_trend 硬上限（并入方向对齐度豁免）
            _theta = min(_theta, self._with_trend_trend / 100.0)
        passed = (direction != "NO_TRADE") and (eq >= _theta)

        # ── 写回 score_result（下游沿用）──
        score_result.direction = direction
        score_result.pre_score = round(eq, 4)
        score_result.threshold = round(_theta, 4)
        score_result.threshold_passed = passed
        score_result.fallback_reason = (reason if (not passed and reason) else "")
        score_result.co_band = f"v2:{ms.state.value}" if ms is not None else "v2"
        score_result.co_exec_fb = bool(_is_fb)  # 盲点兜底单标记：方向由 H1 兜底、进场点位须交 M5 判定
        if passed:
            # 盲点兜底单置信度较低：采用降仓系数（默认 0.6），其余执行参数不变。
            score_result.co_exec_lot_mult = (
                self._v2_h1_fb_lot_mult if _is_fb else self._v2_exec_lot_mult)
            score_result.co_exec_sl_atr_mult = self._v2_exec_sl_atr_mult
            score_result.co_exec_rr_min = self._v2_exec_rr_min
        else:
            score_result.co_exec_sl_atr_mult = None
            score_result.co_exec_rr_min = None
            score_result.co_exec_lot_mult = None
        logger.info(
            "CoSource v2 decision: dir=%s eq=%.3f theta=%.3f passed=%s band=%s reason=%s",
            direction, eq, _theta, passed, score_result.co_band, score_result.fallback_reason,
        )
        return score_result

    # ── 内部：激活模型判定 ──────────────────────
    async def _get_active_model(self) -> str:
        """解析当前激活模型。

        与 scheduler._detect_active_model 保持一致：缺失/异常时回退 co_source（开闸而非关闸），
        避免 signal.active_model 因未双写 Redis 而缺失时，共源闸门静默失效
        （2026-07-25 根因：7-23~7-24 该键只写 PG、未双写 Redis，闸门空转约 1.5 天，
        0.13~0.30 弱分信号放量逆势亏损）。
        """
        try:
            val = await self._config.get(ACTIVE_MODEL_KEY)
            return (val or CO_SOURCE_MODEL).strip()
        except Exception as exc:
            logger.warning(
                "CoSourceEngine active_model read failed: %s → co_source (fail-open)", exc
            )
            return CO_SOURCE_MODEL

    # ── 内部：校准因子 ─────────────────────────
    def _calibration_factor(self, regime: Regime) -> float:
        """返回当前体制的校准乘数（冷启动期配置值=1.0）。"""
        return self._calib.get(regime, _CALIB_DEFAULT)

    # ── 内部：F1–F5 假信号过滤 ─────────────────
    def _apply_filters(
        self,
        sr: ScoreResult,
        ind: IndicatorResults,
        regime: RegimeResult,
        h1: Optional[H1Context],
        event_window: bool,
        consec: int,
        bar_quality: float = 1.0,
        spread_q: float = 1.0,
    ) -> ScoreResult:
        scale = self._score_scale if self._score_scale > 0 else 100.0

        # F1 周期背离检测：价格创新高/低但 MACD 柱未确认
        if self._f1_enabled:
            penalized = False
            highs = getattr(ind, "recent_highs", None) or []
            lows = getattr(ind, "recent_lows", None) or []
            macd_hist = getattr(ind, "macd_histogram", 0.0) or 0.0
            macd_prev = getattr(ind, "macd_histogram_previous", 0.0) or 0.0
            close = getattr(ind, "close", 0.0) or 0.0
            if sr.direction == "BUY" and highs and close >= max(highs) and macd_hist <= macd_prev:
                penalized = True  # 价创新高但 MACD 动能走弱 → 顶背离
            elif sr.direction == "SELL" and lows and close <= min(lows) and macd_hist >= macd_prev:
                penalized = True  # 价创新低但 MACD 动能走强 → 底背离
            if penalized:
                sr.pre_score = round(sr.pre_score - self._f1_penalty / scale, 4)
                sr.fallback_reason = f"co_f1_divergence(-{self._f1_penalty/scale:.3f})"

        # F2 布林收口假突破：带宽 < 近20根均值 × ratio% → 作废
        if self._f2_enabled and sr.direction != "NO_TRADE":
            bbw = getattr(ind, "bbw", 0.0) or 0.0
            bbw_ma = getattr(ind, "bbw_ma20", 0.0) or 0.0
            if bbw_ma > 0 and bbw < bbw_ma * (self._f2_ratio / 100.0):
                sr.direction = "NO_TRADE"
                sr.threshold_passed = False
                sr.fallback_reason = (
                    f"co_f2_bb_squeeze(bbw={bbw:.4f}<{bbw_ma*(self._f2_ratio/100.0):.4f})"
                )
                return sr

        # F3 数据窗口期降分：重大数据公布前 N 分钟（由事件窗口标志触发）
        if self._f3_enabled and event_window and sr.direction != "NO_TRADE":
            sr.pre_score = round(sr.pre_score - self._f3_penalty / scale, 4)
            sr.fallback_reason = f"co_f3_event_window(-{self._f3_penalty/scale:.3f})"

        # F4 超买超卖钝化：单边极端 RSI 下取消反向信号
        # 重构方案 Phase1: NEUTRAL RSI 均值回归与 F4 空头钝化本就冲突 → 跳过 F4
        if (self._f4_enabled and sr.direction != "NO_TRADE"
                and not getattr(sr, "neutral_rsi_confirmed", False)):
            rsi = getattr(ind, "rsi_14", 50.0) or 50.0
            if sr.direction == "BUY" and rsi <= self._f4_rsi_lower:
                sr.direction = "NO_TRADE"  # 空头钝化不做多
                sr.threshold_passed = False
                sr.fallback_reason = f"co_f4_oversold(rsi={rsi:.0f})"
                return sr
            if sr.direction == "SELL" and rsi >= self._f4_rsi_upper:
                sr.direction = "NO_TRADE"  # 多头钝化不做空
                sr.threshold_passed = False
                sr.fallback_reason = f"co_f4_overbought(rsi={rsi:.0f})"
                return sr

        # F5 连续亏损熔断：连续 N 笔止损 → 扣分 + 标记极端补充调用
        if self._f5_enabled and consec >= self._f5_consecutive and sr.direction != "NO_TRADE":
            sr.pre_score = round(sr.pre_score - self._f5_penalty / scale, 4)
            sr.fallback_reason = f"co_f5_drawdown_brake(-{self._f5_penalty/scale:.3f},n={consec})"
            sr.co_emergency_trigger = True  # 供 P2 批量 AI 极端补充调用读取

        # F6 [2026-07-30 C 组] 棒质量 / 点差质量闸门（灰度开关，默认关闭）
        # 仅当 co.filter.f6_quality_enabled=True 时生效。质量分过低（异常棒）或相对
        # 均值点差过宽（spread_q 过高 → 成交成本异常）时，作废当前方向信号。
        if self._f6_enabled and sr.direction != "NO_TRADE":
            if bar_quality < self._f6_quality_min:
                sr.direction = "NO_TRADE"
                sr.threshold_passed = False
                sr.fallback_reason = (
                    f"co_f6_quality_low(q={bar_quality:.3f}<{self._f6_quality_min:.2f})"
                )
                return sr
            if spread_q > self._f6_spread_q_max:
                sr.direction = "NO_TRADE"
                sr.threshold_passed = False
                sr.fallback_reason = (
                    f"co_f6_spread_wide(sq={spread_q:.2f}>{self._f6_spread_q_max:.2f})"
                )
                return sr

        return sr

    # ── 内部：自适应入市门槛 ───────────────────
    def _apply_adaptive_gate(
        self,
        sr: ScoreResult,
        ind: IndicatorResults,
        regime: RegimeResult,
        risk_level: str,
    ) -> None:
        scale = self._score_scale if self._score_scale > 0 else 100.0
        adx = getattr(ind, "adx_14", 0.0) or 0.0
        vol_factor = getattr(regime, "vol_factor", 1.0) or 1.0

        # ── 重构方案 Phase1: NEUTRAL RSI 均值回归不再裸奔(threshold=0) ──
        # 改用专属较低门槛 co.neutral_rsi_min_score（默认 0.15，过滤纯噪声），
        # 仍保留豁免 F4 的均值回归意图；由 Step4 按真实 pre_score 裁决。
        if getattr(sr, "neutral_rsi_confirmed", False) and sr.direction != "NO_TRADE":
            sr.threshold = self._neutral_rsi_min_score
            sr.fallback_reason = f"neutral_rsi_confirmed({sr.direction})"
            return

        # ── 行情带判定 ──
        if vol_factor >= self._shock_atr_mult:
            band = "shock"
        elif regime.regime in (Regime.RANGE, Regime.NEUTRAL):
            band = "range"
        elif adx >= self._adx_strong:
            band = "strong"
        else:
            band = "weak"

        # ── 顺 H1 方向判定（高空/低多豁免，B 项）──
        # h1_bias：H1 已确认的顺势方向（UP→BUY / DOWN→SELL，强度≥h1_bias_min_strength）。
        # 仅当信号方向与 H1 一致时才放宽；逆 H1 单不享受豁免（仍受反向阻断/严门槛约束）。
        with_trend = (
            sr.h1_bias is not None
            and sr.direction != "NO_TRADE"
            and sr.direction == sr.h1_bias
        )

        # ── 震荡/中性市拦截 ──
        base_override = None
        if band == "range":
            if self._range_block and not with_trend:
                sr.direction = "NO_TRADE"
                sr.threshold = 1.0  # 不可达，确保拦截
                sr.threshold_passed = False
                sr.fallback_reason = "co_range_blocked"
                return
            # 未启用拦截：NEUTRAL 用专属评分门槛（0-1 尺度，不经 score_scale 归一），
            # RANGE 仍退化为弱趋势门槛（沿用 co.gate.weak.trend）
            if regime.regime == Regime.NEUTRAL:
                base_override = self._neutral_min_score
                # NEUTRAL 走专属评分门槛，但执行增强参数(lot/sl/rr)需有 gate 源，
                # 否则下方无条件引用 gate["lot"] 等会抛 UnboundLocalError。
                gate = self._gate["weak"]
            else:
                band = "weak"

        if base_override is not None:
            base = base_override
        else:
            gate = self._gate[band]
            base = gate["trend"] / scale  # 0–100 → 0–1

        # ── 风险等级偏移 ──
        if risk_level == "high":
            base += self._risk_high_offset / scale
        elif risk_level == "med":
            base += self._risk_med_offset / scale

        # ── B 项：顺 H1 方向用更宽松门槛（硬上限，覆盖 band / neutral / 风险偏移）──
        # 即便 M5 处于 strong(0.40)/weak(0.50)/neutral(0.45) 带、即便叠加风险偏移，
        # 只要方向顺 H1(h1_bias)，门槛恒降到 co.gate.with_trend.trend（默认 0.30）——
        # 顺 H1 单不享受也不叠加风险惩罚，使"顺 H1 高空/低多"在所有风险等级下都能真正出单。
        # 仍受 scoring_engine 的 min_adx_for_trade 地板（ADX≥18 才可能持方向）约束。
        if with_trend:
            base = self._with_trend_trend / scale

        sr.threshold = round(base, 4)
        # 附带执行增强提示（供 P2/P3 下单尺寸 / SL / TP 使用；不影响 P1a 门控）
        sr.co_exec_lot_mult = gate["lot"]
        sr.co_exec_sl_atr_mult = gate["sl_atr"]
        sr.co_exec_rr_min = gate["rr_min"]
        sr.co_band = ("with_trend" if with_trend else band)
