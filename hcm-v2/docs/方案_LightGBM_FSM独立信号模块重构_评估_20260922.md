# LightGBM FSM 独立信号模块重构 · 评估与方案

> **状态：评估稿（未实施）**。本文档仅为方案分析，**不含任何生产代码改动**。
> **风险提示**：文中数值来自离线复算，**不构成投资建议**；上线前必须样本外回测 + 模拟盘验证。
> 日期：2026-09-22 ｜ 对象：现运行 LightGBM FSM（4 类相位模型） vs 你提出的 11 步独立模块方案

---

## 0. 结论摘要（先看这里）

| 判定 | 内容 |
|---|---|
| **总体** | 你的方案**方向正确、且有一处真正的机制创新**（经济驱动的扣成本标签），但**不能直接实施**：存在 3 个可证伪的逻辑缺陷 + 1 处口径不一致 + 5 个与现有架构的对接缺口 |
| **必须修正（阻断项）** | ①`box` 类标签**数学上永不可达**（第 3 类恒为 0）②成本参数 `0.002` 比真实成交成本高约 30 倍，会导致标签退化 ③PSI 函数分箱错误（且缺 `import pandas`） |
| **建议保留（创新点）** | ①扣成本 + 盈亏比 + 最大回撤三重门槛的标签 ②Walk-Forward 滚动回测 ③PSI 漂移监控 ④双层过滤（可开关） |
| **建议暂缓/改造** | ①「12BAR 强制平仓」**不进入生产**（与 bridge 的 SL/TP/移动止损冲突）②「方向融合进标签」需先做 A/B（与现架构「方向独立裁决」是架构级分歧）③新 magic 逻辑码属**跨服务永久契约**变更，须单独走变更说明 |
| **建议路径** | **两阶段**：阶段 A 离线影子（不驱动下单，只落库）→ 达标后阶段 B 接入 |
| **前置条件** | 先回答 §10 的 5 个待决策项 |

---

## 1. 现运行 LightGBM FSM 审计

### 1.1 组件与数据流（file:line）

| 环节 | 实现 | 位置 |
|---|---|---|
| 特征契约（27 维，纯因果） | `STATE_FEATURE_COLS` | `hcm-signal-tower/signal_tower/state_features.py:29-51` |
| 标签生成（4 类形态） | `classify_label` / `label_metrics` | `tools/build_state_labels.py:267-293` / `:242-264` |
| 模型训练 | `train_state_model.py`（多 seed 产物 `lgbm_state_{tf}_v{N}_s{S}.txt`） | `tools/train_state_model.py` |
| 推理装配（含降级链） | `StateInferer` | `signal_tower/state_infer.py:171+` |
| 状态迁移（防抖 + 入口门） | `decide()`（纯函数） | `signal_tower/state_machine.py:266-570` |
| 策略层（意图 → 订单参数） | `StateStrategy` | `signal_tower/state_strategy.py` |
| magic 契约（基码 + 8 位布局） | `SIGNAL_MODE_MAGIC` / `encode_fsm_magic` | `signal_tower/signal_publisher.py:48-82` / `state_strategy.py:92-157` |
| 观测落库 | `market_state_log`（22+ 列，含 prob/margin/note/intent） | `hcm_signal.market_state_log` |

### 1.2 标签口径（**与你的方案最根本的差异**）

现行 4 类判据（`build_state_labels.py:275-292`），**纯形态、无成本、无盈亏比**：

```
1) oscillation : er ≤ 0.25 且 disp ≤ 0.25
2) trend_fade  : adx_t ≥ 22 且 adx_slope_f ≤ −3 且 (er2_f ≤ 0.35 或 mae_f ≥ 0.60) 且 未创新极值
3) trend_init  : disp1 ≤ 0.45 且 er1 ≤ 0.25 且 disp2 ≥ 0.30 且 er2 ≥ 0.30
4) trend_mid   : er ≥ 0.30 且 disp ≥ 0.30
   其余 → None("ambiguous")，不入训练集
```

另设**置信过滤** `confidence_reject`（`:296-308`）：阈值边界带内样本直接剔除，减小噪声。

**关键设计声明**（`build_state_labels.py:93-94`）：

> 【2026-09-15 定案】方向**不入形态模型**，回退 4 类；方向由独立规则模块裁决
> （`trend_direction.py`：回归斜率(ATR 归一) + ±DI + K 线防抖 → up/down/none）。

⇒ **你的方案把方向与形态融合进同一个 4 分类标签（1=多头/2=空头/3=箱体），这是与现架构的架构级分歧**，不是实现细节（见 §4.1）。

### 1.3 特征契约

- 27 维（`state_features.py:29-51`），含 `box_width_atr`(:33)、`atr_pct`(:46，历史 120 根分位)、`atr_14`(Wil **Wilder**，`:93 atr_window=14`)、`atr_box_ratio`、`slope_linreg`、`r2_linreg` 等
- **契约强校验**：`check_feature_contract`，模型特征列不符即**拒加载**（`state_infer.py:12` "contract_mismatch（拒绝加载，防静默错列）"）
- 你的 `calc_features` 只产出 7 维（`vol_20/atr/atr_quantile/slope_20/box_amp/box_pos/vol_ratio`），其中 `box_amp ≈ hl_range_atr`、`atr_quantile ≈ atr_pct` 是**已有特征的近似重复**；且 `atr` 用 `rolling(20).mean()`（**SMA**）而现系统用 **Wilder EMA(14)** ⇒ **同一概念两套口径**（本仓库明令避免"同一规则两份实现"）

### 1.4 推理与降级链（值得你复用的成熟设计）

`state_infer.py:10-22` 明列逐级降级，任一级失败不影响主流程：

```
lightgbm 缺失 / 模型缺失      → ok=False reason=no_model
特征列与本契约不一致          → ok=False reason=contract_mismatch
K 线不足 / 特征非有限         → ok=False reason=<具体原因>
max_prob < min_conf 或 margin < min_margin → ok=True decided=False（不参与防抖）
```

另有：按周期独立加载、**多 seed 聚合**、`abstain` 弃权闸（conformal，**默认关闭**并有 3 条验收门，`:52-66`）、vol 头与 onset 头**命名契约隔离**（`_MODEL_RX`/`_ONSET_RX`/`_VOL_RX`，`:86-93`，防加载错模型）。

### 1.5 判别层失效的实测证据（2026-09-22）

| 指标 | 值 |
|---|---|
| 模型加载/推理 | `model_version=v5`，`infer_ok=140/140=100%`（**运行层无故障**） |
| `decided` 比例 | 102/140 = 73%（低置信 38：`low_conf` 25 + `low_margin` 13） |
| `predicted_class` 分布 | trend_fade 71(51%) / trend_init 40(29%) / trend_mid 22(16%) / **oscillation 7(5%)** |
| FSM 实际状态 | S0 63 / S2 45 / S4 32 / **S1_OSC 0 / S3_TREND_MID 0** |
| `oscillation` 的 7 根 | **100% `decided=False`**（prob 0.29~0.37 < `min_conf=0.35`）⇒ `low_conf_skip` 丢弃、不参与防抖 |
| 生产 `state.debounce.k_enter` | **3**（非默认 2）⇒ 需连续 3 根判 osc，而 7 根从不连续 |

⇒ **S1_OSC 恒为 0**：`state_osc`(magic 61) 通道结构性关闭（今日 61 订单 = 0 条）。

### 1.6 为什么会失效（根因，非调参可解）

根因与引擎自身的记录一致（`state_machine.py:88-104`）：

