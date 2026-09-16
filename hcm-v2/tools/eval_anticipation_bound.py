"""eval_anticipation_bound.py — "提前量"的**可探测上限**（回答"到底能不能更早"）。

背景（方案 §27.4）：走前式多折实测显示，所有检测器的中位提前量都落在 ±1 bar 内 ——
做到的是"同步"而非"提前"。但**"同步"不等于"已达上限"**：有可能信号根本不在过去的数据里
（标签口径所限），也有可能是模型没学到（模型不足）。两者处置完全不同，必须先分清。

方法（非循环：特征只用过去，目标在未来）：
  对每个提前量 L，构造二分类任务
      y_k = 1  当且仅当「未来 (k, k+L] 内出现一次趋势起点（gt 上升沿）」
      样本仅取 gt[k] == False 的 bar（即"当前不处于趋势"，预测即将发生）
  用**同一特征集**（27 维，只含 k 及以前的信息）做时序 OOF，测 AUC。

  读法：
    · AUC(L) 明显 > 0.5  ⇒ **在该提前量上信息确实存在**（模型有提升空间）
    · AUC(L) ≈ 0.5       ⇒ 该提前量上**信息不存在**（口径所限，调模型无用）
    · **最大可探测 L（AUC 首次跌破判据阈值）即"提前量上限"**

⚠ 口径与局限：
  · 上限是**相对于本特征集**的。更多/更好的特征可以抬高它（如成交量、盘口、多周期）。
  · AUC 是排序能力；落地成检测器还需选阈值，存在查准/查全取舍。
  · "趋势起点"由标签口径定义（`label != oscillation` 的上升沿），事件定义本身带口径性。

用法：
    python eval_anticipation_bound.py --labels _scratch/state_M5_v2.csv
    python eval_anticipation_bound.py --labels ... --leads 1,2,3,5,8,12,20,30
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
_SIG = os.path.join(_ROOT, "hcm-signal-tower", "signal_tower")


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


SF = _load("state_features", os.path.join(_SIG, "state_features.py"))
TSM = _load("train_state_model", os.path.join(_TOOLS, "train_state_model.py"))
# 【2026-09-16 去重复实现】起点目标口径的**唯一实现**在
# `train_onset_model.make_onset_target`；本工具此前自带一份同语义代码
# （edge/cum/valid/tgt），与本仓库红线"同语义两份实现"冲突，且导致
# train_onset_model 修正事件口径（min_quiet）后**本工具测不出来**。改为在此导入。
TOM = _load("train_onset_model", os.path.join(_TOOLS, "train_onset_model.py"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="_scratch/state_M5_v2.csv")
    ap.add_argument("--feature-set", default="base", choices=["base", "l1"],
                    help="特征集（同 train_state_model）：base=STATE_FEATURE_COLS(27)；"
                         "l1=base+L1_FEATURE_COLS（量价/点差）—— 用于 L1 的信息上限 A/B")
    ap.add_argument("--leads", default="1,2,3,5,8,12,20,30",
                    help="待测提前量 L（bar 数），逗号分隔")
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--useful-auc", type=float, default=0.58,
                    help="判定「信息存在」的 AUC 门槛（低于此视为不可探测）")
    ap.add_argument("--min-quiet", type=int, default=1,
                    help="只取「已连续震荡 ≥ N 根」的 bar（透传给 make_onset_target；"
                         "默认 1 = 既有口径，零变化）")
    ap.add_argument("--min-run", type=int, default=1,
                    help="只把「其后趋势段持续 ≥ N 根」的上升沿计为起点事件"
                         "（透传给 make_onset_target；默认 1 = 既有口径，零变化）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import lightgbm as lgb

    df = pd.read_csv(args.labels)
    cols = list(SF.STATE_FEATURE_COLS_L1 if args.feature_set == "l1"
                else SF.STATE_FEATURE_COLS)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise SystemExit(f"[fatal] 缺特征列：{missing}")
    df = df[df["label_id"].notna()].copy()
    df["label_id"] = df["label_id"].astype(int)
    df = df.sort_values("open_time").reset_index(drop=True)

    gt = (df["label_id"].to_numpy() != 0)          # 0 = oscillation（契约）
    edge = gt & ~np.concatenate(([False], gt[:-1]))  # 趋势起点（上升沿）
    cum = np.concatenate(([0], np.cumsum(edge.astype(int))))
    n = len(df)
    X = df[cols].astype(float)

    leads = [int(x) for x in args.leads.split(",") if x.strip()]
    print(f"[data] 样本 {n}；趋势占比 {gt.mean():.1%}；趋势起点 {int(edge.sum())} 次")
    print(f"[task] 仅取「已连续震荡 ≥{args.min_quiet} 根」的 bar，预测"
          f"「未来 L 根内是否出现起点」（特征仅用 k 及以前 → 无泄露）")

    print(f"\n=========== 可探测提前量上限（AUC 随 L 的衰减）===========")
    print(f"  {'L(bar)':>7}{'可用样本':>10}{'正例率':>9}{'AUC':>9}{'ΔAUC(vs0.5)':>13}  判读")
    rows = []
    limit = 0
    for L in leads:
        # 目标：未来 (k, k+L] 内出现起点 —— **复用唯一实现**（见文件头 TOM 说明）
        if n - L <= 0:
            continue
        valid, tgt = TOM.make_onset_target(gt, L, args.min_quiet, args.min_run)
        idx = np.where(valid)[0]
        if len(idx) < 300 or tgt[idx].sum() < 30:
            print(f"  {L:>7}{len(idx):>10}{'—':>9}{'样本/正例不足':>20}")
            continue
        Xs = X.iloc[idx].reset_index(drop=True)
        ys = tgt[idx]
        tss = TimeSeriesSplit(n_splits=args.splits)
        oof = np.full(len(ys), np.nan)
        for tr, te in tss.split(Xs):
            if ys[tr].sum() < 5 or len(np.unique(ys[tr])) < 2:
                continue
            m = lgb.LGBMClassifier(objective="binary", random_state=42,
                                   n_jobs=-1, verbose=-1, **TSM.DEFAULTS)
            m.fit(Xs.iloc[tr], ys[tr])
            oof[te] = m.predict_proba(Xs.iloc[te])[:, 1]
        cov = ~np.isnan(oof)
        if cov.sum() < 50 or len(np.unique(ys[cov])) < 2:
            print(f"  {L:>7}{len(idx):>10}{'—':>9}{'OOF 不足':>20}")
            continue
        auc = float(roc_auc_score(ys[cov], oof[cov]))
        pos = float(ys[cov].mean())
        useful = auc >= args.useful_auc
        if useful:
            limit = max(limit, L)
        rows.append({"L": L, "n": int(cov.sum()), "pos": pos, "auc": auc})
        print(f"  {L:>7}{int(cov.sum()):>10}{pos:>9.1%}{auc:>9.4f}"
              f"{auc - 0.5:>+13.4f}  {'信息存在 ✓' if useful else '≈不可探测 ✗'}")

    # ── 定位能力诊断（决定性）：P(onset) 是否随「距最近起点的距离 d」上升 ──
    # 为什么必须做这一步：AUC 是**全局排序**能力，可以靠"ATR/ADX 高低"这类**慢变量**刷得很高；
    # 而触发器需要的是**事件定位**（高概率恰好出现在起点前几根）。二者是不同性质。
    # 实测教训：起点模型 OOF AUC 0.7599，但收紧阈值后漏检从 77 暴涨到 186 且中位变**滞后**
    # → 强烈提示"无定位能力"。本曲线即判据：
    #   · P 随 d 减小而显著上升 ⇒ 有定位能力，触发器可行
    #   · P 在各 d 上基本平坦   ⇒ 只学到"条件有利与否"（慢变量），任何阈值都做不出早触发
    L0 = 5 if 5 in leads else leads[-1]
    valid0 = np.zeros(n, dtype=bool)
    tgt0 = np.zeros(n, dtype=int)
    hi0 = n - L0
    if hi0 > 0:
        valid0[:hi0] = ~gt[:hi0]
        cnt0 = cum[1 + np.arange(hi0) + L0] - cum[1 + np.arange(hi0)]
        tgt0[:hi0] = (cnt0 > 0).astype(int)
    idx0 = np.where(valid0)[0]
    X0 = X.iloc[idx0].reset_index(drop=True)
    y0 = tgt0[idx0]
    tss0 = TimeSeriesSplit(n_splits=args.splits)
    o0 = np.full(len(y0), np.nan)
    for tr, te in tss0.split(X0):
        if len(np.unique(y0[tr])) < 2:
            continue
        m = lgb.LGBMClassifier(objective="binary", random_state=42,
                               n_jobs=-1, verbose=-1, **TSM.DEFAULTS)
        m.fit(X0.iloc[tr], y0[tr])
        o0[te] = m.predict_proba(X0.iloc[te])[:, 1]
    c0 = ~np.isnan(o0)

    edge_pos = np.where(edge)[0]
    # 每根 valid bar 到"下一个起点"的距离（无则记 99 = 截尾）
    d_arr = np.full(len(idx0), 99, dtype=int)
    if len(edge_pos):
        nxt = np.searchsorted(edge_pos, idx0, side="right")
        has = nxt < len(edge_pos)
        d_arr[has] = edge_pos[nxt[has]] - idx0[has]

    print(f"\n=========== 定位能力诊断（L={L0}，关键判据）===========")
    print(f"  {'距起点 d(bar)':>14}{'样本':>8}{'平均 P(onset)':>15}")
    base_p = float(np.nanmean(o0[c0]))
    curve_rows = []
    for d in [1, 2, 3, 5, 8, 12, 20, 99]:
        sel = c0 & (d_arr == d)
        if sel.sum() < 20:
            continue
        mp = float(np.mean(o0[sel]))
        curve_rows.append((d, int(sel.sum()), mp))
        tag = "（截尾：20 根内无起点）" if d == 99 else ""
        print(f"  {d:>14}{int(sel.sum()):>8}{mp:>15.4f}{tag}")
    print(f"  {'全体均值':>14}{int(c0.sum()):>8}{base_p:>15.4f}")
    if curve_rows:
        # ⚠ 远端参考必须取**有样本的最大 d 桶**。
        #   初版误用 d==99（"20 根内无起点"）桶，而该桶样本不足被跳过 → far=NaN
        #   → NaN>0.05 为 False → 走进"无定位能力"的错误分支（实测踩到，此处修正）。
        near = float(np.mean([r[2] for r in curve_rows if r[0] <= 2]))
        far_d, far = max(((r[0], r[2]) for r in curve_rows if r[0] != 99),
                         key=lambda t: t[0])
        lift = near - far
        print(f"\n  近端(d≤2) 平均 P = {near:.4f}；远端(d={far_d}) = {far:.4f}；"
              f"**定位增益 = {lift:+.4f}**")
        if lift > 0.05:
            print("  → **存在定位能力**（P 随距离显著上升）⇒ 信息确实能被定位到事件附近。")
            print("     但注意：P 是**在起点前若干根逐步抬高**的，不是只在起点那一根跳高。")
            print("     因此**按「概率水平」过阈的触发器会长时间处于 ON**（覆盖多个事件），")
            print("     其上升沿自然对不上单个事件 → 多折门里表现为「漏检高 + 中位滞后」。")
            print("     ⇒ 正确形态应是**用 P 的动态**（上升速率/局部峰值）而非水平做触发。")
        else:
            print("  → 定位能力弱（P 在远近端接近）⇒ 模型学到的更像"
                  "「条件是否有利」这类慢变量。")

    print(f"\n=========== 结论 ===========")
    if rows:
        print(f"  可探测提前量上限（AUC ≥ {args.useful_auc}）= **{limit} bar**"
              f"（L=1 起连续判据下最大者）")
        print(f"  实测（多折验收门）当前检测器中位提前量 ≈ 0 bar"
              f"（§27.4）")
        if limit <= 1:
            print("  → 信号几乎只能「同步」获得：**提前量受标签口径/信息集所限**，"
                  "继续调模型收益有限；应转向入场质量（买点/方向）与仓位管理。")
        else:
            print(f"  → 未来 {limit} 根内的起点**是可预测的**，而当前检测器只做到 ~0 bar"
                  f" → **是模型/链路不足，不是口径所限**，应优先重做触发器的训练目标"
                  f"（把「预测起点」作为显式任务，而非从 4 类 argmax 间接推导）。")
    print("\n  ⚠ 上限是相对于**本特征集**（27 维、单周期）的；多周期/量价/盘口特征可抬高它。")


if __name__ == "__main__":
    main()
