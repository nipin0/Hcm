# 双源信号（co_source）重构优化方案
## —— 实时行情精准买点算法 + 闸阀最小化

> 目标：解决"强趋势不出单、中立市出单止损"两大缺陷，把信号塔从"静态指标加权 + 层层闸阀"
> 重构为"实时微观状态感知 + 精准买点检测 + 单一权威门槛"。

---

## 〇、实施进度（2026-08-05）

**Phase 1（已上线 · signal-tower 已 restart，healthy）：**
- 强趋势 RSI 过热豁免：`scoring.overheat_suppress_in_trend`（默认 True）→ 强趋势(ADX≥强趋势反向阻断阈值 且 体制 TREND/PRE_TREND)中 RSI 极端不再被砍，恢复顺势单。直接修复"强趋势不出单"主因。
- 动量同向门控软折扣：`scoring.lag_momentum_conflict_block`（默认 True，改 False）→ 顺趋势回踩单由硬阻断改为 ×0.70 软折扣，恢复最佳回踩买点。
- NEUTRAL RSI 不再裸奔：`co.neutral_rsi_min_score`（默认 0.15）替代 threshold=0 透传；并跳过 F4 空头钝化冲突。该通道默认 `_neutral_rsi_enabled=False` 离线，代码缺陷已修复、启用后不再无门槛交易。
- 清理废弃键：后端删除死变量 `_trend_min_score` 及其加载；前端面板删除 `scoring.trend_min_score_threshold` 死键，新增上述两个开关。
- **即时回退方式**：把 `scoring.overheat_suppress_in_trend` 置 False / `scoring.lag_momentum_conflict_block` 置 True 即退回旧行为（经 config_provider.set 双写 PG+Redis，风险引擎 60s 热重载生效，无需重启）。

**Phase 0（已上线 2026-08-05）+ 架构级 Phase 1/2/3（已实现·灰度默认关·2026-08-05）：**

> 编号说明：上方"Phase 1"指**即时调参热修**（overheat/lag_momentum/neutral_rsi 三项，已上线）；下方"架构级 Phase 1/2/3"指**六、落地路径**的生产链路重构，二者不同。

- **Phase 0 micro_state 状态机（shadow 观测）已实现并上线（2026-08-05）**：新增 `signal_tower/micro_state.py`（Market Micro-State 5 态连续状态机，ATR 归一化）+ `signal_tower/precision_entry.py`（Layer2 三维精准买点分 `entry_quality = w1·方向对齐 + w2·微观结构确认 + w3·风险回报`），scheduler `_run_shadow_v2` 在 `_produce_signal` 中并行计算、仅日志 + 落 Redis（`hcm:live:micro_state:{symbol}` 最新态 + `hcm:shadow:v2:{symbol}` 最近 200 条对比样本），**绝不改变交易行为**。灰度开关 `co.v2_shadow_enabled`（默认 True，置 False 即停采）。首个实盘样本即捕获 critical 信号：`old_pass=true` 但新分判死（`verdict=NEW_BLOCK_OLD_PASS`，微观态 `TREND_ACCEL` 不追）——架构 Phase 1 启用前须先排查"新分误杀好单"风险。**〔stale-code 已修〕** 此前容器内 `precision_entry.py` 跑的是缺 `align_score/structure_score/rr_score` 的旧版（Windows Docker 绑定挂载同步竞态），导致 shadow 落样字段缺、review 显示 `?`；2026-08-05 重启后经 `grep align_score` 确认容器已加载磁盘现行算法，后续样本字段齐全。
- **架构级 Phase 1/2/3 已实现（灰度 `co.v2_enabled`，默认 False，字节级不变）：**
  - **代码落点（均 bind mount，重启即生效；Windows Docker 竞态可能需重启两次同步）**：
    - `scoring_engine.py`：`ScoringEngine.load_config` 读 `co.v2_enabled`；`compute_pre_score` 内 overheat / lagging_discount / lag_momentum_conflict 三处折扣加 `if not self._v2_enabled` 守卫（架构 Phase 1「删除三折扣」——v2 启用时由微观结构裁决替代）。
    - `co_source.py`：新增 `apply_v2(score_result, ind, regime, h1, micro_state, entry_quality, theta, …)` 收敛决策：
      - Phase 1：方向与分数由 `precision_entry.entry_quality` 产出，co_source 仅作门槛裁决；复用 H1 方向禁区（硬闸）+ 风险闸 F3/F5/F6。
      - Phase 2：F1/F2/F4 **降级为因子**（折扣不再作废方向）；`NEUTRAL RSI` 通道仅限 `REVERSAL/RANGE` 微观态且需结构确认才放行均值回归。
      - Phase 3：band 门槛统一为 θ(状态, 波动率)（`micro_state.adaptive_theta`）；`with_trend` 豁免并入方向对齐度（顺 H1 时门槛放宽到 `co.gate.with_trend.trend/100`）。
      - 放行时写 `co_exec_sl_atr_mult=2.0 / co_exec_rr_min=1.2`，下游 SL·TP·lot 无缝沿用。
    - `scheduler.py`：生产路径新增 `_is_v2_enabled` + `_compute_v2_inputs`；`_produce_signal` 中 **legacy 决策（`co_source.apply` v1）永远照常计算并作为 shadow 的 "old" 端**，`co.v2_enabled=True` 时再叠加 `apply_v2` 作为权威决策覆盖 `score_result`；`_run_shadow_v2` 增加 `legacy_passed/legacy_reason` 入参，v2 启用后仍可观测「新旧分歧」。
    - `web/signal_tower.py`：`co.v2_enabled` 加入 `THRESHOLD_CONFIG_DEFAULTS`（默认 False），面板可配置、API 可双写。
  - **启用流程（必须按序，禁止跳过 shadow 验证）**：① 累积 shadow 样本 **≥500 条且含 ≥50 条 `TREND_PULLBACK`**，确认 `shadow_review.py` 中 `NEW_BLOCK_OLD_PASS` 无"趋势回踩好单被判死"的红旗（`TREND_ACCEL` 被判死属正确）；② 经面板/API 设 `co.v2_enabled=true`（`config_provider` 双写 PG+Redis，信号塔 60s 热重载生效，**无需重启**）；③ 观察 `CoSource v2 decision` 日志与 shadow 分歧；④ 回退：置 `False` 即恢复旧链路。
  - **默认关闭验证（2026-08-05）**：`docker compose restart hcm-signal-tower hcm-web` 后容器 healthy、无 Traceback，`co.v2_enabled` 默认 False → 生产信号链路字节级不变；shadow 仍正常运行（样本字段已齐全）。
