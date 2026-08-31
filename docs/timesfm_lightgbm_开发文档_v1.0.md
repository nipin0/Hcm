# TimesFM + LightGBM 最优融合架构 开发文档 v1.0

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0（草案，待评审） |
| 编制日期 | 2026-08-29 |
| 依据规范 | `【精简正式版】TimesFM+LightGBM_最优融合架构开发&验收规范.txt`（下称《规范》） |
| 参考模型 | https://huggingface.co/google/timesfm-2.5-200m-pytorch |
| 文档状态 | **待确认** —— 本文档为变更说明前置件，未经确认不得改动任何生产代码 |
| 适用系统 | HCM-V2 量化交易系统（乘幂/HEXP 信号 + LightGBM AI 评分链路） |

> **编制纪律**：本文所有"现状"结论均取自真实代码审计并标注 `file:line`；凡《规范》只给名称未给公式的（如 5 类时序特征），本文给出**显式可执行定义并标注〔提案·待确认〕**，禁止任何一方脑补落地。

---

## 0. 摘要（一页看懂）

引入 Google TimesFM 时序基础模型**离线**为每根 K 线抽取时序表征，PCA 降维后与现有手工/HP 因子拼接，喂给**四头 LightGBM**。

- **TimesFM 全程离线**：每日收盘后批量跑，绝不进线上推理链路、绝不产信号。
- **LightGBM 仍是唯一线上决策与置信输出**。
- **收益预期**：PCA 时序特征为主增益（质量头），多周期共振助方向头。
- **最大风险**：现有标注样本仅约 440 条、正样本约 16%，新增 ≤20 维后总维度达 56+，**过拟合风险高于增益预期**；且存在 4 项 P0 前置阻塞（见 §2.6、§15）。

---

## 1. 目标与范围

### 1.1 目标

1. TimesFM 离线产出**时序特征向量**（PCA 降维 + 5 类结构化特征），入库供训练/推理使用。
2. 四头 LightGBM 按《规范》第 3 节重新划分特征并**独立训练/校准**。
3. 建立**样本外回测**能力（当前缺失，见 §2.6 P1-2），按《规范》7.2/7.3 量化验收。

### 1.2 范围内 / 范围外

**范围内**：
- 离线特征提取器（新文件 `tools/timesfm_features.py`）
- 特征存储（PG 新表）+ 在线读取降级
- 特征契约 `MODEL_FEATURE_COLS` 扩展
- 四头训练改造（含新增**卖点头**）
- 回测器（新文件）
- 灰度与回滚

**范围外（明确不动，遵守《规范》5.2 与铁律边界锁定）**：
- 线上缓存机制、打分耦合逻辑、DeepSeek 复核链路、风控执行逻辑 —— **零改动**
- hexp（乘幂）信号方向裁决 —— **禁止改动**（铁律五·红线 1）
- 桥执行/跟单链路 —— 不改动

### 1.3 术语

| 术语 | 含义 |
|---|---|
| HP 因子 | Hand-crafted/HEXP 原生因子，指引擎侧乘幂系统产出的 `hp_score/dir_sum/verdict` 等 |
| 四头 | 方向头 / 质量头 / 买点头 / 卖点头，四个独立 LightGBM 模型 |
| TSS | TimeSeriesSplit，时序滚动交叉验证（禁止 shuffle） |
| PCA 时序特征 | TimesFM 原始 embedding 经 PCA 降维后的分量 |

---

## 2. 现状审计（基线）

> 审计基准：commit `41b00e2`（v1.1.0）。结论均取自源码，非推断。

### 2.1 现有管线总览

```
build_labels.py   →  labels.csv（标签）
quality_features.py → features.csv（特征）
train_signal_quality.py → 四头模型 + 校准器 + feature_baseline.json
quality_scorer.py（sidecar）→ 实时 build_features → 模型推理 → Redis hcm:live:hexp:ai:{sym}
auto_retrain.py   →  自动重训守护
```

### 2.2 特征契约（权威，39 维）

权威来源：`hcm-v2/tools/_model_feature_cols.py:MODEL_FEATURE_COLS`（该文件当前 **已从磁盘丢失**，详见 §2.6 P0-1，内容取自 commit `41b00e2`）。

```python
MODEL_FEATURE_COLS = [  # 39 维
  # --- 市场状态（33 维）---
  adx_14, rsi_14, macd, atr_14, plus_di, minus_di, er, bbw, bbw_pct, hurst,
  mm, ema20_dist_atr, body_ratio, pullback_depth, atr_pct,
  spread_num, spread_atr,
  donchian_q, dev_z_ema20, dev_z_ema60, dev_z_ema200,
  macd_slope3, body_wick_ratio, extreme_reversal,
  di_ratio, di_net, close_mom_atr, trend_aligned,
  session_eu, session_us, event_proximity_min,
  macro_risk_score, sentiment_risk_score,
  # --- DeepSeek 异步票（3 维）---
  ds_fake_prob, ds_sl_coeff, ds_continuity,
  # --- 入场质量（3 维）---
  r_dist_atr, sl_mult_used, entry_atr_ratio,
]
```

- 训练侧强制 `reindex` 到该契约，缺失补 0；模型 `feature_name()` 与之不符即 `raise`（`train_signal_quality.py:443-452`）。
- 推理侧 `quality_scorer.build_features` 产出同序列。
- ⚠️ **该 docstring 自称"33 维"已过时**（实际 39 个，ds_* 3 维与入场质量 3 维后加未同步注释）。**注释不可信，以列表为准**。

### 2.3 四头现状与《规范》差异（关键 Gap）

现有 4 个头（`train_signal_quality.py`）：

| 现有头 | 产物 | 标签 | 位置 |
|---|---|---|---|
| 质量头 | `lgbm_quality_v*.txt` + `calib_*.pkl` | `label`（±R 先触，horizon 12 根 M5） | `:404-558` |
| 方向头 | `lgbm_direction_v{ver}.txt` + `calib_dir_np_v{ver}.pkl` | `dir_label`（±0.8·ATR，horizon 24 根）3 类 | `:314-369` |
| 买点头 | `lgbm_entry_v{ver}.txt` + `calib_entry_np_v{ver}.pkl` | `entry_label`（条件于方向的 R 触达） | `:371-402` |
| **状态头** | `lgbm_state.pkl` | `state_label`（KMeans k=4，无监督，仅诊断，不入特征） | `:285-312` |

**差异**：《规范》第 3 节要求四头 = **方向 / 质量 / 买点 / 卖点**。现有第 4 头是**状态头**（无监督 KMeans 聚类诊断头，源码注释明确"仅作诊断，质量头纯靠连续结构因子分化"，`:306-308`），**并非卖点头**。

→ **结论：需新增卖点头（第 4 个监督头）**，见 §6.3。此为《规范》落地必须项。

### 2.4 训练与校准现状

