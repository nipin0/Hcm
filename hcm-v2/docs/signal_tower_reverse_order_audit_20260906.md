# 信号塔反向下单审计（只读）— 2026-09-06

## 0. 结论先行（TL;DR）
- **修复后，信号塔不会"信号方向与成交方向错位"地下反向单。** 实时库硬证据：1847 笔有关联信号的持仓，持仓 `direction` 与信号 `signal_dir` **100% 一致**，严格反向不匹配数 = **0**（SELL→SELL 986、BUY→BUY 861）。
- 生产确实开启了 **`hexp.reverse_order_enabled = true`**，但它是**设计内的逆势接刀单**（在极值区把被 momentum_flip 封掉的顺势方向翻成反向候选，再经风控接刀护栏后真下单）。翻向后生成的 `signal_dir` 本身就是反向值，持仓 `direction` 跟随它 → 链路内自洽、无错位。
- 唯一会"强行用 AI 翻 HE XP 方向"的代码路径 `dir_lm_flip` 在生产**关闭**（`hexp.dir_lm_flip_enabled` 未配置 → 默认 False），且即使开启，当前 `ai_dir_prob=0.4424` 也远低于触发阈值 0.65。

## 1. 审计范围与方法
- 代码：信号塔 `hcm-signal-tower/signal_tower/hexp_engine.py`、`signal_publisher.py`；桥 `tools/mt5_bridge.py`；跟单 `hcm-copy-trading/.../stream_consumer.py`。
- 实时库：PG `hcm_trading.positions`(direction, signal_id) JOIN `hcm_signal.signals`(signal_id, signal_dir)。
- 运行态配置：Redis `hcm:config:*` 与 `hcm:live:hexp:*`。
- 全程**只读**，未改动任何生产数据 / 配置 / 进程。

## 2. 信号塔方向策略链路（HEXP 引擎）
- `dir_sum` 加权（adx/er/ma/bbw/hurst/rsi/mm）→ 迟滞死区/强制翻转 → `cand`（dir_sum>0→BUY，否则 SELL）→ `direction = cand`（`hexp_engine.py:1530`）。
- 方向字段全程为字符串 `direction`（BUY/SELL/NO_TRADE），**不存在 `side` 字段**。
- 趋势优先兜底 `trend_priority`（`hexp_engine.py:1565+`）：主周期状态机确认 TREND 时收口逆势单为 NO_TRADE。

## 3. 逻辑链路冲突点排查
### A. dir_lm_flip（AI 强制翻向）— 关闭
- `hexp_engine.py:1537-1563`，需 `hexp.dir_lm_flip_enabled=True` + `ai_dir_prob≥0.65` + 连续 2 棒反向。Redis 未配置 → 默认 False。实时快照 `ai_dir_prob=0.4424`、`ai_direction=HOLD`（raw SELL）→ 即便开启此刻也不触发。
- 这是**唯一**会把 `direction` 赋成 AI 相反值的代码路径。

### B. reverse_order（逆势接刀单）— 开启，设计内
- `hexp_engine.py:2392-2468`：`momentum_flip` 先把顺势方向封 NO_TRADE 并标 `_flip_block`；reverse_candidate 在极值区（顶部 BUY 被拦→SELL 候选，底部 SELL 被拦→BUY 候选）产出反向候选。
- `hexp.reverse_order_enabled=True` 时 `order_intent=True`，scheduler 覆写 `final_direction` 并经 `_check_reverse_order` 接刀护栏后真下单。
- **关键点**：这是"信号自身方向被翻转"（signal_dir=SELL），不是"持仓相对信号反向"。桥/跟单原样透传该方向 → 持仓与信号一致。

### C. trend_priority / range_hurst 均值回归拦截 — 收口逆势
- `:1565+` trend_priority；`:2476+` range_hurst 在震荡市均值回归态拦高位追单。二者都是"封单/改 NO_TRADE"，**不翻向**。

### D. value_drive / direction_fuse / value_world — 不存在于方向管线
- 全仓 grep `hexp_engine.py` 对 `value_drive`/`direction_fuse`/`value_world` **无匹配** → 这些开关在当前 HEXP 方向裁决中并非活跃风险（早前记忆中的 B/C 风险项在当前代码已不适用）。

