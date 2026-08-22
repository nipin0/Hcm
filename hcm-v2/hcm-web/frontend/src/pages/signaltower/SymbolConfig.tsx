import React, { useEffect, useState } from 'react';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';
import { useSymbol } from '../../contexts/SymbolContext';

const DESCRIPTIONS: Record<string, string> = {
  enable: '开启/关闭该品种的信号产出与交易',
  lot_size: '每笔订单基础手数（与风控 lot_base 二选一）',
  max_positions: '该品种同时持仓数量上限（与风控 max_concurrent_signals 配合）',
  news_filter: '重大新闻前后 5 分钟是否暂停下单',
};

const SymbolConfig: React.FC = () => {
  const { selectedSymbol } = useSymbol();
  const [initialValues, setInitialValues] = React.useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);
  const [loading, setLoading] = useState(false);

  const fields: ConfigField[] = [
    { key: 'enable', label: '启用该品种', type: 'switch', defaultValue: true, description: DESCRIPTIONS.enable },
    { key: 'lot_size', label: '默认手数', type: 'number', defaultValue: 0.01, min: 0.001, max: 100, step: 0.01, description: DESCRIPTIONS.lot_size },
    { key: 'max_positions', label: '最大持仓数', type: 'number', defaultValue: 3, min: 1, max: 20, description: DESCRIPTIONS.max_positions },
    { key: 'news_filter', label: '启用新闻过滤', type: 'switch', defaultValue: true, description: DESCRIPTIONS.news_filter },
  ];

  const loadAndMount = async (): Promise<void> => {
    if (!selectedSymbol) return;
    setLoading(true);
    setInitialValues(undefined); // 先置 undefined 避免旧数据显示
    try {
      const { data: resp } = await client.get(
        `${ENDPOINTS.signalTower.symbolConfig}?symbol=${selectedSymbol.symbol}`,
      );
      const d = resp.data || resp;
      const cfg = d.items?.[0] || d;
      setInitialValues(cfg);     // 数据就绪后才设值
      setFormKey(k => k + 1);    // 然后重挂载 ConfigForm → 它这次拿到的是真数据
    } catch (_e) {
      setInitialValues({});
      setFormKey(k => k + 1);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAndMount();
  }, [selectedSymbol?.symbol]);

  const handleSubmit = async (values: Record<string, string | number | boolean>): Promise<void> => {
    if (!selectedSymbol) return;
    const symbolItem: Record<string, unknown> = { symbol: selectedSymbol.symbol };
    const allowedKeys = ['enable', 'lot_size', 'max_positions', 'news_filter'];
    for (const k of allowedKeys) {
      if (k in values) symbolItem[k] = values[k];
    }
    if ('max_positions' in symbolItem) {
      symbolItem.max_positions = Math.round(Number(symbolItem.max_positions));
    }
    await client.put(ENDPOINTS.signalTower.symbolConfig, { symbols: [symbolItem] });
    await loadAndMount(); // 保存后重新加载并重挂载
  };

  return (
    <ConfigForm
      formKey={formKey}
      title={`品种级配置 — ${selectedSymbol?.symbol || ''}`}
      fields={fields}
      initialValues={initialValues}
      onSubmit={handleSubmit}
      loading={loading}
      apiEndpoint={`PUT ${ENDPOINTS.signalTower.symbolConfig}`}
    />
  );
};

export default SymbolConfig;
