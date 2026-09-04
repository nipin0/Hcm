/** Unified API endpoint registry — the single source of truth for all API paths.
 *
 * **Rule: NEVER hardcode a URL string in any component. Always import from here.**
 *
 * Usage:
 *   import { ENDPOINTS } from '../../api/endpoints';
 *   client.get(ENDPOINTS.positions.list);
 *   client.get(`${ENDPOINTS.positions.detail(123)}`);
 */

export const ENDPOINTS = {
  auth: {
    login: '/api/auth/login',
  },
  positions: {
    list: '/api/v1/positions',
    history: '/api/v1/positions/history',
    detail: (id: number) => `/api/v1/positions/${id}` as const,
  },
  dashboard: {
    realtime: '/api/dashboard/realtime',
    signalGauges: '/api/dashboard/signal-gauges',
    statistics: '/api/v1/dashboard/statistics',
    // 【2026-08-28】移除 symbolsCompare 端点常量（品种对比页已下线，前端无消费方）。
    // 注意：后端 /api/dashboard/compare → /api/v1/dashboard/symbols-compare
    // （dashboard.py:1222）**保留**，脚本/外部调用仍可访问。
    externalFactors: '/api/v1/dashboard/external-factors',
    /** equity-curve stub — backend returns empty data until full implementation */
    equityCurve: '/api/v1/dashboard/equity-curve',
    /** live score snapshot — 能否下单的核心判定 (pre_score / threshold / threshold_passed) */
    liveScore: '/api/v1/dashboard/live-score',
    /** 全备份触发 + 状态查询（PG dump + Redis RDB + 源码 → D:\HCM_ASST\backup） */
    backup: '/api/v1/system/backup',
    backupStatus: '/api/v1/system/backup/status',
  },
  risk: {
    config: '/api/v1/risk/config',
    symbolLimits: '/api/v1/risk/symbol-limits',
    interceptLogs: '/api/v1/risk/intercept-logs',
  },
  system: {
    users: '/api/system/users',
    mt5: '/api/system/mt5',
    deepseek: '/api/system/deepseek',
    network: '/api/system/network',
    notifications: '/api/system/notifications',
    cacheStats: '/api/system/cache/stats',
    cacheClear: '/api/system/cache/clear',
    accounts: '/api/system/accounts',
    health: '/api/system/health/detailed',
    diagnose: '/api/v1/system/diagnose',
    activate: '/api/v1/system/engine/activate',
    calibrateConfig: '/api/v1/system/calibrate-config',
    // 2026-09-02 AI 组件自愈：TimesFM + LightGBM sidecar（容器只能写信令，
    // 实际拉起由主机计划任务 HCM_AIStackGuard 执行）
    aiHeal: '/api/v1/system/ai/heal',
    aiStatus: '/api/v1/system/ai/status',
  },
  close: {
    config: '/api/close/config',
  },
  // 【2026-08-28】移除 dispatch 端点常量（分发配置页已下线，前端无消费方）。
  // 注意：后端 GET/PUT /api/dispatch/config（dispatch.py:174/181）**保留**，
  // 运维/脚本仍可读写 dispatch.* 配置。
  datasource: {
    config: '/api/datasource/config',
  },
  engine: {
    rules: '/api/engine/rules',
  },
  copy: {
    relationships: '/api/v1/copy/relationships',
    symbolMappings: '/api/v1/copy/symbol-mappings',
    accounts: '/api/v1/copy/accounts',
    brokers: '/api/v1/copy/brokers',
  },
  signalTower: {
    // 【2026-08-28】移除 threshold 端点常量（评分阈值页已下线，前端无消费方）。
    // 注意：后端 GET/PUT /api/signal-tower/threshold（signal_tower.py:1621/1630）**保留**——
    // 它是那 35 个活键（scoring.* / regime.*）唯一的配置读写通道，运维/脚本仍可调参。
    mode: '/api/signal-tower/mode',
    // 【2026-08-28】移除 symbolConfig 端点常量（品种级配置页已下线，前端无消费方）。
    // 注意：后端 GET/PUT /api/signal-tower/symbol-config（signal_tower.py:1655/1664）**保留**，
    // 品种级信号塔参数仍可通过该接口调参。
    prompt: '/api/signal-tower/prompt',
    watchdog: '/api/signal-tower/watchdog',
    retrainSummary: '/api/v1/signal-tower/retrain/summary',
  },
  // 【2026-08-28】原 cosource 端点重命名为 engineMode（双源信号下线，
  // 该端点现仅承载 signal.active_model 引擎切换）。
  engineMode: {
    config: '/api/engine-mode/config',
  },
  ai: {
    config: '/api/v1/ai/config',
    report: {
      health: '/api/v1/ai/report/health',
      layer: '/api/v1/ai/report/layer',
      performance: '/api/v1/ai/report/performance',
      snapshot: '/api/v1/ai/report/snapshot',
      daily: '/api/v1/ai/report/daily',
      monitor: '/api/v1/ai/report/monitor',
      reversal: '/api/v1/ai/report/reversal',
    },
    // [2026-08-29] AI 中枢监控（只读）：三头实时运作 + DeepSeek 工作效果 + 自愈中心
    ops: {
      live: '/api/v1/ai/ops/live',
      decisions: '/api/v1/ai/ops/decisions',
      dsStats: '/api/v1/ai/ops/ds-stats',
      selfHeal: '/api/v1/ai/ops/selfheal',
    },
  },
  hexp: {
    config: '/api/v1/hexp/config',
    signal: '/api/v1/hexp/signal',
    ai: '/api/v1/hexp/ai',
  },
} as const;

export type EndpointRegistry = typeof ENDPOINTS;
