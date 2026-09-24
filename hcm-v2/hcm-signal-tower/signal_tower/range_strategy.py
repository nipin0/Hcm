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
    # 【2026-09-18 用户口径】生产改用 **`box`**：触发源 = 价格贴到**箱体边缘**
    #   （贴下沿 → BUY / 贴上沿 → SELL，容差 `range.box.edge_tol_atr`，见 `_box_edge_direction`）。
    #   为什么必须换：`rsi` 口径要求 RSI≥70 或 ≤30，而震荡市 RSI 围绕 50 回归 ⇒
    #   近 48h 的 **142 条** RANGE 日志**全部**卡在 `range_no_extreme`（RSI 全在 31.8~51.2），
    #   等于该通道**没有触发源**。与用户口径「箱体均值回归」「首单需看箱体」一致。
    "range.entry_trigger": "box",    # rsi / pctb / both(任一命中) / and(两者同时命中) / box(箱体边缘)
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
    # ── 【2026-09-18 用户口径】RANGE 注入的 hexp 否决豁免白名单（逗号分隔**前缀**）──
    # 用户口径（2026-09-18 二次修订）：「Range（Magic=55）箱体一单约束，**其他的豁免**」
    #   ⇒ 默认 `"*"` = **不设限**：RANGE 注入不再被任何 hexp 否决原因挡住。
    # 背景（生产实证，详见 docs/方案_RANGE态箱体_hexp_20260918.md §12）：
    #   A2 修复（2026-09-17）后全局白名单生产值 = 仅 `hexp_no_direction`
    #   （`hexp.entry_gate.overridable_reasons`），而 RANGE **逆微动量**的本性使
    #   `hexp_momentum_flip` 必然命中 ⇒ 通道 40 小时零信号
    #   （`HEXP:Regime.RANGE` 最后一条 = 09-16 23:53；`positions.magic=55` 近 3 天 0 笔）。
    # 为什么"全豁免"在本策略上自洽：RANGE 的入场是**箱体边缘逆势**（离线标定 19143 根 M5：
    #   对称 1:1 胜率 70~81%、随机基线 46%），与 hexp 那些"顺势 / 不接刀"风格闸取向天然相反；
    #   且 `quality_gate` 侧早已按同一理由豁免了 pullback_chase / dir_fuse / entry_fuse
    #   （quality_gate.py:356/375/401）—— 本次只是把同一口径补齐到**注入层**。
    # 豁免边界（**只此一层，不扩大**）：风控链（confidence / max_lot / max_pos / daily_loss /
    #   margin / cool_minutes）与"箱体内单仓闸"**一律保留**；`range.enabled` 仍是一键总开关。
    # ⚠ 诚实标注（风险留痕）：`"*"` 同时放开了此前**刻意保留**的"接刀/位置"族 ——
    #   `hexp_extreme_reversal`（极值+动量反向+长影线）、`hexp_cycle_pos_guard`、
    #   `hexp_zone_guard`（贴脸强阻力追多/支撑追空）、`hexp_entry_gate`（含
    #   `TREND_ACCEL 逆加速/逆H1 不接刀`、`TREND_EXHAUST 衰竭末端不追`）、`hexp_pullback_gate`。
    #   （`hexp_disabled` / `hexp_no_fetcher` 属前置失败，实际到不了注入 —— hexp 停用时快照
    #     无 `period_states` ⇒ 本注入本就被 regime 判定挡住。）
    # 收回方式（热调、无需重启）：改回前缀清单即可，例：
    #   set_cfg.py range.hexp_override_reasons "hexp_no_direction,hexp_momentum_flip,hexp_grade_red"
    "range.hexp_override_reasons": "*",
    # ── 【2026-09-18 用户口径】箱体内单仓闸（"箱体内只跑一个订单"）──
    # True = 本轮箱体**未平**期间不再开新单；判定真源 = `hcm_trading.positions.magic`
    #   （= SIGNAL_MODE_MAGIC["range"] = 55，由 tools/position_sync.py 每轮写 MT5 真值）。
    # 平仓（magic 55 持仓归零）→ 清位；之后仍须**重新满足**入场条件
    #   （period_states 判定 RANGE + 入场触发 `range.entry_trigger` + 箱体口径）才新进。
    # 与风控正交：风控链（含 `risk_cool_minutes`）按用户决策**一律保留**，本闸先于风控生效。
    "range.single_position": True,
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
    # 【2026-09-17 B11 修复】原实现对配置中心返回的**字符串**直接 bool() ⇒
    # `"false"` 被判为 True，`range.require_all=false`（"任一周期 RANGE 即判定"）
    # 永远打不开、恒走最严口径（漏掉大量可注入机会）。
    # 改为显式解析字符串/布尔，与 scheduler 对 break_guard_enabled 的写法一致。
    try:
        _ra = (cfg.get("range.require_all", DEFAULTS["range.require_all"])
               if cfg else DEFAULTS["range.require_all"])
        if isinstance(_ra, str):
            require_all = _ra.strip().lower() in ("1", "true", "yes", "on", "y", "t")
        else:
            require_all = bool(_ra)
    except Exception:
        require_all = True
    if require_all:
        return all(v == "RANGE" for v in vals)
    return any(v == "RANGE" for v in vals)


