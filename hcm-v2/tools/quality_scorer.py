#!/usr/bin/env python3
"""quality_scorer.py — AI 信号质量评分 sidecar（独立进程，零下单影响）。

读 hexp 实时快照 + M5 K 线 + 外部因子综合分 → 装配特征 → LightGBM 推理 → 发布
`hcm:live:hexp:ai:{symbol}`，供前端「AI评分/总分/外部因子评分」三卡 + quality_gate 消费。

纪律红线：
  - 本进程**只读 + 只发布观测快照**，绝不改 direction/grade/lot 下单决策。
  - 模型文件缺失/加载失败 → ai_score=null，自动降级（纯 HEXP）。
  - 特征装配与训练脚本 quality_features.py 同构（train/inference 一致）。

用法:
  DB_URL=... python quality_scorer.py --symbol XAUUSD --model lgbm_quality_final.txt \
    --calib calib_final.pkl --interval 5
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from collections import deque

import numpy as np
import pandas as pd
import psycopg2
import redis

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quality_features import (  # noqa: E402
    enrich_klines, session_onehot, _as_naive_utc, h1_trend_dir_at,
)
# 【阶段2·数据契约单一真值】FEATURE_COLS 来自共享模块 _model_feature_cols，
# 训练侧(train_signal_quality.py)与推理侧必须严格一致，消除双份数据源导致的
# 列错位风险（详见 _model_feature_cols.py 注释）。
from _model_feature_cols import MODEL_FEATURE_COLS, TMF_FEATURE_COLS  # noqa: E402

import faulthandler  # noqa: E402
import logging  # noqa: E402
import logging.handlers  # noqa: E402

faulthandler.enable()  # 原生 DLL 静默死时 dump 到 stderr→launch 日志


def _setup_rolling_log():
    """滚动日志桥接：把 stdout/stderr 全部接管到 RotatingFileHandler。

    【2026-08-27 治本】此前 launcher 以追加模式 open 日志、子进程无轮转，
    长期运行涨到 6.5GB 撑爆磁盘。现改为按大小滚动（20MB/份，保留 5 份，
    上限 100MB），超出自动切割并备份，根治爆盘。
    launcher 不再自己 open 文件，由 sidecar 自管日志（见 quality_scorer_launcher.py）。

    桥接做法：用 logging 的 RotatingFileHandler 写文件，再以 StreamHandler 形式
    把 root logger 接到 sys.stdout/stderr，使所有 print(..., file=sys.stderr) 也落盘滚动。
    """
    _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "models", "quality_scorer.log")
    try:
        os.makedirs(os.path.dirname(_log_path), exist_ok=True)
        _rh = logging.handlers.RotatingFileHandler(
            _log_path, mode="a", maxBytes=20 * 1024 * 1024, backupCount=5,
            encoding="utf-8", delay=False,
        )
        _rh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        _root = logging.getLogger()
        _root.setLevel(logging.INFO)
        # 避免重复添加（模块被重复 import 时）
        if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in _root.handlers):
            _root.addHandler(_rh)
        # 桥接 print：stdout/stderr 写进 logger（INFO/WARNING 级），仍保留控制台可见性由 launcher 决定
        class _LogWriter:
            def __init__(self, lvl):
                self._lvl = lvl
                self._buf = ""

            def write(self, s):
                if not s:
                    return
                self._buf += s
                if "\n" in self._buf:
                    for line in self._buf.split("\n"):
                        if line.strip():
                            _root.log(self._lvl, line.rstrip())
                    self._buf = ""

            def flush(self):
                if self._buf.strip():
                    _root.log(self._lvl, self._buf.rstrip())
                    self._buf = ""

        sys.stdout = _LogWriter(logging.INFO)
        sys.stderr = _LogWriter(logging.WARNING)
        # faulthandler 死时 dump 仍指向原 stderr？这里仅重定向 Python 层；
        # 原生 dump 走实际 fd，无影响。
    except Exception as _e:
        # 日志桥接失败绝不能阻塞主流程：退回原生 stderr
        sys.stderr.write(f"[warn] rolling log setup failed: {_e}\n")


_setup_rolling_log()

# 【P1·推理防御】特征基准分布（训练集统计），供 PSI 漂移/离群检测。
# 由 train_signal_quality.py 导出 models/feature_baseline.json，与模型同目录加载。
FEAT_BASELINE = None


def _inference_defense(ai_score, feats, baseline, force_keep=False):
    """推理防御：单样本级漂移/离群代理 + 业务二次钳位 [20,85]。

    返回 (ai_score, drift, outlier_ratio, level)。
    - drift: 各特征 |z| 偏离 >3σ 的惩罚均值（类 PSI 单样本近似）。
    - outlier_ratio: |z| >5σ 特征占比。
    - 重度(drift>0.25 或 outlier>0.10) → ai_score=None（放弃输出，降级 HEXP-only）。
    - 轻度(drift>0.10) → ×0.6；离群 → ×0.7；最终钳 [20,85]。
    注：此 drift 为单样本代理，非全分布 PSI；真·滑动窗口 PSI 在监控层(P3)补全。
    - force_keep=True（raw_fallback 路径）时：即使判 high 也不置 None，改为强惩罚 ×0.6，
      保证 AI 评分在原始概率直出路径下「始终有值、可观测」，不因单特征漂移整体消失。
    """
    drift, outlier_ratio, level = 0.0, 0.0, "none"
    if baseline is None or not feats:
        return ai_score, drift, outlier_ratio, level
    feats_list = baseline.get("features", [])
    means = baseline.get("mean", {})
    stds = baseline.get("std", {})
    if not feats_list:
        return ai_score, drift, outlier_ratio, level
    _zs = []
    # 【2026-08-24 修复·漂移拦截豁免】event_proximity_min（距下一重大事件分钟数）
    # 分布天然随事件日历剧变，已被 PSI 计算豁免；此处漂移拦截(z 偏离)也必须豁免，
    # 否则事件日历缺失时该特征出现极大值(如 12163)→ 26σ 偏离 → level=high →
    # ai_score 被强制 None（下游降级纯 HEXP）→ AI 评分整体消失。
    # 【2026-08-24 拆炸弹·概念漂移特征豁免】close_mom_atr=(close[-1]-close[-6])/ATR 与
    # trend_aligned(收盘站上EMA20) 是已确诊的「训练分布 vs 生产行情」概念漂移元凶：
    # 训练样本盘整/低波动(close_mom_atr 均值 0.0017、std 0.075)，而黄金单边行情时该值
    # 冲到 2.34 → z=31σ → psi_like>0.25 → 整个 ai_score 被置 None（27h 纯 HEXP 降级，
    # degrade_streak=19513）。这俩是特征分布不匹配(非真实异常)，单特征漂移不应杀掉整个
    # AI 评分。故与 event_proximity_min 同机制豁免 z 偏离，保留其余特征漂移防御。
    _DRIFT_EXEMPT = set(
        (os.environ.get("PSI_EXEMPT_FEATURES",
                        "event_proximity_min,close_mom_atr,trend_aligned") or "").split(",")
    )
    for c in feats_list:
        if c in _DRIFT_EXEMPT:
            continue
        v = feats.get(c)
        if v is None:
            continue
        sd = stds.get(c) or 0.0
        if sd <= 1e-9:
            continue
        _zs.append(abs((float(v) - (means.get(c) or 0.0)) / sd))
    if not _zs:
        return ai_score, drift, outlier_ratio, level
    _zs_arr = np.asarray(_zs, dtype=float)
    psi_like = float(np.mean(np.maximum(0.0, _zs_arr - 3.0)))  # 普遍偏移（类 PSI 代理）
    max_dev = float(np.max(_zs_arr))                            # 单点最大偏离（离群）
    # 【2026-08-24 修复·漂移拦截过度敏感】原 max_dev>5.0 即判 high → 单特征偶发 5σ
    # 偏离（如 close_mom_atr 极端 K 线）即杀掉整个 ai_score（None）→ AI 评分频繁消失。
    # 改为：仅当普遍存在偏移(psi_like>0.25)才 high 拦截；单点离群(max_dev>5)只判 low
    # （×0.7 惩罚），不再整体丢弃 AI 分。
    if psi_like > 0.25:
        level = "high"
    elif psi_like > 0.10 or max_dev > 3.0:
        level = "low"
    if ai_score is not None:
        if level == "high" and not force_keep:
            ai_score = None
        else:
            if psi_like > 0.10:
                ai_score *= 0.6
            if max_dev > 3.0:
                ai_score *= 0.7
            ai_score = max(20.0, min(85.0, float(ai_score)))
    return ai_score, psi_like, max_dev, level


def _feature_health(feats, baseline) -> dict:
    """特征健康统计（P1-O4 2026-08-22）：缺失率 / 恒值率 / 异常占比。

    与全分布 PSI 不同，这是逐样本的实时健康代理：
      - feat_missing_ratio: 特征值为 None/缺列的占比（推理装配不全）。
      - feat_constant_ratio: 恒 0.0 占位（训练-推理分布偏移盲区）占比。
      - feat_outlier_ratio : |z|>5σ 的离群特征占比（基于 feature_baseline）。
    供 _persist 落库观测，监控层可据此识别"装配退化/恒值失真/离群突增"。
    """
    missing = constant = 0
    n = len(feats)
    if n == 0:
        return {"feat_missing_ratio": None, "feat_constant_ratio": None,
                "feat_outlier_ratio": None}
    for c, v in feats.items():
        if v is None:
            missing += 1
        else:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                missing += 1
                continue
            if abs(fv) < 1e-12:
                constant += 1
    outlier = 0
    if baseline and baseline.get("mean") and baseline.get("std"):
        means, stds = baseline["mean"], baseline["std"]
        for c, v in feats.items():
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            sd = stds.get(c) or 0.0
            if sd <= 1e-9:
                continue
            if abs((fv - (means.get(c) or 0.0)) / sd) > 5.0:
                outlier += 1
    return {
        "feat_missing_ratio": round(missing / n, 4),
        "feat_constant_ratio": round(constant / n, 4),
        "feat_outlier_ratio": round(outlier / n, 4),
    }


# 【P2-O6 2026-08-22】sidecar 连续降级告警阈值：连续 N 轮 ai_score=None（模型在场
# 但推理退化/漂移重度拦截）即告警。默认 30 轮（interval=5s → ~2.5 分钟无有效分）。
DEGRADE_ALERT_ROUNDS = 30
_degrade_streak = 0
_last_degrade_alert_ts = 0.0


def _degrade_alert(now: float):
    """连续降级告警（P2-O6）：连续 N 轮无有效 AI 分时打一次 stderr 告警，并防刷屏。
    仅日志告警（生产暂不接 webhook，避免误报）；监控层可据 heartbeat/日志识别。
    返回 (streak, alerted) 供观测。
    """
    global _degrade_streak, _last_degrade_alert_ts
    _degrade_streak += 1
    alerted = False
    if _degrade_streak == DEGRADE_ALERT_ROUNDS:
        # 首达阈值：告警一次
        print(f"[alert] sidecar 连续 {_degrade_streak} 轮无有效 AI 分（模型在场但推理退化/"
              f"漂移重度拦截）→ 下游已降级纯 HEXP，请检查 quality_scorer 日志。",
              file=sys.stderr, flush=True)
        alerted = True
    elif _degrade_streak > DEGRADE_ALERT_ROUNDS and (now - _last_degrade_alert_ts) >= 300.0:
        # 持续降级：每 5 分钟复告一次，避免刷屏
        print(f"[alert] sidecar 持续降级已 {_degrade_streak} 轮无有效 AI 分（纯 HEXP 运行中）",
              file=sys.stderr, flush=True)
        _last_degrade_alert_ts = now
        alerted = True
    return _degrade_streak, alerted


def _degrade_reset():
    """恢复有效 AI 分后清零连续降级计数。"""
    global _degrade_streak
    _degrade_streak = 0


# 【加固·2026-08-18 回归修复】安全浮点转换：值可转 float 则转，否则回退 default。
# 2026-08-17 重写 score_one 时漏写本函数（build_features 内 ds_fake_prob/ds_sl_coeff/
# ds_continuity 三处调用 _num(...)）→ 每轮 NameError 被主循环 except 吞成
# "scoring loop error: name '_num' is not defined" → sidecar 永不发布 AI 快照（面板离线）。
# 定义于此（import 段之后、全局常量之前），供 build_features / score_one 全模块复用。
def _num(v, default=0.0):
    """安全数值转换：v 能转 float 就转 float，否则回退 default（None/非数/空串）。"""
    try:
        if v is None:
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


# 【TimesFM 特征 2026-08-30】按当前 M5 bar 从 hcm_ai.timesfm_features 读取离线时序特征，
# 供 build_features 注入。缺失(调度滞后/未覆盖)→ {}，build_features 填 0.0 降级，
# 与训练侧缺省严格一致，保证训练-推理同分布。60s 缓存按 bar_time 避免每 5s 打 PG。
_TMF_CACHE = {"bar_time": None, "vals": {}, "at": 0.0}
_TMF_VERSION = "tfm25_pca_v1_sig"


def _load_tmf_for_bar(conn, symbol, bar_time, ttl: float = 60.0) -> dict:
    """读当前 M5 bar 的 TimesFM 离线特征；返回 13 列 dict，缺失→{}。"""
    if bar_time is None:
        return {}
    bt = _as_naive_utc(bar_time)
    if bt is None:
        return {}
    _now = time.time()
    if _TMF_CACHE["bar_time"] == bt and (_now - _TMF_CACHE["at"]) < ttl:
        return _TMF_CACHE["vals"]
    vals: dict = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tmf_pc00,tmf_pc01,tmf_pc02,tmf_pc03,tmf_pc04,tmf_pc05,tmf_pc06,tmf_pc07,"
                "tmf_trend_cont,tmf_rev_prob,tmf_vol_cycle,tmf_mtf_resonance,tmf_hist_sim "
                "FROM hcm_ai.timesfm_features "
                "WHERE symbol=%s AND time_frame='M5' AND tmf_version=%s AND bar_time=%s",
                (symbol, _TMF_VERSION, bt),
            )
            r = cur.fetchone()
            if r:
                for c, v in zip(TMF_FEATURE_COLS, r):
                    vals[c] = float(v) if v is not None else 0.0
    except Exception:
        pass
    _TMF_CACHE["bar_time"] = bt
    _TMF_CACHE["vals"] = vals
    _TMF_CACHE["at"] = _now
    return vals


# 用 127.0.0.1 而非 localhost：Windows 解析 localhost 优先命中 ::1(IPv6)，会被
# wslrelay.exe(WSL 端口转发)劫持 [::1]:6379/5432 → 连不上 docker 生产 Redis/PG。
# 强制 IPv4 直连生产(docker 监听 0.0.0.0) → 2026-08-17 根治"AI评分未显示"根因。
DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@127.0.0.1:5432/hcm_v2"
REDIS_URL_DEFAULT = "redis://127.0.0.1:6379"

# 退化校准器与原始概率的混合权重（校准票占比）；由 ai.lm.calib_blend_w 覆盖。
# 0.0=完全用 raw 概率、1.0=完全信校准（即恒定常数）。默认 0.5 兼顾排序与分辨率。
CALIB_BLEND_W = 0.5
# 校准器唯一输出档位数低于此值即判定退化（由 ai.lm.calib_min_levels 覆盖）
CALIB_MIN_LEVELS = 4

# 与 quality_features.py / 训练一致的固定特征顺序（模型训练时所用列）
# 【阶段2·数据契约单一真值】FEATURE_COLS 现来自 _model_feature_cols.MODEL_FEATURE_COLS。
# 历史定义（保留作审计参考）：
#   adx_14/rsi_14/macd/atr_14/h1_adx/h1_trend_strength/plus_di/minus_di/er/bbw/bbw_pct/
#   hurst/mm/ema20_dist_atr/body_ratio/pullback_depth/atr_pct/spread_num/spread_atr/
#   session_asia/session_eu/session_us/event_proximity_min/macro_risk_score/sentiment_risk_score/
#   【DeepSeek 训练特征增强 2026-08-17】ds_fake_prob/ds_sl_coeff/ds_continuity（缺省0.0，旧模型忽略）
#   【特征增强 2026-08-17】di_ratio/di_net/spread_atr_log/close_mom_atr/trend_aligned
#   【多任务状态头 2026-08-17】donchian_q/dev_z_ema20/dev_z_ema60/dev_z_ema200/macd_slope3/body_wick_ratio
#   【极值反转特征 2026-08-18】extreme_reversal
# 以上全部由 _model_feature_cols.MODEL_FEATURE_COLS 权威定义，训练侧 reindex 对齐。
FEATURE_COLS = MODEL_FEATURE_COLS


def load_config(conn, redis_cli=None) -> dict:
    """读 ai.* 配置：PG 为唯一真值，Redis hcm:config:v2 仅作缺失补位。

    修复(2026-08-14)：sidecar 此前只读 PG current_value，但 ai.mode/ai.enabled 等
    经 config_provider.set 双写后 Redis 覆盖值才是运行时真值 → 导致 sidecar 快照
    展示 mode=decoupled 而 scheduler 实际以 coupled 运行，误导前端。故引入 Redis 读取。

    修正(2026-08-28 P1-8)：上一段修复把 Redis 设为**无条件覆盖** PG，与铁律 5.2
    「PG current_value 为唯一真值」冲突 —— Redis 侧任何残留/手工改写的脏值都会
    盖掉 PG。现改为 PG 为准、Redis 仅填补 PG 缺失的键：config_provider 双写正常时
    两端一致，行为与修复(2026-08-14)完全相同；同时杜绝 Redis 脏值反向污染。
    """
    cfg = {}
    redis_override = {}
    if redis_cli is not None:
        try:
            raw = redis_cli.hgetall("hcm:config:v2")
            if raw:
                for k, v in raw.items():
                    if isinstance(k, bytes):
                        k = k.decode("utf-8", "ignore")
                    if isinstance(v, bytes):
                        v = v.decode("utf-8", "ignore")
                    if str(k).startswith("ai."):
                        redis_override[k] = v
        except Exception:
            pass
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config_key, current_value FROM hcm_config.metadata WHERE config_key LIKE 'ai.%'"
        )
        for k, v in cur.fetchall():
            cfg[k] = v
    # 【2026-08-28 P1-8】真值优先级修正：铁律 5.2「配置是否真正生效以 PG
    # current_value 为唯一真值，Redis 仅 L2 缓存」。
    # 原 `cfg.update(redis_override)` 让 Redis 无条件覆盖 PG —— 一旦某条路径
    # 单端直写 Redis（历史上的违规写点 / 运维手工 HSET），脏值就会盖掉 PG 正确值
    # 并被本 sidecar 采用，产出的训练样本配置快照与真源不一致。
    # 改为「PG 为准、Redis 仅补缺」：config_provider 双写正常时两端一致，**行为不变**；
    # 仅当 PG 无该键时才取 Redis 值（兼容历史上只存在于 Redis 的键，不丢配置）。
    for _k, _v in redis_override.items():
        cfg.setdefault(_k, _v)
    return cfg


def _bool(cfg, key, default=False):
    v = cfg.get(key)
    if v is None:
        return default
    return str(v).lower() in ("true", "1", "yes", "on")


def _cfg_float(cfg, key, default: float) -> float:
    """配置读数值（配置中心值为字符串，解析失败回退默认，绝不抛错）。"""
    v = cfg.get(key)
    if v is None or str(v).strip() == "":
        return float(default)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return float(default)


def _calib_is_degenerate(iso, min_levels: int | None = None) -> bool:
    """判断等温校准器是否退化成粗阶梯（小样本过拟合的典型症状）。

    【B5 真凶 2026-08-15】calib_final.pkl 由仅 403 条样本(测试集≈80)拟合，
    y_thresholds_ 只有 {0.0, 0.4, 1.0} 三级 → raw prob ∈[0.256,0.615] 全被压成
    常数 0.40 → ai_score 恒 40.0，LightGBM 的 24 个有效特征增益全部被抹平。
    此处检测唯一输出档位数，过少即视为退化（改用平滑混合，见 score_one）。

    【2026-08-29 扩展·阶段 1 校准裁决】原实现**只认** sklearn IsotonicRegression
    的 ``y_thresholds_``；而方向头 / 买点头用的是 ``calib_np.NumpyCalibrator``
    （纯 numpy、无该属性，``calib_np.py:7`` 已注明"无 y_thresholds_ → 返回
    False"）。后果：**这两头的校准器退化检测恒为 False、完全失效** ——
    一旦小样本过拟合退化成常数，会悄悄抹平模型增益而无人察觉。
    现补充 NumpyCalibrator 分支：以其 ``y_fit`` 唯一值数量作为档位数判定，
    使「退化 → 混合 raw 恢复分辨率」的自愈能力覆盖到三头。
    """
    try:
        _min = CALIB_MIN_LEVELS if min_levels is None else int(min_levels)
        # 分支 1（原逻辑）：sklearn IsotonicRegression
        yt = getattr(iso, "y_thresholds_", None)
        if yt is not None:
            levels = {round(float(v), 4) for v in yt}
            return len(levels) < _min
        # 分支 2（新增）：calib_np.NumpyCalibrator —— 用 y_fit 唯一值数量
        yf = getattr(iso, "y_fit", None)
        if yf is not None:
            levels = {round(float(v), 4) for v in yf}
            return len(levels) < _min
        return False
    except Exception:
        return False


def _latest_version_path(dirname: str, basename_fmt: str) -> str | None:
    """在同目录按版本号发现最新模型文件（替代硬编码 vN 回退）。

    basename_fmt 形如 ``"lgbm_direction_v{}.txt"``：扫描同目录同名模式文件，
    返回版本号**最大者**的完整路径；无匹配返回 None。

    【2026-08-29 硬编码修正】原回退分支写死 ``lgbm_direction_v54.txt`` /
    ``lgbm_entry_v54.txt``，导致 ``auto_retrain`` 重训出 v55+ 新模型后，
    方向头与买点头仍加载 v54 旧版 —— 重训对这两头完全失效。
    改为版本发现后，重训产出新版本即可被自动加载，实现无干预版本演进。

    配置键（``ai.lm.dir_path`` / ``ai.lm.entry_path`` 等）显式指定时优先，
    本函数**仅作缺省回退**，不覆盖人工配置。
    """
    import re as _re
    import glob as _glob

    if not dirname:
        return None
    prefix, suffix = basename_fmt.split("{}", 1)
    pat = _re.compile(_re.escape(prefix) + r"(\d+)" + _re.escape(suffix) + r"$")
    best_n, best_path = -1, None
    try:
        for p in _glob.glob(os.path.join(dirname, prefix + "*" + suffix)):
            m = pat.search(os.path.basename(p))
            if m:
                n = int(m.group(1))
                if n > best_n:
                    best_n, best_path = n, p
    except Exception:
        return None
    return best_path


# 【2026-08-28 校准器修复】纯 numpy 校准器（无 sklearn 依赖），与 calib_v53.pkl 共用。
# 独立模块保证 fit 脚本与生产侧 pickle 限定名一致(calib_np.NumpyCalibrator)。
from calib_np import NumpyCalibrator  # noqa: E402


def load_model(model_path, calib_path, state_path=None, dir_path=None, dir_calib_path=None,
              entry_path=None, entry_calib_path=None):
    import lightgbm as lgb
    m = lgb.Booster(model_file=model_path)
    iso = None
    if calib_path and os.path.exists(calib_path):
        with open(calib_path, "rb") as f:
            iso = pickle.load(f)
        if iso is not None and _calib_is_degenerate(iso):
            print(
                "[warn] isotonic calibrator degenerate (too few levels, small-sample "
                "overfit) → blending with raw probability to restore resolution",
                file=sys.stderr,
            )
    state_model = None
    state_classes = None
    if state_path and os.path.exists(state_path):
        try:
            with open(state_path, "rb") as f:
                state_model = pickle.load(f)
            state_classes = [str(c) for c in state_model.classes_]
            print(f"[info] state head loaded: {state_path} classes={state_classes}",
                  file=sys.stderr)
        except Exception as exc:
            print(f"[warn] state head load failed: {exc}", file=sys.stderr)
    # 【阶段 1·方向头】独立于质量头的多类方向模型（3 类：-1 空 / 0 观望 / +1 多）。
    # 校准器为 calib_np.NumpyCalibrator 字典{dict{-1:..,0:..,1:..}}，生产无 sklearn 可加载。
    # 灰度：dir_path/None 由调用方（_reload_cfg 的 ai.lm.dir_enabled）控制是否加载。
    dir_model = None
    dir_calibs = None
    if dir_path and os.path.exists(dir_path):
        try:
            dir_model = lgb.Booster(model_file=dir_path)
            if dir_calib_path and os.path.exists(dir_calib_path):
                with open(dir_calib_path, "rb") as f:
                    dir_calibs = pickle.load(f)
                # 【阶段 1·校准裁决】逐类别检测退化（NumpyCalibrator 字典）
                if isinstance(dir_calibs, dict):
                    _deg = sorted(
                        str(c) for c, cal in dir_calibs.items()
                        if cal is not None and _calib_is_degenerate(cal)
                    )
                    if _deg:
                        print(f"[warn] direction calibrator degenerate for classes {_deg} "
                              f"(too few levels, small-sample overfit) → blending with "
                              f"raw probability to restore resolution", file=sys.stderr)
            print(f"[info] direction head loaded: {dir_path} calib={dir_calib_path}",
                  file=sys.stderr)
        except Exception as exc:
            print(f"[warn] direction head load failed: {exc}", file=sys.stderr)
    # 【阶段 2·买点头】独立二分类模型（好买点概率），校准器为 calib_np.NumpyCalibrator
    # （纯 numpy，生产可加载）。灰度：entry_path/None 由调用方（ai.lm.entry_enabled）控制。
    entry_model = None
    entry_calib = None
    if entry_path and os.path.exists(entry_path):
        try:
            entry_model = lgb.Booster(model_file=entry_path)
            if entry_calib_path and os.path.exists(entry_calib_path):
                with open(entry_calib_path, "rb") as f:
                    entry_calib = pickle.load(f)
                # 【阶段 1·校准裁决】买点头校准器退化检测
                if entry_calib is not None and _calib_is_degenerate(entry_calib):
                    print("[warn] entry calibrator degenerate (too few levels, "
                          "small-sample overfit) → blending with raw probability "
                          "to restore resolution", file=sys.stderr)
            print(f"[info] entry head loaded: {entry_path} calib={entry_calib_path}",
                  file=sys.stderr)
        except Exception as exc:
            print(f"[warn] entry head load failed: {exc}", file=sys.stderr)
    return m, iso, state_model, state_classes, dir_model, dir_calibs, entry_model, entry_calib


def build_features(snapshot: dict, kl: pd.DataFrame,
                   h1_feats: dict | None = None, env_feats: dict | None = None,
                   align_m5: bool = False, audit: bool = False,
                   ds_out: dict | None = None,
                   tmf_out: dict | None = None) -> dict:
    """从 hexp 快照 + 已 enrich 的 M5 K 线装配单条特征行（与训练同构）。

    【B5 修复 2026-08-14】此前 macd/h1_adx/h1_trend_strength/event_proximity_min/
    macro_risk_score/sentiment_risk_score 恒 None→0.0（25 维特征 6 维恒 0），
    模型输入几乎不随行情变 → ai_score 恒定 40.0 形同摆设。现全部接真实数据源：
      - macd       : M5 收盘 EMA12-26 柱状(hist)，与训练侧 indicator_values.macd 同语义
      - h1_adx / h1_trend_strength : PG H1 K线重算（_h1_features）
      - event/macro/sentiment      : PG 事件日历+快照（_env_features，与训练 load_env 同源）
    """
    row: dict = {}
    # 快照侧的 hexp 元数据（当前训练版本未用这些，占位以保持列对齐）
    row["adx_14"] = snapshot.get("factor_raws", {}).get("adx")
    row["rsi_14"] = snapshot.get("factor_raws", {}).get("rsi")
    row["macd"] = None
    row["atr_14"] = snapshot.get("atr")
    # 【2026-09-02】verdict 多周期共识分：推理侧直接用 hexp 实时 snap["verdict"]（同源于引擎，最准）。
    # 与训练侧 _compute_verdict 重算同分布；键名兼容 live 快照(resonance_verdict)与落库(_hexp.verdict)。
    _verdict = snapshot.get("verdict")
    if _verdict is None:
        _verdict = snapshot.get("resonance_verdict")
    row["verdict"] = float(_verdict) if _verdict is not None else 0.0
    h1f = h1_feats or {}
    if align_m5 and kl is not None and not kl.empty:
        # 周期对齐(2026-08-15)：h1_adx/h1_trend_strength 改由 M5 K线(与 ai_score 其余
        # 特征同源、与 hp_score 取值周期一致)重算 ADX/趋势强度，使整条 25 维特征向量
        # 同周期(M5)一口径 —— LightGBM 输出才有与 hp_score 对齐的校准价值。
        # 【2026-09-02】h1f 先浅拷贝再 update：保留 h1_trend_dir（来自真实 H1 K线，
        # 方向头感知 H1 主趋势的关键输入，不可被 M5 近似覆盖）。
        try:
            pdi = kl["plus_di"].astype(float)
            mdi = kl["minus_di"].astype(float)
            dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0, float("nan"))
            _m5 = dx.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
            if not pd.isna(_m5):
                h1f = dict(h1f)
                h1f["h1_adx"] = float(_m5)
                h1f["h1_trend_strength"] = float(_m5) / 100.0
        except Exception:
            pass
    row["h1_adx"] = h1f.get("h1_adx")
    row["h1_trend_strength"] = h1f.get("h1_trend_strength")
    # 【2026-09-02 h1_trend_dir 特征注入】H1 主趋势方向(±1/0)，与训练侧同源(quality_features
    # h1_trend_dir_at)；方向头靠它区分"H1 UP 回调(标签压 FLAT)" vs "震荡顶反转(保留 SELL)"。
    row["h1_trend_dir"] = h1f.get("h1_trend_dir")
    # 【2026-09-01 bar 对齐修复·推理侧】只用已收盘 bar 构造特征：kl 末根是桥写入的
    # "形成中 bar"(未收盘)，其 mm/close_mom_atr/body_ratio 等每 5s 随 tick 变化；
    # 训练侧(quality_features)对应的是已收盘 bar → 用未收盘 bar 推理会造成训练-推理
    # 分布不一致，是实盘方向判反/追顶的根因之一。此处过滤后统一取最后一根已收盘 bar。
    _klc = kl
    try:
        if kl is not None and not kl.empty:
            _now = pd.Timestamp.now(tz="UTC")
            _closed = kl[kl["open_time"] + pd.Timedelta(seconds=300) <= _now]
            if not _closed.empty:
                _klc = _closed
    except Exception:
        _klc = kl
    if kl is not None and not kl.empty:
        bar = _klc.iloc[-1]
        for c in ["plus_di", "minus_di", "er", "bbw", "bbw_pct", "hurst", "mm",
                  "ema20_dist_atr", "body_ratio", "pullback_depth", "atr_pct",
                  "spread_num", "spread_atr",
                  # 多任务状态头原始结构因子（enrich_klines 已 shift(1) 写入，含历史值）
                  "donchian_q", "dev_z_ema20", "dev_z_ema60", "dev_z_ema200",
                  "macd_slope3", "body_wick_ratio", "extreme_reversal"]:
            row[c] = bar.get(c)
        # MACD 柱状（EMA12-26 差值的 9 周期信号线之差）
        try:
            close = _klc["close"].astype(float)
            macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
            sig9 = macd_line.ewm(span=9, adjust=False).mean()
            row["macd"] = float((macd_line - sig9).iloc[-1])
        except Exception:
            row["macd"] = None
    row.update(session_onehot(pd.Timestamp.now("UTC")))
    # 【特征增强 2026-08-17】让模型对行情尺度漂移鲁棒 + 捕捉趋势/动量（针对分布偏移：
    # plus_di 训练 max16.3 vs 实盘30+、spread_atr 训练 max1.83 vs 实盘5+，绝对值特征越界致评分失真）。
    # 全部用比值/归一化/对数/ATR 归一，消除绝对值漂移盲区。
    if kl is not None and not kl.empty:
        try:
            _bar = _klc.iloc[-1]
            _atr = float(_bar.get("atr") or 0.0) or 1e-9
            _pdi = float(_bar.get("plus_di") or 0.0)
            _mdi = float(_bar.get("minus_di") or 0.0)
            # di_ratio: 方向强弱比(尺度无关)；di_net: 归一化 DI 差 ∈[-1,1]
            row["di_ratio"] = _pdi / (_mdi + 1e-6)
            row["di_net"] = (_pdi - _mdi) / (_pdi + _mdi + 1e-6)
            # spread_atr_log: 压缩点差极值
            row["spread_atr_log"] = float(np.log1p(max(0.0, _bar.get("spread_atr") or 0.0)))
            # close_mom_atr: 近 N 根收盘动量 / ATR(尺规归一, 反映实时动能)
            _cl = _klc["close"].astype(float)
            _mom = (float(_cl.iloc[-1]) - float(_cl.iloc[-6])) if len(_cl) >= 6 else 0.0
            row["close_mom_atr"] = _mom / _atr
            # trend_aligned: 收盘是否站上 EMA20(顺趋势)
            _ema20 = _cl.ewm(span=20, adjust=False).mean().iloc[-1]
            row["trend_aligned"] = 1.0 if float(_cl.iloc[-1]) > float(_ema20) else 0.0
        except (TypeError, ValueError):
            row["di_ratio"] = None; row["di_net"] = None; row["spread_atr_log"] = None
            row["close_mom_atr"] = None; row["trend_aligned"] = None
        # 【阶段 2·方案 A·质量头治本】入场质量特征（推理侧从 hexp 快照取，训练侧从 signals
        # 表取，口径一致）。质量标签="先触 ±R 哪边"，R = atr * ai_sl_mult（build_labels.label_one
        # 同定义）。故入场质量直接由**无量纲**信号表达，避免 entry/sl 与 atr 单位不一致陷阱：
        #   r_dist_atr     : ai_sl_mult（实际 SL 倍数 vs 标签默认 2.0，越大=止损越宽=越易达标）
        #   sl_mult_used   : 同上（冗余保留对齐训练侧）
        #   entry_atr_ratio: (entry - 近期close均值)/atr（入场相对价位的 ATR 归一 z，量纲无关）
        _atr2 = float(snapshot.get("atr") or 0.0) or 1e-9
        _mult = snapshot.get("ai_sl_mult")
        _entry = snapshot.get("entry_price")
        try:
            _mult = float(_mult) if _mult is not None else 2.0
            # entry 相对近期收盘的 ATR 归一（推理侧 kl 可得；缺则 0.0）
            _entry_z = 0.0
            if _entry is not None and kl is not None and not kl.empty:
                _entry_f = float(_entry)
                _close_mean = float(kl["close"].astype(float).iloc[-20:].mean()) if len(kl) >= 20 else float(kl["close"].astype(float).iloc[-1])
                _entry_z = (_entry_f - _close_mean) / (_atr2 + 1e-9)
            row["r_dist_atr"] = _mult
            row["sl_mult_used"] = _mult
            row["entry_atr_ratio"] = _entry_z
        except (TypeError, ValueError):
            row["r_dist_atr"] = 0.0
            row["sl_mult_used"] = 2.0
            row["entry_atr_ratio"] = 0.0
    envf = env_feats or {}
    row["event_proximity_min"] = envf.get("event_proximity_min")
    row["macro_risk_score"] = envf.get("macro_risk_score")
    row["sentiment_risk_score"] = envf.get("sentiment_risk_score")
    row["liquidity"] = envf.get("liquidity")   # 【流动性特征 2026-08-30】外部流动性因子(0~1)
    # 【DeepSeek 训练特征增强 2026-08-17】把 DeepSeek 异步票固化为特征列。
    # ds_out 由调用方(主循环)传入 Redis ai:ds:out:{sym} 当前票；缺失→三项全 0.0，
    # 与训练侧缺省(无 DeepSeek 标注历史)严格一致，保证训练-推理同分布。
    # 语义：ds_fake_prob∈[0,1] 信号真假概率；ds_sl_coeff∈[0.8,1.5] 止损系数建议；
    #      ds_continuity∈[0,100] 行情连续性(高=趋势延续,低=震荡反转)。
    _ds = ds_out or {}
    row["ds_fake_prob"] = _num(_ds.get("fake_prob"), 0.0) if _ds.get("fake_prob") is not None else 0.0
    row["ds_sl_coeff"] = _num(_ds.get("ai_sl_coeff"), 0.0) if _ds.get("ai_sl_coeff") is not None else 0.0
    row["ds_continuity"] = _num(_ds.get("continuity_score"), 0.0) if _ds.get("continuity_score") is not None else 0.0
    # 【TimesFM 特征 2026-08-30】注入 tmf_* 13 列：推理侧主循环按当前 M5 bar 查
    # hcm_ai.timesfm_features，缺失→全 0.0，与训练侧缺省严格一致(保证训练-推理同分布)。
    _tmf = tmf_out or {}
    for c in TMF_FEATURE_COLS:
        row[c] = _tmf.get(c, 0.0)
    # 所有特征缺则补 0.0，避免 LightGBM 因 None/object dtype 抛错（被 score_one 顶层 except 吞）
    feats = {c: (row.get(c) if row.get(c) is not None else 0.0) for c in FEATURE_COLS}
    # 【C·特征口径对齐审计】开启 ai.lm.feature_audit 时打印 FEATURE_COLS 顺序与当前样本，
    # 供与训练脚本(train_signal_quality.py --audit)输出 diff，确认推理与训练同构
    # （列顺序/数值范围一致，杜绝训练-推理分布偏移这一 LightGBM 部署头号风险）。
    if audit:
        try:
            _cols = ",".join(FEATURE_COLS)
            _vals = " ".join(f"{c}={feats[c]:.4f}" for c in FEATURE_COLS)
            print(f"[audit] build_features cols=({_cols}) sample=({_vals})", file=sys.stderr)
        except Exception:
            pass
    return feats


# ── 60s 缓存的慢特征（H1 / 事件 / 宏观情绪，每 5s 主循环不必每轮重查 PG）──
_SLOW_CACHE: dict = {}


def _cached_slow(key: str, ttl_sec: float, fn):
    now = time.time()
    ent = _SLOW_CACHE.get(key)
    if ent and (now - ent[0]) < ttl_sec:
        return ent[1]
    try:
        val = fn()
    except Exception as exc:
        print(f"[warn] slow feature {key} failed: {exc}", file=sys.stderr)
        val = ent[1] if ent else {}
    _SLOW_CACHE[key] = (now, val)
    return val


def _h1_features(conn, symbol: str) -> dict:
    """PG H1 K线 → h1_adx / h1_trend_strength / h1_trend_dir（Wilder DX 14 平滑 + 趋势态）。

    【2026-09-02】h1_trend_dir：H1 主趋势方向(±1/0)，与训练侧 quality_features.h1_trend_dir_at
    同函数同口径（已收盘棒、无泄露）；方向头靠它感知 H1 主趋势，否则趋势对齐标签无法被模型学习。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_time, open, high, low, close, spread FROM hcm_market.klines "
            "WHERE symbol=%s AND time_frame='H1' ORDER BY open_time DESC LIMIT 80",
            (symbol,),
        )
        rows = cur.fetchall()
    if not rows:
        return {}
    kdf = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "spread"])
    kdf["open_time"] = pd.to_datetime(kdf["open_time"], utc=True)
    kdf = kdf.sort_values("open_time").reset_index(drop=True)
    kl = enrich_klines(kdf)
    if kl.empty:
        return {}
    pdi = kl["plus_di"].astype(float)
    mdi = kl["minus_di"].astype(float)
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0, float("nan"))
    adx = dx.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    _out = {"h1_trend_dir": h1_trend_dir_at(kl, pd.Timestamp.now(tz="UTC"))}
    if not pd.isna(adx):
        _out["h1_adx"] = float(adx)
        _out["h1_trend_strength"] = float(adx) / 100.0
    return _out