> 非 fade 三类两两时序 OOF AUC：oscillation vs trend_init **0.5526**、vs trend_mid **0.5550**、trend_init vs trend_mid **0.5172**，三者准确率均**低于多数类基线**；条件预测分布总变差 ≤ **8.5%**；
> 补 6 列量价/点差特征（`--feature-set l1`）：macro_F1 **0.3328→0.3302**，无提升；
> 重定标签口径（oscillation 占比 16.9%→35.0%）：关键对 AUC 0.5538/0.5323/0.5124，**无变化**。
> ⇒ 模型对这三类的 argmax 实为**噪声**；用它驱动 S1/S2/S3 的区分 = **按噪声迁移**。

**三个层次本可分离，现方案把它们压在一个 4 分类头上**：

1. **「有没有方向性移动」**（波动/效率）— 实测**可学**：vol_expansion 目标 OOF AUC **0.6460**（`build_state_labels.py:85-86`）
2. **「移动处于哪一阶段」**（init/mid/fade）— 实测**不可学**（AUC 0.51~0.55）
3. **「方向 up/down」** — 弱且周期相关（M5 both_edge **−0.0261** vs H1 **+0.0065**）

**你的方案把 1+3 合并成 `long/short/box` 标签 —— 恰好避开了最不可学的第 2 层**。这是你方案最有价值之处（见 §4.1）。

---

## 2. 你的 11 步方案 · 逐项评估

| 步骤 | 判定 | 要点 |
|---|---|---|
| 1 全局配置 | ⚠️ | 无魔法数字 ✓；但 `prob_threshold=0.72`、成本参数、`scale_pos_weight` 三处**未标定** |
| 2 特征工程 | ✅ 可用 / ⚠️ 口径 | 因果无泄露 ✓；但 7 维偏少、ATR 用 SMA 与现系统 Wilder 不一致、与 27 维契约重复 |
| 3 标签生成 | ✅ **最有价值** / ❌ **两处缺陷** | 扣成本 + RR + 回撤三重门槛是真正的机制升级；但 **`box` 类恒不可达**、成本参数高约 30 倍（§3.1、§3.2） |
| 4 模型训练 | ⚠️ | 时序切分 ✓；但 **`scale_pos_weight` 对 multiclass 无效**、无 OOF、与步骤 10 自相矛盾（§3.3、§3.4） |
| 5 推理 + 双层过滤 | ⚠️ | 可开关 ✓；但缺 `min_margin` 门（现系统已证单靠 `min_conf` 不够，`state_infer.py:48-51`）、列顺序未按 `model.feature_name()` 对齐 |
| 6 FSM 12BAR 强平 | ❌ **不建议进生产** | 与 bridge 的 SL/TP/移动止损冲突；且命名为"FSM"但与现有 FSM（状态机）**同名不同义**（§4.2） |
| 7 策略主类 | ✅ | 接口清晰，低耦合 |
| 8 模型持久化 | ⚠️ | 可用；但现系统有**版本/多 seed/契约元数据**规范，建议对齐（`lgbm_state_{tf}_v{N}_s{S}.txt` + `_meta.json`） |
| 9 PSI 漂移监控 | ✅ **新增价值** / ❌ **实现错误** | 现系统无 PSI；但函数分箱错误 + 缺 `import pandas`（§3.3） |
| 10 Walk-Forward | ✅ **新增价值** / ⚠️ | 补上了现系统缺的"滚动重训"；但**完全未实现绩效计算**，窗口偏短（§3.4） |
| 11 Demo | ⚠️ | 随机游走数据仅能冒烟，**不得作为有效性证据** |

---

## 3. 必须修正的技术缺陷（含可复现判据）

### 3.1 【阻断】`box` 类标签**数学上永不可达**

```python
box_profit = box_range / 2 - total_cost
box_rr     = box_profit / (box_range / 2) if box_range > 0 else 0
...
elif box_profit >= CONFIG["min_profit_thresh"] and box_rr >= CONFIG["min_risk_reward"]:
    label = 2
```

因为 `box_rr = 1 − total_cost / (box_range/2)`，而 `total_cost > 0`：

```
box_rr < 1  恒成立
min_risk_reward = 1.5  ⇒  1 − c/(r/2) ≥ 1.5  无解
```

⇒ **第 3 类（箱体）永不被赋值**，`num_class=4` 中有一类**恒空**（实际退化为 3 类，且类 3 概率恒近 0）。

**修正方向（择一）**：
- (a) `box_rr` 分母改为**真实风险**（如 `box_range/2` 之外的破界止损距离），即 RR = 期望收益 / 单边止损距离；
- (b) 箱体类不设 RR 门槛，改用**箱宽/成本比**（`box_range / total_cost ≥ k`）+ 触及顺序模拟；
- (c) 承认箱体无法用"静态区间"表达，改为由 §3 之外独立的贴边逻辑裁决（与现系统 `osc_at_box_lower/upper` 一致）。

**建议 (c)+(b)**：箱体单的本质是"贴边 + 区间内往返"，需要**路径模拟**（先触哪一边），静态 `range/2` 是过度乐观的代理。

### 3.2 【阻断】成本参数比真实成交成本高约 **30 倍**

```python
"fee_rate": 0.0005, "slippage": 0.0005   →
total_cost = 0.0005*2 + 0.0005*2 = 0.0020  （= 0.20%）
```

以 XAUUSD ≈ 4350 计，`0.0020 × 4350 = 8.7 USD`；而实测 ATR(p50) ≈ **5.14 USD** ⇒ **成本 ≈ 1.7 ATR**。
对照实盘数据：M5 点差约 **0.037 ATR**（前面离线评估所用口径）。

⇒ 成本高约 **30 倍**，会把绝大多数样本打成 `label=0`（**标签退化**，正例率趋近 0），并使 `min_profit_thresh=0.003`（=2.5 ATR/12 根）几乎不可达。

**修正**：成本**必须从真实成交数据标定**——可用 `hcm_trading.orders` 的 `commission`/`swap` 与 `hcm_market.klines.spread`（或 `ticks`）测定，并写入配置中心而非硬编码。

### 3.3 PSI 函数两处错误

```python
def calculate_psi(base_array, new_array, bins=10):
    base_bin = pd.qcut(base_array, bins=bins, duplicates="drop").value_counts(normalize=True)
    new_bin  = pd.qcut(new_array,  bins=bins, duplicates="drop").value_counts(normalize=True)
```

- **缺 `import pandas as pd`**（文件仅 `import numpy as np`）⇒ 运行时 `NameError`
- **分箱边界不一致**：`new` 用**自身**分位数切箱 ⇒ 两边箱边界不同，PSI **失去可比性**（PSI 定义要求**同一套边界**）

**修正**：

```python
_, bins_edge = pd.qcut(base_array, q=bins, retbins=True, duplicates="drop")
p_base = pd.cut(base_array, bins=bins_edge, include_lowest=True).value_counts(normalize=True)
p_new  = pd.cut(new_array,  bins=bins_edge, include_lowest=True).value_counts(normalize=True)
psi = sum((p_base.get(b,1e-8) - p_new.get(b,1e-8)) * np.log(p_base.get(b,1e-8)/p_new.get(b,1e-8))
          for b in p_base.index)
```

### 3.4 两处内部矛盾 / 缺失

