"""replay_state_chain.py — 离线回放**整条**状态机链路（影子期预演 + 不变式检查）。

链路（每一步都调用**线上同一模块**，不复制任何规则）：

    state_features.compute_indicators
      → StateInferer.infer（4 类 LGBM × 5 seeds，bagging）
      → trend_direction（方向规则模块）
      → trend_trigger（起点触发器，起点 LGBM）
      → MarketStateMachine.step（防抖 + 状态迁移 + 锁止）
      → StateStrategy.decide（状态 → 交易意图）
      → 模拟持仓（SL / 箱体 TP / 移动止损）
      → state_machine.apply_osc_close（平仓归因 → 双计数器，**与桥侧同一纯函数**）
      → 下一根 FSM 读到计数器 → S5 锁止

**目的**：在桥上线前，用**不变式断言**捕捉"各模块单测都过、拼起来却不成立"的集成缺陷。
（`verify_state_strategy_osc.py` 验的是单元语义；本脚本验的是**串联**。）

⚠ 收益口径：M5 方向符号反向问题（方案 §20）未解前，**本回放不评估收益**，
只用于链路连通性与一致性检查；报表里的 R 值仅供"是否明显异常"参考。

用法：
    python replay_state_chain.py --symbol XAUUSD --tf M5 --since 2026-09-07
    python replay_state_chain.py --model-dir <dir> --verbose
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hcm-signal-tower"))

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2"

# ── 规格 §6.3 允许的状态迁移（用于不变式 I1）──
ALLOWED = {
    "S0_IDLE": {"S0_IDLE", "S1_OSC", "S2_TREND_INIT", "S4_TREND_FADE", "S9_PAUSED"},
    "S1_OSC": {"S1_OSC", "S2_TREND_INIT", "S4_TREND_FADE", "S5_OSC_LOCKED", "S9_PAUSED"},
    "S2_TREND_INIT": {"S2_TREND_INIT", "S3_TREND_MID", "S1_OSC", "S4_TREND_FADE",
                      "S0_IDLE", "S9_PAUSED"},
    "S3_TREND_MID": {"S3_TREND_MID", "S2_TREND_INIT", "S1_OSC", "S4_TREND_FADE",
                     "S0_IDLE", "S9_PAUSED"},
    # ── 【2026-09-16 裁决 §55.5：S4 → S2 **放行**（改规格表，不改代码）】──
    # 该迁移由 **4 类 argmax 路径**产生（模型判 `trend_init`；S4 属趋势态 ⇒ 不被
    # `target==S3 且非趋势态 → S2` 那条改写 ⇒ 按 `k_enter=2` 直接落 S2）。生产实测出现过 1 次。
    # 裁定依据（为何改表而非收紧代码）：
    #   · **语义正确**：S4 = 旧趋势**衰竭**、S2 = **轻仓试错**的起点；"衰竭后新起点"正是
    #     模型 `trend_init` 的含义，且落点是 S2（不是加仓阶段的 S3）⇒ 符合试错纪律。
    #   · **收紧代码代价更大**：从 S4 出发丢弃 `trend_init` = 扔掉"新趋势起点"这个最有价值
    #     的信号；若改成落 S3 则跳过试错纪律（更差）。
    #   · **零行为变更**：该迁移本来就在发生，本次只是让规格表与代码一致 ⇒ 仅消除 I1 误报。
    # ⚠ 必须与之区分：2026-09-15 试过的是"放开 **触发器**入口从 S4 进 S2"（**另一条路径**），
    #   实测把误报从 35.3% 推到 46.2% ⇒ 已撤回。**本次放行不涉及它**：触发器路径仍被
    #   `cur not in _TREND_STATES` 挡住 S4（见 `state_machine.py#6.5`）。
    "S4_TREND_FADE": {"S4_TREND_FADE", "S1_OSC", "S2_TREND_INIT", "S3_TREND_MID",
                      "S0_IDLE", "S9_PAUSED"},
    "S5_OSC_LOCKED": {"S5_OSC_LOCKED", "S0_IDLE", "S9_PAUSED"},
    "S9_PAUSED": {"S9_PAUSED", "S0_IDLE", "S9_PAUSED"},
}
_TREND = ("S2_TREND_INIT", "S3_TREND_MID", "S4_TREND_FADE")


def _load_tool(name: str, fname: str):
    """按文件路径加载同目录工具模块。

    【为什么不复制验收门的实现】`lead_offsets` / `stat_of` / `_edges_with_gap` 是
    "漏检 / 误报 / 提前量"的**唯一定义**（`tools/eval_state_leadtime.py`）。
    若在此另写一份，两处口径会漂移 —— 正是本仓库明令禁止的事故模式
    （见 `state_features.py` 顶部"同一指标两份实现"的记录）。故直接加载复用。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fname))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {fname}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


class FakeRedis:
    is_initialized = True

    def __init__(self) -> None:
        self.kv: dict = {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, ex=None):
        self.kv[k] = v


class FakeConfig:
    """最小 async 配置提供者（回放用）——**只回放 `--cfg` 显式给的键**。

    【为什么必须有它】本脚本原先传 `config_provider=None` ⇒ `StateStrategy.load_config()`
    首行即返回、`MarketStateMachine.load_config()` 同理 ⇒ 回放跑的是**代码 DEFAULTS**，
    **测不了生产配置**（quantile / entry_confirm=2 / break_confirm=2 等一概无效）。
    那会让"默认行为逐位一致"这类结论成立、却**无法回答**"生产配置的效果如何"。

    【为什么不用"直接改实例属性"】显式 `--cfg` 经**生产同一条** `load_config()` 路径注入
    （含枚举白名单校验、非法值回退与告警），故回放验证的就是线上那条代码；
    直接赋 `strat._entry_confirm=2` 会绕过校验路径，验的就不是生产行为。

    未列出的键一律返回 `None` / `default` → 代码走"未设置"分支保留 `DEFAULTS` ✓。
    """

    is_initialized = True

    def __init__(self, kv: dict):
        self.kv: dict = {str(k).strip(): str(v).strip() for k, v in (kv or {}).items()}

    async def get(self, key, default=""):
        return self.kv[key] if key in self.kv else None

    async def get_float(self, key, default=0.0):
        if key in self.kv:
            try:
                return float(self.kv[key])
            except (TypeError, ValueError):
                return default
        return default

    async def get_int(self, key, default=0):
        return int(await self.get_float(key, float(default)))

    async def get_bool(self, key, default=False):
        if key in self.kv:
            return str(self.kv[key]).strip().lower() in ("true", "1", "yes", "on")
        return default


