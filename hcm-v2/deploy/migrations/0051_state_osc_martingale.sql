-- 0051_state_osc_martingale.sql
-- 【2026-09-18】震荡态「马丁补仓」开关 seed + 元数据校正（幂等，可重复执行）
--
-- 背景（用户需求原话）："LightGBM FSM 震荡态的阶梯手数，是 0.5 止损后**及时**下 1.0、
--   不是等行情变化后再接着下阶梯手数；逻辑有缺陷，急需优化"。
--
-- 缺陷（2026-09-18 复盘，数据实证）：SL 后箱体被**解冻 + 滚动箱重建** ⇒ 重入场必须等
--   价格回到【新箱体】边缘（`osc_outside_box`/`osc_inside_box`），中间可空很久
--   （实测 09:41 的 0.5 止损 → 10:45 才补 1.0，空 64 分钟）。
--
-- 修复（`state_strategy.py`）：首单路径**不变**（仍由箱体边缘触发）；一旦本轮因**止损**
--   结束且已平仓 ⇒ 下一根 bar 直接用**本轮入场方向**市价补下一档
--   （`state.osc_lot_ladder[连续止损次数]`），不再等箱体重建、不再等 FSM 状态。
--   安全边界**全部保留**：① 已平仓才补；② `state.osc_atr_loss_limit`(4ATR) 为硬刹车；
--   ③ 档位由 `state.osc_lot_ladder` 封顶；④ 下游风控（`risk.max_lot_per_trade` /
--   `risk.max_concurrent_signals` / 同向保本闸门 / 置信度闸门）照常生效。
--
-- 语义：
--   state.osc_martingale_enabled = false → **退回旧行为**（SL 后不主动补仓，
--   仍等箱体边缘重入）。秒级热生效，无需重启/改码。
--
-- 注：本次由 `tools/_scratch/set_cfg.py`（ConfigProviderV3 唯一写入口）先行写入，
--   该行初建的 category/value_type 为占位值（scoring/string）——本迁移把元数据
--   校正为语义正确的 state/bool，**不覆盖 current_value**（尊重运维现值）。

INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('state.osc_martingale_enabled', 'state', 'true', 'true', 'bool',
     '震荡马丁补仓开关',
     '震荡态止损后同向补下一档（真实马丁，不等箱体重建/状态）；关闭则退回"等箱体边缘重入"旧行为')
ON CONFLICT (config_key) DO UPDATE SET
    category    = EXCLUDED.category,
    value_type  = EXCLUDED.value_type,
    label       = EXCLUDED.label,
    description = EXCLUDED.description,
    updated_at  = now();
