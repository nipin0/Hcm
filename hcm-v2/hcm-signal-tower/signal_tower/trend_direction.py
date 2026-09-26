"""trend_direction.py — 趋势方向裁决**单一真值**（规则模块，不是模型）。

规格来源：用户 2026-09-15「趋势方向计算（独立指标模块）」
    UP   : 斜率 >  阈值 且 +DI > −DI    → 多头趋势
    DOWN : 斜率 < −阈值 且 −DI > +DI    → 空头趋势
    NONE : 无明确单向趋势（方向模糊）

FSM 消费方式（用户 2026-09-15「FSM 组合规则」）：
    LGBM 防抖后的**形态**（4 类）与**方向**（本节 3 类）分开输入，联合决策：
      oscillation : 忽略方向，走箱体震荡策略
      trend_init  : 方向 UP/DOWN 允许顺势回调开仓；**NONE 禁止趋势开仓**
      trend_mid   : 方向 UP/DOWN 允许开仓与加仓；NONE 禁止新增（持仓保留）
      trend_fade  : 禁止新增，持仓收紧移动止损
    即：形态判趋势但方向 NONE → 不开趋势仓（规避方向模糊的假趋势）。

【工程补全 1｜阈值必须 ATR 归一 —— 否则不可移植】
    原始回归斜率单位是「价格/根」，随品种与价位量级变化：XAUUSD(≈2000) 的斜率天然是
    EURUSD(≈1.08) 的千倍。若照字面配置"斜率阈值"，同一阈值换品种即失效。
    故本模块统一用 **ATR 归一斜率**：
        slope_atr = slope_raw × slope_window / ATR
    语义 = "回归趋势线在窗口内的总位移相当于几个 ATR"，无量纲、跨品种可比。
    配置键 state.dir.slope_thr_atr 即以此为单位（默认 1.0 ≈ 窗内 1 个 ATR）。

【工程补全 2｜防抖做成"无状态纯窗口函数"——离线/线上逐点一致】
    确认方向 = 仅当**最近 k 根**原始方向全等时才等于该方向，否则 NONE：
        confirmed[i] = raw[i]  iff  raw[i−k+1 .. i] 全部相等且有效
                     = NONE    否则
    为什么不用"带 carry 的迟滞/驻留"（holds previous）：那是有状态逻辑，离线整段计算与
    线上增量计算在"起点不同"时结果可能分叉（本仓库历史事故类型：双实现漂移）。
    纯窗口函数 ⇒ 离线标签与线上推理**逐点相同**，且语义正好是"方向模糊=NONE"（保守侧）。

【单一真值｜指标不重复实现】
    ATR / +DI / −DI 直接复用 `state_features.compute_indicators`（Wilder 口径，与生产同源）；
    斜率直接调用 `state_features._linreg_slope_r2`（不写第二份，理由见下方"斜率"小节实测记录）。
    故 direction 的 slope_atr 与模型特征 `slope_linreg` **同源同值**，
    交叉比对自检在 tools/eval_trend_direction.py（`--no-selfcheck` 可跳过）。

回滚：无（新增文件，不改变任何既有模块行为）。
"""

from __future__ import annotations

import numpy as np

# ── 方向取值（编码顺序即模型/日志口径，禁止改动）──
DIR_NONE, DIR_UP, DIR_DOWN = 0, 1, 2
DIR_NAMES = ["none", "up", "down"]

# ── 配置键默认值（离线工具与信号塔共用同一份缺省，避免两处默认值分叉）──
DIR_CFG_FALLBACK: dict = {
    "state.dir.slope_thr_atr": 1.0,   # ATR 归一斜率阈值（窗内总位移/ATR）
    "state.dir.debounce_bars": 3.0,   # K 线防抖确认根数 k
}
# 指标窗口与 state_features 保持一致（同源）
DIR_PARAM_DEFAULTS: dict = {
    "slope_window": 20,
    "atr_window": 14,
    "di_window": 14,
}

# ── 周期秒数（跨周期对齐用；与 tools 侧同名常量含义一致）──
TF_SECONDS: dict = {"M5": 300, "M15": 900, "H1": 3600}


