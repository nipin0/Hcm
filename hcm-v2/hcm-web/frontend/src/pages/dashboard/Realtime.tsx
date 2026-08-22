import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Box, Typography, Chip, ToggleButton, ToggleButtonGroup } from '@mui/material';
import { AgGridReact } from 'ag-grid-react';
import { ColDef, GridReadyEvent } from 'ag-grid-community';
import 'ag-grid-community/styles/ag-grid.css';
import 'ag-grid-community/styles/ag-theme-alpine.css';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';
import { useSymbol } from '../../contexts/SymbolContext';
import { useWebSocket, WebSocketMessage } from '../../hooks/useWebSocket';
import ReversalWatchCard from '../../components/dashboard/ReversalWatchCard';

/** Signal row matching backend GET /api/dashboard/realtime response */
interface SignalRow {
  signal_id: number;
  account_id: number;
  symbol: string;
  time_frame: string;
  signal_dir: string;
  entry_price: number;
  sl_price: number | null;
  tp1: number | null;
  tp2: number | null;
  lot: number;
  confidence: number;
  signal_status: number; // 0=pending, 1=triggered, 2=executed, 3=rejected
  regime: string;
  pre_score: number;
  reason: string;
  created_at: string;
}

/** Map integer signal_status to Chinese label */
const STATUS_LABELS: Record<number, string> = {
  0: '待触发',
  1: '已触发',
  2: '已执行',
  3: '已拒绝',
};

/** Map integer signal_status to display color */
const STATUS_COLORS: Record<number, string> = {
  0: '#eab308',
  1: '#3b82f6',
  2: '#22c55e',
  3: '#ef4444',
};

