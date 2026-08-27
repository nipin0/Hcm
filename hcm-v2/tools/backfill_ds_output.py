#!/usr/bin/env python
# backfill_ds_output.py — 历史回填 hcm_ai.ds_output（训练侧 DeepSeek 特征补全）
#
# 目的：为「无 DeepSeek 邻票」的 HEXP 历史信号补齐 ds_output 行，使训练侧
#       quality_features._nearest_ds 能在 ±1800s 命中 → ds_nonzero_ratio→~1.0，
#       训练样本数突破 MIN_SAMPLES_FOR_SWITCH=200，模型真正吸收 DeepSeek 语义。
#
# 设计要点（与系统同构，安全）：
#   - build_prompt / parse_output 从 signal_tower/ai_async_client.py 逐字复制，
#     提示词与实时 DeepSeek 异步刷新完全一致 → 输出分布同域。
#   - 历史 HEXP 快照可从 signals.indicator_values._hexp 近乎 1:1 重建
#     （hp_score/k/grade/verdict/factor_raws/factor_scores/period_states/trend_scores 均存）。
#   - 仅写 hcm_ai.ds_output，created_at = 信号原始时间（使 _nearest_ds 命中）；
#     不写 runtime_event（避免污染实时成功率报表）；实时融合只读 Redis，不受影响。
#   - 幂等可重跑：仅处理「当前无 DS 邻票」的信号；逐条插入前再校验。
#   - 限流：默认 sleep 3s（≈20/min）；遇 429 退避 60s 重试。
#
# 红线：本脚本会写生产 PG hcm_ai.ds_output。仅在你明确授权范围内运行。
import os
import sys
import json
import re
import time
import argparse
import logging

import psycopg2

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ds_backfill")

DB_DSN = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"

# ───────────────────────── 从 ai_async_client.py 逐字复制 ─────────────────────────
FALLBACK = {"fake_prob": None, "ai_sl_coeff": 1.0, "continuity_score": 50}
OUTPUT_KEYS = ("fake_prob", "ai_sl_coeff", "continuity_score")


