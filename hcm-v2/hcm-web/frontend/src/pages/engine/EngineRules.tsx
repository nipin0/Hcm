import React, { useState, useEffect, useCallback } from 'react';
import {
  Box, Typography, Button, Chip, Switch, IconButton,
  Dialog, DialogTitle, DialogContent, DialogActions, TextField,
} from '@mui/material';
import { Add, Edit, Delete, RefreshCw, DragIndicator } from '../../components/Icons';
import client from '../../api/client';

interface EngineRule {
  rule_id: number;
  name: string;
  type: 'signal_filter' | 'risk_check' | 'dispatch_rule';
  priority: number;
  enabled: boolean;
  description: string;
  config?: Record<string, any>;
  updated_at: string;
}

const RULE_TYPES: { value: EngineRule['type']; label: string }[] = [
  { value: 'signal_filter', label: '信号过滤' },
  { value: 'risk_check', label: '风险检查' },
  { value: 'dispatch_rule', label: '调度规则' },
];

interface RuleFormData {
  name: string;
  type: EngineRule['type'];
  priority: number;
  enabled: boolean;
  description: string;
  config: string;
}

const EngineRules: React.FC = () => {
  const [rules, setRules] = useState<EngineRule[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [dialogOpen, setDialogOpen] = useState<boolean>(false);
  const [editingRule, setEditingRule] = useState<EngineRule | null>(null);
  const [formData, setFormData] = useState<RuleFormData>({
    name: '',
    type: 'signal_filter',
    priority: 1,
    enabled: true,
    description: '',
    config: '',
  });

  const fetchRules = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const { data: resp } = await client.get('/api/engine/rules');
      // API returns {code, data:{items,total,...}, message}; resp here is the inner data
      const items = (resp as any)?.items ?? (resp as any)?.data?.items ?? [];
      setRules(Array.isArray(items) ? items : []);
    } catch {
      setRules([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRules();
  }, [fetchRules]);

  const openCreateDialog = (): void => {
    setEditingRule(null);
    setFormData({
      name: '',
      type: 'signal_filter',
      priority: 1,
      enabled: true,
      description: '',
      config: '',
    });
    setDialogOpen(true);
  };

  const openEditDialog = (rule: EngineRule): void => {
    setEditingRule(rule);
    setFormData({
      name: rule.name,
      type: rule.type,
      priority: rule.priority,
      enabled: rule.enabled,
      description: rule.description,
      config: rule.config ? JSON.stringify(rule.config, null, 2) : '',
    });
    setDialogOpen(true);
  };

  const handleSave = async (): Promise<void> => {
    const body: Record<string, any> = {
      name: formData.name,
      type: formData.type,
      priority: Number(formData.priority),
      enabled: formData.enabled,
      description: formData.description,
    };

    if (formData.config.trim()) {
      try {
        body.config = JSON.parse(formData.config);
      } catch {
        // If JSON parsing fails, store as raw string
        body.config = formData.config;
      }
    }

    try {
      if (editingRule) {
        await client.put(`/api/engine/rules/${editingRule.rule_id}`, body);
      } else {
        await client.post('/api/engine/rules', body);
      }
      setDialogOpen(false);
      fetchRules();
    } catch {
      // Handle error silently
    }
  };

  const handleToggle = async (rule: EngineRule): Promise<void> => {
    try {
      await client.put(`/api/engine/rules/${rule.rule_id}`, { enabled: !rule.enabled });
      fetchRules();
    } catch {
      // Handle error
    }
  };

  const handleDelete = async (ruleId: number): Promise<void> => {
    if (!window.confirm('确认删除此规则？')) return;
    try {
      await client.delete(`/api/engine/rules/${ruleId}`);
      fetchRules();
    } catch {
      // Handle error
    }
  };

  const getTypeColor = (type: EngineRule['type']): 'primary' | 'secondary' | 'success' | 'warning' => {
    switch (type) {
      case 'signal_filter': return 'primary';
      case 'risk_check': return 'warning';
      case 'dispatch_rule': return 'success';
      default: return 'primary';
    }
  };

  const getTypeLabel = (type: EngineRule['type']): string => {
    const found = RULE_TYPES.find((t) => t.value === type);
    return found ? found.label : type;
  };

  return (
    <Box>
      <Box className="flex items-center justify-between mb-6">
        <Typography variant="h6" className="text-gray-100 font-semibold">
          推理规则引擎
        </Typography>
        <Box className="flex gap-2">
          <Button variant="outlined" startIcon={<RefreshCw />} onClick={fetchRules} disabled={loading}
            sx={{ borderColor: '#4b5563', color: '#94a3b8' }}>
            刷新
          </Button>
          <Button variant="contained" startIcon={<Add />} onClick={openCreateDialog}
            sx={{ backgroundColor: '#3b82f6', '&:hover': { backgroundColor: '#2563eb' } }}>
            添加规则
          </Button>
        </Box>
      </Box>

      <Box className="space-y-2">
        {rules.length === 0 ? (
          <Typography className="text-gray-500 text-center py-12">
            {loading ? '加载中...' : '暂无推理规则'}
          </Typography>
        ) : (
          rules.map((rule) => (
            <Box
              key={rule.rule_id}
              className="bg-gray-900 border border-gray-700 rounded-xl p-4 flex items-center gap-4 hover:border-gray-600 transition-colors"
            >
              <DragIndicator sx={{ color: '#4b5563', cursor: 'grab' }} />
              <Box className="flex-1 min-w-0">
                <Box className="flex items-center gap-3 mb-1">
                  <Typography className="font-semibold text-gray-200">{rule.name}</Typography>
                  <Chip
                    label={getTypeLabel(rule.type)}
                    size="small"
                    color={getTypeColor(rule.type)}
                    sx={{ fontSize: 10, height: 20 }}
                  />
                  <Chip
                    label={`优先级: ${rule.priority}`}
                    size="small"
                    variant="outlined"
                    sx={{ fontSize: 10, height: 20, borderColor: '#4b5563', color: '#94a3b8' }}
                  />
                </Box>
                <Typography variant="body2" className="text-gray-500 truncate">{rule.description}</Typography>
                <Typography variant="caption" className="text-gray-600">{rule.updated_at}</Typography>
              </Box>
              <Switch
                checked={rule.enabled}
                onChange={() => handleToggle(rule)}
                size="small"
                sx={{
                  '& .MuiSwitch-switchBase.Mui-checked': { color: '#22c55e' },
                  '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: '#22c55e' },
                }}
              />
              <IconButton size="small" onClick={() => openEditDialog(rule)} sx={{ color: '#94a3b8' }}>
                <Edit sx={{ fontSize: 16 }} />
              </IconButton>
              <IconButton size="small" onClick={() => handleDelete(rule.rule_id)} sx={{ color: '#ef4444' }}>
                <Delete sx={{ fontSize: 16 }} />
              </IconButton>
            </Box>
          ))
        )}
      </Box>

      {/* Add/Edit Dialog */}
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        PaperProps={{ sx: { backgroundColor: '#111118', border: '1px solid #2a2a3a', borderRadius: 3, minWidth: 480 } }}
      >
        <DialogTitle sx={{ color: '#f1f5f9' }}>
          {editingRule ? '编辑规则' : '添加规则'}
        </DialogTitle>
        <DialogContent>
          <Box className="space-y-3 mt-2">
            <TextField
              fullWidth
              size="small"
              label="名称"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              InputLabelProps={{ sx: { color: '#94a3b8' } }}
              sx={{
                '& .MuiOutlinedInput-root': {
                  backgroundColor: '#1a1a24',
                  '& fieldset': { borderColor: '#2a2a3a' },
                },
                '& .MuiInputBase-input': { color: '#f1f5f9' },
              }}
            />
            <TextField
              fullWidth
              size="small"
              select
              label="类型"
              value={formData.type}
              onChange={(e) => setFormData({ ...formData, type: e.target.value as EngineRule['type'] })}
              SelectProps={{ native: true }}
              InputLabelProps={{ sx: { color: '#94a3b8' } }}
              sx={{
                '& .MuiOutlinedInput-root': {
                  backgroundColor: '#1a1a24',
                  '& fieldset': { borderColor: '#2a2a3a' },
                },
                '& .MuiInputBase-input': { color: '#f1f5f9' },
                '& .MuiSvgIcon-root': { color: '#94a3b8' },
              }}
            >
              {RULE_TYPES.map((rt) => (
                <option key={rt.value} value={rt.value}>{rt.label}</option>
              ))}
            </TextField>
            <TextField
              fullWidth
              size="small"
              label="优先级"
              type="number"
              value={formData.priority}
              onChange={(e) => setFormData({ ...formData, priority: Number(e.target.value) })}
              InputLabelProps={{ sx: { color: '#94a3b8' } }}
              sx={{
                '& .MuiOutlinedInput-root': {
                  backgroundColor: '#1a1a24',
                  '& fieldset': { borderColor: '#2a2a3a' },
                },
                '& .MuiInputBase-input': { color: '#f1f5f9' },
              }}
            />
            <Box className="flex items-center gap-2">
              <Typography sx={{ color: '#94a3b8', fontSize: 14 }}>启用</Typography>
              <Switch
                checked={formData.enabled}
                onChange={(e) => setFormData({ ...formData, enabled: e.target.checked })}
                sx={{
                  '& .MuiSwitch-switchBase.Mui-checked': { color: '#22c55e' },
                  '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: '#22c55e' },
                }}
              />
            </Box>
            <TextField
              fullWidth
              size="small"
              label="描述"
              multiline
              minRows={2}
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              InputLabelProps={{ sx: { color: '#94a3b8' } }}
              sx={{
                '& .MuiOutlinedInput-root': {
                  backgroundColor: '#1a1a24',
                  '& fieldset': { borderColor: '#2a2a3a' },
                },
                '& .MuiInputBase-input': { color: '#f1f5f9' },
              }}
            />
            <TextField
              fullWidth
              size="small"
              label="配置 JSON（可选）"
              multiline
              minRows={3}
              placeholder='{"key": "value"}'
              value={formData.config}
              onChange={(e) => setFormData({ ...formData, config: e.target.value })}
              InputLabelProps={{ sx: { color: '#94a3b8' } }}
              sx={{
                '& .MuiOutlinedInput-root': {
                  backgroundColor: '#1a1a24',
                  '& fieldset': { borderColor: '#2a2a3a' },
                },
                '& .MuiInputBase-input': { color: '#f1f5f9', fontFamily: 'monospace', fontSize: 13 },
              }}
            />
          </Box>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setDialogOpen(false)} sx={{ color: '#94a3b8' }}>取消</Button>
          <Button onClick={handleSave} variant="contained" sx={{ backgroundColor: '#3b82f6' }}>保存</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

export default EngineRules;