| 项 | 现状 | 合规 |
|---|---|---|
| 质量头切分 | TSS `n_splits=5`，取最后 fold 定版，`early_stopping(20)` | ✅ 合规（`:498-551`） |
| 方向头切分 | `train_test_split(random_state=seed)` | ❌ **随机打乱，违反《规范》4.1 禁止打乱**（`:325`） |
| 买点头切分 | `train_test_split(random_state=seed)` | ❌ **同上违规**（`:376`） |
| 概率校准 | Isotonic + `NumpyCalibrator`（生产 sidecar 无 sklearn 亦可加载） | ✅ 合规（`:353-361`, `:390-395`） |
| 校准退化护栏 | `CALIB_MIN_LEVELS=4` / `CALIB_MIN_POS=8`，退化则回退原始概率 | ✅（`:167-204`） |
| 基线导出 | `feature_baseline.json`（mean/std/p01/p50/p99/deciles，供 PSI 漂移检测） | ✅（`:559-582`） |
| 过拟合抑制 | `num_leaves=15, min_child_samples=20-30, subsample=0.8, colsample_bytree=0.75-0.8, reg_lambda=1.0, reg_alpha=0.1` | ✅ 已有（`:421-427`） |

### 2.5 在线推理现状

- `quality_scorer.py`（Windows 主机 sidecar，每 5s 推理）→ 写 Redis `hcm:live:hexp:ai:{symbol}`。
- 运行环境 `C:\Python313`，**无 sklearn**（故校准器用 `calib_np.NumpyCalibrator` 纯 numpy 实现），**更无 torch**。
- 多周期 K 线经 Redis `latest_kline:{symbol}:{tf}` 读取（桥每 5s 写入）。
- 当前 `ai.mode=decoupled`，AI 评分**纯观测不进决策**。

### 2.6 现状问题清单（阻塞分级）

| 编号 | 级别 | 问题 | 依据 |
|---|---|---|---|
| **P0-1** | ✅ **已解决** | 特征契约文件曾从磁盘丢失，**已于 2026-08-29 按裁决用 `41b00e2` 还原并验证**：`IMPORT_OK len=39`、`git status` 干净。训练链路已恢复 | §16.1 |
| **P0-2** | 🔴 阻塞 | **HP（乘幂）原生因子未落库**：`MISSING_HEXP = [hp_score, hp_strength, dir_sum, k, verdict]` 以 NaN 占位，且训练侧直接 drop_cols 删除。`《规范》`第 3 节要求"融合 HP 原生因子"，**当前无 HP 因子可融合** | `quality_features.py:50`；`train_signal_quality.py:119` |
| **P0-3** | ✅ **已验证可行** | 本机**有外网**。实证：Python 3.13.14 可用；`timesfm=3.0.0` 要求 `>=3.10` ✅；`torch=2.13.0` 提供 `cp313-cp313-win_amd64` 轮子（116MB）✅；`pypi.org` 通、`huggingface.co` HTTPS **200**、`hf-mirror.com` 通（备用）✅；磁盘 C:150.9GB / D:647.8GB 空闲 ✅ | §16.2 |
| **P0-4** | 🟠 语义已定 | 缺卖点头（现有第 4 头为状态头）。**用户已裁定语义 = 预测最佳离场点（驱动移动 SL）**，标签定义见 §6.3，**存在与桥 trailing 争夺 SL 控制权的红线风险** | §6.3、§16.4 |
| **P1-1** | 🟠 高 | 方向头/买点头用**随机切分**违反禁止打乱铁律 | `:325`, `:376` |
| **P1-2** | 🟠 高 | **无回测设施**：`evaluate()` 仅算 AUC/胜率/覆盖，**无夏普比率、最大回撤**。《规范》7.3 验收（夏普 +3%、回撤不恶化）当前**无法执行** | `train_signal_quality.py:207-219` |
| **P1-3** | 🟠 高 | **样本量严重不足**：labels 约 440 条、正样本约 16%；新增 ≤20 维后总维 56+，样本外切片约 80 条 → AUC 估计噪声大，验收易失真 | 历史训练记录；§12 R-1 |
| **P1-4** | 🟡 中 | 无 TimesFM 离线任务调度与降级机制（《规范》7.4） | 现状无对应组件 |

---

## 3. 目标架构

### 3.1 架构总览

