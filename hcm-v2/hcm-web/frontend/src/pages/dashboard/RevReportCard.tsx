import React from 'react';
import { Box, Typography } from '@mui/material';
import type { RevDay } from './Positions';

interface Props { days: RevDay[]; loading: boolean; }

/** 反转头每日绩效（设计方案 20260904 §5 表一/表五）：全品种聚合口径，不随上方品种筛选。
 *  小样本纪律：n_settled<10 仅观测不作结论（§9-2）。saved=ok绿 / killed=block红。 */
export default function RevReportCard({ days, loading }: Props): JSX.Element {
  if (days.length === 0) {
    return (
      <Box className="card mt-4">
        <Typography variant="caption" className="text-gray-400 uppercase tracking-wider block mb-1">
          反转头日报（全品种 · 不随上方品种筛选）
        </Typography>
        <Typography className="text-gray-500 text-center py-8">
          {loading ? '加载中…' : '观测中（样本不足）：需反转头 mode=log 积累 shadow 归因样本后出数'}
        </Typography>
      </Box>
    );
  }
  const L = days[days.length - 1];
  const win = L.n_saved + L.n_killed;
  const sum7 = days.slice(-7).reduce((a, d) => a + d.sum_delta_r, 0);
  const recents = L.detail?.recent || [];
  const note = L.detail?.note || '';
  const kpis: Array<[string, string, string]> = [
    ['ΣΔ_R 7日', `${sum7 >= 0 ? '+' : ''}${sum7.toFixed(2)}R`, sum7 >= 0 ? 'text-green-400' : 'text-red-400'],
    ['当日结算', String(L.n_settled), 'text-gray-200'],
    ['saved/killed', `${L.n_saved}/${L.n_killed}`, 'text-gray-200'],
    ['动作胜率', win > 0 ? `${Math.round((L.n_saved / win) * 100)}%` : '—', 'text-gray-200'],
    ['Shadow/Act', `${L.n_shadow}/${L.n_act}`, 'text-gray-200'],
  ];
  return (
    <Box className="card mt-4">
      <Typography variant="caption" className="text-gray-400 uppercase tracking-wider block mb-2">
        反转头日报（全品种 · 不随上方筛选）· 结算日 {L.date}
      </Typography>
      <Box className="grid grid-cols-2 md:grid-cols-5 gap-2 mb-3 text-sm">
        {kpis.map(([k, v, c]) => (
          <Box key={k} className="bg-gray-800/40 rounded p-2">
            <Typography className="text-xs text-gray-400">{k}</Typography>
            <Typography className={`text-base font-semibold ${c}`}>{v}</Typography>
          </Box>
        ))}
      </Box>
      {L.n_settled < 10 && (
        <Typography className="text-xs text-gray-500 mb-2">
          结算样本不足（{L.n_settled} &lt; 10）→ 仅观测，不作结论（小样本纪律）
        </Typography>
      )}
      <Typography variant="caption" className="text-gray-400 block mb-1">
        最近已结算明细（saved=ok绿 / killed=block红，Δ_R = delta/atr）
      </Typography>
      {recents.length === 0 ? (
        <Typography className="text-gray-500 text-xs py-2">暂无已结算样本</Typography>
      ) : (
        <Box className="overflow-x-auto">
          <table className="w-full">
            <tbody>
              {recents.map((r) => {
                const tone = r.verdict === 'saved' ? 'text-green-400' : r.verdict === 'killed' ? 'text-red-400' : 'text-gray-400';
                return (
                  <tr key={r.ticket} className="border-b border-gray-800">
                    <td className="py-1 px-2 text-xs text-gray-300">{r.ticket}</td>
                    <td className="py-1 px-2 text-xs text-gray-300">{r.symbol} {r.direction}</td>
                    <td className="py-1 px-2 text-xs text-right text-gray-300">sc {r.score.toFixed(3)}</td>
                    <td className="py-1 px-2 text-xs text-right text-gray-400">{r.old_sl.toFixed(2)}→{r.new_sl.toFixed(2)}</td>
                    <td className={`py-1 px-2 text-xs text-right ${r.delta_r >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {r.delta_r >= 0 ? '+' : ''}{r.delta_r.toFixed(2)}R
                    </td>
                    <td className={`py-1 px-2 text-xs text-right ${tone}`}>{r.verdict}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Box>
      )}
      {note ? <Typography className="text-xs text-gray-500 mt-2">口径注记：{note}</Typography> : null}
    </Box>
  );
}
