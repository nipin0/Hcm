"""discover_state_labels.py — **数据驱动的状态发现**（替代人工阈值定义标签）。

用户要求（2026-09-15 原话）：
  · "解决'不可学'"（§22：trend_init 的定义依赖未来路径形状 → 在 bar t 不可观测）
  · "解决'无签名'"（缺压缩/能量积聚类特征）
  · **"必须让状态机自主学习，具备不同周期的状态识别能力"**

设计要点（三条，缺一不可）：
  1. **零人工阈值**：状态不由"er ≥ 0.30 且 disp ≥ 0.30"这类阈值定义，而由**未来路径签名
     向量**（连续统计量）上的**无监督聚类**发现。人只做一件事：给簇起名字（rank 规则，
     无绝对阈值）。→ 满足"不再人工写阈值"。
  2. **签名必须建在"未来"上**：⚠ 关键陷阱 —— 若对**当前特征**聚类当标签，LGBM 会完美拟合
     聚类边界（F1 虚高），但状态退化成过去的确定性函数 → **提前量归零**，等于又造一个滞后
     指标。故聚类只建在 t+1..t+N 的路径描述上，标签**编码未来**，模型才可能"提前"。
  3. **按周期独立**：M5/M15/H1 各自跑本流程、各自训练，禁止混用（用户原始硬约束）。

未来路径签名（全部 ATR 归一 / 无量纲，**不含任何阈值**）：
  fwd_abs_ret    |净位移|          ← 取绝对值，**去方向**
  fwd_path       总路径长度
  fwd_er         效率 = |净位移| / 总路径        （0~1）
  fwd_mfe        最大有利偏移（按净位移方向）
  fwd_mae        最大不利偏移（反向）
  fwd_abs_slope  |未来窗口线性回归斜率|           ← 取绝对值，**去方向**
  fwd_consist    同向 bar 占比                    （0~1）
  fwd_vol_ratio  后段波动 / 前段波动              （>1 = 波动扩张）
  fwd_rng_ratio  后半段极差 / 前半段极差          （>1 = 先静后动）
  fwd_ext_pos    极值出现的相对位置               （0=窗口初 1=窗口末）
  fwd_brk_pos    首次突破 t 时刻箱体边界的相对位置（-1 = 未突破）

【为什么签名必须去方向（2026-09-15 实测教训）】
    首版签名含带符号 `fwd_ret` / `fwd_slope`，k=4 聚类实测出来的簇是
    「c1: ret=+1.156 强上涨 / c3: ret=−1.387 强下跌 / c0: ret≈0 / c2: 扩张启动」
    —— **簇结构被"方向"主导，而非"阶段"**，自动命名因此把强上涨簇误命名为 oscillation。
    而本方案的架构里**方向本就由 `trend_direction` 独立负责**，状态只需描述"形状/阶段"。
    故签名一律去方向（取绝对值），否则等于把方向信息重复塞进状态标签，
    既与方向模块冲突，也让状态不可解释。

用法：
    python discover_state_labels.py --symbol XAUUSD --tf M5 --out _scratch/state_clusters_M5.csv
    python discover_state_labels.py --symbol XAUUSD --tf M5 --k 4 --bic-scan
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import numpy as np
import pandas as pd
import psycopg2
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
_SIG = os.path.join(_ROOT, "hcm-signal-tower", "signal_tower")

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"

SIG_COLS = [
    "fwd_abs_ret", "fwd_path", "fwd_er", "fwd_mfe", "fwd_mae", "fwd_abs_slope",
    "fwd_consist", "fwd_vol_ratio", "fwd_rng_ratio", "fwd_ext_pos", "fwd_brk_pos",
    "fwd_giveback", "fwd_er_h1", "fwd_er_h2", "fwd_rev_h2",
]

# 【形状空间】只保留**尺度无关**维度（比值 / 窗口内相对位置），刻意**剔除幅度绝对量**
# （fwd_abs_ret / fwd_path / fwd_mfe / fwd_mae / fwd_abs_slope）。
# 依据：§23.3 实测——「是否会有行情」可学（osc vs trend AUC 0.99），「行情多大」不可学
# （init vs mid 仅 0.588）。若把绝对幅度留在聚类空间里，k=3 必然又按"大/中/小"分层，
# 得到的仍是不可学的类别。故形状空间必须只描述"路径长什么样"，不描述"走了多远"；
# 幅度交由**仓位管理**承担。
#
# 【2026-09-15 修正】用**两段式**替换整窗 `fwd_giveback`（§24.3 实测其失效）：
#   整窗回吐率在纯噪声路径上也趋近 1（net≈0 而 mfe>0）→ **无法区分"涨完回吐"与"从未推进"**，
#   实测导致"衰竭"簇占了 61.5%。改为按窗口前后半程分别度量，把**时间结构**显式化：
#     fwd_er_h1  前半程效率（"是否推进过"）
#     fwd_er_h2  后半程效率（"现在是否还在推进"）
#     fwd_rev_h2 后半程相对前半程的**反向度**（尺度无关、有界）：
#                −sign(net1)·|net2| / (|net1|+|net2|)
#                沿续 → 负；反向 → 正；纯噪声两侧都小 → 接近 0
SHAPE_COLS = [
    "fwd_er",          # 整窗效率（净位移 / 总路径）
    "fwd_er_h1",       # 前半程效率 → "曾推进过吗"
    "fwd_er_h2",       # 后半程效率 → "现在还在推进吗"
    "fwd_rev_h2",      # 后半程反向度 → 衰竭的关键判别（正 = 吐回）
    "fwd_consist",     # 同向占比
    "fwd_vol_ratio",   # 后段波动 / 前段波动
    "fwd_rng_ratio",   # 后半极差 / 前半极差
    "fwd_ext_pos",     # 极值在窗口内的相对位置
    "fwd_brk_pos",     # 首次破箱体的相对位置
]

# 语义命名用的**排序依据**（rank 规则，无绝对阈值）
NAME_HINTS = {
    # k=4（旧四类，保留作对照）
    "oscillation": "fwd_abs_ret 最小（净位移≈0 = 无方向）",
    "trend_init": "fwd_rng_ratio 最高（先静后动）+ 波动扩张",
    "trend_mid": "fwd_er 最高（全程高效推进）",
    "trend_fade": "极值早 + 反向回吐大（fwd_ext_pos 小 & fwd_mae 大）",
    # k=3（三件套形状）
    "quiet": "效率低 + 回吐低（无推进）→ 箱体震荡策略",
    "advancing": "效率高 + 回吐低（持续推进）→ 顺势策略",
    "exhausted": "回吐率高（有利偏移被吐回）→ 收紧止损 / 只持有",
}


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SF = _load("state_features", os.path.join(_SIG, "state_features.py"))
BSL = _load("build_state_labels", os.path.join(_TOOLS, "build_state_labels.py"))


def future_signature(i: int, n: int, high: np.ndarray, low: np.ndarray,
                     close: np.ndarray, atr: np.ndarray, box_w: int) -> dict | None:
    """bar i 的**未来** N 根路径签名（只用 t+1..t+N，零阈值）。"""
    if i + n >= len(close) or not np.isfinite(atr[i]) or atr[i] <= 0.0:
        return None
    a = float(atr[i])
    seg_c = close[i + 1: i + n + 1]
    seg_h = high[i + 1: i + n + 1]
    seg_l = low[i + 1: i + n + 1]
    if len(seg_c) < n:
        return None
    c0 = float(close[i])

    ret = (float(seg_c[-1]) - c0) / a
    path = float(np.abs(np.diff(np.concatenate(([c0], seg_c)))).sum()) / a
    er = abs(ret) / path if path > 0 else 0.0

    # 有利/不利偏移：按净位移方向定义（方向来自未来本身，仅用于描述，不入特征）
    up = ret >= 0.0
    if up:
        mfe = (float(seg_h.max()) - c0) / a
        mae = max(0.0, (c0 - float(seg_l.min())) / a)
        ext_idx = int(np.argmax(seg_h))
    else:
        mfe = (c0 - float(seg_l.min())) / a
        mae = max(0.0, (float(seg_h.max()) - c0) / a)
        ext_idx = int(np.argmin(seg_l))

    # 未来窗口斜率（ATR 归一）
    x = np.arange(len(seg_c), dtype=float)
    xm, ym = x.mean(), seg_c.mean()
    sxx = float(((x - xm) ** 2).sum())
    slope = 0.0 if sxx <= 0 else float(((x - xm) * (seg_c - ym)).sum()) / sxx
    slope_atr = slope * n / a

    d = np.diff(np.concatenate(([c0], seg_c)))
    consist = float((np.sign(d) == np.sign(ret)).mean()) if ret != 0 else 0.0

    half = n // 2
    first, second = seg_c[:half], seg_c[half:]
    vol1 = float(np.abs(np.diff(first)).mean()) if len(first) > 1 else 0.0
    vol2 = float(np.abs(np.diff(second)).mean()) if len(second) > 1 else 0.0
    vol_ratio = (vol2 / vol1) if vol1 > 1e-12 else 0.0
    rng1 = float(first.max() - first.min()) if len(first) else 0.0
    rng2 = float(second.max() - second.min()) if len(second) else 0.0
    rng_ratio = (rng2 / rng1) if rng1 > 1e-12 else 0.0

    ext_pos = float(ext_idx) / max(1, n - 1)

    # 首次突破 t 时刻箱体边界的位置（-1 = 未突破）
    box_hi = float(high[i - box_w + 1: i + 1].max())
    box_lo = float(low[i - box_w + 1: i + 1].min())
    brk = -1.0
    for j in range(n):
        if seg_c[j] > box_hi or seg_c[j] < box_lo:
            brk = j / max(1, n - 1)
            break

    # 回吐率：有利偏移里有多少被吐回去（**保留作诊断**，已从形状空间剔除，理由见 SHAPE_COLS 注）
    giveback = (mfe - abs(ret)) / mfe if mfe > 1e-12 else 0.0
    giveback = float(min(max(giveback, 0.0), 1.0))

    # ── 两段式：显式表达"时间结构"（前后半程各自的推进情况）──
    mid_c = float(first[-1]) if len(first) else c0     # 前半程结束价 = close[t+half]
    net1 = (mid_c - c0) / a
    net2 = (float(seg_c[-1]) - mid_c) / a
    p1 = float(np.abs(np.diff(np.concatenate(([c0], first)))).sum()) if len(first) else 0.0
    p2 = float(np.abs(np.diff(second)).sum()) if len(second) > 1 else 0.0
    er_h1 = abs(net1) / p1 if p1 > 1e-12 else 0.0
    er_h2 = abs(net2) / p2 if p2 > 1e-12 else 0.0
    # 反向度：沿续为负、反转为正；两侧位移都小时接近 0（纯噪声不会被误判为反转）
    _den = abs(net1) + abs(net2) + 1e-12
    rev_h2 = float(-np.sign(net1) * abs(net2) / _den) if abs(net1) > 1e-12 else 0.0

    out = {
        "fwd_abs_ret": abs(ret), "fwd_path": path, "fwd_er": er, "fwd_mfe": mfe,
        "fwd_mae": mae, "fwd_abs_slope": abs(slope_atr), "fwd_consist": consist,
        "fwd_vol_ratio": min(vol_ratio, 10.0), "fwd_rng_ratio": min(rng_ratio, 10.0),
        "fwd_ext_pos": ext_pos, "fwd_brk_pos": brk, "fwd_giveback": giveback,
        "fwd_er_h1": er_h1, "fwd_er_h2": er_h2, "fwd_rev_h2": rev_h2,
    }
    return out if all(np.isfinite(v) for v in out.values()) else None


def auto_name3(prof: pd.DataFrame) -> dict:
    """k=3 的三类**形状**命名（rank 规则，无绝对阈值）。

    语义与策略对应（三件套的"形状"部分）：
      quiet      → 无推进：效率低、回吐低       → 箱体震荡策略
      advancing  → 推进中：效率高、回吐低       → 顺势策略
      exhausted  → 收尾/回吐：回吐率高           → 收紧移动止损 / 只持有

    打分（只用排名，不写死数值；基于**两段式**维度，见 SHAPE_COLS 注）：
      advancing = rank(er_h2) − rank(rev_h2)      取最大 → 后半程仍在推进 且 不反向
      exhausted = rank(er_h1) + rank(rev_h2)      取最大 → **曾推进过** 且 后半程吐回
      其余      = quiet                            → 两段效率都低（一直没推进）
    """
    ids = list(prof.index)
    score_adv = prof["fwd_er_h2"].rank() - prof["fwd_rev_h2"].rank()
    score_exh = prof["fwd_er_h1"].rank() + prof["fwd_rev_h2"].rank()
    names: dict = {}
    names[score_adv.idxmax()] = "advancing"
    rest = [c for c in ids if c not in names]
    if rest:
        names[score_exh.loc[rest].idxmax()] = "exhausted"
    for c in ids:
        names.setdefault(c, "quiet")
    return names


def auto_name(prof: pd.DataFrame) -> dict:
    """按 **rank 规则**（无绝对阈值）把簇映射到四类语义。返回 {cluster_id: 名称}。

    规则顺序（每条只用排名，不写死数值）：
      1. fwd_abs_ret 最小者 → oscillation（净位移≈0 = 无方向；**不能只看 er**：
         实测 er 最低的簇 net=+1.156 ATR 是"低效率上涨"，不是震荡）
      2. 其余中 fwd_rng_ratio 最大者 → trend_init（先静后动）
      3. 其余中 fwd_ext_pos 最小 且 fwd_mae 较大者 → trend_fade（极值早 + 回吐大）
      4. 剩下 → trend_mid
    """
    ids = list(prof.index)
    names: dict = {}
    osc = prof["fwd_abs_ret"].idxmin()
    names[osc] = "oscillation"
    rest = [c for c in ids if c not in names]
    if rest:
        init = prof.loc[rest, "fwd_rng_ratio"].idxmax()
        names[init] = "trend_init"
    rest = [c for c in ids if c not in names]
    if len(rest) >= 2:
        # 衰竭 = 极值位置靠前（早）+ 回吐大；用两项排名之和
        score = prof.loc[rest, "fwd_ext_pos"].rank() + prof.loc[rest, "fwd_mae"].rank(ascending=False)
        fade = score.idxmax()
        names[fade] = "trend_fade"
    for c in ids:
        names.setdefault(c, "trend_mid")
    return names


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5", choices=["M5", "M15", "H1"])
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--horizon", type=int, default=12, help="未来窗口 N")
    ap.add_argument("--box-window", type=int, default=20)
    ap.add_argument("--k", type=int, default=4, help="簇数（三件套形状对应 3）")
    ap.add_argument("--space", default="full", choices=["full", "shape"],
                    help="聚类空间：full=含绝对幅度（会按大小分层）；"
                         "shape=仅尺度无关形状维（三件套应选此项）")
    ap.add_argument("--bic-scan", action="store_true", help="扫描 k=2..7 打印 BIC")
    ap.add_argument("--out", default=None, help="输出 CSV（含 cluster 与签名列）")
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
    if kl.empty or len(kl) < 500:
        raise SystemExit(f"[fatal] K 线不足：{args.symbol} {args.tf} rows={len(kl)}")

    high = kl["high"].to_numpy(float)
    low = kl["low"].to_numpy(float)
    close = kl["close"].to_numpy(float)
    params = dict(SF.DEFAULT_PARAMS)
    ind = SF.compute_indicators(high, low, close, params)
    atr = np.asarray(ind["atr"], dtype=float)

    n = len(close)
    rows, idxs = [], []
    need = SF.min_bars(params)
    for i in range(need - 1, n):
        s = future_signature(i, args.horizon, high, low, close, atr, args.box_window)
        if s is None:
            continue
        rows.append(s)
        idxs.append(i)
    if len(rows) < 200:
        raise SystemExit(f"[fatal] 签名样本不足：{len(rows)}")

    sig = pd.DataFrame(rows, columns=SIG_COLS)
    use_cols = SHAPE_COLS if args.space == "shape" else SIG_COLS
    print(f"[data] {args.symbol} {args.tf} 签名样本={len(sig)} N={args.horizon} "
          f"box_w={args.box_window} space={args.space}({len(use_cols)} 维)")
    print("[sig] 各维分布（分位数，用于判断是否需要缩尾）：")
    print(sig[use_cols].describe(percentiles=[.05, .5, .95]).T[["mean", "std", "5%", "50%", "95%"]]
          .to_string(float_format=lambda v: f"{v:.3f}"))

    X = StandardScaler().fit_transform(sig[use_cols].to_numpy())

    if args.bic_scan:
        print("\n[bic-scan] k → BIC / AIC（越小越好，仅作参考：BIC 偏少簇）")
        for k in range(2, 8):
            g = GaussianMixture(n_components=k, covariance_type="full",
                                random_state=42, n_init=3).fit(X)
            print(f"  k={k}  BIC={g.bic(X):>14.1f}  AIC={g.aic(X):>14.1f}")

    gm = GaussianMixture(n_components=args.k, covariance_type="full",
                         random_state=42, n_init=5).fit(X)
    lab = gm.predict(X)

    # ── 簇画像（决定语义命名，也是"是否真的发现了四类"的直接证据）──
    prof = sig.copy()
    prof["cluster"] = lab
    mean_prof = prof.groupby("cluster")[SIG_COLS].mean()
    share = prof["cluster"].value_counts(normalize=True).sort_index()
    mapping = auto_name3(mean_prof) if args.k == 3 else auto_name(mean_prof)

    print(f"\n=========== 簇画像（k={args.k}，space={args.space}，零阈值）===========")
    # 先列**形状维**（聚类依据），末尾附 fwd_abs_ret 仅供检查"是否仍按幅度分层"
    show = ["fwd_er", "fwd_er_h1", "fwd_er_h2", "fwd_rev_h2", "fwd_consist",
            "fwd_rng_ratio", "fwd_ext_pos", "fwd_brk_pos", "fwd_abs_ret"]
    hdr = "".join(f"{c.replace('fwd_',''):>11}" for c in show)
    print(f"  {'cluster':<9}{'share':>8}{hdr}{'→ 语义':>16}")
    for c in mean_prof.index:
        r = mean_prof.loc[c]
        line = "".join(f"{r[col]:>11.3f}" for col in show)
        print(f"  c{c:<8}{share[c]:>8.1%}{line}{mapping[c]:>16}")

    # ── 与旧人工口径的对照：簇是否能被"当前特征"学到？（下一步才是关键）──
    print("\n=========== 命名依据（rank 规则，无绝对阈值）===========")
    for nm, hint in NAME_HINTS.items():
        who = [f"c{c}" for c, v in mapping.items() if v == nm]
        print(f"  {nm:<12} ← {', '.join(who):<10} {hint}")

    if args.out:
        out = kl.iloc[idxs][["open_time"]].reset_index(drop=True).copy()
        out = pd.concat([out, sig, pd.DataFrame({"cluster": lab})], axis=1)
        out["cluster_name"] = out["cluster"].map(mapping)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        out.to_csv(args.out, index=False)
        print(f"\n[out] {args.out}  rows={len(out)}  "
              f"分布={out['cluster_name'].value_counts().to_dict()}")
        # 下一步（不在本脚本内）：用 LGBM 学「当前特征 → cluster_name」，
        # 再用 eval_state_leadtime.py 验收提前量。切勿跳过验收。

    print("\n⚠ 下一步验收不可跳过：必须用 eval_state_leadtime.py 量到提前量改善，"
          "否则'可学'只是把滞后指标换了个包装。")


if __name__ == "__main__":
    main()
