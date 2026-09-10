#!/usr/bin/env python3
"""position_path_report.py — P0 基线报告（「贴合行情」专项：入场点 + 移动止盈）

输入：hcm_ai.position_path（桥侧每根 M5 落一条 MFE/MAE 路径，P0 采集）
      + hcm_trading.orders（实现盈亏、平仓归因 close_reason）
输出：判断"钱漏在哪"的六个问题
  1. MFE 捕获率   = 实现 R ÷ 同仓最大有利偏移 R —— 吃到多少该吃的行情
  2. 保本转化率   = 曾把 SL 推到保本及以上的单占比（保本门槛是否过早/过晚）
  3. 扫损单画像   = 被止损的单，其 MFE 曾到过多少（差一点就赢的比例）
  4. 止盈单画像   = 止盈单的 MFE 峰值与实现 R 之差（TP 是否过早截断趋势）
  5. 回撤容忍     = MAE_R 分布（止损距离是否给足呼吸空间）
  6. 分档         = 按方向/持仓时长/会话看上述指标差异

R 口径：R = |entry_price − init_sl_price|（init_sl 取 orders.sl，桥侧已用 orders 校正时区）
        无 SL 时回退 2×ATR（与 ai.lm.label_sl_atr_fallback 同口径，需 klines，暂略）

用法（容器内执行，桥侧数据经 PG 共享）：
    docker cp tools/position_path_report.py hcm-v2-hcm-signal-tower-1:/tmp/ppr.py
    docker exec hcm-v2-hcm-signal-tower-1 python /tmp/ppr.py
只读，无任何写操作。
"""

import asyncio

import numpy as np
import pandas as pd
import asyncpg

URL = "postgresql://hcm:hcm_dev_pwd@postgres:5432/hcm_v2"
SYMBOL = None          # None=全部品种；如需只看黄金填 "XAUUSD"
MIN_BARS = 1           # 过滤刚开仓（路径太短）的样本

# 每仓最后一条路径（含 mfe/mae 全窗口峰值）+ 订单实现盈亏
SQL_LAST = """
SELECT DISTINCT ON (p.ticket)
       p.ticket, p.symbol, p.direction, p.open_time, p.entry_price, p.init_sl_price,
       p.mfe_px, p.mae_px, p.bars_in_trade, p.is_closed, p.volume,
       o.profit AS realized, o.close_reason, o.close_time, o.sl AS order_sl, o.tp AS order_tp
FROM hcm_ai.position_path p
LEFT JOIN hcm_trading.orders o ON o.mt5_ticket = p.ticket
ORDER BY p.ticket, p.bar_time DESC
"""

# 保本转化：路径中是否出现过 SL 推到保本及以上
SQL_BE = """
SELECT p.ticket,
       MAX(CASE WHEN p.direction = 'BUY'  AND p.sl_price > 0 AND p.sl_price >= p.entry_price THEN 1
                WHEN p.direction = 'SELL' AND p.sl_price > 0 AND p.sl_price <= p.entry_price THEN 1
                ELSE 0 END) AS reached_be
FROM hcm_ai.position_path p
GROUP BY p.ticket
"""


def session_of(ts) -> str:
    """粗分会话（UTC）：亚 0-7 / 欧 7-12 / 美 12-21 / 尾盘 21-24。"""
    h = pd.Timestamp(ts).hour
    if 7 <= h < 12:
        return "EU"
    if 12 <= h < 21:
        return "US"
    if 21 <= h < 24:
        return "LATE"
    return "ASIA"


