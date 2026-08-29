/** 和乘幂（Hexp）信号看板 —— 类型契约、配色系统、推断逻辑与格式化工具。
 *
 * 数据契约严格对齐信号塔引擎 `hexp_engine.py` 发布到 Redis
 * `hcm:live:hexp:{symbol}` 的实时快照（TTL 15s，约 3s 重算一次），
 * 经 `GET /api/v1/hexp/signal/{symbol}` 透出。
 *
 * **零硬编码原则**：所有阈值（分级门槛、手数系数、SL/TP 倍数、mm 归一尺度）
 * 一律从 `GET /api/v1/hexp/config` 读取，本文件只提供「配置缺失时的兜底常量」，
 * 且兜底值与后端 `HEXP_KEYS` 默认值一一对齐。
 */

// ── 引擎快照契约 ──────────────────────────────────────────────

/** 6 维评分卡（引擎 scorecard 字段），每维 0~100 */
export interface HexpScorecard {
  resonance: number;
  state: number;
  entry: number;
  position: number;
  vol: number;
  session: number;
}

/** 7 因子归一得分（引擎 factor_scores 字段），范围约 -1~+1 */
export interface HexpFactorScores {
  adx: number;
  er: number;
  ma: number;
  bbw: number;
  hurst: number;
  rsi: number;
  mm: number;
}

/**
 * 7 因子原始指标值（引擎 factor_raws 字段，2026-08-12 新增）。
 * 与 factor_scores（归一方向分 -1~+1）不同，这是「指标本身」的实时读数：
 *  - adx   → ADX(14) 原始值（0~100，如 21.3）
 *  - er    → 效率比 Efficiency Ratio（0~1，如 0.352）
 *  - ma    → MA 多头度（0~100，50 为中性，由三腿排列+EMA20 斜率合成）
 *  - bbw   → 布林带宽分位数（0~100%，50 为历史中位）
 *  - hurst → Hurst 指数（0~1，>0.5 趋势持续 / <0.5 均值回归）
 *  - rsi   → RSI(14) 原始值（0~100，如 68.9）
 *  - mm    → 微结构动量（-1~+1，tanh 归一后的幂律动量）
 */
export interface HexpFactorRaws {
  adx: number;
  er: number;
  ma: number;
  bbw: number;
  hurst: number;
  rsi: number;
  mm: number;
}

/**
 * 三阶段趋势进度（引擎 trend_phase 字段，2026-08-13 新增）。
 * 把「预启动(蓄势)→启动(点火)→确立(跟随)」映射到 0-100 连续进度，
 * 分界点固定 33 / 66（squeeze [0,33] / ignite [33,66] / establish [66,100]）。
 * 三分量均 0-100，用于面板进度条渲染与诊断。
 */
export interface TrendPhase {
  /** 连续进度 0-100 */
  progress: number;
  /** 当前阶段：squeeze=预启动 / ignite=启动 / establish=确立 */
  phase: 'squeeze' | 'ignite' | 'establish' | string;
  /** 蓄势度（BBW 收缩）0-100 */
  squeeze: number;
  /** 点火度（趋势态+动量+突破）0-100 */
  ignite: number;
  /** 确立度（ADX/ER/Hurst 趋势强度）0-100 */
  establish: number;
}

/** 三阶段 → 中文标签 */
export function phaseLabel(phase: TrendPhase['phase']): string {
  switch (phase) {
    case 'squeeze': return '预启动';
    case 'ignite': return '启动';
    case 'establish': return '确立';
    default: return phase || '—';
  }
}

/** 三阶段 → 色（灰蓝=蓄势 / 琥珀=点火 / 紫=确立） */
export function phaseColor(phase: TrendPhase['phase']): string {
  switch (phase) {
    case 'squeeze': return '#64748b';
    case 'ignite': return '#eab308';
    case 'establish': return '#a78bfa';
    default: return C.flat;
  }
}

