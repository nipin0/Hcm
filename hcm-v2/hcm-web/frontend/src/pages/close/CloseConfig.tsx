import React, { useEffect, useState } from 'react';
import { Box, Button, Typography } from '@mui/material';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import client from '../../api/client';

const fields: ConfigField[] = [
  // ── Group 1: 平仓策略（位置：左上，灰）──
  { type: 'section', key: '_s1', label: '平仓策略', color: '#94a3b8', description: '仓位退出规则与触发条件' },
  { key: 'close_method', label: '平仓策略', type: 'select', defaultValue: 'auto', options: [
    { label: '信号平仓', value: 'auto' },
    { label: '止损止盈', value: 'sl_tp' },
    { label: '时间平仓', value: 'time' },
    { label: '组合策略', value: 'hybrid' },
  ]},
  // 注：close_on_reverse_signal / close_at_market_close / friday_close_enabled /
  // partial_close_trigger 已移除——后端 close.py 默认值与桥(mt5_bridge)均不消费这些键，
  // 属面板孤儿（保存只会写入 Redis 无人读取的 close.* 孤儿键），故不再展示。
  { key: 'partial_close_enabled', label: '启用分批平仓', type: 'switch', defaultValue: true },
  { key: 'partial_close_ratio', label: '分批平仓比例', type: 'number', defaultValue: 0.5, min: 0.1, max: 1, step: 0.1 },

  // ── Group 2: SL/TP（止损止盈，位置：右上，绿）──
  { type: 'section', key: '_s2', label: 'SL/TP（止损止盈）', color: '#22c55e', description: '开仓时计算止损/止盈位（ATR 倍数制 + zone 锚点偏移）' },
  { key: 'trailing_stop_enabled', label: '启用止损', type: 'switch', defaultValue: true, description: '关闭则不下止损单' },
  { key: 'trailing_stop_distance', label: 'SL 距离 (ATR 倍数)', type: 'number', defaultValue: 2.0, min: 0.5, max: 5, step: 0.1, description: '止损位 = 开仓价 ± ATR × 此倍数' },
  { key: 'max_sl_atr_mult', label: 'SL 上限 (ATR 倍数)', type: 'number', defaultValue: 2.5, min: 0.5, max: 6, step: 0.1, description: 'zone/AI SL 不得宽过此上限，防止止损过宽' },
  { key: 'tp_atr_multiplier', label: 'TP 距离 (ATR 倍数)', type: 'number', defaultValue: 3.0, min: 0, max: 10, step: 0.1, description: 'zone 不可用时回退至此 ATR 倍数算止盈；0=不设止盈' },
  { key: 'tp_min_atr_mult', label: 'Zone TP 下限 (ATR 倍数)', type: 'number', defaultValue: 1.0, min: 0.5, max: 3, step: 0.1, description: 'zone 距离 < 此值则不采用 zone 做 TP（太近没利润空间）' },
  { key: 'tp_max_atr_mult', label: 'Zone TP 上限 (ATR 倍数)', type: 'number', defaultValue: 6.0, min: 3, max: 15, step: 0.5, description: 'zone 距离 > 此值则不采用 zone 做 TP（太远不可达）' },
  { key: 'zone_sl_offset_atr_mult', label: 'Zone SL 后偏移 (ATR)', type: 'number', defaultValue: 0.4, min: 0, max: 2, step: 0.05, description: 'SL 在 zone 边界外侧再退此 ATR，给假突破/wick 留余地' },
  { key: 'zone_tp_offset_atr_mult', label: 'Zone TP 内偏移 (ATR)', type: 'number', defaultValue: 0.3, min: 0, max: 2, step: 0.05, description: 'TP 在 zone 目标内侧收此 ATR，抢在衰竭前落袋' },

  // ── Group 3: 保本止损（左中，蓝）──
  { type: 'section', key: '_s3', label: '保本止损', color: '#3b82f6', description: '盈利后 SL 移至开仓价，确保不亏损' },
  { key: 'break_even_enabled', label: '启用保本止损', type: 'switch', defaultValue: true },
  { key: 'breakeven_atr_mult', label: '保本触发 (ATR 倍数)', type: 'number', defaultValue: 1.0, min: 0.1, max: 3, step: 0.1, description: '盈利 > ATR × 此倍数 → SL 移至开仓价' },
  { key: 'breakeven_buffer_atr_mult', label: '保本缓冲 (ATR 倍数)', type: 'number', defaultValue: 0.15, min: 0.05, max: 1, step: 0.05, description: '保本后 SL = 开仓价 ± 缓冲，覆盖点差/滑点' },

  // ── Group 4: 移动止盈（右中，橙）──
  { type: 'section', key: '_s4', label: '移动止盈', color: '#f59e0b', description: '持仓后 SL 跟随价格移动，锁定利润。trail_start 为启动阈值，trail_wide 为跟踪距离' },
  { key: 'trail_start_atr_mult', label: '移动止盈启动 (ATR 倍数)', type: 'number', defaultValue: 3.0, min: 1.0, max: 8, step: 0.5, description: '盈利 > ATR × 此倍数后才启动移动止盈；此前仅保本保护' },
  { key: 'trail_wide_atr_mult', label: '移动止盈回撤距离 (ATR 倍数)', type: 'number', defaultValue: 1.5, min: 0.1, max: 4, step: 0.05, description: 'SL 始终距当前价 ATR × 此倍数；越小跟得越紧/回撤越小' },

  // ── Group 4.5: 同向尾单止损（2026-09-04，青）──
  { type: 'section', key: '_s45', label: '同向尾单止损', color: '#14b8a6', description: '同向保本后继续加仓场景：最新一笔加仓单（尾单）开仓浮亏超阈值 → 全平该同向组，落袋前面保本单的小利，防「尾单反转大止损 + 前单保本小利」整体转亏。' },
  { key: 'tail_stop_enabled', label: '启用同向尾单止损', type: 'switch', defaultValue: true, description: '同 (品种,方向) 组 ≥2 仓且 ≥1 笔已保本时才生效' },
  { key: 'tail_stop_atr_mult', label: '尾单止损触发 (ATR 倍数)', type: 'number', defaultValue: 0.5, min: 0.1, max: 3, step: 0.05, description: '最新加仓单浮亏 > ATR × 此倍数 → 全平该同向组（保本单小利落袋）' },
  { key: 'tail_stop_cooldown_sec', label: '触发后冷却 (秒)', type: 'number', defaultValue: 60, min: 0, max: 3600, step: 1, description: '全平触发后该组 N 秒内不重复触发（0=关闭冷却）' },

  // ── Group 5: 总仓位移动止盈（左下，紫）──
  { type: 'section', key: '_s5', label: '总仓位移动止盈', color: '#a855f7', description: '所有持仓合计盈利达到阈值后启动整体跟踪，回撤到保护线时全部平仓' },
  { key: 'total_trail_enabled', label: '启用总仓位移动止盈', type: 'switch', defaultValue: false },
  { key: 'total_trail_start_amount', label: '启动阈值 (USD)', type: 'number', defaultValue: 30, min: 5, max: 200, step: 5, description: '总盈利 ≥ 此金额时启动跟踪' },
  { key: 'total_trail_stop_amount', label: '回撤保护线 (USD)', type: 'number', defaultValue: 15, min: 5, max: 100, step: 5, description: '总盈利从峰值回撤至此金额 → 全部平仓锁利' },
  { key: 'total_trail_check_interval', label: '检查间隔 (秒)', type: 'number', defaultValue: 10, min: 5, max: 60, step: 5, description: '每隔此秒数检查一次总仓位盈亏' },

  // ── Group 6: 跟单号每日盈亏熔断（右下，红）──
  { type: 'section', key: '_s6', label: '跟单号每日盈亏熔断', color: '#ef4444', description: '跟单桥专属：当日净盈亏占账户资金（余额）的百分比超盈利/亏损上限即全平跟单号并停止当日跟单，次日 resume_time 自动恢复。主号完全不受影响。' },
  { key: 'follow_circuit_break_enabled', label: '启用跟单熔断', type: 'switch', defaultValue: false, description: '关闭则跟单号不受当日盈亏限制；开启后下列盈亏上限生效' },
  { key: 'follow_daily_profit_max', label: '当日盈利上限 (%)', type: 'number', defaultValue: 0.0, min: 0, max: 100, step: 0.5, description: '当日净盈亏 ≥ 账户资金的此百分比即熔断停跟（如 5 = 5%，0=不限制盈利方向）' },
  { key: 'follow_daily_loss_max', label: '当日亏损上限 (%)', type: 'number', defaultValue: 0.0, min: 0, max: 100, step: 0.5, description: '当日净亏损 ≤ 账户资金的负此百分比即熔断停跟（如 5 = 5%，0=不限制亏损方向）' },
  { key: 'follow_resume_time', label: '每日恢复时间 (HH:MM)', type: 'text', defaultValue: '06:30', description: '本地时区。熔断后到次日此时刻自动恢复跟单运行' },

  // ── Group 7: 执行参数（Execution，右下，青）──
  { type: 'section', key: '_s7', label: '执行参数 (Execution)', color: '#06b6d4', description: '下单执行相关闸门：持仓平仓后冷却，避免刚平仓立即被回调信号重新拉进场。' },
  { key: 'after_close_cooldown_sec', label: '信号冷却 (秒)', type: 'number', defaultValue: 0, min: 0, max: 3600, step: 1, description: '持仓平仓后，该品种 N 秒内不再触发新开仓信号（0=关闭）。覆盖主号与跟单号，仅按交易品种生效。' },
];

