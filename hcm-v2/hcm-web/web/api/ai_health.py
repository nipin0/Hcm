"""AI 体检表 — 只读聚合 API（TimesFM / LightGBM 质量栈 / FSM 状态机）。

【设计定位 2026-09-17】
把"三套模型现在到底跑得怎么样"压成**一次请求**，供「数据看板 → AI 体检表」实时查看：

  GET /api/v1/ai/health?symbol=XAUUSD&hours=48

  · timesfm  —— 运行时存活 / 日调度守护 / 特征表新鲜度 / 特征退化 / 消费侧开关
  · lightgbm —— sidecar 存活 / champion 版本 / 三头健康(最近重训) / 重训守护 /
                 PSI 漂移 / 校准器 / 预测落库 / 评审器 / 闸门裁决
  · state    —— 开关(是否真驱动下单) / 模型版本 / 推理健康 / 判定率 / 类别覆盖 /
                 schema 新鲜度 / FSM 落单 / 当前实时读数

【为什么需要它（2026-09-17 复盘发现的分散隐患）】
  · TimesFM 已停用，但 `hcm_ai.timesfm_features` 仍在写**退化值**，且消费侧
    `ai.lm.tmf_asof_enabled=true` 仍开着；
  · 质量头 v109 重训 **AUC 0.516 被硬性回滚**（`fail_streak=2`）；
  · 校准器 `joined=18 < min_samples=300` ⇒ 三头全部 skipped（未校准）；
  · 状态模型 v3 `trend_init` F1 仅 **0.16**（实测决定数远少于 trend_fade）。
  这些事实分散在 **7 个 Redis 键 + 6 张表 + 若干配置键**里，肉眼巡检成本极高 ⇒ 聚合为体检表。

【铁律合规】
  - 全部**只读**：不写配置、不触发任何决策、不干预下单链路。
  - 每行都带 `source`（Redis 键 / 表 / 配置键），异常行给 `detail` 说明**判据**，不臆造结论。
  - 无数据源一律 `status="na"`（**不伪造 0、不假绿**），符合本项目"如实降级"口径。
  - 状态语义：ok=正常｜warn=需关注｜block=已影响质量｜idle=设计内停用/未启用｜na=无数据源。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

logger = logging.getLogger(__name__)

# 判定阈值集中在此，避免散落字面量（且便于日后调参只改一处）
PSI_WARN = 0.25            # PSI max 超此值 → 漂移关注（业界经验阈值）
SIDECAR_BEAT_WARN = 180.0  # sidecar 心跳超此秒数 → 关注
BEAT_WARN = 180.0          # 日调度/守护心跳阈值
INFER_FRESH_BARS = 3       # 状态机落库滞后超过 N 根 bar → 关注
DECIDE_RATE_WARN = 0.40    # 状态判定率低于此值 → 关注（状态久停）
FAIL_STREAK_WARN = 3       # 重训连续失败次数阈值
FEAT_CONST_WARN = 0.15     # 边车快照常量特征占比超此值 → 关注（特征已无区分度）

# 状态机 4 类模型输出（与 state_infer.STATE_NAMES 同序，契约）
STATE_CLASSES = ("oscillation", "trend_init", "trend_mid", "trend_fade")
# trend_init 是模型已知短板（v3 holdout F1=0.16）⇒ 实测占比过低时如实提示
TREND_INIT_SHARE_WARN = 0.08
# 【2026-09-21 修复】类别覆盖的**最小样本门槛**：`decided` 样本少于此值 ⇒ **不下"类别缺失"结论**。
# 为什么必须加（实测踩到）：窗口用**墙钟小时**，跨周末后 48h 窗口里只剩 30 根 bar
#   ⇒ 任何类"恰好没出现"都属正常，原实现却直接判 warn。
# 取值依据：4 类中占比最低的 oscillation 历史命中率 ≈ 8%（21/269，近 3 天）
#   ⇒ 要让"0 次"具备统计意义（期望 ≥ 4 次），样本量需 ≥ 50；取 48 为下限。
CLASS_COVER_MIN_DECIDED = 48

# FSM 落单口径：`orders` 表**无 magic 列**（magic 只落在 positions），故用 signals.signal_mode 判定
FSM_SIGNAL_MODES = ("state_osc", "state_trend")

# TimesFM 数值特征列（退化检测用；tmf_hist_sim 单列另有语义，单列判断）
TMF_NUM_COLS = (
    "tmf_pc00", "tmf_pc01", "tmf_pc02", "tmf_pc03", "tmf_pc04", "tmf_pc05",
    "tmf_pc06", "tmf_pc07", "tmf_trend_cont", "tmf_rev_prob", "tmf_vol_cycle",
    "tmf_mtf_resonance", "tmf_qf_width", "tmf_qf_skew", "tmf_qf_uptail",
    "tmf_qf_growth",
)


# ─────────────────────────────────────────────────────────────────────────────
# 低层 helper（照 state.py / ai_ops.py 既有写法：模块级 + 吞异常 + 降级返回 None）
# ─────────────────────────────────────────────────────────────────────────────
async def _fetch(db_pool, sql: str, *args):
    if db_pool is None:
        return None
    try:
        return await db_pool.fetch(sql, *args)
    except Exception as exc:  # noqa: BLE001
        logger.error("ai_health query failed: %s", exc)
        return None


async def _fetchrow(db_pool, sql: str, *args):
    if db_pool is None:
        return None
    try:
        return await db_pool.fetchrow(sql, *args)
    except Exception as exc:  # noqa: BLE001
        logger.error("ai_health fetchrow failed: %s", exc)
        return None


async def _redis_raw(redis_client, key: str):
    """读 Redis 原始字符串（状态键是 `k=v|k=v` 文本，不是 JSON）。"""
    if redis_client is None or not getattr(redis_client, "is_initialized", False):
        return None
    try:
        raw = await redis_client.get(key)
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        return raw
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_health redis read failed (%s): %s", key, exc)
        return None


async def _redis_json(redis_client, key: str):
    """读 Redis JSON 对象；缺失/非 dict/异常一律 None（handler 降级，不抛 500）。"""
    raw = await _redis_raw(redis_client, key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def _cfg(config_provider, key: str):
    """读配置真值（PG current_value 为唯一真值，Redis 为 L2 缓存）。"""
    if config_provider is None:
        return None
    try:
        return await config_provider.get(key)
    except Exception:  # noqa: BLE001
        return None


def _parse_kv(s: str | None) -> dict:
    """解析守护状态键 `name=X|pid=1|alive=False|...` → dict。"""
    out: dict[str, str] = {}
    if not s:
        return out
    for part in str(s).split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _num(v) -> float | None:
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool_str(v) -> bool | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "t"):
        return True
    if s in ("false", "0", "no", "f"):
        return False
    return None


def _iso(v) -> str | None:
    if v is None:
        return None
    try:
        return v.isoformat() if hasattr(v, "isoformat") else str(v)
    except Exception:  # noqa: BLE001
        return str(v)


def _lag_s(v) -> float | None:
    """DB 时间 → 距现在的秒数（naive 视为 UTC；解析失败 None）。"""
    if v is None:
        return None
    try:
        if not hasattr(v, "tzinfo"):
            return None
        dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def _human_lag(sec: float | None) -> str:
    if sec is None:
        return "—"
    if sec < 90:
        return f"{sec:.0f} 秒前"
    if sec < 5400:
        return f"{sec / 60:.0f} 分钟前"
    if sec < 172800:
        return f"{sec / 3600:.1f} 小时前"
    return f"{sec / 86400:.1f} 天前"


def _f(v, digits: int = 3) -> str:
    n = _num(v)
    return "—" if n is None else f"{n:.{digits}f}"


def _row(key: str, label: str, status: str, value: str,
         detail: str = "", source: str = "") -> dict:
    return {"key": key, "label": label, "status": status,
            "value": value, "detail": detail, "source": source}


def _rollup(rows: list[dict]) -> str:
    """区段/总体结论：block > warn > ok；idle/na 不参与升降级（设计内停用不算病）。"""
    st = {r.get("status") for r in rows}
    if "block" in st:
        return "block"
    if "warn" in st:
        return "warn"
    if "ok" in st:
        return "ok"
    return "idle"


# ─────────────────────────────────────────────────────────────────────────────
# 一、TimesFM
# ─────────────────────────────────────────────────────────────────────────────
async def _timesfm_section(db_pool, config_provider, redis_client, symbol: str, hours: int) -> dict:
    # ⚠ 【2026-09-21 TimesFM 全栈卸载 · 本函数已是死代码，不再被调用】
    #   调用点已从 `health()` 的遍历中摘除（见本文件末尾）。
    #   函数体与顶部 `TMF_NUM_COLS` 常量**保留未删**，原因：其 section 返回结构依赖本文件
    #   内部 helper，整段删除的改动风险高于收益。**确认无引用后可整体删除**（约 110 行）。
    #   归档与回滚：tools/_attic_timesfm_20260921/README.md
    rows: list[dict] = []

    # 1) 运行时存活（守护上报的权威状态键，TTL≈180s）
    raw_status = await _redis_raw(redis_client, "hcm:ai:timesfm:status")
    kv = _parse_kv(raw_status)
    alive = _bool_str(kv.get("alive"))
    running = _bool_str(kv.get("running"))
    action = (kv.get("action") or "").strip()
    beat = _num(kv.get("beat_age_s"))
    if not kv:
        rows.append(_row(
            "runtime", "运行时存活", "na", "无上报",
            "Redis 键 hcm:ai:timesfm:status 缺失或已过期（TTL≈180s）⇒ 无法判定存活。",
            "hcm:ai:timesfm:status"))
    elif action.lower() == "disabled" or alive is False:
        rows.append(_row(
            "runtime", "运行时存活", "idle",
            f"已停用（action={action or '—'} · running={running} · alive={alive}）",
            "政策停用（ai_stack_guard 的 $TIMESFM.Enabled=false）：日任务不再产出真特征。"
            "本行标 idle 而非 warn —— 停用是**设计内决策**，不算故障；"
            "真正的风险在下方的『特征退化』与『消费侧开关』两行。",
            "hcm:ai:timesfm:status"))
    else:
        ok = beat is not None and beat <= BEAT_WARN
        rows.append(_row(
            "runtime", "运行时存活", "ok" if ok else "warn",
            f"存活（running={running} · 心跳 {_human_lag(beat)}）",
            "" if ok else f"心跳 {beat}s 超过阈值 {BEAT_WARN:.0f}s ⇒ 守护可能卡住或未上报。",
            "hcm:ai:timesfm:status"))

    # 2) 日调度守护（常驻 daemon 心跳 + 上次运行日）
    daily = await _redis_json(redis_client, "hcm:ai:timesfm:daily")
    if daily is None:
        rows.append(_row(
            "daily_daemon", "日调度守护", "na", "无上报",
            "Redis 键 hcm:ai:timesfm:daily 缺失 ⇒ 无法判定守护是否在跑。",
            "hcm:ai:timesfm:daily"))
    else:
        last_run = str(daily.get("last_run") or "—")
        rows.append(_row(
            "daily_daemon", "日调度守护", "ok",
            f"{daily.get('status') or '—'}（last_run={last_run} · next_due={daily.get('next_due') or '—'}）",
            f"守护 mode={daily.get('mode') or '—'} · pid={daily.get('pid')} · 上报时间 {daily.get('at') or '—'}。"
            "（注意：daemon 存活 ≠ 有产出 —— TimesFM 停用时本行仍会是 ok，故需结合上面两行一起看。）",
            "hcm:ai:timesfm:daily"))

    # 3) 特征表新鲜度（hcm_ai.timesfm_features）
    fr = await _fetchrow(
        db_pool,
        "SELECT max(created_at) AS last, count(*) AS n "
        "FROM hcm_ai.timesfm_features "
        "WHERE symbol=$1 AND created_at > now() - make_interval(hours => $2)",
        symbol, hours)
    if fr is None:
        rows.append(_row(
            "feat_fresh", "特征表新鲜度", "na", "查询失败",
            "hcm_ai.timesfm_features 不可达（DB 未就绪或表结构不符）。",
            "hcm_ai.timesfm_features"))
    else:
        last = fr["last"]
        n = int(fr["n"] or 0)
        lag = _lag_s(last)
        if n == 0:
            rows.append(_row(
                "feat_fresh", "特征表新鲜度", "idle", f"近 {hours}h 无写入",
                "TimesFM 停用后特征表应停止写入；若此处**仍有**写入需警惕（见下行退化检测）。",
                "hcm_ai.timesfm_features"))
        else:
            bad = lag is not None and lag > 6 * 3600
            rows.append(_row(
                "feat_fresh", "特征表新鲜度", "warn" if bad else "ok",
                f"近 {hours}h {n} 行 · 最近 {_human_lag(lag)}",
                "⚠ TimesFM 已停用（见首行）却**仍在写特征** ⇒ 写入方（日任务/边车）需要确认："
                "是否在写**退化占位值**污染下游。" if n > 0 else "",
                "hcm_ai.timesfm_features"))

    # 4) 特征退化检测（真值取自最新一行 + 窗口内 DISTINCT 数）
    lat = await _fetchrow(
        db_pool,
        "SELECT * FROM hcm_ai.timesfm_features "
        "WHERE symbol=$1 ORDER BY bar_time DESC LIMIT 1",
        symbol)
    dg = await _fetchrow(
        db_pool,
        "SELECT count(DISTINCT tmf_pc00) AS d0, count(DISTINCT tmf_hist_sim) AS dh "
        "FROM hcm_ai.timesfm_features "
        "WHERE symbol=$1 AND created_at > now() - make_interval(hours => $2)",
        symbol, hours)
    if lat is None:
        rows.append(_row(
            "feat_degenerate", "特征退化检测", "na", "无样本",
            f"hcm_ai.timesfm_features 无 {symbol} 行 ⇒ 无法判定退化。",
            "hcm_ai.timesfm_features"))
    else:
        d = dict(lat)
        nz = sum(1 for c in TMF_NUM_COLS
                 if d.get(c) is not None and abs(_num(d.get(c)) or 0.0) > 1e-9)
        hist = _num(d.get("tmf_hist_sim"))
        d0 = int(dg["d0"] or 0) if dg is not None else None
        dh = int(dg["dh"] or 0) if dg is not None else None
        const_window = (d0 == 1 and dh == 1)
        degenerate = (nz == 0) or const_window or (
            hist is not None and abs(hist - 1.0) < 1e-9 and nz <= 1)
        detail = (
            f"16 维数值特征中非零仅 {nz} 维；tmf_hist_sim={_f(hist, 4)}；"
            f"窗口内 DISTINCT(tmf_pc00)={d0}、DISTINCT(tmf_hist_sim)={dh} ⇒ "
            + ("**常量退化**（窗口内每行完全相同）。" if const_window else "逐行在变但幅值退化。")
        )
        if degenerate:
            detail += ("退化特征若仍被模型消费，等价于给模型喂常量（静默降级）⇒ "
                       "必须与下行『消费侧开关』一起判定。")
        rows.append(_row(
            "feat_degenerate", "特征退化检测",
            "warn" if degenerate else "ok",
            ("退化（非零 %d/16 · hist_sim=%s）" % (nz, _f(hist, 3))) if degenerate
            else ("正常（非零 %d/16）" % nz),
            detail, "hcm_ai.timesfm_features"))

    # 5) 消费侧开关（停用后消费侧是否仍开着 —— 决定退化特征是否真有害）
    asof = await _cfg(config_provider, "ai.lm.tmf_asof_enabled")
    max_age = await _cfg(config_provider, "ai.lm.tmf_max_age_min")
    disabled = (not kv) or action.lower() == "disabled" or alive is False
    asof_on = _bool_str(asof) is True
    if disabled and asof_on:
        rows.append(_row(
            "consumer", "消费侧开关", "warn",
            f"⚠ asof=on（max_age={max_age or '—'}min）但 TimesFM 已停用",
            "配置 ai.lm.tmf_asof_enabled 仍为 true ⇒ 消费侧会按 as-of 取 tmf 特征。"
            "若模型契约**尚未**剔除这 13/16 维特征，则等于喂常量（静默降级）；"
            "若契约已剔除，请把本开关一并关掉以消除歧义。**需人工确认契约**（本 API 不臆断）。",
            "配置 ai.lm.tmf_asof_enabled"))
    else:
        rows.append(_row(
            "consumer", "消费侧开关", "ok" if (disabled and not asof_on) or (not disabled) else "warn",
            f"asof={'on' if asof_on else 'off'} · max_age={max_age or '—'}min",
            "" if disabled and asof_on else "与运行时状态一致（停用则关闭 / 启用则开启）。",
            "配置 ai.lm.tmf_asof_enabled"))

    # 6) 消费侧特征实况 —— **模型真正吃的是边车快照，不是 DB 表**，两处必须对齐
    #    （2026-09-17 实测教训：DB 表 16 维"非零 15/16"看似正常，但消费快照里
    #     `tmf_hist_sim=1.0`、`tmf_rev_prob=0`、`tmf_qf_*` 四维恒 0、pc 幅值仅 ~1e-4
    #     ⇒ 只看 DB 会漏报"模型吃到常量"。故本行以**消费点**为准。）
    snap = await _redis_json(redis_client, f"hcm:live:hexp:ai:{symbol}")
    lfs = (snap or {}).get("lm_features")
    lf = lfs if isinstance(lfs, dict) else {}
    if not lf:
        rows.append(_row(
            "consumer_feat", "消费侧特征实况（边车快照）", "na", "无快照",
            f"Redis 键 hcm:live:hexp:ai:{symbol} 缺失或其 lm_features 为空 ⇒ 无法判定模型"
            "**实际吃到**的 tmf 特征。注意：DB 表健康 ≠ 消费点健康，二者需分别看。",
            f"hcm:live:hexp:ai:{symbol}.lm_features"))
    else:
        tmf = {k: v for k, v in lf.items() if str(k).startswith("tmf")}
        zeros = [k for k, v in tmf.items() if (_num(v) or 0.0) == 0.0]
        pcs = [abs(_num(v) or 0.0) for k, v in tmf.items() if str(k).startswith("tmf_pc")]
        pc_max = max(pcs) if pcs else None
        hist = _num(tmf.get("tmf_hist_sim"))
        pc_flat = pc_max is not None and pc_max < 1e-3
        hist_flat = hist is not None and abs(hist - 1.0) < 1e-9
        bad = len(zeros) >= 4 or hist_flat or pc_flat
        rows.append(_row(
            "consumer_feat", "消费侧特征实况（边车快照）",
            "warn" if bad else "ok",
            f"tmf {len(tmf)} 维：{len(zeros)} 维恒 0 · pc 幅值 {pc_max:.1e}"
            if pc_max is not None else f"tmf {len(tmf)} 维：{len(zeros)} 维恒 0",
            f"恒 0 维度：{'、'.join(sorted(zeros)) or '无'}｜tmf_hist_sim={_f(hist, 4)}"
            + ("（恒 1.0）" if hist_flat else "")
            + (f"｜pc 分量幅值 {pc_max:.1e} ≈ 常数（区分度已丧失）" if pc_flat else "")
            + "。判据：≥4 维恒 0、或 hist_sim 恒 1.0、或 pc 幅值 <1e-3 任一成立即判定退化"
              "（退化特征喂给模型 ≈ 给常量，属**静默降级**）。"
              "本行为消费点真值，优先级高于上面的 DB 表检测。",
            f"hcm:live:hexp:ai:{symbol}.lm_features"))

    return {"key": "timesfm", "label": "TimesFM（时序基础模型 · 日级特征）",
            "status": _rollup(rows), "rows": rows}


# ─────────────────────────────────────────────────────────────────────────────
# 二、LightGBM 质量栈
# ─────────────────────────────────────────────────────────────────────────────
async def _calibrator_row(redis_client, key: str, row_key: str, label: str) -> dict:
    """校准器健康行（**同一份判据用于两条独立校准链**的可观测性）。

    【F4 2026-09-18】为什么需要它：本项目有**两套**校准链、写**两个不同的键** ——
      · `review_recalibrate.py`  → `hcm:ai:review:calib_health`（评审器链路）
      · `recalibrate_quality.py` → `hcm:ai:quality:calib_health`（质量头链路）
    审计发现：本体检表此前**只读 review 键** ⇒ `recalibrate_quality.py` 的
    joined / 每头 n / skipped 永远不可见。实测两条链差距极大
    （review joined=18 而 quality joined=837）⇒ 只读前者会把"更差的那条链"
    当成全貌，掩盖真实瓶颈（"配置了≠看得见"）。故两条都读、各出一行。
    """
    cal = await _redis_json(redis_client, key)
    if cal is None:
        return _row(row_key, label, "na", "无数据源",
                    f"Redis 键 {key} 缺失 ⇒ 无法判定该校准链健康。", key)
    joined = _num(cal.get("joined"))
    need = _num(cal.get("min_samples"))
    skipped = [k for k, v in (cal.get("heads") or {}).items()
               if isinstance(v, dict) and v.get("skipped")]
    alert = _bool_str(cal.get("alert"))
    short = joined is not None and need is not None and joined < need
    return _row(
        row_key, label,
        "block" if alert else ("warn" if (short or skipped) else "ok"),
        f"joined {int(joined) if joined is not None else '—'} / 需 "
        f"{int(need) if need is not None else '—'}"
        + (f" · {len(skipped)} 头未校准" if skipped else ""),
        f"窗口 {cal.get('window_days')} 天｜方法 {cal.get('method')}｜"
        f"预警阈值 ECE {cal.get('ece_alert_threshold')}｜alert={alert}｜"
        f"标签龄 {cal.get('labels_age_hours')}h。"
        + ("⇒ 样本不足，该链各头概率**未经校准**（概率值仅供排序，不可当真实胜率读）。"
           if short or skipped else "")
        + (f"｜未校准头：{'、'.join(skipped)}" if skipped else ""),
        key)


async def _lightgbm_section(db_pool, config_provider, redis_client, symbol: str, hours: int) -> dict:
    rows: list[dict] = []

    # 1) sidecar 存活
    kv = _parse_kv(await _redis_raw(redis_client, "hcm:ai:lightgbm:status"))
    alive = _bool_str(kv.get("alive"))
    beat = _num(kv.get("beat_age_s"))
    if not kv:
        rows.append(_row(
            "sidecar", "Sidecar 存活", "na", "无上报",
            "Redis 键 hcm:ai:lightgbm:status 缺失/过期 ⇒ 无法判定 sidecar 存活。",
            "hcm:ai:lightgbm:status"))
    else:
        ok = alive is True and (beat is None or beat <= SIDECAR_BEAT_WARN)
        rows.append(_row(
            "sidecar", "Sidecar 存活", "ok" if ok else "warn",
            f"{'存活' if alive else '未存活'}（pid={kv.get('pid') or '—'} · 心跳 {_human_lag(beat)}）",
            "" if ok else f"alive={alive} · beat_age_s={beat} ⇒ 边车不在跑时三头不再出新预测。",
            "hcm:ai:lightgbm:status"))

    # 2) champion 版本（影子晋升记录 + 模型路径配置）
    shadow = await _redis_json(redis_client, "hcm:ai:shadow:last")
    model_path = await _cfg(config_provider, "ai.lm.model_path")
    champ_file = str(model_path).replace("\\", "/").split("/")[-1] if model_path else None
    if shadow is None and not champ_file:
        rows.append(_row(
            "champion", "Champion 版本", "na", "无数据源",
            "既无影子晋升记录（hcm:ai:shadow:last），配置 ai.lm.model_path 也为空。",
            "hcm:ai:shadow:last"))
    else:
        adopted = _bool_str(shadow.get("adopt")) if shadow else None
        detail = (f"影子对照：{shadow.get('champion')} → {shadow.get('candidate')}，"
                  f"AUC {_f(shadow.get('shadow', {}).get('auc_champ'))} → "
                  f"{_f(shadow.get('shadow', {}).get('auc_cand'))}（门槛 "
                  f"{shadow.get('shadow', {}).get('min_abs_auc')}，"
                  f"标签源 {shadow.get('shadow', {}).get('label_src')}，"
                  f"n_eval={shadow.get('shadow', {}).get('n_eval')}）"
                  if shadow else "无影子晋升记录（hcm:ai:shadow:last 缺失）。")
        rows.append(_row(
            "champion", "Champion 版本", "ok" if adopted is not False else "warn",
            f"{champ_file or '—'}{'（影子已采纳）' if adopted else ''}",
            detail + f"｜晋升时间 {shadow.get('at') if shadow else '—'}",
            "hcm:ai:shadow:last + 配置 ai.lm.model_path"))

    # 3) 三头健康（最近一次重训的评测 + 裁决）
    last = await _redis_json(redis_client, "hcm:ai:retrain:last")
    if last is None:
        rows.append(_row(
            "heads", "三头健康（最近重训）", "na", "无数据源",
            "Redis 键 hcm:ai:retrain:last 缺失 ⇒ 无评测结果可判。",
            "hcm:ai:retrain:last"))
    else:
        hh = last.get("head_health", {}) or {}
        judge = (last.get("judge", {}) or {}).get("decision") or "—"
        deg = _bool_str(last.get("quality_calib_degenerate"))
        dir_ok = _bool_str(hh.get("dir_ok"))
        entry_ok = _bool_str(hh.get("entry_ok"))
        bad = (judge == "rollback") or (dir_ok is False) or (entry_ok is False) or (deg is True)
        rows.append(_row(
            "heads", "三头健康（最近重训）", "block" if bad else "ok",
            f"{last.get('model_version') or '—'} · AUC {_f(last.get('auc'))} · "
            f"dir_hit {_f(hh.get('dir_hit'), 4)} · entry_auc {_f(hh.get('entry_auc'), 4)}"
            f"{' · 校准退化' if deg else ''}",
            f"裁决={judge}｜回滚理由：{(last.get('judge', {}) or {}).get('reason') or '—'}"
            # 【D3 2026-09-17】把"判决口径"透出：auc 必须取自**落盘模型自身**
            # （auc_src=final_model）；tss_mean 是 5 折均值，仅诊断。
            # 二者混用曾导致 v109 的判决依据（0.516）与产物实际水平（0.477）不一致。
            f"｜判决口径={last.get('auc_src') or '—'}"
            f"（tss_mean={_f(last.get('auc_tss_mean'), 3)}）"
            f"｜样本 {last.get('samples')}｜连续失败 {last.get('fail_streak')}"
            f"｜评测时间 {last.get('at')}。"
            + ("⇒ **候选模型未上线**（champion 仍是旧版），但说明当前数据上三头判别力不达标，"
               "需检查标签质量/特征漂移，而非反复重训。" if bad else ""),
            "hcm:ai:retrain:last"))

    # 4) 重训守护 + 连续失败
    daemon = await _redis_json(redis_client, "hcm:ai:retrain:daemon")
    fs = _num(await _redis_raw(redis_client, "hcm:ai:retrain:fail_streak"))
    if daemon is None and fs is None:
        rows.append(_row(
            "retrain", "重训守护", "na", "无数据源",
            "hcm:ai:retrain:daemon 与 hcm:ai:retrain:fail_streak 均缺失。",
            "hcm:ai:retrain:daemon"))
    else:
        bad = fs is not None and fs >= FAIL_STREAK_WARN
        rows.append(_row(
            "retrain", "重训守护", "warn" if bad else "ok",
            f"pid={daemon.get('pid') if daemon else '—'} · 连续失败 {int(fs) if fs is not None else '—'} 次",
            f"上报时间 {daemon.get('at') if daemon else '—'}。"
            + (f"连续失败 ≥{FAIL_STREAK_WARN} 次 ⇒ 重训在空转：请查训练样本/标签供给，"
               "否则每次都会被回滚（本项只提示，不停用重训）。" if bad else ""),
            "hcm:ai:retrain:daemon / hcm:ai:retrain:fail_streak"))

    # 5) PSI 漂移（监控报告）
    mon = await _redis_json(redis_client, "hcm:ai:monitor:report:latest")
    if mon is None:
        rows.append(_row(
            "drift", "特征漂移（PSI）", "na", "无报告",
            "Redis 键 hcm:ai:monitor:report:latest 缺失 ⇒ 无漂移报告。",
            "hcm:ai:monitor:report:latest"))
    else:
        psi = mon.get("psi", {}) or {}
        pmax = _num(psi.get("max"))
        drifted = psi.get("drifted_features") or []
        bad = pmax is not None and pmax > PSI_WARN
        rows.append(_row(
            "drift", "特征漂移（PSI）", "warn" if bad else "ok",
            f"max PSI {_f(pmax, 4)}（{('、'.join(map(str, drifted[:3])) or '无')}）· 样本 {mon.get('n_samples')}",
            f"窗口 {mon.get('window_hours')}h｜报告时间 {mon.get('generated_at')}｜"
            f"豁免特征 {(mon.get('psi', {}) or {}).get('exempt_features')}。"
            + (f"max PSI > {PSI_WARN} ⇒ 该特征分布已明显偏移，需评估是否重训/剔除（近期"
               "重训被回滚也可能与漂移相关）。" if bad else ""),
            "hcm:ai:monitor:report:latest"))

    # 6) 校准器健康（**两条独立校准链各出一行** —— 见 _calibrator_row 的说明）
    rows.append(await _calibrator_row(
        redis_client, "hcm:ai:review:calib_health", "calibrator", "校准器（评审链路）"))
    rows.append(await _calibrator_row(
        redis_client, "hcm:ai:quality:calib_health", "calibrator_quality", "校准器（质量头链路）"))

    # 7) 预测落库新鲜度
    pr = await _fetchrow(
        db_pool,
        "SELECT max(t_time) AS last, count(*) AS n FROM hcm_ai.ai_pred_raw "
        "WHERE t_time > now() - make_interval(hours => $1)", hours)
    if pr is None:
        rows.append(_row("pred_log", "预测落库", "na", "查询失败",
                         "hcm_ai.ai_pred_raw 不可达。", "hcm_ai.ai_pred_raw"))
    else:
        lag = _lag_s(pr["last"])
        n = int(pr["n"] or 0)
        bad = n == 0 or (lag is not None and lag > 3 * 3600)
        rows.append(_row(
            "pred_log", "预测落库", "warn" if bad else "ok",
            f"近 {hours}h {n} 行 · 最近 {_human_lag(lag)}",
            "每根 bar 一条三头原始预测（raw_proba/cal_p/pred_class）。"
            + ("⇒ 超过 3h 无写入：边车可能停摆或未写库。" if bad else ""),
            "hcm_ai.ai_pred_raw"))

    # 8) 评审器（review_log）
    rv = await _fetchrow(
        db_pool,
        "SELECT max(created_at) AS last, count(*) AS n, avg(feat_missing_ratio) AS miss "
        "FROM hcm_ai.review_log WHERE created_at > now() - make_interval(hours => $1)", hours)
    max_miss = _num(await _cfg(config_provider, "ai.review.max_feat_missing"))
    if rv is None:
        rows.append(_row("reviewer", "评审器", "na", "查询失败",
                         "hcm_ai.review_log 不可达。", "hcm_ai.review_log"))
    else:
        n = int(rv["n"] or 0)
        miss = _num(rv["miss"])
        lag = _lag_s(rv["last"])
        bad_miss = miss is not None and max_miss is not None and miss > max_miss
        bad = n == 0 or bad_miss
        rows.append(_row(
            "reviewer", "评审器", "warn" if bad else "ok",
            f"近 {hours}h {n} 条 · 最近 {_human_lag(lag)} · 平均缺特征 {_f(miss, 3)}",
            f"阈值 ai.review.max_feat_missing={max_miss}｜模式 {await _cfg(config_provider, 'ai.review.mode')}。"
            + ("⇒ 平均特征缺失率超阈值 ⇒ 特征供给有问题（评审结论不可信）。" if bad_miss else "")
            + ("⇒ 无评审记录：评审器可能未运行。" if n == 0 else ""),
            "hcm_ai.review_log"))

    # 9) 闸门裁决（是否在拦单 —— 需结合 ai.mode 判定预期）
    mode = str(await _cfg(config_provider, "ai.mode") or "—")
    enabled = await _cfg(config_provider, "ai.enabled")
    gd = await _fetchrow(
        db_pool,
        "SELECT max(created_at) AS last, count(*) AS n FROM hcm_ai.gate_decision "
        "WHERE created_at > now() - make_interval(hours => $1)", hours)
    if gd is None:
        rows.append(_row("gate", "闸门裁决", "na", "查询失败",
                         "hcm_ai.gate_decision 不可达。", "hcm_ai.gate_decision"))
    else:
        n = int(gd["n"] or 0)
        lag = _lag_s(gd["last"])
        decoupled = mode.lower() in ("decoupled", "shadow", "observe", "off")
        if decoupled:
            rows.append(_row(
                "gate", "闸门裁决", "idle",
                f"AI {mode}（近 {hours}h 裁决 {n} 条）",
                f"ai.enabled={enabled} · ai.mode={mode} ⇒ AI **不参与拦截**，只观测/计分，"
                "故裁决表不写属**设计内**（非故障）。若期望 AI 恢复拦截，需把 ai.mode 切回 coupled。",
                "hcm_ai.gate_decision + 配置 ai.mode"))
        else:
            rows.append(_row(
                "gate", "闸门裁决", "warn" if n == 0 else "ok",
                f"AI {mode}（近 {hours}h 裁决 {n} 条 · 最近 {_human_lag(lag)}）",
                f"ai.enabled={enabled} · ai.mode={mode} ⇒ 处于拦截模式，"
                + ("但近窗口无裁决记录 ⇒ 链路可能断开。" if n == 0 else "裁决在正常写入。"),
                "hcm_ai.gate_decision + 配置 ai.mode"))

    # 10) 边车特征健康（快照侧真值：常量/缺失/离群/漂移级/模型是否加载）
    #     为什么单列一行：PSI 是"窗口统计"，而这里是**此刻喂给模型的这批特征**的健康度；
    #     二者互补（PSI 说分布偏了没，本行说这批特征还能不能用）。
    snap = await _redis_json(redis_client, f"hcm:live:hexp:ai:{symbol}")
    if not snap:
        rows.append(_row(
            "feat_health", "边车特征健康", "na", "无快照",
            f"Redis 键 hcm:live:hexp:ai:{symbol} 缺失 ⇒ 无法判定此刻特征健康度。",
            f"hcm:live:hexp:ai:{symbol}"))
    else:
        const = _num(snap.get("feat_constant_ratio"))
        miss = _num(snap.get("feat_missing_ratio"))
        outl = _num(snap.get("feat_outlier_ratio"))
        drift = snap.get("drift_level")
        psi_d = _num(snap.get("psi_drift"))
        loaded = _bool_str(snap.get("model_loaded"))
        valid = _bool_str(snap.get("valid"))
        bad = ((const is not None and const > FEAT_CONST_WARN)
               or (miss is not None and max_miss is not None and miss > max_miss)
               or loaded is False or valid is False)
        rows.append(_row(
            "feat_health", "边车特征健康", "warn" if bad else "ok",
            f"常量 {_f((const or 0) * 100, 1)}% · 缺失 {_f((miss or 0) * 100, 1)}%"
            f" · 漂移级 {drift if drift is not None else '—'}"
            f" · 模型{'已载' if loaded else '未载'}",
            f"离群占比 {_f(outl, 3)}｜快照 psi_drift={_f(psi_d, 4)}｜"
            f"模型 {snap.get('model_version') or '—'}｜valid={valid}｜"
            f"degrade_streak={snap.get('degrade_streak')}｜ai_score={_f(snap.get('ai_score'), 2)}"
            f"｜快照时间 {snap.get('ts')}。"
            f"判据：常量特征占比 >{FEAT_CONST_WARN:.0%}、或缺失率超 "
            f"ai.review.max_feat_missing（{max_miss}）、或模型未加载/快照无效 任一成立即关注。",
            f"hcm:live:hexp:ai:{symbol}"))

    return {"key": "lightgbm", "label": "LightGBM 质量栈（三头 + 重训 + 漂移 + 校准）",
            "status": _rollup(rows), "rows": rows}


# ─────────────────────────────────────────────────────────────────────────────
# 三、FSM 状态机（LightGBM 4 类状态模型）
# ─────────────────────────────────────────────────────────────────────────────
async def _state_section(db_pool, config_provider, redis_client, symbol: str, hours: int, tf: str) -> dict:
    rows: list[dict] = []
    live = await _redis_json(redis_client, f"hcm:live:state:{symbol}") or {}
    infer = live.get("infer") if isinstance(live.get("infer"), dict) else {}
    infer = infer or {}

    # 1) 开关：是否真驱动下单（shadow_only=false + order_enabled=true ⇒ 真影响交易）
    en = await _cfg(config_provider, "state.enabled")
    oe = await _cfg(config_provider, "state.order_enabled")
    shadow = _bool_str(live.get("shadow_only"))
    en_on = _bool_str(en) is True
    oe_on = _bool_str(oe) is True
    if not en_on:
        rows.append(_row("switch", "启用与下单权", "idle", "推理未启用",
                         "state.enabled=false ⇒ 状态机不推理、不驱动下单（no_model 早退）。",
                         "配置 state.enabled"))
    else:
        rows.append(_row(
            "switch", "启用与下单权", "ok" if (oe_on and shadow is not True) else "warn",
            f"推理启用 · {'允许下单' if oe_on else '仅观测（order_enabled=false）'}"
            f" · {'非影子' if shadow is False else ('影子' if shadow else '—')}",
            "shadow_only=false 且 order_enabled=true ⇒ 本模型**真驱动交易**（非观测）；"
            "这也是它与 sidecar 质量栈（ai.mode=decoupled，只观测）最大的区别。",
            "hcm:live:state + 配置 state.enabled/order_enabled"))

    # 2) 模型版本（48h 内出现过的版本）
    mv = await _fetch(
        db_pool,
        "SELECT coalesce(model_version,'-') AS mv, count(*) AS n "
        "FROM hcm_signal.market_state_log "
        "WHERE symbol=$1 AND time_frame=$2 AND bar_open_time > now() - make_interval(hours => $3) "
        "GROUP BY 1 ORDER BY 2 DESC",
        symbol, tf, hours)
    if mv is None:
        rows.append(_row("model", "模型版本", "na", "查询失败",
                         "hcm_signal.market_state_log 不可达。", "hcm_signal.market_state_log"))
    else:
        vers = [(r["mv"], int(r["n"])) for r in mv]
        cur = live.get("model_version") or (vers[0][0] if vers else None)
        rows.append(_row(
            "model", "模型版本", "ok" if vers else "na",
            f"{cur or '—'}" + (f"（窗口内 {len(vers)} 个版本）" if len(vers) > 1 else ""),
            "窗口内出现多个版本 ⇒ 说明中途换过模型，前后 bar 的可比性下降，分析时需分段。"
            if len(vers) > 1 else "窗口内单一版本（诚实性良好）。",
            "hcm_signal.market_state_log.model_version"))

    # 3) 推理健康（infer_ok + 原因分布）—— 只要出现 no_model/contract_mismatch 即 block
    ih = await _fetchrow(
        db_pool,
        "SELECT count(*) AS n, count(*) FILTER (WHERE infer_ok IS FALSE) AS bad "
        "FROM hcm_signal.market_state_log "
        "WHERE symbol=$1 AND time_frame=$2 AND bar_open_time > now() - make_interval(hours => $3)",
        symbol, tf, hours)
    rs = await _fetch(
        db_pool,
        "SELECT coalesce(infer_reason,'-') AS reason, count(*) AS n "
        "FROM hcm_signal.market_state_log "
        "WHERE symbol=$1 AND time_frame=$2 AND bar_open_time > now() - make_interval(hours => $3) "
        "GROUP BY 1 ORDER BY 2 DESC",
        symbol, tf, hours)
    if ih is None:
        rows.append(_row("infer", "推理健康", "na", "查询失败",
                         "market_state_log 不可达。", "hcm_signal.market_state_log"))
    else:
        n = int(ih["n"] or 0)
        bad = int(ih["bad"] or 0)
        reasons = {r["reason"]: int(r["n"]) for r in (rs or [])}
        fatal = [k for k in reasons if k in ("no_model", "contract_mismatch")]
        st = "block" if fatal else ("warn" if bad > 0 else ("ok" if n > 0 else "na"))
        rows.append(_row(
            "infer", "推理健康", st,
            f"窗口 {n} 根 · 失败 {bad} 根",
            f"推理失败原因分布：{reasons or '—'}。"
            + ("⇒ **致命失败**（模型缺失/特征契约不符）：状态会退化到默认路径，必须立刻处理。"
               if fatal else "")
            + ("（low_conf 不算失败：它表示置信不足、按设计**不迁移状态**。）" if reasons.get("low_conf") else ""),
            "hcm_signal.market_state_log.infer_ok/infer_reason"))

    # 4) 判定率（decided 占比）—— 低判定率 ⇒ 状态久停 ⇒ 不出新单
    dr = await _fetchrow(
        db_pool,
        "SELECT count(*) AS n, count(*) FILTER (WHERE decided) AS d "
        "FROM hcm_signal.market_state_log "
        "WHERE symbol=$1 AND time_frame=$2 AND bar_open_time > now() - make_interval(hours => $3)",
        symbol, tf, hours)
    min_conf = _num(await _cfg(config_provider, "state.min_conf"))
    if dr is None:
        rows.append(_row("decide", "判定率", "na", "查询失败",
                         "market_state_log 不可达。", "hcm_signal.market_state_log"))
    else:
        n = int(dr["n"] or 0)
        d = int(dr["d"] or 0)
        rate = (d / n) if n else None
        bad = rate is not None and rate < DECIDE_RATE_WARN
        rows.append(_row(
            "decide", "判定率", "warn" if bad else ("ok" if rate is not None else "na"),
            f"{(rate * 100):.0f}%（{d}/{n}）· min_conf={_f(min_conf, 2)}"
            if rate is not None else "无样本",
            "decided=false 的 bar **不参与防抖**（状态维持不动）⇒ 判定率过低会让状态"
            "长时间卡在同一态（表现为长时间不出新单）。min_conf 越低判定率越高，"
            "但也越容易被噪声推动 —— 这里只呈现事实，不自动调参。",
            "hcm_signal.market_state_log.decided + 配置 state.min_conf"))

    # 5) 类别覆盖（模型 4 类是否都判出来 + trend_init 已知短板提示）
    # 【2026-09-21 修复】原实现有两个会**误报**的结构缺陷（实测踩到，2026-09-21 报"未出现
    #   oscillation、trend_mid"）：
    #   ① 窗口用**墙钟小时**（`make_interval(hours => $3)`）⇒ 跨周末/节假日后窗口内可能
    #      只剩几十根 bar（实测：48h 窗口跨周末 ⇒ **仅 30 根**）⇒ 任何类"恰好没出现"都属正常，
    #      原实现却直接判 warn；
    #   ② `AND decided` 过滤 ⇒ **系统性丢弃低 margin 的类**（实测 23:20 那根
    #      `p_trend_mid=0.363` 是窗口内最高值，却因 `margin=0.027 < min_margin=0.05` 被滤掉）
    #      ⇒ 放大"缺失"假象。
    #   故：取**两个口径**（全部 bar / 仅 decided），并给"缺失"结论加**最小样本门槛**。
    cc = await _fetch(
        db_pool,
        "SELECT predicted_class AS c, decided, count(*) AS n "
        "FROM hcm_signal.market_state_log "
        "WHERE symbol=$1 AND time_frame=$2 AND bar_open_time > now() - make_interval(hours => $3) "
        "GROUP BY 1, 2",
        symbol, tf, hours)
    if cc is None:
        rows.append(_row("classes", "类别覆盖", "na", "查询失败",
                         "market_state_log 不可达。", "hcm_signal.market_state_log"))
    else:
        cnt = {r["c"]: int(r["n"]) for r in cc if r["decided"]}
        cnt_all: dict = {}
        for r in cc:
            cnt_all[r["c"]] = cnt_all.get(r["c"], 0) + int(r["n"])
        tot = sum(cnt.values())
        tot_all = sum(cnt_all.values())
        missing = [c for c in STATE_CLASSES if cnt.get(c, 0) == 0]
        missing_all = [c for c in STATE_CLASSES if cnt_all.get(c, 0) == 0]
        # 最小样本门槛：低于此值不下"缺失"结论（否则周末/节后必然误报）
        thin = tot < CLASS_COVER_MIN_DECIDED
        ti_share = (cnt.get("trend_init", 0) / tot) if tot else None
        bad = (bool(missing) and not thin) or (
            ti_share is not None and ti_share < TREND_INIT_SHARE_WARN)
        rows.append(_row(
            "classes", "类别覆盖",
            "warn" if bad else ("ok" if tot else "na"),
            (("、".join(f"{c} {cnt.get(c, 0)}" for c in STATE_CLASSES) if tot
              else "无 decided 样本")
             + f"（窗口内 bar {tot_all} 根）"),
            f"窗口内 decided 合计 {tot}（全部 bar {tot_all} 根）。"
            + (f"⇒ 未出现的类别：{'、'.join(missing)}（模型完全判不出 ⇒ 该类驱动的策略分支永不触发）。"
               if missing and not thin else "")
            + (f"⚠ 样本不足（decided {tot} < {CLASS_COVER_MIN_DECIDED}）⇒ **本项不下结论**："
               "窗口是**墙钟小时**，跨周末/节假日后 bar 数会显著偏少，"
               f"此时「某类未出现」属正常噪声（未过滤口径下未出现："
               f"{'、'.join(missing_all) or '无'}）。请以更长窗口或交易日口径复核。"
               if thin else "")
            # ⚠ 必须用显式 `+` 连接：`f"表头" "、".join(...)` 会被 Python 隐式拼接成
            #   `("表头、").join(...)` ⇒ 表头被当成分隔符逐项重复（本轮实测踩到并修复）。
            + (("⇒ 全部 bar（不过滤 decided）口径："
                + "、".join(f"{c} {cnt_all.get(c, 0)}" for c in STATE_CLASSES)
                + "。⚠ `decided` 过滤会**优先丢弃低 margin 的类**（如 trend_mid），"
                  "故 decided 口径的类别缺失**不等于**模型判不出。")
               if tot and (thin or tot_all >= CLASS_COVER_MIN_DECIDED) else "")
            + (f"⇒ trend_init 占比仅 {ti_share * 100:.1f}%：v3 holdout 该头 F1≈0.16（已知短板），"
               "而 trend_init 恰是趋势入场的**关键状态** ⇒ 直接压低趋势首单机会。"
               if ti_share is not None and ti_share < TREND_INIT_SHARE_WARN else ""),
            "hcm_signal.market_state_log.predicted_class + decided"))

    # 6) 落库新鲜度（状态机是否仍在逐 bar 工作；替代"读容器日志自检行"）
    fq = await _fetchrow(
        db_pool,
        "SELECT max(bar_open_time) AS last FROM hcm_signal.market_state_log "
        "WHERE symbol=$1 AND time_frame=$2", symbol, tf)
    tf_min = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}.get(tf, 5)
    if fq is None or fq["last"] is None:
        rows.append(_row("fresh", "落库新鲜度", "na", "无数据",
                         "该品种/周期无任何状态行。", "hcm_signal.market_state_log"))
    else:
        lag = _lag_s(fq["last"])
        bars = (lag / 60.0 / tf_min) if lag is not None else None
        bad = bars is not None and bars > INFER_FRESH_BARS
        rows.append(_row(
            "fresh", "落库新鲜度", "warn" if bad else "ok",
            f"最近 {tf} bar {_iso(fq['last'])}（{_human_lag(lag)}）"
            + (f" ≈ 滞后 {bars:.1f} 根" if bars is not None else ""),
            "状态机应每根 bar 落一行；滞后超过 "
            f"{INFER_FRESH_BARS} 根即说明推理/写库链路可能中断。",
            "hcm_signal.market_state_log.bar_open_time"))

    # 7) FSM 是否真落单（近窗口 state_osc/state_trend 成交）
    od = await _fetchrow(
        db_pool,
        "SELECT count(*) AS n, max(o.open_time) AS last "
        "FROM hcm_trading.orders o JOIN hcm_signal.signals s ON s.signal_id = o.signal_id "
        "WHERE o.open_time > now() - make_interval(hours => $1) "
        "AND s.signal_mode = ANY($2::varchar[])",
        hours, list(FSM_SIGNAL_MODES))
    if od is None:
        rows.append(_row("orders", "FSM 落单", "na", "查询失败",
                         "hcm_trading.orders / hcm_signal.signals 不可达。",
                         "hcm_trading.orders ⋈ hcm_signal.signals"))
    else:
        n = int(od["n"] or 0)
        rows.append(_row(
            "orders", "FSM 落单", "ok" if n > 0 else "idle",
            f"近 {hours}h {n} 笔（最近 {_human_lag(_lag_s(od['last']))}）",
            "口径：orders 表**无 magic 列**，故用 signals.signal_mode ∈ "
            f"{list(FSM_SIGNAL_MODES)} 判定 FSM 驱动单。"
            + ("⇒ 计数为 0 时先看上面『判定率/落库新鲜度』：状态机没新判定就不会有新单，"
               "属于**设计内**而非故障。" if n == 0 else ""),
            "hcm_trading.orders ⋈ hcm_signal.signals.signal_mode"))

    # 8) 当前实时读数（信息行）
    proba = live.get("proba") if isinstance(live.get("proba"), dict) else {}
    rows.append(_row(
        "live", "当前实时读数", "ok",
        f"{live.get('state') or '—'} · {live.get('predicted_class') or '—'}"
        f" · margin {_f(live.get('margin'))} · age {live.get('age_bars')} 根",
        f"概率 {proba or '—'}｜infer_ok={live.get('infer_ok')}｜"
        f"decided={live.get('decided')}｜reason={live.get('infer_reason') or '—'}｜"
        f"快照时间 {live.get('updated_at') or '—'}。"
        "（state=执行态·下单依据；predicted_class=模型 argmax·仅参考。）",
        f"hcm:live:state:{symbol}"))

    return {"key": "state", "label": f"FSM 状态机（LightGBM 4 类状态模型 · {tf}）",
            "status": _rollup(rows), "rows": rows}


# ─────────────────────────────────────────────────────────────────────────────
# 聚合入口（**独立函数**：便于在容器内直接调用做自测，不必经 HTTP+鉴权）
# ─────────────────────────────────────────────────────────────────────────────
async def build_health_report(db_pool=None, config_provider=None, redis_client=None,
                              symbol: str = "XAUUSD", hours: int = 48,
                              time_frame: str = "M5") -> dict:
    t0 = time.time()
    sym = (symbol or "XAUUSD").upper()
    hrs = max(1, min(int(hours or 48), 168))

    sections = []
    # 【2026-09-21 TimesFM 全栈卸载】`_timesfm_section` 已从遍历中移除：
    #   其数据源（Redis hcm:ai:timesfm:*、表 hcm_ai.timesfm_features、ai.lm.tmf_* 配置）
    #   与消费侧代码均已删除。若继续调用，体检表会显示"表不可达 / 守护未上报" —— 
    #   **文案有误导**（实为"已卸载"而非"故障"），故直接摘除。
    #   归档与回滚：tools/_attic_timesfm_20260921/README.md
    for fn in (_lightgbm_section,):
        try:
            sections.append(await fn(db_pool, config_provider, redis_client, sym, hrs))
        except Exception as exc:  # noqa: BLE001
            logger.exception("ai_health section failed: %s", exc)
            sections.append({"key": fn.__name__, "label": fn.__name__,
                             "status": "na", "rows": [_row(
                                 "error", "区段异常", "na", "内部错误",
                                 str(exc)[:200], "ai_health")]})
    try:
        sections.append(await _state_section(db_pool, config_provider, redis_client, sym, hrs, time_frame))
    except Exception as exc:  # noqa: BLE001
        logger.exception("ai_health state section failed: %s", exc)
        sections.append({"key": "state", "label": "FSM 状态机", "status": "na",
                         "rows": [_row("error", "区段异常", "na", "内部错误", str(exc)[:200], "ai_health")]})

    counts = {"ok": 0, "warn": 0, "block": 0, "idle": 0, "na": 0}
    for s in sections:
        for r in s["rows"]:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
    overall = _rollup([{"status": s["status"]} for s in sections])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": sym,
        "time_frame": time_frame,
        "window_hours": hrs,
        "overall": overall,
        "summary": counts,
        "sections": sections,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────
def create_ai_health_router(db_pool=None, config_provider=None, auth_handler=None,
                            redis_client=None) -> APIRouter:
    """AI 体检表只读路由（factory 被 main.py 的 startup() 调用）。

    ⚠️ 新增 web/api/*.py 必须同时在 docker-compose.yml 的 hcm-web.volumes 补一行
    绑定挂载（见该文件 [2026-09-15] state.py 处的说明）：缺挂载时容器 restart 会在
    main.py 的 import 处 ImportError，导致 hcm-web **整体起不来**（不是新接口 404）。
    """
    router = APIRouter(tags=["ai-health"])

    @router.get("/api/v1/ai/health")
    async def ai_health(symbol: str = "XAUUSD", hours: int = 48, tf: str = "M5",
                        user=Depends(auth_handler.require_auth if auth_handler else (lambda: None))):
        """三块体检一次取回（TimesFM / LightGBM 质量栈 / FSM 状态机）。

        参数：symbol（默认 XAUUSD）｜hours 窗口小时（1..168，默认 48）｜tf 状态机周期（默认 M5）。
        """
        data = await build_health_report(db_pool, config_provider, redis_client,
                                         symbol=symbol, hours=hours, time_frame=tf)
        return {"code": 0, "data": data, "message": "ok"}

    return router
