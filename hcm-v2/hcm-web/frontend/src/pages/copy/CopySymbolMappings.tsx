import React, { useState, useEffect, useCallback } from 'react';
import {
  Box,
  Button,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Switch,
  Chip,
  CircularProgress,
  Typography,
  IconButton,
  Tooltip,
} from '@mui/material';
import EditIcon from '@mui/icons-material/Edit';
import DeleteIcon from '@mui/icons-material/Delete';
import AddIcon from '@mui/icons-material/Add';
import {
  CARD_BG,
  ACCENT,
  TEXT_PRIMARY,
  TEXT_SECONDARY,
  BORDER,
  DARK_BG,
} from './CopyLayout';
import type { SymbolMapping, SymbolMappingCreate, SymbolMappingUpdate } from '../../types/copy';
import {
  fetchSymbolMappings,
  createSymbolMapping,
  updateSymbolMapping,
  deleteSymbolMapping,
  toggleSymbolMapping,
} from '../../api/copy';
import SymbolMappingDialog from '../../components/copy/SymbolMappingDialog';
import ConfirmDialog from '../../components/copy/ConfirmDialog';

const MATCH_MODE_LABELS: Record<string, string> = {
  exact: '精确',
  prefix: '前缀',
  suffix: '后缀',
  manual: '手动',
};

const CopySymbolMappings: React.FC = () => {
  const [mappings, setMappings] = useState<SymbolMapping[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // ── Dialogs ──
  const [dialogOpen, setDialogOpen] = useState<boolean>(false);
  const [editingMapping, setEditingMapping] = useState<SymbolMapping | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<SymbolMapping | null>(null);
  const [togglingId, setTogglingId] = useState<number | null>(null);

  // ── Load data ──
  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchSymbolMappings();
      if (res.code === 0) {
        setMappings(res.data.items);
      } else {
        setError(res.message);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to load symbol mappings');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // ── CRUD Handlers ──
  const handleCreate = (): void => {
    setEditingMapping(null);
    setError(null);
    setDialogOpen(true);
  };

  const handleEdit = (mapping: SymbolMapping): void => {
    setEditingMapping(mapping);
    setDialogOpen(true);
  };

  const handleSave = async (data: SymbolMappingCreate): Promise<void> => {
    try {
      if (editingMapping) {
        const updateData: SymbolMappingUpdate = { ...data };
        const res = await updateSymbolMapping(editingMapping.mapping_id, updateData);
        if (res.code !== 0) {
          setError(res.message);
          return;
        }
      } else {
        const res = await createSymbolMapping(data);
        if (res.code !== 0) {
          setError(res.message);
          return;
        }
      }
      setDialogOpen(false);
      setEditingMapping(null);
      await loadData();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Save failed');
    }
  };

  const handleDeleteConfirm = async (): Promise<void> => {
    if (!deleteTarget) return;
    try {
      const res = await deleteSymbolMapping(deleteTarget.mapping_id);
      if (res.code !== 0) {
        setError(res.message);
        return;
      }
      setDeleteTarget(null);
      await loadData();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Delete failed');
    }
  };

  // ── Toggle ──
  const handleToggle = async (mapping: SymbolMapping): Promise<void> => {
    setTogglingId(mapping.mapping_id);
    try {
      const res = await toggleSymbolMapping(mapping.mapping_id);
      if (res.code !== 0) {
        setError(res.message);
        return;
      }
      await loadData();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Toggle failed');
    } finally {
      setTogglingId(null);
    }
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
        <Button
          variant="contained"
          startIcon={<AddIcon />}
          onClick={handleCreate}
          sx={{
            backgroundColor: ACCENT,
            '&:hover': { backgroundColor: '#2563eb' },
            textTransform: 'none',
          }}
        >
          新建映射
        </Button>
      </Box>

      {/* Error */}
      {error && (
        <Typography variant="body2" sx={{ color: '#ef4444', mb: 1 }}>
          {error}
        </Typography>
      )}

      {/* Table */}
      {loading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={32} sx={{ color: ACCENT }} />
        </Box>
      ) : (
        <TableContainer sx={{ backgroundColor: CARD_BG, borderRadius: 1, border: `1px solid ${BORDER}` }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                {renderHeaderCell('ID', '60px')}
                {renderHeaderCell('主经纪商')}
                {renderHeaderCell('主品种')}
                {renderHeaderCell('从经纪商')}
                {renderHeaderCell('从品种')}
                {renderHeaderCell('匹配模式', '80px')}
                {renderHeaderCell('启用', '70px')}
                {renderHeaderCell('操作', '100px')}
              </TableRow>
            </TableHead>
            <TableBody>
              {mappings.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={8} sx={{ color: TEXT_SECONDARY, textAlign: 'center', py: 4, borderBottom: 'none' }}>
                    暂无品种映射
                  </TableCell>
                </TableRow>
              ) : (
                mappings.map((m) => (
                  <TableRow key={m.mapping_id} hover sx={{ '&:hover': { backgroundColor: `${DARK_BG}88` } }}>
                    {renderCell(
                      <Chip
                        label={m.mapping_id}
                        size="small"
                        sx={{ backgroundColor: `${ACCENT}22`, color: ACCENT, fontWeight: 600 }}
                      />
                    )}
                    {renderCell(
                      <Typography variant="body2">
                        {m.master_broker || '—'}
                      </Typography>
                    )}
                    {renderCell(
                      <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                        {m.master_symbol}
                      </Typography>
                    )}
                    {renderCell(
                      <Typography variant="body2">
                        {m.follower_broker || '—'}
                      </Typography>
                    )}
                    {renderCell(
                      <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                        {m.follower_symbol}
                      </Typography>
                    )}
                    {renderCell(
                      <Chip
                        label={MATCH_MODE_LABELS[m.match_mode] ?? m.match_mode}
                        size="small"
                        variant="outlined"
                        sx={{ color: TEXT_SECONDARY, borderColor: BORDER, fontSize: '0.7rem' }}
                      />
                    )}
                    {renderCell(
                      <Switch
                        checked={m.is_active}
                        disabled={togglingId === m.mapping_id}
                        onChange={() => handleToggle(m)}
                        size="small"
                        sx={{
                          '& .MuiSwitch-switchBase.Mui-checked': { color: '#22c55e' },
                          '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: '#22c55e88' },
                        }}
                      />
                    )}
                    {renderCell(
                      <Box sx={{ display: 'flex', gap: 0.5 }}>
                        <Tooltip title="编辑">
                          <IconButton size="small" onClick={() => handleEdit(m)} sx={{ color: ACCENT }}>
                            <EditIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                        <Tooltip title="删除">
                          <IconButton size="small" onClick={() => setDeleteTarget(m)} sx={{ color: '#ef4444' }}>
                            <DeleteIcon fontSize="small" />
                          </IconButton>
                        </Tooltip>
                      </Box>
                    )}
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      {/* Create/Edit Dialog */}
      <SymbolMappingDialog
        open={dialogOpen}
        mapping={editingMapping}
        onSave={handleSave}
        onCancel={() => {
          setDialogOpen(false);
          setEditingMapping(null);
        }}
      />

      {/* Delete Confirmation */}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="删除品种映射"
        message={
          deleteTarget
            ? `确定要删除 ID=${deleteTarget.mapping_id} 的品种映射吗？\n${deleteTarget.master_symbol} → ${deleteTarget.follower_symbol}`
            : ''
        }
        onConfirm={handleDeleteConfirm}
        onCancel={() => setDeleteTarget(null)}
      />
    </Box>
  );
};

export default CopySymbolMappings;
