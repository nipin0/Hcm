#!/usr/bin/env python3
"""eval_causal_state_edge.py — 【方案A 判据实验】「当下定义形态」是否有前视边际？

背景（本仓库已定案的三条诊断，见 docs/方案_状态机判别力改进_20260919.md §8）：
  · D1：现有 4 类标签是**未来窗口的确定性函数**（oracle AUC 1.0000），故 base 特征不可分
    （关键对 0.52~0.56）⇒ 那是**预测**任务，不是"状态"任务。
  · D2/§7.6/§7.7/§7.11：四条"补特征 / 改阈值 / 改 horizon / 补记忆"路线**全部无提升**。
  · 唯一可学的两个量（`trend_fade` recall 0.79、波动幅度 AUC 0.6465）有一个共同特征：
    **它们的未来判据与"当下可观测量"高度相关**（ADX 是慢变量、波动有聚集性）。

**本实验检验的核心假设**：把 `trend_fade` 的判据**从未来窗口翻转到过去窗口**
（其余阈值逐字不动 ⇒ 口径同源、非重新发明），得到"当下可识别的衰竭"，它是否仍有前视价值？

判据（**决定是否立项，不预设结论**）：
  ① `EXHAUSTED` 的"顺过去方向的前视位移"(`cont_bp`) **显著低于**全样本基线（t ≤ −2）
     —— 即"当下已衰竭 ⇒ 未来延续性差"（这正是可交易的语义）；
  ② `COMPRESSED`（当下压缩）的未来振幅 / 未来效率比 **显著低于**基线（t ≤ −2）
     —— 即"当下压缩 ⇒ 未来仍无向"（箱体单友好）；
  ③ 两者分布不塌缩（占比 ∈ [5%, 40%]，否则无统计意义/无可用性）。

不通过 ⇒ **定案走方案 C**：模型不提供"状态 alpha"，只保留弃权（阶段1 机制已就位）。
通过 ⇒ 立项：状态机改为"当下可识别形态"判官（标签由过去定义 ⇒ 高置信度自然可达），
   并用 `tools/_scratch/confidence_econ.sql` 的同类方法做前向验收。

纪律：只读 PG、**不改任何生产配置/模型/契约**；`--with-model` 关闭时连模型都不训。
用法：
  python tools/eval_causal_state_edge.py                       # 只跑判据（秒级）
  python tools/eval_causal_state_edge.py --with-model          # 追加"能否高置信识别"
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


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


SF = _load("state_features", os.path.join(_SIG, "state_features.py"))
BSL = _load("build_state_labels", os.path.join(_TOOLS, "build_state_labels.py"))
MEM = _load("build_state_memory_features",
            os.path.join(_TOOLS, "build_state_memory_features.py"))

# 类别编码（**只在本实验内使用**，刻意不动 `STATE_NAMES` 契约）
CAUSAL_NAMES = ["neutral", "exhausted", "compressed"]


def _tstat(sample: np.ndarray, rest: np.ndarray) -> float:
    """两组均值差的 t 统计量（Welch，不假设等方差）。样本不足返回 nan。"""
    if len(sample) < 30 or len(rest) < 30:
        return float("nan")
    v1, v2 = sample.var(ddof=1), rest.var(ddof=1)
    se = np.sqrt(v1 / len(sample) + v2 / len(rest))
    if not np.isfinite(se) or se <= 0:
        return float("nan")
    return float((sample.mean() - rest.mean()) / se)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="tools/state_M5.csv",
                    help="提供 27 维特征 + bar_index + 现有 label_name（只用其**过去**列）")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5", choices=["M5", "M15", "H1"])
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", BSL.DB_URL_DEFAULT))
    ap.add_argument("--n", type=int, default=12, help="窗口长度（对齐 state.horizon_bars）")
    ap.add_argument("--lookback", type=int, default=50, help="极值回看（对齐 win_window）")
    # 阈值**逐字沿用现有标签口径**（state.label.*），保证是"翻转窗口"而非重新发明
    ap.add_argument("--adx-trend", type=float, default=22.0, dest="adx_trend")
    ap.add_argument("--adx-slope-min", type=float, default=3.0, dest="adx_slope_min")
    ap.add_argument("--er-fade", type=float, default=0.35, dest="er_fade")
    ap.add_argument("--mae-fade", type=float, default=0.6, dest="mae_fade")
    ap.add_argument("--atr-pct-max", type=float, default=25.0, dest="atr_pct_max",
                    help="COMPRESSED 的波动分位上限（atr_pct ∈[0,100]，沿用 state_features）")
    ap.add_argument("--disp-track-max", type=float, default=0.5, dest="disp_track_max",
                    help="COMPRESSED 的过去净位移/ATR 上限（镜像 disp_osc=0.25/disp_min=0.30 量纲）")
    ap.add_argument("--with-model", action="store_true",
                    help="追加第二步：用 27 维特征做时序 OOF，看能否**高置信识别**（较慢）")
    ap.add_argument("--out", default=None, help="可选：落盘 label_id/label_name 供复用")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    df = pd.read_csv(args.labels)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df[df["bar_index"].notna()].copy()
    df["bar_index"] = df["bar_index"].astype(int)
    print(f"[labels] {args.labels} rows={len(df)}", file=sys.stderr)

    conn = psycopg2.connect(args.db_url)
    try:
        kl = BSL.load_klines(conn, args.symbol, args.tf)
    finally:
        conn.close()
    high = kl["high"].to_numpy(dtype=float)
    low = kl["low"].to_numpy(dtype=float)
    close = kl["close"].to_numpy(dtype=float)
    ind = SF.compute_indicators(high, low, close, dict(SF.DEFAULT_PARAMS))
    atr, adx = ind["atr"], ind["adx"]
    print(f"[klines] {args.symbol} {args.tf} bars={len(close)}", file=sys.stderr)

    n, lb = args.n, args.lookback
    # ── 未来度量（**只用于评估**，绝不入特征）────────────────────────────
    cl, hi, lo = pd.Series(close), pd.Series(high), pd.Series(low)
    c_fwd = cl.shift(-n)
    fwd_net = c_fwd - cl                                   # 带符号未来净位移
    path_fwd = cl.diff().abs().rolling(n).sum().shift(-n)   # 未来路径长度
    fwd_er = (fwd_net.abs() / path_fwd).where(path_fwd > 1e-12, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        fwd_abs_bp = (fwd_net.abs() / cl * 1e4)
    fwd_range_atr = (hi.rolling(n).max().shift(-n) - lo.rolling(n).min().shift(-n)) \
        / pd.Series(atr, index=cl.index)
    past_net = cl - cl.shift(n)
    sign_past = np.sign(past_net.fillna(0.0))
    with np.errstate(invalid="ignore", divide="ignore"):
        cont_bp = (fwd_net * sign_past / cl * 1e4)          # 顺过去方向的前视位移

    # ── 过去（因果）判据量 ────────────────────────────────────────────────
    adx_s = pd.Series(adx, index=cl.index)
    adx_slope_past = adx_s - adx_s.shift(n)                 # 镜像 adx_slope_f（未来→过去）
    prior_hi = hi.shift(n).rolling(lb).max()
    prior_lo = lo.shift(n).rolling(lb).min()
    win_hi, win_lo = hi.rolling(n).max(), lo.rolling(n).min()
    new_ext_past = np.where(past_net > 0, win_hi > prior_hi,
                            np.where(past_net < 0, win_lo < prior_lo, False))

    idx = df["bar_index"].to_numpy()
    mem = [MEM.compute_memory(int(i), high, low, close, atr, adx, n=n, lookback=lb)
           for i in idx]
    er_t = np.array([float(m["er_trail12"]) if m else np.nan for m in mem])
    mae_t = np.array([float(m["mae_trail12"]) if m else np.nan for m in mem])
    disp_t = np.array([float(m["disp_trail12"]) if m else np.nan for m in mem])
    atr_pct = pd.to_numeric(df["atr_pct"], errors="coerce").to_numpy(dtype=float)

    adx_now = np.asarray(adx_s.to_numpy()[idx], dtype=float)
    slope_p = np.asarray(adx_slope_past.to_numpy()[idx], dtype=float)
    ext_p = np.asarray(new_ext_past, dtype=bool)[idx]

    # ── 标签：**纯过去定义**（EXHAUSTED 优先）───────────────────────────
    is_exh = ((adx_now >= args.adx_trend)
              & (slope_p <= -args.adx_slope_min)
              & ((er_t <= args.er_fade) | (mae_t >= args.mae_fade))
              & (~ext_p))
    is_cmp = (~is_exh) & (atr_pct <= args.atr_pct_max) & (disp_t <= args.disp_track_max)
    label_id = np.where(is_exh, 1, np.where(is_cmp, 2, 0))
    print(f"[overlap] 同时命中 EXHAUSTED 与 COMPRESSED（按优先级归 EXHAUSTED）="
          f"{int(np.sum((adx_now >= args.adx_trend) & (atr_pct <= args.atr_pct_max)))} 根",
          file=sys.stderr)

    out = pd.DataFrame({
        "label_id": label_id,
        "label_name": [CAUSAL_NAMES[i] for i in label_id],
        "cur_label_name": df["label_name"].to_numpy(),
        "atr_pct": atr_pct,
        "fwd_abs_bp": np.asarray(fwd_abs_bp.to_numpy()[idx], dtype=float),
        "fwd_er": np.asarray(fwd_er.to_numpy()[idx], dtype=float),
        "cont_bp": np.asarray(cont_bp.to_numpy()[idx], dtype=float),
        "fwd_range_atr": np.asarray(fwd_range_atr.to_numpy()[idx], dtype=float),
        # ATR 归一的版本（排除"当期 ATR 大小"的机械效应，见下方分层对照）
        "fwd_abs_atr": np.asarray(
            (fwd_net.abs() / pd.Series(atr, index=cl.index)).to_numpy()[idx], dtype=float),
        "cont_atr": np.asarray(
            (fwd_net * sign_past / pd.Series(atr, index=cl.index)).to_numpy()[idx],
            dtype=float),
    }).dropna(subset=["fwd_abs_bp", "cont_bp", "fwd_range_atr", "fwd_abs_atr"])
    print(f"[数据] 有效样本 {len(out)}（丢弃尾部 {n} 根与镜像窗口不足者）")

    print("\n=========== 判据① ②：各类的前视边际 vs 全样本基线 ===========")
    print(f"  {'类别':<12}{'n':>7}{'占比':>7}{'fwd_abs_bp':>12}{'fwd_er':>9}"
          f"{'cont_bp':>10}{'fwd_range_atr':>15}")
    base = out
    print(f"  {'全样本':<12}{len(base):>7}{100.0:>6.1f}%{base['fwd_abs_bp'].mean():>12.2f}"
          f"{base['fwd_er'].mean():>9.3f}{base['cont_bp'].mean():>10.2f}"
          f"{base['fwd_range_atr'].mean():>15.3f}")
    verdict = {}
    for cid, nm in [(1, "EXHAUSTED"), (2, "COMPRESSED")]:
        s = out[out["label_id"] == cid]
        r = out[out["label_id"] != cid]
        if len(s) == 0:
            print(f"  {nm:<12}{0:>7}  ← 无样本")
            verdict[nm] = None
            continue
        share = len(s) / len(out)
        print(f"  {nm:<12}{len(s):>7}{share * 100:>6.1f}%{s['fwd_abs_bp'].mean():>12.2f}"
              f"{s['fwd_er'].mean():>9.3f}{s['cont_bp'].mean():>10.2f}"
              f"{s['fwd_range_atr'].mean():>15.3f}")
        verdict[nm] = {
            "n": len(s), "share": share,
            "t_abs": _tstat(s["fwd_abs_bp"].to_numpy(), r["fwd_abs_bp"].to_numpy()),
            "t_er": _tstat(s["fwd_er"].to_numpy(), r["fwd_er"].to_numpy()),
            "t_cont": _tstat(s["cont_bp"].to_numpy(), r["cont_bp"].to_numpy()),
            "t_range": _tstat(s["fwd_range_atr"].to_numpy(), r["fwd_range_atr"].to_numpy()),
        }

    print("\n=========== 显著性（Welch t 统计量；|t| ≥ 2 视为显著）===========")
    for nm, v in verdict.items():
        if not v:
            continue
        print(f"  {nm:<12} t(振幅)={v['t_abs']:+.2f}  t(fwd_er)={v['t_er']:+.2f}  "
              f"t(延续)={v['t_cont']:+.2f}  t(ATR振幅)={v['t_range']:+.2f}")

    # ── 对照①：控制"当期波动水平"后的分化（排除机械效应）──────────────────
    # 为什么必须做：`fwd_abs_bp` 是**绝对 bp**，而 `COMPRESSED` 的定义本身含
    #   `atr_pct ≤ 25` ⇒ "未来绝对振幅小"很可能只是"当期 ATR 小"的**同义反复**，
    #   不构成独立前视信息。故两层控制：① 用 **ATR 归一**幅度；② 在 `atr_pct`
    #   五分位**桶内**比较（同波动水平的 bar 之间比）。
    print("\n=========== 对照①：按当期波动分位分层（桶内比较，控制机械效应）===========")
    try:
        out["atr_bin"] = pd.qcut(out["atr_pct"], 5, labels=False, duplicates="drop")
    except Exception:  # noqa: BLE001
        out["atr_bin"] = 0
    print(f"  {'桶(低→高)':<10}{'n':>7}{'类别':<12}{'n_c':>7}{'fwd_abs_atr':>13}"
          f"{'cont_atr':>10}{'fwd_er':>9}{'t(abs_atr)':>12}{'t(cont)':>9}")
    for b in sorted(out["atr_bin"].dropna().unique()):
        sub = out[out["atr_bin"] == b]
        for cid, nm in [(1, "EXHAUSTED"), (2, "COMPRESSED")]:
            s = sub[sub["label_id"] == cid]
            r = sub[sub["label_id"] != cid]
            if len(s) < 30:
                continue
            print(f"  {int(b):<10}{len(sub):>7}{nm:<12}{len(s):>7}"
                  f"{s['fwd_abs_atr'].mean():>13.3f}{s['cont_atr'].mean():>10.3f}"
                  f"{s['fwd_er'].mean():>9.3f}"
                  f"{_tstat(s['fwd_abs_atr'].to_numpy(), r['fwd_abs_atr'].to_numpy()):>12.2f}"
                  f"{_tstat(s['cont_atr'].to_numpy(), r['cont_atr'].to_numpy()):>9.2f}")

    # ── 与既有"未来定义"标签的交叉对照（口径含义的旁证）──────────────────
    print("\n=========== 交叉对照：本实验（过去定义） vs 现有标签（未来定义）===========")
    ct = pd.crosstab(out["label_name"], out["cur_label_name"])
    print(ct.to_string())
    for nm in ("exhausted", "compressed"):
        _row = ct.loc[nm]
        _tot = _row.sum()
        if _tot:
            _fade = _row.get("trend_fade", 0)
            _tag = "近义" if _fade / _tot > 0.6 else "不等同（当下判据 ≠ 未来判据）"
            print(f"  {nm}: 与「未来定义 trend_fade」的重合率 = {_fade / _tot:.1%}  ⇒ {_tag}")

    # ── 判据结论 ─────────────────────────────────────────────────────────
    print("\n=========== 结论 ===========")
    ok_all = True
    for nm, need_t in (("EXHAUSTED", "t_cont"), ("COMPRESSED", "t_er")):
        v = verdict.get(nm)
        if not v:
            print(f"  {nm}: 无样本 → 不通过（判据③）")
            ok_all = False
            continue
        t = v[need_t]
        sig = np.isfinite(t) and t <= -2.0
        ok_share = 0.05 <= v["share"] <= 0.40
        print(f"  {nm}: n={v['n']} 占比={v['share']:.1%} "
              f"关键 t({need_t})={t:+.2f} → 显著分化={'是' if sig else '否'}；"
              f"占比合规={'是' if ok_share else '否'}")
        ok_all = ok_all and sig and ok_share
    print("\n  " + ("**通过** ⇒ 立项：状态机改判「当下可识别形态」"
                    "（标签由过去定义 ⇒ 高置信度自然可达），并做前向验收。"
                    if ok_all else
                    "**不通过** ⇒ 定案走方案 C：模型不提供「状态 alpha」，"
                    "只保留弃权（阶段1 机制已就位）。"))
    print("  口径说明：`cont_bp` = 顺过去方向的未来位移（t<0 = 延续性差，即可交易语义）；"
          "`fwd_er` = 未来效率比（越低越无方向 ⇒ 箱体友好）。")

    if args.out:
        out.to_csv(args.out, index=False)
        print(f"\n[out] {args.out}")

    if args.with_model:
        print("\n=========== 第二步：能否**高置信识别**（时序 OOF）===========")
        import lightgbm as lgb
        from sklearn.model_selection import TimeSeriesSplit
        cols = list(SF.STATE_FEATURE_COLS)
        X = df[cols].astype(float).iloc[:len(out)].reset_index(drop=True)
        y = out["label_id"].to_numpy()
        tss = TimeSeriesSplit(n_splits=5)
        oof = np.full(len(y), -1)
        for tr, te in tss.split(X):
            m = lgb.LGBMClassifier(objective="multiclass", num_class=3, random_state=42,
                                   n_jobs=-1, verbose=-1)
            m.fit(X.iloc[tr], y[tr])
            oof[te] = m.predict_proba(X.iloc[te]).argmax(axis=1)
        cov = oof >= 0
        acc = float((oof[cov] == y[cov]).mean())
        base_acc = float(np.bincount(y[cov]).max() / cov.sum())
        print(f"  3 类 OOF 准确率={acc:.4f}  多数类基线={base_acc:.4f}  Δ={acc - base_acc:+.4f}")
        for cid, nm in [(1, "EXHAUSTED"), (2, "COMPRESSED")]:
            msk = cov & (y == cid)
            if msk.sum():
                print(f"  {nm:<12} recall={(oof[msk] == cid).mean():.3f}  n={int(msk.sum())}")


if __name__ == "__main__":
    main()
