import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Box, Button, Card, CardContent, CardHeader, CircularProgress, Divider,
  Alert, Snackbar,
} from '@mui/material';
import SaveIcon from '@mui/icons-material/Save';
import ConfigForm, { ConfigField, ConfigFormHandle } from '../../components/ConfigForm';
import client from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import SaveGuardDialog from '../../components/SaveGuardDialog';

/** AI 信号质量模块（LightGBM + DeepSeek + 耦合闸门 + 持仓调仓）配置页。
 *  独立命名空间 ai.*，零硬编码；默认全关（纯 HEXP 运行）。 */

interface AiConfig { [key: string]: string | number | boolean; }

const FIELDS: Record<string, ConfigField[]> = {
  top: [
    { key: 'ai.enabled', label: 'AI 质量模块总开关', type: 'switch', defaultValue: false,
      description: 'false=纯 HEXP 运行，AI 零介入；true=启用 AI 质量过滤/耦合。', suggested: 'false（默认关）' },
    { key: 'ai.mode', label: 'AI 耦合模式', type: 'select',
      options: [{ label: '解耦（纯 HEXP）', value: 'decoupled' }, { label: '耦合（综合评分）', value: 'coupled' }],
      defaultValue: 'decoupled',
      description: 'decoupled=纯 HEXP 分数触发；coupled=AI 综合评分触发。', suggested: 'decoupled' },
  ],

  lm: [
    { key: 'sec_lm_note', label: '激活说明', type: 'section', color: '#22c55e',
      description: 'LightGBM 真实激活条件 = 总开关 ai.enabled=true 且 ai.mode=coupled（见"总开关"组）。'
        + '本组仅维护模型/校准路径与阈值，无独立开关（ai.lm.enabled 为已移除的误导幽灵键）。' },
    { key: 'ai.lm.model_path', label: '模型文件路径', type: 'text', defaultValue: '',
      description: '空或加载失败→自动降级纯 HEXP（冷启动安全）。', suggested: '' },
    { key: 'ai.lm.calib_path', label: '概率校准器路径', type: 'text', defaultValue: '',
      description: 'Platt/isotonic 校准器（pkl），空=不校准。', suggested: '' },
    { key: 'ai.lm.model_version', label: '模型版本', type: 'text', defaultValue: 'v0',
      description: '回滚=指向旧版本文件。', suggested: 'v0' },
    { key: 'ai.lm.pass_threshold', label: '否决阈值', type: 'number', defaultValue: 0.50, min: 0, max: 1, step: 0.01,
      description: 'p < 此值 → 过滤信号（不下单）。', suggested: '0.50' },
    { key: 'ai.lm.down_threshold', label: '降级阈值', type: 'number', defaultValue: 0.60, min: 0, max: 1, step: 0.01,
      description: 'pass ≤ p < 此值 → 降一级（C→红灯, B→C）。', suggested: '0.60' },
    { key: 'ai.lm.up_threshold', label: '升级阈值', type: 'number', defaultValue: 0.70, min: 0, max: 1, step: 0.01,
      description: 'p ≥ 此值 → 升一级（B→A, A→S）。', suggested: '0.70' },
    { key: 'ai.lm.veto_quantile', label: '否决分位（重锚）', type: 'number', defaultValue: 0.50, min: 0, max: 1, step: 0.05,
      description: 'p 低于此分位→过滤（相对基率，替代绝对阈值，推荐）。', suggested: '0.50' },
    { key: 'ai.lm.down_quantile', label: '降级分位（重锚）', type: 'number', defaultValue: 0.70, min: 0, max: 1, step: 0.05,
      description: 'p 低于此分位→降一级。', suggested: '0.70' },
    { key: 'ai.lm.up_quantile', label: '升级分位（重锚）', type: 'number', defaultValue: 0.85, min: 0, max: 1, step: 0.05,
      description: 'p 高于此分位→升一级。', suggested: '0.85' },
    { key: 'ai.lm.min_samples_train', label: '最少训练样本', type: 'number', defaultValue: 500, min: 50, max: 5000, step: 10,
      description: '标注样本 < 此值不出模型，保持纯 HEXP。', suggested: '500' },
    { key: 'ai.lm.retrain_cron', label: '重训 cron', type: 'text', defaultValue: '0 2 * * 1',
      description: '离线重训调度（默认每周一 02:00）。', suggested: '0 2 * * 1' },
    { key: 'ai.lm.label_r_win', label: '标签止盈 R 倍数', type: 'number', defaultValue: 1.0, min: 0.5, max: 3, step: 0.1,
      description: '先触 +此×R 记 win=1（已重锚对称 1R）。', suggested: '1.0' },
    { key: 'ai.lm.label_r_loss', label: '标签止损 R 倍数', type: 'number', defaultValue: 1.0, min: 0.5, max: 3, step: 0.1,
      description: '先触 -此×R 记 loss=0。', suggested: '1.0' },
    { key: 'ai.lm.label_horizon_bars', label: '标签回看 M5 根数', type: 'number', defaultValue: 12, min: 3, max: 60, step: 1,
      description: '入场后此根数内判定先触。', suggested: '12' },
    { key: 'ai.lm.label_sl_atr_fallback', label: 'SL 推导回退 ATR 倍数', type: 'number', defaultValue: 2.0, min: 1, max: 4, step: 0.1,
      description: 'sl_price 未落库且无 ai_sl_mult 时，R = 此值 × ATR。', suggested: '2.0' },
    { key: 'ai.lm.sl_scale_enabled', label: 'AI 分缩放 SL 开关', type: 'switch', defaultValue: true,
      description: 'true=用 LightGBM ai_score 缩放 SL 宽度(0.8~1.5×ATR，设计文档 ai_sl_coeff 区间)；'
        + '分高→宽(1.5)、分低→窄(0.8)。AI 断联/失效自动回退会话 SL。', suggested: 'true' },
    { key: 'ai.lm.sl_scale_min', label: 'SL 缩放系数下限', type: 'number', defaultValue: 0.8, min: 0.5, max: 1.5, step: 0.05,
      description: 'ai_score 最低(0)时的 SL 距离(×ATR)。', suggested: '0.8' },
    { key: 'ai.lm.sl_scale_max', label: 'SL 缩放系数上限', type: 'number', defaultValue: 1.5, min: 0.8, max: 3, step: 0.05,
      description: 'ai_score 最高(100)时的 SL 距离(×ATR)。', suggested: '1.5' },
  ],

  ds: [
    { key: 'ai.ds.enabled', label: 'DeepSeek 异步数据源开关', type: 'switch', defaultValue: false,
      description: 'true=后台异步刷新 3 输出（真假概率/sl_coeff/continuity）。', suggested: 'false' },
    { key: 'ai.ds.timeout_sec', label: '调用超时（秒）', type: 'number', defaultValue: 15, min: 5, max: 120, step: 1,
      description: '超时即降级（不阻塞实时决策）。', suggested: '15' },
    { key: 'ai.ds.cache_ttl_min', label: '输出缓存 TTL（分）', type: 'number', defaultValue: 30, min: 5, max: 240, step: 5,
      description: '3 输出在 Redis 缓存的有效期。', suggested: '30' },
    { key: 'ai.ds.sl_coeff_min', label: 'ai_sl_coeff 下限', type: 'number', defaultValue: 0.8, min: 0.5, max: 2, step: 0.1,
      description: '自适应止损系数硬下界（ATR 倍数）。', suggested: '0.8' },
    { key: 'ai.ds.sl_coeff_max', label: 'ai_sl_coeff 上限', type: 'number', defaultValue: 1.5, min: 1, max: 3, step: 0.1,
      description: '自适应止损系数硬上界（ATR 倍数）。', suggested: '1.5' },
    { key: 'ai.ds.fallback_sl_coeff', label: '止损回退系数', type: 'number', defaultValue: 0, min: 0, max: 3, step: 0.1,
      description: '0=回退 hexp.exec.sl_atr_mult；>0=固定回退系数。', suggested: '0' },
    { key: 'ai.ds.fallback_continuity', label: '延续分回退值', type: 'number', defaultValue: 50, min: 0, max: 100, step: 1,
      description: 'DeepSeek 不可用时 continuity_score=此值（中性）。', suggested: '50' },
  ],

  fuse: [
    { key: 'ai.fuse.w_lm', label: 'LightGBM 融合权重', type: 'number', defaultValue: 0.6, min: 0, max: 1, step: 0.05,
      description: '本地快速票权重（0-1）；与 w_ds 归一化。权重越高越信任 sidecar 实时模型。', suggested: '0.6' },
    { key: 'ai.fuse.w_ds', label: 'DeepSeek 融合权重', type: 'number', defaultValue: 0.4, min: 0, max: 1, step: 0.05,
      description: '异步语义票权重（0-1）；与 w_lm 归一化。权重越高越信任 DeepSeek 校准。', suggested: '0.4' },
    { key: 'ai.fuse.ds_max_age_sec', label: 'DeepSeek 票最大龄（秒）', type: 'number', defaultValue: 900, min: 60, max: 3600, step: 30,
      description: '超过此龄的旧判断丢弃（行情已走远，不参与实时裁决）。', suggested: '900' },
  ],

  cpl: [
    { key: 'ai.cpl.enabled', label: '耦合闸门开关', type: 'switch', defaultValue: false,
      description: 'true=总分=w(k)·S_hp+(1-w(k))·C_ai 触发下单。', suggested: 'false' },
    { key: 'ai.cpl.w_trend', label: '趋势档 S_hp 权重', type: 'number', defaultValue: 0.7, min: 0.5, max: 0.9, step: 0.05,
      description: 'k > k_trend_min 时 S_hp 权重（C_ai=1-w）。', suggested: '0.7' },
    { key: 'ai.cpl.w_neutral', label: '中性档 S_hp 权重', type: 'number', defaultValue: 0.6, min: 0.5, max: 0.9, step: 0.05,
      description: 'k_range_max < k ≤ k_trend_min 时。', suggested: '0.6' },
    { key: 'ai.cpl.w_range', label: '震荡档 S_hp 权重', type: 'number', defaultValue: 0.5, min: 0.5, max: 0.9, step: 0.05,
      description: 'k ≤ k_range_max 时（强 AI 过滤，C_ai 上限 0.5）。', suggested: '0.5' },
    { key: 'ai.cpl.k_trend_min', label: '趋势 k 下界', type: 'number', defaultValue: 1.2, min: 1, max: 2, step: 0.05,
      description: 'k > 此值判趋势档。', suggested: '1.2' },
    { key: 'ai.cpl.k_range_max', label: '震荡 k 上界', type: 'number', defaultValue: 0.5, min: 0.3, max: 1, step: 0.05,
      description: 'k ≤ 此值判震荡档。', suggested: '0.5' },
    { key: 'ai.cpl.tier_high', label: '高总分档', type: 'number', defaultValue: 85, min: 60, max: 100, step: 1,
      description: '总分 > 此值 → 基础手数 × lot_high。', suggested: '85' },
    { key: 'ai.cpl.tier_mid', label: '中总分档', type: 'number', defaultValue: 70, min: 50, max: 100, step: 1,
      description: '总分 > 此值 → 基础手数 × 1。', suggested: '70' },
    { key: 'ai.cpl.tier_low', label: '低总分档', type: 'number', defaultValue: 60, min: 40, max: 100, step: 1,
      description: '总分 > 此值 → 基础手数 × lot_low；≤ 此值不发信号。', suggested: '60' },
    { key: 'ai.cpl.lot_high', label: '高手数倍率', type: 'number', defaultValue: 1.5, min: 1, max: 3, step: 0.1,
      description: '高总分档手数倍率。', suggested: '1.5' },
    { key: 'ai.cpl.lot_low', label: '低手数倍率', type: 'number', defaultValue: 0.5, min: 0.1, max: 1, step: 0.1,
      description: '低总分档手数倍率。', suggested: '0.5' },
  ],

  cont: [
    { key: 'ai.cont.enabled', label: '持仓调仓开关', type: 'switch', defaultValue: false,
      description: 'true=用 continuity_score 调仓（默认关，独立 kill-switch）。', suggested: 'false' },
    { key: 'ai.cont.strong_min', label: '强延续下界', type: 'number', defaultValue: 70, min: 50, max: 100, step: 1,
      description: 'continuity ≥ 此值 → 放宽止盈/追踪/允许加仓复核。', suggested: '70' },
    { key: 'ai.cont.weak_max', label: '弱延续上界', type: 'number', defaultValue: 49, min: 0, max: 60, step: 1,
      description: 'continuity < 此值 → 收紧止损/压缩止盈/锁利。', suggested: '49' },
    { key: 'ai.cont.mode', label: '调仓执行模式', type: 'select',
      options: [{ label: '仅记录（log）', value: 'log' }, { label: '真实执行（act）', value: 'act' }],
      defaultValue: 'log',
      description: 'log=仅记录；act=真实执行（红线，另行确认）。', suggested: 'log' },
  ],
};

