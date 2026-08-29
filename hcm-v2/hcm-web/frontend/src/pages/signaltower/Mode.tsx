/** 信号模式与市况判定 — 三种信号模式独立配置面板，带激活状态指示 + 切换确认.
 *
 * Tab 0: 手动模式 — 镜像主账户所有动作
 * Tab 1: 和乘幂 — 独立信号源（hexp 引擎：HP-Score 广义均值 + k 自适应 + 多周期共振 + M1 微结构动量）
 * （五维 AI 动态判定 ai_dynamic 已于 2026-07-24 弃用）
 * （双源信号 co_source 已于 2026-08-28 整体下线，原 Tab 1 移除，和乘幂由 2 降为 1）
 */
import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Box, Typography, Button, Chip, LinearProgress, Alert, Tab, Tabs,
  Dialog, DialogTitle, DialogContent, DialogContentText, DialogActions,
} from '@mui/material';
import { Save, RefreshCw, Psychology, Tune, Hub, CheckCircle } from '../../components/Icons';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';
import { useSymbol } from '../../contexts/SymbolContext';
import { useAuth } from '../../contexts/AuthContext';
import SaveGuardDialog, { SaveChange } from '../../components/SaveGuardDialog';
import HexpConfig from '../hexp/HexpConfig';

interface RegimeInfo {
  regime: string;
  confidence: number;
  ai_score: number;
  manual_score: number;
  last_updated: string;
  indicators: Record<string, number>;
}

// 【2026-08-28 co_source 清除】双源信号模式整体下线，只保留「手动模式 / 和乘幂」。
// 原 MODE_LABELS = ['手动模式', '双源信号', '和乘幂']（索引 1 = 双源，对应 CoSourceConfig）。
const MODE_LABELS = ['手动模式', '和乘幂'];
const MODE_ICONS = [Tune, Psychology];

