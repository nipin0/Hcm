"""range_strategy.py — RANGE（震荡市）均值回归策略：判定 + 反向信号（纯逻辑、可单测）。

【为什么只做单仓、绝不加仓】——2026-09-09 实证（XAUUSD M5, 19137 根）
  信号后"反弹 u·ATR vs 再跌 d·ATR"谁先到（双触同根保守计负）：
                          反弹1.0vs跌0.5   反弹1.0vs跌1.0
    随机基线(无信号)          0.302            0.463
    RSI<30 或 %b<0.05        0.503            0.704
    仅 RSI<30                0.649            0.814
  → 均值回归【信号本身】有真实边际：对称 1:1 时胜率 70~81%，远高于随机 46%。
  → 但【递增摊平(马丁)】满仓后需 66.7% 胜率（数学：期望恒=当前浮亏，摊平不创优势），
    实测最好仅 64.9%、组合仅 50.3% → 负期望。且摊平只在"已逆势1.5ATR"的坏路径触发。
  ⇒ 本策略【单仓、不加仓】：保留有边际的信号，砍掉毁掉期望的摊平。

【边界】本模块只产出"是否 RANGE + 反向方向"，绝不：
  - 改 HEXP 方向裁决（仅在 HEXP 给 NO_TRADE 时注入，不与其已给方向打架）
  - 绕过风控（注入后仍走 AI 闸门 + 风控链 + 桥）

配置键（全部可热调，缺失时用下方 DEFAULTS）：
  range.enabled / range.periods / range.require_all
  range.rsi_overbought / range.rsi_oversold / range.pctb_upper / range.pctb_lower
  range.entry_trigger / range.lot_mult / range.sl_atr / range.tp_atr
"""

from __future__ import annotations

from typing import Optional

# 配置兜底（与 PG/Redis 缺失时一致；铁律：禁硬编码，此处仅为 fallback）
DEFAULTS = {
    "range.enabled": False,          # 总开关：默认关闭（影子期）
    "range.periods": "H1,H4,D1",     # 判定 RANGE 所用的高周期
    "range.require_all": True,       # True=全部周期皆 RANGE 才算（保守）
    "range.rsi_overbought": 70.0,    # RSI 超买 → 做空
    "range.rsi_oversold": 30.0,      # RSI 超卖 → 做多
    "range.pctb_upper": 0.95,        # %b 贴上轨 → 做空
    "range.pctb_lower": 0.05,        # %b 贴下轨 → 做多
    # 【2026-09-10 事故修正】默认由 both(OR) 改为 rsi。
    # 事故：行情单边拉升(4408→4427)，价格沿布林上轨运行使 pct_b 冲到 0.99~1.07，
    #   但 RSI 仅 62~69.8（并【未】超买）→ OR 语义下 %b 单独触发 SELL → 逆势做空止损。
    # 依据（本模块文档实证）：仅 RSI<30 胜率 0.649 / 0.814，
    #   而 "RSI<30 或 %b<0.05" 仅 0.503 / 0.704 —— %b 通道稀释信号质量约 0.15。
    "range.entry_trigger": "rsi",    # rsi / pctb / both(任一命中) / and(两者同时命中)
    # ── 突破熔断（2026-09-10 事故新增）──
    # 均值回归的致命场景是"声称 RANGE、实为突破"。period_states 全 RANGE 时价格仍可能
    # 单边突破（本次即如此），此时继续反向开单 = 逆势送单。
    "range.break_guard_enabled": True,
    "range.break_guard_lookback": 50,   # 用近 N 根(不含当前 bar)的高低点定义区间边界
    "range.break_cooldown_bars": 12,    # 判定突破后停用 N 根 M5(≈1h)
    # ── S1 核心（2026-09-10 实测验证）──
    # 等回踩 offset 个 ATR 再【市价】进场，取代"信号即市价(d=0)"。
    # 依据：双障碍回测（292/188 独立事件，扣 0.12ATR 往返点差）
    #   d=0.0（市价）   E[R]=+0.044 / -0.032，95%CI 跨 0 → 无优势
    #   d=1.0（等回踩） E[R]=+0.197 / +0.190，CI 下沿 >0 → 显著为正
    # 注：不用"挂限价等成交"，因桥 _price_in_zone_band 是 ±5points 对称带、
    #     冲过头不成交（已证缺陷）。改为信号塔侧 armed 状态：等价格走到目标位
    #     再发市价单，必然成交、零改桥，滑点相对 ATR(≈7.5) 可忽略。
    "range.entry_offset_atr": 1.0,
    "range.arm_expire_bars": 12,        # armed 有效期（M5 根数，≈1h），超时作废
    # 区间宽度过滤（实测 +77%：E[R] +0.190 → +0.296/+0.336，CI 下沿 +0.23）
    #   太窄 → 装不下 1.0ATR 止盈；太宽 → 已非震荡
    "range.width_min_atr": 1.5,
    "range.width_max_atr": 6.0,
    "range.lot_mult": 1.0,           # 不再自乘 0.5：手数交由风控动态手数(见 range.confidence)
    "range.confidence": 55.0,        # 0-100 → 归一 0.55：① ≥ risk_min_confidence(0.10) 放行；
                                     # ② < score_tier_mid 的【代码默认值 0.65】(防配置回退误落 mid 档 ×1.0)
    "range.sl_atr": 0.0,             # 0=沿用时段 close.<session>.trailing_stop_distance(越宽越好)
    "range.tp_atr": 1.0,             # RANGE 专用小止盈(实测最优 1.0~1.2)；不接时段 2.5(负期望)
}


