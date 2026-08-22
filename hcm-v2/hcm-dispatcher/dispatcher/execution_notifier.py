"""ExecutionNotifier — 真实开仓事件 → 钉钉通知 的单一事实源。

背景:
  原通知链路由 dispatcher 的 OrderTracker 驱动, 而 dispatcher 运行在 gateway
  stub 模式(无编译 protobuf), 对任何方向都返回 code=0 假成功 → 推的是假 ticket,
  且会把 NO_TRADE 信号误推成 "No-trade" 订单信息。

  现改为: 真实在 MT5 开仓的组件(mt5_bridge / 将来 hcm-gateway)在下单成功后,
  向 Redis Stream `order:executed` 发布真实事件(真实 ticket / 成交价 / 方向 / 账户角色),
  本消费者消费该流并调用 Notifier.on_order, 使钉钉只在 MT5 真开仓时响。

账户角色:
  主号桥(IS_MASTER)发布 account_role="master", 跟单桥(IS_FOLLOWER)发布
  account_role="follower"。按用户要求钉钉【仅通知主号】: follower / standalone
  等非 master 事件直接丢弃(不推送); account_role 透传仅作日志/兜底用途。

设计:
  - 独立消费组 exec-notify-group, 与主分发消费组(signal:risk_passed)解耦。
  - 消费即 xack, 失败不进死信(通知丢失可接受, 但启动时会先回收 pending 防丢)。
  - 去重: 同一 (account_id, mt5_ticket) 在 DEDUP_TTL 内只推一次, 防 pending
    回收重放 / 重复投递造成同一条真实开仓被多次推送(例如历史双 mirror 重复单)。
  - 字段已 JSON 解析(数字串变 int/float), _handle 统一 coerce。
"""

import asyncio
import logging
import time
from typing import Optional

from shared.redis_client import RedisClient

log = logging.getLogger("dispatcher.execution_notifier")

ORDER_EXECUTED_STREAM = "order:executed"
GROUP_NAME = "exec-notify-group"
CONSUMER_NAME = "exec-notify-1"
DEFAULT_BLOCK_MS = 2000
# 去重窗口: 同一 (account_id, ticket) 在该窗口内只推一次
DEDUP_TTL = 3600.0


class ExecutionNotifier:
    """Consume `order:executed` and push real-open notifications via Notifier."""

    def __init__(
        self,
        redis_client: RedisClient,
        notifier: Optional[object] = None,
        consumer_name: str = CONSUMER_NAME,
        block_ms: int = DEFAULT_BLOCK_MS,
    ):
        self._redis = redis_client
        self._notifier = notifier
        self._consumer = consumer_name
        self._block_ms = block_ms
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # 去重表: (account_id, ticket) -> 过期时间戳
        self._seen: dict = {}

    # ── Lifecycle ───────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        # 幂等建组(mkstream 确保流不存在也能建)
        try:
            await self._redis.xgroup_create(
                ORDER_EXECUTED_STREAM, GROUP_NAME, mkstream=True, start_id="0"
            )
        except Exception as exc:  # BUSYGROUP 等已存在情况
            log.debug("xgroup_create order:executed ignored: %s", exc)

        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("ExecutionNotifier started (group=%s)", GROUP_NAME)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def health_check(self) -> dict:
        return {"status": "running" if self._running else "stopped"}

    # ── Loop ────────────────────────────────────

    async def _loop(self) -> None:
        # 启动先回收上轮残留 pending, 避免崩溃期间丢失通知
        await self._recover_pending()
        while self._running:
            try:
                msgs = await self._redis.xreadgroup(
                    GROUP_NAME, self._consumer,
                    {ORDER_EXECUTED_STREAM: ">"},
                    count=10, block=self._block_ms,
                )
                if not msgs:
                    continue
                for m in msgs:
                    try:
                        await self._handle(m.data)
                    finally:
                        await self._redis.xack(ORDER_EXECUTED_STREAM, GROUP_NAME, m.message_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ExecutionNotifier loop error: %s", exc)
                await asyncio.sleep(1)

    async def _recover_pending(self) -> None:
        try:
            pending = await self._redis.xpending(ORDER_EXECUTED_STREAM, GROUP_NAME)
            if not pending:
                return
        except Exception as exc:
            log.debug("xpending order:executed ignored: %s", exc)
            return
        # 用 "0" 读取本消费者未 ack 的 pending, 处理并 xack
        while self._running:
            try:
                msgs = await self._redis.xreadgroup(
                    GROUP_NAME, self._consumer,
                    {ORDER_EXECUTED_STREAM: "0"},
                    count=10, block=1000,
                )
            except Exception as exc:
                log.warning("ExecutionNotifier recover error: %s", exc)
                return
            if not msgs:
                break
            for m in msgs:
                try:
                    await self._handle(m.data)
                finally:
                    await self._redis.xack(ORDER_EXECUTED_STREAM, GROUP_NAME, m.message_id)

    # ── Handle ──────────────────────────────────

    async def _handle(self, data: dict) -> None:
        def num(v, default=0):
            try:
                return float(v) if v not in (None, "") else default
            except (TypeError, ValueError):
                return default

        account_id = str(data.get("account_id", "?"))
        ticket = int(num(data.get("mt5_ticket"), 0))
        account_role = str(data.get("account_role", "master"))

        # ── 钉钉仅通知主号 (2026-07-23 按用户要求) ──
        # follower / standalone 等非 master 事件直接丢弃, 不推送钉钉。
        # 注意: xack 在 _loop 的 finally 中照常执行, 不会堆积 pending。
        if account_role != "master":
            log.info(
                "skip non-master order:executed (%s) %s ticket=%s — notify master-only",
                account_role, account_id, ticket,
            )
            return

        # 去重: 同一 (account_id, ticket) 在 TTL 内只推一次
        # （防 pending 回收重放 / 重复投递造成同一条真实开仓多次推送）。
        # 主号与跟单号 account_id 不同(17 vs 20), 各自独立计数, 互不干扰。
        if ticket:
            now = time.time()
            expired = [k for k, exp in self._seen.items() if exp <= now]
            for k in expired:
                self._seen.pop(k, None)
            key = (account_id, ticket)
            if key in self._seen:
                log.info(
                    "dedup skip duplicate order:executed account=%s ticket=%s",
                    account_id, ticket,
                )
                return
            self._seen[key] = now + DEDUP_TTL

        order = {
            "account_id": account_id,
            "account_role": account_role,
            "symbol": str(data.get("symbol", "?")),
            "direction": str(data.get("direction", "")).upper(),
            "lot": num(data.get("lot"), 0.0),
            "filled_price": num(data.get("filled_price"), 0.0),
            "entry_price": num(data.get("entry_price"), 0.0),
            "mt5_ticket": ticket,
            "ts": num(data.get("ts"), 0.0),
        }
        log.info(
            "📨 order:executed → notifier (%s) %s %s %s ticket=%s",
            account_role, account_id, order["symbol"], order["direction"], ticket,
        )
        if self._notifier is not None:
            try:
                await self._notifier.on_order(order)
            except Exception as exc:
                log.warning("ExecutionNotifier on_order failed: %s", exc)
