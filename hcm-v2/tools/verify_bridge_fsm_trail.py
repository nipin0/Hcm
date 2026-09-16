"""verify_bridge_fsm_trail.py — 桥侧 FSM 移动止损分支的**上线前验证**（§43.4 (c)）。

验证三件事：
  1. `_fsm_clamp_trail_sl`（纯函数）语义正确：不许越过市价、留最小距离；
  2. `_fsm_read_directive` **失败一律返回 {}**（塔失联时不改变止损行为）；
  3. **非 FSM 持仓完全不受影响**（`pos.magic not in (61,62)` → 不读指令、用原 trail_wide）。

⚠ 本脚本只验**纯逻辑**；真实 MT5 交互（order_send）必须靠灰度小仓位实测。

用法：python verify_bridge_fsm_trail.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILED: list[str] = []
N = 0


def ck(name, got, want) -> None:
    global N
    N += 1
    ok = (got == want)
    if not ok:
        FAILED.append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name:<46} got={got!r:<18} want={want!r}")


class FakeRedis:
    is_initialized = True

    def __init__(self, kv=None, boom=False):
        self.kv = kv or {}
        self.boom = boom

    def get(self, k):
        if self.boom:
            raise RuntimeError("redis down")
        return self.kv.get(k)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # 按路径加载（避免 import 期副作用扩散）
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "stw_bridge_check", os.path.join(HERE, "mt5_bridge.py"))
    if spec is None or spec.loader is None:
        print("[fatal] 无法加载 mt5_bridge.py")
        raise SystemExit(1)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["stw_bridge_check"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        print(f"[skip] mt5_bridge 导入失败（{type(e).__name__}: {e}）"
              "—— 需在有 MetaTrader5 的桥主机上验证")
        raise SystemExit(0)

    print("=== 1) _fsm_clamp_trail_sl（纯函数：不许越过市价 + 最小距离）===")
    cl = mod._fsm_clamp_trail_sl
    # 多头：候选合理（低于市价 5、min_dist=1）→ 不动
    ck("多头 候选(100) 市价(110) min=1 → 保持", cl("BUY", 100.0, 110.0, 1.0), 100.0)
    # 多头：候选 109.5 距市价仅 0.5 < min_dist=1 → 压到 109
    ck("多头 候选(109.5) 市价(110) min=1 → 压到 109", cl("BUY", 109.5, 110.0, 1.0), 109.0)
    # 多头：候选越到市价上方（规格字面口径的典型产物）→ 压到 109
    ck("多头 候选(120) 市价(110) min=1 → 压到 109", cl("BUY", 120.0, 110.0, 1.0), 109.0)
    # 空头对称
    ck("空头 候选(120) 市价(110) min=1 → 保持", cl("SELL", 120.0, 110.0, 1.0), 120.0)
    ck("空头 候选(110.5) 市价(110) min=1 → 抬到 111", cl("SELL", 110.5, 110.0, 1.0), 111.0)
    ck("空头 候选(100) 市价(110) min=1 → 抬到 111", cl("SELL", 100.0, 110.0, 1.0), 111.0)
    ck("未知方向 → 原样", cl("", 100.0, 110.0, 1.0), 100.0)

    print("\n=== 2) _fsm_read_directive：失败一律 {}（塔失联不改变止损行为）===")
    rd = mod._fsm_read_directive
    ck("键缺失 → {}", rd(FakeRedis({}), "XAUUSD"), {})
    ck("Redis 抛异常 → {}", rd(FakeRedis(boom=True), "XAUUSD"), {})
    ck("非法 JSON → {}", rd(FakeRedis({"hcm:state:directive:XAUUSD": "not-json"}), "XAUUSD"), {})
    ck("非 dict（JSON 数组）→ {}",
       rd(FakeRedis({"hcm:state:directive:XAUUSD": "[1,2]"}), "XAUUSD"), {})
    good = rd(FakeRedis({"hcm:state:directive:XAUUSD":
                         '{"state":"S4_TREND_FADE","trail_mult":0.5,"exit_ready":true}'}),
              "XAUUSD")
    ck("正常读 → trail_mult", good.get("trail_mult"), 0.5)
    ck("正常读 → exit_ready", good.get("exit_ready"), True)

    print("\n=== 3) magic 白名单：只有 61/62 走 FSM 分支 ===")
    ck("_FSM_MAGICS", mod._FSM_MAGICS, (61, 62))
    ck("hexp(11) 不在白名单", 11 in mod._FSM_MAGICS, False)
    ck("state_osc(61) 在白名单", 61 in mod._FSM_MAGICS, True)
    ck("state_trend(62) 在白名单", 62 in mod._FSM_MAGICS, True)

    print("\n=== 3b) 新 magic 布局：按前导逻辑码识别 + 向后兼容 ===")
    # 【为什么必须断言"加载成功"】`_state_strategy_mod()` 失败会**静默降级**为
    # "只认裸 61/62" —— 于是**新格式的 FSM 单会被判成非 FSM 单**：
    #   ④ 移动止损不收紧、§57 箱体突破离场指令匹配不上 → 功能静默失效。
    ck("state_strategy 按路径加载成功", mod._state_strategy_mod() is not None, True)
    ck("新格式 61010100 是 FSM", mod._is_fsm_magic(61010100), True)
    ck("新格式 62021100 是 FSM", mod._is_fsm_magic(62021100), True)
    ck("历史裸 61 是 FSM（兼容）", mod._is_fsm_magic(61), True)
    ck("历史裸 62 是 FSM（兼容）", mod._is_fsm_magic(62), True)
    ck("hexp 11 不是 FSM", mod._is_fsm_magic(11), False)
    ck("range 55 不是 FSM", mod._is_fsm_magic(55), False)
    ck("logic·新 osc", mod._fsm_magic_logic(61010100), "osc")
    ck("logic·新 trend", mod._fsm_magic_logic(62021100), "trend")
    ck("logic·裸 61 → osc", mod._fsm_magic_logic(61), "osc")
    ck("logic·hexp → 空", mod._fsm_magic_logic(11), "")

    print("\n" + "=" * 70)
    print(f"共 {N} 项，失败 {len(FAILED)} 项"
          + (f"：{FAILED}" if FAILED else " —— 纯逻辑验证通过"))
    print("=" * 70)
    print("⚠ 真实下单/改单行为需灰度小仓位实测（本脚本不覆盖 order_send）。")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