const GROUP_ORDER = ['top', 'lm', 'ds', 'fuse', 'cpl', 'cont'];
const GROUP_TITLES: Record<string, string> = {
  top: '总开关', lm: 'LightGBM 本地评分器 (AI_LM)', ds: 'DeepSeek 异步数据源 (AI_DS)',
  fuse: '融合权重 (AI_FUSE)', cpl: '耦合闸门 (AI_CPL)', cont: '持仓调仓 (AI_CONT)',
};
const GROUP_SUMMARY: Record<string, string> = {
  top: 'AI 模块总开关 + 耦合/解耦模式',
  lm: '信号真假概率 p：否决/降级/保持/升级 + 标签与重训参数',
  ds: 'DeepSeek 后台异步 3 输出（真假概率/sl_coeff/continuity）+ 降级默认',
  fuse: 'LightGBM + DeepSeek 校准融合成单一 c_ai（闭环核心，供耦合闸门裁决）',
  cpl: 'Hexp+AI 动态权重耦合公式 + 总分→手数分档',
  cont: 'continuity_score 三档持仓调仓（默认 log-only）',
};
const GROUP_COLORS: Record<string, string> = {
  top: '#64748b', lm: '#22c55e', ds: '#a855f7', fuse: '#f59e0b', cpl: '#3b82f6', cont: '#ef4444',
};

