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
    symbolsCompare: '/api/v1/dashboard/symbols-compare',
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
  },
  close: {
    config: '/api/close/config',
  },
  dispatch: {
    config: '/api/dispatch/config',
  },
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
    threshold: '/api/signal-tower/threshold',
    mode: '/api/signal-tower/mode',
    symbolConfig: '/api/signal-tower/symbol-config',
    prompt: '/api/signal-tower/prompt',
    watchdog: '/api/signal-tower/watchdog',
    retrainSummary: '/api/v1/signal-tower/retrain/summary',
  },
  cosource: {
    config: '/api/cosource/config',
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
    },
  },
  hexp: {
    config: '/api/v1/hexp/config',
    signal: '/api/v1/hexp/signal',
    ai: '/api/v1/hexp/ai',
  },
} as const;

export type EndpointRegistry = typeof ENDPOINTS;
