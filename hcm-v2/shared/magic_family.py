"""Magic 族（前导逻辑码）—— **跨服务单一实现**。

为什么单独一个模块：同一条归族规则要被两个进程使用，若各写一份就是"同一规则
两份实现"（铁律第十三章）：
  · hcm-risk-engine：容器内按 compose 挂载 `/app/shared/magic_family.py` 导入
    （与 `shared/redis_client.py` 同模式）；
  · 主机 mt5_bridge：按绝对路径 importlib 加载**同一文件**（模式同 state_strategy）。

编码侧真值（本模块不复制布局细节，只依赖"逻辑码占最高两位十进制"这一契约）：
  · `signal_tower/state_strategy.encode_fsm_magic` —— FSM 8 位 `LL·SS·RR·TT`
    （LL=逻辑码, SS=FSM状态, RR=触发原因, TT=手数梯度档）；
  · `signal_publisher.SIGNAL_MODE_MAGIC` —— 11/12/21/55/61/62 基码。

用途（2026-09-16/17 需求）：风控「同向保本闸门」的判定维度由 (账户,方向) 细化为
(账户,方向,magic 族)——持仓单保本后按**同族**判断可否追单；不同族互不牵连。
"""

from __future__ import annotations

MAGIC_FAMILY_HEXP = 11            # hexp 乘幂引擎 M5 bar 收盘主信号
MAGIC_FAMILY_SCORING = 12         # 默认评分引擎
MAGIC_FAMILY_LIVE_OVERRIDE = 21   # bar 内实时触发
MAGIC_FAMILY_RANGE = 55           # RANGE 均值回归（range_strategy 注入）
MAGIC_FAMILY_STATE_OSC = 61       # FSM S1 箱体逆势单
MAGIC_FAMILY_STATE_TREND = 62     # FSM S2/S3/S4 顺势单（含加仓）
MAGIC_FAMILY_UNKNOWN = 0          # 未知 / 手动（调用方须回退旧口径，不得臆造）

MAGIC_FAMILIES: tuple = (
    MAGIC_FAMILY_HEXP, MAGIC_FAMILY_SCORING, MAGIC_FAMILY_LIVE_OVERRIDE,
    MAGIC_FAMILY_RANGE, MAGIC_FAMILY_STATE_OSC, MAGIC_FAMILY_STATE_TREND,
)

# FSM 8 位布局中逻辑码（LL）的权值：LL·10^6 + SS·10^4 + RR·10^2 + TT
_FSM_LEAD_MUL = 1000000


def magic_family(magic) -> int:
    """magic → 族（前导逻辑码）；0 = 未知/手动（调用方须回退旧口径）。

    · 裸基码（11/12/21/55/61/62）→ 原值；
    · FSM 8 位（如 62021100 = state_trend·S2·init_pullback·档0）→ 62021100 // 10^6 = 62；
    · 其余（0 / 123456 / None / 非数值）→ 0。
    """
    try:
        v = int(magic or 0)
    except (TypeError, ValueError):
        return MAGIC_FAMILY_UNKNOWN
    if v in MAGIC_FAMILIES:
        return v
    lead = v // _FSM_LEAD_MUL
    return lead if lead in MAGIC_FAMILIES else MAGIC_FAMILY_UNKNOWN
