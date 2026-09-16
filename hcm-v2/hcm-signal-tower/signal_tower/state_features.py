"""state_features.py — 行情状态模型（4 类）**特征契约单一真值**。

依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §3

设计纪律（图纸 §3「同源要求」）：
  * 本模块是训练侧(tools/build_state_labels.py)与推理侧(信号塔进程内)的**唯一**特征实现。
    任何一侧都不得复制/重写计算逻辑 —— 复制即漂移（历史教训：
    `_model_feature_cols.py` 记录过"双份数据源导致列错位"）。
  * 本模块**只依赖 numpy**（容器与主机环境都可直接跑），不使用 pandas，
    以便训练脚本以文件路径方式加载、推理侧直接 import，两侧字节级同一份代码。
  * 无未来泄露：`compute_features_at(i, ...)` 只引用 `<= i` 的已收盘 bar。

【实现补全说明（规格未明确、但工程上必需，已记录在方案文档差异清单）】
  1. 绝对价格类特征（箱体上沿/下沿/中值）**归一化为 ATR 距离**：
     直接喂 `box_upper=4123.4` 会让模型绑定黄金绝对价位，换品种/换价位即失效。
     归一化后 `(box_upper-close)/atr` 尺度无关（与 `box_dev` 共同表达同一信息）。
  2. 箱体窗口与统计窗口分离：`box_window` 是品种配置参数（信封规则），
     `win_window` 是特征统计窗口（固定）。
  3. `roc_mom`（ROC 动量导数）按 ATR 与窗口长度归一化。

回滚：无（新增文件，不改动任何既有模块）。
"""

from __future__ import annotations

import numpy as np

# ── 特征列顺序（契约。训练与推理必须逐列一致，推理侧须与 model.feature_name() 比对）──
STATE_FEATURE_COLS = [
    # ── 价格箱体（归一化，见模块 docstring 补全说明 1）──
    "box_upper_dist_atr",   # (box_upper − close) / atr
    "box_lower_dist_atr",   # (close − box_lower) / atr
    "box_width_atr",        # (box_upper − box_lower) / atr
    "box_dev",              # (close − box_mid) / box_width   ← 用户规格原式
    "win_high_dist_atr",    # (win_high − close) / atr
    "win_low_dist_atr",     # (close − win_low) / atr
    "hl_range_atr",         # (win_high − win_low) / atr
    "close_pctile",         # 窗口内 close 分位 ∈[0,1]
    "box_break_up",         # close > box_upper → 1
    "box_break_dn",         # close < box_lower → 1
    # ── 价格二阶加速度 ──
    "accel_2nd",            # (c[t] − 2c[t−1] + c[t−2]) / atr
    # ── ATR 波动率 ──
    "atr_14",
    "atr_chg",              # (atr[t] − atr[t−1]) / atr[t−1]
    "atr_pct",              # atr 在历史窗口的分位 ∈[0,100]
    "atr_std_ratio",        # atr 滚动标准差 / atr 滚动均值
    "atr_box_ratio",        # atr / box_width_raw
    # ── 趋势强度 ──
    "slope_linreg",         # 线性回归斜率 × 窗口 / atr
    "r2_linreg",            # 线性拟合 R²
    "adx_14",
    "plus_di",
    "minus_di",
    "roc_mom",              # ROC 动量导数（ATR 归一）
    "new_high_cnt",         # 窗口内创新高计数 / 窗口长度
    "new_low_cnt",          # 窗口内创新低计数 / 窗口长度
    "consec_same_dir_cnt",  # 连续同向 K 线计数（带符号）
    # ── 时段哑变量（UTC）──
    "session_eu",
    "session_us",
]