def _f(cfg, key):
    """读数值配置：cfg 优先，缺失回落 DEFAULTS。类型异常亦回落（绝不抛）。"""
    try:
        v = cfg.get(key, DEFAULTS[key]) if cfg else DEFAULTS[key]
        if v is None:
            return float(DEFAULTS[key])
        return float(v)
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def _s(cfg, key):
    try:
        v = cfg.get(key, DEFAULTS[key]) if cfg else DEFAULTS[key]
        return str(v) if v is not None else str(DEFAULTS[key])
    except Exception:
        return str(DEFAULTS[key])


def is_range(period_states: dict, cfg=None) -> bool:
    """高周期是否处于 RANGE（震荡）。

    period_states: 形如 {"M5":"TRANSITION","H1":"RANGE","H4":"RANGE","D1":"RANGE"}
    取 DEFAULTS["range.periods"] 指定周期；require_all=True 时须全部为 RANGE。
    缺失/异常 → False（保守：不判 RANGE 即不启用策略）。
    """
    if not isinstance(period_states, dict) or not period_states:
        return False
    periods = [p.strip().upper() for p in _s(cfg, "range.periods").split(",") if p.strip()]
    if not periods:
        periods = ["H1", "H4", "D1"]
    vals = [str(period_states.get(p, "") or "").upper() for p in periods]
    try:
        require_all = bool(
            cfg.get("range.require_all", DEFAULTS["range.require_all"])
            if cfg else DEFAULTS["range.require_all"])
    except Exception:
        require_all = True
    if require_all:
        return all(v == "RANGE" for v in vals)
    return any(v == "RANGE" for v in vals)


def mr_direction(rsi_14: Optional[float], pct_b: Optional[float], cfg=None) -> Optional[str]:
    """均值回归反向信号：超卖/贴下轨 → BUY；超买/贴上轨 → SELL；否则 None。

    双向同时命中（极端矛盾）→ None（不动作，避免不确定时下单）。
    """
    trig = _s(cfg, "range.entry_trigger").strip().lower()
    ob, os_ = _f(cfg, "range.rsi_overbought"), _f(cfg, "range.rsi_oversold")
    pu, pl = _f(cfg, "range.pctb_upper"), _f(cfg, "range.pctb_lower")

    rsi_sell = rsi_14 is not None and rsi_14 >= ob
    rsi_buy = rsi_14 is not None and rsi_14 <= os_
    pb_sell = pct_b is not None and pct_b >= pu
    pb_buy = pct_b is not None and pct_b <= pl

    if trig == "rsi":
        sell, buy = rsi_sell, rsi_buy
    elif trig == "pctb":
        sell, buy = pb_sell, pb_buy
    elif trig == "and":
        # 【2026-09-10 新增】两者同时命中——比 OR(both) 严格得多。
        # 未做独立实证，作为可选口径保留；证据充分的默认口径是 "rsi"。
        sell, buy = (rsi_sell and pb_sell), (rsi_buy and pb_buy)
    else:  # both：任一命中（OR，已证实会稀释信号质量）
        sell, buy = (rsi_sell or pb_sell), (rsi_buy or pb_buy)

    if buy and not sell:
        return "BUY"
    if sell and not buy:
        return "SELL"
    return None


def evaluate(period_states: dict, rsi_14: Optional[float], pct_b: Optional[float],
             cfg=None) -> dict:
    """综合判定，返回诊断 dict（影子期仅供观测，不产生交易动作）。

    返回 {in_range, direction, reason}：
      in_range=False         → 非震荡市，策略不启用
      direction=None         → 震荡但无极值信号
      direction="BUY"/"SELL" → 可注入的均值回归反向单
    """
    out = {"in_range": False, "direction": None, "reason": ""}
    if not is_range(period_states, cfg):
        out["reason"] = "not_range"
        return out
    out["in_range"] = True
    d = mr_direction(rsi_14, pct_b, cfg)
    if d is None:
        out["reason"] = "range_no_extreme"
        return out
    out["direction"] = d
    out["reason"] = f"range_mr({d})"
    return out
