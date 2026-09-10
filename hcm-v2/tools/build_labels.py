#!/usr/bin/env python3
"""build_labels.py — 阶段 0 标签构造器（只读，零实盘影响）。

按用户规格构造二分类标签：
  入场后 ``label_horizon_bars`` 根 M5 内，
    先触 +label_r_win·R  ->  label = 1 (win)
    先触 -label_r_loss·R  ->  label = 0 (loss)
  其中 R = |entry - SL| 的风险距离。

排除规则（不进训练集，避免标签噪声）：
  - SL 无法推导（sl_price<=0 且无 ai_sl_mult/ATR）
  - 前向 M5 K 线不足 label_horizon_bars 根
  - label_horizon_bars 根内都未触及 ±R
  - 单根 K 线同时触及止盈与止损（同根双触，无法判先后）

纪律红线：本脚本只读 PG，不写任何数据；产物为本地 CSV。

用法:
  DB_URL=postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2 \
    python build_labels.py --out labels.csv --mode 'HEXP:%'
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timezone

import numpy as np
import pandas as pd
import psycopg2

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"

# 配置回退默认值（与 seed SQL 0011_ai_quality_config.sql 对齐；禁硬编码）
# 标签口径 2026-08-14 重锚：由不对称 1.5R/1R 改为对称 1R/1R（实测 AUC 0.65→0.84，
# 基线胜率 15.5%→20.8%，阈值 0.5/0.6/0.7 才能有意义）。参数仍以配置为准，此处仅兜底。
CFG_FALLBACK = {
    "ai.lm.label_r_win": 1.0,
    "ai.lm.label_r_loss": 1.0,
    "ai.lm.label_horizon_bars": 12,
    "ai.lm.label_sl_atr_fallback": 2.0,
    # 【2026-08-28·质量头治本·标签口径统一】质量标签的 R（风险距离）来源。
    # 根因：引擎在 AI 未生效时故意不写 sl_price（scheduler.py:2377-2378，桥依赖 0=回退会话 SL），
    # 导致历史信号 sl_price=0 → 标签走 atr×2.0 fallback；而近期 AI 生效的信号 sl_price>0 →
    # 标签走真实 SL 距离 R=|entry-sl|。同一训练集混了两种语义的 R → 质量头 AUC≈0.5 不可学。
    #   atr_fallback(默认·向后兼容): 全部用 atr×label_sl_atr_fallback，标签口径统一
    #   real(推荐): 仅保留 sl_price>0 的信号（R=真实 SL 距离），标签口径统一且与入场质量挂钩
    "ai.lm.label_sl_source": "atr_fallback",
    # 【阶段 0·方向头/买点头标签】独立于 hexp 方向，由未来 K 线方向驱动。
    "ai.lm.dir_atr_mult": 0.8,        # 方向幅度阈值(ATR 倍数)：未来 N 根 close 相对 entry 涨/跌 ≥ ±0.8·ATR → 有方向
    "ai.lm.dir_horizon_bars": 24,     # 方向展望期(根 M5)，长于质量头 12 以稳方向
    # 【2026-09-02 趋势对齐】方向头标签只学"顺 H1 主趋势"的运动方向：
    #   逆 H1 趋势的未来 ±dir_atr_mult 运动压为 FLAT(0) —— 趋势市不教摸顶/抄底
    #   （根治"buy 趋势行情判 SELL、被 flip 成逆势空单"系统性背离）。
    "ai.lm.dir_trend_align": "true",  # 开关：false 则完全退回旧双向(纯未来收益)语义
    "ai.lm.dir_trend_tf": "H1",       # 趋势基准周期（当前实现仅 H1；扩展需另加载对应 tf）
}


def load_config(conn) -> dict:
    """从 hcm_config.metadata 读 ai.lm.label_* 键；缺失回退 CFG_FALLBACK。"""
    cfg = dict(CFG_FALLBACK)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config_key, current_value FROM hcm_config.metadata "
                "WHERE config_key = ANY(%s)",
                (list(CFG_FALLBACK.keys()),),
            )
            for key, val in cur.fetchall():
                if val is not None and str(val).strip() != "":
                    try:
                        cfg[key] = float(val)
                    except (TypeError, ValueError):
                        pass
    except Exception as exc:  # 配置读失败不阻断，回退默认值
        print(f"[warn] config load failed, using fallback: {exc}", file=sys.stderr)
    return cfg


def load_ds_output(conn) -> dict:
    """加载 DeepSeek 落库票 hcm_ai.ds_output（设计文档 1.3 标签校准源）。

    返回 {symbol: [(created_at, fake_prob, ai_sl_coeff, continuity_score), ...]} 按时间升序。
    训练侧 quality_features.load_ds_output 同源；此处用于给标签加样本权重，
    不改变 label 本身（纪律红线：标签口径由价格触达决定，DeepSeek 只调权重）。
    """
    out: dict = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, created_at, fake_prob, ai_sl_coeff, continuity_score "
                "FROM hcm_ai.ds_output ORDER BY created_at",
            )
            for sym, ts, fp, sl, cont in cur.fetchall():
                out.setdefault(sym, []).append(
                    (ts, fp if fp is not None else 0.0,
                     sl if sl is not None else 0.0, cont if cont is not None else 0.0)
                )
    except Exception as exc:
        print(f"[warn] ds_output load failed: {exc}", file=sys.stderr)
    return out


def _nearest_ds(ds_list, ts, window_sec: int = 86400):
    """返回 ts 时间最近一条 ds_output 的四元组（双向最近邻，±window_sec）。

    【2026-08-17 修正】原实现只取 t<=ts（过去），但 DeepSeek 异步票(ai:ds:out)
    与信号生产时间接近（分钟级），历史信号可能略早于/晚于票时间 → 单向匹配必落空。
    改为双向最小时间差，与推理侧"读当前实时票"语义对齐（训练-推理同分布）。
    """
    if not ds_list:
        return None
    best = None
    best_dt = None
    for item in ds_list:
        t = item[0]
        dt = abs((ts - t).total_seconds())
        if dt <= window_sec and (best is None or dt < best_dt):
            best = item
            best_dt = dt
    return best


def load_klines(conn, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """一次性加载各 symbol 的 M5 K 线（按 open_time 排序），供标签回看。"""
    out: dict[str, pd.DataFrame] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, open_time, high, low, close FROM hcm_market.klines "
            "WHERE symbol = ANY(%s) AND time_frame = 'M5' ORDER BY open_time",
            (symbols,),
        )
        rows = cur.fetchall()
    if rows:
        df = pd.DataFrame(rows, columns=["symbol", "open_time", "high", "low", "close"])
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
        for sym, g in df.groupby("symbol"):
            out[sym] = g.sort_values("open_time").reset_index(drop=True)
    return out


def label_one(sig_row, kl: pd.DataFrame, cfg: dict, events=None):
    """对单条信号构造标签。返回 (label|None, reason, R, hit_idx)。"""
    direction = sig_row["signal_dir"]
    entry = float(sig_row["entry_price"])

    # ── 推导 R（风险距离）──
    sl_price = sig_row.get("sl_price")
    atr = sig_row.get("atr_14")
    ai_sl_mult = sig_row.get("ai_sl_mult")
    # 【2026-08-28·标签口径统一】按 ai.lm.label_sl_source 决定 R 来源，杜绝同集混语义。
    _sl_source = str(cfg.get("ai.lm.label_sl_source", "atr_fallback") or "atr_fallback").lower()
    if _sl_source == "real":
        # 真实 SL 口径：无有效 sl_price 的信号直接排除（保证全库标签=真实 SL 距离 R，
        # 与入场质量挂钩，使入场质量特征对标签有预测力）。
        if sl_price is None or not (float(sl_price) > 0) or abs(float(sl_price) - entry) <= 1e-9:
            return None, "no_real_sl", 0.0, None
        R = abs(entry - float(sl_price))
    else:
        # 统一 ATR fallback 口径：忽略 sl_price（即便有真实 SL 也不用），全库 R=atr×mult。
        # 保证标签口径一致（修复历史与近期混用两种 R 定义导致质量头不可学）。
        if not (atr and float(atr) > 0):
            return None, "no_sl_atr", 0.0, None
        mult = float(ai_sl_mult) if ai_sl_mult and float(ai_sl_mult) > 0 else cfg["ai.lm.label_sl_atr_fallback"]
        R = mult * float(atr)
    if R <= 0:
        return None, "zero_R", 0.0, None

    r_win = cfg["ai.lm.label_r_win"] * R
    r_loss = cfg["ai.lm.label_r_loss"] * R
    horizon = int(cfg["ai.lm.label_horizon_bars"])

    created = sig_row["created_at"]
    if pd.isna(created) or kl is None or kl.empty:
        return None, "no_klines", R, None

    # 入场后第一根 M5：open_time >= created_at
    idx = kl["open_time"].searchsorted(created, side="left")
    window = kl.iloc[idx: idx + horizon]
    if len(window) < horizon:
        return None, "insufficient_forward", R, None
    # 【P2-T7a】跳空缺口剔除：相邻 M5 bar 出现 >2*ATR 价格缺口 → 跳空。
    # 跳空使 R 触达判定失真（价格跳穿 stop/target 非真实波动）→ 剔除该样本。
    _atr = float(atr) if (atr and float(atr) > 0) else None
    for i in range(1, len(window)):
        _ph, _pl = float(window.iloc[i-1]["high"]), float(window.iloc[i-1]["low"])
        _ch, _cl = float(window.iloc[i]["high"]), float(window.iloc[i]["low"])
        if _atr and _atr > 0:
            _gap = 2.0 * _atr
            if _cl - _ph > _gap or _pl - _ch > _gap:
                return None, "gap_skip", R, i
        else:
            if _cl > _ph * 1.002 or _ch < _pl * 0.998:
                return None, "gap_skip", R, i
    # 【P2-T7b】重大消息 bar 剔除：入场窗口与高 importance 宏观事件窗口重叠 → 剔除。
    if events:
        _w0 = window.iloc[0]["open_time"]
        _w1 = window.iloc[-1]["open_time"]
        for _ev in events:
            _s, _e, _imp = _ev
            if _imp >= 2 and not (_w1 < _s or _w0 > _e):
                return None, "event_skip", R, None

    is_buy = direction == "BUY"
    for i, (_, bar) in enumerate(window.iterrows()):
        hi = float(bar["high"]); lo = float(bar["low"])
        if is_buy:
            hit_target = hi >= entry + r_win
            hit_stop = lo <= entry - r_loss
        else:
            hit_target = lo <= entry - r_win
            hit_stop = hi >= entry + r_loss
        if hit_target and hit_stop:
            return None, "both_touched_same_bar", R, i
        if hit_target:
            return 1, "target_first", R, i
        if hit_stop:
            return 0, "stop_first", R, i
    return None, "no_touch_in_horizon", R, horizon


# ── 阶段 0·方向头标签：独立于 hexp 方向，纯看未来 K 线走向 ──
def _h1_trend_dir(h1kl, ts) -> int:
    """ts 时刻**已收盘** H1 棒的主趋势方向：+1=TREND_UP / -1=TREND_DOWN / 0=其它或缺失。

    复用 quality_features._period_trend_state（ma close vs EMA60 与 DI 同向 + ADX 分段
    TrendScore≥enter），与生产 hexp 共振矩阵口径一致。只读过去已收盘棒 → 无未来泄露。
    """
    if h1kl is None or h1kl.empty:
        return 0
    try:
        import quality_features as _qf
    except Exception:
        return 0
    i = _qf._bar_at_or_before(h1kl, ts)
    if i is None:
        return 0
    try:
        row = h1kl.iloc[i]
        _adx = float(row["adx_14"])
        _pdi = float(row["plus_di"])
        _mdi = float(row["minus_di"])
        _ema60 = float(row["ema60"])
        _close = float(row["close"])
    except Exception:
        return 0
    if any(pd.isna(v) for v in (_adx, _pdi, _mdi, _ema60, _close)):
        return 0
    _st = _qf._period_trend_state(_adx, _pdi, _mdi, _close, _ema60)
    if _st == "TREND_UP":
        return 1
    if _st == "TREND_DOWN":
        return -1
    return 0


def dir_label_one(sig_row, kl: pd.DataFrame, cfg: dict, h1kl: pd.DataFrame | None = None):
    """对单条信号构造「市场方向」标签（不看 signal_dir，彻底解耦 hexp）。

    基础语义（不变）：未来 ``dir_horizon_bars`` 根 M5 收盘相对 entry 的标准化收益：
        fut_ret = (close_N - entry) / atr
        fut_ret >= +dir_atr_mult  -> +1 (BUY 方向)
        fut_ret <= -dir_atr_mult  -> -1 (SELL 方向)
        否则                      ->  0 (FLAT 横盘)

    【2026-09-02 趋势对齐·ai.lm.dir_trend_align】H1 主趋势约束：
        TREND_UP   -> 未来下跌段(原 -1) 压为 0(FLAT) —— 上升趋势不教"摸顶做空"；
        TREND_DOWN -> 未来上涨段(原 +1) 压为 0 —— 下降趋势不教"抄底做多"；
        RANGE/TRANSITION / H1 数据缺失 -> 保留原双向（震荡市均值回归仍可学）。
    开关=false 或 h1kl 缺失时完全退回旧纯收益语义（向后兼容）。

    纪律红线：标签仅用未来 K 线（监督学习标准），趋势约束只读过去已收盘 H1，无泄露。
    排除：atr 缺失 / 前向 K 线不足。
    """
    entry = float(sig_row["entry_price"])
    atr = sig_row.get("atr_14")
    if atr is None or float(atr) <= 0:
        return None
    atr = float(atr)
    x_dir = float(cfg.get("ai.lm.dir_atr_mult", 0.8))
    n = int(cfg.get("ai.lm.dir_horizon_bars", 24))
    created = sig_row["created_at"]
    if pd.isna(created) or kl is None or kl.empty:
        return None
    idx = kl["open_time"].searchsorted(created, side="left")
    window = kl.iloc[idx: idx + n]
    if len(window) < n:
        return None
    fut_ret = (float(window.iloc[-1]["close"]) - entry) / atr
    if fut_ret >= x_dir:
        raw = 1
    elif fut_ret <= -x_dir:
        raw = -1
    else:
        raw = 0
    _align = str(cfg.get("ai.lm.dir_trend_align", "true")).strip().lower()
    if _align in ("true", "1", "yes", "on"):
        _td = _h1_trend_dir(h1kl, created) if h1kl is not None else 0
        if _td > 0 and raw == -1:
            return 0
        if _td < 0 and raw == 1:
            return 0
    return raw


# ── 阶段 0·买点头标签：条件于方向头预测方向(训练时用真实 dir_label)的 R 触达 ──
def entry_label_one(sig_row, kl: pd.DataFrame, cfg: dict, dir_val, events=None):
    """在 ``dir_val`` 方向上构造「买点质量」标签（学习驱动精准点位）。

    与 label_one 同构（复用 R 触达逻辑），唯一区别：方向来源从 signal_dir 换成 dir_val。
        dir_val==+1(做多)：未来 horizon 根内先触 +1R -> 1(好买点)，先触 -1R -> 0
        dir_val==-1(做空)：对称
        dir_val 为 None/0(FLAT) -> None（无方向不评买点）
    排除规则同 label_one（跳空/事件/同根双触/前向不足）。
    """
    if dir_val is None or dir_val == 0:
        return None
    entry = float(sig_row["entry_price"])
    sl_price = sig_row.get("sl_price")
    atr = sig_row.get("atr_14")
    ai_sl_mult = sig_row.get("ai_sl_mult")
    if sl_price is not None and float(sl_price) > 0 and abs(float(sl_price) - entry) > 1e-9:
        R = abs(entry - float(sl_price))
    elif atr and float(atr) > 0:
        mult = float(ai_sl_mult) if ai_sl_mult and float(ai_sl_mult) > 0 else cfg["ai.lm.label_sl_atr_fallback"]
        R = mult * float(atr)
    else:
        return None
    if R <= 0:
        return None
    r_win = cfg["ai.lm.label_r_win"] * R
    r_loss = cfg["ai.lm.label_r_loss"] * R
    horizon = int(cfg["ai.lm.label_horizon_bars"])
    created = sig_row["created_at"]
    if pd.isna(created) or kl is None or kl.empty:
        return None
    idx = kl["open_time"].searchsorted(created, side="left")
    window = kl.iloc[idx: idx + horizon]
    if len(window) < horizon:
        return None
    _atr = float(atr) if (atr and float(atr) > 0) else None
    for i in range(1, len(window)):
        _ph, _pl = float(window.iloc[i-1]["high"]), float(window.iloc[i-1]["low"])
        _ch, _cl = float(window.iloc[i]["high"]), float(window.iloc[i]["low"])
        if _atr and _atr > 0:
            _gap = 2.0 * _atr
            if _cl - _ph > _gap or _pl - _ch > _gap:
                return None
        else:
            if _cl > _ph * 1.002 or _ch < _pl * 0.998:
                return None
    if events:
        _w0 = window.iloc[0]["open_time"]
        _w1 = window.iloc[-1]["open_time"]
        for _ev in events:
            _s, _e, _imp = _ev
            if _imp >= 2 and not (_w1 < _s or _w0 > _e):
                return None
    is_buy = dir_val == 1
    for i, (_, bar) in enumerate(window.iterrows()):
        hi = float(bar["high"]); lo = float(bar["low"])
        if is_buy:
            hit_target = hi >= entry + r_win
            hit_stop = lo <= entry - r_loss
        else:
            hit_target = lo <= entry - r_win
            hit_stop = hi >= entry + r_loss
        if hit_target and hit_stop:
            return None
        if hit_target:
            return 1
        if hit_stop:
            return 0
    return None


# ── 多任务状态标签：数据自动聚类（无人工阈值，B 决策=自动聚类）──
# 用入场后 N 根 K 线的原始结构量聚类成 4 类，再按类中心语义自动映射状态名。
STATE_NAMES = ["TREND", "PULLBACK", "REVERSAL", "RANGE"]
_kmeans_cache: dict = {}


def _state_features(window: pd.DataFrame) -> list[float] | None:
    """取入场后窗口的 3 维原始结构量（无阈值、尺度无关）。"""
    if window.empty:
        return None
    close = window["close"].astype(float)
    high = window["high"].astype(float)
    low = window["low"].astype(float)
    # donchian_q：窗口内 close 在窗口高低通道的分位
    hi = high.max(); lo = low.min()
    rng = (hi - lo)
    dq = 0.5 if rng == 0 else (close.iloc[-1] - lo) / rng
    # dev_z_ema20：窗口末 close 对窗口 EMA20 的 Z 偏离
    ema = close.ewm(span=20, adjust=False).mean()
    std = close.rolling(20).std().replace(0, np.nan)
    dz = 0.0 if std.iloc[-1] != std.iloc[-1] else (close.iloc[-1] - ema.iloc[-1]) / std.iloc[-1]
    # macd_slope3：窗口 MACD 近 3 根斜率
    e12 = close.ewm(span=12, adjust=False).mean()
    e26 = close.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    mstd = macd.rolling(20).std().replace(0, np.nan)
    ms = 0.0 if mstd.iloc[-1] != mstd.iloc[-1] else macd.diff(3).iloc[-1] / mstd.iloc[-1]
    return [float(dq), float(dz), float(ms)]


def build_state_labels(klines_by_sym: dict[str, pd.DataFrame], signals: pd.DataFrame,
                       cfg: dict) -> pd.Series:
    """对 signals 每条自动聚类出 state_label（0..3 → STATE_NAMES）。

    聚类在全部信号的入场窗口结构量上做 KMeans(k=4)，按类中心方向偏置自动语义映射：
      - 平均 dz（偏离）最大且 macd 斜率为正 → TREND（强趋势延伸）
      - dz 中等回归、macd 斜率反向 → PULLBACK（回踩/逆小调）
      - dz 反向最大 → REVERSAL（反转）
      - 其余（低偏离低斜率）→ RANGE（震荡）
    不依赖任何人工阈值（如 RSI>70）。
    """
    from sklearn.cluster import KMeans
    horizon = int(cfg.get("ai.lm.label_horizon_bars", 20))
    feats = []
    valid_idx = []
    for i, (_, r) in enumerate(signals.iterrows()):
        kl = klines_by_sym.get(r["symbol"])
        if kl is None or kl.empty:
            continue
        created = r["created_at"]
        idx = kl["open_time"].searchsorted(created, side="left")
        window = kl.iloc[idx: idx + horizon]
        f = _state_features(window)
        if f is None:
            continue
        feats.append(f)
        valid_idx.append(i)
    if not feats:
        return pd.Series([None] * len(signals), index=signals.index, dtype=object)
    X = np.array(feats)
    km = KMeans(n_clusters=4, n_init=10, random_state=42)
    clusters = km.fit_predict(X)
    # 类中心语义排序：按 (dz_mean 降序, macd_slope 降序) 排 TREND>PULLBACK>REVERSAL，余 RANGE
    centers = km.cluster_centers_  # [dq, dz, ms]
    order = sorted(range(4), key=lambda c: (centers[c][1], centers[c][2]), reverse=True)
    semantic = {}
    for rank, c in enumerate(order):
        semantic[c] = STATE_NAMES[rank] if rank < 3 else "RANGE"
    state_arr = [semantic[clusters[j]] for j in range(len(valid_idx))]
    out = pd.Series([None] * len(signals), index=signals.index, dtype=object)
    for pos, si in enumerate(valid_idx):
        out.iloc[si] = state_arr[pos]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="labels.csv")
    ap.add_argument("--mode", default="HEXP:%",
                    help="signals.signal_mode LIKE 过滤；逗号分隔支持多模式(OR)，"
                         "如 'HEXP:%,live_override'。单模式行为与旧版完全一致(向后兼容)")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--r-win", type=float, default=None, help="覆盖 ai.lm.label_r_win")
    ap.add_argument("--r-loss", type=float, default=None, help="覆盖 ai.lm.label_r_loss")
    ap.add_argument("--horizon", type=int, default=None, help="覆盖 ai.lm.label_horizon_bars")
    ap.add_argument("--ds-calibrate", action="store_true",
                    help="设计文档 1.3：用 DeepSeek 票为样本加权 ds_calib_weight（不改 label）")
    ap.add_argument("--sl-source", choices=["real", "atr_fallback"], default=None,
                    help="【2026-08-28 标签口径统一】R(风险距离)来源：real=仅保留真实 sl_price 的信号"
                         "（标签与入场质量挂钩）；atr_fallback=统一用 atr×倍数（默认，向后兼容）")
    args = ap.parse_args()

    conn = psycopg2.connect(args.db_url)
    try:
        # 【P2-T7b】加载活跃宏观事件窗口（重大消息 bar 剔除用）
        events = []
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_date, importance, flat_before_min, flat_after_min "
                    "FROM hcm_market.event_calendar WHERE is_active = true")
                for _ed, _imp, _fb, _fa in cur.fetchall():
                    if _ed is None:
                        continue
                    _ed = pd.to_datetime(_ed, utc=True)
                    _s = _ed - pd.Timedelta(minutes=_fb or 0)
                    _e = _ed + pd.Timedelta(minutes=_fa or 0)
                    events.append((_s, _e, int(_imp)))
        except Exception as _ee:
            print(f"[warn] event_calendar load failed: {_ee}", file=sys.stderr)
            events = []
        print(f"[events] loaded {len(events)} active macro events for gap/news exclusion",
              file=sys.stderr)
        cfg = load_config(conn)
        # 【2026-09-08 审计修复 P1】标签 R 口径 vs 实盘 SL 口径一致性校验。
        # 标签的 R = atr × ai.lm.label_sl_atr_fallback（默认 2.0）；而实盘 SL 由 MT5 桥
        # 按 close.<session>.trailing_stop_distance × ATR 兜底生成（会话 SL 下限）。
        # 两者一旦漂移（运维调了会话止损却没同步标签倍率），模型学到的"R 触达"就与
        # 真实盈亏不对齐 —— 训练-实盘口径分裂且完全无声（既不报错也不告警）。
        # 此处显式比对：fallback 落在桥会话区间之外即告警（不阻断，仅供观测）。
        try:
            _sl_fb = float(cfg.get("ai.lm.label_sl_atr_fallback", 2.0))
            with conn.cursor() as _cur:
                _cur.execute(
                    "SELECT config_key, current_value FROM hcm_config.metadata "
                    "WHERE config_key LIKE 'close.%trailing_stop_distance'")
                _rows = _cur.fetchall()
            _vals = []
            for _k, _v in _rows:
                try:
                    _vals.append(float(_v))
                except (TypeError, ValueError):
                    continue
            if _vals:
                _lo, _hi = min(_vals), max(_vals)
                if _sl_fb < _lo - 1e-9 or _sl_fb > _hi + 1e-9:
                    print(f"[WARN] 标签 R 口径与实盘 SL 口径不一致："
                          f"label_sl_atr_fallback={_sl_fb} 不在桥会话止损区间 "
                          f"[{_lo}, {_hi}]（close.*.trailing_stop_distance）→ "
                          f"训练标签的 R 与实盘风险距离错位，模型会学到错误的盈亏口径",
                          file=sys.stderr)
                else:
                    print(f"[ok] 标签 R 口径一致：fallback={_sl_fb} ∈ 会话区间 "
                          f"[{_lo}, {_hi}]", file=sys.stderr)
        except Exception as _ce:
            print(f"[warn] SL 口径一致性校验跳过: {_ce}", file=sys.stderr)
        if args.r_win is not None:
            cfg["ai.lm.label_r_win"] = args.r_win
        if args.r_loss is not None:
            cfg["ai.lm.label_r_loss"] = args.r_loss
        if args.horizon is not None:
            cfg["ai.lm.label_horizon_bars"] = args.horizon
        if args.sl_source is not None:
            cfg["ai.lm.label_sl_source"] = args.sl_source
        print(f"[cfg] {cfg}", file=sys.stderr)

        # 【2026-08-31 扩样本】--mode 支持逗号分隔多模式(OR)，单模式向后兼容。
        # 背景：HEXP 信号历史仅约 3 周(864 条)、有效样本 307，不足以稳定训练质量头
        # （AUC 仅 ~0.49，测试集噪声主导）。live_override(1862 条)与 HEXP 同期且
        # indicator_values 口径一致(均含 rsi_14/adx_14/macd/atr_14/h1_*)，可安全并入。
        modes = [m.strip() for m in (args.mode or "").split(",") if m.strip()] or ["HEXP:%"]
        _mode_clause = " OR ".join(["s.signal_mode LIKE %s"] * len(modes))
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.signal_id, s.symbol, s.signal_dir, s.entry_price,
                       s.sl_price, s.created_at,
                       s.indicator_values->>'atr_14' AS atr_14,
                       s.indicator_values->'_collab'->>'ai_sl_mult' AS ai_sl_mult
                FROM hcm_signal.signals s
                WHERE ({_mode_clause})
                  AND s.signal_dir IN ('BUY','SELL')
                  AND s.entry_price IS NOT NULL AND s.entry_price > 0
                ORDER BY s.created_at
                """,
                tuple(modes),
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        signals = pd.DataFrame(rows, columns=cols)
        if signals.empty:
            print("[warn] no matching signals", file=sys.stderr)
            return
        signals["created_at"] = pd.to_datetime(signals["created_at"], utc=True)
        signals["entry_price"] = signals["entry_price"].astype(float)
        signals["sl_price"] = pd.to_numeric(signals["sl_price"], errors="coerce")
        signals["atr_14"] = pd.to_numeric(signals["atr_14"], errors="coerce")
        signals["ai_sl_mult"] = pd.to_numeric(signals["ai_sl_mult"], errors="coerce")

        klines = load_klines(conn, sorted(set(signals["symbol"].tolist())))
        print(f"[klines] symbols={list(klines.keys())}", file=sys.stderr)

        # 【2026-09-02 方向头趋势对齐】加载 H1 已收盘趋势态（仅 dir_trend_align 开启时）。
        # 复用 quality_features.load_klines_multi_tf（enrich 含 adx_14/plus_di/minus_di），
        # 再补 ema60 列供 _period_trend_state 用。
        h1_by_sym = {}
        _align_cfg = str(cfg.get("ai.lm.dir_trend_align", "true")).strip().lower()
        if _align_cfg in ("true", "1", "yes", "on"):
            try:
                import quality_features as _qf
                _mtf = _qf.load_klines_multi_tf(
                    conn, sorted(set(signals["symbol"].tolist())), ["H1"])
                for _sym, _tfmap in _mtf.items():
                    _g = _tfmap.get("H1")
                    if _g is None or _g.empty:
                        continue
                    _g = _g.sort_values("open_time").reset_index(drop=True)
                    _g["ema60"] = _g["close"].astype(float).ewm(span=60, adjust=False).mean()
                    h1_by_sym[_sym] = _g
                print(f"[h1_trend] loaded symbols={list(h1_by_sym.keys())}",
                      file=sys.stderr)
            except Exception as _he:
                print(f"[warn] H1 trend load failed, dir_trend_align disabled: {_he}",
                      file=sys.stderr)
                h1_by_sym = {}

        # 【设计文档 1.3 标签校准】可选：DeepSeek 票近邻匹配，构造样本权重。
        # 规则（不翻 label，只调权重，2026-08-21 增强为 4 象限 + continuity 微调，见下方循环体）：
        #   - DS fake_prob>=0.6 且 label=1(DS看真+价格赢)   → 1.5 强化
        #   - DS fake_prob<=0.4 且 label=1(DS看假+价格赢)   → 0.4 DS误判降权
        #   - DS fake_prob>=0.6 且 label=0(DS看真+价格输)   → 0.4 DS误判降权
        #   - DS fake_prob<=0.4 且 label=0(DS看假+价格输)   → 1.5 强化
        #   - continuity_score>=70 按 label 再微调 ×1.2/×0.8
        #   - 其余 → 1.0（中性）；权重钳制 [0.3, 2.0]
        ds_by_sym = load_ds_output(conn) if args.ds_calibrate else {}
        if args.ds_calibrate:
            print(f"[ds_calibrate] enabled, ds symbols={list(ds_by_sym.keys())}",
                  file=sys.stderr)

        # 多任务状态标签：在全部信号入场窗口上自动聚类（无人工阈值）
        state_series = build_state_labels(klines, signals, cfg)
        print(f"[state] clustered states: "
              f"{state_series.value_counts(dropna=False).to_dict()}", file=sys.stderr)

        results = []
        for i, r in signals.iterrows():
            kl = klines.get(r["symbol"])
            label, reason, R, hit_idx = label_one(r, kl, cfg, events)
            # 【阶段 0·方向头/买点头标签】独立于 hexp 方向，影子输出不破红线。
            # 2026-09-02: h1kl 传入使 dir_label 受 H1 主趋势约束（趋势对齐语义）。
            dir_val = dir_label_one(r, kl, cfg, h1_by_sym.get(r["symbol"]))
            # 【2026-09-03 买入头根因修复】entry_label 必须条件于「意图方向」(signal_dir)，
            # 而非真实方向(dir_label)。旧实现用 dir_label 作 dir_val：
            #   方向头标签用 24 根长视野判定 dir_label，买入头标签用 12 根短视野判定 entry_label；
            #   趋势中"先回踩后延续"使 12 根内常先触反向 −1R → entry_label 与入场即时方向特征
            #   反相关 → 模型学到反向映射 → 测试集 AUC≈0.33(低于随机)。
            #   推理时 ai_entry 是无条件输出、被解释为"该信号方向的好买点"，故标签须对齐 signal_dir。
            #   修复后：entry_label=1 ⟺ 价格先确认「意图方向」±1R，与可学习特征正相关，
            #   且语义与消费端(ai_entry=好买点 for signal_dir)一致。
            _intended = 1 if r["signal_dir"] == "BUY" else (-1 if r["signal_dir"] == "SELL" else 0)
            entry_lbl = entry_label_one(r, kl, cfg, _intended, events)
            # 设计文档 1.3：DeepSeek 视角样本权重（默认 1.0，仅 --ds-calibrate 时计算）
            # 【2026-08-21 增强】原规则只加权"DS看真+价格赢"(1.5)与"DS看假+价格输"(0.5)，
            # 且 label==0 且 fp<0.4 因 DS 实测 fp≥0.42 永不触发 → DS 高置信但价格输的
            # 40 个负样本被浪费。现补全 4 象限(对齐/背离) + continuity 强度微调：
            #   label=1 且 fp>=0.6 (DS看真+价格赢)   → 1.5 强化
            #   label=1 且 fp<=0.4 (DS看假+价格赢)   → 0.4 DS误判降权
            #   label=0 且 fp>=0.6 (DS看真+价格输)   → 0.4 DS误判降权(高价值负样本)
            #   label=0 且 fp<=0.4 (DS看假+价格输)   → 1.5 强化
            #   continuity_score 高(≥70) 且 label=1  → 再×1.2(趋势延续强)
            #   continuity_score 高(≥70) 且 label=0  → 再×0.8(DS强延续但价格输=存疑)
            # 权重钳制到 [0.3, 2.0] 防极端。
            ds_w = 1.0
            if args.ds_calibrate:
                _ds = _nearest_ds(ds_by_sym.get(r["symbol"], []), r["created_at"])
                if _ds is not None:
                    _fp = _ds[1]
                    _cont = _ds[3] if len(_ds) > 3 else 0.0
                    if label == 1:
                        if _fp >= 0.6:
                            ds_w = 1.5
                        elif _fp <= 0.4:
                            ds_w = 0.4
                    else:  # label == 0
                        if _fp >= 0.6:
                            ds_w = 0.4
                        elif _fp <= 0.4:
                            ds_w = 1.5
                    # continuity 强度微调
                    if _cont is not None:
                        try:
                            if float(_cont) >= 70.0:
                                ds_w = ds_w * 1.2 if label == 1 else ds_w * 0.8
                        except (TypeError, ValueError):
                            pass
                    ds_w = max(0.3, min(2.0, ds_w))
            results.append({
                "signal_id": r["signal_id"], "symbol": r["symbol"],
                "signal_dir": r["signal_dir"], "entry_price": r["entry_price"],
                "created_at": r["created_at"].isoformat(), "R": round(R, 6),
                "label": label, "reason": reason, "hit_bar_idx": hit_idx,
                "state_label": state_series.iloc[i],
                "dir_label": dir_val, "entry_label": entry_lbl,
                "ds_calib_weight": ds_w,
            })

        df = pd.DataFrame(results)
        _pre = len(df)
        # 【P2-T9】样本去重：同 (symbol,dir,entry,R,state) 簇超 8 条随机降采样至 8，
        # 防止模型死记高度重复历史片段（规范一）。
        if not df.empty:
            _dup_key = df.apply(
                lambda r: (r["symbol"], r["signal_dir"], round(float(r["entry_price"]), 1),
                           round(float(r["R"]), 1), r.get("state_label")), axis=1)
            df = df.assign(_dup_key=_dup_key)
            _kept = []
            _rng = np.random.default_rng(42)
            for _, _g in df.groupby("_dup_key"):
                if len(_g) > 8:
                    _kept.append(_g.iloc[_rng.choice(len(_g), size=8, replace=False)])
                else:
                    _kept.append(_g)
            df = pd.concat(_kept, ignore_index=True).drop(columns=["_dup_key"])
            print(f"[dedup] {_pre} -> {len(df)} (dropped {_pre - len(df)})", file=sys.stderr)
        df.to_csv(args.out, index=False)
        labeled = df.dropna(subset=["label"])
        print(f"[done] total={len(df)} labeled={len(labeled)} "
              f"win={int((labeled['label']==1).sum())} loss={int((labeled['label']==0).sum())} "
              f"excluded={int(df['label'].isna().sum())}")
        print(f"[excluded_reasons] {df[df['label'].isna()]['reason'].value_counts().to_dict()}")
        print(f"[out] {args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
