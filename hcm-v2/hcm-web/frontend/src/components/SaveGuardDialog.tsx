import React, { useState } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions, Button, TextField,
  Box, Typography, CircularProgress,
} from '@mui/material';
import LockOutlinedIcon from '@mui/icons-material/LockOutlined';
import VisibilityIcon from '@mui/icons-material/Visibility';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';
import client from '../api/client';

/** 单条变更（用于展示即将写入的前后差异）。 */
export interface SaveChange {
  label: string;
  oldValue?: string | number | boolean | null;
  newValue?: string | number | boolean | null;
}

interface SaveGuardDialogProps {
  open: boolean;
  title?: string;
  /** 顶部说明文字（无法逐项 diff 时给一句总括）。 */
  description?: string;
  /** 即将写入的变更清单（旧值 → 新值）。为空则只显示密码框。 */
  changes?: SaveChange[];
  /** 当前登录用户名，用于二次密码鉴权。 */
  username?: string;
  confirmLabel?: string;
  onClose: () => void;
  /** 密码校验通过后执行的真实保存逻辑（PUT 等）。 */
  onConfirmed: () => Promise<void> | void;
}

const fmt = (v?: string | number | boolean | null): string => {
  if (v === undefined || v === null || v === '') return '—';
  return String(v);
};

/**
 * 保存前的「二次确认 + 密码鉴权」弹窗。
 *
 * 流程：确认 → 复用登录接口校验当前用户密码（防误调 / 防他人误操作）→
 * 密码通过才执行 onConfirmed 真实保存。密码错误与保存失败分别提示。
 *
 * 纯前端 UX 闸门：后端写接口本身仍带 require_auth，本组件额外要求
 * 「已登录用户再次输入密码」才能保存，双重保险。
 */
const SaveGuardDialog: React.FC<SaveGuardDialogProps> = ({
  open,
  title = '确认保存参数',
  description,
  changes = [],
  username,
  confirmLabel = '确认保存',
  onClose,
  onConfirmed,
}) => {
  const [password, setPassword] = useState('');
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [phase, setPhase] = useState<'idle' | 'verifying' | 'saving'>('idle');

  const reset = (): void => {
    setPassword('');
    setShow(false);
    setError(null);
    setPhase('idle');
  };

  const handleClose = (): void => {
    if (phase !== 'idle') return; // 校验/保存中禁止关闭，防误触
    reset();
    onClose();
  };

  const handleConfirm = async (): Promise<void> => {
    if (!password) {
      setError('请输入登录密码');
      return;
    }
    setError(null);
    setPhase('verifying');
    // ① 密码二次鉴权：复用登录接口校验当前用户密码
    try {
      const { data } = await client.post('/api/auth/login', {
        username: username || '',
        password,
      });
      if (!data || !data.access_token) throw new Error('no token');
    } catch {
      setError('密码错误，无法授权保存');
      setPhase('idle');
      return;
    }
    // ② 密码通过 → 执行真实保存
    setPhase('saving');
    try {
      await onConfirmed();
      reset();
      onClose();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '未知错误';
      setError('保存失败：' + msg);
      setPhase('idle');
    }
  };

  const busy = phase !== 'idle';

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      maxWidth="sm"
      fullWidth
      PaperProps={{ sx: { backgroundColor: '#1a1a24', color: '#e2e8f0', borderRadius: 2 } }}
    >
      <DialogTitle sx={{ fontSize: 16, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 1 }}>
        <LockOutlinedIcon sx={{ fontSize: 18, color: '#f59e0b' }} />
        {title}
      </DialogTitle>
      <DialogContent>
        {description && (
          <Typography variant="body2" sx={{ color: '#94a3b8', mb: 2 }}>
            {description}
          </Typography>
        )}
        {changes.length > 0 && (
          <Box sx={{ mb: 2, p: 1.5, borderRadius: 1, backgroundColor: '#0f0f17', border: '1px solid #2a2a3a' }}>
            <Typography variant="caption" sx={{ color: '#64748b', display: 'block', mb: 0.5 }}>
              即将写入以下变更：
            </Typography>
            {changes.map((c, i) => (
              <Box
                key={i}
                sx={{ display: 'flex', justifyContent: 'space-between', gap: 2, py: '2px' }}
              >
                <Typography variant="body2" sx={{ color: '#cbd5e1', whiteSpace: 'nowrap' }}>
                  {c.label}
                </Typography>
                <Box sx={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <Typography component="span" variant="body2" sx={{ color: '#ef4444' }}>
                    {fmt(c.oldValue)}
                  </Typography>
                  <Typography component="span" variant="body2" sx={{ color: '#64748b', mx: 0.5 }}>
                    →
                  </Typography>
                  <Typography component="span" variant="body2" sx={{ color: '#22c55e' }}>
                    {fmt(c.newValue)}
                  </Typography>
                </Box>
              </Box>
            ))}
          </Box>
        )}
        <TextField
          label="登录密码（二次确认鉴权）"
          type={show ? 'text' : 'password'}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !busy) void handleConfirm();
          }}
          fullWidth
          autoFocus
          error={!!error}
          helperText={error || '请输入您的登录密码以授权此次保存（防止误调参数）'}
          InputProps={{
            sx: { color: '#e2e8f0' },
            endAdornment: (
              <Button
                size="small"
                onClick={() => setShow((s) => !s)}
                sx={{ color: '#64748b', minWidth: 0 }}
                tabIndex={-1}
                aria-label={show ? '隐藏密码' : '显示密码'}
              >
                {show ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
              </Button>
            ),
          }}
        />
      </DialogContent>
      <DialogActions sx={{ p: 2, gap: 1 }}>
        <Button onClick={handleClose} disabled={busy} variant="outlined"
          sx={{ color: '#94a3b8', borderColor: '#4b5563' }}>
          取消
        </Button>
        <Button onClick={() => void handleConfirm()} disabled={busy} variant="contained"
          sx={{ backgroundColor: '#ef4444', '&:hover': { backgroundColor: '#dc2626' } }}>
          {busy ? <CircularProgress size={18} sx={{ color: '#fff' }} /> : confirmLabel}
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export default SaveGuardDialog;
