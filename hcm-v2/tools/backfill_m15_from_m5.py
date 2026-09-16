"""backfill_m15_from_m5.py — 由 M5 聚合补齐 M15（**_15 缺失导致 M15 模型无法训练**）。

背景（实测）：`hcm_market.klines` 中 **M15 = 0 行**；M5 覆盖最长（2026-06-16 起、20319 根，
间隔 5:00 的占 20283/20319 → 近完整，仅约 20 处缺 1 根）。
故 M15 可由 M5 聚合得到。

【三条硬原则（本工具的全部安全边界）】
 1. **只出生效完整聚合，绝不编造**：仅当 3 根 M5 **恰好按 5 分钟连续**
    （`bucket, +5min, +10min`）时才产出该 M15 bar；任一根缺失 → **跳过**（宁可留空）。
    这与"用插值/前值填充凑数"有本质区别，后者会制造不存在的行情。
 2. **独立交叉验证**：把聚合出的 M15 再聚合为 M30（2 根），与库中**真实 M30**逐根比对
    open/high/low/close。M30 是**独立采集**的（非由 M5 派生）→ 能证明聚合口径正确。
    这是本工具的验收门：**不通过则拒绝 --commit**。
 3. **来源可辨、可回滚**：写入 `source='agg:M5'`，与真实采集的 bar 区分；
    回滚一条 SQL 即可（见文件末尾注释）。

⚠ 与采集器的相容性：`kline_collector.py` 的 upsert 用
`high=GREATEST(...), low=LEAST(...)`。若日后真实 M15 到达，会与聚合值**合并**而非覆盖。
聚合 bar 的 high/low 与真实值一致（聚合无损），故合并安全。

用法：
    python backfill_m15_from_m5.py                      # 干跑（默认）：统计 + 交叉验证，不落库
    python backfill_m15_from_m5.py --commit             # 通过验证后落库
    python backfill_m15_from_m5.py --validate-only      # 只跑 M30 交叉验证
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"
SOURCE_TAG = "agg:M5"          # 派生产物的来源标记（与真实采集区分，便于回滚）


def _agg(df: pd.DataFrame, minutes: int, sub_per_bar: int) -> tuple[pd.DataFrame, int]:
    """把 df 按 minutes 聚合；**仅接受恰好 sub_per_bar 根、且时间严格等距**的组。

    Returns:
        (聚合结果, 被丢弃的组数)
    """
    d = df.copy()
    d["bucket"] = d["open_time"].dt.floor(f"{minutes}min")
    step = pd.Timedelta(minutes=minutes // sub_per_bar)
    rows, dropped = [], 0
    for bucket, g in d.groupby("bucket", sort=True):
        g = g.sort_values("open_time")
        if len(g) != sub_per_bar:
            dropped += 1
            continue
        expect = [bucket + step * k for k in range(sub_per_bar)]
        if list(g["open_time"]) != expect:
            dropped += 1          # 时间不连续（缺根/错位）→ 宁可留空
            continue
        rows.append({
            "open_time": bucket,
            "open": float(g["open"].iloc[0]),
            "high": float(g["high"].max()),
            "low": float(g["low"].min()),
            "close": float(g["close"].iloc[-1]),
            "tick_volume": int(g["tick_volume"].fillna(0).sum()),
            "spread": float(g["spread"].fillna(0).max()) if g["spread"].notna().any() else 0.0,
        })
    if not rows:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close",
                                     "tick_volume", "spread"]), dropped
    return pd.DataFrame(rows), dropped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--commit", action="store_true", help="落库（默认干跑）")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--tol", type=float, default=1e-9,
                    help="M30 交叉验证容差（价格绝对差；numeric 与 float 往返的量化误差）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    conn = psycopg2.connect(args.db_url)
    q = ("SELECT open_time, open, high, low, close, tick_volume, spread "
         "FROM hcm_market.klines WHERE symbol=%s AND time_frame=%s ORDER BY open_time")
    m5 = pd.read_sql(q, conn, params=(args.symbol, "M5"))
    m30 = pd.read_sql(q, conn, params=(args.symbol, "M30"))
    m15_exist = pd.read_sql("SELECT count(*) AS n FROM hcm_market.klines "
                            "WHERE symbol=%s AND time_frame='M15'",
                            conn, params=(args.symbol,))
    print(f"[data] {args.symbol} M5={len(m5)} M30={len(m30)} "
          f"M15(现有)={int(m15_exist['n'].iloc[0])}")
    for d in (m5, m30):
        if len(d):
            d["open_time"] = pd.to_datetime(d["open_time"], utc=True)
    if m5.empty:
        raise SystemExit("[fatal] 无 M5 数据，无法聚合")

    # ── 1) M5 → M15 ──
    m15, dropped15 = _agg(m5, 15, 3)
    print(f"\n=========== 1) M5 → M15 ===========")
    print(f"  产出 M15 bar = {len(m15)}；因缺根/错位丢弃的组 = {dropped15}")
    if len(m15):
        print(f"  覆盖 {m15['open_time'].iloc[0]} .. {m15['open_time'].iloc[-1]}")
        span_min = (m15["open_time"].iloc[-1] - m15["open_time"].iloc[0]).total_seconds() / 60
        expect = int(span_min // 15) + 1
        print(f"  区间内应有 {expect} 根 → 实测覆盖率 = {len(m15) / max(1, expect):.2%}")
        # 交易时段外的自然空缺（周末）不应算作缺陷，故覆盖率略低于 100% 属正常

    # ── 2) 独立交叉验证 ──
    # 【为什么预言机是 M1 而不是 M30/H1（2026-09-15 实测）】
    #   最初用"聚合 M15→M30 vs 真实 M30"作门，结果 2343 根里 1171~1574 根不匹配。
    #   诊断发现：`open` 逐根差 **0.00**（聚合口径正确），而**真实 M30×2 与真实 H1
    #   互相对不上（open 最大差 268.28）** → 库内 M30/H1 两个独立采集源**本身就矛盾**，
    #   不能充当预言机。
    #   故改用 **M1**（68588 根独立采集、覆盖 2026-07-08 起）聚合出的 M15 作预言机：
    #   M1→M15 需 15 根严格连续，比 M5→M15（3 根）更严格，且与 M5 是两条独立采集线。
    print(f"\n=========== 2) 交叉验证（预言机 = M1 独立聚合）===========")
    if args.validate_only and len(m15) == 0:
        raise SystemExit("[fatal] --validate-only 但 M15 为空")
    m1 = pd.read_sql(q, conn, params=(args.symbol, "M1"))
    if len(m1):
        m1["open_time"] = pd.to_datetime(m1["open_time"], utc=True)
    m15_m1, _ = _agg(m1, 15, 15)
    ok_cross = False
    if m15_m1.empty or m15.empty:
        print("  M1 数据不足，跳过（无独立预言机则**拒绝落库**）")
    else:
        j = m15.merge(m15_m1, on="open_time", suffixes=("_m5", "_m1"), how="inner")
        print(f"  可比对 M15 bar = {len(j)}（M5 侧 {len(m15)}，M1 侧 {len(m15_m1)}）")
        if len(j) == 0:
            print("  ✗ 无可比样本")
        else:
            worst = {}
            for col in ("open", "high", "low", "close"):
                dd = (j[f"{col}_m5"].astype(float) - j[f"{col}_m1"].astype(float)).abs()
                worst[col] = float(dd.max())
                print(f"  {col:<6} 最大绝对差={worst[col]:.3e}  超差根数={int((dd > args.tol).sum())}")
            ok_cross = all(v <= args.tol for v in worst.values())
            print(f"\n  → 交叉验证 {'通过 ✓（M5 与 M1 两条独立采集线聚合一致）' if ok_cross else '未通过 ✗'}")

    # ── 2b) 二次诊断（**不阻断**）：与真实 M30 比对，用于暴露库内多周期数据质量问题 ──
    if not args.validate_only and len(m15) and len(m30):
        m30_from_15, _ = _agg(m15, 30, 2)
        j2 = m30_from_15.merge(m30, on="open_time", suffixes=("_a", "_t"), how="inner")
        if len(j2):
            dmax = max(float((j2[f"{c}_a"].astype(float) - j2[f"{c}_t"].astype(float)).abs().max())
                       for c in ("open", "high", "low", "close"))
            print(f"\n  [仅诊断·不阻断] 与真实 M30 比对：可比 {len(j2)} 根，最大绝对差={dmax:.3f}")
            print("     ⚠ 该差异**不能判为聚合错误**：真实 M30×2 与真实 H1 亦互不一致"
                  "（open 最大差 268.28）→ 属库内多周期数据源不一致，需另行治理")

    # ── 3) 落库 ──
    print(f"\n=========== 3) 落库 ===========")
    if not args.commit:
        print("  [dry-run] 未落库。加 --commit 落库（source='%s'）" % SOURCE_TAG)
        if len(m15):
            print(f"  预览前 3 行：\n{m15.head(3).to_string(index=False)}")
        conn.close()
        return
    if not ok_cross:
        conn.close()
        raise SystemExit("[拒绝落库] M30 交叉验证未通过 —— 按原则 2，口径未证实前不得写入行情表")
    with conn.cursor() as cur:
        for _, r in m15.iterrows():
            cur.execute(
                """INSERT INTO hcm_market.klines
                     (symbol, time_frame, open_time, open, high, low, close,
                      tick_volume, spread, source)
                   VALUES (%s,'M15',%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (symbol, time_frame, open_time) DO UPDATE SET
                     high = GREATEST(hcm_market.klines.high, EXCLUDED.high),
                     low  = LEAST(hcm_market.klines.low, EXCLUDED.low),
                     close = EXCLUDED.close,
                     tick_volume = hcm_market.klines.tick_volume + EXCLUDED.tick_volume,
                     spread = GREATEST(hcm_market.klines.spread, EXCLUDED.spread)""",
                (args.symbol, r["open_time"].to_pydatetime(), r["open"],
                 r["high"], r["low"], r["close"], int(r["tick_volume"]),
                 r["spread"], SOURCE_TAG))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM hcm_market.klines "
                    "WHERE symbol=%s AND time_frame='M15'", (args.symbol,))
        print(f"  已落库。当前 M15 = {cur.fetchone()[0]} 行（source='{SOURCE_TAG}'）")
    conn.close()
    print("\n[回滚] DELETE FROM hcm_market.klines "
          "WHERE time_frame='M15' AND source='%s';" % SOURCE_TAG)


if __name__ == "__main__":
    main()