- 前端面板新增开关需 `node:20` 容器重新 `vite build` 后才能在浏览器生效（dist 经 bind mount 即时加载）；后端逻辑与配置键已即时生效。

---

## 一、现状：信号生产数据逻辑链路全图

```
M5 K线收盘
  └─ scheduler._symbol_loop
       └─ _produce_signal
            ├─ [闸0] 幂等守卫(同bar去重, Redis持久)
            ├─ [闸0b] K线数≥30
            ├─ 指标计算: RSI/MACD/MA/ADX/BOLL/Stoch/BBW/ATR/bar_momentum
            ├─ H1 多周期上下文 → H1RegimeClassifier → h1_bias(UP→BUY/DOWN→SELL)
            ├─ M5 体制分类 → RegimeClassifier
            │     优先级: PRE_TREND→TREND→TREND_FADE→RANGE→NEUTRAL(兜底)
            │
            ├─ ScoringEngine.compute_pre_score
            │     ├─ 按体制选权重表(TREND/RANGE/NEUTRAL...)连续混合
            │     ├─ 8指标×权重 → buy_score/sell_score
            │     ├─ [闸1] 方向裁定: buy vs sell 比大小, diff>0.01, max≥0.20
            │     ├─ [闸2] NEUTRAL RSI 均值回归: RSI<30挂起BUY/>70挂起SELL→2根确认
            │     ├─ [闸3] RANGE 均值回归: 真突破放弃 + 极值&贴边双条件
            │     ├─ 【折扣链】_disc = min(...):
            │     │     [折A] RSI overheat: BUY+RSI≥68→×0.3 / ≥65→×0.5
            │     │     [折B] 共识度 agree<3→×0.80
            │     │     [折C] lagging_discount: lag>0.40 & adx>28→×0.90
            │     │     [折D] 校准软折扣 ×0.85
            │     │     [折E] PlanB 强趋势逆势 ×0.30 / 弱逆势 ×0.40
            │     │     [折F] 逆H1 confirmed×0.30 / 未确认×0.55
            │     ├─ [闸4] lag_momentum_conflict: 滞后主导+动量反向→NO_TRADE
            │     └─ [闸5] 引擎基线门槛 _compute_threshold
            │
            ├─ CoSourceEngine.apply
            │     ├─ [豁免] neutral_rsi_confirmed → threshold=0 直接放行(裸奔!)
            │     ├─ F1 周期背离(扣分)
            │     ├─ [闸6] F2 布林收口假突破→NO_TRADE
            │     ├─ F3 数据窗口(扣分)
            │     ├─ [闸7] F4 超买超卖钝化→NO_TRADE
            │     ├─ F5 连亏熔断(扣分)
            │     ├─ [闸8] F6 棒质量/点差→NO_TRADE(默认关)
            │     ├─ 校准因子 ×factor
            │     └─ [闸9] _apply_adaptive_gate:
            │            band(strong0.40/weak0.50)+风险偏移+with_trend豁免(0.20)
            │
            ├─ [闸10] cooldown 冷却(同向时间窗)
            ├─ [闸11] zone 入场: entry_trigger_wait(等回踩结构位, 超时丢单)
            └─ 发布 → signal:stream → 风控 → 桥
                 └─ [闸12] 信号年龄闸门(max_signal_age_seconds)
```

