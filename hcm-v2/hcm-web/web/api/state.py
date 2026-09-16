"""FSM 行情状态机监控 API（只读）— [2026-09-15]

供 WEB「数据看板 → 状态机看板」(`/dashboard/fsm`) 使用。

数据源（**全部只读**：不写配置、不触发决策、不碰下单链路）：
  1. Redis 快照（实测 4/7 键存活）
       hcm:state:fsm:{SYM}        无 TTL   FSMState 全字段（含 pending_streak 防抖计数）
       hcm:live:state:{SYM}       TTL 300  实时快照（proba 4 类 + intent.box_* + model_version）
       hcm:state:directive:{SYM}  TTL 900  下单指令（trail_mult / exit_ready / trail_lookback）
       hcm:state:ctx:{SYM}        无 TTL   StrategyContext（box_upper/lower/mid + box_frozen）
  2. PG hcm_signal.market_state_log   历史逐 bar（29 列，含 prob_* 四列 + transitioned）
  3. PG hcm_market.klines_xauusd      K 线（周期列名是 time_frame；与状态按 open_time=bar_open_time JOIN，已实测可匹配）
  4. PG hcm_trading.positions         当前持仓（列名是 lot，不是 volume）
  5. PG hcm_trading.orders            当日亏损（口径同 hcm-risk-engine/risk_engine/rule_chain.py:_get_daily_loss）

已实测的坑（勿按直觉假设）：
  · 状态是 **7 态字符串枚举** S0_IDLE / S1_OSC / S2_TREND_INIT / S3_TREND_MID /
    S4_TREND_FADE / S5_OSC_LOCKED / S9_PAUSED，**不是 4 态**（"4 类"指模型类别）。
  · ctx **无 box_height 字段** → 本模块自算 upper−lower 返回 `derived.box_height`。
  · `hcm:state:pause|osc_atr_loss|osc_loss_count:{SYM}` 三键**实测不存在**（osc_atr_loss 作字段内嵌在 fsm 快照里）。
  · market_state_log **无 signal_id / signal_mode 列** → 本模块不返回这两项，前端如实留空，不做伪关联。
  · 趋势的 slope_atr / +DI / −DI **线上未发布**（scheduler.py:2231 只取了 name）→ 本模块返回 null 并标注原因。
  · **asyncpg 不接受 str 形式的 timestamptz 参数**（`$n::timestamptz` 传 str 会抛
    `invalid input for query argument`），而 _fetch 吞异常 → 静默变空结果。
    /logs 的 since/until 必须过 `_parse_ts()` 转成 datetime。此坑由本文件自测脚本抓到。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

logger = logging.getLogger(__name__)

# market_state_log 全列（29 列，与 deploy/migrations/0033~0034 一致）
_LOG_COLS = (
    "id, symbol, time_frame, bar_open_time, state, prev_state, transitioned, "
    "predicted_class, prob_oscillation, prob_trend_init, prob_trend_mid, prob_trend_fade, "
    "margin, decided, infer_ok, infer_reason, hold_only, model_version, positions_open, note, "
    "created_at, intent_action, intent_direction, intent_lot_mult, intent_reason, "
    "age_bars, direction, trigger_on, trigger_reason"
)

# 箱体/防抖相关配置键（实测存在；缺失时回落默认值，见 _cfg_num）
_CFG_KEYS = {
    "box_window": ("state.box.window", 20.0),
    "box_min_width_atr": ("state.osc_box_min_width_atr", 1.0),
    "debounce_k_enter": ("state.debounce.k_enter", 2.0),
    "debounce_k_exit": ("state.debounce.k_exit", 2.0),
    "debounce_k_fade": ("state.debounce.k_fade", 2.0),
    "dir_debounce_bars": ("state.dir.debounce_bars", 3.0),
    "dir_slope_thr_atr": ("state.dir.slope_thr_atr", 1.0),
    "horizon_bars": ("state.horizon_bars", 12.0),
    "max_daily_loss": ("risk.max_daily_loss", 200.0),
}

_STATE_CN = {
    "S0_IDLE": "空闲",
    "S1_OSC": "震荡",
    "S2_TREND_INIT": "趋势初生",
    "S3_TREND_MID": "趋势中段",
    "S4_TREND_FADE": "趋势衰竭",
    "S5_OSC_LOCKED": "震荡锁止",
    "S9_PAUSED": "暂停",
}


# ─────────────────────────────────────────────────────────────────────────────
# 低层 helper（照 ai_ops.py / ai_report.py 既有写法：模块级 + 吞异常 + 降级返回 None）
# ─────────────────────────────────────────────────────────────────────────────
async def _fetch(db_pool, sql: str, *args):
    if db_pool is None:
        return None
    try:
        return await db_pool.fetch(sql, *args)
    except Exception as exc:  # noqa: BLE001
        logger.error("state query failed: %s", exc)
        return None


async def _fetchrow(db_pool, sql: str, *args):
    if db_pool is None:
        return None
    try:
        return await db_pool.fetchrow(sql, *args)
    except Exception as exc:  # noqa: BLE001
        logger.error("state fetchrow failed: %s", exc)
        return None


async def _redis_json(redis_client, key: str):
    """读 Redis string 键并按 JSON 解析；缺键/非 dict/异常一律返回 None。"""
    if redis_client is None or not getattr(redis_client, "is_initialized", False):
        return None
    try:
        raw = await redis_client.get(key)
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("state redis read failed (%s): %s", key, exc)
        return None


def _parse_ts(s: str):
    """ISO 字符串 → aware datetime；空/非法返回 None。

    ⚠️ 实测坑（本文件自测抓到）：asyncpg 对 `$n::timestamptz` 参数**只接受
    datetime 对象，传 str 会抛** `invalid input for query argument ... got 'str'`。
    而 _fetch 是吞异常的（照既有 ai_ops.py 写法），异常会被静默转成「空结果」——
    表现为前端只说「暂无数据」，看不到任何报错。故必须在此处显式转换。
    """
    if not s or not str(s).strip():
        return None
    try:
        v = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        logger.warning("state: unparsable timestamp %r -> ignored", s)
        return None


def _rows_to_dicts(rows):
    """asyncpg Record → 可 JSON 序列化的 dict（datetime → isoformat，Decimal → float）。"""
    out = []
    for r in rows or []:
        d = dict(r)
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif hasattr(v, "as_tuple"):  # Decimal
                d[k] = float(v)
        out.append(d)
    return out


async def _cfg_num(config_provider, key: str, default: float):
    if config_provider is None:
        return default
    try:
        raw = await config_provider.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        return float(raw)
    except Exception:  # noqa: BLE001
        return default


async def _cfg_bundle(config_provider) -> dict:
    out = {}
    for name, (key, dflt) in _CFG_KEYS.items():
        out[name] = {"key": key, "value": await _cfg_num(config_provider, key, dflt), "default": dflt}
    return out


def _envelope(data, message: str = "ok"):
    return {"code": 0, "data": data, "message": message}


def _not_ready(what: str):
    return {"code": "SERVICE_NOT_READY", "data": None, "message": f"{what} not available"}


# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────
def create_state_router(db_pool=None, config_provider=None, auth_handler=None, redis_client=None) -> APIRouter:
    """FSM 行情状态机监控路由（只读）。注册见 hcm-web/main.py startup()。"""
    router = APIRouter(tags=["state"])

    # ── 1) 实时快照：合并 4 个 Redis 键 + 派生量 + 相关配置 ────────────────────
    @router.get("/api/v1/state/live/{symbol}")
    async def state_live(symbol: str, user=Depends(auth_handler.require_auth)):
        if redis_client is None or not getattr(redis_client, "is_initialized", False):
            return _not_ready("Redis")
        sym = symbol.upper()
        fsm = await _redis_json(redis_client, f"hcm:state:fsm:{sym}")
        live = await _redis_json(redis_client, f"hcm:live:state:{sym}")
        ctx = await _redis_json(redis_client, f"hcm:state:ctx:{sym}")
        directive = await _redis_json(redis_client, f"hcm:state:directive:{sym}")
        if fsm is None and live is None and ctx is None:
            return _envelope(None, "no_state_snapshot")

        # 派生量：箱体高度（ctx 无该字段，实测确认为自算）
        box_upper = (ctx or {}).get("box_upper")
        box_lower = (ctx or {}).get("box_lower")
        box_height = None
        if isinstance(box_upper, (int, float)) and isinstance(box_lower, (int, float)):
            box_height = round(float(box_upper) - float(box_lower), 5)

        state = (live or {}).get("state") or (fsm or {}).get("state")
        box_frozen = (ctx or {}).get("box_frozen")
        is_osc = state == "S1_OSC"
        # ⚠️ UI 规则（非 S1 置灰）与后端字段 box_frozen 是两个不同信号，实测会不一致；
        #    此处**如实同时返回**，由前端分别呈现，不用 UI 规则覆盖字段真值。
        misaligned = (box_frozen is not None) and (bool(box_frozen) != (not is_osc))

        return _envelope({
            "symbol": sym,
            "ts": datetime.now(timezone.utc).isoformat(),
            "fsm": fsm,
            "live": live,
            "ctx": ctx,
            "directive": directive,
            "derived": {
                "state": state,
                "state_cn": _STATE_CN.get(state or "", None),
                "is_osc": is_osc,
                "box_height": box_height,
                "box_frozen": box_frozen,
                "box_frozen_at": (ctx or {}).get("box_frozen_at"),
                # UI 规则与字段是否打架（true = 打架，面板应如实展示而非掩盖）
                "freeze_rule_misaligned": misaligned,
                # 趋势细节未发布（实测），显式标注而非伪造 0
                "trend_detail_published": False,
                "trend_detail_reason": "scheduler.py:2231 从 latest_direction() 只取 name，slope_atr/di_spread 被丢弃",
            },
            "config": await _cfg_bundle(config_provider),
        })

    # ── 2) 历史日志（逐 bar）────────────────────────────────────────────────
    @router.get("/api/v1/state/logs/{symbol}")
    async def state_logs(symbol: str, tf: str = "M5", limit: int = 300,
                         since: str = "", until: str = "",
                         user=Depends(auth_handler.require_auth)):
        if db_pool is None:
            return _not_ready("db")
        limit = max(1, min(int(limit or 300), 2000))
        rows = await _fetch(
            db_pool,
            f"SELECT {_LOG_COLS} FROM hcm_signal.market_state_log "
            "WHERE symbol = $1 AND time_frame = $2 "
            "AND ($3::timestamptz IS NULL OR bar_open_time >= $3::timestamptz) "
            "AND ($4::timestamptz IS NULL OR bar_open_time <= $4::timestamptz) "
            "ORDER BY bar_open_time DESC LIMIT $5",
            symbol.upper(), tf, _parse_ts(since), _parse_ts(until), limit,
        )
        items = _rows_to_dicts(rows)
        return _envelope({"symbol": symbol.upper(), "time_frame": tf,
                          "items": items, "count": len(items)})

    # ── 3) 概率序列（窄响应，供时序图单独刷新）─────────────────────────────
    @router.get("/api/v1/state/proba/{symbol}")
    async def state_proba(symbol: str, tf: str = "M5", limit: int = 500,
                          user=Depends(auth_handler.require_auth)):
        if db_pool is None:
            return _not_ready("db")
        limit = max(1, min(int(limit or 500), 3000))
        rows = await _fetch(
            db_pool,
            "SELECT bar_open_time, state, prev_state, transitioned, predicted_class, "
            "       prob_oscillation, prob_trend_init, prob_trend_mid, prob_trend_fade, margin "
            "FROM hcm_signal.market_state_log "
            "WHERE symbol = $1 AND time_frame = $2 "
            "ORDER BY bar_open_time DESC LIMIT $3",
            symbol.upper(), tf, limit,
        )
        items = _rows_to_dicts(rows)
        items.reverse()  # 升序，便于前端直接连线
        return _envelope({"symbol": symbol.upper(), "time_frame": tf,
                          "items": items, "count": len(items)})

    # ── 4) K 线 + 状态对齐（一次拿全，避免前端两次对齐）────────────────────
    @router.get("/api/v1/state/kline/{symbol}")
    async def state_kline(symbol: str, tf: str = "M5", limit: int = 300,
                          user=Depends(auth_handler.require_auth)):
        if db_pool is None:
            return _not_ready("db")
        limit = max(10, min(int(limit or 300), 2000))
        rows = await _fetch(
            db_pool,
            "SELECT k.open_time, k.open, k.high, k.low, k.close, k.tick_volume, "
            "       s.state, s.prev_state, s.transitioned, s.predicted_class, "
            "       s.prob_oscillation, s.prob_trend_init, s.prob_trend_mid, s.prob_trend_fade, "
            "       s.margin, s.age_bars, s.direction, s.hold_only, s.model_version, "
            "       s.intent_action, s.intent_direction, s.intent_lot_mult, s.intent_reason, "
            "       s.trigger_on, s.trigger_reason, s.note "
            "FROM (SELECT * FROM hcm_market.klines_xauusd "
            "      WHERE symbol = $1 AND time_frame = $2 "
            "      ORDER BY open_time DESC LIMIT $3) k "
            "LEFT JOIN hcm_signal.market_state_log s "
            "  ON s.symbol = k.symbol AND s.time_frame = k.time_frame "
            " AND s.bar_open_time = k.open_time "
            "ORDER BY k.open_time ASC",
            symbol.upper(), tf, limit,
        )
        items = _rows_to_dicts(rows)
        matched = sum(1 for it in items if it.get("state"))
        return _envelope({"symbol": symbol.upper(), "time_frame": tf, "items": items,
                          "count": len(items), "state_matched": matched})

    # ── 5) 持仓 + 当日亏损（口径同 risk_engine/rule_chain.py:_get_daily_loss）──
    @router.get("/api/v1/state/risk/{symbol}")
    async def state_risk(symbol: str, order_limit: int = 60,
                         user=Depends(auth_handler.require_auth)):
        if db_pool is None:
            return _not_ready("db")
        sym = symbol.upper()
        pos_rows = await _fetch(
            db_pool,
            "SELECT position_id, mt5_ticket, direction, lot, open_price, current_price, "
            "       sl, tp, float_profit, open_time, signal_id "
            "FROM hcm_trading.positions "
            "WHERE status = 'open' AND symbol = $1 ORDER BY open_time DESC",
            sym,
        )
        positions = _rows_to_dicts(pos_rows)
        float_sum = sum(float(p.get("float_profit") or 0) for p in positions)

        # 开/平仓标记（K 线图 markPoint 用）。
        # ⚠️ 实测：hcm_trading.positions 只有 19 列、**没有 close_time**，
        #    故「平仓时点」只能取 orders.close_time；orders 同时自带 open_time，
        #    因此开仓与平仓标记都从本表取，保持时间轴同源。
        #    orders 也无 position_id，与 positions 只能靠 mt5_ticket/signal_id 弱关联
        #    → 前端按时间就近吸附（已在 UI 注明为近似，不做伪精确关联）。
        order_limit = max(1, min(int(order_limit or 60), 300))
        ord_rows = await _fetch(
            db_pool,
            "SELECT order_id, mt5_ticket, signal_id, direction, lot, open_price, close_price, "
            "       profit, open_time, close_time, close_reason "
            "FROM hcm_trading.orders "
            "WHERE symbol = $1 AND order_status = 2 "
            "ORDER BY open_time DESC LIMIT $2",
            sym, order_limit,
        )

        # 全账号当日已平仓盈亏（与风控引擎同口径，故不加 symbol 过滤）
        day_all = await _fetchrow(
            db_pool,
            "SELECT COALESCE(SUM(profit), 0) AS pnl, COUNT(*) AS n "
            "FROM hcm_trading.orders WHERE order_status = 2 AND close_time >= CURRENT_DATE",
        )
        day_sym = await _fetchrow(
            db_pool,
            "SELECT COALESCE(SUM(profit), 0) AS pnl, COUNT(*) AS n "
            "FROM hcm_trading.orders "
            "WHERE order_status = 2 AND close_time >= CURRENT_DATE AND symbol = $1",
            sym,
        )

        def _num(row, k):
            if row is None:
                return None
            v = row[k]
            return float(v) if v is not None else 0.0

        cap = await _cfg_num(config_provider, "risk.max_daily_loss", 200.0)
        return _envelope({
            "symbol": sym,
            "positions": positions,
            "positions_open": len(positions),
            "float_profit_sum": round(float_sum, 2),
            # 与风控引擎口径一致（全账号）—— 告警阈值按它判
            "day_loss": _num(day_all, "pnl"),
            "day_loss_orders": int(day_all["n"]) if day_all is not None else 0,
            # 仅本品种的口径，供参考
            "day_loss_symbol": _num(day_sym, "pnl"),
            "day_loss_cap": cap,
            "day_loss_source": "hcm_trading.orders(order_status=2) 口径同 rule_chain.py:_get_daily_loss",
            # K 线开平仓标记（近 N 笔已平仓单，倒序）
            "recent_orders": _rows_to_dicts(ord_rows),
            "orders_source": ("hcm_trading.orders(order_status=2)；positions 表无 close_time 列，"
                              "平仓时点只能取此表；无 position_id → 标记为时间就近吸附（近似）"),
        })

    return router
