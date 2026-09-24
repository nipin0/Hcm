# 方案：RANGE 态箱体独立模块（结合 HEXP）

- 日期：2026-09-18
- 状态：设计完成 → 已实施（零回归默认值）
- 授权：用户「继续完善方案设计，复核后实施」
- 变更性质：新增独立模块 + 门控口径开关（**默认 legacy = 行为不变**）

---

## 0. 结论先行

新建 `hcm-signal-tower/signal_tower/range_box.py`，把"箱体"从**各消费方各自内联计算**
上收为**唯一的几何模型实现**（纯函数 + frozen dataclass，无 IO）。hexp **零改动**：

- 输入侧复用 hexp 快照的 `period_states`（RANGE 判定单一真源）；
- 输出侧发布独立键 `hcm:live:range_box:{symbol}`（TTL 15s），供面板/归因/后续消费；
- 消费侧开关 `range.box.width_source` / `range.box.break_source` 默认 `legacy`
  ⇒ **上线当天行为与改前逐字节一致**，标定后再翻。

---

## 1. 现状取证

### 1.1 RANGE 注入全链路（`scheduler.py:_produce_signal` 3177-3360）

```
range.enabled(true) → _rng_cfg(逐键读 range_strategy.DEFAULTS)
  → _ps ← hexp 快照 period_states        （RANGE 判定真源 = hexp）
  → range_strategy.evaluate() → 方向      （MR 方向真源 = range_strategy）
  → break_guard（近 50 根 **exclusive** 极值）→ state.range_break_until
  → 宽度过滤（近 50 根 **inclusive** 极值 / ATR ∈ [1.5, 6.0]）→ _w_ok
  → 门：break_cooldown → skip ；not _w_ok → pass
  → armed 等回踩（offset 1.0×ATR，有效期 12 根）→ 用当前 bar h/l 判触及
  → _hexp_reason_overridable 守卫 → 注入 direction/threshold_passed/range_mode/tp
```

### 1.2 系统内"区间"口径共 6 套（本次要治的根因）

| # | 实现 | 窗口 | 含当前 bar | 边缘 | 消费方 |
|---|---|---|---|---|---|
| 1 | `hexp._get_donchian_pct` | 20 (`hexp.extreme.donchian_look`) | 否 | 极值 | 极值护栏 / `sr.position_in_range` |
| 2 | `hexp._get_cycle_position` | 60 (`hexp.cycle.look`) | 否 | 极值 | **hexp 位置因子 `f_pos`（权重 0.30）** |
| 3 | `scheduler` 宽度过滤 | **50** | **是** | 极值 | RANGE MR 宽度门 |
| 4 | `scheduler` break_guard | 50 | 否 | 极值 | RANGE MR 突破熔断 |
| 5 | `range_bonus.compute_range_position` | 30 | 否 | 极值 ∩ 布林(±0.5%) | 评分加成 `%b_range` |
| 6 | `state_strategy.compute_entry_box` | 12 (`state.box.window`) | 否 | **quantile 95/5** | FSM S1 箱体单 / 面板三线 |

⇒ 6 个窗口（12/20/30/50/60 + 50）、2 种边缘口径、2 种 inclusive 语义，互为"同义不同源"。

### 1.3 实测取证（XAUUSD M5，2026-09-18，ATR=4.095）

| 口径 | 点数 | ATR 倍数 | 过 `[1.5,6.0]`？ |
|---|---|---|---|
| 极值 / 12 根 exclusive（FSM 箱体口径） | 10.07 | 2.46 | ✅ |
| **quantile 95/5 / 12 根 exclusive（生产 `osc.bands_mode`）** | **9.23** | **2.25** | ✅ |
| 极值 / 50 根 exclusive（= break_guard 口径） | 46.77 | 11.42 | ❌ |
| quantile 95/5 / 50 根 exclusive | 41.98 | 10.25 | ❌ |
| **极值 / 50 根 inclusive（当前宽度过滤）** | **46.77** | **11.42** | ❌ |

连续日志（`RANGE_MR skip ... width=10.5~11.1ATR outside [1.5, 6.0]`）。

### 1.4 缺陷清单

| # | 缺陷 | 现场 |
|---|---|---|
| D1 | 宽度过滤（inclusive）与 break_guard（exclusive）**同叫 50 却不同口径** | 3226 vs 3246 |
| D2 | 窗口（4.2h）与策略持有期（TP 1ATR / arm 1h）**不匹配** ⇒ 系统性偏大 | 3249-3257 |
| D3 | **6 套区间口径**互为同义不同源 | §1.2 |
| D4 | **无箱体质量概念**：宽度合法 ≠ 真震荡（单边漂移也可能落在 [1.5,6]） | 缺 |
| D5 | 箱体**不可观测**：hexp 快照 32 键无任何箱体/位置字段 | 快照 |
| D6 | armed"触及"用当前 bar h/l，与箱体边界语义分离、无法审计 | 3296-3301 |

### 1.5 运营事实（关键）

`range.enabled = true`（**已启用、非影子**），但因 D1+D2，宽度过滤 **100% 拦截**
⇒ **一条已开启的通道被死锁**。本方案不是纯重构，而是恢复该通道。

---

## 2. 目标与非目标

**目标**
1. 箱体成为**唯一几何实现**（后续 6 套口径逐步收敛到它）。
2. 修 D1/D2（口径一致 + 双尺度：幅度 vs regime）。
3. 补 D4（质量模型：宽度 + 触碰 + 漂移 + Hurst）。
4. 补 D5（发布到 Redis，面板/归因可见）。
5. **hexp 零改动**、**默认零行为变更**、每一步可秒级回滚。

**非目标（本阶段刻意不做）**
- ❌ 不改 hexp 方向裁决（`dir_sum` / `f_pos`）——留作后续灰度（§4.3-L3）。
- ❌ 不取代 `range_strategy.mr_direction`（避免方向双真源）。
- ❌ 不绕过风控/AI 闸门/桥。

---

## 3. 模块设计 `range_box.py`

### 3.1 定位与边界

| 维度 | 设计 |
|---|---|
| 依赖 | 仅 `numpy`（与 `compute_entry_box` 同用 `np.percentile` ⇒ **口径同源可复现**） |
| IO | **无**（不读 Redis/PG、不下单）→ 由调用方注入 `cfg` dict |
| 风格 | 与 `range_strategy.py` 一致：`_f/_s/_b` 读配置、`DEFAULTS` 兜底、绝不抛 |
| 单测 | 纯函数，可离线逐用例验证 |

### 3.2 数据模型

