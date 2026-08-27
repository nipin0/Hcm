"""quality_gate.py — AI 信号质量闸门（纯逻辑，全配置驱动，零硬编码）。

职责：在 HEXP 候选信号上施加 AI 质量裁决。**仅否决 / 降级 / 升级，无独立开仓权。**

纪律红线（结构不变式）：
  - 本模块输出契约 = {action ∈ VETO/DOWNGRADE/HOLD/UPGRADE, final_grade, lot_mult,
    total_score, c_ai, s_hp}，**不含 direction** —— 方向永远由 HEXP 决定。
  - 升级不逆转 HEXP 已拦信号（未过 HEXP 闸门的信号，本模块一律 HOLD，绝不打开）。
  - ai.enabled=false 或 ai.mode=decoupled 时，本模块原样透传（纯 HEXP）。

数据源：p 来自 Redis `hcm:live:hexp:ai:{symbol}`（由独立 sidecar `quality_scorer.py`
发布）；hexp 快照字段（hp_score/k/grade/direction/passed）来自 `hcm:live:hexp:{symbol}`。
本模块只读、纯函数、可单测，不直接读写任何存储。
"""

from __future__ import annotations

import json
from typing import Any, Optional

# 等级梯（低→高）。降级=向左一格，升级=向右一格，两端 clamp。
GRADE_ORDER = ["RED", "C", "B", "A", "S"]

# ── 配置兜底默认值（与 0011_ai_quality_config.sql 对齐；禁硬编码）──
# 注：【2026-08-18 解耦】c_ai 现为【纯 LightGBM 单源】分（0-100），
#     由 ai_async_client.calibrate_lm_score 产出，DeepSeek 不再参与融合。
#     本模块只消费该单值，语义不变（原融合权重 ai.fuse.w_lm/w_ds 已废弃）。
CFG_FALLBACK: dict[str, Any] = {
    "ai.enabled": False,
    "ai.mode": "decoupled",          # decoupled / coupled
    "ai.lm.pass_threshold": 0.50,
    "ai.lm.down_threshold": 0.60,
    "ai.lm.up_threshold": 0.70,
    "ai.lm.veto_floor": 0.30,        # c_ai/100 低于此才否决；否则 HOLD 放行（避免中等分全杀）
    "ai.lm.veto_quantile": 0.50,     # 分位重锚（备用）
    "ai.lm.down_quantile": 0.70,
    "ai.lm.up_quantile": 0.85,
    # 2026-08-17 方案⑤：sidecar 断流兜底新鲜度窗口（秒）。
    # hcm:live:hexp:ai:{symbol} 的 ts 距 now 超过此值 → 视为过期 → lm_score 置 None
    # → 融合 source="none" → 透传纯 HEXP（不误杀）。默认 60s（sidecar 每 ~5s 刷新，
    # 容 12 次刷新空窗）。仅影响"断流期"裁决，模型真在场且分低仍 VETO。
    "ai.lm.max_age_sec": 60.0,
    "ai.cpl.enabled": False,
    "ai.cpl.w_trend": 0.7,
    "ai.cpl.w_neutral": 0.6,
    # 2026-08-15 低共振态自适应：NEUTRAL 档(0.5<k≤1.2)内 w 不再恒 0.6，
    # 而是随 k 连续插值——k 越小(越接近震荡)→w 越大(越信 HEXP)。w_neutral_low
    # 是 NEUTRAL 档下沿(k 趋近 k_range_max)的 HP 权重，默认 0.85(几乎只信 HEXP)。
    "ai.cpl.w_neutral_low": 0.85,
    "ai.cpl.w_range": 0.5,
    # 2026-08-15 K 分支补全：强趋势(k>1.2)与衰竭(k>=2.0)语义分离。
    # 衰竭态(趋势末端、反转前兆)应更信 HEXP(和乘幂自身对极值/反转最敏感)，
    # 故 AI 权重进一步压低到 w_exhaust(< w_trend)。文档 2.2 分布：
    # 强趋势 k=1.8~2.5 / 震荡 k=0.5~0.8 / 转换 k=1.0 / 衰竭 k=2.0~3.0。
    "ai.cpl.w_exhaust": 0.85,
    "ai.cpl.k_trend_min": 1.2,
    "ai.cpl.k_exhaust_min": 2.0,
    "ai.cpl.k_range_max": 0.5,
    # 2026-08-17 方案甲：HEXP 已过闸信号的「耦合二次放行门槛」。
    # coupled 模式 + c_ai 有效时，耦合总分(total=w·hp+(1-w)·c_ai)必须 ≥ 此值才放行；
    # 低于此值 → coupling_pass=False → scheduler 侧拦掉（HEXP 已过闸也不下单）。
    # AI 断联(c_ai=None)/解耦/未启用 → 透传纯 HEXP，coupling_pass 恒 True（兜底放行，不误杀）。
    # 与面板 ai.cpl.tier_low(现行=50) 对齐；置 0 即等效「不设门槛」（完全回退）。
    # 2026-08-26 hexp 独立/解耦模式手数链动总开关：为 true 时，无 c_ai(纯 HEXP/
    # 解耦/断联)的信号用 hp_score 经 lot_tier_for 选档进风控动态手数；
    # false 时保持原 none 语义(不干预手数)。coupled 模式不受此开关影响(走 total 融合)。
    "hexp.lot_tier_enabled": True,
    "hexp.coupling_pass_threshold": 50.0,
    # tier 门槛按真实 total 分布标定(2026-08-14)：total=w·hp+(1-w)·c_ai 实测≈50~65，
    # 原 85/70/60 全部高于分布上限 → 任何信号都 tier=none 被压制(全量不下单)。
    "ai.cpl.tier_high": 70.0,
    "ai.cpl.tier_mid": 58.0,
    "ai.cpl.tier_low": 45.0,
    "ai.cpl.lot_high": 1.5,
    "ai.cpl.lot_low": 0.5,
}


