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
    # 【阶段 1·方向共振】dir_lm(方向头)与 dir_hexp(HEXP 方向)共振，仅否决/增强，
    # 绝不独立开方向（铁律）。默认关闭：不共振（仅保留删 ai_opened 的纪律修正）。
    "ai.lm.direction_fuse": False,
    "ai.lm.dir_veto_prob": 0.65,      # 反向且 dir_lm 概率≥此值 → VETO(反向否决)
    # 【阶段 2·买点共振】entry_lm(买点头)好买点概率 ai_entry(0-1) 与 hexp entry_quality 共振，
    # 仅否决/增强入场时机质量，绝不独立开方向（铁律）。默认关闭。
    "ai.lm.entry_fuse": False,
    "ai.lm.entry_boost_prob": 0.60,   # ai_entry≥此值 → 增强好买点(boost_good_entry)
    "ai.lm.entry_veto_prob": 0.35,    # ai_entry≤此值 → 否决差买点(veto_bad_entry)
    # 【杠杆1·PULLBACK 追单抑制 2026-09-04】ai_state=PULLBACK（回调态）且拟开方向与
    # M5 微动量(ai_mm, sidecar lm_features.mm)相反 → 逆动量追单（大跌后反弹里追空/接刀），
    # 直接 VETO。默认启用；mm_abs 过低会拦到噪声，过高则失灵。
    "ai.lm.pullback_chase_enabled": True,
    "ai.lm.pullback_chase_mm_abs": 0.3,
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
    # 【2026-08-31 口径修正·真因回归】scorecard_total 权威口径 = 0–100
    # （与风控 risk_min_confidence 的 0–100 制对齐，见 scheduler.py:2252-2256）。
    #
    # 历史口径迁移轨迹（实测 hcm_signal.signals.confidence 按天分布，n=1398）：
    #   - 2026-08-27 及之前：scorecard_total 为 0~1  口径（各日 max=1.00）
    #   - 2026-08-28 起    ：scorecard_total 迁移为 0–100 口径（avg 56.47、max 85.17）
    # 而 2026-08-28 那次"口径修正"误按 0~1 标定 tier 阈值（0.65/0.55/0.45），
    # 与迁移后的 0–100 输入失配 → 阈值恒被越过 → lot_tier 恒 "high"（1.5×）
    # → **动态手数分档完全失效，且系统性按最高档放大于每一笔信号**；
    # 解耦态走的是同一 lot_tier_for 路径（decide 透传分支），故同样恒 high。
    #
    # 现按 0~1 → 0–100 等价映射（×100）把阈值改为 65/55/45，并用 0–100 真实分布复核：
    #   实测（2026-08-28 起，n=262）：min=26.94 p25=48.94 p50=57.16 p75=65.30
    #                                p90=73.09 max=85.17
    #   覆盖：low>=45 ≈78%    mid>=55 ≈53%    high>=65 ≈25%
    # 与原设计覆盖意图（low 75% / mid 55% / high 20%）一致。
    # 下游风控 risk.score_tier_* 为同一口径问题，须同步 ×100（52/70/90），否则
    # ai_lot_tier=none 回退到 score 分档时仍会失配。
    "ai.cpl.tier_high": 65.0,
    "ai.cpl.tier_mid": 55.0,
    "ai.cpl.tier_low": 45.0,
    # 【2026-08-31 动态手数链动·纠偏】耦合模式手数档位【不】在 quality_gate 预选、
    # 也【不】新增任何配置键；改为把触发下单的耦合分 total(0–100，即「下单分」)作为信号
    # confidence 透传给风控引擎，由【既有】风控面板动态手数规则裁决：
    #   risk.score_tier_low/mid/high(生产=0.50/0.80/0.95) 对 confidence 分档 →
    #   下单分<80→low(×0.5) / 80≤下单分≤95→mid(×1.0) / 下单分>95→high(×1.5)，
    #   倍率由 risk.lot_multiplier_{low|mid|high}(0.5/1.0/1.5) 决定。
    # 上述 risk.score_tier_* / risk.lot_multiplier_* 均为风控面板既有参数，零新增键。
    # quality_gate 在耦合路径只返回 lot_tier="none"（交风控按 confidence 现算档位）；
    # 解耦/HEXP 独立路径仍用本 tier_*(65/55/45) 经 lot_tier_for 选档(历史口径)。
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
    """耦合总分 = w(k)·S_hp + (1-w(k))·C_ai。

    hp_score: HEXP 综合评分；decide 调用方传入 scorecard_total（HEXP 6 维综合分，
    0–100），不再用强度单维 hp_score（详见 2026-08-31 耦合分下单需求）。
    c_ai: LightGBM 单源分（0–100）= ai_score。
    两者同 0–100 口径，故总分亦 0–100，与 hexp.coupling_pass_threshold 同口径可比。
    """
    w = coupling_weight(k, cfg)
    return w * hp_score + (1.0 - w) * c_ai