def align_last_closed(dir_open_epoch, dir_tf: str, base_open_epoch) -> np.ndarray:
    """跨周期**前视闭合**对齐：对每根基准 bar，返回"最后**已收盘**方向 bar"的下标。

    判据（唯一）：`dir_open_time + dir_tf_seconds ≤ base_open_time`
    —— 即该方向 bar 的**收盘时刻不晚于基准 bar 的开盘时刻**，等价于
    "基准 bar 只能看到已经收盘的大周期 bar"。

    Returns:
        与 `base_open_epoch` 等长的下标数组；`-1` = 该基准 bar 之前没有已收盘的方向 bar。
        **线上单点场景**（只关心最后一根基准 bar）= 取 `[-1]`；**离线整段**直接向量化用。

    【为什么必须是唯一实现】
      离线标定（`tools/eval_trend_direction.py`）、离线回放
      （`tools/replay_state_chain.py`）与线上（`scheduler._fetch_dir_series`）若各写一份，
      一旦某处写成"`≤ base_close`"就**多看到一根大周期 bar = 前视泄露**，
      而三处结论会互相印证"没问题"—— 本仓库的已知事故类型（双实现漂移）。
      故三处一律调用本函数。

    【为什么用 searchsorted 而不是逐 bar 扫描】
      `dir_close = dir_open + sec` 对同周期数据**单调递增**，故"最后一个 ≤ 基准开盘"的
      下标正是 `searchsorted(..., side="right") - 1`。O(n log m)，且对整段离线是纯向量化。
    """
    sec = TF_SECONDS.get(str(dir_tf).upper())
    o = np.asarray(dir_open_epoch, dtype=np.int64)
    b = np.asarray(base_open_epoch, dtype=np.int64)
    if sec is None:
        return np.full(b.shape, -1, dtype=np.int64)
    return np.searchsorted(o + sec, b, side="right") - 1


def dir_name(code: int) -> str:
    """编码 → 名字（越界按 none 处理，保守）。"""
    c = int(code)
    return DIR_NAMES[c] if 0 <= c < len(DIR_NAMES) else "none"


