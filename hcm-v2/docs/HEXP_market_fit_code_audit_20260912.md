# HEXP 能否贴合行情下单 —— 读码深度分析（只读）

日期：2026-09-12 ｜ 范围：`hcm-signal-tower/signal_tower/*`、`tools/mt5_bridge.py` ｜ 全程只读，未改任何生产

## 0. 结论
**能，但是"分级贴合"：方向与入场时机是强贴合，止损止盈中等，仓位大小已退化。**
- 强：**方向裁决**（7 因子加权 + regime 自适应权重 + 多周期状态机 + 迟滞 + 一堆逆势闸门）、**入场时机**（结构位等待 / 滑点闸门 / ATR 尖刺丢单）。
- 中：**止损止盈**（SL 由 ATR 倍数驱动，确实随波动率伸缩；但夹取链内部互撞，TP 是固定 RR 或结构锚点）。
- 弱：**仓位大小**——`vol_scale` / `extreme_lot_mult` 已于 2026-08-28 移除，手数只随「评级 × 市场 composite × AI 档位」变，**不随 ATR 波动率缩放**。
- 一个结构性事实：方向裁决天生**滞后**（基于 M5 收盘 + 迟滞 + MTF），代码自己在 `hexp_engine.py:212-215` 承认会导致"高位追多 / 低位追空"，目前靠一串**事后闸门**兜底，而非提前贴合。

## 1. 方向裁决（贴合度：强，但滞后）
| 机制 | 位置 | 作用 |
|---|---|---|
| 7 因子加权 dir_sum | `hexp_engine.py:956 _factors` | adx/er/ma/bbw/hurst/rsi/mm → 方向分 |
| regime 自适应权重 | `:786 _select_factor_weights` | 按体制切换因子权重 |
| 位置因子顶权 | `:198-202`（weight 0.42） | 顶部压 SELL / 底部托 BUY，直接掐高位追单 |
| 方向迟滞死区 | `:184-190` | 死区 0.06 / 强翻 0.20，防 direction 闪烁 |
| 多周期状态机 | `:1087 _track_reversal` | M5/M30/H1/H4/D1 period_states 共振 |
| 迟滞状态机 | `:455 _HysteresisState` | 进入 60 / 退出 40，动量翻转覆盖 |
| 趋势优先 | `:1565+`（`trend_priority_mode`） | 确认趋势态时逆势单收口 NO_TRADE |
| 震荡均值回归 | `:2476+`（`range_hurst_*`） | NEUTRAL/RANGE + hurst<0.5 拦高位追单 |
| 动量翻转否决 | `:273+`（`momentum_flip_*`） | 微动量反向即拦，含趋势单保护 |
| 极值反转护栏 | `:250-261`（`extreme.reversal_*`） | 极值 + 动量反向 + 长影线 → 拦 |
| 动量枯竭 | `:262-270`（`momentum_drain_*`） | 高位 + er 低 + mm 枯竭 → 拦 |
| AI 强制翻向 | `:1537-1563`（`dir_lm_flip_enabled`） | 唯一"用 AI 翻 hexp 方向"路径，生产**关闭** |
- 实时快照（`hcm:live:hexp:XAUUSD`）：`direction=NO_TRADE, grade=RED, dir_sum=-0.0432, hp_score=20.93, period_states 全 RANGE, trend_phase.phase=squeeze, passed=false`。引擎当前判定"震荡挤压、方向无优势"→ 空仓不追，这本身就是贴合的表现。

## 2. 入场时机（贴合度：强）
- **入场闸门**（`scheduler.py:2141-2180`，开关 `hexp.entry_gate_enabled`）：micro_state 判 `TREND_EXHAUST`（衰竭末端）→ 否决顺势追单；H1 同向时豁免误杀。θ 分状态门槛见 `micro_state.py:75-87`（`hexp.entry.theta.*`）。
- **入场等待 `entry_trigger_wait`**（`scheduler.py:3160-3223`）：
  - 强趋势（adx≥28）或价格已离结构位 → **市价**（wait=0）；
  - 价格正犹豫在结构位带内（band=max(5, 0.7ATR)）→ **等 120s** 触达才成交；
  - 盲点兜底单 → 必须等 M5 结构位，给不出结构位则**放弃本轮，绝不市价追**。
- **桥端 zone-trigger**（`mt5_bridge.py:4165-4212`）：不在带内 → 写延迟键（TTL=wait 秒），每 2s 复查，价格触达才成交；**过期自动作废，无死单、不追单**。
- **ATR 尖刺过滤**（`mt5_bridge.py:4184-4192`）：价格偏离入场价 >3ATR → 丢弃该信号，不追尖刺。
- **滑点闸门（核心）**（`mt5_bridge.py:1335-1352`）：`|exec_price − entry_price| > ATR × bridge.max_entry_slippage_atr_mult` → `REJECT ENTRY_MISSED`。生产该键 = **0.5**（ATR≈2.6 → 约 1.3 pt），即**行情已漂离入场点就拒绝出手**。

