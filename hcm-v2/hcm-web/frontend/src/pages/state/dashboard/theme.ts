/**
 * FSM（LightGBM 行情状态机）看板 —— 色板 / 状态色 / 格式化 / 类型
 * =====================================================================
 * 挂载页：src/pages/state/FsmDashboard.tsx  →  路由 /dashboard/fsm
 *
 * 【设计约定】本文件是唯一色板来源，页面与子组件一律从这里取色，
 * 不新造主题系统（与 src/pages/signaltower/AiOpsConsole.tsx 的做法一致）。
 */

/**
 * 页面色板。切换只改这一行：
 *   'project'  —— 现网看板令牌（底 #0d0d14 / 卡片 #111118 / 文字 #e2e8f0），
 *                 与「数据看板」其它 5 个页面视觉一致（**当前采用**）
 *   'tailwind' —— 需求方给出的 Tailwind gray 系（底 #111827 / 卡片 #1f2937 / 文字 #f3f4f6）
 */
const PALETTE: 'project' | 'tailwind' = 'project';

const PALETTES = {
  project: {
    bg: '#0d0d14', card: '#111118', inner: '#161622', border: '#2a2a3a', divider: '#1e293b',
    text: '#e2e8f0', sub: '#94a3b8', weak: '#64748b',
    ok: '#22c55e', warn: '#eab308', block: '#ef4444', idle: '#94a3b8', info: '#3b82f6',
    accent: '#a855f7',
  },
  tailwind: {
    bg: '#111827', card: '#1f2937', inner: '#1f2937', border: '#374151', divider: '#374151',
    text: '#f3f4f6', sub: '#9ca3af', weak: '#6b7280',
    ok: '#22c55e', warn: '#eab308', block: '#ef4444', idle: '#9ca3af', info: '#3b82f6',
    accent: '#a855f7',
  },
} as const;

export const C = PALETTES[PALETTE];

/**
 * FSM 状态色。
 * S1–S4 为需求方指定值；**S0 / S5 / S9 为补齐**（状态机实为 7 态，需求只给了 4 态，
 * 见 state_machine.py:71-79：S6/S7/S8 保留未用）。
 */
export const STATE_COLOR: Record<string, string> = {
  S0_IDLE: '#64748b',        // 空闲（补齐）
  S1_OSC: '#999999',         // 震荡（指定）
  S2_TREND_INIT: '#63a8ff',  // 趋势初生（指定）
  S3_TREND_MID: '#2468d1',   // 趋势中段（指定）
  S4_TREND_FADE: '#ff9845',  // 趋势衰竭（指定）
  S5_OSC_LOCKED: '#a78bfa',  // 震荡锁止（补齐）
  S9_PAUSED: '#475569',      // 暂停（补齐）
};

export const STATE_CN: Record<string, string> = {
  S0_IDLE: '空闲', S1_OSC: '震荡', S2_TREND_INIT: '趋势初生', S3_TREND_MID: '趋势中段',
  S4_TREND_FADE: '趋势衰竭', S5_OSC_LOCKED: '震荡锁止', S9_PAUSED: '暂停',
};

/** 4 类模型输出 → S1..S4 的对应关系（state_machine.py:83-88 CLASS_TO_STATE） */
export const CLASS_KEYS = ['oscillation', 'trend_init', 'trend_mid', 'trend_fade'] as const;
export type ClassKey = typeof CLASS_KEYS[number];

export const CLASS_CN: Record<string, string> = {
  oscillation: '震荡', trend_init: '趋势初生', trend_mid: '趋势中段', trend_fade: '趋势衰竭',
};

/** 概率曲线色：与状态色同源，但 trend_mid 提亮（#2468d1 在深底上做细线不可辨） */
export const CLASS_LINE_COLOR: Record<ClassKey, string> = {
  oscillation: '#999999',
  trend_init: '#63a8ff',
  trend_mid: '#4d8ef7',
  trend_fade: '#ff9845',
};

