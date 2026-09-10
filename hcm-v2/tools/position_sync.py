"""Position Sync Engine — PG + Redis dual-write for MT5 positions.

同步 MT5 实时持仓到 PostgreSQL 和 Redis，每次主循环执行。
独立于 _update_trailing_stops()，专注数据同步（SL 管理与状态同步解耦）。

Usage:
    from tools.position_sync import sync_positions
    await sync_positions(mt5, pool, redis_conn)
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

# MT5 pos.time 是经纪商服务器时间（实测比 UTC 快 3h），必须经统一入口校正后再写库，
# 否则 open_time 偏大 3h → 与 close_time(真值) 倒挂。详见 _mt5_timeutil 模块 docstring。
from _mt5_timeutil import mt5_time_to_utc, detect_mt5_tz_offset

log = logging.getLogger("mt5_bridge")


# ═══════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════


async def sync_positions(mt5, pool, redis_conn, account_id_mode: Optional[int] = None) -> None:
    """主入口：遍历 MT5 持仓，对每个持仓执行 PG + Redis 双写。

    PG 和 Redis 写入使用独立 try/except，一方失败不影响另一方。
    每次主循环都执行（~5s 间隔），确保 PG/Redis 与 MT5 实时一致。

    Args:
        mt5: MT5 connection object.
        pool: asyncpg connection pool.
        redis_conn: Redis connection (sync client, decode_responses=True).
    """
    try:
        positions = mt5.positions_get()
    except Exception as e:
        log.error(f"sync_positions: mt5.positions_get() failed: {e}")
        return

    # 加固A1：MT5 软断时 positions_get() 返回 None 而非抛异常
    # （之前 'NoneType' object is not iterable 的根因）。防御后安静跳过本轮。
    if positions is None:
        log.warning(
            "sync_positions: mt5.positions_get() returned None "
            "(MT5 disconnected?); skipping this cycle"
        )
        return

    # 读取 ATR 用于 trail_tier 判定
    atr = _get_atr_from_redis(redis_conn, "XAUUSD")

    # 解析真实账户（active master，生产=6），供同步与对账共用
    account_id = await _resolve_account_id(pool)

    # [2026-07-24 跟单修复] 本桥实际账户 = 本桥连接的账户(account_id_mode)。PG 写入与对账
    # 必须用它，不能用 active master(account_id)。否则跟单桥(连 account 20 的 MT5)会用 account_id=17
    # 把 account 20 的持仓误写 account 17，并用 account 20 的 ticket 集合误清 account 17 主号活单
    # （日志 Pos reconcile #...closed + Stale reconciliation ... for account 17）。
    # 主号桥 account_id_mode==active master，self_account==account_id，行为等价不变。
    self_account = account_id_mode if account_id_mode is not None else account_id

    # 手动模式镜像生产者开关：本桥为主号(account_id_mode == 解析出的 active master)
    # → 始终检测主号 MT5 手动持仓并发布 master_stream，独立于 signal_tower.mode。
    # 与信号塔 scheduler 的 always-on manual mirror 配套，实现「手动单与模型信号并存」。
    # （跟单桥的 account_id_mode 为其自身跟单号 id，≠ 主号 → 不会误发布造成循环）
    _manual_publish = (
        account_id_mode is not None
        and account_id_mode == account_id
    )

    live_tickets: set[int] = set()
    for pos in positions:
        ticket = pos.ticket
        try:
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                log.warning(f"Pos sync #{ticket}: no tick data, skipping")
                continue

            # 计算当前价格与浮动盈亏（以 MT5 实时数据为准，不读 PG stale 数据）
            if pos.type == 0:  # BUY
                current_price = tick.bid
                # float_profit: (bid - open_price) * lot * 100
                float_profit = round((tick.bid - pos.price_open) * pos.volume * 100, 2)
                direction = "BUY"
            elif pos.type == 1:  # SELL
                current_price = tick.ask
                # float_profit: (open_price - ask) * lot * 100
                float_profit = round((pos.price_open - tick.ask) * pos.volume * 100, 2)
                direction = "SELL"
            else:
                log.warning(f"Pos sync #{ticket}: unknown pos.type={pos.type}")
                continue

            # 判定当前 trail tier
            trail_tier = _compute_trail_tier(pos, atr, redis_conn)

            # 读取 PG 中已有的 trail_state（用于追加 SL 变更记录）
            existing_trail_state = None
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT trail_state FROM hcm_trading.positions WHERE mt5_ticket=$1",
                        ticket,
                    )
                    if row and row["trail_state"]:
                        raw = row["trail_state"]
                        if isinstance(raw, str):
                            existing_trail_state = json.loads(raw)
                        else:
                            existing_trail_state = raw
            except Exception:
                pass  # 静默继续，不影响主流程

            # 构建 trail_state（追加 SL 变更，保留最近 20 条）
            trail_state = _build_trail_state(
                pos, trail_tier, existing_trail_state
            )

            live_tickets.add(ticket)

            # ── 手动模式镜像生产者：本桥为主号且 manual → 主号每笔持仓即手动开仓动作 ──
            # 去重：每个 ticket 只 publish 一次（manual_mode:master_trade:{symbol} 是单值键，
            # 反复 publish 会用后到的 ticket 覆盖先到的 ticket，导致先开仓的 ticket 永远丢镜像）。
            # signal_tower 启动晚于主桥时尤其危险——只能看到最后一个 ticket。
            if _manual_publish:
                if pos.ticket not in _MANUAL_MIRRORED_TICKETS:
                    # 新持仓：发布 open 事件一次（stream 持久，无需每轮刷新 TTL）
                    # 2026-07-23 修复「开仓双 mirror」：
                    # 主号桥以模型 auto 信号下的单（hcm:signal_for_ticket:{ticket} 存在）
                    # 会经 auto-signal 路径让跟单桥自行开仓；此处若再发 manual open
                    # 会造成跟单号 2x 重复开仓（主号 1 单 → 跟单 2 单）。
                    # → auto 单【跳过 manual open 发布】，仅登记进镜像表，保留后续
                    #   modify/partial_close/add/close 镜像（跟单号仍完整继承主号）。
                    #   用户在 MT5 手动开的单（无该键）仍正常发 manual open。
                    is_auto_opened = _is_auto_opened_ticket(redis_conn, pos.ticket)
                    if not is_auto_opened:
                        _publish_manual_master_trade(redis_conn, pos, "open", account_id)
                        _publish_order_executed_notification(redis_conn, pos, account_id)
                    _MANUAL_MIRRORED_TICKETS[pos.ticket] = pos.symbol
                    _MANUAL_LAST_SNAPSHOT[pos.ticket] = {
                        "sl": float(pos.sl) if pos.sl else 0.0,
                        "tp": float(pos.tp) if pos.tp else 0.0,
                        "volume": float(pos.volume),
                    }
                else:
                    # 持仓存活期间：只检测变更并发布 modify/partial_close/add
                    # （stream 模式不再重复 publish open，避免事件洪泛）
                    _detect_and_publish_master_changes(redis_conn, pos, account_id)

            # PG 写入（独立 try/except）
            try:
                await _pg_update_position(
                    pool, pos, current_price, float_profit, trail_state, self_account, mt5
                )
            except Exception as e:
                log.error(f"PG sync failed for #{ticket}: {e}")

            # Redis 写入（独立 try/except）
            try:
                _redis_update_position(
                    redis_conn, pos, current_price, float_profit, trail_tier, trail_state
                )
            except Exception as e:
                log.error(f"Redis sync failed for #{ticket}: {e}")

            log.info(
                f"Pos sync #{ticket}: price={round(current_price, 5)}, "
                f"profit={float_profit}, tier={trail_tier}"
            )

        except Exception as e:
            log.error(f"Pos sync FAIL #{ticket}: {e}")

    # ═══ 对账：PG 记 open 但 MT5 已不存在(已平仓)的持仓 → 标记 closed ═══
    # 修复「幽灵持仓」核心缺陷：原逻辑只同步活持仓，从不把已平持仓清出 PG，
    # 导致 hcm_trading.positions 永远残留 open → 风险引擎限仓计数虚高 → 不下单。
    try:
        await _close_stale_positions(pool, self_account, live_tickets, redis_conn, mt5)
    except Exception as e:
        log.error(f"Stale position reconciliation failed: {e}")

    # ── 手动模式镜像：已平持仓 → 发 close 事件给跟单号 ──
    if _manual_publish:
        _closed = [t for t in list(_MANUAL_MIRRORED_TICKETS.keys()) if t not in live_tickets]
        for t in _closed:
            sym = _MANUAL_MIRRORED_TICKETS.pop(t)
            _MANUAL_LAST_SNAPSHOT.pop(t, None)
            _MANUAL_ADD_SEQ.pop(t, None)
            _publish_manual_master_close(redis_conn, t, sym, account_id)

    # ── 主号持仓集合快照（供跟单桥周期对账兜底，根治手动平仓事件丢失→孤儿单）──
    # 仅主号桥写入（sync_positions 只在主号桥被调，account_id_mode 即主号）；每轮刷新，TTL=30s。
    # 跟单桥读此快照对比本地持仓：主号某品种已无持仓 → 平掉跟单该品种全部持仓。
    # 这样无论手动平仓事件因桥重启/抖动是否丢失，跟单最终对齐主号，永不残留孤儿单。
    # 2026-08-18 修复：原条件 `account_id_mode == account_id` 因 _resolve_account_id 在桥
    # 启动早期 DB 未就绪时返回 None 并被永久缓存 → account_id=None → 条件永不满足 →
    # 主号快照永不刷新 → 跟单桥 reconcile 新鲜度闸门永远判超龄跳过补开 → 跟单永久缺单。
    # 改为仅判 account_id_mode is not None（sync_positions 必为主号桥调用）。
    if account_id_mode is not None:
        try:
            _publish_master_positions_snapshot(redis_conn, account_id_mode, positions)
        except Exception as e:
            log.error(f"Master positions snapshot failed: {e}")


# ═══════════════════════════════════════════════════════════════
#  PG 操作
# ═══════════════════════════════════════════════════════════════


# module-level 缓存，避免每轮 sync 重复查 DB
_RESOLVED_ACCOUNT_ID: Optional[int] = None
_RESOLVED_AT: float = 0.0
_RESOLVE_TTL: float = 60.0

# ═══════════════════════════════════════════════════════════════
#  手动模式镜像生产者（PRD: 主号手动开仓 → 写 manual_mode:master_trade:{symbol}）
#  仅当「本桥为主号(account_id_mode == 解析出的 active master)」时生效，
#  与 signal_tower.mode 无关（always-on，配套信号塔 scheduler 的 always-on manual mirror）。
#  持仓存活期间每轮刷新 TTL，防止 signal_tower 跨 bar 漏读。
# ═══════════════════════════════════════════════════════════════
_MANUAL_MIRRORED_TICKETS: dict[int, str] = {}   # ticket -> symbol（已镜像持仓）
# P1优化：手动镜像事件改用 Redis Stream（manual_mode:master_stream:{symbol}），
# 信号塔用 XREADGROUP 阻塞消费（事件驱动），消除原 1s LIST 轮询延迟。
# 每条事件 = 一个 XADD；stream 按 maxlen 自动裁剪，无需 TTL/过期处理。
# open 仅在首次检测到持仓时发布一次（stream 持久，不再每轮重发）；
# modify/partial_close/add 在检测到变更时各发布一次。
_MANUAL_STREAM_KEY = "manual_mode:master_stream:{symbol}"
_MANUAL_STREAM_MAXLEN = 2000  # 每品种最多保留 2000 条事件（防内存膨胀）

# [2026-08-25 毫秒级跟单 SL/TP] 主号 auto 持仓 SL/TP 变更（含 trailing 移动）独立流：
# 主号桥在 _update_trailing_stops 成功移动 SL/TP 时即时 XADD（真·毫秒级），
# 另由本模块 sync_positions 每 ~1s 检测 auto 持仓 SL/TP 变更兜底发布；跟单桥主循环内
# xreadgroup(block=50) 消费，经 _modify_follower_position 精确按票镜像（~50-250ms），
# 彻底消除原 10s _sync_follower_sl_tp 轮询滞后（行情已跑远）。10s 轮询保留作安全网。
_SLTP_STREAM_KEY = "hcm:master:sltp"
_SLTP_STREAM_MAXLEN = 5000  # 仅 SL/TP 变更事件，量小，保留 5000 条足够

# ═══════════════════════════════════════════════════════════════
#  手动模式镜像：持仓快照 + 加仓序号（彻底修正"跟单号冻结在开仓初值"）
#  - _MANUAL_LAST_SNAPSHOT: ticket → {sl, tp, volume}，检测主号 SL/TP 变更 / 加仓 / 部分平仓
#  - _MANUAL_ADD_SEQ: ticket → 加仓序号，保证同 ticket 多次加仓的 signal_id 唯一（不塌缩去重）
# ═══════════════════════════════════════════════════════════════
_MANUAL_LAST_SNAPSHOT: dict[int, dict] = {}
_MANUAL_ADD_SEQ: dict[int, int] = {}


def _is_auto_opened_ticket(redis_conn, ticket: int) -> bool:
    """判断主号某持仓是否由模型 auto 信号下单（而非用户在 MT5 手动开仓）。

    主号桥 _execute_signal 成功开仓后写入 hcm:signal_for_ticket:{ticket}（TTL 30d）；
    手动开仓不写此键。据此区分，使 manual-mirror 不对 auto 单重复发 open，
    根治跟单号「开仓双 mirror」（主号 1 单 → 跟单 2 单的 2x 重复开仓）。
    """
    if redis_conn is None or not ticket:
        return False
    try:
        return bool(redis_conn.exists(f"hcm:signal_for_ticket:{ticket}"))
    except Exception:
        return False


def _is_manual_mode(redis_conn) -> bool:
    """读取 signal_tower.mode，判断是否处于手动模式（与 manual_mode.py 同源配置）。"""
    if redis_conn is None:
        return False
    try:
        mode = redis_conn.hget("hcm:config:v2", "signal_tower.mode")
        return str(mode).strip().lower() == "manual"
    except Exception:
        return False


def _publish_manual_master_trade(redis_conn, pos, action: str, master_account_id: int) -> None:
    """主号一笔持仓动作 → XADD 进 manual_mode:master_stream:{symbol} Stream。

    payload: {direction, action, lot, price, sl, tp, signal_id, account_id, magic, ts}
    - account_id = 主号 id（copy-trading 按 master_account_id == account_id 匹配跟单关系）
    - signal_id 用 MT5 ticket（唯一、可作镜像去重键）
    - stream 模式：open 仅在首次检测到时发布一次（见 sync 主循环），无需每轮刷新。
    - 信号塔用 XREADGROUP 阻塞消费（事件驱动），消除原 1s LIST 轮询延迟。
    """
    if redis_conn is None:
        return
    direction = "BUY" if pos.type == 0 else "SELL"
    payload = {
        "direction": direction,
        "action": action,
        "lot": float(pos.volume),
        "price": float(pos.price_open),
        "sl": float(pos.sl) if pos.sl else 0.0,
        "tp": float(pos.tp) if pos.tp else 0.0,
        "signal_id": int(pos.ticket),
        "magic": int(pos.magic) if hasattr(pos, "magic") and pos.magic else 0,
        "account_id": int(master_account_id),
        "ts": int(time.time()),
    }
    _xadd_manual_event(redis_conn, pos.symbol, payload)


def _publish_order_executed_notification(redis_conn, pos, master_account_id: int) -> None:
    """主号手动开仓 → XADD 进 order:executed Stream，驱动钉钉/企微通知。

    2026-07-27 修复：手动开仓（人在 MT5 直接下的单）此前只经 _publish_manual_master_trade
    发 manual_mode:master_stream（仅给跟单镜像），从不发 order:executed；而 ExecutionNotifier
    仅消费 order:executed 推钉钉 → 手动单永远不通知。此处补齐：与桥 _execute_signal 自动单
    发同一流的字段格式，account_role=master（钉钉只推主号），使手动单也收到「新订单」通知。
    auto 单（hcm:signal_for_ticket 存在）    走 _execute_signal 已发 order:executed，此处仅补手动单。

    去重说明(2026-08-06 修复): 旧的去重只靠内存字典 _MANUAL_MIRRORED_TICKETS,
    桥进程一旦重启(看门狗重拉 / 主号 account_id 切换)该 dict 被清空 → 仍持仓的旧单
    被当成「新开仓」重新 XADD order:executed → 钉钉重复通知(实证 ticket=240189520
    在 08-05 19:49 与 08-06 06:29 被发两次)。且发布字段缺 mt5_ticket, 通知端
    ExecutionNotifier 按 mt5_ticket 去重取到 0 而失效。

    修复: (1) 补齐 mt5_ticket 字段, 使通知端 (account_id, ticket) 去重生效;
    (2) 加 Redis 持久去重键 hcm:notified_open:{ticket}(30天 TTL, MT5 票据单调递增
    不重用), 跨桥重启/主号切换仍有效, 同一持仓只通知一次。
    """
    if redis_conn is None:
        return
    direction = "BUY" if pos.type == 0 else "SELL"
    ticket = int(pos.ticket)
    # 持久去重: 同一 MT5 票据(同一持仓)只发布一次开仓通知, 跨桥重启/主号切换生效
    try:
        _nk = f"hcm:notified_open:{ticket}"
        if redis_conn.exists(_nk):
            log.info("skip duplicate manual-open notification (persistent): ticket=%s", ticket)
            return
    except Exception:
        pass
    try:
        mapping = {
            "account_id": str(int(master_account_id)),
            "account_role": "master",
            "mt5_ticket": str(ticket),
            "signal_id": str(ticket),
            "order_id": str(ticket),
            "symbol": str(pos.symbol),
            "direction": direction,
            "entry_price": str(float(pos.price_open)),
            "filled_price": str(float(pos.price_open)),
            "lot": str(float(pos.volume)),
            "stop_loss": str(float(pos.sl)) if pos.sl else "0.0",
            "take_profit": str(float(pos.tp)) if pos.tp else "0.0",
            "status": "filled",
            "ts": str(int(time.time())),
        }
        redis_conn.xadd("order:executed", mapping, maxlen=10000)
        try:
            redis_conn.set(_nk, "1", ex=60 * 60 * 24 * 30)
        except Exception:
            pass
        log.info(
            "Manual open → order:executed published: symbol=%s ticket=%s direction=%s (account_id=%s)",
            pos.symbol, ticket, direction, master_account_id,
        )
    except Exception as e:
        log.warning("Manual open order:executed publish failed (non-fatal): %s", e)


def _publish_manual_master_close(redis_conn, ticket: int, symbol: str, master_account_id: int) -> None:
    """主号持仓已平 → 发 close 事件（signal_id=解析后的 signal_id，action=close）。

    2026-07-24 修复（按票精确平仓，根治「主号止损1单→跟单同品种全平」误平 BUG）：
    主号被平 ticket 经 hcm:signal_for_ticket 解析为跟单号开仓时记录的 signal_id
    （auto 单：主号 ticket→模型 signal_id；手动单：主号 ticket 本身）。
    桥端跟单号仅平 hcm:signal_for_ticket==该值的那一仓，绝不按 symbol 平全部。
    """
    if redis_conn is None:
        return
    # 解析主号 ticket → 跟单号开仓时记录的 signal_id（两账户体系 ticket 不同，
    # 但开仓时两端都用同一 signal_id 写入 hcm:signal_for_ticket）。缺映射则回退主号 ticket。
    resolved = int(ticket)
    try:
        _m = redis_conn.get(f"hcm:signal_for_ticket:{ticket}")
        if _m:
            resolved = int(_m.decode() if isinstance(_m, bytes) else _m)
    except Exception as _e:
        log.warning("master_close resolve failed ticket=%s: %s", ticket, _e)
    payload = {
        "direction": "CLOSE",
        "action": "close",
        "lot": 0.0,
        "price": 0.0,
        "sl": 0.0,
        "tp": 0.0,
        "signal_id": resolved,
        "account_id": int(master_account_id),
        "ts": int(time.time()),
        "close_mode": "ticket",       # 按票精确平仓，低延时复刻主号动作
        "close_ticket": resolved,
    }
    _xadd_manual_event(redis_conn, symbol, payload)

    # [2026-07-24 直连快速通道] 绕过 signal_tower→risk→risk_passed 长链，
    # 直接写 key 供跟单桥主循环 2s 级消费。长链因 ack 失败/XPENDING 堆积(5787)/信号塔重启
    # (xgroup_setid→"$" 抛历史消息) 会丢事件，直连通道永不丢失（TTL 600s 冗余保证交付）。
    # 格式：hcm:direct_close:{resolved_signal_id} = JSON {symbol,master_ticket,resolved,account_id,ts}
    try:
        _dc = json.dumps({
            "symbol": symbol,
            "master_ticket": int(ticket),
            "resolved": resolved,
            "account_id": int(master_account_id),
            "ts": int(time.time()),
        })
        redis_conn.set(f"hcm:direct_close:{resolved}", _dc, ex=600)
    except Exception as e:
        log.warning("Direct close key set failed (non-fatal, stream path still exists): %s", e)


def _xadd_manual_event(redis_conn, symbol: str, payload: dict) -> None:
    """把一条 master trade 事件 XADD 进 manual_mode:master_stream:{symbol} Stream。

    P1优化：原实现 LPUSH 进 LIST（需 1s 轮询消费）；现改 Stream，信号塔可 XREADGROUP
    阻塞消费（事件驱动，延迟 ≤ block_ms）。stream 按 maxlen 自动裁剪，无需 TTL/过期处理。
    payload 字段值统一 str 化（XADD 要求字符串值），信号塔端按字段解析回数值。
    """
    key = _MANUAL_STREAM_KEY.format(symbol=symbol)
    try:
        mapping = {k: (str(v) if v is not None else "") for k, v in payload.items()}
        redis_conn.xadd(key, mapping, maxlen=_MANUAL_STREAM_MAXLEN)
        log.info(
            "Manual master event published: action=%s symbol=%s ticket=%s (account_id=%s)",
            payload.get("action"), symbol, payload.get("signal_id"), payload.get("account_id"),
        )
    except Exception as e:
        log.error(
            "Manual master event publish failed (action=%s ticket=%s): %s",
            payload.get("action"), payload.get("signal_id"), e,
        )


def _publish_master_sltp_event(redis_conn, pos, new_sl: float, new_tp: float, master_account_id: int, action: str = "modify") -> None:
    """主号 auto 持仓 SL/TP 变更 → XADD hcm:master:sltp（毫秒级跟单消费专用流）。

    与 manual_mode:master_stream 解耦：auto 单不走 manual mirror（避免跟单桥无对应仓→无操作），
    改走本流由跟单桥 _follower_consume_sltp 直接精确按票镜像 SL/TP。
    字段统一 str 化（XADD 要求）；master_ticket 供跟单桥经 hcm:signal_for_ticket 解析 signal_id 定位跟单仓。
    """
    if redis_conn is None:
        return
    try:
        mapping = {
            "master_ticket": str(int(pos.ticket)),
            "symbol": str(pos.symbol),
            "sl": str(round(float(new_sl), 5)),
            "tp": str(round(float(new_tp), 5)),
            "account_id": str(int(master_account_id)),
            "action": str(action),
            "ts": str(int(time.time())),
        }
        redis_conn.xadd(_SLTP_STREAM_KEY, mapping, maxlen=_SLTP_STREAM_MAXLEN)
        log.info(
            "Master SL/TP event published (sltp stream): ticket=%s symbol=%s sl=%s tp=%s (account=%s)",
            pos.ticket, pos.symbol, new_sl, new_tp, master_account_id,
        )
    except Exception as e:
        log.error("Master SL/TP event publish failed ticket=%s: %s", pos.ticket, e)


def _publish_manual_master_modify(redis_conn, pos, new_sl: float, new_tp: float, master_account_id: int) -> None:
    """主号持仓 SL/TP 变更 → 发 modify 事件（signal_id=主号 ticket，action=modify）。

    2026-08-06 修复（精确按票改单，根治「主号改 1 仓 SL → 跟单同品种全仓被改而打损」）：
    close_ticket 经 hcm:signal_for_ticket 解析为主号开仓时记录的 signal_id
    （auto 单：主号 ticket→模型 signal_id；手动单：主号 ticket 本身，键缺失时回退），
    与 manual_mode CLOSE 路径完全一致；桥端仅改与该 signal_id 映射对应的那一仓，
    绝不按 symbol 改写跟单号全品种持仓。
    """
    if redis_conn is None:
        return
    # 解析主号 ticket → signal_id（与 _publish_manual_master_close 一致的映射解析：
    # auto 单映射到模型 signal_id，手动单回退主号 ticket 本身）
    resolved = int(pos.ticket)
    try:
        _m = redis_conn.get(f"hcm:signal_for_ticket:{pos.ticket}")
        if _m:
            resolved = int(_m.decode() if isinstance(_m, bytes) else _m)
    except Exception as _e:
        log.warning("Manual modify resolve failed ticket=%s: %s", pos.ticket, _e)
    payload = {
        "direction": "MODIFY",
        "action": "modify",
        "lot": float(pos.volume),
        "price": float(pos.price_open),
        "sl": new_sl,
        "tp": new_tp,
        "signal_id": resolved,
        "account_id": int(master_account_id),
        "ts": int(time.time()),
        "close_mode": "ticket",
        "close_ticket": resolved,
    }
    _xadd_manual_event(redis_conn, pos.symbol, payload)


def _publish_manual_master_partial_close(redis_conn, pos, closed_volume: float, master_account_id: int) -> None:
    """主号部分平仓（手数减少）→ 发 partial_close 事件（signal_id=解析后的 signal_id，action=partial_close）。

    2026-08-06 修复（精确按票减仓，根治「主号减 1 仓 → 跟单同品种全减」）：
    signal_id/close_ticket 经 hcm:signal_for_ticket 解析为主号开仓时记录的 signal_id
    （auto→模型 signal_id，手动→回退主号 ticket），与 manual_mode CLOSE/MODIFY 路径完全对称；
    桥端仅减与该 signal_id 映射对应的那一跟单仓，绝不按 symbol 广播减同品种全部持仓。
    """
    if redis_conn is None:
        return
    # 解析主号 ticket → signal_id（与 _publish_manual_master_close / _modify 一致的映射解析）
    resolved = int(pos.ticket)
    try:
        _m = redis_conn.get(f"hcm:signal_for_ticket:{pos.ticket}")
        if _m:
            resolved = int(_m.decode() if isinstance(_m, bytes) else _m)
    except Exception as _e:
        log.warning("Manual partial_close resolve failed ticket=%s: %s", pos.ticket, _e)
    payload = {
        "direction": "PARTIAL_CLOSE",
        "action": "partial_close",
        "lot": float(closed_volume),
        "price": 0.0,
        "sl": 0.0,
        "tp": 0.0,
        "signal_id": resolved,
        "account_id": int(master_account_id),
        "ts": int(time.time()),
        "close_mode": "ticket",
        "close_ticket": resolved,
    }
    _xadd_manual_event(redis_conn, pos.symbol, payload)


def _publish_manual_master_add(redis_conn, pos, added_volume: float, master_account_id: int) -> None:
    """主号加仓（手数增加）→ 发 add 事件（direction=BUY/SELL，action=add）。

    signal_id 用「主号 ticket*100000 + 本 ticket 加仓序号」保证唯一（同 ticket 多次加仓不塌缩去重）；
    桥端按 BUY/SELL 走正常开仓路径，在跟单号上开一笔等量新持仓。
    """
    if redis_conn is None:
        return
    seq = _MANUAL_ADD_SEQ.get(pos.ticket, 0) + 1
    _MANUAL_ADD_SEQ[pos.ticket] = seq
    add_id = int(pos.ticket) * 100000 + seq
    direction = "BUY" if pos.type == 0 else "SELL"
    payload = {
        "direction": direction,
        "action": "add",
        "lot": float(added_volume),
        "price": float(pos.price_open),
        "sl": float(pos.sl) if pos.sl else 0.0,
        "tp": float(pos.tp) if pos.tp else 0.0,
        "signal_id": add_id,
        "magic": int(pos.magic) if hasattr(pos, "magic") and pos.magic else 0,
        "account_id": int(master_account_id),
        "ts": int(time.time()),
    }
    _xadd_manual_event(redis_conn, pos.symbol, payload)


def _detect_and_publish_master_changes(redis_conn, pos, master_account_id: int) -> None:
    """检测主号持仓相对上轮快照的变化，发 modify / partial_close / add 镜像事件。

    彻底修正根因：原先只发 open/close，跟单号冻结在开仓初值。
    现在每次 SL/TP 变更、部分平仓、加仓都实时镜像给跟单号，实现"完全继承"。
    """
    if redis_conn is None:
        return
    t = pos.ticket
    is_auto = _is_auto_opened_ticket(redis_conn, t)
    # 首次见到该持仓：登记快照即返回（auto / 手动 都登记，供后续变更检测）。
    # 根治：auto 模型单（主号 _execute_signal 开的单）SL/TP 变更【不再丢】——
    # 原 early-return 导致 auto 单 SL/TP 变动完全不发事件，跟单号只能靠 10s 轮询同步。
    # 现改为：auto 单 SL/TP 变更发布到 hcm:master:sltp（毫秒级跟单消费）；
    # 手动单维持 manual_mode:master_stream 镜像（open/close/modify/partial/add 全继承）。
    snap = _MANUAL_LAST_SNAPSHOT.get(t)
    if snap is None:
        _MANUAL_LAST_SNAPSHOT[t] = {
            "sl": float(pos.sl) if pos.sl else 0.0,
            "tp": float(pos.tp) if pos.tp else 0.0,
            "volume": float(pos.volume),
        }
        return
    new_sl = float(pos.sl) if pos.sl else 0.0
    new_tp = float(pos.tp) if pos.tp else 0.0
    new_vol = float(pos.volume)
    if is_auto:
        # auto 模型单：SL/TP 变更发布到 hcm:master:sltp（毫秒级跟单消费）。
        # 手数增减（partial_close/add）同步发布到 master_stream，由跟单桥镜像执行——
        # 跟单桥的广播信号独立开仓路径【不会】在主号部分平仓/加仓时触发，
        # 若不显式发布，自动单的部分平仓/加仓永远不被跟单号继承（动作发散）。
        # 注：开仓事件（open）仍跳过——跟单号随广播信号独立开仓，避免与 master_stream 重复开仓。
        sl_changed = abs(new_sl - snap["sl"]) > 1e-9
        tp_changed = abs(new_tp - snap["tp"]) > 1e-9
        if sl_changed or tp_changed:
            _publish_master_sltp_event(redis_conn, pos, new_sl, new_tp, master_account_id)
        # 手数减少（partial_close）
        if new_vol < snap["volume"] - 1e-9:
            _publish_manual_master_partial_close(
                redis_conn, pos, round(snap["volume"] - new_vol, 2), master_account_id
            )
        # 手数增加（add）
        elif new_vol > snap["volume"] + 1e-9:
            _publish_manual_master_add(
                redis_conn, pos, round(new_vol - snap["volume"], 2), master_account_id
            )
        _MANUAL_LAST_SNAPSHOT[t] = {"sl": new_sl, "tp": new_tp, "volume": new_vol}
        return
    # 手动单：原有 manual_mode:master_stream 镜像逻辑（modify/partial_close/add 全继承）
    changed = False
    # SL/TP 变更（modify）
    if abs(new_sl - snap["sl"]) > 1e-9 or abs(new_tp - snap["tp"]) > 1e-9:
        _publish_manual_master_modify(redis_conn, pos, new_sl, new_tp, master_account_id)
        changed = True
    # 手数减少（partial_close）
    if new_vol < snap["volume"] - 1e-9:
        _publish_manual_master_partial_close(
            redis_conn, pos, round(snap["volume"] - new_vol, 2), master_account_id
        )
        changed = True
    # 手数增加（add）——独立于 SL/TP 判定，避免同周期改动被 elif 漏掉
    if new_vol > snap["volume"] + 1e-9:
        _publish_manual_master_add(
            redis_conn, pos, round(new_vol - snap["volume"], 2), master_account_id
        )
        changed = True
    if changed:
        _MANUAL_LAST_SNAPSHOT[t] = {"sl": new_sl, "tp": new_tp, "volume": new_vol}


async def _resolve_account_id(pool) -> Optional[int]:
    """解析 active master 的 account_id（与信号塔 _resolve_account_id 逻辑一致）。

    根治：每 60s 周期重解析（而非永久缓存），使在 UI 切换/停用主号后，
    跟单平仓传播（manual mirror 的 _manual_publish 判定）即时跟随主号变化。
    查询失败时沿用上次缓存值，避免 DB 抖动导致平仓事件中断。

    持仓落库必须使用真实交易账户，严禁硬编码种子账户。
    """
    global _RESOLVED_ACCOUNT_ID, _RESOLVED_AT
    _now = time.time()
    if _RESOLVED_ACCOUNT_ID is not None and (_now - _RESOLVED_AT) < _RESOLVE_TTL:
        return _RESOLVED_ACCOUNT_ID
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT account_id FROM hcm_broker.accounts
                   WHERE is_active = true AND account_type = 'master'
                   ORDER BY account_id LIMIT 1"""
            )
            if row is not None:
                _RESOLVED_ACCOUNT_ID = int(row["account_id"])
                _RESOLVED_AT = _now
                return _RESOLVED_ACCOUNT_ID
            # 查到 0 行（无 active master）：清空缓存
            _RESOLVED_ACCOUNT_ID = None
    except Exception as e:
        log.warning(f"_resolve_account_id failed: {e}")
        # 查询失败但已有缓存值时沿用，避免抖动
        if _RESOLVED_ACCOUNT_ID is not None:
            return _RESOLVED_ACCOUNT_ID
    # [2026-07-24 禁止硬编码账户号] 账户时常切换，严禁回退到写死的种子账户(6)。
    # 无 active master / 查询失败时返回 None：sync_positions 中所有使用 account_id 的路径
    # 均被 _manual_publish/(account_id_mode==account_id) 条件守护，None → 不发布 close、
    # 不写快照，安全降级；PG 写入用 self_account(=account_id_mode, 动态) 不受影响。
    # 下轮 _RESOLVE_TTL 到期自动重查，DB 恢复后即解析出真实 active master。
    return _RESOLVED_ACCOUNT_ID


