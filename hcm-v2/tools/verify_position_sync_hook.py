"""verify_position_sync_hook.py — `position_sync` 震荡计数器回写的**上线前验证**。

必须证明三件事（否则不能重启桥）：
  1. **按路径加载在真实部署布局下可解析**：`_load_fsm_modules()` 的相对路径
     `../hcm-signal-tower/signal_tower` 在桥所在的目录树下确实存在。
     （若解析失败，回写会**静默降级**成"什么都不做"—— 这正是本项目反复踩的坑。）
  2. **当前完全 inert**：库里尚无 `signal_mode` 以 `state` 开头的信号 →
     任何平仓都必须**零 Redis 写入**。故重启桥对现有交易**零影响**。
  3. **管道本身可用**：一旦下发 `state_osc`，sl/tp 能正确推进两个计数器；
     裸 `state_fsm`（未分子模式）必须**跳过 + 告警**，绝不把趋势亏损记进震荡预算。

用法：python verify_position_sync_hook.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)          # position_sync 与 _mt5_timeutil 都在 tools/ 下

import position_sync as PS  # noqa: E402

LOG = logging.getLogger("verify_position_sync_hook")
FAILED: list[str] = []
N = 0


def ck(name: str, got, want) -> None:
    global N
    N += 1
    ok = (got == want)
    if not ok:
        FAILED.append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name:<44} got={got!r:<34} want={want!r}")


class MockRedis:
    is_initialized = True

    def __init__(self) -> None:
        self.kv: dict = {}
        self.writes: list = []

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, ex=None, nx=False):
        """支持 `nx`（生产代码用 `SET ... NX EX` 做**轮次幂等**，见 BUG-1/2/3 修复）。

        ⚠ 测试替身必须与**生产真实客户端能力对齐**：此前只支持 `ex`，而 `position_sync`
        新增的轮次幂等守卫用 `nx=True` ⇒ 替身抛 TypeError → 守卫走 fail-open 分支
        ⇒ **守卫未被覆盖，套件却显示"全部通过"**（2026-09-16 实测发现）。
        这正是本仓库反复出现的"测试看起来过了、实际没验到"的事故模式，
        故此处补齐 `nx` 并返回布尔（生产代码据其返回值判定"本轮是否已计入"）。
        """
        if nx and k in self.kv:
            return False
        self.kv[k] = v
        self.writes.append((k, v))
        return True

    def incr(self, k, amount=1):
        """【2026-09-16】"轮次结束"标记用（`hcm:state:osc_round_seq:{symbol}`）。

        ⚠ 同 `nx` 的教训：替身必须先具备生产代码用到的能力，否则新增写入会**静默
        fail-open**、套件仍报"全部通过"，而实际**一行都没验到**。
        """
        v = int(float(self.kv.get(k) or 0)) + int(amount)
        self.kv[k] = str(v)
        self.writes.append((k, str(v)))
        return v


class MockConn:
    """最小 asyncpg 替身：fetchval 返回 signal_mode；fetch 返回 K 线。"""

    def __init__(self, mode: str, klines: list | None = None) -> None:
        self.mode = mode
        self.klines = klines or []

    async def fetchval(self, sql, *a):
        return self.mode

    async def fetch(self, sql, *a):
        return self.klines


def _row(entry=100.0, sl=99.0):
    return {"symbol": "XAUUSD", "mt5_ticket": 123, "open_price": entry, "sl": sl,
            "open_time": "2026-09-15T00:00:00+00:00"}


def _flat_klines(n: int = 140):
    """平坦 K 线（high=100.5 / low=99.5 / close=100）→ ATR≈1.0。

    这样 `sl_atr = |entry − sl| / ATR = 1.0`，与兜底值 2.0 **可区分**，
    从而验证走的是真实 ATR 路径而非兜底。
    """
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 9, 15, tzinfo=timezone.utc)
    return [{"open_time": t0 + timedelta(minutes=5 * i),
             "high": 100.5, "low": 99.5, "close": 100.0} for i in range(n)]


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=== 1) 按路径加载（真实部署布局）===")
    sm, sf = PS._load_fsm_modules(LOG)
    ck("state_machine 模块可加载", sm is not None, True)
    ck("state_features 模块可加载", sf is not None, True)
    if sm is None or sf is None:
        print("\n[致命] 路径解析失败 → 回写会静默降级，**不得上线**")
        raise SystemExit(1)
    ck("apply_osc_close 可调用", callable(getattr(sm, "apply_osc_close", None)), True)

    KA = "hcm:state:osc_atr_loss:XAUUSD"
    KC = "hcm:state:osc_loss_count:XAUUSD"

    print("\n=== 2) 当前必须完全 inert（非 FSM 信号 → 零写入）===")
    for mode in ("hexp", "", "live_override"):
        rd = MockRedis()
        await PS._fsm_osc_counter_writeback(MockConn(mode), rd, LOG, _row(), 1, "sl")
        ck(f"mode={mode!r} → 零写入", rd.writes, [])

    print("\n=== 3) 裸 state_fsm（未分子模式）→ 跳过 + 告警，不写 ===")
    rd = MockRedis()
    await PS._fsm_osc_counter_writeback(MockConn("state_fsm"), rd, LOG, _row(), 1, "sl")
    ck("裸 state_fsm → 零写入", rd.writes, [])

    print("\n=== 4) state_osc 管道可用 ===")
    rd = MockRedis()
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", _flat_klines()), rd,
                                        LOG, _row(), 1, "sl")
    ck("sl → 累计 ATR（走真实 ATR=1.0，非兜底 2.0）", rd.kv.get(KA), "1.000000")
    ck("sl → 连续次数", rd.kv.get(KC), "1")

    rd2 = MockRedis()
    rd2.kv[KA], rd2.kv[KC] = "3.0", "2"
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", _flat_klines()), rd2,
                                        LOG, _row(), 1, "tp")
    ck("tp → 双计数器归零", (rd2.kv.get(KA), rd2.kv.get(KC)), ("0.000000", "0"))

    rd3 = MockRedis()
    rd3.kv[KA], rd3.kv[KC] = "1.5", "1"
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", _flat_klines()), rd3,
                                        LOG, _row(), 1, "be")
    ck("be → count 归零、atr_loss 保留", (rd3.kv.get(KA), rd3.kv.get(KC)),
       ("1.500000", "0"))

    print("\n=== 5) 兜底路径（无 K 线可还原 ATR）===")
    rd4 = MockRedis()
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", []), rd4, LOG,
                                        _row(), 1, "sl")
    ck("无 K 线 → 按兜底 2.0 计入（宁可高估）", rd4.kv.get(KA), "2.000000")

    print("\n=== 6) 【BUG-1/2/3 修复】轮次级幂等：同一 signal_id 只计一次 ===")
    # 场景：一个 FSM 信号**扇出 master + follower 两笔 ticket**，两笔都因止损平仓。
    # 修复前：每笔各 +1 ⇒ 每轮 +2 ⇒ 策略取档 `idx=min(count,3)` 只落偶数
    #         ⇒ `ladder[1]`（1.0 档 = 0.02 手）**结构性不可达**
    # （实测 2026-09-15：12 次入场 lot 只在 {0.01,0.03}，0.02 一次未现；ctx
    #   `frozen_atr_loss=6.705` 远超 4ATR 上限 ⇒ S5 提前锁止）。
    rd5 = MockRedis()
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", _flat_klines()), rd5,
                                        LOG, _row(), 20260915001, "sl")   # master
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", _flat_klines()), rd5,
                                        LOG, _row(), 20260915001, "sl")   # follower（同 signal_id）
    ck("同 signal_id 两笔 sl → count 只 +1", rd5.kv.get(KC), "1")
    ck("同 signal_id 两笔 sl → ATR 只累加一次", rd5.kv.get(KA), "1.000000")

    # **反证**：不同 signal_id 必须各自计入 —— 否则幂等键会误吞真实轮次、令锁止失效
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", _flat_klines()), rd5,
                                        LOG, _row(), 20260915002, "sl")
    ck("不同 signal_id → 正常累加（count=2）", rd5.kv.get(KC), "2")

    # BUG-3：同轮两笔归因可能不同（实测 12:57 master=`sl` / follower=`expert`）
    # ⇒ **只让 `sl` 占轮次名额**：非 sl 不消耗名额，故"先到 expert、后到 sl"仍应计入，
    #    结果与对账先后顺序无关（修复前是否计入取决于哪笔先被对账 ⇒ 手数不可复现）。
    rd6 = MockRedis()
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", _flat_klines()), rd6,
                                        LOG, _row(), 20260915003, "expert")
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", _flat_klines()), rd6,
                                        LOG, _row(), 20260915003, "sl")
    ck("同轮 expert+sl → sl 那笔仍计入", rd6.kv.get(KC), "1")

    print("\n=== 7) 【be-不解冻缺口修复】轮次标记：**无论归因**都推进 ===")
    # 规格 §7.1：止盈/止损/状态切换**任一**即结束本轮。`be`（保本离场）在
    # `apply_osc_close` 里**不计入**止损预算（这是对的：保本没有亏损）—— 但它
    # **必须**让"轮次结束"发生，否则策略侧判不出本轮结束 ⇒ **冻结箱体永久不解**
    # ⇒ 之后每根 bar 都拿过期 mid 当 TP 锚点（本次修复的真实缺口）。
    _rk = "hcm:state:osc_round_seq:XAUUSD"
    rd7 = MockRedis()
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", _flat_klines()), rd7,
                                        LOG, _row(), 20260915004, "be")
    ck("be 归因 → 止损计数器**不变**（不计入预算，符合语义）",
       (rd7.kv.get(KA), rd7.kv.get(KC)), (None, None))
    ck("be 归因 → **轮次标记仍推进**（解冻判据的关键）", rd7.kv.get(_rk), "1")
    # 反证：sl 这类归因同样要推进标记（两种语义各自独立推进）
    await PS._fsm_osc_counter_writeback(MockConn("state_osc", _flat_klines()), rd7,
                                        LOG, _row(), 20260915005, "sl")
    ck("sl 归因 → 轮次标记继续推进（累计 2）", rd7.kv.get(_rk), "2")

    print("\n" + "=" * 70)
    print(f"共 {N} 项，失败 {len(FAILED)} 项"
          + (f"：{FAILED}" if FAILED else " —— 全部通过，可以重启桥"))
    print("=" * 70)
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