export type PeriodState = 'TREND_UP' | 'TREND_DOWN' | 'RANGE' | 'TRANSITION' | string;
export type Direction = 'BUY' | 'SELL' | 'NO_TRADE' | string;
export type Grade = 'S' | 'A' | 'B' | 'C' | 'RED' | string;

/** hcm:live:hexp:{symbol} 实时快照全字段 */
export interface HexpSnapshot {
  direction: Direction;
  grade: Grade;
  hp_score: number;
  k: number;
  mm: number;
  verdict: number;
  scorecard_total: number;
  scorecard: HexpScorecard;
  factor_scores: HexpFactorScores;
  factor_raws?: HexpFactorRaws;
  trend_phase?: TrendPhase;
  period_states: Record<string, PeriodState>;
  trend_scores: Record<string, number>;
  used_periods: string[];
  primary_period: string;
  close: number;
  atr: number;
  passed: boolean;
  reason: string;
  ts: number;
  /** 2026-08-27 方向裁决总分（实时值，死标签/迟滞诊断用） */
  dir_sum?: number;
  /** 7 因子分解（adx/er/ma/bbw/hurst/rsi/mm），死标签诊断用 */
  dir_sum_factors?: Record<string, number>;
  /** 位置因子贡献（趋势态已降权，见 hexp.pos_factor.trend_scale） */
  dir_pos_factor?: number;
  /** 上次 direction（迟滞状态机，死标签/迟滞维持诊断用） */
  prev_direction?: Direction;
}

/**
 * 2026-08-27 方向诊断：把后端迟滞+强制翻转+死标签逻辑显式化到看板。
 * 输入 live 快照（含 direction / prev_direction / dir_sum / period_states），输出诊断标签：
 *   - 死标签风险：prev 与实时主周期趋势反向，且被迟滞维持（如下跌趋势 prev=BUY 仍显示 BUY）
 *   - 迟滞翻转放行：prev≠cur 且越过死区/共识（正常翻转）
 *   - 死区维持防抖：dir_sum 在死区内维持 cur（正常防抖，非错误）
 *   - 方向稳定：无切换
 */
export function directionDiag(snap: HexpSnapshot | null): {
  tag: string; color: string; desc: string; switched: boolean; deadLabel: boolean;
} {
  const dir = snap?.direction ?? 'NO_TRADE';
  const prev = snap?.prev_direction ?? 'NO_TRADE';
  const ds = snap?.dir_sum ?? 0;
  const ps = snap?.period_states ?? {};
  const pState = ps[snap?.primary_period ?? ''] ?? '';
  // 死标签：上次方向与实时主周期趋势相反，且当前仍被维持为该方向
  const deadLabel = (
    (prev === 'BUY' && pState === 'TREND_DOWN' && dir === 'BUY')
    || (prev === 'SELL' && pState === 'TREND_UP' && dir === 'SELL')
  );
  const switched = prev !== 'NO_TRADE' && dir !== 'NO_TRADE' && prev !== dir;
  const inDeadZone = Math.abs(ds) < 0.06;
  if (deadLabel) {
    return {
      tag: '死标签风险', color: C.down,
      desc: `上次方向 ${dirLabel(prev)} 与实时趋势(${stateLabel(pState)})反向，却被迟滞维持为 ${dirLabel(dir)}`,
      switched, deadLabel,
    };
  }
  if (switched) {
    return {
      tag: '迟滞翻转放行', color: C.up,
      desc: `方向 ${dirLabel(prev)} → ${dirLabel(dir)}（越过死区/周期共识强制翻转）`,
      switched, deadLabel,
    };
  }
  if (inDeadZone && dir !== 'NO_TRADE') {
    return {
      tag: '死区维持防抖', color: C.textDim,
      desc: `dir_sum=${ds.toFixed(2)} 在死区内，维持 ${dirLabel(dir)} 防抖（正常，非死标签）`,
      switched, deadLabel,
    };
  }
  return { tag: '方向稳定', color: C.textDim, desc: `综合方向 ${dirLabel(dir)}`, switched, deadLabel };
}

/** 配置中心扁平键值（hexp.* 命名空间） */
export type HexpConfig = Record<string, unknown>;