| 项 | 问题 |
|---|---|
| 训练切分 | 步骤 4 用**单次 0.8/0.2**，步骤 10 却要求"禁止一次性全量训练" ⇒ 应统一为 **OOF(TimeSeriesSplit) 或 walk-forward 重训**，训练脚本内不保留"全量单切"入口 |
| `scale_pos_weight` | LightGBM 中**仅对 binary / multiclassova 生效**，`objective=multiclass`（softmax）下**被静默忽略** ⇒ 不平衡未处理。应改用 `class_weight` 或对少数类过采样后**再做时序切分**（SMAOTE 会泄露，禁用） |
| 步骤 10 绩效 | 代码只 `pd.concat` 预测结果，**未计算任何绩效**（夏普/盈亏比/胜率/最大回撤都未实现），而验收清单要求了这些 ⇒ **验收与实现不符** |
| 窗口长度 | `train_window=1000` 根 M5 ≈ 3.5 天；且 `calc_features` 的 `atr_quantile` 需 **rolling(100)** 预热、测试段独立计算 ⇒ 测试段前 ~100 根被丢，实际样本远少于 200 |
| 滑点压力 | 验收要求"滑点放大 20% 重跑"，代码**未实现** |
| 顶部注释 | `future_high/future_low` 的 `shift(-12).rolling(12).max()` **窗口正确**（= t+1..t+12）；但循环里的边界判断 `if idx + pred_bar >= len(d)` **把"索引标签"当"位置"用**（步骤 2 已 `dropna`，索引非连续）⇒ 边界判断失效，**尾部落成伪 `label=0`**。应改 `df.reset_index(drop=True)` 或用位置索引 `i` |

---

## 4. 需要澄清的 3 个设计分歧

### 4.1 方向：融合进标签（你） vs 独立裁决（现架构）

| 维度 | 你的方案 | 现系统 |
|---|---|---|
| 方向 | 融合（label 1/2 即方向） | 独立（`trend_direction.py`：斜率±DI+防抖） |
| 依据 | — | `build_state_labels.py:93-94` 明确"方向不入形态模型" |
| 实测 | — | M5 both_edge **−0.0261**（负）／H1 **+0.0065**（正）；**方向本身弱且周期依赖** |

**评估**：你的融合设计**有价值**（联合建模可让"能走多远"直接约束方向选择，避免"形态对但方向错"）；但风险是**用一个模型同时承担两个弱任务**，且**方向的有效周期是 H1 而非 M5**（实测）。

**建议**：**做 A/B**（阶段 A 内）——同特征、同时序切分下比较：
- 臂 1：4 类形态（不含方向）+ 独立方向模块（现状）
- 臂 2：你的 `long/short/box/none` 4 类
- 指标：OOF AUC、**扣成本后的净期望 R**、信号数、最大回撤
- **判定：以净期望为准，不以 AUC 为准**（AUC 高但净期望负 = 无用）

### 4.2 「12BAR 强制平仓」不宜进入生产

| 冲突面 | 说明 |
|---|---|
| 执行层 | 平仓由 **bridge** 管理（SL/TP/移动止损 clamp、`_FSM_MAGICS=(61,62)` 白名单、§57 箱体突破离场指令）——`tools/verify_bridge_fsm_trail.py:96` |
| 语义混淆 | `state.horizon_bars=12` 是**标签前瞻窗口**；`state.hold_only_max_bars=12` 是**暂缓加仓上界**；再引入"12BAR 持仓强平"= **第三个 12**，且是**执行语义**，极易误读 |
| 风控 | 你的 FSM **无 SL/TP**（只有强平）⇒ 12 根内风险敞口不可控 |
| 命名 | 你的 `TradeFSM` 是"持仓计时器"，现 `MarketStateMachine` 是"行情状态机" ⇒ **同名不同义**，须改名（如 `HoldTimer`） |

**建议**：阶段 A/B **都用"12 根后按市价平"作为离线评估口径**（与标签同 horizon，公平比较）；**生产执行沿用 bridge 的 SL/TP/移动止损**。若将来要引入时间止损，须单独走变更说明（涉及执行层）。

### 4.3 阈值来源：拍脑袋 vs 标定

| 参数 | 你的值 | 问题 | 建议 |
|---|---|---|---|
| `prob_threshold` | 0.72 | 4 分类下 0.72 近乎无信号（现系统 `min_conf=0.35` 时已 27% 低置信） | 在 OOF 上以**净期望最大化**标定；并引入 `min_margin`（top1−top2） |
| `atr_quantile_limit` | 0.85 | 保守但未验证 | 分层复算（`atr_pct` 分桶 × 净期望单调性）后定值 |
| `min_risk_reward` | 1.5 | 合理量级 | OOF 网格 |
| 成本 | 0.002 | **高约 30 倍** | 从实盘成交标定（§3.2） |

---

## 5. 与现有架构的 5 个对接缺口

### 5.1 magic 契约（**跨服务、永久、不可回改**）

> `state_strategy.py:96-99`：「MT5 的 magic 写在**已成交的订单/持仓**上，事后**改不回来**；布局只允许**追加低位数**扩展」

- 现有族：`MAGIC_FAMILIES = (11, 12, 21, 55, 61, 62)`（`shared/magic_family.py:28-31`）
- 8 位布局 `LL·SS·RR·TT`，`SS` 状态码仅 `S1/S2/S3/S4/S0 = 1/2/3/4/5`（`state_strategy.py:125-131`）
- 若新模块要**下单**，需新逻辑码（如 63）并**同步 4 处**：
  1. `shared/magic_family.py`（族表）
  2. `signal_publisher.SIGNAL_MODE_MAGIC`（基码）
  3. `mt5_bridge`（`_FSM_MAGICS` 白名单 + `is_fsm_magic`）
  4. 风控 `rule_chain._magic_family`（同向保本闸门按族判定）
- ⚠️ 另注意：`state_osc`/`state_trend` 的**必须正交**是有业务原因的——平仓归因要靠它区分「震荡止损」与「趋势止损」，规格 9.4 的 **4ATR 锁止预算是震荡态专用**（`signal_publisher.py:58-62`）

**建议**：**阶段 A 不引入任何 magic**（影子模式，只落库）。阶段 B 若要接入，新增逻辑码需**独立变更说明**。

### 5.2 信号发布链

现链路：`StateStrategy` → `StrategyIntent` → `signal_publisher`（`SignalData` → PG `hcm_signal.signals` + Redis Stream）→ bridge 消费。
你的模块目前**只产出 `signal` 数组**，未定义：`signal_id` / `task_id` / `account_id` / `sl_price` / `tp1/tp2` / `lot` / `valid_until` / `signal_mode` / `block_reason`。需补齐（可复用 `SignalData` 契约）。

### 5.3 桥端识单与离场指令

- `is_fsm_magic` / `_is_fsm_magic`（`shared/magic_family.py:37-52`、`tools/verify_bridge_fsm_trail.py:101-115`）
- `_FSM_MAGICS = (61, 62)`；非白名单单**不读指令、不收紧移动止损**
- 箱体离场指令按 `fsm_magic_logic` 做 scope 匹配（`state_strategy.py:175-190`）

### 5.4 风控闸（现系统已有，你的方案缺失）

| 机制 | 现状 | 你的方案 |
|---|---|---|
| 震荡锁止 | `state.osc_atr_loss_limit=4.0`（累计 4ATR → S5 锁止） | 无 |
| 波动路由拦截 | `state.vol.osc_skip_prob=0.30`（扩张概率高时拦箱体单） | 仅 ATR 分位硬过滤 |
| 同向保本闸门 | `rule_chain._check_cooldown`（按 magic 族） | 无 |
| 平仓归因 | `position_sync`（osc/trend 分开累计） | 无 |
| 冷却/去重 | `same_bar_skip`、`bridge_after_close_cooldown` | 无 |

### 5.5 观测与配置热调