def _g(cfg: dict, key: str):
    """读取配置值：数值优先，布尔兜底。

    【B1 修复 2026-08-14】config_provider.get 返回字符串（如 "0.6"），旧实现
    对一切 str 走布尔解析 → "0.6"→False(0.0)，所有数值键(down/up_threshold、
    cpl.w_*/tier_*)全部坏成 0 → 闸门 100% UPGRADE 到 S、lot_tier 恒 high。
    现：str 先尝试 float，失败再按布尔词解析。
    """
    v = cfg.get(key, CFG_FALLBACK[key])
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            return float(s)
        except (TypeError, ValueError):
            pass
        return s.lower() in ("true", "1", "yes", "on")
    return float(v)


def grade_index(grade: str) -> int:
    return GRADE_ORDER.index(grade) if grade in GRADE_ORDER else 2  # 未知→B(中间档)


def coupling_weight(k: float, cfg: dict) -> float:
    """依市场模式 k 选 S_hp 权重 w（C_ai 权重 = 1-w，硬不变式 C_ai ≤ 0.5）。

    2026-08-15 低共振态自适应：原 NEUTRAL 档(0.5<k≤1.2)恒取 w_neutral=0.6，
    导致低波动/弱共振(k≈0.5~0.8)时 AI 仍占 40% 话语权——而此时 HEXP 共振
    矩阵本就最不可信方向、最该压 AI。现改为 NEUTRAL 档内随 k 连续插值：
      k 趋近 k_range_max(0.5) → w=w_neutral_low(默认0.85，几乎只信 HEXP)
      k 趋近 k_trend_min(1.2)  → w=w_neutral(0.6)
    即「共振越弱、越不信 AI」，让 HEXP 共振失败真正生效、不被 AI 拉回。

    其余三档维持语义分离（文档 2.2 分布）：
      强趋势 1.8~2.5 / 震荡 0.5~0.8 / 转换 1.0 / 衰竭 2.0~3.0。
    衰竭态(趋势末端、反转前兆)和乘幂(HEXP)自身对极值/反转最敏感，AI 反
    而容易在拐点误判，故衰竭档进一步压低 AI 权重到 w_exhaust(>w_trend)。
    四档：
      k >= k_exhaust_min(2.0)   → EXHAUST  w_exhaust(更信 HEXP)
      k >  k_trend_min(1.2)     → TREND    w_trend
      k <= k_range_max(0.5)     → RANGE    w_range
      其余(0.5<k≤1.2)          → NEUTRAL  线性插值[w_neutral_low, w_neutral]
    """
    k_exhaust_min = _g(cfg, "ai.cpl.k_exhaust_min")
    k_trend_min = _g(cfg, "ai.cpl.k_trend_min")
    k_range_max = _g(cfg, "ai.cpl.k_range_max")

    if k >= k_exhaust_min:
        return _g(cfg, "ai.cpl.w_exhaust")
    if k > k_trend_min:
        return _g(cfg, "ai.cpl.w_trend")
    if k <= k_range_max:
        return _g(cfg, "ai.cpl.w_range")
    # NEUTRAL 档：在 [k_range_max, k_trend_min] 上把 w 从 w_neutral_low 线性升到 w_neutral
    w_low = _g(cfg, "ai.cpl.w_neutral_low")
    w_high = _g(cfg, "ai.cpl.w_neutral")
    t = (k - k_range_max) / max(k_trend_min - k_range_max, 1e-6)
    t = max(0.0, min(1.0, t))
    return w_low + t * (w_high - w_low)


