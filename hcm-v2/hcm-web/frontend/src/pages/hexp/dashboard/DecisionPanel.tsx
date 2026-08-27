/** A 区 · 核心决策 —— 看板最醒目的一行：方向 / 等级 / HP-Score / 闸门 / 行情快照 */
import React, { useMemo } from 'react';
import { Box, Typography, Tooltip } from '@mui/material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { GaugeChart } from 'echarts/charts';
import { CanvasRenderer } from 'echarts/renderers';
import {
  C, cfgNum, dirColor, dirLabel, gradeColor, gradeDesc, fmt, fmtSigned,
  tsToClock, stateLabel, phaseLabel, phaseColor, directionDiag,
} from './types';
import type { HexpSnapshot, HexpConfig, TrendPhase, HexpAiSnapshot } from './types';

echarts.use([GaugeChart, CanvasRenderer]);

interface Props {
  snap: HexpSnapshot | null;
  cfg: HexpConfig | null;
  ageSec: number | null;
  symbol: string;
  aiSnap: HexpAiSnapshot | null;
}

/** 小号指标块 */
const Metric: React.FC<{ label: string; value: string; color?: string; hint?: string }> = ({
  label, value, color, hint,
}) => (
  <Tooltip title={hint || ''} arrow disableHoverListener={!hint}>
    <Box sx={{ minWidth: 92 }}>
      <Typography sx={{ fontSize: 11, color: C.textFaint, letterSpacing: '.04em' }}>
        {label}
      </Typography>
      <Typography
        sx={{
          fontSize: 17, fontWeight: 700, color: color || C.textMain,
          fontVariantNumeric: 'tabular-nums', lineHeight: 1.3,
        }}
      >
        {value}
      </Typography>
    </Box>
  </Tooltip>
);