def _env_features(conn) -> dict:
    """事件临近度(分钟) + 最新宏观/情绪风险分（与训练 load_env 同表同源）。"""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min(abs(extract(epoch from (event_date - now()))) / 60.0) "
            "FROM hcm_market.event_calendar WHERE is_active AND importance >= 2"
        )
        v = cur.fetchone()
        if v and v[0] is not None:
            out["event_proximity_min"] = float(v[0])
        # 外部因子按 category 分列存储；XAUUSD 信号对应 metals 类别（含真实 K线派生变分），
        # 取该类别最新快照，避免被恒值的 forex/crypto/liquidity 类别淹没为零方差。
        cur.execute(
            "SELECT macro_risk_score FROM hcm_market.macro_snapshots "
            "WHERE category='metals' ORDER BY snapshot_time DESC LIMIT 1"
        )
        v = cur.fetchone()
        if v and v[0] is not None:
            out["macro_risk_score"] = float(v[0])
        cur.execute(
            "SELECT sentiment_risk_score FROM hcm_market.sentiment_snapshots "
            "WHERE category='metals' ORDER BY snapshot_time DESC LIMIT 1"
        )
        v = cur.fetchone()
        if v and v[0] is not None:
            out["sentiment_risk_score"] = float(v[0])
        # 【流动性特征 2026-08-30】读最新 liquidity(0~1)；表缺失时优雅降级(不阻断推理)。
        try:
            cur.execute(
                "SELECT liquidity_score FROM hcm_market.liquidity_snapshots "
                "WHERE category='metals' ORDER BY snapshot_time DESC LIMIT 1"
            )
            v = cur.fetchone()
            if v and v[0] is not None:
                out["liquidity"] = float(v[0])
        except Exception:
            pass
    return out