def coupling_total(hp_score: float, c_ai: float, k: float, cfg: dict) -> float:
    """总分 = w(k)·S_hp + (1-w(k))·C_ai。"""
    w = coupling_weight(k, cfg)
    return w * hp_score + (1.0 - w) * c_ai


def lot_tier_for(total: float, cfg: dict) -> str:
    """耦合总分 → 手数分档（low/mid/high/none）。

    仅当 ai.cpl.enabled=true 时启用；否则 caller 侧 fallback 为 "none"。
    tier 分界（0-100 总分）：
      total >= ai.cpl.tier_high(85) → "high"
      total >= ai.cpl.tier_mid(70)  → "mid"
      total >= ai.cpl.tier_low(60)  → "low"
      否则                          → "none"（极弱：不发）
    返回的是「档位语义」而非固定倍率——实际倍率由风控面板的动态手数
    配置（risk.lot_multiplier_{low|mid|high} + risk.lot_base）决定，
    实现「AI 只选档、风控定准确下单值」的链动设计（2026-08-14 需求）。
    """
    if total >= _g(cfg, "ai.cpl.tier_high"):
        return "high"
    if total >= _g(cfg, "ai.cpl.tier_mid"):
        return "mid"
    if total >= _g(cfg, "ai.cpl.tier_low"):
        return "low"
    return "none"


def cpl_enabled(cfg: dict) -> bool:
    """耦合手数分档是否启用（供 caller 区分 'none=未启用' 与 'none=极弱压制'）。"""
    return bool(_g(cfg, "ai.cpl.enabled"))


