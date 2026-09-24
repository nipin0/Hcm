"""feature_drift.py — 特征分布漂移监控（PSI）**唯一实现点**。

为什么放在中立位置（而非 `lgbm_fsm/drift.py`）
──────────────────────────────────────────────
本能力要被**两条链路**共用：
  · 生产链路：`scheduler._run_shadow_state` 的低频漂移采样（本文件的新增接入点）；
  · 研究链路：`tools/eval_lgbm_fsm_vs_state.py` 的前后半段漂移体检。
若各写一份，就是"同一规则两份实现"（铁律第十三章，本仓库已多次踩坑）。
故实现落在 `signal_tower/feature_drift.py`，`lgbm_fsm/drift.py` 只做 **re-export**
（保持既有导入路径与单测不破）。

PSI 的两个**必须遵守**的实现要求
────────────────────────────────
① **两侧共用同一套箱边界**（只从基准分布导出）。
   若 `base`/`new` 各按自身分位切箱，结果无意义：
   箱数或箱标签不一致时会因兜底概率项 `1e-8` 使 `log(p/1e-8)` 爆掉（实测 PSI=16.1，
   而正确值仅 2.07）；若标签恰好一致，则退化为"两个均匀直方图"，PSI≈0 ⇒ **漂移被掩盖**。
② 越界样本并入首/末箱（`edges[0]=-inf, edges[-1]=+inf`），
   否则极值样本被静默丢弃 —— 而那正是漂移最先显现的地方。

配置键（经 `Config` duck-typing 读取，缺失一律回退默认；**任何失败不得影响调用方**）：
  state.drift.enabled / every_bars / window_bars / ref_kind / psi_warn / psi_block / bins
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 默认值（生产以配置中心为准；此处仅兜底，与 lgbm_fsm.config 独立）──
DEFAULTS: dict = {
    # 总开关：**默认关闭**（零行为变更；回滚 = 置 false）
    "state.drift.enabled": False,
    # 采样频率：每 N 根 bar 计算一次（0/1 = 每根）。默认 12 = 每小时一次（M5）。
    # 为什么降频：PSI 需在**多个特征 × 数百个样本**上做分位分箱，
    #   逐 bar 全量计算会挤占信号塔的实时预算（FSM 求值实测中位 1.6s）。
    "state.drift.every_bars": 12,
    # 采样窗口根数（基准与当前各取这么多根）。**需要 2×window 根可用 K 线**。
    # ⚠ 默认 180（不是 480）的原因：`scheduler._run_shadow_state` 的 K 线序列由
    #   `_fetch_klines(..., limit=max(400, min_bars*2))` 提供 ⇒ 实际约 **400 根**
    #   （`scheduler.py:2315`）。若 window=480 则需 960 根 ⇒ 永远取不到 ⇒ 采样恒被跳过
    #   （**且原先只在 debug 级留痕 ⇒ "配了但没生效"完全不可见**）。
    #   180×2 = 360 根，恰好落在可用区间内；180 样本按 10 箱分 ≈ 18 个/箱（偏少但可用）。
    #   若要更大的统计窗口，必须在 `_maybe_sample_feature_drift` 内**另行补取 K 线**，
    #   不能只调大本键（否则又变成静默跳过）。
    "state.drift.window_bars": 180,
    # 基准来源：
    #   "adaptive"（默认）—— 用**前一个窗口**当基准（前后窗口自比较）。
    #       优点：零外部依赖、立即可用、能捕捉"近期分布突变"；
    #       缺点：测的是**相对近期**的漂移，不是"相对训练集"的漂移。
    #   "file" —— 读模型目录下的 `lgbm_state_{tf}_drift_base.npz`（相对训练集）。
    #       更准，但需先由离线脚本产出基准文件；缺失时**自动回退 adaptive**。
    "state.drift.ref_kind": "adaptive",
    # 模型目录（ref_kind=file 时用于查找基准；与 state.model_dir 同源）
    "state.model_dir": "/app/review_models",
    # PSI 阈值：<0.1 稳定；0.1~0.2 轻微漂移（告警）；>0.2 严重（可触发关闭）
    "state.drift.psi_warn": 0.1,
    "state.drift.psi_block": 0.2,
    "state.drift.bins": 10,
    "state.drift.min_bin_frac": 1e-6,
    # 【高危】超标后是否**自动关闭状态机信号**（state.enabled ← false）。
    # **默认 false**。启用前必须同时满足（对齐 state_infer.py:59-63 的验收门范例）：
    #   ① 连续 ≥3 次采样 verdict=block（避免单次噪声误关）；
    #   ② 该期间 `market_state_log` 复算显示"信号质量确实下降"；
    #   ③ 存在人工恢复路径（不能自动开回来）。
    "state.drift.auto_disable": False,
    "state.drift.block_streak": 3,
    # 【2026-09-23】PSI **排除列**（逗号分隔的特征名）—— 被排除的列**不参与**
    # max_psi / max_col / verdict 判定（其 PSI 仍在报表的 `psi_excluded` 里可见）。
    #
    # 为什么需要（本轮实测，非推测）：
    #   ① **离散 / 计数类**（`new_high_cnt`、`new_low_cnt`）：`_hist_with_edges` 用
    #      `np.unique(np.nanquantile(base, linspace(0,1,bins+1)))` 从基准导箱边界，
    #      而小整数分布会产生**大量重复分位点** ⇒ `unique` 后箱数骤减（远小于 bins）
    #      ⇒ PSI 估计不稳且**系统性偏高**。实测：`new_high_cnt` 的基准/当前
    #      **中位数完全相同**（0.100 / 0.100），PSI 却达 **6.243**（当轮 max_col）。
    #   ② **绝对价格尺度**（`atr_14`）：`STATE_FEATURE_COLS` 27 维中**唯一**非比值列，
    #      其"漂移"主要反映**金价水平位移**，不代表模型输入失效。
    #   ⇒ 二者会**主导 max_col**，把真正有信息量的漂移（如 `win_high_dist_atr` 3.6）
    #     挤到后面，使 verdict 长期裁定在噪声列上。
    #
    # 语义：`""`（默认，**空 = 不排除任何列**）⇒ 逐位保持既有行为，可一行回滚。
    # 取值形如 `"new_high_cnt,new_low_cnt,atr_14"`（首尾空格容错）。
    "state.drift.exclude_cols": "",
}


class DictCfg:
    """把**已读出的配置字典**适配成 `cfg` 接口（`num/flag/raw`）。

    为什么需要：`scheduler` 侧配置是 async 读取的（`self._config.get_*`），
    而本模块刻意保持**同步纯计算**。调用方按 30s 热重载把值读进 dict，
    再用本适配器传入 ⇒ 采样路径零 await、零额外 IO，且与热重载周期天然一致。
    """

    __slots__ = ("_d",)

    def __init__(self, values: Optional[dict] = None) -> None:
        self._d = dict(values or {})

    def raw(self, key: str) -> Any:
        return self._d.get(key, DEFAULTS.get(key))

    def num(self, key: str, default: Optional[float] = None) -> Optional[float]:
        v = self._d.get(key, DEFAULTS.get(key))
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def flag(self, key: str) -> bool:
        v = self._d.get(key, DEFAULTS.get(key))
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        return str(v).strip().lower() in ("1", "true", "yes", "on")


def _cfg_num(cfg: Any, key: str, default: float) -> float:
    if cfg is None:
        return float(DEFAULTS.get(key, default))
    try:
        if hasattr(cfg, "num"):
            v = cfg.num(key, None)
        elif hasattr(cfg, "get_float"):
            v = cfg.get_float(key, None)
        else:
            v = None
        return float(v) if v is not None else float(DEFAULTS.get(key, default))
    except Exception:  # noqa: BLE001
        return float(DEFAULTS.get(key, default))


def _cfg_str(cfg: Any, key: str, default: str) -> str:
    if cfg is None:
        return str(DEFAULTS.get(key, default))
    try:
        if hasattr(cfg, "raw"):
            v = cfg.raw(key)
        elif hasattr(cfg, "get"):
            v = cfg.get(key, None)
        else:
            v = None
        return str(v) if v is not None else str(DEFAULTS.get(key, default))
    except Exception:  # noqa: BLE001
        return str(DEFAULTS.get(key, default))


def _cfg_flag(cfg: Any, key: str, default: bool = False) -> bool:
    if cfg is None:
        return bool(DEFAULTS.get(key, default))
    try:
        if hasattr(cfg, "flag"):
            return bool(cfg.flag(key))
        if hasattr(cfg, "get_bool"):
            v = cfg.get_bool(key, None)
            return bool(v) if v is not None else bool(DEFAULTS.get(key, default))
        if hasattr(cfg, "get"):
            v = cfg.get(key, None)
            if v is None:
                return bool(DEFAULTS.get(key, default))
            return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        pass
    return bool(DEFAULTS.get(key, default))


def _hist_with_edges(base: np.ndarray, new: np.ndarray, bins: int,
                     min_frac: float) -> tuple:
    """返回 `(p_base, p_new, edges)` —— 两侧**共用同一边界**（要求 ①）。"""
    edges = np.unique(np.nanquantile(base, np.linspace(0.0, 1.0, bins + 1)))
    if edges.size < 2:                      # 基准近乎常量 → 无法分箱
        return None, None, None
    edges[0] = -np.inf
    edges[-1] = np.inf

    def _one(x: np.ndarray) -> np.ndarray:
        cats = pd.cut(x, bins=edges, include_lowest=True, labels=False)
        idx = np.asarray(cats, dtype=float)
        # 越界样本并入首/末箱（要求 ②）
        idx = np.where(np.isnan(idx) & (x <= edges[1]), 0.0, idx)
        idx = np.where(np.isnan(idx) & (x > edges[1]), float(edges.size - 2), idx)
        idx = idx[~np.isnan(idx)].astype(int)
        cnt = np.bincount(idx, minlength=edges.size - 1).astype(float)
        tot = cnt.sum()
        if tot <= 0:
            return np.full(edges.size - 1, 1.0 / (edges.size - 1))
        return np.clip(cnt / tot, min_frac, None)

    return _one(base), _one(new), edges


def calculate_psi(base_array, new_array, bins: int = 10,
                  min_bin_frac: float = 1e-6) -> float:
    """PSI（**同一套箱边界**）。返回 `np.nan` = 不可计算（基准退化/样本不足）。"""
    base = np.asarray(base_array, dtype=float)
    new = np.asarray(new_array, dtype=float)
    base = base[np.isfinite(base)]
    new = new[np.isfinite(new)]
    if base.size < max(2, bins) or new.size < 1:
        return float("nan")

    p_base, p_new, _ = _hist_with_edges(base, new, bins, min_bin_frac)
    if p_base is None:
        return float("nan")
    p_base = p_base / p_base.sum()
    p_new = p_new / p_new.sum()
    return float(np.sum((p_base - p_new) * np.log(p_base / p_new)))


def feature_psi_report(
    reference: np.ndarray,
    current: np.ndarray,
    cols: Sequence[str],
    cfg: Any = None,
) -> dict:
    """逐特征 PSI 报表。

    `state.drift.exclude_cols` 列出的列**不参与** max/verdict（理由见 DEFAULTS
    同名键注释），但其 PSI 仍在 `psi_excluded` 中返回，便于审计与面板展示。

    Returns:
        dict(psi={col: value}, psi_excluded={col: value}, excluded_cols=[...],
             max_psi, max_col, warned, blocked, verdict)
        `psi` 只含**参与判定**的列；`verdict ∈ {"ok","warn","block","unknown"}`
    """
    ref = np.asarray(reference, dtype=float)
    cur = np.asarray(current, dtype=float)
    if ref.ndim != 2 or cur.ndim != 2 or ref.shape[1] != cur.shape[1]:
        raise ValueError("reference/current 须为同宽二维数组")
    bins = int(_cfg_num(cfg, "state.drift.bins", 10))
    min_frac = _cfg_num(cfg, "state.drift.min_bin_frac", 1e-6)
    warn = _cfg_num(cfg, "state.drift.psi_warn", 0.1)
    block = _cfg_num(cfg, "state.drift.psi_block", 0.2)
    # 排除集为空 ⇒ 与既有行为**逐位一致**（默认 ""，可一行回滚）
    excl = {s.strip() for s in
            str(_cfg_str(cfg, "state.drift.exclude_cols", "") or "").split(",")
            if s.strip()}

    psi: dict = {}
    psi_excluded: dict = {}
    for j, name in enumerate(cols):
        v = calculate_psi(ref[:, j], cur[:, j], bins, min_frac)
        if str(name) in excl:
            psi_excluded[str(name)] = v      # 仍计算并留痕（不参与判定）
        else:
            psi[str(name)] = v

    finite = {k: v for k, v in psi.items() if np.isfinite(v)}
    max_col = max(finite, key=finite.get) if finite else ""
    max_psi = float(finite[max_col]) if finite else float("nan")

    verdict = "unknown"
    if np.isfinite(max_psi):
        verdict = "block" if max_psi > block else ("warn" if max_psi > warn else "ok")
    return {
        "psi": psi,
        "psi_excluded": psi_excluded,
        "excluded_cols": sorted(excl),
        "max_psi": max_psi,
        "max_col": max_col,
        "warned": verdict == "warn",
        "blocked": verdict == "block",
        "verdict": verdict,
    }


def should_disable_signals(report: dict) -> bool:
    """验收门：PSI 超标 ⇒ 应关闭信号。**仅返回判定**，是否真的关闭由调用方按配置决定
    （`state.drift.auto_disable` + `block_streak`，默认关闭）。"""
    return bool(report.get("blocked", False))


def drift_enabled(cfg: Any) -> bool:
    return _cfg_flag(cfg, "state.drift.enabled", False)


def sample_due(bar_count: int, cfg: Any) -> bool:
    """是否到达采样点（降频）。`every_bars<=1` ⇒ 每根都采。"""
    every = int(_cfg_num(cfg, "state.drift.every_bars", 12))
    if every <= 1:
        return True
    return int(bar_count) % every == 0


def window_bars(cfg: Any) -> int:
    return max(60, int(_cfg_num(cfg, "state.drift.window_bars", 480)))


def ref_kind(cfg: Any) -> str:
    return _cfg_str(cfg, "state.drift.ref_kind", "adaptive").strip().lower()


def _tf_seconds(tf: str) -> int:
    """周期 → 秒（用于无状态槽位判定）。未知周期回退 M5。"""
    t = str(tf or "M5").strip().upper()
    if t.startswith("M"):
        try:
            return max(1, int(t[1:])) * 60
        except ValueError:
            return 300
    if t.startswith("H"):
        try:
            return max(1, int(t[1:])) * 3600
        except ValueError:
            return 3600
    return 300


def slot_due(bar_open_epoch_s: int, tf: str, cfg: Any) -> bool:
    """**无状态**采样时点判定：`bar_epoch % (tf_sec × every_bars) == 0`。

    为什么不用"每 N 根计数一次"：进程重启 / bar 重放 / live_override 的 bar 内重复调用
    都会让计数器漂移，且需要额外的持久状态（本仓库多次踩过"内存计数与真实 bar 不同步"）。
    槽位判定是**纯函数**：同一 bar 无论调用几次，结论一致 ⇒ 天然幂等。
    """
    every = max(1, int(_cfg_num(cfg, "state.drift.every_bars", 12)))
    period = _tf_seconds(tf) * every
    try:
        return int(bar_open_epoch_s) % period == 0
    except Exception:  # noqa: BLE001
        return False


def sample_feature_windows(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_epoch_s: Any,
    cols: Sequence[str],
    window: int,
    params: Optional[dict] = None,
) -> tuple:
    """取序列末尾 `2*window` 根，逐根算特征并切成 (ref, cur) 两段。

    复用 `state_features.compute_features_at`（**单一真值**：与推理侧同一口径），
    故 PSI 测的是"模型真正吃进去的那组数值"的分布漂移 —— 而不是另算一套近似指标。

    Returns:
        `(ref, cur)`（形状 `(m, k)`）或 `(None, None)`（数据不足 / 全为无效行）。
    """
    try:
        from .state_features import (  # noqa: WPS433 - 同包内，避免顶层循环导入
            DEFAULT_PARAMS,
            compute_features_at,
            compute_indicators,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[feature_drift] state_features 不可用：%s", exc)
        return None, None

    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    need = int(window) * 2
    if n < need:
        # 【可见性】不能用 DEBUG/INFO —— 本仓库已多次因"配置了≠生效"且日志级别过低
        # 而误判（见 scheduler.py:2529-2535：整链降级只有 DEBUG 日志）。
        # 生产 `signal_tower.*` 的 INFO 未必输出（实测 scheduler 的 INFO 不可见），
        # 故失败路径一律用 **WARNING**。
        logger.warning("[feature_drift] K 线不足：需 %d 根（2×window=%d），实得 %d → 跳过采样",
                       need, int(window), n)
        return None, None

    p = dict(DEFAULT_PARAMS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    ind = compute_indicators(high, low, close, p)

    start = n - need
    rows: list = []
    keep_idx: list = []
    col_list = [str(c) for c in cols]
    for i in range(start, n):
        f = compute_features_at(i, high, low, close, ind, open_epoch_s, p)
        if f is None:
            continue
        try:
            vec = [float(f[c]) for c in col_list]
        except (KeyError, TypeError, ValueError):
            continue
        if not np.all(np.isfinite(vec)):
            continue
        rows.append(vec)
        keep_idx.append(i)

    if len(rows) < 2 * max(20, int(window) // 4):
        return None, None
    arr = np.asarray(rows, dtype=float)
    mid = arr.shape[0] // 2
    ref, cur = arr[:mid], arr[mid:]
    if ref.shape[0] < 20 or cur.shape[0] < 20:
        return None, None
    return ref, cur


def sample_and_report(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_epoch_s: Any,
    cols: Sequence[str],
    cfg: Any,
    *,
    tf: str = "M5",
    params: Optional[dict] = None,
) -> Optional[dict]:
    """采样 + 出报表（**纯计算，无 IO**）。

    基准来源（`state.drift.ref_kind`）：
      · `adaptive`：前 `window` 根 vs 后 `window` 根（零外部依赖）；
      · `file`：模型目录的基准 npz vs 后 `window` 根；文件缺失/列不匹配 → **自动回退 adaptive**。

    Returns:
        `None`（不可算：数据不足 / 基准退化）；否则
        dict(ref_kind, window_bars, n_ref, n_cur, max_psi, max_col, verdict,
             blocked, warned, psi, disable_hint)
        `disable_hint` = 是否达到"应关闭信号"的判定（**仍需调用方按 auto_disable 决定**）。
    """
    col_list = [str(c) for c in cols]
    w = window_bars(cfg)
    ref, cur = sample_feature_windows(high, low, close, open_epoch_s,
                                      col_list, w, params)
    kind = ref_kind(cfg)

    if kind == "file":
        ref_file = load_reference_from_file(
            _cfg_str(cfg, "state.model_dir", "/app/review_models"), tf, col_list)
        # `cur` 已是**后半 window 根**（与 adaptive 分支同一取法）⇒ 直接复用，
        # 不另取短窗口：曾错用 `w//2`(=90) 导致当前段样本减半、分箱噪声放大、
        # PSI 系统性偏高（实测 n_cur=90 → PSI 7.56，与标定用的 180 不同口径）。
        if ref_file is not None and cur is not None and cur.shape[1] == ref_file.shape[1]:
            ref = ref_file
        else:
            kind = "adaptive"          # 基准/当前段不可用 → 回退

    if ref is None or cur is None:
        return None
    try:
        rep = feature_psi_report(ref, cur, col_list, cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[feature_drift] PSI 计算失败：%s", exc)
        return None

    return {
        "ref_kind": kind,
        "window_bars": int(w),
        "n_ref": int(ref.shape[0]),
        "n_cur": int(cur.shape[0]),
        "max_psi": rep["max_psi"],
        "max_col": rep["max_col"],
        "verdict": rep["verdict"],
        "blocked": bool(rep["blocked"]),
        "warned": bool(rep["warned"]),
        # 落库的逐列 PSI = **参与判定列 ∪ 被排除列** —— 保持字段齐全：
        # 前端/审计按列名取值，少列会让面板出现空白（"改了配置面板就空了"）。
        # 判定（max_col/verdict）**只用** `rep["psi"]`，与落库内容无关。
        "psi": {**rep["psi"], **rep.get("psi_excluded", {})},
        "psi_excluded": rep.get("psi_excluded", {}),
        "excluded_cols": rep.get("excluded_cols", []),
        "disable_hint": should_disable_signals(rep),
    }


def load_reference_from_file(model_dir: str, tf: str,
                             cols: Sequence[str]) -> Optional[np.ndarray]:
    """尝试读取离线产出的基准分布 `lgbm_state_{TF}_drift_base.npz`。

    文件缺失/列不匹配/解析失败 → 返回 None（调用方**回退 adaptive**，绝不抛错）。
    npz 需含 `X`（形状 `(m, k)`）与 `cols`（列名数组）。
    """
    import os

    if not model_dir:
        return None
    path = os.path.join(model_dir, f"lgbm_state_{str(tf).upper()}_drift_base.npz")
    if not os.path.exists(path):
        return None
    try:
        with np.load(path, allow_pickle=False) as z:
            X = np.asarray(z["X"], dtype=float)
            saved = [str(c) for c in z["cols"]] if "cols" in z else []
        if saved and saved != list(cols):
            logger.warning("[feature_drift] %s 基准列不匹配（saved=%d want=%d）→ 回退 adaptive",
                           path, len(saved), len(cols))
            return None
        if X.ndim != 2 or X.shape[1] != len(cols):
            return None
        return X
    except Exception as exc:  # noqa: BLE001
        logger.warning("[feature_drift] 基准读取失败 %s：%s → 回退 adaptive", path, exc)
        return None