async def _pg_update_position(pool, pos, current_price, float_profit, trail_state, account_id: int,
                              mt5=None) -> None:
    """更新或插入持仓行到 PostgreSQL。

    先执行 UPDATE，若 rowcount==0（持仓未在 PG 中）则 INSERT 兜底。
    写入字段: sl, tp, current_price, float_profit, trail_state, snapshot_time, updated_at

    Args:
        pool: asyncpg connection pool.
        pos: MT5 position object.
        current_price: 当前 bid/ask 价格，已 round(x, 5)。
        float_profit: 浮动盈亏，已 round(x, 2)。
        trail_state: trail_state JSON dict。
        account_id: 真实交易账户 ID（active master，生产=6）。
    """
    trail_state_json = json.dumps(trail_state) if trail_state else None
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE hcm_trading.positions SET
                sl = $1,
                tp = $2,
                current_price = $3,
                float_profit = $4,
                trail_state = $5,
                snapshot_time = $6,
                updated_at = $6,
                status = 'open'
            WHERE mt5_ticket = $7
            RETURNING position_id
            """,
            round(pos.sl, 5) if pos.sl else 0.0,
            round(pos.tp, 5) if pos.tp else 0.0,
            round(current_price, 5),
            float_profit,
            trail_state_json,
            now,
            pos.ticket,
        )

        if row is None:
            # UPSERT 兜底：持仓尚未写入 PG（例如在 bridge 之外手动开仓）
            # 时区校正：pos.time 是经纪商服务器时间（实测 +3h），必须减偏移才是真 UTC。
            # detect_mt5_tz_offset 带合理性校验+缓存+回退；mt5 为 None 时走默认 3h。
            open_time = (
                mt5_time_to_utc(pos.time, detect_mt5_tz_offset(mt5, pos.symbol))
                if pos.time else now
            )
            if open_time is None:
                open_time = now
            elif open_time > now:
                # 防御：开仓时间不可能在未来。校正后仍 > now 说明时间戳异常，clamp 到 now。
                open_time = now
            await conn.execute(
                """
                INSERT INTO hcm_trading.positions
                    (account_id, symbol, direction, open_price, current_price, lot,
                     sl, tp, float_profit, trail_state, open_time, snapshot_time, updated_at,
                     mt5_ticket, status, signal_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $12, $13, 'open', $14)
                """,
                account_id,  # 真实交易账户（active master，生产=6），不再硬编码
                pos.symbol,
                "BUY" if pos.type == 0 else "SELL",
                round(pos.price_open, 5),
                round(current_price, 5),
                pos.volume,
                round(pos.sl, 5) if pos.sl else 0.0,
                round(pos.tp, 5) if pos.tp else 0.0,
                float_profit,
                trail_state_json,
                open_time,
                now,
                pos.ticket,
                getattr(pos, "signal_id", None),
            )
            log.info(f"Pos sync #{pos.ticket}: UPSERT (was not in PG)")


def _redis_ticket_signal(redis_conn, ticket) -> Optional[int]:
    """P3 归因修复：从 ticket→signal_id 映射找回 signal_id（开仓时由 bridge 写入）。

    MT5 持仓对象无 signal_id 属性，UPSERT 路径无法获取，故在开仓时建立 Redis 映射，
    平仓对账时按 ticket 找回。返回 int 或 None（含 bytes / 异常兜底）。
    """
    if redis_conn is None or not ticket:
        return None
    try:
        raw = redis_conn.get(f"hcm:signal_for_ticket:{ticket}")
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return int(raw)
    except Exception:
        return None


def _record_after_close_cooldown(redis_conn, symbol: str, direction: str = "") -> None:
    """持仓平仓后写『信号冷却』时间戳键，抑制**同方向** N 秒内的新开仓信号。

    设计（2026-08-11）：
    - 键:   hcm:after_close_cooldown:{symbol}:{DIRECTION}（symbol=逻辑名，DIRECTION=BUY/SELL）
    - 值:   冷却截止 epoch（秒，浮点）
    - TTL:  冷却时长 + 10s 缓冲，便于自动清理
    - 配置: close.after_close_cooldown_sec（秒）；0 或缺失=关闭（不写键）
    - 作用域: 按 (品种, 方向)；主号/跟单号共享同一 Redis 键
              （任一账户平掉该品种该方向即触发双方冷却，避免刚平完立即被回调信号拉回场）

    【2026-09-09 变更·仅同方向冷却】原按品种「全方向」冷却：震荡期连续止损平仓会
    反复续期 120s，把该品种所有方向的新信号一并吞掉（实测 07:08~07:17 连续 4 条
    SELL 被拦，漏斗「过风控未成交」9/11 条源于此）。现仅冷却被平掉的那一方向，
    反向信号正常放行（反转/对冲场景不再被误伤）。direction 为空时回退旧的全品种键。
    """
    if redis_conn is None or not symbol:
        return
    try:
        raw = redis_conn.hget("hcm:config:v2", "close.after_close_cooldown_sec")
        sec = float(raw) if raw not in (None, "") else 0.0
    except (ValueError, TypeError):
        sec = 0.0
    if sec <= 0:
        return
    try:
        expiry = time.time() + sec
        _d = str(direction or "").strip().upper()
        _key = (f"hcm:after_close_cooldown:{symbol}:{_d}" if _d in ("BUY", "SELL")
                else f"hcm:after_close_cooldown:{symbol}")
        redis_conn.set(_key, f"{expiry:.3f}", ex=int(sec) + 10)
        log.info("after_close_cooldown set for %s%s: %.0fs (until epoch %.0f)",
                 symbol, (":" + _d) if _d in ("BUY", "SELL") else "", sec, expiry)
    except Exception as e:
        log.warning("after_close_cooldown set failed for %s: %s", symbol, e)


# ── 2026-09-01：平仓归因（替代硬编码 'sync_reconcile'）──
# 原实现把 orders.close_reason 恒写 'sync_reconcile'，无法区分 SL / TP / 保本(BE) /
# 手动 / 强平 → 扫损、提前止盈等归因完全不可做（hcm-web ai_report.py 已标注此缺陷）。
# MT5 deal.reason 取值：0=CLIENT 1=MOBILE 2=WEB 3=EXPERT 4=SL 5=TP 6=SO(stop out)
_CLOSE_REASON_BY_DEAL = {
    0: "manual",     # DEAL_REASON_CLIENT（客户端手动平仓）
    1: "manual",     # DEAL_REASON_MOBILE
    2: "manual",     # DEAL_REASON_WEB
    3: "expert",     # DEAL_REASON_EXPERT（EA/程序主动平仓：移动止损、强制平仓等）
    4: "sl",         # DEAL_REASON_SL
    5: "tp",         # DEAL_REASON_TP
    6: "stop_out",   # DEAL_REASON_SO（保证金不足强平）
}


def _infer_close_reason(mt5, ticket: int, open_price, logger=None) -> tuple:
    """从 MT5 成交历史推断真实平仓原因 + 成交价 + 已实现盈亏。

    BE 判定：reason=SL 且成交价与开仓价之差在容差内 → 移动止损已推至保本。
    容差取 max(0.05, |开仓价| * 1e-4)，对 XAUUSD(约 4500) 约 0.45 美元。

    Returns:
        (reason_str, deal_close_price, realized_profit)；无法判定时返回
        ('sync_reconcile', 0.0, None)。realized_profit 取离场成交的 deal.profit
        （已实现 PnL，不含 swap/commission），查不到 → None（调用方回退最后同步
        float_profit）。任何异常都被吞掉并回退原值，绝不影响对账主流程。
    """
    if mt5 is None:
        return "sync_reconcile", 0.0, None
    try:
        deals = mt5.history_deals_get(position=int(ticket))
        if not deals:
            return "sync_reconcile", 0.0, None
        def _fld(obj, name, default=None):
            """MT5 官方返回 namedtuple，但部分封装/代理返回 dict，两种形态都要能读。"""
            v = getattr(obj, name, None)
            if v is None and isinstance(obj, dict):
                v = obj.get(name)
            return default if v is None else v

        out = None
        for d in deals:
            if _fld(d, "entry") == 1:  # DEAL_ENTRY_OUT = 离场
                out = d
                break
        if out is None:
            return "sync_reconcile", 0.0, None
        reason_id = _fld(out, "reason")
        price = float(_fld(out, "price", 0.0) or 0.0)
        profit = _fld(out, "profit", None)
        profit = float(profit) if profit is not None else None
        reason = _CLOSE_REASON_BY_DEAL.get(reason_id)
        if reason is None:
            return "sync_reconcile", price, profit
        if reason == "sl" and open_price:
            tol = max(0.05, abs(float(open_price)) * 1e-4)
            if abs(price - float(open_price)) <= tol:
                reason = "be"
        return reason, price, profit
    except Exception as e:
        if logger is not None:
            logger.warning("close reason infer failed #%s: %s", ticket, e)
        return "sync_reconcile", 0.0, None


async def _close_stale_positions(pool, account_id: int, live_tickets: set[int], redis_conn, mt5=None) -> None:
    """对账：将 PG 中记着 open、但 MT5 已不存在的持仓标记 closed，并落库订单。

    根因修复：原 sync_positions 只遍历 mt5.positions_get() 返回的活持仓，
    对已平仓（MT5 不再返回）的持仓从不处理，导致 PG 永远残留 open →
    风险引擎按 hcm_trading.positions 统计持仓数虚高 → 限仓误拦 → 不下单。

    P2-1 新增：平仓时把**已实现 PnL 写入 hcm_trading.orders**（修复历史 PnL
    黑洞）。realized profit ≈ 最后同步的 float_profit（5s 粒度，SL/TP 平仓时
    与成交价几乎一致）；close_price ≈ 最后同步的 current_price。
    """
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT position_id, mt5_ticket, signal_id, symbol, direction,
                      open_price, current_price, lot, sl, tp, float_profit, open_time
               FROM hcm_trading.positions
               WHERE account_id=$1 AND status='open'
                 AND mt5_ticket IS NOT NULL AND mt5_ticket > 0""",
            account_id,
        )
        closed = 0
        # [2026-07-24 审计修复] 清理历史 mt5_ticket=null 的 open 孤儿。
        # 这些单（如 7-20 爆炸式开单产物）因无 ticket 无法被 MT5 对账，
        # 原 SELECT 过滤了 `mt5_ticket IS NOT NULL` 而永远不被清理 → PG 永久残留 open
        # → 经本次最大单数修复后会被 _get_open_positions_count 计入 → 反阻塞新单。
        # 安全阈值：仅清理 open_time 早于 1 天前的历史孤儿，给新近 null 单时间让活同步处理。
        try:
            orphan_rows = await conn.fetch(
                """SELECT position_id FROM hcm_trading.positions
                   WHERE account_id=$1 AND status='open'
                     AND (mt5_ticket IS NULL OR mt5_ticket <= 0)
                     AND open_time < now() - interval '1 day'""",
                account_id,
            )
            for r in orphan_rows:
                await conn.execute(
                    """UPDATE hcm_trading.positions
                       SET status='closed', updated_at=$1
                       WHERE position_id=$2""",
                    now, r["position_id"],
                )
                closed += 1
                log.info(f"Pos reconcile (null-orphan) #{r['position_id']}: PG-only open w/o ticket → closed (historical ghost)")
        except Exception as e:
            log.error(f"Null-orphan cleanup failed: {e}")
        for r in rows:
            tid = r["mt5_ticket"]
            if tid not in live_tickets:
                # ── P2-1: 平仓回填已实现订单（根因修复 2026-09-05）──
                # 开仓时 _execute_signal 已 INSERT 一条 order_status=1 的 open 行；
                # 对账发现 MT5 已平 → **UPDATE 该行** 回填 close_price/profit/close_time/
                # close_reason（原实现因"同 ticket 已存在"去重而永不落库 → PnL 黑洞，
                # orders.close_time 全停在 8/28 之前）。无 open 行（老单/无映射）→ 兜底 INSERT。
                try:
                    _sid = r["signal_id"]
                    if not _sid and redis_conn is not None:
                        _sid = _redis_ticket_signal(redis_conn, tid)
                    # 2026-09-05：回查 MT5 成交历史取真实 deal.reason/成交价/已实现盈亏
                    _close_reason, _deal_px, _deal_profit = _infer_close_reason(
                        mt5, tid, r["open_price"], log)
                    _close_px = _deal_px if _deal_px > 0 else float(r["current_price"] or 0)
                    _profit = (_deal_profit if _deal_profit is not None
                               else float(r["float_profit"] or 0))
                    _oid = await conn.fetchval(
                        """UPDATE hcm_trading.orders
                           SET close_price=$1, profit=$2, order_status=2,
                               close_time=$3, close_reason=$4, updated_at=$3
                           WHERE mt5_ticket=$5 AND close_time IS NULL
                           RETURNING order_id""",
                        _close_px, _profit, now, _close_reason, tid,
                    )
                    if _oid:
                        log.info(
                            f"P2-1 orders close-backfill #{tid}: profit={_profit} "
                            f"close={_close_px} reason={_close_reason} (updated open row)")
                    else:
                        # ──【2026-09-09 幂等修复·根因】──────────────────────────
                        # 兜底 INSERT 原为【无条件写入】：只要持仓仍被判为 open，
                        # 每轮对账(约 1 次/秒)就灌一行，且 UPDATE 因 close_time 非空
                        # 永远匹配不到 → 恒走本分支。
                        # 实测事故：2026-09-08 23:05 ~ 09-09 01:48，仅 2 个真实
                        # mt5_ticket 生成 17738 行重复订单(单均 8869 条)，
                        # 盈亏/胜率/ai_report 全被放大约 8869 倍。
                        # 修复：该 ticket 已存在【任何】订单行 → 跳过，不再重复插入。
                        # （配合 orders(account_id, mt5_ticket) 唯一索引双保险。）
                        _dup = await conn.fetchval(
                            """SELECT 1 FROM hcm_trading.orders
                               WHERE mt5_ticket=$1 LIMIT 1""", tid)
                        if _dup:
                            log.info(
                                f"P2-1 orders skip #{tid}: row already exists "
                                f"→ idempotent guard, no duplicate insert")
                        else:
                            await conn.execute(
                                """INSERT INTO hcm_trading.orders
                                   (signal_id, account_id, mt5_ticket, symbol, direction,
                                    open_price, close_price, lot, sl, tp, profit,
                                    order_status, open_time, close_time, close_reason)
                                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,2,$12,$13,$14)""",
                                _sid, account_id, tid, r["symbol"], r["direction"],
                                r["open_price"], _close_px, r["lot"],
                                r["sl"], r["tp"], _profit,
                                r["open_time"], now, _close_reason,
                            )
                            log.info(
                                f"P2-1 orders write #{tid}: INSERT closed row "
                                f"profit={_profit} close={_close_px} reason={_close_reason}")
                except Exception as e:
                    log.error(f"P2-1 orders write failed for #{tid}: {e}")

                await conn.execute(
                    """UPDATE hcm_trading.positions
                       SET status='closed', updated_at=$1
                       WHERE position_id=$2""",
                    now, r["position_id"],
                )
                closed += 1
                # ── 信号冷却（2026-08-11）：持仓刚平仓 → 抑制该品种 N 秒新开仓 ──
                _record_after_close_cooldown(
                    redis_conn, r["symbol"], str(r.get("direction") or "").upper())
                log.info(f"Pos reconcile #{tid}: MT5 closed → PG status=closed (orders written)")
        if closed:
            log.info(f"Stale reconciliation: closed {closed} ghost position(s) for account {account_id}")