/**
 * AI 信号质量评分快照（`hcm:live:hexp:ai:{symbol}`，由独立 sidecar quality_scorer.py 发布）。
 * 三分数卡数据源：AI评分(ai_score) / 总分(total_score) / 外部因子评分(ext_factor_score)。
 */
export interface HexpAiSnapshot {
  symbol: string;
  /** LightGBM 真假概率×100（模型未启用/缺失=null） */
  ai_score: number | null;
  /** 耦合综合总分（解耦时=HEXP scorecard_total） */
  total_score: number | null;
  /** 外部因子评分 = 宏观/事件/情绪综合分×100（hcm:market:composite:score） */
  ext_factor_score: number | null;
  mode: 'coupled' | 'decoupled' | string;
  /** 三态状态：disabled(开关关)/missing(已开但模型缺失)/degraded(已加载但自检失败)/ready(就绪) */
  status?: 'disabled' | 'missing' | 'degraded' | 'ready' | string;
  /** 总开关 ai.enabled（sidecar 发布，供前端三态判定） */
  ai_enabled?: boolean;
  /** 当前模式 ai.mode（coupled/decoupled） */
  ai_mode?: string;
  model_loaded?: boolean;
  /** 是否真正成功推理（enabled+模型已加载+快照有效） */
  valid?: boolean;
  period_match?: string;
  ts: number;
}

// ── 配置读取（零硬编码，缺失时回落到与后端一致的默认值）──────────

/** 与后端 web/api/hexp.py HEXP_KEYS 默认值对齐的兜底表 */
const CONFIG_FALLBACK: Record<string, number> = {
  'hexp.scorecard.pass_threshold': 50.0,
  'hexp.scorecard.b_threshold': 52.0,
  'hexp.scorecard.a_threshold': 75.0,
  'hexp.scorecard.s_hp_min': 60.0,
  'hexp.scorecard.hp_floor': 30.0,
  'hexp.scorecard.weight_resonance': 12.0,
  'hexp.scorecard.weight_state': 27.0,
  'hexp.scorecard.weight_entry': 26.0,
  'hexp.scorecard.weight_position': 15.0,
  'hexp.scorecard.weight_vol': 10.0,
  'hexp.scorecard.weight_session': 10.0,
  'hexp.k.min': 0.5,
  'hexp.k.max': 3.0,
  'hexp.k.base': 1.5,
  'hexp.k.state_trend': 2.0,
  'hexp.k.state_range': 0.65,
  'hexp.k.state_transition': 1.0,
  'hexp.mm.scale': 0.002,
  'hexp.mm.accel_threshold': 0.7,
  'hexp.exec.sl_atr_mult': 2.0,
  'hexp.exec.rr_min': 1.5,
  'hexp.exec.lot_mult': 1.0,
  'hexp.exec.grade_lot_s': 1.2,
  'hexp.exec.grade_lot_a': 1.0,
  'hexp.exec.grade_lot_b': 0.5,
  'hexp.exec.grade_lot_c': 0.5,
  'hexp.exec.transition_lot_mult': 0.5,
  'hexp.exec.reversal_lot_mult': 0.5,
  'hexp.exec.reversal_hold_bars': 3,
  'hexp.direction_min_score': 0.2,
  'hexp.factor.adx_weight': 25.0,
  'hexp.factor.er_weight': 25.0,
  'hexp.factor.ma_weight': 20.0,
  'hexp.factor.bbw_weight': 15.0,
  'hexp.factor.hurst_weight': 10.0,
  'hexp.factor.rsi_weight': 5.0,
  'hexp.factor.mm_weight': 15.0,
};

/**
 * 共振裁决展示参考线（纯 UI 用，不是交易阈值）。
 *
 * 历史上这里读的是 hexp.mtf.long_threshold / short_threshold —— 那是方案 B 之前
 * "verdict≥0.5 禁止开空 / ≤-0.5 禁止开多" 的硬封铁律。方案 B 已废除硬封，改为
 * 顺/逆风对称降分，两个配置键已于 2026-08-11 删除。此处保留 ±0.5 只作为滑杆上
 * "共振显著"的视觉刻度，改动它不会影响任何下单决策。
 */
