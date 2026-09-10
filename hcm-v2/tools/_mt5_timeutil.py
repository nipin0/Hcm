"""MT5 服务器时间 → 真实 UTC 的统一校正入口（零依赖）。

背景
----
MT5 的 ``position.time`` / ``deal.time`` / ``symbol_info_tick().time`` 返回的是
**经纪商服务器时钟**，不是 UTC。生产实测（XAUUSD）与 UTC 差 **+3 小时**
（2026-09-05 在 mt5_bridge.py 的 ``_PATH_STATE`` 处已实证同一个坑：订单
20:27:10Z 被记成 23:30:09Z）。

代码里凡是用 ``datetime.fromtimestamp(t, tz=timezone.utc)`` 直接把该时间戳
当 UTC 解释的地方，得到的 ``open_time`` 都比真实 UTC **大 3 小时**。已造成两个后果：

1. **position_sync 写库**：``hcm_trading.positions.open_time`` 偏大 3h，而平仓时
   写入的 ``close_time`` 用的是 ``datetime.now(timezone.utc)``（正确的真值），
   于是 ``close_time - open_time = 真实持仓时长 - 180min``。短仓（SL/手平，
   0~9 分钟）直接变成负数 → 约 25.7% 的记录出现 ``close_time < open_time``
   时序倒挂，持仓时长统计失真。

2. **REV 反转头 payload**：写入 Redis ``hcm:ai:rev:req:{acct}:{ticket}`` 的
   ``open_time`` 同样偏大 3h，AI 侧据此计算的「持仓时长 / 持仓多久才浮亏」
   全部失真 —— 这是功能影响（可能改变反转判定），不只是统计影响。

意图
----
把「减偏移」这件事收敛到**一个模块**，避免每处各自演化、再漏改第三处。
所有调用点只需 ``mt5_time_to_utc(pos.time)``。

设计原则（生产系统，零回归硬要求）
----------------------------------
* 零依赖：不 import MetaTrader5 / redis / asyncpg，只依赖标准库，任何环境都能 import。
* 永不抛异常：``mt5_time_to_utc`` 内部全 try/except，异常路径返回 ``None``，
  由调用方决定兜底（通常是 ``now``），绝不让原本能跑的逻辑挂掉。
* 探测结果做合理性校验，异常值不采纳，回退到已缓存值 / 默认偏移。

Usage:
    from _mt5_timeutil import mt5_time_to_utc, detect_mt5_tz_offset

    open_time = mt5_time_to_utc(pos.time) or now
"""

import os
import time
from datetime import datetime, timezone

# ── 默认偏移（小时）──────────────────────────────────────────────────────────
# 生产实测 +3h。可用环境变量 BROKER_TZ_OFFSET_HOURS 覆盖（换经纪商时无需改码）。
# 解析失败时静默回退 3，绝不因环境变量写错而让模块 import 失败。
try:
    BROKER_TZ_OFFSET_HOURS = int(os.environ.get("BROKER_TZ_OFFSET_HOURS", "3"))
except Exception:
    BROKER_TZ_OFFSET_HOURS = 3

# ── 探测结果的合理性区间（含端点）：地球时区物理范围 UTC-12 ~ UTC+14 ──────────
_OFFSET_MIN_SEC = -12 * 3600
_OFFSET_MAX_SEC = 14 * 3600

_DEFAULT_OFFSET_SEC = BROKER_TZ_OFFSET_HOURS * 3600

# ── 模块级缓存：探测成功后写入，后续调用直接复用（经纪商时区内不会漂移）───────
_MT5_TZ_OFFSET_SEC = None


def _fallback_offset_sec() -> int:
    """缓存优先，无缓存则用默认偏移。"""
    if _MT5_TZ_OFFSET_SEC is not None:
        return _MT5_TZ_OFFSET_SEC
    return _DEFAULT_OFFSET_SEC


def detect_mt5_tz_offset(mt5, symbol) -> int:
    """动态探测「MT5 服务器时间 − UTC」的偏移秒数。

    原理：``symbol_info_tick(symbol).time`` 是服务器时钟的 epoch 秒，
    与本地 ``time.time()``（epoch 秒，与时区无关）相减即得偏移。

    结果做合理性校验：落在 [UTC-12h, UTC+14h] 之外视为异常（终端未连接、
    tick 过期、symbol 名不对导致取到脏值等），**不采纳**，回退到已缓存值；
    没缓存则回退 ``BROKER_TZ_OFFSET_HOURS * 3600``。探测成功则写入缓存。

    永不抛异常。

    Args:
        mt5: MetaTrader5 模块（或任何提供 symbol_info_tick 的对象）；为 None 时走回退。
        symbol: **真实**经纪商 symbol（如 "XAUUSD.m"），不要用逻辑 symbol。

    Returns:
        偏移秒数（服务器时间 − UTC），生产实测 10800。
    """
    global _MT5_TZ_OFFSET_SEC
    if _MT5_TZ_OFFSET_SEC is not None:
        return _MT5_TZ_OFFSET_SEC
    try:
        off = int(mt5.symbol_info_tick(symbol).time) - int(time.time())
    except Exception:
        return _fallback_offset_sec()
    if off < _OFFSET_MIN_SEC or off > _OFFSET_MAX_SEC:
        # 异常值不采纳（也不缓存），避免一次脏 tick 污染整轮运行
        return _fallback_offset_sec()
    _MT5_TZ_OFFSET_SEC = off
    return off


def mt5_time_to_utc(mt5_time, offset_sec=None):
    """把 MT5 服务器时间戳转成真实 UTC 的 timezone-aware datetime。

    Args:
        mt5_time: MT5 时间戳（秒）。0 / None / 非法值 → 返回 ``None``。
        offset_sec: 偏移秒数。为 ``None`` 时用缓存值，无缓存用
            ``BROKER_TZ_OFFSET_HOURS * 3600``。

    Returns:
        ``datetime(timezone.utc)``；输入无效或任何异常时返回 ``None``，
        由调用方决定兜底（例如 ``or now``）。**永不抛异常。**
    """
    try:
        if not mt5_time:
            return None
        ts = int(mt5_time)
        if ts <= 0:
            return None
        if offset_sec is None:
            offset_sec = _fallback_offset_sec()
        return datetime.fromtimestamp(ts - int(offset_sec), tz=timezone.utc)
    except Exception:
        return None
