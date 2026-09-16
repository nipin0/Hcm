# 复盘：timesFM 与 LightGBM 昨日（2026-09-11 UTC）运行状态

> 生成时间：2026-09-12 09:43 GMT+8（周六）。本文所有时间戳均为 **UTC**（服务日志口径），上海 = UTC+8。
> 结论先行：**两个组件昨日均无宕机**；K 线停更与 AI 评审停滞是**周五 21:00 UTC 收盘后的周末休市正常表现**，非故障。真正需处理的是两个与休市无关的既有工程缺陷（auto_retrain 特征步反复超时、调度器重训结果盲报）。

---

## 0. 时间基准与关键前提（避免误报）

- **2026-09-11 是周五**。XAUUSD 现货黄金在周五约 **21:00–22:00 UTC 收盘**，周日约 22:00 UTC 重开。当前为周六，市场本就关闭。
- 实测 M5 K 线最后一根 `open_time = 2026-09-11 20:55 UTC`（覆盖 20:55–21:00），恰好对应"周五 21:00 UTC 收盘"——**这是正常收盘，不是采集中断**。
- collector 容器 `hcm-v2-hcm-collector-1` 状态 `Up 20h (healthy)`，日志持续 `GET /health 200 OK` 探活 → **进程存活、健康**，数据层面只是"无行情可采"。

---

## 1. timesFM 运行状态

| 项 | 状态 | 证据 |
|---|---|---|
| 调度器存活 | ✅ | Redis 心跳 `hcm:ai:timesfm:daily` = `{pid:12960, status:IDLE, last_run:2026-09-11, next_due:21:30Z}`，心跳时间 2026-09-12 09:37 UTC |
| 每日 T+1 整批跑批（09-11 21:40 UTC） | ✅ | `_logs/timesfm_daily.log:2674` 触发 → `[saved] 99 行 -> hcm_ai.timesfm_features`，检索库 `tmf_hist_lib_v1.npz` 增至 1294 向量 |
| 抽取后触发 auto_retrain | ⚠️ **ABORT** | `_logs/timesfm_daily.log:2680` `[ABORT] quality_features failed: timeout after 600s` → 目标版本 **v107 未产出** |
| 调度器对 ABORT 的判定 | ⚠️ **盲报** | `timesfm_daily_scheduler.py:317` 仅判 `returncode==0` 即记 `daily run done: status=OK`；`auto_retrain.py` 内部 ABORT 仍退出 0 → 心跳显示 OK，运维无从发现"重训未生效" |
| 滚动抽取（每 15min） | ◐ 正常 | 全天返回 `NO_NEW_SIGNALS`；根因是收盘后无新 K 线（`timesfm_features.py:516` 抛"指定区间内无可用 bar"），**非逻辑 bug**。交易日该滚动抽取本应补齐非信号时刻的线上特征，但休市期无法验证 |

`timesfm_features` 表覆盖：09-11 仅信号时刻行（bar_time 上限 20:55 UTC，受收盘限制），无 21:00+ 覆盖——符合预期。

---

## 2. LightGBM 运行状态

| 项 | 状态 | 证据 |
|---|---|---|
| 活跃模型 | ⚠️ 停留在 v103 | Redis `ai.lm.model_path = .../lgbm_quality_v103.txt`（自 09-10 18:44）；`inference_log` 末条 2026-09-12 01:46 UTC 仍记 v103 |
| 运行模式 | ⚠️ 已切 decoupled | Redis `ai.mode = decoupled`（项目记忆 09-02 记为 coupled；DB `param_history` 为 JSON blob 不存键变更轨迹，仅能从 Redis 现状推断已切换） |
| 在线推理 | ✅ 活跃 | `inference_log` 09-11 共 **21610** 次（coupled 模式 12522）；末条 2026-09-12 01:46 UTC |
| AI 评审（review） | ✅ 活跃 | `review_log` 09-11 共 **236** 条：VETO 56 / DOWNGRADE 111 / PASS 66，模型 `lgbm_review_quality.txt` |
| 风控闸门（gate） | ✅ 活跃 | `gate_decision` 09-11 共 **76** 条：DOWNGRADE 48 / VETO 27 / UPGRADE 1，avg c_ai 30–35 |
| 版本治理 | ⚠️ 卡住 | v104/v105/v106 于 09-10 训练但**未晋升**（验收闸门大概率拒绝）；v107 因 09-11 ABORT 未产出 → 线上连续约 2 天停在 v103 |
| 判别力 | ⚠️ 仍无效 | 延续 09-10 审计：订单级 `c_ai` AUC=**0.513**（≈抛硬币），UPGRADE 为反向指标（142 笔 −71.1）。本轮无改善、无恶化 |

信号/评审随收盘自然停止：最后 signal `2026-09-11 20:55:04`、最后 gate_decision `12:15`、最后 review_log `12:05` UTC。

---

## 3. 两个真正的工程缺陷（与休市无关，建议修）

### D1. `auto_retrain` 的 `quality_features` 步反复超时 ABORT
- 现象：**09-01 与 09-11 同一错误**（`_logs/timesfm_daily.log:78` 与 `:2679` 均为 `quality_features failed: timeout after 600s`）。
- 根因：`auto_retrain.py:194` `run()` 默认 `timeout=600`；`quality_features.py` 在大样本下超 600s。09-11 信号量 274 > 09-10 242 → 标签更多、特征更慢而触发。
- 影响：重训整条链路在特征步即中断，新模型（v107）永不落地。
- 修法：上调 `quality_features` 专属超时（如 1800s）或对其做增量/分片；并在超时后**显式返回非 0**。

### D2. 调度器重训结果 fail-open 盲报
- 现象：`timesfm_daily_scheduler.py:317` 只判 `returncode==0`，`auto_retrain.py` 内部 `[ABORT]` 仍退出 0 → `daily run done: status=OK`，心跳/面板无从发现"重训未切换"。
- 修法：解析 `|retrain|` 输出中的 `[ABORT]`/`[switch]` 关键字决定状态；或让 `auto_retrain.py` 在 ABORT 时 `sys.exit(非 0)`；心跳 `last_run` 附 `retrain_status`。

---

## 4. 与前期审计的衔接
- 09-10 审计报告已确认 `c_ai` AUC=0.513、UPGRADE 反向、calib_factor 死字段、版本抖动（35 版/30天）。本轮未见新恶化，但**模型自 09-10 起未再晋升**，治理实际处于停滞。
- 09-11 设计文档《LightGBM 解耦·信号级评审·校准治理》提的"评审放 scheduler 进程内 + 验收闸门卡 OOF AUC≥0.55"仍未落地；当前 `ai.mode=decoupled` 与文档方向一致，但多头（方向/买点）仍只观测不裁决。
- 滚动抽取"填线上特征"的 09-08 审计修复，在交易日价值待验证；休市期无法证伪，但逻辑上仍是解决 train/serve skew 的正确路径。

---

## 5. 建议（按优先级）
1. **修 D1+D2**（零实盘风险，纯离线/调度层）：让 auto_retrain 超时可控且结果可观测，避免"模型悄悄不更新"。
2. 周日 22:00 UTC 重开后，观察 collector 是否正常恢复写入、timesFM 滚动抽取是否开始产出非信号时刻特征（验证 09-08 修复闭环）。
3. 若要做 LightGBM 价值验证，按 09-11 设计文档的离线验收闸门（AUC≥0.55 + 分层单调 + ECE≤0.05）重新跑，不过闸则停在 v103/纯 HEXP。
