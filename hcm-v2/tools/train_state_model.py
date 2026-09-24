#!/usr/bin/env python3
"""train_state_model.py — 行情状态模型（4 类）训练器。

依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §4

要点（严格对齐方案 §4）：
  * 目标 = 4 分类（multi_logloss）。**不使用 lambdarank**：它面向排序任务、需要 query
    group，与本处的多分类目标不符（方案 §13 差异清单已记录该纠正）。
  * 类别权重：balanced 权重 × 配置 boost（默认给 trend_fade 加权，提升其召回 —— 需求二.3）。
  * 验证：TimeSeriesSplit（**禁止普通 k-fold**）—— 时序切分避免未来泄露，与
    train_signal_quality.oof_proba_time_series 同范式。
  * 集成：bagging —— K 个随机种子各训一份，推理侧取概率平均后再 argmax（需求二.4）。
  * 产物：K 个模型文件 + meta.json（含特征列顺序 / 类别顺序 / 训练区间 / CV 指标）。

纪律：本脚本**只读 CSV、只写 models 目录**，不触碰 PG/Redis/生产配置。

用法：
  python train_state_model.py --csv state_M5.csv --tf M5 --version 1 --seeds 5
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
except Exception as exc:  # pragma: no cover
    print(f"[fatal] lightgbm unavailable: {exc}", file=sys.stderr)
    raise

from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import (
    classification_report, confusion_matrix, f1_score,
    precision_recall_fscore_support, roc_auc_score,
)
from sklearn.model_selection import TimeSeriesSplit
from sklearn.utils.class_weight import compute_class_weight

# ⚠ 【2026-09-19 晋升纪律修复】默认输出目录**不再是生产目录**。
#   此前 `MODELS_DIR_DEFAULT = <tools>/models`，而该目录是容器 `/app/review_models`
#   的 **bind mount 源**（生产模型目录）⇒ **不带 `--outdir` 训练即等于"训练即上线"**。
#   真实风险：`state_infer` 按 `max(version)` 隐式选版 ⇒ 一次误训练会被**立刻采纳**。
#   现**复用本仓库既有的版本四态规范**（见 `auto_retrain.MODEL_STAGING_DIR`）：
#     TRAIN（产物落 `_staging`，**不占版本号**）→ ACCEPT（人工确认）→ promote 到 `models/`
#   为什么用**子目录**而非并列目录：`state_infer._discover` 是**非递归 glob**
#   ⇒ `_staging/` 内文件**天然不会被发现**，无需改动任何加载逻辑（零风险）。
MODELS_DIR_PROD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
MODELS_DIR_DEFAULT = os.path.join(MODELS_DIR_PROD, "_staging")

# 类别顺序即 label_id（与 build_state_labels.STATE_NAMES 严格一致，契约）
#
# 【2026-09-15 定案】本训练器**只训 4 类行情形态**：
#  · 形态：本文件（LGBM、前视标签、防抖后驱动 FSM 状态）。
#  · 方向：**规则模块** signal_tower/trend_direction.py（ATR 归一斜率 + ±DI + K 线防抖），
#    不在此训练 —— 方向若也建模型即形成第二份方向真值，与"同一语义只允许一份实现"红线冲突。
# 否决过的两条路（均有实测依据，见方案 §19/§20）：
#  · 7 类合并（形态×方向并入一个模型）：M5 方向准确率 0.521≈随机，形态 F1 还退化 1.2pt；
#  · 独立 3 类方向模型：M5 方向可用 AUC 0.5186（低于多数类基线）→ 不可学。
STATE_NAMES = ["oscillation", "trend_init", "trend_mid", "trend_fade"]

# 训练默认超参（禁魔法数字：逐项注明用途；生产以 args/配置覆盖）
DEFAULTS = {
    "n_estimators": 400,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 20,
    "subsample": 0.85,
    "subsample_freq": 1,
    "colsample_bytree": 0.85,
    "reg_lambda": 1.0,
    "max_depth": -1,
}
# 类别权重 boost（键=类别名；乘在 `balanced` 权重之上）
#
# 【2026-09-21 依据实测下调 `trend_fade`: 1.5 → 1.0】
# 触发：生产报告"未出现 oscillation / trend_mid"，逐根对照**真值**发现
#   `trend_mid` 被模型系统性误判为 `trend_fade`（本窗口 4/4，OOF `mid→fade` = 33.7%，
#   且这些 bar 的**未来** `adx_slope` 为正 = ADX 在**上升**，按定义不可能是 fade）。
# 归因（三级隔离，全部离线 OOF，base27 / 400 树 / 5 折 / gap=12）：
#   ① 加"过去 12 根 ADX 变化"特征 ⇒ **无效**（`mid-vs-fade` AUC +0.0006，误判率 −0.4pt）
#      ；且诊断显示 **过去 ADX 变化对未来 ADX 变化的符号一致率仅 0.4744**（≈随机）
#   ② `adx_slope ≤ −3` 本身 AUC 0.7992，但**几乎全靠单特征 `adx_14`（0.7774）**
#      ⇒ 模型学到的是"**ADX 高 ⇒ 会下降**"（均值回归），而非 ADX 走向
#   ③ **改变本权重即显著改变 `mid→fade`**（见下表）⇒ **本项才是主因**
#
#   | fade_boost | macro_F1 | mid→fade | osc rec | init rec | mid rec | fade rec |
#   |---|---|---|---|---|---|---|
#   | 1.5（原值） | 0.3312 | 0.3374 | 0.1459 | 0.1542 | 0.4111 | 0.7908 |
#   | **1.0（现值）** | **0.3337** | **0.2885** | **0.1546** | **0.1629** | **0.4414** | 0.7043 |
#   | 0.7 | 0.3354 | 0.2381 | 0.1751 | 0.1758 | 0.4689 | 0.5965 |
#
# 为什么取 1.0 而非 0.7：1.0 = **纯 `balanced`**，是"无需额外假设"的默认；
#   1.5 与 0.7 都需论证。1.0 已实现"三类 recall 全升 + macro_F1 升"的帕累托改善，
#   代价仅 fade recall −8.7pt（而 fade 的 precision 原本只有 0.357、被过预测 2.22×，
#   其假阳性正是"过度禁单 ⇒ 状态饥饿"的成因）。
# ⚠ 诚实边界：以上均为**离线判别力指标**；fade recall 下降会减少"衰竭收紧止损"的触发，
#   属实盘行为变化 ⇒ 晋升前须以 `replay_state_chain` / 前向复核，不可只看本表。
# 回滚：把本值改回 1.5 并重训（或用版本钉切回 v5）。
CLASS_WEIGHT_BOOST_DEFAULT = {"oscillation": 1.0, "trend_init": 1.0,
                              "trend_mid": 1.0, "trend_fade": 1.0}


def load_csv(path: str, cols: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise SystemExit(f"[fatal] csv missing feature columns: {missing}")
    if "label_id" not in df.columns:
        raise SystemExit("[fatal] csv missing label_id")
    df = df[df["label_id"].notna()].copy()
    df["label_id"] = df["label_id"].astype(int)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    return df.sort_values("open_time").reset_index(drop=True)


def class_weights(y: np.ndarray, boost: dict) -> dict:
    present = np.unique(y)
    w = compute_class_weight("balanced", classes=present, y=y)
    out = {}
    for cls, wc in zip(present, w):
        name = STATE_NAMES[int(cls)]
        out[int(cls)] = float(wc) * float(boost.get(name, 1.0))
    return out


def build_model(seed: int) -> lgb.LGBMClassifier:
    """按种子构造分类器（类别权重经 sample_weight 显式传入，不用 class_weight 参数）。"""
    return lgb.LGBMClassifier(
        objective="multiclass",
        num_class=len(STATE_NAMES),
        random_state=seed,
        n_jobs=-1,
        verbose=-1,
        **DEFAULTS,
    )


def sample_weight_for(cw: dict, labels: np.ndarray) -> np.ndarray:
    return np.asarray([cw.get(int(c), 1.0) for c in labels])


def oof_time_series(X: pd.DataFrame, y: np.ndarray, n_splits: int, cw: dict,
                    gap: int = 0):
    """时序 OOF 概率（手写循环：TimeSeriesSplit 首段不进测试集，cross_val_predict 会报错）。

    【2026-09-19 泄漏修复】`gap` = 训练折与验证折之间**丢弃**的 bar 数。
    为什么必须 >0：标签是**前瞻**的（`label_metrics(i)` 用 `close[i+1 : i+horizon]`），
    而 `TimeSeriesSplit` 默认让两折**首尾相接** ⇒ 训练折末尾 `horizon` 根的标签
    用到了验证折开头 `horizon` 根的价格 ⇒ **折边界标签重叠**（轻度泄漏）。
    取 `gap = horizon` 即可消除。默认 0 = 既有行为（调用方须显式给值）。
    """
    tss = TimeSeriesSplit(n_splits=n_splits, gap=gap)
    oof = None
    for tr, te in tss.split(X):
        m = build_model(42)
        m.fit(X.iloc[tr], y[tr], sample_weight=sample_weight_for(cw, y[tr]))
        p = m.predict_proba(X.iloc[te])
        if oof is None:
            oof = np.full((len(X), len(STATE_NAMES)), np.nan)
        oof[te] = p
    if oof is None:
        return None, None
    cov = ~np.isnan(oof[:, 0])
    return oof[cov], y[cov]


def report(tag: str, y_true: np.ndarray, proba: np.ndarray) -> dict:
    pred = proba.argmax(axis=1)
    labels = list(range(len(STATE_NAMES)))
    macro_f1 = f1_score(y_true, pred, average="macro", labels=labels, zero_division=0)
    pr, rc, f1, sup = precision_recall_fscore_support(
        y_true, pred, labels=labels, zero_division=0)
    try:
        auc_ovr = roc_auc_score(y_true, proba, multi_class="ovr", average="macro",
                                labels=labels)
    except ValueError:
        auc_ovr = float("nan")

    print(f"\n===== {tag} =====")
    print(f"  samples={len(y_true)}  macro_F1={macro_f1:.4f}  AUC_OVR(宏观)={auc_ovr:.4f}")
    print(f"  {'class':<14}{'precision':>10}{'recall':>10}{'f1':>10}{'support':>10}")
    per_class = {}
    for c in labels:
        nm = STATE_NAMES[c]
        print(f"  {nm:<14}{pr[c]:>10.4f}{rc[c]:>10.4f}{f1[c]:>10.4f}{int(sup[c]):>10d}")
        per_class[nm] = {"precision": float(pr[c]), "recall": float(rc[c]),
                         "f1": float(f1[c]), "support": int(sup[c])}
    print("  confusion matrix (row=true, col=pred):")
    cm = confusion_matrix(y_true, pred, labels=labels)
    header = "".join(f"{n[:9]:>11}" for n in STATE_NAMES)
    print(f"  {'':<14}{header}")
    for c in labels:
        row = "".join(f"{int(v):>11d}" for v in cm[c])
        print(f"  {STATE_NAMES[c]:<14}{row}")

    return {"macro_f1": float(macro_f1), "auc_ovr_macro": float(auc_ovr),
            "per_class": per_class, "confusion": cm.tolist(),
            "samples": int(len(y_true))}


# ── 【2026-09-19 阶段1】校准 + conformal（弃权闸的两块产物）────────────────────
# 为什么需要：`state_infer` 的 `margin` 是 **top1−top2 的原始差**，不是概率，也没有绝对含义
#   （生产实测：margin p50=0.128、≥0.60 仅 3.4%）。任何"概率闸门"都必须先有可解释的尺度。
# 诚实边界（必须写明）：校准是**单调映射** ⇒ **不改变排序**。故它**不会**把
#   "margin 与经济指标负相关"变成正相关（实测见 docs/方案_状态机判别力改进_20260919.md §8.2-D3、§8.4）。
#   它做的是：让 p 有绝对含义（0.6 真的意味着 ~60% 正确），这是"弃权/路由"可被审计的前提。
CONFORMAL_ALPHAS = (0.05, 0.10, 0.20)


# 【2026-09-21 去重】此处原为 ECE 的**第 4 份**实现（数学等价，但参数序为 (p, hit)）。
# 逻辑已收敛到 _calib_common.ece（唯一真源），本处只保留一层**参数序适配**，
# 使本文件内 6 处调用（_cross_fitted_ece:256、evaluate:282/283/313/314）零改动。
from _calib_common import ece as _ece_impl  # noqa: E402


def _ece(p: np.ndarray, hit: np.ndarray, bins: int = 10) -> float:
    """期望校准误差（等宽分箱，按样本数加权）。0 = 完美校准。实现见 _calib_common.ece。"""
    return _ece_impl(hit, p, bins)


def _cross_fitted_ece(p: np.ndarray, hit: np.ndarray, n_splits: int = 5) -> float:
    """**交叉拟合**的"校准后 ECE" —— 校准验收门里**唯一有信息量**的那个数。

    为什么必须交叉拟合（本项修复的缺陷）：isotonic 能**精确**拟合它自己见过的样本
    ⇒ 在同一批 OOF 上同时"拟合校准器"并"报告校准后 ECE"，必然得到 ≈0
    （**构造性结果，不含泛化信息**）。此前 meta 里的 `ece_cal = 0.0000` 就是这样来的，
    它**不能**作为"校准良好"的证据。

    做法：按**时间顺序**切 K 折（`oof_p` 本就按时间排），每折用其余 K−1 折拟合校准器、
    在该折上评估 ECE，再按样本数加权汇总 ⇒ 得到**样本外**的 ECE。
    分层：**不做随机洗牌**（时序数据洗牌会把未来折的信息带进过去折的校准器）。
    """
    n = len(p)
    if n < n_splits * 20:
        return float("nan")
    idx = np.arange(n)
    num, den = 0.0, 0.0
    for f in np.array_split(idx, n_splits):
        rest = np.setdiff1d(idx, f)
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        try:
            iso.fit(p[rest], hit[rest])
            cal_f = iso.predict(p[f])
        except Exception:  # noqa: BLE001
            continue
        num += float(len(f)) * _ece(cal_f, hit[f])
        den += float(len(f))
    return (num / den) if den > 0 else float("nan")


def fit_confidence_calibrator(oof_p: np.ndarray, y: np.ndarray) -> dict:
    """在 **OOF** 上拟合「top-1 置信 → 经验正确率」的 isotonic 校准。

    为什么必须用 OOF：训练集内概率是**过度自信**的（模型见过这些样本），
    用它拟合出的校准器在前向是错的。OOF 是生产口径的最近似。
    产物以 `(x, y)` 阈值对落 meta，推理侧用 `np.interp` 复现（**不依赖 sklearn**）。

    ⚠ 读 meta 时的纪律：**`ece_cal` 是样本内（构造性 ≈0），只有 `ece_cal_cv` 有信息量。**
    验收门请用 `ece_cal_cv`（以及 Brier 的样本外版本）。
    """
    top = oof_p.max(axis=1)
    hit = (oof_p.argmax(axis=1) == y).astype(float)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(top, hit)
    cal = iso.predict(top)
    return {
        "kind": "isotonic_top1",
        "x": [float(v) for v in np.atleast_1d(iso.X_thresholds_)],
        "y": [float(v) for v in np.atleast_1d(iso.y_thresholds_)],
        "n": int(len(y)),
        # ⚠ 样本内（构造性 ≈0，**不得**当作"校准良好"的证据）
        "ece_raw": float(_ece(top, hit)),
        "ece_cal": float(_ece(cal, hit)),
        # ✅ 样本外（**这才是验收门该看的数**）
        "ece_cal_cv": _cross_fitted_ece(top, hit),
        "brier_raw": float(np.mean((top - hit) ** 2)),
        "brier_cal": float(np.mean((cal - hit) ** 2)),
    }


def fit_binary_prob_calibrator(p: np.ndarray, y: np.ndarray) -> dict:
    """二分类**正类概率**的 isotonic 校准（x = raw p，y = 校正后的 P(y=1)）。

    为什么与 `fit_confidence_calibrator` 分开：后者校准的是**多分类 top-1 置信**
    （x = max prob、y = argmax 是否正确），语义不同、产物不可互换 ——
    属"两个不同语义"，不是"同语义两份实现"（本仓库红线针对后者）。

    用途（P2 波动路由）：`vol` 头需要一个**可比的 P(波动扩张)** 才能与阈值比较；
    未校准的原始概率在不同波动 regime 下尺度会漂移，直接比阈值等于用错尺子。
    """
    pf = np.asarray(p, dtype=float)
    yf = np.asarray(y, dtype=float)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(pf, yf)
    cal = iso.predict(pf)
    return {
        "kind": "isotonic_binary_prob",
        "x": [float(v) for v in np.atleast_1d(iso.X_thresholds_)],
        "y": [float(v) for v in np.atleast_1d(iso.y_thresholds_)],
        "n": int(len(yf)),
        "pos_rate": float(yf.mean()),
        # ⚠ 样本内（构造性 ≈0，**不得**当作"校准良好"的证据；见 `_cross_fitted_ece`）
        "ece_raw": float(_ece(pf, yf)),
        "ece_cal": float(_ece(cal, yf)),
        # ✅ 样本外（**验收门该看这个**）
        "ece_cal_cv": _cross_fitted_ece(pf, yf),
        "brier_raw": float(np.mean((pf - yf) ** 2)),
        "brier_cal": float(np.mean((cal - yf) ** 2)),
    }


def conformal_quantiles(oof_p: np.ndarray, y: np.ndarray,
                        alphas=CONFORMAL_ALPHAS) -> dict:
    """split-conformal：非一致性分数 `s = 1 − p̂(真实类)`，取 ⌈(n+1)(1−α)⌉ 分位 `q̂`。

    保证（有限样本、分布无关）：`P(真实类 ∈ {k : p̂_k ≥ 1 − q̂}) ≥ 1 − α`。
    ⇒ **当预测集合是单元素时，其错误率 ≤ α** —— 这是"高置信度"唯一能拿到**证书**的形态，
    也是本项交付的核心：把"置信度"从"一个说不清含义的数"变成"一个有覆盖率保证的集合"。
    """
    n = len(y)
    s = 1.0 - oof_p[np.arange(n), y]
    out: dict = {"kind": "split_conformal_lac", "n": n}
    for a in alphas:
        k = int(np.ceil((n + 1) * (1.0 - a)))
        if k > n:
            out[f"{a:.2f}"] = None
            continue
        q = float(np.sort(s)[k - 1])
        thr = 1.0 - q
        in_set = oof_p >= thr
        sizes = in_set.sum(axis=1)
        single = sizes == 1
        out[f"{a:.2f}"] = {
            "qhat": q, "thr": thr, "k": k,
            "coverage": float(in_set[np.arange(n), y].mean()),
            "singleton_rate": float(single.mean()),
            "singleton_acc": (float((oof_p[single].argmax(axis=1) == y[single]).mean())
                              if int(single.sum()) else None),
        }
    return out


def main() -> None:
    # 【2026-09-19 实测踩到】Windows 控制台默认 cp936，stdout 被管道重定向时按
    # locale 编码 ⇒ `⇒`（U+21D2）等**非 GBK 字符**会抛 UnicodeEncodeError，且异常走
    # stderr（常被 `2>$null` 吞掉）⇒ 表现为"脚本中途静默死掉、产物不落地"。
    # 与本仓库既有工具（eval_state_separability / eval_anticipation_bound）同款防护。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="build_state_labels.py 的产物")
    ap.add_argument("--feature-set", default="base", choices=["base", "l1"],
                    help="特征集：base = STATE_FEATURE_COLS(27)；"
                         "l1 = base + L1_FEATURE_COLS（量价/点差 6 列）。"
                         "默认 base（与既有模型及推理契约一致，零变化）")
    ap.add_argument("--tf", required=True, choices=["M5", "M15", "H1"])
    ap.add_argument("--version", type=int, required=True, help="模型版本号（vN）")
    ap.add_argument("--seeds", type=int, default=5, help="bagging 随机种子数 K")
    ap.add_argument("--cv-splits", type=int, default=5, help="TimeSeriesSplit 折数")
    ap.add_argument("--gap", type=int, default=12,
                    help="训练/验证折之间丢弃的 bar 数（消除前瞻标签在折边界的重叠）。"
                         "默认 12 = 标签主窗口 `state.horizon_bars`；0 = 旧行为（含泄漏）。")
    ap.add_argument("--test-ratio", type=float, default=0.2, help="末端留出测试比例")
    ap.add_argument("--outdir", default=MODELS_DIR_DEFAULT)
    ap.add_argument("--report", default=None, help="评估报告输出路径（json）")
    args = ap.parse_args()

    # 【2026-09-19 晋升纪律守卫】显式指向生产目录时**必须可见**：
    # 该目录是容器 `/app/review_models` 的 bind mount 源，且 `state_infer` 按
    # `max(version)` 隐式选版 ⇒ 写入即被采纳（无人工确认环节）。
    if os.path.abspath(args.outdir) == os.path.abspath(MODELS_DIR_PROD):
        print("[warn] --outdir 指向**生产模型目录** ⇒ 产物会被立刻采纳"
              "（max(version) 隐式选版）。建议用默认 `_staging`，验收后再显式晋升。")

    # 特征列顺序从特征契约导入（单一真值）
    feat_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "hcm-signal-tower", "signal_tower", "state_features.py")
    spec = importlib.util.spec_from_file_location("state_features", feat_path)
    sf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sf)
    cols = list(sf.STATE_FEATURE_COLS_L1 if args.feature_set == "l1"
                else sf.STATE_FEATURE_COLS)

    df = load_csv(args.csv, cols)
    if len(df) < 200:
        print(f"[warn] 样本仅 {len(df)} 条，指标不可靠（需积累更多 K 线历史）", file=sys.stderr)
    if df["label_id"].nunique() < 2:
        raise SystemExit("[fatal] 标签类别不足 2 类，检查标签阈值标定")

    X = df[cols].astype(float)
    y = df["label_id"].to_numpy()
    cw = class_weights(y, CLASS_WEIGHT_BOOST_DEFAULT)

    print(f"[data] rows={len(df)} tf={args.tf} "
          f"range={df['open_time'].iloc[0]} .. {df['open_time'].iloc[-1]}")
    print(f"[dist] {df['label_name'].value_counts().to_dict()}")
    print(f"[class_weight] { {STATE_NAMES[k]: round(v, 3) for k, v in cw.items()} }")

    # ── 评估 1：全量时序 OOF（最接近线上口径）──
    oof_p, oof_y = oof_time_series(X, y, args.cv_splits, cw, gap=args.gap)
    if oof_p is None:
        raise SystemExit("[fatal] OOF 失败（样本不足或类别缺失）")
    oof_metrics = report(f"TimeSeriesSplit OOF ({args.cv_splits} folds)", oof_y, oof_p)

    # ── 【2026-09-19 阶段1】校准 + conformal（产物落 meta；消费方 = state_infer 的弃权闸）──
    calib = fit_confidence_calibrator(oof_p, oof_y)
    conf = conformal_quantiles(oof_p, oof_y)
    print("\n===== 校准（OOF；isotonic top-1）=====")
    print(f"  ECE    原始={calib['ece_raw']:.4f} → 校准后(样本内)={calib['ece_cal']:.4f}"
          f" → **校准后(交叉拟合)={calib.get('ece_cal_cv', float('nan')):.4f}**")
    print(f"  Brier  原始={calib['brier_raw']:.4f} → 校准后={calib['brier_cal']:.4f}")
    print("===== conformal（OOF；集合为单例 ⇒ 可决策，错误率 ≤ α）=====")
    for _a, _v in conf.items():
        if not isinstance(_v, dict):
            continue
        print(f"  alpha={_a}: qhat={_v['qhat']:.4f} 阈值={_v['thr']:.4f} "
              f"覆盖率={_v['coverage']:.1%} 单例率={_v['singleton_rate']:.1%} "
              f"单例准确率={_v['singleton_acc']}")

    # ── 评估 2：末端留出测试集（bagging 平均口径 = 线上推理口径）──
    # 注意：留出集模型只能用训练段拟合并评估；这与下面的"全量最终模型"是两批不同的模型。
    split = int(len(df) * (1.0 - args.test_ratio))
    Xtr, Xte = X.iloc[:split], X.iloc[split:]
    ytr, yte = y[:split], y[split:]
    proba_te = np.zeros((len(Xte), len(STATE_NAMES)))
    for s in range(args.seeds):
        m = build_model(100 + s)
        m.fit(Xtr, ytr, sample_weight=sample_weight_for(cw, ytr))
        proba_te += m.predict_proba(Xte)
    proba_te /= args.seeds
    holdout_metrics = report(
        f"Holdout last {args.test_ratio:.0%} (bagging x{args.seeds})", yte, proba_te)

    # ── 最终 bagging 模型：全量数据训 K 个种子（保存 + 顺带累计特征重要性）──
    os.makedirs(args.outdir, exist_ok=True)
    paths = []
    imp = np.zeros(len(cols))
    for s in range(args.seeds):
        m = build_model(100 + s)
        m.fit(X, y, sample_weight=sample_weight_for(cw, y))
        name = f"lgbm_state_{args.tf}_v{args.version}_s{s}.txt"
        m.booster_.save_model(os.path.join(args.outdir, name))
        paths.append(name)
        imp += m.feature_importances_
    imp /= args.seeds
    top = sorted(zip(cols, imp), key=lambda t: -t[1])[:12]
    print("\n[top_features] " + ", ".join(f"{c}={v:.0f}" for c, v in top))

    meta = {
        "tf": args.tf,
        "version": args.version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "feature_cols": cols,
        "class_names": STATE_NAMES,
        "n_seeds": args.seeds,
        "model_files": paths,
        "class_weight": {STATE_NAMES[k]: v for k, v in cw.items()},
        "lgbm_params": DEFAULTS,
        "train_rows": int(len(df)),
        "train_range": [df["open_time"].iloc[0].isoformat(),
                        df["open_time"].iloc[-1].isoformat()],
        "eval_oof": oof_metrics,
        "eval_holdout": holdout_metrics,
        "feature_importance": {c: float(v) for c, v in zip(cols, imp)},
        # 【2026-09-19 阶段1】弃权闸（abstain）所需的校准与 conformal 产物。
        # 消费方：`signal_tower/state_infer.py`。**缺这两块 ⇒ 该模型不支持弃权**
        # （`abstain` 恒 False，逐位退回既有行为，不会因产物缺失而改变任何现状）。
        "calibration": calib,
        "conformal": conf,
    }
    meta_path = os.path.join(args.outdir, f"lgbm_state_{args.tf}_v{args.version}_meta.json")
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"\n[out] {len(paths)} models + meta → {args.outdir}")
    print(f"[out] meta: {meta_path}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        print(f"[out] report: {args.report}")


if __name__ == "__main__":
    main()
