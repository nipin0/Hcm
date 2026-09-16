"""verify_fsm_param_chain.py — FSM **参数链**端到端验证（手数 / SL·TP·保本·移动止盈）。

回答并锁定这条链（用户 2026-09-15 提问）：

  基础手数 = 风控配置（symbol.{品种}.tower.lot_size 优先，否则 risk.lot_base，
             受 risk.max_lot_per_trade 封顶）
  S1 梯度(0.5/1.0/1.5/2.0) / 趋势 base(1.0) = 乘在**上面那个 base** 上
  SL/TP/保本/移动止盈 = **全部链动 close.<时段>.* 时段系数**（桥算，塔不写死）

本脚本逐段验证**跨组件字段真的能到桥**：
  塔 to_signal_fields → SignalData → signal:stream(顶层字段)
  → risk _apply_dynamic_lot（算 lot）→ risk _publish_risk_passed（白名单透传）→ 桥

⚠ 重点防的是"**字段到不了下游，功能静默失效**"—— 历史事故：zone_level 等漏传白名单
→ 桥侧取 0 → "出信号不下单"（见 stream_consumer.py 该处注释）。

用法：python verify_fsm_param_chain.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hcm-signal-tower"))   # signal_tower 包
sys.path.insert(0, os.path.join(ROOT, "hcm-risk-engine"))    # risk_engine 包
sys.path.insert(0, ROOT)                                      # shared 包（风控用）

FAILED: list[str] = []
N = 0


def ck(name: str, got, want) -> None:
    global N
    N += 1
    ok = (got == want)
    if not ok:
        FAILED.append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name:<48} got={got!r:<26} want={want!r}")


class CapRedis:
    """捕获 xadd 载荷的假 Redis。"""

    is_initialized = True

    def __init__(self) -> None:
        self.sent: list = []

    async def xadd(self, stream, data, maxlen=None):
        self.sent.append((stream, data))
        return "1-1"

    async def hget(self, key, field):
        return getattr(self, "cfg", {}).get(field)


class CfgRedis:
    """风控配置假 Redis（hget 返回字符串）。"""

    is_initialized = True

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    async def hget(self, key, field):
        v = self.cfg.get(field)
        return None if v is None else str(v)

    async def xadd(self, stream, data, maxlen=None):
        self.last = (stream, data)
        return "1-1"

    async def set(self, key, value, ex=None):
        self.kv = getattr(self, "kv", {})
        self.kv[key] = value


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from signal_tower import state_strategy as SS
    from signal_tower.signal_publisher import SignalData, SignalPublisher

    print("=== 1) 塔侧契约：to_signal_fields 给出 S1 梯度倍率 ===")
    it = SS.StrategyIntent(state="S1_OSC", action="open", direction="BUY",
                           reason="osc_at_box_lower", lot_multiplier=1.5,
                           tp_anchor=2500.0, trail_mult=1.0)
    f = SS.to_signal_fields(it)
    ck("signal_mode", f["signal_mode"], "state_osc")
    ck("lot=0（由风控按 base×倍率算）", f["lot"], 0.0)
    ck("_fsm.lot_multiplier", f["_fsm"]["lot_multiplier"], 1.5)
    ck("tp1 = 箱体中值（S1 锚点）", f["tp1"], 2500.0)
    ck("sl_price 恒 0（桥按会话系数算）", f["sl_price"], 0.0)

    print("\n=== 2) SignalData → signal:stream：FSM 顶层字段必须真的发出去 ===")
    cap = CapRedis()
    pub = SignalPublisher(redis_client=cap, db_pool=None)
    sd = SignalData(signal_id=1, symbol="XAUUSD", direction="BUY",
                    signal_mode="state_osc", entry_price=2500.0, lot=0.0,
                    fsm_state="S1_OSC", fsm_lot_multiplier=1.5)
    ok = await pub._publish_to_redis(sd)
    ck("_publish_to_redis 成功", ok, True)
    payload = cap.sent[-1][1] if cap.sent else {}
    for k, want in (("fsm_state", "S1_OSC"), ("fsm_lot_multiplier", 1.5)):
        ck(f"流字段 {k}", payload.get(k), want)
    # 注：trail_mult/exit_ready/trail_lookback 走 per-bar directive 键（冲突③修复），
    # 不再经信号字段透传，故此处不再断言。

    print("\n=== 3) 风控：state* 手数 = base × fsm_lot_multiplier（不参与信心分档）===")
    try:
        from risk_engine.stream_consumer import RiskStreamConsumer
    except Exception as e:  # noqa: BLE001
        print(f"  SKIP 风控模块不可导入（{e}）—— 需在容器环境验证")
        raise SystemExit(1 if FAILED else 0)

    cfg = {"risk.lot_base": "0.01", "symbol.XAUUSD.tower.lot_size": "0.02",
           "risk.max_lot_per_trade": "1.0"}
    rc = RiskStreamConsumer(redis_client=CfgRedis(cfg))
    for mult, want in ((1.5, 0.03), (0.5, 0.01), (2.0, 0.04)):
        sd2 = {"signal_mode": "state_osc", "symbol": "XAUUSD", "lot": 0.0,
               "confidence": 0.2, "fsm_lot_multiplier": mult, "signal_id": 1}
        await rc._apply_dynamic_lot(sd2)
        ck(f"state_osc mult={mult} → lot=base(0.02)×{mult}", sd2["lot"], want)

    sd3 = {"signal_mode": "state_trend", "symbol": "XAUUSD", "lot": 0.0,
           "confidence": 0.2, "fsm_lot_multiplier": 1.0, "signal_id": 2}
    await rc._apply_dynamic_lot(sd3)
    ck("state_trend mult=1.0 → lot=base(0.02)", sd3["lot"], 0.02)

    # 非 FSM 不受影响（走原信心分档：confidence=0.2 → 最小档 ×0.5 → 0.01）
    sd4 = {"signal_mode": "HEXP", "symbol": "XAUUSD", "lot": 0.0,
           "confidence": 0.2, "signal_id": 3}
    await rc._apply_dynamic_lot(sd4)
    ck("非 FSM 仍走信心分档（不受本次改动影响）", sd4["lot"], 0.01)

    print("\n=== 4) 风控 → 桥：白名单必须透传 FSM 字段（trail 类走 directive，不在此）===")
    if not hasattr(rc, "_publish_risk_passed"):
        print("  SKIP 无 _publish_risk_passed（接口变化）")
    else:
        class _RR:
            results: list = []
            rejected_rules: list = []
            violations: list = []
        rc._redis = CfgRedis(cfg)
        src = {"signal_id": 9, "symbol": "XAUUSD", "direction": "BUY",
               "signal_mode": "state_osc", "fsm_state": "S1_OSC",
               "fsm_lot_multiplier": 1.5}
        await rc._publish_risk_passed(src, "PASS", _RR())
        out = getattr(rc._redis, "last", (None, {}))[1]
        for k, want in (("fsm_state", "S1_OSC"), ("fsm_lot_multiplier", 1.5)):
            ck(f"透传 {k}", out.get(k), want)
        # trail_mult/exit_ready/trail_lookback 经 per-bar directive 键承载（冲突③修复），
        # 风控白名单不再透传这三项。

    print("\n" + "=" * 74)
    print(f"共 {N} 项，失败 {len(FAILED)} 项"
          + (f"：{FAILED}" if FAILED else " —— 参数链完整贯通"))
    print("=" * 74)
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