```python
@dataclass(frozen=True)
class RangeBox:
    valid: bool;   reason: str
    upper: float;  lower: float;  mid: float
    width: float;  width_atr: float
    close: float
    pos: float            # (close-lower)/width，**未截断**（<0/>1 即突破）
    pos_clamped: float    # 截断 [0,1]（与 hexp pos_pct 语义兼容）
    pos_signed: float     # (0.5-pos_clamped)*2 ∈[-1,1]（与 hexp f_pos 同构，可直接喂 dir_sum）
    touches: int          # 边界带内 bar 数（上沿带 + 下沿带）
    cross_mid: int        # 窗口内穿越中值次数（真震荡证据）
    drift_atr_per_bar: float   # 收盘线性回归斜率 / ATR
    window: int;  bands_mode: str;  exclusive: bool;  atr: float
    def as_dict(self) -> dict   # 快照发布用（round 3/4 位）
```

### 3.3 几何口径（**双尺度**，治 D2）

| 尺度 | 键 | 默认 | 语义 | 用途 |
|---|---|---|---|---|
| `fast` | `range.box.window`(+`.{symbol}`) | 12 | 当前震荡**幅度** | 宽度下限（TP 1ATR 装得下吗）、贴边确认、TP 锚点 |
| `slow` | `range.box.window_slow` | 50 | 是否**仍是**震荡 | 突破判定、regime 一致性、宽度上限 |

- `exclusive=True`（默认）：切片 `arr[n-1-window : n-1]`，**与 `compute_entry_box` 完全一致**
  → 结构性消除 D1 的 inclusive 自我抬高。
- `bands_mode="quantile"` + `q_high/q_low=0.95/0.05`：削插针（实测 10.07→9.23 点，−8%），
  与生产 `osc.bands_mode=quantile` 同口径。

### 3.4 质量模型（治 D4；4 分项，各 ∈[0,1]）

| 分项 | 定义 | 直觉 |
|---|---|---|
| `q_width` | `min<=width_atr<=max` → 1；否则按 `min/w` 或 `max/w` 线性衰减 | 太窄装不下 TP、太宽非震荡 |
| `q_touch` | `min(1, touches / touch_min)` | 边界被多次拒绝 = 真箱体 |
| `q_drift` | `1 - min(1, |drift_atr|/drift_max_atr)` | 单边漂移 → 不是震荡 |
| `q_hurst` | `hurst<=0.5` → 1；否则线性衰减到 0 | 均值回归态 |

`quality = Σ(w_i·q_i)/Σ(w_i)`（`hurst` 缺失时该项**剔除并重归一**，不用中值拉低）。
权重 `_QUALITY_W = {width .35, touch .25, drift .25, hurst .15}` 为 **纯混合系数（非交易阈值）**，
刻意不做成配置键；若需调参再提升（见 §8 待办）。

### 3.5 API 清单（纯函数）

```python
compute_box(high, low, closes, atr, cfg=None, *, scale="fast", symbol=None) -> RangeBox
compute_scales(high, low, closes, atr, cfg=None, *, symbol=None) -> (RangeBox, RangeBox)
gate_scale_box(boxes, cfg=None) -> RangeBox          # 取 range.box.gate_scale 指定尺度
quality(box, cfg=None, *, hurst=None, er=None) -> (float, str)
width_gate(box, cfg=None) -> (bool, str)             # 读 range.width_min_atr/max_atr
break_check(box, close, cfg=None) -> (bool, str)
confirm_direction(box, direction, cfg=None) -> (bool, str)   # 位置一致性（不产方向）
```

`confirm_direction` 的设计取舍：**方向仍由 `range_strategy.mr_direction` 单一裁决**，
箱体只回答"这个方向与箱体位置矛盾吗"（BUY 需贴下沿、SELL 需贴上沿，容差 `edge_tol_atr`）
⇒ 避免 D3 式双真源复发。

### 3.6 配置键（`range.box.*`；PG/Redis 权威、DEFAULTS 兜底、热调）

| 键 | 默认 | 说明 |
|---|---|---|
| `range.box.enabled` | `true` | 模块计算/发布总开关 |
| `range.box.window` / `.window.{symbol}` | `12` | 快箱窗口（品种级可覆盖） |
| `range.box.window_slow` | `50` | 慢箱窗口 |
| `range.box.bands_mode` | `quantile` | `extremum`\|`quantile` |
| `range.box.q_high` / `.q_low` | `0.95` / `0.05` | 分位边缘 |
| `range.box.exclusive` | `true` | 只用已收盘 bar |
| `range.box.min_width_atr` | `1.0` | 退化保护（窄于此无意义） |
| `range.box.drift_max_atr` | `0.30` | 漂移上限（ATR/根） |
| `range.box.touch_min` | `2` | 触碰次数下限 |
| `range.box.wick_atr` | `0.25` | 触碰判定容差（ATR） |
| `range.box.edge_tol_atr` | `0.50` | 贴边确认容差（ATR） |
| `range.box.break_buf_atr` | `0.0` | 突破判定缓冲（ATR） |
| `range.box.quality_min` | `0.50` | 质量门 |
| `range.box.width_source` | **`legacy`** | `legacy`\|`box`（宽度过滤口径） |
| `range.box.break_source` | **`legacy`** | `legacy`\|`box`（熔断口径） |
| `range.box.gate_scale` | `slow` | `fast`\|`slow`（宽度门用哪个尺度） |
| `range.box.publish` | `true` | 发布 `hcm:live:range_box:{symbol}` |

上下限阈值**复用既有** `range.width_min_atr` / `range.width_max_atr`（不加键）。

---

## 4. 与 HEXP 的结合

### 4.1 数据流

```
                 ┌──────────── hexp_engine.produce()（零改动）────────────┐
                 │  快照 hcm:live:hexp:{sym}  ── period_states / atr / rsi │
                 └───────────────────────┬────────────────────────────────┘
                                         │ L1 输入（读 period_states 判 RANGE）
                                         ▼
  indicators(M5) ──► range_box.compute_scales() ──► (fast, slow)
                                         │
                    L2 输出 ─────────────┴──► hcm:live:range_box:{sym}（TTL15s）
                                         │            （面板 / 归因 / 后续消费）
                    L3 消费（开关）───────┴──► scheduler RANGE MR 门控
                                                 range.box.width_source=box|legacy
```

### 4.2 三级接缝

- **L1 输入（本次实施）**：RANGE 判定**继续用 hexp 的 `period_states`**（已是权威），
  `range_box` 不自造 regime 判定 ⇒ 不新增第 7 套口径。
- **L2 输出（本次实施）**：发布独立键，**不改 hexp_engine.py**（217KB 关键路径，改动风险不成比例）。
- **L3 消费（本次实施，默认 legacy）**：`scheduler` 宽度过滤 / 熔断可切到箱体口径。

### 4.3 后续（本阶段不做，留证据后灰度）

