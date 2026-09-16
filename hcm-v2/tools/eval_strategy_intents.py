"""eval_strategy_intents.py — S1/S2/S3 与买点阈值的数据定标（纯离线，零生产影响）。

目的（对应用户目标"不再人工写阈值"）：把下列**拍脑袋阈值**换成**实测分布**上的取值——
  · `state.osc_box_min_width_atr`（箱体最小宽度）
  · `state.osc_border_tol_atr`   （边界容差）
  · `state.trend.pullback_atr`   （回踩深度）
  · `state.trend.spike_atr_max`  （追涨过滤）
并回答一个更基础的问题：**三条开仓规则到底能不能被触发**（可触达性），
以及各 `reason` 的占比分布（不是"分布好看"，而是"规则通不通"）。

⚠ **必须在洁净数据窗口上跑**：M5 的 `high/low` 在 2026-07-13 ~ 09-06 被伪极值污染
（方案 §36：修复前伪极值率 18.71%、range 中位 11.16 vs 修复后 4.47）。箱体宽度、
bar 振幅、回踩深度**全部由 high/low 计算** → 污染窗口上的分布不可用。
故默认 `--since 2026-09-07`（实测该周起伪极值率 0.00%）。

口径说明（避免"测的不是线上跑的那份"）：
  · 状态序列来自**冻结的 4 类标签**（`build_state_labels` 产物），非模型在线预测 ——
    本脚本测的是**策略层规则**，不是模型；用标签可排除模型误差的干扰。
  · 逐步调用**线上同一函数** `StateStrategy.decide()`（不复制规则）；
    每根 bar 前清空其上下文缓存，测的是**裸规则可触达性**（不带跨 bar 轮次状态）；
    轮次行为（冻结/解冻/上限）由 `verify_state_strategy_osc.py` 覆盖。
  · 方向来自线上同一模块 `trend_direction.compute_direction_series`。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hcm-signal-tower"))

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"
STATE_OF_LABEL = {
    "oscillation": "S1_OSC", "trend_init": "S2_TREND_INIT",
    "trend_mid": "S3_TREND_MID", "trend_fade": "S4_TREND_FADE",
}


def pct(a, ps=(5, 25, 50, 75, 95)) -> str:
    a = np.asarray([x for x in a if np.isfinite(x)], dtype=float)
    if len(a) == 0:
        return "n=0"
    return "  ".join(f"p{p}={np.percentile(a, p):.2f}" for p in ps)


def dist_line(name: str, a, ps=(5, 25, 50, 75, 95)) -> None:
    a = np.asarray([x for x in a if np.isfinite(x)], dtype=float)
    print(f"  {name:<26} n={len(a):<6} 均值={a.mean() if len(a) else float('nan'):+7.3f}  {pct(a, ps)}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5")
    ap.add_argument("--since", default="2026-09-07",
                    help="洁净窗口起点（伪极值率 0.00% 起），默认 2026-09-07")
    ap.add_argument("--labels", default="_scratch/state_M5_v2.csv")
    ap.add_argument("--thr", type=float, default=1.0, help="方向模块 slope_thr_atr")
    ap.add_argument("--k", type=int, default=3, help="方向模块 debounce_bars")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from signal_tower import state_features as SF
    from signal_tower import state_strategy as SS
    from signal_tower import trend_direction as TD

    # ── 数据 ──
    conn = psycopg2.connect(args.db_url)
    kl = pd.read_sql(
        "SELECT open_time, high, low, close FROM hcm_market.klines "
        "WHERE symbol=%s AND time_frame=%s AND open_time >= %s ORDER BY open_time",
        conn, params=(args.symbol, args.tf, args.since))
    conn.close()
    if len(kl) < 200:
        raise SystemExit(f"[fatal] 洁净窗口仅 {len(kl)} 根，不足以定标（可放宽 --since 但须承担污染）")
    kl["open_time"] = pd.to_datetime(kl["open_time"], utc=True)
    h = kl["high"].astype(float).to_numpy()
    l = kl["low"].astype(float).to_numpy()
    c = kl["close"].astype(float).to_numpy()
    # open_time → Unix 秒（UTC）：**必须用 `build_state_labels.epoch_s`**（全仓唯一实现）。
    # ⚠ 【2026-09-15 实测修复】此前两处都手写 `//1e9`。虽然"两侧同公式"让标签对齐仍能
    #   命中（自洽，故本脚本没报错），但有两处实质错误：
    #     ① `ep` 还会传给 `SF.compute_features_at` 算 session 特征 → 与**线上推理口径
    #        （正确秒）不一致**，测的就不是线上跑的那份；
    #     ② "千秒"使**同组 3~4 根 bar 塌缩成同一个值** → `searchsorted` 可能把标签对到
    #        组内的**相邻 bar**（M5 最多偏 3 根，且静默 —— 正是 epoch_s docstring 记录的坑）。
    import importlib.util as _ilu
    _sp = _ilu.spec_from_file_location(
        "build_state_labels", os.path.join(HERE, "build_state_labels.py"))
    _BSL = _ilu.module_from_spec(_sp)
    sys.modules.setdefault("build_state_labels", _BSL)
    _sp.loader.exec_module(_BSL)
    ep = _BSL.epoch_s(kl["open_time"])
    print(f"[data] {args.symbol} {args.tf} 洁净窗口 {kl['open_time'].iloc[0]} .. "
          f"{kl['open_time'].iloc[-1]}  共 {len(kl)} 根")

    # ── 标签对齐 ──
    lab = pd.read_csv(args.labels)
    lab_ep = _BSL.epoch_s(lab["open_time"])
    lab_ok = lab["label_id"].notna().to_numpy()
    lab_nm = lab["label_name"].astype(str).to_numpy()
    pos = np.searchsorted(lab_ep, ep)
    hit = (pos < len(lab_ep)) & lab_ok[np.minimum(pos, len(lab_ep) - 1)]
    hit &= (lab_ep[np.minimum(pos, len(lab_ep) - 1)] == ep)
    state_of = np.array([""] * len(ep), dtype=object)
    for i in np.where(hit)[0]:
        state_of[i] = STATE_OF_LABEL.get(lab_nm[pos[i]], "")
    print(f"[label] 命中 {int((state_of != '').sum())}/{len(ep)} 根")

    # ── 指标 + 方向 ──
    ind = SF.compute_indicators(h, l, c, None)
    dirs = TD.compute_direction_series(h, l, c, ind=ind,
                                       cfg={"state.dir.slope_thr_atr": args.thr,
                                            "state.dir.debounce_bars": args.k})
    dir_names = np.array([TD.dir_name(int(x)) for x in dirs["confirmed"]], dtype=object)
    dir_ok = dirs["valid"]

    strat = SS.StateStrategy(config_provider=None, redis_client=None)
    need = SF.min_bars(None)

    reasons: dict = {}
    st_cnt: dict = {}
    box_w_atr, border_lo, border_up, spike_ratio, pull_depth = [], [], [], [], []
    s1_touch_lo = s1_touch_up = s1_inside = s1_narrow = 0
    s1_total = 0

    for i in range(need - 1, len(ep)):
        if not state_of[i]:
            continue
        feat = SF.compute_features_at(i, h, l, c, ind, ep, None)
        if feat is None:
            continue
        atr = float(feat["atr_14"])
        if atr <= 0.0:
            continue
        fsm = str(state_of[i])
        st_cnt[fsm] = st_cnt.get(fsm, 0) + 1
        dname = str(dir_names[i]) if bool(dir_ok[i]) else "none"
        strat._cache.clear()          # 测裸规则：不带跨 bar 轮次状态
        it = await strat.decide(args.symbol, fsm, high=h[:i + 1], low=l[:i + 1],
                                close=c[:i + 1], atr=atr,
                                slope=float(feat["slope_linreg"]),
                                positions_open=0, direction=dname)
        reasons[it.reason] = reasons.get(it.reason, 0) + 1

        w = strat.box_window_for(args.symbol)
        up, lo_, mid = SS.compute_entry_box(h[:i + 1], l[:i + 1], w)
        if up > 0:
            width = up - lo_
            spike_ratio.append((float(h[i]) - float(l[i])) / atr)
            if fsm == "S1_OSC":
                s1_total += 1
                box_w_atr.append(width / atr)
                border_lo.append((float(c[i]) - lo_) / atr)
                border_up.append((up - float(c[i])) / atr)
                tol = strat.tuning["border_tol_atr"] * atr
                if width < strat.tuning["box_min_width_atr"] * atr:
                    s1_narrow += 1
                elif float(c[i]) <= lo_ + tol:
                    s1_touch_lo += 1
                elif float(c[i]) >= up - tol:
                    s1_touch_up += 1
                else:
                    s1_inside += 1
            if fsm in ("S2_TREND_INIT", "S3_TREND_MID") and dname in ("up", "down"):
                w_pull = (strat.tuning["pullback_window"]
                          or strat.box_window_for(args.symbol))
                seg_h = float(max(h[max(0, i + 1 - w_pull): i + 1]))
                seg_l = float(min(l[max(0, i + 1 - w_pull): i + 1]))
                pull_depth.append(((seg_h - float(c[i])) if dname == "up"
                                   else (float(c[i]) - seg_l)) / atr)

    tot = sum(st_cnt.values()) or 1
    print("\n=========== 状态占比（标签口径）===========")
    for k_, v in sorted(st_cnt.items()):
        print(f"  {k_:<16}{v:>7}  {v / tot:6.1%}")

    print("\n=========== 方向模块分布（thr=%.2f k=%d）===========" % (args.thr, args.k))
    for nm in ("up", "down", "none"):
        print(f"  {nm:<6}{float(np.mean(dir_names == nm)):6.1%}")

    print("\n=========== S1 箱体（ATR 倍数）===========")
    dist_line("箱体宽度/ATR", box_w_atr)
    dist_line("下沿距离 (c-lo)/ATR", border_lo)
    dist_line("上沿距离 (up-c)/ATR", border_up)
    print(f"\n  S1 样本={s1_total}：窄箱拒={s1_narrow}({s1_narrow / max(1, s1_total):.1%}) "
          f"触下沿={s1_touch_lo}({s1_touch_lo / max(1, s1_total):.1%}) "
          f"触上沿={s1_touch_up}({s1_touch_up / max(1, s1_total):.1%}) "
          f"箱内={s1_inside}({s1_inside / max(1, s1_total):.1%})")

    print("\n=========== 买点输入分布（用于 revert 阈值）===========")
    dist_line("bar 振幅/ATR (spike)", spike_ratio)
    dist_line("回踩深度/ATR (pullback)", pull_depth)

    print("\n=========== reason 分布（全样本）===========")
    for k_, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {k_:<28}{v:>7}  {v / tot:6.1%}")

    print("\n  ⚠ 口径：状态来自冻结标签（非在线预测）；positions_open 恒 0；"
          "每 bar 清上下文 → 测裸规则可触达性。轮次行为见 verify_state_strategy_osc.py。")


if __name__ == "__main__":
    asyncio.run(main())