```
┌─────────────── 离线（每日收盘后批量，绝不盘中调用）───────────────┐
│  多周期 OHLCV (1m/5m/15m/1H)                                     │
│        ↓                                                          │
│  TimesFM 2.5-200m 编码 → 原始 embedding                          │
│        ↓ PCA（训练期拟合，固定复用）                              │
│  12 维 PCA 特征 + 5 类结构化特征 = 17 维                          │
│        ↓ 写 PG hcm_ai.timesfm_features（按 bar 时间戳严格对齐）    │
└──────────────────────────────────────────────────────────────────┘
                                ↓（离线/在线唯一接口：PG 表）
┌─────────────── 线上（毫秒级，只跑 LightGBM）─────────────────────┐
│  实时 HP/手工因子（现有 39 维，build_features 计算）               │
│        ⊕ 拼接                                                     │
│  TimesFM 离线特征（按 bar 时间查表；缺失→填 0 降级，不阻塞）        │
│        ↓                                                          │
│  四头 LightGBM（方向/质量/买点/卖点）→ 概率校准 → 0-100 置信       │
│        ↓                                                          │
│  （原链路不变）DeepSeek 复核 → 耦合 → 风控执行                     │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 铁律分工（《规范》1-2 节，强制）

1. **TimesFM 只离线产出特征**，永不上线、不实时推理、不输出信号。
2. **LightGBM 是唯一线上决策、唯一置信输出、唯一审计模型**。
3. 线上原有缓存、打分耦合、DeepSeek 复核、风控逻辑**零改动**。

---

## 4. TimesFM 特征提取规范

### 4.1 运行环境（隔离，P0-3）

- 独立 venv：**Python 3.11 + torch**（与生产 `C:\Python313` **物理隔离**，禁止污染生产 sidecar 环境）。
- TimesFM：`timesfm-2.5-200m-pytorch`，权重从 HuggingFace 离线导入（网络不可达时走离线拷贝 + `HF_HUB_OFFLINE=1`）。
- 产出**只写 PG**，不写 Redis、不接触任何线上进程。

### 4.2 输入规范（《规范》2.1，严格固定）

| 项 | 规定 |
|---|---|
| 周期 | 1min / 5min / 15min / 1H |
| 字段 | Open、High、Low、Close、Volume |
| 序列窗口 | 256–512 根 K（建议 **512**，四个周期统一） |
| 运行方式 | **每日收盘后离线批量**，无盘中调用 |
| 禁止输入 | 人工因子、标签、任何未来数据 |

### 4.3 输出规范 ①：PCA 降维

- TimesFM 原始 embedding → **PCA 降至 8–16 维**，建议 **12 维**〔提案·待确认〕，累计解释方差目标 ≥80%（以实测为准）。
- **PCA 拟合铁律（关键防泄露）**：
  - ❌ **禁止**用全量历史（含未来）拟合 PCA —— 投影矩阵会隐含未来分布信息，构成未来函数，违反《规范》7.1 与铁律五。
  - ✅ **必须**在**训练期窗口内**拟合 PCA，拟合后**固定**复用（记录 `pca_version`）；后续重训时按 walk-forward 可重新拟合但**只用当 fold 训练段**。
  - PCA 版本与模型版本绑定落盘，推理/离线抽取时加载同一版本。
- PCA 分量命名：`tmf_pc00 … tmf_pc11`。

### 4.4 输出规范 ②：5 类结构化特征（显式定义〔提案·待确认〕）

> 《规范》只给名称。以下为可执行定义，符号：`c_t`=当前收盘，`h`=预测 horizon（建议 **12 根**，对应质量头 horizon），`ŷ_{t+k}`=第 k 步点预测，`σ_Δ`=历史 256 根逐根收益标准差（**仅用 t 及之前**）。

1. **趋势延续得分 `tmf_trend_cont`** ∈[-1,1]
   `tmf_trend_cont = tanh( (ŷ_{t+h} - c_t) / (h · σ_Δ + ε) )`
   语义：预测终值相对当前的标准化位移，>0 延续上行、<0 延续下行。

2. **趋势反转概率 `tmf_rev_prob`** ∈[0,1]
   令 `k* = argmax_k |ŷ_{t+k} - c_t|`（预测路径极值位）：
   `tmf_rev_prob = 0` 若 `sign(ŷ_{t+h}-c_t) == sign(ŷ_{t+k*}-c_t)`
   `tmf_rev_prob = clamp( |ŷ_{t+h}-c_t| / (|ŷ_{t+k*}-c_t| + ε), 0, 1 )` 否则
   语义：路径冲高后回落/探底后回升的回撤占比 → 反转强度。

3. **波动周期强度 `tmf_vol_cycle`**（相对量纲无关）
   `tmf_vol_cycle = std(ŷ_{t+1..t+h}) / (std(Δc)_{近256根} + ε)`
   语义：预测区间波动相对历史波动的倍数，>1 预示波动放大。

4. **多周期共振得分 `tmf_mtf_resonance`** ∈[-1,1]
   `tmf_mtf_resonance = Σ_tf w_tf · tanh(trend_cont_tf) / Σ_tf w_tf`
   权重建议〔待确认〕：1m=0.15, 5m=0.25, 15m=0.25, 1H=0.35。
   语义：|值| 越接近 1 = 四周期方向越一致。

5. **历史行情相似度 `tmf_hist_sim`** ∈[-1,1]
   `tmf_hist_sim = max_{s ∈ 检索库} cos(e_t, e_s)`
   检索库：滚动最近 2048 根历史 bar 的 embedding。
   **防泄露铁律**：检索库**只含 `s ≤ t - gap`** 的 bar，且 `gap ≥ h`（预测 horizon），杜绝用"未来相似段"的结果泄漏。

### 4.5 维度预算

| 类别 | 维数 | 列名前缀 |
|---|---|---|
| PCA 分量 | 12（建议先 8 验证） | `tmf_pc00..11` |
| 5 类结构化 | 5 | `tmf_trend_cont / tmf_rev_prob / tmf_vol_cycle / tmf_mtf_resonance / tmf_hist_sim` |
| **合计新增** | **17（≤20 ✅）** | |
| 现有契约 | 39 | — |
| **扩展后总计** | **56** | |

⚠️ **过拟合警告**：样本约 440 条 / 56 维 → 维数样本比极差。**强制分阶段**：
- **阶段 A**：先加 PCA **8 维** + 5 结构化 = 13 维（总 52），验证增益；
- **阶段 B**：仅当阶段 A 样本外 AUC 相对基线提升 ≥2% 且重要性前 20 名中 PCA 列占比 ≥20% 时，才扩到 12 维。

### 4.6 未来数据泄露防护（总检）

| 风险点 | 防护 |
|---|---|
| PCA 全历史拟合 | 仅训练期拟合并固定（§4.3） |
| 相似度检索命中未来 | 检索库排除 `s > t-gap`，`gap ≥ h`（§4.4-5） |
| K 线未来棒 | 复用桥既有写入源根治逻辑（历史上已修），离线抽取时再校验 `open_time ≤ now()` |
| 特征时间戳错位 | 特征严格按 **bar `open_time`** 对齐，写入即记录 `bar_time`；在线按 bar 时间**左连接**，禁止前视填充 |

---

## 5. 特征存储与拼接设计

### 5.1 存储（PG 新表）

```sql
CREATE TABLE IF NOT EXISTS hcm_ai.timesfm_features (
    symbol        TEXT        NOT NULL,
    time_frame    TEXT        NOT NULL,   -- M1/M5/M15/H1
    bar_time      TIMESTAMPTZ NOT NULL,   -- 严格对齐 K 线 open_time
    tmf_version   TEXT        NOT NULL,   -- 模型+PCA 版本，如 tfm25_pca_v1
    tmf_pc00      DOUBLE PRECISION,
    ...                                    -- tmf_pc01..tmf_pc11
    tmf_trend_cont      DOUBLE PRECISION,
    tmf_rev_prob        DOUBLE PRECISION,
    tmf_vol_cycle       DOUBLE PRECISION,
    tmf_mtf_resonance   DOUBLE PRECISION,
    tmf_hist_sim        DOUBLE PRECISION,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, time_frame, bar_time, tmf_version)
);
CREATE INDEX IF NOT EXISTS idx_tmf_lookup
  ON hcm_ai.timesfm_features (symbol, time_frame, bar_time DESC);
