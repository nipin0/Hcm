"""共源信号 — P3 FORCE_CLOSE 强制平仓检测器（PRD §4.4）。

监控 H1 多周期趋势翻转：
  1. 每个 M5 bar 检查 ``h1_context.regime`` 是否从 BULLISH↔BEARISH 翻转
  2. 连续 ``co.exec.fc_bar_confirm`` 根 bar 确认新方向才触发（防假突破）
  3. 当前 H1 ADX ≥ ``co.exec.fc_adx_min`` 才有效（无势不强制平仓）
  4. 触发后向 ``signal:risk_passed`` 流发布 FORCE_CLOSE 信号

    bridge 不改造：published 信号携带 ``direction=FORCE_CLOSE``
    和 ``close_mode`` 字段，bridge 的 latest-wins / dedup / rate-limit
    沙箱天然覆盖。bridge 侧消费 FORCE_CLOSE 的实现为后续 Phase。

用法（由 scheduler 在每次产生 H1 上下文后调用）：::

    result = await self._force_close.check_and_publish(
        symbol, h1_context, h1_adx
    )
    if result:
        logger.info("FORCE_CLOSE triggered: %s", result["reason"])
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

RISK_PASSED_STREAM = "signal:risk_passed"
STREAM_MAXLEN = 10000


class ForceCloseDetector:
    """H1 趋势翻转检测器 + FORCE_CLOSE 信号发布。

    每个 symbol 独立维护状态（前一次 regime + 连续翻转 bar 计数器）。
    翻转确认后重置计数，避免同一翻转重复触发。
    """

    def __init__(self, config_provider: Any = None, redis_client: Any = None) -> None:
        self._cfg = config_provider
        # 兼容 raw redis.asyncio.Redis 与 RedisClient wrapper（后者 .raw → raw client）
        self._redis = getattr(redis_client, "raw", redis_client) if redis_client else None

        # ── 配置参数（默认值；load_config 从 provider 热加载覆盖）──
        self._enabled: bool = True
        self._close_mode: str = "all"
        self._bar_confirm: int = 3
        self._adx_min: float = 30.0
        self._check_interval_min: int = 15

        # ── 反转 8 态判定表（PRD §4.4.1）──
        # BULLISH → BEARISH → flip to close_all BUY positions
        # BEARISH → BULLISH → flip to close_all SELL positions
        self._flip_map: dict[tuple[str, str], bool] = {
            ("BULLISH", "BEARISH"): True,
            ("BEARISH", "BULLISH"): True,
            ("RANGE", "BULLISH"): False,     # 区间突破不算"翻转"，等下一根 bar 确认新趋势
            ("RANGE", "BEARISH"): False,
            ("TRANSITION", "BULLISH"): True,  # 过渡→确认新方向
            ("TRANSITION", "BEARISH"): True,
        }

        # ── 每品种翻转状态 ──
        self._prev_regime: dict[str, str] = {}
        self._reversal_count: dict[str, int] = {}
        self._last_trigger_time: dict[str, float] = {}  # 冷却：同一翻转 30 分钟内不重复触发

    # ── 配置加载 ────────────────────────────────

    async def load_config(self) -> None:
        """从 ConfigProviderV3 加载 FORCE_CLOSE 参数（约束 ①：零硬编码）。"""
        if self._cfg is None:
            logger.warning("ForceCloseDetector: no config_provider, using defaults")
            return
        try:
            self._enabled = await self._cfg.get_bool("co.exec.force_close_enabled", True)
            self._close_mode = await self._cfg.get("co.exec.fc_close_mode", "all")
            self._close_mode = (self._close_mode or "all").strip()
            self._bar_confirm = await self._cfg.get_int("co.exec.fc_bar_confirm", 3)
            self._adx_min = await self._cfg.get_float("co.exec.fc_adx_min", 30.0)
            self._check_interval_min = await self._cfg.get_int("co.exec.position_check_min", 15)
            logger.info(
                "ForceCloseDetector config loaded: enabled=%s mode=%s confirm=%d adx_min=%.1f interval=%dmin",
                self._enabled, self._close_mode, self._bar_confirm, self._adx_min,
                self._check_interval_min,
            )
        except Exception as exc:
            logger.warning("ForceCloseDetector config load failed: %s (using defaults)", exc)

    # ── 主检测入口 ───────────────────────────────

    async def check_and_publish(
        self,
        symbol: str,
        h1_context: Any,
        h1_adx: Optional[float] = None,
        now: Optional[float] = None,
    ) -> Optional[dict]:
        """检测 H1 翻转，若确认则发布 FORCE_CLOSE 到 ``signal:risk_passed``。

        Args:
            symbol: 品种名。
            h1_context: ``H1Context`` 实例（需含 ``regime`` 字符串 + ``trend_strength/adx``）。
            h1_adx: 当前 H1 ADX（用于门槛判定）；若 None 则跳过 ADX 检查。
            now: Unix timestamp（用于冷却判定），默认 ``time.time()``。

        Returns:
            触发时返回 ``{reason, prev_regime, new_regime, close_mode, ...}``，
            未触发返回 None。
        """
        if not self._enabled or self._redis is None:
            return None

        if h1_context is None:
            return None

        new_regime = getattr(h1_context, "regime", None)
        if not new_regime:
            return None
        new_regime = str(new_regime).upper()

        # H1 ADX 从 context 或显式传入
        adx = h1_adx
        if adx is None:
            adx = getattr(h1_context, "adx", None)
            if adx is None:
                adx = getattr(h1_context, "trend_strength", None)
        if adx is not None:
            adx = float(adx)

        prev = self._prev_regime.get(symbol)

        # ── 首次进入：记录初始 regime ──
        if prev is None:
            self._prev_regime[symbol] = new_regime
            self._reversal_count[symbol] = 0
            return None

        # ── 同向：重置计数器 ──
        if new_regime == prev:
            self._reversal_count[symbol] = 0
            return None

        # ── 检查翻转判定表 ──
        pair = (prev, new_regime)
        is_flip = self._flip_map.get(pair, False)

        if not is_flip:
            # 不构成翻转 → 更新 regime 但重置计数
            self._prev_regime[symbol] = new_regime
            self._reversal_count[symbol] = 0
            return None

        # ── 积累确认 bar 数 ──
        cnt = self._reversal_count.get(symbol, 0) + 1
        self._reversal_count[symbol] = cnt

        if cnt < self._bar_confirm:
            return None  # 尚未确认，等待更多 bar

        # ── ADX 门槛 ──
        if adx is not None and adx < self._adx_min:
            logger.debug(
                "FORCE_CLOSE suppressed for %s: ADX=%.1f < min=%.1f",
                symbol, adx, self._adx_min,
            )
            # 不清理计数——ADX 可能稍后回升
            return None

        # ── 冷却：同一符号 30 分钟内不重复触发 ──
        ts = now or time.time()
        last_trigger = self._last_trigger_time.get(symbol, 0)
        if ts - last_trigger < 1800:
            logger.debug(
                "FORCE_CLOSE cooldown for %s: last trigger %.0fs ago",
                symbol, ts - last_trigger,
            )
            self._reversal_count[symbol] = 0
            self._prev_regime[symbol] = new_regime
            return None

        # ── 触发 FORCE_CLOSE ──
        self._prev_regime[symbol] = new_regime
        self._reversal_count[symbol] = 0
        self._last_trigger_time[symbol] = ts

        close_mode = self._close_mode
        # PRD §4.4.1 half_on_weakening 特殊逻辑：
        # 若 ADX 刚过门槛（处于 30-35 之间），退化为 half
        if close_mode == "half_on_weakening" and adx is not None and adx < 35:
            close_mode = "half"

        signal_data = {
            "signal_type": "force_close",
            "symbol": symbol,
            "direction": "FORCE_CLOSE",
            "close_mode": close_mode,
            "prev_regime": prev,
            "new_regime": new_regime,
            "adx": round(adx, 1) if adx is not None else None,
            "bar_confirm": self._bar_confirm,
            "signal_generated_at": datetime.now(timezone.utc).isoformat(),
        }

        adx_str = f"{adx:.1f}" if adx is not None else "N/A"

        result = {
            "reason": (
                f"H1 reversal {prev}→{new_regime} confirmed "
                f"({self._bar_confirm} bars, ADX={adx_str}) "
                f"mode={close_mode}"
            ),
            **signal_data,
        }

        if not await self._publish(signal_data):
            logger.error("FORCE_CLOSE publish failed for %s", symbol)
            return None

        logger.warning("FORCE_CLOSE TRIGGERED: %s", result["reason"])
        return result

    # ── 发布到 Redis Stream ──────────────────────

    async def _publish(self, data: dict) -> bool:
        """XADD 到 ``signal:risk_passed``（bridge 消费同一流，不需要改动）。"""
        if self._redis is None:
            return False
        try:
            # Redis Stream field values must be strings
            str_data = {k: json.dumps(v) if not isinstance(v, (str, bytes)) else str(v)
                        for k, v in data.items() if v is not None}
            await self._redis.xadd(RISK_PASSED_STREAM, str_data, maxlen=STREAM_MAXLEN)
            return True
        except Exception as exc:
            logger.error("Force close XADD failed: %s", exc)
            return False

    # ── 状态重置（回测/重新加载时使用）──────────

    def reset(self, symbol: Optional[str] = None) -> None:
        """清空指定或全部品种的翻转状态。"""
        if symbol:
            self._prev_regime.pop(symbol, None)
            self._reversal_count.pop(symbol, None)
            self._last_trigger_time.pop(symbol, None)
        else:
            self._prev_regime.clear()
            self._reversal_count.clear()
            self._last_trigger_time.clear()