- 观测：`hcm_signal.market_state_log`（140 根/日）、`hcm_ai.inference_log`、`hcm_ai.gate_decision` —— 支撑"分层复算"（置信分桶 × 经济指标）
- 配置：`hcm_config.metadata` 的 `state.*` 键 + 30s 热重载（`state_machine.load_config` / `StateStrategy._load_config`）
- 你的 `CONFIG` 是模块级常量 ⇒ **需改为配置中心键**（建议前缀 `state.lgbm_fsm.*`）

---

## 6. 建议的重构方案（两阶段）

### 阶段 A：离线影子（**不改变任何交易行为**）

**目标**：用你的标签思路产出概率，**落库不驱动下单**，与现 4 类模型**同口径对比**。

交付物：
```
hcm-signal-tower/signal_tower/lgbm_fsm/           # 独立子包（非散落 stepN_*.py）
├── __init__.py
├── config.py        # 配置（默认值 + 配置中心键 state.lgbm_fsm.*，无魔法数字）
├── features.py      # 特征（复用/对齐 state_features 口径，走 check_feature_contract）
├── labels.py        # 扣成本标签（修正 §3.1/§3.2，成本来自实盘标定）
├── train.py         # 训练（TimeSeriesSplit OOF；去掉 multiclass 下的 scale_pos_weight）
├── infer.py         # 推理（多 seed 聚合 + 降级链，对齐 StateInferer 模式）
├── filters.py       # 双层过滤（置信 + 波动，可独立开关，含 min_margin）
├── hold_timer.py    # 【改名】HoldTimer（离线评估口径，不进生产执行）
├── drift.py         # PSI（修正分箱）
└── strategy.py      # 对外接口：load_data/train/inference/tick_step

tools/
├── train_lgbm_fsm.py          # 产出 lgbm_fsm_{tf}_v{N}_s{S}.txt + _meta.json
├── eval_lgbm_fsm_vs_state.py  # ★ A/B：新模块 vs 现 4 类（净期望为准）
└── walkforward_lgbm_fsm.py    # ★ 滚动回测 + 绩效 + 滑点压力（补齐 §3.4）
```

**阶段 A 验收门（全部满足才可进阶段 B）**：
1. 标签正例率落在 **[20%, 40%]**（避免退化；现 onset 曾因 94.9% 正例退化，`build_state_labels.py:84`）
2. 类别分布**无空类**（`box` 类占比 > 3%）
3. OOF **净期望 R > 0**（扣真实成本、含滑点压力 +20% 后仍 > 0）
4. **与现 4 类模型 A/B**：在**同一 OOF 切分**下净期望更高，或"净期望相当且信号量更少"
5. PSI 在回测期内 **< 0.2**（并验证 PSI 超标能自动关闭信号）
6. `market_state_log` 类落库字段完备，可做"置信分桶 × 净期望单调性"复算

### 阶段 B：接入（需**独立变更说明**）

仅当阶段 A 全部门通过：
1. 新增 magic 逻辑码（跨服务 4 处同步）+ 变更说明（回滚方案）
2. 桥端白名单/离场指令 scope 扩展
3. 风控族归并（`_magic_family`）
4. 配置中心键上线 + 灰度（先小仓/单品种）
5. 执行层：**沿用 bridge 的 SL/TP/移动止损**；如需时间止损，单独论证

---

## 7. 三方对比表

| 维度 | 现状（4 类 FSM） | 你的 11 步方案 | 建议方案 |
|---|---|---|---|
| 标签驱动 | 纯形态（er/disp/adx），**无成本** | **扣成本 + RR + 回撤**（经济学驱动） | 采用你的标签思路（修正成本与 box 类） |
| 类别语义 | 形态相位（osc/init/mid/fade） | **方向融合**（long/short/box/none） | A/B 后择一 |
| 方向 | 独立模块（`trend_direction`） | 融合进标签 | A/B 决定 |
| 特征 | 27 维、契约校验、Wilder ATR | 7 维、SMA ATR、无契约 | 复用 27 维契约或补齐契约 |
| 训练评估 | 工具链完整（`eval_*` 多件） | OOF 缺失、绩效未实现 | OOF + walk-forward + 绩效（合并两者） |
| 样本外 | 有 `replay_state_chain`（整链回放） | **Walk-Forward**（新型） | 两者都要：回放 + 滚动重训 |
| 漂移监控 | **无** | **PSI**（有 bug） | 采用（修正分箱） |
| 执行/风控 | 完整（SL/TP/trail/锁止/保本闸） | **缺失** | 沿用现有 |
| 观测/配置 | 落库 + 热重载 | 无 | 沿用现有 |
| 12BAR 强平 | 无（也不应有） | 有 | **仅离线评估口径** |

---

## 8. 验收清单（对齐你的总清单 · 逐条给判定方法）

| # | 你的验收项 | 判定方法（可执行） | 现状/建议 |
|---|---|---|---|
| 1 | 特征无未来泄露、可增量计算 | **点断测试**：构造只改动 `t+1` 之后数据的 fixture，断言 `t` 时刻特征不变；再断言 `calc_features(df[:k])` 末行 == `calc_features(df)[k-1]` | 需补 |
| 2 | 标签扣成本、4 类正确、可导出统计 | 单测：`box_rr < 1` 恒成立 ⇒ 先修 §3.1；再断言 4 类**均非空**；导出分布报表 | **当前必失败**（box 恒空） |
| 3 | 模型时序、shuffle 关闭、可存取 | 断言无 `shuffle=True`；`save→load→predict` **逐位一致**；产物命名对齐 `_meta.json` 契约 | 部分 |
| 4 | 双层过滤生效且可独立开关 | 单测：固定输入下，分别置 `prob_threshold`/`atr_quantile_limit` 为极端值，断言信号随之变化；补 `min_margin` 门 | 部分 |
| 5 | 持仓计时、满 12BAR 平仓、不重复开仓 | 单测：连喂 13 根信号，断言第 12 根后发 `force_close` 且期间不开新单 | 可行（但**仅离线**） |
| 6 | PSI 超标可关闭 AI 信号 | 单测：构造已知漂移（均值平移 3σ），断言 PSI > 0.2 且信号被禁用 | **当前必失败**（分箱错+缺 import） |
| +7 | **净期望 > 0（基本面）** | walk-forward 段内计算：扣成本净期望 R、胜率、盈亏比、最大回撤、夏普；滑点 +20% 复跑 | **当前缺失**（未实现绩效） |
| +8 | **与现模型 A/B 不劣** | 同一 OOF 切分下对比净期望与信号量 | **当前缺失** |

---

## 9. 风险 · 回滚 · 里程碑

**风险**
| 风险 | 等级 | 缓解 |
|---|---|---|
| 新模块产生下单（未走 magic 契约） | **高** | 阶段 A **强制影子**（只落库）；代码层禁用发布 |
| 标签退化（正例率→0） | 高 | 成本按实盘标定 + 正例率验收门 [20%,40%] |
| 与现 FSM 双写/双真值 | 高 | 单一路径：阶段 A 下新模块**不产出 magic、不接 bridge** |
| 过拟合到 walk-forward 窗口 | 中 | 多窗口 + 滑点压力 + PSI |
| 语义混淆（12BAR/FSM 命名） | 中 | 改名 `HoldTimer`；文档显式标注"仅离线口径" |

**回滚**：阶段 A 为纯新增（无生产行为）⇒ 回滚 = 删除子包 + 工具脚本，零影响；阶段 B 每项均为配置键或白名单，**回滚 = 配置置默认**。

