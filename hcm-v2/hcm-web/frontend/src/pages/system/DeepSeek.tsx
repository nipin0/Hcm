import React, { useEffect, useState } from 'react';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import client from '../../api/client';

const DESCRIPTIONS: Record<string, string> = {
  api_key: 'DeepSeek API Key（留空=保留旧值，不会清空）',
  api_base: 'API 端点 URL，默认官方地址',
  model: '推理模型：deepseek-chat 通用 / deepseek-reasoner 推理强',
  max_tokens: '单次请求最大 Token 数',
  temperature: '温度：0=确定性、1=创意性，金融建议建议 0.3',
};

const DeepSeek: React.FC = () => {
  const [initialValues, setInitialValues] = React.useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);

  const buildFields = (): ConfigField[] => [
    { key: 'api_key', label: 'DeepSeek API Key', type: 'password', defaultValue: '', description: DESCRIPTIONS.api_key },
    { key: 'api_base', label: 'API 端点', type: 'text', defaultValue: 'https://api.deepseek.com', description: DESCRIPTIONS.api_base },
    { key: 'model', label: '模型', type: 'select', defaultValue: 'deepseek-chat', options: [
      { label: 'DeepSeek Chat', value: 'deepseek-chat' },
      { label: 'DeepSeek Reasoner', value: 'deepseek-reasoner' },
    ], description: DESCRIPTIONS.model },
    { key: 'max_tokens', label: '最大 Token 数', type: 'number', defaultValue: 2000, min: 256, max: 32000, description: DESCRIPTIONS.max_tokens },
    { key: 'temperature', label: '温度 (Temperature)', type: 'number', defaultValue: 0.3, min: 0, max: 2, step: 0.1, description: DESCRIPTIONS.temperature },
  ];

  const fetchAndSet = async (): Promise<void> => {
    try {
      const { data: resp } = await client.get('/api/system/deepseek');
      const d = resp.data || resp;
      setInitialValues(d);
    } catch (_e) {
      setInitialValues({});
    }
  };

  useEffect(() => {
    fetchAndSet();
  }, []);

  const handleSubmit = async (values: Record<string, string | number | boolean>): Promise<void> => {
    // Only send fields accepted by Pydantic model
    const allowed = ['api_key', 'api_base', 'model', 'max_tokens', 'temperature'];
    const body: Record<string, unknown> = {};
    for (const k of allowed) {
      if (k in values) body[k] = values[k];
    }
    await client.put('/api/system/deepseek', body);
    await fetchAndSet();
    setFormKey(k => k + 1);
  };

  return (
    <ConfigForm
      formKey={formKey}
      title="DeepSeek AI 配置"
      fields={buildFields()}
      initialValues={initialValues}
      onSubmit={handleSubmit}
      apiEndpoint="PUT /api/system/deepseek"
    />
  );
};

export default DeepSeek;