| 级别 | 内容 | 风险控制 |
|---|---|---|
| L3+ | `hexp.pos_source = legacy\|box`：位置因子 `f_pos` 改用 `box.pos_signed` | 灰度对比 `pos` vs `pos_cycle` 一致率 |
| L3++ | `range_bonus.compute_range_position` 改为箱体薄封装（保留 API） | 评分对照 |
| L4 | 6 套口径收敛到 `range_box`（删冗余实现） | 逐项开关 |

---

## 5. 分期实施与回滚

| 期 | 内容 | 风险 | 回滚 |
|---|---|---|---|
| P1 | 新建 `range_box.py`（无引用） | 零 | 删文件 |
| P2 | 快照发布 `hcm:live:range_box:{symbol}` | 零（仅多写一个键，try/except 包裹） | `range.box.publish=false`（秒级） |
| P3 | 离线标定 → 定 window/gate_scale/width_min/max | 零（不改生产） | — |
| P4 | 切换 `width_source=box` / `break_source=box` | 中（会放开被拦行情） | 置回 `legacy`（秒级，无需重启） |

**整体回滚**：`git checkout scheduler.py` + `docker restart hcm-v2-hcm-signal-tower-1`。

---

## 6. 复核结论（实施前）

| # | 复核项 | 结论 | 对方案的影响 |
|---|---|---|---|
| 1 | `compute_entry_box` 切片语义 | `seg=high[n-1-window:n-1]`，与 `exclusive` 一致 | ✅ 可直接复现，同源 |
| 2 | 仅换口径能否解卡？ | **否**：慢箱 quantile 50 根仍 10.25 ATR > 6.0 | ⚠ 修正：解卡**必须**靠 P3 重标定/缩窗 |
| 3 | 方向判定是否并入箱体？ | 会与 `mr_direction` 双真源 | ⚠ 修正：只做 `confirm_direction` 位置校验 |
| 4 | 快照发布位置 | hexp_engine:2962-3016 内 | ⚠ 修正：改独立键，hexp 零改动 |
| 5 | 生产配置核实 | `range.enabled=true`、`donchian_look=20`、`pos_factor.weight=0.30`、`trend_scale=0.0`、`osc.bands_mode=quantile` | ✅ 设计对齐实际 |
| 6 | 命名空间冲突 | `range.box.*` 在生产 Redis **不存在** | ✅ 干净可用 |

---

## 7. 验证指标

**P2（影子）**
- `hcm:live:range_box:{sym}` 每 bar 刷新；`fast.width_atr` 落在 [1.5,6] 的比例；
- `quality` 分布；`pos` vs hexp `pos_pct` 一致率。

**P4（生效）**
- `RANGE_MR skip ... width` 占比（应从 ~100% 显著下降）；
- `RANGE_MR arm` / `filled` / `break-guard` 计数；
- 注入单前视 E[R]（按 width/quality 分档）；
- 无新增 Traceback / contract_mismatch。

---

## 8. 待办（本方案外，留痕）

1. `_QUALITY_W` 若需调参 → 提升为 `range.box.qw_*` 配置键。
2. L3+ `hexp.pos_source` 灰度（需 P2 累计 ≥ 1 周一致率证据）。
3. `range_bonus.py` 的 30 根 `%b_range` 与箱体合并（L3++）。

---

## 9. 离线标定结果（2026-09-18，XAUUSD M5 60 天）

**样本**：14,230 根 M5（2026-07-20 ~ 09-18）；触发（RSI≤30/≥70）1,398 件，**已了结 758**
（armed 1.0ATR 未成交/未了结 640）。判据：armed 入场 + 双障碍（TP 1.0ATR vs SL 1.0ATR，同根双触计负），
扣 0.12ATR 往返成本。脚本：`tools/_scratch/range_box_calib.py`。

### 9.1 分箱（E[R] 随宽度单调下降的是"慢箱"）

| 慢箱 width(50 根 quantile exclusive) | n | E[R] |
|---|---|---|
| <1.5 | 10 | −0.286 |
| 1.5-3 | 42 | **+0.446** |
| 3-6 | 268 | +0.193 |
| 6-9 | 305 | +0.235 |
| 9-12 | 95 | +0.071 |
| ≥12 | 38 | +0.034 |

| 快箱 width(12 根) | n | E[R] |
|---|---|---|
| 1.5-3 | 117 | +0.160 |
| 3-6 | **521** | +0.193 |
| 6-9 | 94 | +0.273 |

**结论**：**只有慢箱有判别力**（分箱单调）；快箱 69% 样本挤在 3-6 档、E[R]≈基线
⇒ 快箱**不可做宽度门**（≈不过滤），只能做"TP 装得下吗"的下限。

### 9.2 门控对比（同批 758 件）

| 门 | n | 覆盖 | E[R] | CI95 |
|---|---|---|---|---|
| **旧口径 [1.5,6.0]（当前生产）** | 53 | **7.0%** | +0.415 | [+0.218,+0.612] |
| 慢箱 [1.5,6.0] | 310 | 40.9% | +0.227 | [+0.135,+0.319] |
| **快箱≥1.0 + 慢箱∈[1,9]** | **616** | **81.3%** | **+0.238** | **[+0.173,+0.303]** |
| 快箱≥1.0 + 慢箱∈[1,9] + SELL 未破上沿 | 374 | 49.3% | +0.280 | [+0.198,+0.361] |
| 不过滤（基线） | 758 | 100% | +0.194 | [+0.135,+0.254] |

### 9.3 读法与推荐

1. **当前生产的真实病症是"覆盖 7%"**，不是"单笔质量低"：旧门选出的 +0.415 仅 53 样本、
   CI 与其它门**重叠**（[+0.218,+0.612] vs [+0.198,+0.361]）⇒ 不能断言其质量更高，
   但覆盖差 7~12 倍是确定的。**7% × 3 个月 ≈ 通道事实死锁**（与 §1.5 一致）。
2. **推荐门 = 双尺度**：快箱 width_atr ≥ 1.0（TP 可行性）+ 慢箱 width_atr ∈ [1.0, 9.0]
   （避开 ≥9 的"已非震荡"尾档，其 E[R] 仅 +0.034~+0.071）。
3. **`quality` 不上门**：quality≥0.5 覆盖 745/758 ≈ 恒真（其 width 分项与门**共线**），
   实测无判别力 ⇒ 仅作观测/排序（设计照此实施）。
4. **越界否决不对称、暂不上门**：SELL 破上沿 +0.152(296) < 未破 +0.221(462)；
   BUY 破下沿 +0.278(183) **>** 未破 +0.168(575) ⇒ 效应反向、有单窗口过拟合风险，
   故 `range.box.require_unbroken` **默认 off**，先累计样本。
