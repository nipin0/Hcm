-- 0041_osc_box_spec57.sql
-- 【2026-09-15】震荡态箱体规格（§57）上线：seed 8 个新键
--
-- 依据：用户 2026-09-15 规格（12.1–12.5 + 止损/冻结/边界场景）。
-- 消费者：signal_tower/state_strategy.py（`StateStrategy.load_config` → S1 分支）。
--
-- ── 取值原则（**只启有依据的值，不猜**）──────────────────────────────
--   键                          值            依据
--   ───────────────────────────────────────────────────────────────────
--   osc.bands_mode              quantile      规格 12.1 明确推荐"95%分位数/5%分位数"
--   osc.q_high / osc.q_low      0.95 / 0.05   同上（规格给了明确数值）
--   osc.entry_confirm_bars      2             规格场景2"连续 2 根 K 跌破"→ 防抖根数 2
--   osc.break_confirm_bars      2             同上（规格给的确认根数就是 2）
--   osc.tp_mode                 mid           规格 12.3-1"优先中轨落袋，更稳"= 默认
--   osc.buffer_mode             **atr**       ⚠ **规格未给 buffer 数值** → **不启用百分比**，
--                                             沿用既有 `state.osc_border_tol_atr=0.25`（ATR 归一）
--   osc.buffer_pct              0.001         仅登记（让面板可见可改），**当前不生效**
--   osc.tp_pct                  0.5           仅登记（tp_mode≠pct 时不生效）
--
--   ⇒ buffer 留待用户给数值：规格说"设置价格缓冲buffer"但未给具体值，我不猜
--     （尤其它是对**入场价**的直接影响，猜错=直接改成交价）。
--
-- ── 本次一并生效的既有键（未改，列出以便对照）────────────────────
--   state.box.window = 20（规格的 N）
--   state.osc_box_min_width_atr = 1.0（规格 MinBandHeight，ATR 归一）
--   state.osc_border_tol_atr = 0.25（ATR 口径的 buffer）
--   state.osc_lot_ladder = 0.5,1.0,1.5,2.0（梯度加仓，见 0040）
--
-- ── ⚠ 上线前必读：三个已知限制 ────────────────────────────────────
--   1. **本批参数没有离线 A/B**。四个新开关（quantile / 防抖2 / 突破止损2 / TP中轨）
--      是**同时**上线的 —— 规格把它们作为一个设计整体给出，故按整体上线；
--      但代价是"若效果不对，无法从数据上直接归因到哪一个"。灰度期手数 0.01~0.03，
--      且 S1 出手率实测仅 ~2.4% bar ⇒ 暴露面小，可用观测数据反推。
--   2. **突破止损依赖桥侧 Stage B**（mt5_bridge 消费 `exit_now`+`exit_scope`）。
--      桥未重启前，塔会下发指令而**桥不动作** = "配置了≠生效"。故本次**必须重启桥**。
--   3. `osc.break_confirm_bars` 启用后，**有箱体持仓时**破界 2 根即市价离场（硬止损之外的
--      第二道保护）。这是"不扛单"的落地，也是**唯一会主动平仓**的塔侧逻辑。
--
-- ── 回滚（两种粒度）──────────────────────────────────────────────
--   全关：state.order_enabled=false（回到纯观测）
--   只关突破止损：osc.break_confirm_bars=0（塔不再下发离场指令，桥侧分支随之不触发）
--   回滚既有行为：osc.bands_mode=extremum + osc.entry_confirm_bars=1 + osc.break_confirm_bars=0
--
-- ⚠ 写 PG 不足够：ConfigProviderV3 是 L1 → **L2(Redis hcm:config:v2)** → L3(PG)，
--   **Redis 命中即返回**。必须对每个键一并 HSET + PUBLISH（见文件末尾命令）。

INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('osc.bands_mode', 'state', 'extremum', 'quantile', 'string',
     '箱体边界算法',
     'extremum=窗口内原始极值（既有）；quantile=上沿取 high 的 q_high 分位、'
     '下沿取 low 的 q_low 分位（削插针极值）。当前=quantile（规格 12.1 推荐）。'),
    ('osc.q_high', 'state', '0.95', '0.95', 'number',
     '箱体上沿分位数', 'bands_mode=quantile 时生效（规格 12.1 的 95%）。'),
    ('osc.q_low', 'state', '0.05', '0.05', 'number',
     '箱体下沿分位数', 'bands_mode=quantile 时生效（规格 12.1 的 5%）。'),
    ('osc.buffer_mode', 'state', 'atr', 'atr', 'string',
     '入场价格缓冲口径',
     'atr=close≤下沿+tol_atr（ATR 归一，跨品种可移植，**当前**）；'
     'pct=规格 12.2 的 LowBand*(1+buffer)/HighBand*(1-buffer)。'
     '⚠ 规格未给出 buffer 数值，故不启用 pct。'),
    ('osc.buffer_pct', 'state', '0.001', '0.001', 'number',
     '入场价格缓冲（百分比）',
     'buffer_mode=pct 时生效。⚠ 百分比随价位量级漂移：XAUUSD(≈4300) 的 0.001≈4.3 美元，'
     '换品种即不同 —— 这是它默认不启用的原因（同 trend_direction 对斜率做 ATR 归一）。'),
    ('osc.entry_confirm_bars', 'state', '1', '2', 'int',
     '入场防抖根数',
     '连续 N 根满足同侧边界条件才允许开仓（规格 12.2「满足防抖 K 线校验」）。'
     '1=既有行为（单根即触发）；当前=2（规格场景 2 的确认根数）。'
     '防的是"单根插针即触发 → 抄底摸顶被反向收割"。'),
    ('osc.tp_mode', 'state', 'mid', 'mid', 'string',
     '箱体止盈口径',
     'mid=箱体中轨（规格"优先中轨落袋，更稳"）；far=对边；pct=箱体高度×tp_pct。'),
    ('osc.tp_pct', 'state', '0.5', '0.5', 'number',
     '箱体止盈比例', 'tp_mode=pct 时生效：盈利 = (上沿−下沿) × 该比例，自入场价起算。'),
    ('osc.break_confirm_bars', 'state', '0', '2', 'int',
     '箱体突破止损·确认根数',
     '连续 N 根收盘破界（多单破下沿 / 空单破上沿）→ 判定震荡失效，**立即市价离场**'
     '（规格 12.3-2 第 1 条，双止损中的"逻辑止损"）。0=关闭（既有行为）；当前=2。'
     '⚠ 唯一会主动平仓的塔侧逻辑；依赖桥侧消费 exit_now+exit_scope（Stage B）。')
ON CONFLICT (config_key) DO UPDATE SET
    current_value = EXCLUDED.current_value,
    updated_at = now();

-- ── 迁移后须执行的 Redis L2 同步（否则本批全部不生效）──────────────
-- redis-cli HSET hcm:config:v2 osc.bands_mode quantile
-- redis-cli HSET hcm:config:v2 osc.q_high 0.95
-- redis-cli HSET hcm:config:v2 osc.q_low 0.05
-- redis-cli HSET hcm:config:v2 osc.buffer_mode atr
-- redis-cli HSET hcm:config:v2 osc.buffer_pct 0.001
-- redis-cli HSET hcm:config:v2 osc.entry_confirm_bars 2
-- redis-cli HSET hcm:config:v2 osc.tp_mode mid
-- redis-cli HSET hcm:config:v2 osc.tp_pct 0.5
-- redis-cli HSET hcm:config:v2 osc.break_confirm_bars 2
-- redis-cli PUBLISH hcm:config:invalidate osc.bands_mode   （逐键发布）
