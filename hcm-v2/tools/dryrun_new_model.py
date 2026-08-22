#!/usr/bin/env python3
"""只读干跑：用 M5 重训后的新模型 + align_m5 特征算当前 XAUUSD 快照的 ai_score。
不写任何数据、不碰 sidecar 进程；仅验证新模型可加载且给出同周期校准分。"""
import sys, json
sys.path.insert(0, r"D:/HCM_ASST/hcm-v2/tools")
import pandas as pd
import redis, psycopg2
import quality_scorer as qs

MODEL = r"D:/HCM_ASST/hcm-v2/tools/_aiq_artifacts/lgbm_quality_final.txt"
CALIB = r"D:/HCM_ASST/hcm-v2/tools/_aiq_artifacts/calib_final.pkl"
SYM = "XAUUSD"

r = redis.Redis(host="localhost", port=6379, db=0)
conn = psycopg2.connect("postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")

raw = r.get(f"hcm:live:hexp:{SYM}")
snap = json.loads(raw) if raw else {}
print(f"[snap] keys={list(snap.keys())[:8]} hp_score={snap.get('hp_score')} scorecard_total={snap.get('scorecard_total')}")

cur = conn.cursor()
cur.execute(
    "SELECT open_time,open,high,low,close,spread FROM hcm_market.klines "
    "WHERE symbol=%s AND time_frame='M5' ORDER BY open_time DESC LIMIT 150", (SYM,))
kdf = pd.DataFrame(cur.fetchall(),
                   columns=["open_time", "open", "high", "low", "close", "spread"])
kdf["open_time"] = pd.to_datetime(kdf["open_time"], utc=True)
kdf = kdf.sort_values("open_time").reset_index(drop=True)
kl = qs.enrich_klines(kdf)

model, iso = qs.load_model(MODEL, CALIB)
print(f"[model] loaded feature_name count={len(model.feature_name())}")

# align_m5：h1_adx/h1_trend_strength 由 M5 同源算（与部署态一致）
feats_m5 = qs.build_features(snap, kl, None, None, align_m5=True)
raw_m5 = qs.score_one(model, iso, feats_m5)
ai_m5 = raw_m5 * 100.0 if raw_m5 is not None else None
print(f"[align_m5] h1_adx={feats_m5.get('h1_adx')} h1_trend_strength={feats_m5.get('h1_trend_strength')}")
print(f"[align_m5] NEW ai_score = {ai_m5}")
print(f"[baseline] legacy(H1 旧模型) ai_score = 94.43 (重训前 live 实测)")

# 对照：同一新模型但喂 H1 旧口径（none），看输入周期本身造成的分差
_h1f = qs._h1_features(conn, SYM)
feats_h1 = qs.build_features(snap, kl, _h1f, None, align_m5=False)
raw_h1 = qs.score_one(model, iso, feats_h1)
ai_h1 = raw_h1 * 100.0 if raw_h1 is not None else None
print(f"[none/H1-input] h1_adx={feats_h1.get('h1_adx')} NEW ai_score(H1-input)={ai_h1}")
print(f"[解读] 重训效应(同 M5 输入): 旧模型94→新模型{ai_m5}; 周期效应(同新模型): M5输入{ai_m5} vs H1输入{ai_h1}")