export const VERDICT_REF_LONG = 0.5;
export const VERDICT_REF_SHORT = -0.5;

/** 从配置对象安全取数值；缺失或非法时回落到后端对齐的默认值 */
export function cfgNum(cfg: HexpConfig | null, key: string): number {
  const raw = cfg ? cfg[key] : undefined;
  const n = typeof raw === 'number' ? raw : Number(raw);
  if (raw !== undefined && raw !== null && Number.isFinite(n)) return n;
  return CONFIG_FALLBACK[key] ?? 0;
}

// ── 配色系统（中国市场习惯：涨红跌绿）────────────────────────────

export const C = {
  /** 涨 / BUY / 正向因子 —— 红 */
  up: '#ef4444',
  upSoft: 'rgba(239,68,68,0.16)',
  /** 跌 / SELL / 负向因子 —— 绿 */
  down: '#22c55e',
  downSoft: 'rgba(34,197,94,0.16)',
  /** 中性 / 无交易 */
  flat: '#64748b',
  flatSoft: 'rgba(100,116,139,0.16)',
  /** 转换态 / 预警 —— 琥珀 */
  warn: '#eab308',
  warnSoft: 'rgba(234,179,8,0.16)',
  /** 数据蓝（中性量化数值） */
  info: '#3b82f6',
  infoSoft: 'rgba(59,130,246,0.16)',
  /** S 级金 */
  gold: '#fbbf24',
  /** 紫（幂指数专用） */
  violet: '#a78bfa',

  panelBg: '#111118',
  panelBg2: '#0d0d14',
  border: 'rgba(148,163,184,0.16)',
  borderStrong: 'rgba(148,163,184,0.30)',
  textMain: '#f1f5f9',
  textDim: '#94a3b8',
  textFaint: '#64748b',
} as const;

/** 方向 → 主色（BUY 红 / SELL 绿 / NO_TRADE 灰） */
export function dirColor(dir: Direction): string {
  if (dir === 'BUY') return C.up;
  if (dir === 'SELL') return C.down;
  return C.flat;
}

export function dirLabel(dir: Direction): string {
  if (dir === 'BUY') return '做多 BUY';
  if (dir === 'SELL') return '做空 SELL';
  return '无交易 NO_TRADE';
}

/** 等级 → 色。注意：等级色统一走「描边+文字」，不与方向实心色抢语义 */
export function gradeColor(g: Grade): string {
  switch (g) {
    case 'S': return C.gold;
    case 'A': return C.info;
    case 'B': return '#06b6d4';
    case 'C': return C.textDim;
    case 'RED': return C.up;
    default: return C.flat;
  }
}

export function gradeDesc(g: Grade): string {
  switch (g) {
    case 'S': return '最高置信，加仓执行';
    case 'A': return '高置信，标准仓位';
    case 'B': return '中等置信，减半仓';
    case 'C': return '低置信，最小仓';
    case 'RED': return '禁止入场（未过闸门）';
    default: return '未知等级';
  }
}

/** 周期状态 → 色 */
export function stateColor(s: PeriodState): string {
  if (s === 'TREND_UP') return C.up;
  if (s === 'TREND_DOWN') return C.down;
  if (s === 'TRANSITION') return C.warn;
  return C.flat; // RANGE 及未知
}

export function stateLabel(s: PeriodState): string {
  switch (s) {
    case 'TREND_UP': return '上行趋势';
    case 'TREND_DOWN': return '下行趋势';
    case 'RANGE': return '区间震荡';
    case 'TRANSITION': return '状态转换';
    default: return s || '—';
  }
}

// ── 幂指数 k 的市况解读 ────────────────────────────────────────

export interface KRegime {
  label: string;
  color: string;
  desc: string;
}

