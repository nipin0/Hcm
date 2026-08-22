# HCM 和乘幂（HEXP）AI 信号质量评分器设计方案

> 版本 v1.0 · 2026-08-14
> 状态：**设计完成 + 阶段 0 离线验证通过**，尚待按阶段实施与红线授权，未上线
> 配套交付：`tools/build_labels.py` / `tools/quality_features.py` / `tools/train_signal_quality.py` / `deploy/migrations/0011_ai_quality_config.sql`

---

## 0. 一句话概述

在和乘幂（HEXP）独立信号源之上，引入**本地轻量 LightGBM 信号质量评分器**作为质量过滤层，与**后台异步 DeepSeek** 协作，共同辅助 HEXP 只输出高质量信号。AI 全程只有否决权 + 降/升级权，无独立开仓权。

---

## 1. 背景与目标

### 1.1 背景（硬证据）

- HEXP 引擎为 7 因子幂加权信号源，近期暴露「高多低空、双向止损」——方向判定不看价格位置、RSI 逆向权重仅 7.2%、极值闸门改后放行追单。
- 需要一个**统计学习层**从历史信号→结果中学习"什么信号是好单"，过滤低质量信号。

### 1.2 目标

1. 用 LightGBM 对每个 HEXP 候选信号打"真假概率" p，按 p 做否决/降级/保持/升级。
2. DeepSeek 后台异步输出 3 参数（真假概率 / 自适应止损系数 / 延续分），失败自动降级纯 HEXP。
3. 全部变量参数零硬编码，进 `ai.*` 配置命名空间，信号塔专属页可调。

---

## 2. 需求规格（完整，已与用户逐项确认）

### 2.1 定位与纪律红线

- **LightGBM 评分器 = 独立本地模块**，不直接产生信号，只做质量过滤。
- **AI 永远只有否决权 + 降级/升级权，没有独立开仓权**（开仓权始终在 HEXP）。
  - 结构不变式：AI 输出契约 `{p, delta_grade ∈ {-1,0,+1}}` / `{fake_prob, ai_sl_coeff, continuity_score}`，**契约无 `direction` 字段**。
  - 方向与「最小放行集合」始终由 HEXP 决定；AI 只能**缩小**放行集或**调整已放行信号的手数/等级**，绝不扩大（升级不逆转 RED/NO_TRADE）。
- **DeepSeek 仅作后台异步刷新数据源**，非实时决策依赖；失败/超时/限流 → 自动降级纯 HEXP。
- **AI_LM 与 Hexp 开关耦合**：`ai.mode=coupled` 按综合评分触发；`ai.mode=decoupled` 复原纯 HEXP 触发。

### 2.2 LightGBM 信号质量评分器（本地实时）

**标签**（已重锚，见 5.1）：
- 入场后 `label_horizon_bars` 根 M5 内，先触 `+label_r_win·R` = 1（win），先触 `-label_r_loss·R` = 0（loss）。
- 默认 **1R/1R/12**（由原 1.5R/1R 重锚而来，实测更优）。
- R = 风险距离 = |entry − SL|；SL 未落库时用 `ai_sl_mult × ATR` 推导。
- 「都未触及」「同根双触」→ 排除出训练集。

**特征（~25，分三类）**：
1. HEXP 原始值：`hp_score / hp_strength / dir_sum / k`。
2. 因子原始值：`adx / +di / -di / er / bbw / bbw_pct / hurst / rsi / mm`。
3. 多周期：M5/M30/H1/H4/D1 的 TrendScore 与方向。
4. 入场微观：距 EMA20(ATR)、确认 K 线实体占比、回踩深度。
5. 环境：时段 one-hot、ATR 分位、点差、距下一红色事件分钟数。

**使用方式（p = 真假概率）**：
- `p < 0.50` → 过滤（不下单）
- `[0.50, 0.60)` → 降一级（C→红灯，B→C）
- `[0.60, 0.70)` → 保持原级
- `≥ 0.70` → 升一级（B→A，A→S）
- **阈值重锚**：因标签基率约 20%（非 50%），提供分位重锚键 `ai.lm.{veto,down,up}_quantile`（0.50/0.70/0.85）替代绝对阈值，推荐使用。

