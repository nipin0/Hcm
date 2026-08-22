# 方案设计：贴合行情分辨趋势/反转/震荡/预趋势 + 状态机动态调整 7 因子权重

版本：v1（2026-08-18）
状态：**待确认**（核心公式/架构分层改动，按铁律第二章须确认后实施）

---

## 一、问题与目标

### 1.1 现状
hexp 方向判定 = `dir_sum = Σ w·f`（7 因子 adx/er/ma/bbw/hurst/rsi/mm 的方向加权和，f∈[-1,+1]）。
权重当前由 `_select_factor_weights` 依 **M5 regime 离散三档**（trend/range/neutral）强度混合选择。

### 1.2 实证暴露的失衡（72h 生产数据）
| 因子 | BUY 均值 | SELL 均值 | 方向区分度 |
|---|---|---|---|
| ma | 0.947 | -0.768 | 最强 (+1.72) |
| adx | 0.775 | -0.654 | 强 (+1.43) |
| bbw | 0.390 | -0.771 | 强 (+1.16) |
| er | 0.554 | -0.626 | 强 (+1.18) |
| hurst | 0.178 | -0.437 | 中 (+0.62) |
| **rsi** | **-0.387** | **+0.420** | **反向（均值回归）** |
| mm | -0.003 | +0.012 | ≈0（方向无区分） |

**结论**：
1. ma/adx/er/bbw 是可靠方向因子；**rsi 方向相反**（超买逆势空票/超卖逆势多票，属均值回归制衡，防"高多低空"）；**mm 在主周期方向判定上无区分**。
2. 现有体制分类**没有独立"反转 REVERSAL"权重方案**：TREND_FADE（趋势衰竭=反转前兆）仍走 trend（顺趋势）权重，反转窗口内继续用顺趋势因子 → 趋势因子滞后误判方向。
3. 权重切换是"离散体制标签 + 单轴强度混合"（只在 neutral↔target 间插值），非真正的状态机平滑迁移。

### 1.3 目标
用**体制状态机**（含独立反转态）动态驱动 7 因子权重，使方向判定贴合实时行情阶段：趋势市顺趋势、反转市抓拐点、震荡市均值回归、预趋势市提前布局。

---

## 二、体制状态机设计

### 2.1 状态定义（5 态）
复合判定 `{M5 regime} × {周期方向态} × {反转标记}`，归一化为 5 个权重驱动态：

| 状态 | 触发条件（优先级从高到低） | 权重语义 |
|---|---|---|
| **REVERSAL 反转** | `_track_reversal` 命中（主周期 M5 TREND_UP↔TREND_DOWN 切换窗口，`reversal_include_primary=true` 已启用） | 抓拐点：升 rsi/mm，降 ma/adx（趋势滞后） |
| **PRE_TREND 预趋势** | M5 regime=PRE_TREND（ADX 起涨 3K + BBW 扩张 + 20-bar 突破） | 提前布局：升 adx，er/ma 中，降 rsi（勿追顶） |
| **TREND 趋势** | M5 regime∈{TREND} 且无反转 | 顺趋势：升 ma/adx/er，压 rsi（防逆势） |
| **RANGE 震荡** | M5 regime=RANGE | 均值回归：升 rsi/bbw，降趋势因子 |
| **NEUTRAL 中性** | 兜底（TREND_FADE 折中/未命中其它） | 均衡基线 |

**优先级裁决**：REVERSAL > PRE_TREND > TREND > RANGE > NEUTRAL。
- REVERSAL 优先：反转窗口内趋势因子必然滞后，权重必须转向反转/均值回归。
- TREND_FADE（趋势衰竭）不单列，折中计入：regime=TREND_FADE 且无反转 → 归 TREND 但 strength 降低；有反转 → 归 REVERSAL。

### 2.2 状态源
- **regime**：现有 `RegimeClassifier.classify`（PRE_TREND/TREND/TREND_FADE/RANGE/NEUTRAL，带 confirm/lock 防抖）——已具备，直接复用。
- **反转**：现有 `_track_reversal(symbol, period, state, hold_sec, momentum_flip)`——已具备，返回主周期是否处于 REVERSAL 窗口（`reversal_include_primary=true`）。
- 不需要新状态机类；把两个既有信号合并为 5 态决策，减少改动。

---

## 三、7 因子权重方案（每态一套，运行时可热调）

基于实测方向区分度 + 均值回归制衡原则：

| 因子 | TREND | PRE_TREND | REVERSAL | RANGE | NEUTRAL(基线) |
|---|---|---|---|---|---|
| adx | **28** | **30** | 18 | 9 | 25 |
| er | **26** | 24 | 20 | 9 | 25 |
| ma | **22** | 18 | 12 | 9 | 20 |
| bbw | 3 | 8 | 10 | **26** | 15 |
| hurst | 14 | 12 | 12 | 9 | 10 |
| rsi | 6 | 5 | **22** | **22** | 8 |
| mm | 11 | 10 | **16** | 16 | 15 |

**设计要点**：
- **TREND**：ma/adx/er 最高（方向区分最强），rsi 压到 6（防逆势）。相比生产（rsi=8）进一步降，提升顺趋势方向敏锐。
- **PRE_TREND**：adx 最高（ADX 起涨确认突破），er/ma 中，rsi 低（突破初期勿追顶）。
- **REVERSAL**：rsi 提到 22（抄底/逃顶均值回归）、mm 提到 16（动量反转确认），ma 降到 12（趋势 EMA 滞后）、adx 降到 18（ADX 在反转处失稳）。
- **RANGE**：保持生产（rsi 22/bbw 26），均值回归主导。
- **NEUTRAL**：保持生产均衡基线。