def min_bars(params: dict | None = None, cfg: dict | None = None) -> int:
    """产出一个有效方向判定所需的最小已收盘 bar 数（含当前 bar）。"""
    p = dict(DIR_PARAM_DEFAULTS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    c = dict(DIR_CFG_FALLBACK)
    if cfg:
        c.update({k: v for k, v in cfg.items() if v is not None})
    k = max(1, int(c["state.dir.debounce_bars"]))
    return max(int(p["slope_window"]), int(p["atr_window"]), int(p["di_window"])) + k


# ────────────────────────── 斜率：直接复用特征契约实现（不做第二份）──────────────────────────
#
# 【为什么不做向量化滚动版（实测记录）】曾实现 O(n) 前缀和版，与 per-bar 版比对发现
# **相对偏差 3.96e-06**：根因不是 y 的量级，而是 Σ(j·y)（j 为全局 bar 下标）在
# n≈2e4、price≈2000 时量级达 1e10，相邻窗口相减发生灾难性抵消（eps·1e10 ≈ 2e-6）。
# 局部化/中心化只能缓解不能根除。而"同一指标两份实现"正是本仓库明令禁止的事故模式
# （见 state_features docstring 第 6–8 行）→ 结论：**只保留一份**，用契约里的 per-bar 实现。
# 代价：整段 2e4 根约 0.3s（离线一次），线上每根只算 1 次 —— 可接受。

_SF_CACHE: dict = {}


def _sf_module():
    """按文件路径惰性加载 state_features（与 tools 侧加载方式一致，避免包导入依赖）。"""
    if "sf" not in _SF_CACHE:
        import importlib.util
        import os

        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location(
            "state_features", os.path.join(here, "state_features.py"))
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load state_features")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SF_CACHE["sf"] = mod
    return _SF_CACHE["sf"]


def linreg_slope_series(close: np.ndarray, window: int) -> np.ndarray:
    """逐 bar 最小二乘斜率（包装契约实现，保证与模型特征 `slope_linreg` 同源）。

    与 `state_features.compute_features_at` 中 `slope_linreg = slope_raw × w / ATR` 的
    slope_raw 完全一致 —— 自检见 tools/eval_trend_direction.py 的 slope_atr 交叉比对。
    """
    sf = _sf_module()
    y = np.asarray(close, dtype=float)
    n = len(y)
    w = int(window)
    out = np.full(n, np.nan, dtype=float)
    if w < 2 or n < w:
        return out
    for i in range(w - 1, n):
        out[i], _ = sf._linreg_slope_r2(y[i - w + 1: i + 1])
    return out


# ────────────────────────── 方向序列 ──────────────────────────

def compute_direction_series(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    ind: dict | None = None,
    params: dict | None = None,
    cfg: dict | None = None,
) -> dict:
    """整段方向裁决（离线标签与线上推理的**同一实现**）。

    Args:
        ind: `state_features.compute_indicators` 的产物；None 则内部计算（需要 high/low/close）。
        cfg: 至少含 state.dir.slope_thr_atr / state.dir.debounce_bars。

    Returns:
        dict(
          confirmed: np.ndarray[int]  最终方向（0 none / 1 up / 2 down）
          raw:       np.ndarray[int]  防抖前的逐根原始方向
          valid:     np.ndarray[bool] 该根是否可计算（数据充足且指标有限）
          slope_atr: np.ndarray[float] ATR 归一斜率（诊断/标定用）
          di_spread: np.ndarray[float] +DI − −DI（诊断用）
          debounce_bars: int
          slope_thr_atr: float
        )
    """
    p = dict(DIR_PARAM_DEFAULTS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    c = dict(DIR_CFG_FALLBACK)
    if cfg:
        c.update({k: v for k, v in cfg.items() if v is not None})

    thr = float(c["state.dir.slope_thr_atr"])
    k = max(1, int(c["state.dir.debounce_bars"]))
    w = int(p["slope_window"])

    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)

    if ind is None:
        ind = _sf_module().compute_indicators(high, low, close, p)

    atr = np.asarray(ind["atr"], dtype=float)
    plus_di = np.asarray(ind["plus_di"], dtype=float)
    minus_di = np.asarray(ind["minus_di"], dtype=float)

    slope = linreg_slope_series(close, w)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope_atr = slope * w / atr
    di_spread = plus_di - minus_di

    valid = (
        np.isfinite(slope_atr) & np.isfinite(di_spread)
        & (atr > 0.0) & (np.arange(n) >= min_bars(p, c) - 1)
    )

    raw = np.zeros(n, dtype=int)
    up_ok = valid & (slope_atr > thr) & (di_spread > 0.0)
    dn_ok = valid & (slope_atr < -thr) & (di_spread < 0.0)
    raw[up_ok] = DIR_UP
    raw[dn_ok] = DIR_DOWN

    # ── 防抖：最近 k 根原始方向全等才确认（纯窗口函数，无状态）──
    confirmed = np.zeros(n, dtype=int)
    # 【2026-09-25 上屏修复】`run_len` 提到外层：它 = **连续同值段长度**，即"方向防抖进度"
    #   （`confirmed` 要求 run_len ≥ k）。此前是块内局部变量 ⇒ 调用方拿不到 ⇒ 面板把它
    #   标成"无计数器变量/无数据源"（不准确）。暴露它**不改变任何判定**，仅供观测。
    run_len = np.zeros(n, dtype=int)
    if n > 0 and k >= 1:
        idx = np.arange(n)
        # 连续同值段的起点：raw 变化处或无效处开新段
        brk = np.zeros(n, dtype=bool)
        brk[0] = True
        if n > 1:
            brk[1:] = (raw[1:] != raw[:-1]) | (~valid[1:]) | (~valid[:-1])
        grp_start = np.maximum.accumulate(np.where(brk, idx, 0))
        run_len = idx - grp_start + 1
        ok = valid & (run_len >= k)
        confirmed[ok] = raw[ok]

    return {
        "confirmed": confirmed,
        "raw": raw,
        "valid": valid,
        "slope_atr": slope_atr,
        "di_spread": di_spread,
        "debounce_bars": int(k),
        "slope_thr_atr": float(thr),
        # 【2026-09-25 上屏修复】以下三个是**纯新增诊断键**（不参与任何判定）：
        #   plus_di / minus_di = `di_spread` 的两个分量（同一份 `ind`，单一实现口径）
        #   run_len            = 防抖进度（见上）
        # 为什么必须由本模块给出：面板要显示"+DI/−DI/防抖计数"，若另找来源
        # （如 `hcm:live:adx` 的 IndicatorCalculator 口径）就是**同一指标两份实现**，
        # 会出现"面板 DI 与方向裁决用的 DI 不一致"，属本仓库明令禁止的双真源。
        "plus_di": plus_di,
        "minus_di": minus_di,
        "run_len": run_len,
    }


def latest_direction(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    ind: dict | None = None,
    params: dict | None = None,
    cfg: dict | None = None,
) -> dict:
    """线上单点入口：返回最后一根的裁决 + 诊断。

    Returns:
        dict(code, name, valid, slope_atr, di_spread, plus_di, minus_di, run_len,
             raw_name, debounce_bars, slope_thr_atr)
        valid=False ⇒ 数据不足/指标异常（**不得**当作 NONE 用：NONE 是"判过、无方向"，
        valid=False 是"判不了"，按方案 §5 由调用方走 S9 暂停态）。
    """
    s = compute_direction_series(high, low, close, ind, params, cfg)
    i = len(close) - 1
    if i < 0:
        return {"code": DIR_NONE, "name": "none", "valid": False,
                "slope_atr": float("nan"), "di_spread": float("nan"),
                "plus_di": float("nan"), "minus_di": float("nan"),
                "run_len": 0, "raw_name": "none"}
    code = int(s["confirmed"][i])
    return {
        "code": code,
        "name": dir_name(code),
        "valid": bool(s["valid"][i]),
        "slope_atr": float(s["slope_atr"][i]),
        "di_spread": float(s["di_spread"][i]),
        # 【2026-09-25 上屏修复】随裁决一起返回诊断量（单一真源；调用方直接透传上屏）
        "plus_di": float(s["plus_di"][i]),
        "minus_di": float(s["minus_di"][i]),
        "run_len": int(s["run_len"][i]),
        "raw_name": dir_name(int(s["raw"][i])),   # 防抖**前**的原始方向（诊断）
        "debounce_bars": int(s["debounce_bars"]),
        "slope_thr_atr": float(s["slope_thr_atr"]),
    }