def adjust_grade(p: float, grade: str, cfg: dict, passed: bool = True):
    """p → (action, new_grade)。

    p = c_ai/100（融合 AI 分，0-1）。
    passed = hexp 引擎是否原本放行该信号（False=hexp 拦了但有方向）。

    否决纪律（2026-08-14 修正）：
      - 仅当 p < veto_floor（极低分）才 VETO → 杜绝"中等分 38 必全杀"死锁；
      - p ∈ [veto_floor, down_threshold) → DOWNGRADE（降一级，仍放行）；
      - p ≥ up_threshold → UPGRADE（升一级；若 hexp 原未放行则视为 AI 打开）；
      - 其余 → HOLD（原样放行）。
    """
    veto_floor = _g(cfg, "ai.lm.veto_floor")
    down_th = _g(cfg, "ai.lm.down_threshold")
    up_th = _g(cfg, "ai.lm.up_threshold")
    idx = grade_index(grade)
    if p < veto_floor:
        return "VETO", grade
    if p < down_th:
        return "DOWNGRADE", GRADE_ORDER[max(0, idx - 1)]
    if p >= up_th:
        return "UPGRADE", GRADE_ORDER[min(len(GRADE_ORDER) - 1, idx + 1)]
    # HOLD：hexp 未放行但 AI 中等分 → 视为 AI 赋能打开（仅当 hexp 给了明确方向，
    # 由 decide() 上游保证 direction∈BUY/SELL 才走到这）
    if not passed:
        return "UPGRADE", GRADE_ORDER[min(len(GRADE_ORDER) - 1, idx + 1)]
    return "HOLD", grade


