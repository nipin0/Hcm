# 教训记录：和乘幂「可下单等级」保存无效 / 刷新复原

> 固化日期：2026-08-21（两轮迭代）
> 现象：用户在「和乘幂信号 · 参数配置」把「可下单等级」改为 A 级，点保存，刷新页面后恢复成 B。

---

## 一、故障是如何在用功能中产生的一（机制链条）

这个 BUG 不是单个错误，而是**三层叠加**才显形：

### 第 1 层：前端保存触发用了一个脆弱的全局选择器
三个配置页（HexpConfig / CoSourceConfig / AiQualityConfig）的保存逻辑是：
密码守卫 `SaveGuardDialog` 确认 → `onConfirmed` 里执行
```js
const form = document.querySelector('form');   // ❌ 选整个 document 里第一个 <form>
if (form) (form as HTMLFormElement).requestSubmit();
```
`requestSubmit()` 是否真的触发 `ConfigForm` 的 `onSubmit`（进而调 `handleSave` 发 PUT），
**完全取决于 `querySelector('form')` 选中的是不是那个正确的表单**。

### 第 2 层：嵌入场景让选择器必然选错
「和乘幂信号」参数配置页并非独立页，而是嵌在「信号模式与市况」页（`Mode.tsx`）的 Tab 里。
页面上同一时刻存在多个 `<form>` 片段、MUI `Box component="form"` 渲染、以及守卫对话框等，
`document.querySelector('form')` 在多数浏览器/环境下会选中**错误的 form**（或选中后
`requestSubmit()` 因缺少 submit button 静默失败）。

### 第 3 层：结果 = 保存请求从未发出
守卫确认只把 `pwVerified.current` 置 true，并试图 `requestSubmit()`。
一旦选中错误 form，`requestSubmit()` 触发的是别的 form 的 onSubmit（或根本不触发），
**真正的 `handleSave` → `PUT /api/v1/hexp/config` 从未执行**。
- 用户视角：下拉框本地 state 变成 A（以为改成功了），弹窗关闭（以为保存了）。
- 后端视角：从未收到 PUT，`hexp.min_grade` 在 PG/Redis 里仍是旧值 B。
- 刷新后：`GET` 返回后端真实值 B → **"保存后刷新又复原"**。

### 第 4 层（连带真 bug，加重"配置不生效"错觉）：字母档位被 float 吞掉
`signal_tower.py` 的 `_read_hexp_funnel_thresholds` 对 `hexp.min_grade`（字母档位 'A'/'B'/'C'）
误用 `float(v)`，`'A'` 抛异常落入 `except` 用默认 `'C'`。
即使保存成功，漏斗展示页也永远显示默认档，**进一步强化"改了没用/复原"的体感**。
（两处相同逻辑，均已修。）

---

## 二、第一轮误判的教训（非常重要）

最初我**静态读代码**推断根因是 `system.py` 的自愈校准 `calibrate_min_grade`
把 `hexp.min_grade` 强制对齐回意图锚点 `hexp.min_grade_intended`（默认 B），并据此做了
"保存时同步 intended" 的修复。**但这个判断是错的**：

- `calibrate_min_grade` 只在诊断接口 `auto_heal=True` 时运行，**没有定时任务**，不会自动触发；
- 看 **docker logs 真实请求链路**才发现：故障期间浏览器 IP `172.18.0.1` **只有 GET，从未发过 PUT**。
  → 这说明"复原"不是后端把值拉回，而是**前端压根没把保存请求发出来**。

**教训**：定位 BUG 必须看运行时日志 / 真实请求，不能只靠代码静态推理下结论。
第一轮修复（同步 intended）虽不是本故障根因，但作为"自愈不覆盖用户显式保存"的防御仍有价值，保留。

---

## 三、最终修复（已落地并验证）

1. `components/ConfigForm.tsx`：改为 `forwardRef`，`useImperativeHandle` 暴露命令式 `submit()`，
   不再依赖全局 querySelector 触发提交。
