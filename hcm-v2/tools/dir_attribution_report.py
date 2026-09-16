#!/usr/bin/env python3
"""方向归因报表（D 项·验证闭环）—— 回答「上涨趋势里为什么总发 SELL / 亏在哪」。

用途：把"该不该拦逆势单"从直觉变成**可复跑数据**。任何策略/配置改动前后各跑一次对比。

口径（全部只读，绝不写业务表）：
  A) 成交信号 方向 × H1趋势 分布（近 N 日）
  B) 实盘平仓单 方向 × 是否逆势 × 盈亏（join signals.h1_trend_direction）
  C) 按平仓原因（sl/expert/manual/...）分解盈亏 —— 定位"止损驱动"还是"入场驱动"
  D) 逆势/顺势 单均盈亏对比 —— 直接给出"拦掉逆势单"的期望收益
  E) 趋势治理影子观测统计（indicator_values._hexp.trend_governance）

用法：
  python dir_attribution_report.py                 # 默认近 7 日
  python dir_attribution_report.py --days 14
  python dir_attribution_report.py --json          # 机器可读（供计划任务/面板）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg2

DB_URL = os.getenv("DB_URL", "postgresql://hcm:hcm_dev_pwd@127.0.0.1:5432/hcm_v2")

# ── A) 信号：方向 × H1 趋势 ──────────────────────────────────────────
A_Q = """
SELECT signal_dir,
       COALESCE(indicator_values->>'h1_trend_direction','<none>') AS h1dir,
       count(*) AS n
FROM hcm_signal.signals
WHERE created_at >= now() - %(win)s::interval
  AND signal_dir IN ('BUY','SELL')
GROUP BY 1,2 ORDER BY 1,3 DESC
"""

# ── B) 平仓单：方向 × 是否逆势 × 盈亏 ────────────────────────────────
B_Q = """
SELECT o.direction,
       CASE WHEN s.indicator_values->>'h1_trend_direction' = 'UP'   THEN 'counter(H1=UP)'
            WHEN s.indicator_values->>'h1_trend_direction' = 'DOWN' THEN 'with(H1=DOWN)'
            ELSE 'unknown' END AS h1rel,
       count(*) AS n,
       count(*) FILTER (WHERE o.profit > 0) AS win,
       ROUND(sum(o.profit)::numeric,2) AS sum_p,
       ROUND(avg(o.profit)::numeric,2) AS avg_p
FROM hcm_trading.orders o
LEFT JOIN hcm_signal.signals s ON s.signal_id = o.signal_id
WHERE o.close_time >= now() - %(win)s::interval
  AND o.direction IN ('BUY','SELL')
GROUP BY 1,2 ORDER BY 1,2
"""

# ── C) 平仓原因 × 方向 → 盈亏 ────────────────────────────────────────
C_Q = """
SELECT COALESCE(close_reason,'<null>') AS reason, direction,
       count(*) AS n, ROUND(sum(profit)::numeric,2) AS sum_p
FROM hcm_trading.orders
WHERE close_time >= now() - %(win)s::interval
GROUP BY 1,2 ORDER BY 4 ASC NULLS LAST LIMIT 15
"""

# ── D) 逆势 vs 顺势（仅 SELL/BUY 各自）────────────────────────────────
D_Q = """
SELECT o.direction,
       CASE WHEN (o.direction='SELL' AND s.indicator_values->>'h1_trend_direction'='UP')
              OR (o.direction='BUY'  AND s.indicator_values->>'h1_trend_direction'='DOWN')
            THEN 'counter' ELSE 'with' END AS rel,
       count(*) AS n, count(*) FILTER (WHERE o.profit>0) AS win,
       ROUND(sum(o.profit)::numeric,2) AS sum_p, ROUND(avg(o.profit)::numeric,2) AS avg_p
FROM hcm_trading.orders o
JOIN hcm_signal.signals s ON s.signal_id = o.signal_id
WHERE o.close_time >= now() - %(win)s::interval AND o.direction IN ('BUY','SELL')
  AND s.indicator_values->>'h1_trend_direction' IN ('UP','DOWN')
