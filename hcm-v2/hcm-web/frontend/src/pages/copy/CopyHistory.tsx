import React, { useState, useEffect, useCallback } from 'react';
import {
  Box,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Chip,
  CircularProgress,
  Typography,
  Autocomplete,
  TextField,
  TablePagination,
} from '@mui/material';
import {
  CARD_BG,
  ACCENT,
  TEXT_PRIMARY,
  TEXT_SECONDARY,
  BORDER,
  DARK_BG,
} from './CopyLayout';
import type { CopyTradeLog, CopyRelationship } from '../../types/copy';
import { fetchTradeLogs, fetchRelationships } from '../../api/copy';

/** Status chip color mapping */
const STATUS_CONFIG: Record<string, { color: string; label: string }> = {
  success: { color: '#22c55e', label: '成功' },
  failed: { color: '#ef4444', label: '失败' },
  pending: { color: '#eab308', label: '等待中' },
  timeout: { color: '#f97316', label: '超时' },
};

/** Format PnL with sign and color */
const formatPnl = (pnl?: number): { text: string; color: string } => {
  if (pnl === undefined || pnl === null) return { text: '—', color: TEXT_SECONDARY };
  const text = pnl >= 0 ? `+${pnl.toFixed(2)}` : pnl.toFixed(2);
  const color = pnl > 0 ? '#22c55e' : pnl < 0 ? '#ef4444' : TEXT_SECONDARY;
  return { text, color };
};

/** Format direction label */
const formatDirection = (dir: string): string => {
  if (dir === 'BUY' || dir === 'buy') return '买入';
  if (dir === 'SELL' || dir === 'sell') return '卖出';
  return dir;
};

