#!/usr/bin/env python3
"""build_state_labels.py — 行情状态模型（4 类）离线标签 + 特征构造器（只读，零实盘影响）。

依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §2（标签）§3（特征）

产出：单个 CSV，列 = 元信息 + STATE_FEATURE_COLS(27 维) + label_id/label_name + 标定诊断列。
供 tools/train_state_model.py 直接消费。

纪律红线：
  * 只读 PG，不写任何数据（与 tools/build_labels.py 同纪律）。
  * 无未来泄露：特征只用 ≤ t 的已收盘 bar；标签只用 t+1..t+N。
  * 阈值**不硬编码**：全部经 hcm_config.metadata 的 state.label.* 读取，
    缺省回退 CFG_FALLBACK（初值，须先用 --suggest-thresholds 按数据分位数标定）。

用法：
  python build_state_labels.py --symbol XAUUSD --tf M5 \
      --out state_M5.csv --suggest-thresholds

  # 标定出阈值后写回配置中心，再正式产标签：
  python build_state_labels.py --symbol XAUUSD --tf M5 --out state_M5.csv
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from datetime import timezone

import numpy as np
import pandas as pd
import psycopg2

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"

# ── 特征契约模块（单一真值）：按文件路径加载，避免依赖包导入与 cwd ──
_FEAT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hcm-signal-tower", "signal_tower", "state_features.py",
)


def _load_state_features():
    spec = importlib.util.spec_from_file_location("state_features", _FEAT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load feature contract: {_FEAT_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SF = _load_state_features()
STATE_FEATURE_COLS = SF.STATE_FEATURE_COLS

# ── 标签口径默认值 ──
# 【2026-09-14 已标定】下列数值经 XAUUSD M5 网格搜索标定（方案 §14.2），
# 非拍脑袋初值；生产以配置中心 state.label.* 为准，此处仅兜底。
#
# 【分类别 horizon·数据修正 2026-09-14】曾拟"trend_init 用短窗口"（把初生看作短周期概念），
# 但 N 敏感性实测**否决**该推断：init 召回在 N=8 为 0.072、N=12 为 0.125、N=20 为 0.076
# —— 短窗口反而更差（方案 §14.4）。故 init 改回用主窗口。
# 数据支持的另一侧：trend_fade 召回随窗口增大显著改善（N=12: 0.239 → N=20: 0.447），
# 故仅 fade 保留独立的可选窗口键 state.horizon_bars_fade（默认与主窗口一致 = 不改变口径）。
CFG_FALLBACK: dict = {
    "state.horizon_bars": 12,        # 主窗口 N（osc / trend_init / trend_mid 用）
    "state.horizon_bars_fade": 12,   # 衰竭窗口（trend_fade 用；设 20 可提召回，见上注）
    "state.label.er_osc": 0.25,      # 震荡：效率比上限
    "state.label.disp_osc": 0.25,    # 震荡：ATR 归一净位移上限
    "state.label.disp_min": 0.30,    # 趋势：ATR 归一净位移下限
    "state.label.er_trend": 0.30,    # 趋势：效率比下限
    "state.label.er_fade": 0.35,     # 衰竭：后半程效率比上限
    "state.label.disp_init": 0.45,   # 初生：前半程净位移上限（前段仍在压缩）
    "state.label.adx_trend": 22.0,   # 衰竭：当前须处于趋势（ADX 下限）
    "state.label.adx_slope_min": 3.0,   # 衰竭：窗口内 ADX 下降幅下限
    "state.label.fade_ret_atr": 0.60,   # 衰竭：窗口内最大逆行(ATR 倍数)
    "state.label.er_band": 0.015,    # 置信过滤：效率比边界带
    "state.label.disp_band": 0.04,   # 置信过滤：净位移边界带
    "state.label.adx_band": 1.0,     # 置信过滤：ADX 边界带
    # ── 【路线 B · 2026-09-16】波动扩张目标阈值（**独立于 4 类 label_id 契约**）──
    # 目标定义：`vol_expansion = 1 ⟺ 未来窗口振幅/ATR ≥ 本阈值`
    #           振幅/ATR := `mfe_atr + mae_atr`（窗口内最大顺行 + 最大逆行，ATR 归一）
    # 取值依据：XAUUSD M5 全历史 67833 根的 p70 = **3.133**（由 --suggest-thresholds 给出）
    #           ⇒ 正例率 ≈ 30%，落在 [20%,40%] 的可用区间（避免 onset 那种 94.9% 正例的退化）
    # 为什么另立目标：同一特征集在"波动扩张"上 OOF AUC **0.6460**，而"形态/起点"仅
    #           **0.5541** ⇒ 换目标 +9.2pt；且单特征 atr_14 仅 0.5732 ⇒ 非平凡可预测。
    # ⚠ 本键**只影响 `vol_expansion` 列**，不参与 `classify_label` / `label_id` 的任何判定。
    "state.label.vol_amp_min": 3.13,
}

# 类别编码（顺序即 label_id，**禁止改动顺序** —— 模型类别索引契约）
#
# 【2026-09-15 定案】方向**不入形态模型**，回退 4 类；方向由独立规则模块裁决
# （signal_tower/trend_direction.py：回归斜率(ATR 归一) + ±DI + K 线防抖 → up/down/none）。
# 否决 7 类合并的实测依据：见方案 §19（M5 方向准确率 0.521 ≈ 随机）。
STATE_NAMES = ["oscillation", "trend_init", "trend_mid", "trend_fade"]
STATE_ID = {n: i for i, n in enumerate(STATE_NAMES)}


def load_config(conn) -> dict:
    """从 hcm_config.metadata 读 state.* 键；缺失回退 CFG_FALLBACK。"""
    cfg = dict(CFG_FALLBACK)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config_key, current_value FROM hcm_config.metadata "
                "WHERE config_key = ANY(%s)",
                (list(CFG_FALLBACK.keys()),),
            )
            for key, val in cur.fetchall():
                if val is not None and str(val).strip() != "":
                    try:
                        cfg[key] = float(val)
                    except (TypeError, ValueError):
                        pass
    except Exception as exc:
        print(f"[warn] config load failed, using fallback: {exc}", file=sys.stderr)
    return cfg


_UNIT_DIV = {"s": 1, "ms": 10 ** 3, "us": 10 ** 6, "ns": 10 ** 9}


def epoch_s(series) -> np.ndarray:
    """时间列 → Unix 秒（UTC）。**全仓唯一实现**，各工具脚本一律调用本函数。

    ⚠ 单位陷阱（2026-09-15 实测，影响多个脚本的对齐）：
      · `.astype("int64")` 的数值**取决于 dtype 的单位**。pandas 2.x 的 `to_datetime`
        对**字符串列**返回 `datetime64[us]`（微秒），而对 DB 取回的时间可能返回 `[ns]`。
        若一律 `//1e9`，在 [us] 时会得到"千秒"→ 把 5 分钟 bar 塌缩（20168 行 → 6071 唯一），
        且**两侧同公式 → 匹配检查仍通过**，于是按 bar 对齐的指标静默取错行（M5 最多偏 3 根）。
      · `.astype("datetime64[ns]")` 对 **tz-aware** 会直接抛 TypeError（不能从带时区转不带）。
    故按 `dtype.unit`（tz-aware 用 `.unit`，朴素用 `np.datetime_data`）换算，不假设单位。
    """
    t = pd.to_datetime(series, utc=True)
    try:
        unit = str(t.dtype.unit)              # DatetimeTZDtype
    except AttributeError:
        unit = str(np.datetime_data(t.dtype)[0])   # 朴素 datetime64[...]
    return (t.astype("int64") // _UNIT_DIV.get(unit, 10 ** 9)).to_numpy()


def load_klines(conn, symbol: str, tf: str) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(
            # 【2026-09-16 L1】补取 tick_volume / spread —— 供 L1 候选特征集
            # （`state_features.L1_FEATURE_COLS`）使用。base 契约仍只用 OHLC，
            # 故对既有模型/推理无任何影响（见 state_features.py 中 L1 段说明）。
            "SELECT open_time, open, high, low, close, tick_volume, spread "
            "FROM hcm_market.klines "
            "WHERE symbol = %s AND time_frame = %s ORDER BY open_time",
            (symbol, tf),
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    return df.sort_values("open_time").reset_index(drop=True)


# ────────────────────────────── 标签度量 ──────────────────────────────

def _path(closes: np.ndarray) -> float:
    if len(closes) < 2:
        return 0.0
    return float(np.abs(np.diff(closes)).sum())


def window_metrics(
    i: int, n: int, high: np.ndarray, low: np.ndarray, close: np.ndarray,
    atr: np.ndarray, adx: np.ndarray, lookback: int,
) -> dict | None:
    """计算 bar i 在未来长度 n 窗口上的全部结构度量（只用 t+1..t+n，无未来泄露）。

    拆出本函数是为了支持「分类别 horizon」：init 用短窗口、mid/fade 用主窗口，
    两侧都调本函数再合并（避免口径分叉出两份实现）。

    Returns:
        dict 或 None（未来窗口不足 / ATR 不可用）。
    """
    t_end = i + n
    if t_end >= len(close):
        return None
    if i - lookback + 1 < 0:
        return None

    atr_t = float(atr[i])
    if not np.isfinite(atr_t) or atr_t <= 0.0:
        return None

    c0 = float(close[i])
    c1 = float(close[t_end])
    delta = c1 - c0
    path = _path(close[i: t_end + 1])
    er = abs(delta) / path if path > 1e-12 else 0.0
    disp = abs(delta) / atr_t

    mid = i + n // 2
    c_mid = float(close[mid])
    delta2 = c1 - c_mid      # 后段（mid→t_end）位移；trend_init 的方向取自此
    path1 = _path(close[i: mid + 1])
    path2 = _path(close[mid: t_end + 1])
    er1 = abs(c_mid - c0) / path1 if path1 > 1e-12 else 0.0
    er2 = abs(delta2) / path2 if path2 > 1e-12 else 0.0
    disp1 = abs(c_mid - c0) / atr_t
    disp2 = abs(delta2) / atr_t

    adx_t = float(adx[i])
    adx_slope = float(adx[t_end]) - adx_t

    # 顺向创新极值（相对 t 之前的 lookback 窗口，无泄露）
    prior_hi = float(high[i - lookback + 1: i + 1].max())
    prior_lo = float(low[i - lookback + 1: i + 1].min())
    fut_hi = float(high[i + 1: t_end + 1].max())
    fut_lo = float(low[i + 1: t_end + 1].min())
    if delta > 0:
        new_ext_dir = fut_hi > prior_hi
    elif delta < 0:
        new_ext_dir = fut_lo < prior_lo
    else:
        new_ext_dir = False

    # 最大逆行 / 顺行（ATR 归一）
    w_close = close[i + 1: t_end + 1]
    if delta >= 0:
        mae = float((c0 - w_close.min())) / atr_t
        mfe = float((w_close.max() - c0)) / atr_t
    else:
        mae = float((w_close.max() - c0)) / atr_t
        mfe = float((c0 - w_close.min())) / atr_t

    return {
        "n": n, "delta": delta, "delta2": delta2,
        "er": er, "disp": disp, "er1": er1, "er2": er2,
        "disp1": disp1, "disp2": disp2, "adx_t": adx_t, "adx_slope": adx_slope,
        "mae_atr": mae, "mfe_atr": mfe, "new_ext_dir": new_ext_dir,
    }


def label_metrics(
    i: int, n_main: int, n_fade: int, high: np.ndarray, low: np.ndarray,
    close: np.ndarray, atr: np.ndarray, adx: np.ndarray, lookback: int,
) -> dict | None:
    """合并主窗口(main)与衰竭窗口(fade)的度量。

    键空间：主窗口字段用原名（osc / trend_init / trend_mid 消费），
    衰竭窗口字段加 `_f` 后缀（trend_fade 消费）。
    `n_fade == n_main` 时复用同一份度量（零额外开销，且保证口径完全一致）。
    """
    main = window_metrics(i, n_main, high, low, close, atr, adx, lookback)
    if main is None:
        return None
    fd = main if n_fade == n_main else window_metrics(
        i, n_fade, high, low, close, atr, adx, lookback)
    if fd is None:
        return None
    merged = dict(main)
    merged.update({
        "er2_f": fd["er2"], "mae_f": fd["mae_atr"], "adx_slope_f": fd["adx_slope"],
        "new_ext_f": fd["new_ext_dir"], "n_fade": fd["n"],
    })
    return merged


def classify_label(m: dict, cfg: dict) -> tuple[str | None, str]:
    """按方案 §2.2 优先级判类（4 类形态，**不含方向**）；返回 (类别名 | None, 原因)。

    窗口口径：osc / trend_init / trend_mid 用主窗口字段；
    trend_fade 用 `_f` 后缀的衰竭窗口字段（默认与主窗口相同）。
    方向不在本模块裁决（由 trend_direction.py 独立判定，见 STATE_NAMES 上方注释）。
    """
    # 1) 震荡（主窗口；方向无序即震荡）
    if m["er"] <= cfg["state.label.er_osc"] and m["disp"] <= cfg["state.label.disp_osc"]:
        return "oscillation", "ok_osc"
    # 2) 趋势衰竭（衰竭窗口）
    if (m["adx_t"] >= cfg["state.label.adx_trend"]
            and m["adx_slope_f"] <= -cfg["state.label.adx_slope_min"]
            and (m["er2_f"] <= cfg["state.label.er_fade"]
                 or m["mae_f"] >= cfg["state.label.fade_ret_atr"])
            and not m["new_ext_f"]):
        return "trend_fade", "ok_fade"
    # 3) 趋势初生（主窗口：前段静、后段动）
    if (m["disp1"] <= cfg["state.label.disp_init"]
            and m["er1"] <= cfg["state.label.er_osc"]
            and m["disp2"] >= cfg["state.label.disp_min"]
            and m["er2"] >= cfg["state.label.er_trend"]):
        return "trend_init", "ok_init"
    # 4) 趋势中段（主窗口）
    if m["er"] >= cfg["state.label.er_trend"] and m["disp"] >= cfg["state.label.disp_min"]:
        return "trend_mid", "ok_mid"
    return None, "ambiguous"


def confidence_reject(m: dict, cfg: dict) -> str | None:
    """置信过滤（方案 §2.3）：命中即剔除样本，返回原因。"""
    if abs(m["er"] - cfg["state.label.er_osc"]) < cfg["state.label.er_band"]:
        return "band_er_osc"
    if abs(m["er"] - cfg["state.label.er_trend"]) < cfg["state.label.er_band"]:
        return "band_er_trend"
    if abs(m["er2"] - cfg["state.label.er_trend"]) < cfg["state.label.er_band"]:
        return "band_er2_trend"
    if abs(m["disp"] - cfg["state.label.disp_min"]) < cfg["state.label.disp_band"]:
        return "band_disp_min"
    if abs(m["adx_t"] - cfg["state.label.adx_trend"]) < cfg["state.label.adx_band"]:
        return "band_adx_trend"
    return None


# ────────────────────────────── 主流程 ──────────────────────────────

def suggest_thresholds(mets: pd.DataFrame) -> None:
    """按数据分位数打印阈值建议（方案 §2.4 第 1 步）。"""
    print("\n[suggest] 阈值分位数建议（请据此写回配置中心 state.label.*）", file=sys.stderr)
    q = {
        "state.label.er_osc": ("er", 30),
        "state.label.er_trend": ("er", 60),
        "state.label.er_fade": ("er2_f", 30),
        "state.label.disp_osc": ("disp", 25),
        "state.label.disp_min": ("disp", 45),
        "state.label.disp_init": ("disp1", 30),
        "state.label.adx_trend": ("adx_t", 55),
    }
    for key, (col, pct) in q.items():
        val = float(np.nanpercentile(mets[col].to_numpy(dtype=float), pct))
        print(f"  {key:28s} = {val:.4f}   (p{pct} of {col})", file=sys.stderr)
    slope = mets["adx_slope_f"].to_numpy(dtype=float)
    print(f"  {'state.label.adx_slope_min':28s} = "
          f"{abs(float(np.nanpercentile(slope, 10))):.4f}   (|p10| of adx_slope)", file=sys.stderr)
    mae = mets["mae_atr"].to_numpy(dtype=float)
    print(f"  {'state.label.fade_ret_atr':28s} = "
          f"{float(np.nanpercentile(mae, 60)):.4f}   (p60 of mae_atr)", file=sys.stderr)
    # 【路线 B】波动扩张目标阈值建议：p70 ⇒ 正例率 ≈30%（落在 [20%,40%] 可用区间）
    amp = (mets["mfe_atr"].to_numpy(dtype=float) + mets["mae_atr"].to_numpy(dtype=float))
    print(f"  {'state.label.vol_amp_min':28s} = "
          f"{float(np.nanpercentile(amp, 70)):.4f}   "
          f"(p70 of mfe_atr+mae_atr；路线 B 波动扩张目标)", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5", choices=["M5", "M15", "H1"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--horizon", type=int, default=None, help="覆盖 state.horizon_bars（主窗口）")
    ap.add_argument("--horizon-fade", type=int, default=None,
                    help="覆盖 state.horizon_bars_fade（衰竭专用窗口）")
    ap.add_argument("--box-window", type=int, default=None, help="覆盖 state.box.window")
    ap.add_argument("--set", action="append", default=None, metavar="KEY=VALUE",
                    help="临时覆盖任一配置键（标定用，可重复），如 --set state.label.er_osc=0.15")
    ap.add_argument("--suggest-thresholds", action="store_true",
                    help="打印分位数阈值建议（不产 CSV 时也可单独跑）")
    args = ap.parse_args()

    out = args.out or f"state_{args.tf}.csv"

    conn = psycopg2.connect(args.db_url)
    try:
        cfg = load_config(conn)
        for item in (args.set or []):
            if "=" not in item:
                raise SystemExit(f"[fatal] --set 需 KEY=VALUE 形式：{item}")
            k, v = item.split("=", 1)
            k = k.strip()
            if k not in CFG_FALLBACK:
                raise SystemExit(f"[fatal] --set 未知键：{k}")
            cfg[k] = float(v)
        n = int(args.horizon if args.horizon is not None else cfg["state.horizon_bars"])
        n_fade = int(args.horizon_fade if args.horizon_fade is not None
                     else cfg["state.horizon_bars_fade"])
        params = dict(SF.DEFAULT_PARAMS)
        if args.box_window is not None:
            params["box_window"] = int(args.box_window)
        lookback = params["win_window"]
        print(f"[cfg] tf={args.tf} N_main={n} N_fade={n_fade} "
              f"box_window={params['box_window']} win_window={lookback}", file=sys.stderr)
        print(f"[cfg] thresholds={ {k: v for k, v in cfg.items() if k.startswith('state.label')} }",
              file=sys.stderr)

        kl = load_klines(conn, args.symbol, args.tf)
        if kl.empty:
            print(f"[error] no klines for {args.symbol} {args.tf}", file=sys.stderr)
            return
        print(f"[klines] {args.symbol} {args.tf} bars={len(kl)} "
              f"range={kl['open_time'].iloc[0]} .. {kl['open_time'].iloc[-1]}", file=sys.stderr)

        high = kl["high"].to_numpy(dtype=float)
        low = kl["low"].to_numpy(dtype=float)
        close = kl["close"].to_numpy(dtype=float)
        # open_time → Unix 秒（UTC）：**必须用本模块顶部的 `epoch_s`**（全仓唯一实现，
        # 按 `dtype.unit` 换算，见其 docstring 的单位陷阱说明）。
        # ⚠ 【2026-09-15 实测修复】此前此处手写 `astype("int64") // 1e9`，而 `load_klines`
        #   的 dtype 实测为 `datetime64[us, UTC]`（**微秒**）→ 得到的是"千秒"：
        #   68592 根 bar 只剩 20706 个唯一值（比值 0.302），并把 `_session_flags` 的输入
        #   从"时刻"变成**随日期单调漂移的量**。实证：标签里 `session_us` 是
        #   "前 150 天恒 0、后 150 天恒 1"的**日期开关**（只有 10 个不同"小时"取值），
        #   与正确时段值的一致率仅 0.494（≈随机）。
        #   而**线上推理传的是正确秒**（`scheduler.py` 的 `_as_epoch_s` → `int(dt.timestamp())`）
        #   ⇒ 构成**训练/推理偏斜**。危害已量化：v3 的这两列重要性合计仅 0.68%
        #   （session_eu 265.8 / session_us 59.4，总计约 48001）→ 缺陷真实但影响小；
        #   已训练的模型仍可用，**重训非必需**（如重训，本修复会自动生效）。
        open_epoch_s = epoch_s(kl["open_time"])

        ind = SF.compute_indicators(high, low, close, params)
        atr, adx = ind["atr"], ind["adx"]

        feat_rows: list[dict] = []
        idxs: list[int] = []
        mets_rows: list[dict] = []

        need = SF.min_bars(params)
        # 【L1】量价/点差数组：存在则透传 ⇒ `compute_features_at` 会额外返回 L1_FEATURE_COLS
        # （`rec.update(feat)` 会自动把新列写进 CSV，无需改本文件的下游构造）。
        _vol = kl["tick_volume"].to_numpy(dtype=float) if "tick_volume" in kl.columns else None
        _spr = kl["spread"].to_numpy(dtype=float) if "spread" in kl.columns else None
        for i in range(need - 1, len(close)):
            feat = SF.compute_features_at(i, high, low, close, ind, open_epoch_s, params,
                                          volume=_vol, spread=_spr)
            if feat is None:
                continue
            m = label_metrics(i, n, n_fade, high, low, close, atr, adx, lookback)
            if m is None:
                continue
            feat_rows.append(feat)
            mets_rows.append(m)
            idxs.append(i)

        if not feat_rows:
            print("[error] no usable samples (bars insufficient)", file=sys.stderr)
            return

        mets_df = pd.DataFrame(mets_rows)
        if args.suggest_thresholds:
            suggest_thresholds(mets_df)

        recs = []
        for k, i in enumerate(idxs):
            feat, m = feat_rows[k], mets_rows[k]
            rej = confidence_reject(m, cfg)
            if rej:
                name, reason = None, f"conf_{rej}"
            else:
                name, reason = classify_label(m, cfg)
            rec = {
                "symbol": args.symbol,
                "time_frame": args.tf,
                "open_time": kl["open_time"].iloc[i].isoformat(),
                "bar_index": i,
            }
            rec.update(feat)
            rec.update({
                "label_name": name,
                "label_id": STATE_ID[name] if name else None,
                "reject_reason": reason,
                # 主窗口度量（osc / trend_mid / trend_fade 消费）
                "er": round(float(m["er"]), 6),
                "disp": round(float(m["disp"]), 6),
                "er1": round(float(m["er1"]), 6),
                "er2": round(float(m["er2"]), 6),
                "disp1": round(float(m["disp1"]), 6),
                "disp2": round(float(m["disp2"]), 6),
                # 衰竭窗口度量（trend_fade 消费，分类别 horizon）
                "er2_f": round(float(m["er2_f"]), 6),
                "mae_f": round(float(m["mae_f"]), 4),
                "adx_slope_f": round(float(m["adx_slope_f"]), 4),
                "new_ext_f": bool(m["new_ext_f"]),
                "adx_t": round(float(m["adx_t"]), 4),
                "adx_slope": round(float(m["adx_slope"]), 4),
                "mae_atr": round(float(m["mae_atr"]), 4),
                "mfe_atr": round(float(m["mfe_atr"]), 4),
                "new_ext_dir": bool(m["new_ext_dir"]),
                # 位移（带符号）：方向标签的取值依据，落盘供审计
                "delta": round(float(m["delta"]), 6),
                "delta2": round(float(m["delta2"]), 6),
                # ── 【路线 B · 2026-09-16】波动扩张目标（**与 label_id 并列、互不影响**）──
                # `vol_amp_atr` 落盘原始值，供参数重标定与审计；
                # `vol_expansion` 是可直接训练的 0/1 目标（阈值走 state.label.vol_amp_min）。
                # 为什么不用"数据集分位"直接定标签：那会让目标随数据窗口漂移（不可复现）；
                # 用配置中的绝对值 ⇒ 口径稳定、可跨窗口/跨品种对照（与其它 state.label.* 同构）。
                "vol_amp_atr": round(float(m["mfe_atr"]) + float(m["mae_atr"]), 4),
                "vol_expansion": int((float(m["mfe_atr"]) + float(m["mae_atr"]))
                                     >= float(cfg["state.label.vol_amp_min"])),
            })
            recs.append(rec)

        df = pd.DataFrame(recs)
        df.to_csv(out, index=False)

        total = len(df)
        labeled = df[df["label_id"].notna()]
        print(f"\n[done] total={total} labeled={len(labeled)} "
              f"dropped={total - len(labeled)} ({((total - len(labeled)) / total):.1%})")
        print(f"[label_dist] {labeled['label_name'].value_counts().to_dict()}")
        print(f"[label_dist%] "
              f"{(labeled['label_name'].value_counts(normalize=True) * 100).round(1).to_dict()}")
        print(f"[drop_reasons] "
              f"{df[df['label_id'].isna()]['reject_reason'].value_counts().to_dict()}")
        print(f"[out] {out}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