const Mode: React.FC = () => {
  const { selectedSymbol } = useSymbol();
  const [tabValue, setTabValue] = useState<number>(0);       // UI 选中 Tab（可能与 activeMode 不同）
  const [activeMode, setActiveMode] = useState<number>(0);    // 后端真实激活的模式
  const [pendingTab, setPendingTab] = useState<number | null>(null); // 待确认切换的目标 Tab
  const [regime, setRegime] = useState<RegimeInfo | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);
  const [switching, setSwitching] = useState<boolean>(false);
  const [manualScore, setManualScore] = useState<number>(50);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const { user } = useAuth();
  // 保存守卫：打开密码鉴权弹窗时记录待执行动作 + 变更 diff
  const [guard, setGuard] = useState<{
    title: string;
    description?: string;
    changes: SaveChange[];
    action: () => Promise<void>;
  } | null>(null);

  // ── 从后端读取当前激活的模式 ──
  const fetchActiveMode = useCallback(async (): Promise<number> => {
    try {
      const [{ data: engineModeResp }, { data: modeResp }] = await Promise.all([
        client.get(ENDPOINTS.engineMode.config),
        client.get('/api/signal-tower/mode?symbol=' + (selectedSymbol?.symbol || 'XAUUSD')),
      ]);
      const engineModeData = engineModeResp.data || engineModeResp;
      const modeData = modeResp.data || modeResp;

      const coModel = String(engineModeData['signal.active_model'] || 'default');
      const stMode = String(modeData.mode || 'manual');

      // 【2026-08-28 co_source 清除】Tab 索引重映射：0=manual, 1=hexp
      // （原 0=manual, 1=co_source, 2=hexp；双源下线后 hexp 由 2 降为 1）。
      if (stMode === 'manual') return 0;
      if (coModel === 'hexp') return 1;
      // 后端已无 co_source 分支（_detect_active_model 只返 hexp/manual），
      // 其余一律按 hexp 呈现（索引 1），避免 MODE_LABELS[2] 越界为 undefined。
      return 1;
    } catch {
      return 0;
    }
  }, [selectedSymbol]);

  const fetchRegime = useCallback(async (): Promise<void> => {
    if (!selectedSymbol) return;
    setLoading(true);
    try {
      const [{ data: modeData }, mode] = await Promise.all([
        client.get(`/api/signal-tower/mode?symbol=${selectedSymbol.symbol}`),
        fetchActiveMode(),
      ]);
      const raw = modeData.data || modeData;
      const normalized: RegimeInfo = {
        regime: raw.current_regime || raw.regime || 'neutral',
        confidence: raw.regime_confidence ?? raw.confidence ?? 0,
        ai_score: raw.ai_score ?? 50,
        manual_score: raw.manual_score ?? 50,
        last_updated: raw.last_updated || '-',
        indicators: raw.indicators || {},
      };
      setRegime(normalized);
      setManualScore(normalized.manual_score);
      setActiveMode(mode);
      setTabValue(mode); // 同步 UI Tab 到真实激活模式
    } catch {
      setRegime(null);
    } finally {
      setLoading(false);
    }
  }, [selectedSymbol, fetchActiveMode]);

  useEffect(() => {
    fetchRegime();
    const handler = (): void => { fetchRegime(); };
    window.addEventListener('symbolChanged', handler);
    return () => window.removeEventListener('symbolChanged', handler);
  }, [fetchRegime]);

  // ── Tab 点击 → 弹出确认对话框 ──
  const handleTabClick = (_: any, v: number) => {
    if (v === activeMode) return; // 同一模式，无需切换
    setPendingTab(v);
  };

  // ── 模式切换：真实写入（由密码守卫弹窗在鉴权通过后调用）──
  const performSwitch = async (target: number): Promise<void> => {
    setSwitching(true);
    try {
      // 【2026-08-28 co_source 清除】索引重映射：0=manual, 1=hexp（原 1=双源、2=和乘幂）。
      // 修复：此前 target!==0 一律写 'co_source'，导致用户点「和乘幂」时把
      // signal.active_model 写成已删除的引擎名 → 后端找不到 co_source 分支。
      // 写 signal_tower.mode：signal_tower.mode 仅区分手动镜像与自动路由，
      // 非手动即 'hexp'（和乘幂是唯一自动引擎）。
      const stMode = target === 0 ? 'manual' : 'hexp';
      await client.put('/api/signal-tower/mode', {
        symbol: selectedSymbol?.symbol || 'XAUUSD',
        mode: stMode,
        manual_regime_score: manualScore,
      });
      // 写 signal.active_model：0=manual → 'default'；1=hexp → 'hexp'。不再出现 co_source。
      await client.put(ENDPOINTS.engineMode.config, {
        'signal.active_model': target === 0 ? 'default' : 'hexp',
      });
      // 触发管线：下发激活指令让调度器立即重置并重新检测 active_model，
      // 使引擎 status 的 mode_switched_at 即时更新、按新模式恢复信号生产。
      try {
        await client.post(ENDPOINTS.system.activate);
      } catch {
        /* 激活为增强动作，失败不影响模式配置本身 */
      }
      setMessage({ type: 'success', text: `已切换至「${MODE_LABELS[target]}」模式，已触发信号管线` });
      await fetchRegime(); // 刷新 activeMode 并同步 UI
    } catch {
      setMessage({ type: 'error', text: '模式切换失败，请重试' });
    } finally {
      setSwitching(false);
    }
  };

  // 点击「确认切换」→ 打开密码鉴权弹窗（保留待切换目标，关闭原确认框）
  const openSwitchGuard = (): void => {
    const target = pendingTab;
    if (target === null) return;
    setGuard({
      title: '确认切换信号模式',
      description: `将信号模式从「${MODE_LABELS[activeMode]}」切换为「${MODE_LABELS[target]}」。切换立即生效，信号塔将在下一 bar 使用新模式。`,
      changes: [{ label: '激活信号模式', oldValue: MODE_LABELS[activeMode], newValue: MODE_LABELS[target] }],
      action: () => performSwitch(target),
    });
    setPendingTab(null); // 关闭切换确认框，改由密码守卫接管
  };

  const handleCancelSwitch = () => {
    setPendingTab(null);
  };

  // ── 市况配置保存：真实写入（由密码守卫弹窗在鉴权通过后调用）──
  const performSave = async (): Promise<void> => {
    if (!selectedSymbol) return;
    setSaving(true);
    setMessage(null);
    try {
      await client.put('/api/signal-tower/mode', {
        symbol: selectedSymbol.symbol,
        // 【2026-08-28 co_source 清除】双源下线，非 manual 即 hexp
        mode: activeMode === 0 ? 'manual' : 'hexp',
        manual_regime_score: manualScore,
      });
      setMessage({ type: 'success', text: '手动模式配置已保存' });
      fetchRegime();
    } catch {
      setMessage({ type: 'error', text: '保存失败，请重试' });
    } finally {
      setSaving(false);
    }
  };

  // 点击「保存配置」→ 打开密码鉴权弹窗
  const openSaveGuard = (): void => {
    setGuard({
      title: '确认保存市况配置',
      description: '将保存当前品种的信号模式与市况判定参数到配置中心（PG↔Redis）。请确认无误后输入密码授权。',
      changes: [
        { label: '信号模式', oldValue: MODE_LABELS[activeMode], newValue: MODE_LABELS[0] },
        { label: '市况判定分数', oldValue: regime?.manual_score, newValue: manualScore },
      ],
      action: performSave,
    });
  };

  const regimeColor = (regimeName: string): string => {
    switch (regimeName) {
      case 'trending': return '#22c55e';
      case 'ranging': return '#eab308';
      case 'volatile': return '#ef4444';
      case 'breakout': return '#3b82f6';
      default: return '#94a3b8';
    }
  };

  const regimeLabel = (regimeName: string): string => {
    switch (regimeName) {
      case 'trending': return '趋势市';
      case 'ranging': return '震荡市';
      case 'volatile': return '高波动市';
      case 'breakout': return '突破市';
      default: return regimeName;
    }
  };

  return (
    <Box>
      {/* ── 标题 + 激活模式状态条 ── */}
      <Typography variant="h6" className="text-gray-100 font-semibold mb-2">
        信号模式与市况判定
      </Typography>
      <Typography variant="body2" className="text-gray-500 mb-3">
        当前品种: <span className="text-blue-400 font-medium">{selectedSymbol?.symbol}</span>
        — 三种信号模式独立配置
      </Typography>

      {/* 当前激活模式指示器 */}
      <Box
        sx={{
          display: 'flex', alignItems: 'center', gap: 1.5, mb: 3,
          p: 1.5, borderRadius: 2,
          backgroundColor: '#0a2e1a', border: '1px solid #22c55e44',
        }}
      >
        <CheckCircle sx={{ fontSize: 18, color: '#22c55e' }} />
        <Typography variant="body2" sx={{ color: '#86efac', fontWeight: 500 }}>
          当前激活: {MODE_LABELS[activeMode]}
        </Typography>
        <Chip
          label={MODE_LABELS[activeMode]}
          size="small"
          sx={{
            ml: 'auto',
            // 【2026-08-28 索引重映射】0=manual, 1=hexp（原 2=hexp 已降为 1）
            backgroundColor: activeMode === 0 ? '#8b5cf622' : '#a855f722',
            color: activeMode === 0 ? '#c4b5fd' : '#c084fc',
            border: `1px solid ${activeMode === 0 ? '#8b5cf644' : '#a855f744'}`,
            fontWeight: 600,
          }}
        />
      </Box>

      {message && (
        <Alert
          severity={message.type}
          sx={{
            mb: 3,
            backgroundColor: message.type === 'success' ? '#0a2e1a' : '#3b1111',
            color: message.type === 'success' ? '#86efac' : '#fca5a5',
          }}
          onClose={() => setMessage(null)}
        >
          {message.text}
        </Alert>
      )}

      {/* ── 信号模式 Tabs ── */}
      <Tabs
        value={tabValue}
        onChange={handleTabClick}
        sx={{
          mb: 3,
          '& .MuiTab-root': { color: '#94a3b8', textTransform: 'none', fontSize: 13 },
          '& .Mui-selected': { color: '#3b82f6' },
          '& .MuiTabs-indicator': { backgroundColor: '#3b82f6' },
        }}
      >
        {MODE_LABELS.map((label, i) => {
          const Icon = MODE_ICONS[i];
          return (
            <Tab
              key={i}
              icon={<Icon sx={{ fontSize: 18 }} />}
              iconPosition="start"
              label={label}
            />
          );
        })}
      </Tabs>

      {/* ── 切换确认对话框 ── */}
      <Dialog open={pendingTab !== null} onClose={handleCancelSwitch} maxWidth="xs" fullWidth
        PaperProps={{ sx: { backgroundColor: '#1a1a24', color: '#e2e8f0', borderRadius: 2 } }}>
        <DialogTitle sx={{ fontSize: 16, fontWeight: 600 }}>
          确认切换信号模式
        </DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ color: '#94a3b8' }}>
            正在将信号模式从「{MODE_LABELS[activeMode]}」切换为「{pendingTab !== null ? MODE_LABELS[pendingTab] : ''}」。
            此操作将立即生效，信号塔将在下一次 bar 周期使用新模式。
          </DialogContentText>
        </DialogContent>
        <DialogActions sx={{ p: 2, gap: 1 }}>
          <Button onClick={handleCancelSwitch} disabled={switching}
            sx={{ color: '#94a3b8', borderColor: '#4b5563' }} variant="outlined">
            取消
          </Button>
          <Button onClick={openSwitchGuard} disabled={switching}
            variant="contained"
            sx={{ backgroundColor: '#22c55e', '&:hover': { backgroundColor: '#16a34a' } }}>
            {switching ? '切换中...' : '确认切换'}
          </Button>
        </DialogActions>
      </Dialog>



      {/* ═══ Tab 0: 手动模式 ═══ */}
      {tabValue === 0 && (
        <Box>
          <Box className="card mb-4 flex items-center gap-3">
            <Chip
              label="手动模式已激活"
              size="medium"
              sx={{
                backgroundColor: '#8b5cf622',
                color: '#8b5cf6',
                fontWeight: 600,
                border: '1px solid #8b5cf644',
              }}
            />
            <Typography variant="body2" className="text-gray-400">
              信号塔每 bar 自动从 Redis 读取主账户交易并镜像到子账户信号流
            </Typography>
          </Box>

          <Button
            variant="contained"
            startIcon={<Save />}
            onClick={openSaveGuard}
            disabled={saving}
            sx={{ backgroundColor: '#3b82f6', '&:hover': { backgroundColor: '#2563eb' } }}
          >
            {saving ? '保存中...' : '保存配置'}
          </Button>
        </Box>
      )}

      {/* 【2026-08-28 co_source 清除】原「Tab 1: 双源信号（CoSourceConfig）」整块删除；
          双源模式已下线，Tab 只剩 0=手动模式 / 1=和乘幂。 */}

      {/* ═══ Tab 1: 和乘幂（独立信号源；HP-Score + k 自适应 + 多周期共振 + 微结构动量）═══
          【2026-08-28 索引重映射】原 tabValue === 2，双源 Tab 移除后和乘幂由索引 2 降为 1；
          漏改会导致「和乘幂参数配置」页永不渲染（Tab 只有 0/1 两个）。 */}
      {tabValue === 1 && (
        <Box>
          <Box className="card mb-4 flex items-center gap-3">
            <Chip
              // 【2026-08-28 索引重映射】hexp 由 2 → 1
              label={activeMode === 1 ? '和乘幂已激活' : '和乘幂未激活（切换即激活）'}
              size="medium"
              sx={{
                backgroundColor: activeMode === 1 ? '#a855f722' : '#64748b22',
                color: activeMode === 1 ? '#c084fc' : '#94a3b8',
                fontWeight: 600,
                border: `1px solid ${activeMode === 1 ? '#a855f744' : '#64748b44'}`,
              }}
            />
            <Typography variant="body2" className="text-gray-400">
              和乘幂独立信号源：HP-Score 广义均值 + 幂指数 k 自适应 + 多周期共振 + M1 微结构动量 + 6 维评分卡分级
            </Typography>
          </Box>
          <HexpConfig embedded />
        </Box>
      )}

      {/* ── 保存守卫：二次确认 + 密码鉴权 ── */}
      <SaveGuardDialog
        open={guard !== null}
        title={guard?.title}
        description={guard?.description}
        changes={guard?.changes || []}
        username={user?.username}
        onClose={() => setGuard(null)}
        onConfirmed={() => guard?.action()}
      />
    </Box>
  );
};

export default Mode;