5. **诚实标注**：E[R] 提升幅度有限（+0.194 → +0.238，+23%），且为**单一品种/单一 60 天窗口**；
   本方案的主要收益是**恢复通道覆盖**，不是提高单笔期望。

### 9.4 建议配置（待用户拍板后再落）

```
set_cfg.py range.box.width_source box      # 门控口径切箱体
set_cfg.py range.box.break_source box      # 熔断口径切箱体
set_cfg.py range.width_max_atr 9.0         # 6.0 → 9.0（新口径下重标定）
set_cfg.py range.width_min_atr 1.0         # 1.5 → 1.0（下限改由快箱承担）
```

---

## 10. 实施记录（2026-09-18）

### 10.1 交付物

| 文件 | 说明 |
|---|---|
| `hcm-signal-tower/signal_tower/range_box.py` | **新增**（纯逻辑、无 IO，`DEFAULTS` 22 键） |
| `hcm-signal-tower/signal_tower/scheduler.py` | 接线：import + 箱体计算/发布 + 两个口径开关 + 可选越界否决 |
| `docker-compose.yml` | **补挂载** `range_box.py`（新文件非镜像烘焙，缺则 recreate 后 import 崩溃） |
| `tools/_scratch/test_range_box.py` | 复核用例（32 项，含 None 回归） |
| `tools/_scratch/range_box_calib.py` | 离线标定脚本 |
| 本文件 | 设计与实施记录 |

### 10.2 新增配置键

除 §3.6 外，实施中补 1 键：`range.box.publish_ttl_sec`（默认 **600**，见 §10.4 缺陷 1）。

### 10.3 复核自证（32/32 通过）

- **口径一致性**：12/20/50 × {quantile, extremum} 三线与 `state_strategy.compute_entry_box`
  **逐位一致**（如 w=12 quantile：box=(4273.6280, 4266.8219, 4270.2250) = fsm 同值）。
- exclusive/inclusive 语义、字符串 `'false'` 解析、pos 未截断/截断/符号、
  宽度门/突破/方向一致性、quality 重归一、数据不足/关闭/退化保护、双尺度 + gate_scale + as_dict。

### 10.4 实盘验证中发现并修复的 2 个缺陷（诚实记录）

| # | 缺陷 | 现象 | 根因 | 修复 |
|---|---|---|---|---|
| 1 | 发布 TTL 过短 | 观测键 95% 时间不存在（面板恒空） | 误照抄 hexp 快照的 `ex=15`，但本键**每根 M5 才刷新**（300s） | 新增 `range.box.publish_ttl_sec=600`（≥2×bar） |
| 2 | **`_b()` 不处理显式 None** | 快照恒 `valid=false / reason="disabled"` | scheduler 逐键读配置时写入**显式 None**（非"缺键"）⇒ `dict.get(k, default)` 返回 None ⇒ `bool(None)=False`，把**默认开启**的开关静默关掉（`_f`/`_s` 都处理了 None，`_b` 漏了） | `_b` 补 None → DEFAULTS 回落；并加回归用例 §6b |

> 缺陷 2 是典型的"守卫看起来装了、实际没生效"事故模式（与 range_strategy B11、config_provider
> get_current 同类）。**新读值函数必须与既有三个（`_f/_s/_b`）口径一致** —— 已写入代码注释。

### 10.5 部署与验证

- `docker compose up -d --no-deps hcm-signal-tower`（**重建**使新挂载生效）+ `docker restart`。
- 容器 healthy；`range_box.py` 挂载确认；`py_compile` 通过；md5 容器=宿主机；无 Traceback/ImportError。
- **零回归实测**：`RANGE_MR skip XAUUSD: width=10.22/10.42/10.59ATR outside [1.5, 6.0]`
  在 08:55:26 / 09:01:01 / 09:05:01 逐字保持 legacy 格式；hexp 快照 TTL 15 未受影响。
- **观测键实测**（`hcm:live:range_box:XAUUSD`，TTL 387）：
  ```
  fast: window=12 quantile exclusive  width_atr=2.968 pos=0.792 touches=5 cross_mid=4 drift≈0.0003
  slow: window=50 quantile exclusive  width_atr=9.367 pos=0.894 touches=13 cross_mid=1 drift=0.1466
  width_source=legacy  break_source=legacy  period_states={5 周期全 RANGE}
  ```

## 11.5b 【切换后复查·缺陷】箱体突破熔断 × RSI 极值的冲突（2026-09-18 发现）

**问题**：切 `break_source=box` 后，突破熔断用**慢箱 quantile 95/5** 边界判定，
而 RSI 极值（= 成交量能条件）天然发生在价格贴近/越过边界处 ⇒ 两者在同一根 bar 撞车。
量级（最近 3,000 根 M5，258 件 RSI 极值事件；脚本 `tools/_scratch/range_box_conflict_check.py`）：

| 口径 | 全样本收盘破沿率 | **RSI 极值当根被判破沿** |
|---|---|---|
| 旧口径（近 50 根**极值**，不含当前） | 6.9% | **46.5%** |
| 箱体（慢箱 **quantile 95/5**） | 18.0% | **82.9%** ← 本次切换引入（+36.4pp） |

**后果**：熔断立即设 `range_break_until`（冷却 12 根）⇒ 当根 `else:`（arm/注入）**整段跳过**。
即 **82.9% 的 RANGE 建仓机会在"产生的那一根"就被自己的熔断作废**。

**活到 arm 的比例（宽度门 ∧ 未破沿）**：

| 方案 | 比例 |
|---|---|
| 当前生产（门 ∧ ¬箱体 quantile 破沿） | **15.5%** |
| 方案A：突破判定改用**极值**边界（`bands_mode=extremum`） | **48.1%** |
| 方案B：箱体边界 + **连续 2 根越界**才算破沿 | **66.3%** |
| 参考：加缓冲 0.25/0.50 ATR | 74.8% / 67.8%（**仍破**，单独不够） |

**根因（设计错误，须记录）**：`quantile 95/5` 削掉插针 ⇒ 边界落在极值**内侧**。
该口径适合**宽度/位置/质量**（稳健性优先），但**不适合突破判定** ——
"突破"的定义本就是**创新极值**，旧实现用极值边界正是因此。两者混用一个箱体是本次缺陷来源。

**修复（A+B，2026-09-18 用户授权「按 A+B 修」→ 已实施）**：