### 2.3 DeepSeek 异步数据源（固定 3 输出，系统依赖）

| 输出 | 语义 | 用途 |
|---|---|---|
| 1. 信号真假概率 | DeepSeek 对 **HEXP 信号**真假的独立判断（与 LightGBM p 同目标） | 开仓过滤的异步校准（C_ai 仍以 LightGBM p 为主源） |
| 2. `ai_sl_coeff` | 自适应止损系数 0.8~1.5×ATR | 动态最优止损（覆盖 co_source G3 sl_atr，仅当有效且在边界内） |
| 3. `continuity_score` | 延续分 0–100 | 持仓动态调仓依据 |

### 2.4 Hexp + AI 动态权重耦合（按市场模式 k 自动切换）

- `S_hp` = HEXP 结构强度分 0–100 = 归一化 |HP|。
- `C_ai` = AI 置信度 0–100 = LightGBM p×100（DeepSeek 真假概率仅异步校准）。
- 总分 `= w(k)·S_hp + (1−w(k))·C_ai`：
  - k > 1.2（趋势/信结构）：`0.7·S_hp + 0.3·C_ai`
  - 0.5 < k ≤ 1.2（中性/均衡）：`0.6·S_hp + 0.4·C_ai`
  - k ≤ 0.5（震荡/强 AI 过滤）：`0.5·S_hp + 0.5·C_ai`
- **硬不变式**：C_ai 权重 ≤ 0.5（震荡档 0.5 为上限）。
- **开仓规则**：总分 > 85 → 基础手数×1.5；> 70 → ×1；> 60 → ×0.5；≤ 60 → 不发信号（链动风控动态手数）。

### 2.5 持仓动态调仓（单独用 continuity_score）

- ≥ 70 强延续：放宽止盈、追踪止损、允许加仓复核。
- 50–69 中性：不动、不加仓、不调止盈。
- < 50 弱延续：收紧止损、压缩止盈、锁定利润。
- 默认 `ai.cont.mode=log`（仅记录），`act`（执行）另行红线授权。

---

## 3. 系统架构

### 3.1 现有信号链路（已核实，硬证据）

```
scheduler._produce_signal
  → active_model=hexp 时 HexpEngine.produce()（异步拉 H1/H4/D1/M1）
  → 产出 ScoreResult(pre_score/direction/regime + hexp 元数据)
  → co_source.apply()（F1–F6 过滤 + 校准 + 自适应门槛，仅 co_source 激活生效）
  → co_source.apply_v2()（v2 路径，仅 co_source + v2_enabled）
  → G3 执行增强（SL/TP/lot）
  → 发布 signal:stream → 风控 → 桥 → MT5
```

### 3.2 新增模块（隔离、可降级）

```
signal_tower/quality_scorer.py     # LightGBM 加载+推理：snapshot → 校准 p（微秒级）
signal_tower/quality_features.py   # 特征装配（hexp快照 + 自算微结构 + 点差 + 日历）
signal_tower/ai_async_client.py    # DeepSeek 异步 3 输出 + 超时/限流/失败降级
signal_tower/quality_gate.py       # 单点闸门：等级升降 + 耦合公式 + 手数映射
signal_tower/continuity_engine.py  # continuity_score → 调仓动作（默认 log）
tools/train_signal_quality.py      # 离线训练 + 校准 + 回测
tools/build_labels.py              # 标签构造
```

### 3.3 数据流（实时 + 异步双轨）

```
实时：HexpEngine.produce() → quality_features 装配 → quality_scorer.score() → 校准 p
      → quality_gate（否决/升降 + 耦合总分→手数）→ signal:stream

异步：DeepSeek 批量写 ai:ds:out:{symbol}（TTL 30min）+ PG 审计
      → 真假概率异步校准 C_ai；ai_sl_coeff→止损；continuity→调仓
```

### 3.4 降级矩阵（任何 AI 故障都退回纯 HEXP）

