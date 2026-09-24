"""verify_state_strategy_osc.py — 震荡态（S1）策略生命周期自检。

覆盖（每条对应用户规格里的一个硬要求）：
  1. 箱体边界逆势：`≤下沿`→BUY / `≥上沿`→SELL / 箱内→不开仓（规格 9.2）
  2. 箱体退化保护：宽度 < min_width×ATR → 拒绝（否则两边界条件同时成立、方向随机）
  3. 冻结箱体：开仓后 TP 锚点锁定为**当轮**中值，不随新箱体滑动（规格 §7.1）
  4. 冻结箱体生命周期：离开 S1 → 解冻（此前永不成立 → 旧箱体永久沿用）
  5. 梯度手数：档位读自桥侧计数器 `hcm:state:osc_loss_count`
  6. 4ATR 锁止清锁：S5→S0 必须把双计数器**落 Redis**（否则下一根立即复锁）
  7. 计数器推进纯函数 `apply_osc_close`（桥侧回写调用的同一实现）：
     `tp` → 双计数器归零；`sl` → `atr_loss` 累加且 `count`+1；
     其余（be/manual/expert/stop_out，含未知/空）→ **只把 `count` 归零**、`atr_loss` 原样保留
  8. 端到端：2 次 2×ATR 止损 → 累计 4.0ATR → S1 判为 S5 锁止

用法：python verify_state_strategy_osc.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hcm-signal-tower"))

from signal_tower import state_machine as SM      # noqa: E402
from signal_tower import state_strategy as SS     # noqa: E402

SYM = "XAUUSD"
FAILED: list[str] = []
CHECKS = 0


def ck(name: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    ok = (got == want)
    if not ok:
        FAILED.append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name:<46} got={got!r:<28} want={want!r}")


class FakeRedis:
    is_initialized = True

    def __init__(self) -> None:
        self.kv: dict = {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, ex=None):
        self.kv[k] = v


def _infer(state="oscillation", ok=True, decided=True):
    return types.SimpleNamespace(
        ok=ok, decided=decided, state=state, reason="",
        proba={state: 0.6}, margin=0.2, model_version="test")


# 箱体窗口 5：box 取 high/low[len-1-5 : len-1]（**排除**当前 bar）
W = 5


def bars(upper: float, lower: float, n: int = W + 2) -> tuple:
    """造一段 K 线：前 n-1 根构成箱体，最后一根为当前 bar。"""
    high = [upper] * (n - 1) + [upper]
    low = [lower] * (n - 1) + [lower]
    close = [lower] * (n - 1) + [lower]
    return high, low, close


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    redis = FakeRedis()
    strat = SS.StateStrategy(config_provider=None, redis_client=redis)
    strat._box_window = W          # 缩短窗口便于构造
    ATR = 1.0

    print(f"=== 1) 箱体边界逆势（箱体 100~113，中值 106.5，ATR={ATR}, tol=0.25）===")
    high, low, close = bars(113.0, 100.0)

    close[-1] = 100.1
    it = await strat.decide(SYM, "S1_OSC", high=high, low=low, close=close,
                            atr=ATR, slope=0.0)
    ck("触下沿 → action", it.action, "open")
    ck("触下沿 → direction", it.direction, "BUY")
    ck("触下沿 → reason", it.reason, "osc_at_box_lower")
    ck("触下沿 → tp_anchor=箱体中值", round(it.tp_anchor, 2), 106.5)

    print("\n=== 2) 箱体退化保护（宽度 0.5 < 1.0×ATR → 拒绝）===")
    h2, l2, c2 = bars(100.5, 100.0)
    it2 = await strat.decide(SYM + "NARROW", "S1_OSC", high=h2, low=l2, close=c2,
                             atr=ATR, slope=0.0)
    ck("窄箱 → action", it2.action, "")
    ck("窄箱 → reason", it2.reason, "osc_box_too_narrow")

    print("\n=== 3) 箱内不开仓 + 上沿 SELL（用独立品种，避开 §4 的冻结）===")
    it3 = await strat.decide(SYM + "MID", "S1_OSC", high=high, low=low,
                             close=[106.0] * len(high), atr=ATR, slope=0.0)
    ck("箱内 → action", it3.action, "")
    ck("箱内 → reason", it3.reason, "osc_inside_box")
    it4 = await strat.decide(SYM + "UP", "S1_OSC", high=high, low=low,
                             close=[112.9] * len(high), atr=ATR, slope=0.0)
    ck("触上沿 → direction", it4.direction, "SELL")
    ck("触上沿 → reason", it4.reason, "osc_at_box_upper")

    print("\n=== 4) 冻结箱体：TP 锚点锁定当轮中值，不随新箱体滑动 ===")
    # SYM 在第 1 步已开仓 → 箱体已冻结（mid=106.5）。现在把**滚动箱**整体上移 100 点。
    # 【2026-09-17 C 修订本用例】原用 `c5[-1]=200.1`（远在冻结箱 [100,113] **之外**）触发入场
    # — 那正是 C 新增的双侧判据要拦的"箱外"情形 ✗（旧单边判据 `c >= up-tol` 会放行）。
    # 改用**冻结箱上沿以内**的 112.9：对本用例反而**更严格** —— 若代码误用滚动箱 [200,213]，
    # 112.9 会落在该箱**下方**（`_at_lower` 要求 c≥200 不成立）⇒ 不触发入场 ⇒ 用例即失败。
    h5, l5, _c5 = bars(213.0, 200.0)
    c4 = [112.9] * len(h5)
    it5 = await strat.decide(SYM, "S1_OSC", high=h5, low=l5, close=c4,
                             atr=ATR, slope=0.0)
    ck("冻结后 box_mid 仍为当轮值", round(it5.box_mid, 2), 106.5)
    ck("冻结后 tp_anchor 仍为当轮值", round(it5.tp_anchor, 2), 106.5)

    print("\n=== 5) 离开箱体入场态且无持仓 → 解冻（规格 §7.1；B 修追加该条件）===")
    c5 = [200.1] * len(h5)          # 贴**滚动箱**下沿（解冻后应改用新箱体）
    it6 = await strat.decide(SYM, "S2_TREND_INIT", high=h5, low=l5, close=c5,
                             atr=ATR, slope=0.5, direction="up")
    ctx = await strat.get_ctx(SYM)
    ck("离开 S1 后 box_frozen", ctx.box_frozen, False)
    ck("离开 S1 后 osc_round_active", ctx.osc_round_active, False)
    # 回到 S1：应按**新**箱体（213/200）重新冻结
    it7 = await strat.decide(SYM, "S1_OSC", high=h5, low=l5, close=c5,
                             atr=ATR, slope=0.0)
    ck("再入 S1 → 用新箱体中值", round(it7.tp_anchor, 2), 206.5)

    print("\n=== 6) 梯度手数：档位读自桥侧计数器 ===")
    ladder = strat.tuning["ladder"]
    for cnt, want in ((0, ladder[0]), (2, ladder[2]), (99, ladder[-1])):
        redis.kv[SM.OSC_LOSS_COUNT_KEY_TMPL.format(symbol=SYM + "L")] = str(cnt)
        _it = await strat.decide(SYM + "L", "S1_OSC", high=high, low=low,
                                 close=[100.1] * len(high), atr=ATR, slope=0.0)
        ck(f"osc_loss_count={cnt} → lot_multiplier", _it.lot_multiplier, want)

    print("\n=== 7) 4ATR 锁止清锁：S5→S0 必须落 Redis ===")
    fsm = SM.MarketStateMachine(config_provider=None, redis_client=redis)
    fsm._flat_reset = True
    fsm._cache[SYM] = SM.FSMState(symbol=SYM, time_frame="M5",
                                  state=SM.MarketState.S5_OSC_LOCKED.value)
    redis.kv[SM.OSC_LOSS_KEY_TMPL.format(symbol=SYM)] = "5.5"
    redis.kv[SM.OSC_LOSS_COUNT_KEY_TMPL.format(symbol=SYM)] = "3"
    dec = await fsm.step(SYM, "M5", _infer(), positions_open=0, paused=False,
                         bar_time="t1")
    ck("S5 清锁 → state", dec.state, SM.MarketState.S0_IDLE.value)
    ck("S5 清锁 → 键已归零(osc_atr_loss)",
       redis.kv[SM.OSC_LOSS_KEY_TMPL.format(symbol=SYM)], "0")
    ck("S5 清锁 → 键已归零(osc_loss_count)",
       redis.kv[SM.OSC_LOSS_COUNT_KEY_TMPL.format(symbol=SYM)], "0")

    print("\n=== 8) 计数器推进纯函数 apply_osc_close（桥侧回写调用的同一实现）===")
    ck("tp → 双计数器归零", SM.apply_osc_close("tp", 99.0, 3.5, 2), (0.0, 0))
    ck("sl → 累加 ATR 且计数+1", SM.apply_osc_close("sl", 2.0, 1.5, 1), (3.5, 2))
    # 【2026-09-21 语义变更】非 sl 平仓 ⇒ 只归零 count（打断"连续"），atr_loss 原样保留
    ck("be → count 归零、atr_loss 不动", SM.apply_osc_close("be", 2.0, 1.5, 1), (1.5, 0))
    ck("manual → count 归零、atr_loss 不动",
       SM.apply_osc_close("manual", 2.0, 1.5, 1), (1.5, 0))
    ck("expert → count 归零、atr_loss 不动",
       SM.apply_osc_close("expert", 2.0, 1.5, 1), (1.5, 0))
    ck("stop_out → count 归零、atr_loss 不动",
       SM.apply_osc_close("stop_out", 2.0, 1.5, 1), (1.5, 0))
    ck("未知/空归因 → 同样只归零 count（规则全域，无静默特例）",
       SM.apply_osc_close("", 2.0, 1.5, 1), (1.5, 0))
    ck("sl 的 sl_atr 负值被夹到 0", SM.apply_osc_close("sl", -5.0, 1.5, 1), (1.5, 2))

    # 【2026-09-21 新增不变式 · "连续"语义】`sl` 累进；任意非 `sl` 打断 ⇒ `count` 回 0；
    # 而 `atr_loss` **只在 `tp` 归零** —— 两种语义必须各自独立（不可混用）。
    _a_, _c_ = 0.0, 0
    for _r_ in ("sl", "sl"):
        _a_, _c_ = SM.apply_osc_close(_r_, 1.0, _a_, _c_)
    ck("sl,sl → 连续 2 次、累计 2.0ATR", (_c_, round(_a_, 3)), (2, 2.0))
    _a_, _c_ = SM.apply_osc_close("expert", 1.0, _a_, _c_)
    ck("再遇 expert → count 归零（连续被打断）", _c_, 0)
    ck("再遇 expert → atr_loss 保留（预算未被误清）", round(_a_, 3), 2.0)
    _a_, _c_ = SM.apply_osc_close("sl", 1.0, _a_, _c_)
    ck("expert 后再 sl → count 从 0 重新计为 1", _c_, 1)
    ck("直到 tp → atr_loss 才归零", SM.apply_osc_close("tp", 0.0, _a_, _c_), (0.0, 0))

    print("\n=== 9) 端到端：2 次 2×ATR 止损 → 累计 4.0ATR → S1 锁止 ===")
    fsm2 = SM.MarketStateMachine(config_provider=None, redis_client=redis)
    a, c = 0.0, 0
    for _ in range(2):
        a, c = SM.apply_osc_close("sl", 2.0, a, c)
    ck("两次 2×ATR 止损 → 累计", round(a, 3), 4.0)
    ck("两次 2×ATR 止损 → 连续次数", c, 2)
    redis.kv[SM.OSC_LOSS_KEY_TMPL.format(symbol=SYM)] = f"{a:.6f}"
    redis.kv[SM.OSC_LOSS_COUNT_KEY_TMPL.format(symbol=SYM)] = str(c)
    fsm2._cache[SYM] = SM.FSMState(symbol=SYM, time_frame="M5",
                                   state=SM.MarketState.S1_OSC.value)
    dec2 = await fsm2.step(SYM, "M5", _infer("oscillation"), positions_open=1,
                           paused=False, bar_time="t9")
    ck("累计达 4×ATR → 锁止", dec2.state, SM.MarketState.S5_OSC_LOCKED.value)

    print("\n=== 10) 趋势态 S2/S3 与买点 L4 ===")
    # 造 K 线：前段 110/100 构成箱体，最后一根收 109（自近期高点回踩 1.0 > 0.5×ATR），
    # 且最后一根振幅 0.4 ≤ 1.5×ATR（不过追涨过滤）。
    n = W + 3
    hT = [110.0] * (n - 1) + [110.2]
    lT = [100.0] * (n - 1) + [109.8]
    cT = [100.0] * (n - 1) + [109.0]

    itA = await strat.decide(SYM + "S2A", "S2_TREND_INIT", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, direction="none")
    ck("S2 方向 NONE → 否决", itA.reason, "dir_none_veto")

    itB = await strat.decide(SYM + "S2B", "S2_TREND_INIT", high=hT, low=lT,
                             close=[111.9] * n, atr=ATR, slope=0.1, direction="up")
    ck("S2 未回踩 → 不开仓", itB.reason, "init_no_pullback")

    itC = await strat.decide(SYM + "S2C", "S2_TREND_INIT", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, direction="up")
    ck("S2 回踩到位 → 开仓", itC.action, "open")
    ck("S2 → 方向", itC.direction, "BUY")
    ck("S2 → reason", itC.reason, "init_pullback")
    ctxC = await strat.get_ctx(SYM + "S2C")
    ck("S2 → 锁定方向", ctxC.trend_dir, "UP")

    itD = await strat.decide(SYM + "S2C", "S3_TREND_MID", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, direction="up",
                             positions_open=1, position_dir="BUY")
    ck("S3 顺势加仓 → add", itD.action, "add")
    ck("S3 → reason", itD.reason, "mid_add_on_pullback")

    itE = await strat.decide(SYM + "S2C", "S3_TREND_MID", high=hT, low=lT, close=cT,
                             atr=ATR, slope=-0.1, direction="down",
                             positions_open=1, position_dir="BUY")
    ck("S3 反转头 → 拒绝加仓", itE.reason, "add_dir_conflict_trail_tighten")
    # 反转头必须**同时矫准 SL**（收紧 + 准备离场），否则桥不知道要收紧
    ck("反转头 → 收紧系数", itE.trail_mult, strat.tuning["fade_trail_mult"])
    ck("反转头 → 准备离场", itE.exit_ready, True)
    ck("反转头 → 指令也带收紧",
       SS.to_directive(itE)["trail_mult"], strat.tuning["fade_trail_mult"])

    itF = await strat.decide(SYM + "S2C", "S3_TREND_MID", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, direction="up",
                             positions_open=1, position_dir="SELL")
    ck("S3 与持仓反向 → 拒绝加仓", itF.reason, "add_against_position")

    ctxC = await strat.get_ctx(SYM + "S2C")
    ctxC.add_count = strat.tuning["max_adds"]
    itG = await strat.decide(SYM + "S2C", "S3_TREND_MID", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, direction="up",
                             positions_open=1, position_dir="BUY")
    ck("S3 达加仓上限 → 不再加", itG.reason, "mid_max_adds_reached")

    print("\n=== 11) 冻结箱体只在**真的下单**时发生 ===")
    itI = await strat.decide(SYM + "NFZ", "S1_OSC", high=high, low=low,
                             close=[100.1] * len(high), atr=ATR, slope=0.0,
                             positions_open=1)
    ck("被持仓拦下 → action", itI.action, "none")
    ctxI = await strat.get_ctx(SYM + "NFZ")
    ck("未下单 → 箱体不冻结", ctxI.box_frozen, False)

    print("\n=== 12) 趋势轮次生命周期：离开趋势态且无持仓 → 清除锁定方向/加仓计数 ===")
    itH = await strat.decide(SYM + "S2C", "S0_IDLE", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, direction="up", positions_open=0)
    # 离开趋势态不是"未裁决"，而是**显式不下单** → action="none"（StrategyIntent 语义）
    ck("离开趋势态 → action=none", itH.action, "none")
    ck("离开趋势态 → reason", itH.reason, "s0_idle_no_new_order")
    ctxC = await strat.get_ctx(SYM + "S2C")
    ck("轮次结束 → 锁定方向已清", ctxC.trend_dir, "")
    ck("轮次结束 → 加仓计数已清", ctxC.add_count, 0)

    print("\n=== 13) S4 趋势衰竭：禁新开 + 收紧移动止损 + 准备离场 ===")
    itJ = await strat.decide(SYM + "S4", "S4_TREND_FADE", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, direction="up", positions_open=1,
                             position_dir="BUY")
    ck("S4 → action", itJ.action, "none")
    ck("S4 → 收紧系数", itJ.trail_mult, strat.tuning["fade_trail_mult"])
    ck("S4 → 准备离场", itJ.exit_ready, True)
    ck("S4 → reason", itJ.reason, "s4_no_new_order_trail_tighten")

    print("\n=== 14) S5 锁止 / S9 暂停 ===")
    itK = await strat.decide(SYM + "S5", "S5_OSC_LOCKED", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, positions_open=1)
    ck("S5 → action", itK.action, "none")
    ck("S5 → reason", itK.reason, "s5_osc_locked_no_new_order")
    itL = await strat.decide(SYM + "S9", "S9_PAUSED", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, positions_open=1)
    ck("S9 → 不收紧（正常维护）", itL.trail_mult, 1.0)
    ck("S9 → 非离场", itL.exit_ready, False)
    ck("S9 → reason", itL.reason, "s9_paused_no_new_order_trail_keep")

    print("\n=== 15) 移动止损回看根数与方向窗口**解耦** ===")
    strat._slope_window, strat._trail_lookback = 7, 33
    itM = await strat.decide(SYM + "TL", "S3_TREND_MID", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, direction="up")
    ck("trail_lookback 取自独立键（非 slope_window）", itM.trail_lookback, 33)

    print("\n=== 16) 塔→桥 下单契约（to_signal_fields，纯函数）===")
    # S1 箱体单
    f_osc = SS.to_signal_fields(itC if itC.state == "S1_OSC" else it4, base_lot=0.01)
    _o = SS.to_signal_fields(await strat.decide(
        SYM + "CT1", "S1_OSC", high=high, low=low, close=[100.1] * len(high),
        atr=ATR, slope=0.0), base_lot=0.01)
    ck("S1 → signal_mode", _o["signal_mode"], "state_osc")
    ck("S1 → sl_price 恒 0（桥算）", _o["sl_price"], 0.0)
    ck("S1 → tp1 = 冻结箱体中值", round(_o["tp1"], 2), 106.5)
    ck("S1 → lot = base × 倍率", _o["lot"], round(0.01 * _o["_fsm"]["lot_multiplier"], 4))
    ck("S1 → _fsm.state", _o["_fsm"]["state"], "S1_OSC")

    # 趋势加仓单
    _t = SS.to_signal_fields(itD, base_lot=0.01)
    ck("S3 加仓 → signal_mode", _t["signal_mode"], "state_trend")
    ck("S3 加仓 → tp1 = 0（桥算）", _t["tp1"], 0.0)
    ck("S3 加仓 → _fsm.action", _t["_fsm"]["action"], "add")

    # S4 收紧意图必须随单下发（规格：S4 收紧移动止损）
    _s4 = SS.to_signal_fields(itJ, base_lot=0.01)
    ck("S4 未下单 → 契约返回空", _s4, {})

    # 非下单意图 → 空
    ck("S5 意图 → 契约返回空",
       SS.to_signal_fields(await strat.decide(
           SYM + "CT5", "S5_OSC_LOCKED", high=high, low=low,
           close=[106.0] * len(high), atr=ATR, slope=0.0), base_lot=0.01), {})

    # 【(a) 的安全门】默认必须不下单：`state.order_enabled` 缺省 False
    ck("默认门控：order_enabled=False", SS.StateStrategy().order_enabled, False)
    # `_fsm` 要写进 `indicator_values` JSONB → 必须可 JSON 序列化
    # （numpy 标量会在此炸；这是 JSONB 插入前最容易漏的一类错误）
    import json as _json
    _ser = _json.dumps(_o["_fsm"], ensure_ascii=False)
    ck("_fsm 可 JSON 序列化（JSONB 前置）", isinstance(_ser, str), True)

    print("\n=== 16b) 持仓管理指令 to_directive（(c) 前置）===")
    _d4 = SS.to_directive(itJ)                 # S4 = 收紧 + 准备离场
    ck("S4 指令 → trail_mult", _d4["trail_mult"], strat.tuning["fade_trail_mult"])
    ck("S4 指令 → exit_ready", _d4["exit_ready"], True)
    ck("S4 指令 → is_trend", _d4["is_trend"], True)
    _d9 = SS.to_directive(itL)                 # S9 = 不收紧
    ck("S9 指令 → trail_mult", _d9["trail_mult"], 1.0)
    ck("S9 指令 → exit_ready", _d9["exit_ready"], False)
    _d1 = SS.to_directive(it3)                 # S1
    ck("S1 指令 → is_trend", _d1["is_trend"], False)
    ck("S1 指令 → sub_mode", _d1["sub_mode"], "state_osc")
    ck("指令键模板", SS.DIRECTIVE_KEY_TMPL.format(symbol="XAUUSD"),
       "hcm:state:directive:XAUUSD")

    print("\n=== 16c) 加仓笔数以真实持仓为权威（fsm_adds_used）===")
    for tag, adds, want in (("AU", 2, "mid_max_adds_reached"),   # 已用满 → 封顶
                            ("AV", 0, "mid_add_on_pullback"),    # 未用 → 允许加
                            ("AW", 1, "mid_add_on_pullback")):
        _sy = f"{SYM}{tag}"
        await strat.decide(_sy, "S2_TREND_INIT", high=hT, low=lT, close=cT,
                           atr=ATR, slope=0.1, direction="up")     # 先建仓锁定方向
        _r = await strat.decide(_sy, "S3_TREND_MID", high=hT, low=lT, close=cT,
                                atr=ATR, slope=0.1, direction="up",
                                positions_open=1, position_dir="BUY",
                                fsm_adds_used=adds)
        ck(f"fsm_adds_used={adds} → reason", _r.reason, want)

    print("\n=== 17) mode → magic 映射（单一真源在 signal_publisher）===")
    try:
        from signal_tower.signal_publisher import magic_for_signal_mode as _mg
        ck("state_osc → magic", _mg("state_osc"), 61)
        ck("state_trend → magic", _mg("state_trend"), 62)
        ck("裸 state_fsm → 0（不误映射）", _mg("state_fsm"), 0)
        ck("hexp → magic 保持", _mg("HEXP:Regime.RANGE"), 11)

        # ── 【§57 magic 布局】触发下单信号信息编码（8 位 LL·SS·RR·TT）──
        # 这是**永久契约**（magic 写在已成交订单上、事后改不回），故必须断言住：
        # 布局只允许**追加低位数**，前导段语义不得变更。
        _em = SS.encode_fsm_magic
        ck("编码 osc·S1·触下沿·档0", _em("state_osc", "osc_at_box_lower", "S1_OSC", 0), 61010100)
        ck("编码 osc·S1·触上沿·档2", _em("state_osc", "osc_at_box_upper", "S1_OSC", 2), 61010202)
        ck("编码 trend·S2·回踩·档0",
           _em("state_trend", "init_pullback", "S2_TREND_INIT", 0), 62021100)
        ck("编码 trend·S3·加仓·档1",
           _em("state_trend", "mid_add_on_pullback", "S3_TREND_MID", 1), 62032301)
        ck("编码·未知 reason → 99", _em("state_osc", "whatever", "S1_OSC", 0), 61019900)
        ck("编码·非 FSM 子模式 → 0（调用方回落基码）", _em("hexp", "x", "S1_OSC", 0), 0)
        # 向后兼容：历史单用**裸 61/62**，必须仍被认作 FSM（否则动不了它们的止损）
        ck("兼容·裸 61 是 FSM", SS.is_fsm_magic(61), True)
        ck("兼容·裸 62 是 FSM", SS.is_fsm_magic(62), True)
        ck("兼容·新格式是 FSM", SS.is_fsm_magic(61010100), True)
        ck("非 FSM·hexp 11", SS.is_fsm_magic(11), False)
        ck("非 FSM·range 55", SS.is_fsm_magic(55), False)
        ck("logic·裸 61 → osc", SS.fsm_magic_logic(61), "osc")
        ck("logic·新 62021100 → trend", SS.fsm_magic_logic(62021100), "trend")
        ck("logic·11 → 空（不是 FSM 就不给它 scope）", SS.fsm_magic_logic(11), "")
        ck("解码回环", SS.decode_fsm_magic(61010202),
           {"logic": 61, "state_code": 1, "reason_code": 2, "tier": 2, "legacy": False})
    except Exception as _e:  # noqa: BLE001
        print(f"  SKIP signal_publisher 不可导入（{_e}）—— 该项需在有依赖的环境验证")

    print("\n=== 18) L4 触价入场（§49）：复用桥既有 zone gate，塔只下发触价位 ===")
    # 默认模式必须是 close_check（= 既有行为），且默认下**不**布置触价单
    ck("默认 entry_mode = close_check（既有行为）", strat.tuning["entry_mode"], "close_check")
    ck("默认 entry_wait_sec = 300", strat.tuning["entry_wait_sec"], 300)
    ck("close_check 下未回踩 → 不布置触价", itB.zone_level, 0.0)

    # 切到 zone_touch：未回踩 → 应产出"触价单"
    # hT 近 5 根高点=110.2、ATR=1.0、pullback_atr=0.5 → 触价位 = 110.2 − 0.5 = 109.7
    strat._entry_mode = "zone_touch"
    _c1119 = [111.9] * n          # 收在 111.9：高于 109.7 → **未**回踩到位
    itN = await strat.decide(SYM + "TZ", "S2_TREND_INIT", high=hT, low=lT,
                             close=_c1119, atr=ATR, slope=0.1, direction="up")
    ck("zone_touch 未回踩 → 仍产 open 意图", itN.action, "open")
    ck("zone_touch → reason", itN.reason, "init_touch_wait")
    ck("zone_touch → 方向", itN.direction, "BUY")
    ck("触价位 = 近5根高点 − pullback_atr×ATR", round(itN.zone_level, 3), 109.7)
    ck("触价位在现价**下方**（等回落，非追高）", itN.zone_level < 111.9, True)
    ck("zone_touch → 等待秒数", itN.entry_wait_sec, 300)

    # 契约：三字段必须**成对**下发 —— 少任一个桥侧即退化为"市价立即成交"（语义静默降级）
    fT = SS.to_signal_fields(itN, base_lot=0.01)
    ck("契约 → zone_level", round(fT["zone_level"], 3), 109.7)
    ck("契约 → zone_type", fT["zone_type"], SS.ZONE_TYPE_TREND_PULLBACK)
    ck("契约 → entry_trigger_wait", fT["entry_trigger_wait"], 300)
    ck("契约 → signal_mode 仍为 state_trend", fT["signal_mode"], "state_trend")
    _fC = SS.to_signal_fields(itC, base_lot=0.01)          # itC = 回踩到位的市价入场
    ck("无触价意图 → zone_level=0", _fC["zone_level"], 0.0)
    ck("无触价意图 → entry_trigger_wait=0（必须成对）", _fC["entry_trigger_wait"], 0)
    ck("无触价意图 → zone_type=''", _fC["zone_type"], "")

    # ── 去重闸（**最关键的安全断言**）──
    # 塔每 bar 重评估，而桥侧触价单最长等 entry_wait_sec；若不去重，等待期内每 bar 都发
    # 一张同价位触价单 → 价格触及时**同时成交多笔**（实盘=超仓）。
    itN2 = await strat.decide(SYM + "TZ", "S2_TREND_INIT", high=hT, low=lT,
                              close=_c1119, atr=ATR, slope=0.1, direction="up")
    ck("等待期内重复评估 → 不下单", itN2.action, "")
    ck("等待期内 → reason", itN2.reason, "init_touch_pending")
    ck("等待期内 → 不带触价位", itN2.zone_level, 0.0)

    # 过期后可再布置（把"下发时刻"改老模拟，避免真等 300s）
    ctxTZ = await strat.get_ctx(SYM + "TZ")
    ctxTZ.touch_pending_at = "2020-01-01T00:00:00+00:00"
    itN3 = await strat.decide(SYM + "TZ", "S2_TREND_INIT", high=hT, low=lT,
                              close=_c1119, atr=ATR, slope=0.1, direction="up")
    ck("触价单过期 → 可再布置", itN3.reason, "init_touch_wait")

    # 已回踩到位时**不走触价**（仍市价），且不下发 zone
    # —— 否则桥会把价位当"待触及"而 defer，等于去等**反弹回**一个已经跌破的位（方向反了）
    itP = await strat.decide(SYM + "TZ2", "S2_TREND_INIT", high=hT, low=lT, close=cT,
                             atr=ATR, slope=0.1, direction="up")
    ck("已回踩到位 → 仍走市价入场", itP.reason, "init_pullback")
    ck("已回踩到位 → 不下发触价", itP.zone_level, 0.0)

    # DOWN 对称：触价位须在现价**上方**（等反弹），不是下方
    hD = [100.1] * (n - 1) + [100.2]
    lD = [99.9] * (n - 1) + [99.8]      # 近 5 根低点 min = 99.8 → 触价位 = 100.3
    cD = [100.0] * (n - 1) + [100.0]    # 收 100.0 < 100.3 → 未回踩到位
    itQ = await strat.decide(SYM + "TZ3", "S2_TREND_INIT", high=hD, low=lD, close=cD,
                             atr=ATR, slope=-0.1, direction="down")
    ck("DOWN 触价 → SELL", itQ.direction, "SELL")
    ck("DOWN 触价位 = 近5根低点 + pullback_atr×ATR", round(itQ.zone_level, 3), 100.3)
    ck("DOWN 触价位在现价**上方**（等反弹）", itQ.zone_level > 100.0, True)

    # 配置闸：entry_wait_sec=0 → 不下发触价（等价 close_check）
    strat._entry_wait_sec = 0
    itR = await strat.decide(SYM + "TZ4", "S2_TREND_INIT", high=hT, low=lT,
                             close=_c1119, atr=ATR, slope=0.1, direction="up")
    ck("entry_wait_sec=0 → 不布置触价", itR.reason, "init_no_pullback")
    strat._entry_wait_sec = 300

    # 离开趋势态且无持仓 → 未结触价单一并作废（否则去重闸会误伤下一轮）
    _ = await strat.decide(SYM + "TZ", "S0_IDLE", high=hT, low=lT, close=cT,
                           atr=ATR, slope=0.1, direction="up", positions_open=0)
    ck("离开趋势态 → 触价单已作废", ctxTZ.touch_pending_at, "")

    # `_pullback_level` 与 `_pullback_entry` **同一真值**（临界两侧必须一致）
    _lv = strat._pullback_level("UP", hT, lT, W, ATR, 111.9)
    _ok_below, _ = strat._pullback_entry("UP", _lv - 0.01, hT, lT, W, ATR)
    _ok_above, _ = strat._pullback_entry("UP", _lv + 0.01, hT, lT, W, ATR)
    ck("价略低于触价位 → 视为已到位", _ok_below, True)
    ck("价略高于触价位 → 未到位", _ok_above, False)
    ck("窗口不足 → 触价位=0（不布置）",
       strat._pullback_level("UP", hT[:3], lT[:3], W, ATR, 111.9), 0.0)

    strat._entry_mode = "close_check"      # 复原默认，避免污染后续断言

    print("\n=== 19) 马丁补仓：SL 后同向补下一档（不等箱体重建/不等状态）===")
    # 场景：箱体 [100,113]、ATR=1。首单仍走箱体下沿 BUY；随后模拟桥侧写回的**止损**
    #   （`osc_loss_count` 0→1、`osc_atr_loss` 0→1.5），下一根 bar **价格在箱体中部
    #   （远不贴边）、且状态为 S0_IDLE（非箱体入场态）** ⇒ 若仍需箱体条件就绝不会开仓；
    #   马丁补仓应在此**直接同向 BUY 补下一档**（证明与箱体/状态均已解耦）。
    _MAL = SM.OSC_LOSS_COUNT_KEY_TMPL
    _LOK = SM.OSC_LOSS_KEY_TMPL
    _SY = SYM + "MA"
    redis.kv[_MAL.format(symbol=_SY)] = "0"
    redis.kv[_LOK.format(symbol=_SY)] = "0.0"
    _itM0 = await strat.decide(_SY, "S1_OSC", high=high, low=low,
                               close=[100.1] * len(high), atr=ATR, slope=0.0)
    ck("马丁·首单仍走箱体（触下沿 BUY）", _itM0.reason, "osc_at_box_lower")
    ck("马丁·首单档位=ladder[0]", _itM0.lot_multiplier, ladder[0])
    _ctxM = await strat.get_ctx(_SY)
    ck("马丁·已记录本轮入场方向", _ctxM.osc_last_dir, "BUY")

    # 桥侧写回止损：count +1、atr_loss 累加（真值由 `apply_osc_close` 给出，见 §8）
    redis.kv[_MAL.format(symbol=_SY)] = "1"
    redis.kv[_LOK.format(symbol=_SY)] = "1.5"
    _itM1 = await strat.decide(_SY, "S0_IDLE", high=high, low=low,
                               close=[106.0] * len(high), atr=ATR, slope=0.0,
                               positions_open=0)
    ck("马丁·止损后同向补 → action", _itM1.action, "open")
    ck("马丁·止损后同向补 → 方向=本轮方向", _itM1.direction, "BUY")
    ck("马丁·止损后同向补 → reason", _itM1.reason, "osc_martingale_sl")
    ck("马丁·止损后同向补 → 档位=ladder[1]", _itM1.lot_multiplier, ladder[1])
    ck("马丁·止损后同向补 → 无视状态（S0 也补）", _itM1.action, "open")
    # 幂等：同一笔止损只补一次（快照已更新 ⇒ 再评估不得重复补）
    _itM2 = await strat.decide(_SY, "S0_IDLE", high=high, low=low,
                               close=[106.0] * len(high), atr=ATR, slope=0.0,
                               positions_open=0)
    ck("马丁·同一止损不重复补", _itM2.reason != "osc_martingale_sl", True)
    # 4ATR 预算为**硬刹车**：预算用尽 ⇒ 不补（不放宽既有防爆仓闸门）
    _SY2 = SYM + "MA2"
    redis.kv[_MAL.format(symbol=_SY2)] = "0"
    redis.kv[_LOK.format(symbol=_SY2)] = "0.0"
    await strat.decide(_SY2, "S1_OSC", high=high, low=low,
                       close=[100.1] * len(high), atr=ATR, slope=0.0)
    redis.kv[_MAL.format(symbol=_SY2)] = "3"
    redis.kv[_LOK.format(symbol=_SY2)] = "5.0"          # ≥ state.osc_atr_loss_limit(4.0)
    _itM3 = await strat.decide(_SY2, "S0_IDLE", high=high, low=low,
                               close=[106.0] * len(high), atr=ATR, slope=0.0,
                               positions_open=0)
    ck("马丁·4ATR 预算用尽 → 不补（刹车）",
       _itM3.reason != "osc_martingale_sl", True)
    # 未平仓（positions_open>0）时也不补 —— 不制造超仓
    _SY3 = SYM + "MA3"
    redis.kv[_MAL.format(symbol=_SY3)] = "0"
    redis.kv[_LOK.format(symbol=_SY3)] = "0.0"
    await strat.decide(_SY3, "S1_OSC", high=high, low=low,
                       close=[100.1] * len(high), atr=ATR, slope=0.0)
    redis.kv[_MAL.format(symbol=_SY3)] = "1"
    redis.kv[_LOK.format(symbol=_SY3)] = "1.5"
    _itM4 = await strat.decide(_SY3, "S0_IDLE", high=high, low=low,
                               close=[106.0] * len(high), atr=ATR, slope=0.0,
                               positions_open=1)
    ck("马丁·未平仓 → 不补", _itM4.reason != "osc_martingale_sl", True)

    print("\n=== 20) 连续根数**同 bar 去重**（P1-7：防重复评估把 N 根虚增）===")
    # 破界：同一 bar_id 被评估两次 → 只推进 1 根（无去重会 +2 ⇒ `break_confirm=2`
    #   被 1 根满足 ⇒ 提前 1 根离场。实证：2026-09-18 ticket 426224111）。
    _bc_save = strat._break_confirm
    strat._break_confirm = 2       # 与生产一致（DEFAULTS=0 关闭，离线需显式打开）
    _SYB = SYM + "BK"
    _itbk0 = await strat.decide(_SYB, "S1_OSC", high=high, low=low,
                                close=[100.1] * len(high), atr=ATR, slope=0.0,
                                bar_id="B1")
    ck("同bar去重·B1 开仓", _itbk0.action, "open")
    _itbk1 = await strat.decide(_SYB, "S1_OSC", high=high, low=low,
                                close=[99.0] * len(high), atr=ATR, slope=0.0,
                                positions_open=1, bar_id="B2")
    ck("同bar去重·B2 破界=1", _itbk1.break_streak, 1)
    _itbk2 = await strat.decide(_SYB, "S1_OSC", high=high, low=low,
                                close=[99.0] * len(high), atr=ATR, slope=0.0,
                                positions_open=1, bar_id="B2")
    ck("同bar去重·B2 重复评估仍=1", _itbk2.break_streak, 1)
    ck("同bar去重·B2 重复评估不触发离场", _itbk2.exit_now, False)
    _itbk3 = await strat.decide(_SYB, "S1_OSC", high=high, low=low,
                                close=[99.0] * len(high), atr=ATR, slope=0.0,
                                positions_open=1, bar_id="B3")
    ck("同bar去重·B3 破界=2 → 触发离场", _itbk3.exit_now, True)
    strat._break_confirm = _bc_save
    # 贴边防抖同理（`entry_confirm` 调大到 3 才可见；默认 1 时无行为差异）
    _ec_save = strat._entry_confirm
    strat._entry_confirm = 3
    _SYE = SYM + "EG"
    _ite1 = await strat.decide(_SYE, "S1_OSC", high=high, low=low,
                               close=[100.1] * len(high), atr=ATR, slope=0.0, bar_id="E1")
    ck("同bar去重·E1 贴边=1（未达 3）", _ite1.edge_streak, 1)
    _ite2 = await strat.decide(_SYE, "S1_OSC", high=high, low=low,
                               close=[100.1] * len(high), atr=ATR, slope=0.0, bar_id="E1")
    ck("同bar去重·E1 重复评估仍=1", _ite2.edge_streak, 1)
    await strat.decide(_SYE, "S1_OSC", high=high, low=low,
                       close=[100.1] * len(high), atr=ATR, slope=0.0, bar_id="E2")
    _ite4 = await strat.decide(_SYE, "S1_OSC", high=high, low=low,
                               close=[100.1] * len(high), atr=ATR, slope=0.0, bar_id="E3")
    ck("同bar去重·E3 贴边=3 → 开仓", _ite4.action, "open")
    strat._entry_confirm = _ec_save

    print("\n" + "=" * 70)
    print(f"共 {CHECKS} 项，失败 {len(FAILED)} 项"
          + (f"：{FAILED}" if FAILED else " —— 全部通过"))
    print("=" * 70)
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
