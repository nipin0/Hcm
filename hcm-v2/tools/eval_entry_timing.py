"""eval_entry_timing.py — **买点规则对比**（触发即入 vs 等回调 vs 追涨过滤）。

背景：策略层现有买点是"自最近 `box_window`（20）根高点回落 ≥ pullback_atr×ATR"。
两个疑点：
  1. 用**箱体窗口**当回调基准：新趋势刚起时，20 根高点可能来自**趋势之前**，
     使回调条件要么恒真、要么恒假 —— 基准与"本轮趋势的极值"无关。
  2. 该规则**从未被实测过**。而触发器只提前约 1 根（§30），若再等回调若干根，
     可能把唯一的提前量优势等没了。

故本脚本不预设"更合理的窗口"，而是**让数据比较几种买点规则**（同一信号集，只换入场时点）：
  A 触发即入        ：信号 bar 收盘价入场
  B 等回调 X ATR    ：X ∈ {0.3, 0.5, 0.8}，最多等 K 根（K ∈ {3,5}），超时则市价入场
  C 追涨过滤        ：信号 bar 实体/振幅过大（> 1.5×ATR）则放弃该信号

结果口径（**与入场时点强相关**，故必须用"入场后"的量）：
  · 顺向位移（H 根后，ATR 归一，按交易方向取符号）
  · 命中率（顺向位移 > 0）
  · 简易 R 代理：入场即挂 1R=ATR 止损 + 2R 止盈，先触者为准 → 平均 R
    （用后续 H 根 bar 的 high/low 逐根判定，近似实盘顺序）

⚠ 口径声明：
  · 信号集来自 `trend_trigger`（rise 用**已保存的起点模型**推全段；donchian 直接算）。
    模型在全段上训练过 → 信号本身**含乐观成分**，但**规则 A/B/C 用的是同一信号集**，
    故规则之间的比较是公平的（本脚本的目的正是"同一信号下选买点"）。
  · 方向来自 `trend_direction`（规则模块），`none` 的信号直接剔除。

用法：
    python eval_entry_timing.py --symbol XAUUSD --tf M5 --models _scratch/models
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import sys

import numpy as np
import pandas as pd
import psycopg2

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
_SIG = os.path.join(_ROOT, "hcm-signal-tower", "signal_tower")
DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


SF = _load("state_features", os.path.join(_SIG, "state_features.py"))
TD = _load("trend_direction", os.path.join(_SIG, "trend_direction.py"))
TT = _load("trend_trigger", os.path.join(_SIG, "trend_trigger.py"))
BSL = _load("build_state_labels", os.path.join(_TOOLS, "build_state_labels.py"))
# 【2026-09-15 L4 触价入场】规则 D/E 必须与**生产同一真值**（阈值取自策略层 DEFAULTS），
# 否则"评估通过、上线不一致"——本项目反复强调"禁止第二份实现"。
SS = _load("state_strategy", os.path.join(_SIG, "state_strategy.py"))
PB_W = int(SS.DEFAULTS["state.box.window"])              # 回踩基准窗口（= 入场箱体窗口）
PB_X = float(SS.DEFAULTS["state.trend.pullback_atr"])    # 回踩深度阈值（ATR 倍数）

_ONSET_RX = re.compile(r"lgbm_onset_([A-Z0-9]+)_v(\d+)_s(\d+)\.txt$")


def onset_proba_series(model_dir: str, tf: str, high, low, close, params) -> np.ndarray | None:
    """用已保存的起点模型推**全段** P(起点)。缺失则返回 None（rise 分支关闭）。"""
    files = [p for p in glob.glob(os.path.join(model_dir, f"lgbm_onset_{tf}_v*_s*.txt"))
             if _ONSET_RX.search(os.path.basename(p))]
    if not files:
        return None
    try:
        import lightgbm as lgb
    except Exception:  # noqa: BLE001
        return None
    ver = max(int(_ONSET_RX.search(os.path.basename(p)).group(2)) for p in files)
    files = [p for p in files if int(_ONSET_RX.search(os.path.basename(p)).group(2)) == ver]
    boosters = [lgb.Booster(model_file=p) for p in sorted(files)]
    n = len(close)
    ind = SF.compute_indicators(high, low, close, params)
    rows, idx = [], []
    for i in range(n):
        f = SF.compute_features_at(i, high, low, close, ind, None, params)
        if f is None:
            continue
        rows.append({k: float(f[k]) for k in SF.STATE_FEATURE_COLS})
        idx.append(i)
    if not rows:
        return None
    x = pd.DataFrame(rows)
    pr = None
    for b in boosters:
        q = b.predict(x)
        pr = q if pr is None else pr + q
    pr = pr / len(boosters)
    # ⚠ 二分类 Booster.predict 返回**一维**（正类概率），不是 (n,2)。
    #   直接取 pr[:, 1] 会 IndexError（实测踩到；state_infer.infer_onset_tail 同坑）。
    vals = pr if pr.ndim == 1 else (pr[:, 1] if pr.shape[1] > 1 else pr[:, 0])
    out = np.full(n, np.nan)
    for k, i in enumerate(idx):
        out[i] = float(vals[k])
    return out


def r_proxy(direction: int, entry: float, atr: float, high, low, close,
            i0: int, horizon: int, stop_r: float = 1.0, tp_r: float = 2.0) -> float:
    """简易 R 代理：入场即挂 stop_r×ATR 止损、tp_r×ATR 止盈，逐根判定先后。

    direction: +1 多 / −1 空。返回该笔的 R（止损 −1R / 止盈 +tp_r R / 超时按浮盈折算）。
    """
    if atr <= 0:
        return 0.0
    sl = entry - direction * stop_r * atr
    tp = entry + direction * tp_r * atr
    n = len(close)
    for j in range(1, horizon + 1):
        i = i0 + j
        if i >= n:
            break
        if direction > 0:
            if low[i] <= sl:
                return -stop_r
            if high[i] >= tp:
                return tp_r
        else:
            if high[i] >= sl:
                return -stop_r
            if low[i] <= tp:
                return tp_r
    i = min(n - 1, i0 + horizon)
    return direction * (close[i] - entry) / atr


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5", choices=["M5", "M15", "H1"])
    ap.add_argument("--models", default="_scratch/models", help="起点模型目录")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--horizon", type=int, default=12, help="持有上限（根）")
    ap.add_argument("--spike-atr", type=float, default=1.5,
                    help="追涨过滤阈值：信号 bar 振幅 > 此 ×ATR 视为追高，放弃")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    conn = psycopg2.connect(args.db_url)
    try:
        kl = BSL.load_klines(conn, args.symbol, args.tf)
    finally:
        conn.close()
    if kl.empty or len(kl) < 500:
        raise SystemExit(f"[fatal] K 线不足：{len(kl)}")

    high = kl["high"].to_numpy(float)
    low = kl["low"].to_numpy(float)
    close = kl["close"].to_numpy(float)
    params = dict(SF.DEFAULT_PARAMS)
    ind = SF.compute_indicators(high, low, close, params)
    atr = np.asarray(ind["atr"], dtype=float)

    # 方向（规则模块，单一真值）
    dres = TD.compute_direction_series(high, low, close, ind=ind, params=params)
    dirs = dres["confirmed"]
    dvalid = dres["valid"]

    # 触发器：rise(ΔP) ∪ donchian
    p = onset_proba_series(args.models, args.tf, high, low, close, params)
    rise = TT.compute_rise(p, int(TT.TRIGGER_CFG_FALLBACK["state.trigger.rise_m"]),
                           float(TT.TRIGGER_CFG_FALLBACK["state.trigger.rise_thr"])) \
        if p is not None else np.zeros(len(close), dtype=bool)
    don = TT.compute_donchian(high, low, close,
                             int(TT.TRIGGER_CFG_FALLBACK["state.trigger.donchian_w"]))
    trig = (rise | don)
    if p is None:
        print("[warn] 未找到起点模型 → 触发器仅剩突破分支（rise 关闭）")

    # 信号集：触发 且 方向明确 且 方向有效
    sig = np.where(trig & dvalid & np.isin(dirs, [TD.DIR_UP, TD.DIR_DOWN]))[0]
    sig = sig[(sig + args.horizon + 5) < len(close)]
    print(f"[data] {args.symbol} {args.tf} bars={len(close)}；信号 {len(sig)} 个"
          f"（触发 {int(trig.sum())} / 方向明确 {int((dvalid & np.isin(dirs,[1,2])).sum())}）")

    rng_ = high - low

    def eval_rule(name: str, entry_idx: np.ndarray, dir_sign: np.ndarray) -> dict:
        n = len(entry_idx)
        if n == 0:
            return {"name": name, "n": 0}
        disp, rr, hit = [], [], 0
        for k in range(n):
            i0 = int(entry_idx[k])
            d = int(dir_sign[k])
            e = float(close[i0])
            a = float(atr[i0])
            if a <= 0:
                continue
            j = min(len(close) - 1, i0 + args.horizon)
            disp.append(d * (close[j] - e) / a)
            rr.append(r_proxy(d, e, a, high, low, close, i0, args.horizon))
            hit += 1 if d * (close[j] - e) > 0 else 0
        return {"name": name, "n": len(disp),
                "disp_mean": float(np.mean(disp)), "disp_median": float(np.median(disp)),
                "hit": hit / max(1, len(disp)), "R_mean": float(np.mean(rr))}

    rows = []
    dsign = np.where(dirs[sig] == TD.DIR_UP, 1, -1)

    # A 触发即入
    rows.append(eval_rule("A 触发即入", sig, dsign))

    # B 等回调（X ATR，最多等 K 根；超时市价入场）
    for X in (0.3, 0.5, 0.8):
        for K in (3, 5):
            e_idx, e_dir = [], []
            for k, i0 in enumerate(sig):
                d = int(dsign[k])
                a = float(atr[i0])
                if a <= 0:
                    continue
                ref = float(high[i0]) if d > 0 else float(low[i0])
                tgt = ref - d * X * a
                chosen = None
                for j in range(1, K + 1):
                    i = i0 + j
                    if i >= len(close):
                        break
                    if (d > 0 and low[i] <= tgt) or (d < 0 and high[i] >= tgt):
                        chosen = i
                        break
                if chosen is None:
                    chosen = min(len(close) - 1, i0 + K)   # 超时市价
                e_idx.append(chosen)
                e_dir.append(d)
            rows.append(eval_rule(f"B 回调{X}ATR/等{K}根", np.array(e_idx), np.array(e_dir)))

    # C 追涨过滤（在 A 的基础上剔除大幅信号 bar）
    keep = rng_[sig] <= args.spike_atr * atr[sig]
    rows.append(eval_rule(f"C 触发即入+滤振幅>{args.spike_atr}ATR", sig[keep], dsign[keep]))

    # ── 【2026-09-15 L4 触价入场】两条规则：E = 现实现（基线）、D = 触价（本次新增）──
    # 入价不再恒为收盘价 → 另用一个显式入价的评估器（其余口径与 eval_rule 完全一致）。
    def eval_rule_px(name: str, entry_idx: np.ndarray, dir_sign: np.ndarray,
                     entry_px: np.ndarray) -> dict:
        n = len(entry_idx)
        if n == 0:
            return {"name": name, "n": 0}
        disp, rr, hit = [], [], 0
        for k in range(n):
            i0 = int(entry_idx[k])
            d = int(dir_sign[k])
            e = float(entry_px[k])          # ← 与 eval_rule 的唯一差异：入价由调用方给定
            a = float(atr[i0])
            if a <= 0:
                continue
            j = min(len(close) - 1, i0 + args.horizon)
            disp.append(d * (close[j] - e) / a)
            rr.append(r_proxy(d, e, a, high, low, close, i0, args.horizon))
            hit += 1 if d * (close[j] - e) > 0 else 0
        return {"name": name, "n": len(disp),
                "disp_mean": float(np.mean(disp)), "disp_median": float(np.median(disp)),
                "hit": hit / max(1, len(disp)), "R_mean": float(np.mean(rr))}

    def _pullback_level(d: int, i0: int):
        """触价目标位 —— 与 `state_strategy._pullback_level` **同一口径**：
        近 `PB_W` 根极值 ∓ `PB_X`×ATR（窗口**含**信号 bar，因为
        `_pullback_entry` 用的是 `high[-w:]`）。"""
        lo_i = max(0, i0 - PB_W + 1)
        a = float(atr[i0])
        if a <= 0:
            return None
        if d > 0:
            return float(np.max(high[lo_i:i0 + 1])) - PB_X * a
        return float(np.min(low[lo_i:i0 + 1])) + PB_X * a

    for K in (1, 2):          # K 根 ≈ entry_wait_sec 300/600（M5 一根 300s）
        # E 回踩到位市价（**现实现**：未到位就放弃，无触价单）
        e_idx, e_dir, e_px = [], [], []
        # D 触价入场（未到位则挂触价位等 K 根，触及按**触价位**成交，等不到就放弃）
        d_idx, d_dir, d_px = [], [], []
        for i0, d in zip(sig, dsign):
            lv = _pullback_level(int(d), int(i0))
            if lv is None:
                continue
            c0 = float(close[i0])
            done = (c0 <= lv) if d > 0 else (c0 >= lv)
            if done:
                # 已回踩到位：两条规则**都**走市价（D 的"已到位"分支与现实现一致）
                e_idx.append(i0); e_dir.append(d); e_px.append(c0)
                d_idx.append(i0); d_dir.append(d); d_px.append(c0)
                continue
            # 未到位：E 放弃；D 挂触价单
            chosen = None
            for j in range(1, K + 1):
                i = i0 + j
                if i >= len(close):
                    break
                if (d > 0 and low[i] <= lv) or (d < 0 and high[i] >= lv):
                    chosen = i
                    break
            if chosen is not None:
                d_idx.append(chosen); d_dir.append(d); d_px.append(lv)
        rows.append(eval_rule_px("E 回踩到位市价(现实现)", np.array(e_idx, dtype=int),
                                 np.array(e_dir, dtype=int), np.array(e_px, dtype=float)))
        rows.append(eval_rule_px(f"D 触价入场/等{K}根", np.array(d_idx, dtype=int),
                                 np.array(d_dir, dtype=int), np.array(d_px, dtype=float)))

    print(f"\n=========== 买点规则对比（同一信号集，H={args.horizon} 根）===========")
    print(f"  {'规则':<26}{'n':>6}{'顺向位移均值':>13}{'中位':>10}{'命中率':>9}{'平均R':>9}")
    for r in rows:
        if not r.get("n"):
            print(f"  {r['name']:<26}{0:>6}   （无样本）")
            continue
        print(f"  {r['name']:<26}{r['n']:>6}{r['disp_mean']:>+13.4f}"
              f"{r['disp_median']:>+10.4f}{r['hit']:>9.1%}{r['R_mean']:>+9.3f}")

    best = max((r for r in rows if r.get("n")), key=lambda r: r["R_mean"], default=None)
    print(f"\n  → 按平均 R 最优：{best['name']}（R={best['R_mean']:+.3f}）" if best else "")
    print("\n  ⚠ 口径：信号集含乐观成分（起点模型在全段训练过），但 A/B/C 用**同一信号集**，"
          "\n     故规则间比较公平；不可用本表绝对值代表线上收益。")


if __name__ == "__main__":
    main()
