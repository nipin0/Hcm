import React, { useState, useEffect } from 'react';
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  Autocomplete,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Switch,
  FormControlLabel,
  ToggleButtonGroup,
  ToggleButton,
  Box,
  Typography,
} from '@mui/material';
import { DARK_BG, CARD_BG, ACCENT, TEXT_PRIMARY, TEXT_SECONDARY, BORDER } from '../../pages/copy/CopyLayout';
import type { CopyRelationship, CopyRelationshipCreate, CopyAccount } from '../../types/copy';
import { fetchAccounts } from '../../api/copy';

interface RelationshipDialogProps {
  open: boolean;
  /** Pass existing relationship to edit; undefined for create mode. */
  relationship?: CopyRelationship | null;
  onSave: (data: CopyRelationshipCreate) => void;
  onCancel: () => void;
}

/** Shared style for dark-themed TextFields */
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

/** Shared style for dark-themed FormControl (Select) */
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

const LOT_MODE_OPTIONS: { label: string; value: string }[] = [
  { label: '倍数跟单', value: 'multiplier' },
  { label: '固定手数', value: 'fixed' },
  { label: '余额比例', value: 'balance_ratio' },
];

const RelationshipDialog: React.FC<RelationshipDialogProps> = ({
  open,
  relationship,
  onSave,
  onCancel,
}) => {
  const isEdit = relationship !== null && relationship !== undefined;

  // ── Accounts for Autocomplete ──
  const [masterAccounts, setMasterAccounts] = useState<CopyAccount[]>([]);
  const [followerAccounts, setFollowerAccounts] = useState<CopyAccount[]>([]);

  // ── Form State ──
  const [masterId, setMasterId] = useState<number | null>(null);
  const [copyId, setCopyId] = useState<number | null>(null);
  const [lotMode, setLotMode] = useState<string>('multiplier');
  const [lotMultiplier, setLotMultiplier] = useState<number>(1.0);
  const [minLot, setMinLot] = useState<number | ''>(0.01);
  const [maxLot, setMaxLot] = useState<number | ''>(5.0);
  const [maxPositions, setMaxPositions] = useState<number>(10);
  const [directionMode, setDirectionMode] = useState<string>('FORWARD');
  const [copySl, setCopySl] = useState<boolean>(true);
  const [copyTp, setCopyTp] = useState<boolean>(true);
  const [retryOnFailure, setRetryOnFailure] = useState<boolean>(true);
  const [maxDailyLoss, setMaxDailyLoss] = useState<number | ''>(0.0);
  const [maxConsecutiveLosses, setMaxConsecutiveLosses] = useState<number>(3);
  const [retryMax, setRetryMax] = useState<number>(3);
  const [maxSlippagePips, setMaxSlippagePips] = useState<number | ''>('');
  const [maxExecutionDelayMs, setMaxExecutionDelayMs] = useState<number | ''>('');

  // ── Load accounts on open ──
  useEffect(() => {
    if (!open) return;

    const loadAccounts = async (): Promise<void> => {
      try {
        const [mRes, fRes] = await Promise.all([
          fetchAccounts(),
          fetchAccounts(),
        ]);
        if (mRes.code === 0) setMasterAccounts(mRes.data);
        if (fRes.code === 0) setFollowerAccounts(fRes.data);
      } catch {
        // silently fail; dropdowns will be empty
      }
    };
    loadAccounts();
  }, [open]);

  // ── Reset form when dialog opens ──
  useEffect(() => {
    if (!open) return;

    if (relationship) {
      setMasterId(relationship.master_account_id);
      setCopyId(relationship.copy_account_id);
      setLotMode(relationship.lot_mode);
      setLotMultiplier(relationship.lot_multiplier);
      setMinLot(relationship.min_lot);
      setMaxLot(relationship.max_lot);
      setMaxPositions(relationship.max_positions);
      setDirectionMode(relationship.direction_mode);
      setCopySl(relationship.copy_sl);
      setCopyTp(relationship.copy_tp);
      setRetryOnFailure(relationship.retry_on_failure);
      setMaxDailyLoss(relationship.max_daily_loss);
      setMaxConsecutiveLosses(relationship.max_consecutive_losses);
      setRetryMax(relationship.retry_max);
      setMaxSlippagePips(relationship.max_slippage_pips ?? '');
      setMaxExecutionDelayMs(relationship.max_execution_delay_ms ?? '');
    } else {
      setMasterId(null);
      setCopyId(null);
      setLotMode('multiplier');
      setLotMultiplier(1.0);
      setMinLot(0.01);
      setMaxLot(5.0);
      setMaxPositions(10);
      setDirectionMode('FORWARD');
      setCopySl(true);
      setCopyTp(true);
      setRetryOnFailure(true);
      setMaxDailyLoss(0.0);
      setMaxConsecutiveLosses(3);
      setRetryMax(3);
      setMaxSlippagePips('');
      setMaxExecutionDelayMs('');
    }
  }, [open, relationship]);

  const handleSubmit = (): void => {
    if (masterId === null || copyId === null) return;

    // 所有数字字段统一转为 number（空串回退默认值），避免把字符串/空串交给后端触发 422
    const num = (v: number | '', fallback: number): number => (v === '' ? fallback : v);

    const data: CopyRelationshipCreate = {
      master_account_id: masterId,
      copy_account_id: copyId,
      lot_mode: lotMode as CopyRelationshipCreate['lot_mode'],
      lot_multiplier: num(lotMultiplier, 1.0),
      min_lot: num(minLot, 0.01),
      max_lot: num(maxLot, 5.0),
      max_positions: num(maxPositions, 10),
      direction_mode: directionMode as CopyRelationshipCreate['direction_mode'],
      copy_sl: copySl,
      copy_tp: copyTp,
      retry_on_failure: retryOnFailure,
      max_daily_loss: num(maxDailyLoss, 0.0),
      max_consecutive_losses: num(maxConsecutiveLosses, 3),
      retry_max: num(retryMax, 3),
    };

    if (maxSlippagePips !== '') data.max_slippage_pips = Number(maxSlippagePips);
    if (maxExecutionDelayMs !== '') data.max_execution_delay_ms = Number(maxExecutionDelayMs);

    onSave(data);
  };

  // ── Helper: number input handler ──
  const numField = (
    label: string,
    value: number | '',
    setter: React.Dispatch<React.SetStateAction<number | ''>>,
    step: number = 0.01,
    min: number = 0,
  ): React.ReactNode => (
    <TextField
      label={label}
      type="number"
      size="small"
      fullWidth
      value={value}
      onChange={(e) => {
        const v = e.target.value;
        setter(v === '' ? '' : Number(v));
      }}
      inputProps={{ step, min }}
      sx={darkTextFieldSx}
      InputLabelProps={{ shrink: true }}
    />
  );

  return (
    <Dialog
      open={open}
      onClose={onCancel}
      maxWidth="md"
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
        {isEdit ? '编辑跟单关系' : '新建跟单关系'}
      </DialogTitle>

      <DialogContent sx={{ pt: 1 }}>
        <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 2, mt: 1 }}>
          {/* Row 1: Master / Follower */}
          <Autocomplete
            options={masterAccounts}
            getOptionLabel={(a) => `${a.account_name} (${a.broker_name})`}
            value={masterAccounts.find((a) => a.account_id === masterId) ?? null}
            onChange={(_, v) => setMasterId(v?.account_id ?? null)}
            isOptionEqualToValue={(o, v) => o.account_id === v.account_id}
            renderInput={(params) => (
              <TextField
                {...params}
                label="主账户"
                size="small"
                sx={darkTextFieldSx}
                InputLabelProps={{ shrink: true }}
              />
            )}
            ListboxProps={{
              sx: { backgroundColor: CARD_BG, color: TEXT_PRIMARY },
            }}
          />

          <Autocomplete
            options={followerAccounts}
            getOptionLabel={(a) => `${a.account_name} (${a.broker_name})`}
            value={followerAccounts.find((a) => a.account_id === copyId) ?? null}
            onChange={(_, v) => setCopyId(v?.account_id ?? null)}
            isOptionEqualToValue={(o, v) => o.account_id === v.account_id}
            renderInput={(params) => (
              <TextField
                {...params}
                label="从账户"
                size="small"
                sx={darkTextFieldSx}
                InputLabelProps={{ shrink: true }}
              />
            )}
            ListboxProps={{
              sx: { backgroundColor: CARD_BG, color: TEXT_PRIMARY },
            }}
          />

          {/* Row 2: Lot Mode / Lot Multiplier */}
          <FormControl size="small" fullWidth sx={darkSelectSx}>
            <InputLabel>手数模式</InputLabel>
            <Select
              value={lotMode}
              label="手数模式"
              onChange={(e) => setLotMode(e.target.value)}
            >
              {LOT_MODE_OPTIONS.map((opt) => (
                <MenuItem key={opt.value} value={opt.value}>
                  {opt.label}
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          <TextField
            label="手数倍数"
            type="number"
            size="small"
            fullWidth
            value={lotMultiplier}
            onChange={(e) => setLotMultiplier(Number(e.target.value))}
            inputProps={{ step: 0.1, min: 0.01 }}
            sx={darkTextFieldSx}
            InputLabelProps={{ shrink: true }}
          />

          {/* Row 3: Min Lot / Max Lot */}
          {numField('最小手数', minLot, setMinLot)}
          {numField('最大手数', maxLot, setMaxLot)}

          {/* Row 4: Max Positions / Direction */}
          <TextField
            label="最大持仓数"
            type="number"
            size="small"
            fullWidth
            value={maxPositions}
            onChange={(e) => setMaxPositions(Number(e.target.value))}
            inputProps={{ step: 1, min: 0 }}
            sx={darkTextFieldSx}
            InputLabelProps={{ shrink: true }}
          />

          <FormControl size="small" fullWidth>
            <Typography variant="body2" sx={{ color: TEXT_SECONDARY, mb: 0.5 }}>
              跟单方向
            </Typography>
            <ToggleButtonGroup
              value={directionMode}
              exclusive
              size="small"
              onChange={(_, v) => {
                if (v !== null) setDirectionMode(v);
              }}
              sx={{
                '& .MuiToggleButton-root': {
                  color: TEXT_SECONDARY,
                  borderColor: BORDER,
                  '&.Mui-selected': {
                    color: ACCENT,
                    backgroundColor: `${ACCENT}22`,
                  },
                },
              }}
            >
              <ToggleButton value="FORWARD">同步</ToggleButton>
              <ToggleButton value="REVERSE">反向</ToggleButton>
            </ToggleButtonGroup>
          </FormControl>

          {/* Row 5: Switches */}
          <Box sx={{ display: 'flex', gap: 2 }}>
            <FormControlLabel
              control={
                <Switch
                  checked={copySl}
                  onChange={(e) => setCopySl(e.target.checked)}
                  sx={{
                    '& .MuiSwitch-switchBase.Mui-checked': { color: ACCENT },
                    '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: ACCENT },
                  }}
                />
              }
              label={<Typography variant="body2" sx={{ color: TEXT_SECONDARY }}>复制止损</Typography>}
            />
            <FormControlLabel
              control={
                <Switch
                  checked={copyTp}
                  onChange={(e) => setCopyTp(e.target.checked)}
                  sx={{
                    '& .MuiSwitch-switchBase.Mui-checked': { color: ACCENT },
                    '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: ACCENT },
                  }}
                />
              }
              label={<Typography variant="body2" sx={{ color: TEXT_SECONDARY }}>复制止盈</Typography>}
            />
            <FormControlLabel
              control={
                <Switch
                  checked={retryOnFailure}
                  onChange={(e) => setRetryOnFailure(e.target.checked)}
                  sx={{
                    '& .MuiSwitch-switchBase.Mui-checked': { color: ACCENT },
                    '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: ACCENT },
                  }}
                />
              }
              label={<Typography variant="body2" sx={{ color: TEXT_SECONDARY }}>失败重试</Typography>}
            />
          </Box>

          {/* Row 6: Max Daily Loss / Max Consecutive Losses */}
          {numField('日内最大亏损', maxDailyLoss, setMaxDailyLoss, 1, 0)}
          <TextField
            label="最大连续亏损次数"
            type="number"
            size="small"
            fullWidth
            value={maxConsecutiveLosses}
            onChange={(e) => setMaxConsecutiveLosses(Number(e.target.value))}
            inputProps={{ step: 1, min: 0 }}
            sx={darkTextFieldSx}
            InputLabelProps={{ shrink: true }}
          />

          {/* Row 7: Retry Max / Slippage Pips */}
          <TextField
            label="最大重试次数"
            type="number"
            size="small"
            fullWidth
            value={retryMax}
            onChange={(e) => setRetryMax(Number(e.target.value))}
            inputProps={{ step: 1, min: 0 }}
            sx={darkTextFieldSx}
            InputLabelProps={{ shrink: true }}
          />

          <TextField
            label="滑点保护 (点数)"
            type="number"
            size="small"
            fullWidth
            value={maxSlippagePips}
            onChange={(e) => {
              const v = e.target.value;
              setMaxSlippagePips(v === '' ? '' : Number(v));
            }}
            inputProps={{ step: 0.1, min: 0 }}
            sx={darkTextFieldSx}
            InputLabelProps={{ shrink: true }}
          />

          {/* Row 8: Execution Delay */}
          <TextField
            label="开仓最大延迟 (毫秒)"
            type="number"
            size="small"
            fullWidth
            value={maxExecutionDelayMs}
            onChange={(e) => {
              const v = e.target.value;
              setMaxExecutionDelayMs(v === '' ? '' : Number(v));
            }}
            inputProps={{ step: 100, min: 0 }}
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
          disabled={masterId === null || copyId === null}
        >
          {isEdit ? '更新' : '创建'}
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export default RelationshipDialog;