## 3. 止损 / 止盈（贴合度：中）
- SL 由 ATR 倍数驱动，**确实随波动率伸缩**：`hexp_engine.py:2644-2652`（`sl_atr_mult` 生产 2.0；极值区收 `reversal_sl_atr_mult=0.5`）→ `scheduler.py:3442-3468` 显式换算成 `sl_price` → 桥优先采用（`mt5_bridge.py:1293`）。
- 桥端夹取链（`mt5_bridge.py:1376-1478`）：
  1. ai_risk 路径（`signal_tower.ai_risk_enabled=true`，已启用）AI SL 倍数；
  2. zone 锚点：SL 压到结构边界外侧 + back-offset 0.4ATR（`close.zone_sl_offset_atr_mult`）；
  3. 无则会话系数 `close.<session>.trailing_stop_distance`；
  4. **会话下限兜底**（`:1435-1466`，`sl_locked` 豁免）；
  5. **上限封顶** `close.max_sl_atr_mult`（生产 **2.5**）。
- TP（`:1480+`）：优先用**对向结构 zone** 作锚点（结构目标位），需落在 tp_min~tp_max ATR 且 R:R≥1，否则回退 `ai_tp_mult = sl_mult × rr_min(1.5)`。

## 4. 仓位大小（贴合度：弱）
- `hexp_engine.py:2617-2636`：`lot = grade_lot(S/A/B/C) × hexp.exec.lot_mult × red_mult`，`red_mult` 仅来自 transition/reversal 减仓。
- **`vol_scale` / `extreme_lot_mult` 已于 2026-08-28 移除**（`:182-183`、`:2631-2634` 明确注释"Atr 波动率不再缩放手数（vol_scale 恒 1.0）"）。
- 仅有的市场自适应：`_composite_atten = 0.5 + 0.5×composite`（`scheduler.py:2896`，外部市场分，实时 0.577 → 0.789），乘在 `suggested_lot_ratio`（`:3601`）；再加 `ai_lot_tier`（`:3605`）交风控选档，桥端 `suggested_lot_ratio` 生效（`mt5_bridge.py:1282-1292`）。
- → **同样的手数，在 ATR=1 与 ATR=10 的行情里单笔风险差 10 倍**。这是最明确的"不贴合"点。

## 5. 关键发现 / 隐患（读码所得）
1. **手数不随波动率**：见上。需要"风险平价"的话这是缺口。
2. **`ai.lm.entry_fuse = false` → 买点头 veto/boost 配置了但不生效**：`quality_gate.py:387` 以 `ai.lm.entry_fuse` 为总门；生产为 false，故 `ai.lm.entry_veto_prob.{asia,europe,us}=0.35`、`entry_boost_prob.europe=0.60` 全部**空转**。属本项目反复出现的"配置了≠生效"。
3. **SL 夹取互撞 + 注释过时**：`scheduler.py:3448` 硬编码 `min(ai_sl_mult, 1.8)` 并注释"与桥 max_sl_atr_mult 一致"，但生产 `close.max_sl_atr_mult = 2.5`。叠加会话下限（asia 2.5 / europe 2.0 / us 2.0）后：欧洲/美盘 SL≈2.0ATR、亚盘≈2.5ATR，**AI SL 缩放被会话下限吞掉**。结果是"随会话变"（算贴合盘口），但机制是夹取碰撞而非设计意图。
4. **方向裁决结构性滞后**：M5 收盘级 + 迟滞 + MTF 确认 → 反转时慢半拍；代码自认需 `dir_lm_flip` 补（当前关闭）。开之前必须先解决 `:1550-1553` 记录的"翻 1 棒即被弹回"抖动。
5. **`ai.mode = decoupled`（实时）**：与旧记录 coupled 不符，耦合手数档位路径本次未走（另需核实）。
6. **`reverse_order_enabled = true`**：会主动下逆势接刀单（与"贴合趋势"方向相反，但属设计内，受风控 `_check_reverse_order` 护栏）。

## 6. 实时运行态真值（Redis `hcm:config:v2` / `hcm:live:*`）
- 方向/评级：`min_grade=B`，`regime_min_grade=RANGE:B,NEUTRAL:B`（比旧记录 C 更严）；`trend_priority_mode=on`。
- 闸门全开：`extreme.reversal_enabled=true`、`momentum_flip_enabled=true`、`range_hurst_enabled=true`、`anti_cancel.enabled=true`、`zone.block_enabled=true`、`zone.penalty_enabled=true`。
- 执行：`sl_atr_mult=2`、`rr_min=1.5`、`lot_mult=1`。
- 入场：`zone_trigger_enabled=true`、`entry_gate_enabled=true`、`entry.min_rr=1.2`、`bridge.max_entry_slippage_atr_mult=0.5`。
- 风控/桥：`signal_tower.ai_risk_enabled=true`、`close.max_sl_atr_mult=2.5`、`zone_sl_offset_atr_mult=0.4`、会话 floor asia 2.5 / europe 2 / us 2。
- AI：`ai.enabled=true`、`ai.mode=decoupled`、**`ai.review.enabled=true` + `ai.review.mode=active`（非 shadow，真实拦单）**、`ai.lm.dir_veto_prob.europe=0.65`。
- 外部：`hcm:market:composite:score = 0.577` → 手数衰减 ×0.789。
- 实时信号：`NO_TRADE / RED / passed=false`，entry 分 71.02 但 state 分仅 20.93（total 43.77 < B 门槛）→ 买点尚可但状态质量不足，故不出手。

