import React, { useState, useEffect, useRef, forwardRef, useImperativeHandle } from 'react';
import { Box, TextField, Button, Typography, Alert, CircularProgress, Tooltip } from '@mui/material';
import { Save, Undo, RefreshCw } from './Icons';

export interface ConfigField {
  key: string;
  label: string;
  type: 'text' | 'number' | 'select' | 'textarea' | 'password' | 'switch' | 'section';
  defaultValue?: string | number | boolean;
  options?: { label: string; value: string }[];
  required?: boolean;
  placeholder?: string;
  disabled?: boolean;
  min?: number;
  max?: number;
  step?: number;
  multiline?: boolean;
  rows?: number;
  description?: string;
  /** 建议运行值（悬停提示用，仅前端展示，不影响引擎计算）。 */
  suggested?: string;
  /** 分组/字段主题色（hex）。section 用它渲染标题；非 section 字段继承所属分组的颜色。 */
  color?: string;
}

interface ConfigFormProps {
  /** 表单标题；嵌入其它卡片（如已自带标题的 Config 页）时可省略 */
  title?: string;
  fields: ConfigField[];
  initialValues?: Record<string, string | number | boolean>;
  onSubmit: (values: Record<string, string | number | boolean>) => Promise<Record<string, string | number | boolean> | void | undefined>;
  onCancel?: () => void;
  loading?: boolean;
  apiEndpoint?: string;
  fetchValues?: () => Promise<Record<string, string | number | boolean>>;
  formKey?: number;
  /** 分组竖排模式：为 true 时按 section 把字段拆成竖排分色卡片（每组一种颜色）。默认 false 保持平铺网格。 */
  grouped?: boolean;
  /** 分组列数：1=单列（默认），2=双列（按 section 顺序左右分）。仅 grouped=true 生效。 */
  groupColumns?: number;
  /** 组内字段列数：仅 grouped=true 生效。1=单列竖排（默认），2/3=横排多列（同时缩短参数条长度）。 */
  memberColumns?: number;
  /** 隐藏底部 保存/取消/重置 按钮（交由外部自定义按钮触发，如 CardHeader 保存键）。默认 false。 */
  hideSubmit?: boolean;
  /** 外部可控的 form 元素 ref，供父组件精确触发 requestSubmit()（避免全局 document.querySelector('form') 的歧义）。 */
  formRef?: React.Ref<HTMLFormElement>;
}

/** 暴露给父组件的命令式句柄：用于在不依赖全局 DOM 查询的情况下触发提交。 */
export interface ConfigFormHandle {
  /** 以当前表单值触发一次提交（等价于点击保存）。 */
  submit: () => void;
}