def decide(
    snapshot: dict,
    c_ai: Optional[float],
    cfg: dict,
    c_ai_meta: Optional[dict] = None,
) -> dict:
    """闸门主入口。

    输入：
      snapshot  来自 hexp 评分结果（字段：hp_score 0-100 / k / grade /
                direction / passed / scorecard_total）。
      c_ai      AI 分（0-100）=【纯 LightGBM 概率分】，由 scheduler 侧
                ai_async_client.calibrate_lm_score(lm_score, ds_out, cfg) 产出。
                【2026-08-18 解耦】DeepSeek fake_prob 不再参与融合，
                其赋能已移至离线训练管线（ds_calib_weight → sample_weight）。
                本模块是纯逻辑，不调用任何 AI（保持只读、可单测）。
      c_ai_meta 诊断（source="lm_only"/"none"、lm_score，及纯观测的
                ds_score/ds_age_sec），仅透传进返回 dict，不参与裁决。

    返回决策 dict；ai 未启用/解耦/无 c_ai/引擎未放行 时原样透传纯 HEXP。
    """
    enabled = _g(cfg, "ai.enabled")
    mode = str(cfg.get("ai.mode", "decoupled"))
    hp_score = float(snapshot.get("hp_score") or snapshot.get("scorecard_total") or 0.0)
    # 2026-08-27 修正：手数链动严格使用 6 维综合分 scorecard_total（稳定），
    # 禁止用 hp_score（强度单维，行情强度暴涨暴跌不稳定）决定动态手数档。
    # 解耦/耦合两种模式的手数分档(low/mid/high)均由 scorecard_total 经 lot_tier_for 选出。
    scorecard_total = float(snapshot.get("scorecard_total") or 0.0)
    k = float(snapshot.get("k") or 1.0)
    grade = str(snapshot.get("grade") or "C")
    passed = bool(snapshot.get("passed", False))
    direction = str(snapshot.get("direction") or "NO_TRADE")

    meta = c_ai_meta or {}

    # 纯 HEXP 透传（未启用 / 解耦 / 无融合票 / 无方向）
    if not enabled or mode != "coupled" or c_ai is None or direction == "NO_TRADE":
        # 【2026-08-26 手数链动】hexp 独立/解耦/无融合票模式也链动手数：
        # 用 HEXP 自身评分 hp_score 经 lot_tier_for 选档（不再恒 none），
        # 让「hexp 独立下单」也能进风控动态手数（risk.lot_multiplier_*）。
        # 受 hexp.lot_tier_enabled 总开关控制；关闭时保持原 none 语义（不干预手数）。
        _hexp_lot_enabled = _g(cfg, "hexp.lot_tier_enabled")
        _lot_tier = ("none", False)
        if _hexp_lot_enabled:
            # 手数链动用 6 维综合分（scorecard_total），不用强度单维 hp_score
            _tier = lot_tier_for(scorecard_total, cfg)
            _lot_tier = (_tier, bool(_tier != "none"))
        else:
            _lot_tier = ("none", False)
        return {
            "action": "HOLD",
            "final_grade": grade,
            "lot_tier": _lot_tier[0],
            "total_score": round(hp_score, 2),
            "c_ai": None,
            "s_hp": hp_score,
            "ai_opened": False,
            "coupling_pass": True,   # hexp 独立：无耦合，兜底放行
            "cpl_enabled": _lot_tier[1],  # 仅当 hexp 独立选档真正产出非 none 时才非"极弱压制"
            "c_ai_meta": meta,
        }

    c_ai = float(max(0.0, min(100.0, c_ai)))

    # 耦合总分（方向恒定 HEXP，此分仅用于等级裁决 + 手数分档）
    total = coupling_total(hp_score, c_ai, k, cfg)

    # 1) 等级裁决（否决/降级/保持/升级）——门槛用 0-1 归一（c_ai/100）
    #    passed=False（hexp 拦了但给了方向）也参与裁决：AI 高分可"打开"该信号，
    #    实现双信号融合的真正赋能（任一侧强信号都应能开仓）。
    action, final_grade = adjust_grade(c_ai / 100.0, grade, cfg, passed=passed)

    # 2) 手数分档（low/mid/high/none）：统一用 6 维综合分 scorecard_total 决定，
    #    禁止用耦合总分 total（含 hp_score 强度权重，不稳定）或 hp_score 单维。
    #    none 表示极弱（不发）；实际倍率由风控面板动态手数决定（链动需求）。
    lot_tier = lot_tier_for(scorecard_total, cfg) if _g(cfg, "ai.cpl.enabled") else "none"

    # 否决 → 手数分档置 none（不发信号）
    if action == "VETO":
        lot_tier = "none"
    # AI 打开 hexp 未放行信号 → 标记 ai_opened，供 scheduler 覆盖 threshold_passed
    ai_opened = (action == "UPGRADE" and not passed)

    # 2026-08-17 方案甲：HEXP 已过闸信号的「耦合二次放行门槛」。
    # 仅在 coupled + c_ai 有效路径（即已走到此处）计算；total 低于门槛 → coupling_pass=False，
    # 由 scheduler 侧拦掉（HEXP 已过闸也不下单）。c_ai=None 透传分支（见上方 return）
    # 不携带此字段，scheduler 视 coupling_pass 缺失为兜底放行（HEXP 兜底）。
    coupling_pass = total >= _g(cfg, "hexp.coupling_pass_threshold")

    return {
        "action": action,
        "final_grade": final_grade,
        "lot_tier": lot_tier,
        "total_score": round(total, 2),
        "c_ai": round(c_ai, 2),
        "s_hp": hp_score,
        "ai_opened": ai_opened,
        "coupling_pass": coupling_pass,
        # caller 区分 "none=cpl 未启用(不得压制)" 与 "none=极弱压制"
        "cpl_enabled": bool(_g(cfg, "ai.cpl.enabled")),
        "c_ai_meta": meta,
    }


def _regime_of(k: float, cfg: dict) -> str:
    """k → 市场模式字符串（与 coupling_weight 同一分段，供报表②四模式统计）。

    2026-08-15：新增 EXHAUST 档（k>=k_exhaust_min），与 coupling_weight 对齐。
    """
    if k >= _g(cfg, "ai.cpl.k_exhaust_min"):
        return "EXHAUST"
    if k > _g(cfg, "ai.cpl.k_trend_min"):
        return "TREND"
    if k <= _g(cfg, "ai.cpl.k_range_max"):
        return "RANGE"
    return "NEUTRAL"


