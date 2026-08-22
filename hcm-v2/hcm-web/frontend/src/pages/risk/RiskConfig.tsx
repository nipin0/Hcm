import React, { useEffect, useState } from 'react';
import { Box, Typography } from '@mui/material';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';

const fields: ConfigField[] = [
  {
    key: 'max_daily_loss',
    label: '日内最大亏损 ($)',
    type: 'number',
    defaultValue: 5000,
    min: 0,
    max: 1000000,
    description: '单日累计亏损超过此值时停止开新仓；含已实现 + 未实现亏损。单位 USD。',
  },
  {
    key: 'max_drawdown_percent',
    label: '最大回撤 (%)',
    type: 'number',
    defaultValue: 20,
    min: 0.1,
    max: 100,
    description: '相对账户峰值权益的最大回撤比例；超过则触发减仓或暂停。建议 10-30%。',
  },
  {
    key: 'max_leverage',
    label: '最大杠杆',
    type: 'number',
    defaultValue: 100,
    min: 1,
    max: 500,
    description: '允许的最大账户杠杆倍数；超过此值的开仓请求将被拒。',
  },
  {
    key: 'max_total_exposure',
    label: '最大总敞口 ($)',
    type: 'number',
    defaultValue: 100000,
    min: 0,
    max: 10000000,
    description: '所有持仓合计名义价值上限（按入场价计算）。',
  },
  {
    key: 'max_total_lot',
    label: '最大总仓位 (手)',
    type: 'number',
    defaultValue: 1.0,
    min: 0.01,
    max: 100,
    step: 0.01,
    description: '所有持仓合计最大手数；超过时新信号进入队列。注：前端保存到 risk.max_total_lot，RuleChain 实际读此 key。',
  },
  {
    key: 'max_correlation',
    label: '最大品种相关性',
    type: 'number',
    defaultValue: 0.8,
    min: 0,
    max: 1,
    step: 0.05,
    description: '允许同时持仓的相关性上限；0.8 表示两品种相关系数超过 0.8 时禁止同向持仓。',
  },
  {
    key: 'stop_out_level',
    label: '强制平仓水平 (%)',
    type: 'number',
    defaultValue: 50,
    min: 0,
    max: 100,
    description: '账户保证金水平低于此值时由券商/系统强制平仓。MT5 常见值 20-50。',
  },
  {
    key: 'margin_call_level',
    label: '追保通知水平 (%)',
    type: 'number',
    defaultValue: 100,
    min: 0,
    max: 200,
    description: '保证金水平低于此值时触发追加保证金通知。',
  },
  {
    key: 'max_concurrent_signals',
    label: '最大持仓数',
    type: 'number',
    defaultValue: 3,
    min: 1,
    max: 20,
    description: '同一时间允许的最大持仓数量；超过时新信号进入队列等待平仓释放名额。',
  },
  {
    key: 'cooldown_minutes',
    label: '同向开仓冷却 (分钟)',
    type: 'number',
    defaultValue: 15,
    min: 0,
    max: 1440,
    description: '同一品种同一方向两次开仓之间的最小间隔；防止震荡市反复开仓。',
  },
  {
    key: 'max_lot_per_trade',
    label: '单笔最大手数',
    type: 'number',
    defaultValue: 0.5,
    min: 0.01,
    max: 100,
    step: 0.01,
    description: '单笔订单允许的最大手数；超过此值的订单将被自动调整或拒单。',
  },
  {
    key: 'news_filter_enabled',
    label: '新闻过滤',
    type: 'switch',
    defaultValue: true,
    description: '启用后，高影响新闻（NFP/CPI 等）发布前后 30 分钟内不开启新仓。',
  },
  {
    key: 'kill_switch',
    label: '紧急停止开关',
    type: 'switch',
    defaultValue: false,
    description: '立即停止所有交易行为（开仓 + 平仓 + 信号生成）。紧急情况下使用。',
  },
  {
    key: 'kill_switch_action',
    label: '紧急停止操作',
    type: 'select',
    defaultValue: 'close_all',
    options: [
      { label: '平仓所有', value: 'close_all' },
      { label: '暂停交易', value: 'pause' },
      { label: '仅通知', value: 'notify_only' },
    ],
    description: '紧急停止触发时的具体动作。',
  },
  {
    key: 'risk_check_interval',
    label: '风控检查间隔 (秒)',
    type: 'number',
    defaultValue: 1,
    min: 1,
    max: 60,
    description: '风控引擎扫描持仓/账户状态的周期；建议 1-5 秒。',
  },
  // ── Dynamic lot sizing tiers (config-driven, no hardcode) ──
  {
    key: 'lot_base',
    label: '基础手数',
    type: 'number',
    defaultValue: 0.02,
    min: 0.001,
    max: 100,
    step: 0.01,
    description: '每笔订单的基础手数（默认 0.02）。实际下单手数 = 基础手数 × 评分倍率。XAUUSD 由 symbol.XAUUSD.tower.lot_size 覆盖为 0.02。无亚/欧/美盘会话分流。',
  },
  {
    key: 'score_tier_low',
    label: '低评分阈值 (score ≥)',
    type: 'number',
    defaultValue: 0.65,
    min: 0,
    max: 1,
    step: 0.05,
    description: '低挡下界：score < 0.65 时用低倍率(×0.5)。低于此值仍归低挡（不下单门槛由 min_confidence 控制）。',
  },
  {
    key: 'lot_multiplier_low',
    label: '低评分倍率',
    type: 'number',
    defaultValue: 0.5,
    min: 0,
    max: 5,
    step: 0.1,
    description: 'score≥0.50 时, lot = 基础手数 × 此倍率',
  },
  {
    key: 'score_tier_mid',
    label: '中评分阈值 (score ≥)',
    type: 'number',
    defaultValue: 0.65,
    min: 0,
    max: 1,
    step: 0.05,
    description: '中评分阈值。score≥此值时用中倍率',
  },
  {
    key: 'lot_multiplier_mid',
    label: '中评分倍率',
    type: 'number',
    defaultValue: 1.0,
    min: 0,
    max: 5,
    step: 0.1,
    description: 'score≥0.65 时, lot = 基础手数 × 此倍率',
  },
  {
    key: 'score_tier_high',
    label: '高评分阈值 (score ≥)',
    type: 'number',
    defaultValue: 0.95,
    min: 0,
    max: 1,
    step: 0.05,
    description: '高评分阈值。score≥0.95 时用高倍率(×1.5)。',
  },
  {
    key: 'lot_multiplier_high',
    label: '高评分倍率',
    type: 'number',
    defaultValue: 1.5,
    min: 0,
    max: 10,
    step: 0.1,
    description: 'score≥0.95 时, lot = 基础手数 × 1.5',
  },
  // ── 分值驱动最大持仓数（2026-08-11 v2.5）──
  { type: 'section', key: '_score_pos_section', label: '分值-持仓数链动', color: '#8b5cf6',
    description: '根据信号分值动态调整最大持仓数：高分值信号允许更多持仓（自信放大），低分值保守限制（控制风险）。关闭则使用全局固定最大持仓数。' },
  {
    key: 'score_driven_positions_enabled',
    label: '启用心智驱动',
    type: 'switch',
    defaultValue: false,
    description: '开启后，最大持仓数不再使用固定值，而是根据信号分值动态选择低/中/高三档。关闭=使用 max_concurrent_signals 固定值。',
  },
  {
    key: 'score_tier_low_positions',
    label: '低分值门槛 (score ≥)',
    type: 'number',
    defaultValue: 0.50,
    min: 0,
    max: 1,
    step: 0.05,
    description: '分值低于此门槛时使用"低分值最大持仓数"（保守）。建议 0.50。',
  },
  {
    key: 'score_tier_mid_positions',
    label: '中分值门槛 (score ≥)',
    type: 'number',
    defaultValue: 0.70,
    min: 0,
    max: 1,
    step: 0.05,
    description: '分值≥此门槛时使用"高分值最大持仓数"（激进）；在低-中之间使用"中分值最大持仓数"。建议 0.70。',
  },
  {
    key: 'max_positions_score_low',
    label: '低分值最大持仓数',
    type: 'number',
    defaultValue: 1,
    min: 0,
    max: 20,
    description: 'score < score_tier_low_positions 时的最大持仓数。低分信号保守，建议 1（仅限极品单）。',
  },
  {
    key: 'max_positions_score_mid',
    label: '中分值最大持仓数',
    type: 'number',
    defaultValue: 2,
    min: 0,
    max: 20,
    description: '低分 ≤ score < 中分 时的最大持仓数。普通信号，建议 2。',
  },
  {
    key: 'max_positions_score_high',
    label: '高分值最大持仓数',
    type: 'number',
    defaultValue: 3,
    min: 0,
    max: 20,
    description: 'score ≥ score_tier_mid_positions 时的最大持仓数。高分信号自信放大，建议 3。',
  },
];