const ConfigForm = forwardRef<ConfigFormHandle, ConfigFormProps>(({
  title,
  fields,
  initialValues,
  onSubmit,
  onCancel,
  loading: externalLoading = false,
  apiEndpoint,
  fetchValues,
  formKey = 0,
  grouped = false,
  groupColumns = 1,
  memberColumns = 1,
  hideSubmit = false,
  formRef,
}, ref) => {
  const [values, setValues] = useState<Record<string, string | number | boolean>>(() => {
    if (initialValues) return { ...initialValues };
    const defaults: Record<string, string | number | boolean> = {};
    fields.filter((f) => f.type !== 'section').forEach((f) => {
      if (f.defaultValue !== undefined) defaults[f.key] = f.defaultValue;
    });
    return defaults;
  });

  const initialValuesRef = useRef(initialValues);
  useEffect(() => {
    // Only sync when initialValues changes externally and form is not dirty
    if (initialValues && JSON.stringify(initialValues) !== JSON.stringify(initialValuesRef.current)) {
      initialValuesRef.current = initialValues;
      setValues({ ...initialValues });
      setDirty(false);
    }
  }, [initialValues]);

  const [internalLoading, setInternalLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [dirty, setDirty] = useState<boolean>(false);

  const loading: boolean = externalLoading || internalLoading;

  const handleChange = (key: string, value: string | number | boolean): void => {
    setValues((prev) => ({ ...prev, [key]: value }));
    setDirty(true);
    setError(null);
    setSuccess(null);
  };

  const handleReset = async (): Promise<void> => {
    if (fetchValues) {
      setInternalLoading(true);
      try {
        const fetched = await fetchValues();
        setValues(fetched);
        setDirty(false);
        setSuccess('已从服务器重新加载配置');
      } catch {
        setError('加载配置失败');
      } finally {
        setInternalLoading(false);
      }
    } else {
      const defaults: Record<string, string | number | boolean> = {};
      fields.filter((f) => f.type !== 'section').forEach((f) => {
        if (f.defaultValue !== undefined) defaults[f.key] = f.defaultValue;
      });
      setValues(defaults);
      setDirty(false);
    }
  };

  // 暴露命令式提交句柄：父组件（如带密码守卫的保存按钮）可精确触发一次提交，
  // 避免依赖全局 document.querySelector('form') 选取到错误表单导致保存请求根本不发出。
  useImperativeHandle(ref, () => ({
    submit: () => {
      void handleSubmit();
    },
  }));

  const handleSubmit = async (): Promise<void> => {
    setInternalLoading(true);
    setError(null);
    setSuccess(null);
    try {
      const result = await onSubmit(values);
      setDirty(false);
      setSuccess('配置已保存');
      // If parent returned the new values, apply them directly
      if (result && typeof result === 'object') {
        setValues({ ...(result as Record<string, string | number | boolean>) });
        initialValuesRef.current = result as Record<string, string | number | boolean>;
      }
    } catch (err: unknown) {
      const message: string =
        err instanceof Error ? err.message : '保存失败';
      setError(message);
    } finally {
      setInternalLoading(false);
    }
  };

  // 计算每个字段所属分组的主题色（section 自身 color 或最近一个 section 的 color）
  const fieldColors: string[] = (() => {
    let cur = '#3b82f6';
    return fields.map((f) => {
      if (f.type === 'section') cur = f.color || '#3b82f6';
      return cur;
    });
  })();

  // 分组竖排模式：把 fields 按 section 拆成 [{section, color, members[]}]
  type FieldGroup = { section: ConfigField | null; color: string; members: ConfigField[] };
  const buildGroups = (): FieldGroup[] => {
    const groups: FieldGroup[] = [];
    let current: FieldGroup | null = null;
    for (const f of fields) {
      if (f.type === 'section') {
        current = { section: f, color: f.color || '#3b82f6', members: [] };
        groups.push(current);
      } else {
        if (!current) {
          current = { section: null, color: '#3b82f6', members: [] };
          groups.push(current);
        }
        current.members.push(f);
      }
    }
    return groups;
  };

  const renderField = (field: ConfigField, groupColor?: string, plainWrap = false): React.ReactNode => {
    const value = values[field.key];
    const fieldControl = (() => {
      switch (field.type) {
        case 'section':
          return (
            <Box sx={{ mt: 3, mb: 1, pb: 1, borderBottom: `1px solid ${field.color || '#334155'}`, gridColumn: '1 / -1' }}>
              <Typography
                variant="subtitle2"
                sx={{ color: field.color || '#3b82f6', fontWeight: 600, fontSize: '0.85rem', letterSpacing: '0.02em' }}
              >
                {field.label}
              </Typography>
              {field.description && (
                <Typography variant="caption" sx={{ color: '#64748b', mt: 0.25, display: 'block' }}>
                  {field.description}
                </Typography>
              )}
            </Box>
          );
        case 'text':
        case 'password':
          return (
            <TextField
              fullWidth
              size="small"
              type={field.type}
              label={field.label}
              value={value as string}
              onChange={(e) => handleChange(field.key, e.target.value)}
              required={field.required}
              placeholder={field.placeholder}
              disabled={field.disabled || loading}
              InputLabelProps={{ shrink: true, sx: { backgroundColor: '#0f0f19', px: 0.5 } }}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            />
          );
        case 'number':
          return (
            <TextField
              fullWidth
              size="small"
              type="number"
              label={field.label}
              value={value as number}
              onChange={(e) => {
                // 0.3 不会被吞——保留原始值，不使用 || 0（会吞 0.0 和中间态 "0."）
                const raw = e.target.value;
                if (raw === '' || raw === '-') {
                  handleChange(field.key, 0);
                  return;
                }
                const num = parseFloat(raw);
                handleChange(field.key, Number.isNaN(num) ? 0 : num);
              }}
              required={field.required}
              disabled={field.disabled || loading}
              inputProps={{ min: field.min, max: field.max, step: field.step ?? 'any' }}
              InputLabelProps={{ shrink: true, sx: { backgroundColor: '#0f0f19', px: 0.5 } }}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            />
          );
        case 'textarea':
          return (
            <TextField
              fullWidth
              size="small"
              multiline
              rows={field.rows || 4}
              label={field.label}
              value={value as string}
              onChange={(e) => handleChange(field.key, e.target.value)}
              required={field.required}
              placeholder={field.placeholder}
              InputLabelProps={{ shrink: true, sx: { backgroundColor: '#0f0f19', px: 0.5 } }}
              disabled={field.disabled || loading}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            />
          );
        case 'select':
          return (
            <TextField
              fullWidth
              size="small"
              select
              label={field.label}
              value={value as string}
              onChange={(e) => handleChange(field.key, e.target.value)}
              required={field.required}
              disabled={field.disabled || loading}
              SelectProps={{ native: true }}
              InputLabelProps={{ shrink: true, sx: { backgroundColor: '#0f0f19', px: 0.5 } }}
              sx={{ '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' } }}
            >
              {field.options?.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </TextField>
          );
        case 'switch':
          return (
            <Box className="flex items-center justify-between py-1">
              <Typography variant="body2" className="text-gray-300">
                {field.label}
              </Typography>
              <button
                type="button"
                disabled={field.disabled || loading}
                onClick={() => handleChange(field.key, !value)}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors duration-200 ${
                  value ? 'bg-blue-600' : 'bg-gray-600'
                } ${field.disabled || loading ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform duration-200 ${
                    value ? 'translate-x-6' : 'translate-x-1'
                  }`}
                />
              </button>
            </Box>
          );
        default:
          return null;
      }
    })();

    const tipParts: React.ReactNode[] = [];
    if (field.description) tipParts.push(<div key="d">作用：{field.description}</div>);
    if (field.suggested) tipParts.push(<div key="s">建议值：{field.suggested}</div>);
    const tipTitle: React.ReactNode = tipParts.length ? (
      <Box sx={{ whiteSpace: 'normal', maxWidth: 280, fontSize: '0.78rem', lineHeight: 1.5 }}>{tipParts}</Box>
    ) : null;

    return (
      <Tooltip
        key={field.key}
        title={tipTitle || field.label}
        placement="top"
        arrow
        disableHoverListener={!tipTitle}
      >
        <Box
          className="w-full"
          sx={
            !plainWrap && field.type !== 'section' && groupColor && groupColor !== '#3b82f6'
              ? {
                  borderLeft: `3px solid ${groupColor}`,
                  pl: 1,
                  backgroundColor: `${groupColor}14`,
                  borderRadius: '4px',
                }
              : undefined
          }
        >
          {fieldControl}
        </Box>
      </Tooltip>
    );
  };

  return (
    <Box key={formKey} component="form" ref={formRef} onSubmit={(e) => { e.preventDefault(); void handleSubmit(); }}>
      {title && (
        <Typography variant="h6" className="text-gray-100 mb-6 font-semibold">
          {title}
        </Typography>
      )}

      {apiEndpoint && (
        <Typography variant="caption" className="text-gray-500 block mb-4">
          API: {apiEndpoint}
        </Typography>
      )}

      {error && (
        <Alert severity="error" sx={{ mb: 2, backgroundColor: '#3b1111', color: '#fca5a5' }}>
          {error}
        </Alert>
      )}

      {success && (
        <Alert severity="success" sx={{ mb: 2, backgroundColor: '#0a2e1a', color: '#86efac' }}>
          {success}
        </Alert>
      )}

      {grouped ? (
        // ── 分组竖排：每组一张分色卡片，竖向排列；组内字段网格铺开 ──
        (() => {
          const groups = buildGroups();
          const cols = Math.max(1, groupColumns);
          // 按列数切片：cols=2 时 [g0,g1] 左列、[g2,g3] 右列
          const colsArr: FieldGroup[][] = Array.from({ length: cols }, () => []);
          groups.forEach((g, i) => colsArr[i % cols].push(g));
          return (
            <Box
              className="mb-6"
              sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: `repeat(${cols}, minmax(0, 1fr))` }, gap: 2 }}
            >
              {colsArr.map((col, ci) => (
                <Box key={ci} className="flex flex-col gap-4">
                  {col.map((g, gi) => (
                    <Box
                      key={gi}
                      sx={{
                        borderLeft: `4px solid ${g.color}`,
                        backgroundColor: `${g.color}0f`,
                        border: `1px solid ${g.color}33`,
                        borderLeftWidth: '4px',
                        borderRadius: '8px',
                        p: 2,
                      }}
                    >
                      {g.section && (
                        <Box sx={{ mb: 1.5, pb: 1, borderBottom: `1px solid ${g.color}44` }}>
                          <Typography
                            variant="subtitle2"
                            sx={{ color: g.color, fontWeight: 700, fontSize: '0.9rem', letterSpacing: '0.02em' }}
                          >
                            {g.section.label}
                          </Typography>
                          {g.section.description && (
                            <Typography variant="caption" sx={{ color: '#94a3b8', mt: 0.25, display: 'block' }}>
                              {g.section.description}
                            </Typography>
                          )}
                        </Box>
                      )}
                      <Box
                        className="grid gap-3"
                        sx={{
                          gridTemplateColumns: {
                            xs: '1fr',
                            sm: memberColumns >= 2 ? `repeat(2, minmax(0, 1fr))` : '1fr',
                            lg: memberColumns >= 2 ? `repeat(${memberColumns}, minmax(0, 1fr))` : '1fr',
                          },
                        }}
                      >
                        {g.members.map((f) => renderField(f, g.color, true))}
                      </Box>
                    </Box>
                  ))}
                </Box>
              ))}
            </Box>
          );
        })()
      ) : (
        <Box className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
          {fields.map((f, i) => renderField(f, fieldColors[i]))}
        </Box>
      )}

      {!hideSubmit && (
      <Box className="flex gap-3">
        <Button
          variant="contained"
          type="submit"
          startIcon={loading ? <CircularProgress size={16} /> : <Save />}
          onClick={handleSubmit}
          disabled={loading || !dirty}
          sx={{
            backgroundColor: '#3b82f6',
            '&:hover': { backgroundColor: '#2563eb' },
            '&.Mui-disabled': { backgroundColor: '#1e3a5f', color: '#64748b' },
          }}
        >
          保存
        </Button>
        <Button
          variant="outlined"
          startIcon={<Undo />}
          onClick={onCancel}
          disabled={loading}
          sx={{
            borderColor: '#4b5563',
            color: '#94a3b8',
            '&:hover': { borderColor: '#6b7280', backgroundColor: 'rgba(255,255,255,0.04)' },
          }}
        >
          取消
        </Button>
        <Button
          variant="text"
          startIcon={<RefreshCw />}
          onClick={handleReset}
          disabled={loading}
          sx={{ color: '#64748b', '&:hover': { color: '#94a3b8' } }}
        >
          重置
        </Button>
      </Box>
      )}
    </Box>
  );
});

export default ConfigForm;
