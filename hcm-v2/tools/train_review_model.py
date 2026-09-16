#!/usr/bin/env python3
"""train_review_model.py — 【P1】信号级评审模型训练 + OOF 校准 + 验收闸门。

依据：docs/设计方案_LightGBM解耦信号级评审与校准治理_20260911.md §5.1 / §6.2

三头（与输入契约 REVIEW_FEATURE_COLS 同源）：
  quality   : label        （真实成交胜负，二分类）
  entry     : entry_label  （条件于 signal_dir 的 1R 先触，二分类）
  direction : dir_label    （未来 ±0.8·ATR 方向，3 分类）

纪律：
  · 时序切分（前段训练/后段测试），禁随机 shuffle（金融时序泄漏）；
  · 校准走 **OOF 跨折汇总** 拟合 isotonic（单测试切片拟合会退化成粗阶梯）；
  · 产物落 **staging 目录、不占正式版本号**（方案 §6.1「拒收不占号」）；
  · 验收闸门全硬性，不达标即 acceptance=FAIL，绝不自动 promote。

用法：
  python train_review_model.py --data _artifacts/review_dataset.csv \
      --outdir models/review_staging
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lightgbm as lgb  # noqa: E402
from sklearn.isotonic import IsotonicRegression  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402

from _review_feature_cols import REVIEW_FEATURE_COLS  # noqa: E402
from calib_np import NumpyCalibrator, PlattCalibrator  # noqa: E402

# ── 验收闸门（方案 §6.2，全部硬性；可由环境变量覆盖便于灰度试验）──
GATE_AUC = float(os.environ.get("REVIEW_GATE_AUC", "0.55"))
GATE_DIR_HIT = float(os.environ.get("REVIEW_GATE_DIR_HIT", "0.55"))
GATE_ECE = float(os.environ.get("REVIEW_GATE_ECE", "0.05"))
GATE_MIN_LEVELS = int(os.environ.get("REVIEW_GATE_MIN_LEVELS", "8"))
GATE_MIN_TRAIN = int(os.environ.get("REVIEW_GATE_MIN_TRAIN", "2000"))
GATE_MIN_POS = int(os.environ.get("REVIEW_GATE_MIN_POS", "200"))
TEST_RATIO = 0.2
# 【2026-09-11 校准协议修正】留出校准集比例（占总量）。模型只用 (1-TEST-CALIB) 拟合，
# 校准器用**同一模型**在该段上的预测拟合 → 校准器与模型同源、可迁移。
# 设 0 即回退旧协议（TimeSeriesSplit OOF 汇总；实测不可迁移，不推荐）。
CALIB_RATIO = float(os.environ.get("REVIEW_CALIB_RATIO", "0.1"))
# 【2026-09-11 B 方案】校准器类型：
#   platt（默认）·参数化 sigmoid，2 参数，小样本稳、输出连续（无档位限制）
#   isotonic    ·阶梯函数，分辨率受校准样本量硬约束（292 样本实测仅 5~9 档）
CALIB_METHOD = str(os.environ.get("REVIEW_CALIB_METHOD", "platt")).strip().lower()

# 【§6.1 ACCEPT 闸门接线 2026-09-11】候选六件套（staging 产物）
ARTIFACTS = ("lgbm_review_quality.txt", "lgbm_review_entry.txt", "lgbm_review_direction.txt",
             "calib_review_quality.pkl", "calib_review_entry.pkl", "calib_review_direction.pkl")


def _promote(outdir: str, promote_dir: str) -> bool:
    """【§6.1 2026-09-11】验收通过才把六件套 staging → 正式目录。

    此前缺口：`ai.review.model_dir` 直指 `review_staging`，等于**未验收的模型被线上
    直接读取**（P1 验收 FAIL 仍在跑），治理闸门形同虚设。现改为：训练只写 staging，
    仅本函数（且调用方已确认 accepted=True）才落到正式目录。
    仅复制、保留 staging 供审计。
    """
    import shutil
    os.makedirs(promote_dir, exist_ok=True)
    ok = True
    for n in ARTIFACTS:
        src = os.path.join(outdir, n)
        if not os.path.exists(src):
            print(f"[promote] missing artifact: {n}", file=sys.stderr)
            ok = False
            continue
        shutil.copy2(src, os.path.join(promote_dir, n))
    print(f"[promote] staging → {promote_dir}: {'OK' if ok else 'INCOMPLETE'}")
    return ok


def _set_model_dir(container_path: str) -> None:
    """双写 `ai.review.model_dir`（PG 真源 + Redis 缓存 + PUB），铁律 5.2。"""
    db = os.environ.get("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
    rurl = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        import psycopg2
        with psycopg2.connect(db, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE hcm_config.metadata SET current_value=%s, updated_at=now() "
                    "WHERE config_key='ai.review.model_dir'", (container_path,))
                if cur.rowcount == 0:
                    cur.execute(
                        "INSERT INTO hcm_config.metadata "
                        "(config_key,current_value,default_value,value_type,category) "
                        "VALUES ('ai.review.model_dir',%s,%s,'string','ai')",
                        (container_path, container_path))
        print(f"[promote] PG ai.review.model_dir = {container_path}")
    except Exception as e:  # noqa: BLE001
        print(f"[promote] PG write failed: {e}", file=sys.stderr)
    try:
        import redis as _r
        _rc = _r.Redis.from_url(rurl, socket_timeout=3)
        _rc.hset("hcm:config:v2", "ai.review.model_dir", container_path)
        _rc.publish("hcm:config:invalidate", "ai.review.model_dir")
        print(f"[promote] Redis ai.review.model_dir = {container_path} (+PUB)")
    except Exception as e:  # noqa: BLE001
        print(f"[promote] Redis write failed: {e}", file=sys.stderr)


def _ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """期望校准误差：Σ (n_b/N)·|mean(p_b) − mean(y_b)|。"""
    edges = np.linspace(0.0, 1.0, bins + 1)
    tot = len(y)
    e = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1.0 else p <= hi)
        if m.sum() == 0:
            continue
        e += (m.sum() / tot) * abs(float(p[m].mean()) - float(y[m].mean()))
    return float(e)


def _monotonicity(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """10 分桶「桶内预测均值 vs 桶内实测胜率」的 Pearson 相关（方案 §6.2，须 >0）。"""
    q = pd.qcut(pd.Series(p), bins, labels=False, duplicates="drop")
    xs, ys = [], []
    for b in sorted(pd.unique(q.dropna())):
        m = (q == b).values
        if m.sum() < 3:
            continue
        xs.append(float(p[m].mean()))
        ys.append(float(y[m].mean()))
    if len(xs) < 3:
        return 0.0
    return float(np.corrcoef(xs, ys)[0, 1])


def _fit_platt(p: np.ndarray, y: np.ndarray) -> PlattCalibrator:
    """【2026-09-11 B 方案】拟合 Platt scaling（参数化 sigmoid，2 参数）。

    在 `logit(p_raw)` 上做（近）无正则 logistic 回归（MLE）得到 (a, b)：
        p_cal = sigmoid(a · logit(p_raw) + b)
    相比 isotonic：参数少、小样本不退化、输出连续（不受「档位数」门槛约束）。
    返回的 `PlattCalibrator` 仅依赖 numpy，生产容器可直接反序列化（同 calib_np 约定）。
    """
    from sklearn.linear_model import LogisticRegression
    _eps = 1e-6
    _p = np.clip(np.asarray(p, float), _eps, 1.0 - _eps)
    z = np.log(_p / (1.0 - _p)).reshape(-1, 1)
    lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=2000)
    lr.fit(z, np.asarray(y).astype(int))
    return PlattCalibrator(float(lr.coef_[0][0]), float(lr.intercept_[0]))


def _fit_calibrator(mdl, X_calib: pd.DataFrame, y_calib: np.ndarray,
                    binary: bool) -> tuple:
    """【2026-09-11 校准协议修正】用**同一个已训练模型**在留出校准集上的预测拟合 isotonic。

    为什么必须改（实测根因，非推测）：
      旧协议用 TimeSeriesSplit **折模型**的 OOF 预测拟合校准器，却把它作用于
      **最终模型**的预测。两者分布不同 —— 折模型预测集中在 ≈0（`X_thresholds_`
      前 12 档全在 1e-4 量级），而最终模型测试预测铺满 [0.0138, 0.9624] → isotonic
      的断点全部落在低分密集区、其余区间饱和 → 校准后预测近乎常数（实测 `pc` 仅
      **2 个取值**）→ `monotonicity` 恒 0、`ece ≈ |常数 − base_rate|`。
      本函数保证**校准器与模型同源**（同一 `mdl` 产生预测），分布一致、可迁移。

    纪律：`X_calib` 必须**未参与** `mdl.fit`（否则校准器过拟合训练段 → 指标虚高）。

    返回 (NumpyCalibrator, 档位数, 校准集预测)。
    """
    y_calib = np.asarray(y_calib)
    if binary:
        fit_p = mdl.predict_proba(X_calib)[:, 1]
        fit_y = y_calib
    else:
        _p3 = mdl.predict_proba(X_calib)
        _idx = np.argmax(_p3, axis=1)
        # 【BUG 防护】`argmax` 返回**列序号** {0,1,2}，而 `y` 是原标签空间 {-1,0,1}
        # （dir_label）。必须按 classes_ 映射回原标签再比较，否则 fit_y 恒 0
        # → 校准器退化为常数（实测曾 AUC(fp,fit_y)=0.3197、fit_y.mean()=0.083）。
        _classes = np.unique(y_calib)
        fit_p = _p3[np.arange(len(_p3)), _idx]                       # 预测类置信
        fit_y = (_classes[_idx] == y_calib).astype(int)              # 是否命中
    # 【2026-09-11 B 方案】按 CALIB_METHOD 分派；档位数统一由校准器自身 level_count()
    # 给出（语义对齐：isotonic=阶梯数 / platt=网格分辨率），避免两套判据漂移。
    if CALIB_METHOD == "isotonic":
        ir = IsotonicRegression(out_of_bounds="clip")
        ir.fit(fit_p, fit_y)
        cal = NumpyCalibrator(ir.X_thresholds_, ir.y_thresholds_)
    else:
        cal = _fit_platt(fit_p, fit_y)
    return cal, int(cal.level_count()), fit_p


def _train_head(X: pd.DataFrame, y: pd.Series, name: str, binary: bool) -> dict:
    """时序三分：train-A（拟合模型）/ calib-B（拟合校准器）/ test-C（评估）。

    【2026-09-11 校准协议修正】原为 80/20 两分 + OOF 汇总校准；实测校准器与最终模型
    **不同源** → 不可迁移（根因详见 `_fit_calibrator`）。现引入留出校准集 B：
        模型   ← A 段（(1-TEST-CALIB) 比例）
        校准器 ← **同一个模型**在 B 上的预测（B 未参与 fit → 无泄漏）
        指标   ← 同一个模型在 C 上的预测 + 该校准器
    代价：模型拟合样本由 80% 降为 70%（校准器必须与模型同源，B 不可参与拟合）。
    """
    idx = y.dropna().index
    Xh, yh = X.loc[idx], y.loc[idx].astype(int)
    n = len(Xh)
    cut_test = int(n * (1 - TEST_RATIO))
    cut_calib = int(n * (1 - TEST_RATIO - CALIB_RATIO))
    res: dict = {"n": int(n), "n_train": int(cut_calib),
                 "n_calib": int(cut_test - cut_calib), "n_test": int(n - cut_test)}
    if n - cut_test < 20 or cut_calib < 50 or (cut_test - cut_calib) < 30:
        res["skipped"] = "insufficient samples"
        return res

    mdl = (lgb.LGBMClassifier(objective="binary", n_estimators=200, learning_rate=0.05,
                              num_leaves=15, min_child_samples=20, subsample=0.8,
                              colsample_bytree=0.8, random_state=42, verbose=-1)
           if binary else
           lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=200,
                              learning_rate=0.05, num_leaves=15, min_child_samples=20,
                              subsample=0.8, colsample_bytree=0.8,
                              random_state=42, verbose=-1))
    mdl.fit(Xh.iloc[:cut_calib], yh.iloc[:cut_calib])
    yte = yh.iloc[cut_test:].values
    pte = mdl.predict_proba(Xh.iloc[cut_test:])
    res["test_pos"] = int((yte == 1).sum()) if binary else int((yte != 0).sum())

    # 校准器：同一 mdl 在留出校准集 B 上的预测（B 未参与 fit → 无泄漏、同分布）
    cal, lv, _ = _fit_calibrator(mdl, Xh.iloc[cut_calib:cut_test],
                                 yh.iloc[cut_calib:cut_test].values, binary)
    res["cal_levels"] = int(lv)

    if binary:
        p1 = pte[:, 1]
        res["auc"] = float(roc_auc_score(yte, p1)) if len(set(yte)) > 1 else None
        res["base_rate"] = float(yte.mean())
        pc = np.asarray(cal.predict(p1)).ravel()
        res["ece"] = _ece(yte, pc)
        res["monotonicity"] = _monotonicity(yte, pc)
    else:
        pred = mdl.predict(Xh.iloc[cut_test:])
        m = yte != 0                       # C 口径：仅真实有方向样本
        res["dir_hit"] = float((pred[m] == yte[m]).mean()) if m.sum() else None
        res["n_dir"] = int(m.sum())
        res["base_rate"] = float(m.mean()) if len(m) else None
        # 方向头校准质量：用"预测类置信"对"是否命中"做 ECE/单调性
        conf = pte.max(axis=1)
        hit = (pred == yte).astype(int)
        pc = np.asarray(cal.predict(conf)).ravel()
        res["ece"] = _ece(hit, pc)
        res["monotonicity"] = _monotonicity(hit, pc)

    res["_model"] = mdl
    res["_calib"] = cal
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="_artifacts/review_dataset.csv")
    ap.add_argument("--outdir", default="models/review_staging")
    ap.add_argument("--promote", action="store_true",
                    help="【§6.1 闸门】验收通过才把六件套提升到 --promote-dir 并双写 "
                         "ai.review.model_dir；验收 FAIL 则拒绝并 exit 1")
    ap.add_argument("--promote-dir", default="models/review_active")
    ap.add_argument("--promote-container-dir", default="/app/review_models/review_active")
    args = ap.parse_args()

    df = pd.read_csv(args.data)
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    df = df.sort_values("created_at").reset_index(drop=True)
    for c in REVIEW_FEATURE_COLS:
        if c not in df.columns:
            raise RuntimeError(f"[fatal] 数据集缺特征列 {c}（契约 REVIEW_FEATURE_COLS）")
    X = df[REVIEW_FEATURE_COLS].astype(float)

    print(f"[data] rows={len(df)} feats={len(REVIEW_FEATURE_COLS)}")
    heads = {
        "quality":   _train_head(X, df["label"], "quality", True),
        "entry":     _train_head(X, df["entry_label"], "entry", True),
        "direction": _train_head(X, df["dir_label"], "direction", False),
    }

    # ── 验收闸门（方案 §6.2）──
    checks: dict = {}
    q, e, d = heads["quality"], heads["entry"], heads["direction"]
    train_n = max(int(q.get("n_train") or 0), 0)
    checks["min_train_samples"] = {"value": train_n, "threshold": GATE_MIN_TRAIN,
                                   "ok": train_n >= GATE_MIN_TRAIN}
    for hn in ("quality", "entry"):
        h = heads[hn]
        checks[f"{hn}_auc"] = {"value": h.get("auc"), "threshold": GATE_AUC,
                               "ok": bool(h.get("auc") is not None and h["auc"] >= GATE_AUC)}
        checks[f"{hn}_ece"] = {"value": h.get("ece"), "threshold": GATE_ECE,
                               "ok": bool(h.get("ece") is not None and h["ece"] <= GATE_ECE)}
        checks[f"{hn}_monotonic"] = {"value": h.get("monotonicity"), "threshold": 0.0,
                                     "ok": bool((h.get("monotonicity") or 0) > 0)}
        checks[f"{hn}_calib_levels"] = {"value": h.get("cal_levels"),
                                        "threshold": GATE_MIN_LEVELS,
                                        "ok": bool((h.get("cal_levels") or 0) >= GATE_MIN_LEVELS)}
        checks[f"{hn}_oof_pos"] = {"value": h.get("test_pos"), "threshold": GATE_MIN_POS,
                                   "ok": bool((h.get("test_pos") or 0) >= GATE_MIN_POS)}
    checks["direction_dir_hit"] = {"value": d.get("dir_hit"), "threshold": GATE_DIR_HIT,
                                   "ok": bool(d.get("dir_hit") is not None and d["dir_hit"] >= GATE_DIR_HIT)}
    checks["direction_monotonic"] = {"value": d.get("monotonicity"), "threshold": 0.0,
                                     "ok": bool((d.get("monotonicity") or 0) > 0)}
    accepted = all(v["ok"] for v in checks.values())

    os.makedirs(args.outdir, exist_ok=True)
    for name, h in heads.items():
        if "_model" in h:
            h["_model"].booster_.save_model(os.path.join(args.outdir, f"lgbm_review_{name}.txt"))
        if "_calib" in h:
            with open(os.path.join(args.outdir, f"calib_review_{name}.pkl"), "wb") as f:
                pickle.dump(h["_calib"], f)

    report = {
        "accepted": bool(accepted),
        "feature_contract": REVIEW_FEATURE_COLS,
        "n_features": len(REVIEW_FEATURE_COLS),
        "rows": int(len(df)),
        "heads": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                  for k, v in heads.items()},
        "checks": checks,
    }
    with open(os.path.join(args.outdir, "acceptance.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n=== 三头测试集指标（时序 80/20）===")
    for k, v in heads.items():
        print(f"  {k:9s}: " + ", ".join(f"{kk}={vv}" for kk, vv in v.items() if not kk.startswith("_")))
    print("\n=== 验收闸门 ===")
    for k, v in checks.items():
        flag = "OK  " if v["ok"] else "FAIL"
        print(f"  [{flag}] {k}: value={v['value']} threshold={v['threshold']}")
    print(f"\n[acceptance] {'PASS' if accepted else 'FAIL'}  -> {args.outdir}/acceptance.json")

    # ── 验收闸门接线（方案 §6.1/§6.3）：只有 PASS 才允许 promote ──
    if args.promote:
        if not accepted:
            print("[promote] REFUSED —— 验收未通过，候选仅留 staging（不占正式目录）",
                  file=sys.stderr)
            sys.exit(1)
        if _promote(args.outdir, args.promote_dir):
            _set_model_dir(args.promote_container_dir)
        else:
            print("[promote] 有缺失产物，未更新 ai.review.model_dir", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
