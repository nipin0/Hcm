"""_psi_diag2.py — 只读诊断：列出 top 漂移特征的分布结构(分箱计数/唯一值/偏度)。

不改动任何生产文件/配置；仅查询 inference_log + 打印。属 read-only 侦察。
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_retrain import fetch_recent, load_live_baseline, load_baseline
from _monitor_common import compute_psi_batch
import numpy as np, pandas as pd

baseline = load_live_baseline() or load_baseline()
src = "live" if load_live_baseline() else "train"
feat_cols = baseline["features"]
ai, feats, passed = fetch_recent(24.0)
print(f"[diag] baseline source={src} n_features={len(feat_cols)} recent_n={len(feats)}")
fdf = pd.DataFrame([{c: f.get(c, float("nan")) for c in feat_cols} for f in feats])

psi_batch = compute_psi_batch(fdf, baseline, feat_cols)
per = psi_batch["per_feature"]
items = sorted(per.items(), key=lambda kv: -kv[1])[:12]
print(f"[diag] TOP drifted (psi>0.25): {[k for k,v in per.items() if v>0.25]}")
print("=" * 100)
for name, v in items:
    raw = pd.to_numeric(fdf[name], errors="coerce").values
    col = raw[~np.isnan(raw)]
    dec = baseline["deciles"].get(name)
    edges = np.concatenate(([-np.inf], dec, [np.inf]))
    counts, _ = np.histogram(col, bins=edges)
    uniq = np.unique(col)
    skew = (col.mean() - np.median(col)) if col.size else 0
    print(f"\n### {name}: PSI={v:.4f} n={col.size} distinct={uniq.size} std={np.nanstd(col):.4f} "
          f"min={np.nanmin(col):.3f} med={np.nanmedian(col):.3f} max={np.nanmax(col):.3f}")
    if uniq.size <= 12:
        vc = pd.Series(col).value_counts().sort_index()
        print(f"   value_counts={dict(vc)}")
    else:
        print(f"   deciles={[round(x,3) for x in dec]}")
        print(f"   live_bin_counts={counts.tolist()}")
