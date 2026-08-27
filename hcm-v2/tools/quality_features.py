#!/usr/bin/env python3
"""quality_features.py — 阶段 0 特征装配器（只读，零实盘影响）。

为每条信号装配训练特征，分三类：
  A. 已持久化特征（引擎落库，精确）：
       pre_score / regime / position_in_range / confidence / lot /
       adx_14 / rsi_14 / macd / atr_14 / h1_adx / h1_regime /
       h1_trend_direction / h1_trend_strength / ai_sl_mult / suggested_lot_ratio
  B. 从 M5 K 线重算（入场时刻已收盘 bar，标准公式）：
       plus_di / minus_di / er / bbw / bbw_pct / hurst / mm /
       ema20_dist_atr / body_ratio / pullback_depth / atr_pct
  C. 环境特征：
       session 亚/欧/美 one-hot / spread / spread_atr /
       event_proximity_min / macro_risk_score / sentiment_risk_score

缺失的 HEXP 专属特征（hp_score/hp_strength/dir_sum/k/verdict/trend_phase/多周期趋势）
当前引擎未落库，本脚本以 NaN 占位并在列尾 `missing_hexp_features` 元数据中显式标注。
→ 补齐需引擎在 hexp 路径把 factor_raws 持久化到 signals.indicator_values（红线改动，另行确认）。

用法:
  DB_URL=postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2 \
    python quality_features.py --out features.csv --mode 'HEXP:%'
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"

# 缺省 EMA/布林/Hurst 参数（后续并入 ai.lm.feature.* 配置，禁硬编码）
EMA_FAST = 20
BB_PERIOD = 20
BB_STD = 2.0
BBW_PCT_WINDOW = 120
ER_PERIOD = 10
HURST_WINDOW = 30
ATR_WINDOW = 14
ATR_PCT_WINDOW = 120
DI_PERIOD = 14
MM_PERIOD = 5

# HEXP 专属特征（引擎未落库，NaN 占位）
MISSING_HEXP = ["hp_score", "hp_strength", "dir_sum", "k", "verdict"]


def load_signals(conn, mode: str) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.signal_id, s.symbol, s.signal_dir, s.entry_price, s.sl_price,
                   s.created_at, s.regime, s.pre_score, s.position_in_range,
                   s.confidence, s.lot, s.indicator_values
            FROM hcm_signal.signals s
            WHERE s.signal_mode LIKE %s
              AND s.signal_dir IN ('BUY','SELL')
              AND s.entry_price IS NOT NULL AND s.entry_price > 0
              -- 【2026-08-25 训练集时间窗对齐】仅保留能与 DeepSeek 落库票
              -- (hcm_ai.ds_output) 在 ±1800s 同 symbol 近邻匹配上的信号，
              -- 窗口与下游 _nearest_ds() 严格一致（通过 EXISTS ⟺ 三特征非 0）。
              -- 根治：此前训练窗(8/10 起)远早于 ds_output 落库起点(8/14)，
              -- 且 8/21·8/22 断天，致约83pct样本三特征全0 → 模型无从学习
              -- DeepSeek 语义(ds_nonzero_ratio≈0.16)。对齐后仅训练"有 DS 上下文"
              -- 的样本，ds_nonzero_ratio→~1.0，模型开始真正吸收 DS 语义。
              AND EXISTS (
                SELECT 1 FROM hcm_ai.ds_output d
                WHERE d.symbol = s.symbol
                  AND abs(extract(epoch from (s.created_at - d.created_at))) <= 1800
              )
            ORDER BY s.created_at
            """,
            (mode,),
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    df["entry_price"] = pd.to_numeric(df["entry_price"], errors="coerce")
    df["sl_price"] = pd.to_numeric(df["sl_price"], errors="coerce")
    df["pre_score"] = pd.to_numeric(df["pre_score"], errors="coerce")
    df["position_in_range"] = pd.to_numeric(df["position_in_range"], errors="coerce")
    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    df["lot"] = pd.to_numeric(df["lot"], errors="coerce")

    # 展开 indicator_values JSONB → 独立列
    def _extract(iv):
        if iv is None:
            return {}
        return iv if isinstance(iv, dict) else {}

    ind = df["indicator_values"].apply(_extract)
    for k in ["adx_14", "rsi_14", "macd", "atr_14", "h1_adx",
              "h1_regime", "h1_trend_direction", "h1_trend_strength"]:
        df[k] = ind.apply(lambda d, _k=k: d.get(_k))
    df["ai_sl_mult"] = ind.apply(lambda d: (d.get("_collab") or {}).get("ai_sl_mult"))
    df["suggested_lot_ratio"] = ind.apply(lambda d: (d.get("_collab") or {}).get("suggested_lot_ratio"))
    for c in ["adx_14", "rsi_14", "macd", "atr_14", "h1_adx", "h1_trend_strength",
              "ai_sl_mult", "suggested_lot_ratio"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def enrich_klines(kl: pd.DataFrame) -> pd.DataFrame:
    """在 M5 序列上向量化重算技术指标（入场时刻用已收盘 bar 的值）。"""
    kl = kl.sort_values("open_time").reset_index(drop=True)
    close = kl["close"].astype(float)
    high = kl["high"].astype(float)
    low = kl["low"].astype(float)
    opn = kl["open"].astype(float) if "open" in kl else close.shift(1).fillna(close)
    n = len(kl)

    # ── ATR(14) Wilder ──
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / ATR_WINDOW, adjust=False).mean()

    # ── +DI / -DI (Wilder) ──
    up = high.diff()
    dn = -low.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=kl.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=kl.index)
    atr_sm = tr.ewm(alpha=1 / DI_PERIOD, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / DI_PERIOD, adjust=False).mean() / atr_sm.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / DI_PERIOD, adjust=False).mean() / atr_sm.replace(0, np.nan)

    # ── ER 效率比 ──
    er = (close.diff(ER_PERIOD).abs() /
          close.diff().abs().rolling(ER_PERIOD).sum().replace(0, np.nan))

    # ── 布林带宽 BBW + 分位 ──
    mid = close.rolling(BB_PERIOD).mean()
    sd = close.rolling(BB_PERIOD).std()
    bbw = (2 * BB_STD * sd) / mid.replace(0, np.nan)
    bbw_pct = bbw.rolling(BBW_PCT_WINDOW).apply(
        lambda x: (x[-1] <= x).mean() * 100.0, raw=True)

    # ── Hurst (R/S 近似) ──
    def _hurst(x):
        if len(x) < HURST_WINDOW or np.std(x) == 0:
            return np.nan
        seg = x[-HURST_WINDOW:]
        mean = seg.mean()
        dev = (seg - mean).cumsum()
        r = dev.max() - dev.min()
        s = seg.std()
        return np.log(r / s) / np.log(len(seg))

    hurst = close.rolling(HURST_WINDOW).apply(_hurst, raw=True)

    # ── 微结构动量 mm（tanh 归一，近似）──
    mm = np.tanh(close.pct_change(MM_PERIOD) / (atr / close).replace(0, np.nan))

    # ── 入场微观 ──
    ema_fast = close.ewm(span=EMA_FAST, adjust=False).mean()
    ema20_dist_atr = (close - ema_fast).abs() / atr.replace(0, np.nan)
    rng = (high - low).replace(0, np.nan)
    body_ratio = (close - opn).abs() / rng
    roll_high = high.rolling(MM_PERIOD * 4).max()
    roll_low = low.rolling(MM_PERIOD * 4).min()
    pullback_depth = (roll_high - close) / atr.replace(0, np.nan)  # 距近期高点回踩深度
    atr_pct = atr.rolling(ATR_PCT_WINDOW).apply(
        lambda x: (x[-1] <= x).mean() * 100.0, raw=True)

    # ── RSI(14)（训练侧 m5 对齐分支用，推理侧从 factor_raws 取 rsi 同源）──
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi_14 = 100.0 - 100.0 / (1.0 + (gain / loss.replace(0, float("nan"))))
    kl = kl.assign(
        atr=atr, plus_di=plus_di, minus_di=minus_di, er=er,
        bbw=bbw, bbw_pct=bbw_pct, hurst=hurst, mm=mm,
        rsi_14=rsi_14, ema20=ema_fast,
        ema20_dist_atr=ema20_dist_atr, body_ratio=body_ratio,
        pullback_depth=pullback_depth, atr_pct=atr_pct,
        spread_num=pd.to_numeric(kl.get("spread"), errors="coerce"),
    )
    kl["spread_atr"] = kl["spread_num"] / kl["atr"].replace(0, np.nan)
    # ── 多任务状态头：无阈值原始结构因子（让模型自己找边界，不喂人工判定）──
    kl = _add_structure_factors(kl)
    return kl