const DecisionPanel: React.FC<Props> = ({ snap, cfg, ageSec, symbol, aiSnap }) => {
  const passTh = cfgNum(cfg, 'hexp.scorecard.pass_threshold');
  const bTh = cfgNum(cfg, 'hexp.scorecard.b_threshold');
  const aTh = cfgNum(cfg, 'hexp.scorecard.a_threshold');
  const hpFloor = cfgNum(cfg, 'hexp.scorecard.hp_floor');

  const dir = snap?.direction ?? 'NO_TRADE';
  const grade = snap?.grade ?? '—';
  const dc = dirColor(dir);
  const gc = gradeColor(grade);
  const hp = snap?.hp_score ?? 0;
  const total = snap?.scorecard_total ?? 0;
  const passed = snap?.passed ?? false;

  const tp: TrendPhase | null = snap?.trend_phase ?? null;
  const tpProgress = tp?.progress ?? 0;
  const tpPhase = tp?.phase ?? 'squeeze';
  const pc = phaseColor(tpPhase);
  // 2026-08-27 方向诊断：迟滞/死标签/翻转状态显式化（状态卡联动）
  const diag = directionDiag(snap);

  const stale = ageSec !== null && ageSec > 20;

  const gaugeOption = useMemo(() => ({
    series: [{
      type: 'gauge',
      startAngle: 200,
      endAngle: -20,
      min: 0,
      max: 100,
      radius: '96%',
      center: ['50%', '62%'],
      progress: { show: true, width: 9, itemStyle: { color: hp >= hpFloor ? C.info : C.flat } },
      axisLine: { lineStyle: { width: 9, color: [[1, 'rgba(148,163,184,0.18)']] } },
      pointer: {
        icon: 'path://M2,0 L-2,0 L-1,-58 L1,-58 Z',
        width: 5, length: '58%', offsetCenter: [0, 0],
        itemStyle: { color: hp >= hpFloor ? C.info : C.flat },
      },
      axisTick: { show: false },
      splitLine: { distance: -9, length: 8, lineStyle: { color: 'rgba(148,163,184,0.35)', width: 1 } },
      axisLabel: { distance: 10, color: C.textFaint, fontSize: 9 },
      anchor: { show: true, size: 10, itemStyle: { color: C.border } },
      detail: {
        valueAnimation: true,
        offsetCenter: [0, '32%'],
        fontSize: 26, fontWeight: 700,
        color: hp >= hpFloor ? C.textMain : C.textDim,
        formatter: (v: number) => v.toFixed(1),
      },
      title: { show: false },
      data: [{ value: Number(hp.toFixed(2)) }],
    }],
  }), [hp, hpFloor]);

  return (
    <Box
      sx={{
        display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 2.5,
        p: 2, borderRadius: 2,
        background: `linear-gradient(90deg, ${dc}14 0%, ${C.panelBg} 42%)`,
        border: `1px solid ${C.border}`,
        borderLeft: `4px solid ${dc}`,
      }}
    >
      {/* 方向灯 */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, minWidth: 210 }}>
        <Box
          sx={{
            width: 54, height: 54, borderRadius: '50%',
            background: dir === 'NO_TRADE' ? C.flatSoft : `${dc}28`,
            border: `2px solid ${dc}`,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            boxShadow: dir === 'NO_TRADE' ? 'none' : `0 0 18px ${dc}55`,
            flexShrink: 0,
          }}
        >
          <Typography sx={{ fontSize: 24, color: dc, lineHeight: 1 }}>
            {dir === 'BUY' ? '▲' : dir === 'SELL' ? '▼' : '■'}
          </Typography>
        </Box>
        <Box>
          <Typography sx={{ fontSize: 11, color: C.textFaint }}>交易方向</Typography>
          <Typography sx={{ fontSize: 21, fontWeight: 800, color: dc, lineHeight: 1.2 }}>
            {dirLabel(dir)}
          </Typography>
          <Typography sx={{ fontSize: 11, color: C.textDim }}>
            {symbol} · 主周期 {snap?.primary_period ?? '—'}
          </Typography>
          {/* 2026-08-27 方向诊断标签：死标签/迟滞维持/翻转放行 显式化 */}
          <Tooltip title={diag.desc} arrow>
            <Box
              sx={{
                display: 'inline-block', mt: 0.3, px: 0.8, py: 0.1, borderRadius: 0.6,
                fontSize: 9.5, fontWeight: 700,
                color: diag.color, border: `1px solid ${diag.color}66`, background: `${diag.color}14`,
                cursor: 'help',
              }}
            >
              {diag.tag}
            </Box>
          </Tooltip>
        </Box>
      </Box>

      {/* HP-Score 仪表 */}
      <Box sx={{ width: 150, height: 104, flexShrink: 0 }}>
        <ReactEChartsCore
          echarts={echarts}
          option={gaugeOption}
          style={{ height: '100%', width: '100%' }}
          notMerge
          lazyUpdate
        />
        <Typography sx={{ fontSize: 10, color: C.textFaint, textAlign: 'center', mt: -1.2 }}>
          HP耦合分(纯HEXP) · 下限 {fmt(hpFloor, 0)}
        </Typography>
      </Box>

      {/* 等级徽章 */}
      <Box sx={{ minWidth: 132 }}>
        <Typography sx={{ fontSize: 11, color: C.textFaint, mb: 0.5 }}>信号等级</Typography>
        <Box
          sx={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            minWidth: 52, height: 38, px: 1.5, borderRadius: 1.5,
            border: `2px solid ${gc}`, color: gc,
            fontSize: 20, fontWeight: 800, letterSpacing: '.06em',
            background: `${gc}12`,
          }}
        >
          {grade}
        </Box>
        <Typography sx={{ fontSize: 11, color: C.textDim, mt: 0.5 }}>
          {gradeDesc(grade)}
        </Typography>
      </Box>

      {/* 闸门 */}
      <Box sx={{ flex: 1, minWidth: 260 }}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, mb: 0.5 }}>
          <Typography sx={{ fontSize: 11, color: C.textFaint }}>交易闸门</Typography>
          <Typography
            sx={{ fontSize: 13, fontWeight: 700, color: passed ? C.up : C.textDim }}
          >
            {passed ? '✓ 放行' : '✗ 拦截'}
          </Typography>
          <Typography sx={{ fontSize: 12, color: C.textDim, fontVariantNumeric: 'tabular-nums' }}>
            总分 {fmt(total, 1)} / 门槛 {fmt(passTh, 0)}
          </Typography>
        </Box>
        {/* 进度条 + 门槛刻度 */}
        <Box sx={{ position: 'relative', height: 10, borderRadius: 5, background: 'rgba(148,163,184,0.14)' }}>
          <Box
            sx={{
              position: 'absolute', left: 0, top: 0, bottom: 0, borderRadius: 5,
              width: `${Math.min(100, Math.max(0, total))}%`,
              background: passed
                ? `linear-gradient(90deg, ${C.info}, ${C.up})`
                : `linear-gradient(90deg, ${C.flat}, ${C.textDim})`,
              transition: 'width .4s ease',
            }}
          />
          {[
            { v: passTh, label: '过', color: C.warn },
            { v: bTh, label: 'B', color: '#06b6d4' },
            { v: aTh, label: 'A', color: C.info },
          ].map((m) => (
            <Tooltip key={m.label} title={`${m.label} 门槛 ${fmt(m.v, 0)}`} arrow>
              <Box
                sx={{
                  position: 'absolute', top: -3, bottom: -3, width: 2,
                  left: `${Math.min(100, Math.max(0, m.v))}%`,
                  background: m.color, opacity: 0.85,
                }}
              />
            </Tooltip>
          ))}
        </Box>
        <Typography
          sx={{
            fontSize: 11, color: C.textDim, mt: 0.7,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}
          title={snap?.reason ?? ''}
        >
          {snap?.reason ?? '等待引擎快照…'}
        </Typography>
      </Box>

      {/* 行情与新鲜度 */}
      <Box sx={{ display: 'flex', gap: 2.5, flexWrap: 'wrap' }}>
        <Metric label="现价" value={fmt(snap?.close, 2)} />
        <Metric label="ATR" value={fmt(snap?.atr, 2)} hint="主周期平均真实波幅，用于 SL/TP 距离" />
        <Metric
          label="裁决 verdict"
          value={fmtSigned(snap?.verdict, 3)}
          color={(snap?.verdict ?? 0) > 0 ? C.up : (snap?.verdict ?? 0) < 0 ? C.down : C.textDim}
          hint="多周期共振加权裁决，正=偏多 负=偏空"
        />
        <Metric
          label="主周期状态"
          value={stateLabel((snap?.period_states ?? {})[snap?.primary_period ?? ''] ?? '')}
        />
        <Metric
          label="快照时间"
          value={tsToClock(snap?.ts)}
          color={stale ? C.warn : C.textMain}
          hint={
            ageSec === null ? '无数据'
              : `${ageSec.toFixed(0)}s 前更新${stale ? '（已超 TTL，数据可能陈旧）' : ''}`
          }
        />
      </Box>

      {/* AI / HEXP 分数卡（趋势进度条左前方，同一排）—— 口径严格区分 */}
      <Box sx={{ display: 'flex', gap: 2, alignItems: 'center', alignSelf: 'flex-start', flexWrap: 'wrap' }}>
        <Box>
          <Metric
            label="AI评分"
            value={aiSnap?.ai_score != null ? fmt(aiSnap.ai_score, 0) : '—'}
            color={aiSnap?.valid ? C.violet : (aiSnap?.ai_enabled ? C.warn : C.textFaint)}
            hint={
              aiSnap?.valid
                ? "LightGBM 实时推理中（enabled + 模型已加载 + 快照有效）"
                : (aiSnap?.ai_enabled
                    ? (aiSnap.status === 'degraded'
                        ? "AI 降级：模型已加载但自检失败（predict 返回 NaN/常量）。请检查模型文件版本，当前信号退化为纯 HEXP"
                        : "AI 降级：总开关已开启，但模型文件缺失/未加载（ai.lm.model_path）。请配置模型路径，当前信号退化为纯 HEXP")
                    : "AI 未启用（ai.enabled=false 或 sidecar 未运行）。仅用纯 HEXP 六维分决策")
            }
          />
          {aiSnap?.ai_score == null && (
            <Box
              sx={{
                display: 'inline-block',
                mt: 0.5,
                px: 1,
                py: 0.25,
                borderRadius: 1,
                fontSize: 11,
                fontWeight: 700,
                color: '#fff',
                backgroundColor: aiSnap?.valid ? C.violet : (aiSnap?.ai_enabled ? C.warn : C.textFaint),
              }}
            >
              {aiSnap?.valid ? 'AI 在线' : (aiSnap?.ai_enabled ? 'AI 降级' : 'AI 未启用')}
            </Box>
          )}
        </Box>
        <Box>
          <Metric
            label="耦合总分(含AI)"
            value={
              aiSnap?.total_score != null ? fmt(aiSnap.total_score, 0)
                : (snap?.scorecard_total != null ? fmt(snap.scorecard_total, 0) : '—')
            }
            color={aiSnap?.total_score != null ? C.gold : C.warn}
            hint={
              aiSnap?.total_score != null
                ? "决策依据 = 耦合综合总分 w·S_hp+(1-w)·C_ai（HP耦合分与AI融合）。闸门对比此值。"
                : "纯 HEXP 六维总分（AI 未接入，无耦合加权）"
            }
          />
          {aiSnap?.total_score == null && (
            <Box
              sx={{
                display: 'inline-block',
                mt: 0.5,
                px: 1,
                py: 0.25,
                borderRadius: 1,
                fontSize: 11,
                fontWeight: 700,
                color: '#fff',
                backgroundColor: C.warn,
              }}
            >
              纯 HEXP
            </Box>
          )}
        </Box>
        <Box>
          <Metric
            label="HEXP六维分(纯技术)"
            value={snap?.scorecard_total != null ? fmt(snap.scorecard_total, 0) : '—'}
            color={C.info}
            hint={
              "纯 HEXP 六维加权技术分（未过 AI 耦合）。与左侧「耦合总分」算法不同，仅供对照，不参与闸门决策。"
            }
          />
        </Box>
        <Metric
          label="外部因子评分"
          value={aiSnap?.ext_factor_score != null ? fmt(aiSnap.ext_factor_score, 0) : '—'}
          color={C.info}
          hint="宏观/事件/情绪综合分×100（hcm-market-intel 已存在，复用）"
        />
      </Box>

      {/* 趋势阶段进度条（右上方） */}
      <Box sx={{ marginLeft: 'auto', alignSelf: 'flex-start', minWidth: 220, maxWidth: 280 }}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, mb: 0.5 }}>
          <Typography sx={{ fontSize: 11, color: C.textFaint }}>趋势阶段</Typography>
          <Typography sx={{ fontSize: 13, fontWeight: 700, color: pc }}>
            {phaseLabel(tpPhase)}
          </Typography>
          <Typography sx={{ fontSize: 11, color: C.textDim, fontVariantNumeric: 'tabular-nums' }}>
            {fmt(tpProgress, 0)}/100
          </Typography>
        </Box>
        <Tooltip
          arrow
          title={tp
            ? `蓄势 ${fmt(tp.squeeze, 0)} · 点火 ${fmt(tp.ignite, 0)} · 确立 ${fmt(tp.establish, 0)}`
            : '引擎尚未发布趋势阶段数据'}
        >
          <Box sx={{ position: 'relative', height: 10, borderRadius: 5, background: 'rgba(148,163,184,0.14)' }}>
            <Box
              sx={{
                position: 'absolute', inset: 0, borderRadius: 5, opacity: 0.22,
                background: `linear-gradient(90deg, ${C.flat} 0%, ${C.warn} 33%, ${C.violet} 66%, ${C.violet} 100%)`,
              }}
            />
            <Box
              sx={{
                position: 'absolute', left: 0, top: 0, bottom: 0, borderRadius: 5,
                width: `${Math.min(100, Math.max(0, tpProgress))}%`,
                background: `linear-gradient(90deg, ${C.flat}, ${C.warn}, ${C.violet})`,
                transition: 'width .4s ease',
              }}
            />
            {[33, 66].map((v) => (
              <Box
                key={v}
                sx={{
                  position: 'absolute', top: -3, bottom: -3, width: 2,
                  left: `${v}%`, background: '#f1f5f9', opacity: 0.65,
                }}
              />
            ))}
          </Box>
        </Tooltip>
        <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 0.3 }}>
          <Typography sx={{ fontSize: 9, color: C.textFaint }}>预启动</Typography>
          <Typography sx={{ fontSize: 9, color: C.textFaint }}>启动</Typography>
          <Typography sx={{ fontSize: 9, color: C.textFaint }}>确立</Typography>
        </Box>
      </Box>
    </Box>
  );
};

export default DecisionPanel;
