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

缺失的 HEXP 专属特征（hp_score/hp_strength/dir_sum/k/trend_phase/多周期趋势）
当前引擎未落库，本脚本以 NaN 占位并在列尾 `missing_hexp_features` 元数据中显式标注。
→ 补齐需引擎在 hexp 路径把 factor_raws 持久化到 signals.indicator_values（红线改动，另行确认）。
【2026-09-02】verdict 已不再缺失：训练侧由 _compute_verdict 从多周期 K 线重算、推理侧由
build_features 读 hexp 实时 snap["verdict"]，故已从 MISSING_HEXP 占位名单移除并纳入 MODEL_FEATURE_COLS。

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

# 【TimesFM 特征 2026-08-30】契约单一真值：tmf 列名与训练/推理侧共享
from _model_feature_cols import TMF_FEATURE_COLS  # noqa: E402

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

# HEXP 专属特征（引擎未落库，NaN 占位）。【2026-09-02】verdict 已不再缺失：
# 训练侧由 _compute_verdict 从多周期 K 线重算填入，推理侧由 build_features 读 hexp 实时
# snap["verdict"]，故从占位名单移除（否则会被误当 NaN 占位）。
MISSING_HEXP = ["hp_score", "hp_strength", "dir_sum", "k"]


def load_signals(conn, mode: str) -> pd.DataFrame:
    # 【2026-08-31 扩样本】mode 支持逗号分隔多模式(OR)，单模式行为完全不变(向后兼容)。
    # 与 build_labels.py 同批改造：HEXP 信号历史仅约 3 周、有效样本不足，
    # 并入同期的 live_override（indicator_values 口径已验证一致）以扩充训练样本。
    modes = [m.strip() for m in (mode or "").split(",") if m.strip()] or ["HEXP:%"]
    _mode_clause = " OR ".join(["s.signal_mode LIKE %s"] * len(modes))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.signal_id, s.symbol, s.signal_dir, s.entry_price, s.sl_price,
                   s.created_at, s.regime, s.pre_score, s.position_in_range,
                   s.confidence, s.lot, s.indicator_values
            FROM hcm_signal.signals s
            WHERE ({_mode_clause})
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
            tuple(modes),
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
    # 【2026-09-08 审计修复 P1】verdict 取引擎落库真值（供训练优先采用）：
    # 推理侧 build_features 读的是 hexp **实时** snap["verdict"]，由带迟滞的状态机产出
    # （exit_score=40 + confirm_enter 连续确认）；而本脚本 _compute_verdict 是**无状态
    # 快照近似**（MTF_EXIT_TS=35、无迟滞）→ 同一时刻两侧值系统性不同，训练分布≠线上
    # 分布（模型学到线上不存在的 verdict）。引擎落库值与推理同源，优先采用。
    df["verdict_engine"] = ind.apply(
        lambda d: (d.get("_hexp") or {}).get("verdict", d.get("verdict")))
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
    # ADX(14)：DX 的 Wilder 平滑（与推理侧 _h1_features / align_m5 同口径）。
    # 供多周期 verdict 重算(_compute_verdict)取用；M5 路径仍优先用 indicator_values 的
    # adx_14（或 period_align=m5 时同源重算），此处补齐不影响 M5 既有口径。
    _dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _dx.ewm(alpha=1 / DI_PERIOD, adjust=False).mean()

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
        bbw=bbw, bbw_pct=bbw_pct, hurst=hurst, mm=mm, adx_14=adx,
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