def parse_cfg(pairs) -> dict:
    """`["k=v", ...]` → dict；非法项直接报错退出（**不静默忽略**：静默忽略会让回放
    "看起来跑了覆盖、实际没覆盖"，正是本仓库反复出现的那类盲区）。"""
    out: dict = {}
    for p in pairs or []:
        s = str(p)
        if "=" not in s:
            raise SystemExit(f"[fatal] --cfg 需形如 KEY=VALUE，收到 {s!r}")
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            raise SystemExit(f"[fatal] --cfg 键为空：{s!r}")
        out[k] = v
    return out


class SimPosition:
    """极简持仓模拟（口径全部显式，便于审查）。

    入场：以下单那根 bar 的 **close** 成交。
    出场检查：**从下一根 bar 开始**（入场 bar 的 high/low 发生在入场之前，用它会引入前视）。
    同一根内 SL 与 TP 都触发 → **按 SL 计**（悲观口径，避免高估）。
    S1：TP = 冻结箱体中值（intent.tp_anchor），SL = 初始 k×ATR，**不移动**（规格：固定 SL 走桥会话系数）。
    S2/S3/S4：无固定 TP，用移动止损（近 N 根极值 ∓ k×ATR，只前移，S4 按 trail_mult 收紧）。
    """

    def __init__(self, k_sl: float, trail_lb: int) -> None:
        self.k_sl, self.trail_lb = float(k_sl), int(trail_lb)
        self.pos: dict | None = None
        self.closes: list[dict] = []

    def open(self, intent, i: int, atr: float, price: float) -> None:
        side = 1.0 if intent.direction == "BUY" else -1.0
        is_osc = intent.state == "S1_OSC"
        self.pos = {
            "direction": intent.direction, "entry": price, "atr": atr, "side": side,
            "sl": price - side * self.k_sl * atr,
            "tp": (float(intent.tp_anchor) if (is_osc and intent.tp_anchor) else None),
            "is_osc": is_osc, "state_at_open": intent.state, "bar_open": i,
            "trail_lb": int(intent.trail_lookback or self.trail_lb),
            # **自入场起**的最大有利偏移（锚定用）。初值 = 入场价 → 止损起点恰为
            # `entry − k×ATR`，不会一开仓就跳到市价上方。
            "max_fav": price,
        }

    def manage(self, h, l, i: int, trail_mult: float) -> dict | None:
        p = self.pos
        if p is None or i <= p["bar_open"]:
            return None
        side, atr = p["side"], p["atr"]
        if not p["is_osc"]:
            # 【口径修正 2026-09-15 · 本脚本第三版】
            # 原先用"近 N 根极值（含入场前的 bar）"锚定 → 对 S2 的**回踩入场**
            # （回踩仅 0.5×ATR）会算出 `高20 − k×ATR > 入场价` 的止损，即**多头止损被放到
            # 市价上方** —— 那不是止损而是止盈，模拟里凭空产生"稳赢"单（实测把均值 R 抬到
            # +0.41、胜率 84.6%，R 几乎全部来自 S2）。现实中 MT5 会直接拒绝这种无效止损。
            # 改为锚定 **自入场起** 的最大有利偏移：`max_fav` 初值 = 入场价 → 起步止损恰为
            # `entry − k×ATR`；只有价格真的向有利方向走，止损才随之上移（正常追踪语义）。
            # ⚠ 与规格字面"近 N 根极值"的差异见方案 §41（**桥侧实现前必须定清**）。
            if side > 0:
                p["sl"] = max(p["sl"], p["max_fav"] - self.k_sl * atr * trail_mult)
            else:
                p["sl"] = min(p["sl"], p["max_fav"] + self.k_sl * atr * trail_mult)
        # 触发判定用**本根**的极值（止损价位来自截至上一根的锚点 → 无前视）
        hit_sl = (float(l[i]) <= p["sl"]) if side > 0 else (float(h[i]) >= p["sl"])
        hit_tp = False
        if p["tp"] is not None:
            hit_tp = (float(h[i]) >= p["tp"]) if side > 0 else (float(l[i]) <= p["tp"])
        reason = "sl" if hit_sl else ("tp" if hit_tp else None)
        if reason is None:
            # 未出场 → 用本根极值推进"自入场起最大有利偏移"（放在触发判定**之后**，
            # 否则又是"用本根高点抬高止损、再被本根低点触发"的前视）
            p["max_fav"] = (max(p["max_fav"], float(h[i])) if side > 0
                            else min(p["max_fav"], float(l[i])))
            return None
        exit_px = p["sl"] if reason == "sl" else p["tp"]
        rec = {"reason": reason, "is_osc": p["is_osc"], "state": p["state_at_open"],
               "r": (exit_px - p["entry"]) * side / (self.k_sl * atr),
               "sl_atr": self.k_sl, "bar": i}
        self.closes.append(rec)
        self.pos = None
        return rec


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5")
    ap.add_argument("--since", default="2026-09-07", help="洁净窗口起点（§36）")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--atr-sl-mult", type=float, default=2.0, help="模拟持仓的初始 SL（ATR 倍数）")
    ap.add_argument("--dir-thr", type=float, default=1.0)
    ap.add_argument("--dir-k", type=int, default=3)
    # 【item 2（方案 §56）】方向**来源周期**：缺省 = 回放周期（既有行为）。
    # 跨周期时用 `trend_direction.align_last_closed` 做前视闭合对齐
    # （与线上 scheduler、离线标定 eval_trend_direction **同一函数**）。
    ap.add_argument("--dir-tf", default=None, choices=["M5", "M15", "H1"])
    # 【item 1（方案 §55）】趋势态入口是否**必须**由起点触发器确认 —— A/B 开关。
    #   -1 = 不干预（用模块默认 False，即**当前生产行为**）；0/1 = 强制。
    # 为什么用三态而非布尔：需要能"完全不干预"地跑出与生产一致的基线，
    # 否则就成了"为了做对比而先改了基线"，A/B 失去意义。
    ap.add_argument("--require-trigger", type=int, default=-1, choices=[-1, 0, 1])
    # 【保真开关】生产 `state.fsm.flat_reset_enabled=false`（模块默认，从未开启）。
    # 本参数**默认 0 = 忠实复现生产**；置 1 = 设计意图（"防卡在无持仓趋势态"）。
    # 此前脚本硬编码 `fsm._flat_reset = True` ⇒ 回放跑在与生产**不同**的 FSM 语义上，
    # 使"回放能持续入场、生产零下单"成为**不可比的对错**（伪交付根因）。
    ap.add_argument("--flat-reset", type=int, default=0, choices=[0, 1])
    # 【保真开关】生产 `state.fsm.low_conf_policy`（模块默认 "hold"）。
    #   hold  = 低置信不参与类别防抖（既有行为，进快退慢）
    #   decay = 低置信视为"退化为震荡"，照常参与防抖（与验收门 model_nohold 同义）
    # 用于 A/B：唯一变量 = 低置信语义。
    ap.add_argument("--low-conf", default="hold", choices=["hold", "decay"])
    # ── 【第四步：扇出模拟】────────────────────────────────────────────
    # 生产实况：桥是**每个账户一个独立进程**（2026-09-16 实测：master=MetaTrader 5、
    #   follower=MetaTrader1，两个 `mt5_bridge.py`），各自对本账户做平仓对账；
    #   而震荡计数器键 `hcm:state:osc_*:{symbol}` 是**品种级**
    #   ⇒ **一次入场（1 个 signal_id）触发 N 次回写**（N = 账户数）。
    # 此前本回放**每轮只调一次** `apply_osc_close` ⇒ 扇出完全没被模拟
    #   ⇒ **BUG-1（梯度跳档）/BUG-2（4ATR 预算双倍消耗）在回放里不可见**
    #   —— 这正是它们能在生产潜伏一整天而回放"全过"的根本原因。
    ap.add_argument("--fanout", type=int, default=2,
                    help="1 个 FSM 信号扇出几笔 ticket（生产实测 = 2：master+follower）")
    # 是否复刻桥侧的**轮次级幂等**（`_fsm_osc_counter_writeback` 按 signal_id 只计一次）：
    #   1（默认，= 修复后行为）：同一轮仅一笔 `sl` 计入 ⇒ 档位每轮走**一档**
    #   0（**复刻修复前行为**）：N 笔全计 ⇒ 档位每轮跳 N 档 ⇒ 用于
    #     **证明本回放确实能看见该缺陷**（I10 会失败）
    ap.add_argument("--round-idem", type=int, default=1, choices=[0, 1])
    # 洁净标签 CSV（`build_state_labels` 产物）→ 输出**验收口径**（漏检/误报/提前量）。
    # 不传则只输出链路连通性与不变式（本脚本原有职责，不变）。
    ap.add_argument("--labels", default=None,
                    help="如 _scratch/state_M5_v3.csv；给了才做验收口径对照")
    ap.add_argument("--tag", default="", help="本次运行的标签（A/B 对照用）")
    # ── 【诊断能力】配置覆盖 ──
    # 为什么必须有：本脚本此前传 `config_provider=None` ⇒ 只跑**代码 DEFAULTS**，
    #   **测不了生产配置**（quantile / entry_confirm=2 / break_confirm=2 一概无效）。
    #   于是"默认行为逐位一致"能证，"生产配置效果如何"证不了 —— 而后者才是决策依据。
    ap.add_argument("--cfg", action="append", default=[], metavar="KEY=VALUE",
                    help="策略层配置覆盖（可重复），走**生产同一条** load_config 路径。"
                         "例：--cfg osc.bands_mode=quantile "
                         "--cfg osc.entry_confirm_bars=2")
    # FSM 防抖根数覆盖（诊断"k_enter=2 遇上模型抖动"的代价）
    ap.add_argument("--k-enter", type=int, default=None)
    ap.add_argument("--k-exit", type=int, default=None)
    ap.add_argument("--k-fade", type=int, default=None)
    # 验收口径的两个参数，**默认值刻意与 eval_state_leadtime 一致**（否则同名义指标口径不同）
    ap.add_argument("--window", type=int, default=40, help="匹配上升沿的搜索窗（bar）")
    ap.add_argument("--gap", type=int, default=3, help="真值连续性容忍（bar）")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    from signal_tower.state_infer import StateInferer
    from signal_tower.state_machine import MarketStateMachine, apply_osc_close
    from signal_tower.state_machine import OSC_LOSS_COUNT_KEY_TMPL, OSC_LOSS_KEY_TMPL
    from signal_tower.state_strategy import StateStrategy

    # ── 数据 ──
    conn = psycopg2.connect(args.db_url)
    kl = pd.read_sql(
        "SELECT open_time, high, low, close FROM hcm_market.klines "
        "WHERE symbol=%s AND time_frame=%s AND open_time >= %s ORDER BY open_time",
        conn, params=(args.symbol, args.tf, args.since))
    conn.close()
    if len(kl) < 200:
        raise SystemExit(f"[fatal] 仅 {len(kl)} 根，不足以回放")
    kl["open_time"] = pd.to_datetime(kl["open_time"], utc=True)
    h = kl["high"].astype(float).to_numpy()
    l = kl["low"].astype(float).to_numpy()
    c = kl["close"].astype(float).to_numpy()
    # open_time → Unix 秒（UTC）：**必须用 `build_state_labels.epoch_s`**（全仓唯一实现）。
    # ⚠ 【2026-09-15 实测修复】此前此处手写 `//1e9`，而上游 `pd.to_datetime(..., utc=True)`
    #   实测得到 `datetime64[us, UTC]`（**微秒**）→ 结果是"千秒"：M5 **每 3~4 根 bar 共享
    #   同一个 epoch**。而 `fsm.step(bar_time=...)` 正是用 bar_time 去重（"一根 bar 只推进
    #   一次"）⇒ 回放里**每 3 根被吃掉 2 根**，防抖 k=2 实际变成 k≈6 根，且与线上语义不符
    #   （线上 `_as_epoch_s` 给的是正确秒）。此修复使回放与线上口径一致。
    _BSL = _load_tool("build_state_labels", "build_state_labels.py")
    ep = _BSL.epoch_s(kl["open_time"])
    print(f"[data] {args.symbol} {args.tf} {kl['open_time'].iloc[0]} .. "
          f"{kl['open_time'].iloc[-1]}  共 {len(kl)} 根")

    # ── 模型 ──
    # 【2026-09-16 定标能力】此前 `config_provider=None` ⇒ `state.min_conf` **恒为模块
    #   默认 0.45**，`--cfg state.min_conf=...` 完全无效 ⇒ **无法对"置信闸"做数据定标**，
    #   而它正是"51% 的 bar 直接 low_conf_skip ⇒ 状态机迁不动"的那个旋钮。
    #   改为与策略层同源（吃 `--cfg`），并打印生效值以便审计。
    _CFG = parse_cfg(args.cfg)
    inferer = StateInferer(
        config_provider=(FakeConfig(_CFG) if _CFG else None),
        model_dir=args.model_dir)
    if _CFG:
        await inferer.load_config()
    if not inferer.load_models(args.tf):
        raise SystemExit(f"[fatal] {args.tf} 模型未加载（--model-dir={args.model_dir}）："
                         "回放必须用真实模型，否则验证的不是整条链路")
    # 【必须显式启用】`StateInferer._enabled` 缺省 False（生产由 `state.enabled` 配置决定）。
    # `config_provider=None` 时 `load_config()` 直接返回 → 若此处不置 True，
    # `infer()` 会在第一行返回 `reason="disabled"`、`ok=False` → **每根 bar 都推理失败**
    # → FSM 只在 S9↔S0 摆动、策略层恒 `no_box_or_atr` → 回放"跑通但不验任何东西"。
    # 这个坑本脚本第一次运行就踩到了（1741/1741 全 fal），故显式设置并打印。
    inferer._enabled = True
    onset_ok = inferer.load_onset_models(args.tf)
    print(f"[model] 4 类模型已加载（enabled=True）；"
          f"起点模型={'已加载' if onset_ok else '缺失（触发器降级）'}")

    redis = FakeRedis()
    fsm = MarketStateMachine(config_provider=None, redis_client=redis)
    # ── 【2026-09-19 保真修复】FSM 是**唯一**没走 `load_config()` 的组件 ─────────────
    # 此前 `config_provider=None` ⇒ 回放里 FSM 只跑**代码 DEFAULTS**，`--cfg` 对它完全无效
    #   （与 StateInferer(:331-336) / StateStrategy(:383-388) 的既有修法不一致 —— 那两处
    #    早已改为"吃 --cfg"，本处遗漏）。
    # 后果：任何**后加的 FSM 配置键**（例：③-B 的 `state.fsm.non_fade_target`）都无法做 A/B，
    #   而"用同一沙盘验证生产配置"正是本脚本存在的理由。
    # 修法：与另两处同源 —— 有 `--cfg` 就走**生产同一条** `load_config()`
    #   （含枚举白名单校验与非法值回退告警）；随后下方 `--flat-reset / --low-conf /
    #   --require-trigger / --k-*` 这些**显式诊断开关**照旧覆盖。
    #   优先级不变：显式开关 > --cfg > 代码 DEFAULTS。
    if _CFG:
        fsm._config = FakeConfig(_CFG)
        await fsm.load_config()
        print(f"[cfg-fsm] 走生产 load_config 路径 → k_enter={fsm._k_enter} "
              f"k_exit={fsm._k_exit} k_fade={fsm._k_fade} "
              f"require_trigger={fsm._require_trigger} flat_reset={fsm._flat_reset} "
              f"low_conf={fsm._low_conf_policy} non_fade_target={fsm._non_fade_target!r}")
    # ── 【2026-09-15 保真修复：伪交付根因】──
    # 此前**硬编码 True**，而生产 `state.fsm.flat_reset_enabled = false`（模块默认，从未开启）
    # ⇒ 回放与生产的 FSM 语义不同，两者结论不可比：
    #     回放 true ：趋势态 + 无持仓 → **下一根立即复位 S0** → 状态自由流动、可反复入场
    #     生产 false：**无限期滞留**趋势态 → 一旦进 S4（模型 fade 先验 68.6%）就出不来
    #                （迁出需 k_fade=2 根**连续且 decided** 的非 fade，而 min_conf=0.45
    #                 让 51% 的 bar 直接 low_conf_skip 不计数）
    #                ⇒ **S4 = 禁新开** ⇒ 生产长期零下单
    # 该开关的既有设计意图（verify_state_machine_age.py 原注释）："**防止卡在无持仓的趋势态**"
    # —— 正是本病的解药。故"生产关闭它"本身即缺陷，而回放此前**替生产开了**它，
    # 把缺陷掩盖掉了。
    # 现改为 **默认 0 = 生产真值**（回放忠实复现生产）；`--flat-reset 1` 作对照。
    fsm._flat_reset = bool(args.flat_reset)
    fsm._low_conf_policy = str(args.low_conf)
    # 【item 1】A/B：-1 → 沿用模块默认（= 当前生产行为），0/1 → 强制覆盖。
    if args.require_trigger >= 0:
        fsm._require_trigger = bool(args.require_trigger)
    # 【诊断】FSM 防抖根数覆盖（量化"k_enter=2 遇上模型抖动"的代价）
    if args.k_enter is not None:
        fsm._k_enter = max(1, int(args.k_enter))
    if args.k_exit is not None:
        fsm._k_exit = max(1, int(args.k_exit))
    if args.k_fade is not None:
        fsm._k_fade = max(1, int(args.k_fade))
    print(f"[cfg] require_trigger={fsm._require_trigger} flat_reset={fsm._flat_reset} "
          f"k_enter={fsm._k_enter} k_exit={fsm._k_exit} k_fade={fsm._k_fade} "
          f"osc_limit={fsm._osc_limit} | dir_thr={args.dir_thr} dir_k={args.dir_k} "
          f"| **fanout={args.fanout}**(扇出笔数) round_idem={args.round_idem} "
          f"| **min_conf={inferer.min_conf}**(生效值，由 --cfg state.min_conf 驱动) "
          f"| tag={args.tag!r}")
    # ── 策略层配置：走**生产同一条** `load_config` 路径（含枚举白名单校验）──
    _CFG = parse_cfg(args.cfg)
    strat = StateStrategy(
        config_provider=(FakeConfig(_CFG) if _CFG else None), redis_client=redis)
    if _CFG:
        await strat.load_config([args.symbol])
        _t = strat.tuning
        print(f"[cfg-strategy] 覆盖 {len(_CFG)} 项 → bands={_t['bands_mode']}"
              f"(q={_t['q_high']}/{_t['q_low']}) buffer={_t['buffer_mode']}"
              f"({_t['buffer_pct']}) confirm={_t['entry_confirm_bars']}"
              f" tp={_t['tp_mode']} break={_t['break_confirm_bars']}"
              f" ladder={_t['ladder']}")
    else:
        print("[cfg-strategy] **无 --cfg ⇒ 跑代码 DEFAULTS**（此结论不能代表生产配置）")
    sim = SimPosition(args.atr_sl_mult, strat.tuning["trail_lookback"])

    from signal_tower import state_features as SF
    need = SF.min_bars(None)

    try:
        from signal_tower import trend_direction as TD
        from signal_tower.trend_direction import compute_direction_series, dir_name
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"[fatal] 方向模块不可用：{e}")
    _dcfg = {"state.dir.slope_thr_atr": args.dir_thr,
             "state.dir.debounce_bars": args.dir_k}
    dirs = compute_direction_series(h, l, c, ind=None, cfg=_dcfg)

    # ── 【item 2（方案 §56）】方向来源周期：缺省 = 回放周期（既有行为，零变化）──
    _dir_tf = (args.dir_tf or args.tf).upper()
    _dnames = None
    if _dir_tf != args.tf:
        conn = psycopg2.connect(args.db_url)
        try:
            kl_d = pd.read_sql(
                "SELECT open_time, high, low, close FROM hcm_market.klines "
                "WHERE symbol=%s AND time_frame=%s ORDER BY open_time",
                conn, params=(args.symbol, _dir_tf))
        finally:
            conn.close()
        if len(kl_d) < 200:
            raise SystemExit(f"[fatal] 方向周期 {_dir_tf} 数据不足（{len(kl_d)} 根）")
        kl_d["open_time"] = pd.to_datetime(kl_d["open_time"], utc=True)
        _dep = _BSL.epoch_s(kl_d["open_time"])
        _dres = compute_direction_series(kl_d["high"].astype(float).to_numpy(),
                                         kl_d["low"].astype(float).to_numpy(),
                                         kl_d["close"].astype(float).to_numpy(),
                                         ind=None, cfg=_dcfg)
        # 前视闭合对齐：**同一函数**（线上 scheduler / 离线标定 / 本回放三处共用）
        _dpos = TD.align_last_closed(_dep, _dir_tf, ep)
        _dnames = np.array([
            (str(dir_name(int(_dres["confirmed"][p])))
             if (p >= 0 and bool(_dres["valid"][p])) else "none")
            for p in _dpos.tolist()], dtype=object)
        print(f"[dir] 方向来源={_dir_tf} rows={len(_dep)} → 对齐命中 "
              f"{int((_dpos >= 0).sum())}/{len(ep)} 根（只看到**已收盘**的 {_dir_tf} bar）")
    else:
        print(f"[dir] 方向来源={_dir_tf}（与回放周期相同）")

    from signal_tower.trend_trigger import latest as _trigger_latest
    # ── 【2026-09-15 保真修复：触发器配置】──
    # 此前是 `_tcfg: dict = {}`（**恒为空**）⇒ 回放只跑 `trend_trigger` 的**模块默认值**，
    # 生产参数（`state.trigger.rise_thr=0.2948` / `use_rise` / `use_donchian` /
    # `donchian_w` / `rise_m`）**一概未被采纳**；`--cfg state.trigger.*` 也因此完全无效
    # （实测：三种互斥配置下 open/入口/误报**逐位相同**）。
    # ⇒ 历史上所有"触发器"结论都只是"默认参数"下的结论。
    # 修法：**复用 `Scheduler._load_trigger_config`**（同一实现点，不复制语义）——
    #   该类方法只依赖 `self._config`，故用 duck-typed shim 调用即可。
    _tcfg: dict = {}
    try:
        # 调度器 import 链依赖仓库根下的 `shared` 包（本脚本原只加了 hcm-signal-tower）
        _root = os.path.dirname(HERE)
        if _root not in sys.path:
            sys.path.insert(0, _root)
        import signal_tower.scheduler as _S
        _cls = next(o for o in vars(_S).values()
                    if isinstance(o, type) and hasattr(o, "_load_trigger_config"))

        class _TrigShim:      # 只为把 `_config` 喂给那份唯一实现
            def __init__(self, cfg, inferer):
                self._config = cfg
                self._trigger_cfg: dict = {}
                self._state_infer = inferer

        _shim = _TrigShim(FakeConfig(_CFG or {}), inferer)
        await _cls._load_trigger_config(_shim)
        _tcfg = dict(_shim._trigger_cfg or {})
        print(f"[cfg-trigger] 复用生产加载器 → {len(_tcfg)} 项 {_tcfg}")
        if "state.trigger.rise_thr" not in _tcfg:
            print("[cfg-trigger][warn] rise_thr 未显式给值 → 线上调用点会回落到起点模型 meta "
                  "阈值；本回放**不做该回落**，请用 `--cfg state.trigger.rise_thr=<生产值>`"
                  "显式给出（否则与生产不等价）")
    except Exception as exc:      # noqa: BLE001
        print(f"[cfg-trigger][FAIL] 无法复用生产加载器（{exc}）→ 退回空配置："
              f"**本次结论不代表生产配置**")

    # ── 统计容器 ──
    st_cnt: Counter = Counter()
    trans: Counter = Counter()
    reasons: Counter = Counter()
    infer_reasons: Counter = Counter()
    inv: dict[str, int] = defaultdict(int)
    add_in_round = 0
    prev_state = "S0_IDLE"
    max_adds = strat.tuning["max_adds"]
    osc_limit = 4.0
    trades_r: list[float] = []
    s5_hits = 0
    in_s5_runs = 0
    # 【第四步】扇出模拟账面：已止损**轮次**数（用于新不变式 I10）
    osc_rounds = 0
    # 【item 1】趋势态 ON 序列（验收口径的"检测器"）+ 入口归因。
    # 为什么入口要单独归因：`require_trigger=true` **只会**拦掉"触发器没响却仍进趋势态"
    # 的那些入口（来自 4 类 argmax 推导）。故必须能逐条看出：拦掉的是哪些、当时方向如何、
    # 模型判的是什么 —— 否则只能看到"占比变了"，无法判断变化是好是坏。
    trend_on = np.zeros(len(h), dtype=bool)
    entry_rows: list[dict] = []

    for i in range(need - 1, len(h)):
        # 0) 先管理已有持仓（用**本根**的 high/low；入场 bar 跳过）
        rec = sim.manage(h, l, i, trail_mult=strat.tuning["fade_trail_mult"])
        if rec is not None:
            trades_r.append(rec["r"])
            if rec["is_osc"]:
                # ── 【第四步：忠实模拟**扇出**】─────────────────────────────────
                # 与桥侧 `_fsm_osc_counter_writeback` 用**同一个纯函数**（语义不复制）；
                # 但**调用次数**必须与生产一致：桥是每账户一个进程，各自对本账户做
                # 平仓对账（实测 master=MetaTrader 5 / follower=MetaTrader1 两个进程），
                # 而键是**品种级** ⇒ 一次入场触发 `--fanout` 次回写。
                # 此前本处**只调一次** ⇒ 扇出没被模拟 ⇒ BUG-1/2 不可见。
                ka, kc = (OSC_LOSS_KEY_TMPL.format(symbol=args.symbol),
                          OSC_LOSS_COUNT_KEY_TMPL.format(symbol=args.symbol))
                _is_sl = (str(rec["reason"]).strip().lower() == "sl")
                _counted = False        # 本轮是否已有一笔计入（复刻桥侧轮次级幂等）
                # 【2026-09-16】**外部清零检测**：`hcm:state:osc_loss_count` 还可能被
                # **FSM 自己**清零而**不经过平仓** —— `_clear_osc_counters`（人工复位 /
                # 规格 9.4「S5 清锁时归零计数」）。此时键会**变小**，期望值必须同步归零。
                # 实测（I10 第一版的失配现场）：`bar=1058` 后键由 3 掉到 0 且中间**无平仓**
                #   ⇒ `bar=1092` 报 `key=1 期望=4`、`bar=1250` 报 `key=2 期望=5`
                #   ⇒ **偏移恒 +3 = 少认了一次清零**（= 把合法清零误判为缺陷）。
                # 为什么用"键是否变小"而不是枚举清零调用点：清零点会随规格增删，而
                #   "键变小必有外部清零"是**不变量** ⇒ 单一直值，不会漏。
                _kc_before = int(float(redis.kv.get(kc) or 0))
                if _kc_before < osc_rounds:
                    osc_rounds = _kc_before
                for _t in range(max(1, int(args.fanout))):
                    if _is_sl and args.round_idem and _counted:
                        break           # 同一 signal_id 只计一次（= 桥修复后的语义）
                    cur_a = float(redis.kv.get(ka) or 0.0)
                    cur_c = int(float(redis.kv.get(kc) or 0))
                    na, nc = apply_osc_close(rec["reason"], rec["sl_atr"], cur_a, cur_c)
                    redis.kv[ka], redis.kv[kc] = f"{na:.6f}", str(nc)
                    if _is_sl:
                        _counted = True
                _r = str(rec["reason"]).strip().lower()
                if _is_sl:
                    osc_rounds += 1
                else:
                    # 【2026-09-21 语义变更同步】`apply_osc_close` 现在对**任何非 `sl` 归因**
                    # （`tp`/`be`/`manual`/`expert`/`stop_out`）都把 `count` 归零 ——
                    # `count` 的语义是「**连续**止损次数」，非 `sl` 平仓即打断"连续"。
                    # 旧实现**仅 `tp` 归零**；而生产止盈由桥侧主动平仓实现 ⇒ 归因是 `expert`
                    # ⇒ `tp` 路径**实际不可达** ⇒ `count` 单调不减、档位长期顶格（实测 0.03）。
                    # 期望值必须同步归零，否则会把**合法重置**误判为缺陷
                    # （与 I10 第一版把 `tp` 的合法清零误报为缺陷属同型错误）。
                    osc_rounds = 0
                # 【新不变式 I10】轮次计数一致：`count` 必须等于"上次归零以来的止损轮次数"。
                # 若桥侧幂等被改坏（回到"每笔 ticket 各 +1"）⇒ 此处立刻失败
                # —— 这就是"让回放能看见 BUG-1/2"的判定点。
                if int(float(redis.kv.get(kc) or 0)) != osc_rounds:
                    inv["osc_round_mismatch"] += 1
                    if args.verbose:
                        print(f"  [I10] bar={i} reason={rec['reason']!r} "
                              f"key_count={redis.kv.get(kc)} 期望={osc_rounds} "
                              f"(is_osc={rec['is_osc']})")
            if args.verbose:
                print(f"  bar={i} 平仓 reason={rec['reason']} R={rec['r']:+.2f} "
                      f"({rec['state']}{'·震荡计数' if rec['is_osc'] else '·趋势不计入'})")

        # 1) 推理（真实模型）
        infer = inferer.infer(args.tf, h[:i + 1], l[:i + 1], c[:i + 1], ep[:i + 1])
        infer_reasons[infer.reason] += 1
        if not infer.ok:
            inv["infer_not_ok"] += 1

        # 2) 方向 / 触发器（失败降级，与调度器一致）
        dname = (_dnames[i] if _dnames is not None
                 else (str(dir_name(int(dirs["confirmed"][i])))
                       if bool(dirs["valid"][i]) else "none"))
        trg_on = False
        try:
            tail = inferer.infer_onset_tail(args.tf, h[:i + 1], l[:i + 1], c[:i + 1], tail=8)
            if tail:
                trg_on = bool(_trigger_latest(h[:i + 1], l[:i + 1], c[:i + 1],
                                              tail, cfg=_tcfg).get("on"))
        except Exception:  # noqa: BLE001
            inv["trigger_error"] += 1

        # 3) FSM
        pos = 1 if sim.pos is not None else 0
        dec = await fsm.step(args.symbol, args.tf, infer, positions_open=pos,
                             paused=False, bar_time=str(int(ep[i])),
                             trigger_on=trg_on, direction=dname)
        st_cnt[dec.state] += 1
        trans[(dec.prev_state, dec.state)] += 1
        # 【item 1】记趋势态 ON 序列 + 入口归因（note/触发器/方向/模型类别）
        trend_on[i] = dec.state in _TREND
        if dec.state in _TREND and dec.prev_state not in _TREND:
            entry_rows.append({
                "bar": int(i), "note": str(dec.note), "trg": bool(trg_on),
                "dir": str(dname), "pred": str(getattr(infer, "state", "") or ""),
                "state": dec.state,
            })
        if dec.state not in ALLOWED.get(dec.prev_state, set()):
            inv["illegal_transition"] += 1
            if args.verbose:
                print(f"  [I1] bar={i} 非法迁移 {dec.prev_state} → {dec.state} ({dec.note})")
        prev_state = dec.state

        # 4) 策略意图
        atr = float(infer.feats.get("atr_14", 0.0) or 0.0)
        intent = await strat.decide(
            args.symbol, dec.state, high=h[:i + 1], low=l[:i + 1], close=c[:i + 1],
            atr=atr, slope=float(infer.feats.get("slope_linreg", 0.0) or 0.0),
            positions_open=pos, hold_only=dec.hold_only,
            direction=dec.direction or dname, age_bars=dec.age_bars,
            position_dir=(sim.pos["direction"] if sim.pos else ""))
        reasons[intent.reason] += 1
        if intent.action == "open":
            inv["open_intents"] += 1
        elif intent.action == "add":
            inv["add_intents"] += 1

        # ── 不变式 ──
        # I2：S4/S5/S9 不得产生 open/add
        if dec.state in ("S4_TREND_FADE", "S5_OSC_LOCKED", "S9_PAUSED") \
                and intent.action in ("open", "add"):
            inv["S4S5S9_order"] += 1
        # I3：S1 开仓必须触及边界
        if intent.action == "open" and intent.state == "S1_OSC":
            tol = strat.tuning["border_tol_atr"] * atr
            if not (c[i] <= intent.box_lower + tol or c[i] >= intent.box_upper - tol):
                inv["osc_open_inside_box"] += 1
            if (intent.box_upper - intent.box_lower) < strat.tuning["box_min_width_atr"] * atr:
                inv["osc_open_narrow_box"] += 1
        # I4：趋势轮次内加仓次数 ≤ max_adds
        if intent.action == "add":
            add_in_round += 1
            if add_in_round > max_adds:
                inv["add_over_max"] += 1
        # 轮次边界 = **持仓归零**（而非"离开趋势态"）：规格 10.2 的加仓上限是"每轮每仓"，
        # 而策略层的 `add_count` 在"无持仓时的 S3 首建"处归零 → 两者的轮次定义必须一致。
        # （第一版按"离开趋势态"重置，与策略层不一致 → 误报 I4 一次，已更正。）
        if pos == 0 or dec.state not in _TREND:
            add_in_round = 0
        # I5/I6：S5 出现时必须累计 ≥ limit；S5 期间不得有 S1 开仓意图
        if dec.state == "S5_OSC_LOCKED":
            s5_hits += (1 if dec.prev_state != "S5_OSC_LOCKED" else 0)
            in_s5_runs += 1
            acc = float(redis.kv.get(OSC_LOSS_KEY_TMPL.format(symbol=args.symbol)) or 0.0)
            if acc < osc_limit:
                inv["s5_below_limit"] += 1
            if intent.state == "S1_OSC" and intent.action in ("open", "add"):
                inv["s1_order_in_s5"] += 1
        # I7：清理——离开趋势态且无仓时不该残留锁定方向
        if dec.state not in _TREND and pos == 0:
            ctx = await strat.get_ctx(args.symbol)
            if ctx and (ctx.trend_dir or ctx.add_count):
                inv["stale_trend_ctx"] += 1

        # 5) 下单（模拟）
        if intent.action in ("open", "add") and atr > 0.0:
            sim.open(intent, i, atr, float(c[i]))
            if args.verbose:
                print(f"  bar={i} {dec.state} {intent.action} {intent.direction} "
                      f"({intent.reason}) tp={intent.tp_anchor}")

    # ── 报表 ──
    tot = sum(st_cnt.values()) or 1
    print("\n=========== 状态占比 ===========")
    for k_, v in sorted(st_cnt.items()):
        print(f"  {k_:<16}{v:>6}  {v / tot:6.1%}")

    print("\n=========== 状态迁移次数（前 12）===========")
    for (a_, b_), v in trans.most_common(12):
        flag = "" if b_ in ALLOWED.get(a_, set()) else "  ← 非法"
        print(f"  {a_:<16} → {b_:<16}{v:>5}{flag}")

    print("\n=========== 意图 reason 分布（前 12）===========")
    for k_, v in reasons.most_common(12):
        print(f"  {k_:<34}{v:>6}  {v / tot:6.1%}")

    n_tr = len(trades_r)
    wins = sum(1 for r in trades_r if r > 0)
    print(f"\n=========== 模拟成交（⚠ 仅供链路检查，非收益评估）===========")
    print(f"  成交 {n_tr} 笔；胜率 {wins / n_tr:.1%}" if n_tr else "  无成交")
    if n_tr:
        print(f"  累计 R = {sum(trades_r):+.2f}；均值 R = {np.mean(trades_r):+.3f}")
        by_state: dict = defaultdict(list)
        for rec_ in sim.closes:
            by_state[rec_["state"]].append(rec_["r"])
        print("  分状态拆解（R 相对**初始风险** 2ATR；用于定位 R 来源，非收益结论）：")
        for st_, rs in sorted(by_state.items()):
            rs_a = np.asarray(rs, dtype=float)
            print(f"    {st_:<16} n={len(rs_a):<4} 均值R={rs_a.mean():+.3f} "
                  f"胜率={float((rs_a > 0).mean()):.1%} 累计={rs_a.sum():+.1f}")
        print("  ⚠ 已知简化（**因此 R 不能当业绩**）：无点差/手续费/滑点；无冷却与"
              "重复开仓限制；服务端 SL/TP 用简化模型而非桥的会话系数；"
              "单持仓、按 bar 收盘成交。")
    print(f"  S5 锁止进入次数 = {s5_hits}；S5 停留 bar 数 = {in_s5_runs}")
    print(f"  最终计数器：osc_atr_loss="
          f"{float(redis.kv.get(OSC_LOSS_KEY_TMPL.format(symbol=args.symbol)) or 0.0):.3f}  "
          f"count={redis.kv.get(OSC_LOSS_COUNT_KEY_TMPL.format(symbol=args.symbol)) or 0}")

    print("\n=========== 推理 reason 分布（前 6）===========")
    for k_, v in infer_reasons.most_common(6):
        print(f"  {k_:<24}{v:>6}  {v / tot:6.1%}")

    # ── 链路连通性：**必须有东西真的跑起来**（第一版缺了这组检查，导致
    #    "全链 1741/1741 推理失败、状态恒 S0/S9"却报了"全部通过"——教训记在此）──
    bars = sum(st_cnt.values()) or 1
    ok_ratio = 1.0 - inv.get("infer_not_ok", 0) / bars
    entered = sorted(s for s in st_cnt if s not in ("S0_IDLE", "S9_PAUSED"))
    print("\n=========== 链路连通性 ===========")
    conn_checks = [
        ("推理成功率 ≥ 90%", ok_ratio >= 0.90, f"{ok_ratio:.1%}"),
        ("进入过趋势/震荡态", len(entered) > 0, ",".join(entered) or "无"),
        ("产生过开仓意图", inv.get("open_intents", 0) > 0, str(inv.get("open_intents", 0))),
        ("意图非 no_box_or_atr ≥ 50%",
         1.0 - reasons.get("no_box_or_atr", 0) / bars >= 0.50,
         f"{1.0 - reasons.get('no_box_or_atr', 0) / bars:.1%}"),
    ]
    conn_bad = 0
    for name, ok, detail in conn_checks:
        if not ok:
            conn_bad += 1
        print(f"  {'OK  ' if ok else 'FAIL'} {name:<26} {detail}")

    if inv.get("illegal_transition", 0):
        print("\n  【非法迁移逐条】（规格 §6.3 允许表之外的迁移）")
        for (a_, b_), v in trans.most_common():
            if b_ not in ALLOWED.get(a_, set()):
                print(f"    {a_} → {b_}  ×{v}")

    # ── 【item 1】趋势态入口归因 ──
    # `require_trigger=true` **只会**拦掉"触发器没响却仍进趋势态"的入口（即来自 4 类
    # argmax 推导的那条路径，§21 实测漏检 20%/误报 67.8%/中位**滞后 +3.5**）。
    # 故必须逐条看出：拦掉的是哪些、当时方向如何、模型判的是什么类别。
    tr_bars = int(trend_on.sum())
    # **关键口径**：`require_trigger` 只作用于 `target ∈ {S2, S3}` 的入口（`decide` 的 8.5 门）。
    # 进入 **S4（趋势衰竭）的入口不受它约束** —— 若把 S4 入口也算作"会被拦掉"，
    # 会**高估**该开关的作用面（本脚本第一版正是如此，故此处显式拆开）。
    _gate = [r for r in entry_rows if r["state"] in ("S2_TREND_INIT", "S3_TREND_MID")]
    _gate_ntrg = [r for r in _gate if not r["trg"]]
    _s4_ntrg = [r for r in entry_rows
                if r["state"] == "S4_TREND_FADE" and not r["trg"]]
    print("\n=========== 趋势态入口归因（item 1）===========")
    print(f"  趋势态占用 = {tr_bars}/{len(h)} = {tr_bars / max(1, len(h)):.1%}")
    print(f"  入口次数 = {len(entry_rows)}（→S2/S3 = {len(_gate)}；→S4 = "
          f"{len(entry_rows) - len(_gate)}）")
    print(f"  **受 `require_trigger` 约束的入口（→S2/S3）= {len(_gate)}**，"
          f"其中触发器未响 = {len(_gate_ntrg)} ← **这才是该开关会拦掉的量**")
    print(f"  （→S4 的入口不受该开关约束；其中触发器未响 {len(_s4_ntrg)}"
          f" —— 列出以免高估作用面）")
    if entry_rows:
        print("  全部入口按 note：")
        for n_, v in Counter(r["note"] for r in entry_rows).most_common():
            print(f"    {n_:<26}{v:>4}")
    if _gate_ntrg:
        print("  会被拦掉的入口（→S2/S3 且触发器未响）：")
        for n_, v in Counter(r["note"] for r in _gate_ntrg).most_common():
            print(f"    {n_:<26}{v:>4}")
        print("    方向分布：" + ", ".join(
            f"{k}={v}" for k, v in Counter(r["dir"] for r in _gate_ntrg).most_common()))
        print("    模型类别：" + ", ".join(
            f"{k}={v}" for k, v in Counter(r["pred"] for r in _gate_ntrg).most_common()))

    # ── 【item 1】验收口径对照（与 eval_state_leadtime **同一函数**）──
    if args.labels:
        E = _load_tool("eval_state_leadtime", "eval_state_leadtime.py")
        lbl = pd.read_csv(args.labels)
        lbl = lbl[lbl["label_id"].notna()].copy()
        lbl["label_id"] = lbl["label_id"].astype(int)
        lbl = lbl.sort_values("open_time").reset_index(drop=True)
        ep_l = E.epoch_s(lbl["open_time"])
        pos_l = np.searchsorted(ep, ep_l)
        np.clip(pos_l, 0, len(ep) - 1, out=pos_l)
        # 只保留**落在本次回放窗口内**（且指标已就绪）的标注行：窗口外没有 det，
        # 掺进来会把它们算成"漏检"，使结论虚高。
        inside = (ep[pos_l] == ep_l) & (pos_l >= need - 1)
        y = lbl["label_id"].to_numpy()[inside]
        gt = y != 0
        det = trend_on[pos_l[inside]]
        gt_edges = E._edges_with_gap(gt, args.gap)
        st = E.stat_of(det, gt, gt_edges, args.window)
        print("\n=========== 验收口径对照（真值 = 趋势段上升沿）===========")
        print(f"  [data] {args.labels}：窗口内标注 {int(inside.sum())} 行，"
              f"窗口外 {int((~inside).sum())} 行（已排除）")
        print(f"  [口径] 真值=label_id!=0；window={args.window} gap={args.gap}；"
              f"函数来自 eval_state_leadtime（单一真值）")
        print(E.fmt_stat("FSM趋势态", st))
        print("  参照（§51.6 验收门同口径，真值事件数不同不可直接比绝对值，看**量级**）：")
        print("    donchian(基线)  漏检  10 / 误报 10.1% / 中位  0.0")
        print("    model           漏检 324 / 误报 96.1% / 中位 +1.0（滞后）")

    print("\n=========== 不变式检查 ===========")
    checks = [
        ("I1 状态迁移合法", "illegal_transition"),
        ("I2 S4/S5/S9 不下单", "S4S5S9_order"),
        ("I3 S1 只在箱体边界开仓", "osc_open_inside_box"),
        ("I3b S1 不开窄箱", "osc_open_narrow_box"),
        ("I4 加仓 ≤ max_adds", "add_over_max"),
        ("I5 S5 时累计 ≥ 4ATR", "s5_below_limit"),
        ("I6 S5 期间无 S1 开仓", "s1_order_in_s5"),
        ("I7 无残留趋势上下文", "stale_trend_ctx"),
        ("I8 推理失败次数", "infer_not_ok"),
        ("I9 触发器异常次数", "trigger_error"),
        # 【第四步】扇出类缺陷的判定点：一次止损轮次必须只让 count +1。
        # 桥侧"每账户一个进程各回写一次"是既成事实；若忘了按 signal_id 归一轮次，
        # 这里会失败 —— 而**在此之前回放对这类缺陷完全无感**（漏检 BUG-1/2 一天）。
        ("I10 轮次计数一致（扇出未双计）", "osc_round_mismatch"),
    ]
    bad = 0
    for name, key in checks:
        v = inv.get(key, 0)
        # I8/I9 是"计数型"，非零不等于违规（允许少量环境性失败）
        # 【第四步】`osc_round_mismatch`（I10）**已升级为硬闸**（2026-09-16 完成）：
        #   · 外部清零路径已建模（用"键是否变小"判定 FSM 的 `_clear_osc_counters`，
        #     不枚举调用点）⇒ "轮次幂等"语义下实测应为 **0 次**。
        #   · 判别力保留：`--round-idem 0`（复刻"每笔 ticket 各 +1"）时**必然失败** ⇒
        #     这类"扇出双计"缺陷以后在回放里就能被抓住（此前潜伏一整天而无感）。
        #   · 若本闸失败：说明"计数器数值"与"实际止损轮次"脱钩 —— 直接对应生产里
        #     "梯度跳档 / 4ATR 预算被放大"那一类事故，**必须先查桥侧回写再放开**。
        soft = key in ("infer_not_ok", "trigger_error")
        ok = (v == 0) if not soft else True
        if not ok:
            bad += 1
        print(f"  {'OK  ' if ok else 'FAIL'} {name:<26} 次数={v}"
              + ("（计数型，非零可接受）" if soft else ""))
    # 【item 1】一行式 A/B 摘要：两次运行（require_trigger=0/1）直接对比这一行即可。
    print(f"\n[A/B 摘要] tag={args.tag!r} require_trigger={fsm._require_trigger} "
          f"趋势态占比={tr_bars / max(1, len(h)):.1%} 入口={len(entry_rows)} "
          f"→S2S3={len(_gate)}(触发器未响{len(_gate_ntrg)}) "
          f"open={inv.get('open_intents', 0)} "
          f"add={inv.get('add_intents', 0)} 非法迁移={inv.get('illegal_transition', 0)} "
          f"违反I2/I3/I3b/I4={inv.get('S4S5S9_order', 0)}/"
          f"{inv.get('osc_open_inside_box', 0)}/{inv.get('osc_open_narrow_box', 0)}/"
          f"{inv.get('add_over_max', 0)}")
    print("\n" + "=" * 70)
    total_bad = bad + conn_bad
    print("链路验证：" + ("连通性 + 不变式全部通过"
                        if total_bad == 0
                        else f"{total_bad} 项失败（连通性 {conn_bad} / 不变式 {bad}）"))
    print("=" * 70)
    if total_bad:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