/**
 * 幂指数 k 的语义解读。
 * k > 1 → 凸增强（趋势市，强因子被放大）；
 * k ≈ 1 → 线性（转换中）；
 * k < 1 → 凹收敛（震荡市，抑制单因子暴走）。
 */
export function kRegime(k: number): KRegime {
  if (k >= 1.6) return { label: '趋势·凸增强', color: C.up, desc: 'k>1 放大强因子，顺势追踪' };
  if (k > 1.15) return { label: '偏趋势', color: C.warn, desc: '轻度凸性，趋势初现' };
  if (k >= 0.85) return { label: '转换·线性', color: C.info, desc: 'k≈1 等权线性，市况过渡' };
  if (k >= 0.6) return { label: '偏震荡', color: '#06b6d4', desc: '轻度凹性，谨慎追单' };
  return { label: '震荡·凹收敛', color: C.down, desc: 'k<1 抑制单因子暴走，防假突破' };
}

// ── 因子中文名 ────────────────────────────────────────────────

export const FACTOR_LABELS: Record<keyof HexpFactorScores, string> = {
  adx: 'ADX 趋向强度',
  er: 'ER 效率比',
  ma: 'MA 均线结构',
  bbw: 'BBW 波动带宽',
  hurst: 'Hurst 持续性',
  rsi: 'RSI 超买超卖',
  mm: 'MM 微结构动量',
};

/** 因子原始值的单位/后缀（用于展示 factor_raws 实时读数） */
export const FACTOR_RAW_UNITS: Record<keyof HexpFactorRaws, string> = {
  adx: '',
  er: '',
  ma: '',
  bbw: '%',
  hurst: '',
  rsi: '',
  mm: '',
};

/** 因子原始值的小数位（按指标量纲定：ADX/RSI 1 位，ER/Hurst 3 位，MM 2 位，ma/bbw 取整） */
const FACTOR_RAW_DIGITS: Record<keyof HexpFactorRaws, number> = {
  adx: 1,
  er: 3,
  ma: 0,
  bbw: 0,
  hurst: 3,
  rsi: 1,
  mm: 2,
};

/** 格式化单个因子原始值（如 adx → "21.3"，bbw → "50%"） */
export function factorRawText(key: keyof HexpFactorRaws, v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const d = FACTOR_RAW_DIGITS[key] ?? 2;
  const unit = FACTOR_RAW_UNITS[key] ?? '';
  return `${v.toFixed(d)}${unit}`;
}

/** 因子 → 对应权重配置键 */
export const FACTOR_WEIGHT_KEYS: Record<keyof HexpFactorScores, string> = {
  adx: 'hexp.factor.adx_weight',
  er: 'hexp.factor.er_weight',
  ma: 'hexp.factor.ma_weight',
  bbw: 'hexp.factor.bbw_weight',
  hurst: 'hexp.factor.hurst_weight',
  rsi: 'hexp.factor.rsi_weight',
  mm: 'hexp.factor.mm_weight',
};

export const SCORECARD_LABELS: Record<keyof HexpScorecard, string> = {
  resonance: '多周期共振',
  state: '状态强度',
  entry: '入场时机',
  position: '价格位置',
  vol: '波动适配',
  session: '时段质量',
};

export const SCORECARD_WEIGHT_KEYS: Record<keyof HexpScorecard, string> = {
  resonance: 'hexp.scorecard.weight_resonance',
  state: 'hexp.scorecard.weight_state',
  entry: 'hexp.scorecard.weight_entry',
  position: 'hexp.scorecard.weight_position',
  vol: 'hexp.scorecard.weight_vol',
  session: 'hexp.scorecard.weight_session',
};

// ── 入场模式推断（⚠ 前端推断，引擎未输出该字段）────────────────

export type EntryModeKey = 'A_PULLBACK' | 'B_BREAKOUT' | 'WAIT';