| 项 | 内容 |
|---|---|
| **A** | 新增 `range.box.break_bands_mode`（默认 **`extremum`**）+ `compute_box(..., bands_mode=...)` 覆盖参数；突破判定改用**独立的极值箱体** `_box_break`，与宽度门的 quantile 箱体分离 |
| **B** | 新增 `range.box.break_confirm_bars`（默认 **2**）+ 纯函数 `break_streak_update()`；**连续 N 根同向越界**才认定突破（单根 = 回踩） |
| 去重 | 连续计数须"每根只计一次" → 用 bar 键 `klines[-1]["open_time"]`（同 `_live_score_publisher` 既有做法）防 bar 内重入重复计数；per-symbol 状态 `state.range_break_{side,streak,bar,last,last_why}`（动态属性，同 `range_arm`/`range_break_until` 惯例） |
| 观测 | 快照新增 `break`（extremum 箱体三线）+ `break_side` / `break_streak` |
| 配置 | 两键已 `set_cfg.py` 种入配置中心（面板可见/可回滚）；`break_confirm_bars=1` = 退回单根判定 |

**回归用例**：`tools/_scratch/test_range_box.py` 新增 §6c（bands_mode 覆盖、streak 1/2 根、回箱清零、
反向重置、`confirm_bars=1` 回退），全套 **38/38 通过**。

**生效验证（重启后首批 bar）**：
```
快照 break(extremum): upper=4399.570 lower=4370.450 width_atr=7.584
      slow(quantile): upper=4398.145 lower=4370.850 width_atr=7.109
      break_side="" break_streak=0     ← 极值边界在 quantile 边界**之外**（A 生效）
09:46:01 RANGE_MR arm XAUUSD: BUY target=4370.15 (close=4374.15 off=1.00×atr=4.00)
         ← ★ 该通道**9 天来第一次真正 arm**（切换前恒被宽度门拦死）
09:50:07/12 RANGE_MR skip XAUUSD: range_no_extreme (rsi=30.1/30.6)
         ← 无 width-skip、无 break_cooldown → 门控与熔断均已不再误拦
```
> 诚实标注：A+B 把"活到 arm"从 15.5% 提到 **66.3%**（离线），但**仍未**验证实际成交——
> 需前向观察 `arm filled` 计数与成交后 E[R]（见 §11.6）。

---

## 11. P4 切换执行记录（2026-09-18，用户授权「切生产」）

### 11.1 执行（`set_cfg.py`，唯一写入口 PG SoT → Redis → PUB 失效）

| 顺序 | 键 | before | after |
|---|---|---|---|
| 1 | `range.box.width_source` | `None` | `box` |
| 2 | `range.box.break_source` | `None` | `box` |
| 3 | `range.width_min_atr` | `1.5` | `1.0` |
| 4 | `range.width_max_atr` | `6.0` | `9.0` |

**为什么先切口径、后放宽阈值**：若先改阈值，legacy 口径会短暂以 `[1.0,9.0]` 运行
（标定显示 legacy `[1.5,12.0]` 覆盖已达 88.9%）→ 出现一次**突发放量**。
先切口径则中间态是 box `[1.5,6.0]`（+0.227）→ 再单调放宽，全程无过冲。

### 11.2 生效验证

- Redis 4 键回读全部为期望值；`hcm:live:range_box:XAUUSD` 快照 `width_source="box"`、`break_source="box"`。
- 日志口径切换干净：
  ```
  09:10:11  RANGE_MR skip XAUUSD: width=10.84ATR outside [1.5, 6.0]      ← legacy（切换前）
  09:15:25  RANGE_MR skip XAUUSD: box width slow_too_wide(9.35>9.0)      ← box（切换后）
  ```
- 无 Traceback / `range_box compute failed` / `Redis SET failed`；容器 healthy；hexp 快照 TTL 正常。

### 11.3 门控体检（`tools/_scratch/range_box_gatecheck.py`，最近 400 根 M5）

```
箱体门通过率: 325/400 = 81.2%          ← 与标定预测 81.3% 几乎完全吻合
慢箱 width_atr: p10=4.67 p50=7.48 p90=9.77 max=17.76
拦截原因: {'slow_too_wide': 75}        ← 无 too_narrow（下限 1.0 几乎不触发）
```

**读法**：门**非恒闭**（81% 通过）。切换当刻之所以仍 skip，是因为当前慢箱 9.35 落在
上尾（p90=9.77）——这是**标定意图**（9-12 档 E[R] 仅 +0.071、≥12 仅 +0.034）。

### 11.4 回滚（秒级，无需重启）

```
set_cfg.py range.box.width_source legacy
set_cfg.py range.box.break_source legacy
set_cfg.py range.width_max_atr 6.0
set_cfg.py range.width_min_atr 1.5
```

### 11.5 通道活性实证：Magic=55 九日零成交（印证 §1.5）

**Magic=55 的归属**（单一真源，无歧义）：`signal_publisher.SIGNAL_MODE_MAGIC["range"]=55`，
由 `score_result.range_mode` 显式覆盖进入 magic；而 `range_mode` **全仓仅 1 处写入**
（`scheduler.py:3435`，即 `range_mr` 注入块），其余 8 处皆为读取
⇒ **Magic=55 ⟺ 本次箱体接入点**。

**切换前实测（DB 查询）**：

| 查询 | 结果 |
|---|---|
| `hcm_trading.positions WHERE magic=55` | **0 笔**（历史全量） |
| `hcm_signal.signals WHERE fallback_reason LIKE 'range_mr%'` | **0 行** |
| `hcm_signal.signals WHERE indicator_values->>'range_mode' = true` | **0 行** |
| 近 7 天 magic 分布 | 空141 / 62021101:20 / 62021201:19 / 61010200:4 / **11:4** / 21:2 / 6105**:4 …**无 55** |

**配置时间线**（`hcm_config.metadata`）：`range.enabled` 自 **2026-09-09 13:07:54** 起即为
`true`（创建时已翻）；`range.width_min/max_atr=1.5/6.0` 自 2026-09-10 09:46 起。

**结论**：该通道**已开启 9 天、产出 0 笔成交、0 行信号** —— 全部被 old 宽度口径拦在注入之前
（每 bar 可见 `RANGE_MR skip ... width=10.xATR outside [1.5, 6.0]`）。这正是 §1.5
"已开启却事实死锁"的直接实证，也是本次切换的预期收益所在。

> ⚠ 注意区分：Magic=11（HEXP 引擎**自身**在震荡市出的单，近 7 天 4 笔）**不受本次改动影响**
> —— `hexp_engine.py` 零改动，箱体未参与其 `pos`/`f_pos`/评级（属 §4.3-L3+）。

### 11.6 待观察与可选后续

1. **前向观测（不影响成交链路）**：`RANGE_MR arm/filled` 计数、注入单前视 E[R]、
   `break-guard(box)` 命中次数、`slow_too_wide` 占比。
2. **可选**：若生产长期处于宽区间（p90=9.77 表明约 13% 的 bar 被 ≤9 拦掉），
   可评估放宽到 `range.width_max_atr=12.0`（标定：覆盖 94.3%，E[R]=+0.211,
   CI [+0.150,+0.272]）—— 用 +0.022 的单笔 E[R] 换 12.5% 覆盖。**当前维持 9.0**。
