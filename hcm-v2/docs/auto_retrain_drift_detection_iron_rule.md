# 铁律：LightGBM 特征漂移的及时检测与自动重训

> 固化日期：2026-08-24
> 关联：`tools/auto_retrain.py`（数据驱动重训 daemon）、`tools/monitoring_report.py`、
> `tools/quality_scorer.py`（推理 sidecar）、`tools/_model_feature_cols.py`（特征契约）、`tools/train_signal_quality.py`。
> 前置文档：`docs/hexp_config_min_grade_iron_rule.md`（配置 seed/运行时真相）、`docs/ai_scorer_retrain_plan_b.md`（训练管线）。

---

## 一、本次事故根因（三层叠加）

现象：**LightGBM 特征已重度漂移（PSI 最大 0.7+）却没有触发 DeepSeek 校准 / 自动重训**。

### 第 1 层（主因）：auto_retrain daemon 根本没在运行
- 宿主 python 进程里没有任何 `auto_retrain.py` 进程。
- 日志停在 **08-22 03:03**（UTC），心跳 `hcm:ai:retrain:daemon` 停在 08-22——**daemon 在 08-22 凌晨后停止**（大概率机器重启后未自启）。
- daemon 不跑 → `monitor_and_trigger()` 不执行 → 漂移检测失效 → 重训和 DeepSeek 裁判都不触发。

### 第 2 层：触发逻辑缺陷（即使 daemon 在跑也会漏掉单特征重度漂移）
日志实证（08-22 03:03）：
```
[trigger] psi_max=0.701 psi_mean=0.037 drifted=['event_proximity_min'] -> triggered=False
```
原逻辑 `triggered = (psi["max"]>0.25 AND psi["mean"]>0.25) or collapse`：
- `psi_max=0.701`（`event_proximity_min` 单一特征重度漂移）已超阈值，但
- `psi_mean=0.037`（其余 32 特征正常，稀释了均值）<0.25 → **不触发**。
→ **单一/少量特征重度漂移被 `mean` 稀释而漏掉。**

### 第 3 层：DeepSeek 三特征从未进入模型（ds 语义断链）
- 权威特征契约 `_model_feature_cols.MODEL_FEATURE_COLS`（33 维）**没有 `ds_fake_prob`/`ds_sl_coeff`/`ds_continuity`**。
- `quality_features.py` 算出了 ds 列，但训练 `reindex` 到 `MODEL_FEATURE_COLS` 后 **ds 列被丢弃** → 模型从未吸收 DeepSeek 语义。
- 后果：`train_signal_quality.py` 的 `ds_diag` 检查因 `_ds_cols` 为空**从不执行** → `ds_nonzero_ratio` 永远 `None` → **DeepSeek 裁判拿不到"模型 ds 吸收率"，只能信息不足而保守回滚**。

---

## 二、修复（已落地并验证）

1. **重启 daemon**：`python auto_retrain.py --daemon --interval-hours 24`，宿主后台常驻（PID 检查）。
2. **触发逻辑修复**（`auto_retrain.py`）：
   - 豁免高波动/非平稳环境特征 `event_proximity_min`（"距下一重大事件分钟数"，PSI 恒虚高，不作触发依据，可 `PSI_EXEMPT_FEATURES` 追加）。
   - 新增单特征重度漂移判定：排除豁免后**任一特征 `psi>PSI_HARD_TRIGGER(0.5)` 即触发**。
   - 触发 = `collapse OR (max>0.25 AND mean>0.25) OR (单特征>0.5)`。
3. **ds 三特征纳入契约**（`_model_feature_cols.py` 33→36 维）：训练 `reindex` 保留 ds 列 → `ds_diag` 生效 → `ds_nonzero_ratio` 可解析。
4. **编码/流解析修复**：
   - `train_signal_quality.py` 的 `ds_diag` 改纯 ASCII 输出（Windows 下子进程 stdout 是 GBK，UTF-8 解码中文会乱码 → regex 匹配失败）。
   - `auto_retrain.py` 的 `run()` 强制 `encoding="utf-8"` + 合并 stdout+stderr 解析（`ds_diag` 打印在 stderr）。

**验证**：重训闭环 `ds_nonzero_ratio=0.139`（不再 None）；DeepSeek 裁判 reason 明确引用吸收率；O3 护栏正确阻止 ds 不足时的"假精准"切换。

---

## 三、铁律（不可违背）

> **铁律 1（漂移检测守护必须常驻）**：auto_retrain daemon 是"特征漂移→自动重训→DeepSeek 裁判"的唯一驱动，**必须常驻运行**。部署/机器重启后必须自启（服务/计划任务/容器编排），并用 `hcm:ai:retrain:daemon` 心跳 + 进程检查监控其存活。**daemon 停了 = 漂移检测、自动重训、DeepSeek 裁判三者全部失效**，且通常静默无感（最危险的降级）。