async def main() -> None:
    conn = await asyncpg.connect(URL)
    try:
        rows = await conn.fetch(SQL_LAST)
        be_rows = await conn.fetch(SQL_BE)
    finally:
        await conn.close()

    if not rows:
        print("hcm_ai.position_path 无样本 —— 桥侧采样未运行或无持仓。")
        return

    d = pd.DataFrame(rows, columns=[
        "ticket", "symbol", "direction", "open_time", "entry_price", "init_sl_price",
        "mfe_px", "mae_px", "bars_in_trade", "is_closed", "volume",
        "realized", "close_reason", "close_time", "order_sl", "order_tp"])
    be = pd.DataFrame(be_rows, columns=["ticket", "reached_be"])
    d = d.merge(be, on="ticket", how="left")

    if SYMBOL:
        d = d[d["symbol"] == SYMBOL]
    d = d[d["bars_in_trade"] >= MIN_BARS].copy()
    for c in ("entry_price", "init_sl_price", "mfe_px", "mae_px", "realized"):
        d[c] = pd.to_numeric(d[c], errors="coerce")

    # R 归一：init_sl 缺失时回退 order_sl
    d["init_sl_price"] = d["init_sl_price"].fillna(d["order_sl"])
    d["R"] = (d["entry_price"] - d["init_sl_price"]).abs()
    d = d[d["R"] > 1e-9].copy()
    d["mfe_R"] = d["mfe_px"] / d["R"]
    d["mae_R"] = d["mae_px"] / d["R"]
    d["realized_R"] = d["realized"] / d["R"]
    sign = np.where(d["direction"] == "BUY", 1.0, -1.0)
    # 价格口径 profit 已是账户货币且带方向；realized_R 直接用 profit/R（profit 已含方向）
    d["session"] = d["open_time"].map(session_of)

    closed = d[d["close_reason"].notna() & (d["is_closed"] == 1)]
    print("=" * 96)
    print(f"P0 基线报告  样本={len(d)} 仓（已平仓 {len(closed)}）  品种={sorted(d['symbol'].unique())}")
    print("=" * 96)

    def _stat(name: str, sub: pd.DataFrame) -> None:
        if sub.empty:
            print(f"   {name:<22} n=0")
            return
        rr = sub["realized_R"].dropna()
        mfe = sub["mfe_R"].dropna()
        mae = sub["mae_R"].dropna()
        capture = (rr.mean() / mfe.mean()) if (len(rr) and mfe.mean() > 0) else float("nan")
        print(f"   {name:<22} n={len(sub):4d} | 实现R均值={rr.mean():+.3f} | MFE_R均值={mfe.mean():.3f} "
              f"| MAE_R均值={mae.mean():.3f} | 捕获率={capture * 100:6.1f}% "
              f"| 保本转化={sub['reached_be'].fillna(0).mean() * 100:5.1f}%")

    print("\n[1] 总览")
    _stat("全部", d)
    _stat("已平仓", closed)

    print("\n[2] 按平仓归因（closed）")
    for reason in ("sl", "tp", "be", "manual", "sync_reconcile"):
        sub = closed[closed["close_reason"] == reason]
        if not sub.empty:
            _stat(reason, sub)

    sl = closed[closed["close_reason"] == "sl"]
    if not sl.empty:
        near = sl[sl["mfe_R"] >= 0.8]
        print(f"\n[3] 扫损单画像：n={len(sl)}，其中 MFE 曾到 ≥0.8R 的『差一点就赢』占比 "
              f"{len(near) / len(sl) * 100:.1f}%（均值 MFE_R={sl['mfe_R'].mean():.2f}，"
              f"MAE_R={sl['mae_R'].mean():.2f}）")

    tp = closed[closed["close_reason"] == "tp"]
    if not tp.empty:
        gap = (tp["mfe_R"] - tp["realized_R"]).dropna()
        print(f"[4] 止盈单画像：n={len(tp)}，实现R均值={tp['realized_R'].mean():+.2f}，"
              f"MFE_R均值={tp['mfe_R'].mean():.2f}，未吃到差值均值={gap.mean():.2f}R"
              f"（越大=TP 越早截断趋势）")

    print("\n[5] 分档")
    for col in ("direction", "session"):
        for v, sub in closed.groupby(col):
            if len(sub) >= 1:
                _stat(f"{col}={v}", sub)

    bins = [0, 12, 36, 96, 10 ** 6]
    labels = ["<1h", "1-3h", "3-8h", ">8h"]
    closed = closed.assign(hold_bucket=pd.cut(closed["bars_in_trade"], bins=bins, labels=labels))
    for v, sub in closed.groupby("hold_bucket", observed=True):
        if not sub.empty:
            _stat(f"hold={v}", sub)

    print("\n[6] 判读提示")
    print("   · 捕获率低(<40%) + 止盈单未吃到差值大 → 出场过早（TP/追踪太紧），P2 优先调 trail/分批")
    print("   · 扫损单『差一点就赢』占比高(>30%) → 入场点偏晚或止损过近，P1 优先（挂单等回踩 + 按 MAE 分布定 SL）")
    print("   · 保本转化率低 + 扫损多 → 保本门槛过高（breakeven_atr_mult / breakeven_tp_ratio 待调）")


if __name__ == "__main__":
    asyncio.run(main())