（数值为初稿，待影子回测/实盘样本微调。）

---

## 四、状态机动态调权（平滑迁移）

### 4.1 连续插值替代离散跳变
当前 `_select_factor_weights` 只在 neutral↔target 间 `(1-strength)×neutral + strength×target` 单轴插值。改为**多态连续混合**：

```
权重 = Σ_state  conf(state) × scheme(state, factor)
```
其中 `conf(state)` 为**状态置信度**（软分布，和=1）：
- 命中态 conf=1，其余 0（离散，最简单，先行落地）
- 进阶：沿状态图相邻插值（REVERSAL↔TREND 相邻、TREND↔PRE_TREND 相邻、TREND↔RANGE 相邻），避免状态切换瞬间权重跳变

### 4.2 平滑系数
复用现有 `regime_result.strength`（0-1，体制置信度）作为**向目标方案靠拢的程度**：
- strength 越高 → 越偏目标态权重
- strength 低（体制模糊）→ 向 NEUTRAL 基线靠拢，避免权重大幅摆动

### 4.3 防抖
- regime 已有 confirm/lock（`switch_*_confirm_bars`/`lock_bars`）防抖，不动。
- REVERSAL 窗口用现有 `reversal_hold_bars=3`（15min）维持，窗口内权重稳定在 REVERSAL。

---

## 五、落地改动清单

### 5.1 hexp_engine.py
1. **`factor_weights_json` 扩展为 5 方案**：`{trend, pretrend, reversal, range, neutral}`，每方案 7 因子。`_load_weight_schemes` 对缺失方案用 neutral 补齐（向后兼容旧 3 方案配置）。
2. **`_select_factor_weights` 重构**：
   - 输入追加主周期反转标记（`is_primary_reversal`）
   - 按 2.1 优先级选目标态 + 对应方案
   - 输出 = `(1-strength)×neutral + strength×target`（保留平滑），或进阶多态插值
3. **调用点**（produce 第 3 步）：把 `_track_reversal` 对主周期 M5 的结果传入 `_select_factor_weights`。

### 5.2 配置
- `hexp.factor_weights_json` 扩展（PG `hcm_config.metadata` + Redis `hcm:config:v2` 双写 + PUB）
- 新增 `hexp.rev.fade_maps_to_reversal`（默认 true，TREND_FADE 且无反转→是否降级为弱 TREND）

### 5.3 迁移
新建 `deploy/migrations/0017_hexp_factor_weight_state_machine.sql`：
- UPDATE `hexp.factor_weights_json` 为 5 方案
- 幂等（ON CONFLICT）

### 5.4 可观测性
- `sr.regime_weights` 已落库（当前方案权重），追加 `sr.regime_state`（当前 5 态）+ `sr.reversal`（是否反转窗口）
- 日志：状态切换打 `hexp state machine: TREND→REVERSAL (momentum)` 便于核对
- 前端 hexp 面板展示当前状态 + 生效权重（复用现有 regime_weights）

### 5.5 面板
- `web/api/hexp.py HEXP_KEYS` 确认 `hexp.factor_weights_json` 白名单在列（已含）
- 前端 HexpConfig 确认可编辑 5 方案（需扩展渲染，若只读则先仅后端生效）

---

## 六、验证计划

1. **离线单测**（纯函数）：构造 TREND/RANGE/REVERSAL/PRE_TREND 四场景的因子分 + regime，断言选态与权重正确。
2. **影子对照**：启用 shadow 前先观察 `regime_state`/`regime_weights` 分布，确认状态切换合理。
3. **回归基线**（改前记录）：当前 `dir_sum` 分布（BUY 0.447 / SELL -0.459）、各体制信号量占比。改后对比方向分离度是否提升、信号量变化。
4. **无回归验证**：reversal_include_primary 关闭时回退旧 3 方案行为。

---

## 七、风险与权衡

| 风险 | 说明 | 缓解 |
|---|---|---|
| REVERSAL 权重过度放大反转信号 | 反转窗口 15min 内频繁反向 | 反转也降手数（reversal_lot_mult=0.5 已有）；rsi/mm 权重上限可调 |
| 状态切换抖动 | regime/反转标签抖动致权重摆动 | confirm/lock + strength 平滑 + hold_bars 窗口 |
| 信号量变化 | 权重调整改变 dir_sum 符号分布 | 影子对照 + 分体制统计，先观察再全量 |
| 改变核心公式 | 7 因子权重是方向判定核心 | 需确认后实施；提供回滚（还原 factor_weights_json） |

---

## 八、需要确认的决策点
1. **5 态 vs 4 态**：是否保留 PRE_TREND 独立态（建议保留，突破初期与趋势中权重不同），还是并入 TREND？
2. **REVERSAL 权重侧重**：rsi 22/mm16 是否合适，还是要更强反转？
3. **离散 vs 多态插值**：先行离散（conf 0/1）还是直接上连续插值？
4. **TREND_FADE 处理**：归 REVERSAL（有反转时）还是独立？
5. **是否先影子后实盘**：建议先开影子/观测 `regime_state` 累积样本再全量生效。

请确认上述决策点后，我再实施。
