"""reversal_features.py —— 「反转头」特征与指标的唯一真源（离线训练 / 线上推理共用）。

【为什么必须是单一真源】
反转头的 22 维特征中，14 维是行情指标（ADX/RSI/MACD/ER/BBW/Donchian/ATR…）。
离线训练由 build_reversal_labels.py 计算，线上推理由 quality_scorer.py 计算。
若两边各写一份实现，口径稍有差异（EMA alpha、Wilder 平滑、rolling min_periods…）
就会造成 train-serve skew —— 分数失真、AUC 0.723 直接退化成随机。
故本模块是唯一实现，两边都 import 它。

【无未来函数保证】
所有指标均为「向后看」：递归类（EMA/Wilder ATR/ADX）在位置 i 只依赖 <=i 的棒；
滚动类（bbw/donchian/er）窗口末端为 i。因此在完整序列上算完再取 [i]，
等价于只用 series[:i+1] 计算 —— 这是特征平价校验成立的前提。

【用途】
  离线：build_reversal_labels.py → build_indicators() / assemble()
  线上：quality_scorer.py       → assemble()
  校验：_scratch/validate_reversal_parity.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

ATR_PERIOD = 14
SYMBOL = "XAUUSD"
TF = "M5"

# 触发与判定口径（与训练标签保持一致，勿单边修改）
TRIGGER_DD_ATR = 0.6     # 触发：浮亏 > 0.6 ATR
REV_CONT_ATR = 1.0       # 标签：从 T 起逆行再走 1.0 ATR = 反转
PB_RECOVER_ATR = 0.2     # 标签：浮亏缩回 0.2 ATR = 回踩

# 训练时使用的 22 维特征顺序（模型契约，改动须重训）
FEATURE_ORDER = [
    "direction", "pos_drawdown_atr", "pos_bars_in_trade", "pos_mfe_atr",
    "pos_mae_atr", "pos_sl_dist_atr", "pos_tp_dist_atr", "session",
    "adx_14", "rsi_14", "macd", "macd_hist", "di_plus", "di_minus",
    "di_net", "er", "bbw", "donchian_q", "close_mom_atr",
    "body_ratio", "upper_wick", "lower_wick",
]


# ────────────────────────── 指标（唯一实现）──────────────────────────
def true_range(h, l, c):
    pc = np.roll(c, 1)
    pc[0] = c[0]
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def ema(a, n):
    return pd.Series(a).ewm(span=n, adjust=False).mean().to_numpy()


def rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag = pd.Series(gain).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    al = pd.Series(loss).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    rs = ag / np.where(al == 0, np.nan, al)
    return 100 - 100 / (1 + rs)


def atr_wilder(h, l, c, n=ATR_PERIOD):
    """Wilder ATR(n)。"""
    tr = true_range(h, l, c)
    out = np.full(len(tr), np.nan)
    if len(tr) < n:
        return out
    out[n - 1] = np.nanmean(tr[:n])
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def adx(h, l, c, n=ATR_PERIOD):
    """Wilder ADX(n) + DI+/DI-。返回 (adx, di_plus, di_minus)。"""
    up = np.diff(h, prepend=h[0])
    dn = -np.diff(l, prepend=l[0])
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(h, l, c)
    atr_ = atr_wilder(h, l, c, n)
    ap = np.full(len(tr), np.nan)
    am = np.full(len(tr), np.nan)
    for i in range(n - 1, len(tr)):
        if atr_[i] and atr_[i] > 0:
            ap[i] = 100 * np.nansum(plus_dm[i - n + 1:i + 1]) / (atr_[i] * n)
            am[i] = 100 * np.nansum(minus_dm[i - n + 1:i + 1]) / (atr_[i] * n)
    dx = np.full(len(tr), np.nan)
    denom = ap + am
    valid = denom > 0
    dx[valid] = 100 * np.abs(ap[valid] - am[valid]) / denom[valid]
    adx_ = np.full(len(tr), np.nan)
    if len(tr) >= 2 * n - 1:
        adx_[2 * n - 2] = np.nanmean(dx[n - 1:2 * n - 1])
        for i in range(2 * n - 1, len(tr)):
            adx_[i] = (adx_[i - 1] * (n - 1) + dx[i]) / n
    return adx_, ap, am


def efficiency_ratio(c, n=10):
    net = np.abs(c - np.roll(c, n))
    path = np.abs(np.diff(c, prepend=c[0]))
    path_sum = pd.Series(path).rolling(n, min_periods=n).sum().to_numpy()
    out = np.full(len(c), np.nan)
    ok = path_sum > 0
    out[ok] = net[ok] / path_sum[ok]
    out[:n] = np.nan
    return out


def session_of(ts):
    """时段（UTC）：asia 00-08 / europe 08-13 / us 13-22，22-24 归 asia。"""
    hh = ts.hour if isinstance(ts, pd.Timestamp) else pd.Timestamp(ts).hour
    if 8 <= hh < 13:
        return "europe"
    if 13 <= hh < 22:
        return "us"
    return "asia"


def build_indicators(df):
    """在整段 K 线上预计算指标数组；取 [i] 即"只用 <=i 的数据"的结果。"""
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    o = df["open"].to_numpy()
    ind = {}
    ind["atr"] = atr_wilder(h, l, c)
    ind["rsi"] = rsi(c)
    ind["macd"] = ema(c, 12) - ema(c, 26)
    ind["macd_sig"] = ema(ind["macd"], 9)
    ind["adx"], ind["di_plus"], ind["di_minus"] = adx(h, l, c)
    ind["er"] = efficiency_ratio(c, 10)
    mid = pd.Series(c).rolling(20, min_periods=20).mean().to_numpy()
    sd = pd.Series(c).rolling(20, min_periods=20).std().to_numpy()
    ind["bbw"] = np.where(mid > 0, (4 * sd) / np.where(mid == 0, np.nan, mid), np.nan)
    hh20 = pd.Series(h).rolling(20, min_periods=20).max().to_numpy()
    ll20 = pd.Series(l).rolling(20, min_periods=20).min().to_numpy()
    ind["donchian_q"] = np.where(hh20 > ll20, (c - ll20) / (hh20 - ll20), np.nan)
    atr_ = ind["atr"]
    mom = c - np.roll(c, 5)
    ind["close_mom_atr"] = np.where(atr_ > 0, mom / np.where(atr_ == 0, np.nan, atr_), np.nan)
    hl = np.where((h - l) > 1e-9, h - l, 1e-9)
    ind["body_ratio"] = np.abs(c - o) / hl
    ind["upper_wick"] = (h - np.maximum(o, c)) / hl
    ind["lower_wick"] = (np.minimum(o, c) - l) / hl
    # ── 结构/拐点/衰竭特征（2026-09-04 增强；全部向后看，ATR 归一）──
    hh10 = pd.Series(h).rolling(10, min_periods=10).max().to_numpy()
    ll10 = pd.Series(l).rolling(10, min_periods=10).min().to_numpy()
    ind["hh20"] = hh20
    ind["ll20"] = ll20
    ind["hh10"] = hh10
    ind["ll10"] = ll10
    ind["uwick_atr"] = np.where(atr_ > 0,
                                (h - np.maximum(o, c)) / np.where(atr_ == 0, np.nan, atr_), np.nan)
    ind["dwick_atr"] = np.where(atr_ > 0,
                                (np.minimum(o, c) - l) / np.where(atr_ == 0, np.nan, atr_), np.nan)
    ind["body_atr"] = np.where(atr_ > 0,
                               np.abs(c - o) / np.where(atr_ == 0, np.nan, atr_), np.nan)
    return ind


# ────────────────────────── 特征装配（离线/线上共用）──────────────────────────
def assemble(pos, df, ind, T, entry_idx):
    """在触发点 T 装配 22 维特征。

    pos        : dict(direction, open_price, sl, tp)  —— 开仓价/SL/TP 来自持仓快照
    df         : K 线 DataFrame（含 open/high/low/close/open_time）
    ind        : build_indicators(df) 的结果
    T          : 触发点下标（线上=当前最后一根已收盘棒）
    entry_idx  : 入场棒下标
    返回 dict（键与 FEATURE_ORDER 一致）。
    """
    h = df["high"].to_numpy(); l = df["low"].to_numpy(); c = df["close"].to_numpy()
    direction = pos["direction"]
    entry = float(pos["open_price"])
    sign = 1.0 if direction == "BUY" else -1.0     # BUY 逆行=跌；SELL 逆行=涨
    a_T = float(ind["atr"][T])
    if not np.isfinite(a_T) or a_T <= 0:
        return None

    dd_T = sign * (entry - c[T]) / a_T
    seg_h = h[entry_idx:T + 1]
    seg_l = l[entry_idx:T + 1]
    if direction == "BUY":
        mfe = (np.nanmax(seg_h) - entry) / a_T
        mae = (entry - np.nanmin(seg_l)) / a_T
    else:
        mfe = (entry - np.nanmin(seg_l)) / a_T
        mae = (np.nanmax(seg_h) - entry) / a_T

    sl = float(pos.get("sl") or 0)
    tp = float(pos.get("tp") or 0)

    def g(k):
        v = ind[k][T]
        return float(v) if v is not None and np.isfinite(v) else np.nan

    feat = {
        "direction": direction,
        "pos_drawdown_atr": dd_T,
        "pos_bars_in_trade": int(T - entry_idx),
        "pos_mfe_atr": float(mfe),
        "pos_mae_atr": float(mae),
        "pos_sl_dist_atr": (sign * (entry - sl) / a_T) if sl > 0 else np.nan,
        "pos_tp_dist_atr": (-sign * (entry - tp) / a_T) if tp > 0 else np.nan,
        "session": session_of(df["open_time"].iloc[T]),
        "adx_14": g("adx"),
        "rsi_14": g("rsi"),
        "macd": g("macd"),
        "macd_hist": g("macd") - g("macd_sig"),
        "di_plus": g("di_plus"),
        "di_minus": g("di_minus"),
        "di_net": g("di_plus") - g("di_minus"),
        "er": g("er"),
        "bbw": g("bbw"),
        "donchian_q": g("donchian_q"),
        "close_mom_atr": g("close_mom_atr"),
        "body_ratio": g("body_ratio"),
        "upper_wick": g("upper_wick"),
        "lower_wick": g("lower_wick"),
    }
    # ── 结构/拐点/衰竭（2026-09-04 特征增强，向后看）──
    _hh20, _ll20 = g("hh20"), g("ll20")
    _hh10, _ll10 = g("hh10"), g("ll10")
    feat["hh20_dist_atr"] = (_hh20 - c[T]) / a_T if np.isfinite(_hh20) else np.nan
    feat["ll20_dist_atr"] = (c[T] - _ll20) / a_T if np.isfinite(_ll20) else np.nan
    if T >= 10:
        _p10h, _p10l = ind["hh10"][T - 10], ind["ll10"][T - 10]
        feat["hh10_mom_atr"] = ((_hh10 - _p10h) / a_T
                                if np.isfinite(_hh10) and np.isfinite(_p10h) else np.nan)
        feat["ll10_mom_atr"] = ((_ll10 - _p10l) / a_T
                                if np.isfinite(_ll10) and np.isfinite(_p10l) else np.nan)
    else:
        feat["hh10_mom_atr"] = feat["ll10_mom_atr"] = np.nan
    feat["uwick_atr"] = g("uwick_atr")
    feat["dwick_atr"] = g("dwick_atr")
    feat["body_atr"] = g("body_atr")
    # 与训练口径必须完全一致：数值特征四舍五入到 4 位小数
    # （模型是在 4 位小数版特征上训练的，线上不 round 即产生 train-serve skew）
    for k, v in feat.items():
        if isinstance(v, float) and np.isfinite(v):
            feat[k] = round(v, 4)
    return feat