# ── 2026-09-01 dir_head 防抖参数与状态（模块级，sidecar 单 symbol 场景安全）──
# DIR_EMA_ALPHA：概率 EMA 权重，0.5 → 半衰约 2 采样（@5s 刷新 ≈ 10s）
# DIR_MARGIN   ：间隙带，top1-top2 低于此视为"不确定"，维持前值不跳
# _DIR_STATE   ：跨采样状态（proba=EMA 后向量 / dir=当前防抖方向 / raw=原始 argmax）
DIR_EMA_ALPHA = 0.5
DIR_MARGIN = 0.15
_DIR_STATE: dict = {"proba": None, "dir": None, "raw": None}


def score_one(model, iso, feats: dict, state_model=None, state_classes=None,
              dir_model=None, dir_calibs=None, entry_model=None, entry_calib=None,
              _probe: bool = False):
    """用模型的 feature_name() 对齐列（train/inference 同构，禁硬编码列序）。

    【路线①·状态粒度优化 2026-08-18】去离散 state one-hot，质量头纯靠连续结构
    因子(donchian_q/dev_z_ema20/60/200/macd_slope3/body_wick_ratio/extreme_reversal
    等，均在 FEATURE_COLS 且训练侧 prepare() 已保留)分化 —— 树模型自学习趋势强弱
    边界，TREND 类内随 dz/macd_slope 连续分化(不再"见 TREND 直接给 1.0")。
    状态头(state_model)仅作诊断：预测 predicted_state 供主循环在线分位缓冲观测，
    【不再拼回特征向量】(原 state_{sc} one-hot 拼接是 TREND 无区分度的根因)。
    【阶段 1·方向头】dir_model 加载时额外推断 ai_direction(BUY/SELL/HOLD)+ ai_dir_prob。
    方向头仅作"同向增强/反向否决"输入（永不独立开方向，红线），灰度默认 None。
    """
    try:
        names = model.feature_name()
        row = {k: feats.get(k) for k in names}
        # 状态头仅作诊断(预测 predicted_state)，不拼回质量头特征
        predicted_state = None
        if state_model is not None and state_classes:
            try:
                _snames = (state_model.feature_name() if hasattr(state_model, "feature_name")
                           else state_model.booster_.feature_name())
                sfeats = {k: feats.get(k) for k in _snames}
                sdf = pd.DataFrame([sfeats])
                for c in sdf.columns:
                    sdf[c] = pd.to_numeric(sdf[c], errors="coerce").fillna(0.0)
                predicted_state = str(state_model.predict(sdf)[0])
            except Exception as exc:
                print(f"[warn] state head predict failed: {exc}", file=sys.stderr)
                predicted_state = None
        df = pd.DataFrame([row])
        # 强制数值化：训练特征可能含字符串类别(None/str)，推理侧缺失时补 0.0，
        # 避免 LightGBM 4.x "bad pandas dtypes: object" 报错（被下方 except 吞导致 ai_score=None）
        for c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        p_raw = float(model.predict(df)[0])
        p = p_raw
        if iso is not None:
            p_cal = float(iso.predict([p_raw])[0])
            # 【B5 真凶修复】退化校准器（粗阶梯）会把整段 raw 概率压成同一常数，
            # 使 ai_score 恒定、模型形同摆设。此时按 blend_w 与 raw 概率混合，
            # 既保留校准的单调排序信息，又恢复分辨率（随行情波动）。
            if _calib_is_degenerate(iso):
                w = CALIB_BLEND_W
                p = w * p_cal + (1.0 - w) * p_raw
            else:
                p = p_cal
        # 【阶段 1·方向头】仅在 dir_model 加载时推断方向（灰度安全：默认 None）。
        # 类序与训练对齐：[-1, 0, 1]（空 / 观望 / 多）。校准器字典对各类概率 isotonic 校准。
        ai_direction = None
        ai_dir_prob = None
        if dir_model is not None:
            try:
                _dnames = (dir_model.feature_name() if hasattr(dir_model, "feature_name")
                           else dir_model.booster_.feature_name())
                dfeats = {k: feats.get(k) for k in _dnames}
                ddf = pd.DataFrame([dfeats])
                for c in ddf.columns:
                    ddf[c] = pd.to_numeric(ddf[c], errors="coerce").fillna(0.0)
                _raw_proba = dir_model.predict(ddf)  # (1,3) 类序 [-1,0,1]
                _classes = (-1, 0, 1)
                if dir_calibs is not None:
                    _cal_proba = []
                    for _i, _c in enumerate(_classes):
                        _raw_p = float(_raw_proba[0][_i])
                        _cal = dir_calibs.get(_c)
                        if _cal is not None:
                            _cp = float(_cal.predict([_raw_p])[0])
                            # 【阶段 1·校准裁决】该类别校准器退化 → 直接退回 raw 概率。
                            # 退化校准器是分段常数/ERROR，与 raw 混合仍残留常数偏置
                            # （如 v54 的 BUY/SELL 校准器），导致方向头恒 HOLD 塌缩；
                            # 退回 raw 才能恢复方向头随行情变化，解除恒 BUY/恒 HOLD。
                            if _calib_is_degenerate(_cal):
                                _cp = _raw_p
                            _cal_proba.append(_cp)
                        else:
                            _cal_proba.append(_raw_p)
                    _proba = _cal_proba
                else:
                    _proba = [float(x) for x in _raw_proba[0]]
                _best = int(max(range(3), key=lambda i: _proba[i]))
                ai_direction = ("BUY" if _classes[_best] == 1
                                else "SELL" if _classes[_best] == -1 else "HOLD")
                ai_dir_prob = float(_proba[_best])
                # ── 2026-09-01 dir_head 防抖：概率 EMA + 间隙带 ──
                # 背景（实测）：三分类 BUY/SELL 概率长期接近（探针 gap=0.077），argmax
                # 随 M5 实时特征噪声瞬间跳转；isotonic 校准分段常数使概率档位跳变
                # (0.5714↔0.6↔0.6667↔0.8571↔0.9)。本块两层稳定：
                #   ① EMA 平滑（抑制单次采样噪声，DIR_EMA_ALPHA=0.5）
                #   ② 间隙带：top1-top2 < DIR_MARGIN → 判定"不确定"，维持前值不跳
                # 原始 argmax 保留到 ai_direction_raw 供观测/面板，裁决用 ai_direction。
                _ai_dir_raw = ai_direction
                if _probe:
                    # probe（self-check / 配置重载探针）用全 0 特征，不应更新防抖状态，
                    # 否则 30s 周期探针会污染 EMA/方向状态，导致防抖间歇性失效。
                    pass
                else:
                    _p_sm = list(_proba)
                    if _DIR_STATE["proba"] is not None:
                        _p_sm = [DIR_EMA_ALPHA * a + (1.0 - DIR_EMA_ALPHA) * b
                                 for a, b in zip(_proba, _DIR_STATE["proba"])]
                    _DIR_STATE["proba"] = _p_sm
                    _top1, _top2 = sorted(_p_sm, reverse=True)[:2]
                    if _top1 - _top2 >= DIR_MARGIN:
                        _best_sm = int(max(range(3), key=lambda i: _p_sm[i]))
                        _dir_sm = ("BUY" if _classes[_best_sm] == 1
                                   else "SELL" if _classes[_best_sm] == -1 else "HOLD")
                    else:
                        _dir_sm = _DIR_STATE["dir"] if _DIR_STATE["dir"] else "HOLD"
                    _DIR_STATE["dir"] = _dir_sm
                    _DIR_STATE["raw"] = _ai_dir_raw
                    ai_direction = _dir_sm
                    ai_dir_prob = float(_top1)
            except Exception as exc:
                print(f"[warn] direction head predict failed: {exc}", file=sys.stderr)
                ai_direction = None
                ai_dir_prob = None
        # 【阶段 2·买点头】仅在 entry_model 加载时推断好买点概率（灰度安全：默认 None）。
        # 二分类：predict_proba[:,1] = 好买点概率，经 calib_np 校准后输出 ai_entry(0-1)。
        ai_entry = None
        if entry_model is not None:
            try:
                _enames = (entry_model.feature_name() if hasattr(entry_model, "feature_name")
                           else entry_model.booster_.feature_name())
                efeats = {k: feats.get(k) for k in _enames}
                edf = pd.DataFrame([efeats])
                for c in edf.columns:
                    edf[c] = pd.to_numeric(edf[c], errors="coerce").fillna(0.0)
                _e_raw = float(entry_model.predict(edf)[0])
                if entry_calib is not None:
                    _ep = float(entry_calib.predict([_e_raw])[0])
                    # 【阶段 1·校准裁决】校准器退化 → 与 raw 混合恢复分辨率
                    if _calib_is_degenerate(entry_calib):
                        _ep = CALIB_BLEND_W * _ep + (1.0 - CALIB_BLEND_W) * _e_raw
                    ai_entry = _ep
                else:
                    ai_entry = _e_raw
            except Exception as exc:
                print(f"[warn] entry head predict failed: {exc}", file=sys.stderr)
                ai_entry = None
        return (float(max(0.0, min(1.0, p))), predicted_state, ai_direction,
                ai_dir_prob, ai_entry, _DIR_STATE.get("raw"))
    except Exception as exc:
        print(f"[warn] score failed (feature mismatch?): {exc}", file=sys.stderr)
        return (None, None)


