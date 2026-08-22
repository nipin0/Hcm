import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { Box } from '@mui/material';
import Layout from './components/Layout';
import StatusBar from './components/StatusBar';
import { useAuth } from './contexts/AuthContext';

// Lazy-loaded pages
import Login from './pages/Login';
import Users from './pages/system/Users';
import Network from './pages/system/Network';
import MT5 from './pages/system/MT5';
import DeepSeek from './pages/system/DeepSeek';
import Notifications from './pages/system/Notifications';
import Cache from './pages/system/Cache';
import Mode from './pages/signaltower/Mode';
import Prompt from './pages/signaltower/Prompt';
import Threshold from './pages/signaltower/Threshold';
import Watchdog from './pages/signaltower/Watchdog';
import SymbolConfig from './pages/signaltower/SymbolConfig';
import SignalFunnel from './pages/signaltower/SignalFunnel';
import AiQualityConfig from './pages/signaltower/AiQualityConfig';
import AiReport from './pages/signaltower/AiReport';
import ModelReport from './pages/signaltower/ModelReport';
import ModelMonitor from './pages/signaltower/ModelMonitor';
import DatasourceList from './pages/datasource/DatasourceList';
import EngineRules from './pages/engine/EngineRules';
import DispatchConfig from './pages/dispatch/DispatchConfig';
import RiskConfig from './pages/risk/RiskConfig';
import CloseConfig from './pages/close/CloseConfig';
import CopyLayout from './pages/copy/CopyLayout';
import CoSourceConfig from './pages/cosource/CoSourceConfig';
import HexpConfig from './pages/hexp/HexpConfig';
import HexpDashboard from './pages/hexp/HexpDashboard';
import Realtime from './pages/dashboard/Realtime';
import CalibrationTimeline from './pages/dashboard/CalibrationTimeline';
import Positions from './pages/dashboard/Positions';
import Statistics from './pages/dashboard/Statistics';
import Health from './pages/dashboard/Health';
import Factors from './pages/dashboard/Factors';
import Compare from './pages/dashboard/Compare';

const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return (
      <Box className="flex items-center justify-center h-screen bg-gray-950">
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-blue-500" />
      </Box>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return (
    <Box className="flex flex-col h-screen overflow-hidden">
      <Layout>{children}</Layout>
      <StatusBar />
    </Box>
  );
};

const App: React.FC = () => {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      {/* System Settings */}
      <Route path="/system/users" element={<ProtectedRoute><Users /></ProtectedRoute>} />
      <Route path="/system/network" element={<ProtectedRoute><Network /></ProtectedRoute>} />
      <Route path="/system/mt5" element={<ProtectedRoute><MT5 /></ProtectedRoute>} />
      <Route path="/system/deepseek" element={<ProtectedRoute><DeepSeek /></ProtectedRoute>} />
      <Route path="/system/notifications" element={<ProtectedRoute><Notifications /></ProtectedRoute>} />
      <Route path="/system/cache" element={<ProtectedRoute><Cache /></ProtectedRoute>} />

      {/* Signal Tower */}
                <Route path="/signal-tower/mode" element={<ProtectedRoute><Mode /></ProtectedRoute>} />
                <Route path="/signal-tower/threshold" element={<ProtectedRoute><Threshold /></ProtectedRoute>} />
                <Route path="/signal-tower/prompt" element={<ProtectedRoute><Prompt /></ProtectedRoute>} />
      <Route path="/signal-tower/watchdog" element={<ProtectedRoute><Watchdog /></ProtectedRoute>} />
      <Route path="/signal-tower/symbol-config" element={<ProtectedRoute><SymbolConfig /></ProtectedRoute>} />
      <Route path="/engine/ai-quality" element={<ProtectedRoute><AiQualityConfig /></ProtectedRoute>} />
      <Route path="/engine/ai-report" element={<ProtectedRoute><AiReport /></ProtectedRoute>} />
      <Route path="/engine/model-report" element={<ProtectedRoute><ModelReport /></ProtectedRoute>} />
      <Route path="/engine/model-monitor" element={<ProtectedRoute><ModelMonitor /></ProtectedRoute>} />

      {/* Datasource */}
      <Route path="/datasource" element={<ProtectedRoute><DatasourceList /></ProtectedRoute>} />

      {/* Engine */}
      <Route path="/engine/rules" element={<ProtectedRoute><EngineRules /></ProtectedRoute>} />

      {/* Other config pages */}
      <Route path="/dispatch" element={<ProtectedRoute><DispatchConfig /></ProtectedRoute>} />
      <Route path="/risk" element={<ProtectedRoute><RiskConfig /></ProtectedRoute>} />
      <Route path="/close" element={<ProtectedRoute><CloseConfig /></ProtectedRoute>} />
      <Route path="/copy" element={<ProtectedRoute><CopyLayout /></ProtectedRoute>} />
      <Route path="/cosource" element={<ProtectedRoute><CoSourceConfig /></ProtectedRoute>} />
      <Route path="/hexp" element={<ProtectedRoute><HexpConfig /></ProtectedRoute>} />
      <Route path="/hexp/dashboard" element={<ProtectedRoute><HexpDashboard /></ProtectedRoute>} />

      {/* Dashboard */}
      <Route path="/dashboard/realtime" element={<ProtectedRoute><Realtime /></ProtectedRoute>} />
      <Route path="/dashboard/calibration-timeline" element={<ProtectedRoute><CalibrationTimeline /></ProtectedRoute>} />
      <Route path="/dashboard/positions" element={<ProtectedRoute><Positions /></ProtectedRoute>} />
      <Route path="/dashboard/statistics" element={<ProtectedRoute><Statistics /></ProtectedRoute>} />
      <Route path="/dashboard/health" element={<ProtectedRoute><Health /></ProtectedRoute>} />
      <Route path="/dashboard/factors" element={<ProtectedRoute><Factors /></ProtectedRoute>} />
      <Route path="/dashboard/compare" element={<ProtectedRoute><Compare /></ProtectedRoute>} />
      <Route path="/dashboard/funnel" element={<ProtectedRoute><SignalFunnel /></ProtectedRoute>} />

      {/* Default redirect */}
      <Route path="/" element={<Navigate to="/dashboard/realtime" replace />} />
      <Route path="*" element={<Navigate to="/dashboard/realtime" replace />} />
    </Routes>
  );
};

export default App;