2. `HexpConfig.tsx` / `CoSourceConfig.tsx` / `AiQualityConfig.tsx`：用
   `const formRef = useRef<ConfigFormHandle>(null)` 精确持有表单句柄；
   `onConfirmed` 改为 `formRef.current?.submit()`（彻底移除 `document.querySelector('form')`）。
3. `web/api/signal_tower.py`：`hexp.min_grade` 等字母/枚举档位读取时 `isinstance(d, str)` 原样保留字符串，
   禁止 `float(v)`（共两处）。
4. 前端 `npm run build` 重建 dist（新哈希 `index-CueeEEyp.js`），web 容器已重启加载。

**验证**：真实 HTTP 链路 登录→保存 A→GET 返回 `A/A`→持久无回退；容器内新 dist 已生效；
全前端 `document.querySelector('form')` 调用清零（仅剩注释）。

---

## 四、铁律（不可违背）

> **铁律 1（保存触发必须精确）**：任何「守卫确认后提交表单」的逻辑，**禁止用
> `document.querySelector('form')` 等全局 DOM 选择器触发提交**。必须用
> `ref` / `forwardRef + useImperativeHandle` 精确持有表单实例并调用其提交方法。
> 全局选择器在嵌入 / 多表单场景下必然出错且极难排查。

> **铁律 2（字母档位禁止 float）**：配置键凡值为字母 / 枚举档位（如 `hexp.min_grade` 的
> A/B/C），读取时**禁止 `float(v)`**，必须按字符串原样处理，否则抛异常落入默认档，
> 造成"配置不生效 / 复原"假象。

> **铁律 3（修复必须真落地 + 须复现验证）**：
> - 改了源码 ≠ 修复生效。前端必须**重新构建 dist**；Python 改动若容器是镜像构建、未 bind mount，
>   则必须 `docker cp` 进容器或加入 bind mount，否则 `docker recreate` 后回退镜像旧版、丢失修复。
> - 定位 BUG 必须**看运行时日志 / 真实请求**（如 docker logs），核对请求是否真到达后端，
>   不能只靠代码静态推理就下结论。
> - 用真实 HTTP 链路（登录 → GET → PUT → GET）复现，并 `grep` 容器内实际运行的文件确认修复生效。

> **铁律 4（自愈不覆盖用户显式保存）**：任何"自愈 / 校准 / 对齐"逻辑禁止覆盖用户在面板
> 显式保存过的值；存在 `xxx` / `xxx_intended` 双键时，面板暴露的必须是用户直接编辑的键且
> 保存时同步意图锚点。

---

## 五、可复用教训清单（给团队）

1. **"保存后刷新复原" 优先排查请求是否真的发出**，而不是先怀疑后端回写。
   最快证据：`docker logs` 看对应 IP 有没有 PUT；没有 PUT → 问题在前端提交链路。
2. **全局 DOM 选择器（`document.querySelector`）在组件化 / 嵌入页面里是高危写法**，
   凡是"找到某个 form/button 然后触发"的写法都应替换为 ref。
3. **配置值的类型要自洽**：前端下拉是字符串枚举，后端读取就按字符串处理，
   任何 `float()/int()` 强转枚举档位都会静默走入默认分支，制造"改了没用"的假象。
4. **排查要先看日志再下结论**：第一轮因没看日志误判自愈，浪费一轮修复。
   铁律是：先 `docker logs --since` + `grep` 拿到真实请求证据，再改代码。
5. **改完要确认"运行态"而非"源码态"**：前端改 src 不 rebuild dist 等于没改；
   python 改了没挂载/没 cp 等于没改。验证时用 `grep 容器内文件` 确认生效。
6. **同源 bug 要成批修**：`document.querySelector('form')` 在三个配置页都有，
   发现一个就全局搜、一起修，避免按下葫芦浮起瓢。

---

## 六、回归防护建议

