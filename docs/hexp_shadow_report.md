# 和乘幂（Hexp）信号影子验证 · 信号对照报表设计

> 目标：在「双跑落库、不下单」的 shadow 模式下验证和乘幂信号的准确度，使其达到可切主引擎的标准。
> 已落地代码：
> - `signal_tower/scheduler.py`：`_run_shadow_hexp()`（双跑落库）+ `_reconcile_hexp_shadow()`（K 线模拟评估）
> - `web/api/signal_tower.py`：`GET /api/v1/signal-tower/hexp-shadow`（对照报表）
> - `deploy/init.sql` + `tools/migrate_hexp_shadow.sql`：`hcm_signal.hexp_shadow_eval` 表与配置种子

---

## 一、核心设计原则：双跑、落库、零实盘影响

和乘幂激活前最大的风险是「直接接管方向裁决 + 参数未回测」。影子模式用一条铁律规避：

- 每根 M5 棒收盘，`_produce_signal` 在产出 active（co_source）信号后，**额外**调用 `self._hexp_engine.produce(...)`；
- hexp 的 BUY/SELL 决策经 `SignalData(signal_mode="hexp_shadow")` 落 `hcm_signal.signals`，但 `publish(..., to_stream=False)` —— **只写 PG，不进 `signal:stream`**；
- 不进流 → 风控引擎不消费 → 桥不下单 → **零真实成交、零跟单副作用**；
- 开关 `hexp.shadow_enabled`（默认 `false`）控制；生产现网保持 `false`，验证阶段再开启。

落库样本除了 hexp 自身的 `hp_score / k / verdict / grade / 方向 / SL·TP 倍率`，还顺带记录与 active 模型的对照：`co_dir / co_agree / co_pre_score`，以便直接算「方向一致率」。

---

## 二、如何验证和乘幂信号的准确度（不依赖真实下单）

因为 hexp 影子信号永远不成交，准确度必须用**历史 K 线把信号模拟成「假设成交」**：

`_reconcile_hexp_shadow()` 每 60s 跑一次，挑出「已越过评估窗口、且尚未评估」的 hexp_shadow 信号：

1. 取该信号 `entry / sl / tp`（SL·TP 由 `co_exec_sl_atr_mult × ATR` 与 `RR` 推算，与真实下单同口径）；
2. 拉取 `created_at` 之后的 `hexp.shadow.eval_bars` 根 M5 K 线（默认 60 根 ≈ 5h）；
3. 逐根判定（先到先得）：
   - **BUY**：`low ≤ sl` → `loss`；`high ≥ tp` → `win`；否则若 `close ≥ entry + dir_atr_ratio×ATR` 标记 `dir_hit`
   - **SELL**：`high ≥ sl` → `loss`；`low ≤ tp` → `win`；否则若 `close ≤ entry − dir_atr_ratio×ATR` 标记 `dir_hit`
   - 窗口内两者皆未触达 → `expired`（计入方向命中，但不计 win/loss）
4. 结果写 `hcm_signal.hexp_shadow_eval`（幂等，`ON CONFLICT DO NOTHING`）。

**两个互补的准确率口径**：

| 口径 | 含义 | 说明 |
|---|---|---|
| **方向命中率 dir_hit** | 窗口内价格朝预测方向移动 ≥ 0.5×ATR | 衡量 hexp「方向对不对」，独立于 SL/TP 设置 |
| **SL/TP 胜率 win_rate** | `win / (win+loss)` | 衡量「按 hexp 给出的 R:R 能否赚钱」，最贴近真实下单效果 |
| **平均 R (avg_pnl_r)** | 盈利单 R 倍率均值（亏损单 = −1） | 期望收益，结合胜率判断系统期望是否为正 |

> 关键：这套模拟用**与实盘相同**的 SL/TP 倍率，因此 win_rate 是 hexp 切主引擎后的「近似真实胜率」，误差仅来自滑点/点差（模拟用 K 线 high/low 不含点差）。

---

## 三、信号对照报表结构（API 返回）

`GET /api/v1/signal-tower/hexp-shadow?hours=72&symbol=XAUUSD`