## 4. 代码 BUG 排查
- 全文件 `direction = "SELL"/"BUY"` 赋值仅命中 `hexp_engine.py:1530 direction = cand`（正常裁决输出），**无意外翻向赋值**。
- 桥 `mt5_bridge.py:1566` `BUY→ORDER_TYPE_BUY / SELL→ORDER_TYPE_SELL`（无取反）；`:1567` 价格 ask/bid 正确；镜像（position_sync）只动 SL/TP；反转头（reversal）只调 SL；copy_trading 原样透传字符串 `direction`。
- → 桥对方向零二次校验，**真反向下单根因只可能在信号生成侧**；而生成侧已证无错位（见第 5 节）。

## 5. 实时库反向核对（硬证据）
- positions_total = 2374；joined_with_signal = 1847；null signal_id = 527。
- 交叉分布：
  - (SELL, SELL) 986
  - (BUY, BUY) 861
- **严格反向不匹配数 REVERSE_MISMATCH_COUNT = 0**。
- 枚举：positions.direction ∈ {SELL:1200, BUY:1174}；signals.signal_dir ∈ {NO_TRADE:8100, BUY:2599, SELL:2111, MODIFY:72, CLOSE:2}。

## 6. 生产配置运行态真值（Redis）
| key | 值 | 含义 |
|---|---|---|
| hexp.reverse_order_enabled | true | 逆势接刀单已启用（设计内） |
| hexp.dir_lm_flip_enabled | nil → 默认 False | AI 强制翻向关闭 |
| hexp.value_drive_enabled | nil（代码无此键） | 非活跃 |
| ai.lm.direction_fuse | nil（代码无此键） | 非活跃 |
| ai.lm.dir_enabled | nil | 见注 |

- 实时 AI 快照 `hcm:live:hexp:ai:XAUUSD`：`ai_direction=HOLD`, `ai_dir_prob=0.4424`, `ai_direction_raw=SELL`, `mode=decoupled`, `valid=true`, `model_loaded=true`。

## 7. 那 527 笔未关联持仓（数据盲区）
- 全部 `signal_id IS NULL`（0 笔是"孤儿 signal_id"）。方向分布 BUY 313 / SELL 214，时间跨度 2026-07-14 ~ 2026-09-11。
- 它们没有可比对信号，**无法纳入"信号反向"审计**，但也不是反向证据。疑似手动/历史成交或 signal_id 未落库（数据链路缺口），建议单独排查 positions.signal_id 落库完整性。

## 8. 最终结论
**修复后信号塔不会"成交方向与信号指定方向相反"（即信号→持仓错位型 BUG）地下单。**
1. 链路全口径方向一致（代码 + 实时库双证，0 不匹配）。
2. 生产开启的 `reverse_order` 是**策略级逆势接刀单**，方向在信号侧已自洽，不是 BUG；其接刀护栏 `_check_reverse_order` 仍是最后防线。
3. `dir_lm_flip` 强制翻向路径关闭，且当前 AI 概率不足，无 AI 翻向风险。

## 9. 残留风险与建议
1. **527 笔 null signal_id 持仓**：排查 positions.signal_id 落库完整性（是否手动/外部 EA 成交未带 signal_id）。若是 reverse_order 路径漏写 signal_id，需补；若是手动单，应在审计口径中剔除。
2. **reverse_order 逆势接刀单**：虽设计内，建议持续观察 `reverse_candidate` 命中率与接刀护栏拦截率（已有 observation 日志），确认不偏离预期。
3. **dir_lm_flip 若未来启用**：务必先把 `ai_dir_prob` 阈值与连续棒数设保守，并确认 `_dir_hyst_state` 回写（:1553）已就位，否则会出现"翻 1 棒即被弹回"的抖动（代码注释已记载该坑）。
4. 实时核对脚本建议固化为每日巡检，监控 REVERSE_MISMATCH_COUNT 是否突增。

## 10. 附：复现命令（只读）
```sql
-- 反向不匹配数
select count(*) from hcm_trading.positions p
join hcm_signal.signals s on s.signal_id = p.signal_id
where (p.direction='BUY' and s.signal_dir='SELL') or (p.direction='SELL' and s.signal_dir='BUY');
```
```bash
docker exec hcm-v2-redis-1 redis-cli get hcm:config:hexp.reverse_order_enabled
docker exec hcm-v2-redis-1 redis-cli get hcm:live:hexp:ai:XAUUSD
```
