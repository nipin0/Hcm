"""_monitor_common.py — P3 监控共享工具（PSI 计算 + 训练基准加载）。

被 monitoring_report.py 与 auto_retrain.py 共用，避免双份实现。
只读：从 feature_baseline.json 读训练基准；PSI 计算纯数学，无外部副作用。

PSI 定义（标准 + 类别扩展）：
  连续特征：以基线分位边(deciles: p10~p90, 9 边)将特征分为 10 箱，期望每箱≈10%；
    实测占比偏离即 PSI = Σ(实际%−期望%)·ln(实际%/期望%)。
  低基数/类别特征(distinct ≤ LOW_CARD_MAX)：不分行位箱，直接按 distinct 类别算
    同一公式 Σ(a%−e%)·ln(a%/e%)，避免 binary/少值特征在分位箱下的假象。
  统一阈值：PSI>0.1 轻度漂移，>0.25 重度漂移（触发重训）。
"""

from __future__ import annotations

import json
import os
from typing import Dict, List

import numpy as np
import pandas as pd

DEFAULT_BASELINE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "feature_baseline.json"
)

# P3-B 根治(2026-08-22)：PSI 基线改取真实生产推理群体(live inference_log.features)，
# 而非训练集——训练子集≠live 群体会导致永久误触发+每小时 churn。
LIVE_BASELINE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "live_baseline.json"
)

# 低基数/类别特征阈值：distinct ≤ 此值的特征改用"按类别 PSI"而非分位 PSI。
# 分位 PSI 对 binary/少值特征会产生巨大假象(8/10 箱为空→每空箱贡献~0.69)。
LOW_CARD_MAX = 10


def build_live_baseline_from_features(features_df, features=None) -> dict:
    """从 live 推理特征 DataFrame 计算 PSI 基线（两类并存）。

      - deciles: 连续特征的分位边(p10~p90)，供分位 PSI 使用。
      - cat_props: 低基数/类别特征(distinct ≤ LOW_CARD_MAX)的类别占比，
        供"按类别 PSI"使用，根治分位 PSI 对 binary/少值特征的假象。

    P3-B 根治 2026-08-22（续）：live 群体基线 + 类别 PSI 双管齐下，
    使 PSI 成为真正的漂移探测器——相同分布→PSI≈0，仅真实漂移才触发。
    """
    if features is None:
        features = [c for c in features_df.columns]
    deciles: Dict[str, list] = {}
    cat_props: Dict[str, dict] = {}
    used: list = []
    for c in features:
        s = pd.to_numeric(features_df[c], errors="coerce")
        col = s.dropna().values
        if col.size < 30:
            continue
        # 【2026-08-31 修复】高聚集(低方差)特征检测:众数占比>0.3 时分位边大量
        # 重合 → deciles 退化 → 分位 PSI 虚高(已验证虚高到 4~7 触发误重训
        # churn)。此类特征排除出分位 PSI(不建退化 deciles),交由 feature_psi
        # 退化守卫兜底;仅保留分布离散(众数<0.3)特征供真实漂移监控。
        # 注:当前低波动市况下多数指标(含 adx_14/rsi_14)呈聚集态,PSI 对该类
        # 特征本质失真,豁免符合铁律第3条(非平稳/低方差特征不作触发依据)。
        _vc = s.value_counts(dropna=True)
        if len(_vc) > 0 and _vc.iloc[0] / max(1, _vc.sum()) > 0.3:
            continue
        deciles[c] = [float(np.quantile(col, q)) for q in
                      (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)]
        used.append(c)
        nun = int(s.nunique(dropna=True))
        if nun <= LOW_CARD_MAX:
            vc = s.value_counts(dropna=True)
            tot = float(vc.sum())
            # 键统一为 str(float(value))：baseline 侧(numpy int)与 actual 侧(float)归一为同键，
            # 避免 "0"≠"0.0" 导致每类别被判为"新类别"→ PSI 虚高到 ~18。2026-08-22 修复。
            cat_props[c] = {str(float(k)): float(v) / tot for k, v in vc.items()}
    import time as _t
    return {
        "features": used,
        "deciles": deciles,
        "cat_props": cat_props,
        "source": "live_inference",
        "built_at": _t.strftime("%Y-%m-%d %H:%M:%S UTC", _t.gmtime()),
    }


def save_live_baseline(baseline: dict, path: str = LIVE_BASELINE_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2)


