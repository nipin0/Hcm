/**
 * HCM v2 Frontend — QA Validation Test Suite
 * Comprehensive static analysis verification covering 7 areas:
 * 1. Route completeness
 * 2. Component structure
 * 3. Authentication flow
 * 4. Symbol linkage
 * 5. Dashboard chart libraries
 * 6. WebSocket hook
 * 7. TypeScript types
 */

import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = join(__dirname, '..', 'src');

// ============================================================
// Helper utilities
// ============================================================
function readSrc(relativePath) {
  return readFileSync(join(SRC, relativePath), 'utf-8');
}

function fileExists(relativePath) {
  return existsSync(join(SRC, relativePath));
}

// ============================================================
// AREA 1: Route Integrity
// ============================================================
describe('AREA 1 — Route Integrity (App.tsx)', () => {
  const appContent = readSrc('App.tsx');

  const expectedRoutes = [
    // Login
    '/login',
    // System (6 routes)
    '/system/users', '/system/network', '/system/mt5',
    '/system/deepseek', '/system/notifications', '/system/cache',
    // Signal Tower (3 routes) — [2026-08-28] 移除 /signal-tower/threshold（评分阈值页下线）、
    // /signal-tower/symbol-config（品种级配置页下线）
    '/signal-tower/mode', '/signal-tower/prompt',
    '/signal-tower/watchdog',
    // Datasource
    '/datasource',
    // Engine
    '/engine/rules',
    // Other config (3 routes) — [2026-08-28] 移除 /dispatch（分发配置页下线）
    '/risk', '/close', '/copy',
    // Dashboard (3 routes) — [2026-08-28] 移除 /dashboard/realtime、
    // /dashboard/statistics、/dashboard/compare（页面下线）
    '/dashboard/positions', '/dashboard/health', '/dashboard/factors',
  ];

  it('should contain all 19 primary routes (login + 18 protected)', () => {
    const routeCount = (appContent.match(/path="\/[\w/-]+"/g) || []).length;
    // Expect at least 24 path= attributes (including catch-all)
    assert.ok(routeCount >= 24, `Expected >= 24 routes, found ${routeCount}`);
  });

  for (const route of expectedRoutes) {
    it(`should have route: ${route}`, () => {
      assert.ok(
        appContent.includes(`path="${route}"`),
        `Missing route: ${route}`,
      );
    });
  }

  it('should have ProtectedRoute component wrapping protected pages', () => {
    const protectedRouteDef = appContent.match(/const ProtectedRoute/g);
    assert.ok(protectedRouteDef, 'ProtectedRoute component definition missing');
  });

  it('ProtectedRoute should redirect to /login when not authenticated', () => {
    assert.ok(appContent.includes('Navigate to="/login"'), 'ProtectedRoute missing login redirect');
  });

  it('ProtectedRoute should show loading spinner when isLoading', () => {
    assert.ok(appContent.includes('isLoading'), 'ProtectedRoute missing isLoading check');
    assert.ok(appContent.includes('animate-spin'), 'ProtectedRoute missing loading spinner');
  });

  // 【2026-08-28】实时信号流页下线，默认/Catch-all 落地页改为 /hexp/dashboard
  it('should have default redirect from / to /hexp/dashboard', () => {
    assert.ok(
      appContent.includes('path="/"') && appContent.includes('/hexp/dashboard'),
      'Default redirect / → /hexp/dashboard missing',
    );
  });

  it('should have catch-all redirect to /hexp/dashboard', () => {
    assert.ok(
      appContent.includes('path="*"') && appContent.includes('/hexp/dashboard'),
      'Catch-all redirect * → /hexp/dashboard missing',
    );
  });

  it('should lazy-load page components (import statements)', () => {
    const importCount = (appContent.match(/import \w+ from '\.\/pages\//g) || []).length;
    // 1 login + 6 system + 5 signaltower + 1 datasource + 1 engine + 4 config + 6 dashboard = 24
    assert.ok(importCount >= 23, `Expected >= 23 page imports, found ${importCount}`);
  });

  it('should wrap all non-login routes with ProtectedRoute', () => {
    // Count ProtectedRoute usages in JSX
    const protectedUsages = (appContent.match(/<ProtectedRoute>/g) || []).length;
    assert.ok(protectedUsages >= 23, `Expected >= 23 ProtectedRoute usages, found ${protectedUsages}`);
  });

  it('Login page should NOT be wrapped with ProtectedRoute', () => {
    // Login should be a direct element
    assert.ok(
      appContent.includes('path="/login" element={<Login />}'),
      'Login page should not have ProtectedRoute',
    );
  });
});

// ============================================================
// AREA 2: Component Structure
// ============================================================
describe('AREA 2 — Component Structure', () => {
  const requiredComponents = [
    'Layout.tsx',
    'SymbolSelector.tsx',
    'KpiCard.tsx',
    'StatusBar.tsx',
    'ConfigForm.tsx',
  ];

  for (const comp of requiredComponents) {
    it(`component ${comp} should exist`, () => {
      assert.ok(fileExists(`components/${comp}`), `Missing component: ${comp}`);
    });
  }

  describe('Layout.tsx', () => {
    const layout = readSrc('components/Layout.tsx');

    // [2026-08-28] 9 → 8：移除「分发」组（分发配置页下线）
    it('should have 8 navigation groups', () => {
      const navGroupCount = (layout.match(/basePath: '/g) || []).length;
      assert.equal(navGroupCount, 8, `Expected 8 nav groups, found ${navGroupCount}`);
    });

    it('should include SymbolSelector in sidebar', () => {
      assert.ok(layout.includes('SymbolSelector'), 'Layout missing SymbolSelector');
    });

    it('should have collapsible navigation groups', () => {
      assert.ok(layout.includes('Collapse'), 'Layout missing Collapse for nav groups');
      assert.ok(layout.includes('expandedGroups'), 'Layout missing expandedGroups state');
    });

    it('should render breadcrumbs', () => {
      assert.ok(layout.includes('Breadcrumbs'), 'Layout missing Breadcrumbs');
    });

    it('should have sidebar width of 260px', () => {
      assert.ok(layout.includes('w-[260px]'), 'Layout sidebar should be 260px');
    });

    it('nav group labels should be in Chinese', () => {
      const labels = ['数据看板', '系统设置', '信号塔', '数据源', '推理引擎', '分发', '风控', '平仓', '跟单'];
      for (const label of labels) {
        assert.ok(layout.includes(label), `Layout nav missing label: ${label}`);
      }
    });
  });

  describe('SymbolSelector.tsx', () => {
    const selector = readSrc('components/SymbolSelector.tsx');

    it('should use Select component from MUI', () => {
      assert.ok(selector.includes('Select'), 'SymbolSelector missing MUI Select');
    });

    it('should iterate symbolList for MenuItems', () => {
      assert.ok(selector.includes('symbolList.map'), 'SymbolSelector should map symbolList');
    });

    it('should display exchange and category info', () => {
      assert.ok(selector.includes('exchange'), 'SymbolSelector missing exchange display');
      assert.ok(selector.includes('category'), 'SymbolSelector missing category display');
    });

    it('should call setSelectedSymbol on change', () => {
      assert.ok(selector.includes('setSelectedSymbol'), 'SymbolSelector missing setSelectedSymbol call');
    });
  });

  describe('KpiCard.tsx', () => {
    const kpiCard = readSrc('components/KpiCard.tsx');

    it('should have required props: title, value, unit, change, color, subtitle', () => {
      assert.ok(kpiCard.includes('title: string'), 'KpiCard missing title prop');
      assert.ok(kpiCard.includes('value: string | number'), 'KpiCard missing value prop');
      assert.ok(kpiCard.includes('unit?: string'), 'KpiCard missing unit prop');
      assert.ok(kpiCard.includes('change?: number'), 'KpiCard missing change prop');
      assert.ok(kpiCard.includes('color?: string'), 'KpiCard missing color prop');
      assert.ok(kpiCard.includes('subtitle?: string'), 'KpiCard missing subtitle prop');
    });

    it('should use TrendingUp/TrendingDown for change indicator', () => {
      assert.ok(kpiCard.includes('TrendingUp'), 'KpiCard missing TrendingUp');
      assert.ok(kpiCard.includes('TrendingDown'), 'KpiCard missing TrendingDown');
    });

    it('should have colored top border accent', () => {
      assert.ok(kpiCard.includes('backgroundColor: color'), 'KpiCard missing colored accent');
    });
  });

  describe('StatusBar.tsx', () => {
    const statusBar = readSrc('components/StatusBar.tsx');

    it('should track PG, Redis, Gateway services', () => {
      assert.ok(statusBar.includes("'PG'"), 'StatusBar missing PG');
      assert.ok(statusBar.includes("'Redis'"), 'StatusBar missing Redis');
      assert.ok(statusBar.includes("'Gateway'"), 'StatusBar missing Gateway');
    });

    it('should poll /api/health endpoint', () => {
      assert.ok(statusBar.includes('/api/health'), 'StatusBar missing health endpoint');
    });

    it('should have a live clock', () => {
      assert.ok(statusBar.includes('setInterval'), 'StatusBar missing clock interval');
      assert.ok(statusBar.includes('toLocaleString'), 'StatusBar missing clock formatting');
    });

    it('should show WS connection status', () => {
      assert.ok(statusBar.includes('wsConnected'), 'StatusBar missing WS status');
    });

    it('should have color coding for status (online=green, offline=red, degraded=yellow)', () => {
      assert.ok(statusBar.includes('#22c55e'), 'StatusBar missing online (green) color');
      assert.ok(statusBar.includes('#ef4444'), 'StatusBar missing offline (red) color');
      assert.ok(statusBar.includes('#eab308'), 'StatusBar missing degraded (yellow) color');
    });
  });

  describe('ConfigForm.tsx', () => {
    const form = readSrc('components/ConfigForm.tsx');

    it('should support 6 field types: text, number, select, textarea, password, switch', () => {
      const types = ['text', 'number', 'select', 'textarea', 'password', 'switch'];
      for (const t of types) {
        assert.ok(form.includes(`'${t}'`), `ConfigForm missing field type: ${t}`);
      }
    });

    it('should have save/reset/cancel buttons', () => {
      assert.ok(form.includes('保存'), 'ConfigForm missing save button');
      assert.ok(form.includes('取消'), 'ConfigForm missing cancel button');
      assert.ok(form.includes('重置'), 'ConfigForm missing reset button');
    });

    it('should track dirty state', () => {
      assert.ok(form.includes('dirty'), 'ConfigForm missing dirty state');
    });

    it('should support apiEndpoint display', () => {
      assert.ok(form.includes('apiEndpoint'), 'ConfigForm missing apiEndpoint prop');
    });

    it('should support fetchValues for reloading', () => {
      assert.ok(form.includes('fetchValues'), 'ConfigForm missing fetchValues prop');
    });
  });
});

// ============================================================
// AREA 3: Authentication Flow
// ============================================================
describe('AREA 3 — Authentication Flow', () => {
  describe('AuthContext.tsx', () => {
    const authCtx = readSrc('contexts/AuthContext.tsx');

    it('should export AuthProvider and useAuth hook', () => {
      assert.ok(authCtx.includes('export const AuthProvider'), 'AuthContext missing AuthProvider export');
      assert.ok(authCtx.includes('export const useAuth'), 'AuthContext missing useAuth export');
    });

    it('should define AuthUser interface with id, username, email, role, permissions', () => {
      assert.ok(authCtx.includes('id: string'), 'AuthUser missing id');
      assert.ok(authCtx.includes('username: string'), 'AuthUser missing username');
      assert.ok(authCtx.includes('email: string'), 'AuthUser missing email');
      assert.ok(authCtx.includes('role: string'), 'AuthUser missing role');
      assert.ok(authCtx.includes('permissions: string[]'), 'AuthUser missing permissions');
    });

    it('should implement login(username, password) returning Promise<void>', () => {
      assert.ok(authCtx.includes('login: (username: string, password: string) => Promise<void>'), 'AuthContext missing login signature');
    });

    it('should implement logout()', () => {
      assert.ok(authCtx.includes('logout: () => void'), 'AuthContext missing logout signature');
    });

    it('should implement hasPermission(permission) returning boolean', () => {
      assert.ok(authCtx.includes('hasPermission: (permission: string) => boolean'), 'AuthContext missing hasPermission signature');
    });

    it('should store tokens in localStorage (hcm_token, hcm_refresh_token)', () => {
      assert.ok(authCtx.includes("localStorage.setItem('hcm_token'"), 'AuthContext missing hcm_token storage');
      assert.ok(authCtx.includes("localStorage.setItem('hcm_refresh_token'"), 'AuthContext missing hcm_refresh_token storage');
    });

    it('should call /api/auth/me on mount to verify session', () => {
      assert.ok(authCtx.includes('/api/auth/me'), 'AuthContext missing session verification');
    });

    it('should call /api/auth/login for authentication', () => {
      assert.ok(authCtx.includes('/api/auth/login'), 'AuthContext missing login endpoint');
    });

    it('should implement RBAC: admin role gets all permissions', () => {
      assert.ok(authCtx.includes("role === 'admin'"), 'AuthContext missing admin role check');
      assert.ok(authCtx.includes('return true'), 'AuthContext admin should return true for all permissions');
    });

    it('should check user.permissions.includes for non-admin roles', () => {
      assert.ok(authCtx.includes('user.permissions.includes'), 'AuthContext missing permission check');
    });

    it('should redirect to /login on logout', () => {
      assert.ok(authCtx.includes("window.location.href = '/login'"), 'AuthContext missing login redirect on logout');
    });
  });

  describe('API Client (client.ts)', () => {
    const apiClient = readSrc('api/client.ts');

    it('should create axios instance with baseURL http://localhost:8000', () => {
      assert.ok(apiClient.includes("baseURL: BASE_URL"), 'Client missing baseURL');
      assert.ok(apiClient.includes("http://localhost:8000"), 'Client missing correct base URL');
    });

    it('should have request interceptor that injects JWT from localStorage', () => {
      assert.ok(apiClient.includes('interceptors.request.use'), 'Client missing request interceptor');
      assert.ok(apiClient.includes("localStorage.getItem('hcm_token')"), 'Client not reading hcm_token');
      assert.ok(apiClient.includes('Authorization'), 'Client not setting Authorization header');
      assert.ok(apiClient.includes('Bearer'), 'Client not using Bearer scheme');
    });

    it('should have response interceptor for 401 handling', () => {
      assert.ok(apiClient.includes('interceptors.response.use'), 'Client missing response interceptor');
      assert.ok(apiClient.includes('401'), 'Client not handling 401');
    });

    it('should implement token refresh on 401', () => {
      assert.ok(apiClient.includes('hcm_refresh_token'), 'Client not reading refresh token');
      assert.ok(apiClient.includes('/api/auth/refresh'), 'Client missing refresh endpoint');
      assert.ok(apiClient.includes('_retry'), 'Client missing retry flag to prevent loops');
    });

    it('should redirect to /login when refresh fails', () => {
      assert.ok(apiClient.includes("window.location.href = '/login'"), 'Client missing redirect on refresh failure');
    });

    it('should have 30-second timeout', () => {
      assert.ok(apiClient.includes('timeout: 30000'), 'Client timeout should be 30s');
    });
  });

  describe('main.tsx Provider Hierarchy', () => {
    const main = readSrc('main.tsx');

    it('should wrap App with BrowserRouter > ThemeProvider > AuthProvider > SymbolProvider', () => {
      assert.ok(main.includes('BrowserRouter'), 'main.tsx missing BrowserRouter');
      assert.ok(main.includes('ThemeProvider'), 'main.tsx missing ThemeProvider');
      assert.ok(main.includes('AuthProvider'), 'main.tsx missing AuthProvider');
      assert.ok(main.includes('SymbolProvider'), 'main.tsx missing SymbolProvider');
    });

    it('should use dark theme', () => {
      assert.ok(main.includes("mode: 'dark'"), 'Theme should be dark mode');
    });

    it('should include CssBaseline for reset', () => {
      assert.ok(main.includes('CssBaseline'), 'main.tsx missing CssBaseline');
    });
  });
});

// ============================================================
// AREA 4: Symbol Linkage
// ============================================================
describe('AREA 4 — Symbol Linkage', () => {
  const symCtx = readSrc('contexts/SymbolContext.tsx');

  it('should export SymbolProvider and useSymbol hook', () => {
    assert.ok(symCtx.includes('export const SymbolProvider'), 'SymbolContext missing SymbolProvider export');
    assert.ok(symCtx.includes('export const useSymbol'), 'SymbolContext missing useSymbol export');
  });

  it('should define SymbolInfo with symbol, name, exchange, category, pipSize, contractSize', () => {
    assert.ok(symCtx.includes('symbol: string'), 'SymbolInfo missing symbol');
    assert.ok(symCtx.includes('exchange: string'), 'SymbolInfo missing exchange');
    assert.ok(symCtx.includes('category: string'), 'SymbolInfo missing category');
    assert.ok(symCtx.includes('pipSize: number'), 'SymbolInfo missing pipSize');
    assert.ok(symCtx.includes('contractSize: number'), 'SymbolInfo missing contractSize');
  });

  it('should include 6 default symbols (XAUUSD, EURUSD, GBPUSD, USDJPY, US30, NAS100)', () => {
    const defaultSymbols = ['XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'US30', 'NAS100'];
    for (const sym of defaultSymbols) {
      assert.ok(symCtx.includes(sym), `Default symbols missing: ${sym}`);
    }
  });

  it('should dispatch CustomEvent symbolChanged when symbol changes', () => {
    assert.ok(symCtx.includes('CustomEvent'), 'SymbolContext missing CustomEvent');
    assert.ok(symCtx.includes("'symbolChanged'"), 'SymbolContext missing symbolChanged event name');
    assert.ok(symCtx.includes('window.dispatchEvent'), 'SymbolContext missing dispatchEvent call');
  });

  it('should pass symbol detail in the CustomEvent', () => {
    assert.ok(symCtx.includes('detail: symbol'), 'SymbolContext should include symbol in event detail');
  });

  // Verify dashboard pages listen to symbolChanged
  describe('Dashboard pages listen to symbolChanged', () => {
    // [2026-08-28] 移除 'Realtime.tsx'、'Statistics.tsx'（页面下线，文件已删除）
    const dashboardPages = [
      'Positions.tsx',
      'Factors.tsx',
    ];

    for (const page of dashboardPages) {
      it(`${page} should listen to symbolChanged event`, () => {
        const content = readSrc(`pages/dashboard/${page}`);
        assert.ok(
          content.includes('symbolChanged'),
          `${page} missing symbolChanged event listener`,
        );
      });
    }
  });

  // 【2026-08-28】移除 Realtime.tsx 断言（实时信号流页已下线）
});

// ============================================================
// AREA 5: Dashboard Chart Libraries
// ============================================================
describe('AREA 5 — Dashboard Chart Libraries', () => {
  // 【2026-08-28】移除 Realtime.tsx — AG Grid 描述块（实时信号流页已下线）

  describe('Positions.tsx — ECharts', () => {
    const positions = readSrc('pages/dashboard/Positions.tsx');

    it('should import ReactEChartsCore from echarts-for-react', () => {
      assert.ok(positions.includes('echarts-for-react'), 'Positions missing echarts-for-react import');
      assert.ok(positions.includes('ReactEChartsCore'), 'Positions missing ReactEChartsCore usage');
    });

    it('should register LineChart and BarChart components', () => {
      assert.ok(positions.includes('LineChart'), 'Positions missing LineChart');
      assert.ok(positions.includes('BarChart'), 'Positions missing BarChart');
    });

    it('should display equity curve and positions table', () => {
      assert.ok(positions.includes('净值曲线'), 'Positions missing equity curve');
      assert.ok(positions.includes('持仓列表'), 'Positions missing positions table');
    });
  });

  // 【2026-08-28】移除 Statistics.tsx — ECharts 描述块（统计分析页已下线）

  describe('Health.tsx — Service Cards', () => {
    const health = readSrc('pages/dashboard/Health.tsx');

    it('should handle 4 service states: healthy, degraded, down, starting', () => {
      for (const state of ['healthy', 'degraded', 'down', 'starting']) {
        assert.ok(health.includes(state), `Health missing state: ${state}`);
      }
    });

    it('should poll /api/health/detailed every 15 seconds', () => {
      assert.ok(health.includes('/api/health/detailed'), 'Health missing detailed endpoint');
      assert.ok(health.includes('15000'), 'Health should poll every 15s');
    });

    it('should compute overall health status', () => {
      assert.ok(health.includes('overallHealth'), 'Health missing overallHealth function');
    });
  });

  describe('Factors.tsx — ECharts', () => {
    const factors = readSrc('pages/dashboard/Factors.tsx');

    it('should register LineChart for factor trends', () => {
      assert.ok(factors.includes('LineChart'), 'Factors missing LineChart');
    });

    it('should use KpiCard for factor scores', () => {
      assert.ok(factors.includes('KpiCard'), 'Factors missing KpiCard');
    });

    it('should refresh every 30 seconds', () => {
      assert.ok(factors.includes('30000'), 'Factors should refresh every 30s');
    });
  });

  // [2026-08-28] 移除 Compare.tsx — ECharts Heatmap 用例（品种对比页下线）
});

// ============================================================
// AREA 6: WebSocket Hook
// ============================================================
describe('AREA 6 — WebSocket Hook', () => {
  const ws = readSrc('hooks/useWebSocket.ts');

  it('should export useWebSocket function', () => {
    assert.ok(ws.includes('export function useWebSocket'), 'useWebSocket missing export');
  });

  it('should accept symbol and options parameters', () => {
    assert.ok(ws.includes('symbol: string'), 'useWebSocket missing symbol parameter');
    assert.ok(ws.includes('options: UseWebSocketOptions'), 'useWebSocket missing options parameter');
  });

  it('should return isConnected, lastMessage, sendMessage, reconnect', () => {
    assert.ok(ws.includes('isConnected'), 'useWebSocket return missing isConnected');
    assert.ok(ws.includes('lastMessage'), 'useWebSocket return missing lastMessage');
    assert.ok(ws.includes('sendMessage'), 'useWebSocket return missing sendMessage');
    assert.ok(ws.includes('reconnect'), 'useWebSocket return missing reconnect');
  });

  it('should auto-reconnect on close with exponential backoff', () => {
    assert.ok(ws.includes('retriesRef'), 'useWebSocket missing retries counter');
    assert.ok(ws.includes('maxRetries'), 'useWebSocket missing maxRetries');
    assert.ok(ws.includes('reconnectInterval'), 'useWebSocket missing reconnectInterval');
    assert.ok(ws.includes('setTimeout'), 'useWebSocket missing reconnect timer');
  });

  it('should include JWT token in WebSocket URL', () => {
    assert.ok(ws.includes("localStorage.getItem('hcm_token')"), 'useWebSocket missing token read');
    assert.ok(ws.includes('token='), 'useWebSocket not passing token in URL');
  });

  it('should parse incoming JSON messages', () => {
    assert.ok(ws.includes('JSON.parse'), 'useWebSocket missing JSON parse');
    assert.ok(ws.includes('event.data'), 'useWebSocket not reading event data');
  });

  it('should handle parse errors gracefully', () => {
    assert.ok(ws.includes('Failed to parse message'), 'useWebSocket missing parse error handling');
  });

  it('should cleanup WebSocket on unmount', () => {
    assert.ok(ws.includes('clearTimeout'), 'useWebSocket missing timer cleanup');
    assert.ok(ws.includes('wsRef.current.close()'), 'useWebSocket not closing WS on unmount');
  });

  it('should cap retries at maxRetries (default 10)', () => {
    assert.ok(ws.includes('maxRetries = 10'), 'useWebSocket default maxRetries should be 10');
  });

  it('should default reconnectInterval to 3000ms', () => {
    assert.ok(ws.includes('reconnectInterval = 3000'), 'useWebSocket default reconnectInterval should be 3000');
  });

  it('should connect to ws://localhost:8000/ws', () => {
    assert.ok(ws.includes('ws://localhost:8000/ws'), 'useWebSocket wrong WS base URL');
  });

  it('should call onConnect callback when connection opens', () => {
    assert.ok(ws.includes('onConnect?.()'), 'useWebSocket missing onConnect callback');
  });

  it('should call onDisconnect callback when connection closes', () => {
    assert.ok(ws.includes('onDisconnect?.()'), 'useWebSocket missing onDisconnect callback');
  });

  it('should call onMessage callback with parsed message', () => {
    assert.ok(ws.includes('onMessage?.(msg)'), 'useWebSocket missing onMessage callback');
  });

  it('should define WebSocketMessage interface with type, symbol, payload, timestamp', () => {
    assert.ok(ws.includes('type: string'), 'WebSocketMessage missing type');
    assert.ok(ws.includes('symbol: string'), 'WebSocketMessage missing symbol');
    assert.ok(ws.includes('payload: Record<string, unknown>'), 'WebSocketMessage missing payload');
    assert.ok(ws.includes('timestamp: number'), 'WebSocketMessage missing timestamp');
  });
});

// ============================================================
// AREA 7: File Completeness & Config
// ============================================================
describe('AREA 7 — File Completeness & Configuration', () => {
  it('all expected page files exist', () => {
    const pages = [
      'pages/Login.tsx',
      'pages/system/Users.tsx', 'pages/system/Network.tsx', 'pages/system/MT5.tsx',
      'pages/system/DeepSeek.tsx', 'pages/system/Notifications.tsx', 'pages/system/Cache.tsx',
      // [2026-08-28] 移除 'pages/signaltower/Threshold.tsx'（评分阈值页下线）
      'pages/signaltower/Mode.tsx', 'pages/signaltower/Prompt.tsx',
      // [2026-08-28] 移除 'pages/signaltower/SymbolConfig.tsx'（品种级配置页下线）
      'pages/signaltower/Watchdog.tsx',
      'pages/datasource/DatasourceList.tsx',
      'pages/engine/EngineRules.tsx',
      // [2026-08-28] 移除 'pages/dispatch/DispatchConfig.tsx'（分发配置页下线）
      'pages/risk/RiskConfig.tsx',
      'pages/close/CloseConfig.tsx',
      // [2026-08-28] 'pages/copy/CopyConfig.tsx' → 'CopyLayout.tsx'（实际入口文件，原期望已过时）
      'pages/copy/CopyLayout.tsx',
      // [2026-08-28] 移除 'pages/dashboard/Realtime.tsx'、'pages/dashboard/Statistics.tsx'
      // [2026-08-28] 移除 'pages/dashboard/Compare.tsx'（品种对比页下线）
      'pages/dashboard/Positions.tsx', 'pages/dashboard/Health.tsx',
      'pages/dashboard/Factors.tsx',
    ];
    for (const page of pages) {
      assert.ok(fileExists(page), `Missing page file: ${page}`);
    }
  });

  it('package.json has all required dependencies', () => {
    const pkg = JSON.parse(readFileSync(join(SRC, '..', 'package.json'), 'utf-8'));
    const deps = { ...pkg.dependencies, ...pkg.devDependencies };

    const required = [
      'react', 'react-dom', 'react-router-dom',
      '@mui/material', '@mui/icons-material', '@emotion/react', '@emotion/styled',
      'axios', 'ag-grid-community', 'ag-grid-react',
      'echarts', 'echarts-for-react',
      'typescript', 'vite', '@vitejs/plugin-react',
      'tailwindcss',
    ];

    for (const dep of required) {
      assert.ok(deps[dep], `Missing dependency: ${dep}`);
    }
  });

  it('vite.config.ts should proxy /api and /ws to localhost:8000', () => {
    const vite = readFileSync(join(SRC, '..', 'vite.config.ts'), 'utf-8');
    assert.ok(vite.includes("'/api'"), 'vite.config missing /api proxy');
    assert.ok(vite.includes("target: 'http://localhost:8000'"), 'vite.config wrong proxy target');
    assert.ok(vite.includes("'/ws'"), 'vite.config missing /ws proxy');
    assert.ok(vite.includes("ws: true"), 'vite.config missing WebSocket proxy');
  });

  it('index.html exists and has root div', () => {
    const html = readFileSync(join(SRC, '..', 'index.html'), 'utf-8');
    assert.ok(html.includes('id="root"'), 'index.html missing root div');
    assert.ok(html.includes('script'), 'index.html should load script');
  });

  it('index.css has Tailwind directives and dark theme variables', () => {
    const css = readSrc('index.css');
    assert.ok(css.includes('@tailwind'), 'index.css missing @tailwind directive');
    assert.ok(css.includes('--color-bg-primary'), 'index.css missing CSS variables');
  });
});

// ============================================================
// AREA 8: ConfigForm Usage in Pages (Bonus)
// ============================================================
describe('AREA 8 — ConfigForm Usage in Config Pages', () => {
  const configPages = [
    { file: 'pages/system/DeepSeek.tsx', title: 'DeepSeek AI' },
    { file: 'pages/system/MT5.tsx', title: 'MT5 接入' },
    // 【2026-08-28】移除 pages/signaltower/Threshold.tsx（评分阈值公用参数页已下线）
  ];

  for (const { file, title } of configPages) {
    it(`${file} should use ConfigForm component`, () => {
      const content = readSrc(file);
      assert.ok(content.includes('ConfigForm'), `${file} missing ConfigForm import`);
    });
  }

  it('DeepSeek.tsx should have 10 config fields', () => {
    const deepseek = readSrc('pages/system/DeepSeek.tsx');
    // Count field definitions
    const fieldCount = (deepseek.match(/key: '/g) || []).length;
    assert.equal(fieldCount, 10, `DeepSeek expected 10 fields, found ${fieldCount}`);
  });

  it('MT5.tsx should have 9 config fields', () => {
    const mt5 = readSrc('pages/system/MT5.tsx');
    const fieldCount = (mt5.match(/key: '/g) || []).length;
    assert.equal(fieldCount, 9, `MT5 expected 9 fields, found ${fieldCount}`);
  });

  // 【2026-08-28】移除 Threshold.tsx 的 useSymbol 断言（页面已下线）
});

console.log('\n✅ All test suites loaded. Run with: node --test tests/qa-validation.test.mjs\n');