# ── 【2026-09-16 L1 候选特征集】量价 / 点差（**刻意不进 base 契约**）──────────────
# 为什么不直接追加进 `STATE_FEATURE_COLS`：
#   1) 它是**模型与推理共享契约**，`check_feature_contract()`（本文件下方）做**严格相等**
#      校验并 fail-fast ⇒ 一旦改动，线上 v3 模型立刻被拒绝加载（推理失败 → FSM 进 S9）。
#      故必须与"重训 + 换模型"**在同一次部署里原子切换**。
#   2) 先以"候选集"落地：`compute_features_at` 在**调用方提供 volume/spread 时**多算这几列
#      （返回 dict 的超集）；而推理侧 `state_infer.py` 只按 `STATE_FEATURE_COLS` 取列
#      ⇒ **生产行为零变化**，同时可离线用 `STATE_FEATURE_COLS_L1` 训练候选并 A/B。
#   3) 依据（2026-09-16 实测）：现有 27 维**全部是当期/历史量**，零成交量、零点差；
#      而 `klines` 的 `tick_volume` 填充率 **100%**（均值 2035）、`spread` **98.7%**
#      （均值 19.9）⇒ 两者可直接派生。`real_volume` 恒 0（CFD 特性）、`ticks` 表为空
#      ⇒ 那两项不可用（已实测，非推测）。
#      动机：口径修正后的 onset 任务 OOF AUC 仅 **0.5375**（≈随机）⇒ 需要"前置量"。
L1_FEATURE_COLS = [
    "vol_ratio",        # tick_volume / MA(tick_volume, vol_window)
    "vol_chg",          # (v[t] − v[t−1]) / v[t−1]
    "vol_pctile",       # v 在 percentile_window 内的分位 ∈[0,1]
    "spread_atr",       # spread / atr（流动性成本相对化）
    "spread_pctile",    # spread 在 percentile_window 内的分位 ∈[0,1]
    "vol_price_align",  # 相对量偏移 × sign(Δclose)：放量上涨 + / 放量下跌 −
]
# 候选特征集 = base(27) + L1(6)。**base 在前**，便于逐列对照与消融。
STATE_FEATURE_COLS_L1 = STATE_FEATURE_COLS + L1_FEATURE_COLS

# ── 参数默认值（与 web/api 白名单、PG seed 对齐；此处只作缺省兜底）──
DEFAULT_PARAMS: dict = {
    "box_window": 20,        # 箱体回看窗口（品种参数 state.box.window.{symbol}）
    "win_window": 50,        # 统计窗口（区间高/低/分位）
    "slope_window": 20,      # 线性回归窗口
    "atr_window": 14,
    "di_window": 14,
    "rsi_window": 14,
    "roc_window": 10,        # ROC 动量窗口
    "percentile_window": 120,  # atr_pct / close_pctile 的历史窗口
    "atr_std_window": 20,    # atr 滚动标准差的窗口
    "vol_window": 20,        # 【L1】相对量 vol_ratio 的均值窗口
}

# 计算 features_at(i) 所需的最小已收盘 bar 数（含 i 本身）
def min_bars(params: dict | None = None) -> int:
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    return max(
        int(p["percentile_window"]),
        int(p["win_window"]) + int(p["box_window"]),
        int(p["slope_window"]),
        int(p["roc_window"]),
    ) + 2


# ────────────────────────────── 基础指标（Wilder 口径）──────────────────────────────
# 口径与 tools/quality_features.enrich_klines 对齐（ewm alpha=1/period），保证与既有
# 生产指标同源，避免"同一指标两套数值"。

def _ewm(x: np.ndarray, alpha: float) -> np.ndarray:
    """一阶指数平滑（Wilder 平滑即 alpha=1/period）。"""
    out = np.empty_like(x, dtype=float)
    if len(x) == 0:
        return out
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    tr = np.empty_like(close, dtype=float)
    tr[0] = high[0] - low[0]
    prev = close[:-1]
    tr[1:] = np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - prev),
        np.abs(low[1:] - prev),
    ])
    return tr


def compute_indicators(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    params: dict | None = None,
) -> dict:
    """全序列指标（一次算完，features_at 按索引取用，避免逐 bar 重算）。"""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    atr_w, di_w = int(p["atr_window"]), int(p["di_window"])
    rsi_w = int(p["rsi_window"])

    tr = _true_range(high, low, close)
    atr = _ewm(tr, 1.0 / atr_w)

    up = np.zeros_like(close)
    dn = np.zeros_like(close)
    up[1:] = high[1:] - high[:-1]
    dn[1:] = low[:-1] - low[1:]
    plus_dm = np.where((up > dn) & (up > 0.0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0.0), dn, 0.0)
    atr_di = _ewm(tr, 1.0 / di_w)
    safe_atr = np.where(atr_di <= 0.0, np.nan, atr_di)
    plus_di = 100.0 * _ewm(plus_dm, 1.0 / di_w) / safe_atr
    minus_di = 100.0 * _ewm(minus_dm, 1.0 / di_w) / safe_atr
    di_sum = plus_di + minus_di
    dx = 100.0 * np.abs(plus_di - minus_di) / np.where(di_sum <= 0.0, np.nan, di_sum)
    dx = np.nan_to_num(dx, nan=0.0)
    adx = _ewm(dx, 1.0 / di_w)

    delta = np.zeros_like(close)
    delta[1:] = close[1:] - close[:-1]
    gain = _ewm(np.clip(delta, 0.0, None), 1.0 / rsi_w)
    loss = _ewm(np.clip(-delta, 0.0, None), 1.0 / rsi_w)
    rs = gain / np.where(loss <= 0.0, np.nan, loss)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = np.nan_to_num(rsi, nan=100.0)

    return {
        "atr": atr,
        "plus_di": np.nan_to_num(plus_di, nan=0.0),
        "minus_di": np.nan_to_num(minus_di, nan=0.0),
        "adx": adx,
        "rsi": rsi,
    }


