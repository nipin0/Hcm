import React, { useState, useEffect, useCallback } from 'react';
import { Box, Typography, Button, Dialog, DialogTitle, DialogContent, DialogActions, TextField, IconButton, Chip } from '@mui/material';
import { Add, Edit, Delete, RefreshCw } from '../../components/Icons';
import client from '../../api/client';

interface User {
  user_id: number;
  username: string;
  display_name: string;
  role_id: number;
  role_name: string;
  is_active: boolean;
  last_login: string | null;
  created_at: string;
  updated_at: string;
}

const ROLES: string[] = ['admin', 'operator', 'viewer', 'trader'];

const Users: React.FC = () => {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [dialogOpen, setDialogOpen] = useState<boolean>(false);
  const [editingUser, setEditingUser] = useState<User | null>(null);
  const [formData, setFormData] = useState({ username: '', password: '', role: 'viewer' });

  const fetchUsers = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const { data } = await client.get('/api/system/users');
      setUsers(data.data?.items || []);
    } catch {
      // Silent fail, show empty state
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchUsers();
  }, [fetchUsers]);

  const openCreateDialog = (): void => {
    setEditingUser(null);
    setFormData({ username: '', password: '', role: 'viewer' });
    setDialogOpen(true);
  };

  const openEditDialog = (user: User): void => {
    setEditingUser(user);
    setFormData({ username: user.username, password: '', role: user.role_name });
    setDialogOpen(true);
  };

  const handleSave = async (): Promise<void> => {
    try {
      if (editingUser) {
        await client.put(`/api/system/users/${editingUser.user_id}`, formData);
      } else {
        await client.post('/api/system/users', formData);
      }
      setDialogOpen(false);
      fetchUsers();
    } catch {
      // Handle error silently
    }
  };

  const handleDelete = async (id: number): Promise<void> => {
    if (!window.confirm('确认删除此用户？')) return;
    try {
      await client.delete(`/api/system/users/${id}`);
      fetchUsers();
    } catch {
      // Handle error silently
    }
  };

  const roleColor = (role: string): 'primary' | 'secondary' | 'default' | 'warning' => {
    switch (role) {
      case 'admin': return 'primary';
      case 'operator': return 'warning';
      case 'trader': return 'secondary';
      default: return 'default';
    }
  };

  return (
    <Box>
      <Box className="flex items-center justify-between mb-6">
        <Typography variant="h6" className="text-gray-100 font-semibold">
          用户与权限管理
        </Typography>
        <Box className="flex gap-2">
          <Button
            variant="outlined"
            startIcon={<RefreshCw />}
            onClick={fetchUsers}
            disabled={loading}
            sx={{ borderColor: '#4b5563', color: '#94a3b8' }}
          >
            刷新
          </Button>
          <Button
            variant="contained"
            startIcon={<Add />}
            onClick={openCreateDialog}
            sx={{ backgroundColor: '#3b82f6', '&:hover': { backgroundColor: '#2563eb' } }}
          >
            添加用户
          </Button>
        </Box>
      </Box>

      <Box className="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-gray-700 bg-gray-800">
              <th className="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase">用户名</th>
              <th className="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase">角色</th>
              <th className="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase">状态</th>
              <th className="text-left py-3 px-4 text-xs font-medium text-gray-400 uppercase">创建时间</th>
              <th className="text-right py-3 px-4 text-xs font-medium text-gray-400 uppercase">操作</th>
            </tr>
          </thead>
          <tbody>
            {users.length === 0 ? (
              <tr>
                <td colSpan={5} className="py-8 text-center text-gray-500">
                  {loading ? '加载中...' : '暂无用户数据'}
                </td>
              </tr>
            ) : (
              users.map((user) => (
                <tr key={user.user_id} className="border-b border-gray-800 hover:bg-gray-800/50 transition-colors">
                  <td className="py-3 px-4 text-sm text-gray-200 font-medium">{user.username}</td>
                  <td className="py-3 px-4">
                    <Chip
                      label={user.role_name}
                      size="small"
                      color={roleColor(user.role_name)}
                      sx={{ fontSize: 11, height: 22 }}
                    />
                  </td>
                  <td className="py-3 px-4">
                    <span className={`inline-block w-2 h-2 rounded-full mr-2 ${user.is_active ? 'bg-green-500' : 'bg-gray-500'}`} />
                    <span className="text-sm text-gray-400">{user.is_active ? '活跃' : '禁用'}</span>
                  </td>
                  <td className="py-3 px-4 text-sm text-gray-500">{user.created_at}</td>
                  <td className="py-3 px-4 text-right">
                    <IconButton size="small" onClick={() => openEditDialog(user)} sx={{ color: '#94a3b8' }}>
                      <Edit sx={{ fontSize: 16 }} />
                    </IconButton>
                    <IconButton size="small" onClick={() => handleDelete(user.user_id)} sx={{ color: '#ef4444' }}>
                      <Delete sx={{ fontSize: 16 }} />
                    </IconButton>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </Box>

      {/* Add/Edit Dialog */}
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        PaperProps={{ sx: { backgroundColor: '#111118', border: '1px solid #2a2a3a', borderRadius: 3, minWidth: 420 } }}
      >
        <DialogTitle sx={{ color: '#f1f5f9' }}>
          {editingUser ? '编辑用户' : '添加用户'}
        </DialogTitle>
        <DialogContent>
          <Box className="space-y-3 mt-2">
            <TextField
              fullWidth
              size="small"
              label="用户名"
              value={formData.username}
              onChange={(e) => setFormData({ ...formData, username: e.target.value })}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            />
            <TextField
              fullWidth
              size="small"
              type="password"
              label="密码"
              value={formData.password}
              onChange={(e) => setFormData({ ...formData, password: e.target.value })}
              placeholder={editingUser ? '留空则不修改' : ''}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            />
            <TextField
              fullWidth
              size="small"
              select
              label="角色"
              value={formData.role}
              onChange={(e) => setFormData({ ...formData, role: e.target.value })}
              SelectProps={{ native: true }}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </TextField>
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

export default Users;
