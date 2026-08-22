# 跟单号「每日盈亏熔断」方案设计

> 目标：跟单号当日累计盈亏（盈利或亏损）触及面板阈值时，**即时平掉跟单号全部持仓并停止当天的跟单交易**；到次日设定的恢复时间（默认 06:30）自动恢复跟单。
> 作用域：**仅跟单桥（IS_FOLLOWER）**，主号不受影响；与现有风控 `risk.max_daily_loss` 互补不冲突。

---

## 一、触发逻辑

```
主循环每 circuit_check_interval(默认15s) 计算跟单号「当日盈亏」：
    当日盈亏 = 当前浮动盈亏(MT5 open positions 实时 sum) + 今日已实现盈亏(MT5 history deals)
    若 当日盈亏 >= follow.daily_profit_limit（盈利上限）   → 盈利熔断
    或 当日盈亏 <= -follow.daily_loss_limit（亏损上限）    → 亏损熔断
    → 触发：平掉跟单号全部持仓 + 写熔断标志(带 expireat=下一个 reset_hour)
信号消费闸门：IS_FOLLOWER 且熔断标志存在 → 跳过所有开仓类信号（xack 不执行）
reconcile 补缺失：熔断中 → 不补开跟单仓（防恢复前的重复开仓）
恢复：熔断标志 Redis TTL 到点(下一个 reset_hour)自动过期 → 闸门自然放行 → 跟单恢复
```

---

## 二、盈亏口径（关键决策）

采用 **MT5 原生口径**，不依赖任何未写表（`hcm_trade.closed_positions` 当前无写入者，记忆 74262885 已记录，故不依赖它）：

| 项 | 来源 | 说明 |
|---|---|---|
| 浮动盈亏 | `mt5.positions_get()` 遍历 `p.profit` 求和 | 实时、USD，已含点差/杠杆 |
| 今日已实现 | `mt5.history_deals_get(今日00:00, now)` 累加 `entry==DEAL_ENTRY_OUT` 的 `profit` | 今日已平仓落袋盈亏 |

- **当日总盈亏 = 浮动 + 今日已实现**。
- 熔断时已全平跟单号 → 恢复时刻跟单号 0 仓、盈亏从 0 起算，无需手动重置计数器。
- `history_deals_get` 较重，**只在 circuit_check_interval(15s) 定时器调用**，不每轮跑。
- 不采用「仅浮动」或「仅已实现」：仅浮动会在盘中反复穿越阈值抖动；仅已实现会漏掉持仓浮亏爆仓风险。二者相加最稳健。

---

## 三、配置键（面板暴露，5 个）

命名空间 `follow.*`（独立，不混入 `risk.*` 以免与风控引擎混淆），加进 `close.py` 的 `CLOSE_CONFIG_DEFAULTS` 白名单（复用其 GET/PUT 双写 `config_provider.set` → PG+Redis 机制）。

| 键 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `follow.daily_circuit_enabled` | bool | `true` | 总开关。关=不熔断，纯跟单 |
| `follow.daily_profit_limit` | float(USD) | `500` | 当日盈利达到此值即熔断（如 500=赚 $500 停） |
| `follow.daily_loss_limit` | float(USD) | `300` | 当日亏损达到此值即熔断（如 300=亏 $300 停；配置写正数，代码取负比较） |
| `follow.circuit_reset_hour` | float(小时,支持小数) | `6.5` | 恢复时间，默认 6.5=06:30；键值可设 |
| `follow.circuit_check_interval` | int(秒) | `15` | 盈亏检查间隔，避免每轮重算 history |

> 双向限额：盈利上限 + 亏损上限任一触发即熔断（符合「盈利、亏损大于面板键值」语义）。

---

## 四、熔断标志与恢复（Redis 自管理，无需定时任务）

