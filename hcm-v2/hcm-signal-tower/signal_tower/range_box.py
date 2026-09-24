"""range_box.py — RANGE（震荡）态**箱体几何模型**的唯一实现（纯逻辑、可单测、无 IO）。

【为什么单独成模块】（2026-09-18 用户指令：RANGE 态箱体独立设计 + 结合 hexp）
  设计前取证发现系统内"区间"口径已有 **6 套**（互为同义不同源）：
    #  实现                                      窗口  含当前bar  边缘         消费方
    1  hexp._get_donchian_pct                     20   否        极值          极值护栏
    2  hexp._get_cycle_position                   60   否        极值          hexp 位置因子 f_pos
    3  scheduler RANGE 宽度过滤                    50   **是**     极值          RANGE MR 宽度门
    4  scheduler RANGE break_guard                50   否        极值          RANGE MR 熔断
    5  range_bonus.compute_range_position         30   否        极值∩布林     评分加成 %b_range
    6  state_strategy.compute_entry_box           12   否        quantile 95/5 FSM S1 箱体单/面板
  ⇒ 同一个"箱体"被各消费方各自内联重算，参数/含不含当前 bar/边缘口径全不一致，
    导致「口径漂移」类缺陷（如 #3 inclusive 使极值信号自我抬高、与 #4 同叫 50 却不同语义）。
  本模块把箱体**上收为唯一几何实现**，各消费方改为调用它（分期收敛，见
  `docs/方案_RANGE态箱体_hexp_20260918.md`）。

【本模块不做什么】（避免再造双真源）
  · **不做方向裁决** —— MR 方向仍由 `range_strategy.mr_direction` 单一裁决；
    本模块只回答"给定方向与箱体位置是否矛盾"（`confirm_direction`）。
  · **不判 regime** —— RANGE 判定沿用 hexp 快照的 `period_states`（已权威）。
  · **不读写 Redis/PG、不下单** —— 纯函数，配置由调用方注入 `cfg` dict。

【口径与 FSM 同源（可复现）】
  `exclusive=True` 时切片为 `arr[n-1-window : n-1]`，与
  `state_strategy.compute_entry_box` 的 `seg = high[n-1-window:n-1]` **逐字节一致**；
  quantile 边缘同样用 `np.percentile` ⇒ 给定相同 window/bands_mode，两者产出相同三线
  （见 §复核：自证用例）。

【双尺度（治"一个窗口两用"）】
  fast（`range.box.window`，默认 12）：当前震荡**幅度** → 宽度下限 / 贴边确认 / TP 可行性
  slow（`range.box.window_slow`，默认 50）：是否**仍是**震荡 → 突破判定 / 宽度上限

配置键（生产以 PG/Redis 权威；DEFAULTS 仅 fallback，铁律：禁硬编码）：
  range.box.enabled / window / window.{symbol} / window_slow
  range.box.bands_mode / q_high / q_low / exclusive
  range.box.min_width_atr / drift_max_atr / touch_min / wick_atr
  range.box.edge_tol_atr / break_buf_atr / quality_min
  range.box.width_source / break_source / gate_scale / publish
  （上下限阈值**复用既有** range.width_min_atr / range.width_max_atr，不新增键）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── 配置兜底（与 PG/Redis 缺失时一致；生产以配置中心为准）──────────────
DEFAULTS: dict = {
    "range.box.enabled": True,
    # 快箱：当前震荡幅度（贴边确认 / TP 可行性 / 宽度下限）
    "range.box.window": 12,
    # 慢箱：是否仍是震荡（突破判定 / 宽度上限）
    "range.box.window_slow": 50,
    # 边缘口径：quantile（生产 FSM 同款，削插针，实测比 extremum 窄 8%）| extremum
    "range.box.bands_mode": "quantile",
    "range.box.q_high": 0.95,
    "range.box.q_low": 0.05,
    # True = 只用**已收盘** bar（切片 n-1-window:n-1），与 compute_entry_box 一致。
    # 为什么必须默认 True：本箱体服务于"用收盘价贴边"判定；若含当前 bar，
    # 则 upper==max(high) 恒成立 → 宽度被自身创新高抬高（§7.1 箱体滑动陷阱）。
    "range.box.exclusive": True,
    # 退化保护：窄于此宽度视为无效箱体（与 state.osc_box_min_width_atr 语义对齐）
    "range.box.min_width_atr": 1.0,
    # 漂移上限（ATR/根）：收盘线性回归斜率超过此值 → 单边漂移，非震荡
    "range.box.drift_max_atr": 0.30,
    # 边界触碰次数下限（真箱体应有多次边界拒绝）
    "range.box.touch_min": 2,
    # 触碰判定容差（ATR 倍数）
    "range.box.wick_atr": 0.25,
    # 贴边确认容差（ATR 倍数）：BUY 需 close <= lower + tol*ATR，SELL 需 close >= upper - tol*ATR
    "range.box.edge_tol_atr": 0.50,
    # 突破判定缓冲（ATR 倍数）：close 越过边界此缓冲才算破箱
    "range.box.break_buf_atr": 0.0,
    # 【A 2026-09-18】突破判定专用边缘口径：`extremum` | `quantile`。
    # 必须与宽度门的 quantile **分离**（同一箱体两用是缺陷来源）：
    #   quantile 削插针 ⇒ 边界落在极值**内侧**，用它判"突破"会把 RSI 极值当根
    #   几乎全判成破沿（实测 82.9%；极值口径仅 46.5%）—— 而"突破"的定义本就是
    #   **创新极值**，旧实现用极值边界正是因此。宽度/位置仍用 quantile（稳健优先）。
    "range.box.break_bands_mode": "extremum",
    # 【B 2026-09-18】连续确认根数：需连续 N 根**同向**越界才认定突破。
    # 单根越界 = 回踩（≈ 本策略的入场条件本身），不应触发熔断并作废本根机会。
    # 实测「宽度门 ∧ 未破沿」存活率：单根判定 15.5% → A 48.1% → **A+B 66.3%**。
    # 同构先例：FSM 侧 `osc.break_confirm_bars`。置 1 = 退回单根判定。
    "range.box.break_confirm_bars": 2,
    # 质量门（**仅观测**：60 天标定显示 quality≥0.5 覆盖 745/758 ≈ 恒真、无判别力，
    # 故不作为 RANGE MR 硬门，仅用于日志/面板排序。见方案 §标定）
    "range.box.quality_min": 0.50,
    # 越界否决口径：off | sell | both
    #   标定（60 天 758 件）：SELL 破上沿 E[R]=+0.152(n=296) 低于 SELL 未破 +0.221(n=462)；
    #   而 BUY 破下沿 +0.278(n=183) **高于** BUY 未破 +0.168(n=575) ⇒ 效应**不对称**。
    # 非对称效应存在过拟合风险（单窗口/单品种），故**默认 off**，先观测累计样本再定。
    "range.box.require_unbroken": "off",
    # 【口径开关】默认 legacy ⇒ 生产行为逐字节不变；标定后再翻 box
    "range.box.width_source": "legacy",   # legacy | box（宽度过滤口径）
    "range.box.break_source": "legacy",   # legacy | box（突破熔断口径）
    "range.box.gate_scale": "slow",       # fast | slow（宽度门用哪个尺度）
    "range.box.publish": True,            # 发布 hcm:live:range_box:{symbol}
    # 发布 TTL（秒）。**必须 ≥ 2×bar 周期**：本键只在 `_produce_signal`（每根 M5 收盘，
    # 300s）刷新一次；若 TTL 取短值（如 15s，同 hexp 快照），键将在 95% 的时间内不存在
    # → 面板恒空。默认 600 = 2 根 M5（漏 1 根仍可见，漏 2 根自然过期）。
    "range.box.publish_ttl_sec": 600,
    # ── 上下限阈值：镜像 range_strategy.DEFAULTS（生产以 PG/Redis 为准）──
    "range.width_min_atr": 1.5,
    "range.width_max_atr": 6.0,
}

# 质量混合权重（**纯混合系数，非交易阈值**；刻意不做配置键以免配置面膨胀，
# 如需调参见方案 §8 待办 1）。各项 q∈[0,1]，hurst 缺失时该项剔除并重归一。
_QUALITY_W = {"width": 0.35, "touch": 0.25, "drift": 0.25, "hurst": 0.15}


# ── 配置读取（类型异常一律回落，绝不抛；与 range_strategy 同风格）──────
def _f(cfg, key: str) -> float:
    try:
        v = cfg.get(key, DEFAULTS.get(key)) if cfg else DEFAULTS.get(key)
        if v is None:
            return float(DEFAULTS.get(key, 0.0))
        return float(v)
    except (TypeError, ValueError):
        try:
            return float(DEFAULTS.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0


def _s(cfg, key: str) -> str:
    try:
        v = cfg.get(key, DEFAULTS.get(key)) if cfg else DEFAULTS.get(key)
        return str(v) if v is not None else str(DEFAULTS.get(key, ""))
    except Exception:  # noqa: BLE001
        return str(DEFAULTS.get(key, ""))


def _b(cfg, key: str) -> bool:
    """解析布尔：兼顾 ①字符串（'false' 不能 bool() 成 True）②None 回落 DEFAULTS。

    ① 同 range_strategy B11 修复口径，避免"配置写 false 却恒为真"的静默失效。
    ② 【2026-09-18 实盘抓到】调用方（scheduler）按 DEFAULTS 的键逐个读配置中心，
       键不存在时写入的是**显式 None**（不是"缺键"）⇒ `dict.get(key, default)` 返回
       None 而非 default ⇒ `bool(None)=False` 会把「默认开启」的开关静默关掉
       （实测表现：range.box.enabled 默认 True，但箱体恒 valid=False/reason="disabled"）。
       `_f`/`_s` 均已处理 None，`_b` 此前漏了 —— 此处补齐，三个读值函数口径一致。
    """
    try:
        v = cfg.get(key, DEFAULTS.get(key)) if cfg else DEFAULTS.get(key)
    except Exception:  # noqa: BLE001
        v = DEFAULTS.get(key)
    if v is None:
        v = DEFAULTS.get(key)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y", "t")
    return bool(v)


def _window_for(cfg, scale: str, symbol: Optional[str]) -> int:
    """取窗口：scale=fast → range.box.window(+.{symbol})；scale=slow → range.box.window_slow。"""
    if str(scale).lower() == "slow":
        return int(_f(cfg, "range.box.window_slow"))
    if symbol:
        try:
            _v = cfg.get(f"range.box.window.{symbol}") if cfg else None
            if _v is not None and str(_v).strip() != "":
                return int(float(_v))
        except (TypeError, ValueError):
            pass
    return int(_f(cfg, "range.box.window"))


# ── 数据模型 ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RangeBox:
    """箱体几何快照（不可变）。

    位置语义（两种都保留，避免下游各自截断）：
      pos         = (close-lower)/width，**未截断** → <0 破下沿、>1 破上沿（突破检测用）
      pos_clamped = 截断 [0,1]（与 hexp `pos_pct` 语义兼容）
      pos_signed  = (0.5-pos_clamped)*2 ∈[-1,1]（与 hexp `f_pos` 同构，可直接喂 dir_sum）
    """

    valid: bool = False
    reason: str = ""
    upper: float = 0.0
    lower: float = 0.0
    mid: float = 0.0
    width: float = 0.0
    width_atr: float = 0.0
    close: float = 0.0
    pos: float = 0.5
    pos_clamped: float = 0.5
    pos_signed: float = 0.0
    touches: int = 0
    cross_mid: int = 0
    drift_atr_per_bar: float = 0.0
    window: int = 0
    bands_mode: str = ""
    exclusive: bool = True
    atr: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """快照发布用（面板/归因）；三线 3 位、比率 4 位。"""
        return {
            "valid": bool(self.valid),
            "reason": self.reason,
            "upper": round(self.upper, 3),
            "lower": round(self.lower, 3),
            "mid": round(self.mid, 3),
            "width": round(self.width, 3),
            "width_atr": round(self.width_atr, 3),
            "close": round(self.close, 3),
            "pos": round(self.pos, 4),
            "pos_clamped": round(self.pos_clamped, 4),
            "pos_signed": round(self.pos_signed, 4),
            "touches": int(self.touches),
            "cross_mid": int(self.cross_mid),
            "drift_atr_per_bar": round(self.drift_atr_per_bar, 4),
            "window": int(self.window),
            "bands_mode": self.bands_mode,
            "exclusive": bool(self.exclusive),
            "atr": round(self.atr, 4),
        }


def _invalid(reason: str, window: int = 0, atr: float = 0.0,
             close: float = 0.0) -> RangeBox:
    return RangeBox(valid=False, reason=reason, window=window, atr=atr, close=close)


# ── 几何 ─────────────────────────────────────────────────────────────
def _slice(a: Any, window: int, exclusive: bool) -> Optional[np.ndarray]:
    """窗口切片。exclusive → arr[n-1-window : n-1]（与 compute_entry_box 一致）。

    数据不足返回 None（调用方转 invalid 箱体，绝不抛）。
    """
    try:
        arr = np.asarray(a, dtype=float)
    except (TypeError, ValueError):
        return None
    n = arr.size
    if window <= 0:
        return None
    if exclusive:
        if n < window + 1:
            return None
        return arr[n - 1 - window: n - 1]
    if n < window:
        return None
    return arr[n - window: n]


def compute_box(high: Any, low: Any, closes: Any, atr: float,
                cfg: Optional[dict] = None, *, scale: str = "fast",
                symbol: Optional[str] = None,
                bands_mode: Optional[str] = None) -> RangeBox:
    """计算箱体三线 + 宽度 + 位置 + 触碰/穿越/漂移。

    Args:
        high/low/closes: M5 序列（最近一根在末尾）。closes[-1] = 当前评估 bar 收盘。
        atr: 同周期 ATR（用于宽度/漂移/容差归一）。
        scale: "fast"（range.box.window）|"slow"（range.box.window_slow）。
        symbol: 品种级窗口覆盖（range.box.window.{symbol}）。
        bands_mode: 覆盖 `range.box.bands_mode`（"extremum"|"quantile"）。
            **用途**：突破判定需用**极值**边界（`range.box.break_bands_mode`），
            与宽度/位置用的 quantile 箱体分离 —— 见 DEFAULTS 注释。

    Returns:
        RangeBox；数据不足/退化/关闭时 valid=False（reason 说明原因）。
    """
    if not _b(cfg, "range.box.enabled"):
        return _invalid("disabled")
    win = _window_for(cfg, scale, symbol)
    _bm = str(bands_mode).strip().lower() if bands_mode else ""
    mode = _bm or _s(cfg, "range.box.bands_mode").strip().lower() or "quantile"
    excl = _b(cfg, "range.box.exclusive")
    try:
        seg_h = _slice(high, win, excl)
        seg_l = _slice(low, win, excl)
        seg_c = _slice(closes, win, excl)
        if seg_h is None or seg_l is None or seg_c is None:
            return _invalid("insufficient_data", win, float(atr or 0.0))
        if mode == "quantile":
            _qh = min(max(_f(cfg, "range.box.q_high"), 0.0), 1.0) * 100.0
            _ql = min(max(_f(cfg, "range.box.q_low"), 0.0), 1.0) * 100.0
            upper = float(np.percentile(seg_h, _qh))
            lower = float(np.percentile(seg_l, _ql))
        else:
            upper = float(np.max(seg_h))
            lower = float(np.min(seg_l))
    except Exception as exc:  # noqa: BLE001
        logger.warning("range_box compute failed (%s): %s", symbol or "-", exc)
        return _invalid("compute_error", win, float(atr or 0.0))
    if not (upper > lower):
        # 退化保护：分位异常/数据错乱 → 视为无效箱体，由调用方拒绝
        return _invalid("degenerate", win, float(atr or 0.0))

    _atr = float(atr or 0.0)
    width = upper - lower
    mid = 0.5 * (upper + lower)
    width_atr = (width / _atr) if _atr > 0 else 0.0
    try:
        close_v = float(np.asarray(closes, dtype=float)[-1])
    except Exception:  # noqa: BLE001
        close_v = 0.0
    pos = ((close_v - lower) / width) if width > 0 else 0.5
    pos_clamped = min(1.0, max(0.0, pos))
    pos_signed = (0.5 - pos_clamped) * 2.0

    # ── 触碰次数：上下沿带内 bar 数（带内 = 边界 ± wick×ATR）──
    touches = 0
    wick = _f(cfg, "range.box.wick_atr") * _atr
    try:
        touches = int(np.sum(seg_h >= (upper - wick)) + np.sum(seg_l <= (lower + wick)))
    except Exception:  # noqa: BLE001
        touches = 0

    # ── 穿越中值次数：真震荡证据（单边腿几乎不穿越）──
    cross_mid = 0
    try:
        _d = np.asarray(seg_c, dtype=float) - mid
        _d = _d[np.abs(_d) > 1e-12]
        if _d.size > 1:
            cross_mid = int(np.sum(np.sign(_d[1:]) != np.sign(_d[:-1])))
    except Exception:  # noqa: BLE001
        cross_mid = 0

    # ── 漂移：收盘线性回归斜率 / ATR（ATR/根）──
    drift = 0.0
    try:
        _n = seg_c.size
        if _n >= 3 and _atr > 0:
            _slope = float(np.polyfit(np.arange(_n, dtype=float),
                                      np.asarray(seg_c, dtype=float), 1)[0])
            drift = _slope / _atr
    except Exception:  # noqa: BLE001
        drift = 0.0

    return RangeBox(
        valid=True, reason="ok",
        upper=upper, lower=lower, mid=mid,
        width=width, width_atr=width_atr, close=close_v,
        pos=pos, pos_clamped=pos_clamped, pos_signed=pos_signed,
        touches=touches, cross_mid=cross_mid, drift_atr_per_bar=drift,
        window=win, bands_mode=mode, exclusive=excl, atr=_atr,
    )


def compute_scales(high: Any, low: Any, closes: Any, atr: float,
                   cfg: Optional[dict] = None, *,
                   symbol: Optional[str] = None) -> tuple[RangeBox, RangeBox]:
    """双尺度箱体：(fast, slow)。任一侧失败 → 该侧 valid=False（不影响另一侧）。"""
    fast = compute_box(high, low, closes, atr, cfg, scale="fast", symbol=symbol)
    slow = compute_box(high, low, closes, atr, cfg, scale="slow", symbol=symbol)
    return fast, slow


def gate_scale_box(boxes: tuple[RangeBox, RangeBox],
                   cfg: Optional[dict] = None) -> RangeBox:
    """按 range.box.gate_scale 取用于宽度门的尺度（fast|slow，默认 slow）。"""
    fast, slow = boxes
    return fast if _s(cfg, "range.box.gate_scale").strip().lower() == "fast" else slow


# ── 质量 / 门控 ───────────────────────────────────────────────────────
def quality(box: RangeBox, cfg: Optional[dict] = None, *,
            hurst: Optional[float] = None,
            er: Optional[float] = None) -> tuple[float, str]:
    """箱体质量 ∈[0,1] + 原因串；invalid 箱体直接 0.0。

    分项：宽度合适 / 边界触碰 / 漂移小 / 均值回归(hurst)。
    hurst 缺失 → 该项剔除并**重归一**（不用中值拉低，避免"无证据即扣分"）。
    """
    if not box.valid:
        return 0.0, f"invalid({box.reason})"
    wmin = _f(cfg, "range.width_min_atr")
    wmax = _f(cfg, "range.width_max_atr")
    if wmin <= 0:
        wmin = _f(cfg, "range.box.min_width_atr")
    w = box.width_atr
    if w <= 0:
        q_w = 0.0
    elif w < wmin:
        q_w = (w / wmin) if wmin > 0 else 0.0
    elif wmax > 0 and w > wmax:
        q_w = (wmax / w) if w > 0 else 0.0
    else:
        q_w = 1.0
    tmin = max(1.0, _f(cfg, "range.box.touch_min"))
    q_t = min(1.0, float(box.touches) / tmin)
    dmax = _f(cfg, "range.box.drift_max_atr")
    q_d = 1.0 - min(1.0, abs(box.drift_atr_per_bar) / dmax) if dmax > 0 else 1.0
    parts = {
        "width": (q_w, _QUALITY_W["width"]),
        "touch": (q_t, _QUALITY_W["touch"]),
        "drift": (q_d, _QUALITY_W["drift"]),
    }
    if hurst is not None:
        try:
            _h = float(hurst)
            q_h = 1.0 if _h <= 0.5 else max(0.0, 1.0 - (_h - 0.5) / 0.5)
        except (TypeError, ValueError):
            q_h = None  # type: ignore[assignment]
        if q_h is not None:
            parts["hurst"] = (q_h, _QUALITY_W["hurst"])
    _wtot = sum(wt for _, wt in parts.values())
    if _wtot <= 0:
        return 0.0, "no_components"
    score = sum(q * wt for q, wt in parts.values()) / _wtot
    reason = " ".join(f"{k}={v:.2f}" for k, (v, _) in parts.items())
    return max(0.0, min(1.0, score)), reason


def width_gate(box: RangeBox, cfg: Optional[dict] = None) -> tuple[bool, str]:
    """宽度门：width_atr ∈ [range.width_min_atr, range.width_max_atr]。

    与旧口径（50 根 inclusive 极值）**同键不同口径** ⇒ 切换时必须先离线重标定阈值
    （见方案 §6 复核 2：仅换口径不能解卡，慢箱 quantile 50 根仍 10.25 ATR > 6.0）。
    """
    if not box.valid:
        return False, f"invalid({box.reason})"
    wmin = _f(cfg, "range.width_min_atr")
    wmax = _f(cfg, "range.width_max_atr")
    w = box.width_atr
    if wmin > 0 and w < wmin:
        return False, f"too_narrow({w:.2f}<{wmin:.1f})"
    if wmax > 0 and w > wmax:
        return False, f"too_wide({w:.2f}>{wmax:.1f})"
    return True, f"ok({w:.2f})"


def scales_gate(fast: RangeBox, slow: RangeBox,
                cfg: Optional[dict] = None) -> tuple[bool, str]:
    """**双尺度宽度门**（60 天标定推荐口径）。

      下限 = 快箱 width_atr ≥ range.box.min_width_atr(1.0) —— "1.0ATR 止盈装得下"
      上限 = 慢箱 width_atr ≤ range.width_max_atr          —— "宽到失控 = 已非震荡"
      （慢箱下沿同时受 range.width_min_atr 约束，保持与既有键语义一致）

    为什么上下限用**不同尺度**：这是本方案的核心 ——
      · 快箱(12 根)回答"当前震荡**幅度**"，故只适合下限（TP 可行性）；
        标定显示快箱宽度几乎恒落在 [1.5,6]（521/758），单独做门 **无判别力**（≈不过滤）。
      · 慢箱(50 根)回答"是否**仍是**震荡"，其分箱才单调（1.5-3:+0.446 → ≥12:+0.034）。
    标定（60 天 758 件已了结，与不过滤基线 E[R]=+0.194 对比）：
      本门       n=616(81.3%) E[R]=+0.238 CI95=[+0.173,+0.303]
      当前生产   n= 53( 7.0%) E[R]=+0.415 CI95=[+0.218,+0.612]  ← 覆盖仅 7% = 通道事实死锁
    """
    if not fast.valid or not slow.valid:
        return False, (f"invalid(fast={fast.reason},slow={slow.reason})")
    wmin_f = _f(cfg, "range.box.min_width_atr")
    if wmin_f > 0 and fast.width_atr < wmin_f:
        return False, f"fast_too_narrow({fast.width_atr:.2f}<{wmin_f:.1f})"
    ok_s, why_s = width_gate(slow, cfg)
    if not ok_s:
        return False, f"slow_{why_s}"
    return True, f"ok(fast={fast.width_atr:.2f},slow={slow.width_atr:.2f})"


def unbroken_veto(box: RangeBox, direction: str,
                  cfg: Optional[dict] = None) -> tuple[bool, str]:
    """越界否决（可选，`range.box.require_unbroken` = off|sell|both，默认 off）。

    否决条件：要求"未破边界"的方向恰好已越界（BUY 破下沿 / SELL 破上沿）。
    标定显示该效应**不对称**（SELL 侧有利、BUY 侧相反）⇒ 默认 off，先观测。
    """
    mode = _s(cfg, "range.box.require_unbroken").strip().lower() or "off"
    if mode not in ("sell", "both"):
        return False, "off"
    if not box.valid:
        return False, f"invalid({box.reason})"
    _dir = str(direction or "").upper()
    if _dir == "SELL" and box.pos > 1.0:
        return True, f"unbroken_veto(SELL破上沿 pos={box.pos:.2f})"
    if mode == "both" and _dir == "BUY" and box.pos < 0.0:
        return True, f"unbroken_veto(BUY破下沿 pos={box.pos:.2f})"
    return False, "ok"


def break_check(box: RangeBox, close: float, cfg: Optional[dict] = None
                ) -> tuple[bool, str]:
    """突破判定：收盘越过边界 ± range.box.break_buf_atr×ATR → broken。

    exclusive 口径（箱体边界不含当前 bar）⇒ 极值信号本身创新高**不会**自我误封，
    这正是旧 break_guard 注释里手工规避的坑，现由口径结构性保证。
    """
    if not box.valid:
        return False, f"invalid({box.reason})"
    try:
        _c = float(close)
    except (TypeError, ValueError):
        return False, "bad_close"
    if _c <= 0:
        return False, "bad_close"
    buf = _f(cfg, "range.box.break_buf_atr") * float(box.atr or 0.0)
    if _c > (box.upper + buf):
        return True, f"break_up(close={_c:.2f}>{box.upper + buf:.2f})"
    if _c < (box.lower - buf):
        return True, f"break_down(close={_c:.2f}<{box.lower - buf:.2f})"
    return False, "in_box"


def break_streak_update(box: RangeBox, close: float, cfg: Optional[dict] = None, *,
                        prev_side: str = "", prev_streak: int = 0
                        ) -> tuple[bool, str, str, int]:
    """**连续越界确认**（治"回踩 vs 突破"之分）。

    返回 `(confirmed, why, new_side, new_streak)`；调用方须把 `new_side/new_streak`
    存回 per-symbol 状态，且**每根 bar 只调用一次**（bar 内重入须靠 bar 键去重，
    否则"连续 N 根"会退化成"连续 N 次调用"）。

    【为什么需要】单根收盘越界 ≠ 突破：
      · RSI 极值（= 本策略的**入场条件**）天然发生在边界附近；
      · 单根越界就熔断 → 冷却 N 根 → 机会在**产生的那一根**即被作废。
      实测（3,000 根 M5 / 258 件 RSI 极值）：quantile 边界单根判定下，82.9% 的事件
      当根被判破沿，"宽度门 ∧ 未破沿"存活率仅 **15.5%**；
      改用极值边界（A）+ 连续 2 根确认（B）→ **66.3%**。
    与 FSM 侧 `osc.break_confirm_bars` 同构（同一语义不在两处各写一份口径）。
    """
    if not box.valid:
        return False, f"invalid({box.reason})", "", 0
    try:
        _c = float(close)
    except (TypeError, ValueError):
        return False, "bad_close", "", 0
    if _c <= 0:
        return False, "bad_close", "", 0
    buf = _f(cfg, "range.box.break_buf_atr") * float(box.atr or 0.0)
    if _c > (box.upper + buf):
        side = "up"
    elif _c < (box.lower - buf):
        side = "down"
    else:
        return False, "in_box", "", 0          # 未越界 → 计数清零
    need = max(1, int(_f(cfg, "range.box.break_confirm_bars")))
    streak = (int(prev_streak) + 1) if str(prev_side) == side else 1
    _lim = (box.upper + buf) if side == "up" else (box.lower - buf)
    if streak >= need:
        return True, (f"break_{side}(close={_c:.2f} vs {_lim:.2f}, "
                      f"streak={streak}/{need})"), side, streak
    return False, f"break_{side}_pending(streak={streak}/{need})", side, streak


def confirm_direction(box: RangeBox, direction: str,
                      cfg: Optional[dict] = None) -> tuple[bool, str]:
    """位置一致性校验（**不产方向**，只回答"该方向与箱体位置是否矛盾"）。

    BUY  需 close <= lower + edge_tol×ATR（贴下沿）
    SELL 需 close >= upper - edge_tol×ATR（贴上沿）
    方向本身仍由 range_strategy.mr_direction 单一裁决 ⇒ 不新增第 7 套口径/双真源。
    """
    if not box.valid:
        return False, f"invalid({box.reason})"
    _dir = str(direction or "").upper()
    if _dir not in ("BUY", "SELL"):
        return False, "no_direction"
    tol = _f(cfg, "range.box.edge_tol_atr") * float(box.atr or 0.0)
    _c = float(box.close or 0.0)
    if _dir == "BUY":
        _lim = box.lower + tol
        return (_c <= _lim), (f"buy@pos={box.pos:.2f}(<= {_lim:.2f})" if _c <= _lim
                              else f"buy_conflict(pos={box.pos:.2f} close={_c:.2f}>{_lim:.2f})")
    _lim = box.upper - tol
    return (_c >= _lim), (f"sell@pos={box.pos:.2f}(>= {_lim:.2f})" if _c >= _lim
                          else f"sell_conflict(pos={box.pos:.2f} close={_c:.2f}<{_lim:.2f})")


def box_cfg(cfg: Optional[dict] = None) -> dict:
    """便捷：把 cfg 中 range.box.* / range.width_* 抽成独立 dict（缺失回落 DEFAULTS）。

    供调用方先取一次配置、随后多次 compute（避免逐键重复读取）。
    """
    out: dict = {}
    for k in list(DEFAULTS):
        if k.startswith("range.box.") or k.startswith("range.width_"):
            try:
                out[k] = cfg.get(k) if cfg else None
            except Exception:  # noqa: BLE001
                out[k] = None
    return out
