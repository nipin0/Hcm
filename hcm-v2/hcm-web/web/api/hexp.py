"""和乘幂信号配置 API（Hexp / Harmonic Power-Mean）— 独立配置端点.

与 cosource.py 同构但**完全独立**：仅管理 ``hexp.*`` 命名空间配置键，不触碰
``co.*`` / ``scoring.*``。前端「和乘幂」模式 Tab 内嵌配置面板调用本端点读写。

设计原则：
  - 白名单 HEXP_KEYS：仅允许列出的键写入（PATCH 语义，未变化键跳过）。
  - 双写：config_provider.set 已是 PG→Redis 双写 + PUB 失效广播。
  - 读取：未命中配置中心时回退 HEXP_KEYS 默认值（零硬编码）。
  - 默认值与 signal_tower/hexp_engine.py 引擎 _DEFAULTS 完全对齐。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)

# ── 和乘幂配置键白名单（与 hexp_engine.py _DEFAULTS 一一对应）──
HEXP_KEYS: dict[str, Any] = {
    # 总开关与周期
    "hexp.enabled": True,
    "hexp.periods": "M5,H1,H4,D1",
    "hexp.period_minutes": "M1=1,M5=5,M15=15,M30=30,H1=60,H2=120,H4=240,D1=1440",
    "hexp.primary_period": "M5",
    # 配置热重载节流秒数（BUG-16）：调度器未接线时 produce() 惰性自愈刷新的上限
    "hexp.config_reload_interval": 60,
    # 和乘幂核心（幂指数 k 自适应）
    "hexp.k.base": 1.5,
    "hexp.k.min": 0.5,
    "hexp.k.max": 3.0,
    "hexp.k.alpha": 0.8,
    "hexp.k.beta": 0.4,
    "hexp.k.state_trend": 2.0,
    "hexp.k.state_range": 0.65,
    "hexp.k.state_transition": 1.0,
    "hexp.k.state_fade": 2.5,
    # 六因子 + 微结构动量基础权重
    "hexp.factor.adx_weight": 25.0,
    "hexp.factor.er_weight": 25.0,
    "hexp.factor.ma_weight": 20.0,
    "hexp.factor.bbw_weight": 15.0,
    "hexp.factor.hurst_weight": 10.0,
    "hexp.factor.rsi_weight": 5.0,
    "hexp.factor.mm_weight": 15.0,
    # 体制感知因子权重方案（BUG-3）：趋势/震荡/中性三套 7 因子权重 JSON
    "hexp.factor_weights_json": json.dumps({
        "trend":   {"adx": 28.0, "er": 26.0, "ma": 21.0, "bbw": 3.0,  "hurst": 14.0, "rsi": 8.0,  "mm": 11.0},
        "range":   {"adx": 9.0,  "er": 9.0,  "ma": 9.0,  "bbw": 26.0, "hurst": 9.0,  "rsi": 22.0, "mm": 16.0},
        "neutral": {"adx": 25.0, "er": 25.0, "ma": 20.0, "bbw": 15.0, "hurst": 10.0, "rsi": 5.0,  "mm": 15.0},
    }),
    # 因子参数
    "hexp.adx.min": 15.0,
    "hexp.adx.max": 35.0,
    "hexp.er.period": 20,
    "hexp.er.min": 0.10,
    "hexp.er.max": 0.40,
    "hexp.ma.ema_fast": 20,
    "hexp.ma.ema_mid": 50,
    "hexp.ma.ema_long": 100,
    "hexp.ma.align_score": 50.0,
    "hexp.ma.slope_norm_bp": 3.0,
    "hexp.ma.slope_bars": 10,
    "hexp.bbw.window": 120,
    "hexp.bbw.boll_period": 20,
    "hexp.bbw.boll_std": 2.0,
    "hexp.hurst.min": 0.40,
    "hexp.hurst.max": 0.60,
    "hexp.hurst.max_lag": 32,
    # 状态机（迟滞）
    "hexp.state.enter_score": 60.0,
    "hexp.state.exit_score": 40.0,
    "hexp.state.confirm_bars": 1,
    # MTF 迟滞机动量翻转（2026-08-10 方案 A：消除高周期滞后，让裁决"跟手"）
    "hexp.mtf.flip_enabled": True,
    "hexp.mtf.flip_window": 8,
    "hexp.mtf.flip_bars": 2,
    "hexp.mtf.flip_slope_mult": 1.0,
    # 微结构动量（幂律）
    "hexp.mm.alpha": 0.5,
    "hexp.mm.window": 20,
    "hexp.mm.period": "M1",
    "hexp.mm.scale": 0.002,
    "hexp.mm.accel_k_boost": 0.3,
    "hexp.mm.accel_threshold": 0.7,
    # 共振矩阵
    "hexp.mtf.weight_D1": 0.25,
    "hexp.mtf.weight_H4": 0.35,
    "hexp.mtf.weight_H1": 0.25,
    "hexp.mtf.weight_M30": 0.15,
    "hexp.resonance.tailwind_bonus": 0.0,   # 方案 B：顺风加成(默认0=完全对称)
    "hexp.resonance.penalty": 0.15,         # 方案 B：逆风折扣(硬封已移除)
    # B' 延伸（2026-08-17）：逆风/回踩单综合评分(total)额外下压系数。
    # 1.0=不额外罚（默认，向后兼容）；<1.0=逆风时 total 乘性下压，使 grade 更难达 min_grade。
    # 2026-08-21 补入白名单：此前仅在引擎 _DEFAULTS，面板无控件 → 改代码默认重启复原。
    "hexp.resonance.pullback_penalty": 1.0,
    # 已删除的弃用键（2026-08-11 清理）：hexp.resonance.bonus /
    # hexp.mtf.long_threshold / hexp.mtf.short_threshold —— 方案 B 已废除 verdict 硬封，
    # 引擎侧无读取点；保留在白名单只会让面板误以为可调且仍生效。
    # 评分卡（2026-08-18 对齐生产 PG/Redis：resonance 25→12 / state 20→27 /
    # entry 20→26 / pass 45→50 / b 60→52）
    "hexp.scorecard.weight_resonance": 12.0,
    "hexp.scorecard.weight_state": 27.0,
    "hexp.scorecard.weight_entry": 26.0,
    "hexp.scorecard.weight_position": 15.0,
    "hexp.scorecard.weight_vol": 10.0,
    "hexp.scorecard.weight_session": 10.0,
    "hexp.scorecard.pass_threshold": 50.0,
    "hexp.scorecard.b_threshold": 52.0,
    "hexp.scorecard.a_threshold": 75.0,
    "hexp.scorecard.s_hp_min": 60.0,
    "hexp.scorecard.hp_floor": 30.0,
    # 执行参数
    "hexp.exec.sl_atr_mult": 2.0,
    "hexp.exec.rr_min": 1.5,
    "hexp.exec.lot_mult": 1.0,
    "hexp.exec.grade_lot_s": 1.2,
    "hexp.exec.grade_lot_a": 1.0,
    "hexp.exec.grade_lot_b": 0.5,
    "hexp.exec.grade_lot_c": 0.5,
    # 反转态(_REVERSAL)减仓（2026-08-11 B-1）：周期趋势态 TREND_UP↔TREND_DOWN 切换即触发，
    # 独立于 transition_lot_mult、取更谨慎者，避免被其部署值 1.0 拉平。
    "hexp.exec.reversal_lot_mult": 0.5,
    # 反转态持有窗口（主周期棒数，0=仅触发当次）：反转是「态」不是瞬时事件
    "hexp.exec.reversal_hold_bars": 3,
    # 是否把主执行周期(M5)方向切换也计入反转态（默认 False，避免减仓常驻）
    "hexp.exec.reversal_include_primary": False,
    "hexp.exec.transition_lot_mult": 0.5,
    # 极值区手数递减（2026-08-21 方案2）：处于 Donchian 极值区(_in_extreme)且未被护栏
    # 封单的放行单，按此折扣降仓（与 transition/reversal 用 min() 聚合，不叠加打折）。
    # 2026-08-21 补入白名单：此前仅在引擎 _DEFAULTS，面板无控件 → 改代码默认重启复原。
    "hexp.exec.extreme_lot_mult": 0.5,
    # 行情波动率缩放手数（2026-08-21 方案2）：ATR 相对常态缩放，让风控基础手数随行情调整。
    "hexp.exec.vol_scale_enabled": True,
    "hexp.exec.vol_scale_atr_ref": 7.0,
    "hexp.exec.vol_scale_min": 0.5,
    "hexp.exec.vol_scale_max": 1.0,
    # 方向判定
    "hexp.direction_min_score": 0.20,
    # 位置因子（BUG-1 修复 2026-08-13）：Donchian 分位→均值回归方向因子参与方向裁决，
    # 底部托 BUY / 顶部压 SELL / 中部无影响，根治"高多低空"。
    "hexp.pos_factor.enabled": True,
    "hexp.pos_factor.weight": 0.15,
    # 精确信号闸门：最低可下单分级（S/A/B/C）。低于此级只落库观测、不产交易方向。
    "hexp.min_grade": "C",
    # 部署意图档位：一键诊断 min_grade_drift 节点的"锚点"。换档位只改此键即可，
    # 诊断漂移判定与自愈 calibrate_min_grade 均读此键把 hexp.min_grade 对齐到这里。
    # 默认 "B"（2026-08-15 用户确认的有意档位）。
    "hexp.min_grade_intended": "B",
    # 极值硬闸门（2026-08-13 治「高位做多/低位做空」）：价格相对 Donchian 通道分位
    # 触及极值时禁止顺势追单。命中即 NO_TRADE（reason=hexp_extreme_guard）。
    "hexp.extreme.high_pct": 0.85,
    "hexp.extreme.low_pct": 0.15,
    "hexp.extreme.donchian_look": 20,
    # 极值动量感知闸门（2026-08-13 升级·替换无条件硬封）：极值区仅当 mm 动量回撤才封单
    "hexp.extreme.mm_retreat_enabled": True,
    # BUG-3 修复(2026-08-13)：0.05→0.20，要求 M1 动量明显同向才放行极值追单
    "hexp.extreme.mm_retreat_min": 0.20,
    # 回踩支撑位诊断（B）：回看窗口取近期摆动低(BUY)/高(SELL)，现价落入 ±support_atr×ATR 即标记
    "hexp.extreme.support_lookback": 20,
    "hexp.extreme.support_atr": 1.0,
    # 极值追单收紧 SL 距离（C）：extreme_chase 时 SL 的 ATR 倍数 ×此值（默认 0.7），TP 不变→R:R 改善
    "hexp.extreme.chase_sl_mult": 0.7,
    # 极值反转护栏（2026-08-18）：顶/底极值区 + 动量减弱且反向 + 长影线 → 拦原趋势延续单
    "hexp.extreme.reversal_enabled": True,
    "hexp.extreme.wick_min": 0.60,
    "hexp.extreme.reversal_sl_atr_mult": 0.5,
    # 极值护栏自动开/关（2026-08-19）：auto_mode=auto 时按 M5 regime 自动推导开关
    "hexp.extreme.auto_mode": "off",
    "hexp.extreme.auto_on_regimes": "RANGE,NEUTRAL",
    "hexp.extreme.auto_off_regimes": "TREND,PRE_TREND",
    # 极值识别 k 补充阈值（2026-08-21 方案B）：_in_extreme 用 k 作 _pos_pct 的 OR 补充。
    # 双条件：k>k_extreme 且 pos 已接近极值侧(>k_pos_high / <k_pos_low) 才视为真极值追单，
    # 避免剧烈行情下通道中部(pos 0.3~0.7)被 k>阈值误判而批量拦截卡死。
    "hexp.extreme.k_extreme": 1.8,
    "hexp.extreme.k_pos_high": 0.7,
    "hexp.extreme.k_pos_low": 0.3,
    # 动量枯竭保护（2026-08-21）：极值盲区补充——pos 高位 + 趋势质量差(er 低) + 微动量枯竭(mm 近 0)
    # 三条件齐拦原趋势延续单（高位接刀/追顶）。独立于 _in_extreme，真趋势 er>0.22 不受影响。
    "hexp.momentum_drain_enabled": True,
    "hexp.momentum_drain_hi": 0.65,
    "hexp.momentum_drain_er": 0.20,
    "hexp.momentum_drain_mm": 0.15,
    # 动量方向否决（2026-08-21）：动量明确反向时拦逆动量单（BUY 而 mm<0、SELL 而 mm>0）。
    # 不依赖 pos/趋势结构，让"动量转负即不再追多/追空"（比 momentum_drain 更敏捷的硬护栏）。
    "hexp.momentum_flip_enabled": True,
    "hexp.momentum_flip_mm": 0.04,
    # 反向单观测（2026-08-21，先观测不下单）：momentum_flip 判动量反向且处于高位/低位时，
    # 记录反向候选落库到 indicator_values._hexp.reverse_candidate，供后续 SQL 对照评估。
    # 2026-08-21 补入白名单：此前仅在引擎 _DEFAULTS，面板无控件 → 改代码默认重启复原。
    "hexp.reverse_candidate_enabled": True,
    "hexp.reverse_candidate_hi": 0.7,
    "hexp.reverse_candidate_lo": 0.3,
    # 方案 B (2026-08-19) zone 硬闸门：把「方向逆着结构位开仓」挡在门外。
    # 与 hexp.extreme.* (Donchian 极值反转陷阱) 互补、串联：extreme 拦极值接刀单，
    # 本闸门拦「现价已显著越过方向对齐结构位」的逆结构位单。
    # 默认关闭(hard_block_enabled=False)，需显式开启；atr_mult 控制「越过多远算逆结构」。
    "hexp.zone.hard_block_enabled": False,
    "hexp.zone.hard_block_atr_mult": 0.3,
}


async def _read_config(config_provider: Any) -> dict[str, Any]:
    """从 ConfigProviderV3 读取全量 hexp.* 配置；未读到值回退 HEXP_KEYS 默认值。

    2026-08-21 修复「保存刷新又复原」根因：改用 ``get_current``（只读
    ``current_value``、不做 PG ``default_value`` 兜底）。原因是 PG ``metadata``
    表中早期 seed 的 ``default_value``（如 pass=45/b=60/resonance=25）已与代码
    ``HEXP_KEYS``（pass=50/b=52/resonance=12）漂移；若此处沿用
    ``config_provider.get()`` 的 ``COALESCE(current_value, default_value)``，
    未显式保存的键会读到旧的 seed 默认值并回填到表单，用户一次全量保存就把
    旧值写回 ``current_value``，把引擎想用的新默认覆盖掉，表现为"刷新又复原"。
    """
    result: dict[str, Any] = {}
    for key, default in HEXP_KEYS.items():
        try:
            raw = await config_provider.get_current(key)
        except Exception:
            raw = None
        if raw is not None:
            try:
                result[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                result[key] = raw
        else:
            result[key] = default
    return result


def _normalize_config_body(body: Any) -> dict:
    """把前端两种传参约定统一成扁平 dict {key: value}。"""
    if not isinstance(body, dict):
        return {}
    updates = body.get("updates")
    if isinstance(updates, list):
        flat: dict = {}
        for item in updates:
            if isinstance(item, dict) and "config_key" in item:
                flat[item["config_key"]] = item.get("value")
        return flat
    return body


def _serialize_value(value: Any) -> str:
    """把前端提交值序列化成配置中心存储用字符串。

    dict / list 必须走 ``json.dumps``，**绝不能用 ``str()``**：
    ``_read_config`` 读取时会对 JSON 值做 ``json.loads``（例如
    ``hexp.factor_weights_json`` 会以 dict 形式回给前端），面板原样回传后若用
    ``str(dict)`` 落库，写进去的是 Python repr（单引号）——引擎侧
    ``json.loads`` 必然抛错并静默回退中性权重，导致"体制感知权重"名存实亡。
    这是一次面板保存就能永久打坏的闭环自损，2026-08-11 修复。
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def _write_config(config_provider: Any, body: dict) -> dict:
    """逐键写入 ConfigProviderV3（PG→Redis 双写 + PUB）。PATCH 语义：未变化键跳过。"""
    results: list[dict] = []
    success_count = 0
    fail_count = 0
    skip_count = 0

    for key, value in body.items():
        if key not in HEXP_KEYS:
            skip_count += 1
            results.append({"key": key, "status": "skipped", "error": "not in whitelist"})
            continue
        new_val = _serialize_value(value)
        try:
            current = await config_provider.get(key)
        except Exception:
            current = None
        if current is not None and current == new_val:
            skip_count += 1
            results.append({"key": key, "status": "unchanged"})
            continue
        try:
            ok = await config_provider.set(key, new_val)
            if ok:
                success_count += 1
                results.append({"key": key, "status": "ok"})
            else:
                fail_count += 1
                results.append({"key": key, "status": "failed"})
        except Exception as exc:
            fail_count += 1
            results.append({"key": key, "status": "error", "error": str(exc)})

    # 2026-08-21 修复「可下单等级保存后刷新又复原」：
    # web/system.py 的自愈校准 calibrate_min_grade 会把 hexp.min_grade 强制锚定回
    # 部署意图 hexp.min_grade_intended（默认 B）。若只改 min_grade 不同步 intended，
    # 一次诊断自愈就把用户选档拉回 B。这里在保存 min_grade 时同步 intended，使
    # 部署意图跟随用户显式选择（无论从面板还是 API 触发，都保持一致）。
    if "hexp.min_grade" in body and "hexp.min_grade_intended" not in body:
        _mg_val = _serialize_value(body["hexp.min_grade"])
        try:
            ok = await config_provider.set("hexp.min_grade_intended", _mg_val)
            if ok:
                success_count += 1
                results.append({"key": "hexp.min_grade_intended",
                                "status": "ok",
                                "note": "synced from hexp.min_grade"})
        except Exception as exc:
            fail_count += 1
            results.append({"key": "hexp.min_grade_intended",
                            "status": "error", "error": str(exc)})

    return {
        "results": results,
        "success": success_count,
        "failed": fail_count,
        "skipped": skip_count,
        "total": len(body),
    }