export interface EntryModeInference {
  mode: EntryModeKey;
  label: string;
  color: string;
  /** 回踩模式得分 0~1 */
  pullbackScore: number;
  /** 突破模式得分 0~1 */
  breakoutScore: number;
  /** 归一后的微结构动量 -1~1 */
  mmNorm: number;
  /** 动量是否与主周期趋势同向 */
  aligned: boolean;
  /** 判定依据的人话解释 */
  rationale: string;
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/**
 * 从实时快照推断当前更接近哪种入场模式。
 *
 * **重要**：引擎 `hexp_engine.py` 当前不输出 `entry_mode` 字段，本函数是
 * 看板侧的启发式推断，**仅供观察参考，不参与任何下单决策**。UI 必须显式
 * 标注「推断」二字，避免被误当作引擎结论。
 *
 * 推断依据：
 *  - 模式 B（突破）：主周期处于趋势态，且微结构动量 |mm| 放大并与趋势同向，
 *    叠加幂指数 k 偏高（凸增强，趋势自我强化）。
 *  - 模式 A（回踩）：主周期趋势态，但动量暂时逆向/衰减，且 RSI 因子朝趋势
 *    反方向偏离（回调），属于顺势回踩candidate。
 *  - 观望：主周期震荡，或两种模式得分都不足。
 */
export function inferEntryMode(
  snap: HexpSnapshot | null,
  cfg: HexpConfig | null,
): EntryModeInference {
  const base: EntryModeInference = {
    mode: 'WAIT',
    label: '观望 · 无明确入场模式',
    color: C.flat,
    pullbackScore: 0,
    breakoutScore: 0,
    mmNorm: 0,
    aligned: false,
    rationale: '暂无实时快照数据',
  };
  if (!snap) return base;

  const scale = cfgNum(cfg, 'hexp.mm.scale') || 0.002;
  const accelTh = cfgNum(cfg, 'hexp.mm.accel_threshold');
  const kMin = cfgNum(cfg, 'hexp.k.min');
  const kMax = cfgNum(cfg, 'hexp.k.max');

  const mmNorm = clamp((snap.mm ?? 0) / scale, -1, 1);
  const primary = snap.primary_period || 'M5';
  const pState = (snap.period_states || {})[primary] || 'RANGE';
  const isTrend = pState === 'TREND_UP' || pState === 'TREND_DOWN';
  const trendUp = pState === 'TREND_UP';
  // 2026-08-28 修复：trendDown 此前从未定义（2026-08-27 铁律对齐改动遗漏），
  // 导致下方 aligned 判定引用未定义标识符 → tsc 报 TS2304、前端构建失败。
  // 补上对称定义，语义不变（下行趋势态）。
  const trendDown = pState === 'TREND_DOWN';

  if (!isTrend) {
    return {
      ...base,
      mmNorm,
      rationale: `主周期 ${primary} 为${stateLabel(pState)}，非趋势态不适用回踩/突破模式`,
    };
  }

  // 2026-08-27 铁律对齐：方向语义统一用综合裁决 snap.direction，禁止用 mm 符号
  // （mm 在 0 附近高频抖 → 方向闪烁缺陷）。mmNorm 仅保留作强度(absMM)，不再驱动 aligned（顺势判定）。
  const aligned = (snap.direction === 'BUY' && trendUp) || (snap.direction === 'SELL' && trendDown);
  const absMM = Math.abs(mmNorm);

  // k 归一（越高越偏凸性 → 越支持突破追单）
  const kNorm = clamp((snap.k - kMin) / Math.max(1e-6, kMax - kMin), 0, 1);
  // RSI 因子与趋势反向的程度（越大 → 越像回调）
  const rsi = snap.factor_scores?.rsi ?? 0;
  const counterRsi = clamp(trendUp ? -rsi : rsi, 0, 1);

  const breakoutScore = clamp((aligned ? absMM : 0) * 0.62 + kNorm * 0.38, 0, 1);
  const pullbackScore = clamp(
    (aligned ? Math.max(0, 0.45 - absMM) : Math.min(1, 0.45 + absMM * 0.55)) * 0.55 +
      counterRsi * 0.45,
    0,
    1,
  );

  const trendWord = trendUp ? '上行' : '下行';
  const decisive = Math.max(breakoutScore, pullbackScore);
  if (decisive < 0.45) {
    return {
      mode: 'WAIT',
      label: '观望 · 模式特征不明确',
      color: C.flat,
      pullbackScore,
      breakoutScore,
      mmNorm,
      aligned,
      rationale: `${primary} ${trendWord}趋势，但动量与位置特征均不突出（两模式得分均 < 0.45）`,
    };
  }

  if (breakoutScore >= pullbackScore) {
    return {
      mode: 'B_BREAKOUT',
      label: '模式 B · 突破追进（推断）',
      color: trendUp ? C.up : C.down,
      pullbackScore,
      breakoutScore,
      mmNorm,
      aligned,
      rationale:
        `${primary} ${trendWord}趋势，微结构动量 ${mmNorm >= 0 ? '+' : ''}${mmNorm.toFixed(2)} ` +
        `${aligned ? '与趋势同向' : '逆向'}${absMM >= accelTh ? '且已过加速阈值' : ''}，` +
        `k=${snap.k.toFixed(2)} 偏凸增强 → 更接近突破型入场`,
    };
  }

  return {
    mode: 'A_PULLBACK',
    label: '模式 A · 回踩承接（推断）',
    color: C.warn,
    pullbackScore,
    breakoutScore,
    mmNorm,
    aligned,
    rationale:
      `${primary} ${trendWord}趋势未破，但动量 ${mmNorm >= 0 ? '+' : ''}${mmNorm.toFixed(2)} ` +
      `${aligned ? '衰减' : '短暂逆向'}、RSI 因子朝反向偏离 ${counterRsi.toFixed(2)} → ` +
      `更接近顺势回踩型入场`,
  };
}

// ── 格式化工具 ────────────────────────────────────────────────

export function fmt(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—';
  return n.toFixed(digits);
}

export function fmtSigned(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—';
  return `${n >= 0 ? '+' : ''}${n.toFixed(digits)}`;
}

/** Unix 秒 → HH:MM:SS */
export function tsToClock(ts: number | null | undefined): string {
  if (!ts || !Number.isFinite(ts)) return '—';
  const d = new Date(ts * 1000);
  const p = (v: number): string => String(v).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 快照新鲜度（秒）。引擎 TTL 15s，超过约 20s 视为陈旧 */
export function snapshotAgeSec(ts: number | null | undefined): number | null {
  if (!ts || !Number.isFinite(ts)) return null;
  return Math.max(0, Date.now() / 1000 - ts);
}

// ── K 线入库实时状态 ──────────────────────────────────────────
// 数据源：hcm-collector 采集 K 线/Tick → 写入 PG `hcm_market.klines`（即「入库」）。
// 经 `GET /api/v1/dashboard/realtime?symbol=&timeframe=` 透出 `latest_kline.open_time`
// 与 `live_price.updated_at`（bridge 每 2s tick，最实时的行情源心跳）。
// 看板借此直观呈现「各周期数据是否在生产、行情源是否实时」。

/** 各周期 K 线的标准棒间隔（秒），用于判定入库新鲜度 */
export const KLINE_INTERVAL_SEC: Record<string, number> = {
  M1: 60,
  M5: 300,
  M30: 1800,
  H1: 3600,
  H4: 14400,
  D1: 86400,
};

export type KlineFresh = 'live' | 'lag' | 'stale' | 'none';

/** 单周期 K 线入库状态 */
export interface KlineIngest {
  /** 最新一根已入库 K 线的 open_time（unix 秒）；无则为 null */
  openTime: number | null;
  /** 最新一根收盘价（用于 tooltip 展示，可选） */
  close: number | null;
  /** now - openTime（秒） */
  ageSec: number | null;
  /** 新鲜度判定 */
  fresh: KlineFresh;
  /** 当前棒已累计 tick 数（来自 collector 实时写入，是「实时生产」最直接的证据；无则 null） */
  tickCount: number | null;
}

/**
 * 由最新棒 open_time 判定入库新鲜度。
 * 数据源为 collector 每 tick 写入的 Redis `latest_kline:{symbol}:{tf}`（实时棒，
 * open_time 为该棒起始时刻，会随棒推进而滚动）。collector 写入节奏通常落后 0~2 根棒，
 * 故阈值按周期倍数的较宽松档位判定，避免把正常节奏误判为异常：
 *  - live ：age ≤ 2×interval              → 当前/最近棒已就绪，实时入库正常
 *  - lag  ：2×interval < age ≤ 6×interval → 落后数根棒，滞后再现中
 *  - stale：age > 6×interval 或 无数据     → 入库中断
 * 注：tickCount>0 是「实时生产仍在流动」的更强证据，优先级高于 open_time 年龄。
 */
export function klineFreshness(openTime: number | null, intervalSec: number): KlineFresh {
  if (!openTime || !Number.isFinite(openTime)) return 'none';
  const age = Date.now() / 1000 - openTime;
  if (age < 0) return 'none';
  if (age <= intervalSec * 2) return 'live';
  if (age <= intervalSec * 6) return 'lag';
  return 'stale';
}

/**
 * 新鲜度 → 状态色与中文标签。
 * 注意：此处是「健康指示」语义（绿=正常/红=异常），与价格方向配色（红涨绿跌）无关。
 */
export function klineStatusMeta(f: KlineFresh): { color: string; label: string } {
  switch (f) {
    case 'live': return { color: C.down, label: '实时' };      // 绿=正常
    case 'lag': return { color: C.warn, label: '滞后' };       // 琥珀=滞后
    case 'stale': return { color: C.up, label: '中断' };       // 红=异常
    default: return { color: C.flat, label: '无数据' };
  }
}

/** 行情源（bridge 2s tick）实时性判定：<15s 实时 / <60s 延迟 / 否则断开 */
export function feedStatusMeta(ageSec: number | null): { color: string; label: string } {
  if (ageSec === null || !Number.isFinite(ageSec)) return { color: C.flat, label: '未知' };
  if (ageSec <= 15) return { color: C.down, label: '实时' };
  if (ageSec <= 60) return { color: C.warn, label: '延迟' };
  return { color: C.up, label: '断开' };
}

// ── 信号日志（前端环形缓冲）────────────────────────────────────

export interface LogEntry {
  id: number;
  ts: number;
  direction: Direction;
  grade: Grade;
  hpScore: number;
  k: number;
  total: number;
  passed: boolean;
  verdict: number;
  primaryState: PeriodState;
  entryMode: EntryModeKey;
  reason: string;
  /** 触发本条记录的变化描述（如「等级 C→B」） */
  change: string;
}

/** 环形缓冲容量 */
export const LOG_CAPACITY = 200;

/**
 * 判定新快照相对上一条日志是否构成「值得记录的变化」。
 * 返回变化描述；无实质变化返回 null。
 */
export function diffForLog(
  prev: LogEntry | undefined,
  snap: HexpSnapshot,
  mode: EntryModeKey,
  hpDelta: number,
): string | null {
  if (!prev) return '首帧快照';
  const parts: string[] = [];
  if (prev.direction !== snap.direction) parts.push(`方向 ${prev.direction}→${snap.direction}`);
  if (prev.grade !== snap.grade) parts.push(`等级 ${prev.grade}→${snap.grade}`);
  if (prev.passed !== snap.passed) parts.push(`闸门 ${prev.passed ? '放行' : '拦截'}→${snap.passed ? '放行' : '拦截'}`);
  const pState = (snap.period_states || {})[snap.primary_period] || '';
  if (prev.primaryState !== pState) parts.push(`主周期 ${stateLabel(prev.primaryState)}→${stateLabel(pState)}`);
  if (prev.entryMode !== mode) parts.push('入场模式切换');
  if (Math.abs(prev.hpScore - snap.hp_score) >= hpDelta) {
    parts.push(`HP ${prev.hpScore.toFixed(1)}→${snap.hp_score.toFixed(1)}`);
  }
  return parts.length ? parts.join(' · ') : null;
}
