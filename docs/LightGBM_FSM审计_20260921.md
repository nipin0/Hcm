# LightGBM FSM 运行状况审计

**审计时点**：数据库最新状态行 `2026-09-21 14:45 UTC`（沙箱内 PG/Redis 可读；注入上下文声称的 09-16 与库时间不符，以数据库时间为准）。
**范围**：只读侦察，未改动任何代码/配置/生产数据。
**一句话结论**：LightGBM FSM **当前已启用且真实交易**（非影子），推理层健康（模型 v5 加载成功、3 天内零 S9/零推理失败）；但其**4 类状态分类器本身判别力近随机**（代码自述），实际边际来自触发器+防抖；近 7 天 FSM 净 PnL **−19.39**，且存在多处与代码注释不符的配置/文档偏差，以及若干运营监控缺口。

---

## 1. 前提校正（先查错误前提 / 逻辑跳跃）

继承上下文摘要里两条关于本模块的描述**不准确**，以代码+运行态为准修正：

| 继承说法 | 事实（file:line / 运行态） | 判定 |
|---|---|---|
| "FSM 四态" | FSM 实际 **7 态**：`S0_IDLE/S1_OSC/S2_TREND_INIT/S3_TREND_MID/S4_TREND_FADE/S5_OSC_LOCKED/S9_PAUSED`（`state_machine.py:110-117`） | ❌ 前提错 |
| "4 类 LightGBM 状态模型 M5/M15/H1 各自独立"（`scheduler.py:489` 注释） | 实为 **4 分类**（oscillation/trend_init/trend_mid/trend_fade，`state_infer.py:80`），且**仅 M5 有模型、仅 M5 在用**（磁盘 `tools/models/` 全为 `*_M5_*`；`market_state_log.time_frame` 全部 = M5）。M15/H1 模型文件**不存在** | ⚠️ 注释误导（见风险 R2） |

"审计 LightGBM FSM 运行状况"这一请求本身成立——模块真实存在且在跑。

---

## 2. 架构事实（代码层，已读源码）

| 组件 | 位置 | 职责 |
|---|---|---|
| `StateInferer` | `state_infer.py` | 进程内 LightGBM 推理装配：4 分类(state) + 起点(onset) + 波动(vol)，每 tf 独立加载 `lgbm_{state,onset,vol}_{tf}_vN_s*.txt` |
| `MarketStateMachine` | `state_machine.py` | 7 态有限状态机 + 防抖（k_enter/exit/fade=3）+ 触发器驱动入口 + flat_reset/osc_lock |
| `StateStrategy` | `state_strategy.py` | 状态 → 交易意图（方向/手数梯/SL-TP 数值由桥按 session 算）；`state.order_enabled` 门控是否真下单 |
| 编排 | `scheduler.py:2305 _run_shadow_state` | 每根 bar：`state.enabled` 否 → 早退；否则 `_resolve_state_tf` → `infer` → `decide` → 落 `market_state_log` + 发布 `hcm:live:state` / `hcm:state:directive` |

**降级链**（`state_infer.py:10-14`）：lightgbm 缺失/模型缺失 → `ok=False reason=no_model`；连续 `infer_fail_bars`(=3) 根 → `S9_PAUSED`。

---

## 3. 运行态事实（配置 + 实时证据）

### 3.1 开关（Redis `hcm:config:v2`，运行进程已热加载——最新状态行 93s 前、model_version=v5 为证）
- `state.enabled = true` ✅ 已启用
- `state.order_enabled = true` ✅ **真实下单**（非影子）
- `state.model_dir = /app/review_models`（docker-compose.yml:172 挂载 `./tools/models:/app/review_models:ro`）
- `state.min_conf = 0.35`、`state.min_margin = 0.05`（低阈值）
- `state.trigger.required = true`、`use_donchian/use_rise = true`
- `state.fsm.flat_reset_enabled = true`、`state.osc_atr_loss_limit = 4.0`
- `state.osc_martingale_enabled = true`、`osc_lot_ladder = 0.5,1.0,1.5`、`trend_max_adds = 2`
- `state.vol.alpha = 0.10`（波动头 conformal 已激活）；`state.abstain.enabled` 未设 → 默认 False（弃权闸关，符合设计）

### 3.2 推理健康（PG `hcm_signal.market_state_log`，最近 1 天）
- `infer_ok=True` 占比 100%（145 decided + 51 未 decided）；**3 天内零 `S9_PAUSED`、零 `infer_fail`、零 `feed_stale` 告警** → 推理管线健康。
- 模型版本恒为 **v5**（M5 4 分类，自动选最高版；`_staging` 的 v6 不被 glob 递归扫描，故不误上线）。
- 状态分布：S0_IDLE 154 / S2_TREND_INIT 25 / S4_TREND_FADE 17；近 1 天**未出现 S1_OSC/S3/S5/S9**。
- 迁移 note 主因：`no_trigger` 74、`low_conf_skip` 49、`pending(S4:1/3,2/3)` 27、`transition(need=3)` 12、`flat_reset` 11、`trigger_no_dir` 10、`trigger_enter` 4。→ 系统保守，约 4 次趋势入口/天。

### 3.3 执行闭环（**orders JOIN signals**，近 7 天，确证"真在交易"）
| signal_mode | 订单数 | 手数合计 | 已实现 PnL | 已平/未平 |
|---|---|---|---|---|
| `state_osc` (magic 61) | 60 | 1.09 | **−15.15** | 60/0 |
| `state_trend` (magic 62) | 69 | 1.38 | **−4.24** | 69/0 |