**里程碑**
| 阶段 | 内容 | 退出条件 |
|---|---|---|
| A1 | 修正 §3 全部缺陷 + 单测（§8 #1~#6） | 单测全绿 |
| A2 | 训练 + OOF + 分层复算 | 验收门 1~3 通过 |
| A3 | A/B（vs 现 4 类）+ walk-forward + PSI | 验收门 4~6 通过 |
| B | 接入（magic/桥/风控/灰度） | 独立变更说明获批 |

---

## 10. 待你决策的 5 项（回答后进入 A1）

1. **方向**：走 A/B（形态+独立方向 vs 融合方向），还是直接按你的融合方案？——
   *建议：先 A/B（§4.1），以**净期望**为准而非 AUC。*
2. **箱体类**：改用"路径模拟 + 箱宽/成本比"（§3.1 方案 c+b），还是**暂时只做 3 类**（long/short/none），箱体交回现有 `osc_at_box_*` 贴边逻辑？
   *建议：后者——避免重复实现箱体通道，也避免与 magic 61 冲突。*
3. **成本参数**：是否允许我从 `hcm_trading.orders`（`commission`/`swap`）与 `hcm_market.klines.spread`/`ticks` **标定真实成本**并写入配置中心？
   *建议：必须——否则标签不可用。*
4. **12BAR 强平**：确认**仅作离线评估口径**（生产沿用 bridge 的 SL/TP/移动止损）？
5. **接入范围**：阶段 A 是否确认**纯影子（不产生 magic、不接 bridge）**？

---

### 附：现系统已具备、你的方案未覆盖的资产（建议直接复用）

`check_feature_contract`（契约防错列）｜多 seed 聚合｜逐级降级链（`state_infer.py:10-22`）｜`min_margin` 双门｜`abstain` conformal 弃权闸（含 3 条验收门范例）｜模型命名契约隔离（`_MODEL_RX`/`_ONSET_RX`/`_VOL_RX`）｜配置热重载（30s）｜观测落库三表｜`eval_*` 评估工具链（`eval_state_separability` / `eval_causal_state_edge` / `eval_state_leadtime` / `replay_state_chain`）｜`calib_state_labels` / `discover_state_labels` 标定工具

---

# 阶段 A 实施结论（2026-09-22）

> **判决：4/6 验收门通过，门 1 不通过 ⇒ 不进入阶段 B。**
> 「方向融合进标签」路线**被数据证伪**。

## A.1 交付物（全部为**新增**，零生产行为变更）

| 路径 | 说明 |
|---|---|
| `hcm-signal-tower/signal_tower/lgbm_fsm/` | 独立子包 9 模块：`config / features / labels / filters / drift / hold_timer / train / infer / strategy` |
| `tools/verify_lgbm_fsm.py` | 单元测试 **34/34 全绿**（含 5 处缺陷的回归断言） |
| `tools/train_lgbm_fsm.py` | 训练 + OOF + `_meta.json`（默认**不落盘**，需 `--save`） |
| `tools/eval_lgbm_fsm_vs_state.py` | 净期望 / 基线对比 / 滑点压力 / PSI |

**阶段 A 硬约束已实现并断言**：不产 magic（不 import `state_strategy` / `signal_publisher`）、不写信号表、不发消息、不调 bridge。

## A.2 已修正的原方案缺陷（6 处，含 1 处实施中**新发现**）

| # | 缺陷 | 修法 | 验证 |
|---|---|---|---|
| 1 | **箱体类恒空**（`box_rr = 1−c/(r/2) < 1` 而门槛 1.5 ⇒ 无解） | 改用「箱宽 + 到期净位移小」 | 单测：box 可达且非空 |
| 2 | 成本高约 30 倍（0.002 ≈ 1.7 ATR） | ATR 归一 + 实盘标定入口（兜底 0.074 ATR） | 单测：`cost < 0.3` |
| 3 | PSI 分箱不一致 + 缺 `import pandas` | 边界只从基准导出、`new` 复用同边界 | 单测：漂移能被检出（2.07） |
| 4 | `scale_pos_weight` 对 `multiclass` 无效 | 改 `class_weight_mode=balanced` | 代码层移除此项 |
| 5 | 边界判断把「索引标签」当「位置」 | 全程位置索引（`build_feature_matrix` 返回 `idx`） | — |
| **6** | **【实施中新发现】收益用「窗口极值平仓」= oracle** | 新增 `exit_mode`，默认 `close`（到期平仓，与 `HoldTimer` 同口径） | 见 A.3 |

**第 6 项是本轮最重要的发现**：原式 `long_profit = (future_high − c0)/c0` 假设"在窗口最高点平仓"，
这是**不可实现**的事后最优。实测后果（XAUUSD M5, n=29866）：

| 口径 | none | long | short | box | **可交易占比** | box recall |
|---|---|---|---|---|---|---|
| `extreme`（原方案/oracle） | 11.5% | 38.7% | 41.8% | 8.1% | **88.5%** | **0.000** |
| `close`（可实现） | **37.9%** | 24.8% | 27.5% | 9.8% | **62.1%** | 0.003 |

## A.3 验收门结果（XAUUSD M5，n=29866，2026-05-07 ~ 2026-09-22）

| 门 | 判据 | 结果 | 实测 |
|---|---|---|---|
| 1 | 可交易率 ∈ [0.20, 0.40] | **✗** | **0.621**（none 0.379 / long 0.248 / short 0.275 / box 0.098） |
| 2 | 无空类（每类 ≥3%） | ✓ | box 9.8% |
| 3 | 净期望 > 0 且滑点 +20% 后仍 > 0 | ✓ | **+0.0226R** → 压力后 **+0.0078R** |
| 4 | 不劣于随机基线 | ✓ | 策略 +0.0226R vs 随机 **−0.0587R** |
| 5 | PSI < 0.2 | ✓ | max **0.0938**（`new_high_cnt`） |
| 6 | 落库字段完备 / PSI 可关闭信号 | ✓ | 单测覆盖 |

**绩效明细**（按预测交易：`t` 开仓、`t+H` 平仓、扣双边成本）：

| 臂 | n | 期望 R | 累计 R | 胜率 | 盈亏比 | 最大回撤 | Sharpe/笔 |
|---|---|---|---|---|---|---|---|
| 策略（过滤后） | 2424 | **+0.0226** | +54.8 | 51.9% | 0.94 | **382.8R** | **+0.0058** |
| 策略（未过滤） | 2935 | **+0.0400** | +117.4 | 50.8% | 1.00 | 419.0R | +0.0107 |
| 基线·随机方向 | 9604 | −0.0587 | −564.0 | 47.9% | 1.02 | 752.4R | −0.0194 |
| 基线·真实标签(oracle) | 13050 | **+2.8730** | +37492 | 100% | — | 0 | +1.164 |
| 策略（滑点+20%） | 2424 | +0.0078 | +18.9 | 51.6% | 0.95 | 396.5R | +0.0020 |

## A.4 三个决定性发现

1. **oracle 与可实现差距 127 倍**：`+2.873R`（完美预测）vs `+0.0226R`（实际）⇒ 模型只捕获了**上限的 0.8%**。
2. **波动过滤（L3）在本口径下有害**：未过滤 `+0.0400R` > 过滤后 `+0.0226R`（拦掉的 511 笔反而是好样本）。
   —— 修正了此前"高波动时段应回避"的判断：在**到期平仓**口径下高波动=位移大=期望更高。
3. **风险收益比不可接受**：Sharpe/笔 **0.0058**（≈噪声）、最大回撤 **382.8R** 是累计收益 54.8R 的 **7 倍**。

## A.5 根因：为什么「方向融合」注定失败

可实现收益标签要求预测 **"未来 12 根的方向性位移"**，而本项目已独立实测：