# ═══════════════════════════════════════════════════════════════
#  Redis 操作
# ═══════════════════════════════════════════════════════════════


def _redis_update_position(redis_conn, pos, current_price, float_profit, trail_tier, trail_state) -> None:
    """写入持仓状态到 Redis Hash，TTL=3600s。

    Key:   hcm:position:{mt5_ticket}
    Type:  Hash
    TTL:   3600s（自动清理已平仓 key；活跃持仓每次 sync 刷新 TTL）

    Fields: symbol, direction, entry, sl, tp, profit, lot, trail_tier, current_price, updated_at

    Args:
        redis_conn: Redis connection (sync client, decode_responses=True)。
        pos: MT5 position object。
        current_price: 当前价格。
        float_profit: 浮动盈亏。
        trail_tier: 分级追踪字符串 ("none"|"tier1"|"tier2"|"tier3")。
        trail_state: trail_state dict（用于提取 last_sl_move_time）。
    """
    key = f"hcm:position:{pos.ticket}"
    direction = "BUY" if pos.type == 0 else "SELL"

    # 提取 last_sl_move_time
    last_move_time = ""
    if trail_state:
        last_move_time = trail_state.get("last_sl_move_time", "")

    mapping = {
        "symbol": pos.symbol,
        "direction": direction,
        "entry": str(round(pos.price_open, 5)),
        "sl": str(round(pos.sl, 5)) if pos.sl else "0.0",
        "tp": str(round(pos.tp, 5)) if pos.tp else "0.0",
        "profit": str(float_profit),
        "lot": str(pos.volume),
        "trail_tier": trail_tier,
        "current_price": str(round(current_price, 5)),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    redis_conn.hset(key, mapping=mapping)
    redis_conn.expire(key, 3600)


def _publish_master_positions_snapshot(redis_conn, account_id: int, positions) -> None:
    """写入主号当前全部持仓集合到 Redis，供跟单桥周期对账兜底。

    根治「主号手动平仓 → 跟单号未跟上」：手动平仓本是一次性事件流，
    跟单桥重启/抖动会丢弃未消费事件且无持仓自愈 → 残留孤儿单。
    本快照让跟单桥能对比主号实时持仓，主动清掉孤儿单。

    Key:   hcm:master:positions:{account_id}
    Type:  Hash (field=ticket, value=json
           {symbol,direction,volume,sl,tp,open_time,open_price,updated_at})
    TTL:   30s（主号桥每 ~1s 刷新；失效=主号桥离线，跟单桥对账跳过以防误平）

    updated_at：快照写入 UTC 时间戳，供跟单桥判断「快照新鲜度」——超龄快照视为
    主号桥离线/抖动，对账与 SL/TP 跟随均跳过（不误平、不盲补、不盲改）。
    open_time/open_price 供跟单桥「补缺失」做时效+滑点闸门：
    行情已走远的历史漏单不得补开（补=错价入场必止损）。
    """
    key = f"hcm:master:positions:{account_id}"
    _snap_ts = int(time.time())
    mapping = {}
    for pos in (positions or []):
        direction = "BUY" if pos.type == 0 else "SELL"
        mapping[str(pos.ticket)] = json.dumps({
            "symbol": pos.symbol,
            "direction": direction,
            "volume": float(pos.volume),
            "sl": float(pos.sl) if pos.sl else 0.0,
            "tp": float(pos.tp) if pos.tp else 0.0,
            "open_time": int(getattr(pos, "time", 0) or 0),
            "open_price": float(getattr(pos, "price_open", 0.0) or 0.0),
            "updated_at": _snap_ts,
        })
    pipe = redis_conn.pipeline()
    pipe.delete(key)
    if mapping:
        pipe.hset(key, mapping=mapping)
    else:
        # 主号当前无持仓：保留键（哨兵字段）以区分「主号确实空仓(应清孤儿单)」
        # 与「主号桥离线/快照不可用(对账须防误平)」。跟单桥据此决定是否平孤儿。
        pipe.hset(key, "synced", "1")
    pipe.expire(key, 30)
    pipe.execute()


# ═══════════════════════════════════════════════════════════════
#  trail 分级判定
# ═══════════════════════════════════════════════════════════════


def _compute_trail_tier(pos, atr: float, redis_conn) -> str:
    """根据当前 SL 与 entry/各级阈值的关系判定 trail tier。

    阈值 key 与 mt5_bridge._get_close_config() 保持一致：
      - close.lock_amount_atr_mult (default 1.0) — Tier2 锁利幅度

    判定逻辑（以 BUY 为例）：
      - SL == 0                      → "none"（无 SL）
      - SL <= entry                  → "tier1"（保本已触发，SL 在 entry）
      - entry < SL <= entry+lock_amt → "tier2"（锁利已触发）
      - SL > entry+lock_amt          → "tier3"（正在跟进）

    Args:
        pos: MT5 position object。
        atr: 当前 ATR 值（0.0 时使用保守默认）。
        redis_conn: Redis connection。

    Returns:
        "none" | "tier1" | "tier2" | "tier3"
    """
    if pos.sl == 0 or pos.sl is None:
        return "none"

    entry = pos.price_open
    current_sl = pos.sl

    # 读取锁利幅度阈值（与 _get_close_config 同 key + default）
    # 使用安全 fallback: atr==0 时 lock_amount=0，保守全部归为 tier1
    if atr <= 0:
        lock_amount = 0.0
    else:
        lock_amount = atr * _read_close_config(redis_conn, "close.lock_amount_atr_mult", 1.0)

    if pos.type == 0:  # BUY
        if current_sl <= entry:
            return "tier1"
        elif current_sl <= entry + lock_amount:
            return "tier2"
        else:
            return "tier3"
    elif pos.type == 1:  # SELL
        if current_sl >= entry:
            return "tier1"
        elif current_sl >= entry - lock_amount:
            return "tier2"
        else:
            return "tier3"

    return "none"


# ═══════════════════════════════════════════════════════════════
#  trail_state 构建
# ═══════════════════════════════════════════════════════════════


def _build_trail_state(pos, current_tier: str, existing_trail_state: dict | None) -> dict:
    """构建 trail_state JSONB 结构。

    若已有 PG 中的 trail_state，则追加新的 SL 变更记录到 sl_history，
    保留最近 20 条（FIFO）。若已有记录与当前 SL 相同则跳过追加。

    sl_history 每条记录包含:
      - time: ISO8601 时间字符串
      - sl: 当前 SL 值
      - tier: 当前分级
      - reason: "initial"|"breakeven"|"lock_profit"|"trail"

    Args:
        pos: MT5 position object。
        current_tier: 当前 trail tier 字符串。
        existing_trail_state: PG 中已有的 trail_state dict（可为 None）。

    Returns:
        trail_state dict。
    """
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    current_sl = round(pos.sl, 5) if pos.sl else 0.0

    # 从已有 trail_state 继承 sl_history
    sl_history = []
    if existing_trail_state and isinstance(existing_trail_state, dict):
        sl_history = existing_trail_state.get("sl_history", [])
        if not isinstance(sl_history, list):
            sl_history = []

    # 仅当 SL 与上一条记录不同时才追加
    should_append = True
    if sl_history:
        last_entry = sl_history[-1]
        if isinstance(last_entry, dict) and last_entry.get("sl") == current_sl:
            should_append = False

    if should_append:
        sl_entry = {
            "time": now_str,
            "sl": current_sl,
            "tier": current_tier,
            "reason": _tier_to_reason(current_tier),
        }
        sl_history.append(sl_entry)
        # FIFO: 保留最近 20 条
        if len(sl_history) > 20:
            sl_history = sl_history[-20:]

    return {
        "current_tier": current_tier,
        "last_sl_move_time": now_str,
        "sl_history": sl_history,
    }


def _tier_to_reason(tier: str) -> str:
    """tier 字符串 → sl_history reason 字段映射。"""
    mapping = {
        "none": "initial",
        "tier1": "breakeven",
        "tier2": "lock_profit",
        "tier3": "trail",
    }
    return mapping.get(tier, "initial")


# ═══════════════════════════════════════════════════════════════
#  内部辅助
# ═══════════════════════════════════════════════════════════════


def _read_close_config(redis_conn, key: str, default: float) -> float:
    """读取 Redis hcm:config:v2 中的 float 配置值。

    与 mt5_bridge._get_close_config() 使用相同的 key namespace 和 fallback 默认值。
    不带 WARNING（避免 position_sync 重复告警——WARNING 统一在 mt5_bridge 侧输出）。

    Args:
        redis_conn: Redis connection。
        key: 配置 key（如 "close.lock_amount_atr_mult"）。
        default: 缺省默认值。

    Returns:
        float 配置值。
    """
    val = redis_conn.hget("hcm:config:v2", key)
    if val is None or val == "":
        return float(default)
    try:
        return float(val)
    except (ValueError, TypeError):
        return float(default)


def _get_atr_from_redis(redis_conn, symbol: str) -> float:
    """从 Redis 读取 ATR 缓存值。

    Args:
        redis_conn: Redis connection。
        symbol: 交易品种。

    Returns:
        ATR 值，不可用时返回 0.0。
    """
    try:
        atr = redis_conn.get(f"hcm:atr:{symbol}")
        if atr:
            return float(atr)
        return 0.0
    except Exception:
        return 0.0
