// 信号漏斗「拦截/触发原因」分类映射 —— 前端漏斗明细（SignalFunnel）直接消费。
//
// 数据来源：后端 signal_tower.py 的 map_funnel_reason() 把引擎写入的英文
// fallback_reason 翻译成中文卡点名（reason_cn）后，前端用本模块按中文子串
// 归到 8+1 个语义类别（接刀防护 / 动量逆势 / 评分类 / 结构 / 冷却 / 风控硬拦 /
// 数据引擎 / 策略放弃 / 触发成单），用于配色与分组。
//
// classifyReason() 是纯函数、零依赖，可直接在组件里调用。

export type ReasonCategoryKey =
  | 'trigger'      // 触发成单（已下单）
  | 'strategic'    // 策略主动放弃
  | 'extreme'      // 极值/接刀防护
  | 'momentum'     // 动量/逆势裁决
  | 'score'        // 评分类
  | 'structure'    // 结构/位置
  | 'cooldown'     // 冷却节流
  | 'risk'         // 风控硬拦
  | 'engine'       // 数据/引擎/质量异常
  | 'neutral';     // 无卡点

export interface ReasonCategory {
  key: ReasonCategoryKey;
  label: string;
  color: string;
}

export const REASON_CATEGORIES: Record<ReasonCategoryKey, ReasonCategory> = {
  trigger:   { key: 'trigger',   label: '触发成单',   color: '#22c55e' }, // 绿：已下单
  strategic: { key: 'strategic', label: '策略主动放弃', color: '#38bdf8' }, // 天蓝
  extreme:   { key: 'extreme',   label: '极值/接刀防护', color: '#f59e0b' }, // 琥珀
  momentum:  { key: 'momentum',  label: '动量/逆势裁决', color: '#fb7185' }, // 玫红
  score:     { key: 'score',     label: '评分类',      color: '#eab308' }, // 黄
  structure: { key: 'structure', label: '结构/位置',    color: '#22d3ee' }, // 青
  cooldown:  { key: 'cooldown',  label: '冷却节流',    color: '#a78bfa' }, // 紫
  risk:      { key: 'risk',      label: '风控硬拦',    color: '#ef4444' }, // 红
  engine:    { key: 'engine',    label: '数据/引擎异常', color: '#f97316' }, // 深橙
  neutral:   { key: 'neutral',   label: '无卡点',      color: '#64748b' }, // 灰
};

/**
 * 把后端 map_funnel_reason() 返回的「中文卡点名」归到语义类别。
 * @param reasonCn 信号明细里的 reason_cn（可能为 null/空/"未标记"）
 */
export function classifyReason(reasonCn?: string | null): ReasonCategory {
  const cn = (reasonCn || '').trim();
  if (!cn || cn === '未标记' || cn.startsWith('无（')) return REASON_CATEGORIES.neutral;

  // 1) 触发成单（绿）优先
  if (cn.includes('趋势抢跑成单')) return REASON_CATEGORIES.trigger;

  // 2) 风控硬拦（红）
  if (cn.includes('最大订单') || cn.includes('亏损熔断') ||
      cn.includes('置信度') || cn.includes('点差过大')) return REASON_CATEGORIES.risk;

  // 3) 冷却节流（紫）
  if (cn.includes('冷却') || cn.includes('保本闸门')) return REASON_CATEGORIES.cooldown;

  // 4) 极值/接刀防护（琥珀）—— 须在动量之前，避免「动量枯竭」被动量类误吞
  if (cn.includes('极值') || cn.includes('动量枯竭') ||
      cn.includes('接刀') || cn.includes('摸顶') || cn.includes('Hurst')) return REASON_CATEGORIES.extreme;

  // 5) 动量/逆势裁决（玫红）
  if (cn.includes('动量') || cn.includes('逆势')) return REASON_CATEGORIES.momentum;

  // 6) 评分类（黄）
  if (cn.includes('评分') || cn.includes('耦合') || cn.includes('校准') ||
      cn.includes('门槛') || cn.includes('等级')) return REASON_CATEGORIES.score;

  // 7) 结构/位置（青）
  if (cn.includes('结构') || cn.includes('突破') || cn.includes('震荡市拦截')) return REASON_CATEGORIES.structure;

  // 8) 数据/引擎/质量异常（深橙）
  if (cn.includes('未就绪') || cn.includes('已关闭') || cn.includes('地板') ||
      cn.includes('过滤') || /F[1-6]/.test(cn)) return REASON_CATEGORIES.engine;

  // 9) 策略主动放弃（天蓝）
  if (cn.includes('无明确方向') || cn.includes('策略放弃') ||
      cn.includes('不做均值回归') || cn.includes('回撤刹车') ||
      cn.includes('试探单已用') || cn.includes('震荡市拦截')) return REASON_CATEGORIES.strategic;

  return REASON_CATEGORIES.neutral;
}