# ────────────────────────────── 单 bar 特征 ──────────────────────────────

def _session_flags(open_epoch_s: np.ndarray | None, i: int) -> tuple[float, float]:
    """UTC 时段哑变量：eu=08–13，us=13–22，其余（含 22–24/00–08）归 asia。

    入参用 **Unix 秒（UTC）** 而非 datetime64：datetime64 无时区语义，
    传入 tz-aware 序列会触发 numpy 警告且语义含糊（历史踩坑），epoch 秒无歧义。
    """
    if open_epoch_s is None:
        return 0.0, 0.0
    try:
        h = (int(open_epoch_s[i]) % 86400) // 3600
    except Exception:
        return 0.0, 0.0
    return (1.0 if 8 <= h < 13 else 0.0), (1.0 if 13 <= h < 22 else 0.0)


def _linreg_slope_r2(y: np.ndarray) -> tuple[float, float]:
    """最小二乘斜率 + R²（x 为 0..n−1）。"""
    n = len(y)
    if n < 3:
        return 0.0, 0.0
    x = np.arange(n, dtype=float)
    xm, ym = x.mean(), y.mean()
    sxx = float(((x - xm) ** 2).sum())
    if sxx <= 0.0:
        return 0.0, 0.0
    sxy = float(((x - xm) * (y - ym)).sum())
    slope = sxy / sxx
    intercept = ym - slope * xm
    pred = slope * x + intercept
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - ym) ** 2).sum())
    r2 = 0.0 if ss_tot <= 0.0 else max(0.0, min(1.0, 1.0 - ss_res / ss_tot))
    return slope, r2


