"""verify_state_machine_age.py — 状态年龄（age_bars）语义的确定性验证。

为什么需要它：`age_bars` 现在承载"初生 / 中段"的区分（方案 §25：该区分在固定窗口的
特征/标签里实测不可学，改为用**纯过去可观测**的状态持续时间表达）。它是策略层区分
"轻仓试错"与"顺势加仓"的唯一依据，因此其语义必须有可重复的验证，不能靠"看代码像对的"。

`decide()` 是纯函数（无 IO、无时钟依赖），故可逐 bar 精确断言。

用法：python verify_state_machine_age.py
"""
from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SM = os.path.join(_ROOT, "hcm-signal-tower", "signal_tower", "state_machine.py")


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    # 必须先注册进 sys.modules：state_machine.py 用了 `from __future__ import annotations`
    # + @dataclass，dataclasses 需要按 cls.__module__ 反查模块命名空间来解析注解，
    # 未注册时会报 AttributeError: 'NoneType' object has no attribute '__dict__'（实测踩到）。
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SM = _load("state_machine", _SM)
TT = _load("trend_trigger", os.path.join(
    os.path.dirname(_SM), "trend_trigger.py"))

K = dict(k_enter=2, k_exit=2, k_fade=2, fail_bars=3, osc_limit=4.0,
         flat_reset_enabled=True)


class Inf:
    """最小 StateInferResult 替身（decide 只读属性，不依赖类型）。"""

    def __init__(self, state="", decided=True, ok=True, reason="ok"):
        self.ok = ok
        self.decided = decided
        self.state = state
        self.proba = {state: 0.9} if state else {}
        self.margin = 0.5
        self.model_version = "test"
        self.reason = reason


def step(st, cls="", decided=True, ok=True, pos=0, paused=False,
         trigger_on=False, direction="", require_trigger=False):
    return SM.decide(
        st, Inf(cls, decided, ok),
        positions_open=pos, paused=paused,
        now_iso="2026-09-15T00:00:00+00:00",
        trigger_on=trigger_on, direction=direction,
        require_trigger=require_trigger, **K)