- 信号→订单转化：state_osc 61 信号→60 单（98%）；state_trend 53 信号→69 单（**130%**，即加仓/金字塔生效，`trend_max_adds=2`）。
- 按日 PnL：09-15 **+76.30**(11)、09-16 **+88.08**(24)、09-17 −68.05(27)、09-18 **−91.46**(53)、09-19 **0 单**、09-20 +8.92(2)、09-21 −33.18(12，未完)。
- **7 日净 PnL = −19.39**（与两模式之和一致）。
- 当前 0 笔 FSM 在仓（全部已平）→ 这正是 `positions` 中 magic 61/62 = 0 的真因（非"从未交易"）。

---

## 4. 风险 / 偏差 / 被忽略变量（铁律主动提示）

**R1 · 代码注释与线上配置严重不符（运营盲点）**
`scheduler.py:494`、`:2647`、`:2127` 注释写"默认 `state.order_enabled=False` → 只算意图不下单 / 生产当前 False"。但 Redis 实测 `order_enabled=true`、`shadow_only=false`。任何人只读代码会误判为"影子系统、不动钱"，低估真实资金风险。→ 注释已过时，须修订或加运行时断言。

**R2 · "M5/M15/H1 各自独立"是空承诺（静默降级陷阱）**
`scheduler.py:489` 注释与 `_resolve_state_tf`（`scheduler.py:2001`，支持 `state.tf.{symbol}` 配 M15/H1）暗示多周期可用，但磁盘**无 M15/H1 模型**。若有人设 `state.tf.XAUUSD=M15` 想启用多周期 → `no_model` → 连续 3 根 `infer_fail` → **永久 S9_PAUSED**，且仅有日志无告警。→ 要么补模型，要么在配置校验处拒绝非法 tf。

**R3 · 4 类状态分类器判别力近随机（核心局限，代码自述）**
`state_infer.py:54-63` 明文：split-conformal 单例准确率仅 0.502/0.497/0.456，`margin` 与经济指标**无正相关** → "够格决策"样本不比随机好。因此 `decided` 的 4 类概率**不可当作可信方向/状态信号**；FSM 的实际边际来自 `trigger`（onset 模型，漏检 0.4%/提前 −1.0 根，见 `scheduler.py:2411`）+ 防抖 + flat_reset。**偏差提示**：不要因"状态概率"数值高就认为模型有 alpha。

**R4 · 净 PnL 近 7 日为负，且金字塔/马丁放大亏损**
−19.39，且 09-17/18/21 连续为负；09-18 单日 53 单、−91.46，与 `trend_max_adds=2`+`osc_martingale` 的加仓逻辑吻合——逆势时亏损被放大。薄 alpha + 加仓 = 回撤风险。需复盘 09-17/18 是否触发了马丁 escalation。

**R5 · 监控/可观测性缺口**
- `hcm_trading.positions` 为**仅在仓**表（无 `close_time`，`q_fsm_pos2.py` 验证），`orders` 表**无 magic 列**，`closed_positions` 为空表（据项目记忆）→ **无法仅凭 magic 直接拉 FSM 历史 PnL**，必须走 `orders JOIN signals.signal_mode` 这种间接关联。建议：① 在 `orders` 落 `magic`；② 或建专用 FSM PnL 视图。
- **模型缺失无 Redis 告警**：`state_infer` 对 `no_model` 仅 `logger.warning`；若 `/app/review_models` 挂载丢失或 `lightgbm` 依赖被移除，FSM 会在 3 根内转 S9，但无持久告警（此前 collector 修复只加了 `feed_stale` 告警，未覆盖模型缺失）。→ 补 `hcm:alerts:model_missing`。

**R6 · 09-19 全天零 FSM 订单 / 09-20 仅 2 单**
09-20 仅 2 单与已确认的 09-20 桥断流(00:07–08:16)吻合；但 **09-19 零单且 3 天内无 S9** → 既非暂停也非推理失败，大概率是当日触发器未 fired（市场/触发阈值）。属观测项，建议拉 09-19 `market_state_log` 的 `trigger_on/note` 分布确认非静默故障。

**R7 · 单品种单周期集中**
仅 XAUUSD/M5。概念漂移或该周期数据质量退化会一次性击中全部 FSM 活动，无分散。

---

## 5. 审计结论与后续建议（仅建议，未改系统）

1. **运行状况：健康（推理层）+ 真实交易（执行层）**，但**经济表现近 7 日微亏**、且 alpha 主要依赖触发器而非 4 类模型。
2. **必修文档/配置一致性**：修订 `scheduler.py` 中"生产 order_enabled=False/影子"的过时注释（R1）；在 `state.tf.{symbol}` 配置入口加"仅 M5 有模型"的校验/告警（R2）。
3. **补监控**：`orders` 落 `magic` 或建 FSM PnL 视图（R5）；加 `model_missing` Redis 告警（R5）。
4. **复盘 R4**：导出 09-17/18 FSM 成交明细，确认马丁/加仓 escalation 是否过度；必要时收紧 `osc_lot_ladder` 或 `trend_max_adds`。
5. **确认 R6**：拉 09-19 `note` 分布，排除静默故障。

> 说明：以上均基于 PG/Redis 只读查询与源码阅读。桥侧实际下单日志（mt5_bridge）与容器内 `lightgbm` 版本需宿主机 `docker` 访问复核，沙箱内不可达，未臆断。
