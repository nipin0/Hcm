# HCM 共源模型 PRD — 信号塔双源信号增强方案

> 版本：v1.1（评审修订） | 日期：2026-07-18 | 作者：产品团队
> 修订摘要：校准维度 4D→1D（5 态对齐）、Phase 1 拆 P1a/P1b（冷启动路径）、Optuna 优先级链、FORCE_CLOSE 反转表、75→30 天 Token 安全、ConfigProviderV3 双写扩展、面板字段补齐
> 适用范围：HCM v2 信号塔（hcm-signal-tower）
> 核心口诀：**H1 定方向，M5 定买点，DeepSeek 判断赋分，AI 测算调权重值**

---

## 目录

1. [文档解读与现状对齐](#1-文档解读与现状对齐)
2. [产品目标与范围](#2-产品目标与范围)
3. [系统架构（优化后）](#3-系统架构优化后)
4. [详细需求](#4-详细需求)
5. [与现有信号塔集成方案](#5-与现有信号塔集成方案)
6. [DeepSeek 调用量削减方案](#6-deepseek-调用量削减方案)
7. [实现路线图](#7-实现路线图)
8. [风险评估与约束](#8-风险评估与约束)
9. [验收标准](#9-验收标准)
10. [术语表](#10-术语表)

---

## 1. 文档解读与现状对齐

### 1.1 原方案核心思路

用户提供的《共源模型》文档提出了一套**双源信号增强架构**，核心理念是将 AI 推理拆分为实时层和离线层：

| 层 | 时效性 | 计算主体 | 作用 |
| --- | --- | --- | --- |
| 实时层 | 每根 M5 K 线 | **本地规则 + 轻量模型**（逻辑回归 / 随机森林）| 假信号过滤、动态阈值、信号打分 |
| 离线层 | 每日 2 次 | **DeepSeek 大模型** | 行情样本标注（供本地模型训练）、参数稳定性校验、策略漏洞复盘 |
| 优化层 | 每日滚动 | **Optuna 本地** | 贝叶斯参数寻优 |
| 执行层 | 实时 | **MT5 bridge** | 下单、止损、趋势反转强制平仓 |

**原方案的价值主张**：DeepSeek 调用量降低 80-90%（从每信号数百次 → 每日固定 2 次），同时胜率仅衰减 1-2%。

### 1.2 现有信号塔架构对照

HCM v2 信号塔当前已具备以下能力，这些是**共源模型落地的现成底座**：

| 现有模块 | 文件 | 与共源模型的关系 |
| --- | --- | --- |
| M5 行情分类 | `regime_classifier.py` | 5 态分类（PRE_TREND / TREND / TREND_FADE / RANGE / NEUTRAL），**可直接替代原方案的 4 类行情** |
| H1 宏观定向 | `h1_regime_classifier.py` | 4 态（BULLISH / BEARISH / RANGE / TRANSITION），**H1 权重偏移表已落地** |
| 指标计算 | `indicator_calculator.py` | MA/MACD/ADX/BOLL/RSI/STOCH/ATR 全覆盖，**输入齐全** |
| 评分引擎 | `scoring_engine.py` | 5 套体制权重 + H1 偏置 + bar_momentum，**规则引擎基础坚实** |
| AI 调用 | `ai_invoker.py` | 15s 超时 + 熔断器 + 回退，**调用基础设施已有** |
| 信号发布 | `signal_publisher.py` | Redis Stream 发布，**扩展信号类型即可** |
| 调度器 | `scheduler.py` | bar_close 触发 → 指标 → 体制 → 评分 → AI → 发布，**管线清晰** |
| 配置系统 | `hcm_config.metadata`（PG）| **所有参数存储 SoT** |

### 1.3 原方案需要优化的点

对照现有系统后，对原方案做以下 5 项优化：

| # | 原方案设计 | 问题 | 优化方向 |
| --- | --- | --- | --- |
| ① | 本地训练随机森林模型 | 与现有规则引擎两套体系并存、维护成本高 | **保留并增强规则引擎**（`scoring_engine.py`），本地模型只做**权重校准**和**阈值自适应**，不替代规则 |
| ② | DeepSeek 标注数据存本地文件 | 无审计追踪、无回滚能力 | 标注结果**存入 PG**（`hcm_ai.labeled_samples`），可追溯、可回滚、可对比 |
| ③ | 每日收盘调用（收盘后 + 开盘前） | 黄金 24h 交易无"收盘"概念 | 改为**北京时间 06:00（亚盘开盘前）** 和 **18:00（欧盘开盘前）** 两次固定时刻 + 周日夜间一次全周复盘 |
| ④ | "强制平仓"信号未定义 | 只说"触发强制平仓"，未说如何接入 | 通过现有 **Redis Stream `signal:risk_passed`** 发布 `FORCE_CLOSE` 类信号，bridge 已有处理该 stream 的能力 |
| ⑤ | "80-90% 成本降低"乐观估计 | 未考虑 Prompt 长度增加（批量分析比单信号 Prompt 长得多）| 保守估计 **60-70% 成本降低**，在 PRD 中按保守数设计 |

---

## 2. 产品目标与范围

### 2.1 量化目标

| 指标 | 当前（v2 现状） | 共源模型后 | 测量方式 |
| --- | --- | --- | --- |
| DeepSeek 日调用量 | ~288 次/天（每 M5 bar × 24h） | ≤ 4 次/天（固定 2 + 极端 2） | AI invoker metrics |
| DeepSeek 月 API 成本 | ~$150-200/月（估算） | ≤ $30-50/月 | 账单 |
| 信号质量（胜率） | 基线（当前规则+AI 混合） | ≥ 基线的 95%（衰减 ≤ 5%） | 回测 60 天对比 |
| 本地模型推理延迟 | 不适用（无本地模型） | ≤ 10ms（非阻塞） | Python `time.perf_counter` |
| 每日标注训练耗时 | 不适用 | ≤ 5 分钟（异步） | 日志计时 |
| 参数自动调优周期 | 手动（配置面板） | 每日自动（Optuna）+ DeepSeek 校验 | 配置更新日志 |

### 2.2 范围边界

**在范围内**：
- 新建本地轻量评分校准模型（基于现有规则引擎 + DeepSeek 标注）
- 新建每日 DeepSeek 批量分析（行情标注、参数校验、漏洞复盘）
- 新建 Optuna 本地参数优化管线
- 新增 `FORCE_CLOSE` 信号类型（H1 趋势反转强制平仓）
- 扩展 `scoring_engine.py` 支持模型校准权重
- 扩展 `scheduler.py` 支持定时批量 AI 调用（替代逐信号调用）
- PG 新增 `hcm_ai.labeled_samples` / `hcm_ai.param_history` / `hcm_ai.gap_rules` 三张表

**不在范围内**：
- 替换现有 `scoring_engine.py` 的规则引擎（只增强、不替换）
- 变更 bridge 的 SL/TP 计算逻辑（bridge 稳定栈不动）
- 实现"随机森林"模型（**否决**：维护成本高、漂移风险大；改用量化规则 + 标注反馈校准）
- 每日训练的文件输出（全部走 PG，可审计）

### 2.3 核心约束（最高优先级，刚性不可违反）

以下三条约束在本方案全生命周期内**不得违反**，任何代码提交前必须检查：

#### 2.3.1 约束 ①：全程禁用硬编码

**定义**：任何参数值、阈值、开关、URL、key 名、权重值**不得以字面量出现在代码中**。必须从配置系统读取。

- **唯一配置 SoT**：PG `hcm_config.metadata`（`config_key` → `current_value` / `default_value`）。
- **读取路径**：信号塔内通过 `ConfigProviderV3.get()` 或直接 `hcm:config:v2`（Redis 热层）；bridge 通过 Redis `hget`。
- **写入路径**：前端面板 → `PUT /api/system/config/batch` → `ConfigProviderV3.set()` → PG + Redis 双写（见约束 ②）。
- **覆盖范围**：包括但不限于——打分门槛、各体制权重、假信号过滤阈值、ADX 阈值、ATR 乘数、EMA 周期、Optuna 超参空间、批量调用时间、熔断器配置、校准因子查表值。
- **自检手段**：代码审查时 grep `=\s*\d+` 或 `"threshold"\s*:\s*\d+` 必须追问是否该走配置。

#### 2.3.2 约束 ②：所有参数值 PG + Redis 双写

**定义**：任何参数变更（无论是面板保存、DeepSeek 批量调用产出、Optuna 建议采纳），必须**同时写入 PG 和 Redis**，不得只写其一。

| 写入触发方 | PG 落点 | Redis 落点 | 实现方式 |
| --- | --- | --- | --- |
| 前端面板保存 | `hcm_config.metadata` | `hcm:config:v2` | `ConfigProviderV3.set()` 扩展：PG 写成功后自动 `hset hcm:config:v2` |
| DeepSeek 批量调用产出 | `hcm_config.metadata` + `hcm_ai.param_history` | `hcm:config:v2` | 批量调用完成后统一写入 |
| Optuna 建议采纳 | `hcm_config.metadata` | `hcm:config:v2` | 同批量调用写入路径 |
| Bridge 启动自愈 | 无（只读） | `hcm:config:v2`（HSETNX 从 PG 回填缺键） | 已有 `_selfheal_config_from_pg`，不动 |

**PG 为 SoT（Source of Truth）**：任何时刻 PG 的值是权威的；Redis 是易失热层（容器重建可恢复）。排查配置必须查 PG，不可只看 Redis。

**一致性保障**：
- `ConfigProviderV3.set()` 必须原子化：先写 PG，成功后写 Redis。PG 失败不写 Redis；Redis 写入失败记录 WARNING 但不回滚 PG（Redis 是缓存层，不应因 Redis 故障阻塞配置更新；bridge 启动自愈会补）。
- 批量写入（DeepSeek/Optuna）使用**事务批量写 PG + pipeline 批量写 Redis**，减少往返。

#### 2.3.3 约束 ③：共源信号专属参数面板（选模型后显示）

**触发条件**：信号塔管理页面增加「信号模型」下拉选择器。当用户选择「共源信号」时，下方展示该模型专属的参数面板；选择其它模型时面板隐藏。

**面板设计原则**：
- **按功能分 6 组**，每组用不同颜色区分（呼应当前 `ConfigForm` grouped 分组卡片模式）
- **标签即中文**，不含英文 config_key（key 仍用英文，仅 label 中文）
- **帮助文本**：每个参数必须有 `description`（悬浮 tooltip），解释其作用和调整建议
- **默认值**：每个参数必须有 `defaultValue`，从 `hcm_config.metadata` 的 `default_value` 取（未配置时 fallback 到该字段）

详细面板字段设计见 [§4.5](#45-前端参数面板共源信号专属)。

---

## 3. 系统架构（优化后）

### 3.1 整体架构图

```
┌──────────────────────────────────────────────────────────────────┐
│                     HCM 信号塔 — 共源模型                           │
├──────────────────────────────────────────────────────────────────┤
│                                                                    │
│  实时层（每根 M5 K 线，< 10ms）                                     │
│  ┌──────────────┐   ┌──────────────┐   ┌───────────────────┐      │
│  │ H1 宏观定向   │ → │ M5 体制分类   │ → │ 规则引擎评分       │      │
│  │ (已有)        │   │ (已有)        │   │ (增强: 模型校准权重) │      │
│  └──────────────┘   └──────────────┘   └──────┬────────────┘      │
│                                                 │                  │
│  ┌──────────────────────────────────────────────▼───────────┐     │
│  │              本地假信号过滤（增强）                         │     │
│  │  · 周期背离检测  · 布林收口假突破  · 数据窗口期降分         │     │
│  │  · 超买超卖钝化  · 连续亏损熔断    · 突发波动阈值提升       │     │
│  └──────────────────────────────────────────────────────────┘     │
│                                    │                               │
│              ▼ signal:risk_passed (Redis Stream)                    │
│                                    │                               │
│  ┌─────────────────────────────────▼─────────────────────────┐    │
│  │                  MT5 执行层（bridge，不改造）                │    │
│  │  · 动态 ATR SL/TP  · 保本/移动止盈  · FORCE_CLOSE 信号     │    │
│  └──────────────────────────────────────────────────────────┘     │
│                                                                    │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─              │
│                                                                    │
│  离线层（每日定时，批量）                                            │
│                                                                    │
│  调用 ① 每日 06:00（亚盘开盘前）                                    │
│  ┌──────────────────────────────────────────────────────────┐     │
│  │  输入：近 75 天完整行情 + 所有信号 + 盈亏记录                 │     │
│  │  输出三件事：                                               │     │
│  │  ① 行情 K 线标注（强趋势/弱趋势/震荡/异动）→ 写 PG 标注表    │     │
│  │  ② 参数稳定性分析 → 输出次日基准参数组 → 写 PG 参数历史表    │     │
│  │  ③ 策略漏洞复盘 → 识别频繁亏损信号形态 → 写 PG 规则表        │     │
│  └──────────────────────────────────────────────────────────┘     │
│                                                                    │
│  调用 ② 每日 18:00（欧盘开盘前）                                    │
│  ┌──────────────────────────────────────────────────────────┐     │
│  │  输入：当日财经日历 + 隔夜走势 + 当日已产生信号               │     │
│  │  输出：风险系数（低/中/高）→ 影响次日打分门槛 0/+5/+10       │     │
│  └──────────────────────────────────────────────────────────┘     │
│                                                                    │
│  调用 ③ 周日 20:00（全周复盘，周度，非每日）                        │
│  ┌──────────────────────────────────────────────────────────┐     │
│  │  输入：前一周完整交易记录 + 波动率分布 + 最大回撤             │     │
│  │  输出：周度策略调整建议 + 紧急回撤告警                       │     │
│  └──────────────────────────────────────────────────────────┘     │
│                                                                    │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─              │
│                                                                    │
│  优化层（每日滚动，本地 Optuna，不占用 AI 额度）                      │
│  ┌──────────────────────────────────────────────────────────┐     │
│  │  Optuna 贝叶斯调参 → 产出 30 组优质参数组合                  │     │
│  │  DeepSeek 校验（合并到调用 ①）→ 从 30 组中挑最抗过拟合的     │     │
│  │  次日基准参数自动写入 hcm_config.metadata                   │     │
│  └──────────────────────────────────────────────────────────┘     │
│                                                                    │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 四大模块映射

| 模块 | 改造策略 | 新建/改造 |
| --- | --- | --- |
| **模块一：本地实时评分层** | 增强现有 `scoring_engine.py` + 假信号过滤 | **改造** |
| **模块二：每日 DeepSeek 批量分析** | 新建 `daily_batch.py`，替代逐信号 AI 调用 | **新建** |
| **模块三：离线参数调优** | 新建 `optuna_tuner.py`，调用结果经 DeepSeek 校验 | **新建** |
| **模块四：执行增强** | 新增 `FORCE_CLOSE` 信号类型 | **改造** |

---

## 4. 详细需求

### 4.1 模块一：本地实时评分层（增强现有评分引擎）

#### 4.1.1 轻量校准模型

**设计原则**：不新建随机森林，而是在现有规则评分之上叠加**校准权重**。

现有评分公式（简化）：

```
raw_score = Σ(component_weight_i × component_score_i)
```
- H1 偏置通过 `H1_WEIGHT_OFFSET` 调整各 component 的权重

增强后：

```
calibrated_score = raw_score × calibration_factor
calibration_factor = calib[M5_regime]     ← 简化到 M5 体制单维（5 态）
```

`calibration_factor` 是一个查表型乘数（0.7~1.3），按 M5 体制（PRE_TREND / TREND / TREND_FADE / RANGE / NEUTRAL）分别取值，由每日 DeepSeek 标注结果更新。

> **设计决策（v1.1 评审修订）**：原设计为 4 维函数 `f(H1×M5×波动率×时段)`，组合爆炸 240 个条目——配置键不可管理、标注量过大。**降维到 M5 体制单维（5 个 key）**，与现有 `regime_classifier` 的 5 态完全对齐。H1 偏置、波动率、时段的影响通过 §4.1.3 的自适应门槛独立生效，不与校准因子耦合。

**存储**：`hcm_config.metadata`（5 个 `co.calib.*` 键 + 该体制对应 factor 值），比 PG 查表更轻量、配置面板可直接编辑。

**更新**：每日 DeepSeek 调用 ① 结束时自动刷新；未积累 ≥ 7 天标注数据前全部校准因子恒为 1.0（等效关闭校准，见 §4.1.4 冷启动说明）。

#### 4.1.2 假信号过滤规则

在原方案的基础上，增加 **5 条假信号过滤规则**（全本地、毫秒级）：

| # | 规则 | 检测方式 | 动作 |
| --- | --- | --- | --- |
| F1 | **周期背离** | H1 价格新高/新低 但 MACD/RSI 顶/底背离 | 信号分 -20 |
| F2 | **布林收口假突破** | BOLL 带宽 < 近 20 根均值 × 50% | 信号直接作废 |
| F3 | **数据窗口期** | 非农/利率决议前 30 分钟 | 信号分 -25 |
| F4 | **超买超卖钝化** | 单边行情 RSI 持续 >70 或 <30，反转信号取消 | 反转方向信号作废 |
| F5 | **连续亏损熔断** | 近 3 笔连续全止损 | 全部信号分 -30 + 触发极端行情 DeepSeek 补充调用 |

**实现位置**：在 `scoring_engine.py` 的 `_assess()` 中，于阈值门控之前执行过滤。

#### 4.1.3 自适应入市门槛

| 行情状态 | 打分门槛 | 仓位系数 | 止损 ATR 倍数 | 盈亏比 |
| --- | --- | --- | --- | --- |
| 强趋势 (ADX > 35) | 65 | 1.0× | 0.5 | ≥ 2:1 |
| 温和趋势 (25 ≤ ADX ≤ 35) | 70 | 1.0× | 0.6 | ≥ 1.5:1 |
| 震荡 (ADX < 20) | 不通过（拦截） | 0 | N/A | N/A |
| 突发波动 (ATR > 2× 近期均值) | 80 | 0.5× | 0.7 | ≥ 1.5:1 |
| 高风险日（DeepSeek 调用 ② 判定） | 原门槛 +10 | 0.5× | 0.5 | ≥ 2:1 |

**管理方式**：上述参数均存在 `hcm_config.metadata`，DeepSeek 调用 ① 按日输出、Optuna 按周建议调整、面板仍可手动覆盖。

#### 4.1.4 校准层冷启动

**问题**：共源模型首次部署时，`hcm_ai.labeled_samples` 为空——DeepSeek 尚未标注任何 K 线，校准因子无数据可更新。

**策略**：
- 部署后 7 天内（自然日后，非交易日），**全部校准因子恒为 1.0**（等效关闭校准），评分引擎仅启用 F1-F5 过滤 + 自适应门槛。
- 每日 06:00 DeepSeek 调用 ① 正常执行、标注结果正常写入 `hcm_ai.labeled_samples`。
- 第 8 天起，`local_calibrator.py` 检查标注数据行数 ≥ 阈值的天数（`co.calib.min_days`，默认 7），满足则启用校准因子计算（从 PG 读标注结果、按 M5 体制聚合平均 factor、写入 5 个 `co.calib.*` 配置键、触发 PG+Redis 双写）。

**回测验证时的处置**：Phase 1 拆分为两个子阶段（见 §7）：
- **P1a**：仅 F1-F5 过滤 + 自适应门槛（校准 = 1.0），直接回测可验证
- **P1b**：校准层激活（需先积累 ≥ 7 天标注数据后启用），追加回测验证

---

### 4.2 模块二：每日 DeepSeek 批量分析层（新建 `daily_batch.py`）

#### 4.2.1 调用 ① — 每日 06:00 批量分析

**触发**：asyncio 定时任务，每日 06:00 北京时间（周日不触发，改为周日 20:00 周复盘）。

**输入**：
- 近 30 天 M5/H1 完整 K 线（OHLCV）——**上限 30 天**，75 天原始 K 线超出 DeepSeek 128K token 上下文窗口（30 天 × 288 M5 bars = ~8.6K 行，约 40K tokens，安全余量大）
- 同期所有产生的信号（方向、分数、是否成交、盈亏）
- 当前运行的全部配置参数（从 PG 读）
- Optuna 最近一轮产出的参数组合（如有）

**Prompt 结构**（一次调用，三个子任务）：

```
【任务 1 — 行情标注】
请将以下 30 天的 M5/H1 K 线数据按 4 类行情标注：
- strong_trend: ADX>35 且 MA 多/空排列持续 > 4h
- weak_trend: 25≤ADX≤35
- range: ADX<20 且布林收窄
- shock: ATR 突增 > 2× 且价格急涨急跌
输出 JSON 数组: [{bar_time, label, confidence}]

【任务 2 — 参数稳定性分析】
当前参数: (EMA, ADX, RSI, ATR, 盈亏比...)
Optuna 建议的 30 组参数: [...]
请判断每组参数的抗过拟合能力，输出最稳定的 1 组（及理由）。

【任务 3 — 策略漏洞复盘】
回顾近期亏损信号（连续止损 ≥2 笔），识别重复出现的亏损形态，
输出 1-3 条补充过滤规则（规则格式：条件 + 动作）。
```

**输出处理**：
- 标注结果 → 写入 `hcm_ai.labeled_samples`（PG 表，供本地模型校准）
- 最佳参数组 → 写入 `hcm_ai.param_history` + 更新 `hcm_config.metadata` 对应键
- 补充过滤规则 → 写入 `hcm_ai.gap_rules`（PG 表，供 `scoring_engine.py` 加载）

#### 4.2.2 调用 ② — 每日 18:00 风险预判

**触发**：asyncio 定时任务，每日 18:00。

**输入**：
- 当天财经日历（非农/利率/CPI 等）——**来源由 `co.batch.calendar_source` 决定**，默认 `deepseek_knowledge`（DeepSeek 自身训练数据覆盖公开财经日历，无需额外数据源）。未来如需精确到分钟的实时事件可加 `forexfactory_rss` 选项。
- 隔夜亚盘走势（开盘价 → 当前价）
- 当日已产信号统计

**Prompt**：
```
【任务 — 欧盘开盘风险预判】
基于当日财经事件（等级: 高/中/低）和隔夜亚盘走势，输出今日风险系数：
- low: 无重大事件、亚盘平稳
- medium: 有中等级别事件、或亚盘波动 > 0.3%
- high: 有高等级事件（非农/利率）、或亚盘波动 > 0.5%
仅输出: {"risk_level": "low|medium|high", "reason": "..."}
```

**本地反应**：`risk_level` 写入 Redis `hcm:risk:daily_level`（scheduler 每 bar 读取，对应调整打分门槛）。

#### 4.2.3 补充调用 — 极端行情（每日上限 2 次）

**触发条件**：

| 条件 | 检测方式 | 频率限制 |
| --- | --- | --- |
| ATR 翻倍 | 当前 ATR > 近 3 个 M5 周期 ATR 均值 × 2.0 | 单日首次触发后冷却 2h |
| 连续 3 笔全止损 | 统计 `orders` 表中近 3 笔均为 `profit < 0` | 每次触发后冷却 2h |

**调用内容**：仅请求短期风险评估（无全量复盘），返回 `{"adjust_factor": 0.7-1.0, "reason": "..."}`，本地据此临时调整打分门槛（最多 2h 有效）。

#### 4.2.4 调用 ③ — 周日 20:00 全周复盘

**触发**：asyncio 定时任务，每周日 20:00。

**输入**：前一周完整交易记录（信号量、成交率、胜率、总盈亏、最大回撤）

**输出**：周度策略调整建议 + 连续 2 周回撤 > 10% 时触达用户（推送到通知系统）。

---

### 4.3 模块三：离线参数自动优化层（新建 `optuna_tuner.py`）

#### 4.3.1 优化设计

**完全依赖标准库 + optuna（`pip install optuna`），本地运行，不调用 AI**。

| 参数 | 值 |
| --- | --- |
| 滚动窗口 | 训练 60 天 / 测试 15 天，每日滚动更新 |
| 试验次数 | 每轮 100 trials（TPE sampler） |
| 优化目标 | 最大化夏普比率（回测），约束：最大回撤 < 15%、有效交易 ≥ 60 笔 |
| 超参维度 | EMA 周期(8-80)、ADX 阈值(20-28)、RSI(20-30/70-80)、ATR sl(0.3-0.8)、ATR tp(2-5)、min RR(1.3-2.0)、打分门槛(55-75) |
| 频率 | 每日一次（在 06:00 DeepSeek 调用 ① 之前完成） |
| DeepSeek 角色 | **仅校验**：optuna 产出 30 组最优参数，写入调用 ① 的 Prompt，由 DeepSeek 挑出最不易过拟合的 1 组 |

#### 4.3.2 结果存储与优先级

最优参数组写入 `hcm_ai.param_history`（含日期、夏普比率、回撤、DeepSeek 稳定性评分、**来源标记 `source`**：`optuna` / `deepseek_pick` / `manual`），同时更新 `hcm_config.metadata` 对应配置键（使次日运行即生效）。

**参数生效优先级**（链式，高优先级覆盖低优先级）：

```
面板手动覆盖  >  DeepSeek 校验采纳  >  Optuna 原始建议
   (manual)          (deepseek_pick)        (optuna)
```

- **Optuna** 每日产出 30 组参数写入 `hcm_ai.param_history`（source=`optuna`），但不直接写入 `hcm_config.metadata`。
- **DeepSeek 调用 ①** 从 30 组中挑出最稳定 1 组，写入 `hcm_ai.param_history`（source=`deepseek_pick`），**同时**写入 `hcm_config.metadata` 对应键——次日生效。
- **面板手动修改**后，写入 `hcm_config.metadata`（source=`manual`），直到下次 DeepSeek 校验采纳前保持覆盖。
- **Optuna 下次运行时**从 `hcm_config.metadata` 读当前生效值作为基准（不自己玩自己的循环），避免漂移。

---

### 4.4 模块四：MT5 执行增强层（改造信号发布 + bridge）

#### 4.4.1 新增信号类型：FORCE_CLOSE

当 H1 分类器判定趋势反转，在 `scheduler.py` 的 bar_close 处理中产生 `FORCE_CLOSE` 信号。**反转判定表**（明确到每种状态跃迁）：

| 原状态 | 新状态 | 动作 | 理由 |
| --- | --- | --- | --- |
| BULLISH | BEARISH | 全平 (`all`) | 强趋势翻转 |
| BEARISH | BULLISH | 全平 (`all`) | 同上 |
| BULLISH | RANGE | 半平 (`half`) | 趋势走弱，部分退出保留利润 |
| BEARISH | RANGE | 半平 (`half`) | 同上 |
| TRANSITION | BULLISH/BEARISH | **不触发** | 混沌到趋定是入场信号、不是平仓 |
| RANGE | BULLISH/BEARISH | **不触发** | 突破是入场信号、不是平仓 |
| RANGE | TRANSITION | **不触发** | 无方向变化 |
| TRANSITION | RANGE | **不触发** | 无方向变化 |

> 反转确认：连续 3 根 M5 bar 确认新方向且 ADX > `co.exec.fc_adx_min` 后才触发（防假翻转）。

信号格式：

```python
{
    "signal_type": "FORCE_CLOSE",
    "symbol": "XAUUSD",
    "reason": "H1 trend reversed: BULLISH -> BEARISH (ADX 32)",
    "close_mode": "all"  # "all"（全平）或 "half"（半平），由面板 co.exec.fc_close_mode 配置
}
```

**发送方式**：与现有交易信号同一 Stream（`signal:risk_passed`），bridge **无需改造**——已有消费该 stream 的能力，只需识别新 `signal_type`。

#### 4.4.2 持仓检查频率增强

| 检查项 | 当前 | 增强后 |
| --- | --- | --- |
| H1 趋势方向 | 仅在 M5 bar_close 触发 | **每 15 分钟**独立检查（`h1_regime_classifier` 已有能力） |
| 趋势反转判定 | 不触发平仓信号 | ADX>30 且 regime 翻转 → 发布 `FORCE_CLOSE` |
| 反转确认 | N/A | 连续 3 个 M5 bar 确认新方向后触发（防假翻转） |

#### 4.4.3 每日数据落库增强

确保每笔订单的成交记录（订单时间/方向/手数/入场价/止损价/止盈价/出场价/盈亏/持仓时长）**完整写 `hcm_broker.orders` 表**（已在 P2-1 中落地），供 DeepSeek 每日复盘使用。

### 4.5 前端参数面板（共源信号专属）

#### 4.5.1 显示逻辑

信号塔管理页面（`hcm-web/frontend/src/pages/system/SignalTower.tsx` 或新建 `CoSourceConfig.tsx`）顶部增加「信号模型」下拉选择器：

```
信号模型: [ 默认规则引擎 ▼ ]   ← 下拉
          ├─ 默认规则引擎       ← 当前 v2 评分引擎
          └─ 共源信号           ← 选择后下方面板出现
```

- 选择「默认规则引擎」：不显示共源参数面板（缺省行为，向后兼容）。
- 选择「共源信号」：下方展示本节设计的 6 组参数卡片。
- 模型选择值写入 `hcm_config.metadata` → `signal.active_model`（值：`"default"` / `"co_source"`），scheduler 读取后决定走哪条评分链路。

#### 4.5.2 分组与配色

共 6 组，每组独立卡片，颜色区分，避免混淆：

| 组 | 名称 | 颜色 | 说明 |
| --- | --- | --- | --- |
| G1 | 校准因子 | 蓝 `#3b82f6` | 查表型乘数（0.7~1.3），叠加到规则评分 |
| G2 | 假信号过滤 | 橙 `#f59e0b` | F1-F5 开关 + 阈值 |
| G3 | 自适应门槛 | 绿 `#10b981` | 各行情状态下的打分门槛、仓位、SL/TP |
| G4 | 批量 AI 调用 | 紫 `#a855f7` | 每日 DeepSeek 调用 ①/②/③ 开关与参数 |
| G5 | Optuna 优化 | 青 `#06b6d4` | 参数寻优窗口、目标函数、试验次数 |
| G6 | 执行增强 | 红 `#ef4444` | FORCE_CLOSE、反转确认、持仓检查 |

#### 4.5.3 字段详表

##### G1 — 校准因子（蓝）

| config_key | label | type | default | description |
| --- | --- | --- | --- | --- |
| `co.calib.pre_trend` | 前趋势校准因子 | number (0.5-1.5, step 0.05) | 1.0 | PRE_TREND 体制时 raw_score 乘以此系数 |
| `co.calib.trend` | 强趋势校准因子 | number (0.5-1.5) | 1.0 | TREND 体制（ADX>35）时乘数；>1=更积极开仓 |
| `co.calib.trend_fade` | 趋势衰减校准因子 | number (0.5-1.5) | 1.0 | TREND_FADE 体制（趋势转弱）时乘数 |
| `co.calib.range` | 震荡校准因子 | number (0.5-1.5) | 1.0 | RANGE 体制（ADX<20）时乘数（通常 <1 抑制开仓，但震荡已被 §4.1.3 拦截） |
| `co.calib.neutral` | 中性校准因子 | number (0.5-1.5) | 1.0 | NEUTRAL 体制时乘数 |

> 以上 5 个 key 与 `regime_classifier.py` 的 5 个 Regime 枚举一一对应。冷启动期（标注数据 < 7 天）全部恒为 1.0。 |

##### G2 — 假信号过滤（橙）

| config_key | label | type | default | description |
| --- | --- | --- | --- | --- |
| `co.filter.f1_enabled` | F1 周期背离检测 | switch | true | H1 价格新高/新低但 MACD/RSI 背离时扣 20 分 |
| `co.filter.f1_penalty` | F1 扣分值 | number (0-40) | 20 | 背离检测命中后的扣分额度 |
| `co.filter.f2_enabled` | F2 布林收口假突破 | switch | true | 带宽 < 近 20 根均值 50% 时作废信号 |
| `co.filter.f2_ratio` | F2 带宽比例阈值 | percentage (10-80) | 50 | 当前带宽 / 近 20 根均值 < 此比例即触发 |
| `co.filter.f3_enabled` | F3 数据窗口期降分 | switch | true | 非农/利率前 30 分钟降低信号分 |
| `co.filter.f3_minutes` | F3 窗口时间 (分钟) | number (10-60) | 30 | 数据公布前几分钟生效 |
| `co.filter.f3_penalty` | F3 降分值 | number (0-40) | 25 | |
| `co.filter.f4_enabled` | F4 超买超卖钝化 | switch | true | 单边行情 RSI 持续极端时取消反转信号 |
| `co.filter.f4_rsi_upper` | F4 RSI 超买阈值 | number (60-80) | 70 | RSI 持续高于此值且方向为 SELL 时取消（多头钝化不做空） |
| `co.filter.f4_rsi_lower` | F4 RSI 超卖阈值 | number (20-40) | 30 | RSI 持续低于此值且方向为 BUY 时取消（空头钝化不做多） |
| `co.filter.f5_enabled` | F5 连续亏损熔断 | switch | true | 连续 N 笔全止损时 -30 分 + 触发极端补充调用 |
| `co.filter.f5_consecutive` | F5 连续亏损次数 | number (2-5) | 3 | 达到此次数即熔断 |

##### G3 — 自适应门槛（绿）

| config_key | label | type | default | description |
| --- | --- | --- | --- | --- |
| `co.gate.strong.trend` | 强趋势打分门槛 | number (50-80) | 65 | ADX>35 时信号放行最低分 |
| `co.gate.strong.lot` | 强趋势仓位系数 | number (0.5-2.0) | 1.0 | 标准仓位乘数 |
| `co.gate.strong.sl_atr` | 强趋势 SL ATR 倍数 | number (0.2-1.0) | 0.5 | |
| `co.gate.strong.rr_min` | 强趋势最低盈亏比 | number (1.0-3.0) | 2.0 | |
| `co.gate.weak.trend` | 弱趋势打分门槛 | number (50-80) | 70 | 25≤ADX≤35 |
| `co.gate.weak.lot` | 弱趋势仓位系数 | number (0.2-1.0) | 1.0 | |
| `co.gate.weak.sl_atr` | 弱趋势 SL ATR 倍数 | number (0.2-1.0) | 0.6 | |
| `co.gate.weak.rr_min` | 弱趋势最低盈亏比 | number (1.0-3.0) | 1.5 | |
| `co.gate.range.block` | 震荡市拦截开关 | switch | true | ADX<20 时直接拦截全部开仓信号 |
| `co.gate.shock.trend` | 突发波动打分门槛 | number (60-90) | 80 | ATR 翻倍时 |
| `co.gate.shock.lot` | 突发波动仓位系数 | number (0.1-1.0) | 0.5 | |
| `co.gate.shock.sl_atr` | 突发波动 SL ATR 倍数 | number (0.3-1.0) | 0.7 | |
| `co.gate.shock.rr_min` | 突发波动最低盈亏比 | number (1.0-3.0) | 1.5 | |
| `co.gate.risk.high_offset` | 高风险日门槛偏移 | number (0-20) | 10 | DeepSeek 风险系数=high 时门槛加此值 |
| `co.gate.risk.med_offset` | 中风险日门槛偏移 | number (0-15) | 5 | |

##### G4 — 批量 AI 调用（紫）

| config_key | label | type | default | description |
| --- | --- | --- | --- | --- |
| `co.batch.enabled` | 启用每日批量分析 | switch | true | 关闭后信号塔退回到逐信号 AI 调用 |
| `co.batch.call1_time` | 调用 ① 时间 (BJ) | text | `06:00` | 行情标注+参数校验+漏洞复盘 |
| `co.batch.call2_time` | 调用 ② 时间 (BJ) | text | `18:00` | 欧盘开盘前风险预判 |
| `co.batch.call3_enabled` | 启用周日周复盘 | switch | true | 每周日 20:00 全周策略回顾 |
| `co.batch.emergency.enabled` | 启用极端行情补充调用 | switch | true | ATR 翻倍或连续亏损时临时调 AI |
| `co.batch.emergency.max_per_day` | 补充调用每日上限 | number (1-4) | 2 | |
| `co.batch.emergency.cooldown_h` | 补充调用冷却时间 (h) | number (1-6) | 2 | 同条件触发间隔 |
| `co.batch.lookback_days` | 批量分析回看天数 | number (15-60) | 30 | DeepSeek 接收的历史数据天数（上限受模型上下文窗口约束） |
| `co.batch.timeout_sec` | 批量调用超时 (秒) | number (30-120) | 60 | 单次 DeepSeek 批量调用最长等待 |
| `co.batch.calendar_source` | 财经日历来源 | select | `deepseek_knowledge` | 选项: deepseek_knowledge（DeepSeek 自身判断）/ none（关闭） |

##### G5 — Optuna 优化（青）

| config_key | label | type | default | description |
| --- | --- | --- | --- | --- |
| `co.optuna.enabled` | 启用 Optuna 自动调参 | switch | true | 关闭后只用面板手动参数 |
| `co.optuna.train_days` | 训练集天数 | number (30-90) | 60 | Optuna 回测训练窗口 |
| `co.optuna.test_days` | 测试集天数 | number (7-30) | 15 | |
| `co.optuna.trials` | 每轮试验次数 | number (50-200) | 100 | TPE sampler trials |
| `co.optuna.target` | 优化目标 | select | `sharpe_ratio` | 选项: sharpe_ratio / sortino_ratio / calmar_ratio |
| `co.optuna.max_drawdown_pct` | 最大回撤约束 (%) | number (5-30) | 15 | 任何参数组的回撤超过此值即淘汰 |
| `co.optuna.min_trades` | 最少有效交易 | number (30-100) | 60 | 测试期内交易次数低于此值淘汰 |

##### G6 — 执行增强（红）

| config_key | label | type | default | description |
| --- | --- | --- | --- | --- |
| `co.exec.force_close_enabled` | 启用 FORCE_CLOSE 信号 | switch | true | H1 趋势反转时发布强制平仓信号 |
| `co.exec.fc_close_mode` | 强反转平仓模式 | select | `all` | 选项: all（全平）/ half（半平）/ half_on_weakening（走弱半平+翻转全平）——对应 §4.4.1 反转判定表的全平/半平行 |
| `co.exec.fc_bar_confirm` | 反转确认 bar 数 | number (2-5) | 3 | 连续 N 根 M5 bar 确认新方向后才触发 |
| `co.exec.fc_adx_min` | 反转最低 ADX | number (20-40) | 30 | ADX 低于此值不触发 FORCE_CLOSE |
| `co.exec.position_check_min` | 持仓检查间隔 (分钟) | number (5-30) | 15 | 每 N 分钟检查一次 H1 趋势是否翻转 |

#### 4.5.4 前端技术实现

- **组件复用**：使用 `ConfigForm`（`grouped` + `groupColumns={2}`，6 组 2 列布局，每组独立卡片）。
- **数据流**：GET `/api/system/config/batch?keys=co.*` 加载全部 `co.*` 前缀配置 → 渲染面板 → 用户修改后 PUT `/api/system/config/batch` → 后端 `ConfigProviderV3.set()` 逐键双写 PG + Redis（见约束 ②）。
- **模型选择器**：独立 `Select` 组件在上述 ConfigForm 上方，选择「共源信号」→ 渲染 `CoSourcePanel`。
- **路由**：`/system/signal-tower/co-source`（或集成到现有信号塔页面）。
- **安全**：无 secret 字段，无需脱敏。

---

## 5. 与现有信号塔集成方案

### 5.1 保留不变

| 组件 | 理由 |
| --- | --- |
| `regime_classifier.py`（M5 5 态） | 比原方案的 4 类更精细，无需降级 |
| `h1_regime_classifier.py`（H1 4 态） | H1 偏置权重已落地、成熟 |
| `indicator_calculator.py` | 指标全覆盖 |
| `signal_publisher.py` | 支持扩展信号类型，不需改动核心 |
| `ai_invoker.py`（熔断器） | 仍用于批量调用，可复用 |
| `mt5_bridge.py` | 刚稳定的栈，不碰 |

### 5.2 新增/改造

| 组件 | 操作 | 说明 |
| --- | --- | --- |
| `daily_batch.py` | **新建** | DeepSeek 批量调用 + 输出处理 |
| `optuna_tuner.py` | **新建** | 本地参数优化 |
| `local_calibrator.py` | **新建** | 查表型校准乘数，被 `scoring_engine.py` 调用 |
| `scoring_engine.py` | **改造** | 接入 `local_calibrator` + 假信号过滤 5 规则 + 自适应门槛 |
| `scheduler.py` | **改造** | ① `bar_close` 中 AI 调用改为优先级队列（逐信号 → 优先本地评分）② 新增定时任务（06:00/18:00/周日）③ 新增 FORCE_CLOSE 信号 |
| `signal_publisher.py` | **扩展** | 支持 `FORCE_CLOSE` 信号类型 |
| PG | **新建 3 表** + **扩展配置键** | `hcm_ai.labeled_samples` / `hcm_ai.param_history` / `hcm_ai.gap_rules`；`hcm_config.metadata` 新增 **55 个 `co.*` 配置键**（校准因子 5 + 冷启动 1、假信号过滤 12、自适应门槛 14、批量 AI 调用 10、Optuna 7、执行增强 5、信号模型选择 1） |
| `hcm_config.metadata` | **扩展** | 新增 `ai.daily_batch_enabled` / `ai.optuna_enabled` / 各过滤规则总开关 |

### 5.3 数据流

```
每根 M5 K 线：
  bar_close 触发
    → indicator_calculator (指标, 已有)
    → h1_regime_classifier (H1 方向, 已有)
    → regime_classifier (M5 体制, 已有)
    → scoring_engine (评分 + 过滤, 改造后)
      → local_calibrator (查表校准, 新建)
      → 假信号过滤 5 规则 (新建)
      → 自适应门槛判断 (新建)
    → signal_publisher (发布, 扩展后)

每日 06:00：
  optuna_tuner (本地调参, 新建)
    → 30 组参数 → 写入调用 ① Prompt
  daily_batch.call_1 (DeepSeek 批量, 新建)
    → 标注 → labeled_samples
    → 参数 → param_history + hcm_config.metadata
    → 规则 → gap_rules
  local_calibrator.reload (从 PG 重载校准表)

每日 18:00：
  daily_batch.call_2 (DeepSeek 风险预判, 新建)
    → Redis hcm:risk:daily_level

极端行情（按需）：
  daily_batch.emergency_call (最多 2 次/天)
    → Redis hcm:risk:emergency_adjust (2h TTL)

周日 20:00：
  daily_batch.call_3 (全周复盘)
    → 周报建议 + 回撤告警
```

---

## 6. DeepSeek 调用量削减方案

### 6.1 调用频率对比

| 场景 | 当前（v2） | 共源模型后 | 削减 |
| --- | --- | --- | --- |
| 常规交易日（24h / 288 根 M5 bar） | ~288 次/天（每 bar 可能调 AI） | **2 次/天**（06:00 + 18:00） | **99%+** |
| 全周（5 交易日） | ~1440 次/周 | 10 次固定 + 最多 10 次极端 = **≤20 次/周** | **98%+** |
| 全月（22 交易日） | ~6336 次/月 | 44 次固定 + 最多 44 次极端 + 4 次周复盘 = **≤92 次/月** | **98%+** |

> **重要更正**：原方案说"80-90% 削减"，但当前 v2 的 AI 调用并非"每 bar 必调"——熔断器、回退机制、NO_TRADE 过滤已减少大量调用。保守估计**月调用量从 ~1000-1500 降至 ≤92**，削减 **90-94%**。

### 6.2 成本变化

| 项目 | 当前月估 | 共源模型后 | 注释 |
| --- | --- | --- | --- |
| DeepSeek API tokens | ~2M tokens/月 | 批量 Prompt 更长：~500K tokens/月 | 批量分析一次 Prompt 可能 10-20K tokens，但只有 2 次/天 |
| 月 API 费用 | ~$10-30（已有 circuit breaker 减少） | ~$5-15 | 固定调用少但单次 tokens 多 |
| Optuna 计算 | 无 | 本地 CPU，0 成本 | 100 trials × 几秒 = 几分钟/天 |

---

## 7. 实现路线图

### Phase 1a：假信号过滤 + 自适应门槛（无需标注数据，可立即验证）

| 步骤 | 内容 | 产出 |
| --- | --- | --- |
| P1a.1 | `scoring_engine.py` 改造：F1-F5 假信号过滤（全部 5 条） + 自适应门槛表读取 | 过滤 + 门槛 |
| P1a.2 | `scheduler.py` 改造：bar_close 中接入过滤 + 门槛，AI 调用改为可跳过（`signal.active_model` 配置开关） | 调度改造 |
| P1a.3 | `hcm_config.metadata` 扩展：新增全部 `co.filter.*` / `co.gate.*` 配置键 + seed SQL | 配置 |
| P1a.4 | **回测验证**：60 天回测对比 old scoring vs 仅过滤+自适应门槛（校准=1.0），胜率 ≥ 基线 95% | 质量验证 |
| P1a.5 | 部署到 docker 环境，保留原评分链路作为 fallback | 灰度上线 |

### Phase 1b：校准层（需积累 ≥ 7 天标注数据后启用）

| 步骤 | 内容 | 产出 |
| --- | --- | --- |
| P1b.1 | 新建 `daily_batch.py` — 调用 ① 每日 06:00 DeepSeek 批量分析（标注 + 参数校验 + 漏洞复盘） | DeepSeek 批量调用 |
| P1b.2 | 新建 `hcm_ai.labeled_samples` / `hcm_ai.param_history` / `hcm_ai.gap_rules` 三张 PG 表 + DDL | 数据表 |
| P1b.3 | 新建 `local_calibrator.py`（查表型 5 态校准因子，从 PG 标注表计算） | 校准模块 |
| P1b.4 | `scoring_engine.py` 接入 `local_calibrator`（冷启动期校准=1.0，≥7 天标注后启用） | 增强评分 |
| P1b.5 | 扩展 `ConfigProviderV3.set()`：PG 写成功后自动 `hset hcm:config:v2`（约束 ② PG+Redis 双写落地） | 双写机制 |
| P1b.6 | 积累 ≥ 7 天标注数据后，追加回测验证：过滤+门槛+校准 vs 仅过滤+门槛，确认校准对胜率有 ≥0.5% 正向贡献 | 校准效果验证 |

### Phase 2：Optuna 参数优化 + 风险预判

| 步骤 | 内容 | 产出 |
| --- | --- | --- |
| P2.1 | 新建 `optuna_tuner.py`（60+15 天滚动 + 100 trials TPE） | 参数优化 |
| P2.2 | `daily_batch.py` 扩展调用 ②（18:00 风险预判）| 风险预判 |
| P2.3 | `daily_batch.py` 扩展补充调用（极端行情触发）| 应急调用 |
| P2.4 | `scheduler.py` 扩展 18:00 定时 + 极端行情触发检测 | 调度扩展 |
| P2.5 | 回测验证：Optuna 优化参数 vs 当前固定参数 | 优化效果验证 |

### Phase 3：全量切换 + 闭环

| 步骤 | 内容 | 产出 |
| --- | --- | --- |
| P3.1 | 新增 `FORCE_CLOSE` 信号类型 + `signal_publisher.py` 扩展 | 强制平仓 |
| P3.2 | `scheduler.py` 每 15 分钟 H1 趋势反转检测 | 反转检测 |
| P3.3 | `daily_batch.py` 扩展调用 ③（周日 20:00 全周复盘）| 周复盘 |
| P3.4 | **2 周实盘并行对比**（共源模型 vs 原评分引擎，仅 log 不下单） | 实盘验证 |
| P3.5 | 确认胜率衰减 ≤ 5% → 切除原逐信号 AI 调用，全量切换 | 全量上线 |

---

## 8. 风险评估与约束

| 风险 | 等级 | 缓释措施 |
| --- | --- | --- |
| 本地校准模型过拟合 | 中 | 查表型设计（非自由参数模型）、每日 DeepSeek 标注刷新、保留原规则引擎 fallback |
| DeepSeek 批量 Prompt 返回格式不稳定 | 高 | 严格 JSON 输出约束 + `daily_batch.py` 解析容错（字段缺失用旧值兜底）+ 解析失败不阻塞次日运行 |
| DeepSeek 批量调用 Token 超限 | 中 | 回看天数上限 30 天（~40K tokens）+ `co.batch.lookback_days` 上限 60（面板约束）；超出时自动截断并 WARNING 日志 |
| Optuna 与 DeepSeek 写同一批配置键 | 中 | 优先级链（面板 > DeepSeek > Optuna）；Optuna 只写 `hcm_ai.param_history`（source=`optuna`），不直接写 `hcm_config.metadata`；DeepSeek 挑中后才写入配置表 |
| Optuna 建议参数过拟合 | 中 | DeepSeek 二次校验（从 30 组挑 1 组）+ 回测验证 + 面板仍可手动覆盖 |
| 定时任务错过执行（容器重启） | 低 | 启动后检查最后执行时间，补跑（cron-style 追赶） |
| 周末/假日无 K 线更新时调用无效 | 低 | 周日调用 ③ 设计为独立调用；调用 ① 周六自动跳过（检查最后 K 线时间 > 24h） |
| DeepSeek 不可用时质量降级 | 中 | 熔断器已有（复用 `ai_invoker.py`）；失败后沿用昨日参数 + 昨日校准表 |
| FORCE_CLOSE 误触发 | 中 | 3 根 M5 bar 连续确认 + ADX>30 才触发；bridge 侧已有 SL/TP 保护 |
| 与原评分引擎性能下降（Phase 3 全量切换后） | 中 | Phase 3 先并行 2 周（log-only）+ 实测对比后再切 |

### 约束

**以下 4 条为刚性约束（与 §2.3 核心约束保持一致，此处聚焦实现层面）：**

1. **不碰 bridge 栈**：任何信号处理改动都限制在信号塔一侧，bridge 通过 `signal:risk_passed` 统一接收，不单独改造。
2. **禁用硬编码**：本方案涉及的全部参数（校准因子、过滤阈值、门槛、AI 调用时间、Optuna 超参、FORCE_CLOSE 参数等）**必须来自 PG + Redis 双写配置**，代码中不得出现字面量。代码审查时 grep 出任何硬编码值必须修正为配置读取。（对应 §2.3.1）
3. **所有参数 PG + Redis 双写**：任何参数变更必须同时写入 PG `hcm_config.metadata` 和 Redis `hcm:config:v2`。`ConfigProviderV3.set()` 需扩展为原子化双写（PG 先、Redis 后）。PG 为 SoT，Redis 为热层。排查配置必须查 PG，不可只看 Redis。（对应 §2.3.2）
4. **共源信号面板选模型才显示**：前端增加「信号模型」下拉选择器，选「共源信号」才展示 6 组参数卡片（校准因子/假信号过滤/自适应门槛/批量AI/Optuna/执行增强），每组独立颜色区分；选「默认规则引擎」时面板隐藏。面板字段禁止使用英文 config_key 作为标签。（对应 §2.3.3）
5. **向后兼容**：Phase 1 上线时保留原评分引擎作为 fallback，通过配置开关（`signal.active_model` = `default` / `co_source`）切换。
6. **DeepSeek API 稳定性依赖**：共源模型的核心定价依赖于 DeepSeek API 的可用性和稳定性，长期观察，不假设 100% 正常。

---

## 9. 验收标准

### Phase 1a 验收（假信号过滤 + 自适应门槛）

- [ ] F1-F5 假信号过滤规则在回测中命中率 > 20%（至少过滤掉一些噪音信号）
- [ ] 60 天回测中，仅过滤+自适应门槛的胜率 ≥ 原评分引擎 95%
- [ ] 配置开关 `signal.active_model` = `default` 时系统退回原行���
- [ ] **（硬约束 ①）** 代码审查确认：本方案涉及的全部参数均来自 PG 配置，无字面量硬编码
- [ ] **（硬约束 ②）** `ConfigProviderV3.set()` 任意 co.* 键写入后，PG `hcm_config.metadata` 和 Redis `hcm:config:v2` 均有对应键值，且值一致
- [ ] **（硬约束 ③）** 前端信号模型下拉选择「共源信号」后，6 组参数卡片正确展示、颜色区分、保存可读写；选择「默认规则引擎」后面板隐藏

### Phase 1b 验收（校准层，需 ≥ 7 天标注数据）

- [ ] `daily_batch.py` 调用 ① 能成功获取标注结果并写入 `hcm_ai.labeled_samples`
- [ ] `local_calibrator.py` 能从 PG 标注表计算 5 态校准因子并写入 `hcm_config.metadata`
- [ ] 冷启动期（< 7 天标注）校准因子恒为 1.0，≥ 7 天后自动激活
- [ ] 激活校准后追加回测：校准版本胜率 ≥ P1a 版本 98%（允许微小退化，但不应显著劣化）
- [ ] `ConfigProviderV3.set()` 扩展完成：任意键写入后 PG 写成功即 Redis 同步更新（约束 ② 落地）

### Phase 2 验收

- [ ] `optuna_tuner.py` 每日自动产出 30 组参数、写入 `hcm_ai.param_history`
- [ ] DeepSeek 调用 ① 能从 30 组中挑出最稳定 1 组并更新 `hcm_config.metadata`
- [ ] DeepSeek 调用 ② 产出风险系数并写入 Redis，scheduler 能正确读取并调整打分门槛
- [ ] 极端行情补充调用触发正确（ATR 翻倍 / 连续亏损），冷却 2h 有效

### Phase 3 验收

- [ ] `FORCE_CLOSE` 信号在 H1 趋势翻转 + 3 bar 确认 + ADX>30 时正确发布
- [ ] 2 周实盘并行对比中，共源模型胜率衰减 ≤ 5%
- [ ] DeepSeek 月调用量 ≤ 100 次
- [ ] 月 API 成本 ≤ $20
- [ ] 周日周复盘正常执行并输出周报

---

## 10. 术语表

| 术语 | 含义 |
| --- | --- |
| 共源模型 | 本文档提出的双源信号增强架构（本地实时 + DeepSeek 离线） |
| 实时层 | 每根 M5 K 线触发的本地评分 + 过滤（不含 AI 调用） |
| 离线层 | 每日定时触发的 DeepSeek 批量分析（不含实时决策） |
| 优化层 | Optuna 本地参数寻优 |
| 校准因子 | 查表型乘数（0.7~1.3），叠加到规则评分上，由 DeepSeek 标注结果更新 |
| 假信号过滤 | 本地规则（F1-F5）在评分后、阈值门控前执行，剔除无效信号 |
| FORCE_CLOSE | 新增信号类型，由 H1 趋势翻转触发，通知 bridge 平掉当前持仓 |
| 批量分析调用 ① | 每日 06:00 DeepSeek 调用：行情标注 + 参数校验 + 漏洞复盘 |
| 风险预判调用 ② | 每日 18:00 DeepSeek 调用：风险系数（低/中/高） |
| 全周复盘调用 ③ | 每周日 20:00 DeepSeek 调用：整周策略回顾 + 回撤告警 |
| 补充调用 | 极端行情触发的应急 DeepSeek 调用（单日上限 2 次） |
| 标注表 | `hcm_ai.labeled_samples`：DeepSeek 标注的 K 线标签，供校准因子更新 |
| 参数历史表 | `hcm_ai.param_history`：Optuna + DeepSeek 产出的历史参数组 |
| 规则表 | `hcm_ai.gap_rules`：DeepSeek 识别的策略漏洞与补充过滤规则 |