3. `range.box.require_unbroken` 维持 `off`（§9.3 第 4 条：效应不对称，先累计样本）。

---

## 12. 【用户口径】注入豁免白名单 + 箱体内单仓（2026-09-18，用户授权「只豁免 #3 + 本轮箱体未平标记」）

### 12.1 背景：切换后仍 0 笔的**真因**（与 §11.5 的"宽度门"不是同一个闸）

P4 切换后宽度门已放开（§11.3 通过率 81.2%），但 `Magic=55` 仍是 **0 笔**。逐层取证定位到**下一道闸**：

| # | 证据（2026-09-18 实测） | 结论 |
|---|---|---|
| 1 | 近 48h 日志 **142 条 `RANGE_MR`**，**全部**为 `range_no_extreme`（rsi=31.8~51.2） | RANGE 段**在跑**；未走到宽度/熔断段（RSI 未达极值 ⇒ `_rng["direction"]` 为空） |
| 2 | PG：`signals WHERE signal_mode='HEXP:Regime.RANGE'` 最后一条 = **09-16 23:53** | A2 修复（09-17）之后，RANGE 通道 **40 小时零信号** |
| 3 | `hexp.entry_gate.overridable_reasons` 生产值 = **`hexp_no_direction`** | 注入前守卫 `_hexp_reason_overridable`（`scheduler.py:3553`）只放行"hexp 自身没方向" |
| 4 | RANGE 是**逆微动量**入场 ⇒ 必然命中 `hexp_momentum_flip(BUY but mm=-0.30…)` | **风格闸把策略锁死**：RANGE 只在自己该进的时刻被拦住 |

**为什么这是"自相矛盾"而非"风控有效"**：`quality_gate` 早已按同一理由豁免了
`pullback_chase` / `dir_resonance` / `entry_resonance`（`quality_gate.py:356/375/401`，
注释原话"RANGE 均值回归**天生逆动量**"）⇒ 风格层已认、注入层未认，属**实现缺口**。

### 12.2 改动（2 项；**风控链零改动**，按用户决策 #6/#7/#8 一律保留）

| 项 | 内容 | 开关 |
|---|---|---|
| 豁免 | `scheduler` 的 RANGE 注入守卫改用 **RANGE 专属白名单** `range.hexp_override_reasons`（**不动全局** `_hex_ovr_wl` ⇒ `micro_state` / `value_drive` 两处注入行为逐字节不变） | 热调 |
| 单仓 | 三级判据（见 §12.4）：① `positions.magic=55` 计数 ≠ 0 → 拦（**持仓为真值 ⇒ 塔重启自愈**）；② 标记在位且未过 `_RANGE_ROUND_GRACE_SEC`(420s) → 拦（防 bar 内重入）；③ 标记在位 ∧ 已过宽限 ∧ 计数=0 → 清位（= 平仓） | `range.single_position`（默认 true） |

### 12.3 默认白名单：**全豁免**（`"*"`）—— 只保留「箱体单仓」这一条约束

> **口径二次修订（用户原话，2026-09-18）**：「Range（Magic=55）箱体一单约束，**其他的豁免**」。
> 初版我按"最小改动"只放行了风格/质量族、刻意保留"接刀/位置"族；本次按用户决策**改为 `"*"`**。
> 两条口径的差异只在一个配置键的默认值上（代码零改动 ⇒ 秒级互切）。

| 维度 | 口径 |
|---|---|
| 默认值 | `range.hexp_override_reasons = "*"` ⇒ `_hexp_reason_overridable` 直接放行**任意** hexp 否决原因 |
| **仍保留** | ① **箱体内单仓闸**（§12.4）；② **风控链全链**（confidence / max_lot / max_pos / daily_loss / margin / cool_minutes）；③ `range.enabled` 一键总开关 |
| **一并放开**（诚实留痕） | `hexp_extreme_reversal`（极值+动量反向+长影线 → 接刀）、`hexp_cycle_pos_guard`（周期极值位置）、`hexp_zone_guard`（贴脸强阻力追多/支撑追空）、`hexp_entry_gate`（含 `TREND_ACCEL 逆加速/逆H1 **不接刀**`、`TREND_EXHAUST 衰竭末端不追`；近 7 天最高频 526 次）、`hexp_pullback_gate` |
| 不受影响 | `micro_state` / `value_drive` 两处注入仍用**全局** `_hex_ovr_wl`（生产值 `hexp_no_direction`）⇒ 其行为逐字节不变 |

**为什么"全豁免"在本策略自洽**：RANGE 的入场是**箱体边缘逆势**（离线标定 19,143 根 M5：
对称 1:1 胜率 70~81%、随机基线 46%），与 hexp 那些"顺势 / 不接刀"风格闸取向**天然相反**；
`quality_gate` 侧早已按同一理由豁免了 `pullback_chase` / `dir_fuse` / `entry_fuse`
（`quality_gate.py:356/375/401`）—— 本次只是把同一口径补齐到**注入层**。

**收回/收紧（秒级，无需重启）**：
```
set_cfg.py range.hexp_override_reasons "hexp_no_direction,hexp_momentum_flip,hexp_grade_red"
```
> 前缀清单语义仍精确生效（回归用例 C1–C4 覆盖）。

> **诚实标注**：本次**仍不宣称**能立刻出单 —— 证据 #1 显示注入**之前**的瓶颈是
> `range_no_extreme`（近 48h 的 142 条 RANGE 日志**全部**为它，RSI 全在 31.8~51.2）。
> 即：**hexp 注入守卫这一层已按口径打通**，但**入场触发**（`range.entry_trigger=rsi`）
> 在观察窗内从未触发过 —— 见 §12.7 待确认项。

### 12.4 单仓判据为什么用 `magic` 而非 `signal_mode`

RANGE 注入单（magic 55）与 hexp **自身**在震荡市出的单（magic 11）的 `signals.signal_mode`
**同为 `HEXP:Regime.RANGE`**、`fallback_reason` 同为字面量 `'none'`（注入打戳
`range_mr(...)` 被 `if not fallback_reason` 挡掉 ⇒ **从不写入**）⇒ PG 侧**不可区分**。
而 `hcm_trading.positions.magic`（2026-09-17 起由 `tools/position_sync.py` 每轮写 **MT5 真值**）
是**唯一**能按 magic 精确区分的依据（`scheduler.py:2036` 那句"positions 无 magic 列"已过期）。
计数失败返回 **−1 → 保守拦截**（fail-closed，同 `_count_fsm_open_positions` 语义）。

**两个必须处理的边界（实现中自查发现，非事后补）**：

