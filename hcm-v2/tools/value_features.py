#!/usr/bin/env python3
"""value_features.py — Step-2.4「前瞻价值头」实时特征与推理（影子观测用）。

与 build_path_labels.py 训练口径严格一致：
  - H1 趋势世界（EMA60×DI×ADX）→ 空头段价格镜像 → 伪序列 enrich（quality_features）
  - 每候选点特征含 risk/rise_atr/dist_h1e_atr/session/world + enrich 全量指标。
推理仅取伪序列**最后一根（当前已收盘 bar）**，world≠0 时输出价值分（E[R] 期望），
world=0（无趋势世界）返回 None（观测语义：该时段不在任何顺向世界，不评价值）。

用法（供 quality_scorer 主循环调用，亦可用于离线 sanity）:
    booster = value_features.load_model()
    v = value_features.compute(conn, "XAUUSD", booster, window_m5=240)
    # v = {"score": 0.62, "world": 1, "risk": 2.1, "ts": ...} 或 None
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

try:
    import quality_features as QF  # noqa: E402
except Exception as _e:  # pragma: no cover
    print(f"[fatal] quality_features import failed: {_e}", file=sys.stderr)
    raise

SWING = 20
ENTER_TS = 60.0
DEFAULT_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "models", "lgbm_value_v1.txt")
EXCLUDE = {"symbol", "open_time", "world", "label", "close", "h1e60"}


def h1_world_dir(h1: pd.DataFrame) -> pd.Series:
    """每根 H1 收盘后的世界方向 +1/-1/0（index = close_time）。与 build_path_labels 同口径。"""
    cl = h1["close"].astype(float)
    hi, lo = h1["high"].astype(float), h1["low"].astype(float)
    ema60 = cl.ewm(span=60, adjust=False).mean()
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


def load_model(path: str = DEFAULT_MODEL):
    import lightgbm as lgb
    return lgb.Booster(model_file=path)


def compute(conn, symbol: str, booster, window_m5: int = 240) -> dict | None:
    """用最近 M5/H1 K 线计算当前价值分（影子观测，只读）。失败/无世界返回 None。"""
    try:
        m5 = pd.read_sql(
            "SELECT open_time, open, high, low, close, spread FROM hcm_market.klines "
            "WHERE symbol=%s AND time_frame='M5' ORDER BY open_time DESC LIMIT %s",
            conn, params=(symbol, window_m5))
        m5 = m5.sort_values("open_time").reset_index(drop=True)
        h1 = pd.read_sql(
            "SELECT open_time, open, high, low, close, spread FROM hcm_market.klines "
            "WHERE symbol=%s AND time_frame='H1' ORDER BY open_time DESC LIMIT 120",
            conn, params=(symbol,))
        h1 = h1.sort_values("open_time").reset_index(drop=True)
        if len(m5) < 120 or len(h1) < 60:
            return None
        m5["open_time"] = pd.to_datetime(m5["open_time"], utc=True)
        h1["open_time"] = pd.to_datetime(h1["open_time"], utc=True)
    except Exception as e:
        print(f"[value] klines load failed: {e}", file=sys.stderr)
        return None

    w = h1_world_dir(h1).rename("world")
    m5 = m5.merge(w, how="left", left_on="open_time", right_index=True)
    m5["world"] = m5["world"].ffill().fillna(0).astype(int)
    world_now = int(m5["world"].iloc[-1])
    if world_now == 0:
        return {"score": None, "world": 0, "reason": "no_world"}

    orig_close = m5["close"].astype(float).to_numpy()   # 镜像前的真实 close（供乖离计算）
    m5m = m5.copy()
    for col in ("open", "high", "low", "close"):
        m5m[col] = np.where(m5m["world"] == -1, -m5m[col], m5m[col])
    try:
        kl = QF.enrich_klines(m5m.reset_index(drop=True))
        kl = kl.assign(open_time=m5m["open_time"].reset_index(drop=True),
                       world=m5m["world"].reset_index(drop=True))
    except Exception as e:
        print(f"[value] enrich failed: {e}", file=sys.stderr)
        return None

    atr = kl["atr"]
    lo20 = kl["low"].rolling(SWING).min()
    kl["risk"] = (kl["close"] - lo20.shift(1)).clip(lower=0.3 * atr, upper=3.0 * atr)
    kl["rise_atr"] = (kl["close"] - lo20) / atr.replace(0, np.nan)
    # 顺向 H1-EMA60 乖离 = world × (真实 close − 真实 H1 EMA60)/M5 ATR。
    # （训练在伪序列上等价于此 sign 变换；逐段镜像 H1 需更长窗口，sanity 发现窗口失配，
    #   故用 sign 口径——段内一致、避免 ewm 起点/窗口污染。）
    h1e_last = float(h1["close"].astype(float).ewm(span=60, adjust=False).mean().iloc[-1])
    atr_last = float(atr.iloc[-1])
    dist_last = (world_now * (float(orig_close[-1]) - h1e_last) / atr_last) if atr_last > 0 else 0.0
    kl["dist_h1e_atr"] = dist_last  # 仅末行用于预测（整列占位同值，推理只用 iloc[-1]）

    row = kl.iloc[-1].copy()
    names = booster.feature_name()
    feats = {}
    for c in names:
        v = row.get(c)
        if c in ("session_asia", "session_eu", "session_us"):
            v = QF.session_onehot(row["open_time"]).get(c, 0)
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