---

## 二、两大缺陷的根因诊断

### 缺陷①：强趋势不出单 —— RSI overheat 折扣 × lag 硬阻断 × 高门槛三重绞杀

强趋势的物理特征是**价格持续单向 → RSI 长期卡极端区、ADX 高、MA 多头排列**。当前架构对此有三重叠加抑制：

| 抑制点 | 代码位置 | 强趋势下的行为 | 后果 |
|--------|----------|----------------|------|
| **RSI overheat** | scoring_engine.py:608-625 | 强涨 RSI≥68→×0.3，强跌 RSI≤32→×0.3 | pre_score 砍 70% |
| **lag_momentum_conflict** | scoring_engine.py:710-725 | 回踩时滞后组(ma+adx)主导+动量组反向→硬阻断 | 回踩买点全杀 |
| **强趋势高门槛** | co.gate.strong.trend=40 | 门槛 0.40 | 砍后难过闸 |

**量化**：强趋势确认单原始 pre_score≈0.65 → overheat×0.3 → **0.195 < 0.40 → NO_TRADE**。
即便顺 H1 豁免（with_trend=20→0.20），0.195 仍临界或不过。

**反直觉结论**：趋势越强 → RSI 越极端 → 砍得越狠 → 越不出单。
主升浪（RSI 极端）被 overheat 砍死，回踩段（最佳买点）被 lag_momentum 硬阻断——**强趋势整段信号真空**。

### 缺陷②：中立市出单止损 —— NEUTRAL RSI 通道裸奔 + 体制误标

**根因链**：
1. **NEUTRAL 是分类器的"兜底灰色地带"**（regime_classifier.py:236-239）：既不是明确趋势（ADX 不够高）、也不是明确震荡（RANGE 需 ADX<22 且 BBW≤1.0）时的 fallback。XAUUSD 常处**弱趋势/过渡态**被归入 NEUTRAL（NEUTRAL_WEIGHTS 注释 116-117 自认"XAUUSD 常处弱趋势被误标 NEUTRAL"）。
2. **NEUTRAL RSI 均值回归是"抄底摸顶"**（scoring_engine.py:461-529）：RSI<30 挂起 BUY、RSI>70 挂起 SELL，第二根确认即 `neutral_rsi_confirmed=True`。
3. **co_source 对该标记完全豁免**（co_source.py:282-289）：`threshold=0.0, threshold_passed=True, 直接 return`——**绕过全部闸门裸奔**。
4. 弱趋势下跌中 RSI<30 往往是**下跌中继而非反转** → 抄底 BUY → 趋势延续 → 止损。

**设计矛盾**：最不确定的 NEUTRAL（灰色地带）信号保护**最少**（threshold=0 全豁免），
而最确定的 RANGE（真震荡）均值回归保护**最多**（极值+贴边+真突破放弃）。完全倒置。

### 共同的架构病根

1. **评分是静态"指标加权求和"，不感知实时微观结构**：不知道当前是趋势加速/回踩/衰竭/震荡/反转，只用 8 个指标的瞬时值比大小。
2. **方向裁定是 buy_score vs sell_score 比大小**（scoring_engine.py:423-432）：相对强弱而非绝对买点。强趋势高分被折扣砍、中立市 RSI 极值直接给方向绕过评分——两套方向来源互相矛盾。
3. **闸阀过多且目标冲突**：20+ 个拦截/折扣点分散在 3 个模块，每个都想"防错"，叠加后"该出的出不来、不该出的放行了"。
4. **入场时机粗糙**：zone 入场是"等回踩到静态结构位"，强趋势中结构位太远、中立市无精准买点。

---

## 三、闸阀冗余度量化（20+ 拦截点，大量重复冲突）

