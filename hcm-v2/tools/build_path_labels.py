#!/usr/bin/env python3
"""build_path_labels.py — Step-2.2「前瞻价值」标签构造器（只读 PG，产物本地 CSV）。

背景（2026-09-05 复盘转向）：现引擎/方向头在"信号点"上做确认式分类，
实证（path-replay）其入场多在行情末端 E[R]<0；而"贴低位/回踩企稳"点 E[R]≈+0.5~0.7。
本脚本在**全 M5 网格**（不再只在引擎信号点）采样，产出每个候选点的
「顺向做多期望 R」连续标签 + 与推理同源特征 → 供价值头回归训练。

方法：
  1) H1 趋势世界 direction（close vs EMA60 与 DI 同向 + ADX 分段，同 quality_features）。
  2) 空头世界段构造"顺向伪序列"：**差分链式仿射镜像**（段内 ≡ −price+常数，跨段连续），
     全序列统一按顺向做多研究。见 value_pipeline.build_pseudo_ohlc。
  3) 每根已收盘 M5 bar（世界≠0）为候选：
       risk = clamp(entry − 前20根low, 0.3×ATR, 3×ATR)；SL=entry−risk；TP=entry+1.6×risk；
       向前 90 根 M5：先触 TP → R=+1.6；先触 SL → R=−1；双触同根 → 剔除；
       到期未触 → R=(close_last−entry)/risk。label = 实现 R（连续，扣成本见 train 参数）。
  4) 特征：**统一由 value_pipeline.build_features 产出**（训练/推理同一实现）——
     quality_features 全指标/结构因子 + risk/rise_atr + dist_h1e_atr + world + 时段。
     纪律：一切特征只用 ≤ 当前收盘 bar 的历史值。

【2026-09-12 修复】本文件原自带一份"镜像 + enrich + H1 乖离"实现，与推理侧
value_features.py 并不一致，且含两处真 bug（均已迁出并修复，详见 value_pipeline 文档）：
  · Bug-A：`w.reindex(h1.index)` 索引错配（DatetimeIndex vs RangeIndex）→ H1 镜像静默失效
           → world=−1 的 dist_h1e_atr 变成 (−4300−4300)/ATR ≈ −733 的量纲垃圾。
  · Bug-B：逐 bar 取反【价格水平】使 world 切换处 +4400→−4400 跳变 → ATR 被放大 10~100 倍。

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
    import value_pipeline as VP  # noqa: E402  训练/推理【唯一共用】特征管线
except Exception as _e:  # pragma: no cover
    print(f"[fatal] value_pipeline import failed: {_e}", file=sys.stderr)
    sys.exit(1)

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"

HORIZON = 90       # 持仓展望（根 M5 ≈ 7.5h）
POST_MARGIN = 100  # 坏棒后屏蔽余量（根 M5 ≈ 8.3h；ATR-EWM(1/14) 恢复期）
TP_MULT = 1.6      # 目标倍数
SWING = VP.SWING       # 口径常量统一由 value_pipeline 提供（防两侧漂移）
ENTER_TS = VP.ENTER_TS


def load_kl(tf: str, conn) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT open_time, open, high, low, close, spread FROM hcm_market.klines "
        "WHERE symbol='XAUUSD' AND time_frame=%s ORDER BY open_time", conn, params=(tf,))
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    return df


# 2026-09-12 重构：原 h1_world_dir / mirror_where 已删除，统一由 value_pipeline 提供。
#   · h1_world_dir  → VP.h1_world_dir（唯一实现）
#   · mirror_where  → 由 VP.build_pseudo_ohlc 取代（差分链式仿射镜像，修 Bug-B 跳变）
#   · H1 镜像       → 彻底删除（修 Bug-A 索引错配）；H1 乖离改 sign 口径
# 这两处曾是"训练/推理口径漂移"与 dist_h1e_atr 垃圾值的根源。


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

    # 特征：训练/推理【唯一共用】的 value_pipeline.build_features
    #   · Bug-B 修复：build_pseudo_ohlc 用差分链式仿射镜像替代逐 bar 取反价格水平
    #   · Bug-A 修复：H1 不做镜像，dist_h1e_atr 统一 sign 口径（与推理完全一致）
    kl = VP.build_features(m5, h1)
    print(f"[feat] ATR mean={kl['atr'].mean():.2f} max={kl['atr'].max():.2f} | "
          f"risk/ATR mean={(kl['risk'] / kl['atr']).mean():.3f} | "
          f"dist_h1e_atr by world: " +
          " ".join(f"{w}={kl.loc[kl['world'] == w, 'dist_h1e_atr'].mean():+.2f}"
                   for w in (-1, 1) if (kl['world'] == w).any()))

    # 标签
    close = kl["close"].to_numpy()
    high = kl["high"].to_numpy()
    low = kl["low"].to_numpy()
    risk = kl["risk"].to_numpy()
    R = simulate_forward(close, high, low, risk, horizon=args.horizon, tp_mult=args.tp_mult)
    kl["label"] = R

    # ── 坏棒隔离（2026-09-12）────────────────────────────────────────────
    # ① 坏棒自身（open/low 为坏值）② 坏棒后 POST_MARGIN 根（ATR-EWM 恢复期）
    # ③ 候选的向前 horizon 窗口一旦触及 ①/②（否则 SL/TP 三触判定会被坏 low 伪造）
    # ⇒ 一律置 label=NaN。修复只做有界收敛（low=open=close），不反演真值。
    anom = kl["anomaly"].to_numpy()
    blocked = anom.copy()
    for _i in np.where(anom)[0]:
        blocked[_i + 1:_i + 1 + POST_MARGIN] = True
    nxt = np.full(len(blocked), len(blocked), dtype=int)
    _run = len(blocked)
    for _i in range(len(blocked) - 1, -1, -1):
        _run = _i if blocked[_i] else _run
        nxt[_i] = _run
    _t = np.arange(len(blocked))
    _ws = np.minimum(_t + 1, len(blocked) - 1)
    _we = np.minimum(_t + args.horizon, len(blocked) - 1)
    touch = (nxt[_ws] <= _we) | blocked
    kl.loc[touch, "label"] = np.nan
    print(f"[anom] 坏棒={int(anom.sum())} 根；隔离(含 POST_MARGIN={POST_MARGIN} 与前向窗口) "
          f"= {int(touch.sum())} 根 ({touch.mean():.1%} of 全序列)")

    out = kl[kl["world"] != 0].copy()
    out = out[out["label"].notna()].reset_index(drop=True)
    out["symbol"] = "XAUUSD"
    print(f"[labels] 有效顺向候选={len(out)}  E[label]={out['label'].mean():+.4f}  "
          f"胜率(>0)={np.mean(out['label'] > 0):.3f}")
    cols = ["symbol", "open_time", "world", "label"] + \
        [c for c in out.columns
         if c not in ("symbol", "open_time", "world", "label", "anomaly",
                      "open", "high", "low", "spread", "h1e60")
         and c not in VP.NON_STATIONARY_FEATURES]
    out[cols].to_csv(args.out, index=False)
    print(f"[out] {args.out}  rows={len(out)} cols={len(cols)}  "
          f"已剔除非平稳特征={list(VP.NON_STATIONARY_FEATURES)}")


if __name__ == "__main__":
    main()