def create_hexp_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
    redis_client: Any = None,
) -> APIRouter:
    """创建和乘幂信号配置路由（factory 函数被 main.py 的 startup() 调用）。

    - config_provider: PG↔Redis 双写的配置中心（读写 hexp.* 命名空间）
    - redis_client: 读取 hcm:live:hexp:{symbol} 实时信号快照（信号塔面板数据源）
    """
    router = APIRouter(tags=["hexp"])

    # ── 实时信号快照（和乘幂面板数据源）──
    @router.get("/api/v1/hexp/signal/{symbol}")
    async def get_hexp_signal(
        symbol: str,
        request: Request,
        user: HTTPAuthorizationCredentials = Depends(auth_handler.require_auth),
    ):
        """读取和乘幂实时信号快照。

        数据来自信号塔 hexp 引擎 live 发布：每 ~3s 重算并写入
        ``hcm:live:hexp:{symbol}``（TTL 15s）。未激活 / 无数据返回 data=null。
        """
        if redis_client is None or not getattr(redis_client, "is_initialized", False):
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Redis not available"}
        try:
            raw = await redis_client.get(f"hcm:live:hexp:{symbol.upper()}")
            if not raw:
                return {"code": 0, "data": None, "message": "no_signal_yet"}
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            for k in ("hp_score", "k", "mm", "verdict", "scorecard_total", "close", "atr"):
                if k in data and data[k] is not None:
                    try:
                        data[k] = float(data[k])
                    except (TypeError, ValueError):
                        pass
            return {"code": 0, "data": data, "message": "ok"}
        except Exception as exc:
            logger.error("Hexp signal read failed: %s", exc)
            return {"code": "WB_HEXP_004", "data": None, "message": str(exc)}

    # ── AI 信号质量评分快照（和乘幂面板三分数卡数据源）──
    @router.get("/api/v1/hexp/ai/{symbol}")
    async def get_hexp_ai_snapshot(
        symbol: str,
        request: Request,
        user: HTTPAuthorizationCredentials = Depends(auth_handler.require_auth),
    ):
        """读取 AI 信号质量评分快照。

        数据来自独立 sidecar ``quality_scorer.py`` 发布的 ``hcm:live:hexp:ai:{symbol}``
        （TTL 15s，含 ai_score/total_score/ext_factor_score）。未启用/无 sidecar → data=null。
        """
        if redis_client is None or not getattr(redis_client, "is_initialized", False):
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Redis not available"}
        try:
            raw = await redis_client.get(f"hcm:live:hexp:ai:{symbol.upper()}")
            if not raw:
                return {"code": 0, "data": None, "message": "no_ai_snapshot"}
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return {"code": 0, "data": json.loads(raw), "message": "ok"}
        except Exception as exc:
            logger.error("Hexp AI snapshot read failed: %s", exc)
            return {"code": "WB_HEXP_005", "data": None, "message": str(exc)}

    async def _get_config(
        request: Request,
        user: HTTPAuthorizationCredentials = Depends(auth_handler.require_auth),
    ):
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None,
                    "message": "Config provider not available"}
        try:
            data = await _read_config(config_provider)
            return {"code": 0, "data": data, "message": "ok"}
        except Exception as exc:
            logger.error("Hexp config read failed: %s", exc)
            return {"code": "WB_HEXP_001", "data": None, "message": str(exc)}

    async def _put_config(
        body: dict,
        request: Request,
        user: HTTPAuthorizationCredentials = Depends(auth_handler.require_auth),
    ):
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None,
                    "message": "Config provider not available"}
        try:
            flat_body = _normalize_config_body(body)
            summary = await _write_config(config_provider, flat_body)
            code = 0 if summary["failed"] == 0 else "WB_HEXP_003"
            msg = (
                f"ok ({summary['success']} written, {summary['skipped']} skipped)"
                if summary["failed"] == 0
                else f"partial ({summary['success']}/{summary['total']} succeeded, {summary['failed']} failed)"
            )
            return {"code": code, "data": summary, "message": msg}
        except Exception as exc:
            logger.error("Hexp config write failed: %s", exc)
            return {"code": "WB_HEXP_002", "data": None, "message": str(exc)}

    router.add_api_route(
        "/api/v1/hexp/config",
        _get_config,
        methods=["GET"],
        summary="Read all hexp (和乘幂) configuration keys",
    )
    router.add_api_route(
        "/api/v1/hexp/config",
        _put_config,
        methods=["PUT"],
        summary="Batch update hexp (和乘幂) configuration keys",
    )
    # Legacy（隐藏）
    router.add_api_route(
        "/api/hexp/config",
        _get_config,
        methods=["GET"],
        summary="[Legacy] Read all hexp configuration keys",
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/hexp/config",
        _put_config,
        methods=["PUT"],
        summary="[Legacy] Batch update hexp configuration keys",
        include_in_schema=False,
    )

    return router