- **键**：`hcm:follow:circuit_break:{account_id}`
- **值**：`json{triggered_at, pnl, reason("profit"|"loss"), reset_at}`
- **过期**：`redis_conn.expireat(key, next_reset_epoch)` —— `next_reset_epoch` = 今日 `reset_hour` 若已过期则取**明日** `reset_hour` 的 epoch 秒。
  - 例：今天 14:00 触发、reset=6.5 → TTL 到明早 06:30 自动过期 → 跟单恢复。
  - 优雅点：信号消费闸门与 reconcile 闸门都只读此 key，过期即无 → 自动恢复，无需主循环每日扫描。
- **持久化**：存 Redis，桥重启（看门狗重拉）后读回 → 熔断状态连续，不会重启后误跟单。
- **钉钉/通知**（可选增强）：触发时经 `order:executed` 或现有 notifier 推一条「跟单熔断」告警（master-only 通知逻辑需放宽此特殊事件，或走独立通道）。

---

## 五、实现落点清单（`tools/mt5_bridge.py`，主机进程）

1. **新增 `_follower_daily_pnl(mt5, redis_conn, account_id) -> float`**
   - 浮动 = `positions_get()` 求和 `p.profit`；已实现 = `history_deals_get(今日0点, now)` 累加平仓 deal 的 `profit`。
   - DB/MT5 异常 → 返回 `0.0`（fail-open：异常时不熔断，避免误杀跟单）。

2. **新增 `_trigger_follower_circuit_break(mt5, redis_conn, account_id, pnl, reason)`**
   - 平跟单号全部持仓：遍历 `positions_get()` 每个 `_force_close_one(mt5, redis_conn, pos)`（复用现有精确按票平仓，带 20s 安全闸）。
   - 写熔断标志 + `expireat` 到 next reset_hour。
   - 日志 `Follower circuit BREAK (reason=profit/loss, pnl=%.2f) → closed all, resume at HH:MM`。

3. **主循环新增定时器 `last_circuit_check`（仿 `last_trail`）**
   - `if IS_FOLLOWER and circuit_enabled and now - last_circuit_check > interval:` 调 `_follower_daily_pnl` → 超阈值则 `_trigger_follower_circuit_break`。
   - 仅在 `IS_FOLLOWER and FOLLOW_MASTERS` 时生效（跟单号专属）。

4. **信号消费闸门（line 3149 附近，与 account.status / is_active 跳过同级）**
   ```python
   if IS_FOLLOWER:
       _cb = redis_conn.get(f"hcm:follow:circuit_break:{ACCOUNT_ID_MODE}")
       if _cb:
           for _dmid, _dmsg in entries_sorted:
               redis_conn.xack("signal:risk_passed", BRIDGE_GROUP, _dmid)
           log.info("Follower circuit-break active — skipping signal %s",
                    int(latest_data.get("signal_id", 0)))
           continue
   ```
   - 熔断中跳过**全部**信号（含 manual_mirror）：跟单号已全平，主号 CLOSE 镜像来时 no-op 安全；主号 OPEN/modify 来时不跟 → 正确。

5. **reconcile 补缺失开仓闸门（`reconcile_follower_positions` 调 `_open_follower_position` 前）**
   - 检查熔断标志，熔断中跳过补开（防恢复前跟单号被 reconcile 重新开仓）。
   - 注：恢复后 reconcile 只补主号「近 120s 内」新仓（`RECONCILE_BACKFILL_MAX_AGE_SEC` 闸门），不补历史老仓 → 跟单号从恢复时刻起干净跟新信号。

---

## 六、与现有机制协同（不破坏既有链路）

| 现有机制 | 协同方式 |
|---|---|
| 风控 `risk.max_daily_loss` | 互补：风控是主号+跟单都过、基于 closed_positions realized、REJECT 信号不强制平仓；本方案是跟单专属、浮动+已实现、触发即全平+停跟。二者可并存。 |
| `reconcile_follower_positions` | 熔断中禁用「补缺失开仓」，但保留「清多余/孤儿」（已平，无多余可清，安全）。 |
| `manual_mirror` 主号平仓镜像 | 熔断时跟单号已全平，CLOSE 镜像 no-op；OPEN/modify 镜像被信号闸门跳过。 |
| 跟单桥跳过独立 trailing（line 3412） | 不受影响：熔断是「开仓类」闸门，trailing 本就由主号镜像驱动。 |
| 单实例锁 / 看门狗 | 不受影响：熔断标志在 Redis，多桥/重启均读回。 |
| `bridge:alive:*` 心跳 | 不受影响：桥进程存活，仅停止跟单交易。 |

