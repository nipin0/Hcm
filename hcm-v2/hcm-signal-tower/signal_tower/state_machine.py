"""state_machine.py — 行情状态有限状态机（FSM）+ 防抖 + 跨重启持久化。

依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §6（防抖 + FSM）

状态表（决策 Q4 已确认；S6–S8 刻意保留不使用，不得自行发明）：

    S0 IDLE        空闲（初始/复位）
    S1 OSC         震荡（箱体均值回归策略域）
    S2 TREND_INIT  趋势初生（轻仓试错）
    S3 TREND_MID   趋势中段（顺势加仓）
    S4 TREND_FADE  趋势衰竭（只持有）
    S5 OSC_LOCKED  震荡锁止（累计止损达上限，禁震荡开仓）
    S9 PAUSED      暂停（人工暂停 / K 线异常 / 推理连续失败）

设计要点：
  · `decide()` 是**纯函数**（无 IO、无时间依赖，now 由外部注入）→ 可离线单测状态迁移。
  · `step()` 只负责 IO：读/写 Redis、组装 FSMDecision。Redis 为跨重启真源。
  · 绝不改变交易行为：shadow 阶段只产出状态与决策供落库/上屏。

关键约束（务必遵守，否则影子观察会失真）：
  · 防御性回落：`state.fsm.flat_reset_enabled` 默认 **False**。
    原因：规格中"S4 全部平仓后回 S0""S3 触碰移动止损后回 S0"属**策略层驱动**的复位。
    策略层尚未接线时持仓恒为 0，若开启会让趋势态刚进入就被立刻复位 → 影子观测失效。
    策略层落地后再置 True。
  · 震荡锁止计数器 `hcm:state:osc_atr_loss:{symbol}` 的**唯一写者**是桥的平仓归因
    （`tools/position_sync.py:_infer_close_reason` → sl/tp/be；方案 §18.4）。
    本模块只在**清锁/人工复位**时将其清零。缺省 0 → S5 不会误触发（读得到才判）。
    ⚠ 桥侧回写**尚未接线** → 当前恒 0 ⇒ **S5 锁止不生效**（方案 §37）。

配置键（生产以 PG/Redis 为准，下列仅兜底）：
  state.debounce.k_enter       进入新态所需连续同类别根数（默认 2）
  state.debounce.k_exit        趋势态转入震荡所需连续根数（默认 2）
  state.debounce.k_fade        进入趋势衰竭所需连续根数（默认 2）
  state.infer_fail_bars        连续推理失败 → S9 的根数（默认 3）
  state.osc_atr_loss_limit     累计震荡止损上限（ATR 倍数，默认 4.0）
  state.fsm.flat_reset_enabled 持仓归零复位（默认 False，见上）
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

FSM_KEY_TMPL = "hcm:state:fsm:{symbol}"
OSC_LOSS_KEY_TMPL = "hcm:state:osc_atr_loss:{symbol}"
# 震荡连续止损**次数**（策略层读它决定梯度手数档位）。与上面的 ATR 倍数是**两种量纲**，
# 故是两个键；但**写者必须同一处**（桥的平仓归因），否则形成双真值。
OSC_LOSS_COUNT_KEY_TMPL = "hcm:state:osc_loss_count:{symbol}"
PAUSE_KEY_TMPL = "hcm:state:pause:{symbol}"

DEFAULTS: dict = {
    "state.infer_fail_bars": 3,
    "state.debounce.k_enter": 2,
    "state.debounce.k_exit": 2,
    "state.debounce.k_fade": 2,
    "state.osc_atr_loss_limit": 4.0,
    "state.fsm.flat_reset_enabled": False,
    # 【2026-09-15】趋势态入口必须由**起点触发器**确认（实测漏检 0.4%/误报 16.6%/提前 1 根，
    # 远优于 4 类 argmax 推导的 20%/67.8%/滞后 3.5）。默认 False = 保持既有行为，
    # 开启属**核心机制变更**，须走变更说明与灰度（方案 §21.2）。
    "state.trigger.required": False,
    # 【2026-09-16 新增】低置信（`decided=False`）时的**类别迁移语义**：
    #   · "hold"（默认 = 既有行为，零变化）：该根不参与类别防抖（不进不退）。
    #     代价（验收门实测，走前式 3 折 / 生产对齐口径）：一旦判过趋势，
    #     "退出趋势"需连续 k_exit 根 **decided** 非趋势 ⇒ ON 难熄灭
    #     ⇒ 漏检 286 / 误报 92.8% / 中位提前量 **+1.0（滞后）**。
    #   · "decay"：低置信即视为**退化为震荡**，照常参与防抖（与验收门
    #     `model_nohold` 检测器同义）。同一模型/特征/k 实测：
    #     漏检 **49** / 误报 84.7% / 中位提前量 **−1.0（提前）** ⇒ 由滞后转提前。
    #   ⇒ 不动任何阈值与防抖根数，仅改"低置信如何参与防抖"。
    "state.fsm.low_conf_policy": "hold",
}


class MarketState(str, Enum):
    S0_IDLE = "S0_IDLE"
    S1_OSC = "S1_OSC"
    S2_TREND_INIT = "S2_TREND_INIT"
    S3_TREND_MID = "S3_TREND_MID"
    S4_TREND_FADE = "S4_TREND_FADE"
    S5_OSC_LOCKED = "S5_OSC_LOCKED"
    # S6 / S7 / S8 —— 规格未定义，保留不使用
    S9_PAUSED = "S9_PAUSED"


# 预测类别 → 目标状态（契约：类别名与 tools/build_state_labels.STATE_NAMES 一致）
CLASS_TO_STATE = {
    "oscillation": MarketState.S1_OSC,
    "trend_init": MarketState.S2_TREND_INIT,
    "trend_mid": MarketState.S3_TREND_MID,
    "trend_fade": MarketState.S4_TREND_FADE,
}

_TREND_STATES = (MarketState.S2_TREND_INIT, MarketState.S3_TREND_MID,
                 MarketState.S4_TREND_FADE)


def apply_osc_close(reason: str, sl_atr: float,
                    atr_loss: float, count: int) -> tuple[float, int]:
    """推进震荡风控双计数器：返回 `(新的 osc_atr_loss, 新的 osc_loss_count)`。

    **纯函数**，是这两个计数器推进语义的**唯一实现点** —— 桥侧回写
    （`tools/position_sync.py`）通过 `importlib` 按路径加载本模块后直接调用它，
    **不得另写一份**（同语义两份实现 = 本仓库红线）。

    规格 9.4「累计震荡止损达 4×ATR → S5 锁止」，故：

    | reason | 语义 | 动作 |
    |---|---|---|
    | `tp` | 一轮成功结束 | **两个计数器都归零**（梯度回到第一档） |
    | `sl` | 策略性止损 | `atr_loss += sl_atr`、`count += 1` |
    | 其它 | `be` / `manual` / `expert` / `stop_out` | **不计入**（原样返回） |

    *其它不计入的理由*：
      · `be`（保本出场）**没有亏损** —— 规格说的是"累计**止损**"，保本不应计入；
        若计入会让"连续止损"计数虚高、梯度手数无端放大。
        ⚠ 该口径**尚未经用户确认**（方案 §37.5-3），当前取"不计入"。
      · `manual` / `expert` / `stop_out` 不是策略性止损，混入会污染锁止判定
        （其中 `expert` 还包含桥自身的移动止损成交，计入会把正常止盈保护算成亏损）。

    Args:
        reason: 平仓归因（`tools/position_sync.py:_CLOSE_REASON_BY_DEAL`）。
        sl_atr: 本次止损距离 ÷ **开仓时** ATR（即止损实际用掉多少倍 ATR）。
        atr_loss: 当前累计值；count: 当前计数。
    """
    r = str(reason or "").strip().lower()
    if r == "tp":
        return 0.0, 0
    if r == "sl":
        return float(atr_loss) + max(0.0, float(sl_atr)), int(count) + 1
    return float(atr_loss), int(count)


@dataclass
class FSMState:
    """单个品种的状态机快照（可 JSON 序列化 → Redis 跨重启持久）。"""
    symbol: str = ""
    time_frame: str = ""
    state: str = MarketState.S0_IDLE.value
    since: str = ""
    pending_class: str = ""
    pending_streak: int = 0
    hold_only: bool = False          # 停止新开/加仓但持仓保留（S3/S4 转震荡）
    consecutive_fail: int = 0        # 连续推理失败计数
    osc_atr_loss: float = 0.0        # 累计震荡止损（ATR 倍数）
    last_class: str = ""             # 最近一次判定类别（观测）
    last_margin: float = 0.0
    # 【2026-09-15】方向模块结果（up/down/none）：趋势态入口的**否决项**。
    # 规格：形态判趋势但方向 NONE → 禁止趋势开仓（规避方向模糊的假趋势）。
    direction: str = "none"
    last_bar_time: str = ""          # 已处理的 bar open_time（保证一根 bar 只计一次）
    # 【2026-09-15 状态年龄】当前**确认状态**已持续的 bar 数（含当前 bar；迁移时置 1）。
    # 为什么需要它：方案 §25 实测证明「初生 vs 中段」在**固定窗口的特征/标签里不可分**
    # （两次独立尝试 AUC 0.535 / 0.588），因为"处在趋势哪一段"不是窗口内的形状属性，
    # 而是**跨窗口的位置**属性。而"已持续多少根"是**纯过去可观测量** —— 不需要预测、
    # 也不需要人工阈值，由状态机自己数出来。策略层据此区分轻仓试错与顺势加仓。
    age_bars: int = 0
    updated_at: str = ""


@dataclass
class FSMDecision:
    """一次 step 的结果（供调用方落库/上屏）。"""
    symbol: str
    time_frame: str
    prev_state: str
    state: str
    transitioned: bool
    hold_only: bool
    note: str
    pending: str = ""
    infer_ok: bool = False
    infer_decided: bool = False
    infer_reason: str = ""
    predicted_class: str = ""
    proba: dict = field(default_factory=dict)
    margin: float = 0.0
    model_version: str = ""
    age_bars: int = 0                # 当前状态已持续 bar 数（供策略层区分试错/加仓阶段）
    direction: str = ""              # up/down/none（方向模块结果；趋势态入场依据）


def decide(
    st: FSMState,
    infer: Any,
    *,
    positions_open: int,
    paused: bool,
    now_iso: str,
    k_enter: int,
    k_exit: int,
    k_fade: int,
    fail_bars: int,
    osc_limit: float,
    flat_reset_enabled: bool,
    trigger_on: bool = False,
    direction: str = "",
    require_trigger: bool = False,
    low_conf_policy: str = "hold",
) -> FSMDecision:
    """纯状态迁移函数（无 IO / 无时钟依赖）。

    `infer` 需具备 state_infer.StateInferResult 的属性：
    ok / decided / state / proba / margin / model_version / reason。

    【三件套组合规则（用户 2026-09-15）】
      `trigger_on` —— 起点触发器（`trend_trigger`，实测漏检 0.4%/误报 16.6%/中位提前 1 根）
      `direction`  —— 方向模块（`trend_direction`）："up" / "down" / "none" / ""（未提供）
      `require_trigger` —— 开启后，**趋势态入口（S2/S3）必须同时满足**：
                            (a) 触发器确认；(b) 方向 ≠ NONE
                          即"形态判趋势但方向 NONE → 不开仓"（规避方向模糊的假趋势）。
      新增入口路径：**触发器响 + 方向明确 → 直接进 S2**（不依赖 4 类 argmax，
      这是"敏锐捕捉"的落地：起点被判出即入场，无需等模型确认为趋势）。
      `direction=""` 表示未接入方向模块 → 不做否决（保持向后兼容）。
    """
    prev = st.state
    cur = MarketState(st.state)

    def _mk(state_value: str, note: str) -> FSMDecision:
        """组装决策（**不改动 st**，仅读取）。"""
        return FSMDecision(
            symbol=st.symbol, time_frame=st.time_frame, prev_state=prev,
            state=state_value, transitioned=(state_value != prev),
            hold_only=st.hold_only, note=note,
            pending=(f"{st.pending_class}:{st.pending_streak}" if st.pending_class else ""),
            infer_ok=bool(getattr(infer, "ok", False)),
            infer_decided=bool(getattr(infer, "decided", False)),
            infer_reason=str(getattr(infer, "reason", "")),
            predicted_class=str(getattr(infer, "state", "") or ""),
            proba=dict(getattr(infer, "proba", {}) or {}),
            margin=float(getattr(infer, "margin", 0.0) or 0.0),
            model_version=str(getattr(infer, "model_version", "") or ""),
            age_bars=st.age_bars,
            direction=st.direction,
        )

    def _keep(note: str) -> FSMDecision:
        """保持当前状态（**不清 since、不清防抖计数**，否则防抖永远无法累积）。

        年龄语义：状态未变 → 已持续 bar 数 +1。**包括**低置信跳过、单根推理失败等
        "不推进也不回退"的情形 —— 因为在那些 bar 上该状态事实上仍在持续，年龄应继续走。
        （decide 每根 bar 只被调用一次，由 step() 的 bar_time 去重保证，故不会重复累加。）
        """
        st.age_bars += 1
        return _mk(cur.value, note)

    def _to(target: MarketState, note: str) -> FSMDecision:
        """真正发生迁移：更新 state/since、清零待迁计数、**年龄置 1**（本根为第 1 根）。"""
        st.state = target.value
        st.since = now_iso
        st.pending_class = ""
        st.pending_streak = 0
        st.age_bars = 1
        return _mk(target.value, note)

    # 1) 暂停态最高优先（人工暂停 / 品种停用 / 外部异常）
    if paused:
        if cur != MarketState.S9_PAUSED:
            return _to(MarketState.S9_PAUSED, "paused")
        return _keep("paused")

    # 2) 从暂停恢复 → 复位到 S0（持仓由策略层/桥按既有 SL 管理，不受影响）
    if cur == MarketState.S9_PAUSED:
        st.hold_only = False
        st.consecutive_fail = 0
        return _to(MarketState.S0_IDLE, "resumed")

    # 3) 推理失败：单根保持原态；连续 fail_bars 根 → S9
    if not getattr(infer, "ok", False):
        st.consecutive_fail += 1
        if st.consecutive_fail >= max(1, fail_bars):
            return _to(MarketState.S9_PAUSED,
                       f"infer_fail_x{st.consecutive_fail}({getattr(infer, 'reason', '')})")
        return _keep(f"infer_fail_single({getattr(infer, 'reason', '')})")
    st.consecutive_fail = 0

    # 4) 【触发器驱动的趋势态入口】—— **必须在"置信不足"早退之前**（见下）。
    #    与"按 4 类类别迁移"并列：触发器响且方向明确 → 直接进 S2（轻仓试错），
    #    不等模型把类别确认为 trend_mid（后者实测只会滞后 +3.5 根）。
    #    方向 NONE 时**不开仓**（规格：规避方向模糊的假趋势）。
    if trigger_on:
        st.direction = direction or st.direction
    # ── 【2026-09-15 根因修复：逻辑倒置】──
    # 本块原在第 5~9 步之间（`low_conf_skip` 早退**之后**）。而 `low_conf_skip` 位于
    # 第 4 步、**无条件 return** ⇒ 只要模型置信 < `state.min_conf`(0.45)，
    # **触发器入口也一起被丢弃**。这与 `state.trigger.required=true` 的设计本意
    # **正好相反**：该开关的意义就是"用触发器替代**不可信**的 4 类 argmax"（§21：
    # 4 类单独不可信；§30：触发器 漏检 0.4%/提前 −1.0 根）。用"模型不可信"去否决
    # 一个本就为绕开模型而设的信号，是逻辑倒置。
    # **生产铁证**：2026-09-15 12:20 那根 bar `trigger_on=true`（起点模型已确认）
    # 却 `note=low_conf_skip` —— 触发器响了，连看都没被看一眼。
    # **量化**：近 6h 有 **36/70 = 51%** 的 bar 走此早退 ⇒ **一半的触发器机会被静默丢弃**。
    # （`infer_fail`（推理失败）仍优先于本块：那是"算都算不出来"，保守起见不在此放行。）
    if (trigger_on and cur not in _TREND_STATES
            and cur not in (MarketState.S5_OSC_LOCKED, MarketState.S9_PAUSED)):
        if direction in ("up", "down"):
            st.hold_only = False
            st.direction = direction
            return _to(MarketState.S2_TREND_INIT, f"trigger_enter({direction})")
        return _keep("trigger_no_dir")   # 响了但方向模糊 → 明确不开仓

    # 4b) 【不依赖模型类别的"持仓 / 风控驱动"规则】—— 必须在置信闸**之前**。
    #     推理置信只应闸住"**类别驱动**的迁移"（下方第 5~9 步）；而下列两条只看
    #     持仓与风控计数器，与模型判什么类别无关。把它们放在置信闸之后，等于
    #     "**模型不确定时连风控与复位都不做**" —— 职责错置。
    #     ⚠ 生产铁证（2026-09-15）：`state.fsm.flat_reset_enabled` 已为 true，但
    #       12:15→12:40 **连续 6 根** `S4_TREND_FADE + positions_open=0` 始终
    #       `note=low_conf_skip`、**从未复位 S0** ⇒ 根因 A 的修复被本闸架空；
    #       同期 `12:20 trg=true` 的触发器入口也被它吃掉（同一病根的另一半）。
    # 震荡锁止（S1 累计止损达上限 → S5；S5 在无持仓时清锁）
    if cur == MarketState.S1_OSC and osc_limit > 0 and st.osc_atr_loss >= osc_limit:
        return _to(MarketState.S5_OSC_LOCKED, f"osc_atr_lock({st.osc_atr_loss:.2f}>={osc_limit})")
    if cur == MarketState.S5_OSC_LOCKED:
        # 规格 9.4：只有该品种全部持仓平完才清锁止并归零计数
        if flat_reset_enabled and positions_open <= 0:
            st.osc_atr_loss = 0.0
            st.hold_only = False
            return _to(MarketState.S0_IDLE, "osc_lock_cleared")
        return _keep("osc_locked")

    # 趋势态已无持仓 → 复位（受 flat_reset_enabled 保护，见模块 docstring）
    if flat_reset_enabled and cur in (MarketState.S3_TREND_MID, MarketState.S4_TREND_FADE) \
            and positions_open <= 0:
        st.hold_only = False
        return _to(MarketState.S0_IDLE, "flat_reset")

    # 4c) 置信不足 → 按 `low_conf_policy` 处置（见 DEFAULTS 该键的长注释）
    #   · hold （默认/既有）：不参与类别防抖 ⇒ 退出趋势需连续 k_exit 根 decided 非趋势
    #     ⇒ 实测 漏检 286 / 误报 92.8% / 中位 **+1.0（滞后）**
    #   · decay：视为"退化为震荡"，**沿用下方既有迁移与防抖逻辑**（不另写一份实现）
    #     ⇒ 实测 漏检 49 / 中位 **−1.0（提前）**
    #   ⚠ decay 路径**不更新** last_class/last_margin —— 低置信不产生新的类别信息。
    _policy = str(low_conf_policy or "hold").strip().lower()
    if not getattr(infer, "decided", False):
        if _policy != "decay":
            return _keep("low_conf_skip")
        target = MarketState.S1_OSC          # 退化目标：震荡（箱体逆势域，最保守）
    else:
        st.last_class = str(getattr(infer, "state", "") or "")
        st.last_margin = float(getattr(infer, "margin", 0.0) or 0.0)
        target = CLASS_TO_STATE.get(str(getattr(infer, "state", "") or ""))
        if target is None:
            return _keep("unknown_class")

    # 【规格 §6.3 对齐 2026-09-15】非趋势态**不得直接跳进 S3**。
    # 规格的迁移表里趋势入口是 S2（"轻仓试错、仅 1 笔、**禁加仓**"），S3 才是加仓阶段；
    # 而 `CLASS_TO_STATE` 把 trend_mid 直接映到 S3 → 非趋势态判出 trend_mid 时会
    # **首次进场就进入允许加仓的状态**，规避了试错纪律。
    # 实测依据（tools/replay_state_chain.py 整链回放，洁净窗口 1862 根）：
    #   S0_IDLE → S3_TREND_MID ×19、S1_OSC → S3_TREND_MID ×1（均为规格外迁移）。
    # 修复：非趋势态判出 trend_mid → 先落 S2；后续 bar 再按 S2→S3 正常推进。
    # 触发器驱动的入口（`trigger_on`）本就直达 S2，不受影响。
    if target == MarketState.S3_TREND_MID and cur not in _TREND_STATES:
        target = MarketState.S2_TREND_INIT

    # 6.5) 【触发器驱动的趋势态入口】—— 已**上移**至第 4 步（"置信不足"早退之前）。
    #      **不得在此另写一份**：该入口的语义（含排除元组与方向要求）只有一个实现点，
    #      双实现会漂移 —— 本仓库红线（见 state_features.py 顶部记录的事故模式）。
    #      与 S4 相关的历史实验（放开 S4 → 误报 35.3%→46.2%，已撤回）见 git 记录与
    #      迁移 0043 的说明。

    # 7) 同态：清零待迁计数
    if target == cur:
        st.pending_class = ""
        st.pending_streak = 0
        if cur in (MarketState.S2_TREND_INIT, MarketState.S3_TREND_MID):
            st.hold_only = False
        return _keep("same_state")

    # 8) hold_only：趋势态下转震荡且仍有持仓 → 保留趋势态、仅置 hold_only
    #    （规格 10.2：停止加仓、持仓保留、不新增趋势单；决策 Q4 已确认此语义）
    if target == MarketState.S1_OSC and cur in _TREND_STATES and positions_open > 0:
        st.hold_only = True
        st.pending_class = ""
        st.pending_streak = 0
        return _keep("hold_only")

    # 8.5) 趋势态入口的两道门（`require_trigger` 开启时生效）
    #      (a) 触发器必须确认（否则即使模型判趋势也不开仓 —— 4 类 argmax 单独不可信，§21）
    #      (b) 方向必须明确（"none" → 拒绝；"" = 未接入方向模块 → 不否决）
    if target in (MarketState.S2_TREND_INIT, MarketState.S3_TREND_MID):
        if require_trigger and not trigger_on:
            return _keep("no_trigger")
        if direction == "none":
            return _keep("trend_no_dir")
        if direction in ("up", "down"):
            st.direction = direction

    # 9) 防抖计数
    if st.pending_class != target.value:
        st.pending_class = target.value
        st.pending_streak = 1
    else:
        st.pending_streak += 1

    if target == MarketState.S4_TREND_FADE:
        need = max(1, k_fade)
    elif target == MarketState.S1_OSC and cur in _TREND_STATES:
        need = max(1, k_exit)
    else:
        need = max(1, k_enter)

    if st.pending_streak >= need:
        st.hold_only = False
        return _to(target, f"transition(need={need})")
    return _keep(f"pending({st.pending_class}:{st.pending_streak}/{need})")


class MarketStateMachine:
    """状态机驱动：负责配置、Redis 读写与 step 编排（迁移逻辑在 decide()）。"""

    def __init__(self, config_provider: Any = None, redis_client: Any = None):
        self._config = config_provider
        self._redis = redis_client
        self._fail_bars = int(DEFAULTS["state.infer_fail_bars"])
        self._k_enter = int(DEFAULTS["state.debounce.k_enter"])
        self._k_exit = int(DEFAULTS["state.debounce.k_exit"])
        self._k_fade = int(DEFAULTS["state.debounce.k_fade"])
        self._osc_limit = float(DEFAULTS["state.osc_atr_loss_limit"])
        self._flat_reset = bool(DEFAULTS["state.fsm.flat_reset_enabled"])
        self._require_trigger = bool(DEFAULTS["state.trigger.required"])
        self._low_conf_policy = str(DEFAULTS["state.fsm.low_conf_policy"])
        self._cache: dict[str, FSMState] = {}

    # ── 配置 ───────────────────────────────────────────────
    async def load_config(self) -> None:
        if self._config is None:
            return
        try:
            self._fail_bars = int(await self._config.get_float(
                "state.infer_fail_bars", self._fail_bars))
            self._k_enter = int(await self._config.get_float(
                "state.debounce.k_enter", self._k_enter))
            self._k_exit = int(await self._config.get_float(
                "state.debounce.k_exit", self._k_exit))
            self._k_fade = int(await self._config.get_float(
                "state.debounce.k_fade", self._k_fade))
            self._osc_limit = await self._config.get_float(
                "state.osc_atr_loss_limit", self._osc_limit)
            self._flat_reset = await self._config.get_bool(
                "state.fsm.flat_reset_enabled", self._flat_reset)
            self._require_trigger = await self._config.get_bool(
                "state.trigger.required", self._require_trigger)
            # 字符串键（非数值）⇒ 用 get() 而非 get_float()，缺省保留现值
            _lcp = await self._config.get("state.fsm.low_conf_policy", None)
            if _lcp is not None and str(_lcp).strip():
                self._low_conf_policy = str(_lcp).strip().lower()
            # 仅当配置实际变化时打 INFO（30s 热重载一次 → 否则产生大量重复日志）
            _sig = (self._k_enter, self._k_exit, self._k_fade, self._fail_bars,
                    round(self._osc_limit, 6), self._flat_reset, self._require_trigger,
                    self._low_conf_policy)
            if _sig != getattr(self, "_cfg_sig", None):
                self._cfg_sig = _sig
                logger.info(
                    "MarketStateMachine config loaded | k_enter=%d k_exit=%d k_fade=%d "
                    "fail_bars=%d osc_limit=%.1f flat_reset=%s require_trigger=%s "
                    "low_conf_policy=%s",
                    self._k_enter, self._k_exit, self._k_fade,
                    self._fail_bars, self._osc_limit, self._flat_reset,
                    self._require_trigger, self._low_conf_policy,
                )
            else:
                logger.debug("MarketStateMachine config unchanged")
        except Exception as exc:  # pragma: no cover
            logger.warning("MarketStateMachine config load failed (using defaults): %s", exc)

    # ── 持久化 ─────────────────────────────────────────────
    async def _load_state(self, symbol: str, tf: str) -> FSMState:
        st = self._cache.get(symbol)
        if st is not None:
            return st
        st = FSMState(symbol=symbol, time_frame=tf)
        if self._redis is not None and getattr(self._redis, "is_initialized", False):
            try:
                raw = await self._redis.get(FSM_KEY_TMPL.format(symbol=symbol))
                if raw:
                    data = json.loads(raw)
                    known = {f for f in FSMState.__dataclass_fields__}
                    st = FSMState(**{k: v for k, v in data.items() if k in known})
                    st.symbol, st.time_frame = symbol, tf
            except Exception as exc:  # noqa: BLE001
                logger.warning("[state_machine] %s 状态读取失败（按初始态继续）：%s", symbol, exc)
        self._cache[symbol] = st
        return st

    async def _save_state(self, st: FSMState) -> None:
        st.updated_at = datetime.now(timezone.utc).isoformat()
        if self._redis is None or not getattr(self._redis, "is_initialized", False):
            return
        try:
            # 无 TTL：状态必须跨信号塔重启存活（否则重启即丢锁止/防抖进度）
            await self._redis.set(
                FSM_KEY_TMPL.format(symbol=st.symbol),
                json.dumps(asdict(st), ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[state_machine] %s 状态写入失败：%s", st.symbol, exc)

    async def _read_osc_loss(self, symbol: str) -> float:
        """读累计震荡止损（ATR 倍数）。缺省 0 → S5 不误触发。**写者是桥**。"""
        if self._redis is None or not getattr(self._redis, "is_initialized", False):
            return 0.0
        try:
            raw = await self._redis.get(OSC_LOSS_KEY_TMPL.format(symbol=symbol))
            return float(raw) if raw else 0.0
        except (TypeError, ValueError):
            return 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    async def _clear_osc_counters(self, symbol: str) -> None:
        """清零震荡风控双计数器（ATR 倍数 + 连续止损次数）。

        【为什么必须写 Redis，而不能只改内存】`st.osc_atr_loss` 每根 bar 都由
        `_read_osc_loss()` 从键重新读入 → 只把内存置 0 而不写键，清锁后下一根立刻复锁
        （`decide()` 第 5 步读到的仍是旧值）。当前桥侧尚未回写、键恒缺省，故该缺陷
        是**潜伏**的；但一旦接线即显形，故此处一并修掉。

        本方法是这两个键**唯一**的写入点（正常累加由桥负责）—— 语义单一，避免双写。
        """
        if self._redis is None or not getattr(self._redis, "is_initialized", False):
            return
        try:
            for tmpl in (OSC_LOSS_KEY_TMPL, OSC_LOSS_COUNT_KEY_TMPL):
                await self._redis.set(tmpl.format(symbol=symbol), "0")
            logger.info("[state_machine] %s 震荡风控计数器已清零"
                        "（osc_atr_loss / osc_loss_count）", symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[state_machine] %s 计数器清零失败：%s", symbol, exc)

    async def _read_paused(self, symbol: str) -> bool:
        """读人工暂停标记（供 UI/运维设置）。缺省不暂停。"""
        if self._redis is None or not getattr(self._redis, "is_initialized", False):
            return False
        try:
            raw = await self._redis.get(PAUSE_KEY_TMPL.format(symbol=symbol))
            return str(raw).strip().lower() in ("1", "true", "yes", "on") if raw else False
        except Exception:  # noqa: BLE001
            return False

    # ── 公共 API ───────────────────────────────────────────
    async def step(
        self,
        symbol: str,
        time_frame: str,
        infer: Any,
        *,
        positions_open: int = 0,
        paused: Optional[bool] = None,
        bar_time: Optional[str] = None,
        now: Optional[datetime] = None,
        trigger_on: bool = False,
        direction: str = "",
    ) -> FSMDecision:
        """推进一根 bar 的状态机。`paused=None` 时自动读 Redis 暂停标记。

        `bar_time`（bar open_time 的字符串形式）用于保证**一根 bar 只推进一次**：
        `_produce_signal` 会被 live_override 路径在 bar 内每 ~30s 反复调用，
        若按调用计数，防抖的"连续 2 根"会被同一根 bar 的两次调用满足 → 防抖失效。
        故同一 bar_time 的重复调用直接返回 same_bar_skip（不改状态、不累加计数）。
        """
        st = await self._load_state(symbol, time_frame)
        st.time_frame = time_frame
        if bar_time and st.last_bar_time == bar_time:
            return FSMDecision(
                symbol=symbol, time_frame=time_frame,
                prev_state=st.state, state=st.state, transitioned=False,
                hold_only=st.hold_only, note="same_bar_skip",
                infer_ok=bool(getattr(infer, "ok", False)),
                infer_decided=bool(getattr(infer, "decided", False)),
                infer_reason=str(getattr(infer, "reason", "")),
                predicted_class=str(getattr(infer, "state", "") or ""),
                proba=dict(getattr(infer, "proba", {}) or {}),
                margin=float(getattr(infer, "margin", 0.0) or 0.0),
                model_version=str(getattr(infer, "model_version", "") or ""),
                age_bars=st.age_bars,
                direction=st.direction,
            )
        if paused is None:
            paused = await self._read_paused(symbol)
        st.osc_atr_loss = await self._read_osc_loss(symbol)
        now_iso = (now or datetime.now(timezone.utc)).isoformat()
        dec = decide(
            st, infer,
            positions_open=int(positions_open or 0),
            paused=bool(paused),
            now_iso=now_iso,
            k_enter=self._k_enter, k_exit=self._k_exit, k_fade=self._k_fade,
            fail_bars=self._fail_bars, osc_limit=self._osc_limit,
            flat_reset_enabled=self._flat_reset,
            trigger_on=bool(trigger_on), direction=str(direction or ""),
            require_trigger=self._require_trigger,
            low_conf_policy=self._low_conf_policy,
        )
        if bar_time:
            st.last_bar_time = bar_time
        await self._save_state(st)
        # 【2026-09-15】S5 清锁必须落 Redis，否则下一根立即复锁（见 _clear_osc_counters）
        if (dec.prev_state == MarketState.S5_OSC_LOCKED.value
                and dec.state == MarketState.S0_IDLE.value):
            await self._clear_osc_counters(symbol)
        if dec.transitioned:
            logger.info("[state_machine] %s %s → %s (%s)",
                        symbol, dec.prev_state, dec.state, dec.note)
        return dec

    async def get_state(self, symbol: str) -> Optional[FSMState]:
        return self._cache.get(symbol)

    def since_of(self, symbol: str) -> str:
        """当前状态的进入时间（观测用）。缓存为事件循环内单线程访问，直接读安全。"""
        st = self._cache.get(symbol)
        return st.since if st else ""

    async def reset(self, symbol: str, reason: str = "manual") -> None:
        """人工复位（规格 9.6：一键全平 → S0，计数器重置）。"""
        st = await self._load_state(symbol, "")
        st.state = MarketState.S0_IDLE.value
        st.since = datetime.now(timezone.utc).isoformat()
        st.pending_class = ""
        st.pending_streak = 0
        st.hold_only = False
        st.consecutive_fail = 0
        st.osc_atr_loss = 0.0
        st.age_bars = 0         # 人工复位后年龄归零（新状态尚未持有）
        st.last_bar_time = ""   # 允许复位后立即处理当前 bar
        await self._save_state(st)
        await self._clear_osc_counters(symbol)   # 规格 9.6：计数器一并重置
        logger.info("[state_machine] %s 人工复位 → S0_IDLE (%s)", symbol, reason)

    @property
    def tuning(self) -> dict:
        """当前生效参数（观测/自检用）。"""
        return {
            "k_enter": self._k_enter, "k_exit": self._k_exit, "k_fade": self._k_fade,
            "fail_bars": self._fail_bars, "osc_limit": self._osc_limit,
            "flat_reset_enabled": self._flat_reset,
        }
