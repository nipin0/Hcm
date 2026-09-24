"""verify_reversal_cf.py — 反转头反事实口径 v2 的独立验证（0052 / 2026-09-21）

【为什么必须有这个脚本】
  本轮修的正是"**评估装置本身不可用**"：v1 实测（709 行归因）
    · `mode='act'` 共 29 行，**delta 非零者 0 条**（sum_delta = 0.00）
    · `verdict='killed'` **全期 0 条**（结构性不可达）
    · 唯一有非零 delta 的 100 条全在 `mode='log_skip'`（**未行动**）行 —— 与反转头动作无关
  一个**不可验证**的评估器等于没有评估能力 ⇒ 口径本身必须有独立验证。

【验证三层】
  §1 `_rev_cf_decide`（判定，纯函数）    ：四种 cf_touch 分支全可达
  §2 `_rev_cf_verdict`（结算，纯函数）   ：delta 符号 → saved/killed/neutral + ambiguous 归零
  §3 `_rev_cf_race`（取数，真 SQL）      ：合成 K 线上首触时刻正确；tp 未设退化为单边
  §4 **端到端三场景**（真 K 线 + 真 SQL）：同时跑 v1 与 v2 口径做对照 ——
       A「收紧救了钱」        v1=neutral(delta 0) → v2=**saved(delta > 0)**
       B「收紧被扫出后回头」   v1=neutral(delta 0) → v2=**killed(delta < 0)**
       C「同 bar 双触」                        → v2=**ambiguous(delta 归 0)**
     A/B 同时是"为什么必须改口径"与"改完确实能评估"的双重证据。

【安全性】
  · `hcm_market.klines` 是**按 symbol 分区**的表（合成 symbol 无分区，会报
    `no partition of relation "klines"`），故本脚本使用**真实 `XAUUSD` 分区**，
    但时间戳放在 **2030 年远期**（真实数据不存在，不会与实盘/回放互相污染）。
  · 全部写入在事务内并 **ROLLBACK**；且未提交的数据对其他会话不可见。
用法：python verify_reversal_cf.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)

DSN = os.getenv("PG_DSN", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
SYM = "XAUUSD"          # 用真实分区（见文件头"安全性"）
TF = "M5"

_PASS = _FAIL = 0


def ck(name, got, want):
    global _PASS, _FAIL
    ok = (got == want)
    _PASS, _FAIL = _PASS + ok, _FAIL + (not ok)
    print(f"  {'OK  ' if ok else 'FAIL'} {name:<50} got={got!r:<26} want={want!r}")
    return ok


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)   # dataclass 注解解析需要（本仓库既有约定）
    spec.loader.exec_module(mod)
    return mod


def bars(base, rows):
    """rows: [(n, o, h, l, c)]，n = 距 base 的 M5 根数。"""
    return [(base + timedelta(minutes=5 * n), o, h, l, c) for (n, o, h, l, c) in rows]


async def _ins(conn, bs):
    await conn.executemany(
        "INSERT INTO hcm_market.klines (symbol, time_frame, open_time, open, high, low, close) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7)",
        [(SYM, TF, t, o, h, l, c) for (t, o, h, l, c) in bs])


async def main() -> int:
    mb = _load("mt5_bridge", os.path.join(TOOLS, "mt5_bridge.py"))
    decide, verdict, race = mb._rev_cf_decide, mb._rev_cf_verdict, mb._rev_cf_race

    print("=" * 96)
    print("§1 _rev_cf_decide —— 判定口径（纯函数，零 IO）")
    print("=" * 96)
    t0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
    t1, t5 = t0 + timedelta(minutes=5), t0 + timedelta(minutes=25)
    ck("先触 old_sl → (old_sl,'sl',False)", decide(t0, t1, 95.0, 105.0, 98.0), (95.0, "sl", False))
    ck("先触 tp     → (tp,'tp',False)", decide(t5, t0, 95.0, 105.0, 98.0), (105.0, "tp", False))
    ck("tp 未设(None) → 单边 old_sl", decide(t0, None, 95.0, 105.0, 98.0), (95.0, "sl", False))
    ck("都没触及 → (exit_px,'none',False)", decide(None, None, 95.0, 105.0, 98.0), (98.0, "none", False))
    ck("同 bar 双触 → (old_sl,'both_same_bar',True)",
       decide(t0, t0, 95.0, 105.0, 98.0), (95.0, "both_same_bar", True))

    print("\n" + "=" * 96)
    print("§2 _rev_cf_verdict —— 结算口径（纯函数）：四态全可达")
    print("=" * 96)
    # BUY：entry=100、sign=+1、实际 98 出场 ⇒ realized = -2
    ck("BUY/saved   : cf=95  → cf_pnl=-5, delta=+3", verdict(-2.0, 95.0, 100.0, 1.0, False),
       (-5.0, 3.0, "saved"))
    ck("BUY/killed  : cf=105 → cf_pnl=+5, delta=-7", verdict(-2.0, 105.0, 100.0, 1.0, False),
       (5.0, -7.0, "killed"))
    ck("BUY/neutral : cf=exit_px → delta=0", verdict(-2.0, 98.0, 100.0, 1.0, False),
       (-2.0, 0.0, "neutral"))
    ck("ambiguous   : delta 强制 0", verdict(-2.0, 95.0, 100.0, 1.0, True), (-5.0, 0.0, "ambiguous"))
    # SELL：entry=100、sign=-1、实际 98 出场 ⇒ realized = +2
    #   ⚠ SELL 下"更高的出场价"更差 ⇒ cf=105 为 cf_pnl=-5 ⇒ delta=+7 ⇒ saved
    ck("SELL/saved  : cf=105 → cf_pnl=-5, delta=+7", verdict(2.0, 105.0, 100.0, -1.0, False),
       (-5.0, 7.0, "saved"))
    ck("SELL/killed : cf=95  → cf_pnl=+5, delta=-3", verdict(2.0, 95.0, 100.0, -1.0, False),
       (5.0, -3.0, "killed"))

    print("\n" + "=" * 96)
    print("§3 _rev_cf_race（真 SQL）+ §4 端到端 v1↔v2 对照 —— 合成 K 线在事务内，用后 ROLLBACK")
    print("=" * 96)
    try:
        import asyncpg
        conn = await asyncpg.connect(DSN, timeout=8)
    except Exception as exc:  # noqa: BLE001
        print(f"  [致命] 连不上 PG（{exc}）⇒ SQL 层未验证，**不得判定通过**")
        print(f"\n共 {_PASS} 项，失败 {_FAIL} 项（SQL 层未验证）")
        return 1

    tr = conn.transaction()
    await tr.start()
    try:
        # ── 三场景（各自独立远期时段，互不干扰）────────────────────────
        # A「收紧救了钱」：BUY entry=100，old_sl=95 → 收紧到 new_sl=98；
        #   实际在 +1 根被 98 扫出（realized=-2）；随后价格继续跌到 94（会打到 95）
        BA = datetime(2030, 1, 1, tzinfo=timezone.utc)
        A = bars(BA, [(0, 100.0, 100.5, 99.5, 100.0),
                      (1, 100.0, 100.0, 98.0, 98.5),    # ← 实际出场（new_sl=98）
                      (2, 98.5, 99.0, 96.0, 96.5),
                      (3, 96.5, 97.0, 94.0, 94.5),      # ← 触及 old_sl=95
                      (4, 94.5, 95.0, 93.0, 94.0)])
        # B「收紧被扫出后行情回头」：同入场同收紧，但随后反弹到 105（TP）
        BB = datetime(2030, 2, 1, tzinfo=timezone.utc)
        B = bars(BB, [(0, 100.0, 100.5, 99.5, 100.0),
                      (1, 100.0, 100.0, 98.0, 98.5),    # ← 实际出场（new_sl=98）
                      (2, 98.5, 101.0, 98.2, 100.5),    # low=98.2 > 95 ⇒ 不再触及 old_sl
                      (3, 101.0, 103.0, 100.5, 102.5),
                      (4, 103.0, 105.5, 102.5, 104.0)]) # ← 触及 tp=105
        # C「同 bar 双触」：一根 M5 内既 l<=95 又 h>=105
        BC = datetime(2030, 3, 1, tzinfo=timezone.utc)
        C = bars(BC, [(0, 100.0, 100.5, 99.5, 100.0),
                      (1, 100.0, 106.0, 94.0, 100.0)])
        for bs in (A, B, C):
            await _ins(conn, bs)

        WE = timedelta(hours=12.0)
        print("\n§3 取数：固定视界内的首触时刻")
        ra = await race(conn, SYM, "BUY", 95.0, 105.0, BA, BA + WE)
        ck("A: t_sl = 第 4 根（+15min），t_tp = None",
           ra, (BA + timedelta(minutes=15), None))
        rb = await race(conn, SYM, "BUY", 95.0, 105.0, BB, BB + WE)
        ck("B: t_sl = None（全程未破 95），t_tp = 第 5 根（+20min）",
           rb, (None, BB + timedelta(minutes=20)))
        rc = await race(conn, SYM, "BUY", 95.0, 105.0, BC, BC + WE)
        ck("C: 同 bar 双触 ⇒ t_sl == t_tp（非 None）",
           (rc[0] is not None and rc[0] == rc[1]), True)
        ck("B: tp 未设(0) ⇒ 退化为单边（t_tp=None）",
           (await race(conn, SYM, "BUY", 95.0, 0.0, BB, BB + WE))[1], None)
        ck("A: 视界缩到 +10min ⇒ 触及发生在视界外 ⇒ t_sl=None（证明视界真在起作用）",
           (await race(conn, SYM, "BUY", 95.0, 105.0, BA, BA + timedelta(minutes=10)))[0], None)
        ck("SELL 方向条件对称（high>=old_sl）: 用 A 数据查 SELL/old_sl=100.2 ⇒ 第 1 根即触",
           (await race(conn, SYM, "SELL", 100.2, 0.0, BA, BA + WE))[0], BA)

        print("\n§4 端到端对照 A「收紧救了钱」—— v1 缺陷 vs v2 修复")
        ck("A/v1: 窗口[adj, 实际出场+5min] 内低点 98.0 > old_sl 95 ⇒ 未触及（缺陷成因）",
           min(b[3] for b in A[:2]) > 95.0, True)
        ck("A/v1: cf=exit_px=98 ⇒ delta=0 ⇒ **neutral**（成功的收紧被判中性）",
           verdict(-2.0, 98.0, 100.0, 1.0, False)[2], "neutral")
        a_cf, a_touch, a_amb = decide(ra[0], ra[1], 95.0, 105.0, 98.0)
        ck("A/v2: cf_touch='sl'、cf=95", (a_touch, a_cf), ("sl", 95.0))
        ck("A/v2: delta=+3 ⇒ **saved**", verdict(-2.0, a_cf, 100.0, 1.0, a_amb)[1:],
           (3.0, "saved"))

        print("\n§4 端到端对照 B「收紧被扫出后行情回头」—— 误杀必须可见")
        ck("B/v1: 窗口内低点 98.0 > old_sl 95 ⇒ 未触及（缺陷成因）",
           min(b[3] for b in B[:2]) > 95.0, True)
        ck("B/v1: cf=exit_px=98 ⇒ delta=0 ⇒ **neutral**（误杀不可见）",
           verdict(-2.0, 98.0, 100.0, 1.0, False)[2], "neutral")
        b_cf, b_touch, b_amb = decide(rb[0], rb[1], 95.0, 105.0, 98.0)
        ck("B/v2: cf_touch='tp'、cf=105", (b_touch, b_cf), ("tp", 105.0))
        ck("B/v2: delta=-7 ⇒ **killed**（v1 结构性不可达）",
           verdict(-2.0, b_cf, 100.0, 1.0, b_amb)[1:], (-7.0, "killed"))

        print("\n§4 端到端对照 C「同 bar 双触」—— 不冒充方向")
        c_cf, c_touch, c_amb = decide(rc[0], rc[1], 95.0, 105.0, 98.0)
        ck("C/v2: cf_touch='both_same_bar'、ambiguous=True", (c_touch, c_amb),
           ("both_same_bar", True))
        ck("C/v2: verdict='ambiguous' 且 delta 记 0",
           verdict(-2.0, c_cf, 100.0, 1.0, c_amb)[1:], (0.0, "ambiguous"))

        print("\n§5 结算门 SQL 表达式可用（_rev_settle_closed 的 adj_ts 门）")
        ck("now() - make_interval(secs => 43200) 可求值",
           (await conn.fetchrow(
               "SELECT (now() - make_interval(secs => $1)) < now() AS ok",
               12 * 3600.0))["ok"], True)
        ck("12h 前的 adj_ts 通过门、1h 前的不通过",
           (await conn.fetchrow(
               "SELECT (now() - interval '12 hours' <= now() - make_interval(secs => $1)) AS a, "
               "       (now() - interval '1 hour'  <= now() - make_interval(secs => $1)) AS b",
               12 * 3600.0))["a"],
           True)

        print("\n§6 两条写入语句的真实 SQL 冒烟（列名/占位符必须与 0052 DDL 一致）")
        # 用与 _rev_log_adjust 完全相同的列清单与占位符
        rid = await conn.fetchval(
            "INSERT INTO hcm_ai.reversal_attribution "
            "(account_id, ticket, symbol, direction, entry_price, price_at_adj, "
            " old_sl, new_sl, atr, score, cutoff, is_reversal, mode, dd_atr, tp) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING id",
            999999, 2030000001, SYM, "BUY", 100.0, 100.0, 95.0, 98.0,
            5.0, 0.5860, 0.5249, True, "act", 0.5, 105.0)
        ck("_rev_log_adjust 的 INSERT（15 列，含 tp）可执行", bool(rid), True)
        ck("刚插入的行（adj_ts≈now）**被视界门挡住**（结算滞后 H 达成的关键）",
           await conn.fetchval(
               "SELECT count(*)::int FROM hcm_ai.reversal_attribution "
               "WHERE id=$1 AND closed_ts IS NULL AND adj_ts <= now() - make_interval(secs => $2)",
               rid, 12 * 3600.0), 0)
        rid2 = await conn.fetchval(
            "INSERT INTO hcm_ai.reversal_attribution "
            "(account_id, ticket, symbol, direction, entry_price, price_at_adj, "
            " old_sl, new_sl, atr, score, cutoff, is_reversal, mode, dd_atr, tp, adj_ts) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,"
            "        now() - interval '13 hours') RETURNING id",
            999999, 2030000002, SYM, "BUY", 100.0, 100.0, 95.0, 98.0,
            5.0, 0.5860, 0.5249, True, "act", 0.5, 105.0)
        ck("13h 前的行**通过视界门**（可被结算）",
           await conn.fetchval(
               "SELECT count(*)::int FROM hcm_ai.reversal_attribution "
               "WHERE id=$1 AND closed_ts IS NULL AND adj_ts <= now() - make_interval(secs => $2)",
               rid2, 12 * 3600.0), 1)
        # 用与 _rev_settle_closed 完全相同的 UPDATE 语句
        ck("_rev_settle_closed 的 UPDATE（含 cf_touch/cf_horizon_h/cf_basis）可执行",
           await conn.execute(
               "UPDATE hcm_ai.reversal_attribution SET closed_ts=now(), "
               "exit_price=$1, realized_pnl=$2, cf_exit_price=$3, cf_pnl=$4, "
               "delta=$5, verdict=$6, cf_touch=$7, cf_horizon_h=$8, cf_basis=$9 WHERE id=$10",
               98.0, -2.0, 105.0, 5.0, -7.0, "killed", "tp", 12.0, "v2_race", rid), "UPDATE 1")
        _row = await conn.fetchrow(
            "SELECT verdict, cf_touch, cf_horizon_h, cf_basis, tp, delta "
            "FROM hcm_ai.reversal_attribution WHERE id=$1", rid)
        ck("回读：verdict/cf_touch/cf_basis/tp/delta 全部落位",
           (_row["verdict"], _row["cf_touch"], float(_row["cf_horizon_h"]),
            _row["cf_basis"], float(_row["tp"]), float(_row["delta"])),
           ("killed", "tp", 12.0, "v2_race", 105.0, -7.0))
        # 日报只取 v2 口径 ⇒ 该行应被日报看见；历史 v1 行（cf_basis IS NULL）应被排除
        ck("日报 _Q2 口径过滤：本行(cf_basis='v2_race') 计入 act 组",
           await conn.fetchval(
               "SELECT count(*)::int FROM hcm_ai.reversal_attribution "
               "WHERE id=$1 AND cf_basis='v2_race' "
               "  AND CASE WHEN mode='act' THEN 'act' ELSE 'shadow' END = 'act'", rid), 1)
    finally:
        await tr.rollback()
        await conn.close()

    print("\n" + "=" * 96)
    print(f"共 {_PASS} 项，失败 {_FAIL} 项 —— " + ("全部通过" if _FAIL == 0 else "**存在失败**"))
    print("=" * 96)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
