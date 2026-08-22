import React, { useEffect, useState } from 'react';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import client from '../../api/client';

const DESCRIPTIONS: Record<string, string> = {
  service_host: '服务绑定地址。0.0.0.0=所有网卡。127.0.0.1=仅本机',
  service_port: 'HTTP 端口。改后需重启服务',
  ws_port: 'WebSocket 端口',
  cors_origins: '允许跨域来源。开发填*，生产用逗号分隔具体域名',
};

const Network: React.FC = () => {
  const [initialValues, setInitialValues] = React.useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);

  const fields: ConfigField[] = [
    { key: 'service_host', label: '监听地址', type: 'text', defaultValue: '0.0.0.0', description: DESCRIPTIONS.service_host },
    { key: 'service_port', label: 'HTTP 端口', type: 'number', defaultValue: 8000, min: 1, max: 65535, description: DESCRIPTIONS.service_port },
    { key: 'ws_port', label: 'WebSocket 端口', type: 'number', defaultValue: 8001, min: 1, max: 65535, description: DESCRIPTIONS.ws_port },
    { key: 'cors_origins', label: 'CORS 来源', type: 'text', defaultValue: '*', placeholder: '用逗号分隔', description: DESCRIPTIONS.cors_origins },
  ];

  async function fetchAndSet(): Promise<void> {
    try {
      const { data: resp } = await client.get('/api/system/network');
      const d = resp.data || resp;
      setInitialValues(d);
    } catch { setInitialValues({}); }
  }

  useEffect(() => { fetchAndSet(); }, []);

  const handleSubmit = async (values: Record<string, string | number | boolean>) => {
    const allowed = ['service_host', 'service_port', 'cors_origins', 'ws_port'];
    const body: Record<string, unknown> = {};
    for (const k of allowed) { if (k in values) body[k] = values[k]; }
    await client.put('/api/system/network', body);
    // Re-fetch and update initialValues BEFORE triggering remount
    const { data: resp } = await client.get('/api/system/network');
    const newValues = (resp.data || resp) as Record<string, string | number | boolean>;
    setInitialValues(newValues);
    setFormKey(k => k + 1);
    return newValues;
  };

  return (
    <ConfigForm
      formKey={formKey}
      title="网络与端口配置"
      fields={fields}
      initialValues={initialValues}
      onSubmit={handleSubmit}
      apiEndpoint="PUT /api/system/network"
    />
  );
};

export default Network;