## 7. 建议（按优先级）
1. **手数风险平价**：恢复按波动率缩放（如 `lot × (ATR / ATR_median)`），否则高波动期单笔风险失控。
2. **配置卫生**：`ai.lm.entry_fuse` 要么打开让买点否决真正生效，要么删除相关键与 UI，避免"面板可调但不生效"。
3. **SL 夹取统一**：把 `scheduler.py:3448` 的硬编码 1.8 改为读 `close.max_sl_atr_mult`，并明确"会话下限 vs 上限"优先序，消除互撞。
4. **方向滞后**：属结构性问题；若要贴实时行情，需评估启用 `dir_lm_flip`（先解决抖动），或引入更短周期（M1）方向补强。
5. **核对 `ai.mode`**：确认为何是 decoupled，是否与耦合手数档位设计意图一致。

## 8. 复现命令（只读）
```bash
docker exec hcm-v2-redis-1 redis-cli hget hcm:config:v2 hexp.entry_gate_enabled
docker exec hcm-v2-redis-1 redis-cli hget hcm:config:v2 bridge.max_entry_slippage_atr_mult
docker exec hcm-v2-redis-1 redis-cli get hcm:market:composite:score
docker exec hcm-v2-redis-1 redis-cli get hcm:live:hexp:XAUUSD
```


---

## 附：2026-09-12 晚 变更记录（AI 评审 / 买点头融合）
### 更正
- 本报告原文把 `ai.review` 标注为 "shadow"，**系误读**（只看代码注释未读实时 mode）。实测 `ai.review.mode = active` —— 评审器已在生产真拦单。

### 侦察结论（关键陷阱）
- `ai.review` 的 shadow/live 开关是 **`ai.review.mode`**（`reviewer.py:34`：shadow 只记录 / canary 只 DOWNGRADE / active 含 VETO），`ai.review.enabled` 早已=true。
- `review_log` mode 分布：shadow 181 → **active 55**（09-11 切换）；active 段 VETO 9 / DOWNGRADE 9 / PASS 37，理由均 `LOW_SCORE`。
- `_ai_cfg_dict()` 白名单（`scheduler.py:684-714`）**只含全局键**，不含 `.europe/.asia/.us` → `scheduler.py:2482` 的会话覆盖**恒不命中（死代码）**。故 `ai.lm.entry_veto_prob.europe/.asia/.us=0.35` 三个会话键**改了不生效**；生效阈值取**全局** `ai.lm.entry_veto_prob`（原为空串 → `quality_gate` 回退 `CFG_FALLBACK=0.35`）。

### 已执行变更（经授权）
| 键 | 原值 | 新值 | 说明 |
|---|---|---|---|
| `ai.lm.entry_veto_prob` | *(空→回退0.35)* | **0.3** | 唯一生效键；未注册，`set()` 自动登记 |
| `ai.lm.entry_fuse` | false | **true** | 开启买点头否决/增强 |
- 通道：`PUT /api/v1/config/{key}` → `ConfigProviderV3.set()` 双写 PG 元数据 + Redis + PUB 失效。
- 回滚：`PUT /api/v1/config/ai.lm.entry_fuse` `{"value":"false"}`（阈值可按需改回 0.35 或清空）。
- 未改动：`ai.review.mode`（保持 active）、会话键（改了无用）、`ai.lm.entry_boost_prob`（空→回退 0.60 不变）。

### 影响预估（sidecar entry 头，`ai_pred_raw` head='entry'，167 条）
- 均值 0.4478｜`≤0.30` = **28.1%**（47/167）｜`≤0.35` = 30.5%｜`≥0.60` = 22.8%
- → 开启后预计新增拦掉约 **28%** 信号；与已 active 的评审器（拦 ~16%）叠加，合计可能少下 35~45% 单。

### 验证状态
- ✅ 配置层：Redis 回读 = `true` / `0.3`；PG `hcm_config.metadata.current_value` 已更新；配置中心 GET 一致。
- ✅ 引擎读路径：从 signal-tower 容器内部读取，引擎可见 `ai.lm.entry_fuse=true`、`ai.lm.entry_veto_prob='0.3'`；version key 已刷新；13:26:45 引擎消费失效通知并重载依赖模块。
- ⏸ **行为层未验证**：`ALERT kline feed STALE for XAUUSD/M5 ... Pausing signal production`（M5 最后 Bar 停在 12:55 UTC）。当日为**周六休市**，信号生产暂停，故 entry_fuse 的实际拦截动作需**开市后**观测（日志关键字 `veto_bad_entry` / `boost_good_entry`，或 `hcm_ai.gate_decision`）。