| 类别 | 拦截点 | 重复/冲突 |
|------|--------|-----------|
| **RSI 相关 ×4** | ①scoring overheat ②NEUTRAL RSI ③RANGE RSI ④co_source F4 | 同一指标 4 套规则互相打架 |
| **门槛 ×3** | ①引擎 min_score ②引擎 _compute_threshold ③co_source gate | 曾引发双裁决混乱(记忆43453531) |
| **逆势拦截 ×3** | ①PlanB ②H1 防火墙 ③lag_momentum_conflict | 三重防守同一目标 |
| **趋势末端 ×2** | ①lagging_discount ②lag_momentum_conflict | 同根因两套实现 |
| **过滤器 ×6** | co_source F1-F6 | F4 与 scoring overheat 语义重复 |

**结论**：闸阀不是不够，而是**冗余且互相冲突**。真正的安全应来自"方向禁区 + 精准买点"，
而非"层层设卡"。

---

## 四、重构设计：实时行情精准买点算法

### 设计原则

1. **单一决策权威**：一个信号 = 一个买点质量分 × 一个动态门槛。消灭多模块互相覆盖。
2. **状态先行**：先识别实时微观状态，再针对性找买点——不同状态有不同"正确买点"。
3. **买点 = 方向 × 微观结构确认 × 风险回报位置**，三维合成，不再 buy/sell 比大小。
4. **闸阀最小化**：只保留 1 个硬安全（方向禁区）+ 1 个评分门槛，其余全部降级为评分因子。

### Layer 1 —— 实时微观状态机（Market Micro-State，替代粗粒度 regime）

在 M5 上识别 5 个微观状态（用 ATR 归一化，连续量化非硬切换）：

| 状态 | 识别条件（实时） | 正确买点策略 |
|------|------------------|--------------|
| **TREND_ACCEL** 趋势加速 | ADX↑ 且价格沿趋势连续同向 bar 且动量同向 | **不追 RSI 极端**，等回踩；挂"回踩预警" |
| **TREND_PULLBACK** 趋势回踩 ★ | 趋势方向明确 + 价格回踩到动态支撑/压力(EMA21/前结构位) + 回踩幅度∈[0.5,1.5]ATR | **核心买点**：回踩止跌确认后顺趋势入场 |
| **TREND_EXHAUST** 趋势衰竭 | ADX 高位回落 + 顶/底背离 + 动量衰减 | 不追，等反转确认 |
| **RANGE** 震荡 | ADX 低 + 布林带宽收窄 + 价格在区间内 | 边界均值回归（需贴边+极值确认） |
| **REVERSAL** 反转 | 结构突破 + 动量翻转 + H1 方向配合 | 新趋势起点，突破回踩入场 |

**关键创新**：把"趋势回踩（TREND_PULLBACK）"从被 `lag_momentum_conflict` 硬阻断的对象，
提升为**核心买点状态**——回踩时动量短暂反向正是买点特征，而非拦截理由。

### Layer 2 —— 精准买点检测（Precision Entry Score）

对每个状态计算买点质量分 `entry_quality ∈ [0,1]`，三维合成：

```
entry_quality = w1·方向对齐度 + w2·微观结构确认 + w3·风险回报位置
```

- **方向对齐度**：信号方向与 H1 bias / 微观状态方向的一致度（0~1 连续）。
- **微观结构确认**（核心，替代静态指标加权）：
  - 趋势回踩买点：回踩深度（ATR 归一化）∈ 黄金区间 + 止跌确认（下影线/小实体/动量柱回升）
    + 趋势结构未破坏（更高高点/更低低点序列 intact）。
  - 震荡反转买点：贴边程度 + 振荡器极值 + 反向 K 线确认。
  - 用**实时 tick**（hcm:config:v2 的 market:latest）确认止跌/突破，而非仅 M5 收盘价。
- **风险回报位置**：入场价到动态止损（结构位/ATR）与到目标（对向结构位）的实时 R:R，
  R:R<1.2 直接降分（把桥的 R:R guard 上移为信号层因子）。

### Layer 3 —— 单一权威门槛 + 方向禁区

- **方向禁区（唯一硬闸）**：只做 H1 bias 顺势方向（防逆势，这是唯一保留的硬安全）。
- **单一动态门槛**：`entry_quality >= θ(状态, 波动率)`。
  θ 按状态与实时 ATR 波动率自适应：趋势回踩 θ 低（鼓励）、震荡 θ 中、衰竭 θ 高（谨慎）。

---

## 五、双源信号（co_source）重构映射

### 保留 / 降级 / 删除