| 边界 | 风险 | 处理 |
|---|---|---|
| **塔重启** | `range_round_open` 是**内存态**，重启即丢；若 magic55 仓仍持有 ⇒ 单仓闸不自锁（只剩风控 `risk_cool_minutes` 兜底，而它**保本后即放行**） | 闸门**不以标记为唯一判据**：计数 ≠ 0（含 −1 失败）即拦，并**补回标记**（WARNING `单仓闸自愈`） |
| **bar 内重入** | `_produce_signal` 一根 bar 内会被**重复调用**（实证同 bar 3 条 RANGE 日志：14:25:22 / 14:25:24 / 14:25:52）；注入后到 `position_sync` 落库有**秒级延迟** ⇒ 若以"计数=0"清位，会在空窗**重复下单** | 置位时记 `range_round_ts`；**未过 `_RANGE_ROUND_GRACE_SEC`(420s > 1 根 bar) 时即使无持仓也不开** |

### 12.5 回归自证与验证

- 自证用例：`tools/_scratch/test_range_exempt.py`，**40/40 通过**
  （A 配置键与默认值 `"*"`；**B 全豁免 18 条** —— 含此前刻意保留的"接刀/位置"族逐条断言不再被挡；
  **C 收回路径 C1–C4** —— 改回前缀清单后精确生效、`hexp_grade` 前缀同时命中同族两条、空原因保守拦；
  D 开关 `None`/`'false'`/`False` 解析；E 宽限常量与 `SymbolState` 字段；
  **F 单仓闸分支决策表** 7 场景 —— 含 `F2` 计数失败保守拦、`F4` bar 内重入拦、
  `F5` 平仓清位、`F7` 重启后凭持仓自愈补位）。
- `py_compile`（容器内）通过；容器重启后 **healthy、零 Traceback/ImportError**；
  `signal_tower.scheduler` 可正常 import。
- **已知坑留痕**：既有 `str(cfg.get(k) or "true")` 写法会把**布尔 `False`** 变成 `"true"`
  （= 关不掉的开关，与 `range_box._b()` 的 None 坑同型）⇒ 本次 `range.single_position`
  改用**显式判 None**（`str("true" if v is None else v)`），并写了 D3 回归用例。

### 12.6 回滚（秒级，三档）

```
# ① 只收紧注入豁免（保留单仓闸）：退回"风格族放行 / 接刀族拦截"
set_cfg.py range.hexp_override_reasons "hexp_no_direction,hexp_trend_priority,hexp_grade_red,hexp_grade_below_min,hexp_resonance_counter_block,hexp_momentum_flip,hexp_momentum_drain,hexp_mm_chase_veto,hexp_pullback_gate"
# ② 退回 A2 全局等价行为（= 通道重新冻结，仅 `hexp_no_direction` 可覆盖）
set_cfg.py range.hexp_override_reasons "hexp_no_direction"
# ③ 只关单仓闸（豁免保留）
set_cfg.py range.single_position false
# ④ 入场触发退回 RSI 极值口径（Magic55 通道将重新冻结在 range_no_extreme）
set_cfg.py range.entry_trigger rsi
```

### 12.7 入场触发改为**箱体边缘**（用户拍板 A 案，已实施）

豁免打通"注入守卫"后，观察窗内 RANGE 日志**全部**卡在更前面一步
`range_no_extreme`（142/142 条，RSI 全在 31.8~51.2）—— 即 `range.entry_trigger=rsi`
要求 RSI≥70 或 ≤30，而震荡市 RSI 围绕 50 回归，**该通道等于没有触发源**。
用户拍板「改用箱体边缘」，故：

| 项 | 实施内容 |
|---|---|
| 新增口径 | `range.entry_trigger` 增加取值 **`box`**（与既有 rsi/pctb/both/and 并列）；生产已 `set_cfg.py range.entry_trigger box`（`before='rsi' → after='box'`，ok=True） |
| 裁决位置 | **仍在 `range_strategy`**（方向单一真源不破）：新增纯函数 `_box_edge_direction(box, cfg)`，`mr_direction`/`evaluate` 增 `box=` 入参；箱体只作**输入**（duck-typed，本模块不 import `range_box`） |
| 容差口径 | **复用** `range_box.confirm_direction`（`range.box.edge_tol_atr`），**不在 range_strategy 重算**（否则同一容差两份实现 = 双真源） |
| 判定 | 贴下沿（`close ≤ lower + tol×ATR`）→ BUY；贴上沿（`close ≥ upper − tol×ATR`）→ SELL；两端同时命中（箱体窄于 2×容差）→ **无方向**（与 `mr_direction` 的"矛盾不动作"同口径） |
| 尺度 | 用**快箱**（`range.box.window`=12）—— 其语义即"当前震荡幅度 → 贴边确认"（§3.3 表） |
| scheduler 侧 | `evaluate` 调用点**下移**到箱体算完之后（原位置拿不到 `_box_fast`），传 `box=_box_fast` |

**keep（未一并放开）**：宽度门（`range.width_min/max_atr`）与破箱熔断（`range.box.break_source=box`）
**保留** —— 二者是"箱体口径"自身的**风控**（太窄装不下 1.0ATR 止盈；破沿即"声称 RANGE 实为突破"，
是 2026-09-10 事故的直接护栏），与 hexp 那类**风格闸**性质不同。若用户要求也放开，
改法是把 `_w_ok` / `_bg_on` 置真（配置即可，见 §12.6 追加档）。

### 12.8 生效验证（重启后首批 bar，实测日志）

```
[RANGE-DIAG] XAUUSD _rng={'in_range': True, 'direction': None,
                          'reason': 'range_box_not_at_edge'} trig='box'
                          ps={'M5':'RANGE','M30':'RANGE','H1':'RANGE','H4':'RANGE','D1':'RANGE'}
16:00  RANGE_MR arm  XAUUSD: SELL target=4361.33 (close=4356.16 off=1.00×atr=5.17)   ← ★贴**上沿**武装
16:00  RANGE_MR skip XAUUSD: range_mr(SELL/edge_upper) (rsi=50.4 pct_b=0.683)
16:00  RANGE_MR skip XAUUSD: range_box_not_at_edge   (rsi=48.9 pct_b=0.608)
```

- 触发源已切换：reason 由 `range_no_extreme` → **`range_box_*` / `range_mr(<dir>/edge_*)`**；
- **`RANGE_MR arm` 真实发生**（贴箱体上沿 → SELL，按 `range.entry_offset_atr=1.0` 等回踩）；
- 容器 healthy、`level=ERROR` 计数 **0**、`range_mr inject failed` **0**；
- `positions` 当前无 magic=55（armed 目标 4361.33 未触及；触及后即 `arm filled` → 注入 → 风控链 → 桥）。

