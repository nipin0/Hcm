import React, { useEffect, useState } from 'react';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import client from '../../api/client';
import { Tabs, Tab, Box, Typography } from '@mui/material';

// ── 全局（legacy 回退）字段：与后端 PROMPT_CONFIG_FIELDS 对齐 ──
const globalFields: ConfigField[] = [
  { key: 'user_prompt_template', label: 'AI 用户 Prompt（变量模板）★', type: 'textarea', defaultValue: '', rows: 8,
    description: '实际发送给 DeepSeek 的 prompt 模板。支持 {rsi} {macd} {adx} {atr} 等变量。留空=用硬编码默认。出错自动回退',
    placeholder: 'XAUUSD {timeframe} | {regime}\nRSI={rsi} MACD={macd} ADX={adx}...' },
  { key: 'system_prompt', label: '系统 Prompt', type: 'textarea', defaultValue: '', rows: 2,
    description: 'API 调用 system message。紧凑英文，~20 tokens。模型未单独配置时继承此值',
    placeholder: 'XAUUSD M5 quant analyst. Output compact JSON.' },
  { key: 'signal_prompt_template', label: '信号逻辑（参考）', type: 'textarea', defaultValue: '', rows: 2,
    description: '信号规则文档，非实际注入。AI 调用逻辑见上方"用户 Prompt"',
    placeholder: 'RSI/MACD/ADX/BBW/ATR/bar_momentum → direction+confidence...' },
  { key: 'risk_prompt_template', label: '风控规则（参考）', type: 'textarea', defaultValue: '', rows: 2,
    description: '风控约束文档。实际由 RiskEngine 独立执行',
    placeholder: 'Hard: max_positions exceeded→reject. Soft: drawdown>30%→risk=high...' },
  { key: 'regime_prompt_template', label: '市况定义（参考）', type: 'textarea', defaultValue: '', rows: 2,
    description: 'Regime 分类文档。实际由 RegimeClassifier 硬编码',
    placeholder: 'TREND:ADX>24+MACD. RANGE:BBW<1. PRE_TREND:ADX 18-24...' },
  { key: 'max_context_length', label: '最大上下文 (tokens)', type: 'number', defaultValue: 4096, min: 512, max: 32768,
    description: 'compact 模式实际消耗 ~200 tokens/次' },
  { key: 'include_market_data', label: '包含市场数据', type: 'switch', defaultValue: true,
    description: '始终 true（compact 模式内联指标值）' },
  { key: 'include_news', label: '新闻情绪（预留）', type: 'switch', defaultValue: true,
    description: '当前未接入' },
  { key: 'include_position_info', label: '持仓信息（预留）', type: 'switch', defaultValue: true,
    description: '当前未接入' },
  { key: 'prompt_version', label: '版本', type: 'text', defaultValue: '2.1-template', disabled: true,
    description: '2.1-template = Redis 模板优先 + 硬编码兜底' },
];

// ── 单模型专属字段（system_prompt + user_prompt_template）──
const modelFields: ConfigField[] = [
  { key: 'system_prompt', label: '系统 Prompt (system_message)', type: 'textarea', defaultValue: '', rows: 3,
    description: '该模型专属 system message（~20 tokens）。留空则继承全局 system_prompt',
    placeholder: 'XAUUSD M5 quant analyst. Output compact JSON.' },
  { key: 'user_prompt_template', label: '用户 Prompt 模板（变量）★', type: 'textarea', defaultValue: '', rows: 8,
    description: '该模型专属 user prompt 模板。支持 {rsi}{macd}{adx}{atr} 等变量；JSON 示例字面花括号用 {{ }}。留空则继承全局',
    placeholder: 'XAUUSD {timeframe} | {regime}\nRSI={rsi} MACD={macd} ADX={adx}...' },
];

