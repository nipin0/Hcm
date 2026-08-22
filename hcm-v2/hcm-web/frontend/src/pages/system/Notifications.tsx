import React, { useEffect, useState } from 'react';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import client from '../../api/client';

// 字段与后端 NotificationConfigUpdate(POST /api/system/notifications) 一一对应。
// secret 字段由后端脱敏返回(***开头), 提交时回写会被后端忽略(保留真值)。
const fields: ConfigField[] = [
  // ── 钉钉 ──
  {
    key: 'section_dingtalk',
    label: '钉钉 (DingTalk)',
    type: 'section',
    color: '#3b82f6',
    description: '钉钉群机器人: 群设置 → 智能群助手 → 添加机器人(自定义) → 安全设置选「加签」',
  },
  { key: 'dingtalk_webhook', label: 'Webhook 地址', type: 'text', defaultValue: '', placeholder: 'https://oapi.dingtalk.com/robot/send?access_token=...' },
  { key: 'dingtalk_secret', label: '加签密钥 (Secret)', type: 'password', defaultValue: '', description: '安全设置「加签」给出的密钥。留空或显示 *** 表示不修改' },

  // ── 企业微信 ──
  {
    key: 'section_wecom',
    label: '企业微信 (WeCom)',
    type: 'section',
    color: '#10b981',
    description: '企业微信群机器人 Webhook。开启「微信插件」后消息可转发到个人微信',
  },
  { key: 'wecom_webhook', label: 'Webhook 地址', type: 'text', defaultValue: '', placeholder: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...' },
  { key: 'wecom_secret', label: '密钥 (可选)', type: 'password', defaultValue: '', description: '企业微信机器人一般无需密钥。留空或显示 *** 表示不修改' },

  // ── 通道启用（勾选使用）──
  {
    key: 'section_channels',
    label: '通道启用（勾选使用）',
    type: 'section',
    color: '#f59e0b',
    description: '勾选后才向对应群机器人推送；未勾选或 Webhook 为空则静默跳过，不产生错误',
  },
  { key: 'dingtalk_enabled', label: '启用钉钉推送', type: 'switch', defaultValue: true, description: '取消勾选后即使配置了 Webhook 也不再推钉钉' },
  { key: 'wecom_enabled', label: '启用企业微信推送', type: 'switch', defaultValue: true, description: '取消勾选后即使配置了 Webhook 也不再推企业微信' },

  // ── 通知开关 ──
  {
    key: 'section_alerts',
    label: '通知开关',
    type: 'section',
    color: '#a855f7',
    description: '选择哪些事件推送到上方配置的通道',
  },
  { key: 'enable_trade_alert', label: '交易通知 (新订单)', type: 'switch', defaultValue: true, description: '每笔新订单推送: 有新订单啦 / 进单价格 / 手数' },
  { key: 'enable_signal_alert', label: '信号通知', type: 'switch', defaultValue: true },
  { key: 'enable_risk_alert', label: '风险通知', type: 'switch', defaultValue: true },
  { key: 'enable_system_alert', label: '系统通知', type: 'switch', defaultValue: false },
];

const Notifications: React.FC = () => {
  const [initialValues, setInitialValues] = React.useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);

  useEffect(() => {
    client.get('/api/system/notifications')
      .then(({ data: resp }) => setInitialValues(resp.data || resp))
      .catch(() => {});
  }, []);

  const handleSubmit = async (values: Record<string, string | number | boolean>): Promise<Record<string, string | number | boolean>> => {
    // 防御: 不把脱敏串(***开头)回写, 避免覆盖服务端真实密钥
    const payload: Record<string, string | number | boolean> = { ...values };
    for (const k of ['dingtalk_secret', 'wecom_secret']) {
      if (typeof payload[k] === 'string' && (payload[k] as string).startsWith('***')) {
        delete payload[k];
      }
    }
    await client.put('/api/system/notifications', payload);
    const { data: resp } = await client.get('/api/system/notifications');
    const refreshed = (resp.data || resp) as Record<string, string | number | boolean>;
    setInitialValues(refreshed);
    setFormKey(k => k + 1);
    return refreshed;
  };

  return (
    <ConfigForm
      formKey={formKey}
      title="通知配置"
      fields={fields}
      initialValues={initialValues}
      onSubmit={handleSubmit}
      apiEndpoint="PUT /api/system/notifications"
      grouped
      groupColumns={1}
    />
  );
};

export default Notifications;