/** 箱体线（需求方指定） */
export const BOX_COLOR = '#4ade80';

/**
 * 涨跌 / 多空配色。
 *   'cn'   = 国内习惯：涨红跌绿、多红空绿（**当前采用**）
 *   'intl' = 国际习惯：涨绿跌红、多绿空红
 *
 * ⚠️ 需求方原始规范给的是「多单 #22c55e(绿) / 空单 #ef4444(红)」= 'intl'。
 * 之所以默认取 'cn'：同一张 K 线图里若蜡烛是"涨红跌绿"、而多单标记是绿，
 * 会出现"绿蜡烛配绿多单"的语义打架。改回原规范只需把这里改成 'intl'。
 */
export const COLOR_CONVENTION: 'cn' | 'intl' = 'cn';

export const UD = COLOR_CONVENTION === 'cn'
  ? { up: '#ef4444', down: '#22c55e', long: '#ef4444', short: '#22c55e' }
  : { up: '#22c55e', down: '#ef4444', long: '#22c55e', short: '#ef4444' };

// ── 格式化 ──────────────────────────────────────────────────────────────────
export const fmt = (n: any, digits = 2): string => {
  const v = typeof n === 'number' ? n : Number(n);
  return Number.isFinite(v) ? v.toFixed(digits) : '—';
};

export const fmtSigned = (n: any, digits = 2): string => {
  const v = typeof n === 'number' ? n : Number(n);
  if (!Number.isFinite(v)) return '—';
  return (v > 0 ? '+' : '') + v.toFixed(digits);
};

/** ISO 字符串 → 'MM-DD HH:MM' */
export const tsShort = (s?: string | null): string =>
  s ? String(s).slice(5, 16).replace('T', ' ') : '—';

/** ISO 字符串 → 'HH:MM' */
export const tsClock = (s?: string | null): string =>
  s ? String(s).slice(11, 16) : '—';

// ── 类型（对齐 web/api/state.py 的响应结构）────────────────────────────────
export interface Derived {
  state: string | null;
  state_cn: string | null;
  is_osc: boolean;
  box_height: number | null;
  box_frozen: boolean | null;
  box_frozen_at: string | null;
  freeze_rule_misaligned: boolean;
  trend_detail_published: boolean;
  trend_detail_reason: string;
}

export interface CfgItem { key: string; value: number; default: number }

export interface LiveResp {
  symbol: string;
  ts: string;
  fsm: any | null;
  live: any | null;
  ctx: any | null;
  directive: any | null;
  derived: Derived;
  config: Record<string, CfgItem>;
}

export interface KlineBar {
  open_time: string;
  open: number; high: number; low: number; close: number;
  tick_volume?: number;
  state: string | null;
  prev_state?: string | null;
  transitioned: boolean | null;
  predicted_class: string | null;
  prob_oscillation: number | null;
  prob_trend_init: number | null;
  prob_trend_mid: number | null;
  prob_trend_fade: number | null;
  margin: number | null;
  age_bars: number | null;
  direction: string | null;
  hold_only?: boolean | null;
  model_version?: string | null;
  intent_action?: string | null;
  intent_direction?: string | null;
  intent_lot_mult?: number | null;
  intent_reason?: string | null;
  trigger_on?: boolean | null;
  trigger_reason?: string | null;
  note?: string | null;
}

export interface RiskResp {
  symbol: string;
  positions: any[];
  positions_open: number;
  float_profit_sum: number;
  day_loss: number | null;
  day_loss_orders: number;
  day_loss_symbol: number | null;
  day_loss_cap: number;
  day_loss_source: string;
  /** 近 N 笔已平仓单（含 open_time / close_time），供 K 线开平仓标记 */
  recent_orders?: any[];
  orders_source?: string;
}

export interface LogRow { [k: string]: any }

/** 后端信封 */
export interface Envelope<T> { code: number | string; data: T | null; message: string }
