-- 0050_market_state_log_box.sql
-- 【2026-09-17 D4】逐 bar 箱体落库 —— 让面板能按**真实历史箱体**分段绘制箱体三线。
--
-- 背景（同一处两次误读，根因都是"面板没有逐 bar 箱体真值"）：
--   ① 面板曾把 `ctx` 的**当前箱体**当三条"横贯全图"的静态线画 ⇒ 与任何历史时刻的箱体
--      无关，实测被读成"价格在箱底 / 开仓在箱底"（把排查方向带偏）；
--   ② 2026-09-17 改为只画最右 3 根后 ⇒ 用户反馈"**箱体三线看不见**"（叠加图表缩放后
--      连那 3 根也可能不在可视区）。
--
-- 现由信号塔在每根 bar 落 `intent.box_*`：`state_strategy` 已按"冻结箱 / 滚动箱"分支
-- 归一（冻结轮次内 `it.box_*` 就是锁定值）⇒ 本层**不在落库处重算**，保持箱体唯一实现点。
--
-- 幂等：`ADD COLUMN IF NOT EXISTS`；不建新索引（已有 (symbol,time_frame,bar_open_time)
--       唯一约束支撑 kline JOIN，新增列不改变访问路径）。

ALTER TABLE hcm_signal.market_state_log
    ADD COLUMN IF NOT EXISTS box_upper  double precision,
    ADD COLUMN IF NOT EXISTS box_lower  double precision,
    ADD COLUMN IF NOT EXISTS box_mid    double precision,
    ADD COLUMN IF NOT EXISTS box_frozen boolean;

COMMENT ON COLUMN hcm_signal.market_state_log.box_upper IS
    '逐 bar 箱体上沿；NULL = 该 bar 箱体不可算（K线/ATR 不足或策略层未就绪）。冻结轮次内 = 冻结箱上沿（规格 §7.1）';
COMMENT ON COLUMN hcm_signal.market_state_log.box_lower IS
    '逐 bar 箱体下沿（取值语义同上）';
COMMENT ON COLUMN hcm_signal.market_state_log.box_mid IS
    '逐 bar 箱体中轨（= 箱体止盈锚点，`osc.tp_mode=mid`）';
COMMENT ON COLUMN hcm_signal.market_state_log.box_frozen IS
    '该 bar 是否处于"冻结箱"轮次（true = 本轮已开仓、三线锁定；false = 每 bar 重算的滚动箱）';