def _num(cfg, key, default):
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def build_prompt(snapshot, symbol=""):
    fr = snapshot.get("factor_raws") or {}
    sc = snapshot.get("scorecard") or {}
    fc = snapshot.get("factor_scores") or {}

    def _conflict_count():
        cnt = 0
        pos = 0
        for v in fc.values():
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0.02:
                pos += 1
            elif f < -0.02:
                cnt += 1
        return min(pos, cnt)

    ext = snapshot.get("external") or {}
    if isinstance(ext, dict):
        _evt = ext.get("event")
        _comp = ext.get("composite")
        _liq = ext.get("liquidity")
        _macro = ext.get("macro")
        _sent = ext.get("sentiment")
        _stub = ext.get("stub_mode")
        _ext_lines = [
            f"外部因子(composite={_comp} macro={_macro} sentiment={_sent} "
            f"event={_evt} liquidity={_liq})",
            f"外部因子采集状态={'STUB(占位/未采集)' if _stub else '真实'}",
            "解读：event 越高=重大财经事件窗口越近(信号风险越高)；"
            "liquidity 越低=流动性越枯竭(滑点/假突破风险越高)；"
            "composite 越高=外部综合风险越高。",
        ]
    else:
        _ext_lines = ["外部因子=未知(采集器未就绪)"]

    _lmf = snapshot.get("lm_features") or {}
    if isinstance(_lmf, dict) and _lmf:
        _lm_lines = [
            "LightGBM特征(与本地AI评分模型同构，25维，单位已对齐)：",
            f"  adx_14={_lmf.get('adx_14')} rsi_14={_lmf.get('rsi_14')} "
            f"macd={_lmf.get('macd')} atr_14={_lmf.get('atr_14')}",
            f"  h1_adx={_lmf.get('h1_adx')} h1_trend_strength={_lmf.get('h1_trend_strength')}",
            f"  plus_di={_lmf.get('plus_di')} minus_di={_lmf.get('minus_di')} "
            f"er={_lmf.get('er')} bbw={_lmf.get('bbw')} bbw_pct={_lmf.get('bbw_pct')}",
            f"  hurst={_lmf.get('hurst')} mm={_lmf.get('mm')} "
            f"ema20_dist_atr={_lmf.get('ema20_dist_atr')} body_ratio={_lmf.get('body_ratio')}",
            f"  pullback_depth={_lmf.get('pullback_depth')} atr_pct={_lmf.get('atr_pct')} "
            f"spread_num={_lmf.get('spread_num')} spread_atr={_lmf.get('spread_atr')}",
            f"  session(asia/eu/us)={_lmf.get('session_asia')}/{_lmf.get('session_eu')}/{_lmf.get('session_us')}",
            f"  event_proximity_min={_lmf.get('event_proximity_min')} "
            f"macro_risk_score={_lmf.get('macro_risk_score')} "
            f"sentiment_risk_score={_lmf.get('sentiment_risk_score')}",
            "说明：以上 25 维与本地 LightGBM 质量分(ai_score)所用特征完全一致；"
            "其中 event_proximity_min 越小=重大事件越近，macro/sentiment_risk_score 越大=风险越高，"
            "与上方'外部因子'段的0~1分同义但尺度不同，请综合判断。",
        ]
    else:
        _lm_lines = ["LightGBM特征=未知(sidecar未发布，仅作HEXP文本特征裁判)"]

    _grade = snapshot.get("grade")
    _pstates = snapshot.get("period_states") or {}
    _h1_state = _pstates.get("H1") if isinstance(_pstates, dict) else None
    _h1_confirm = None
    if _h1_state in ("TREND_UP", "TREND_DOWN"):
        _h1_confirm = _h1_state

    lines = [
        "你是黄金 XAUUSD 信号质量裁判。基于以下和乘幂(HEXP)信号特征，输出 JSON 三参数。",
        f"symbol={symbol or 'XAUUSD'}",
        f"hp_score={snapshot.get('hp_score')} hp_abs={abs(float(snapshot.get('hp_score') or 0))}",
        f"direction={snapshot.get('direction')} k={snapshot.get('k')} grade={_grade}",
        f"H1趋势确认态={_h1_confirm} (TREND_UP=已确认看多/TREND_DOWN=已确认看空/None=未确认)",
        f"行情模式(period_states)={snapshot.get('period_states')}",
        f"因子原始值={fr}",
        f"因子冲突数量={_conflict_count()}",
        f"六维评分卡={sc} 六维总分={snapshot.get('scorecard_total')}",
        f"ATR={snapshot.get('atr')} M1-MM={snapshot.get('mm')} verdict={snapshot.get('verdict')}",
        *_ext_lines,
        *_lm_lines,
        "输出仅一个 JSON 对象，键固定为：",
        '{"fake_prob": <0~1 该HEXP信号为真的概率>, '
        '"ai_sl_coeff": <0.8~1.5 自适应止损ATR倍数>, "continuity_score": <0~100 趋势延续分>}',
    ]
    return "\n".join(lines)


