# LightGBM 运行审计 × 八段设计规范 对照设计报告

> 审计性质：**纯只读源码审计**（不改任何代码/配置/模型）。
> 审计范围：8 个核心文件 + 全仓 PSI/校准/监控 Grep。
> 审计基准时间：2026-08-22。
> 对照对象：用户《LightGBM 设计规范》八段 + 禁止黑名单 + 极简验收标准。

---

## 〇、审计结论速览（先给结论）

| 维度 | 符合度 | 一句话 |
|---|---|---|
| 一、数据准备（时序切分/标签/泄露） | 🟡 部分 | 主切分**时序正确**，但**验证块 shuffle**、**无跳空/消息剔除**、**无样本去重** |
| 二、特征工程（冗余过滤/状态特征/一致性） | 🟡 部分 | 状态特征齐全、线上一致性强（契约 fail-fast），但**无 VIF/相关系数冗余过滤**、重要性只打印不剔除 |
| 三、训练超参（稳健优先） | 🔴 偏差 | **缺 `lambda_l2` 正则**、`min_child_samples=20` 偏低、**无 `bagging_freq`** |
| 四、概率校准（重中之重） | 🟡 部分 | 用 Isotonic + 只用验证集 ✅，但 **clip 是 [0,1] 非 [0.05,0.95]**、无分桶校验报表 |
| 五、推理防御（PSI/离群/二次钳位） | 🔴 缺失 | **无 PSI 漂移检测、无离群检测、无 0.2~0.85 业务二次钳位** |
| 六、与和乘幂耦合 | 🟢 基本符合 | DeepSeek 已解耦、LGBM 权重上限 ≤0.5 ✅；DS 未吸收语义、报表埋点不全 |
| 七、上线监控与复盘 | 🔴 缺失 | **无 PSI/校准/置信分布/行情环境监控报表、无自动重训触发** |
| 八、禁止黑名单 | 🟡 部分 | 多数规避，但 shuffle 验证块、无监控、predict 链路 clip 错仍触线 |

**核心判定**：当前实现"骨架正确、关键防御缺失"。最致命的三处是 **①缺 L2 正则、②校准 clip 错配 [0,1]、③推理端零 PSI/离群/二次钳位防御**——这三点直接违反规范三/四/五，是"模型驯化合格"验收标准 3/4/5 不达标的根因。

---

## 一、数据准备阶段

### 1.1 严格时间序列切分
- ✅ **主切分时序正确**：`train_signal_quality.py:206-208` 按 `np.argsort(created_at)` 排序；`:235-239` `cut=int(n*(1-test_ratio))`，训练=早段、测试=最新段（walk-forward，注释明写"时间序 walk-forward, 无泄漏"）。
- ⚠️ **验证块 shuffle 违反**：`:242-246` 用 `train_test_split(test_size=0.2, stratify=y_tr)` 从训练集切验证块——sklearn 默认 `shuffle=True`，**验证集是随机子集而非"中间时间段"**。早停观测对象（`:297-300` `eval_set=[(Xva_s,yva_s)]`）即此 shuffle 块，不符合"验证集=中间时间段"。
- ✅ 无全局随机 shuffle 打乱 K 线顺序（主流程时序）。

### 1.2 标签定义（未来 N 根盈亏）
- ✅ `build_labels.py` 的 `label_one()` 用**未来 N=12 根 M5 K 线触达 ±1R** 判 win/loss（优于"下一根涨跌"）。

### 1.3 异常样本处理（跳空/重大消息）
- ❌ **缺失**：全仓 Grep 未见 gap/跳空缺口/非农等异常 bar 剔除或降权逻辑。训练特征含极端事件 K 线直接灌入。

### 1.4 样本均衡（震荡降权/趋势提权）
- 🟡 **部分**：`build_labels.py:309-365` 实现了 `ds_calib_weight`——按 **DeepSeek 视角 4 象限**（DS看真/看假 × 价格赢/输）+ continuity 微调，权重钳 [0.3,2.0]。这是**语义维度加权**，但**非规范要求的"按行情环境（震荡/趋势）加权"**。未见按 ATR/通道状态显式区分震荡 vs 趋势样本权重。