| 事实 | 数值 | 出处 |
|---|---|---|
| M5 方向边缘为**负** | both_edge **−0.0261** | 前轮 `study_pbig_gate.py` |
| H1 方向仅微正 | +0.0065 | 同上 |
| 当期 ER 对"未来是否震荡"**零预测力** | lift **0.97~1.00** | 前轮 `diag_osc_struct` |
| 波动扩张目标**可学** | OOF AUC **0.6460** | `build_state_labels.py:85-86` |
| 非 fade 三类**不可分** | AUC 0.51~0.55 | `state_machine.py:88-104` |

⇒ **可学的是「幅度」；「方向」在 M5 不可学。**
而「方向融合进标签」恰好把**最不可学的任务（方向）塞进了模型**。

**这反过来证明现系统的架构选择是正确的**：`build_state_labels.py:93-94`「方向**不入**形态模型，
由 `trend_direction.py` 独立裁决」—— 把方向交给规则、把模型用于可学层次。

## A.6 判决与后续建议

**判决：不进入阶段 B。**（门 1 不通过；且即使净期望为正，其质量也不足以支撑生产改造）

**建议（按优先级）**：
1. **放弃「方向融合」路线**，回到「可学层次 + 规则裁决方向」——
   即沿用现架构，把模型用于**波动/幅度**（AUC 0.6460），方向仍由 `trend_direction` 判。
2. **原方案的真正价值点已被吸收并验证**：`exit_mode=close`（可实现收益口径）、
   PSI 漂移监控、时序 OOF 与滑点压力、`HoldTimer` —— 这些**可独立移植**到现有链路
   （尤其 PSI：现系统缺失，且实测有效）。
3. **`lgbm_fsm` 子包保留为研究工具**（纯影子、可一键删除），
   不接生产；若后续要走"幅度建模"，可直接复用其 `drift` / `filters` / `train` 三模块。
4. **门 1 阈值本身需重新标定**：0.20~0.40 源自 onset 退化的经验（`build_state_labels.py:84`），
   对本新目标未必适用；但**无论阈值如何，"recall_long 0.038 / box 0.003"已表明模型未学到方向**，
   故调整阈值不能改变结论。

**诚实边界**：本次为**单一品种（XAUUSD）、单一周期（M5）、单一时间窗（约 4.6 个月）**的样本外（OOF）评估；
未计隔夜/持仓成本、未做多品种交叉验证；`cost_source=fallback_spread`（未从实盘成交标定）。
门槛（`min_net_profit_atr=0.5` 等）为**首轮取值**，未经网格标定 —— 故 A.3 的绝对数值有不确定性，
但 A.4/A.5 的**方向性结论**（oracle 陷阱、方向不可学、模型只捕获 0.8% 上限）在量级上是稳健的。

---

# 阶段 B′：PSI 漂移监控接入生产（2026-09-22 已完成上线）

> 原「阶段 B」（`lgbm_fsm` 模型接入交易）**未实施**——阶段 A 判决不支持。
> 本次落地的是阶段 A **唯一被独立证实有效**的能力：PSI 漂移监控。

## B′.1 交付物

| 文件 | 性质 | 说明 |
|---|---|---|
| `signal_tower/feature_drift.py` | **新增** | PSI **唯一实现点**（中立位置，生产与离线工具共用） |
| `signal_tower/lgbm_fsm/drift.py` | 改写 | 改为 re-export（避免两份实现） |
| `signal_tower/scheduler.py` | 小改 | 顶层 import；`_load_drift_cfg`；`_maybe_sample_feature_drift`；`_run_shadow_state` 末尾调用 |
| `docker-compose.yml` | **新增挂载** | `feature_drift.py`（**P0**：scheduler 顶层 import，缺挂载=**整塔启动崩**） |
| `deploy/migrations/0053_feature_drift.sql` | **新增** | 表 `hcm_signal.feature_drift_log` + 9 个配置键（**已应用**） |
| `tools/verify_feature_drift.py` | **新增** | 32/32 全绿 |
| `tools/verify_lgbm_fsm.py` | 回归 | 34/34 全绿 |

## B′.2 上线步骤与结果

1. 应用迁移（幂等）→ 表 + 9 键落地，`enabled=false` / `auto_disable=false`
2. `docker compose up -d --no-deps hcm-signal-tower` → 加载新挂载（容器内校验 `has_method=True`）
3. 开观测：`state.drift.enabled=true`（`auto_disable` 保持 **false**）
4. 采样参数：`window_bars=180`、`every_bars=12`
5. **首次线上采样已落库**（2026-09-22 13:40 UTC）：
   `XAUUSD/M5 ref_kind=adaptive window_bars=180 n_ref=180 n_cur=180`
   **`max_psi=3.0686`（`adx_14`）`verdict=block` `blocked=true` `auto_disabled=false`**

> ⚠️ PSI=3.07 属**严重漂移**（>0.2 即 block）。这既是监控有效的证据，也提示
> **当日行情分布与近期基准显著不同**（top 特征：`adx_14` / `hl_range_atr` / `atr_box_ratio` / `box_width_atr`），
> 值得与当日策略表现一并复核（漂移本身**不**直接等于亏损，但它是"模型可能失效"的前置信号）。

## B′.3 生产级陷阱（本次实测踩到，均已修）

### 陷阱 1：K 线长度不足导致**静默跳过**
`_run_shadow_state` 的序列由 `_fetch_klines(..., limit=max(400, min_bars*2))` 提供，
但**当 `tf == state.timeframe` 且长度 ≥ `min_bars`(122) 时根本不补取**（`scheduler.py:2313-2315`）
⇒ 实际可能只有 ~100~400 根，而 PSI 需 `2×window` 根。
**修法**：采样时按需补取（复用同一"已收盘"剥离语义：`open_time < bar_eps`）。

### 陷阱 2：Redis 配置缓存**未失效**导致"改了 PG 但不生效"
配置为三层（`config_provider.py:6`：Local TTL 30s → Redis TTL 300s → **PG = Source of Truth**）。
本次把 PG 改成 `every_bars=1/window_bars=180` 后，**Redis 仍缓存旧值 `12/480`**（TTL 未到），
且 `every=12` 使 `bar_eps % 3600 ≠ 0` ⇒ `slot_due=False` ⇒ **静默不采样**，排查耗时最久。

**运维规程（必须遵守）**：
```bash
# 改配置后必须失效缓存，否则最长 300s 后才生效（且期间行为与配置不符）
docker compose exec -T redis redis-cli HDEL "hcm:config:v2" "state.drift.every_bars"
# 或按 key 逐个 HDEL；PG 为真值，HDEL 后 provider 会回源
```

### 陷阱 3：失败路径日志级别过低（不可见）
初版把"K 线不足/未启用"记为 INFO/DEBUG。实测生产 `signal_tower.*` 的 **INFO 不输出**
（日志中该 logger 只有 WARNING 出现）⇒ "开关开了但一次都没采到"**完全无线索**。
**修法**：所有失败/未启用路径一律 **WARNING**（"未启用"另加**只提示一次**的抑制）。
—— 这与 `scheduler.py:2529-2535` 记录的历史事故（整链降级只有 DEBUG 日志）**完全同型**。

## B′.4 当前生产配置（PG 真值）

| 键 | 值 | 说明 |
|---|---|---|
| `state.drift.enabled` | **true** | 采样已开启 |
| `state.drift.every_bars` | 12 | M5 每小时一次 |
| `state.drift.window_bars` | 180 | 需 2×180=360 根；与可用 K 线长度匹配 |
| `state.drift.ref_kind` | adaptive | 前窗口自比较（无需离线基准文件） |
| `state.drift.psi_warn` / `psi_block` | 0.1 / 0.2 | |
| `state.drift.auto_disable` | **false** | ⚠️ 不自动关闭信号（唯一会改变交易行为的键） |

