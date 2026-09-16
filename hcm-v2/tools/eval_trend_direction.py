"""eval_trend_direction.py — 方向规则模块的离线验证与阈值标定（只读，零生产影响）。

判据（唯一有意义的判据，不是"分布好看"）：
    1. **前视边际**：判 UP 之后未来 N 根位移（ATR 归一）是否显著 > 0；判 DOWN 是否 < 0。
       单看均值会被长尾骗（少数大赢家拉高均值），故**必须同时看中位数与上涨占比**。
    2. **NONE 是否真在过滤**：NONE 组未来 |位移| 应明显小于无条件 |位移|。
    3. **换手率**：方向翻转频率（过多→抖动，过少→滞后）。

两种模式：
    (A) 单周期：`--tf M5`            —— 方向与评估在同一周期。
    (B) 多周期：`--tf M5 --dir-tf H1` —— 用 H1 判方向、在 M5 上评估（FSM 的"大周期定方向、
        小周期定入场"用法）。对齐口径：M5 第 t 根只能看到**收盘时间 ≤ 其开盘时间**的 H1 bar
        （严格前视闭合，无泄露）。

用法：
    python eval_trend_direction.py --symbol XAUUSD --tf M5
    python eval_trend_direction.py --symbol XAUUSD --tf M5 --dir-tf H1
    python eval_trend_direction.py --symbol XAUUSD --tf M5 --sweep
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
_SIG = os.path.join(_ROOT, "hcm-signal-tower", "signal_tower")

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"
TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600}


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SF = _load("state_features", os.path.join(_SIG, "state_features.py"))
TD = _load("trend_direction", os.path.join(_SIG, "trend_direction.py"))
BSL = _load("build_state_labels", os.path.join(_TOOLS, "build_state_labels.py"))


# 时间→秒的换算**统一委托 build_state_labels.epoch_s**（单一真值，含单位陷阱说明）
epoch_s = BSL.epoch_s


def forward_shift(close: np.ndarray, atr: np.ndarray, horizon: int) -> np.ndarray:
    """未来 horizon 根的 ATR 归一位移；不足处为 NaN。"""
    n = len(close)
    out = np.full(n, np.nan)
    ok = np.arange(n) + horizon < n
    idx = np.arange(n)[ok]
    out[ok] = (close[idx + horizon] - close[idx]) / atr[idx]
    return out


def class_stats(mask: np.ndarray, shift: np.ndarray, baseline: dict) -> dict | None:
    """单类前视统计。均值/中位数/上涨占比/|位移| 全给 —— 只看均值会被长尾误导。"""
    m = mask & np.isfinite(shift)
    if m.sum() < 30:
        return None
    d = shift[m]
    return {
        "n": int(m.sum()),
        "mean_atr": float(d.mean()),
        "median_atr": float(np.median(d)),
        "abs_mean_atr": float(np.abs(d).mean()),
        "up_rate": float((d > 0).mean()),
        # 相对基线的边际（百分点）：up 应 > 0，down 应 < 0
        "up_rate_edge": float((d > 0).mean() - baseline["up_rate"]),
    }


def summarize(conf: np.ndarray, valid: np.ndarray, shift: np.ndarray,
              dir_codes: tuple = (TD.DIR_UP, TD.DIR_DOWN, TD.DIR_NONE)) -> dict:
    base_m = valid & np.isfinite(shift)
    if base_m.sum() < 30:
        return {"error": "有效样本不足"}
    d = shift[base_m]
    baseline = {"n": int(base_m.sum()), "up_rate": float((d > 0).mean()),
                "abs_mean_atr": float(np.abs(d).mean())}
    out = {"baseline": baseline, "dist": {}, "stats": {}}
    for code, nm in zip(dir_codes, ("up", "down", "none")):
        mask = valid & (conf == code)
        out["dist"][nm] = int(mask.sum())
        out["stats"][nm] = class_stats(mask, shift, baseline)
    v = conf[valid]
    out["switch_rate"] = float((np.diff(v) != 0).sum() / max(1, len(v)))
    return out


def print_summary(r: dict, title: str) -> None:
    if "error" in r:
        print(f"\n---- {title} ----\n  {r['error']}")
        return
    d, st, b = r["dist"], r["stats"], r["baseline"]
    tot = max(1, sum(d.values()))
    _none = d.get("none", 0)
    print(f"\n---- {title} | 切换率={r['switch_rate']:.3f} ----")
    print(f"  分布: up={d.get('up', 0) / tot:.1%} down={d.get('down', 0) / tot:.1%} "
          f"none={_none / tot:.1%}" + ("（该口径无 NONE 分支）" if "none" not in d else ""))
    print(f"  基线: 上涨占比={b['up_rate']:.4f}  无条件|位移|={b['abs_mean_atr']:.3f}ATR  n={b['n']}")
    for nm in ("up", "down", "none"):
        x = st.get(nm)
        if not x:
            print(f"  {nm:<5}: 样本不足")
            continue
        print(f"  {nm:<5}: n={x['n']:<6} 均值={x['mean_atr']:+.4f} 中位={x['median_atr']:+.4f} "
              f"上涨占比={x['up_rate']:.4f}({x['up_rate_edge']:+.4f}) |位移|={x['abs_mean_atr']:.3f}")


def run_length(seq: np.ndarray) -> float:
    """平均同值段长度（根 bar）——换手率的可读形式：越小越抖。"""
    if len(seq) == 0:
        return 0.0
    sw = int((np.diff(seq) != 0).sum())
    return float(len(seq)) / max(1, sw + 1)


def vs_form_labels(res: dict, atr: np.ndarray, kl, labels_path: str,
                   sf_min_bars: int, tf: str) -> None:
    """与**形态真值**（build_state_labels 产物）交叉：谁更"贴合实时行情"。

    判据（避免循环论证）：一个"贴合行情"的方向模块应当**知道何时不该有观点** ——
    震荡形态下多说 NONE、趋势形态下才断言 UP/DOWN。裸符号恒 100% 断言 ⟹ 零区分度。
    另加"滞后代理"：趋势形态（trend_mid）**刚进入的前若干根**里断言率越高 = 反应越快。
    """
    FORM = ["oscillation", "trend_init", "trend_mid", "trend_fade"]
    lab = pd.read_csv(labels_path)
    lab_ep = epoch_s(lab["open_time"])
    # label_id 可能含 NaN（未标注行）→ 先填 -1 再 cast，避免 "invalid value in cast"
    lab_id = lab["label_id"].fillna(-1).astype(int).to_numpy()
    m_ep = epoch_s(kl["open_time"])
    pos = np.searchsorted(lab_ep, m_ep)
    ok = (pos < len(lab_ep)) & (lab_ep[np.minimum(pos, len(lab_ep) - 1)] == m_ep)
    form = np.full(len(m_ep), -1, dtype=int)
    form[ok] = lab_id[pos[ok]]

    n = len(m_ep)
    sl = res["slope_atr"]
    raw = np.where(sl > 0.0, TD.DIR_UP, TD.DIR_DOWN)
    ok_raw = np.isfinite(sl) & (atr > 0.0) & (np.arange(n) >= sf_min_bars - 1)
    rule, ok_rule = res["confirmed"], res["valid"]
    both = ok_raw & ok_rule & (form >= 0)

    print(f"\n=========== 与形态真值交叉（标注 bar = {int(both.sum())}）===========")
    print(f"  {'形态':<12}{'n':>7}{'A断言率':>9}{'A_up占比':>10}"
          f"{'B断言率':>9}{'B_none率':>10}{'B_up占其断言':>13}")
    rows = {}
    for f_id, fname in enumerate(FORM):
        m = both & (form == f_id)
        if m.sum() < 50:
            continue
        a = raw[m]
        b = rule[m]
        a_assert = float((a != TD.DIR_NONE).mean())          # 恒 100%（无 NONE 分支）
        b_assert = float((b != TD.DIR_NONE).mean())
        a_up = float((a == TD.DIR_UP).mean())
        b_up_of_assert = (float((b == TD.DIR_UP).sum() / max(1, (b != TD.DIR_NONE).sum())))
        rows[fname] = {"n": int(m.sum()), "a_assert": a_assert, "b_assert": b_assert}
        print(f"  {fname:<12}{int(m.sum()):>7}{a_assert:>9.1%}{a_up:>10.1%}"
              f"{b_assert:>9.1%}{1 - b_assert:>10.1%}{b_up_of_assert:>13.1%}")

    # ── 滞后代理：进入 trend_mid 后的前几根，断言率是否跟上 ──
    tm = (form == 2)
    entry = tm & ~np.concatenate(([False], tm[:-1]))
    idx = np.where(entry)[0]
    idx = idx[(idx + 30 < n)]
    if len(idx) > 5:
        def _rate(off_lo, off_hi):
            mm = np.zeros(n, dtype=bool)
            for i in idx:
                mm[i + off_lo: i + off_hi] = True
            mm &= ok_rule
            return float((rule[mm] != TD.DIR_NONE).mean()) if mm.sum() > 0 else float("nan")
        print("\n  滞后代理（trend_mid 刚进入后，B 的断言率；A 恒 100%）：")
        print(f"    前 1–3 根  = {_rate(0, 3):.1%}")
        print(f"    第 4–9 根  = {_rate(3, 9):.1%}")
        print(f"    第 10–30 根= {_rate(9, 30):.1%}")
        print(f"    （进入事件 {len(idx)} 次）")


def compare_raw_sign(res: dict, shift: np.ndarray, sf_min_bars: int,
                     atr: np.ndarray) -> None:
    """对照：**现状 state_strategy 的斜率裸符号** vs **trend_direction 规则模块**。

    两者共用同一个斜率（scheduler.py:1989 传 `feats['slope_linreg']`，
    即 state_features 的 `slope_raw × slope_window / ATR`）→ 差别**不在归一化尺度**，
    只在：是否设阈值 / 是否要 ±DI 确认 / 是否 K 线防抖 / 是否允许输出 NONE。
    """
    sl = res["slope_atr"]
    n = len(sl)
    # A) 现状口径：sign(slope)，**永远断言方向**（无 NONE 分支）
    raw = np.where(sl > 0.0, TD.DIR_UP, TD.DIR_DOWN)
    ok_raw = np.isfinite(sl) & (atr > 0.0) & (np.arange(n) >= sf_min_bars - 1)
    # B) 规则模块口径
    ok_rule = res["valid"]
    rule = res["confirmed"]

    print("\n=========== A) 现状 state_strategy：斜率裸符号 ===========")
    ra = summarize(raw, ok_raw, shift, dir_codes=(TD.DIR_UP, TD.DIR_DOWN))
    print_summary(ra, "裸符号（无阈值 / 无 DI / 无防抖 / 无 NONE）")
    print(f"  平均同向持续 = {run_length(raw[ok_raw]):.2f} 根 bar"
          f"（规则模块 = {run_length(rule[ok_rule]):.2f}）")

    print("\n=========== B) trend_direction 规则模块 ===========")
    rb = summarize(rule, ok_rule, shift)
    print_summary(rb, "阈值+±DI+K线防抖+允许 NONE")

    # ── 一致性：规则说 NONE 时裸符号在干什么；两者断言时是否互相矛盾 ──
    both = ok_raw & ok_rule
    a, b = raw[both], rule[both]
    n_both = max(1, len(a))
    rule_none = b == TD.DIR_NONE
    print("\n=========== 一致性 ===========")
    print(f"  可比 bar = {len(a)}")
    print(f"  规则判 NONE 的占比 = {rule_none.mean():.1%}"
          f"  ← 这些 bar 裸符号**仍然在断言 up/down**（它没有 NONE 可用）")
    opposite = (a != TD.DIR_NONE) & (b != TD.DIR_NONE) & (a != b)
    assert_both = (b != TD.DIR_NONE)
    print(f"  规则断言方向的 bar = {assert_both.mean():.1%}；"
          f"其中与裸符号**方向相反** = {opposite.sum() / max(1, assert_both.sum()):.1%}")
    print(f"  裸符号方向分布: up={float((a == TD.DIR_UP).mean()):.1%} "
          f"down={float((a == TD.DIR_DOWN).mean()):.1%}（恒 100% 断言）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5", choices=["M5", "M15", "H1"], help="评估周期")
    ap.add_argument("--dir-tf", default=None, choices=["M5", "M15", "H1"],
                    help="方向来源周期（缺省=与 --tf 相同）")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--horizon", type=int, default=12, help="前视窗口 N（与形态主窗口对齐）")
    ap.add_argument("--sweep", action="store_true", help="阈值/防抖网格标定")
    ap.add_argument("--compare", action="store_true",
                    help="对照：现状 state_strategy 斜率裸符号 vs trend_direction 规则模块")
    ap.add_argument("--vs-form", action="store_true",
                    help="与形态真值交叉（需 --labels），判断谁更贴合行情")
    ap.add_argument("--labels", default="_scratch/state_M5_v2.csv",
                    help="build_state_labels 产物（--vs-form 用）")
    ap.add_argument("--thr", type=float, default=1.0)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--no-selfcheck", action="store_true", help="跳过 slope_atr 同源自检")
    args = ap.parse_args()

    # Windows 控制台默认 GBK：中文输出会 UnicodeEncodeError 中断脚本（实测踩到）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    dir_tf = args.dir_tf or args.tf
    conn = psycopg2.connect(args.db_url)
    try:
        kl = BSL.load_klines(conn, args.symbol, args.tf)
        kl_dir = kl if dir_tf == args.tf else BSL.load_klines(conn, args.symbol, dir_tf)
    finally:
        conn.close()

    if kl.empty or len(kl) < 300:
        raise SystemExit(f"[fatal] 评估周期 K 线不足：{args.symbol} {args.tf} rows={len(kl)}")
    if kl_dir.empty or len(kl_dir) < 200:
        raise SystemExit(
            f"[fatal] 方向周期 K 线不足：{args.symbol} {dir_tf} rows={len(kl_dir)}"
            f"（该周期可能尚未采集/回填）")

    high = kl["high"].to_numpy(dtype=float)
    low = kl["low"].to_numpy(dtype=float)
    close = kl["close"].to_numpy(dtype=float)
    params = dict(SF.DEFAULT_PARAMS)
    ind = SF.compute_indicators(high, low, close, params)
    atr = np.asarray(ind["atr"], dtype=float)
    shift = forward_shift(close, atr, args.horizon)

    print(f"[data] 评估 {args.symbol} {args.tf} rows={len(close)} "
          f"{kl['open_time'].iloc[0]} .. {kl['open_time'].iloc[-1]}")
    if dir_tf != args.tf:
        print(f"[data] 方向 {args.symbol} {dir_tf} rows={len(kl_dir)}")

    # ── 自检：方向模块的 slope_atr 必须等于模型特征 slope_linreg（同源同值）──
    if not args.no_selfcheck:
        s_chk = TD.compute_direction_series(high, low, close, ind=ind, params=params)
        need = SF.min_bars(params)
        worst, checked = 0.0, 0
        for i in range(need - 1, len(close), max(1, (len(close) - need) // 50)):
            feat = SF.compute_features_at(i, high, low, close, ind, None, params)
            a, b_ = float(s_chk["slope_atr"][i]), float(feat["slope_linreg"])
            if np.isfinite(a) and np.isfinite(b_):
                worst = max(worst, abs(a - b_))
                checked += 1
        if checked == 0:
            raise SystemExit("[fatal] 自检未取到样本")
        if worst > 1e-9:
            raise SystemExit(
                f"[fatal] slope_atr 与契约 slope_linreg 不一致：最大偏差 {worst:.3e}")
        print(f"[selfcheck] slope_atr == 契约 slope_linreg（{checked} 抽样点，"
              f"最大偏差 {worst:.2e}）OK")

    # ── 自检：跨周期**前视闭合**对齐 ──
    # 该函数是线上（scheduler）/ 离线回放（replay_state_chain）/ 本标定的**唯一实现**，
    # 一旦口径写成 `≤ base_close` 就是前视泄露（会多看到一根大周期 bar），
    # 而三处结论会互相印证"没问题" —— 故必须有一个**构造性**断言守住边界。
    if not args.no_selfcheck:
        _h1_open = np.array([0, 3600, 7200, 10800], dtype=np.int64)      # H1 开盘时刻
        _base = np.array([0, 3599, 3600, 7199, 7200], dtype=np.int64)    # 基准 bar 开盘时刻
        _pos = TD.align_last_closed(_h1_open, "H1", _base)
        # 期望：base=0/3599 → 无（-1）；base=3600（恰为第 0 根收盘时刻）→ 第 0 根；
        #       base=7199 → 仍第 0 根（第 1 根未收盘）；base=7200 → 第 1 根
        _want = np.array([-1, -1, 0, 0, 1], dtype=np.int64)
        if not np.array_equal(_pos, _want):
            raise SystemExit(
                f"[fatal] align_last_closed 前视闭合口径错误：got={_pos.tolist()} "
                f"want={_want.tolist()}（边界处多看到一根 = 前视泄露）")
        print(f"[selfcheck] align_last_closed 前视闭合 OK（{_pos.tolist()}）："
              f"边界 bar 只在其**收盘时刻**起可见")

    # ── 与形态真值交叉：谁更"贴合实时行情" ──
    if args.vs_form:
        if dir_tf != args.tf:
            raise SystemExit("[fatal] --vs-form 要求 --dir-tf 与 --tf 相同")
        _res = TD.compute_direction_series(
            high, low, close, ind=ind, params=params,
            cfg={"state.dir.slope_thr_atr": args.thr,
                 "state.dir.debounce_bars": args.k})
        vs_form_labels(_res, atr, kl, args.labels, SF.min_bars(params), args.tf)
        return

    # ── 对照模式：现状（斜率裸符号）vs 规则模块（同一条斜率，两种口径）──
    if args.compare:
        if dir_tf != args.tf:
            raise SystemExit("[fatal] --compare 要求 --dir-tf 与 --tf 相同")
        _res = TD.compute_direction_series(
            high, low, close, ind=ind, params=params,
            cfg={"state.dir.slope_thr_atr": args.thr,
                 "state.dir.debounce_bars": args.k})
        compare_raw_sign(_res, shift, SF.min_bars(params), atr)
        return

    # ── 方向来源：同周期 或 跨周期对齐（严格用已收盘的 Hn bar）──
    if dir_tf == args.tf:
        res = TD.compute_direction_series(high, low, close, ind=ind, params=params,
                                          cfg={"state.dir.slope_thr_atr": args.thr,
                                               "state.dir.debounce_bars": args.k})
        conf, valid = res["confirmed"], res["valid"]

        def run_one(thr: float, k: int):
            r = TD.compute_direction_series(
                high, low, close, ind=ind, params=params,
                cfg={"state.dir.slope_thr_atr": thr, "state.dir.debounce_bars": k})
            return summarize(r["confirmed"], r["valid"], shift)
    else:
        dh = kl_dir["high"].to_numpy(dtype=float)
        dl = kl_dir["low"].to_numpy(dtype=float)
        dc = kl_dir["close"].to_numpy(dtype=float)
        d_epoch = epoch_s(kl_dir["open_time"])
        m_epoch = epoch_s(kl["open_time"])

        def run_one(thr: float, k: int):
            r = TD.compute_direction_series(
                dh, dl, dc, params=params,
                cfg={"state.dir.slope_thr_atr": thr, "state.dir.debounce_bars": k})
            # 对每根评估 bar：取"已收盘且早于其开盘"的最后一个方向 bar（**前视闭合**）。
            # 该规则**只此一份实现**，线上（scheduler）与离线回放共用同一函数；
            # 此前是本脚本内的 `searchsorted` 内联版 —— 一旦某处口径写成 `≤ base_close`
            # 就会多看到一根大周期 bar（前视泄露），故收敛为单一真值。
            pos = TD.align_last_closed(d_epoch, dir_tf, m_epoch)
            ok = pos >= 0
            conf = np.zeros(len(close), dtype=int)
            valid = np.zeros(len(close), dtype=bool)
            conf[ok] = r["confirmed"][pos[ok]]
            valid[ok] = r["valid"][pos[ok]]
            return summarize(conf, valid, shift)

    if args.sweep:
        grid = [(t, k) for t in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0) for k in (1, 3, 5)]
        print(f"\n[sweep] 评估={args.tf} 方向来源={dir_tf} 前视 N={args.horizon}"
              f"（位移单位 = {args.tf} 的 ATR）")
        rows = []
        for t, k in grid:
            r = run_one(t, k)
            print_summary(r, f"thr={t} k={k}")
            if "error" in r:
                continue
            up, dn, no = r["stats"].get("up"), r["stats"].get("down"), r["stats"].get("none")
            rows.append({
                "thr": t, "k": k, "switch": r["switch_rate"],
                "up_n": r["dist"]["up"], "dn_n": r["dist"]["down"],
                "up_edge": up["up_rate_edge"] if up else np.nan,
                "dn_edge": dn["up_rate_edge"] if dn else np.nan,
                "up_mean": up["mean_atr"] if up else np.nan,
                "dn_mean": dn["mean_atr"] if dn else np.nan,
                # 双侧合计边际：up 的正边际 + down 的负边际（越大越好）
                "both_edge": ((up["up_rate_edge"] if up else 0.0)
                              - (dn["up_rate_edge"] if dn else 0.0)),
                "none_abs": no["abs_mean_atr"] if no else np.nan,
            })
        df = pd.DataFrame(rows)
        print("\n================ 汇总（按双侧合计边际排序）================")
        print(df.sort_values("both_edge", ascending=False)
              .to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    else:
        r = run_one(args.thr, args.k)
        print_summary(r, f"评估={args.tf} 方向来源={dir_tf} thr={args.thr} k={args.k} N={args.horizon}")


if __name__ == "__main__":
    main()
