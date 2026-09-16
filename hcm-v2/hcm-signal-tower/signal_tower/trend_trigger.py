"""trend_trigger.py — 趋势**起点触发器**（单一真值，纯计算、无 IO/无模型加载）。

依据：方案 §30（走前式 3 折、262 次真值切换的实测）

| 方案 | 漏检率 | 误报率 | 中位提前量 |
|---|---|---|---|
| 起点(4 类 argmax 推导) | 20.0% | 67.8% | **+3.5（滞后）** |
| **本模块 `rise|donch`** | **0.4%** | **16.6%** | **−1.0（提前）** |

构成 = 两个子检测器的 **OR**：
  · `rise`     ：`ΔP = P(起点)[t] − P(起点)[t−m]` ≥ 阈值 —— **动态**判据
  · `donchian` ：`close` 突破前 w 根极值

【为什么 rise 必须用"动态"而非"水平"——这是本模块存在的核心理由】
    §29.4 实测：`P(起点)` 是在起点前若干根**逐步抬高**的（d=5 已 0.676、d=20 才 0.483），
    不是只在起点那一根跳高。故"P ≥ 阈值"的 ON 段会**横跨多个事件**持续有效，
    触发器一旦 ON 就长时间不灭 → 一根 ON 段覆盖多次起点 → 其上升沿对不上单个起点。
    实测后果：漏检 142/262、中位**滞后** +3.0。改用 ΔP 后：漏检 54、中位 −1.0 且三折全负。
    （并见 §29.2：阈值规则若用"最大 F1"会随 base rate 漂移，触发率从 40.9% 飙到 87.0%；
      故 `rise_thr` 必须用**目标触发率分位**标定，不能事后挑分位。）

【与其他模块的边界】
    本模块**只做判定**，不加载模型、不读配置、不碰 Redis。
    `onset_proba`（P(起点) 序列）由推理层提供（`state_infer` 加载 `lgbm_onset_*` 模型），
    方向由 `trend_direction` 提供，二者与形状描述器在 FSM 中按三件套组合规则汇合。

回滚：无（新增文件，不改变任何既有行为）。
"""

from __future__ import annotations

import numpy as np

# ── 配置键默认值（离线标定产物；生产以 PG/Redis 覆盖）──
TRIGGER_CFG_FALLBACK: dict = {
    "state.trigger.donchian_w": 20.0,   # 突破回看根数（§27 数据驱动标定：w=20 为局部最优）
    "state.trigger.rise_m": 3.0,        # ΔP 跨度的 bar 数
    # rise_thr 由"目标触发率分位"在训练段标定；M5/L=5 实测值如下（**按品种/周期需重标**）
    "state.trigger.rise_thr": 0.2948,
    "state.trigger.use_rise": 1.0,      # 1/0 开关
    "state.trigger.use_donchian": 1.0,
}


def _cfg(cfg: dict | None) -> dict:
    c = dict(TRIGGER_CFG_FALLBACK)
    if cfg:
        c.update({k: v for k, v in cfg.items() if v is not None})
    return c


def compute_donchian(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                     window: int = 20) -> np.ndarray:
    """突破检测：close 突破**前 window 根**（不含当前）的最高/最低 → True。

    用 shift(1) 语义（只用 t 之前的数据）→ 无未来泄露。
    向量化实现（`sliding_window_view`）：逐 bar 循环在 2e4 根上约 0.5s，
    而离线标定会对多个 w × 多折反复调用 —— 故必须向量化。
    等价性由 `tools/verify_state_machine_age.py` 的对照自检守门。
    """
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    n = len(c)
    out = np.zeros(n, dtype=bool)
    w = int(window)
    if w < 1 or n <= w:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    # 第 i 根（i ≥ w）的比较基准 = 窗口 [i−w, i) → 即滑窗视图的前 n−w 行
    m_hi = sliding_window_view(h, w)[:n - w].max(axis=1)
    m_lo = sliding_window_view(l, w)[:n - w].min(axis=1)
    out[w:] = (c[w:] > m_hi) | (c[w:] < m_lo)
    return out


def compute_rise(onset_proba: np.ndarray | None, m: int = 3,
                 thr: float = 0.2) -> np.ndarray:
    """上升速率：`P[t] − P[t−m] ≥ thr`（**动态**判据，见模块 docstring）。"""
    if onset_proba is None:
        return np.zeros(0, dtype=bool)
    p = np.asarray(onset_proba, dtype=float)
    n = len(p)
    out = np.zeros(n, dtype=bool)
    mm = max(1, int(m))
    if n <= mm:
        return out
    d = np.full(n, np.nan)
    d[mm:] = p[mm:] - p[:-mm]
    ok = np.isfinite(d)
    out[ok] = d[ok] >= float(thr)
    return out


def latest(high: np.ndarray, low: np.ndarray, close: np.ndarray,
           onset_proba_tail: np.ndarray | None = None,
           cfg: dict | None = None) -> dict:
    """取**最后一根** bar 的触发器判定（线上入口）。

    Args:
        onset_proba_tail: P(起点) 序列的**末尾若干根**（至少 m+1 根即可）。
            线上无需整段概率 —— ΔP 只用到 P[t] 与 P[t−m]，故推理层只推末尾几根，
            避免每根 bar 对全历史重算（`compute_rise` 供离线整段使用）。

    Returns:
        dict(on, rise, donchian, valid, reason)
        on=True 表示"检测到趋势起点" → FSM 可在方向非 NONE 时进入趋势态。
        valid=False 表示数据不足（**不得**当作 on=False 与"判过但没触发"混用）。
    """
    c = _cfg(cfg)
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    cl = np.asarray(close, dtype=float)
    n = len(cl)
    if n < 2:
        return {"on": False, "rise": False, "donchian": False,
                "valid": False, "reason": "insufficient_bars"}

    w = int(c["state.trigger.donchian_w"])
    m = max(1, int(c["state.trigger.rise_m"]))
    need = max(w + 1, m + 1)
    use_rise = float(c["state.trigger.use_rise"]) > 0.5
    use_don = float(c["state.trigger.use_donchian"]) > 0.5

    don_now = bool(compute_donchian(h, l, cl, w)[-1]) if use_don else False

    rise_now = False
    if use_rise and onset_proba_tail is not None:
        pt = np.asarray(onset_proba_tail, dtype=float)
        if len(pt) > m:
            d = float(pt[-1]) - float(pt[-1 - m])
            rise_now = bool(np.isfinite(d) and d >= float(c["state.trigger.rise_thr"]))

    parts = [nm for nm, flag in (("rise", rise_now), ("donchian", don_now)) if flag]
    return {
        "on": bool(don_now or rise_now),
        "rise": rise_now,
        "donchian": don_now,
        "valid": n >= need,
        "reason": ("+".join(parts) if parts else "none"),
    }


def calibrate_rise_thr(onset_proba_train: np.ndarray, m: int,
                       on_rate: float = 0.10) -> float:
    """按**目标触发率分位**标定 ΔP 阈值（与 base rate 解耦）。

    为什么不用"最大 F1"：§29.2 实测该规则会随 base rate 漂移（阈值 0.9166→0.6868，
    触发率 40.9%→87.0%）→ 触发器几乎一直 ON，上升沿与事件脱节。故必须用分位规则。
    """
    p = np.asarray(onset_proba_train, dtype=float)
    mm = max(1, int(m))
    if len(p) <= mm:
        return 0.2
    d = p[mm:] - p[:-mm]
    d = d[np.isfinite(d)]
    if d.size < 50:
        return 0.2
    return float(np.quantile(d, 1.0 - float(on_rate)))