```jsonc
{
  "code": 0,
  "data": {
    "window_hours": 72,
    "symbol_filter": "XAUUSD",
    "co_direction_agreement": {   // ① hexp 与 active(co_source) 方向一致率
      "total": 120, "agree": 96, "agree_rate": 0.80,
      "hexp_buys": 70, "hexp_sells": 50
    },
    "accuracy": {                  // ② 模拟成交准确率
      "evaluated": 110, "wins": 58, "losses": 42, "expired": 10,
      "win_rate": 0.58,            // wins/(wins+losses)
      "direction_hit_rate": 0.66,
      "avg_pnl_r": 0.21
    },
    "by_regime": [                 // ③ 分体制明细（定位 hexp 在哪个市况失灵）
      {"regime":"TREND","total":40,"agree_rate":0.85,"win_rate":0.62,"wins":25,"losses":15,"avg_pnl_r":0.34},
      {"regime":"RANGE","total":35,"agree_rate":0.71,"win_rate":0.49,"wins":17,"losses":18,"avg_pnl_r":-0.05},
      {"regime":"NEUTRAL","total":35,"agree_rate":0.83,"win_rate":0.60,"wins":16,"losses":9,"avg_pnl_r":0.40}
    ],
    "recent_samples": [            // ④ 近 50 条逐笔对照（前端表格用）
      {"signal_id":...,"symbol":"XAUUSD","direction":"BUY","pre_score":0.62,
       "hp_score":0.71,"grade":"A","verdict":1.2,"regime":"TREND",
       "co_direction":"BUY","co_agree":true,"co_pre_score":0.55,
       "created_at":"...","outcome":"win","dir_hit":true,"pnl_r":1.10}
    ],
    "note": "影子模式：hexp 决策仅落库不下单，准确率为 K 线模拟假设成交结果，非真实账户盈亏。"
  },
  "message": "ok"
}
```

### 报表四类指标解读

1. **方向一致率（co_direction_agreement）**：hexp 与现生产 co_source 同向的比例。
   - 过低（< 0.6）→ hexp 与现有护栏严重分歧，需排查体制/方向裁决逻辑；
   - 过高（≈ 1.0）→ hexp 只是 co_source 的复读机，没有增量价值，要确认它是否真的在独立决策。
2. **SL/TP 胜率（accuracy.win_rate）**：与 co_source 历史 40%+ 胜率对比的硬指标。
3. **平均 R（avg_pnl_r > 0）**：胜率×R 期望为正才是可持续系统（如 0.55 胜率 + 1.0 R → 期望 +0.10）。
4. **分体制明细（by_regime）**：定位 hexp 在 TREND / RANGE / NEUTRAL 哪类市况失灵——对应调 `hexp.mtf.*` 共振权重或 `hexp.k.*` 凸凹系数。

---

## 四、上线与验证步骤

```bash
# 1. 生产库建表 + 种子配置（执行 tools/migrate_hexp_shadow.sql 或 config_provider.set 双写）
# 2. 重建信号塔（scheduler.py 已 bind mount，源码即权威）
docker compose restart hcm-signal-tower
docker compose restart hcm-web          # 报表接口生效

# 3. 开启影子模式（仅落库不下单）
config_provider.set('hexp.shadow_enabled','true')

# 4. 等待积累样本（建议 ≥ 72h，跨亚/欧/美三盘，样本 ≥ 80 条 BUY/SELL）
# 5. 拉报表
curl -H "Authorization: Bearer <token>" \
  'http://localhost/api/v1/signal-tower/hexp-shadow?hours=72&symbol=XAUUSD'
```

### 切主引擎的准入门槛（建议，需业务确认）
- 影子样本 `evaluated ≥ 80`；
- `win_rate ≥ 0.52` 且 `avg_pnl_r > 0`；
- `by_regime` 中无「胜率 < 0.45 且样本 ≥ 15」的灾难性市况；
- 方向一致率落在 `0.65–0.90`（既非复读机、也非系统性逆势）。

满足后，再经 `_detect_active_model` 把全局 `active_model` 切到 `hexp`，并把 `hexp.mtf.*` 护栏参数经 PG/Redis 双写固化。

---

## 五、已知边界与后续
- **M1 数据可用性**：hexp 的 MM 微结构动量依赖 M1 K 线；若 M1 因时钟偏差写不进 PG，MM 因子退化为噪声，HP-Score 失真。验证期需先确认 M1 入库健康。
- **滑点/点差未建模**：模拟用 K 线 high/low，不含点差；真实 R 会略低于 `avg_pnl_r`。
- **参数未回测**：hexp.* 71 个参数当前走 `_DEFAULTS` 设计初值，验证期应经回测/seed 固化最优值，而非直接上线默认值。
- **前端页面（可选）**：报表接口已就绪，可在 SignalFunnel 同级加「和乘幂影子对照」页面消费该接口；当前先用 curl/Postman 看数据。