def lot_tier_for(total: float, cfg: dict, prefix: str = "tier") -> str:
    """综合分 → 手数分档（low/mid/high/none）。

    **0–100 口径**。prefix 选择阈值键集：
      - "tier"（默认/唯一在用）：ai.cpl.tier_high/mid/low（65/55/45），用于【解耦 / HEXP 独立】
        模式，基准=scorecard_total（HEXP 6 维综合分，旧规则）。

    【2026-08-31 纠偏】耦合模式【不再】经本函数预选档位、也【不】新增任何配置键。
    耦合路径在 decide() 中固定返回 lot_tier="none"，由 scheduler 把耦合分 total 透传为
    信号 confidence，最终交【既有】风控面板 risk.score_tier_{low,mid,high} 规则裁决
    （下单分<80→low / 80≤≤95→mid / >95→high，倍率 risk.lot_multiplier_*）。故 "lot_tier"
    前缀分支已废弃、不再被调用。
    返回的是「档位语义」而非固定倍率——实际倍率由风控面板的动态手数
    配置（risk.lot_multiplier_{low|mid|high} = 0.5/1.0/1.5）决定，
    实现「AI 只选档、风控定准确下单值」的链动设计（2026-08-14 需求）。
    """
    hk = f"ai.cpl.{prefix}_high"
    mk = f"ai.cpl.{prefix}_mid"
    lk = f"ai.cpl.{prefix}_low"
    if total >= _g(cfg, hk):
        return "high"
    if total >= _g(cfg, mk):
        return "mid"
    if total >= _g(cfg, lk):
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
      - p ≥ up_threshold 且 hexp 已放行(passed) → UPGRADE（升一级，增强已放行信号）；
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
    # 【阶段 1·铁律】UPGRADE 仅当 hexp 已放行(passed=True)。passed=False（hexp 已拦）
    # 时 AI 不得 UPGRADE 打开信号（绝不越过 HEXP 闸门独立开仓）。
    if passed and p >= up_th:
        return "UPGRADE", GRADE_ORDER[min(len(GRADE_ORDER) - 1, idx + 1)]
    # HOLD：hexp 未放行但 AI 中等分 → 【阶段 1·纪律修正】不再"UPGRADE 打开"。
    # 铁律：AI 绝不独立开出 HEXP 没给的方向。故 hexp 未放行(passed=False)的信号，
    # AI 再高分也只能 HOLD（不打开），绝不允许 AI 越过 HEXP 闸门开仓。
    return "HOLD", grade


