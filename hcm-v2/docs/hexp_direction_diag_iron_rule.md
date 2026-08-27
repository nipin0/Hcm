# 教训记录：方向「死标签 BUY / 全周期 RANGE / 方向闪烁」——诊断必须前后端联动显式化

> 固化日期：2026-08-27（多轮迭代：方向闪烁 → 死标签 → 全 RANGE → 预演端点 → 看板联动）
> 现象：明显下跌趋势，面板方向指针却**长期死黏 BUY**（"死标签"）；MTF 矩阵**长时间全显示 RANGE**（行情是趋势）；或方向在 BUY/SELL 间**高频闪烁**。

---

## 一、故障是如何产生的（机制链条）

这不是单个 bug，而是**「方向裁决 + 状态机 + 位置因子 + 前端显示」四层叠加**才显形：

### 第 1 层：方向裁决用硬阈值，无迟滞 → 闪烁
`hexp_engine.py` 的 `direction` 由 `dir_sum` 的**硬符号阈值**决定（`dir_sum>0→BUY`，`<0→SELL`，`≈0→NO_TRADE`）。
`dir_sum` 是多因子加权和（含 `f_mm_s` 等），在 0 附近因任一因子的微小波动就跨 0 翻转 → **direction 在 BUY/SELL/NO_TRADE 间抖**。

### 第 2 层：只加迟滞死区 → 死标签
第一轮修复给 `direction` 加了**迟滞死区**（`hexp.direction_hysteresis`，默认 0.06）：`|dir_sum|<死区` 时维持上一方向。
但这是**单点迟滞**——只防抖、不防黏。下跌趋势中位置因子把 `dir_sum` 顶在小幅正 → 迟滞**永远维持首次的 BUY** → **死标签**（行情已 SELL 却不翻）。

### 第 3 层：位置因子在趋势态强推反向 → 死标签根因
上一轮为治"高多低空"把 `pos_factor.weight` 提到 0.30。下跌趋势中 `_pos_cycle` 低位（如 0.1）→
`f_pos=(0.5-0.1)*2=+0.8` → `dir_sum += 0.30*0.8=+0.24` **强力推 BUY**，与真实 SELL 行情反向。
且 `anti_cancel` 调制**只在 RANGE/NEUTRAL 生效**，趋势态不调制 → 真实 ma/DI 的 SELL 信号无法对冲 → 方向被位置因子绑架。

### 第 4 层：regime 把下跌趋势标 TREND_FADE → 全 RANGE
成熟下跌趋势 ADX 从高位回落（容易连续 ≥3 根下降）→ `regime_classifier` 标 `TREND_FADE`。
但 `hexp_engine` 的 `_trend_regime` **只认 TREND/PRE_TREND，不含 TREND_FADE** → 当非趋势态 →
`ts` 公式中 `rsi/hurst` 项**带符号**（下跌中为负）→ 拉低 `trend_score` → 状态机进不了 TREND → **全周期 RANGE**。
（同时 `period_states` 全 RANGE 又让"MTF 周期共识翻转"失去数据，迟滞更不敢翻。）

### 第 5 层（本次核心教训）：前端只显示源值，不显诊断 → 问题不可见
前端 MicroPanel 指针、DecisionPanel 方向灯、ExecutionPanel 方向块**只用 `snap.direction` 画方向**，
但**不暴露 `dir_sum` / `prev_direction` / 迟滞状态**。结果：死标签、迟滞维持、正常翻转**在面板上长得一模一样**——
用户只能看到"方向不对"，无法区分"是引擎 bug 还是正常防抖"，定位与回归全靠读码，极慢。

---

## 二、第一轮误判的教训（重要）

最初把"方向闪烁"当成纯前端问题（MicroPanel 用 `snap.mm` 符号），改成用 `snap.direction` 后**仍抖**。
**原因**：后端 `direction` 本身就在抖（第 1 层硬阈值），前端换数据源治标不治本。
**再误判**：认为加迟滞死区就够（第 2 层），结果引出死标签（第 2/3 层）。
**真正确认根因是读码 + 跑通裁决链**：`pos_factor` 趋势态降权 + `TREND_FADE` 纳入趋势态 + 迟滞加强制翻转通道，三处联动才根治。