def load_tmf_features(conn, version: str = "tfm25_pca_v1_sig") -> dict:
    """加载 TimesFM 离线特征表 hcm_ai.timesfm_features，按 (symbol, bar_time) 精确索引。

    【TimesFM 特征 2026-08-30】与推理侧 quality_scorer.build_features 读同一张表、同口径。
    特征严格按 bar open_time 对齐(scheduler 用 --at-signal-times 抽取，bar_time 即信号所在
    M5 bar 的 open_time)，训练侧 _bar['open_time'] 同义 → 精确 join。缺失(调度未覆盖的日期/
    非 XAUUSD)→ 该 signal 全 0.0，与推理侧缺省一致，保证训练-推理同分布。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, bar_time, "
            "tmf_pc00,tmf_pc01,tmf_pc02,tmf_pc03,tmf_pc04,tmf_pc05,tmf_pc06,tmf_pc07,"
            "tmf_trend_cont,tmf_rev_prob,tmf_vol_cycle,tmf_mtf_resonance,tmf_hist_sim "
            "FROM hcm_ai.timesfm_features "
            "WHERE time_frame='M5' AND tmf_version=%s ORDER BY bar_time",
            (version,),
        )
        rows = cur.fetchall()
    out = {}
    for r in rows:
        sym = r[0]
        bt = _as_naive_utc(r[1])
        if bt is None:
            continue
        vals = {}
        for c, v in zip(TMF_FEATURE_COLS, r[2:]):
            vals[c] = float(v) if v is not None else 0.0
        out.setdefault(sym, {})[bt] = vals
    return out


# ── 【2026-09-02】多周期 MTF 共识分 verdict 重算（与 hexp_engine 同构近似）──
# 目的：把 hexp 实时算的 M30/H1/H4/D1 加权共识分 verdict 作为方向头特征，让 dir_head
# 拿到"多周期共振方向"这一最强方向信号。训练侧历史无 verdict 落库（仅 HEXP 信号有、
# live_override 无）→ 必须从多周期 K 线重算，保证 HEXP+live_override 全覆盖。
# 关键：仅用截至信号时刻的"已收盘 bar"计算，杜绝未来数据泄露（与推理侧 hexp 口径一致）。
# 近似说明：hexp 用带连续性的迟滞状态机(_HysteresisState)判各周期趋势态，本函数用"截至
# 时刻的窗口快照"等效判定（无跨 bar 状态），作为训练特征近似合理、且确定性可复现。
MTF_WEIGHTS = {"M30": 0.15, "H1": 0.25, "H4": 0.35, "D1": 0.25}
MTF_PERIODS = ["M30", "H1", "H4", "D1"]
MTF_ENTER_TS = 60.0   # 对齐 hexp.state.enter_score 默认
MTF_EXIT_TS = 35.0    # 对齐 hexp.state.exit_score 默认
MTF_MIN_PERIODS = 2.0  # 对齐 hexp.resonance.min_periods 默认


def load_klines_multi_tf(conn, symbols, tfs):
    """加载并 enrich 多周期 K 线（M30/H1/H4/D1），供 _compute_verdict 重算 verdict。

    与 M5 管线同构（enrich_klines + _add_structure_factors 算出 adx_14/plus_di/minus_di
    等），按 (symbol, tf) 存于嵌套 dict。时间窗取全量（重算时按 created_at 截断到已收盘 bar）。
    """
    out: dict = {}
    _syms = sorted(set(symbols))
    for tf in tfs:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, open_time, open, high, low, close, spread "
                "FROM hcm_market.klines WHERE symbol = ANY(%s) AND time_frame=%s ORDER BY open_time",
                (_syms, tf),
            )
            rows = cur.fetchall()
        if not rows:
            continue
        kdf = pd.DataFrame(rows, columns=["symbol", "open_time", "open", "high", "low", "close", "spread"])
        kdf["open_time"] = pd.to_datetime(kdf["open_time"], utc=True)
        for sym, g in kdf.groupby("symbol"):
            g = g.sort_values("open_time").reset_index(drop=True)
            out.setdefault(sym, {})[tf] = _add_structure_factors(enrich_klines(g))
    return out


def _bar_at_or_before(kl: pd.DataFrame, ts) -> int | None:
    """返回 kl 中 open_time <= ts 的最后一根已收盘 bar 的 index（防未来泄露）。"""
    if kl is None or kl.empty:
        return None
    idx = kl["open_time"].searchsorted(ts, side="right") - 1
    if idx < 0:
        return None
    return int(min(idx, len(kl) - 1))


def _period_trend_state(adx, pdi, mdi, close, ema60) -> str:
    """各周期趋势态判定（与 hexp_engine 同构近似，单点快照版，无迟滞状态机连续性）。

    方向：ma(close vs EMA60) 与 DI 同向取之，矛盾记 0；强度用 ADX 分段近似 TrendScore；
    方向确定且 ts>=enter→TREND；ts<exit→RANGE；否则 TRANSITION。
    """
    if adx is None or pdi is None or mdi is None or close is None or ema60 is None:
        return "RANGE"
    di_dir = 1 if pdi >= mdi else -1
    ma_dir = 1 if close > ema60 else (-1 if close < ema60 else 0)
    if ma_dir == 0:
        pdir = di_dir
    elif ma_dir == di_dir:
        pdir = ma_dir
    else:
        pdir = 0
    if adx >= 50:
        ts = 85.0
    elif adx >= 25:
        ts = 60.0
    elif adx >= 15:
        ts = 45.0
    else:
        ts = 20.0
    if pdir != 0 and ts >= MTF_ENTER_TS:
        return "TREND_UP" if pdir > 0 else "TREND_DOWN"
    elif ts < MTF_EXIT_TS:
        return "RANGE"
    return "TRANSITION"


def h1_trend_dir_at(h1kl, ts) -> float:
    """返回 ts 时刻**已收盘** H1 棒的主趋势方向：+1.0=TREND_UP / -1.0=TREND_DOWN / 0.0=其它。

    【2026-09-02 方向头特征注入】供 direction head 感知"H1 主趋势方向"，使趋势对齐标签
    (build_labels dir_trend_align)能被模型真正学习 —— 否则方向头特征集无 H1 方向输入，
    即便标签把逆 H1 运动压 FLAT，模型也无法区分"H1 UP 的回调"与"震荡顶的反转"，仍在超买处判 SELL。

    与 _compute_verdict 同口径（quality_features 训练侧 / build_labels / quality_scorer 推理侧三方复用）：
      - 复用 _period_trend_state（ma close vs EMA60 与 DI 同向 + ADX 分段 TrendScore≥enter）；
      - ema60 现算（累计含至 i 的 EWM，与 _compute_verdict 一致）；
      - 只读过去已收盘棒 → 无未来泄露。
    """
    if h1kl is None or h1kl.empty:
        return 0.0
    i = _bar_at_or_before(h1kl, ts)
    if i is None:
        return 0.0
    try:
        row = h1kl.iloc[i]
        _adx = float(row["adx_14"])
        _pdi = float(row["plus_di"])
        _mdi = float(row["minus_di"])
        _close = float(row["close"])
    except Exception:
        return 0.0
    _ema60 = float(h1kl["close"].astype(float).iloc[: i + 1]
                   .ewm(span=60, adjust=False).mean().iloc[i])
    if any(pd.isna(v) for v in (_adx, _pdi, _mdi, _close, _ema60)):
        return 0.0
    _st = _period_trend_state(_adx, _pdi, _mdi, _close, _ema60)
    if _st == "TREND_UP":
        return 1.0
    if _st == "TREND_DOWN":
        return -1.0
    return 0.0


def _compute_verdict(kl_mtf: dict, kl_m5: pd.DataFrame, created_at) -> float:
    """从多周期 K 线重算 hexp 的 MTF 加权共识分 verdict ∈[-1,1]。

    同构 hexp_engine 第 6 步：M30/H1/H4/D1 按 weight_* 加权，RANGE 周期按主周期位置/RSI
    推导反向共识，单周期弱信号置信折扣(conf)。仅用截至 created_at 的已收盘 bar，无未来泄露。
    返回 float（-1~1），缺数据→0.0（与推理侧 hexp 缺省一致）。
    """
    if kl_m5 is None or kl_m5.empty:
        return 0.0
    i5 = _bar_at_or_before(kl_m5, created_at)
    if i5 is None:
        return 0.0
    # 主周期(M5) pos_pct（Donchian 60 分位）与 rsi，供 RANGE 周期反向共识推导
    _hi60 = kl_m5["high"].iloc[: i5 + 1].rolling(60).max()
    _lo60 = kl_m5["low"].iloc[: i5 + 1].rolling(60).min()
    _close = float(kl_m5["close"].iloc[i5])
    _hi = _hi60.iloc[i5]
    _lo = _lo60.iloc[i5]
    _rng = (_hi - _lo)
    _pos_pct = 0.5 if _rng == 0 else float((_close - _lo) / _rng)
    _rsi = kl_m5["rsi_14"].iloc[i5]
    _rsi = float(_rsi) if pd.notna(_rsi) else 50.0

    verdict = 0.0
    wsum_r = 0.0
    n_eff = 0
    for p in MTF_PERIODS:
        kl = kl_mtf.get(p)
        i = _bar_at_or_before(kl, created_at)
        if i is None:
            continue
        _adx = kl["adx_14"].iloc[i]
        _pdi = kl["plus_di"].iloc[i]
        _mdi = kl["minus_di"].iloc[i]
        if pd.isna(_adx) or pd.isna(_pdi) or pd.isna(_mdi):
            continue
        _c = float(kl["close"].iloc[i])
        _ema60 = float(kl["close"].iloc[: i + 1].ewm(span=60, adjust=False).mean().iloc[i])
        st = _period_trend_state(float(_adx), float(_pdi), float(_mdi), _c, _ema60)
        wp = MTF_WEIGHTS.get(p, 0.0)
        if wp <= 0:
            continue
        if st == "TREND_UP":
            pv = 1.0
        elif st == "TREND_DOWN":
            pv = -1.0
        elif st == "RANGE":
            # 方案 A：区间震荡反向共识（与 hexp 同口径）——高位/超买→一致做空(-1)，
            # 低位/超卖→一致做多(+1)，中位→无共识(0)
            if _pos_pct > 0.7 or _rsi > 70.0:
                pv = -1.0
            elif _pos_pct < 0.3 or _rsi < 35.0:
                pv = 1.0
            else:
                pv = 0.0
        else:  # TRANSITION
            pv = 0.0
        if pv == 0:
            continue
        verdict += pv * wp
        wsum_r += wp
        n_eff += 1
    if wsum_r > 0:
        verdict /= wsum_r
    # 最小有效周期数约束（防单周期拉满 verdict 假象），与 hexp 同口径
    if MTF_MIN_PERIODS > 0 and n_eff > 0:
        _conf = min(1.0, n_eff / MTF_MIN_PERIODS)
        verdict *= _conf
    return float(verdict)


def _row_tmf(tmf_sym, bar):
    """返回 tmf_* 13 列：命中则填真实值，未命中(无 bar / 无覆盖)→全 0.0（与推理侧缺省一致）。"""
    if bar is not None:
        bt = _as_naive_utc(bar["open_time"])
        if bt is not None and tmf_sym is not None and bt in tmf_sym:
            return dict(tmf_sym[bt])
    return {c: 0.0 for c in TMF_FEATURE_COLS}


def load_env(conn, symbols: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """加载事件日历 + 最近宏观/情绪/流动性快照。

    【流动性特征 2026-08-30】新增 liquidity_snapshots 表（与 macro/sentiment 同构）。
    表缺失时优雅降级：liq_df 为空 → 训练侧 liquidity 列全空 → 该特征填 0.0，不阻断训练。
    """
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
    # 【流动性特征 2026-08-30】独立查询 liquidity_snapshots（表可能尚未建，try 降级）
    liq_df = pd.DataFrame(columns=["category", "liquidity_score", "snapshot_time"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT category, liquidity_score, snapshot_time "
                "FROM hcm_market.liquidity_snapshots ORDER BY snapshot_time"
            )
            _l = cur.fetchall()
        liq_df = pd.DataFrame(_l, columns=["category", "liquidity_score", "snapshot_time"])
        if not liq_df.empty:
            liq_df["snapshot_time"] = pd.to_datetime(liq_df["snapshot_time"], utc=True)
            liq_df["liquidity_score"] = pd.to_numeric(liq_df["liquidity_score"], errors="coerce")
    except Exception as _e:
        print(f"[warn] liquidity_snapshots 读取失败(表未建?): {_e}", file=sys.stderr)
        # 【修复 2026-08-30】失败未回滚会使整个连接事务进入中止态，导致后续
        # load_ds_output / load_tmf_features 的查询全部报 InFailedSqlTransaction。
        # 已 fetch 的 events/snaps 数据在客户端，回滚不影响它们，仅重置服务端事务。
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    return ev, snap_df, liq_df


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

        events, snaps, liq = load_env(conn, sorted(set(signals["symbol"].tolist())))
        # 【DeepSeek 训练特征增强 2026-08-17】加载 DeepSeek 落库票，与推理侧同构
        ds_by_sym = load_ds_output(conn)
        # 【TimesFM 特征 2026-08-30】加载 TimesFM 离线特征，按 (symbol, M5 bar_time) 精确索引，
        # 供下方 per-signal 循环 join（与推理侧 build_features 同表同口径）。
        tmf_by_sym = load_tmf_features(conn)

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

        # 【2026-09-02】多周期 K 线加载（M30/H1/H4/D1），供 _compute_verdict 重算 verdict 特征。
        # 与 M5 同构 enrich；每 symbol 嵌套 {tf: kl}。仅离线重算用，运行期不占内存常驻。
        klines_mtf = load_klines_multi_tf(conn, signals["symbol"].tolist(), MTF_PERIODS)

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
                # 【2026-09-01 bar 对齐修复·训练侧】只用已收盘 bar：若信号时刻该 bar
                # 仍在形成中(open_time + 周期 > created_at)，其特征(mm/close_mom_atr/
                # body_ratio 等)尚未定型、随 tick 漂移 → 回退到前一根已收盘 bar，
                # 与推理侧 _last_closed 同口径（否则训练-推理分布不一致 → 实盘判反）。
                try:
                    _tf = str(r.get("time_frame") or "M5").upper()
                    _bar_sec = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                                "H1": 3600, "H4": 14400}.get(_tf, 300)
                    if kl.iloc[idx]["open_time"] + pd.Timedelta(seconds=_bar_sec) > r["created_at"]:
                        idx = max(0, idx - 1)
                except Exception:
                    pass
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
            # 【TimesFM 特征 2026-08-30】按 (symbol, M5 bar_time) 精确 join hcm_ai.timesfm_features。
            # _bar['open_time'] 即该信号所在 M5 bar 的 open_time，与特征表 bar_time 同义(精确对齐)。
            # 未命中(调度未覆盖的日期/非 XAUUSD)→ 13 列全 0.0，与推理侧缺省同分布。
            row.update(_row_tmf(tmf_by_sym.get(r["symbol"]), _bar))
            # 【2026-09-02】verdict 多周期共识分：从多周期 K 线重算（截至信号时刻已收盘 bar，无泄露）。
            # 与 hexp_engine 第 6 步同构近似；HEXP+live_override 全覆盖（落库 verdict 仅 HEXP 有）。
            # 推理侧 build_features 直接读 hexp 实时 snap["verdict"]（同源），两侧分布一致。
            _mtf = klines_mtf.get(r["symbol"], {})
            # 【2026-09-08 审计修复 P1】优先用引擎落库 verdict（推理同源真值），
            # 缺失时（live_override 等未落 _hexp 的模式）才回退离线无状态重算。
            _eng_v = r.get("verdict_engine")
            try:
                _eng_v = (float(_eng_v) if _eng_v is not None
                          and str(_eng_v).lower() != "nan" else None)
            except (TypeError, ValueError):
                _eng_v = None
            if _eng_v is not None:
                row["verdict"] = _eng_v
            else:
                row["verdict"] = _compute_verdict(_mtf, kl, r["created_at"])
            # 【2026-09-02 h1_trend_dir 特征注入】H1 主趋势方向(±1/0)：供方向头真正感知
            # "H1 主趋势"，使趋势对齐标签(dir_trend_align)可被模型学习(否则无 H1 输入，
            # 模型无法区分"H1 UP 回调" vs "震荡顶反转"→ 超买仍判 SELL)。
            # 与推理侧 quality_scorer.build_features(h1_feats["h1_trend_dir"]) 同源同口径。
            row["h1_trend_dir"] = h1_trend_dir_at(_mtf.get("H1"), r["created_at"])
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
            # 【流动性特征 2026-08-30】注入 liquidity(0~1)，与 macro/sentiment 同口径取 metals 最近快照；
            # 表未建或该信号早于所有快照 → None → 训练侧 fillna 后该特征恒 0.0（降级，不阻断）。
            row["liquidity"] = None
            if not liq.empty:
                _metals_l = liq[liq["category"] == "metals"]
                if not _metals_l.empty:
                    m_l = _metals_l[(_metals_l["snapshot_time"] <= r["created_at"]) & _metals_l["liquidity_score"].notna()]
                    if not m_l.empty:
                        row["liquidity"] = float(m_l.iloc[-1]["liquidity_score"])
                    else:
                        _fb_l = _metals_l[_metals_l["liquidity_score"].notna()].sort_values("snapshot_time")
                        if not _fb_l.empty:
                            row["liquidity"] = float(_fb_l.iloc[-1]["liquidity_score"])
            # 【DeepSeek 训练特征增强 2026-08-17】注入 ds_fake_prob/ds_sl_coeff/ds_continuity
            # 三特征，与推理侧 build_features 缺省(0.0)严格一致；按 signal 时刻近邻匹配落库票。
            _fp, _sl, _cont = _nearest_ds(ds_by_sym.get(r["symbol"], []), r["created_at"])
            row["ds_fake_prob"] = _fp
            row["ds_sl_coeff"] = _sl
            row["ds_continuity"] = _cont
            # 【阶段 2·方案 A·质量头治本】入场质量特征（与推理侧 build_features 同口径）。
            # 质量标签 R = atr * ai_sl_mult（build_labels.label_one 同定义）→ 入场质量用**无量纲**
            # 信号，避免 entry/sl 与 atr 单位不一致陷阱：
            #   r_dist_atr      : ai_sl_mult（实际 SL 倍数，越大=越易达标）
            #   sl_mult_used    : 同上（冗余对齐）
            #   entry_atr_ratio : (entry - 近期close均值)/atr（入场价位 ATR 归一 z，量纲无关）
            _atr_f = float(r.get("atr_14") or 0.0) or 1e-9
            _mult_f = r.get("ai_sl_mult")
            _entry_f = r.get("entry_price")
            try:
                _mult_f = float(_mult_f) if pd.notna(_mult_f) and _mult_f is not None else 2.0
                _entry_z = 0.0
                if pd.notna(_entry_f) and _entry_f is not None and kl is not None and not kl.empty:
                    _entry_v = float(_entry_f)
                    _close_arr = kl["close"].astype(float)
                    _close_mean = float(_close_arr.iloc[-20:].mean()) if len(_close_arr) >= 20 else float(_close_arr.iloc[-1])
                    _entry_z = (_entry_v - _close_mean) / (_atr_f + 1e-9)
                row["r_dist_atr"] = _mult_f
                row["sl_mult_used"] = _mult_f
                row["entry_atr_ratio"] = _entry_z
            except (TypeError, ValueError):
                row["r_dist_atr"] = 2.0
                row["sl_mult_used"] = 2.0
                row["entry_atr_ratio"] = 0.0
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
