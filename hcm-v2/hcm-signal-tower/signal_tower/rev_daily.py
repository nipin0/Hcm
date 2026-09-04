"""反转头每日绩效聚合（设计方案 §4/§7.1）→ hcm_ai.rev_daily_report（幂等 upsert, fail-open）。"""
import json, logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

_Q1 = "SELECT COUNT(*)::int n, COUNT(*) FILTER (WHERE is_reversal)::int rc, COUNT(*) FILTER (WHERE NOT is_reversal)::int pc, COUNT(*) FILTER (WHERE mode='act')::int act, COUNT(*) FILTER (WHERE mode IN ('log','log_skip'))::int sh FROM hcm_ai.reversal_attribution WHERE adj_ts>=$1 AND adj_ts<$2"
_Q2 = "SELECT COUNT(*)::int n, COUNT(*) FILTER (WHERE verdict='saved')::int sv, COUNT(*) FILTER (WHERE verdict='killed')::int kl, COUNT(*) FILTER (WHERE verdict='neutral')::int nt, COALESCE(SUM(delta),0)::float8 sd, COALESCE(SUM(delta/NULLIF(atr,0)),0)::float8 sr FROM hcm_ai.reversal_attribution WHERE closed_ts>=$1 AND closed_ts<$2"
_Q3 = "SELECT floor(score*10)::int b, COUNT(*)::int n, COUNT(*) FILTER (WHERE is_reversal)::int rc, ROUND(COUNT(*) FILTER (WHERE is_reversal)::numeric/NULLIF(COUNT(*),0),4) rr, ROUND(COUNT(*) FILTER (WHERE verdict='saved')::numeric/NULLIF(COUNT(*),0),4) vrs FROM hcm_ai.reversal_attribution WHERE closed_ts>=$1 AND closed_ts<$2 AND mode IN ('log','log_skip') AND score IS NOT NULL GROUP BY 1 ORDER BY 1"
_Q4 = "SELECT CASE WHEN is_reversal THEN 'rev' ELSE 'pull' END vd, CASE WHEN mode='act' THEN 'act' ELSE 'shadow' END md, CASE WHEN EXTRACT(HOUR FROM adj_ts AT TIME ZONE 'UTC')<8 THEN 'asia' WHEN EXTRACT(HOUR FROM adj_ts AT TIME ZONE 'UTC')<13 THEN 'europe' WHEN EXTRACT(HOUR FROM adj_ts AT TIME ZONE 'UTC')<22 THEN 'us' ELSE 'asia' END ss, CASE WHEN dd_atr<1.1 THEN 'd1' WHEN dd_atr<1.6 THEN 'd2' ELSE 'd3' END dd, COUNT(*)::int n, COALESCE(SUM(delta/NULLIF(atr,0)),0)::float8 sr, COUNT(*) FILTER (WHERE verdict='saved')::int sv FROM hcm_ai.reversal_attribution WHERE closed_ts>=$1 AND closed_ts<$2 GROUP BY 1,2,3,4"
_Q5 = "SELECT ticket, symbol, direction, ROUND(dd_atr::numeric,2) dd_atr, ROUND(score::numeric,4) score, verdict, ROUND(old_sl::numeric,2) old_sl, ROUND(new_sl::numeric,2) new_sl, ROUND(realized_pnl::numeric,2) realized, ROUND(cf_pnl::numeric,2) cf, ROUND(delta::numeric,2) delta, ROUND((delta/NULLIF(atr,0))::numeric,4) dr FROM hcm_ai.reversal_attribution WHERE closed_ts>=$1 AND closed_ts<$2 AND verdict IS NOT NULL ORDER BY closed_ts DESC LIMIT 20"
_UPS = "INSERT INTO hcm_ai.rev_daily_report (stat_date, n_trigger_pos, n_requests, n_scored, n_rev_call, n_pull_call, n_act, n_shadow, n_settled, n_saved, n_killed, n_neutral, sum_delta, sum_delta_r, avg_delta_r, detail, updated_at) VALUES ($1,0,0,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,now()) ON CONFLICT (stat_date) DO UPDATE SET n_trigger_pos=0, n_requests=0, n_scored=$2, n_rev_call=$3, n_pull_call=$4, n_act=$5, n_shadow=$6, n_settled=$7, n_saved=$8, n_killed=$9, n_neutral=$10, sum_delta=$11, sum_delta_r=$12, avg_delta_r=$13, detail=$14::jsonb, updated_at=now()"


async def aggregate_rev_daily(db, trade_date: date) -> None:
    """聚合单日反转头绩效。口径：漏斗/动作按 adj_ts 归日、结算按 closed_ts 归日；
    主指标 sum_delta_r=Σ(delta/atr)；分箱仅取 shadow 行；时段对齐 _current_session_utc；
    n_trigger_pos/n_requests 暂无埋点源恒 0。"""
    ds = datetime.combine(trade_date, datetime.min.time())
    de = ds + timedelta(days=1)
    try:
        fn = await db.fetchrow(_Q1, ds, de)
        st = await db.fetchrow(_Q2, ds, de)
        bk = await db.fetch(_Q3, ds, de)
        sl = await db.fetch(_Q4, ds, de)
        r20 = await db.fetch(_Q5, ds, de)
        ns = int(st["n"] or 0)
        nsd, nkl, nnt = int(st["sv"] or 0), int(st["kl"] or 0), int(st["nt"] or 0)
        sdr = float(st["sr"] or 0)
        detail = {
            "buckets": [{"bucket": f"{r['b']/10:.1f}-{(r['b']+1)/10:.1f}", "n": int(r["n"]),
                         "n_rev_call": int(r["rc"]), "rev_rate": float(r["rr"] or 0),
                         "saved_rate": float(r["vrs"] or 0)} for r in (bk or [])],
            "slices": [{"verdict": r["vd"], "mode": r["md"], "session": r["ss"], "dd": r["dd"],
                        "n": int(r["n"]), "sum_delta_r": float(r["sr"] or 0),
                        "n_saved": int(r["sv"] or 0)} for r in (sl or [])],
            "recent": [{"ticket": int(r["ticket"]), "symbol": r["symbol"],
                        "direction": r["direction"], "dd_atr": float(r["dd_atr"] or 0),
                        "score": float(r["score"] or 0), "verdict": r["verdict"],
                        "old_sl": float(r["old_sl"] or 0), "new_sl": float(r["new_sl"] or 0),
                        "realized": float(r["realized"] or 0), "cf": float(r["cf"] or 0),
                        "delta": float(r["delta"] or 0), "delta_r": float(r["dr"] or 0)}
                       for r in (r20 or [])],
            "note": "n_trigger_pos/n_requests 暂无埋点源恒 0；delta 价格差口径未乘手数",
        }
        await db.execute(_UPS, trade_date, int(fn["n"] or 0), int(fn["rc"] or 0),
                         int(fn["pc"] or 0), int(fn["act"] or 0), int(fn["sh"] or 0),
                         ns, nsd, nkl, nnt, float(st["sd"] or 0), sdr,
                         round(sdr / ns, 4) if ns else None,
                         json.dumps(detail, ensure_ascii=False))
        logger.info("REV daily %s: scored=%d act=%d shadow=%d settled=%d saved=%d killed=%d sumR=%.3f",
                    trade_date, int(fn["n"] or 0), int(fn["act"] or 0), int(fn["sh"] or 0),
                    ns, nsd, nkl, sdr)
    except Exception as exc:
        logger.warning("Aggregate REV daily failed for %s: %s", trade_date, exc)
