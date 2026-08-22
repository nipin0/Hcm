import React, { useState, useEffect, useCallback, lazy, Suspense } from 'react';
import {
  Box, Button, Table, TableBody, TableCell, TableContainer,
  TableHead, TableRow, Chip, CircularProgress, Typography, IconButton,
} from '@mui/material';
import EditIcon from '@mui/icons-material/Edit';
import DeleteIcon from '@mui/icons-material/Delete';
import AddIcon from '@mui/icons-material/Add';
import { CARD_BG, ACCENT, TEXT_PRIMARY, TEXT_SECONDARY, BORDER, DARK_BG } from './CopyLayout';
import type { CopyRelationship, CopyRelationshipCreate, CopyStatus } from '../../types/copy';
import { fetchRelationships, createRelationship, updateRelationship, deleteRelationship } from '../../api/copy';
import StatusButton, { STATUS_COLOR } from '../../components/copy/StatusButton';
import ConfirmDialog from '../../components/copy/ConfirmDialog';

const RelationshipDialog = lazy(() => import('../../components/copy/RelationshipDialog'));

const LOT_MODE_LABELS: Record<string, string> = { multiplier: '\u500d\u6570', fixed: '\u56fa\u5b9a', balance_ratio: '\u4f59\u989d\u6bd4' };
const DIRECTION_LABELS: Record<string, string> = { FORWARD: '\u540c\u6b65', REVERSE: '\u53cd\u5411', BOTH: '\u53cc\u5411' };

const CopyRelationships: React.FC = () => {
  const [relationships, setRelationships] = useState<CopyRelationship[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CopyRelationship | null>(null);
  const [statusLoadingId, setStatusLoadingId] = useState<number | null>(null);
  const [dialogOpen, setDialogOpen] = useState<boolean>(false);
  const [editingRel, setEditingRel] = useState<CopyRelationship | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchRelationships({});
      if (res.code === 0) setRelationships(res.data.items);
      else setError(res.message);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const handleSave = async (data: CopyRelationshipCreate) => {
    try {
      if (editingRel) {
        const res = await updateRelationship(editingRel.relationship_id, data);
        if (res.code !== 0) { setError(res.message); return; }
      } else {
        const res = await createRelationship(data);
        if (res.code !== 0) { setError(res.message); return; }
      }
      setDialogOpen(false);
      setEditingRel(null);
      await loadData();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Save failed');
    }
  };

  const handleStatusChange = async (rel: CopyRelationship, newStatus: CopyStatus) => {
    setStatusLoadingId(rel.relationship_id);
    try {
      const res = await updateRelationship(rel.relationship_id, { status: newStatus });
      if (res.code !== 0) setError(res.message);
      else await loadData();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed');
    } finally { setStatusLoadingId(null); }
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    try {
      const res = await deleteRelationship(deleteTarget.relationship_id);
      if (res.code !== 0) setError(res.message);
      else { setDeleteTarget(null); await loadData(); }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed');
    }
  };

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 2 }}>
        <Button variant="contained" startIcon={<AddIcon />} onClick={() => { setEditingRel(null); setDialogOpen(true); }}
          sx={{ backgroundColor: ACCENT, textTransform: 'none', '&:hover': { backgroundColor: '#2563eb' } }}>
          New Relationship
        </Button>
      </Box>
      {error && <Typography sx={{ color: '#ef4444', mb: 1 }}>{error}</Typography>}
      {loading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={32} sx={{ color: ACCENT }} />
        </Box>
      ) : (
        <TableContainer sx={{ backgroundColor: CARD_BG, borderRadius: 1, border: `1px solid ${BORDER}` }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell sx={{ color: TEXT_SECONDARY, fontWeight: 600 }}>ID</TableCell>
                <TableCell sx={{ color: TEXT_SECONDARY, fontWeight: 600 }}>Master</TableCell>
                <TableCell sx={{ color: TEXT_SECONDARY, fontWeight: 600 }}>Follower</TableCell>
                <TableCell sx={{ color: TEXT_SECONDARY, fontWeight: 600 }}>Mode</TableCell>
                <TableCell sx={{ color: TEXT_SECONDARY, fontWeight: 600 }}>Status</TableCell>
                <TableCell sx={{ color: TEXT_SECONDARY, fontWeight: 600 }}>Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {relationships.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} sx={{ color: TEXT_SECONDARY, textAlign: 'center', py: 4 }}>
                    No relationships found
                  </TableCell>
                </TableRow>
              ) : (
                relationships.map((rel) => (
                  <TableRow key={rel.relationship_id} hover sx={{ '&:hover': { backgroundColor: `${DARK_BG}88` } }}>
                    <TableCell sx={{ color: TEXT_PRIMARY }}>
                      <Chip label={rel.relationship_id} size="small" sx={{ backgroundColor: `${ACCENT}22`, color: ACCENT, fontWeight: 600 }} />
                    </TableCell>
                    <TableCell sx={{ color: TEXT_PRIMARY }}>#{rel.master_account_id}</TableCell>
                    <TableCell sx={{ color: TEXT_PRIMARY }}>#{rel.copy_account_id}</TableCell>
                    <TableCell sx={{ color: TEXT_PRIMARY }}>{LOT_MODE_LABELS[rel.lot_mode] ?? rel.lot_mode}</TableCell>
                    <TableCell>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                        <StatusButton status={rel.status}
                          loading={statusLoadingId === rel.relationship_id}
                          onChange={(ns) => handleStatusChange(rel, ns)} />
                        <Chip label={rel.status} size="small"
                          sx={{ backgroundColor: `${STATUS_COLOR[rel.status]}22`, color: STATUS_COLOR[rel.status], fontSize: '0.7rem' }} />
                      </Box>
                    </TableCell>
                    <TableCell>
                      <IconButton size="small" onClick={() => { setEditingRel(rel); setDialogOpen(true); }} sx={{ color: ACCENT }}><EditIcon fontSize="small" /></IconButton>
                      <IconButton size="small" onClick={() => setDeleteTarget(rel)} sx={{ color: '#ef4444' }}><DeleteIcon fontSize="small" /></IconButton>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {dialogOpen && (
        <Suspense fallback={null}>
          <RelationshipDialog open={dialogOpen} relationship={editingRel}
        onSave={handleSave}
        onCancel={() => setDialogOpen(false)} />
        </Suspense>
      )}
      <ConfirmDialog open={!!deleteTarget} message={`Delete relationship #${deleteTarget?.relationship_id}?`}
        onConfirm={handleDeleteConfirm} onCancel={() => setDeleteTarget(null)} />
    </Box>
  );
};

export default CopyRelationships;