def decide(
    snapshot: dict,
    c_ai: Optional[float],
    cfg: dict,
    c_ai_meta: Optional[dict] = None,
    ai_direction: Optional[str] = None,
    ai_dir_prob: Optional[float] = None,
    ai_entry: Optional[float] = None,
    ai_state: Optional[str] = None,
    ai_mm: Optional[float] = None,
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
      ai_state  sidecar 状态头输出（如 "PULLBACK"），供杠杆1追单抑制。
      ai_mm     sidecar lm_features.mm（M5 微动量，tanh 归一），供杠杆1判动量方向。

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
            _tier = lot_tier_for(scorecard_total, cfg, "tier")
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

    # 耦合总分（方向恒定 HEXP）：融合 S_hp=scorecard_total(HEXP 6维综合) + C_ai(ai_score)。
    # 2026-08-31：S_hp 改用 scorecard_total（6维）而非 hp_score（强度单维），
    # 满足"用 LightGBM ai_score 和 scorecard_total(HEXP 6维) 耦合分下单"。
    total = coupling_total(scorecard_total, c_ai, k, cfg)

    # 1) 等级裁决（否决/降级/保持/升级）——门槛用 0-1 归一（c_ai/100）
    #    铁律：HEXP 闸门是开仓唯一总开关。passed=False（hexp 已拦）的信号，
    #    AI 再高分也只能 HOLD（不打开）——绝不允许 AI 越过 HEXP 闸门独立开仓
    #    （2026-08-28 阶段 1·纪律修正：删原 ai_opened 打开逻辑）。
    action, final_grade = adjust_grade(c_ai / 100.0, grade, cfg, passed=passed)

    # ── RANGE 均值回归：豁免常规分数否决（2026-09-10 用户决策）──
    # 背景：RANGE 信号【天生低分】——它做的是逆动量交易，而质量模型是按
    #   "顺势/高质量" 学出来的（实测 RANGE 的 SELL 侧被系统性压到 lm=20.0 地板）。
    #   生产 veto_floor=0.15（即 c_ai=15），RANGE 的 c_ai 恒在 20 附近，余量仅 5 分：
    #   一旦模型或行情让分下探，连 BUY 也会被常规否决 → 策略归零。
    # 决策：RANGE 不参与常规分数否决。但【保留低分可见性】——转 DOWNGRADE（降一级）
    #   而非 HOLD，不把低质量信号伪装成高质量。
    # 风控不因此消失：ai_lot_tier="low" → 确定性 0.5× 手数；TP=1.0ATR 小止盈 +
    #   会话宽止损；总开关 range.enabled 可一键停。
    if snapshot.get("range_mode", False) and action == "VETO":
        _rv_idx = grade_index(grade)
        action, final_grade = "DOWNGRADE", GRADE_ORDER[max(0, _rv_idx - 1)]

    # 【杠杆1·PULLBACK 追单抑制 2026-09-04】回调态 + 逆 M5 微动量 → 追单/接刀，直接 VETO。
    # 场景实证：H1 深跌后 V 型反弹，方向头仍 0.72 置信追空被反打。此处拦截
    # 「HEXP 方向与当前微动量相反」的逆势追单（SELL 但 mm 上行 / BUY 但 mm 下行）。
    # 输入 ai_mm=sidecar lm_features.mm；仅 hexp 已放行(passed)时拦；mm 缺失不拦。
    pullback_chase = "none"
    # ── RANGE 均值回归豁免（2026-09-10）──
    # 本规则否决「逆动量交易」：SELL 且 M5 微动量上行 / BUY 且微动量下行。
    # 而【均值回归的定义就是逆动量交易】——二者根本冲突：
    #   · RANGE 的启用前提(H1/H4/D1 全 RANGE)下，sidecar 的 ai_state 恰恰恒为
    #     "PULLBACK"（实测当前 ai_state=PULLBACK、ai_mm=+0.61），故该规则对
    #     RANGE 是【必然命中】而非偶然。
    #   · 实测 14 次注入：7 次 SELL 的 lm 分全被压到 20.0 并 100% VETO，
    #     仅 1 次 BUY 因动量温和(|mm|<0.3)放行 → 表现为"只出 BUY、从不出 SELL"。
    # 故 RANGE 豁免本规则。风险控制不因此消失：RANGE 另有 TP=1.0ATR 小止盈
    # + ai_lot_tier="low"(0.5× 手数) 兜底。
    if (passed and _g(cfg, "ai.lm.pullback_chase_enabled")
            and ai_state == "PULLBACK" and direction in ("BUY", "SELL")
            and ai_mm is not None
            and not snapshot.get("range_mode", False)):
        _pbc_abs = _g(cfg, "ai.lm.pullback_chase_mm_abs")
        if _pbc_abs > 0 and (
                (direction == "SELL" and ai_mm >= _pbc_abs) or
                (direction == "BUY" and ai_mm <= -_pbc_abs)):
            action, final_grade, lot_tier = "VETO", grade, "none"
            pullback_chase = "veto_pullback_chase"

    # 【阶段 1·方向共振】dir_lm(方向头) × dir_hexp(HEXP 方向)：
    #   仅否决/增强 action，绝不改 snapshot.direction（方向永远由 HEXP 决定）。
    #   灰度：ai.lm.direction_fuse=false 时不参与（仅保留上方删 ai_opened 纪律修正）。
    dir_resonance = "none"
    if _g(cfg, "ai.lm.direction_fuse") and ai_direction not in (None, "HOLD", "NO_TRADE"):
        dir_hexp = direction
        _p_veto = _g(cfg, "ai.lm.dir_veto_prob")
        _opp = {"BUY": "SELL", "SELL": "BUY"}
        if dir_hexp in ("BUY", "SELL"):
            if ai_direction == _opp.get(dir_hexp) and (ai_dir_prob or 0.0) >= _p_veto:
                # 反向否决：dir_lm 高置信反向 → 否决该信号（仅拒绝，不改方向，铁律友好）
                action, final_grade, lot_tier = "VETO", grade, "none"
                dir_resonance = "veto_reverse"
            elif ai_direction == dir_hexp:
                # 同向增强：hexp 已放行(passed)的已开信号升级一级增强置信；
                # hexp 未放行(passed=False)不打开（铁律：不越过 HEXP 闸门）。
                if passed and action in ("HOLD", "DOWNGRADE"):
                    _idx = grade_index(grade)
                    action = "UPGRADE"
                    final_grade = GRADE_ORDER[min(len(GRADE_ORDER) - 1, _idx + 1)]
                    dir_resonance = "boost_same"

    # 【阶段 2·买点共振】entry_lm(买点头)好买点概率 ai_entry(0-1) × hexp 入场质量：
    #   仅否决/增强入场时机质量，绝不改 snapshot.direction（方向永远由 HEXP 决定，铁律友好）。
    #   灰度：ai.lm.entry_fuse=false 时不参与。与 dir_resonance 平行、独立维度，VETO 优先。
    entry_resonance = "none"
    if _g(cfg, "ai.lm.entry_fuse") and ai_entry is not None:
        _e_boost = _g(cfg, "ai.lm.entry_boost_prob")
        _e_veto = _g(cfg, "ai.lm.entry_veto_prob")
        if ai_entry <= _e_veto:
            # 否决差买点：点位质量差 → 否决该信号（仅拒绝入场时机，不改方向）。
            action, final_grade, lot_tier = "VETO", grade, "none"
            entry_resonance = "veto_bad_entry"
        elif ai_entry >= _e_boost:
            # 增强好买点：hexp 已放行(passed)的已开信号升级一级增强入场质量置信；
            # hexp 未放行(passed=False)不打开（铁律：不越过 HEXP 闸门）。
            if passed and action in ("HOLD", "DOWNGRADE"):
                _idx = grade_index(grade)
                action = "UPGRADE"
                final_grade = GRADE_ORDER[min(len(GRADE_ORDER) - 1, _idx + 1)]
                entry_resonance = "boost_good_entry"

    # 2) 手数分档（low/mid/high/none）：耦合模式【不再】在信号塔预选档位、
    #    【不】新增任何配置键（贴合用户 2026-08-31 指令）。改为把触发下单的耦合分
    #    total(0–100，即「下单分」) 经 decide 返回为 total_score，由 scheduler 透传为信号
    #    confidence，最终交【既有】风控面板动态手数规则裁决：
    #      risk.score_tier_low/mid/high(生产=0.50/0.80/0.95) 对 confidence 分档 →
    #      下单分<80→low(×0.5) / 80≤下单分≤95→mid(×1.0) / 下单分>95→high(×1.5)，
    #      倍率由 risk.lot_multiplier_{low|mid|high}(0.5/1.0/1.5) 决定（均风控面板既有参数）。
    #    故此处固定返回 lot_tier="none"，交风控按 confidence 现算档位（零新增键）。
    #    none 表示"档位交由风控"；实际倍率由风控面板动态手数决定（链动需求）。
    lot_tier = "none"

    # 否决 → 手数分档置 none（不发信号）
    if action == "VETO":
        lot_tier = "none"
    # 【阶段 1·纪律修正】原 ai_opened（AI 打开 hexp 未放行信号）已永久删除：
    # 铁律要求 AI 绝不独立开出 HEXP 没给的方向，故恒为 False（保留字段兼容 scheduler）。
    ai_opened = False

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
        "dir_resonance": dir_resonance,  # 阶段 1·方向共振诊断: none/boost_same/veto_reverse
        "entry_resonance": entry_resonance,  # 阶段 2·买点共振诊断: none/boost_good_entry/veto_bad_entry
        "pullback_chase": pullback_chase,  # 杠杆1·追单抑制诊断: none / veto_pullback_chase
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
