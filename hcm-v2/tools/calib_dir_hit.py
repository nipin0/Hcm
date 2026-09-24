#!/usr/bin/env python3
"""calib_dir_hit.py — 方向头「命中概率」校准器（唯一真源）。

━━ 为什么需要它（2026-09-21 实测根因，全部有代码/数据佐证）━━━━━━━━━━━━━━━━━━━━━━
生产方向头 `ai_dir_prob` **当前不是校准概率，也没有 0.65 以上的取值域**，四层叠加：

  1. 【退化即退回 raw】`quality_scorer.py:1074-1075`：
        if _calib_is_degenerate(_cal): _cp = _raw_p
     ⇒ 三类校准器一旦被判退化，**校准输出被整体丢弃**，`ai_dir_prob` 退回 softmax 原值。
     实证：`hcm_ai.ai_pred_raw` 中 `cal_p == raw_proba` 逐位相等（15 位有效数字），
     如 `0.46566109377548537 == 0.46566109377548537`。
  2. 【输出质量集中】部署中的 `models/calib_dir_np_v108.pkl` 实测：
        class=+1  pred(0.5)=0.3833  pred(0.7)=0.3833   ← 0.5 与 0.7 同值
        class=-1  pred(0.5)=0.4312  pred(0.7)=0.4312   ← 0.5 与 0.7 同值
     ⇒ 决策区被压成常数、**输出集中在 0.38~0.43**。
     与质量头同病（`quality_scorer.py:438-446` 已记录 v108 质量头"8 档、跨度 0.80
     但 6 档挤在 0.29~0.44 ⇒ 闸门阈值 0.60/0.70 **数学上不可达**"）。
  3. 【阈值不可达】`ai.lm.dir_veto_prob = 0.65`（分时段三键）> 0.43 上限
     ⇒ **`direction_fuse` 即便打开，反向否决也永不触发** —— 配了但不生效。
  4. 【方向与概率不对应】`quality_scorer.py:1106-1116`：
        _top1, _top2 = sorted(_p_sm, reverse=True)[:2]
        ai_dir_prob = float(_top1)
     而 `ai_direction` 在间隙带（`_top1-_top2 < DIR_MARGIN=0.15`）**维持前值** ⇒
     概率来自"当前平滑 argmax"，方向来自"维持值"，**二者可能不是同一个类**。

━━ 本模块做什么 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
按**预测方向**分层（`pred_class ∈ {+1,-1}`），直接拟合
      p_hit = P(dir_label == pred | pred_class == pred, raw_top1)
即"**这个方向会被行情兑现（≥ ai.lm.dir_atr_mult·ATR）的概率**"。

· 用 `_calib_common.fit_platt`（Platt：logit 上近无正则 logistic）而非 isotonic：
  连续输出 ⇒ **不受"档位 ≥ CALIB_MIN_LEVELS"判据约束**、小样本更稳，
  直击上面第 1/2 条（退化退回 raw 与输出集中）。
· **只对 `+1/-1` 拟合**：`0`(FLAT) 不参与方向裁决，拟合它无意义。
· 口径与 `recalibrate_quality.py:336` 一致（`y=(dir_label==pred)`，**含"未达 ±0.8ATR 即失败"**），
  **不新造第二套标签语义**；差别仅在"用 Platt 且真正生效"，不在标签。

━━ 【验收结论 2026-09-21：**未通过，默认不接入生产**】━━━━━━━━━━━━━━━━━━━━━━━━━━
`tools/_scratch/_eval_dir_hit_calib.py`（时间序 70/30 留出，评测 n=340，
2026-09-17 ~ 09-21）三变体实测：

    pred=+1   A.raw      ece=0.1925  mono=+0.6689  p∈[0.345,0.815]  cov(p>=.65)=21.7%  hit=0.5455
              B.现行pkl   ece=0.0316  mono=+0.0000  p∈[0.383,0.383]（**常数**）
              C.本模块     ece=0.0552  mono=-0.6626  p∈[0.303,0.443]
    pred=-1   A.raw      ece=0.0431  mono=+0.7042  p∈[0.362,0.569]
              C.本模块     ece=0.2848  mono=-0.7086

  ① **本模块单调性为负（-0.66 / -0.71）⇒ 未过"monotonicity > 0"验收门**
     （本仓库纪律：不过门即不替换，见 `recalibrate_quality._fit_quality_grade`）。
  ② **A（raw = 当前生产实况，因退化退回 raw）反而单调且铺开** ⇒ 本模块要"改良"的
     对象其实**更好** ⇒ "退化即退回 raw"（`quality_scorer.py:1074-1075`）在本窗口
     不是 bug 而是**有效自愈**。
  ③ **B（现行 pkl）恒为常数**（p∈[0.383,0.383]）⇒ 其 ECE 最低（0.03/0.006）纯属
     "**常数 ≈ base_rate**"的假象 ⇒ **教训：ECE 单独用会被常数校准器欺骗，
     必须与 monotonicity / p 域跨度同时看**。
  ④ ⚠ 更正前序判断："`dir_veto_prob=0.65` 数学上不可达"**只对 B（pkl）成立**；
     对 A（raw）不成立 —— A 的 `+1` 侧 p 上限 **0.815**；而 A 的 `-1` 侧 p 上限
     **0.569 < 0.65** ⇒ **不可达的只有 SELL 侧**。
  ⑤ ⚠⚠ **决定性判据（`tools/_scratch/_dir_head_ceiling.py`，n=1133）—— 本模块的前提被否证**：
        · 排序 **AUC = 0.5144（`pred=+1`）/ 0.4773（`pred=-1`）**，95%CI 均跨 0.5 ⇒
          **本窗口无法证实方向头存在排序信息**；十分位命中率非单调、`pred=-1` 最高档最差。
        · 无条件类分布 `lab=+1 37.0% / 0 21.2% / -1 41.8%` ⇒ 多数类基线 **0.4184**。
        · `+1` 侧 `t>=0.65 ⇒ hit=0.5053(n=95)` 看似有 lift，但 **AUC≈0.5 时"从 9 个阈值里
          挑最高档"天然产生选择偏差** ⇒ **该 lift 不能作为证据**（前序曾据它立论，已撤回）。
      ⇒ **校准的前提是"模型排序有效、只是档位失准"。AUC≈0.5 时该前提不成立 ⇒
        校准（含本模块）在结构上不可能改善命中率** ⇒ 本模块**永久搁置**，
        除非先有 AUC 显著 >0.5（建议 ≥0.55）的新方向头。

  ⇒ 故**保留本文件作为负面结论证据（含方法）**，但
    **严禁在未按同一口径重新验证前接入生产**。重试前须知：本窗口评测样本仅 340、
    单品种；本仓库已有"**窗口特例翻转结论**"的教训（审计 §9.2：近 30 天窗口
    曾让 M5/H1 的方向源结论整体翻转）。

━━ 纪律 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
· **默认不接入生产**：由 `ai.lm.dir_hit_calib_path` 是否配置决定（缺省空 ⇒ 零行为变化）。
· 拟合需 sklearn（宿主机 `recalibrate_quality.py` 侧）；**预测只需 numpy**
  （`PlattCalibrator` 见 `calib_np.py`）⇒ 生产容器无 sklearn 亦可加载。
· 评估口径（`ece`/`monotonicity`）**一律 import `_calib_common`**，不另写一份，
  否则与质量链的 calib_health 无法归因（同一纪律，见 `_calib_common` docstring）。
· 时间序切分评估，**严禁随机切分**：`train_signal_quality.py:379-382` 已记录该事故
  （随机切分把未来样本混进训练 ⇒ `dir_hit` 虚高至 0.89/0.95 而实盘判反）。

回滚：删除本文件 + 清空 `ai.lm.dir_hit_calib_path`（或删 `calib_dir_hit_v*.pkl`）。
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _calib_common import ece as _ece        # noqa: E402  （唯一真源）
from _calib_common import fit_platt           # noqa: E402
from _calib_common import monotonicity as _monotonicity  # noqa: E402

__all__ = ["DIR_CODES", "fit_hit_calibrators", "apply_hit", "p_hit_metrics"]

# 参与方向裁决的类（0=FLAT 不参与，不拟合）
DIR_CODES = (1, -1)

MIN_SAMPLES_DEFAULT = 300


def fit_hit_calibrators(pred_class, raw_top1, dir_label,
                        min_samples: int = MIN_SAMPLES_DEFAULT,
                        ) -> tuple[dict, dict]:
    """按预测方向分层拟合命中率校准器。

    Args:
        pred_class: 方向头预测类（∈ {+1, -1} 参与；其它行被忽略）。
        raw_top1:   `raw_proba` 中**该预测类得分**（= argmax 分量，即 softmax top1）。
        dir_label:  真值方向（∈ {-1,0,+1}）；`0` 表示"未达 ±阈值"，**计为未命中**。
        min_samples: 单侧最小样本；不足则**该侧不拟合**（调用方保留原逻辑）。

    Returns:
        (calibs, report)
          calibs : {+1: PlattCalibrator, -1: PlattCalibrator}（仅含拟合成功的侧）
          report : {"+1": {...指标...}, "-1": {...}}，供健康键/日志消费。

    指标口径与 `recalibrate_quality._fit_quality_grade` 一致（levels/ece/monotonicity/
    brier/base_rate），**但 levels 用连续网格量化**（Platt 无阶梯，见 calib_np
    `PlattCalibrator.level_count`），故档位判据不会误杀。
    """
    pred_class = np.asarray(pred_class)
    raw_top1 = np.asarray(raw_top1, dtype=float)
    dir_label = np.asarray(dir_label)

    calibs: dict = {}
    report: dict = {}
    for _c in DIR_CODES:
        _m = (pred_class == _c) & np.isfinite(raw_top1) & np.isin(dir_label, (-1, 0, 1))
        _n = int(_m.sum())
        _r: dict = {"n": _n}
        if _n < int(min_samples):
            _r["skipped"] = f"n<{int(min_samples)}"
            report[_c] = _r
            continue
        _x = raw_top1[_m]
        # 命中 = 预测的那个方向被兑现（含"没到 ±dir_atr_mult·ATR"算未命中）
        _y = (dir_label[_m] == _c).astype(int)
        if len(set(_y.tolist())) < 2:
            _r["skipped"] = "single-class"
            report[_c] = _r
            continue
        try:
            _cal = fit_platt(_x, _y)
        except Exception as _e:  # noqa: BLE001
            _r["skipped"] = f"fit failed: {_e}"
            report[_c] = _r
            continue
        _p = np.asarray(_cal.predict(_x)).ravel()
        _r.update({
            "levels": int(_cal.level_count()),
            "ece": round(_ece(_y, _p), 4),
            "monotonicity": round(_monotonicity(_y, _p), 4),
            "brier": round(float(np.mean((_p - _y) ** 2)), 4),
            "base_rate": round(float(_y.mean()), 4),
            "a": round(float(getattr(_cal, "a", float("nan"))), 4),
            "b": round(float(getattr(_cal, "b", float("nan"))), 4),
            # 可用性：p 是否铺开（决定 dir_veto_prob 类阈值是否可达）
            "p_min": round(float(np.min(_p)), 4),
            "p_max": round(float(np.max(_p)), 4),
        })
        # 与质量头同一自愈取向：单调性 ≤ 0（高置信反而更差）⇒ 拒绝该侧
        _r["ok"] = bool(_r["monotonicity"] > 0)
        if not _r["ok"]:
            _r["rejected"] = f"monotonicity={_r['monotonicity']}<=0"
        else:
            calibs[_c] = _cal
        report[_c] = _r
    return calibs, report


def apply_hit(calibs: dict, pred_code: int, raw_top1: float) -> float | None:
    """推断期应用：返回该预测方向的命中概率；无可用校准器则 None（调用方回退）。"""
    if not calibs:
        return None
    _c = int(pred_code)
    _cal = calibs.get(_c) or calibs.get(str(_c))
    if _cal is None:
        return None
    try:
        return float(np.asarray(_cal.predict([float(raw_top1)])).ravel()[0])
    except Exception:  # noqa: BLE001
        return None


def p_hit_metrics(y_hit, p_hit) -> dict:
    """分层评估：ECE / 单调性 / 高置信档的命中率与覆盖（供"出数后拍板"）。"""
    y = np.asarray(y_hit, dtype=float)
    p = np.asarray(p_hit, dtype=float)
    out: dict = {"n": int(len(y)), "ece": round(_ece(y, p), 4),
                 "monotonicity": round(_monotonicity(y, p), 4),
                 "base_rate": round(float(y.mean()), 4) if len(y) else None}
    for _thr in (0.50, 0.60, 0.65):
        _m = p >= _thr
        out[f"cov_p>={_thr:.2f}"] = round(float(_m.mean()), 4) if len(p) else None
        out[f"hit_p>={_thr:.2f}"] = (round(float(y[_m].mean()), 4)
                                     if int(_m.sum()) >= 20 else None)
        out[f"n_p>={_thr:.2f}"] = int(_m.sum())
    return out
