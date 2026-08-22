"""Temporary diagnostic: compare baseline deciles vs live inference feature distribution."""
import json as J, os, sys
import numpy as np
import psycopg2
import pandas as pd

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from _monitor_common import load_baseline

bl = load_baseline()
dec = bl.get("deciles", {})
feats = bl.get("features", [])
print(f"baseline features={len(feats)} deciles_keys={len(dec)}")

DB = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"
conn = psycopg2.connect(DB, connect_timeout=10)
cur = conn.cursor()
cur.execute("SELECT features FROM hcm_ai.inference_log ORDER BY created_at DESC LIMIT 15000")
rows = cur.fetchall()
recs = []
for (f,) in rows:
    if isinstance(f, str):
        try: f = J.loads(f)
        except Exception: continue
    if isinstance(f, dict):
        recs.append(f)
print(f"live samples parsed={len(recs)}")
df = pd.DataFrame(recs)

for feat in feats:
    if feat not in dec or feat not in df.columns:
        continue
    col = pd.to_numeric(df[feat], errors="coerce").dropna().values
    if col.size < 10:
        continue
    d = dec[feat]
    qs = np.percentile(col, [1, 5, 10, 50, 90, 95, 99])
    # PSI contribution
    edges = np.concatenate(([-np.inf], np.asarray(d, float), [np.inf]))
    counts, _ = np.histogram(col, bins=edges)
    n = counts.sum()
    ap = counts / n if n > 0 else np.zeros(10)
    ex = np.full(10, 0.1)
    eps = 1e-4
    ap = np.clip(ap, eps, 1.0); ex = np.clip(ex, eps, 1.0)
    psi = float(np.sum((ap - ex) * np.log(ap / ex)))
    print(f"\n{feat}: PSI={psi:.3f}")
    print(f"  baseline deciles p10..p90 = {np.round(d,3)}")
    print(f"  live     p1/p10/p50/p90/p99 = {np.round(qs,3)}")
