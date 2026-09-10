#!/usr/bin/env python3
"""auto_retrain.py — LightGBM 信号质量模型自动重训闭环（路径 A+B+C）。

设计目标（用户 2026-08-20 决策：A/B/C 全做，先搭功能架构，数据再积累）：
  A. 自动重训：PG 标注(hcm_ai.labeled_samples) → labels.csv/features.csv →
     训练产出新版本模型 → 回测 → 切 ai.lm.model_path（sidecar 30s 内热重载）。
  B. DeepSeek 裁判：回测指标 + 样本分布摘要发给 DeepSeek，由其判定
     "采用/回滚 + 理由"；DeepSeek 不可用/超时 → 按本地 AUC 阈值 fail-open 决策。
  C. DeepSeek 特征增强：quality_features.py 已把 ds_fake_prob/ds_sl_coeff/
     ds_continuity 近邻匹配注入训练 CSV（与推理侧 build_features 同契约），
     本脚本只需调用 quality_features 即自动生效，无需改任何已上线代码。

铁律合规：
  - 纯新增文件，不改动任何已上线功能（路径 C 的契约层已于 2026-08-17 就绪）。
  - 训练产物落 tools/_artifacts/，模型落 tools/models/（与生产 sidecar 同目录）。
  - 所有外部调用（PG / DeepSeek / Redis）均有超时+降级兜底，绝不因重训失败卡死主链路。
  - 模型切换经 config_provider.set 双写 PG+Redis+PUB，与现有配置热重载机制一致。

用法：
  # 单次跑（验证/调试）
  python auto_retrain.py --once

  # 常驻守护（每 --interval-hours 小时一轮，默认 24）
  python auto_retrain.py --daemon --interval-hours 24

  # 强制忽略 DeepSeek 裁判、纯本地 AUC 决策（离线/无 key 环境）
  python auto_retrain.py --once --no-deepseek

依赖：lightgbm, scikit-learn, pandas, numpy, psycopg2, requests
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import psycopg2
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
try:
    import lightgbm as lgb
except Exception:
    lgb = None
try:
    from sklearn.metrics import roc_auc_score as _roc_auc
except Exception:
    _roc_auc = None

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS_DIR)
from _monitor_common import (load_baseline, compute_psi_batch,
                             build_live_baseline_from_features,
                             load_live_baseline, save_live_baseline, LIVE_BASELINE_PATH)
ARTIFACTS = os.path.join(TOOLS_DIR, "_artifacts")
MODELS_DIR = os.path.join(TOOLS_DIR, "models")
os.makedirs(ARTIFACTS, exist_ok=True)
os.makedirs(MODELS_DIR, exist_ok=True)

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"
REDIS_HOST = "localhost"
REDIS_PORT = 6379

# 本地决策阈值（fail-open 护栏）：测试集 AUC 低于此值视为退化，不切模型
LOCAL_AUC_ADOPT_MIN = 0.55

# 【阶段 2·健康判定 2026-08-29】方向头 / 买点头本地采纳阈值（用户决策 1：0.55）。
# 与质量头同阈值，但**独立判定、独立处置**（用户决策 2 / 4）：
#   - 决策 2：单头不达标 → **立即校准**，而非禁用该头（避免一损俱损）
#   - 决策 4：方向/买点头走**本地阈值**，不交 DeepSeek 裁判（省调用、降外部依赖）
#   - 指标口径：dir_hit 为 C 口径（仅真实有方向样本的正确率），entry 为测试集 AUC
# 可用环境变量覆盖，便于灰度调参而不改代码。
HEAD_METRIC_MIN = float(os.environ.get("HEAD_METRIC_MIN", "0.55"))

# 【阶段 3·自愈闭环 2026-08-29】
# 决策 5：连续 N 轮复活失败才告警人工（Redis 计数 hcm:ai:retrain:fail_streak）。
#   判定口径：切换成功(switched=True)=本轮复活成功 → 清零；未切换/训练失败=+1。
FAIL_STREAK_KEY = "hcm:ai:retrain:fail_streak"
FAIL_STREAK_ALERT = int(os.environ.get("RETRAIN_FAIL_ALERT", "3"))
# 决策 6：模型版本保留最近 N 版，更老的连同配套三头文件一并清理。
KEEP_MODEL_VERSIONS = int(os.environ.get("RETRAIN_KEEP_VERSIONS", "3"))
# 样本量下限：少于此数训练结果不可靠，仅产模型不切（避免用噪声数据覆盖好模型）
# 支持环境变量 RETRAIN_MIN_SAMPLES 覆盖（验证时可临时调大，避免误切线上模型）
MIN_SAMPLES_FOR_SWITCH = int(os.environ.get("RETRAIN_MIN_SAMPLES", "200"))
# 【P0-O3 2026-08-22】DeepSeek 三特征非零占比硬门槛：低于此值说明训练集大部分
# ds_* 列是 0.0 占位（历史缺 DS 票）→ 模型实质未吸收 ds 语义，切上线只会带来
# "看似重训了、实则没吸收新信号"的假精准。低于阈值 → 只产模型不切换（与样本量
# 不足同级别的护栏）。支持环境变量 DS_MIN_NONZERO_RATIO 覆盖。
DS_MIN_NONZERO_RATIO = float(os.environ.get("DS_MIN_NONZERO_RATIO", "0.30"))

# ── P3-B/C 触发阈值（环境变量可覆盖）────────────────────────────────────
PSI_TRIGGER = float(os.environ.get("PSI_TRIGGER", "0.25"))          # 特征分布漂移阈值（普遍漂移）
PSI_HARD_TRIGGER = float(os.environ.get("PSI_HARD_TRIGGER", "0.50"))  # 单特征重度漂移阈值（≥1 个即触发）
CALIB_COLLAPSE_PCT = float(os.environ.get("CALIB_COLLAPSE_PCT", "0.90"))  # 置信坍缩占比阈值
DRY_RUN = False  # 置 True 时 switch_model 只记录不切换（验证/灰度用）
# 【2026-08-24 修复】高波动/非平稳环境特征：分布天然不平稳（如 event_proximity_min
# 是"距下一重大事件分钟数"，事件日历变化导致其分布随时间剧变），PSI 恒虚高，
# 既不应作为重训触发依据，也避免干扰 psi_max（制造"重度漂移"假象）。报表仍展示，
# 仅重训触发时豁免。可从环境变量 PSI_EXEMPT_FEATURES 追加（逗号分隔）。
# 【2026-08-25 修复】新增豁免 ds_continuity/ds_fake_prob/ds_sl_coeff：三者是
# DeepSeek 输出特征，训练侧 92%+ 为缺省 0.0（近常数/极偏分布），PSI 对近常数特征
# 数值爆炸（实证 ds_continuity PSI=3.3154 制造 max 重度漂移假象，且单特征>硬阈值
# 0.5 会误触发重训）。豁免后不参与重训触发与 psi.max 统计，报表 per_feature 仍展示。
PSI_EXEMPT_FEATURES = set(
    (os.environ.get("PSI_EXEMPT_FEATURES",
                    "event_proximity_min,ds_continuity,ds_fake_prob,ds_sl_coeff") or "").split(",")
)

PY = sys.executable
DS_API_DEFAULT = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1/chat/completions")
DS_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DS_MODEL_DEFAULT = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def load_ds_config_from_pg() -> dict:
    """从 PG 配置中心(hcm_config.metadata)读取 DeepSeek 配置——与系统其他模块一致的真源。

    优先级：环境变量 > PG(hcm_config.metadata) > 模块内置默认值。
    这样守护无需手动填 key：只要 PG 里 deepseek.api_key 已配（实测已配，
    sk-5... 35字符），裁判即自动链动；PG 缺失则回退模块默认，再由
    deepseek_judge 的 no_key 兜底走 local_fallback（不卡死）。
    """
    cfg = {
        "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "api_base": os.environ.get("DEEPSEEK_API_BASE", ""),
        "model": os.environ.get("DEEPSEEK_MODEL", ""),
    }
    try:
        import psycopg2
        conn = psycopg2.connect(DB_URL_DEFAULT, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT config_key, current_value FROM hcm_config.metadata "
                    "WHERE config_key IN ('deepseek.api_key','deepseek.api_base','deepseek.model')"
                )
                for k, v in cur.fetchall():
                    if k == "deepseek.api_key" and not cfg["api_key"]:
                        cfg["api_key"] = v or ""
                    elif k == "deepseek.api_base" and not cfg["api_base"]:
                        # 归一化：DeepSeek 开放 API 标准端点为 /v1/chat/completions。
                        # PG 真源常只存根域名 https://api.deepseek.com（别处共享配置），
                        # 此处补全路径，不擅自改 PG 真源值（铁律：不单边改配置）。
                        base = (v or "").rstrip("/")
                        if base and not base.endswith("/chat/completions"):
                            if base.endswith("/v1"):
                                base += "/chat/completions"
                            elif "/v1/" not in base:
                                base += "/v1/chat/completions"
                        cfg["api_base"] = base
                    elif k == "deepseek.model" and not cfg["model"]:
                        cfg["model"] = v or ""
        finally:
            conn.close()
    except Exception as e:
        log(f"[ds-config] PG read failed (non-fatal, fall back to env/default): {e}")
    return cfg


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(os.path.join(TOOLS_DIR, "auto_retrain.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── 版本自增 ──────────────────────────────────────────────────────────────
def next_model_version() -> int:
    """扫描 tools/models/lgbm_quality_vN.txt，返回最大 N+1。"""
    import re
    max_v = 0
    for fn in os.listdir(MODELS_DIR):
        m = re.match(r"lgbm_quality_v(\d+)\.txt$", fn)
        if m:
            max_v = max(max_v, int(m.group(1)))
    return max_v + 1


# ── 子进程调用 ────────────────────────────────────────────────────────────
def run(cmd: list[str], timeout: int = 600) -> tuple[int, str, str]:
    log(f"[run] {' '.join(cmd)}")
    try:
        # 【2026-08-24 修复】Windows 下 subprocess text=True 默认用 locale 编码(GBK)解码
        # 子进程 UTF-8 中文输出 → 乱码 → parse_auc/ds_nonzero_ratio 正则匹配失败
        # （日志实证 ds_nonzero_ratio=None、裁判信息不足）。强制 UTF-8 解码。
        p = subprocess.run(cmd, cwd=TOOLS_DIR, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


# ── 回测指标解析 ──────────────────────────────────────────────────────────
def parse_auc(stdout: str) -> float | None:
    """从 train_signal_quality.py 输出抓 [split] 或 [tss-summary] 的 AUC。"""
    import re
    best = None
    for line in stdout.splitlines():
        m = re.search(r"AUC=(\d+\.\d+)", line)
        if m:
            v = float(m.group(1))
            if best is None or "tss-summary" in line or "test" in line.lower():
                best = v
    # 优先 tss-summary 均值（时序交叉验证更可靠）
    m = re.search(r"tss-summary\] AUC mean=(\d+\.\d+)", stdout)
    if m:
        return float(m.group(1))
    return best


def count_samples(stdout: str) -> int:
    import re
    m = re.search(r"joined rows=(\d+)", stdout)
    return int(m.group(1)) if m else 0


def ds_nonzero_ratio(stdout: str) -> float | None:
    import re
    # 【2026-08-24 修复】train 的 ds_diag 改纯 ASCII 输出（避免 Windows 编码乱码）；
    # 兼容新旧两种格式：
    #   新(ASCII): [ds_diag] WARNING: DeepSeek nonzero ratio only 13.9% ...
    #   新(ASCII): [ds_diag] DeepSeek nonzero ratio 35.0% (>=30%), usable ...
    #   旧(中文): [ds_diag] DeepSeek 特征非零占比 13.9% ...
    for pat in (
        r"DeepSeek nonzero ratio (?:only |)(\d+\.\d+)%",
        r"DeepSeek 特征非零占比(?:仅|)(\d+\.\d+)%",
    ):
        m = re.search(pat, stdout)
        if m:
            return float(m.group(1)) / 100.0
    return None


def _bump_fail_streak(r, ok: bool) -> int:
    """更新「连续复活失败」计数（Redis）；成功清零。返回当前连续失败轮数。

    【阶段 3·自愈闭环 2026-08-29】用户决策 5：连续 3 轮复活失败才告警人工。
    判定口径：切换成功(switched=True) = 复活成功 → 清零；
    未切换 / 训练失败 = 本轮未复活 → 计数 +1。
    仅记录，绝不因计数失败影响重训主流程（try/except 兜底）。
    """
    try:
        if ok:
            if r is not None:
                r.delete(FAIL_STREAK_KEY)
            return 0
        n = 0
        if r is not None:
            raw = r.get(FAIL_STREAK_KEY)
            n = int(raw or 0) + 1
            r.set(FAIL_STREAK_KEY, str(n))
        return n
    except Exception:
        return 0


def _protected_model_files() -> set:
    """当前 ai.lm.*_path 正引用的模型/校准器文件名集合（清理时一律跳过）。

    【2026-09-03 防误删兜底】方向头/买点头/反转头的路径配置由人工维护，
    可能仍指向旧版本（如 entry_path=v81 而磁盘已推进到 v85）。若只按版本号
    清理，会把配置正引用的文件删掉 → 配置指向悬空文件 → sidecar 加载失败
    → 该头在前端显示「未启用」（2026-09-03 方向头事故的直接成因）。
    故无论版本号多老，只要被配置引用就永不删除。
    """
    names = set()
    keys = ("ai.lm.model_path", "ai.lm.calib_path", "ai.lm.dir_path",
            "ai.lm.dir_calib_path", "ai.lm.entry_path", "ai.lm.entry_calib_path")
    vals = []
    # ① Redis 缓存（hcm:config:v2 单哈希）
    try:
        import redis as _redis_lib
        _rc = _redis_lib.Redis.from_url(
            os.environ.get("REDIS_URL", "") or "redis://localhost:6379",
            socket_timeout=3,
        )
        for k in keys:
            v = _rc.hget("hcm:config:v2", k)
            if v:
                vals.append(v.decode("utf-8", "ignore")
                            if isinstance(v, (bytes, bytearray)) else str(v))
    except Exception as e:
        log(f"[cleanup] Redis 配置引用读取失败（回退 PG）: {e}")
    # ② PG 真源（hcm_config.metadata），覆盖 Redis 缺失/过期/分裂的键
    try:
        conn = psycopg2.connect(DB_URL_DEFAULT, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT config_key, current_value FROM hcm_config.metadata "
                    "WHERE config_key LIKE 'ai.lm.%_path'"
                )
                for _k, _v in cur.fetchall():
                    if _v:
                        vals.append(str(_v))
        finally:
            conn.close()
    except Exception as e:
        log(f"[cleanup] PG 配置引用读取失败: {e}")
    for v in vals:
        v = (v or "").strip()
        if not v:
            continue
        for sep in ("/", "\\"):
            if sep in v:
                v = v.split(sep)[-1]
        names.add(v.strip())
    return names


def cleanup_old_versions(keep: int | None = None) -> None:
    """清理 models 目录旧版本（用户决策 6：保留最近 3 版）。

    【2026-09-03 五头独立版本管理·替换旧的一刀切逻辑】
    旧逻辑以「质量头版本号」为基准，一次性删除该版本号的六件套
    （quality/calib/direction/calib_dir/entry/calib_entry）。但三头版本号
    并不同步，且 switch 只更新质量头路径、从不更新 dir_path/entry_path
    → 方向头/买点头配置僵死在旧版本号，该版本号一旦滚出「最近 keep 版」
    即被连带删除 → 配置指向悬空 → 该头加载失败（前端「未启用」）。

    新逻辑（三条）：
      1) 五头（quality / direction / entry / reversal / state）**各自按自身
         文件名模式独立解析版本号、各自保留最近 keep 版**，互不影响；
      2) 每头只清理**自己那头**的配套文件（模型 + 校准器），绝不跨头删除；
      3) 叠加「配置引用保护」：ai.lm.*_path 正指向的文件一律跳过，永不删除。

    调用时机：仅在切换成功后（此时新版本已上线，清理更老版本不影响 sidecar）。
    """
    import re as _re
    import glob as _glob
    import os as _os

    keep = KEEP_MODEL_VERSIONS if keep is None else int(keep)

    # (头名称, 版本文件 glob, 版本号正则, 该头配套文件名模板(含 {v} 占位))
    _HEADS = (
        ("quality", "lgbm_quality_v*.txt", r"lgbm_quality_v(\d+)\.txt$",
         ("lgbm_quality_v{v}.txt", "calib_v{v}.pkl")),
        ("direction", "lgbm_direction_v*.txt", r"lgbm_direction_v(\d+)\.txt$",
         ("lgbm_direction_v{v}.txt", "calib_dir_np_v{v}.pkl")),
        ("entry", "lgbm_entry_v*.txt", r"lgbm_entry_v(\d+)\.txt$",
         ("lgbm_entry_v{v}.txt", "calib_entry_np_v{v}.pkl")),
        ("reversal", "lgbm_reversal_v*.txt", r"lgbm_reversal_v(\d+)\.txt$",
         ("lgbm_reversal_v{v}.txt", "calib_reversal_v{v}.pkl")),
        ("state", "lgbm_state_v*.pkl", r"lgbm_state_v(\d+)\.pkl$",
         ("lgbm_state_v{v}.pkl",)),
    )

    protected = _protected_model_files()

    for head, pat, rx, tmpl in _HEADS:
        versions = []
        for p in _glob.glob(_os.path.join(MODELS_DIR, pat)):
            m = _re.search(rx, _os.path.basename(p))
            if m:
                versions.append(int(m.group(1)))
        if len(versions) <= keep:
            continue
        versions.sort(reverse=True)
        for v in versions[keep:]:
            grp = [t.format(v=v) for t in tmpl]
            # 整组保护：该版本任一文件被配置引用 → 整组保留。
            # 避免"留模型删校准器"（或反之）的半组残留——两者缺一均无法加载。
            if any(n in protected for n in grp):
                log(f"[cleanup] 跳过 {head} v{v} 整组"
                    f"（含被 ai.lm.*_path 引用的文件，保持配套完整）")
                continue
            for name in grp:
                p = _os.path.join(MODELS_DIR, name)
                if _os.path.exists(p):
                    try:
                        _os.remove(p)
                        log(f"[cleanup] 删除旧版本文件 {name}")
                    except Exception as e:
                        log(f"[cleanup] 删除失败 {name}: {e}")
            log(f"[cleanup] {head} 头版本 v{v} 已清理（保留最近 {keep} 版）")


def parse_dir_hit(stdout: str) -> float | None:
    """【阶段 2·健康判定 2026-08-29】解析方向头 C 口径命中率。

    训练侧输出形如::

        [direction_head] dir_hit=0.6123 n_dir=1234 n_flat=567

    **C 口径**（用户 2026-08-29 选定）：只统计「真实有方向」
    (``dir_label ∈ {-1,+1}``) 的样本中模型猜对的比例；观望样本
    (``dir_label=0``) 既不计入分子也不计入分母 —— 衡量"该出手时准不准"，
    避免被大量观望样本稀释出虚高准确率。

    ``dir_hit=None``（无真实有方向样本）返回 None，调用方按"不阻塞"处理。
    """
    import re
    m = re.search(r"\[direction_head\]\s+dir_hit=(\d+\.\d+)", stdout)
    return float(m.group(1)) if m else None


def parse_entry_auc(stdout: str) -> float | None:
    """【阶段 2·健康判定 2026-08-29】解析买点头测试集 AUC。

    训练侧输出形如::

        [entry_head] AUC=0.6789 win_rate=0.512 n=1234

    ⚠️ 必须用 ``[entry_head]`` 前缀精确定位：既有 ``parse_auc`` 用的是通用
    正则 ``AUC=(\\d+\\.\\d+)``，**会一并匹配到本行**。若不加前缀区分，
    质量头 AUC 可能被买点头数值污染（反之亦然）。
    """
    import re
    m = re.search(r"\[entry_head\]\s+AUC=(\d+\.\d+)", stdout)
    return float(m.group(1)) if m else None


# ── 路径 B：DeepSeek 裁判 ─────────────────────────────────────────────────
def deepseek_judge(api_base: str, api_key: str, payload: dict, timeout: int = 60,
                   model: str = "deepseek-chat") -> dict:
    """把回测摘要发给 DeepSeek，返回 {decision: 'adopt'|'rollback', reason: str}。

    超时/无 key/异常 → 返回 {decision: 'local_fallback', reason: 'ds_unavailable'}，
    调用方据此走本地 AUC 决策（fail-open，绝不卡死）。
    """
    if not api_key:
        return {"decision": "local_fallback", "reason": "no_deepseek_key"}
    try:
        import requests
        prompt = (
            "你是量化模型训练的质量裁判。以下是 LightGBM 信号质量模型的一次重训结果摘要，"
            "请判断【是否采用新模型替换线上模型】。\n"
            "采用标准：测试 AUC 较基线有实质提升、样本量充足、DeepSeek 特征吸收充分、"
            "无退化迹象。\n"
            "【硬性回滚条件】满足任一即必须 rollback：\n"
            "  (1) AUC<0.55 或样本<200 或校准器退化；\n"
            "  (2) ds_nonzero_ratio(DeepSeek特征非零占比)<0.30 —— 此时模型未真正吸收 DeepSeek"
            "语义，即使AUC达标也是\"假精准\"，必须回滚（不要因AUC达标就判adopt）。\n"
            "只有 AUC≥0.55、样本≥200、校准器未退化、且 ds_nonzero_ratio≥0.30 同时满足才 adopt。\n"
            "只返回一个 JSON：{\"decision\": \"adopt\" 或 \"rollback\", \"reason\": \"中文简述\"}\n\n"
            f"摘要：{json.dumps(payload, ensure_ascii=False)}"
        )
        resp = requests.post(
            api_base,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.0, "max_tokens": 300},
            timeout=timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        # 容错：从返回里抠 JSON
        import re
        jm = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if jm:
            return json.loads(jm.group(0))
        return {"decision": "local_fallback", "reason": f"ds_unparseable: {content[:120]}"}
    except Exception as e:
        return {"decision": "local_fallback", "reason": f"ds_error: {e}"}


# ── 模型切换（双写 PG+Redis）─────────────────────────────────────────────
def switch_model(model_path: str, calib_path: str) -> bool:
    """把新模型路径写 ai.lm.model_path / ai.lm.calib_path（PG+Redis 双写+PUB）。

    优先用 config_provider.set（若存在），否则直接双写 PG+Redis。
    返回是否成功。DRY_RUN=True 时只记录不切换（验证/灰度用）。
    """
    if DRY_RUN:
        log(f"[switch] DRY_RUN=True, skip actual switch (would set {model_path})")
        return True
    try:
        import psycopg2
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5, decode_responses=True)

        # 【2026-08-28 P1-8】写序修正（原：先 Redis 后 PG）。
        # 原实现一旦 PG 写入失败/抛异常，Redis 已被改写且**无回滚** → 产生不可自愈的
        # split-brain：引擎读 Redis 拿到新模型路径，而 PG 真值仍是旧路径；此后任何
        # 回填或校准都会用 PG 旧值覆盖回来，表现为"模型切换后又复原"。
        # 铁律 5.2：PG 为真源 —— 必须先写 PG 且提交成功后，才写 Redis 并广播失效。
        #
        # 1) PG 直写（hcm_config.metadata 真源）—— 失败即整体失败，Redis 保持原样
        conn = psycopg2.connect(DB_URL_DEFAULT, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                for key, val in (("ai.lm.model_path", model_path), ("ai.lm.calib_path", calib_path)):
                    # default_value 是 NOT NULL 列：INSERT 时与 current_value 同值；
                    # 已存在则仅更新 current_value（default_value 保持不变）。
                    cur.execute(
                        "INSERT INTO hcm_config.metadata "
                        "(config_key, current_value, default_value, value_type, category) "
                        "VALUES (%s, %s, %s, 'string', 'ai') "
                        "ON CONFLICT (config_key) DO UPDATE SET current_value = EXCLUDED.current_value",
                        (key, val, val),
                    )
                conn.commit()
            log("[switch] PG hcm_config.metadata updated")
        finally:
            conn.close()

        # 2) Redis 直写 + PUB（仅在 PG 提交成功后执行）
        r.hset("hcm:config:v2", "ai.lm.model_path", model_path)
        r.hset("hcm:config:v2", "ai.lm.calib_path", calib_path)
        r.publish("hcm:config:invalidate", "ai.lm.model_path")
        r.publish("hcm:config:invalidate", "ai.lm.calib_path")
        log("[switch] Redis hcm:config:v2 updated + PUB")
        return True
    except Exception as e:
        log(f"[switch] FAILED: {e}")
        return False


def record_retrain_run(payload: dict):
    """记录重训历史到 Redis 键 hcm:ai:retrain:last（JSON）+ 保留最近 50 条列表。

    设计取舍：不写入 hcm_ai.calibration_daily（该表是 reconcile_labels 的产出表，
    列结构 win_rate/trades 等与本脚本的模型版本语义不同，擅自入侵列=铁律禁止的
    schema 改动）。改用 Redis 单一真源记录，零 schema 风险，面板/诊断可经
    hcm:ai:retrain:last 查最新、hcm:ai:retrain:history 查近 50 轮。
    """
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5, decode_responses=True)
        _payload = dict(payload)  # 不污染调用方 payload（retrain_once 还要 return）
        _payload["dry_run"] = bool(DRY_RUN)
        _payload.setdefault("at", datetime.now(timezone.utc).isoformat())
        blob = json.dumps(_payload, ensure_ascii=False, default=str)
        # DRY_RUN 只写 history，不覆盖 retrain:last（避免 dry-run 污染最新真源）
        r.lpush("hcm:ai:retrain:history", blob)
        r.ltrim("hcm:ai:retrain:history", 0, 49)
        if DRY_RUN:
            log("[record] DRY_RUN=True -> skip hcm:ai:retrain:last (history only)")
        else:
            r.set("hcm:ai:retrain:last", blob)
            log("[record] retrain run saved to Redis hcm:ai:retrain:last")
    except Exception as e:
        log(f"[record] Redis save FAILED (non-fatal, see auto_retrain.log): {e}")


def record_shadow_eval(payload: dict):
    """持久化 champion vs challenger 影子对比结果（P1-O5 2026-08-22）。

    每轮重训的 shadow_eval 不再只打日志（事后无法回看在线表现），
    落 Redis hcm:ai:shadow:last + 保留近 50 条 hcm:ai:shadow:history，
    形成「候选 vs 现役」随时间轴的可回看记录，支持模型版本归因。
    零 schema 风险（复用 Redis，不碰 PG 表结构）。失败仅记日志。
    """
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5, decode_responses=True)
        _payload = dict(payload)  # 不污染调用方 payload
        _payload["dry_run"] = bool(DRY_RUN)
        _payload.setdefault("at", datetime.now(timezone.utc).isoformat())
        blob = json.dumps(_payload, ensure_ascii=False, default=str)
        # DRY_RUN 只写 history，不覆盖 shadow:last（避免 dry-run 污染最新真源）
        r.lpush("hcm:ai:shadow:history", blob)
        r.ltrim("hcm:ai:shadow:history", 0, 49)
        if DRY_RUN:
            log("[shadow-record] DRY_RUN=True -> skip hcm:ai:shadow:last (history only)")
        else:
            r.set("hcm:ai:shadow:last", blob)
            log("[shadow-record] shadow eval saved to Redis hcm:ai:shadow:history")
    except Exception as e:
        log(f"[shadow-record] Redis save FAILED (non-fatal): {e}")


# ── P3 共享工具 ──────────────────────────────────────────────────────────
def _safe_json(x):
    """inference_log 的 JSON 列 psycopg2 已解析为 dict；str 才需 loads。"""
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    try:
        return json.loads(x)
    except Exception:
        return {}


def safe_auc(y, p):
    """LightGBM predict 返回 raw 或概率，统一做 AUC；形状异常或单类→None。"""
    if _roc_auc is None or y is None or p is None:
        return None
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float).ravel()
    if y.size < 30 or len(set(y.tolist())) < 2 or p.size != y.size:
        return None
    try:
        return float(_roc_auc(y, p))
    except Exception:
        return None


def fetch_recent(window_hours: float = 24.0):
    """读近窗口 inference_log，返回 (ai_scores, features_list, passed_list)。"""
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    conn = psycopg2.connect(DB_URL_DEFAULT, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ai_score, features, snapshot "
                "FROM hcm_ai.inference_log WHERE created_at >= %s",
                (since,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    ai, feats, passed = [], [], []
    for a, fj, sj in rows:
        ai.append(a)
        feats.append(_safe_json(fj))
        s = _safe_json(sj)
        passed.append(1.0 if s.get("passed") else 0.0)
    return ai, feats, passed


# 【2026-08-31 加固】live 基线健康阈值：恒定(唯一值<=1)特征占比超过此值即判定
# 输入退化，拒绝 re-pin 并保留旧基线（完整教训见 build_live_baseline 内注释）。
# 参考：健康状态下恒定占比约 0.06~0.14(实测)；2026-08-30 故障期高达 25/39≈0.64。
LIVE_BASELINE_MAX_CONST_RATIO = 0.30

# 【2026-09-08 审计修复 P1】影子验收的绝对 AUC 下限（见 shadow_compare 内注释）：
# 仅有"相对冠军"判据会让模型族自洽劣化，必须叠加"候选自身达标"下限。
SHADOW_MIN_ABS_AUC = 0.55

# 【治本 2026-09-09】真实结果评估集的留出比例：必须与 train_signal_quality.py 的
# --test-ratio **保持一致**。auto_retrain 调用训练时未传该参数(:1137)，故取其默认 0.2。
# ⚠️ 契约：两者不一致 → 评估集落入候选训练集 → 候选 AUC 变成"样本内"而虚高
#    （实测 25% 时候选 0.821 vs 训练自报 test 0.681，即此偏差），验收失去意义。
#    若今后给训练传了 --test-ratio，此常量必须同步修改。
REAL_EVAL_HOLDOUT_RATIO = float(os.environ.get("REAL_EVAL_HOLDOUT_RATIO", "0.20"))

# 【2026-09-08 审计修复 P1】漂移触发的 fail_streak 熔断阈值：
# 连续 N 轮重训都被影子验收拒收时，说明"重训也换不出更好的模型"（当前
# fail_streak=9：样本/标签/特征侧的问题未解决，重训必然被拒）。此时继续按 PSI
# 漂移触发重训 = 空转 churn：每轮全量 build_labels→features→train→裁判耗时数小时、
# 抢占 CPU，且结果必然 adopted=false。达到阈值后降级为"仅告警不重训"，
# 待 fail_streak 被成功采纳重置（或人工清零）后自动恢复。
RETRAIN_FAIL_STREAK_HALT = 3


def build_live_baseline(window_days: float = 1.0) -> dict | None:
    """从 inference_log 近窗口特征构建 live 群体基线(deciles)→ models/live_baseline.json。

    P3-B 根治：PSI 基线改取真实生产推理群体，而非训练集(训练子集≠live 群体
    导致永久误触发+每小时 churn)。重训切换后调用本函数 re-pin，使 PSI 归零、
    仅未来真漂移才再触发。2026-08-22。
    窗口默认 24h：捕捉当前(post-shift)regime，与 _compute_trigger 的 24h 比较窗口
    对齐→PSI≈0、停 churn；更长窗口会混入 pre-shift 旧 regime 致误触发。
    """
    ai, feats, passed = fetch_recent(window_days * 24.0)
    if len(feats) < 50:
        log(f"[live-baseline] insufficient samples ({len(feats)}), skip")
        return None
    try:
        from _model_feature_cols import MODEL_FEATURE_COLS
        present = set()
        for _f in feats[:200]:
            present |= set(_f.keys())
        cols = [c for c in MODEL_FEATURE_COLS if c in present] or list(present)
    except Exception:
        present = set()
        for _f in feats[:200]:
            present |= set(_f.keys())
        cols = list(present)
    fdf = pd.DataFrame([{c: f.get(c, float("nan")) for c in cols} for f in feats])

    # 【2026-08-31 加固】拒绝用退化数据 re-pin 基线（重要教训，务必读完）。
    # 背景：2026-08-30 因桥时区偏移在休市重启被探测成 +0h → K 线停入库；
    # sidecar 用同一批陈旧 K 线计算特征 → 全部特征恒定（实测 2026-08-30
    # 13:00~21:00 期间 adx_14 恒为 44.80，该时段 100% 行命中）。
    # 本函数于是"忠实"记录下 25/39 特征恒定，而这份故障期快照随后被当作
    # train/serve skew 的判据使用，导致误删 7 个生产实际有真实取值的特征
    # （ds_fake_prob/ds_sl_coeff/ds_continuity/dev_z_ema200/extreme_reversal/
    # trend_aligned/entry_atr_ratio），质量头 AUC 因此被压到 0.37。
    # 教训：live_baseline 只反映"构建当时"的生产状态。生产故障期它会如实失真，
    # 因此**不能直接当作"某特征在生产是否有信息"的判据**；判断特征是否有信息，
    # 应直接读 sidecar 发布的 lm_features（真实入模值）交叉验证。
    # 故此处增加健康校验：恒定特征占比超阈值 → 告警并跳过 re-pin，保留旧基线。
    try:
        _n_const = sum(1 for c in cols if fdf[c].nunique(dropna=True) <= 1)
        const_ratio = _n_const / max(1, len(cols))
    except Exception:
        _n_const, const_ratio = 0, 0.0
    if const_ratio > LIVE_BASELINE_MAX_CONST_RATIO:
        log(f"[live-baseline] DEGRADED INPUT: {_n_const}/{len(cols)} features constant "
            f"(ratio={const_ratio:.3f} > {LIVE_BASELINE_MAX_CONST_RATIO}) — "
            f"skip re-pin, keep previous baseline. "
            f"This usually means the production pipeline is broken (e.g. K-line stalled).")
        return None

    bl = build_live_baseline_from_features(fdf, cols)
    save_live_baseline(bl)
    log(f"[live-baseline] built from {len(feats)} samples, "
        f"{len(bl['features'])} features -> {LIVE_BASELINE_PATH}")
    return bl


def _current_model_path() -> str | None:
    """读现役 champion 模型路径（Redis hcm:config:v2 > PG hcm_config.metadata）。"""
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5, decode_responses=True)
        v = r.hget("hcm:config:v2", "ai.lm.model_path")
        if v:
            return v
    except Exception:
        pass
    try:
        import psycopg2
        conn = psycopg2.connect(DB_URL_DEFAULT, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_value FROM hcm_config.metadata WHERE config_key='ai.lm.model_path'")
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
        finally:
            conn.close()
    except Exception:
        pass
    return None


def _compute_trigger(baseline, feat_cols, ai, feats, passed):
    """返回 (psi_dict, collapse_bool, triggered_bool)。"""
    fdf = pd.DataFrame([{c: f.get(c, float("nan")) for c in feat_cols} for f in feats])
    psi = compute_psi_batch(fdf, baseline, feat_cols)
    arr = np.asarray([a for a in ai if a is not None], dtype=float)
    edges = list(range(0, 101, 5))
    edges.append(100)
    counts, _ = np.histogram(arr, bins=edges)
    collapse = (counts.max() / max(1, int(counts.sum()))) > CALIB_COLLAPSE_PCT
    # 【2026-08-24 修复】触发逻辑根治：
    #   (1) 豁免高波动/非平稳环境特征（如 event_proximity_min，PSI 恒虚高），
    #       排除其干扰后重算 max/mean——否则该特征 psi=0.7 制造"重度漂移"假象。
    #   (2) 原条件 max>0.25 AND mean>0.25 只认"普遍漂移"，单一特征重度漂移
    #       会被其它 32 个正常特征的均值稀释而漏掉（日志实证 psi_max=0.701 未触发）。
    #       现补充单特征重度漂移判定：排除豁免后任一特征 psi>PSI_HARD_TRIGGER → 触发。
    per_feat = psi.get("per_feature", {})
    eval_feats = {k: v for k, v in per_feat.items() if k not in PSI_EXEMPT_FEATURES}
    if eval_feats:
        _max = max(eval_feats.values())
        _mean = sum(eval_feats.values()) / len(eval_feats)
        _n_hard = sum(1 for v in eval_feats.values() if v > PSI_HARD_TRIGGER)
    else:
        _max, _mean, _n_hard = 0.0, 0.0, 0
    triggered = collapse or (
        _max > PSI_TRIGGER and _mean > PSI_TRIGGER) or (_n_hard >= 1)
    # 观测一致性：psi.max/mean/drifted 用排除豁免特征后的统计，避免日志/报表
    # 仍显示豁免特征（如 event_proximity_min）制造的重度漂移假象。
    psi = dict(psi, max=_max, mean=_mean,
               drifted=[k for k in psi.get("drifted", []) if k not in PSI_EXEMPT_FEATURES])
    return psi, collapse, triggered


# ── P3-A 监控报表联动（每轮守护落库四视图报表到 Redis）────────────────────
def run_monitor_report(window_hours: float = 24.0) -> bool:
    """P3-A 联动：每轮守护调用 monitoring_report.py 落库四视图监控报表到 Redis。

    只读聚合 inference_log + 写 Redis 监控键（不写 PG / 不改配置 / 不切模型），
    与 daemon 其他步骤同属非致命（失败仅记日志，不卡死主链路）。
    落库后前端 /signal-tower/model-monitor 经 /api/v1/ai/report/monitor 展示。
    2026-08-22 接入守护循环。
    """
    rc, out, err = run([PY, "monitoring_report.py", "--window-hours", str(window_hours)], timeout=120)
    if rc != 0:
        log(f"[monitor-report] FAILED rc={rc}: {err[-500:]}")
        return False
    return True


# ── P3-D 链动：HEXP 参数变更 → re-pin PSI 基线（O7 2026-08-22）────────────────
# 背景：调 HEXP 极值参数(k_extreme/mm_retreat_min 等)会改变 hexp 快照 → 改变
#   ai 特征分布 → feature_baseline.json 失配 → 守护每轮误触发重训(churn)。
# 方案：扫描 Redis hcm:config:version 里 hexp.* 前缀时间戳的最大值，与上次记录
#   比较；变更即 re-pin live_baseline（build_live_baseline），使 PSI 归零、停 churn，
#   并把重训交由 PSI 触发机制决定（不因参数变更强制重训，避免过度）。
_HEXP_CFG_TS_REDIS_KEY = "hcm:ai:retrain:hexp_cfg_ts"


def _read_hexp_config_max_ts() -> float | None:
    """扫描 Redis hcm:config:version 中所有 hexp.* 键的最大时间戳。"""
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5, decode_responses=True)
        raw = r.hgetall("hcm:config:version")
        max_ts = None
        for k, v in raw.items():
            if isinstance(k, bytes):
                k = k.decode("utf-8", "ignore")
            if str(k).startswith("hexp."):
                try:
                    f = float(v)
                    max_ts = f if max_ts is None or f > max_ts else max_ts
                except (TypeError, ValueError):
                    continue
        return max_ts
    except Exception:
        return None


def _persist_hexp_cfg_ts(ts: float):
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5, decode_responses=True)
        r.set(_HEXP_CFG_TS_REDIS_KEY, str(ts))
    except Exception:
        pass


def _load_hexp_cfg_ts() -> float | None:
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5, decode_responses=True)
        v = r.get(_HEXP_CFG_TS_REDIS_KEY)
        return float(v) if v else None
    except Exception:
        return None


def hexp_config_link(force_repin: bool = False) -> bool:
    """HEXP 参数变更 → re-pin live PSI 基线（O7 链动）。

    返回是否触发了 re-pin。首次运行（无历史记录）→ 直接 pin，不视为变更。
    force_repin=True（--build-live-baseline）→ 无条件重 pin（供调参后手动调用）。
    失败/无 Redis 时不抛错（非致命），返回 False。
    """
    try:
        cur_ts = _read_hexp_config_max_ts()
        if cur_ts is None:
            log("[hexp-link] cannot read hexp config ts (redis?)")
            return False
        prev_ts = _load_hexp_cfg_ts()
        _persist_hexp_cfg_ts(cur_ts)
        if force_repin:
            log(f"[hexp-link] force re-pin live baseline (current hexp cfg ts={cur_ts:.0f})")
            return build_live_baseline() is not None
        if prev_ts is None:
            log(f"[hexp-link] first run, pin hexp cfg ts={cur_ts:.0f} (no repin)")
            return False
        if abs(cur_ts - prev_ts) > 1e-6:
            log(f"[hexp-link] HEXP 参数变更检测到：ts {prev_ts:.0f}→{cur_ts:.0f} "
                f"→ re-pin live baseline（PSI 归零，避免 churn）")
            return build_live_baseline() is not None
        return False
    except Exception as e:
        log(f"[hexp-link] error (non-fatal): {e}")
        return False


# ── P3-B 数据驱动重训触发 ─────────────────────────────────────────────────
def monitor_and_trigger(use_deepseek: bool = True) -> bool:
    """每轮 daemon 先算 PSI/校准坍缩/regime，触发则调 retrain_once()（路径A护栏+切换）。

    触发条件（任一项）：
      - PSI>PSI_TRIGGER 且均值>PSI_TRIGGER（特征分布持续漂移）
      - 置信分布坍缩（单 ai_score 箱占比>CALIB_COLLAPSE_PCT）
    独立于 24h 定时；零新增生产写路径（仍走 switch_model 双写+PUB）。
    """
    baseline = load_live_baseline() or load_baseline()
    feat_cols = baseline.get("features", [])
    ai, feats, passed = fetch_recent(24.0)
    if len(feats) < 50:
        log("[trigger] insufficient recent samples, skip")
        return False
    psi, collapse, triggered = _compute_trigger(baseline, feat_cols, ai, feats, passed)
    log(f"[trigger] psi_max={psi['max']:.3f} psi_mean={psi['mean']:.3f} "
        f"collapsed={collapse} drifted={psi['drifted']} -> triggered={triggered}")
    # 【2026-09-08 审计修复 P1】退化/常量特征告警：这类特征 PSI 恒 0，此前被
    # 静默当作"无漂移"（健康），实为"线上无信息"（如 tmf_* 曾长期恒 0）。
    _degen = psi.get("degenerate", [])
    if _degen:
        log(f"[WARN] 退化/常量特征 {len(_degen)} 个（线上无信息，PSI=0 不代表健康）: "
            f"{_degen}")
    if triggered:
        # 【2026-09-08 审计修复 P1】fail_streak 熔断（见 RETRAIN_FAIL_STREAK_HALT 注释）：
        # 漂移可以是真的（实测基线重建后 psi_max 仍 1.40、22 维漂移），但若连续多轮
        # 重训都被影子验收拒收，再触发重训只是空转。达到阈值 → 本轮仅告警不重训。
        _streak = 0
        try:
            import redis as _rlib
            _rc = _rlib.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=3,
                              decode_responses=True)
            _streak = int(float(_rc.get("hcm:ai:retrain:fail_streak") or 0))
        except Exception as _se:
            log(f"[trigger] fail_streak 读取失败({_se})，按 0 处理")
        if _streak >= RETRAIN_FAIL_STREAK_HALT:
            log(f"[trigger] 漂移确认，但 fail_streak={_streak} >= "
                f"{RETRAIN_FAIL_STREAK_HALT} → 本轮**不重训**（避免空转 churn），"
                f"仅告警。待新模型通过影子验收或人工重置 fail_streak 后自动恢复。")
            return triggered
        log("[trigger] condition met -> launching retrain_once()")
        retrain_once(use_deepseek=use_deepseek)
    return triggered


# ── P3-C 灰度对比（champion/challenger）──────────────────────────────────
def fetch_real_eval_set(holdout_ratio: float = REAL_EVAL_HOLDOUT_RATIO,
                        min_samples: int = 100):
    """【治本 2026-09-09】真实成交结果评估集：join(features.csv, labels.csv) → y=真实胜负。

    留出比例必须与训练一致（见 REAL_EVAL_HOLDOUT_RATIO 契约注释）：取时间序最近
    holdout_ratio 切片 == train_signal_quality.py 的测试段，从而对**冠军与候选都是
    样本外**，比较才公平（候选只在其前 1-ratio 段上训练过）。

    背景（空转根因）：原影子验收用 HEXP `passed` 代理标签，而质量模型特征含 HEXP
    自身输出 verdict → "用答案考答案"循环自证：冠军恒赢(0.82)、候选恒输(0.69)，
    连续 13 次拒收、模型永不更新（churn）。既有 TODO 亦指向此。

    治本：改用 labels.csv 的**真实 R 倍数**作标签（R>0 为正例）——与训练目标
    （真实成交结果）对齐；features.csv 按 signal_id 提供特征。取时间序最近
    holdout_ratio 切片作近似留出集，降低与训练集重叠风险。

    样本不足/文件缺失/异常 → 返回 None（调用方回退 passed 代理并标注 label_src）。
    """
    try:
        _tools = os.path.dirname(os.path.abspath(__file__))
        fpath = os.path.join(_tools, "_artifacts", "features.csv")
        lpath = os.path.join(_tools, "_artifacts", "labels.csv")
        if not (os.path.exists(fpath) and os.path.exists(lpath)):
            log("[real-eval] features/labels csv missing -> fallback")
            return None
        fdf = pd.read_csv(fpath)
        ldf = pd.read_csv(lpath)
        if "signal_id" not in fdf.columns or "signal_id" not in ldf.columns:
            return None
        keep = ["signal_id"] + [c for c in ("R", "label", "created_at") if c in ldf.columns]
        m = fdf.merge(ldf[keep], on="signal_id", how="inner")
        if m.empty:
            return None
        # 标签优先级：**label(真实胜负) 优先**。
        # 【实测订正 2026-09-09】labels.csv 的 R 列恒 >0（3721/3721 全为正、R<=0 计数 0），
        # 是无符号量（非已实现盈亏），不能作二分类标签 —— 首次验证即因 y 全 1 导致
        # set(y) 单值、AUC 无法计算而回退。label 才是真实成交结果(1.0=win / 0.0=loss)。
        # R 仅作兜底（且需其确实存在正负两侧，否则下方 set(y)<2 仍会拦截）。
        if "label" in m.columns and pd.to_numeric(m["label"], errors="coerce").notna().any():
            m = m.copy()
            m["_lb"] = pd.to_numeric(m["label"], errors="coerce")
            m = m[m["_lb"].notna()]
            _src = "label_winloss"
        elif "R" in m.columns and pd.to_numeric(m["R"], errors="coerce").notna().any():
            m = m.copy()
            m["_lb"] = pd.to_numeric(m["R"], errors="coerce")
            m = m[m["_lb"].notna()]
            _src = "R"
        else:
            return None
        # 时间序取最近切片（近似留出集）
        if "created_at" in m.columns:
            m["_ts"] = pd.to_datetime(m["created_at"], utc=True, errors="coerce")
            m = m.sort_values("_ts")
        n_hold = max(min_samples, int(len(m) * holdout_ratio))
        if len(m) > n_hold:
            m = m.iloc[-n_hold:]
        y = ((m["_lb"] > 0) if _src == "R" else m["_lb"]).astype(float).to_numpy()
        if len(y) < min_samples or len(set(y.tolist())) < 2:
            log(f"[real-eval] insufficient/degenerate labels (n={len(y)} "
                f"classes={len(set(y.tolist()))}) -> fallback")
            return None
        m = m.drop(columns=[c for c in ("_ts", "_lb") if c in m.columns])
        log(f"[real-eval] real-outcome eval set n={len(m)} pos_rate={y.mean():.3f} "
            f"src={_src}")
        return m.to_dict("records"), y
    except Exception as e:
        log(f"[real-eval] build failed: {e} -> fallback")
        return None


def shadow_eval(champion_path: str, candidate_path: str, feats, y,
                label_src: str = "passed_proxy") -> dict:
    """候选 vs 现役 champion 影子对比：在给定评估集上比较 AUC。

    y 的语义由 label_src 标明（治本后优先真实成交结果）：
      - "real_R"        ：labels.csv 真实 R 倍数(R>0)，与训练目标一致（推荐）
      - "passed_proxy"  ：HEXP 闸门结果代理（含 verdict 特征→循环自证，仅兜底）

    返回 {available, auc_champ, auc_cand, adopt, label_src}。adopt=候选 AUC 不劣于
    champion（允许 -0.02 容差）且不低于绝对下限。库缺失→available=False 且 adopt=True
    （放行，交由既有 DS/本地护栏兜底）。
    """
    if lgb is None or _roc_auc is None:
        return {"available": False, "adopt": True, "reason": "libs_unavailable",
                "label_src": label_src}
    try:
        champ = lgb.Booster(model_file=champion_path)
        cand = lgb.Booster(model_file=candidate_path)
    except Exception as e:
        return {"available": False, "adopt": True, "reason": f"load_err:{e}",
                "label_src": label_src}
    cfeats = champ.feature_name()
    nfeats = cand.feature_name()
    Xc = pd.DataFrame(feats).reindex(columns=cfeats, fill_value=0.0)
    Xn = pd.DataFrame(feats).reindex(columns=nfeats, fill_value=0.0)
    y = np.asarray(y, dtype=float)
    if len(y) < 30 or len(set(y.tolist())) < 2:
        return {"available": True, "adopt": True, "reason": "insufficient_labels",
                "label_src": label_src}
    p_champ = np.asarray(champ.predict(Xc), dtype=float).ravel()
    p_cand = np.asarray(cand.predict(Xn), dtype=float).ravel()
    auc_champ = safe_auc(y, p_champ)
    auc_cand = safe_auc(y, p_cand)
    # 【2026-09-08 审计修复 P1】追加绝对下限，根除"与冠军相当即可上线"的自洽劣化：
    # 原判据仅为 `auc_cand >= auc_champ - 0.02`，是**相对**比较 —— 当 champion 自身
    # 退化时（如 0.55），候选 0.54 也会被采纳，模型族可一代代缓慢劣化而不触发任何
    # 拒收；且 y 用的是 hexp 闸门结果(passed)作**代理标签**，只证明"与 HEXP 一致"，
    # 不证明对行情有效。现叠加绝对下限：候选必须同时"不显著差于冠军"且"本身达标"。
    # 注：实测当前 champion≈0.606 / cand≈0.549 < 0.55 → 与修复前同为拒收，行为不变。
    # 【2026-09-09 治本 DONE】原 TODO「把 y 换成真实 R 触达标签」已实现：
    #   由 fetch_real_eval_set() 从 labels.csv 取真实 R(R>0) 作标签，调用点优先用它；
    #   仅在真实集不可用(样本不足/文件缺失)时回退 passed 代理，并以 label_src 标注，
    #   使"验收是否基于真实结果"在 hcm:ai:shadow:last / retrain:last 中可审计。
    adopt = (
        auc_cand is not None and auc_champ is not None
        and auc_cand >= auc_champ - 0.02
        and auc_cand >= SHADOW_MIN_ABS_AUC
    )
    return {"available": True, "auc_champ": auc_champ, "auc_cand": auc_cand,
            "adopt": adopt, "min_abs_auc": SHADOW_MIN_ABS_AUC,
            "label_src": label_src,
            "n_eval": int(len(y)),
            "reason": ("ok" if adopt else
                       ("cand_auc_below_abs_floor" if (auc_cand is not None
                                                       and auc_cand < SHADOW_MIN_ABS_AUC)
                        else "cand_worse_than_champ"))}


def _check_trigger_only():
    """--check-trigger：仅计算并打印触发条件，不重训（安全验证 P3-B）。"""
    baseline = load_live_baseline() or load_baseline()
    feat_cols = baseline.get("features", [])
    ai, feats, passed = fetch_recent(24.0)
    log(f"[check] recent samples={len(feats)}")
    if len(feats) < 50:
        log("[check] insufficient samples"); return
    psi, collapse, triggered = _compute_trigger(baseline, feat_cols, ai, feats, passed)
    print(json.dumps({
        "psi_max": round(psi["max"], 4), "psi_mean": round(psi["mean"], 4),
        "n_drifted": len(psi["drifted"]), "calib_collapse": bool(collapse),
        "triggered": bool(triggered),
    }, ensure_ascii=False, indent=2))


def _run_shadow_eval():
    """--shadow-eval：最新候选 vN vs 现役 champion 影子对比，不切换（安全验证 P3-C）。"""
    champ = _current_model_path()
    v = next_model_version() - 1
    cand = os.path.join(MODELS_DIR, f"lgbm_quality_v{v}.txt")
    if not champ or not os.path.exists(cand):
        log(f"[shadow-eval] champion={champ} candidate(v{v})={cand} missing"); return
    ai, feats, passed = fetch_recent(24.0)
    # 【治本 2026-09-09】与 retrain_once 同口径：优先真实成交结果标签。
    _real = fetch_real_eval_set()
    if _real is not None:
        feats, passed = _real
        _src = "real_outcome"
    else:
        if not feats:
            log("[shadow-eval] no recent features"); return
        _src = "passed_proxy"
    se = shadow_eval(champ, cand, feats, passed, label_src=_src)
    print(json.dumps({"champion": champ, "candidate": cand, "shadow": se},
                     ensure_ascii=False, indent=2))


# ── 主流程 ────────────────────────────────────────────────────────────────
def retrain_once(use_deepseek: bool = True, force: bool = False) -> dict:
    """执行一轮完整重训闭环，返回结果摘要。

    【2026-09-09 修复·堵塞熔断旁路】fail_streak 熔断统一前置到本函数：
    此前熔断仅作用于 monitor_and_trigger（PSI 数据驱动触发路径，:874-892），
    而 _tmf_then_retrain.py 在 TimesFM 抽取完成后经 `auto_retrain.py --once`
    （main:1239）以及 daemon 定时分支（main:1265）都直接调用本函数 → 完全绕过
    熔断。实测：fail_streak=12（远超 RETRAIN_FAIL_STREAK_HALT=3）时仍在
    2026-09-08T21:44 训练出 v96，版本继续堆积、CPU 空转。
    现统一在此判定：fail_streak >= RETRAIN_FAIL_STREAK_HALT 且非 force → 跳过
    训练（仅告警，避免 churn）；人工确实要强制重训时用 --force 覆盖。
    """
    if not force:
        try:
            import redis as _rlib
            _rc = _rlib.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=3,
                              decode_responses=True)
            _streak = int(float(_rc.get(FAIL_STREAK_KEY) or 0))
        except Exception as _se:
            log(f"[halt] fail_streak 读取失败({_se})，按 0 处理")
            _streak = 0
        if _streak >= RETRAIN_FAIL_STREAK_HALT:
            log(f"[halt] fail_streak={_streak} >= {RETRAIN_FAIL_STREAK_HALT} "
                f"→ 跳过本轮重训（空转防护）；人工强制请加 --force")
            return {"ok": False, "stage": "fail_streak_halt", "fail_streak": _streak}
    v = next_model_version()
    model_out = os.path.join(MODELS_DIR, f"lgbm_quality_v{v}.txt")
    calib_out = os.path.join(MODELS_DIR, f"calib_v{v}.pkl")
    labels_csv = os.path.join(ARTIFACTS, "labels.csv")
    features_csv = os.path.join(ARTIFACTS, "features.csv")

    log(f"=== retrain round: target version v{v} ===")

    # 1) 标签
    # 2026-08-21: 加 --ds-calibrate，使 build_labels 计算 ds_calib_weight
    # (DeepSeek 票近邻匹配加权 1.5/0.5)，供 train_signal_quality 作 sample_weight。
    # 修复前未传此参数 → ds_calib_weight 恒 1.0 → DeepSeek 完全未参与训练加权(断链)。
    # 【2026-08-31 扩样本】原单模式 HEXP:% 仅约 864 条信号、有效样本 307，
    # 不足以稳定训练（质量头 AUC 长期 ~0.49、测试集仅 62 条噪声主导）。
    # 并入同期的 live_override（indicator_values 口径已验证与 HEXP 一致）后
    # 有效样本 857、测试集 172、质量头 AUC 提升至 0.5917。
    # 与 build_labels.py / quality_features.py 的逗号分隔多模式支持配套
    # （单模式行为向后兼容）。
    rc, out, err = run([PY, "build_labels.py", "--out", labels_csv,
                        "--mode", "HEXP:%,live_override",
                        "--ds-calibrate"])
    if rc != 0:
        log(f"[ABORT] build_labels failed: {err[-500:]}")
        return {"ok": False, "stage": "build_labels", "error": err[-500:]}

    # 2) 特征（自动带 ds_* DeepSeek 特征 = 路径 C）
    # 【2026-09-08 审计修复 P0】训练口径必须与线上推理严格一致（train-serve skew）。
    # quality_scorer 侧仅当 `ai.lm.period_match == "align_m5"` 才启用 M5 同源重算
    # （adx_14/rsi_14/macd/atr_14/h1_adx/h1_trend_strength）；此前本处固定传
    # --period-align m5，而线上该键无种子 → 默认 none → 训练按 M5 重算、线上读
    # hexp 快照原值，4~6 个核心技术指标两套口径，模型学到线上不存在的分布。
    # 现按线上实际配置自动对齐（线上改 align_m5 后训练自动跟随，反之亦然）。
    _pm = "(unknown)"
    _period_align = "none"
    try:
        import redis as _rlib
        _rc = _rlib.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5,
                          decode_responses=True)
        _pm = str(_rc.hget("hcm:config:v2", "ai.lm.period_match") or "").strip().lower()
        _period_align = "m5" if _pm == "align_m5" else "none"
    except Exception as _e:
        log(f"[warn] 读取 ai.lm.period_match 失败({_e}) → 回退 none（与线上默认一致）")
    log(f"[align] ai.lm.period_match={_pm or '(empty)'} → quality_features "
        f"--period-align {_period_align}（与推理口径对齐）")
    rc, out, err = run([PY, "quality_features.py", "--out", features_csv,
                        "--mode", "HEXP:%,live_override", "--period-align", _period_align])
    if rc != 0:
        log(f"[ABORT] quality_features failed: {err[-500:]}")
        return {"ok": False, "stage": "quality_features", "error": err[-500:]}

    # 3) 训练
    rc, out, err = run(
        [PY, "train_signal_quality.py", "--labels", labels_csv, "--features", features_csv,
         "--model", model_out, "--calib", calib_out, "--outdir", ARTIFACTS],
        timeout=900,
    )
    if rc != 0:
        log(f"[ABORT] train failed: {err[-800:]}")
        return {"ok": False, "stage": "train", "error": err[-800:]}

    # 【2026-08-24 修复】train 的 ds_diag(DeepSeek 特征吸收率)打印到 stderr，而
    # auc/samples 在 stdout——此前只用 stdout 解析 → ds_nonzero_ratio 恒 None →
    # DeepSeek 裁判拿不到"模型 ds 吸收率"信息而保守回滚。合并两流再解析。
    _all_out = out + "\n" + err
    auc = parse_auc(_all_out)
    samples = count_samples(_all_out)
    ds_ratio = ds_nonzero_ratio(_all_out)
    log(f"[train] done: auc={auc} samples={samples} ds_nonzero_ratio={ds_ratio}")

    # 【阶段 2·健康判定 2026-08-29】方向头 / 买点头**独立**健康判定（用户决策 1/2/4）：
    #   - 阈值 0.55（HEAD_METRIC_MIN）
    #   - 不达标 → 立即校准，不禁用该头（决策 2）
    #   - 走本地阈值，不交 DeepSeek 裁判（决策 4）
    # 判定与质量头**解耦**：单头不达标**不阻塞**质量头采纳（避免一损俱损），
    # 仅记录 + 标记 recalib_required，供调用方触发立即重训/校准。
    # 指标缺失（None，如样本不足未训练该头）按"不阻塞"处理。
    dir_hit = parse_dir_hit(_all_out)
    entry_auc = parse_entry_auc(_all_out)
    dir_ok = (dir_hit is None) or (dir_hit >= HEAD_METRIC_MIN)
    entry_ok = (entry_auc is None) or (entry_auc >= HEAD_METRIC_MIN)
    recalib_required = not (dir_ok and entry_ok)
    head_health = {
        "dir_hit": dir_hit,
        "dir_ok": dir_ok,
        "entry_auc": entry_auc,
        "entry_ok": entry_ok,
        "threshold": HEAD_METRIC_MIN,
        "recalib_required": recalib_required,
    }
    log(f"[head-health] dir_hit={dir_hit} ok={dir_ok} | entry_auc={entry_auc} "
        f"ok={entry_ok} | threshold={HEAD_METRIC_MIN}")
    if recalib_required:
        log("[head-health] 单头不达标 → 按用户决策 2 立即校准（不禁用该头，"
            "不阻塞质量头采纳）")

    payload = {
        "model_version": f"v{v}",
        "auc": auc,
        "samples": samples,
        "ds_nonzero_ratio": ds_ratio,
        "baseline_win_rate": None,  # train 输出含，按需扩展解析
        # 阶段 2：三头健康指标（供 DeepSeek 裁判参考 + 本地决策 + 落库追溯）
        "head_health": head_health,
    }

    # 4) 决策：DeepSeek 裁判（路径 B）或本地 AUC 护栏
    # 从 PG 配置中心读取 DeepSeek 真源 key/api_base/model（与系统其他模块一致），
    # 覆盖模块默认，使守护无需手动填 key 即可链动裁判。
    ds_cfg = load_ds_config_from_pg()
    ds_key = ds_cfg["api_key"] or DS_KEY
    ds_api = ds_cfg["api_base"] or DS_API_DEFAULT
    ds_model = ds_cfg["model"] or DS_MODEL_DEFAULT
    log(f"[ds-config] key_present={bool(ds_key)} api_base={ds_api[:24]}... model={ds_model}")

    decided_adopt = False
    judge = {"decision": "local_fallback", "reason": "disabled"}
    if use_deepseek:
        judge = deepseek_judge(ds_api, ds_key, payload, model=ds_model)
        if judge.get("decision") == "adopt":
            decided_adopt = True
        elif judge.get("decision") == "rollback":
            decided_adopt = False
        else:  # local_fallback
            decided_adopt = (auc is not None and auc >= LOCAL_AUC_ADOPT_MIN
                             and samples >= MIN_SAMPLES_FOR_SWITCH)
    else:
        decided_adopt = (auc is not None and auc >= LOCAL_AUC_ADOPT_MIN
                         and samples >= MIN_SAMPLES_FOR_SWITCH)

    log(f"[judge] decision={judge.get('decision')} reason={judge.get('reason')} "
        f"-> adopt={decided_adopt}")

    # 5) 样本不足 → 只产模型不切（避免噪声覆盖）
    if samples < MIN_SAMPLES_FOR_SWITCH:
        log(f"[skip-switch] samples={samples} < {MIN_SAMPLES_FOR_SWITCH}，模型已产出但未切换")
        decided_adopt = False
    # 【P0-O3 2026-08-22】DeepSeek 特征非零占比硬门槛：占比过低说明模型未真正吸收
    # ds 语义，切换上线会造成"假精准"。只产模型不切（与样本不足同级别护栏）。
    if ds_ratio is not None and ds_ratio < DS_MIN_NONZERO_RATIO:
        log(f"[skip-switch] ds_nonzero_ratio={ds_ratio:.3f} < {DS_MIN_NONZERO_RATIO}，"
            f"模型实质未吸收 DeepSeek 语义，只产不切（避免假精准）")
        payload["ds_gate_blocked"] = True
        decided_adopt = False

    # 6) 切换（P3-C 灰度闸门：候选需不劣于现役 champion 才切）
    switched = False
    if decided_adopt:
        champ_path = _current_model_path()
        if champ_path and os.path.exists(champ_path) and \
                os.path.abspath(champ_path) != os.path.abspath(model_out):
            _ai_l, _f_l, _p_l = fetch_recent(24.0)
            # 【治本 2026-09-09】优先用"真实成交结果"标签验收（破除 passed 代理 +
            # verdict 特征的循环自证）；真实集不可用时回退 passed 代理并标注来源。
            _real = fetch_real_eval_set()
            if _real is not None:
                _ev_f, _ev_y = _real
                se = shadow_eval(champ_path, model_out, _ev_f, _ev_y,
                                 label_src="real_outcome")
            elif _f_l:
                se = shadow_eval(champ_path, model_out, _f_l, _p_l,
                                 label_src="passed_proxy")
            else:
                se = None
            if se is not None:
                log(f"[shadow] {se}")
                # 【P1-O5 2026-08-22】影子对比持久化：候选 vs 现役 AUC 差异 + 决定落库，
                # 形成在线表现时间线，供按版本归因/回看（不再仅打日志）。
                record_shadow_eval({
                    "at": datetime.now(timezone.utc).isoformat(),
                    "champion": os.path.basename(champ_path),
                    "candidate": os.path.basename(model_out),
                    "shadow": se,
                    "blocked": bool(se.get("available") and not se.get("adopt")),
                })
                payload["shadow"] = se
                if se.get("available") and not se.get("adopt"):
                    log("[shadow] challenger 未优于 champion -> 不切换")
                    decided_adopt = False
        if decided_adopt:
            switched = switch_model(model_out, calib_out)
            log(f"[switch] {'OK' if switched else 'FAILED'} -> {model_out}")
        else:
            log("[switch] skipped (shadow gate blocked)")

    if switched:
        try:
            build_live_baseline()
            log("[live-baseline] re-pinned after switch")
        except Exception as e:
            log(f"[live-baseline] re-pin failed (non-fatal): {e}")

    payload.update({"adopted": decided_adopt, "switched": switched, "judge": judge})

    # 【阶段 3·自愈闭环 2026-08-29】
    # 决策 5：连续失败告警（switched=True = 复活成功 → 清零；否则 +1；≥3 告警人工）
    # 决策 6：版本保留 3 版（仅切换成功后清理，避免误删 sidecar 在用的旧版）
    _redis_url = os.environ.get("REDIS_URL", "") or "redis://localhost:6379"
    _rc = None
    try:
        import redis as _r
        _rc = _r.Redis.from_url(_redis_url, socket_timeout=3)
    except Exception:
        _rc = None
    _streak = _bump_fail_streak(_rc, ok=bool(switched))
    payload["fail_streak"] = _streak
    if _streak >= FAIL_STREAK_ALERT:
        log(f"[ALERT][需人工介入] 连续 {_streak} 轮复活失败（阈值 {FAIL_STREAK_ALERT}）："
            f"stage={payload.get('stage', '-')} "
            f"judge={judge.get('decision')} reason={str(judge.get('reason'))[:160]}")
    if switched:
        cleanup_old_versions()

    record_retrain_run(payload)
    log(f"=== round done: adopted={decided_adopt} switched={switched} "
        f"fail_streak={_streak} ===")
    return payload


def _heartbeat():
    """守护存活心跳：写 Redis hcm:ai:retrain:daemon（TTL 续期）。

    前端报表据此判断守护是否在线（死守护/未启动→离线，避免误以为'没训练'）。
    每轮重训后 + 崩溃兜底都刷新，TTL 取 2 倍间隔(48h)容错。
    """
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5, decode_responses=True)
        import time as _t
        blob = json.dumps({"pid": os.getpid(), "at": _t.strftime("%Y-%m-%d %H:%M:%S")},
                          ensure_ascii=False)
        r.set("hcm:ai:retrain:daemon", blob, ex=48 * 3600)
    except Exception as e:
        log(f"[heartbeat] write failed (non-fatal): {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单次运行后退出")
    ap.add_argument("--daemon", action="store_true", help="常驻循环")
    ap.add_argument("--interval-hours", type=float, default=24.0)
    ap.add_argument("--no-deepseek", action="store_true", help="禁用 DeepSeek 裁判，纯本地 AUC 决策")
    ap.add_argument("--check-trigger", action="store_true",
                    help="仅计算 PSI/校准触发条件并打印，不重训（安全验证 P3-B）")
    ap.add_argument("--shadow-eval", action="store_true",
                    help="最新候选 vN vs 现役 champion 影子对比，不切换（安全验证 P3-C）")
    ap.add_argument("--build-live-baseline", action="store_true",
                    help="从 inference_log 重建 live 群体 PSI 基线(写 models/live_baseline.json)，不重训")
    ap.add_argument("--link-hexp", action="store_true",
                    help="单次执行 HEXP 参数变更链动检测（变更→re-pin live baseline，O7）")
    ap.add_argument("--force-repin", action="store_true",
                    help="强制 re-pin live baseline（调 HEXP 参数后手动调用，配合 --link-hexp）")
    ap.add_argument("--dry-run", action="store_true",
                    help="switch_model 只记录不切换（验证/灰度用）")
    ap.add_argument("--force", action="store_true",
                    help="绕过 fail_streak 熔断强制重训（人工介入用；默认受熔断约束）")
    args = ap.parse_args()

    if args.dry_run:
        global DRY_RUN
        DRY_RUN = True

    use_ds = not args.no_deepseek

    if args.check_trigger:
        _check_trigger_only()
        return
    if args.shadow_eval:
        _run_shadow_eval()
        return
    if args.build_live_baseline:
        build_live_baseline()
        return
    if args.link_hexp:
        # O7：单次链动检测（变更→re-pin baseline）；--force-repin 强制重 pin
        hexp_config_link(force_repin=args.force_repin)
        return
    if args.once:
        retrain_once(use_deepseek=use_ds, force=args.force)
        return

    # daemon 模式：monitor_and_trigger 高频(≤1h)查数据驱动触发，retrain_once 按 interval 定时全量
    log(f"auto_retrain daemon started: interval={args.interval_hours}h deepseek={use_ds} "
        f"dry_run={DRY_RUN}")
    loop_h = max(0.05, min(args.interval_hours, 1.0))  # 触发检查频率上限 1h
    last_sched = time.time()
    while True:
        # P3-D 链动（O7）：HEXP 参数变更 → re-pin live baseline，避免 PSI churn
        try:
            hexp_config_link()
        except Exception as e:
            log(f"[daemon] hexp_config_link crashed (non-fatal): {e}")
        try:
            monitor_and_trigger(use_deepseek=use_ds)  # P3-B 数据驱动触发
        except Exception as e:
            log(f"[daemon] monitor_and_trigger crashed (non-fatal): {e}")
        # P3-A 联动：每轮落库四视图监控报表（PSI/校准/置信/行情环境）
        try:
            run_monitor_report()
        except Exception as e:
            log(f"[daemon] monitor_report crashed (non-fatal): {e}")
        if time.time() - last_sched >= args.interval_hours * 3600.0:
            try:
                retrain_once(use_deepseek=use_ds)
            except Exception as e:
                log(f"[daemon] retrain_once crashed (non-fatal): {e}")
            last_sched = time.time()
        _heartbeat()  # 不论成功/崩溃都刷新存活标记
        time.sleep(loop_h * 3600.0)


if __name__ == "__main__":
    main()