| 现有机制 | 处置 | 理由 |
|----------|------|------|
| H1 bias 方向禁区 | **保留**（唯一硬闸） | 防逆势是真安全 |
| RSI overheat 折扣(608-625) | **删除** | 强趋势误杀主凶；趋势加速态由状态机处理 |
| lag_momentum_conflict(710-725) | **删除** | 回踩买点被杀主凶；回踩态天然豁免 |
| lagging_discount(683-689) | **删除** | 由 TREND_EXHAUST 状态取代 |
| NEUTRAL RSI 全豁免(co_source:282-289) | **重构** | 移入 REVERSAL/RANGE 态，加结构确认+正常门槛，不再裸奔 |
| NEUTRAL 兜底 | **消除** | 微观状态机无"灰色兜底"，每个bar必归某态 |
| co_source F1(背离) | **降级为因子** | 融入 TREND_EXHAUST 衰竭分 |
| co_source F2(布林收口) | **降级为因子** | 融入 RANGE 态识别 |
| co_source F4(超买超卖钝化) | **删除** | 与 overheat 重复；由状态机处理 |
| co_source F3(数据窗口)/F5(连亏) | **保留**（独立风险闸门） | 与行情无关的真实风险 |
| co_source F6(质量) | **保留**（默认关，灰度） | 数据卫生 |
| band 门槛(strong/weak/shock) | **重构** | 统一为 θ(状态,波动率) |
| zone 静态入场 | **升级** | 改动态支撑/压力回踩检测 |

### 新 `co_source.apply` 流程（闸阀从 9 减到 3）

```
apply(score_result, micro_state, h1_context, ...):
    # [硬闸1·唯一方向禁区] 逆 H1 bias → NO_TRADE（结构性安全，不可豁免）
    if against_h1_bias: return NO_TRADE

    # [独立风险闸·与行情无关] F3 数据窗口 / F5 连亏熔断 / F6 质量
    if risk_block: return NO_TRADE

    # [单一权威门槛] entry_quality >= θ(micro_state, atr_vol)
    entry_quality = precision_entry_score(...)   # Layer 2 三维合成
    threshold = adaptive_theta(micro_state, atr)  # 状态+波动率自适应
    threshold_passed = entry_quality >= threshold
```

**拦截点从 20+ 收敛到：1 方向禁区 + 1 风险闸 + 1 买点门槛。**

### 缺陷修复映射

| 缺陷 | 现状 | 重构后 |
|------|------|--------|
| 强趋势不出单 | overheat×0.3 砍死 | TREND_ACCEL 识别→不追极端、等回踩；RSI 极端不再砍分 |
| 回踩买点被杀 | lag_momentum 硬阻断 | TREND_PULLBACK 是核心买点，动量反向=买点特征 |
| 中立市抄底止损 | neutral_rsi 豁免裸奔 | 反转/震荡态才许逆势，需结构确认+正常门槛，不豁免 |
| 闸阀冗余冲突 | 20+ 点互相打架 | 3 个清晰闸门，职责单一 |

---

## 六、落地路径与风险控制

### 分阶段（灰度，可回退）

- **Phase 0（并行观测，不改行为）**：新增 `micro_state.py` 状态机 + `precision_entry.py` 买点分，
  仅计算并打日志/落库（shadow mode），与现有评分并行，对比"现有拦截 vs 新买点"的差异样本。
- **Phase 1（替换评分源）**：compute_pre_score 的方向与分数改由买点分产出，
  删除 overheat/lag_momentum/lagging_discount；co_source 仅作门槛裁决。配置开关 `co.v2_enabled` 灰度。
- **Phase 2（闸阀收敛）**：F1/F2/F4 降级为因子，NEUTRAL RSI 通道移入 REVERSAL/RANGE 态并加结构确认。
- **Phase 3（门槛统一）**：band 门槛替换为 θ(状态,波动率)，with_trend 豁免并入方向对齐度。

### 风险与回退

- 每个 Phase 独立开关（PG+Redis 双写），异常即回退到 `co.v2_enabled=false`（旧链路完整保留）。
- shadow mode 期间用 hcm_signal.signals 对比新旧决策，确保新买点不劣化再放行。
- 回测用 optuna 对买点分权重（w1/w2/w3）与 θ 分状态寻优。

### 验证指标

- 强趋势时段（ADX>28 单向）信号产出数：从≈0 恢复到合理频率。
- 回踩买点胜率：TREND_PULLBACK 入场单 p_win 目标 >50%。
- NEUTRAL/灰色地带止损率：逆势单需结构确认后，止损率显著下降。
- 闸阀拦截分布：fallback_reason 从 20+ 种收敛到 3 类，可解释性提升。
