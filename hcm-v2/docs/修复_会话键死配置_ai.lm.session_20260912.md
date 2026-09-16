# 修复：ai.lm 会话键死配置（`ai.lm.*.{asia,europe,us}` 改了不生效）

- 日期：2026-09-12（周六，休市）
- 触发：用户复核指令「三个会话键 `ai.lm.entry_veto_prob.{asia,europe,us}` 是配置中心里的"摆设"的判断是否成立？如确认，修复这个缺陷」
- 性质：**生产代码修复 + 生产配置写入 + 容器重启**（三项均已显式授权）

---

## 一、复核结论：**CLAIM CONFIRMED —— 确系死配置**

`ai.lm.{dir_veto_prob, entry_veto_prob, entry_boost_prob}.{asia,europe,us}` 共 **9 个键**在配置中心可改、可存、可读，
但**没有任何代码会消费它们**。

### 证据链（file:line）

| # | 事实 | 证据 |
|---|---|---|
| 1 | 会话覆盖逻辑**确实存在**，写在 `_produce_signal` 内 | `scheduler.py`（修复前）`:2478-2486`：`_sv = _ai_cfg.get(f"{_sk}.{_cur_sess}")` → 命中才 `_ai_cfg[_sk] = _sv` |
| 2 | 但它读的是 `_ai_cfg`，而 `_ai_cfg` 来自白名单，**白名单只登记全局键** | `scheduler.py:684-714`，30 个键，无一带 `.asia/.europe/.us` |
| 3 | 故 `_ai_cfg.get("<key>.<session>")` **恒 None** | `:2482` 永不命中 → `:2484` 永不执行 |
| 4 | `_ai_cfg` 的**唯一赋值点**是 `:2474`，**唯一写入点**是 `:2484`（在死块内） | 全仓 grep 全证 |
| 5 | 生效阈值只能取**全局键**，全局为空则回退硬编码 | `quality_gate.py:389` `_g(cfg,"ai.lm.entry_veto_prob")`；`:47` `CFG_FALLBACK = 0.35` |
| 6 | 会话键在 Redis / PG **真实存在**，非幻觉 | `hcm:config:v2` 实测 9 键齐备；PG `hcm_config.metadata` 9 行（2026-09-04 08:48-08:50 种入） |

**独立对抗验证**：另起一个只读审查代理专门尝试推翻该判断，返回 `CLAIM CONFIRMED`（`_ai_cfg` 唯一赋值/写入点均在死块内或死块外无交集）。

---

## 二、缺陷完整范围（PG 全量 57 个会话后缀键，5 组）

| 键族 | 数量 | 消费点 | 判定 |
|---|---|---|---|
| `ai.lm.dir_veto_prob / entry_veto_prob / entry_boost_prob .{sess}` | 9 | `quality_gate.py:367/388/389` **只读全局** | ❌ **死**（本次修复） |
| `ai.rev.confirm_atr / tighten_buf_atr / trigger_dd_atr .{sess}` | 9 | `mt5_bridge.py:4563` 直读 `ai.rev.<suffix>.<sess>` | ✅ 活 |
| `close.<session>.<suffix>` | 27 | `_session_float()`（`scheduler.py:55-81`）Redis 直读 | ✅ 活 |
| `hexp.exec.lot_mult.{sess}` | 3 | `hexp_engine.py:2619` 只读全局 | ⚠️ 孤儿（无读取点） |
| `hexp.ai.coupling_lot_floor.{sess}` | 3 | 全仓（含 `-i`）**零匹配** | ⚠️ 幽灵键族 |

**性质区分（重要）**：
- 前 9 个是 **「有代码意图、实现残缺」**——`scheduler.py`（修复前）`:2475-2477` 注释明写「用户需求：方向/买入/反转头会话化」，说明是**真需求没落地**。
- 后 6 个是 **「配置中心有键、代码里从不存在」**的纯孤儿，**不同源**。经用户决策：**仅报告，暂不处理**（接线会改变手数 = 交易行为变更，须另开方案）。

---

## 三、修复方案（改一处即根治本缺陷类）

**设计要点**：把会话覆盖从「白名单产物的下游」下沉到 `_ai_cfg_dict()` **内部直读配置中心** ——
这样**白名单不再是会话键生效的前提**，从机制上消灭「配了没登记 = 不生效」这一缺陷类。

### 变更清单（`hcm-signal-tower/signal_tower/scheduler.py`）

| 位置 | 变更 |
|---|---|
| `:55-62`（新增） | 模块级 `_AI_SESSION_OVERRIDE_KEYS = ("ai.lm.dir_veto_prob", "ai.lm.entry_veto_prob", "ai.lm.entry_boost_prob")` |
| `:734-753`（新增） | `_ai_cfg_dict()` 内 `return out` 前：取 `_current_session_utc()` → 对每键 `await self._config.get(f"{_sk}.{_sess}")`，**非空**才覆盖 `out[_sk]` |
| `:2504`（删除+注释） | `_produce_signal` 内原死块删除，留注释阻断回归 |

- 覆盖优先级：**会话键 > 全局键 > `quality_gate.CFG_FALLBACK`**
- 收益：`_ai_cfg_dict()` 的 **4 个调用点**（`ai_ds` 循环 / `calibrate` / `_read_ai_quality` / `_produce_signal`）同时获得会话感知
- 空值语义：会话键为空串 → 不覆盖，回落全局（与 `config_provider.get()` 的「空串穿透」一致）
- 异常安全：`_current_session_utc()` 与每次 `get()` 各自 try/except；`_config is None` 的早退在会话块**之前**，不会触达