def _persist(conn, out, feats, snap):
    """落库 inference_log + runtime_event（纯观测，失败不影响主循环）。

    【P0-O1 2026-08-22】model_version 由 out["model_version"]（主循环已填 basename）
    写入，根治「inference_log.model_version 恒 NULL → 无法按版本切片归因/回滚评估」。
    【P1-O4 2026-08-22】snapshot JSON 补 ai_state 与特征健康统计（缺失/恒值/异常占比），
    使监控可分别按"预测状态"与"特征健康度"切片归因（不新增表列，最小侵入）。
    """
    try:
        _snap = ({k: snap.get(k) for k in ("direction", "grade", "hp_score", "k", "passed", "verdict")}
                 if snap else None)
        # 观测增强：ai_state + 特征健康统计合并进 snapshot JSON（纯观测，不改表结构）
        if isinstance(_snap, dict):
            if out.get("ai_state"):
                _snap["ai_state"] = out.get("ai_state")
            for _hk in ("feat_missing_ratio", "feat_constant_ratio", "feat_outlier_ratio"):
                if out.get(_hk) is not None:
                    _snap[_hk] = out.get(_hk)
            if out.get("model_version"):
                _snap["model_version"] = out.get("model_version")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO hcm_ai.inference_log "
                "(symbol, ai_score, total_score, ext_factor_score, sl_coeff, continuity, mode, model_version, features, snapshot) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    out.get("symbol"),
                    out.get("ai_score"),
                    out.get("total_score"),
                    out.get("ext_factor_score"),
                    None,
                    None,
                    out.get("mode"),
                    out.get("model_version"),
                    json.dumps(feats) if feats else None,
                    json.dumps(_snap, default=str) if _snap else None,
                ),
            )
            cur.execute(
                "INSERT INTO hcm_ai.runtime_event (event_type, symbol, status, detail) "
                "VALUES ('lm_inference', %s, 'ok', %s)",
                (out.get("symbol"),
                 json.dumps({"ai_score": out.get("ai_score"), "total_score": out.get("total_score")})),
            )
        conn.commit()
    except Exception as exc:
        print(f"[warn] persist failed: {exc}", file=sys.stderr)
        try:
            conn.rollback()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# 反转头（独立 head，2026-09-02 接入）
