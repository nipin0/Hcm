"""【已下线】共源信号增强引擎（Co-Source Signal Enhancer）— 仅保留共享常量。

【2026-08-28 双源信号模式清除】
原 CoSourceEngine（v1 apply / v2 apply_v2 收敛决策）整体下线，系统只保留 HEXP
（和乘幂）引擎。引擎类及其全部逻辑（F1–F5 过滤、G1 校准、G3 自适应门槛、
micro_state 收敛决策、周期位置否决等）已删除。

保留本文件的原因（架构约束，非历史遗留）：
  docker-compose.yml 将本文件 bind mount 进容器；若删除文件，容器启动时
  Docker 会因挂载源不存在而 **OCI runtime create failed** 无法启动
  （2026-08-28 实测）。故保留文件名，仅承载被下列生产模块依赖的共享常量：
    - signal_publisher.py（DB m5_regime → co.calib.* 配置键映射）
    - local_calibrator.py（本地校准回写）

常量归属说明：``_CALIB_KEYS`` 是「Regime 五态 → co.calib.* 配置键」映射，
为 HEXP 与发布/校准链路共用，与已删除的双源引擎无关。
"""

from __future__ import annotations

from signal_tower.regime_classifier import Regime

# ── G1 校准因子键（5 态 → 配置键）──
_CALIB_KEYS = {
    Regime.PRE_TREND: "co.calib.pre_trend",
    Regime.TREND: "co.calib.trend",
    Regime.TREND_FADE: "co.calib.trend_fade",
    Regime.RANGE: "co.calib.range",
    Regime.NEUTRAL: "co.calib.neutral",
}
_CALIB_DEFAULT = 1.0
_CALIB_MIN_DAYS_DEFAULT = 7

__all__ = ["_CALIB_KEYS", "_CALIB_DEFAULT", "_CALIB_MIN_DAYS_DEFAULT"]