def _box_edge_direction(box, cfg=None):
    """【2026-09-18 用户口径】按**箱体边缘**裁决方向：贴下沿 → BUY，贴上沿 → SELL。

    背景（用户原话 + 选定口径）：「Range（Magic=55）箱体一单约束」，
    入场触发"改用箱体边缘"。**为什么必须换触发源**：`rsi` 口径要求 RSI≥70 或 ≤30，
    而震荡市 RSI 围绕 50 回归 ⇒ 近 48h 的 **142 条** RANGE 日志**全部**卡在
    `range_no_extreme`（RSI 全在 31.8~51.2）—— 该通道等于**没有触发源**。

    容差口径**复用** `range_box.confirm_direction`（读 `range.box.edge_tol_atr`）：
    刻意不在本模块重算，否则同一容差会有两份实现（双真源，本项目已多次踩坑）。
    惰性 import + 全 try 包裹：`range_box` 缺失/异常时本分支退化为"无方向"（不抛），
    其余 `entry_trigger` 口径完全不受影响。

    返回 `(direction, reason)`，direction ∈ {None,"BUY","SELL"}。
    """
    try:
        from signal_tower import range_box
    except Exception:  # noqa: BLE001
        return None, "module_missing"
    if box is None or not bool(getattr(box, "valid", False)):
        return None, "invalid"
    try:
        _buy = bool(range_box.confirm_direction(box, "BUY", cfg)[0])
        _sell = bool(range_box.confirm_direction(box, "SELL", cfg)[0])
    except Exception:  # noqa: BLE001
        return None, "edge_error"
    if _buy and not _sell:
        return "BUY", "edge_lower"
    if _sell and not _buy:
        return "SELL", "edge_upper"
    if _buy and _sell:
        # 箱体宽度 < 2×容差（极窄箱体）→ 两端同时命中 = 矛盾，与 mr_direction 同口径不动作
        return None, "edge_both"
    return None, "not_at_edge"


def mr_direction(rsi_14: Optional[float], pct_b: Optional[float], cfg=None,
                 box=None) -> Optional[str]:
    """均值回归反向信号：超卖/贴下轨 → BUY；超买/贴上轨 → SELL；否则 None。

    双向同时命中（极端矛盾）→ None（不动作，避免不确定时下单）。

    【2026-09-18 用户口径】`range.entry_trigger="box"` → 触发源改为**箱体边缘**
    （见 `_box_edge_direction`）；此时 `rsi_14`/`pct_b` **不参与**方向裁决。
    """
    trig = _s(cfg, "range.entry_trigger").strip().lower()
    if trig == "box":
        return _box_edge_direction(box, cfg)[0]
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
             cfg=None, box=None) -> dict:
    """综合判定，返回诊断 dict（影子期仅供观测，不产生交易动作）。

    返回 {in_range, direction, reason}：
      in_range=False         → 非震荡市，策略不启用
      direction=None         → 震荡但无入场信号（reason 说明卡在哪一步）
      direction="BUY"/"SELL" → 可注入的均值回归反向单

    `box`：`range_box.RangeBox`（**快箱**，`range.entry_trigger="box"` 的触发源；
    duck-typed —— 本模块不 import `range_box`，避免方向模块与几何模块互相耦合）。
    """
    out = {"in_range": False, "direction": None, "reason": ""}
    if not is_range(period_states, cfg):
        out["reason"] = "not_range"
        return out
    out["in_range"] = True
    # 触发源分派：box（箱体边缘）| rsi / pctb / both / and（极值类）
    if _s(cfg, "range.entry_trigger").strip().lower() == "box":
        d, _why = _box_edge_direction(box, cfg)
        if d is None:
            out["reason"] = f"range_box_{_why}"
            return out
        out["direction"] = d
        out["reason"] = f"range_mr({d}/{_why})"
        return out
    d = mr_direction(rsi_14, pct_b, cfg)
    if d is None:
        out["reason"] = "range_no_extreme"
        return out
    out["direction"] = d
    out["reason"] = f"range_mr({d})"
    return out