> **诚实标注（诊断过程中的一次误判，留痕）**：我一度据 `hcm:live:range_box` 键 TTL=108
> 与 `AI gate probe` 最后一条（15:41:24）判定"RANGE 段根本没跑"，并沿此推理排查了
> 数轮（连查时钟、PG 阻塞、symbol 循环、风控态）。**结论是错的**：该 TTL 是在键
> **即将过期前**读到的（15:50:30 会被下一次 `_produce_signal` 刷新为 600），而
> `AI gate probe` 只在"有方向"时才会走到，RANGE 长期无方向 ⇒ 本就不打印。
> 教训：**用"应当出现的日志缺席"推断"代码未执行"必须排除"日志本身有前置条件"**
> —— 本次直接用一次临时 `[RANGE-DIAG]` 打印 `_rng` 才定死（已撤除）。

---

## 13. 箱体窗口扫描：**30 bar 是否更好？—— 否**（2026-09-19）

**触发**：用户提问「RANGE 注入单（Magic=55）箱体用 30 bar 是否会更好」。

### 13.1 先定"改哪一侧"（读码事实，决定问题有两义）

| 键 | 现值 | 实际影响 |
|---|---|---|
| `range.box.window`（**fast**） | **12** | **只影响"贴边方向判定"**（`confirm_direction`） |
| `range.box.window_slow`（**slow**） | **50** | **宽度门**（`gate_scale=slow`）+ **突破熔断**（`break_source=box`） |

⇒ 故"箱体用 30 bar"有**两义**，本项两义都测。

### 13.2 方法（**复用生产实现，不重写几何**）

`tools/eval_range_box_window.py` —— 逐根 bar 调用 `range_box.compute_box` /
`scales_gate` / `confirm_direction`（`range_box.py` 是箱体几何的**唯一实现**，重写即违反红线），
**只改 window 两个配置值** ⇒ 结论可直接映射成一条 `set_cfg.py`。

模拟口径：双尺度箱体（quantile 95/5，exclusive）→ 宽度门 → 贴边方向 → 等回踩
`entry_offset_atr=1.0`（≤12 根内触价才成交）→ 双障碍 TP=1.0ATR vs SL（待测），
**同根双触保守计负**，扣 **0.12ATR** 往返成本，24 根未决记 `open`。
数据：XAUUSD M5 **69,825 根**；配置取自**配置中心现行值**（window=12/slow=50/width=[1.0,9.0]）。

**⚠ 未含（诚实声明）**：破箱熔断、单仓闸、下游 AI/风控链 ⇒ 本表只回答"箱体窗口对**入场质量**的影响"，
不等于实盘期望。（破箱熔断在 `slow` 固定时对各档同口径；`slow` 变化档会受影响。）

### 13.3 结果

**SL = 1.0×ATR（对称 1:1）**

| 快×慢 | 信号 | arm未触 | 已决 | TP先到率 | E[净R] | 总净R | vs 基线 | 判读 |
|---|---|---|---|---|---|---|---|---|
| **12×50（现行）** | 16262 | 10239 | 16238 | 65.3% | 0.1867 | 3031.44 | — | *基线* |
| 20×50 | 13831 | 8822 | 13819 | 65.4% | **0.1886** | 2606.72 | **+0.0019** | 更好（噪声级） |
| **30×50** | 12386 | 8016 | 12377 | **65.3%** | **0.1865** | **2307.76** | **−0.0002** | **更差** |
| 50×50 | 11303 | 7230 | 11293 | 64.8% | 0.1767 | 1995.84 | −0.0100 | 更差 |
| 12×30 | **18258** | 11564 | 18222 | 65.2% | 0.1844 | **3359.36** | −0.0023 | 更差 |
| 30×30 | 13658 | 8848 | 13645 | 65.2% | 0.1830 | 2497.60 | −0.0036 | 更差 |

**SL = 2.0×ATR**（结论同向）

| 快×慢 | E[净R] | vs 基线 |
|---|---|---|
| 12×50（基线） | 0.2784 | — |
| 20×50 | **0.2837** | **+0.0054** |
| **30×50** | 0.2739 | **−0.0045** |
| 50×50 | 0.2673 | −0.0110 |
| 12×30 | 0.2739 | −0.0044 |
| 30×30 | 0.2729 | −0.0055 |

### 13.4 结论（三条读法）

1. **TP 先到率几乎不随窗口变化**：65.3% → 65.3%（12→30）、64.8%（50）；
   SL2.0 档 79.9% → 79.8% ⇒ **"箱体多宽"不是决定入场质量的因素**。
2. **E[净R] 在 12~30 间基本持平**（0.1867 / 0.1886 / 0.1865，±0.002 = 噪声）⇒
   **"30 更好"不成立**；"20 微好"也**不成立**（同量级）。真正结论是
   **"窗口在 12~30 之间对单笔质量不敏感"**。
3. **总净收益随窗口单调下降**（3031 → 2607 → **2308** → 1996）—— 因为**信号数单调下降**
   （16262 → 13831 → 12386 → 11303）⇒ **窗口越大，贴边机会越少**。
   **这正是"30 bar 更差"的直接原因：单笔期望不变，但总样本少了 24%。**

> **⇒ 建议维持 `range.box.window = 12`（不改成 30）。** 若要动，`20` 与 `12` 等价
> （单笔 +0.002 但总净 R −14%）⇒ **同样不建议动**（少做 14% 的单去换噪声级的单笔提升）。

### 13.5 模拟口径的自校验（重要的可信度证据）

本模拟在 **1:1** 档得 E[净R] = **0.1867**，与 `range_strategy.DEFAULTS` 记录的
「双障碍回测（292/188 独立事件，扣 0.12ATR）**d=1.0（等回踩）E[R]=+0.197 / +0.190**」
**吻合**（差 ≤0.011）⇒ 说明本模拟的等回踩/成本/双障碍口径与当初的标定一致，
**结论可信**。同时它也说明：**`box` 贴边口径与 `rsi` 口径在"等回踩后"的单笔期望接近**
（换触发源没有明显牺牲质量）。

**附带观测**：`arm 未触` 占信号数的 **39%**（10239/16262）⇒ `entry_offset_atr=1.0`
是很激进的一刀（约 2/5 的贴边信号因"不回踩 1 ATR"而作废）。**注**：这是既有设计
（文档实测 d=1.0 优于 d=0 的市价入场），本项**不建议**改动，仅留痕。

### 13.6 回滚/落地

**无需任何改动**（结论是"维持现状"）。若将来要试，一条即可：

```
set_cfg.py range.box.window 20        # 或 30；⚠ 按本表总净 R 会下降
set_cfg.py range.box.window 12        # 回滚
```

> **本项为纯离线诊断，未改任何配置。** 产物：`tools/eval_range_box_window.py`（可复跑，
> 支持 `--pairs 12x50,30x50,...` 与 `--sl-atr`）。