### 1.5 杜绝数据泄露
- ✅ **无全局标准化器泄露**：特征工程（`quality_features.py enrich_klines`）全部使用**滚动/相对量**（dev_z、pct、ratio、one-hot、EWM），无 `StandardScaler/MinMaxScaler` 全局对象 → 天然"滚动标准化"，线上推理复用同一函数，无全量统计泄露。
- ✅ 结构因子均 `shift(1)` 防未来价格泄露（已确认）。

### 1.6 样本去重
- ❌ **缺失**：无高度重复 K 线降采样逻辑，模型可能死记历史片段。

---

## 二、特征工程规则

### 2.1 高冗余特征过滤（VIF/相关系数）
- ❌ **缺失**：`_model_feature_cols.py` 41 维 + `quality_scorer.py` 25 维中，**`align_m5` 开启后 `h1_adx` 与 `adx_14` 同为 M5 ADX（近乎重复）**；`plus_di/minus_di` 与 `di_ratio/di_net` 重复；`dev_z_ema20/60/200` 高度相关；`session_*` 三组 one-hot 相关——**全程无 VIF/相关系数过滤**，特征爆炸带来过拟合风险。

### 2.2 市场状态特征
- ✅ **齐全**：`quality_features.py` 含 ATR 波动率（`atr_pct`）、通道位（donchian）、多周期 dev_z 趋势/区间偏离、`macd_slope`、`extreme_reversal`，模型能识别当前环境。

### 2.3 特征重要性筛查
- 🟡 **只观测不剔除**：`train_signal_quality.py:356-357` 打印 `feature_importances_` top15，但**训练完成后未自动剔除极低/不稳定特征**（规范二要求"直接剔除"）。

### 2.4 线上推理预处理 100% 一致
- ✅✅ **强保障**：`quality_scorer.py build_features` 与 `quality_features.py enrich_klines` **同源函数**；且 `train_signal_quality.py:304-314` 有**契约 fail-fast**——模型特征名必须 `== MODEL_FEATURE_COLS`，否则 `raise RuntimeError`。列错位风险被硬性拦截。

### 2.5 禁止未来可获取特征
- ✅ 结构因子 `shift(1)`（已确认），无未来均线/未来价格。

---

## 三、训练超参配置（交易稳健优先）

`train_signal_quality.py:285-290` 实际参数：

| 超参 | 当前值 | 规范建议 | 判定 |
|---|---|---|---|
| `objective` | `binary` | `binary` | ✅ |
| `eval_metric` | `auc` | 优先 `auc` | ✅ |
| `num_leaves` | `15` | 20-40（偏大易过拟合） | ✅ 偏保守 |
| `min_child_samples` | `20` | ≥30-40 | ⚠️ **偏低**，记噪声风险 |
| `lambda_l2` | **未设置** | ≥1.0 | 🔴 **缺失**，权重无 L2 约束 |
| `lambda_l1` | 未设置 | 少量 | ⚠️ 缺失 |
| `learning_rate` | `0.05` | 0.03-0.06 | ✅ |
| `n_estimators` | `300` + `early_stopping(50)` | 早停定树数 | ✅ |
| `subsample` | `0.8` | 0.7-0.85 | ✅ |
| `bagging_freq` | **未设置** | 3-5 | ⚠️ 缺失（subsample 每轮生效，非间隔） |
| `colsample_bytree` | `0.8` | 0.6-0.75 | ⚠️ 略高 |
| `scale_pos_weight` | 自动算 | 处理类不平衡 | ✅ |
| 早停观测对象 | 验证块（shuffle） | 时间序验证集 | ⚠️ 见 1.1 |

**核心偏差**：**缺 `lambda_l2`**（规范三明文要求 ≥1.0）+ `min_child_samples` 偏低 + 无 `bagging_freq`，三叠加 → 小样本下过拟合倾向明显（raw AUC=1.0 即证据）。

---

## 四、概率校准（重中之重）

