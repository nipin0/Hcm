#!/usr/bin/env python3
"""reconcile_labels.py — 真实成交盈亏标签回流 + AI 分校准报表（P0-O2 2026-08-22）。

背景（评审结论）：build_labels.py 的 label 由「入场后 K线价格触达 ±1R」判定，
是"价格走没走对"的代理，≠"这笔单真的赚没赚"。production 想筛的是"能赚钱的信号"，
但训练标签没接真实成交盈亏 → 模型学的是价格代理而非真实盈亏，精准信号名不副实。

本脚本把真实成交盈亏回流，作为训练标签的第二来源/校正依据：
  1. 真源：hcm_trading.orders.profit（平仓盈亏 USD），按 signal_id 关联。
  2. JOIN hcm_ai.inference_log 取同信号的 AI 分 / total_score / model_version。
  3. 产出两份物：
     - 报表（stdout/JSON）：AI 分对"真实盈亏正负"的判别力（AUC on real PnL）、
       分桶真实胜率、覆盖、各模型版本的真实表现 → 人工核对模型排序与真实盈亏是否一致。
     - 标签 CSV（--out）：以 profit>0 为真实标签，供 build_labels / train 校验对照。

纪律红线：
  - 只读 orders/inference_log/gate_decision，不写任何表（产物为本地 CSV + 报表）。
  - 不改变线上模型/配置，纯观测 + 训练标签回流依据。
  - 与 build_labels 的"价格触达标签"并存：本脚本产出的是"真实盈亏标签"，用于
    校准/校验，而非替代价格触达标签（真实标签覆盖有限：仅平仓且有 profit 的单）。

用法:
  python reconcile_labels.py --out labels_real.csv          # 产真实盈亏标签 CSV + 报表
  python reconcile_labels.py --auc-only                     # 只出 AI 分 vs 真实盈亏 AUC 报表
  python reconcile_labels.py --window-days 7                # 只看近 7 天成交
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg2

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@127.0.0.1:5432/hcm_v2"

# 与 quality_gate 的等级阈值对齐（0-100 的 AI 分）
UP_TH = 70.0
PASS_TH = 50.0
DOWN_TH = 60.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="真实盈亏标签 CSV 输出路径（不传则不写文件）")
    ap.add_argument("--auc-only", action="store_true", help="只出 AI 分 vs 真实盈亏 AUC 报表")
    ap.add_argument("--window-days", type=int, default=0, help="只看近 N 天成交（0=全部）")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    args = ap.parse_args()

    conn = psycopg2.connect(args.db_url)
    try:
        _win = ""
        if args.window_days and args.window_days > 0:
            _win = f"AND o.close_time >= now() - interval '{int(args.window_days)} days'"
        # 真实盈亏标签源：orders.profit，按 signal_id 关联 AI 快照（inference_log 按信号最近一条）
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    o.signal_id, o.symbol, o.direction, o.profit,
                    o.open_time, o.close_time,
                    il.ai_score, il.total_score, il.model_version
                FROM hcm_trading.orders o
                LEFT JOIN LATERAL (
                    SELECT ai_score, total_score, model_version, created_at
                    FROM hcm_ai.inference_log il
                    WHERE il.symbol = o.symbol
                      AND il.created_at <= o.close_time
                    ORDER BY il.created_at DESC
                    LIMIT 1
                ) il ON true
                WHERE o.signal_id IS NOT NULL
                  AND o.profit IS NOT NULL AND o.profit <> 0
                  {win}
                ORDER BY o.close_time
                """.format(win=_win)
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()

        import numpy as np
        import pandas as pd

        df = pd.DataFrame(rows, columns=cols)
        if df.empty:
            print("[reconcile] no closed orders with profit in window", file=sys.stderr)
            return

        df["profit"] = df["profit"].astype(float)
        df["ai_score"] = pd.to_numeric(df["ai_score"], errors="coerce")
        # 真实标签：profit > 0 视为 win
        df["real_label"] = (df["profit"] > 0).astype(int)

        n = len(df)
        n_win = int(df["real_label"].sum())
        print(f"[reconcile] closed orders with profit: {n} | real_win_rate={n_win / n:.3f} "
              f"({n_win} win / {n - n_win} loss)")

        # 1) AI 分对真实盈亏正负的判别力（有效 AI 分样本）
        has_ai = df["ai_score"].notna()
        if has_ai.sum() >= 30 and df.loc[has_ai, "real_label"].nunique() >= 2:
            from sklearn.metrics import roc_auc_score
            y = df.loc[has_ai, "real_label"].values
            p = df.loc[has_ai, "ai_score"].values / 100.0
            auc = roc_auc_score(y, p)
            print(f"[reconcile] AI分 vs 真实盈亏 AUC={auc:.4f} (n_ai={int(has_ai.sum())})")
            print("  ↑ 该 AUC 是模型对『真实赚钱』的判别力，与 build_labels 的"
                  "『价格触达』AUC 并非同一口径。若显著低于价格触达 AUC，说明模型"
                  "排序与真实盈亏脱节，需要接真实标签重训。")
        else:
            print(f"[reconcile] AI分样本不足（n_ai={int(has_ai.sum())}），跳过 AUC")

        # 2) 分桶真实胜率（对照 quality_gate 阈值）
        print("[reconcile] AI分分桶真实胜率（对照等级阈值 50/60/70）：")
        base = n_win / n
        for lo, hi, tag in [(70.0, 101.0, "高分≥70(升级区)"),
                            (60.0, 70.0, "中高60-70(保持/升级边)"),
                            (50.0, 60.0, "中分50-60(降级区)"),
                            (0.0, 50.0, "低分<50(否决/降级区)")]:
            m = df["ai_score"].notna() & df["ai_score"].ge(lo) & df["ai_score"].lt(hi)
            if m.sum() == 0:
                print(f"    {tag}: 覆盖 0")
                continue
            wr = df.loc[m, "real_label"].mean()
            print(f"    {tag}: n={int(m.sum())} 真实胜率={wr:.3f} "
                  f"vs 基线{base:.3f} (提升{wr - base:+.3f})")

        # 3) 按模型版本统计真实表现
        if df["model_version"].notna().any():
            print("[reconcile] 各模型版本真实表现：")
            for ver, g in df[df["model_version"].notna()].groupby("model_version"):
                print(f"    {ver}: n={len(g)} 真实胜率={g['real_label'].mean():.3f} "
                      f"均AI分={g['ai_score'].mean():.1f}")

        # 4) 可选：输出真实盈亏标签 CSV
        if args.out:
            df.to_csv(args.out, index=False)
            print(f"[reconcile] real-PnL labels -> {args.out}")

        # 5) --auc-only 时只打印 JSON 摘要
        if args.auc_only:
            summary = {
                "n_closed_with_profit": int(n),
                "real_win_rate": round(n_win / n, 4),
                "n_with_ai": int(has_ai.sum()),
            }
            if has_ai.sum() >= 30:
                try:
                    from sklearn.metrics import roc_auc_score as _r
                    summary["auc_ai_vs_real_pnl"] = round(
                        _r(df.loc[has_ai, "real_label"].values,
                           df.loc[has_ai, "ai_score"].values / 100.0), 4)
                except Exception:
                    pass
            print(json.dumps(summary, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
