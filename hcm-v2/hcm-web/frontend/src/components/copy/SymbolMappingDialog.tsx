import React, { useState, useEffect } from 'react';
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Box,
} from '@mui/material';
import { DARK_BG, CARD_BG, ACCENT, TEXT_PRIMARY, TEXT_SECONDARY, BORDER } from '../../pages/copy/CopyLayout';
import type { SymbolMapping, SymbolMappingCreate } from '../../types/copy';
import { fetchBrokers } from '../../api/copy';

interface SymbolMappingDialogProps {
  open: boolean;
  mapping?: SymbolMapping | null;
  onSave: (data: SymbolMappingCreate) => void;
  onCancel: () => void;
}

const MATCH_MODE_OPTIONS: { label: string; value: string }[] = [
  { label: '精确匹配', value: 'exact' },
  { label: '前缀匹配', value: 'prefix' },
  { label: '后缀匹配', value: 'suffix' },
  { label: '手动指定', value: 'manual' },
];

/** Shared dark text field style */
const darkTextFieldSx = {
  '& .MuiOutlinedInput-root': {
    backgroundColor: DARK_BG,
    color: TEXT_PRIMARY,
    '& fieldset': { borderColor: BORDER },
    '&:hover fieldset': { borderColor: ACCENT },
    '&.Mui-focused fieldset': { borderColor: ACCENT },
  },
  '& .MuiInputLabel-root': { color: TEXT_SECONDARY },
  '& .MuiInputLabel-root.Mui-focused': { color: ACCENT },
};

/** Shared dark Select style */
const darkSelectSx = {
  '& .MuiOutlinedInput-root': {
    backgroundColor: DARK_BG,
    color: TEXT_PRIMARY,
    '& fieldset': { borderColor: BORDER },
    '&:hover fieldset': { borderColor: ACCENT },
    '&.Mui-focused fieldset': { borderColor: ACCENT },
  },
  '& .MuiInputLabel-root': { color: TEXT_SECONDARY },
  '& .MuiInputLabel-root.Mui-focused': { color: ACCENT },
  '& .MuiSvgIcon-root': { color: TEXT_SECONDARY },
};