def compute_features_at(
    i: int,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    ind: dict,
    open_epoch_s: np.ndarray | None = None,
    params: dict | None = None,
    volume: np.ndarray | None = None,
    spread: np.ndarray | None = None,
) -> dict | None:
    """计算 bar i 的状态特征（只用 `<= i` 的已收盘数据）。

    Args:
        open_epoch_s: 各 bar 的 open_time（Unix 秒，UTC），仅用于时段哑变量；可 None。
        volume / spread: 【L1 候选】tick_volume 与 spread 序列。**两者都非 None 时**才会
            额外计算 `L1_FEATURE_COLS`（返回 dict 因此成为"超集"）；任一为 None 则
            返回与既有**逐位一致**的 base 键集。推理侧只按 `STATE_FEATURE_COLS` 取列，
            故默认路径（不传）对生产零影响。

    Returns:
        dict(列名 → float)，或 None（数据不足/出现非有限值 → 调用方判"特征不可用"）。
    """
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    box_w, win_w = int(p["box_window"]), int(p["win_window"])
    slope_w, roc_w = int(p["slope_window"]), int(p["roc_window"])
    pct_w, atr_std_w = int(p["percentile_window"]), int(p["atr_std_window"])

    need = max(pct_w, win_w + box_w, slope_w, roc_w) + 2
    if i < need - 1:
        return None

    atr_arr = ind["atr"]
    atr = float(atr_arr[i])
    if not np.isfinite(atr) or atr <= 0.0:
        return None

    c = float(close[i])
    h = float(high[i])
    lo = float(low[i])

    # ── 价格箱体（截至 i 的回看窗口，含 i）──
    box_hi = float(high[i - box_w + 1: i + 1].max())
    box_lo = float(low[i - box_w + 1: i + 1].min())
    box_width = box_hi - box_lo
    box_mid = 0.5 * (box_hi + box_lo)
    # ── 【2026-09-16 修复·死特征】突破判定必须用**不含当前 bar 的前窗** ──────────
    # 原实现 `box_break_up = 1.0 if c > box_hi` 而 `box_hi` 的窗口**含 i**，
    # 而 `high[i] >= close[i]` ⇒ `max >= high[i] >= c` ⇒ **条件恒假**，
    # `box_break_up`/`box_break_dn` **恒 0.0**（实测印证：模型 meta 的
    # feature importance 两列均为 **0.0**，2/27 维对模型零贡献、契约含死列）。
    # 语义上"突破箱体"本就该用 **i 之前**的箱体极值（= Donchian 口径），
    # 否则"箱体"由当前 bar 自己撑开，突破永不成立（同义反复）。
    # ⚠ 本次**只修这两个标志**：`box_hi/box_lo` 及其余 5 个箱体特征
    #   （upper/lower_dist_atr、width_atr、box_dev、atr_box_ratio）的窗口语义
    #   **保持不变** —— 隔离变量，便于把增益单独归因到"突破信息"这一项。
    #   （`i >= box_w` 不会发生：`min_bars` ≫ box_w，此处仅作防御。）
    _prev_hi = float(high[i - box_w: i].max()) if i >= box_w else c
    _prev_lo = float(low[i - box_w: i].min()) if i >= box_w else c

    win_hi = float(high[i - win_w + 1: i + 1].max())
    win_lo = float(low[i - win_w + 1: i + 1].min())

    win_close = close[i - win_w + 1: i + 1]
    close_pctile = float((win_close <= c).mean())

    # ── 二阶加速度 ──
    accel_2nd = (c - 2.0 * float(close[i - 1]) + float(close[i - 2])) / atr

    # ── ATR 族 ──
    atr_prev = float(atr_arr[i - 1])
    atr_chg = (atr - atr_prev) / atr_prev if atr_prev > 0.0 else 0.0
    atr_hist = atr_arr[max(0, i - pct_w + 1): i + 1]
    atr_pct = float((atr_hist <= atr).mean() * 100.0)
    atr_std_seg = atr_arr[max(0, i - atr_std_w + 1): i + 1]
    atr_mean = float(atr_std_seg.mean())
    atr_std_ratio = (float(atr_std_seg.std()) / atr_mean) if atr_mean > 0.0 else 0.0
    atr_box_ratio = (atr / box_width) if box_width > 0.0 else 0.0

    # ── 趋势强度 ──
    slope_raw, r2 = _linreg_slope_r2(close[i - slope_w + 1: i + 1])
    slope_linreg = slope_raw * slope_w / atr
    if i - roc_w - 1 >= 0:
        denom = atr * roc_w
        roc_mom = (c - float(close[i - roc_w])) / denom if denom > 0.0 else 0.0
    else:
        roc_mom = 0.0

    # 窗口内创新高/新低计数（相对其自身回看 box_w 的极值）
    n_hi = 0
    n_lo = 0
    for j in range(i - win_w + 1, i + 1):
        s = j - box_w
        if s < 0:
            continue
        if high[j] >= high[s: j + 1].max():
            n_hi += 1
        if low[j] <= low[s: j + 1].min():
            n_lo += 1
    new_high_cnt = n_hi / float(win_w)
    new_low_cnt = n_lo / float(win_w)

    # 连续同向 K 线计数（带符号：正=连续阳，负=连续阴）
    consec = 0
    j = i
    while j >= 1:
        d = close[j] - close[j - 1]
        if d == 0.0:
            break
        sign = 1 if d > 0 else -1
        if consec == 0:
            consec = sign
        elif (consec > 0) == (sign > 0):
            consec += sign
        else:
            break
        j -= 1
    consec_same_dir_cnt = float(consec)

    sess_eu, sess_us = _session_flags(open_epoch_s, i)

    feat = {
        "box_upper_dist_atr": (box_hi - c) / atr,
        "box_lower_dist_atr": (c - box_lo) / atr,
        "box_width_atr": box_width / atr,
        "box_dev": ((c - box_mid) / box_width) if box_width > 0.0 else 0.0,
        "win_high_dist_atr": (win_hi - c) / atr,
        "win_low_dist_atr": (c - win_lo) / atr,
        "hl_range_atr": (win_hi - win_lo) / atr,
        "close_pctile": close_pctile,
        # 【2026-09-16 修复】用**前窗**极值判突破（原用含 i 的 box_hi/box_lo ⇒ 恒 0）
        "box_break_up": 1.0 if c > _prev_hi else 0.0,
        "box_break_dn": 1.0 if c < _prev_lo else 0.0,
        "accel_2nd": accel_2nd,
        "atr_14": atr,
        "atr_chg": atr_chg,
        "atr_pct": atr_pct,
        "atr_std_ratio": atr_std_ratio,
        "atr_box_ratio": atr_box_ratio,
        "slope_linreg": slope_linreg,
        "r2_linreg": r2,
        "adx_14": float(ind["adx"][i]),
        "plus_di": float(ind["plus_di"][i]),
        "minus_di": float(ind["minus_di"][i]),
        "roc_mom": roc_mom,
        "new_high_cnt": new_high_cnt,
        "new_low_cnt": new_low_cnt,
        "consec_same_dir_cnt": consec_same_dir_cnt,
        "session_eu": sess_eu,
        "session_us": sess_us,
    }
    # ── 【L1 候选】量价 / 点差：**仅当调用方同时提供 volume 与 spread 时**计算 ──
    #    放在"有限性检查"**之前** ⇒ L1 若出现非有限值，本根同样判为不可用（保守，与 base 一致）。
    #    ⚠ 默认（两者为 None）时本块整体跳过 ⇒ 返回键集与改动前**逐位一致**（生产零影响）。
    if volume is not None and spread is not None:
        _vw = max(1, int(p["vol_window"]))
        _v = np.asarray(volume, dtype=float)
        _s = np.asarray(spread, dtype=float)
        _v_mean = float(_v[max(0, i - _vw + 1): i + 1].mean())
        _v_prev = float(_v[i - 1]) if i >= 1 else 0.0
        _v_hist = _v[max(0, i - pct_w + 1): i + 1]
        _s_hist = _s[max(0, i - pct_w + 1): i + 1]
        _c_prev = float(close[i - 1]) if i >= 1 else c
        _z = ((float(_v[i]) - _v_mean) / _v_mean) if _v_mean > 0.0 else 0.0
        feat.update({
            "vol_ratio": (float(_v[i]) / _v_mean) if _v_mean > 0.0 else 1.0,
            "vol_chg": ((float(_v[i]) - _v_prev) / _v_prev) if _v_prev > 0.0 else 0.0,
            "vol_pctile": float((_v_hist <= float(_v[i])).mean()),
            "spread_atr": (float(_s[i]) / atr) if atr > 0.0 else 0.0,
            "spread_pctile": float((_s_hist <= float(_s[i])).mean()),
            # 量价配合：相对量偏移 × 涨跌符号（放量上涨 +，放量下跌 −）
            "vol_price_align": _z * (1.0 if c > _c_prev else (-1.0 if c < _c_prev else 0.0)),
        })

    for k, v in feat.items():
        if not np.isfinite(v):
            return None
    return feat


