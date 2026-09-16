"""train_onset_model.py — **趋势起点预测模型**（显式任务，替代从 4 类 argmax 推导触发器）。

依据（方案 §28）：把"预测起点"做成**显式二值任务**后，可探测提前量上限实测 ≈20 bar，
且 L=5 达峰 AUC **0.7599**；而现有链路（4 类 argmax + 防抖）只能做到 **0 bar**。
根因是**训练目标错位**：从未有人要求模型预测"起点"，而 `trend_init` 恰是最不可分的一类
（两两 AUC 0.535，§22）。本文件即为该显式任务的训练器。

任务定义（`make_onset_target`，**本函数是全仓库唯一实现**，验收门从此处导入）：
    对 bar k（仅取 gt[k] == False，即"当前不处于趋势"）
        y_k = 1  ⟺  未来 (k, k+L] 内出现趋势起点（gt 的上升沿）
    gt ≡ (label_id ≠ 0)，即"未来窗口处于趋势"（前视口径，故编码未来 ⇒ 有提前量可能）
    ⚠ 边界：只在**片段内部**寻找起点 —— 构造训练集时须丢弃该段末尾 L 根，
      否则会用到下一段的信息（走前式评估下即为泄露）。

产出：`lgbm_onset_{tf}_v{N}_s{k}.txt` × K 个种子 + `..._meta.json`
      meta 含：lead / **决策阈值（按 OOF 最大 F1 标定）** / 特征列 / OOF 指标 / PR 曲线表

用法：
    python train_onset_model.py --csv _scratch/state_M5_v2.csv --tf M5 --version 1 --lead 5
    python train_onset_model.py --csv ... --lead 3 --splits 5 --seeds 5
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
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

MODELS_DIR_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

# 类别顺序即模型输出列序（契约）
ONSET_NAMES = ["no_onset", "onset"]


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


# ── 【为什么需要 min_quiet（2026-09-16 根因修复）】────────────────────────────
# 实测线上模型 `lgbm_onset_M5_v1` 的 `train_pos_rate = 0.9493` —— 即"未来 5 根内出现
# 起点"在震荡 bar 上有 **94.9% 为真** ⇒ 事件不再是"事件"而是"常态" ⇒ 任务退化：
#   · 模型 P(onset) 被压成恒 1（实测 p50=0.9996 / p90=0.9999）；
#   · 标定出的决策阈值落到 **0.99984**（meta.threshold），毫无判别力；
#   · 退化继续向下游传导：生产 `state.trigger.rise_thr = 0.2948` 下 ΔP 触发率仅
#     **0.825%**（按目标触发率 10% 标定应为 0.002301）⇒ 趋势入口事实上关闭
#     （全历史 trigger_on = 1/363 = 0.28%），与铁律「及时判断行情使用对应交易策略」冲突。
# 机制：`gt`（未来窗口处于趋势）占比高达 78~82% ⇒ **震荡段极短** ⇒ 旧判据
#   `valid = ~gt` 会把"刚离开趋势的第 1 根"也纳入样本，而这类 bar 之后几乎必然
#   又出现上升沿 ⇒ **必然正例**污染训练集。
# 修法：要求"当前已**连续**处于震荡 ≥ min_quiet 根"。`min_quiet=1` 与旧行为
#   **逐位等价**（`run>=1 ⟺ ~gt`）⇒ 默认零变化、可一行回滚。
# 标定口径：以 `[task] 正例率` 落到 **10%~30%** 为目标（不预猜数值，实测反解）。
def make_onset_target(gt: np.ndarray, lead: int, min_quiet: int = 1,
                      min_run: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """构造起点预测目标。**唯一实现**（验收门亦导入本函数）。

    Args:
        gt: 布尔序列，True = 该 bar 处于趋势（label ≠ oscillation）。
            **必须是片段内的序列**：调用方须保证 gt 不含跨段信息，否则边界会泄露。
        lead: 提前量 L（bar 数）。

    Returns:
        (valid, y)：长度与 gt 相同。
        valid[k]=False 表示该 bar 不参与（处于趋势中、或 k+L 越界）。
    """
    gt = np.asarray(gt, dtype=bool)
    n = len(gt)
    valid = np.zeros(n, dtype=bool)
    y = np.zeros(n, dtype=int)
    L = int(lead)
    hi = n - L
    if hi <= 0 or L < 1:
        return valid, y
    edge = gt & ~np.concatenate(([False], gt[:-1]))
    # 【min_run：只保留"其后趋势段持续 ≥ min_run 根"的上升沿】—— 与 min_quiet 配对。
    #   为什么两把旋钮都要（2026-09-16 实测，标签全历史 68687 根）：
    #     · gt 趋势占比 79.8%（trend_mid 48.0 + fade 19.6 + init 15.3；震荡仅 17.1）
    #       ⇒ 起点 4102 次、"未来 5 根内出现起点"正例率 **94.1%** ⇒ 事件退化为常态；
    #     · **仅靠 min_quiet 压不下来**：L=5 时即便要求"已连续震荡 ≥15 根"，
    #       正例率仍 46.1% 且可用样本只剩 102（< 训练下限 500）⇒ 实测否决单旋钮方案；
    #     · 故还需在**趋势侧**剔除"一闪而过"的假起点，让事件真正稀有。
    #   目标：正例率落到 10%~30% 且可用样本 ≥500（不预猜数值，实测反解）。
    #   `min_run=1` ⇒ 保留全部上升沿 ⇒ 与旧行为**逐位等价**（默认零变化、可一行回滚）。
    _min_run = max(1, int(min_run))
    if _min_run > 1:
        _keep = np.zeros(n, dtype=bool)
        for _s in np.where(edge)[0]:
            _e = _s
            while _e + 1 < n and gt[_e + 1]:
                _e += 1
            if _e - _s + 1 >= _min_run:
                _keep[_s] = True
        edge = _keep
    cum = np.concatenate(([0], np.cumsum(edge.astype(int))))
    ks = np.arange(hi)
    cnt = cum[ks + 1 + L] - cum[ks + 1]      # (k, k+L] 内起点数
    # `run[k]` = 截至 k 的"连续非趋势"根数（含 k 自身）；gt[k] 为真时为 0。
    # min_quiet=1 时 `run>=1 ⟺ ~gt` ⇒ 与旧行为**逐位等价**（默认零变化）。
    _idx = np.arange(n)
    run = _idx - np.maximum.accumulate(np.where(gt, _idx, -1))
    valid[ks] = ~gt[ks] & (run[ks] >= max(1, int(min_quiet)))
    y[ks] = (cnt > 0).astype(int)
    return valid, y


def pick_threshold(y: np.ndarray, p: np.ndarray, grid: int = 60) -> dict:
    """按 **OOF 上的最大 F1** 选决策阈值（规则事先声明，不事后挑分位）。

    同时输出 PR 曲线表，便于人工复核取舍（如"要更高精度"时改选保守阈值）。
    """
    qs = np.linspace(0.5, 0.995, grid)
    thr_c = np.quantile(p, qs)
    best = None
    curve = []
    for t in np.unique(thr_c):
        pred = p >= t
        tp = float((pred & (y == 1)).sum())
        fp = float((pred & (y == 0)).sum())
        fn = float((~pred & (y == 1)).sum())
        prec = tp / max(1.0, tp + fp)
        rec = tp / max(1.0, tp + fn)
        f1 = 2 * prec * rec / max(1e-12, prec + rec)
        on_rate = float(pred.mean())
        curve.append({"thr": float(t), "precision": prec, "recall": rec,
                      "f1": f1, "on_rate": on_rate})
        if best is None or f1 > best["f1"]:
            best = curve[-1]
    return {"best": best, "curve": curve}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="build_state_labels.py 的产物")
    ap.add_argument("--tf", required=True, choices=["M5", "M15", "H1"])
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument("--lead", type=int, default=5,
                    help="提前量 L（实测 L=5 的 AUC 最高，见方案 §28.1）")
    ap.add_argument("--min-quiet", type=int, default=1,
                    help="只取「当前已连续震荡 ≥ N 根」的 bar 作为样本（默认 1 = 既有行为，"
                         "零变化）。理由见 make_onset_target 上方注释。")
    ap.add_argument("--min-run", type=int, default=1,
                    help="只把「其后趋势段持续 ≥ N 根」的上升沿计为起点事件（默认 1 = "
                         "保留全部上升沿，零变化）。与 --min-quiet 配对使用，见同处注释。")
    ap.add_argument("--target", default="onset", choices=["onset", "vol"],
                    help="训练目标：onset（默认，4 类标签导出'未来 L 根内起点'）；"
                         "vol（路线 B：直接读标签管线的 vol_expansion 列，不在本脚本另算）")
    ap.add_argument("--feature-set", default="base", choices=["base", "l1"],
                    help="特征集：base = STATE_FEATURE_COLS(27)；"
                         "l1 = base + L1_FEATURE_COLS（量价/点差 6 列）。"
                         "默认 base（与既有 onset 模型契约一致，零变化）")
    ap.add_argument("--seeds", type=int, default=5, help="最终模型 bagging 种子数")
    ap.add_argument("--splits", type=int, default=5, help="时序 OOF 折数")
    ap.add_argument("--outdir", default=MODELS_DIR_DEFAULT)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import lightgbm as lgb

    here = os.path.dirname(os.path.abspath(__file__))
    sf = _load("state_features", os.path.join(
        os.path.dirname(here), "hcm-signal-tower", "signal_tower", "state_features.py"))
    tsm = _load("train_state_model", os.path.join(here, "train_state_model.py"))
    cols = list(sf.STATE_FEATURE_COLS_L1 if args.feature_set == "l1"
                else sf.STATE_FEATURE_COLS)

    df = pd.read_csv(args.csv)
    miss = [c for c in cols if c not in df.columns]
    if miss:
        raise SystemExit(f"[fatal] 缺特征列：{miss}")
    # ── 目标选择（--target）──────────────────────────────────────────────
    #   onset（默认，零变化）：由 4 类标签导出"未来 L 根内出现趋势起点"。
    #   vol （路线 B）：**直接读标签管线给出的 `vol_expansion` 列** —— 该列的唯一实现点是
    #        `build_state_labels.py`（阈值 `state.label.vol_amp_min`）。本脚本**不再另算目标**，
    #        避免出现第二份"波动扩张"定义（本仓库红线）。
    #        波动目标对**每根 bar** 都有定义（不经 4 类置信过滤）⇒ 样本量显著更大。
    if args.target == "vol":
        if "vol_expansion" not in df.columns:
            raise SystemExit("[fatal] CSV 缺 vol_expansion 列"
                             "（需 build_state_labels.py 新版产出）")
        df = df[df["vol_expansion"].notna()].copy()
        df = df.sort_values("open_time").reset_index(drop=True)
        valid = df["vol_expansion"].notna().to_numpy()
        y = df["vol_expansion"].to_numpy(dtype=int)
        gt = (y > 0)
        print(f"[data] tf={args.tf} 样本 {len(df)}；gt 波动扩张占比 {gt.mean():.1%}")
    else:
        df = df[df["label_id"].notna()].copy()
        df["label_id"] = df["label_id"].astype(int)
        df = df.sort_values("open_time").reset_index(drop=True)
        gt = (df["label_id"].to_numpy() != 0)
        valid, y = make_onset_target(gt, args.lead, args.min_quiet, args.min_run)

    idx = np.where(valid)[0]
    if len(idx) < 500:
        raise SystemExit(f"[fatal] 可用样本过少：{len(idx)}（调小 --lead 或补数据）")
    X = df[cols].astype(float).iloc[idx].reset_index(drop=True)
    ys = y[idx]

    if args.target == "vol":
        print(f"[task] target=vol（波动扩张）  可用样本 {len(idx)}  "
              f"正例率 {ys.mean():.1%}（验收：正例率 ∈[20%,40%]）")
    else:
        print(f"[data] gt 趋势占比 {gt.mean():.1%}；"
              f"真值起点 {int((gt & ~np.concatenate(([False], gt[:-1]))).sum())} 次")
        print(f"[task] lead={args.lead} min_quiet={args.min_quiet} min_run={args.min_run}  "
              f"可用样本 {len(idx)}  正例率 {ys.mean():.1%}"
              f"（标定目标：10%~30% 且样本 ≥500；过高 = 事件退化为常态）")

    def mk():
        return lgb.LGBMClassifier(objective="binary", random_state=42,
                                  n_jobs=-1, verbose=-1, **tsm.DEFAULTS)

    # ── 时序 OOF：用于无偏评估与阈值标定 ──
    tss = TimeSeriesSplit(n_splits=args.splits)
    oof = np.full(len(ys), np.nan)
    for tr, te in tss.split(X):
        if len(np.unique(ys[tr])) < 2:
            continue
        m = mk()
        m.fit(X.iloc[tr], ys[tr])
        oof[te] = m.predict_proba(X.iloc[te])[:, 1]
    cov = ~np.isnan(oof)
    auc = float(roc_auc_score(ys[cov], oof[cov])) if len(np.unique(ys[cov])) > 1 else float("nan")
    thr_info = pick_threshold(ys[cov], oof[cov])
    best = thr_info["best"]
    print(f"\n===== 时序 OOF（{args.splits} 折，样本 {int(cov.sum())}）=====")
    print(f"  AUC = {auc:.4f}   正例率 = {ys[cov].mean():.4f}")
    print(f"  阈值标定（按最大 F1）= {best['thr']:.4f} | "
          f"precision={best['precision']:.4f} recall={best['recall']:.4f} "
          f"F1={best['f1']:.4f} | 触发占比={best['on_rate']:.1%}")
    print("\n  PR 曲线抽样（如需更高精度，可改选更保守阈值）：")
    curve = sorted(thr_info["curve"], key=lambda c: -c["thr"])
    step = max(1, len(curve) // 8)
    for c in curve[::step][:8]:
        print(f"    thr={c['thr']:.4f}  P={c['precision']:.3f}  R={c['recall']:.3f}  "
              f"F1={c['f1']:.3f}  触发={c['on_rate']:.1%}")

    # ── 最终 bagging 模型（全量训练）──
    os.makedirs(args.outdir, exist_ok=True)
    paths, imp = [], np.zeros(len(cols))
    for s in range(args.seeds):
        m = lgb.LGBMClassifier(objective="binary", random_state=100 + s,
                               n_jobs=-1, verbose=-1, **tsm.DEFAULTS)
        m.fit(X, ys)
        # 文件名前缀随目标变化：onset 保持既有命名（向后兼容），vol 用 `lgbm_vol_*`。
        # 两者语义不同（起点 vs 波动扩张），不可互相加载 ⇒ 必须靠文件名区分。
        name = f"lgbm_{args.target}_{args.tf}_v{args.version}_s{s}.txt"
        m.booster_.save_model(os.path.join(args.outdir, name))
        paths.append(name)
        imp += m.feature_importances_
    imp /= args.seeds
    top = sorted(zip(cols, imp), key=lambda t: -t[1])[:10]
    print("\n[top_features] " + ", ".join(f"{c}={v:.0f}" for c, v in top))

    meta = {
        "tf": args.tf, "version": args.version, "task": args.target,
        "lead": (int(args.lead) if args.target == "onset" else None),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "feature_cols": cols,
        "class_names": ONSET_NAMES,
        "threshold": float(best["thr"]),
        "threshold_rule": "OOF 上最大 F1（事先声明，非事后挑分位）",
        "n_seeds": args.seeds,
        "model_files": paths,
        "lgbm_params": tsm.DEFAULTS,
        "train_rows": int(len(X)),
        "train_pos_rate": float(ys.mean()),
        "eval_oof": {"auc": auc, **{k: float(v) for k, v in best.items()}},
        "pr_curve": thr_info["curve"],
        "feature_importance": {c: float(v) for c, v in zip(cols, imp)},
    }
    meta_path = os.path.join(args.outdir, f"lgbm_onset_{args.tf}_v{args.version}_meta.json")
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"\n[out] {len(paths)} models + meta → {args.outdir}")
    print(f"[out] meta: {meta_path}")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        print(f"[out] report: {args.report}")
    print("\n[提示] 下一步必须用 eval_state_leadtime.py 的多折验收门量提前量，"
          "AUC 高不等于触发器可用（单折 regime 会骗人，见方案 §27）。")


if __name__ == "__main__":
    main()
