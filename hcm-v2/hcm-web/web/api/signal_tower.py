"""Signal Tower API — Cooldown config, dual-mode switching, symbol-level config, prompt, watchdog.

Provides:
- GET /api/v1/signal-tower/cooldown — Cooldown config (global + per-symbol overrides)
- PUT /api/v1/signal-tower/cooldown — Batch update cooldown config
- GET /api/v1/signal-tower/cooldown/status — Current cooldown status (per symbol)
- GET /api/v1/signal-tower/mode — Current mode (AI dynamic / manual)
- PUT /api/v1/signal-tower/mode — Switch mode and set manual_regime_score
- GET /api/v1/signal-tower/symbol-config — Symbol-level tower config list
- PUT /api/v1/signal-tower/symbol-config — Batch update symbol-level tower config
- GET /api/v1/signal-tower/prompt — Prompt configuration (9 fields)
- PUT /api/v1/signal-tower/prompt — Update prompt configuration
- GET /api/v1/signal-tower/watchdog — Watchdog status and services
- POST /api/v1/signal-tower/watchdog/{action} — Watchdog pause/resume/restart
- GET /api/v1/signal-tower/retrain/summary — 自动重训守护报表（LightGBM 运行状况 + DeepSeek 裁判总结）
- Legacy aliases: /api/signal-tower/threshold, /mode, /symbol-config, /prompt, /watchdog
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _retrain_redis() -> Any:
    """返回只读 Redis 连接（用于查 auto_retrain 守护写的重训历史/心跳）。

    auto_retrain 是 Windows 主机进程，写的是宿主机 Redis（docker 单实例）。
    web 容器经 REDIS_URL=redis://redis:6379（docker 服务名）可达同一实例，
    故此处优先用 REDIS_URL，回退 localhost:6379。
    失败返回 None（handler 做降级，不抛 500）。
    """
    try:
        import redis
        url = os.environ.get("REDIS_URL", "")
        if url:
            return redis.from_url(url, socket_timeout=5, decode_responses=True)
        return redis.Redis(host="localhost", port=6379, socket_timeout=5, decode_responses=True)
    except Exception as e:  # pragma: no cover
        logger.warning("retrain redis connect failed: %s", e)
        return None


# ── Funnel reason → 精准中文卡点映射 ──────────────────
# 信号漏斗「最近信号明细」的「原因」列原样透传引擎写入的英文 fallback_reason
# （如 hexp_grade_below_min(C<B) / hexp_extreme_guard(retreat mm=0.07 pos=1.00)），
# 非程序员无法判读。此处做**只读展示层**映射：不改变任何拦截逻辑与落库值，
# 仅把原始串翻译成精准卡点名。原始串仍由 detail.reason 字段保留（前端悬浮可查）。
#
# 覆盖来源（file:line 为真源）：
#   hexp_engine.py  ~1036-1140  hexp_grade_red / hexp_grade_below_min / hexp_no_direction
#                               / hexp_extreme_reversal / hexp_extreme_guard
#                               / hexp_coupling_below_min / hexp_zone_blocked
#                               / hexp_disabled / hexp_no_fetcher / hexp_no_periods
#   scheduler.py    ~2060       cooldown_active(remaining=..s,cooldown=..s)
#   scoring_engine / co_source  below_threshold / *_reverse* / lag_momentum_conflict
#                               / co_f1..co_f6 / range_no_extreme / calib_block
#   rule_chain.py               risk_max_open_positions / risk_cool_minutes
_REASON_EXACT: dict[str, str] = {
    "hexp_grade_red": "评分等级=红（最差档，禁止下单）",
    "hexp_no_direction": "无明确方向（七因子方向裁决未达方向门槛）",
    "hexp_zone_blocked": "结构位卡点（未触及入场区，zone 未触发）",
    "hexp_disabled": "乘幂引擎已关闭（未评分）",
    "hexp_no_fetcher": "数据源未就绪（K线读取器缺失）",
    "hexp_no_periods": "数据源未就绪（多周期K线缺失）",
    "hexp_no_primary_data": "数据源未就绪（主周期K线缺失）",
    "range_no_extreme": "震荡市极值闸门（价格未到布林/RSI极值，不做均值回归）",
    "no_trade_direction": "无明确方向（买卖分差不足）",
    "none": "无（未标记卡点）",
}

# 前缀 / 子串匹配规则（按顺序命中，先精确后模糊）
_REASON_PREFIX: tuple[tuple[str, str], ...] = (
    ("hexp_extreme_reversal", "极值反转保护（顶部接刀/底部抄底：极值区+动量反转被拦截）"),
    ("hexp_extreme_guard", "极值保护（极值区动量回撤，不追原趋势单）"),
    ("hexp_coupling_below_min", "共振耦合分不足（多周期未共振）"),
    ("hexp_grade_below_min", "评分等级不足（低于可下单评级）"),
    # AI 耦合闸门（历史数据；当前 ai.mode=decoupled 已不再产生）
    ("hexp_ai_coupling_blocked", "AI 耦合拦截（乘幂分与 AI 分耦合后总评不达门槛）"),
    # 评分过门槛但方向裁决失败（非"分不够"，是买卖分差不足）
    ("no_trade_direction", "无明确方向（评分已过门槛，但买卖方向分差不足）"),
    # H1 方向补位：不是拦截，是 M5 无方向时由 H1 趋势补方向
    ("v2_h1_directional_fallback", "H1 方向补位（M5 无方向，采用 H1 趋势方向）"),
    # 主号手动镜像（跟单生命周期动作，非模型信号，不是卡点）
    ("Manual mirror", "主号手动镜像（跟单同步动作，非模型信号）"),
)

_REASON_CONTAINS: tuple[tuple[str, str], ...] = (
    # 风控层（rule_chain）
    ("risk_max_open_positions", "同向最大订单（同方向持仓数已达上限）"),
    ("max_open_positions", "同向最大订单（同方向持仓数已达上限）"),
    ("same_dir_bypass_limit", "同向最大订单（同方向实时持仓数达上限）"),
    ("risk_cool_minutes", "同向保本闸门（最新同向持仓SL达保本才放行）"),
    ("risk_daily_loss", "当日亏损熔断（已达单日最大亏损）"),
    ("risk_min_confidence", "置信度不足（低于风控最小置信度）"),
    ("risk_spread", "点差过大（超出风控点差上限）"),
    # 逆势 / 动量
    ("lag_momentum", "动量反向（滞后组主导但动量反侧，冲突拦截）"),
    ("counter_trend", "逆势拦截（与 H1 趋势方向相反）"),
    ("reverse", "逆势/动量反向（反趋势保护降分或拦截）"),
    # 评分门槛
    ("below_threshold", "评分未达门槛（低于当前体制评分门槛）"),
    ("grade_below", "评分等级不足（低于可下单评级）"),
    # ADX
    ("adx_floor", "ADX 地板（趋势强度不足，禁止交易）"),
    ("min_adx", "ADX 地板（趋势强度不足，禁止交易）"),
    # 共源过滤器
    ("co_f1", "共源F1 过滤（双源方向背离）"),
    ("co_f2", "共源F2 过滤（结构位冲突）"),
    ("co_f3", "共源F3 过滤（波动率异常）"),
    ("co_f4", "共源F4 过滤（时段/流动性受限）"),
    ("co_f5", "共源F5 过滤（连续亏损回撤刹车）"),
    ("co_f6", "共源F6 过滤（棒质量/点差质量不达标）"),
    ("co_range_blocked", "震荡市拦截（RANGE 体制禁止开仓）"),
    ("calib_block", "校准器拦截（历史胜率过低的市况桶）"),
    # 冷却
    ("cooldown", "信号冷却（同品种同向产出节流中）"),
    ("range_breakout", "假突破保护（震荡边界突破未确认）"),
    ("giveup_reverse", "策略放弃（方向反转未确认）"),
    ("drawdown_brake", "回撤刹车（连续亏损后降分）"),
    ("probe_already_fired", "试探单已用（本轮衰竭周期仅允许一笔）"),
)


def _map_one_reason(r: str) -> str:
    """映射单个 reason 片段（不含 " | " 分隔符）。"""
    # 1) 精确匹配
    if r in _REASON_EXACT:
        return _REASON_EXACT[r]

    # 2) 前缀匹配（带数值回填，让用户看到具体卡在哪个值）
    for prefix, label in _REASON_PREFIX:
        if not r.startswith(prefix):
            continue
        if prefix == "hexp_grade_below_min":
            m = re.search(r"\(\s*([^<\s]+)\s*<\s*([^)\s]+)\s*\)", r)
            if m:
                return f"评分等级不足（当前 {m.group(1)} 级 < 可下单评级 {m.group(2)} 级）"
        elif prefix == "hexp_coupling_below_min":
            m = re.search(r"total=([\d.]+)\s*<\s*([\d.]+)", r)
            if m:
                return f"共振耦合分不足（总评 {m.group(1)} < 门槛 {m.group(2)}）"
        elif prefix == "hexp_extreme_guard":
            m = re.search(r"pos=([\d.]+)", r)
            if m:
                return f"极值保护（价格处极值分位 {m.group(1)}，动量回撤不追单）"
        elif prefix == "hexp_ai_coupling_blocked":
            m = re.search(r"total=([\d.]+)", r)
            if m:
                return f"AI 耦合拦截（耦合后总评 {m.group(1)} 不达门槛）"
        elif prefix == "no_trade_direction":
            m = re.search(r"pre=([\d.]+)\s*,\s*thr=([\d.]+)", r)
            if m:
                return (f"无明确方向（评分 {m.group(1)} 已过门槛 {m.group(2)}，"
                        f"但买卖方向分差不足）")
        elif prefix == "v2_h1_directional_fallback":
            m = re.search(r"h1=([A-Z]+)", r)
            if m:
                return f"H1 方向补位（M5 无方向，采用 H1 {m.group(1)} 方向）"
        return label

    # 3) 冷却（带剩余秒数回填）
    if r.startswith("cooldown_active"):
        m = re.search(r"remaining=(\d+)s\s*,\s*cooldown=(\d+)s", r)
        if m:
            return f"信号冷却（冷却 {m.group(2)}s，剩余 {m.group(1)}s）"
        return "信号冷却（同品种同向产出节流中）"

    # 3b) 评分未达门槛（带实际分值/门槛回填，形如 below_threshold(0.194<0.300)）
    if r.startswith("below_threshold"):
        m = re.search(r"\(\s*([\d.]+)\s*<\s*([\d.]+)\s*\)", r)
        if m:
            return f"评分未达门槛（评分 {m.group(1)} < 门槛 {m.group(2)}）"
        return "评分未达门槛（低于当前体制评分门槛）"

    # 4) 子串匹配
    low = r.lower()
    for token, label in _REASON_CONTAINS:
        if token in low:
            return label

    # 5) 未命中：原样返回，避免信息丢失
    return r


def map_funnel_reason(raw: Optional[str]) -> str:
    """把引擎写入的英文 fallback_reason 映射为精准中文卡点名（纯展示层，不改逻辑）。

    引擎在同一条信号上可能叠加多个拦截原因，用 " | " 拼接落库
    （实测存在前导空段，如 " | hexp_coupling_below_min(total=44.0<50.0)"），
    故先按 "|" 拆分、逐段映射、去重后再用 " + " 连接展示。

    Args:
        raw: signals.fallback_reason 原始值，可能为 None/空串/多原因拼接串。

    Returns:
        精准中文卡点名；含关键数值时回填到文案里（如评级、耦合分、剩余冷却秒数）。
        未命中任何规则的片段原样保留，保证信息不丢失。
    """
    if not raw or not raw.strip():
        return "未标记"

    parts = [p.strip() for p in raw.split("|")]
    mapped: list[str] = []
    for p in parts:
        if not p:
            continue  # 跳过空段（引擎拼接产生的前导/尾随空片段）
        cn = _map_one_reason(p)
        if cn and cn not in mapped:
            mapped.append(cn)

    if not mapped:
        return "未标记"
    return " + ".join(mapped)

# ── Constants ───────────────────────────────────

# Threshold panel config keys & defaults — SOURCE OF TRUTH for BOTH the
# /threshold endpoint and the /cooldown endpoint. MUST match frontend
# buildFields() in hcm-web/frontend/src/pages/signaltower/Threshold.tsx
# (7 functional groups). Only keys the engine actually CONSUMES are exposed
# here — dead keys (midbar.*, scoring offset/floor/ceiling, score_threshold,
# pretrend/trend/fade_threshold bare, trend_same_dir_*) were removed after a
# source audit (read+logged or never referenced in any computation).
# Stored as real types so the frontend parses them directly.
THRESHOLD_CONFIG_DEFAULTS: dict[str, Any] = {
    # ── ① Cooldown (regime-specific seconds) — scoring_engine.py 971-987 ──
    # [2026-08-01] 默认值对齐部署真值：生产实际 pretrend/trend/neutral/range=0（关闭冷却），fade=300。
    "pretrend_cooldown_seconds": 0,
    "trend_cooldown_seconds": 0,
    "fade_cooldown_seconds": 300,
    "range_boundary_cooldown_seconds": 0,
    "neutral_cooldown_seconds": 0,
    # ── ② Trade-gate ADX (hard NO_TRADE floor) — scoring_engine.py:444 / scheduler.py:454 ──
    "scoring.min_adx_for_trade": 18.0,
    "scoring.M5.min_adx_for_trade": 18.0,
    # ── ③ Regime classification ADX — regime_classifier.py 348/442/444/457 ──
    "regime_adx_trend": 24.0,  # [2026-08-01] 对齐部署（scheduler/前端均 24）
    "regime_adx_range": 22.0,
    "regime.trend_strong_adx_threshold": 28.0,
    # ── ④ Score gate — scoring_engine.py _compute_threshold & co_source.py _apply_adaptive_gate ──
    "scoring.min_score_threshold": 0.15,
    "scoring.neutral_min_score_threshold": 0.28,  # [2026-08-01] 对齐部署（NEUTRAL 体制专属评分门槛）
    # ── ⑤ AI dispute dispatch — scheduler.py:885 ──
    "scoring.M5.dispute_diff_threshold": 0.05,
    # ── ⑥ Reverse-trend protection (Plan B) — scoring_engine.py gate ──
    "scoring.trend_reverse_suppress_factor": 0.40,  # [2026-08-01] 对齐部署
    "scoring.strong_trend_block_reverse": True,
    "scoring.strong_trend_reverse_penalty": 0.30,  # [2026-08-04] 强趋势逆势降分系数
    # ── ⑥-b H1 主趋势方向门控（跨周期）— scoring_engine.py，2026-08-04 解耦硬阻断→逆势降分 ──
    # 逆 H1 一律降分（乘性折扣 pre_score），力度随 H1 确认度分级。
    "scoring.h1_reverse_block_enabled": True,
    "scoring.h1_reverse_penalty": 0.55,        # 逆 H1 未确认：较轻降分
    "scoring.h1_reverse_penalty_confirmed": 0.30,  # 逆 H1 已确认：更重降分
    # ── ⑥-c NEUTRAL RSI 均值回归总开关（scoring_engine.py 消费）──
    # 必须与前端 Threshold.tsx ⑥-c 组字段一致：本键若缺失，GET 不返回它 →
    # 面板刷新回退到 defaultValue:false（"保存后复原为关"的根因）。PG/Redis 已存的值仍会被读回。
    "scoring.neutral_rsi_enabled": False,
    # ── ⑦ Live-override rescue (reversal agility) — scheduler.py _live_override_loop ──
    "scoring.live_override_enabled": True,
    "scoring.live_override_sustained_sec": 20,
    "scoring.live_override_check_interval_sec": 5,
    "scoring.live_override_rate_limit_sec": 60,
    "trend_reverse_cooldown_seconds": 90,
    # 反向阻断 ADX（scoring_engine.py:516 Plan B）—— 与 regime.trend_strong_adx_threshold(28) 是不同键，勿合并
    "scoring.trend_strong_adx_threshold": 18.0,
    # ── 2026-07-25: RANGE 均值回归硬闸门（震荡市逢高做空/逢低做多）──
    "scoring.range_min_pct_b": 0.15,
    "scoring.range_stoch_extreme": 25.0,
    "scoring.range_rsi_extreme_low": 30.0,  # [2026-08-01] 对齐部署
    "scoring.range_rsi_extreme_high": 70.0,  # [2026-08-01] 对齐部署
    "scoring.range_breakout_mult": 1.005,
    # ── 2026-07-25: 校准硬闸门（多而准）── 低胜率(体制,分数桶)直接 NO_TRADE
    "scoring.calibration_enabled": True,
    "scoring.calibration_hard_gate": False,  # [2026-08-01] 对齐部署（生产实关硬闸门）
    "scoring.calibration_gate_p": 0.55,  # [2026-08-01] 对齐部署
    "scoring.calibration_min_n": 12,

    # ── Phase 1/2/3 重构灰度总开关（docs/cosource_refactor_plan.md 六）──
    # False = 关闭，生产信号链路字节级不变；True = 启用 precision_entry/micro_state
    # 驱动的收敛决策（co_source.apply_v2）。仅灰度暴露，启用前必须经 shadow 验证。
    "co.v2_enabled": False,
    # ── Phase 0/1/2/3 重构可调参数（micro_state / precision_entry 消费）──
    # 灰度关闭时这些值仅经 get_float 代码默认生效；显式暴露使面板/GET 可见、
    # 可经 config_provider.set 持久化热调。键名须与 micro_state.load_config /
    # precision_entry.load_config 读取完全一致。
    "co.v2_shadow_enabled": True,
    "co.v2.weight.align": 0.40,
    "co.v2.weight.structure": 0.40,
    "co.v2.weight.rr": 0.20,
    "co.v2.min_rr": 1.2,
    "co.v2.pullback_atr_min": 0.5,
    "co.v2.pullback_atr_max": 1.5,
    "co.v2.theta.TREND_ACCEL": 0.45,
    "co.v2.theta.TREND_PULLBACK": 0.30,
    "co.v2.theta.TREND_EXHAUST": 0.55,
    "co.v2.theta.RANGE": 0.45,
    "co.v2.theta.REVERSAL": 0.50,
}

# Cooldown endpoint = SUBSET of THRESHOLD_CONFIG_DEFAULTS (the 6 regime
# cooldown-second keys). Single source of truth = THRESHOLD_CONFIG_DEFAULTS,
# so cooldown-second values can never drift from the panel or engine. Dead
# keys (trend_same_dir_*, pretrend/trend/fade_threshold bare, score_threshold)
# are intentionally EXCLUDED — they belong to scoring_engine / registry, not
# the /cooldown endpoint.
_COOLDOWN_KEYS: list[str] = [
    "pretrend_cooldown_seconds",
    "trend_cooldown_seconds",
    "fade_cooldown_seconds",
    "range_boundary_cooldown_seconds",
    "neutral_cooldown_seconds",
    "trend_reverse_cooldown_seconds",
]
COOLDOWN_CONFIG_DEFAULTS: dict[str, Any] = {
    k: THRESHOLD_CONFIG_DEFAULTS[k] for k in _COOLDOWN_KEYS
}
COOLDOWN_CONFIG_KEYS: list[str] = list(COOLDOWN_CONFIG_DEFAULTS.keys())

# Symbol tower config keys and their defaults
SYMBOL_TOWER_FIELDS: dict[str, Any] = {
    "enable": True,
    "lot_size": 0.01,
    "max_positions": 3,
    "stop_loss_pips": 200,
    "take_profit_pips": 800,
    "signal_weight": 1.0,
    "trading_hours": "00:00-23:59",
    "news_filter": True,
}

# Prompt config keys. `user_prompt_template` is the only key actually read
# by the scheduler (via _load_prompt_template → _build_prompt). It supports
# {variable} interpolation: {symbol} {timeframe} {rsi} {macd} {adx} {bbw}
# {pct_b} {stoch_k} {atr} {bar_open} {close} {bar_momentum} {ma_alignment}
# {regime} {regime_strength} {pre_dir} {pre_score} {trend_note} {momentum_note}.
# If missing or broken, falls back to hardcoded compact prompt.
PROMPT_CONFIG_FIELDS: dict[str, Any] = {
    "system_prompt": "XAUUSD M5 quant analyst. Output compact JSON without markdown.",
    "regime_prompt_template": "Regime: TREND(ADX>24+MACD) PRE_TREND(ADX 18-24) FADE(falling) RANGE(BBW<1)",
    "signal_prompt_template": "RSI/MACD/ADX/BBW/ATR/bar_momentum → direction+confidence. TREND:align MACD. RANGE:extreme %b.",
    "risk_prompt_template": "Hard: max_pos>limit→reject. Soft: drawdown>30%→risk=high. Daily loss>limit→kill_switch.",
    "user_prompt_template": (
        "XAUUSD {timeframe} | {regime} r={regime_strength}\n"
        "RSI={rsi} MACD={macd} ADX={adx} %b={pct_b} StochK={stoch_k}\n"
        "ATR14={atr} bar_open={bar_open} close={close} bar_momentum={bar_momentum} (range/ATR)\n"
        "MA={ma_alignment} pre_dir={pre_dir} pre_score={pre_score}\n"
        "{trend_note} {momentum_note}\n"
        'Output JSON: {{"direction":"BUY|SELL|NO_TRADE","confidence":0-1,'
        '"sl_atr_mult":1.5-2.5,"tp_atr_mult":2.0-4.0,"reason":"<50chars","risk":"<30chars"}}'
    ),
    "max_context_length": 4096,
    "include_market_data": True,
    "include_news": True,
    "include_position_info": True,
    "prompt_version": "2.1-template",
}

PROMPT_CONFIG_KEYS: list[str] = list(PROMPT_CONFIG_FIELDS.keys())

# Per-model prompt routing (P2 multi-model). Each active model can override
# its own system_prompt + user_prompt_template; otherwise it inherits the
# global signal_tower.prompt.* keys (scheduler fallback chain).
PROMPT_MODELS: tuple[str, ...] = ("ai_dynamic", "co_source", "manual")
PER_MODEL_PROMPT_FIELDS: tuple[str, ...] = ("system_prompt", "user_prompt_template")

# Watchdog default services to monitor
WATCHDOG_DEFAULT_SERVICES: list[dict[str, str]] = [
    {"name": "collector", "status": "unknown", "last_heartbeat": None},
    {"name": "signal-tower", "status": "unknown", "last_heartbeat": None},
    {"name": "risk-engine", "status": "unknown", "last_heartbeat": None},
]


# ── Pydantic Models ─────────────────────────────

class CooldownConfigItem(BaseModel):
    """A single cooldown config key-value pair."""
    config_key: str = Field(..., description="Cooldown config key (e.g. 'pretrend_cooldown_seconds')")
    value: str = Field(..., description="New value (as string)")


class CooldownBatchUpdate(BaseModel):
    """Batch cooldown config update request."""
    updates: list[CooldownConfigItem] = Field(..., min_items=1, max_items=100)


class CooldownConfigResponse(BaseModel):
    """Cooldown config response model."""
    symbol: str = ""
    config: dict = Field(default_factory=dict)


class CooldownStatusResponse(BaseModel):
    """Cooldown status for a given symbol."""
    symbol: str = ""
    last_signal_at: Optional[str] = None
    seconds_since_last_signal: Optional[int] = None
    remaining_cooldown_seconds: int = 0
    current_regime: str = "NEUTRAL"
    is_cooling_down: bool = False


class SignalTowerModeUpdate(BaseModel):
    """Mode switch request body."""
    mode: str = Field("ai_dynamic", description="'ai_dynamic' or 'manual'")
    manual_regime_score: int = Field(50, ge=0, le=100, description="0-100, only used in manual mode")


class SignalTowerModeResponse(BaseModel):
    """Signal tower mode response (per symbol)."""
    symbol: str = ""
    mode: str = "ai_dynamic"
    ai_dynamic: bool = True
    manual_score: int = 50
    current_regime: Optional[str] = None
    regime_confidence: Optional[float] = None
    ai_score: Optional[int] = None
    last_updated: Optional[str] = None
    indicators: dict = Field(default_factory=dict)


class SymbolTowerConfigItem(BaseModel):
    """Symbol-level tower config for a single field update."""
    symbol: str = Field(..., description="Trading symbol, e.g. 'XAUUSD'")
    enable: Optional[bool] = None
    lot_size: Optional[float] = None
    max_positions: Optional[int] = None
    stop_loss_pips: Optional[int] = None
    take_profit_pips: Optional[int] = None
    signal_weight: Optional[float] = None
    trading_hours: Optional[str] = None
    news_filter: Optional[bool] = None


class SymbolTowerConfigBatchUpdate(BaseModel):
    """Batch update for symbol-level tower configs."""
    symbols: list[SymbolTowerConfigItem] = Field(..., min_items=1, max_items=200)


class PromptConfigResponse(BaseModel):
    """Prompt configuration response model."""
    system_prompt: str = ""
    regime_prompt_template: str = ""
    signal_prompt_template: str = ""
    risk_prompt_template: str = ""
    user_prompt_template: str = ""
    max_context_length: int = 4096
    include_market_data: bool = True
    include_news: bool = True
    include_position_info: bool = True
    prompt_version: str = "1.0"


class PromptConfigUpdate(BaseModel):
    """Prompt configuration update request."""
    system_prompt: Optional[str] = None
    regime_prompt_template: Optional[str] = None
    signal_prompt_template: Optional[str] = None
    risk_prompt_template: Optional[str] = None
    user_prompt_template: Optional[str] = None
    max_context_length: Optional[int] = None
    include_market_data: Optional[bool] = None
    include_news: Optional[bool] = None
    include_position_info: Optional[bool] = None
    prompt_version: Optional[str] = None


class ModelPromptUpdate(BaseModel):
    """Per-model prompt override update (P2 multi-model).

    Only non-None fields are persisted to signal_tower.prompt.<model>.* keys.
    """
    system_prompt: Optional[str] = None
    user_prompt_template: Optional[str] = None


class WatchdogServiceInfo(BaseModel):
    """Individual service status in watchdog."""
    name: str = ""
    status: str = "unknown"
    last_heartbeat: Optional[str] = None


class WatchdogAlertInfo(BaseModel):
    """Watchdog alert entry."""
    level: str = "info"
    message: str = ""
    time: Optional[str] = None


class WatchdogInfoResponse(BaseModel):
    """Watchdog information response."""
    status: str = "running"
    uptime_seconds: int = 0
    last_check: Optional[str] = None
    watched_services: list[WatchdogServiceInfo] = Field(default_factory=list)
    restarts: int = 0
    alerts: list[WatchdogAlertInfo] = Field(default_factory=list)


# ── Helpers ─────────────────────────────────────

def _coerce_value(raw: Any, default: Any, target_type: type = str) -> Any:
    """Coerce a raw config value to the target type, falling back to default."""
    if raw is None or raw == "":
        return default
    try:
        if target_type is int:
            return int(raw)
        if target_type is float:
            return float(raw)
        if target_type is bool:
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("true", "1", "yes", "on")
        return str(raw)
    except (ValueError, TypeError):
        return default


def _parse_cooldown_value(key: str, raw: Any) -> Any:
    """Parse a cooldown config value to its proper type."""
    default = COOLDOWN_CONFIG_DEFAULTS.get(key)
    if default is None:
        return str(raw) if raw else ""
    if isinstance(default, bool):
        return _coerce_value(raw, default, bool)
    if isinstance(default, int):
        return _coerce_value(raw, default, int)
    if isinstance(default, float):
        return _coerce_value(raw, default, float)
    return str(raw) if raw else str(default)


# ── Router Factory ──────────────────────────────

def create_signal_tower_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with signal tower endpoints.

    Args:
        db_pool: DatabasePool instance (asyncpg wrapper).
        config_provider: ConfigProviderV3 instance.
        auth_handler: AuthHandler instance for RBAC authentication.

    Returns:
        APIRouter with /api/v1/signal-tower routes and legacy /api/signal-tower aliases.
    """
    router = APIRouter(tags=["signal-tower"])

    # ═════════════════════════════════════════════
    # Shared handler implementations
    # ═════════════════════════════════════════════

    async def _read_cooldown_from_db(symbol: Optional[str] = None) -> dict:
        """Read cooldown config via ConfigProviderV3 (unified GET path).

        Reads the 8 global cooldown keys with per-symbol override resolution
        (symbol.{SYMBOL}.{key} → {key}) via get_with_resolution.
        """
        config: dict[str, Any] = dict(COOLDOWN_CONFIG_DEFAULTS)

        if config_provider is None:
            return config

        for key in COOLDOWN_CONFIG_KEYS:
            try:
                if symbol:
                    val = await config_provider.get_with_resolution(key, symbol=symbol)
                else:
                    val = await config_provider.get(key)
                if val:
                    config[key] = _parse_cooldown_value(key, val)
            except Exception as exc:
                logger.warning("Cooldown config read failed for %s: %s", key, exc)

        return config

    async def _get_cooldown_impl(symbol: Optional[str]) -> dict:
        """Handler: GET cooldown configuration from config_provider (same source as PUT)."""
        config: dict[str, Any] = dict(COOLDOWN_CONFIG_DEFAULTS)
        if config_provider is not None:
            for key in COOLDOWN_CONFIG_KEYS:
                try:
                    val = await config_provider.get(key)
                    if val is not None:
                        config[key] = _parse_cooldown_value(key, val)
                except Exception:
                    pass
        return {
            "code": 0,
            "data": {
                "symbol": symbol or "*",
                "config": config,
            },
            "message": "ok",
        }

    # ── 自动重训守护报表（LightGBM 运行状况 + DeepSeek 裁判总结）─────────────
    @router.get("/api/v1/signal-tower/retrain/summary")
    async def _get_retrain_summary_impl(request: Request,
                                        user=Depends(auth_handler.require_auth)):
        """Handler: GET 自动重训守护的运行状况与 DeepSeek 裁判总结。

        数据真源（零 schema 改动，只读）：
        - hcm:ai:retrain:daemon   守护存活心跳（auto_retrain.main 每轮刷新，TTL 48h）
        - hcm:ai:retrain:last     最新一轮重训 JSON
        - hcm:ai:retrain:history  近 50 轮列表
        - ai.lm.model_path/calib_path  当前线上模型版本（config_provider，与切换双写一致）
        """
        r = _retrain_redis()

        # 1) 守护存活
        daemon_online = False
        daemon_info = None
        last_run_at = None
        if r is not None:
            try:
                blob = r.get("hcm:ai:retrain:daemon")
                if blob:
                    daemon_info = json.loads(blob)
                    daemon_online = True
            except Exception as e:
                logger.warning("retrain daemon read failed: %s", e)

        # 2) 当前线上模型版本
        current_model = None
        current_calib = None
        if config_provider is not None:
            try:
                current_model = await config_provider.get("ai.lm.model_path")
                current_calib = await config_provider.get("ai.lm.calib_path")
            except Exception as e:
                logger.warning("retrain model_path read failed: %s", e)

        # 3) 最新一轮 + 历史
        last_run = None
        history = []
        if r is not None:
            try:
                last_blob = r.get("hcm:ai:retrain:last")
                if last_blob:
                    last_run = json.loads(last_blob)
                hist_blobs = r.lrange("hcm:ai:retrain:history", 0, 49)
                for hb in hist_blobs:
                    try:
                        history.append(json.loads(hb))
                    except Exception:
                        continue
            except Exception as e:
                logger.warning("retrain history read failed: %s", e)

        if last_run and last_run.get("at"):
            last_run_at = last_run.get("at")

        # 4) 汇总 KPI
        total = len(history)
        adopt_cnt = sum(1 for h in history if h.get("adopted"))
        rollback_cnt = sum(
            1 for h in history
            if (h.get("judge") or {}).get("decision") == "rollback"
        )
        fallback_cnt = sum(
            1 for h in history
            if (h.get("judge") or {}).get("decision") == "local_fallback"
        )
        auc_trend = [h.get("auc") for h in history if h.get("auc") is not None]

        data = {
            "daemon_online": daemon_online,
            "daemon_info": daemon_info,
            "current_model": current_model,
            "current_calib": current_calib,
            "last_run": last_run,
            "last_run_at": last_run_at,
            "history": history,
            "summary_kpis": {
                "total_rounds": total,
                "adopt_count": adopt_cnt,
                "rollback_count": rollback_cnt,
                "local_fallback_count": fallback_cnt,
                "latest_auc": (auc_trend[0] if auc_trend else None),
                "auc_trend": auc_trend,
            },
        }
        return {"code": 0, "data": data, "message": "ok"}

    async def _get_threshold_impl(symbol: Optional[str]) -> dict:
        """Handler: GET full Threshold panel config (cooldown + scoring + midbar + dispute).

        Fix for "保存刷新复原": the legacy GET alias previously delegated to
        _get_cooldown_impl, which only iterated COOLDOWN_CONFIG_KEYS — so the
        midbar / scoring-threshold / dispute fields were NEVER returned to the
        frontend. The panel therefore fell back to hardcoded defaults on every
        refresh, making any save look like it "didn't stick". This returns ALL
        Threshold.tsx fields, reading each from config_provider (PG source of
        truth) and falling back to the matching frontend default when unset.
        """
        config: dict[str, Any] = {}
        if config_provider is not None:
            for key, default in THRESHOLD_CONFIG_DEFAULTS.items():
                try:
                    val = await config_provider.get(key)
                    config[key] = val if val is not None else default
                except Exception:
                    config[key] = default
        else:
            # Config provider unavailable — return hard-coded defaults so the
            # panel still renders something sensible.
            config = dict(THRESHOLD_CONFIG_DEFAULTS)
        return {
            "code": 0,
            "data": {
                "symbol": symbol or "*",
                "config": config,
            },
            "message": "ok",
        }

    async def _put_cooldown_impl(body: CooldownBatchUpdate) -> dict:
        """Handler: PUT cooldown configuration (batch)."""
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }

        results: list[dict] = []
        success_count = 0
        fail_count = 0

        for item in body.updates:
            try:
                ok = await config_provider.set(item.config_key, item.value)
                if ok:
                    success_count += 1
                    results.append({"config_key": item.config_key, "status": "ok"})
                else:
                    fail_count += 1
                    results.append({"config_key": item.config_key, "status": "failed"})
            except Exception as exc:
                fail_count += 1
                results.append({
                    "config_key": item.config_key,
                    "status": "error",
                    "error": str(exc),
                })

        logger.info(
            "Cooldown config batch update: %d/%d succeeded",
            success_count, len(body.updates),
        )

        return {
            "code": 0,
            "data": {
                "results": results,
                "success": success_count,
                "failed": fail_count,
                "total": len(body.updates),
            },
            "message": f"Batch update: {success_count}/{len(body.updates)} succeeded",
        }

    async def _get_cooldown_status_impl(symbol: str) -> dict:
        """Handler: GET current cooldown status for a symbol.

        Queries hcm_signal.signals for the latest signal's created_at,
        then calculates remaining cooldown time based on the current regime.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            # ── 1. Get latest signal for this symbol ──
            signal_row = await db_pool.fetchrow(
                """SELECT signal_id, symbol, created_at
                   FROM hcm_signal.signals
                   WHERE symbol = $1
                   ORDER BY created_at DESC
                   LIMIT 1""",
                symbol,
            )

            last_signal_at: Optional[str] = None
            seconds_since_last_signal: Optional[int] = None
            remaining_cooldown: int = 0
            is_cooling: bool = False

            if signal_row and signal_row["created_at"]:
                created: datetime = signal_row["created_at"]
                # Ensure timezone-aware
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                delta = (now - created).total_seconds()
                seconds_since_last_signal = max(0, int(delta))
                last_signal_at = created.isoformat()

            # ── 2. Determine current regime (via config_provider) ──
            regime = "NEUTRAL"
            if config_provider is not None:
                regime_raw = await config_provider.get("signal_tower.current_regime")
                if regime_raw:
                    regime = regime_raw.upper()

            # ── 3. Read cooldown config to determine remaining time ──
            cooldown_config = await _read_cooldown_from_db(symbol)

            # Map regime to cooldown key
            regime_to_key: dict[str, str] = {
                "PRETREND": "pretrend_cooldown_seconds",
                "TREND": "trend_cooldown_seconds",
                "FADE": "fade_cooldown_seconds",
                "RANGE_BOUNDARY": "range_boundary_cooldown_seconds",
                "NEUTRAL": "neutral_cooldown_seconds",
            }
            cooldown_key = regime_to_key.get(regime, "neutral_cooldown_seconds")
            cooldown_seconds = int(cooldown_config.get(cooldown_key, 300))

            if seconds_since_last_signal is not None:
                remaining_cooldown = max(0, cooldown_seconds - seconds_since_last_signal)
                is_cooling = remaining_cooldown > 0

            return {
                "code": 0,
                "data": {
                    "symbol": symbol,
                    "last_signal_at": last_signal_at,
                    "seconds_since_last_signal": seconds_since_last_signal,
                    "remaining_cooldown_seconds": remaining_cooldown,
                    "current_regime": regime,
                    "is_cooling_down": is_cooling,
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Cooldown status query failed for symbol=%s: %s", symbol, exc)
            return {"code": "RISK_001", "data": None, "message": str(exc)}

    async def _get_mode_impl(symbol: str) -> dict:
        """Handler: GET signal tower mode for a symbol (via config_provider)."""
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}

        try:
            mode = (await config_provider.get("signal_tower.mode")) or "ai_dynamic"

            manual_score_raw = await config_provider.get("signal_tower.manual_regime_score")
            manual_score = 50
            if manual_score_raw is not None:
                try:
                    manual_score = int(manual_score_raw)
                except (ValueError, TypeError):
                    manual_score = 50

            regime_raw = await config_provider.get("signal_tower.current_regime")
            current_regime = regime_raw.upper() if regime_raw else None

            conf_raw = await config_provider.get("signal_tower.regime_confidence")
            regime_confidence = None
            if conf_raw is not None:
                try:
                    regime_confidence = float(conf_raw)
                except (ValueError, TypeError):
                    regime_confidence = None

            ai_raw = await config_provider.get("signal_tower.ai_score")
            ai_score = None
            if ai_raw is not None:
                try:
                    ai_score = int(ai_raw)
                except (ValueError, TypeError):
                    ai_score = None

            last_updated = await config_provider.get("signal_tower.last_updated")

            return {
                "code": 0,
                "data": {
                    "symbol": symbol,
                    "mode": mode,
                    "ai_dynamic": mode == "ai_dynamic",
                    "manual_score": manual_score,
                    "current_regime": current_regime,
                    "regime_confidence": regime_confidence,
                    "ai_score": ai_score,
                    "last_updated": last_updated,
                    "indicators": {},
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Signal tower mode query failed for symbol=%s: %s", symbol, exc)
            return {"code": "RISK_001", "data": None, "message": str(exc)}

    async def _put_mode_impl(body: SignalTowerModeUpdate) -> dict:
        """Handler: PUT signal tower mode."""
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }

        errors: list[str] = []

        try:
            await config_provider.set("signal_tower.mode", body.mode)
        except Exception as exc:
            errors.append(f"mode: {exc}")

        try:
            await config_provider.set(
                "signal_tower.manual_regime_score", str(body.manual_regime_score),
            )
        except Exception as exc:
            errors.append(f"manual_regime_score: {exc}")

        # Update last_updated timestamp
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            await config_provider.set("signal_tower.last_updated", now_iso)
        except Exception:
            pass

        logger.info(
            "Signal tower mode updated: mode=%s, manual_score=%d",
            body.mode, body.manual_regime_score,
        )

        return {
            "code": 0,
            "data": {
                "mode": body.mode,
                "ai_dynamic": body.mode == "ai_dynamic",
                "manual_regime_score": body.manual_regime_score,
                "errors": errors if errors else None,
            },
            "message": "Mode updated" if not errors else f"Mode updated with {len(errors)} error(s)",
        }

    async def _get_symbol_config_impl(symbol: Optional[str] = None) -> dict:
        """Handler: GET symbol-level tower config (list).

        If symbol is provided, returns only that symbol's config.
        Otherwise, returns all symbols with tower config.
        """
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}

        try:
            # Discover symbols: query for symbol.%.tower.enable to find all configured symbols
            if symbol:
                # Build single-symbol config from its fields (unified config_provider path)
                fields = SYMBOL_TOWER_FIELDS
                config: dict[str, Any] = {"symbol": symbol}
                for field_name, default_val in fields.items():
                    try:
                        val = await config_provider.get(f"symbol.{symbol}.tower.{field_name}")
                        if val is not None:
                            config[field_name] = _coerce_value(val, default_val, type(default_val))
                        else:
                            config[field_name] = default_val
                    except Exception:
                        config[field_name] = default_val
                return {
                    "code": 0,
                    "data": {"items": [config], "total": 1},
                    "message": "ok",
                }

            # Discover all symbols with tower config (unified config_provider path)
            rows = await config_provider.get_keys_by_prefix("symbol.")

            symbol_map: dict[str, dict[str, Any]] = {}
            for key, val in rows.items():
                # Only process symbol-level tower configs
                if ".tower." not in key:
                    continue
                # Parse: "symbol.XAUUSD.tower.enable" → symbol="XAUUSD", field="enable"
                parts = key.split(".", 3)
                if len(parts) < 4:
                    continue
                sym = parts[1]
                field = parts[3]

                if sym not in symbol_map:
                    symbol_map[sym] = {"symbol": sym}
                    # Initialize all fields with defaults
                    for fname, fdefault in SYMBOL_TOWER_FIELDS.items():
                        symbol_map[sym][fname] = fdefault

                symbol_map[sym][field] = _coerce_value(
                    val,
                    SYMBOL_TOWER_FIELDS.get(field, ""),
                    type(SYMBOL_TOWER_FIELDS.get(field, "")),
                )

            items = sorted(symbol_map.values(), key=lambda x: x["symbol"])

            return {
                "code": 0,
                "data": {"items": items, "total": len(items)},
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Symbol tower config query failed: %s", exc)
            return {"code": "RISK_001", "data": None, "message": str(exc)}

    async def _put_symbol_config_impl(body: SymbolTowerConfigBatchUpdate) -> dict:
        """Handler: PUT batch update symbol-level tower configs."""
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }

        results: list[dict] = []
        success_count = 0
        fail_count = 0

        field_map = {
            "enable": "enable",
            "lot_size": "lot_size",
            "max_positions": "max_positions",
            "stop_loss_pips": "stop_loss_pips",
            "take_profit_pips": "take_profit_pips",
            "signal_weight": "signal_weight",
            "trading_hours": "trading_hours",
            "news_filter": "news_filter",
        }

        for item in body.symbols:
            sym = item.symbol
            updates_for_symbol: list[str] = []

            for field_attr, config_suffix in field_map.items():
                val = getattr(item, field_attr, None)
                if val is not None:
                    config_key = f"symbol.{sym}.tower.{config_suffix}"
                    try:
                        # Convert to string for config_provider
                        str_val = str(val).lower() if isinstance(val, bool) else str(val)
                        ok = await config_provider.set(config_key, str_val)
                        if ok:
                            updates_for_symbol.append(config_suffix)
                        else:
                            fail_count += 1
                            results.append({
                                "symbol": sym,
                                "field": config_suffix,
                                "status": "failed",
                            })
                    except Exception as exc:
                        fail_count += 1
                        results.append({
                            "symbol": sym,
                            "field": config_suffix,
                            "status": "error",
                            "error": str(exc),
                        })

            if updates_for_symbol:
                success_count += len(updates_for_symbol)
                results.append({
                    "symbol": sym,
                    "updated": updates_for_symbol,
                    "status": "ok",
                })

        logger.info(
            "Symbol tower config batch update: %d fields succeeded, %d failed",
            success_count, fail_count,
        )

        return {
            "code": 0,
            "data": {
                "results": results,
                "success": success_count,
                "failed": fail_count,
                "total": success_count + fail_count,
            },
            "message": f"Batch update: {success_count} succeeded, {fail_count} failed",
        }

    async def _get_prompt_impl() -> dict:
        """Handler: GET prompt configuration (via config_provider).

        Reads all prompt config fields from signal_tower.prompt.* keys.
        Falls back to defaults when config_provider is unavailable.
        """
        config: dict[str, Any] = dict(PROMPT_CONFIG_FIELDS)

        if config_provider is not None:
            for base_key in PROMPT_CONFIG_KEYS:
                full_key = f"signal_tower.prompt.{base_key}"
                try:
                    val = await config_provider.get(full_key)
                    if val is not None:
                        default = PROMPT_CONFIG_FIELDS[base_key]
                        config[base_key] = _coerce_value(val, default, type(default))
                except Exception as exc:
                    logger.warning("Prompt config read failed for %s: %s", full_key, exc)

        return {
            "code": 0,
            "data": config,
            "message": "ok",
        }

    async def _put_prompt_impl(body: PromptConfigUpdate) -> dict:
        """Handler: PUT prompt configuration.

        Saves each non-None field to signal_tower.prompt.* config keys.
        """
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }

        results: list[dict] = []
        success_count = 0
        fail_count = 0

        field_names = list(PROMPT_CONFIG_FIELDS.keys())
        for field_name in field_names:
            val = getattr(body, field_name, None)
            if val is None:
                continue
            config_key = f"signal_tower.prompt.{field_name}"
            try:
                # Convert to string for config_provider
                str_val = str(val).lower() if isinstance(val, bool) else str(val)
                ok = await config_provider.set(config_key, str_val)
                if ok:
                    success_count += 1
                    results.append({"field": field_name, "status": "ok"})
                else:
                    fail_count += 1
                    results.append({"field": field_name, "status": "failed"})
            except Exception as exc:
                fail_count += 1
                results.append({
                    "field": field_name,
                    "status": "error",
                    "error": str(exc),
                })

        logger.info(
            "Prompt config update: %d/%d fields succeeded",
            success_count, success_count + fail_count,
        )

        return {
            "code": 0,
            "data": {
                "results": results,
                "success": success_count,
                "failed": fail_count,
            },
            "message": f"Prompt config updated: {success_count} succeeded, {fail_count} failed",
        }

    async def _get_model_prompt_impl(model: str) -> dict:
        """Handler: GET a single model's prompt override.

        Returns the model-specific system_prompt + user_prompt_template if set,
        otherwise the global signal_tower.prompt.* values (inherits_global=True),
        mirroring the scheduler's fallback chain.
        """
        if model not in PROMPT_MODELS:
            return {
                "code": "INVALID_MODEL",
                "data": None,
                "message": f"model must be one of {PROMPT_MODELS}",
            }
        result: dict[str, Any] = {
            "model": model,
            "system_prompt": "",
            "user_prompt_template": "",
            "inherits_global": True,
        }
        if config_provider is not None:
            sys_m = await config_provider.get(f"signal_tower.prompt.{model}.system_prompt")
            usr_m = await config_provider.get(f"signal_tower.prompt.{model}.user_prompt_template")
            sys_g = await config_provider.get("signal_tower.prompt.system_prompt")
            usr_g = await config_provider.get("signal_tower.prompt.user_prompt_template")
            result["system_prompt"] = (sys_m or sys_g or "")
            result["user_prompt_template"] = (usr_m or usr_g or "")
            result["inherits_global"] = (not sys_m) and (not usr_m)
        return {"code": 0, "data": result, "message": "ok"}

    async def _get_all_model_prompts_impl() -> dict:
        """Handler: GET all models' prompt overrides in one call."""
        models: dict[str, Any] = {}
        for m in PROMPT_MODELS:
            models[m] = (await _get_model_prompt_impl(m))["data"]
        return {"code": 0, "data": models, "message": "ok"}

    async def _put_model_prompt_impl(model: str, body: ModelPromptUpdate) -> dict:
        """Handler: PUT a single model's prompt override.

        Persists non-None fields to signal_tower.prompt.<model>.<field>.
        """
        if model not in PROMPT_MODELS:
            return {
                "code": "INVALID_MODEL",
                "data": None,
                "message": f"model must be one of {PROMPT_MODELS}",
            }
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }

        results: list[dict] = []
        success_count = 0
        fail_count = 0
        for field_name in PER_MODEL_PROMPT_FIELDS:
            val = getattr(body, field_name, None)
            if val is None:
                continue
            config_key = f"signal_tower.prompt.{model}.{field_name}"
            try:
                ok = await config_provider.set(config_key, str(val))
                if ok:
                    success_count += 1
                    results.append({"field": field_name, "status": "ok"})
                else:
                    fail_count += 1
                    results.append({"field": field_name, "status": "failed"})
            except Exception as exc:
                fail_count += 1
                results.append({
                    "field": field_name,
                    "status": "error",
                    "error": str(exc),
                })

        logger.info(
            "Model prompt update (%s): %d/%d fields succeeded",
            model, success_count, success_count + fail_count,
        )
        return {
            "code": 0,
            "data": {
                "model": model,
                "results": results,
                "success": success_count,
                "failed": fail_count,
            },
            "message": f"Model prompt updated ({model}): {success_count} succeeded, {fail_count} failed",
        }

    async def _get_watchdog_impl() -> dict:
        """Handler: GET watchdog status.

        Returns WatchdogInfo with service heartbeats from DB when available,
        falling back to default status when DB is unavailable.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        services: list[dict] = []
        restarts: int = 0
        uptime_seconds: int = 0
        status: str = "running"
        alerts: list[dict] = []

        if config_provider is not None:
            try:
                # Read watchdog service heartbeats from config (unified path)
                service_rows = await config_provider.get_keys_by_prefix("watchdog.service.")
                service_map: dict[str, dict] = {}
                for key, val in service_rows.items():
                    # Parse: "watchdog.service.collector" → "collector"
                    # or "watchdog.service.collector.status" → service="collector", attr="status"
                    parts = key.split(".")
                    if len(parts) < 3:
                        continue
                    svc_name = parts[2]
                    attr = parts[3] if len(parts) >= 4 else "status"
                    if svc_name not in service_map:
                        service_map[svc_name] = {
                            "name": svc_name,
                            "status": "unknown",
                            "last_heartbeat": None,
                        }
                    service_map[svc_name][attr] = val

                services = list(service_map.values())

                # Read watchdog global status
                watchdog_status = await config_provider.get("watchdog.status")
                if watchdog_status:
                    status = watchdog_status

                # Read uptime
                started_raw = await config_provider.get("watchdog.started_at")
                if started_raw:
                    try:
                        started = datetime.fromisoformat(started_raw)
                        if started.tzinfo is None:
                            started = started.replace(tzinfo=timezone.utc)
                        uptime_seconds = max(
                            0,
                            int((datetime.now(timezone.utc) - started).total_seconds()),
                        )
                    except (ValueError, TypeError):
                        pass

                # Read restarts count
                restarts_raw = await config_provider.get("watchdog.restarts")
                if restarts_raw:
                    try:
                        restarts = int(restarts_raw)
                    except (ValueError, TypeError):
                        pass

                # Read recent alerts (sorted by config_key DESC, latest first)
                alert_items = sorted(
                    (await config_provider.get_keys_by_prefix("watchdog.alert.")).items(),
                    key=lambda kv: kv[0], reverse=True,
                )[:20]
                for _akey, araw in alert_items:
                    try:
                        alert = json.loads(araw)
                        alerts.append(alert)
                    except Exception:
                        pass

            except Exception as exc:
                logger.warning("Watchdog status read failed: %s", exc)

        # Fallback defaults
        if not services:
            services = [dict(s) for s in WATCHDOG_DEFAULT_SERVICES]

        if not alerts:
            alerts = [{
                "level": "info",
                "message": "System started",
                "time": now_iso,
            }]

        return {
            "code": 0,
            "data": {
                "status": status,
                "uptime_seconds": uptime_seconds,
                "last_check": now_iso,
                "watched_services": services,
                "restarts": restarts,
                "alerts": alerts,
            },
            "message": "ok",
        }

    async def _post_watchdog_action_impl(action: str) -> dict:
        """Handler: POST watchdog action (pause/resume/restart).

        Updates watchdog.status config key and returns the new state.
        """
        valid_actions = {"pause", "resume", "restart"}
        if action not in valid_actions:
            return {
                "code": "INVALID_ACTION",
                "data": None,
                "message": f"Invalid action '{action}'. Valid actions: {', '.join(sorted(valid_actions))}",
            }

        new_status: str = "running"
        if action == "pause":
            new_status = "paused"
        elif action == "resume":
            new_status = "running"
        elif action == "restart":
            new_status = "running"
            # Reset uptime
            if config_provider is not None:
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    await config_provider.set("watchdog.started_at", now_iso)
                except Exception:
                    pass

        # Persist status
        if config_provider is not None:
            try:
                await config_provider.set("watchdog.status", new_status)
            except Exception as exc:
                logger.error("Failed to persist watchdog status: %s", exc)
                return {
                    "code": "CONFIG_WRITE_FAILED",
                    "data": {"action": action, "status": new_status},
                    "message": f"Action accepted but status persistence failed: {exc}",
                }

        logger.info("Watchdog action '%s' executed, new status: %s", action, new_status)

        return {
            "code": 0,
            "data": {
                "action": action,
                "status": new_status,
                "message": f"Watchdog {action} completed successfully",
            },
            "message": "ok",
        }

    # ═════════════════════════════════════════════
    # API v1 routes
    # ═════════════════════════════════════════════

    # ── Cooldown Config ─────────────────────────

    @router.get("/api/v1/signal-tower/cooldown")
    async def get_cooldown(
        request: Request,
        symbol: Optional[str] = Query(None, description="Filter by symbol for per-symbol overrides"),
        user=Depends(auth_handler.require_auth),
    ):
        """Get cooldown configuration.

        Returns the 8 cooldown fields (pretrend_cooldown_seconds, etc.)
        with global defaults and optional per-symbol overrides.
        """
        return await _get_cooldown_impl(symbol)

    @router.put("/api/v1/signal-tower/cooldown")
    async def put_cooldown(
        body: CooldownBatchUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Batch update cooldown configuration.

        Accepts a list of {config_key, value} pairs for the 8 cooldown fields.
        """
        return await _put_cooldown_impl(body)

    # ── Cooldown Status ─────────────────────────

    @router.get("/api/v1/signal-tower/cooldown/status")
    async def get_cooldown_status(
        request: Request,
        symbol: str = Query(..., description="Trading symbol (required)"),
        user=Depends(auth_handler.require_auth),
    ):
        """Get current cooldown status for a symbol.

        Queries the latest signal from hcm_signal.signals and calculates
        remaining cooldown time based on the current market regime.
        """
        return await _get_cooldown_status_impl(symbol)

    # ── Mode ────────────────────────────────────

    @router.get("/api/v1/signal-tower/mode")
    async def get_mode(
        request: Request,
        symbol: str = Query(..., description="Trading symbol (required)"),
        user=Depends(auth_handler.require_auth),
    ):
        """Get current signal tower mode (AI dynamic / manual).

        Returns mode, ai_dynamic flag, manual_regime_score, current regime,
        and optional AI indicators.
        """
        return await _get_mode_impl(symbol)

    @router.put("/api/v1/signal-tower/mode")
    async def put_mode(
        body: SignalTowerModeUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Switch signal tower mode.

        Set mode to 'ai_dynamic' (AI-driven regime detection) or 'manual'
        (fixed regime_score set by user).
        """
        return await _put_mode_impl(body)

    # ── Symbol Config ───────────────────────────

    @router.get("/api/v1/signal-tower/symbol-config")
    async def get_symbol_config(
        request: Request,
        symbol: Optional[str] = Query(None, description="Filter by symbol (optional)"),
        user=Depends(auth_handler.require_auth),
    ):
        """Get symbol-level signal tower configuration.

        Returns tower config for all symbols (or a specific symbol if provided).
        Each entry includes enable, lot_size, max_positions, stop_loss_pips,
        take_profit_pips, signal_weight, trading_hours, and news_filter.
        """
        return await _get_symbol_config_impl(symbol)

    @router.put("/api/v1/signal-tower/symbol-config")
    async def put_symbol_config(
        body: SymbolTowerConfigBatchUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Batch update symbol-level signal tower configuration.

        Accepts a list of symbol configs; each may specify any subset of the
        8 tower fields (enable, lot_size, max_positions, etc.).
        """
        return await _put_symbol_config_impl(body)

    # ── Prompt Config ───────────────────────────

    @router.get("/api/v1/signal-tower/prompt")
    async def get_prompt(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get prompt configuration.

        Returns all 9 prompt config fields (system_prompt, regime_prompt_template,
        signal_prompt_template, risk_prompt_template, max_context_length,
        include_market_data, include_news, include_position_info, prompt_version).
        """
        return await _get_prompt_impl()

    @router.put("/api/v1/signal-tower/prompt")
    async def put_prompt(
        body: PromptConfigUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Update prompt configuration.

        Accepts any subset of the 9 prompt config fields. Only non-None fields
        are persisted; omitted fields keep their current values.
        """
        return await _put_prompt_impl(body)

    # ── Per-model Prompt (P2 multi-model) ───────
    # NOTE: declare /prompt/all BEFORE /prompt/{model} so FastAPI does not
    # capture "/all" as a model name.

    @router.get("/api/v1/signal-tower/prompt/all")
    async def get_all_model_prompts(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get prompt overrides for all models in one call."""
        return await _get_all_model_prompts_impl()

    @router.get("/api/v1/signal-tower/prompt/{model}")
    async def get_model_prompt(
        model: str,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get a single model's prompt override (system_prompt + user_prompt_template)."""
        return await _get_model_prompt_impl(model)

    @router.put("/api/v1/signal-tower/prompt/{model}")
    async def put_model_prompt(
        model: str,
        body: ModelPromptUpdate,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Update a single model's prompt override.

        Body: {system_prompt?, user_prompt_template?}. Only non-None fields saved.
        """
        return await _put_model_prompt_impl(model, body)

    # ── Watchdog ────────────────────────────────

    @router.get("/api/v1/signal-tower/watchdog")
    async def get_watchdog(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get watchdog status and monitored services.

        Returns uptime, service heartbeats, restart count, and recent alerts.
        """
        return await _get_watchdog_impl()

    @router.post("/api/v1/signal-tower/watchdog/{action}")
    async def post_watchdog_action(
        action: str,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Execute a watchdog action: pause, resume, or restart.

        - pause: Pauses watchdog monitoring
        - resume: Resumes watchdog monitoring
        - restart: Restarts watchdog (resets uptime counter)
        """
        return await _post_watchdog_action_impl(action)

    # ═════════════════════════════════════════════
    # Legacy backward-compatible routes
    # ═════════════════════════════════════════════

    @router.get("/api/signal-tower/threshold")
    async def legacy_get_threshold(
        request: Request,
        symbol: Optional[str] = Query(None, description="Filter by symbol"),
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get full Threshold panel config (cooldown + scoring + midbar + dispute)."""
        return await _get_threshold_impl(symbol)

    @router.put("/api/signal-tower/threshold")
    async def legacy_put_threshold(
        body: CooldownBatchUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Update cooldown/threshold config — alias for /api/v1/signal-tower/cooldown."""
        return await _put_cooldown_impl(body)

    @router.get("/api/signal-tower/mode")
    async def legacy_get_mode(
        request: Request,
        symbol: str = Query(..., description="Trading symbol (required)"),
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get signal tower mode — alias for /api/v1/signal-tower/mode."""
        return await _get_mode_impl(symbol)

    @router.put("/api/signal-tower/mode")
    async def legacy_put_mode(
        body: SignalTowerModeUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Switch signal tower mode — alias for /api/v1/signal-tower/mode."""
        return await _put_mode_impl(body)

    @router.get("/api/signal-tower/symbol-config")
    async def legacy_get_symbol_config(
        request: Request,
        symbol: Optional[str] = Query(None, description="Filter by symbol"),
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get symbol tower config — alias for /api/v1/signal-tower/symbol-config."""
        return await _get_symbol_config_impl(symbol)

    @router.put("/api/signal-tower/symbol-config")
    async def legacy_put_symbol_config(
        body: SymbolTowerConfigBatchUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Update symbol tower config — alias for /api/v1/signal-tower/symbol-config."""
        return await _put_symbol_config_impl(body)

    @router.get("/api/signal-tower/prompt")
    async def legacy_get_prompt(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get prompt config — alias for /api/v1/signal-tower/prompt."""
        return await _get_prompt_impl()

    @router.put("/api/signal-tower/prompt")
    async def legacy_put_prompt(
        body: PromptConfigUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Update prompt config — alias for /api/v1/signal-tower/prompt."""
        return await _put_prompt_impl(body)

    @router.get("/api/signal-tower/watchdog")
    async def legacy_get_watchdog(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get watchdog status — alias for /api/v1/signal-tower/watchdog."""
        return await _get_watchdog_impl()

    @router.post("/api/signal-tower/watchdog/{action}")
    async def legacy_post_watchdog_action(
        action: str,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Execute watchdog action — alias for /api/v1/signal-tower/watchdog/{action}."""
        return await _post_watchdog_action_impl(action)

    # ── Signal Funnel (why-no-trade diagnostics) ──────────

    async def _read_funnel_thresholds() -> dict:
        """Read live gate thresholds from ConfigProvider (PG + Redis)."""
        # [2026-08-01] 漏斗诊断回退默认对齐部署真值（与 THRESHOLD_CONFIG_DEFAULTS / Redis 一致）
        defaults: dict[str, Any] = {
            "co.gate.strong.trend": 40.0,
            "co.gate.weak.trend": 50.0,
            "co.gate.adx_strong": 18.0,
            "co.gate.direction_min_score": 0.30,
            "co.gate.range.block": False,
            "scoring.min_adx_for_trade": 18.0,
            "scoring.trend_strong_adx_threshold": 18.0,
            "scoring.trend_reverse_suppress_factor": 0.40,
            "scoring.min_score_threshold": 0.15,
        }
        out: dict[str, Any] = {}
        for k, d in defaults.items():
            try:
                v = await config_provider.get(k) if config_provider is not None else None
                if v is None:
                    out[k] = d
                elif isinstance(d, bool):
                    out[k] = str(v).strip().lower() in ("true", "1", "yes")
                elif isinstance(d, str):
                    # 字母档位（如 hexp.min_grade 的 'A'/'B'/'C'）原样保留字符串，禁止 float() 否则会抛异常落入默认档
                    out[k] = str(v).strip()
                else:
                    out[k] = float(v)
            except Exception:
                out[k] = d
        try:
            out["strong_score"] = round(float(out["co.gate.strong.trend"]) / 100.0, 3)
            out["weak_score"] = round(float(out["co.gate.weak.trend"]) / 100.0, 3)
        except Exception:
            out["strong_score"] = 0.40
            out["weak_score"] = 0.50
        return out

    async def _detect_active_model_for_funnel() -> str:
        """复刻 scheduler._detect_active_model，确定当前 LIVE 主路径引擎。

        返回 'hexp' / 'co_source' / 'manual'。signal_tower.py 与 scheduler.py
        同进程但不同容器，无法 import，故就地复刻判定逻辑（优先级一致）：
          manual  ← signal_tower.mode == 'manual'
          hexp    ← signal.active_model == 'hexp'
          co_source ← 其它（含 'co_source' / 'default' / 读取失败）
        """
        try:
            mode = (await config_provider.get("signal_tower.mode", "co_source") or "co_source").strip().lower()
            if mode == "manual":
                return "manual"
            active = (await config_provider.get("signal.active_model", "default") or "default").strip()
            if active == "hexp":
                return "hexp"
            if active == "co_source":
                return "co_source"
        except Exception as exc:
            logger.warning("Funnel active-model detection failed: %s → co_source", exc)
        return "co_source"

    async def _read_hexp_funnel_thresholds() -> dict:
        """读取 HEXP（和乘幂）主生产路径的实时热配置阈值。"""
        defaults: dict[str, Any] = {
            "hexp.scorecard.pass_threshold": 45.0,
            "hexp.scorecard.hp_floor": 30.0,
            "hexp.min_grade": "C",
            "hexp.scorecard.b_threshold": 52.0,
            "hexp.scorecard.a_threshold": 75.0,
            "hexp.scorecard.s_hp_min": 60.0,
            "hexp.mm.accel_threshold": 0.7,
            "bridge.max_signal_age_seconds": 180.0,
            "close.after_close_cooldown_sec": 120.0,
            "close.reverse_guard_enabled": False,
            "signal_tower.zone_atr_filter_enabled": False,
        }
        out: dict[str, Any] = {}
        for k, d in defaults.items():
            try:
                v = await config_provider.get(k) if config_provider is not None else None
                if v is None:
                    out[k] = d
                elif isinstance(d, bool):
                    out[k] = str(v).strip().lower() in ("true", "1", "yes")
                elif isinstance(d, str):
                    # 字母档位（如 hexp.min_grade 的 'A'/'B'/'C'）原样保留字符串，禁止 float() 否则会抛异常落入默认档
                    out[k] = str(v).strip()
                else:
                    out[k] = float(v)
            except Exception:
                out[k] = d
        return out

    async def _count_status(
        db_pool, hours: int, symbol: Optional[str], status: int
    ) -> int:
        """Count direction signals with a given signal_status in the window."""
        sym_filter = "AND symbol = $2" if symbol else ""
        sql = f"""
            SELECT COUNT(*) AS cnt
            FROM hcm_signal.signals
            WHERE signal_status = {status}
              AND signal_dir IN ('BUY', 'SELL')
              AND created_at >= now() - ($1::int || ' hours')::interval
              {sym_filter}
            """
        if symbol:
            row = await db_pool.fetchrow(sql, hours, symbol)
        else:
            row = await db_pool.fetchrow(sql, hours)
        return int(row["cnt"]) if row else 0

    def _jsonb_to_dict(value) -> dict:
        """将 jsonb 列安全转为 dict。

        asyncpg 在某些部署下把 jsonb 以 str 形式返回（而非已解码的 dict），
        若直接 isinstance(value, dict) 判断会失败 → 信号漏斗 detail 的
        indicator_values / component_scores / collab 全部变空 → 前端各项指标显示 '-'。
        这里统一兜底：str→json.loads，其它非 dict→{}。
        """
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, ValueError, TypeError):
                return {}
        if isinstance(value, (list, tuple)) and value:
            return {"items": list(value)}
        return {}

    async def _get_funnel_hexp_impl(hours: int = 24, symbol: Optional[str] = None,
                                    active_model: str = "hexp") -> dict:
        """HEXP（和乘幂）主生产路径漏斗：真实反映 HEXP 生产 → 下单 单链路。

        HEXP 落库语义（与 co_source 根本不同，已用线上数据坐实）：
          * 候选 = HEXP 引擎产出的全部信号：signal_mode IN ('HEXP:Regime.*','live_override')
                  或 'filtered'，排除 manual_mirror，排除订单管理方向(MODIFY/CLOSE/...)。
          * HEXP 闸门拦截（不向风控发布）的信号以 signal_mode='filtered' + signal_dir='NO_TRADE'
            + signal_status=0 落库，fallback_reason 带 hexp_* / cooldown_active 命名空间：
              - hexp_grade_red            → RED 档，禁止交易
              - hexp_grade_below_min(X<Y) → 分级低于 hexp.min_grade
              - hexp_extreme_guard(...)   → 极值动量护栏拦（追单反向空间趋0）
              - hexp_no_direction         → 方向分离失败（direction=NO_TRADE）
              - cooldown_active(...)      → 同向冷却未过
              - 其它(filtered 且不匹配上列) → 引擎关闭/无数据/未分类
          * 通过 HEXP 闸门（signal_mode='HEXP:Regime.*' 或 'live_override'，BUY/SELL）才发风控：
              signal_status=2 风控拒(真丢弃) / =1 过风控未成交 / =3 桥成交 / =0 在途待审。
        """
        try:
            sym_filter = "AND symbol = $2" if symbol else ""
            mode_in = "(signal_mode LIKE 'HEXP:%' OR signal_mode = 'live_override' OR signal_mode = 'filtered')"
            dir_filter = "AND signal_dir NOT IN ('MODIFY', 'CLOSE', 'PARTIAL_CLOSE', 'ADD')"

            candidate_sql = f"""
                SELECT COUNT(*) AS cnt
                FROM hcm_signal.signals
                WHERE {mode_in}
                  {dir_filter}
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter}
            """
            # HEXP 闸门拦截分布（全部 filtered + NO_TRADE + status=0），按原因归类
            intercept_sql = f"""
                SELECT
                    CASE
                        WHEN fallback_reason LIKE 'hexp_grade_red%' THEN 'grade'
                        WHEN fallback_reason LIKE 'hexp_grade_below_min%' THEN 'grade'
                        WHEN fallback_reason LIKE 'hexp_extreme_guard%' THEN 'extreme'
                        WHEN fallback_reason LIKE 'hexp_no_direction%' THEN 'direction'
                        WHEN fallback_reason LIKE 'cooldown_active%' THEN 'cooldown'
                        ELSE 'other'
                    END AS layer,
                    COUNT(*) AS cnt
                FROM hcm_signal.signals
                WHERE signal_mode = 'filtered'
                  AND signal_dir = 'NO_TRADE'
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter}
                GROUP BY layer
            """
            # 已发风控（通过 HEXP 闸门）的总数
            published_sql = f"""
                SELECT COUNT(*) AS cnt
                FROM hcm_signal.signals
                WHERE (signal_mode LIKE 'HEXP:%' OR signal_mode = 'live_override')
                  {dir_filter}
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter}
            """
            risk_reject_sql = f"""
                SELECT COUNT(*) AS cnt
                FROM hcm_signal.signals
                WHERE (signal_mode LIKE 'HEXP:%' OR signal_mode = 'live_override')
                  AND signal_status = 2
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter}
            """
            filled_sql = f"""
                SELECT COUNT(*) AS cnt
                FROM hcm_signal.signals
                WHERE (signal_mode LIKE 'HEXP:%' OR signal_mode = 'live_override')
                  AND signal_status = 3
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter}
            """
            detail_sql = f"""
                SELECT symbol, created_at, signal_dir, pre_score,
                       fallback_reason, regime, indicator_values, signal_status,
                       zone_level, zone_type, weight_scheme, signal_mode
                FROM hcm_signal.signals
                WHERE {mode_in}
                  {dir_filter}
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter}
                ORDER BY created_at DESC
                LIMIT 20
            """

            if symbol:
                cand_row = await db_pool.fetchrow(candidate_sql, hours, symbol)
                intercept_rows = await db_pool.fetch(intercept_sql, hours, symbol)
                published_row = await db_pool.fetchrow(published_sql, hours, symbol)
                risk_reject_row = await db_pool.fetchrow(risk_reject_sql, hours, symbol)
                filled_row = await db_pool.fetchrow(filled_sql, hours, symbol)
                detail_rows = await db_pool.fetch(detail_sql, hours, symbol)
            else:
                cand_row = await db_pool.fetchrow(candidate_sql, hours)
                intercept_rows = await db_pool.fetch(intercept_sql, hours)
                published_row = await db_pool.fetchrow(published_sql, hours)
                risk_reject_row = await db_pool.fetchrow(risk_reject_sql, hours)
                filled_row = await db_pool.fetchrow(filled_sql, hours)
                detail_rows = await db_pool.fetch(detail_sql, hours)

            candidate = int(cand_row["cnt"]) if cand_row else 0
            intercept = {r["layer"]: int(r["cnt"]) for r in intercept_rows}
            published = int(published_row["cnt"]) if published_row else 0
            risk_rejected = int(risk_reject_row["cnt"]) if risk_reject_row else 0
            filled = int(filled_row["cnt"]) if filled_row else 0

            grade_blocked = intercept.get("grade", 0)
            extreme_blocked = intercept.get("extreme", 0)
            direction_blocked = intercept.get("direction", 0)
            cooldown_blocked = intercept.get("cooldown", 0)
            other_blocked = intercept.get("other", 0)
            # 一致性兜底：通过 HEXP 闸门数 = 候选 减 全部拦截；以实际发风控数为准
            passed_gate = candidate - (grade_blocked + extreme_blocked + direction_blocked
                                       + cooldown_blocked + other_blocked)
            if published > 0:
                passed_gate = published
            risk_passed = passed_gate - risk_rejected
            not_filled = risk_passed - filled  # 在途(status=0/1) 未成交

            thresholds = await _read_hexp_funnel_thresholds()
            min_grade = thresholds.get("hexp.min_grade", "C")

            layers = [
                {"key": "candidate", "label": "候选信号 (HEXP 生产)", "passed": candidate, "blocked": 0},
                {"key": "grade", "label": f"评分档位闸门 (grade≥{min_grade})",
                 "passed": candidate - grade_blocked, "blocked": grade_blocked},
                {"key": "extreme", "label": "极值动量护栏 (extreme_guard)",
                 "passed": candidate - grade_blocked - extreme_blocked, "blocked": extreme_blocked},
                {"key": "direction", "label": "方向分离 (direction≠NO_TRADE)",
                 "passed": candidate - grade_blocked - extreme_blocked - direction_blocked,
                 "blocked": direction_blocked},
                {"key": "cooldown", "label": "同向冷却闸门 (cooldown_active)",
                 "passed": candidate - grade_blocked - extreme_blocked - direction_blocked - cooldown_blocked,
                 "blocked": cooldown_blocked},
                {"key": "other", "label": "引擎关闭/无数据/其它拦截",
                 "passed": passed_gate, "blocked": other_blocked},
                {"key": "risk", "label": "过风控 (风控未拒)",
                 "passed": risk_passed, "blocked": risk_rejected},
                {"key": "filled", "label": "成交 (MT5 真实成交)",
                 "passed": filled, "blocked": not_filled},
            ]

            sym_rows = await db_pool.fetch(
                "SELECT DISTINCT symbol FROM hcm_signal.signals ORDER BY symbol"
            )
            symbols = [r["symbol"] for r in sym_rows]

            latest_adx = None
            latest_regime = None
            if detail_rows:
                iv = _jsonb_to_dict(detail_rows[0]["indicator_values"])
                latest_adx = iv.get("adx_14")
                latest_regime = detail_rows[0]["regime"]

            detail = []
            for row in detail_rows:
                iv = _jsonb_to_dict(row["indicator_values"])
                ca = row["created_at"]
                st = row["signal_status"]
                smode = row["signal_mode"]
                if smode == "filtered":
                    outcome = "HEXP拦截"
                elif st == 3:
                    outcome = "成交"
                elif st == 1:
                    outcome = "过风控未成交"
                elif st == 2:
                    outcome = "风控拒绝"
                elif st == 0:
                    outcome = "在途待审"
                else:
                    outcome = "候选待定"
                comp = iv.get("_component_scores") if isinstance(iv.get("_component_scores"), dict) else {}
                top_factor = None
                top_val = -1.0
                for fname, fval in comp.items():
                    try:
                        pos = float(fval[0]) if isinstance(fval, (list, tuple)) and len(fval) >= 1 else float(fval)
                    except (TypeError, ValueError, IndexError):
                        pos = 0.0
                    if pos > top_val:
                        top_val = pos
                        top_factor = fname
                collab = iv.get("_collab") if isinstance(iv.get("_collab"), dict) else {}
                detail.append({
                    "symbol": row["symbol"],
                    "created_at": ca.isoformat() if ca else None,
                    "direction": row["signal_dir"],
                    "pre_score": row["pre_score"],
                    "reason": row["fallback_reason"],
                    "reason_cn": map_funnel_reason(row["fallback_reason"]),
                    "regime": row["regime"],
                    "adx": iv.get("adx_14"),
                    "status": st,
                    "outcome": outcome,
                    "marked": False,
                    "indicators": iv,
                    "rsi": iv.get("rsi_14"),
                    "macd": iv.get("macd"),
                    "atr": iv.get("atr_14"),
                    "h1_dir": iv.get("h1_trend_direction"),
                    "h1_regime": iv.get("h1_regime"),
                    "h1_strength": iv.get("h1_trend_strength"),
                    "h1_adx": iv.get("h1_adx"),
                    "comp_scores": comp,
                    "collab": collab,
                    "top_factor": top_factor,
                    "top_factor_val": round(top_val, 4) if top_val >= 0 else None,
                    "zone_level": float(row["zone_level"]) if row["zone_level"] is not None else None,
                    "zone_type": row["zone_type"],
                    "weight_scheme": row["weight_scheme"],
                })

            data = {
                "window_hours": hours,
                "symbol": symbol,
                "symbols": symbols,
                "model": active_model,
                "candidates": candidate,
                "traded": filled,
                "filtered_total": grade_blocked + extreme_blocked + direction_blocked + cooldown_blocked + other_blocked,
                "discarded_total": grade_blocked + extreme_blocked + direction_blocked + cooldown_blocked + other_blocked,
                "marked_total": 0,
                "risk_rejected": risk_rejected,
                "passed_not_filled": not_filled,
                "no_trade": other_blocked,
                "conversion": round(filled / candidate, 4) if candidate else 0.0,
                "layers": layers,
                "block_reason_breakdown": intercept,
                "marked_breakdown": {},
                "thresholds": thresholds,
                "latest_adx": latest_adx,
                "latest_regime": latest_regime,
                "detail": detail,
            }
            return {"code": 0, "data": data, "message": "ok"}
        except Exception as exc:
            logger.exception("HEXP funnel aggregation failed: %s", exc)
            return {"code": "SYS_ERR", "data": None, "message": str(exc)}

    async def _get_funnel_impl(hours: int = 24, symbol: Optional[str] = None) -> dict:
        """Aggregate the signal funnel: how many candidates survive each gate.

        A candidate is every M5-bar scoring attempt. Filtered signals
        (signal_mode='filtered') carry a fallback_reason that identifies the
        gate that blocked them; traded signals passed all gates.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}
        try:
            # HEXP 是当前 LIVE 主路径时，改用 HEXP 命名空间漏斗（真实生产→下单链）
            active_model = await _detect_active_model_for_funnel()
            if active_model == "hexp":
                return await _get_funnel_hexp_impl(hours, symbol, active_model)
            sym_filter = "AND symbol = $2" if symbol else ""
            # 漏斗只统计"真实交易意图"信号(BUY/SELL)。NO_TRADE 是策略主动放弃交易
            # (range_breakout giveup / no_trade_direction / drawdown_brake)，根本没打算下单，
            # 绝不能混入 candidates / 真丢弃 / 过闸门未成交，否则把"没想做"算成"想做没做成"虚高近10倍。
            dir_filter = "AND signal_dir IN ('BUY', 'SELL')"
            # NOTE 【P0-3 统一 2026-08-03】signal_status lifecycle:
            # 0=published 在途(待风控), 1=风控 PASS/DEGRADE(过风控未成交),
            # 2=风控 REJECT(真丢弃), 3=bridge filled (真实成交 MT5),
            # 4=publish_failed (孤儿：发布失败从未进流，不应算过闸门未成交).
            # fallback_reason is set on EVERY candidate (even status=3), so a
            # "marked" signal (e.g. F5 soft penalty) may STILL have traded.
            # We therefore separate three concepts the old funnel conflated:
            #   * 真丢弃 (actually discarded) = status=2  (rejected by risk)
            #   * 被标记仍成交 (marked but traded) = status=3 AND fallback_reason<>''  (soft penalty, still filled)
            #   * 过闸门未成交 (passed risk, not filled) = status=1  (passed risk, pending/in-flight)
            layer_sql = f"""
                SELECT
                    CASE
                        WHEN fallback_reason LIKE '%adx_floor%' THEN 'adx'
                        WHEN fallback_reason LIKE '%reverse%' THEN 'reverse'
                        WHEN fallback_reason LIKE '%co_range_blocked%' THEN 'direction'
                        WHEN fallback_reason LIKE '%cooldown%' THEN 'cooldown'
                        WHEN fallback_reason LIKE '%below_threshold%' THEN 'threshold'
                        WHEN fallback_reason ~* 'co_f[1-5]|f[1-5]_' THEN 'factors'
                        WHEN fallback_reason LIKE '%range_no_extreme%'
                             OR fallback_reason LIKE '%range_breakout%' THEN 'range'
                        WHEN fallback_reason LIKE '%calib_block%'
                             OR fallback_reason LIKE '%calib_unknown%'
                             OR fallback_reason LIKE '%calib_missing%' THEN 'calibration'
                        ELSE 'direction'
                    END AS layer,
                    COUNT(*) AS cnt
                FROM hcm_signal.signals
                WHERE signal_status = 2
                  AND signal_dir NOT IN ('MODIFY', 'CLOSE', 'PARTIAL_CLOSE', 'ADD')
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter} {dir_filter}
                GROUP BY layer
            """
            # 被标记但仍成交 (soft-penalty signals that still got filled)
            marked_sql = f"""
                SELECT
                    CASE
                        WHEN fallback_reason LIKE '%adx_floor%' THEN 'adx'
                        WHEN fallback_reason LIKE '%reverse%' THEN 'reverse'
                        WHEN fallback_reason LIKE '%co_range_blocked%' THEN 'direction'
                        WHEN fallback_reason LIKE '%cooldown%' THEN 'cooldown'
                        WHEN fallback_reason LIKE '%below_threshold%' THEN 'threshold'
                        WHEN fallback_reason ~* 'co_f[1-5]|f[1-5]_' THEN 'factors'
                        WHEN fallback_reason LIKE '%range_no_extreme%'
                             OR fallback_reason LIKE '%range_breakout%' THEN 'range'
                        WHEN fallback_reason LIKE '%calib_block%'
                             OR fallback_reason LIKE '%calib_unknown%'
                             OR fallback_reason LIKE '%calib_missing%' THEN 'calibration'
                        ELSE 'direction'
                    END AS layer,
                    COUNT(*) AS cnt
                FROM hcm_signal.signals
                WHERE signal_status = 3 AND fallback_reason IS NOT NULL AND fallback_reason <> ''
                  AND signal_dir NOT IN ('MODIFY', 'CLOSE', 'PARTIAL_CLOSE', 'ADD')
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter} {dir_filter}
                GROUP BY layer
            """
            # 真实成交 = 桥成功下到 MT5 (signal_status=3)，而非平仓归档表
            traded_sql = f"""
                SELECT COUNT(*) AS cnt
                FROM hcm_signal.signals
                WHERE signal_status = 3
                  AND signal_dir IN ('BUY', 'SELL')
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter}
            """
            # 候选总数（仅 BUY/SELL 真实交易意图，排除 NO_TRADE）
            candidates_sql = f"""
                SELECT COUNT(*) AS cnt
                FROM hcm_signal.signals
                WHERE signal_dir IN ('BUY', 'SELL')
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter}
            """
            detail_sql = f"""
                SELECT symbol, created_at, signal_dir, pre_score,
                       fallback_reason, regime, indicator_values, signal_status,
                       zone_level, zone_type, weight_scheme
                FROM hcm_signal.signals
                WHERE signal_dir NOT IN ('MODIFY', 'CLOSE', 'PARTIAL_CLOSE', 'ADD')
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter}
                ORDER BY created_at DESC
                LIMIT 20
            """
            # 策略主动放弃交易(NO_TRADE)：不计入候选漏斗/真丢弃/过闸门未成交，
            # 单独统计以解释"为何不下单"的主体原因（约占总信号绝大多数）。
            no_trade_sql = f"""
                SELECT COUNT(*) AS cnt
                FROM hcm_signal.signals
                WHERE signal_dir = 'NO_TRADE'
                  AND created_at >= now() - ($1::int || ' hours')::interval
                  {sym_filter}
            """
            if symbol:
                rows = await db_pool.fetch(layer_sql, hours, symbol)
                marked_rows = await db_pool.fetch(marked_sql, hours, symbol)
                traded_row = await db_pool.fetchrow(traded_sql, hours, symbol)
                cand_row = await db_pool.fetchrow(candidates_sql, hours, symbol)
                detail_rows = await db_pool.fetch(detail_sql, hours, symbol)
                no_trade_row = await db_pool.fetchrow(no_trade_sql, hours, symbol)
            else:
                rows = await db_pool.fetch(layer_sql, hours)
                marked_rows = await db_pool.fetch(marked_sql, hours)
                traded_row = await db_pool.fetchrow(traded_sql, hours)
                cand_row = await db_pool.fetchrow(candidates_sql, hours)
                detail_rows = await db_pool.fetch(detail_sql, hours)
                no_trade_row = await db_pool.fetchrow(no_trade_sql, hours)

            block: dict[str, int] = {r["layer"]: int(r["cnt"]) for r in rows}
            marked: dict[str, int] = {r["layer"]: int(r["cnt"]) for r in marked_rows}
            traded = int(traded_row["cnt"]) if traded_row else 0
            candidates = int(cand_row["cnt"]) if cand_row else 0
            no_trade = int(no_trade_row["cnt"]) if no_trade_row else 0

            blk_direction = block.get("direction", 0) + block.get("factors", 0)
            blk_adx = block.get("adx", 0)
            blk_reverse = block.get("reverse", 0)
            blk_threshold = block.get("threshold", 0)
            blk_cooldown = block.get("cooldown", 0)
            # 真丢弃 = 风控拒绝(status=2)；另有 过闸门未成交(status=1) 与 被标记仍成交(status=3+reason)
            discarded_total = sum(block.values())
            marked_total = sum(marked.values())
            passed_not_filled = int(await _count_status(
                db_pool, hours, symbol, status=1))

            l1 = candidates - blk_direction
            l2 = l1 - blk_adx
            l3 = l2 - blk_reverse
            l4 = l3 - blk_threshold
            l5 = l4 - blk_cooldown

            sym_rows = await db_pool.fetch(
                "SELECT DISTINCT symbol FROM hcm_signal.signals ORDER BY symbol"
            )
            symbols = [r["symbol"] for r in sym_rows]
            thresholds = await _read_funnel_thresholds()

            latest_adx = None
            latest_regime = None
            if detail_rows:
                iv = _jsonb_to_dict(detail_rows[0]["indicator_values"])
                latest_adx = iv.get("adx_14")
                latest_regime = detail_rows[0]["regime"]

            detail = []
            for row in detail_rows:
                iv = _jsonb_to_dict(row["indicator_values"])
                ca = row["created_at"]
                st = row["signal_status"]
                # NO_TRADE 是策略主动放弃交易，无论 status 如何都应标注为"策略放弃"，
                # 避免被误显示为"过闸门未成交"/"真丢弃"。
                if row["signal_dir"] == "NO_TRADE":
                    outcome = "策略放弃"
                elif st == 3:
                    outcome = "成交"
                elif st == 1:
                    outcome = "过风控未成交"
                elif st == 2:
                    outcome = "风控拒绝"
                elif st == 4:
                    outcome = "发布失败(孤儿)"
                elif st == 0:
                    outcome = "在途待审"
                else:
                    outcome = "候选待定"
                # 触发因子画像：提取各分量"正相关"得分(列表第0项)，
                # 找出最强驱动该信号方向的指标，供前端直观展示"基于哪些指标触发"。
                comp = iv.get("_component_scores") if isinstance(iv.get("_component_scores"), dict) else {}
                top_factor = None
                top_val = -1.0
                for fname, fval in comp.items():
                    try:
                        pos = float(fval[0]) if isinstance(fval, (list, tuple)) and len(fval) >= 1 else float(fval)
                    except (TypeError, ValueError, IndexError):
                        pos = 0.0
                    if pos > top_val:
                        top_val = pos
                        top_factor = fname
                collab = iv.get("_collab") if isinstance(iv.get("_collab"), dict) else {}
                detail.append({
                    "symbol": row["symbol"],
                    "created_at": ca.isoformat() if ca else None,
                    "direction": row["signal_dir"],
                    "pre_score": row["pre_score"],
                    "reason": row["fallback_reason"],
                    "reason_cn": map_funnel_reason(row["fallback_reason"]),
                    "regime": row["regime"],
                    "adx": iv.get("adx_14"),
                    "status": st,
                    "outcome": outcome,
                    "marked": bool(st == 3 and row["fallback_reason"]),
                    # ── 触发因子增强透传（前端"触发画像"消费）──
                    "indicators": iv,
                    "rsi": iv.get("rsi_14"),
                    "macd": iv.get("macd"),
                    "atr": iv.get("atr_14"),
                    "h1_dir": iv.get("h1_trend_direction"),
                    "h1_regime": iv.get("h1_regime"),
                    "h1_strength": iv.get("h1_trend_strength"),
                    "h1_adx": iv.get("h1_adx"),
                    "comp_scores": comp,
                    "collab": collab,
                    "top_factor": top_factor,
                    "top_factor_val": round(top_val, 4) if top_val >= 0 else None,
                    "zone_level": float(row["zone_level"]) if row["zone_level"] is not None else None,
                    "zone_type": row["zone_type"],
                    "weight_scheme": row["weight_scheme"],
                })

            layers = [
                {"key": "candidate", "label": "候选信号", "passed": candidates, "blocked": 0},
                {"key": "direction", "label": "方向 / 市况有效", "passed": l1, "blocked": blk_direction},
                {"key": "adx", "label": f"ADX 下限 (≥ {thresholds['scoring.min_adx_for_trade']:.0f})", "passed": l2, "blocked": blk_adx},
                {"key": "reverse", "label": "趋势方向有效 (非硬阻断逆势)", "passed": l3, "blocked": blk_reverse},
                {"key": "threshold", "label": "评分过共源门槛", "passed": l4, "blocked": blk_threshold},
                {"key": "cooldown", "label": "冷却通过 → 待成交/成交", "passed": l5, "blocked": blk_cooldown},
            ]

            data = {
                "window_hours": hours,
                "symbol": symbol,
                "symbols": symbols,
                "model": "co_source",
                "candidates": candidates,
                "traded": traded,
                "filtered_total": discarded_total,
                "discarded_total": discarded_total,
                "marked_total": marked_total,
                "risk_rejected": 0,
                "passed_not_filled": passed_not_filled,
                "no_trade": no_trade,
                "conversion": round(traded / candidates, 4) if candidates else 0.0,
                "layers": layers,
                "block_reason_breakdown": block,
                "marked_breakdown": marked,
                "thresholds": thresholds,
                "latest_adx": latest_adx,
                "latest_regime": latest_regime,
                "detail": detail,
            }
            return {"code": 0, "data": data, "message": "ok"}
        except Exception as exc:
            logger.exception("Funnel aggregation failed: %s", exc)
            return {"code": "SYS_ERR", "data": None, "message": str(exc)}

    @router.get("/api/v1/signal-tower/funnel")
    async def get_funnel(
        request: Request,
        hours: int = Query(24, ge=1, le=720, description="Time window in hours"),
        symbol: Optional[str] = Query(None, description="Optional symbol filter"),
        user=Depends(auth_handler.require_auth),
    ):
        """Signal funnel — per-gate pass/block counts + latest verdicts."""
        return await _get_funnel_impl(hours, symbol)

    # ── Hexp Signal Quality Timeline (校准时序 · AI 自我发展轨迹) ──
    # 呈现和乘幂(hexp)模型的信号质量随时间的演变：hp_score / grade / verdict / 方向，
    # 并叠加影子模拟胜率(outcome/dir_hit/pnl_r 来自 hcm_signal.hexp_shadow_eval)。

    async def _get_hexp_quality_impl(hours: int = 24, symbol: Optional[str] = None) -> dict:
        """和乘幂(hexp)信号质量时序。

        数据来源：
          * hcm_signal.signals (signal_mode IN ('hexp','hexp_shadow')) —— hexp 决策落库，
            质量元数据在 indicator_values->_hexp (hp_score/grade/k/verdict/scorecard/...)，
            与 active 模型对照在 indicator_values 顶层 (co_dir/co_agree/co_pre_score)。
          * hcm_signal.hexp_shadow_eval —— 影子模拟 SL/TP 命中评估(仅 shadow 模式有行)。

        Returns: 时序采样 + 汇总(hp_score 均值/分位、grade 分布、方向分布、与 co_source
            一致率、影子模拟胜率)。
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}
        try:
            _sym = "AND symbol = $2" if symbol else ""
            _sym_s = "AND s.symbol = $2" if symbol else ""
            _params = (hours, symbol) if symbol else (hours,)

            # 1) 时序采样：每根 hexp 信号的质量快照（按时间升序）
            _rows = await db_pool.fetch(
                f"""
                SELECT
                    created_at,
                    symbol,
                    signal_dir,
                    COALESCE((indicator_values->>'hp_score')::float, 0)   AS hp_score,
                    COALESCE((indicator_values->>'grade'), '')            AS grade,
                    COALESCE((indicator_values->>'verdict')::float, 0)    AS verdict,
                    COALESCE((indicator_values->>'k')::float, 0)          AS k_value,
                    COALESCE((indicator_values->>'co_dir'), '')                    AS co_dir,
                    COALESCE((indicator_values->>'co_agree') = 'true', false)      AS co_agree,
                    COALESCE((indicator_values->>'co_pre_score')::float, 0)        AS co_pre_score,
                    signal_mode
                FROM hcm_signal.signals
                WHERE signal_mode IN ('hexp','hexp_shadow')
                  AND created_at > NOW() - ($1::int || ' hours')::interval
                  {_sym}
                ORDER BY created_at ASC
                """,
                *_params,
            )
            _series = [
                {
                    "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
                    "symbol": r["symbol"],
                    "direction": r["signal_dir"],
                    "hp_score": round(float(r["hp_score"] or 0), 3),
                    "grade": r["grade"] or "NONE",
                    "verdict": round(float(r["verdict"] or 0), 3),
                    "k_value": round(float(r["k_value"] or 0), 3),
                    "co_dir": r["co_dir"] or "",
                    "co_agree": bool(r["co_agree"]),
                    "co_pre_score": round(float(r["co_pre_score"] or 0), 3),
                    "signal_mode": r["signal_mode"],
                }
                for r in _rows
            ]

            # 2) 汇总
            _n = len(_series)
            _hp_vals = [s["hp_score"] for s in _series if s["hp_score"] > 0]
            _hp_mean = round(sum(_hp_vals) / len(_hp_vals), 3) if _hp_vals else 0.0
            _hp_vals_sorted = sorted(_hp_vals)
            _hp_p50 = _hp_vals_sorted[len(_hp_vals_sorted) // 2] if _hp_vals_sorted else 0.0
            _hp_p90 = _hp_vals_sorted[int(len(_hp_vals_sorted) * 0.9)] if _hp_vals_sorted else 0.0
            _grade_dist: dict = {}
            for s in _series:
                _grade_dist[s["grade"]] = _grade_dist.get(s["grade"], 0) + 1
            _dir_dist: dict = {}
            for s in _series:
                _dir_dist[s["direction"]] = _dir_dist.get(s["direction"], 0) + 1
            _co_agree_n = sum(1 for s in _series if s["co_agree"])
            _co_agree_rate = round(_co_agree_n / _n, 3) if _n else 0.0

            return {
                "code": 0,
                "data": {
                    "summary": {
                        "total": _n,
                        "hp_score_mean": _hp_mean,
                        "hp_score_p50": _hp_p50,
                        "hp_score_p90": _hp_p90,
                        "grade_distribution": _grade_dist,
                        "direction_distribution": _dir_dist,
                        "co_agree_rate": _co_agree_rate,
                        "co_agree_n": _co_agree_n,
                    },
                    "series": _series,
                },
                "message": "ok",
            }
        except Exception as _e:
            return {"code": "SYS_ERR", "data": None, "message": f"hexp quality timeline failed: {_e}"}

    @router.get("/api/v1/signal-tower/hexp-quality")
    async def get_hexp_quality(
        request: Request,
        hours: int = Query(24, ge=1, le=720, description="Time window in hours"),
        symbol: Optional[str] = Query(None, description="Optional symbol filter"),
        user=Depends(auth_handler.require_auth),
    ):
        """Hexp signal quality timeline — hp_score/grade/verdict/direction evolution + shadow win-rate."""
        return await _get_hexp_quality_impl(hours, symbol)

    # ── Calibration Daily History (self-development timeline) ──

    async def _get_calibration_history_impl() -> dict:
        """Read hcm_ai.calibration_daily and present per-regime time series.

        Returns daily cumulative calibration factors + daily win rates per
        M5 regime, the latest applied snapshot, and a deterministic daily
        diagnosis (no LLM dependency — derived purely from the numbers so
        the page is always renderable offline).
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}
        try:
            rows = await db_pool.fetch(
                "SELECT report_date, m5_regime, trades, wins, losses, "
                "win_rate, calib_factor, sample_days, cold_start, applied "
                "FROM hcm_ai.calibration_daily "
                "ORDER BY report_date, m5_regime"
            )
            if not rows:
                return {
                    "code": 0,
                    "data": {
                        "regimes": [],
                        "dates": [],
                        "series": {},
                        "rows": [],
                        "ai_diagnosis": None,
                        "latest": None,
                        "diagnosis": "尚无校准快照：标注样本不足或每日校准任务尚未运行。",
                    },
                    "message": "ok",
                }

            dates_set: list = []
            regimes_set: set = set()
            series: dict = {}
            out_rows: list = []
            for r in rows:
                rd = r["report_date"].isoformat() if hasattr(r["report_date"], "isoformat") else str(r["report_date"])
                reg = r["m5_regime"]
                regimes_set.add(reg)
                if rd not in dates_set:
                    dates_set.append(rd)
                if reg not in series:
                    series[reg] = {"calib": [], "win_rate": []}
                series[reg]["calib"].append(round(float(r["calib_factor"]), 4))
                series[reg]["win_rate"].append(round(float(r["win_rate"]), 4))
                out_rows.append({
                    "report_date": rd,
                    "regime": reg,
                    "trades": int(r["trades"]),
                    "wins": int(r["wins"]),
                    "losses": int(r["losses"]),
                    "win_rate": round(float(r["win_rate"]), 4),
                    "calib_factor": round(float(r["calib_factor"]), 4),
                    "sample_days": int(r["sample_days"]),
                    "cold_start": bool(r["cold_start"]),
                    "applied": bool(r["applied"]),
                })

            # 最新（applied）快照：按 report_date 最大的一组
            latest_date = dates_set[-1]
            per_regime: dict = {}
            total_trades = 0
            total_days = 0
            for r in out_rows:
                if r["report_date"] == latest_date:
                    per_regime[r["regime"]] = {
                        "win_rate": r["win_rate"],
                        "calib_factor": r["calib_factor"],
                        "trades": r["trades"],
                    }
                    total_trades += r["trades"]
            # 样本日 = 最新日期那一组里最大的 sample_days（各体制累计日数）
            latest_rows = [r for r in out_rows if r["report_date"] == latest_date]
            total_days = max((r["sample_days"] for r in latest_rows), default=0)

            # 确定性「每日诊断」：纯由数字推导，无 LLM 依赖
            diag_parts: list = []
            diag_parts.append(
                f"截至 {latest_date}，累计 {total_days} 个样本日、共 {total_trades} 笔已平仓成交。"
            )
            if per_regime:
                wr = " / ".join(
                    f"{k} {v['win_rate'] * 100:.1f}%"
                    for k, v in sorted(per_regime.items())
                )
                diag_parts.append(f"各体制胜率：{wr}。")
                cf = " / ".join(
                    f"{k} {v['calib_factor']:.2f}"
                    for k, v in sorted(per_regime.items())
                )
                diag_parts.append(f"校准因子(越<1越抑制开仓)：{cf}。")
                weak = [k for k, v in per_regime.items() if v["win_rate"] < 0.4]
                strong = [k for k, v in per_regime.items() if v["win_rate"] > 0.55]
                recs: list = []
                if weak:
                    recs.append(
                        f"{'、'.join(weak)} 持续低胜率(≈{min(per_regime[k]['win_rate'] for k in weak) * 100:.0f}%)："
                        "建议人工复核是否加强该体制门控(co.gate.<regime>.trend)或限手数"
                    )
                if strong:
                    recs.append(
                        f"{'、'.join(strong)} 胜率良好：维持当前校准，无需干预"
                    )
                if recs:
                    diag_parts.append("建议：" + "；".join(recs) + "。")
            diagnosis = "".join(diag_parts)

            # AI（DeepSeek）自然语言诊断：取与最新快照同日的诊断；无则 null
            # （前端回退到上面的确定性诊断，离线仍可用）
            ai_diagnosis = None
            try:
                if latest_date:
                    adiag = await db_pool.fetchrow(
                        "SELECT report_date, content, recommendations, needs_review, "
                        "confidence, model, created_at FROM hcm_ai.calibration_diagnosis "
                        "WHERE report_date = $1", latest_date
                    )
                    if adiag:
                        ai_diagnosis = {
                            "report_date": adiag["report_date"].isoformat()
                            if hasattr(adiag["report_date"], "isoformat")
                            else str(adiag["report_date"]),
                            "content": adiag["content"],
                            "recommendations": list(adiag["recommendations"] or []),
                            "needs_review": bool(adiag["needs_review"]),
                            "confidence": adiag["confidence"],
                            "model": adiag["model"],
                            "created_at": adiag["created_at"].isoformat()
                            if hasattr(adiag["created_at"], "isoformat")
                            else str(adiag["created_at"]),
                        }
            except Exception as exc:  # noqa: BLE001
                logger.warning("Calibration ai_diagnosis read failed: %s", exc)

            data = {
                "regimes": sorted(regimes_set),
                "dates": dates_set,
                "series": series,
                "rows": out_rows,
                "ai_diagnosis": ai_diagnosis,
                "latest": {
                    "report_date": latest_date,
                    "per_regime": per_regime,
                    "total_trades": total_trades,
                    "sample_days": total_days,
                    "diagnosis": diagnosis,
                    "ai_diagnosis": ai_diagnosis,
                },
            }
            return {"code": 0, "data": data, "message": "ok"}
        except Exception as exc:
            logger.exception("Calibration history failed: %s", exc)
            return {"code": "SYS_ERR", "data": None, "message": str(exc)}

    @router.get("/api/v1/signal-tower/calibration-history")
    async def get_calibration_history(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Daily calibration time series + latest diagnosis (self-development)."""
        return await _get_calibration_history_impl()

    return router