**教训**：方向类问题必须**同时看后端裁决链（dir_sum 构成、regime 语义、迟滞状态）和前端显示**，不能只在单层修；且**诊断信息必须显式化到看板**，否则下次回归无法一眼识别。

---

## 三、最终修复（已落地并验证）

### 后端（`hcm-signal-tower/signal_tower/hexp_engine.py`）
1. **`_DEFAULTS` 加 3 键**：`hexp.direction_hysteresis=0.06`、`hexp.direction_hysteresis_strong=0.20`、`hexp.pos_factor.trend_scale=0.25`（铁律 5/6 同步）。
2. **方向迟滞 + 强制翻转通道**（原硬阈值段）：`|dir_sum|>=0.20`（趋势级）或 MTF 周期共识反向 → 绕过死区立即翻；`|dir_sum|<0.06` → 维持防抖；中间地带保守维持。
3. **`pos_factor` 趋势态降权**：`regime_tag in (TREND/PRE_TREND/TREND_FADE)` 时 `pos_weight *= trend_scale(0.25)`，下跌趋势不再被位置因子强推 BUY。
4. **`TREND_FADE` 纳入 `_trend_regime`**：趋势衰减态仍按趋势处理，`ts` 公式取 abs、周期态正确判 TREND_UP/DOWN。
5. **`produce` 发布补诊断字段**：`dir_sum` / `dir_sum_factors`（7 因子分解）/ `dir_pos_factor` / `prev_direction`（供前端与预演端点诊断）。

### 后端 API（`hcm-web/web/api/hexp.py`）
6. **`GET /api/v1/hexp/hypothesis/{symbol}`**：读 live 快照 → `_hypothesis_preview()` 套用裁决链，输出 `predicted_direction` + `dead_label_risk`/`flip_blocked` 诊断。
7. **`GET /api/v1/hexp/hypothesis/scan`**：遍历 `hcm:live:hexp:*` 全品种，返回**死标签/漏翻风险榜**（自动选品种）。
8. **`HEXP_KEYS` 白名单同步** 3 个新键（铁律 5）。

### 前端（`hcm-web/frontend/src/pages/hexp/dashboard/`）
9. **`types.ts`**：`HexpSnapshot` 补 `dir_sum`/`dir_sum_factors`/`dir_pos_factor`/`prev_direction`；新增 `directionDiag(snap)` 共用诊断函数（输出 4 态：死标签风险/迟滞翻转放行/死区维持防抖/方向稳定）。
10. **`MicroPanel`（方向标签卡）**、**`DecisionPanel`（状态卡）**、**`ExecutionPanel`（执行预案卡）**：均接入 `directionDiag`，显式显示诊断标签 + `dir_sum`/`prev_direction`。

**验证（铁律 3）**：后端 `docker restart` 让 `produce` 发布新字段；前端 `npm run build` 重建 dist 并 restart web。
下跌品种三卡方向应一致指 SELL，诊断标签不再报"死标签风险"；`/hypothesis/scan` 风险榜应为空。

---

## 四、铁律（不可违背）

> **铁律 7（方向诊断三态必须前后端联动显式化，禁止"只画方向不显诊断"）**：
> - 任何"方向裁决"改动（`direction` / `dir_sum` / 迟滞 / 位置因子 / regime 语义），**必须同时把诊断字段发布到 live 快照**（`dir_sum`、`prev_direction` 等），且**前端必须把"死标签 / 迟滞维持 / 翻转放行 / 稳定"三态显式渲染**到方向相关卡片（方向标签、状态卡、执行预案卡）。
> - 禁止只显示 `snap.direction` 画指针而隐藏裁决过程——死标签、正常防抖、真翻转在面板上必须**可区分**（不同颜色/标签），否则下次回归无法一眼识别，定位成本回到"读码级"。
> - 方向裁决的"防抖"与"防黏"必须**成对实现**：有迟滞死区（`direction_hysteresis`）就必须有强制翻转通道（`direction_hysteresis_strong` 或 MTF 共识翻），否则单点迟滞必成死标签。
> - 位置因子（`pos_factor`）在趋势/反转态必须降权或禁用——它本意是 RANGE/NEUTRAL 均值回归抄底摸顶，**趋势态强推反向=死标签根因**。
> - `regime` 的"趋势衰减态"（如 `TREND_FADE`）必须纳入引擎的趋势态判定，否则成熟趋势被当非趋势 → `ts` 被符号项拉低 → 全周期 RANGE（与方向死标签同源）。