#   消费主号桥写入的持仓风险评分请求 hcm:ai:rev:req:*，用 300 根 M5 窗口
#   装配 22 维特征 → 反转头推理 → 写回 hcm:ai:reversal:{acct}:{ticket}。
# 纪律：
#   - 只算分、绝不改仓；SL 执行权唯一归主号桥。
#   - 全程 try/except：本模块异常绝不影响 quality 主链路（AI 评分）。
#   - 取数窗口 300 根（平价校验结论：<300 会截断持仓历史致特征失真）。
#   - 默认 ai.rev.enabled=false；未启用时零开销（不查库、不 scan Redis）。
# ══════════════════════════════════════════════════════════════════════
_REV_WINDOW = 300
_REV_CATS = {"direction": ["BUY", "SELL"], "session": ["asia", "europe", "us"]}
_rev_booster = None
_rev_calib = None
_rev_meta = None
_rev_loaded = False

try:
    from reversal_features import assemble as _rev_assemble, \
        build_indicators as _rev_build_indicators
except Exception as _rev_imp_err:      # 缺模块不得拖垮主链路
    _rev_assemble = _rev_build_indicators = None
    print(f"[warn] reversal_features import failed: {_rev_imp_err}", file=sys.stderr)


def _rev_load():
    """加载反转头模型+校准器+元数据。失败置 None，_rev_tick 静默降级。"""
    global _rev_booster, _rev_calib, _rev_meta, _rev_loaded
    try:
        if _rev_assemble is None:
            return False
        import lightgbm as lgb
        _d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
        _m, _c = os.path.join(_d, "lgbm_reversal_v1.txt"), os.path.join(_d, "calib_reversal_v1.pkl")
        _j = os.path.join(_d, "reversal_v1_meta.json")
        if not (os.path.exists(_m) and os.path.exists(_c)):
            print("[info] reversal head absent (model/calib missing) → 不启用", file=sys.stderr)
            return False
        _rev_booster = lgb.Booster(model_file=_m)
        with open(_c, "rb") as f:
            _rev_calib = pickle.load(f)
        if os.path.exists(_j):
            with open(_j, encoding="utf-8") as f:
                _rev_meta = json.load(f)
        _rev_loaded = True
        print(f"[info] reversal head loaded cutoff={(_rev_meta or {}).get('cutoff')} "
              f"pct={(_rev_meta or {}).get('score_pct')}", file=sys.stderr)
        return True
    except Exception as _e:
        print(f"[warn] reversal head load failed: {_e}", file=sys.stderr)
        _rev_booster = _rev_calib = _rev_meta = None
        _rev_loaded = False
        return False


def _rev_hget(r, key, default=""):
    """读配置项并解码为 str。

    ⚠️ redis-py 默认 decode_responses=False，hget 返回 bytes；
    直接 str(b'true') 会得到 "b'true'"（含 b'' 外壳），导致布尔判断恒假、
    功能静默失效。必须先 decode。
    """
    try:
        v = r.hget("hcm:config:v2", key)
        if v is None:
            return default
        return v.decode("utf-8", "ignore") if isinstance(v, (bytes, bytearray)) else str(v)
    except Exception:
        return default