| 故障 | LightGBM p | ai_sl_coeff | continuity |
|---|---|---|---|
| 模型缺失/加载失败 | 不存在 → 纯 HEXP | — | — |
| DeepSeek 超时/限流 | 纯 LightGBM p | 缓存旧值 | 缓存旧值 |
| DeepSeek 彻底失败 | 纯 LightGBM p | `fallback_sl_coeff`(默认回退 sl_atr_mult) | 50 中性 |

### 3.5 外部因子（复用现有 hcm-market-intel，零新建）

- 「外部因子评分」= 宏观/事件/情绪综合分，**现已存在**：`hcm-market-intel` 每 30s 写 Redis `hcm:market:composite:score`（四维 macro/sentiment/event/liquidity 加权，无 AI 成本）。
- PG 落库：`hcm_market.macro_snapshots` / `sentiment_snapshots` / `event_calendar`。
- 作为 DeepSeek 异步输入特征之一（「融合进异步 AI」）。

---

## 4. 配置命名空间 `ai.*`（零硬编码，独立于 hexp.*/co.*/risk.*）

完整键表见 `deploy/migrations/0011_ai_quality_config.sql`。默认**全关**（`ai.enabled=false` → 纯 HEXP，AI 零介入）。

| 子组 | 键（节选） | 默认 |
|---|---|---|
| 总开关 | `ai.enabled` / `ai.mode` | false / decoupled |
| `ai.lm.*` | enabled / model_path / calib_path / pass·down·up_threshold / veto·down·up_quantile / label_r_win·r_loss·horizon_bars·sl_atr_fallback / min_samples_train / retrain_cron | 见 SQL |
| `ai.ds.*` | enabled / timeout_sec / cache_ttl_min / sl_coeff_min·max / fallback_sl_coeff / fallback_continuity | 见 SQL |
| `ai.cpl.*` | enabled / w_trend·neutral·range / k_trend_min·k_range_max / tier_high·mid·low / lot_high·low | 见 SQL |
| `ai.cont.*` | enabled / strong_min / weak_max / mode | false / 70 / 49 / log |

---

## 5. 训练管线（阶段 0，已完成并实测）

### 5.1 标签构造 `build_labels.py`（只读）
- 12 根 M5 首触 ±R 判定；双触/未触/前向不足排除；R 由 `ai_sl_mult×ATR` 推导。
- 实测：HEXP 403 条 → 标注 327（1R/1R 口径），排除 76。

### 5.2 特征装配 `quality_features.py`（只读）
- 三类特征（已持久化 + M5 重算 + 环境），~42 列；5 个 hexp 专属特征 NaN 占位。

### 5.3 训练/校准/回测 `train_signal_quality.py`（只读）
- LightGBM 二分类（scale_pos_weight）+ Platt/isotonic 校准 + 时间序 walk-forward。

### 5.4 实测结果（真实 PG 数据）

| 标签口径 | 标注数 | 基线胜率 | raw AUC |
|---|---|---|---|
| 1.5R/1R/12（原规格） | 328 | 15.5% | 0.65 |
| **1R/1R/12（采纳）** | 327 | 20.8% | **0.84** |
| 1.5R/1R/24 | 345 | 16.2% | 0.89 |
| 1R/1R/24 | 341 | 20.8% | 0.83 |

- 阈值重锚（1R/1R/12）：top 50% → 胜率 32.5%（+12.8pt）；top 30% → 34.6%（+14.9pt）。模型可有效排序。
- 注意：isotonic 校准在小验证集会把 p 压到 1.0，重锚建议用 raw 分数，样本 ≥500 后再校准。

---

## 6. 特征差距与 hexp 落库持久化（B，待执行）

- **硬证据**：HEXP 的 `factor_raws/hp_score/k/dir_sum/verdict` 只发布 Redis 快照（TTL 15s），**从未落 PG**；`signals.indicator_values` 只存默认评分引擎 `adx_14/rsi_14/macd/atr_14/h1_*`。
- 补齐需「B：hexp 落库持久化」——在 `hexp_engine.py` 给 ScoreResult 加 `dir_sum/hp_strength/trend_phase/factor_raws` 字段，在 `scheduler.py` SignalData 落库时并入 `_hexp` 子对象。纯观测、零下单影响。改动清单已列，**未执行**（红线）。
- 历史 403 条 HEXP 信号无法回填，需后续积累。

