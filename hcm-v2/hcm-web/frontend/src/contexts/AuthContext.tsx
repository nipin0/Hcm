import React, { createContext, useContext, useState, useCallback, useEffect, ReactNode } from 'react';
import client from '../api/client';

interface AuthUser {
  id: string;
  username: string;
  email: string;
  role: string;
  permissions: string[];
}

interface AuthContextType {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  hasPermission: (permission: string) => boolean;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  isAuthenticated: false,
  isLoading: true,
  login: async () => {},
  logout: () => {},
  hasPermission: () => false,
});

export const useAuth = (): AuthContextType => useContext(AuthContext);

interface AuthProviderProps {
  children: ReactNode;
}

export const AuthProvider: React.FC<AuthProviderProps> = ({ children }) => {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);

  // Check for existing token on mount
  useEffect(() => {
    const token: string | null = localStorage.getItem('hcm_token');
    if (token) {
      client
        .get('/api/auth/me')
        .then((res) => {
          // /auth/me 响应为 {code, data: userObj, message}，需取 res.data.data 才是真实用户对象
          setUser(res.data?.data ?? res.data);
        })
        .catch(() => {
          localStorage.removeItem('hcm_token');
          localStorage.removeItem('hcm_refresh_token');
        })
        .finally(() => setIsLoading(false));
    } else {
      setIsLoading(false);
    }
  }, []);

  const login = useCallback(async (username: string, password: string): Promise<void> => {
    const { data } = await client.post('/api/auth/login', { username, password });
    localStorage.setItem('hcm_token', data.access_token);
    localStorage.setItem('hcm_refresh_token', data.refresh_token);
    setUser(data.user);
  }, []);

  const logout = useCallback((): void => {
    localStorage.removeItem('hcm_token');
    localStorage.removeItem('hcm_refresh_token');
    setUser(null);
    window.location.href = '/login';
  }, []);

  const hasPermission = useCallback(
    (permission: string): boolean => {
      if (!user) return false;
      if (user.role === 'admin') return true;
      return user.permissions.includes(permission);
    },
    [user],
  );

  const value: AuthContextType = {
    user,
    isAuthenticated: !!user,
    isLoading,
    login,
    logout,
    hasPermission,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};
