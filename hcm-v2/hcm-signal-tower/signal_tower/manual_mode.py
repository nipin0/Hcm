"""手动模式 — 全部继承主账号所有动作（Manually Mirror Master Account）.

当 ``signal_tower.mode == "manual"`` 时，信号塔不产生自有信号，转而：
  1. 从 Redis Stream ``manual_mode:master_stream:{symbol}`` 阻塞读取主账户最新事件
  2. 将主账户的每笔开仓/平仓/改单动作**原样镜像**到当前子账户
  3. 镜像信号经 ``SignalPublisher.publish()`` 走 ``signal:stream → risk → risk_passed → bridge`` 全链路

前置依赖：
  - bridge 或 copy-trading 服务需在每次主账户完成交易后 SET
    ``manual_mode:master_trade:{symbol}`` → JSON ``{direction, lot, sl, tp, action, price, signal_id}``
  - 若该 key 不存在，手动模式**退化为只读、不产生任何交易信号**（安全回退）

安全：
  - 信号去重：通过 ``signal_id`` + ``bridge:processed:{signal_id}`` 防重复执行
  - 空/过期数据：key 超时 120s 自动清理，防止消费陈旧主账户数据
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

MANUAL_STREAM_KEY = "manual_mode:master_stream:{symbol}"
MANUAL_CONSUMER_GROUP = "manual-mode-group"
MANUAL_CONSUMER_NAME = "st-manual"
# 消费组以 id="$" 创建 → 仅消费新建事件，避免信号塔重启重放陈旧持仓（手动镜像安全要求）


class ManualModeHandler:
    """手动模式镜像引擎 — 读取主账户交易并发布到子账户信号流。"""

    def __init__(self, redis_client: Any = None, config_provider: Any = None) -> None:
        self._redis = getattr(redis_client, "raw", redis_client) if redis_client else None
        self._cfg = config_provider
        # 复合去重键 (signal_id, action) 缓存（防重跑；本进程内）
        # 彻底修正：open/close/modify/partial_close/add 各自独立，不再因共用 ticket 塌缩成一条
        self._mirrored_ids: set[str] = set()
        self._mirrored_ids_max = 200
        # 已在 LIST 里"看到过"的复合键（防 list 内同 ticket 同 action 重复 publish）
        self._seen_keys: set[str] = set()
        self._seen_sids_max = 500

    async def load_config(self) -> None:
        """热加载配置（当前无独立参数，预留扩展）。"""
        pass  # 后续可加 manual_master_account_id 等

    async def get_master_trade(self, symbol: str, block_ms: int = 300) -> Optional[dict]:
        """从 manual_mode:master_stream:{symbol} Stream 阻塞读取主账户最新事件（事件驱动）。

        P1优化：原实现轮询 LIST（每 1s 一次 sleep）；现改用 XREADGROUP 阻塞消费，
        新事件到达即返回（延迟 ≤ block_ms），消除固定 1s 轮询延迟。
        返回事件 dict（含 _msg_id 字段供 XACK），无事件时返回 None。
        """
        if self._redis is None:
            return None
        key = MANUAL_STREAM_KEY.format(symbol=symbol)
        try:
            # 确保消费组存在（仅新事件；BUSYGROUP 已有则忽略）
            try:
                await self._redis.xgroup_create(key, MANUAL_CONSUMER_GROUP, id="$", mkstream=True)
            except Exception as e:
                if "BUSYGROUP" in str(e):
                    # 重启场景：组已存在且游标停在停机前的位置 → 会把停机期间产生的
                    # 事件重放（陈旧持仓被重新镜像）。手动镜像安全起见，重置游标到
                    # stream 尾，仅消费启动后的新事件。仅首次创建/重启时做一次。
                    try:
                        await self._redis.xgroup_setid(key, MANUAL_CONSUMER_GROUP, "$")
                    except Exception:
                        pass
                else:
                    logger.debug("xgroup_create skipped for %s: %s", symbol, e)
            # 阻塞读最新一条未投递事件
            resp = await self._redis.xreadgroup(
                MANUAL_CONSUMER_GROUP, MANUAL_CONSUMER_NAME,
                {key: ">"}, count=1, block=block_ms,
            )
            if not resp:
                return None
            stream_msgs = resp[0][1]
            if not stream_msgs:
                return None
            msg_id, fields = stream_msgs[0]
            event = self._decode_stream_fields(fields)
            event["_msg_id"] = msg_id  # 供 mirror_trade 成功后 XACK
            return event
        except Exception as exc:
            logger.debug("Failed to read master trade stream for %s: %s", symbol, exc)
            return None

    def _decode_stream_fields(self, fields) -> dict:
        """Stream 字段值可能是 bytes，统一解码为 str（数值字段由 mirror_trade 解析回数值）。"""
        out = {}
        for k, v in fields.items():
            key = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            out[key] = val
        return out

    async def mirror_trade(
        self,
        symbol: str,
        master_trade: dict,
        signal_publisher: Any,
    ) -> Optional[dict]:
        """将主账户交易镜像为子账户信号并发布。

        Args:
            symbol: 当前品种。
            master_trade: 主账户交易数据。
                ``{direction, lot, sl, tp, action, price, signal_id, account_id}``
            signal_publisher: ``SignalPublisher`` 实例。

        Returns:
            发布的信号数据或 None（重复/无效）。
        """
        # 复合去重键 (signal_id, action)：open/close/modify/partial_close/add 各自独立
        master_sid = master_trade.get("signal_id", 0)
        action = str(master_trade.get("action", "open")).lower()
        dedup_key = f"{master_sid}:{action}"
        if dedup_key and dedup_key in self._mirrored_ids:
            logger.debug("Manual mode: %s already mirrored, skipping", dedup_key)
            return None

        direction = str(master_trade.get("direction", "")).upper()
        # 仅镜像有效方向（BUY/SELL）和 动作类（CLOSE/MODIFY/PARTIAL_CLOSE/ADD）
        valid_open_dir = direction in ("BUY", "SELL")
        valid_action = action in ("close", "modify", "partial_close", "add")
        if not valid_open_dir and not valid_action:
            logger.debug("Manual mode: unsupported direction=%s action=%s, skipping", direction, action)
            return None

        # 生成镜像 signal_data
        lot = float(master_trade.get("lot", 0.01))
        entry_price = float(master_trade.get("price", 0))
        sl = float(master_trade.get("sl", 0))
        tp = float(master_trade.get("tp", 0))

        # 方向映射：open/add → BUY/SELL；close → CLOSE；modify → MODIFY；partial_close → PARTIAL_CLOSE
        if action == "close":
            out_direction = "CLOSE"
        elif action == "modify":
            out_direction = "MODIFY"
        elif action == "partial_close":
            out_direction = "PARTIAL_CLOSE"
        else:
            out_direction = direction  # open / add → BUY/SELL

        # 精确复刻：close/modify/partial_close 携带主号 ticket，供跟单号按票定位平仓/改单
        close_mode = ""
        close_ticket = 0
        if action in ("close", "modify", "partial_close"):
            close_mode = str(master_trade.get("close_mode", "ticket"))
            try:
                close_ticket = int(master_sid)
            except (TypeError, ValueError):
                close_ticket = 0

        from signal_tower.signal_publisher import SignalData

        mirror_signal = SignalData(
            symbol=symbol,
            direction=out_direction,
            lot=lot,
            entry_price=entry_price,
            sl_price=sl,
            tp1=tp,
            confidence=1.0,  # 手动模式置信度=100%（直接镜像）
            signal_mode="manual_mirror",
            signal_id=int(master_sid),
            action=action,
            close_mode=close_mode,
            close_ticket=close_ticket,
            # magic 从主号 payload 透传：跟单号下单时使用主号原 magic（而非硬编码 123456）
            magic=int(master_trade.get("magic") or 0),
            # account_id 取主号 id（生产者 A 写入 payload.account_id），
            # copy-trading 按 master_account_id == account_id 匹配跟单关系（stream_consumer.py:232）
            account_id=int(master_trade.get("account_id") or 0),
            fallback_reason=f"Manual mirror from master (sid={master_sid}, action={action})",
        )

        # 发布
        if signal_publisher is None:
            logger.warning("Manual mode: no signal_publisher, cannot mirror trade")
            return None

        success = await signal_publisher.publish(mirror_signal)
        if success:
            # 复合键缓存（stream 模式下去重，open/close/modify/partial_close/add 互不干扰）
            self._mirrored_ids.add(dedup_key)
            self._seen_keys.add(dedup_key)
            if len(self._mirrored_ids) > self._mirrored_ids_max:
                # FIFO 清理：移除最旧的 50 个
                old = sorted(self._mirrored_ids)[:50]
                self._mirrored_ids.difference_update(old)
            if len(self._seen_keys) > self._seen_sids_max:
                old_seen = sorted(self._seen_keys)[:100]
                self._seen_keys.difference_update(old_seen)
            # [2026-07-24 ack 根因修复] XACK 已上移至 _manual_mode_loop 的 finally 块
            # 无条件执行（原实现仅在 publish 成功时 ack；去重跳过/无效方向/发布失败三条
            # 路径都不 ack → XPENDING 堆积至 5787，重启 xgroup_setid("$") 抛历史消息）。
            # 此处不再 ack，保留 _msg_id 供调用方使用。

            logger.info(
                "Manual mode: mirrored master trade %s → %s %s lot=%.2f",
                dedup_key, symbol, out_direction, lot,
            )
            return {"mirrored": True, "master_signal_id": master_sid, "action": action,
                    "direction": out_direction, "lot": lot}
        else:
            logger.warning("Manual mode: publish failed for mirrored trade %s", dedup_key)
            return None

    async def ack_master_trade(self, symbol: str, msg_id) -> None:
        """XACK 已消费的事件（避免 XPENDING 堆积 + 重启重放）。

        [2026-07-24] 失败不再静默吞掉——记录 WARNING 便于排查 ack 链路。
        """
        if self._redis is None or not msg_id:
            return
        try:
            await self._redis.xack(
                MANUAL_STREAM_KEY.format(symbol=symbol), MANUAL_CONSUMER_GROUP, msg_id,
            )
        except Exception as exc:
            logger.warning("XACK failed for %s msg_id=%s: %s", symbol, msg_id, exc)

    async def is_manual_mode(self) -> bool:
        """检查 ``signal_tower.mode`` 是否为 manual。"""
        if self._cfg is None:
            return False
        try:
            mode = (await self._cfg.get("signal_tower.mode")) or "co_source"
            return mode.strip().lower() == "manual"
        except Exception:
            return False