- ✅ **校准器只用验证集**：`:317` `fit_calibrator_safe(model, Xva_s, yva_s)`（验证块，非训练集）✅ 符合"严禁训练集"。
- ✅ **校准器类型正确**：`:139/162` `IsotonicRegression`（规范优先 isotonic）✅。
- 🔴 **数值钳位错配**：`:139/162` `IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)` → clip 到 **[0,1]**，规范明确要求 **clip(0.05, 0.95)**。当前配置允许输出 0/1.0 极端概率，直接违反规范四。
- 🟡 **分桶校验弱**：`:331-341` 有"分位阈值重锚"打印各分位胜率，但**无标准 reliability diagram / 分桶→真实胜率校验报表**（如 0.6 分桶真实胜率是否≈60%）。
- ✅ **校准退化护栏**：`:144-162` `fit_calibrator_safe` 小样本退化回退原始概率（额外稳健性，好）。
- ✅ **校准器随模型保存/加载**：`calib_final.pkl` 线上加载（已确认）。

---

## 五、推理阶段防御（当前最大缺口）

Grep 全仓（`PSI|drift|monitor|Isotonic|calibrat|分桶`）结果：**无任何 PSI 特征漂移检测实现**（仅有 `archive/backtest_*.py` 的价格 drift 变量、`hcm-market-intel` 流动性监控、`shared/metrics.py` 指标直方图桶，均与模型特征 PSI 无关）。

- 🔴 **PSI 特征漂移检测（PSI>0.25）**：**完全缺失**。行情风格切换无报警信号 → 验收标准 4 不达标。
- 🔴 **离群样本检测（远离训练空间压低置信）**：**缺失**。罕见行情/跳空无防御 → 验收标准 3（罕见行情不输出 0.9+）无保障。
- 🔴 **二次钳位 0.2~0.85**：**缺失**。推理侧 `quality_scorer.py` 最终 `ai_score` 走"状态内分位重锚"，且 calib clip 为 [0,1]，**无业务层 0.2~0.85 钳位** → 可能输出极端满分/0 分。
- 🟡 **推理失败/特征缺失降级**：✅ `quality_scorer.py` 有 try/except，`ai_score` 取不到时回退 `scorecard_total`（部分满足规范五降级要求）。

---

## 六、与和乘幂系统耦合规则