def _add_structure_factors(kl: pd.DataFrame) -> pd.DataFrame:
    """无阈值原始结构因子：donchian_q / dev_z_multi×3 / macd_slope3 / body_wick_ratio。

    设计原则（铁律五·量化红线）：禁止未来数据泄露——所有量均用 shift(1) 后的历史值计算，
    不引用当前未收盘棒的未来信息。全部为 ∈[0,1] 或带符号的原始量，边界由模型自学习。
    """
    close = kl["close"].astype(float)
    high = kl["high"].astype(float)
    low = kl["low"].astype(float)
    opn = kl["open"].astype(float) if "open" in kl else close.shift(1).fillna(close)
    atr = kl["atr"]

    # 1) donchian_q：close 在 60 根通道的分位（∈[0,1]，原始无边界）
    hi60 = high.rolling(60).max()
    lo60 = low.rolling(60).min()
    rng = (hi60 - lo60).replace(0, np.nan)
    kl["donchian_q"] = ((close - lo60) / rng).clip(0, 1).fillna(0.5)

    # 2) dev_z_multi：close 对 EMA20/60/200 的多周期 Z 偏离（向量，三列）
    for n, col in [(20, "dev_z_ema20"), (60, "dev_z_ema60"), (200, "dev_z_ema200")]:
        ema = close.ewm(span=n, adjust=False).mean()
        std = close.rolling(n).std().replace(0, np.nan)
        kl[col] = ((close - ema) / std).fillna(0.0)

    # 3) macd_slope3：近 3 根 MACD 柱状斜率（量能衰减，原始带符号）
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_std = macd.rolling(20).std().replace(0, np.nan)
    kl["macd_slope3"] = (macd.diff(3) / macd_std).fillna(0.0)

    # 4) body_wick_ratio：影线/实体比（高位滞涨的长上影=接刀前兆，原始 ≥0）
    body = (close - opn).abs()
    wick = (high - low) - body
    kl["body_wick_ratio"] = (wick / body.replace(0, np.nan)).clip(lower=0).fillna(0.0)

    # 5) extreme_reversal：顶/底极值反转信号（-1/0/+1），与 hexp 极值反转护栏同源但
    #    纯可重算（不依赖引擎内部 M1 微动量 f_mm）。用于把"顶部动量减弱+长上影/底部
    #    反转+长下影"规则喂给 LightGBM。阈值固定（与 hexp.extreme 默认对齐），不热调，
    #    避免训练-推理分布偏移。无未来数据泄露（全部用 shift(1) 后历史值）。
    _hi_pct = 0.85   # 对齐 hexp.extreme.high_pct 默认
    _lo_pct = 0.15   # 对齐 hexp.extreme.low_pct 默认
    _wick_min = 0.50  # 对齐 hexp.extreme.wick_min 默认(训练侧略放宽, 用 body_wick_ratio 代用)
    _dq = kl["donchian_q"]
    _mom3 = close.diff(3)  # 近 3 根收盘动量（>0 上行 / <0 回落）
    _rev = pd.Series(0.0, index=kl.index)
    # 顶反转：高位 + 上影偏长(影线>实体即 body_wick_ratio>0.5) + 动量转负
    _top_mask = (_dq >= _hi_pct) & (kl["body_wick_ratio"] >= _wick_min) & (_mom3 < 0)
    # 底反转：低位 + 下影偏长 + 动量转正
    _bot_mask = (_dq <= _lo_pct) & (kl["body_wick_ratio"] >= _wick_min) & (_mom3 > 0)
    _rev = _rev.mask(_top_mask, 1.0).mask(_bot_mask, -1.0)
    kl["extreme_reversal"] = _rev.fillna(0.0)

    # 全部用历史值（防止状态头在训练时偷看当前棒），训练/推理一致
    struct_cols = ["donchian_q", "dev_z_ema20", "dev_z_ema60", "dev_z_ema200",
                   "macd_slope3", "body_wick_ratio", "extreme_reversal"]
    kl[struct_cols] = kl[struct_cols].shift(1)
    return kl


