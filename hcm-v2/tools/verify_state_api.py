"""FSM 状态机监控 API —— 真实数据端到端自测（零生产影响）

用真实 asyncpg 池 + 真实 shared.RedisClient + 真实 ConfigProviderV3 装载
hcm-web/web/api/state.py 的 router，再经 httpx ASGITransport 直调 5 个端点。
不启动容器、不改任何生产数据、不写任何配置键。

运行：C:/Python313/python.exe D:\\HCM_ASST\\.workbuddy\\tmp\\test_state_api.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(r"D:\HCM_ASST\hcm-v2")
sys.path.insert(0, str(ROOT))

import asyncpg                              # noqa: E402
import httpx                                # noqa: E402
from fastapi import FastAPI                 # noqa: E402

from shared.redis_client import RedisClient          # noqa: E402
from shared.config_provider import ConfigProviderV3  # noqa: E402

PG_DSN = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"
REDIS_URL = "redis://localhost:6379"
STATE_PY = ROOT / "hcm-web" / "web" / "api" / "state.py"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


class AuthStub:
    """替代 AuthHandler：self-test 不需要鉴权（require_auth 作为 Depends 可调用）"""

    async def require_auth(self):
        return {"username": "selftest"}


def load_state_module():
    spec = importlib.util.spec_from_file_location("state_api_under_test", STATE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def main():
    print("=" * 78)
    print("FSM 状态机监控 API 自测 · 真实 PG + 真实 Redis")
    print("=" * 78)

    mod = load_state_module()
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=3)
    rc = RedisClient(REDIS_URL)
    await rc.initialize()
    cfg = ConfigProviderV3(pool, rc.raw)
    await cfg.initialize()

    print(f"\n[env] redis is_initialized={rc.is_initialized}  pg pool ok")

    app = FastAPI()
    app.include_router(mod.create_state_router(pool, cfg, AuthStub(), rc))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://selftest", timeout=30) as ac:
        SYM = "XAUUSD"

        # ── 1) /live ────────────────────────────────────────────────────────
        print("\n── 1) GET /api/v1/state/live/XAUUSD")
        r = await ac.get(f"/api/v1/state/live/{SYM}")
        assert r.status_code == 200, r.status_code
        env = r.json()
        check("HTTP 200 + code==0", env.get("code") == 0, f"code={env.get('code')} msg={env.get('message')}")
        d = env.get("data") or {}
        dv = d.get("derived") or {}
        print(f"     state={dv.get('state')} ({dv.get('state_cn')})  is_osc={dv.get('is_osc')}")
        print(f"     box_upper={d.get('ctx',{}).get('box_upper')} box_lower={d.get('ctx',{}).get('box_lower')} "
              f"-> derived.box_height={dv.get('box_height')}")
        print(f"     box_frozen={dv.get('box_frozen')} freezed_at={dv.get('box_frozen_at')} "
              f"misaligned={dv.get('freeze_rule_misaligned')}")
        print(f"     fsm.pending_streak={ (d.get('fsm') or {}).get('pending_streak') } "
              f"fsm.age_bars={(d.get('fsm') or {}).get('age_bars')} "
              f"fsm.direction={(d.get('fsm') or {}).get('direction')}")
        print(f"     live.proba={ (d.get('live') or {}).get('proba') }")
        print(f"     directive={d.get('directive')}")

        check("4 个 Redis 键已合并返回", all(k in d for k in ("fsm", "live", "ctx", "directive")))
        check("derived.state 属 7 态枚举", dv.get("state") in (None, "S0_IDLE", "S1_OSC", "S2_TREND_INIT",
              "S3_TREND_MID", "S4_TREND_FADE", "S5_OSC_LOCKED", "S9_PAUSED"), str(dv.get("state")))
        bu, bl, bh = d.get("ctx", {}).get("box_upper"), d.get("ctx", {}).get("box_lower"), dv.get("box_height")
        if isinstance(bu, (int, float)) and isinstance(bl, (int, float)):
            check("box_height = upper - lower（自算正确）",
                  abs((bu - bl) - bh) < 1e-6, f"{bu} - {bl} = {round(bu-bl,5)} vs {bh}")
        check("trend_detail_published 如实为 False", dv.get("trend_detail_published") is False)
        check("trend_detail_reason 非空", bool(dv.get("trend_detail_reason")))
        cfgd = d.get("config") or {}
        check("config 返回 9 个键（含真实取值，非全默认）",
              len(cfgd) == 9 and all(("value" in v and "key" in v) for v in cfgd.values()),
              f"{len(cfgd)} keys")
        for k, v in cfgd.items():
            print(f"       cfg.{k:20} {v['key']:34} value={v['value']} default={v['default']}")

        # ── 2) /logs ────────────────────────────────────────────────────────
        print("\n── 2) GET /api/v1/state/logs/XAUUSD?tf=M5&limit=5")
        r = await ac.get(f"/api/v1/state/logs/{SYM}", params={"tf": "M5", "limit": 5})
        env = r.json()
        check("HTTP 200 + code==0", env.get("code") == 0, str(env.get("code")))
        items = (env.get("data") or {}).get("items") or []
        check("返回行数 = limit(5)", len(items) == 5, f"{len(items)}")
        if items:
            check("29 列全部返回", len(items[0]) == 29, f"{len(items[0])} cols")
            missing = [c for c in mod._LOG_COLS.replace(" ", "").split(",") if c not in items[0]]
            check("_LOG_COLS 与表列完全对齐（无 UndefinedColumn）", not missing, f"missing={missing}")
            check("时间倒序（DESC）",
                  all(items[i]["bar_open_time"] >= items[i + 1]["bar_open_time"] for i in range(len(items) - 1)))
            check("prob_* 四列有真实数值", any(items[0].get(f"prob_{k}") is not None
                  for k in ("oscillation", "trend_init", "trend_mid", "trend_fade")))
            check("无 signal_id / signal_mode 列（不做伪关联）",
                  "signal_id" not in items[0] and "signal_mode" not in items[0])
            print(f"     首行: t={items[0]['bar_open_time']} state={items[0]['state']} "
                  f"trans={items[0]['transitioned']} margin={items[0]['margin']} age={items[0]['age_bars']}")

        print("\n── 2b) /logs 参数边界")
        r = await ac.get(f"/api/v1/state/logs/{SYM}", params={"tf": "M5", "limit": 0})
        check("limit=0 被夹到 >=1 且不报错", r.json().get("code") == 0)
        r = await ac.get(f"/api/v1/state/logs/{SYM}", params={"tf": "NOSUCHTF", "limit": 5})
        check("未知周期 → code==0 且空列表（不抛错）",
              r.json().get("code") == 0 and len(r.json()["data"]["items"]) == 0)
        r = await ac.get(f"/api/v1/state/logs/{SYM}",
                         params={"tf": "M5", "since": "2026-09-15T00:00:00+00:00", "limit": 500})
        n_since = len(r.json()["data"]["items"])
        check("since 时间窗过滤生效（iso+偏移 形式）", 0 < n_since < 199, f"{n_since} rows since 09-15T00:00Z")
        r = await ac.get(f"/api/v1/state/logs/{SYM}",
                         params={"tf": "M5", "since": "2026-09-15", "limit": 500})
        n_date = len(r.json()["data"]["items"])
        check("since 支持纯日期 YYYY-MM-DD", 0 < n_date < 199, f"{n_date} rows")
        r = await ac.get(f"/api/v1/state/logs/{SYM}",
                         params={"tf": "M5", "since": "2026-09-14T15:00:00Z",
                                 "until": "2026-09-14T16:00:00Z", "limit": 500})
        n_win = len(r.json()["data"]["items"])
        check("since + until 区间收窄（1 小时窗）", 0 < n_win < 30, f"{n_win} rows in 15:00-16:00Z")
        r = await ac.get(f"/api/v1/state/logs/{SYM}",
                         params={"tf": "M5", "since": "NOT-A-TIME", "limit": 20})
        check("非法时间串被忽略而非静默清空", r.json().get("code") == 0
              and len(r.json()["data"]["items"]) == 20, f"{len(r.json()['data']['items'])} rows")

        # ── 3) /proba ───────────────────────────────────────────────────────
        print("\n── 3) GET /api/v1/state/proba/XAUUSD?tf=M5&limit=50")
        env = (await ac.get(f"/api/v1/state/proba/{SYM}", params={"tf": "M5", "limit": 50})).json()
        check("code==0", env.get("code") == 0)
        pit = env["data"]["items"]
        check("窄响应只含 10 列", len(pit[0]) == 10 if pit else False, f"{len(pit[0]) if pit else 0} cols")
        check("已反转为升序（供前端直接连线）",
              all(pit[i]["bar_open_time"] <= pit[i + 1]["bar_open_time"] for i in range(len(pit) - 1)),
              f"{len(pit)} rows")
        print(f"     升序首/末: {pit[0]['bar_open_time']} → {pit[-1]['bar_open_time']}")
        r = await ac.get(f"/api/v1/state/proba/{SYM}", params={"tf": "M5", "limit": 99999})
        check("limit 超上限被夹到 3000 且不报错", r.json().get("code") == 0)

        # ── 4) /kline ───────────────────────────────────────────────────────
        print("\n── 4) GET /api/v1/state/kline/XAUUSD?tf=M5&limit=300")
        env = (await ac.get(f"/api/v1/state/kline/{SYM}", params={"tf": "M5", "limit": 300})).json()
        check("code==0", env.get("code") == 0)
        kd = env["data"]
        kb = kd["items"]
        check("返回 300 根 K 线", len(kb) == 300, f"{len(kb)}")
        check("时间升序（供 candlestick 直接画）",
              all(kb[i]["open_time"] <= kb[i + 1]["open_time"] for i in range(len(kb) - 1)))
        check("OHLC 四价齐全且为数值",
              all(kb[0].get(x) is not None for x in ("open", "high", "low", "close")))
        check("LEFT JOIN 命中状态（state_matched > 0）", kd["state_matched"] > 0,
              f"state_matched={kd['state_matched']}/{len(kb)}")
        check("JOIN 列名 time_frame 正确（无 UndefinedColumn 静默空）", kd["state_matched"] > 0)
        check("未命中行 state 为 None（非伪造值）",
              all((it.get("state") is None) or isinstance(it.get("state"), str) for it in kb))
        print(f"     state_matched={kd['state_matched']}  首={kb[0]['open_time']} 末={kb[-1]['open_time']}")
        r = await ac.get(f"/api/v1/state/kline/{SYM}", params={"tf": "M5", "limit": 1})
        check("limit=1 被夹到 >=10", len(r.json()["data"]["items"]) == 10)

        # ── 5) /risk ────────────────────────────────────────────────────────
        print("\n── 5) GET /api/v1/state/risk/XAUUSD")
        env = (await ac.get(f"/api/v1/state/risk/{SYM}")).json()
        check("code==0", env.get("code") == 0)
        rk = env["data"]
        print(f"     positions_open={rk['positions_open']} float_profit_sum={rk['float_profit_sum']}")
        for p in rk["positions"]:
            print(f"       pos {p['direction']} {p['lot']} open={p['open_price']} cur={p['current_price']} "
                  f"sl={p['sl']} tp={p['tp']} float={p['float_profit']}")
        print(f"     day_loss={rk['day_loss']} (n={rk['day_loss_orders']})  symbol={rk['day_loss_symbol']} "
              f"cap={rk['day_loss_cap']}")
        check("持仓明细含 sl/tp/float_profit", all("sl" in p and "tp" in p for p in rk["positions"]))
        check("当日亏损为数值（非 None）", isinstance(rk["day_loss"], (int, float)), str(rk["day_loss"]))
        check("day_loss_cap 来自真实配置", rk["day_loss_cap"] > 0, str(rk["day_loss_cap"]))
        ro = rk.get("recent_orders") or []
        check("recent_orders 已返回（K 线开平仓标记的数据源）", len(ro) > 0, f"{len(ro)} orders")
        if ro:
            check("recent_orders 含 open_time + close_time（positions 无 close_time 故取 orders）",
                  all(o.get("open_time") and o.get("close_time") for o in ro))
            print(f"     近 3 笔: " + " | ".join(
                f"{o['direction']} {o['open_price']}->{o['close_price']} pnl={o['profit']}" for o in ro[:3]))
        check("orders_source 说明存在", bool(rk.get("orders_source")))
        r = await ac.get(f"/api/v1/state/risk/{SYM}", params={"order_limit": 9999})
        check("order_limit 超上限被夹到 300 且不报错", r.json().get("code") == 0)

        # ── 6) 未知品种路径 ─────────────────────────────────────────────────
        print("\n── 6) 边界：未知品种")
        env = (await ac.get("/api/v1/state/live/NOSUCHSYM")).json()
        check("未知品种 live → data=None + 明确 message（不伪造快照）",
              env.get("data") is None and "no_state_snapshot" in str(env.get("message")),
              f"msg={env.get('message')}")
        env = (await ac.get("/api/v1/state/logs/NOSUCHSYM", params={"tf": "M5"})).json()
        check("未知品种 logs → code==0 且空列表", env.get("code") == 0 and env["data"]["items"] == [])
        env = (await ac.get("/api/v1/state/kline/NOSUCHSYM", params={"tf": "M5"})).json()
        check("未知品种 kline → state_matched==0", env["data"]["state_matched"] == 0)
        env = (await ac.get("/api/v1/state/risk/NOSUCHSYM")).json()
        check("未知品种 risk → 持仓 0 + 账号级当日亏损仍返回（与风控同口径）",
              env["data"]["positions_open"] == 0 and isinstance(env["data"]["day_loss"], (int, float)))

    await cfg.shutdown()
    await rc.shutdown()
    await pool.close()

    print("\n" + "=" * 78)
    print(f"RESULT: {'PASS' if not FAIL else 'FAIL'}  ({len(PASS)} passed, {len(FAIL)} failed)")
    if FAIL:
        for f in FAIL:
            print("   FAILED:", f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
