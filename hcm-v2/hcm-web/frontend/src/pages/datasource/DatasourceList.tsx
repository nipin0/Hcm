import React, { useEffect, useState } from 'react';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import client from '../../api/client';

const fields: ConfigField[] = [
  { key: 'primary_source', label: '主数据源', type: 'select', defaultValue: 'mt5', options: [
    { label: 'MT5', value: 'mt5' },
    { label: 'Dukascopy', value: 'dukascopy' },
    { label: 'OANDA', value: 'oanda' },
    { label: 'Interactive Brokers', value: 'ib' },
  ]},
  { key: 'backup_source', label: '备用数据源', type: 'select', defaultValue: 'dukascopy', options: [
    { label: 'MT5', value: 'mt5' },
    { label: 'Dukascopy', value: 'dukascopy' },
    { label: 'OANDA', value: 'oanda' },
    { label: '无', value: 'none' },
  ]},
  { key: 'timeframe', label: '默认时间周期', type: 'select', defaultValue: 'M5', options: [
    { label: 'M1', value: 'M1' },
    { label: 'M5', value: 'M5' },
    { label: 'M15', value: 'M15' },
    { label: 'H1', value: 'H1' },
    { label: 'H4', value: 'H4' },
    { label: 'D1', value: 'D1' },
  ], description: '影响信号塔的数据采集推理周期和 mt5_bridge 的 K 线拉取周期。修改后需重启信号塔和 bridge 生效。' },
  { key: 'max_bars', label: '最大K线数', type: 'number', defaultValue: 5000, min: 100, max: 50000 },
  { key: 'enable_tick_data', label: '启用Tick数据', type: 'switch', defaultValue: false },
  { key: 'enable_news_feed', label: '启用新闻推送', type: 'switch', defaultValue: true },
  { key: 'news_sources', label: '新闻来源', type: 'text', defaultValue: 'forexfactory,investing', placeholder: '逗号分隔' },
  { key: 'auto_failover', label: '自动故障切换', type: 'switch', defaultValue: true },
  { key: 'failover_timeout', label: '故障切换超时 (秒)', type: 'number', defaultValue: 30, min: 5, max: 300 },
  { key: 'cache_duration', label: '数据缓存时长 (秒)', type: 'number', defaultValue: 300, min: 30, max: 7200 },
];

const DatasourceList: React.FC = () => {
  const [initialValues, setInitialValues] = React.useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);

  useEffect(() => {
    client.get('/api/datasource/config')
      .then(({ data: resp }) => setInitialValues(resp.data || resp))
      .catch(() => {});
  }, []);

  const handleSubmit = async (values: Record<string, string | number | boolean>): Promise<void> => {
    await client.put('/api/datasource/config', values);
    const { data: resp } = await client.get('/api/datasource/config');
    setInitialValues(resp.data || resp);
    setFormKey(k => k + 1);
  };

  return (
    <ConfigForm
      formKey={formKey}
      title="数据源管理"
      fields={fields}
      initialValues={initialValues}
      onSubmit={handleSubmit}
      apiEndpoint="PUT /api/datasource/config"
    />
  );
};

export default DatasourceList;
