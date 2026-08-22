import React, { useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import {
  Box,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Collapse,
  Breadcrumbs,
  Link,
  Typography,
} from '@mui/material';
import {
  Dashboard,
  Settings,
  CellTower,
  CloudDownload,
  Psychology,
  Send,
  Shield,
  Cancel,
  ContentCopy,
  Tune,
  ExpandLess,
  ExpandMore,
  ChevronRight,
} from './Icons';
import SymbolSelector from './SymbolSelector';

interface NavGroup {
  label: string;
  icon: React.ReactNode;
  basePath: string;
  children: { label: string; path: string }[];
}

const navGroups: NavGroup[] = [
  {
    label: '数据看板',
    icon: <Dashboard />,
    basePath: '/dashboard',
    children: [
      { label: '实时信号流', path: '/dashboard/realtime' },
      { label: '和乘幂信号状态', path: '/hexp/dashboard' },
      { label: '信号漏斗', path: '/dashboard/funnel' },
      { label: '校准时序', path: '/dashboard/calibration-timeline' },
      { label: '仓位与盈亏', path: '/dashboard/positions' },
      { label: '统计分析', path: '/dashboard/statistics' },
      { label: '系统健康', path: '/dashboard/health' },
      { label: '外部因子', path: '/dashboard/factors' },
      { label: '品种对比', path: '/dashboard/compare' },
    ],
  },
  {
    label: '系统设置',
    icon: <Settings />,
    basePath: '/system',
    children: [
      { label: '用户与权限', path: '/system/users' },
      { label: '网络与端口', path: '/system/network' },
      { label: 'MT5 接入', path: '/system/mt5' },
      { label: 'DeepSeek AI', path: '/system/deepseek' },
      { label: '通知', path: '/system/notifications' },
      { label: '缓存', path: '/system/cache' },
    ],
  },
  {
    label: '信号塔',
    icon: <CellTower />,
    basePath: '/signal-tower',
    children: [
      { label: '信号模式与市况', path: '/signal-tower/mode' },
      { label: '评分阈值（公用）', path: '/signal-tower/threshold' },
      { label: 'Prompt 模板', path: '/signal-tower/prompt' },
      { label: '看门狗状态', path: '/signal-tower/watchdog' },
      { label: '品种级配置', path: '/signal-tower/symbol-config' },
    ],
  },
  {
    label: '数据源',
    icon: <CloudDownload />,
    basePath: '/datasource',
    children: [
      { label: '数据源管理', path: '/datasource' },
    ],
  },
  {
    label: '推理引擎',
    icon: <Psychology />,
    basePath: '/engine',
    children: [
      { label: '推理规则', path: '/engine/rules' },
      { label: 'AI 信号质量', path: '/engine/ai-quality' },
      { label: 'AI 报表', path: '/engine/ai-report' },
      { label: '模型重训报表', path: '/engine/model-report' },
      { label: '模型监控(PSI/漂移)', path: '/engine/model-monitor' },
    ],
  },
  {
    label: '分发',
    icon: <Send />,
    basePath: '/dispatch',
    children: [
      { label: '分发配置', path: '/dispatch' },
    ],
  },
  {
    label: '风控',
    icon: <Shield />,
    basePath: '/risk',
    children: [
      { label: '风控配置', path: '/risk' },
    ],
  },
  {
    label: '平仓',
    icon: <Cancel />,
    basePath: '/close',
    children: [
      { label: '平仓配置', path: '/close' },
    ],
  },
  {
    label: '跟单',
    icon: <ContentCopy />,
    basePath: '/copy',
    children: [
      { label: '跟单配置', path: '/copy' },
    ],
  },
];

interface LayoutProps {
  children: React.ReactNode;
}

const Layout: React.FC<LayoutProps> = ({ children }) => {
  const navigate = useNavigate();
  const location = useLocation();
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>(
    navGroups.reduce(
      (acc, g) => {
        acc[g.label] = true;
        return acc;
      },
      {} as Record<string, boolean>,
    ),
  );

  const toggleGroup = (label: string): void => {
    setExpandedGroups((prev) => ({ ...prev, [label]: !prev[label] }));
  };

  // Build breadcrumbs from current path
  const pathParts: string[] = location.pathname.split('/').filter(Boolean);
  const breadcrumbs: { label: string; path: string }[] = [];
  let accumulatedPath: string = '';
  for (const part of pathParts) {
    accumulatedPath += `/${part}`;
    // Find label from navGroups
    let label: string = part;
    for (const group of navGroups) {
      if (accumulatedPath === group.basePath) {
        label = group.label;
        break;
      }
      for (const child of group.children) {
        if (accumulatedPath === child.path) {
          label = child.label;
          break;
        }
      }
    }
    breadcrumbs.push({ label, path: accumulatedPath });
  }

  return (
    <Box className="flex flex-1 overflow-hidden">
      {/* Left Sidebar — 260px */}
      <Box
        className="w-[260px] bg-gray-900 border-r border-gray-700 flex flex-col overflow-hidden shrink-0"
      >
        {/* Symbol Selector */}
        <Box className="p-3 border-b border-gray-700">
          <SymbolSelector />
        </Box>

        {/* Navigation */}
        <Box className="flex-1 overflow-y-auto py-2">
          <List component="nav" dense>
            {navGroups.map((group) => {
              const isExpanded: boolean = expandedGroups[group.label] || false;
              const isActive: boolean = location.pathname.startsWith(group.basePath);

              return (
                <Box key={group.label}>
                  <ListItemButton
                    onClick={() => toggleGroup(group.label)}
                    selected={isActive}
                    sx={{
                      px: 2,
                      py: 1,
                      '&.Mui-selected': {
                        backgroundColor: 'rgba(59,130,246,0.1)',
                        borderRight: '3px solid #3b82f6',
                      },
                      '&:hover': {
                        backgroundColor: 'rgba(255,255,255,0.04)',
                      },
                    }}
                  >
                    <ListItemIcon sx={{ minWidth: 36, color: isActive ? '#3b82f6' : '#94a3b8' }}>
                      {group.icon}
                    </ListItemIcon>
                    <ListItemText
                      primary={group.label}
                      primaryTypographyProps={{
                        fontSize: 13,
                        fontWeight: isActive ? 600 : 400,
                        color: isActive ? '#f1f5f9' : '#94a3b8',
                      }}
                    />
                    {isExpanded ? <ExpandLess sx={{ fontSize: 18, color: '#64748b' }} /> : <ExpandMore sx={{ fontSize: 18, color: '#64748b' }} />}
                  </ListItemButton>
                  <Collapse in={isExpanded} timeout="auto" unmountOnExit>
                    <List component="div" disablePadding dense>
                      {group.children.map((child) => {
                        const childActive: boolean = location.pathname === child.path;
                        return (
                          <ListItemButton
                            key={child.path}
                            onClick={() => navigate(child.path)}
                            selected={childActive}
                            sx={{
                              pl: 6,
                              py: 0.75,
                              '&.Mui-selected': {
                                backgroundColor: 'rgba(59,130,246,0.08)',
                                borderRight: '3px solid #3b82f6',
                              },
                            }}
                          >
                            <ListItemIcon sx={{ minWidth: 20 }}>
                              <ChevronRight sx={{ fontSize: 14, color: childActive ? '#3b82f6' : '#475569' }} />
                            </ListItemIcon>
                            <ListItemText
                              primary={child.label}
                              primaryTypographyProps={{
                                fontSize: 12.5,
                                fontWeight: childActive ? 500 : 400,
                                color: childActive ? '#e2e8f0' : '#94a3b8',
                              }}
                            />
                          </ListItemButton>
                        );
                      })}
                    </List>
                  </Collapse>
                </Box>
              );
            })}
          </List>
        </Box>
      </Box>

      {/* Right Content Area */}
      <Box className="flex-1 flex flex-col overflow-hidden">
        {/* Breadcrumbs */}
        <Box className="px-6 py-2 bg-gray-900 border-b border-gray-700 shrink-0">
          <Breadcrumbs
            separator={<ChevronRight sx={{ fontSize: 14, color: '#64748b' }} />}
            aria-label="breadcrumb"
          >
            <Link
              underline="hover"
              color="inherit"
              onClick={() => navigate('/')}
              sx={{ cursor: 'pointer', fontSize: 12, color: '#64748b' }}
            >
              HCM
            </Link>
            {breadcrumbs.map((crumb) => (
              <Typography key={crumb.path} sx={{ fontSize: 12, color: '#94a3b8' }}>
                {crumb.label}
              </Typography>
            ))}
          </Breadcrumbs>
        </Box>

        {/* Page Content */}
        <Box className="flex-1 overflow-y-auto p-6">
          {children}
        </Box>
      </Box>
    </Box>
  );
};

export default Layout;
