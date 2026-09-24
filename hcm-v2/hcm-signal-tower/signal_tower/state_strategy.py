"""state_strategy.py — 行情状态机·策略层（状态 → 交易意图）。

依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §7（策略规则）§18（Phase C 变更说明）

职责边界（**关键**）：
  · 本模块只产出**意图**（StrategyIntent）：状态 → 是否开仓/加仓、方向、手数倍率、箱体锚点。
  · **不计算 SL/TP 数值**：SL/TP 一律由桥按「平仓配置」的时段系数
    （`close.<session>.trailing_stop_distance` / `tp_atr_multiplier` 等）计算 —— 用户 2026-09-14 决策。
    信号塔只透传"箱体中值"作为 TP 锚点（既有 `tp_price` 字段）与 `sl_locked` 豁免标记。
  · **不直接下单**：是否真下单由调用方的 `state.order_enabled` 决定（默认 False = 纯观测）。

【两个"箱体"的区别（刻意如此，各自单一实现点）】
  1. **特征箱体**（inclusive，含当前 bar）：`state_features.compute_features_at` 产出，
     喂模型用（归一化为 ATR 距离）。**不用于触价判定**。
  2. **入场箱体**（exclusive，截至上一根）：本模块 `compute_entry_box` 产出。
     用于"价格触及边界"判定 —— 若含当前 bar，则 `close >= min(low)` 恒成立，
     `close <= box_lower` 永不触发（这是方案 §7.1 记录的箱体滑动陷阱）。
  两者语义不同、不可互相替代，故各有一处定义，不存在重复实现。

配置键（生产以 PG/Redis 为准，下列仅兜底）：
  state.order_enabled        是否真下单（**默认 False = 只算意图**）
  state.box.window.{symbol}  入场箱体回看根数（品种级；缺省用 state.box.window）
  state.trend.slope_window   趋势方向线性回归窗口
  state.trend.pullback_atr   顺势"回踩/反弹"深度阈值（ATR 倍数）
  state.trend.pullback_window 回踩判定回看根数（0 = 跟随箱体窗口）
  state.trend_max_adds       S3 最大加仓次数
  state.trend.trail_lookback 移动止损"近 N 根极值"的 N（与 slope_window 解耦）
  state.trend.fade_trail_mult S4 移动止损收紧系数（<1.0 = 收紧；0.5 = 距离减半）
  state.osc_lot_ladder       震荡梯度手数倍率（逗号分隔，按连续止损次数取）
  state.osc_border_tol_atr   边界容差（ATR 倍数，允许"附近"触及）
  state.osc_box_min_width_atr 箱体最小宽度（ATR 倍数；窄于此视为无效箱体，不开仓）
  state.trend.entry_mode     首次入场模式：close_check（既有的"回踩到位后市价"）
                             | zone_touch（新增的"回调触价"，见方案 §49）
  state.trend.entry_wait_sec 触价入场最长等待秒数（0 = 不下发触价，等价 close_check）

【震荡风控计数器：单一真值在两个 Redis 键，**由桥写入、本模块只读**】
  hcm:state:osc_atr_loss:{symbol}   累计震荡止损（ATR 倍数）→ FSM 读；≥ `state.osc_atr_loss_limit`
                                    则 S5 锁止（规格 9.4「4ATR 防爆仓」）
  hcm:state:osc_loss_count:{symbol} 震荡连续止损**次数** → 本模块读；决定梯度手数档位
  为什么不由本模块写：本模块是纯计算/意图层，重启即丢；而**平仓事件发生在桥侧**，
  只有桥能拿到成交归因（`tools/position_sync.py:_infer_close_reason` → sl/tp/be）。
  两处各写一份 = 双真值漂移（本仓库红线）。
  **接线状态（2026-09-15 已接线并部署，方案 §42/§48）**：桥侧 `tools/position_sync.py`
  的平仓归因处已写入这两个键（`_fsm_osc_counter_writeback`），桥已于 12:54 重启生效
  ⇒ S5 锁止与梯度手数**已具备生效条件**。但仍需 `state.order_enabled=true` 且出现真实
  `state*` 信号才会被触发（当前生产 0 条）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

# 【2026-09-15 §57】箱体分位数模式需要分位计算。此前本模块刻意不依赖 numpy（只用 max/min），
# 但分位数不能用 max/min 表达，且**不应另写一份分位实现**（本仓库红线：同一规则两份实现）
# → 直接复用 numpy 的 percentile（与 `state_features` 计算分位类特征同一原语）。
import numpy as np

logger = logging.getLogger(__name__)

CTX_KEY_TMPL = "hcm:state:ctx:{symbol}"
OSC_LOSS_COUNT_KEY_TMPL = "hcm:state:osc_loss_count:{symbol}"
OSC_LOSS_KEY_TMPL = "hcm:state:osc_atr_loss:{symbol}"
# 【2026-09-16】"轮次结束"**显式标记**（桥每次 FSM 震荡单平仓 INCR，**无论归因**）。
# 与上面两个计数器**刻意分开**：那两个表达"用掉多少止损预算"（故 `be` 不计入是对的），
# 本键表达"这一轮结束了"（`be` 也是结束）。详见 `StrategyContext.frozen_round_seq` 注释。
OSC_ROUND_SEQ_KEY_TMPL = "hcm:state:osc_round_seq:{symbol}"

# 趋势态集合（与 FSM 的 S2/S3/S4 对应）：用于"趋势轮次"生命周期判定
TREND_STATES = ("S2_TREND_INIT", "S3_TREND_MID", "S4_TREND_FADE")

# 【C4 2026-09-17】震荡止损预算（规格 9.4「4ATR 防爆仓」）"平完即清零"的**作用域**。
# 为什么含 S5：S5 的本意是"**预算已用尽**"的锁止，而非时间冷却；持仓全平后预算即应
# 归零、手数梯度回第一档，否则 `osc_atr_loss`/`osc_loss_count` 只增不减、档位长期顶格。
# 注意：**不含 S2/S3/S4**（趋势态不消费该预算；且趋势态下平仓不应干扰震荡预算语义）。
OSC_BUDGET_STATES = ("S0_IDLE", "S1_OSC", "S5_OSC_LOCKED")

# ══════════════════════════════════════════════════════════════════════════
# 【2026-09-15 用户要求】把**触发下单信号的信息**写入 MT5 magic
#
# 背景：此前 FSM 单的 magic 只有裸 `61`（state_osc）/`62`（state_trend），
#   终端里**看不出是哪个状态、哪个规则触发的**，只能回查 DB。
#
# 布局（**十进制、终端里可直读**，非位域；沿用本仓库 `ticket*100000+seq` 的可读风格）：
#
#     magic = LL SS RR TT         共 8 位
#       LL = 逻辑码   61=state_osc / 62=state_trend
#       SS = FSM 状态 01=S1_OSC 02=S2_TREND_INIT 03=S3_TREND_MID 04=S4_TREND_FADE 00=未知
#       RR = 触发原因 见 `MAGIC_REASON_CODES`（01=触下沿、02=触上沿、11/12=趋势首建…）
#       TT = 手数梯度档位 00..03（连续止损次数档，对应 `osc_lot_ladder` 的下标）
#     例：`61010100` = state_osc · S1_OSC · 触下沿 · 梯度档 0
#
# 【为什么这是**永久契约**，必须一次定对】MT5 的 magic 写在**已成交的订单/持仓**上，
#   事后**改不回来**（改也只能改新单）。故：
#     · 布局只允许**追加低位数**扩展，不允许改前导位；
#     · `is_fsm_magic` **必须向后兼容**裸 61/62（历史单仍在库里/终端里）。
#
# 【为什么放本模块而不是 signal_publisher】本模块拥有 `StrategyIntent.reason`
#   （即"触发原因"的真源）；且桥可以按**已验证的** importlib 路径加载本模块
#   （先例：`tools/position_sync.py` 按路径加载 `state_machine`），无需新增文件、
#   无需改 compose 挂载。`signal_publisher.SIGNAL_MODE_MAGIC` 仍是"基码"的单一真源，
#   本模块的 `MAGIC_LOGIC_*` 与之**必须相等**（下方有自检常量）。
# ══════════════════════════════════════════════════════════════════════════
MAGIC_LOGIC_OSC = 61            # == signal_publisher.SIGNAL_MODE_MAGIC["state_osc"]
MAGIC_LOGIC_TREND = 62          # == signal_publisher.SIGNAL_MODE_MAGIC["state_trend"]
_MAGIC_MUL = 100               # 每段的位宽（十进制 2 位/段）

# 触发原因 → 2 位码。**只登记会真正下单的 reason**（magic 只在下单时产生），
# 其余一律落 99（"其它"）—— 这样新增 reason 不会导致 magic 语义漂移。
MAGIC_REASON_CODES: dict = {
    "osc_at_box_lower": 1,
    "osc_at_box_upper": 2,
    "osc_martingale_sl": 3,     # 【马丁补仓 2026-09-18】止损后同向补下一档
    "init_pullback": 11,
    "init_touch_wait": 12,              # L4 触价入场（S2）
    "mid_initial_entry": 21,
    "mid_initial_touch_wait": 22,       # L4 触价入场（S3 无仓首建）
    "mid_add_on_pullback": 23,
}
MAGIC_REASON_OTHER = 99

_STATE_CODES: dict = {
    "S1_OSC": 1, "S2_TREND_INIT": 2, "S3_TREND_MID": 3, "S4_TREND_FADE": 4,
    # 【2026-09-17 C】S0_IDLE 现在也允许出箱体单（开关 `state.osc_in_idle`）⇒ 给它一个
    # 明确状态码，便于终端/归因辨识。0 仍表示"未知/其它"（既有语义不变）；本项为**追加**，
    # 不影响任何历史 magic 的编码含义。
    "S0_IDLE": 5,
}


def encode_fsm_magic(signal_mode: str, reason: str = "", state: str = "",
                     tier: int = 0) -> int:
    """把一个 FSM 下单意图编码为 MT5 magic（**唯一实现点**）。

    Args:
        signal_mode: `state_osc` / `state_trend`（决定逻辑码；未知 → 返回 0，不臆造）。
        reason: 触发原因（`StrategyIntent.reason`），映射到 2 位码，未知落 99。
        state: FSM 状态（`StrategyIntent.state`），映射到 2 位码，未知落 0。
        tier: 手数梯度档位（0..3）。

    Returns:
        8 位整数 magic；`signal_mode` 非 FSM 子模式时返回 **0**（调用方据 0 走既有路径）。
    """
    m = str(signal_mode or "").strip().lower()
    if m == "state_osc":
        logic = MAGIC_LOGIC_OSC
    elif m == "state_trend":
        logic = MAGIC_LOGIC_TREND
    else:
        return 0
    _rr = int(MAGIC_REASON_CODES.get(str(reason or "").strip(), MAGIC_REASON_OTHER))
    _ss = int(_STATE_CODES.get(str(state or "").strip(), 0))
    _tt = min(max(int(tier or 0), 0), 99)
    return ((logic * _MAGIC_MUL + _ss) * _MAGIC_MUL + _rr) * _MAGIC_MUL + _tt


def is_fsm_magic(magic: Any) -> bool:
    """是否 FSM 单的 magic —— **向后兼容裸 61/62**（历史单用的是旧格式）。

    放在本模块的理由：桥侧必须能判"这是我的仓吗"（④移动止损分支与 §57 离场指令
    的 scope 匹配都依赖它）。若桥自行写一份前缀判断，就是同一规则两份实现。
    """
    try:
        v = int(magic or 0)
    except (TypeError, ValueError):
        return False
    if v in (MAGIC_LOGIC_OSC, MAGIC_LOGIC_TREND):        # 旧格式（裸 61/62）
        return True
    return (v // (100 ** 3)) in (MAGIC_LOGIC_OSC, MAGIC_LOGIC_TREND)   # 新格式前导逻辑码


def fsm_magic_logic(magic: Any) -> str:
    """magic → `"osc"` / `"trend"` / `""`（非 FSM）。用于 §57 离场指令的 scope 匹配。"""
    try:
        v = int(magic or 0)
    except (TypeError, ValueError):
        return ""
    if v == MAGIC_LOGIC_OSC:
        return "osc"
    if v == MAGIC_LOGIC_TREND:
        return "trend"
    lead = v // (100 ** 3)
    if lead == MAGIC_LOGIC_OSC:
        return "osc"
    if lead == MAGIC_LOGIC_TREND:
        return "trend"
    return ""


def decode_fsm_magic(magic: Any) -> dict:
    """magic → 可读字段（排障/核对用；**不参与交易判定**）。

    Returns: `dict(logic=…, state_code=…, reason_code=…, tier=…)`；非 FSM → `{}`。
    """
    if not is_fsm_magic(magic):
        return {}
    v = int(magic or 0)
    if v in (MAGIC_LOGIC_OSC, MAGIC_LOGIC_TREND):
        return {"logic": v, "state_code": None, "reason_code": None, "tier": None,
                "legacy": True}
    tt = v % 100
    rr = (v // 100) % 100
    ss = (v // (100 ** 2)) % 100
    return {"logic": v // (100 ** 3), "state_code": ss, "reason_code": rr,
            "tier": tt, "legacy": False}

DEFAULTS: dict = {
    "state.order_enabled": False,
    "state.box.window": 20,
    "state.trend.slope_window": 20,
    "state.trend.pullback_atr": 0.5,
    # 【2026-09-22 新增】回踩**上界**（ATR 倍数）：距近期极值超过此值 = "深度回撤"
    #   而非"顺势回踩" → 不算回踩入场（S2 首建 / S3 首建 / S3 加仓共用同一判据）。
    # 依据（magic62 生产实证 n=44，实盘 orders⊕signals）：`_pullback_entry` 原为**单边**
    #   判据（仅 ≥pullback_atr），无上界 ⇒ 允许在距近期极值 p90=2.68ATR 处开仓；
    #   实测 `dist20≥1.5ATR` 的 10 单 avg −6.81 / 胜率 30%（vs 全体 −2.52 / 52.3%），
    #   即"深度回撤"单贡献 62% 亏损；`dist20≤1.5` 的 34 单 avg −1.25 / 胜率 58.8%。
    # 取值：**0 = 关闭本闸**（默认，零行为变更，一行回滚）；>0 = 上界（ATR 倍数）。
    "state.trend.pullback_max_atr": 0.0,
    # 【2026-09-15 §18.7-2】回踩判定的回看根数，**与箱体窗口解耦**。
    # 0 = 跟随 `state.box.window`（保持既有行为，便于灰度对比）。
    # 解耦理由：两者语义无关 —— 箱体窗口是"震荡区间宽度"的参数，
    # 回踩窗口是"距最近极值多远算回踩"的参数；共用一个值会让调其中一个必动另一个。
    "state.trend.pullback_window": 0,
    "state.trend_max_adds": 2,
    # 【2026-09-15 §18.7-2】移动止损"近 N 根极值"的 N，**与 slope_window 解耦**。
    # 此前它被错误地绑在 `state.trend.slope_window` 上（方向回归窗口）——
    # 两者语义无关：一个是"止损看多远的极值"，一个是"判方向用多长的回归"。
    "state.trend.trail_lookback": 20,
    # 【S4 趋势衰竭】移动止损收紧系数（规格 11：S4 收紧止损、准备离场）。
    # 0.5 = 止损距离减半。⚠ 该值**尚未标定**（无 S4 离场行为的离线证据，
    # M5 方向符号反向问题未解前也无从标定）→ 属占位默认，须在灰度期标定。
    "state.trend.fade_trail_mult": 0.5,
    "state.osc_border_tol_atr": 0.25,
    # 【2026-09-15】箱体退化保护：宽度 < 此值 × ATR → 视为无效箱体，不开仓。
    # 依据：除"窄箱逆势单 R 极小"外，更硬的理由是 **width < 2×border_tol_atr×ATR 时
    # `c<=lo+tol` 与 `c>=up-tol` 会同时成立**，方向由 if/elif 顺序决定（等价随机）。
    # 取值 1.0 与 tol(0.25) 满足 1.0 > 2×0.25 → 结构性排除该情形。
    # ⚠ 该阈值**尚未经离线标定**（诚实标注：与"摆脱人工阈值"的目标部分相悖，待标定）。
    "state.osc_box_min_width_atr": 1.0,
    # 【2026-09-23】箱体**宽度上限**（ATR 倍数）：宽于此 ⇒ 视为"假箱体"，不开仓。
    # 为什么需要（**实测双证据**，非推测 —— 铁律 §15）：
    #   · 离线矩阵（20,061 根；复用生产同一 `compute_entry_box` + 同一贴边判定，池 n=2289）：
    #     箱宽 >4ATR 桶 **均值R −0.4755 / 胜率 35.2%**，而 1.0~1.5 桶 **+0.2432 / 90.6%**
    #     ⇒ 单调递减；
    #   · 生产实证（`market_state_log` × 实盘 orders，关联 16 单）：>4ATR 的 4 笔
    #     **合计 −98.54 / 均值 −24.64 / 胜率 0%**（占关联单 25%，贡献约 98% 的合计亏损）。
    #   机制：箱宽大 ⇒ ① 距中值远 ⇒ TP(`mid`) 难达；② 大箱体本身即"波动放大/趋势"的代理
    #     ⇒ 逆势单易被突破（正是本文件 `:1499-1502` 记录的 −47 事故的成因）。
    # 语义：**0.0 = 关闭本闸**（默认，零行为变更，一行回滚、秒级热生效）。
    "state.osc_box_max_width_atr": 0.0,
    # 【D2 2026-09-17 治本】震荡止损**预算上限**（ATR 倍数）。与 FSM 共用**同一配置键**
    #   `state.osc_atr_loss_limit`（真值是 `hcm:state:osc_atr_loss:{symbol}`，由桥写入）。
    # 为什么本层也要读它：4ATR 锁止原本**只在 FSM 处于 S1_OSC 时**判定
    #   （state_machine.decide：`if cur == S1_OSC and osc_atr_loss >= limit → S5`），
    #   而 2026-09-17 起 **S0_IDLE 也允许出箱体单** ⇒ 状态是 S0 时预算用尽**照样开新单**
    #   ⇒ "4ATR 防爆仓"被绕过（实测当时 `osc_atr_loss=4.814 ≥ 4.0` 仍在开）。
    #   故在下单路径本身补一道闸（见 decide 内 `osc_atr_locked_no_new_order`）。
    "state.osc_atr_loss_limit": 4.0,
    # ── 【路线 B · 2026-09-16】箱体「波动扩张闸」阈值 ──────────────────────────
    # 语义：S1 箱体是**逆势**策略，前提是波动收敛；一旦波动开始扩张，边界被有效突破的
    #   概率上升 ⇒ 此时逆势开仓把亏损源放进组合。
    # 取值：**< 0 = 关闭本闸**（默认，零行为变更，可一行回滚）。
    #   开启时填 P(波动扩张) 的**触发阈值**，按"目标拦截率分位"标定（与 rise_thr 同纪律：
    #   不能用"最大 F1"，会随 base rate 漂移）。
    # 依据（生产实证）：箱体单 6 轮中 8/10 张盈利，但 2 张亏损各 **−14.61 USD**，
    #   显著大于典型盈利(+1.37) ⇒ 亏损集中在**波动放大的入场**上。
    # 依据（模型实证）：波动扩张模型 OOF AUC 0.6465；验收门（真值=波动扩张上升沿）
    #   漏检 91 / 误报 11.6% / 中位 **+0.5**，而"当期波动分位"平凡规则为
    #   漏检 547 / 中位 **+12.0（滞后 12 根）** ⇒ 把滞后确认变成同步预警。
    "state.vol.osc_skip_prob": -1.0,
    # 【P2 2026-09-19】"仅当 vol 预测**高置信**（conformal 单例）时才拦箱体单"。
    # 为什么需要：拦箱体是**放弃机会**，误拦的代价随"模型不确定"上升；单例判定给出
    #   "这个预测本身可信"的分布无关保证 ⇒ 让闸门只在可信时生效（置信度真正接到闸门上）。
    # 语义：`vol_route_singleton is True` 才允许拦截；`False` 与 **`None`（未知）都不拦**
    #   （`None` = α≤0 或缺 conformal 产物 ⇒ 不裁决，保持既有行为）。
    # ⚠ 启用本键**必须同时**把 `state.vol.alpha` 设为 >0（见 `state_infer.DEFAULTS`），
    #   否则 singleton 恒为 None ⇒ 本闸恒不触发（**fail-safe，不会多拦**）。
    # 阈值标定基准（`tools/eval_vol_route_gate.py`，OOF n=56995，**校准后**概率分桶）：
    #   桶0 p≈0.175 → 未来振幅 2.231 ATR（箱体友好）… 桶4 p≈0.466 → 3.578 ATR（最不利）
    #   ⇒ `osc_skip_prob = 0.30` ≈ 拦截 58.5% / 放行 41.5%（桶0+桶1）。
    #   ⚠ 该校准值建立在**校准后**尺度上；meta 缺 `calibration` 时 `p_cal` 退回原始概率，
    #     阈值不再对应上述分位 —— 启用前须确认日志 `calibration=有`。
    "state.vol.osc_skip_require_singleton": False,
    # 【2026-09-15 L4 实测】追涨过滤：信号 bar 振幅 > 此 ×ATR 视为"追高/追低"，放弃首次入场。
    # 依据（tools/eval_entry_timing.py，同一信号集对比 8 种买点规则）：
    #   A 触发即入          R=−0.336
    #   B 等回调 0.5ATR     R=−0.289（等回调优于即入）
    #   C 即入+滤振幅>1.5ATR R=−0.249（**最优**，+0.09R vs A）
    # ⚠ 但该表全部为负期望 → 买点不是瓶颈（根因是 M5 方向符号反向，见方案 §20/§34）。
    "state.trend.spike_atr_max": 1.5,
    "state.osc_lot_ladder": "0.5,1.0,1.5,2.0",
    # 【2026-09-15 L4 触价入场（方案 §49）】首次入场模式。
    #   close_check（**默认 = 既有行为，零变化**）：只在"已回踩到位"的那根 bar 收盘市价入场。
    #   zone_touch（新增）：若**尚未**回踩到位，则额外下发"触价入场位"（= 回踩目标价）
    #     与等待秒数，交桥侧**既有** P1a zone gate 等价格触及后成交。
    # 为什么走"下发 zone 字段"而不是新增通道：`mt5_bridge.py:4200-4218` 已有完整的
    #   "价位未到 → 存 deferred（带 TTL）→ 每 2-5s 复查 → 触及即成交 / 超时自动作废"，
    #   并带**信号新鲜度闸门**与 T3c 尖刺过滤；配置 `signal_tower.zone_trigger_enabled`
    #   生产**已为 true**，`filtered` / `HEXP:Regime.*` 信号长期在跑这条路径。
    #   ⇒ 另写一套挂单/等待逻辑即重复实现（本仓库红线）。
    # 语义是**严格超集**（见 decide 内注释）：已回踩 → 仍市价入场；未回踩 → 才下发触价位。
    "state.trend.entry_mode": "close_check",
    # 触价入场最长等待秒数。默认 300s ≈ 1 根 M5，使"每 bar 至多存在一个未成交的触价单"
    # （塔每 bar 刷新，等待期与 bar 周期对齐，天然去重）。
    "state.trend.entry_wait_sec": 300,
    # ═══ 【2026-09-15 §57 箱体规格吸收】═══════════════════════════════════
    # 来源：用户 2026-09-15 提供的震荡态箱体策略设计（12.1–12.5 + 止损/冻结/边界场景）。
    # 原则：**能改配置的不加键，能复用的不新写** —— 故下列仅为"规格确有、现状确无"的差集。
    # 已被现有实现覆盖、**刻意不新增**的项（列此以免后来者重复加键）：
    #   · N 根闭合 K 线 / 三条线 High·Low·Mid → `state.box.window` + `compute_entry_box`
    #   · MinBandHeight                    → `state.osc_box_min_width_atr`（ATR 归一，比绝对高度可移植）
    #   · 单边 1 笔 / 非震荡禁开            → S1 分支 + `hold_only`（规格 9.2）
    #   · 硬止损兜底                       → 桥按 `close.<session>.trailing_stop_distance` 强制执行
    #   · 趋势下不出新信号 / 稳定回震荡才解锁 → FSM 状态 + `k_enter` 防抖（§12.4 的等价实现）
    #
    # 边界口径：`extremum` = 窗口内原始极值（既有行为）；`quantile` = 95%/5% 分位
    #   （规格 12.1 推荐）。**为什么保留 extremum 为默认**：既有生产行为零变化；
    #   两模式可 A/B 后再切换（规格本身写的是"可选原始极值"）。
    "osc.bands_mode": "extremum",
    "osc.q_high": 0.95,               # bands_mode=quantile 时的上沿分位
    "osc.q_low": 0.05,                #                                 下沿分位
    # 价格缓冲口径：`atr` = 既有 `state.osc_border_tol_atr`（ATR 归一，跨品种可移植）；
    #   `pct` = 规格 12.2 的 `LowBand*(1+buffer)` / `HighBand*(1-buffer)`。
    # ⚠ 规格用百分比：对 XAUUSD(≈4300) buffer=0.001 ≈ 4.3 美元，**随价位量级漂移**，
    #   换品种即失效 —— 故做成**可选模式**而非直接替换（同 `trend_direction` 对斜率归一的处理）。
    "osc.buffer_mode": "atr",
    "osc.buffer_pct": 0.001,
    # 入场防抖（规格 12.2「满足防抖 K 线校验」）：连续 N 根满足边界条件才开仓。
    # 默认 1 = 与既有行为一致（单根即触发）。
    "osc.entry_confirm_bars": 1,
    # 止盈口径（规格 12.3-1）：mid（默认，规格"优先中轨落袋，更稳"）| far（对边）| pct（箱高比例）
    "osc.tp_mode": "mid",
    "osc.tp_pct": 0.5,                # tp_mode=pct：盈利率 = 箱体高度 × 该比例
    # 箱体突破**逻辑止损**（规格 12.3-2 / 止损第 1 条）：连续 N 根收盘价破界 → 判定震荡失效，
    # 立刻离场。**这是现状完全没有的一条**（原先只有桥的会话 ATR 硬止损）。
    # 默认 0 = **关闭**（不改变既有行为）；规格值 2。
    "osc.break_confirm_bars": 0,
    # ── 【2026-09-17 A/B/C】S0 箱体入场的三道收口（全部由实测事故驱动）──────────────
    # 事故（07:15）：`state.osc_in_idle`（今早放宽 S0）⇒ S4 衰竭后当根即开箱体 SELL 0.03
    #   ⇒ 5 分钟后转 S2 被判"轮次结束" ⇒ TP 锚点丢 + 破界离场永久不下发 ⇒ 孤儿单 -47。
    # ① `state.osc_in_idle`：S0_IDLE 是否可作为箱体入场态（**显式登记默认值**，
    #    原为代码内 `get_bool(..., True)` 隐含默认 ⇒ 运维在配置中心看不到它）。
    "state.osc_in_idle": True,
    # ② 【A】S0 且**上一状态 ∈ 趋势态**时禁出箱体单：S0 最常见的来源就是 S4_TREND_FADE，
    #    刚离开趋势态就按箱体逆势入场 = 在趋势里反向开仓。true = 启用本护栏。
    #    回滚：`set_cfg.py state.osc_idle_block_after_trend false`（秒级）。
    "state.osc_idle_block_after_trend": True,
    # ④ 【P1-6 F7 2026-09-18】S0 护栏**窗口长度**（根）：离开趋势态后，S0 连续 N 根禁出
    #    箱体单。背景（审计 F7）：原护栏只看 `prev_state`（上一根起始态）⇒ 仅拦 S4→S0 那
    #    1 根，次根 prev=S0 即放行；而模型出趋势本身要 k_exit=2 根 ⇒ 07:15 逆势箱体单(-47)
    #    的时序模式未被根除。0 = 关闭本窗口（退回原 1 根语义）；默认 3（≈15min）。
    #    回滚：`set_cfg.py state.osc_idle_block_bars 0`（秒级热生效，无需重启）。
    "state.osc_idle_block_bars": 3,
    # ③ 【C】"贴边"判据允许的**越界容忍**（ATR 倍数，双侧判据的上界）。
    #    0.0 = 必须仍在箱内（**严格**，本次选定）；>0 = 允许越过边界一点仍算贴边。
    #    ⚠ 原实现无上界（等价 +∞）⇒ 价格突破上沿 8.9 点仍判"贴上沿" ⇒ 突破途中抄顶，
    #      是本次事故的直接成因之一。
    "state.osc_edge_max_overshoot_atr": 0.0,
    # ── 【马丁补仓 2026-09-18 · 用户决策】SL 后「及时」同向补下一档 ──────────────
    # 需求（用户原话）："0.5 止损后及时下 1.0、不是等行情变化后再接着下阶梯手数"。
    # 缺陷：SL 后箱体被解冻 + 滚动箱重建 ⇒ 重入场必须等价格回到【新箱体】边缘
    #   （实测 2026-09-18：09:41 的 0.5 止损 → 10:45 才补 1.0，空 64 分钟）。
    # 本键开启后：**首单路径完全不变**（仍由箱体边缘触发）；一旦本轮因【止损】结束
    #   且已平仓 ⇒ 下一根 bar 直接用**本轮入场方向**市价补下一档（`ladder[连亏次数]`），
    #   不再等箱体重建、不再等 FSM 状态。
    # 安全边界（**不放宽任何既有刹车**）：① 必须已平仓；② `state.osc_atr_loss_limit`
    #   （4ATR 预算）为硬刹车，用尽则不补；③ 档位仍由 `state.osc_lot_ladder` 封顶；
    #   ④ 下游风控（`risk.max_lot_per_trade` / `risk.max_concurrent_signals` / 同向保本
    #   闸门 / 置信度闸门）**照常生效**，本键不绕过任何风控。
    # 回滚：`set_cfg.py state.osc_martingale_enabled false`（秒级热生效，无需重启/改码）。
    "state.osc_martingale_enabled": True,
}


@dataclass
class StrategyContext:
    """品种级策略上下文（Redis 持久，跨重启存活）。"""
    symbol: str = ""
    box_upper: float = 0.0
    box_lower: float = 0.0
    box_mid: float = 0.0
    box_frozen: bool = False        # 开仓后冻结（本轮止盈目标锁定）
    box_frozen_at: str = ""
    # 冻结那一刻的震荡计数器快照（-1 = 尚未冻结）。
    # 用途：**判定本轮是否已因止盈/止损结束**（规格 §7.1：止盈/止损/状态切换任一即结束本轮）。
    # 止盈/止损发生在桥侧、信号塔拿不到事件，但桥会把结果写进这两个计数器
    # （见模块 docstring）→ 用"计数器是否变化"即可判轮次结束，无需新增事件通道。
    frozen_loss_count: int = -1
    frozen_atr_loss: float = -1.0
    # 【2026-09-16 修 be-不解冻缺口】"轮次结束"的**显式标记**快照。
    # 为什么必须新增（上面那个"计数器是否变化"的判据**有洞**）：
    #   `apply_osc_close` 对 `be` / `manual` / `expert` / `stop_out` **原样返回**
    #   （不计入止损预算 —— 这对 4ATR 预算是对的），但**策略却拿同两个数判"本轮结束"**
    #   ⇒ 若一轮的平仓**全部**是这些归因（例如 master+follower 两笔都是保本离场），
    #   计数器不变 ⇒ **判不出"本轮结束" ⇒ 冻结箱体永久不解**（此后每根 bar 都拿
    #   过期 mid 当 TP 锚点）。规格 §7.1 明确"止盈/止损/状态切换**任一**即结束本轮"
    #   ⇒ "是否计入预算"与"轮次是否结束"**本就不该是同一个数**。
    # 语义：-1 = **未知**（桥未升级 / 读取失败）→ 回退旧判据，行为与修复前一致（无回归）。
    frozen_round_seq: int = -1
    # 震荡连续止损**次数**（决定梯度手数档位）。
    # ⚠ 这是**读透缓存**，不是真值：每轮 decide 从 `hcm:state:osc_loss_count:{symbol}`
    #   刷新（真值由桥的平仓归因写入）。保留字段仅为面板展示与序列化兼容。
    consec_losses: int = 0
    osc_round_active: bool = False  # 震荡轮次是否进行中
    add_count: int = 0              # 趋势已加仓次数
    trend_dir: str = ""             # 趋势方向（UP/DOWN，开仓后锁定）
    # ── L4 触价入场（§49）：最近一次"触价单"的下发时刻（ISO；"" = 无未结触价单）与方向 ──
    # 【为什么必须有这道闸】塔是**每根 bar 重新评估**的，而桥侧触价单要等最多
    # `entry_wait_sec` 才成交/作废。若不去重，等待期内每根 bar 都会再下发一张**同价位**
    # 触价单 → 价格触及时**同时成交多笔**（实盘 = 超仓，且策略层以为只有 1 笔）。
    # 判据只用"时刻 + 等待秒数"（与桥侧 TTL 同口径），不引入第二个真值源。
    touch_pending_at: str = ""
    touch_pending_dir: str = ""
    # ── 【§57】箱体规则的**跨 bar 计数**（规格 12.2 入场防抖 / 12.3-2 突破止损）──
    # 为什么放 ctx 而不是模块内变量：`decide` 是每 bar 的**无状态**调用，而"连续 N 根"
    # 天然是跨 bar 状态；ctx 是 Redis 持久（跨重启存活），与其它字段同构 —— 不引入第二套
    # 状态存储。每根 bar 由 decide 更新一次（bar 去重由调度层的 bar_time 保证）。
    osc_edge_streak: int = 0            # 贴边连续根数（入场防抖进度）
    osc_break_streak: int = 0           # 破界连续根数（突破止损进度）
    osc_break_side: str = ""            # 破界方向 "below"/"above"；**换向即重置计数**
    # ── 【P1-7 2026-09-18】上面两个"连续根数"的**同 bar 去重**（bar 身份 = 被评估的
    #    已收盘 bar，由调用方传入 `decide(bar_id=...)`）。
    # 缺陷（2026-09-18 实测）：`_run_shadow_state` **同一根 bar 可能被调用两次**
    #   （`scheduler.py:2079-2086` 原文自述：首行可能早于"策略层就绪"或早于
    #   "live_override 重入"写入；FSM 用 `last_bar_time` 去重 ⇒ 状态不重复推进，但
    #   `decide()` 不在那条去重路径里）。而这两个计数**原先按调用次数 +1、不看 bar 身份**
    #   ⇒ 同一根 bar 评估两次就 **+2** ⇒ `osc.break_confirm_bars=2` 被**1 根**满足。
    # 实证：2026-09-18 ticket 426224111 —— 塔日志报"连续 2 根破下沿 (close=4342.990)"，
    #   而 4342.990 是 **14:25 那一根**（第 2 根应为 14:30 的 4346.970，当时尚未评估）
    #   ⇒ 该笔实为**提前 1 根离场**（当次侥幸结论正确：14:30 确实也破了）。
    # 语义：`bar_id` 非空时，同一 `bar_id` **只推进一次**（重复评估不重复计数，
    #   但**下方触发判定仍执行** —— 已达标必须继续下发离场，不得因去重而卡住）。
    # 空串 = 调用方未提供 ⇒ **回退旧行为**（每次调用都推进，向后兼容离线/影子调用）。
    osc_edge_bar: str = ""              # 上一次推进贴边计数的 bar 身份
    osc_break_bar: str = ""             # 上一次推进破界计数的 bar 身份
    # 【P1-6 F7 2026-09-18】S0 护栏**剩余根数**（>0 = "离开趋势后的静默期"，禁 S0 箱体单）。
    # 推进（每 bar 一次，见 decide 箱体入口块）：上一状态∈趋势态 → 重置为
    # `state.osc_idle_block_bars`；否则递减。持久于 ctx（跨 bar / 跨重启存活）。
    osc_idle_guard_left: int = 0
    # ── 【马丁补仓 2026-09-18 · 用户决策】SL 后「及时」同向补下一档 ──────────────
    # 本轮箱体单的**入场方向**（BUY/SELL）。用途：止损后按"同向"补下一档
    # （用户选择"经典马丁"：0.5 做多止损 → 1.0 继续做多），方向必须锚定在
    # **被止损的那一笔**上，而解冻后箱边可能已在另一侧 —— 故必须持久记住。
    osc_last_dir: str = ""
    updated_at: str = ""


@dataclass
class StrategyIntent:
    """一次策略裁决的意图（不落单，仅描述"若要下单会怎么下"）。"""
    symbol: str = ""
    state: str = ""
    action: str = ""                # "" | open | add | none
    direction: str = ""             # BUY / SELL
    reason: str = ""
    lot_multiplier: float = 0.0     # base_lot × 该倍率（S1 梯度 / 其余 = 1.0）
    box_upper: float = 0.0
    box_lower: float = 0.0
    box_mid: float = 0.0
    # 【2026-09-16】上面三线是"冻结快照"还是"每 bar 重算的滚动箱"？面板据此区分：
    #   True = 本轮已开仓、三线锁定（用作 TP 锚点/破界判定）；False = 滚动箱（当前行情）。
    # 没有这个标志时两种语义的三线**外观完全一样**，容易被读成"价格离箱体很远"；
    # 而"未冻结就清零"的上一版做法会让面板**直接看不到箱体**（用户当即发现），故两者都不可取。
    box_frozen: bool = False
    tp_anchor: Optional[float] = None   # S1：冻结箱体中值（透传 tp_price 锚点）
    sl_locked: bool = False             # True → 桥豁免会话 SL 地板
    trail_lookback: int = 0             # 移动止损回看根数（"近 N 根极值"的 N）
    # 移动止损**收紧系数**：1.0 = 按桥的会话系数正常执行；< 1.0 = 收紧（S4 趋势衰竭）。
    # 桥侧应按 `trail 距离 × trail_mult` 收紧。**数值由桥计算**（信号塔不写死 SL/TP 数值，
    # 见模块 docstring 的职责边界），此处只表达"收紧多少倍"这一意图。
    trail_mult: float = 1.0
    # 是否"准备离场"（S4）：给桥/面板一个显式信号，避免只能从 trail_mult<1 反推。
    exit_ready: bool = False
    add_count: int = 0
    order_enabled: bool = False         # 本次是否允许真下单（观测用）
    # ── L4 触价入场（方案 §49；`state.trend.entry_mode="zone_touch"` 且**尚未**回踩到位时才有值）──
    # 语义：**触价入场位**（= 回踩目标价）。桥侧既有 P1a zone gate 消费它：
    # 价格已在该位附近 → 立即成交；否则挂 deferred 等触及，超时（`entry_wait_sec`）自动作废。
    # `0.0` = 不下发触价（走市价，与既有行为一致）。
    zone_level: float = 0.0
    entry_wait_sec: int = 0             # 触价等待秒数（0 = 不下发）
    # ── 【2026-09-15 §57 箱体规格吸收】──
    # **立刻离场**指令（规格 12.3-2 第 1 条：箱体突破逻辑止损 —— "震荡一旦被突破…
    # 不能扛单"，要求"立刻止损离场"）。
    # 为什么必须与 `exit_ready` **分开**：`exit_ready`（S4）= "**准备**离场"（收紧移动止损、
    # 等止盈/止损自然发生）；本字段 = "**现在**离场"。若合并成同一个，S4 一到就会把所有
    # FSM 持仓一次性市价平掉 —— 与 §43.4/S4 的"渐进离场"语义冲突。
    exit_now: bool = False
    exit_reason: str = ""               # 归因：band_break_lower / band_break_upper
    # **作用域**：指令键 `hcm:state:directive:{symbol}` 是**按品种**的，而同一品种上可能
    # 同时存在箱体单（magic 61）与趋势单（magic 62）。若不限定作用域，"箱体结构破坏"
    # 会连带把**正常的趋势单**一起平掉。故：`osc` = 只平 magic 61；`trend` = 只平 62；
    # `""`（默认）= 不下发离场。
    exit_scope: str = ""
    break_streak: int = 0               # 破界连续根数（观测：让"差一根就止损"可见）
    edge_streak: int = 0                # 贴边连续根数（观测：入场防抖进度）
    # ── 三件套输入的三项观测字段（方案 §31.3；目前**只记录不裁决**，除 direction）──
    age_bars: int = 0                   # 状态年龄：承载"初生/中段"（§25）
    dir_source: str = ""                # 方向来源：dir_module / slope_fallback / ...
    shape: str = ""                     # 形状描述器结果（推理尚未接线，见 decide 注释）

    def as_dict(self) -> dict:
        d = asdict(self)
        d["trail_mult"] = round(self.trail_mult, 4)
        d["tp_anchor"] = (round(self.tp_anchor, 3) if self.tp_anchor else None)
        for k in ("box_upper", "box_lower", "box_mid", "zone_level"):
            d[k] = round(d[k], 3)
        return d


def resolve_trend_dir(direction: str, slope: float) -> tuple[str, str]:
    """解析趋势方向，返回 (UP/DOWN/"none"/"" , 来源)。

    优先级与语义（方案 §20 / §31）：
      · 方向模块给出 up/down → 直接采用（**唯一真值**：ATR 归一斜率 + ±DI + 防抖）
      · 方向模块给出 **none** → 返回 "none"，调用方**必须拒绝趋势开仓**
        （用户规格：形态判趋势但方向 NONE → 不开仓，规避方向模糊的假趋势）
      · 方向模块未接入（""）→ 回退旧口径（斜率符号），并标注 `slope_fallback`
        以便灰度期区分"模块方向"与"斜率方向"的贡献；**不得**把二者混为一谈。
    """
    d = str(direction or "").strip().lower()
    if d in ("up", "down"):
        return d.upper(), "dir_module"
    if d == "none":
        return "none", "dir_module_none"
    if slope > 0:
        return "UP", "slope_fallback"
    if slope < 0:
        return "DOWN", "slope_fallback"
    return "", "no_dir"


# ── 塔 → 桥 的下单契约（方案 §12-4 / §18.3）───────────────────────────────
FSM_SIGNAL_MODE_OSC = "state_osc"      # S1 箱体逆势单
FSM_SIGNAL_MODE_TREND = "state_trend"  # S2/S3/S4 顺势单（含加仓）
# L4 触价入场的 zone 标签（方案 §49）。桥侧 zone gate 只要求 `zone_level>0` +
# `entry_trigger_wait>0`，**不校验 zone_type** —— 故本标签仅作**归因/排障**用
# （便于在桥日志里区分"FSM 回踩触价"与 hexp 的结构位等待）。
ZONE_TYPE_TREND_PULLBACK = "FSM_PULLBACK"
# 每根 bar 刷新一次的"持仓管理指令"键（供桥侧 FSM 分支读；见 to_directive）
DIRECTIVE_KEY_TMPL = "hcm:state:directive:{symbol}"


def fsm_signal_mode(intent: "StrategyIntent") -> str:
    """意图 → `signal_mode` 子模式。

    必须分子模式（而非统一的 `state_fsm`）：桥侧平仓归因靠它区分"**震荡**止损"与
    "趋势止损" —— 规格 9.4 的 4ATR 锁止预算是震荡态专用的（见 `tools/position_sync.py`
    的子模式过滤）。子模式串里带 `osc` 才会被计入。

    【2026-09-17 C 修复】原实现只认 `intent.state == "S1_OSC"`。而 S0_IDLE 现在也可出
    箱体单（开关 `state.osc_in_idle`）⇒ 那些单会被**错标成 `state_trend`(62)**，连带
    magic 逻辑码、桥侧平仓归因（震荡/趋势止损之分）、`scope=osc` 的破界平仓全部错位。
    故改为**按意图语义判定**：箱体逆势单的 reason 恒以 `osc_` 开头
    （`osc_at_box_lower` / `osc_at_box_upper`），而趋势侧 reason
    （`init_*` / `mid_*` / `dir_none_veto` / `no_trend_dir` …）不含该前缀
    ⇒ 用前缀判定比枚举状态名更稳（将来新增"可出箱体单"的状态无需再改此处）。
    """
    if str(intent.state or "") == "S1_OSC" \
            or str(intent.reason or "").startswith("osc_"):
        return FSM_SIGNAL_MODE_OSC
    return FSM_SIGNAL_MODE_TREND


def to_signal_fields(intent: "StrategyIntent", base_lot: float = 0.0) -> dict:
    """意图 → 下单字段（**纯函数**：塔→桥契约的**唯一实现点**，可离线断言）。

    与"桥按会话系数算 SL/TP"的分工（用户 2026-09-14 决策，方案 §18.2）保持一致：
    **信号塔不写死 SL/TP 数值**，只用 0 / 锚点表达"由桥计算"或"用这个价"。

    | 字段 | 值 | 语义 |
    |---|---|---|
    | `direction` | `BUY`/`SELL` | `""` = 不下单（调用方须跳过） |
    | `lot` | `base_lot × lot_multiplier` | `base_lot<=0` 时传 **0** = 由桥按默认手数逻辑决定 |
    | `sl_price` | **恒 0** | 桥按 `close.<session>.*` 时段系数计算（**塔不写死**） |
    | `tp1` | S1 = 冻结箱体中值；其余 **0** | 非 0 → 桥直接采用；0 → 桥按会话 TP |
    | `sl_locked` | 意图值 | True → 桥豁免会话 SL 地板 |
    | `signal_mode` | `state_osc` / `state_trend` | 桥侧平仓归因据此区分震荡/趋势 |
    | `zone_level` | **0 或 触价入场位** | L4：`>0` → 桥侧 zone gate **等价格触及**再成交（§49） |
    | `zone_type` | `FSM_PULLBACK` / `""` | 仅归因用（桥不校验） |
    | `entry_trigger_wait` | 秒 | 与 `zone_level` 配对；`0` → 立即市价（既有行为） |
    | `_fsm` | 元数据（放 `indicator_values`） | 状态/子模式/收紧系数/加仓等，**无 schema 迁移** |

    **不返回 `magic`**：mode→magic 的单一真源是
    `signal_publisher.magic_for_signal_mode`（`state_osc`=61 / `state_trend`=62），
    由发布方按 `signal_mode` 派生；此处再算一份就是重复映射。

    `_fsm` 里必须带 `trail_mult` / `exit_ready`：规格 S4 要求"收紧移动止损、准备离场"，
    而数值由桥算 —— 塔只表达"收紧多少倍"这一意图（§40）。
    """
    if intent.action not in ("open", "add") or not intent.direction:
        return {}                     # 非下单意图 → 空字典，调用方据此跳过
    mult = float(intent.lot_multiplier or 0.0)
    lot = (float(base_lot) * mult) if (float(base_lot) > 0.0 and mult > 0.0) else 0.0
    # L4 触价入场（§49）：**只有"尚未回踩到位"时** intent.zone_level 才 >0。
    # 桥侧 zone gate 的触发条件是 `entry_trigger_wait>0` **且** `zone_level>0`
    # （`mt5_bridge.py:4209-4211`）—— 缺任一即退化为"市价立即成交"。
    # 故两者必须**成对**下发/不下发，不能只给一个（否则语义静默降级）。
    _zl = float(intent.zone_level or 0.0)
    _zw = int(intent.entry_wait_sec or 0) if _zl > 0.0 else 0
    if _zl <= 0.0:
        _zw = 0
    return {
        "direction": intent.direction,
        "lot": round(lot, 4),
        "sl_price": 0.0,                                  # 桥按会话系数计算
        "tp1": float(intent.tp_anchor or 0.0),            # S1 冻结箱体中值；0 = 桥算
        "tp2": 0.0,
        "sl_locked": bool(intent.sl_locked),
        "signal_mode": fsm_signal_mode(intent),
        "reason": intent.reason,
        "zone_level": round(_zl, 4),
        "zone_type": (ZONE_TYPE_TREND_PULLBACK if _zl > 0.0 else ""),
        "entry_trigger_wait": _zw,
        "_fsm": {
            "action": intent.action,
            "state": intent.state,
            "sub_mode": fsm_signal_mode(intent),
            "lot_multiplier": round(mult, 4),
            "add_count": int(intent.add_count or 0),
            "box_upper": round(float(intent.box_upper), 3),
            "box_lower": round(float(intent.box_lower), 3),
            "box_mid": round(float(intent.box_mid), 3),
            "dir_source": intent.dir_source,
            # L4 触价入场归因（与实际下发的顶层字段同值；放这里便于 `_fsm` 一处看全）
            "zone_level": round(_zl, 3),
            "entry_wait_sec": _zw,
        },
    }


def to_directive(intent: "StrategyIntent") -> dict:
    """意图 → **持仓管理指令**（纯函数；写 Redis 键 `hcm:state:directive:{symbol}`）。

    **为什么不复用下单字段 `to_signal_fields`**：`trail_mult` / `exit_ready` 是**随状态变化**
    的量（S4 才收紧、S9 维持），而持仓的移动止损要在**每个 tick** 用**当前**值 ——
    若只在开仓时随信号快照一次，S4 到来时桥拿不到"该收紧了"。
    故：下单字段随信号走，**管理指令每根 bar 刷新**（塔每 bar 一次 set，桥随时读）。

    桥侧据此对 **magic 61/62** 的持仓执行（实现见 §43.4 的三步 clamp 规则）：
      · `trail_mult` < 1.0 → 收紧移动止损距离
      · `exit_ready` True → 准备离场（S4）
      · `trail_lookback` → "近 N 根极值"的 N

    组内商品为 `magic 61/62` 的持仓专属；**非 FSM 持仓（hexp 11/12/21/55）不受影响**。
    """
    return {
        "state": intent.state,
        "sub_mode": fsm_signal_mode(intent),
        "is_trend": intent.state in TREND_STATES,
        "trail_mult": round(float(intent.trail_mult), 4),
        # 【冲突②修复】trail_mult 的作用域：收紧意图只针对某一 FSM 子类。
        # 趋势态(S2/S3/S4)的 trail_mult<1.0 收紧只应作用于趋势单(magic 62, trend)；
        # 箱体/震荡态的 trail_mult 只作用于箱体单(magic 61, osc)。桥侧据此做 scope 匹配。
        "trail_scope": "trend" if intent.state in TREND_STATES else "osc",
        "exit_ready": bool(intent.exit_ready),
        "trail_lookback": int(intent.trail_lookback or 0),
        # ── 【§57】箱体突破的**立即离场**指令 ──
        # 桥侧消费点：`mt5_bridge` 的 FSM 持仓分支（每 tick 读本键，magic 61/62）。
        # **必须带 scope**：本键按品种，而同一品种可能同时有箱体单(61)与趋势单(62)，
        # 不带 scope 会把正常的趋势单一并平掉。
        "exit_now": bool(intent.exit_now),
        "exit_scope": str(intent.exit_scope or ""),
        "exit_reason": str(intent.exit_reason or ""),
    }


def compute_entry_box(high: Any, low: Any, window: int,
                      bands_mode: str = "extremum",
                      q_high: float = 0.95, q_low: float = 0.05,
                      ) -> tuple[float, float, float]:
    """入场箱体三线（**exclusive**：只用截至上一根已收盘 bar 的数据）。

    【口径说明 —— 必须写清，否则容易各算一份】
      · **exclusive 的理由**：本函数服务于"**用收盘价**贴着边界开仓"的判定。若把
        正在评估的那根 bar 纳入窗口，则 `box_lower == min(low)` 必 ≤ 该根 close
        ⇒ `close <= box_lower + tol` 近乎不可能成立（方案 §7.1 记录的箱体滑动陷阱）。
        故窗口 = **评估 bar 之前**的 window 根已收盘 K 线 —— 与规格"只取最近 N 根
        完成闭合 K 线"一致（"闭合"指**已收盘**，而非"含即将评估的那根"）。
      · `bands_mode`（规格 12.1 的两种模式）：
          `extremum`（**默认 = 既有行为**）：上沿 = 窗口内最高价、下沿 = 窗口内最低价；
          `quantile`（规格推荐）：上沿 = **high 的 q_high 分位**、下沿 = **low 的 q_low 分位**
                     （默认 95%/5%，削掉插针极值）。
      · `mid = (upper + lower) / 2` —— **两模式通用**（规格三线定义）。

    Returns:
        (box_upper, box_lower, box_mid)；数据不足或箱体退化返回 (0, 0, 0)。
    """
    n = len(high)
    if n < window + 1 or window <= 0:
        return 0.0, 0.0, 0.0
    seg_h = high[n - 1 - window: n - 1]
    seg_l = low[n - 1 - window: n - 1]
    if str(bands_mode).lower() == "quantile":
        _qh = min(max(float(q_high), 0.0), 1.0) * 100.0
        _ql = min(max(float(q_low), 0.0), 1.0) * 100.0
        hi = float(np.percentile(np.asarray(seg_h, dtype=float), _qh))
        lo = float(np.percentile(np.asarray(seg_l, dtype=float), _ql))
    else:
        hi, lo = float(max(seg_h)), float(min(seg_l))
    if not (hi > lo):            # 退化保护（分位异常/数据错乱）→ 视为无效箱体，由调用方拒绝
        return 0.0, 0.0, 0.0
    return hi, lo, 0.5 * (hi + lo)


class StateStrategy:
    """状态 → 意图映射器（纯计算 + Redis 上下文存取；不下单）。"""

    def __init__(self, config_provider: Any = None, redis_client: Any = None):
        self._config = config_provider
        self._redis = redis_client
        self._order_enabled = bool(DEFAULTS["state.order_enabled"])
        self._box_window = int(DEFAULTS["state.box.window"])
        self._slope_window = int(DEFAULTS["state.trend.slope_window"])
        self._pullback_atr = float(DEFAULTS["state.trend.pullback_atr"])
        self._pullback_max_atr = float(DEFAULTS["state.trend.pullback_max_atr"])
        self._pullback_window = int(DEFAULTS["state.trend.pullback_window"])
        self._spike_atr_max = float(DEFAULTS["state.trend.spike_atr_max"])
        # L4 触价入场（§49）：默认 close_check = 既有行为，代码路径与改动前逐位一致
        self._entry_mode = str(DEFAULTS["state.trend.entry_mode"])
        self._entry_wait_sec = int(DEFAULTS["state.trend.entry_wait_sec"])
        # 【§57 箱体规格吸收】默认值**全部 = 既有行为**
        # （extremum / atr 容差 / 防抖 1 根 / TP 中轨 / 突破止损**关闭**）
        self._bands_mode = str(DEFAULTS["osc.bands_mode"])
        self._q_high = float(DEFAULTS["osc.q_high"])
        self._q_low = float(DEFAULTS["osc.q_low"])
        self._buffer_mode = str(DEFAULTS["osc.buffer_mode"])
        self._buffer_pct = float(DEFAULTS["osc.buffer_pct"])
        self._entry_confirm = int(DEFAULTS["osc.entry_confirm_bars"])
        self._tp_mode = str(DEFAULTS["osc.tp_mode"])
        self._tp_pct = float(DEFAULTS["osc.tp_pct"])
        self._break_confirm = int(DEFAULTS["osc.break_confirm_bars"])
        self._max_adds = int(DEFAULTS["state.trend_max_adds"])
        self._trail_lookback = int(DEFAULTS["state.trend.trail_lookback"])
        self._fade_trail_mult = float(DEFAULTS["state.trend.fade_trail_mult"])
        self._border_tol_atr = float(DEFAULTS["state.osc_border_tol_atr"])
        self._box_min_width_atr = float(DEFAULTS["state.osc_box_min_width_atr"])
        # 【2026-09-23】箱宽上限（0.0 = 关闭；见 DEFAULTS 同名键注释）
        self._box_max_width_atr = float(DEFAULTS["state.osc_box_max_width_atr"])
        # 【D2 2026-09-17】震荡止损预算上限（键与 FSM 共用，见 DEFAULTS 注释）
        self._osc_limit = float(DEFAULTS["state.osc_atr_loss_limit"])
        # 【路线 B】箱体波动扩张闸阈值；< 0 = 关闭（默认，零行为变更）
        self._vol_osc_skip_prob = float(DEFAULTS["state.vol.osc_skip_prob"])
        # 【P2】仅当 vol 预测为 conformal 单例（高置信）时才拦（默认 False = 不附加该约束）
        self._vol_skip_require_singleton = bool(
            DEFAULTS["state.vol.osc_skip_require_singleton"])
        self._ladder = [float(x) for x in DEFAULTS["state.osc_lot_ladder"].split(",")]
        # 【2026-09-17 A/C】S0 箱体入场收口（见 DEFAULTS 同名键注释）
        self._osc_idle_block_after_trend = bool(
            DEFAULTS["state.osc_idle_block_after_trend"])
        # 【P1-6 F7】S0 护栏窗口长度（根；0 = 退回原"仅拦 1 根"语义）
        self._osc_idle_block_bars = int(DEFAULTS["state.osc_idle_block_bars"])
        self._edge_max_overshoot_atr = float(
            DEFAULTS["state.osc_edge_max_overshoot_atr"])
        # 【马丁补仓 2026-09-18】SL 后同向补下一档（见 DEFAULTS 同名键注释）
        self._osc_ma_enabled = bool(DEFAULTS["state.osc_martingale_enabled"])
        # 【可观测性 2026-09-18】`state.osc_in_idle` 提升为**实例属性**（原只在
        # `decide()` 内即时读取 ⇒ 日志/签名/面板**全都看不见它**，"改了不等于看得见"）。
        # ⚠ 初值取 **False**（保守）：热载成功前不放行 S0 箱体单 —— 与原先
        #   "读配置失败 → 取 False（S0 不出单）"的取舍**逐位一致**。热载在
        #   `load_config()` 完成（scheduler 每 30s 一次），故启动后最多 30s 生效。
        self._osc_in_idle = False
        self._box_window_by_symbol: dict[str, int] = {}
        self._cache: dict[str, StrategyContext] = {}

    # ── 配置 ───────────────────────────────────────────────
    async def load_config(self, symbols: Optional[list] = None) -> None:
        if self._config is None:
            return
        try:
            self._order_enabled = await self._config.get_bool(
                "state.order_enabled", self._order_enabled)
            self._box_window = int(await self._config.get_float(
                "state.box.window", self._box_window))
            self._slope_window = int(await self._config.get_float(
                "state.trend.slope_window", self._slope_window))
            self._pullback_atr = await self._config.get_float(
                "state.trend.pullback_atr", self._pullback_atr)
            self._pullback_max_atr = await self._config.get_float(
                "state.trend.pullback_max_atr", self._pullback_max_atr)
            self._pullback_window = int(await self._config.get_float(
                "state.trend.pullback_window", self._pullback_window))
            self._spike_atr_max = await self._config.get_float(
                "state.trend.spike_atr_max", self._spike_atr_max)
            # ── 【§57】箱体模式/口径 ──
            # 枚举值走**白名单校验**：非法值只告警并回退既有行为，**绝不**因拼错而悄悄
            # 变成另一种语义（同 `state.trend.entry_mode` 的处理）。
            _bm = (await self._config.get("osc.bands_mode", "") or "").strip().lower()
            if _bm in ("extremum", "quantile"):
                self._bands_mode = _bm
            elif _bm:
                logger.warning("osc.bands_mode 非法（%s）→ 回退 extremum", _bm)
                self._bands_mode = "extremum"
            _pm = (await self._config.get("osc.buffer_mode", "") or "").strip().lower()
            if _pm in ("atr", "pct"):
                self._buffer_mode = _pm
            elif _pm:
                logger.warning("osc.buffer_mode 非法（%s）→ 回退 atr", _pm)
                self._buffer_mode = "atr"
            _tp2 = (await self._config.get("osc.tp_mode", "") or "").strip().lower()
            if _tp2 in ("mid", "far", "pct"):
                self._tp_mode = _tp2
            elif _tp2:
                logger.warning("osc.tp_mode 非法（%s）→ 回退 mid", _tp2)
                self._tp_mode = "mid"
            self._q_high = await self._config.get_float("osc.q_high", self._q_high)
            self._q_low = await self._config.get_float("osc.q_low", self._q_low)
            self._buffer_pct = await self._config.get_float(
                "osc.buffer_pct", self._buffer_pct)
            self._tp_pct = await self._config.get_float("osc.tp_pct", self._tp_pct)
            # 防抖根数下界 1（0 会让"连续 0 根"恒真 = 无防抖，语义混乱）；突破根数下界 0（= 关闭）
            self._entry_confirm = max(1, int(await self._config.get_float(
                "osc.entry_confirm_bars", self._entry_confirm)))
            self._break_confirm = max(0, int(await self._config.get_float(
                "osc.break_confirm_bars", self._break_confirm)))
            _em = (await self._config.get("state.trend.entry_mode", "") or "").strip().lower()
            if _em in ("close_check", "zone_touch"):
                self._entry_mode = _em
            elif _em:
                # 拼错就回退既有行为并告警 —— 绝不因配置手误而**悄悄**变成触价下单
                logger.warning("state.trend.entry_mode 非法（%s）→ 回退 close_check", _em)
                self._entry_mode = "close_check"
            self._entry_wait_sec = int(await self._config.get_float(
                "state.trend.entry_wait_sec", self._entry_wait_sec))
            self._max_adds = int(await self._config.get_float(
                "state.trend_max_adds", self._max_adds))
            self._trail_lookback = int(await self._config.get_float(
                "state.trend.trail_lookback", self._trail_lookback))
            self._fade_trail_mult = await self._config.get_float(
                "state.trend.fade_trail_mult", self._fade_trail_mult)
            self._border_tol_atr = await self._config.get_float(
                "state.osc_border_tol_atr", self._border_tol_atr)
            self._box_min_width_atr = await self._config.get_float(
                "state.osc_box_min_width_atr", self._box_min_width_atr)
            # 【2026-09-23】箱宽上限（0.0 = 关闭）。**必须计入下方 `_sig`** ——
            # 否则"改了阈值而日志不变"，配置生效与否**看不见**（本仓库反复出现的盲区）。
            _bmw = await self._config.get_float(
                "state.osc_box_max_width_atr", self._box_max_width_atr)
            if _bmw is not None:
                self._box_max_width_atr = float(_bmw)
            # 【D2 2026-09-17】震荡止损预算上限（与 FSM 共用同一键；.get_float 返回 None
            #   时保留现值，避免配置缺失把闸门静默关掉）
            _ol = await self._config.get_float("state.osc_atr_loss_limit", self._osc_limit)
            if _ol is not None:
                self._osc_limit = float(_ol)
            # 【路线 B】箱体波动扩张闸（< 0 = 关闭）。**必须计入下方 _sig**，
            # 否则"改了阈值而日志不变" —— 配置生效与否看不见（本仓库反复出现的盲区）。
            self._vol_osc_skip_prob = await self._config.get_float(
                "state.vol.osc_skip_prob", self._vol_osc_skip_prob)
            self._vol_skip_require_singleton = await self._config.get_bool(
                "state.vol.osc_skip_require_singleton", self._vol_skip_require_singleton)
            _lad = (await self._config.get("state.osc_lot_ladder", "") or "").strip()
            if _lad:
                try:
                    self._ladder = [float(x) for x in _lad.split(",") if x.strip()]
                except ValueError:
                    logger.warning("state.osc_lot_ladder 非法（%s）→ 保留默认", _lad)
            # 【2026-09-17 A/C】S0 箱体入场收口（两键见 DEFAULTS 注释）
            self._osc_idle_block_after_trend = await self._config.get_bool(
                "state.osc_idle_block_after_trend", self._osc_idle_block_after_trend)
            # 【P1-6 F7】护栏窗口长度（下界 0 —— 0 = 原 1 根语义；不设上界，由运维决定）
            self._osc_idle_block_bars = max(0, int(await self._config.get_float(
                "state.osc_idle_block_bars", self._osc_idle_block_bars)))
            # 【可观测性 2026-09-18】S0 是否可作箱体入场态 —— 原只在 `decide()` 内读取，
            # 本行使其可热载 + 进签名 + 上日志 + 上 panel（`tuning`）。
            # 读失败时由外层 except 兜住 ⇒ **保留现值**（初始 False = 不放行，保守）。
            self._osc_in_idle = bool(await self._config.get_bool(
                "state.osc_in_idle", self._osc_in_idle))
            self._edge_max_overshoot_atr = await self._config.get_float(
                "state.osc_edge_max_overshoot_atr", self._edge_max_overshoot_atr)
            # 【马丁补仓 2026-09-18】SL 后同向补下一档（读失败由外层 except 兜住 ⇒ 保留现值）
            self._osc_ma_enabled = bool(await self._config.get_bool(
                "state.osc_martingale_enabled", self._osc_ma_enabled))
            for sym in (symbols or []):
                try:
                    self._box_window_by_symbol[sym] = int(await self._config.get_float(
                        f"state.box.window.{sym}", self._box_window))
                except Exception:  # noqa: BLE001
                    pass
            # 仅当配置实际变化时打 INFO（30s 热重载一次 → 否则产生大量重复日志）
            _sig = (self._order_enabled, self._box_window, self._slope_window,
                    round(self._pullback_atr, 6), round(self._pullback_max_atr, 6),
                    self._pullback_window, self._max_adds,
                    self._trail_lookback, round(self._fade_trail_mult, 6),
                    round(self._border_tol_atr, 6), round(self._box_min_width_atr, 6),
                    round(self._box_max_width_atr, 6),
                    round(self._spike_atr_max, 6),
                    self._entry_mode, self._entry_wait_sec,
                    # 【§57】箱体模式/口径**也计入签名** —— 否则"改了模式而日志不变"，
                    # 等于配置生效与否**看不见**（本仓库反复出现的盲区，同 dir_src 处理）。
                    self._bands_mode, round(self._q_high, 4), round(self._q_low, 4),
                    self._buffer_mode, round(self._buffer_pct, 6),
                    self._entry_confirm, self._tp_mode, round(self._tp_pct, 4),
                    self._break_confirm,
                    round(self._vol_osc_skip_prob, 6),
                    self._vol_skip_require_singleton,
                    tuple(self._ladder),
                    # 【2026-09-17 A/C】新增两键也计入签名（否则改了看不见）
                    self._osc_idle_block_after_trend,
                    # 【P1-6 F7 2026-09-18】护栏窗口长度也计入签名
                    self._osc_idle_block_bars,
                    # 【可观测性 2026-09-18】S0 箱体入场开关也计入签名
                    self._osc_in_idle,
                    # 【马丁补仓 2026-09-18】开关也计入签名（改了必须在日志里看见）
                    self._osc_ma_enabled,
                    round(self._edge_max_overshoot_atr, 6),
                    # 【D2 2026-09-17】预算上限也计入签名（同纪律：改了必须在日志里看见）
                    round(self._osc_limit, 6),
                    tuple(sorted(self._box_window_by_symbol.items())))
            if _sig != getattr(self, "_cfg_sig", None):
                self._cfg_sig = _sig
                logger.info(
                    "StateStrategy config loaded | order_enabled=%s box_window=%d "
                    "slope_window=%d pullback_atr=%.2f pullback_max_atr=%.2f "
                    "max_adds=%d entry_mode=%s "
                    "entry_wait_sec=%d bands=%s(q=%s/%s) buffer=%s(%s) entry_confirm=%d "
                    "tp=%s(%.2f) break_confirm=%d vol_osc_skip_prob=%.2f "
                    "vol_skip_singleton=%s ladder=%s "
                    "idle_guard=%s idle_guard_bars=%d osc_in_idle=%s edge_overshoot_atr=%.2f "
                    "osc_martingale=%s osc_atr_loss_limit=%.2f "
                    # 【2026-09-23】箱宽上下限也上日志（改了必须看得见）
                    "box_width=[%.2f,%.2f]",
                    self._order_enabled, self._box_window, self._slope_window,
                    self._pullback_atr, round(self._pullback_max_atr, 6),
                    self._max_adds, self._entry_mode,
                    self._entry_wait_sec,
                    self._bands_mode, self._q_high, self._q_low,
                    self._buffer_mode, self._buffer_pct, self._entry_confirm,
                    self._tp_mode, self._tp_pct, self._break_confirm,
                    self._vol_osc_skip_prob,
                    self._vol_skip_require_singleton,
                    self._ladder,
                    self._osc_idle_block_after_trend,
                    self._osc_idle_block_bars,
                    self._osc_in_idle,
                    self._edge_max_overshoot_atr,
                    self._osc_ma_enabled,
                    self._osc_limit,
                    self._box_min_width_atr,
                    self._box_max_width_atr,
                )
            else:
                logger.debug("StateStrategy config unchanged")
        except Exception as exc:  # pragma: no cover
            logger.warning("StateStrategy config load failed (using defaults): %s", exc)

    @property
    def order_enabled(self) -> bool:
        return self._order_enabled

    def box_window_for(self, symbol: str) -> int:
        return self._box_window_by_symbol.get(symbol, self._box_window)

    # ── 上下文持久化 ───────────────────────────────────────
    async def _load_ctx(self, symbol: str) -> StrategyContext:
        ctx = self._cache.get(symbol)
        if ctx is not None:
            return ctx
        ctx = StrategyContext(symbol=symbol)
        if self._redis is not None and getattr(self._redis, "is_initialized", False):
            try:
                raw = await self._redis.get(CTX_KEY_TMPL.format(symbol=symbol))
                if raw:
                    data = json.loads(raw)
                    known = {f for f in StrategyContext.__dataclass_fields__}
                    ctx = StrategyContext(**{k: v for k, v in data.items() if k in known})
                    ctx.symbol = symbol
            except Exception as exc:  # noqa: BLE001
                logger.warning("[state_strategy] %s 上下文读取失败（按空上下文继续）：%s",
                               symbol, exc)
        self._cache[symbol] = ctx
        return ctx

    async def _save_ctx(self, ctx: StrategyContext) -> None:
        ctx.updated_at = datetime.now(timezone.utc).isoformat()
        if self._redis is None or not getattr(self._redis, "is_initialized", False):
            return
        try:
            await self._redis.set(CTX_KEY_TMPL.format(symbol=ctx.symbol),
                                  json.dumps(asdict(ctx), ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[state_strategy] %s 上下文写入失败：%s", ctx.symbol, exc)

    # ── 震荡风控计数器（**只读**；写者是桥的平仓归因，见模块 docstring）──
    async def _read_osc_loss_count(self, symbol: str) -> int:
        """读震荡连续止损次数。缺省 0 → 梯度取第一档（不误放大手数）。"""
        if self._redis is None or not getattr(self._redis, "is_initialized", False):
            return 0
        try:
            raw = await self._redis.get(OSC_LOSS_COUNT_KEY_TMPL.format(symbol=symbol))
            return max(0, int(float(raw))) if raw else 0
        except (TypeError, ValueError):
            return 0
        except Exception:  # noqa: BLE001
            return 0

    async def _read_osc_round_seq(self, symbol: str) -> int:
        """读"震荡轮次结束"**显式标记**（桥每次 FSM 震荡单平仓 INCR 一次）。

        与两个计数器**刻意分开**（理由见 `frozen_round_seq` 注释）：
        计数器 = "用掉多少止损预算"；本标记 = "这一轮结束了"。
        Returns: -1 = **未知**（Redis 不可用/桥未升级）→ 调用方回退旧判据（不引入回归）。
        """
        if self._redis is None or not getattr(self._redis, "is_initialized", False):
            return -1
        try:
            raw = await self._redis.get(OSC_ROUND_SEQ_KEY_TMPL.format(symbol=symbol))
            return int(float(raw)) if raw else 0
        except (TypeError, ValueError):
            return -1
        except Exception:  # noqa: BLE001
            return -1

    async def _read_osc_atr_loss(self, symbol: str) -> float:
        """读累计震荡止损（ATR 倍数）。用途：判定"本轮是否已因止盈/止损结束"。"""
        if self._redis is None or not getattr(self._redis, "is_initialized", False):
            return 0.0
        try:
            raw = await self._redis.get(OSC_LOSS_KEY_TMPL.format(symbol=symbol))
            return float(raw) if raw else 0.0
        except (TypeError, ValueError):
            return 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _spike_ok(self, high: Any, low: Any, atr: float,
                  base_high: Any = None, base_low: Any = None) -> bool:
        """追涨过滤（L4 实测最优项）：当前 bar 振幅 > spike_atr_max × ATR → 放弃入场。

        只作用于**首次入场**（S2 试错 / S3 无仓首建），**不加在加仓上** ——
        加仓路径未经该口径实测，不擅自推广（避免"改了没测的地方"）。

        【B15-8 2026-09-17】"当前 bar" = **决策基准 bar**（`base_high/base_low`，由
        scheduler 在**剥离基准 bar 之前**取出并传入）。为什么不能直接用 `high[-1]`：
        2026-09-17 前视闭合修复后，`high/low` 是**已收盘**序列 ⇒ `high[-1]` 是 **T-1**
        （非本根），与本文档声明的"当前 bar 振幅"不符。
        `base_*` 为 None（调用方未提供）⇒ **回退旧口径**（行为与改动前逐位一致）。
        """
        if atr <= 0.0 or self._spike_atr_max <= 0.0:
            return True
        try:
            if base_high is not None and base_low is not None:
                rng = float(base_high) - float(base_low)      # 本根（决策基准 bar）
            else:
                rng = float(high[-1]) - float(low[-1])        # 回退：T-1（旧口径）
        except Exception:  # noqa: BLE001
            return True
        return rng <= self._spike_atr_max * atr

    async def _clear_osc_budget_if_flat(
        self, symbol: str, ctx: "StrategyContext", cur_atr_loss: float, positions_open: int
    ) -> float:
        """【规格 9.4「平完即清零」】该品种**已无持仓**且预算已用尽 ⇒ 清锁并归零计数。

        【C4 2026-09-17 · 为什么抽成独立方法并前移】原实现内联在**贴边防抖 `return`
        之后**，⇒ 未贴边的 bar（绝大多数）永远走不到 ⇒ `osc_atr_loss` /
        `osc_loss_count` 只增不减、手数档位长期顶格。抽出后可在**入面判定之前**、
        对 S0/S1/S5 统一调用（`OSC_BUDGET_STATES`）。

        与桥的写入互补：桥只增（平仓时累加），本方法只在该品种**全平**后归零 ——
        与 FSM 的清锁同一语义，且**幂等**（已清零 ⇒ 首行即返回）。

        Returns:
            清零后的 `cur_atr_loss`；未满足条件时**原样返回**。
        """
        if (self._osc_limit <= 0 or positions_open > 0
                or cur_atr_loss < self._osc_limit):
            return cur_atr_loss
        logger.warning(
            "[state_strategy] %s 震荡止损预算已用尽（%.3fATR ≥ 上限 %.2f，"
            "连续止损 %d 次）且**已无持仓** → 按规格 9.4 清锁：两个计数器归零，"
            "手数梯度回到第一档（%s）",
            symbol, cur_atr_loss, self._osc_limit, ctx.consec_losses,
            self._ladder[0] if self._ladder else 1.0)
        try:
            if self._redis is not None and getattr(
                    self._redis, "is_initialized", False):
                await self._redis.set(
                    OSC_LOSS_KEY_TMPL.format(symbol=symbol), "0.000000")
                await self._redis.set(
                    OSC_LOSS_COUNT_KEY_TMPL.format(symbol=symbol), "0")
        except Exception as _ce:  # noqa: BLE001
            logger.warning("[state_strategy] %s 清锁写 Redis 失败（本轮仍按已清零"
                           "处理，下轮会重试）：%s", symbol, _ce)
        # 本进程内立即生效（否则本轮仍按旧计数取档）
        ctx.consec_losses = 0
        return 0.0

    def _pullback_entry(
        self, tdir: str, c: float, high: Any, low: Any, w: int, atr: float
    ) -> tuple[bool, str]:
        """顺势回踩入场判定（S2 试错与 S3 无仓首建**共用**，口径唯一，避免两处漂移）。

        规则（规格 10.1「等待价格小幅回踩，顺势开仓」）：
          BUY  需自近期高点回落 ≥ pullback_atr×ATR；
          SELL 需自近期低点反弹 ≥ pullback_atr×ATR。

        Returns:
            (是否满足, 方向)；不满足时方向为 ""。
        """
        seg_h = float(max(high[-w:])) if len(high) >= w else c
        seg_l = float(min(low[-w:])) if len(low) >= w else c
        _lo = self._pullback_atr * atr
        # 【2026-09-22 新增】上界：0 = 关闭（既有行为）；>0 时"越过上界"= 深度回撤，
        #   不算顺势回踩（调用方会走触价单路径；价位在另一侧时自然放弃，见
        #   `_pullback_level` 的 `0 < lv < ref` 约束）。
        _hi = (self._pullback_max_atr * atr) if self._pullback_max_atr > 0 else float("inf")
        if tdir == "UP" and _lo <= (seg_h - c) <= _hi:
            return True, "BUY"
        if tdir == "DOWN" and _lo <= (c - seg_l) <= _hi:
            return True, "SELL"
        return False, ""

    def _pullback_level(self, tdir: str, high: Any, low: Any, w: int, atr: float,
                        ref: float) -> float:
        """**触价入场位** —— `_pullback_entry` 阈值所对应的那个价位（L4，§49）。

        必须与 `_pullback_entry` **同窗口、同阈值**（同一真值）：`_ok` 的临界价位正是
        `seg_h − pullback_atr×ATR`（UP）/ `seg_l + pullback_atr×ATR`（DOWN）。
        若各自算一份，就会出现"价已过、触价单还没到"的自相矛盾。

        Returns:
            触价价位；不可算或**该位已在当前价的另一侧**（"等待触及"无意义）→ 0.0
            （= 不下发触价，调用方退化为既有行为）。
        """
        if atr <= 0.0 or w <= 0 or len(high) < w or len(low) < w:
            return 0.0
        if tdir == "UP":
            lv = float(max(high[-w:])) - self._pullback_atr * atr
            return lv if 0.0 < lv < ref else 0.0      # 须在现价**下方**（等回落）
        if tdir == "DOWN":
            lv = float(min(low[-w:])) + self._pullback_atr * atr
            return lv if lv > ref else 0.0            # 须在现价**上方**（等反弹）
        return 0.0

    def _touch_pending_alive(self, ctx: StrategyContext) -> bool:
        """是否已有**未过期**的触价单在等（去重闸；见 `StrategyContext.touch_pending_at`）。"""
        if not ctx.touch_pending_at or self._entry_wait_sec <= 0:
            return False
        try:
            ts = datetime.fromisoformat(ctx.touch_pending_at)
        except (TypeError, ValueError):
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() < self._entry_wait_sec

    def _apply_touch_entry(self, it: StrategyIntent, ctx: StrategyContext, tdir: str,
                           high: Any, low: Any, w: int, atr: float, c: float) -> str:
        """未回踩到位时布置"触价入场"（L4，§49）。

        Returns:
            `"armed"` 本次已布置（调用方应产出 `action="open"` 意图，带 `zone_level`）；
            `"pending"` 已有未结触价单在等 → **不重复下发**（去重）；
            `""`      未启用 / 价格不可算 → 调用方按既有行为处理。
        """
        if self._entry_mode != "zone_touch" or self._entry_wait_sec <= 0:
            return ""
        if self._touch_pending_alive(ctx):
            return "pending"
        lv = self._pullback_level(tdir, high, low, w, atr, c)
        if lv <= 0.0:
            return ""
        it.zone_level = lv
        it.entry_wait_sec = self._entry_wait_sec
        ctx.touch_pending_at = datetime.now(timezone.utc).isoformat()
        ctx.touch_pending_dir = "BUY" if tdir == "UP" else "SELL"
        return "armed"

    # ── 主裁决 ─────────────────────────────────────────────
    async def decide(
        self,
        symbol: str,
        fsm_state: str,
        *,
        high: Any,
        low: Any,
        close: Any,
        atr: float,
        slope: float,
        positions_open: int = 0,
        hold_only: bool = False,
        # 【2026-09-19 阶段1】弃权闸（**按 bar 的一次性信号**，默认 False）：
        #   True = 本根**禁止新开与加仓**，持仓保留；**离场/尾随/破箱止损完全不受影响**。
        # 为什么不能复用 `hold_only`：它是**持久标志**，且"无持仓即自动解除"
        #   （`state_machine` 第 7b 步），**无法表达"空仓也禁新开这一根"**。
        # 闸点 = 本函数内**全部 4 处会产生 `action ∈ {open, add}` 的入口**（见各处注释）；
        #   刻意**不用"提前 return"**：那会跳过破箱离场指令与 S4 的尾随收紧（放宽既有刹车）。
        # 生效开关在 `state_infer`（`state.abstain.*`，默认关闭）。
        abstain: bool = False,
        direction: str = "",
        age_bars: int = 0,
        shape: str = "",
        position_dir: str = "",
        fsm_adds_used: int = -1,
        vol_expand_proba: float = -1.0,
        # 【P2 2026-09-19】vol 预测是否为 **conformal 单例**（高置信）。
        #   True = 可信；False = 不可信；**None = 未知**（α≤0 / 缺 conformal 产物）。
        # 语义：只有 `True` 才允许"高波动 ⇒ 拦箱体单"生效（见 `osc_skip_require_singleton`）；
        #   **None 一律不拦**（不裁决 ⇒ 保持既有行为，绝不因调用方漏传而加码限制）。
        # 刻意不加类型标注：本文件不保证已导入 `Optional`，标注会在定义期求值。
        vol_route_singleton=None,
        # 【2026-09-17 A】上一根 bar 的 FSM 状态：供"刚离开趋势态 ⇒ S0 不出箱体单"的护栏
        # （唯一消费者 = `state.osc_idle_block_after_trend`；缺省 "" ⇒ 视为未知、**不放行**
        #  S0 箱体单，即"绝不因调用方漏传而静默放宽闸门"）。
        prev_state: str = "",
        # 【B15-8 2026-09-17】决策基准 bar（**本根**）的 high/low：`high/low` 入参是
        #   **已收盘**序列（`high[-1]` = T-1），而 `_spike_ok` 要判的是"本根是否插针"，
        #   故由调用方单独传入。**None = 未提供 ⇒ 回退旧口径**（向后兼容、行为不变）。
        base_high: Any = None,
        base_low: Any = None,
        # 【P1-7 2026-09-18】被评估的**已收盘 bar 身份**（`osc_edge_streak` /
        #   `osc_break_streak` 的**同 bar 去重**键，详见 `StrategyContext.osc_edge_bar`）。
        #   **"" = 未提供 ⇒ 回退旧行为**（每次调用都推进计数，向后兼容）。
        bar_id: str = "",
    ) -> StrategyIntent:
        """产出交易意图（**不下单**；是否下单由调用方按 order_enabled 决定）。

        Args:
            fsm_state: MarketState 的值（S0_IDLE / S1_OSC / ... ）。
            high/low/close: 已收盘 K 线序列（用于入场箱体与回踩判定）。
            atr: 当前 ATR（用于容差/回踩换算）。
            slope: 线性回归斜率（`state_features` 的 slope_linreg）——**仅作回退**：
                方向以 `direction` 为准（见 `resolve_trend_dir`）。
            positions_open: 该品种真实持仓数（**影子期为 0/近似值**，见模块注释）。
            hold_only: FSM 的 hold_only 标志（停止新开/加仓但持仓保留）。
            abstain: 【阶段1】本根弃权（True = 禁止新开/加仓，持仓保留）。
                与 `hold_only` 正交、**不持久**；来源 = `state_infer` 的校准/conformal 弃权闸
                （默认关闭；依据与验收门见 `state_infer.DEFAULTS` 的 `state.abstain.*`）。
            direction: 方向模块结果 up/down/none（`trend_direction`，**单一真值**）。
                **"none" → 禁止趋势开仓**（用户规格：规避方向模糊的假趋势）。
            age_bars: 状态年龄（FSM 自计，§25）——承载"初生/中段"。
                当前**只记录不裁决**：S2/S3 的区分仍由 FSM 状态承担（两者语义已足够），
                年龄留给后续"加仓梯度/收紧紧紧度"使用（避免在此引入未标定的阈值）。
            shape: 形状描述器结果（quiet/advancing/exhausted）。
                ⚠ **当前只记录、不裁决**：形状推理尚未接线，且其 `exhausted` 命名
                在 §24.3 实测中未获语义确认 —— **不得用未验证的标签去拦截订单**。
            position_dir: 当前持仓方向（"BUY"/"SELL"；空 = 调用方未提供）。
                用途：S3「**禁逆势加仓**」（规格 10.2）。此前只有 `positions_open`
                这个**数量**，无法判断持仓是顺势还是逆势 → "禁逆势"实际无法执行。
                提供后本模块会拦下"加仓方向与持仓方向相反"的意图。
                ⚠ 未提供（空）时**不拦**（保持向后兼容，但该约束即为空转）。
            fsm_adds_used: 本轮趋势**已成交**的加仓笔数（= FSM 真实持仓数 − 1）。
                **-1 = 调用方未提供（未知）**。提供时**以它为权威**替代自增计数。
                为什么必须由外部给（2026-09-15）：原实现是在**产生意图**时 `add_count += 1`，
                而意图可能被风控/限仓拦下不发单 → 计数与真实持仓脱钩 →
                随后被 `mid_max_adds_reached` **提前封顶**，本轮加仓机会被静默吃掉。
                （(d) 选定"不豁免持仓上限"后，这条更易发生。）
                ⚠ 未提供（-1）时回退自增计数 —— 那只是**近似**，影子/离线场景可接受。
            vol_expand_proba: 【路线 B · 2026-09-16】P(未来窗口振幅/ATR ≥ 阈值)，由
                推理层 `state_infer.infer_vol_proba` 经调度器透传（**不新增真值源**）。
                用途：S1 箱体「**波动扩张闸**」—— 箱体是逆势策略，其前提是波动收敛；
                波动开始扩张时边界被有效突破的概率上升，此时逆势开仓即把亏损源放进组合。
                **-1.0 = 未提供 ⇒ 不裁决**（保持既有行为）；闸本身由
                `state.vol.osc_skip_prob`（< 0 = 关闭，默认）控制。
                ⚠ 它是"**同步**预警"（验收门中位 +0.5），**不是提前预测**
                —— 因此它用于"拦掉已开始扩张的入场"，不可当作"提前布局"的依据。
        """
        ctx = await self._load_ctx(symbol)
        # 梯度手数档位以桥侧计数器为**单一真值**（读透；真值见模块 docstring）
        ctx.consec_losses = await self._read_osc_loss_count(symbol)
        _cur_atr_loss = await self._read_osc_atr_loss(symbol)
        # 【2026-09-16】显式"轮次结束"标记（-1 = 未知 → 回退旧判据，见 frozen_round_seq）
        _cur_round_seq = await self._read_osc_round_seq(symbol)
        # 加仓笔数以**真实 FSM 持仓数**为权威（提供时）；未提供则保留自增（近似）
        if int(fsm_adds_used) >= 0:
            ctx.add_count = max(0, int(fsm_adds_used))
        w = self.box_window_for(symbol)
        # 回踩窗口：与箱体窗口**解耦**（配置 0 = 跟随箱体窗口，保持既有行为）
        w_pull = self._pullback_window if self._pullback_window > 0 else w
        up, lo, mid = compute_entry_box(high, low, w, bands_mode=self._bands_mode,
                                        q_high=self._q_high, q_low=self._q_low)
        c = float(close[-1]) if len(close) else 0.0
        it = StrategyIntent(
            symbol=symbol, state=fsm_state, box_upper=up, box_lower=lo, box_mid=mid,
            order_enabled=self._order_enabled, trail_lookback=self._trail_lookback,
            age_bars=int(age_bars or 0), shape=str(shape or ""),
        )
        if up <= 0.0 or atr <= 0.0:
            it.reason = "no_box_or_atr"
            return it

        # ── 【2026-09-17 B】箱体入场态集合（**唯一真源**：入场面与轮次生命周期共用）──
        # `state.osc_in_idle`（默认 True）把 S0_IDLE 也放开为箱体入场态。该标志**必须被
        # 两处共用** —— 否则就是"半截修复"：入场面放开了、生命周期还只认 S1 ⇒ 实测 07:15
        # 在 S0 开单、07:20 转 S2 时立刻被判"结束震荡轮次、解冻箱体" ✗。
        # （原先该读取在下方 S1 分支内，生命周期看不到 ⇒ 本次上提到这里。）
        # 【可观测性 2026-09-18】改取 `load_config()` 热载的**实例属性**（原为每 bar
        # 即时读取）：同一份真值现在同时进配置签名 / 启动日志 / panel `tuning`，
        # 解决"改了它也完全看不见"（本仓库反复踩的盲区）。
        # **保守语义逐位保留**：属性初值 False ⇒ 热载成功前 S0 不放行箱体单，
        # 与原"读配置失败 → 取 False（S0 不出单）"的取舍一致。
        _osc_in_idle = self._osc_in_idle
        _BOX_ENTRY_STATES = (("S1_OSC", "S0_IDLE") if _osc_in_idle else ("S1_OSC",))

        # ── 冻结箱体生命周期（规格 §7.1：止盈 / 止损 / **状态切换** 任一即结束本轮）──
        # 此前只有"止盈"路径会解冻（且该路径无调用者）→ 一旦冻结就永久沿用旧箱体：
        # 后续每轮都用已经失效的 mid 作 TP 锚点（偏离真实箱体中值，止盈位错）。
        # 状态切换在此判定：本模块每根 bar 都拿到当前 fsm_state，无需额外事件源。
        # 【2026-09-17 B 修复·孤儿单】★ 追加 `positions_open <= 0`：原实现只要"离开 S1"
        #   就解冻并置 `osc_round_active=False`，而该标志是**破界离场**的判据之一
        #   （`break_streak >= N **and** osc_round_active`）⇒ 持仓期间被清 ⇒
        #   ① 冻结中值（TP 锚点）丢失；② **离场指令永久不再下发**（实测 07:15 开的 0.03 空单，
        #   07:20 被"解冻"后价格突破上沿 4 根（阈值 2）却一次离场都没发，浮亏 -47 且持续扩大）
        #   ⇒ 持仓被"孤儿化"，只剩会话 SL 兜底。
        #   修法与**趋势侧先例**（上方"趋势轮次生命周期"）完全一致：**持仓仍在时不清**。
        if (ctx.box_frozen and fsm_state not in _BOX_ENTRY_STATES
                and positions_open <= 0):
            logger.info("[state_strategy] %s 离开箱体入场态（→ %s，无持仓）"
                        "→ 结束震荡轮次、解冻箱体", symbol, fsm_state)
            ctx.box_frozen, ctx.box_frozen_at, ctx.osc_round_active = False, "", False
            await self._save_ctx(ctx)

        # 冻结箱体：本轮开仓后 TP 锚点必须锁定，否则中值随价格滑动 → 止盈目标失效
        if ctx.box_frozen and ctx.box_mid > 0:
            it.box_upper, it.box_lower, it.box_mid = ctx.box_upper, ctx.box_lower, ctx.box_mid
        else:
            # ── 【2026-09-16 观测修复 v2（纠正 v1 的过度纠正）】──
            # v1 曾在此处"**未冻结就清零** `ctx.box_*`"，目的是消除"陈旧箱体被误读为
            #   当前箱体"（实测事故：`ctx.box=[4296.171, 4310.396]` vs 滚动箱
            #   `[4281.118, 4298.941]`，差 5.7 ATR，把排查带偏）。**但用"删数据"解决是错的**：
            #   面板/接口读 `hcm:state:ctx:{symbol}` ⇒ **箱体三线直接消失**（用户当即发现）。
            # v2 正解：未冻结时把**本 bar 刚算出的滚动箱**写进 ctx（每 bar 刷新）——
            #   ① 面板始终有箱体可看；② 写的是"当前"滚动箱 ⇒ **不存在陈旧值**（原问题自然消失）；
            #   ③ 配合 `box_frozen` 标志，读的人能明确区分"滚动箱 / 冻结快照"。
            # 安全性：`ctx.box_*` 只在 `box_frozen=True` 时被当作冻结快照读取（上面那个分支），
            #   故未冻结时写滚动箱**不可能**污染冻结语义。
            # ⚠ **必须落盘**：`decide` 在 S0/S5 等分支会**提前 return 且不 save**
            #   ⇒ 只改内存的话 `ctx.updated_at` 长期不动、面板读到的仍是旧值
            #   （2026-09-16 实测：改了内存但 `updated_at` 停在前 30 分钟 ⇒ 箱体仍不显示）。
            #   仅在**箱体确实变化**时写，避免每 bar 无谓写 Redis。
            if (ctx.box_upper, ctx.box_lower, ctx.box_mid) != (up, lo, mid):
                ctx.box_upper, ctx.box_lower, ctx.box_mid = up, lo, mid
                await self._save_ctx(ctx)
        # 面板/接口据此区分"滚动箱（未冻结）"与"冻结快照（本轮锁定）"
        it.box_frozen = bool(ctx.box_frozen)

        tol = self._border_tol_atr * atr

        # ── 震荡轮次结束：止盈 / 止损 → 解冻箱体（规格 §7.1 三者之一）──
        # 【2026-09-15 回放发现的 gap】此前只处理了"状态切换"这一条，**止盈/止损后不解冻**
        # → 在 S1 长时间不切换状态时，冻结的旧 mid 会被后续每一轮一直沿用（TP 锚点是失效值）。
        # 判据用"计数器是否变化"：止盈/止损由桥写进这两个计数器，本模块无需新事件通道。
        # 【2026-09-16 修 be-不解冻缺口】`_cur_round_seq` 是**显式**的"轮次结束"标记
        # （桥每次 FSM 震荡平仓 INCR，**无论归因**）⇒ 对 `be`/`manual`/`expert`/`stop_out`
        # 也有效；而下面两个计数器对它们**不变**（不计入预算）。
        # 二者**刻意并存**：桥未升级时 `_cur_round_seq = -1` ⇒ `_seq_changed` 恒 False
        # ⇒ 行为与修复前**逐位一致**（无回归）；升级后补齐 `be` 一类漏判。
        _seq_changed = (_cur_round_seq >= 0 and ctx.frozen_round_seq >= 0
                        and _cur_round_seq != ctx.frozen_round_seq)
        # 【2026-09-17 B9 修复·孤儿单】★ 追加 `positions_open <= 0`（与上方路径 A 对齐）：
        #   本判据只认"计数器变化"，**不校验持仓** ⇒ 多仓下平掉任意一仓即写计数器
        #   （`consec_losses`/`osc_atr_loss`/`_cur_round_seq`），于是 `osc_round_active`
        #   被清 ⇒ 破界离场判据（`break_streak >= N **and** osc_round_active`）永不成立，
        #   剩余持仓被"孤儿化"，只剩会话 SL 兜底。
        #   修法：持仓仍在时**不解冻**；待最后一仓平完（positions_open==0）再正常解冻。
        if (ctx.box_frozen and positions_open <= 0
                and (ctx.consec_losses != ctx.frozen_loss_count
                     or _cur_atr_loss != ctx.frozen_atr_loss
                     or _seq_changed)):
            logger.info("[state_strategy] %s 震荡本轮结束（计数 %s→%s / %.3f→%.3f / "
                        "轮次标记 %s→%s，止盈或止损）→ 解冻箱体，下一轮重算",
                        symbol, ctx.frozen_loss_count, ctx.consec_losses,
                        ctx.frozen_atr_loss, _cur_atr_loss,
                        ctx.frozen_round_seq, _cur_round_seq)
            # 【2026-09-16 观测修复】解冻时**清空箱体残留**：未冻结时 `ctx.box_*` 不再
            #   代表"当前箱体"（当前箱体是每 bar 重算的滚动箱），留着旧值会被面板/目视
            #   误读成"价格离箱体很远" —— 2026-09-16 实际发生过：ctx 箱
            #   [4296.171, 4310.396] vs 滚动箱 [4281.118, 4298.941]，相差 5.7 ATR，
            #   把排查方向完全带偏。清零后 `ctx.box_mid > 0` 的既有守卫天然生效。
            ctx.box_frozen, ctx.box_frozen_at, ctx.osc_round_active = False, "", False
            ctx.frozen_round_seq = -1
            # 【2026-09-16 观测修复 v2】**不再清零箱体**：解冻后本函数紧接着会把
            #   **当前滚动箱**写回 ctx（见上方"冻结箱体"分支的 else），故面板不会断档。
            #   v1 的清零会让箱体三线在面板上消失（用户当即发现），已撤回。
            await self._save_ctx(ctx)

        # ══════════════════════════════════════════════════════════════════════
        # ── 【马丁补仓 2026-09-18 · 用户决策】SL 后「及时」同向补下一档 ──────────
        # 需求（用户原话）："0.5 止损后及时下 1.0、不是等行情变化后再接着下阶梯手数"。
        # 缺陷（2026-09-18 复盘）：SL 后箱体被**解冻 + 滚动箱重建** ⇒ 重入场必须等价格
        #   回到【新箱体】边缘（`osc_outside_box` / `osc_inside_box`），中间可空很久
        #   （实测 09:41 的 0.5 止损 → 10:45 才补 1.0，空 64 分钟）＝用户说的"等行情变化"。
        # 本块把"阶梯递进"与"箱体重建/FSM 状态"**解耦**：一旦本轮因**止损**结束且已平仓，
        #   下一根 bar 直接用**本轮入场方向**市价补下一档（`ladder[连续止损次数]`）。
        #   · 方向 = `ctx.osc_last_dir`（本轮入场方向）。用户选择「经典马丁·同向补」：
        #     0.5 做多止损 → 1.0 继续做多（平均价下移），方向锚定在被止损的那一笔。
        #   · 判据 = `ctx.consec_losses > ctx.frozen_loss_count`（本轮冻结快照）：
        #     止盈会把 count 归零、be/expert count 不变 ⇒ 均不触发；
        #     **只有真止损才 +1** ⇒ 精确锚定"止损之后"。
        #   · 位置纪律：放在**两处解冻判定之后**（`box_frozen` 已清）+ 所有
        #     箱体分支 `return` 之前 ⇒ 无论 FSM 处于 S0/S1 还是已切趋势态，都能补。
        # ⚠ 安全边界（**不放宽任何既有刹车**）：
        #   ① 必须 `positions_open<=0`（已平仓）才能补；
        #   ② `state.osc_atr_loss_limit`（4ATR 预算）为**硬刹车**：预算已用尽 ⇒ 不补；
        #   ③ 档位仍由 `state.osc_lot_ladder` 封顶（`min(count, len-1)`）；
        #   ④ 下游风控 **全部照常生效**（`risk.max_lot_per_trade` / `max_concurrent_signals`
        #      / 同向保本闸门 / 置信度闸门）—— 本块只产生"意图"，不放行任何风控。
        # 幂等：补仓即冻结**新一轮**并把 `frozen_loss_count` 快照更新为当前 count
        #   ⇒ 同一笔止损**只补一次**（下一笔止损才会再次触发）。
        # 回滚：`set_cfg.py state.osc_martingale_enabled false`（秒级热生效，无需重启）。
        # ══════════════════════════════════════════════════════════════════════
        if (self._osc_ma_enabled and positions_open <= 0
                # 【2026-09-19 阶段1】弃权闸点 ①（马丁补仓是本函数内**最早的**入场点）
                and not abstain
                and ctx.consec_losses > ctx.frozen_loss_count
                and ctx.osc_last_dir in ("BUY", "SELL")
                and _cur_atr_loss < self._osc_limit):
            _ma_dir = ctx.osc_last_dir
            _ma_idx = min(max(ctx.consec_losses, 0), len(self._ladder) - 1)
            it.action, it.direction = "open", _ma_dir
            it.reason = "osc_martingale_sl"
            it.lot_multiplier = self._ladder[_ma_idx] if self._ladder else 1.0
            it.tp_anchor = it.box_mid if it.box_mid > 0 else None
            # 冻结当前滚动箱（与常规开仓**同构**）：保留 TP 锚点 + 破界离场护栏 + 轮次快照
            ctx.box_upper, ctx.box_lower, ctx.box_mid = up, lo, mid
            ctx.box_frozen = True
            ctx.box_frozen_at = datetime.now(timezone.utc).isoformat()
            ctx.osc_round_active = True
            it.box_upper, it.box_lower, it.box_mid = up, lo, mid
            it.box_frozen = True
            ctx.frozen_loss_count, ctx.frozen_atr_loss = ctx.consec_losses, _cur_atr_loss
            ctx.frozen_round_seq = _cur_round_seq
            ctx.osc_edge_streak = 0
            ctx.osc_break_streak = 0
            ctx.osc_last_dir = _ma_dir
            await self._save_ctx(ctx)
            logger.warning(
                "[state_strategy] %s 震荡马丁补仓：上轮 %s 止损（连亏 %d 次）→ 同向 %s 补第 %d 档"
                "×%.2f（不等箱体重建/不等状态；预算 %.3f/%.2f；tp_anchor=%s）",
                symbol, _ma_dir, ctx.consec_losses, _ma_dir, _ma_idx,
                it.lot_multiplier, _cur_atr_loss, self._osc_limit,
                f"{it.tp_anchor:.3f}" if it.tp_anchor else "-")
            return it

        # ── 趋势轮次生命周期（与 S1 冻结箱体同构）──
        # 【必须放在所有状态分支**之前**】2026-09-15 回放（replay_state_chain.py）抓到：
        # 原先该块位于 S1/S5/S9 分支之后，而那些分支会 `return` 提前退出
        # → 离开趋势态后残留的 `trend_dir`/`add_count` **清不掉**（实测 3 次）。
        # 条件本身已限定为"非趋势态且无持仓"，故提前执行不会误清任何在途轮次。
        # 持仓仍在时**不清**：规格 10.2 是"停止加仓、持仓保留"，该仓真正平掉才算本轮结束；
        # 否则 add_count 被重置会让同一仓位的加仓次数突破 max_adds。
        if (fsm_state not in TREND_STATES and positions_open <= 0
                and (ctx.trend_dir or ctx.add_count or ctx.touch_pending_at)):
            logger.info("[state_strategy] %s 趋势轮次结束（→ %s，无持仓）"
                        "→ 清除锁定方向/加仓计数/未结触价单", symbol, fsm_state)
            ctx.trend_dir, ctx.add_count = "", 0
            # 未结触价单也一并作废：桥侧 deferred 的 Redis TTL 会自行到期，
            # 但本地去重闸若不清，会把**下一轮**的触价单误判为"重复"而拒发。
            ctx.touch_pending_at, ctx.touch_pending_dir = "", ""
            await self._save_ctx(ctx)

        # ── 【C4 2026-09-17】震荡止损预算"平完即清零"**前移**（规格 9.4）────────
        # 原实现位于**贴边防抖 `return` 之后**（见下方贴边块内）⇒ 未贴边时
        # （`osc_edge_wait` / `osc_inside_box` / `osc_outside_box`，即绝大多数 bar）
        # **永远走不到** ⇒ `osc_atr_loss` / `osc_loss_count` 只增不减、手数档位长期
        # 顶格（用户实测"新轮首单直接 0.03"的另一半原因）。
        # 位置纪律：必须在**任何入面 return 之前**；作用域：S0/S1/S5 且**无持仓**时清零。
        # 有持仓的"封堵"（不出新箱体单）**仍保留在贴边处** —— 它依赖贴边上下文，不可前移。
        # 幂等：已清零则 `_cur_atr_loss < _osc_limit` ⇒ 直接返回，无副作用、每 bar 可调。
        # 回滚：`state.osc_atr_loss_limit` 调大（或 ≤0 = 关闭本闸）——秒级，无需改码/重启。
        if fsm_state in OSC_BUDGET_STATES and positions_open <= 0:
            _cur_atr_loss = await self._clear_osc_budget_if_flat(
                symbol, ctx, _cur_atr_loss, positions_open)

        # ══════════════════════════════════════════════════════════════════════
        # 【§57-12.3-2】箱体突破**逻辑止损**（规格："震荡一旦被突破…箱体逆势单不能扛单"）
        # ⚠ **位置纪律**：必须在 S1 分支的**任何 return 之前** —— 否则 `osc_inside_box`
        #   一 return，破界永远检不到，规格最关键的一条会**静默失效**。
        # ⚠ **作用域**：只对进行中的箱体轮次（`osc_round_active`）下发，且 `scope="osc"`
        #   → 桥**只平 magic 61**，不波及同品种的趋势单（62）。
        # ⚠ **关闭条件**：`osc.break_confirm_bars = 0`（默认）→ 整段不执行，行为与改动前逐位一致。
        # ══════════════════════════════════════════════════════════════════════
        _bh = it.box_upper - it.box_lower
        if _bh > 0.0 and self._break_confirm > 0:
            _side = ("below" if c < it.box_lower
                     else "above" if c > it.box_upper else "")
            # ── 【P1-7 2026-09-18】**同 bar 去重**（详见 `StrategyContext.osc_break_bar`）──
            # 同一根 bar 被重复评估（`_run_shadow_state` 已知会跑两次）时**不重复推进**；
            # 但下方"是否已达确认根数"的触发判定**在守卫之外**照常执行 —— 已达标必须继续
            # 下发离场指令，不得因去重而卡住（那会让该保护静默失效）。
            if not (bar_id and bar_id == ctx.osc_break_bar):
                _brk_before = (ctx.osc_break_side, ctx.osc_break_streak)
                if _side and _side == ctx.osc_break_side:
                    ctx.osc_break_streak += 1
                elif _side:
                    # **换向即重置**：否则"先破下沿 1 根、再破上沿 1 根"会被误累加成连续 2 根
                    ctx.osc_break_side, ctx.osc_break_streak = _side, 1
                else:
                    ctx.osc_break_side, ctx.osc_break_streak = "", 0
                ctx.osc_break_bar = bar_id
                # 【落盘】非 S1 时**下面的 S1 分支不会执行** → 若只在那里落盘，破界进度
                # 会在"离开 S1 但箱体轮次仍活跃（冻结）"期间丢失（重启即从 0 重算，
                # 使"差一根就止损"的判定被推迟）。仅在**计数确实变化**时写，避免每 bar 写 Redis。
                if (ctx.osc_break_side, ctx.osc_break_streak) != _brk_before:
                    await self._save_ctx(ctx)
            it.break_streak = ctx.osc_break_streak
            if (ctx.osc_break_streak >= self._break_confirm
                    and ctx.osc_round_active):
                it.exit_now, it.exit_scope = True, "osc"
                it.exit_ready = True          # 同时置"准备离场"（收紧止损）→ 双保险
                it.exit_reason = f"band_break_{_side}"
                logger.warning(
                    "[state_strategy] %s 箱体突破止损：连续 %d 根收盘破%s沿 "
                    "(close=%.3f band=[%.3f, %.3f]) → 指令桥立即平 magic 61",
                    symbol, ctx.osc_break_streak,
                    "下" if _side == "below" else "上", c, it.box_lower, it.box_upper)

        # ── S1 震荡：箱体边界逆势（规格 12.2 高抛低吸）──
        # 【2026-09-17 C】S0_IDLE 也允许出箱体单（开关 `state.osc_in_idle`，默认 True）。
        # 动机（实测 2026-09-17）：HEXP 家族停用后仅剩 FSM 供单，而模型在"衰竭段"连续判
        # `trend_fade`（margin 0.25~0.70）⇒ FSM 在 S4（设计禁新单）↔ S0（原本也禁新单）
        # 之间空转，02:00~02:55 连续 11 根零开仓意图 ⇒ 全系统无单。
        # S0 是"未分类/复位"态，其箱体判定与 S1 **同源**（同一个 `compute_entry_box` +
        # 同一个 ctx 轮次机 + 同一份破界止损），放开后仍受**全部既有护栏**：
        # 箱宽下限、贴边防抖（`osc.entry_confirm_bars`）、同向仅 1 单、破界止损、
        # 梯度手数、以及下游全部风控闸门。
        # 回滚：`set_cfg.py state.osc_in_idle false`（秒级，无需重启/改码）。
        # 【2026-09-17 A 新增·趋势污染护栏】S0 是"未分类/复位"态，其**最常见来源正是
        #   S4_TREND_FADE 之后**（实测 07:10 S4 → 07:15 S0 → 07:20 S2）。刚离开趋势态就在
        #   S0 按箱体逆势入场 = **在趋势里反向开仓**（07:15 那笔：S0 开 SELL 0.03 @4324.17，
        #   随后连涨，浮亏 -47）。故：**S0 且上一状态 ∈ 趋势态**时不出箱体单。
        #   开关 `state.osc_idle_block_after_trend`（默认 true；回滚 = 设 false）。
        #   S1 不受影响 —— S1 本身就是"震荡判定"的结论，不存在该污染。
        # 【2026-09-19 阶段1】弃权闸点 ②（S0/S1 箱体单）
        if (fsm_state in _BOX_ENTRY_STATES and not hold_only and not abstain):
            # 【P1-6 F7 2026-09-18】S0 护栏**窗口化**：原只看 `prev_state` ⇒ 仅拦 S4→S0 当根
            #   1 根，次根 prev=S0 即放行（审计 F7）。现改为"离开趋势态后连续 N 根"：
            #   推进（每 bar 一次）：prev∈趋势态 → 重置为 N 并拦；否则递减，>0 则拦。
            #   N=`state.osc_idle_block_bars`（默认 3；0 = 退回原"仅拦 1 根"语义）。
            #   S1_OSC 不受影响（它本身即"震荡判定"的结论，无趋势污染）。
            if self._osc_idle_block_after_trend and fsm_state != "S1_OSC":
                _guard_before = ctx.osc_idle_guard_left
                _transition = str(prev_state or "") in TREND_STATES
                if _transition:
                    ctx.osc_idle_guard_left = self._osc_idle_block_bars
                elif ctx.osc_idle_guard_left > 0:
                    ctx.osc_idle_guard_left -= 1
                if ctx.osc_idle_guard_left != _guard_before:
                    await self._save_ctx(ctx)   # 窗口进度必须跨 bar 持久（否则永不递减）
                if _transition or ctx.osc_idle_guard_left > 0:
                    it.reason = "osc_idle_after_trend"
                    return it
            # 箱体退化保护（理由见 DEFAULTS）：width < 2×tol 时 `c<=lo+tol` 与
            # `c>=up-tol` **同时成立** → 方向由 if/elif 顺序决定（等价随机），必须拦。
            if _bh < self._box_min_width_atr * atr:
                it.reason = "osc_box_too_narrow"
                await self._save_ctx(ctx)     # 破界计数也要落盘（否则跨 bar 丢进度）
                return it
            # 【2026-09-23】箱体**宽度上限**（`0.0` = 关闭，默认零行为变更；依据见 DEFAULTS）：
            # 宽箱 = "假箱体"（波动放大/趋势的代理）⇒ 逆势单易被突破。
            # 与上面"下限"对称：**下限防"方向二义性"，上限防"在趋势里逆势开仓"**。
            if self._box_max_width_atr > 0.0 and _bh > self._box_max_width_atr * atr:
                it.reason = "osc_box_too_wide"
                await self._save_ctx(ctx)
                return it
            # ── 入场触发区（规格 12.2「价格缓冲 buffer」；两种口径）──
            #   atr（**默认 = 既有行为**）：`close <= lo + tol_atr`（ATR 归一 → 跨品种可移植）
            #   pct（规格字面）：`close <= lo*(1+buffer)` / `close >= up*(1-buffer)`
            #   ⚠ 百分比口径**随价位量级漂移**（同 `trend_direction` 对斜率归一的理由），
            #     故做成**可选模式**而非直接替换既有归一化。
            if self._buffer_mode == "pct":
                _lo_t = it.box_lower * (1.0 + self._buffer_pct)
                _up_t = it.box_upper * (1.0 - self._buffer_pct)
            else:
                _lo_t, _up_t = it.box_lower + tol, it.box_upper - tol
            # 【2026-09-17 C 修复·贴边判据由"单边"改"双侧"】原为单边（`c <= lo+tol` /
            #   `c >= up-tol`）⇒ 价格**已突破**边界（远在箱外）仍判"贴上沿" ⇒ 在突破途中
            #   逆势抄顶/抄底（实测事故：box=[4292.22, 4315.26]，成交 4324.17 = 上沿之上 8.9 点）。
            # 现为双侧：越界容忍 = `state.osc_edge_max_overshoot_atr × ATR`（默认 **0.0**
            #   = 必须仍在箱内）⇒ "贴边" = 靠近边界**且尚未越界**。
            _ovs = self._edge_max_overshoot_atr * atr
            _at_lower = (_lo_t >= c >= it.box_lower - _ovs)
            _at_upper = (_up_t <= c <= it.box_upper + _ovs)
            # ── 入场防抖（规格 12.2「满足防抖 K 线校验」）──
            # 连续 N 根满足**同侧**边界条件才允许开仓。为什么需要：单根插针即触发是
            # "抄底摸顶被反向收割"的直接来源，连续确认能把一次性插针滤掉。
            # 默认 1 = 与既有行为逐位一致（单根即触发）。
            # 【P1-7 2026-09-18】**同 bar 去重**（同 `osc_break_streak`，见 ctx.osc_edge_bar）：
            #   同一根 bar 被重复评估时**不重复推进**防抖进度（否则 `entry_confirm=N` 会被
            #   N/2 根满足）。`osc.entry_confirm_bars` 默认 1 ⇒ 现值下无行为差异，
            #   但一旦调大（或与破界共用同一根 bar 的重复调用）即必须正确。
            if not (bar_id and bar_id == ctx.osc_edge_bar):
                ctx.osc_edge_streak = (ctx.osc_edge_streak + 1) if (_at_lower or _at_upper) else 0
                ctx.osc_edge_bar = bar_id
            it.edge_streak = ctx.osc_edge_streak
            if ctx.osc_edge_streak < self._entry_confirm:
                # `osc_edge_wait` = 贴边但防抖未满；`osc_inside_box` = 仍在箱内（规格 9.2）
                # 【2026-09-17 C】新增 `osc_outside_box` = **已在箱外（突破）** —— 双侧判据
                #   拦下的正是这一类；不区分会让人把"突破被拦"误读成"价格在箱内"（与事实相反）。
                it.reason = ("osc_edge_wait" if (_at_lower or _at_upper)
                             else ("osc_outside_box"
                                   if (c > it.box_upper or c < it.box_lower)
                                   else "osc_inside_box"))
                await self._save_ctx(ctx)
                return it
            # ── 【D2 2026-09-17 治本】震荡止损预算闸 + 规格 9.4「平完即清零」──────────
            # 缺陷 A（预算被绕过）：4ATR 预算此前**只在 FSM 处于 S1_OSC 时**被判
            #   （state_machine.decide：`if cur == S1_OSC and osc_atr_loss >= limit → S5`）。
            #   而 S0_IDLE 现在也允许出箱体单 ⇒ 状态是 S0 时预算即便已用尽也照开
            #   （实测 `osc_atr_loss=4.814 ≥ limit=4.0`，07:15 仍开 SELL 0.03）⇒ 防爆仓失效。
            # 缺陷 B（计数器永不复位 ⇒ 手数档位长期顶格）：规格 9.4 的"清锁"只在 FSM
            #   自身处于 S5 时执行，而 **S5 只能从 S1_OSC 进入** ⇒ S0 出的箱体单永远推进不到
            #   S5 ⇒ `osc_atr_loss` / `osc_loss_count` **只增不减** ⇒ 手数档位长期停在最高档
            #   （这正是用户实测"新轮首单直接 0.03"的另一半原因）。
            # 本处按规格原文补全：**该品种全部持仓平完 ⇒ 清锁止并归零计数**（与桥的
            #   "只增"写入互补；清零是 docstring 允许的例外，且与 FSM 的清锁同一语义、幂等）。
            #   用量化证据驱动，不依赖 FSM 是否进过 S5。
            # 回滚：把 `state.osc_atr_loss_limit` 调大（或 ≤0 = 关闭本闸）即可。
            if self._osc_limit > 0 and _cur_atr_loss >= self._osc_limit:
                if positions_open <= 0:
                    # 【C4 2026-09-17】清锁逻辑已抽为 `_clear_osc_budget_if_flat` 并**前移**到
                    # 入面判定之前（未贴边的 bar 也必须能清锁）；此处保留调用以免出现第二份
                    # 实现（铁律第十三章：同一语义只有一个实现点）。幂等：通常此时已清零。
                    _cur_atr_loss = await self._clear_osc_budget_if_flat(
                        symbol, ctx, _cur_atr_loss, positions_open)
                else:
                    it.reason = "osc_atr_locked_no_new_order"
                    it.lot_multiplier = 0.0
                    logger.warning(
                        "[state_strategy] %s 震荡止损预算已用尽（%.3fATR ≥ 上限 %.2f，"
                        "连续止损 %d 次）→ 不出新箱体单（持仓保留、沿用既有 SL，规格 9.4）",
                        symbol, _cur_atr_loss, self._osc_limit, ctx.consec_losses)
                    await self._save_ctx(ctx)
                    return it
            if _at_lower:
                it.action, it.direction, it.reason = "open", "BUY", "osc_at_box_lower"
            else:
                it.action, it.direction, it.reason = "open", "SELL", "osc_at_box_upper"
            # 梯度手数：连续止损次数 → 倍率（计数真值在 Redis，由桥在平仓时归零/递增）
            idx = min(max(ctx.consec_losses, 0), len(self._ladder) - 1)
            it.lot_multiplier = self._ladder[idx] if self._ladder else 1.0
            # ── 止盈锚点（规格 12.3-1 三种口径）──
            #   mid（**默认**，规格"优先中轨落袋，更稳"）| far（对边）| pct（箱高比例，自入场价算）
            if self._tp_mode == "far":
                it.tp_anchor = (it.box_upper if it.direction == "BUY" else it.box_lower)
            elif self._tp_mode == "pct":
                _d = _bh * self._tp_pct
                it.tp_anchor = (c + _d) if it.direction == "BUY" else (c - _d)
            else:
                it.tp_anchor = it.box_mid     # 冻结时 it.box_mid 已是冻结值（锚点不随价滑动）
            it.sl_locked = False                  # SL 走桥会话系数（用户决策）
            if positions_open > 0:
                it.action, it.reason = "none", "osc_same_dir_hold"  # 同向仅 1 单
            # ── 【路线 B · 2026-09-16】箱体波动扩张闸（**本迭代的核心接入点**）──────
            # 放在这里的原因：① 只在"真的会开新单"时裁决（上面 `positions_open>0` 已降级）；
            #   ② 在下方"冻结箱体"之前 ⇒ 被拦时 `action != "open"`，**不会冻结箱体**、
            #      不会产生假轮次（冻结逻辑本身就要求 `it.action == "open"`）。
            # 契约：`vol_expand_proba < 0` = 未提供 ⇒ **不裁决**（保持既有行为）；
            #   `self._vol_osc_skip_prob < 0` = 本闸关闭（默认，可一行回滚）。
            # 边界：**只拦箱体新开**，不碰持仓管理、不碰趋势路径（FSM 不参与预测）。
            # 【P2 2026-09-19】`osc_skip_require_singleton` 开启时**额外要求预测可信**
            #   （conformal 单例）⇒ 置信度真正接到闸门上。
            #   `vol_route_singleton is None`（未知）⇒ `_vol_conf_ok = False` ⇒ **不拦**
            #   （fail-safe：宁可少拦，不因"算不出置信度"而静默加码限制）。
            _vol_conf_ok = (not self._vol_skip_require_singleton
                            or vol_route_singleton is True)
            # 【可观测性 2026-09-19】闸门候选标志（**只读**，用于下方曝光点）
            _vol_open_cand = (it.action == "open")
            if (it.action == "open" and self._vol_osc_skip_prob >= 0.0
                    and vol_expand_proba >= self._vol_osc_skip_prob
                    and _vol_conf_ok):
                it.action, it.reason = "none", "osc_vol_expand_skip"
            # ── 【可观测性 2026-09-19】vol 路由**只读曝光点** ──────────────────────
            # 动机（本轮实测）：`state.vol.osc_skip_prob` 默认 -1.0（关闭）⇒ 上方那个 if
            #   不成立 ⇒ `vol_expand_proba` **既不参与决策、也不被记录、也不进面板**
            #   ⇒ 模型即便部署，闸门关闭时全链路**没有任何 p_cal 曝光位**（"只观测"观测不到）。
            # 为什么记在这里：这是**唯一"真的会开箱体新单"**的入场点
            #   （上方 `positions_open > 0` 已把同向持仓降级为 none；下方冻结要求 action==open）
            #   ⇒ 记下的样本恰好是"第二跳（振幅 → 箱体单 R）"所需的样本集，
            #   且频次有界（**不是每 bar 一行**，只在贴边候选时产生）。
            # 纪律：本段**只读**，不修改 `it` 任何字段 ⇒ 决策逐位不变、零回滚风险。
            #   回滚 = 删除本段（无配置键、无状态）。
            # 【2026-09-19 开闸取证】必须落**可结算的字段**：闸门一旦开启，"被拦"的候选
            #   **不会产生订单**，事后无从结算 ⇒ 若不在此记录 `bar_id` 与箱体三线，
            #   "运行结果能否证明价值"永远无法回答（本仓库红线：改动必须可归因）。
            #   有了 (bar_id, dir, close, box 三线) 即可**离线重放**该候选的
            #   TP(箱体中值/对边) 与破界结局 ⇒ 得到反事实 R，与"放行"组直接对照。
            if _vol_open_cand:
                logger.info(
                    "[vol_route] %s bar=%s 箱体入场候选 | p_cal=%s singleton=%s "
                    "阈值=%s 结果=%s | dir=%s close=%.5f box=[%.5f,%.5f,%.5f] reason=%s",
                    symbol, (bar_id or "?"),
                    ("NA" if vol_expand_proba < 0.0 else f"{vol_expand_proba:.4f}"),
                    ("未知" if vol_route_singleton is None
                     else ("单例" if vol_route_singleton else "非单例")),
                    self._vol_osc_skip_prob,
                    ("放行" if it.action == "open" else "被拦"),
                    it.direction, float(close[-1]),
                    float(lo), float(mid), float(up), it.reason)
            # 【2026-09-15 修复】冻结箱体**只在真的下单时**发生（`it.action == "open"`）。
            # 此前是"只要触及边界就冻" —— 即使被 `positions_open` 拦成 `none`（或下游
            # `state.order_enabled=False` 不下单）也照样冻结 ⇒ 凭空产生一个 TP 锚点，
            # 且这个"假轮次"会一直持续到离开 S1 才解冻（期间所有 bar 都用这个失效 mid）。
            if it.action == "open" and not ctx.box_frozen:
                ctx.box_upper, ctx.box_lower, ctx.box_mid = up, lo, mid
                ctx.box_frozen, ctx.box_frozen_at = True, datetime.now(timezone.utc).isoformat()
                ctx.osc_round_active = True
                # 记下冻结时的计数器快照 → 它们一旦变化即代表本轮已止盈/止损结束
                ctx.frozen_loss_count, ctx.frozen_atr_loss = ctx.consec_losses, _cur_atr_loss
                # 【2026-09-16】"轮次结束"显式标记快照（见 frozen_round_seq 注释）
                ctx.frozen_round_seq = _cur_round_seq
                ctx.osc_edge_streak = 0       # 已开仓 → 防抖进度清零（下一轮重新累计）
                ctx.osc_break_streak = 0      # 新一轮从 0 计破界
                # 【马丁补仓 2026-09-18】记住本轮入场方向 → 止损后按"同向"补下一档
                ctx.osc_last_dir = it.direction
            await self._save_ctx(ctx)
            return it

        # ── S2 趋势初生：顺势回踩，仅 1 笔 ──
        # 【2026-09-19 阶段1】弃权闸点 ③（S2 趋势初生）
        if fsm_state == "S2_TREND_INIT" and not hold_only and not abstain:
            tdir, _src = resolve_trend_dir(direction, slope)
            it.dir_source = _src
            if tdir == "none":
                # 方向模块判"方向模糊" → 禁止趋势开仓（用户规格；实测 M5 斜率符号反向，
                # 故"没方向就别做"比"硬按斜率选边"更安全，见方案 §20）
                it.reason = "dir_none_veto"
                return it
            if not tdir:
                it.reason = "no_trend_dir"
                return it
            if positions_open > 0:
                it.reason = "init_already_open"
                return it
            if not self._spike_ok(high, low, atr, base_high, base_low):
                it.reason = "init_spike_skip"     # 追涨过滤（L4 实测最优项）
                return it
            _ok, _dir = self._pullback_entry(tdir, c, high, low, w_pull, atr)
            if not _ok:
                # ── L4 触价入场（§49）──
                # 未回踩到位时**不再直接放弃**：改为下发"触价入场位"，由桥侧既有 zone gate
                # 等价格回落到位再成交（超时作废）。语义是**严格超集**：
                #   `_ok=True`（价已到位）→ 仍走下方市价入场（**与改动前逐位一致**）；
                #   `_ok=False`（价未到位）→ 才新增"布置触价单"这条路。
                _st = self._apply_touch_entry(it, ctx, tdir, high, low, w_pull, atr, c)
                if _st == "armed":
                    it.action = "open"
                    it.direction = "BUY" if tdir == "UP" else "SELL"
                    it.reason = "init_touch_wait"
                    it.lot_multiplier = 1.0
                    ctx.trend_dir, ctx.add_count = tdir, 0
                    await self._save_ctx(ctx)
                    return it
                it.reason = ("init_touch_pending" if _st == "pending"
                             else "init_no_pullback")
                return it
            it.action, it.direction, it.reason = "open", _dir, "init_pullback"
            it.lot_multiplier = 1.0
            ctx.trend_dir, ctx.add_count = tdir, 0
            await self._save_ctx(ctx)
            return it

        # ── S3 趋势中段：无仓则首建，有仓则顺势加仓 ──
        # 【2026-09-19 阶段1】弃权闸点 ④（S3 首建 + 顺势加仓）
        if fsm_state == "S3_TREND_MID" and not hold_only and not abstain:
            # 方向来源：**方向模块是当场真值**；`ctx.trend_dir` 只是"本轮已锁定的方向"。
            # 【2026-09-15 修复】此前是 `tdir = ctx.trend_dir or tdir`（锁定值**盖过**模块读数）
            # → 模块已改判反向时，仍会按旧方向加仓（"一错到底"）。现改为：
            #   · 无持仓 → 一律以模块方向为准（新轮次，旧锁定值不得沿用）
            #   · 有持仓 → 锁定方向优先，但模块**明确反向**时拒绝加仓（见下 (a)）
            mod_dir, _src = resolve_trend_dir(direction, slope)
            it.dir_source = _src
            if mod_dir == "none":
                it.reason = "dir_none_veto"
                return it

            # 【缺口修复 2026-09-14】规格 10.2 的前提是"S3 已有初始仓位"，但实际存在
            # S3 却无持仓的路径：S2 的回踩条件未满足/被风控拦下、持仓已平后 FSM 仍在 S3。
            # 若此处 no-op，趋势策略将**永久空转**（S0↔S3 反复复位、永不进场）。
            # 故无持仓时退化为"S2 等价的首次试错入场"，入场口径复用同一函数。
            if positions_open <= 0:
                if not mod_dir:
                    it.reason = "no_trend_dir"
                    return it
                if not self._spike_ok(high, low, atr, base_high, base_low):
                    it.reason = "mid_spike_skip"
                    return it
                _ok, _dir = self._pullback_entry(mod_dir, c, high, low, w_pull, atr)
                if not _ok:
                    # L4 触价入场（§49）：与 S2 同一口径（复用 `_apply_touch_entry`，
                    # 不在此另写一份），去重闸同样生效 —— 两条首次入场路径共用同一触价单。
                    _st = self._apply_touch_entry(it, ctx, mod_dir, high, low,
                                                  w_pull, atr, c)
                    if _st == "armed":
                        it.action = "open"
                        it.direction = "BUY" if mod_dir == "UP" else "SELL"
                        it.reason = "mid_initial_touch_wait"
                        it.lot_multiplier = 1.0
                        ctx.trend_dir, ctx.add_count = mod_dir, 0
                        await self._save_ctx(ctx)
                        return it
                    it.reason = ("mid_initial_touch_pending" if _st == "pending"
                                 else "mid_initial_no_pullback")
                    return it
                it.action, it.direction, it.reason = "open", _dir, "mid_initial_entry"
                it.lot_multiplier = 1.0
                ctx.trend_dir, ctx.add_count = mod_dir, 0
                await self._save_ctx(ctx)
                return it

            # ── 加仓路径（已有持仓）──
            # (a) 反转头：模块的当场读数与本轮锁定方向**相反** → 视为趋势反转。
            #     处置（用户规格「反转头…矫准SL」）：
            #       ① **拒绝加仓**（既不加旧方向——那是一错到底；也不加新方向——
            #          那是逆着已有持仓加，违反规格 10.2）；
            #       ② **矫准 SL**：同时收紧移动止损、置"准备离场"（与 S4 同口径）——
            #          反向时不是去开反向单，而是把**已有持仓**的止损矫准/收紧。
            #     ⇒ 故此处**不再是裸 `none`**：必须带上 trail_mult/exit_ready，
            #       否则桥拿不到"该收紧了"，与 S4 的处理不一致（用户 2026-09-15 指出）。
            if ctx.trend_dir and mod_dir in ("UP", "DOWN") and mod_dir != ctx.trend_dir:
                it.reason = "add_dir_conflict_trail_tighten"
                it.dir_source = f"{_src}_reversed"
                it.trail_mult = self._fade_trail_mult
                it.exit_ready = True
                return it
            # (b) 本轮锁定方向优先；无锁定时用模块方向
            tdir = ctx.trend_dir or mod_dir
            if not tdir:
                it.reason = "no_trend_dir"
                return it
            # (c) 禁逆势加仓（规格 10.2）：需调用方提供**持仓方向**才可判定。
            #     此前只有 positions_open（数量）→ 无法判断顺势/逆势，该约束实际是空转。
            want_side = "BUY" if tdir == "UP" else "SELL"
            pos_side = str(position_dir or "").strip().upper()
            if pos_side in ("BUY", "SELL") and pos_side != want_side:
                it.reason = "add_against_position"
                return it
            if ctx.add_count >= self._max_adds:
                it.reason = "mid_max_adds_reached"
                return it
            _ok, _dir = self._pullback_entry(tdir, c, high, low, w_pull, atr)
            if not _ok:
                it.reason = "mid_no_pullback"
                return it
            it.action, it.direction, it.reason = "add", _dir, "mid_add_on_pullback"
            it.lot_multiplier = 1.0                # 趋势加仓固定 base_lot（规格 10.2）
            it.add_count = ctx.add_count + 1
            # 【C5 2026-09-17】无权威来源（`fsm_adds_used < 0` = 持仓计数失败/未知）时
            # **不再自增** `ctx.add_count`：原实现自增 ⇒ 被风控拦下、**未成交**的加仓意图
            # 也被计入 ⇒ 达到 `max_adds` 后 `mid_max_adds_reached` **提前封顶**，本轮加仓
            # 机会被静默吃掉（计数与真实成交数脱钩）。
            # 取舍：**宁少算、不提前封顶** —— 代价是 `-1` 窗口内可能超出 `max_adds`；
            # 由风控侧 `risk.max_concurrent_signals` / `risk.max_total_exposure` /
            # 单笔上限兜底（不会无限加仓）。WARNING 便于统计 `-1` 揭示率（两段式观察）。
            if int(fsm_adds_used) < 0:
                logger.warning(
                    "[state_strategy] %s 加仓计数权威缺失（fsm_adds_used=-1，持仓计数失败）"
                    "→ 本轮**不自增** add_count（当前 %d / 上限 %d），避免提前封顶",
                    symbol, ctx.add_count, self._max_adds)
            await self._save_ctx(ctx)
            return it

        # ── S4 趋势衰竭：禁新开/加仓；**收紧移动止损**、准备离场（规格 11）──
        # 规格原文：trend_fade「禁止新增趋势开仓；已有持仓**收紧移动止损，准备离场**」。
        # 此前只做到了"禁新开"，**收紧这件事没有任何输出** —— 桥无从知道要收紧，
        # 规格里这一半实际是空的。现在通过 `trail_mult` / `exit_ready` 表达意图：
        # 数值仍由桥按会话系数计算（信号塔不写死 SL/TP 数值，见模块 docstring）。
        if fsm_state == "S4_TREND_FADE":
            it.action = "none"
            it.trail_mult = self._fade_trail_mult
            it.exit_ready = True
            it.reason = "s4_no_new_order_trail_tighten"
            return it

        # ── S5 震荡锁止：禁震荡开仓；已有持仓沿用既有 SL（规格 9.4）──
        if fsm_state == "S5_OSC_LOCKED":
            it.action, it.reason = "none", "s5_osc_locked_no_new_order"
            return it

        # ── S9 暂停：禁一切新开，但**仍维护移动止损**（决策 Q4：不裸奔）──
        # 故显式给出 trail_mult=1.0（= 按会话系数正常维护），而不是让桥"什么都收不到"
        # 而误判为无需管理。
        if fsm_state == "S9_PAUSED":
            it.action = "none"
            it.trail_mult = 1.0
            it.exit_ready = False
            it.reason = "s9_paused_no_new_order_trail_keep"
            return it

        # ── 其它（S0 空闲等）：不下单 ──
        # 【2026-09-19 阶段1】弃权留痕：`abstain` 只拦**新开/加仓**（上方 4 处闸点），
        #   不拦离场与尾随 ⇒ 这里**只改留痕原因**，动作本就是 none（不改任何行为）。
        #   留痕落在 `intent_reason`（已落库的既有列）⇒ **无需新增 DB 列**。
        it.action, it.reason = "none", (
            "abstain_veto" if abstain else f"{fsm_state.lower()}_no_new_order")
        return it

    # ── 【已移除】normalize_after_close ──────────────────────
    # 原实现假设"桥在平仓后回调本方法"，但存在两个问题（2026-09-15 复核）：
    #  1. **全仓库无调用者** —— 桥是独立进程，无法调用信号塔进程内的 Python 方法
    #     （方案 §18.4 设想的是"桥回写 Redis，FSM 读"，而非进程内回调）→ 实为死代码；
    #     且它自增 `ctx.consec_losses`，一旦将来有人接上，就与桥侧计数器形成**双真值**。
    #  2. "解冻箱体"的职责已归 `decide()`（状态切换即解冻，见规格 §7.1），无需平仓回调。
    # 平仓侧的唯一真值改为两个 Redis 键（`hcm:state:osc_loss_count` /
    # `hcm:state:osc_atr_loss`），由桥在 `tools/position_sync.py` 的平仓归因处写入。
    # 保留本注释，是为让后来者知道"这里曾有一个看起来合理但接不上的回调"，别再把它加回来。

    async def get_ctx(self, symbol: str) -> Optional[StrategyContext]:
        return self._cache.get(symbol)

    @property
    def tuning(self) -> dict:
        return {
            "order_enabled": self._order_enabled, "box_window": self._box_window,
            "slope_window": self._slope_window, "pullback_atr": self._pullback_atr,
            "max_adds": self._max_adds, "border_tol_atr": self._border_tol_atr,
            "pullback_window": self._pullback_window,
            "trail_lookback": self._trail_lookback,
            "fade_trail_mult": self._fade_trail_mult,
            "box_min_width_atr": self._box_min_width_atr,
            "box_max_width_atr": self._box_max_width_atr,
            "entry_mode": self._entry_mode,
            "entry_wait_sec": self._entry_wait_sec,
            # 【§57 箱体规格】上屏可见 —— 否则"改了模式但看不到生效"（反复出现的盲区）
            "bands_mode": self._bands_mode,
            "q_high": self._q_high,
            "q_low": self._q_low,
            "buffer_mode": self._buffer_mode,
            "buffer_pct": self._buffer_pct,
            "entry_confirm_bars": self._entry_confirm,
            "tp_mode": self._tp_mode,
            "tp_pct": self._tp_pct,
            "break_confirm_bars": self._break_confirm,
            "ladder": self._ladder,
            # 【马丁补仓 2026-09-18】开关上屏（改了必须看得见；面板/接口读 `tuning`）
            "osc_martingale_enabled": self._osc_ma_enabled,
            # 【可观测性 2026-09-18】S0 箱体入场开关 + 趋势污染护栏上屏
            "osc_in_idle": self._osc_in_idle,
            "osc_idle_block_after_trend": self._osc_idle_block_after_trend,
            # 【P1-6 F7】护栏窗口长度上屏（改了必须看得见）
            "osc_idle_block_bars": self._osc_idle_block_bars,
        }
