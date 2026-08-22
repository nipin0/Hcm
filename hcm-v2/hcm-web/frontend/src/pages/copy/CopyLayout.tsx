import React, { useState, Component, lazy, Suspense } from 'react';
import { Box, Tabs, Tab, Typography, Button } from '@mui/material';

const CopyRelationships = lazy(() => import('./CopyRelationships'));
const CopySymbolMappings = lazy(() => import('./CopySymbolMappings'));
const CopyHistory = lazy(() => import('./CopyHistory'));

/** Error boundary to prevent black screen on crash */
class ErrorBoundary extends Component<{ children: React.ReactNode }, { hasError: boolean; error: string }> {
  state = { hasError: false, error: '' };
  static getDerivedStateFromError(e: Error) { return { hasError: true, error: e.message }; }
  render() {
    if (this.state.hasError) {
      return (
        <Box sx={{ p: 4, textAlign: 'center' }}>
          <Typography sx={{ color: '#ef4444', mb: 2, fontSize: '1.2rem' }}>页面渲染出错</Typography>
          <Typography sx={{ color: '#94a3b8', mb: 3, fontSize: '0.85rem', fontFamily: 'monospace', whiteSpace: 'pre-wrap' }}>
            {this.state.error}
          </Typography>
          <Button variant="outlined" onClick={() => this.setState({ hasError: false, error: '' })}>
            重试
          </Button>
        </Box>
      );
    }
    return this.props.children;
  }
}

/** Dark theme style constants shared across copy components. */
export const DARK_BG = '#0f0f19';
export const CARD_BG = '#1a1a24';
export const ACCENT = '#3b82f6';
export const TEXT_PRIMARY = '#e2e8f0';
export const TEXT_SECONDARY = '#94a3b8';
export const BORDER = '#334155';

const TAB_LABELS: string[] = ['跟单关系', '品种映射', '执行历史'];

/** Tab label style helper */
const tabSx = {
  color: TEXT_SECONDARY,
  fontWeight: 500,
  textTransform: 'none' as const,
  fontSize: '0.9rem',
  '&.Mui-selected': { color: ACCENT },
};

const CopyLayout: React.FC = () => {
  const [tabIndex, setTabIndex] = useState<number>(0);

  const handleTabChange = (_event: React.SyntheticEvent, newValue: number): void => {
    setTabIndex(newValue);
  };

  const renderTabContent = (): React.ReactNode => {
    return (
      <Suspense fallback={<Box sx={{ p: 4, textAlign: 'center' }}><Typography sx={{ color: '#94a3b8' }}>Loading...</Typography></Box>}>
        {tabIndex === 0 && <CopyRelationships />}
        {tabIndex === 1 && <CopySymbolMappings />}
        {tabIndex === 2 && <CopyHistory />}
      </Suspense>
    );
  };

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', bgcolor: DARK_BG, pb: 4 }}>
      {/* Header */}
      <Box sx={{ px: 3, pt: 2, pb: 0 }}>
        <Typography variant="h5" sx={{ color: TEXT_PRIMARY, fontWeight: 600, mb: 1 }}>
          跟单配置
        </Typography>
      </Box>

      {/* Tabs */}
      <Tabs
        value={tabIndex}
        onChange={handleTabChange}
        sx={{
          px: 3,
          minHeight: 40,
          '& .MuiTabs-indicator': { backgroundColor: ACCENT, height: 2 },
          '& .MuiTabs-flexContainer': { gap: 1 },
        }}
        TabIndicatorProps={{ children: <span /> }}
      >
        {TAB_LABELS.map((label, idx) => (
          <Tab
            key={idx}
            label={label}
            sx={tabSx}
            disableRipple
          />
        ))}
      </Tabs>

      {/* Tab Content */}
      <Box sx={{ flex: 1, overflow: 'auto', px: 3, py: 2 }}>
        <ErrorBoundary>
          {renderTabContent()}
        </ErrorBoundary>
      </Box>
    </Box>
  );
};

export default CopyLayout;