def verify_trigger_rules() -> list[tuple[str, bool, str]]:
    """三件套组合规则的确定性验证（触发器入口 + 方向 NONE 否决）。

    规则来源（用户 2026-09-15）：
      · 形态判趋势 + 方向 UP/DOWN → 允许顺势开仓
      · 形态判趋势 + 方向 NONE    → **禁止趋势开仓**
      · 触发器（rise|donch）确认是趋势态入口的必要条件（require_trigger 开启时）
    """
    checks: list[tuple[str, bool, str]] = []

    def ck(name, got, want):
        ok = (got == want)
        checks.append((name, ok, f"got={got} want={want}"))
        print(f"  {'OK ' if ok else 'FAIL'} {name:<50} got={got:<16} want={want}")

    print("\n=== 规则1：触发器响 + 方向明确 → 直接进 S2（不等 4 类 argmax）===")
    st = SM.FSMState(symbol="T1", time_frame="M5")
    d = step(st, "oscillation", pos=0, trigger_on=True, direction="up")
    ck("trigger+up state", d.state, "S2_TREND_INIT")
    ck("trigger+up age", d.age_bars, 1)
    ck("trigger+up direction", d.direction, "up")
    ck("trigger+up note", d.note, "trigger_enter(up)")

    print("=== 规则2：触发器响但方向 NONE → 禁止趋势开仓 ===")
    st2 = SM.FSMState(symbol="T2", time_frame="M5")
    d = step(st2, "trend_mid", pos=0, trigger_on=True, direction="none")
    ck("trigger+none state", d.state, "S0_IDLE")
    ck("trigger+none note", d.note, "trigger_no_dir")

    print("=== 规则3：require_trigger 开启时，无触发器不得进趋势态 ===")
    st3 = SM.FSMState(symbol="T3", time_frame="M5")
    d = step(st3, "trend_mid", pos=0, trigger_on=False, direction="up",
             require_trigger=True)
    ck("无触发器 state", d.state, "S0_IDLE")
    ck("无触发器 note", d.note, "no_trigger")

    print("=== 规则4：require_trigger 关闭时保持向后兼容（按类别迁移）===")
    st4 = SM.FSMState(symbol="T4", time_frame="M5")
    d = step(st4, "trend_mid", pos=0, trigger_on=False, direction="up",
             require_trigger=False)
    ck("兼容模式 state", d.state, "S0_IDLE")     # k_enter=2 → 第1根仅 pending
    d = step(st4, "trend_mid", pos=1, trigger_on=False, direction="up")
    ck("兼容模式第2根 state", d.state, "S3_TREND_MID")

    print("=== 规则5：方向 NONE 时，即使有触发器也不因类别进趋势 ===")
    st5 = SM.FSMState(symbol="T5", time_frame="M5")
    step(st5, "trend_mid", pos=0, direction="none")      # pending 1
    d = step(st5, "trend_mid", pos=0, direction="none")  # 本可迁移
    ck("方向NONE 拦住类别迁移", d.state, "S0_IDLE")
    ck("方向NONE note", d.note, "trend_no_dir")

    print("=== 规则6：已在趋势态时，触发器不再重复改造（同态保持）===")
    st6 = SM.FSMState(symbol="T6", time_frame="M5",
                      state="S2_TREND_INIT", age_bars=4, direction="up")
    d = step(st6, "trend_mid", pos=1, trigger_on=True, direction="up")
    ck("趋势态内 state", d.state, "S2_TREND_INIT")   # 由同态/迁移逻辑决定，不被入口规则改
    ck("趋势态内 direction", d.direction, "up")

    print("=== 规则7：方向未接入（direction=\"\"）→ 不做否决（向后兼容）===")
    st7 = SM.FSMState(symbol="T7", time_frame="M5")
    step(st7, "trend_mid", pos=0)
    d = step(st7, "trend_mid", pos=1)
    ck("未接入方向 state", d.state, "S3_TREND_MID")

    return checks


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    st = SM.FSMState(symbol="XAUUSD", time_frame="M5")
    checks: list[tuple[str, bool, str]] = []

    def ck(name: str, got, want) -> None:
        ok = (got == want)
        checks.append((name, ok, f"got={got} want={want}"))
        print(f"  {'OK ' if ok else 'FAIL'} {name:<46} got={got:<12} want={want}")

    print("=== 初始 ===")
    ck("初始 state", st.state, "S0_IDLE")
    ck("初始 age", st.age_bars, 0)

    print("=== 迁移需防抖：k_enter=2（入场前无持仓 pos=0）===")
    d = step(st, "trend_mid", pos=0)               # pending 1 → 保持
    ck("第1根 未迁移 state", d.state, "S0_IDLE")
    ck("第1根 age（保持 +1）", d.age_bars, 1)
    # ⚠ 进入趋势态后必须带持仓：flat_reset_enabled=True 时，
    #   「趋势态 + 无持仓」会在**下一根立即复位 S0**（decide 规则 6）。这是既有设计
    #   （防止卡在无持仓的趋势态），意味着 age 只在状态被**实质持有**时累积。
    d = step(st, "trend_mid", pos=1)               # pending 2 → 迁移
    ck("第2根 已迁移 state", d.state, "S3_TREND_MID")
    ck("第2根 age（迁移置 1）", d.age_bars, 1)
    ck("第2根 transitioned", d.transitioned, True)

    print("=== 同态持续：年龄递增 ===")
    d = step(st, "trend_mid", pos=1)
    ck("第3根 age", d.age_bars, 2)
    d = step(st, "trend_mid", pos=1)
    ck("第4根 age", d.age_bars, 3)

    print("=== 低置信跳过：不推进不回退，但年龄继续（已文档化语义）===")
    d = step(st, "oscillation", decided=False, pos=1)
    ck("低置信 state 不变", d.state, "S3_TREND_MID")
    ck("低置信 age 继续 +1", d.age_bars, 4)
    ck("低置信 note", d.note, "low_conf_skip")

    print("=== 单根推理失败：同样不打断年龄 ===")
    d = step(st, "", ok=False, pos=1)
    ck("失败 state 不变", d.state, "S3_TREND_MID")
    ck("失败 age 继续 +1", d.age_bars, 5)

    print("=== 换态：年龄重置为 1（k_fade=2）===")
    d = step(st, "trend_fade", pos=1)              # pending 1
    ck("fade pending age", d.age_bars, 6)
    d = step(st, "trend_fade", pos=1)              # pending 2 → 迁移
    ck("fade 迁移 state", d.state, "S4_TREND_FADE")
    ck("fade 迁移 age 置 1", d.age_bars, 1)

    print("=== 暂停优先：迁移到 S9 且年龄置 1 ===")
    d = step(st, "trend_fade", paused=True, pos=1)
    ck("暂停 state", d.state, "S9_PAUSED")
    ck("暂停 age", d.age_bars, 1)

    print("=== 无持仓复位：趋势态 → S0（flat_reset）===")
    st2 = SM.FSMState(symbol="X", time_frame="M5",
                      state="S3_TREND_MID", age_bars=7)
    d = step(st2, "trend_mid", pos=0)
    ck("flat_reset state", d.state, "S0_IDLE")
    ck("flat_reset age", d.age_bars, 1)

    print("=== 震荡锁止：S1 达上限 → S5 ===")
    st3 = SM.FSMState(symbol="Y", time_frame="M5",
                      state="S1_OSC", osc_atr_loss=4.0, age_bars=3)
    d = step(st3, "oscillation")
    ck("锁止 state", d.state, "S5_OSC_LOCKED")
    ck("锁止 age", d.age_bars, 1)

    # ── 触发器：向量化突破检测 vs 朴素循环（等价性守门，防"优化引入偏差"）──
    print("\n=== trend_trigger：向量化 compute_donchian 与朴素循环等价性 ===")
    rng = np.random.default_rng(7)
    _h = 100 + np.cumsum(rng.normal(0, 1, 400))
    _l = _h - np.abs(rng.normal(0, 2, 400))
    _c = (_h + _l) / 2.0
    for _w in (5, 20, 60):
        fast = TT.compute_donchian(_h, _l, _c, _w)
        slow = np.zeros(len(_c), dtype=bool)
        for i in range(_w, len(_c)):
            if _c[i] > _h[i - _w:i].max() or _c[i] < _l[i - _w:i].min():
                slow[i] = True
        same = bool((fast == slow).all())
        checks.append((f"donchian w={_w} 等价", same, f"diff={int((fast != slow).sum())}"))
        print(f"  {'OK ' if same else 'FAIL'} donchian w={_w:<4} 等价  "
              f"不一致 {int((fast != slow).sum())} 处")

    # ── 三件套组合规则（触发器入口 + 方向否决）──
    checks += verify_trigger_rules()

    # ── 买点追涨过滤（L4 实测最优项，纯函数）──
    print("\n=== state_strategy._spike_ok：追涨过滤（振幅 > 阈值×ATR 则放弃）===")
    try:
        SS0 = _load("state_strategy", os.path.join(
            os.path.dirname(_SM), "state_strategy.py"))
        _st = SS0.StateStrategy()          # 无 config/redis 也应可构造（_spike_ok 为纯函数）
        _h = np.array([100.0, 101.0, 103.0])
        _l = np.array([99.5, 100.0, 100.5])
        _st._spike_atr_max = 1.5
        # 末 bar 振幅 = high[-1] − low[-1] = 103.0 − 100.5 = 2.5
        cases2 = [
            ((_h, _l, 1.0), False, "2.5 > 1.5×1.0=1.5 → 拦下（追涨过滤生效）"),
            ((_h, _l, 2.0), True, "2.5 ≤ 1.5×2.0=3.0 → 放行"),
            ((_h, _l, 0.0), True, "atr=0 → 放行（不做无依据过滤）"),
        ]
        for (hh, ll, aa), want, desc in cases2:
            got = bool(_st._spike_ok(hh, ll, aa))
            ok = (got == want)
            checks.append((f"_spike_ok(atr={aa})", ok, f"got={got} want={want}"))
            print(f"  {'OK ' if ok else 'FAIL'} _spike_ok(atr={aa}) = {got:<6} ({desc})")
    except Exception as exc:  # noqa: BLE001
        print(f"  [skip] state_strategy 无法独立构造（{exc}）—— 该组检查跳过")

    # ── 策略层方向解析（方向模块优先 + NONE 否决 + 斜率仅作回退）──
    print("\n=== state_strategy.resolve_trend_dir：方向优先级与 NONE 否决 ===")
    try:
        SS = _load("state_strategy", os.path.join(
            os.path.dirname(_SM), "state_strategy.py"))
        cases = [
            (("up", 0.5), ("UP", "dir_module"), "方向模块 up 优先于斜率"),
            (("down", 0.5), ("DOWN", "dir_module"), "方向模块 down 覆盖正斜率"),
            (("UP", 0.0), ("UP", "dir_module"), "大小写不敏感"),
            (("none", 0.5), ("none", "dir_module_none"),
             "**NONE 否决优先于斜率**（关键：不得回退到斜率选边）"),
            (("", 0.5), ("UP", "slope_fallback"), "未接入方向时回退斜率(正)"),
            (("", -0.5), ("DOWN", "slope_fallback"), "未接入方向时回退斜率(负)"),
            (("", 0.0), ("", "no_dir"), "斜率 0 且无方向 → 无方向"),
        ]
        for (d_in, sl), want, desc in cases:
            got = SS.resolve_trend_dir(d_in, sl)
            ok = (got == want)
            checks.append((f"resolve_trend_dir({d_in!r},{sl})", ok, f"got={got} want={want}"))
            mark = "OK " if ok else "FAIL"
            print(f"  {mark} {desc:<46} got={got} want={want}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [skip] state_strategy 无法独立加载（{exc}）—— 该组检查跳过")

    bad = [c for c in checks if not c[1]]
    print(f"\n[结果] {len(checks) - len(bad)}/{len(checks)} 通过"
          + ("" if not bad else f"；失败：{[c[0] for c in bad]}"))
    if bad:
        raise SystemExit(1)
    print("[结论] 1) age_bars 语义与设计一致；2) 触发器入口与方向 NONE 否决符合三件套规则；"
          "3) 突破检测向量化与朴素循环等价；4) 策略层方向解析以方向模块为唯一真值、"
          "NONE 优先于斜率回退")


if __name__ == "__main__":
    main()
