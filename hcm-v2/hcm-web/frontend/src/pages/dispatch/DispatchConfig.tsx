import React, { useEffect, useState } from 'react';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import client from '../../api/client';

const fields: ConfigField[] = [
  { key: 'dispatch_method', label: '分发方式', type: 'select', defaultValue: 'auto', options: [
    { label: '自动分发', value: 'auto' },
    { label: '手动确认', value: 'manual' },
    { label: '半自动', value: 'semi_auto' },
  ]},
  { key: 'target_platform', label: '目标平台', type: 'select', defaultValue: 'mt5', options: [
    { label: 'MT5', value: 'mt5' },
    { label: 'MT4', value: 'mt4' },
    { label: 'cTrader', value: 'ctrader' },
  ]},
  { key: 'max_retry', label: '最大重试次数', type: 'number', defaultValue: 3, min: 0, max: 10 },
  { key: 'retry_interval', label: '重试间隔 (秒)', type: 'number', defaultValue: 5, min: 1, max: 60 },
  { key: 'order_timeout', label: '订单超时 (秒)', type: 'number', defaultValue: 30, min: 5, max: 120 },
  { key: 'enable_partial_fill', label: '允许部分成交', type: 'switch', defaultValue: false },
  { key: 'slippage_tolerance', label: '滑点容忍度 (点数)', type: 'number', defaultValue: 5, min: 0, max: 50 },
  { key: 'dispatch_log_level', label: '日志级别', type: 'select', defaultValue: 'info', options: [
    { label: 'DEBUG', value: 'debug' },
    { label: 'INFO', value: 'info' },
    { label: 'WARNING', value: 'warning' },
    { label: 'ERROR', value: 'error' },
  ]},
];

const DispatchConfig: React.FC = () => {
  const [initialValues, setInitialValues] = React.useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);

  useEffect(() => {
    client.get('/api/dispatch/config')
      .then(({ data: resp }) => setInitialValues(resp.data || resp))
      .catch(() => {});
  }, []);

  const handleSubmit = async (values: Record<string, string | number | boolean>): Promise<void> => {
    await client.put('/api/dispatch/config', values);
    const { data: resp } = await client.get('/api/dispatch/config');
    setInitialValues(resp.data || resp);
    setFormKey(k => k + 1);
  };

  return (
    <ConfigForm
      formKey={formKey}
      title="分发配置"
      fields={fields}
      initialValues={initialValues}
      onSubmit={handleSubmit}
      apiEndpoint="PUT /api/dispatch/config"
    />
  );
};

export default DispatchConfig;