- 前端增加测试：守卫确认后断言 `formRef.current.submit()` 被调用且 PUT 请求发出。
- CODE REVIEW 必查：任何「确认后提交」场景是否用了全局 `querySelector`。
- `system.py` 自愈逻辑保留铁律 4 注释（第一轮），但已确认其非本故障元凶。
- 配置读取处增加类型守卫单测：字母档位传入 `'A'` 必须返回 `'A'`，不得落入默认。

---

## 七、延伸事故：持仓"开在最高点又止损"（2026-08-21，同源 seed 漂移）

### 现象
用户在「和乘幂」参数配置把可下单等级改 A 之外，还出现：**持仓单开在 Donchian 极值高位（pos 0.87~0.94）后立刻被止损扫掉**。

### 根因（与 min_grade 问题同源：PG seed 与代码 `_DEFAULTS` 系统性漂移）
信号塔 `hexp_engine.py` 的极值护栏阈值在 `hcm_config.metadata`（PG seed）中被漂移成**更激进/更松的错误值**：
- `hexp.extreme.mm_retreat_min`: seed=**0.02**，代码 `_DEFAULTS`=**0.20** → extreme_guard 的拦截条件 `_mm_aligned < 0.02` 几乎永不命中，极值高位 BUY 全部放行追顶。
- `hexp.momentum_drain_mm`: seed=**0.02**，代码=**0.15** → 高位接刀的动能枯竭保护失效。
- `hexp.extreme.k_extreme`: seed=**2.3**，代码=**1.8**；`high_pct` 0.8→0.85、`low_pct` 0.2→0.15、`wick_min` 0.5→0.6 一并漂移。
- 配套 `reversal_sl_atr_mult=0.5`（代码同）把极值区 SL 收紧到 0.5 ATR，高位轻微回撤即扫损。

**机制链**：pos>0.85（极端区）+ 动量仅微正（≈0.04）→ 本应被 extreme_guard 拦（阈值 0.20）→ 但 seed 阈值被压成 0.02，不拦 → 放行"极值追单"在顶部 BUY → SL 仅 0.5 ATR → 价格稍回落即止损 = **"开在最高点又止损"**。
（日志铁证：14:28–14:29 XAUUSD M5 `pos=0.87~0.94 mm_aligned=0.04` 连续 `EXTREME CHASE allowed`；14:35 后动量转负才被 flip 拦。）

### 修复（已落地并热重载验证）
1. 迁移 `0020_hexp_extreme_drift_fix.sql`：把 6 个漂移键的 `default_value`/`current_value` 对齐回代码 `_DEFAULTS`（带 CASE 仅覆盖明显漂移值，不误伤有意调优）。
2. Redis `HSET hcm:config:v2` 立即生效 + `PUBLISH ... invalidate`；信号塔日志确认 `hot-reloaded | mm_retreat_min: 0.02→0.2 ...`。
3. 后续：其余 hexp 键（mtf 权重互换、scorecard 权重、resonance penalty 等）也存在 seed 漂移，建议一并核对但对本次 bug 非直接成因，列入回归清单。

### 新增铁律（补充）
> **铁律 5（配置 seed 必须对齐代码 `_DEFAULTS`，护栏阈值漂移即实盘事故）**：
> - `hcm_config.metadata` 的 `current_value`/`default_value` 是配置真相源的 L3，必须与引擎 `_DEFAULTS` 一致。**护栏/风控类阈值（如 `*_min`、`*_mm`、`*_pct`、`*_sl_atr_mult`）一旦被压低/放宽，会直接造成实盘亏损**，属 P0 级配置事故。
> - 任何改 `_DEFAULTS` 的提交，必须同步出 migration 修正 PG seed（反之 seed 被手动改也必须回写代码）。
> - 增加 CI/启动自检：对比 `_DEFAULTS` 与 PG `metadata` 的所有 `hexp.*` 键，存在 drift 则告警/阻断启动（防止"又"复现）。
> - 排查"为什么单子开在极值点"时，第一动作是核对相关护栏键的运行时值（`redis-cli HGET hcm:config:v2 <key>`），而非只看代码 `_DEFAULTS`（代码对、PG/Redis 错 = 运行时仍错）。