/** Format ISO timestamp to relative time string (e.g. "2h前", "5m前", "刚刚") */
function relativeTime(isoStr: string): string {
  const now = Date.now();
  const then = new Date(isoStr).getTime();
  if (isNaN(then)) return '';
  const diffMs = now - then;
  if (diffMs < 60_000) return '刚刚';
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 60) return `${mins}m前`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h前`;
  const days = Math.floor(hours / 24);
  return `${days}d前`;
}

type FilterMode = 'recent30' | 'active';

const Realtime: React.FC = () => {
  const { selectedSymbol } = useSymbol();
  const [rowData, setRowData] = useState<SignalRow[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  // Filter state
  const [filterModes, setFilterModes] = useState<string[]>(['recent30']);
  const [cleared, setCleared] = useState<boolean>(false);

  const handleFilterChange = useCallback(
    (_event: React.MouseEvent<HTMLElement>, newModes: string[] | null) => {
      // If user clicks "清除显示", toggle cleared mode
      if (newModes === null) {
        setFilterModes([]);
        return;
      }
      // Check if the "clear" toggle was activated
      if (newModes.includes('clear')) {
        setFilterModes(newModes.filter((m) => m !== 'clear'));
        setCleared(true);
        return;
      }
      setFilterModes(newModes);
    },
    [],
  );

  const handleClearClick = useCallback(async () => {
    if (cleared) {
      // Re-fetch data
      setCleared(false);
      if (selectedSymbol) {
        setLoading(true);
        try {
          const { data } = await client.get(
            `${ENDPOINTS.dashboard.realtime}?symbol=${selectedSymbol.symbol}&timeframe=M5`,
          );
          setRowData((data.data || data).signals || []);
        } catch {
          setRowData([]);
        } finally {
          setLoading(false);
        }
      }
    } else {
      setRowData([]);
      setCleared(true);
    }
  }, [cleared, selectedSymbol]);

  // --- Fetch initial data ---
  const fetchSignals = useCallback(async (): Promise<void> => {
    if (!selectedSymbol) return;
    setLoading(true);
    try {
      const { data } = await client.get(
        `${ENDPOINTS.dashboard.realtime}?symbol=${selectedSymbol.symbol}&timeframe=M5`,
      );
      setRowData((data.data || data).signals || []);
    } catch {
      setRowData([]);
    } finally {
      setLoading(false);
    }
  }, [selectedSymbol]);

  useEffect(() => {
    fetchSignals();
    const handler = (): void => {
      fetchSignals();
    };
    window.addEventListener('symbolChanged', handler);
    return () => window.removeEventListener('symbolChanged', handler);
  }, [fetchSignals]);

  // --- WebSocket for incremental updates ---
  const handleWsMessage = useCallback((msg: WebSocketMessage): void => {
    if (msg.type === 'signal_update' || msg.type === 'new_signal') {
      setRowData((prev) => {
        const signal = msg.payload as unknown as SignalRow;
        const existingIdx = prev.findIndex((r) => r.signal_id === signal.signal_id);
        if (existingIdx >= 0) {
          const updated = [...prev];
          updated[existingIdx] = signal;
          return updated;
        }
        return [signal, ...prev].slice(0, 500); // Keep max 500 rows
      });
    }
    setCleared(false); // ws message re-populates
  }, []);

  const { isConnected } = useWebSocket(
    selectedSymbol?.symbol || 'XAUUSD',
    { onMessage: handleWsMessage },
  );

  // --- Filtered row data ---
  const filteredRowData = useMemo<SignalRow[]>(() => {
    if (cleared) return [];
    let result = [...rowData];
    if (filterModes.includes('recent30')) {
      result = result.slice(0, 30);
    }
    if (filterModes.includes('active')) {
      result = result.filter(
        (r) => r.signal_status === 0 || r.signal_status === 1,
      );
    }
    return result;
  }, [rowData, filterModes, cleared]);

  // --- Column definitions ---
  const columnDefs = useMemo<ColDef<SignalRow>[]>(() => [
    {
      field: 'signal_id',
      headerName: '信号ID',
      width: 80,
      sort: 'desc',
    },
    {
      field: 'created_at',
      headerName: '时间',
      width: 160,
      sort: 'desc',
      valueFormatter: (params) => {
        if (!params.value) return '';
        return new Date(params.value).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai' });
      },
    },
    { field: 'symbol', headerName: '品种', width: 90, cellClass: 'font-medium' },
    {
      field: 'signal_dir',
      headerName: '方向',
      width: 80,
      cellRenderer: (params: { value: string }) => {
        if (!params.value) return null;
        const isLong =
          params.value === 'BUY' || params.value === 'LONG';
        return (
          <Chip
            label={params.value}
            size="small"
            sx={{
              backgroundColor: isLong ? '#0a2e1a' : '#3b1111',
              color: isLong ? '#22c55e' : '#ef4444',
              fontWeight: 600,
              fontSize: 11,
              height: 22,
            }}
          />
        );
      },
    },
    {
      field: 'pre_score',
      headerName: '预评分',
      width: 90,
      cellRenderer: (params: { value: number }) => {
        if (params.value === undefined || params.value === null) return null;
        // 共源引擎 pre_score 为 0-1 尺度（非旧 AI 的 0-100）
        const v = params.value;
        const color = v >= 0.4 ? '#22c55e' : v >= 0.3 ? '#eab308' : '#ef4444';
        return (
          <span style={{ color, fontWeight: 600 }}>
            {(v * 100).toFixed(1)}%
          </span>
        );
      },
    },
    {
      field: 'confidence',
      headerName: '置信度',
      width: 100,
      valueFormatter: (params) =>
        params.value ? `${(params.value * 100).toFixed(1)}%` : '',
    },
    {
      field: 'regime',
      headerName: '市况',
      width: 90,
      cellRenderer: (params: { value: string }) => {
        if (!params.value) return null;
        return (
          <Chip
            label={params.value}
            size="small"
            variant="outlined"
            sx={{
              fontSize: 10,
              height: 20,
              borderColor: '#4b5563',
              color: '#94a3b8',
            }}
          />
        );
      },
    },
    {
      field: 'signal_status',
      headerName: '状态',
      width: 90,
      cellRenderer: (params: { value: number }) => {
        if (params.value === undefined || params.value === null) return null;
        const label = STATUS_LABELS[params.value] || String(params.value);
        const color = STATUS_COLORS[params.value] || '#94a3b8';
        return (
          <Chip
            label={label}
            size="small"
            sx={{
              backgroundColor: `${color}22`,
              color,
              fontSize: 10,
              height: 20,
            }}
          />
        );
      },
    },
    {
      field: 'entry_price',
      headerName: '入场价',
      width: 90,
      valueFormatter: (params) =>
        params.value != null ? params.value.toFixed(2) : '',
    },
    {
      field: 'lot',
      headerName: '手数',
      width: 70,
      valueFormatter: (params) =>
        params.value != null ? params.value.toFixed(2) : '',
    },
    {
      headerName: '更新时间',
      width: 90,
      valueGetter: (params) => params.data?.created_at || '',
      cellRenderer: (params: { value: string }) => {
        if (!params.value) return null;
        return (
          <span style={{ fontSize: 11, color: '#94a3b8' }}>
            {relativeTime(params.value)}
          </span>
        );
      },
    },
    {
      field: 'reason',
      headerName: '原因',
      flex: 1,
      minWidth: 150,
      cellClass: 'text-gray-400 text-xs',
    },
  ], []);

  const defaultColDef = useMemo(
    () => ({
      resizable: true,
      sortable: true,
      filter: false,
    }),
    [],
  );

  const onGridReady = useCallback((_params: GridReadyEvent): void => {
    // Grid ready
  }, []);

  return (
    <Box>
      <Box className="flex items-center justify-between mb-4">
        <Box>
          <Typography variant="h6" className="text-gray-100 font-semibold">
            实时信号流
          </Typography>
          <Typography variant="caption" className="text-gray-500">
            {selectedSymbol?.symbol} · {filteredRowData.length}
            {filteredRowData.length !== rowData.length
              ? ` / ${rowData.length}`
              : ''}{' '}
            条信号 · WS: {isConnected ? '已连接' : '未连接'}
          </Typography>
        </Box>
      </Box>

      <ReversalWatchCard />

      {/* Filter controls */}
      <Box className="flex items-center gap-2 mb-3 flex-wrap">
        <ToggleButtonGroup
          value={filterModes}
          onChange={handleFilterChange}
          size="small"
          sx={{
            '& .MuiToggleButton-root': {
              color: '#94a3b8',
              borderColor: '#4b5563',
              fontSize: 11,
              px: 1.5,
              py: 0.25,
              textTransform: 'none',
              '&.Mui-selected': {
                color: '#e2e8f0',
                backgroundColor: '#1e293b',
              },
            },
          }}
        >
          <ToggleButton value="recent30">仅显示最近 30 条</ToggleButton>
          <ToggleButton value="active">仅显示 pending+triggered</ToggleButton>
        </ToggleButtonGroup>
        <Chip
          label={cleared ? '恢复显示' : '清除显示'}
          size="small"
          onClick={handleClearClick}
          sx={{
            backgroundColor: cleared ? '#0a2e1a' : '#1e293b',
            color: cleared ? '#22c55e' : '#94a3b8',
            borderColor: '#4b5563',
            fontSize: 11,
            height: 26,
            cursor: 'pointer',
          }}
        />
      </Box>

      <Box
          className="ag-theme-alpine-dark rounded-xl overflow-hidden border border-gray-700"
          style={{ height: 'calc(100vh - 310px)', width: '100%' }}
        >
          <AgGridReact<SignalRow>
            rowData={filteredRowData}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            onGridReady={onGridReady}
            loading={loading}
            rowHeight={36}
            headerHeight={40}
            animateRows
            enableCellTextSelection
            suppressCellFocus
            overlayNoRowsTemplate="<span class='text-gray-500'>暂无信号数据</span>"
            overlayLoadingTemplate="<span class='text-gray-500'>加载中...</span>"
          />
        </Box>
    </Box>
  );
};

export default Realtime;
