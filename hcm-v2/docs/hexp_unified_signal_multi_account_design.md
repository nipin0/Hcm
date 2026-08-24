# 和乘幂信号 · 去跟单化统一链路 设计规划

> 版本：v1（2026-08-24）
> 状态：已确认，按阶段实施
> 目标：去掉"复制主号成交单"的跟单思路，改为**全部账户同时接收同一信号、走同一链路、各自独立执行**，根治"跟单号跟不上主号"问题。
> 关联代码：`tools/mt5_bridge.py`、`hcm-signal-tower/signal_tower/scheduler.py`、`hcm-risk-engine/risk_engine/stream_consumer.py`、`hcm-copy-trading/`（STUB）。

---

## 一、现状与问题

### 1.1 现状架构（已核实）

```
signal_tower (scheduler._resolve_account_id)
  └─ 只解析 1 个 active master 账号 → 产 1 份信号 (account_id=主号)
      └─ SignalPublisher.publish → signal:stream (XADD 双写 PG)
          └─ risk-engine RiskStreamConsumer → 风控规则链评估
              └─ _publish_risk_passed → signal:risk_passed (广播到所有消费组)
                  ├─ 主号 bridge (group:{主号id}) → 独立下单 ✅
                  ├─ 跟单桥 bridge (group:{跟单号id}) → A-护栏校验主号已建仓 → 复制主号成交 ❌
                  ├─ dispatcher (STUB 假ticket) / copy-trading (STUB 假ticket)
```

**信号本身已是广播**（`signal:risk_passed` 推到所有 bridge 消费组，跟单号收到与主号完全相同的信号）。

### 1.2 "跟不上"根因（不是信号收不到，是复制成交拖慢）

跟单号收到信号后，`mt5_bridge.py` 的复制成交逻辑强行叠加两个串行依赖：

1. **A-护栏**（3568-3634）：跟单桥必须先确认主号**已为本信号建仓**（校验 `hcm:master:positions:{master}` 或 PG 二次确认），主号未建仓时 `pending` 丢弃。→ 跟单号下单时点 = 主号成交时点 + 校验/快照/传输延迟。
2. **master-fill 价格锚点**（1266-1293）：跟单号用主号 T0 成交价做滑点基准，该价格在链路延迟下已漂移 1-3 美元 → 滑点闸门可能 `ENTRY_MISSED` 拒绝，或强行走漂移后价格。

**结论**：跟单号不是没信号，而是被迫"等主号成交再行动"。

---

## 二、目标架构（去跟单化 · 平权同链路）

```
signal_tower (scheduler) —— 信号保持广播（account_id 保留主号，但 bridge 平权）
  └─ publish → signal:stream
      └─ risk-engine → 风控评估 → signal:risk_passed (广播)
          ├─ 账号A bridge → 独立下单 ✅
          ├─ 账号B bridge → 独立下单 ✅（不再复制主号、不再等主号）
          ├─ 账号C bridge → 独立下单 ✅
```

**核心原则**：一份信号 → 广播 → **每个 active 账号的 bridge 独立决策下单**（手数按各账号配置缩放）。所有账号行为一致（同信号、同价格口径、同时机），仅去掉"跟单号等主号成交"的串行依赖。

---

## 三、已确认的决策点

| 决策点 | 选择 | 说明 |
|---|---|---|
| **D1 信号广播** | 方案① | 信号保留主号 `account_id`，bridge 过滤改为"凡 active 账号都执行"，不再区分 master/follower。signal_tower 基本不改。 |
| **D2 风控维度** | 方案① | 各账号**共享主号同一份信号级风控结果**（方向/极值/AI分被拒则全部不开），但**账号维度持仓风控**（max_open_positions/max_daily_loss）按各账号自己持仓独立评估。 |
| **D3 手数策略** | 方案① | 每账号配置自己手数倍率（`account.lot_mult`），信号 `lot` × 各账号倍率。 |
| **D4 手动镜像** | 保留 | 手动平仓/改单仍按主号动作广播给各账号（平仓/风控必需，不属"复制开仓"）。 |

---

## 四、分阶段实施

### 阶段 1：去串行依赖（跟单号独立下单）

目标：跟单号不再等主号成交，直接独立下单。改动集中在 `mt5_bridge.py`。

- **移除 A-护栏**（3563-3634）：跟单桥收到信号后直接执行，不再校验主号建仓、不再 pending。
- **移除 master-fill 锚点**（1261-1293）：跟单号改用自身实时 tick + 信号 T0 entry_price 做滑点判断（与主号同口径）。
- **`reconcile_follower_positions`**（4045-4069）：改为仅"清孤儿"（不平多余、不补缺失），避免与独立开仓冲突。

### 阶段 2：风控维度调整 + 手数配置

- `hcm-risk-engine/stream_consumer.py`：持仓维度风控（max_open_positions/max_daily_loss）按**账号维度**评估（当前按 signal 单次评估）。
- 新增/复用每账号手数倍率配置。

### 阶段 3：全面去跟单化

- 清理 copy-trading STUB 路径（`hcm-copy-trading/`、gateway stub），统一走 mt5_bridge 独立执行链路。
- 删除 `hcm_copy.relationships` 的复制逻辑残留，确认无死代码。

---

## 五、风险与防护

| 风险 | 防护 |
|---|---|
| "主号不开、跟单号开" | D2 方案①：共享信号级风控，同一信号被拒则全部账号都不开。 |
| 滑点/行情漂移 | 各账号用自身实时 tick 做滑点闸门（保留 1266-1293 逻辑，仅改锚点）。 |
| 账号超额开仓 | 账号维度 max_open_positions / max_daily_loss 独立熔断（保留并改造 `_check_follower_circuit_break`）。 |
| 回归 | 阶段 1 灰度观察：跟单号不再复制主号后，下单时点应与主号接近、滑点拒绝率应下降。 |

---

## 六、回滚方案

- 阶段 1 改动集中在 `mt5_bridge.py`（bind mount，重启即生效）。回滚 = git revert 该文件 + 重启桥。
- 阶段 2 风控改动在 risk-engine，回滚 = revert + 重启 risk-engine。
- 全程双写 PG 订单，可审计、可对比主号 vs 跟单号下单差异。

---

## 七、验收标准

1. 跟单号下单时点与主号接近（无"等主号成交"延迟）。
2. 跟单号不再因 master-fill 价格漂移触发 `ENTRY_MISSED`。
3. 主号被风控拒单时，跟单号也不开（共享信号级风控）。
4. 各账号独立持仓风控生效（单账号 max_open_positions/max_daily_loss）。
5. 手动平仓/改单仍同步到所有账号。