async def log_gate_decision(db_pool, signal_id, symbol: str, snapshot: dict, decision: dict, cfg=None):
    """落库闸门决策到 hcm_ai.gate_decision（纯观测，供报表②信号分层统计）。

    可选落库 helper：不改变 decide() 的纯函数性质；db_pool 为 None 时静默跳过。
    调用方（scheduler 接入点）在 decide() 后调用一次；失败不抛出。
    """
    if db_pool is None:
        return
    try:
        cfg = cfg or CFG_FALLBACK
        k = float(snapshot.get("k") or 1.0)
        regime = _regime_of(k, cfg)
        c_ai = decision.get("c_ai")
        await db_pool.execute(
            "INSERT INTO hcm_ai.gate_decision "
            "(signal_id, symbol, direction, regime, hp_score, c_ai, p, action, "
            " orig_grade, final_grade, lot_tier, total_score, passed, c_ai_meta) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
            signal_id,
            symbol,
            snapshot.get("direction"),
            regime,
            decision.get("s_hp"),
            c_ai,
            (float(c_ai) / 100.0) if c_ai is not None else None,
            decision.get("action"),
            snapshot.get("grade"),
            decision.get("final_grade"),
            decision.get("lot_tier"),
            decision.get("total_score"),
            bool(snapshot.get("passed", False)),
            json.dumps(decision.get("c_ai_meta")) if decision.get("c_ai_meta") else None,
        )
    except Exception:
        pass


if __name__ == "__main__":
    # 自测（纯逻辑，不依赖存储）
    cfg = dict(CFG_FALLBACK)
    cfg["ai.enabled"] = True
    cfg["ai.mode"] = "coupled"
    cfg["ai.cpl.enabled"] = True

    snap_pass = {"hp_score": 80.0, "k": 1.5, "grade": "B", "direction": "BUY", "passed": True}
    # 注意 veto_floor=0.30 用严格小于判定：c_ai=30.0（p=0.30）恰在边界上不否决，
    # 走 DOWNGRADE；真正 VETO 需 p<0.30（如 c_ai=25）。
    for c, tag in [(25.0, "否决"), (30.0, "边界降级"), (55.0, "降级"),
                   (65.0, "保持"), (85.0, "升级")]:
        # c_ai 已是 0-100 的纯 LightGBM 分；meta 透传 source 供观测
        # （2026-08-18 解耦：source 只有 lm_only/none，ds_score 为纯观测不入 c_ai）
        meta = {"source": "lm_only", "lm_score": c, "ds_score": None}
        d = decide(snap_pass, c, cfg, c_ai_meta=meta)
        print(f"c_ai={c:<5} [{tag}] action={d['action']:<9} grade={d['final_grade']} "
              f"lot_tier={d['lot_tier']} total={d['total_score']} "
              f"c_ai_meta.src={(d.get('c_ai_meta') or {}).get('source')}")

    # 引擎未放行 + AI 高分 → UPGRADE 并标记 ai_opened（2026-08-14 双信号融合赋能设计：
    # hexp 拦了但给了明确方向时，AI 强票可"打开"该信号；scheduler 据 ai_opened
    # 覆盖 threshold_passed）。若 hexp 连方向都没有（NO_TRADE），上游直接透传不裁决。
    snap_block = dict(snap_pass, passed=False)
    d = decide(snap_block, 90.0, cfg)
    print(f"blocked+high c_ai → action={d['action']} lot_tier={d['lot_tier']} "
          f"ai_opened={d.get('ai_opened')}（AI 赋能打开）")

    # 引擎未放行 + AI 极低分 → VETO（不得开仓）
    d = decide(snap_block, 20.0, cfg)
    print(f"blocked+low c_ai → action={d['action']} lot_tier={d['lot_tier']}"
          f"（否决，不发信号）")

    # 无融合票（c_ai=None）→ 透传纯 HEXP
    d = decide(snap_pass, None, cfg)
    print(f"no c_ai → action={d['action']} lot_tier={d['lot_tier']} c_ai={d['c_ai']}（透传 HEXP）")