// ── 时段感知风险系数面板 (2026-07-24) ────────────────────────────────────────
// 亚盘/欧盘/美盘各自一组 SL/TP/追利参数，桥与信号塔按当前 UTC 小时自动取对应系数。
const SESSION_TABS = [
  { id: 'asia', label: '亚盘', sub: '00:00–08:00 UTC', color: '#38bdf8' },
  { id: 'europe', label: '欧盘', sub: '08:00–13:00 UTC', color: '#a3e635' },
  { id: 'us', label: '美盘', sub: '13:00–22:00 UTC', color: '#fb923c' },
];
const SESSION_SUFFIXES: ConfigField[] = [
  { key: 'trailing_stop_distance', label: 'SL 距离 (ATR)', type: 'number', step: 0.1, defaultValue: 2.0, min: 0.5, max: 5 },
  { key: 'tp_atr_multiplier', label: 'TP 距离 (ATR)', type: 'number', step: 0.1, defaultValue: 2.4, min: 0, max: 10 },
  { key: 'min_rr', label: '最低 R:R', type: 'number', step: 0.05, defaultValue: 1.2, min: 0.5, max: 3 },
  { key: 'breakeven_atr_mult', label: '保本触发 (ATR)', type: 'number', step: 0.05, defaultValue: 0.5, min: 0.1, max: 3 },
  { key: 'breakeven_buffer_atr_mult', label: '保本地板缓冲 (ATR)', type: 'number', step: 0.05, defaultValue: 0.15, min: 0.05, max: 1 },
  { key: 'trail_start_atr_mult', label: '移动止盈启动 (ATR)', type: 'number', step: 0.1, defaultValue: 2.0, min: 1, max: 8 },
  { key: 'trail_wide_atr_mult', label: '移动止盈线宽 (ATR)', type: 'number', step: 0.05, defaultValue: 0.7, min: 0.1, max: 4 },
  { key: 'tp_relay_enabled', label: '移动止盈接力 TP 追利', type: 'switch', defaultValue: true },
  { key: 'tp_trail_wide_atr_mult', label: 'TP 追利缓冲 (ATR)', type: 'number', step: 0.05, defaultValue: 0.7, min: 0.1, max: 4, description: '承接追利时 TP 跟随现价前移的缓冲距离；越小跟得越紧，越大留更多回撤空间' },
  { key: 'trail_start_tp_ratio', label: '移动止盈封顶比例', type: 'number', step: 0.05, defaultValue: 0.8, min: 0.3, max: 1, description: '追踪启动阈值 trail_start 被「固定 TP 距离 × 此比例」封顶（下限 0.3 硬编码不可配）。调高=赢家跑得更远才启动追踪；必须 <1 以保证固定 TP 先于追踪接管。若想让面板上的 trail_start 名义值真正生效，此比例须 ≥ 名义值 ÷ TP距离。' },
];
const sessionFields = (s: string): ConfigField[] =>
  SESSION_SUFFIXES.map((f) => ({ ...f, key: `${s}.${f.key}` }));