### 可复用改动
- 本次暴露 signal-tower 容器已 bind mount `hexp_engine.py`（改源码重启即生效），但**配置真相在 PG+Redis**，代码改了不等于配置对——验证时务必 `HGET` 运行时值。
- `docker logs --since` + `grep "EXTREME CHASE" / "BLOCK hexp_extreme_guard"` 是判断"极值单是否被正确拦截"的最快手段。

---

## 八、延伸事故：调参只写 Redis 被 PG 回填复原（2026-08-24）

### 现象
用户按第七节调优 `hexp.extreme.k_extreme=2.2`、`hexp.extreme.mm_retreat_min=0.03`、`hexp.momentum_drain_mm=0.04`，但一段时间后 `redis-cli HGET hcm:config:v2` 显示它们**回到 2.0 / 0.02 / 0.06**（PG `updated_at` 停在 08-21，用户调优值从未落 PG）。复盘订单 245098349（BUY 动量转负后止损）时发现。

### 根因（与第七节同源：配置真相层不一致，但本次是"写路径"问题）
`shared/config_provider.py` 的 `ConfigProviderV3` 是三层配置（L1 本地缓存 → L2 Redis `hcm:config:v2` → L3 PG `hcm_config.metadata`）。关键机制：
- `get()` 未命中 L1/L2 → 走 L3 `_load_from_pg()`，用 PG 的 `COALESCE(current_value, default_value)` 取值，**并回填写回 Redis**（447-472 行）。
- **用户/手工调参若只 `HSET hcm:config:v2`（只改 Redis 缓存、不改 PG Source of Truth），则一旦该键回填/失效/重建，`_load_from_pg` 用 PG 旧值覆盖 Redis → 调优值丢失** = "调参被复原"。

> 注：这次订单 245098349 的止损**并非参数被复原造成**（`mm=-0.0329` 绝对值 < `momentum_flip_mm=0.04`、`er=0.289>0.20` 未枯竭、`pos=0.6862` 未达极值区，三个护栏在任何参数下都不拦），它是阈值内正常放行的单子。但"调参被复原"是**独立的系统性缺陷**，本次一并修复。

### 修复（已落地并双写验证）
把调优值**双写 PG（Source of Truth）+ Redis（缓存）**：
1. `UPDATE hcm_config.metadata SET current_value='2.2', updated_at=now() WHERE config_key='hexp.extreme.k_extreme'`（同法更新 `mm_retreat_min=0.03`、`momentum_drain_mm=0.04`、`momentum_flip_mm=0.04`、`reversal_sl_atr_mult=0.5`）。
2. `HSET hcm:config:v2 <key> <value>` + bump `hcm:config:version` 时间戳，触发热加载。
3. 验证 PG == Redis 一致，signal-tower 30s reload 完成。

### 新增铁律（补充）
> **铁律 6（改参数必须 PG+Redis 双写，严禁只改 Redis 缓存）**：
> - 配置真相源是 PG `hcm_config.metadata`（`current_value`），Redis `hcm:config:v2` 只是 L2 缓存。**任何调参（手工 SQL/脚本/面板）都必须写 PG（Source of Truth）**，Redis 是回填缓存，会在键缺失时用 PG 值覆盖——**只改 Redis 的值迟早被复原成 PG 旧值**。
> - 正确姿势：写 PG（`UPDATE metadata SET current_value=...`）+ 写 Redis（`HSET hcm:config:v2`）+ bump `hcm:config:version` 时间戳 +（可）`PUBLISH hcm:config:invalidate <key>`。或直接走前端面板（其 PUT 经 `ConfigProviderV3.set/set_batch` 自动双写）。
> - 排查"参数不生效/被复原"时，**同时核对 PG `current_value` 与 Redis 运行时值**（`HGET`），两者不一致 = Redis 将被 PG 回填覆盖，先修 PG。