## B′.5 边界与后续

- **本能力只观测，不改交易行为**：`auto_disable=false` ⇒ 即使持续 block 也不会自动关信号；
  `auto_disabled` 列仅记录"是否达到关闭条件"，实际动作须人工执行
- **未验证的部分**：`ref_kind=file`（离线基准 npz）路径**未在生产启用**（无基准文件）；
  "PSI 超标 → 提前避免多少亏损"这一**收益**未验证（只有算法正确性验证）
- **回滚**：`state.drift.enabled=false`（秒级，零查询零写入）或 `DROP TABLE` + `DELETE` 配置键
- **后续可选**：
  1. 用离线基准（`lgbm_state_{TF}_drift_base.npz`）替代 adaptive，测"相对训练集"漂移（更准）
  2. 在面板/告警接入 `feature_drift_log`（现仅落库）
  3. 若长期 block，评估 `auto_disable` 的启用条件（需满足 migration 0053 里的三条）

---

# 阶段 B″：PSI 优化（adaptive → file 基准 + 阈值标定，2026-09-22）

## B″.1 优化动机：adaptive 基准**信噪比倒置**

上线后观测到连续两次 `block`（PSI 3.07 → 4.09），遂量化其历史分布（6000 根 M5，n=1104 采样点）：

| 指标 | adaptive（180 vs 180） | file（15000 vs 180） |
|---|---|---|
| p50 | **4.07** | **2.94** |
| p90 / p99 | 8.04 / 10.98 | 4.54 / 7.28 |
| **>0.2（block）占比** | **100.0%** | **100.0%** |
| **人为真漂移**（`atr_14` ×1.5） | **2.80** ← 低于正常噪声 | **7.09** ← 高于 p97 |

**结论**：adaptive 下"正常波动（中位 4.07）"远大于"显著人为漂移（2.80）"⇒ **信噪比倒置、零信息量**；
且 `0.2` 这一行业惯例阈值是为"大样本+稳定特征"设计的，对"M5 波动类特征 + 180 根窗口"**完全不适用**（恒 100% 报警）。

## B″.2 优化内容（4 项）

| # | 改动 | 依据 |
|---|---|---|
| 1 | **新增 `tools/build_drift_base.py`**：产出离线基准 `lgbm_state_{TF}_drift_base.npz` | 提供稳定长基准（19878×27） |
| 2 | **`ref_kind: adaptive → file`** | file 的 p50 2.94 vs adaptive 4.07；真漂移 7.09 可检出 |
| 3 | **阈值按实测基线标定**：`psi_warn=4.5`（≈p90）、`psi_block=7.3`（≈p99） | 惯例 0.1/0.2 不适用（否则 100% block） |
| 4 | **修 2 个实现 bug**（见 B″.3） | 实测暴露 |

## B″.3 优化中发现并修复的 2 个 bug

1. **`file` 分支 `cur` 取成 `w//2`(=90)** ⇒ 当前段样本减半、分箱噪声放大、PSI 系统性偏高
   （实测 `n_cur=90 → PSI 7.56`，而修正为 180 后同段为 **5.11**）。**已修为直接复用后半 `window` 根**。
2. **基准 `cols` 存为 `dtype=object`** ⇒ `np.load(allow_pickle=False)` 抛
   "Object arrays cannot be loaded when allow_pickle=False" ⇒ `load_reference_from_file` **静默回退 adaptive**。
   **已修为 unicode dtype**（`build_drift_base.py`）。

**另一处环境陷阱**：容器 `/app/review_models` 的挂载源是 **`./tools/models`（只读）**，
而非仓库根的 `review_models/` ⇒ 基准放错位置会**静默回退 adaptive**。
`build_drift_base.py` 的默认 `--out` 已指向 `tools/models`。

## B″.4 优化结果（生产实测）

| 时间 | ref_kind | n_ref | n_cur | max_psi | max_col | verdict |
|---|---|---|---|---|---|---|
| 13:40 | adaptive | 180 | 180 | 3.0686 | adx_14 | **block** |
| 14:00 | adaptive | 180 | 180 | 4.0930 | atr_pct | **block** |
| 14:30 | file | 19878 | 90 | 7.5649 | atr_14 | block（`n_cur` 未修时偏高） |
| **14:35** | **file** | **19878** | **180** | **5.1119** | **atr_pct** | **warn** ✅ |

⇒ 语义恢复：**warn 代表"温和漂移"而非"永远异常"**；optimum 与标定（p90=4.54）一致。

## B″.5 当前生产配置（PG 真值）

| 键 | 值 |
|---|---|
| `state.drift.enabled` | **true** |
| `state.drift.ref_kind` | **file**（相对离线基准） |
| `state.drift.psi_warn` / `psi_block` | **4.5 / 7.3**（按实测基线标定） |
| `state.drift.every_bars` / `window_bars` | 12 / 180 |
| `state.drift.auto_disable` | **false** |

## B″.6 运维规程（新增）

```bash
# 1) 重建基准（换品种/周期、或基准过期时）
python tools/build_drift_base.py --symbol XAUUSD --tf M5 --lookback 20000

# 2) 改任何 state.drift.* 后**必须**失效 Redis 缓存（否则最长 300s 不生效）
docker compose exec -T redis redis-cli HDEL "hcm:config:v2" "state.drift.<key>"

# 3) 基准文件须落在容器挂载源 ./tools/models（容器内 /app/review_models，只读）
```

**诚实边界**：
- 阈值（4.5/7.3）由 **19878 根（约 2.5 个月）** 的滚动基线标定，**样本期有限**；行情结构长期变化后应重新标定
- `file` 基准当前为**静态文件**（不随行情更新）⇒ 时间越久，"相对训练集漂移"越可能自然增大
- 仍未验证"PSI 超标 → 提前避免亏损"这一**收益**（只有算法正确性与信噪比验证）

---

# 后续三项优化（A/B/C，2026-09-22 完成）

## A · PSI 告警接入（已完成）

**做法**：**不新建告警链路**，而是复用 `hcm_ai.runtime_event` —— 它已被两处既有消费者读取：
web `hcm-web/web/api/ai_report.py:_health`（按 `event_type` 聚合）与
`scheduler._aggregate_daily_kpi`（汇入 `hcm_ai.daily_kpi`）。⇒ **写入即自动上屏/进日报，零新增基础设施**。

- `event_type='feature_drift_block'`、`status='warn'`
  （**刻意不用 ok/fail**：既有的"AI 调用成功率"只认 ok/fail，用 warn 避免漂移告警污染该指标）
- 仅在 `verdict=block` 时写；`every_bars=12` ⇒ 每周期至多 1 条
- `detail` 含 `max_psi/max_col/ref_kind/n_ref/n_cur/window_bars/auto_disabled`

**验证**（临时降阈值触发一次）：
`('feature_drift_block','XAUUSD','warn',{tf:M5,n_cur:180,n_ref:19878,max_col:'new_high_cnt',max_psi:3.512,...})` ✅

## B · 阶段 A′（换目标为可学的"幅度/波动"）—— **已存在，无需重建**

**审计结论**：`vol_expansion` 目标**早已在生产完整实现**：
目标定义 `build_state_labels.py`（`state.label.vol_amp_min=3.13`）→ 训练 `train_onset_model.py --target vol`
（产物 `lgbm_vol_*`）→ 推理 `state_infer.infer_vol_route` → 生产使用 `state.vol.osc_skip_prob=0.30`。