const SymbolMappingDialog: React.FC<SymbolMappingDialogProps> = ({
  open,
  mapping,
  onSave,
  onCancel,
}) => {
  const isEdit = mapping !== null && mapping !== undefined;

  const [brokers, setBrokers] = useState<string[]>([]);

  const [masterBroker, setMasterBroker] = useState<string>('');
  const [masterSymbol, setMasterSymbol] = useState<string>('');
  const [followerBroker, setFollowerBroker] = useState<string>('');
  const [followerSymbol, setFollowerSymbol] = useState<string>('');
  const [matchMode, setMatchMode] = useState<string>('exact');
  const [matchPriority, setMatchPriority] = useState<number>(0);

  // ── Load brokers ──
  useEffect(() => {
    if (!open) return;
    const load = async (): Promise<void> => {
      try {
        const res = await fetchBrokers();
        if (res.code === 0) {
          setBrokers(res.data.map((b) => b.broker_name));
        }
      } catch {
        // silently fail
      }
    };
    load();
  }, [open]);

  // ── Reset form ──
  useEffect(() => {
    if (!open) return;

    if (mapping) {
      setMasterBroker(mapping.master_broker);
      setMasterSymbol(mapping.master_symbol);
      setFollowerBroker(mapping.follower_broker);
      setFollowerSymbol(mapping.follower_symbol);
      setMatchMode(mapping.match_mode);
      setMatchPriority(mapping.match_priority);
    } else {
      setMasterBroker('');
      setMasterSymbol('');
      setFollowerBroker('');
      setFollowerSymbol('');
      setMatchMode('exact');
      setMatchPriority(0);
    }
  }, [open, mapping]);

  const handleSubmit = (): void => {
    if (!masterBroker || !masterSymbol || !followerSymbol) return;

    const data: SymbolMappingCreate = {
      master_broker: masterBroker,
      master_symbol: masterSymbol,
      follower_broker: followerBroker,
      follower_symbol: followerSymbol,
      match_mode: matchMode as SymbolMappingCreate['match_mode'],
      match_priority: matchPriority,
    };

    onSave(data);
  };

  const isValid = masterBroker.trim() !== '' && masterSymbol.trim() !== '' && followerSymbol.trim() !== '';

  return (
    <Dialog
      open={open}
      onClose={onCancel}
      maxWidth="sm"
      fullWidth
      PaperProps={{
        sx: {
          backgroundColor: CARD_BG,
          color: TEXT_PRIMARY,
          borderRadius: 2,
          border: `1px solid ${DARK_BG}`,
        },
      }}
    >
      <DialogTitle sx={{ color: TEXT_PRIMARY, pb: 1 }}>
        {isEdit ? '编辑品种映射' : '新建品种映射'}
      </DialogTitle>

      <DialogContent sx={{ pt: 1 }}>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, mt: 1 }}>
          {/* Master Broker */}
          <FormControl size="small" fullWidth sx={darkSelectSx}>
            <InputLabel>主经纪商</InputLabel>
            <Select
              value={masterBroker}
              label="主经纪商"
              onChange={(e) => setMasterBroker(e.target.value)}
            >
              {brokers.map((b) => (
                <MenuItem key={b} value={b}>{b}</MenuItem>
              ))}
            </Select>
          </FormControl>

          {/* Master Symbol */}
          <TextField
            label="主品种"
            size="small"
            fullWidth
            value={masterSymbol}
            onChange={(e) => setMasterSymbol(e.target.value)}
            placeholder="例如: XAUUSD"
            sx={darkTextFieldSx}
            InputLabelProps={{ shrink: true }}
          />

          {/* Follower Broker */}
          <FormControl size="small" fullWidth sx={darkSelectSx}>
            <InputLabel>从经纪商</InputLabel>
            <Select
              value={followerBroker}
              label="从经纪商"
              onChange={(e) => setFollowerBroker(e.target.value)}
            >
              {brokers.map((b) => (
                <MenuItem key={b} value={b}>{b}</MenuItem>
              ))}
            </Select>
          </FormControl>

          {/* Follower Symbol */}
          <TextField
            label="从品种"
            size="small"
            fullWidth
            value={followerSymbol}
            onChange={(e) => setFollowerSymbol(e.target.value)}
            placeholder="例如: XAUUSD"
            sx={darkTextFieldSx}
            InputLabelProps={{ shrink: true }}
          />

          {/* Match Mode */}
          <FormControl size="small" fullWidth sx={darkSelectSx}>
            <InputLabel>匹配模式</InputLabel>
            <Select
              value={matchMode}
              label="匹配模式"
              onChange={(e) => setMatchMode(e.target.value)}
            >
              {MATCH_MODE_OPTIONS.map((opt) => (
                <MenuItem key={opt.value} value={opt.value}>
                  {opt.label}
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {/* Match Priority */}
          <TextField
            label="匹配优先级"
            type="number"
            size="small"
            fullWidth
            value={matchPriority}
            onChange={(e) => setMatchPriority(Number(e.target.value))}
            inputProps={{ step: 1, min: 0 }}
            sx={darkTextFieldSx}
            InputLabelProps={{ shrink: true }}
          />
        </Box>
      </DialogContent>

      <DialogActions sx={{ px: 3, pb: 2, gap: 1 }}>
        <Button
          onClick={onCancel}
          sx={{ color: TEXT_SECONDARY, borderColor: BORDER, '&:hover': { borderColor: TEXT_SECONDARY } }}
          variant="outlined"
        >
          取消
        </Button>
        <Button
          onClick={handleSubmit}
          sx={{ backgroundColor: ACCENT, color: '#fff', '&:hover': { backgroundColor: '#2563eb' } }}
          variant="contained"
          disabled={!isValid}
        >
          {isEdit ? '更新' : '创建'}
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export default SymbolMappingDialog;
