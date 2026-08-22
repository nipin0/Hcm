-- ─────────────────────────────────────────────────────────────────────────
-- 和乘幂(hexp)影子模式：上线迁移 SQL
-- 作用：在生产库建立准确率评估表 + 种子配置键（幂等，可重复执行）。
-- 来源：deploy/init.sql 中 hcm_signal.hexp_shadow_eval 表与 hexp.shadow.* 种子。
-- 部署方式（任选其一）：
--   A) 直连 PG 执行本文件；
--   B) 经 config_provider.set 双写（推荐，自动 PG+Redis+PUB）：
--      config_provider.set('hexp.shadow_enabled','false')   # 先关闭，验证后再开
--      config_provider.set('hexp.shadow.eval_bars','60')
--      config_provider.set('hexp.shadow.dir_atr_ratio','0.5')
-- ─────────────────────────────────────────────────────────────────────────

-- 1) 准确率评估表（影子信号模拟"假设成交"结果）
CREATE TABLE IF NOT EXISTS hcm_signal.hexp_shadow_eval (
    signal_id      BIGINT PRIMARY KEY REFERENCES hcm_signal.signals(signal_id),
    symbol         VARCHAR(20) NOT NULL,
    time_frame     VARCHAR(10) NOT NULL,
    signal_dir     VARCHAR(10) NOT NULL,
    entry_price    DECIMAL(12,5),
    sl_price       DECIMAL(12,5),
    tp1            DECIMAL(12,5),
    created_at     TIMESTAMPTZ,
    eval_at        TIMESTAMPTZ DEFAULT now(),
    horizon_bars   INT,
    outcome        VARCHAR(10),   -- win / loss / expired
    dir_hit        BOOLEAN,       -- 窗口内价格朝预测方向移动 >= dir_atr_ratio*ATR
    pnl_r          REAL           -- 命中盈亏比(R multiple)：win=(TP-entry)/(entry-SL)，loss=-1.0
);

-- 2) 配置种子（hexp 影子模式总开关 + 评估参数）
--    注意：hcm_config.metadata 真实列名为 current_value（非 config_value）。
INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, description)
VALUES
    ('hexp.shadow_enabled',        'signal_tower', 'false', 'false', 'bool',  '和乘幂影子模式：双跑落库不下单，验证准确率'),
    ('hexp.shadow.eval_bars',      'signal_tower', '60',    '60',    'int',   '影子信号模拟命中回看 M5 棒数(≈5h)'),
    ('hexp.shadow.dir_atr_ratio',  'signal_tower', '0.5',  '0.5',  'float', '方向命中阈值=该比例×ATR')
ON CONFLICT (config_key) DO NOTHING;


-- 3) 自愈：若已有历史 hexp_shadow 信号但未评估（旧数据），清空让其被重新对账
--    （可选，仅当从早期测试升级时执行）
-- DELETE FROM hcm_signal.hexp_shadow_eval WHERE 1=0;
