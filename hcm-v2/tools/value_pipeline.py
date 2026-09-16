#!/usr/bin/env python3
"""value_pipeline.py — 价值头【训练 / 推理唯一共用】特征管线。

存在理由（根治口径漂移）：此前 `build_path_labels.py`（训练）与 `value_features.py`
（推理）各自实现了一份"镜像 + enrich + risk + dist_h1e_atr"，两份并不一致 —— 这既是
Bug-A 的土壤，也是"训练-推理 skew"的根源。本模块提供**唯一实现**，两侧只调用不复制。

── 本次修复（2026-09-12，读码 + 数据双证）────────────────────────────
Bug-A｜H1 镜像索引错配（旧 build_path_labels.py:153）
    `w.reindex(h1.index)` 中 w.index 是 DatetimeIndex、h1.index 是 RangeIndex
    → reindex 全 NaN → fillna(0) → mirror_where 内 d 恒 0 → **H1 从不镜像**。
    而 kl["close"] 对 world=-1 已翻负 → dist_h1e_atr = (−4300 − 4300)/ATR
    → 实测 world=-1 均值 **−732.60**（world=+1 为 +5.24），量纲完全不同 → 特征垃圾。
    修法：**彻底删除 H1 镜像**，dist_h1e_atr 统一用「sign 口径」
        dist = world × (真实 close − 真实 H1 EMA60) / ATR
    （与推理侧旧实现一致 → 顺带完成该特征的训练/推理口径统一。）

Bug-B｜逐 bar 镜像造成价格水平跳变（旧 build_path_labels.py:138 / value_features.py:94）
    `mirror_where` 按每根 bar 的 world 对**价格水平**取反：world 在 H1 边界切换时
    价格从 +4400 → −4400 跳变，污染 EWM/rolling → 实测 M5 ATR 均值 **84.57 / 126.26**
    （max 1415 / 1898），而正常应为 5~15；`risk` max 424 / 5696（代码规定 ≤3×ATR）。
    修法：改为**差分链式仿射镜像** —— 段内 pseudo = s·price + 常数，跨段**连续**：
        s[t] = −1 (world=−1) 否则 +1
        pc[t] = pc[t−1] + s[t]·(close[t] − close[t−1])
        po/ph/pl = pc + s·(o/h/l − close)，其中 h/l 用 max/min 保证 h≥l
    可证明：段内与「−price」仅差常数 ⇒ 全部仿射/比值类指标（rsi、donchian_q、
    dev_z_*、macd_slope3、extreme_reversal、risk、rise_atr、plus_di/minus_di 互换、
    pullback_depth）语义与旧实现**逐值一致**；且 TR 三项（h−l、h−pc、l−pc）逐项等幅
    ⇒ **ATR(pseudo) ≡ ATR(real)**，跨段不再有跳变污染。

── 口径统一 ─────────────────────────────────────────────────────
两侧调用同一 `build_features()`。推理侧用足够长窗口（M5≥M5_WINDOW、H1≥H1_WINDOW）后，
与训练侧"全历史"的差异仅来自 EWM 初始种子，量级 <1e-4（见 `WARMUP_NOTE`）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

try:
    import quality_features as QF  # noqa: E402  同目录复用指标管线
except Exception as _e:  # pragma: no cover
    print(f"[fatal] quality_features import failed: {_e}", file=sys.stderr)
    raise

# ── 口径常量（两侧必须一致）──
SWING = 20            # 顺向低位窗口（risk / rise_atr）
ENTER_TS = 60.0       # 世界判定强度门槛（与 QF.MTF_ENTER_TS 对齐）
SPREAD_POINT_VALUE = 0.01  # 1 spread 点 = 0.01 USD（XAUUSD digits=2；实测全价为 0.01 整数倍）
ANOM_WICK = 0.03      # 坏棒判据：实体外影线 > 3%×close，或 |open−前收| > 3%×close
                      # （实测本次全部命中 99.7% 落在 11 个突发日；黄金 M5 3%≈129 点，
                      #   远超任何可信的 5 分钟波动，且周末跳空零误判）
# 非平稳/量纲失真特征：不进入模型（训练侧在 build_path_labels 输出列中剔除；推理侧由
# 模型的 feature_name() 自动同步，不会读取）。
NON_STATIONARY_FEATURES = ("ema20",)
H1_EMA_SPAN = 60      # H1 EMA60 乖离基准
H1_EMA_WARMUP = 240   # 丢弃前 N 根 H1 的 EMA 值（=4×span，种子残权≈3e-4，保证"已收敛"）
M5_WINDOW = 1200      # 推理 M5 拉取长度（保证 dev_z_ema200 的 ewm 收敛）
H1_WINDOW = 400       # 推理 H1 拉取长度（≥ H1_EMA_WARMUP + 覆盖 M5 窗口所需）

WARMUP_NOTE = (
    "训练侧用全历史、推理侧用 M5_WINDOW/H1_WINDOW 定长窗口；固定长度 rolling 特征逐值相同，"
    "EWM 仅差初始种子（M5_WINDOW=1200 时 dev_z_ema200 残权≈6e-6；H1 丢弃前 240 根后残权≈3e-4）。"
)

# enrich_klines 产出的、需与模型特征名对齐的列（仅供 sanity 检查，不参与计算）
_PSEUDO_COLS = ("open", "high", "low", "close")


def h1_world_dir(h1: pd.DataFrame) -> pd.Series:
    """每根 H1 收盘后的世界方向 +1/-1/0（index = close_time）。唯一实现。

    判据（与旧版逐行一致）：close vs EMA60 与 DI 同向，且 ADX 分段强度 ≥ ENTER_TS。
    """
    cl = h1["close"].astype(float)
    hi, lo = h1["high"].astype(float), h1["low"].astype(float)
    ema60 = cl.ewm(span=60, adjust=False).mean()
    pc = cl.shift(1)
    tr = pd.concat([hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    up, dn = hi.diff(), -lo.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pdi = 100 * pd.Series(plus_dm, index=h1.index).ewm(alpha=1 / 14, adjust=False).mean() \
        / atr.replace(0, np.nan)
    mdi = 100 * pd.Series(minus_dm, index=h1.index).ewm(alpha=1 / 14, adjust=False).mean() \
        / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / 14, adjust=False).mean()
    dirs = np.where(pdi >= mdi, 1, -1)
    ma_dir = np.where(cl > ema60, 1, np.where(cl < ema60, -1, 0))
    pdir = np.where((ma_dir == 0) | (ma_dir == dirs), dirs, 0)
    ts = np.where(adx >= 50, 85.0, np.where(adx >= 25, 60.0, np.where(adx >= 15, 45.0, 20.0)))
    w = np.where((pdir != 0) & (ts >= ENTER_TS), pdir, 0)
    return pd.Series(w, index=h1["open_time"] + pd.Timedelta(hours=1)).sort_index()


def _asof_lookup(ref_index: pd.DatetimeIndex, ref_values: np.ndarray,
                 targets: pd.Series) -> np.ndarray:
    """对每个 target 取"最后一个 ≤ target"的 ref 值（因果 as-of，防未来泄露）。

    旧实现用 `merge(left_on='open_time', right_index=True)` 精确匹配 + ffill —— 因 M5
    open_time 与 H1 close_time 仅在 :00 命中，实际也退化为 as-of；此处改为显式 as-of，
    语义等价但不再依赖"恰好有 :00 命中"这一隐含前提。
    """
    if len(ref_index) == 0:
        return np.full(len(targets), np.nan)
    ri = np.asarray(ref_index.asi8)               # int64 ns（tz-aware 取 UTC）
    tv = np.asarray(pd.DatetimeIndex(targets).asi8)
    pos = np.searchsorted(ri, tv, side="right") - 1
    out = np.full(len(tv), np.nan)
    ok = pos >= 0
    out[ok] = ref_values[pos[ok]]
    return out


def repair_anomalous_bars(df: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """把「open/low 被写入坏值」的棒收敛到有界保守值，返回 (修复后 df, 坏棒掩码)。

    ── 取证（2026-09-12，实测非推断）────────────────────────────────────
    · 成因：`mt5_bridge.write_klines_to_pg` 的 upsert 用
      `high=GREATEST(existing,EXCLUDED) / low=LEAST(...)` → 一次坏写入把 low **永久粘住**；
      `open` 不在 UPDATE 列表 → 保留**首次写入**值。两个机制共同留下本次异常。
    · 范围：2026-07-08 16:50 ~ 07-10 13:20，连续 2 段共 **533 根** M5（占 2.7%）。
    · 脏字段：`open`/`low` 100% 异常（≈0.648×真实价）；`close` 0/533 异常且与邻棒连续；
      `high` 0/533 异常 ⇒ **close/high 可信**。
    · 可否反演：坏值比例 cv≈0.68%（p5 0.6415 / p95 0.6558）**非恒定** ⇒ 无法复原真值。

    ── 处理 ──────────────────────────────────────────────────────────
    只做"有界收敛"而非"复原"（坏值比例 cv≈0.68% 非恒定，无法反演）。坏棒窗口的标签
    候选由调用侧另行剔除。判据只用当前棒与前收（因果），训练/推理共用同一实现。

    判据（W = ANOM_WICK = 3%）：
      a) 实体下方孤立影线  (min(O,C) − L)/C > W
      b) 实体上方孤立影线  (H − max(O,C))/C > W
      c) 开盘跳空          |O − 前收|/C > W
    实测三类家族：
      · 07-08~07-10 共 533 根：open/low 同步被缩放为 ≈0.648×真实价（单看影线抓不到，
        因 open/low 一致 → 由 c) 「开盘跳空 34.7%」捕获）
      · 08-06/08-07 等：low 孤立下探 ≈250 点（下影 5~6.5%）→ 由 a) 捕获
      · 08-20 等：open 跳空 4% 而 high/close 正常 → 由 c) 捕获
    反例校验（**不误判真实大棒**）：06-17 19:20 `O=4365.36 H=4366.45 L=4236.26 C=4236.28`
    （开在最高、收在最低的真实大阴棒）→ 实体外影线≈0、开盘跳空 2.86% < 3% ⇒ 不命中 ✓
    实测该判据命中 1411 根（7.13%），**99.7% 落在 11 个突发日**（07-09 全天 288 根、
    08-07 276 根…），强证据表明全部为数据缺陷而非市场行为。

    处理：命中棒的 open/high/low **统一收敛为 close**（close 在三类家族中实测均可信：
    0/533 异常且与邻棒 close 连续）。收敛后该棒 range=0 → 不制造任何虚假极值（保守），
    且 TR=|close−前收| 有界。
    """
    out = df.copy()
    close = out["close"].astype(float).to_numpy()
    low = out["low"].astype(float).to_numpy()
    high = out["high"].astype(float).to_numpy()
    opn = (out["open"].astype(float).to_numpy() if "open" in out.columns
           else np.r_[close[0], close[:-1]])
    prev_close = np.r_[close[0], close[:-1]]
    scale = np.maximum(close, 1e-9)

    body_lo = np.minimum(opn, close)
    body_hi = np.maximum(opn, close)
    bad = (((body_lo - low) / scale > ANOM_WICK)
           | ((high - body_hi) / scale > ANOM_WICK)
           | (np.abs(opn - prev_close) / scale > ANOM_WICK))
    if bad.any():
        out.loc[bad, "high"] = close[bad]
        out.loc[bad, "low"] = close[bad]
        out.loc[bad, "open"] = close[bad]
    return out, bad


def attach_world(m5_raw: pd.DataFrame, h1_raw: pd.DataFrame) -> pd.DataFrame:
    """给 M5 挂 world（as-of H1 收盘后生效，缺失前向填充，world=0 表示无趋势世界）。"""
    m5 = m5_raw.sort_values("open_time").reset_index(drop=True).copy()
    w = h1_world_dir(h1_raw.sort_values("open_time").reset_index(drop=True))
    vals = _asof_lookup(w.index, w.to_numpy().astype(float), m5["open_time"])
    m5["world"] = pd.Series(vals).ffill().fillna(0).astype(int).to_numpy()
    return m5


def build_pseudo_ohlc(m5: pd.DataFrame) -> pd.DataFrame:
    """顺向伪序列（差分链式仿射镜像，跨段连续）——修 Bug-B。

    段内 pseudo = s·price + const（s=−1 当 world=−1），与旧「逐 bar 取反价格水平」
    仅差一个段内常数 ⇒ 所有仿射/比值类指标逐值一致；跨段不跳变 ⇒ 不污染 ATR/滚动量。
    """
    world = m5["world"].to_numpy()
    s = np.where(world == -1, -1.0, 1.0)
    close = m5["close"].astype(float).to_numpy()
    high = m5["high"].astype(float).to_numpy()
    low = m5["low"].astype(float).to_numpy()
    opn = (m5["open"].astype(float).to_numpy() if "open" in m5.columns
           else np.r_[close[0], close[:-1]])

    # 链式：pc[t] = pc[t-1] + s[t]·(close[t] − close[t-1])；首根取 s[0]·close[0]
    d = np.diff(close, prepend=close[0])
    d[0] = 0.0
    step = s * d
    step[0] = 0.0
    pc = s[0] * close[0] + np.cumsum(step)

    off_h = s * (high - close)
    off_l = s * (low - close)
    out = m5.copy()
    out["close"] = pc
    out["high"] = pc + np.maximum(off_h, off_l)   # s=−1 时 high/low 角色互换，取极值保证 h≥l
    out["low"] = pc + np.minimum(off_h, off_l)
    out["open"] = pc + s * (opn - close)
    return out


def h1_ema60_ref(h1_raw: pd.DataFrame) -> pd.Series:
    """真实 H1 的 EMA60（**不做镜像**），丢弃前 H1_EMA_WARMUP 根保证"已收敛"口径。

    返回 index = H1 close_time。诊断见 Bug-A：旧实现试图镜像 H1 但因索引错配静默失败，
    导致 dist_h1e_atr 对 world=−1 变成量纲垃圾；此处改为不镜像 + sign 口径。
    """
    h1 = h1_raw.sort_values("open_time").reset_index(drop=True)
    e = h1["close"].astype(float).ewm(span=H1_EMA_SPAN, adjust=False).mean()
    e = e.iloc[H1_EMA_WARMUP:]
    idx = pd.DatetimeIndex(h1["open_time"].iloc[H1_EMA_WARMUP:] + pd.Timedelta(hours=1))
    return pd.Series(e.to_numpy(), index=idx).sort_index()


def build_features(m5_raw: pd.DataFrame, h1_raw: pd.DataFrame) -> pd.DataFrame:
    """训练/推理共用的完整特征帧（含 label 所需 risk）。返回按 open_time 升序的 kl。

    列：enrich_klines 全量 + risk / rise_atr / dist_h1e_atr / session_* + world + open_time
        + anomaly（坏棒掩码，供标签侧在异常窗±余量内剔除候选）。
    """
    m5_s = m5_raw.sort_values("open_time").reset_index(drop=True)
    m5_fixed, anom = repair_anomalous_bars(m5_s)     # 先修坏棒，再算指标
    m5 = attach_world(m5_fixed, h1_raw)              # 已排序；attach_world 内部再 sort 顺序不变
    kl = QF.enrich_klines(build_pseudo_ohlc(m5))
    # enrich_klines 内部已 sort+reset_index，此处用 numpy 赋值保证索引无关
    kl = kl.assign(open_time=m5["open_time"].to_numpy(),
                   world=m5["world"].to_numpy(),
                   anomaly=anom.astype(bool))

    atr = kl["atr"]
    lo20 = kl["low"].rolling(SWING).min()
    kl["risk"] = (kl["close"] - lo20.shift(1)).clip(lower=0.3 * atr, upper=3.0 * atr)
    kl["rise_atr"] = (kl["close"] - lo20) / atr.replace(0, np.nan)

    # 顺向 H1-EMA60 乖离（sign 口径，训练/推理统一；真实价格，不镜像）
    h1e = h1_ema60_ref(h1_raw)
    kl["h1e60"] = _asof_lookup(h1e.index, h1e.to_numpy(), kl["open_time"])
    kl["h1e60"] = pd.Series(kl["h1e60"]).ffill().to_numpy()
    real_close = m5["close"].astype(float).to_numpy()
    kl["dist_h1e_atr"] = (kl["world"] * (real_close - kl["h1e60"])) / atr.replace(0, np.nan)

    # 【口径修正 · ema20】31 个特征中唯一的"价格水平类"量。差分链式的段内偏移 K 不归零，
    # 若沿用 enrich 产出的 ema20（= s·EMA20_real + K）会随历史漂移 → 训练(全历史)与推理
    # (定长窗口) 不一致（实测差 3.4e2）。改为在【真实】close 上算 EMA20 后乘 world：
    #   = world × EMA20_real，即镜像语义的精确值（旧实现 world=−1 时为 −EMA20 + K 的跳变残渣）。
    # 该式与窗口长度无关、无 K，恢复"定长窗口可复现"。
    # 符号用 s（world==−1 → −1，否则 +1），与伪序列镜像语义一致：world=0 行取 +EMA20_real
    # （旧实现即如此），避免出现 world=0 → ema20≡0 的语义突变。
    _s_dir = np.where(kl["world"].to_numpy() == -1, -1.0, 1.0)
    _ema20_real = pd.Series(real_close).ewm(span=QF.EMA_FAST, adjust=False).mean().to_numpy()
    kl["ema20"] = _s_dir * _ema20_real

    # 【量纲修正 2026-09-12】enrich 产出的 spread_atr = spread_num/atr 把「点」直接除以
    # 「价格单位」（实测中位 ≈2.9，语义失真）。实测 XAUUSD 报价全为 0.01 整数倍
    # ⇒ MT5 point = 0.01 ⇒ 正确换算 spread(点) × 0.01 / ATR（黄金点差 0.20 USD，量级吻合，
    # 而非 20 USD 的荒谬值）。仅在本管线覆盖；quality_features 保持原样以免波及质量头。
    kl["spread_atr"] = (kl["spread_num"] * SPREAD_POINT_VALUE) / atr.replace(0, np.nan)

    kl = kl.join(kl["open_time"].apply(lambda t: pd.Series(QF.session_onehot(t))))
    return kl


if __name__ == "__main__":  # 简易 sanity（需 DB）
    import psycopg2

    conn = psycopg2.connect(os.environ.get(
        "DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"))
    m5 = pd.read_sql("SELECT open_time,open,high,low,close,spread FROM hcm_market.klines "
                     "WHERE symbol='XAUUSD' AND time_frame='M5' ORDER BY open_time DESC "
                     "LIMIT 1200", conn)
    h1 = pd.read_sql("SELECT open_time,open,high,low,close,spread FROM hcm_market.klines "
                     "WHERE symbol='XAUUSD' AND time_frame='H1' ORDER BY open_time DESC "
                     "LIMIT 400", conn)
    conn.close()
    m5["open_time"] = pd.to_datetime(m5["open_time"], utc=True)
    h1["open_time"] = pd.to_datetime(h1["open_time"], utc=True)
    kl = build_features(m5, h1)
    last = kl.iloc[-1]
    print("world=", int(last["world"]), " atr=", round(float(last["atr"]), 3),
          " risk=", round(float(last["risk"]), 3),
          " dist_h1e_atr=", round(float(last["dist_h1e_atr"]), 3))
