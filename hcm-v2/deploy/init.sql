-- ═══════════════════════════════════════════════════════════════
-- HCM v2 — Complete Database DDL
-- 版本: v1.0 | 基于 PRD v1.3 附录 A
-- 建表顺序: Level 0 → Level 1 → Level 2 → Level 3 (严格按外键依赖)
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ============================================================
-- Phase 0: Schema 创建 (8 个 Schema)
-- ============================================================

CREATE SCHEMA IF NOT EXISTS hcm_system;
CREATE SCHEMA IF NOT EXISTS hcm_config;
CREATE SCHEMA IF NOT EXISTS hcm_broker;
CREATE SCHEMA IF NOT EXISTS hcm_signal;
CREATE SCHEMA IF NOT EXISTS hcm_market;
CREATE SCHEMA IF NOT EXISTS hcm_trading;
CREATE SCHEMA IF NOT EXISTS hcm_copy;
CREATE SCHEMA IF NOT EXISTS hcm_ai;
CREATE SCHEMA IF NOT EXISTS hcm_risk;

-- ============================================================
-- Level 0: 无外键依赖（先创建）
-- ============================================================

-- ── hcm_system.roles ──────────────────────────
CREATE TABLE hcm_system.roles (
    role_id       SERIAL PRIMARY KEY,
    role_name     VARCHAR(30) UNIQUE NOT NULL,
    permissions   JSONB NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_system.users ──────────────────────────
CREATE TABLE hcm_system.users (
    user_id       SERIAL PRIMARY KEY,
    username      VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    display_name  VARCHAR(50),
    role_id       INT REFERENCES hcm_system.roles(role_id),
    is_active     BOOLEAN DEFAULT true,
    last_login    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_system.auth_logs ──────────────────────
CREATE TABLE hcm_system.auth_logs (
    log_id        BIGSERIAL PRIMARY KEY,
    user_id       INT,
    event         VARCHAR(20),
    ip_address    INET,
    user_agent    TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_config.metadata ───────────────────────
CREATE TABLE hcm_config.metadata (
    config_key    VARCHAR(100) PRIMARY KEY,
    category      VARCHAR(50) NOT NULL,
    subcategory   VARCHAR(50),
    default_value TEXT NOT NULL,
    current_value TEXT,
    value_type    VARCHAR(20) NOT NULL,
    label         VARCHAR(100),
    description   TEXT,
    ui_control    VARCHAR(30) DEFAULT 'text',
    ui_options    JSONB,
    ui_order      INT DEFAULT 0,
    scope         VARCHAR(20) DEFAULT 'global',
    is_sensitive  BOOLEAN DEFAULT false,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_config.account_overrides ──────────────
CREATE TABLE hcm_config.account_overrides (
    override_id   SERIAL PRIMARY KEY,
    account_id    INT NOT NULL,
    config_key    VARCHAR(100) REFERENCES hcm_config.metadata(config_key),
    override_value TEXT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(account_id, config_key)
);

-- ── hcm_config.symbol_meta ────────────────────
CREATE TABLE hcm_config.symbol_meta (
    symbol          VARCHAR(20) PRIMARY KEY,
    category        VARCHAR(20) NOT NULL,       -- metals/crypto/forex
    display_name    VARCHAR(50),
    base_currency   VARCHAR(10) DEFAULT 'USD',
    quote_currency  VARCHAR(10),
    lot_step        DECIMAL(10,4) DEFAULT 0.01,
    min_lot         DECIMAL(10,2) DEFAULT 0.01,
    max_lot         DECIMAL(10,2) DEFAULT 5.0,
    pip_value       DECIMAL(10,2),
    is_active       BOOLEAN DEFAULT true,
    phase           INT DEFAULT 1,              -- 1=Phase1, 2=Phase2
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_broker.accounts ───────────────────────
CREATE TABLE hcm_broker.accounts (
    account_id         SERIAL PRIMARY KEY,
    account_name       VARCHAR(100) NOT NULL,
    account_number     BIGINT NOT NULL,
    password_enc       VARCHAR(512) NOT NULL,
    server_name        VARCHAR(200) NOT NULL,
    broker_name        VARCHAR(100),
    account_type       VARCHAR(20) DEFAULT 'master',
    env_type           SMALLINT DEFAULT 0,
    leverage           INT DEFAULT 100,
    base_currency      VARCHAR(10) DEFAULT 'USD',
    initial_deposit    DECIMAL(14,2) DEFAULT 0,
    is_active          BOOLEAN DEFAULT true,
    last_balance       DECIMAL(14,2),
    last_equity        DECIMAL(14,2),
    last_heartbeat     TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT now(),
    updated_at         TIMESTAMPTZ DEFAULT now()
);

-- Ensure account_number has a UNIQUE constraint (for ON CONFLICT upsert)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uk_accounts_account_number'
          AND contype = 'u'
    ) THEN
        ALTER TABLE hcm_broker.accounts
            ADD CONSTRAINT uk_accounts_account_number UNIQUE (account_number);
    END IF;
END $$;

-- ── hcm_broker.trade_rules ────────────────────
CREATE TABLE hcm_broker.trade_rules (
    rule_id                SERIAL PRIMARY KEY,
    account_id             INT REFERENCES hcm_broker.accounts(account_id),
    symbol                 VARCHAR(20) DEFAULT '*',
    lot_calc_mode          VARCHAR(20) DEFAULT 'FIXED',
    fixed_lot              DECIMAL(10,2) DEFAULT 0.01,
    risk_percent           REAL DEFAULT 1.0,
    balance_ratio          REAL DEFAULT 0.01,
    equity_ratio           REAL DEFAULT 0.05,
    max_lot                DECIMAL(10,2) DEFAULT 1.0,
    min_lot                DECIMAL(10,2) DEFAULT 0.01,
    sl_multiplier          REAL DEFAULT 1.5,
    tp1_multiplier         REAL DEFAULT 4.0,
    tp2_multiplier         REAL DEFAULT 5.0,
    max_daily_trades       INT DEFAULT 200,
    trailing_tp_enabled    BOOLEAN DEFAULT true,
    trailing_tp_activation_pips INT DEFAULT 125,
    trailing_tp_distance_pips   INT DEFAULT 75,
    trailing_tp_step_pips       INT DEFAULT 25,
    breakeven_enabled      BOOLEAN DEFAULT false,
    breakeven_activation_pips   INT DEFAULT 50,
    partial_close_enabled  BOOLEAN DEFAULT false,
    partial_close_tp1_ratio     REAL DEFAULT 0.5,
    max_hold_minutes       INT DEFAULT 0,
    is_active              BOOLEAN DEFAULT true,
    created_at             TIMESTAMPTZ DEFAULT now(),
    updated_at             TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_ai.prompt_templates ───────────────────
CREATE TABLE hcm_ai.prompt_templates (
    template_id      SERIAL PRIMARY KEY,
    name             VARCHAR(100),
    category         VARCHAR(50),
    prompt_content   TEXT NOT NULL,
    description      TEXT,
    is_active        BOOLEAN DEFAULT true,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Level 1: 依赖 Level 0
-- ============================================================

-- ── hcm_market.klines (分区表) ────────────────
CREATE TABLE hcm_market.klines (
    id            BIGSERIAL,
    symbol        VARCHAR(20) NOT NULL,
    time_frame    VARCHAR(10) NOT NULL,
    open_time     TIMESTAMPTZ NOT NULL,
    open          DECIMAL(12,5),
    high          DECIMAL(12,5),
    low           DECIMAL(12,5),
    close         DECIMAL(12,5),
    tick_volume   INT DEFAULT 0,
    -- [2026-07-30 A 组] Bar 质量分维度：点差 + 真实成交量（声明式分区，加列自动级联到各分区）
    spread        DECIMAL(10,2),
    real_volume   BIGINT DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT now(),
    -- [2026-08-05 D4] 写入源标识：bridge=mt5_bridge 主机进程 / collector=采集服务。
    -- 双写并存时用于诊断"谁在写 K线"，并防止双写污染无标识。
    source        VARCHAR(16) NOT NULL DEFAULT 'bridge',
    PRIMARY KEY (symbol, time_frame, open_time)
) PARTITION BY LIST (symbol);

-- ── K线分区: XAUUSD ───────────────────────────
CREATE TABLE hcm_market.klines_xauusd PARTITION OF hcm_market.klines
    FOR VALUES IN ('XAUUSD');

-- ── K线分区: BTCUSD ───────────────────────────
CREATE TABLE hcm_market.klines_btcusd PARTITION OF hcm_market.klines
    FOR VALUES IN ('BTCUSD');

-- ── hcm_market.ticks (分区表) ─────────────────
CREATE TABLE hcm_market.ticks (
    id            BIGSERIAL,
    symbol        VARCHAR(20) NOT NULL,
    bid           DECIMAL(12,5),
    ask           DECIMAL(12,5),
    spread        DECIMAL(10,2),
    volume        INT DEFAULT 0,
    timestamp     TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
) PARTITION BY RANGE (timestamp);

-- ── Tick 初始日分区 (部署当天) ──────────────────
CREATE TABLE hcm_market.ticks_20260709 PARTITION OF hcm_market.ticks
    FOR VALUES FROM ('2026-07-09') TO ('2026-07-10');
CREATE TABLE hcm_market.ticks_20260710 PARTITION OF hcm_market.ticks
    FOR VALUES FROM ('2026-07-10') TO ('2026-07-11');
CREATE TABLE hcm_market.ticks_20260711 PARTITION OF hcm_market.ticks
    FOR VALUES FROM ('2026-07-11') TO ('2026-07-12');

-- ── hcm_market.macro_snapshots ────────────────
CREATE TABLE hcm_market.macro_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    category        VARCHAR(20) NOT NULL,        -- metals/crypto/forex
    macro_risk_score INT DEFAULT 0,
    macro_bias      VARCHAR(20),
    ai_summary      VARCHAR(500),
    category_data   JSONB DEFAULT '{}',
    snapshot_time   TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_market.sentiment_snapshots ────────────
CREATE TABLE hcm_market.sentiment_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    category        VARCHAR(20) NOT NULL,
    sentiment_risk_score INT DEFAULT 0,
    sentiment_bias  VARCHAR(20),
    ai_summary      VARCHAR(500),
    category_data   JSONB DEFAULT '{}',
    snapshot_time   TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_market.event_calendar ─────────────────
CREATE TABLE hcm_market.event_calendar (
    event_id        SERIAL PRIMARY KEY,
    event_name      VARCHAR(200) NOT NULL,
    category        VARCHAR(20) DEFAULT 'all',   -- all / metals / crypto / forex
    event_date      TIMESTAMPTZ NOT NULL,
    importance      SMALLINT DEFAULT 1,          -- 1=MEDIUM, 2=HIGH, 3=CRITICAL
    flat_before_min INT DEFAULT 0,
    flat_after_min  INT DEFAULT 0,
    lot_scale       REAL DEFAULT 1.0,
    confidence_discount REAL DEFAULT 0.0,
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Level 2: 依赖 Level 0 + Level 1
-- ============================================================

-- ── hcm_signal.tasks ──────────────────────────
CREATE TABLE hcm_signal.tasks (
    task_id           BIGSERIAL PRIMARY KEY,
    symbol            VARCHAR(20) NOT NULL,
    time_frame        VARCHAR(10) NOT NULL,
    signal_mode       VARCHAR(30) DEFAULT 'indicator_scoring',
    composite_score   REAL,
    mode2_direction   VARCHAR(10),
    indicator_values  JSONB,
    request_body      JSONB,
    response_body     JSONB,
    tokens_in         INT DEFAULT 0,
    tokens_out        INT DEFAULT 0,
    model             VARCHAR(50) DEFAULT 'deepseek-chat',
    latency_ms        INT DEFAULT 0,
    status            SMALLINT DEFAULT 1,
    created_at        TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_signal.signals (含 v1.3 增强字段) ──────
CREATE TABLE hcm_signal.signals (
    signal_id         BIGSERIAL PRIMARY KEY,
    task_id           BIGINT REFERENCES hcm_signal.tasks(task_id),
    account_id        INT REFERENCES hcm_broker.accounts(account_id),
    symbol            VARCHAR(20) NOT NULL,
    time_frame        VARCHAR(10) NOT NULL,
    signal_dir        VARCHAR(10) NOT NULL,
    entry_price       DECIMAL(12,5),
    sl_price          DECIMAL(12,5),
    tp1               DECIMAL(12,5),
    tp2               DECIMAL(12,5),
    lot               DECIMAL(10,2) DEFAULT 0,
    confidence        REAL,
    priority          SMALLINT DEFAULT 1,
    risk_ratio        REAL DEFAULT 0,
    reason            TEXT,
    block_reason      TEXT,
    signal_status     SMALLINT DEFAULT 0,
    signal_mode       VARCHAR(30),
    indicator_values  JSONB,
    composite_score   REAL,
    valid_until       TIMESTAMPTZ,
    -- v2 增强字段 (双模式 + 外部因子溯源)
    signal_tower_mode VARCHAR(20) DEFAULT 'ai_dynamic',
    manual_regime_score INT,
    ai_context_score  INT,
    macro_snapshot_id BIGINT,
    sentiment_snapshot_id BIGINT,
    fallback_reason   VARCHAR(50),
    -- v1.3 五级市况溯源字段
    regime            VARCHAR(20),
    pre_score         REAL,
    weight_scheme     VARCHAR(30),
    position_in_range REAL,
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_signal.hexp_shadow_eval (和乘幂影子模式准确率评估) ──────
-- 影子模式：hexp 引擎双跑落库(signal_mode='hexp_shadow')但绝不进 signal:stream，
-- 故永不真实成交。本表用历史 K 线把每条 shadow 信号模拟成"假设成交"结果，
-- 供「信号对照报表」评估 hexp 方向准确率 / SL-TP 命中率 / 盈亏比(R)。
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

-- ── hexp 影子模式配置种子（生产前需经 config_provider.set 双写 PG+Redis 生效）──
-- hexp.shadow_enabled   : 是否开启双跑落库（默认 false → 不影响实盘）
-- hexp.shadow.eval_bars : 模拟命中回看 M5 棒数（默认 60 ≈ 5h）
-- hexp.shadow.dir_atr_ratio : 方向命中阈值 = 该比例 × ATR（默认 0.5）
INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, description)
VALUES
    ('hexp.shadow_enabled',        'signal_tower', 'false', 'false', 'bool',  '和乘幂影子模式：双跑落库不下单，验证准确率'),
    ('hexp.shadow.eval_bars',      'signal_tower', '60',    '60',    'int',   '影子信号模拟命中回看 M5 棒数(≈5h)'),
    ('hexp.shadow.dir_atr_ratio',  'signal_tower', '0.5',  '0.5',  'float', '方向命中阈值=该比例×ATR')
ON CONFLICT (config_key) DO NOTHING;

-- ── hcm_risk.intercept_logs ───────────────────
-- Stores risk interception events for audit trail.
-- (Design doc §9 item 1 — recommended approach B)
CREATE TABLE hcm_risk.intercept_logs (
    log_id        BIGSERIAL PRIMARY KEY,
    signal_id     BIGINT REFERENCES hcm_signal.signals(signal_id),
    symbol        VARCHAR(20) NOT NULL,
    rule_name     VARCHAR(100) NOT NULL,
    reason        TEXT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Level 3: 依赖所有上层
-- ============================================================

-- ── hcm_trading.orders ────────────────────────
CREATE TABLE hcm_trading.orders (
    order_id       BIGSERIAL PRIMARY KEY,
    signal_id      BIGINT REFERENCES hcm_signal.signals(signal_id),
    account_id     INT REFERENCES hcm_broker.accounts(account_id),
    client_id      VARCHAR(50),
    mt5_ticket     BIGINT,
    symbol         VARCHAR(20),
    direction      VARCHAR(10),
    open_price     DECIMAL(14,5),
    close_price    DECIMAL(14,5),
    lot            DECIMAL(10,2),
    sl             DECIMAL(14,5),
    tp             DECIMAL(14,5),
    commission     DECIMAL(10,2) DEFAULT 0,
    swap           DECIMAL(10,2) DEFAULT 0,
    profit         DECIMAL(10,2) DEFAULT 0,
    order_status   SMALLINT DEFAULT 0,
    open_time      TIMESTAMPTZ,
    close_time     TIMESTAMPTZ,
    close_reason   VARCHAR(50),
    trail_state    JSONB,
    error_code     INT DEFAULT 0,
    error_msg      TEXT,
    created_at     TIMESTAMPTZ DEFAULT now(),
    updated_at     TIMESTAMPTZ DEFAULT now()
);

-- ── hcm_trading.positions ─────────────────────
CREATE TABLE hcm_trading.positions (
    position_id    BIGSERIAL PRIMARY KEY,
    account_id     INT REFERENCES hcm_broker.accounts(account_id),
    order_id       BIGINT REFERENCES hcm_trading.orders(order_id),
    client_id      VARCHAR(50),
    mt5_ticket     BIGINT,
    symbol         VARCHAR(20),
    direction      VARCHAR(10),
    open_price     DECIMAL(14,5),
    current_price  DECIMAL(14,5),
    lot            DECIMAL(10,2),
    sl             DECIMAL(14,5),
    tp             DECIMAL(14,5),
    float_profit   DECIMAL(10,2) DEFAULT 0,
    trail_state    JSONB,
    open_time      TIMESTAMPTZ,
    snapshot_time  TIMESTAMPTZ DEFAULT now(),
    updated_at     TIMESTAMPTZ DEFAULT now(),
    status         VARCHAR(10) DEFAULT 'open'
);

-- ── hcm_copy.relationships ────────────────────
CREATE TABLE hcm_copy.relationships (
    relationship_id  SERIAL PRIMARY KEY,
    master_account_id INT NOT NULL REFERENCES hcm_broker.accounts(account_id),
    copy_account_id   INT NOT NULL REFERENCES hcm_broker.accounts(account_id),
    status           VARCHAR(20) DEFAULT 'stopped',
    lot_mode         VARCHAR(20) DEFAULT 'multiplier',
    lot_multiplier   REAL DEFAULT 1.0,
    min_lot          DECIMAL(10,2) DEFAULT 0.01,
    max_lot          DECIMAL(10,2) DEFAULT 5.0,
    max_positions    INT DEFAULT 10,
    max_daily_loss   DECIMAL(10,2) DEFAULT 0,
    max_consecutive_losses INT DEFAULT 3,
    direction_mode   VARCHAR(20) DEFAULT 'FORWARD',
    copy_sl          BOOLEAN DEFAULT true,
    copy_tp          BOOLEAN DEFAULT true,
    only_open_orders BOOLEAN DEFAULT false,
    sync_mode        VARCHAR(20) DEFAULT 'pubsub',
    max_sync_delay_ms INT DEFAULT 500,
    dedup_window_sec INT DEFAULT 300,
    retry_on_failure  BOOLEAN DEFAULT true,
    retry_max         INT DEFAULT 3,
    retry_delay_ms    INT DEFAULT 100,
    total_copied     INT DEFAULT 0,
    today_copied     INT DEFAULT 0,
    last_sync_at     TIMESTAMPTZ,
    last_error       TEXT,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now(),
    created_by       VARCHAR(50)
);

-- ── hcm_copy.symbol_mappings ──────────────────
CREATE TABLE hcm_copy.symbol_mappings (
    mapping_id       SERIAL PRIMARY KEY,
    master_broker    VARCHAR(100) NOT NULL,
    master_symbol    VARCHAR(30) NOT NULL,
    follower_broker  VARCHAR(100),
    follower_symbol  VARCHAR(30) NOT NULL,
    match_mode       VARCHAR(20) DEFAULT 'exact',
    match_priority   INT DEFAULT 0,
    strip_suffixes   TEXT DEFAULT '_',
    case_sensitive   BOOLEAN DEFAULT false,
    is_active        BOOLEAN DEFAULT true,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE(master_broker, master_symbol, follower_broker)
);

-- ── hcm_copy.trade_logs (分区表) ──────────────
CREATE TABLE hcm_copy.trade_logs (
    log_id           BIGSERIAL,
    signal_id        BIGINT REFERENCES hcm_signal.signals(signal_id),
    relationship_id  INT REFERENCES hcm_copy.relationships(relationship_id),
    master_symbol    VARCHAR(30),
    follower_symbol  VARCHAR(30),
    original_lot     DECIMAL(10,2),
    actual_lot       DECIMAL(10,2),
    direction        VARCHAR(10),
    status           VARCHAR(20),
    error_msg        TEXT,
    created_at       TIMESTAMPTZ DEFAULT now()
) PARTITION BY RANGE (created_at);

-- ── Trade logs 初始日分区 ──────────────────────
CREATE TABLE hcm_copy.trade_logs_202607 PARTITION OF hcm_copy.trade_logs
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

-- ============================================================
-- Indexes (集中在最后创建，提升写入性能)
-- ============================================================

-- hcm_system
CREATE INDEX idx_users_role ON hcm_system.users(role_id);
CREATE INDEX idx_auth_logs_user ON hcm_system.auth_logs(user_id, created_at DESC);
CREATE INDEX idx_auth_logs_time ON hcm_system.auth_logs(created_at DESC);

-- hcm_config
CREATE INDEX idx_config_category ON hcm_config.metadata(category, subcategory);
CREATE INDEX idx_config_ui_order ON hcm_config.metadata(category, ui_order);
CREATE INDEX idx_account_overrides_account ON hcm_config.account_overrides(account_id);
CREATE INDEX idx_symbol_meta_category ON hcm_config.symbol_meta(category, is_active);

-- hcm_broker
CREATE INDEX idx_accounts_type ON hcm_broker.accounts(account_type, is_active);
CREATE INDEX idx_trade_rules_account ON hcm_broker.trade_rules(account_id, symbol);

-- hcm_signal
CREATE INDEX idx_signal_tasks_time ON hcm_signal.tasks(created_at DESC);
CREATE INDEX idx_signal_tasks_symbol_tf ON hcm_signal.tasks(symbol, time_frame);
CREATE INDEX idx_signals_time ON hcm_signal.signals(created_at DESC);
CREATE INDEX idx_signals_status ON hcm_signal.signals(signal_status);
CREATE INDEX idx_signals_account ON hcm_signal.signals(account_id, created_at DESC);
CREATE INDEX idx_signals_symbol ON hcm_signal.signals(symbol, created_at DESC);
CREATE INDEX idx_signals_regime ON hcm_signal.signals(regime);

-- hcm_market
CREATE INDEX idx_klines_symbol_tf_time ON hcm_market.klines(symbol, time_frame, open_time DESC);
CREATE INDEX idx_ticks_symbol_time ON hcm_market.ticks(symbol, timestamp DESC);
CREATE INDEX idx_macro_snapshots_category ON hcm_market.macro_snapshots(category, snapshot_time DESC);
CREATE INDEX idx_sentiment_snapshots_category ON hcm_market.sentiment_snapshots(category, snapshot_time DESC);
CREATE INDEX idx_event_calendar_date ON hcm_market.event_calendar(event_date, is_active);
CREATE INDEX idx_event_calendar_category ON hcm_market.event_calendar(category, importance);

-- hcm_trading
CREATE INDEX idx_orders_signal ON hcm_trading.orders(signal_id);
CREATE INDEX idx_orders_account_time ON hcm_trading.orders(account_id, created_at DESC);
CREATE INDEX idx_orders_mt5_ticket ON hcm_trading.orders(mt5_ticket);
CREATE INDEX idx_orders_status ON hcm_trading.orders(order_status);
CREATE INDEX idx_positions_account ON hcm_trading.positions(account_id, symbol);
CREATE INDEX idx_positions_mt5 ON hcm_trading.positions(mt5_ticket);
CREATE INDEX IF NOT EXISTS idx_positions_mt5_status ON hcm_trading.positions(mt5_ticket, status);

-- hcm_copy
CREATE INDEX idx_copy_relationships_master ON hcm_copy.relationships(master_account_id, status);
CREATE INDEX idx_copy_relationships_copy ON hcm_copy.relationships(copy_account_id, status);
CREATE INDEX idx_sym_mapping_follower ON hcm_copy.symbol_mappings(follower_broker, is_active);
CREATE INDEX idx_sym_mapping_master ON hcm_copy.symbol_mappings(master_broker, master_symbol);
CREATE INDEX idx_copy_logs_signal ON hcm_copy.trade_logs(signal_id);
CREATE INDEX idx_copy_logs_time ON hcm_copy.trade_logs(created_at DESC);
CREATE INDEX idx_copy_logs_relationship ON hcm_copy.trade_logs(relationship_id);

-- hcm_risk
CREATE INDEX idx_intercept_logs_signal ON hcm_risk.intercept_logs(signal_id);
CREATE INDEX idx_intercept_logs_symbol ON hcm_risk.intercept_logs(symbol, created_at DESC);
CREATE INDEX idx_intercept_logs_time ON hcm_risk.intercept_logs(created_at DESC);

-- hcm_ai
CREATE INDEX idx_prompt_templates_cat ON hcm_ai.prompt_templates(category, is_active);

-- ============================================================
-- Default Data
-- ============================================================

-- 默认角色
INSERT INTO hcm_system.roles (role_id, role_name, permissions) VALUES
    (1, 'admin', '["*"]'),
    (2, 'trader', '["dashboard.view", "signal.view", "copy.view"]'),
    (3, 'risk_manager', '["dashboard.view", "risk.view", "risk.edit"]'),
    (4, 'operator', '["dashboard.view", "system.view"]'),
    (5, 'researcher', '["dashboard.view", "dashboard.export"]')
ON CONFLICT (role_id) DO NOTHING;

-- 默认管理员用户 (password: admin123 — bcrypt hash)
INSERT INTO hcm_system.users (user_id, username, password_hash, display_name, role_id, is_active) VALUES
    (1, 'admin', '$2b$12$LJ3m4ys3Lk0TSwHCpNqr4OyUwCxGmYFmYxJqAfBVEpZ6KkF0qVj7u', '系统管理员', 1, true)
ON CONFLICT (user_id) DO NOTHING;

-- 默认 prompt 模板
INSERT INTO hcm_ai.prompt_templates (name, category, prompt_content, description, is_active) VALUES
    ('default_v2', 'signal', 
     '你是{category_label}（{symbol}）量化交易专家。{category_traits}\n\n【任务】基于技术指标方向判定 + 预计算的外部环境赋分，输出最终交易信号。\n规则：外部因子赋分已由采集服务预计算，你只需参考，不需要重新评估原始数据。\n\n【品种上下文】\n交易品种={symbol} | 品种类别={category} | 推理周期={tf}\n品种特征={category_traits}\n\n【技术面 — 方向来源】\n技术指标: RSI={rsi} | MACD={macd} | ADX={adx} | Boll={pct_b} | Stoch={stoch}\n多周期: M5={m5_dir} | M15={m15_dir} | H1={h1_dir} | H4={h4_dir}\n预评分方向={pre_dir} 评分={pre_score} | 推理周期={tf}\n时段={session} | AI市况={ai_regime}({ai_strength})\n\n【外部环境 — 预计算赋分（不需重新评估原始数据）】\n宏观({category}): {macro_risk_score}/30 → {macro_bias} | {macro_summary}\n情绪({category}): {sentiment_risk_score}/20 → {sentiment_bias} | {sentiment_summary}\n事件: 风险级别={event_risk_level} | 限制={event_restriction} | 手数缩放={event_lot_scale}\n流动性: 点差={spread} | 深度={depth_level}\n\n【请输出JSON】\n{{\n  "direction": "{final_direction}",\n  "confidence": 0.0-1.0,\n  "environment_note": "{基于预计算赋分的一句话说明环境判断}",\n  "risk_note": "{具体风险}",\n  "suggested_lot_ratio": 0.0-1.0,\n  "sl_atr_multiplier": 1.5,\n  "tp_atr_multiplier": 4.0\n}}',
     '默认信号研判 Prompt v2（精简版）', true)
ON CONFLICT DO NOTHING;

-- ── Self-bootstrap DDL (for containers where the volume already exists) ──

CREATE SCHEMA IF NOT EXISTS hcm_risk;
CREATE TABLE IF NOT EXISTS hcm_risk.intercept_logs (
    log_id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT REFERENCES hcm_signal.signals(signal_id),
    symbol VARCHAR(20) NOT NULL,
    rule_name VARCHAR(100) NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_intercept_logs_signal ON hcm_risk.intercept_logs(signal_id);
CREATE INDEX IF NOT EXISTS idx_intercept_logs_symbol ON hcm_risk.intercept_logs(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intercept_logs_time ON hcm_risk.intercept_logs(created_at DESC);

-- ── Default broker accounts ────────────────────

INSERT INTO hcm_broker.accounts (account_id, account_name, account_number, password_enc, server_name, is_active, broker_name, account_type, env_type, leverage, base_currency) VALUES
(1, '主账户 #1', 100001, 'enc_placeholder', 'ICMarkets-Demo', true, 'ICMarkets', 'master', 1, 500, 'USD'),
(2, '主账户 #2', 100002, 'enc_placeholder', 'ICMarkets-Demo', true, 'ICMarkets', 'master', 1, 500, 'USD'),
(3, '跟单账户 #1', 200001, 'enc_placeholder', 'ICMarkets-Demo', true, 'ICMarkets', 'follower', 1, 500, 'USD'),
(4, '跟单账户 #2', 200002, 'enc_placeholder', 'ICMarkets-Demo', true, 'ICMarkets', 'follower', 1, 500, 'USD'),
(5, '跟单账户 #3', 200003, 'enc_placeholder', 'ICMarkets-Demo', true, 'ICMarkets', 'follower', 1, 100, 'USD')
ON CONFLICT (account_id) DO NOTHING;

-- Reset sequence to avoid duplicate key on subsequent INSERTs
SELECT setval('hcm_broker.accounts_account_id_seq', (SELECT COALESCE(MAX(account_id), 0) FROM hcm_broker.accounts));

COMMIT;
