"""build_drift_base.py — 产出 PSI 漂移基准（`lgbm_state_{TF}_drift_base.npz`）。

为什么必须有它
──────────────
`state.drift.ref_kind=adaptive`（前窗口自比较）实测**信噪比倒置**：
  · 滚动基线 max_psi 中位 **4.07**（n=1104 采样点）；
  · 人为把 `atr_14` 放大 1.5 倍的**真漂移**仅 **2.80** ⇒ 真漂移淹没在噪声里。
改用 `file`（长基准 vs 短当前）后：基线 p50 **2.94** / 真漂移 **7.09** ⇒ 信噪比恢复。
故生产应使用 `ref_kind=file` + 本脚本产出的基准。

产物
────
`{out}/lgbm_state_{TF}_drift_base.npz`：`X`(m,k) + `cols`(k,) + 若干 meta 标量。
`feature_drift.load_reference_from_file` 读取时会**校验列名一致**，不一致即回退 adaptive。

用法
────
    python tools/build_drift_base.py --symbol XAUUSD --tf M5 --lookback 50000
    python tools/build_drift_base.py --symbol XAUUSD --tf M5 --out d:/HCM_ASST/hcm-v2/review_models
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SIG = os.path.join(_ROOT, "hcm-signal-tower")
for _p in (_ROOT, _SIG):
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from signal_tower import feature_drift as FD  # noqa: E402
from signal_tower.state_features import (  # noqa: E402
    STATE_FEATURE_COLS,
    compute_features_at,
    compute_indicators,
    min_bars,
)

DB_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"


def epoch_seconds(ts) -> np.ndarray:
    """时间戳 → Unix 秒（UTC）。与 `state_features` 口径一致。"""
    s = pd.to_datetime(pd.Series(ts), utc=True)
    return np.asarray((s - pd.Timestamp(0, tz="UTC")).dt.total_seconds(), dtype=np.int64)


def build_feature_matrix(high, low, close, eps, cols):
    """逐根调度 `compute_features_at`（**口径单一真值**；此处仅做遍历与组装）。

    为什么不复用 `lgbm_fsm.features`：那是阶段 A 的**研究影子包**，
    生产工具不应依赖它（否则"可一键删除"的性质被破坏）。
    """
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)
    ind = compute_indicators(high, low, close)
    rows: list = []
    for i in range(min_bars(), len(close)):
        f = compute_features_at(i, high, low, close, ind, eps)
        if f is None:
            continue
        try:
            vec = [float(f[c]) for c in cols]
        except (KeyError, TypeError, ValueError):
            continue
        if not np.all(np.isfinite(vec)):
            continue
        rows.append(vec)
    return np.asarray(rows, dtype=float) if rows else np.empty((0, len(cols)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5")
    ap.add_argument("--lookback", type=int, default=50000,
                    help="基准取样根数（越多越稳；建议 >=20000）")
    # ⚠ 输出目录必须是**容器内 `/app/review_models` 的挂载源** ——
    #   实测 compose 为 `./tools/models:/app/review_models:ro`（只读），
    #   放到别处容器里读不到（`load_reference_from_file` 会静默回退 adaptive）。
    ap.add_argument("--out", default=os.path.join(_ROOT, "tools", "models"))
    ap.add_argument("--db-url", default=DB_DEFAULT)
    ap.add_argument("--use-l1", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(args.db_url)
    q = ("SELECT open_time, high, low, close, tick_volume AS volume, spread "
         "FROM hcm_market.klines "
         "WHERE symbol=%s AND time_frame=%s ORDER BY open_time DESC LIMIT %s")
    kl = pd.read_sql(q, conn, params=(args.symbol, args.tf, int(args.lookback)))
    conn.close()
    if kl is None or len(kl) == 0:
        print(f"[fatal] 无 K 线：{args.symbol} {args.tf}")
        return 2
    kl = kl.iloc[::-1].reset_index(drop=True)
    print(f"[data] {args.symbol} {args.tf} rows={len(kl)}  "
          f"{kl['open_time'].iloc[0]} .. {kl['open_time'].iloc[-1]}")

    high = np.asarray(kl["high"], float)
    low = np.asarray(kl["low"], float)
    close = np.asarray(kl["close"], float)
    eps = epoch_seconds(kl["open_time"])
    vol = np.asarray(kl["volume"], float) if kl["volume"].notna().any() else None
    spr = np.asarray(kl["spread"], float) if kl["spread"].notna().any() else None
    if args.use_l1 and (vol is None or spr is None):
        print("[warn] --use-l1 但 volume/spread 缺失 → 回退 base 27 维")
        vol = spr = None

    cols = [str(c) for c in STATE_FEATURE_COLS]
    if args.use_l1:
        # 本脚本刻意只支持 base 契约：L1 需 volume/spread 且生产推理侧只按 base 取列，
        # 基准与推理必须同列（否则加载时 contract 校验会拒绝）。
        print("[warn] --use-l1 暂不支持（基准须与生产推理同列=base 27）→ 按 base 生成")
    X = build_feature_matrix(high, low, close, eps, cols)
    if X.shape[0] == 0:
        print("[fatal] 特征矩阵为空（K 线不足）")
        return 4
    if X.shape[1] != len(cols):
        print(f"[fatal] 列数不符：X={X.shape[1]} cols={len(cols)}")
        return 3
    print(f"[feat] X={X.shape}")

    # 滚动基线（用于**阈值标定**，不是基准本身）：
    # 基准固定 = 前 BASE_N 根（模拟"产出基准时的训练段"），当前段 = 其后滚动的 cur_n 根
    # —— 这模拟"基准上线后随时间推移"的真实情形。
    base_cfg = FD.DictCfg({})
    cur_n = FD.window_bars(base_cfg)
    BASE_N = min(10000, X.shape[0] // 2)
    max_psis: list = []
    step = max(1, (X.shape[0] - BASE_N) // 400)
    if step < 1:
        step = 1
    for t in range(BASE_N, X.shape[0] - cur_n, step):
        ref, cur = X[:BASE_N], X[t:t + cur_n]
        vals = np.asarray([FD.calculate_psi(ref[:, j], cur[:, j], 10)
                           for j in range(X.shape[1])], float)
        if np.any(np.isfinite(vals)):
            max_psis.append(float(np.nanmax(vals)))
    mp = np.asarray(max_psis, float)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"lgbm_state_{args.tf.upper()}_drift_base.npz")
    # ⚠ `cols` **不得**用 `dtype=object`：object 数组在 `np.load(allow_pickle=False)`
    #   下会直接抛 "Object arrays cannot be loaded when allow_pickle=False"
    #   ⇒ `load_reference_from_file` 静默回退 adaptive（**且线上表现为"配置没生效"**）。
    #   用默认的 unicode dtype（`<U…`）即可安全存取。
    np.savez_compressed(
        path,
        X=X.astype(np.float32),
        cols=np.asarray(cols),
        symbol=np.asarray([args.symbol]),
        tf=np.asarray([args.tf.upper()]),
        n=np.asarray([X.shape[0]]),
        t0=np.asarray([str(kl["open_time"].iloc[0])]),
        t1=np.asarray([str(kl["open_time"].iloc[-1])]),
    )
    print(f"[saved] {path}  X={X.shape}")

    if mp.size:
        p90, p99 = np.percentile(mp, 90), np.percentile(mp, 99)
        print(f"\n[阈值标定参考]（以本基准 vs 滚动当前段，n={mp.size}）")
        print(f"  p50={np.percentile(mp,50):.3f}  p90={p90:.3f}  p99={p99:.3f}")
        print(f"  ⇒ 建议 state.drift.psi_warn={p90:.2f}  state.drift.psi_block={p99:.2f}")
        print("  ⚠ 这两个值**显著大于** 0.2 —— 行业惯例阈值 0.1/0.2 是为"
              "'大样本+稳定特征'设计的，对 M5 波动类特征不适用（否则 100% 报警）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
