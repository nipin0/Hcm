"""Dashboard API — Aggregated stats and analytics.

Provides:
- GET /api/v1/dashboard/today — Today's statistics
- GET /api/v1/dashboard/pnl — P&L summary
- GET /api/v1/dashboard/winrate — Win rate analysis
- GET /api/v1/dashboard/symbols-compare — Symbol comparison
- GET /api/v1/dashboard/external-factors — External factor status
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

logger = logging.getLogger(__name__)

# Lazy import to avoid circular dependency
_signal_gauge_engine: Any = None


def _get_signal_gauge_engine() -> Any:
    """Lazy-load the signal_gauge module."""
    global _signal_gauge_engine
    if _signal_gauge_engine is None:
        from web.api import signal_gauge as _sg  # noqa: F811
        _signal_gauge_engine = _sg
    return _signal_gauge_engine


# ── Router Factory ─────────────────────────────

def create_dashboard_router(
    db_pool: Any = None,
    redis_client: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with dashboard analytics endpoints.

    Args:
        db_pool: DatabasePool instance.
        redis_client: RedisClient instance.
        auth_handler: AuthHandler instance for RBAC.

    Returns:
        APIRouter with dashboard routes.
    """
    router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

    # ── Today's Statistics ──────────────────────

    @router.get("/today")
    async def today_stats(
        request: Request,
        account_id: Optional[int] = Query(None),
    ):
        """Get today's trading statistics.

        Returns signals count, trades placed, orders executed,
        and active positions for the current day.

        Args:
            account_id: Optional account filter.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            account_filter = "AND s.account_id = $1" if account_id is not None else ""
            params: list[Any] = [account_id] if account_id is not None else []

            # Today's signals
            signal_row = await db_pool.fetchrow(
                f"""SELECT COUNT(*) as total_signals,
                           COUNT(*) FILTER (WHERE s.signal_dir = 'BUY') as buy_signals,
                           COUNT(*) FILTER (WHERE s.signal_dir = 'SELL') as sell_signals,
                           AVG(s.confidence) as avg_confidence,
                           AVG(s.pre_score) as avg_pre_score
                    FROM hcm_signal.signals s
                    WHERE s.created_at >= CURRENT_DATE
                      {account_filter}""",
                *params,
            )

            # Today's orders
            order_row = await db_pool.fetchrow(
                f"""SELECT COUNT(*) as total_orders,
                           COUNT(*) FILTER (WHERE o.close_time IS NOT NULL) as closed_orders,
                           SUM(o.profit) as total_profit,
                           SUM(o.commission) as total_commission,
                           SUM(o.swap) as total_swap
                    FROM hcm_trading.orders o
                    WHERE o.created_at >= CURRENT_DATE
                      {account_filter.replace('s.', 'o.')}""",
                *params,
            )

            # Active positions
            pos_row = await db_pool.fetchrow(
                f"""SELECT COUNT(DISTINCT p.mt5_ticket) as active_positions,
                           SUM(p.float_profit) as total_float
                    FROM hcm_trading.positions p
                    WHERE p.snapshot_time >= now() - interval '10 minutes'
                      {account_filter.replace('s.', 'p.')}""",
                *params,
            )

            return {
                "code": 0,
                "data": {
                    "signals": {
                        "total": signal_row["total_signals"] if signal_row else 0,
                        "buy": signal_row["buy_signals"] if signal_row else 0,
                        "sell": signal_row["sell_signals"] if signal_row else 0,
                        "avg_confidence": round(float(signal_row["avg_confidence"] or 0), 4) if signal_row else 0,
                        "avg_pre_score": round(float(signal_row["avg_pre_score"] or 0), 4) if signal_row else 0,
                    },
                    "orders": {
                        "total": order_row["total_orders"] if order_row else 0,
                        "closed": order_row["closed_orders"] if order_row else 0,
                        "total_profit": round(float(order_row["total_profit"] or 0), 2) if order_row else 0,
                        "total_commission": round(float(order_row["total_commission"] or 0), 2) if order_row else 0,
                        "total_swap": round(float(order_row["total_swap"] or 0), 2) if order_row else 0,
                    },
                    "positions": {
                        "active": pos_row["active_positions"] if pos_row else 0,
                        "total_float": round(float(pos_row["total_float"] or 0), 2) if pos_row else 0,
                    },
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Dashboard today stats failed: %s", exc)
            return {"code": "WB_DB_001", "data": None, "message": str(exc)}

    # ── P&L Summary ─────────────────────────────

    @router.get("/pnl")
    async def pnl_summary(
        request: Request,
        account_id: Optional[int] = Query(None),
        days: int = Query(7, ge=1, le=365),
    ):
        """Get P&L summary for a period.

        Aggregates from hcm_trading.positions (live open positions) plus
        hcm_trading.orders (closed trades). Falls back gracefully when
        schema columns don't exist yet.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            total_profit = 0.0
            total_trades = 0
            wins = 0
            losses = 0
            wins_sum = 0.0
            losses_sum = 0.0
            max_win = 0.0
            max_loss = 0.0
            daily_map: dict[str, dict] = {}

            # Try orders (closed trades) — schema has profit
            try:
                rows = await db_pool.fetch(
                    """SELECT DATE(close_time) AS trade_date,
                              mt5_ticket, profit, commission, swap, lot
                       FROM hcm_trading.orders
                       WHERE close_time IS NOT NULL
                         AND close_time >= now() - ($1 || ' days')::interval
                       ORDER BY close_time DESC
                       LIMIT 500""",
                    str(days),
                )
                for r in rows:
                    profit = float(r["profit"] or 0)
                    total_profit += profit
                    total_trades += 1
                    if profit > 0:
                        wins += 1
                        wins_sum += profit
                    elif profit < 0:
                        losses += 1
                        losses_sum += profit
                    if profit > max_win: max_win = profit
                    if profit < max_loss: max_loss = profit
                    d = r["trade_date"].isoformat() if r["trade_date"] else "unknown"
                    if d not in daily_map: daily_map[d] = {"profit":0.0,"trades":0}
                    daily_map[d]["profit"] += profit
                    daily_map[d]["trades"] += 1
            except Exception:
                pass

            # Also count signals (proxies for trade count)
            try:
                sig_count = await db_pool.fetchval(
                    "SELECT count(*) FROM hcm_signal.signals WHERE created_at >= now() - ($1 || ' days')::interval",
                    str(days),
                )
                if total_trades == 0 and sig_count:
                    total_trades = int(sig_count)
            except Exception:
                pass

            # Add open positions floating P&L (live unrealized)
            try:
                position_rows = await db_pool.fetch(
                    """SELECT symbol, direction, lot, open_price, current_price, float_profit
                       FROM hcm_trading.positions""")
                for p in position_rows:
                    fp = float(p["float_profit"] or 0)
                    total_profit += fp
                    if fp > 0: wins += 1
                    elif fp < 0: losses += 1
            except Exception:
                pass

            daily = [{"date": d, **v} for d, v in daily_map.items()]
            daily.sort(key=lambda x: x["date"], reverse=True)

            win_rate = round(wins / (wins + losses) * 100, 2) if (wins + losses) > 0 else 0
            profit_loss_ratio = round(wins_sum / abs(losses_sum), 2) if losses_sum < 0 else 0
            avg_win = round(wins_sum / wins, 2) if wins > 0 else 0
            avg_loss = round(losses_sum / losses, 2) if losses > 0 else 0
            avg_profit_per_trade = round(total_profit / total_trades, 2) if total_trades > 0 else 0

            return {
                "code": 0,
                "data": {
                    "total_profit": round(total_profit, 2),
                    "total_trades": total_trades,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": win_rate,
                    "profit_loss_ratio": profit_loss_ratio,
                    "avg_win": avg_win,
                    "avg_loss": avg_loss,
                    "avg_profit_per_trade": avg_profit_per_trade,
                    "max_win": round(max_win, 2),
                    "max_loss": round(max_loss, 2),
                    "days": days,
                    "winning_days": sum(1 for d in daily if d["profit"] > 0),
                    "losing_days": sum(1 for d in daily if d["profit"] < 0),
                    "daily": daily,
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Dashboard P&L failed: %s", exc)
            return {"code": "WB_DB_001", "data": None, "message": str(exc)}

    # ── Win Rate Analysis ───────────────────────

    @router.get("/winrate")
    async def winrate_analysis(
        request: Request,
        account_id: Optional[int] = Query(None),
        days: int = Query(30, ge=1, le=365),
    ):
        """Get win rate analysis from closed orders + signals (proxies)."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            wins = losses = total = 0
            avg_win = avg_loss = 0.0
            symbol_rows: list = []
            dir_rows: list = []
            max_drawdown = 0.0

            # Real closed trades from orders
            try:
                overall = await db_pool.fetchrow(
                    """SELECT COUNT(*) AS total,
                              COUNT(*) FILTER (WHERE profit > 0) AS wins,
                              COUNT(*) FILTER (WHERE profit < 0) AS losses,
                              COUNT(*) FILTER (WHERE profit = 0) AS breakeven,
                              AVG(profit) FILTER (WHERE profit > 0) AS avg_win,
                              AVG(profit) FILTER (WHERE profit < 0) AS avg_loss,
                              MIN(profit) AS max_loss,
                              MAX(profit) AS max_win
                       FROM hcm_trading.orders
                       WHERE close_time IS NOT NULL""")
                if overall:
                    total = overall["total"] or 0
                    wins = overall["wins"] or 0
                    losses = overall["losses"] or 0
                    avg_win = float(overall["avg_win"] or 0)
                    avg_loss = float(overall["avg_loss"] or 0)
                    if overall["max_loss"] and float(overall["max_loss"]) < max_drawdown:
                        max_drawdown = float(overall["max_loss"])

                symbol_rows = await db_pool.fetch(
                    """SELECT symbol,
                              COUNT(*) AS total,
                              COUNT(*) FILTER (WHERE profit > 0) AS wins,
                              SUM(profit) AS total_profit
                       FROM hcm_trading.orders
                       WHERE close_time IS NOT NULL
                       GROUP BY symbol ORDER BY total DESC""")
                dir_rows = await db_pool.fetch(
                    """SELECT direction,
                              COUNT(*) AS total,
                              COUNT(*) FILTER (WHERE profit > 0) AS wins,
                              SUM(profit) AS total_profit
                       FROM hcm_trading.orders
                       WHERE close_time IS NOT NULL
                       GROUP BY direction""")
            except Exception:
                pass

            # If no closed orders, fall back to signals (proxy)
            if total == 0:
                try:
                    sig_rows = await db_pool.fetch(
                        """SELECT signal_dir AS direction, count(*) AS total,
                                  count(*) FILTER (WHERE signal_status = 3) AS wins,
                                  count(*) FILTER (WHERE signal_status = 2) AS losses
                           FROM hcm_signal.signals
                           GROUP BY signal_dir""")
                    for r in sig_rows:
                        total += r["total"]; wins += r["wins"]; losses += r["losses"]
                    avg_win = 0.0
                    avg_loss = 0.0
                    dir_rows = list(sig_rows)
                except Exception:
                    pass

            # Compute max drawdown from open positions (live unrealized)
            try:
                pos_rows = await db_pool.fetch(
                    """SELECT float_profit FROM hcm_trading.positions WHERE float_profit < 0""")
                if pos_rows:
                    worst = min(float(p["float_profit"]) for p in pos_rows)
                    if worst < max_drawdown:
                        max_drawdown = worst
            except Exception:
                pass

            total_trades = total
            win_rate = round(wins / total * 100, 2) if total > 0 else 0
            profit_factor = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 and total > 0 else 0

            return {
                "code": 0,
                "data": {
                    "overall": {
                        "total_trades": total_trades,
                        "wins": wins,
                        "losses": losses,
                        "breakeven": 0,
                        "win_rate": win_rate,
                        "avg_win": round(avg_win, 2),
                        "avg_loss": round(avg_loss, 2),
                        "profit_factor": profit_factor,
                    },
                    "by_symbol": [
                        {
                            "symbol": r["symbol"],
                            "total": r["total"],
                            "wins": r["wins"],
                            "win_rate": round(r["wins"] / r["total"] * 100, 2) if r["total"] > 0 else 0,
                            "total_profit": round(float(r["total_profit"] or 0), 2),
                        } for r in symbol_rows
                    ],
                    "by_direction": [
                        {
                            "direction": r["direction"],
                            "total": r["total"],
                            "wins": r["wins"],
                            "win_rate": round(r["wins"] / r["total"] * 100, 2) if r["total"] > 0 else 0,
                            "total_profit": round(float(r.get("total_profit") or 0), 2),
                        } for r in dir_rows
                    ],
                    "max_drawdown": round(max_drawdown, 2),
                    "days": days,
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Dashboard winrate failed: %s", exc)
            return {"code": "WB_DB_001", "data": None, "message": str(exc)}

    # ── KPI Summary (used by /statistics page) ──

    @router.get("/statistics")
    async def statistics_summary(
        request: Request,
        symbol: Optional[str] = Query(None, description="Optional symbol filter"),
        days: int = Query(30, ge=1, le=365),
        start_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
        end_date: Optional[str] = Query(None, description="End date YYYY-MM-DD (inclusive)"),
    ):
        """Aggregate KPI summary with real-time P&L from bridge price feed.

        Supports both legacy ``days`` parameter (N days back from now) and
        explicit ``start_date``/``end_date`` range queries.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        # ── Build time-filter SQL fragments ──
        if start_date and end_date:
            # Explicit date range: inclusive start, exclusive end+1day
            sig_time_filter = "s.created_at >= $1::date AND s.created_at < ($2::date + interval '1 day')"
            sig_time_params: list[Any] = [start_date, end_date]
            ord_time_filter = "o.close_time >= $1::date AND o.close_time < ($2::date + interval '1 day')"
            ord_time_params: list[Any] = [start_date, end_date]
        else:
            # Legacy days-based window
            sig_time_filter = "s.created_at >= now() - ($1 || ' days')::interval"
            sig_time_params = [str(days)]
            ord_time_filter = "o.close_time >= now() - ($1 || ' days')::interval"
            ord_time_params = [str(days)]

        try:
            total_signals = 0
            wins = losses = 0
            total_profit = 0.0
            wins_sum = losses_sum = 0.0
            max_drawdown = 0.0

            # === 1. Get live prices from Redis (bridge pushes every 2s) ===
            live_prices: dict[str, dict] = {}  # symbol -> {bid, ask, mid, updated_at}
            try:
                if redis_client is not None and redis_client.is_initialized:
                    for sym in ["XAUUSD"]:
                        price_raw = await redis_client.hget("hcm:config:v2", f"market:latest:{sym}")
                        if price_raw:
                            import json as _json
                            data = _json.loads(price_raw)
                            bid = float(data.get("bid", 0))
                            ask = float(data.get("ask", 0))
                            utc_time = data.get("updated_at") or data.get("time")
                            if bid > 0 and ask > 0:
                                live_prices[sym] = {
                                    "bid": bid, "ask": ask,
                                    "mid": round((bid + ask) / 2, 5),
                                    "updated_at": utc_time,  # UTC unix timestamp
                                }
            except Exception:
                pass

            # Fallback: latest kline close
            if not live_prices:
                try:
                    row = await db_pool.fetchrow(
                        "SELECT close FROM hcm_market.klines WHERE symbol='XAUUSD' ORDER BY open_time DESC LIMIT 1")
                    if row:
                        close = float(row["close"])
                        live_prices["XAUUSD"] = {"bid": close - 0.2, "ask": close + 0.2}
                except Exception:
                    pass

            # === 2. Count signals ===
            try:
                sig_rows = await db_pool.fetch(
                    f"""SELECT signal_id, signal_status, signal_dir, symbol
                       FROM hcm_signal.signals s
                       WHERE {sig_time_filter}
                       ORDER BY s.created_at DESC""", *sig_time_params)
                total_signals = len(sig_rows)
            except Exception:
                pass

            # === 3. Closed orders (realized P&L) ===
            pnl_rows: list = []
            try:
                pnl_rows = await db_pool.fetch(
                    f"""SELECT o.profit FROM hcm_trading.orders o
                       WHERE o.close_time IS NOT NULL
                         AND {ord_time_filter}
                       LIMIT 5000""", *ord_time_params)
            except Exception:
                pass

            for r in pnl_rows:
                p = float(r["profit"] or 0)
                total_profit += p
                if p > 0:
                    wins += 1; wins_sum += p
                elif p < 0:
                    losses += 1; losses_sum += p
                if p < max_drawdown:
                    max_drawdown = p

            # === 4. Open positions (unrealized P&L using live prices) ===
            open_pnl_rows: list = []
            try:
                open_pnl_rows = await db_pool.fetch(
                    """SELECT position_id, symbol, direction, open_price, lot, float_profit
                       FROM hcm_trading.positions""")
            except Exception:
                pass

            position_profit = 0.0
            for pos in open_pnl_rows:
                fp = float(pos["float_profit"] or 0)
                if fp != 0:
                    total_profit += fp
                    if fp > 0: wins += 1; wins_sum += fp
                    elif fp < 0: losses += 1; losses_sum += fp
                    position_profit += fp
                    if fp < max_drawdown: max_drawdown = fp
                else:
                    # Compute real-time unrealized P&L from live price
                    sym = pos["symbol"]
                    direction = pos["direction"]
                    open_price = float(pos["open_price"])
                    lot = float(pos["lot"])
                    prices = live_prices.get(sym, {})
                    bid = prices.get("bid", open_price)
                    ask = prices.get("ask", open_price)
                    live_price = bid if direction == "SELL" else ask if direction == "BUY" else open_price
                    if live_price and open_price > 0:
                        pip_value = 100.0  # XAUUSD: 1 lot = $100 per $1 move
                        if direction == "BUY":
                            unrealized = (live_price - open_price) * lot * pip_value
                        else:
                            unrealized = (open_price - live_price) * lot * pip_value
                        total_profit += unrealized
                        if unrealized > 0:
                            wins += 1; wins_sum += unrealized
                        elif unrealized < 0:
                            losses += 1; losses_sum += unrealized
                        if unrealized < max_drawdown:
                            max_drawdown = unrealized

            # === 5. Compute KPIs ===
            total_trades = wins + losses
            win_rate = wins / total_trades * 100 if total_trades > 0 else 0
            profit_factor = (abs(wins_sum / losses_sum) if losses_sum < 0
                           else (wins_sum if wins > 0 else 0))
            if wins > 0: avg_win = wins_sum / wins
            else: avg_win = 0.0
            if losses > 0: avg_loss = losses_sum / losses
            else: avg_loss = 0.0

            # Sharpe ratio (annualized)
            sharpe_ratio = 0.0
            if total_trades >= 5:
                mean = total_profit / total_trades
                try:
                    diffs = []
                    for r in pnl_rows:
                        diffs.append((float(r["profit"] or 0) - mean) ** 2)
                    if diffs:
                        variance = sum(diffs) / len(diffs)
                        stddev = variance ** 0.5
                        if stddev > 0:
                            sharpe_ratio = mean / stddev * (252 ** 0.5)
                except Exception:
                    pass

            # Get total trade count (orders + positions)
            total_trades_final = total_trades or total_signals or 0

            return {
                "code": 0,
                "data": {
                    "total_trades": total_trades_final,
                    "total_signals": total_signals,
                    "total_profit": round(total_profit, 2),
                    "win_rate": round(win_rate, 2),
                    "profit_factor": round(profit_factor, 2),
                    "max_drawdown": round(max_drawdown, 2),
                    "avg_profit": round(avg_win, 2),
                    "avg_loss": round(avg_loss, 2),
                    "sharpe_ratio": round(sharpe_ratio, 2),
                    "days": days,
                    "start_date": start_date,
                    "end_date": end_date,
                    "symbol": symbol or "ALL",
                },
                "message": "ok",
            }
        except Exception as exc:
            logger.error("Dashboard statistics failed: %s", exc)
            return {"code": "WB_DB_001", "data": None, "message": str(exc)}

    # ── Symbol Comparison ───────────────────────

    @router.get("/symbols-compare")
    async def symbols_compare(
        request: Request,
        days: int = Query(7, ge=1, le=90),
    ):
        """Compare performance across symbols.

        Args:
            days: Lookback period.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            rows = await db_pool.fetch(
                """SELECT symbol,
                          COUNT(*) as total_trades,
                          COUNT(*) FILTER (WHERE profit > 0) as wins,
                          COUNT(*) FILTER (WHERE profit < 0) as losses,
                          SUM(profit) as total_profit,
                          AVG(profit) as avg_profit_per_trade,
                          AVG(lot) as avg_lot,
                          SUM(commission) as total_commission
                   FROM hcm_trading.orders
                   WHERE close_time >= now() - make_interval(days => $1)
                     AND close_time IS NOT NULL
                   GROUP BY symbol
                   ORDER BY total_profit DESC""",
                days,
            )

            comparison = []
            for r in rows:
                total = r["total_trades"]
                wins = r["wins"]
                comparison.append({
                    "symbol": r["symbol"],
                    "total_trades": total,
                    "wins": wins,
                    "losses": r["losses"],
                    "win_rate": round(wins / total * 100, 2) if total > 0 else 0,
                    "total_profit": round(float(r["total_profit"] or 0), 2),
                    "avg_profit_per_trade": round(float(r["avg_profit_per_trade"] or 0), 2),
                    "avg_lot": round(float(r["avg_lot"] or 0), 2),
                    "total_commission": round(float(r["total_commission"] or 0), 2),
                })

            return {
                "code": 0,
                "data": {
                    "symbols": comparison,
                    "days": days,
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Dashboard symbols compare failed: %s", exc)
            return {"code": "WB_DB_001", "data": None, "message": str(exc)}

    # ── External Factor Status ──────────────────

    @router.get("/external-factors")
    async def external_factors(request: Request):
        """Get current external factor status from Redis cache.

        Returns latest macro, sentiment, event, and liquidity data
        from Redis cached snapshots.
        """
        result: dict[str, Any] = {
            "macro": {},
            "sentiment": {},
            "event": {},
            "liquidity": {},
        }

        # Try Redis cache for each factor type
        if redis_client is not None and redis_client.is_initialized:
            try:
                for category in ("metals", "crypto", "forex"):
                    import json
                    # Macro
                    macro_raw = await redis_client.get(f"macro:latest:{category}")
                    if macro_raw:
                        result["macro"][category] = json.loads(macro_raw) if isinstance(macro_raw, str) else macro_raw

                    # Sentiment
                    sent_raw = await redis_client.get(f"sentiment:latest:{category}")
                    if sent_raw:
                        result["sentiment"][category] = json.loads(sent_raw) if isinstance(sent_raw, str) else sent_raw

                # Event
                event_raw = await redis_client.get("event:active")
                if event_raw:
                    result["event"] = json.loads(event_raw) if isinstance(event_raw, str) else event_raw

                # Liquidity
                liq_raw = await redis_client.get("liquidity:current")
                if liq_raw:
                    result["liquidity"] = json.loads(liq_raw) if isinstance(liq_raw, str) else liq_raw

            except Exception as exc:
                logger.warning("Redis external factors read failed: %s", exc)

        # Attach collector status so the panel can show whether the AI final
        # scoring is live (DeepSeek real key) or degraded to heuristics, plus
        # the last-collection heartbeat (stale => collector stalled).
        status: dict[str, Any] = {
            "stub_mode": None,
            "ai_offline": None,
            "ai_source": "unknown",
            "last_collection": None,
            "composite": None,
            "fresh": None,
        }
        if redis_client is not None and redis_client.is_initialized:
            try:
                import json as _json

                async def _rget(key: str):
                    return await redis_client.get(key)

                stub_raw = await _rget("hcm:market:stub_mode")
                offline_raw = await _rget("hcm:market:ai_offline")
                last_raw = await _rget("hcm:market:last_collection")
                comp_raw = await _rget("hcm:market:composite:score")

                if stub_raw is not None:
                    status["stub_mode"] = str(stub_raw).lower() in ("true", "1", "yes")
                if offline_raw is not None:
                    status["ai_offline"] = str(offline_raw).lower() in ("true", "1", "yes")
                # ai_source: real DeepSeek when not stub and not offline.
                if status["stub_mode"] is False and status["ai_offline"] is False:
                    status["ai_source"] = "deepseek"
                elif status["ai_offline"] is True:
                    status["ai_source"] = "heuristic"
                elif status["stub_mode"] is True:
                    status["ai_source"] = "stub"
                status["last_collection"] = last_raw
                if comp_raw is not None:
                    try:
                        status["composite"] = float(comp_raw)
                    except (TypeError, ValueError):
                        pass
                # Freshness: collector is considered stale beyond 48h.
                if last_raw:
                    try:
                        from datetime import datetime, timezone
                        last_dt = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
                        age_s = (datetime.now(timezone.utc) - last_dt).total_seconds()
                        status["fresh"] = age_s < 48 * 3600
                    except (ValueError, TypeError):
                        status["fresh"] = None
            except Exception as exc:
                logger.warning("Redis external factors status read failed: %s", exc)
        result["status"] = status

        # Fallback to PG if Redis is empty
        if not result["macro"] and db_pool is not None and db_pool.is_initialized:
            try:
                for category in ("metals", "crypto", "forex"):
                    macro_row = await db_pool.fetchrow(
                        """SELECT macro_risk_score, macro_bias, ai_summary
                           FROM hcm_market.macro_snapshots
                           WHERE category = $1
                           ORDER BY snapshot_time DESC LIMIT 1""",
                        category,
                    )
                    if macro_row:
                        result["macro"][category] = {
                            "score": macro_row["macro_risk_score"],
                            "bias": macro_row["macro_bias"],
                            "summary": macro_row["ai_summary"],
                        }

                    sent_row = await db_pool.fetchrow(
                        """SELECT sentiment_risk_score, sentiment_bias, ai_summary
                           FROM hcm_market.sentiment_snapshots
                           WHERE category = $1
                           ORDER BY snapshot_time DESC LIMIT 1""",
                        category,
                    )
                    if sent_row:
                        result["sentiment"][category] = {
                            "score": sent_row["sentiment_risk_score"],
                            "bias": sent_row["sentiment_bias"],
                            "summary": sent_row["ai_summary"],
                        }
            except Exception as exc:
                logger.warning("PG external factors fallback failed: %s", exc)

        return {"code": 0, "data": result, "message": "ok"}

    # ── Realtime Signal Stream ──────────────────

    @router.get("/realtime")
    async def realtime_stream(
        request: Request,
        symbol: str = Query(default="XAUUSD"),
        timeframe: str = Query(default="M5"),
    ):
        """Get real-time signal stream data for the dashboard.

        Returns latest signals, current kline, and open positions
        for the specified symbol and timeframe.
        """
        result: dict[str, Any] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "signals": [],
            "latest_kline": None,
            "live_price": None,
            "positions": [],
        }

        if db_pool is None or not db_pool.is_initialized:
            return {"code": 0, "data": result, "message": "ok"}

        # Query latest kline
        try:
            kline_row = await db_pool.fetchrow(
                """SELECT open_time, open, high, low, close, tick_volume
                   FROM hcm_market.klines
                   WHERE symbol=$1 AND time_frame=$2
                   ORDER BY open_time DESC LIMIT 1""",
                symbol, timeframe,
            )
            if kline_row:
                result["latest_kline"] = {
                    "open_time": kline_row["open_time"].isoformat() if kline_row["open_time"] else None,
                    "open": float(kline_row["open"]) if kline_row["open"] else 0,
                    "high": float(kline_row["high"]) if kline_row["high"] else 0,
                    "low": float(kline_row["low"]) if kline_row["low"] else 0,
                    "close": float(kline_row["close"]) if kline_row["close"] else 0,
                    "tick_volume": kline_row.get("tick_volume", 0),
                }
        except Exception as exc:
            logger.warning("Realtime kline query failed: %s", exc)

        # Inject live MT5 price (bid/ask/updated_at from bridge 2s tick)
        try:
            if redis_client is not None and redis_client.is_initialized:
                price_raw = await redis_client.hget("hcm:config:v2", f"market:latest:{symbol}")
                if price_raw:
                    import json as _json2
                    data = _json2.loads(price_raw)
                    bid = float(data.get("bid", 0))
                    ask = float(data.get("ask", 0))
                    utc_time = data.get("updated_at") or data.get("time")
                    if bid > 0 and ask > 0:
                        result["live_price"] = {
                            "bid": bid, "ask": ask,
                            "mid": round((bid + ask) / 2, 5),
                            "updated_at": utc_time,
                        }
        except Exception:
            pass

        # Query latest signals
        try:
            signal_rows = await db_pool.fetch(
                """SELECT signal_id, account_id, symbol, time_frame, signal_dir,
                          entry_price, sl_price, tp1, tp2, lot, confidence,
                          signal_status, regime, pre_score, reason, created_at
                   FROM hcm_signal.signals
                   WHERE symbol=$1 AND time_frame=$2
                   ORDER BY created_at DESC LIMIT 10""",
                symbol, timeframe,
            )
            for row in signal_rows:
                result["signals"].append({
                    "signal_id": row["signal_id"],
                    "account_id": row["account_id"],
                    "symbol": row["symbol"],
                    "time_frame": row["time_frame"],
                    "signal_dir": row["signal_dir"],
                    "entry_price": float(row["entry_price"]) if row["entry_price"] else 0,
                    "sl_price": float(row["sl_price"]) if row["sl_price"] else None,
                    "tp1": float(row["tp1"]) if row["tp1"] else None,
                    "tp2": float(row["tp2"]) if row["tp2"] else None,
                    "lot": float(row["lot"]) if row["lot"] else 0.01,
                    "confidence": float(row["confidence"]) if row["confidence"] else 0,
                    "signal_status": row["signal_status"],
                    "regime": row["regime"] or "NEUTRAL",
                    "pre_score": float(row["pre_score"]) if row["pre_score"] else 0,
                    "reason": row["reason"] or "",
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                })
        except Exception as exc:
            logger.warning("Realtime signals query failed: %s", exc)

        # Query open positions
        try:
            pos_rows = await db_pool.fetch(
                """SELECT position_id, account_id, symbol, direction,
                          entry_price, current_price, lot, floating_pnl,
                          realized_pnl, status, opened_at
                   FROM hcm_trading.positions
                   WHERE symbol=$1 AND status='open'
                   ORDER BY opened_at DESC LIMIT 20""",
                symbol,
            )
            for row in pos_rows:
                result["positions"].append({
                    "position_id": row["position_id"],
                    "account_id": row["account_id"],
                    "symbol": row["symbol"],
                    "direction": row["direction"],
                    "entry_price": float(row["entry_price"]) if row["entry_price"] else 0,
                    "current_price": float(row["current_price"]) if row.get("current_price") else 0,
                    "lot": float(row["lot"]) if row["lot"] else 0,
                    "floating_pnl": float(row["floating_pnl"]) if row.get("floating_pnl") else 0,
                    "realized_pnl": float(row["realized_pnl"]) if row.get("realized_pnl") else 0,
                    "status": row["status"],
                    "opened_at": row["opened_at"].isoformat() if row["opened_at"] else None,
                })
        except Exception:
            pass

        return {"code": 0, "data": result, "message": "ok"}

    # ── K-line Ingestion Real-time Status ────────
    @router.get("/klines/latest/{symbol}")
    async def klines_latest(symbol: str):
        """Read-only: latest ingested kline per timeframe from Redis.

        Reads collector's real-time key `latest_kline:{symbol}:{tf}` (written on
        every incoming tick, ex=3600). This reflects *real-time* ingestion (the
        same source the signal engine merges via `_fetch_klines`), NOT the slower
        PG archival `hcm_market.klines` (bars written on close).

        Returns data[{tf}] = { open_time, close, tick_count, tick_volume } for
        M5/M30/H1/H4/D1. Zero writes — safe to call from any read-only context.
        """
        # 【2026-08-24】加入 M30，与引擎 hexp.periods(含 M30) 的 K 线入库状态保持一致；
        # 此前 M30 不在列表 → 前端 klines/latest 拿不到 M30 → 共振矩阵 M30 卡片无 K 线行。
        periods = ["M5", "M30", "H1", "H4", "D1"]

        def _to_epoch(v: Any) -> Optional[float]:
            """把 open_time 统一归一化为 unix 秒。

            两个数据源格式不一致：collector 的 `latest_kline:{sym}:{tf}` 写 ISO
            字符串且**不带时区后缀**（语义为 UTC，如 "2026-08-10T11:57:00"），
            而 bridge 的 `hcm:config:v2` 字段写 epoch 整数。若原样透出，前端
            `new Date("2026-08-10T11:57:00")` 会按 ECMA-262 规范当作**浏览器本地
            时间**解析（UTC+8 下偏早 8 小时 = 28800s），使新鲜度判定凭空多算 8h，
            把正常的 M5/H1 误标成「中断」。这里统一转成数值，前端只走 number 分支。
            """
            if v is None:
                return None
            if isinstance(v, datetime):
                dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            if isinstance(v, (int, float)):
                f = float(v)
                return f / 1000.0 if f > 1e12 else f  # 毫秒→秒
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return None
                try:  # 纯数字字符串
                    f = float(s)
                    return f / 1000.0 if f > 1e12 else f
                except ValueError:
                    pass
                try:
                    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                except ValueError:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)  # 无时区 → 按 UTC 解释
                return dt.timestamp()
            return None

        out: dict[str, Any] = {}

        # 注入 bridge 每 2s 写入的真实 tick 时间，供前端判定「行情源」实时性。
        # 注意：不能用 M5 棒的「距棒起始」秒数顶替——一根 300s 的 M5 棒进行到
        # 中段就有 ~150s，会被 feedStatusMeta 误判为「断开」。真实 tick 才是权威心跳。
        live_price = None
        try:
            if redis_client is not None and getattr(redis_client, "is_initialized", False):
                price_raw = await redis_client.hget("hcm:config:v2", f"market:latest:{symbol}")
                if price_raw:
                    pd = json.loads(price_raw.decode("utf-8") if isinstance(price_raw, bytes) else price_raw)
                    bid = float(pd.get("bid", 0) or 0)
                    ask = float(pd.get("ask", 0) or 0)
                    utc_time = pd.get("updated_at") or pd.get("time")
                    if bid > 0 and ask > 0:
                        live_price = {
                            "bid": bid, "ask": ask,
                            "mid": round((bid + ask) / 2, 5),
                            "updated_at": utc_time,
                        }
        except Exception as exc:  # pragma: no cover
            logger.warning("klines_latest live_price failed: %s", symbol, exc)

        for tf in periods:
            rec: dict[str, Any] = {"open_time": None, "close": None, "tick_count": None, "tick_volume": None}
            try:
                if redis_client is not None and getattr(redis_client, "is_initialized", False):
                    # 主源：collector 每 tick 写入的实时键
                    raw = await redis_client.raw.get(f"latest_kline:{symbol}:{tf}")
                    if raw:
                        d = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                        rec["open_time"] = _to_epoch(d.get("open_time"))
                        rec["close"] = d.get("close")
                        rec["tick_count"] = d.get("tick_count", d.get("tick_volume"))
                        rec["tick_volume"] = d.get("tick_volume")
                    else:
                        # 兜底：signal-tower 合并读取的 hcm:config:v2 字段
                        cfg_raw = await redis_client.hget("hcm:config:v2", f"latest_kline:{symbol}:{tf}")
                        if cfg_raw:
                            cd = json.loads(cfg_raw.decode("utf-8") if isinstance(cfg_raw, bytes) else cfg_raw)
                            rec["open_time"] = _to_epoch(cd.get("open_time"))
                            rec["close"] = cd.get("close")
                            rec["tick_count"] = cd.get("tick_count", cd.get("volume"))
                            rec["tick_volume"] = cd.get("volume", cd.get("tick_volume"))
            except Exception as exc:  # pragma: no cover
                logger.warning("klines_latest %s/%s failed: %s", symbol, tf, exc)
            out[tf] = rec

        out["live_price"] = live_price
        return {"code": 0, "data": out, "message": "ok"}

    # ── Signal Gauges ───────────────────────────

    @router.get("/signal-gauges")
    async def signal_gauges(
        request: Request,
        symbol: str = Query(default="XAUUSD"),
    ):
        """Get signal gauge data for the dashboard.

        Loads the real scoring-engine component breakdown from
        hcm:live:component_scores:{symbol}_M5 (published by signal-tower)
        and maps each component's buy/sell contribution to a long/neutral/short
        percentage. Falls back to a local M5 kline recomputation if the
        scoring-engine data is not yet available.

        Args:
            symbol: Trading symbol, e.g. "XAUUSD".
        """
        try:
            engine = _get_signal_gauge_engine()
            result = await engine.compute_all(symbol, db_pool, redis_client)

            # ── latest_inference: most recent MODEL signal (skip manual_mirror) ──
            # 【修复 2026-08-03】原实现从 signal:stream 取最新一条（含 manual_mirror
            # 镜像 CLOSE/MODIFY 类信号，pre_score=0），导致用户在震荡市看到"分值0.00
            # 方向CLOSE 信号年龄持续增长"的伪卡顿。改为从 PG hcm_signal.signals 查
            # 最新非 manual_mirror 的模型信号（BUY/SELL/NO_TRADE），与评分生产链路
            # 一致。signal_mode 列在旧数据中为 NULL（INSERT 错位 bug 已修），用
            # fallback_reason LIKE 'Manual mirror%' 兜底过滤历史镜像。
            latest_inference: Optional[dict[str, Any]] = None
            try:
                row = await db_pool.fetchrow(
                    """SELECT created_at, symbol, time_frame, signal_dir, pre_score,
                              regime, fallback_reason, signal_mode, signal_id,
                              zone_level, zone_type, zone_strength, indicator_values
                       FROM hcm_signal.signals
                       WHERE symbol = $1
                         AND (signal_mode IS NULL OR signal_mode <> 'manual_mirror')
                         AND (fallback_reason IS NULL
                              OR fallback_reason NOT LIKE 'Manual mirror%')
                       ORDER BY created_at DESC LIMIT 1""",
                    symbol,
                )
                if row:
                    # Parse indicator_values JSON for adx_14
                    adx_val: float = 0.0
                    try:
                        iv_raw = row["indicator_values"]
                        iv = json.loads(iv_raw) if isinstance(iv_raw, str) else (iv_raw or {})
                        adx_val = float(iv.get("adx_14", 0))
                    except Exception:
                        adx_val = 0.0
                    latest_inference = {
                        "score": float(row["pre_score"] or 0),
                        "symbol": row["symbol"] or symbol,
                        "timeframe": row["time_frame"] or "M5",
                        "direction": row["signal_dir"] or "NO_TRADE",
                        "regime": row["regime"] or "NEUTRAL",
                        "updated_at": row["created_at"].isoformat() if row["created_at"] else None,
                        "adx_14": adx_val,
                        "weight_scheme": "",
                        "pre_score": float(row["pre_score"] or 0),
                        "confidence": 0.0,
                        "fallback_reason": row["fallback_reason"] or "",
                        "zone_level": float(row["zone_level"] or 0),
                        "zone_type": row["zone_type"] or "",
                        "zone_strength": int(row["zone_strength"] or 0),
                        "ai_sl_mult": 0.0, "ai_tp_mult": 0.0, "entry_trigger_wait": 0,
                        "signal_mode": row["signal_mode"] or "",
                        "signal_id": str(row["signal_id"] or ""),
                        "live_adx_14": 0,
                    }
            except Exception as exc:
                logger.warning("latest_inference PG query failed for %s: %s", symbol, exc)
                latest_inference = None

            # ── scoring_thresholds: compute all 6 regime dynamic thresholds from Redis ──
            scoring_thresholds: Optional[dict[str, Any]] = None
            try:
                if redis_client is not None and redis_client.is_initialized:
                    base = float(await redis_client.hget("hcm:config:v2", "score_threshold") or 0.50)
                    pretrend_offset = float(await redis_client.hget("hcm:config:v2", "scoring.pretrend_threshold_offset") or -0.08)
                    pretrend_floor = float(await redis_client.hget("hcm:config:v2", "scoring.pretrend_threshold_floor") or 0.45)
                    fade_offset = float(await redis_client.hget("hcm:config:v2", "scoring.fade_threshold_offset") or 0.08)
                    fade_ceiling = float(await redis_client.hget("hcm:config:v2", "scoring.fade_threshold_ceiling") or 0.65)
                    range_offset = float(await redis_client.hget("hcm:config:v2", "scoring.range_threshold_offset") or -0.10)
                    range_floor = float(await redis_client.hget("hcm:config:v2", "scoring.range_threshold_floor") or 0.45)
                    neutral_offset = float(await redis_client.hget("hcm:config:v2", "scoring.neutral_threshold_offset") or 0.15)
                    neutral_ceiling = float(await redis_client.hget("hcm:config:v2", "scoring.neutral_threshold_ceiling") or 0.80)
                    strong_adx = float(await redis_client.hget("hcm:config:v2", "scoring.trend_strong_adx_threshold") or 25)
                    strong_offset = float(await redis_client.hget("hcm:config:v2", "scoring.trend_strong_threshold_offset") or -0.05)

                    scoring_thresholds = {
                        "base": base,
                        "trend_strong": round(base + strong_offset, 4),
                        "trend": round(base, 4),
                        "pretrend": round(max(pretrend_floor, base + pretrend_offset), 4),
                        "fade": round(min(fade_ceiling, base + fade_offset), 4),
                        "range": round(max(range_floor, base + range_offset), 4),
                        "neutral": round(min(neutral_ceiling, base + neutral_offset), 4),
                        "strong_adx_threshold": strong_adx,
                    }
                else:
                    scoring_thresholds = {"base": 0.50, "trend_strong": 0.45, "trend": 0.50, "pretrend": 0.45, "fade": 0.58, "range": 0.40, "neutral": 0.60, "strong_adx_threshold": 25}
            except Exception:
                scoring_thresholds = {"base": 0.50, "trend_strong": 0.45, "trend": 0.50, "pretrend": 0.45, "fade": 0.58, "range": 0.40, "neutral": 0.60, "strong_adx_threshold": 25}

            # B1 fix: propagate live ADX into latest_inference so the dashboard
            # ADX row shows the *current* market state, not a stale snapshot from
            # the last signal. The last-signal ADX is still in `adx_14` (signal-time
            # value); `live_adx_14` is the realtime value.
            # P2: gauge now uses scoring-engine components, so ADX raw value comes
            # from the dedicated hcm:live:adx:{symbol}_M5 key published by
            # signal-tower every 5s.
            if latest_inference:
                try:
                    if redis_client is not None and redis_client.is_initialized:
                        raw_adx = await redis_client.raw.get(f"hcm:live:adx:{symbol}_M5")
                        if raw_adx:
                            import json as _json_adx
                            adx_data = _json_adx.loads(raw_adx.decode() if isinstance(raw_adx, bytes) else raw_adx)
                            latest_inference["live_adx_14"] = float(adx_data.get("raw_value", latest_inference.get("live_adx_14", 0)))
                except Exception:
                    pass

                # Legacy fallback: if the old gauge ADX factor is still present, use it.
                if "live_adx_14" not in latest_inference and "factors" in result:
                    adx_factor = next(
                        (f for f in result["factors"] if f.get("key") == "ADX"),
                        None,
                    )
                    if adx_factor and "raw_value" in adx_factor:
                        latest_inference["live_adx_14"] = float(adx_factor["raw_value"])

            # Merge into result dict
            result["latest_inference"] = latest_inference
            result["scoring_thresholds"] = scoring_thresholds

            return {"code": 0, "data": result, "message": "ok"}
        except Exception as exc:
            logger.error("Signal gauges computation failed for %s: %s", symbol, exc)
            return {"code": "WB_DB_001", "data": None, "message": str(exc)}

    # ── Live score snapshot (能否下单的核心判定) ──

    @router.get("/live-score")
    async def live_score(
        request: Request,
        symbol: str = Query(default="XAUUSD"),
        timeframe: str = Query(default="M5"),
    ):
        """实时评分快照：信号塔每 3s 写入 hcm:live:score:{symbol}_{tf}。

        返回 co_source 评分引擎对当前行情的最终判定：
          pre_score        0-1 尺度的最终评分
          direction        BUY / SELL / NO_TRADE
          threshold        自适应评分门槛（随行情带/风险/顺H1 动态变化）
          threshold_passed 评分是否越过门槛（能否下单的核心布尔）
          adx / close / ts 行情上下文与更新时间
        """
        try:
            if redis_client is None or not redis_client.is_initialized:
                return {"code": 0, "data": None, "message": "redis unavailable"}
            raw = await redis_client.raw.get(f"hcm:live:score:{symbol}_{timeframe}")
            if not raw:
                return {"code": 0, "data": None, "message": "no live score yet"}
            import json as _json
            payload = _json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            return {"code": 0, "data": payload, "message": "ok"}
        except Exception as exc:
            logger.warning("live-score read failed: %s", exc)
            return {"code": "SYS_ERR", "data": None, "message": str(exc)}

    # ── Legacy path aliases ──────────────────────

    # Create a second router for legacy paths (no /v1 prefix)
    legacy_router = APIRouter(tags=["dashboard-legacy"], include_in_schema=False)

    @legacy_router.get("/api/dashboard/realtime")
    async def _legacy_realtime(request: Request, symbol: str = Query("XAUUSD"), timeframe: str = Query("M5")):
        """[Legacy] alias for /api/v1/dashboard/realtime."""
        return await realtime_stream(request, symbol, timeframe)

    @legacy_router.get("/api/dashboard/signal-gauges")
    async def _legacy_signal_gauges(request: Request, symbol: str = Query("XAUUSD")):
        """[Legacy] alias for /api/v1/dashboard/signal-gauges."""
        return await signal_gauges(request, symbol)

    # --- P0 修复：新增 Legacy aliases ---

    @legacy_router.get("/api/dashboard/statistics")
    async def _legacy_statistics(
        request: Request,
        symbol: str = Query("XAUUSD"),
        timeframe: str = Query("D1"),
        indicator: str = Query("returns"),
        lookback: int = Query(90),
    ):
        """[Legacy] alias for /api/v1/dashboard/statistics."""
        return await statistics_summary(request, symbol=symbol, days=lookback)

    @legacy_router.get("/api/dashboard/symbols-compare")
    async def _legacy_symbols_compare(request: Request):
        """[Legacy] alias for /api/v1/dashboard/symbols-compare."""
        return await symbols_compare(request)

    @legacy_router.get("/api/dashboard/compare")
    async def _legacy_compare(request: Request):
        """[Legacy] alias: routes /api/dashboard/compare → /api/v1/dashboard/symbols-compare."""
        return await symbols_compare(request)

    # --- equity-curve stub (P0 修复：前端需要此端点，当前返回空数据兜底) ---

    @router.get("/equity-curve")
    async def equity_curve_stub(request: Request, symbol: str = Query("XAUUSD")):
        """[待实现] 权益曲线端点——当前返回空数据兜底，防止前端 404 崩溃。"""
        return {"code": 0, "data": {"symbol": symbol, "curve": [], "message": "待实现"}, "message": "ok"}

    return router, legacy_router