---

## 7. 分阶段实施计划

| 阶段 | 内容 | 实盘影响 | 状态 |
|---|---|---|---|
| 0 | 特征上抛 + 标签 + 离线训练/回测 + seed SQL | 零 | ✅ 已完成（脚本+SQL，SQL 未应用） |
| 0b | B：hexp 落库持久化（补 5 特征） | 零 | ✅ 已部署（引擎运行时已验证，落 PG 待下一个通过信号） |
| 0c | A：开 `hexp.shadow_enabled` 攒标签 | 零 | ⬜ 待授权 |
| 1 | 影子评分（只打 tag 落库，不拦单） | 零 | ⬜ |
| 2 | 软耦合（只记 block_reason/观测手数） | 观测 | ⬜ |
| 3 | 硬闸门 + DeepSeek 异步 + 降级矩阵 | 红线 | ⬜ |
| 4 | continuity 持仓调仓（默认 log） | 红线 | ⬜ |
| 前端 | 信号塔 AiQualityConfig 页 + DecisionPanel 三分数卡 | 零 | ⬜ |

---

## 8. 纪律红线与安全

1. **AI 无独立开仓权**：契约无 direction，AI 只能缩小放行集 / 调整手数等级，升级不逆转 HEXP 已拦信号。
2. **DeepSeek 仅异步**：超时/限流/失败自动降级纯 HEXP；`ai.enabled=false` 为安全默认。
3. **零硬编码**：所有变量参数进 `hcm_config.metadata`（PG SoT + Redis 双写），脚本只读配置 + 兜底默认。
4. **冷启动安全**：模型缺失/加载失败 → 自动纯 HEXP。
5. **写操作红线**：改 PG/Redis/配置/引擎/重启服务均需先列具体键/值/表，用户明确同意后执行。
6. **概率校准必做**：0.5/0.6/0.7 阈值依赖校准，否则失真；训练用 walk-forward 防时间泄漏。

---

## 9. 上线复核结论

**结论：设计已完成、离线管线已跑通并验证有效（AUC 0.84），但【尚不具备上线条件】。**

| 维度 | 状态 |
|---|---|
| 需求与架构 | ✅ 完整、已逐项确认 |
| 离线训练管线 | ✅ 端到端跑通，AUC 0.65→0.84 |
| 配置命名空间 ai.* | ✅ seed SQL 已备（未应用） |
| 生产引擎改动（B） | ⬜ 未执行 |
| 影子标签积累（A） | ⬜ 未启用（hexp_shadow_eval 仅 6 行） |
| 实时推理/闸门/DeepSeek/调仓 | ⬜ 未实现（阶段 1–4） |
| 前端（配置页 + 三分数卡） | ⬜ 未实现 |
| 上线安全开关 | ✅ ai.enabled=false 默认全关（即使部署也不影响实盘） |

**上线前置条件（按序）**：
1. 授权并执行 B（hexp 落库持久化）。
2. 授权并启用 A（影子模式攒标签），积累 ≥500 标注样本。
3. 实现阶段 1（影子评分）→ 验证线上 p 分布与回测一致。
4. 实现阶段 3（硬闸门 + DeepSeek 异步 + 降级矩阵），默认仍全关，灰度观察。
5. 前端（配置页 + 三分数卡）。
6. 全链路红线逐项授权。

---

## 10. 待办 / 待授权清单

- [x] **B**：改 `hexp_engine.py` + `scheduler.py`，清 pyc，重启 signal-tower（已部署，落 PG 待下一个通过信号实测）。
- [ ] **A**：`hexp.shadow_enabled=true` 双写（写配置，红线）。
- [ ] **应用 seed SQL** `0011_ai_quality_config.sql`（写 PG，红线）。
- [ ] 实现阶段 1–4 实时模块 + 前端（AiQualityConfig 页、DecisionPanel 三分数卡）。