const CopyHistory: React.FC = () => {
  const [logs, setLogs] = useState<CopyTradeLog[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // ── Pagination ──
  const [page, setPage] = useState<number>(0);
  const [pageSize, setPageSize] = useState<number>(50);

  // ── Relationship filter ──
  const [relationships, setRelationships] = useState<CopyRelationship[]>([]);
  const [selectedRel, setSelectedRel] = useState<CopyRelationship | null>(null);

  // ── Load relationship list for dropdown ──
  useEffect(() => {
    const load = async (): Promise<void> => {
      try {
        const res = await fetchRelationships({ page_size: 200 });
        if (res.code === 0) {
          setRelationships(res.data.items);
        }
      } catch {
        // silently fail
      }
    };
    load();
  }, []);

  // ── Load trade logs ──
  const loadLogs = useCallback(async () => {
    if (!selectedRel) return;

    setLoading(true);
    setError(null);
    try {
      const res = await fetchTradeLogs(selectedRel.relationship_id, page + 1, pageSize);
      if (res.code === 0) {
        setLogs(res.data.items);
        setTotal(res.data.total);
      } else {
        setError(res.message);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to load trade logs');
    } finally {
      setLoading(false);
    }
  }, [selectedRel, page, pageSize]);

  useEffect(() => {
    loadLogs();
  }, [loadLogs]);

  const handleRelChange = (
    _event: React.SyntheticEvent,
    value: CopyRelationship | null,
  ): void => {
    setSelectedRel(value);
    setPage(0);
  };

  // ── Render helpers ──
  const renderHeaderCell = (label: string, width?: string): React.ReactNode => (
    <TableCell sx={{ color: TEXT_SECONDARY, fontWeight: 600, borderBottom: `1px solid ${BORDER}`, width }}>
      {label}
    </TableCell>
  );

  const renderCell = (content: React.ReactNode): React.ReactNode => (
    <TableCell sx={{ color: TEXT_PRIMARY, borderBottom: `1px solid ${BORDER}` }}>
      {content}
    </TableCell>
  );

  return (
    <Box>
      {/* Toolbar */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 2 }}>
        <Autocomplete
          options={relationships}
          getOptionLabel={(r) =>
            `#${r.relationship_id} — 主账户 #${r.master_account_id} → 从账户 #${r.copy_account_id}`
          }
          value={selectedRel}
          onChange={handleRelChange}
          isOptionEqualToValue={(o, v) => o.relationship_id === v.relationship_id}
          renderInput={(params) => (
            <TextField
              {...params}
              label="选择跟单关系"
              size="small"
              sx={{
                minWidth: 360,
                '& .MuiOutlinedInput-root': {
                  backgroundColor: DARK_BG,
                  color: TEXT_PRIMARY,
                  '& fieldset': { borderColor: BORDER },
                  '&:hover fieldset': { borderColor: ACCENT },
                  '&.Mui-focused fieldset': { borderColor: ACCENT },
                },
                '& .MuiInputLabel-root': { color: TEXT_SECONDARY },
                '& .MuiInputLabel-root.Mui-focused': { color: ACCENT },
              }}
            />
          )}
          ListboxProps={{
            sx: { backgroundColor: CARD_BG, color: TEXT_PRIMARY },
          }}
          sx={{ minWidth: 360 }}
        />
      </Box>

      {/* Error */}
      {error && (
        <Typography variant="body2" sx={{ color: '#ef4444', mb: 1 }}>
          {error}
        </Typography>
      )}

      {/* Empty state */}
      {!selectedRel ? (
        <Box sx={{ textAlign: 'center', py: 6 }}>
          <Typography variant="body1" sx={{ color: TEXT_SECONDARY }}>
            请先选择一个跟单关系以查看执行历史
          </Typography>
        </Box>
      ) : loading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={32} sx={{ color: ACCENT }} />
        </Box>
      ) : (
        <>
          <TableContainer sx={{ backgroundColor: CARD_BG, borderRadius: 1, border: `1px solid ${BORDER}` }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  {renderHeaderCell('时间', '160px')}
                  {renderHeaderCell('品种', '100px')}
                  {renderHeaderCell('方向', '60px')}
                  {renderHeaderCell('手数', '70px')}
                  {renderHeaderCell('入场价', '90px')}
                  {renderHeaderCell('出场价', '90px')}
                  {renderHeaderCell('盈亏', '90px')}
                  {renderHeaderCell('状态', '70px')}
                  {renderHeaderCell('延迟', '80px')}
                </TableRow>
              </TableHead>
              <TableBody>
                {logs.length === 0 ? (
                  <TableRow>
                    <TableCell
                      colSpan={9}
                      sx={{ color: TEXT_SECONDARY, textAlign: 'center', py: 4, borderBottom: 'none' }}
                    >
                      暂无执行历史
                    </TableCell>
                  </TableRow>
                ) : (
                  logs.map((log) => {
                    const pnl = formatPnl(log.pnl ?? log.profit);
                    const statusCfg = STATUS_CONFIG[log.status] ?? {
                      color: TEXT_SECONDARY,
                      label: log.status,
                    };
                    return (
                      <TableRow key={log.log_id} hover sx={{ '&:hover': { backgroundColor: `${DARK_BG}88` } }}>
                        {renderCell(
                          <Typography variant="body2" sx={{ fontSize: '0.8rem' }}>
                            {log.created_at}
                          </Typography>
                        )}
                        {renderCell(
                          <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                            {log.symbol}
                          </Typography>
                        )}
                        {renderCell(
                          <Typography variant="body2">
                            {formatDirection(log.direction)}
                          </Typography>
                        )}
                        {renderCell(
                          <Typography variant="body2">{log.lot}</Typography>
                        )}
                        {renderCell(
                          <Typography variant="body2">
                            {log.entry_price?.toFixed(4) ?? '—'}
                          </Typography>
                        )}
                        {renderCell(
                          <Typography variant="body2">
                            {log.exit_price?.toFixed(4) ?? '—'}
                          </Typography>
                        )}
                        {renderCell(
                          <Typography variant="body2" sx={{ color: pnl.color, fontWeight: 600 }}>
                            {pnl.text}
                          </Typography>
                        )}
                        {renderCell(
                          <Chip
                            label={statusCfg.label}
                            size="small"
                            sx={{
                              backgroundColor: `${statusCfg.color}22`,
                              color: statusCfg.color,
                              fontSize: '0.7rem',
                            }}
                          />
                        )}
                        {renderCell(
                          <Typography variant="body2">
                            {log.latency_ms !== undefined ? `${log.latency_ms}ms` : '—'}
                          </Typography>
                        )}
                      </TableRow>
                    );
                  })
                )}
              </TableBody>
            </Table>
          </TableContainer>

          {/* Pagination */}
          <TablePagination
            component="div"
            count={total}
            page={page}
            onPageChange={(_e, newPage) => setPage(newPage)}
            rowsPerPage={pageSize}
            onRowsPerPageChange={(e) => {
              setPageSize(parseInt(e.target.value, 10));
              setPage(0);
            }}
            rowsPerPageOptions={[20, 50, 100]}
            sx={{
              color: TEXT_SECONDARY,
              '.MuiTablePagination-selectIcon': { color: TEXT_SECONDARY },
              '.MuiTablePagination-actions button': { color: TEXT_SECONDARY },
              '.MuiTablePagination-actions button.Mui-disabled': { color: `${TEXT_SECONDARY}44` },
            }}
          />
        </>
      )}
    </Box>
  );
};

export default CopyHistory;