```

- **5 类结构化特征只在主周期（M5）落库**（供拼接）；PCA 分量四周期均可落（多周期共振已在 M5 记录汇总）。〔待确认：是否需按周期分别落 PCA〕

### 5.2 在线读取与**降级（关键）**

- 在线 `build_features` 按 `(symbol, 'M5', bar_time)` 查表，**左连接**取最近且 `bar_time ≤ 当前bar` 的记录。
- **缺失 → 全部填 `0.0`**，与现有 `ds_fake_prob/ds_sl_coeff/ds_continuity` 的"缺省 0.0"语义**完全一致**（该语义已在生产验证：`train_signal_quality.py:257-269` 的 ds_diag、及 `_model_feature_cols.py` 注释明确"缺省 0.0 保证训练-推理同分布"）。
- **降级保证**：TimesFM 离线任务失败/延迟 → 特征恒 0，模型照常推理，**绝不阻塞下单链路**。此为《规范》7.4"异常可降级"的落地。

### 5.3 特征契约扩展

`MODEL_FEATURE_COLS` 追加 17 列（顺序固定，追加在尾部，避免打乱既有 39 维顺序导致旧模型失效）：

```python
# --- TimesFM 时序特征（v1, 17 维）---
"tmf_pc00", ..., "tmf_pc07",           # 阶段 A：8 维
"tmf_trend_cont", "tmf_rev_prob", "tmf_vol_cycle",
"tmf_mtf_resonance", "tmf_hist_sim",
# 阶段 B 若启用再追加 tmf_pc08..tmf_pc11
```

⚠️ **契约变更即模型不兼容**：追加列后必须**全量重训四头**，旧模型不可混用（LightGBM 按名取列，缺列→NaN→预测崩）。

---

## 6. 四头特征分配与标签体系

### 6.1 四头映射（现状 → 目标）

| 《规范》头 | 现状 | 动作 |
|---|---|---|
| 方向头 | ✅ 已有（`lgbm_direction_v*`，3 类） | 改特征子集 + 改 TSS |
| 质量头 | ✅ 已有（`lgbm_quality_v*`） | 改特征子集（PCA 为核心增益） |
| 买点头 | ✅ 已有（`lgbm_entry_v*`） | 改特征子集 + 改 TSS |
| 卖点头 | ❌ 无（现状为状态头） | **新增** |

> 状态头处置：保留为**诊断工具**（不删，避免丢失既有诊断能力），但不再计入"四头"。

### 6.2 各头特征子集（《规范》第 3 节）

| 头 | TimesFM 特征子集 | 说明 |
|---|---|---|
| 方向头 | `tmf_trend_cont`、`tmf_rev_prob`、`tmf_mtf_resonance` | 趋势延续 / 反转 / 周期共振 |
| 质量头 | `tmf_hist_sim`、`tmf_vol_cycle`、**全部 PCA 分量** | 行情相似度 / 波动强度 / PCA（**核心增益**） |
| 买点头 | `tmf_rev_prob`（短期反转）、短周期 PCA（`tmf_pc` 中按重要性选 4 维）〔待确认〕 | 短期反转 + 短时时序 |
| 卖点头 | `tmf_trend_cont` 取反语义、`tmf_vol_cycle`、波动率相关 PCA | 趋势衰竭 + 波动率时序 |

> 各头仍共享全部 39 维现有特征 + 上表子集（**子集只决定 TimesFM 列的可见性**，不是只喂 TimesFM 列）。四头**独立数据集/训练/校准/模型文件**（《规范》3）。

### 6.3 卖点头标签（新增）—— 语义已裁定：**预测最佳离场点（驱动移动 SL）**

> 用户 2026-08-29 裁定：卖点头 = **预测最佳离场点**，用途是**驱动移动 SL**（不是"是否平仓"的二分类）。

现有标签构造器提供 `label_one`（质量，±R 触达 horizon=12）、`dir_label_one`（方向，±0.8·ATR/24 根）、`entry_label_one`（买点），**无卖点标签**，需新增 `sell_label_one`（`build_labels.py`）。

#### 6.3.1 标签定义〔提案·待确认〕

**语义**：该笔持仓**在达到 +1R 浮盈后，是否会发生显著利润回吐**（即"是否需要移动 SL 锁定利润"）。

计算（未来 `sell_horizon_bars` 根 M5，建议 24；`d`=方向，`R`=风险距离，`extreme`=按方向取 high/low）：

1. `mfe = max_k( (extreme_k − entry) · d ) / R` —— 最大有利偏移
2. `k_peak` = 达到 `mfe` 的 bar 索引
3. `retrace = ( (extreme_peak − min_after_peak) · d ) / R` —— 峰值后最大回撤

| 条件 | `sell_label` | 含义 |
|---|---|---|
| `mfe < sell_mfe_target`（默认 1.0R） | `None`（排除） | 从未达 1R 盈利，无"锁定利润"可言 |
| `retrace >= sell_retrace_R`（默认 0.5R） | `1` | 利润显著回吐 → **需要移动 SL 保护** |
| 其余 | `0` | 趋势延续，可放心持有 |

配置项（**必须**走 `config_provider.set` 双写 PG+Redis，禁硬编码）：
`ai.lm.sell_mfe_target`(1.0)、`ai.lm.sell_retrace_R`(0.5)、`ai.lm.sell_horizon_bars`(24)

#### 6.3.2 🔴 红线风险：SL 控制权唯一性（最高优先级）

卖点头驱动移动 SL 会**直接触碰系统最敏感的控制权问题**，历史已有重大事故：

- 桥 `mt5_bridge._update_trailing_stops` **已实现**完整移动止损：保本 `breakeven`、移动追利 `trail_start/trail_wide`、TP 接力 `tp_relay`，且全部**会话化可配**（`close.<session>.*`）。
- **历史事故（必读）**：跟单桥曾独立跑 trailing，与主号镜像 MODIFY **争夺 SL 控制权**，导致跟单号 SL 被推得比主号更激进 → 盈利阶段跟单号先被扫平。修复方式是"跟单桥跳过独立 SL/TP 管理，完全由主号镜像驱动"（`IS_FOLLOWER` 分支）。
- **SL 真相源铁律**：**跟单号的 SL/TP 唯一真相源是主号**，跟单桥禁止自行改 SL。

因此卖点头落地**必须**遵守：

1. **二选一**：要么由桥既有 `trailing` 管 SL，要么由 AI 卖点头管 SL，**禁止两者并行争夺**。建议灰度期保留桥 trailing，AI 卖点头先只做**观测/影子**，对比后再切换。
2. **只收紧、不放宽**：AI 驱动的移动 SL 仅允许向**减小风险**方向移动（BUY 只上调 SL / SELL 只下调 SL），禁止放宽。
3. **仅作用于主号**：AI 改 SL 只能作用于**主号**持仓；跟单号经既有 `manual_mirror MODIFY` 链路同步，**跟单桥绝不自行按 AI 改 SL**。
4. **降级**：卖点头信号不可用时，SL 完全交回桥 `trailing`，行为与今天一致。

> 违反以上任一条，即可能复现"跟单号提前被扫平"级事故。此项需**单独变更说明**后再实施。

---

## 7. 训练规范改造

### 7.1 时序切分（修正 P1-1 违规）

- **方向头、买点头**：`train_test_split(random_state=seed)` → 改为 **TimeSeriesSplit**（与质量头一致）。
- 全头统一：训练 → 验证 → **样本外测试集**（唯一验收标准，禁止用训练/验证指标验收，《规范》4.2、6）。
- 禁止任何 shuffle。

### 7.2 四头独立训练 + 校准

- 四个模型文件、四个校准器、四份 `feature_baseline` 分位数（或共用基线但按头存）。
- 校准统一 `Isotonic → NumpyCalibrator`（保持生产 sidecar 无 sklearn 可加载的既有优势），沿用退化护栏 `CALIB_MIN_LEVELS=4` / `CALIB_MIN_POS=8`。
- 输出 0–100 稳定置信分。

### 7.3 抑制过拟合

沿用并强化现有参数：`num_leaves≤15`、`max_depth` 限制、`learning_rate=0.05`、`min_child_samples≥20`、`subsample=0.8`、`colsample_bytree≤0.8`、`reg_lambda/reg_alpha`。
**新增**：`colsample_bytree` 建议下调至 0.6（维度从 39→56，需更强列采样）〔待确认〕。

### 7.4 基线对照（《规范》4.5，强制）

每次训练**必须**同时训练并评估"**纯 HP/手工因子基线模型**"（仅现有 39 维，不含 TimesFM 列），并记录：

```
baseline_auc / with_tmf_auc / lift = (with_tmf_auc - baseline_auc) / baseline_auc
```

该 `lift` 为验收唯一依据（目标 ≥2%）。

---

## 8. 回测设施（新建，P1-2）

当前无回测能力，《规范》7.3 无法执行。新建 `tools/backtest_signal.py`：

| 项 | 要求 |
|---|---|
| 驱动方式 | 事件驱动，逐 bar 回放，禁止未来数据 |
| 输入 | `signals`（真实历史信号）+ M5 K 线 + 四头置信分 |
| 交易规则 | **必须复用生产真实规则**：会话化 SL/TP（`close.<session>.*`）、保本/trailing（`mt5_bridge._update_trailing_stops` 同逻辑）、手数规则、最大持仓数 |
| 成本 | 固定滑点 + 手续费（与实盘经纪商口径一致，参数显式声明） |
| 输出指标 | 夏普比率、最大回撤、胜率、盈亏比、无效开仓数、震荡假信号数 |
| 验收口径 | 基线模型 vs TimesFM 模型**同滑点/手续费/仓位**对比 |

⚠️ **伪交付红线**：回测必须复用生产 SL/TP/手数规则，否则回测与实盘不一致 → 验收无效。

---

## 9. 上线方案与灰度（《规范》5.3、6，禁止无灰度直接替换）

| 阶段 | 内容 | 通过条件 |
|---|---|---|
| **G0 模拟** | 新模型旁路运行，**只写影子预测不执行**，与线上并行 ≥1 周 | 置信分分布平稳、无剧烈跳变；四头无退化 |
| **G1 小仓位** | 按 `risk` 配置降至最小仓位（如 10%）实盘 | 样本外指标不劣化；回撤不恶化 |
| **G2 全量** | 恢复全仓位 | 连续观察 ≥2 周，夏普/回撤达标 |

- 灰度开关走**配置中心**（`config_provider.set` 双写 PG+Redis+PUB），禁硬编码。
- 全程可一键切回旧模型（§10）。

---

## 10. 降级与回滚

| 场景 | 处置 |
|---|---|
| TimesFM 离线任务失败/延迟 | 特征填 0，模型照常推理，**不阻塞**（§5.2） |
| 新模型指标退化 | 配置切回旧模型版本（`ai.lm.model_path` / `calib_path`），秒级生效 |
| 契约不匹配 | 训练侧 `raise` fail-fast（`train_signal_quality.py:443-452`），不产出坏模型 |
| 回滚锚点 | 模型文件按版本号落盘（已有 `snapshots/*.json` 训练快照机制），保留最近 N 版 |

---

## 11. 验收标准（对齐《规范》第 7 节，全部通过方可上线）

### 11.1 特征验收（7.1）

- [ ] **无未来函数**：PCA 仅训练期拟合（§4.3）；相似度检索 gap 防护（§4.4-5）；时间戳 `bar_time ≤ 信号时刻`
- [ ] **时间戳完全对齐**：抽样 ≥100 条，特征 `bar_time` 与信号 bar 一一对应，错位率 = 0
- [ ] **维度合规**：新增 ≤20 维（实际 17 ✅）
- [ ] **无严重共线性**：新增列间 `|Pearson r| < 0.9`；对现有 39 维做 VIF，VIF > 10 的列剔除或合并

### 11.2 模型指标验收（7.2，**样本外**）

- [ ] AUC **相对基线提升 ≥2%**（`lift`，§7.4）
- [ ] IC 相对基线提升 ≥2%
- [ ] 置信分无剧烈跳变：相邻 bar 置信分差分 p99 在基线 ±20% 内；分布（分位数）平稳
- [ ] 四头**无指标退化**：各头样本外指标 ≥ 基线头指标 −0.005（容差）
- [ ] 判据以 **TSS 5-fold AUC 均值 ± 标准差**为准，**禁止**用单一切片或训练集指标验收

### 11.3 回测验收（7.3，同滑点/手续费/仓位）

- [ ] 夏普比率**相对提升 ≥3%**
- [ ] 最大回撤**不恶化**（≤ 基线 × 1.02）
- [ ] 无效开仓数减少、震荡假信号数下降（需给出前后计数）

### 11.4 系统验收（7.4）

- [ ] 离线任务自动执行（每日收盘后调度），失败有告警
- [ ] 异常可降级切回旧模型（§10 已验证演练）
- [ ] 线上延迟：拼接 TimesFM 特征后单次推理 P99 延迟增幅 ≤10%（查表为本地 PG/缓存，应远低于此）
- [ ] 链路与审计完全兼容：Redis `hcm:live:hexp:ai:{sym}` 结构不变，下游零改动

---

## 12. 风险登记册

| 编号 | 风险 | 等级 | 缓解 |
|---|---|---|---|
| **R-1** | **样本量不足**（~440 条 / 56 维），过拟合风险 > 增益 | 🔴 高 | 分阶段加维（先 8 维）；强列采样；以 TSS 多 fold 均值验收；样本不足则**暂缓引入** |
| **R-2** | TimesFM 权重/环境不可得（无外网） | 🔴 高 | P0-3，先做环境可行性验证（**第一步**） |
| **R-3** | 特征契约文件丢失导致训练崩溃 | 🔴 高 | P0-1 立即恢复（先查 IDE 本地历史） |
| **R-4** | HP 因子未落库，融合无源 | 🟠 中 | P0-2，需 hexp 引擎落库改动（**独立红线变更，需单独变更说明**） |
| **R-5** | 回测与实盘规则不一致 → 伪验收 | 🟠 中 | §8 强制复用生产 SL/TP/手数规则 |
| **R-6** | PCA 隐式未来信息 | 🟠 中 | §4.3 拟合窗口铁律 |
| **R-7** | 卖点头标签语义偏差 | 🟠 中 | §6.3 需用户确认语义后再构造 |
| **R-8** | 维度膨胀拖慢线上推理 | 🟡 低 | 查表 O(1)，影响可忽略；仍需实测 P99 |

---

## 13. 红线合规检查（《规范》第 6 节逐条）

| 红线 | 本方案合规 | 落地保障 |
|---|---|---|
| 禁止盘中调用 TimesFM | ✅ | 离线任务仅在收盘后调度；线上无 TimesFM 依赖（sidecar 无 torch） |
| 禁止高维 Embedding 直接训练 | ✅ | 强制 PCA 降至 8–16 维（§4.3） |
| 禁止四头混训、共用参数 | ✅ | 四头独立数据集/训练/校准/模型文件（§7.2） |
| 禁止跳过校准 | ✅ | 沿用 Isotonic+NumpyCalibrator + 退化护栏（§7.2） |
| 禁止用训练集指标验收 | ✅ | 仅样本外 + TSS 多 fold（§11.2） |
| 禁止特征带未来函数 | ✅ | §4.6 六项防护 |
| 禁止无灰度直接替换上线 | ✅ | G0→G1→G2 三段灰度（§9） |

**铁律五（量化红线）额外核对**：
- ✅ 未改 hexp 原始信号方向
- ✅ 未改异步架构（TimesFM 为离线批处理，不涉 Stream/消费组）
- ✅ 未删减缓存/队列/超时/重试/熔断/降级机制（反而新增特征缺失降级）
- ✅ 时序数据未 shuffle（并修正方向/买点头既有 shuffle 违规）
- ✅ 所有外部调用（HuggingFace 权重下载）离线化，线上路径零外部依赖

---

## 14. 六段权衡与排期

| 维度 | 评估 |
|---|---|
| **收益** | PCA 时序特征为质量头提供现有 39 维不具备的"时序表征"信息；多周期共振助方向头。理论上可提升样本外 AUC |
| **风险** | 样本量/维度比失衡（R-1）为主风险；环境不可得（R-2）为前置不确定性 |
| **兼容性** | 线上仅"特征拼接 + 重训模型"两点变化；Redis 结构、下游链路、风控、DeepSeek 均零改动 |
| **回退** | 配置切回旧模型版本，秒级；模型按版本号留存 |
| **监控** | 复用 `feature_baseline.json` PSI 漂移检测（新增 17 列同步纳入基线）；TimesFM 离线任务需新增成功/延迟告警 |
| **排期** | 见下 |

**里程碑（建议）**

| 阶段 | 任务 | 前置 | 产出 |
|---|---|---|---|
| **M0 环境可行性** | 验证 Python 3.11+torch 环境、TimesFM 权重离线导入、单样本推理跑通 | — | 可行性结论；**不通过则终止** |
| **M1 止血** | 恢复 `_model_feature_cols.py`（P0-1）；修正方向/买点头 shuffle 违规（P1-1） | — | 训练链路恢复 |
| **M2 离线特征** | `timesfm_features.py` + PG 表 + 调度 + 降级 | M0 | 17 维特征入库 |
| **M3 特征验收** | 7.1 四项（未来函数/对齐/维度/共线性） | M2 | 特征验收报告 |
| **M4 四头改造** | 新增卖点头标签 + 四头特征子集 + TSS + 基线对照 | M1, M3, 卖点语义确认 | 四头模型 + lift 报告 |
| **M5 回测** | `backtest_signal.py` + 7.3 验收 | M4 | 夏普/回撤对比报告 |
| **M6 灰度上线** | G0→G1→G2 | M5 全通过 | 上线 |

> **M0 是首要动作**：环境不可得则后续全部无意义。

---

## 15. 待确认事项（阻塞项，需用户裁决）

| # | 事项 | 状态 |
|---|---|---|
| 1 | 【P0-3 环境】TimesFM 运行环境与权重获取是否可行 | ✅ **已验证可行**（有外网），见 §16.2 |
| 2 | 【P0-4 卖点头语义】 | ✅ **已裁定 = 预测最佳离场点（移动 SL）**，见 §6.3、§16.4 |
| 3 | 【P0-2 HP 因子】是否批准引擎持久化 `factor_raws` | ❌ **事项撤销** —— 审计更正：早已落库且在用，见 §16.3 |
| 4 | 【P0-1 文件丢失】是否立即恢复 | ✅ **已裁决并执行**（`41b00e2` 还原），见 §16.1 |
| 5 | 【周期】PCA 是否按 4 周期分别落库 | ✅ **已建议 = 否（仅 M5）**，见 §16.5 |
| 6 | 【维度】PCA 取 8 维还是 12 维 | ⏳ 建议先 8 维验证（§4.5） |
| 7 | 【HP 因子消费改造】训练侧是否消费 `_hexp.factor_raws` | ⏳ 建议先做 VIF/AUC 增益验证再决定（§16.6） |
| 8 | 【容器栈停机】是否立即 `docker compose up -d` | ⏳ **待裁决**（系统已停约 1.5h） |

---

## 16. 决议记录（2026-08-29）

### 16.1 P0-1 文件还原 —— 已执行

- 裁决：用 `41b00e2` 还原 `hcm-v2/tools/_model_feature_cols.py`。
- 执行：`git checkout 41b00e2 -- hcm-v2/tools/_model_feature_cols.py`。
- 验证：`FILE_EXISTS=True`；`IMPORT_OK len=39`（首 3 列 `adx_14/rsi_14/macd`，末 3 列 `r_dist_atr/sl_mult_used/entry_atr_ratio`）；`git status` 无残留变更（还原内容与 HEAD 一致）。
- **训练链路已恢复**。

### 16.2 P0-3 环境可行性 —— 已实证通过

| 检测项 | 结果 |
|---|---|
| 可用 Python | `C:\Python313\python.exe` = **3.13.14**（无 `py` 启动器） |
| timesfm 包 | `version=3.0.0`，`requires_python=**>=3.10**` ✅ |
| timesfm[torch] 依赖 | numpy / huggingface_hub / safetensors / torch>=2.0.0（**不含 jax**，Windows 友好）✅ |
| torch 轮子 | `torch-2.13.0-cp313-cp313-win_amd64.whl`（116 MB）✅ |
| 网络 | `pypi.org` ✅；`huggingface.co` HTTPS **200** ✅（TCP 探测报 False 但 HTTPS 正常）；`hf-mirror.com` ✅（备用） |
| 磁盘 | C: 空闲 **150.9 GB**、D: 空闲 **647.8 GB** ✅ |

**环境方案（已落地）**：

1. **禁止**把 torch/timesfm 装入生产 `C:\Python313`（那是 `quality_scorer.py` sidecar 运行时）。
2. 建**独立 venv**（已建）：`C:\Python313\python.exe -m venv D:\.venv_timesfm`。
   放**工作区外**，避免污染仓库（铁律八）。
3. **CPU-only torch**：`pip install torch --no-deps`（`--no-deps` 跳过 nvidia-* 包），
   再补 `filelock typing-extensions sympy networkx jinja2 fsspec setuptools scikit-learn huggingface_hub safetensors timesfm`。
4. 该 venv **仅供离线批处理**，任何线上进程（sidecar/桥/信号塔）不得依赖它。

#### 16.2.1 实施实测坑位（2026-08-29，**与原预期多处相反，务必遵守**）

| # | 坑 | 实测证据 | 正确做法 |
|---|---|---|---|
| 1 | **timesfm 1.3.0 不可用** | `requires_python=<3.12,>=3.10`；`torch[cuda]` 仅在 `python_version=="3.11"` 提供；依赖 paxml/wandb/absl-py | **必须用 3.0.0**。3.0.0 内含 `timesfm/timesfm_2p5/timesfm_2p5_torch.py`，**向后兼容 2.5 权重**；其 `DEFAULT_REPO_ID="google/timesfm-2.5-200m-pytorch"` 与《规范》指定模型一致 ✅ |
| 2 | **Windows 必须关 torch_compile** | 类默认 `torch_compile=True` 会触发 `torch.compile`；权重 `config.json` 亦为 `"torch_compile": false` | `from_pretrained(..., torch_compile=False)` |
| 3 | **Xet 后端导致静默挂起** | `cas-server.xethub.hf.co` **超时不可达**；表现为进程 CPU=0、缓存恒 0 字节、**无任何报错** | 必须 `HF_HUB_DISABLE_XET=1`，回退 classic HTTP |
| 4 | **hf-mirror.com 对本仓库不可用** | `.../model.safetensors` → **308 Permanent Redirect** | 不可用（原"镜像作备用"**对本仓库无效**，已修正） |
| 5 | **HF 官方源限速且为全局上限**；**ModelScope 快 32 倍** | HF 单连接 125–132 KB/s、**12 线程并行聚合仅 114 KB/s**（并行无效，882MB 约 2 小时）；**ModelScope 实测 4067–7214 KB/s，882MB 仅 2.5 分钟** ✅ | **权重从 ModelScope 下载**（须带 `User-Agent`，否则 CDN 403），落本地目录后用 `--model-dir` 离线加载 |
| 6 | **ModelScope 的 403 是缺 User-Agent 所致** | `WebClient` 请求 → 403；**Python urllib 带 UA → 206 正常**（同一 URL）。文件与 HF 完全一致（925,181,104 B） | **可用且最快**，必须设置 `User-Agent` 请求头 |
| 7 | **PyPI 官方源极慢/挂死** | `files.pythonhosted.org` 下 122MB 恒 0 字节；`download.pytorch.org` 挂死 14min 仅 58MB | **改用清华镜像** `https://pypi.tuna.tsinghua.edu.cn/simple`（实测 **19.6 MB/s**，122MB 仅 6 秒） |
| 8 | **TimesFM 2.5 是单变量模型** | API `forecast(horizon, inputs=[1D 序列])`，无多通道入参 | 《规范》§2.1"输入字段 OHLCV"应理解为**数据取自 OHLCV K 线、实际建模序列用 close**；多变量需另用 `forecast_with_covariates`（非本次范围） |

#### 16.2.2 已确认的 TimesFM API（timesfm 3.0.0）

```python
import timesfm
from timesfm.configs import ForecastConfig

model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
    "google/timesfm-2.5-200m-pytorch", torch_compile=False)
model.compile(ForecastConfig(max_context=512, max_horizon=128,
                             per_core_batch_size=1, normalize_inputs=False))

# ① 预测：点预测 + 9 分位数（供 5 类结构化特征）
point, quant = model.forecast(horizon=12, inputs=[close_1d])   # (12,) / (12, 9)

# ② Embedding（供 PCA）：forward() 返回四元组
# 【关键坑·实测】forecast() 内部会自动做 patch 分块，但**直接调 forward() 不会分块**，
# 必须自行把序列切成 (batch, n_patches, patch_length=32)；否则 tokenizer 收到
# (1, 512) 会被当作 512 个独立特征，报
#   RuntimeError: mat1 and mat2 shapes cannot be multiplied (1x1024 and 64x1280)
n_patch = len(window) // 32
x = torch.from_numpy(window[:n_patch * 32].astype(np.float32)).reshape(1, n_patch, 32)
mask = torch.ones_like(x)
(in_emb, out_emb, out_ts, out_q), caches = model.model.forward(x, mask)
# out_emb: (1, 16, 1280) → 沿 patch 维均值池化 → 1280 维向量（实测 dim=1280 ✅）
```

- `max_context` 须为 `patch_length=32` 整数倍（256/512 合规）；`max_horizon` 须为 `horizon_length=128` 整数倍（取 128 后再截取前 12 步）。
- 权重 `config.json`：`hidden_size=1280`、`context_length=16384`、`horizon_length=128`、声明 9 个分位数(0.1–0.9)，但 **`forecast()` 实际输出 10 个槽位**（`(horizon, 10)`），以实测形状为准。

#### 16.2.3 M0 冒烟测试结果（2026-08-29 通过）

| 验证项 | 结果 |
|---|---|
| 本地离线加载权重（`--model-dir`） | ✅ 2.1s |
| `compile(ForecastConfig(max_context=512, max_horizon=128))` | ✅ |
| `forecast(horizon=12)` | ✅ 0.16s，`point=(1,12)`、`quant=(1,12,10)` |
| `forward()` 取 `output_embeddings` | ✅ `(1, 16, 1280)`（修复分块 bug 后） |
| 池化 embedding 维度 | ✅ 1280 == `config.hidden_size` |
| **M0 结论** | **ALL OK —— 环境与 API 准入通过** |

同期完成两项前置验证：
- **5 类特征数学单测 18 项全通过**（单调上行 `trend_cont=+0.682`、冲高回落 `rev_prob=0.5`、
  全周期同向 `mtf_resonance=0.9999`、相同向量 `hist_sim=1.0` 等）。
- **生产代码端到端通过**：`tools/timesfm_features.py` 的 `infer_window()` 在真实权重上
  返回 `point=(12,)`、`quant=(12,10)`、`pooled=(1280,)`，5 类特征取值均在合法区间。

#### 16.2.4 🔴 关键发现：embedding 退化为近似一维，及定维结论（2026-08-29）

**现象**：首次 PCA 拟合（809 个 bar 级样本）得到：

```
k= 1  单维=99.950%   累计= 99.95%
k= 2  单维= 0.016%   累计= 99.97%
...    k=16 单维=0.000%  累计=100.00%
```

**PC1 独占 99.95% 方差** —— 1280 维 embedding 实际退化为 **1 个标量**。
此时"PCA 取 8 维还是 12 维"**该问题本身已被证伪**：两者都是"1 个有效维 + N 个噪声维"。
（这正是坚持"用真实曲线定死、不拍脑袋"的价值——避免了在错误前提下做选择。）

**诊断（三步定位）**：

| 步骤 | 结果 | 推断 |
|---|---|---|
| ① 原始 PCA | PC1 = 99.95% | 疑似一维 |
| ② 标准化(z-score)后 PCA | PC1 = **96.10%**，@16 = 99.59% | 排除"单一高方差方向掩盖"；若 1280 维独立，标准化后每维应仅 ~0.078% ⇒ **确为真退化** |
| ③ 逐样本 L2 归一化后 PCA | PC1 = **32.84%**，@8 = 84.07% | **曲线显著变平 ⇒ 差异主要在"幅度"而非"方向"** |

补充证据：维度间 `|corr|>0.9` 占比 **87.6%**（近乎完全共线）；
per-dim std 变异系数 **1.81**（min 0.64 vs max 2066，少数维方差占优）。

**根因**：embedding 的**逐样本模长**差异（transformer 激活尺度随输入价位漂移）
压倒了一切方向信息。喂的是原始价格水平，故模长携带价位/尺度这种**非平稳、无预测价值**的量。

**`--normalize-inputs` 无效（已实测证伪）**：
开启后两次运行的 embedding 池经 `np.allclose` 比对 **完全相同** ——
该参数只作用于 `forecast()` 的编译解码路径，而 embedding 经 `model.model.forward()`
直接提取，**绕过了输入归一化**。故归一化必须在 embedding 侧事后处理。

**修复**：新增 `_normalize_embeddings(pool, mode)`，默认 `l2`（逐样本单位化）。
修复后 PC1 从 99.95% 降至 **33.68%**（生产代码路径实测）。

**维数定案 = 8**（L2 归一化后实测曲线）：

| k | 4 | 6 | **8** | 10 | 12 | 16 |
|---|---|---|---|---|---|---|
| 累计解释方差 | 68.91% | 78.10% | **84.07%** | 87.62% | 90.14% | 93.90% |

边际增益自 k=8 后明显放缓（+2.82 → +1.24 个百分点）。
**8→12 仅 +6.07 个百分点，却多占 4 维**；而标注样本仅约 440 条，
52 维与 56 维的样本/维数比分别为 8.5 与 7.9（均低于经验下限 10）。
**故取 8 维**，总新增 8+5=13 维（远低于《规范》≤20 上限），为样本预算留有余量。

**护栏**：`embed_norm` 写入 PCA meta，`--extract` 时若与拟合值不一致**直接报错**，
防止"拟合用 l2、抽取用 none"导致特征语义静默错位（此类错位极难排查）。

### 16.3 P0-2 HP 因子 —— 事项撤销（审计更正）

**更正**：此前"HP 因子未落库、需批准引擎持久化"的判断**错误**。`factor_raws` **早已落库且在用**：

- 落库：`scheduler.py:2709-2743` 对 HEXP 信号写 `indicator_values._hexp`，含 `factor_raws`（`:2727`）、`dir_sum`、`hp_strength`、`trend_phase`、`period_states` 等（源码注释："B: hexp 落库持久化（纯观测，零下单影响）"）。
- 在线消费：`quality_scorer.py:1117-1118` 读 `hcm:live:hexp:{sym}`，`:525-526` 取 `snapshot["factor_raws"]["adx"]/["rsi"]`。

**真正缺口在训练侧消费**（`quality_features.py:100-104` 只取顶层，未取 `_hexp` 子字典）。风险与建议见 §16.6。

### 16.4 P0-4 卖点头语义 —— 已裁定

**语义 = 预测最佳离场点，驱动移动 SL**。标签定义与 SL 控制权红线见 **§6.3**。

### 16.5 周期 PCA 落库 —— 建议：否（仅主周期 M5）

**硬约束**：《规范》第 2 节"新增总特征 ≤20 维"。若 4 周期各落 12 维 = **48 维，直接违规**。

**建议方案**：

- **仅主周期 M5 落 12 维 PCA**（`tmf_pc00..11`）。
- 多周期信息**不占独立维度**，改由结构化特征 `tmf_mtf_resonance`（四周期 `trend_cont` 加权聚合为 **1 列**）表达。
- 维度预算：12（PCA）+ 5（结构化）= **17 ≤ 20** ✅

**理由**：M5 是信号产生与标签定义周期，信息最直接；多周期一致性已由共振得分单列显式表达，无需 48 维冗余；单周期 PCA 拟合更稳定。

**§4.3 补充修正**：PCA 拟合样本应为 **bar 级**（历史 K 线根数，可达数万），**不是信号级**（仅约 440 条）——后者远不足以估计高维协方差。

**升级路径**：若离线验证证明多周期 embedding 有显著增量，可改为"每周期 3 维（4×3=12）"，但须重算维度预算并全量重训。

### 16.6 HP 因子消费改造 —— 风险评估与建议

**关键发现（现存隐患）**：训练侧与推理侧的 `adx_14`/`rsi_14` **早已不同源**：

- 训练侧：`indicators.adx_14`（`scheduler.py:2699`）
- 推理侧：`factor_raws.adx` = hexp `pf["_adx_raw"]`（`quality_scorer.py:525` / `hexp_engine.py:2047`）

这是"训练-推理口径不一致"的现存实例，加维前建议先统一。

**风险**：`factor_raws` 的 `adx/er/bbw/hurst/rsi` 与现有 39 维中的同名指标**语义高度重叠**（同一批指标的两种算法）→ 强共线性，增量信息可能接近 0；维度从 39 增至约 48，叠加 TimesFM 将达 65 维，而样本仅约 440 条 → 过拟合风险显著。

**建议**：先做离线验证（VIF + 单特征 AUC 增益），**增益不显著则不加**；且 HP 因子与 TimesFM 应**合并为一次维度评估**，避免 39→48→65 两次重训。

### 16.7 TimesFM 每日 T+1 增量抽取调度 —— B 方案落地（2026-08-30）

**决策（用户裁定）**：严格遵循规范，先跑架构、再积累样本。立即投产每日 T+1 增量抽取调度器，架构先运转、样本持续积累，13 天后（约 2026-09-11）按 §7.2 门禁重测。

**四层架构（与桥栈 / AI 评分侧完全对等）**：
1. 脚本本体 `tools/timesfm_daily_scheduler.py`：每日 21:30 UTC 触发（重叠 2 天防跨日漏抽），水位线对齐、`tmf_version=tfm25_pca_v1_sig` 幂等 upsert；`--once/--force-start/--force-end` 支持手动回填；Redis 心跳 `hcm:ai:timesfm:daily`（TTL 48h）。
2. 单实例 launcher `tools/timesfm_daily_launcher.py`：Global 互斥体 + 进程探测，幂等拉起。
3. OS 层守护 `tools/timesfm_daily_boot.ps1` + `register_timesfm_daily_guard.ps1` + 计划任务 `HCM_TimesFMDailyGuard`（每 10 分钟 + 登录时触发）；含 `_baseline/` 还原点自愈。
4. `tools/_baseline/`：脚本与产物（PCA / 检索库缓存）还原点。

**关键工程决策**：
- **检索库缓存增量一致性**（`timesfm_features.py --lib-cache`）：将 861 条历史向量物化到 `tools/models/tmf_hist_lib_v1.npz`，每批抽取按"信号时间严格早于本批首目标"过滤载入，确保任意时刻检索库恒等于"所有历史向量"，与一次性整批结果**逐项一致**（已实证：hist_sim min/avg/max = 0.0/0.9988/1.0 逐位复现，trend_cont 均值 -0.0186、rev_prob 均值 0.0310 不变）。该开关纯新增、幂等，未更改任何既有值。
- **铁律 5.1 守住**：调度器只产出离线特征，G0 影子模式不参与决策；质量头未过 §7.2 门禁前不得接入下单链路。
- **OS 守护对等桥栈**：与 `bridge_boot.ps1`/`HCM_BridgeGuard`、`ai_scorer_boot.ps1`/`HCM_AIScorerGuard` 同源同构，消除"关键进程只随 start.bat 手动启动、机器重启即静默消失"的整栈停机风险（2026-08-29 曾停机 1h22m）。

**实施中修复的缺陷（已写入代码注释）**：
- 调度器 `log()` 在 GBK 控制台打印含 U+FFFD 字符时二次崩溃 → 改为完全静默降级，日志落盘独立不受影响。
- launcher 进程探测未带 Name 过滤，会匹配自身派生的 PowerShell → 改为按命令行匹配 + 排除 powershell/wscript/cmd/conhost。
- 单实例锁只加在 launcher 短命进程上：计划任务与手动启动会各拉一个调度器并存 → 锁改加在长驻调度器本身（DAEMON + EXTRACT 两把互斥体，前者守生命周期、后者守抽取期）。
- venv 的 `pythonw.exe`/`python.exe` 为 uv 风格 shim（会再 exec 出 `python.exe` 子进程）→ 进程探测按命令行匹配而非进程名。

**验收**：
- 回填 861 行 + 物化缓存，回溯基线逐位一致 ✅
- 增量重抽（水位线回看 2 天）与整批等价 ✅
- 单实例：重复拉起被互斥体拒绝（`another scheduler daemon already holds the singleton mutex; exiting`）✅
- OS 守护：计划任务 + 登录触发，进程缺失自动重拉；心跳 `hcm:ai:timesfm:daily` 正常写入 ✅
- 当前状态：调度器经 `HCM_TimesFMDailyGuard` 运行中，每日 21:30 UTC 增量抽取。

---

## 附录 A：关键文件与代码位置索引

| 文件 | 作用 | 关键位置 |
|---|---|---|
| `tools/_model_feature_cols.py` | **特征契约单一真值（39 维）** | `MODEL_FEATURE_COLS`（当前磁盘丢失） |
| `tools/quality_features.py` | 离线特征装配 | `:50` MISSING_HEXP；`:195-252` 结构因子；`:482-487` ds 特征 |
| `tools/build_labels.py` | 标签构造 | `:140` label_one；`:221` dir_label_one；`:255` entry_label_one；`:355` build_state_labels |
| `tools/train_signal_quality.py` | 四头训练 | `:30` 契约 import；`:285-312` 状态头；`:314-369` 方向头；`:371-402` 买点头；`:404-558` 质量头；`:443-452` 契约 fail-fast；`:498-551` TSS |
| `tools/quality_scorer.py` | 在线推理 sidecar | `build_features`、`FEATURE_COLS` |
| `tools/auto_retrain.py` | 自动重训守护 | — |
| `tools/models/feature_baseline.json` | 特征基线（PSI 漂移检测） | 39 维 mean/std/p01/p50/p99/deciles |

---

*本文为 v1.0 草案，基于 commit `41b00e2` 审计编制。所有〔提案·待确认〕项未经用户确认不得实施；§15 阻塞项未裁决前，禁止改动任何生产代码。*