const RiskConfig: React.FC = () => {
  const [initialValues, setInitialValues] = useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);
  const [symbolLimits, setSymbolLimits] = useState<Record<string, { max_positions: number; cooldown_seconds: number; max_daily_trades?: number }>>({});

  useEffect(() => {
    client.get(ENDPOINTS.risk.config)
      .then(({ data: resp }) => {
        const d = resp.data || resp;
        const cfg = d.global_config || {};
        const parsed: Record<string, string | number | boolean> = {};
        // Build type map for declared field types
        const typeMap = new Map<string, string>();
        for (const f of fields) typeMap.set(f.key, f.type);
        for (const [k, v] of Object.entries(cfg)) {
          if (v === '' || v === null || v === undefined) {
            const f = fields.find((f) => f.key === k);
            if (f && f.defaultValue !== undefined) parsed[k] = f.defaultValue;
            continue;
          }
          const declaredType = typeMap.get(k);
          if (declaredType === 'text' || declaredType === 'select' || declaredType === 'textarea') {
            // Preserve text fields as strings
            parsed[k] = String(v);
          } else if (declaredType === 'switch') {
            parsed[k] = String(v) === 'true' || v === true || v === '1';
          } else if (k === 'kill_switch_action') {
            // Legacy string field
            parsed[k] = String(v);
          } else {
            // Number type
            const num = parseFloat(String(v));
            parsed[k] = isNaN(num) ? String(v) : num;
          }
        }
        setInitialValues(parsed);
      })
      .catch(() => {});

    client.get(ENDPOINTS.risk.symbolLimits)
      .then(({ data: resp }) => {
        const items = (resp.data || resp).items || [];
        const map: Record<string, any> = {};
        for (const item of items) {
          map[item.symbol] = item;
        }
        setSymbolLimits(map);
      })
      .catch(() => {});
  }, []);

  const handleSubmit = async (values: Record<string, string | number | boolean>): Promise<void> => {
    const updates = Object.entries(values).map(([k, v]: [string, string | number | boolean]) => ({
      config_key: k,
      value: String(v),
    }));
    await client.put(ENDPOINTS.risk.config, { updates });
    // Re-fetch before formKey remount so initialValues are fresh
    const { data: resp } = await client.get(ENDPOINTS.risk.config);
    const d = resp.data || resp;
    const cfg = d.global_config || {};
    const parsed: Record<string, string | number | boolean> = {};
    for (const [k, v] of Object.entries(cfg)) {
      if (v === '' || v === null || v === undefined) {
        const f = fields.find((f) => f.key === k);
        if (f && f.defaultValue !== undefined) parsed[k] = f.defaultValue;
      } else if (k === 'kill_switch' || k === 'news_filter_enabled') {
        parsed[k] = v === 'true' || v === true || v === '1';
      } else if (k === 'kill_switch_action') {
        parsed[k] = String(v);
      } else {
        const num = parseFloat(String(v));
        parsed[k] = isNaN(num) ? String(v) : num;
      }
    }
    setInitialValues(parsed);
    setFormKey(k => k + 1);
  };

  return (
    <Box>
      <ConfigForm
        formKey={formKey}
        title="风控配置"
        fields={fields}
        initialValues={initialValues}
        onSubmit={handleSubmit}
        apiEndpoint={`PUT ${ENDPOINTS.risk.config}`}
      />

      <Box className="mt-8">
        <Typography variant="h6" className="text-gray-100 mb-4 font-semibold">
          品种级风控（{Object.keys(symbolLimits).length} 个品种）
        </Typography>
        {Object.keys(symbolLimits).length === 0 ? (
          <Typography className="text-gray-500 text-center py-8">暂无品种级风控配置</Typography>
        ) : (
          <Box className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {Object.entries(symbolLimits).map(([symbol, cfg]) => (
              <Box
                key={symbol}
                className="bg-gray-900 border border-gray-700 rounded-xl p-4"
                sx={{ borderLeft: '3px solid #3b82f6' }}
              >
                <Typography className="text-blue-400 font-mono font-semibold mb-3 text-lg">
                  {symbol}
                </Typography>
                <Box className="space-y-2 text-sm">
                  <Box className="flex justify-between">
                    <Typography variant="caption" className="text-gray-500">最大持仓</Typography>
                    <Typography variant="caption" className="text-gray-200">{cfg.max_positions}</Typography>
                  </Box>
                  <Box className="flex justify-between">
                    <Typography variant="caption" className="text-gray-500">同向冷却 (秒)</Typography>
                    <Typography variant="caption" className="text-gray-200">
                      {cfg.cooldown_seconds > 0 ? `${cfg.cooldown_seconds}s (${(cfg.cooldown_seconds / 60).toFixed(1)}min)` : '未设置'}
                    </Typography>
                  </Box>
                  <Box className="flex justify-between">
                    <Typography variant="caption" className="text-gray-500">日交易上限</Typography>
                    <Typography variant="caption" className="text-gray-200">
                      {cfg.max_daily_trades ?? '—'}
                    </Typography>
                  </Box>
                </Box>
              </Box>
            ))}
          </Box>
        )}
      </Box>
    </Box>
  );
};

export default RiskConfig;