def session_onehot(ts: pd.Timestamp) -> dict:
    """UTC 小时 → 亚/欧/美 one-hot（0=亚 08:00 前，1=欧 08-16，2=美 16-24）。"""
    h = ts.hour
    if 8 <= h < 16:
        return {"session_asia": 0, "session_eu": 1, "session_us": 0}
    if 16 <= h < 24:
        return {"session_asia": 0, "session_eu": 0, "session_us": 1}
    return {"session_asia": 1, "session_eu": 0, "session_us": 0}


def load_ds_output(conn) -> dict:
    """加载 DeepSeek 异步票落库表 hcm_ai.ds_output，按 (symbol, ts) 索引近邻查询。

    【DeepSeek 训练特征增强 2026-08-17】与推理侧 build_features 读 Redis ai:ds:out
    同源：推理用实时票，训练用落库票（同一 run_loop 写入），保证训练-推理同分布。
    历史缺失(旧数据无 DeepSeek 标注)→ 该 signal 三特征全 0.0，与推理侧缺省一致。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, created_at, fake_prob, ai_sl_coeff, continuity_score "
            "FROM hcm_ai.ds_output ORDER BY created_at",
        )
        rows = cur.fetchall()
    out = {}
    for sym, ts, fp, sl, cont in rows:
        out.setdefault(sym, []).append(
            (ts, fp if fp is not None else 0.0,
             sl if sl is not None else 0.0, cont if cont is not None else 0.0)
        )
    return out


def _as_naive_utc(ts):
    """统一转 UTC naive，消除时区戳差异导致的邻近匹配漏判。"""
    if ts is None:
        return None
    try:
        ts = pd.to_datetime(ts, utc=True)
    except Exception:
        return None
    return ts.tz_convert("UTC").tz_localize(None)


def _nearest_ds(ds_list, ts, window_sec: int = 1800):
    """返回 ts 时间最近一条 ds_output 的三元组（双向最近邻，±window_sec）。

    【2026-08-19 阶段0修正】DeepSeek 异步票(run_loop 消费 trigger)与信号生产是
    分钟级邻近，原 window_sec=86400（±1天）过宽→会把多空不同/过期票误注入特征，
    对小样本噪声放大严重。收紧到默认 1800s（±30分钟双向）精确匹配，同时比较前
    统一转 UTC naive 防时区戳错位漏匹配（与推理侧 build_features 同口径）。
    超出窗口→返回全 0.0（与推理侧缺省严格一致），绝不注入过期票。
    """
    if not ds_list:
        return 0.0, 0.0, 0.0
    ts_n = _as_naive_utc(ts)
    if ts_n is None:
        return 0.0, 0.0, 0.0
    best = None
    best_dt = None
    for (t, fp, sl, cont) in ds_list:
        t_n = _as_naive_utc(t)
        if t_n is None:
            continue
        dt = abs((ts_n - t_n).total_seconds())
        if dt <= window_sec and (best is None or dt < best_dt):
            best = (fp, sl, cont)
            best_dt = dt
    return best if best is not None else (0.0, 0.0, 0.0)


def load_env(conn, symbols: list[str]) -> tuple[pd.DataFrame, dict, dict]:
    """加载事件日历 + 最近宏观/情绪快照。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT category, event_date, importance FROM hcm_market.event_calendar "
            "WHERE is_active AND importance >= 2 ORDER BY event_date",
        )
        events = cur.fetchall()
        cur.execute(
            "SELECT category, macro_risk_score, sentiment_risk_score, snapshot_time FROM "
            "(SELECT category, macro_risk_score, NULL::int AS sentiment_risk_score, snapshot_time "
            " FROM hcm_market.macro_snapshots UNION ALL "
            " SELECT category, NULL::int, sentiment_risk_score, snapshot_time "
            " FROM hcm_market.sentiment_snapshots) t ORDER BY snapshot_time",
        )
        snaps = cur.fetchall()
    ev = pd.DataFrame(events, columns=["category", "event_date", "importance"])
    if not ev.empty:
        ev["event_date"] = pd.to_datetime(ev["event_date"], utc=True)
    snap_df = pd.DataFrame(snaps, columns=["category", "macro_risk_score", "sentiment_risk_score", "snapshot_time"])
    if not snap_df.empty:
        snap_df["snapshot_time"] = pd.to_datetime(snap_df["snapshot_time"], utc=True)
    # 每 category 最新宏观/情绪快照索引（在下方按时间就近取）
    return ev, snap_df, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="features.csv")
    ap.add_argument("--mode", default="HEXP:%")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--period-align", default="none", choices=["none", "m5"],
                    help="none=legacy(读 indicator_values 的 H1 h1_adx/h1_trend_strength); "
                         "m5=由 M5 K线同源重算这两个特征(与 quality_scorer.py align_m5 "
                         "推理口径一致)，用于 M5 周期对齐重训。默认 none 零影响。")
    args = ap.parse_args()
    period_align = args.period_align

    conn = psycopg2.connect(args.db_url)
    try:
        signals = load_signals(conn, args.mode)
        if signals.empty:
            print("[warn] no signals", file=sys.stderr)
            return

        events, snaps, _ = load_env(conn, sorted(set(signals["symbol"].tolist())))
        # 【DeepSeek 训练特征增强 2026-08-17】加载 DeepSeek 落库票，与推理侧同构
        ds_by_sym = load_ds_output(conn)

        # 加载并重算各 symbol M5 指标
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, open_time, open, high, low, close, spread "
                "FROM hcm_market.klines WHERE symbol = ANY(%s) AND time_frame='M5' ORDER BY open_time",
                (sorted(set(signals["symbol"].tolist())),),
            )
            kcols = [d[0] for d in cur.description]
            krows = cur.fetchall()
        kdf = pd.DataFrame(krows, columns=kcols)
        kdf["open_time"] = pd.to_datetime(kdf["open_time"], utc=True)
        # 训练侧 K线也经过 _add_structure_factors，与推理 build_features 完全同源
        # （donchian_q/dev_z_ema20-60-200/macd_slope3/body_wick_ratio 等结构因子列
        # 推理侧在 build_features 内计算，训练侧必须同步，否则推理独有列被忽略 → 根因 B）。
        klines_by_sym = {s: _add_structure_factors(enrich_klines(g)) for s, g in kdf.groupby("symbol")}

        # 【A+B 同源对齐 2026-08-17】训练特征集严格对齐推理侧 build_features 输出列。
        # 推理时不可得的特征(pre_score/regime/position_in_range/confidence/lot/
        # h1_regime/h1_trend_direction/ai_sl_mult/suggested_lot_ratio/MISSING_HEXP)一律剔除，
        # 避免训练-推理列错配(此前推理时 9 列恒 0、11 个推理增强列被忽略 → 根因 B)。
        # period_align=m5 时 adx_14/rsi_14/macd/atr_14 也改由 M5 K线同源重算，
        # 与推理侧 build_features(align_m5=True) 完全同公式。
        feats = []
        for _, r in signals.iterrows():
            row = {"signal_id": r["signal_id"], "symbol": r["symbol"], "signal_dir": r["signal_dir"]}
            kl = klines_by_sym.get(r["symbol"])
            _bar = None
            if kl is not None and not kl.empty:
                idx = kl["open_time"].searchsorted(r["created_at"], side="right") - 1
                idx = max(0, min(idx, len(kl) - 1))
                _bar = kl.iloc[idx]
            if period_align == "m5" and _bar is not None:
                # M5 同源重算全部指标因子（与推理 build_features 同口径）
                _pdi = float(_bar["plus_di"]); _mdi = float(_bar["minus_di"])
                _dx = 100.0 * abs(_pdi - _mdi) / (_pdi + _mdi) if (_pdi + _mdi) else float("nan")
                _adx = _dx  # 单 bar 近似（推理侧用 ewm 序列末值，训练侧用单 bar 对齐）
                row["adx_14"] = _adx
                row["rsi_14"] = float(_bar.get("rsi_14", r["rsi_14"])) if "rsi_14" in _bar else r["rsi_14"]
                _cl = kl["close"].astype(float)
                _macd_line = _cl.ewm(span=12, adjust=False).mean() - _cl.ewm(span=26, adjust=False).mean()
                _sig9 = _macd_line.ewm(span=9, adjust=False).mean()
                row["macd"] = float((_macd_line - _sig9).iloc[idx])
                row["atr_14"] = float(_bar["atr"])
                # h1_adx/h1_trend_strength 由 M5 同源 ADX 序列重算
                _pdi_s = kl["plus_di"].astype(float); _mdi_s = kl["minus_di"].astype(float)
                _dx_s = 100.0 * (_pdi_s - _mdi_s).abs() / (_pdi_s + _mdi_s).replace(0, float("nan"))
                _adx_s = _dx_s.ewm(alpha=1 / 14, adjust=False).mean().iloc[idx]
                if not pd.isna(_adx_s):
                    row["h1_adx"] = float(_adx_s)
                    row["h1_trend_strength"] = float(_adx_s) / 100.0
                else:
                    row["h1_adx"] = r["h1_adx"]; row["h1_trend_strength"] = r["h1_trend_strength"]
            else:
                # 非对齐模式：从 indicator_values 取历史值（旧口径，仅兜底）
                row["adx_14"] = r["adx_14"]; row["rsi_14"] = r["rsi_14"]
                row["macd"] = r["macd"]; row["atr_14"] = r["atr_14"]
                row["h1_adx"] = r["h1_adx"]; row["h1_trend_strength"] = r["h1_trend_strength"]
            if _bar is not None:
                # 【回归修复 2026-08-17】_cl 原仅在 period_align=='m5' 块内定义，
                # 但下方增强特征块(di_ratio/close_mom_atr 等)无条件使用 →
                # 非 m5 对齐模式崩溃 UnboundLocalError。统一在此按 kl 定义，缺则 [0.0]。
                _cl = kl["close"].astype(float) if (kl is not None and not kl.empty) else pd.Series([0.0])
                for c in ["plus_di", "minus_di", "er", "bbw", "bbw_pct", "hurst", "mm",
                          "ema20_dist_atr", "body_ratio", "pullback_depth", "atr_pct",
                          "spread_num", "spread_atr",
                          "donchian_q", "dev_z_ema20", "dev_z_ema60", "dev_z_ema200",
                          "macd_slope3", "body_wick_ratio", "extreme_reversal"]:
                    row[c] = _bar[c] if c in _bar else np.nan
                # 推理侧增强特征（与 build_features 同源）
                _atr = float(_bar.get("atr") or 0.0) or 1e-9
                _pdi = float(_bar.get("plus_di") or 0.0); _mdi = float(_bar.get("minus_di") or 0.0)
                row["di_ratio"] = _pdi / (_mdi + 1e-6)
                row["di_net"] = (_pdi - _mdi) / (_pdi + _mdi + 1e-6)
                row["spread_atr_log"] = float(np.log1p(max(0.0, _bar.get("spread_atr") or 0.0)))
                _mom = (float(_cl.iloc[idx]) - float(_cl.iloc[max(0, idx - 6)])) if len(_cl) > 6 else 0.0
                row["close_mom_atr"] = _mom / _atr
                row["trend_aligned"] = 1.0 if float(_cl.iloc[idx]) >= float(_bar.get("ema20", _cl.iloc[idx])) else 0.0
            # 环境
            row.update(session_onehot(r["created_at"]))
            row["event_proximity_min"] = None
            if not events.empty:
                ev_cat = events[events["event_date"] > r["created_at"]]
                if not ev_cat.empty:
                    row["event_proximity_min"] = (ev_cat.iloc[0]["event_date"] - r["created_at"]).total_seconds() / 60.0
            row["macro_risk_score"] = None
            row["sentiment_risk_score"] = None
            if not snaps.empty:
                # 外部因子按 category 分列存储；XAUUSD 信号对应 metals 类别（含真实 K线派生变分）。
                # 与推理侧 build_features 同口径：仅取 metals 类别的最近快照，
                # 避免被恒值的 forex/crypto/liquidity 类别淹没为零方差。
                # 若该信号早于所有 metals 快照（采集起点晚于信号时间），回退到 metals 全局最新一条，
                # 避免缺口样本落入 NaN 后被 train 的 fillna(median) 抹平。
                _metals = snaps[snaps["category"] == "metals"]
                if not _metals.empty:
                    m = _metals[(_metals["snapshot_time"] <= r["created_at"]) & _metals["macro_risk_score"].notna()]
                    if not m.empty:
                        row["macro_risk_score"] = m.iloc[-1]["macro_risk_score"]
                    else:
                        _fb = _metals[_metals["macro_risk_score"].notna()].sort_values("snapshot_time")
                        if not _fb.empty:
                            row["macro_risk_score"] = _fb.iloc[-1]["macro_risk_score"]
                    s2 = _metals[(_metals["snapshot_time"] <= r["created_at"]) & _metals["sentiment_risk_score"].notna()]
                    if not s2.empty:
                        row["sentiment_risk_score"] = s2.iloc[-1]["sentiment_risk_score"]
                    else:
                        _fb2 = _metals[_metals["sentiment_risk_score"].notna()].sort_values("snapshot_time")
                        if not _fb2.empty:
                            row["sentiment_risk_score"] = _fb2.iloc[-1]["sentiment_risk_score"]
            # 【DeepSeek 训练特征增强 2026-08-17】注入 ds_fake_prob/ds_sl_coeff/ds_continuity
            # 三特征，与推理侧 build_features 缺省(0.0)严格一致；按 signal 时刻近邻匹配落库票。
            _fp, _sl, _cont = _nearest_ds(ds_by_sym.get(r["symbol"], []), r["created_at"])
            row["ds_fake_prob"] = _fp
            row["ds_sl_coeff"] = _sl
            row["ds_continuity"] = _cont
            feats.append(row)

        df = pd.DataFrame(feats)
        df.to_csv(args.out, index=False)
        print(f"[done] features rows={len(df)} cols={len(df.columns)}")
        print(f"[missing_hexp_features] {MISSING_HEXP} (NaN 占位，待引擎持久化)")
        print(f"[out] {args.out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