const currentSession = (): string => {
  const h = new Date().getUTCHours();
  if (h < 8) return 'asia';
  if (h < 13) return 'europe';
  if (h < 22) return 'us';
  return 'asia';
};

const SessionRiskPanel: React.FC = () => {
  const [tab, setTab] = useState<string>(currentSession());
  const [allValues, setAllValues] = useState<Record<string, string | number | boolean>>({});
  const [loading, setLoading] = useState(false);

  const fetchAll = async (): Promise<Record<string, string | number | boolean>> => {
    const { data: resp } = await client.get('/api/v1/close/config');
    const vals = (resp && (resp.data || resp)) as Record<string, string | number | boolean>;
    setAllValues(vals || {});
    return vals || {};
  };

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    fetchAll().finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fields = sessionFields(tab);
  const initialValues: Record<string, string | number | boolean> = {};
  fields.forEach((f) => {
    initialValues[f.key] = (allValues as Record<string, string | number | boolean>)[f.key] ?? (f.defaultValue as string | number | boolean);
  });

  const handleSubmit = async (vals: Record<string, string | number | boolean>) => {
    await client.put('/api/v1/close/config', vals);
    setAllValues((prev) => ({ ...prev, ...vals }));
    return vals;
  };

  return (
    <Box sx={{ mt: 4, p: 3, border: '1px solid #334155', borderRadius: '12px', backgroundColor: '#0f0f19' }}>
      <Typography variant="h6" className="text-gray-100 mb-1 font-semibold">
        时段系数（亚盘 / 欧盘 / 美盘）
      </Typography>
      <Typography variant="caption" className="text-gray-500 block mb-3">
        按 UTC 小时自动切换生效：亚盘 00–08 / 欧盘 08–13 / 美盘 13–22。开仓 SL/TP 与移动止盈（保本 / 移动止盈 / TP 接力追利）均随当前盘口取值；跨盘持仓时追利参数随当前盘动态生效（带 ● 标记的是当前生效盘口）。
      </Typography>
      <Box className="flex gap-2 mb-3 flex-wrap">
        {SESSION_TABS.map((t) => {
          const active = t.id === tab;
          const isNow = t.id === currentSession();
          return (
            <Button
              key={t.id}
              onClick={() => setTab(t.id)}
              sx={{
                textTransform: 'none',
                borderLeft: `4px solid ${t.color}`,
                backgroundColor: active ? `${t.color}22` : 'transparent',
                color: active ? t.color : '#94a3b8',
                border: `1px solid ${active ? t.color : '#334155'}`,
                borderRadius: '8px',
                px: 2, py: 1,
              }}
            >
              <Box className="flex flex-col items-start">
                <span className="font-semibold">{t.label}{isNow ? ' ●' : ''}</span>
                <span className="text-[10px] opacity-70">{t.sub}</span>
              </Box>
            </Button>
          );
        })}
      </Box>
      <ConfigForm
        key={tab}
        title={`${SESSION_TABS.find((t) => t.id === tab)?.label} 系数`}
        fields={fields}
        initialValues={initialValues}
        onSubmit={handleSubmit}
        loading={loading}
        apiEndpoint="PUT /api/v1/close/config"
        fetchValues={fetchAll}
      />
    </Box>
  );
};

const CloseConfig: React.FC = () => {
  const [initialValues, setInitialValues] = useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);

  useEffect(() => {
    client.get('/api/v1/close/config')
      .then(({ data: resp }) => setInitialValues(resp.data || resp))
      .catch(() => {});
  }, []);

  const handleSubmit = async (values: Record<string, string | number | boolean>): Promise<void> => {
    await client.put('/api/v1/close/config', values);
    const { data: resp } = await client.get('/api/v1/close/config');
    setInitialValues(resp.data || resp);
    setFormKey(k => k + 1);
  };

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
      <ConfigForm
        formKey={formKey}
        title="平仓配置"
        fields={fields}
        initialValues={initialValues}
        onSubmit={handleSubmit}
        apiEndpoint="PUT /api/close/config"
        grouped={true}
        groupColumns={2}
      />
      <SessionRiskPanel />
    </Box>
  );
};

export default CloseConfig;
