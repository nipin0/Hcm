import React, { useState, useEffect, useCallback } from 'react';
import { Box, Typography, Tooltip, Button } from '@mui/material';
import client from '../api/client';

/** A single node on the pipeline axis bar. */
interface PipelineNode {
  name: string;
  status: 'healthy' | 'warning' | 'down' | 'inactive';
  group: 'core' | 'aux';
  metric: string;
  last_activity: string | null;
  troubleshooting: string[];
  // 信号生成节点扩展字段（引擎状态 / 激活模型 / 切换时间）
  activeModel?: string | null;
  modelLabel?: string | null;
  modeSwitchedAt?: string | null;
  engineRunning?: boolean;
  lastLoopAt?: string | null;
  signalsProduced?: number;
  canActivate?: boolean;
}

/** 相对时间格式化（Xs前 / Xm前 / Xh前）。 */
function formatAgo(iso?: string): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '';
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return `${s}s前`;
  if (s < 3600) return `${Math.floor(s / 60)}m前`;
  return `${Math.floor(s / 3600)}h前`;
}

/** Full response from /api/system/pipeline. */
interface PipelineData {
  nodes: PipelineNode[];
  timestamp: string;
}

/** Status → color mapping for borders, backgrounds, and icons. */
const STATUS_COLORS: Record<string, string> = {
  healthy: '#22c55e',
  warning: '#eab308',
  down: '#ef4444',
  inactive: '#64748b',
};

/** Status → dot icon. */
const STATUS_DOTS: Record<string, string> = {
  healthy: '●',
  warning: '●',
  down: '●',
  inactive: '○',
};

/** Human-readable status labels. */
const STATUS_LABELS: Record<string, string> = {
  healthy: '正常',
  warning: '预警',
  down: '宕机',
  inactive: '闲置',
};

/** Props for a single pipeline node chip. */
interface NodeChipProps {
  node: PipelineNode;
  onActivate?: () => void;
}

const NodeChip: React.FC<NodeChipProps> = ({ node, onActivate }) => {
  const color = STATUS_COLORS[node.status] ?? STATUS_COLORS.inactive;
  const dot = STATUS_DOTS[node.status] ?? STATUS_DOTS.inactive;
  const [activating, setActivating] = useState(false);

  const modelLabel = (node as any).modelLabel as string | undefined;
  const modeSwitchedAt = (node as any).modeSwitchedAt as string | undefined;
  const canActivate = !!((node as any).canActivate) && node.name === '信号生成';

  const handleActivate = async () => {
    if (!onActivate) return;
    setActivating(true);
    try {
      await onActivate();
    } finally {
      setActivating(false);
    }
  };

  // Use short_metric for chip display, full metric for tooltip
  const chipMetric = (node as any).short_metric || node.metric;
  const tooltipMetric = node.metric;

  const tooltipContent: React.ReactNode = (
    <Box sx={{ minWidth: 220 }}>
      <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 0.5 }}>
        {dot} {node.name}
      </Typography>
      <Typography variant="caption" sx={{ color: '#94a3b8', display: 'block', mb: 0.5 }}>
        状态: {STATUS_LABELS[node.status] ?? node.status} &nbsp;|&nbsp; {tooltipMetric}
      </Typography>
      {modelLabel && (
        <Typography variant="caption" sx={{ color: '#38bdf8', display: 'block', mb: 0.5 }}>
          激活模型: {modelLabel}
          {modeSwitchedAt ? ` · 切换于 ${formatAgo(modeSwitchedAt)}` : ''}
        </Typography>
      )}
      {node.engineRunning !== undefined && (
        <Typography variant="caption" sx={{ color: '#64748b', display: 'block', mb: 0.5 }}>
          引擎运行: {node.engineRunning ? '是' : '否'}
          {node.lastLoopAt ? ` · 循环: ${new Date(node.lastLoopAt).toLocaleTimeString('zh-CN')}` : ''}
        </Typography>
      )}
      {node.last_activity && (
        <Typography variant="caption" sx={{ color: '#64748b', display: 'block', mb: 0.5 }}>
          最近活动: {new Date(node.last_activity).toLocaleString('zh-CN')}
        </Typography>
      )}
      {node.troubleshooting.length > 0 && (
        <Box component="ol" sx={{ m: 0, pl: 2, mt: 0.5, fontSize: '0.75rem', color: '#94a3b8', lineHeight: 1.7 }}>
          {node.troubleshooting.map((tip, idx) => (
            <li key={idx}>{tip}</li>
          ))}
        </Box>
      )}
    </Box>
  );

  return (
    <Tooltip
      title={tooltipContent}
      arrow
      placement="top"
      componentsProps={{
        tooltip: {
          sx: {
            backgroundColor: '#1e293b',
            border: '1px solid #334155',
            borderRadius: 2,
            p: 1.5,
            maxWidth: 380,
            color: '#e2e8f0',
            fontSize: '0.8rem',
            boxShadow: '0 4px 24px rgba(0,0,0,0.5)',
          },
        },
      }}
    >
      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          px: 2,
          py: 1,
          minWidth: 90,
          border: `2px solid ${color}`,
          borderRadius: 2,
          backgroundColor: `${color}14`,
          cursor: 'pointer',
          transition: 'all 0.2s ease',
          '&:hover': {
            backgroundColor: `${color}28`,
            borderColor: color,
            transform: 'translateY(-1px)',
          },
        }}
      >
        <Typography variant="caption" sx={{ color, fontWeight: 600, fontSize: '0.65rem', lineHeight: 1.2 }}>
          {node.name}
        </Typography>
        <Typography variant="caption" sx={{ color: '#94a3b8', fontSize: '0.65rem', mt: 0.15, maxWidth: 100, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {chipMetric}
        </Typography>
        {modelLabel && (
          <Typography variant="caption" sx={{ color: '#38bdf8', fontSize: '0.6rem', mt: 0.15 }}>
            {modelLabel}
          </Typography>
        )}
        {canActivate && (
          <Button
            size="small"
            variant="contained"
            disabled={activating}
            onClick={(e) => { e.stopPropagation(); handleActivate(); }}
            sx={{ mt: 0.5, fontSize: '0.55rem', py: 0.2, px: 1, lineHeight: 1, backgroundColor: '#ef4444', '&:hover': { backgroundColor: '#dc2626' } }}
          >
            {activating ? '激活中…' : '激活引擎'}
          </Button>
        )}
      </Box>
    </Tooltip>
  );
};

