-- 0035_state_trend_entry_mode.sql
-- 【2026-09-15 L4 买点】趋势态首次入场的"回调触价"模式参数 seed
--
-- 依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §34（买点规则对比）§49（触价入场落地）
--
-- 背景：L4 买点此前只有一条口径 —— `_pullback_entry`：**bar 收盘时**检查"是否已自近期极值
--       回撤 ≥ pullback_atr×ATR"，满足则**市价**入场。它回答的是"价已经回撤到位了吗"，
--       而不是"让价格回撤到某个位再入场"。规格/路线图（§20.4-3 / §30.4-3 / §32.5-2 三处）
--       一直把**回调触价入场**列为待办。
--
-- 本次落地方式（**零改桥**）：复用桥侧**既有** P1a zone-trigger gate
--       （tools/mt5_bridge.py:4200-4218 + _recheck_zone_pending:2988）——
--       其语义正是"价位未到 → 存 deferred（带 TTL）→ 每 2-5s 复查 →
--       价格触及即成交 / 超时自动作废"，且已带**信号新鲜度闸门**与 T3c 尖刺过滤。
--       桥侧开关 signal_tower.zone_trigger_enabled 生产**已为 true**，
--       filtered / HEXP:Regime.* 信号长期在跑这条路径（久经验证）。
--       塔只需在下单信号里带 zone_level + entry_trigger_wait；
--       风控白名单**已透传**这两个字段（stream_consumer.py:596-600）。
--
-- 安全边界：
--   1. state.trend.entry_mode 默认 close_check = 既有行为**逐位不变**
--      （仅当"已回踩到位"的那根 bar 才市价入场）。
--   2. 切到 zone_touch 后语义是**严格超集**：已回踩 → 仍市价入场；
--      未回踩 → 才额外下发触价单。**不会**把已有的入场路径改掉。
--   3. state.trend.entry_wait_sec=0 时不下发触价（等价 close_check）。
--   4. ⚠ **尚未标定**：§34.3 实测显示 M5 上各买点规则期望值**均为负**
--      （根因是方向符号反向，§20），故"触价"相对"回踩到位市价"的增量收益
--      必须由**离线评估**给出后才可切换。本键的默认值即为此设的闸。
--
-- 生效方式：塔 load_config 30s 热重载 → 配置**无需重启**（但容器需已含本次代码）。

INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('state.trend.entry_mode', 'state', 'close_check', 'close_check', 'string',
     '趋势首次入场模式',
     'close_check=仅当 bar 收盘时已回踩到位才市价入场（既有行为，默认）；'
     'zone_touch=未回踩到位时额外下发触价入场位，交桥侧既有 zone gate 等价格回落到位再成交'
     '（超时按 entry_wait_sec 作废）。触价模式的增量收益尚未离线标定'
     '（§34.3：M5 上各买点规则期望均为负，根因是方向问题），切换前须先出评估证据'),
    ('state.trend.entry_wait_sec', 'state', '300', '300', 'int',
     '触价入场最长等待(秒)',
     '触价单在桥侧 deferred 的最长存活时间，超时由 Redis TTL 自动作废（不会留下挂单）。'
     '默认 300 约等于 1 根 M5，使每 bar 至多存在一个未成交触价单（塔侧另有去重闸）。'
     '0 = 不下发触价，等价 close_check')
ON CONFLICT (config_key) DO UPDATE SET
    default_value = EXCLUDED.default_value,
    current_value = EXCLUDED.current_value,
    updated_at = now();
