#!/usr/bin/env python3
"""timesfm_features.py — TimesFM 离线时序特征提取器（M2）。

依据：【精简正式版】TimesFM+LightGBM_最优融合架构开发&验收规范
      + docs/timesfm_lightgbm_开发文档_v1.0.md（§4 / §5 / §16）

═══ 铁律（违反即破坏架构）═══
1. **只离线运行**：每日收盘后批量执行，绝不进线上推理链路、绝不输出信号。
2. **原始 Embedding 必须 PCA 降维**（8–16 维），禁止高维直接训练。
3. **PCA 仅训练期拟合并固定复用** —— 全历史拟合会引入未来信息（未来函数）。
4. **相似度检索只匹配历史**：检索库仅含 `s <= t - gap`（gap >= horizon）。
5. 特征严格按 bar `open_time` 对齐，写入即落 `bar_time`。

═══ 产出（每根 bar 一行）═══
  tmf_pc00..tmf_pc07 : M5 PCA 分量（§16.5：仅主周期落 PCA；实测定案 8 维）
  tmf_trend_cont     : 趋势延续得分      ∈[-1,1]
  tmf_rev_prob       : 趋势反转概率      ∈[0,1]
  tmf_vol_cycle      : 波动周期强度      (相对倍数)
  tmf_mtf_resonance  : 多周期共振得分    ∈[-1,1]
  tmf_hist_sim       : 历史行情相似度    ∈[-1,1]

═══ 运行环境 ═══
**独立 venv（禁止使用生产 C:\\Python313）**：D:\\.venv_timesfm
  Python 3.13 + torch(cpu) + timesfm==3.0.0 + scikit-learn

═══ 实测环境结论（2026-08-29，务必知悉）═══
- timesfm 1.3.0 要求 Python <3.12，**本机 3.13 不可用**；必须用 3.0.0。
  3.0.0 内含 `timesfm/timesfm_2p5/timesfm_2p5_torch.py`，向后兼容 2.5 权重 ✅
  且 `DEFAULT_REPO_ID = "google/timesfm-2.5-200m-pytorch"` 与《规范》指定模型一致 ✅
- **Windows 必须 torch_compile=False**（权重 config.json 亦为 false）。
- **Xet 后端主机 cas-server.xethub.hf.co 在本环境超时不可达**，会导致
  hf_hub_download 静默挂起（CPU=0、缓存 0 字节）→ 必须 `HF_HUB_DISABLE_XET=1`。
- **huggingface.co 官方 CDN 限速约 114KB/s（全局上限，多线程并行无效）**，
  882MB 权重需约 2 小时；hf-mirror.com 返回 308 不可用；ModelScope 大文件 403。
  建议先用 .NET/浏览器下载到本地目录，再用 --model-dir 离线加载。
- **TimesFM 2.5 为单变量模型**：`forecast()` 只接受一维序列。
  《规范》§2.1"输入字段 OHLCV"应理解为"数据取自 OHLCV K 线"，实际建模序列用 close。
  （多变量可另用 `forecast_with_covariates`，但非本次范围。）

═══ 用法 ═══
  # 1) 拟合 PCA（**只在训练期时间窗内**执行，禁止跨到未来）
  python timesfm_features.py --fit-pca --pca-out models/tmf_pca_v1.pkl \
      --pca-start 2026-01-01 --pca-end 2026-06-30

  # 2) 抽取特征并写库
  python timesfm_features.py --extract --pca models/tmf_pca_v1.pkl \
      --start 2026-07-01 --end 2026-08-29

注意：本脚本为离线批处理，**不写 Redis、不接触任何线上进程**。
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from datetime import datetime, timezone

import numpy as np

# ── 必须在使用 huggingface_hub 之前设置：Xet 后端在本环境不可达，会静默挂起 ──
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"

# TimesFM 2.5 约束（取自 google/timesfm-2.5-200m-pytorch/config.json）
PATCH_LEN = 32          # patch_length：max_context 须为其整数倍
HIDDEN_SIZE = 1280      # hidden_size：embedding 维度
OUT_PATCH = 128         # horizon_length：max_horizon 须为其整数倍
N_QUANTILES = 9         # quantiles: 0.1..0.9

# 默认参数（均可命令行覆盖，禁止硬编码散落 —— 铁律四）
CTX_BARS = 512          # 输入窗口（规范：256–512）
HORIZON = 12            # 预测步数（对齐质量头 label_horizon_bars=12）
# PCA 降维目标（《规范》要求 8–16）。
# 【2026-08-29 实测定案 = 8】逐样本 L2 归一化后的累计解释方差：
#   k=4 68.91% | k=6 78.10% | k=8 84.07% | k=10 87.62% | k=12 90.14% | k=16 93.90%
# 边际增益在 k=8 之后明显放缓（+2.82 -> +1.24）；8→12 仅 +6.07 个百分点却多占 4 维，
# 在标注样本约 440 条的预算下不划算，故取 8。
PCA_DIM = 8
HIST_LIB = 2048         # 相似度检索库容量（历史 bar 数）
MTF_WEIGHTS = {"M1": 0.15, "M5": 0.25, "M15": 0.25, "H1": 0.35}

TF_ORDER = ["M1", "M5", "M15", "H1"]

DB_TIMEFRAMES = {"M1": "M1", "M5": "M5", "M15": "M15", "H1": "H1"}

FEATURE_PREFIX = ["tmf_trend_cont", "tmf_rev_prob", "tmf_vol_cycle",
                  "tmf_mtf_resonance", "tmf_hist_sim"]


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def _normalize_embeddings(pool: np.ndarray, mode: str) -> np.ndarray:
    """对 embedding 做**逐样本**归一化。

    【实测 2026-08-29 · 关键】不做归一化时，embedding 的逐样本模长差异（激活
    尺度随输入价位漂移）压倒一切：PCA 的 **PC1 独占 99.95% 方差**，1280 维实际
    退化为 1 个标量，PCA 维数无论取 8 还是 12 都无意义。
    逐样本 L2 归一化后 PC1 降至 **32.84%**，曲线恢复正常：
        k=4 -> 68.91%   k=6 -> 78.10%   k=8 -> 84.07%
        k=10 -> 87.62%  k=12 -> 90.14%  k=16 -> 93.90%
    边际增益在 k=8 之后明显放缓（+2.82 -> +1.24），故在样本预算紧张时取 8 维。

    注意：`--normalize-inputs` 对本函数无效（实测 embedding 逐位相同），
    因其只作用于 forecast() 的编译解码路径，而 embedding 经 forward() 直接提取。
    """
    if mode == "none":
        return pool
    if mode != "l2":
        raise ValueError(f"未知的 embed-norm: {mode}（可选 none/l2）")
    norm = np.linalg.norm(pool, axis=-1, keepdims=True)
    norm[norm == 0] = 1.0
    return pool / norm


def _to_naive_utc64(ts_seq) -> np.ndarray:
    """把 tz-aware 的 datetime 序列统一转换为 **UTC naive** 的 np.datetime64 数组。

    直接 `np.datetime64(t)` 会静默丢弃时区并触发
    `UserWarning: no explicit representation of timezones available for np.datetime64`。
    此处显式转 UTC 再去掉 tzinfo，保证与命令行传入的 naive 日期字符串
    （按 UTC 解释）口径严格一致，避免跨时区错位。
    """
    return np.array([
        np.datetime64(t.astimezone(timezone.utc).replace(tzinfo=None))
        if getattr(t, "tzinfo", None) is not None
        else np.datetime64(t)
        for t in ts_seq
    ])


# ══════════════════ K 线读取（只读 PG）═════════════════
def load_klines(conn, symbol: str, time_frame: str) -> tuple[np.ndarray, np.ndarray]:
    """返回 (open_time 数组, close 数组)，按时间升序。"""
    import psycopg2

    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_time, close FROM hcm_market.klines "
            "WHERE symbol = %s AND time_frame = %s ORDER BY open_time",
            (symbol, time_frame),
        )
        rows = cur.fetchall()
    if not rows:
        return np.array([]), np.array([], dtype=float)
    ts = np.array([r[0] for r in rows])
    close = np.array([float(r[1]) for r in rows], dtype=float)
    return ts, close


# ══════════════════ TimesFM 加载 ══════════════════
def load_model(model_dir: str | None, repo_id: str, normalize_inputs: bool = False):
    """加载 TimesFM 2.5。优先本地目录（离线、快），否则从 HF 下载。

    normalize_inputs：让 TimesFM 在推理前对输入做实例归一化。
    【实测 2026-08-29】喂原始价格水平（normalize_inputs=False）时，激活尺度随价位漂移，
    导致 embedding 的"逐样本模长"差异压倒一切 —— 未归一化时 PCA 的 PC1 独占
    99.95% 方差，embedding 实际退化为 1 个标量，无法提供有效特征。
    开启本项或事后对 embedding 做逐样本 L2 归一化，可使 PCA 曲线恢复正常。
    """
    import timesfm
    from timesfm.configs import ForecastConfig

    kwargs = {"torch_compile": False}  # Windows 必须关闭
    src = model_dir if model_dir else repo_id
    log(f"loading TimesFM from {src} (torch_compile=False)")
    t0 = time.time()
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(src, **kwargs)
    log(f"loaded in {time.time() - t0:.1f}s")

    fc = ForecastConfig(
        max_context=CTX_BARS,
        max_horizon=OUT_PATCH,
        per_core_batch_size=1,
        normalize_inputs=normalize_inputs,
    )
    model.compile(fc)
    log(f"compiled ForecastConfig(max_context={CTX_BARS}, max_horizon={OUT_PATCH})")
    return model


# ══════════════════ 单窗口推理：预测 + embedding ══════════════════
def infer_window(model, window: np.ndarray, horizon: int):
    """对一段 close 序列做推理。

    返回 (point, quant, pooled_embedding)
      point  : (horizon,)            点预测
      quant  : (horizon, N_QUANTILES) 分位数预测
      pooled : (HIDDEN_SIZE,)         output_embeddings 沿 patch 维均值池化
    """
    import torch

    point, quant = model.forecast(horizon=horizon, inputs=[window])
    p = np.asarray(point)[0]      # (horizon,)
    q = np.asarray(quant)[0]      # (horizon, 9)

    # 取 output_embeddings（transformer 末层隐状态）→ 池化为 1280 维向量
    # 【关键】forecast() 内部会做 patch 分块，但直接调 forward() 会绕过分块。
    # tokenizer 期待 (batch, n_patches, patch_length=32)，与 mask 沿最后一维拼接成
    # 64 维后再投影到 hidden_size；若直接传 (1, len(window)) 会被当作 len(window) 个
    # 独立特征 → RuntimeError: mat1 and mat2 shapes cannot be multiplied。
    # 故此处必须自行分块，并截断到 patch_length 的整数倍。
    m = model.model
    n_patch = len(window) // PATCH_LEN
    usable = n_patch * PATCH_LEN
    if n_patch <= 0:
        raise ValueError(f"窗口长度 {len(window)} 不足一个 patch({PATCH_LEN})")
    x = torch.from_numpy(window[-usable:].astype(np.float32)).reshape(1, n_patch, PATCH_LEN)
    mask = torch.ones_like(x)
    with torch.no_grad():
        (_, output_emb, _, _), _ = m.forward(x, mask)
    pooled = output_emb.mean(dim=1).squeeze(0).numpy().astype(np.float64)
    return p, q, pooled


# ══════════════════ 5 类结构化特征（§4.4 显式定义）═════════════════
def trend_cont(close_now: float, forecast: np.ndarray, hist_std: float, h: int) -> float:
    """趋势延续得分 = tanh( (ŷ_h - c_t) / (h·σ_Δ) )  ∈[-1,1]"""
    denom = h * (hist_std + 1e-12)
    return float(np.tanh((forecast[h - 1] - close_now) / denom))


def rev_prob(close_now: float, forecast: np.ndarray) -> float:
    """趋势反转概率：路径极值后终值反向 → 回撤占比 ∈[0,1]"""
    dev = forecast - close_now
    k_peak = int(np.argmax(np.abs(dev)))
    if k_peak == len(dev) - 1:
        return 0.0
    d_peak, d_end = dev[k_peak], dev[-1]
    if np.sign(d_peak) == np.sign(d_end) or abs(d_peak) < 1e-12:
        return 0.0
    return float(min(1.0, max(0.0, abs(d_end) / (abs(d_peak) + 1e-12))))


def vol_cycle(forecast: np.ndarray, hist_std: float) -> float:
    """波动周期强度 = std(预测路径) / std(历史逐根收益)"""
    return float(np.std(forecast) / (hist_std + 1e-12))


def mtf_resonance(per_tf_cont: dict[str, float]) -> float:
    """多周期共振得分 = Σ w·tanh(cont) / Σ w  ∈[-1,1]"""
    num = den = 0.0
    for tf, w in MTF_WEIGHTS.items():
        c = per_tf_cont.get(tf)
        if c is None:
            continue
        num += w * float(np.tanh(c))
        den += w
    return float(num / den) if den > 0 else 0.0


def hist_sim(vec: np.ndarray, lib: np.ndarray) -> float:
    """历史行情相似度 = 与检索库中历史向量的最大余弦相似度 ∈[-1,1]

    铁律：lib 只含 `s <= t - gap` 的历史向量（由调用方保证），杜绝未来泄露。
    """
    if lib.size == 0:
        return 0.0
    v = vec / (np.linalg.norm(vec) + 1e-12)
    L = lib / (np.linalg.norm(lib, axis=1, keepdims=True) + 1e-12)
    return float(np.max(L @ v))


# ══════════════════ PCA（§4.3：仅训练期拟合）═════════════════
def fit_pca(pool: np.ndarray, dim: int):
    """在**训练期**的 bar 级 embedding 上拟合 PCA。

    pool: (n_bars, HIDDEN_SIZE)，n 应为 bar 级（数万），
    严禁只用信号级样本（约 440 条）拟合 —— 维数远大于样本数会导致协方差奇异。
    """
    from sklearn.decomposition import PCA

    n, d = pool.shape
    log(f"fitting PCA on {n} bar-level embeddings (dim={d} -> {dim})")
    if n < dim * 10:
        log(f"[warn] 样本量偏少 (n={n} < {dim * 10})，PCA 估计可能不稳")
    k = int(min(dim, n, d))
    pca = PCA(n_components=k, random_state=42)
    pca.fit(pool)
    evr = pca.explained_variance_ratio_
    cum_total = float(np.sum(evr))

    # 输出 k=1..K 的累计解释方差表 —— 用于"按达到目标解释方差的最小 k"选维，
    # 而不是拍脑袋定 8 或 12。
    log("累计解释方差表（PCA 维数选型依据）：")
    cum = 0.0
    table: list[dict] = []
    for i in range(k):
        cum += float(evr[i])
        table.append({"k": i + 1, "evr": float(evr[i]), "cumulative": cum})
        log(f"    k={i + 1:2d}   单维={float(evr[i]) * 100:6.3f}%   累计={cum * 100:6.2f}%")
    for target in (0.70, 0.80, 0.90):
        hit = next((t["k"] for t in table if t["cumulative"] >= target), None)
        if hit is None:
            log(f"    达到 {int(target * 100)}% 累计解释方差 -> 当前 k={k} 仍未达到，需增大 --pca-dim")
        else:
            log(f"    达到 {int(target * 100)}% 累计解释方差的最小 k = {hit}")
    log(f"PCA fitted: k={k} 累计解释方差={cum_total:.4f}")

    # 诊断对照：若原始 PCA 出现"PC1 独占绝大部分方差"，需区分两种成因：
    #   (a) embedding 真的近似 1 维（表征退化，需改提取方式）
    #   (b) 存在单一高方差方向把其他维掩盖（应改用相关性/标准化 PCA）
    # 故同时给出标准化（z-score）后的 PCA 曲线便于判断。
    std_table: list[dict] = []
    try:
        from sklearn.preprocessing import StandardScaler

        Z = StandardScaler().fit_transform(pool)
        pca_s = PCA(n_components=k, random_state=42)
        pca_s.fit(Z)
        cum_s = 0.0
        log("对照·标准化(z-score)后 PCA 累计解释方差：")
        for i in range(k):
            cum_s += float(pca_s.explained_variance_ratio_[i])
            std_table.append({"k": i + 1, "cumulative": cum_s})
            log(f"    k={i + 1:2d}   累计={cum_s * 100:6.2f}%")
        hit = next((t["k"] for t in std_table if t["cumulative"] >= 0.80), None)
        log(f"    标准化后达到 80% 的最小 k = {hit if hit else '未达到'}")
    except Exception as _e:  # 诊断失败不影响主流程
        log(f"[warn] 标准化 PCA 对照失败: {_e}")

    return pca, table, std_table


# ══════════════════ 主流程 ══════════════════
def collect_embeddings(model, close: np.ndarray, indices: list[int],
                       log_every: int = 100) -> np.ndarray:
    """对指定 bar 索引逐一取窗口 embedding（批量化留待性能优化）。

    长任务必须可观测：每 log_every 次打印速率与 ETA，避免"跑了 3 小时不知进度"。
    """
    out = []
    total = len(indices)
    t0 = time.time()
    for n, i in enumerate(indices):
        if i + 1 < CTX_BARS:
            continue
        win = close[i + 1 - CTX_BARS: i + 1]
        _, _, pooled = infer_window(model, win, HORIZON)
        out.append(pooled)
        if (n + 1) % log_every == 0:
            el = time.time() - t0
            rate = (n + 1) / el if el > 0 else 0.0
            eta = (total - n - 1) / rate / 60 if rate > 0 else float("nan")
            log(f"  embeddings {n + 1}/{total}  {rate:.2f} it/s  ETA {eta:.1f} min")
    return np.asarray(out, dtype=np.float64) if out else np.zeros((0, HIDDEN_SIZE))


def cmd_fit_pca(args) -> None:
    import psycopg2

    conn = psycopg2.connect(args.db_url)
    try:
        ts, close = load_klines(conn, args.symbol, "M5")
        if len(close) < CTX_BARS * 4:
            raise SystemExit(f"[fatal] M5 K 线不足（{len(close)} 根），无法拟合")
        lo = np.datetime64(args.pca_start)
        hi = np.datetime64(args.pca_end)
        ts64 = _to_naive_utc64(ts)
        idx = np.where((ts64 >= lo) & (ts64 <= hi))[0]
        idx = [int(i) for i in idx if i + 1 >= CTX_BARS]
        if not idx:
            raise SystemExit("[fatal] 训练期窗口内无可用 bar")

        # 等间隔抽样：PCA 需要 bar 级样本（数千量级），但无需全量。
        # 全量约 5 万根 × ~0.25s/次 ≈ 3.5 小时；抽样到数千根 20 分钟内可完成，
        # 而 PCA 协方差估计的稳定性在样本达数千量级后边际收益很小。
        raw_n = len(idx)
        stride = max(1, int(args.pca_stride))
        idx = idx[::stride]
        if args.pca_max_samples and len(idx) > int(args.pca_max_samples):
            idx = idx[: int(args.pca_max_samples)]
        est_min = len(idx) * 0.25 / 60
        log(f"PCA 拟合窗口 bars={raw_n} -> 抽样后 n={len(idx)} (stride={stride}"
            + (f", max={args.pca_max_samples}" if args.pca_max_samples else "")
            + f")，预计耗时 ~{est_min:.1f} 分钟（按 0.25s/次 估算）")

        model = load_model(args.model_dir, args.repo, args.normalize_inputs)
        pool = collect_embeddings(model, close, idx)
        # 逐样本归一化：不做则 PC1 独占 99.95% 方差，embedding 退化为 1 维
        pool = _normalize_embeddings(pool, args.embed_norm)
        # 缓存 embedding：推理一次约 0.3s、数千样本需 15 分钟，
        # 落盘后后续调整 k / 换标准化方式 / 换池化方式都无需重跑推理。
        if args.save_pool:
            np.savez_compressed(args.save_pool, pool=pool, indices=np.asarray(idx))
            log(f"[saved] embeddings -> {args.save_pool}  shape={pool.shape}")
        pca, evr_table, std_table = fit_pca(pool, args.pca_dim)
        meta = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "symbol": args.symbol,
            "time_frame": "M5",
            "ctx_bars": CTX_BARS,
            "horizon": HORIZON,
            "normalize_inputs": bool(args.normalize_inputs),
            "embed_norm": args.embed_norm,
            "pca_dim": int(pca.n_components_),
            "n_samples": int(pool.shape[0]),
            "explained_variance_ratio": float(np.sum(pca.explained_variance_ratio_)),
            # 各 k 的累计解释方差，供"按达到目标解释方差的最小 k"选维，而非拍脑袋
            "evr_table": evr_table,
            "evr_table_standardized": std_table,
            "sampling": {
                "bars_in_window": raw_n,
                "stride": stride,
                "max_samples": args.pca_max_samples,
                "note": "等间隔抽样；PCA 仅需 bar 级样本达数千量级，无需全量",
            },
            "window": {"start": args.pca_start, "end": args.pca_end},
            "note": "仅在训练期窗口内拟合（防未来函数）",
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.pca_out)), exist_ok=True)
        with open(args.pca_out, "wb") as f:
            pickle.dump({"pca": pca, "meta": meta}, f)
        log(f"[saved] PCA -> {args.pca_out}")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_extract(args) -> None:
    import psycopg2

    if not args.pca or not os.path.exists(args.pca):
        raise SystemExit("[fatal] 需先 --fit-pca 生成 PCA 并用 --pca 指定")
    with open(args.pca, "rb") as f:
        blob = pickle.load(f)
    pca, meta = blob["pca"], blob["meta"]
    log(f"PCA loaded: dim={pca.n_components_} evr={meta.get('explained_variance_ratio'):.4f}")
    # 一致性护栏：PCA 拟合与本次抽取必须用相同的 normalize_inputs 设置，
    # 否则 embedding 分布不同，投影后的特征语义错位（静默劣化，极难排查）。
    if "embed_norm" in meta and str(meta["embed_norm"]) != str(args.embed_norm):
        raise SystemExit(
            f"[fatal] embed_norm 不一致：PCA 拟合时为 {meta['embed_norm']}，"
            f"本次抽取为 {args.embed_norm}。embedding 归一化方式不同会导致"
            f"投影后特征语义错位（静默劣化，极难排查）。")
    if "normalize_inputs" in meta and bool(meta["normalize_inputs"]) != bool(args.normalize_inputs):
        raise SystemExit(
            f"[fatal] normalize_inputs 不一致：PCA 拟合时为 "
            f"{meta['normalize_inputs']}，本次抽取为 {args.normalize_inputs}。"
            f"请保持一致，否则 embedding 分布不同导致特征错位。")

    conn = psycopg2.connect(args.db_url)
    try:
        # 主周期 M5：落 PCA + 5 类结构化特征
        ts_m5, close_m5 = load_klines(conn, args.symbol, "M5")
        if len(close_m5) < CTX_BARS + 1:
            raise SystemExit("[fatal] M5 K 线不足")

        # 多周期：仅用于计算 mtf_resonance，不落 PCA（§16.5）
        per_tf_close: dict[str, np.ndarray] = {}
        per_tf_ts: dict[str, np.ndarray] = {}
        for tf in TF_ORDER:
            t, c = load_klines(conn, args.symbol, DB_TIMEFRAMES[tf])
            if len(c) > CTX_BARS:
                per_tf_ts[tf], per_tf_close[tf] = t, c

        lo = np.datetime64(args.start)
        hi = np.datetime64(args.end)
        ts64 = _to_naive_utc64(ts_m5)
        # 预转换各周期时间戳（UTC naive）：避免在逐 bar × 逐周期循环内反复重建数组
        # （M1 有 5 万+ 元素，循环内重建会让抽取阶段慢几个数量级）
        per_tf_ts64 = {tf: _to_naive_utc64(per_tf_ts[tf]) for tf in per_tf_ts}
        targets = [int(i) for i in np.where((ts64 >= lo) & (ts64 <= hi))[0]
                   if i + 1 >= CTX_BARS]
        if not targets:
            raise SystemExit("[fatal] 指定区间内无可用 bar")
        log(f"extract bars={len(targets)}")

        model = load_model(args.model_dir, args.repo, args.normalize_inputs)

        # 相似度检索库：只取最早目标之前的 HIST_LIB 根（严格历史）
        lib_start = max(CTX_BARS - 1, targets[0] - HIST_LIB - HORIZON)
        lib_idx = list(range(lib_start, max(lib_start + 1, targets[0] - HORIZON)))
        lib_vecs = np.zeros((0, HIDDEN_SIZE))

        rows = []
        for n, i in enumerate(targets):
            win = close_m5[i + 1 - CTX_BARS: i + 1]
            c_now = float(close_m5[i])
            hist_std = float(np.std(np.diff(win[-256:]))) + 1e-12

            p, q, pooled = infer_window(model, win, HORIZON)
            # 与 PCA 拟合时保持一致的逐样本归一化（缺省 l2）
            pooled = _normalize_embeddings(
                pooled.reshape(1, -1), args.embed_norm)[0]

            # 多周期共振：各周期取截至该时刻的窗口算 trend_cont
            conts: dict[str, float] = {}
            for tf, c_arr in per_tf_close.items():
                # per_tf_ts64 已在循环外预转换，此处直接二分查找
                pos = int(np.searchsorted(per_tf_ts64[tf], ts64[i], side="right")) - 1
                if pos + 1 < CTX_BARS:
                    continue
                w = c_arr[pos + 1 - CTX_BARS: pos + 1]
                pp, _, _ = infer_window(model, w, HORIZON)
                conts[tf] = trend_cont(float(c_arr[pos]), pp,
                                       float(np.std(np.diff(w[-256:]))) + 1e-12, HORIZON)

            pc = pca.transform(pooled.reshape(1, -1))[0]
            row = {
                "symbol": args.symbol,
                "time_frame": "M5",
                "bar_time": ts_m5[i],
                "tmf_version": args.version,
                "tmf_trend_cont": trend_cont(c_now, p, hist_std, HORIZON),
                "tmf_rev_prob": rev_prob(c_now, p),
                "tmf_vol_cycle": vol_cycle(p, hist_std),
                "tmf_mtf_resonance": mtf_resonance(conts),
                "tmf_hist_sim": hist_sim(pooled, lib_vecs),
            }
            for k in range(pca.n_components_):
                row[f"tmf_pc{k:02d}"] = float(pc[k])
            rows.append(row)

            # 检索库滚动纳入（保持 HIST_LIB 容量）
            lib_vecs = np.vstack([lib_vecs, pooled]) if lib_vecs.size else pooled.reshape(1, -1)
            if len(lib_vecs) > HIST_LIB:
                lib_vecs = lib_vecs[-HIST_LIB:]

            if (n + 1) % 50 == 0:
                log(f"  {n + 1}/{len(targets)}")

        if args.dry_run:
            log(f"[dry-run] 共 {len(rows)} 行，样例：")
            print(json.dumps(rows[-1], ensure_ascii=False, indent=2, default=str))
            return

        write_rows(conn, rows, args.create_table)
        log(f"[saved] {len(rows)} 行 -> hcm_ai.timesfm_features")
    finally:
        conn.close()


def write_rows(conn, rows: list[dict], create_table: bool) -> None:
    import psycopg2

    pc_keys = sorted([k for k in rows[0] if k.startswith("tmf_pc")])
    cols = (["symbol", "time_frame", "bar_time", "tmf_version"]
            + pc_keys + FEATURE_PREFIX + ["created_at"])

    with conn.cursor() as cur:
        if create_table:
            cur.execute("CREATE SCHEMA IF NOT EXISTS hcm_ai;")
            pc_ddl = ",\n  ".join(f"{k} DOUBLE PRECISION" for k in pc_keys)
            feat_ddl = ",\n  ".join(f"{k} DOUBLE PRECISION" for k in FEATURE_PREFIX)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS hcm_ai.timesfm_features (
                  symbol TEXT NOT NULL,
                  time_frame TEXT NOT NULL,
                  bar_time TIMESTAMPTZ NOT NULL,
                  tmf_version TEXT NOT NULL,
                  {pc_ddl},
                  {feat_ddl},
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  PRIMARY KEY (symbol, time_frame, bar_time, tmf_version)
                );
            """)
            conn.commit()
        ph = ", ".join(["%s"] * len(cols))
        collist = ", ".join(cols)
        upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in
                        ("symbol", "time_frame", "bar_time", "tmf_version"))
        sql = f"INSERT INTO hcm_ai.timesfm_features ({collist}) VALUES ({ph}) " \
              f"ON CONFLICT (symbol, time_frame, bar_time, tmf_version) DO UPDATE SET {upd}"
        for r in rows:
            r["created_at"] = datetime.now(timezone.utc)
            cur.execute(sql, [r.get(c) for c in cols])
    conn.commit()


