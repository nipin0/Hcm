import React from 'react';
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
  Button,
} from '@mui/material';
import { DARK_BG, CARD_BG, ACCENT, TEXT_PRIMARY, TEXT_SECONDARY, BORDER } from '../../pages/copy/CopyLayout';

interface ConfirmDialogProps {
  open: boolean;
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  open,
  title = '确认操作',
  message,
  confirmLabel = '确定',
  cancelLabel = '取消',
  onConfirm,
  onCancel,
}) => {
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      maxWidth="xs"
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
      <DialogTitle sx={{ color: TEXT_PRIMARY, fontSize: '1.1rem', pb: 0 }}>
        {title}
      </DialogTitle>
      <DialogContent sx={{ py: 2 }}>
        <DialogContentText sx={{ color: TEXT_SECONDARY }}>
          {message}
        </DialogContentText>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2, gap: 1 }}>
        <Button
          onClick={onCancel}
          sx={{
            color: TEXT_SECONDARY,
            borderColor: BORDER,
            '&:hover': { borderColor: TEXT_SECONDARY },
          }}
          variant="outlined"
        >
          {cancelLabel}
        </Button>
        <Button
          onClick={onConfirm}
          sx={{
            backgroundColor: '#dc2626',
            color: '#fff',
            '&:hover': { backgroundColor: '#b91c1c' },
          }}
          variant="contained"
        >
          {confirmLabel}
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export default ConfirmDialog;
