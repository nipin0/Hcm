import React, { useEffect, useState, useCallback } from 'react';
import { Box, Button, Dialog, DialogTitle, DialogContent, DialogActions, TextField, MenuItem, Switch } from '@mui/material';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import { Add, Edit, Delete } from '../../components/Icons';
import client from '../../api/client';

interface Account {
  account_id: number;
  account_name: string;
  account_number: number;
  server_name: string;
  broker_name: string;
  account_type: 'master' | 'follower';
  env_type: number;
  leverage: string;
  base_currency: string;
  is_active: boolean;
  terminal_path?: string | null;
  status?: 'running' | 'stopped' | 'paused';
  last_balance: string;
  last_equity: string;
  last_heartbeat: string | null;
  created_at: string;
  updated_at: string | null;
}

const fields: ConfigField[] = [
  { key: 'server', label: 'MT5 服务器地址', type: 'text', defaultValue: '', required: true, placeholder: '例如: demo.mt5server.com:443' },
  { key: 'login', label: 'MT5 登录账号', type: 'number', defaultValue: 0 },
  { key: 'password', label: 'MT5 密码', type: 'password', defaultValue: '' },
  { key: 'trade_mode', label: '交易模式', type: 'select', defaultValue: 'demo', options: [
    { label: '模拟账户', value: 'demo' },
    { label: '实盘账户', value: 'live' },
    { label: '比赛账户', value: 'contest' },
  ]},
  { key: 'account_type', label: '账户类型', type: 'select', defaultValue: 'master', options: [
    { label: '主号', value: 'master' },
    { label: '跟单号', value: 'follower' },
  ]},
  { key: 'max_spread', label: '最大允许点差', type: 'number', defaultValue: 30, min: 0, max: 1000 },
  { key: 'max_slippage', label: '最大允许滑点', type: 'number', defaultValue: 3, min: 0, max: 50 },
  { key: 'auto_reconnect', label: '自动重连', type: 'switch', defaultValue: true },
  { key: 'reconnect_interval', label: '重连间隔 (秒)', type: 'number', defaultValue: 5, min: 1, max: 60 },
  { key: 'heartbeat_interval', label: '心跳间隔 (秒)', type: 'number', defaultValue: 30, min: 5, max: 300 },
];

const styles: Record<string, React.CSSProperties> = {
  card: { background: '#111118', border: '1px solid #2a2a3a', borderRadius: 12, padding: 24, marginTop: 24 },
  header: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 },
  title: { color: '#f1f5f9', fontSize: 16, fontWeight: 600, margin: 0 },
  count: { color: '#94a3b8', fontSize: 13, backgroundColor: '#1a1a2e', padding: '2px 10px', borderRadius: 12 },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { textAlign: 'left', padding: '10px 12px', color: '#94a3b8', fontWeight: 500, fontSize: 12, textTransform: 'uppercase', borderBottom: '1px solid #2a2a3a' },
  td: { padding: '10px 12px', color: '#f1f5f9', borderBottom: '1px solid #1e1e2e' },
  badgeBase: { display: 'inline-block', padding: '2px 10px', borderRadius: 9999, fontSize: 12, fontWeight: 600 },
  badgeMaster: { backgroundColor: 'rgba(59, 130, 246, 0.15)', color: '#60a5fa' },
  badgeFollower: { backgroundColor: 'rgba(168, 85, 247, 0.15)', color: '#c084fc' },
  badgeActive: { backgroundColor: 'rgba(34, 197, 94, 0.15)', color: '#4ade80' },
  badgeInactive: { backgroundColor: 'rgba(239, 68, 68, 0.15)', color: '#f87171' },
  badgeRunning: { backgroundColor: 'rgba(34, 197, 94, 0.15)', color: '#4ade80' },
  badgeStopped: { backgroundColor: 'rgba(100, 116, 139, 0.15)', color: '#94a3b8' },
  badgePaused: { backgroundColor: 'rgba(234, 179, 8, 0.15)', color: '#facc15' },
  empty: { textAlign: 'center', padding: 40, color: '#64748b', fontSize: 14 },
  heartbeat: { color: '#94a3b8', fontSize: 12 },
  iconBtn: { background: 'none', border: 'none', cursor: 'pointer', color: '#60a5fa', padding: '4px 8px', borderRadius: 6, display: 'inline-flex', alignItems: 'center' },
  delBtn: { background: 'none', border: 'none', cursor: 'pointer', color: '#ef4444', padding: '4px 8px', borderRadius: 6, display: 'inline-flex', alignItems: 'center' },
  addBtn: { background: '#3b82f6', color: '#fff', border: 'none', borderRadius: 6, padding: '8px 14px', fontSize: 13, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6 },
};

