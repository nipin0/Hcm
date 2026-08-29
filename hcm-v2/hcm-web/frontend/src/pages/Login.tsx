import React, { useState } from 'react';
import { Box, TextField, Button, Typography, Alert, CircularProgress } from '@mui/material';
import { Login as LoginIcon, Visibility, VisibilityOff } from "../components/Icons";
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

const Login: React.FC = () => {
  const [username, setUsername] = useState<string>('');
  const [password, setPassword] = useState<string>('');
  const [showPassword, setShowPassword] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

  const { login, isAuthenticated } = useAuth();
  const navigate = useNavigate();

  // Already logged in
  // 【2026-08-28】「实时信号流」页下线，落地页改到和乘幂信号状态（生产主引擎看板）。
  React.useEffect(() => {
    if (isAuthenticated) {
      navigate('/hexp/dashboard', { replace: true });
    }
  }, [isAuthenticated, navigate]);

  const handleSubmit = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    if (!username.trim() || !password.trim()) {
      setError('请输入用户名和密码');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      await login(username.trim(), password);
      navigate('/hexp/dashboard', { replace: true });
    } catch (err: unknown) {
      if (err && typeof err === 'object' && 'response' in err) {
        const axiosErr = err as { response?: { data?: { detail?: string }; status?: number } };
        if (axiosErr.response?.status === 401) {
          setError('用户名或密码错误');
        } else {
          setError(axiosErr.response?.data?.detail || '登录失败，请稍后重试');
        }
      } else {
        setError('网络错误，请检查连接');
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <Box className="min-h-screen flex items-center justify-center bg-gray-950 p-4">
      <Box className="w-full max-w-md">
        {/* Logo / Brand */}
        <Box className="text-center mb-8">
          <Box className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-br from-blue-600 to-purple-600 mb-4">
            <Typography sx={{ fontSize: 28, fontWeight: 800, color: '#fff' }}>H</Typography>
          </Box>
          <Typography sx={{ fontSize: 22, fontWeight: 700, color: '#f1f5f9' }}>
            HCM v2
          </Typography>
          <Typography variant="body2" className="text-gray-500 mt-1">
            对冲组合管理系统
          </Typography>
        </Box>

        {/* Login Form */}
        <Box
          component="form"
          onSubmit={handleSubmit}
          className="bg-gray-900 border border-gray-700 rounded-2xl p-8 space-y-5"
        >
          <Typography sx={{ fontSize: 18, fontWeight: 600, color: '#e2e8f0', mb: 1 }}>
            登录
          </Typography>

          {error && (
            <Alert
              severity="error"
              sx={{ backgroundColor: '#3b1111', color: '#fca5a5', '.MuiAlert-icon': { color: '#ef4444' } }}
            >
              {error}
            </Alert>
          )}

          <TextField
            fullWidth
            label="用户名"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={loading}
            autoComplete="username"
            autoFocus
            sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
          />

          <TextField
            fullWidth
            type={showPassword ? 'text' : 'password'}
            label="密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={loading}
            autoComplete="current-password"
            sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            InputProps={{
              endAdornment: (
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="text-gray-500 hover:text-gray-300 p-1"
                >
                  {showPassword ? <VisibilityOff sx={{ fontSize: 20 }} /> : <Visibility sx={{ fontSize: 20 }} />}
                </button>
              ),
            }}
          />

          <Button
            type="submit"
            fullWidth
            variant="contained"
            disabled={loading}
            startIcon={loading ? <CircularProgress size={18} /> : <LoginIcon />}
            sx={{
              py: 1.5,
              backgroundColor: '#3b82f6',
              '&:hover': { backgroundColor: '#2563eb' },
              fontWeight: 600,
            }}
          >
            {loading ? '登录中...' : '登录'}
          </Button>
        </Box>

        <Typography variant="caption" className="text-gray-600 block text-center mt-6">
          HCM v2 © 2024
        </Typography>
      </Box>
    </Box>
  );
};

export default Login;
