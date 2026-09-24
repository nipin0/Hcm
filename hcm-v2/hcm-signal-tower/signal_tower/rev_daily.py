"""反转头每日绩效聚合（设计方案 §4/§7.1）→ hcm_ai.rev_daily_report（幂等 upsert, fail-open）。

【2026-09-21 0052 · 评估能力修复】三处口径变更，目的都是让这份报表**能回答
"反转头到底有没有用"**（旧报表在结构上回答不了，见下）：

  ① **只统计 `cf_basis='v2_race'`**（0052 新口径）。历史行是 0011 的
     `v1_window_to_exit` 口径（`cf_basis IS NULL`），其 delta 与 v2 **不可比** ——
     本仓库反复出过同一类事故：**口径变了却不标注 ⇒ 新旧混算**。
     旧口径结算数单列于 `detail.legacy_v1_settled`，让口径切换**可见**而非静默消失。

  ② **主指标只取 `mode='act'`（真实动作）**，shadow（`log`/`log_skip`，未行动）单列进
     `detail.settle_shadow`。理由：v1 主指标 `sum_delta_r` 全期 564.07 R **全部来自
     "未行动"的行**（实测 100 条非零 delta 全部落在 `log_skip`），而它度量的是
     "实际出场 vs 止损本该被打"的差，**与反转头动作无关** ⇒ **数字大 ≠ 反转头有用**。

  ③ **新增 `detail.act_buckets`：act 行按 score 分箱 × saved/killed/ΣR** ——
     这才是可用的「决策质量」检查（高置信的动作是否更常 saved？）。
     旧 `_Q3` 分箱只看 shadow 行的 `rev_rate`，而那只是"**被判**反转的比例"
     （= 阈值率的复述；表内无真值列），**不是准确率** ⇒ 降为参考项保留。

口径（沿用，未变）：漏斗/动作按 `adj_ts` 归日、结算按 `closed_ts` 归日；
主指标 `sum_delta_r = Σ(delta/atr)`（`delta` 为**价格差**口径、**未乘手数**）；
`n_trigger_pos`/`n_requests` 暂无埋点源恒 0。

⚠ **结算现滞后平仓 H**（默认 12h，见 `ai.rev.cf_horizon_hours`）：
  `_rev_settle_closed` 只在 `adj_ts + H` 过后才结算该行，以保证反事实赛跑窗口内
  K 线完整。故"当日结算数"会晚 H 小时才补齐（可评估性换时效性）。
"""
import json, logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# 漏斗（adj_ts 归日）：模式维度的分列在 v1 就是对的 —— 保留
_Q1 = "SELECT COUNT(*)::int n, COUNT(*) FILTER (WHERE is_reversal)::int rc, COUNT(*) FILTER (WHERE NOT is_reversal)::int pc, COUNT(*) FILTER (WHERE mode='act')::int act, COUNT(*) FILTER (WHERE mode IN ('log','log_skip'))::int sh FROM hcm_ai.reversal_attribution WHERE adj_ts>=$1 AND adj_ts<$2"
# 结算（closed_ts 归日）：【0052】按 mode 拆组 + 只取 v2 口径
_Q2 = "SELECT CASE WHEN mode='act' THEN 'act' ELSE 'shadow' END AS mg, COUNT(*)::int n, COUNT(*) FILTER (WHERE verdict='saved')::int sv, COUNT(*) FILTER (WHERE verdict='killed')::int kl, COUNT(*) FILTER (WHERE verdict='neutral')::int nt, COUNT(*) FILTER (WHERE verdict='ambiguous')::int amb, COALESCE(SUM(delta),0)::float8 sd, COALESCE(SUM(delta/NULLIF(atr,0)),0)::float8 sr FROM hcm_ai.reversal_attribution WHERE closed_ts>=$1 AND closed_ts<$2 AND cf_basis='v2_race' GROUP BY 1"
# 【0052】旧口径（v1）结算数：单列，让"口径切换"可见，而不是让数字静默消失
_Q2L = "SELECT COUNT(*)::int n FROM hcm_ai.reversal_attribution WHERE closed_ts>=$1 AND closed_ts<$2 AND cf_basis IS DISTINCT FROM 'v2_race'"
# 【0052】act 行按置信度分箱 —— 可用的"决策质量"检查（v1 缺这一项）
_Q3A = "SELECT floor(score*10)::int b, COUNT(*)::int n, COUNT(*) FILTER (WHERE verdict='saved')::int sv, COUNT(*) FILTER (WHERE verdict='killed')::int kl, COALESCE(SUM(delta/NULLIF(atr,0)),0)::float8 sr FROM hcm_ai.reversal_attribution WHERE closed_ts>=$1 AND closed_ts<$2 AND cf_basis='v2_race' AND mode='act' AND score IS NOT NULL GROUP BY 1 ORDER BY 1"
# shadow 行的 score 分箱：⚠ 其 rr 只是"被判反转的比例"，**不是准确率**（表内无真值列），仅作参考
_Q3 = "SELECT floor(score*10)::int b, COUNT(*)::int n, COUNT(*) FILTER (WHERE is_reversal)::int rc, ROUND(COUNT(*) FILTER (WHERE is_reversal)::numeric/NULLIF(COUNT(*),0),4) rr, ROUND(COUNT(*) FILTER (WHERE verdict='saved')::numeric/NULLIF(COUNT(*),0),4) vrs FROM hcm_ai.reversal_attribution WHERE closed_ts>=$1 AND closed_ts<$2 AND cf_basis='v2_race' AND mode IN ('log','log_skip') AND score IS NOT NULL GROUP BY 1 ORDER BY 1"
_Q4 = "SELECT CASE WHEN is_reversal THEN 'rev' ELSE 'pull' END vd, CASE WHEN mode='act' THEN 'act' ELSE 'shadow' END md, CASE WHEN EXTRACT(HOUR FROM adj_ts AT TIME ZONE 'UTC')<8 THEN 'asia' WHEN EXTRACT(HOUR FROM adj_ts AT TIME ZONE 'UTC')<13 THEN 'europe' WHEN EXTRACT(HOUR FROM adj_ts AT TIME ZONE 'UTC')<22 THEN 'us' ELSE 'asia' END ss, CASE WHEN dd_atr<1.1 THEN 'd1' WHEN dd_atr<1.6 THEN 'd2' ELSE 'd3' END dd, COUNT(*)::int n, COALESCE(SUM(delta/NULLIF(atr,0)),0)::float8 sr, COUNT(*) FILTER (WHERE verdict='saved')::int sv FROM hcm_ai.reversal_attribution WHERE closed_ts>=$1 AND closed_ts<$2 AND cf_basis='v2_race' GROUP BY 1,2,3,4"
_Q5 = "SELECT ticket, symbol, direction, ROUND(dd_atr::numeric,2) dd_atr, ROUND(score::numeric,4) score, verdict, mode, cf_touch, ROUND(old_sl::numeric,2) old_sl, ROUND(new_sl::numeric,2) new_sl, ROUND(tp::numeric,2) tp, ROUND(realized_pnl::numeric,2) realized, ROUND(cf_pnl::numeric,2) cf, ROUND(delta::numeric,2) delta, ROUND((delta/NULLIF(atr,0))::numeric,4) dr FROM hcm_ai.reversal_attribution WHERE closed_ts>=$1 AND closed_ts<$2 AND cf_basis='v2_race' AND verdict IS NOT NULL ORDER BY closed_ts DESC LIMIT 20"
_UPS = "INSERT INTO hcm_ai.rev_daily_report (stat_date, n_trigger_pos, n_requests, n_scored, n_rev_call, n_pull_call, n_act, n_shadow, n_settled, n_saved, n_killed, n_neutral, sum_delta, sum_delta_r, avg_delta_r, detail, updated_at) VALUES ($1,0,0,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,now()) ON CONFLICT (stat_date) DO UPDATE SET n_trigger_pos=0, n_requests=0, n_scored=$2, n_rev_call=$3, n_pull_call=$4, n_act=$5, n_shadow=$6, n_settled=$7, n_saved=$8, n_killed=$9, n_neutral=$10, sum_delta=$11, sum_delta_r=$12, avg_delta_r=$13, detail=$14::jsonb, updated_at=now()"