GROUP BY 1,2 ORDER BY 1,2
"""

# ── E) 趋势治理影子观测（B/C 生效度）──────────────────────────────────
E_Q = """
SELECT COALESCE(indicator_values->'_hexp'->'trend_governance'->>'mode','<none>') AS mode,
       COALESCE(indicator_values->'_hexp'->'trend_governance'->>'confirmed_trend','<none>') AS conf,
       COALESCE(indicator_values->'_hexp'->'trend_governance'->>'would_block','<none>') AS would_block,
       signal_dir, count(*) AS n
FROM hcm_signal.signals
WHERE created_at >= now() - %(win)s::interval
  AND indicator_values->'_hexp'->'trend_governance' IS NOT NULL
GROUP BY 1,2,3,4 ORDER BY 5 DESC LIMIT 20
"""


def _run(cur, q, win):
    cur.execute(q, {"win": win})
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db-url", default=DB_URL)
    ap.add_argument("--json", action="store_true", help="输出 JSON（供计划任务）")
    args = ap.parse_args()
    win = f"{int(args.days)} days"

    conn = psycopg2.connect(args.db_url, connect_timeout=8)
    try:
        cur = conn.cursor()
        a = _run(cur, A_Q, win)
        b = _run(cur, B_Q, win)
        c = _run(cur, C_Q, win)
        d = _run(cur, D_Q, win)
        e = _run(cur, E_Q, win)
    finally:
        conn.close()

    if args.json:
        print(json.dumps({
            "window_days": args.days,
            "signals_by_dir_h1": a, "closed_by_dir_h1": b,
            "closed_by_reason": c, "counter_vs_with": d, "trend_governance": e,
        }, ensure_ascii=False, default=str))
        return 0

    print(f"\n{'='*78}\n方向归因报表  窗口=近 {args.days} 日\n{'='*78}")

    print("\n[A] 成交信号：方向 × H1 趋势")
    print(f"    {'dir':<6}{'h1_trend':<10}{'n':>6}")
    for dir_, h1, n in a:
        print(f"    {dir_:<6}{h1 or '<none>':<10}{n:>6}")

    print("\n[B] 平仓单：方向 × 与 H1 关系 × 盈亏")
    print(f"    {'dir':<6}{'relation':<16}{'n':>5}{'win':>6}{'sum':>11}{'avg':>9}")
    for dir_, rel, n, win_, sum_p, avg_p in b:
        print(f"    {dir_:<6}{rel:<16}{n:>5}{win_:>6}{float(sum_p or 0):>11.2f}{float(avg_p or 0):>9.2f}")

    print("\n[C] 按平仓原因分解（升序=亏损最大在前）")
    print(f"    {'reason':<20}{'dir':<6}{'n':>5}{'sum':>12}")
    for reason, dir_, n, sum_p in c:
        print(f"    {reason:<20}{dir_:<6}{n:>5}{float(sum_p or 0):>12.2f}")

    print("\n[D] 逆势 vs 顺势（单均盈亏 → 直接反映「拦掉逆势单」的期望收益）")
    print(f"    {'dir':<6}{'rel':<9}{'n':>5}{'win':>6}{'sum':>11}{'avg':>9}")
    for dir_, rel, n, win_, sum_p, avg_p in d:
        print(f"    {dir_:<6}{rel:<9}{n:>5}{win_:>6}{float(sum_p or 0):>11.2f}{float(avg_p or 0):>9.2f}")

    print("\n[E] 趋势治理影子观测（B/C）")
    if not e:
        print("    （窗口内无 trend_governance 观测：引擎未重启加载新代码，或未产出 HEXP 信号）")
    else:
        print(f"    {'mode':<8}{'confirmed':<11}{'would_block':<13}{'dir':<6}{'n':>6}")
        for mode, conf, wb, dir_, n in e:
            print(f"    {str(mode):<8}{str(conf):<11}{str(wb):<13}{str(dir_):<6}{n:>6}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