---

## 七、前端面板

- 在 `CloseConfig.tsx`（或新建跟单面板）加一组「跟单每日盈亏熔断」字段：
  - `follow.daily_circuit_enabled`（switch）
  - `follow.daily_profit_limit`（number, USD）
  - `follow.daily_loss_limit`（number, USD）
  - `follow.circuit_reset_hour`（number, 默认 6.5，悬停说明「恢复时间，6.5=06:30」）
  - `follow.circuit_check_interval`（number, 秒）
- 后端白名单：5 键加入 `close.py` 的 `CLOSE_CONFIG_DEFAULTS`（GET/PUT 经 `config_provider.set` 双写 PG+Redis）。
- （可选）面板显示当前跟单号当日盈亏：从桥心跳 `bridge:alive:{login}` 扩展 payload（加 `daily_pnl`）或新增轻量接口；非必须，先不做。

---

## 八、部署与验证

1. **后端键 seed**：`config_provider.set("follow.daily_circuit_enabled","true")` + 其余 4 键，双写 PG+Redis + PUBLISH hcm:config:invalidate。
2. **桥改动**：改 `tools/mt5_bridge.py` → `C:\Python313\python -m py_compile` 过 → 清 `tools/__pycache__` → kill 双桥 → 看门狗重拉（存活+锁键双齐）。
3. **前端**：node:20 容器内 `/tmp/fe` 构建（避 Windows 绑定挂载 ETXTBSY）→ `dist` 绑定挂载即时生效 → 浏览器 Ctrl+F5。
4. **验证**：
   - 临时把 `follow.daily_loss_limit` 设很小（如 5）→ 观察跟单号达到即全平 + 日志 `circuit BREAK` + 信号闸门跳过新信号。
   - 手动 `redis_conn.expireat` 提前到点 → 验证恢复跟单。
   - 桥重启 → 熔断标志读回 → 仍不跟单直到 reset 点。

---

## 九、边界与风险

- **fail-open**：MT5/DB 异常时 `_follower_daily_pnl` 返回 0 → 不熔断（宁可不熔断也不误杀跟单）。
- **抖动防护**：仅用阈值触发 + 一旦触发即全平锁死到 reset_hour，不在阈值附近反复熔断/恢复（避免今天平了明天又跟的乱序）。
- **主号不受影响**：熔断只作用于 IS_FOLLOWER 桥，主号照常交易、照常镜像（CLOSE no-op）。
- **恢复干净**：恢复时跟单号 0 仓，reconcile 不补历史老仓，从恢复时刻起跟新信号，当日盈亏自然归零。
- **多跟单号**：熔断标志按 `account_id` 隔离，每个跟单桥各自判断各自账号。
- **与现有总盈亏追利 `_update_total_trailing_stop` 区别**：那是主号桥的「峰值回撤全平」，本方案是跟单号「当日净盈亏硬熔断」，作用对象与触发口径完全不同，互不干扰。

---

## 十、待确认的关键点

1. **盈亏口径**：采用「浮动 + 今日已实现」（MT5 原生，推荐）—— 确认 OK 还是只要「已实现」？
2. **双向限额**：盈利上限 + 亏损上限都做（推荐，符合需求）—— 还是只做亏损上限？
3. **恢复时间键**：`follow.circuit_reset_hour=6.5`（06:30，可配）—— 确认默认 6.5？
4. 确认后我直接改 `mt5_bridge.py` + `close.py` 白名单 + 前端面板并实现、构建、部署验证。