def _grp(rows):
    """把 _Q2 的分组结果拍成 {mg: row}，缺失组补零。"""
    zero = {"n": 0, "sv": 0, "kl": 0, "nt": 0, "amb": 0, "sd": 0.0, "sr": 0.0}
    out = {"act": dict(zero), "shadow": dict(zero)}
    for r in (rows or []):
        g = r["mg"] if r["mg"] in out else "shadow"
        out[g] = {"n": int(r["n"] or 0), "sv": int(r["sv"] or 0), "kl": int(r["kl"] or 0),
                  "nt": int(r["nt"] or 0), "amb": int(r["amb"] or 0),
                  "sd": float(r["sd"] or 0), "sr": float(r["sr"] or 0)}
    return out


def _fmt_settle(g):
    return {"n_settled": g["n"], "n_saved": g["sv"], "n_killed": g["kl"],
            "n_neutral": g["nt"], "n_ambiguous": g["amb"],
            "sum_delta": round(g["sd"], 4), "sum_delta_r": round(g["sr"], 4)}


async def aggregate_rev_daily(db, trade_date: date) -> None:
    """聚合单日反转头绩效。

    口径：漏斗/动作按 adj_ts 归日、结算按 closed_ts 归日；主指标 sum_delta_r=Σ(delta/atr)；
    【0052】结算只取 cf_basis='v2_race' 且**主列仅为 mode='act'**（shadow 见 detail）；
    n_trigger_pos/n_requests 暂无埋点源恒 0。
    """
    ds = datetime.combine(trade_date, datetime.min.time())
    de = ds + timedelta(days=1)
    try:
        fn = await db.fetchrow(_Q1, ds, de)
        st = _grp(await db.fetch(_Q2, ds, de))
        _lg = await db.fetchrow(_Q2L, ds, de)
        ab = await db.fetch(_Q3A, ds, de)
        bk = await db.fetch(_Q3, ds, de)
        sl = await db.fetch(_Q4, ds, de)
        r20 = await db.fetch(_Q5, ds, de)
        # 主列 = 真实动作（act）；shadow 单列 —— v1 把两者混算，导致 564 R 的假业绩
        ns = st["act"]["n"]
        nsd, nkl, nnt = st["act"]["sv"], st["act"]["kl"], st["act"]["nt"]
        sdr = st["act"]["sr"]
        detail = {
            # 【0052 新增】act 行按置信度分箱：这才是可用的决策质量检查
            "act_buckets": [{"bucket": f"{r['b']/10:.1f}-{(r['b']+1)/10:.1f}",
                             "n": int(r["n"]), "n_saved": int(r["sv"]),
                             "n_killed": int(r["kl"]), "sum_delta_r": float(r["sr"] or 0)}
                            for r in (ab or [])],
            "settle_act": _fmt_settle(st["act"]),
            "settle_shadow": _fmt_settle(st["shadow"]),
            "legacy_v1_settled": int((_lg or {}).get("n") or 0),
            "buckets": [{"bucket": f"{r['b']/10:.1f}-{(r['b']+1)/10:.1f}", "n": int(r["n"]),
                         "n_rev_call": int(r["rc"]), "rev_rate": float(r["rr"] or 0),
                         "saved_rate": float(r["vrs"] or 0)} for r in (bk or [])],
            "slices": [{"verdict": r["vd"], "mode": r["md"], "session": r["ss"], "dd": r["dd"],
                        "n": int(r["n"]), "sum_delta_r": float(r["sr"] or 0),
                        "n_saved": int(r["sv"] or 0)} for r in (sl or [])],
            "recent": [{"ticket": int(r["ticket"]), "symbol": r["symbol"],
                        "direction": r["direction"], "dd_atr": float(r["dd_atr"] or 0),
                        "score": float(r["score"] or 0), "verdict": r["verdict"],
                        "mode": r["mode"], "cf_touch": r["cf_touch"],
                        "old_sl": float(r["old_sl"] or 0), "new_sl": float(r["new_sl"] or 0),
                        "tp": float(r["tp"] or 0),
                        "realized": float(r["realized"] or 0), "cf": float(r["cf"] or 0),
                        "delta": float(r["delta"] or 0), "delta_r": float(r["dr"] or 0)}
                       for r in (r20 or [])],
            "note": ("n_trigger_pos/n_requests 暂无埋点源恒 0；delta 为价格差口径、未乘手数；"
                     "自 2026-09-21(0052) 起：主列 n_settled/n_saved/n_killed/n_sum 仅统计 "
                     "cf_basis='v2_race' 且 mode='act'（真实动作），shadow 见 settle_shadow，"
                     "v1 旧口径见 legacy_v1_settled（口径不可比故不计入主列）；"
                     "buckets.rev_rate 是'被判反转的比例'而非准确率（表内无真值列）"),
        }
        await db.execute(_UPS, trade_date, int(fn["n"] or 0), int(fn["rc"] or 0),
                         int(fn["pc"] or 0), int(fn["act"] or 0), int(fn["sh"] or 0),
                         ns, nsd, nkl, nnt, round(st["act"]["sd"], 4), sdr,
                         round(sdr / ns, 4) if ns else None,
                         json.dumps(detail, ensure_ascii=False))
        logger.info("REV daily %s: scored=%d act=%d shadow=%d | settle(act) n=%d saved=%d "
                    "killed=%d sumR=%.3f | settle(shadow) n=%d | legacy_v1=%d",
                    trade_date, int(fn["n"] or 0), int(fn["act"] or 0), int(fn["sh"] or 0),
                    ns, nsd, nkl, sdr, st["shadow"]["n"],
                    int((_lg or {}).get("n") or 0))
    except Exception as exc:
        logger.warning("Aggregate REV daily failed for %s: %s", trade_date, exc)