### 配置变更（`PUT /api/v1/config/{key}`，双写 PG + Redis）

| 键 | 修复前 | 修复后 | 理由 |
|---|---|---|---|
| `ai.lm.entry_veto_prob.asia` | 0.35 | **0.3** | 机制生效后会话键会覆盖全局；三键对齐到已授权的 0.3 |
| `ai.lm.entry_veto_prob.europe` | 0.35 | **0.3** | 同上 |
| `ai.lm.entry_veto_prob.us` | 0.35 | **0.3** | 同上 |

**未改动**：`ai.lm.entry_veto_prob`（全局，今日早些时候已授权改为 0.3）、`ai.lm.entry_fuse=true`（今日已授权）、
`ai.lm.dir_veto_prob.{sess}=0.65`、`ai.lm.entry_boost_prob.{sess}=0.60`（与 `CFG_FALLBACK` 等值 → 无行为漂移）。

> ⚠️ **若不改这三个会话键**：机制生效后 entry veto 实际阈值会从 0.30 变 0.35（阈值越高否决越多），
> 等于静默收回当日已授权变更。这是修复的**必要配套动作**。

---

## 四、执行记录与验证

| 步骤 | 结果 |
|---|---|
| 备份 | `tools/_scratch/scheduler.py.bak_20260912_sessfix`（md5 `f3d81e84…` = 改动前原文） |
| 代码改动 | 主机 md5 `f3d81e8424004233486741240ca595a7` → `5769d91b8a2e927708e8eda5a03ed744`；`py_compile` OK；死块残留计数 = 0 |
| 配置写入 | 3 键 PUT 全 `code=0 ok`；GET 读回 `current='0.3'`；Redis `hcm:config:v2` 与 PG `hcm_config.metadata` 一致（`updated_at 13:41:19Z`） |
| 重启 | `docker restart hcm-v2-hcm-signal-tower-1` → `Up (healthy)`；配置加载日志正常（ScoringEngine / H1Regime / Gate / Scheduler config loaded） |

### 机制级实测（容器内，因休市无法做行为级验证）

| 测试 | 内容 | 结果 |
|---|---|---|
| [1] 代码版本核验 | 容器内 `inspect.getsource` 含会话直读代码；常量已加载；死块不在源码中 | ✅ 三项一致 |
| [2] 覆盖优先级（stub cfg） | 构造 3 个会话 × 全局组合，断言取值 | ✅ PASS |
| | · `asia` 会话键**空** + 全局 0.30 → `0.30`（回落正确） | ✅ |
| | · `europe` 会话键 **0.99** + 全局 0.30 → `0.99`（**覆盖生效**） | ✅ |
| | · `us` 会话键 0.30 → `0.30` | ✅ |
| | · `dir_veto_prob` 全局空 + europe 会话 0.65 → `0.65`；asia/us 无会话键 → 缺席（→ fallback 0.65） | ✅ |
| [3] 真实配置中心（Redis） | 真 session `us`，走真 `ConfigProviderV3` | ✅ `entry_veto_prob='0.3'`、`dir_veto_prob='0.65'`、`entry_boost_prob='0.60'`、`entry_fuse='true'` |
| [4] 配置键登记巡检 | `tools/config_key_audit.py` | ✅ `RESULT: PASS`，exit 0；白名单 33 键 / quality_gate 读 26 键 / 缺失 0 |
| [5] 独立对抗审查 | 专门尝试推翻修复（完整性 / 正确性 / 回归三命题） | ✅ `无法推翻（修复完整）` |

> 修复前 `_ai_cfg` 里**根本没有** `dir_veto_prob` / `entry_boost_prob` 两键（全局为空），
> `quality_gate` 靠 `CFG_FALLBACK` 兜到 0.65 / 0.60；修复后这两键**由会话键显式提供**，**数值相同 → 无行为漂移**，
> 唯一实质变化就是 `entry_veto_prob` 从「fallback 0.35」变为「会话 0.3」。

### 尚未验证（受限于休市）

`ALERT kline feed STALE for XAUUSD/M5 ... Pausing signal production`（M5 最后 Bar 停滞）。
信号生产暂停 → 闸门不会被触发 → **无法观测行为层**。
开市后观测关键字：`veto_bad_entry` / `boost_good_entry`（`quality_gate.py:390-401` 分支），
或查 `hcm_ai.gate_decision` 落库。

---

## 五、回滚

- **代码**：`cp tools/_scratch/scheduler.py.bak_20260912_sessfix hcm-signal-tower/signal_tower/scheduler.py` → `docker restart hcm-v2-hcm-signal-tower-1`
- **配置**：`PUT /api/v1/config/ai.lm.entry_veto_prob.{asia,europe,us}` `{"value":"0.35"}`（或传空串恢复为"未设置"）

---

## 六、遗留项与防回归

1. **遗留（用户决策：暂不处理）**：`hexp.exec.lot_mult.{sess}`(3) 与 `hexp.ai.coupling_lot_floor.{sess}`(3) 共 6 个孤儿键，
   全仓零读取点。前者若接线会**改变下单手数（交易行为）**，须单独立项评估；后者疑似未实现的规划键。
2. **防回归建议**：`tools/config_key_audit.py` 目前只校验「读取点 ⊆ 登记表」，对「配置中心存在但无人读」的键无检测能力。
   建议新增 [4] 段：扫描 PG 中符合 `%.{asia|europe|us}` 的键，反查全仓引用数，引用数为 0 者告警。
   （本次 6 个孤儿键正是该类，靠人工 grep 才发现。）
