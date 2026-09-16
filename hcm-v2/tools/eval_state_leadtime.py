"""eval_state_leadtime.py — **验收门**：状态模型到底有没有解决"指标滞后"。

用户目标（2026-09-15 原话）：*"必须敏锐捕捉行情变化"*、*"解决指标滞后问题"*。
故唯一有意义的验收判据**不是** macro F1，而是：
    **同一个"行情阶段切换"事件上，检测器比滞后指标早几根 bar 改判？**

【2026-09-15 改为走前式多折（walk-forward）】
    此前只用"最后 20%"一段评估，实测该段 regime 与训练段严重不同
    （训练段 quiet 55% / 样本外段 advancing 60%，见方案 §26.1）→ **单段结论会被 regime 左右**。
    现改为：把标注数据切成 N 折，逐折"用该折之前的全部数据训练、在该折上评估"，
    再跨折汇总。每折同时输出 **regime 构成**，使结论可判读其稳定性。

判据（每个检测器）：
  · **提前量 offset**：负 = 比真值早，正 = 滞后（单位：标注子序列 bar 步）
  · **漏检**：真值上升沿 ±window 内无检测器上升沿
  · **误报**：真值为"震荡"而检测器为 ON 的比例

⚠ 口径声明：
  · 标签 CSV 只含**已标注** bar（剔除约 41% 模糊样本）→ 在不规则子序列上评估，时间差以子序列步计。
  · `adx_rule` 基线**存在循环引用**（ADX 是标签构造阈值之一）→ 仅参考，不作判据。
  · 防抖近似 FSM 的 k 根连续语义，**非逐行等价**。

用法：
    python eval_state_leadtime.py --symbol XAUUSD --tf M5 --labels _scratch/state_M5_v2.csv
    python eval_state_leadtime.py ... --shape-labels _scratch/shapes_M5.csv --shape-col cluster_name
    python eval_state_leadtime.py ... --folds 5 --min-train-frac 0.3
    python eval_state_leadtime.py ... --donchian-scan
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
_SIG = os.path.join(_ROOT, "hcm-signal-tower", "signal_tower")

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


SF = _load("state_features", os.path.join(_SIG, "state_features.py"))
TD = _load("trend_direction", os.path.join(_SIG, "trend_direction.py"))
BSL = _load("build_state_labels", os.path.join(_TOOLS, "build_state_labels.py"))
TSM = _load("train_state_model", os.path.join(_TOOLS, "train_state_model.py"))
# 起点预测的目标构造与阈值标定**只从训练器导入**，避免两处实现漂移（单一真值）
TOM = _load("train_onset_model", os.path.join(_TOOLS, "train_onset_model.py"))
from sklearn.model_selection import TimeSeriesSplit  # noqa: E402


# ────────────────────────── 基础工具 ──────────────────────────

# 时间→秒的换算**统一委托 build_state_labels.epoch_s**（单一真值，含单位陷阱说明）
epoch_s = BSL.epoch_s


def _rising_edges(flag: np.ndarray) -> np.ndarray:
    prev = np.concatenate(([False], flag[:-1]))
    return np.where(flag & ~prev)[0]


def _edges_with_gap(flag: np.ndarray, max_gap: int) -> np.ndarray:
    """把"真值有洞"（未标注 bar）容忍掉：只要求前 max_gap 根内出现过 False。"""
    n = len(flag)
    out = []
    for i in range(n):
        if not flag[i]:
            continue
        lo = max(0, i - max_gap)
        if not flag[lo:i].any():
            out.append(i)
    return np.array(out, dtype=int)


def apply_debounce(classes: np.ndarray, decided: np.ndarray, k: int,
                   init: str = "oscillation") -> np.ndarray:
    """近似 FSM 防抖（**hold 语义**）：低置信不参与（保持前值）；改判需连续 k 根同值。

    ⚠ 本函数只实现 FSM 的 **hold** 分支（`state.fsm.low_conf_policy="hold"`）。
    **decay** 分支不在本函数内另写一份：调用方把低置信 bar 的类别先替换为
    `"oscillation"` 并以 `decided=全 True` 调用本函数 —— 与 `state_machine.decide()`
    第 4c 步"复用既有迁移与防抖逻辑"的做法逐条对应（单一语义，防双实现漂移）。
    """
    cur = init
    streak = 0
    out = []
    for c, d in zip(classes, decided):
        if not d:
            out.append(cur)
            continue
        if c == cur:
            streak = 0
            out.append(cur)
            continue
        streak += 1
        if streak >= max(1, k):
            cur = c
            streak = 0
        out.append(cur)
    return np.array(out, dtype=object)


def lead_offsets(gt_edges: np.ndarray, det: np.ndarray,
                 window: int) -> tuple[np.ndarray, int]:
    det_edges = _rising_edges(det)
    offs, miss = [], 0
    for e in gt_edges:
        if len(det_edges) == 0:
            miss += 1
            continue
        d = det_edges - e
        near = d[np.abs(d) <= window]
        if len(near) == 0:
            miss += 1
            continue
        offs.append(int(near[np.argmin(np.abs(near))]))
    return np.array(offs, dtype=int), miss


def stat_of(det: np.ndarray, gt: np.ndarray, gt_edges: np.ndarray,
            window: int) -> dict:
    offs, miss = lead_offsets(gt_edges, det, window)
    return {
        "n_match": int(len(offs)), "n_miss": int(miss), "episodes": int(len(gt_edges)),
        "mean_offset": float(offs.mean()) if len(offs) else float("nan"),
        "median_offset": float(np.median(offs)) if len(offs) else float("nan"),
        "early_rate": float((offs < 0).mean()) if len(offs) else float("nan"),
        "false_alarm": float((det & ~gt).sum() / max(1, (~gt).sum())),
    }


def fmt_stat(name: str, r: dict) -> str:
    return (f"  {name:<13} 匹配 {r['n_match']:>3}/{r['episodes']:<3} 漏检 {r['n_miss']:>3} | "
            f"提前量 均值={r['mean_offset']:+.2f} 中位={r['median_offset']:+.1f} | "
            f"提前占比={r['early_rate']:.1%} | 误报={r['false_alarm']:.1%}")


# ────────────────────────── 主流程 ──────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5", choices=["M5", "M15", "H1"])
    ap.add_argument("--labels", default="_scratch/state_M5_v2.csv")
    ap.add_argument("--gt-col", default="label_id",
                    help="真值列：label_id（默认，4 类形态）或 vol_expansion（路线 B 波动扩张）。"
                         "判真值取 >0；非 label_id 时只构造不依赖 label_id 的检测器（见 build_dets）")
    ap.add_argument("--feature-set", default="base", choices=["base", "l1"],
                    help="特征集（同 train_state_model）：base=27；l1=base+量价/点差 6 列。"
                         "默认 base（与既有模型/推理契约一致）")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--folds", type=int, default=5,
                    help="走前式折数（1 = 退回单段 --test-ratio 模式）")
    ap.add_argument("--min-train-frac", type=float, default=0.3,
                    help="首折最小训练比例（训练不足的折跳过）")
    ap.add_argument("--test-ratio", type=float, default=0.2, help="folds=1 时的测试比例")
    ap.add_argument("--window", type=int, default=40, help="匹配上升沿的搜索窗（bar）")
    ap.add_argument("--k", type=int, default=3, help="防抖根数（近似 FSM）")
    ap.add_argument("--min-conf", type=float, default=0.45)
    ap.add_argument("--adx-thr", type=float, default=22.0,
                    help="循环引用基线 ADX 阈值（仅参考）")
    ap.add_argument("--slope-thr", type=float, default=1.0)
    ap.add_argument("--donchian-w", type=int, default=20)
    ap.add_argument("--gap", type=int, default=3, help="真值连续性容忍")
    ap.add_argument("--shape-labels", default=None)
    ap.add_argument("--shape-col", default="cluster_name")
    ap.add_argument("--shape-quiet", default="quiet")
    ap.add_argument("--shape-balanced", action="store_true")
    ap.add_argument("--onset-lead", type=int, default=None,
                    help="启用显式起点预测模型检测器（未来 L 根内出现起点）")
    ap.add_argument("--onset-oof-splits", type=int, default=3,
                    help="折内阈目标定用的时序 OOF 折数")
    ap.add_argument("--onset-onrate", type=float, default=0.10,
                    help="起点触发器的**目标触发率**（按训练段 P 分位标定，与 base rate 解耦）"
                         "；实测「最大 F1」规则会随 regime 漂到 87%% 触发率从而失效")
    ap.add_argument("--onset-rise-m", type=int, default=3,
                    help="动态触发器：上升速率跨度的 bar 数 m（ΔP = P[t] − P[t−m]）")
    ap.add_argument("--onset-peak-w", type=int, default=3,
                    help="动态触发器：局部峰值的半窗宽 w（P[t] 为 ±w 邻域最大）")
    ap.add_argument("--donchian-scan", action="store_true")
    # 【L3 同步 · 2026-09-16】必须与 FSM 的 `state.fsm.low_conf_policy` **同义**。
    # 此前本门恒用 hold（低置信保持前值）⇒ 一旦生产切到 decay，本门评的就是"另一个系统"，
    # A/B 结论无法迁移到生产。默认 hold = 既有行为（零变化）。
    ap.add_argument("--low-conf", default="hold", choices=["hold", "decay"],
                    help="低置信语义，须与 state.fsm.low_conf_policy 一致："
                         "hold=保持前值（既有）| decay=视为震荡并参与防抖")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    import lightgbm as lgb

    conn = psycopg2.connect(args.db_url)
    try:
        kl = BSL.load_klines(conn, args.symbol, args.tf)
    finally:
        conn.close()
    if kl.empty or len(kl) < 500:
        raise SystemExit(f"[fatal] K 线不足：{args.symbol} {args.tf} rows={len(kl)}")

    df = pd.read_csv(args.labels)
    cols = list(SF.STATE_FEATURE_COLS_L1 if args.feature_set == "l1"
                else SF.STATE_FEATURE_COLS)
    missc = [c for c in cols if c not in df.columns]
    if missc:
        raise SystemExit(f"[fatal] 标签 CSV 缺特征列：{missc}")
    df = df[df[args.gt_col].notna()].copy()
    if args.gt_col == "label_id":
        df["label_id"] = df["label_id"].astype(int)
    df = df.sort_values("open_time").reset_index(drop=True)
    y = (df["label_id"].to_numpy() if "label_id" in df.columns
         else np.zeros(len(df), dtype=int))
    # 真值：4 类模式 = `label_id != 0`（0 = oscillation 契约）；波动模式 = `vol_expansion > 0`
    gt_all = df[args.gt_col].to_numpy(dtype=float) > 0

    # ── 每行对齐到 K 线（指标/突破算一次，全折复用）──
    ep = epoch_s(df["open_time"])
    k_ep = epoch_s(kl["open_time"])
    pos = np.searchsorted(k_ep, ep)
    np.clip(pos, 0, len(k_ep) - 1, out=pos)
    hit = k_ep[pos] == ep
    if not hit.all():
        print(f"[warn] {int((~hit).sum())} 个标注 bar 未匹配到 K 线 → 指标类检测器在这些 bar 上置 False")

    high = kl["high"].to_numpy(float)
    low = kl["low"].to_numpy(float)
    close = kl["close"].to_numpy(float)
    params = dict(SF.DEFAULT_PARAMS)
    ind = SF.compute_indicators(high, low, close, params)
    adx_row = np.where(hit, np.asarray(ind["adx"], dtype=float)[pos], np.nan)
    dirs = TD.compute_direction_series(
        high, low, close, ind=ind, params=params,
        cfg={"state.dir.slope_thr_atr": args.slope_thr,
             "state.dir.debounce_bars": args.k})
    slope_row = np.where(hit, np.abs(dirs["slope_atr"][pos]), np.nan)

    def _donchian(w: int) -> np.ndarray:
        hp = pd.Series(high).rolling(w).max().shift(1).to_numpy()
        lp = pd.Series(low).rolling(w).min().shift(1).to_numpy()
        ok = (hit & (close[pos] > hp[pos])) | (hit & (close[pos] < lp[pos]))
        return ok

    # 形状标签查找表（epoch → 形状名）
    shape_lut = None
    if args.shape_labels:
        sh = pd.read_csv(args.shape_labels)
        if args.shape_col not in sh.columns:
            raise SystemExit(f"[fatal] 形状 CSV 缺列 {args.shape_col}")
        s_ep = epoch_s(sh["open_time"])
        shape_lut = dict(zip(s_ep.tolist(), sh[args.shape_col].astype(str).tolist()))
    shape_names = np.array([shape_lut.get(int(e), "") if shape_lut else ""
                            for e in ep], dtype=object)

    print(f"[data] {args.symbol} {args.tf} K线={len(kl)} 标注={len(df)} "
          f"未匹配={int((~hit).sum())}")
    print(f"[cfg] folds={args.folds} min_train_frac={args.min_train_frac} k={args.k} "
          f"window={args.window} donchian_w={args.donchian_w}")

    def build_dets(i_tr: np.ndarray, i_te: np.ndarray) -> dict:
        """在给定训练/测试下标上训练模型并构造全部检测器。"""
        dets: dict = {}
        X = df[cols].astype(float)
        # ── 【路线 B · 2026-09-16】gt 非 4 类标签时，只构造**不依赖 label_id** 的检测器 ──
        # 为什么必须早退：波动目标（`vol_expansion`）对**每根 bar** 都有定义、且**不经 4 类
        #   置信过滤** ⇒ `label_id` 在这些行上可能为空。把它喂给多分类检测器会产生
        #   NaN 训练集（崩溃）或伪造标签（静默失真）—— 两者都不可接受，故直接返回。
        # 检测器：
        #   vol_model —— 折内训练的二分类（特征同契约，目标 = `--gt-col` 列）
        #   atr_rule* —— **平凡基线**：只用"当期波动分位"（`atr_pct`）在训练折的
        #                (1−onset_onrate) 分位作阈值。它回答关键问题：
        #                **模型是否超越了"当前波动高就报警"这条规则？**
        if args.gt_col != "label_id":
            yv = np.nan_to_num(df[args.gt_col].to_numpy(dtype=float)[i_tr]).astype(int)
            if len(np.unique(yv)) > 1:
                vm = lgb.LGBMClassifier(objective="binary", random_state=42,
                                        n_jobs=-1, verbose=-1, **TSM.DEFAULTS)
                vm.fit(X.iloc[i_tr], yv)
                dets["vol_model"] = vm.predict_proba(X.iloc[i_te])[:, 1] >= 0.5
            ap_all = df["atr_pct"].to_numpy(dtype=float)
            ap_tr = ap_all[i_tr]
            ap_tr = ap_tr[np.isfinite(ap_tr)]
            if ap_tr.size > 50:
                qv = float(np.quantile(ap_tr, 1.0 - args.onset_onrate))
                ap_te = ap_all[i_te]
                dets["atr_rule*"] = np.where(np.isfinite(ap_te), ap_te >= qv, False)
            return dets
        # 现有 4 类模型
        m4 = lgb.LGBMClassifier(objective="multiclass", num_class=len(TSM.STATE_NAMES),
                                random_state=42, n_jobs=-1, verbose=-1, **TSM.DEFAULTS)
        m4.fit(X.iloc[i_tr], y[i_tr])
        pr = m4.predict_proba(X.iloc[i_te])
        pred = np.array([TSM.STATE_NAMES[i] for i in pr.argmax(axis=1)], dtype=object)
        decided = pr.max(axis=1) >= args.min_conf
        # ── 【L3 同步 · 2026-09-16】`model` 检测器按 `--low-conf` 反映**生产语义** ──
        # 两条路径与 FSM `state_machine.decide()` 第 4c 步**逐条对应**（单一语义，见该处注释）：
        #   hold  → 低置信不参与防抖（`apply_debounce` 原生语义，低置信即 return _keep）
        #   decay → 低置信**退化为震荡**后照常参与防抖
        #           （FSM 里同样复用既有迁移与防抖逻辑，**不另写一份实现**）
        _pnh = np.where(decided, pred, "oscillation")
        if args.low_conf == "decay":
            dets["model"] = np.array([c != "oscillation" for c in
                                      apply_debounce(_pnh, np.ones(len(_pnh), bool), args.k)])
        else:
            dets["model"] = np.array([c != "oscillation" for c in
                                      apply_debounce(pred, decided, args.k)])
        # 另一策略恒以 `model_nohold` 并列输出（同一张表内对照；复用同一变换，不重复实现）
        dets["model_nohold"] = np.array(
            [c != "oscillation" for c in
             apply_debounce(_pnh, np.ones(len(_pnh), bool), args.k)])
        # 形状模型（数据驱动标签）
        if shape_lut is not None:
            s_tr, s_te = shape_names[i_tr], shape_names[i_te]
            j_tr = np.where(s_tr != "")[0]
            j_te = np.where(s_te != "")[0]
            if len(j_tr) >= 200 and len(j_te) > 0:
                classes = sorted({s_tr[j] for j in j_tr})
                c2i = {c: i for i, c in enumerate(classes)}
                sm = lgb.LGBMClassifier(
                    objective="multiclass", num_class=len(classes),
                    class_weight=("balanced" if args.shape_balanced else None),
                    random_state=42, n_jobs=-1, verbose=-1, **TSM.DEFAULTS)
                sm.fit(X.iloc[i_tr[j_tr]], [c2i[s_tr[j]] for j in j_tr])
                ps = sm.predict_proba(X.iloc[i_te[j_te]])
                prd = np.array([classes[i] for i in ps.argmax(axis=1)], dtype=object)
                full = np.array([args.shape_quiet] * len(i_te), dtype=object)
                full[j_te] = prd
                dets["shape_model"] = np.array(
                    [c != args.shape_quiet for c in
                     apply_debounce(full, np.ones(len(full), bool), args.k,
                                    init=args.shape_quiet)])
                dets["_shape_acc"] = float((prd == s_te[j_te]).mean())
                vc = pd.Series(s_te[j_te]).value_counts()
                dets["_shape_major"] = float(vc.iloc[0] / vc.sum())
                dets["_shape_dist"] = vc.to_dict()
        # 【候选链路·核心】显式起点预测模型：目标 = 未来 L 根内出现趋势起点。
        # 阈值在**折内**用时序 OOF 按最大 F1 标定（规则事先声明，不用测试段信息）。
        if args.onset_lead:
            v_tr, yt = TOM.make_onset_target(gt_all[tr_i], args.onset_lead)
            j_tr = np.where(v_tr)[0]
            if len(j_tr) >= 300 and yt[j_tr].sum() >= 30:
                Xs = X.iloc[tr_i[j_tr]].reset_index(drop=True)
                ysr = yt[j_tr]
                tss2 = TimeSeriesSplit(n_splits=args.onset_oof_splits)
                oof2 = np.full(len(ysr), np.nan)
                for tr2, te2 in tss2.split(Xs):
                    if len(np.unique(ysr[tr2])) < 2:
                        continue
                    m2 = lgb.LGBMClassifier(objective="binary", random_state=42,
                                            n_jobs=-1, verbose=-1, **TSM.DEFAULTS)
                    m2.fit(Xs.iloc[tr2], ysr[tr2])
                    oof2[te2] = m2.predict_proba(Xs.iloc[te2])[:, 1]
                c2 = ~np.isnan(oof2)
                # 【阈值规则·关键】用**目标触发率分位**，不用"最大 F1"。
                # 依据：实测「最大 F1」规则下，阈值随 base rate 漂移
                # （折1 0.9166 → 折3 0.6868），触发占比从 40.9% 飙到 87.0% →
                # 触发器"几乎一直报警"，上升沿与起点脱节，漏检 29/55。
                # 触发器需要的是**有界触发率**（与 regime 解耦），故按训练段 P 的 (1−onrate) 分位取阈。
                thr = 0.5
                if c2.sum() > 50:
                    thr = float(np.quantile(oof2[c2], 1.0 - args.onset_onrate))
                mf = lgb.LGBMClassifier(objective="binary", random_state=42,
                                        n_jobs=-1, verbose=-1, **TSM.DEFAULTS)
                mf.fit(Xs, ysr)
                p_te = mf.predict_proba(X.iloc[i_te])[:, 1]
                cls = np.where(p_te >= thr, "onset", "base")
                dets["onset_model"] = (
                    apply_debounce(cls, np.ones(len(cls), bool), args.k,
                                   init="base") == "onset")
                dets["_onset_thr"] = thr
                dets["_onset_on"] = float((p_te >= thr).mean())

                # ── 动态触发器（本轮的修正核心）──
                # 为什么不用水平：§29.4 实测 P(onset) 在起点前若干根**逐步抬高**，
                # 故"P ≥ 阈值"的 ON 段会横跨多个事件 → 上升沿对不上单个起点
                # （漏检 77~186、中位滞后）。改用 P 的**动态**，使 ON 段变短。
                # 两者都**不做防抖**（本身已很短，再防抖会人为推迟）。
                nz = np.isfinite(oof2)
                # 1) 上升速率：ΔP ≥ 训练段 OOF ΔP 的 (1−onrate) 分位
                mm = max(1, args.onset_rise_m)
                dtr = np.full(len(oof2), np.nan)
                dtr[mm:] = oof2[mm:] - oof2[:-mm]
                dv = dtr[np.isfinite(dtr)]
                thr_d = float(np.quantile(dv, 1.0 - args.onset_onrate)) if dv.size > 50 else 0.0
                dp_te = np.full(len(p_te), -1.0)
                dp_te[mm:] = p_te[mm:] - p_te[:-mm]
                dets["onset_rise"] = dp_te >= thr_d
                dets["_rise_thr"] = thr_d
                # 2) 局部峰值：P[t] 为 ±w 邻域最大，且 ≥ 训练段 P 的 (1−onrate) 分位
                w = max(1, args.onset_peak_w)
                pv = oof2[nz]
                pq = float(np.quantile(pv, 1.0 - args.onset_onrate)) if pv.size > 50 else 0.5
                pk = np.zeros(len(p_te), dtype=bool)
                for i in range(w, len(p_te) - w):
                    seg = p_te[i - w: i + w + 1]
                    if p_te[i] >= seg.max() and p_te[i] >= pq:
                        pk[i] = True
                dets["onset_peak"] = pk
                dets["_peak_q"] = pq
        dets["adx_rule*"] = np.where(hit[i_te], adx_row[i_te] >= args.adx_thr, False)
        dets["slope_rule"] = np.where(hit[i_te], slope_row[i_te] >= args.slope_thr, False)
        dets["donchian"] = _donchian(args.donchian_w)[i_te]
        # ── 组合触发器（OR）：三者取舍互补，取并集以同时压低漏检与误报 ──
        #   onset_rise ：低误报 + **稳定提前**（三折中位全负）
        #   model_nohold：漏检最少，但误报高
        #   donchian   ：误报最低，但提前量不稳
        if "onset_rise" in dets and "model_nohold" in dets:
            dets["rise|nohold"] = dets["onset_rise"] | dets["model_nohold"]
        if "onset_rise" in dets:
            dets["rise|donch"] = dets["onset_rise"] | dets["donchian"]
        return dets

    # ── 折划分 ──
    n = len(df)
    if args.folds <= 1:
        sp = int(n * (1.0 - args.test_ratio))
        folds = [(np.arange(0, sp), np.arange(sp, n))]
    else:
        bounds = np.linspace(0, n, args.folds + 1).astype(int)
        folds = []
        for f in range(args.folds):
            te = np.arange(bounds[f], bounds[f + 1])
            tr = np.arange(0, bounds[f])
            if len(tr) < int(n * args.min_train_frac) or len(te) < 50:
                continue
            folds.append((tr, te))
    if not folds:
        raise SystemExit("[fatal] 无有效折（调小 --min-train-frac 或增大数据）")

    # ── Donchian 窗口标定 ──
    if args.donchian_scan:
        print(f"\n=========== Donchian 窗口标定（{len(folds)} 折汇总）===========")
        agg: dict = {}
        for w in (5, 10, 15, 20, 30, 40, 60):
            rows = []
            for tr_i, te_i in folds:
                g = gt_all[te_i]
                ge = _edges_with_gap(g, args.gap)
                rows.append(stat_of(_donchian(w)[te_i], g, ge, args.window))
            miss = sum(r["n_miss"] for r in rows)
            eps = sum(r["episodes"] for r in rows)
            fa = float(np.mean([r["false_alarm"] for r in rows]))
            med = float(np.median([r["median_offset"] for r in rows]))
            agg[w] = (miss, eps, fa, med, rows)
            print(f"  w={w:<4} 漏检 {miss:>3}/{eps:<4} | 误报(各折均值)={fa:.1%} | "
                  f"中位(各折中位)= {med:+.1f} bar")
        best = sorted(agg.items(), key=lambda kv: (kv[1][0] != 0, kv[1][2], kv[1][3]))
        print(f"\n  → 标定结果（规则：漏检0优先 → 误报低 → 中位早）：w={best[0][0]}")
        return

    # ── 逐折评估 ──
    if args.gt_col != "label_id":
        # 波动扩张模式：只列不依赖 label_id 的检测器（理由见 build_dets 的早退说明）
        det_names = ["vol_model", "atr_rule*"]
    else:
        det_names = ["rise|nohold", "rise|donch", "onset_rise", "onset_peak",
                     "onset_model", "shape_model", "model", "model_nohold",
                     "adx_rule*", "slope_rule", "donchian"]
    per_fold: list[dict] = []
    for fi, (tr_i, te_i) in enumerate(folds, 1):
        g = gt_all[te_i]
        ge = _edges_with_gap(g, args.gap)
        dets = build_dets(tr_i, te_i)
        avail = [nm for nm in det_names if nm in dets]
        trend_share = float(g.mean())
        print(f"\n---- 折 {fi}/{len(folds)}  训练 {len(tr_i)} / 测试 {len(te_i)} | "
              f"gt趋势占比={trend_share:.1%} | 真值切换 {len(ge)} 次 ----")
        if "_shape_acc" in dets:
            print(f"  [形状] 准确率={dets['_shape_acc']:.3f} "
                  f"(恒多数类={dets['_shape_major']:.3f}) 分布={dets['_shape_dist']}")
        if "_onset_thr" in dets:
            print(f"  [起点·水平] L={args.onset_lead} 阈值={dets['_onset_thr']:.4f}"
                  f"  触发占比={dets['_onset_on']:.1%}"
                  f" | [动态] ΔP阈值={dets['_rise_thr']:+.4f}"
                  f" 峰值分位={dets['_peak_q']:.4f}")
        res = {}
        for nm in avail:
            r = stat_of(dets[nm], g, ge, args.window)
            res[nm] = r
            print(fmt_stat(nm, r))
        per_fold.append({"trend_share": trend_share, "eps": int(len(ge)), "res": res})

    # ── 跨折汇总 ──
    print("\n=========== 跨折汇总 ===========")
    print(f"  {'检测器':<14}{'零漏检折':>10}{'漏检合计':>10}{'误报(均)':>10}"
          f"{'中位提前量':>22}{'早于基线折数':>14}")
    base_med = None
    if per_fold and "donchian" in per_fold[0]["res"]:
        base_med = float(np.median([f["res"]["donchian"]["median_offset"]
                                    for f in per_fold]))
    summary = {}
    for nm in det_names:
        rows = [f["res"][nm] for f in per_fold if nm in f["res"]]
        if not rows:
            continue
        zero_miss = sum(1 for r in rows if r["n_miss"] == 0)
        miss = sum(r["n_miss"] for r in rows)
        fa = float(np.mean([r["false_alarm"] for r in rows]))
        meds = [r["median_offset"] for r in rows]
        med = float(np.median(meds))
        meds_s = "/".join(f"{m:+.0f}" for m in meds)
        beat = (sum(1 for m in meds if base_med is not None and m < base_med)
                if base_med is not None else 0)
        summary[nm] = {"zero_miss": zero_miss, "folds": len(rows), "miss": miss,
                       "fa": fa, "med": med, "meds": meds}
        print(f"  {nm:<14}{zero_miss:>7}/{len(rows):<3}{miss:>10}{fa:>10.1%}"
              f"{med:>13.1f}  [{meds_s}]{beat:>10}/{len(rows)}")

    print(f"\n  基线(donchian) 各折中位 = {base_med:+.1f} bar" if base_med is not None else "")
    print("\n  ⚠ 口径：标签 CSV 仅含已标注 bar，时间差以子序列 bar 步计；防抖近似 FSM；"
          "adx_rule 存在循环引用（ADX 是标签阈值之一）仅作参考。")
    print("  折间差异即为 regime 敏感度 —— 任何「单段结论」都应以此表复核。")


if __name__ == "__main__":
    main()