def parse_json_block(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def parse_output(text, cfg):
    data = parse_json_block(text) or {}
    out = dict(FALLBACK)
    if not isinstance(data, dict):
        return out
    if "fake_prob" in data:
        try:
            out["fake_prob"] = float(max(0.0, min(1.0, data["fake_prob"])))
        except (TypeError, ValueError):
            pass
    if "ai_sl_coeff" in data:
        lo = _num(cfg, "ai.ds.sl_coeff_min", 0.8)
        hi = _num(cfg, "ai.ds.sl_coeff_max", 1.5)
        try:
            out["ai_sl_coeff"] = float(max(lo, min(hi, data["ai_sl_coeff"])))
        except (TypeError, ValueError):
            out["ai_sl_coeff"] = 1.0
    if "continuity_score" in data:
        try:
            out["continuity_score"] = int(max(0, min(100, data["continuity_score"])))
        except (TypeError, ValueError):
            pass
    return out


# ───────────────────────── 配置与数据访问 ─────────────────────────
def load_ds_cfg():
    cfg = {"api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
           "api_base": os.environ.get("DEEPSEEK_API_BASE", ""),
           "model": os.environ.get("DEEPSEEK_MODEL", "")}
    try:
        cc = psycopg2.connect(DB_DSN, connect_timeout=5)
        try:
            with cc.cursor() as cur:
                cur.execute(
                    "SELECT config_key, current_value FROM hcm_config.metadata "
                    "WHERE config_key IN ('deepseek.api_key','deepseek.api_base','deepseek.model')"
                )
                for k, v in cur.fetchall():
                    if k == "deepseek.api_key" and not cfg["api_key"]:
                        cfg["api_key"] = v or ""
                    elif k == "deepseek.api_base" and not cfg["api_base"]:
                        base = (v or "").rstrip("/")
                        if base and not base.endswith("/chat/completions"):
                            if base.endswith("/v1"):
                                base += "/chat/completions"
                            elif "/v1/" not in base:
                                base += "/v1/chat/completions"
                        cfg["api_base"] = base
                    elif k == "deepseek.model" and not cfg["model"]:
                        cfg["model"] = v or ""
        finally:
            cc.close()
    except Exception as e:
        log.warning("PG ds cfg read failed (fall back to env/default): %s", e)
    if not cfg["api_key"]:
        cfg["api_key"] = os.environ.get("DEEPSEEK_API_KEY", "")
    if not cfg["api_base"]:
        cfg["api_base"] = os.environ.get(
            "DEEPSEEK_API_BASE", "https://api.deepseek.com/v1/chat/completions")
    if not cfg["model"]:
        cfg["model"] = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    return cfg


def build_snapshot(signal_dir, indicator_values):
    ind = indicator_values
    if isinstance(ind, str):
        ind = json.loads(ind)
    _hexp = (ind.get("_hexp") or {}) if isinstance(ind, dict) else {}
    ts = _hexp.get("trend_scores") or {}
    sc_total = None
    if isinstance(ts, dict) and ts:
        try:
            sc_total = round(sum(float(x) for x in ts.values()) / len(ts), 2)
        except Exception:
            sc_total = None
    return {
        "hp_score": _hexp.get("hp_score"),
        "direction": signal_dir,
        "k": _hexp.get("k"),
        "grade": _hexp.get("grade"),
        "period_states": _hexp.get("period_states"),
        "verdict": _hexp.get("verdict"),
        "factor_raws": _hexp.get("factor_raws"),
        "factor_scores": _hexp.get("factor_scores"),
        "scorecard": ts,
        "scorecard_total": sc_total,
        "atr": ind.get("atr_14") if isinstance(ind, dict) else None,
        "mm": _hexp.get("mm"),
        "external": None,
        "lm_features": None,
    }


def call_deepseek(api_base, api_key, model, prompt, timeout=30):
    import requests
    if not api_key:
        return None, "no_key"
    try:
        resp = requests.post(
            api_base,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": [
                {"role": "system", "content": "你是量化信号质量裁判，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ], "temperature": 0.0, "max_tokens": 300},
            timeout=timeout,
        )
        if resp.status_code == 429:
            return None, "ratelimit"
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return content, "ok"
    except Exception as e:
        return None, f"err:{e}"


def has_ds_neighbor(conn, created_at, symbol, window=1800):
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM hcm_ai.ds_output d WHERE d.symbol=%s "
        "AND abs(extract(epoch from (d.created_at - %s::timestamptz)))<=%s LIMIT 1",
        (symbol, created_at, window),
    )
    r = cur.fetchone()
    cur.close()
    return r is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", required=True,
                    help="逗号分隔 YYYY-MM-DD 列表，回填这些天的缺口信号")
    ap.add_argument("--dry", action="store_true",
                    help="仅导出计划(回滚锚)不写库")
    ap.add_argument("--sleep", type=float, default=3.0,
                    help="每次调用间隔秒(默认3≈20/min，避免限流)")
    args = ap.parse_args()
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    conn = psycopg2.connect(DB_DSN)
    ds_cfg = load_ds_cfg()
    log.info("ds key_present=%s base=%s model=%s",
             bool(ds_cfg["api_key"]), ds_cfg["api_base"][:24], ds_cfg["model"])

    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.signal_id, s.symbol, s.signal_dir, s.created_at, s.indicator_values
        FROM hcm_signal.signals s
        WHERE s.signal_mode LIKE 'HEXP%%'
          AND s.signal_dir IN ('BUY','SELL') AND s.entry_price > 0
          AND date_trunc('day', s.created_at)::date = ANY(%s::date[])
          AND NOT EXISTS (
            SELECT 1 FROM hcm_ai.ds_output d
            WHERE d.symbol = s.symbol
              AND abs(extract(epoch from (s.created_at - d.created_at))) <= 1800
          )
        ORDER BY s.created_at
        """,
        (dates,),
    )
    rows = cur.fetchall()
    cur.close()
    log.info("gap signals to backfill: %d", len(rows))

    plan_path = f"ds_backfill_plan_{int(time.time())}.json"
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(
            [{"signal_id": r[0], "symbol": r[1], "dir": r[2],
              "created_at": str(r[3])} for r in rows],
            f, ensure_ascii=False, indent=1,
        )
    log.info("plan exported -> %s", plan_path)

    if args.dry:
        log.info("DRY mode: no writes performed.")
        conn.close()
        return

    if not ds_cfg["api_key"]:
        log.error("no DeepSeek key available, abort.")
        conn.close()
        return

    result_path = f"ds_backfill_result_{int(time.time())}.jsonl"
    inserted = skipped = failed = 0
    with open(result_path, "w", encoding="utf-8") as rf:
        for r in rows:
            sig_id, symbol, direction, created_at, ind = r
            if has_ds_neighbor(conn, created_at, symbol):
                skipped += 1
                continue
            snap = build_snapshot(direction, ind)
            prompt = build_prompt(snap, symbol)
            content, status = call_deepseek(
                ds_cfg["api_base"], ds_cfg["api_key"], ds_cfg["model"], prompt)
            if status == "ratelimit":
                log.warning("429 ratelimit -> sleep 60s, retry once")
                time.sleep(60)
                content, status = call_deepseek(
                    ds_cfg["api_base"], ds_cfg["api_key"], ds_cfg["model"], prompt)
            if status != "ok" or content is None:
                failed += 1
                rf.write(json.dumps({"signal_id": sig_id, "status": status},
                                    ensure_ascii=False) + "\n")
                rf.flush()
                time.sleep(args.sleep)
                continue
            out = parse_output(content, ds_cfg)
            write_status = "ok" if out.get("fake_prob") is not None else "fail"
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO hcm_ai.ds_output
                   (symbol, fake_prob, ai_sl_coeff, continuity_score,
                    status, cached, detail, created_at)
                   VALUES (%s,%s,%s,%s,%s,FALSE,NULL,%s) RETURNING output_id""",
                (symbol, out["fake_prob"], out["ai_sl_coeff"],
                 out["continuity_score"], write_status, created_at),
            )
            oid = cur.fetchone()[0]
            conn.commit()
            cur.close()
            inserted += 1
            rf.write(json.dumps({"signal_id": sig_id, "output_id": oid,
                                 "status": write_status, "out": out,
                                 "created_at": str(created_at)},
                                ensure_ascii=False) + "\n")
            rf.flush()
            if inserted % 20 == 0:
                log.info("progress inserted=%d skipped=%d failed=%d",
                         inserted, skipped, failed)
            time.sleep(args.sleep)

    log.info("DONE inserted=%d skipped=%d failed=%d -> %s",
             inserted, skipped, failed, result_path)
    conn.close()


if __name__ == "__main__":
    main()