/** Arrow connector between core pipeline nodes. */
const Arrow: React.FC = () => (
  <Typography sx={{ color: '#475569', mx: 0.5, fontSize: '0.9rem', userSelect: 'none', lineHeight: 1 }}>
    →
  </Typography>
);

const PipelineBar: React.FC = () => {
  const [data, setData] = useState<PipelineData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchPipeline = useCallback(async (): Promise<void> => {
    try {
      const res = await client.get('/api/system/pipeline');
      const payload: PipelineData = res.data?.data ?? res.data;
      if (payload && Array.isArray(payload.nodes)) {
        setData(payload);
        setError(null);
      } else {
        setError('Invalid pipeline response');
      }
    } catch {
      setError('无法获取管线状态');
    }
  }, []);

  const activateEngine = useCallback(async (): Promise<void> => {
    try {
      await client.post('/api/v1/system/engine/activate');
    } catch {
      /* 忽略，下面仍刷新状态 */
    } finally {
      fetchPipeline();
    }
  }, [fetchPipeline]);

  useEffect(() => {
    fetchPipeline();
    const interval = setInterval(fetchPipeline, 15000);
    return () => clearInterval(interval);
  }, [fetchPipeline]);

  if (error && !data) {
    return (
      <Box sx={{ mb: 3, p: 2, border: '1px solid #334155', borderRadius: 2, backgroundColor: '#0f1117' }}>
        <Typography variant="caption" sx={{ color: '#ef4444' }}>{error}</Typography>
      </Box>
    );
  }

  const nodes = data?.nodes ?? [];
  const coreNodes = nodes.filter((n) => n.group === 'core');
  const auxNodes = nodes.filter((n) => n.group === 'aux');

  return (
    <Box
      sx={{
        mb: 3,
        p: 2,
        border: '1px solid #1e293b',
        borderRadius: 2,
        backgroundColor: '#0f1117',
      }}
    >
      {/* Header row */}
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1.5 }}>
        <Typography variant="subtitle2" sx={{ color: '#94a3b8', fontWeight: 600, fontSize: '0.75rem', letterSpacing: '0.05em' }}>
          数据流管线状态
        </Typography>
        {data?.timestamp && (
          <Typography variant="caption" sx={{ color: '#475569', fontSize: '0.65rem' }}>
            {new Date(data.timestamp).toLocaleTimeString('zh-CN')}
          </Typography>
        )}
      </Box>

      {/* Merged single-row pipeline: core → … → core │ 辅助 · aux · … */}
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 0,
        }}
      >
        {/* Core section */}
        <Typography variant="caption" sx={{ color: '#3b82f6', fontWeight: 600, mr: 1, fontSize: '0.65rem', minWidth: 28 }}>
          核心
        </Typography>
        {coreNodes.map((node, idx) => (
          <React.Fragment key={node.name}>
            {idx > 0 && <Arrow />}
            <NodeChip node={node} onActivate={activateEngine} />
          </React.Fragment>
        ))}
        {coreNodes.length === 0 && (
          <Typography variant="caption" sx={{ color: '#475569' }}>—</Typography>
        )}

        {/* Separator between core and aux */}
        {auxNodes.length > 0 && (
          <Typography sx={{ color: '#475569', mx: 2, fontSize: '0.9rem', userSelect: 'none', lineHeight: 1 }}>
            │
          </Typography>
        )}

        {/* Auxiliary section (dot-separated) */}
        {auxNodes.length > 0 && (
          <>
            <Typography variant="caption" sx={{ color: '#8b5cf6', fontWeight: 600, mr: 1, fontSize: '0.65rem', minWidth: 28 }}>
              辅助
            </Typography>
            {auxNodes.map((node, idx) => (
              <React.Fragment key={node.name}>
                {idx > 0 && (
                  <Typography sx={{ color: '#334155', mx: 0.75, fontSize: '0.7rem', userSelect: 'none' }}>·</Typography>
                )}
                <NodeChip node={node} onActivate={activateEngine} />
              </React.Fragment>
            ))}
          </>
        )}
      </Box>
    </Box>
  );
};

export default PipelineBar;