def _rev_tick(conn, r, symbol):
    """处理待评分的持仓请求。幂等、可重入、绝不抛出。"""
    if not _rev_loaded or _rev_calib is None or _rev_meta is None:
        return
    try:
        if _rev_hget(r, "ai.rev.enabled", "false").strip().lower() \
                not in ("true", "1", "yes", "on"):
            return
        keys = r.keys("hcm:ai:rev:req:*")
        if not keys:
            return
        # 惰性取 300 根（仅当有待评分请求），不动 quality 主链路的 150 根取数
        with conn.cursor() as cur:
            cur.execute(
                "SELECT open_time, open, high, low, close FROM hcm_market.klines "
                "WHERE symbol=%s AND time_frame='M5' ORDER BY open_time DESC LIMIT %s",
                (symbol, _REV_WINDOW))
            rows = cur.fetchall()
        if not rows or len(rows) < 60:
            return
        kdf = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close"])
        for _c in ("open", "high", "low", "close"):
            kdf[_c] = kdf[_c].astype(float)
        kdf["open_time"] = pd.to_datetime(kdf["open_time"], utc=True)
        kdf = kdf.sort_values("open_time").reset_index(drop=True)
        ind = _rev_build_indicators(kdf)
        T = len(kdf) - 1
        times = kdf["open_time"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        cutoff = float(_rev_meta.get("cutoff", 1.1))
        order = _rev_meta.get("features") or []
        mode = _rev_hget(r, "ai.rev.mode", "log").strip().lower()

        for k in keys:
            try:
                raw = r.get(k)
                if not raw:
                    continue
                req = json.loads(raw)
                direction = req.get("direction")
                entry = float(req.get("open_price") or 0)
                if direction not in ("BUY", "SELL") or entry <= 0:
                    continue
                pos = {"direction": direction, "open_price": entry,
                       "sl": float(req.get("sl") or 0), "tp": float(req.get("tp") or 0)}
                et = pd.Timestamp(req.get("open_time"))
                if et.tzinfo is None:
                    et = et.tz_localize("UTC")
                eidx = min(int(np.searchsorted(
                    times, et.tz_convert("UTC").tz_localize(None).to_datetime64(),
                    side="left")), T)
                f = _rev_assemble(pos, kdf, ind, T, eidx)
                if f is None:
                    continue
                X = pd.DataFrame([f])[order]
                for _cc, _cats in _REV_CATS.items():
                    if _cc in X.columns:
                        X[_cc] = pd.Categorical(X[_cc], categories=_cats)
                score = float(_rev_calib.predict_proba(X)[:, 1][0])
                acct, tick = req.get("account_id", 0), req.get("ticket", 0)
                vd = {"score": round(score, 6), "cutoff": round(cutoff, 6),
                      "is_reversal": bool(score > cutoff), "mode": mode,
                      "symbol": symbol, "ticket": tick, "account_id": acct,
                      "ts": time.time()}
                r.set(f"hcm:ai:reversal:{acct}:{tick}", json.dumps(vd), ex=600)
                # 【2026-09-04 修复·重复算分】算分成功后删除请求键。
                # 桥侧 req 键 TTL=900s(mt5_bridge.py:4545)，若消费后不删除，主循环
                # 每轮都会重新扫到并重算重打日志（实测同 ticket 1 秒内重复 10 次）。
                # 异常路径不删，保留请求待下轮重试（失败安全）。
                try:
                    r.delete(k)
                except Exception:
                    pass
                print(f"[info] rev score ticket={tick} score={score:.4f} "
                      f"cutoff={cutoff:.4f} reversal={vd['is_reversal']} mode={mode}",
                      file=sys.stderr)
            except Exception as _e:
                print(f"[warn] rev score failed {k}: {_e}", file=sys.stderr)
    except Exception as _e:
        print(f"[warn] rev tick failed: {_e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--model", default=None, help="LightGBM 模型文件路径")
    ap.add_argument("--calib", default=None, help="校准器 pkl 路径")
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--redis-url", default=os.environ.get("REDIS_URL", REDIS_URL_DEFAULT))
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    print(f"[boot] main entered: symbol={args.symbol} redis={args.redis_url} db={args.db_url}", file=sys.stderr, flush=True)

    # 反转头模型加载（失败仅告警，不影响 quality 主模型与 AI 评分）
    _rev_load()

    # 连接建立包进重试循环：PG/Redis 短暂不可用时每 5s 重试，不退出进程。
    while True:
        r = None
        conn = None
        while True:
            try:
                if r is None:
                    r = redis.from_url(args.redis_url, socket_connect_timeout=5, socket_timeout=5)
                    r.ping()
                    print(f"[info] redis connected", file=sys.stderr, flush=True)
                if conn is None:
                    _db = args.db_url + ("?connect_timeout=5" if "?" not in args.db_url else "&connect_timeout=5")
                    conn = psycopg2.connect(_db)
                    print(f"[info] pg connected", file=sys.stderr, flush=True)
                break
            except Exception as exc:
                print(f"[warn] connect failed ({exc}); retry in 5s", file=sys.stderr, flush=True)
                time.sleep(5)

        # ── 配置热重载 + 模型加载（连接成功后执行，位于连接循环之外、外层 while 内）──
        # 【B4·2026-08-17 根因修复】配置块须位于连接循环之外、外层 while 内，
        # 连接成功 break 后才执行 _reload_cfg/主循环 → 正常发布 AI 快照。
        _cfg_reload_sec = 30.0
        _cfg_reloaded_at = -1e9
        _period_match = "none"
        _align_m5 = False
        # 【阶段 1·方向头】main 作用域变量，供 _reload_cfg 内 nonlocal 绑定。
        dir_model = None
        dir_calibs = None
        # 【阶段 2·买点头】main 作用域变量，供 _reload_cfg 内 nonlocal 绑定。
        entry_model = None
        entry_calib = None
        enabled = False
        _ai_mode = "decoupled"
        _model_path = None
        _calib_path = None
        model, iso, state_model, state_classes = None, None, None, None
        _model_status = "init"
        # 【B5·2026-08-17 修复】_feature_audit/_health_ttl 须在外层定义(主循环可访问)。
        # 原实现仅在 _reload_cfg 内定义局部变量，主循环引用 → NameError → 每轮 loop error。
        _feature_audit = False
        _health_ttl = 30
        # 【C·2026-08-17 回滚开关】ai.lm.raw_fallback=true 时跳过状态分层重锚，
        # 直接 ai_score=_raw*100（全局概率直出），状态头仅作 ai_state 观测。
        # 用于方案 X 上线后若发现重锚异常时的安全回退，无需改码/重启（配置热调）。
        _raw_fallback = False
        # 【A 档·2026-08-18 增强·配置热调】三者均为可选加固，默认关闭/零(=维持现状行为)，
        # 经 ai.lm.* 配置双写(PG+Redis)即时生效，无需重启 sidecar。
        #  (1) score_zscore   : 状态内 Z-score 标准化替代纯分位输出。极端低值映射为"低分有区分度"
        #                       而非被分位压成 0（根除你刚遇到的「评分变 0」脆弱点）。
        #  (2) score_smooth   : 跨轮 EMA 平滑 ai_score(0~1)，消除单棒抖动、提升稳定性。=0 关闭。
        #  (3) min_valid      : 平滑后 ai_score(<键阈值, 如 3.0) 自动回退 _raw*100，双保险防变 0。
        _score_zscore = False
        _score_smooth = 0.0
        _min_valid = 0.0
        # 【D1·2026-08-24 训练-推理 ds_* 口径统一】DeepSeek 票匹配窗口(秒)。
        # 与训练侧 quality_features._nearest_ds 的 ±window 对齐（默认 1800s=±30min）：
        # 推理侧读 Redis ai:ds:out 时，若票时间距今超过该窗口则视为"无票"→ ds_* 置 0，
        # 与训练侧"±30min 内匹配不到就 0 占位"严格一致，消除 ds_* 训练-推理分布错位
        # （PSI 恒虚高 7~8 的根因）。0 或负值=不做时间窗过滤（维持旧行为：恒读最新票）。
        _ds_window_sec = 1800
        # 【方案 X·2026-08-17 状态分层重锚】按预测状态维护在线分位缓冲。
        # 小样本下全局质量概率被 label 共用抹平(delta=0.0000)，但同状态内相对排序有效；
        # 改用「状态内相对分位×100」输出 ai_score，让 TREND/PULLBACK/REVERSAL/RANGE 各自分化。
        # 全局缓冲(_state_buf["__all__"])作 state 头缺失/失败时的兜底（全状态混合分位）。
        _STATE_BUF_MAX = 200
        _state_buf: dict = {"__all__": deque(maxlen=_STATE_BUF_MAX)}
        # 【A 档·2026-08-18 跨轮 EMA 平滑状态】按状态键存上一轮平滑后的 ai_score(0~100)，
        # 与新轮原始分位输出做 EMA 融合（_score_smooth 为平滑系数）。避免单棒抖动导致评分跳变。
        _smooth_state: dict = {}

        def _reload_cfg(force=False):
            """节流热重载 ai.* 配置；返回是否真重载了。"""
            global CALIB_BLEND_W, CALIB_MIN_LEVELS, FEAT_BASELINE
            nonlocal _cfg_reloaded_at, _period_match, _align_m5, enabled, _ai_mode
            nonlocal _model_path, _calib_path, model, iso, _feature_audit, _health_ttl
            nonlocal state_model, state_classes, _raw_fallback
            nonlocal dir_model, dir_calibs, entry_model, entry_calib
            nonlocal _score_zscore, _score_smooth, _min_valid, _ds_window_sec
            now = time.time()
            if not force and (now - _cfg_reloaded_at) < _cfg_reload_sec:
                return False
            _cfg_reloaded_at = now
            cfg = load_config(conn, redis_cli=r)
            enabled = _bool(cfg, "ai.enabled")
            _ai_mode = str(cfg.get("ai.mode") or "decoupled").strip().lower()
            _feature_audit = _bool(cfg, "ai.lm.feature_audit", False)
            _health_ttl = int(_cfg_float(cfg, "ai.lm.health_check_ttl", 30))
            _period_match = str(cfg.get("ai.lm.period_match") or "none").strip().lower()
            _align_m5 = _period_match == "align_m5"
            _raw_fallback = _bool(cfg, "ai.lm.raw_fallback", False)
            # 【阶段 1·方向头灰度】默认关闭：不加载方向头、不发布 ai_direction。
            # 开启后 sidecar 发布 ai_direction(BUY/SELL/HOLD)+ ai_dir_prob，供信号塔共振。
            _dir_enabled = _bool(cfg, "ai.lm.dir_enabled", False)
            _dir_path = (cfg.get("ai.lm.dir_path") or "").strip() or None
            _dir_calib_path = (cfg.get("ai.lm.dir_calib_path") or "").strip() or None
            # 【阶段 2·买点头灰度】默认关闭：不加载买点头、不发布 ai_entry。
            # 开启后 sidecar 发布 ai_entry(好买点概率 0-1)，供信号塔与 hexp entry_quality 共振。
            _entry_enabled = _bool(cfg, "ai.lm.entry_enabled", False)
            _entry_path = (cfg.get("ai.lm.entry_path") or "").strip() or None
            _entry_calib_path = (cfg.get("ai.lm.entry_calib_path") or "").strip() or None
            # 【A 档·2026-08-18 增强配置】三键缺省=关闭/零，维持现状行为(纯分位 + 无平滑)。
            _score_zscore = _bool(cfg, "ai.lm.score_zscore", False)
            _score_smooth = _cfg_float(cfg, "ai.lm.score_smooth", 0.0)
            _min_valid = _cfg_float(cfg, "ai.lm.min_valid", 0.0)
            # 【D1·2026-08-24】DS 票匹配窗口(秒)，与训练侧 quality_features 对齐。
            _ds_window_sec = _cfg_float(cfg, "ai.lm.ds_match_window_sec", _ds_window_sec)
            if _ds_window_sec < 0:
                _ds_window_sec = 1800
            # 平滑系数钳制到 [0,1)，0=关闭平滑(用当前轮值)；接近 1=强平滑(慢跟随)。
            if not (0.0 <= _score_smooth < 1.0):
                _score_smooth = 0.0
            CALIB_BLEND_W = _cfg_float(cfg, "ai.lm.calib_blend_w", CALIB_BLEND_W)
            CALIB_MIN_LEVELS = int(_cfg_float(cfg, "ai.lm.calib_min_levels", CALIB_MIN_LEVELS))
            _new_model = args.model or (cfg.get("ai.lm.model_path") or "").strip() or None
            _new_calib = args.calib or (cfg.get("ai.lm.calib_path") or "").strip() or None
            # 多任务状态头模型：与质量模型同目录的 lgbm_state.pkl（灰度切换时一并切换）
            _new_state = None
            if _new_model:
                _cand = os.path.join(os.path.dirname(_new_model), "lgbm_state.pkl")
                if os.path.exists(_cand):
                    _new_state = _cand
            # 【阶段 1·方向头】默认同目录按版本发现最新 lgbm_direction_v{N}.txt
            # + calib_dir_np_v{N}.pkl（2026-08-29 起不再硬编码 v54，见 _latest_version_path），
            # 仅当 ai.lm.dir_enabled=true 才加载（灰度安全，默认不加载）。
            # 显式配置 ai.lm.dir_path / dir_calib_path 优先于版本发现。
            _new_dir = None
            _new_dir_calib = None
            if _dir_enabled and _new_model:
                _d_cand = _dir_path or _latest_version_path(
                    os.path.dirname(_new_model), "lgbm_direction_v{}.txt")
                _dc_cand = _dir_calib_path or _latest_version_path(
                    os.path.dirname(_new_model), "calib_dir_np_v{}.pkl")
                if _d_cand and os.path.exists(_d_cand):
                    _new_dir = _d_cand
                    if _dc_cand and os.path.exists(_dc_cand):
                        _new_dir_calib = _dc_cand
            # 【阶段 2·买点头】与方向头同构：默认同目录按版本发现最新 lgbm_entry_v{N}.txt
            # + calib_entry_np_v{N}.pkl（2026-08-29 起不再硬编码 v54，见 _latest_version_path），
            # 仅当 ai.lm.entry_enabled=true 才加载（灰度安全，默认不加载）。
            # 显式配置 ai.lm.entry_path / entry_calib_path 优先于版本发现。
            _new_entry = None
            _new_entry_calib = None
            if _entry_enabled and _new_model:
                _e_cand = _entry_path or _latest_version_path(
                    os.path.dirname(_new_model), "lgbm_entry_v{}.txt")
                _ec_cand = _entry_calib_path or _latest_version_path(
                    os.path.dirname(_new_model), "calib_entry_np_v{}.pkl")
                if _e_cand and os.path.exists(_e_cand):
                    _new_entry = _e_cand
                    if _ec_cand and os.path.exists(_ec_cand):
                        _new_entry_calib = _ec_cand
            # 【滚动校准热加载 A 2026-09-04】模型/校准文件 mtime 指纹：路径不变但
            # 内容被原子替换（滚动校准新产物）时也触发重载（无需重启进程）。
            _mtime_sig = ""
            try:
                _sig_parts = []
                for _fp in (_new_calib, _new_dir_calib, _new_entry_calib,
                            _new_model, _new_dir, _new_entry, _new_state):
                    if _fp and os.path.exists(_fp):
                        _sig_parts.append(f"{os.path.getmtime(_fp):.0f}")
                _mtime_sig = "|".join(_sig_parts)
            except Exception:
                pass
            if ((_new_model, _new_calib) != (_model_path, _calib_path)
                    or model is None or _mtime_sig != getattr(_reload_cfg, "_mtime_sig", None)
                    or (_new_state != getattr(_reload_cfg, "_state_path", None))
                    or (_new_dir, _new_dir_calib) != (getattr(_reload_cfg, "_dir_path_loaded", None),
                                                     getattr(_reload_cfg, "_dir_calib_loaded", None))
                    or (_new_entry, _new_entry_calib) != (getattr(_reload_cfg, "_entry_path_loaded", None),
                                                         getattr(_reload_cfg, "_entry_calib_loaded", None))):
                _model_path, _calib_path = _new_model, _new_calib
                _reload_cfg._state_path = _new_state
                _reload_cfg._dir_path_loaded = _new_dir
                _reload_cfg._dir_calib_loaded = _new_dir_calib
                _reload_cfg._entry_path_loaded = _new_entry
                _reload_cfg._entry_calib_loaded = _new_entry_calib
                _reload_cfg._mtime_sig = _mtime_sig
                if _model_path and os.path.exists(_model_path):
                    model, iso, state_model, state_classes, dir_model, dir_calibs, entry_model, entry_calib = load_model(
                        _model_path, _calib_path, _new_state, _new_dir, _new_dir_calib,
                        _new_entry, _new_entry_calib)
                    # P1：加载同目录特征基准分布（PSI/离群检测用）
                    _bp = os.path.join(os.path.dirname(_model_path), "feature_baseline.json")
                    if os.path.exists(_bp):
                        try:
                            with open(_bp) as _bf:
                                FEAT_BASELINE = json.load(_bf)
                            print(f"[info] feature baseline loaded: "
                                  f"{len(FEAT_BASELINE.get('features', []))} features", file=sys.stderr)
                        except Exception as _be:
                            FEAT_BASELINE = None
                            print(f"[warn] baseline load failed: {_be}", file=sys.stderr)
                    else:
                        FEAT_BASELINE = None
                    # 模型重载 → 清空状态分位缓冲（避免新旧状态分布混合导致分位失真）
                    _state_buf.clear()
                    _state_buf["__all__"] = deque(maxlen=_STATE_BUF_MAX)
                    # 【D·模型热重载自检】加载后立即跑 score_one 自检，检测推理废。
                    _model_status = "ready"
                    try:
                        _probe = {c: 0.0 for c in (model.feature_name() if hasattr(model, "feature_name") else FEATURE_COLS)}
                        _p, _ps, _pd, _pdp, _pe, _pr = score_one(
                            model, iso, _probe, state_model, state_classes,
                            dir_model, dir_calibs, entry_model, entry_calib, _probe=True)
                        if _p is None or not (float("-inf") < float(_p) < float("inf")):
                            _model_status = "degraded"
                            model, iso, state_model, state_classes = None, None, None, None
                            dir_model, dir_calibs = None, None
                            entry_model, entry_calib = None, None
                            print(f"[warn] model self-check FAILED (NaN/inf predict) → degraded; "
                                  f"ai_score=null（纯 HEXP 降级）", file=sys.stderr)
                        else:
                            print(f"[info] model loaded+self-check OK: {_model_path} calib={_calib_path} "
                                  f"state={state_classes} blend_w={CALIB_BLEND_W} min_levels={CALIB_MIN_LEVELS} "
                                  f"probe_p={_p:.4f}", file=sys.stderr)
                    except Exception as _e:
                        _model_status = "degraded"
                        model, iso, state_model, state_classes = None, None, None, None
                        print(f"[warn] model self-check ERROR ({_e}) → degraded; ai_score=null", file=sys.stderr)
                else:
                    model, iso, state_model, state_classes = None, None, None, None
                    _model_status = "missing" if enabled else "disabled"
                    print(f"[warn] model missing ({_model_path}); enabled={enabled} → status={_model_status} "
                          f"（纯 HEXP 降级）", file=sys.stderr)
            print(f"[info] cfg reloaded: enabled={enabled} mode={cfg.get('ai.mode')} "
                  f"align_m5={_align_m5}", file=sys.stderr)
            return True

        try:
            _reload_cfg(force=True)  # 启动即加载一次
        except Exception as _re_cfg_e:
            print(f"[err] initial _reload_cfg FAILED: {_re_cfg_e!r}", file=sys.stderr, flush=True)
            import traceback as _re_tb
            _re_tb.print_exc(file=sys.stderr)
        _last_key = [None]
        while True:
            try:
                _reload_cfg()  # 节流热重载（<=30s 一次）
                raw = r.get(f"hcm:live:hexp:{args.symbol.upper()}")
                snap = json.loads(raw) if raw else {}
                ext = r.get("hcm:market:composite:score")
                ext_score = float(ext) * 100.0 if ext else None
                feats = None

                ai_score = None
                if enabled and model is not None and snap:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT open_time, open, high, low, close, spread FROM hcm_market.klines "
                            "WHERE symbol=%s AND time_frame='M5' ORDER BY open_time DESC LIMIT 150",
                            (args.symbol.upper(),),
                        )
                        rows = cur.fetchall()
                    if rows:
                        kdf = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "spread"])
                        kdf["open_time"] = pd.to_datetime(kdf["open_time"], utc=True)
                        kdf = kdf.sort_values("open_time").reset_index(drop=True)
                        kl = enrich_klines(kdf)
                        # 【2026-09-02 h1_trend_dir 特征注入】无论 align_m5 都加载 H1 特征(60s 缓存)：
                        # align_m5 时 build_features 仍会用 M5 同源覆盖 h1_adx/h1_trend_strength，
                        # 但 h1_trend_dir 必须来自真实 H1 K线（方向头感知 H1 主趋势的关键输入）。
                        _h1f = _cached_slow("h1", 60.0,
                                            lambda: _h1_features(conn, args.symbol.upper()))
                        _envf = _cached_slow("env", 60.0, lambda: _env_features(conn))
                        # 【DeepSeek 训练特征增强 2026-08-17】读 DeepSeek 异步票作为特征输入。
                        # 同步读 ai:ds:out:{symbol}（与主循环 r 同句柄）；缺失/损坏→None→三特征 0.0。
                        # 【D1·2026-08-24 训练-推理 ds_* 口径统一】读票后做时间窗过滤：
                        # 票的 ts 距今超过 _ds_window_sec（默认 1800s=±30min，与训练侧
                        # quality_features._nearest_ds 对齐）时视为"无票"→ _ds_out=None →
                        # build_features 里 ds_* 置 0，消除"训练侧 85% 为 0 vs 推理侧恒有值"
                        # 的分布错位（ds_* PSI 恒虚高 7~8 的根因）。_ds_window_sec<=0 则
                        # 不做过滤（维持旧行为：恒读最新票）。
                        _ds_out = None
                        try:
                            _ds_raw = r.get(f"ai:ds:out:{args.symbol.upper()}")
                            if _ds_raw:
                                _ds_out = json.loads(_ds_raw)
                                if not isinstance(_ds_out, dict):
                                    _ds_out = None
                                elif _ds_window_sec > 0:
                                    _ds_ts = _ds_out.get("ts")
                                    try:
                                        _ds_age = time.time() - float(_ds_ts)
                                        if _ds_age > _ds_window_sec:
                                            _ds_out = None  # 过期票视为无票，ds_* 置 0
                                    except (TypeError, ValueError):
                                        _ds_out = None
                        except Exception:
                            _ds_out = None
                        # 【TimesFM 特征 2026-08-30】按当前 M5 bar 查离线特征；缺失→{}→0.0 降级。
                        _bar_time = kl.iloc[-1]["open_time"] if (kl is not None and not kl.empty) else None
                        _tmf_out = _load_tmf_for_bar(conn, args.symbol.upper(), _bar_time)
                        feats = build_features(snap, kl, _h1f, _envf, align_m5=_align_m5,
                                               audit=_feature_audit, ds_out=_ds_out, tmf_out=_tmf_out)
                        _raw, _pred_state, _ai_dir, _ai_dir_prob, _ai_entry, _ai_dir_raw = score_one(
                            model, iso, feats, state_model, state_classes, dir_model, dir_calibs,
                            entry_model, entry_calib)
                        ai_score = None
                        if _raw is not None:
                            if _raw_fallback:
                                # 【C·回滚开关】全局概率直出（v0 行为），状态头仅作 ai_state 观测
                                ai_score = _raw * 100.0
                                # 【A 档·2026-08-18 修复·raw_fallback 路径补 min_valid 双保险】
                                # 裸绑模型输出时若 p_raw≈0 → ai_score 硬 0 → 面板/quality_gate 失敏。
                                # 与方案 X 分支对称：低于 min_valid 阈值(如 3.0)时回退到该下限而非 0，
                                # 保留"低质量但有区分度"的语义。_min_valid=0 关闭(=裸绑原行为)。
                                if _min_valid > 0.0 and ai_score < _min_valid:
                                    ai_score = _min_valid
                            else:
                                # 方案 X：状态内分位重锚（相对排序 > 全局绝对概率）
                                _key = _pred_state if _pred_state else "__all__"
                                if _key not in _state_buf:
                                    _state_buf[_key] = deque(maxlen=_STATE_BUF_MAX)
                                _buf = _state_buf[_key]
                                _buf.append(_raw)
                                # 全状态缓冲始终更新（兜底用）
                                _state_buf["__all__"].append(_raw)
                                # 分位 = 当前 p_raw 在状态缓冲中的相对位置（∈[0,1]）
                                _arr = list(_buf)
                                # warm-up 期（缓冲样本不足）→ 退回全局概率，避免首样恒 100% 虚高
                                if len(_arr) < 20:
                                    _pct = _raw
                                else:
                                    _below = sum(1 for v in _arr if v <= _raw)
                                    _pct = _below / len(_arr)
                                ai_score = _pct * 100.0

                                # 【A 档·2026-08-18 增强 1/3】Z-score 标准化替代纯分位输出。
                                # 纯分位在极端行情(状态模型恒低)下被压成 0 → 评分失敏(你刚遇的 bug)。
                                # Z-score=(p_raw-μ)/σ 保留"相对该状态分布"的方向与距离，
                                # 经 tanh 压到 [0,1] 且极端低值映射为低分(如 5~15)而非 0，维持区分度。
                                if _score_zscore and len(_arr) >= 20:
                                    _mu = float(np.mean(_arr))
                                    _sd = float(np.std(_arr))
                                    if _sd > 1e-6:
                                        _z = (_raw - _mu) / _sd
                                        # tanh 把 (-∞,+∞) 压到 (-1,1)，+1 后 /2 到 (0,1)
                                        _pct = (float(np.tanh(_z)) + 1.0) / 2.0
                                        ai_score = _pct * 100.0

                                # 【A 档·2026-08-18 增强 2/3】跨轮 EMA 平滑（消除单棒抖动）。
                                # smoothed = (1-α)*new + α*prev，α=_score_smooth(0=关,→1 强平滑)。
                                if _score_smooth > 0.0:
                                    _prev = _smooth_state.get(_key)
                                    if _prev is not None:
                                        ai_score = (1.0 - _score_smooth) * ai_score + _score_smooth * _prev
                                    _smooth_state[_key] = ai_score

                                # 【A 档·2026-08-18 增强 3/3】min_valid 双保险回退。
                                # 平滑/重锚后 ai_score 仍低于阈值(如 3.0) → 回退 _raw*100，
                                # 彻底根除"评分变 0"导致面板/quality_gate 失敏。_min_valid=0 关闭。
                                if _min_valid > 0.0 and ai_score < _min_valid:
                                    ai_score = _raw * 100.0
                                    # 回退时同步刷新平滑状态，避免下一轮被旧低值拉低
                                    _smooth_state[_key] = ai_score

                    # 【P1·推理防御】PSI 漂移/离群检测 + 业务二次钳位
                    # 【2026-08-24 拆炸弹】raw_fallback 路径(ai_score=_raw*100 原始概率直出)
                    # 传 force_keep=True：high 不置 None，改为强惩罚 ×0.6，保证评分持续有值可观测。
                    # 状态重锚路径(方案X)仍保留 force_keep=False，重度漂移仍可降级 HEXP-only。
                    ai_score, _psi_drift, _max_z, _drift_level = _inference_defense(
                        ai_score, feats, FEAT_BASELINE, force_keep=_raw_fallback)

                    hp = float(snap.get("hp_score") or 0.0)
                    total = (0.6 * hp + 0.4 * ai_score) if ai_score is not None else float(snap.get("scorecard_total") or hp)

                    _valid = bool(enabled and model is not None and ai_score is not None and snap)
                    if not enabled:
                        _status = "disabled"
                    elif model is None:
                        _status = _model_status if _model_status in ("missing", "degraded") else "missing"
                    else:
                        _status = _model_status if _model_status == "ready" else "ready"
                    out = {
                        "symbol": args.symbol.upper(),
                        "ai_score": round(ai_score, 2) if ai_score is not None else None,
                        "total_score": round(total, 2),
                        "ext_factor_score": round(ext_score, 2) if ext_score is not None else None,
                        "mode": "coupled" if (enabled and _ai_mode == "coupled") else "decoupled",
                        "period_match": _period_match,
                        "psi_drift": round(_psi_drift, 4) if "_psi_drift" in dir() else None,
                        "max_z": round(_max_z, 4) if "_max_z" in dir() else None,
                        "drift_level": _drift_level if "_drift_level" in dir() else "none",
                        "valid": _valid,
                        "status": _status,
                        "ai_enabled": bool(enabled),
                        "ai_mode": _ai_mode,
                        "model_loaded": bool(model is not None),
                        "ai_state": _pred_state if _pred_state else ("__all__" if (enabled and model is not None) else None),
                        # 【P0-O1 2026-08-22】记录当前模型版本 basename，供 inference_log
                        # 按版本切片归因/回滚评估（根治 model_version 恒 NULL 缺陷）。
                        "model_version": os.path.basename(_model_path) if _model_path else None,
                        # 【阶段 1·方向头】dir_lm 输出：供信号塔与 dir_hexp 共振（同向增强/反向否决）。
                        # 灰度过期：dir_enabled=false 时 dir_model=None → 两字段恒 None，完全不影响现有逻辑。
                        "ai_direction": _ai_dir if (_ai_dir is not None) else None,
                        "ai_dir_prob": round(float(_ai_dir_prob), 4) if _ai_dir_prob is not None else None,
                        # 【阶段 1·防抖观测 2026-09-01】ai_direction_raw = argmax 原始方向（未防抖），
                        # 供对比防抖前后差异与面板实时跳动；裁决统一用 ai_direction。
                        "ai_direction_raw": _ai_dir_raw if _ai_dir_raw is not None else None,
                        # 【阶段 2·买点头】entry_lm 输出：好买点概率(0-1)，供信号塔与 hexp entry_quality 共振
                        # （boost_good_entry 增强好点位 / veto_bad_entry 否决差点位）。灰度过期恒 None。
                        "ai_entry": round(float(_ai_entry), 4) if _ai_entry is not None else None,
                        # 【P1-O4 2026-08-22】特征健康统计（缺失/恒值/离群占比）→ 落库观测。
                        "feat_missing_ratio": None,
                        "feat_constant_ratio": None,
                        "feat_outlier_ratio": None,
                        "ts": time.time(),
                    }
                    # 特征健康统计需在 feats 就绪后计算（_feature_health 接受 feats dict）
                    if feats is not None:
                        try:
                            _fh = _feature_health(feats, FEAT_BASELINE)
                            out["feat_missing_ratio"] = _fh["feat_missing_ratio"]
                            out["feat_constant_ratio"] = _fh["feat_constant_ratio"]
                            out["feat_outlier_ratio"] = _fh["feat_outlier_ratio"]
                        except Exception:
                            pass
                    if feats is not None:
                        try:
                            out["lm_features"] = {k: (None if v is None else round(float(v), 6))
                                                  for k, v in feats.items()}
                        except Exception:
                            pass
                    # 【P2-O6 2026-08-22】连续降级告警：模型在场但无有效 AI 分（推理退化/
                    # 漂移重度拦截）连续 N 轮 → 打告警日志，提醒下游已纯 HEXP 降级。
                    # 有有效 AI 分 → 清零计数。
                    try:
                        if enabled and model is not None and ai_score is None:
                            _streak, _al = _degrade_alert(time.time())
                            out["degrade_streak"] = _streak
                        else:
                            _degrade_reset()
                            out["degrade_streak"] = 0
                    except Exception:
                        pass
                    r.set(f"hcm:live:hexp:ai:{args.symbol.upper()}", json.dumps(out), ex=15)
                    try:
                        r.set("ai.lm.health_check", json.dumps({
                            "status": _status, "enabled": bool(enabled),
                            "mode": _ai_mode, "model_loaded": bool(model is not None),
                            "symbol": args.symbol.upper(), "ts": time.time(),
                        }), ex=_health_ttl)
                    except Exception:
                        pass
                    # 反转头：处理持仓风险评分请求（内部全异常隔离，不影响 AI 评分主链路）
                    try:
                        _rev_tick(conn, r, args.symbol.upper())
                    except Exception:
                        pass
                    _key = (out.get("ai_score"), out.get("total_score"), out.get("ext_factor_score"), out.get("mode"))
                    if _key != _last_key[0]:
                        _persist(conn, out, feats, snap)
                        _last_key[0] = _key
                    if args.once:
                        print(json.dumps(out, ensure_ascii=False))
                        break
            except Exception as exc:
                import traceback as _tb
                _tb.print_exc(file=sys.stderr)
                print(f"[warn] scoring loop error: {exc}", file=sys.stderr)
                time.sleep(args.interval)
            # 【B4·结构修复】不再每轮 conn.close()，改为复用连接；仅损坏时置 None 触发外层重连，
            # 消除"finally 关 conn + 外层重连"导致的无限重连循环。
            # 【B8·2026-08-17 回归修复】conn=None 后必须 break 跳出内层主循环，回到外层 while 重连。
            # 原实现仅置 None 未 break → 下一轮主循环 _reload_cfg 用 None conn 崩
            # （'NoneType' has no attribute 'cursor'）→ 刷屏 scoring loop error → 永不发布 AI 快照。
            try:
                conn.rollback()
            except Exception:
                conn = None
                break  # 回外层 while 重连（:363），不再继续用 None conn 空转

if __name__ == "__main__":
    main()