const buildFields = (): ConfigField[] => {
  const out: ConfigField[] = [];
  for (const g of GROUP_ORDER) {
    out.push({ key: `sec_${g}`, label: GROUP_TITLES[g], type: 'section', color: GROUP_COLORS[g], description: GROUP_SUMMARY[g] });
    out.push(...FIELDS[g]);
  }
  return out;
};

export default function AiQualityConfig() {
  const [config, setConfig] = useState<AiConfig>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ msg: string; severity: 'success' | 'error' } | null>(null);
  const [guardOpen, setGuardOpen] = useState(false);
  const pwVerified = useRef(false);
  const formRef = useRef<ConfigFormHandle>(null);
  const { user } = useAuth();
  const groupedFields = useMemo(() => buildFields(), []);

  useEffect(() => {
    (async () => {
      try {
        const resp = await client.get<AiConfig>('/api/v1/ai/config');
        const body = resp.data as { code?: number; data?: AiConfig; message?: string };
        const d = body && body.data ? body.data : (resp.data as AiConfig);
        setConfig(d || {});
      } catch (e) {
        console.error('加载 AI 质量配置失败', e);
        setToast({ msg: '加载失败，请重试', severity: 'error' });
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const handleSave = async (values: Record<string, unknown>) => {
    if (!pwVerified.current) {
      setToast({ msg: '请先通过密码鉴权再保存', severity: 'error' });
      return;
    }
    pwVerified.current = false;
    setSaving(true);
    const updates = Object.entries(values).map(([config_key, value]) => {
      let v = value;
      if (typeof value === 'boolean') v = value ? 'true' : 'false';
      else if (typeof value === 'number') v = String(value);
      return { config_key, value: v as string };
    });
    try {
      await client.put('/api/v1/ai/config', { updates });
      setConfig(values as AiConfig);
      setToast({ msg: 'AI 质量配置已保存', severity: 'success' });
    } catch (e) {
      console.error('保存 AI 质量配置失败', e);
      setToast({ msg: '保存失败，请重试', severity: 'error' });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" minHeight={200}>
        <CircularProgress />
      </Box>
    );
  }

  return (
    <Box>
      <Card>
        <CardHeader
          title="AI 信号质量模块配置"
          subheader="LightGBM 本地评分器 + DeepSeek 异步 + 耦合闸门 + 持仓调仓。默认全关（纯 HEXP）。悬停看说明与建议值。"
          action={
            <Button variant="contained" startIcon={<SaveIcon />} disabled={saving} onClick={() => setGuardOpen(true)}>
              {saving ? '保存中…' : '保存配置'}
            </Button>
          }
        />
        <Divider />
        <CardContent>
          <Alert severity="warning" sx={{ mb: 2 }}>
            纪律红线：AI 只有否决/降级/升级权，无独立开仓权；DeepSeek 仅后台异步，失败自动降级纯 HEXP。任何开关改动需密码授权。
          </Alert>
          <ConfigForm
            ref={formRef}
            fields={groupedFields}
            initialValues={config}
            onSubmit={handleSave}
            grouped
            groupColumns={2}
            memberColumns={2}
            hideSubmit
          />
        </CardContent>
      </Card>

      <SaveGuardDialog
        open={guardOpen}
        title="确认保存 AI 质量参数"
        description="即将写入 AI 质量配置中心（PG↔Redis）。请确认参数无误，输入登录密码授权保存。"
        username={user?.username}
        onClose={() => setGuardOpen(false)}
        onConfirmed={() => {
          pwVerified.current = true;
          formRef.current?.submit();
        }}
      />

      <Snackbar open={!!toast} autoHideDuration={3000} onClose={() => setToast(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity={toast?.severity ?? 'info'} onClose={() => setToast(null)}>{toast?.msg}</Alert>
      </Snackbar>
    </Box>
  );
}