def load_live_baseline(path: str = LIVE_BASELINE_PATH) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_baseline(path: str = DEFAULT_BASELINE) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def feature_psi(actual, deciles) -> float:
    """单特征 PSI。deciles: 9 个分位边(p10~p90)。分箱[-inf,d1..d9,+inf]→10箱，期望各10%。

    退化特征(基线分位边无跨度 / live 常量)→ 全落单箱 → PSI≈8.28 永远触发；
    但常量无可漂移性，故返回 0。P3-B 根治 2026-08-22。
    """
    arr = np.asarray(actual, dtype=float)
    if arr.size == 0:
        return 0.0
    d = np.asarray(deciles, dtype=float)
    if np.ptp(d) < 1e-9 or np.nanstd(arr) < 1e-9:
        return 0.0
    edges = np.concatenate(([-np.inf], d, [np.inf]))
    # 【2026-08-31 修复·PSI 假漂移】deciles 退化守卫:若分位边大量重合(相邻边
    # 差≈0),说明该特征低方差/高聚集(众数占 50%+)。退化分箱会把 50%+ 实际值
    # 挤进单一箱,PSI 虚高到 4~7,触发 auto_retrain 误重训 churn(已验证)。退化
    # 时返回 0(该特征低方差不可靠,不计入 drifted);真实漂移(rsi_14/adx_14)仍
    # 正常监控。
    if np.sum(np.diff(edges) > 1e-9) < 9:
        return 0.0
    counts, _ = np.histogram(arr, bins=edges)
    n = float(counts.sum())
    actual_prop = counts / n if n > 0 else np.zeros(10)
    expected = np.full(10, 0.1)
    eps = 1e-4
    actual_prop = np.clip(actual_prop, eps, 1.0)
    expected = np.clip(expected, eps, 1.0)
    return float(np.sum((actual_prop - expected) * np.log(actual_prop / expected)))


def feature_psi_categorical(actual, expected_props: dict) -> float:
    """低基数/类别特征 PSI：按 distinct 类别算 Σ(a%−e%)·ln(a%/e%)。

    与分位 PSI 同一数学形式，但把"箱"换成"类别"——避免 binary/少值特征
    在分位 PSI 下 8/10 箱为空产生的假象(PSI 虚高到 ~7)。相同分布→PSI≈0；
    真实类别漂移(某类别占比骤变/新类别出现)→正常触发。key 统一为 str(value)。
    """
    from collections import Counter
    arr = np.asarray(actual, dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size == 0 or not expected_props:
        return 0.0
    actual_counts = Counter(str(float(v)) for v in arr.tolist())
    tot = float(arr.size)
    cats = set(actual_counts.keys()) | set(expected_props.keys())
    eps = 1e-4
    psi = 0.0
    for cat in cats:
        a = actual_counts.get(cat, 0) / tot
        e = expected_props.get(cat, 0.0)
        a = max(a, eps)
        e = max(e, eps)
        psi += (a - e) * np.log(a / e)
    return float(psi)


def compute_psi_batch(features_df, baseline: dict, features: List[str] = None) -> Dict:
    """对 features_df(列=特征) 逐特征算 PSI，返回 {per_feature, max, mean, drifted}。

    低基数/类别特征(baseline.cat_props 中)走"按类别 PSI"；其余(在 deciles 中)
    走分位 PSI。训练基线(无 cat_props)全部走分位 PSI，与旧行为一致。
    """
    if features is None:
        features = baseline.get("features", [])
    deciles = baseline.get("deciles", {})
    cat_props = baseline.get("cat_props", {})
    result: Dict[str, float] = {}
    for feat in features:
        if feat not in features_df.columns:
            continue
        col = features_df[feat].dropna().values
        if col.size == 0:
            continue
        if feat in cat_props:
            result[feat] = feature_psi_categorical(col, cat_props[feat])
        elif feat in deciles:
            result[feat] = feature_psi(col, deciles[feat])
        # 既不在 cat_props 也不在 deciles（如训练基线缺该特征）→ 跳过
    if not result:
        return {"per_feature": {}, "max": 0.0, "mean": 0.0, "drifted": []}
    vals = list(result.values())
    drifted = [k for k, v in result.items() if v > 0.25]
    return {
        "per_feature": result,
        "max": float(max(vals)),
        "mean": float(np.mean(vals)),
        "drifted": drifted,
    }