function formatHeartbeat(ts: string | null): string {
  if (!ts) return '—';
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

interface AccountFormData {
  account_name: string;
  account_number: string;
  server_name: string;
  broker_name: string;
  account_type: 'master' | 'follower';
  password: string;
  leverage: string;
  base_currency: string;
  terminal_path: string;
}

const MT5: React.FC = () => {
  const [initialValues, setInitialValues] = useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountsLoading, setAccountsLoading] = useState(true);

  // Edit dialog state
  const [editOpen, setEditOpen] = useState(false);
  const [editingAccount, setEditingAccount] = useState<Account | null>(null);
  const [formData, setFormData] = useState<AccountFormData>({
    account_name: '', account_number: '', server_name: '', broker_name: '',
    account_type: 'master', password: '', leverage: '100', base_currency: 'USD',
    terminal_path: '',
  });
  const [saving, setSaving] = useState(false);

  const fetchConfig = useCallback(() => {
    client.get('/api/system/mt5')
      .then(({ data: resp }) => {
        const d = resp.data || resp;
        setInitialValues((prev) => {
          // Force re-render with new values (ConfigForm watches ref)
          return { ...d };
        });
      })
      .catch(() => {});
  }, []);

  const fetchAccounts = useCallback(async () => {
    setAccountsLoading(true);
    try {
      const { data: resp } = await client.get('/api/v1/system/accounts');
      const items: Account[] = resp?.data?.items ?? resp?.items ?? [];
      setAccounts(items);
    } catch {
      // silent
    } finally {
      setAccountsLoading(false);
    }
  }, []);

  useEffect(() => { fetchConfig(); }, [fetchConfig]);
  useEffect(() => { fetchAccounts(); }, [fetchAccounts]);

  const handleDelete = async (accountId: number) => {
    if (!window.confirm('确认删除此账户？删除后将一并清除该账户相关的信号、订单、持仓与跟单关系记录，并释放对应的桥实例。')) return;
    try {
      const { data: resp } = await client.delete(`/api/v1/system/accounts/${accountId}`);
      // 后端在失败(如外键冲突/账户不存在)时仍返回 HTTP 200 + code!=0，必须显式检查，
      // 否则会被 axios 当成功处理，导致“删除看似未生效”。
      if (resp?.code !== 0 && resp?.code !== undefined) {
        const msg = resp?.message || '未知错误';
        window.alert('账户删除失败：' + msg);
        return;
      }
      await fetchAccounts();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        (err as { message?: string })?.message ||
        '未知错误';
      window.alert('账户删除失败：' + msg);
    }
  };

  const openEditDialog = (acc: Account): void => {
    setEditingAccount(acc);
    setFormData({
      account_name: acc.account_name,
      account_number: String(acc.account_number),
      server_name: acc.server_name,
      broker_name: acc.broker_name,
      account_type: acc.account_type,
      password: '', // empty = keep unchanged
      leverage: acc.leverage || '100',
      base_currency: acc.base_currency || 'USD',
      terminal_path: acc.terminal_path || '',
    });
    setEditOpen(true);
  };

  const openCreateDialog = (): void => {
    setEditingAccount(null);
    setFormData({
      account_name: '', account_number: '', server_name: '', broker_name: '',
      account_type: 'master', password: '', leverage: '100', base_currency: 'USD',
      terminal_path: '',
    });
    setEditOpen(true);
  };

  const handleSaveAccount = async (): Promise<void> => {
    if (!formData.account_name || !formData.account_number) {
      window.alert('请填写账户名和账号');
      return;
    }
    setSaving(true);
    try {
      const payload = {
        account_name: formData.account_name,
        account_number: parseInt(formData.account_number, 10),
        server_name: formData.server_name,
        broker_name: formData.broker_name,
        account_type: formData.account_type,
        leverage: parseInt(formData.leverage, 10) || 100,
        base_currency: formData.base_currency,
        terminal_path: formData.terminal_path,
        env_type: 1, // demo
      };
      if (editingAccount) {
        // Update existing
        await client.put(`/api/system/accounts/${editingAccount.account_id}`, payload);
      } else {
        // Create new
        await client.post('/api/system/accounts', payload);
      }
      // Update password separately if provided
      if (formData.password && formData.password.length > 0) {
        try {
          await client.put(`/api/system/accounts/${editingAccount?.account_id || 0}/password`, {
            password: formData.password,
          });
        } catch {
          // ignore
        }
      }
      setEditOpen(false);
      fetchAccounts();
    } catch (err) {
      window.alert(`保存失败: ${err instanceof Error ? err.message : '未知错误'}`);
    } finally {
      setSaving(false);
    }
  };

  const handleToggleStatus = async (acc: Account): Promise<void> => {
    const next = acc.status === 'running' ? 'stopped' : 'running';
    const prevStatus = acc.status;
    // optimistic update
    setAccounts(prev => prev.map(a => a.account_id === acc.account_id ? { ...a, status: next } : a));
    try {
      await client.put(`/api/v1/system/accounts/${acc.account_id}/status`, { status: next });
    } catch {
      // revert on failure
      setAccounts(prev => prev.map(a => a.account_id === acc.account_id ? { ...a, status: prevStatus } : a));
      window.alert('切换跟单启停失败');
    }
  };

  const handleToggleActive = async (acc: Account): Promise<void> => {
    const next = !acc.is_active;
    const prevActive = acc.is_active;
    // optimistic update
    setAccounts(prev => prev.map(a => a.account_id === acc.account_id ? { ...a, is_active: next } : a));
    try {
      await client.put(`/api/v1/system/accounts/${acc.account_id}/active`, { is_active: next });
      fetchAccounts();
    } catch {
      // revert on failure
      setAccounts(prev => prev.map(a => a.account_id === acc.account_id ? { ...a, is_active: prevActive } : a));
      window.alert(next ? '启用账户失败' : '停用账户失败');
    }
  };

  const handleSubmit = useCallback(async (values: Record<string, string | number | boolean>): Promise<void> => {
    await client.put('/api/system/mt5', values);
    // Re-fetch config before formKey remount so initialValues are fresh
    try {
      const { data: resp } = await client.get('/api/system/mt5');
      const d = resp.data || resp;
      setInitialValues({ ...d });
    } catch { /* ignore */ }
    setFormKey(k => k + 1);
    fetchAccounts();
  }, [fetchAccounts]);

  return (
    <>
      <ConfigForm
        formKey={formKey}
        title="MT5 接入配置"
        fields={fields}
        initialValues={initialValues}
        onSubmit={handleSubmit}
        apiEndpoint="PUT /api/system/mt5"
      />

      <div style={styles.card}>
        <div style={styles.header}>
          <h3 style={styles.title}>账户列表</h3>
          <div className="flex items-center gap-2">
            <span style={styles.count}>
              {accountsLoading ? '加载中...' : `${accounts.length} 个账户`}
            </span>
            <button style={styles.addBtn} onClick={openCreateDialog}>
              <Add sx={{ fontSize: 16 }} /> 新建账户
            </button>
          </div>
        </div>

        {!accountsLoading && accounts.length === 0 ? (
          <div style={styles.empty}>暂无账户数据</div>
        ) : (
          <table style={styles.table}>
            <thead>
              <tr>
                <th style={styles.th}>账户名</th>
                <th style={styles.th}>账号</th>
                <th style={styles.th}>券商</th>
                <th style={styles.th}>服务器</th>
                <th style={styles.th}>终端路径</th>
                <th style={styles.th}>类型</th>
                <th style={styles.th}>货币</th>
                <th style={styles.th}>杠杆</th>
                <th style={styles.th}>余额</th>
                <th style={styles.th}>净值</th>
                <th style={styles.th}>账号状态</th>
                <th style={styles.th}>跟单启停</th>
                <th style={styles.th}>最后心跳</th>
                <th style={{ ...styles.th, textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((acc) => (
                <tr key={acc.account_id}>
                  <td style={styles.td}>{acc.account_name}</td>
                  <td style={styles.td}>{acc.account_number}</td>
                  <td style={styles.td}>{acc.broker_name}</td>
                  <td style={styles.td}>{acc.server_name}</td>
                  <td style={{ ...styles.td, maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={acc.terminal_path || ''}>{acc.terminal_path || '—'}</td>
                  <td style={styles.td}>
                    <span style={{
                      ...styles.badgeBase,
                      ...(acc.account_type === 'master' ? styles.badgeMaster : styles.badgeFollower),
                    }}>
                      {acc.account_type === 'master' ? '主号' : '跟单号'}
                    </span>
                  </td>
                  <td style={styles.td}>{acc.base_currency}</td>
                  <td style={styles.td}>1:{acc.leverage}</td>
                  <td style={styles.td}>{acc.last_balance}</td>
                  <td style={styles.td}>{acc.last_equity}</td>
                  <td style={styles.td}>
                    <span style={{
                      ...styles.badgeBase,
                      ...(acc.is_active ? styles.badgeActive : styles.badgeInactive),
                    }}>
                      {acc.is_active ? '启用' : '禁用'}
                    </span>
                  </td>
                  <td style={styles.td}>
                    <Box className="flex items-center gap-2">
                      <Switch
                        checked={acc.status === 'running'}
                        onChange={() => handleToggleStatus(acc)}
                        size="small"
                        sx={{
                          '& .MuiSwitch-switchBase.Mui-checked': { color: '#4ade80' },
                          '& .MuiSwitch-track': { opacity: 0.5 },
                        }}
                      />
                      <span style={{
                        ...styles.badgeBase,
                        ...(acc.status === 'running' ? styles.badgeRunning : acc.status === 'paused' ? styles.badgePaused : styles.badgeStopped),
                      }}>
                        {acc.status === 'running' ? '运行中' : acc.status === 'paused' ? '暂停' : '已停止'}
                      </span>
                    </Box>
                  </td>
                  <td style={{ ...styles.td, ...styles.heartbeat }}>
                    {formatHeartbeat(acc.last_heartbeat)}
                  </td>
                  <td style={{ ...styles.td, textAlign: 'right' }}>
                    <button style={styles.iconBtn} title="编辑账户" onClick={() => openEditDialog(acc)}>
                      <Edit sx={{ fontSize: 16 }} />
                    </button>
                    <button
                      style={acc.is_active ? { ...styles.delBtn, fontSize: 12, padding: '2px 8px' } : { ...styles.addBtn, fontSize: 12, padding: '2px 8px' }}
                      title={acc.is_active ? '停用账户（保留在列表）' : '启用账户'}
                      onClick={() => handleToggleActive(acc)}
                    >
                      {acc.is_active ? '停用' : '启用'}
                    </button>
                    <button style={styles.delBtn} title="删除账户（真删除）" onClick={() => handleDelete(acc.account_id)}>
                      <Delete sx={{ fontSize: 16 }} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* ── Edit / Create Account Dialog ──────── */}
      <Dialog
        open={editOpen}
        onClose={() => setEditOpen(false)}
        maxWidth="sm"
        fullWidth
        PaperProps={{
          sx: {
            backgroundColor: '#111118',
            border: '1px solid #2a2a3a',
            color: '#f1f5f9',
          },
        }}
      >
        <DialogTitle sx={{ color: '#f1f5f9', fontWeight: 600 }}>
          {editingAccount ? `编辑账户 #${editingAccount.account_id}` : '新建账户'}
        </DialogTitle>
        <DialogContent>
          <Box className="space-y-3 mt-2">
            <TextField
              fullWidth size="small" label="账户名"
              value={formData.account_name}
              onChange={(e) => setFormData({ ...formData, account_name: e.target.value })}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            />
            <TextField
              fullWidth size="small" label="MT5 账号" type="number"
              value={formData.account_number}
              onChange={(e) => setFormData({ ...formData, account_number: e.target.value })}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            />
            <TextField
              fullWidth size="small" label="MT5 服务器"
              value={formData.server_name}
              onChange={(e) => setFormData({ ...formData, server_name: e.target.value })}
              placeholder="STARTRADERFinancial-Demo"
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            />
            <TextField
              fullWidth size="small" label="券商名称"
              value={formData.broker_name}
              onChange={(e) => setFormData({ ...formData, broker_name: e.target.value })}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            />
            <TextField
              fullWidth size="small" label="MT5 密码" type="password"
              value={formData.password}
              onChange={(e) => setFormData({ ...formData, password: e.target.value })}
              placeholder={editingAccount ? '留空则不修改' : 'MT5 交易密码'}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            />
            <TextField
              fullWidth size="small" label="MT5 终端路径（系统自动发现）"
              value={formData.terminal_path}
              disabled
              placeholder={editingAccount ? '系统自动发现' : '新建后由系统自动发现'}
              helperText="终端路径由系统在 MT5 登录后自动发现，无需手动填写"
              InputProps={{ readOnly: true }}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' }, mt: 1 }}
            />
            <TextField
              fullWidth size="small" select label="账户类型"
              value={formData.account_type}
              onChange={(e) => setFormData({ ...formData, account_type: e.target.value as 'master' | 'follower' })}
              SelectProps={{ native: true }}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            >
              <option value="master">主号</option>
              <option value="follower">跟单号</option>
            </TextField>
            <Box className="grid grid-cols-2 gap-2">
              <TextField
                fullWidth size="small" label="杠杆" type="number"
                value={formData.leverage}
                onChange={(e) => setFormData({ ...formData, leverage: e.target.value })}
                sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
              />
              <TextField
                fullWidth size="small" label="基础货币"
                value={formData.base_currency}
                onChange={(e) => setFormData({ ...formData, base_currency: e.target.value })}
                sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
              />
            </Box>
          </Box>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setEditOpen(false)} sx={{ color: '#94a3b8' }}>取消</Button>
          <Button
            onClick={handleSaveAccount}
            disabled={saving}
            variant="contained"
            sx={{ backgroundColor: '#3b82f6', '&:hover': { backgroundColor: '#2563eb' } }}
          >
            {saving ? '保存中...' : '保存'}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
};

export default MT5;