// ──────────────────────────────────────────────────────────────
// 信号「触发下单」原型图例 —— 漏斗明细可用来解释「这条信号属于哪种下单逻辑」。
// 每条记录并非都存了原型字段，故作为图例常量供 UI 解释用。
// ──────────────────────────────────────────────────────────────
export interface TriggerArchetype {
  id: string;
  label: string;
  trigger: string;   // 触发条件
  gates: string;     // 必须通过的护栏
  codeRef: string;   // 代码位置
}

export const TRIGGER_ARCHETYPES: TriggerArchetype[] = [
  {
    id: 'trend_follow',
    label: '趋势顺势单',
    trigger: '多周期共振（M30/H1/H4/D1 同向）+ M5 七因子 dir_sum 同向 + ADX≥下限 + 微动量顺势（mm_aligned>0）。',
    gates: 'grade≥min_grade、momentum_flip 不否决、未触发极值/周期位置护栏。',
    codeRef: 'hexp_engine.py produce() 七因子 dir_sum；micro_state TREND_*',
  },
  {
    id: 'trend_start',
    label: '趋势抢跑（顺势突破）',
    trigger: 'BBW 带宽压缩至 120 根最低分位 + 收盘突破 Donchian 轨 + 近完成 H1 棒与 H1 EMA 共振（squeeze_breakout）。',
    gates: 'hexp.trend_start_order_enabled=true 时轻仓顺势突破单，且位于全部五道护栏之后（任一护栏拦过则不放行）。',
    codeRef: 'hexp_engine.py trend_start_candidate；scheduler 主生产路径',
  },
  {
    id: 'mean_reversion',
    label: '均值回归',
    trigger: 'RANGE/NEUTRAL 体制下 RSI 极值（>68 做空 / <35 做多）或 %b 贴边 + 随机极值，价格回归布林中轨。',
    gates: 'scoring RANGE 闸门（rsi_extreme 即放行，豁免 %b 贴边）；neutral_rsi_confirmed 武装。',
    codeRef: 'scoring_engine.py RANGE gate；micro_state RANGE；EXHAUST 探针',
  },
  {
    id: 'extreme_fade',
    label: '极值反转（逆势衰减）',
    trigger: 'Donchian 分位极值（top>0.85 / bottom<0.15）+ 微动量回撤（mm_aligned<mm_retreat_min）+ 影线反转 → 系统下反向（fade）单。',
    gates: '七因子方向裁决逆向前置；注意 hexp_extreme_reversal/hexp_extreme_guard 是「拦追单」护栏，fade 单本身由方向裁决产生。',
    codeRef: 'hexp_engine.py 极值块（hexp_extreme_reversal）；HEXP_KEYS mm_retreat_min',
  },
  {
    id: 'exhaust_probe',
    label: '衰竭逆势探针',
    trigger: 'EXHAUST 态后首根逆原趋势反转 K + 结构未破 → 轻仓反向试探（左侧埋伏），每个衰竭周期仅一笔。',
    gates: 'co.v2.exhaust_probe_enabled=true；co_exec_lot_mult≈0.4 轻仓；H1 确认逆势仍硬拦。',
    codeRef: 'co_source.py apply_v2 EXHAUST 探针；scheduler exhaust_probe 一次性闸门',
  },
  {
    id: 'pullback',
    label: '回踩支撑/阻力',
    trigger: '顺势趋势中回踩支撑（BUY）/阻力（SELL）入场，由 zone 结构位（S/R/枢轴/整数关口）锚定触达。',
    gates: 'zone 结构位方向对齐；entry_trigger_wait 等触达；不在结构位贴脸逆向下单。',
    codeRef: 'scheduler.py _resolve_entry_zone_for_direction；HEXP 回踩支撑诊断',
  },
];