def main() -> None:
    # 必须在使用 CTX_BARS / HORIZON（含作 argparse 默认值）之前声明 global
    global CTX_BARS, HORIZON

    ap = argparse.ArgumentParser(description="TimesFM 离线时序特征提取器（M2）")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--repo", default="google/timesfm-2.5-200m-pytorch")
    ap.add_argument("--model-dir", default=None,
                    help="本地权重目录（含 config.json + model.safetensors），离线加载，推荐")
    ap.add_argument("--version", default="tfm25_pca_v1")
    ap.add_argument("--pca-dim", type=int, default=PCA_DIM)
    ap.add_argument("--pca-stride", type=int, default=10,
                    help="PCA 拟合时按每 N 根 bar 等间隔抽样（默认 10）。"
                         "全量 5 万根约 3.5 小时，抽样到数千根约 20 分钟，"
                         "对 PCA 稳定性影响很小。设为 1 表示不抽样。")
    ap.add_argument("--pca-max-samples", type=int, default=5000,
                    help="PCA 拟合样本数上限（在 stride 抽样之后截断，默认 5000）")
    ap.add_argument("--embed-norm", choices=["none", "l2"], default="l2",
                    help="embedding 逐样本归一化方式（默认 l2）。设为 none 时 PC1 会"
                         "独占 99.95% 方差、embedding 退化为近似 1 维，仅用于对照实验。")
    ap.add_argument("--normalize-inputs", action="store_true",
                    help="让 TimesFM 对输入做实例归一化（强烈建议）。喂原始价格水平时"
                         "激活尺度随价位漂移，会使 embedding 退化为近似一维")
    ap.add_argument("--save-pool", default=None,
                    help="把抽取到的 embedding 落盘为 npz（供后续调整 k/标准化/池化"
                         "方式时复用，避免重复 15 分钟推理）")
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--ctx-bars", type=int, default=CTX_BARS)

    sub = ap.add_mutually_exclusive_group(required=True)
    sub.add_argument("--fit-pca", action="store_true")
    sub.add_argument("--extract", action="store_true")

    ap.add_argument("--pca-out", default="models/tmf_pca_v1.pkl")
    ap.add_argument("--pca", default=None, help="--extract 时使用的 PCA 文件")
    ap.add_argument("--pca-start", default=None)
    ap.add_argument("--pca-end", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--create-table", action="store_true",
                    help="建表 hcm_ai.timesfm_features（缺省不建，避免误改库）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = ap.parse_args()
    CTX_BARS, HORIZON = args.ctx_bars, args.horizon
    if CTX_BARS % PATCH_LEN != 0:
        raise SystemExit(f"[fatal] ctx-bars 须为 {PATCH_LEN} 的整数倍")

    if args.fit_pca:
        if not (args.pca_start and args.pca_end):
            raise SystemExit("[fatal] --fit-pca 需 --pca-start / --pca-end（训练期窗口）")
        cmd_fit_pca(args)
    else:
        if not (args.start and args.end):
            raise SystemExit("[fatal] --extract 需 --start / --end")
        cmd_extract(args)


if __name__ == "__main__":
    main()
