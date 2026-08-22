import sys, numpy as np, pandas as pd
sys.path.insert(0, "D:/HCM_ASST/hcm-v2/tools")
from _model_feature_cols import MODEL_FEATURE_COLS
df = pd.read_csv("D:/HCM_ASST/hcm-v2/tools/models/features_m5.csv")
cols = [c for c in MODEL_FEATURE_COLS if c in df.columns]
X = df[cols].astype(float).fillna(0.0)
corr = X.corr().abs()
pairs = []
for i in range(len(cols)):
    for j in range(i+1, len(cols)):
        c = corr.iloc[i, j]
        if c > 0.9:
            pairs.append((cols[i], cols[j], round(float(c), 3)))
pairs.sort(key=lambda x: -x[2])
print("HIGH CORR PAIRS (>0.9):")
for p in pairs: print("  ", p)
print("\nAPPROX VIF (1/(1-R^2)) for VIF>5:")
import numpy.linalg as la
for c in cols:
    others = [x for x in cols if x != c]
    Xo = X[others].values
    Xc = X[c].values
    A = np.column_stack([Xo, np.ones(len(Xo))])
    coef, _, _, _ = la.lstsq(A, Xc, rcond=None)
    pred = A @ coef
    ss_res = ((Xc - pred)**2).sum()
    ss_tot = ((Xc - Xc.mean())**2).sum()
    r2 = 1 - ss_res/ss_tot if ss_tot > 0 else 0
    vif = 1/(1-r2) if r2 < 1 else 999.0
    if vif > 5:
        print(f"  {c}: VIF≈{vif:.1f}")