// 信号模型的 Tab（顺序与 scheduler 机制对齐；五维 ai_dynamic 已弃用 2026-07-24）
// 【2026-08-28 co_source 清除】双源信号模式下线，模型 Tab 改为「和乘幂 / 手动模式」。
// 与后端 scheduler 的 per-model 提示词读取列表 ("hexp", "manual") 保持一致。
const MODEL_TABS: { key: string; label: string }[] = [
  { key: 'hexp', label: '和乘幂' },
  { key: 'manual', label: '手动模式' },
];

// ── 单模型面板：独立加载/保存 signal_tower.prompt.<model>.* ──
const ModelPromptPanel: React.FC<{ model: string; label: string }> = ({ model, label }) => {
  const [initialValues, setInitialValues] = useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);

  const fetchAndSet = async (): Promise<void> => {
    try {
      const { data: resp } = await client.get(`/api/v1/signal-tower/prompt/${model}`);
      setInitialValues(resp.data || {});
    } catch (_e) {
      setInitialValues({});
    }
  };

  useEffect(() => {
    fetchAndSet();
  }, [model]);

  const handleSubmit = async (values: Record<string, string | number | boolean>): Promise<void> => {
    await client.put(`/api/v1/signal-tower/prompt/${model}`, values);
    await fetchAndSet();
    setFormKey((k) => k + 1);
  };

  return (
    <ConfigForm
      formKey={formKey}
      title={`提示词 — ${label}`}
      fields={modelFields}
      initialValues={initialValues}
      onSubmit={handleSubmit}
      apiEndpoint={`PUT /api/v1/signal-tower/prompt/${model}`}
    />
  );
};

const Prompt: React.FC = () => {
  // tab: 0 = 全局默认(回退)；1..3 = 三个模型
  const [tab, setTab] = useState(0);

  const [globalValues, setGlobalValues] = useState<Record<string, string | number | boolean> | undefined>();
  const [globalFormKey, setGlobalFormKey] = useState(0);

  const fetchGlobal = async (): Promise<void> => {
    try {
      const { data: resp } = await client.get('/api/signal-tower/prompt');
      setGlobalValues(resp.data || resp);
    } catch (_e) {
      setGlobalValues({});
    }
  };

  useEffect(() => {
    fetchGlobal();
  }, []);

  const handleGlobalSubmit = async (values: Record<string, string | number | boolean>): Promise<void> => {
    await client.put('/api/signal-tower/prompt', values);
    await fetchGlobal();
    setGlobalFormKey((k) => k + 1);
  };

  const tabSx = {
    color: '#cbd5e1',
    '&.Mui-selected': { color: '#60a5fa' },
    textTransform: 'none' as const,
    fontWeight: 600,
  };

  return (
    <Box>
      <Typography variant="h6" className="text-gray-100 mb-4 font-semibold">
        Prompt 模板管理
      </Typography>

      <Box sx={{ borderBottom: '1px solid #334155', mb: 3 }}>
        <Tabs
          value={tab}
          onChange={(_e, v) => setTab(v)}
          textColor="primary"
          indicatorColor="primary"
          variant="scrollable"
          scrollButtons="auto"
        >
          <Tab label="全局默认(回退)" sx={tabSx} />
          {MODEL_TABS.map((m) => (
            <Tab key={m.key} label={m.label} sx={tabSx} />
          ))}
        </Tabs>
      </Box>

      {tab === 0 && (
        <ConfigForm
          key={`global-${globalFormKey}`}
          title="全局 Prompt（回退默认值）"
          fields={globalFields}
          initialValues={globalValues}
          onSubmit={handleGlobalSubmit}
          apiEndpoint="PUT /api/signal-tower/prompt"
        />
      )}

      {MODEL_TABS.map((m, i) => (
        <Box key={m.key} sx={{ display: tab === i + 1 ? 'block' : 'none' }}>
          <ModelPromptPanel model={m.key} label={m.label} />
        </Box>
      ))}
    </Box>
  );
};

export default Prompt;