def feature_vector(feat: dict) -> np.ndarray:
    """按 STATE_FEATURE_COLS 顺序装配一维向量（推理侧入模用）。"""
    return np.asarray([float(feat[k]) for k in STATE_FEATURE_COLS], dtype=np.float64)


def check_feature_contract(feature_names, allowed=None) -> list[str]:
    """比对模型自带 feature_name() 与**允许的特征集**，返回差异列表（空 = 一致）。

    推理侧必须在加载模型后调用（fail-fast：不一致即判推理失败，见方案 §5）。

    Args:
        allowed: 允许的特征列集合（"列名列表"的可迭代）；缺省 = 仅 base 契约
            ⇒ 与改动前**逐位一致**。
            波动扩张模型（路线 B）用 `STATE_FEATURE_COLS_L1`，故其加载点显式传
            `allowed=(STATE_FEATURE_COLS, STATE_FEATURE_COLS_L1)` —— 这样"放宽到哪
            几个集合"是**显式声明**的，而不是把校验改成"任意子集"（后者会静默放过
            列错位，正是本校验存在的意义）。
    """
    got = list(feature_names or [])
    cands = [list(c) for c in (allowed if allowed else (STATE_FEATURE_COLS,))]
    if got in cands:
        return []
    want = cands[0]
    missing = [c for c in want if c not in got]
    extra = [c for c in got if c not in want]
    diff: list[str] = []
    if missing:
        diff.append(f"missing={missing}")
    if extra:
        diff.append(f"extra={extra}")
    if not diff:
        diff.append("order_mismatch")
    return diff
