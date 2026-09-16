"""validate_shape_semantics.py — 形状簇的**语义验证**（接线前的前置门）。

为什么必须做：`discover_state_labels` 的簇是由**未来路径签名**聚类得来的（零阈值）。
若直接拿它的名字（quiet / advancing / exhausted）去裁决订单，就是"用一个未经检验的
标签拦单"。§24.3 已经出现过一次警讯：当时的 `exhausted` 簇占 61.5%、只是"低效 + 没守住"
的大杂烩 → 命名与事实相反。

**如何避免循环论证**：簇标签来自「未来」，故不能再用「未来」去验证它。本脚本用两个
**与聚类无关**的独立维度：

  1. **接续性检验（主判据，且与交易直接相关）**
        past_dir  = sign(过去窗口回归斜率)        ← 只用过去（特征列，参与簇定义的只有未来签名）
        fwd_disp  = (close[t+N] − close[t]) / ATR ← 未来位移
        cont      = past_dir × fwd_disp
     语义要求：
        · `advancing` → cont 显著 > 0（**过去的方向在延续**）
        · `exhausted` → cont 显著 < 0（**过去的方向失效/回吐**）
        · `quiet`     → |fwd_disp| 明显更小（本就没有趋势可言）
     若 `exhausted` 的 cont ≥ 0，则该名字**语义不成立** → 只可作观测，**不得用于裁决**。

  2. **过去强度画像**：各簇的 |slope|、ADX 均值。"衰竭"应富集于**过去确有趋势**的 bar；
     若是大杂烩，则其过去画像与 quiet 无异。

  3. **与人工资签的一致性**：形状 × 4 类标签交叉表（两套独立定义，可互相印证）。

用法：
    python validate_shape_semantics.py --shapes _scratch/shapes_M5.csv --labels _scratch/state_M5_v2.csv
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
BSL = _load("build_state_labels", os.path.join(_TOOLS, "build_state_labels.py"))


# 时间→秒的换算**统一委托 build_state_labels.epoch_s**（单一真值，含单位陷阱说明）
epoch_s = BSL.epoch_s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5", choices=["M5", "M15", "H1"])
    ap.add_argument("--shapes", default="_scratch/shapes_M5.csv")
    ap.add_argument("--labels", default="_scratch/state_M5_v2.csv")
    ap.add_argument("--horizon", type=int, default=12)
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--min-atr", type=float, default=0.10,
                    help="判据量级门槛：|cont 中位| 至少达此 ATR（防噪声级差异被放过）")
    ap.add_argument("--min-dev", type=float, default=0.05,
                    help="判据偏离门槛：|延续占比−50%%| 至少达此值")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    conn = psycopg2.connect(args.db_url)
    try:
        kl = BSL.load_klines(conn, args.symbol, args.tf)
    finally:
        conn.close()
    if kl.empty:
        raise SystemExit(f"[fatal] 无 K 线：{args.symbol} {args.tf}")

    high = kl["high"].to_numpy(float)
    low = kl["low"].to_numpy(float)
    close = kl["close"].to_numpy(float)
    params = dict(SF.DEFAULT_PARAMS)
    ind = SF.compute_indicators(high, low, close, params)
    atr = np.asarray(ind["atr"], dtype=float)
    adx = np.asarray(ind["adx"], dtype=float)
    k_ep = epoch_s(kl["open_time"])

    sh = pd.read_csv(args.shapes)
    lb = pd.read_csv(args.labels)
    for d in (sh, lb):
        d["_ep"] = epoch_s(d["open_time"])
    cols = ["_ep"] + [c for c in SF.STATE_FEATURE_COLS
                      if c in lb.columns and c in ("slope_linreg", "adx_14", "rsi")]
    m = sh.merge(lb[cols + (["label_name"] if "label_name" in lb.columns else [])],
                 on="_ep", how="inner")
    if m.empty:
        raise SystemExit("[fatal] shapes 与 labels 无法对齐（检查 open_time 时区/格式）")

    # 行 → K 线下标
    pos = np.searchsorted(k_ep, m["_ep"].to_numpy())
    np.clip(pos, 0, len(k_ep) - 1, out=pos)
    ok = k_ep[pos] == m["_ep"].to_numpy()
    m = m[ok].reset_index(drop=True)
    pos = pos[ok]
    N = args.horizon
    keep = pos + N < len(close)
    m, pos = m[keep].reset_index(drop=True), pos[keep]

    # 接续性：past_dir(过去) × fwd_disp(未来)
    past_dir = np.sign(m["slope_linreg"].to_numpy(dtype=float))
    fwd = (close[pos + N] - close[pos]) / atr[pos]
    cont = past_dir * fwd
    m["fwd_atr"] = fwd
    m["cont"] = cont
    m["past_abs_slope"] = np.abs(m["slope_linreg"].to_numpy(dtype=float))
    m["adx"] = adx[pos]
    valid = past_dir != 0

    print(f"[data] {args.symbol} {args.tf} 对齐样本 {len(m)}（有过去方向的 {int(valid.sum())}）"
          f"  N={N}")
    name_col = "cluster_name" if "cluster_name" in m.columns else "cluster"

    print("\n=========== 1) 接续性检验（主判据；past_dir × 未来位移）===========")
    print(f"  {'形状':<12}{'n':>7}{'cont均值':>11}{'cont中位':>11}"
          f"{'延续占比':>10}{'|fwd|均值':>11}")
    verdict: dict = {}
    for nm, g in m[valid].groupby(name_col):
        c = g["cont"].to_numpy()
        row = (len(g), float(c.mean()), float(np.median(c)),
               float((c > 0).mean()), float(np.abs(g["fwd_atr"]).mean()))
        verdict[nm] = row
        print(f"  {nm:<12}{row[0]:>7}{row[1]:>+11.4f}{row[2]:>+11.4f}"
              f"{row[3]:>10.1%}{row[4]:>11.4f}")
    print("\n  读法：advancing 应 cont>0（延续）；exhausted 应 cont<0（失效/回吐）；"
          "quiet 的 |fwd| 应最小")

    print("\n=========== 2) 过去强度画像（独立于未来签名）===========")
    print(f"  {'形状':<12}{'n':>7}{'|slope|均值':>13}{'ADX均值':>10}{'过去有向占比':>14}")
    for nm, g in m.groupby(name_col):
        print(f"  {nm:<12}{len(g):>7}{g['past_abs_slope'].mean():>13.4f}"
              f"{g['adx'].mean():>10.2f}{float((np.sign(g['slope_linreg']) != 0).mean()):>14.1%}")

    if "label_name" in m.columns:
        print("\n=========== 3) 与 4 类人工标签交叉（两套独立定义）===========")
        ct = pd.crosstab(m[name_col], m["label_name"], normalize="index")
        print(ct.map(lambda v: f"{v:.1%}").to_string())

    # ── 判据 ──
    # 【判据必须带"量级"要求】初版只判"cont 中位符号"（>0 / <0），导致 exhausted 的
    # 中位 −0.0064 ATR 被误判为"成立" —— 那是噪声级差异（约 0.6% 个 ATR），无交易含义。
    # 且两侧的"延续占比"都 ≈48.8%（≈50%）说明**根本没有延续性信息**。
    # 故要求同时满足：(a) 符号正确；(b) 量级 ≥ MIN_ATR；(c) 延续占比偏离 50% ≥ MIN_DEV。
    MIN_ATR = float(args.min_atr)
    MIN_DEV = float(args.min_dev)
    print(f"\n=========== 判据（要求：符号正确 且 |cont中位| ≥ {MIN_ATR} ATR "
          f"且 |延续占比−50%| ≥ {MIN_DEV:.0%}）===========")
    adv = verdict.get("advancing")
    exh = verdict.get("exhausted")
    qui = verdict.get("quiet")

    def _ok(v, want_pos: bool):
        if not v:
            return False, "缺该簇"
        dev = abs(v[3] - 0.5)
        sign_ok = (v[2] > 0) if want_pos else (v[2] < 0)
        mag_ok = abs(v[2]) >= MIN_ATR
        dev_ok = dev >= MIN_DEV
        why = []
        if not sign_ok:
            why.append("符号不符")
        if not mag_ok:
            why.append(f"量级不足(|{v[2]:.4f}|<{MIN_ATR})")
        if not dev_ok:
            why.append(f"延续占比≈50%(偏离{dev:.1%}<{MIN_DEV:.0%})")
        return (sign_ok and mag_ok and dev_ok), ("、".join(why) if why else "全部满足")

    ok_adv, why_a = _ok(adv, True)
    ok_exh, why_e = _ok(exh, False)
    print(f"  advancing 语义（cont 中位 > 0 = 过去方向延续）: "
          f"{'成立 ✓' if ok_adv else '不成立 ✗'}"
          + (f"  cont中位={adv[2]:+.4f} 延续占比={adv[3]:.1%}  ← {why_a}" if adv else ""))
    print(f"  exhausted 语义（cont 中位 < 0 = 过去方向失效）: "
          f"{'成立 ✓' if ok_exh else '不成立 ✗'}"
          + (f"  cont中位={exh[2]:+.4f} 延续占比={exh[3]:.1%}  ← {why_e}" if exh else ""))
    if qui:
        smallest = qui[4] < min(v[4] for k, v in verdict.items() if k != "quiet")
        print(f"  quiet  |fwd| 是否最小: {'是 ✓' if smallest else '否 ✗'}  |fwd|={qui[4]:.4f}")
    print()
    if ok_adv and ok_exh:
        print("  ⇒ 两簇语义**均获证实**，可进入接线裁决。")
    else:
        print("  ⇒ 形状簇语义**未获证实**（至少 `advancing`/`exhausted` 之一不成立）"
              "\n     → 按 §32.3 的决定：**仅作观测、不得用于裁决**；"
              "接线时只把形状写入观测字段，不参与开仓/加仓判定。")
        print("     ⚠ 另注：三簇的「延续占比」都在 48~49%（≈50%）→ 形状簇**不携带"
              "\n       「过去趋势是否延续」的信息**，故它无法承担'趋势是否收尾'的裁决职责。")


if __name__ == "__main__":
    main()
