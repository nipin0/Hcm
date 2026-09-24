#!/usr/bin/env python3
"""eval_range_box_window.py — RANGE（Magic=55）**箱体窗口**扫描（只读，离线）

回答：「RANGE 注入单的箱体用 30 bar 是否更好？」

**为什么必须复用生产实现**：`range_box.py` 是箱体几何的**唯一实现**（它自己记录了
"同一区间口径曾有 6 套"的历史）。故本脚本**逐次调用 `range_box.compute_box`**，
只改 `range.box.window` / `window_slow` 两个配置值 —— 结论可直接映射成一条 `set_cfg.py`。

**当前接线（读码确认，决定了"改哪一侧"）**：
  · `range.box.window`(fast,12)     → **只影响"贴边方向判定"**（`confirm_direction`）
  · `range.box.window_slow`(slow,50)→ **宽度门**（`gate_scale=slow`）+ **突破熔断**（`break_source=box`）
  ⇒ 所以"箱体用 30 bar"有两义，本脚本把两义都测。

模拟口径（尽量贴生产；**明确声明未含**的部分见下）：
  ① 双尺度箱体（快/慢，quantile 95/5，exclusive）—— 复用 `range_box.compute_box`
  ② 宽度门 —— 复用 `range_box.scales_gate`
  ③ 贴边方向 —— 复用 `range_box.confirm_direction`（BUY 贴下沿 / SELL 贴上沿）
  ④ 等回踩 —— `range.entry_offset_atr=1.0`：先 arm 目标位，≤12 根内被触价才成交
  ⑤ 双障碍 —— TP=`range.tp_atr`(1.0)ATR vs SL=**待测** ATR；**同根双触保守计负**
  ⑥ 成本 —— 扣 `--cost-atr`(0.12) 往返
  ⚠ **未含**（诚实声明）：破箱熔断、单仓闸、下游 AI/风控链。
    其中"破箱熔断"在 `slow` 固定时对各档**同口径**（`slow` 变化档会受影响）；后两者在塔外。

判据（写死，防事后挑选）：某档要在 **E[R] 优于基线 (12,50)** 且**信号数不塌缩**
（≥ 基线的 50%）时才判"更好"；两者冲突时以 E[R] 为准并如实标注信号数代价。

用法：
  python tools/eval_range_box_window.py
  python tools/eval_range_box_window.py --pairs 12x50,30x50,12x30,30x30 --sl-atr 1.0,2.0
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import numpy as np
import psycopg2

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
_SIG = os.path.join(_ROOT, "hcm-signal-tower", "signal_tower")


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


SF = _load("state_features", os.path.join(_SIG, "state_features.py"))
RB = _load("range_box", os.path.join(_SIG, "range_box.py"))
RS = _load("range_strategy", os.path.join(_SIG, "range_strategy.py"))
BSL = _load("build_state_labels", os.path.join(_TOOLS, "build_state_labels.py"))


def load_pg_cfg(conn, keys) -> dict:
    """从配置中心读 `range.*` 现行值（**与 scheduler 同源**，避免用 DEFAULTS 冒充生产）。"""
    db = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT config_key, current_value FROM hcm_config.metadata "
                        "WHERE config_key = ANY(%s)", (list(keys),))
            for k, v in cur.fetchall():
                db[k] = v
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 配置读取失败，回落 DEFAULTS：{exc}", file=sys.stderr)
    return db


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", BSL.DB_URL_DEFAULT))
    ap.add_argument("--pairs", default="12x50,20x50,30x50,50x50,12x30,30x30",
                    help="快箱x慢箱 组合（逗号分隔），第一项视为基线")
    ap.add_argument("--sl-atr", default="1.0,2.0", dest="sl_atr",
                    help="待测 SL（ATR 倍数；生产 `range.sl_atr=0` 交回会话宽止损）")
    ap.add_argument("--cost-atr", type=float, default=0.12, dest="cost_atr",
                    help="往返成本（ATR）：点差+滑点（与 range_strategy 文档同口径）")
    ap.add_argument("--exit-cap", type=int, default=24, dest="exit_cap",
                    help="入场后最多观察 N 根；到期未触任一障碍记 open")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    pairs = []
    for p in args.pairs.split(","):
        a, b = p.strip().lower().split("x")
        pairs.append((int(a), int(b)))
    sl_list = [float(x) for x in args.sl_atr.split(",") if x.strip()]

    # 配置 = `range_box.DEFAULTS` ∪ `range_strategy.DEFAULTS`（**两类都要**：
    #   箱体几何键在 RB，入场偏移/TP/arm 键在 RS），再以**配置中心现行值**覆盖。
    cfg = {**RS.DEFAULTS, **RB.DEFAULTS}
    conn = psycopg2.connect(args.db_url)
    try:
        cfg_db = load_pg_cfg(conn, list(cfg.keys()))
        kl = BSL.load_klines(conn, args.symbol, args.tf)
    finally:
        conn.close()
    for k in list(cfg.keys()):
        v = cfg_db.get(k)
        if v is not None and str(v).strip() != "":
            cfg[k] = v
    print(f"[cfg] 生产现行：window={cfg['range.box.window']} window_slow={cfg['range.box.window_slow']} "
          f"gate_scale={cfg['range.box.gate_scale']} edge_tol_atr={cfg['range.box.edge_tol_atr']} "
          f"width=[{cfg['range.width_min_atr']}, {cfg['range.width_max_atr']}] "
          f"bands={cfg['range.box.bands_mode']} excl={cfg['range.box.exclusive']}")

    high = kl["high"].to_numpy(dtype=float)
    low = kl["low"].to_numpy(dtype=float)
    close = kl["close"].to_numpy(dtype=float)
    atr = SF.compute_indicators(high, low, close, dict(SF.DEFAULT_PARAMS))["atr"]
    n = len(close)
    off = float(cfg["range.entry_offset_atr"])
    tp_atr = float(cfg["range.tp_atr"])
    arm_exp = int(cfg["range.arm_expire_bars"])
    cost = float(args.cost_atr)
    wins = sorted({w for pr in pairs for w in pr})
    i0 = max(wins + [SF.min_bars(dict(SF.DEFAULT_PARAMS))]) + 2
    print(f"[data] {args.symbol} {args.tf} bars={n} 可评估区间 i∈[{i0}, {n - args.exit_cap - 1}] "
          f"窗口集合={wins} 对偶={pairs}")

    # 统计容器：stats[(pair_idx, sl)] = [信号, arm未触, win, loss, open, sum_R]
    stats = {(pi, s): [0, 0, 0, 0, 0, 0.0] for pi in range(len(pairs)) for s in sl_list}

    box_cache: dict = {}

    def _box(w, i, mode=None):
        key = (w, i, mode)
        if key in box_cache:
            return box_cache[key]
        sub = slice(max(0, i - w), i + 1)
        c = dict(cfg)
        c["range.box.window"] = w
        c["range.box.window_slow"] = w
        b = RB.compute_box(high[sub], low[sub], close[sub], float(atr[i]), c,
                           scale="fast", bands_mode=mode)
        box_cache[key] = b
        return b

    for i in range(i0, n - args.exit_cap - 1):
        a = float(atr[i])
        if not np.isfinite(a) or a <= 0:
            continue
        for pi, (wf, ws) in enumerate(pairs):
            fast = _box(wf, i)
            slow = _box(ws, i)
            if not fast.valid or not slow.valid:
                continue
            ok, _why = RB.scales_gate(fast, slow, cfg)
            if not ok:
                continue
            buy = bool(RB.confirm_direction(fast, "BUY", cfg)[0])
            sell = bool(RB.confirm_direction(fast, "SELL", cfg)[0])
            dirs = ([("BUY", buy)] if buy and not sell else
                    [("SELL", sell)] if sell and not buy else [])
            for side, _ in dirs:
                # 等回踩：arm 目标位（≤ arm_exp 根内被触价才成交）
                tgt = close[i] - off * a if side == "BUY" else close[i] + off * a
                j0 = None
                for j in range(i + 1, min(i + 1 + arm_exp, n)):
                    if (side == "BUY" and low[j] <= tgt) or (side == "SELL" and high[j] >= tgt):
                        j0 = j
                        break
                for s in sl_list:
                    st = stats[(pi, s)]
                    if j0 is None:
                        st[1] += 1
                        continue
                    st[0] += 1
                    tp_p = tgt + tp_atr * a if side == "BUY" else tgt - tp_atr * a
                    sl_p = tgt - s * a if side == "BUY" else tgt + s * a
                    res = "open"
                    for j in range(j0, min(j0 + args.exit_cap, n)):
                        if side == "BUY":
                            hit_tp, hit_sl = high[j] >= tp_p, low[j] <= sl_p
                        else:
                            hit_tp, hit_sl = low[j] <= tp_p, high[j] >= sl_p
                        if hit_sl:
                            res = "loss"          # 同根双触 → 保守计负
                            break
                        if hit_tp:
                            res = "win"
                            break
                    if res == "win":
                        st[2] += 1
                        st[5] += tp_atr - cost
                    elif res == "loss":
                        st[3] += 1
                        st[5] += -s - cost
                    else:
                        st[4] += 1
        # 缓存只服务于**当前 bar**（同一 bar 内 wf==ws 时复用）⇒ 每根清空，防 57k×W 累积占内存
        box_cache.clear()

    base = pairs[0]
    for s in sl_list:
        print(f"\n=========== SL = {s:.1f}×ATR（TP={tp_atr:.1f}×ATR，扣 {cost:.2f}ATR 成本，"
              f"双触同根计负，{args.exit_cap} 根未决记 open）===========")
        print(f"  {'快x慢':<10}{'信号':>8}{'arm未触':>9}{'已决':>8}{'TP先到率':>10}"
              f"{'未决率':>9}{'E[净R]':>10}{'总净R':>10}{'vs基线':>10}   判读")
        b_st = stats[(0, s)]
        b_dec = b_st[2] + b_st[3]
        b_e = (b_st[5] / b_dec) if b_dec else float("nan")
        for pi, pr in enumerate(pairs):
            st = stats[(pi, s)]
            dec = st[2] + st[3]
            tot = st[2] + st[3] + st[4]
            e = (st[5] / dec) if dec else float("nan")
            tag = ""
            if pi > 0 and dec >= 30 and np.isfinite(e) and np.isfinite(b_e):
                better = e > b_e
                collapse = st[0] < 0.5 * max(1, b_st[0])
                tag = ("**更好**" if better else "更差") + ("（信号塌缩）" if collapse else "")
            mark = " *基线*" if pi == 0 else ""
            print(f"  {pr[0]}x{pr[1]:<7}{st[0]:>8}{st[1]:>9}{dec:>8}"
                  f"{(st[2] / dec if dec else float('nan')):>10.1%}"
                  f"{(st[4] / tot if tot else float('nan')):>9.1%}"
                  f"{e:>10.4f}{st[5]:>10.2f}"
                  f"{(e - b_e if np.isfinite(e) and np.isfinite(b_e) else float('nan')):>+10.4f}"
                  f"   {tag}{mark}")

    print("\n=========== 读法与边界 ===========")
    print("  · `信号` = arm 后被触价成交的次数（不含 arm 未触）；`已决` = TP/SL 已触；")
    print("    `未决` = 到观察上限仍未触任一障碍（**既未计盈也未计亏**，故 E[净R] 为条件期望）。")
    print("  · `快箱` = `range.box.window`（**只影响贴边方向**）；`慢箱` = `range.box.window_slow`")
    print("    （影响**宽度门**与突破熔断）—— 生产 `gate_scale=slow` / `break_source=box`。")
    print("  · **未含**：破箱熔断、单仓闸、下游 AI/风控链 ⇒ 本表只回答"
          "「箱体窗口对入场质量的影响」，不等于实盘期望；")
    print("    SL 的「会话宽止损」需另用 `close.<session>.trailing_stop_distance` 代入复算。")


if __name__ == "__main__":
    main()