> **铁律 2（漂移触发必须覆盖单特征重度漂移）**：触发条件**禁止**只写成 `max>X AND mean>X`（会被大量正常特征稀释均值而漏掉单特征重漂移）。必须同时覆盖：
> - 普遍漂移：排除豁免后 `max>0.25 AND mean>0.25`；
> - **单特征重度漂移**：任一特征 `>0.5` 即触发；
> - 置信坍缩：`collapse`。

> **铁律 3（高波动环境特征必须豁免，不得作触发依据）**：凡分布天然非平稳的特征（如 `event_proximity_min` 这类"距事件分钟数"），其 PSI 恒虚高，**必须排除在重训触发判定之外**（报表可展示，触发时豁免）。否则会要么持续制造"重度漂移"假象、要么在放开单特征触发后误触发重训 churn。可通过 `PSI_EXEMPT_FEATURES` 追加。

> **铁律 4（数据契约必须包含实际喂给模型的全部特征）**：`_model_feature_cols.MODEL_FEATURE_COLS` 是训练/推理**单一真源**，凡训练想用的特征（含 DeepSeek ds_*）**必须**列入其中并训练/推理双侧同步。**契约漏列 = 该特征被 reindex 静默丢弃 = 模型从未用它**，且相关诊断（如 ds_diag）因列缺失而永不触发——这是"看起来在工作、实际断链"的隐藏缺陷。

> **铁律 5（DeepSeek 裁判必须拿到完整指标）**：裁判决策依赖 `ds_nonzero_ratio`（模型 ds 吸收率）与 AUC 等。若解析层（编码/流/正则）有问题导致这些指标恒 `None`，裁判只能"信息不足而保守回滚"或"盲审"。**任何"模型评估/裁判"依赖的中间指标，必须保证能从子进程输出稳定解析**（优先 ASCII 输出 + UTF-8 解码 + stdout/stderr 合并）。

> **铁律 6（ds 吸收不足时禁止切上线——防"假精准"）**：当 `ds_nonzero_ratio < DS_MIN_NONZERO_RATIO(0.3)`，说明模型实质未吸收 DeepSeek 语义。**只允许产出新模型，禁止切换线上**（即使 DeepSeek 裁判 adopt）。否则会用一个"未吸收新信号、却改变特征分布"的模型替换现役，造成假精准。等 ds 票积累到阈值以上再切换。

---

## 四、可复用教训清单（给团队）

1. **"没触发重训/校准"的第一排查动作**：查 daemon 是否在跑（进程 + 心跳 `hcm:ai:retrain:daemon` + `auto_retrain.log` 最后时间）。**daemon 停运是最常见的静默失效原因**。
2. **漂移触发逻辑的坑**：`max AND mean` 会漏单特征重漂移。日志里 `psi_max=0.7` 但 `triggered=False` 就是铁证，必须看 per-feature 分布而非只看聚合。
3. **契约漏列是隐藏断链**：特征被 `reindex` 静默丢弃时**不报错**，只是模型没用它 + 相关诊断永不触发。改特征时必须同时核对 `MODEL_FEATURE_COLS` 双侧。
4. **Windows 子进程编码是真实故障源**：Python 子进程输出中文时 stdout 是 GBK，UTF-8 解码会乱码 → 正则匹配失败 → 解析返回 None。**跨平台解析用 ASCII 输出最稳**。
5. **护栏比裁判更重要**：即使 AI 裁判（DeepSeek）判定 adopt，数据充分性硬门槛（样本量、ds 吸收率）仍要独立守住"不切换"底线——裁判可能信息不全或偏激进。
6. **调参后要 re-pin baseline**：改 HEXP 参数（`k_extreme`/`mm_retreat_min` 等）会系统性改变特征分布 → PSI 基线失配 → 误触发重训 churn。调参后执行 `auto_retrain.py --link-hexp --force-repin`（HEXP 参数变更链动 re-pin 机制）。

---

## 五、回归防护建议

- **daemon 自启 + 存活探针**：开机/部署自动拉起 daemon；监控 `hcm:ai:retrain:daemon` 心跳新鲜度（如 >2h 未刷新即告警）。
- **漂移触发单测**：构造"单特征 psi=0.7、其余正常"的输入，断言 `triggered=True`；构造"仅豁免特征 psi=0.7"断言 `triggered=False`。
- **契约完整性校验**：CI/启动时对比 `MODEL_FEATURE_COLS` 与 `train_signal_quality` 实际建模列 + `quality_scorer.build_features` 产出列，三者不一致即告警。
- **解析层单测**：喂一段含 `DeepSeek nonzero ratio 13.9%` 的 ASCII 文本，断言 `ds_nonzero_ratio()==0.139`。
- **裁判指标完整性**：DeepSeek 裁判 reason 依赖的每个指标（ds 吸收率、AUC、基线胜率）都必须有兜底，缺失时应显式标注"指标缺失"而非静默 None，避免盲审。