- ✅ **DeepSeek 自由数字分不直加 LGBM**：`ai_async_client.py:249-266` 已**解耦（2026-08-18）**，运行期 `c_ai` 单源取 LightGBM（`:299`），DeepSeek 不直接改分。
- 🟡 **DS 输出转特征喂入 LGBM**：实现为**离线训练期权重**（`build_labels.py ds_calib_weight` → `sample_weight`），而非规范字面"运行期转特征"。且 `train_signal_quality.py:223-232` 诊断：**DeepSeek 特征在训练集 92%+ 为缺省 0.0** → 模型实质未吸收 DS 语义（需积累 `ai:ds:out`` 后再重训）。
- ✅ **LGBM 置信分权重上限**：`quality_gate.py` `coupling_weight` 硬上限——HEXP 始终 ≥0.5，LGBM ≤0.5，不允许覆盖主信号。
- 🟡 **报表埋点**：`inference_log` 埋 `ai_score/total_score/features/snapshot`，但**无 PSI 字段、无原始分与校准分分离埋点、无最终交易结果回填**。

---

## 七、上线监控与定期复盘

- 🔴 **监控报表缺失**：无 PSI 漂移、校准分桶、置信分布统计、行情环境分布（震荡/趋势占比）的专门监控落库/报表。
- 🔴 **自动重训触发条件缺失**：无"PSI 长期>0.25 / 校准分桶持续错位 / 风格切换连续失效"的自动触发（`auto_retrain.py` 存在但非规范要求的自动条件触发）。
- ✅ **重训练校准器同步重训**：每次 `train_signal_quality.py` 都重训校准器（`:317`），不沿用旧校准器 ✅。
- 🟡 **旧模型灰度对比**：仅有手动 `.bak` 备份惯例（部署时做的），**无系统性 A/B 灰度对比框架**。

---

## 八、禁止行为黑名单对照

| 黑名单项 | 当前状态 |
|---|---|
| ❌ predict_proba 原生输出当置信分 | 🟡 部分规避（有校准+分位重锚），但 calib clip[0,1] 仍触线 |
| ❌ shuffle 打乱时序 | 🔴 **验证块 shuffle**（1.1 已证） |
| ❌ 用训练集训练校准器 | ✅ 规避（验证块） |
| ❌ 线上与训练预处理不一致 | ✅ 规避（契约 fail-fast + 同源） |
| ❌ 盲目调大 num_leaves/去正则刷 AUC | ✅ 规避（num_leaves=15 保守） |
| ❌ 上线后永不监控 | 🔴 部分违反（有推理日志无专门监控） |
| ❌ LLM 自由数字分直加 LGBM | ✅ 规避（解耦） |

---

## 九、极简验收标准判定

1. 🔴 **离线 vs 时间外测试差距大**：raw AUC=1.0（完美过拟合），calib AUC=0.92，测试集胜率仅 1.4% → 离线极高、外测崩盘倾向，违反验收 1。
2. 🟡 **校准分桶单调对齐**：有分位胜率打印但未系统性校验，待补 reliability 报表。
3. 🔴 **罕见行情不输出 0.9+**：无 PSI/离群检测 → 无法保证。
4. 🔴 **PSI 监控报警**：完全缺失。
5. 🔴 **不大量两极分化**：calib clip[0,1] + 分位重锚，可能仍输出极端 0/100。

---

## 十、修复路线图（分阶段，待确认才改）

> 按"先止血、后加固、再监控"顺序。每步需复述需求+你明确同意才落地（铁律前置闸门）。

**P0 止血（最小改动、最高性价比）**
- T1: 校准器 `y_min/y_max` 由 [0,1] 改为 **[0.05, 0.95]**（`train_signal_quality.py:139/162`）。
- T2: 训练超参加 **`lambda_l2=1.0` + `lambda_l1=0.1`**，**`min_child_samples` 20→30**，补 **`bagging_freq=5`**（`train_signal_quality.py:285-290`）。
- T3: 验证块改用**时序切分**（取训练段后 20% 时间，禁用 shuffle）（`train_signal_quality.py:242-246`）。

**P1 加固（推理防御）**
- T4: 推理侧加 **PSI 特征漂移检测**（存训练集特征分布统计，每轮算 PSI，>0.25 衰减/放弃）。
- T5: 加 **离群检测**（Mahalanobis/分位）压低罕见行情置信。
- T6: 业务层 **二次钳位 [0.2, 0.85]**（`quality_scorer.py` 输出前）。

**P2 数据与特征治理**
- T7: 标签加 **跳空/重大消息 bar 剔除或降权**（`build_labels.py label_one`）。
- T8: **VIF/相关系数冗余过滤** + 低重要性特征自动剔除。
- T9: **样本去重**降采样。

**P3 监控与复盘**
- T10: 监控报表落库（PSI/校准分桶/置信分布/行情环境分布）。
- T11: 自动重训触发条件 + 灰度对比框架。

---

## 附：审计证据文件清单

| 文件 | 角色 | 关键行 |
|---|---|---|
| `tools/train_signal_quality.py` | 训练主脚本 | 206-208/235-246/285-302/317/331-341/356-357 |
| `tools/quality_scorer.py` | 推理 sidecar | build_features / score_one / 输出分位重锚 |
| `tools/quality_features.py` | 特征工程 | enrich_klines（滚动/相对量、shift(1)） |
| `tools/build_labels.py` | 标签 | label_one（未来N根±1R）、309-365（ds_calib_weight） |
| `tools/_model_feature_cols.py` | 特征列契约 | 41 维定义 |
| `signal_tower/quality_gate.py` | 耦合层 | coupling_weight（HEXP≥0.5） |
| `signal_tower/ai_async_client.py` | DeepSeek 解耦 | 249-266/299 |
| 全仓 Grep | PSI/校准/监控 | 无 PSI 实现（仅无关 drift/liquidity/metrics） |
