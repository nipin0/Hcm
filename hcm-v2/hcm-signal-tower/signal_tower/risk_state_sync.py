"""D6: 共源信号风险态同步。

周期任务从 PG（hcm_trading.orders）推算风险态并写入 Redis，
使 co_source.py 的 F3（事件窗口降分）、F5（连亏熔断）、
风险等级门槛偏移从死代码变为生效。

写入的键严格匹配 scheduler._read_co_risk_state 的解析：
  hcm:risk:daily_level       -> "low" | "med" | "high"
  hcm:risk:event_window      -> "1" | "0"（当前无财经日历源，留 manual 覆盖）
  hcm:risk:consecutive_loss  -> str(int)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

RISK_KEY_DAILY_LEVEL = "hcm:risk:daily_level"
RISK_KEY_EVENT_WINDOW = "hcm:risk:event_window"
RISK_KEY_CONSEC_LOSS = "hcm:risk:consecutive_loss"


async def sync_risk_state(
    redis,
    db,
    event_window_manual: bool = False,
    ex: int = 900,
    config_provider: Any = None,
) -> dict:
    """从 PG 计算共源信号风险态并写入 Redis。

    Args:
        redis: RedisClient（与 scheduler._redis 同类型，需 .set(name, value, ex=)）。
        db: DatabasePool（与 scheduler._db 同类型，需 .fetch(sql)）。
        event_window_manual: 手动覆盖事件窗口（默认 False；后续可接财经日历源）。
        ex: Redis 键过期秒数安全网（周期任务每 300s 刷新，设 900 防卡死）。
        config_provider: ConfigProviderV3（可选），用于读取 co.f5.cooldown_minutes
            连亏制动冷却窗口（默认 60 分钟）。

    Returns:
        dict: {consecutive_loss, daily_level, pnl}
    """
    result: dict = {"consecutive_loss": 0, "daily_level": "low", "pnl": 0.0}

    # 连亏制动冷却窗口：尾部连亏的最近一笔亏损平仓距现在超过该值 → 视为已平静，
    # 清零连亏计数，解除"制动→不下单→无盈利→永不复位"的永久死锁（2026-08-04 治本③）。
    consec_cooldown_min = 60
    if config_provider is not None:
        try:
            consec_cooldown_min = int(await config_provider.get_int(
                "co.f5.cooldown_minutes", 60))
        except Exception:  # noqa: BLE001
            consec_cooldown_min = 60

    # 1) consecutive_loss：最近平仓单尾部连续亏损笔数（F5 熔断依据）
    try:
        rows = await db.fetch(
            "SELECT profit, close_time FROM hcm_trading.orders "
            "WHERE close_time IS NOT NULL ORDER BY close_time DESC LIMIT 20"
        )
        consec = 0
        last_loss_close = None
        for r in rows:
            if (r["profit"] or 0) <= 0:
                consec += 1
                if last_loss_close is None:
                    last_loss_close = r["close_time"]
            else:
                break
        # [2026-08-04 治本③] 连亏制动死锁防护：连亏的最近一笔亏损平仓已老旧，
        # 说明市场已平静一段时间，制动应自动释放，否则会永久卡死不下单。
        if consec > 0 and last_loss_close is not None:
            try:
                from datetime import datetime, timezone
                _now = datetime.now(timezone.utc)
                _last = last_loss_close
                if getattr(_last, "tzinfo", None) is None:
                    _last = _last.replace(tzinfo=timezone.utc)
                _age_min = (_now - _last).total_seconds() / 60.0
                if _age_min > consec_cooldown_min:
                    logger.info(
                        "Risk sync: consecutive-loss streak stale (age=%.0fmin > %dmin) "
                        "→ reset brake (consec %d→0)",
                        _age_min, consec_cooldown_min, consec,
                    )
                    consec = 0
            except Exception as exc:  # noqa: BLE001
                logger.debug("Risk sync: consec age check failed: %s", exc)
        result["consecutive_loss"] = consec
    except Exception as exc:  # noqa: BLE001
        logger.warning("Risk sync: consecutive_loss compute failed: %s", exc)

    # 2) daily_level：今日平仓 SUM(profit) 映射 low/med/high（风险门槛偏移依据）
    try:
        row = await db.fetch(
            "SELECT COALESCE(SUM(profit), 0) AS pnl FROM hcm_trading.orders "
            "WHERE DATE(close_time) = CURRENT_DATE"
        )
        pnl = float(row[0]["pnl"]) if row else 0.0
        result["pnl"] = pnl
        result["daily_level"] = "high" if pnl <= -20 else ("med" if pnl <= -10 else "low")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Risk sync: daily_level compute failed: %s", exc)

    # 3) event_window：暂无财经日历源，留 manual 覆盖 / 默认 False（F3 触发依据）
    #    _read_co_risk_state 解析 "1"/"true"/"yes"/"on" 为 True。

    # 写入 Redis（与 _read_co_risk_state 解析严格匹配）
    try:
        await redis.set(RISK_KEY_CONSEC_LOSS, str(result["consecutive_loss"]), ex=ex)
        await redis.set(RISK_KEY_DAILY_LEVEL, result["daily_level"], ex=ex)
        await redis.set(RISK_KEY_EVENT_WINDOW, "1" if event_window_manual else "0", ex=ex)
        logger.info(
            "Risk state synced -> consec=%s level=%s manual_evt=%s",
            result["consecutive_loss"], result["daily_level"], event_window_manual,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Risk sync: Redis write failed: %s", exc)

    return result