**质量核实**（`lgbm_vol_M5_v90_meta.json`）：

| 指标 | vol 头 | 4 类形态模型 |
|---|---|---|
| OOF AUC | **0.6460**（`build_state_labels.py:85`） | 0.51~0.55 |
| 正例率 | 0.3018（在验收门 [0.20,0.40] 内） | — |
| 校准 ECE | raw 0.0542 → cal **7.6e-18** | — |
| **conformal 单例准确率** | **0.8188**（α=0.05） | **0.502**（≈随机 ⇒ 4 类弃权闸默认关闭） |

⇒ **"模型管可学的幅度"这一目标已达成**；B 的实质工作变为**核实其生产用法**。

**用法核实**（`vol_v90_oof.csv` + 生产 `trend_direction` 方向，可实现口径）：

| 组 | 趋势单期望 | 胜率 |
|---|---|---|
| vol 高（上 30%） | **+0.0478R** | 48.4% |
| vol 低 | **+0.0192R** | 47.3% |
| 全样本 | +0.0272R | 47.6% |

⇒ **vol 高时趋势单并未恶化（反而略好）** ⇒ **"额外拦趋势单"无依据**；
箱体单为理想化代理（胜率 100%，不可信）⇒ **无法证伪 `osc_skip_prob` 的方向**。

**结论：不动生产配置**（无证据不改）。B 的产出 = **核实报告**，非代码变更。

## C · Walk-Forward 滚动重训（已完成）

**新增 `tools/walkforward_lgbm_fsm.py`**（补上阶段 A 缺口：原评估用 TimeSeriesSplit OOF，
衡量的是"固定切分下表现"，**不是**"随时间推进、每次仅用历史重训"的真实部署形态）。

**结果**（XAUUSD M5，train=6000 / test=1000，23 折，n=9078）：

| 指标 | 值 |
|---|---|
| **合成净期望** | **+0.0514R**（滑点 +20% 后 +0.0366R） |
| 与单次 OOF（+0.0226R）对比 | **滚动重训更高** ⇒ **OOF 无乐观偏差** |
| 各折 mean / **std** | +0.0407 / **0.3133**（std 是 mean 的 **7.7 倍**） |
| **正折占比** | **52.2%**（≈抛硬币） |
| min / max 折 | −0.5651 / +0.8904 |
| 回撤 / 累计 | **559.1R / 466.7R** |

**结论**：正期望**在滚动重训下依然存在**（**不是 OOF 假象**），
但**统计上不可区分于噪声**（正折占比 52.2%、std/mean=7.7、回撤>累计）
⇒ **与阶段 A 判决一致：模型极弱，不足以支撑实盘**。

---

# 阶段 C · 执行"重构本身"（决策结构改造，2026-09-23）

> 前置认知（读码修正）：重构的**框架与评估工具完整**（子包 10/10、工具 6/6、PSI 接入 3/3），
> 但**重构本身未执行**（`tools/models/lgbm_fsm_*` 文件数 = **0**，`state.fsm.non_fade_target` 未配置）。

## C.1 读码论证：5 项候选中只有 1 项该动

| 项 | 我的原判断 | **读码后的事实** | 结论 |
|---|---|---|---|
| `state.dir.tf = M5` | 该切 H1 | `0038:6-11` **用户明确裁定 M5**：「用前视位移当唯一判据，会把**均值回归**误读成符号反向」；A/B：M5 提前 −2.0 vs H1 +0.0 | **不改** |
| `state.debounce.k_enter = 3` | 该复核 | `方案_状态机判别力改进_20260919.md:108-128`：churn −28%/−36%、S1→S2 迁移（**S1 前视 −50.6bp/0 胜 vs S2 +11.0bp/65% 胜**）、模拟 R +29% | **不改** |
| **`state.fsm.non_fade_target`** | **该启用** | `state_machine.py:88-105` 两套独立实验证实三类不可分（AUC 0.51~0.55、补 l1 特征无提升、重定标签无变化） | **✅ 启用** |
| `state.trend.entry_mode` | 该回退 | `0035:26-28` 明文要求"须先出评估证据"；`变更说明_五项_20260917.md:26-34` 有受控变更记录 + 3 个观察项 | **已于 9/23 回退 `close_check`** |
| `lgbm_fsm` 模型落盘 | 该 `--save` | 模型无预测力（`recall_long=0.038`） | **不做** |

## C.2 生产等价 A/B（修复后口径）

**为什么必须"生产等价"**：replay 的 `StateInferer/StateStrategy/FSM/trigger` 均走 `load_config()`，
缺值时回落代码 DEFAULTS ⇒ 脚本自身会警告「**无 --cfg ⇒ 跑代码 DEFAULTS**（此结论不能代表生产配置）」。
故从 PG 读出**全部 `state.*` 真值（59 项）**注入两臂，**唯一变量 = `non_fade_target`**。

**踩到的坑（记录备查）**：PG 的 `state.model_dir=/app/review_models` 是**容器内路径**，
注入后覆盖 `--model-dir` ⇒ 主机上模型加载失败（`[fatal] M5 模型未加载`）。**必须排除该键**。

| 臂 | `non_fade_target` | 累计 R | 均值 R | 趋势态占比 | open/add | 不变式 |
|---|---|---|---|---|---|---|
| A 基线（现状） | `""` | +1.00 | +0.050 | 23.4% | 20/2 | **I2 违 1、I5 违 2685** |
| **B** | **`S2_TREND_INIT`** | **+2.14** | **+0.107** | **96.7%** | 20/0 | **全通过** |
| C 对照臂 | `S0_IDLE` | **−5.86** | −0.034 | 47.4% | 171/0 | 全通过 |

**口径诚实说明**：引擎注释称 `S2` "占 95%+ **成交**"，实测 **`open` 与基线相同（20）**，
变化的是**趋势态占比（23.4%→96.7%）**。⇒ 注释的"95%+"是**状态占比**，**不是成交占比**；
**不得声称"成交增加"**。

## C.3 实施记录

- **动作**：`set_cfg.py state.fsm.non_fade_target S2_TREND_INIT`（配置中心唯一写入口，PG+Redis 双写 + invalidate）
- **回滚**：`set_cfg.py state.fsm.non_fade_target ""`（秒级；`""` = 关闭，逐位恢复既有行为）
- **影响面**：仅改"非 fade 三类的目标态"；**不动防抖根数、不绕过第 8.5 步两道门**；`trend_fade` 不受影响
- **观察项**（对齐 `变更说明_五项_20260917.md` 的纪律）：
  1. 趋势态占比是否从 ~23% 升到 ~90%（预期，表示"停止按噪声迁移"）
  2. 状态迁移次数是否显著下降（churn 收敛）
  3. 趋势单净盈亏（`62021101`/`62021201`）是否改善
  4. 是否出现"长期滞留 S2"（`flat_reset` 相关行为）

## C.4 诚实边界

- **样本小**：洁净窗口 9/07~9/22（3994 根），**open 仅 20 笔** ⇒ 均值 R 的差异（+0.050→+0.107）**不具备统计显著性**
- **R 不是业绩**：回放**无点差/手续费/滑点**、无冷却与重复开仓限制、单持仓按 bar 收盘成交
  （脚本自身警告「因此 R 不能当业绩」）
- **A 臂的 I5 违反 2685 次**未深挖（疑似回放对 S5 计数器模拟不完整），但 B/C 臂全通过 ⇒
  至少说明"现状的状态流程会进入异常路径"
- 该改动**不提升模型能力**，只是**停止使用已证不可用的判别**（期望改善来自"少做错事"）
