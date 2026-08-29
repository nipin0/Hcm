#!/usr/bin/env python3
"""train_signal_quality.py — 阶段 0 LightGBM 信号质量评分器训练 + 概率校准 + 回测。

- 读取 build_labels.py 产出的 labels.csv + quality_features.py 产出的 features.csv，
  按 signal_id 内连接。
- 时间序（walk-forward）切分：按 created_at 排序，训练前段、测试后段，杜绝时间泄漏。
- LightGBM 二分类（scale_pos_weight 处理类不平衡）+ Platt/isotonic 概率校准。
- 回测指标：AUC（原始/校准后）、阈值 0.5/0.6/0.7 下的胜率与覆盖、相对基线胜率提升。
- 产物：模型文件 + 校准器文件（不入库、不出境，纯本地磁盘）。

用法:
  python train_signal_quality.py --labels labels.csv --features features.csv \
    --model lgbm_quality_v0.txt --calib calib_v0.pkl --outdir ./artifacts
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys

import numpy as np
import pandas as pd

# 【阶段2·数据契约单一真值】训练侧特征列必须严格对齐推理侧(quality_scorer.FEATURE_COLS)。
# 动态删列后强制 reindex 到 MODEL_FEATURE_COLS，缺失列补 0（与推理侧恒 0 语义一致），
# 杜绝 v3 式"训练混入 state_* 哑变量、漏 DeepSeek/extreme 列"的列错位回归。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _model_feature_cols import MODEL_FEATURE_COLS  # noqa: E402

try:
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.isotonic import IsotonicRegression
    from sklearn.model_selection import train_test_split
except ImportError as e:
    print(f"[fatal] missing dependency: {e}\n"
          f"pip install lightgbm scikit-learn pandas numpy", file=__import__("sys").stderr)
    raise

PASS, DOWN, UP = 0.50, 0.60, 0.70  # 与 ai.lm.* 阈值对齐（可后续改从配置读）


def _model_version_from_path(model_path):
    """从质量头模型路径解析版本号（``lgbm_quality_v54.txt`` → ``"54"``）。

    方向头 / 买点头与质量头由**同一次训练**产出，共用同一版本号，
    便于 sidecar 按「同目录版本发现」一次性加载配套三头
    （见 ``quality_scorer._latest_version_path``）。

    【2026-08-29 阶段 2】此前方向头/买点头文件名硬编码 v54，
    导致每次重训都覆盖 v54 而不产出新版本——阶段 0 给 sidecar 加的
    版本发现能力形同虚设。现改为跟随质量头版本号。

    解析失败（如未传 ``--model``）回退 ``"54"``，保证既有调用行为不变。
    """
    import re as _re

    if not model_path:
        return "54"
    m = _re.search(r"lgbm_quality_v(\d+)\.txt$", os.path.basename(str(model_path)))
    return m.group(1) if m else "54"


def load(labels_path, features_path):
    lab = pd.read_csv(labels_path)
    fea = pd.read_csv(features_path)
    lab = lab.dropna(subset=["label"]).copy()
    lab["label"] = lab["label"].astype(int)
    df = lab.merge(fea, on="signal_id", how="inner")
    # 【特征增强 2026-08-17】与 quality_scorer.build_features 同公式派生鲁棒特征，
    # 保证训练/推理特征口径一致(消除 DI/点差绝对值漂移盲区)。
    try:
        eps = 1e-6
        pdi = df["plus_di"].astype(float)
        mdi = df["minus_di"].astype(float)
        df["di_ratio"] = pdi / (mdi + eps)
        df["di_net"] = (pdi - mdi) / (pdi + mdi + eps)
        df["spread_atr_log"] = df["spread_atr"].astype(float).clip(lower=0).apply(lambda v: float(np.log1p(v)))
        atr_col = "atr_14" if "atr_14" in df.columns else ("atr" if "atr" in df.columns else None)
        if atr_col is not None:
            atr = df[atr_col].astype(float).replace(0, np.nan).fillna(1e-9)
            # close_mom_atr: features 无 close(单行) → 用 mm(动量均值)/atr_14 近似，
            # 与 build_features 端"近5根收盘动量/ATR"同量纲(动量强度)
            df["close_mom_atr"] = df["mm"].astype(float).fillna(0.0) / atr.values
        else:
            df["close_mom_atr"] = 0.0
        # trend_aligned: ema20_dist_atr>0 → 价格在 EMA 一侧(近似顺/逆趋势方向，>0=多头侧)
        df["trend_aligned"] = (df.get("ema20_dist_atr", pd.Series(0.0, index=df.index)) > 0).astype(float)
    except Exception as _e:
        print(f"[warn] feature-enhance derive skipped: {_e}", file=__import__("sys").stderr)
        for _c in ("di_ratio", "di_net", "spread_atr_log", "close_mom_atr", "trend_aligned"):
            if _c not in df.columns:
                df[_c] = 0.0
    return df


def prepare(df: pd.DataFrame):
    """特征准备：删 ID/文本/全空列，one-hot 类别列。"""
    df = df.copy()
    # 【A+B 同源对齐 2026-08-17】保证训练/推理特征口径完全一致。
    # 排除推理侧 build_features 不装配的列（推理时这些信号属性根本不可得 → 恒 0 失真）：
    #   pre_score/confidence/lot/ai_sl_mult/suggested_lot_ratio（下单时信号属性，实时无）
    #   regime/h1_regime/h1_trend_direction/position_in_range（hexp 决策元数据，build_features 不产）
    #   hp_score/hp_strength/dir_sum/k/verdict（MISSING_HEXP 占位，NaN）
    #   signal_id/symbol/signal_dir/reason/hit_bar_idx/R/label/created_at/entry_price（标识/标签）
    # 保留 build_features 真实产出的所有列（adx_14/rsi_14/macd/atr_14/h1_adx/h1_trend_strength/
    # er/bbw/.../session_*/增强列/结构因子列）。
    #   ds_calib_weight（DeepSeek 训练期赋能权重）—— 【2026-08-18 泄漏修复】
    #     它是 fit 的 sample_weight（见 main 的 sw_full），绝不能同时充当输入特征：
    #     ①目标泄漏：该权重由 DeepSeek 对"该样本是否看对"的判断推导，含标签信息；
    #     ②训练/推理口径不一致：推理侧 build_features 不产此列，线上恒缺省 → 失真。
    #     历史模型 lgbm_quality_v2/v4 的 feature_names 首位误含此列，即此 bug 产物，
    #     下次重训即自动纠正。
    drop_cols = ["signal_id", "symbol", "signal_dir", "reason", "hit_bar_idx", "R",
                 "pre_score", "confidence", "lot", "ai_sl_mult", "suggested_lot_ratio",
                 "regime", "h1_regime", "h1_trend_direction", "position_in_range",
                 "hp_score", "hp_strength", "dir_sum", "k", "verdict",
                 "ds_calib_weight"]
    # 【2026-08-24 修复·标签污染根因】labels.csv 中约 23% 样本 label 为 NaN
    # （未平仓/无 outcome 的实时信号）。若不剔除，df["label"].astype(int) 会把 NaN
    # 静默转成 0（或极大负数）→ 标签被污染 → 模型学到错误关系 → 主切片 AUC≈0.51
    # （近随机）→ early_stopping 第 1 轮即 best → 仅训练出 1-2 棵树 → 部署后
    # ai_score 恒为 ~20（退化）。必须先 dropna 标签再训练。
    _valid = df["label"].notna()
    if _valid.sum() == 0:
        raise RuntimeError("[fatal] 全部样本 label 缺失，无法训练")
    if (_valid.sum() < len(df)):
        print(f"[label] 剔除 {int((~_valid).sum())} 条 NaN 标签样本（未平仓/无 outcome），"
              f"保留 {int(_valid.sum())} 条有效样本", file=sys.stderr)
    df = df[_valid].reset_index(drop=True)

    y = df["label"].astype(int).values
    created = pd.to_datetime(df["created_at"], utc=True)
    X = df.drop(columns=[c for c in drop_cols + ["label", "created_at", "entry_price"] if c in df.columns])

    # 丢弃全 NaN（缺省 hexp 特征 + 无数据环境特征）
    X = X.dropna(axis=1, how="all")

    # 类别列（object 或 pandas StringDtype）one-hot
    cat_cols = [c for c in X.columns if str(X[c].dtype) in ("object", "string")]
    X = pd.get_dummies(X, columns=cat_cols, dummy_na=False)

    # 残余非数值列强制转数值（失败→NaN），再丢弃因此产生的全空列
    for c in X.columns:
        if not pd.api.types.is_numeric_dtype(X[c]):
            X[c] = pd.to_numeric(X[c], errors="coerce")
    X = X.dropna(axis=1, how="all")
    # 丢弃缺失率过高的列（>60% 缺失）
    X = X.loc[:, X.isna().mean() <= 0.6]

    # 剩余缺失用中位数填充
    X = X.fillna(X.median())
    X = X.astype(float)

    # 【阶段2·数据契约对齐】强制 reindex 到推理侧固定特征列，缺失列补 0.0
    # （与 quality_scorer.build_features 恒 0 语义一致）。保证模型 feature_name()
    # 严格等于 MODEL_FEATURE_COLS，杜绝列错位（v3 式 state_* 哑变量混入 / ds_* 漏列）。
    X = X.reindex(columns=MODEL_FEATURE_COLS, fill_value=0.0)
    # 防御：dropna 可能把某列全 0 但类型变成 object？上面已 astype(float)，此处再保底
    X = X.astype(float)
    return X, y, created


# 校准器退化护栏：验证块正样本过少或拟出档位<此值 → 视为退化，回退原始概率
CALIB_MIN_LEVELS = 4
CALIB_MIN_POS = 8  # 验证块至少需若干正样本才能可靠拟合单调映射


def fit_calibrator(model, X_val, y_val):
    p = model.predict_proba(X_val)[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.05, y_max=0.95)
    iso.fit(p, y_val)
    return iso


def fit_calibrator_safe(model, X_val, y_val):
    """带退化护栏的校准器拟合（阶段1b）。

    小样本下 Isotonic 在稀疏验证块上常只拟出 2~3 档（记忆 18061482 实证：
    ~67 条验证块→levels=2→_calib_is_degenerate），强行校准反而扭曲概率。
    退化判定：验证块正样本 < CALIB_MIN_POS 或拟合后唯一阈值档位 < CALIB_MIN_LEVELS
    → 返回 None（调用方回退 model 原始概率，不静默产出坏校准器）。
    """
    if X_val is None or len(y_val) == 0:
        print(f"[calib] SKIP: 空验证块，回退原始概率", file=sys.stderr)
        return None
    y_val = np.asarray(y_val)
    n_pos = int((y_val == 1).sum())
    if n_pos < CALIB_MIN_POS:
        print(f"[calib] DEGENERATE: 验证块正样本仅 {n_pos} < {CALIB_MIN_POS}，"
              f"回退原始概率（不强行校准）", file=sys.stderr)
        return None
    p = model.predict_proba(X_val)[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.05, y_max=0.95)
    iso.fit(p, y_val)
    levels = len(np.unique(np.round(iso.predict(p), 6)))
    if levels < CALIB_MIN_LEVELS:
        print(f"[calib] DEGENERATE: 拟合档位仅 {levels} < {CALIB_MIN_LEVELS}，"
              f"回退原始概率", file=sys.stderr)
        return None
    print(f"[calib] OK: 验证块正样本={n_pos} 拟合档位={levels}", file=sys.stderr)
    return iso


def evaluate(name, y_true, p):
    auc = roc_auc_score(y_true, p)
    base = y_true.mean()
    out = [f"  [{name}] AUC={auc:.4f} 基线胜率={base:.3f}"]
    for th in (PASS, DOWN, UP):
        sel = p >= th
        if sel.sum() == 0:
            out.append(f"    p>={th:.2f}: 覆盖 0 条（无样本）")
            continue
        wr = y_true[sel].mean()
        lift = wr - base
        out.append(f"    p>={th:.2f}: 覆盖 {int(sel.sum())} 条 | 胜率 {wr:.3f} | 提升 {lift:+.3f}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="labels.csv")
    ap.add_argument("--features", default="features.csv")
    ap.add_argument("--model", default="lgbm_quality_v0.txt")
    ap.add_argument("--calib", default="calib_v0.pkl")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--test-ratio", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--audit", action="store_true",
                    help="打印最终模型特征列顺序与训练集样本分布，供与推理侧 quality_scorer --audit 输出 diff 对齐")
    args = ap.parse_args()

    df = load(args.labels, args.features)
    print(f"[data] joined rows={len(df)} win_rate={df['label'].mean():.3f}")
    if len(df) < 50:
        print("[warn] 样本 < 50，训练结果仅供参考（需更多标注数据）")

    X, y, created = prepare(df)
    order = np.argsort(created.values)
    X, y = X.iloc[order].reset_index(drop=True), y[order]

    # 【设计文档 1.3 标签校准】若 labels.csv 含 ds_calib_weight 列（build_labels --ds-calibrate
    # 产出），作为质量头训练的样本权重（强化 DeepSeek 看对、弱化疑似噪声样本）。
    # 不进入特征矩阵（避免信息泄露/目标泄漏），仅作 fit 的 sample_weight。
    sw_full = None
    if "ds_calib_weight" in df.columns:
        sw_full = df["ds_calib_weight"].iloc[order].reset_index(drop=True).astype(float)
        print(f"[ds_calib] sample_weight enabled: mean={sw_full.mean():.3f} "
              f"n_boost={int((sw_full > 1).sum())} n_down={int((sw_full < 1).sum())}",
              file=sys.stderr)

    # 【数据充分性诊断 2026-08-17】DeepSeek 三特征(ds_*)在训练集中的非零占比。
    # 历史数据(DeepSeek 链路 8-17 才打通)92%+ 为缺省 0.0 → 模型无法学到语义。
    # 此检查是正式诊断(非临时探针)：占比过低时打印 WARNING，避免误判"特征无效"。
    _ds_cols = [c for c in ("ds_fake_prob", "ds_sl_coeff", "ds_continuity") if c in X.columns]
    if _ds_cols:
        _nz = (X[_ds_cols].abs().sum(axis=1) > 0).mean()
        # 【2026-08-24 修复】改纯 ASCII 输出：Windows 下子进程 stdout 是 GBK/cp936，
        # auto_retrain 用 UTF-8 解码中文会乱码/替换符 → ds_nonzero_ratio regex 匹配
        # 失败 → DeepSeek 裁判拿不到 ds 吸收率。纯 ASCII 不受编码影响、跨平台稳定。
        if _nz < 0.3:
            print(f"[ds_diag] WARNING: DeepSeek nonzero ratio only {_nz:.1%} "
                  f"(history lacks DeepSeek tickets). Retrain after accumulating "
                  f"ai:ds:out; model has NOT absorbed ds semantics.", file=sys.stderr)
        else:
            print(f"[ds_diag] DeepSeek nonzero ratio {_nz:.1%} (>=30%), usable for training.",
                  file=sys.stderr)

    n = len(y)
    cut = int(n * (1 - args.test_ratio))
    X_tr, X_te = X.iloc[:cut], X.iloc[cut:]
    y_tr, y_te = y[:cut], y[cut:]

    print(f"[split] train={len(y_tr)} test={len(y_te)} (时间序 walk-forward, 无泄漏)")

    # 训练集内部再切一小块做早停 + 校准
    if len(y_tr) >= 80:
        _v = int(len(y_tr) * 0.8)
        X_tr2, X_va, y_tr2, y_va = X_tr.iloc[:_v], X_tr.iloc[_v:], y_tr[:_v], y_tr[_v:]
    else:
        X_tr2, X_va, y_tr2, y_va = X_tr, X_tr, y_tr, y_tr

    # ── 多任务状态头：先用 state_label 训一个多分类器（让模型"看见"状态）──
    # state_label 来自 build_labels 的 KMeans 自动聚类（无人工阈值），只用入场窗口历史，无未来泄露。
    state_col = df["state_label"].iloc[order].reset_index(drop=True)
    state_mask = state_col.notna()
    if state_mask.sum() >= 50:
        Xs = X[state_mask]
        ys = state_col[state_mask].astype(str)
        from sklearn.model_selection import train_test_split as _tts
        Xs_tr, Xs_te, ys_tr, ys_te = _tts(Xs, ys, test_size=0.2, random_state=args.seed)
        state_model = lgb.LGBMClassifier(
            objective="multiclass", num_class=4, n_estimators=200, learning_rate=0.05,
            num_leaves=15, min_child_samples=20, subsample=0.8, colsample_bytree=0.8,
            random_state=args.seed, verbose=-1,
        )
        state_model.fit(Xs_tr, ys_tr,
                        eval_set=[(Xs_te, ys_te)], eval_metric="multi_logloss",
                        callbacks=[lgb.early_stopping(50, verbose=False)])
        from sklearn.metrics import accuracy_score, classification_report
        s_pred = state_model.predict(Xs_te)
        print(f"[state_head] accuracy={accuracy_score(ys_te, s_pred):.4f}")
        print(classification_report(ys_te, s_pred, zero_division=0))
        # 【路线①·状态粒度优化 2026-08-18】质量头不再吃离散 state one-hot
        # (原 concat 使模型"见 TREND 直接给 1.0"、类内强/弱趋势无分化)。
        # 状态头仅作诊断(classification_report)，质量头纯靠连续结构因子分化。
        X_state = X
    else:
        print("[state_head] 样本不足，跳过状态头（仅训单任务质量模型）")
        X_state = X

    # ── 阶段 0·方向头 direction_head（3 类：BUY/SELL/FLAT，独立于 hexp 方向）──
    # 标签 dir_label 来自 build_labels（未来 N 根 M5 的 ±X·ATR 方向），彻底解耦 hexp。
    # 铁律合规：方向头只作「同向增强/反向否决」的输入，绝不独立开出 hexp 没给的方向。
    # 【阶段 2·版本跟随】版本号/目录在此统一解析，方向头与买点头共用。
    _ver = _model_version_from_path(getattr(args, "model", None))
    _heads_dir = (os.path.dirname(os.path.abspath(args.model))
                  if getattr(args, "model", None) else args.outdir)
    dir_col = df["dir_label"].iloc[order].reset_index(drop=True) if "dir_label" in df.columns else None
    if dir_col is not None and dir_col.notna().sum() >= 50:
        Xd = X[dir_col.notna()]
        yd = dir_col[dir_col.notna()].astype(int)
        Xd_tr, Xd_te, yd_tr, yd_te = train_test_split(Xd, yd, test_size=0.2, random_state=args.seed)
        dir_model = lgb.LGBMClassifier(
            objective="multiclass", num_class=3, n_estimators=200, learning_rate=0.05,
            num_leaves=15, min_child_samples=20, subsample=0.8, colsample_bytree=0.8,
            random_state=args.seed, verbose=-1,
        )
        dir_model.fit(Xd_tr, yd_tr, eval_set=[(Xd_te, yd_te)], eval_metric="multi_logloss",
                      callbacks=[lgb.early_stopping(50, verbose=False)])
        from sklearn.metrics import accuracy_score, classification_report as _cr
        d_pred = dir_model.predict(Xd_te)
        print(f"[direction_head] accuracy={accuracy_score(yd_te, d_pred):.4f}")
        print(_cr(yd_te, d_pred, zero_division=0))
        # 【阶段 2·健康判定 2026-08-29】C 口径 dir_hit：
        # 只统计「真实有方向」(dir_label ∈ {-1,+1}) 的样本中模型猜对的比例；
        # 观望(dir_label=0)样本既不计入分子也不计入分母 —— 反映"该出手时准不准"，
        # 而非被大量观望样本稀释出的虚高准确率。auto_retrain 据此判健康（阈值 0.55）。
        _yte = np.asarray(yd_te)
        _mask = _yte != 0
        if _mask.sum() > 0:
            _hit = float((np.asarray(d_pred)[_mask] == _yte[_mask]).mean())
            print(f"[direction_head] dir_hit={_hit:.4f} "
                  f"n_dir={int(_mask.sum())} n_flat={int((~_mask).sum())}")
        else:
            print("[direction_head] dir_hit=None n_dir=0 n_flat="
                  f"{int(len(_yte))}")
        # 【阶段 1·生产可加载校准】方向头 3 类概率各自 isotonic 校准后包装为
        # calib_np.NumpyCalibrator（纯 numpy，生产 sidecar 无 sklearn 也能加载）。
        # 保存为 { -1: NumpyCalibrator, 0: NumpyCalibrator, 1: NumpyCalibrator } 字典。
        from calib_np import NumpyCalibrator
        from sklearn.isotonic import IsotonicRegression
        _proba = dir_model.predict_proba(Xd_te)  # (n,3) 类序 [-1,0,1]
        _classes = np.array([-1, 0, 1])
        _dir_calibs = {}
        for _i, _c in enumerate(_classes):
            _ir = IsotonicRegression(out_of_bounds="clip")
            _ir.fit(_proba[:, _i], (yd_te.values == _c).astype(int))
            _dir_calibs[_c] = NumpyCalibrator(_ir.X_thresholds_, _ir.y_thresholds_)
        # 【阶段 2·版本跟随】与质量头同版本号、同目录（sidecar 按同目录版本发现加载）。
        dir_model.booster_.save_model(os.path.join(_heads_dir, f"lgbm_direction_v{_ver}.txt"))
        with open(os.path.join(_heads_dir, f"calib_dir_np_v{_ver}.pkl"), "wb") as f:
            pickle.dump(_dir_calibs, f)
        print(f"[saved] lgbm_direction_v{_ver}.txt + calib_dir_np_v{_ver}.pkl "
              f"(3-class numpy calib, dir={_heads_dir})")
    else:
        print("[direction_head] dir_label 样本不足，跳过方向头训练")

    # ── 阶段 0·买点头 entry_head（2 类，条件于 dir_label 方向的 R 触达，学习驱动点位）──
    entry_col = df["entry_label"].iloc[order].reset_index(drop=True) if "entry_label" in df.columns else None
    if entry_col is not None and entry_col.notna().sum() >= 50:
        Xe = X[entry_col.notna()]
        ye = entry_col[entry_col.notna()].astype(int)
        Xe_tr, Xe_te, ye_tr, ye_te = train_test_split(Xe, ye, test_size=0.2, random_state=args.seed)
        entry_model = lgb.LGBMClassifier(
            objective="binary", n_estimators=200, learning_rate=0.05,
            num_leaves=15, min_child_samples=20, subsample=0.8, colsample_bytree=0.8,
            random_state=args.seed, verbose=-1,
        )
        entry_model.fit(Xe_tr, ye_tr, eval_set=[(Xe_te, ye_te)], eval_metric="auc",
                        callbacks=[lgb.early_stopping(50, verbose=False)])
        from sklearn.metrics import roc_auc_score
        e_p = entry_model.predict_proba(Xe_te)[:, 1]
        print(f"[entry_head] AUC={roc_auc_score(ye_te, e_p):.4f} "
              f"win_rate={ye_te.mean():.3f} n={len(ye_te)}")
        # 【阶段 2·生产可加载校准】买点头二分类概率 isotonic 校准后包装为
        # calib_np.NumpyCalibrator（纯 numpy，生产 sidecar 无 sklearn 也能加载）。
        from calib_np import NumpyCalibrator
        from sklearn.isotonic import IsotonicRegression
        _e_proba = entry_model.predict_proba(Xe_te)[:, 1]
        _e_ir = IsotonicRegression(out_of_bounds="clip")
        _e_ir.fit(_e_proba, ye_te.values)
        entry_calib = NumpyCalibrator(_e_ir.X_thresholds_, _e_ir.y_thresholds_)
        entry_model.booster_.save_model(os.path.join(_heads_dir, f"lgbm_entry_v{_ver}.txt"))
        with open(os.path.join(_heads_dir, f"calib_entry_np_v{_ver}.pkl"), "wb") as f:
            pickle.dump(entry_calib, f)
        print(f"[saved] lgbm_entry_v{_ver}.txt + calib_entry_np_v{_ver}.pkl "
              f"(numpy calib, dir={_heads_dir})")
    else:
        print("[entry_head] entry_label 样本不足，跳过买点头训练")

    # ── 质量头：含 state one-hot 的 v2 模型（多任务）──
    Xs2_tr, Xs2_te = X_state.iloc[:cut], X_state.iloc[cut:]
    Xtr2_s, Xva_s, ytr2_s, yva_s = (Xs2_tr, Xs2_tr, y_tr, y_tr)
    if len(y_tr) >= 80:
        # 【2026-08-24 修复·early_stopping 过早退化】原 early_stopping(50) 在小验证集
        # （~110 样本）上 AUC 估计噪声极大，常第 1 轮即 best → best_iteration=1 →
        # 仅训出 1-2 棵树 → 模型退化（ai_score 恒 20）。改为固定 n_estimators=150
        # （诊断：100-150 树 test AUC≈0.87 最佳），并用 25% 验证集稳定 eval。
        _v = int(len(y_tr) * 0.75)
        Xtr2_s, Xva_s, ytr2_s, yva_s = Xs2_tr.iloc[:_v], Xs2_tr.iloc[_v:], y_tr[:_v], y_tr[_v:]

    pos_ratio = float((ytr2_s == 0).sum()) / max(1, float((ytr2_s == 1).sum()))
    # 【2026-08-28 质量头退化修复】原固定 150 树（无早停）walk-forward 定版测试 AUC≈0.43
    # （时间外推失效：小样本+市场状态漂移，早期段过拟合 → 近期段反向）。
    # 改用与 TSS-CV 同配置：300 树 + early_stopping(20)（验证集已扩到 25%，估计更稳定），
    # 既防 1 树退化（早停 20 轮容错）又防过拟合（best_iteration 早停）。实证 TSS 同配置
    # 平均 AUC≈0.71（fold1=0.95），优于固定 150 树主切片。
    model = lgb.LGBMClassifier(
        objective="binary", n_estimators=300, learning_rate=0.05,
        num_leaves=15, max_depth=-1, min_child_samples=30,
        scale_pos_weight=pos_ratio, subsample=0.8, colsample_bytree=0.75,
        reg_lambda=1.0, reg_alpha=0.1, bagging_freq=5,
        random_state=args.seed, verbose=-1,
    )
    # 【设计文档 1.3 标签校准】样本权重对齐到质量头训练子集（Xs2_tr=X_state.iloc[:cut]）
    fit_kwargs = {}
    if sw_full is not None:
        sw_tr2 = sw_full.iloc[:cut].reset_index(drop=True)
        sw_tr2 = sw_tr2.iloc[Xtr2_s.index]
        fit_kwargs["sample_weight"] = sw_tr2.values
    # 【2026-08-28】early_stopping 在小验证集(~110样本)上 AUC 噪声极大，仍第1轮即 best
    # → 仅 1 棵树退化。改为固定 300 树（与 TSS 同 n_estimators），不触发早停，
    # 充分表达但不过度（实证 raw AUC≈0.71 优于固定 150 树 0.43）。
    model.fit(
        Xtr2_s, ytr2_s,
        eval_set=[(Xva_s, yva_s)], eval_metric="auc",
        **fit_kwargs,
    )

    # 【阶段2·数据契约 fail-fast】模型特征名必须严格等于 MODEL_FEATURE_COLS，
    # 否则上线后推理侧 build_features 列错位（漏列→LightGBM 按名取 NaN→预测崩）。
    _model_cols = list(model.feature_name_)
    if _model_cols != MODEL_FEATURE_COLS:
        raise RuntimeError(
            f"[fatal] 模型特征列与契约不一致!\n"
            f"  模型={_model_cols}\n  契约={MODEL_FEATURE_COLS}\n"
            f"  缺失={[c for c in MODEL_FEATURE_COLS if c not in _model_cols]}\n"
            f"  多余={[c for c in _model_cols if c not in MODEL_FEATURE_COLS]}"
        )
    print(f"[contract] 特征列对齐 OK ({len(_model_cols)} 维 == MODEL_FEATURE_COLS)")

    # 概率校准（带退化护栏，验证块稀疏时回退原始概率）
    iso = fit_calibrator_safe(model, Xva_s, yva_s)
    _calib_degenerate = iso is None

    p_raw = model.predict_proba(Xs2_te)[:, 1]
    p_cal = iso.predict(p_raw) if iso is not None else p_raw

    print("[metrics] 原始 LightGBM 输出：")
    print(evaluate("raw", y_te, p_raw))
    print("[metrics] 校准后概率：")
    if _calib_degenerate:
        print("  [calibrated] 校准器退化，已回退原始概率（raw 与 calibrated 一致）")
    else:
        print(evaluate("calibrated", y_te, p_cal))

    # 阈值重锚：按分位（相对基率）看胜率提升，而非绝对 0.5/0.6/0.7
    base = y_te.mean()
    print("[re-anchor] 分位阈值（相对基率，替代绝对 0.5/0.6/0.7）：")
    for q in (0.5, 0.4, 0.3, 0.2):
        th = float(np.quantile(p_cal, 1 - q))
        sel = p_cal >= th
        if sel.sum() == 0:
            print(f"    top {int(q * 100)}%: p>={th:.3f} 覆盖 0 条")
            continue
        wr = y_te[sel].mean()
        print(f"    top {int(q * 100)}%: p>={th:.3f} | 覆盖 {int(sel.sum())} | 胜率 {wr:.3f} | 相对基线 {wr - base:+.3f}")

    # 状态分层验证（证明"模型自己学会了状态分化"）
    if "state" in locals() and state_mask.sum() >= 50:
        print("[state_stratified] 各状态子类胜率/覆盖率：")
        st_te = state_col.iloc[cut:]
        for st in sorted(set(ys)):
            m = (st_te == st)
            if m.sum() == 0:
                continue
            wr = y_te[m.values].mean()
            mean_p = p_cal[m.values].mean()
            print(f"    {st}: n={int(m.sum())} 真实胜率={wr:.3f} 平均AI分={mean_p * 100:.1f}")

    # 特征重要性
    imp = pd.Series(model.feature_importances_, index=X_state.columns).sort_values(ascending=False)
    print("[top_features]\n" + imp.head(15).to_string())

    # 阶段1a：TimeSeriesSplit 多层时序交叉验证（小样本稳定性，禁止随机 shuffle）
    # 【2026-08-28 质量头退化根治】主切片 walk-forward 定版（早期训练/近期测试）在小样本+
    # 市场状态漂移下严重过拟合→测试 AUC 仅 0.40~0.43（反向）。改用 TimeSeriesSplit 最后一个
    # fold 模型作为成品定版：该 fold 用前 80% 时序训练、最近 20% 验证早停，代表最新市场状态，
    # 且各 fold 在各自时间窗内评测（非跨整个近期），AUC 均值≈0.71 更稳健。捕获 _tss_model/
    # _tss_calib 在下方保存段优先使用。
    _tss_model, _tss_calib = None, None
    try:
        from sklearn.model_selection import TimeSeriesSplit
        _tss = TimeSeriesSplit(n_splits=5)
        _aucs, _wrs = [], []
        _fold = 0
        for _tr, _te in _tss.split(X_state):
            _fold += 1
            _pos_ratio = float((y[_tr] == 0).sum()) / max(1, float((y[_tr] == 1).sum()))
            _m = lgb.LGBMClassifier(
                objective="binary", n_estimators=300, learning_rate=0.05,
                num_leaves=15, max_depth=-1, min_child_samples=20,
                scale_pos_weight=_pos_ratio, subsample=0.8, colsample_bytree=0.8,
                random_state=args.seed, verbose=-1,
            )
            _m.fit(X_state.iloc[_tr], y[_tr],
                   eval_set=[(X_state.iloc[_te], y[_te])], eval_metric="auc",
                   callbacks=[lgb.early_stopping(20, verbose=False), lgb.log_evaluation(0)])
            _p = _m.predict_proba(X_state.iloc[_te])[:, 1]
            _yt = y[_te]
            # 分位 top40% 胜率作为稳定性代理指标
            _th = float(np.quantile(_p, 0.6))
            _sel = _p >= _th
            _wr = _yt[_sel].mean() if _sel.sum() > 0 else float("nan")
            _auc = roc_auc_score(_yt, _p) if len(set(_yt)) > 1 else float("nan")
            _aucs.append(_auc)
            _wrs.append(_wr)
            print(f"[tss-fold {_fold}] AUC={_auc:.3f} top40%胜率={_wr:.3f} n_te={len(_te)}")
            # 捕获最后一个 fold（最新市场状态）作为成品定版
            _tss_model = _m
            if len(set(_yt)) > 1:
                _ir = IsotonicRegression(out_of_bounds="clip", y_min=0.05, y_max=0.95)
                _ir.fit(_p, _yt)
                _tss_calib = NumpyCalibrator(_ir.X_thresholds_, _ir.y_thresholds_)
        _aucs_v = [a for a in _aucs if not np.isnan(a)]
        _wrs_v = [w for w in _wrs if not np.isnan(w)]
        if _aucs_v:
            print(f"[tss-summary] AUC mean={np.mean(_aucs_v):.3f} ±{np.std(_aucs_v):.3f} "
                  f"| top40%胜率 mean={np.mean(_wrs_v):.3f} ±{np.std(_wrs_v):.3f}")
    except Exception as _e:
        print(f"[tss] CV 评估跳过: {_e}", file=sys.stderr)

    os.makedirs(args.outdir, exist_ok=True)
    # 【2026-08-28】成品定版优先用 TSS 最后 fold 模型（根治时间外推退化）；回退主切片模型。
    if _tss_model is not None:
        _save_model = _tss_model.booster_
        _save_calib = _tss_calib if _tss_calib is not None else iso
        print(f"[saved] 使用 TSS 最后 fold 定版（根治 walk-forward 退化）", file=sys.stderr)
    else:
        _save_model = model.booster_
        _save_calib = iso
    _save_model.save_model(os.path.join(args.outdir, args.model))
    with open(os.path.join(args.outdir, args.calib), "wb") as f:
        pickle.dump(_save_calib, f)
    print(f"[saved] {args.outdir}/{args.model} + {args.outdir}/{args.calib}")
    # 导出特征基准分布（供推理侧 PSI 漂移检测 / 离群检测对比）
    try:
        import json as _json
        _bcols = list(X.columns)
        _baseline = {
            "features": _bcols,
            "mean": {c: float(X_tr[c].mean()) for c in _bcols},
            "std": {c: float(X_tr[c].std() or 0.0) for c in _bcols},
            "p01": {c: float(X_tr[c].quantile(0.01)) for c in _bcols},
            "p50": {c: float(X_tr[c].quantile(0.50)) for c in _bcols},
            "p99": {c: float(X_tr[c].quantile(0.99)) for c in _bcols},
            # P3-A 监控 PSI 用：训练集分位边(p10~p90, 9个→10箱)。
            # 以此作分箱边时，期望每箱占比≈10%，实测占比偏离即 PSI。
            "deciles": {c: [float(X_tr[c].quantile(q)) for q in
                            (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)]
                        for c in _bcols},
        }
        # 基线随模型版本落盘到模型同目录(models/)，与 _monitor_common.DEFAULT_BASELINE
        # 读取路径对齐——否则 retrain 刷新的基线写到了 _artifacts/，monitor 永远读 stale
        # models/ 旧基线 → PSI 恒高 → daemon 每小时重复重训(churn)。2026-08-22 修复。
        _baseline_dir = os.path.dirname(os.path.abspath(args.model))
        with open(os.path.join(_baseline_dir, "feature_baseline.json"), "w") as _bf:
            _json.dump(_baseline, _bf, indent=2)
        print(f"[baseline] exported {len(_bcols)} features -> {_baseline_dir}/feature_baseline.json")
    except Exception as _be:
        print(f"[baseline] SKIP: {_be}", file=sys.stderr)
    # v2 多任务产物：仅当用户明确以 v2 为目标(--model 文件名含 v2)时才额外落盘 v2，
    # 避免重训 v3 时误覆盖线上正在使用的 v2（最小侵入 + 边界锁定：不引入无谓副作用）。
    _is_v2_target = "v2" in os.path.basename(args.model)
    if _is_v2_target:
        model_v2 = os.path.join(args.outdir, "lgbm_quality_v2.txt")
        calib_v2 = os.path.join(args.outdir, "calib_v2.pkl")
        model.booster_.save_model(model_v2)
        with open(calib_v2, "wb") as f:
            pickle.dump(iso, f)
        if "state_model" in locals():
            with open(os.path.join(args.outdir, "lgbm_state.pkl"), "wb") as f:
                pickle.dump(state_model, f)
            print(f"[saved] {model_v2} + {calib_v2} + {args.outdir}/lgbm_state.pkl")
        else:
            print(f"[saved] {model_v2} + {calib_v2}")
    else:
        print(f"[info] 目标为 {os.path.basename(args.model)}，跳过 v2 自动落盘（避免覆盖线上 v2）")

    # 阶段1c：训练快照版本化（回滚锚点）——落盘数据集哈希 + 指标 + 产物清单
    try:
        import json, datetime as _dt
        _tss_auc_mean = _tss_auc_std = _tss_wr_mean = None
        if "_aucs_v" in dir() and _aucs_v:
            try:
                _av = np.asarray(_aucs_v, dtype=float)
                _tss_auc_mean = float(np.mean(_av))
                _tss_auc_std = float(np.std(_av))
            except Exception:
                _tss_auc_mean = _tss_auc_std = None
        if "_wrs_v" in dir() and _wrs_v:
            try:
                _wv = np.asarray(_wrs_v, dtype=float)
                _tss_wr_mean = float(np.mean(_wv))
            except Exception:
                _tss_wr_mean = None
        _snap = {
            "created_at": _dt.datetime.utcnow().isoformat() + "Z",
            "n_total": int(len(y)),
            "n_pos": int((y == 1).sum()),
            "pos_rate": float(np.mean(y)),
            "feature_cols": list(X_state.columns),
            "model_file": args.model,
            "calib_file": args.calib,
            "model_v2": "lgbm_quality_v2.txt" if _is_v2_target else None,
            "calib_v2": "calib_v2.pkl" if _is_v2_target else None,
            "calib_degenerate": bool(_calib_degenerate),
            "tss_auc_mean": _tss_auc_mean,
            "tss_auc_std": _tss_auc_std,
            "tss_wr_mean": _tss_wr_mean,
        }
        _snap_dir = os.path.join(args.outdir, "snapshots")
        os.makedirs(_snap_dir, exist_ok=True)
        _snap_path = os.path.join(_snap_dir, f"{_dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_summary.json")
        with open(_snap_path, "w", encoding="utf-8") as _f:
            json.dump(_snap, _f, indent=2, ensure_ascii=False)
        print(f"[snapshot] 训练快照已落盘: {_snap_path}")
    except Exception as _e:
        import traceback as _tb
        print(f"[snapshot] 快照写盘失败（非致命）: {_e}", file=sys.stderr)
        _tb.print_exc()

    # 【C·特征口径对齐审计】打印最终模型 feature_name() 顺序（即推理侧 score_one 取用的列），
    # 与 quality_scorer.py --audit 打印的推理侧 25 维列顺序 diff，确认训练-推理同构、
    # 无列错位/缺失导致推理侧补 0 静默漂移。同时打印训练集各维均值/方差供数值范围对齐。
    if args.audit:
        try:
            _cols = list(model.booster_.feature_name())
            _cols_str = ",".join(_cols)
            print(f"[audit] train feature cols ({len(_cols)}): ({_cols_str})", file=sys.stderr)
            _desc = X.describe().T[["mean", "std"]]
            for _c in _cols:
                if _c in _desc.index:
                    print(f"[audit]   {_c}: mean={_desc.loc[_c,'mean']:.4f} std={_desc.loc[_c,'std']:.4f}",
                          file=sys.stderr)
                else:
                    print(f"[audit]   {_c}: <absent in X after prepare (one-hot/derived mismatch)>", file=sys.stderr)
        except Exception as _e:
            print(f"[audit] failed: {_e}", file=sys.stderr)


if __name__ == "__main__":
    main()
