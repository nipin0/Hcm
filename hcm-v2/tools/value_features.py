#!/usr/bin/env python3
"""value_features.py — Step-2.4「前瞻价值头」实时特征与推理（影子观测用）。

【2026-09-12 重构】特征计算**全部委托 value_pipeline.build_features**（训练/推理唯一共用）。
本文件只负责：拉取定长窗口 K 线 → 取末根特征 → 一次推理 → 返回分数字典。

修复（根因与证明见 value_pipeline 模块文档）：
  · Bug-A：H1 镜像索引错配（`w.reindex(h1.index)`）→ H1 从不镜像 → world=−1 的
           dist_h1e_atr = (−4300−4300)/ATR ≈ −733 的量纲垃圾。→ 已删除 H1 镜像，
           改「sign 口径」dist = world×(真实 close − 真实 H1 EMA60)/ATR（本文件原已如此）。
  · Bug-B：逐 bar 取反【价格水平】使 world 切换处价格 +4400→−4400 跳变 → ATR 被放大
           10~100 倍（实测 84.57/126.26，max 1415/1898）。→ 已改为差分链式仿射镜像
           （段内 ≡ −price+常数，跨段连续），可证 ATR(pseudo) ≡ ATR(real)。
  · 口径统一：两侧同调 build_features。固定长度 rolling 逐值一致；EWM 仅差初始种子
           （M5_WINDOW=1200 / H1 丢弃前 240 根后，残差 <1e-4）。见 VP.WARMUP_NOTE。

用法（供 quality_scorer 主循环调用，亦可用于离线 sanity）:
    booster = value_features.load_model()
    v = value_features.compute(conn, "XAUUSD", booster)
    # v = {"score": 0.62, "world": 1, "risk": 2.1, ...} 或 {"score": None, "world": 0, "reason": "no_world"}
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

import value_pipeline as VP  # 训练/推理唯一共用特征管线（禁止在本地复制实现）

# 【原子切换 2026-09-12】本模块用 value_pipeline（现已含：坏棒治理 + 剔除 ema20 +
# spread_atr 量纲修正），故必须配套同口径的 v3 模型：
#   · v1 = 旧管线；v2 = 修复管线但【未做坏棒治理、仍含 ema20、spread_atr 量纲错】
#   · v3 = 坏棒治理 + ema20 剔除 + spread_atr 修正 + 无泄漏训练（labels_path_v3.csv）
# 实测（v3，无泄漏）：Spearman=0.1152；多窗 top20% 7/9 优于全体；阈值不敏感（0.5→1.0
# 单笔 E[R] +0.169→+0.162）；在【信号候选】上 gate 方向随行情翻转（3 子窗 −0.499* / +0.280* /
# −0.094）⇒ **不足以支撑再作为闸门**，见评审结论。
# 回退：三处一起退回（value_pipeline.py / value_features.py / 本行）+ 重启侧车。
DEFAULT_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "models", "lgbm_value_v3.txt")

# 兼容旧引用名（原为本模块自带常量/函数，现统一由 value_pipeline 提供）
h1_world_dir = VP.h1_world_dir
SWING = VP.SWING
ENTER_TS = VP.ENTER_TS
M5_WINDOW = VP.M5_WINDOW
H1_WINDOW = VP.H1_WINDOW

_M5_MIN = 300          # 最少 M5 根数（dev_z_ema200 需 rolling 200 + hull）
_H1_MIN = VP.H1_EMA_WARMUP + 60   # warm-up 240 之外还需覆盖 M5 窗口的 H1 参考


def load_model(path: str = DEFAULT_MODEL):
    import lightgbm as lgb
    return lgb.Booster(model_file=path)


def _load_window(conn, symbol: str, window_m5: int) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """拉取 M5/H1 定长窗口（与训练侧同口径所需的 warm-up 长度）。失败返回 None。"""
    try:
        m5 = pd.read_sql(
            "SELECT open_time, open, high, low, close, spread FROM hcm_market.klines "
            "WHERE symbol=%s AND time_frame='M5' ORDER BY open_time DESC LIMIT %s",
            conn, params=(symbol, window_m5))
        m5 = m5.sort_values("open_time").reset_index(drop=True)
        h1 = pd.read_sql(
            "SELECT open_time, open, high, low, close, spread FROM hcm_market.klines "
            "WHERE symbol=%s AND time_frame='H1' ORDER BY open_time DESC LIMIT %s",
            conn, params=(symbol, H1_WINDOW))
        h1 = h1.sort_values("open_time").reset_index(drop=True)
        if len(m5) < _M5_MIN or len(h1) < _H1_MIN:
            print(f"[value] insufficient history M5={len(m5)} H1={len(h1)}", file=sys.stderr)
            return None
        m5["open_time"] = pd.to_datetime(m5["open_time"], utc=True)
        h1["open_time"] = pd.to_datetime(h1["open_time"], utc=True)
        return m5, h1
    except Exception as e:
        print(f"[value] klines load failed: {e}", file=sys.stderr)
        return None


def compute(conn, symbol: str, booster, window_m5: int = M5_WINDOW) -> dict | None:
    """用最近 M5/H1 K 线计算当前价值分（只读）。失败/无世界返回 None。

    world=0（无趋势世界）→ {"score": None, "world": 0, "reason": "no_world"}。
    """
    loaded = _load_window(conn, symbol, window_m5)
    if loaded is None:
        return None
    m5, h1 = loaded

    kl = VP.build_features(m5, h1)
    if kl.empty:
        return None
    world_now = int(kl["world"].iloc[-1])
    if world_now == 0:
        return {"score": None, "world": 0, "reason": "no_world"}

    row = kl.iloc[-1]
    names = booster.feature_name()
    feats = {}
    for c in names:
        v = row.get(c)
        if c in ("session_asia", "session_eu", "session_us"):
            v = VP.QF.session_onehot(row["open_time"]).get(c, 0)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return {"score": None, "world": world_now, "reason": f"nan_feature:{c}"}
        feats[c] = float(v)
    if any(c not in feats for c in names):
        return {"score": None, "world": world_now, "reason": "feature_mismatch"}

    X = pd.DataFrame([feats])[names]
    score = float(booster.predict(X)[0])
    return {"score": round(score, 4), "world": world_now,
            "risk": float(row.get("risk") or 0.0) if row.get("risk") else None,
            "rise_atr": float(row.get("rise_atr") or 0.0),
            "dist_h1e_atr": float(row.get("dist_h1e_atr") or 0.0)}


if __name__ == "__main__":
    import psycopg2
    conn = psycopg2.connect(os.environ.get("DB_URL",
                                           "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"))
    b = load_model()
    v = compute(conn, "XAUUSD", b)
    print("value:", v)
