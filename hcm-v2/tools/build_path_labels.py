#!/usr/bin/env python3
"""build_path_labels.py — Step-2.2「前瞻价值」标签构造器（只读 PG，产物本地 CSV）。

背景（2026-09-05 复盘转向）：现引擎/方向头在"信号点"上做确认式分类，
实证（path-replay）其入场多在行情末端 E[R]<0；而"贴低位/回踩企稳"点 E[R]≈+0.5~0.7。
本脚本在**全 M5 网格**（不再只在引擎信号点）采样，产出每个候选点的
「顺向做多期望 R」连续标签 + 与推理同源特征 → 供价值头回归训练。

方法：
  1) H1 趋势世界 direction（close vs EMA60 与 DI 同向 + ADX 分段，同 quality_features）。
  2) 空头世界段镜像（-price）拼成"顺向伪序列"，全序列统一按顺向做多研究。
  3) 每根已收盘 M5 bar（世界≠0）为候选：
       risk = clamp(entry − 前20根low, 0.3×ATR, 3×ATR)；SL=entry−risk；TP=entry+1.6×risk；
       向前 90 根 M5：先触 TP → R=+1.6；先触 SL → R=−1；双触同根 → 剔除；
       到期未触 → R=(close_last−entry)/risk。label = 实现 R（连续，扣成本见 train 参数）。
  4) 特征：伪序列上 quality_features 全指标/结构因子 + rise_atr + donchian_q +
     H1-EMA60 乖离 + world + 时段。纪律：一切特征只用 ≤ 当前收盘 bar 的历史值。

用法:
  DB_URL=postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2 \
    python build_path_labels.py --out labels_path.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

try:
    import quality_features as QF  # noqa: E402  同目录复用指标管线
except Exception as _e:  # pragma: no cover
    print(f"[fatal] quality_features import failed: {_e}", file=sys.stderr)
    sys.exit(1)

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"

HORIZON = 90       # 持仓展望（根 M5 ≈ 7.5h）
TP_MULT = 1.6      # 目标倍数
SWING = 20         # 顺向低位窗口
ENTER_TS = 60.0    # 与 quality_features MTF_ENTER_TS 对齐


def load_kl(tf: str, conn) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT open_time, open, high, low, close, spread FROM hcm_market.klines "
        "WHERE symbol='XAUUSD' AND time_frame=%s ORDER BY open_time", conn, params=(tf,))
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    return df


def h1_world_dir(h1: pd.DataFrame) -> pd.Series:
    """每根 H1 收盘后的世界方向 +1/-1/0（index = close_time）。"""
    cl = h1["close"].astype(float)
    hi, lo = h1["high"].astype(float), h1["low"].astype(float)
    ema60 = cl.ewm(span=60, adjust=False).mean()
    atr = QF.atr if hasattr(QF, "atr") else None  # 使用本地重算
    if atr is None:
        pc = cl.shift(1)
        tr = pd.concat([hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    up, dn = hi.diff(), -lo.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pdi = 100 * pd.Series(plus_dm, index=h1.index).ewm(alpha=1 / 14, adjust=False).mean() / atr.replace(0, np.nan)
    mdi = 100 * pd.Series(minus_dm, index=h1.index).ewm(alpha=1 / 14, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / 14, adjust=False).mean()
    dirs = np.where(pdi >= mdi, 1, -1)
    ma_dir = np.where(cl > ema60, 1, np.where(cl < ema60, -1, 0))
    pdir = np.where((ma_dir == 0) | (ma_dir == dirs), dirs, 0)
    ts = np.where(adx >= 50, 85.0, np.where(adx >= 25, 60.0, np.where(adx >= 15, 45.0, 20.0)))
    w = np.where((pdir != 0) & (ts >= ENTER_TS), pdir, 0)
    return pd.Series(w, index=h1["open_time"] + pd.Timedelta(hours=1)).sort_index()


def mirror_where(df: pd.DataFrame, world: pd.Series) -> pd.DataFrame:
    """world==-1 的 bar 做价格镜像（空头世界 → 顺向多头视角）。返回副本。"""
    out = df.copy()
    d = world.reindex(df.index).fillna(0)
    for col in ("open", "high", "low", "close"):
        out[col] = np.where(d == -1, -out[col], out[col])
    return out


def simulate_forward(close, high, low, risk, horizon=90, tp_mult=1.6):
    """每候选点的实现 R（顺向做多）。返回 R array。"""
    n = len(close)
    R = np.full(n, np.nan)
    for i in range(n - 1):
        if not np.isfinite(risk[i]) or risk[i] <= 0:
            continue
        entry, sl, tp = close[i], close[i] - risk[i], close[i] + tp_mult * risk[i]
        end = min(n, i + 1 + horizon)
        hit = None
        j = i + 1
        while j < end:
            if low[j] <= sl and high[j] >= tp:
                hit = "both"
                break
            if low[j] <= sl:
                hit = "sl"
                break
            if high[j] >= tp:
                hit = "tp"
                break
            j += 1
        if hit == "both":
            continue
        R[i] = tp_mult if hit == "tp" else (-1.0 if hit == "sl" else (close[end - 1] - entry) / risk[i])
    return R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="labels_path.csv")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--tp-mult", type=float, default=TP_MULT)
    args = ap.parse_args()

    conn = psycopg2.connect(args.db_url)
    try:
        m5 = load_kl("M5", conn)
        h1 = load_kl("H1", conn)
    finally:
        conn.close()
    print(f"[data] M5={len(m5)} H1={len(h1)}")

    w = h1_world_dir(h1).rename("world")
    m5 = m5.merge(w, how="left", left_on="open_time", right_index=True)
    m5["world"] = m5["world"].ffill().fillna(0).astype(int)

    # 顺向伪序列（含特征镜像段）：指标在伪序列上统一按"顺向"语义
    m5m = mirror_where(m5, m5["world"])
    try:
        kl = QF.enrich_klines(m5m.reset_index(drop=True))
        kl = kl.assign(open_time=m5m["open_time"].reset_index(drop=True),
                       world=m5m["world"].reset_index(drop=True))
    except Exception as e:
        print(f"[fatal] enrich_klines failed: {e}", file=sys.stderr)
        raise

    # 位置/风险（伪序列，只用历史）
    atr = kl["atr"]
    lo20 = kl["low"].rolling(SWING).min()
    kl["risk"] = (kl["close"] - lo20.shift(1)).clip(lower=0.3 * atr, upper=3.0 * atr)
    kl["rise_atr"] = (kl["close"] - lo20) / atr.replace(0, np.nan)
    # H1 EMA60 乖离（顺向上方偏离，×M5 ATR）
    h1m = mirror_where(h1, w.reindex(h1.index).fillna(0))
    h1e60 = h1m["close"].astype(float).ewm(span=60, adjust=False).mean()
    h1e60 = pd.Series(h1e60.to_numpy(),
                      index=h1["open_time"] + pd.Timedelta(hours=1)).sort_index()
    kl = kl.merge(h1e60.rename("h1e60"), how="left", left_on="open_time", right_index=True)
    kl["h1e60"] = kl["h1e60"].ffill()
    kl["dist_h1e_atr"] = (kl["close"] - kl["h1e60"]) / atr.replace(0, np.nan)

    # 时段 one-hot
    kl = kl.join(kl["open_time"].apply(lambda t: pd.Series(QF.session_onehot(t))))

    # 标签
    close = kl["close"].to_numpy()
    high = kl["high"].to_numpy()
    low = kl["low"].to_numpy()
    risk = kl["risk"].to_numpy()
    R = simulate_forward(close, high, low, risk, horizon=args.horizon, tp_mult=args.tp_mult)
    kl["label"] = R

    out = kl[kl["world"] != 0].copy()
    out = out[out["label"].notna()].reset_index(drop=True)
    out["symbol"] = "XAUUSD"
    print(f"[labels] 有效顺向候选={len(out)}  E[label]={out['label'].mean():+.4f}  "
          f"胜率(>0)={np.mean(out['label'] > 0):.3f}")
    cols = ["symbol", "open_time", "world", "label"] + \
        [c for c in out.columns if c not in ("symbol", "open_time", "world", "label",
                                             "open", "high", "low", "spread", "h1e60")]
    out[cols].to_csv(args.out, index=False)
    print(f"[out] {args.out}  rows={len(out)} cols={len(cols)}")


if __name__ == "__main__":
    main()