> **铁律 8（AI 质量模型版本与校准器必须统一，回测/验证须用生产 champion 模型）**：
> - **模型路径唯一真源**：生产模型由配置 `ai.lm.model_path` 指定（当前 champion = `tools/models/lgbm_quality_v53.txt`，经 `auto_retrain.py` 影子选举 adopt），**禁止用仓库 `_artifacts/lgbm_quality_v2.txt` 等历史遗留文件做验证或回测**——它们版本落后、特征契约可能漂移，结论对生产无效。
> - **校准器随模型走**：`calib_final.pkl` 等校准器必须与对应模型同版本（V53 须有其专属 calib）。退化校准器（等温回归小样本过拟合成 `{0,0.4,1}` 三档）会把 `p_raw` 压成常数 → `ai_score` 恒 40、模型形同摆设。加载时 `_calib_is_degenerate` 检测 + `CALIB_BLEND_W` 混合仅救分辨率，**治标不治本**，须重训校准器替换 pkl。
> - **回测/验证脚本固化模型来源**：从 `ai.lm.model_path`（或 `inference_log.model_version` 最新 champion）读真值，禁止硬编码旧版本号。
> - **产物命名带版本**：特征/标签/评估产物必须带模型版本后缀（如 `features_v53.csv` / `labels_v53.csv`），禁止无后缀或沿用旧 `v2` 命名与生产产物混淆。
> - **验证结论标注模型版本**：任何"置信度验证通过 / lift=xxx"结论必须写明所用模型版本；未在 V53（当前 champion）上验证的，不得作为生产决策依据。
> - **监测模型离线**：`inference_log.model_version=NULL` 占比过高 = sidecar 未加载模型（纯 HEXP 降级），须告警——模型离线期间所有"AI 质量"维度失效，信号质量回到无 AI 状态。

---

## 五、可复用教训清单（给团队）

1. **方向闪烁 ≠ 前端 bug**：先查后端 `direction` 是否本身抖（硬阈值/多因子跨 0），再查前端数据源。两层都要治。
2. **迟滞必须配强制翻转**：单点死区 = 死标签。翻转通道用"幅度阈值 + 周期共识"双条件，噪声级（<死区）才防抖。
3. **位置因子趋势态降权**：抄底摸顶逻辑在趋势中是与行情反向的，必须降权/禁用，否则绑架方向。
4. **regime 语义前后端要对齐**：`TREND_FADE` 这类"衰减但仍趋势"的标签，引擎各模块（状态机、`ts` 公式、方向裁决）必须一致处理，否则系统性误判。
5. **诊断要显式化，不要藏在代码里**：`dir_sum`/`prev_direction` 必须发布 + 前端渲染，让"死标签 vs 防抖 vs 翻转"一眼可分。回归防护靠"看得见"。
6. **预演端点做自动选品种**：`/hypothesis/scan` 遍历全品种跑裁决链，比人工盯盘更快发现死标签。

---

## 六、回归防护建议

- **CODE REVIEW 必查**：任何改 `direction`/`dir_sum`/迟滞/位置因子的 PR，是否同步 (a) 后端发布诊断字段 (b) 前端三卡接入 `directionDiag`。缺任一 = 违反铁律 7。
- **前端单测**：`directionDiag()` 覆盖 4 态用例（死标签：prev=BUY & TREND_DOWN & dir=BUY；翻转：prev≠cur 且强反转；防抖：|dir_sum|<0.06；稳定）。
- **后端自检**：`produce` 发布字典必须含 `dir_sum`/`prev_direction`，缺失即启动告警（防有人删字段导致前端诊断全"稳定"假象）。
- **监控**：`/hypothesis/scan` 可接定时任务，风险榜非空即告警（自动发现死标签品种），比人工排查快一个数量级。
- **铁律 3 复验**：前端改 `src` 不 `build` dist = 没改；后端 Python 改了未 bind mount/未 cp = 没改。验证用 `grep` 容器内运行文件 + `docker logs` 看 `dir=` 是否仍翻。
